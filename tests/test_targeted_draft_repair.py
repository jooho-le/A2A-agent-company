"""The real bounded engine targets the missing file before accepting READY.

Synthetic provider/tools only; generated browser JSON is parsed, never launched.
"""
import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest

from agents.llm.contracts import LLMRuntimeError
from agents.llm.corrections import case_expectation_check
from agents.llm.engine import LLMEngine
from agents.roles.qa_contract import build_qa_output_contract, validate_qa_decision
from agents.runtime.generated_tests_precheck import inspect_generated_browser_tests, inspect_generated_tests
from agents.runtime.qa_catalog import refresh_qa_output
from orchestrator.domain.states import AgentRole
from test_llm_runtime import FakeProvider, FakeToolExecutor, LLMRuntimeTests as _RuntimeFixture, object_schema, text_response, tool_response


def exercise(root, mode):
    fixture = _RuntimeFixture()
    fixture.setUp()
    test_root = root / 'outputs/qa/tests'
    test_root.mkdir(parents=True)
    (test_root / 'test_sample.py').write_text('import unittest\nclass Tests(unittest.TestCase):\n def test_ok(self): pass\n')
    requirement = uuid4()
    selectors = {'run_unit_tests': ('qa-unit',), 'run_browser_tests': ('qa-browser',)}
    output = build_qa_output_contract((requirement,), selectors)
    scope = [('qa-unit', 'test_*.py')]
    browser = [('qa-browser', 'tests/browser/suite.json')]
    path = 'outputs/qa/tests/browser/suite.json'
    draft = {'kind': 'READY', 'questions': [], 'cases': [
        {'toolName': tool, 'selector': selector, 'testId': identifier, 'requirementId': str(requirement),
         'title': 'Approved check', 'expectedResult': 'Approved criterion'}
        for tool, selector, identifier in (
            ('run_unit_tests', 'qa-unit', 'test_sample.Tests.test_ok'),
            ('run_browser_tests', 'qa-browser', 'application.ready'))]}
    ready = text_response(json.dumps(draft))
    suite = {'format': 'browser-suite-v1', 'tests': [{'testId': 'application.ready',
             'steps': [{'action': 'goto', 'path': '/'}, {'action': 'assert_visible', 'selector': 'body'}]}]}
    call = fixture.call(name='write_test_file', path=path if mode != 'wrong_file' else 'outputs/qa/tests/test_other.py',
                        content=json.dumps(suite))
    if mode == 'repeat_ready':
        actions = [ready, ready]
    elif mode == 'wrong_file':
        actions = [ready, tool_response(call), tool_response(fixture.call('call-2', name='write_test_file',
            path='outputs/qa/tests/test_other.py', content=json.dumps(suite)))]
    else:
        actions = [ready, tool_response(call), ready]
    provider = FakeProvider(*actions)

    def write(_call, args, _context):
        target = root / args['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args['content'])
        return {'answer': 'written'}

    executor = FakeToolExecutor(write)
    tool = fixture.tool('write_test_file', input_schema=object_schema({
        'workspaceId': {'type': 'string'}, 'path': {'type': 'string'}, 'content': {'type': 'string'}}),
        source_argument_fields=('content',))
    events = []

    def issues(data):
        unit_rows = [r for r in data['cases'] if r['toolName'] == 'run_unit_tests']
        return (inspect_generated_tests(test_root, scope, unit_rows, 'cases')
                + inspect_generated_browser_tests(test_root.parent, browser, data['cases'], 'cases'))

    async def run():
        engine = LLMEngine(role=AgentRole.QA, provider=provider, tools=(tool,), tool_executor=executor)
        return await engine.run(prompt=fixture.prepared(AgentRole.QA), model=fixture.model, output=output,
            workspace_id=fixture.workspace_id,
            output_refresh=lambda wire: refresh_qa_output(wire, test_root, scope,
                snapshot_sha256='a' * 64, unit_only=False, browser_suites=browser),
            output_check=case_expectation_check(lambda data: validate_qa_decision(data, (requirement,), selectors),
                extra_checks=(('GENERATED_TESTS_INVALID', issues),)),
            close_invalid_qa_draft=True, recovery_sink=events.append,
            budget=fixture.budget(max_output_corrections=2, max_model_calls=6, max_tool_calls=4))

    return run, provider, executor, events, draft, path


def test_missing_browser_is_written_at_the_exact_host_path(tmp_path):
    run, provider, executor, _events, draft, path = exercise(tmp_path, 'fixed')
    assert asyncio.run(run()).data == draft
    assert len(executor.calls) == 1
    wire = provider.requests[1].tools[0].input_schema.to_dict()
    assert wire['properties']['path']['enum'] == [path]


def test_unrelated_file_write_cannot_satisfy_required_browser_write(tmp_path):
    run, _provider, executor, _events, _draft, _path = exercise(tmp_path, 'wrong_file')
    with pytest.raises(LLMRuntimeError):
        asyncio.run(run())
    assert executor.calls == []


def test_repeated_ready_without_the_file_still_stops_with_evidence(tmp_path):
    run, provider, executor, events, _draft, _path = exercise(tmp_path, 'repeat_ready')
    with pytest.raises(LLMRuntimeError):
        asyncio.run(run())
    assert len(provider.requests) == 2
    assert executor.calls == []
    assert events[-1].public()['code'] == 'LLM_VALIDATION_DRAFT_EXHAUSTED'
