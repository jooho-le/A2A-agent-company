"""New API-only experiment policy and honest result categories; no paid Run."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys

import pytest

from test_scn001_smoke import smoke


def api_contract():
    return {'scenarioKey': 'SCN-001', 'externalContract': {'signupApi': {'method': 'POST'}}}


def test_new_api_run_does_not_inherit_an_unspecified_browser_ui():
    tools = {'browser': {'suites': [{'name': 'qa-browser', 'kind': 'QA_TESTS'}]},
             'unit': {'scopes': [{'kind': 'PROTECTED', 'name': 'practice'}]}, 'security': {'profiles': []}}
    before = deepcopy(tools)
    configured = smoke.api_only_tools(tools, api_contract())
    assert configured['browser'] is None
    assert configured['unit'] == tools['unit'] and configured['security'] == tools['security']
    assert tools == before


def test_protected_browser_suite_is_never_silently_removed():
    with pytest.raises(ValueError, match='SCN001_BROWSER_PROTECTED_POLICY_CONFLICT'):
        smoke.api_only_tools({'browser': {'suites': [{'kind': 'PROTECTED'}]}}, api_contract())


def test_declared_ui_requires_a_separate_explicit_policy():
    contract = api_contract()
    contract['externalContract']['ui'] = {'page': '/signup'}
    with pytest.raises(ValueError, match='SCN001_API_CONTRACT_REQUIRED'):
        smoke.api_only_tools({'browser': None}, contract)


def comparison(monkeypatch):
    monkeypatch.setitem(sys.modules, 'run_scn001_smoke', smoke)
    spec = importlib.util.spec_from_file_location('comparison_under_test',
        Path(__file__).resolve().parents[1] / 'scripts/run_scn001_comparison.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(*, product_pass=False, scored=True, failed=False, verdict='UNVERIFIED'):
    return {'condition': 'multi-ver1', 'runStatus': 'FINISHED', 'verdict': verdict, 'scored': scored,
            'accuracy': {'reason': None, 'productPass': product_pass, 'fullSatisfactionLower': .5} if scored else None,
            'checkOutcomes': [{'status': 'FAIL' if failed else 'PASS' if product_pass else 'UNVERIFIED'}] if scored else [],
            'metrics': {'costComplete': True, 'costUsd': .1, 'verdictMs': 1000,
                        'totalMs': 2000, 'fixAttempts': 1}}


def test_unverified_host_decision_time_is_not_a_success_time(monkeypatch):
    report = comparison(monkeypatch).means([row(product_pass=True)], 10)['multi-ver1']
    assert report['productPasses'] == 1 and report['hostSuccesses'] == 0
    assert report['productPassedWithoutHostSuccess'] == 1
    assert report['meanPassingVerdictSeconds'] is None and report['passingVerdictTimeSamples'] == 0


def test_product_failures_and_missing_evaluations_are_separate(monkeypatch):
    rows = [row(failed=True), row(scored=False), row(), row(product_pass=True, verdict='SUCCESS')]
    report = comparison(monkeypatch).means(rows, 10)['multi-ver1']
    assert report['executed'] == 4 and report['productPassRate'] == .25
    assert report['confirmedProductFailureRuns'] == 1
    assert report['evaluationUnavailableRuns'] == report['evaluationUnverifiedRuns'] == 1
    assert report['hostSuccesses'] == report['passingVerdictTimeSamples'] == 1
