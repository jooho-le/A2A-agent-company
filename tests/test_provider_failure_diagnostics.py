"""Actual REST adapter and Engine, using MockTransport only. No paid requests."""
import asyncio
from datetime import datetime, timezone
import json
from unittest.mock import patch
from uuid import uuid4

import httpx
from pydantic import SecretStr, ValidationError
import pytest

from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, TokenUsage
from agents.llm.engine import LLMEngine
from agents.llm.failure_diagnostics import FailureDiagnostic, retry_after_hint
from agents.llm.openai_responses import OpenAIResponsesProvider
from agents.roles.prompts import prepare_role_prompt
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.domain.states import AgentRole
from test_openai_responses import KEY, SECRET, make_request, response_payload


async def adapter_error(handler):
    provider = OpenAIResponsesProvider(SecretStr(KEY), transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(LLMRuntimeError) as caught:
            await provider.complete(make_request())
        error = caught.value
        public = json.dumps(error.public_failure())
        assert KEY not in public and SECRET not in public
        assert str(error) == error.code.value
        return error
    finally:
        await provider.aclose()


@pytest.mark.parametrize('status,code,kind,reason,retry', [
    (401, 'invalid_api_key', None, 'AUTHENTICATION_REJECTED', False),
    (403, None, None, 'PERMISSION_REJECTED', False),
    (429, 'insufficient_quota', None, 'QUOTA_EXHAUSTED', False),
    (429, 'credit_balance_exhausted', 'insufficient_quota', 'CREDIT_BALANCE_EXHAUSTED', False),
    (429, 'project_spend_limit_exceeded', 'insufficient_quota', 'SPEND_LIMIT_EXCEEDED', False),
    (429, 'organization_usage_limit_exceeded', 'insufficient_quota', 'USAGE_LIMIT_EXCEEDED', False),
    (429, 'rate_limit_exceeded', None, 'RATE_LIMITED', True),
    (429, 'slow_down', 'rate_limit_error', 'RATE_LIMITED', True),
    (429, None, None, 'HTTP_429_UNCLASSIFIED', True),
    (400, 'context_length_exceeded', None, 'REQUEST_REJECTED', False),
    (404, 'model_not_found', None, 'REQUEST_REJECTED', False),
    (302, None, None, 'REDIRECT_REJECTED', False),
    (500, None, None, 'SERVER_ERROR', True),
    (503, 'server_is_overloaded', 'service_unavailable_error', 'SERVER_ERROR', True),
])
def test_http_status_and_allowlisted_codes_survive_without_error_messages(status, code, kind, reason, retry):
    error = asyncio.run(adapter_error(lambda _request: httpx.Response(status, json={
        'error': {'code': code, 'type': kind, 'message': SECRET, 'param': SECRET}}, headers={'X-Private': KEY})))
    diagnostic = error.public_failure()
    assert diagnostic['httpStatus'] == status and diagnostic['reason'] == reason
    assert diagnostic.get('providerCode') == code
    assert error.transient is retry
    assert error.usage is None  # an HTTP error is not proof of zero billed work


@pytest.mark.parametrize('error_type,reason,known_zero,retry', [
    (httpx.ConnectError, 'CONNECT_ERROR', True, True),
    (httpx.ConnectTimeout, 'CONNECT_TIMEOUT', True, True),
    (httpx.PoolTimeout, 'POOL_TIMEOUT', True, True),
    (httpx.ReadTimeout, 'READ_TIMEOUT', False, False),
    (httpx.WriteTimeout, 'WRITE_TIMEOUT', False, False),
    (httpx.TimeoutException, 'NETWORK_TIMEOUT', False, False),
    (httpx.RemoteProtocolError, 'TRANSPORT_ERROR', False, False),
])
def test_transport_boundaries_keep_factual_usage_and_never_retry_uncertain_reads(error_type, reason, known_zero, retry):
    def fail(request):
        raise error_type(SECRET, request=request)
    error = asyncio.run(adapter_error(fail))
    assert error.public_failure()['boundary'] == 'TRANSPORT'
    assert error.public_failure()['reason'] == reason
    assert error.transient is retry
    assert error.usage == (TokenUsage(0, 0, 0) if known_zero else None)


@pytest.mark.parametrize('body', [SECRET, '{"error":{"code":"insufficient_quota","code":"slow_down"}}',
                                   SECRET * 1000, '{"error":{"code":NaN}}'])
def test_invalid_or_oversized_error_body_is_not_repaired_or_saved(body):
    error = asyncio.run(adapter_error(lambda _request: httpx.Response(429, text=body)))
    assert error.public_failure()['reason'] == 'HTTP_429_UNCLASSIFIED'
    assert 'providerCode' not in error.public_failure()


def test_unknown_code_is_not_copied_and_specific_billing_code_beats_broad_type():
    error = asyncio.run(adapter_error(lambda _request: httpx.Response(429, json={
        'error': {'code': SECRET, 'type': 'insufficient_quota', 'message': KEY}})))
    assert error.public_failure()['reason'] == 'QUOTA_EXHAUSTED'
    assert 'providerCode' not in error.public_failure() and not error.transient


@pytest.mark.parametrize('hint,expected', [('0.25', (250, False)), ('60', (60000, False)),
    ('61', (None, True)), ('9' * 100, (None, True)), ('NaN', (None, False)),
    ('-1', (None, False)), ('garbage', (None, False)), (None, (None, False))])
def test_retry_hint_is_bounded_and_never_becomes_untrusted_text(hint, expected):
    assert retry_after_hint(hint) == expected


def test_http_date_retry_hint_uses_a_bounded_relative_wait():
    now = datetime(2026, 11, 23, tzinfo=timezone.utc)
    assert retry_after_hint('Mon, 23 Nov 2026 00:00:03 GMT', now=now) == (3000, False)


async def engine_call(handler, *, timeout=1, runtime_ms=5000, retries=1):
    provider = OpenAIResponsesProvider(SecretStr(KEY), transport=httpx.MockTransport(handler))
    metadata = A2AWorkflowMetadata(run_id=uuid4(), workflow_step_id=uuid4(), scenario_id=uuid4(), attempt=0)
    prompt = prepare_role_prompt(AgentRole.PLANNER, task_input={'request': 'plan'}, metadata=metadata)
    budget = ExecutionBudget(runtime_budget_ms=runtime_ms, limits=LLMLimits(
        max_model_calls=3, max_provider_retries=retries, model_timeout_seconds=timeout))
    records, routes = [], []
    try:
        engine = LLMEngine(role=AgentRole.PLANNER, provider=provider)
        result = await engine.run(prompt=prompt, model=make_request().model, output=make_request().output,
            budget=budget, usage_sink=records.append, recovery_sink=routes.append)
        return result, None, records, routes, budget
    except LLMRuntimeError as error:
        return None, error, records, routes, budget
    finally:
        await provider.aclose()


@pytest.mark.parametrize('hint,quota,expected_calls', [('0.2', False, 2), ('61', False, 1),
                                                       ('2', True, 1), ('6', False, 1)])
def test_real_adapter_retry_preserves_error_then_success_without_exceeding_limits(hint, quota, expected_calls):
    calls, waited = [], []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={'Retry-After': hint}, json={'error': {
                'code': 'insufficient_quota' if quota else 'rate_limit_exceeded', 'message': SECRET}})
        return httpx.Response(200, json=response_payload())
    async def measured_sleep(seconds):
        waited.append(seconds)
    with patch('agents.llm.engine.asyncio.sleep', measured_sleep), patch('agents.llm.engine.PROVIDER_RETRY_DELAY_SECONDS', 0):
        result, error, records, routes, budget = asyncio.run(engine_call(handler))
    assert len(calls) == expected_calls and budget.model_calls == expected_calls
    assert records[0].failure_diagnostic.boundary == 'HTTP'
    assert records[0].usage is None and records[0].cost_usd is None
    if expected_calls == 2:
        assert error is None and result.data == {'summary': 'done'}
        assert waited == [0.2]
        assert records[1].usage.total_tokens == 24
        assert budget.total_tokens is None  # a later success cannot fill the first unknown usage
        retried = [r for r in routes if r.kind == 'DECISION' and r.action == 'REGENERATE']
        assert retried[0].public()['failureDiagnostic']['httpStatus'] == 429
    else:
        assert error.public_failure() == records[0].failure_diagnostic.public()
        assert waited == []
        assert routes[-1].public()['failureDiagnostic'] == error.public_failure()


def test_outer_model_deadline_is_identified_and_does_not_replay_an_uncertain_request():
    calls = []
    async def slow_handler(request):
        calls.append(request)
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=response_payload())
    _result, error, records, _routes, budget = asyncio.run(engine_call(slow_handler, timeout=0.03))
    assert error.code is LLMErrorCode.TIMEOUT
    assert error.public_failure()['boundary'] == 'MODEL_CALL'
    assert 0 < error.public_failure()['limitMs'] <= 30
    assert budget.model_calls == len(calls) == 1
    assert records[0].usage is None and records[0].failure_diagnostic == error.failure_diagnostic


def test_only_closed_facts_can_enter_error_and_usage_metadata():
    with pytest.raises(ValidationError):
        FailureDiagnostic(boundary='HTTP', reason='SERVER_ERROR', httpStatus=500, message=SECRET)
    valid = FailureDiagnostic(boundary='HTTP', reason='SERVER_ERROR', httpStatus=500)
    error = LLMRuntimeError(LLMErrorCode.PROVIDER, failure_diagnostic=valid)
    object.__setattr__(error.failure_diagnostic, 'reason', SECRET)
    assert error.public_failure() is None
    assert LLMRuntimeError(LLMErrorCode.AUTH, failure_diagnostic=valid).public_failure() is None


def test_a_read_timeout_is_not_blindly_replayed_even_when_one_retry_is_enabled():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout(SECRET, request=request)
    _result, error, records, _routes, budget = asyncio.run(engine_call(handler))
    assert error.public_failure()['reason'] == 'READ_TIMEOUT'
    assert len(calls) == budget.model_calls == 1
    assert records[0].usage is None and budget.total_tokens is None


def test_persistent_server_failure_stops_at_the_existing_single_retry():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(500, json={'error': {'message': SECRET}})
    async def no_wait(_seconds):
        pass
    with patch('agents.llm.engine.asyncio.sleep', no_wait):
        _result, error, records, _routes, budget = asyncio.run(engine_call(handler))
    assert len(calls) == budget.model_calls == 2
    assert len(records) == 2 and all(r.usage is None for r in records)
    assert error.public_failure()['httpStatus'] == 500


def test_run_deadline_stays_a_budget_failure_and_does_not_start_a_model_call():
    budget = ExecutionBudget(runtime_budget_ms=1000)
    with patch('agents.llm.budget.monotonic', return_value=budget._deadline + 1):
        with pytest.raises(LLMRuntimeError) as caught:
            budget.check()
    assert caught.value.code is LLMErrorCode.BUDGET
    assert caught.value.public_failure()['boundary'] == 'RUN_BUDGET'
    assert budget.model_calls == 0


def test_tool_timeout_is_distinct_from_the_model_call_and_never_replays_the_write():
    import test_llm_runtime as fixtures
    f = fixtures.LLMRuntimeTests()
    f.setUp()
    call = f.call('write-once', name='write_source_file', path='source/main.py', content='new implementation')
    provider = fixtures.FakeProvider(fixtures.tool_response(call), fixtures.text_response())
    async def slow_tool(*_args):
        await asyncio.sleep(0.2)
        return {'changed': True}
    executor = fixtures.FakeToolExecutor(slow_tool)
    definition = f.tool('write_source_file', input_schema=fixtures.object_schema({k: {'type': 'string'}
        for k in ('workspaceId', 'path', 'content')}), output_schema=fixtures.object_schema({'changed': {'type': 'boolean'}}),
        source_argument_fields=('content',))
    with pytest.raises(LLMRuntimeError) as caught:
        asyncio.run(f.run_engine(provider, tools=(definition,), executor=executor,
                                budget=f.budget(tool_timeout_seconds=0.02, max_provider_retries=1)))
    assert caught.value.code is LLMErrorCode.TIMEOUT
    assert caught.value.public_failure()['boundary'] == 'TOOL_CALL'
    assert len(executor.calls) == len(provider.requests) == 1
