"""보안 판정기(security/)의 assess()·evaluate()를 시험하는 파일이다. (D1)

signup_examples.py의 정상·결함 예제를 판정기에 넣어서 "답을 아는 코드"를 맞게 판정하는지 확인한다.
또한 팀 시스템(dev)이 실제로 쓰는 요청·응답 타입(SecurityAssessmentRequest, SecurityAssessment)으로 시험해서
시스템의 규칙(허용된 증거 주소만 인용, 근거 없는 PASS 금지 등)과 어긋나지 않는지도 확인한다.
"""
import json
from pathlib import Path
from types import MappingProxyType  # 읽기 전용 사전(바꿀 수 없는 사전)
from urllib.parse import quote
from uuid import UUID, uuid4

import pytest

from agents.llm.content import sanitize_content
from agents.platform.security_verifier import SecurityAssessment, SecurityAssessmentRequest
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario

from evaluation.security import assess, evaluate
from tests.evaluation.signup_examples import DEFECTS, GOOD, GOOD_EXPECTED, SAFE_VARIANTS, expected, variant

ROOT = Path(__file__).resolve().parents[2]  # 저장소 맨 위 폴더(이 파일에서 두 단계 위)
# 판정 기준 파일(JSON)을 읽어 둔다. 시험 대부분이 이 기준으로 판정한다.
CRITERIA = json.loads((ROOT / 'src/evaluation/security/scn001_security_criteria.json').read_text(encoding='utf-8'))
CONTRACT = get_scenario(SCN_001_ID).planner_contract()  # 팀 시스템에 등록된 회원가입 시나리오의 요구사항·정책
SOURCE_ID = uuid4()  # 시험용 가짜 소스 산출물 번호
KEYS = {item['key']: UUID(item['requirementId']) for item in CONTRACT['requirements']}  # {"REQ-005": UUID, ...}
SECURITY_REQUIREMENTS = (KEYS['REQ-003'], KEYS['REQ-005'], KEYS['REQ-006'])  # 보안 판정기가 맡는 요구사항 세 개


def make_request(files, *, criteria=CRITERIA, findings=(), requirement_ids=SECURITY_REQUIREMENTS):
    # 팀 시스템(dev Host)이 만드는 것과 똑같은 모양의 판정 요청(SecurityAssessmentRequest)을 직접 만든다.
    # files = {파일 이름: 소스 글자}. 소스는 바이트(bytes)로 바꾸고, 증거로 인용할 수 있는 주소는 파일마다 하나씩 만든다.
    sources = {path: text.encode('utf-8') for path, text in files.items()}
    refs = tuple(f'artifact://{SOURCE_ID}/source.tar#path=' + quote(path, safe='/') for path in sorted(sources))
    scans = [{'reportRef': f'artifact://{uuid4()}/security-report.json',
              'report': {'format': 'bandit-v1', 'findings': [dict(item) for _id, item in findings]}}]
    payload = {'criteria': criteria, 'scenarioContract': CONTRACT, 'modelProposal': {},
               'scans': scans, 'executionManifest': {}}
    return SecurityAssessmentRequest(
        request_id=uuid4(), request_sha256='a' * 64, approval_sha256='b' * 64,
        run_id=uuid4(), workspace_id=uuid4(), workflow_step_id=uuid4(), source_artifact_id=SOURCE_ID,
        snapshot_sha256='c' * 64, requirement_ids=requirement_ids,
        finding_ids=tuple(finding_id for finding_id, _item in findings),
        permitted_evidence_refs=refs, _payload_json=json.dumps(payload),
        _source_files=MappingProxyType(sources))


def outcomes(files, **kwargs):
    # 소스를 판정기에 넣고 "{REQ-005: 'PASS', ...}" 모양의 결과와 원래 응답을 돌려준다. (시험에서 자주 쓰는 도우미)
    result = assess(make_request(files, **kwargs))
    assert isinstance(result, SecurityAssessment)  # None(오류)이 아니라 정상 응답이어야 한다.
    names = {value: key for key, value in KEYS.items()}  # UUID → "REQ-005" 같은 표시 이름으로 되돌리는 표
    return {names[requirement_id]: value for requirement_id, value in result.requirement_outcomes}, result


def test_criteria_passes_dev_secret_check():
    # 판정 기준 파일은 시스템이 "비밀번호 같은 이름이 들어 있는지" 검사한다.
    # 그래서 기준 JSON의 항목 이름에 password 계열 이름을 쓰면 승인이 거부된다. 쓰지 않았는지 확인한다.
    sanitize_content({'criteria': CRITERIA}, reject_secrets=True)


def test_good_example_passes_all_security_requirements():
    """정상 예제는 세 요구사항 모두 PASS이고, 증거 주소와 관찰 기록이 함께 나온다."""
    result, assessment = outcomes(GOOD)
    assert result == GOOD_EXPECTED
    assert assessment.evidence_refs and json.loads(assessment.evidence_json)['requirements']


@pytest.mark.parametrize('name', [item[0] for item in DEFECTS])  # 결함 예제 하나하나에 대해 같은 시험을 반복한다.
def test_each_defect_is_caught_by_its_check(name):
    """결함을 넣은 코드는 그 결함을 맡은 요구사항에서 기대한 판정(FAIL 또는 UNVERIFIED)이 나와야 한다."""
    result, _assessment = outcomes(variant(name))
    assert result == expected(name)


@pytest.mark.parametrize('name', [item[0] for item in SAFE_VARIANTS])
def test_safe_variants_are_not_false_positives(name):
    # 정상 코드를 다른 방식으로 쓴 것을 FAIL로 오해(오탐)하면 불필요한 수정 작업이 생긴다. 오탐이 없는지 확인한다.
    result, _assessment = outcomes(variant(name))
    assert result == expected(name)


def test_response_model_filters_object_response():
    # 비밀번호 칸이 있는 모델 객체를 반환해도, response_model(응답 모양 지정)이 걸러 주면 노출이 아니다.
    files = dict(GOOD, **{'routers.py': GOOD['routers.py'].replace(
        '@router.post("/api/signup", status_code=201)', '@router.post("/api/signup", status_code=201, response_model=SignupResult)').replace(
        '{"success": True, "message": "회원가입에 성공했습니다."}', 'user').replace(
        'from schemas import SignupRequest', 'from schemas import SignupRequest, SignupResult'),
        'schemas.py': GOOD['schemas.py'] + '\n\nclass SignupResult(BaseModel):\n    id: int\n    email: str\n'})
    assert outcomes(files)[0]['REQ-006'] == 'PASS'


@pytest.mark.parametrize('text,expected_hit', [
    ('password', True), ('user_password', True), ('passwordHash', True), ('hashed_password', True),
    ('pwd', True), ('passed', False), ('bypass', False), ('compass', False),
    ('MIN_PASSWORD_LENGTH', False), ('PASSWORD_ERROR_MESSAGE', False), ('password_policy', False),
])
def test_sensitive_name_is_matched_by_word(text, expected_hit):
    # 이름을 "단어 단위"로 판정하는지 확인한다. 앞 칸은 비밀번호로 봐야 하는 이름(True),
    # 뒷칸은 passed(통과했다)·bypass(우회)·길이/문구/정책 같은 설명 이름이라 비밀번호로 보면 안 되는 이름(False)이다.
    from evaluation.security.static_analysis import _sensitive
    assert _sensitive(CRITERIA).search(text) is expected_hit


def test_exact_rule_fails_library_defaults_but_at_least_passes():
    # 라이브러리 기본 설정(PasswordHasher())은 정책 숫자보다 큰 값이다.
    # "이상(AT_LEAST)" 규칙이면 통과(PASS), "정확히 같아야(EXACT)" 규칙이면 FAIL이어야 한다. 규칙을 기준 파일 한 줄로 바꿀 수 있다.
    files = dict(GOOD, **{'security.py': GOOD['security.py'].replace(
        'PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)', 'PasswordHasher()')})
    assert outcomes(files)[0]['REQ-005'] == 'PASS'
    exact = json.loads(json.dumps(CRITERIA))  # 기준을 복사한 뒤 규칙만 EXACT로 바꾼다.
    exact['hashPolicy']['parameterRule'] = 'EXACT'
    assert outcomes(files, criteria=exact)[0]['REQ-005'] == 'FAIL'


def test_syntax_error_never_becomes_pass():
    # 문법이 틀려서 읽지 못한 파일이 있으면 "문제 없음(PASS)"이라고 단정할 수 없다. 모두 UNVERIFIED여야 한다.
    files = dict(GOOD, **{'broken.py': 'def broken(:\n'})
    result, _assessment = outcomes(files)
    assert set(result.values()) == {'UNVERIFIED'}


def test_missing_evidence_refs_downgrade_to_unverified():
    # 시스템이 허용한 증거 주소가 하나도 없으면, 판정을 확정할 수 없으므로 UNVERIFIED로 낮춘다. (PASS/FAIL에는 증거가 필수)
    request = make_request(GOOD)
    object.__setattr__(request, 'permitted_evidence_refs', ())  # 바꿀 수 없는 객체의 값을 시험용으로 강제로 비운다.
    data = evaluate(request)
    assert {value for _key, value in data['requirement_outcomes']} == {'UNVERIFIED'}
    assert data['evidence_refs'] == ()


def test_criteria_for_other_scenario_is_rejected():
    # 다른 시나리오(SCN-999)용 기준이 들어오면 아무 요구사항도 판정하지 않는다.
    other = dict(CRITERIA, scenarioKey='SCN-999')
    result, assessment = outcomes(GOOD, criteria=other)
    assert result == {} and assessment.evidence_refs == ()


def test_only_requested_requirements_are_judged():
    # 요청에 포함된 요구사항만 판정한다. REQ-005만 요청하면 결과에도 REQ-005만 나온다.
    result, _assessment = outcomes(GOOD, requirement_ids=(KEYS['REQ-005'],))
    assert result == {'REQ-005': 'PASS'}


def test_tests_folder_is_not_analyzed():
    # tests 폴더의 파일은 제품 코드가 아니므로 분석하지 않는다. (그 안에 비밀번호를 찍는 코드가 있어도 판정에 영향이 없어야 한다.)
    files = dict(GOOD, **{'tests/test_leak.py': 'import logging\nlogging.info("%s", payload.password)\n'})
    assert outcomes(files)[0] == GOOD_EXPECTED


def test_bandit_findings_are_disposed_only_for_supported_rules():
    # Bandit 경고 중 판정기가 아는 규칙(약한 해시 B324, 코드 속 비밀번호 B105)만 판정하고, 모르는 규칙(B101)은 판정하지 않는다.
    files = variant('md5')
    md5_line = files['security.py'].splitlines().index('    return hashlib.md5(password.encode()).hexdigest()') + 1
    message_line = files['schemas.py'].splitlines().index('            raise ValueError("비밀번호는 최소 8자 이상이어야 합니다.")') + 1
    findings = (
        ('f-md5', {'ruleId': 'B324', 'path': 'security.py', 'line': md5_line, 'severity': 'HIGH'}),
        ('f-msg', {'ruleId': 'B105', 'path': 'schemas.py', 'line': message_line, 'severity': 'LOW'}),
        ('f-other', {'ruleId': 'B101', 'path': 'routers.py', 'line': 1, 'severity': 'LOW'}),
    )
    _result, assessment = outcomes(files, findings=findings)
    assert dict(assessment.finding_dispositions) == {'f-md5': 'CONFIRMED', 'f-msg': 'FALSE_POSITIVE'}


def test_bandit_literal_judged_by_code_evidence():
    # 경고만으로 CONFIRMED(확정)하지 않는다(개발정의서 10절).
    #   PASSWORD_FIELD = "password"     → 필드 이름 글자일 뿐이라 오탐(FALSE_POSITIVE)
    #   DB_PASSWORD = "Sup3rS3cr3t"     → 비밀번호 이름에 진짜 같은 값을 넣었으므로 확정(CONFIRMED)
    #   SESSION_LABEL = "Sup3rS3cr3t"   → 이름이 비밀번호가 아니라 근거가 부족하므로 판정하지 않고 SUSPECTED로 남긴다.
    files = dict(GOOD, **{'consts.py': 'PASSWORD_FIELD = "password"\nDB_PASSWORD = "Sup3rS3cr3t"\nSESSION_LABEL = "Sup3rS3cr3t"\n'})
    findings = tuple((f'f{n}', {'ruleId': 'B105', 'path': 'consts.py', 'line': n, 'severity': 'LOW'}) for n in (1, 2, 3))
    _result, assessment = outcomes(files, findings=findings)
    assert dict(assessment.finding_dispositions) == {'f1': 'FALSE_POSITIVE', 'f2': 'CONFIRMED'}


def test_reference_backend_is_judged_against_registry_policy():
    # 세화님이 만든 플랫폼 backend(사람이 만든 회원가입 코드)를 "진짜 코드 표본"으로 판정해 본다.
    # 팀 코드가 바뀌어도 이 시험이 갑자기 깨지지 않도록, 그때의 파일을 tests/evaluation/fixtures/reference_backend에 복사해 두었다.
    # (표본일 뿐이며 생성물의 정답이나 규격이 아니다.)
    # 기대: 해시는 라이브러리 기본값이 "이상" 규칙을 만족해 PASS, 이메일 앞뒤 공백 제거가 없어서 REQ-003은 FAIL.
    backend = Path(__file__).parent / 'fixtures' / 'reference_backend'
    files = {str(path.relative_to(backend)): path.read_text(encoding='utf-8') for path in backend.rglob('*.py')}
    result, _assessment = outcomes(files)
    assert result == {'REQ-003': 'FAIL', 'REQ-005': 'PASS', 'REQ-006': 'PASS'}


def test_dev_approval_loader_accepts_assess(tmp_path):
    # 팀 시스템(dev 42번)의 "승인 절차"를 실제로 통과하는지 확인한다.
    # 절차: 구현 파일의 해시를 계산 → 승인 JSON 파일을 만듦 → load_approved_assessor가 읽어서 받아들임.
    from hashlib import sha256
    from agents.platform.security_approval import calculate_implementation_sha256, load_approved_assessor
    import importlib
    assess_module = importlib.import_module('evaluation.security.assess')
    package = ROOT / 'src/evaluation/security'
    # 승인 파일의 "논리 이름"은 영문자로 시작해야 한다. __init__.py는 밑줄로 시작하므로 security_init.py라는 이름으로 등록한다.
    names = {'assess.py': 'assess.py', 'static_analysis.py': 'static_analysis.py', 'security_init.py': '__init__.py'}
    files = tuple((name, (package / file).resolve(), sha256((package / file).read_bytes()).hexdigest())
                  for name, file in names.items())
    contract_json = json.dumps(CONTRACT, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    approval = {
        'schemaVersion': 1, 'verifierId': 'scn001-security-static', 'verifierVersion': '0.1.0',
        'criteriaRef': CRITERIA['criteriaId'],
        'scenarioContractSha256': sha256(contract_json.encode('utf-8')).hexdigest(),
        'criteria': CRITERIA,
        'implementationSha256': calculate_implementation_sha256(assess_module.assess, files),
        'implementationFiles': [{'name': name, 'path': str(path), 'sha256': digest} for name, path, digest in files],
    }
    approval_file = tmp_path / 'owned-security-approval.local.json'
    approval_file.write_text(json.dumps(approval), encoding='utf-8')
    approved = load_approved_assessor(approval_file, assess_module.assess)
    assert approved is not None


# 아래는 "코드를 이렇게 쓰면 이렇게 판정한다"는 추가 사례 모음이다. 하나씩 직접 돌려 확인한 경로를 회귀 시험으로 고정해 둔 것이다.
# HANDLER = 정상 예제(GOOD)의 입력 검증 오류 처리기 코드. UNSAFE_HANDLER_HEAD = 그 처리기의 함수 선언 부분(위험한 변형을 만들 때 재사용).
HANDLER = ('@app.exception_handler(RequestValidationError)\nasync def validation_error_handler(request: Request, exc: RequestValidationError):\n'
           '    errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()]\n'
           '    return JSONResponse(status_code=422, content={"detail": errors})\n')
UNSAFE_HANDLER_HEAD = ('@app.exception_handler(RequestValidationError)\nasync def validation_error_handler(request: Request, exc: RequestValidationError):\n')


def _replace(path, old, new):
    # "소스 묶음을 받아서, path 파일의 old를 new로 바꾼 새 묶음을 돌려주는 함수"를 만든다.
    return lambda files: dict(files, **{path: files[path].replace(old, new, 1)})


def _append(path, text):
    # "path 파일의 맨 뒤에 text를 덧붙인 새 묶음을 돌려주는 함수"를 만든다.
    return lambda files: dict(files, **{path: files[path] + text})


# 각 사례: (이름, 소스를 바꾸는 함수, GOOD과 달라지는 기대 판정). 기대 판정이 {}이면 "GOOD과 같다(모두 PASS)"는 뜻이다.
EXTRA_CASES = [
    ('handler returns exc.errors() directly', _replace('main.py', HANDLER, UNSAFE_HANDLER_HEAD +
        '    return JSONResponse(status_code=422, content={"detail": exc.errors()})\n'), {'REQ-006': 'FAIL'}),
    ('handler wraps exc.errors() in jsonable_encoder', _replace('main.py', HANDLER, UNSAFE_HANDLER_HEAD +
        '    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})\n'), {'REQ-006': 'FAIL'}),
    ('handler returns only a constant', _replace('main.py', HANDLER, UNSAFE_HANDLER_HEAD +
        '    return JSONResponse(status_code=422, content={"detail": "invalid request"})\n'), {}),
    ('safe handler via add_exception_handler', _replace('main.py', HANDLER,
        'async def vh(request: Request, exc: RequestValidationError):\n    return JSONResponse(status_code=422, content={"detail": "invalid"})\n\n'
        'app.add_exception_handler(RequestValidationError, vh)\n'), {}),
    ('unsafe handler via add_exception_handler', _replace('main.py', HANDLER,
        'async def vh(request: Request, exc: RequestValidationError):\n    return JSONResponse(status_code=422, content={"detail": exc.errors()})\n\n'
        'app.add_exception_handler(RequestValidationError, vh)\n'), {'REQ-006': 'FAIL'}),
    ('UniqueConstraint on email', lambda f: _append('models.py', '    __table_args__ = (UniqueConstraint("email"),)\n')(
        _replace('models.py', 'unique=True, index=True', 'index=True')(f)), {}),
    ('unique Index on email', lambda f: _append('models.py', '    __table_args__ = (Index("ix_u", "email", unique=True),)\n')(
        _replace('models.py', 'unique=True, index=True', 'index=True')(f)), {}),
    ('sensitive response_model', _replace('routers.py', '@router.post("/api/signup", status_code=201)',
        '@router.post("/api/signup", status_code=201, response_model=SignupRequest)'), {'REQ-006': 'FAIL'}),
    ('pwdlib recommended() defaults', lambda f: dict(f, **{'security.py':
        'from pwdlib import PasswordHash\nph = PasswordHash.recommended()\ndef hash_password(password):\n    return ph.hash(password)\n'}), {}),
    ('aliased PasswordHasher with positional params', lambda f: dict(f, **{'security.py':
        'from argon2 import PasswordHasher as PH\nHASHER = PH(2, 19456, 1)\ndef hash_password(password):\n    return HASHER.hash(password)\n'}), {}),
    ('PasswordHasher.from_parameters profile', lambda f: dict(f, **{'security.py':
        'import argon2\nHASHER = argon2.PasswordHasher.from_parameters(argon2.profiles.RFC_9106_LOW_MEMORY)\n'
        'def hash_password(password):\n    return HASHER.hash(password)\n'}), {}),
    ('bcrypt through passlib', lambda f: dict(f, **{'security.py':
        'from passlib.hash import bcrypt\ndef hash_password(password):\n    return bcrypt.hash(password)\n'}), {'REQ-005': 'FAIL'}),
    ('Argon2i instead of Argon2id', lambda f: dict(f, **{'security.py':
        'from argon2 import PasswordHasher, Type\nHASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, type=Type.I)\n'
        'def hash_password(password):\n    return HASHER.hash(password)\n'}), {'REQ-005': 'FAIL'}),
    ('awaited async hash helper is still a hash path', lambda f: _append('security.py',
        '\nasync def hash_async(password):\n    return HASHER.hash(password)\n')(
        _replace('routers.py', 'password_hash=hash_password(payload.password)', 'password_hash=await hash_async(payload.password)')(f)), {}),
    ('hash helper that returns the original password', _replace('security.py', 'return HASHER.hash(password)',
        'HASHER.hash("dummy")\n    return password'), {'REQ-005': 'UNVERIFIED'}),
]


@pytest.mark.parametrize('label,change,changed', EXTRA_CASES, ids=[item[0] for item in EXTRA_CASES])
def test_extra_code_shapes(label, change, changed):
    # 위 추가 사례를 하나씩 판정해서 기대한 결과와 같은지 확인한다.
    result, _assessment = outcomes(change(dict(GOOD)))
    assert result == {**GOOD_EXPECTED, **changed}


def test_unparseable_deep_file_marks_only_that_file_not_the_whole_assessment():
    # 너무 길어서 파이썬이 읽다가 재귀 한도를 넘는 파일은, 문법 오류 파일처럼 "읽지 못한 파일"로 기록하고 평가는 계속한다.
    # 단, 읽지 못한 파일이 있으므로 PASS는 만들지 않는다.
    files = dict(GOOD, **{'deep.py': 'x = ' + ' + '.join(['1'] * 20000) + '\n'})
    result, assessment = outcomes(files)
    assert set(result.values()) == {'UNVERIFIED'}
    assert json.loads(assessment.evidence_json)['parseErrors'] == ['deep.py']
