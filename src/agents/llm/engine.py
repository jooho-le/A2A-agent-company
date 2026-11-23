"""Bounded model → approved Tool → structured JSON loop; no product verdict."""

import asyncio
import json
from dataclasses import replace
from collections.abc import Callable
from time import monotonic

from agents.llm import corrections as correction_rules
from agents.llm import raw_capture
from agents.llm.developer_continuation import MeasuredInput, RepairReadHistory
from agents.llm.recovery import CallRoute, CorrectionObservation, FailureRoute, correction_route, failure_route
from agents.llm.budget import ExecutionBudget
from agents.llm.content import sanitize_content, validate_source_fields
from agents.llm.tool_wire import strict_tool_schema, project_tool_arguments, STRICT_TOOL_ARGUMENTS, PROJECT_TOOL_ARGUMENTS, pin_workspace_argument
from agents.llm.validation import validate as validate_schema, policy_error, parse_candidate
from agents.llm.contracts import (
    LLMErrorCode, LLMProvider, LLMRequest, LLMResult, LLMRuntimeError,
    LLMResponse, SourceEditStatus, StructuredOutput, TokenUsage, ToolContext, ToolDefinition, ToolExecutor, UsageRecord,
    json_text, parse_json, validate_billing_metadata,
)
from agents.roles.contracts import get_role_contract
from agents.roles.prompts import PreparedRolePrompt, build_system_prompt
from agents.semantic_review import PreparedSemanticPrompt, SEMANTIC_PROMPT_VERSION, SYSTEM as SEMANTIC_SYSTEM
from agents.core.identity import AgentServiceIdentity, SingleAgentIdentity, SinglePhase
from agents.single.prompts import PreparedSinglePrompt, build_single_system_prompt, SINGLE_PROMPT_VERSION
from mcp_tools.core.policy import SINGLE_PHASE_TOOL_ROLES
from orchestrator.core.security import redact_data, redact_text
from orchestrator.domain.run_configuration import ModelConfiguration
from orchestrator.domain.states import AgentRole
from orchestrator.observability import capture_engine_output

# Pause before re-sending after a rate limit/server error; counted in the Run deadline.
PROVIDER_RETRY_DELAY_SECONDS = 1.0
# Recorded in the Host policy: a transient error with unknown usage books an upper bound before the one retry.
UNKNOWN_USAGE_POLICY = 'UNKNOWN_USAGE_KEPT_WITH_SEPARATE_CAP_BOUND_V2'
INPUT_RESERVATION_POLICY = 'OPENAI_MEASURED_DEVELOPER_PREFIX_AND_SHARED_RESERVATIONS_V4'
# V2: a call blocked only by another role's pending reservation waits for it within the Run deadline.
RESERVATION_WAIT_SECONDS = 0.05


class LLMEngine:
    """Tool executors are trusted injection points; MCP/ACL arrive in 20–29.

    Failed/uncertain Tool execution is never repeated. A single pre-execution
    test-source credential rejection may receive one value-free correction.
    Other pre-side-effect format mistakes may be regenerated only when
    ``LLMLimits.max_output_corrections`` allows it (default 0; see
    agents.llm.corrections). ``output_check`` lets a caller run its existing
    decision validator before the result is returned; the caller still applies
    that validator afterwards, so acceptance and error codes are unchanged.
    Optional accounting/lifecycle sinks are trusted Host writers, never models.
    The default A2A server stays Bootstrap/REJECTED until role executors exist.
    """

    def __init__(
        self, *, role: AgentServiceIdentity, provider: LLMProvider,
        tools: tuple[ToolDefinition, ...] = (), tool_executor: ToolExecutor | None = None,
        single_phase: SinglePhase | None = None,
        semantic_review: bool = False,
    ):
        single = role is SingleAgentIdentity.SINGLE_AGENT
        if (single and not isinstance(single_phase, SinglePhase)
                or not single and single_phase is not None):
            raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
        self._single_phase = single_phase
        self._role = role if single else get_role_contract(role).role
        self._provider = provider
        self._semantic_review = semantic_review
        if semantic_review and (tools or tool_executor is not None):
            raise LLMRuntimeError(LLMErrorCode.TOOL_POLICY)
        allowed = SINGLE_PHASE_TOOL_ROLES[single_phase] if single else get_role_contract(role).allowed_tool_names
        if not isinstance(tools, tuple) or any(tool.name not in allowed for tool in tools):
            raise LLMRuntimeError(LLMErrorCode.TOOL_POLICY)
        if len({tool.name for tool in tools}) != len(tools) or tools and tool_executor is None:
            raise LLMRuntimeError(LLMErrorCode.TOOL_POLICY)
        for tool in tools:
            validate_source_fields(tool)
        self._tools = tools
        self._definitions = {tool.name: tool for tool in tools}
        self._executor = tool_executor

    @capture_engine_output
    async def run(
        self, *, prompt: PreparedRolePrompt | PreparedSinglePrompt | PreparedSemanticPrompt, model: ModelConfiguration,
        output: StructuredOutput, budget: ExecutionBudget,
        workspace_id: str | None = None,
        usage_sink: Callable[[UsageRecord], None] | None = None,
        call_started_sink: Callable[[int], None] | None = None,
        input_observed_sink: Callable[[dict], None] | None = None,
        output_observed_sink: Callable[[dict], None] | None = None,
        validation_failed_sink: Callable | None = None,
        recovery_sink: Callable | None = None,
        output_check: Callable[[dict, tuple], dict | None] | None = None,
        source_write_precheck: Callable[[], list[dict]] | None = None,
        source_preservation_check: Callable | None = None,
        source_progress_check: Callable[[], bool | SourceEditStatus] | None = None,
        correction_candidates: Callable | None = None,
        output_refresh: Callable | None = None,
        close_invalid_qa_draft: bool = False,
        development_rejection_check: Callable | None = None,
    ) -> LLMResult:
        records: list[UsageRecord] = []
        seen_calls: set[str] = set()
        executed = 0
        executed_names = []
        source_changed = False
        source_notices = set()
        required_tool = None
        required_alternatives = ()  # e.g. edit_source_file beside a forced write_source_file
        required_test_paths = ()
        source_repair_path = source_repair_sha256 = None
        source_repair_full_write = False
        source_review_pending = []
        sequence = operation_sequence = None
        decided_sequence = None
        recovery_sink_failed = False
        pending_correction = None
        correction_attempt = 0
        base_output, catalog_json = output, None
        catalog = {}
        restricted_paths = set()
        refresh_path_output = False
        last_development_rejection = None

        def exhausted_qa_draft(validation=None):
            # Preserve the value-free cause when replacing the failure code
            # with its terminal recovery route; A2A/Host must still see it.
            raise LLMRuntimeError(LLMErrorCode.VALIDATION_DRAFT, validation=validation)

        security_draft = (self._role is AgentRole.SECURITY or self._role is SingleAgentIdentity.SINGLE_AGENT
                          and self._single_phase is SinglePhase.SECURITY)

        def missing_test_files(kind, issues):
            return (close_invalid_qa_draft and kind == 'GENERATED_TESTS_INVALID'
                    and any(i['reasonCode'] == 'GENERATED_TEST_FILE_MISSING' for i in issues))

        def qa_draft_rejection(kind, issues):
            # Draft-format exhaustion that ends the step as a technical STOP (QA, and Security with
            # generated tests). For Security a code reference that stays invalid also qualifies: the
            # step still never reaches semantic review with known-invalid evidence.
            return close_invalid_qa_draft and (kind in {
                'MODEL_OUTPUT_INVALID', 'OUTPUT_CONTRACT_INVALID'} or
                kind == 'GENERATED_TESTS_INVALID' and any(i['reasonCode'] in {
                    'TEST_IN_FILE_NOT_DECLARED', 'DECLARED_TEST_NOT_IN_FILE',
                    'GENERATED_TEST_FILE_MISSING'} for i in issues)
                or security_draft and kind == 'CODE_REFERENCE_INVALID')

        def source_status():
            try:
                status = source_changed if source_progress_check is None else source_progress_check()
                if isinstance(status, SourceEditStatus):
                    return status
                if type(status) is bool:
                    return SourceEditStatus.CHANGED if status else SourceEditStatus.UNCHANGED
                raise ValueError("Invalid source progress")
            except Exception:
                raise LLMRuntimeError(LLMErrorCode.CONFIGURATION) from None

        def publish_route(route):
            nonlocal recovery_sink_failed
            if recovery_sink is not None:
                try:
                    recovery_sink(route)
                except Exception:
                    recovery_sink_failed = True
                    budget.invalidate()
                    raise LLMRuntimeError(LLMErrorCode.CONFIGURATION) from None

        def observed(*, response_kind, requested=(), executed=False, refusal=None):
            """Closed facts of a corrective turn (Codex review 2026-11-19 #4); never model text."""
            try:
                return CorrectionObservation(requiredTools=tuple((required_tool, *required_alternatives))
                    if required_tool is not None else (), responseKind=response_kind,
                    requestedTools=tuple(dict.fromkeys(str(name) for name in requested))[:16],
                    executed=executed, refusal=refusal)
            except Exception:
                return None

        def decide_correction(sequence, kind, allowed, reason, *, qa_exhausted=False, draft_issue=None,
                              development_rejection=None, observation=None, unlinked_paths=(), path_evidence=()):
            nonlocal pending_correction, decided_sequence, correction_attempt
            route = (FailureRoute(sequence=sequence, operationSequence=operation_sequence,
                code=LLMErrorCode.VALIDATION_DRAFT.value, category='MODEL_FORMAT', action='STOP', reason=reason)
                if qa_exhausted else correction_route(kind, sequence=sequence, operation_sequence=operation_sequence,
                                                     allowed=allowed, reason=reason,
                                                     development_rejection=development_rejection))
            if draft_issue is not None:
                route = route.model_copy(update={'draft_issue': draft_issue})
            if observation is not None:
                route = route.model_copy(update={'observation': observation})
            if unlinked_paths:
                route = route.model_copy(update={'unlinked_repair_paths': unlinked_paths})
            if path_evidence:
                route = route.model_copy(update={'repair_path_evidence': path_evidence})
            publish_route(route)
            decided_sequence = sequence
            if allowed:
                correction_attempt += 1
                pending_correction = (sequence, kind, correction_attempt)
        try:
            if self._semantic_review:
                review_system = (build_single_system_prompt() if self._role is SingleAgentIdentity.SINGLE_AGENT
                                 else SEMANTIC_SYSTEM)
                valid = (type(prompt) is PreparedSemanticPrompt and prompt.actor == self._role
                         and prompt.phase is self._single_phase and prompt.version == SEMANTIC_PROMPT_VERSION
                         and prompt.system_prompt == review_system and prompt.output == output)
            elif self._role is SingleAgentIdentity.SINGLE_AGENT:
                valid = (isinstance(prompt, PreparedSinglePrompt)
                         and prompt.identity is SingleAgentIdentity.SINGLE_AGENT
                         and prompt.phase is self._single_phase and prompt.version == SINGLE_PROMPT_VERSION
                         and prompt.system_prompt == build_single_system_prompt()
                         and prompt.draft_contract.output == output)
            else:
                valid = (isinstance(prompt, PreparedRolePrompt) and prompt.role == self._role
                         and prompt.version == get_role_contract(self._role).version
                         and prompt.system_prompt == build_system_prompt(self._role))
            if development_rejection_check is not None and (self._semantic_review or source_progress_check is None
                    or not (self._role is AgentRole.DEVELOPER or self._role is SingleAgentIdentity.SINGLE_AGENT
                            and self._single_phase in (SinglePhase.IMPLEMENTING, SinglePhase.FIXING))):
                raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
            if not valid or model.provider != self._provider.name:
                raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
            # QA and (with generated tests) Security draft their test bindings; only they get the
            # Host test-ID catalog and the evidence-bound technical stop (Codex review 2026-11-18 #1·#2).
            if (output_refresh is not None or close_invalid_qa_draft) and not (
                    not self._semantic_review and (self._role in (AgentRole.QA, AgentRole.SECURITY) or
                    self._role is SingleAgentIdentity.SINGLE_AGENT
                    and self._single_phase in (SinglePhase.QA, SinglePhase.SECURITY))):
                raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
            if workspace_id is not None and (not isinstance(workspace_id, str) or not workspace_id.strip()):
                raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
            # The workspace is Host-owned: the wire offers only its value (#31 WORKSPACE_MISMATCH).
            available_tools = tuple(pin_workspace_argument(tool, workspace_id) for tool in self._tools)
            self._provider.validate_configuration(model, output)
            limit = budget.limits.max_json_bytes
            task_input = parse_json(prompt.input_json, max_bytes=limit)
            if not isinstance(task_input, dict):
                raise LLMRuntimeError(LLMErrorCode.RESPONSE)
            items = [{"role": "user", "content": json_text(redact_data(task_input), max_bytes=limit)}]
            developer_repair = (not self._semantic_review and
                (self._role is AgentRole.DEVELOPER or self._single_phase is SinglePhase.FIXING)
                and isinstance(task_input.get('taskInput'), dict)
                and isinstance(task_input['taskInput'].get('fixRequest'), dict))
            read_history = RepairReadHistory(task_input['taskInput']) if developer_repair else None
            if correction_candidates is not None and (not developer_repair or source_progress_check is None
                                                       or output_check is None or output_refresh is not None):
                raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
            measured_input = None
            test_source_corrections = 0
            allowance = correction_rules.CorrectionAllowance(budget.limits.max_output_corrections)
            reminders = correction_rules.CorrectionAllowance(budget.limits.max_question_reminders)
            provider_retries = correction_rules.CorrectionAllowance(budget.limits.max_provider_retries)
            tool_errors = correction_rules.CorrectionAllowance(budget.limits.max_output_corrections)

            def capture(sequence, kind, corrected, report=None, output_text=None, tool_calls=None):
                raw_capture.capture(role=self._role, phase=self._single_phase, sequence=sequence, kind=kind,
                                    corrected=corrected, workspace_id=workspace_id, report=report,
                                    output_text=output_text, tool_calls=tool_calls)

            def reserve_for(history, messages, wire_tools):
                if self._provider.name != 'openai' or budget.limits.max_total_tokens is None:
                    return 0
                estimate = self._usage_upper_bound(prompt, history, wire_tools, output, 0)
                if estimate is None:
                    raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
                if measured_input is None:
                    return estimate.input_tokens
                contract = (model, prompt.system_prompt, wire_tools, output, required_tool,
                            required_alternatives if required_tool is not None else ())
                return measured_input.reserve(estimate.input_tokens, history, messages, contract,
                                              repair_schema_changed=bool(restricted_paths))

            def correct(sequence, kind, *, issues, report=None, stage=None, tool_name=None,
                        output_text=None, tool_calls=None, development_rejection=None) -> bool:
                """Append fixed feedback and allow one more model call, or refuse."""
                budget_for = reminders if kind == 'QUESTIONS_NOT_ANSWERED' else allowance
                # Only an unexplained contract rejection is told apart by its output;
                # coded mistakes (no change, cut-off, question) stay "same mistake once".
                fallback = (correction_rules.output_digest(output_text)
                            if kind in correction_rules.UNEXPLAINED_KINDS else None)
                signature = correction_rules.signature(kind, stage, tool_name, issues, fallback=fallback)
                if kind == 'GENERATED_TESTS_INVALID' and catalog.get('filesSha256'):
                    # A real file change may expose the next defect; it is not
                    # the same unchanged draft. The total correction cap stays fixed.
                    signature = (*signature, catalog['filesSha256'])
                normal_correction = kind == 'READY_RUNTIME_REPAIR_REQUIRED' or (
                    kind == 'READY_PRIORITY_REPAIR_NEEDED' and any(
                        i['fieldPath'] == '/repairPaths/NORMAL_REQUEST_RUNTIME_FAILURE' for i in issues))
                if normal_correction:
                    # Switching from an overclaim to an honest partial (or changing other cited paths)
                    # must not buy a second repair opportunity for the same measured prerequisite.
                    signature = ('NORMAL_REQUEST_RUNTIME_FAILURE',)
                if kind in ('READY_LIBRARY_TYPE_MISMATCH', 'READY_OUTPUT_SAFETY_REVIEW'):
                    # One source correction per stage, even if the next READY changes file, line or type.
                    signature = (kind,)
                if kind == 'READY_SIGNUP_QUALITY_REVIEW':
                    # A resolved path and a newly exposed risk may use the
                    # remaining shared allowance; moving a line or rewording
                    # READY does not buy another correction for the same risks.
                    signature = (kind, tuple(sorted({(r.get('sourcePath', ''), r['reasonCode'],
                                                       r.get('reviewRevision', '')) for r in issues})))
                allowed = budget_for.take(signature)
                reason = budget_for.last_reason
                repair_input = task_input.get('taskInput')
                repair_brief = None
                if kind in ('READY_PATH_EVIDENCE_MISMATCH', 'READY_RUNTIME_REPAIR_REQUIRED') and isinstance(repair_input, dict):
                    repair_brief = repair_input.get('activeRepairFocus', {}).get('brief')
                feedback = correction_rules.feedback(kind, issues=issues, remaining=budget_for.remaining,
                                                     stage=stage, tool_name=tool_name, repair_brief=repair_brief)
                if kind == 'GENERATED_TESTS_INVALID':
                    feedback['hostCorrection']['testRepairTargets'] = {
                        'missingRequiredWritePaths': catalog.get('missingRequiredWritePaths', [])[:16],
                        'catalogFilesSha256': catalog.get('filesSha256'),
                        'instruction': 'Use exact IDs from the latest hostTestIdCatalog already supplied. '
                                       'Write the missing paths, not an unrelated unit file.'}
                if allowed and normal_correction:
                    from agents.runtime.normal_request_repair import priority_repair_evidence
                    evidence = priority_repair_evidence(task_input['taskInput'])
                    if evidence:
                        feedback['hostCorrection']['measuredFailure'] = evidence
                if allowed and kind in ('READY_PRIORITY_REPAIR_NEEDED', 'READY_RUNTIME_REPAIR_REQUIRED',
                                        'READY_LIBRARY_TYPE_MISMATCH', 'READY_OUTPUT_SAFETY_REVIEW', 'READY_SIGNUP_QUALITY_REVIEW'):
                    # Source work and a subsequent truthful report share the existing Run budget.
                    messages = [*items, {'role': 'user', 'content': json_text(feedback, max_bytes=limit)}]
                    history = json.dumps(messages, ensure_ascii=False, separators=(',', ':'))
                    review_paths = (correction_rules.source_review_paths(issues)
                                    if kind == 'READY_SIGNUP_QUALITY_REVIEW'
                                    and {'read_project_file', 'write_source_file'} <= self._definitions.keys() else ())
                    required_calls = 2 * len(review_paths) + 1 if review_paths else 2
                    required_tools = 2 * len(review_paths) if review_paths else 1
                    if (len(history.encode()) + len(prompt.system_prompt.encode()) > limit
                            or budget.limits.max_tool_calls - budget.tool_calls < required_tools
                            or kind in ('READY_LIBRARY_TYPE_MISMATCH', 'READY_OUTPUT_SAFETY_REVIEW', 'READY_SIGNUP_QUALITY_REVIEW') and
                            budget.limits.max_model_calls - budget.model_calls < required_calls):
                        # Fixing the incompatible call requires a source tool turn and a later READY turn.
                        allowed, reason = False, 'LIMIT_REACHED'
                    else:
                        try:
                            budget.check_model_call(input_tokens=reserve_for(history, messages, available_tools))
                        except LLMRuntimeError as exc:
                            if exc.code is not LLMErrorCode.BUDGET:
                                raise
                            allowed, reason = False, 'LIMIT_REACHED'
                decide_correction(sequence, kind, allowed, reason,
                                  qa_exhausted=not allowed and qa_draft_rejection(kind, issues),
                                  draft_issue='GENERATED_TEST_FILE_MISSING' if missing_test_files(kind, issues) else None,
                                  development_rejection=development_rejection,
                                  path_evidence=correction_rules.ready_path_trace(feedback['hostCorrection']),
                                  unlinked_paths=(tuple(dict.fromkeys(i['fieldPath'].split('/')[2] for i in issues))
                                                  if kind in ('READY_PATH_EVIDENCE_MISMATCH', 'READY_PRIORITY_REPAIR_NEEDED',
                                                              'READY_RUNTIME_REPAIR_REQUIRED') else ()),
                                  observation=observed(
                                      response_kind='TOOL_CALLS' if tool_calls else (
                                          'FINAL_OUTPUT' if output_text is not None else 'EMPTY'),
                                      requested=[call.name for call in tool_calls or ()],
                                      refusal='TOOL_BATCH_REJECTED' if tool_calls else 'OUTPUT_REJECTED'))
                if not allowed:
                    if kind != 'QUESTIONS_NOT_ANSWERED':  # a plain question is not a rejection to capture
                        capture(sequence, kind, False, report, output_text, tool_calls)
                    return False
                capture(sequence, kind, True, report, output_text, tool_calls)
                if report is not None and validation_failed_sink is not None:
                    try:
                        validation_failed_sink(report)
                    except Exception:
                        pass
                items.append({'role': 'user', 'content': json_text(feedback, max_bytes=limit)})
                return True

            while True:
                if refresh_path_output:
                    from agents.roles.developer_contract import corrective_ready_output
                    source_status()  # Refresh the checkpoint's links, including reversions and partial edits.
                    output = corrective_ready_output(base_output, correction_candidates(sorted(restricted_paths)))
                    self._provider.validate_configuration(model, output)
                    refresh_path_output = False
                if output_refresh is not None:
                    try:
                        output, catalog = output_refresh(base_output)
                        self._provider.validate_configuration(model, output)
                        refreshed = json_text({'hostTestIdCatalog': catalog}, max_bytes=limit)
                        if refreshed != catalog_json:
                            items.append({'role': 'user', 'content': refreshed})
                            catalog_json = refreshed
                    except Exception:
                        raise LLMRuntimeError(LLMErrorCode.CONFIGURATION) from None
                # Includes wire-only reasoning continuation; never send it to a sink.
                history = json_text(items, max_bytes=limit)
                # With alternative edit tools the provider requires any one of the offered tools.
                # Offer exactly the operations the Host will execute on this corrective turn.
                wire_tools = available_tools
                if required_tool == 'write_test_file' and required_test_paths:
                    from agents.llm.tool_wire import pin_test_write_paths
                    wire_tools = tuple(pin_test_write_paths(tool, required_test_paths)
                                       for tool in available_tools if tool.name == 'write_test_file')
                if required_tool and required_alternatives:
                    wire_tools = tuple(tool for tool in available_tools
                                       if tool.name in (required_tool, *required_alternatives))
                if source_repair_path is not None:
                    from agents.llm.tool_wire import pin_source_repair
                    wire_tools = tuple(pin_source_repair(tool, source_repair_path, source_repair_sha256)
                                       for tool in wire_tools if tool.name in (required_tool, *required_alternatives))
                if len(history.encode("utf-8")) + len(prompt.system_prompt.encode("utf-8")) > limit:
                    raise LLMRuntimeError(LLMErrorCode.BUDGET)
                input_reserve = reserve_for(history, items, wire_tools)
                while True:
                    try:
                        sequence, output_cap, timeout = budget.reserve_model_call(input_tokens=input_reserve)
                        break
                    except LLMRuntimeError as error:
                        # Another role's in-flight reservation is not exhaustion: wait for it to settle
                        # within the Run deadline (remaining_seconds raises BUDGET once time is up).
                        if (error.code is not LLMErrorCode.BUDGET
                                or not budget.waits_for_pending_reservations(input_tokens=input_reserve)):
                            raise
                        await asyncio.sleep(min(RESERVATION_WAIT_SECONDS, budget.remaining_seconds()))
                if call_started_sink is not None:
                    try:
                        call_started_sink(sequence)
                    except Exception:
                        budget.invalidate()
                        raise
                operation_sequence = operation_sequence or sequence
                publish_route(CallRoute(sequence=sequence, operationSequence=operation_sequence,
                    mode='REGENERATION' if pending_correction is not None else (
                        'INITIAL' if sequence == operation_sequence else 'CONTINUATION'),
                    triggerSequence=None if pending_correction is None else pending_correction[0],
                    code=None if pending_correction is None else pending_correction[1],
                    correctionAttempt=0 if pending_correction is None else pending_correction[2]))
                pending_correction = None
                # Reservation/Trace persistence consumes the same Run deadline.
                # Never start a provider after those writes exhausted it.
                budget.check()
                timeout = min(timeout, budget.remaining_seconds())
                request = LLMRequest(
                    model=model, system_prompt=prompt.system_prompt,
                    input_items_json=history, tools=wire_tools, output=output,
                    max_output_tokens=output_cap, timeout_seconds=timeout, required_tool=required_tool,
                    required_tool_alternatives=required_alternatives if required_tool is not None else (),
                )
                if input_observed_sink is not None:
                    try:
                        from .input_evidence import observe_input
                        input_observed_sink(observe_input(sequence, request))
                    except Exception:
                        budget.invalidate()
                        raise
                budget.check()
                timeout = min(timeout, budget.remaining_seconds())
                started = monotonic()
                try:
                    response = await asyncio.wait_for(self._provider.complete(request), timeout=timeout)
                    # Raw frozen dataclasses can still be privilegedly mutated.
                    # Invalid metadata must not escape into a usage sink; valid
                    # token accounting survives even if provenance is rejected.
                    known_usage = None
                    try:
                        if type(response) is not LLMResponse:
                            raise LLMRuntimeError(LLMErrorCode.RESPONSE)
                        if response.usage is not None:
                            if type(response.usage) is not TokenUsage:
                                raise LLMRuntimeError(LLMErrorCode.RESPONSE)
                            response.usage.__post_init__()
                            known_usage = response.usage
                        validate_billing_metadata(response.reported_service_tier, response.billing_context)
                        if (type(response.tool_arguments_format) is not str
                                or response.tool_arguments_format not in (PROJECT_TOOL_ARGUMENTS, STRICT_TOOL_ARGUMENTS)
                                or response.tool_arguments_format == STRICT_TOOL_ARGUMENTS and self._provider.name != 'openai'):
                            raise LLMRuntimeError(LLMErrorCode.RESPONSE)
                    except Exception:
                        raise LLMRuntimeError(LLMErrorCode.RESPONSE, usage=known_usage) from None
                except asyncio.CancelledError as canceled:
                    budget.account_usage(None, sequence=sequence)
                    record = UsageRecord(sequence, self._role, model, None, "canceled", self._duration(started), None)
                    records.append(record)
                    try:
                        if usage_sink is not None:
                            self._publish_usage(usage_sink, record, budget)
                    finally:
                        raise canceled from None
                except Exception as error:
                    code = error.code if isinstance(error, LLMRuntimeError) else (
                        LLMErrorCode.TIMEOUT if isinstance(error, asyncio.TimeoutError) else LLMErrorCode.PROVIDER
                    )
                    known_usage = error.usage if isinstance(error, LLMRuntimeError) else None
                    if known_usage is not None:
                        try:
                            if type(known_usage) is not TokenUsage:
                                raise ValueError()
                            known_usage.__post_init__()
                        except Exception:
                            known_usage = None
                    bound = None
                    if (known_usage is None and isinstance(error, LLMRuntimeError) and error.transient
                            and provider_retries.enabled):
                        # 2026-11-18 (#29): an HTTP 5xx without usage left the budget "unknown", which
                        # under a token cap refuses every later call. A token is at least one byte, so
                        # the request's bytes plus the output cap bound the real usage from above.
                        # V2 (Codex review 2026-11-18): the ledger keeps the usage unknown, exactly like
                        # the usage record below, and holds the bound separately for the cap check only.
                        if input_reserve:
                            bound = input_reserve + output_cap
                        else:
                            estimate = self._usage_upper_bound(prompt, history, wire_tools, output, output_cap)
                            bound = None if estimate is None else estimate.total_tokens
                    budget.account_usage(known_usage, sequence=sequence, bound=bound)
                    record = UsageRecord(sequence, self._role, model, None, code.value, self._duration(started), known_usage)
                    records.append(record)
                    if usage_sink is not None:
                        self._publish_usage(usage_sink, record, budget)
                    # The adapter marks only failures with no model response (no
                    # connection, HTTP 429/5xx). Nothing ran, so the same request may
                    # be sent once more when llmLimits.max_provider_retries allows it.
                    # A refused retry keeps the original failure record below.
                    # Codex review 2026-11-18 #1: wait like a normal call while only another role's
                    # pending reservation blocks the re-send; spend the retry only once it can be sent.
                    if (isinstance(error, LLMRuntimeError) and error.transient and provider_retries.enabled
                            and await self._budget_allows_another_call(budget, input_reserve)
                            and provider_retries.take(('PROVIDER_TRANSIENT_RETRY',))):
                        decide_correction(sequence, 'PROVIDER_TRANSIENT_RETRY', True, provider_retries.last_reason)
                        await asyncio.sleep(min(PROVIDER_RETRY_DELAY_SECONDS, max(0.0, budget.remaining_seconds())))
                        budget.check()
                        continue
                    raise LLMRuntimeError(code) from None
                budget.account_usage(response.usage, sequence=sequence)
                if developer_repair and input_reserve and not any(item.get('type') == 'reasoning' for item in items):
                    measured_input = MeasuredInput.from_receipt(request, response, len(items))
                else:
                    measured_input = None
                outcome = "refused" if response.refused else (
                    response.status if response.status in ("completed", "incomplete", "failed") else LLMErrorCode.RESPONSE.value
                )
                reported_model = redact_text(response.model_id)[:256] if isinstance(response.model_id, str) else None
                record = UsageRecord(
                    sequence, self._role, model, reported_model, outcome,
                    self._duration(started), response.usage,
                    reported_service_tier=response.reported_service_tier,
                    billing_context=response.billing_context,
                )
                records.append(record)
                if usage_sink is not None:
                    self._publish_usage(usage_sink, record, budget)
                if output_observed_sink is not None:
                    try:
                        # Usage is durable even if output observation fails.
                        # Provider reasoning/continuation internals are excluded.
                        output_observed_sink({'sequence': sequence, 'outputText': response.output_text,
                            'toolCalls': [{'name': call.name, 'arguments': call.arguments_json}
                                          for call in response.tool_calls]})
                    except Exception:
                        budget.invalidate()
                        raise
                budget.check()
                if response.refused:
                    raise LLMRuntimeError(LLMErrorCode.REFUSAL)
                if response.status == "incomplete":
                    # Discarded before any Tool runs. Only a stop at the output cap
                    # (a runaway answer) earns one bounded regeneration.
                    at_cap = response.incomplete_reason == 'max_output_tokens' or (
                        response.incomplete_reason is None and type(response.usage) is TokenUsage
                        and type(output_cap) is int and response.usage.output_tokens >= output_cap)
                    if at_cap:
                        if correct(sequence, 'OUTPUT_LIMIT_REACHED', issues=[], output_text=response.output_text,
                                   tool_calls=response.tool_calls):
                            continue
                    else:
                        capture(sequence, 'RESPONSE_INCOMPLETE', False, output_text=response.output_text,
                                tool_calls=response.tool_calls)
                        stopped = failure_route(LLMRuntimeError(LLMErrorCode.INCOMPLETE), sequence=sequence,
                                                operation_sequence=operation_sequence)
                        publish_route(stopped.model_copy(update={'incomplete_reason': response.incomplete_reason or 'unreported'}))
                        decided_sequence = sequence
                    raise LLMRuntimeError(LLMErrorCode.INCOMPLETE)
                if response.status != "completed":
                    raise LLMRuntimeError(LLMErrorCode.PROVIDER)
                # Test writers still pass through the ordinary contract gate: a genuine question or
                # valid REJECTED stays a hold, while another file-less READY exhausts the draft.
                if required_tool is not None and required_tool != 'write_test_file' and not response.tool_calls:
                    # A forced edit must not hide an explained rejection or a real question.
                    # This peek grants nothing: full schema/contract/routing gates still follow.
                    deferred_hold = False
                    if development_rejection_check is not None and response.output_text is not None:
                        try:
                            candidate = parse_json(response.output_text, max_bytes=limit)
                            deferred_hold = isinstance(candidate, dict) and candidate.get('kind') in (
                                'REJECTED', 'INPUT_REQUIRED')
                        except (ValueError, LLMRuntimeError):
                            pass
                    if not deferred_hold:
                        decide_correction(sequence, 'READY_WITHOUT_CHANGES', False, 'REPEATED',
                            observation=observed(response_kind='FINAL_OUTPUT' if response.output_text is not None
                                                 else 'EMPTY', refusal='FINAL_OUTPUT_INSTEAD_OF_REQUIRED_TOOL'))
                        capture(sequence, 'READY_WITHOUT_CHANGES', False, output_text=response.output_text)
                        raise LLMRuntimeError(LLMErrorCode.NO_PROGRESS)
                if response.tool_calls:
                    budget.check_tool_batch(len(response.tool_calls))
                    prepared = []
                    batch_ids = set()
                    correct_test_source = False
                    # Validate the ENTIRE batch before the first side effect.
                    try:
                        for call in response.tool_calls:
                            definition = self._definitions.get(call.name)
                            if definition is None:
                                raise policy_error('TOOL_NOT_OFFERED', sequence=sequence, tool_name=call.name,
                                                   boundary='TOOL_SELECTION', field_path='/toolName')
                            if not isinstance(call.call_id, str) or not call.call_id.strip():
                                raise policy_error('CALL_ID_INVALID', sequence=sequence, tool_name=call.name,
                                                   boundary='TOOL_SELECTION', field_path='/callId')
                            if call.call_id in seen_calls or call.call_id in batch_ids:
                                raise policy_error('CALL_ID_REUSED', sequence=sequence, tool_name=call.name,
                                                   boundary='TOOL_SELECTION', field_path='/callId')
                            arguments = parse_candidate(call.arguments_json, definition.input_schema, max_bytes=limit,
                                stage='TOOL_ARGUMENTS', sequence=sequence, tool_name=definition.name)
                            execution_call = call
                            if response.tool_arguments_format == STRICT_TOOL_ARGUMENTS:
                                validate_schema(strict_tool_schema(definition.input_schema), arguments,
                                    stage='TOOL_ARGUMENTS_WIRE', sequence=sequence, tool_name=definition.name)
                                arguments = project_tool_arguments(definition.input_schema, arguments)
                                # Keep the raw wire call in observations/continuation.
                                # The executor and its journal receive the same decoded
                                # project JSON, preserving their exact-argument checks.
                                execution_call = replace(call, arguments_json=json_text(arguments, max_bytes=limit))
                            validate_schema(definition.input_schema, arguments,
                                stage='TOOL_ARGUMENTS', sequence=sequence, tool_name=definition.name)
                            if not isinstance(arguments, dict):
                                raise LLMRuntimeError(LLMErrorCode.SCHEMA)
                            if "workspaceId" in arguments and arguments["workspaceId"] != workspace_id:
                                raise policy_error('WORKSPACE_MISMATCH', sequence=sequence, tool_name=call.name,
                                                   field_path='/workspaceId')
                            if (required_tool == 'write_test_file' and required_test_paths
                                    and call.name == 'write_test_file'
                                    and arguments.get('path') not in required_test_paths):
                                raise policy_error('TOOL_NOT_OFFERED', sequence=sequence, tool_name=call.name,
                                                   field_path='/path')
                            if source_repair_path is not None:
                                narrowed = next((t for t in wire_tools if t.name == call.name), None)
                                if narrowed is None:
                                    raise policy_error('TOOL_NOT_OFFERED', sequence=sequence, tool_name=call.name)
                                validate_schema(narrowed.input_schema, arguments, stage='TOOL_ARGUMENTS',
                                                sequence=sequence, tool_name=call.name)
                            # Never pass recognized secrets from model output into Tools.
                            try:
                                sanitize_content(arguments, source_fields=definition.source_argument_fields, reject_secrets=True,
                                                 sequence=sequence, tool_name=definition.name)
                            except LLMRuntimeError as rejected:
                                # One pre-execution correction, only for a single
                                # rejected test write. Never repeat an executed Tool
                                # or echo its unsafe arguments into the transcript.
                                report = rejected.validation
                                test_write = (len(response.tool_calls) == 1 and call.name == 'write_test_file'
                                        and (self._role in (AgentRole.QA, AgentRole.SECURITY)
                                             or self._role is SingleAgentIdentity.SINGLE_AGENT
                                             and self._single_phase in (SinglePhase.QA, SinglePhase.SECURITY)))
                                # Developer (and Single implement/fix) source writes get the same one
                                # rewrite when corrections are on (handoff P5, point 2). The whole batch
                                # was validated before any call ran, so nothing in it has executed.
                                source_write = (call.name in {'write_source_file', 'edit_source_file'} and allowance.enabled
                                        and (self._role is AgentRole.DEVELOPER
                                             or self._role is SingleAgentIdentity.SINGLE_AGENT
                                             and self._single_phase in (SinglePhase.IMPLEMENTING, SinglePhase.FIXING)))
                                if not ((test_write or source_write)
                                        and rejected.code is LLMErrorCode.TOOL_POLICY
                                        and getattr(report, 'reason_code', None) == 'SOURCE_CREDENTIAL_DENIED'
                                        and test_source_corrections == 0):
                                    raise
                                if validation_failed_sink is not None:
                                    try:
                                        validation_failed_sink(report)
                                    except Exception:
                                        pass
                                test_source_corrections += 1
                                decide_correction(sequence, 'SOURCE_CREDENTIAL_DENIED', True, 'ALLOWED')
                                capture(sequence, 'SOURCE_CREDENTIAL_DENIED', True, report, tool_calls=response.tool_calls)
                                seen_calls.update(item.call_id for item in response.tool_calls
                                                  if isinstance(item.call_id, str) and item.call_id.strip())
                                from agents.runtime.test_guidance import PASSWORD_FIXTURE_GUIDANCE
                                instruction = ('The rejected test file was NOT written. Generate a new tool call. '
                                    'Do not embed password, token or hash literals in test source, dictionaries, '
                                    'default arguments or f-strings. Generate synthetic test passwords at runtime. '
                                    + PASSWORD_FIXTURE_GUIDANCE + ' '
                                    'Fix all reported locations and inspect the whole file, not only the first match. '
                                    'Do not print credentials. Use the existing requirements unchanged.') if test_write else (
                                    'No call from your previous response was executed: the source write held a '
                                    'credential-like literal. Re-issue the calls you still need. Do not embed password, '
                                    'token, secret key or hash literals in source files or tests; read configuration '
                                    'from environment variables, and in tests generate synthetic passwords at runtime '
                                    'with secrets.token_urlsafe and pass variables. Do not print credentials.')
                                from orchestrator.core.source_credentials import source_credential_locations
                                source_field = report.field_path.removeprefix('/') if call.name == 'edit_source_file' else 'content'
                                locations = source_credential_locations(arguments.get(source_field))
                                items.append({'role': 'user', 'content': json_text({
                                    'hostToolFeedback': {
                                        'code': 'SOURCE_CREDENTIAL_DENIED', 'toolName': call.name,
                                        'executed': False, 'correctionsRemaining': 0,
                                        'sourceRule': report.source_rule, 'sourceLine': report.source_line,
                                        'sourceIssues': locations['issues'],
                                        'sourceIssuesTruncated': locations['truncated'],
                                        'sourceIssuesCoverage': locations['coverage'],
                                        'instruction': instruction}})})
                                correct_test_source = True
                                break
                            if (call.name == 'write_test_file'
                                    and (self._role in (AgentRole.QA, AgentRole.SECURITY)
                                         or self._role is SingleAgentIdentity.SINGLE_AGENT
                                         and self._single_phase in (SinglePhase.QA, SinglePhase.SECURITY))):
                                from agents.runtime.test_write_precheck import inspect_test_write
                                issues = correction_rules.checked_rejection({
                                    'code': 'GENERATED_TESTS_INVALID', 'issues': inspect_test_write(arguments)})
                                if issues is not None and issues['issues']:
                                    failure = policy_error('TEST_SOURCE_INVALID', sequence=sequence,
                                                           tool_name=call.name, field_path='/content')
                                    if correct(sequence, 'GENERATED_TESTS_INVALID', issues=issues['issues'],
                                            report=failure.validation, stage='TOOL_ARGUMENTS',
                                            tool_name=call.name, tool_calls=response.tool_calls):
                                        seen_calls.update(item.call_id for item in response.tool_calls
                                                          if isinstance(item.call_id, str) and item.call_id.strip())
                                        correct_test_source = True
                                        break
                                    exhausted_qa_draft(failure.validation)
                            prepared.append((execution_call, definition, arguments))
                            batch_ids.add(call.call_id)
                    except LLMRuntimeError as rejected:
                        # Pre-execution only: nothing in this batch has run. Corrections are
                        # off unless llmLimits.max_output_corrections allows them.
                        kind = correction_rules.classify(rejected)
                        report = rejected.validation
                        if kind is not None and correct(
                                sequence, kind, issues=correction_rules.report_issues(report), report=report,
                                stage=getattr(report, 'stage', None), tool_name=getattr(report, 'tool_name', None),
                                tool_calls=response.tool_calls):
                            seen_calls.update(item.call_id for item in response.tool_calls
                                              if isinstance(item.call_id, str) and item.call_id.strip())
                            continue
                        if kind is None:
                            capture(sequence, 'TOOL_BATCH_REJECTED', False, report, tool_calls=response.tool_calls)
                        raise
                    if correct_test_source:
                        continue
                    # ACL/schema/credential checks above keep precedence. Only a valid,
                    # unexecuted batch that ignores the forced edit is no-progress.
                    if required_tool is not None and (
                            len(prepared) != 1 or prepared[0][0].name not in (required_tool, *required_alternatives)):
                        if required_tool == 'write_test_file':
                            decide_correction(sequence, 'GENERATED_TESTS_INVALID', False, 'REPEATED',
                                qa_exhausted=True, draft_issue='GENERATED_TEST_FILE_MISSING')
                            capture(sequence, 'GENERATED_TESTS_INVALID', False, tool_calls=response.tool_calls)
                            exhausted_qa_draft()
                        decide_correction(sequence, 'READY_WITHOUT_CHANGES', False, 'REPEATED',
                            observation=observed(response_kind='TOOL_CALLS',
                                                 requested=[call.name for call in response.tool_calls],
                                                 refusal='REQUIRED_TOOL_NOT_CALLED'))
                        capture(sequence, 'READY_WITHOUT_CHANGES', False, tool_calls=response.tool_calls)
                        raise LLMRuntimeError(LLMErrorCode.NO_PROGRESS)
                    continuation = parse_json(response.output_items_json, max_bytes=limit)
                    self._validate_continuation(continuation, response.tool_calls)
                    items.extend(continuation)
                    required_tool_succeeded = False
                    recovery_source_changed = False
                    check_written_paths = set()
                    changed_source_paths = set()
                    for call, definition, arguments in prepared:
                        tool_sequence, timeout = budget.reserve_tracked_tool_call()
                        seen_calls.add(call.call_id)
                        context = ToolContext(self._role, workspace_id, budget.deadline_monotonic)
                        try:
                            tool_result = await asyncio.wait_for(
                                self._executor.execute(call, arguments, context), timeout=timeout,
                            )
                        except asyncio.TimeoutError:
                            raise LLMRuntimeError(LLMErrorCode.TIMEOUT) from None
                        except Exception as failure:
                            # A read-only Tool that settled with a known rejection (file not
                            # found, report not found, ...) changed nothing. When corrections
                            # are on, tell the model instead of ending the step (handoff P5,
                            # point 3). Writes and uncertain results stay fail-closed.
                            code = correction_rules.returnable_tool_error(call.name, failure)
                            if code is None or not tool_errors.take(('TOOL_RESULT_ERROR', call.call_id)):
                                raise LLMRuntimeError(LLMErrorCode.TOOL_FAILED) from None
                            budget.account_tool_call(tool_sequence)
                            executed += 1
                            executed_names.append(call.name)
                            budget.check()
                            items.append({"type": "function_call_output", "call_id": call.call_id,
                                          "output": json_text(correction_rules.tool_error_output(
                                              call.name, code, tool_errors.remaining), max_bytes=limit)})
                            # A known, rejected write changed nothing. Bind the
                            # next read and write to this exact file; never replay
                            # an uncertain write or an entire partly completed batch.
                            if (len(prepared) == 1 and call.name in ('write_source_file', 'edit_source_file')
                                    and code in correction_rules.RETURNABLE_EDIT_ERRORS | {'WRITE_CONFLICT'}
                                    and isinstance(arguments.get('path'), str) and arguments['path'].startswith('source/')
                                    and {'read_project_file', 'write_source_file'} <= self._definitions.keys()):
                                source_repair_path, source_repair_sha256 = arguments['path'], None
                                source_repair_full_write |= code in ('EDIT_TARGET_MISSING', 'EDIT_TARGET_AMBIGUOUS')
                                required_tool, required_alternatives = 'read_project_file', ()
                                if read_history is not None:
                                    read_history.require_fresh_content(source_repair_path)
                            continue
                        budget.account_tool_call(tool_sequence)
                        executed += 1
                        executed_names.append(call.name)
                        budget.check()
                        validate_schema(definition.output_schema, tool_result,
                            stage='TOOL_RESULT', sequence=sequence, tool_name=definition.name)
                        if (source_write_precheck is not None and tool_result.get('changed') is True
                                and correction_rules.wrote_handler_registration(call.name, arguments)):
                            check_written_paths.add(arguments['path'])
                        changed_now = (
                            call.name in {'write_source_file', 'edit_source_file'} and tool_result.get('changed') is True
                            or call.name == 'apply_patch' and bool(tool_result.get('changedFiles')))
                        source_changed |= changed_now
                        if changed_now and call.name in {'write_source_file', 'edit_source_file'}:
                            changed_source_paths.add(arguments['path'])
                        recovery_source_changed |= changed_now
                        if changed_now and restricted_paths:
                            refresh_path_output = True
                        # Transcript copy is redacted, evidence assembly uses host data later.
                        tool_output = json_text(sanitize_content(
                            tool_result, source_fields=definition.source_output_fields,
                            sequence=sequence, tool_name=definition.name, boundary='TOOL_RESULT',
                        ), max_bytes=limit)
                        sanitized_result = parse_json(tool_output, max_bytes=limit)
                        validate_schema(definition.output_schema, sanitized_result,
                            stage='TOOL_RESULT_REDACTED', sequence=sequence, tool_name=definition.name)
                        if call.name == 'read_project_file' and arguments.get('path') == source_repair_path:
                            digest = sanitized_result.get('sha256')
                            if (not isinstance(digest, str) or len(digest) != 64
                                    or any(c not in '0123456789abcdef' for c in digest)):
                                raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
                            source_repair_sha256 = digest
                        if read_history is not None:
                            tool_output = read_history.for_model(call.name, arguments, sanitized_result,
                                                                 tool_output, call.call_id)
                        items.append({"type": "function_call_output", "call_id": call.call_id, "output": tool_output})
                        required_tool_succeeded |= call.name in (required_tool, *required_alternatives)
                    notices = (correction_rules.source_write_notice(source_write_precheck, check_written_paths),
                               source_preservation_check(tuple(sorted(changed_source_paths)))
                                   if source_preservation_check is not None and changed_source_paths else None,
                               read_history.completed_inspection() if read_history is not None else None)
                    for notice in filter(None, notices):
                        message = json_text(notice)
                        if message not in source_notices:
                            entry = {'role': 'user', 'content': message}
                            # Optional feedback must not make an otherwise valid conversation exceed the JSON bound.
                            occupied = len(json_text(items, max_bytes=limit).encode()) + len(prompt.system_prompt.encode())
                            if occupied + len(json_text([entry]).encode()) <= limit:
                                items.append(entry)
                                source_notices.add(message)
                    if required_tool == 'write_test_file':
                        # A settled write rejection must not satisfy the required write. After a
                        # successful write, READY rechecks actual files and bindings, not a call count.
                        if required_tool_succeeded:
                            required_tool = None
                            required_test_paths = ()
                    elif required_tool == 'read_project_file':
                        # A failed read must not count as having refreshed the file.
                        if required_tool_succeeded:
                            required_tool = 'write_source_file'
                            required_alternatives = (('edit_source_file',) if not source_repair_full_write and 'edit_source_file' in self._definitions
                                                     else ())
                    elif required_tool is not None:
                        if (source_status() is not SourceEditStatus.CHANGED
                                or source_repair_path is not None and not recovery_source_changed):
                            decide_correction(sequence, 'READY_WITHOUT_CHANGES', False, 'REPEATED',
                                observation=observed(response_kind='TOOL_CALLS',
                                                     requested=[call.name for call, _d, _a in prepared],
                                                     executed=True, refusal='NO_NET_SOURCE_CHANGE'))
                            raise LLMRuntimeError(LLMErrorCode.NO_PROGRESS)
                        required_tool = None
                        source_repair_path = source_repair_sha256 = None
                        source_repair_full_write = False
                        if source_review_pending:
                            source_repair_path = source_review_pending.pop(0)
                            required_tool, required_alternatives = 'read_project_file', ()
                            if read_history is not None:
                                read_history.require_fresh_content(source_repair_path)
                    continue
                if response.output_text is None:
                    raise LLMRuntimeError(LLMErrorCode.RESPONSE)
                # The provider receives the narrowed schema. Locally, retain the authoritative diff
                # rejection (including its retry key and audit trail) for a nonconforming provider.
                response_schema = base_output.schema if restricted_paths else output.schema
                try:
                    data = parse_candidate(response.output_text, response_schema, max_bytes=limit,
                                           stage='MODEL_OUTPUT', sequence=sequence)
                    validate_schema(response_schema, data, stage='MODEL_OUTPUT', sequence=sequence)
                    sanitized = redact_data(data)
                    validate_schema(response_schema, sanitized, stage='MODEL_OUTPUT_REDACTED', sequence=sequence)
                except LLMRuntimeError as rejected:
                    kind = correction_rules.classify(rejected)
                    report = rejected.validation
                    if kind is not None and correct(sequence, kind, issues=correction_rules.report_issues(report),
                                                    report=report, stage=getattr(report, 'stage', None),
                                                    output_text=response.output_text):
                        continue
                    if close_invalid_qa_draft and kind == 'MODEL_OUTPUT_INVALID':
                        exhausted_qa_draft(report)
                    if kind is None:
                        capture(sequence, 'MODEL_OUTPUT_REJECTED', False, report, output_text=response.output_text)
                    raise
                status = source_status() if development_rejection_check is not None or output_check is not None else None
                if development_rejection_check is not None:
                    from agents.llm.development_rejection import RejectionAssessment
                    try:
                        assessment = development_rejection_check(sanitized, status is SourceEditStatus.CHANGED)
                        if assessment is not None:
                            if type(assessment) is not RejectionAssessment:
                                raise ValueError('invalid rejection assessment')
                            assessment = RejectionAssessment.model_validate(assessment.public())
                    except Exception:
                        raise LLMRuntimeError(LLMErrorCode.CONFIGURATION) from None
                    if assessment is not None:
                        required_tool = None
                        last_development_rejection = assessment.public()
                        if assessment.classification == 'HUMAN_REVIEW':
                            publish_route(FailureRoute(sequence=sequence, operationSequence=operation_sequence,
                                code='DEVELOPER_REJECTION_HELD', category='HUMAN_DECISION',
                                action='RETURN_TO_VALIDATOR', reason='NOT_SAFE_TO_RETRY',
                                developmentRejection=assessment))
                            decided_sequence = sequence
                        else:
                            allowed = correct(sequence, 'DEVELOPER_REJECTION_RECOVERABLE',
                                issues=[{'fieldPath': '/rejection', 'reasonCode': 'DEVELOPMENT_REJECTION_REVIEW'}],
                                output_text=response.output_text, development_rejection=assessment)
                            if allowed:
                                continue
                            raise LLMRuntimeError(LLMErrorCode.DEVELOPMENT_BLOCKED)
                if output_check is not None:
                    # The caller's validator, run early. A rejection only buys one
                    # regeneration; the caller re-validates whatever is returned.
                    try:
                        checked = output_check(sanitized, correction_rules.ExecutedTools(
                            executed_names, source_changed=status is SourceEditStatus.CHANGED,
                            unused_rewrite=status is SourceEditStatus.UNUSED_TEXT_REWRITE))
                        rejection = correction_rules.checked_rejection(checked)
                        if checked is not None and rejection is None:
                            raise ValueError('invalid Host checker result')
                    except Exception:
                        # A broken Host validator cannot grant model acceptance or
                        # buy a generation attempt. Its owning stage must stop.
                        raise LLMRuntimeError(LLMErrorCode.CONFIGURATION) from None
                    if rejection is not None:
                        if rejection['code'] == 'QUESTIONS_NOT_ANSWERED' and development_rejection_check is not None:
                            required_tool = None  # A valid question keeps its existing bounded reminder/hold.
                        allowed = correct(sequence, rejection['code'], issues=rejection['issues'],
                                          output_text=response.output_text)
                        if allowed and correction_candidates is not None and rejection['code'] in (
                                'READY_PATH_EVIDENCE_MISMATCH', 'READY_RUNTIME_REPAIR_REQUIRED'):
                            restricted_paths.update(issue['fieldPath'].split('/')[2] for issue in rejection['issues']
                                                    if issue['reasonCode'] == 'NO_LINKED_CODE_CHANGE')
                            refresh_path_output = bool(restricted_paths)
                        if not allowed and rejection['code'] in ('READY_PATH_EVIDENCE_MISMATCH', 'READY_RUNTIME_REPAIR_REQUIRED',
                                                                 'READY_LIBRARY_TYPE_MISMATCH', 'READY_OUTPUT_SAFETY_REVIEW', 'READY_SIGNUP_QUALITY_REVIEW'):
                            raise LLMRuntimeError(LLMErrorCode.SOURCE_REPAIR)
                        if allowed and rejection['code'] == 'READY_SIGNUP_QUALITY_REVIEW':
                            paths = correction_rules.source_review_paths(rejection['issues'])
                            if paths and {'read_project_file', 'write_source_file'} <= self._definitions.keys():
                                source_repair_path, *source_review_pending = paths
                                source_repair_sha256 = None
                                required_tool, required_alternatives = 'read_project_file', ()
                                if read_history is not None:
                                    read_history.require_fresh_content(source_repair_path)
                        if rejection['code'] == 'READY_WITHOUT_CHANGES':
                            if not allowed:
                                raise LLMRuntimeError(LLMErrorCode.NO_PROGRESS)
                            if 'write_source_file' not in self._definitions:
                                raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
                            if ('read_project_file' in self._definitions and 'edit_source_file' in self._definitions
                                    and 'read_project_file' not in executed_names):
                                # Codex review 2026-11-19 #2 (replay c8x4dxb7): READY before reading anything
                                # left no way to edit (an edit needs a read in this step). One bounded read
                                # turn first; the change is required right after it.
                                required_tool, required_alternatives = 'read_project_file', ()
                            else:
                                required_tool = 'write_source_file'
                                # Codex review 2026-11-18 #2: a one-fragment security fix in a file that
                                # already passed build and QA should not need a full rewrite.
                                required_alternatives = (('edit_source_file',) if 'edit_source_file' in self._definitions
                                                         else ())
                        if allowed and missing_test_files(rejection['code'], rejection['issues']):
                            if 'write_test_file' not in self._definitions:
                                raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
                            required_tool = 'write_test_file'
                            required_alternatives = ()
                            required_test_paths = tuple(catalog.get('missingRequiredWritePaths', ()))
                        if not allowed and qa_draft_rejection(rejection['code'], rejection['issues']):
                            exhausted_qa_draft()
                        if rejection['code'] == 'CODE_REFERENCE_INVALID' and not allowed:
                            # A semantic model must never review or publish known-invalid
                            # read/coordinate evidence after its bounded correction ends.
                            raise LLMRuntimeError(LLMErrorCode.SECURITY_REFERENCE)
                        if allowed:
                            continue
                if restricted_paths:
                    # Even a semantically supported citation must have been offered on this turn.
                    # This final admission check never grants another correction to a faulty provider.
                    validate_schema(output.schema, sanitized, stage='MODEL_OUTPUT', sequence=sequence)
                budget.check()
                return LLMResult(json_text(sanitized, max_bytes=limit), tuple(records), executed, tuple(executed_names))
        except LLMRuntimeError as error:
            code, validation = error.code, error.validation
        except asyncio.CancelledError:
            raise
        except Exception:
            code, validation = LLMErrorCode.RESPONSE, None
        if (sequence is not None and operation_sequence is not None and decided_sequence != sequence
                and not recovery_sink_failed):
            publish_route(failure_route(LLMRuntimeError(code, validation=validation),
                                       sequence=sequence, operation_sequence=operation_sequence))
        recorded = False
        if validation is not None and validation_failed_sink is not None:
            try:
                recorded = validation_failed_sink(validation) is True
            except Exception:
                pass  # Preserve the original rejection and its A2A report.
        # Do not retain raw validation exceptions in __context__ or __cause__.
        raise LLMRuntimeError(code, records=tuple(records), validation=validation,
                              validation_recorded=recorded,
                              development_rejection=last_development_rejection
                              if code is LLMErrorCode.DEVELOPMENT_BLOCKED else None) from None

    @staticmethod
    def _usage_upper_bound(prompt, history, tools, output, output_cap):
        """Conservative UTF-8 wire allowance plus framing margin, not measured billing."""
        import json
        try:
            size = len(prompt.system_prompt.encode('utf-8')) + len(history.encode('utf-8')) + 4096
            for tool in tools:
                size += len(json.dumps(tool.input_schema.to_dict(), ensure_ascii=False).encode('utf-8'))
                size += len(tool.name.encode('utf-8')) + len(tool.description.encode('utf-8'))
            if output is not None:
                size += len(json.dumps(output.schema.to_dict(), ensure_ascii=False).encode('utf-8'))
            return TokenUsage(input_tokens=size, output_tokens=output_cap, total_tokens=size + output_cap)
        except Exception:
            return None

    @staticmethod
    async def _budget_allows_another_call(budget, input_tokens=0) -> bool:
        """Non-consuming check before deciding a re-send (Codex review 2026-11-17 #5).

        With a token cap, an unknown-usage failure without a recorded bound makes the budget
        refuse further calls; deciding a re-send then would record a retry that can never happen.
        A refusal caused only by another role's in-flight reservation is not exhaustion: wait for
        it within the Run deadline, exactly like a normal call (Codex review 2026-11-18 #1).
        """
        while True:
            try:
                budget.check_model_call()
                return True
            except LLMRuntimeError:
                if not budget.waits_for_pending_reservations(input_tokens=input_tokens):
                    return False
            try:
                await asyncio.sleep(min(RESERVATION_WAIT_SECONDS, budget.remaining_seconds()))
            except LLMRuntimeError:
                return False

    @staticmethod
    def _publish_usage(sink, record, budget):
        try:
            sink(record)
        except Exception:
            budget.invalidate()
            raise

    @staticmethod
    def _duration(started: float) -> int:
        return max(0, int((monotonic() - started) * 1000))

    @staticmethod
    def _validate_continuation(items, calls) -> None:
        """No System/User injection or hidden calls in a provider continuation."""
        if not isinstance(items, list) or not items:
            raise LLMRuntimeError(LLMErrorCode.RESPONSE)
        expected = {call.call_id: (call.name, call.arguments_json) for call in calls}
        found = set()
        item_ids = set()
        for item in items:
            if not isinstance(item, dict) or item.get("status") not in (None, "completed"):
                raise LLMRuntimeError(LLMErrorCode.RESPONSE)
            identity = item.get("id")
            if not isinstance(identity, str) or not identity.strip() or identity in item_ids:
                raise LLMRuntimeError(LLMErrorCode.RESPONSE)
            item_ids.add(identity)
            kind = item.get("type")
            if kind == "function_call":
                call_id = item.get("call_id")
                if (
                    set(item) - {"id", "type", "status", "call_id", "name", "arguments"}
                    or not isinstance(call_id, str) or call_id in found
                    or call_id not in expected
                    or (item.get("name"), item.get("arguments")) != expected[call_id]
                ):
                    raise LLMRuntimeError(LLMErrorCode.RESPONSE)
                found.add(call_id)
            elif kind == "message":
                if (
                    set(item) - {"id", "type", "status", "role", "content"}
                    or item.get("role") != "assistant" or not isinstance(item.get("content"), list)
                ):
                    raise LLMRuntimeError(LLMErrorCode.RESPONSE)
                for part in item["content"]:
                    if (
                        not isinstance(part, dict) or part.get("type") != "output_text"
                        or not isinstance(part.get("text"), str)
                        or set(part) - {"type", "text", "annotations", "logprobs"}
                    ):
                        raise LLMRuntimeError(LLMErrorCode.RESPONSE)
            elif kind == "reasoning":
                if (
                    set(item) - {"id", "type", "status", "summary", "encrypted_content"}
                    or not isinstance(item.get("summary"), list)
                    or item.get("encrypted_content") is not None and not isinstance(item["encrypted_content"], str)
                ):
                    raise LLMRuntimeError(LLMErrorCode.RESPONSE)
            else:
                raise LLMRuntimeError(LLMErrorCode.RESPONSE)
        if found != set(expected):
            raise LLMRuntimeError(LLMErrorCode.RESPONSE)
