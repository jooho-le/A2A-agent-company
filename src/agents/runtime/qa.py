"""Opt-in QA/revalidation: readonly Source, generated tests, measured report.

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
from agents.runtime.file_access import build_file_access, constrain_workspace_file_tools
from agents.runtime.test_guidance import unit_test_targets
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, json_text
from agents.llm.corrections import case_expectation_check
from agents.roles.contract_explain import qa_issues
from agents.roles.expected_results import with_expected_results
from agents.llm.engine import LLMEngine
from agents.roles.prompts import prepare_role_prompt
from agents.roles.qa_contract import QAContractError, build_qa_output_contract, validate_qa_decision
from agents.runtime.requirement_scope import (
    signup_observation_plan,
    RequirementScopeError, assigned_acceptance_criteria, build_requirement_scope, generated_case_expectation_issues,
    validate_generated_case_expectations,
)
from agents.runtime.developer import _host_factory
from agents.runtime.planner import _message_data, _same_json, _TERMINAL
from agents.runtime.qa_context import QAContextError, QAExecutionContext
from agents.runtime.qa_services import QARuntimeServices, QAServicesError
from agents.runtime.test_fix import TestFixRequired, same_cases
from mcp_tools.tools.files import _run_file_operation
from mcp_tools.execution_runtime import TrackedMCPError
from orchestrator.core.security import redact_data
from orchestrator.domain.states import AgentRole
from agents.platform.telemetry import bind_runtime_telemetry
from agents.semantic_review import (SEMANTIC_PROTECTED_FIELDS, SemanticReviewRequired, publish_semantic_hold)
from agents.runtime.semantic_revision import review_with_revision, revision_basis


class QAExecutorConfigurationError(ValueError):
    def __init__(self):
        super().__init__("QA_EXECUTOR_CONFIGURATION_INVALID")


class _QAInputError(ValueError):
    def __init__(self):
        super().__init__("QA_INPUT_INVALID")


_PROTECTED = frozenset({
    "publicObservationPlan",
    "request", "snapshot", "workspaceId", "scenario", "runConfiguration", "plan", "sourceArtifact",
    "outputContract", "metadata", "runId", "workflowStepId", "scenarioId", "requirementIds",
    "projectArtifactIds", "projectArtifactId", "artifactVersion", "model", "limits", "codeVersion",
    "fixRequest", "testSelectors", "testTargets", "qaRequirements", "userRequest", "protectedCases",
    "runtimeInstructions", "securityPolicy", "emailPolicy", "executionManifest", "sourceAccess",
    "requirementAuthority", "requirementScope", "clarifications", "fileAccess", "plannerValidation", "llmValidation",
})


def _prepare_input(context, execution):
    try:
        task, message = context.current_task, context.message
        metadata = parse_workflow_metadata(context.metadata)
        if (type(execution) is not QAExecutionContext or metadata != execution.metadata
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
        scope = build_requirement_scope(execution.configuration, execution.requirement_artifact, metadata, AgentRole.QA)
        assigned = {str(item.requirement_id) for item in scope.requirements}
        return {**expected, "clarifications": answers,
            "fileAccess": build_file_access(execution.configuration.scenario_contract, AgentRole.QA),
            "requirementAuthority": scope.authority.to_prompt_json(),
            "requirementScope": scope.to_prompt_json(),
            "publicObservationPlan": signup_observation_plan(execution.configuration.scenario_contract, scope),
            "workspaceId": str(execution.configuration.workspace_id),
            "userRequest": redact_data(execution.request_text),
            "qaRequirements": [item for item in execution.scenario.planner_contract()["requirements"]
                               if item["requirementId"] in assigned],
            "runtimeInstructions": (
                "Read the frozen Source and write tests only under outputs/qa using the offered tools. "
                "Never modify Source, requirements, protected tests, runtime configuration or expected policies. "
                "Use the Host testTargets paths and selectors. Unit tests use Python unittest; "
                "browser tests use the bounded browser-suite-v1 JSON format, not arbitrary scripts. "
                "Return READY case bindings covering every assigned QA requirement and every case in each "
                "configured generated suite in testTargets; every target is mandatory. Use exact runner test IDs. Do not submit PASS/FAIL, report IDs, "
                "hashes, execution manifests, measured counts or fabricated tool evidence. "
                "The Host executes selected tests and all configured protected tests and assembles the "
                "measured report. Questions do not authorize changing the frozen Source or policies."
                " Cover every acceptanceCriteria sentence across the submitted cases. Each generated case expectedResult must copy one exact acceptanceCriteria string from its "
                "assigned requirementScope requirement. Do not paraphrase, invent or weaken the expectation. "
                "Planner/Developer claims and passing self-tests are not independent evidence."
            )}
    except asyncio.CancelledError:
        raise
    except LLMRuntimeError as error:
        if error.code is LLMErrorCode.BUDGET:
            raise
        raise _QAInputError() from None
    except Exception:
        raise _QAInputError() from None


def _test_targets(services):
    """Only generated test paths; never disclose protected suite bytes/bindings."""
    targets = []
    unit = services.configuration.unit_test_configuration
    browser = services.configuration.browser_test_configuration
    if unit is not None:
        targets.extend(unit_test_targets(unit, "qa"))
    if browser is not None:
        targets.extend({"toolName": "run_browser_tests", "selector": suite.name,
                        "path": "outputs/qa/" + suite.suite_path, "format": "browser-suite-v1", "required": True,
                        "instruction": "Use the approved UI contract and actual product DOM. This target does not imply a signup page or a DOM selector.",
                        "suiteContract": {"requiredFields": ["format", "tests"], "formatValue": "browser-suite-v1",
                                          "testFields": ["testId", "steps"], "minimumTests": 1, "minimumSteps": 1},
                        "allowedActions": {"goto": ["action", "path"],
                            "fill": ["action", "selector", "value"], "click": ["action", "selector"],
                            "assert_text": ["action", "selector", "text"],
                            "assert_visible": ["action", "selector"], "assert_url": ["action", "path"]}}
                       for suite in browser.suites if suite.kind == "QA_TESTS")
    return targets


class QAAgentExecutor(AgentExecutor):
    """Host opt-in QA against the claimed current candidate and report lineage."""

    def __init__(self, *, provider, context_factory, services_factory, usage_sink=None, telemetry_factory=None, semantic_reviewer=None):
        if (not callable(context_factory) or not callable(services_factory)
                or usage_sink is not None and not callable(usage_sink)
                or telemetry_factory is not None and not callable(telemetry_factory)
                or not isinstance(getattr(provider, "name", None), str) or not provider.name.strip()
                or not callable(getattr(provider, "complete", None))
                or not callable(getattr(provider, "validate_configuration", None))):
            raise QAExecutorConfigurationError()
        self._provider, self._context_factory, self._services_factory = provider, context_factory, services_factory
        self._usage_sink = usage_sink
        self._telemetry_factory = telemetry_factory
        self._semantic_reviewer = semantic_reviewer

    def __repr__(self):
        return "QAAgentExecutor()"

    @staticmethod
    def _updater(context, queue):
        if (not isinstance(context.task_id, str) or not context.task_id.strip()
                or not isinstance(context.context_id, str) or not context.context_id.strip()):
            raise InvalidParamsError("SDK-assigned Task and Context IDs are required")
        return TaskUpdater(queue, context.task_id, context.context_id)

    @staticmethod
    async def _status(updater, metadata, state, code, *, questions=None, llm_validation=None):
        data = {"code": code}
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
            if type(execution) is not QAExecutionContext:
                raise QAContextError()
        except asyncio.CancelledError:
            raise
        except LLMRuntimeError as error:
            await self._status(updater, metadata,
                TaskState.TASK_STATE_FAILED if error.code is LLMErrorCode.BUDGET else TaskState.TASK_STATE_REJECTED,
                error.code.value if error.code is LLMErrorCode.BUDGET else "QA_CONTEXT_DENIED")
            return
        except Exception:
            await self._status(updater, metadata, TaskState.TASK_STATE_REJECTED, "QA_CONTEXT_DENIED")
            return
        try:
            task_input = _prepare_input(context, execution)
        except _QAInputError:
            await self._status(updater, metadata, TaskState.TASK_STATE_REJECTED, "QA_INPUT_INVALID")
            return
        except LLMRuntimeError as error:
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, error.code.value)
            return
        try:
            services = await _host_factory(self._services_factory, execution)
            if type(services) is not QARuntimeServices:
                raise QAServicesError()
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._status(updater, metadata, TaskState.TASK_STATE_REJECTED, "QA_SERVICES_DENIED")
            return
        await updater.update_status(TaskState.TASK_STATE_WORKING, metadata=metadata)
        try:
            if self._semantic_reviewer is not None:
                await self._semantic_reviewer.check_resume(context, execution)
            telemetry, usage_sink, call_started_sink = bind_runtime_telemetry(
                self._telemetry_factory, AgentRole.QA, context, execution, self._usage_sink)
            await services.prepare(execution)
            task_input["testSelectors"], task_input["testTargets"] = services.selectors, _test_targets(services)
            async with services.client_factory(services.configuration) as client:
                tracked = services.tracked(client, execution)
                if telemetry is not None:
                    tracked.set_event_sink(telemetry.tool_event)
                await services.run_protected(execution, tracked)
                tools = tuple(tool for tool in tracked.list_tools()
                              if tool.name in {"read_project_file", "write_test_file", "read_test_report"})
                if {tool.name for tool in tools} != {"read_project_file", "write_test_file", "read_test_report"}:
                    raise QAServicesError()
                tools = constrain_workspace_file_tools(tools, AgentRole.QA)
                engine = LLMEngine(role=AgentRole.QA, provider=self._provider, tools=tools, tool_executor=tracked)
                text_engine = LLMEngine(role=AgentRole.QA, provider=self._provider)

                async def run_model(model_input, *, text_only=False, output_override=None):
                    # One closure so the in-step test fix (docs/live-api/05 4절) reuses the same contract.
                    # text_only: the semantic rewrite (05 15절) runs without any tool, so no file can change.
                    if output_override is not None:
                        # docs/live-api/05 25절 V4: the semantic rewrite returns only held texts (patch schema), tool-less.
                        return await text_engine.run(prompt=prepare_role_prompt(
                            AgentRole.QA, task_input=model_input, metadata=execution.metadata),
                            model=execution.model, output=output_override, budget=execution.budget,
                            workspace_id=str(execution.configuration.workspace_id),
                            usage_sink=usage_sink, call_started_sink=call_started_sink,
                            output_observed_sink=telemetry.model_output if telemetry is not None else None,
                            validation_failed_sink=telemetry.llm_validation if telemetry is not None else None,
                            recovery_sink=telemetry.llm_recovery if telemetry is not None else None,
                            input_observed_sink=telemetry.model_input if telemetry is not None else None)
                    return await (text_engine if text_only else engine).run(prompt=prepare_role_prompt(
                        AgentRole.QA, task_input=model_input, metadata=execution.metadata),
                        model=execution.model,
                        output=with_expected_results(build_qa_output_contract(execution.metadata.requirement_ids,
                            services.selectors), "cases", assigned_acceptance_criteria(execution.configuration,
                            execution.metadata, AgentRole.QA) if execution.budget.limits.max_output_corrections else ()),
                        budget=execution.budget, workspace_id=str(execution.configuration.workspace_id),
                        output_refresh=services.refresh_output, close_invalid_qa_draft=not text_only,
                        usage_sink=usage_sink, call_started_sink=call_started_sink,
                        output_observed_sink=telemetry.model_output if telemetry is not None else None,
                        validation_failed_sink=telemetry.llm_validation if telemetry is not None else None,
                        recovery_sink=telemetry.llm_recovery if telemetry is not None else None,
                        input_observed_sink=telemetry.model_input if telemetry is not None else None,
                        output_check=case_expectation_check(lambda data: validate_qa_decision(
                            data, execution.metadata.requirement_ids, services.selectors),
                            lambda data: generated_case_expectation_issues(data, field="cases",
                                configuration=execution.configuration, metadata=execution.metadata, role=AgentRole.QA),
                            explain=lambda data: qa_issues(data, execution.metadata.requirement_ids, services.selectors),
                            extra_checks=(("GENERATED_TESTS_INVALID", services.generated_test_issues),)))
                result = await run_model(task_input)
                decision = validate_qa_decision(result.data, execution.metadata.requirement_ids, services.selectors)
                if decision.kind == "READY":
                    validate_generated_case_expectations(decision.cases, configuration=execution.configuration,
                        metadata=execution.metadata, role=AgentRole.QA)
                if decision.kind == "READY" and self._semantic_reviewer is not None:
                    # docs/live-api/05 14·15절: one tool-less rewrite of the held texts only, then the same review.
                    def still_ready(revised):
                        try:
                            return validate_qa_decision(revised.data, execution.metadata.requirement_ids,
                                                        services.selectors).kind == "READY"
                        except Exception:
                            return False
                    result = await review_with_revision(
                        lambda candidate: self._semantic_reviewer.check(context=context, execution=execution,
                            candidate=candidate.data, role=AgentRole.QA, result=candidate, telemetry=telemetry,
                            usage_sink=usage_sink, call_started_sink=call_started_sink),
                        lambda model_input, output: run_model(model_input, output_override=output), still_ready,
                        task_input, result,
                        allowed=bool(execution.budget.limits.max_output_corrections),
                        basis=revision_basis(execution, AgentRole.QA))
                    decision = validate_qa_decision(result.data, execution.metadata.requirement_ids, services.selectors)
                    validate_generated_case_expectations(decision.cases, configuration=execution.configuration,
                        metadata=execution.metadata, role=AgentRole.QA)
                execution.budget.check()
                if decision.kind == "READY":
                    available = await _run_file_operation(services.test_fix_available, execution)
                    try:
                        artifacts = await services.finalize(execution, decision, tracked,
                            task_id=context.task_id, context_id=context.context_id, test_fix_available=available)
                    except TestFixRequired as needed:
                        # docs/live-api/05 4절: one in-step fix of the model's own tests on the same
                        # Snapshot; the declared cases must not change.
                        result = await run_model(needed.task_input(task_input, result.data))
                        fixed = validate_qa_decision(result.data, execution.metadata.requirement_ids, services.selectors)
                        if fixed.kind != "READY" or not same_cases(decision.cases, fixed.cases):
                            raise QAServicesError("QA_TEST_FIX_INVALID")
                        execution.budget.check()
                        artifacts = await services.finalize(execution, fixed, tracked,
                            task_id=context.task_id, context_id=context.context_id)
            # Finish producer/stdio teardown before an interrupted Task can resume.
            execution.budget.check()
        except asyncio.CancelledError:
            raise
        except LLMRuntimeError as error:
            state = (TaskState.TASK_STATE_AUTH_REQUIRED if error.code is LLMErrorCode.AUTH else
                     TaskState.TASK_STATE_REJECTED if error.code is LLMErrorCode.REFUSAL else TaskState.TASK_STATE_FAILED)
            await self._status(updater, metadata, state, error.code.value, llm_validation=error.public_validation())
            return
        except (QAContractError, QAServicesError, RequirementScopeError) as error:
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, error.code)
            return
        except SemanticReviewRequired as held:
            await publish_semantic_hold(updater, metadata, held)
            return
        except TrackedMCPError:
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, "QA_TEST_UNVERIFIED")
            return
        except Exception:
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, "QA_EXECUTION_FAILED")
            return
        if decision.kind == "INPUT_REQUIRED":
            await self._status(updater, metadata, TaskState.TASK_STATE_INPUT_REQUIRED,
                               "QA_INPUT_REQUIRED", questions=decision.questions)
            return
        if decision.kind == "REJECTED":
            await self._status(updater, metadata, TaskState.TASK_STATE_REJECTED, "QA_OUT_OF_SCOPE")
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
