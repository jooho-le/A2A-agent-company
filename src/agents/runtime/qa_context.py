"""Frozen QA inputs for initial validation and bounded candidate revalidation.

Continuation attempts are independent of the three code-fix cycles. The
loader never installs schemas, changes a Registry or obtains a fresh budget.
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
from orchestrator.domain.models import AgentContext, WorkflowRun, WorkflowStep
from orchestrator.domain.planning_artifacts import RequirementArtifact
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.scenario_registry import RequirementValidator
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact, SnapshotHandoff
from orchestrator.domain.states import A2ATaskState, AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.validation_artifacts import QAReportArtifact
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository


class QAContextError(ValueError):
    """Stable denial only: no protected inputs, paths, SQL or provider prose."""

    code = "QA_CONTEXT_INVALID"

    def __init__(self):
        super().__init__(self.code)


def validation_cycle(run):
    """Native fix count and current candidate, never A2A Step.attempt."""
    if (type(run.fix_attempt) is not int or not 0 <= run.fix_attempt <= 3
            or type(run.code_version) is not int or run.code_version != run.fix_attempt + 1
            or run.status is not (WorkflowStatus.VALIDATING if run.fix_attempt == 0
                                  else WorkflowStatus.REVALIDATING)):
        raise ValueError
    return run.fix_attempt


def _context_lineage(execution, report_type):
    source = execution.source
    if (type(execution.fix_attempt) is not int or not 0 <= execution.fix_attempt <= 3
            or source.code_version != execution.fix_attempt + 1
            or source.artifact_version != source.code_version):
        raise ValueError
    previous = execution.previous_source
    if source.code_version == 1:
        if previous is not None or source.previous_artifact_id is not None:
            raise ValueError
    else:
        if type(previous) is not CodeSnapshotArtifact:
            raise ValueError
        copied = CodeSnapshotArtifact.model_validate(_json(previous.model_dump_json(warnings=False)))
        if (copied != previous or previous.artifact_id != source.previous_artifact_id
                or previous.run_id != source.run_id or previous.code_version != source.code_version - 1
                or previous.artifact_version != source.artifact_version - 1
                or previous.workflow_step_id == source.workflow_step_id
                or previous.repository_id != source.repository_id
                or previous.git_object_format != source.git_object_format
                or previous.requirement_ids != source.requirement_ids
                or previous.container_image_digest != source.container_image_digest
                or previous.dependency_lock_hash != source.dependency_lock_hash):
            raise ValueError
        sanitize_content(previous.model_dump(mode="json"), reject_secrets=True)
    report = execution.previous_report
    report_source = execution.previous_report_source
    if report is None and report_source is not None:
        raise ValueError
    if report is not None:
        if type(report) is not report_type:
            raise ValueError
        copied = report_type.model_validate(_json(report.model_dump_json(warnings=False)))
        if (copied != report or report.run_id != source.run_id
                or not 1 <= report.code_version < source.code_version
                or report.workflow_step_id == execution.metadata.workflow_step_id
                or report.requirement_ids != execution.metadata.requirement_ids
                or report.execution_manifest.project_artifact_id == source.artifact_id
                or report.execution_manifest.repository_id != source.repository_id
                or report.execution_manifest.container_image_digest != source.container_image_digest
                or report.execution_manifest.dependency_lock_hash != source.dependency_lock_hash):
            raise ValueError
        # A skipped Build can leave the most recent report older than the
        # immediate Source predecessor. That older target must be explicit;
        # an arbitrary same-environment Manifest is not an authenticated link.
        target = previous if report_source is None and previous is not None and (
            report.code_version == previous.code_version) else report_source
        if type(target) is not CodeSnapshotArtifact:
            raise ValueError
        copied_target = CodeSnapshotArtifact.model_validate(_json(target.model_dump_json(warnings=False)))
        if (copied_target != target or target.run_id != source.run_id
                or target.code_version != report.code_version
                or target.artifact_version != target.code_version
                or target.artifact_id == source.artifact_id
                or target.workflow_step_id == report.workflow_step_id
                or target.repository_id != source.repository_id
                or target.git_object_format != source.git_object_format
                or target.requirement_ids != source.requirement_ids
                or target.container_image_digest != source.container_image_digest
                or target.dependency_lock_hash != source.dependency_lock_hash
                or target.artifact_uri != f"artifact://{target.artifact_id}/source.tar"
                or previous is not None and target.code_version == previous.code_version and target != previous
                or report.execution_manifest != target.execution_manifest()):
            raise ValueError
        _text(target.a2a_task_id)
        _text(target.a2a_artifact_id)
        sanitize_content(target.model_dump(mode="json"), reject_secrets=True)
        sanitize_content(report.model_dump(mode="json"), reject_secrets=True)


def _registered_row(row, artifact):
    if (row["artifact_id"] != str(artifact.artifact_id) or row["run_id"] != str(artifact.run_id)
            or row["artifact_type"] != artifact.artifact_type
            or row["artifact_version"] != artifact.artifact_version):
        raise ValueError


def _read_validation_lineage(connection, run, steps, source, role, report_type, requirement_ids):
    """Read complete Source/report ancestry in the caller's SQLite snapshot.

    Report versions count reports that actually exist, not code candidates:
    a Build failure can skip validation and leave the first report at code 2.
    """
    validation_cycle(run)
    if source.code_version != run.code_version:
        raise ValueError
    by_step = {step.workflow_step_id: step for step in steps}
    source_rows = connection.execute(
        "SELECT * FROM project_artifacts WHERE run_id=? AND artifact_type='SOURCE' ORDER BY artifact_version",
        (str(run.run_id),)).fetchall()
    if len(source_rows) != run.code_version:
        raise ValueError
    sources, previous = {}, None
    for version, row in enumerate(source_rows, 1):
        item = CodeSnapshotArtifact.model_validate(_json(row["payload_json"]))
        _registered_row(row, item)
        producer = by_step.get(item.workflow_step_id)
        if (item.run_id != run.run_id or item.code_version != version or item.artifact_version != version
                or item.previous_artifact_id != (None if previous is None else previous.artifact_id)
                or producer is None or producer.agent_role is not AgentRole.DEVELOPER
                or producer.status is not WorkflowStepStatus.SUCCEEDED
                or producer.a2a_task_state is not A2ATaskState.COMPLETED
                or producer.a2a_task_id != item.a2a_task_id or item.a2a_task_id is None
                or item.a2a_artifact_id is None or item.a2a_artifact_id not in producer.a2a_artifact_ids
                or item.artifact_id not in producer.output_artifact_ids
                or producer.code_version != item.code_version
                or tuple(producer.requirement_ids) != item.requirement_ids
                or previous is not None and previous.artifact_id not in producer.input_artifact_ids
                or item.repository_id != source.repository_id or item.requirement_ids != source.requirement_ids
                or item.git_object_format != source.git_object_format
                or item.container_image_digest != source.container_image_digest
                or item.dependency_lock_hash != source.dependency_lock_hash):
            raise ValueError
        private_row = connection.execute("SELECT * FROM artifact_contents WHERE artifact_id=? AND run_id=?",
            (str(item.artifact_id), str(run.run_id))).fetchone()
        if private_row is None:
            raise ValueError
        private = SQLiteArtifactContentStore._decode(connection, private_row).metadata
        if (type(private) is not CodeSnapshotArtifact
                or private.a2a_task_id not in (None, item.a2a_task_id)
                or private.a2a_artifact_id not in (None, item.a2a_artifact_id)
                or private.model_copy(update={"a2a_task_id": item.a2a_task_id,
                    "a2a_artifact_id": item.a2a_artifact_id}) != item):
            raise ValueError
        sources[item.artifact_id] = item
        previous = item
    if previous != source:
        raise ValueError
    previous_source = None if source.code_version == 1 else sources[source.previous_artifact_id]
    rows = connection.execute("SELECT * FROM project_artifacts WHERE run_id=? AND artifact_type=? ORDER BY artifact_version",
        (str(run.run_id), "QA_REPORT" if role is AgentRole.QA else "SECURITY_REPORT")).fetchall()
    previous_report = None
    for version, row in enumerate(rows, 1):
        report = report_type.model_validate(_json(row["payload_json"]))
        _registered_row(row, report)
        target = sources.get(report.execution_manifest.project_artifact_id)
        producer = by_step.get(report.workflow_step_id)
        if (report.run_id != run.run_id or report.artifact_version != version
                or report.previous_artifact_id != (None if previous_report is None else previous_report.artifact_id)
                or not 1 <= report.code_version < source.code_version
                or previous_report is not None and report.code_version <= previous_report.code_version
                or report.requirement_ids != requirement_ids or target is None
                or report.execution_manifest != target.execution_manifest()
                or producer is None or producer.agent_role is not role
                or producer.status is not WorkflowStepStatus.SUCCEEDED
                or producer.a2a_task_state is not A2ATaskState.COMPLETED
                or producer.a2a_task_id != report.a2a_task_id
                or report.a2a_artifact_id not in producer.a2a_artifact_ids
                or report.artifact_id not in producer.output_artifact_ids
                or producer.code_version != report.code_version
                or tuple(producer.requirement_ids) != report.requirement_ids
                or producer.input_artifact_ids != [target.artifact_id]):
            raise ValueError
        previous_report = report
    previous_report_source = None if previous_report is None else sources[
        previous_report.execution_manifest.project_artifact_id]
    return previous_source, previous_report, previous_report_source


def _verify_role_context(connection, run, steps, active, context, role):
    """Bind every prior role Task/Context and the Host's observed mapping.

    A2A_MESSAGE_SENT may persist latest Task=None before the new Agent Task is
    observed, but it cannot authorize another Context or any historical Task.
    """
    previous = [step for step in steps if step.agent_role is role
                and step.workflow_step_id != active.workflow_step_id]
    task_ids = [step.a2a_task_id for step in previous if step.a2a_task_id is not None]
    if len(task_ids) != len(set(task_ids)) or context.task_id in task_ids:
        raise ValueError
    contexts = {step.agent_context_id for step in previous if step.agent_context_id is not None}
    if (any(step.a2a_task_id is not None and step.agent_context_id is None for step in previous)
            or len(contexts) > 1 or contexts and contexts != {context.context_id}
            or active.agent_context_id is not None and active.agent_context_id != context.context_id):
        raise ValueError
    agent_id = role.value.lower()
    row = connection.execute("SELECT * FROM agent_contexts WHERE run_id=? AND agent_id=?",
                             (str(run.run_id), agent_id)).fetchone()
    if row is None:
        if contexts:
            raise ValueError
        return
    mapping = AgentContext.model_validate(_json(row["payload_json"]))
    observed = [step for step in previous if step.a2a_task_id is not None]
    latest_previous = None if not observed else max(observed,
        key=lambda step: (step.code_version or 0, step.created_at)).a2a_task_id
    allowed_tasks = {context.task_id}
    if latest_previous is not None:
        allowed_tasks.add(latest_previous)
    if active.a2a_task_id is None:
        allowed_tasks.add(None)
    if (row["run_id"] != str(mapping.run_id) or row["agent_id"] != mapping.agent_id
            or mapping.run_id != run.run_id or mapping.agent_id != agent_id
            or mapping.agent_context_id is not None and mapping.agent_context_id != context.context_id
            or contexts and mapping.agent_context_id != context.context_id
            or mapping.latest_a2a_task_id not in allowed_tasks):
        raise ValueError


def verify_report_predecessor(repository, execution, role, report_type):
    """Services repeat actual registered report ancestry before publication."""
    with repository._connection() as connection:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        rows = connection.execute("SELECT * FROM project_artifacts WHERE run_id=? AND artifact_type=? ORDER BY artifact_version",
            (str(execution.metadata.run_id), "QA_REPORT" if role is AgentRole.QA else "SECURITY_REPORT")).fetchall()
        previous = None
        for version, row in enumerate(rows, 1):
            report = report_type.model_validate(_json(row["payload_json"]))
            _registered_row(row, report)
            if (report.artifact_version != version or report.run_id != execution.metadata.run_id
                    or report.previous_artifact_id != (None if previous is None else previous.artifact_id)
                    or report.requirement_ids != execution.metadata.requirement_ids
                    or report.code_version >= execution.source.code_version
                    or previous is not None and report.code_version <= previous.code_version):
                raise ValueError
            previous = report
        if previous != getattr(execution, "previous_report", None):
            raise ValueError
    return 1 if previous is None else previous.artifact_version + 1


def _dispatch_request(configuration, plan, scenario, requirement_ids):
    """Reproduce the existing Orchestrator QA dispatch without changing it."""
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
        "기능 테스트를 수행하고 QA 결과를 보고한다. 요구사항: " + encode(requirements)
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
class QAExecutionContext:
    metadata: A2AWorkflowMetadata
    configuration: RunConfigurationArtifact = field(repr=False)
    budget: ExecutionBudget = field(repr=False)
    request_text: str = field(repr=False)
    requirement_artifact: RequirementArtifact = field(repr=False)
    source: CodeSnapshotArtifact = field(repr=False)
    previous_source: CodeSnapshotArtifact | None = field(default=None, repr=False)
    previous_report: QAReportArtifact | None = field(default=None, repr=False)
    previous_report_source: CodeSnapshotArtifact | None = field(default=None, repr=False)
    fix_attempt: int = 0
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
                    or metadata.code_version != source.code_version
                    or source.run_id != metadata.run_id or requirement.run_id != metadata.run_id
                    or source.workflow_step_id == metadata.workflow_step_id
                    or requirement.workflow_step_id in (metadata.workflow_step_id, source.workflow_step_id)
                    or metadata.project_artifact_ids != (source.artifact_id,)
                    or source.artifact_uri != f"artifact://{source.artifact_id}/source.tar"
                    or requirement.artifact_uri != f"artifact://{requirement.artifact_id}/requirements.json"):
                raise ValueError
            _context_lineage(self, QAReportArtifact)
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
                    or metadata.requirement_ids != scenario.requirement_ids_for(RequirementValidator.QA)):
                raise ValueError
            scenario.validate_planner_requirements(plan.requirements)
            payload = build_snapshot_handoff_data(
                SnapshotHandoff.from_snapshot(source), AgentRole.QA,
                _dispatch_request(configuration, plan, scenario, metadata.requirement_ids),
            )
            object.__setattr__(self, "_initial_payload_json", json_text(payload))
        except LLMRuntimeError as error:
            if error.code is LLMErrorCode.BUDGET:
                raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
            raise QAContextError() from None
        except Exception:
            raise QAContextError() from None

    @property
    def initial_payload(self):
        return _json(self._initial_payload_json)

    @property
    def scenario(self):
        return _scenario(self.configuration)

    @property
    def model(self):
        return self.configuration.configuration.model


class SQLiteQAContextLoader:
    """Read one consistent SQLite snapshot and retain existing Run budget."""

    def __init__(self, repository, budget_resolver):
        if not isinstance(repository, SQLiteWorkflowRepository) or not callable(budget_resolver):
            raise QAContextError()
        self._repository, self._budget_resolver = repository, budget_resolver

    def __repr__(self):
        return "SQLiteQAContextLoader()"

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
                active = [step for step in steps if step.agent_role is AgentRole.QA
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
                previous_source, previous_report, previous_report_source = _read_validation_lineage(
                    connection, run, steps, source, AgentRole.QA, QAReportArtifact, metadata.requirement_ids)
                _verify_role_context(connection, run, steps, step, context, AgentRole.QA)
            planners = [producer for producer in steps if producer.agent_role is AgentRole.PLANNER]
            developers = [producer for producer in steps if producer.agent_role is AgentRole.DEVELOPER
                          and producer.workflow_step_id == source.workflow_step_id]
            if len(planners) != 1 or len(developers) != 1:
                raise ValueError
            planner, developer = planners[0], developers[0]
            if (run.run_id != metadata.run_id or run.scenario_id != metadata.scenario_id
                    or run_row["status"] != run.status.value
                    or step.run_id != run.run_id or step.workflow_step_id != metadata.workflow_step_id
                    or step.attempt != metadata.attempt or step.code_version != run.code_version
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
                    or run.fix_attempt == 0 and developer.input_artifact_ids != [requirement.artifact_id]
                    or step.a2a_task_id is not None and step.a2a_task_id != context.task_id
                    or step.agent_context_id is not None and step.agent_context_id != context.context_id):
                raise ValueError
            return QAExecutionContext(metadata=metadata, configuration=configuration,
                budget=self._budget_resolver(configuration), request_text=run.request_text,
                requirement_artifact=requirement, source=source, previous_source=previous_source,
                previous_report=previous_report, previous_report_source=previous_report_source,
                fix_attempt=run.fix_attempt)
        except LLMRuntimeError as error:
            if error.code is LLMErrorCode.BUDGET:
                raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
            raise QAContextError() from None
        except Exception:
            raise QAContextError() from None
