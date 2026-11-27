"""A status code alone cannot locate which layer handled a request."""
import json
import sys
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel

from agents.runtime.developer_input import _case_feedback
from evaluation.check_details import _validated
from orchestrator.domain.validation_feedback import observation_json


class Quantity(BaseModel):
    number: int


@pytest.fixture
def observe(monkeypatch):
    path = Path(__file__).resolve().parents[2] / 'src/evaluation/protected_tests/tests/signup'
    monkeypatch.syspath_prepend(str(path))
    from signup_security_checks import observed_signup
    return observed_signup


@pytest.mark.skipif(not hasattr(sys, 'monitoring'), reason='Optional CPython observation')
@pytest.mark.parametrize('asynchronous', [False, True])
def test_same_status_has_two_distinct_measured_routes(observe, asynchronous):
    entries = []
    def sync_handler(body: Quantity):
        entries.append(body.number)
        return JSONResponse({}, status_code=422)
    async def async_handler(body: Quantity):
        return sync_handler(body)
    router = APIRouter()
    router.post('/signup')(async_handler if asynchronous else sync_handler)
    app = FastAPI()
    app.include_router(router, prefix='/api/auth')
    with TestClient(app) as client:
        def post(payload):
            return observe(lambda: client.post('/api/auth/signup', json=payload).status_code)
        assert post({'number': 'one'}) == (422, 'NOT_ENTERED')
        assert post({'number': 1}) == (422, 'ENTERED')
        assert post({}) == (422, 'NOT_ENTERED')
        assert observe(lambda: client.post('/unregistered').status_code) == (404, 'UNKNOWN')
    assert entries == [1]


def test_unsupported_observer_does_not_change_request_result(observe, monkeypatch):
    monkeypatch.delattr(sys, 'monitoring', raising=False)
    calls = []
    assert observe(lambda: calls.append(True) or 422) == (422, 'UNKNOWN')
    assert calls == [True]


@pytest.mark.skipif(not hasattr(sys, 'monitoring'), reason='Optional CPython observation')
def test_observer_cleanup_on_request_error(observe):
    before = [sys.monitoring.get_tool(i) for i in (3, 4)]
    def broken():
        raise LookupError()
    with pytest.raises(LookupError):
        observe(broken)
    assert [sys.monitoring.get_tool(i) for i in (3, 4)] == before


def test_optional_entry_survives_validated_detail_receipt_and_model_projection():
    definition = dict(caseId='sample', itemId='response_status', kind='precondition',
                      expected={'operator': 'range', 'value': [100, 599]})
    row = dict(definition, status='PASS', actual=422, reason=None)
    for marker in ('ENTERED', 'NOT_ENTERED', 'UNKNOWN', None):
        detail, = _validated([{**row, **({'handlerEntry': marker} if marker else {})}], [definition])
        wire = {k: v for k, v in detail.items() if k != 'expected'}
        saved = json.loads(observation_json([wire]))
        assert _case_feedback(saved)['sample'] == {
            'confirmedConditions': {'response_status': 422}, 'handlerEntry': marker or 'UNKNOWN'}
    for invalid in ({'handlerEntry': 'arbitrary text'}, {'handlerEntry': 'ENTERED', 'kind': 'assertion'}):
        with pytest.raises(ValueError):
            observation_json([{**wire, **invalid}])
        with pytest.raises(ValueError):
            _validated([{**row, **invalid}], [definition])
