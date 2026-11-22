"""The source-risk check consumes the same bounded correction in Single and Multi."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from agents.core.identity import SingleAgentIdentity, SinglePhase
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.llm.corrections import development_check
from agents.llm.engine import LLMEngine
from agents.roles.developer_contract import build_developer_output_contract, validate_developer_decision
from agents.runtime.output_safety_precheck import output_safety_precheck_for
from agents.single.contracts import build_single_draft_contract
from agents.single.prompts import PreparedSinglePrompt, build_single_system_prompt
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario
from test_agent_output_safety_precheck import SOURCE
from test_llm_runtime import FakeProvider, FakeToolExecutor, LLMRuntimeTests as _RuntimeFixture, text_response, tool_response


@pytest.mark.parametrize('single', [False, True], ids=['multi','single'])
@pytest.mark.parametrize('outcome', ['fixed','reworded','one_call_left','no_tools'])
def test_actual_engine_requires_a_safe_candidate_or_stops_within_shared_limits(tmp_path, single, outcome):
    fixture = _RuntimeFixture()
    fixture.setUp()
    source = tmp_path / 'main.py'
    source.write_text(SOURCE)
    data = {'kind':'READY','summary':'Candidate for Host validation.','questions':[],'rejection':None}
    ready = text_response(json.dumps(data))
    actions = [ready]
    if outcome == 'fixed':
        actions += [tool_response(fixture.call(name='edit_source_file')), ready]
    elif outcome == 'reworded':
        actions.append(text_response(json.dumps({**data,'summary':'Everything is safe now.'})))
    provider, events = FakeProvider(*actions), []

    def edit(*_):
        source.write_text(SOURCE + '\n@app.exception_handler(RequestValidationError)\nasync def safe_error(request, exc):\n return JSONResponse(status_code=422, content={"detail":"invalid request"})\n')
        return {'answer':'changed'}

    if single:
        phase, role = SinglePhase.FIXING, SingleAgentIdentity.SINGLE_AGENT
        contract = build_single_draft_contract(phase, scenario=get_scenario(SCN_001_ID))
        prompt = PreparedSinglePrompt(phase,contract,build_single_system_prompt(),json.dumps({
            'trusted':{'phase':phase.value},'contextId':'offline','history':[],'taskInput':{}}),'offline')
        output, validate = contract.output, contract.validate
    else:
        phase, role, prompt = None, fixture.role, fixture.prompt
        output, validate = build_developer_output_contract(), validate_developer_decision
    engine = LLMEngine(role=role,single_phase=phase,provider=provider,tools=(fixture.tool('edit_source_file'),),
                       tool_executor=FakeToolExecutor(edit))
    scenario = get_scenario(SCN_001_ID).planner_contract()
    probe = output_safety_precheck_for(SimpleNamespace(source_root=tmp_path),scenario)
    operation = engine.run(prompt=prompt,model=fixture.model,workspace_id=fixture.workspace_id,output=output,
        source_progress_check=lambda:True, output_check=development_check(validate,output_safety_check=probe),
        recovery_sink=events.append, budget=fixture.budget(max_output_corrections=2,
            max_model_calls=2 if outcome=='one_call_left' else 8,
            max_tool_calls=0 if outcome=='no_tools' else 4))
    if outcome == 'fixed':
        assert asyncio.run(operation).data == data
        assert probe() == []
    else:
        with pytest.raises(LLMRuntimeError) as caught:
            asyncio.run(operation)
        assert caught.value.code is LLMErrorCode.SOURCE_REPAIR
        stopped = events[-1].public()
        assert stopped['code'] == 'READY_OUTPUT_SAFETY_REVIEW' and stopped['action'] == 'STOP'
        assert stopped['reason'] == ('REPEATED' if outcome=='reworded' else 'LIMIT_REACHED')
    assert len(provider.requests) == len(actions)
