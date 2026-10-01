"""Validate the project-level JSON contract returned by the Planner Agent."""

from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from a2a.types import Task, TaskState
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    UUID4,
    ValidationError,
    field_validator,
    model_validator,
)


class PlannerOutputValidationError(ValueError):
    """Raised when a completed Planner Task has no usable project Plan Artifact."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class PlanRequirement(_StrictModel):
    requirement_id: UUID4 = Field(alias="requirementId")
    key: str = Field(pattern=r"^REQ-[0-9]{3,}$")
    description: str = Field(min_length=1)
    acceptance_criteria: list[str] = Field(alias="acceptanceCriteria", min_length=1)

    @field_validator("description")
    @classmethod
    def description_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("description must not be blank")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def criteria_must_be_non_blank_and_unique(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("acceptance criteria must not be blank")
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("acceptance criteria must be unique per requirement")
        return normalized


class ImplementationTask(_StrictModel):
    task_id: str = Field(alias="taskId", pattern=r"^TASK-[A-Z0-9_-]{1,64}$")
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    requirement_ids: list[UUID4] = Field(alias="requirementIds", min_length=1)
    depends_on: list[str] = Field(default_factory=list, alias="dependsOn")

    @field_validator("title", "description")
    @classmethod
    def text_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("task title and description must not be blank")
        return value

    @field_validator("requirement_ids")
    @classmethod
    def requirement_ids_must_be_unique(cls, values: list[UUID4]) -> list[UUID4]:
        if len(values) != len(set(values)):
            raise ValueError("requirementIds must not contain duplicates")
        return values

    @field_validator("depends_on")
    @classmethod
    def dependencies_must_be_unique(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("dependsOn must not contain duplicates")
        return values


class PlannerPlan(_StrictModel):
    schema_version: Literal[1] = Field(alias="schemaVersion")
    requirements: list[PlanRequirement] = Field(min_length=1)
    implementation_plan: list[ImplementationTask] = Field(
        alias="implementationPlan", min_length=1
    )

    @model_validator(mode="after")
    def validate_references(self) -> "PlannerPlan":
        requirement_ids = [item.requirement_id for item in self.requirements]
        requirement_keys = [item.key for item in self.requirements]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("requirementId values must be unique")
        if len(requirement_keys) != len(set(requirement_keys)):
            raise ValueError("requirement keys must be unique")

        task_ids = [item.task_id for item in self.implementation_plan]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("taskId values must be unique")
        task_id_set = set(task_ids)
        requirement_id_set = set(requirement_ids)
        referenced_requirement_ids: set[UUID] = set()
        dependency_graph: dict[str, set[str]] = {}
        for task in self.implementation_plan:
            unknown_requirements = set(task.requirement_ids) - requirement_id_set
            if unknown_requirements:
                raise ValueError("implementation task references an unknown requirementId")
            referenced_requirement_ids.update(task.requirement_ids)
            dependencies = set(task.depends_on)
            if task.task_id in dependencies or dependencies - task_id_set:
                raise ValueError("task dependencies must reference other known taskIds")
            dependency_graph[task.task_id] = dependencies

        if referenced_requirement_ids != requirement_id_set:
            raise ValueError("every requirement must be covered by an implementation task")
        if _has_dependency_cycle(dependency_graph):
            raise ValueError("implementation task dependencies must be acyclic")
        return self


class ValidatedPlannerOutput(_StrictModel):
    a2a_artifact_id: str = Field(alias="a2aArtifactId", min_length=1)
    project_artifact_id: UUID4 = Field(alias="projectArtifactId")
    artifact_version: int = Field(alias="artifactVersion", ge=1, strict=True)
    plan: PlannerPlan


def parse_planner_output(
    task: Task,
    *,
    run_id: UUID,
    workflow_step_id: UUID,
) -> ValidatedPlannerOutput:
    """Find and validate the single requirements.json Artifact on a completed Task."""
    try:
        if TaskState.Name(task.status.state) != "TASK_STATE_COMPLETED":
            raise ValueError("Planner Task is not completed")
        artifacts = [
            artifact
            for artifact in task.artifacts
            if artifact.name == "requirements.json"
        ]
        if len(artifacts) != 1:
            raise ValueError("Planner Task must return exactly one requirements.json Artifact")
        artifact = artifacts[0]
        if not artifact.artifact_id.strip():
            raise ValueError("Planner Artifact must have an A2A artifactId")
        if len(artifact.parts) != 1:
            raise ValueError("requirements.json must contain exactly one JSON data Part")
        part = artifact.parts[0]
        if part.WhichOneof("content") != "data" or part.media_type != "application/json":
            raise ValueError("requirements.json must use one application/json data Part")

        metadata = _struct_mapping(artifact.metadata)
        if metadata.get("runId") != str(run_id):
            raise ValueError("Planner Artifact runId does not match the current Run")
        if metadata.get("workflowStepId") != str(workflow_step_id):
            raise ValueError("Planner Artifact workflowStepId does not match its Step")
        artifact_version = metadata.get("artifactVersion")
        if (
            isinstance(artifact_version, bool)
            or not isinstance(artifact_version, (int, float))
            or not float(artifact_version).is_integer()
        ):
            raise ValueError("Planner Artifact artifactVersion must be an integer")
        source = {
            "a2aArtifactId": artifact.artifact_id,
            "projectArtifactId": metadata.get("projectArtifactId"),
            "artifactVersion": int(artifact_version),
            "plan": MessageToDict(part.data),
        }
        return ValidatedPlannerOutput.model_validate(source)
    except (ValidationError, ValueError, TypeError) as exc:
        raise PlannerOutputValidationError("Planner output violates the project contract") from exc


def _struct_mapping(value: Struct) -> Mapping[str, object]:
    return MessageToDict(value)


def _has_dependency_cycle(graph: dict[str, set[str]]) -> bool:
    dependent_tasks: dict[str, set[str]] = {task_id: set() for task_id in graph}
    unresolved_dependencies = {
        task_id: len(dependencies) for task_id, dependencies in graph.items()
    }
    for task_id, dependencies in graph.items():
        for dependency in dependencies:
            dependent_tasks[dependency].add(task_id)

    ready = [task_id for task_id, count in unresolved_dependencies.items() if count == 0]
    resolved_count = 0
    while ready:
        task_id = ready.pop()
        resolved_count += 1
        for dependent in dependent_tasks[task_id]:
            unresolved_dependencies[dependent] -= 1
            if unresolved_dependencies[dependent] == 0:
                ready.append(dependent)
    return resolved_count != len(graph)
