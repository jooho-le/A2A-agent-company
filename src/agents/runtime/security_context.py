"""Opt-in initial Security inputs from one frozen, read-only Host handoff.

Step 33 admits VALIDATING/fix_attempt=0/codeVersion=1 only. Continuation
attempts are independent of code revisions; revalidation belongs to step 35.
The loader never installs storage schemas or obtains a fresh Run budget.
"""

from dataclasses import dataclass, field
import json
import math

from a2a.server.agent_execution import RequestContext

from agents.api.validation import parse_workflow_metadata
from agents.llm.budget import ExecutionBudget
from agents.llm.content import sanitize_content
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, json_text
from agents.runtime.planner_context import _configuration, _json, _scenario, _text
from orchestrator.a2a.requests import A2AWorkflowMetadata, build_snapshot_handoff_data
from orchestrator.application.planner_output import PlannerPlan
from orchestrator.artifacts.sqlite_store import SQLiteArtifactContentStore
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.planning_artifacts import RequirementArtifact
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.scenario_registry import RequirementValidator
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact, SnapshotHandoff
from orchestrator.domain.states import A2ATaskState, AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository


class SecurityContextError(ValueError):
    """Stable denial only: no protected inputs, paths, SQL or provider prose."""

    code = "SECURITY_CONTEXT_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _dispatch_request(configuration, plan, scenario, requirement_ids):
    """Reproduce the existing Orchestrator Security dispatch without changing it."""
    run_configuration = configuration.to_artifact_json()
    run_configuration.pop("scenarioContract", None)
    run_configuration.pop("frozenScenarioContractJson", None)
    requirements = [
        item for item in plan.model_dump(mode="json", by_alias=True)["requirements"]
        if item["requirementId"] in {str(value) for value in requirement_ids}
    ]
    policies = {key: value for key, value in scenario.planner_contract().items()
                if key in {"securityPolicy", "emailPolicy"}}
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        f"workspaceId={configuration.workspace_id}\n"
        + "Run Configuration: " + encode(run_configuration) + "\n"
        + "검증 대상은 전달된 불변 Source Snapshot이다. 파일을 수정하지 말고 "
        "READ_ONLY로 접근한다. 요구사항과 Acceptance Criteria를 확인해 "
        "보안 취약점을 점검하고 Security 결과를 보고한다. 요구사항: " + encode(requirements)
        + "\n\n동결 보안/이메일 정책: " + encode(policies)
        + "\n\n완료 시 A2A Task Artifact를 정확히 하나 반환한다. "
        "QA는 qa-report.json, Security는 security-report.json을 사용하고, "
        "각 Artifact는 application/json Data Part 하나와 project metadata를 "
        "가져야 한다. Report Schema는 "
        "schemas/project/qa_report.schema.json 또는 "
        "schemas/project/security_report.schema.json을 따른다. "
        "Task COMPLETED는 업무 완료일 뿐 결과 PASS를 뜻하지 않는다."
    )


@dataclass(frozen=True, kw_only=True)
class SecurityExecutionContext:
    metadata: A2AWorkflowMetadata
    configuration: RunConfigurationArtifact = field(repr=False)
    budget: ExecutionBudget = field(repr=False)
    request_text: str = field(repr=False)
    requirement_artifact: RequirementArtifact = field(repr=False)
    source: CodeSnapshotArtifact = field(repr=False)
    _initial_payload_json: str = field(init=False, repr=False)

    def __post_init__(self):
        try:
            if (type(self.metadata) is not A2AWorkflowMetadata
                    or type(self.configuration) is not RunConfigurationArtifact
                    or type(self.requirement_artifact) is not RequirementArtifact
                    or type(self.source) is not CodeSnapshotArtifact
                    or not isinstance(self.budget, ExecutionBudget)):
                raise ValueError
            metadata = A2AWorkflowMetadata.model_validate(_json(self.metadata.model_dump_json(warnings=False)))
            configuration = _configuration(_json(self.configuration.model_dump_json(warnings=False)))
            requirement = RequirementArtifact.model_validate(_json(self.requirement_artifact.model_dump_json(warnings=False)))
            source = CodeSnapshotArtifact.model_validate(_json(self.source.model_dump_json(warnings=False)))
            if (metadata != self.metadata or configuration != self.configuration
                    or requirement != self.requirement_artifact or source != self.source
                    or metadata.run_id != configuration.run_id
                    or metadata.scenario_id != configuration.scenario_id
                    or metadata.code_version != 1 or source.code_version != 1
                    or source.run_id != metadata.run_id or requirement.run_id != metadata.run_id
                    or source.workflow_step_id == metadata.workflow_step_id
                    or requirement.workflow_step_id in (metadata.workflow_step_id, source.workflow_step_id)
                    or metadata.project_artifact_ids != (source.artifact_id,)
                    or source.artifact_version != 1 or source.previous_artifact_id is not None
                    or source.artifact_uri != f"artifact://{source.artifact_id}/source.tar"
                    or requirement.artifact_uri != f"artifact://{requirement.artifact_id}/requirements.json"):
                raise ValueError
            _text(self.request_text, max_bytes=1_048_576)
            for opaque_reference in (requirement.a2a_task_id, requirement.a2a_artifact_id,
                                     source.a2a_task_id, source.a2a_artifact_id):
                _text(opaque_reference)
            frozen = configuration.configuration
            if (frozen.model is None or frozen.limits.runtime_budget_ms is None
                    or frozen.environment is None or frozen.environment.network_policy != "DENY"
                    or source.container_image_digest != frozen.environment.container_image_digest
                    or source.dependency_lock_hash != frozen.environment.dependency_lock_hash):
                raise ValueError
            self.budget.check()
            remaining = self.budget.remaining_seconds()
            if not math.isfinite(remaining) or remaining > frozen.limits.runtime_budget_ms / 1000 + 0.001:
                raise ValueError
            for protected in (configuration, requirement, source):
                sanitize_content(protected.model_dump(mode="json"), reject_secrets=True)
            scenario = _scenario(configuration)
            plan = PlannerPlan.model_validate(requirement.payload)
            if (plan.model_dump(mode="json", by_alias=True) != requirement.payload
                    or tuple(item.requirement_id for item in plan.requirements) != scenario.requirement_ids
                    or requirement.requirement_ids != scenario.requirement_ids
                    or source.requirement_ids != scenario.requirement_ids
                    or metadata.requirement_ids != scenario.requirement_ids_for(RequirementValidator.SECURITY)):
                raise ValueError
            scenario.validate_planner_requirements(plan.requirements)
            payload = build_snapshot_handoff_data(
                SnapshotHandoff.from_snapshot(source), AgentRole.SECURITY,
                _dispatch_request(configuration, plan, scenario, metadata.requirement_ids),
            )
            object.__setattr__(self, "_initial_payload_json", json_text(payload))
        except LLMRuntimeError as error:
            if error.code is LLMErrorCode.BUDGET:
                raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
            raise SecurityContextError() from None
        except Exception:
            raise SecurityContextError() from None

    @property
    def initial_payload(self):
        return _json(self._initial_payload_json)

    @property
    def scenario(self):
        return _scenario(self.configuration)

    @property
    def model(self):
        return self.configuration.configuration.model


class SQLiteSecurityContextLoader:
    """Read one consistent SQLite snapshot and retain existing Run budget."""

    def __init__(self, repository, budget_resolver):
        if not isinstance(repository, SQLiteWorkflowRepository) or not callable(budget_resolver):
            raise SecurityContextError()
        self._repository, self._budget_resolver = repository, budget_resolver

    def __repr__(self):
        return "SQLiteSecurityContextLoader()"

    def __call__(self, context):
        try:
            if not isinstance(context, RequestContext):
                raise ValueError
            metadata = parse_workflow_metadata(context.metadata)
            _text(context.task_id)
            _text(context.context_id)
            with self._repository._connection() as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                run_row = connection.execute("SELECT status,payload_json FROM workflow_runs WHERE run_id=?",
                    (str(metadata.run_id),)).fetchone()
                step_rows = connection.execute("SELECT workflow_step_id,run_id,status,payload_json FROM workflow_steps WHERE run_id=?",
                    (str(metadata.run_id),)).fetchall()
                configuration_row = connection.execute("SELECT payload_json FROM run_configurations WHERE run_id=?",
                    (str(metadata.run_id),)).fetchone()
                if run_row is None or configuration_row is None:
                    raise ValueError
                run = WorkflowRun.model_validate(_json(run_row["payload_json"]))
                steps = [WorkflowStep.model_validate(_json(row["payload_json"])) for row in step_rows]
                if any(row["status"] != step.status.value or row["run_id"] != str(step.run_id)
                       or row["workflow_step_id"] != str(step.workflow_step_id)
                       for row, step in zip(step_rows, steps)):
                    raise ValueError
                active = [step for step in steps if step.agent_role is AgentRole.SECURITY
                          and step.status is WorkflowStepStatus.RUNNING]
                if len(active) != 1 or len(active[0].input_artifact_ids) != 1:
                    raise ValueError
                step = active[0]
                configuration = _configuration(_json(configuration_row["payload_json"]))
                source_row = connection.execute("SELECT * FROM project_artifacts WHERE artifact_id=? AND run_id=?",
                    (str(step.input_artifact_ids[0]), str(run.run_id))).fetchone()
                requirement_rows = connection.execute("SELECT * FROM project_artifacts WHERE run_id=? AND artifact_type='REQUIREMENT'",
                    (str(run.run_id),)).fetchall()
                if source_row is None or source_row["artifact_type"] != "SOURCE" or len(requirement_rows) != 1:
                    raise ValueError
                requirement_row = requirement_rows[0]
                source = CodeSnapshotArtifact.model_validate(_json(source_row["payload_json"]))
                requirement = RequirementArtifact.model_validate(_json(requirement_row["payload_json"]))
                private_row = connection.execute("SELECT * FROM artifact_contents WHERE artifact_id=? AND run_id=?",
                    (str(source.artifact_id), str(run.run_id))).fetchone()
                if private_row is None:
                    raise ValueError
                # Reuse the existing private store's bounded hash, environment
                # and exact QA/Security READ_ONLY-grant checks in this transaction.
                private_source = SQLiteArtifactContentStore._decode(connection, private_row).metadata
                if (type(private_source) is not CodeSnapshotArtifact
                        or private_source.a2a_task_id not in (None, source.a2a_task_id)
                        or private_source.a2a_artifact_id not in (None, source.a2a_artifact_id)
                        or private_source.model_copy(update={"a2a_task_id": source.a2a_task_id,
                            "a2a_artifact_id": source.a2a_artifact_id}) != source):
                    raise ValueError
                latest_private = connection.execute("SELECT artifact_id FROM artifact_contents WHERE run_id=? AND artifact_type='SOURCE' ORDER BY code_version DESC LIMIT 1",
                    (str(run.run_id),)).fetchone()
                latest_registry = connection.execute("SELECT artifact_id FROM project_artifacts WHERE run_id=? AND artifact_type='SOURCE' ORDER BY artifact_version DESC LIMIT 1",
                    (str(run.run_id),)).fetchone()
                if (latest_private is None or latest_registry is None
                        or latest_private["artifact_id"] != str(source.artifact_id)
                        or latest_registry["artifact_id"] != str(source.artifact_id)):
                    raise ValueError
                workspace_row = connection.execute("SELECT workspace_id,run_id,payload_json FROM workspaces WHERE workspace_id=?",
                    (str(run.workspace_id),)).fetchone()
                if workspace_row is None:
                    raise ValueError
                workspace = WorkspaceRecord.model_validate(_json(workspace_row["payload_json"]))
            planners = [producer for producer in steps if producer.agent_role is AgentRole.PLANNER]
            developers = [producer for producer in steps if producer.agent_role is AgentRole.DEVELOPER]
            if len(planners) != 1 or len(developers) != 1:
                raise ValueError
            planner, developer = planners[0], developers[0]
            if (run.run_id != metadata.run_id or run.scenario_id != metadata.scenario_id
                    or run.status is not WorkflowStatus.VALIDATING or run_row["status"] != run.status.value
                    or run.fix_attempt != 0 or run.code_version != 1
                    or step.run_id != run.run_id or step.workflow_step_id != metadata.workflow_step_id
                    or step.attempt != metadata.attempt or step.code_version != 1
                    or tuple(step.requirement_ids) != metadata.requirement_ids
                    or tuple(step.input_artifact_ids) != metadata.project_artifact_ids
                    or step.output_artifact_ids or step.a2a_artifact_ids
                    or configuration.run_id != run.run_id or configuration.scenario_id != run.scenario_id
                    or configuration.workspace_id != run.workspace_id
                    or workspace.run_id != run.run_id or workspace.workspace_id != run.workspace_id
                    or workspace_row["run_id"] != str(workspace.run_id)
                    or workspace_row["workspace_id"] != str(workspace.workspace_id)
                    or source.artifact_id != step.input_artifact_ids[0]
                    or any(row["artifact_id"] != str(artifact.artifact_id)
                           or row["run_id"] != str(artifact.run_id)
                           or row["artifact_type"] != artifact.artifact_type
                           or row["artifact_version"] != artifact.artifact_version
                           for row, artifact in ((source_row, source), (requirement_row, requirement)))
                    or source.run_id != run.run_id or source.workflow_step_id != developer.workflow_step_id
                    or developer.run_id != run.run_id or developer.status is not WorkflowStepStatus.SUCCEEDED
                    or developer.a2a_task_state is not A2ATaskState.COMPLETED
                    or developer.a2a_task_id != source.a2a_task_id
                    or source.a2a_artifact_id not in developer.a2a_artifact_ids
                    or source.artifact_id not in developer.output_artifact_ids
                    or developer.code_version != source.code_version
                    or tuple(developer.requirement_ids) != source.requirement_ids
                    or requirement.run_id != run.run_id or requirement.workflow_step_id != planner.workflow_step_id
                    or planner.run_id != run.run_id or planner.status is not WorkflowStepStatus.SUCCEEDED
                    or planner.a2a_task_state is not A2ATaskState.COMPLETED
                    or planner.a2a_task_id != requirement.a2a_task_id
                    or requirement.a2a_artifact_id not in planner.a2a_artifact_ids
                    or requirement.artifact_id not in planner.output_artifact_ids
                    or developer.input_artifact_ids != [requirement.artifact_id]
                    or step.a2a_task_id is not None and step.a2a_task_id != context.task_id
                    or step.agent_context_id is not None and step.agent_context_id != context.context_id):
                raise ValueError
            return SecurityExecutionContext(metadata=metadata, configuration=configuration,
                budget=self._budget_resolver(configuration), request_text=run.request_text,
                requirement_artifact=requirement, source=source)
        except LLMRuntimeError as error:
            if error.code is LLMErrorCode.BUDGET:
                raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
            raise SecurityContextError() from None
        except Exception:
            raise SecurityContextError() from None

