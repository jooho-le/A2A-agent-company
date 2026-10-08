"""Initial-candidate Developer Host inputs; read-only and explicitly opt-in.

Only IMPLEMENTING/fix_attempt=0/codeVersion=1 is admitted in step 31. A2A
continuation attempt is independent of codeVersion. FIXING and automatic
Pipeline wiring are later stages, not an implicit extension of this loader.
"""

from dataclasses import dataclass, field
import math

from a2a.server.agent_execution import RequestContext

from agents.api.validation import parse_workflow_metadata
from agents.llm.budget import ExecutionBudget
from agents.llm.content import sanitize_content
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, json_text
from agents.runtime.planner_context import _configuration, _json, _scenario, _text
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.application.dispatch import _DEVELOPER_OUTPUT_CONTRACT
from orchestrator.application.planner_output import PlannerPlan
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.planning_artifacts import RequirementArtifact
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.states import A2ATaskState, AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository


class DeveloperContextError(ValueError):
    """Stable reason only; no request, protected input, path or SQL details."""

    code = "DEVELOPER_CONTEXT_INVALID"

    def __init__(self):
        super().__init__(self.code)


@dataclass(frozen=True, kw_only=True)
class DeveloperExecutionContext:
    metadata: A2AWorkflowMetadata
    configuration: RunConfigurationArtifact = field(repr=False)
    budget: ExecutionBudget = field(repr=False)
    request_text: str = field(repr=False)
    requirement_artifact: RequirementArtifact = field(repr=False)
    _initial_payload_json: str = field(init=False, repr=False)

    def __post_init__(self):
        try:
            if (type(self.metadata) is not A2AWorkflowMetadata
                    or type(self.configuration) is not RunConfigurationArtifact
                    or type(self.requirement_artifact) is not RequirementArtifact
                    or not isinstance(self.budget, ExecutionBudget)):
                raise ValueError
            metadata = A2AWorkflowMetadata.model_validate(_json(self.metadata.model_dump_json(warnings=False)))
            configuration = _configuration(_json(self.configuration.model_dump_json(warnings=False)))
            artifact = RequirementArtifact.model_validate(_json(self.requirement_artifact.model_dump_json(warnings=False)))
            if (metadata != self.metadata or configuration != self.configuration
                    or artifact != self.requirement_artifact
                    or metadata.run_id != configuration.run_id
                    or metadata.scenario_id != configuration.scenario_id
                    or metadata.code_version != 1
                    or artifact.run_id != metadata.run_id
                    or artifact.workflow_step_id == metadata.workflow_step_id
                    or metadata.project_artifact_ids != (artifact.artifact_id,)
                    or artifact.artifact_uri != f"artifact://{artifact.artifact_id}/requirements.json"):
                raise ValueError
            _text(self.request_text, max_bytes=1_048_576)
            _text(artifact.a2a_task_id)
            _text(artifact.a2a_artifact_id)
            frozen = configuration.configuration
            if (frozen.model is None or frozen.limits.runtime_budget_ms is None
                    or frozen.environment is None or frozen.environment.network_policy != "DENY"):
                raise ValueError
            self.budget.check()
            remaining = self.budget.remaining_seconds()
            if not math.isfinite(remaining) or remaining > frozen.limits.runtime_budget_ms / 1000 + 0.001:
                raise ValueError
            sanitize_content(configuration.model_dump(mode="json"), reject_secrets=True)
            sanitize_content(artifact.model_dump(mode="json"), reject_secrets=True)
            scenario = _scenario(configuration)
            plan = PlannerPlan.model_validate(artifact.payload)
            canonical = plan.model_dump(mode="json", by_alias=True)
            if (canonical != artifact.payload
                    or tuple(requirement.requirement_id for requirement in plan.requirements) != scenario.requirement_ids
                    or artifact.requirement_ids != scenario.requirement_ids
                    or metadata.requirement_ids != scenario.requirement_ids):
                raise ValueError
            scenario.validate_planner_requirements(plan.requirements)
            payload = {
                "workspaceId": str(configuration.workspace_id),
                "scenario": scenario.planner_contract(),
                "runConfiguration": configuration.to_artifact_json(),
                "plan": canonical,
                # This is a Planner REQUIREMENT reference, not a SOURCE.
                "sourceArtifact": {
                    "a2aArtifactId": artifact.a2a_artifact_id,
                    "projectArtifactId": str(artifact.artifact_id),
                    "artifactVersion": artifact.artifact_version,
                },
                "outputContract": _DEVELOPER_OUTPUT_CONTRACT,
            }
            object.__setattr__(self, "_initial_payload_json", json_text(payload))
        except LLMRuntimeError as error:
            if error.code is LLMErrorCode.BUDGET:
                raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
            raise DeveloperContextError() from None
        except Exception:
            raise DeveloperContextError() from None

    @property
    def initial_payload(self):
        return _json(self._initial_payload_json)

    @property
    def plan(self):
        return PlannerPlan.model_validate(self.initial_payload["plan"])

    @property
    def scenario(self):
        return _scenario(self.configuration)

    @property
    def model(self):
        return self.configuration.configuration.model


class SQLiteDeveloperContextLoader:
    """One immutable read snapshot; no budget reset, writes or MCP factory.

    The initial Agent-assigned Task ID may not yet be observed by Orchestrator.
    Already stored Task/Context references must match byte-for-byte. A2A
    continuation must first update the Step's RUNNING status and attempt.
    """

    def __init__(self, repository, budget_resolver):
        if not isinstance(repository, SQLiteWorkflowRepository) or not callable(budget_resolver):
            raise DeveloperContextError()
        self._repository, self._budget_resolver = repository, budget_resolver

    def __repr__(self):
        return "SQLiteDeveloperContextLoader()"

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
                step_rows = connection.execute("SELECT status,payload_json FROM workflow_steps WHERE run_id=?",
                    (str(metadata.run_id),)).fetchall()
                configuration_row = connection.execute("SELECT payload_json FROM run_configurations WHERE run_id=?",
                    (str(metadata.run_id),)).fetchone()
                if run_row is None or configuration_row is None:
                    raise ValueError
                run = WorkflowRun.model_validate(_json(run_row["payload_json"]))
                steps = [WorkflowStep.model_validate(_json(row["payload_json"])) for row in step_rows]
                if any(row["status"] != step.status.value for row, step in zip(step_rows, steps)):
                    raise ValueError
                active = [step for step in steps if step.agent_role is AgentRole.DEVELOPER
                          and step.status is WorkflowStepStatus.RUNNING]
                if len(active) != 1:
                    raise ValueError
                step = active[0]
                configuration = _configuration(_json(configuration_row["payload_json"]))
                if len(step.input_artifact_ids) != 1:
                    raise ValueError
                artifact_row = connection.execute(
                    "SELECT artifact_id,run_id,artifact_type,payload_json FROM project_artifacts WHERE artifact_id=? AND run_id=?",
                    (str(step.input_artifact_ids[0]), str(run.run_id)),
                ).fetchone()
                if artifact_row is None or artifact_row["artifact_type"] != "REQUIREMENT":
                    raise ValueError
                artifact = RequirementArtifact.model_validate(_json(artifact_row["payload_json"]))
                workspace_row = connection.execute(
                    "SELECT workspace_id,run_id,payload_json FROM workspaces WHERE workspace_id=?",
                    (str(run.workspace_id),),
                ).fetchone()
                if workspace_row is None:
                    raise ValueError
                workspace = WorkspaceRecord.model_validate(_json(workspace_row["payload_json"]))
            planners = [producer for producer in steps if producer.agent_role is AgentRole.PLANNER]
            if len(planners) != 1:
                raise ValueError
            planner = planners[0]
            if (run.run_id != metadata.run_id or run.scenario_id != metadata.scenario_id
                    or run.status is not WorkflowStatus.IMPLEMENTING or run_row["status"] != run.status.value
                    or run.fix_attempt != 0 or run.code_version is not None
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
                    or artifact.artifact_id != step.input_artifact_ids[0]
                    or artifact_row["artifact_id"] != str(artifact.artifact_id)
                    or artifact_row["run_id"] != str(artifact.run_id)
                    or artifact.run_id != run.run_id or artifact.workflow_step_id != planner.workflow_step_id
                    or planner.run_id != run.run_id or planner.status is not WorkflowStepStatus.SUCCEEDED
                    or planner.a2a_task_state is not A2ATaskState.COMPLETED
                    or planner.a2a_task_id != artifact.a2a_task_id
                    or artifact.a2a_artifact_id not in planner.a2a_artifact_ids
                    or artifact.artifact_id not in planner.output_artifact_ids
                    or step.a2a_task_id is not None and step.a2a_task_id != context.task_id
                    or step.agent_context_id is not None and step.agent_context_id != context.context_id):
                raise ValueError
            return DeveloperExecutionContext(
                metadata=metadata, configuration=configuration,
                budget=self._budget_resolver(configuration), request_text=run.request_text,
                requirement_artifact=artifact,
            )
        except LLMRuntimeError as error:
            if error.code is LLMErrorCode.BUDGET:
                raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
            raise DeveloperContextError() from None
        except Exception:
            raise DeveloperContextError() from None
