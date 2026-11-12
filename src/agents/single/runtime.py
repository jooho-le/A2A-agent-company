"""One provider, one persisted A2A Context, sequential Host-selected phases.

Role-scoped services below are trusted receipt/Source validators, not Agent
executors. No role prompt, second model identity or delegated Agent is used.
"""

import asyncio
from dataclasses import replace

from a2a.helpers import new_data_part
from a2a.server.agent_execution import AgentExecutor
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict

from agents.api.sqlite_task_store import SQLiteAgentTaskStore
from agents.api.validation import parse_workflow_metadata
from agents.core.identity import SingleAgentIdentity, SinglePhase
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, ToolCall, ToolContext, parse_json
from agents.llm.engine import LLMEngine
from agents.runtime.planner import _host_factory, _TERMINAL, _prepare_input as planner_input
from agents.runtime.developer import _prepare_input as developer_input
from agents.runtime.qa import _prepare_input as qa_input, _test_targets
from agents.runtime.security import _prepare_input as security_input
from agents.runtime.planner_context import SQLitePlannerContextLoader
from agents.runtime.developer_context import SQLiteDeveloperContextLoader
from agents.runtime.qa_context import SQLiteQAContextLoader
from agents.runtime.security_context import SQLiteSecurityContextLoader
from agents.single.contracts import build_single_draft_contract
from agents.single.mcp import open_single_model_client
from agents.single.prompts import prepare_single_prompt
from agents.platform.telemetry import bind_runtime_telemetry
from mcp_tools.execution_runtime import TrackedMCPError
from orchestrator.domain.states import AgentRole


_PHASE = {AgentRole.PLANNER: SinglePhase.PLANNING, AgentRole.DEVELOPER: SinglePhase.IMPLEMENTING,
          AgentRole.QA: SinglePhase.QA, AgentRole.SECURITY: SinglePhase.SECURITY}
_INPUT = {AgentRole.PLANNER: planner_input, AgentRole.DEVELOPER: developer_input,
          AgentRole.QA: qa_input, AgentRole.SECURITY: security_input}


class SingleExecutionError(ValueError):
    def __init__(self):
        super().__init__("SINGLE_AGENT_EXECUTION_DENIED")


class _SingleTools:
    def __init__(self, tracked, execution, role, *, services=None, measured=None):
        self.tracked, self.execution, self.role = tracked, execution, role
        self.services, self.measured = services, measured
        self._seen = set()

    async def execute(self, call, arguments, context):
        authority = self.tracked._client.principal_configuration
        if (not isinstance(call, ToolCall) or not isinstance(context, ToolContext)
                or context.role is not SingleAgentIdentity.SINGLE_AGENT
                or authority is None or authority.binding.role is not SingleAgentIdentity.SINGLE_AGENT
                or context.workspace_id != str(authority.binding.workspace_id)
                or context.deadline_monotonic != self.execution.budget.deadline_monotonic
                or parse_json(call.arguments_json) != arguments
                or not isinstance(call.call_id, str) or not 1 <= len(call.call_id.encode()) <= 4096
                or call.call_id in self._seen or len(self._seen) >= 1000):
            raise TrackedMCPError("MCP_EXECUTION_CONTEXT_DENIED")
        self._seen.add(call.call_id)
        if self.role is AgentRole.SECURITY:
            result = await self.services.invoke(self.tracked, self.execution, call.name, arguments, self.measured)
            return result.data
        # Only private receipt bookkeeping projects the phase into its old ACL.
        # The actual child/process/principal and journal policy hash stay Single.
        return await self.tracked.execute(call, arguments, replace(context, role=self.role))


class SingleAgentExecutor(AgentExecutor):
    def __init__(self, *, repository, budgets, provider, services_factories, usage_sink=None, telemetry_factory=None):
        from agents.platform.budgets import RunBudgetRegistry
        from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
        if (type(repository) is not SQLiteWorkflowRepository or type(budgets) is not RunBudgetRegistry
                or set(services_factories) != {AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY}
                or any(not callable(value) for value in services_factories.values())
                or usage_sink is not None and not callable(usage_sink)
                or telemetry_factory is not None and not callable(telemetry_factory)
                or not callable(getattr(provider, "complete", None))):
            raise SingleExecutionError()
        self._repository, self._provider = repository, provider
        self._services = dict(services_factories)
        self._loaders = {role: loader(repository, budgets.resolve) for role, loader in (
            (AgentRole.PLANNER, SQLitePlannerContextLoader), (AgentRole.DEVELOPER, SQLiteDeveloperContextLoader),
            (AgentRole.QA, SQLiteQAContextLoader), (AgentRole.SECURITY, SQLiteSecurityContextLoader))}
        self._store, self._usage_sink = None, usage_sink
        self._telemetry_factory = telemetry_factory

    def bind_task_store(self, store):
        if self._store is not None or type(store) is not SQLiteAgentTaskStore or store.role is not SingleAgentIdentity.SINGLE_AGENT:
            raise SingleExecutionError()
        self._store = store

    def _load(self, context):
        metadata = parse_workflow_metadata(context.metadata)
        config = self._repository.get_run_configuration(metadata.run_id)
        run = self._repository.get_run(metadata.run_id)
        step = next((item for item in self._repository.list_steps(metadata.run_id)
                     if item.workflow_step_id == metadata.workflow_step_id), None)
        if (self._store is None or config is None or config.configuration.architecture != "SINGLE_AGENT"
                or run is None or not 0 <= run.fix_attempt <= 3 or step is None or step.agent_role not in _PHASE):
            raise SingleExecutionError()
        return step.agent_role, self._loaders[step.agent_role](context)

    @staticmethod
    async def _status(updater, metadata, state, code, questions=None, unapproved_assumptions=()):
        data = {"code": code}
        if questions is not None:
            data["questions"] = list(questions)
        if unapproved_assumptions:
            data["unapprovedAssumptions"] = list(unapproved_assumptions)
            data["assumptionApprovalStatus"] = "UNAPPROVED"
        await updater.update_status(state, metadata=metadata,
            message=updater.new_agent_message(parts=[new_data_part(data, media_type="application/json")]))

    async def _model(self, context, execution, task_input, phase, contract, *, tools=(), executor=None, telemetry=None):
        prompt = await prepare_single_prompt(self._store, context_id=context.context_id,
            metadata=execution.metadata, scenario=execution.scenario, draft_contract=contract,
            task_input=task_input, call_context=context.call_context,
            max_bytes=min(execution.budget.limits.max_json_bytes, 1_048_576))
        engine = LLMEngine(role=SingleAgentIdentity.SINGLE_AGENT, single_phase=phase,
                           provider=self._provider, tools=tools, tool_executor=executor)
        result = await engine.run(prompt=prompt, model=execution.model, output=contract.output,
            budget=execution.budget, workspace_id=str(execution.configuration.workspace_id),
            usage_sink=telemetry.usage if telemetry is not None else self._usage_sink,
            call_started_sink=telemetry.model_started if telemetry is not None else None)
        return contract.validate(result.data)

    async def _work(self, context, role, execution, task_input):
        phase = (SinglePhase.FIXING if role is AgentRole.DEVELOPER and execution.fix_attempt
                 else _PHASE[role])
        telemetry, _usage, _started = bind_runtime_telemetry(self._telemetry_factory,
            SingleAgentIdentity.SINGLE_AGENT, context, execution, self._usage_sink)
        if role is AgentRole.PLANNER:
            contract = build_single_draft_contract(phase, scenario=execution.scenario)
            return await self._model(context, execution, task_input, phase, contract, telemetry=telemetry), ()
        services = await _host_factory(self._services[role], execution)
        checkpoint = await services.prepare(execution)
        async with services.client_factory(services.configuration) as host:
            host_tracked = services.tracked(host, execution)
            if telemetry is not None:
                host_tracked.set_event_sink(telemetry.tool_event)
            options, measured = {}, None
            if role is AgentRole.DEVELOPER:
                wanted = {"read_project_file", "write_source_file"}
            elif role is AgentRole.QA:
                wanted = {"read_project_file", "write_test_file", "read_test_report"}
                task_input["testSelectors"] = {name: list(values) for name, values in services.selectors.items()}
                task_input["testTargets"] = _test_targets(services)
                options["selectors"] = services.selectors
            else:
                measured = await services.scan(execution, host_tracked)
                wanted = {"read_project_file", "read_security_report"}
                task_input["measuredSecurity"], task_input["sourcePaths"] = measured.analysis_input, list(measured.source_paths)
                options.update(finding_ids=measured.finding_ids, source_paths=measured.source_paths)
                if services.test_selectors:
                    wanted.add("write_test_file")
                    selectors = {"run_unit_tests": services.test_selectors}
                    options["security_test_selectors"] = selectors
                    task_input["testSelectors"] = {"run_unit_tests": list(services.test_selectors)}
                    task_input["testTargets"] = [{"toolName": "run_unit_tests", "selector": scope.name,
                        "directory": "outputs/security/" + scope.source_directory,
                        "pattern": scope.pattern, "format": "python-unittest"}
                        for scope in services.configuration.unit_test_configuration.scopes if scope.kind == "QA_TESTS"]
                    task_input["runtimeInstructions"] = (
                        "Write and bind generated Security tests for every assigned requirement using testTargets/testSelectors. "
                        "Only outputs/security/tests/ may be written. Return testCases in READY; the Host runs every "
                        "approved scope, including hidden protected cases, and derives outcomes from measured receipts. "
                        "Static scanner ranks or model proposals cannot establish PASS/FAIL. Do not invent evidence.")
            contract = build_single_draft_contract(phase, scenario=execution.scenario, **options)
            async with open_single_model_client(services.configuration, phase, services.client_factory) as client:
                tracked = services.tracked(client, execution)
                if telemetry is not None:
                    tracked.set_event_sink(telemetry.tool_event)
                tools = tuple(tool for tool in tracked.list_tools() if tool.name in wanted)
                if {tool.name for tool in tools} != wanted:
                    raise SingleExecutionError()
                decision = await self._model(context, execution, task_input, phase, contract, tools=tools,
                    executor=_SingleTools(tracked, execution, role, services=services, measured=measured), telemetry=telemetry)
            execution.budget.check()
            artifacts = ()
            if decision.kind == "READY":
                arguments = (execution, checkpoint, decision, host_tracked) if role is AgentRole.DEVELOPER else (
                    (execution, decision, host_tracked, measured) if role is AgentRole.SECURITY else (execution, decision, host_tracked))
                artifacts = await services.finalize(*arguments, task_id=context.task_id, context_id=context.context_id)
        execution.budget.check()
        return decision, artifacts

    async def execute(self, context, event_queue):
        if not context.task_id or not context.context_id:
            raise InvalidParamsError("SDK-assigned Task and Context IDs are required")
        if context.current_task is not None and context.current_task.status.state in _TERMINAL:
            return
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        try:
            role, execution = await _host_factory(self._load, context)
            task_input = _INPUT[role](context, execution)
        except asyncio.CancelledError:
            raise
        except LLMRuntimeError as error:
            await self._status(updater, context.metadata, TaskState.TASK_STATE_FAILED, error.code.value)
            return
        except Exception:
            await self._status(updater, context.metadata, TaskState.TASK_STATE_REJECTED, "SINGLE_AGENT_CONTEXT_DENIED")
            return
        await updater.update_status(TaskState.TASK_STATE_WORKING, metadata=context.metadata)
        try:
            decision, artifacts = await self._work(context, role, execution, task_input)
            execution.budget.check()
        except asyncio.CancelledError:
            raise
        except LLMRuntimeError as error:
            state = TaskState.TASK_STATE_AUTH_REQUIRED if error.code is LLMErrorCode.AUTH else (
                TaskState.TASK_STATE_REJECTED if error.code is LLMErrorCode.REFUSAL else TaskState.TASK_STATE_FAILED)
            await self._status(updater, context.metadata, state, error.code.value)
            return
        except Exception:
            # Private journals preserve uncertain effects; no fabricated PASS or retry.
            await self._status(updater, context.metadata, TaskState.TASK_STATE_FAILED, "SINGLE_AGENT_EXECUTION_FAILED")
            return
        if decision.kind == "INPUT_REQUIRED":
            await self._status(updater, context.metadata, TaskState.TASK_STATE_INPUT_REQUIRED,
                               "SINGLE_AGENT_INPUT_REQUIRED", decision.questions,
                               decision.unapproved_assumptions if role is AgentRole.PLANNER else ())
            return
        if decision.kind == "REJECTED":
            await self._status(updater, context.metadata, TaskState.TASK_STATE_REJECTED, "SINGLE_AGENT_OUT_OF_SCOPE")
            return
        if role is AgentRole.PLANNER:
            await updater.add_artifact([new_data_part(decision.plan.model_dump(mode="json", by_alias=True), media_type="application/json")],
                name="requirements.json", metadata={"runId": str(execution.metadata.run_id),
                    "workflowStepId": str(execution.metadata.workflow_step_id),
                    "projectArtifactId": str(execution.project_artifact_id), "artifactVersion": execution.artifact_version})
        else:
            for artifact in artifacts:
                await updater.add_artifact(list(artifact.parts), artifact_id=artifact.artifact_id,
                                          name=artifact.name, metadata=MessageToDict(artifact.metadata))
        await updater.update_status(TaskState.TASK_STATE_COMPLETED, metadata=context.metadata)

    async def cancel(self, context, event_queue):
        task = context.current_task
        if task is None or task.id != context.task_id or task.context_id != context.context_id:
            raise InvalidParamsError("A stored Task of the same identity is required")
        if task.status.state in _TERMINAL:
            return
        metadata = MessageToDict(task.metadata)
        parse_workflow_metadata(metadata)
        await TaskUpdater(event_queue, context.task_id, context.context_id).update_status(TaskState.TASK_STATE_CANCELED, metadata=metadata)
