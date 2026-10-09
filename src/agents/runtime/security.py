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
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, ToolCall, ToolContext, json_text, parse_json
from agents.llm.engine import LLMEngine
from agents.roles.prompts import prepare_role_prompt
from agents.roles.security_contract import SecurityContractError, build_security_output_contract, validate_security_decision
from agents.runtime.developer import _host_factory
from agents.runtime.planner import _message_data, _same_json, _TERMINAL
from agents.runtime.security_context import SecurityContextError, SecurityExecutionContext
from agents.runtime.security_services import SecurityRuntimeServices, SecurityServicesError
from mcp_tools.execution_runtime import TrackedMCPError
from orchestrator.core.security import redact_data
from orchestrator.domain.states import AgentRole
from agents.platform.telemetry import bind_runtime_telemetry
from orchestrator.workspaces.policy import workspace_uuid


class SecurityExecutorConfigurationError(ValueError):
    def __init__(self):
        super().__init__("SECURITY_EXECUTOR_CONFIGURATION_INVALID")


class _SecurityInputError(ValueError):
    def __init__(self):
        super().__init__("SECURITY_INPUT_INVALID")


_PROTECTED = frozenset({
    "request", "snapshot", "workspaceId", "scenario", "runConfiguration", "plan", "sourceArtifact",
    "outputContract", "metadata", "runId", "workflowStepId", "scenarioId", "requirementIds",
    "projectArtifactIds", "projectArtifactId", "artifactVersion", "model", "limits", "codeVersion",
    "fixRequest", "scannerProfiles", "measuredSecurity", "securityRequirements", "userRequest", "sourcePaths",
    "runtimeInstructions", "securityPolicy", "emailPolicy", "executionManifest", "sourceAccess",
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
            if set(answer) & _PROTECTED:
                raise ValueError
            answers.append(redact_data(answer))
        assigned = {str(value) for value in metadata.requirement_ids}
        return {**expected, "clarifications": answers,
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
            )}
    except asyncio.CancelledError:
        raise
    except LLMRuntimeError as error:
        if error.code is LLMErrorCode.BUDGET:
            raise
        raise _SecurityInputError() from None
    except Exception:
        raise _SecurityInputError() from None


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

    def __init__(self, *, provider, context_factory, services_factory, usage_sink=None, telemetry_factory=None):
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

    def __repr__(self):
        return "SecurityAgentExecutor()"

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
            telemetry, usage_sink, call_started_sink = bind_runtime_telemetry(
                self._telemetry_factory, AgentRole.SECURITY, context, execution, self._usage_sink)
            await services.prepare(execution)
            async with services.client_factory(services.configuration) as client:
                tracked = services.tracked(client, execution)
                if telemetry is not None:
                    tracked.set_event_sink(telemetry.tool_event)
                measured = await services.scan(execution, tracked)
                task_input["measuredSecurity"] = measured.analysis_input
                task_input["sourcePaths"] = list(measured.source_paths)
                tools = tuple(tool for tool in tracked.list_tools()
                              if tool.name in {"read_project_file", "read_security_report"})
                if {tool.name for tool in tools} != {"read_project_file", "read_security_report"}:
                    raise SecurityServicesError()
                engine = LLMEngine(role=AgentRole.SECURITY, provider=self._provider, tools=tools,
                    tool_executor=_SecurityReadExecutor(services, tracked, execution, measured))
                result = await engine.run(prompt=prepare_role_prompt(
                    AgentRole.SECURITY, task_input=task_input, metadata=execution.metadata),
                    model=execution.model,
                    output=build_security_output_contract(execution.metadata.requirement_ids, measured.finding_ids, measured.source_paths),
                    budget=execution.budget, workspace_id=str(execution.configuration.workspace_id),
                    usage_sink=usage_sink, call_started_sink=call_started_sink)
                decision = validate_security_decision(result.data, execution.metadata.requirement_ids, measured.finding_ids, measured.source_paths)
                execution.budget.check()
                if decision.kind == "READY":
                    artifacts = await services.finalize(execution, decision, tracked, measured,
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
        except (SecurityContractError, SecurityServicesError) as error:
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, error.code)
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
