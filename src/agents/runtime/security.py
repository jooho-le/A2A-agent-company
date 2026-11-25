"""Opt-in initial Security: frozen scans, grounded proposals, Host proof admission.

Neither a model verdict nor A2A COMPLETED establishes project success.
Only the Orchestrator changes Workflow/Registry state or starts fix cycles.
"""

import asyncio

from a2a.helpers import new_data_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Role, TaskState
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict

from agents.api.validation import parse_workflow_metadata
from agents.runtime.file_access import build_file_access, constrain_frozen_read_tools, constrain_workspace_file_tools
from agents.runtime.test_guidance import unit_test_targets
from agents.runtime.service_scope import service_input
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, ToolCall, ToolContext, json_text, parse_json
from agents.llm.corrections import case_expectation_check
from agents.roles.contract_explain import security_issues
from agents.roles.expected_results import with_expected_results
from agents.roles.security_draft import SecurityTestDraft, TEST_PLAN_INSTRUCTIONS
from agents.llm.engine import LLMEngine
from agents.roles.prompts import prepare_role_prompt
from agents.roles.security_contract import SecurityContractError, build_security_output_contract, validate_security_decision
from agents.runtime.requirement_scope import (
    signup_observation_plan,
    RequirementScopeError, assigned_acceptance_criteria, build_requirement_scope, generated_case_expectation_issues,
    validate_generated_case_expectations,
)
from agents.runtime.developer import _host_factory
from agents.runtime.planner import _message_data, _same_json, _TERMINAL
from agents.runtime.security_context import SecurityContextError, SecurityExecutionContext
from agents.runtime.security_services import SecurityRuntimeServices, SecurityServicesError
from agents.runtime.test_fix import TestFixRequired, same_cases
from mcp_tools.tools.files import _run_file_operation
from mcp_tools.execution_runtime import TrackedMCPError
from orchestrator.core.security import redact_data
from orchestrator.domain.states import AgentRole
from agents.platform.telemetry import bind_runtime_telemetry
from agents.semantic_review import (SEMANTIC_PROTECTED_FIELDS, SemanticReviewRequired, publish_semantic_hold)
from agents.runtime.semantic_revision import review_with_revision, revision_basis
from orchestrator.workspaces.policy import workspace_uuid


class SecurityExecutorConfigurationError(ValueError):
    def __init__(self):
        super().__init__("SECURITY_EXECUTOR_CONFIGURATION_INVALID")


class _SecurityInputError(ValueError):
    def __init__(self):
        super().__init__("SECURITY_INPUT_INVALID")


_PROTECTED = frozenset({
    "serviceContract", "sourceInterface",
    "publicObservationPlan",
    "request", "snapshot", "workspaceId", "scenario", "runConfiguration", "plan", "sourceArtifact",
    "outputContract", "metadata", "runId", "workflowStepId", "scenarioId", "requirementIds",
    "projectArtifactIds", "projectArtifactId", "artifactVersion", "model", "limits", "codeVersion",
    "fixRequest", "scannerProfiles", "measuredSecurity", "securityRequirements", "userRequest", "sourcePaths",
    "runtimeInstructions", "securityPolicy", "emailPolicy", "executionManifest", "sourceAccess",
    "testSelectors", "testTargets", "sourceReferenceCatalog",
    "requirementAuthority", "requirementScope", "clarifications", "fileAccess", "plannerValidation", "llmValidation", "llmFailure",
})


def _prepare_input(context, execution):
    try:
        task, message = context.current_task, context.message
        metadata = parse_workflow_metadata(context.metadata)
        if (type(execution) is not SecurityExecutionContext or metadata != execution.metadata
                or task is None or task.id != context.task_id or task.context_id != context.context_id
                or task.status.state != TaskState.TASK_STATE_SUBMITTED or task.artifacts
                or parse_workflow_metadata(MessageToDict(task.metadata)) != metadata or message is None):
            raise ValueError
        execution.budget.check()
        maximum = execution.budget.limits.max_json_bytes
        json_text(MessageToDict(task), max_bytes=maximum)
        inputs = [item for item in task.history if item.role == Role.ROLE_USER]
        if (not inputs or len(inputs) > 32 or len({item.message_id for item in inputs}) != len(inputs)
                or inputs[-1].message_id != message.message_id or len(message.parts) != 1
                or len(inputs[-1].parts) != 1
                or not _same_json(MessageToDict(inputs[-1].parts[0]), MessageToDict(message.parts[0]))):
            raise ValueError
        expected = redact_data(execution.initial_payload)
        original = _message_data(inputs[0], maximum)
        _message_data(message, maximum)
        if set(original) != set(expected) or not _same_json(original, expected):
            raise ValueError
        answers = []
        for item in inputs[1:]:
            answer = _message_data(item, maximum)
            if set(answer) & (_PROTECTED | SEMANTIC_PROTECTED_FIELDS):
                raise ValueError
            answers.append(redact_data(answer))
        scope = build_requirement_scope(execution.configuration, execution.requirement_artifact, metadata, AgentRole.SECURITY)
        assigned = {str(item.requirement_id) for item in scope.requirements}
        return {**expected, **service_input(execution), "clarifications": answers,
            "fileAccess": build_file_access(execution.configuration.scenario_contract, AgentRole.SECURITY),
            "requirementAuthority": scope.authority.to_prompt_json(),
            "requirementScope": scope.to_prompt_json(),
            "publicObservationPlan": signup_observation_plan(execution.configuration.scenario_contract, scope),
            "workspaceId": str(execution.configuration.workspace_id),
            "userRequest": redact_data(execution.request_text),
            "securityRequirements": [item for item in execution.scenario.planner_contract()["requirements"]
                                     if item["requirementId"] in assigned],
            "runtimeInstructions": (
                "Inspect the measured scanner reports and read the exact frozen Source using the offered tools. "
                "Return a bounded READY review covering every assigned requirement and Host finding ID exactly once. "
                "Source references use paths from sourcePaths and real inclusive line ranges from files you read. "
                "Propose PASS/FAIL or CONFIRMED/FALSE_POSITIVE only with explicit code reasoning, not scanner ranks "
                "or a zero-warning count. Do not include source snippets, secrets, report IDs, manifests, hashes, "
                "tool receipts, final verdicts, commands, new policies or source writes. "
                "These are proposals, not proven outcomes: only a trusted Host semantic verifier can admit "
                "PASS/FAIL or CONFIRMED/FALSE_POSITIVE against actual read evidence; otherwise requirements "
                "stay UNVERIFIED and scanner warnings stay SUSPECTED. Questions cannot change frozen policies."
                " Use only the approved requirementScope as the acceptance baseline, independently of prior "
                "role claims. Cover every acceptanceCriteria sentence across the submitted cases. Any generated testCases expectedResult must copy one exact acceptanceCriteria "
                "string for its own assigned requirement, without extra restrictions or weaker conditions."
            )}
    except asyncio.CancelledError:
        raise
    except LLMRuntimeError as error:
        if error.code is LLMErrorCode.BUDGET:
            raise
        raise _SecurityInputError() from None
    except Exception:
        raise _SecurityInputError() from None


_TEST_INSTRUCTIONS = (
    "Inspect the measured scanner reports and read the exact frozen Source using the offered tools. "
    "measuredSecurity.profiles already contains measured findings. If reading the full report, copy "
    "its exact profiles[].reportRef into read_security_report; a scanner profile name/profileRef is not a reportRef. "
    "Write Python unittest tests only under outputs/security using write_test_file and the Host testTargets, "
    "exercising the assigned security requirements against the running product (for example stored password "
    "values, database constraints, and responses or logs that must not carry secrets). "
    "Return a bounded READY review covering every assigned requirement and Host finding ID exactly once, and "
    "testCases binding every assigned requirement to at least one of your tests by exact runner test ID. "
    "Do not submit report IDs, manifests, hashes, tool receipts, final verdicts, commands, new policies or source "
    "writes. The Host executes your tests and all configured protected Security tests; only those measured results "
    "decide requirement outcomes. Scanner warnings stay SUSPECTED unless proven. Questions cannot change frozen policies. "
    "This READY draft is produced BEFORE the Host executes those tests. Keep requirementReviews.proposedOutcome "
    "UNVERIFIED and use the Host's typed pending-state marker in rationale. Describe the approved test intent "
    "in testCases and leave results to Host execution. Do not broaden or weaken any acceptance condition."
)


def _test_targets(services, execution=None):
    """Only generated Security test paths; never disclose protected suite bytes/bindings."""
    unit = services.configuration.unit_test_configuration
    return unit_test_targets(unit, "security", scenario_key=execution.scenario.key if execution else None)


class _SecurityReadExecutor:
    """Only measured read capabilities; the model cannot launch or omit scans."""

    def __init__(self, services, tracked, execution, measured):
        self._services, self._tracked = services, tracked
        self._execution, self._measured = execution, measured
        self._call_ids = set()

    async def execute(self, call, arguments, context):
        binding = self._tracked._client.configuration.binding
        try:
            if (not isinstance(call, ToolCall) or not isinstance(context, ToolContext)
                    or context.role is not AgentRole.SECURITY or binding.agent_role is not context.role
                    or workspace_uuid(context.workspace_id) != binding.workspace_id
                    or context.deadline_monotonic != self._execution.budget.deadline_monotonic
                    or call.name not in {"read_project_file", "read_security_report"}
                    | ({"write_test_file"} if self._services.test_selectors else set())
                    or type(call.call_id) is not str or not 1 <= len(call.call_id.encode("utf-8")) <= 4096
                    or call.call_id in self._call_ids or len(self._call_ids) >= 1000
                    or parse_json(call.arguments_json) != arguments):
                raise ValueError
            self._call_ids.add(call.call_id)
        except Exception:
            raise TrackedMCPError("MCP_EXECUTION_CONTEXT_DENIED") from None
        result = await self._services.invoke(self._tracked, self._execution, call.name, arguments, self._measured)
        return result.data


class SecurityAgentExecutor(AgentExecutor):
    """Host opt-in Security for the claimed current candidate and report lineage."""

    def __init__(self, *, provider, context_factory, services_factory, usage_sink=None, telemetry_factory=None, semantic_reviewer=None):
        if (not callable(context_factory) or not callable(services_factory)
                or usage_sink is not None and not callable(usage_sink)
                or telemetry_factory is not None and not callable(telemetry_factory)
                or not isinstance(getattr(provider, "name", None), str) or not provider.name.strip()
                or not callable(getattr(provider, "complete", None))
                or not callable(getattr(provider, "validate_configuration", None))):
            raise SecurityExecutorConfigurationError()
        self._provider, self._context_factory, self._services_factory = provider, context_factory, services_factory
        self._usage_sink = usage_sink
        self._telemetry_factory = telemetry_factory
        self._semantic_reviewer = semantic_reviewer

    def __repr__(self):
        return "SecurityAgentExecutor()"

    @staticmethod
    def _updater(context, queue):
        if (not isinstance(context.task_id, str) or not context.task_id.strip()
                or not isinstance(context.context_id, str) or not context.context_id.strip()):
            raise InvalidParamsError("SDK-assigned Task and Context IDs are required")
        return TaskUpdater(queue, context.task_id, context.context_id)

    @staticmethod
    async def _status(updater, metadata, state, code, *, questions=None, llm_validation=None, llm_failure=None):
        data = {"code": code}
        if llm_failure is not None:
            data['llmFailure'] = llm_failure
        if llm_validation is not None:
            data["llmValidation"] = llm_validation
        if questions is not None:
            data["questions"] = list(questions)
        await updater.update_status(state, metadata=metadata, message=updater.new_agent_message(
            parts=[new_data_part(data, media_type="application/json")]))

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        updater = self._updater(context, event_queue)
        if context.current_task is not None and context.current_task.status.state in _TERMINAL:
            return
        metadata = context.metadata
        try:
            execution = await _host_factory(self._context_factory, context)
            if type(execution) is not SecurityExecutionContext:
                raise SecurityContextError()
        except asyncio.CancelledError:
            raise
        except LLMRuntimeError as error:
            await self._status(updater, metadata,
                TaskState.TASK_STATE_FAILED if error.code is LLMErrorCode.BUDGET else TaskState.TASK_STATE_REJECTED,
                error.code.value if error.code is LLMErrorCode.BUDGET else "SECURITY_CONTEXT_DENIED")
            return
        except Exception:
            await self._status(updater, metadata, TaskState.TASK_STATE_REJECTED, "SECURITY_CONTEXT_DENIED")
            return
        try:
            task_input = _prepare_input(context, execution)
        except _SecurityInputError:
            await self._status(updater, metadata, TaskState.TASK_STATE_REJECTED, "SECURITY_INPUT_INVALID")
            return
        except LLMRuntimeError as error:
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, error.code.value)
            return
        try:
            services = await _host_factory(self._services_factory, execution)
            if type(services) is not SecurityRuntimeServices:
                raise SecurityServicesError()
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._status(updater, metadata, TaskState.TASK_STATE_REJECTED, "SECURITY_SERVICES_DENIED")
            return
        await updater.update_status(TaskState.TASK_STATE_WORKING, metadata=metadata)
        try:
            if self._semantic_reviewer is not None:
                await self._semantic_reviewer.check_resume(context, execution)
            telemetry, usage_sink, call_started_sink = bind_runtime_telemetry(
                self._telemetry_factory, AgentRole.SECURITY, context, execution, self._usage_sink)
            await services.prepare(execution)
            from orchestrator.infrastructure.run_feedback import feedback_input
            task_input.update(await _run_file_operation(feedback_input, services, execution))
            async with services.client_factory(services.configuration) as client:
                tracked = services.tracked(client, execution)
                if telemetry is not None:
                    tracked.set_event_sink(telemetry.tool_event)
                await services.run_protected(execution, tracked)
                measured = await services.scan(execution, tracked)
                task_input["measuredSecurity"] = measured.analysis_input
                task_input["sourcePaths"] = list(measured.source_paths)
                task_input["fileAccess"] = build_file_access(execution.configuration.scenario_contract,
                    AgentRole.SECURITY, observed_source_paths=measured.source_paths)
                wanted = {"read_project_file", "read_security_report"}
                draft_wire = None
                if services.test_selectors:
                    # Same shape as QA: the model writes and binds its own tests; the Host runs them.
                    wanted.add("write_test_file")
                    task_input["testSelectors"] = {"run_unit_tests": list(services.test_selectors)}
                    task_input["testTargets"] = _test_targets(services, execution)
                    draft_wire = SecurityTestDraft(measured.source_files, execution.source.snapshot_sha256)
                    task_input["sourceReferenceCatalog"] = draft_wire.catalog
                    task_input["runtimeInstructions"] = _TEST_INSTRUCTIONS + ' ' + TEST_PLAN_INSTRUCTIONS
                tools = tuple(tool for tool in tracked.list_tools() if tool.name in wanted)
                if {tool.name for tool in tools} != wanted:
                    raise SecurityServicesError()
                tools = constrain_frozen_read_tools(
                    constrain_workspace_file_tools(tools, AgentRole.SECURITY), measured.source_paths,
                    [receipt.report_ref for _record, receipt, _evidence in measured.scans])
                engine = LLMEngine(role=AgentRole.SECURITY, provider=self._provider, tools=tools,
                    tool_executor=_SecurityReadExecutor(services, tracked, execution, measured))
                decode = draft_wire.decode if draft_wire is not None else lambda data: data
                def validate(data):
                    return validate_security_decision(decode(data), execution.metadata.requirement_ids,
                        measured.finding_ids, measured.source_paths, services.test_selectors or None)
                output = with_expected_results(build_security_output_contract(
                    execution.metadata.requirement_ids, measured.finding_ids, measured.source_paths,
                    services.test_selectors or None), "testCases", assigned_acceptance_criteria(
                    execution.configuration, execution.metadata, AgentRole.SECURITY)
                    if execution.budget.limits.max_output_corrections else ())
                if draft_wire is not None:
                    output = draft_wire.output(output)
                text_engine = LLMEngine(role=AgentRole.SECURITY, provider=self._provider)

                async def run_model(model_input, *, text_only=False, output_override=None):
                    # One closure so the in-step test fix (docs/live-api/05 4절) reuses the same contract.
                    # text_only: the semantic rewrite (05 15절) runs without any tool, so no file can change.
                    if output_override is not None:
                        # docs/live-api/05 25절 V4: the semantic rewrite returns only held texts (patch schema), tool-less.
                        return await text_engine.run(prompt=prepare_role_prompt(
                            AgentRole.SECURITY, task_input=model_input, metadata=execution.metadata),
                            model=execution.model, output=output_override, budget=execution.budget,
                            workspace_id=str(execution.configuration.workspace_id),
                            usage_sink=usage_sink, call_started_sink=call_started_sink,
                            output_observed_sink=telemetry.model_output if telemetry is not None else None,
                            validation_failed_sink=telemetry.llm_validation if telemetry is not None else None,
                            recovery_sink=telemetry.llm_recovery if telemetry is not None else None,
                            input_observed_sink=telemetry.model_input if telemetry is not None else None)
                    return await (text_engine if text_only else engine).run(prompt=prepare_role_prompt(
                        AgentRole.SECURITY, task_input=model_input, metadata=execution.metadata),
                        model=execution.model,
                        output=output,
                        budget=execution.budget, workspace_id=str(execution.configuration.workspace_id),
                        usage_sink=usage_sink, call_started_sink=call_started_sink,
                        output_observed_sink=telemetry.model_output if telemetry is not None else None,
                        validation_failed_sink=telemetry.llm_validation if telemetry is not None else None,
                        recovery_sink=telemetry.llm_recovery if telemetry is not None else None,
                        input_observed_sink=telemetry.model_input if telemetry is not None else None,
                        # Codex review 2026-11-18 #1·#2: Host-catalogued test IDs and the evidence-bound
                        # technical stop, as for QA, whenever Security writes generated tests.
                        output_refresh=(None if not services.test_selectors else
                            (lambda base: services.refresh_output(draft_wire.restrict_to_read(
                                base, services.read_paths(measured))) if draft_wire is not None
                             else services.refresh_output(base))),
                        close_invalid_qa_draft=bool(services.test_selectors) and not text_only,
                        output_check=case_expectation_check(validate,
                            (lambda data: generated_case_expectation_issues(data, field="testCases",
                                configuration=execution.configuration, metadata=execution.metadata,
                                role=AgentRole.SECURITY)) if services.test_selectors else None,
                            explain=lambda data: security_issues(decode(data), execution.metadata.requirement_ids,
                                measured.finding_ids, measured.source_paths, services.test_selectors or None),
                            extra_checks=(("GENERATED_TESTS_INVALID", services.generated_test_issues),
                                          ("CODE_REFERENCE_INVALID", lambda data: (
                                              draft_wire.reference_issues(services, measured, data) if draft_wire is not None
                                              else services.reference_issues(measured, data))))))
                result = await run_model(task_input)
                decision = validate(result.data)
                if decision.kind == "READY" and services.test_selectors:
                    validate_generated_case_expectations(decision.test_cases, configuration=execution.configuration,
                        metadata=execution.metadata, role=AgentRole.SECURITY)
                if decision.kind == "READY" and self._semantic_reviewer is not None:
                    # docs/live-api/05 14·15절: one tool-less rewrite of the held texts only, then the same review.
                    def still_ready(revised):
                        try:
                            return validate(revised.data).kind == "READY"
                        except Exception:
                            return False
                    result = await review_with_revision(
                        lambda candidate: self._semantic_reviewer.check(context=context, execution=execution,
                            candidate=candidate.data, role=AgentRole.SECURITY, result=candidate, telemetry=telemetry,
                            usage_sink=usage_sink, call_started_sink=call_started_sink),
                        lambda model_input, output: run_model(model_input, output_override=output), still_ready,
                        task_input, result,
                        allowed=bool(execution.budget.limits.max_output_corrections),
                        basis=revision_basis(execution, AgentRole.SECURITY))
                    decision = validate(result.data)
                    if services.test_selectors:
                        validate_generated_case_expectations(decision.test_cases, configuration=execution.configuration,
                            metadata=execution.metadata, role=AgentRole.SECURITY)
                execution.budget.check()
                if decision.kind == "READY":
                    available = bool(services.test_selectors) and await _run_file_operation(
                        services.test_fix_available, execution)
                    try:
                        artifacts = await services.finalize(execution, decision, tracked, measured,
                            task_id=context.task_id, context_id=context.context_id, test_fix_available=available)
                    except TestFixRequired as needed:
                        # docs/live-api/05 4절: one in-step fix of the model's own tests on the same
                        # Snapshot; the declared cases must not change.
                        result = await run_model(needed.task_input(task_input, result.data))
                        fixed = validate(result.data)
                        if fixed.kind != "READY" or not same_cases(decision.test_cases, fixed.test_cases):
                            raise SecurityServicesError("SECURITY_TEST_FIX_INVALID")
                        if self._semantic_reviewer is not None:
                            await self._semantic_reviewer.check(context=context, execution=execution, candidate=result.data,
                                role=AgentRole.SECURITY, result=result, telemetry=telemetry,
                                usage_sink=usage_sink, call_started_sink=call_started_sink)
                        execution.budget.check()
                        artifacts = await services.finalize(execution, fixed, tracked, measured,
                            task_id=context.task_id, context_id=context.context_id)
            # Finish producer/stdio teardown before an interrupted Task can resume.
            execution.budget.check()
        except asyncio.CancelledError:
            raise
        except LLMRuntimeError as error:
            state = (TaskState.TASK_STATE_AUTH_REQUIRED if error.code is LLMErrorCode.AUTH else
                     TaskState.TASK_STATE_REJECTED if error.code is LLMErrorCode.REFUSAL else TaskState.TASK_STATE_FAILED)
            await self._status(updater, metadata, state, error.code.value, llm_validation=error.public_validation(),
                               llm_failure=error.public_failure())
            return
        except (SecurityContractError, SecurityServicesError, RequirementScopeError) as error:
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, error.code)
            return
        except SemanticReviewRequired as held:
            await publish_semantic_hold(updater, metadata, held)
            return
        except TrackedMCPError:
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, "SECURITY_SCAN_UNVERIFIED")
            return
        except Exception:
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, "SECURITY_EXECUTION_FAILED")
            return
        if decision.kind == "INPUT_REQUIRED":
            await self._status(updater, metadata, TaskState.TASK_STATE_INPUT_REQUIRED,
                               "SECURITY_INPUT_REQUIRED", questions=decision.questions)
            return
        if decision.kind == "REJECTED":
            await self._status(updater, metadata, TaskState.TASK_STATE_REJECTED, "SECURITY_OUT_OF_SCOPE")
            return
        for artifact in artifacts:
            await updater.add_artifact(list(artifact.parts), artifact_id=artifact.artifact_id,
                                       name=artifact.name, metadata=MessageToDict(artifact.metadata))
        await updater.update_status(TaskState.TASK_STATE_COMPLETED, metadata=metadata)

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        updater = self._updater(context, event_queue)
        task = context.current_task
        if task is None or task.id != context.task_id or task.context_id != context.context_id:
            raise InvalidParamsError("A stored Task of the same identity is required")
        if task.status.state in _TERMINAL:
            return
        metadata = MessageToDict(task.metadata)
        parse_workflow_metadata(metadata)
        await updater.update_status(TaskState.TASK_STATE_CANCELED, metadata=metadata)
