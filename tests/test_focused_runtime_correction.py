"""An unsupported normal-request claim gets one implementation opportunity in the shared engine."""

import asyncio
import json

import pytest

from agents.core.identity import SingleAgentIdentity, SinglePhase
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.llm.corrections import development_check
from agents.llm.engine import LLMEngine
from agents.roles.developer_contract import build_developer_output_contract, validate_developer_decision
from agents.runtime.normal_request_repair import NORMAL_REQUEST
from agents.runtime.ready_path_diff import claim_issues
from agents.single.contracts import build_single_draft_contract
from agents.single.prompts import PreparedSinglePrompt, build_single_system_prompt
from orchestrator.domain import SCN_001_ID
from orchestrator.domain.scenario_registry import get_scenario
from test_llm_runtime import FakeProvider, FakeToolExecutor, object_schema, text_response, tool_response


CAUSE = 'source/hasher.py::create'
WRONG = 'source/main.py::save'


def ready(ref):
    row = {'edit': 'Changed the producer.' if ref else None, 'sourceRef': ref, 'recheck': 'Retry the request.'}
    return text_response(json.dumps({'kind': 'READY', 'summary': 'Candidate for revalidation.',
        'rejection': None, 'questions': [], 'repairPaths': {NORMAL_REQUEST: row}}))


@pytest.mark.parametrize('single', [False, True], ids=['multi', 'single'])
@pytest.mark.parametrize('outcome', ['edited', 'open', 'repeat', 'citation', 'outside_catalog', 'no_budget'])
def test_runtime_claim_focus_and_its_measured_exit(outcome, single):
    from test_llm_runtime import LLMRuntimeTests
    fixture = LLMRuntimeTests()
    fixture.setUp()
    fifth = 'source/z.py::other_fix'
    links = {NORMAL_REQUEST: {CAUSE} if outcome in ('citation', 'outside_catalog') else set()}
    if outcome == 'outside_catalog':
        links[NORMAL_REQUEST].update({'source/other.py::first', 'source/other.py::second', 'source/other.py::third', fifth})
    events = []

    def source_edit(_call, _arguments, _context):
        links[NORMAL_REQUEST].add(CAUSE)
        return {'changed': True}

    actions = [ready(WRONG)]
    if outcome == 'edited':
        actions.extend([tool_response(fixture.call(name='edit_source_file')), ready(CAUSE)])
    elif outcome != 'no_budget':
        actions.append(ready({'citation': CAUSE, 'open': None, 'repeat': WRONG, 'outside_catalog': fifth}[outcome]))
    fake = FakeProvider(*actions)
    edits = FakeToolExecutor(source_edit)
    site = {'toolPath': 'source/main.py', 'sourceRef': 'source/main.py::signup', 'line': 50,
            'sourceSha256': 'a' * 64, 'statementExcerpt': 'configured = make_hasher(type=<string>)'}
    blocker = {'httpStatuses': [500], 'exceptionTypes': ['builtins.TypeError'], 'blockedCheckCount': 4,
               'sourceSites': [site], 'failedCreationCases': [{'httpStatus': 500, 'user_delta': 0}]}
    task_input = {'fixRequest': {}, 'activeRepairFocus': {'brief': {'blockingPrerequisite': blocker}}}
    role, phase = (SingleAgentIdentity.SINGLE_AGENT, SinglePhase.FIXING) if single else (fixture.role, None)
    tool = fixture.tool('edit_source_file', output_schema=object_schema({'changed': {'type': 'boolean'}}))
    engine = LLMEngine(role=role, single_phase=phase, provider=fake, tool_executor=edits, tools=(tool,))
    if single:
        contract = build_single_draft_contract(phase, scenario=get_scenario(SCN_001_ID), repair_paths=[NORMAL_REQUEST])
        prompt = PreparedSinglePrompt(phase, contract, build_single_system_prompt(), json.dumps(
            {'trusted': {'phase': phase.value}, 'contextId': 'test', 'history': [], 'taskInput': task_input}), 'test')
        output, validate = contract.output, contract.validate
    else:
        prompt = fixture.prepared(role, task_input)
        output = build_developer_output_contract([NORMAL_REQUEST])
        def validate(value):
            return validate_developer_decision(value, [NORMAL_REQUEST])
    check = development_check(validate, path_check=lambda v: claim_issues(v['repairPaths'], links),
                              priority_check=lambda: () if links[NORMAL_REQUEST] else (NORMAL_REQUEST,))
    task = engine.run(prompt=prompt, model=fixture.model, workspace_id=fixture.workspace_id,
                      output=output, output_check=check,
                      correction_candidates=lambda paths: {p: sorted(links[p])[:4] for p in paths},
                      source_progress_check=lambda: True, recovery_sink=events.append,
                      budget=fixture.budget(max_output_corrections=2, max_model_calls=1 if outcome == 'no_budget' else 8))
    if outcome == 'outside_catalog':
        with pytest.raises(LLMRuntimeError) as exc:
            asyncio.run(task)
        assert exc.value.code is LLMErrorCode.SCHEMA
    elif outcome in ('repeat', 'no_budget'):
        with pytest.raises(LLMRuntimeError) as exc:
            asyncio.run(task)
        assert exc.value.code == LLMErrorCode.SCHEMA
        assert events[-1].public()['action'] == 'STOP'
        assert events[-1].public()['reason'] == ('REPEATED' if outcome == 'repeat' else 'LIMIT_REACHED')
    else:
        result = asyncio.run(task)
        assert result.data['repairPaths'][NORMAL_REQUEST]['sourceRef'] == (None if outcome == 'open' else CAUSE)
    assert len(fake.requests) == len(actions)
    assert len(edits.calls) == (1 if outcome == 'edited' else 0)
    if outcome != 'no_budget':
        def accepts(request, ref):
            try:
                request.output.schema.validate(json.loads(ready(ref).output_text))
                return True
            except LLMRuntimeError:
                return False
        corrected_request = fake.requests[1]
        assert not accepts(corrected_request, WRONG)
        assert accepts(corrected_request, None)
        assert accepts(corrected_request, CAUSE) == (outcome in ('citation', 'outside_catalog'))
        assert [t.name for t in corrected_request.tools] == ['edit_source_file']
        if outcome == 'edited':
            assert accepts(fake.requests[-1], CAUSE)
        body = json.loads(json.loads(fake.requests[1].input_items_json)[-1]['content'])['hostCorrection']
        if outcome in ('citation', 'outside_catalog'):
            assert body['code'] == 'READY_PATH_EVIDENCE_MISMATCH'
            assert body['issues'][0]['candidateRefs'] == sorted(links[NORMAL_REQUEST])[:4]
            assert 'No new source edit is required' in body['instruction']
            assert 'measuredFailure' not in body
        else:
            assert body['code'] == 'READY_RUNTIME_REPAIR_REQUIRED'
            assert 'truthful partial' in body['instruction']
            assert body['measuredFailure']['sourceSites'] == [site]
            assert body['measuredFailure']['observedCreation'] == {'httpStatus': 500, 'user_delta': 0}
            assert sum(e.kind == 'DECISION' and e.action == 'REGENERATE' for e in events) == 1
