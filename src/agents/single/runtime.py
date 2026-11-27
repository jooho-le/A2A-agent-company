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
from agents.llm.development_rejection import rejection_check
from agents.llm.corrections import case_expectation_check, contract_check, development_check
from agents.roles.expected_results import with_expected_results
from agents.runtime.start_precheck import start_precheck_for
from agents.runtime.startup_preservation import preservation_notice_for
from agents.runtime.signup_quality_precheck import signup_quality_precheck_for
from agents.runtime.service_interface import service_interface_check
from agents.llm.engine import LLMEngine
from agents.runtime.file_access import build_file_access, constrain_frozen_read_tools, constrain_workspace_file_tools
from agents.runtime.test_guidance import unit_test_targets
from agents.runtime.test_fix import TestFixRequired, same_cases
from agents.runtime.qa_services import QAServicesError
from agents.runtime.security_services import SecurityServicesError
from mcp_tools.tools.files import _run_file_operation
from agents.runtime.planner import _host_factory, _TERMINAL, _prepare_input as planner_input
from agents.runtime.developer import _prepare_input as developer_input
from agents.runtime.qa import _prepare_input as qa_input, _test_targets
from agents.runtime.security import _prepare_input as security_input, _TEST_INSTRUCTIONS as security_test_instructions
from agents.runtime.planner_context import SQLitePlannerContextLoader
from agents.runtime.developer_context import SQLiteDeveloperContextLoader
from agents.runtime.qa_context import SQLiteQAContextLoader
from agents.runtime.security_context import SQLiteSecurityContextLoader
from agents.runtime.requirement_scope import (
    RequirementScopeError, assigned_acceptance_criteria, generated_case_expectation_issues,
    validate_generated_case_expectations,
)
from agents.single.contracts import SingleContractError, build_single_draft_contract
from agents.single.mcp import open_single_model_client
from agents.single.prompts import SinglePromptLimitError, prepare_single_prompt
from agents.platform.telemetry import bind_runtime_telemetry
from agents.semantic_review import SemanticReviewRequired, publish_semantic_hold
from agents.runtime.semantic_revision import review_with_revision, revision_basis
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
    def __init__(self, *, repository, budgets, provider, services_factories, usage_sink=None, telemetry_factory=None, semantic_reviewer=None):
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
        self._semantic_reviewer = semantic_reviewer

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
    async def _status(updater, metadata, state, code, questions=None, unapproved_assumptions=(), planner_validation=None,
                      llm_validation=None, development_rejection=None, llm_failure=None):
        data = {"code": code}
        if llm_failure is not None:
            data['llmFailure'] = llm_failure
        if development_rejection is not None:
            data["developmentRejection"] = development_rejection
        if llm_validation is not None:
            data["llmValidation"] = llm_validation
        if planner_validation is not None:
            data["plannerValidation"] = planner_validation
        if questions is not None:
            data["questions"] = list(questions)
        if unapproved_assumptions:
            data["unapprovedAssumptions"] = list(unapproved_assumptions)
            data["assumptionApprovalStatus"] = "UNAPPROVED"
        await updater.update_status(state, metadata=metadata,
            message=updater.new_agent_message(parts=[new_data_part(data, media_type="application/json")]))

    async def _model(self, context, execution, task_input, phase, contract, *, tools=(), executor=None, telemetry=None,
                     precheck=None, source_progress_check=None, expectations=None, explain=None, extra_checks=(),
                     output_refresh=None, text_only=False, failure_evidence=None, path_check=None, priority_check=None,
                     library_type_check=None, correction_candidates=None, output_safety_check=None,
                     signup_quality_check=None, source_preservation_check=None, service_ui_check=None):
        # text_only: the semantic rewrite (docs/live-api/05 15절) — no tool, no forced edit, no catalog stop.
        if text_only and (tools or executor is not None or source_progress_check is not None):
            raise SingleExecutionError()
        if phase is SinglePhase.QA:
            from agents.runtime.test_fix import available_report_tools
            tools = available_report_tools(tools, task_input)
        prompt = await prepare_single_prompt(self._store, context_id=context.context_id,
            metadata=execution.metadata, scenario=execution.scenario, draft_contract=contract,
            task_input=task_input, call_context=context.call_context,
            max_bytes=min(execution.budget.limits.max_json_bytes, 1_048_576))
        engine = LLMEngine(role=SingleAgentIdentity.SINGLE_AGENT, single_phase=phase,
                           provider=self._provider, tools=tools, tool_executor=executor)
        result = await engine.run(prompt=prompt, model=execution.model, output=contract.output,
            budget=execution.budget, workspace_id=str(execution.configuration.workspace_id),
            usage_sink=telemetry.usage if telemetry is not None else self._usage_sink,
            call_started_sink=telemetry.model_started if telemetry is not None else None,
            output_observed_sink=telemetry.model_output if telemetry is not None else None,
            validation_failed_sink=telemetry.llm_validation if telemetry is not None else None,
            recovery_sink=telemetry.llm_recovery if telemetry is not None else None,
            input_observed_sink=telemetry.model_input if telemetry is not None else None,
            development_rejection_check=(rejection_check(execution.metadata.requirement_ids, failure_evidence=failure_evidence, validate=contract.validate)
                if not text_only and phase in (SinglePhase.IMPLEMENTING, SinglePhase.FIXING) else None),
            source_progress_check=source_progress_check,
            source_write_precheck=precheck if not text_only and phase in (SinglePhase.IMPLEMENTING, SinglePhase.FIXING) else None,
            source_preservation_check=source_preservation_check if not text_only else None,
            output_refresh=output_refresh,
            correction_candidates=correction_candidates,
            close_invalid_qa_draft=not text_only and (
                phase is SinglePhase.QA or (phase is SinglePhase.SECURITY and output_refresh is not None)),
            output_check=(contract_check(contract.validate) if text_only and phase in (
                              SinglePhase.IMPLEMENTING, SinglePhase.FIXING)
                          else development_check(contract.validate, precheck=precheck, path_check=path_check,
                                                 allow_unchanged_source=execution.scenario.key == 'SERVICE-WEB',
                                                 priority_check=priority_check, library_type_check=library_type_check,
                                                 output_safety_check=output_safety_check,
                                                 signup_quality_check=signup_quality_check, service_interface_check=service_ui_check)
                          if phase in (SinglePhase.IMPLEMENTING, SinglePhase.FIXING)
                          else case_expectation_check(contract.validate, expectations, explain=explain,
                                                      extra_checks=extra_checks)))
        try:
            decision = contract.validate(result.data)
        except SingleContractError as error:
            if error.planner_validation is not None and telemetry is not None:
                error.trace_recorded = telemetry.planner_validation(error.planner_validation)
            raise
        return decision, result, prompt

    async def _review(self, context, role, execution, phase, decision, result, telemetry, prompt):
        if self._semantic_reviewer is not None and decision.kind in {'PLAN', 'READY'}:
            await self._semantic_reviewer.check(context=context, execution=execution, candidate=result.data,
                role=role, actor=SingleAgentIdentity.SINGLE_AGENT, phase=phase, result=result,
                telemetry=telemetry, usage_sink=telemetry.usage if telemetry is not None else self._usage_sink,
                call_started_sink=telemetry.model_started if telemetry is not None else None, single_prompt=prompt)

    async def _reviewed(self, context, role, execution, phase, telemetry, task_input, contract, first,
                        expectations=None):
        """docs/live-api/05 14·15절: same rule as Multi — one tool-less rewrite of the held texts only."""
        decision, result, prompt = first
        if self._semantic_reviewer is None or decision.kind not in {'PLAN', 'READY'}:
            return first
        latest = {id(result): first}

        async def check(candidate):
            d, r, p = latest[id(candidate)]
            await self._review(context, role, execution, phase, d, r, telemetry, p)

        async def rerun_text(model_input, output):
            # docs/live-api/05 25절 V4: tool-less, patch schema only; the Host applies the held texts.
            # The Single prompt binds its draft contract's output; the rewrite uses the same phase and
            # scenario with only the patch output (the patched answer is validated by `contract` below).
            from dataclasses import replace as _replace
            patch_contract = _replace(contract, output=output, _validate=lambda data: data)
            prompt = await prepare_single_prompt(self._store, context_id=context.context_id,
                metadata=execution.metadata, scenario=execution.scenario, draft_contract=patch_contract,
                task_input=model_input, call_context=context.call_context,
                max_bytes=min(execution.budget.limits.max_json_bytes, 1_048_576))
            engine = LLMEngine(role=SingleAgentIdentity.SINGLE_AGENT, single_phase=phase, provider=self._provider)
            return await engine.run(prompt=prompt, model=execution.model, output=output,
                budget=execution.budget, workspace_id=str(execution.configuration.workspace_id),
                usage_sink=telemetry.usage if telemetry is not None else self._usage_sink,
                call_started_sink=telemetry.model_started if telemetry is not None else None,
                output_observed_sink=telemetry.model_output if telemetry is not None else None,
                validation_failed_sink=telemetry.llm_validation if telemetry is not None else None,
                recovery_sink=telemetry.llm_recovery if telemetry is not None else None,
                input_observed_sink=telemetry.model_input if telemetry is not None else None)

        def same_kind(candidate):
            if id(candidate) not in latest:
                # The Host-patched answer: validate it with the phase contract and review it in the
                # first candidate's trusted Single context.
                try:
                    latest[id(candidate)] = (contract.validate(candidate.data), candidate, first[2])
                except Exception:
                    return False
            return latest[id(candidate)][0].kind == decision.kind
        reviewed = await review_with_revision(check, rerun_text, same_kind, task_input, result,
                                              allowed=bool(execution.budget.limits.max_output_corrections),
                                              basis=revision_basis(execution, role))
        return latest[id(reviewed)]

    async def _work(self, context, role, execution, task_input):
        if self._semantic_reviewer is not None:
            await self._semantic_reviewer.check_resume(context, execution)
        phase = (SinglePhase.FIXING if role is AgentRole.DEVELOPER and execution.fix_attempt
                 else _PHASE[role])
        telemetry, _usage, _started = bind_runtime_telemetry(self._telemetry_factory,
            SingleAgentIdentity.SINGLE_AGENT, context, execution, self._usage_sink)
        if role is AgentRole.PLANNER:
            contract = build_single_draft_contract(phase, scenario=execution.scenario)
            decision, result, prompt = await self._reviewed(context, role, execution, phase, telemetry, task_input,
                contract, await self._model(context, execution, task_input, phase, contract, telemetry=telemetry))
            return decision, ()
        services = await _host_factory(self._services[role], execution)
        checkpoint = await services.prepare(execution)
        from orchestrator.infrastructure.run_feedback import feedback_input
        task_input.update(await _run_file_operation(feedback_input, services, execution))
        if getattr(checkpoint, 'runtime_capabilities', None) is not None:
            task_input['runtimeCapabilities'] = checkpoint.runtime_capabilities
        from agents.runtime.development_failure import failure_reader
        read_failure = failure_reader(services, execution) if role is AgentRole.DEVELOPER else None
        if role is AgentRole.DEVELOPER:
            from agents.runtime.repair_baseline import with_repair_baseline
            task_input = with_repair_baseline(task_input, checkpoint)
            from agents.runtime.startup_preservation import with_startup_context
            task_input = with_startup_context(task_input, checkpoint, execution.configuration.scenario_contract)
        async with services.client_factory(services.configuration) as host:
            host_tracked = services.tracked(host, execution)
            if telemetry is not None:
                host_tracked.set_event_sink(telemetry.tool_event)
            if role in (AgentRole.QA, AgentRole.SECURITY):
                await services.run_protected(execution, host_tracked)
            options, measured, draft_wire = {}, None, None
            if role is AgentRole.DEVELOPER:
                from agents.roles.developer_contract import ready_report_paths
                options["repair_paths"] = ready_report_paths(task_input)
                brief = task_input.get('activeRepairFocus', {}).get('brief', {})
                checkpoint.configure_path_review(options["repair_paths"], execution.configuration.scenario_contract,
                    brief.get('priorityRepairPaths', ()), brief.get('blockingPrerequisite', {}).get('sourceSites', ()),
                    brief.get('invalidInputServerError', {}).get('sourceSites', ()))
                wanted = {"read_project_file", "write_source_file", "edit_source_file"}
            elif role is AgentRole.QA:
                wanted = {"read_project_file", "write_test_file", "read_test_report"}
                task_input["testSelectors"] = {name: list(values) for name, values in services.selectors.items()}
                task_input["testTargets"] = _test_targets(services, execution)
                if execution.scenario.key == 'SERVICE-WEB':
                    task_input['sourceInterface'] = await services.source_interface(execution)
                options["selectors"] = services.selectors
            else:
                measured = await services.scan(execution, host_tracked)
                wanted = {"read_project_file", "read_security_report"}
                task_input["measuredSecurity"], task_input["sourcePaths"] = measured.analysis_input, list(measured.source_paths)
                task_input["fileAccess"] = build_file_access(execution.configuration.scenario_contract,
                    AgentRole.SECURITY, observed_source_paths=measured.source_paths)
                options.update(finding_ids=measured.finding_ids, source_paths=measured.source_paths)
                if services.test_selectors:
                    from agents.roles.security_draft import SecurityTestDraft, TEST_PLAN_INSTRUCTIONS
                    draft_wire = SecurityTestDraft(measured.source_files, execution.source.snapshot_sha256,
                        pending_requirement_ids=execution.metadata.requirement_ids)
                    task_input["sourceReferenceCatalog"] = draft_wire.catalog
                    wanted.add("write_test_file")
                    selectors = {"run_unit_tests": services.test_selectors}
                    options["security_test_selectors"] = selectors
                    task_input["testSelectors"] = {"run_unit_tests": list(services.test_selectors)}
                    task_input["testTargets"] = unit_test_targets(services.configuration.unit_test_configuration, "security", scenario_key=execution.scenario.key)
                    task_input["runtimeInstructions"] = security_test_instructions + ' ' + TEST_PLAN_INSTRUCTIONS
            contract = build_single_draft_contract(phase, scenario=execution.scenario, **options)
            if role in (AgentRole.QA, AgentRole.SECURITY) and execution.budget.limits.max_output_corrections:
                # Same enum as Multi (docs/live-api P3): the criteria are chosen, never typed.
                # Measurement settings only, like the other recovery measures; the product
                # keeps its schema and REQUIREMENT_SCOPE_INVALID path unchanged.
                contract = replace(contract, output=with_expected_results(
                    contract.output, "cases" if role is AgentRole.QA else "testCases",
                    assigned_acceptance_criteria(execution.configuration, execution.metadata, role)))
            decode = draft_wire.decode if draft_wire is not None else lambda data: data
            if draft_wire is not None:
                original_validate = contract.validate
                contract = replace(contract, output=draft_wire.output(contract.output),
                    _validate=lambda data: original_validate(decode(data)))
            async def model_round(model_input):
                # One closure so the in-step test fix (docs/live-api/05 4절) reuses the same contract.
                async with open_single_model_client(services.configuration, phase, services.client_factory) as client:
                    tracked = services.tracked(client, execution)
                    if telemetry is not None:
                        tracked.set_event_sink(telemetry.tool_event)
                    tools = tuple(tool for tool in tracked.list_tools() if tool.name in wanted)
                    if {tool.name for tool in tools} != wanted:
                        raise SingleExecutionError()
                    tools = constrain_workspace_file_tools(tools, role)
                    if role is AgentRole.SECURITY:
                        tools = constrain_frozen_read_tools(tools, measured.source_paths,
                            [receipt.report_ref for _record, receipt, _evidence in measured.scans])
                    decision, result, prompt = await self._model(context, execution, model_input, phase, contract, tools=tools,
                        source_preservation_check=(preservation_notice_for(checkpoint,
                            execution.configuration.scenario_contract, deadline_monotonic=execution.budget.deadline_monotonic)
                            if role is AgentRole.DEVELOPER else None),
                        failure_evidence=read_failure,
                        library_type_check=checkpoint.library_type_issues if role is AgentRole.DEVELOPER else None,
                        signup_quality_check=signup_quality_precheck_for(checkpoint, execution.configuration.scenario_contract)
                            if role is AgentRole.DEVELOPER and execution.budget.limits.max_output_corrections else None,
                        service_ui_check=service_interface_check(checkpoint, execution.configuration.scenario_contract,
                            task_input.get('serviceDesign'))
                            if role is AgentRole.DEVELOPER else None,
                        path_check=checkpoint.ready_path_issues if options.get("repair_paths") else None,
                        correction_candidates=checkpoint.correction_candidates if options.get("repair_paths") else None,
                        priority_check=checkpoint.pending_priority_paths if options.get("repair_paths") else None,
                        executor=_SingleTools(tracked, execution, role, services=services, measured=measured), telemetry=telemetry,
                        source_progress_check=((lambda: checkpoint.source_edit_status(
                            deadline_monotonic=execution.budget.deadline_monotonic))
                            if role is AgentRole.DEVELOPER else None),
                        precheck=(start_precheck_for(checkpoint, execution.configuration.scenario_contract)
                                  if role is AgentRole.DEVELOPER else None),
                        # Same rule and condition as validate_generated_case_expectations below.
                        expectations=((lambda data, role=role: generated_case_expectation_issues(
                            data, field="cases" if role is AgentRole.QA else "testCases",
                            configuration=execution.configuration, metadata=execution.metadata, role=role))
                            if role is AgentRole.QA or role is AgentRole.SECURITY and services.test_selectors else None),
                        # Same explainers and pre-READY checks as Multi QA/Security (handoff P5).
                        explain=_contract_explainer(role, execution, services, measured, decode=decode),
                        output_refresh=services.refresh_output if (role is AgentRole.QA or role is AgentRole.SECURITY
                                                                   and services.test_selectors) else None,
                        extra_checks=_pre_ready_checks(role, services, measured, draft_wire=draft_wire))
                    if decision.kind == "READY" and role in (AgentRole.QA, AgentRole.SECURITY):
                        cases = decision.cases if role is AgentRole.QA else decision.test_cases
                        if role is AgentRole.QA or services.test_selectors:
                            validate_generated_case_expectations(cases, configuration=execution.configuration,
                                metadata=execution.metadata, role=role)
                return decision, result, prompt

            decision, result, prompt = await self._reviewed(context, role, execution, phase, telemetry, task_input,
                contract, await model_round(task_input), expectations=(
                    (lambda data, role=role: generated_case_expectation_issues(
                        data, field="cases" if role is AgentRole.QA else "testCases",
                        configuration=execution.configuration, metadata=execution.metadata, role=role))
                    if role is AgentRole.QA or role is AgentRole.SECURITY and services.test_selectors else None))
            execution.budget.check()
            artifacts = ()
            if decision.kind == "READY":
                arguments = (execution, checkpoint, decision, host_tracked) if role is AgentRole.DEVELOPER else (
                    (execution, decision, host_tracked, measured) if role is AgentRole.SECURITY else (execution, decision, host_tracked))
                options = {}
                if role is AgentRole.QA or role is AgentRole.SECURITY and services.test_selectors:
                    options["test_fix_available"] = await _run_file_operation(services.test_fix_available, execution)
                try:
                    artifacts = await services.finalize(*arguments, task_id=context.task_id, context_id=context.context_id,
                                                        **options)
                except TestFixRequired as needed:
                    # docs/live-api/05 4절: same rule as Multi QA/Security.
                    field = "cases" if role is AgentRole.QA else "test_cases"
                    previous = getattr(decision, field)
                    decision, result, prompt = await model_round(needed.task_input(task_input, result.data))
                    if decision.kind != "READY" or not same_cases(previous, getattr(decision, field, ())):
                        if role is AgentRole.QA:
                            raise QAServicesError("QA_TEST_FIX_INVALID")
                        raise SecurityServicesError("SECURITY_TEST_FIX_INVALID")
                    if role is AgentRole.SECURITY:
                        await self._review(context, role, execution, phase, decision, result, telemetry, prompt)
                    execution.budget.check()
                    arguments = (execution, decision, host_tracked, measured) if role is AgentRole.SECURITY else (
                        execution, decision, host_tracked)
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
            await self._status(updater, context.metadata, TaskState.TASK_STATE_FAILED, error.code.value,
                               llm_failure=error.public_failure())
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
            await self._status(updater, context.metadata, state, error.code.value, llm_validation=error.public_validation(),
                               development_rejection=error.development_rejection, llm_failure=error.public_failure())
            return
        except SemanticReviewRequired as held:
            await publish_semantic_hold(updater, context.metadata, held)
            return
        except (RequirementScopeError, QAServicesError, SecurityServicesError, SinglePromptLimitError) as error:
            await self._status(updater, context.metadata, TaskState.TASK_STATE_FAILED, error.code)
            return
        except SingleContractError as error:
            report = (None if error.planner_validation is None else
                      {**error.planner_validation.public(), "traceRecorded": error.trace_recorded})
            await self._status(updater, context.metadata, TaskState.TASK_STATE_FAILED,
                f"{role.value}_OUTPUT_INVALID" if error.output_invalid else "SINGLE_AGENT_EXECUTION_FAILED",
                planner_validation=report)
            return
        except Exception:
            # Private journals preserve uncertain effects; no fabricated PASS or retry.
            await self._status(updater, context.metadata, TaskState.TASK_STATE_FAILED, "SINGLE_AGENT_EXECUTION_FAILED")
            return
        if decision.kind == "INPUT_REQUIRED":
            await self._status(updater, context.metadata, TaskState.TASK_STATE_INPUT_REQUIRED,
                               f"{role.value}_INPUT_REQUIRED", decision.questions,
                               decision.unapproved_assumptions if role is AgentRole.PLANNER else ())
            return
        if decision.kind == "REJECTED":
            await self._status(updater, context.metadata, TaskState.TASK_STATE_REJECTED,
                               "DEVELOPER_REJECTED" if role is AgentRole.DEVELOPER else "SINGLE_AGENT_OUT_OF_SCOPE",
                               development_rejection={"proposal": decision.rejection.public()}
                               if role is AgentRole.DEVELOPER else None)
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


def _contract_explainer(role, execution, services, measured, *, decode=lambda data: data):
    from agents.roles.contract_explain import qa_issues, security_issues
    from agents.roles.security_draft import SecurityTestDraft
    from orchestrator.domain.scenario_registry import RequirementValidator
    if role is AgentRole.QA:
        ids = execution.scenario.requirement_ids_for(RequirementValidator.QA)
        return lambda data: qa_issues(data, ids, services.selectors)
    if role is AgentRole.SECURITY:
        ids = execution.scenario.requirement_ids_for(RequirementValidator.SECURITY)
        explain = lambda value: security_issues(value, ids, measured.finding_ids, measured.source_paths,
                                                services.test_selectors or None)
        wire = getattr(decode, '__self__', None)
        return (lambda data: wire.explain(data, explain)) if isinstance(wire, SecurityTestDraft) else lambda data: explain(decode(data))
    return None


def _pre_ready_checks(role, services, measured, *, draft_wire=None):
    if role is AgentRole.QA:
        return (("GENERATED_TESTS_INVALID", services.generated_test_issues),)
    if role is AgentRole.SECURITY:
        return (("GENERATED_TESTS_INVALID", services.generated_test_issues),
                ("CODE_REFERENCE_INVALID", lambda data: (
                    draft_wire.reference_issues(services, measured, data) if draft_wire is not None
                    else services.reference_issues(measured, data))))
    return ()
