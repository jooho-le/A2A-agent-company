"""The archived simple validation leak is caught without requiring one safe design."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.llm.corrections import checked_rejection, development_check, feedback
from agents.runtime.output_safety_precheck import inspect_output_safety, output_safety_precheck_for, REASON
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario

SOURCE = '''from fastapi import FastAPI
from pydantic import BaseModel
app = FastAPI()
class Signup(BaseModel):
    email: str
    password: str
@app.post('/api/auth/signup')
def signup(payload: Signup):
    return {'created': True}
'''


def contract():
    return get_scenario(SCN_001_ID).planner_contract()


def write(root, source=SOURCE):
    (root / "main.py").write_text(source)


def test_plain_request_model_default_errors_are_reported_without_execution(tmp_path):
    write(tmp_path, SOURCE + '\nraise RuntimeError("must-never-execute-on-host")\n')
    assert inspect_output_safety(tmp_path, contract()) == [{"fieldPath": "/source", "reasonCode": REASON}]


@pytest.mark.parametrize("safe_variant", [
    SOURCE + '\n@app.exception_handler(RequestValidationError)\nasync def safe_error(request, exc):\n    return JSONResponse(status_code=422, content={"detail":"invalid request"})\n',
    SOURCE.replace('app = FastAPI()', 'app = FastAPI(exception_handlers={RequestValidationError: safe_error})'),
    SOURCE + '\n@app.middleware("http")\nasync def sanitize(request, call_next):\n    return await call_next(request)\n',
    SOURCE + '\napp.exception_handlers.update({RequestValidationError: safe_error})\n',
    SOURCE + '\ninstall_handlers(app)\n',
    SOURCE.replace('password: str', 'password: SecretStr'),
    SOURCE.replace('payload: Signup', 'request: Request'),
    SOURCE.replace('app = FastAPI()', 'app = make_safe_app()'),
    SOURCE.replace('app = FastAPI()', 'FastAPI = safe_factory\napp = FastAPI()'),
])
def test_alternative_or_unknown_designs_are_left_to_measured_security_checks(tmp_path, safe_variant):
    write(tmp_path, safe_variant)
    assert inspect_output_safety(tmp_path, contract()) == []


def test_other_scenarios_and_absent_public_policy_are_not_reinterpreted(tmp_path):
    write(tmp_path)
    altered = deepcopy(contract())
    altered['scenarioKey'] = 'OTHER'
    assert inspect_output_safety(tmp_path, altered) == []
    altered = contract()
    altered['requirements'] = [r for r in altered['requirements'] if r['key'] != 'REQ-006']
    assert inspect_output_safety(tmp_path, altered) == []


def test_symlink_and_oversized_files_are_not_analyzed(tmp_path):
    actual = tmp_path / 'other.py'
    actual.write_text(SOURCE)
    (tmp_path / 'main.py').symlink_to(actual)
    assert inspect_output_safety(tmp_path, contract()) == []
    (tmp_path / 'main.py').unlink()
    write(tmp_path, SOURCE + '#' * 128_001)
    assert inspect_output_safety(tmp_path, contract()) == []


def test_malformed_source_remains_a_build_problem_not_a_security_verdict(tmp_path):
    write(tmp_path, 'def broken(')
    assert inspect_output_safety(tmp_path, contract()) == []


def test_same_pre_ready_correction_can_be_used_by_single_and_multi(tmp_path):
    write(tmp_path)
    probe = output_safety_precheck_for(SimpleNamespace(source_root=tmp_path), contract())
    check = development_check(lambda _: None, output_safety_check=probe)
    rejection = checked_rejection(check({'kind': 'READY'}, {'write_source_file'}))
    assert rejection['code'] == 'READY_OUTPUT_SAFETY_REVIEW'
    packet = feedback(rejection['code'], issues=rejection['issues'], remaining=1)
    assert 'exc.errors()' in packet['hostCorrection']['instruction']
    write(tmp_path, SOURCE + '\n@app.exception_handler(RequestValidationError)\nasync def sanitized(request, exc):\n    return JSONResponse(status_code=422, content={"detail":"invalid request"})\n')
    assert check({'kind': 'READY'}, {'write_source_file'}) is None


def test_fake_checkpoint_and_disabled_guard_keep_existing_submission_rules():
    assert output_safety_precheck_for(object(), contract())() == []
    assert development_check(lambda _: None)({'kind':'READY'}, {'write_source_file'}) is None
