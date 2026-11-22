"""Public source-review paths, without importing the generated application."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from agents.llm.corrections import development_check, checked_rejection, feedback
from agents.runtime.signup_quality_precheck import inspect_signup_quality, signup_quality_precheck_for
from orchestrator.domain.scenario_registry import get_scenario, SCN_001_ID


def contract():
    return get_scenario(SCN_001_ID).planner_contract()


APP = '''from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
app = FastAPI()
class Payload(BaseModel):
    email: str
    password: str
@app.post('/api/auth/signup')
def signup(payload: Payload):
    return {'created': True}
'''
SAFE_HANDLER = '''
@app.exception_handler(RequestValidationError)
async def rejected(request, exc):
    return JSONResponse(status_code=422, content={'detail': 'invalid request'})
'''


def inspect(root, files, configuration=None):
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return inspect_signup_quality(root, configuration or contract())


def test_router_and_separate_request_model_do_not_disable_validation_review(tmp_path):
    issues = inspect(tmp_path, {
        'main.py': 'from fastapi import FastAPI\nfrom routes import router\napp=FastAPI()\napp.include_router(router)\n',
        'routes.py': 'from fastapi import APIRouter\nfrom schema import Payload\nrouter=APIRouter()\n'
                     '@router.post("/api/auth/signup")\ndef signup(payload: Payload): return {}\n',
        'schema.py': 'from pydantic import BaseModel\nclass Payload(BaseModel):\n email:str\n password:str\n',
    })
    assert any(r['reasonCode'] == 'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS' and r.get('sourcePath') == 'routes.py'
               for r in issues)


def test_relative_model_imports_are_resolved_without_importing_modules(tmp_path):
    issues = inspect(tmp_path, {
        'main.py': 'from fastapi import FastAPI\nfrom api.routes import router\napp=FastAPI()\napp.include_router(router)\n',
        'api/__init__.py': '',
        'api/schema.py': 'from pydantic import BaseModel\nclass Payload(BaseModel):\n email:str\n password:str\n',
        'api/routes.py': 'from fastapi import APIRouter\nfrom .schema import Payload\nrouter=APIRouter()\n'
                         '@router.post("/api/auth/signup")\ndef signup(payload: Payload): return {}\n'
                         'raise RuntimeError("never execute generated code")\n',
    })
    assert any(r.get('sourcePath') == 'api/routes.py' for r in issues)


def test_safe_validation_handler_does_not_hide_database_exception_logging(tmp_path):
    source = APP + SAFE_HANDLER + '''
import logging
def save(db):
    try:
        db.commit()
    except Exception as exc:
        logging.error('storage failed: %s', exc, exc_info=True)
'''
    issues = inspect(tmp_path, {'main.py': source})
    assert any(r['reasonCode'] == 'EXCEPTION_LOG_MAY_EXPOSE_DB_PARAMETERS' for r in issues)
    assert not any(r['reasonCode'] == 'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS' for r in issues)
    assert 'storage failed' not in str(issues)


def test_raw_validation_errors_remain_reviewable_with_a_registered_handler(tmp_path):
    source = APP + SAFE_HANDLER.replace("{'detail': 'invalid request'}", "{'detail': exc.errors()}")
    issues = inspect(tmp_path, {'main.py': source})
    assert any(r['reasonCode'] == 'VALIDATION_ERRORS_FORWARD_INPUT' for r in issues)


def test_unrelated_exception_handler_does_not_suppress_default_validation_hint(tmp_path):
    source = APP + SAFE_HANDLER.replace('RequestValidationError)', 'ValueError)')
    assert any(r['reasonCode'] == 'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS'
               for r in inspect(tmp_path, {'main.py': source}))


def test_positional_response_exception_text_is_not_ignored(tmp_path):
    source = APP + SAFE_HANDLER.replace("status_code=422, content={'detail': 'invalid request'}",
                                        "{'detail': str(exc)}, status_code=422")
    assert any(r['reasonCode'] == 'VALIDATION_ERRORS_FORWARD_INPUT'
               for r in inspect(tmp_path, {'main.py': source}))


@pytest.mark.parametrize('safe', [False, True])
def test_validate_after_trim_is_distinguished_from_validating_raw_request(tmp_path, safe):
    endpoint = '''from email_validator import validate_email
from fastapi import APIRouter
router = APIRouter()
@router.post('/api/auth/signup')
def signup(payload):
'''
    endpoint += '    payload.email = payload.email.strip().lower()\n' if safe else ''
    endpoint += '    checked = validate_email(payload.email, check_deliverability=False)\n    return {}\n'
    issues = inspect(tmp_path, {'main.py': APP + SAFE_HANDLER + '\nfrom routes import router\napp.include_router(router)\n',
                               'routes.py': endpoint})
    assert any(r['reasonCode'] == 'EMAIL_VALIDATED_BEFORE_CANONICAL_TRIM' for r in issues) is not safe


@pytest.mark.parametrize('conflict_handled', [False, True])
def test_commit_race_with_generic_500_is_reviewed_even_with_prelookup(tmp_path, conflict_handled):
    source = APP[:APP.index('@app.post')] + '''from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
@app.post('/api/auth/signup')
def signup(payload: Payload, db):
    if db.lookup(payload.email):
        raise HTTPException(status_code=409)
    try:
        db.commit()
'''
    if conflict_handled:
        source += '    except IntegrityError:\n        db.rollback()\n        raise HTTPException(status_code=409)\n'
    source += '    except Exception:\n        db.rollback()\n        raise HTTPException(status_code=500)\n'
    issues = inspect(tmp_path, {'main.py': source + SAFE_HANDLER})
    assert any(r['reasonCode'] == 'COMMIT_CONFLICT_MAPPED_TO_SERVER_ERROR' for r in issues) is not conflict_handled


def test_safe_error_messages_and_value_free_logs_do_not_trigger_review(tmp_path):
    source = APP + SAFE_HANDLER + '''
import logging
from fastapi import HTTPException
def save(db):
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logging.error('STORAGE_OPERATION_FAILED')
        raise HTTPException(status_code=500, detail='STORAGE_OPERATION_FAILED')
'''
    assert inspect(tmp_path, {'main.py': source}) == []


def test_source_links_survive_feedback_but_source_text_never_does(tmp_path):
    inspect(tmp_path, {'main.py': APP})
    probe = signup_quality_precheck_for(SimpleNamespace(source_root=tmp_path), contract())
    check = development_check(lambda _: None, signup_quality_check=probe)
    rejection = checked_rejection(check({'kind': 'READY'}, {'write_source_file'}))
    assert rejection['code'] == 'READY_SIGNUP_QUALITY_REVIEW'
    packet = feedback(rejection['code'], issues=rejection['issues'], remaining=1)
    assert 'EXCEPTION_LOG_MAY_EXPOSE_DB_PARAMETERS' in packet['hostCorrection']['instruction']
    assert APP not in str(packet)


def test_canonicalization_through_a_local_alias_is_tracked(tmp_path):
    endpoint = '''from email_validator import validate_email
def check(payload):
    email = payload.email
    email = email.strip().lower()
    return validate_email(email, check_deliverability=False)
'''
    assert not any(r['reasonCode'] == 'EMAIL_VALIDATED_BEFORE_CANONICAL_TRIM' for r in
                   inspect(tmp_path, {'main.py': APP + SAFE_HANDLER + '\nfrom helpers import check\n', 'helpers.py': endpoint}))
    (tmp_path / 'helpers.py').write_text(endpoint.replace('    email = email.strip().lower()\n', ''))
    assert any(r['reasonCode'] == 'EMAIL_VALIDATED_BEFORE_CANONICAL_TRIM'
               for r in inspect_signup_quality(tmp_path, contract()))


def test_unrelated_contracts_and_symlink_modules_are_not_reinterpreted(tmp_path):
    other = deepcopy(contract())
    other['scenarioKey'] = 'OTHER'
    assert inspect(tmp_path, {'main.py': APP}, other) == []
    outside = tmp_path / 'outside.py'
    outside.write_text(APP)
    (tmp_path / 'main.py').unlink()
    (tmp_path / 'main.py').symlink_to(outside)
    assert inspect_signup_quality(tmp_path, contract()) == []
