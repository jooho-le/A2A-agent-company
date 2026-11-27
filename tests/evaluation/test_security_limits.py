"""보안 판정기가 이상하게 쓴 코드에서도 정해진 시간 안에 끝나는지 확인한다. (6차 외부 점검 대응)

6차 외부 점검에서 정상 코드에 `unused = payload`, `unused = db`를 연속으로 넣기만 해도 판정기가 끝나지 않는 문제가 나왔다.
판정기는 에이전트가 만든 코드를 읽는 도구이므로 어떤 코드가 들어와도 다음 세 가지를 지켜야 한다.
  1) 반드시 끝난다 (같은 이름에 여러 객체를 번갈아 대입하는 코드, 서로 부르는 함수, 지수적으로 불어나는 식 등).
  2) 큰 입력도 정해진 시간 안에 끝나는 것을 목표로 한다. 아래 고정 사례의 시간 제한은 모든 코드의 선형 시간 복잡도를 증명하지 않는다.
  3) 끝낼 수 없으면 PASS를 지어내지 않고 UNVERIFIED로 답한다 (ANALYSIS_TOO_COMPLEX).

멈추는 코드를 같은 프로세스에서 시험하면 시험 전체가 함께 멈춘다. 그래서 1)과 2)는 사례들을 별도 프로세스에서 시간 제한을 두고 돌린다.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from evaluation.security import assess
from evaluation.security import static_analysis as sa
from tests.evaluation.signup_examples import GOOD, GOOD_EXPECTED
from tests.evaluation.test_security_assess import HASH_FUNCTION, _both, _replace, make_request, outcomes

ROOT = Path(__file__).resolve().parents[2]
BATCH_SECONDS = 120  # 사례 전체를 돌리는 별도 프로세스에 주는 시간. 정상이면 10초 안팎에 끝난다. 넘기면 어느 사례에서 멈췄는지 알려 준다.
CASE_SECONDS = 15    # 사례 하나에 허용하는 시간. 정상 판정은 1초 안팎이고, 시간이 폭발하는 결함은 이 시간을 크게 넘는다.


def _in_route(body):
    # 가입 경로 함수에서 사용자를 만드는 줄 바로 앞에 body(문장들)를 끼워 넣은 소스 묶음 함수를 만든다.
    return _replace('routers.py', '    user = User(', body + '    user = User(')


def _in_hash(body, tail=''):
    # hash_password 함수를 body로 바꾸고 tail(도우미 함수들)을 security.py 맨 뒤에 붙인 소스 묶음 함수를 만든다.
    return lambda files: dict(files, **{'security.py': files['security.py'].replace(HASH_FUNCTION, body) + tail})


def _in_models(text):
    # models.py 맨 뒤에 text를 덧붙인 소스 묶음 함수를 만든다.
    return lambda files: dict(files, **{'models.py': files['models.py'] + text})


def _lines(template, count):
    # template 안의 %d를 모두 번호(0, 1, 2, ...)로 바꾼 글을 count개 이어 붙인다. 예) '    t%d = f%d\n'은 '    t0 = f0\n    t1 = f1\n...'이 된다.
    return ''.join(template.replace('%d', str(number)) for number in range(count))


def _hash_chain(length):
    # hash_password가 도우미 h0 → h1 → ... → h(length-1) 를 차례로 거쳐 마지막 도우미에서 HASHER.hash를 부르는 소스 묶음 함수를 만든다.
    tail = ''.join('\n\ndef h%d(x):\n    return h%d(x)\n' % (index, index + 1) for index in range(length - 1))
    tail += '\n\ndef h%d(x):\n    return HASHER.hash(x)\n' % (length - 1)
    return _in_hash('def hash_password(password: str) -> str:\n    return h0(password)\n', tail)


UNVERIFIED_HASH = {'REQ-005': 'UNVERIFIED'}
LEAK = {'REQ-006': 'FAIL'}
ANY_OF_PASS_OR_UNVERIFIED = ('PASS', 'UNVERIFIED')  # 파이썬의 재귀 한도에 따라 달라질 수 있는 사례. 틀린 FAIL이나 오류만 아니면 된다.

# 각 사례: (이름, 소스를 바꾸는 함수, GOOD과 달라지는 기대 판정). {}이면 "GOOD과 같다(모두 PASS)"는 뜻이다.
LIMIT_CASES = [
    # --- 같은 변수에 여러 객체를 번갈아 대입하는 코드 (6차 점검 ①) ---
    ('alias reassigned to another object', _in_route('    unused = payload\n    unused = db\n'), {}),
    ('alias handed back and forth', _in_route('    a = payload\n    b = a\n    a = b\n    b = db\n    a = b\n'), {}),
    ('swap inside a loop', _in_route('    a = payload\n    b = db\n    for _ in range(3):\n        a, b = b, a\n'), {}),
    ('rotating names', _in_route('    a = payload\n    b = db\n    c = a\n    a = b\n    b = c\n    c = a\n    a = c\n    b = a\n'), {}),
    ('field path growing in a loop', _in_route('    node = payload\n    for _ in range(3):\n        node = node.child\n'), {}),
    ('field path growing 60 steps', _in_route('    x = payload\n' + _lines('    x = x.f%d\n', 60)), {}),
    ('alias ring of 300 names', _in_route(''.join('    v%d = v%d\n' % (i, (i + 1) % 300) for i in range(300)) + '    v0 = payload\n'), {}),
    # 위 코드들은 입력 객체를 바꾸지 않으므로 통과해야 하고, 아래는 별칭을 거쳐 비밀번호 필드를 덮어쓰므로 통과하면 안 된다.
    ('write through swapped alias', _in_route(
        '    a = payload\n    b = db\n    for _ in range(3):\n        a, b = b, a\n    a.password = payload.email\n'), UNVERIFIED_HASH),
    ('write through growing path', _in_route(
        '    node = payload\n    for _ in range(3):\n        node = node.child\n    node.password = payload.email\n'), UNVERIFIED_HASH),
    ('write through alias that was reassigned', _in_route(
        '    alias = payload\n    alias = db\n    alias.password = payload.email\n'), UNVERIFIED_HASH),
    ('write through a chain of attribute aliases', _in_route(
        '    a = payload.profile\n    b = a.inner\n    a = b.inner\n    b = a.inner\n    a.password = payload.email\n'), UNVERIFIED_HASH),
    ('write through alias ring of 300 names', _in_route(
        ''.join('    v%d = v%d\n' % (i, (i + 1) % 300) for i in range(300)) + '    v0 = payload\n    v150.password = payload.email\n'),
        UNVERIFIED_HASH),
    # --- 식이 지수적으로 불어나거나 길게 이어지는 코드 ---
    ('doubling expression 45 levels', _in_hash(
        'def hash_password(password: str) -> str:\n    a0 = password\n' + ''.join('    a%d = a%d + a%d\n' % (i + 1, i, i) for i in range(45))
        + '    return HASHER.hash(a45)\n'), {}),
    ('fibonacci expression 80 levels', _in_hash(
        'def hash_password(password: str) -> str:\n    a0 = password\n    a1 = password\n'
        + ''.join('    a%d = a%d + a%d\n' % (i, i - 1, i - 2) for i in range(2, 80)) + '    return HASHER.hash(a79)\n'), {}),
    ('one password repeated 3000 times', _in_hash(
        'def hash_password(password: str) -> str:\n    return HASHER.hash(' + ' + '.join(['password'] * 3000) + ')\n'), {}),
    ('ternary nested 150 deep', _in_hash(
        'def hash_password(password: str) -> str:\n    return HASHER.hash(' + 'password if password else (' * 150 + 'password' + ')' * 150 + ')\n'),
        {'REQ-005': ANY_OF_PASS_OR_UNVERIFIED}),
    ('reassigning the password 2000 times', _in_hash(
        'def hash_password(password: str) -> str:\n' + '    password = password + ""\n' * 2000 + '    return HASHER.hash(password)\n'), {}),
    # --- 함수가 서로를 부르거나 길게 이어지는 코드 ---
    ('hash helper chain of 2 functions', _hash_chain(2), {}),
    ('hash helper chain of 40 functions is beyond what is followed', _hash_chain(40), UNVERIFIED_HASH),
    ('helpers calling each other forever', _in_hash(
        'def hash_password(password: str) -> str:\n    return ping(password)\n',
        '\n\ndef ping(x):\n    return pong(x)\n\n\ndef pong(x):\n    return ping(x)\n'), UNVERIFIED_HASH),
    ('twelve helpers calling each other 50 times', _in_hash(
        'def hash_password(password: str) -> str:\n    return h0(password)\n',
        ''.join('\n\ndef h%d(x):\n' % i + ''.join('    h%d(x)\n' % ((i + 1 + j) % 12) for j in range(50)) + '    return HASHER.hash(x)\n' for i in range(12))), {}),
    ('2000 functions calling the next one', _in_models(''.join('\n\ndef g%d(x):\n    return g%d(x)\n' % (i, i + 1) for i in range(2000))), {}),
    ('1500 classes inheriting in reverse order', _in_models(
        ''.join('\n\nclass K%d(K%d):\n    pass\n' % (i, i + 1) for i in range(1500)) + '\n\nclass K1500(User):\n    pass\n'), {}),
    # --- 같은 종류의 문장이 아주 많은 코드 (시간이 코드 길이에 비례해야 한다) ---
    ('2000 assignments', _in_route(_lines('    t%d = payload.f%d\n', 2000)), {}),
    ('reverse assignment chain of 3000', _in_route(
        ''.join('    t%d = t%d\n' % (i + 1, i) for i in reversed(range(3000))) + '    t0 = payload\n'), {}),
    ('3000-step assignment chain into a log call', _in_route(
        '    import logging\n' + ''.join('    t%d = t%d\n' % (i + 1, i) for i in reversed(range(3000)))
        + '    t0 = payload.password\n    logging.info(t3000)\n'), LEAK),
    ('2000 email normalizations', _in_route(_lines('    e%d = payload.email.strip().lower()\n', 2000)), {}),
    ('2000 password hashes', _in_route(_lines('    h%d = hash_password(payload.password)\n', 2000)), {}),
    ('1500 user objects', _in_route(_lines('    u%d = User(email=payload.email, password_hash=hash_password(payload.password))\n', 1500)), {}),
    ('2000 input field overwrites', _in_route(_lines('    payload.f%d = payload.email\n', 2000)), {}),
    ('2000 helpers each overwriting a field', _both(
        _in_route(_lines('    scrub%d(payload)\n', 2000)),
        lambda files: dict(files, **{'routers.py': files['routers.py'] + _lines('\n\ndef scrub%d(item):\n    item.f%d = item.email\n', 2000)})), {}),
    ('5000 print calls', _in_route('    pass\n' * 5 + _lines('    print(%d)\n', 5000)), {}),
    ('2000 module-level functions', _in_models(_lines('\n\ndef f%d(a):\n    b = a\n    return b.strip()\n', 2000)), {}),
    ('large CREATE TABLE with 5000 columns', _in_models(
        '\nimport sqlite3\nS = "create table users (' + ', '.join('c%d text' % i for i in range(5000))
        + ', email text, password_hash text)"\nsqlite3.connect(":memory:").execute(S)\n'), {}),
    ('SQL with unclosed parentheses', _in_models('\nS = "create unique index ix on t(' + '(a' * 400 + '"\n'), {}),
    ('SQL with deeply nested parentheses', _in_models(
        '\nimport sqlite3\nS = "create table users (email text check ' + '(' * 300 + '1' + ')' * 300
        + ', password_hash text)"\nsqlite3.connect(":memory:").execute(S)\n'), {}),
]

RUNNER = '''
import json, sys, time, warnings
warnings.simplefilter("ignore")
sys.path[:0] = [__SRC__, __ROOT__]
from tests.evaluation.signup_examples import GOOD
from tests.evaluation.test_security_assess import outcomes
from tests.evaluation.test_security_limits import LIMIT_CASES
for label, change, _changed in LIMIT_CASES:
    started = time.monotonic()
    result, _assessment = outcomes(change(dict(GOOD)))
    print(json.dumps({"label": label, "outcomes": result, "seconds": round(time.monotonic() - started, 3)}), flush=True)
'''.replace('__SRC__', repr(str(ROOT / 'src'))).replace('__ROOT__', repr(str(ROOT)))


@pytest.fixture(scope='module')
def batch():
    # 모든 사례를 별도 프로세스 하나에서 차례로 판정한다. 돌려주는 값: ({이름: 결과}, 시간 안에 끝나지 못한 사례 이름 또는 None)
    # 어느 사례에서 멈추면 그 프로세스를 끊고, 거기까지 끝낸 결과와 멈춘 사례 이름을 알려 준다. (시험 전체가 같이 멈추지 않게 하려는 것이다.)
    try:
        done = subprocess.run([sys.executable, '-c', RUNNER], cwd=ROOT, capture_output=True, text=True, timeout=BATCH_SECONDS)
        text, failure = done.stdout, (done.stderr[-2000:] if done.returncode else None)
    except subprocess.TimeoutExpired as expired:
        text = expired.stdout.decode('utf-8', 'replace') if isinstance(expired.stdout, bytes) else (expired.stdout or '')
        failure = None
    results = {item['label']: item for item in (json.loads(line) for line in text.splitlines() if line.startswith('{'))}
    unfinished = next((case[0] for case in LIMIT_CASES if case[0] not in results), None)
    if failure:
        pytest.fail(f'사례를 돌리는 프로세스가 오류로 끝났다:\n{failure}')
    return results, unfinished


@pytest.mark.parametrize('label,change,changed', LIMIT_CASES, ids=[item[0] for item in LIMIT_CASES])
def test_case_finishes_in_time_with_the_expected_outcome(batch, label, change, changed):
    results, unfinished = batch
    assert label in results, f'{BATCH_SECONDS}초 안에 끝나지 않았다(멈춘 사례: {unfinished!r})'
    result = results[label]
    assert result['seconds'] < CASE_SECONDS, f'{result["seconds"]}초 걸렸다. 코드 길이에 비해 시간이 지나치게 늘어난다.'
    for key, value in {**GOOD_EXPECTED, **changed}.items():
        allowed = value if isinstance(value, tuple) else (value,)
        assert result['outcomes'][key] in allowed, f'{key}: {result["outcomes"][key]} (기대 {allowed})'


def test_every_case_name_is_unique():
    # 결과를 이름으로 찾으므로 이름이 겹치면 안 된다.
    names = [case[0] for case in LIMIT_CASES]
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------------------------
# 끝낼 수 없을 때의 동작: 판정을 지어내지 않고 UNVERIFIED로 답한다.
# ---------------------------------------------------------------------------------------------
def _evidence(assessment, key):
    return json.loads(assessment.evidence_json)['requirements'][key]


def test_work_limit_makes_hash_requirements_unverified_instead_of_guessing(monkeypatch):
    # 작업량 한도를 아주 작게 줄이면, 입력의 출처를 따라가는 REQ-005는 PASS를 지어내지 않고 ANALYSIS_TOO_COMPLEX로 UNVERIFIED가 된다.
    monkeypatch.setattr(sa, 'WORK_LIMIT', 3)
    result, assessment = outcomes(dict(GOOD))
    assert assessment is not None
    assert result['REQ-005'] == 'UNVERIFIED'
    assert _evidence(assessment, 'REQ-005')['basis'] == 'ANALYSIS_TOO_COMPLEX'
    # REQ-003도 이제 정규화 결과의 반환 경로를 따라가므로 같은 한도를 넘으면 미검증이다.
    assert result['REQ-003'] == 'UNVERIFIED'
    assert _evidence(assessment, 'REQ-003')['basis'] == 'ANALYSIS_TOO_COMPLEX'


def test_default_work_limit_is_far_above_what_the_reference_example_needs(monkeypatch):
    # 정상 예제는 기본 한도의 아주 작은 부분만 쓴다. (한도가 빡빡해서 정상 코드를 UNVERIFIED로 만들지 않는다는 확인)
    seen = {}
    original = sa._tick

    def counting(ctx, cost=1):
        seen[id(ctx)] = ctx
        original(ctx, cost)

    monkeypatch.setattr(sa, '_tick', counting)
    result, _assessment = outcomes(dict(GOOD))
    assert result == GOOD_EXPECTED
    assert seen and max(ctx.work for ctx in seen.values()) < sa.WORK_LIMIT // 1000


@pytest.mark.parametrize('requirement,checker', [('REQ-003', 'check_duplicate_email'), ('REQ-005', 'check_password_storage'),
                                                 ('REQ-006', 'check_exposure')])
def test_recursion_error_in_one_requirement_only_affects_that_requirement(monkeypatch, requirement, checker):
    # 식이 너무 깊게 중첩되어 파이썬의 재귀 한도를 넘으면 그 요구사항만 UNVERIFIED가 되고, 나머지 판정과 응답은 그대로 나온다.
    def too_deep(*_args, **_kwargs):
        raise RecursionError

    monkeypatch.setattr(sa, checker, too_deep)
    result, assessment = outcomes(dict(GOOD))
    assert assessment is not None
    assert result == {**GOOD_EXPECTED, requirement: 'UNVERIFIED'}
    assert _evidence(assessment, requirement)['basis'] == 'ANALYSIS_TOO_COMPLEX'


def test_assess_never_returns_a_pass_when_every_requirement_is_too_complex(monkeypatch):
    def too_complex(*_args, **_kwargs):
        raise sa.AnalysisTooComplex

    for checker in ('check_duplicate_email', 'check_password_storage', 'check_exposure'):
        monkeypatch.setattr(sa, checker, too_complex)
    assessment = assess(make_request(dict(GOOD)))
    assert assessment is not None
    assert {value for _requirement, value in assessment.requirement_outcomes} == {'UNVERIFIED'}
