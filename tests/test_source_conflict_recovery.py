"""A known rejected edit requires a fresh read and compare-and-swap repair.

Auth and uncertain effects remain closed; no actual product or provider runs.
"""
import asyncio
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


def test_conflict_refresh_resends_content_instead_of_an_old_read_pointer():
    history = RepairReadHistory({})
    path = 'source/main.py'
    result = {'path': path, 'content': 'current content', 'sha256': 'a' * 64, 'sizeBytes': 15}
    serialized = json.dumps(result)
    assert history.for_model('read_project_file', {'path': path}, result, serialized, 'read-1') == serialized
    assert history.for_model('read_project_file', {'path': path}, result, serialized, 'read-2') != serialized
    history.require_fresh_content(path)
    assert history.for_model('read_project_file', {'path': path}, result, serialized, 'read-3') == serialized


@pytest.mark.parametrize('single', [False, True], ids=['multi', 'single'])
@pytest.mark.parametrize('mode', ['changed', 'unchanged', 'unknown'])
def test_source_recovery_is_read_bound_and_finite(single, mode):
    fixture = _RuntimeFixture()
    fixture.setUp()
    source = 'source/main.py'
    old_hash, fresh_hash = 'a' * 64, 'b' * 64
    start = fixture.call('edit-first', name='edit_source_file', path=source,
                         oldText='old fragment', newText='repaired fragment', expectedSha256=old_hash)
    read = fixture.call('read-current', path=source)
    repaired = fixture.call('edit-fresh', name='edit_source_file', path=source,
                            oldText='current fragment', newText='repaired fragment', expectedSha256=fresh_hash)
    ready = {'kind': 'READY', 'summary': 'Ready for measured validation.', 'questions': [], 'rejection': None}
    provider = FakeProvider(tool_response(start), tool_response(read), tool_response(repaired),
                            text_response(json.dumps(ready)))
    error = RejectedEdit()
    if mode == 'unknown':
        error.result_unknown = True
    executor = FakeToolExecutor(error, {'sha256': fresh_hash, 'content': 'current fragment'},
                                {'changed': mode == 'changed'})
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
    if mode == 'changed':
        assert asyncio.run(operation).data == ready
        assert provider.requests[1].required_tool == 'read_project_file'
        assert provider.requests[1].tools[0].input_schema.to_dict()['properties']['path']['enum'] == [source]
        for tool in provider.requests[2].tools:
            assert tool.input_schema.to_dict()['properties']['expectedSha256']['enum'] == [fresh_hash]
    else:
        with pytest.raises(LLMRuntimeError) as caught:
            asyncio.run(operation)
        assert caught.value.code is (LLMErrorCode.TOOL_FAILED if mode == 'unknown' else LLMErrorCode.NO_PROGRESS)
        assert len(executor.calls) == (1 if mode == 'unknown' else 3)
