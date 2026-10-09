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
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, json_text
from agents.llm.engine import LLMEngine
from agents.roles.prompts import prepare_role_prompt
from agents.roles.qa_contract import QAContractError, build_qa_output_contract, validate_qa_decision
from agents.runtime.developer import _host_factory
from agents.runtime.planner import _message_data, _same_json, _TERMINAL
from agents.runtime.qa_context import QAContextError, QAExecutionContext
from agents.runtime.qa_services import QARuntimeServices, QAServicesError
from mcp_tools.execution_runtime import TrackedMCPError
from orchestrator.core.security import redact_data
from orchestrator.domain.states import AgentRole


class QAExecutorConfigurationError(ValueError):
    def __init__(self):
        super().__init__("QA_EXECUTOR_CONFIGURATION_INVALID")


class _QAInputError(ValueError):
    def __init__(self):
        super().__init__("QA_INPUT_INVALID")


_PROTECTED = frozenset({
    "request", "snapshot", "workspaceId", "scenario", "runConfiguration", "plan", "sourceArtifact",
    "outputContract", "metadata", "runId", "workflowStepId", "scenarioId", "requirementIds",
    "projectArtifactIds", "projectArtifactId", "artifactVersion", "model", "limits", "codeVersion",
    "fixRequest", "testSelectors", "testTargets", "qaRequirements", "userRequest", "protectedCases",
    "runtimeInstructions", "securityPolicy", "emailPolicy", "executionManifest", "sourceAccess",
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
            if set(answer) & _PROTECTED:
                raise ValueError
            answers.append(redact_data(answer))
        assigned = {str(value) for value in metadata.requirement_ids}
        return {**expected, "clarifications": answers,
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
                "selected suite, using the exact runner test IDs. Do not submit PASS/FAIL, report IDs, "
                "hashes, execution manifests, measured counts or fabricated tool evidence. "
                "The Host executes selected tests and all configured protected tests and assembles the "
                "measured report. Questions do not authorize changing the frozen Source or policies."
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
        targets.extend({"toolName": "run_unit_tests", "selector": scope.name,
                        "directory": "outputs/qa/" + scope.source_directory,
                        "pattern": scope.pattern, "format": "python-unittest"}
                       for scope in unit.scopes if scope.kind == "QA_TESTS")
    if browser is not None:
        targets.extend({"toolName": "run_browser_tests", "selector": suite.name,
                        "path": "outputs/qa/" + suite.suite_path, "format": "browser-suite-v1",
                        "example": {"format": "browser-suite-v1", "tests": [{
                            "testId": "signup.visible", "steps": [{"action": "goto", "path": "/"},
                            {"action": "assert_visible", "selector": "#signup"}]}]},
                        "allowedActions": {"goto": ["action", "path"],
                            "fill": ["action", "selector", "value"], "click": ["action", "selector"],
                            "assert_text": ["action", "selector", "text"],
                            "assert_visible": ["action", "selector"], "assert_url": ["action", "path"]}}
                       for suite in browser.suites if suite.kind == "QA_TESTS")
    return targets


class QAAgentExecutor(AgentExecutor):
    """Host opt-in QA against the claimed current candidate and report lineage."""

    def __init__(self, *, provider, context_factory, services_factory, usage_sink=None):
        if (not callable(context_factory) or not callable(services_factory)
                or usage_sink is not None and not callable(usage_sink)
                or not isinstance(getattr(provider, "name", None), str) or not provider.name.strip()
                or not callable(getattr(provider, "complete", None))
                or not callable(getattr(provider, "validate_configuration", None))):
            raise QAExecutorConfigurationError()
        self._provider, self._context_factory, self._services_factory = provider, context_factory, services_factory
        self._usage_sink = usage_sink

    def __repr__(self):
        return "QAAgentExecutor()"

    @staticmethod
    def _updater(context, queue):
        if (not isinstance(context.task_id, str) or not context.task_id.strip()
                or not isinstance(context.context_id, str) or not context.context_id.strip()):
            raise InvalidParamsError("SDK-assigned Task and Context IDs are required")
        return TaskUpdater(queue, context.task_id, context.context_id)

    @staticmethod
    async def _status(updater, metadata, state, code, *, questions=None):
        data = {"code": code}
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
            await services.prepare(execution)
            task_input["testSelectors"], task_input["testTargets"] = services.selectors, _test_targets(services)
            async with services.client_factory(services.configuration) as client:
                tracked = services.tracked(client, execution)
                tools = tuple(tool for tool in tracked.list_tools()
                              if tool.name in {"read_project_file", "write_test_file", "read_test_report"})
                if {tool.name for tool in tools} != {"read_project_file", "write_test_file", "read_test_report"}:
                    raise QAServicesError()
                engine = LLMEngine(role=AgentRole.QA, provider=self._provider, tools=tools, tool_executor=tracked)
                result = await engine.run(prompt=prepare_role_prompt(
                    AgentRole.QA, task_input=task_input, metadata=execution.metadata),
                    model=execution.model,
                    output=build_qa_output_contract(execution.metadata.requirement_ids, services.selectors),
                    budget=execution.budget, workspace_id=str(execution.configuration.workspace_id),
                    usage_sink=self._usage_sink)
                decision = validate_qa_decision(result.data, execution.metadata.requirement_ids, services.selectors)
                execution.budget.check()
                if decision.kind == "READY":
                    artifacts = await services.finalize(execution, decision, tracked,
                        task_id=context.task_id, context_id=context.context_id)
            # Finish producer/stdio teardown before an interrupted Task can resume.
            execution.budget.check()
        except asyncio.CancelledError:
            raise
        except LLMRuntimeError as error:
            state = (TaskState.TASK_STATE_AUTH_REQUIRED if error.code is LLMErrorCode.AUTH else
                     TaskState.TASK_STATE_REJECTED if error.code is LLMErrorCode.REFUSAL else TaskState.TASK_STATE_FAILED)
            await self._status(updater, metadata, state, error.code.value)
            return
        except (QAContractError, QAServicesError) as error:
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, error.code)
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
