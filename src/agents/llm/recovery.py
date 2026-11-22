"""Closed recovery routes shared by Single, Multi and semantic review.

These are Host records, never model verdicts. A regeneration replaces only an
unexecuted response, within the current engine context and Run budget. Product
build/test failures belong to the existing workflow fix loop, not this loop.
"""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.llm.contracts import LLMErrorCode
from agents.llm.development_rejection import RejectionAssessment

POLICY = 'BOUNDED_PRE_EXECUTION_REGENERATION_V1'
DEVELOPMENT_PROGRESS_POLICY = 'HOST_NET_DIFF_FOCUSED_RUNTIME_REPAIR_V6'
GENERATED_TEST_READY_POLICY = 'HOST_REQUIRED_TEST_FILE_WRITE_AND_EXHAUSTION_V1'
CORRECTION_CODES = frozenset({
    'TOOL_ARGUMENTS_INVALID', 'TOOL_REQUEST_REJECTED', 'MODEL_OUTPUT_INVALID',
    'OUTPUT_CONTRACT_INVALID', 'QUESTIONS_NOT_ANSWERED', 'READY_WITHOUT_CHANGES',
    'OUTPUT_LIMIT_REACHED', 'SEMANTIC_REVIEW_FORMAT_INVALID', 'SOURCE_CREDENTIAL_DENIED',
    'START_CONTRACT_PRECHECK_FAILED', 'PROVIDER_TRANSIENT_RETRY', 'CASE_EXPECTATION_INVALID',
    'GENERATED_TESTS_INVALID', 'CODE_REFERENCE_INVALID', 'DEVELOPER_REJECTION_RECOVERABLE',
    'DEVELOPER_REJECTION_HELD',
    'READY_PATH_EVIDENCE_MISMATCH', 'READY_PRIORITY_REPAIR_NEEDED', 'READY_RUNTIME_REPAIR_REQUIRED',
    'READY_LIBRARY_TYPE_MISMATCH', 'READY_OUTPUT_SAFETY_REVIEW', 'READY_SIGNUP_QUALITY_REVIEW',
})
CATEGORIES = Literal['MODEL_FORMAT', 'HOST_CONFIGURATION', 'HUMAN_DECISION',
                     'EXECUTION_UNCERTAIN', 'BUDGET_LIMIT', 'POLICY_BLOCK', 'MODEL_EXECUTION']


class _Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', populate_by_name=True, hide_input_in_errors=True)
    policy: Literal['BOUNDED_PRE_EXECUTION_REGENERATION_V1'] = POLICY
    sequence: int = Field(ge=1, strict=True)
    operation_sequence: int = Field(alias='operationSequence', ge=1, strict=True)

    def public(self):
        return self.model_dump(mode='json', by_alias=True)


class CallRoute(_Record):
    kind: Literal['CALL'] = 'CALL'
    mode: Literal['INITIAL', 'CONTINUATION', 'REGENERATION']
    correction_attempt: int = Field(alias='correctionAttempt', ge=0, le=5, strict=True)
    trigger_sequence: int | None = Field(default=None, alias='triggerSequence', ge=1, strict=True)
    code: str | None = None

    @model_validator(mode='after')
    def consistent(self):
        if self.operation_sequence > self.sequence:
            raise ValueError('invalid operation sequence')
        if self.mode == 'REGENERATION':
            if (self.code not in CORRECTION_CODES or self.trigger_sequence is None
                    or not self.operation_sequence <= self.trigger_sequence < self.sequence
                    or self.correction_attempt < 1):
                raise ValueError('invalid regeneration')
        elif self.code is not None or self.trigger_sequence is not None or self.correction_attempt != 0:
            raise ValueError('ordinary call is not a retry')
        if (self.mode == 'INITIAL') != (self.operation_sequence == self.sequence):
            raise ValueError('invalid initial call')
        return self


class CorrectionObservation(BaseModel):
    """What the Host required and what the model's response did, without any model text or arguments
    (Codex review 2026-11-19 #4: a replay showed two responses and no execution but could not tell a
    repeated READY from a refused tool request)."""
    required_tools: tuple[str, ...] = Field(default=(), alias='requiredTools', max_length=8)
    response_kind: Literal['FINAL_OUTPUT', 'TOOL_CALLS', 'EMPTY'] = Field(alias='responseKind')
    requested_tools: tuple[str, ...] = Field(default=(), alias='requestedTools', max_length=16)
    executed: bool = Field(strict=True)
    model_config = ConfigDict(frozen=True, extra='forbid', populate_by_name=True, hide_input_in_errors=True)
    refusal: Literal['FINAL_OUTPUT_INSTEAD_OF_REQUIRED_TOOL', 'REQUIRED_TOOL_NOT_CALLED', 'NO_NET_SOURCE_CHANGE',
                     'TOOL_BATCH_REJECTED', 'OUTPUT_REJECTED'] | None = None

    @model_validator(mode='after')
    def bounded_names(self):
        if any(type(name) is not str or not re.fullmatch(r'[a-z_]{1,64}', name)
               for name in (*self.required_tools, *self.requested_tools)):
            raise ValueError('invalid tool name')
        return self


def safe_source_ref(ref):
    from orchestrator.core.security import redact_text
    from orchestrator.domain.states import AgentRole
    from orchestrator.workspaces.policy import WorkspaceAccess, WorkspaceAccessError, authorize_path
    if not isinstance(ref, str) or len(ref) > 400 or redact_text(ref) != ref:
        return False
    path, sep, name = ref.partition('::')
    if not sep or not path.startswith('source/') or not path.endswith('.py'):
        return False
    try:
        authorize_path(AgentRole.DEVELOPER, path, WorkspaceAccess.READ)
    except WorkspaceAccessError:
        return False
    return name == '@module' or all(part.isidentifier() for part in name.split('.'))


def ready_comparison_reason(path, candidate_refs):
    if candidate_refs:
        return 'CITATION_NOT_AMONG_LINKED_CHANGES'
    return {'INVALID_INPUT_SERVER_ERROR': 'NO_LINK_TO_INVALID_INPUT',
            'RESPONSE_BEFORE_SIGNUP_HANDLER': 'NO_LINK_TO_PRE_ENTRY_RESPONSE'}.get(path, 'NO_LINK_TO_PATH')


class ReadyPathEvidence(BaseModel):
    """Bounded refs verified by Host, compared before any candidate is remeasured."""
    model_config = ConfigDict(frozen=True, extra='forbid', hide_input_in_errors=True)
    path: str = Field(pattern=r'^[A-Z][A-Z0-9_]{1,63}$')
    action: Literal['CORRECT_CITATION', 'EDIT_SOURCE_OR_REPORT_OPEN']
    comparisonReason: Literal['CITATION_NOT_AMONG_LINKED_CHANGES', 'NO_LINK_TO_INVALID_INPUT',
                              'NO_LINK_TO_PRE_ENTRY_RESPONSE', 'NO_LINK_TO_PATH']
    attemptedSourceRef: str | None = Field(default=None, exclude_if=lambda v: v is None)
    attemptedRefStatus: Literal['NOT_VERIFIED_IN_SOURCE'] | None = Field(default=None, exclude_if=lambda v: v is None)
    candidateRefs: tuple[str, ...] = Field(max_length=4)
    changedRefs: tuple[str, ...] = Field(max_length=4)
    unchangedRelatedFunctions: tuple[str, ...] = Field(default=(), max_length=4, exclude_if=lambda v: not v)
    unchangedRouteFunctions: tuple[str, ...] = Field(default=(), max_length=4, exclude_if=lambda v: not v)
    changedExceptionDecorators: tuple[str, ...] = Field(default=(), max_length=4, exclude_if=lambda v: not v)

    @model_validator(mode='after')
    def consistent(self):
        refs = (*self.candidateRefs, *self.changedRefs, *self.unchangedRelatedFunctions,
                *self.unchangedRouteFunctions, *self.changedExceptionDecorators)
        if self.attemptedSourceRef is not None:
            if self.attemptedRefStatus is not None:
                raise ValueError('unverified citation cannot retain model text')
            refs += (self.attemptedSourceRef,)
        if any(not safe_source_ref(ref) for ref in refs):
            raise ValueError('invalid source reference')
        if (bool(self.candidateRefs) != (self.action == 'CORRECT_CITATION') or
                self.comparisonReason != ready_comparison_reason(self.path, self.candidateRefs)):
            raise ValueError('inconsistent correction action')
        return self


class FailureRoute(_Record):
    kind: Literal['DECISION'] = 'DECISION'
    code: str
    category: CATEGORIES
    action: Literal['REGENERATE', 'RETURN_TO_VALIDATOR', 'STOP']
    reason: Literal['ALLOWED', 'DISABLED', 'REPEATED', 'LIMIT_REACHED', 'NOT_SAFE_TO_RETRY',
                    'HOST_CHECK_FAILED']
    development_rejection: RejectionAssessment | None = Field(default=None, alias='developmentRejection')
    draft_issue: Literal['GENERATED_TEST_FILE_MISSING'] | None = Field(default=None, alias='draftIssue')
    observation: CorrectionObservation | None = None
    unlinked_repair_paths: tuple[str, ...] = Field(default=(), alias='unlinkedRepairPaths',
                                                   max_length=16, exclude_if=lambda v: not v)
    repair_path_evidence: tuple[ReadyPathEvidence, ...] = Field(default=(), alias='repairPathEvidence',
        max_length=16, exclude_if=lambda v: not v)
    incomplete_reason: Literal['content_filter', 'max_messages', 'steered', 'other', 'unreported'] | None = Field(
        default=None, alias='incompleteReason', exclude_if=lambda v: v is None)

    def public(self):
        data = super().public()
        if self.development_rejection is None:
            data.pop('developmentRejection', None)
        if self.draft_issue is None:
            data.pop('draftIssue', None)
        if self.observation is None:
            data.pop('observation', None)
        return data

    @model_validator(mode='after')
    def consistent(self):
        if self.unlinked_repair_paths and (self.code not in (
                'READY_PATH_EVIDENCE_MISMATCH', 'READY_PRIORITY_REPAIR_NEEDED', 'READY_RUNTIME_REPAIR_REQUIRED') or
                any(not re.fullmatch(r'[A-Z][A-Z0-9_]{1,63}', path) for path in self.unlinked_repair_paths)):
            raise ValueError('invalid repair paths')
        if self.repair_path_evidence and (
                self.code not in ('READY_PATH_EVIDENCE_MISMATCH', 'READY_RUNTIME_REPAIR_REQUIRED') or
                any(row.path not in self.unlinked_repair_paths for row in self.repair_path_evidence)):
            raise ValueError('invalid repair path evidence')
        rejection_codes = {'DEVELOPER_REJECTION_RECOVERABLE', 'DEVELOPER_REJECTION_HELD'}
        if self.incomplete_reason is not None and (self.code != LLMErrorCode.INCOMPLETE.value or self.action != 'STOP'):
            raise ValueError('incomplete reason requires a stopped incomplete response')
        if (self.code in rejection_codes) != (self.development_rejection is not None):
            raise ValueError('rejection routing requires its assessment')
        if self.development_rejection is not None:
            held = self.development_rejection.classification == 'HUMAN_REVIEW'
            if (held != (self.code == 'DEVELOPER_REJECTION_HELD')
                    or held and (self.action != 'RETURN_TO_VALIDATOR' or self.category != 'HUMAN_DECISION')
                    or not held and self.category != 'MODEL_EXECUTION'):
                raise ValueError('invalid development rejection route')
        if self.operation_sequence > self.sequence or self.code not in CORRECTION_CODES | {c.value for c in LLMErrorCode}:
            raise ValueError('invalid failure route')
        if self.action == 'REGENERATE' and (self.code not in CORRECTION_CODES or self.reason != 'ALLOWED'):
            raise ValueError('unsafe regeneration')
        if self.action != 'REGENERATE' and self.reason == 'ALLOWED':
            raise ValueError('invalid decision')
        return self


def correction_route(code, *, sequence, operation_sequence, allowed, reason, development_rejection=None):
    # PROVIDER_TRANSIENT_RETRY re-sends the same request after a provider failure
    # that returned no model output; it is not a model mistake.
    category = ('HUMAN_DECISION' if code == 'QUESTIONS_NOT_ANSWERED'
                else 'EXECUTION_UNCERTAIN' if code == 'PROVIDER_TRANSIENT_RETRY'
                else 'MODEL_EXECUTION' if code in {'DEVELOPER_REJECTION_RECOVERABLE', 'READY_LIBRARY_TYPE_MISMATCH',
                                                  'READY_OUTPUT_SAFETY_REVIEW', 'READY_SIGNUP_QUALITY_REVIEW'}
                else 'MODEL_FORMAT')
    return FailureRoute(sequence=sequence, operationSequence=operation_sequence, code=code, category=category,
        action='REGENERATE' if allowed else ('RETURN_TO_VALIDATOR' if code in {
            'OUTPUT_CONTRACT_INVALID', 'SEMANTIC_REVIEW_FORMAT_INVALID', 'QUESTIONS_NOT_ANSWERED',
            'START_CONTRACT_PRECHECK_FAILED', 'CASE_EXPECTATION_INVALID',
            'GENERATED_TESTS_INVALID', 'READY_PRIORITY_REPAIR_NEEDED'} else 'STOP'),
        reason=reason, developmentRejection=development_rejection)


def failure_route(error, *, sequence, operation_sequence):
    """Final error routing, including the boundary that actually failed.

    A schema error after a Tool has executed is an integration failure, not a
    correctable argument. Provider/timeouts/unknown Tool outcomes are never
    blindly replayed. Authentication, policy holds and budget caps stay closed.
    """
    code = error.code
    stage = getattr(error.validation, 'stage', None)
    if code in (LLMErrorCode.CONFIGURATION, LLMErrorCode.AUTH) or stage in ('TOOL_RESULT', 'TOOL_RESULT_REDACTED'):
        category = 'HOST_CONFIGURATION'
    elif code in (LLMErrorCode.PROVIDER, LLMErrorCode.TIMEOUT, LLMErrorCode.TOOL_FAILED, LLMErrorCode.RESPONSE):
        category = 'EXECUTION_UNCERTAIN'
    elif code is LLMErrorCode.BUDGET:
        category = 'BUDGET_LIMIT'
    elif code in (LLMErrorCode.REFUSAL, LLMErrorCode.TOOL_POLICY):
        category = 'POLICY_BLOCK'
    else:
        category = 'MODEL_FORMAT'
    return FailureRoute(sequence=sequence, operationSequence=operation_sequence, code=code.value,
        category=category, action='STOP', reason='NOT_SAFE_TO_RETRY')
