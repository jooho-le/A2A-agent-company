"""Protected initial/FIXING Developer inputs from one read-only Host snapshot.

Code revisions follow the approved three-cycle policy. A2A continuation attempt
is independent of fix_attempt; this loader never allocates a new Run budget.
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
from orchestrator.application.dispatch import _DEVELOPER_OUTPUT_CONTRACT, PlannerRunDispatcher
from orchestrator.application.planner_output import PlannerPlan
from orchestrator.artifacts.sqlite_store import SQLiteArtifactContentStore
from orchestrator.domain.developer_artifacts import BuildReportArtifact, ChangeReportArtifact
from orchestrator.domain.issues import IssueRecord
from orchestrator.domain.models import AgentContext, WorkflowRun, WorkflowStep
from orchestrator.domain.planning_artifacts import RequirementArtifact
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.states import A2ATaskState, AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.validation_artifacts import QAReportArtifact, SecurityReportArtifact
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository


class DeveloperContextError(ValueError):
    """Stable reason only; no request, protected input, path or SQL details."""

    code = "DEVELOPER_CONTEXT_INVALID"

    def __init__(self):
        super().__init__(self.code)


_PREVIOUS_MODELS = {
    "SOURCE": CodeSnapshotArtifact, "CHANGE_REPORT": ChangeReportArtifact,
    "BUILD_REPORT": BuildReportArtifact, "QA_REPORT": QAReportArtifact,
    "SECURITY_REPORT": SecurityReportArtifact,
}


def _previous_inputs(metadata, previous_source, previous_artifacts, fix_issues, fix_attempt):
    """Structural admission only; SQLite loader authenticates stored ownership."""
    if type(fix_attempt) is not int or not 0 <= fix_attempt <= 3:
        raise ValueError
    if metadata.code_version != fix_attempt + 1:
        raise ValueError
    if type(previous_artifacts) is not tuple or type(fix_issues) is not tuple:
        raise ValueError
    if fix_attempt == 0:
        if previous_source is not None or previous_artifacts or fix_issues:
            raise ValueError
        return
    if (type(previous_source) is not CodeSnapshotArtifact or not 3 <= len(previous_artifacts) <= 5
            or not fix_issues or len(fix_issues) > 256
            or metadata.project_artifact_ids != tuple(item.artifact_id for item in previous_artifacts)
            or len({item.artifact_id for item in previous_artifacts}) != len(previous_artifacts)):
        raise ValueError
    by_type = {}
    for item in previous_artifacts:
        if type(item) not in _PREVIOUS_MODELS.values() or item.artifact_type in by_type:
            raise ValueError
        copied = type(item).model_validate(_json(item.model_dump_json(warnings=False)))
        if (copied != item or item.run_id != metadata.run_id or item.code_version != fix_attempt
                or item.workflow_step_id == metadata.workflow_step_id):
            raise ValueError
        _text(item.a2a_task_id)
        _text(item.a2a_artifact_id)
        sanitize_content(item.model_dump(mode="json"), reject_secrets=True)
        by_type[item.artifact_type] = item
    if (not {"SOURCE", "CHANGE_REPORT", "BUILD_REPORT"}.issubset(by_type)
            or by_type["SOURCE"] != previous_source
            or previous_source.artifact_version != fix_attempt
            or previous_source.artifact_uri != f"artifact://{previous_source.artifact_id}/source.tar"):
        raise ValueError
    for kind in ("CHANGE_REPORT", "BUILD_REPORT"):
        item = by_type[kind]
        if (item.workflow_step_id != previous_source.workflow_step_id
                or item.a2a_task_id != previous_source.a2a_task_id
                or item.requirement_ids != previous_source.requirement_ids):
            raise ValueError
    for kind in ("BUILD_REPORT", "QA_REPORT", "SECURITY_REPORT"):
        if kind in by_type and by_type[kind].execution_manifest != previous_source.execution_manifest():
            raise ValueError
    if len({item.issue_id for item in fix_issues}) != len(fix_issues):
        raise ValueError
    for issue in fix_issues:
        if type(issue) is not IssueRecord or IssueRecord.model_validate(_json(issue.model_dump_json())) != issue:
            raise ValueError
        if (issue.run_id != metadata.run_id or issue.code_version != fix_attempt
                or issue.source_artifact_id != previous_source.artifact_id
                or not set(issue.requirement_ids).issubset(previous_source.requirement_ids)
                or issue.fixed_by is not AgentRole.DEVELOPER
                or issue.fix_workflow_step_id != metadata.workflow_step_id
                or issue.previous_code_version != fix_attempt or issue.new_code_version != fix_attempt + 1
                or issue.consecutive_repeat_count >= 2):
            raise ValueError
        expected_report = {AgentRole.DEVELOPER: "BUILD_REPORT", AgentRole.QA: "QA_REPORT",
                           AgentRole.SECURITY: "SECURITY_REPORT"}.get(issue.reporter)
        if (expected_report not in by_type or issue.report_artifact_id != by_type[expected_report].artifact_id):
            raise ValueError
        sanitize_content(issue.model_dump(mode="json"), reject_secrets=True)


def _load_fix_inputs(connection, run, step, steps, context):
    """Authenticate the exact journaled Fix Request before resolving budget."""
    if (not 1 <= run.fix_attempt <= 3 or run.code_version != run.fix_attempt
            or step.code_version != run.fix_attempt + 1):
        raise ValueError
    rows = connection.execute("SELECT * FROM project_artifacts WHERE run_id=? ORDER BY rowid",
                              (str(run.run_id),)).fetchall()
    previous = []
    for row in rows:
        if row["artifact_type"] not in _PREVIOUS_MODELS:
            continue
        item = _PREVIOUS_MODELS[row["artifact_type"]].model_validate(_json(row["payload_json"]))
        if (row["run_id"] != str(item.run_id) or row["artifact_id"] != str(item.artifact_id)
                or row["artifact_type"] != item.artifact_type or row["artifact_version"] != item.artifact_version
                or item.run_id != run.run_id):
            raise ValueError
        if item.code_version == run.code_version:
            previous.append(item)
        elif item.code_version > run.code_version:
            raise ValueError
    if tuple(item.artifact_id for item in previous) != tuple(step.input_artifact_ids):
        raise ValueError
    sources = [item for item in previous if type(item) is CodeSnapshotArtifact]
    if len(sources) != 1:
        raise ValueError
    source = sources[0]
    producers = {item.workflow_step_id: item for item in steps}
    for item in previous:
        producer = producers.get(item.workflow_step_id)
        role = AgentRole.QA if type(item) is QAReportArtifact else (
            AgentRole.SECURITY if type(item) is SecurityReportArtifact else AgentRole.DEVELOPER)
        if (producer is None or producer.agent_role is not role
                or producer.status is not WorkflowStepStatus.SUCCEEDED
                or producer.a2a_task_state is not A2ATaskState.COMPLETED
                or producer.code_version != run.code_version
                or producer.a2a_task_id != item.a2a_task_id
                or item.artifact_id not in producer.output_artifact_ids
                or item.a2a_artifact_id not in producer.a2a_artifact_ids):
            raise ValueError
    previous_developer = producers[source.workflow_step_id]
    if (previous_developer.agent_context_id is None
            or context.context_id != previous_developer.agent_context_id
            or context.task_id in {item.a2a_task_id for item in steps
                                  if item.agent_role is AgentRole.DEVELOPER and item.workflow_step_id != step.workflow_step_id}):
        raise ValueError
    mapping_row = connection.execute("SELECT * FROM agent_contexts WHERE run_id=? AND agent_id='developer'",
                                     (str(run.run_id),)).fetchone()
    if mapping_row is None:
        raise ValueError
    mapping = AgentContext.model_validate(_json(mapping_row["payload_json"]))
    # A2A_MESSAGE_SENT persists the reused Context before the Agent has assigned
    # and returned its new Task. None is legitimate only in that observation gap.
    allowed_tasks = (previous_developer.a2a_task_id, context.task_id)
    if step.a2a_task_id is None:
        allowed_tasks = (None, *allowed_tasks)
    if (mapping.run_id != run.run_id or mapping.agent_id != "developer"
            or mapping_row["run_id"] != str(mapping.run_id) or mapping_row["agent_id"] != mapping.agent_id
            or mapping.agent_context_id != context.context_id
            or mapping.latest_a2a_task_id not in allowed_tasks):
        raise ValueError
    private_row = connection.execute("SELECT * FROM artifact_contents WHERE artifact_id=? AND run_id=?",
                                     (str(source.artifact_id), str(run.run_id))).fetchone()
    if private_row is None:
        raise ValueError
    private_source = SQLiteArtifactContentStore._decode(connection, private_row).metadata
    if (type(private_source) is not CodeSnapshotArtifact
            or private_source.a2a_task_id not in (None, source.a2a_task_id)
            or private_source.a2a_artifact_id not in (None, source.a2a_artifact_id)
            or private_source.model_copy(update={"a2a_task_id": source.a2a_task_id,
                "a2a_artifact_id": source.a2a_artifact_id}) != source):
        raise ValueError
    latest = connection.execute("SELECT artifact_id FROM artifact_contents WHERE run_id=? AND artifact_type='SOURCE' ORDER BY code_version DESC LIMIT 1",
                                (str(run.run_id),)).fetchone()
    if latest is None or latest["artifact_id"] != str(source.artifact_id):
        raise ValueError
    issues = []
    issue_events = connection.execute("SELECT * FROM issue_events WHERE run_id=? AND event_type='FIX_REQUESTED' ORDER BY sequence",
                                      (str(run.run_id),)).fetchall()
    for event in issue_events:
        changes = _json(event["payload_json"])
        if changes.get("fix_workflow_step_id") != str(step.workflow_step_id):
            continue
        if (set(changes) != {"fixed_by", "fix_workflow_step_id", "previous_code_version", "new_code_version"}
                or changes["fixed_by"] != AgentRole.DEVELOPER.value
                or type(changes["previous_code_version"]) is not int or changes["previous_code_version"] != run.code_version
                or type(changes["new_code_version"]) is not int or changes["new_code_version"] != step.code_version):
            raise ValueError
        issue_row = connection.execute("SELECT * FROM issue_records WHERE issue_id=? AND run_id=?",
                                       (event["issue_id"], str(run.run_id))).fetchone()
        if issue_row is None:
            raise ValueError
        issue = IssueRecord.model_validate(_json(issue_row["payload_json"]))
        if (issue_row["issue_id"] != str(issue.issue_id) or issue_row["run_id"] != str(issue.run_id)
                or issue_row["code_version"] != issue.code_version
                or issue_row["fingerprint"] != issue.fingerprint
                or issue_row["consecutive_repeat_count"] != issue.consecutive_repeat_count):
            raise ValueError
        issues.append(IssueRecord.model_validate({**issue.model_dump(), **changes}))
    return source, tuple(previous), tuple(issues)


@dataclass(frozen=True, kw_only=True)
class DeveloperExecutionContext:
    metadata: A2AWorkflowMetadata
    configuration: RunConfigurationArtifact = field(repr=False)
    budget: ExecutionBudget = field(repr=False)
    request_text: str = field(repr=False)
    requirement_artifact: RequirementArtifact = field(repr=False)
    fix_attempt: int = 0
    previous_source: CodeSnapshotArtifact | None = field(default=None, repr=False)
    previous_artifacts: tuple = field(default=(), repr=False)
    fix_issues: tuple[IssueRecord, ...] = field(default=(), repr=False)
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
                    or artifact.run_id != metadata.run_id
                    or artifact.workflow_step_id == metadata.workflow_step_id
                    or artifact.artifact_uri != f"artifact://{artifact.artifact_id}/requirements.json"):
                raise ValueError
            _previous_inputs(metadata, self.previous_source, self.previous_artifacts, self.fix_issues, self.fix_attempt)
            if self.fix_attempt == 0 and metadata.project_artifact_ids != (artifact.artifact_id,):
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
                    or metadata.requirement_ids != scenario.requirement_ids
                    or self.previous_source is not None and (
                        self.previous_source.requirement_ids != scenario.requirement_ids
                        or self.previous_source.container_image_digest != frozen.environment.container_image_digest
                        or self.previous_source.dependency_lock_hash != frozen.environment.dependency_lock_hash)):
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
            if self.fix_attempt:
                payload = PlannerRunDispatcher.build_fix_payload(
                    WorkflowRun(run_id=metadata.run_id, workspace_id=configuration.workspace_id,
                        scenario_id=metadata.scenario_id, request_text=self.request_text,
                        status=WorkflowStatus.FIXING, fix_attempt=self.fix_attempt,
                        code_version=self.previous_source.code_version),
                    None, plan, scenario, self.fix_issues, self.previous_artifacts)
                payload["runConfiguration"] = configuration.to_artifact_json()
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
                active = [step for step in steps if step.agent_role is AgentRole.DEVELOPER
                          and step.status is WorkflowStepStatus.RUNNING]
                if len(active) != 1:
                    raise ValueError
                step = active[0]
                configuration = _configuration(_json(configuration_row["payload_json"]))
                requirement_rows = connection.execute(
                    "SELECT * FROM project_artifacts WHERE artifact_type='REQUIREMENT' AND run_id=?",
                    (str(run.run_id),)).fetchall()
                if len(requirement_rows) != 1:
                    raise ValueError
                artifact_row = requirement_rows[0]
                artifact = RequirementArtifact.model_validate(_json(artifact_row["payload_json"]))
                previous_source, previous_artifacts, fix_issues = None, (), ()
                if run.status is WorkflowStatus.FIXING:
                    previous_source, previous_artifacts, fix_issues = _load_fix_inputs(connection, run, step, steps, context)
                elif (run.status is not WorkflowStatus.IMPLEMENTING or run.fix_attempt != 0
                      or run.code_version is not None or tuple(step.input_artifact_ids) != (artifact.artifact_id,)):
                    raise ValueError
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
                    or run_row["status"] != run.status.value
                    or step.run_id != run.run_id or step.workflow_step_id != metadata.workflow_step_id
                    or step.attempt != metadata.attempt or step.code_version != run.fix_attempt + 1
                    or tuple(step.requirement_ids) != metadata.requirement_ids
                    or tuple(step.input_artifact_ids) != metadata.project_artifact_ids
                    or step.output_artifact_ids or step.a2a_artifact_ids
                    or configuration.run_id != run.run_id or configuration.scenario_id != run.scenario_id
                    or configuration.workspace_id != run.workspace_id
                    or workspace.run_id != run.run_id or workspace.workspace_id != run.workspace_id
                    or workspace_row["run_id"] != str(workspace.run_id)
                    or workspace_row["workspace_id"] != str(workspace.workspace_id)
                    or artifact_row["artifact_id"] != str(artifact.artifact_id)
                    or artifact_row["run_id"] != str(artifact.run_id)
                    or artifact_row["artifact_version"] != artifact.artifact_version
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
                requirement_artifact=artifact, fix_attempt=run.fix_attempt,
                previous_source=previous_source, previous_artifacts=previous_artifacts, fix_issues=fix_issues,
            )
        except LLMRuntimeError as error:
            if error.code is LLMErrorCode.BUDGET:
                raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
            raise DeveloperContextError() from None
        except Exception:
            raise DeveloperContextError() from None
