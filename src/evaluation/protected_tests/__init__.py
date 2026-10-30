# SCN-001 회원가입 보호 테스트 묶음(D2)을 읽고, 두 곳에서 쓰는 모양으로 바꿔 주는 모듈이다.
#   1) 개발 순환 안(dev PROTECTED, Ver1만): 역할별 보호 파일 설정(unit_test_scope)과 보호 케이스(owned_cases)
#   2) 최종 독립 평가(Ver1·Ver2): 검사 계획·꼬리표(evaluation_inputs)와 검사 결과(final_check_results)
#
# 묶음 구성(suite.json). QA와 Security는 같은 방식(실행 테스트)이고, 검사마다 시험지 세 벌이 있다.
#   - 시험지(set): practice(순환용, Ver1 개발 순환 안에서 에이전트가 결과를 봄), pilot(시범 최종용, 조정에 쓰면 소모),
#     final(공식 최종용, Ver1·Ver2 공식 비교 채점에만). 세 벌은 같은 검사를 같은 구조로 하되 입력 값·모양·순서가 다르다.
#   - tests/signup/signup_link.py              : 연결부. 생성물 규격이 바뀌면 이 파일만 고친다.
#   - tests/signup/signup_qa_checks.py         : QA 검사 내용(QA-SIGNUP-001~006)
#   - tests/signup/signup_security_checks.py   : Security 검사 내용(SEC-SIGNUP-002·003·005)
#   - tests/signup/practice_qa.py·practice_security.py·exam_pilot.py·exam_final.py : 시험지(입력만 다름)
#   - 정적 판정(security/assess.py)은 점수에 넣지 않고 최종 평가의 보조 점검으로만 쓴다(final_evaluation).
# 순환 안 보호 케이스는 그 역할의 요구사항에만 연결할 수 있다(QA: REQ-001·002·003·004·007, Security: REQ-003·005·006).
# 이 모듈은 테스트를 실행하지 않는다. 실행은 dev run_unit_tests(컨테이너) 또는 D5 채점기가 한다.
#
# [처음 읽는 분을 위한 용어]
# - 보호 테스트(PROTECTED): 개발 에이전트가 읽거나 고칠 수 없게 따로 넣어 실행하는 테스트. 에이전트가 테스트를 바꿔
#   통과시키는 일을 막는다.
# - 범위(scope): dev 설정에서 한 번에 실행하는 파일 묶음. in-loop-qa·in-loop-security(순환 안), practice·pilot·final(최종 평가).
# - 선택자(selector): dev 설정에서 보호 범위를 부르는 이름. 패턴(pattern): 그 범위에서 실행할 테스트 파일 이름 규칙.
# - 지문(suite SHA-256): 파일 내용으로 만든 해시. 한 글자라도 바뀌면 값이 달라져서 "같은 묶음"인지 확인할 수 있다.
import ast  # 보호 파일을 실행하지 않고 읽어 시험 메서드 목록 만들기
import hashlib  # SHA-256 지문 계산
import json  # suite.json 읽기, 지문 계산용 canonical JSON 만들기
import re  # 선택자·패턴·요구사항 이름 모양 검사(정규식)
from pathlib import Path  # 파일 경로 다루기
from typing import Mapping, Optional  # 자료형 이름표(실행에는 영향 없음)

from ..aggregation import CheckPlan, _uuid4  # 검사 계획 상자, UUIDv4 모양 검사
from ..check_adapters import check_results_from_unit_report  # unittest 보고서 → 검사 결과
from ..independent import EvaluationBinding  # 검사 꼬리표(요구사항·관점·제목)

ROOT = Path(__file__).resolve().parent  # 이 폴더(protected_tests). 묶음 파일 경로는 여기서부터 센다
SUITE_FILE = ROOT / 'suite.json'  # 묶음 정의 파일(시험지·범위·검사 목록)
SCOPES = ('in-loop-qa', 'in-loop-security', 'practice', 'pilot', 'final')  # 쓸 수 있는 범위 이름
SETS = ('practice', 'pilot', 'final')  # 시험지 세 벌(최종 평가는 이 중 하나로 채점하고 practice는 참고로도 돈다)
ROLES = ('QA', 'SECURITY')  # 검사 관점이자 순환 안에서 그 검사를 실행하는 에이전트 역할
LOOP_SCOPES = {'QA': 'in-loop-qa', 'SECURITY': 'in-loop-security'}  # 역할별 순환 안 범위
_SELECTOR = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')           # dev qa_contract.py와 같은 규칙
_PATTERN = re.compile(r'[A-Za-z0-9_*?\[\].-]{1,128}\.py\Z')   # dev unit_runner.py와 같은 규칙
_REQUIREMENT = re.compile(r'REQ-\d{3}\Z')  # 요구사항 이름 모양(예: REQ-001)
_METHOD = re.compile(r'test_[a-z0-9_]{1,120}\Z')  # 시험 메서드 이름


def _text(value, name):
    # 값이 빈칸이 아닌 글자인지 확인한다. 아니면 어떤 필드(name)인지 적어 ValueError로 멈춘다.
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be nonempty text')


def unit_test_id(check: Mapping, exam: str, suite: Mapping) -> str:
    # 그 시험지(exam)에서 검사(check)를 실행하는 unittest ID("모듈.클래스.메서드")
    return suite['sets'][exam][check['perspective']] + '.' + check['method']


def load_suite(path: Path = SUITE_FILE) -> dict:
    # suite.json을 읽고 모양을 확인한다. 이상하면 ValueError로 멈춘다.
    suite = json.loads(Path(path).read_text(encoding='utf-8'))
    # 1) 머리 부분: 형식 버전, 시험지 세 벌, 범위 다섯 개, 이름·버전 같은 필수 글자 필드
    if (suite.get('schemaVersion') != 2 or set(suite.get('scopes', {})) != set(SCOPES)
            or set(suite.get('sets', {})) != set(SETS)):
        raise ValueError('Unsupported protected suite')
    for name in ('suiteId', 'suiteVersion', 'scenarioKey', 'toolName', 'sourceDirectory'):
        _text(suite.get(name), name)
    for exam, classes in suite['sets'].items():
        if not isinstance(classes, dict) or set(classes) != set(ROLES):
            raise ValueError('Each set names a test class for every role')
    # 2) 범위마다 시험지·역할·선택자·패턴과 파일 목록을 확인한다. 파일은 sourceDirectory 바로 아래의 .py여야 한다
    #    (dev가 보호 파일을 그 폴더에 넣어 실행하므로 하위 폴더·다른 위치는 받지 않는다).
    directory = suite['sourceDirectory']
    defined = {}  # {범위 이름: 그 범위 파일에 실제로 있는 시험 ID("모듈.클래스.메서드")들}
    for scope_name, scope in suite['scopes'].items():
        if not _SELECTOR.fullmatch(scope.get('selector', '')) or not _PATTERN.fullmatch(scope.get('pattern', '')):
            raise ValueError('Invalid scope selector or pattern')
        if scope.get('set') not in SETS:
            raise ValueError('Scope must name its set')
        role = scope.get('role')
        if (scope_name in LOOP_SCOPES.values()) != (role is not None) or role is not None and (
                LOOP_SCOPES.get(role) != scope_name or scope['set'] != 'practice'):
            raise ValueError('Only in-loop scopes have a role, and they run the practice set')
        files = scope.get('files')
        if not files or len(set(files)) != len(files):
            raise ValueError('Scope files must be nonempty and unique')
        for file in files:
            if not file.startswith(directory + '/') or '/' in file[len(directory) + 1:] or not file.endswith('.py'):
                raise ValueError('Protected files must sit directly in the source directory')
            if not (ROOT / file).is_file():
                raise ValueError('Protected file is missing')
        defined[scope_name] = _defined_tests(files)
    if len({scope['selector'] for scope in suite['scopes'].values()}) != len(SCOPES):
        raise ValueError('Scopes need distinct selectors')
    # 3) 검사 목록: 번호·이름·요구사항·관점·메서드를 확인하고 겹침을 막는다. 검사마다 모든 시험지에 시험이 있어야 하고,
    #    그 시험이 그 시험지를 쓰는 범위 파일 안에 있어야 한다(순환 안 범위는 자기 역할의 검사만).
    seen = {'key': set(), 'checkId': set(), 'method': set()}  # 이미 나온 값
    for check in suite.get('checks', []):
        _uuid4(check.get('checkId'))
        for name in ('key', 'title', 'expectedResult'):
            _text(check.get(name), name)
        if not _REQUIREMENT.fullmatch(check.get('requirementKey', '')):
            raise ValueError('Invalid requirement key')
        if check.get('perspective') not in ROLES:
            raise ValueError('Unsupported perspective')
        if not _METHOD.fullmatch(check.get('method') or ''):
            raise ValueError('Invalid test method')
        for scope_name, scope in suite['scopes'].items():
            if scope.get('role') not in (None, check['perspective']):
                continue
            if unit_test_id(check, scope['set'], suite) not in defined[scope_name]:
                raise ValueError('Check test is not part of its scope')
        for name in seen:
            if check[name] in seen[name]:
                raise ValueError(f'Duplicate check {name}')
            seen[name].add(check[name])
    if not seen['key']:
        raise ValueError('Protected suite has no checks')
    return suite


def _defined_tests(files) -> set:
    # 파일을 실행하지 않고 읽어(AST) 그 안에 정의된 시험 ID("모듈.클래스.test_메서드")를 모은다.
    found = set()
    for file in files:
        module = Path(file).stem
        for node in ast.parse((ROOT / file).read_text(encoding='utf-8')).body:
            if isinstance(node, ast.ClassDef):
                found |= {f'{module}.{node.name}.{item.name}' for item in node.body
                          if isinstance(item, ast.FunctionDef) and item.name.startswith('test')}
    return found


def _scope(scope: str, suite: Optional[dict]):
    # (묶음, 그 범위의 설정)을 돌려준다. 묶음을 주지 않으면 suite.json을 읽는다. 모르는 범위 이름이면 멈춘다.
    suite = suite or load_suite()
    if scope not in SCOPES:
        raise ValueError('Unknown scope')
    return suite, suite['scopes'][scope]


def protected_files(scope: str, suite: Optional[dict] = None) -> dict[str, str]:
    # {"tests/signup/...py": 파일 내용} 사전. dev 보호 파일 설정(protected_files)에 그대로 넣는다.
    _suite, entry = _scope(scope, suite)
    return {file: (ROOT / file).read_text(encoding='utf-8') for file in sorted(entry['files'])}


def suite_sha256(scope: str, suite: Optional[dict] = None) -> str:
    # 묶음의 지문. 정렬한 [{path, sha256, sizeBytes}] 목록을 canonical JSON으로 만들어 SHA-256을 구한다
    # (dev unittest 도구의 Input Manifest와 같은 방식). 독립 평가 보고서의 suite_sha256에 쓴다.
    entries = []
    for path, content in protected_files(scope, suite).items():
        data = content.encode('utf-8')
        entries.append({'path': path, 'sha256': hashlib.sha256(data).hexdigest(), 'sizeBytes': len(data)})
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def unit_test_scope(scope: str, *, suite_ref: str, suite: Optional[dict] = None) -> dict:
    # dev UnitTestScope 설정 한 개(PROTECTED). suite_ref는 동결 RunConfiguration의 protected_test_suite_ref와 같아야 한다.
    # Security 범위에는 실행할 역할(role)을 적는다(적지 않으면 dev는 QA 범위로 본다).
    suite, entry = _scope(scope, suite)
    config = {'name': entry['selector'], 'kind': 'PROTECTED', 'pattern': entry['pattern'],
              'source_directory': suite['sourceDirectory'], 'protected_files': protected_files(scope, suite),
              'protected_suite_ref': suite_ref}
    if entry.get('role') not in (None, 'QA'):
        config['role'] = entry['role']
    return config


def _requirement_ids(scenario_contract: Mapping, suite: dict) -> dict[str, str]:
    # 동결 시나리오 정책에서 {REQ-001: 요구사항 UUID} 표를 만든다.
    if not isinstance(scenario_contract, Mapping) or scenario_contract.get('scenarioKey') != suite['scenarioKey']:
        raise ValueError('Scenario contract does not match the protected suite')
    ids = {}
    for item in scenario_contract.get('requirements', []):
        key, requirement_id = item.get('key'), str(item.get('requirementId'))
        _uuid4(requirement_id)
        if key in ids:
            raise ValueError('Duplicate requirement key in scenario contract')
        ids[key] = requirement_id
    missing = {check['requirementKey'] for check in suite['checks']} - set(ids)
    if missing:
        raise ValueError('Scenario contract lacks a requirement used by the suite')
    return ids


def owned_cases(scenario_contract: Mapping, role: str, suite: Optional[dict] = None) -> dict:
    # 그 역할(QA·SECURITY)의 순환 안 PROTECTED 케이스(dev owned-qa-cases·owned-security-cases 파일 내용).
    # 보고서의 모든 테스트가 여기 묶여야 한다. 순환용(practice) 시험지만 넣는다.
    if role not in ROLES:
        raise ValueError('Unknown role')
    suite, entry = _scope(LOOP_SCOPES[role], suite)
    ids = _requirement_ids(scenario_contract, suite)
    return {'schemaVersion': 1, 'cases': [
        {'toolName': suite['toolName'], 'selector': entry['selector'], 'testId': unit_test_id(check, 'practice', suite),
         'requirementId': ids[check['requirementKey']], 'title': check['title'],
         'expectedResult': check['expectedResult']}
        for check in suite['checks'] if check['perspective'] == role]}


def owned_qa_cases(scenario_contract: Mapping, suite: Optional[dict] = None) -> dict:
    # QA의 순환 안 PROTECTED 케이스(dev configs/owned-qa-cases.local.json 내용)
    return owned_cases(scenario_contract, 'QA', suite)


def evaluation_inputs(scenario_contract: Mapping, exam: str, suite: Optional[dict] = None) -> dict:
    # 시험지(exam: practice·pilot·final) 하나로 최종 독립 평가(build_evaluation_report)에 넣을 계획·꼬리표와
    # 결과 대응표를 만든다. 점수 비교는 pilot·final로 하고, practice는 연습 효과를 재는 참고 채점에만 쓴다.
    suite = suite or load_suite()
    if exam not in SETS:
        raise ValueError('Unknown exam set')
    ids = _requirement_ids(scenario_contract, suite)
    checks = suite['checks']
    return {
        'exam': exam, 'scope': exam,
        'suite_id': suite['suiteId'], 'suite_version': suite['suiteVersion'],
        'suite_sha256': suite_sha256(exam, suite),
        'plan': tuple(CheckPlan(test_id=check['checkId']) for check in checks),
        'bindings': {check['checkId']: EvaluationBinding(ids[check['requirementKey']], check['perspective'],
                                                         check['key'] + ' ' + check['title'])
                     for check in checks},
        'required_requirement_ids': tuple(dict.fromkeys(ids[check['requirementKey']] for check in checks)),
        'unit_cases': {unit_test_id(check, exam, suite): check['checkId'] for check in checks},
        'expected': {check['checkId']: check['expectedResult'] for check in checks},
    }


def final_check_results(inputs: dict, *, unit_report: Optional[dict], unit_evidence_id: str,
                        runner_error: str = 'TEST_RUNNER_ERROR') -> tuple:
    # 실행 테스트 보고서를 최종 평가의 검사 결과 목록으로 바꾼다(모든 검사가 실행 검사).
    return check_results_from_unit_report(unit_report, inputs['unit_cases'], evidence_id=unit_evidence_id,
                                          expected=inputs['expected'], runner_error=runner_error)
