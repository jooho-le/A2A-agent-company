"""The installed-library READY check uses one existing correction in both Developer modes."""
import asyncio
import json

import pytest

from agents.core.identity import SingleAgentIdentity, SinglePhase
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.llm.corrections import development_check
from agents.llm.engine import LLMEngine
from agents.roles.developer_contract import build_developer_output_contract, validate_developer_decision
from agents.runtime.ready_library_types import ready_library_type_mismatches
from agents.single.contracts import build_single_draft_contract
from agents.single.prompts import PreparedSinglePrompt, build_single_system_prompt
from orchestrator.domain import SCN_001_ID
from orchestrator.domain.scenario_registry import get_scenario
from test_llm_runtime import FakeProvider, FakeToolExecutor, text_response, tool_response
from test_ready_library_types import BAD, ENV


@pytest.mark.parametrize('single', [False, True], ids=['multi', 'single'])
@pytest.mark.parametrize('outcome', ['fixed', 'reworded', 'new_location', 'disabled', 'one_call_left', 'no_tools'])
def test_library_type_stops_before_acceptance_or_resumes_within_budget(outcome, single):
    from test_llm_runtime import LLMRuntimeTests
    f = LLMRuntimeTests()
    f.setUp()
    data = {'kind': 'READY', 'summary': 'Candidate for Host verification.', 'questions': [], 'rejection': None}
    ready = text_response(json.dumps(data))
    actions = [ready]
    files = {'main.py': BAD}
    events = []

    def apply(_call, _arguments, _context):
        if outcome == 'new_location':
            files['main.py'] = b'\n' + BAD.replace(b'private-value', b'different-string')
        elif outcome == 'fixed':
            files['main.py'] = BAD.replace(b"type='private-value'", b"")
        return {'answer': 'changed'}

    if outcome in {'fixed', 'new_location'}:
        actions.extend((tool_response(f.call(name='edit_source_file')), ready))
    if outcome == 'reworded':
        actions.append(text_response(json.dumps({**data, 'summary': 'Addressed the issue.'})))
    provider = FakeProvider(*actions)
    if single:
        role, phase = SingleAgentIdentity.SINGLE_AGENT, SinglePhase.FIXING
        contract = build_single_draft_contract(phase, scenario=get_scenario(SCN_001_ID))
        prompt = PreparedSinglePrompt(phase, contract, build_single_system_prompt(), json.dumps(
            {'trusted': {'phase': phase.value}, 'contextId': 'test', 'history': [], 'taskInput': {}}), 'test')
        output, validate = contract.output, contract.validate
    else:
        role, phase = f.role, None
        prompt, output, validate = f.prompt, build_developer_output_contract(), validate_developer_decision
    engine = LLMEngine(role=role, single_phase=phase, provider=provider, tools=(f.tool('edit_source_file'),),
                       tool_executor=FakeToolExecutor(apply))
    # No repairPaths are declared. The original call must still be checked even before a net edit.
    runner = engine.run(prompt=prompt, model=f.model, workspace_id=f.workspace_id, output=output,
        output_check=development_check(validate, library_type_check=lambda: ready_library_type_mismatches(files, ENV)),
        source_progress_check=lambda: files['main.py'] != BAD, recovery_sink=events.append,
        budget=f.budget(max_output_corrections=0 if outcome == 'disabled' else 2,
                        max_model_calls=2 if outcome == 'one_call_left' else 8,
                        max_tool_calls=0 if outcome == 'no_tools' else 4))
    if outcome == 'fixed':
        assert asyncio.run(runner).data == data
    else:
        with pytest.raises(LLMRuntimeError) as caught:
            asyncio.run(runner)
        assert caught.value.code is LLMErrorCode.SCHEMA
        last = events[-1].public()
        assert (last['code'], last['action']) == ('READY_LIBRARY_TYPE_MISMATCH', 'STOP')
        assert last['reason'] == {'disabled': 'DISABLED', 'one_call_left': 'LIMIT_REACHED',
                                  'no_tools': 'LIMIT_REACHED'}.get(outcome, 'REPEATED')
    assert len(provider.requests) == len(actions)
    expected = int(outcome not in {'disabled', 'one_call_left', 'no_tools'})
    assert sum(e.kind == 'DECISION' and e.action == 'REGENERATE' for e in events) == expected
    if expected:
        content = json.loads(provider.requests[1].input_items_json)[-1]['content']
        correction = json.loads(content)['hostCorrection']
        [issue] = correction['issues']
        assert correction['correctionsRemaining'] == 1
        assert 'only library-type correction in this stage' in correction['instruction']
        assert issue == {'fieldPath': '/source', 'reasonCode': 'LIBRARY_ARGUMENT_TYPE_MISMATCH',
                         'toolPath': 'source/main.py', 'line': 4, 'callable': 'argon2.PasswordHasher',
                         'parameter': 'type', 'requiredType': 'argon2.low_level.Type', 'actualType': 'str'}
        assert 'private-value' not in content
