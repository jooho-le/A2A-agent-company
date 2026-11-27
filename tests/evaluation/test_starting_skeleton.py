"""SCN-001 시작 코드 골격 초안(examples/starting-source/scn001-signup)이 숨긴 시험지 연결 규격과 맞는지 시험한다.

확인하는 것:
  - 골격에 시험지·평가 코드가 없고, 요구사항 구현(User 모델·가입 API)도 없는가?
  - 골격만 채점하면 모든 검사가 FAIL이 아니라 미검증(User 모델 없음)인가?
  - 골격에 최소 구현을 더하면 공식 최종 시험지의 모든 검사가 PASS인가(이 골격과 규격으로 통과 가능한 구현이 있는가)?
  - 골격 자체 점검 시험이 돈다.
골격과 최소 구현은 신뢰된 합성 코드라서 dev unittest 실행기로 이 컴퓨터에서 돌린다(생성 코드가 아님).
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from evaluation import protected_tests as pt
from evaluation.aggregation import Status
from evaluation.check_adapters import check_results_from_unit_report
from tests.evaluation import signup_service
from tests.evaluation.test_protected_signup import (CONTRACT, RUNNER, RUNTIME, SUITE, _RUN, _set_read_only, by_id,
                                                    uid)

SKELETON = Path(__file__).resolve().parents[2] / 'examples/starting-source/scn001-signup'
CODE_FILES = ('main.py', 'database.py', 'models.py', 'tests/test_health.py')


def skeleton_files():
    return {name: (SKELETON / name).read_text(encoding='utf-8') for name in CODE_FILES}


def with_minimal_signup():
    # 개발 에이전트가 할 일을 흉내 낸 최소 구현: User 모델을 더하고, 가입 API를 붙인다(합성 정상 서비스의 부분을 재사용).
    files = skeleton_files()
    good = signup_service.build('good')
    files['models.py'] += '\n' + good['models.py'].split('from database import Base\n', 1)[1]
    files['models.py'] = 'from sqlalchemy import Column, Integer, String\n' + files['models.py']
    for name in ('schemas.py', 'security.py', 'routers/__init__.py', 'routers/auth.py'):
        files[name] = good[name]
    files['main.py'] += ('\n\nfrom fastapi import Request  # noqa: E402\n'
                         'from fastapi.exceptions import RequestValidationError  # noqa: E402\n'
                         'from fastapi.responses import JSONResponse  # noqa: E402\n\n'
                         'from routers.auth import router as auth_router  # noqa: E402\n\n'
                         'app.include_router(auth_router)\n\n\n'
                         '@app.exception_handler(RequestValidationError)\n'
                         'async def validation_error_handler(request: Request, exc: RequestValidationError):\n'
                         '    errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]\n'
                         '    return JSONResponse(status_code=422, content={"detail": errors})\n')
    return files


def run_exam(files, exam='final'):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for path, content in files.items():
            (root / 'snapshot' / path).parent.mkdir(parents=True, exist_ok=True)
            (root / 'snapshot' / path).write_text(content, encoding='utf-8')
        for path, content in pt.protected_files(exam).items():
            (root / 'inputs' / path).parent.mkdir(parents=True, exist_ok=True)
            (root / 'inputs' / path).write_text(content, encoding='utf-8')
        _set_read_only(root / 'snapshot', True)
        try:
            completed = subprocess.run(
                [sys.executable, '-I', '-c', _RUN, str(RUNNER), str(root / 'inputs/tests/signup'), str(root / 'snapshot'),
                 SUITE['scopes'][exam]['pattern']], capture_output=True, text=True, timeout=600, cwd=root)
        finally:
            _set_read_only(root / 'snapshot', False)
    output = json.loads(completed.stdout)
    inputs = pt.evaluation_inputs(CONTRACT, exam)
    names = {check['checkId']: check['key'] for check in SUITE['checks']}
    results = by_id(check_results_from_unit_report(output['report'], inputs['unit_cases'], evidence_id=uid(70)))
    return {names[check_id]: result for check_id, result in results.items()}


def test_skeleton_has_no_exam_no_evaluation_code_and_no_requirement_implementation():
    files = {name: text.encode('utf-8') for name, text in skeleton_files().items()}
    files['README.md'] = (SKELETON / 'README.md').read_bytes()
    assert not pt.exam_exposure(files)
    code = ''.join(skeleton_files().values())
    assert 'class User' not in code and '/api/auth/signup' not in code and 'argon2' not in code
    assert 'def get_db' in code and 'DATABASE_URL' in code and 'app = FastAPI(' in code


@pytest.mark.skipif(not RUNTIME, reason='fastapi·httpx·sqlalchemy·argon2-cffi가 있어야 실행 시험을 돈다')
def test_skeleton_alone_is_unverified_for_every_check_not_failed():
    results = run_exam(skeleton_files())
    assert len(results) == 9
    assert {result.status for result in results.values()} == {Status.ERROR}
    assert {result.error_code for result in results.values()} == {'USER_MODEL_MISSING'}


@pytest.mark.skipif(not RUNTIME, reason='fastapi·httpx·sqlalchemy·argon2-cffi가 있어야 실행 시험을 돈다')
def test_skeleton_with_a_minimal_signup_passes_every_final_check():
    results = run_exam(with_minimal_signup())
    assert {key: result.status for key, result in results.items()} == {key: Status.PASS for key in results}
    assert len(results) == 9


@pytest.mark.skipif(not RUNTIME, reason='fastapi·httpx·sqlalchemy·argon2-cffi가 있어야 실행 시험을 돈다')
def test_skeleton_health_test_runs_in_the_dev_unit_runner(tmp_path):
    # dev 개발자 시험 범위(Snapshot 안의 tests/test_*.py)와 같은 방식: 읽기 전용 Snapshot에서 dev unittest 실행기로 돌린다.
    snapshot = tmp_path / 'snapshot'
    for name, text in skeleton_files().items():
        (snapshot / name).parent.mkdir(parents=True, exist_ok=True)
        (snapshot / name).write_text(text, encoding='utf-8')
    _set_read_only(snapshot, True)
    try:
        completed = subprocess.run([sys.executable, '-I', '-c', _RUN, str(RUNNER), str(snapshot / 'tests'), str(snapshot),
                                    'test_*.py'], capture_output=True, text=True, timeout=120, cwd=tmp_path)
    finally:
        _set_read_only(snapshot, False)
    report = json.loads(completed.stdout)['report']
    assert (report['total'], report['passed'], report['failed']) == (1, 1, 0), report
