"""Run-to-Agent dispatch across Planner, Developer, QA, and Security stages."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from a2a.types import Task

from orchestrator.a2a import A2AAgentClient, A2AAgentRegistry
from orchestrator.application.a2a_tasks import (
    A2ATaskRunResult,
    A2ATaskRunner,
    TaskRunDisposition,
)
from orchestrator.application.planner_output import (
    PlannerOutputValidationError,
    PlannerPlan,
    parse_planner_output,
)
from orchestrator.application.developer_output import (
    DeveloperOutputValidationError,
    parse_developer_output,
)
from orchestrator.application.validation_output import (
    ProtectedSupersession,
    VerdictDecision,
    decide_verdict,
    protected_supersession,
    parse_validation_output,
)
from orchestrator.application.validation_request import build_validation_request
from orchestrator.core.async_control import await_owned
from orchestrator.domain import (
    AgentRole,
    BuildReportArtifact,
    CodeSnapshotArtifact,
    FinalVerdict,
    FindingDisposition,
    IssueRecord,
    QAReportArtifact,
    RequirementValidator,
    ScenarioDefinition,
    SecuritySeverity,
    SecurityReportArtifact,
    SnapshotHandoff,
    ValidationOutcome,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    get_scenario,
    make_issue_fingerprint,
)
from orchestrator.infrastructure import (
    RunDispatchConflict,
    RunNotFoundError,
    SQLiteWorkflowRepository,
)
from orchestrator.domain.tool_evidence import ToolExecutionOutcome
from orchestrator.domain.recovery_causes import execution_receipts
from orchestrator.core.security import redact_data as redact, redact_text


logger = logging.getLogger(__name__)
ClientFactory = Callable[[str], A2AAgentClient]
_DEVELOPER_OUTPUT_CONTRACT: dict[str, object] = {
    "schemaVersion": 1,
    "requiredArtifactNames": [
        "source-snapshot.json",
        "change-report.json",
        "build-report.json",
    ],
    "partMediaType": "application/json",
    "artifactMetadataFields": [
        "runId",
        "workflowStepId",
        "projectArtifactId",
        "artifactVersion",
    ],
    "dataSchemas": {
        "source-snapshot.json": "schemas/project/developer_source_snapshot.schema.json",
        "change-report.json": "schemas/project/developer_change_report.schema.json",
        "build-report.json": "schemas/project/developer_build_report.schema.json",
    },
    "buildTool": {
        "name": "run_build",
        "requiredResultFields": ["exitCode", "durationMs", "executionManifestId"],
        "executor": "Developer Agent MCP client",
    },
    "requiredInvariants": [
        "All result Artifacts refer to the current Run, Developer Step, A2A Task, "
        "requirements and codeVersion.",
        "Build Report sourceArtifactId and executionManifest equal source-snapshot.json.",
        "Freeze the Source Snapshot before Build; do not mutate it after registration.",
    ],
    "executionBoundary": {
        "responsibility": (
            "Implement the validated Plan without changing the Requirements or "
            "Acceptance Criteria."
        ),
        "forbidden": [
            "Do not claim Build PASS without a successful run_build Tool result.",
            "Do not modify QA/Security protected tests or validation criteria.",
        ],
    },
}


class PlannerRunDispatcher:
    """Dispatch one Run through its configured Agents in Workflow order."""

    def __init__(
        self,
        repository: SQLiteWorkflowRepository,
        agent_registry: A2AAgentRegistry,
        *,
        client_factory: ClientFactory = A2AAgentClient,
        before_dispatch: Callable[[UUID], Awaitable[None]] | None = None,
        after_dispatch: Callable[[UUID], Awaitable[None]] | None = None,
        allow_fix_dispatch: bool = True,
        single_agent: bool = False,
        protected_selectors: frozenset[str] = frozenset(),
        protected_cases: tuple = (),
        automatic_stop: bool = False,
    ) -> None:
        self._repository = repository
        self._agent_registry = agent_registry
        self._client_factory = client_factory
        if before_dispatch is not None and not callable(before_dispatch):
            raise ValueError("DISPATCH_CONFIGURATION_INVALID")
        if (type(allow_fix_dispatch) is not bool or type(single_agent) is not bool
                or type(automatic_stop) is not bool):
            raise ValueError("DISPATCH_CONFIGURATION_INVALID")
        # docs/live-api/05 22절 일반 종료 규칙(석민 결정 2026-11-18, 주호 확인 필요). 소유 Host 구성
        # (Multi·Single)이 켠다. 꺼 두면 지금까지의 사람 검토 경로를 그대로 쓴다.
        self._automatic_stop = automatic_stop
        # Host in-loop protected ``tool:scope`` pairs (Ver1). Empty keeps the
        # existing verdict policy exactly; see validation_output.ProtectedSupersession.
        if (type(protected_selectors) is not frozenset
                or any(type(item) is not str or item.count(":") != 1 or not all(item.split(":"))
                       for item in protected_selectors)):
            raise ValueError("DISPATCH_CONFIGURATION_INVALID")
        self._protected_selectors = protected_selectors
        self._protected_cases = tuple(protected_cases)
        self._single_agent = single_agent
        self._before_dispatch = before_dispatch
        self.set_after_dispatch(after_dispatch)
        self._allow_fix_dispatch = allow_fix_dispatch
        self._active_runs: set[UUID] = set()
        self._dispatch_tasks: dict[UUID, asyncio.Task] = {}
        self._dispatch_tokens: dict[UUID, str] = {}
        self._control_stops: set[asyncio.Task] = set()

    def set_after_dispatch(self, callback):
        if callback is not None and not callable(callback):
            raise ValueError("DISPATCH_CONFIGURATION_INVALID")
        self._after_dispatch = callback

    async def notify_settled(self, run_id):
        if self._after_dispatch is not None:
            await self._after_dispatch(run_id)

    def supports_architecture(self, architecture: str) -> bool:
        return architecture == ("SINGLE_AGENT" if self._single_agent else "MULTI_AGENT")

    def _agent_id(self, role: AgentRole) -> str:
        return "single_agent" if self._single_agent else role.value.lower()

    def is_run_active(self, run_id: UUID) -> bool:
        return run_id in self._active_runs

    async def dispatch_planner(self, run_id: UUID) -> None:
        if run_id in self._active_runs:
            return
        try:
            await self.control_operation(run_id, lambda: self._dispatch_planner(run_id), swallow_stop=True)
        except (RunDispatchConflict, RunNotFoundError):
            logger.info("Planner dispatch skipped for busy or missing Run %s", run_id)

    def _release_owner(self, run_id, token):
        self._repository.release_control(run_id, token)
        if self._dispatch_tokens.get(run_id) == token:
            self._active_runs.discard(run_id)
            self._dispatch_tasks.pop(run_id, None)
            self._dispatch_tokens.pop(run_id, None)

    async def control_operation(self, run_id, operation, *, swallow_stop=False):
        """Own initial dispatch and explicit resume with the same drain/lease."""
        if run_id in self._active_runs:
            raise RunDispatchConflict("Run already has an active control operation")
        token = self._repository.acquire_control(run_id)
        self._active_runs.add(run_id)
        self._dispatch_tokens[run_id] = token

        async def dispatch_owned():
            from orchestrator.observability import observe_run
            try:
                with observe_run(self._repository, run_id):
                    return await operation()
            finally:
                try:
                    await self.notify_settled(run_id)
                finally:
                    self._release_owner(run_id, token)

        task = asyncio.create_task(dispatch_owned(), name=f"workflow-dispatch:{run_id}")
        self._dispatch_tasks[run_id] = task
        try:
            return await await_owned(task, cancel_on_interrupt=True)
        except asyncio.CancelledError:
            # A control-owned stop cancels the child, not the HTTP/background
            # request owning this await. Server shutdown still drains the child.
            if asyncio.current_task().cancelling():
                raise
            if task not in self._control_stops:
                # Preparation/provider self-cancellation is not a human stop.
                raise
            if not swallow_stop:
                raise RunDispatchConflict("Run resumption was interrupted by cancellation; inspect its current status") from None

        finally:
            # A child cancelled before starting never enters its finally.
            if task.done():
                self._release_owner(run_id, token)
                self._control_stops.discard(task)

    async def stop_active_dispatch(self, run_id: UUID) -> None:
        """Drain a local poller before a human control lease is acquired.

        The caller preflights every send receipt first. This never cancels an
        Agent remotely or claims product success; known remote Tasks are next
        cancelled explicitly by WorkflowControlService.
        """
        task = self._dispatch_tasks.get(run_id)
        if task is None:
            return
        token = self._dispatch_tokens[run_id]
        self._control_stops.add(task)
        task.cancel()
        try:
            await await_owned(task)
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
        finally:
            self._release_owner(run_id, token)

    async def _dispatch_planner(self, run_id: UUID) -> None:
        """Claim the Planner Step, execute/poll its A2A Task, then map disposition."""
        try:
            run, step = self._repository.claim_planner_dispatch(run_id)
        except (RunDispatchConflict, RunNotFoundError):
            # Background delivery may be duplicated. The durable claim is one-shot.
            logger.info("Planner dispatch skipped for Run %s", run_id)
            return

        configuration = self._repository.get_run_configuration(run_id)
        if (configuration is None and self._single_agent or configuration is not None
                and not self.supports_architecture(configuration.configuration.architecture)):
            self._move_to_human_review(run_id, step.workflow_step_id, step.attempt,
                additional_event_types=("RUNTIME_ARCHITECTURE_MISMATCH",))
            return

        if self._before_dispatch is not None:
            try:
                await self._before_dispatch(run_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error("Run preparation failed for %s (error type: %s)", run_id, type(error).__name__)
                self._move_to_human_review(
                    run_id, step.workflow_step_id, step.attempt,
                    additional_event_types=("OWNED_AGENT_PREPARATION_FAILED",),
                )
                return

        scenario = self._scenario_for_run(run)
        if scenario is None:
            self._move_to_human_review(
                run_id,
                step.workflow_step_id,
                step.attempt,
                additional_event_types=("UNSUPPORTED_SCENARIO",),
            )
            return

        try:
            agent_url = self._agent_registry.require_base_url(AgentRole.PLANNER)
            async with self._client_factory(agent_url) as client:
                await client.resolve_agent_card()
                runner = A2ATaskRunner(client)
                result = await runner.submit_and_wait(
                    run,
                    step,
                    agent_id=self._agent_id(AgentRole.PLANNER),
                    payload={
                        "request": run.request_text,
                        "workspaceId": str(run.workspace_id),
                        "runConfiguration": self._run_configuration_payload(run.run_id),
                        "scenarioContract": scenario.planner_contract(),
                    },
                    observer=self._repository.task_update_observer(run),
                )
        except Exception as exc:
            # Do not retry an uncertain SendMessage side effect automatically.
            logger.error(
                "Planner dispatch requires review for Run %s (error type: %s)",
                run_id,
                type(exc).__name__,
            )
            self._move_to_human_review(run_id, step.workflow_step_id, step.attempt)
            return

        if result.disposition == TaskRunDisposition.COMPLETED:
            await self._advance_to_developer(
                run, result.step.workflow_step_id, result.task
            )
            return
        if result.disposition == TaskRunDisposition.WAITING_INPUT:
            try:
                self._repository.transition_run_and_record(
                    run_id,
                    WorkflowStatus.WAITING_INPUT,
                )
            except Exception as exc:
                self._log_transition_error(run_id, exc)
            return

        if result.disposition in (
            TaskRunDisposition.HUMAN_REVIEW,
            TaskRunDisposition.AGENT_FAILED,
            TaskRunDisposition.CANCELED,
            TaskRunDisposition.PROTOCOL_ERROR,
            TaskRunDisposition.POLLING_TIMEOUT,
        ):
            self._move_to_human_review(
                run_id,
                result.step.workflow_step_id,
                result.step.attempt,
            )

    async def _advance_to_developer(
        self, run: WorkflowRun, planner_step_id: UUID, planner_task: Task
    ) -> None:
        try:
            planner_output = parse_planner_output(
                planner_task,
                run_id=run.run_id,
                workflow_step_id=planner_step_id,
            )
            scenario = self._scenario_for_run(run)
            if scenario is None:
                raise ValueError("Run scenario is not supported by the Scenario Registry")
            scenario.validate_planner_requirements(planner_output.plan.requirements)
        except PlannerOutputValidationError as exc:
            logger.warning(
                "Planner output requires review for Run %s (error type: %s)",
                run.run_id,
                type(exc.__cause__ or exc).__name__,
            )
            self._move_to_human_review(
                run.run_id,
                planner_step_id,
                0,
                additional_event_types=("PLANNER_OUTPUT_REJECTED",),
            )
            return
        except ValueError as exc:
            logger.warning(
                "Planner requirements require review for Run %s (%s)",
                run.run_id,
                str(exc),
            )
            self._move_to_human_review(
                run.run_id,
                planner_step_id,
                0,
                additional_event_types=("PLANNER_REQUIREMENTS_NOT_CANONICAL",),
            )
            return

        developer_url = self._agent_registry.get_base_url(AgentRole.DEVELOPER)
        assert scenario is not None
        try:
            developer_run, developer_step = self._repository.create_developer_step_from_plan(
                run.run_id,
                planner_step_id,
                a2a_artifact_id=planner_output.a2a_artifact_id,
                requirement_ids=[
                    requirement.requirement_id
                    for requirement in planner_output.plan.requirements
                ],
                project_artifact_id=planner_output.project_artifact_id,
                developer_configured=developer_url is not None,
                requirement_payload=planner_output.plan.model_dump(mode="json", by_alias=True),
                artifact_version=planner_output.artifact_version,
                artifact_uri=f"artifact://{planner_output.project_artifact_id}/requirements.json",
            )
        except Exception as exc:
            logger.error(
                "Could not create Developer Step for Run %s (error type: %s)",
                run.run_id,
                type(exc).__name__,
            )
            self._move_to_human_review(
                run.run_id,
                planner_step_id,
                0,
                additional_event_types=("PLANNER_OUTPUT_HANDOFF_FAILED",),
            )
            return

        if developer_url is None:
            logger.info("Developer dispatch requires configuration for Run %s", run.run_id)
            return

        payload = {
            "workspaceId": str(run.workspace_id),
            "scenario": scenario.planner_contract(),
            "runConfiguration": self._run_configuration_payload(run.run_id),
            "plan": planner_output.plan.model_dump(mode="json", by_alias=True),
            "sourceArtifact": {
                "a2aArtifactId": planner_output.a2a_artifact_id,
                "projectArtifactId": str(planner_output.project_artifact_id),
                "artifactVersion": planner_output.artifact_version,
            },
            "outputContract": _DEVELOPER_OUTPUT_CONTRACT,
        }
        try:
            async with self._client_factory(developer_url) as client:
                await client.resolve_agent_card()
                result = await A2ATaskRunner(client).submit_and_wait(
                    developer_run,
                    developer_step,
                    agent_id=self._agent_id(AgentRole.DEVELOPER),
                    payload=payload,
                    agent_context=self._context_for(run.run_id, AgentRole.DEVELOPER) if self._single_agent else None,
                    observer=self._repository.task_update_observer(developer_run),
                )
        except Exception as exc:
            logger.error(
                "Developer dispatch requires review for Run %s (error type: %s)",
                run.run_id,
                type(exc).__name__,
            )
            self._move_to_human_review(
                run.run_id,
                developer_step.workflow_step_id,
                developer_step.attempt,
            )
            return

        if result.disposition == TaskRunDisposition.WAITING_INPUT:
            # The definition's IMPLEMENTING stage pauses in HUMAN_REVIEW;
            # WAITING_INPUT is the Planner state, not a new universal edge.
            self._move_to_human_review(
                run.run_id, result.step.workflow_step_id, result.step.attempt,
                additional_event_types=("DEVELOPER_INPUT_REQUIRED",),
            )
            return
        if result.disposition == TaskRunDisposition.COMPLETED:
            await self._advance_to_validation(
                developer_run,
                result.step,
                result.task,
                planner_output.plan,
                scenario,
            )
            return
        if self._finish_no_progress(result):
            return
        if result.disposition in (
            TaskRunDisposition.HUMAN_REVIEW,
            TaskRunDisposition.AGENT_FAILED,
            TaskRunDisposition.CANCELED,
            TaskRunDisposition.PROTOCOL_ERROR,
            TaskRunDisposition.POLLING_TIMEOUT,
        ):
            self._move_to_human_review(
                run.run_id,
                result.step.workflow_step_id,
                result.step.attempt,
            )

    async def _advance_to_validation(
        self,
        run: WorkflowRun,
        developer_step: WorkflowStep,
        developer_task: Task,
        plan: PlannerPlan,
        scenario: ScenarioDefinition,
    ) -> None:
        try:
            output = parse_developer_output(
                developer_task,
                run=run,
                step=developer_step,
            )
        except DeveloperOutputValidationError as exc:
            logger.warning(
                "Developer output requires review for Run %s (error type: %s)",
                run.run_id,
                type(exc.__cause__ or exc).__name__,
            )
            self._move_to_human_review(
                run.run_id,
                developer_step.workflow_step_id,
                developer_step.attempt,
                additional_event_types=("DEVELOPER_OUTPUT_REJECTED",),
            )
            return

        configuration = self._repository.get_run_configuration(run.run_id)
        baseline = configuration.configuration.environment if configuration is not None else None
        if baseline is not None and (
            output.source.container_image_digest != baseline.container_image_digest
            or output.source.dependency_lock_hash != baseline.dependency_lock_hash
        ):
            self._move_to_human_review(run.run_id, developer_step.workflow_step_id, developer_step.attempt, additional_event_types=("FROZEN_EXECUTION_BASELINE_MISMATCH",))
            return

        qa_url = self._agent_registry.get_base_url(AgentRole.QA)
        security_url = self._agent_registry.get_base_url(AgentRole.SECURITY)
        validators_configured = qa_url is not None and security_url is not None
        try:
            if output.build_report.tool_evidence is not None:
                self._repository.ingest_tool_evidence(run.run_id, developer_step.workflow_step_id, (output.build_report.tool_evidence,))
            updated_run, _, validation_steps = self._repository.record_developer_candidate(
                run.run_id,
                developer_step.workflow_step_id,
                source=output.source,
                change_report=output.change_report,
                build_report=output.build_report,
                detected_issues=(self._build_issue(run, output.source, output.build_report),) if output.build_report.execution_outcome == ToolExecutionOutcome.FAIL and output.build_report.tool_evidence is not None else (),
                validation_agents_configured=validators_configured,
                validation_requirement_ids={
                    AgentRole.QA: scenario.requirement_ids_for(RequirementValidator.QA),
                    AgentRole.SECURITY: scenario.requirement_ids_for(
                        RequirementValidator.SECURITY
                    ),
                },
            )
        except Exception as exc:
            logger.error(
                "Could not register Developer Artifacts for Run %s (error type: %s)",
                run.run_id,
                type(exc).__name__,
            )
            self._move_to_human_review(
                run.run_id,
                developer_step.workflow_step_id,
                developer_step.attempt,
                additional_event_types=("DEVELOPER_ARTIFACT_REGISTRATION_FAILED",),
            )
            return

        if output.build_report.execution_outcome == ToolExecutionOutcome.UNVERIFIED:
            return
        if output.build_report.tool_evidence is None:
            self._move_to_human_review(run.run_id, developer_step.workflow_step_id, developer_step.attempt, additional_event_types=("BUILD_TOOL_EVIDENCE_MISSING",))
            return
        if not output.build_report.passed:
            current = self._repository.get_run(run.run_id)
            if current is not None:
                stored = tuple(item for item in self._repository.list_issue_records(run.run_id) if item.code_version == current.code_version and item.report_artifact_id == output.build_report.artifact_id)
                if current.status == WorkflowStatus.FIX_REQUIRED:
                    await self._dispatch_fix(current, plan, scenario, stored)
            return
        if not validators_configured:
            return

        handoff = SnapshotHandoff.from_snapshot(output.source)
        await self._dispatch_validation_agents(
            updated_run,
            validation_steps,
            handoff,
            agent_urls={AgentRole.QA: qa_url, AgentRole.SECURITY: security_url},
            plan=plan,
            scenario=scenario,
        )

    async def _dispatch_validation_agents(
        self,
        run: WorkflowRun,
        steps: tuple[WorkflowStep, ...],
        handoff: SnapshotHandoff,
        *,
        agent_urls: dict[AgentRole, str | None],
        plan: PlannerPlan,
        scenario: ScenarioDefinition,
    ) -> None:
        async def dispatch_one(step: WorkflowStep) -> A2ATaskRunResult | Exception:
            role = step.agent_role
            agent_url = agent_urls[role]
            if agent_url is None:
                return RuntimeError(f"{role.value} Agent is not configured")
            request_text = build_validation_request(
                self._repository.get_run_configuration(run.run_id), plan,
                scenario, step.requirement_ids, role,
            )
            try:
                async with self._client_factory(agent_url) as client:
                    await client.resolve_agent_card()
                    result = await A2ATaskRunner(client).submit_snapshot_and_wait(
                        run,
                        step,
                        handoff,
                        agent_id=self._agent_id(role),
                        recipient=role,
                        request_text=request_text,
                        agent_context=self._context_for(run.run_id, role),
                        observer=self._repository.task_update_observer(run),
                    )
                return result
            except Exception as exc:
                logger.error(
                    "%s dispatch requires review for Run %s (error type: %s)",
                    role.value,
                    run.run_id,
                    type(exc).__name__,
                )
                return exc

        if self._single_agent:
            # Same opaque Context cannot execute two phase Tasks concurrently.
            steps = tuple(sorted(steps, key=lambda item: item.agent_role is AgentRole.SECURITY))
            outcomes = []
            for step in steps:
                outcome = await dispatch_one(step)
                outcomes.append(outcome)
                if (isinstance(outcome, Exception) or outcome.disposition != TaskRunDisposition.COMPLETED) and not self._validation_draft_stop(outcome):
                    outcomes.extend(RuntimeError("SINGLE_PREVIOUS_PHASE_INCOMPLETE") for _ in steps[len(outcomes):])
                    break
        else:
            outcomes = await asyncio.gather(*(dispatch_one(step) for step in steps))
        if any(
            isinstance(outcome, Exception)
            or outcome.disposition != TaskRunDisposition.COMPLETED
            for outcome in outcomes
        ):
            has_draft_stop = any(self._validation_draft_stop(o) for o in outcomes)
            # A draft stop requires its Host recovery proof, even in a mixed group.
            # Never downgrade a failed evidence check into an unverified generic close.
            if has_draft_stop or self._automatic_stop:
                if self._finish_validation_stops(run, outcomes):
                    current = self._repository.get_run(run.run_id)
                    if current is not None and current.status == WorkflowStatus.FIX_REQUIRED:
                        issues = tuple(i for i in self._repository.list_issue_records(run.run_id)
                                       if i.code_version == current.code_version)
                        await self._dispatch_fix(current, plan, scenario, issues)
                    return
            step_id = next(
                (
                    step.workflow_step_id
                    for step, outcome in zip(steps, outcomes, strict=True)
                    if isinstance(outcome, Exception)
                    or outcome.disposition != TaskRunDisposition.COMPLETED
                ),
                steps[0].workflow_step_id,
            )
            self._move_to_human_review(
                run.run_id,
                step_id,
                0,
                additional_event_types=("QA_SECURITY_DISPATCH_REQUIRES_REVIEW",),
            )
            return

        await self.consume_validation_results(run, steps, handoff, outcomes, plan=plan, scenario=scenario)

    @staticmethod
    def _validation_draft_stop(outcome):
        """A QA or Security Task that failed only because its draft corrections ran out (Host-coded)."""
        from google.protobuf.json_format import MessageToDict
        if (not isinstance(outcome, A2ATaskRunResult) or outcome.disposition != TaskRunDisposition.AGENT_FAILED
                or outcome.step.agent_role not in (AgentRole.QA, AgentRole.SECURITY) or outcome.task is None):
            return False
        status = MessageToDict(outcome.task.status)
        parts = status.get('message', {}).get('parts', [])
        return (status.get('state') == 'TASK_STATE_FAILED' and len(parts) == 1
                and parts[0].get('data', {}).get('code') == 'LLM_VALIDATION_DRAFT_EXHAUSTED')

    @classmethod
    def _qa_draft_stop(cls, outcome):
        return cls._validation_draft_stop(outcome) and outcome.step.agent_role == AgentRole.QA

    def _finish_validation_stops(self, run, outcomes):
        from orchestrator.infrastructure import automatic_stop, validation_stop
        try:
            completed, stopped, generic = [], [], {}
            for outcome in outcomes:
                if isinstance(outcome, RuntimeError) and str(outcome) == "SINGLE_PREVIOUS_PHASE_INCOMPLETE":
                    continue
                if not isinstance(outcome, A2ATaskRunResult):
                    return False
                if outcome.disposition == TaskRunDisposition.COMPLETED:
                    completed.append(outcome)
                    continue
                if not self._validation_draft_stop(outcome):
                    code = automatic_stop.agent_stop_code(outcome) if self._automatic_stop else None
                    if code is None:
                        return False
                    generic[outcome.step.workflow_step_id] = code
                stopped.append((outcome.step.workflow_step_id, outcome.task.id))
            if not stopped:
                return False
            if not completed and len(stopped) == len(generic) == 1:
                if (self._protected_cases and set(generic.values()) == {'LLM_TOOL_POLICY_VIOLATION'}
                        and validation_stop.finish_stopped(self._repository, run.run_id, stopped, generic=generic,
                            protected_cases=self._protected_cases)):
                    return True
                return self._finish_automatic_stop(run.run_id, outcomes)
            report = None
            issues = ()
            if completed:
                other, = completed
                source = next(a for a in self._repository.list_project_artifacts(run.run_id)
                              if isinstance(a, CodeSnapshotArtifact) and a.code_version == run.code_version)
                report = parse_validation_output(other.task, run=run, step=other.step, source=source)
                qa = report if isinstance(report, QAReportArtifact) else None
                security = report if isinstance(report, SecurityReportArtifact) else None
                items = qa.tests if qa is not None else security.requirement_results
                evidence = tuple(receipt for item in items for receipt in execution_receipts(item))
                self._repository.ingest_tool_evidence(run.run_id, report.workflow_step_id, evidence)
                excluded = protected_supersession(qa, security, self._protected_selectors)
                missing_requirements = set().union(*(outcome.step.requirement_ids for outcome in outcomes
                    if isinstance(outcome, A2ATaskRunResult) and outcome.disposition != TaskRunDisposition.COMPLETED))
                withheld = set()
                if self._protected_selectors:
                    from orchestrator.application.validation_output import _selector, _measured_security_tests
                    # A missing role can have protected tests for a shared requirement. Its unknown
                    # result cannot authorize suppression or establish a generated-only product failure.
                    shared = missing_requirements.intersection(report.requirement_ids)
                    if shared:
                        from orchestrator.domain.repair_progress import legacy_practice_versions
                        frozen = self._repository.get_run_configuration(run.run_id)
                        try:
                            baseline = legacy_practice_versions(frozen) if frozen else {}
                        except (ValueError, OSError):
                            baseline = {}
                        if baseline:
                            missing_role = AgentRole.SECURITY if qa else AgentRole.QA
                            shared.intersection_update(UUID(req) for (role, req, name) in baseline
                                if role == missing_role.value and _selector(name) in self._protected_selectors)
                    excluded = ProtectedSupersession(
                        frozenset(test.test_id for test in (qa.tests if qa else ())
                                  if test.test_id in excluded.qa_test_ids and test.requirement_id not in shared),
                        excluded.security_requirement_ids - shared)
                    if qa:
                        withheld.update(test.test_id for test in qa.tests if test.requirement_id in shared
                                        and _selector(test.test_id) not in self._protected_selectors)
                    if security:
                        for entry in security.requirement_results:
                            measured = _measured_security_tests(entry)
                            if (entry.requirement_id in shared and measured and not any(
                                    status == 'FAIL' and _selector(identity) in self._protected_selectors
                                    for identity, status in measured)):
                                withheld.add(str(entry.requirement_id))
                issues = tuple(issue for issue in self._validation_issues(run, source, qa, security, excluded)
                               if issue.category.startswith('SECURITY_FINDING_') or issue.reference_id not in withheld)
            options = {}
            if completed:
                excluded_requirements = {test.requirement_id for test in (qa.tests if qa else ())
                                         if test.test_id in excluded.qa_test_ids} | set(excluded.security_requirement_ids)
                if excluded_requirements:
                    options['policy_excluded_requirement_ids'] = sorted(excluded_requirements, key=str)
            if generic:
                options['generic'] = generic
                if self._protected_cases:
                    options['protected_cases'] = self._protected_cases
            if issues:
                options['issues'] = issues
            return validation_stop.finish_stopped(self._repository, run.run_id, stopped, report, **options)
        except Exception as error:
            logger.warning('Could not verify validation stop (%s)', type(error).__name__)
            return False

    def _finish_automatic_stop(self, run_id, outcomes):
        """docs/live-api/05 22절: every unfinished Task stopped for a model reason (closed code set), so the
        Run ends with its last Host-measured candidate (FAIL/UNVERIFIED, never SUCCESS). Exceptions,
        cancellation, protocol/timeout, infrastructure and permission codes keep the review path."""
        if not self._automatic_stop:
            return False
        from orchestrator.infrastructure import automatic_stop
        try:
            stopped = [o for o in outcomes
                       if not (isinstance(o, A2ATaskRunResult) and o.disposition == TaskRunDisposition.COMPLETED)
                       # Single: phases after a stopped phase were never sent; they are not failures.
                       and not (isinstance(o, RuntimeError) and str(o) == "SINGLE_PREVIOUS_PHASE_INCOMPLETE")]
            codes = [automatic_stop.agent_stop_code(o) for o in stopped]
            if not stopped or any(code is None for code in codes):
                return False
            first = stopped[0]
            return automatic_stop.finish_agent_stop(
                self._repository, run_id, first.step.workflow_step_id, first.task.id, codes[0])
        except Exception as error:
            logger.warning("Could not verify automatic stop (%s)", type(error).__name__)
            return False

    async def consume_validation_results(
        self,
        run: WorkflowRun,
        steps: tuple[WorkflowStep, ...],
        handoff: SnapshotHandoff,
        outcomes: list[A2ATaskRunResult | Exception],
        *,
        plan: PlannerPlan,
        scenario: ScenarioDefinition,
        allow_fix_dispatch: bool = True,
    ) -> None:
        """Consume completed Tasks after initial dispatch or safe recovery polling."""
        artifacts = self._repository.list_project_artifacts(run.run_id)
        source = next(
            (
                artifact
                for artifact in artifacts
                if isinstance(artifact, CodeSnapshotArtifact)
                and artifact.code_version == handoff.execution_manifest.code_version
            ),
            None,
        )
        build = next(
            (
                artifact
                for artifact in artifacts
                if isinstance(artifact, BuildReportArtifact)
                and artifact.code_version == handoff.execution_manifest.code_version
            ),
            None,
        )
        if source is None or build is None:
            self._move_to_human_review(
                run.run_id,
                steps[0].workflow_step_id,
                0,
                additional_event_types=("VALIDATION_SOURCE_ARTIFACT_MISSING",),
            )
            return

        reports: dict[AgentRole, QAReportArtifact | SecurityReportArtifact] = {}
        try:
            for step, outcome in zip(steps, outcomes, strict=True):
                assert isinstance(outcome, A2ATaskRunResult)
                reports[step.agent_role] = parse_validation_output(
                    outcome.task,
                    run=run,
                    step=outcome.step,
                    source=source,
                )
            qa_report = reports[AgentRole.QA]
            security_report = reports[AgentRole.SECURITY]
            assert isinstance(qa_report, QAReportArtifact)
            assert isinstance(security_report, SecurityReportArtifact)
            for report in (qa_report, security_report):
                result_items = report.tests if isinstance(report, QAReportArtifact) else report.requirement_results
                evidence = tuple(receipt for item in result_items for receipt in execution_receipts(item))
                self._repository.ingest_tool_evidence(run.run_id, report.workflow_step_id, evidence)
            superseded = protected_supersession(qa_report, security_report, self._protected_selectors)
            decision = decide_verdict(
                qa_report,
                security_report,
                fix_attempt=run.fix_attempt,
                requirements_authoritative=True,
                protected_selectors=self._protected_selectors,
            )
            if build.tool_evidence is None:
                decision = VerdictDecision(WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW, "Build PASS lacks successful MCP Tool provenance")
            if (
                decision.target_status == WorkflowStatus.FINISHED
                and decision.verdict == FinalVerdict.SUCCESS
                and not self._trace_satisfies_success_contract(run, scenario)
            ):
                decision = VerdictDecision(
                    WorkflowStatus.HUMAN_REVIEW,
                    FinalVerdict.HUMAN_REVIEW,
                    "Trace does not prove complete requirement-to-validation lineage",
                )
            # docs/live-api/05 22절: an evidence-shortage or Security-finding judgment hold goes on with the
            # measured result instead: known FAILs with fix attempts left go to the fix cycle; otherwise
            # FAIL (measured failure) or UNVERIFIED, never SUCCESS. Manifest mismatch and a missing
            # Scenario Registry stay holds.
            automatic_stop_code = None
            if (self._automatic_stop and decision.target_status == WorkflowStatus.HUMAN_REVIEW
                    and build.tool_evidence is not None):
                from orchestrator.infrastructure import automatic_stop
                measured_failure = (
                    any(item.outcome == ValidationOutcome.FAIL and execution_receipts(item)
                        for item in qa_report.tests if item.test_id not in superseded.qa_test_ids)
                    or any(item.outcome == ValidationOutcome.FAIL and execution_receipts(item)
                           for item in security_report.requirement_results
                           if item.requirement_id not in superseded.security_requirement_ids)
                    or any(item.disposition == FindingDisposition.CONFIRMED
                           and item.severity in (SecuritySeverity.HIGH, SecuritySeverity.CRITICAL)
                           for item in security_report.findings))
                fallback = automatic_stop.verdict_fallback(decision.reason, measured_failure=measured_failure,
                                                           fix_attempt=run.fix_attempt)
                if fallback is not None:
                    target, verdict, automatic_stop_code = fallback
                    decision = VerdictDecision(target, verdict, decision.reason)
            self._repository.record_validation_results(
                run.run_id,
                qa_report=qa_report,
                security_report=security_report,
                decision_status=decision.target_status,
                verdict=decision.verdict,
                detected_issues=self._validation_issues(run, source, qa_report, security_report, superseded) if decision.target_status == WorkflowStatus.FIX_REQUIRED or decision.verdict == FinalVerdict.FAIL else (),
                policy_excluded_requirement_ids=tuple(dict.fromkeys(
                    [test.requirement_id for test in qa_report.tests if test.test_id in superseded.qa_test_ids]
                    + sorted(superseded.security_requirement_ids, key=str))),
            )
            if automatic_stop_code is not None:
                from orchestrator.infrastructure import automatic_stop
                if decision.target_status == WorkflowStatus.FIX_REQUIRED:
                    automatic_stop.record_hold_routed_to_fix(self._repository, run.run_id, automatic_stop_code)
                else:
                    automatic_stop.record_verdict_stop(self._repository, run.run_id, automatic_stop_code)
            current = self._repository.get_run(run.run_id)
            if current is not None and (
                any(item.outcome == ValidationOutcome.FAIL for item in qa_report.tests
                    if item.test_id not in superseded.qa_test_ids)
                or any(item.outcome == ValidationOutcome.FAIL for item in security_report.requirement_results
                       if item.requirement_id not in superseded.security_requirement_ids)
                or any(item.disposition == FindingDisposition.CONFIRMED and item.severity in (SecuritySeverity.HIGH, SecuritySeverity.CRITICAL) for item in security_report.findings)
            ):
                stored = tuple(item for item in self._repository.list_issue_records(run.run_id) if item.code_version == current.code_version and item.report_artifact_id in {qa_report.artifact_id, security_report.artifact_id})
                if not stored:
                    stored = self._repository.record_detected_issues(run.run_id, self._validation_issues(current, source, qa_report, security_report, superseded), allow_terminal=True)
                if current.status == WorkflowStatus.FIX_REQUIRED and allow_fix_dispatch:
                    await self._dispatch_fix(current, plan, scenario, stored)
        except Exception as exc:
            logger.error(
                "QA/Security result requires review for Run %s (error type: %s)",
                run.run_id,
                type(exc).__name__,
            )
            self._move_to_human_review(
                run.run_id,
                steps[0].workflow_step_id,
                0,
                additional_event_types=("QA_SECURITY_OUTPUT_REJECTED",),
            )

    async def _dispatch_fix(
        self,
        run: WorkflowRun,
        plan: PlannerPlan,
        scenario: ScenarioDefinition,
        issues: tuple[IssueRecord, ...],
    ) -> None:
        # An explicit Host capability gate may stop before issuing a fix.
        # It never changes the frozen three-cycle policy or creates a new budget.
        if not self._allow_fix_dispatch:
            return
        developer_url = self._agent_registry.get_base_url(AgentRole.DEVELOPER)
        try:
            fixing_run, step, stored_issues, input_artifact_ids = (
                self._repository.start_fix_cycle(
                    run.run_id,
                    issues,
                    developer_configured=developer_url is not None,
                )
            )
        except Exception as exc:
            logger.error(
                "Could not start fix cycle for Run %s (error type: %s)",
                run.run_id,
                type(exc).__name__ + ": " + redact_text(str(exc)),
            )
            self._move_to_human_review(
                run.run_id,
                None,
                run.fix_attempt,
                additional_event_types=("FIX_CYCLE_CREATION_FAILED",),
            )
            return
        if fixing_run.status != WorkflowStatus.FIXING or step is None or developer_url is None:
            return

        artifacts = self._repository.list_project_artifacts(run.run_id)
        input_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.artifact_id in input_artifact_ids
            or artifact.artifact_id in {issue.report_artifact_id for issue in stored_issues}
        ]
        payload = self.build_fix_payload(fixing_run, step, plan, scenario, stored_issues, input_artifacts)
        payload["runConfiguration"] = self._run_configuration_payload(run.run_id)
        try:
            async with self._client_factory(developer_url) as client:
                await client.resolve_agent_card()
                result = await A2ATaskRunner(client).submit_and_wait(
                    fixing_run,
                    step,
                    agent_id=self._agent_id(AgentRole.DEVELOPER),
                    payload=payload,
                    agent_context=self._context_for(run.run_id, AgentRole.DEVELOPER),
                    observer=self._repository.task_update_observer(fixing_run),
                )
        except Exception as exc:
            logger.error(
                "Developer fix dispatch requires review for Run %s (error type: %s)",
                run.run_id,
                type(exc).__name__,
            )
            self._move_to_human_review(
                run.run_id,
                step.workflow_step_id,
                step.attempt,
                additional_event_types=("FIX_DISPATCH_REQUIRES_REVIEW",),
            )
            return

        if result.disposition == TaskRunDisposition.COMPLETED:
            await self._advance_to_validation(
                fixing_run,
                result.step,
                result.task,
                plan,
                scenario,
            )
            return
        if self._finish_no_progress(result):
            return
        if self._finish_automatic_stop(run.run_id, [result]):
            return
        self._move_to_human_review(
            run.run_id,
            result.step.workflow_step_id,
            result.step.attempt,
            additional_event_types=("FIX_TASK_REQUIRES_REVIEW",),
        )

    def _finish_no_progress(self, result):
        if result.disposition != TaskRunDisposition.AGENT_FAILED or result.task is None:
            return False
        from google.protobuf.json_format import MessageToDict
        try:
            status = MessageToDict(result.task.status)
            parts = status.get('message', {}).get('parts', [])
            # The Agent's code only selects the candidates; the Host recovery record is the proof
            # (READY without changes, or exhausted Developer format corrections: docs/live-api/05 14절).
            if (status.get('state') != 'TASK_STATE_FAILED' or len(parts) != 1
                    or parts[0].get('data', {}).get('code') not in (
                        'LLM_DEVELOPMENT_NO_PROGRESS', 'LLM_DEVELOPMENT_REJECTION_EXHAUSTED', 'LLM_RESPONSE_INVALID', 'LLM_SCHEMA_INVALID',
                        'LLM_RESPONSE_INCOMPLETE')):
                return False
            return self._repository.finish_development_no_progress(
                result.step.run_id, result.step.workflow_step_id, result.task.id)
        except Exception as error:
            logger.warning('Could not verify development stop (%s)', type(error).__name__)
            return False

    def _context_for(self, run_id: UUID, role: AgentRole):
        return next((item for item in self._repository.list_agent_contexts(run_id) if item.agent_id == self._agent_id(role)), None)

    def _run_configuration_payload(self, run_id: UUID, *, include_scenario=True):
        configuration = self._repository.get_run_configuration(run_id)
        if configuration is None:
            return None
        payload = configuration.to_artifact_json()
        if not include_scenario:
            payload.pop("scenarioContract", None)
            payload.pop("frozenScenarioContractJson", None)
        return payload

    def _scenario_for_run(self, run: WorkflowRun) -> ScenarioDefinition | None:
        configuration = self._repository.get_run_configuration(run.run_id)
        contract = getattr(configuration, "scenario_contract", None)
        if contract:
            try:
                scenario = ScenarioDefinition.from_contract(contract)
            except (ValueError, TypeError, KeyError):
                return None
            return scenario if scenario.scenario_id == run.scenario_id else None
        return get_scenario(run.scenario_id)

    @staticmethod
    def build_fix_payload(run, step, plan, scenario, issues, input_artifacts):
        """Create a recoverable Fix Request with scrubbed, actionable evidence."""
        from orchestrator.domain.validation_feedback import validation_feedback, without_expectations
        payload = {
            "workspaceId": str(run.workspace_id),
            "plan": redact(plan.model_dump(mode="json", by_alias=True)),
            "fixRequest": {
                "attempt": run.fix_attempt,
                "validationFeedback": validation_feedback(input_artifacts),
                "issues": [redact({
                    "issueId": str(issue.issue_id), "fingerprint": issue.fingerprint,
                    "category": issue.category, "referenceId": issue.reference_id,
                    "severity": issue.severity,
                    "requirementIds": [str(value) for value in issue.requirement_ids],
                    "codeVersion": issue.code_version,
                    "sourceArtifactId": str(issue.source_artifact_id),
                    "reportArtifactId": str(issue.report_artifact_id) if issue.report_artifact_id else None,
                    "title": issue.title, "description": issue.description,
                    "expectedResult": issue.expected_result, "actualResult": issue.actual_result,
                    "normalizedLocation": issue.normalized_location,
                    "evidenceRefs": list(issue.evidence_refs),
                }) for issue in issues],
                "inputArtifacts": [{
                    "artifactId": str(artifact.artifact_id),
                    "artifactType": artifact.artifact_type,
                    "artifactVersion": artifact.artifact_version,
                    "artifactUri": artifact.artifact_uri,
                    "record": redact(artifact.model_dump(mode="json", by_alias=True)),
                } for artifact in input_artifacts],
                "constraints": [
                    "수정 Issue만 해결하고 기존 Requirement와 Acceptance Criteria를 변경하지 않는다.",
                    # 2026-11-18 Codex 재검토 #2: 역할 지침(READY 뒤 Host가 빌드)과 같게 맞춘다. 주호 확인 필요.
                    "이전 Candidate와 다른 변경을 Source 파일에 쓴 뒤 READY를 제출한다.",
                    "새 codeVersion Snapshot 생성, Build 실행, 결과 기록은 Host가 맡는다. Developer는 Build를 실행하거나 결과를 보고하지 않는다.",
                    "QA/Security Report와 보호된 테스트를 수정하지 않는다.",
                ],
            },
            "outputContract": _DEVELOPER_OUTPUT_CONTRACT,
            "scenario": scenario.planner_contract(),
        }
        fix = payload['fixRequest']
        for issue in fix['issues']:
            if issue['category'] in ('QA_ASSERTION_FAILURE', 'SECURITY_REQUIREMENT_FAILURE') or issue['category'].startswith('SECURITY_FINDING_'):
                issue.pop('expectedResult', None)
        for artifact in fix['inputArtifacts']:
            if artifact['artifactType'] in ('QA_REPORT', 'SECURITY_REPORT'):
                artifact['record'] = without_expectations(artifact['record'])
        from orchestrator.domain.protected_feedback import append_protected_feedback
        append_protected_feedback(fix, issues, input_artifacts)
        return payload

    @staticmethod
    def _build_issue(
        run: WorkflowRun,
        source: CodeSnapshotArtifact,
        build: BuildReportArtifact,
    ) -> IssueRecord:
        requirement_ids = list(source.requirement_ids)
        diagnostic_identity = tuple((item.rule_id, item.normalized_location.casefold()) for item in build.diagnostics)
        if diagnostic_identity:
            diagnostic_key = json.dumps(sorted(diagnostic_identity), ensure_ascii=False, separators=(",", ":"))
            location = ";".join(sorted(item.normalized_location for item in build.diagnostics))
        else:
            # Unknown diagnostics cannot prove the same defect across different code.
            # Re-delivery of one recorded report retains the same identity.
            diagnostic_key = str(build.artifact_id)
            location = "unknown"
        return IssueRecord(
            run_id=run.run_id,
            fingerprint=make_issue_fingerprint(
                "BUILD", diagnostic_key, "BUILD_CODE_FAILURE", location
            ),
            code_version=source.code_version,
            source_artifact_id=source.artifact_id,
            report_artifact_id=build.artifact_id,
            requirement_ids=requirement_ids,
            reporter=AgentRole.DEVELOPER,
            category="BUILD_CODE_FAILURE",
            reference_id="BUILD",
            severity="ERROR",
            title="Build failed",
            description="; ".join(item.message for item in build.diagnostics) or f"Build exited with code {build.exit_code}; diagnostic location is unverified.",
            expected_result="Build command exits with code 0.",
            actual_result=f"Build command exited with code {build.exit_code}.",
            normalized_location=location,
            evidence_refs=tuple(ref for ref in (build.stderr_ref, build.stdout_ref, build.tool_evidence.evidence_ref if build.tool_evidence else None) if ref),
            cause_status="CONFIRMED" if diagnostic_identity else "UNKNOWN",
            consecutive_repeat_count=0,
        )

    @staticmethod
    def _validation_issues(
        run: WorkflowRun,
        source: CodeSnapshotArtifact,
        qa: QAReportArtifact | None,
        security: SecurityReportArtifact | None,
        superseded: ProtectedSupersession = ProtectedSupersession(),
    ) -> tuple[IssueRecord, ...]:
        # Generated-test results settled by passing protected tests (Ver1) are
        # not product defects and never become Developer fix requests.
        issues: list[IssueRecord] = []
        for test in (qa.tests if qa is not None else ()):
            if test.outcome != ValidationOutcome.FAIL or test.test_id in superseded.qa_test_ids:
                continue
            requirement = str(test.requirement_id)
            issues.append(
                IssueRecord(
                    run_id=run.run_id,
                    fingerprint=make_issue_fingerprint(
                        requirement, test.test_id, "QA_ASSERTION_FAILURE", test.normalized_location or test.test_id
                    ),
                    code_version=source.code_version,
                    source_artifact_id=source.artifact_id,
                    report_artifact_id=qa.artifact_id,
                    requirement_ids=[test.requirement_id],
                    reporter=AgentRole.QA,
                    category="QA_ASSERTION_FAILURE",
                    reference_id=test.test_id,
                    severity="FUNCTIONAL",
                    title=test.title,
                    description=test.details or test.title,
                    expected_result=test.expected_result or "Acceptance criterion passes.",
                    actual_result=test.actual_result or test.details or "Required QA assertion failed.",
                    normalized_location=test.normalized_location or test.test_id,
                    evidence_refs=(test.tool_evidence.evidence_ref,) if test.tool_evidence else (),
                    cause_status="CONFIRMED",
                    consecutive_repeat_count=0,
                )
            )
        for result in (security.requirement_results if security is not None else ()):
            if result.outcome != ValidationOutcome.FAIL or result.requirement_id in superseded.security_requirement_ids:
                continue
            requirement = str(result.requirement_id)
            issues.append(
                IssueRecord(
                    run_id=run.run_id,
                    fingerprint=make_issue_fingerprint(
                        requirement, requirement, "SECURITY_REQUIREMENT_FAILURE", result.normalized_location or requirement
                    ),
                    code_version=source.code_version,
                    source_artifact_id=source.artifact_id,
                    report_artifact_id=security.artifact_id,
                    requirement_ids=[result.requirement_id],
                    reporter=AgentRole.SECURITY,
                    category="SECURITY_REQUIREMENT_FAILURE",
                    reference_id=requirement,
                    severity="HIGH",
                    title="Security requirement failed",
                    description=result.details or "Security acceptance criterion failed.",
                    expected_result=result.expected_result or "Security acceptance criterion passes.",
                    actual_result=result.actual_result or result.details or "Required Security check failed.",
                    normalized_location=result.normalized_location or requirement,
                    evidence_refs=(result.tool_evidence.evidence_ref,) if result.tool_evidence else (),
                    cause_status="CONFIRMED",
                    consecutive_repeat_count=0,
                )
            )
        for finding in (security.findings if security is not None else ()):
            if (
                finding.disposition != FindingDisposition.CONFIRMED
                or finding.severity not in (SecuritySeverity.HIGH, SecuritySeverity.CRITICAL)
            ):
                continue
            requirement_ids = (
                [finding.requirement_id]
                if finding.requirement_id is not None
                else list(security.requirement_ids)
            )
            requirement_key = ",".join(sorted(str(item) for item in requirement_ids))
            issues.append(
                IssueRecord(
                    run_id=run.run_id,
                    fingerprint=make_issue_fingerprint(
                        requirement_key,
                        finding.rule_id or finding.finding_id,
                        f"SECURITY_FINDING_{finding.severity.value}",
                        (finding.normalized_location or finding.rule_id or finding.finding_id).strip().casefold(),
                    ),
                    code_version=source.code_version,
                    source_artifact_id=source.artifact_id,
                    report_artifact_id=security.artifact_id,
                    requirement_ids=requirement_ids,
                    reporter=AgentRole.SECURITY,
                    category=f"SECURITY_FINDING_{finding.severity.value}",
                    reference_id=finding.finding_id,
                    severity=finding.severity.value,
                    title=finding.title,
                    description=finding.description,
                    expected_result="No confirmed blocking Security finding.",
                    actual_result=finding.description,
                    normalized_location=finding.normalized_location or finding.rule_id or finding.finding_id,
                    evidence_refs=(finding.evidence_ref,) if finding.evidence_ref else (),
                    cause_status="CONFIRMED",
                    consecutive_repeat_count=0,
                )
            )
        if not issues and qa is not None and security is not None:
            raise ValueError("FIX_REQUIRED verdict has no actionable QA/Security Issue")
        return tuple(issues)

    def _trace_satisfies_success_contract(
        self, run: WorkflowRun, scenario: ScenarioDefinition
    ) -> bool:
        required = set(scenario.requirement_ids)
        planner_ok = developer_ok = build_ok = issue_created = False
        fix_starts = offset = 0
        total_at_start = None
        while True:
            events, total = self._repository.list_events(
                run.run_id, limit=1000, offset=offset
            )
            if total_at_start is None:
                # Trace is append-only. Inspect the stable prefix existing when
                # this check began; concurrent telemetry cannot extend the scan
                # forever or make a long valid Run fail just by its length.
                total_at_start = total
            for event in events[:max(0, total_at_start - offset)]:
                covers_requirements = required.issubset(set(event.requirement_ids))
                planner_ok |= (
                    event.event_type == "PLANNER_OUTPUT_VALIDATED"
                    and covers_requirements
                )
                developer_ok |= (
                    event.event_type == "DEVELOPER_ARTIFACTS_VALIDATED"
                    and event.code_version == run.code_version
                    and covers_requirements
                )
                build_ok |= (
                    event.event_type == "BUILD_PASSED"
                    and event.code_version == run.code_version
                    and covers_requirements
                )
                issue_created |= event.event_type == "ISSUE_CREATED"
                fix_starts += event.event_type == "FIX_ATTEMPT_STARTED"
            offset += len(events)
            if offset >= total_at_start:
                break
            if not events:
                # A truncated/unavailable prefix is missing evidence, not PASS.
                return False
        fix_ok = run.fix_attempt == 0 or (issue_created and fix_starts >= run.fix_attempt)
        return planner_ok and developer_ok and build_ok and fix_ok

    def _move_to_human_review(
        self,
        run_id: UUID,
        workflow_step_id: UUID | None,
        attempt: int,
        *,
        additional_event_types: tuple[str, ...] = ("A2A_DISPATCH_REQUIRES_REVIEW",),
    ) -> None:
        try:
            self._repository.transition_run_and_record(
                run_id,
                WorkflowStatus.HUMAN_REVIEW,
                additional_event_types=(*additional_event_types, 'PROCESS_GATE_REJECTED'),
                workflow_step_id=workflow_step_id,
                attempt=attempt,
            )
        except Exception as exc:
            self._log_transition_error(run_id, exc)

    @staticmethod
    def _log_transition_error(run_id: UUID, error: Exception) -> None:
        logger.error(
            "Could not persist workflow disposition for Run %s (error type: %s)",
            run_id,
            type(error).__name__,
        )
