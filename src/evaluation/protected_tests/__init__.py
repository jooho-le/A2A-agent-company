# SCN-001 회원가입 보호 테스트 묶음(D2)을 읽고, 두 곳에서 쓰는 모양으로 바꿔 주는 모듈이다.
#   1) 개발 순환 안(dev PROTECTED): 보호 파일 설정(unit_test_scope)과 보호 QA 케이스(owned_qa_cases)
#   2) 최종 독립 평가: 검사 계획·꼬리표(evaluation_inputs)와 검사 결과(final_check_results)
#
# 묶음 구성(suite.json)
#   - tests/signup/signup_link.py      : 연결부. 생성물 규격이 바뀌면 이 파일만 고친다.
#   - tests/signup/protected_signup_qa.py    : QA-SIGNUP-001·002·003·004·006 (순환 안 + 최종)
#   - tests/signup/protected_signup_final.py : QA-SIGNUP-005, SEC-SIGNUP-002·005 (최종 평가 전용)
#   - 정적 검사 SEC-SIGNUP-001·003·004는 security/assess.py가 판정하고 최종 평가에서 함께 집계한다.
# 순환 안 보호 테스트는 QA 담당 요구사항(REQ-001·002·003·004·007)만 연결할 수 있다(dev qa_services.py).
# 이 모듈은 테스트를 실행하지 않는다. 실행은 dev run_unit_tests(컨테이너) 또는 D5 채점기가 한다.
#
# [처음 읽는 분을 위한 용어]
# - 보호 테스트(PROTECTED): 개발 에이전트가 읽거나 고칠 수 없게 따로 넣어 실행하는 테스트. 에이전트가 테스트를 바꿔
#   통과시키는 일을 막는다.
# - 범위(scope): "in-loop"(개발 순환 안에서 QA가 돌리는 것)와 "final"(최종 독립 평가에서 돌리는 것) 두 가지.
# - 선택자(selector): dev 설정에서 보호 범위를 부르는 이름. 패턴(pattern): 그 범위에서 실행할 테스트 파일 이름 규칙.
# - 지문(suite SHA-256): 파일 내용으로 만든 해시. 한 글자라도 바뀌면 값이 달라져서 "같은 묶음"인지 확인할 수 있다.
import hashlib  # SHA-256 지문 계산
import json  # suite.json 읽기, 지문 계산용 canonical JSON 만들기
import re  # 선택자·패턴·요구사항 이름 모양 검사(정규식)
from pathlib import Path  # 파일 경로 다루기
from typing import Mapping, Optional  # 자료형 이름표(실행에는 영향 없음)

from ..aggregation import CheckPlan, _uuid4  # 검사 계획 상자, UUIDv4 모양 검사
from ..check_adapters import check_results_from_assessment, check_results_from_unit_report  # 결과 어댑터 두 가지
from ..independent import EvaluationBinding  # 검사 꼬리표(요구사항·관점·제목)

ROOT = Path(__file__).resolve().parent  # 이 폴더(protected_tests). 묶음 파일 경로는 여기서부터 센다
SUITE_FILE = ROOT / 'suite.json'  # 묶음 정의 파일(검사 목록·범위·파일 목록)
SCOPES = ('in-loop', 'final')  # 쓸 수 있는 범위 이름 두 가지
_SELECTOR = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')           # dev qa_contract.py와 같은 규칙
_PATTERN = re.compile(r'[A-Za-z0-9_*?\[\].-]{1,128}\.py\Z')   # dev unit_runner.py와 같은 규칙
_REQUIREMENT = re.compile(r'REQ-\d{3}\Z')  # 요구사항 이름 모양(예: REQ-001)


def _text(value, name):
    # 값이 빈칸이 아닌 글자인지 확인한다. 아니면 어떤 필드(name)인지 적어 ValueError로 멈춘다.
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be nonempty text')


def load_suite(path: Path = SUITE_FILE) -> dict:
    # suite.json을 읽고 모양을 확인한다. 이상하면 ValueError로 멈춘다.
    suite = json.loads(Path(path).read_text(encoding='utf-8'))
    # 1) 머리 부분: 형식 버전, 범위 두 개(in-loop·final), 이름·버전 같은 필수 글자 필드
    if suite.get('schemaVersion') != 1 or set(suite.get('scopes', {})) != set(SCOPES):
        raise ValueError('Unsupported protected suite')
    for name in ('suiteId', 'suiteVersion', 'scenarioKey', 'toolName', 'sourceDirectory'):
        _text(suite.get(name), name)
    # 2) 범위마다 선택자·패턴 모양과 파일 목록을 확인한다. 파일은 sourceDirectory 바로 아래의 .py여야 한다
    #    (dev가 보호 파일을 그 폴더에 넣어 실행하므로 하위 폴더·다른 위치는 받지 않는다).
    directory = suite['sourceDirectory']
    modules = {}  # {범위 이름: 그 범위의 테스트 모듈 이름들}. 검사의 testId가 어느 범위 파일에 있는지 확인할 때 쓴다
    for scope_name, scope in suite['scopes'].items():
        if not _SELECTOR.fullmatch(scope.get('selector', '')) or not _PATTERN.fullmatch(scope.get('pattern', '')):
            raise ValueError('Invalid scope selector or pattern')
        files = scope.get('files')
        if not files or len(set(files)) != len(files):
            raise ValueError('Scope files must be nonempty and unique')
        for file in files:
            if not file.startswith(directory + '/') or '/' in file[len(directory) + 1:] or not file.endswith('.py'):
                raise ValueError('Protected files must sit directly in the source directory')
            if not (ROOT / file).is_file():
                raise ValueError('Protected file is missing')
        modules[scope_name] = {Path(file).stem for file in files}
    if suite['scopes']['in-loop']['selector'] == suite['scopes']['final']['selector']:
        raise ValueError('Scopes need distinct selectors')
    # 3) 검사 목록: 검사마다 번호·이름·요구사항·관점·범위·방법(unittest/static)을 확인하고 겹침을 막는다.
    seen = {'key': set(), 'checkId': set(), 'testId': set()}  # 이미 나온 값(검사 이름·검사 번호·테스트 ID)
    for check in suite.get('checks', []):
        _uuid4(check.get('checkId'))
        for name in ('key', 'title', 'expectedResult'):
            _text(check.get(name), name)
        if not _REQUIREMENT.fullmatch(check.get('requirementKey', '')):
            raise ValueError('Invalid requirement key')
        if check.get('perspective') not in ('QA', 'SECURITY'):
            raise ValueError('Unsupported perspective')
        scopes = check.get('scopes')
        if not scopes or not set(scopes) <= set(SCOPES) or len(set(scopes)) != len(scopes):
            raise ValueError('Invalid check scopes')
        if check.get('method') == 'unittest':
            _text(check.get('testId'), 'testId')
            module = check['testId'].split('.', 1)[0]  # "protected_signup_qa.클래스.메서드"의 첫 부분 = 모듈 이름
            if any(module not in modules[scope] for scope in scopes):
                raise ValueError('Check test module is not part of its scope')
        elif check.get('method') == 'static':
            if check.get('testId') is not None or scopes != ['final']:
                raise ValueError('Static checks belong to the final scope without a unittest ID')
        else:
            raise ValueError('Unsupported check method')
        # 순환 안에서는 QA가 실행 테스트로만 확인한다(Security·정적 검사는 최종 평가에서만).
        if 'in-loop' in scopes and (check['perspective'] != 'QA' or check['method'] != 'unittest'):
            raise ValueError('Only QA unittest checks can run inside the development loop')
        for name in seen:
            value = check.get(name)
            if value is not None and value in seen[name]:
                raise ValueError(f'Duplicate check {name}')
            seen[name].add(value)
    if not seen['key']:
        raise ValueError('Protected suite has no checks')
    return suite


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
    suite, entry = _scope(scope, suite)
    return {'name': entry['selector'], 'kind': 'PROTECTED', 'pattern': entry['pattern'],
            'source_directory': suite['sourceDirectory'], 'protected_files': protected_files(scope, suite),
            'protected_suite_ref': suite_ref}


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


def owned_qa_cases(scenario_contract: Mapping, suite: Optional[dict] = None) -> dict:
    # dev configs/owned-qa-cases.local.json 내용(순환 안 PROTECTED 케이스). 보고서의 모든 테스트가 여기 묶여야 한다.
    suite, entry = _scope('in-loop', suite)
    ids = _requirement_ids(scenario_contract, suite)
    return {'schemaVersion': 1, 'cases': [
        {'toolName': suite['toolName'], 'selector': entry['selector'], 'testId': check['testId'],
         'requirementId': ids[check['requirementKey']], 'title': check['title'],
         'expectedResult': check['expectedResult']}
        for check in suite['checks'] if 'in-loop' in check['scopes']]}


def evaluation_inputs(scenario_contract: Mapping, suite: Optional[dict] = None) -> dict:
    # 최종 독립 평가(build_evaluation_report)에 넣을 계획·꼬리표와 결과 대응표를 만든다.
    suite = suite or load_suite()
    ids = _requirement_ids(scenario_contract, suite)
    checks = [check for check in suite['checks'] if 'final' in check['scopes']]
    return {
        'suite_id': suite['suiteId'], 'suite_version': suite['suiteVersion'],
        'suite_sha256': suite_sha256('final', suite),
        'plan': tuple(CheckPlan(test_id=check['checkId']) for check in checks),
        'bindings': {check['checkId']: EvaluationBinding(ids[check['requirementKey']], check['perspective'],
                                                         check['key'] + ' ' + check['title'])
                     for check in checks},
        'required_requirement_ids': tuple(dict.fromkeys(ids[check['requirementKey']] for check in checks)),
        'unit_cases': {check['testId']: check['checkId'] for check in checks if check['method'] == 'unittest'},
        'static_cases': {check['key']: check['checkId'] for check in checks if check['method'] == 'static'},
        'expected': {check['checkId']: check['expectedResult'] for check in checks},
    }


def final_check_results(inputs: dict, *, unit_report: Optional[dict], unit_evidence_id: str,
                        assessment_requirements: Optional[Mapping] = None,
                        static_evidence_id: Optional[str] = None,
                        runner_error: str = 'TEST_RUNNER_ERROR') -> tuple:
    # 실행 테스트 보고서와 정적 판정 결과를 합쳐 최종 평가의 검사 결과 목록을 만든다.
    # 정적 판정이 없으면 정적 검사의 결과를 만들지 않아 집계에서 미검증(RESULT_MISSING)이 된다.
    results = check_results_from_unit_report(unit_report, inputs['unit_cases'], evidence_id=unit_evidence_id,
                                             expected=inputs['expected'], runner_error=runner_error)
    if assessment_requirements is not None:
        if static_evidence_id is None:
            raise ValueError('Static results need their evidence ID')
        results += check_results_from_assessment(assessment_requirements, inputs['static_cases'],
                                                 evidence_id=static_evidence_id, expected=inputs['expected'])
    return results
