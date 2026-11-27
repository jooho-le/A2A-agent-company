"""Source coordinates and canonical-input order, without importing generated apps."""
from copy import deepcopy

import pytest

from agents.llm.corrections import checked_rejection, feedback
from agents.runtime.signup_quality_precheck import inspect_signup_quality
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario
from test_signup_quality_precheck import APP, SAFE_HANDLER


CONTRACT = get_scenario(SCN_001_ID).planner_contract()
BAD = '''
import email_validator
def normalize_email(email: str):
    try:
        value = email_validator.validate_email(email, check_deliverability=False)
    except email_validator.EmailNotValidError:
        raise ValueError("PRIVATE_LITERAL")
    return value.email.lower()
'''
CALLER = APP.replace("return {'created': True}", 'return normalize_email(payload.email)')


def inspect(tmp_path, helper=BAD, caller=CALLER, contract=CONTRACT):
    (tmp_path / 'main.py').write_text(caller + SAFE_HANDLER + helper)
    return [r for r in inspect_signup_quality(tmp_path, contract)
            if r['reasonCode'] == 'EMAIL_VALIDATED_BEFORE_CANONICAL_TRIM']


def test_module_qualified_parser_and_helper_parameter_are_tracked(tmp_path):
    row, = inspect(tmp_path)
    context = row['emailNormalizationContext']
    assert context['sourceRef'] == 'source/main.py::normalize_email'
    assert context['parser'] == 'email_validator.validate_email'
    assert context['trimBeforeValidationObserved'] is False
    packet = checked_rejection({'code': 'READY_SIGNUP_QUALITY_REVIEW', 'issues': [row]})
    target, = feedback(packet['code'], issues=packet['issues'], remaining=1)['hostCorrection']['sourceRepairTargets']
    assert target['sourceRef'] == context['sourceRef']
    assert target['requiredOrder'] == ['TRIM_WHITESPACE', 'CASE_NORMALIZATION', 'FORMAT_VALIDATION', 'LOOKUP_AND_INSERT']
    assert target['toolPath'] == 'source/main.py'
    assert 'PRIVATE_LITERAL' not in str(target)


@pytest.mark.parametrize('library,call', [
    ('import email_validator as ev', 'ev.validate_email(email, check_deliverability=False)'),
    ('from email_validator import validate_email as parse', 'parse(email, check_deliverability=False)'),
    ('import email_validator', 'email_validator.validate_email(email=email, check_deliverability=False)'),
])
def test_public_import_aliases_and_keyword_arguments(tmp_path, library, call):
    helper = BAD.replace('import email_validator', library).replace(
        'email_validator.validate_email(email, check_deliverability=False)', call)
    assert inspect(tmp_path, helper)


@pytest.mark.parametrize('preprocess', ['email = email.strip().lower()', 'email = email.strip()',
    'clean = email.strip().lower()\n    email = clean'])
def test_preprocessing_before_the_parser_is_not_rejected(tmp_path, preprocess):
    helper = BAD.replace('    try:', '    ' + preprocess + '\n    try:')
    assert not inspect(tmp_path, helper)


def test_trim_after_validation_or_on_an_unrelated_value_does_not_clear_warning(tmp_path):
    assert inspect(tmp_path, BAD.replace('    return value.email.lower()',
                                       '    email = email.strip()\n    return value.email.lower()'))
    assert inspect(tmp_path, BAD.replace('    try:', '    other = "private".strip()\n    try:'))


def test_safe_caller_can_pass_the_canonical_value_to_a_helper(tmp_path):
    caller = CALLER.replace('normalize_email(payload.email)', 'normalize_email(payload.email.strip().lower())')
    assert not inspect(tmp_path, caller=caller)


def test_cross_module_helper_and_two_hop_argument_alias(tmp_path):
    (tmp_path / 'main.py').write_text(CALLER + SAFE_HANDLER + '\nfrom helpers import normalize_email\n')
    (tmp_path / 'helpers.py').write_text(BAD.replace('def normalize_email(email: str):',
        'def normalize_email(value):\n    return check(value)\ndef check(email):'))
    rows = [r for r in inspect_signup_quality(tmp_path, CONTRACT)
            if r['reasonCode'] == 'EMAIL_VALIDATED_BEFORE_CANONICAL_TRIM']
    assert rows[0]['emailNormalizationContext']['sourceRef'] == 'source/helpers.py::check'


@pytest.mark.parametrize('helper', [
    BAD.replace('def normalize_email(email: str):', 'def normalize_email(email: str, email_validator=None):'),
    BAD.replace('def normalize_email(email: str):', 'email_validator = object()\ndef normalize_email(email: str):'),
])
def test_shadowed_library_names_do_not_become_public_api_findings(tmp_path, helper):
    assert not inspect(tmp_path, helper)


def test_unknown_prevalidator_is_left_to_measured_validation(tmp_path):
    caller = CALLER.replace('    password: str', '    password: str\n'
        '    @field_validator("email", mode="before")\n'
        '    @classmethod\n    def clean(cls, value):\n        return value.strip().lower()')
    caller = caller.replace('from pydantic import BaseModel', 'from pydantic import BaseModel, field_validator')
    assert not inspect(tmp_path, caller=caller)


def test_password_validator_or_unrelated_model_config_does_not_hide_email_order(tmp_path):
    caller = CALLER.replace('    password: str', '    password: str\n'
        '    @field_validator("password")\n'
        '    @classmethod\n    def password_length(cls, value):\n        return value')
    caller = caller.replace('from pydantic import BaseModel', 'from pydantic import BaseModel, field_validator')
    assert inspect(tmp_path, caller=caller)
    caller = CALLER.replace('    password: str', '    password: str\n    model_config = ConfigDict(extra="forbid")')
    assert inspect(tmp_path, caller=caller)


def test_model_config_that_can_strip_email_is_not_assumed_unsafe(tmp_path):
    caller = CALLER.replace('    password: str', '    password: str\n    model_config = ConfigDict(str_strip_whitespace=True)')
    assert not inspect(tmp_path, caller=caller)


def test_unrelated_parser_helper_without_email_input_is_not_flagged(tmp_path):
    assert not inspect(tmp_path, caller=APP)


def test_db_only_edit_or_comment_does_not_change_the_normalization_review_revision(tmp_path):
    old, = inspect(tmp_path)
    edited = CALLER.replace('return normalize_email(payload.email)', 'db = None\n    return normalize_email(payload.email)')
    current, = inspect(tmp_path, caller=edited)
    assert old['reviewRevision'] == current['reviewRevision']
    comment, = inspect(tmp_path, helper=BAD + '\n# supposedly fixed\n')
    assert comment['reviewRevision'] == old['reviewRevision']


def test_contract_without_trim_does_not_create_a_new_rule(tmp_path):
    contract = deepcopy(CONTRACT)
    contract['emailPolicy']['trimWhitespace'] = False
    assert not inspect(tmp_path, contract=contract)


@pytest.mark.parametrize('single_phase', [None, 'IMPLEMENTING', 'FIXING'],
                         ids=['multi', 'single-implementing', 'single-fixing'])
def test_exact_function_guidance_reaches_model_and_hash_bound_repair(tmp_path, single_phase):
    import asyncio
    from hashlib import sha256
    import json
    import test_llm_runtime as fixtures
    from test_llm_runtime import FakeProvider, FakeToolExecutor, object_schema, text_response, tool_response
    from agents.core.identity import SingleAgentIdentity, SinglePhase
    from agents.llm.engine import LLMEngine
    from agents.llm.corrections import development_check
    from agents.roles.developer_contract import build_developer_output_contract, validate_developer_decision
    from agents.single.contracts import build_single_draft_contract
    from agents.single.prompts import PreparedSinglePrompt, build_single_system_prompt
    f = fixtures.LLMRuntimeTests()
    f.setUp()
    inspect(tmp_path)
    path = tmp_path / 'main.py'
    initial = path.read_text()
    ready = {'kind': 'READY', 'summary': 'Candidate for measured revalidation.', 'questions': [], 'rejection': None}
    targets = []

    def use_feedback(request):
        for item in json.loads(request.input_items_json):
            if item.get('role')=='user' and isinstance(item.get('content'), str):
                targets.extend(json.loads(item['content']).get('hostCorrection', {}).get('sourceRepairTargets', []))
        target, = targets
        assert target['sourceRef'] == 'source/main.py::normalize_email'
        assert target['parser'] == 'email_validator.validate_email'
        return tool_response(f.call('read-normalizer', path=target['toolPath']))

    def write_repair(_request):
        repaired = initial.replace('def normalize_email(email: str):\n',
                                   'def normalize_email(email: str):\n    email = email.strip().lower()\n')
        return tool_response(f.call('write-normalizer', name='write_source_file', path=targets[0]['toolPath'],
            expectedSha256=sha256(path.read_bytes()).hexdigest(), content=repaired))

    def execute(call, args, _context):
        assert args['path'] == 'source/main.py'
        if call.name == 'read_project_file':
            return {'content':path.read_text(), 'sha256':sha256(path.read_bytes()).hexdigest()}
        assert args['expectedSha256'] == sha256(path.read_bytes()).hexdigest()
        path.write_text(args['content'])
        return {'changed':True}
    tools = (f.tool('read_project_file', output_schema=object_schema({
        'content':{'type':'string'}, 'sha256':{'type':'string'}}), source_output_fields=('content',)),
        f.tool('write_source_file', input_schema=object_schema({k:{'type':'string'} for k in
            ('workspaceId','path','content','expectedSha256')}), output_schema=object_schema({'changed':{'type':'boolean'}}),
            source_argument_fields=('content',)))
    if single_phase:
        phase, role = SinglePhase(single_phase), SingleAgentIdentity.SINGLE_AGENT
        draft=build_single_draft_contract(phase, scenario=get_scenario(SCN_001_ID))
        prompt=PreparedSinglePrompt(phase, draft, build_single_system_prompt(), json.dumps({
            'trusted':{'phase':phase.value}, 'contextId':'offline', 'history':[], 'taskInput':{}}), 'offline')
        output,validate=draft.output,draft.validate
    else:
        phase,role,prompt=None,f.role,f.prompt
        output,validate=build_developer_output_contract(),validate_developer_decision
    provider=FakeProvider(text_response(json.dumps(ready)), use_feedback, write_repair, text_response(json.dumps(ready)))
    engine=LLMEngine(role=role, single_phase=phase, provider=provider, tools=tools,
                     tool_executor=FakeToolExecutor(execute,execute))
    budget=f.budget(max_model_calls=4,max_tool_calls=2,max_output_corrections=1)
    checker=lambda: inspect_signup_quality(tmp_path, CONTRACT)
    result=asyncio.run(engine.run(prompt=prompt,model=f.model,output=output,budget=budget,
        workspace_id=f.workspace_id,source_progress_check=lambda:True,
        output_check=development_check(validate,signup_quality_check=checker)))
    assert result.data==ready and checker()==[]
    assert (budget.model_calls,budget.tool_calls)==(4,2)
    assert 'email = email.strip().lower()' in path.read_text()


def test_correction_context_cannot_smuggle_literals_or_another_file(tmp_path):
    row,=inspect(tmp_path)
    row['emailNormalizationContext']['private']='DO_NOT_COPY'
    packet=checked_rejection({'code':'READY_SIGNUP_QUALITY_REVIEW','issues':[row]})
    output=feedback(packet['code'],issues=packet['issues'],remaining=1)
    assert 'DO_NOT_COPY' not in str(output)
    row['emailNormalizationContext']['sourceRef']='source/other.py::normalize_email'
    packet=checked_rejection({'code':'READY_SIGNUP_QUALITY_REVIEW','issues':[row]})
    assert not feedback(packet['code'],issues=packet['issues'],remaining=1)['hostCorrection'].get('sourceRepairTargets')
