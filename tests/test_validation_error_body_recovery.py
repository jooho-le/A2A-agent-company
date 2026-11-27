"""Handwritten framework reproductions; saved Agent apps are never imported."""
from secrets import token_urlsafe

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr

from agents.llm.corrections import checked_rejection, feedback, validation_error_repair_targets
from test_signup_quality_precheck import APP, SAFE_HANDLER, inspect


@pytest.mark.parametrize('signature', [
    'data: dict', 'data: dict[str, str]', 'data: list[dict]',
    'data = Body(...)', '*, data: dict',
])
def test_dictionary_and_explicit_body_inputs_get_app_level_validation_repair(tmp_path, signature):
    source = ('from fastapi import FastAPI, Body, HTTPException\n'
              'from fastapi.responses import JSONResponse\napp=FastAPI()\n'
              "@app.post('/api/auth/signup')\ndef signup(" + signature + "): return {}\n"
              '@app.exception_handler(HTTPException)\nasync def http_error(request, exc):\n'
              " return JSONResponse({'detail': 'invalid'}, status_code=400)\n"
              '@app.exception_handler(Exception)\nasync def other_error(request, exc):\n'
              " return JSONResponse({'detail': 'invalid'}, status_code=400)\n")
    issues = inspect(tmp_path, {'main.py': source})
    selected = [i for i in issues if i['reasonCode'] == 'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS']
    assert len(selected) == 1
    rejection = checked_rejection({'code': 'READY_SIGNUP_QUALITY_REVIEW', 'issues': selected})
    target, = validation_error_repair_targets(rejection['issues'])
    assert target['toolPath'] == 'source/main.py'
    assert target['preserveExistingHttpExceptionHandler']
    packet = feedback(rejection['code'], issues=rejection['issues'], remaining=1)
    assert 'RequestValidationError' in str(packet)
    assert source not in str(packet)


def test_secret_field_type_does_not_protect_an_invalid_whole_body(tmp_path):
    source = APP.replace('from pydantic import BaseModel', 'from pydantic import BaseModel, SecretStr')
    source = source.replace('password: str', 'password: SecretStr')
    assert any(i['reasonCode'] == 'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS'
               for i in inspect(tmp_path, {'main.py': source}))
    assert not any(i['reasonCode'] == 'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS'
                   for i in inspect(tmp_path, {'main.py': source + SAFE_HANDLER}))


def test_query_dependency_and_manual_request_are_not_mistaken_for_typed_body(tmp_path):
    source = ("from fastapi import FastAPI, Depends, Request\napp=FastAPI()\n"
              "@app.post('/api/auth/signup')\ndef signup(data: dict=Depends(load_data)): return {}\n")
    assert not any(i['reasonCode'] == 'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS'
                   for i in inspect(tmp_path, {'main.py': source}))


def test_raw_validation_body_in_a_custom_handler_is_still_rejected(tmp_path):
    source = APP + SAFE_HANDLER.replace("{'detail': 'invalid request'}", "{'detail': exc.body}")
    issues = inspect(tmp_path, {'main.py': source})
    selected = [i for i in issues if i['reasonCode'] == 'VALIDATION_ERRORS_FORWARD_INPUT']
    assert selected
    selected = checked_rejection({'code': 'READY_SIGNUP_QUALITY_REVIEW', 'issues': selected})['issues']
    assert validation_error_repair_targets(selected)[0]['exceptionType'].endswith('RequestValidationError')


class MaskedBody(BaseModel):
    email: str
    password: SecretStr


@pytest.mark.parametrize('body_type', ['dictionary', 'secret_model'])
@pytest.mark.parametrize('safe', [False, True])
def test_real_framework_validation_response_never_needs_to_echo_input(body_type, safe):
    app = FastAPI()

    @app.exception_handler(HTTPException)
    async def business_error(_request, exc):
        return JSONResponse({'detail': 'invalid'}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(_request, _exc):
        return JSONResponse({'detail': 'invalid'}, status_code=400)

    if safe:
        @app.exception_handler(RequestValidationError)
        async def validation_error(_request, _exc):
            return JSONResponse({'detail': 'invalid request'}, status_code=422)

    if body_type == 'dictionary':
        @app.post('/api/auth/signup')
        def signup(data: dict):
            if data.get('email') == 'duplicate@example.com':
                raise HTTPException(status_code=409, detail='DUPLICATE_EMAIL')
            return {'accepted': True}
    else:
        @app.post('/api/auth/signup')
        def signup(data: MaskedBody):
            if data.email == 'duplicate@example.com':
                raise HTTPException(status_code=409, detail='DUPLICATE_EMAIL')
            return {'accepted': True}

    sample = token_urlsafe(18)
    payload = {'email': 'member@example.com', 'password': sample}
    with TestClient(app) as client:
        normal = client.post('/api/auth/signup', json=payload)
        malformed = client.post('/api/auth/signup', json=[payload])
        duplicate = client.post('/api/auth/signup', json={**payload, 'email': 'duplicate@example.com'})
    exposed = sample in malformed.text
    normal_exposed = sample in normal.text
    assert normal.status_code == 200 and not normal_exposed
    assert malformed.status_code == 422
    assert duplicate.status_code == 409 and duplicate.json() == {'detail': 'invalid'}
    assert exposed is (not safe), 'VALIDATION_BODY_EXPOSURE_BOUNDARY'
