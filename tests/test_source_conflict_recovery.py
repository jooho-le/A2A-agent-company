"""A known rejected edit requires a fresh read and compare-and-swap repair.

Auth and uncertain effects remain closed; no actual product or provider runs.
"""
import asyncio
from hashlib import sha256
import json

import pytest

from agents.core.identity import SingleAgentIdentity, SinglePhase
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.llm.engine import LLMEngine
from agents.llm.developer_continuation import RepairReadHistory
from agents.roles.developer_contract import build_developer_output_contract
from agents.single.contracts import build_single_draft_contract
from agents.single.prompts import PreparedSinglePrompt, build_single_system_prompt
from orchestrator.domain.scenario_registry import get_scenario, SCN_001_ID
from test_llm_runtime import (FakeProvider, FakeToolExecutor, LLMRuntimeTests as _RuntimeFixture,
                             object_schema, text_response, tool_response)


class RejectedEdit(Exception):
    error_kind = 'EDIT_TARGET_MISSING'
    result_unknown = False


class SettledConflict(Exception):
    error_kind = 'WRITE_CONFLICT'
    result_unknown = False


def test_conflict_refresh_resends_content_instead_of_an_old_read_pointer():
    history = RepairReadHistory({})
    path = 'source/main.py'
    content = 'current content\n' * 32
    result = {'path': path, 'content': content, 'sha256': 'a' * 64, 'sizeBytes': len(content.encode())}
    serialized = json.dumps(result)
    assert history.for_model('read_project_file', {'path': path}, result, serialized, 'read-1') == serialized
    assert history.for_model('read_project_file', {'path': path}, result, serialized, 'read-2') != serialized
    history.require_fresh_content(path)
    assert history.for_model('read_project_file', {'path': path}, result, serialized, 'read-3') == serialized


@pytest.mark.parametrize('single', [False, True], ids=['multi', 'single'])
@pytest.mark.parametrize('mode', ['changed', 'unchanged', 'unknown', 'bad_partial_retry'])
def test_source_recovery_is_read_bound_and_finite(single, mode):
    fixture = _RuntimeFixture()
    fixture.setUp()
    source = 'source/main.py'
    old_hash, fresh_hash = 'a' * 64, 'b' * 64
    start = fixture.call('edit-first', name='edit_source_file', path=source,
                         oldText='old fragment', newText='repaired fragment', expectedSha256=old_hash)
    read = fixture.call('read-current', path=source)
    repaired = fixture.call('write-fresh', name='write_source_file', path=source,
                            content='repaired fragment', expectedSha256=fresh_hash)
    ready = {'kind': 'READY', 'summary': 'Ready for measured validation.', 'questions': [], 'rejection': None}
    provider = FakeProvider(tool_response(start), tool_response(read), tool_response(repaired),
                            text_response(json.dumps(ready)))
    if mode == 'bad_partial_retry':
        provider.script.insert(2, tool_response(fixture.call('ignored-partial', name='edit_source_file', path=source,
            oldText='guessed fragment', newText='another guess', expectedSha256=fresh_hash)))
    error = RejectedEdit()
    if mode == 'unknown':
        error.result_unknown = True
    executor = FakeToolExecutor(error, {'sha256': fresh_hash, 'content': 'current fragment'},
                                {'changed': mode in ('changed', 'bad_partial_retry')})
    write_output = object_schema({'changed': {'type': 'boolean'}})
    tools = (
        fixture.tool('read_project_file', output_schema=object_schema({
            'sha256': {'type': 'string'}, 'content': {'type': 'string'}}), source_output_fields=('content',)),
        fixture.tool('edit_source_file', input_schema=object_schema({
            k: {'type': 'string'} for k in ('workspaceId', 'path', 'oldText', 'newText', 'expectedSha256')}),
            output_schema=write_output, source_argument_fields=('oldText', 'newText')),
        fixture.tool('write_source_file', input_schema=object_schema({
            k: {'type': 'string'} for k in ('workspaceId', 'path', 'content', 'expectedSha256')}),
            output_schema=write_output, source_argument_fields=('content',)),
    )
    if single:
        phase, role = SinglePhase.FIXING, SingleAgentIdentity.SINGLE_AGENT
        contract = build_single_draft_contract(phase, scenario=get_scenario(SCN_001_ID))
        prompt = PreparedSinglePrompt(phase, contract, build_single_system_prompt(), json.dumps({
            'trusted': {'phase': phase.value}, 'contextId': 'offline', 'history': [], 'taskInput': {}}), 'offline')
        output = contract.output
    else:
        phase, role, prompt = None, fixture.role, fixture.prompt
        output = build_developer_output_contract()
    engine = LLMEngine(role=role, single_phase=phase, provider=provider, tools=tools, tool_executor=executor)
    operation = engine.run(prompt=prompt, model=fixture.model, output=output, workspace_id=fixture.workspace_id,
        source_progress_check=lambda: True,
        budget=fixture.budget(max_output_corrections=2, max_model_calls=6, max_tool_calls=5))
    if mode in ('changed', 'bad_partial_retry'):
        assert asyncio.run(operation).data == ready
        assert provider.requests[1].required_tool == 'read_project_file'
        assert provider.requests[1].tools[0].input_schema.to_dict()['properties']['path']['enum'] == [source]
        assert [tool.name for tool in provider.requests[2].tools] == ['write_source_file']
        for tool in provider.requests[2].tools:
            assert tool.input_schema.to_dict()['properties']['expectedSha256']['enum'] == [fresh_hash]
        assert len(executor.calls) == 3  # a second partial edit is never executed
    else:
        with pytest.raises(LLMRuntimeError) as caught:
            asyncio.run(operation)
        assert caught.value.code is (LLMErrorCode.TOOL_FAILED if mode == 'unknown' else LLMErrorCode.NO_PROGRESS)
        assert len(executor.calls) == (1 if mode == 'unknown' else 3)


@pytest.mark.parametrize('single', [False, True], ids=['multi', 'single'])
@pytest.mark.parametrize('mode', ['current', 'no_prior_write', 'other_path', 'wrong_receipt',
                                 'wrong_content', 'baseline_restored', 'uncertain', 'quality_still_open'])
def test_conflict_noop_requires_prior_same_file_edit_fresh_hash_and_ready_recheck(tmp_path, single, mode):
    """Reproduce write → edit → stale CAS → read → identical write without a paid model."""
    f = _RuntimeFixture()
    f.setUp()
    initial, current = 'original implementation\n', 'corrected implementation\n'
    paths = {'source/main.py': tmp_path / 'main.py', 'source/database.py': tmp_path / 'database.py'}
    for path in paths.values():
        path.write_text(initial)
    before = {key: path.read_bytes() for key, path in paths.items()}
    old_hash, fresh_hash = sha256(initial.encode()).hexdigest(), sha256(current.encode()).hexdigest()
    fresh_content = initial if mode == 'baseline_restored' else current
    pinned_hash = sha256(fresh_content.encode()).hexdigest()
    first = (f.call('first-read', path='source/main.py') if mode == 'no_prior_write' else
             f.call('first-write', name='write_source_file', path='source/database.py' if mode == 'other_path'
                    else 'source/main.py', content=current, expectedSha256=old_hash))
    conflict = f.call('stale-write', name='write_source_file', path='source/main.py',
                      content=current, expectedSha256=fresh_hash if mode == 'baseline_restored' else old_hash)
    read = f.call('refresh', path='source/main.py')
    noop = f.call('current-write', name='write_source_file', path='source/main.py',
                 content='different requested bytes\n' if mode == 'wrong_content' else fresh_content,
                 expectedSha256=pinned_hash)
    ready = {'kind': 'READY', 'summary': 'Candidate for actual validation.', 'questions': [], 'rejection': None}
    provider = FakeProvider(tool_response(first), tool_response(conflict), tool_response(read),
                            tool_response(noop), text_response(json.dumps(ready)))

    def execute(call, args, _context):
        path = paths[args['path']]
        if call.call_id == 'stale-write':
            # Simulate a concurrent writer only in negative cases. The Engine
            # must not attribute this external change to its own prior Tool.
            if mode in ('no_prior_write', 'other_path', 'baseline_restored'):
                path.write_text(fresh_content)
            error = SettledConflict()
            if mode == 'uncertain':
                error.result_unknown = True
            raise error
        if call.name == 'read_project_file':
            return {'content': path.read_text(), 'sha256': sha256(path.read_bytes()).hexdigest()}
        assert args['expectedSha256'] == sha256(path.read_bytes()).hexdigest()
        if call.call_id == 'first-write':
            path.write_text(args['content'])
            return {'path': args['path'], 'sha256': sha256(path.read_bytes()).hexdigest(), 'changed': True}
        # The trusted file Tool's no-op receipt is exact. Negative cases verify
        # that even a lying/mismatched receipt cannot discharge recovery.
        return {'path': args['path'], 'sha256': '0' * 64 if mode == 'wrong_receipt' else pinned_hash,
                'changed': False}

    tool_output = object_schema({'path': {'type': 'string'}, 'sha256': {'type': 'string'},
                                 'changed': {'type': 'boolean'}})
    tools = (f.tool('read_project_file', output_schema=object_schema({
        'content': {'type': 'string'}, 'sha256': {'type': 'string'}}), source_output_fields=('content',)),
        f.tool('write_source_file', input_schema=object_schema({k: {'type': 'string'} for k in
            ('workspaceId', 'path', 'content', 'expectedSha256')}), output_schema=tool_output,
            source_argument_fields=('content',)))
    if single:
        phase, role = SinglePhase.FIXING, SingleAgentIdentity.SINGLE_AGENT
        draft = build_single_draft_contract(phase, scenario=get_scenario(SCN_001_ID))
        prompt = PreparedSinglePrompt(phase, draft, build_single_system_prompt(), json.dumps({
            'trusted': {'phase': phase.value}, 'contextId': 'offline', 'history': [], 'taskInput': {}}), 'offline')
        output = draft.output
    else:
        phase, role, prompt, output = None, f.role, f.prompt, build_developer_output_contract()
    checked = []

    def final_check(_candidate, _tools):
        checked.append(True)
        if mode == 'quality_still_open':
            return {'code': 'READY_SIGNUP_QUALITY_REVIEW', 'issues': [
                {'fieldPath': '/source', 'reasonCode': 'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS',
                 'requirementKey': 'REQ-006', 'sourcePath': 'main.py', 'repairSourcePath': 'main.py'}]}
        return None

    executor = FakeToolExecutor(*([execute] * 4))
    engine = LLMEngine(role=role, single_phase=phase, provider=provider, tools=tools, tool_executor=executor)
    operation = engine.run(prompt=prompt, model=f.model, output=output, workspace_id=f.workspace_id,
        source_progress_check=lambda: any(p.read_bytes() != before[k] for k, p in paths.items()),
        output_check=final_check, budget=f.budget(max_output_corrections=2, max_model_calls=5, max_tool_calls=4))
    if mode == 'current':
        assert asyncio.run(operation).data == ready
        assert checked == [True]
        assert paths['source/main.py'].read_text() == current
        assert len(executor.calls) == 4
        assert provider.requests[2].required_tool == 'read_project_file'
        assert provider.requests[3].tools[0].input_schema.to_dict()['properties']['expectedSha256']['enum'] == [fresh_hash]
        assert 'RECOVERY_CONTENT_ALREADY_CURRENT' in provider.requests[4].input_items_json
    else:
        with pytest.raises(LLMRuntimeError) as caught:
            asyncio.run(operation)
        expected = LLMErrorCode.TOOL_FAILED if mode == 'uncertain' else (
            LLMErrorCode.SOURCE_REPAIR if mode == 'quality_still_open' else LLMErrorCode.NO_PROGRESS)
        assert caught.value.code is expected
        assert checked == ([True] if mode == 'quality_still_open' else [])
        assert len(executor.calls) == (2 if mode == 'uncertain' else 4)
