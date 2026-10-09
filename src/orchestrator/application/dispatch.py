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
    VerdictDecision,
    decide_verdict,
    parse_validation_output,
)
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
        allow_fix_dispatch: bool = True,
    ) -> None:
        self._repository = repository
        self._agent_registry = agent_registry
        self._client_factory = client_factory
        if before_dispatch is not None and not callable(before_dispatch):
            raise ValueError("DISPATCH_CONFIGURATION_INVALID")
        if type(allow_fix_dispatch) is not bool:
            raise ValueError("DISPATCH_CONFIGURATION_INVALID")
        self._before_dispatch = before_dispatch
        self._allow_fix_dispatch = allow_fix_dispatch
        self._active_runs: set[UUID] = set()

    def is_run_active(self, run_id: UUID) -> bool:
        return run_id in self._active_runs

    async def dispatch_planner(self, run_id: UUID) -> None:
        if run_id in self._active_runs:
            return
        try:
            token = self._repository.acquire_control(run_id)
        except (RunDispatchConflict, RunNotFoundError):
            logger.info("Planner dispatch skipped for busy or missing Run %s", run_id)
            return
        self._active_runs.add(run_id)
        try:
            await self._dispatch_planner(run_id)
        finally:
            try:
                self._repository.release_control(run_id, token)
            finally:
                self._active_runs.discard(run_id)

    async def _dispatch_planner(self, run_id: UUID) -> None:
        """Claim the Planner Step, execute/poll its A2A Task, then map disposition."""
        try:
            run, step = self._repository.claim_planner_dispatch(run_id)
        except (RunDispatchConflict, RunNotFoundError):
            # Background delivery may be duplicated. The durable claim is one-shot.
            logger.info("Planner dispatch skipped for Run %s", run_id)
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
                    agent_id="planner",
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
                    agent_id="developer",
                    payload=payload,
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
            try:
                self._repository.transition_run_and_record(
                    run.run_id,
                    WorkflowStatus.WAITING_INPUT,
                    workflow_step_id=result.step.workflow_step_id,
                    attempt=result.step.attempt,
                )
            except Exception as exc:
                self._log_transition_error(run.run_id, exc)
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
            duty = (
                "기능 테스트를 수행하고 QA 결과를 보고한다."
                if role == AgentRole.QA
                else "보안 취약점을 점검하고 Security 결과를 보고한다."
            )
            request_text = (
                f"workspaceId={run.workspace_id}\n"
                + "Run Configuration: " + json.dumps(self._run_configuration_payload(run.run_id, include_scenario=False), ensure_ascii=False, separators=(",", ":")) + "\n"
                +
                "검증 대상은 전달된 불변 Source Snapshot이다. 파일을 수정하지 말고 "
                "READ_ONLY로 접근한다. 요구사항과 Acceptance Criteria를 확인해 "
                f"{duty} 요구사항: "
                + json.dumps(
                    [
                        item for item in plan.model_dump(mode="json", by_alias=True)["requirements"]
                        if item["requirementId"] in {str(value) for value in step.requirement_ids}
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n\n"
                + "동결 보안/이메일 정책: " + json.dumps({key: value for key, value in scenario.planner_contract().items() if key in {"securityPolicy", "emailPolicy"}}, ensure_ascii=False, separators=(",", ":"))
                + "\n\n"
                "완료 시 A2A Task Artifact를 정확히 하나 반환한다. "
                "QA는 qa-report.json, Security는 security-report.json을 사용하고, "
                "각 Artifact는 application/json Data Part 하나와 project metadata를 "
                "가져야 한다. Report Schema는 "
                "schemas/project/qa_report.schema.json 또는 "
                "schemas/project/security_report.schema.json을 따른다. "
                "Task COMPLETED는 업무 완료일 뿐 결과 PASS를 뜻하지 않는다."
            )
            try:
                async with self._client_factory(agent_url) as client:
                    await client.resolve_agent_card()
                    result = await A2ATaskRunner(client).submit_snapshot_and_wait(
                        run,
                        step,
                        handoff,
                        agent_id=role.value.lower(),
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

        outcomes = await asyncio.gather(*(dispatch_one(step) for step in steps))
        if any(
            isinstance(outcome, Exception)
            or outcome.disposition != TaskRunDisposition.COMPLETED
            for outcome in outcomes
        ):
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

    async def consume_validation_results(
        self,
        run: WorkflowRun,
        steps: tuple[WorkflowStep, ...],
        handoff: SnapshotHandoff,
        outcomes: list[A2ATaskRunResult | Exception],
        *,
        plan: PlannerPlan,
        scenario: ScenarioDefinition,
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
                evidence = tuple(item.tool_evidence for item in result_items if item.tool_evidence is not None)
                self._repository.ingest_tool_evidence(run.run_id, report.workflow_step_id, evidence)
            decision = decide_verdict(
                qa_report,
                security_report,
                fix_attempt=run.fix_attempt,
                requirements_authoritative=True,
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
            self._repository.record_validation_results(
                run.run_id,
                qa_report=qa_report,
                security_report=security_report,
                decision_status=decision.target_status,
                verdict=decision.verdict,
                detected_issues=self._validation_issues(run, source, qa_report, security_report) if decision.target_status == WorkflowStatus.FIX_REQUIRED or decision.verdict == FinalVerdict.FAIL else (),
            )
            current = self._repository.get_run(run.run_id)
            if current is not None and (
                any(item.outcome == ValidationOutcome.FAIL for item in qa_report.tests)
                or any(item.outcome == ValidationOutcome.FAIL for item in security_report.requirement_results)
                or any(item.disposition == FindingDisposition.CONFIRMED and item.severity in (SecuritySeverity.HIGH, SecuritySeverity.CRITICAL) for item in security_report.findings)
            ):
                stored = tuple(item for item in self._repository.list_issue_records(run.run_id) if item.code_version == current.code_version and item.report_artifact_id in {qa_report.artifact_id, security_report.artifact_id})
                if not stored:
                    stored = self._repository.record_detected_issues(run.run_id, self._validation_issues(current, source, qa_report, security_report), allow_terminal=True)
                if current.status == WorkflowStatus.FIX_REQUIRED:
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
        # Keep real failures at FIX_REQUIRED until owned executors support fixes.
        # This capability gate does not change the frozen three-cycle policy.
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
                    agent_id="developer",
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
        self._move_to_human_review(
            run.run_id,
            result.step.workflow_step_id,
            result.step.attempt,
            additional_event_types=("FIX_TASK_REQUIRES_REVIEW",),
        )

    def _context_for(self, run_id: UUID, role: AgentRole):
        return next((item for item in self._repository.list_agent_contexts(run_id) if item.agent_id == role.value.lower()), None)

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
        return {
            "workspaceId": str(run.workspace_id),
            "plan": redact(plan.model_dump(mode="json", by_alias=True)),
            "fixRequest": {
                "attempt": run.fix_attempt,
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
                    "이전 Candidate와 다른 새 codeVersion의 불변 Source Snapshot을 생성한다.",
                    "새 Snapshot으로 Build를 실제 실행하고 결과를 보고한다.",
                    "QA/Security Report와 보호된 테스트를 수정하지 않는다.",
                ],
            },
            "outputContract": _DEVELOPER_OUTPUT_CONTRACT,
            "scenario": scenario.planner_contract(),
        }

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
        qa: QAReportArtifact,
        security: SecurityReportArtifact,
    ) -> tuple[IssueRecord, ...]:
        issues: list[IssueRecord] = []
        for test in qa.tests:
            if test.outcome != ValidationOutcome.FAIL:
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
        for result in security.requirement_results:
            if result.outcome != ValidationOutcome.FAIL:
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
        for finding in security.findings:
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
        if not issues:
            raise ValueError("FIX_REQUIRED verdict has no actionable QA/Security Issue")
        return tuple(issues)

    def _trace_satisfies_success_contract(
        self, run: WorkflowRun, scenario: ScenarioDefinition
    ) -> bool:
        events, total = self._repository.list_events(run.run_id, limit=1000, offset=0)
        if total > len(events):
            return False
        required = set(scenario.requirement_ids)
        planner_ok = any(
            event.event_type == "PLANNER_OUTPUT_VALIDATED"
            and required.issubset(set(event.requirement_ids))
            for event in events
        )
        developer_ok = any(
            event.event_type == "DEVELOPER_ARTIFACTS_VALIDATED"
            and event.code_version == run.code_version
            and required.issubset(set(event.requirement_ids))
            for event in events
        )
        build_ok = any(
            event.event_type == "BUILD_PASSED"
            and event.code_version == run.code_version
            and required.issubset(set(event.requirement_ids))
            for event in events
        )
        fix_ok = run.fix_attempt == 0 or (
            any(event.event_type == "ISSUE_CREATED" for event in events)
            and sum(event.event_type == "FIX_ATTEMPT_STARTED" for event in events)
            >= run.fix_attempt
        )
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
                additional_event_types=additional_event_types,
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
