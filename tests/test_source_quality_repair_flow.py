"""Actual source changes through bounded engine turns, without a paid provider.

These checks prove the repair protocol, not real-model signup success rates.
Generated application code is never imported on the Host.
"""
import asyncio
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from agents.core.identity import SingleAgentIdentity, SinglePhase
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.llm.corrections import development_check
from agents.llm.engine import LLMEngine
from agents.roles.developer_contract import build_developer_output_contract, validate_developer_decision
from agents.runtime.signup_quality_precheck import signup_quality_precheck_for
from agents.single.contracts import build_single_draft_contract
from agents.single.prompts import PreparedSinglePrompt, build_single_system_prompt
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario
import test_llm_runtime as runtime_fixtures
from test_llm_runtime import (FakeProvider, FakeToolExecutor,
                             object_schema, text_response, tool_response)
from test_signup_quality_precheck import APP, SAFE_HANDLER


@pytest.mark.parametrize('phase', [None, SinglePhase.IMPLEMENTING, SinglePhase.FIXING],
                         ids=['multi', 'single-implementing', 'single-fixing'])
@pytest.mark.parametrize('mode', ['fixed', 'partial_then_fixed', 'noop', 'wrong_path', 'budget', 'uncertain'])
def test_warning_requires_read_hash_bound_edit_and_real_recheck(tmp_path, phase, mode):
    f = runtime_fixtures.LLMRuntimeTests()
    f.setUp()
    path = tmp_path / 'main.py'
    path.write_text(APP)
    ready = {'kind': 'READY', 'summary': 'Candidate for measured validation.', 'rejection': None, 'questions': []}
    changes = [APP + '\nmarker = 1\n', APP + SAFE_HANDLER] if mode == 'partial_then_fixed' else [APP + SAFE_HANDLER]
    actions = [text_response(json.dumps(ready))]
    for index, content in enumerate(changes):
        def read_response(_request, index=index):
            return tool_response(f.call(f'read-{index}', path='source/other.py' if mode == 'wrong_path'
                                        else 'source/main.py'))
        def write_response(_request, index=index, content=content):
            return tool_response(f.call(f'write-{index}', name='write_source_file', path='source/main.py',
                content=content, expectedSha256=sha256(path.read_bytes()).hexdigest()))
        actions.extend([read_response, write_response, text_response(json.dumps(ready))])
    provider, events = FakeProvider(*actions), []

    def source_tool(call, arguments, _context):
        assert arguments['path'] == 'source/main.py'
        if call.name == 'read_project_file':
            return {'content': path.read_text(), 'sha256': sha256(path.read_bytes()).hexdigest()}
        assert arguments['expectedSha256'] == sha256(path.read_bytes()).hexdigest()
        if mode == 'uncertain':
            class UnknownWrite(Exception):
                error_kind = 'WRITE_CONFLICT'
                result_unknown = True
            raise UnknownWrite()
        if mode == 'noop':
            return {'changed': False}
        path.write_text(arguments['content'])
        return {'changed': True}

    executor = FakeToolExecutor(*([source_tool] * 4))
    tools = (f.tool('read_project_file', output_schema=object_schema({
        'content': {'type': 'string'}, 'sha256': {'type': 'string'}}), source_output_fields=('content',)),
        f.tool('write_source_file', input_schema=object_schema({k: {'type': 'string'} for k in
            ('workspaceId', 'path', 'content', 'expectedSha256')}),
            output_schema=object_schema({'changed': {'type': 'boolean'}}), source_argument_fields=('content',)))
    scenario = get_scenario(SCN_001_ID)
    if phase is None:
        role, prompt, output, validate = f.role, f.prompt, build_developer_output_contract(), validate_developer_decision
    else:
        role = SingleAgentIdentity.SINGLE_AGENT
        draft = build_single_draft_contract(phase, scenario=scenario)
        prompt = PreparedSinglePrompt(phase, draft, build_single_system_prompt(), json.dumps({
            'trusted': {'phase': phase.value}, 'contextId': 'offline', 'history': [], 'taskInput': {}}), 'offline')
        output, validate = draft.output, draft.validate
    probe = signup_quality_precheck_for(SimpleNamespace(source_root=tmp_path), scenario.planner_contract())
    engine = LLMEngine(role=role, single_phase=phase, provider=provider, tools=tools, tool_executor=executor)
    budget = f.budget(max_output_corrections=1 if mode == 'wrong_path' else 2,
                      max_model_calls=3 if mode == 'budget' else 8, max_tool_calls=6)
    operation = engine.run(prompt=prompt, model=f.model, workspace_id=f.workspace_id, output=output,
        source_progress_check=lambda: True, output_check=development_check(validate, signup_quality_check=probe),
        recovery_sink=events.append, budget=budget)
    if mode in ('fixed', 'partial_then_fixed'):
        assert asyncio.run(operation).data == ready
        assert path.read_text() == APP + SAFE_HANDLER
        assert probe() == []
        for offset in range(len(changes)):
            request = provider.requests[1 + 3 * offset]
            assert request.required_tool == 'read_project_file'
            assert len(request.tools) == 1
            assert request.tools[0].input_schema.to_dict()['properties']['path']['enum'] == ['source/main.py']
        assert budget.model_calls == (7 if mode == 'partial_then_fixed' else 4)
        assert sum(e.public().get('action') == 'REGENERATE' for e in events) == len(changes)
    else:
        with pytest.raises(LLMRuntimeError) as caught:
            asyncio.run(operation)
        assert caught.value.code is {'noop': LLMErrorCode.NO_PROGRESS,
                                     'wrong_path': LLMErrorCode.SCHEMA,
                                     'budget': LLMErrorCode.SOURCE_REPAIR,
                                     'uncertain': LLMErrorCode.TOOL_FAILED}[mode]
        assert len(executor.calls) == (2 if mode in ('noop', 'uncertain') else 0)
        assert probe()


def test_router_warning_repairs_app_entrypoint_and_revision_tracks_actual_source(tmp_path):
    from test_signup_quality_precheck import inspect
    from agents.llm.corrections import checked_rejection, source_review_paths
    files = {'main.py': 'from fastapi import FastAPI\nfrom routes import router\napp=FastAPI()\napp.include_router(router)\n',
             'routes.py': 'from fastapi import APIRouter\nfrom schema import Payload\nrouter=APIRouter()\n'
                          '@router.post("/api/auth/signup")\ndef signup(payload: Payload): return {}\n',
             'schema.py': 'from pydantic import BaseModel\nclass Payload(BaseModel):\n email:str\n password:str\n'}
    first = inspect(tmp_path, files)
    first = checked_rejection({'code': 'READY_SIGNUP_QUALITY_REVIEW', 'issues': first})['issues']
    assert first[0]['sourcePath'] == 'routes.py'
    assert source_review_paths(first) == ('source/main.py',)
    files['main.py'] += '\nmarker=1\n'
    second = inspect(tmp_path, files)
    assert first[0]['reviewRevision'] != second[0]['reviewRevision']


def test_closed_review_targets_cannot_escape_source_capability():
    from agents.llm.corrections import source_review_paths
    assert source_review_paths([{'repairSourcePath': p} for p in
        ('../private.py', '/tmp/private.py', 'x/../../private.py', 'source/../private.py')]) == ()


def test_multiple_review_files_share_one_correction_and_keep_exact_caps(tmp_path):
    f = runtime_fixtures.LLMRuntimeTests()
    f.setUp()
    source = APP + '\nfrom storage import save\n'
    storage = ('import logging\ndef save(db):\n    try:\n        db.commit()\n'
               '    except Exception as exc:\n        logging.exception(exc)\n')
    (tmp_path / 'main.py').write_text(source)
    (tmp_path / 'storage.py').write_text(storage)
    replacements = {'main.py': source + SAFE_HANDLER,
                    'storage.py': storage.replace('logging.exception(exc)', 'logging.error("STORAGE_FAILED")')}
    ready = {'kind': 'READY', 'summary': 'Candidate for measurement.', 'questions': [], 'rejection': None}
    actions = [text_response(json.dumps(ready))]
    for name in replacements:
        actions.append(tool_response(f.call('read-' + name, path='source/' + name)))
        def write_response(_request, name=name):
            return tool_response(f.call('write-' + name, name='write_source_file', path='source/' + name,
                content=replacements[name], expectedSha256=sha256((tmp_path / name).read_bytes()).hexdigest()))
        actions.append(write_response)
    actions.append(text_response(json.dumps(ready)))
    calls = []
    def execute(call, arguments, _context):
        calls.append((call.name, arguments['path']))
        path = tmp_path / arguments['path'].removeprefix('source/')
        if call.name == 'read_project_file':
            return {'content': path.read_text(), 'sha256': sha256(path.read_bytes()).hexdigest()}
        assert arguments['expectedSha256'] == sha256(path.read_bytes()).hexdigest()
        path.write_text(arguments['content'])
        return {'changed': True}
    tools = (f.tool('read_project_file', output_schema=object_schema({
        'content': {'type': 'string'}, 'sha256': {'type': 'string'}}), source_output_fields=('content',)),
        f.tool('write_source_file', input_schema=object_schema({k: {'type': 'string'} for k in
            ('workspaceId', 'path', 'content', 'expectedSha256')}),
            output_schema=object_schema({'changed': {'type': 'boolean'}}), source_argument_fields=('content',)))
    probe = signup_quality_precheck_for(SimpleNamespace(source_root=tmp_path), get_scenario(SCN_001_ID).planner_contract())
    assert len(probe()) == 2
    budget = f.budget(max_output_corrections=1, max_model_calls=6, max_tool_calls=4)
    engine = LLMEngine(role=f.role, provider=FakeProvider(*actions), tools=tools,
                       tool_executor=FakeToolExecutor(execute, execute, execute, execute))
    result = asyncio.run(engine.run(prompt=f.prompt, model=f.model, workspace_id=f.workspace_id,
        output=build_developer_output_contract(), budget=budget, source_progress_check=lambda: True,
        output_check=development_check(validate_developer_decision, signup_quality_check=probe)))
    assert result.data == ready and not probe()
    assert calls == [('read_project_file', 'source/main.py'), ('write_source_file', 'source/main.py'),
                     ('read_project_file', 'source/storage.py'), ('write_source_file', 'source/storage.py')]
    assert (budget.model_calls, budget.tool_calls) == (6, 4)
