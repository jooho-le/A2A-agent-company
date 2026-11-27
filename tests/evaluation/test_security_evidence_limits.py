# Host의 JSON/증거 주소 한도를 넘겨도 근거 없는 확정 판정을 내거나 전체 응답을 잃지 않는지 확인한다.
import importlib
import json

import pytest

from evaluation.security import assess
from tests.evaluation.signup_examples import GOOD, GOOD_EXPECTED
from tests.evaluation.test_security_assess import make_request, outcomes

module = importlib.import_module('evaluation.security.assess')


@pytest.mark.parametrize('path', ['config.py', '검사' * 100 + '.py'])
def test_large_finding_list_keeps_requirement_results_and_matching_evidence(path):
    files = dict(GOOD)
    files[path] = ''.join(f'password_{i} = "synthetic-credential"\n' for i in range(600))
    findings = [(f'경고-{i}', {'path': path, 'line': i + 1, 'ruleId': 'B105'}) for i in range(600)]
    result, response = outcomes(files, findings=findings)
    evidence = json.loads(response.evidence_json)
    assert result == GOOD_EXPECTED
    assert len(response.evidence_json.encode('utf-8')) <= 65536
    selected = dict(response.finding_dispositions)
    assert 0 < len(selected) < len(findings)
    assert {row['findingId']: row['disposition'] for row in evidence['findings']} == selected
    assert evidence['omittedFindings'] == len(findings) - len(selected)
    assert all(value == 'CONFIRMED' for value in selected.values())


def test_oversized_requirement_evidence_becomes_explicit_unverified(monkeypatch):
    monkeypatch.setattr(module, 'MAX_EVIDENCE_BYTES', 100)
    result, response = outcomes(dict(GOOD))
    assert set(result.values()) == {'UNVERIFIED'}
    assert not response.evidence_refs
    assert json.loads(response.evidence_json)['error'] == 'EVIDENCE_LIMIT_EXCEEDED'
    assert len(response.evidence_json.encode('utf-8')) <= 100


def test_missing_refs_do_not_leave_pass_in_observation_json():
    request = make_request(dict(GOOD))
    object.__setattr__(request, 'permitted_evidence_refs', ())
    response = assess(request)
    assert response is not None
    evidence = json.loads(response.evidence_json)
    assert {row['outcome'] for row in evidence['requirements'].values()} == {'UNVERIFIED'}


def test_ref_limit_does_not_confirm_findings_without_their_file(monkeypatch):
    monkeypatch.setattr(module, 'MAX_REFS', 1)
    files = dict(GOOD, **{'zz_config.py': 'password = "synthetic-credential"\n'})
    _result, response = outcomes(files, findings=[('finding-1', {'path': 'zz_config.py', 'line': 1, 'ruleId': 'B105'})])
    assert len(response.evidence_refs) <= 1
    assert not response.finding_dispositions
    evidence = json.loads(response.evidence_json)
    assert evidence['findings'] == [] and evidence['omittedFindings'] == 1
    assert {row['outcome'] for row in evidence['requirements'].values()} == set(dict(response.requirement_outcomes).values())
