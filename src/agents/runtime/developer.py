"""Opt-in initial Developer: real MCP edits, Host checkpoint, measured Build.

The Orchestrator alone owns Workflow/Registry transitions and final verdicts.
Interrupted work with uncommitted edits is not automatically replayed or reset.
"""

import asyncio
import inspect

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
from agents.roles.developer_contract import (
    DeveloperContractError, build_developer_output_contract, validate_developer_decision,
)
from agents.roles.prompts import prepare_role_prompt
from agents.runtime.developer_context import DeveloperContextError, DeveloperExecutionContext
from agents.runtime.developer_services import DeveloperRuntimeServices, DeveloperServicesError
from agents.runtime.developer_workspace import DeveloperCheckpointError
from agents.runtime.planner import _message_data, _same_json, _TERMINAL
from mcp_tools.execution_runtime import TrackedMCPError
from orchestrator.core.security import redact_data
from orchestrator.domain.states import AgentRole


class DeveloperExecutorConfigurationError(ValueError):
    def __init__(self):
        super().__init__("DEVELOPER_EXECUTOR_CONFIGURATION_INVALID")


class _DeveloperInputError(ValueError):
    def __init__(self):
        super().__init__("DEVELOPER_INPUT_INVALID")


_PROTECTED = frozenset({
    "workspaceId", "scenario", "runConfiguration", "plan", "sourceArtifact", "outputContract",
    "metadata", "runId", "workflowStepId", "scenarioId", "requirementIds", "projectArtifactIds",
    "projectArtifactId", "artifactVersion", "model", "limits", "codeVersion", "fixRequest",
})


def _prepare_input(context, execution):
    try:
        task, message = context.current_task, context.message
        metadata = parse_workflow_metadata(context.metadata)
        if (type(execution) is not DeveloperExecutionContext or metadata != execution.metadata
                or task is None or task.id != context.task_id or task.context_id != context.context_id
                or task.status.state != TaskState.TASK_STATE_SUBMITTED or task.artifacts
                or parse_workflow_metadata(MessageToDict(task.metadata)) != metadata or message is None):
            raise ValueError
        execution.budget.check()
        maximum = execution.budget.limits.max_json_bytes
        json_text(MessageToDict(task), max_bytes=maximum)
        inputs = [item for item in task.history if item.role == Role.ROLE_USER]
        if (not inputs or len(inputs) > 32 or len({item.message_id for item in inputs}) != len(inputs)
                or inputs[-1].message_id != message.message_id
                or len(message.parts) != 1 or len(inputs[-1].parts) != 1
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
        return {**expected, "clarifications": answers,
            "runtimeInstructions": (
                "Use the offered read_project_file/write_source_file tools to implement the protected plan. "
                "READY is only a bounded work summary, not a Build or project success claim. "
                "The Host computes actual changed files, safely commits and freezes the candidate, "
                "and runs Build afterward. Do not invent artifact IDs, hashes, changes or evidence. "
                "Initial codeVersion=1 has no frozen patch base; apply_patch is not offered. "
                "Questions do not authorize baseline changes or replay of uncertain writes."
            )}
    except asyncio.CancelledError:
        raise
    except LLMRuntimeError as error:
        if error.code is LLMErrorCode.BUDGET:
            raise
        raise _DeveloperInputError() from None
    except Exception:
        raise _DeveloperInputError() from None


async def _host_factory(factory, argument):
    result = factory(argument) if inspect.iscoroutinefunction(factory) else await asyncio.to_thread(factory, argument)
    return await result if inspect.isawaitable(result) else result


class DeveloperAgentExecutor(AgentExecutor):
    """Host opt-in; no env auto-wiring or automatic Fix cycle in step 31."""

    def __init__(self, *, provider, context_factory, services_factory, usage_sink=None):
        if (not callable(context_factory) or not callable(services_factory)
                or usage_sink is not None and not callable(usage_sink)
                or not isinstance(getattr(provider, "name", None), str) or not provider.name.strip()
                or not callable(getattr(provider, "complete", None))
                or not callable(getattr(provider, "validate_configuration", None))):
            raise DeveloperExecutorConfigurationError()
        self._provider, self._context_factory, self._services_factory = provider, context_factory, services_factory
        self._usage_sink = usage_sink

    def __repr__(self):
        return "DeveloperAgentExecutor()"

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
            if type(execution) is not DeveloperExecutionContext:
                raise DeveloperContextError()
        except asyncio.CancelledError:
            raise
        except LLMRuntimeError as error:
            await self._status(updater, metadata,
                TaskState.TASK_STATE_FAILED if error.code is LLMErrorCode.BUDGET else TaskState.TASK_STATE_REJECTED,
                error.code.value if error.code is LLMErrorCode.BUDGET else "DEVELOPER_CONTEXT_DENIED")
            return
        except Exception:
            await self._status(updater, metadata, TaskState.TASK_STATE_REJECTED, "DEVELOPER_CONTEXT_DENIED")
            return
        try:
            task_input = _prepare_input(context, execution)
        except _DeveloperInputError:
            await self._status(updater, metadata, TaskState.TASK_STATE_REJECTED, "DEVELOPER_INPUT_INVALID")
            return
        except LLMRuntimeError as error:
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, error.code.value)
            return
        try:
            services = await _host_factory(self._services_factory, execution)
            if type(services) is not DeveloperRuntimeServices:
                raise DeveloperServicesError()
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._status(updater, metadata, TaskState.TASK_STATE_REJECTED, "DEVELOPER_SERVICES_DENIED")
            return

        await updater.update_status(TaskState.TASK_STATE_WORKING, metadata=metadata)
        try:
            checkpoint = await services.prepare(execution)
            async with services.client_factory(services.configuration) as client:
                tracked = services.tracked(client, execution)
                tools = tuple(tool for tool in tracked.list_tools()
                              if tool.name in {"read_project_file", "write_source_file"})
                if {tool.name for tool in tools} != {"read_project_file", "write_source_file"}:
                    raise DeveloperServicesError()
                engine = LLMEngine(role=AgentRole.DEVELOPER, provider=self._provider,
                                   tools=tools, tool_executor=tracked)
                result = await engine.run(prompt=prepare_role_prompt(
                    AgentRole.DEVELOPER, task_input=task_input, metadata=execution.metadata),
                    model=execution.model, output=build_developer_output_contract(), budget=execution.budget,
                    workspace_id=str(execution.configuration.workspace_id), usage_sink=self._usage_sink)
                decision = validate_developer_decision(result.data)
                execution.budget.check()
                if decision.kind == "READY":
                    artifacts = await services.finalize(execution, checkpoint, decision, tracked,
                        task_id=context.task_id, context_id=context.context_id)
            # The SDK may resume an interrupted Task immediately. Finish MCP
            # teardown before publishing that state so an old producer cannot
            # close the shared queue while the resumed producer is using it.
            execution.budget.check()
        except asyncio.CancelledError:
            raise
        except LLMRuntimeError as error:
            state = (TaskState.TASK_STATE_AUTH_REQUIRED if error.code is LLMErrorCode.AUTH else
                     TaskState.TASK_STATE_REJECTED if error.code is LLMErrorCode.REFUSAL else TaskState.TASK_STATE_FAILED)
            await self._status(updater, metadata, state, error.code.value)
            return
        except (DeveloperContractError, DeveloperCheckpointError, DeveloperServicesError) as error:
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, error.code)
            return
        except TrackedMCPError:
            # The real private journal retains uncertainty. Do not fabricate an
            # infrastructure Build Report with made-up manifest/exit values.
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, "DEVELOPER_BUILD_UNVERIFIED")
            return
        except Exception:
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, "DEVELOPER_EXECUTION_FAILED")
            return
        if decision.kind == "INPUT_REQUIRED":
            await self._status(updater, metadata, TaskState.TASK_STATE_INPUT_REQUIRED,
                               "DEVELOPER_INPUT_REQUIRED", questions=decision.questions)
            return
        if decision.kind == "REJECTED":
            await self._status(updater, metadata, TaskState.TASK_STATE_REJECTED, "DEVELOPER_OUT_OF_SCOPE")
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
