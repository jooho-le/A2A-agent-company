"""Bounded draft repair using inert files and fake models, never a paid/product run."""
import asyncio
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from agents.llm.contracts import LLMRuntimeError
from agents.llm.corrections import case_expectation_check
from agents.llm.engine import LLMEngine
from agents.roles.qa_contract import build_qa_output_contract, validate_qa_decision
from agents.runtime.generated_tests_precheck import inspect_generated_tests
from agents.runtime.qa_catalog import case_repair_targets, refresh_qa_output
from orchestrator.domain import AgentRole
from test_llm_runtime import FakeProvider, FakeToolExecutor, LLMRuntimeTests as _RuntimeFixture, text_response


METHODS = ('page_loads', 'success', 'duplicate_email', 'empty_email', 'empty_password',
           'missing_email', 'missing_password', 'invalid_email_type', 'invalid_password_type')


def inventory(tmp_path):
    root = tmp_path / 'tests'
    root.mkdir()
    file = root / 'test_signup.py'
    # Importing this would fail. Host discovery must inspect AST only.
    file.write_text('from _host_service_test import ServiceTestCase\n'
                    'raise RuntimeError("NEVER_IMPORT_GENERATED_SOURCE")\n'
                    'class Signup(ServiceTestCase):\n' + ''.join(
                        f'    def test_{name}(self): pass\n' for name in METHODS))
    requirement = uuid4()
    selectors = {'run_unit_tests': ('qa-unit',)}
    output = build_qa_output_contract((requirement,), selectors)
    scopes = [('qa-unit', 'test_*.py')]
    _, catalog = refresh_qa_output(output, root, scopes, snapshot_sha256='a' * 64, unit_only=True)
    rows = [{'toolName': 'run_unit_tests', **row, 'requirementId': str(requirement),
             'title': 'Assigned input check', 'expectedResult': 'Approved criterion'}
            for row in catalog['cases']]
    return root, file, requirement, selectors, output, scopes, catalog, rows


def test_all_nine_file_methods_get_exact_missing_bindings_without_requirement_guesses(tmp_path):
    _root, file, _req, _selectors, _output, _scopes, catalog, rows = inventory(tmp_path)
    original = deepcopy(catalog)
    targets = case_repair_targets(catalog, json.dumps({'kind': 'READY', 'cases': rows[:-1]}))
    assert targets['requiredGeneratedCaseCount'] == 9 and targets['missingCaseCount'] == 1
    assert targets['missingCaseBindings'] == [{k: rows[-1][k] for k in ('toolName', 'selector', 'testId')}]
    assert targets['unexpectedCaseBindings'] == []
    assert catalog == original and len(file.read_text().split('def test_')) - 1 == 9
    assert not {'requirementId', 'expectedResult', 'outcome', 'content'} & targets['missingCaseBindings'][0].keys()
    assert 'NEVER_IMPORT_GENERATED_SOURCE' not in json.dumps(targets)


def test_security_existing_schema_needs_no_tool_name_and_reports_only_missing_rows(tmp_path):
    *_other, catalog, rows = inventory(tmp_path)
    security_rows = [{k: v for k, v in row.items() if k != 'toolName'} for row in rows]
    assert case_repair_targets(catalog, json.dumps({'kind': 'READY', 'testCases': security_rows}))['missingCaseCount'] == 0
    targets = case_repair_targets(catalog, json.dumps({'kind': 'READY', 'testCases': security_rows[1:]}))
    assert targets['missingCaseCount'] == 1
    assert targets['missingCaseBindings'][0]['testId'] == rows[0]['testId']
    assert 'do not add it to Security testCases' in targets['bindingInstruction']


def test_mixed_catalog_keeps_scope_and_tool_identity_and_never_guesses_unresolved_cases():
    catalog = {'cases': [{'selector': 'unit', 'testId': 'test_app.Checks.test_ok'}],
               'unresolvedSelectors': ['dynamic'],
               'browserCases': [{'selector': 'browser', 'testId': 'page.ready', 'path': 'outputs/qa/tests/suite.json'}]}
    rows = [{'toolName': 'run_unit_tests', 'selector': 'unit', 'testId': 'tests.test_app.Checks.test_ok'},
            {'toolName': 'run_unit_tests', 'selector': 'dynamic', 'testId': 'unknown.Check.test_one'}]
    targets = case_repair_targets(catalog, json.dumps({'kind': 'READY', 'cases': rows}))
    assert targets['missingCaseCount'] == 2
    assert targets['unexpectedCaseBindings'] == [rows[0]]
    assert {r['toolName'] for r in targets['missingCaseBindings']} == {'run_unit_tests', 'run_browser_tests'}
    assert 'unknown.Check.test_one' not in json.dumps(targets)


def test_repair_feedback_is_bounded_and_does_not_echo_other_candidate_fields():
    catalog = {'cases': [{'selector': 'unit', 'testId': f'test_checks.Check.test_{i:03}'} for i in range(100)]}
    targets = case_repair_targets(catalog, json.dumps({'kind': 'READY', 'cases': [], 'private': 'NEVER_ECHO_INPUT'}))
    assert targets['missingCaseCount'] == 100 and len(targets['missingCaseBindings']) == 32
    assert 'NEVER_ECHO_INPUT' not in json.dumps(targets)
    for candidate in (None, 'not-json', '{}', '{"kind":"INPUT_REQUIRED","cases":[]}',
                      json.dumps({'kind': 'READY', 'cases': [{}] * 257})):
        assert case_repair_targets(catalog, candidate) == {}


@pytest.mark.parametrize('repair', [True, False])
def test_engine_missing_binding_repairs_without_rewriting_source_or_raising_retry_cap(tmp_path, repair):
    root, file, requirement, selectors, output, scopes, _catalog, rows = inventory(tmp_path)
    before = file.read_bytes()
    fixture = _RuntimeFixture(); fixture.setUp()
    draft = {'kind': 'READY', 'questions': [], 'cases': rows}
    missing = {**draft, 'cases': rows[:-1]}

    def second(request):
        messages = json.loads(request.input_items_json)
        feedback = next(json.loads(row['content'])['hostCorrection'] for row in reversed(messages)
                        if 'hostCorrection' in row.get('content', ''))
        assert feedback['testRepairTargets']['missingCaseCount'] == 1
        assert feedback['testRepairTargets']['missingCaseBindings'][0]['testId'] == rows[-1]['testId']
        return text_response(json.dumps(draft if repair else missing))

    provider = FakeProvider(text_response(json.dumps(missing)), second)
    executor = FakeToolExecutor()
    budget = fixture.budget(max_output_corrections=1, max_model_calls=3)
    events = []

    async def run():
        return await LLMEngine(role=AgentRole.QA, provider=provider, tools=(), tool_executor=executor).run(
            prompt=fixture.prepared(AgentRole.QA), model=fixture.model, output=output,
            workspace_id=fixture.workspace_id, budget=budget, close_invalid_qa_draft=True,
            output_refresh=lambda wire: refresh_qa_output(wire, root, scopes,
                snapshot_sha256='a' * 64, unit_only=True), recovery_sink=events.append,
            output_check=case_expectation_check(lambda data: validate_qa_decision(data, (requirement,), selectors),
                extra_checks=(('GENERATED_TESTS_INVALID', lambda data:
                    inspect_generated_tests(root, scopes, data['cases'], 'cases')),)))

    if repair:
        assert asyncio.run(run()).data == draft
    else:
        with pytest.raises(LLMRuntimeError) as caught:
            asyncio.run(run())
        assert caught.value.code.value == 'LLM_VALIDATION_DRAFT_EXHAUSTED'
        assert events[-1].public()['reason'] == 'REPEATED'
    assert len(provider.requests) == budget.model_calls == 2
    assert executor.calls == [] and file.read_bytes() == before
