"""Regression for Run 330e2984: HTTPException handler did not cover request validation."""
from copy import deepcopy
import asyncio
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from agents.llm.corrections import checked_rejection, feedback
from agents.runtime.signup_quality_precheck import inspect_signup_quality
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario
from test_signup_quality_precheck import APP, SAFE_HANDLER


HTTP_ONLY = APP.replace('from fastapi.exceptions import RequestValidationError', 'from fastapi import HTTPException')
HTTP_ONLY += SAFE_HANDLER.replace('RequestValidationError)', 'HTTPException)')


def packet_for(root, text, contract=None):
    (root / 'main.py').write_text(text)
    contract = contract or get_scenario(SCN_001_ID).planner_contract()
    issues = inspect_signup_quality(root, contract)
    rejection = checked_rejection({'code': 'READY_SIGNUP_QUALITY_REVIEW', 'issues': issues})
    return issues, feedback(rejection['code'], issues=rejection['issues'], remaining=1)['hostCorrection']


def test_http_only_handler_gets_exact_missing_exception_and_preservation_guidance(tmp_path):
    issues, packet = packet_for(tmp_path, HTTP_ONLY)
    warning = next(r for r in issues if r['reasonCode'] == 'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS')
    context = warning['validationErrorContext']
    assert context['applicationAttribute'] == 'app'
    assert context['httpExceptionHandlerDetected'] is True
    assert context['requestValidationHandlerDetected'] is False
    assert context['httpExceptionHandlerLines']
    target, = packet['sourceRepairTargets']
    assert target['toolPath'] == 'source/main.py'
    assert target['exceptionType'] == 'fastapi.exceptions.RequestValidationError'
    assert target['registrationExample'] == '@app.exception_handler(RequestValidationError)'
    assert target['preserveExistingHttpExceptionHandler'] is True
    assert 'HTTPException does not catch RequestValidationError' in packet['instruction']
    assert HTTP_ONLY not in str(packet)


def test_registration_uses_the_approved_application_name(tmp_path):
    contract = deepcopy(get_scenario(SCN_001_ID).planner_contract())
    contract['externalContract']['app']['attribute'] = 'company_app'
    _, packet = packet_for(tmp_path, HTTP_ONLY.replace('app =', 'company_app =').replace('@app.', '@company_app.'), contract)
    assert packet['sourceRepairTargets'][0]['registrationExample'] == '@company_app.exception_handler(RequestValidationError)'


def test_handler_on_another_app_does_not_protect_the_signup_app(tmp_path):
    text = APP + '\nother = FastAPI()\n' + SAFE_HANDLER.replace('@app.', '@other.')
    issues, packet = packet_for(tmp_path, text)
    assert any(r['reasonCode'] == 'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS' for r in issues)
    assert packet['sourceRepairTargets'][0]['applicationAttribute'] == 'app'


@pytest.mark.parametrize('other', [
    'other = FastAPI(exception_handlers={RequestValidationError: lambda request, exc: None})\n',
    'other = FastAPI()\nother.add_middleware(object)\n',
    'other = FastAPI()\nobserved_handlers = other.exception_handlers\n',
])
def test_other_app_configuration_does_not_suppress_the_warning(tmp_path, other):
    issues, _ = packet_for(tmp_path, APP + '\n' + other)
    assert any(r['reasonCode'] == 'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS' for r in issues)


def test_custom_mapping_on_the_actual_app_remains_for_measured_validation(tmp_path):
    text = APP.replace('app = FastAPI()', 'app = FastAPI(exception_handlers={RequestValidationError: handler})')
    (tmp_path / 'main.py').write_text(text)
    assert not inspect_signup_quality(tmp_path, get_scenario(SCN_001_ID).planner_contract())


def test_http_only_constructor_mapping_does_not_count_as_validation_protection(tmp_path):
    text = APP.replace('app = FastAPI()', 'app = FastAPI(exception_handlers={HTTPException: handler})')
    text = 'from fastapi import HTTPException\n' + text
    _, packet = packet_for(tmp_path, text)
    target, = packet['sourceRepairTargets']
    assert target['observed']['httpExceptionHandlerDetected'] is True
    assert target['observed']['requestValidationHandlerDetected'] is False


def test_aliased_request_validation_registration_is_recognized(tmp_path):
    text = APP.replace('import RequestValidationError', 'import RequestValidationError as InputError')
    text += SAFE_HANDLER.replace('(RequestValidationError)', '(InputError)')
    (tmp_path / 'main.py').write_text(text)
    assert not inspect_signup_quality(tmp_path, get_scenario(SCN_001_ID).planner_contract())


@pytest.mark.parametrize('context', [
    {'applicationAttribute': '../private-raw', 'httpExceptionHandlerDetected': True,
     'requestValidationHandlerDetected': False, 'httpExceptionHandlerLines': [1]},
    {'applicationAttribute': 'app', 'httpExceptionHandlerDetected': 'private-raw',
     'requestValidationHandlerDetected': False, 'httpExceptionHandlerLines': [1]},
])
def test_untrusted_context_values_never_enter_repair_targets(context):
    row = {'fieldPath': '/source', 'reasonCode': 'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS',
           'sourcePath': 'main.py', 'repairSourcePath': 'main.py', 'lineNumber': 1,
           'validationErrorContext': context}
    rejection = checked_rejection({'code': 'READY_SIGNUP_QUALITY_REVIEW', 'issues': [row]})
    packet = feedback(rejection['code'], issues=rejection['issues'], remaining=1)
    assert 'private-raw' not in str(packet)
    assert not packet['hostCorrection'].get('sourceRepairTargets')


@pytest.mark.parametrize('single_phase', [None, 'IMPLEMENTING', 'FIXING'],
                         ids=['multi', 'single-implementing', 'single-fixing'])
def test_exact_target_reaches_model_then_actual_hash_bound_repair(tmp_path, single_phase):
    import test_llm_runtime as fixtures
    from test_llm_runtime import FakeProvider, FakeToolExecutor, object_schema, text_response, tool_response
    from agents.core.identity import SingleAgentIdentity, SinglePhase
    from agents.llm.engine import LLMEngine
    from agents.llm.corrections import development_check
    from agents.roles.developer_contract import build_developer_output_contract, validate_developer_decision
    from agents.runtime.signup_quality_precheck import signup_quality_precheck_for
    from agents.single.contracts import build_single_draft_contract
    from agents.single.prompts import PreparedSinglePrompt, build_single_system_prompt
    f = fixtures.LLMRuntimeTests()
    f.setUp()
    path = tmp_path / 'main.py'
    path.write_text(HTTP_ONLY)
    ready = {'kind': 'READY', 'summary': 'Candidate for Host validation.', 'questions': [], 'rejection': None}
    targets = []

    def use_feedback(request):
        for item in json.loads(request.input_items_json):
            if item.get('role') != 'user' or not isinstance(item.get('content'), str):
                continue
            value = json.loads(item['content']).get('hostCorrection', {})
            targets.extend(value.get('sourceRepairTargets', []))
        target, = targets
        assert target['exceptionType'] == 'fastapi.exceptions.RequestValidationError'
        assert target['preserveExistingHttpExceptionHandler'] is True
        return tool_response(f.call('read-validation-target', path=target['toolPath']))

    def write_repair(_request):
        target, = targets
        content = (path.read_text() + '\n' + target['importExample'] + '\n' + target['registrationExample']
            + '\nasync def request_validation_rejection(request, exc):\n'
              '    return JSONResponse(status_code=422, content={"detail": "invalid request"})\n')
        return tool_response(f.call('write-validation-target', name='write_source_file', path=target['toolPath'],
                                    content=content, expectedSha256=sha256(path.read_bytes()).hexdigest()))

    def execute(call, arguments, _context):
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
    if single_phase:
        phase, role = SinglePhase(single_phase), SingleAgentIdentity.SINGLE_AGENT
        draft = build_single_draft_contract(phase, scenario=scenario)
        prompt = PreparedSinglePrompt(phase, draft, build_single_system_prompt(), json.dumps({
            'trusted': {'phase': phase.value}, 'contextId': 'offline', 'history': [], 'taskInput': {}}), 'offline')
        output, validate = draft.output, draft.validate
    else:
        phase, role, prompt = None, f.role, f.prompt
        output, validate = build_developer_output_contract(), validate_developer_decision
    provider = FakeProvider(text_response(json.dumps(ready)), use_feedback, write_repair, text_response(json.dumps(ready)))
    probe = signup_quality_precheck_for(SimpleNamespace(source_root=tmp_path), scenario.planner_contract())
    engine = LLMEngine(role=role, single_phase=phase, provider=provider, tools=tools,
                       tool_executor=FakeToolExecutor(execute, execute))
    budget = f.budget(max_output_corrections=1, max_model_calls=4, max_tool_calls=2)
    result = asyncio.run(engine.run(prompt=prompt, model=f.model, output=output, budget=budget,
        workspace_id=f.workspace_id, source_progress_check=lambda: True,
        output_check=development_check(validate, signup_quality_check=probe)))
    assert result.data == ready and not probe()
    assert path.read_text().startswith(HTTP_ONLY)
    assert (budget.model_calls, budget.tool_calls) == (4, 2)
