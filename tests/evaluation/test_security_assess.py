"""보안 판정기(security/)의 assess()·evaluate()를 시험하는 파일이다. (D1)

signup_examples.py의 정상·결함 예제를 판정기에 넣어서 "답을 아는 코드"를 맞게 판정하는지 확인한다.
또한 팀 시스템(dev)이 실제로 쓰는 요청·응답 타입(SecurityAssessmentRequest, SecurityAssessment)으로 시험해서
시스템의 규칙(허용된 증거 주소만 인용, 근거 없는 PASS 금지 등)과 어긋나지 않는지도 확인한다.
"""
import json
import re
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


def _run_sql(text):
    # SQL 글자를 담은 이름(SCHEMA = "CREATE TABLE ...")을 실제로 실행하는 줄을 text 뒤에 덧붙여 돌려준다.
    # 분석기는 "실제로 실행하는 SQL"만 근거로 인정하므로(어디에도 쓰이지 않는 글자는 증거가 아니다), SQL 시험 입력에는 실행 줄이 필요하다.
    names = re.findall(r'^([A-Z_]+) = ', text, flags=re.MULTILINE)
    return text + '\nimport sqlite3\n_db = sqlite3.connect(":memory:")\n' + ''.join(f'_db.executescript({name})\n' for name in names)


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


# ---------------------------------------------------------------------------------------------
# 외부 점검(2026-10-14)에서 찾은 "결함인데 PASS"가 나오던 사례들이다. 다시는 놓치지 않도록 정식 시험으로 고정한다.
# 기대 판정에서 UNVERIFIED는 "분석 범위 밖이라 확신할 수 없으니 PASS를 내지 않는다"는 뜻이다. (FAIL이 아니어도 PASS만 아니면 안전하다.)
# ---------------------------------------------------------------------------------------------
ERROR_ITEM = '{"loc": error["loc"], "msg": error["msg"], "type": error["type"]}'  # GOOD의 안전한 오류 응답 항목
HASH_FUNCTION = 'def hash_password(password: str) -> str:\n    return HASHER.hash(password)\n'

REVIEW_CASES = [
    # --- REQ-005: 해시 저장 ---
    ('hash variable overwritten with the plain password', _replace('security.py', 'return HASHER.hash(password)',
        'result = HASHER.hash(password)\n    result = password\n    return result'), {'REQ-005': 'UNVERIFIED'}),
    ('hash() method that just returns its input is not an approved hasher', lambda f: dict(f, **{'security.py':
        f['security.py'].replace('def hash_password',
            'class Identity:\n    def hash(self, value):\n        return value\n\nIDENTITY = Identity()\n\ndef hash_password'
        ).replace('return HASHER.hash(password)', 'return IDENTITY.hash(password)')}), {'REQ-005': 'UNVERIFIED'}),
    ('hashing only when a flag is set can return the unhashed parameter', _replace('security.py', HASH_FUNCTION,
        'def hash_password(password, flag=True):\n    if flag:\n        password = HASHER.hash(password)\n    return password\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('variable that only ever holds a hash result is fine', _replace('security.py', 'return HASHER.hash(password)',
        'result = HASHER.hash(password)\n    return result'), {}),
    # --- REQ-003: 이메일 중복 금지 ---
    ('composite UniqueConstraint does not protect email alone', lambda f: _append('models.py',
        '    tenant_id = Column(Integer)\n    __table_args__ = (UniqueConstraint("email", "tenant_id"),)\n')(
        _replace('models.py', 'unique=True, index=True', 'index=True')(f)), {'REQ-003': 'FAIL'}),
    ('composite unique Index does not protect email alone', lambda f: _append('models.py',
        '    __table_args__ = (Index("ix_u", "email", "tenant_id", unique=True),)\n')(
        _replace('models.py', 'unique=True, index=True', 'index=True')(f)), {'REQ-003': 'FAIL'}),
    ('SQL composite UNIQUE does not protect email alone', lambda f: dict(f, **{'models.py': _run_sql(
        'SCHEMA = """CREATE TABLE users (\n id INTEGER,\n email TEXT,\n password_hash TEXT,\n tenant TEXT,\n UNIQUE (email, tenant)\n)"""\n')}),
        {'REQ-003': 'FAIL', 'REQ-005': 'UNVERIFIED'}),
    ('SQL UNIQUE on the email column alone is enough', lambda f: dict(f, **{'models.py': _run_sql(
        'SCHEMA = """CREATE TABLE users (\n id INTEGER,\n email TEXT,\n password_hash TEXT,\n UNIQUE (email)\n)"""\n')}),
        {'REQ-005': 'UNVERIFIED'}),
    # --- REQ-006: 검증 오류 응답에 입력값이 실리는 경우 ---
    ('input copied under another key', _replace('main.py', ERROR_ITEM, '{"detail": error["input"]}'), {'REQ-006': 'FAIL'}),
    ('whole error item turned into text', _replace('main.py', ERROR_ITEM, '{"detail": str(error)}'), {'REQ-006': 'FAIL'}),
    ('error ctx is not known to be free of the input', _replace('main.py', ERROR_ITEM,
        '{"loc": error["loc"], "ctx": error["ctx"]}'), {'REQ-006': 'UNVERIFIED'}),
    ('allowed keys read with get() are fine', _replace('main.py', ERROR_ITEM,
        '{"loc": error.get("loc"), "msg": error.get("msg")}'), {}),
]


@pytest.mark.parametrize('label,change,changed', REVIEW_CASES, ids=[item[0] for item in REVIEW_CASES])
def test_review_cases(label, change, changed):
    # 위 사례를 하나씩 판정해서 기대한 결과(GOOD의 판정에서 바뀌는 부분만 적음)와 같은지 확인한다.
    result, _assessment = outcomes(change(dict(GOOD)))
    assert result == {**GOOD_EXPECTED, **changed}


@pytest.mark.parametrize('source,expected_value', [
    ('password = "password"', 'CONFIRMED'),                       # 변수 이름이 비밀번호이면, 값이 "password"여도 하드코딩이다
    ('password = "correct horse battery staple"', 'CONFIRMED'),   # 공백이 있는 암호문도 하드코딩이다(문장처럼 보여도 근거가 아니다)
    ('password = "한글암호문자열"', 'CONFIRMED'),                  # 한글 암호문도 마찬가지
    ('DB_PASSWORD = "x7Kp9"', 'CONFIRMED'),
    ('PASSWORD_FIELD = "password"', 'FALSE_POSITIVE'),            # 이름에 FIELD가 있어 "필드 이름을 적은 글자"이다
    ('password = ""', 'FALSE_POSITIVE'),                          # 빈 값은 자격증명이 아니다
    ('label = "correct horse battery staple"', 'FALSE_POSITIVE'),  # 비밀번호 이름이 아니고 안내 문구처럼 생김
    ('session_label = "x7Kp9"', None),                            # 이름도 글자도 근거가 부족 → 판정하지 않는다(SUSPECTED 유지)
])
def test_bandit_literal_decision_follows_the_variable_name_first(source, expected_value):
    # "코드에 비밀번호가 적혀 있다"는 Bandit 경고(B105)의 판정 순서를 확인한다.
    # 변수 이름이 비밀번호이면 글자 모양과 상관없이 먼저 확정하고, 그 다음에 안내 문구·필드 이름 글자를 오탐으로 거른다.
    files = dict(GOOD, **{'consts.py': source + '\n'})
    findings = (('f1', {'ruleId': 'B105', 'path': 'consts.py', 'line': 1, 'severity': 'LOW'}),)
    _result, assessment = outcomes(files, findings=findings)
    decided = dict(assessment.finding_dispositions).get('f1')
    assert decided == expected_value


# ---------------------------------------------------------------------------------------------
# 외부 점검(2026-10-15)에서 찾은 "결함인데 PASS"가 나오던 사례들이다. 다시는 놓치지 않도록 정식 시험으로 고정한다.
# 같은 종류의 우회(이름이 다른 것을 가리키게 만들기, 인자를 풀어서 숨기기, 일부 항목만 처리하기)를 함께 모았다.
# 기대 판정에서 UNVERIFIED는 "분석 범위 밖이라 확신할 수 없으니 PASS를 내지 않는다"는 뜻이다.
# ---------------------------------------------------------------------------------------------
ERROR_LINE = ('errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} '
              'for error in exc.errors()]')  # GOOD의 검증 오류 처리기에서 오류 목록을 만드는 줄
IDENTITY_CLASS = 'class Identity:\n    def hash(self, value):\n        return value\n\n'  # hash()가 입력을 그대로 돌려주는 가짜 해시기
WEAK_HASHER = 'PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)'  # GOOD이 쓰는 승인된 설정
MODEL_ARGS_OLD = 'unique=True, index=True'  # GOOD 모델의 이메일 칸 설정(이것을 지우면 UNIQUE가 없어진다)


def _handler(body):
    # "검증 오류 처리기의 오류 목록 만드는 줄"을 body로 바꾼 새 소스 묶음을 만드는 함수를 돌려준다.
    return _replace('main.py', ERROR_LINE, body)


def _email_constraint(extra_text):
    # 이메일 칸의 unique=True를 없애고, 클래스 맨 뒤에 extra_text(중복 금지 제약 선언)를 덧붙인 소스 묶음을 만든다.
    return lambda files: _append('models.py', extra_text)(_replace('models.py', MODEL_ARGS_OLD, 'index=True')(files))


PREPUSH_CASES = [
    # --- REQ-005: 해시 저장 (진짜 해시기를 쓴다는 근거가 흔들리는 경우) ---
    ('parameter default is a fake hasher', lambda f: dict(f, **{'security.py': f['security.py'].replace(
        'def hash_password', IDENTITY_CLASS + 'def hash_password').replace(
        'def hash_password(password: str)', 'def hash_password(password: str, HASHER=Identity())')}),
        {'REQ-005': 'UNVERIFIED'}),
    ('hasher received as a parameter is not trusted', _replace('security.py', HASH_FUNCTION,
        'def hash_password(password: str, hasher=HASHER) -> str:\n    return hasher.hash(password)\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('weak parameters hidden in a ** dict', _replace('security.py', WEAK_HASHER,
        'PasswordHasher(**{"time_cost": 1, "memory_cost": 8, "parallelism": 1})'), {'REQ-005': 'UNVERIFIED'}),
    ('weak parameters hidden in a * list', _replace('security.py', WEAK_HASHER, 'PasswordHasher(*[1, 8, 1])'),
        {'REQ-005': 'UNVERIFIED'}),
    ('memory constant assigned twice', lambda f: dict(f, **{'security.py':
        'MEM = 8\n' + f['security.py'].replace('memory_cost=19456', 'memory_cost=MEM') + 'MEM = 19456\n'}),
        {'REQ-005': 'UNVERIFIED'}),
    ('library hash method replaced at runtime', _append('security.py',
        '\nPasswordHasher.hash = lambda self, value: value\n'), {'REQ-005': 'UNVERIFIED'}),
    ('imported name redefined by a class of the same name', _replace('security.py', 'HASHER = PasswordHasher(',
        'class PasswordHasher:\n    def __init__(self, **options): pass\n    def hash(self, value): return value\n\n'
        'HASHER = PasswordHasher('), {'REQ-005': 'UNVERIFIED'}),
    ('hashing a constant instead of the password', _replace('security.py', 'return HASHER.hash(password)',
        'return HASHER.hash("fixed")'), {'REQ-005': 'UNVERIFIED'}),
    ('hashing only when a flag is on', _replace('security.py', HASH_FUNCTION,
        'def hash_password(password: str, enabled=True) -> str:\n    if enabled:\n        return HASHER.hash(password)\n'
        '    return password\n'), {'REQ-005': 'UNVERIFIED'}),
    # --- REQ-003: 이메일 중복 금지 (칸 인자의 종류가 섞여도 복합 제약은 복합이다) ---
    ('constraint mixing a string and a column object is composite', _email_constraint(
        '    tenant_id = Column(Integer)\n    __table_args__ = (UniqueConstraint("email", tenant_id),)\n'),
        {'REQ-003': 'FAIL'}),
    ('constraint on one column object is enough', _email_constraint(
        '    __table_args__ = (UniqueConstraint(email),)\n'), {}),
    ('composite unique Index made of column objects', _email_constraint(
        '    tenant_id = Column(Integer)\n    __table_args__ = (Index("ix_u", email, tenant_id, unique=True),)\n'),
        {'REQ-003': 'FAIL'}),
    ('constraint with an unreadable column list is undetermined', _email_constraint(
        '    __table_args__ = (UniqueConstraint(*COLUMNS),)\n'), {'REQ-003': 'UNVERIFIED'}),
    ('unique Index on lower(email) is undetermined', _email_constraint(
        '    __table_args__ = (Index("ix_email_lower", func.lower(email), unique=True),)\n'), {'REQ-003': 'UNVERIFIED'}),
    # --- REQ-006: 검증 오류 응답 (input을 "모든 항목에서" 지우는 반복문만 안전하다) ---
    ('removing input from every item is safe', _handler(
        'errors = exc.errors()\n    for error in errors:\n        error.pop("input", None)'), {}),
    ('del statement is as good as pop', _handler(
        'errors = exc.errors()\n    for error in errors:\n        del error["input"]'), {}),
    ('removing several keys including input is safe', _handler(
        'errors = exc.errors()\n    for error in errors:\n        error.pop("input", None)\n        error.pop("ctx", None)'), {}),
    ('checking the length before cleaning does not leak', _handler(
        'errors = exc.errors()\n    count = len(errors)\n    for error in errors:\n        error.pop("input", None)'), {}),
    ('removal only for some error types', _handler(
        'errors = exc.errors()\n    for error in errors:\n        if error["type"] == "missing":\n'
        '            error.pop("input", None)'), {'REQ-006': 'UNVERIFIED'}),
    ('continue skips the removal for some items', _handler(
        'errors = exc.errors()\n    for error in errors:\n        if error["type"] == "x":\n            continue\n'
        '        error.pop("input", None)'), {'REQ-006': 'UNVERIFIED'}),
    ('cleaning loop inside an if block may not run', _handler(
        'errors = exc.errors()\n    if errors:\n        for error in errors:\n            error.pop("input", None)'),
        {'REQ-006': 'UNVERIFIED'}),
    ('for-else loop is not a plain cleaning loop', _handler(
        'errors = exc.errors()\n    for error in errors:\n        error.pop("input", None)\n    else:\n        pass'),
        {'REQ-006': 'UNVERIFIED'}),
    ('loop variable replaced before the removal', _handler(
        'errors = exc.errors()\n    for error in errors:\n        error = {}\n        error.pop("input", None)'),
        {'REQ-006': 'UNVERIFIED'}),
    ('error list reassigned to the raw errors after cleaning', _handler(
        'errors = exc.errors()\n    for error in errors:\n        error.pop("input", None)\n    errors = exc.errors()'),
        {'REQ-006': 'UNVERIFIED'}),
    ('input put back after the removal', _handler(
        'errors = exc.errors()\n    for error in errors:\n        error.pop("input", None)\n        error["input"] = exc.body'),
        {'REQ-006': 'FAIL'}),
    ('removing a different key does not help', _handler(
        'errors = exc.errors()\n    for error in errors:\n        error.pop("ctx", None)'), {'REQ-006': 'FAIL'}),
    ('raw list returned before the cleaning', _handler(
        'errors = exc.errors()\n    if len(errors) > 1:\n        return JSONResponse(status_code=422, content={"detail": errors})\n'
        '    for error in errors:\n        error.pop("input", None)'), {'REQ-006': 'FAIL'}),
    ('only the first item is cleaned', _handler(
        'errors = exc.errors()\n    for error in errors[:1]:\n        error.pop("input", None)'), {'REQ-006': 'FAIL'}),
]


# 같은 점검에서 이어서 확인한 "이름이 가리키는 대상을 믿어도 되는가"와 "비밀번호가 변수를 거쳐 새는가"의 사례들이다.
ROUTE_HEAD = 'def signup(payload: SignupRequest, db=Depends(get_db)):\n'  # GOOD의 가입 경로 함수 선언 줄
USER_LINE = 'user = User(email=payload.email, password_hash=hash_password(payload.password))'  # GOOD이 사용자를 만드는 줄
FACTORY_NOOP = 'def noop(function):\n    return lambda value: value\n\n'  # 어떤 함수든 "입력을 그대로 돌려주는 함수"로 바꿔 놓는 데코레이터
OVERRIDE_HANDLER = ('async def vh(request: Request, exc: RequestValidationError):\n'
                    '    return JSONResponse(status_code=422, content={"detail": "invalid"})\n\n')  # 안전한 처리기


def _route_logging(body):
    # 가입 경로 함수 맨 앞에 body(로깅 등)를 끼워 넣고, 파일 맨 위에 logging을 가져오는 소스 묶음을 만드는 함수를 돌려준다.
    return lambda files: dict(files, **{'routers.py': 'import logging\n' + files['routers.py'].replace(
        ROUTE_HEAD, ROUTE_HEAD + body, 1)})


def _extra_function(text):
    # routers.py 맨 뒤에 함수(또는 클래스)를 덧붙이고, 파일 맨 위에 logging을 가져오는 소스 묶음을 만드는 함수를 돌려준다.
    return lambda files: dict(files, **{'routers.py': 'import logging\n' + files['routers.py'] + '\n\n' + text})


PREPUSH_CASES += [
    # --- REQ-005: 해시 "함수"의 이름도 다른 것으로 바뀔 수 있다 ---
    ('hash helper name reassigned to a lambda', _append('security.py', '\nhash_password = lambda value: value\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('hash helper replaced by a decorator', lambda f: dict(f, **{'security.py': FACTORY_NOOP + f['security.py'].replace(
        'def hash_password', '@noop\ndef hash_password')}), {'REQ-005': 'UNVERIFIED'}),
    ('route parameter hides the hash helper', _replace('routers.py', ROUTE_HEAD,
        'def signup(payload: SignupRequest, db=Depends(get_db), hash_password=lambda value: value):\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('local variable hides the hash helper', _replace('routers.py', USER_LINE,
        'hash_password = lambda value: value\n    ' + USER_LINE), {'REQ-005': 'UNVERIFIED'}),
    ('hash helper imported twice', _replace('routers.py', 'from security import hash_password',
        'from security import hash_password\nfrom elsewhere import hash_password'), {'REQ-005': 'UNVERIFIED'}),
    ('imported hash helper redefined in the same file', _replace('routers.py', 'from security import hash_password\n',
        'from security import hash_password\n\ndef hash_password(value):\n    return value\n'), {'REQ-005': 'UNVERIFIED'}),
    ('with-as variable hides the hash helper', _replace('routers.py', USER_LINE,
        'with open("x") as hash_password:\n        pass\n    ' + USER_LINE), {'REQ-005': 'UNVERIFIED'}),
    ('staticmethod does not change a hash helper', _append('security.py',
        '\n\nclass Hasher:\n    @staticmethod\n    def hash_password(password: str) -> str:\n        return HASHER.hash(password)\n'), {}),
    # --- REQ-006: 검증 오류 처리기를 "등록하는 방법"과 "실제로 등록되는 함수" ---
    ('validation handler registered as a lambda', _replace('main.py', HANDLER,
        'app.add_exception_handler(RequestValidationError, lambda request, exc: JSONResponse(status_code=422, '
        'content={"detail": exc.errors()}))\n'), {'REQ-006': 'UNVERIFIED'}),
    ('validation handler replaced by a decorator', _replace('main.py', HANDLER,
        'def replace(function):\n    async def unsafe(request, exc):\n        return JSONResponse(status_code=422, '
        'content={"detail": exc.errors()})\n    return unsafe\n\n@app.exception_handler(RequestValidationError)\n@replace\n'
        'async def validation_error_handler(request: Request, exc: RequestValidationError):\n'
        '    return JSONResponse(status_code=422, content={"detail": "invalid"})\n'), {'REQ-006': 'UNVERIFIED'}),
    ('validation handler name reassigned before registration', _replace('main.py', HANDLER, OVERRIDE_HANDLER +
        'async def unsafe(request, exc):\n    return JSONResponse(status_code=422, content={"detail": exc.errors()})\n\n'
        'vh = unsafe\napp.add_exception_handler(RequestValidationError, vh)\n'), {'REQ-006': 'UNVERIFIED'}),
    ('safe handler registered through exception_handlers', _replace('main.py', HANDLER, OVERRIDE_HANDLER +
        'app.exception_handlers[RequestValidationError] = vh\n'), {}),
    ('unsafe handler registered through exception_handlers', _replace('main.py', HANDLER,
        'async def vh(request: Request, exc: RequestValidationError):\n    return JSONResponse(status_code=422, '
        'content={"detail": exc.errors()})\n\napp.exception_handlers[RequestValidationError] = vh\n'), {'REQ-006': 'FAIL'}),
    ('safe handler registered through the FastAPI constructor', lambda f: dict(f, **{'main.py': f['main.py'].replace(
        HANDLER, '').replace('app = FastAPI()', OVERRIDE_HANDLER + 'app = FastAPI(exception_handlers={RequestValidationError: vh})')}),
        {}),
    # --- REQ-006: 예외 객체(exc) 자체도 입력값을 품고 있다 ---
    ('str(exc) put into the response', _handler('detail = str(exc)'), {'REQ-006': 'FAIL'}),
    ('exc.args put into the response', _handler('detail = exc.args'), {'REQ-006': 'FAIL'}),
    ('exc written to the log', _handler('import logging\n    logging.error(exc)'), {'REQ-006': 'FAIL'}),
    ('exc encoded into the response', _handler('detail = jsonable_encoder(exc)'), {'REQ-006': 'FAIL'}),
    ('only the type of exc is used', _handler(
        'errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()]\n'
        '    kind = type(exc).__name__'), {}),
    # --- REQ-006: 비밀번호가 변수·하위 클래스·별칭을 거쳐 로그로 새는 경우 ---
    ('subclass of the request model is logged', _extra_function(
        'class SignupIn(SignupRequest):\n    pass\n\n\ndef audit(payload: SignupIn):\n    logging.info(payload)\n'),
        {'REQ-006': 'FAIL'}),
    ('alias of the request model is logged', _extra_function(
        'Req = SignupRequest\n\n\ndef audit(payload: Req):\n    logging.info(payload)\n'), {'REQ-006': 'FAIL'}),
    ('request object copied to another variable then logged', _route_logging(
        '    copy = payload\n    logging.info(copy)\n'), {'REQ-006': 'FAIL'}),
    ('password put into a dict then logged', _route_logging(
        '    data = {"p": payload.password}\n    logging.info(data)\n'), {'REQ-006': 'FAIL'}),
    ('password joined into a message then logged', _route_logging(
        '    message = "user " + payload.password\n    logging.info(message)\n'), {'REQ-006': 'FAIL'}),
    ('password copied under a harmless name then logged', _route_logging(
        '    value = payload.password\n    logging.info(value)\n'), {'REQ-006': 'FAIL'}),
    ('hash kept in a variable then logged', _route_logging(
        '    digest = hash_password(payload.password)\n    logging.info(digest)\n'), {'REQ-006': 'FAIL'}),
    ('whole request body logged', _route_logging('    data = request.get_json()\n    logging.info(data)\n'),
        {'REQ-006': 'FAIL'}),
    # --- 위 추적이 정상 코드를 FAIL로 만들지 않는지 (오탐 방지) ---
    ('hash kept in a variable and only the email logged', _route_logging(
        '    digest = hash_password(payload.password)\n    logging.info("created %s", payload.email)\n'), {}),
    ('comparison result is not the password', _route_logging(
        '    same = payload.password == payload.email\n    logging.info("same=%s", same)\n'), {}),
    ('result of an unknown function is not the password', _route_logging(
        '    strength = score(payload.password)\n    logging.info("strength=%s", strength)\n'), {}),
    ('email only converted then logged', _route_logging(
        '    email = payload.email.strip().lower()\n    logging.info("signup %s", email)\n'), {}),
    ('one safe field of the request body logged', _route_logging(
        '    data = request.get_json()\n    email = data["email"]\n    logging.info("signup %s", email)\n'), {}),
    ('key names of the request body logged', _route_logging(
        '    data = request.get_json()\n    logging.info("keys %s", list(data.keys()))\n'), {}),
    ('only the length of the password logged', _route_logging(
        '    size = len(payload.password)\n    logging.info("size %d", size)\n'), {}),
    ('upper-case constants like Status.PASS are not secrets', _route_logging(
        '    levels = {"ok": Status.PASS}\n    for name, level in levels.items():\n        logging.info(name)\n'), {}),
]


@pytest.mark.parametrize('label,change,changed', PREPUSH_CASES, ids=[item[0] for item in PREPUSH_CASES])
def test_prepush_review_cases(label, change, changed):
    # 위 사례를 하나씩 판정해서 기대한 결과(GOOD의 판정에서 바뀌는 부분만 적음)와 같은지 확인한다.
    result, _assessment = outcomes(change(dict(GOOD)))
    assert result == {**GOOD_EXPECTED, **changed}


# ---------------------------------------------------------------------------------------------
# 3차 외부 점검(2026-10-16)에서 찾은 "결함인데 PASS" 4건과 같은 종류의 변형들이다.
#   ① and·or가 비밀번호 값을 그대로 돌려주는데 True/False만 만든다고 본 문제
#   ② 입력 비밀번호가 아니라 상수(변수에 담은 상수 포함)를 해시해도 통과한 문제
#   ③ 고정 salt를 지정해도 통과한 문제
#   ④ 이메일이 아닌 다른 칸의 UNIQUE, 또는 다른 표의 이메일 UNIQUE를 근거로 인정한 문제
# SQL·Table 사례는 models.py를 통째로 바꾸므로 사용자 모델(User)이 사라져 REQ-005의 저장 경로가 없어진다(UNVERIFIED).
# ---------------------------------------------------------------------------------------------
def _hash_body(body):
    # security.py의 hash_password 함수 본문을 body로 바꾼 소스 묶음을 만드는 함수를 돌려준다.
    return _replace('security.py', HASH_FUNCTION, 'def hash_password(password: str) -> str:\n' + body)


def _models_file(text):
    # models.py를 text로 통째로 바꾼 소스 묶음을 만드는 함수를 돌려준다. (사용자 모델 대신 SQL·Table 선언을 시험할 때 쓴다.)
    return lambda files: dict(files, **{'models.py': _run_sql(text) if re.search(r'^[A-Z_]+ = ', text, re.MULTILINE) and 'sqlite3' not in text else text})


def _without_email_unique(change):
    # 이메일 칸의 unique=True를 없앤 뒤 change를 적용하는 소스 묶음 함수를 만든다.
    return lambda files: change(_replace('models.py', MODEL_ARGS_OLD, 'index=True')(files))


def _before_add(body, head='import logging\n'):
    # 가입 경로 함수에서 db.add(user) 바로 앞에 body를 끼워 넣고, 파일 맨 위에 head(import 문)를 붙인 소스 묶음을 만드는 함수를 돌려준다.
    return lambda files: dict(files, **{'routers.py': head + files['routers.py'].replace(
        '    db.add(user)\n', body + '    db.add(user)\n', 1)})


UNVERIFIED_STORAGE = {'REQ-005': 'UNVERIFIED'}  # 사용자 모델이 사라져서 저장 경로를 찾지 못하는 사례들의 REQ-005 판정

THIRD_REVIEW_CASES = [
    # --- ① and·or는 피연산자 값을 그대로 돌려준다 ---
    ('or keeps the password value', _route_logging('    value = payload.password or ""\n    logging.info(value)\n'),
        {'REQ-006': 'FAIL'}),
    ('and keeps the password value', _route_logging('    value = payload.email and payload.password\n    print(value)\n'),
        {'REQ-006': 'FAIL'}),
    ('function stored in a variable returns the password', _route_logging(
        '    getter = lambda: payload.password\n    logging.info(getter())\n'), {'REQ-006': 'FAIL'}),
    ('inner function returns the password', _route_logging(
        '    def reveal():\n        return payload.password\n    logging.info(reveal())\n'), {'REQ-006': 'FAIL'}),
    ('comparisons joined by and are only booleans', _route_logging(
        '    ok = len(payload.password) > 8 and payload.email is not None\n    logging.info("ok=%s", ok)\n'), {}),
    # --- ② 해시하는 값이 입력(매개변수)에서 나와야 한다 ---
    ('constant kept in a variable is hashed', _hash_body('    fixed = "constant-value"\n    return HASHER.hash(fixed)\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('parameter overwritten by a constant before hashing', _hash_body(
        '    password = "constant"\n    return HASHER.hash(password)\n'), {'REQ-005': 'UNVERIFIED'}),
    ('module constant is hashed', lambda f: _replace('security.py', HASH_FUNCTION,
        'FIXED = "constant"\n\ndef hash_password(password: str) -> str:\n    return HASHER.hash(FIXED)\n')(f),
        {'REQ-005': 'UNVERIFIED'}),
    ('constant fallback after or', _hash_body('    return HASHER.hash(password or "x")\n'), {'REQ-005': 'UNVERIFIED'}),
    ('hash helper called with a constant', _replace('routers.py', 'hash_password(payload.password)', 'hash_password("fixed")'),
        {'REQ-005': 'UNVERIFIED'}),
    ('stripping the password before hashing is fine', _hash_body(
        '    password = password.strip()\n    return HASHER.hash(password)\n'), {}),
    ('hashing the encoded password is fine', _hash_body(
        '    data = password.encode("utf-8")\n    return HASHER.hash(data)\n'), {}),
    ('parameter with another name is fine', _replace('security.py', HASH_FUNCTION,
        'def hash_password(raw: str) -> str:\n    return HASHER.hash(raw)\n'), {}),
    ('hashing directly in the route is fine', _replace('routers.py', 'password_hash=hash_password(payload.password)',
        'password_hash=HASHER.hash(payload.password)'), {}),
    ('only an unrelated parameter is hashed', lambda f: _replace('routers.py', 'hash_password(payload.password)',
        'hash_password(payload.password, "pep")')(_replace('security.py', HASH_FUNCTION,
        'def hash_password(password: str, pepper: str = "x") -> str:\n    return HASHER.hash(pepper)\n')(f)),
        {'REQ-005': 'UNVERIFIED'}),
    ('input hashed together with a constant parameter is fine', lambda f: _replace('routers.py', 'hash_password(payload.password)',
        'hash_password(payload.password, "pep")')(_replace('security.py', HASH_FUNCTION,
        'def hash_password(password: str, pepper: str = "x") -> str:\n    return HASHER.hash(password + pepper)\n')(f)), {}),
    ('constant passed by keyword to the hash helper', _replace('routers.py', 'hash_password(payload.password)',
        'hash_password(password="fixed")'), {'REQ-005': 'UNVERIFIED'}),
    ('input passed by keyword to the hash helper', _replace('routers.py', 'hash_password(payload.password)',
        'hash_password(password=payload.password)'), {}),
    ('constant passed through a nested hash helper', _replace('security.py', HASH_FUNCTION,
        'def _inner(value: str) -> str:\n    return HASHER.hash(value)\n\n\n'
        'def hash_password(password: str) -> str:\n    return _inner("fixed")\n'), {'REQ-005': 'UNVERIFIED'}),
    ('input passed through a nested hash helper', _replace('security.py', HASH_FUNCTION,
        'def _inner(value: str) -> str:\n    return HASHER.hash(value)\n\n\n'
        'def hash_password(password: str) -> str:\n    return _inner(password)\n'), {}),
    ('constant fallback in a conditional expression', _hash_body(
        '    return HASHER.hash(password if password else "x")\n'), {'REQ-005': 'UNVERIFIED'}),
    ('constant assigned on one branch before hashing', _hash_body(
        '    value = password\n    if not value:\n        value = "x"\n    return HASHER.hash(value)\n'),
        {'REQ-005': 'UNVERIFIED'}),
    # --- ③ 고정 salt ---
    ('fixed salt given as a keyword', _hash_body('    return HASHER.hash(password, salt=b"0123456789abcdef")\n'),
        {'REQ-005': 'FAIL'}),
    ('fixed salt kept in a module constant', lambda f: _replace('security.py', HASH_FUNCTION,
        'SALT = b"0123456789abcdef"\n\ndef hash_password(password: str) -> str:\n    return HASHER.hash(password, salt=SALT)\n')(f),
        {'REQ-005': 'FAIL'}),
    ('fixed salt written as a literal expression', _hash_body('    return HASHER.hash(password, salt=b"abcd" * 4)\n'),
        {'REQ-005': 'FAIL'}),
    ('salt length of one byte is not unique enough', _replace('security.py', WEAK_HASHER,
        'PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, salt_len=1)'), {'REQ-005': 'FAIL'}),
    ('salt length of sixteen bytes is fine', _replace('security.py', WEAK_HASHER,
        'PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1, salt_len=16)'), {}),
    ('salt from an unknown source is not proven', _hash_body('    return HASHER.hash(password, salt=make_salt())\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('random salt from os.urandom is fine', lambda f: dict(_hash_body('    return HASHER.hash(password, salt=os.urandom(16))\n')(f), **{
        'security.py': 'import os\n' + _hash_body('    return HASHER.hash(password, salt=os.urandom(16))\n')(f)['security.py']}), {}),
    ('unpacked options passed to hash', _hash_body('    return HASHER.hash(password, **options)\n'),
        {'REQ-005': 'UNVERIFIED'}),
    # --- ④ UNIQUE는 이메일 칸 자신의, 사용자 표(비밀번호 칸이 있는 표)의 것이어야 한다 ---
    ('Codex case: UNIQUE only on the id column of another SQL table', _without_email_unique(_append('models.py',
        _run_sql('\nSCHEMA = "CREATE TABLE other (id INTEGER UNIQUE, email TEXT)"\n'))), {'REQ-003': 'FAIL'}),
    ('UNIQUE on another column of the same SQL table', _without_email_unique(_models_file(
        'SCHEMA = """CREATE TABLE users (id INTEGER UNIQUE, email TEXT, password_hash TEXT)"""\n')),
        {**UNVERIFIED_STORAGE, 'REQ-003': 'FAIL'}),
    ('unique email in a SQL table without a password column', _without_email_unique(_models_file(
        'SCHEMA = """CREATE TABLE audit (id INTEGER, email TEXT UNIQUE)"""\n')), {**UNVERIFIED_STORAGE, 'REQ-003': 'UNVERIFIED'}),
    ('unique email in the SQL users table', _models_file(
        'SCHEMA = """CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT)"""\n'),
        UNVERIFIED_STORAGE),
    ('table constraint and quoted names in SQL', _models_file(
        'SCHEMA = """CREATE TABLE IF NOT EXISTS "main"."users" (id INTEGER, "email" TEXT, "password_hash" TEXT, '
        'CONSTRAINT uq_email UNIQUE (email))"""\n'), UNVERIFIED_STORAGE),
    ('primary key on the email column in SQL', _models_file(
        'SCHEMA = """CREATE TABLE users (email TEXT, password_hash TEXT, PRIMARY KEY (email))"""\n'), UNVERIFIED_STORAGE),
    ('unique index on another table is undetermined', _models_file(
        'SCHEMA = """CREATE TABLE users (id INTEGER, email TEXT, password_hash TEXT)"""\n'
        'INDEX = "CREATE UNIQUE INDEX ux ON audit(email)"\n'), {**UNVERIFIED_STORAGE, 'REQ-003': 'UNVERIFIED'}),
    ('unique index on the users table', _models_file(
        'SCHEMA = """CREATE TABLE users (id INTEGER, email TEXT, password_hash TEXT)"""\n'
        'INDEX = "CREATE UNIQUE INDEX ux ON users(email)"\n'), UNVERIFIED_STORAGE),
    ('unique email in an ORM class without a password column', _without_email_unique(_append('models.py',
        '\n\nclass Audit(Base):\n    __tablename__ = "audit"\n    id = Column(Integer, primary_key=True)\n'
        '    email = Column(String, unique=True)\n')), {'REQ-003': 'FAIL'}),
    ('UniqueConstraint in an ORM class without a password column', _without_email_unique(_append('models.py',
        '\n\nclass Audit(Base):\n    __tablename__ = "audit"\n    id = Column(Integer, primary_key=True)\n'
        '    email = Column(String)\n    __table_args__ = (UniqueConstraint("email"),)\n')), {'REQ-003': 'FAIL'}),
    ('unique email in a Core Table with a password column', _models_file(
        'from sqlalchemy import Table, Column, String, MetaData\n'
        'users = Table("users", MetaData(), Column("email", String, unique=True), Column("password_hash", String))\n'),
        UNVERIFIED_STORAGE),
    ('unique email in a Core Table without a password column', _models_file(
        'from sqlalchemy import Table, Column, String, MetaData\n'
        'logs = Table("logs", MetaData(), Column("email", String, unique=True))\n'),
        {**UNVERIFIED_STORAGE, 'REQ-003': 'UNVERIFIED'}),
    ('SQLModel field with unique', _models_file(
        'from sqlmodel import SQLModel, Field\n\n\nclass User(SQLModel, table=True):\n'
        '    email: str = Field(unique=True)\n    hashed_password: str\n'), {}),
    ('unique email only in a mixin is not tied to the users table', _models_file(
        'from sqlalchemy import Column, String\n\n\nclass EmailMixin:\n    email = Column(String, unique=True)\n\n\n'
        'class User(EmailMixin, Base):\n    __tablename__ = "users"\n    password_hash = Column(String)\n'),
        {'REQ-003': 'UNVERIFIED'}),
    # --- 같은 점검의 연장: 정리(strip·lower)는 결과가 실제로 쓰일 때만 인정한다 ---
    ('normalization result is thrown away', _replace('schemas.py', 'value = value.strip().lower()', 'value.strip().lower()'),
        {'REQ-003': 'FAIL'}),
    ('normalization result is never read', _replace('schemas.py', 'value = value.strip().lower()',
        'unused = value.strip().lower()'), {'REQ-003': 'FAIL'}),
    ('normalization in two steps is fine', _replace('schemas.py', 'value = value.strip().lower()',
        'value = value.strip()\n        value = value.lower()'), {}),
    # --- 같은 점검의 연장: 평문이 저장되는 다른 길, 비밀번호가 나가는 다른 출구 ---
    ('plaintext stored with setattr', _before_add('    setattr(user, "password_hash", payload.password)\n'),
        {'REQ-005': 'FAIL'}),
    ('plaintext stored through __dict__', _before_add('    user.__dict__["password_hash"] = payload.password\n'),
        {'REQ-005': 'FAIL'}),
    ('plaintext stored through values()', _before_add(
        '    db.execute(update(User).values(password_hash=payload.password))\n'), {'REQ-005': 'FAIL'}),
    ('plaintext stored through a row dictionary', _before_add(
        '    row = {"password_hash": payload.password}\n    db.execute(insert(User), row)\n'), {'REQ-005': 'FAIL'}),
    ('hash stored through values() is fine', _before_add(
        '    db.execute(update(User).values(password_hash=hash_password(payload.password)))\n'), {}),
    ('plaintext stored through raw SQL', _before_add(
        '    db.execute("UPDATE users SET password_hash = ?", (payload.password,))\n'), {'REQ-005': 'FAIL'}),
    ('hash stored through raw SQL is fine', _before_add(
        '    db.execute("UPDATE users SET password_hash = ?", (hash_password(payload.password),))\n'), {}),
    ('raw SQL that does not touch the password column is fine', _before_add(
        '    db.execute("UPDATE users SET email = ?", (payload.email,))\n'), {}),
    ('whole local scope written to the log', _before_add('    logging.info(locals())\n'), {'REQ-006': 'FAIL'}),
    ('password pretty-printed', _before_add('    pprint(payload)\n', 'from pprint import pprint\n'), {'REQ-006': 'FAIL'}),
    ('password written to a file', _before_add('    open("log.txt", "a").write(payload.password)\n'), {'REQ-006': 'FAIL'}),
    ('password put into a response header', _before_add('    response.headers["X-Debug"] = payload.password\n'),
        {'REQ-006': 'FAIL'}),
    ('email written to a file is fine', _before_add('    open("log.txt", "a").write(payload.email)\n'), {}),
]


@pytest.mark.parametrize('label,change,changed', THIRD_REVIEW_CASES, ids=[item[0] for item in THIRD_REVIEW_CASES])
def test_third_review_cases(label, change, changed):
    # 위 사례를 하나씩 판정해서 기대한 결과(GOOD의 판정에서 바뀌는 부분만 적음)와 같은지 확인한다.
    result, _assessment = outcomes(change(dict(GOOD)))
    assert result == {**GOOD_EXPECTED, **changed}


# ---------------------------------------------------------------------------------------------
# 4차 외부 점검(2026-10-16)에서 찾은 "결함인데 PASS" 사례들과 같은 종류의 변형들이다.
#   ① 입력을 버리는 함수·연산(discard(), *= 0, 잘라내기)을 거친 값을 "입력에서 나온 해시"로 인정한 문제
#   ② 이름만 같은 가짜 난수 함수(os를 직접 만든 객체로 바꿈)를 믿은 문제
#   ③ 비밀번호가 아니라 이메일을 해시해도 통과한 문제
#   ④ SQL의 문자열·주석 속 단어(DEFAULT 'unique')를 UNIQUE 제약으로 오인한 문제
#   ⑤ 다른 계정 표(보관용 표)의 UNIQUE가 실제 가입 표의 결함을 가린 문제
#   ⑥ response_model=None이나 응답 객체 직접 반환인데 걸러진다고 믿은 문제
# ---------------------------------------------------------------------------------------------
ARCHIVE_CLASS = ('\n\nclass ArchivedUser(Base):\n    __tablename__ = "archived_users"\n    id = Column(Integer, primary_key=True)\n'
                 '    email = Column(String{unique})\n    password_hash = Column(String)\n')  # 비밀번호 칸이 있는 두 번째 표
HASH_CALL = 'hash_password(payload.password)'  # GOOD의 가입 경로가 비밀번호를 해시하는 호출
PUBLIC_MODEL = '\n\nclass Public(BaseModel):\n    email: str\n'  # 비밀번호 칸이 없는 응답 모델
PLAIN_RESPONSE = '{"success": True, "message": "회원가입에 성공했습니다."}'  # GOOD의 가입 응답


def _security_with(body, head='', tail=''):
    # security.py의 hash_password 본문을 body로 바꾸고 파일 맨 앞에 head, 맨 뒤에 tail을 붙인 소스 묶음 함수를 만든다.
    return lambda files: dict(files, **{'security.py': head + _hash_body(body)(files)['security.py'] + tail})


def _route_response(model, response, schema='', head=''):
    # 가입 경로의 response_model을 model로, 응답을 response로 바꾸고 schemas.py 뒤에 schema, routers.py 앞에 head를 붙인다.
    def change(files):
        files = dict(files)
        files['schemas.py'] += schema
        files['routers.py'] = head + files['routers.py'].replace('status_code=201)', f'status_code=201, response_model={model})').replace(
            PLAIN_RESPONSE, response)
        return files
    return change


FOURTH_REVIEW_CASES = [
    # --- ① 해시하는 값이 "입력의 내용을 통째로 간직한 것"이어야 한다 ---
    ('helper that discards its input', _security_with('    return HASHER.hash(discard(password))\n',
        tail='\ndef discard(value):\n    return "fixed-value"\n'), {'REQ-005': 'UNVERIFIED'}),
    ('helper that returns the other parameter', _security_with('    return HASHER.hash(pick(password, "x"))\n',
        tail='\ndef pick(first, second):\n    return second\n'), {'REQ-005': 'UNVERIFIED'}),
    ('helper that keeps its input is fine', _security_with('    return HASHER.hash(normalize(password))\n',
        tail='\ndef normalize(value):\n    return value.strip()\n'), {}),
    ('augmented multiplication erases the input', _security_with('    password *= 0\n    return HASHER.hash(password)\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('multiplication by zero erases the input', _security_with('    password = password * 0\n    return HASHER.hash(password)\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('empty slice erases the input', _security_with('    return HASHER.hash(password[:0])\n'), {'REQ-005': 'UNVERIFIED'}),
    ('replace can erase the input', _security_with('    return HASHER.hash(password.replace(password, ""))\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('strip with characters can erase the input', _security_with('    return HASHER.hash(password.strip(password))\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('format specification can cut the input', _security_with('    return HASHER.hash(f"{password:.0s}")\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('one character taken from a loop', _security_with('    for char in password:\n        pass\n    return HASHER.hash(char)\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('part of the input taken by unpacking', _security_with('    first, *rest = password\n    return HASHER.hash(first)\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('strip redefined by the project', _security_with('    return HASHER.hash(password.strip())\n',
        tail='\nclass Pw(str):\n    def strip(self):\n        return ""\n'), {'REQ-005': 'UNVERIFIED'}),
    ('builtin str rebound in the file', _security_with('    return HASHER.hash(str(password))\n', head='str = lambda value: "x"\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('unicode normalization keeps the input', _security_with('    return HASHER.hash(unicodedata.normalize("NFKC", password))\n',
        head='import unicodedata\n'), {}),
    ('str and encode keep the input', _security_with('    return HASHER.hash(str(password).encode("utf-8"))\n'), {}),
    ('SecretStr value is accepted', _replace('routers.py', HASH_CALL, 'hash_password(payload.password.get_secret_value())'), {}),
    ('password copied to a local name is fine', _replace('routers.py',
        'user = User(email=payload.email, password_hash=hash_password(payload.password))',
        'pw = payload.password\n    user = User(email=payload.email, password_hash=hash_password(pw))'), {}),
    # --- 해시 결과를 변수에 담았다가 저장하는 흔한 모양 ---
    ('hash kept in a variable and stored later is fine', _replace('routers.py',
        'user = User(email=payload.email, password_hash=hash_password(payload.password))',
        'hashed = hash_password(payload.password)\n    user = User(email=payload.email, password_hash=hashed)'), {}),
    ('variable that is hashed on one branch and plaintext on another', _replace('routers.py',
        'user = User(email=payload.email, password_hash=hash_password(payload.password))',
        'hashed = hash_password(payload.password)\n    if not hashed:\n        hashed = payload.password\n'
        '    user = User(email=payload.email, password_hash=hashed)'), {'REQ-005': 'UNVERIFIED'}),
    ('variable that holds the hash of the email', _replace('routers.py',
        'user = User(email=payload.email, password_hash=hash_password(payload.password))',
        'hashed = hash_password(payload.email)\n    user = User(email=payload.email, password_hash=hashed)'), {'REQ-005': 'UNVERIFIED'}),
    ('variable passed in as a parameter is not known to be a hash', _replace('routers.py',
        'def signup(payload: SignupRequest, db=Depends(get_db)):', 'def signup(payload: SignupRequest, db=Depends(get_db), hashed=None):'),
        {}) if False else ('variable received as a parameter is not known to be a hash', lambda f: _replace('routers.py',
        'user = User(email=payload.email, password_hash=hash_password(payload.password))',
        'user = User(email=payload.email, password_hash=hashed)')(_replace('routers.py',
        'def signup(payload: SignupRequest, db=Depends(get_db)):', 'def signup(payload: SignupRequest, hashed: str, db=Depends(get_db)):')(f)),
        {'REQ-005': 'UNVERIFIED'}),
    # --- ③ 해시하는 값은 비밀번호에서 나온 것이어야 한다(다른 입력 필드를 해시하면 안 된다) ---
    ('email hashed through the helper', _replace('routers.py', HASH_CALL, 'hash_password(payload.email)'), {'REQ-005': 'UNVERIFIED'}),
    ('email hashed directly in the route', _replace('routers.py', HASH_CALL, 'HASHER.hash(payload.email)'),
        {'REQ-005': 'UNVERIFIED'}),
    # --- ② 난수는 진짜 라이브러리의 것으로 확인될 때만 인정한다 ---
    ('fake os object without import', _security_with('    return HASHER.hash(password, salt=os.urandom(16))\n',
        tail='\nclass FakeOS:\n    def urandom(self, size):\n        return b"0123456789abcdef"\nos = FakeOS()\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('imported os replaced by a fake object', _security_with('    return HASHER.hash(password, salt=os.urandom(16))\n',
        head='import os\n', tail='\nclass FakeOS:\n    def urandom(self, size):\n        return b"0123456789abcdef"\nos = FakeOS()\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('project file named os.py shadows the standard library', lambda f: dict(
        _security_with('    return HASHER.hash(password, salt=os.urandom(16))\n', head='import os\n')(f),
        **{'os.py': 'def urandom(size):\n    return b"0123456789abcdef"\n'}), {'REQ-005': 'UNVERIFIED'}),
    ('urandom replaced at runtime', _security_with('    return HASHER.hash(password, salt=os.urandom(16))\n', head='import os\n',
        tail='\nos.urandom = lambda size: b"0123456789abcdef"\n'), {'REQ-005': 'UNVERIFIED'}),
    ('one random byte is too short for a salt', _security_with('    return HASHER.hash(password, salt=os.urandom(1))\n', head='import os\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('salt=None means the library chooses a random salt', _hash_body('    return HASHER.hash(password, salt=None)\n'), {}),
    ('secrets.token_bytes salt is fine', _security_with('    return HASHER.hash(password, salt=secrets.token_bytes(16))\n',
        head='import secrets\n'), {}),
    ('urandom imported by name is fine', _security_with('    return HASHER.hash(password, salt=urandom(16))\n',
        head='from os import urandom\n'), {}),
    ('plaintext stored under an unusual column name', _replace('routers.py', 'password_hash=hash_password(payload.password)',
        'password_hash=hash_password(payload.password), pw=payload.password'), {'REQ-005': 'FAIL'}),
    # --- ④ SQL은 문자열·주석을 걷어 내고, 칸별로, 읽을 수 있을 때만 판정한다 ---
    ('DEFAULT string containing the word unique', _models_file(
        'SCHEMA = "CREATE TABLE users (email TEXT DEFAULT \'unique\', password_hash TEXT)"\n'),
        {**UNVERIFIED_STORAGE, 'REQ-003': 'FAIL'}),
    ('line comment containing the word unique', _models_file(
        'SCHEMA = """CREATE TABLE users (email TEXT, -- unique\n password_hash TEXT)"""\n'), {**UNVERIFIED_STORAGE, 'REQ-003': 'FAIL'}),
    ('block comment containing the word unique', _models_file(
        'SCHEMA = """CREATE TABLE users (email TEXT /* unique */, password_hash TEXT)"""\n'), {**UNVERIFIED_STORAGE, 'REQ-003': 'FAIL'}),
    ('CHECK expression containing the word unique', _models_file(
        'SCHEMA = "CREATE TABLE users (email TEXT CHECK (email <> \'unique\'), password_hash TEXT)"\n'),
        {**UNVERIFIED_STORAGE, 'REQ-003': 'FAIL'}),
    ('commas and parentheses inside a string do not break the columns', _models_file(
        'SCHEMA = "CREATE TABLE users (email TEXT UNIQUE DEFAULT \'a,(b\', password_hash TEXT)"\n'), UNVERIFIED_STORAGE),
    ('MySQL UNIQUE KEY with a name', _models_file(
        'SCHEMA = "CREATE TABLE users (email VARCHAR(255), password_hash VARCHAR(255), UNIQUE KEY ux_email (email))"\n'),
        UNVERIFIED_STORAGE),
    ('SQL with unbalanced parentheses is not guessed', _models_file(
        'SCHEMA = "CREATE TABLE users (email TEXT, password_hash TEXT"\n'), {**UNVERIFIED_STORAGE, 'REQ-003': 'UNVERIFIED'}),
    ('constraint added later with ALTER TABLE is not analyzed', _models_file(
        'SCHEMA = "CREATE TABLE users (email TEXT, password_hash TEXT)"\nALTER = "ALTER TABLE users ADD CONSTRAINT uq UNIQUE (email)"\n'),
        {**UNVERIFIED_STORAGE, 'REQ-003': 'UNVERIFIED'}),
    ('DDL written only in a docstring is not evidence', _without_email_unique(_append('models.py',
        '\n\ndef describe():\n    \"\"\"CREATE TABLE users (email TEXT UNIQUE, password_hash TEXT)\"\"\"\n')), {'REQ-003': 'FAIL'}),
    ('SQL string that is never executed is not evidence', _without_email_unique(_append('models.py',
        '\nSCHEMA = "CREATE TABLE users (email TEXT UNIQUE, password_hash TEXT)"\n')), {'REQ-003': 'FAIL'}),
    ('SQL passed directly to execute counts', _models_file(
        'import sqlite3\nsqlite3.connect(":memory:").execute("CREATE TABLE users (email TEXT UNIQUE, password_hash TEXT)")\n'),
        UNVERIFIED_STORAGE),
    ('SQL wrapped in text() counts', _models_file(
        'from sqlalchemy import text\nSTATEMENT = text("CREATE TABLE users (email TEXT UNIQUE, password_hash TEXT)")\n'),
        UNVERIFIED_STORAGE),
    ('partial unique index in SQL covers only some rows', _models_file(
        'SCHEMA = "CREATE TABLE users (id INTEGER, email TEXT, password_hash TEXT)"\n'
        'IDX = "CREATE UNIQUE INDEX ux ON users(email) WHERE active = 1"\n'), {**UNVERIFIED_STORAGE, 'REQ-003': 'UNVERIFIED'}),
    ('unique index on a computed column in SQL', _models_file(
        'SCHEMA = "CREATE TABLE users (id INTEGER, email TEXT, password_hash TEXT)"\n'
        'IDX = "CREATE UNIQUE INDEX ux ON users(substr(email, 1, 3))"\n'), {**UNVERIFIED_STORAGE, 'REQ-003': 'UNVERIFIED'}),
    ('unique constraint on a computed column in SQL', _models_file(
        'SCHEMA = "CREATE TABLE users (email TEXT, password_hash TEXT, UNIQUE (lower(email)))"\n'),
        {**UNVERIFIED_STORAGE, 'REQ-003': 'UNVERIFIED'}),
    ('SQL unique index with a USING clause', _models_file(
        'SCHEMA = "CREATE TABLE users (id INTEGER, email TEXT, password_hash TEXT)"\n'
        'IDX = "CREATE UNIQUE INDEX ux ON users USING btree (email)"\n'), UNVERIFIED_STORAGE),
    ('partial unique index declared in the ORM', _without_email_unique(_append('models.py',
        '    __table_args__ = (Index("ix_email", "email", unique=True, postgresql_where=text("active")),)\n')),
        {'REQ-003': 'UNVERIFIED'}),
    # --- ⑤ UNIQUE는 가입으로 사용자를 저장하는 표의 것이어야 한다 ---
    ('unique email only on an archive table', _without_email_unique(_append('models.py', ARCHIVE_CLASS.format(unique=', unique=True'))),
        {'REQ-003': 'FAIL'}),
    ('unique email on both user tables', _append('models.py', ARCHIVE_CLASS.format(unique=', unique=True')), {}),
    ('archive table without unique does not matter when signup uses the other table', _append('models.py',
        ARCHIVE_CLASS.format(unique='')), {}),
    ('two user tables and an unknown signup table', lambda f: _replace('routers.py',
        'User(email=payload.email, password_hash=hash_password(payload.password))', 'User(**payload.model_dump())')(
        _append('models.py', ARCHIVE_CLASS.format(unique=''))(f)), {'REQ-003': 'UNVERIFIED', 'REQ-005': 'UNVERIFIED'}),
    ('SQL: unique email only on an archive table', _models_file(
        'SCHEMA = """CREATE TABLE users (email TEXT, password_hash TEXT);\nCREATE TABLE archived (email TEXT UNIQUE, password_hash TEXT);"""\n'
        'INSERT = "INSERT INTO users (email, password_hash) VALUES (?, ?)"\n'), {**UNVERIFIED_STORAGE, 'REQ-003': 'FAIL'}),
    # --- ⑥ response_model이 실제로 걸러 주는 경우만 인정한다 ---
    ('response_model=None returns the request model', _route_response('None', 'payload'), {'REQ-006': 'FAIL'}),
    ('response_model=False returns the request model', _route_response('False', 'payload'), {'REQ-006': 'FAIL'}),
    ('direct JSONResponse is not filtered by response_model', _route_response('Public', 'JSONResponse(content=payload.model_dump())',
        PUBLIC_MODEL, 'from fastapi.responses import JSONResponse\nfrom schemas import Public\n'), {'REQ-006': 'FAIL'}),
    ('response model defined in the project filters the object', _route_response('Public', 'payload', PUBLIC_MODEL,
        'from schemas import Public\n'), {}),
    ('list of a project response model filters too', _route_response('list[Public]', '[payload]', PUBLIC_MODEL,
        'from schemas import Public\n'), {}),
    ('response model that cannot be found is not trusted', _route_response('ExternalModel', 'payload'), {'REQ-006': 'FAIL'}),
    ('helper that builds a response object is not trusted', lambda f: _append('routers.py',
        '\n\ndef build(payload):\n    return JSONResponse(content={"ok": 1})\n')(_route_response(
        'Public', 'build(payload)', PUBLIC_MODEL, 'from fastapi.responses import JSONResponse\nfrom schemas import Public\n')(f)),
        {'REQ-006': 'UNVERIFIED'}),
    ('route registered with add_api_route returns the request model', _append('routers.py',
        '\n\ndef signup2(payload: SignupRequest):\n    return payload\n\n\nrouter.add_api_route("/api/signup2", signup2, response_model=None)\n'),
        {'REQ-006': 'FAIL'}),
    ('Flask route returning the password', _append('routers.py',
        '\n\n@app.route("/x", methods=["POST"])\ndef flask_signup(payload: SignupRequest):\n    return jsonify({"p": payload.password})\n'),
        {'REQ-006': 'FAIL'}),
    ('Flask route that returns a safe value is fine', _append('routers.py',
        '\n\n@app.route("/x", methods=["POST"])\ndef flask_signup(payload: SignupRequest):\n    return jsonify({"ok": True})\n'), {}),
]


@pytest.mark.parametrize('label,change,changed', FOURTH_REVIEW_CASES, ids=[item[0] for item in FOURTH_REVIEW_CASES])
def test_fourth_review_cases(label, change, changed):
    # 위 사례를 하나씩 판정해서 기대한 결과(GOOD의 판정에서 바뀌는 부분만 적음)와 같은지 확인한다.
    result, _assessment = outcomes(change(dict(GOOD)))
    assert result == {**GOOD_EXPECTED, **changed}


# ---------------------------------------------------------------------------------------------
# 5차 외부 점검(2026-10-16)에서 찾은 사례와 같은 종류의 변형들이다.
# "변수 이름이나 넘긴 인자의 이름"이 아니라 "실제로 해시되는 값이 어느 입력의 어느 필드에서 왔는지"를 따라가야 한다.
#   ① 이름만 password인 변수에 이메일을 담아 해시해도 통과한 문제
#   ② 비밀번호와 이메일을 함께 받아 이메일만 돌려주는 함수의 결과를 해시해도 통과한 문제
#   ③ 같은 종류: 입력 객체의 필드를 덮어쓴 뒤 해시, self 필드, 튜플 교환 등
# ---------------------------------------------------------------------------------------------
SIGNUP_USER = 'user = User(email=payload.email, password_hash=hash_password(payload.password))'  # GOOD이 사용자를 만드는 줄


def _signup_user(text):
    # 사용자를 만드는 줄을 text로 바꾼 소스 묶음 함수를 만든다.
    return _replace('routers.py', SIGNUP_USER, text)


def _router_helper(text):
    # routers.py 맨 뒤에 도우미 함수(또는 클래스)를 덧붙인 소스 묶음 함수를 만든다.
    return _append('routers.py', '\n\n' + text)


def _both(*changes):
    # 여러 변경을 차례로 적용하는 소스 묶음 함수를 만든다.
    def run(files):
        for change in changes:
            files = change(files)
        return files
    return run


def _form_route(body, item=False):
    # routers.py에 폼 객체(form)를 받는 경로 함수를 하나 더 덧붙인다. 이 함수는 body를 실행한 뒤 form.passwd를 해시해 저장한다.
    # item=True이면 form을 사전처럼 꺼내 쓴다(form["passwd"]).
    value = 'form["passwd"]' if item else 'form.passwd'
    email = 'form["email"]' if item else 'form.email'
    return _router_helper(
        f'def signup_form(form, db=Depends(get_db)):\n{body}    user = User(email={email}, password_hash=hash_password({value}))\n    return {{}}\n')


FIFTH_REVIEW_CASES = [
    # --- ① 이름이 아니라 값의 출처를 본다 ---
    ('variable named password that holds the email', _signup_user(
        'password = payload.email\n    user = User(email=payload.email, password_hash=hash_password(password))'),
        {'REQ-005': 'UNVERIFIED'}),
    ('parameter-like name overwritten by the email', _signup_user(
        'password = payload.password\n    password = payload.email\n    user = User(email=payload.email, password_hash=hash_password(password))'),
        {'REQ-005': 'UNVERIFIED'}),
    ('email taken from an object whose name looks like a password', _signup_user(
        'password_form = payload\n    user = User(email=payload.email, password_hash=hash_password(password_form.email))'),
        {'REQ-005': 'UNVERIFIED'}),
    ('email hashed directly through a variable named password', _signup_user(
        'password = payload.email\n    user = User(email=payload.email, password_hash=HASHER.hash(password))'),
        {'REQ-005': 'UNVERIFIED'}),
    ('two-step copy where the email slips in', _signup_user(
        'first = payload.password\n    first = payload.email\n    second = first\n'
        '    user = User(email=payload.email, password_hash=hash_password(second))'), {'REQ-005': 'UNVERIFIED'}),
    ('two-step copy of the password is fine', _signup_user(
        'first = payload.password\n    second = first\n    user = User(email=payload.email, password_hash=hash_password(second))'), {}),
    ('swapped values in a tuple assignment', _signup_user(
        'password, email = payload.email, payload.password\n    user = User(email=payload.email, password_hash=hash_password(password))'),
        {'REQ-005': 'UNVERIFIED'}),
    ('tuple assignment in the right order is fine', _signup_user(
        'email, password = payload.email, payload.password\n    user = User(email=email, password_hash=hash_password(password))'), {}),
    # --- ② 함수가 실제로 돌려주는 값의 출처를 본다 ---
    ('helper that gets both but returns only the email', _both(
        _router_helper('def pick(password, email):\n    return email\n'),
        _signup_user('user = User(email=payload.email, password_hash=hash_password(pick(payload.password, payload.email)))')),
        {'REQ-005': 'UNVERIFIED'}),
    ('helper that returns the password is fine', _both(
        _router_helper('def pick(password, email):\n    return password\n'),
        _signup_user('user = User(email=payload.email, password_hash=hash_password(pick(payload.password, payload.email)))')), {}),
    ('helper that returns the email field of an object', _both(
        _router_helper('def extract(form):\n    return form.email\n'),
        _signup_user('user = User(email=payload.email, password_hash=hash_password(extract(payload)))')), {'REQ-005': 'UNVERIFIED'}),
    ('helper that returns the password field of an object is fine', _both(
        _router_helper('def extract(form):\n    return form.password\n'),
        _signup_user('user = User(email=payload.email, password_hash=hash_password(extract(payload)))')), {}),
    ('method that returns the email', _both(
        _router_helper('class Form:\n    def get_email(self):\n        return self.email\n'),
        _signup_user('user = User(email=payload.email, password_hash=hash_password(Form().get_email()))')),
        {'REQ-005': 'UNVERIFIED'}),
    ('hash helper that ignores its password parameter and hashes the email', _both(
        _replace('security.py', HASH_FUNCTION,
                 'def hash_password(password: str, email: str = "") -> str:\n    return HASHER.hash(email)\n'),
        _signup_user('user = User(email=payload.email, password_hash=hash_password(payload.password, payload.email))')),
        {'REQ-005': 'UNVERIFIED'}),
    ('email and password hashed together is fine', _signup_user(
        'user = User(email=payload.email, password_hash=hash_password(payload.email + payload.password))'), {}),
    ('email and a constant only', _signup_user(
        'user = User(email=payload.email, password_hash=hash_password(payload.email + "x"))'), {'REQ-005': 'UNVERIFIED'}),
    ('password or email depending on a condition', _signup_user(
        'secret = payload.password if payload.email else payload.email\n'
        '    user = User(email=payload.email, password_hash=hash_password(secret))'), {'REQ-005': 'UNVERIFIED'}),
    # --- ③ 입력 객체의 필드를 덮어쓰면 그 필드는 더 이상 원래 입력이 아니다 ---
    # 아래 사례는 필드 이름을 passwd로 해서(저장 칸 이름 password와 달라서) "필드를 덮어쓴 것" 자체를 판정하는지 확인한다.
    ('passwd field overwritten with the email', _form_route('    form.passwd = form.email\n'), {'REQ-005': 'UNVERIFIED'}),
    ('passwd item overwritten with the email', _form_route('    form["passwd"] = form["email"]\n', item=True), {'REQ-005': 'UNVERIFIED'}),
    ('passwd field overwritten through setattr', _form_route('    setattr(form, "passwd", form.email)\n'), {'REQ-005': 'UNVERIFIED'}),
    ('passwd field overwritten through an alias', _form_route('    alias = form\n    alias.passwd = form.email\n'), {'REQ-005': 'UNVERIFIED'}),
    ('passwd field overwritten through vars().update', _form_route('    vars(form).update({"passwd": form.email})\n'),
        {'REQ-005': 'UNVERIFIED'}),
    ('passwd field overwritten by a helper function', _both(
        _router_helper('def scrub(item):\n    item.passwd = item.email\n'), _form_route('    scrub(form)\n')), {'REQ-005': 'UNVERIFIED'}),
    ('passwd field overwritten two helper calls deep', _both(
        _router_helper('def inner(item):\n    item.passwd = item.email\n\n\ndef outer(item):\n    inner(item)\n'),
        _form_route('    outer(form)\n')), {'REQ-005': 'UNVERIFIED'}),
    ('another field changing does not matter for passwd', _form_route('    form.email = form.email.strip().lower()\n'), {}),
    ('helper that only reads the form is fine', _both(
        _router_helper('def audit(item):\n    return item.email\n'), _form_route('    audit(form)\n')), {}),
    ('password field overwritten with the email', _signup_user('payload.password = payload.email\n    ' + SIGNUP_USER),
        {'REQ-005': 'UNVERIFIED'}),
    ('password field overwritten through setattr', _signup_user('setattr(payload, "password", payload.email)\n    ' + SIGNUP_USER),
        {'REQ-005': 'UNVERIFIED'}),
    ('password field overwritten through an alias', _signup_user('alias = payload\n    alias.password = payload.email\n    ' + SIGNUP_USER),
        {'REQ-005': 'UNVERIFIED'}),
    ('password field overwritten through vars().update', _signup_user(
        'vars(payload).update({"password": payload.email})\n    ' + SIGNUP_USER), {'REQ-005': 'UNVERIFIED'}),
    ('password field overwritten by a helper function', _both(
        _router_helper('def scrub(form):\n    form.password = form.email\n'), _signup_user('scrub(payload)\n    ' + SIGNUP_USER)),
        {'REQ-005': 'UNVERIFIED'}),
    ('password field overwritten two helper calls deep', _both(
        _router_helper('def inner(form):\n    form.password = form.email\n\n\ndef outer(form):\n    inner(form)\n'),
        _signup_user('outer(payload)\n    ' + SIGNUP_USER)), {'REQ-005': 'UNVERIFIED'}),
    ('another field changing does not matter', _signup_user('payload.email = payload.email.strip().lower()\n    ' + SIGNUP_USER), {}),
    ('helper that only reads the input is fine', _both(
        _router_helper('def audit(form):\n    return form.email\n'), _signup_user('audit(payload)\n    ' + SIGNUP_USER)), {}),
    ('self field is not a caller-supplied input', _append('security.py',
        '\n\nclass Service:\n    def __init__(self, email):\n        self.__dict__["password"] = email\n\n'
        '    def make(self):\n        return User(email="x", password_hash=HASHER.hash(self.password))\n'),
        {'REQ-005': 'UNVERIFIED'}),
]


@pytest.mark.parametrize('label,change,changed', FIFTH_REVIEW_CASES, ids=[item[0] for item in FIFTH_REVIEW_CASES])
def test_fifth_review_cases(label, change, changed):
    # 위 사례를 하나씩 판정해서 기대한 결과(GOOD의 판정에서 바뀌는 부분만 적음)와 같은지 확인한다.
    result, _assessment = outcomes(change(dict(GOOD)))
    assert result == {**GOOD_EXPECTED, **changed}
