"""Closed failure facts. Never retain a provider message, URL, header or body."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

POLICY = 'CLOSED_PROVIDER_FAILURE_AND_BOUNDED_SERVER_DELAY_V1'
MAX_ERROR_BYTES = 8192
MAX_RETRY_WAIT_MS = 60_000
PERMANENT_REASONS = frozenset({'QUOTA_EXHAUSTED', 'CREDIT_BALANCE_EXHAUSTED', 'SPEND_LIMIT_EXCEEDED',
                              'USAGE_LIMIT_EXCEEDED', 'AUTHENTICATION_REJECTED', 'PERMISSION_REJECTED',
                              'REQUEST_REJECTED', 'REDIRECT_REJECTED'})
ProviderCode = Literal['insufficient_quota', 'credit_balance_exhausted', 'billing_hard_limit_reached',
    'organization_spend_limit_exceeded', 'project_spend_limit_exceeded', 'organization_usage_limit_exceeded',
    'rate_limit_exceeded', 'slow_down', 'server_is_overloaded', 'invalid_api_key', 'model_not_found',
    'context_length_exceeded', 'invalid_request_error']
ProviderType = Literal['insufficient_quota', 'rate_limit_error', 'rate_limit_exceeded', 'invalid_request_error',
                       'server_error', 'service_unavailable_error', 'authentication_error', 'permission_error']
HTTP_REASONS = frozenset({'AUTHENTICATION_REJECTED', 'PERMISSION_REJECTED', 'QUOTA_EXHAUSTED',
    'CREDIT_BALANCE_EXHAUSTED', 'SPEND_LIMIT_EXCEEDED', 'USAGE_LIMIT_EXCEEDED', 'RATE_LIMITED',
    'HTTP_429_UNCLASSIFIED', 'REQUEST_REJECTED', 'SERVER_ERROR', 'REDIRECT_REJECTED', 'HTTP_REJECTED'})
TRANSPORT_REASONS = frozenset({'CONNECT_ERROR', 'CONNECT_TIMEOUT', 'POOL_TIMEOUT', 'READ_TIMEOUT',
                             'WRITE_TIMEOUT', 'NETWORK_TIMEOUT', 'TRANSPORT_ERROR'})


class FailureDiagnostic(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', populate_by_name=True,
                              hide_input_in_errors=True, revalidate_instances='always')
    policy: Literal['CLOSED_PROVIDER_FAILURE_AND_BOUNDED_SERVER_DELAY_V1'] = POLICY
    boundary: Literal['HTTP', 'TRANSPORT', 'MODEL_CALL', 'TOOL_CALL', 'RUN_BUDGET', 'RESPONSE']
    reason: Literal['AUTHENTICATION_REJECTED', 'PERMISSION_REJECTED', 'QUOTA_EXHAUSTED',
        'CREDIT_BALANCE_EXHAUSTED', 'SPEND_LIMIT_EXCEEDED', 'USAGE_LIMIT_EXCEEDED', 'RATE_LIMITED',
        'HTTP_429_UNCLASSIFIED', 'REQUEST_REJECTED', 'SERVER_ERROR', 'REDIRECT_REJECTED', 'HTTP_REJECTED',
        'CONNECT_ERROR', 'CONNECT_TIMEOUT', 'POOL_TIMEOUT', 'READ_TIMEOUT', 'WRITE_TIMEOUT',
        'NETWORK_TIMEOUT', 'TRANSPORT_ERROR', 'MODEL_CALL_TIMEOUT', 'TOOL_CALL_TIMEOUT',
        'RUN_DEADLINE_EXHAUSTED', 'INVALID_RESPONSE']
    http_status: int | None = Field(default=None, alias='httpStatus', ge=300, le=599, strict=True)
    provider_code: ProviderCode | None = Field(default=None, alias='providerCode')
    provider_type: ProviderType | None = Field(default=None, alias='providerType')
    retry_after_ms: int | None = Field(default=None, alias='retryAfterMs', ge=0, le=MAX_RETRY_WAIT_MS, strict=True)
    retry_after_exceeds_limit: bool = Field(default=False, alias='retryAfterExceedsLimit', strict=True)
    limit_ms: int | None = Field(default=None, alias='limitMs', ge=1, le=86_400_000, strict=True)

    @model_validator(mode='after')
    def consistent(self):
        if self.boundary == 'HTTP':
            if self.http_status is None or self.reason not in HTTP_REASONS or self.limit_ms is not None:
                raise ValueError('invalid HTTP failure facts')
            if self.reason == 'AUTHENTICATION_REJECTED' and self.http_status != 401:
                raise ValueError('invalid authentication status')
            if self.reason == 'PERMISSION_REJECTED' and self.http_status != 403:
                raise ValueError('invalid permission status')
            if self.reason in HTTP_REASONS - {'AUTHENTICATION_REJECTED', 'PERMISSION_REJECTED',
                    'REQUEST_REJECTED', 'SERVER_ERROR', 'REDIRECT_REJECTED', 'HTTP_REJECTED'} and self.http_status != 429:
                raise ValueError('invalid rate/quota status')
            if self.reason == 'SERVER_ERROR' and not 500 <= self.http_status < 600:
                raise ValueError('invalid server status')
            if self.reason == 'REDIRECT_REJECTED' and not 300 <= self.http_status < 400:
                raise ValueError('invalid redirect status')
            if (self.retry_after_ms is not None or self.retry_after_exceeds_limit) and not (
                    self.http_status == 429 or self.http_status >= 500):
                raise ValueError('unexpected retry hint')
        else:
            if any(value is not None for value in (self.http_status, self.provider_code, self.provider_type,
                                                   self.retry_after_ms)) or self.retry_after_exceeds_limit:
                raise ValueError('HTTP facts at a non-HTTP boundary')
            expected = {'MODEL_CALL': 'MODEL_CALL_TIMEOUT', 'TOOL_CALL': 'TOOL_CALL_TIMEOUT',
                        'RUN_BUDGET': 'RUN_DEADLINE_EXHAUSTED', 'RESPONSE': 'INVALID_RESPONSE'}
            if (self.boundary == 'TRANSPORT' and self.reason not in TRANSPORT_REASONS
                    or self.boundary in expected and self.reason != expected[self.boundary]):
                raise ValueError('invalid failure boundary')
        if self.retry_after_exceeds_limit and self.retry_after_ms is not None:
            raise ValueError('a capped hint is not a measured wait')
        return self

    def public(self):
        checked = type(self).model_validate(self.model_dump(by_alias=True), strict=True)
        result = checked.model_dump(mode='json', by_alias=True, exclude_none=True)
        if not result['retryAfterExceedsLimit']:
            result.pop('retryAfterExceedsLimit')
        return result


def compatible(diagnostic, code):
    if diagnostic.boundary == 'HTTP':
        return code == ('LLM_AUTH_REQUIRED' if diagnostic.http_status in (401, 403) else 'LLM_PROVIDER_ERROR')
    if diagnostic.boundary == 'TRANSPORT':
        return code == ('LLM_EXECUTION_TIMEOUT' if diagnostic.reason.endswith('TIMEOUT') else 'LLM_PROVIDER_ERROR')
    if diagnostic.boundary == 'RUN_BUDGET':
        return code == 'LLM_BUDGET_EXHAUSTED'
    return code == ('LLM_RESPONSE_INVALID' if diagnostic.boundary == 'RESPONSE' else 'LLM_EXECUTION_TIMEOUT')


def checked_diagnostic(value, *, code=None):
    if value is None:
        return None
    if type(value) is not FailureDiagnostic:
        raise ValueError('invalid failure diagnostic')
    checked = FailureDiagnostic.model_validate(value.model_dump(by_alias=True), strict=True)
    if code is not None and not compatible(checked, code):
        raise ValueError('failure diagnostic does not match its code')
    return checked


def safe_diagnostic(value, *, code):
    """An invalid optional diagnostic must not erase factual usage or the main error."""
    try:
        return checked_diagnostic(value, code=code)
    except Exception:
        return None


def timeout_diagnostic(boundary, seconds=None):
    limit = None
    if type(seconds) in (int, float) and math.isfinite(seconds) and 0 < seconds <= 86400:
        limit = max(1, math.ceil(seconds * 1000))
    return FailureDiagnostic(boundary=boundary, reason={
        'MODEL_CALL': 'MODEL_CALL_TIMEOUT', 'TOOL_CALL': 'TOOL_CALL_TIMEOUT',
        'RUN_BUDGET': 'RUN_DEADLINE_EXHAUSTED'}[boundary], limitMs=limit)


def provider_retry_delay(diagnostic, fallback_seconds):
    """Never shorten a valid server hint or extend the fixed retry wait bound."""
    checked = checked_diagnostic(diagnostic)
    if checked is not None and checked.retry_after_exceeds_limit:
        return None
    hint = checked.retry_after_ms / 1000 if checked is not None and checked.retry_after_ms is not None else 0.0
    delay = max(hint, fallback_seconds)
    return delay if math.isfinite(delay) and 0 <= delay * 1000 <= MAX_RETRY_WAIT_MS else None


def retry_after_hint(value, *, now=None):
    """Only a bounded numeric delay leaves this parser; never retain a header."""
    if value is None:
        return None, False
    if not isinstance(value, str):
        return None, False
    if len(value) > 128:
        return None, True
    text = value.strip()
    try:
        if re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', text):
            seconds = float(text)
        else:
            date = parsedate_to_datetime(text)
            if date.tzinfo is None:
                return None, False
            seconds = max(0.0, (date - (now or datetime.now(timezone.utc))).total_seconds())
        if not math.isfinite(seconds) or seconds * 1000 > MAX_RETRY_WAIT_MS:
            return None, True
        return math.ceil(seconds * 1000), False
    except (TypeError, ValueError, OverflowError):
        return None, False


def http_diagnostic(status, content, retry_after=None):
    # Lazy import keeps the closed model usable from contracts.py without a cycle.
    from agents.llm.contracts import parse_json
    from typing import get_args
    error = {}
    if isinstance(content, bytes) and len(content) <= MAX_ERROR_BYTES:
        try:
            payload = parse_json(content.decode('utf-8'), max_bytes=MAX_ERROR_BYTES)
            candidate = payload.get('error') if type(payload) is dict else None
            if type(candidate) is dict:
                error = candidate
        except Exception:
            pass
    code = error.get('code')
    code = code if type(code) is str and code in get_args(ProviderCode) else None
    kind = error.get('type')
    kind = kind if type(kind) is str and kind in get_args(ProviderType) else None
    if status == 401:
        reason = 'AUTHENTICATION_REJECTED'
    elif status == 403:
        reason = 'PERMISSION_REJECTED'
    elif status == 429:
        reason = ({'credit_balance_exhausted': 'CREDIT_BALANCE_EXHAUSTED',
            'billing_hard_limit_reached': 'SPEND_LIMIT_EXCEEDED',
            'organization_spend_limit_exceeded': 'SPEND_LIMIT_EXCEEDED',
            'project_spend_limit_exceeded': 'SPEND_LIMIT_EXCEEDED',
            'organization_usage_limit_exceeded': 'USAGE_LIMIT_EXCEEDED',
            'insufficient_quota': 'QUOTA_EXHAUSTED', 'rate_limit_exceeded': 'RATE_LIMITED',
            'slow_down': 'RATE_LIMITED'}.get(code)
            or ('QUOTA_EXHAUSTED' if kind == 'insufficient_quota' else
                'RATE_LIMITED' if code in ('rate_limit_exceeded', 'slow_down') or kind in (
                    'rate_limit_error', 'rate_limit_exceeded') else 'HTTP_429_UNCLASSIFIED'))
    elif 500 <= status < 600:
        reason = 'SERVER_ERROR'
    elif 300 <= status < 400:
        reason = 'REDIRECT_REJECTED'
    else:
        reason = 'REQUEST_REJECTED' if 400 <= status < 500 else 'HTTP_REJECTED'
    hint, too_long = retry_after_hint(retry_after) if status == 429 or status >= 500 else (None, False)
    return FailureDiagnostic(boundary='HTTP', reason=reason, httpStatus=status, providerCode=code,
                             providerType=kind, retryAfterMs=hint, retryAfterExceedsLimit=too_long)
