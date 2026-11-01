"""D2 회원가입 보호 테스트 묶음(src/evaluation/protected_tests)과 결과 어댑터(check_adapters.py)를 시험한다.

확인하는 것:
  - 묶음 대응표(suite.json)가 실제 테스트 메서드·기준표 검사 ID와 정확히 맞는가? QA와 Security가 같은 방식(실행
    테스트)이고, 검사마다 시험지 세 벌(순환용·시범 최종용·공식 최종용)이 있는가?
  - dev가 역할별 보호 파일 설정(UnitTestScope)과 보호 QA 케이스(QACaseBinding)를 받아 주는가? 보내는 사본은 주석만 뺀
    같은 코드이고, 지문이 요구사항 연결까지 담는가? 시험지 세 벌이 같은 입력 종류를 쓰는가? 지원하지 않는 정책이면 멈추는가?
  - unittest 보고서가 검사 결과로 옮겨질 때 근거 없는 PASS가 생기지 않는가? 독립 평가 보고서까지 이어지는가?
  - (실행) 최소 회원가입 서비스와 변형 47개(FAIL을 기대하는 결함 36·모두 미검증 3·정상 경계 8)를 컨테이너처럼 읽기 전용 Snapshot으로 두고 dev unittest 실행기로 돌렸을 때,
    시험지 세 벌 모두에서 결함마다 같은 검사만 FAIL이 되고(시험지 난이도가 같음), 연결이 안 되면 모두 UNVERIFIED인가?
실행 시험은 sqlalchemy·argon2-cffi가 있어야 돈다(프로젝트 dev 그룹에 있음. 없는 환경에서는 건너뜀).
"""
import ast
import importlib.util
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID

import pytest

from evaluation import protected_tests as pt
from evaluation.aggregation import Status
from evaluation.check_adapters import check_results_from_assessment, check_results_from_unit_report
from evaluation.independent import build_evaluation_report
from evaluation.manifest import ExecutionManifest
from orchestrator.domain.scenario_registry import SCN_001_ID, RequirementValidator, get_scenario

from tests.evaluation import signup_service

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = get_scenario(SCN_001_ID).planner_contract()
SUITE = pt.load_suite()
CHECKS = {check['key']: check for check in SUITE['checks']}


def uid(number):
    """시험용 가짜 UUIDv4. 같은 숫자면 같은 값이다."""
    return str(UUID(int=number, version=4))


def defined_tests(scope):
    """보호 파일을 읽기만 해서(import 없이) unittest 테스트 ID 목록을 만든다."""
    ids = set()
    for path, content in pt.protected_files(scope).items():
        module = Path(path).stem
        for node in ast.parse(content).body:
            if isinstance(node, ast.ClassDef):
                ids |= {f'{module}.{node.name}.{item.name}' for item in node.body
                        if isinstance(item, ast.FunctionDef) and item.name.startswith('test')}
    return ids


def matches(pattern, name):
    """파일 이름이 범위의 패턴(예: practice_*.py)에 맞는지 본다. dev 실행기와 같은 fnmatch 규칙이다."""
    import fnmatch
    return fnmatch.fnmatch(name, pattern)


def scope_checks(scope):
    """그 범위가 실행하는 검사들(순환 안 범위는 자기 역할의 검사만)"""
    role = SUITE['scopes'][scope].get('role')
    return [check for check in SUITE['checks'] if role in (None, check['perspective'])]


# ---------------------------------------------------------------- 대응표


def test_suite_lists_the_execution_checks_of_the_criteria_table():
    """묶음의 검사는 기준표(scn001_criteria.md)의 QA 6개·Security 3개이고 모두 실행 검사다. 요구사항 7개
    (REQ-001~007)를 빠짐없이 덮고, 관점별 요구사항은 dev 시나리오의 담당 역할과 맞는다."""
    assert set(CHECKS) == {f'QA-SIGNUP-00{n}' for n in range(1, 7)} | {'SEC-SIGNUP-002', 'SEC-SIGNUP-003', 'SEC-SIGNUP-005'}
    assert {check['requirementKey'] for check in SUITE['checks']} == {f'REQ-00{n}' for n in range(1, 8)}
    scenario = get_scenario(SCN_001_ID)
    keys = {str(item['requirementId']): item['key'] for item in CONTRACT['requirements']}
    for role, validator in (('QA', RequirementValidator.QA), ('SECURITY', RequirementValidator.SECURITY)):
        owned = {keys[str(value)] for value in scenario.requirement_ids_for(validator)}
        assert {check['requirementKey'] for check in SUITE['checks'] if check['perspective'] == role} == owned


@pytest.mark.parametrize('scope', pt.SCOPES)
def test_every_protected_test_is_bound_and_every_bound_test_exists(scope):
    """dev는 대응표에 없는 테스트가 보고서에 있으면 보고서 전체를 거부한다. 범위마다 빠짐없이 1:1이어야 한다."""
    entry = SUITE['scopes'][scope]
    discovered = {test for test in defined_tests(scope) if matches(entry['pattern'], test.split('.', 1)[0] + '.py')}
    assert discovered == {pt.unit_test_id(check, entry['set'], SUITE) for check in scope_checks(scope)}


def test_every_set_has_the_same_checks_with_different_inputs():
    """시험지 세 벌은 같은 검사(같은 메서드)를 갖되 입력은 서로 다르다. 순환용 시험지의 입력 글자가 최종 시험지에
    그대로 들어 있지 않다(같은 공통 도우미 호출 제외)."""
    sources = {name: (pt.ROOT / 'tests/signup' / (name + '.py')).read_text(encoding='utf-8')
               for name in ('practice_qa', 'practice_security', 'exam_pilot', 'exam_final')}
    for exam in pt.SETS:
        assert {pt.unit_test_id(check, exam, SUITE).rsplit('.', 1)[1] for check in SUITE['checks']} == \
            {check['method'] for check in SUITE['checks']}

    def literals(*names):
        found = set()
        for name in names:
            for node in ast.walk(ast.parse(sources[name])):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) > 3:
                    found.add(node.value)
        return found
    shared = {'invalid', 'template', 'emails', 'forms', 'steps', 'duplicate', 'malformed', 'db_error', 'length',
              'reject', 'accept', 'workers', 'attempts', 'accounts', 'short', 'kinds', 'lower', 'digits', 'aliases',
              # 입력 종류 이름(값이 아님. 실제 값은 실행할 때 무작위로 만든다): 형식이 틀린 본문 종류, 이메일 변형 종류
              'missing_email', 'list', 'misspelled', 'upper', 'title', 'lead', 'trail', 'swap_lead', 'title_trail',
              'upper_pad', 'swapcase', 'swap_pad'}
    practice = literals('practice_qa', 'practice_security') - shared
    for exam in ('exam_pilot', 'exam_final'):
        assert not (practice & (literals(exam) - shared) - {'same'}), exam
    assert not (literals('exam_pilot') & literals('exam_final')) - shared - {'same'}



def test_delivered_copies_drop_only_comments():
    """실행하러 보내는 보호 파일 사본은 주석만 뺀 것이다(AST가 저장소 파일과 같음). 문자열 안의 #은 남는다.
    시험지 노출 확인은 주석 있는 원본과 주석 없는 사본을 모두 찾는다."""
    for scope in pt.SCOPES:
        for path, content in pt.protected_files(scope).items():
            original = (pt.ROOT / path).read_text(encoding='utf-8')
            assert ast.dump(ast.parse(content)) == ast.dump(ast.parse(original)), path
            assert '\n#' not in '\n' + content, path
    assert pt.without_comments('x = "a # b"  # note\n# only\ny = 1\n') == 'x = "a # b"\ny = 1\n'
    for path in ('tests/signup/exam_final.py', 'tests/signup/signup_link.py'):
        original = (pt.ROOT / path).read_text(encoding='utf-8')
        for content in (original, pt.without_comments(original)):
            assert [item['reason'] for item in pt.exam_exposure({'tests/copy.py': content.encode('utf-8')})] == \
                ['SAME_AS_PROTECTED_FILE']


def test_suite_fingerprint_covers_the_requirement_mapping():
    """파일이 같아도 검사의 요구사항 연결(점수 계산에 쓰임)이 바뀌면 지문이 달라져 다른 시험지로 본다(Codex 10.30).
    범위·시험지마다 지문이 다르다."""
    import copy
    changed = copy.deepcopy(SUITE)
    next(check for check in changed['checks'] if check['key'] == 'QA-SIGNUP-003')['requirementKey'] = 'REQ-001'
    for scope in ('final', 'pilot', 'practice', 'in-loop-qa'):
        assert pt.suite_sha256(scope, changed) != pt.suite_sha256(scope, SUITE), scope
    assert pt.suite_sha256('in-loop-security', changed) == pt.suite_sha256('in-loop-security', SUITE)  # QA 검사는 안 담음
    assert len({pt.suite_sha256(scope) for scope in pt.SCOPES}) == len(pt.SCOPES)



def _set_inputs(names):
    """시험지 파일들(names)을 실행하지 않고 읽어 {검사 메서드: 입력 사전}을 만든다(T는 그 파일의 기본 이메일로 바꿈)."""
    found = {}
    for name in names:
        tree = ast.parse((pt.ROOT / 'tests/signup' / (name + '.py')).read_text(encoding='utf-8'))
        template = next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                        and getattr(node.targets[0], 'id', None) == 'T')

        class Resolve(ast.NodeTransformer):
            # 입력 사전 안의 이름 T를 그 파일의 기본 이메일 글자로 바꾼다(실행하지 않고 literal_eval로 읽으려고).
            def visit_Name(self, node):
                return ast.copy_location(ast.Constant(template), node) if node.id == 'T' else node
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name.startswith('test_'):
                call = node.body[0].value
                found[node.name] = ast.literal_eval(Resolve().visit(call.args[1]))
    return found


def _email_kind(template):
    """이메일 모양의 종류: (로컬에 점, 하위 도메인, 2글자 최상위 도메인, +태그, Gmail)"""
    local, domain = template.format(h='0a1b2c').rsplit('@', 1)
    return ('.' in local, domain.count('.') >= 2, len(domain.rsplit('.', 1)[-1]) == 2, '+' in local, domain == 'gmail.com')


def _invalid_kind(value):
    """형식 오류 이메일의 종류"""
    text = value.strip()
    return ('empty' if not value else 'blank' if not text else 'space' if ' ' in text else 'no_at' if '@' not in text
            else 'many_at' if text.count('@') > 1 else 'no_local' if text.startswith('@') else 'no_domain'
            if text.endswith('@') else 'other')


def _input_kinds(data, form_kind):
    """검사 입력 하나를 값이 아닌 종류로 바꾼다. 세 시험지에서 같아야 난이도가 같다. 변형 종류는 그 검사의 기본 이메일로
    판단한다(같은 변형도 이메일 모양에 따라 대소문자가 안 바뀔 수 있음)."""
    kinds, template = {}, data.get('template', 'ab.cd{h}@ex.example.com')
    form_kind = (lambda form, base=template, kind=form_kind: kind(form, base))
    for key, value in data.items():
        if key == 'template':
            kinds[key] = _email_kind(value)
        elif key == 'emails':
            kinds[key] = frozenset(_email_kind(item) for item in value)
        elif key == 'forms':
            marks = [form_kind(form) for form in value]
            kinds[key] = ('same' in value, *(any(mark[index] for mark in marks) for index in range(4)))
        elif key == 'duplicate':
            kinds[key] = form_kind(value)
        elif key == 'invalid':
            kinds[key] = _invalid_kind(value) if isinstance(value, str) else frozenset(map(_invalid_kind, value))
        elif key in ('reject', 'short'):
            values = value if isinstance(value, tuple) else (value,)
            kinds[key] = (7 in values or key == 'short', all(item <= 7 for item in values))
        elif key in ('accept', 'length'):
            values = value if isinstance(value, tuple) else (value,)
            kinds[key] = (all(item >= 8 for item in values), 8 in values if key == 'accept' else None)
        elif key == 'kinds':
            kinds[key] = frozenset(kind for kind, length in value if length >= 8)
        elif key == 'aliases':
            kinds[key] = frozenset('plus' if '+' in first else 'dot' for first, _second in value)
        elif key == 'malformed':
            kinds[key] = frozenset(value)
        elif key == 'steps':
            names = {}
            for step in value:
                names.setdefault(step[1], []).append(step[0]) if step[0] != 'invalid' else None
            kinds[key] = (tuple(sorted(step[0] for step in value)),
                          any(seen[:2] == ['short', 'new'] for seen in names.values()),
                          frozenset(_invalid_kind(step[1]) for step in value if step[0] == 'invalid'),
                          frozenset(form_kind(step[2]) for step in value if step[0] == 'duplicate'))
        else:
            kinds[key] = value
    return kinds


def test_every_set_uses_the_same_input_kinds():
    """시험지 세 벌은 값은 다르지만 입력 종류가 같아야 난이도가 같다(2026-10-30 정책 칸 대응 점검). 검사마다 이메일 모양,
    변형의 대소문자·앞 공백·뒤 공백, 형식 오류 이메일 종류, 비밀번호 길이 경계·구성, 요청 순서 종류, 형식이 틀린 본문
    종류를 뽑아 세 벌이 같은지 본다. 예전에는 시범은 앞 공백만·공식은 뒤 공백만, 2글자 최상위 도메인은 공식에만 있었다."""
    spec = importlib.util.spec_from_file_location('signup_link_kinds', pt.ROOT / 'tests/signup/signup_link.py')
    link = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(link)

    def form_kind(form, template):
        # (앞 공백, 뒤 공백, 로컬 대소문자 바뀜, 도메인 대소문자 바뀜). 무작위 값이 숫자로 시작하는 최악의 경우로 본다.
        email = template.format(h='0a1b2c')
        sample = link.variant(email, form).strip()
        local, domain = sample.rsplit('@', 1)
        original_local, original_domain = email.rsplit('@', 1)
        return (link.variant(email, form)[:1].isspace(), link.variant(email, form)[-1:].isspace(),
                local != original_local, domain != original_domain)

    sets = {'practice': _set_inputs(('practice_qa', 'practice_security')), 'pilot': _set_inputs(('exam_pilot',)),
            'final': _set_inputs(('exam_final',))}
    assert len({frozenset(inputs) for inputs in sets.values()}) == 1
    for method in sets['final']:
        kinds = {name: _input_kinds(inputs[method], form_kind) for name, inputs in sets.items()}
        assert kinds['practice'] == kinds['pilot'] == kinds['final'], (method, kinds)

def test_suite_refuses_policies_its_checks_do_not_implement():
    """검사 내용이 전제로 하는 정책(SUPPORTED_POLICY)은 실행 검사의 상수(Argon2 최소값)·dev 등록 정책과 같다. 받은 정책이
    다르면(Argon2 메모리 기준을 올림, +태그 제거를 허용) 보호 케이스·평가 입력을 만들지 않는다(Codex 10.30 3차 5)."""
    import copy
    source = (pt.ROOT / 'tests/signup/signup_security_checks.py').read_text(encoding='utf-8')
    constant = next(node.value for node in ast.parse(source).body if isinstance(node, ast.Assign)
                    and getattr(node.targets[0], 'id', None) == 'ARGON2_MINIMUM')
    minimum = ast.literal_eval(constant)
    supported = pt.SUPPORTED_POLICY['passwordHashPolicy']
    assert minimum == {'m': supported['memoryKiB'], 't': supported['iterations'], 'p': supported['parallelism']}
    pt.check_policy(CONTRACT)
    stronger = copy.deepcopy(CONTRACT)
    stronger['securityPolicy']['passwordHashPolicy']['memoryKiB'] = 65536
    plus = copy.deepcopy(CONTRACT)
    plus['emailPolicy']['plusTagRemoval'] = True
    for contract in (stronger, plus):
        with pytest.raises(ValueError):
            pt.evaluation_inputs(contract, 'final')
        with pytest.raises(ValueError):
            pt.owned_cases(contract, 'SECURITY')


@pytest.mark.skipif(sys.version_info < (3, 12), reason='필터가 기록 사본을 돌려주는 방식은 Python 3.12부터')
def test_log_capture_uses_the_record_a_filter_returns():
    """handler 필터가 가린 사본을 돌려주면(3.12부터) 실제로 나가는 것은 사본이므로, 모은 글에도 원문이 없다.
    가리지 않은 로거의 원문은 그대로 모인다(실제 출력 기준 수집, Codex 10.30 3차 2의 연장)."""
    import logging
    import os
    spec = importlib.util.spec_from_file_location('signup_link_probe', pt.ROOT / 'tests/signup/signup_link.py')
    link = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(link)
    secret = 'Probe' + 'Value' + '4821'

    class MaskCopy(logging.Filter):
        def filter(self, record):
            copy = logging.makeLogRecord(record.__dict__)
            copy.msg, copy.args = 'credential [REDACTED]', ()
            return copy

    loggers = []
    for name, mask in (('probe.masked.copy', True), ('probe.plain', False)):
        logger, handler = logging.getLogger(name), logging.FileHandler(os.devnull)
        logger.propagate = False
        if mask:
            handler.addFilter(MaskCopy())
        logger.addHandler(handler)
        loggers.append((logger, handler))
    service = link.Service()
    try:
        service._capture()
        for logger, _handler in loggers:
            logger.warning('credential %s', secret)
    finally:
        service.close()
        for logger, handler in loggers:
            logger.removeHandler(handler)
            handler.close()
    text = service.log_text()
    assert 'credential [REDACTED]' in text and text.count(secret) == 1

def test_in_loop_scopes_hold_only_their_role_requirements():
    """순환 안 보호 케이스는 dev가 그 역할 담당으로 정한 요구사항만 쓰고, 그 역할의 순환용 선택자에 묶인다."""
    scenario = get_scenario(SCN_001_ID)
    for role, validator in (('QA', RequirementValidator.QA), ('SECURITY', RequirementValidator.SECURITY)):
        cases = pt.owned_cases(CONTRACT, role)['cases']
        assert {case['requirementId'] for case in cases} == {str(value) for value in scenario.requirement_ids_for(validator)}
        selector = SUITE['scopes'][pt.LOOP_SCOPES[role]]['selector']
        assert all(case['selector'] == selector and case['toolName'] == 'run_unit_tests' for case in cases)
    assert pt.owned_qa_cases(CONTRACT) == pt.owned_cases(CONTRACT, 'QA')


def _dev_size(config):
    """dev와 같은 방식(한글 그대로·공백 없는 JSON)으로 잰 설정 크기(바이트)"""
    return len(json.dumps(config, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def test_dev_accepts_protected_files_and_cases():
    """dev 코드가 우리 설정을 그대로 받아 준다: 역할별 보호 파일 설정(UnitTestScope)과 보호 QA 케이스(QACaseBinding).
    dev는 Unit 설정 전체를 64KiB 이하로 받으므로(unit_config.py) 순환 안 두 범위를 합쳐도 여유가 있어야 한다."""
    from agents.roles.qa_contract import QACaseBinding
    from mcp_tools.tools.unit_config import UnitTestScope
    configs = {scope: pt.unit_test_scope(scope, suite_ref='artifact://protected-suite/scn-001/signup') for scope in pt.SCOPES}
    for scope, config in configs.items():
        UnitTestScope(**config)
        assert _dev_size(config) < 64 * 1024
    assert configs['in-loop-security']['role'] == 'SECURITY' and 'role' not in configs['in-loop-qa']
    assert _dev_size(configs['in-loop-qa']) + _dev_size(configs['in-loop-security']) < 56 * 1024
    for role in ('QA', 'SECURITY'):
        for case in pt.owned_cases(CONTRACT, role)['cases']:
            QACaseBinding(tool_name=case['toolName'], selector=case['selector'], test_id=case['testId'],
                          requirement_id=UUID(case['requirementId']), title=case['title'],
                          expected_result=case['expectedResult'])


@pytest.mark.parametrize('scope', pt.SCOPES)
def test_protected_files_parse_as_python_310(scope):
    """팀이 선언한 최소 버전(3.10) 문법으로 읽혀야 한다."""
    for content in pt.protected_files(scope).values():
        ast.parse(content, feature_version=(3, 10))


def test_suite_fingerprint_follows_file_bytes(monkeypatch):
    """지문(suite SHA-256)은 같은 내용이면 같고, 시험지가 다르거나 파일에 한 글자(줄바꿈)만 더해져도 달라진다."""
    first = pt.suite_sha256('final')
    assert first == pt.suite_sha256('final') and first != pt.suite_sha256('pilot') != pt.suite_sha256('practice')
    original = pt.protected_files
    monkeypatch.setattr(pt, 'protected_files', lambda scope, suite=None: {
        path: content + ('\n' if path.endswith('signup_link.py') else '') for path, content in original(scope).items()})
    assert pt.suite_sha256('final') != first


@pytest.mark.parametrize('mutate, message', [
    (lambda s: s['checks'].append(dict(s['checks'][0])), 'Duplicate'),
    (lambda s: s['scopes']['final'].update(role='QA'), 'Only in-loop'),
    (lambda s: s['scopes']['in-loop-qa'].update(set='final'), 'Only in-loop'),
    (lambda s: s['scopes']['in-loop-qa']['files'].remove('tests/signup/practice_qa.py'), 'Check test is not part of its scope'),
    (lambda s: s['checks'][6].update(perspective='QA'), 'Check test is not part of its scope'),
    (lambda s: s['scopes']['final'].update(selector='pilot-signup'), 'distinct'),
    (lambda s: s['scopes']['final']['files'].append('tests/signup/../secret.py'), 'directly'),
    (lambda s: s['sets']['final'].pop('SECURITY'), 'every role'),
])
def test_broken_suite_is_rejected(tmp_path, mutate, message):
    """잘못된 suite.json(검사 중복, 순환 밖 범위에 역할, 순환 안 범위가 순환용이 아닌 시험지, 범위 밖 테스트 모듈,
    순환 안 범위에 다른 역할의 검사, 같은 선택자, 폴더 밖 파일, 역할이 빠진 시험지)은 읽을 때 ValueError로 거부된다."""
    suite = json.loads(pt.SUITE_FILE.read_text(encoding='utf-8'))
    mutate(suite)
    path = tmp_path / 'suite.json'
    path.write_text(json.dumps(suite), encoding='utf-8')
    with pytest.raises(ValueError, match=message):
        pt.load_suite(path)

# ---------------------------------------------------------------- 결과 어댑터

CASES = {'m.C.test_a': uid(1), 'm.C.test_b': uid(2), 'm.C.test_c': uid(3)}


def report(*rows):
    """unittest-v1 보고서를 흉내 낸다. 줄은 (testId, outcome) 또는 (testId, outcome, details)이고 건수는 자동으로 맞춘다."""
    tests = [dict(zip(('testId', 'outcome', 'details'), row)) if len(row) == 3
             else {'testId': row[0], 'outcome': row[1]} for row in rows]
    count = {outcome: sum(t['outcome'] == outcome for t in tests) for outcome in ('PASS', 'FAIL', 'SKIP')}
    return {'format': 'unittest-v1', 'total': len(tests), 'passed': count['PASS'],
            'failed': count['FAIL'], 'skipped': count['SKIP'], 'tests': tests}


def by_id(results):
    """검사 결과 목록을 {검사 번호: 결과} 사전으로 바꾼다."""
    return {result.test_id: result for result in results}


def test_unit_report_outcomes_map_to_check_statuses():
    """PASS → PASS(증거 번호 붙음), FAIL → FAIL(실패 설명 유지),
    SKIP "UNVERIFIED:코드" → ERROR(그 코드, 증거 번호 없음)로 옮겨진다."""
    results = by_id(check_results_from_unit_report(
        report(('m.C.test_a', 'PASS'), ('m.C.test_b', 'FAIL', 'AssertionError: 7자 거부 안 됨'),
               ('m.C.test_c', 'SKIP', 'UNVERIFIED:APP_IMPORT_FAILED')),
        CASES, evidence_id=uid(9)))
    assert results[uid(1)].status == Status.PASS and results[uid(1)].evidence_ids == (uid(9),)
    assert results[uid(2)].status == Status.FAIL and '7자' in results[uid(2)].actual_result
    assert results[uid(3)].status == Status.ERROR and results[uid(3)].error_code == 'APP_IMPORT_FAILED'
    assert results[uid(3)].evidence_ids == ()


def test_plain_skip_and_missing_case_are_unverified():
    """사유 없는 SKIP은 RUNNER_REPORTED_SKIP, 보고서에 없는 계획 검사는 PLANNED_CASE_NOT_REPORTED로 미검증이다."""
    results = by_id(check_results_from_unit_report(
        report(('m.C.test_a', 'SKIP', 'not supported'), ('m.C.test_b', 'PASS')), CASES, evidence_id=uid(9)))
    assert results[uid(1)].error_code == 'RUNNER_REPORTED_SKIP'
    assert results[uid(3)].status == Status.ERROR and results[uid(3)].error_code == 'PLANNED_CASE_NOT_REPORTED'


@pytest.mark.parametrize('bad, code', [
    (None, 'TEST_RUNNER_ERROR'),
    ({'error': 'TEST_RUNNER_ERROR'}, 'UNIT_REPORT_INVALID'),
    (dict(report(('m.C.test_a', 'PASS')), passed=0), 'UNIT_REPORT_INVALID'),
    (report(('m.C.test_a', 'PASS'), ('m.C.test_a', 'PASS')), 'UNIT_REPORT_INVALID'),
    (report(('m.C.test_a', 'PASS'), ('setUpClass (m.C)', 'FAIL', 'boom')), 'UNIT_REPORT_UNBOUND_CASE'),
])
def test_untrusted_report_never_yields_pass(bad, code):
    """보고서가 없거나 형식이 틀리거나 대응표 밖의 줄이 섞이면, 통과한 줄이 있어도 모두 미검증이다."""
    results = check_results_from_unit_report(bad, CASES, evidence_id=uid(9))
    assert {result.status for result in results} == {Status.ERROR}
    assert {result.error_code for result in results} == {code}


def test_assessment_outcomes_map_to_check_statuses():
    """정적 판정의 PASS·FAIL·UNVERIFIED가 검사 상태로 옮겨지고, 모르는 outcome은 ASSESSMENT_INVALID 미검증이다."""
    requirements = {
        'REQ-003': {'checkId': 'STATIC-REQ-003', 'outcome': 'FAIL', 'basis': 'VIOLATION_FOUND'},
        'REQ-005': {'checkId': 'STATIC-REQ-005', 'outcome': 'PASS', 'basis': 'APPROVED_HASH_STORAGE'},
        'REQ-006': {'checkId': 'STATIC-REQ-006', 'outcome': 'UNVERIFIED', 'basis': 'ANALYSIS_TOO_COMPLEX'},
    }
    cases = {'STATIC-REQ-005': uid(1), 'STATIC-REQ-003': uid(2), 'STATIC-REQ-006': uid(3)}
    results = by_id(check_results_from_assessment(requirements, cases, evidence_id=uid(9)))
    assert results[uid(1)].status == Status.PASS and results[uid(1)].evidence_ids == (uid(9),)
    assert results[uid(2)].status == Status.FAIL
    assert results[uid(3)].status == Status.ERROR and results[uid(3)].error_code == 'ANALYSIS_TOO_COMPLEX'
    weird = {'REQ-005': {'checkId': 'STATIC-REQ-005', 'outcome': 'MAYBE', 'basis': 'X'}}
    assert check_results_from_assessment(weird, cases, evidence_id=uid(9))[0].error_code == 'ASSESSMENT_INVALID'



def evaluate(unit_outcome='PASS', exam='final'):
    """시험지(exam) 하나의 보호 테스트 결과(모두 unit_outcome)로 독립 평가 보고서를 만든다.
    Manifest는 기대값과 관측값을 같게 둔다."""
    inputs = pt.evaluation_inputs(CONTRACT, exam)
    unit = report(*[(test_id, unit_outcome) + (('UNVERIFIED:APP_IMPORT_FAILED',) if unit_outcome == 'SKIP' else ())
                    for test_id in inputs['unit_cases']])
    results = pt.final_check_results(inputs, unit_report=unit, unit_evidence_id=uid(50))
    manifest = ExecutionManifest('repo', 2, uid(1), 'sha1', 'a' * 40, 'b' * 40, 'c' * 64,
                                 'sha256:' + 'd' * 64, 'sha256:' + 'e' * 64)
    return build_evaluation_report(
        evaluation_id=uid(60), source_run_id=uid(61), architecture='MULTI_AGENT',
        suite_id=inputs['suite_id'], suite_version=inputs['suite_version'], suite_sha256=inputs['suite_sha256'],
        created_at='2026-10-28T12:00:00+09:00', required_requirement_ids=inputs['required_requirement_ids'],
        expected=manifest, observed=manifest, plan=inputs['plan'], results=results, bindings=inputs['bindings'])


@pytest.mark.parametrize('exam', pt.SETS)
def test_final_inputs_build_an_independent_report(exam):
    """시험지마다 검사 9개·요구사항 7개 보고서가 만들어지고, 하나라도 FAIL이면 FAIL, 미검증(SKIP)이 있으면 UNVERIFIED다.
    보고서의 suite 지문은 그 시험지의 것이다."""
    passed = evaluate(exam=exam)
    assert passed['evaluationVerdict'] == 'PASS' and len(passed['checks']) == 9
    assert len(passed['requirements']) == 7  # REQ-008은 Orchestrator 검증이라 제외
    assert passed['suite']['sha256'] == pt.suite_sha256(exam)
    assert evaluate(unit_outcome='FAIL', exam=exam)['evaluationVerdict'] == 'FAIL'
    assert evaluate(unit_outcome='SKIP', exam=exam)['evaluationVerdict'] == 'UNVERIFIED'
    with pytest.raises(ValueError):
        pt.evaluation_inputs(CONTRACT, 'in-loop-qa')


# ---------------------------------------------------------------- 실행(최소 서비스와 결함 변형)

RUNTIME = all(importlib.util.find_spec(name) for name in ('fastapi', 'httpx', 'sqlalchemy', 'argon2'))
RUNNER = ROOT / 'src/mcp_tools/tools/unit_runner.py'
_RUN = '''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("trusted_unit_runner", sys.argv[1])
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
report, code = runner.run_tests(sys.argv[2], sys.argv[3], sys.argv[4])
sys.__stdout__.write(json.dumps({"report": report, "exit": code}))
'''

# 변형마다 FAIL이 되어야 하는 검사. 나머지 검사는 모두 PASS여야 한다(None이면 모두 미검증). 시험지 세 벌 모두 같다.
EXPECTED_FAILURES = {
    'good': set(),
    'plaintext_storage': {'SEC-SIGNUP-002'},
    'weak_argon2_memory': {'SEC-SIGNUP-002'},
    'fixed_salt': {'SEC-SIGNUP-002'},
    'seven_char_password': {'QA-SIGNUP-004', 'QA-SIGNUP-006'},
    'duplicates_allowed': {'QA-SIGNUP-003', 'QA-SIGNUP-005', 'QA-SIGNUP-006', 'SEC-SIGNUP-003'},
    'case_sensitive_email': {'QA-SIGNUP-003', 'QA-SIGNUP-006', 'SEC-SIGNUP-003'},
    # 공백 이메일을 정리하지 않고 거부하면 중복 계정은 생기지 않지만 정규화 저장(SEC-SIGNUP-003)이 FAIL이다.
    'padded_email_rejected': {'SEC-SIGNUP-003'},
    'invalid_email_accepted': {'QA-SIGNUP-002', 'QA-SIGNUP-006'},
    'validation_input_echoed': {'SEC-SIGNUP-005'},
    'password_logged': {'SEC-SIGNUP-005'},
    # info·debug 수준 로그의 비밀번호도 노출이다. 비밀번호가 없는 debug 로그는 노출이 아니다.
    'password_logged_info': {'SEC-SIGNUP-005'},
    'password_logged_debug': {'SEC-SIGNUP-005'},
    'debug_log_without_secret': set(),
    'hash_in_response': {'SEC-SIGNUP-005'},
    # 처리하지 못한 DB 오류: 중복(동시 요청 포함)이 500으로 응답되고, traceback의 SQL 파라미터로 해시가 로그에 남는다.
    'unhandled_duplicate': {'QA-SIGNUP-003', 'QA-SIGNUP-005', 'QA-SIGNUP-006', 'SEC-SIGNUP-005'},
    # 저장 실패(DB 오류)를 처리하지 않으면 traceback의 SQL 값으로 해시가 로그에 남는다(오류 주입으로 찾음).
    'db_error_unhandled': {'SEC-SIGNUP-005'},
    # 대문자·공백 이메일이 500: 중복은 생기지 않지만 4xx 거부가 아니고 정규화 저장도 못 한다(Codex F1 재현).
    'variant_email_server_error': {'QA-SIGNUP-002', 'QA-SIGNUP-003', 'QA-SIGNUP-006', 'SEC-SIGNUP-003'},
    'password_printed': {'SEC-SIGNUP-005'},
    'isolated_logger': {'SEC-SIGNUP-005'},
    # 모든 가입이 422가 되는 서비스: 정상 가입 실패가 FAIL이고, 거부 검사·전제 조건이 필요한 검사는 미검증이다.
    'different_request_field': {'QA-SIGNUP-001', 'QA-SIGNUP-004', 'QA-SIGNUP-006'},
    # 동시 요청이 모두 실패해 계정이 없어도, 응답을 받았고 단독 가입은 되므로 미검증이 아니라 FAIL이다.
    'concurrent_server_error': {'QA-SIGNUP-005'},
    'concurrent_rejected': {'QA-SIGNUP-005'},
    # 모든 가입이 500: 서버 오류를 관찰한 검사는 FAIL이고, 정상 가입이 전제인 검사는 미검증이다.
    'signup_server_error': {'QA-SIGNUP-001', 'QA-SIGNUP-004', 'QA-SIGNUP-005', 'QA-SIGNUP-006'},
    'app_missing': None,
    'wrong_signup_path': None,
    # 시작할 때 테이블을 만드는 앱: DB 위치를 DATABASE_URL로 받으면 정상, 소스 폴더 옆에 고정하면 읽기 전용
    # Snapshot에서 시작이 실패해 모두 미검증이다(제품 결함으로 FAIL을 내지도, 시작 단계를 건너뛰어 PASS를 내지도 않는다).
    'lifespan_env_database': set(),
    'lifespan_source_database': None,
    # Codex 10.30 재현: 시작할 때 만든 독립 로거·종료할 때 남긴 로그의 비밀번호도 노출이다(종료까지 모은 뒤 판정).
    'startup_isolated_logger': {'SEC-SIGNUP-005'},
    'shutdown_log_flush': {'SEC-SIGNUP-005'},
    'shutdown_log_without_secret': set(),
    # Codex 10.30 재현: lifespan 상태를 쓰는 정상 앱은 동시 요청에서도 정상, 종료할 때 엔진을 정리해도 다음 검사가 정상.
    'lifespan_request_state': set(),
    'lifespan_engine_dispose': set(),
    # Codex 10.30 추가: 중복 가입을 거부하면서 기존 계정을 바꾸면, 거부된 요청이 상태를 바꾼 것이라 FAIL이다.
    'duplicate_overwrites_password': {'QA-SIGNUP-003', 'QA-SIGNUP-006'},
    # Codex 10.30 추가 점검: DB 실패 요청에서 새로 만든 해시를 base64로 로그에 남기면 노출이다(정상 경로의 base64 로그는 아님).
    'db_error_new_hash_base64': {'SEC-SIGNUP-005'},
    'base64_log_without_secret': set(),
    # Codex 10.30 추가 점검: 8자 이상인데 소문자만·숫자만이라고 거부하면 REQ-004(8자 이상 정상 처리) 위반이다.
    'password_complexity_required': {'QA-SIGNUP-004'},
    # Codex 10.30 3차: 정책상 다른 주소(점·+태그만 다름)를 합치면 정규화 저장 위반이다. 가린 뒤 내보낸 로그는 노출이 아니다.
    'email_alias_merged': {'SEC-SIGNUP-003'},
    'masked_logger_filter': set(),
    'masked_handler_filter': set(),
    # 정책 칸·완료 조건 대응표(2026-10-30): 앞·뒤 공백 한쪽만 지우는 정리, 2글자 최상위 도메인 거부, Argon2 반복 미달·argon2i,
    # 중복 가입에 성공 응답. 시험지 세 벌 모두에서 같은 검사만 FAIL이어야 한다(입력 종류가 같다는 확인).
    'strip_leading_only': {'SEC-SIGNUP-003'},
    'strip_trailing_only': {'SEC-SIGNUP-003'},
    'email_tld_min3': {'QA-SIGNUP-001'},
    'weak_argon2_iterations': {'SEC-SIGNUP-002'},
    'argon2i_type': {'SEC-SIGNUP-002'},
    'duplicate_success_response': {'QA-SIGNUP-003', 'QA-SIGNUP-005', 'QA-SIGNUP-006'},
    'sha256_storage': {'SEC-SIGNUP-002'},
    'local_case_kept': {'QA-SIGNUP-003', 'QA-SIGNUP-006', 'SEC-SIGNUP-003'},
}
# 모두 미검증이 되는 변형의 근거 코드
UNVERIFIED_CODES = {'app_missing': {'APP_IMPORT_FAILED'}, 'wrong_signup_path': {'SIGNUP_ROUTE_NOT_FOUND'},
                    'lifespan_source_database': {'SERVICE_SETUP_FAILED'}}
# FAIL도 PASS도 아닌(미검증) 검사가 있는 변형
EXPECTED_UNVERIFIED = {'different_request_field': {'QA-SIGNUP-002', 'QA-SIGNUP-003', 'QA-SIGNUP-005',
                                                   'SEC-SIGNUP-002', 'SEC-SIGNUP-003', 'SEC-SIGNUP-005'},
                       'signup_server_error': {'QA-SIGNUP-002', 'QA-SIGNUP-003', 'SEC-SIGNUP-002', 'SEC-SIGNUP-003',
                                               'SEC-SIGNUP-005'}}


def _set_read_only(folder, read_only):
    """폴더 아래 파일·폴더의 쓰기 권한을 빼거나(read_only=True) 되돌린다. 컨테이너의 읽기 전용 /snapshot을 흉내 낸다."""
    for path in [folder, *folder.rglob('*')]:
        path.chmod((0o555 if path.is_dir() else 0o444) if read_only else (0o755 if path.is_dir() else 0o644))


def _read_only_enforced():
    """이 컴퓨터에서 쓰기 권한을 뺀 폴더에 정말 쓸 수 없는지 확인한다(관리자 계정 등은 권한과 상관없이 쓸 수 있다)."""
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory) / 'locked'
        folder.mkdir()
        _set_read_only(folder, True)
        try:
            (folder / 'probe').write_text('x')
            return False
        except OSError:
            return True
        finally:
            _set_read_only(folder, False)


READ_ONLY = _read_only_enforced()


def run_protected(variant, scope='final'):
    """합성 회원가입 서비스 변형(variant)을 임시 폴더에 쓰고, 그 범위(scope)의 보호 테스트를 dev unittest 실행기로 돌린다.
    돌려주는 값: {"report": unittest-v1 보고서, "exit": 종료 코드}. 믿을 수 있는 합성 코드만 이 방식으로 실행한다.
    Snapshot 폴더는 컨테이너(dev 샌드박스·D5)처럼 읽기 전용으로 둔다."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for path, content in signup_service.build(variant).items():
            (root / 'snapshot' / path).parent.mkdir(parents=True, exist_ok=True)
            (root / 'snapshot' / path).write_text(content, encoding='utf-8')
        for path, content in pt.protected_files(scope).items():
            (root / 'inputs' / path).parent.mkdir(parents=True, exist_ok=True)
            (root / 'inputs' / path).write_text(content, encoding='utf-8')
        _set_read_only(root / 'snapshot', True)
        try:
            # 변형마다 새 파이썬 프로세스에서 돌린다(앱 모듈 main·models가 프로세스에 남지 않게).
            completed = subprocess.run(
                [sys.executable, '-I', '-c', _RUN, str(RUNNER), str(root / 'inputs/tests/signup'),
                 str(root / 'snapshot'), SUITE['scopes'][scope]['pattern']],
                capture_output=True, text=True, timeout=600, cwd=root)
        finally:
            _set_read_only(root / 'snapshot', False)  # 임시 폴더를 지울 수 있게 되돌린다
        return json.loads(completed.stdout)


@pytest.mark.skipif(not RUNTIME, reason='fastapi·httpx·sqlalchemy·argon2-cffi가 있어야 실행 시험을 돈다')
@pytest.mark.parametrize('variant', sorted(EXPECTED_FAILURES))
def test_protected_suite_catches_each_defect_in_every_set(variant):
    """변형마다 시험지 세 벌(순환용·시범 최종용·공식 최종용) 모두에서 기대한 검사만 FAIL이고 나머지는 PASS다
    (EXPECTED_FAILURES, 세 시험지의 난이도가 같다는 확인). 연결이 안 되는 변형은 모두 미검증이고 근거 코드가 맞다.
    실패 설명에 해시 형식 글자가 남지 않는다. 시험지 세 벌은 프로세스를 나눠 함께 돌린다."""
    if variant == 'lifespan_source_database' and not READ_ONLY:
        pytest.skip('이 계정은 쓰기 권한을 뺀 폴더에도 쓸 수 있어 읽기 전용 Snapshot을 흉내 낼 수 없다')
    with ThreadPoolExecutor(len(pt.SETS)) as pool:
        outputs = dict(zip(pt.SETS, pool.map(lambda exam: run_protected(variant, exam), pt.SETS)))
    names = {check['checkId']: check['key'] for check in SUITE['checks']}
    expected = EXPECTED_FAILURES[variant]
    for exam, output in outputs.items():
        inputs = pt.evaluation_inputs(CONTRACT, exam)
        results = by_id(check_results_from_unit_report(output['report'], inputs['unit_cases'], evidence_id=uid(70)))
        statuses = {names[check_id]: result.status for check_id, result in results.items()}
        if expected is None:
            assert set(statuses.values()) == {Status.ERROR}, exam
            assert {result.error_code for result in results.values()} == UNVERIFIED_CODES[variant], exam
            continue
        assert {key for key, status in statuses.items() if status == Status.FAIL} == expected, exam
        assert {key for key, status in statuses.items() if status == Status.ERROR} == \
            EXPECTED_UNVERIFIED.get(variant, set()), exam
        for result in results.values():  # 실패 설명에 해시 형식 글자가 남지 않는다.
            assert '$argon2' not in (result.actual_result or '')


# ---------------------------------------------------------------- 팀장 전달용 설정 내보내기


def test_export_writes_dev_ready_files(tmp_path):
    """export가 만든 세 파일(QA·Security 보호 케이스 하나, 보호 범위 두 개, 인계 정보)을 dev 코드가 받아 주고,
    suite 참조·지문이 서로 맞는다. 이미 있는 파일은 --force 없이 덮어쓰지 않고, 명령줄(main)의 --suite-ref가 반영된다."""
    from agents.roles.qa_contract import QACaseBinding
    from evaluation.protected_tests.__main__ import export, main
    from mcp_tools.tools.unit_config import UnitTestScope
    written = export(tmp_path, contract=CONTRACT)
    cases = json.loads(written['owned-protected-cases.local.json'].read_text(encoding='utf-8'))
    scopes = json.loads(written['unit-protected-scopes.json'].read_text(encoding='utf-8'))
    handoff = json.loads(written['handoff.json'].read_text(encoding='utf-8'))
    assert cases['cases'] == pt.owned_cases(CONTRACT, 'QA')['cases'] + pt.owned_cases(CONTRACT, 'SECURITY')['cases']
    assert len(cases['cases']) == 9
    assert [scope['name'] for scope in scopes] == ['protected-signup-qa', 'protected-signup-security']
    for scope in scopes:
        UnitTestScope(**scope)
        assert scope['protected_suite_ref'] == handoff['protectedSuiteRef']
    for case in cases['cases']:
        QACaseBinding(tool_name=case['toolName'], selector=case['selector'], test_id=case['testId'],
                      requirement_id=UUID(case['requirementId']), title=case['title'], expected_result=case['expectedResult'])
    assert handoff['suiteSha256'] == {scope: pt.suite_sha256(scope) for scope in pt.SCOPES}
    with pytest.raises(FileExistsError):
        export(tmp_path, contract=CONTRACT)  # 이미 있는 파일은 --force 없이 덮어쓰지 않는다.
    assert main(['export', '--out', str(tmp_path), '--force', '--suite-ref', 'artifact://protected-tests/x']) == 0
    assert {scope['protected_suite_ref'] for scope in json.loads(
        written['unit-protected-scopes.json'].read_text(encoding='utf-8'))} == {'artifact://protected-tests/x'}


# ---------------------------------------------------------------- 시험지 노출 확인


def test_exam_exposure_finds_copied_exams_and_evaluation_paths():
    """코드 묶음에 시험지가 이름만 바뀌어(또는 줄바꿈만 바뀌어) 들어 있거나 평가 코드 경로가 있으면 찾는다.
    정상 서비스 코드에서는 아무것도 찾지 않는다."""
    final_exam = pt.protected_files('final')['tests/signup/exam_final.py'].encode('utf-8')
    practice = pt.protected_files('in-loop-qa')['tests/signup/practice_qa.py'].encode('utf-8')
    clean = {path: text.encode('utf-8') for path, text in signup_service.build('good').items()}
    assert pt.exam_exposure(clean) == []
    found = pt.exam_exposure({**clean, 'tests/test_copy.py': final_exam,
                              'notes/practice.py': practice.replace(b'\n', b'\r\n'),
                              'src/evaluation/README.md': b'x', 'vendor/protected_tests/a.py': b'y',
                              'experiments/scn001/final/day1/run/output.json': b'{}', 'experiments/notes.txt': b'z'})
    assert {(item['path'], item['reason']) for item in found} == {
        ('tests/test_copy.py', 'SAME_AS_PROTECTED_FILE'), ('notes/practice.py', 'SAME_AS_PROTECTED_FILE'),
        ('src/evaluation/README.md', 'EVALUATION_PATH'), ('vendor/protected_tests/a.py', 'EVALUATION_PATH'),
        ('experiments/scn001/final/day1/run/output.json', 'RESULTS_PATH')}
    assert next(item for item in found if item['path'] == 'tests/test_copy.py')['protectedFile'] == \
        'tests/signup/exam_final.py'
