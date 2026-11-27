"""Role output admission using the existing project parsers, never a verdict."""

from a2a.types import Task, TaskState
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict

from agents.api.validation import parse_workflow_metadata
from agents.roles.contracts import get_role_contract
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.application.developer_output import ValidatedDeveloperOutput, parse_developer_output
from orchestrator.application.planner_output import ValidatedPlannerOutput, parse_planner_output
from orchestrator.application.validation_output import parse_validation_output
from orchestrator.domain import (
    A2ATaskState, AgentRole, CodeSnapshotArtifact, QAReportArtifact,
    SecurityReportArtifact, WorkflowRun, WorkflowStep, WorkflowStepStatus,
)
from orchestrator.domain.scenario_registry import ScenarioDefinition
from orchestrator.domain.tool_evidence import ToolExecutionOutcome


class RoleOutputContractError(ValueError):
    """A generic, non-echoing failure of the role's structured output contract."""


ValidatedRoleOutput = (
    ValidatedPlannerOutput | ValidatedDeveloperOutput | QAReportArtifact | SecurityReportArtifact
)


def _require_execution_proof(outcome, evidence) -> None:
    if outcome.value in {"PASS", "FAIL"} and (
        evidence is None or evidence.outcome != ToolExecutionOutcome.PASS
    ):
        raise ValueError("A measured product result requires completed Tool execution")


def validate_completed_role_output(
    role: AgentRole, *, task: Task, run: WorkflowRun, step: WorkflowStep,
    scenario: ScenarioDefinition | None = None, source: CodeSnapshotArtifact | None = None,
) -> ValidatedRoleOutput:
    """Validate one completed response without storing it or selecting SUCCESS.

    scenario/source must come from the host's protected Run configuration and
    Artifact Registry, never model output. Format/cross-reference checks do not
    authenticate fabricated Tool evidence or fetch/verify Archive bytes. Those
    require the trusted execution/storage boundaries in subsequent steps.
    Optional Task metadata is checked when present; older response parsers do
    not require it. Actual Agent Task storage always supplies the binding.
    """
    try:
        contract = get_role_contract(role)
        if (
            step.agent_role != contract.role or step.run_id != run.run_id
            or step.status != WorkflowStepStatus.SUCCEEDED
            or step.a2a_task_state != A2ATaskState.COMPLETED
            or task.status.state != TaskState.TASK_STATE_COMPLETED
            or not task.id.strip() or task.id != step.a2a_task_id
            or not task.context_id.strip()
            or (step.agent_context_id is not None and task.context_id != step.agent_context_id)
        ):
            raise ValueError("Completed Task and service Workflow identity must match")
        metadata_json = MessageToDict(task.metadata)
        if metadata_json:
            actual = parse_workflow_metadata(metadata_json)
            expected = A2AWorkflowMetadata(
                run_id=run.run_id, workflow_step_id=step.workflow_step_id,
                scenario_id=run.scenario_id, attempt=step.attempt,
                requirement_ids=tuple(step.requirement_ids) or None,
                code_version=step.code_version,
                project_artifact_ids=tuple(step.input_artifact_ids) or None,
            )
            if actual != expected:
                raise ValueError("Task workflow metadata must preserve the host binding")
        if contract.role == AgentRole.PLANNER:
            if scenario is None or scenario.scenario_id != run.scenario_id:
                raise ValueError("The protected Run scenario baseline is required")
            result = parse_planner_output(
                task, run_id=run.run_id, workflow_step_id=step.workflow_step_id,
            )
            scenario.validate_planner_requirements(result.plan.requirements)
            return result
        if contract.role == AgentRole.DEVELOPER:
            result = parse_developer_output(task, run=run, step=step)
            _require_execution_proof(result.build_report.execution_outcome, result.build_report.tool_evidence)
            return result
        if source is None or source.run_id != run.run_id:
            raise ValueError("A registered Source Snapshot of the same Run is required")
        report = parse_validation_output(task, run=run, step=step, source=source)
        results = report.tests if isinstance(report, QAReportArtifact) else report.requirement_results
        for result in results:
            _require_execution_proof(result.outcome, result.tool_evidence)
        return report
    except (ValueError, TypeError, AttributeError, InvalidParamsError):
        raise RoleOutputContractError("Agent output violates the protected role contract") from None
