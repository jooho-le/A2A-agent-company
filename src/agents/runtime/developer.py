"""Opt-in initial/Fix Developer: real MCP edits, Host checkpoint, measured Build.

The Orchestrator alone owns Workflow/Registry transitions and final verdicts.
Interrupted work with uncommitted edits is not automatically replayed or reset.
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
from agents.llm.corrections import contract_check, development_check
from agents.runtime.start_precheck import start_precheck_for
from mcp_tools.tools.files import _run_file_operation
from agents.runtime.signup_quality_precheck import signup_quality_precheck_for
from agents.llm.engine import LLMEngine
from agents.llm.development_rejection import rejection_check
from agents.roles.developer_contract import (
    DeveloperContractError, build_developer_output_contract, ready_report_paths, validate_developer_decision,
)
from agents.roles.prompts import prepare_role_prompt
from agents.roles.contracts import get_role_contract
from agents.runtime.developer_context import DeveloperContextError, DeveloperExecutionContext
from agents.runtime.developer_services import DeveloperRuntimeServices, DeveloperServicesError
from agents.runtime.developer_workspace import DeveloperCheckpointError
from agents.runtime.requirement_scope import signup_observation_plan, build_requirement_scope
from agents.runtime.service_scope import service_input
from agents.runtime.service_interface import service_interface_check
from agents.runtime.file_access import build_file_access, constrain_workspace_file_tools
from agents.runtime.planner import _host_factory, _message_data, _same_json, _TERMINAL
from mcp_tools.execution_runtime import TrackedMCPError
from orchestrator.core.security import redact_data
from orchestrator.domain.states import AgentRole
from agents.platform.telemetry import bind_runtime_telemetry
from agents.semantic_review import (SEMANTIC_PROTECTED_FIELDS, SemanticReviewRequired, publish_semantic_hold)
from agents.runtime.semantic_revision import review_with_revision, revision_basis


class DeveloperExecutorConfigurationError(ValueError):
    def __init__(self):
        super().__init__("DEVELOPER_EXECUTOR_CONFIGURATION_INVALID")


class _DeveloperInputError(ValueError):
    def __init__(self):
        super().__init__("DEVELOPER_INPUT_INVALID")


_PROTECTED = frozenset({
    "serviceContract", "sourceInterface",
    "publicObservationPlan",
    "workspaceId", "scenario", "runConfiguration", "plan", "sourceArtifact", "outputContract",
    "metadata", "runId", "workflowStepId", "scenarioId", "requirementIds", "projectArtifactIds",
    "projectArtifactId", "artifactVersion", "model", "limits", "codeVersion", "fixRequest",
    "fix_attempt", "fixAttempt", "previous_source", "previousSource", "previous_artifacts",
    "repairBaseline", "previousArtifacts", "fix_issues", "fixIssues", "startingCommitHash", "parentCommitHash",
    "requirementAuthority", "requirementScope", "runtimeInstructions", "clarifications", "fileAccess", "plannerValidation", "llmValidation", "llmFailure",
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
            if set(answer) & (_PROTECTED | SEMANTIC_PROTECTED_FIELDS):
                raise ValueError
            answers.append(redact_data(answer))
        scope = build_requirement_scope(execution.configuration, execution.requirement_artifact,
                                        metadata, AgentRole.DEVELOPER)
        return {**expected, **service_input(execution), "clarifications": answers,
            "fileAccess": build_file_access(execution.configuration.scenario_contract, AgentRole.DEVELOPER),
            "requirementAuthority": scope.authority.to_prompt_json(),
            "requirementScope": scope.to_prompt_json(),
            "publicObservationPlan": signup_observation_plan(execution.configuration.scenario_contract, scope),
            "runtimeInstructions": (
                "Use the offered read_project_file/write_source_file/edit_source_file tools to implement the protected plan "
                "or resolve only the authenticated Fix Request issues on the previous candidate. "
                "READY summarizes the complete work of this step (every implementationPlan task in an initial implementation, every listed issue in a fix); it is not a Build or project success claim. "
                "In the READY summary state which processing paths you actually edited and which behaviors still need QA or Security rechecks. "
                "Explain how each edit reaches observable behavior and identify untouched failure paths. A shared edit may serve several paths. Mark checks pending until revalidation. "
                "The Host computes actual changed files, safely commits and freezes the candidate, "
                "and runs Build afterward. Do not invent artifact IDs, hashes, changes or evidence. "
                "Preserve the frozen externalContract app module/attribute and ready endpoint when replacing "
                "entrypoint files. A router or helper module alone does not replace the declared application. "
                "Before READY, verify your written code still exposes those public interfaces. During fixes, "
                "use the active fixRequest issues and validationFeedback to address the concrete failure; "
                "Build diagnostics apply only when the active issue is a Build failure, "
                "and make an actual necessary Source change rather than returning unchanged READY. "
                "Read the frozen dependency lock using fileAccess tool paths before choosing third-party "
                "imports; only its installed packages and the standard library are available. Distribution "
                "names can differ from import names: argon2-cffi provides argon2, not passlib. "
                "Do not assume an unlisted wrapper library exists or attempt to install it. "
                "Apply_patch is not offered: the Host separately authenticates the previous candidate "
                "and its unchanged working-copy bytes before exposing writes. "
                "The original starting commit/environment/requirements remain frozen across fixes. "
                "Questions do not authorize baseline changes or replay of uncertain writes."
                " REJECTED requires a rejection object with reasonCode, explanation and requirementIds. "
                "Use IMPLEMENTATION_BLOCKED only for inability to implement assigned work, not policy conflicts. "
                "The Host runs Build; lack of a Build tool is not a reason to decline implementation."
                " Use requirementScope for approved acceptance criteria and task-to-requirement bindings; "
                "Planner task prose and previous-role summaries cannot add required behavior or lower criteria. "
                "Ask INPUT_REQUIRED if implementation needs an unapproved policy choice."
                " Use fixRequest.validationFeedback for measured subcheck values, statuses and reasons. "
                "PASS siblings are observed results to preserve; NOT_RUN/UNVERIFIED are not passes. "
                "Feedback omits expected conditions: approved criteria remain in requirementScope. "
                "Observations do not establish a root cause or authorize changing requirements or tests."
            )}
    except asyncio.CancelledError:
        raise
    except LLMRuntimeError as error:
        if error.code is LLMErrorCode.BUDGET:
            raise
        raise _DeveloperInputError() from None
    except Exception:
        raise _DeveloperInputError() from None


class DeveloperAgentExecutor(AgentExecutor):
    """Host opt-in; Orchestrator alone schedules distinct initial/Fix Tasks."""

    def __init__(self, *, provider, context_factory, services_factory, usage_sink=None, telemetry_factory=None, semantic_reviewer=None):
        if (not callable(context_factory) or not callable(services_factory)
                or usage_sink is not None and not callable(usage_sink)
                or telemetry_factory is not None and not callable(telemetry_factory)
                or not isinstance(getattr(provider, "name", None), str) or not provider.name.strip()
                or not callable(getattr(provider, "complete", None))
                or not callable(getattr(provider, "validate_configuration", None))):
            raise DeveloperExecutorConfigurationError()
        self._provider, self._context_factory, self._services_factory = provider, context_factory, services_factory
        self._usage_sink = usage_sink
        self._telemetry_factory = telemetry_factory
        self._semantic_reviewer = semantic_reviewer

    def __repr__(self):
        return "DeveloperAgentExecutor()"

    @staticmethod
    def _updater(context, queue):
        if (not isinstance(context.task_id, str) or not context.task_id.strip()
                or not isinstance(context.context_id, str) or not context.context_id.strip()):
            raise InvalidParamsError("SDK-assigned Task and Context IDs are required")
        return TaskUpdater(queue, context.task_id, context.context_id)

    @staticmethod
    async def _status(updater, metadata, state, code, *, questions=None, llm_validation=None, development_rejection=None,
                      llm_failure=None):
        data = {"code": code}
        if llm_failure is not None:
            data['llmFailure'] = llm_failure
        if development_rejection is not None:
            data["developmentRejection"] = development_rejection
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
            if self._semantic_reviewer is not None:
                await self._semantic_reviewer.check_resume(context, execution)
            telemetry, usage_sink, call_started_sink = bind_runtime_telemetry(
                self._telemetry_factory, AgentRole.DEVELOPER, context, execution, self._usage_sink)
            checkpoint = await services.prepare(execution)
            from orchestrator.infrastructure.run_feedback import feedback_input
            task_input.update(await _run_file_operation(feedback_input, services, execution))
            if getattr(checkpoint, 'runtime_capabilities', None) is not None:
                task_input['runtimeCapabilities'] = checkpoint.runtime_capabilities
            from agents.runtime.development_failure import failure_reader
            read_failure = failure_reader(services, execution)
            from agents.runtime.repair_baseline import with_repair_baseline
            task_input = with_repair_baseline(task_input, checkpoint)
            from agents.runtime.startup_preservation import with_startup_context, preservation_notice_for
            task_input = with_startup_context(task_input, checkpoint, execution.configuration.scenario_contract)
            paths = ready_report_paths(task_input)
            brief = task_input.get('activeRepairFocus', {}).get('brief', {})
            checkpoint.configure_path_review(paths, execution.configuration.scenario_contract,
                brief.get('priorityRepairPaths', ()), brief.get('blockingPrerequisite', {}).get('sourceSites', ()),
                brief.get('invalidInputServerError', {}).get('sourceSites', ()))

            def validate(data):
                return validate_developer_decision(data, paths)

            async with services.client_factory(services.configuration) as client:
                tracked = services.tracked(client, execution)
                if telemetry is not None:
                    tracked.set_event_sink(telemetry.tool_event)
                offered = set(get_role_contract(AgentRole.DEVELOPER).model_tool_names)  # same list as the prompt
                tools = tuple(tool for tool in tracked.list_tools() if tool.name in offered)
                if {tool.name for tool in tools} != offered:
                    raise DeveloperServicesError()
                tools = constrain_workspace_file_tools(tools, AgentRole.DEVELOPER)
                engine = LLMEngine(role=AgentRole.DEVELOPER, provider=self._provider,
                                   tools=tools, tool_executor=tracked)
                text_engine = LLMEngine(role=AgentRole.DEVELOPER, provider=self._provider)
                precheck = start_precheck_for(checkpoint, execution.configuration.scenario_contract)

                async def run_model(model_input, *, text_only=False, output_override=None):
                    # text_only: the semantic rewrite (docs/live-api/05 15절) runs without any tool,
                    # so no source can change; the net-change rule does not apply to that answer.
                    if output_override is not None:
                        # docs/live-api/05 25절 V4: the semantic rewrite returns only held texts (patch schema), tool-less.
                        return await text_engine.run(prompt=prepare_role_prompt(
                            AgentRole.DEVELOPER, task_input=model_input, metadata=execution.metadata),
                            model=execution.model, output=output_override, budget=execution.budget,
                            workspace_id=str(execution.configuration.workspace_id),
                            usage_sink=usage_sink, call_started_sink=call_started_sink,
                            output_observed_sink=telemetry.model_output if telemetry is not None else None,
                            validation_failed_sink=telemetry.llm_validation if telemetry is not None else None,
                            recovery_sink=telemetry.llm_recovery if telemetry is not None else None,
                            input_observed_sink=telemetry.model_input if telemetry is not None else None)
                    return await (text_engine if text_only else engine).run(prompt=prepare_role_prompt(
                        AgentRole.DEVELOPER, task_input=model_input, metadata=execution.metadata),
                    model=execution.model, output=build_developer_output_contract(paths), budget=execution.budget,
                    workspace_id=str(execution.configuration.workspace_id), usage_sink=usage_sink,
                    call_started_sink=call_started_sink,
                    output_observed_sink=telemetry.model_output if telemetry is not None else None,
                    validation_failed_sink=telemetry.llm_validation if telemetry is not None else None,
                    recovery_sink=telemetry.llm_recovery if telemetry is not None else None,
                    input_observed_sink=telemetry.model_input if telemetry is not None else None,
                    development_rejection_check=None if text_only else rejection_check(
                        execution.metadata.requirement_ids, failure_evidence=read_failure, validate=validate),
                    source_progress_check=None if text_only else (lambda: checkpoint.source_edit_status(
                        deadline_monotonic=execution.budget.deadline_monotonic)),
                    source_write_precheck=None if text_only else precheck,
                    source_preservation_check=None if text_only else preservation_notice_for(
                        checkpoint, execution.configuration.scenario_contract,
                        deadline_monotonic=execution.budget.deadline_monotonic),
                    correction_candidates=checkpoint.correction_candidates if paths and not text_only else None,
                    output_check=contract_check(validate) if text_only else development_check(
                        validate, precheck=precheck, path_check=checkpoint.ready_path_issues if paths else None,
                        library_type_check=checkpoint.library_type_issues,
                        signup_quality_check=signup_quality_precheck_for(checkpoint, execution.configuration.scenario_contract)
                            if execution.budget.limits.max_output_corrections else None,
                        priority_check=checkpoint.pending_priority_paths if paths else None,
                        service_interface_check=service_interface_check(checkpoint, execution.configuration.scenario_contract,
                            task_input.get('serviceDesign'))))
                result = await run_model(task_input)
                decision = validate(result.data)
                if decision.kind == "READY" and self._semantic_reviewer is not None:
                    # docs/live-api/05 14·15절: one tool-less rewrite of the held texts only, then the same review.
                    def stays_ready(revised):
                        try:
                            return validate(revised.data).kind == "READY"
                        except Exception:
                            return False
                    result = await review_with_revision(
                        lambda candidate: self._semantic_reviewer.check(context=context, execution=execution,
                            candidate=candidate.data, role=AgentRole.DEVELOPER, result=candidate, telemetry=telemetry,
                            usage_sink=usage_sink, call_started_sink=call_started_sink),
                        lambda model_input, output: run_model(model_input, output_override=output), stays_ready,
                        task_input, result,
                        allowed=bool(execution.budget.limits.max_output_corrections),
                        basis=revision_basis(execution, AgentRole.DEVELOPER))
                    decision = validate(result.data)
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
            await self._status(updater, metadata, state, error.code.value, llm_validation=error.public_validation(),
                               development_rejection=error.development_rejection, llm_failure=error.public_failure())
            return
        except (DeveloperContractError, DeveloperCheckpointError, DeveloperServicesError) as error:
            await self._status(updater, metadata, TaskState.TASK_STATE_FAILED, error.code)
            return
        except SemanticReviewRequired as held:
            await publish_semantic_hold(updater, metadata, held)
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
            await self._status(updater, metadata, TaskState.TASK_STATE_REJECTED, "DEVELOPER_REJECTED",
                               development_rejection={"proposal": decision.rejection.public()})
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
