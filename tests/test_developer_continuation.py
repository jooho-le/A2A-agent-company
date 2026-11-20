"""Offline regressions for the November 19 Developer replay and its admission boundaries."""

from dataclasses import replace
from hashlib import sha256
import asyncio
import json

import pytest

from agents.llm.contracts import (LLMErrorCode, LLMRequest, LLMRuntimeError,
                                 OPENAI_RESPONSES_GLOBAL_TEXT, TokenUsage, json_text)
from agents.llm.developer_continuation import MeasuredInput
from agents.llm.engine import LLMEngine
import test_llm_runtime as runtime


MODEL = 'gpt-4.1-2025-04-14'
# Measured input/output pairs before the seventh request was refused, replay dafcda87.
RECEIPTS = [(31861, 42), (33028, 42), (33367, 42), (33621, 43), (35486, 44), (35759, 2272)]
FILES = ['main.py', 'database.py', 'models.py', 'requirements.lock', 'tests/test_health.py']


def receipt(response, pair):
    inp, out = pair
    return replace(response, model_id=MODEL, billing_context=OPENAI_RESPONSES_GLOBAL_TEXT,
                   usage=TokenUsage(inp, out, inp + out, cached_input_tokens=inp - 128))


@pytest.fixture
def runtime_case():
    case = runtime.LLMRuntimeTests('runTest')
    case.setUp()
    return case


@pytest.mark.parametrize('cap, fits', [(300000, True), (240000, False)])
def test_archived_six_receipts_then_submit_or_stop_within_the_same_cap(runtime_case, cap, fits):
    f = runtime_case
    # Artificial text of the same scale, no product code or private evidence in the fixture.
    prompt = f.prepared(f.role, {'fixRequest': {'evidence': 'x' * 100000}})
    tools = (f.tool(), f.tool('edit_source_file'))
    calls = [f.call(f'read-{i}', path='source/' + path) for i, path in enumerate(FILES)]
    calls.append(f.call('edit', name='edit_source_file', path='source/main.py'))
    provider = runtime.FakeProvider(*(receipt(runtime.tool_response(call), pair)
                                      for call, pair in zip(calls, RECEIPTS)), runtime.text_response())
    provider.name = 'openai'
    model = f.model.model_copy(update={'provider': 'openai', 'model_id': MODEL, 'seed': None})
    budget = f.budget(max_model_calls=10, max_tool_calls=18, max_output_tokens=8192, max_total_tokens=cap)
    executor = runtime.FakeToolExecutor()
    arguments = dict(tools=tools, prompt=prompt, model=model, budget=budget, executor=executor)
    if fits:
        result = asyncio.run(f.run_engine(provider, **arguments))
        assert result.data == {'answer': 'done'}
        last = provider.requests[-1]
        assert LLMEngine._usage_upper_bound(prompt, last.input_items_json, last.tools, last.output, 0).input_tokens > 94393
        assert budget.model_calls == 7 and budget.total_tokens == 205607 + 15
    else:
        with pytest.raises(LLMRuntimeError) as error:
            asyncio.run(f.run_engine(provider, **arguments))
        assert error.value.code is LLMErrorCode.BUDGET
        assert budget.model_calls == len(provider.requests) == 6 and budget.total_tokens == 205607
    assert len(executor.calls) == budget.tool_calls == 6


def test_only_the_same_pinned_wire_prefix_can_reuse_measured_input(runtime_case):
    f = runtime_case
    model = f.model.model_copy(update={'provider': 'openai', 'model_id': MODEL, 'seed': None})
    items = [{'role': 'user', 'content': 'previous input'}]
    request = LLMRequest(model, 'system', json_text(items), (), f.output, 1000, 30)
    response = receipt(runtime.text_response(), (32000, 42))
    measured = MeasuredInput.from_receipt(request, response, 1)
    contract = (model, 'system', (), f.output, None, ())
    more = items + [{'type': 'function_call_output', 'call_id': 'r', 'output': '한글' * 500}]
    bound = measured.reserve(110000, json_text(more), more, contract)
    assert 32000 + 3000 < bound < 45000  # includes the cached part of the 32,000
    new_output = replace(f.output, schema=runtime.object_schema({'fix': {'type': 'null'}}))
    narrowed = (model, 'system', (), new_output, None, ())
    assert measured.reserve(110000, json_text(more), more, narrowed) == 110000
    assert measured.reserve(110000, json_text(more), more, narrowed, repair_schema_changed=True) == (
        bound + len(new_output.schema.schema_json.encode('utf-8')))
    assert measured.reserve(110000, json_text(more), more, (*narrowed[:-2], 'read_project_file', ()),
                            repair_schema_changed=True) == 110000
    changed = [{'role': 'user', 'content': 'different original'}, *more[1:]]
    reasoning = items + [{'type': 'reasoning', 'encrypted_content': 'opaque'}]
    for history, wire in [(more, (*contract[:4], 'read_project_file', ())), (more, (model, 'other', *contract[2:])),
                          (more, (model, 'system', (f.tool(),), *contract[3:])),
                          (more, (model, 'system', (), replace(f.output, name='different'), None, ())),
                          (changed, contract), (reasoning, contract)]:
        assert measured.reserve(110000, json_text(history), history, wire) == 110000
    for untrusted in [replace(response, model_id='other'), replace(response, billing_context=None),
                      replace(response, usage=None), replace(response, status='incomplete')]:
        assert MeasuredInput.from_receipt(request, untrusted, 1) is None
    alias = replace(request, model=model.model_copy(update={'model_id': 'gpt-4.1'}))
    assert MeasuredInput.from_receipt(alias, replace(response, model_id='gpt-4.1'), 1) is None


def test_unknown_usage_after_a_measured_read_keeps_its_reservation_and_one_retry(runtime_case, monkeypatch):
    f = runtime_case
    monkeypatch.setattr('agents.llm.engine.PROVIDER_RETRY_DELAY_SECONDS', 0)
    read = receipt(runtime.tool_response(f.call(path='source/main.py')), RECEIPTS[0])
    provider = runtime.FakeProvider(read, LLMRuntimeError(LLMErrorCode.PROVIDER, transient=True),
                                    runtime.text_response())
    provider.name = 'openai'
    budget = f.budget(max_total_tokens=175000, max_provider_retries=1, max_output_tokens=8192)
    model = f.model.model_copy(update={'provider': 'openai', 'model_id': MODEL, 'seed': None})
    result = asyncio.run(f.run_engine(provider, budget=budget, model=model,
                         prompt=f.prepared(f.role, {'fixRequest': {'evidence': 'x' * 100000}}),
                         tools=(f.tool(),), executor=runtime.FakeToolExecutor()))
    assert budget.model_calls == 3 and budget.total_tokens is None
    assert result.records[1].usage is None and budget.known_total_tokens == sum(RECEIPTS[0]) + 15
    assert provider.requests[1].input_items_json == provider.requests[2].input_items_json


def test_repeated_reads_still_authorize_and_only_identical_source_is_referenced(runtime_case):
    f = runtime_case
    path = 'source/main.py'
    read = f.tool(output_schema=runtime.object_schema({
        'path': {'type': 'string'}, 'sha256': {'type': 'string'},
        'sizeBytes': {'type': 'integer'}, 'content': {'type': 'string'}}), source_output_fields=('content',))
    edit = f.tool('edit_source_file')
    def file_result(content):
        raw = content.encode()
        return dict(path=path, content=content, sha256=sha256(raw).hexdigest(), sizeBytes=len(raw))
    first = file_result('x = 1\n' * 100)
    altered = file_result('x = 2\n' * 100)
    names = [read.name, read.name, read.name, edit.name, read.name, read.name]
    executor = runtime.FakeToolExecutor(first, first, altered, {'answer': 'ok'}, altered, altered)
    provider = runtime.FakeProvider(*(runtime.tool_response(f.call(str(i), name=name, path=path))
                                      for i, name in enumerate(names)), runtime.text_response())
    focus = {'brief': {'sourceInspection': {'firstReadBatch': [path]}}}
    asyncio.run(f.run_engine(provider, tools=(read, edit), executor=executor,
                             prompt=f.prepared(f.role, {'fixRequest': {}, 'activeRepairFocus': focus})))
    transcript = json.loads(provider.requests[-1].input_items_json)
    payloads = [json.loads(item['output']) for item in transcript if item.get('type') == 'function_call_output']
    assert sum('hostRepairInspection' in json.loads(item['content']) for item in transcript
               if item.get('role') == 'user') == 1
    assert [call.name for call, _, _ in executor.calls] == names
    assert payloads[0] == first and payloads[2] == payloads[4] == altered
    for index, previous in [(1, '0'), (5, '4')]:
        assert payloads[index]['unchangedSinceReadCallId'] == previous and 'content' not in payloads[index]
    # A later access rejection must reach the engine even when the first read succeeded.
    rejected = runtime.FakeProvider(*(runtime.tool_response(f.call(str(i), path=path)) for i in range(2)))
    with pytest.raises(LLMRuntimeError) as error:
        asyncio.run(f.run_engine(rejected, tools=(read,), executor=runtime.FakeToolExecutor(first, ValueError()),
                                 prompt=f.prepared(f.role, {'fixRequest': {}})))
    assert error.value.code is LLMErrorCode.TOOL_FAILED
