"""Opt-in real container checks; model responses are scripted, never OpenAI.

Proves a reproduced defect -> actual file repair -> unchanged final checks.
Does not measure model performance or count as an experimental Run.
"""
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agents.core.identity import SingleAgentIdentity, SinglePhase
from agents.llm.corrections import development_check
from agents.llm.engine import LLMEngine
from agents.roles.developer_contract import build_developer_output_contract, validate_developer_decision
from agents.runtime.signup_quality_precheck import signup_quality_precheck_for
from agents.single.contracts import build_single_draft_contract
from agents.single.prompts import PreparedSinglePrompt, build_single_system_prompt
from evaluation import protected_tests as pt
from evaluation.final_evaluation import DockerRunner, _runner_source
from orchestrator.domain.scenario_registry import get_scenario, SCN_001_ID
from orchestrator.sandbox.scn001_start_contract import scn001_build_argv
from tests.evaluation import signup_service
import test_llm_runtime as runtime_fixtures
from test_llm_runtime import (FakeProvider, FakeToolExecutor,
                             object_schema, text_response, tool_response)


pytestmark = pytest.mark.skipif(os.getenv('A2A_DOCKER_REPAIR_TEST') != '1',
                                reason='requires explicit local Docker verification')


def write_tree(root, files):
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    for name, value in files.items():
        path = root / name
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.write_text(value)
        path.chmod(0o644)


@pytest.mark.parametrize('single', [False, True], ids=['multi', 'single'])
def test_real_privacy_failure_then_engine_repair_then_nine_checks_pass(tmp_path, single):
    config = Path(__file__).resolve().parents[1] / 'configs/owned-tools.local.json'
    image = json.loads(config.read_text())['imageReference']
    runner = DockerRunner(image, timeout=60)
    assert runner.image_digest() == image
    source, inputs = tmp_path / 'source', tmp_path / 'inputs'
    good = signup_service.build('good')
    good['main.py'] += '\n@app.get("/")\ndef health():\n    return {"status": "ok"}\n'
    unsafe = dict(good)
    unsafe['main.py'] = good['main.py'].split('@app.exception_handler(RequestValidationError)')[0]
    unsafe['main.py'] += '\n@app.get("/")\ndef health():\n    return {"status": "ok"}\n'
    unsafe['main.py'] += ('\nfrom fastapi import HTTPException\n'
        '@app.exception_handler(HTTPException)\nasync def business_rejection(request, exc):\n'
        '    return JSONResponse(status_code=exc.status_code, content={"detail": "REQUEST_REJECTED"})\n')
    write_tree(source, unsafe)
    write_tree(inputs, pt.protected_files('final'))
    (inputs / '_unit_runner.py').write_bytes(_runner_source())
    write_tree(inputs, {'_replay_runner.py': (Path(__file__).resolve().parents[1] /
        'src/evaluation/replay_runner.py').read_text(), '_input_replay.json': '{}'})
    before = runner.run(source, inputs, 'tests/signup', pt.load_suite()['scopes']['final']['pattern'],
                        script='_replay_runner.py')
    if before.error or before.report is None:
        pytest.fail('DOCKER_INITIAL_EVALUATION_UNAVAILABLE: ' + str(before.error))
    assert before.report['total'] == 9 and before.report['failed'] > 0
    privacy = next(c for c in pt.load_suite()['checks'] if c['key'] == 'SEC-SIGNUP-005')
    assert {c['testId'] for c in before.report['tests'] if c['outcome'] == 'FAIL'} == {
        pt.unit_test_id(privacy, 'final', pt.load_suite())}
    f = runtime_fixtures.LLMRuntimeTests()
    f.setUp()
    path = source / 'main.py'
    ready = {'kind': 'READY', 'summary': 'Candidate for Host validation.', 'questions': [], 'rejection': None}
    targets = []
    def read_target(request):
        for item in json.loads(request.input_items_json):
            if item.get('role') == 'user' and isinstance(item.get('content'), str):
                targets.extend(json.loads(item['content']).get('hostCorrection', {}).get('sourceRepairTargets', []))
        target, = targets
        assert target['exceptionType'] == 'fastapi.exceptions.RequestValidationError'
        assert target['preserveExistingHttpExceptionHandler'] is True
        return tool_response(f.call('read-main', path=target['toolPath']))
    def edit_response(_request):
        target, = targets
        content = (path.read_text() + '\n' + target['importExample'] + '\n' + target['registrationExample']
            + '\nasync def request_validation_rejection(request, exc):\n'
              '    return JSONResponse(status_code=422, content={"detail": "INVALID_REQUEST"})\n')
        return tool_response(f.call('repair-main', name='write_source_file', path='source/main.py',
            expectedSha256=sha256(path.read_bytes()).hexdigest(), content=content))
    provider = FakeProvider(text_response(json.dumps(ready)), read_target, edit_response, text_response(json.dumps(ready)))
    def file_tool(call, arguments, _context):
        assert arguments['path'] == 'source/main.py'
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
    scenario = get_scenario(SCN_001_ID)
    if single:
        phase, role = SinglePhase.IMPLEMENTING, SingleAgentIdentity.SINGLE_AGENT
        contract = build_single_draft_contract(phase, scenario=scenario)
        prompt = PreparedSinglePrompt(phase, contract, build_single_system_prompt(), json.dumps({
            'trusted': {'phase': phase.value}, 'contextId': 'offline', 'history': [], 'taskInput': {}}), 'offline')
        output, validate = contract.output, contract.validate
    else:
        phase, role, prompt = None, f.role, f.prompt
        output, validate = build_developer_output_contract(), validate_developer_decision
    probe = signup_quality_precheck_for(SimpleNamespace(source_root=source), scenario.planner_contract())
    assert probe()
    engine = LLMEngine(role=role, single_phase=phase, provider=provider, tools=tools,
                       tool_executor=FakeToolExecutor(file_tool, file_tool))
    result = asyncio.run(engine.run(prompt=prompt, model=f.model, output=output, workspace_id=f.workspace_id,
        source_progress_check=lambda: True, output_check=development_check(validate, signup_quality_check=probe),
        budget=f.budget(max_output_corrections=2, max_model_calls=8, max_tool_calls=6)))
    assert result.data == ready and not probe()
    assert path.read_text().startswith(unsafe['main.py'])
    name = 'source-repair-start-' + uuid4().hex[:12]
    command = runner._base_command(name) + ['--mount', f'type=bind,src={source},dst=/snapshot,readonly',
        '--entrypoint', runner.python, image, *scn001_build_argv(runner.python)[1:]]
    try:
        started = runner._call(command, 60)
    except Exception:
        runner._remove_timed_out_container(name)
        raise
    assert started.returncode == 0, 'APP_START_CONTRACT_FAILED'
    after = runner.run(source, inputs, 'tests/signup', pt.load_suite()['scopes']['final']['pattern'],
                       script='_replay_runner.py')
    if after.error or after.report is None:
        pytest.fail('DOCKER_REPAIRED_EVALUATION_UNAVAILABLE')
    assert after.report['total'] == after.report['passed'] == 9 and after.report['failed'] == 0


def test_start_probe_rejects_self_import_inside_container(tmp_path):
    image = json.loads((Path(__file__).resolve().parents[1] / 'configs/owned-tools.local.json').read_text())['imageReference']
    runner = DockerRunner(image, timeout=60)
    write_tree(tmp_path / 'source', {'main.py': 'from main import app\n'})
    name = 'source-repair-broken-' + uuid4().hex[:12]
    command = runner._base_command(name) + ['--mount', f'type=bind,src={tmp_path / "source"},dst=/snapshot,readonly',
        '--entrypoint', runner.python, image, *scn001_build_argv(runner.python)[1:]]
    try:
        result = runner._call(command, 60)
    except Exception:
        runner._remove_timed_out_container(name)
        raise
    assert result.returncode == 1
    assert result.stdout.splitlines()[-1] == 'SCN001_IMPORT_FAILED'
