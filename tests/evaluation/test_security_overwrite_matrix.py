"""입력 객체의 비밀번호 필드를 덮어쓰면 REQ-005가 통과하면 안 된다. "별칭을 만드는 방식"과 "필드를 쓰는 방식"을 모든 조합으로 시험한다. (6차 외부 점검의 연장)

외부 점검은 같은 종류의 우회를 한 번에 하나씩 찾아 왔다(필드 대입 → setattr → 별칭 → vars().update → ...).
조합을 하나하나 사례로 적으면 빠지는 칸이 생기므로, 다음 두 목록의 모든 조합을 만들어 판정한다.
  · 별칭 만들기: 대입, 튜플 풀기, or·조건식, 리스트·사전에 담았다 꺼내기, for·with·:=, 입력을 돌려주는 도우미 함수·메서드, vars()·__dict__, list()·next(iter())
  · 필드 쓰기: 대입, 증감 대입, del, setattr, __dict__·vars() 항목, object.__setattr__, __setattr__ 메서드, pop, setdefault, operator.setitem, dict.update
확인하는 것 세 가지:
  1) 비밀번호 필드(passwd)를 덮어쓰는 모든 조합은 통과(PASS)하지 않는다.
  2) 같은 조합이라도 다른 필드(nickname)만 바꾸거나, 입력이 아닌 다른 객체(db)를 바꾸면 그대로 통과한다. (정상 코드를 거르는 오탐 방지)
  3) 어느 필드가 바뀌는지 알 수 없는 쓰기(update(다른_사전), 변수 이름의 setattr)는 입력의 별칭에 하면 통과하지 않고, 입력과 상관없는 객체에 하면 통과한다.
     (단 vars()·__dict__ 같은 이름 공간에 쓰는 것은 승인한 해시 도구를 바꿔치기할 수 있어서 어디에 하든 통과하지 않는다.)
"""
import pytest

from tests.evaluation.signup_examples import GOOD, GOOD_EXPECTED
from tests.evaluation.test_security_assess import _both, _form_route, _router_helper, outcomes

IDENTITY_HELPER = 'def ident(item):\n    return item\n\n\ndef outer(item):\n    return ident(item)\n\n\nclass Box:\n    def me(self):\n        return self\n'

# 별칭 만들기: (이름, 본문 틀, 도우미 함수 소스, 쓰는 대상). 틀의 {src}는 별칭을 만들 원래 객체, {w}는 대상에 쓰는 문장들이 들어갈 줄이다.
# 쓰는 대상은 별칭 이름 a이다. 별칭 없이 원래 객체에 바로 쓰는 경우(itself)만 원래 객체이다.
ALIAS_FORMS = [
    ('itself', '    {w}\n', None, '{src}'),
    ('assignment', '    a = {src}\n    {w}\n', None, 'a'),
    ('chained assignment', '    a = b = {src}\n    {w}\n', None, 'a'),
    ('annotated assignment', '    a: object = {src}\n    {w}\n', None, 'a'),
    ('tuple unpacking', '    a, b = {src}, db\n    {w}\n', None, 'a'),
    ('starred unpacking', '    a, *rest = [{src}, db]\n    {w}\n', None, 'a'),
    ('or', '    a = {src} or db\n    {w}\n', None, 'a'),
    ('conditional expression', '    a = {src} if db else db\n    {w}\n', None, 'a'),
    ('walrus', '    (a := {src})\n    {w}\n', None, 'a'),
    ('list item', '    a = [{src}][0]\n    {w}\n', None, 'a'),
    ('dict item', '    a = {"k": {src}}["k"]\n    {w}\n', None, 'a'),
    ('dict get', '    a = {"k": {src}}.get("k")\n    {w}\n', None, 'a'),
    ('copy of a list', '    a = list([{src}])[0]\n    {w}\n', None, 'a'),
    ('next of iter', '    a = next(iter([{src}]))\n    {w}\n', None, 'a'),
    ('for loop', '    for a in [{src}]:\n        {w}\n', None, 'a'),
    ('with block', '    with {src} as a:\n        {w}\n', None, 'a'),
    ('helper returning its argument', '    a = ident({src})\n    {w}\n', IDENTITY_HELPER, 'a'),
    ('two nested helpers', '    a = outer({src})\n    {w}\n', IDENTITY_HELPER, 'a'),
    ('method returning self', '    a = {src}.me()\n    {w}\n', IDENTITY_HELPER, 'a'),
]

# 필드 쓰기: (이름, 줄들). {o}는 쓰는 대상 객체, {f}는 필드 이름이다. 모두 "필드 {f} 하나"를 바꾸는 방식이다.
FIELD_WRITES = [
    ('attribute assignment', ['{o}.{f} = form.email']),
    ('item assignment', ['{o}["{f}"] = form.email']),
    ('augmented assignment', ['{o}.{f} += "x"']),
    ('attribute delete', ['del {o}.{f}']),
    ('item delete', ['del {o}["{f}"]']),
    ('setattr', ['setattr({o}, "{f}", form.email)']),
    ('delattr', ['delattr({o}, "{f}")']),
    ('__dict__ item', ['{o}.__dict__["{f}"] = form.email']),
    ('vars() item', ['vars({o})["{f}"] = form.email']),
    ('object.__setattr__', ['object.__setattr__({o}, "{f}", form.email)']),
    ('__setattr__ method', ['{o}.__setattr__("{f}", form.email)']),
    ('__delattr__ method', ['{o}.__delattr__("{f}")']),
    ('pop', ['{o}.pop("{f}")']),
    ('setdefault', ['{o}.setdefault("{f}", form.email)']),
    ('operator.setitem', ['import operator', 'operator.setitem(vars({o}), "{f}", form.email)']),
    ('dict.update on vars()', ['dict.update(vars({o}), {"{f}": form.email})']),
    ('update with a dict', ['{o}.update({"{f}": form.email})']),
    ('update with a keyword', ['{o}.update({f}=form.email)']),
    ('__dict__.update', ['{o}.__dict__.update({"{f}": form.email})']),
    ('vars().update with a keyword', ['vars({o}).update({f}=form.email)']),
]

# 어느 필드가 바뀌는지 알 수 없는 쓰기: 입력의 별칭에 하면 통과하면 안 된다. (이름, 줄들, 이름 공간(vars·__dict__)에 쓰는가)
# 이름 공간에 어느 항목인지 모르게 쓰는 코드는 어떤 객체에 하든 승인한 해시 도구의 hash를 바꿔치기할 수 있어서(PasswordHasher의 이름 공간일 수도 있다),
# 입력과 상관없는 객체에 해도 REQ-005를 단정하지 않는다(UNVERIFIED). 이름 공간을 거치지 않는 쓰기는 입력과 상관없는 객체에 하면 통과한다.
UNKNOWN_FIELD_WRITES = [
    ('update with another dict', ['{o}.update(extra)'], False),
    ('update with an unpacked dict', ['{o}.update(**extra)'], False),
    ('setattr with a variable name', ['setattr({o}, key, 1)'], False),
    ('object.__setattr__ with a variable name', ['object.__setattr__({o}, key, 1)'], False),
    ('__setattr__ with a variable name', ['{o}.__setattr__(key, 1)'], False),
    ('pop with a variable name', ['{o}.pop(key)'], False),
    ('setdefault with a variable name', ['{o}.setdefault(key, 1)'], False),
    ('clear', ['{o}.clear()'], False),
    ('popitem', ['{o}.popitem()'], False),
    ('__dict__.update with another dict', ['{o}.__dict__.update(extra)'], True),
    ('vars().update with another dict', ['vars({o}).update(extra)'], True),
    ('dict.update on vars() with another dict', ['dict.update(vars({o}), extra)'], True),
    ('operator.setitem with a variable key', ['import operator', 'operator.setitem(vars({o}), key, 1)'], True),
]


def _build(alias, write_lines, source, field):
    # 별칭 틀의 {w} 자리에 쓰는 문장들을 들여쓰기에 맞춰 넣은 가입 경로 본문(문자열)과 도우미 함수 소스를 만든다.
    _name, template, helper, target = alias
    out = []
    for line in template.replace('{src}', source).split('\n'):
        if line.strip() == '{w}':
            indent = line[:len(line) - len(line.lstrip())]
            out.extend(indent + item.replace('{o}', target.replace('{src}', source)).replace('{f}', field) for item in write_lines)
        elif line:
            out.append(line)
    return '\n'.join(out) + '\n', helper


def _verdict(alias, write_lines, source, field):
    body, helper = _build(alias, write_lines, source, field)
    change = _form_route(body)
    if helper:
        change = _both(_router_helper(helper), change)
    return outcomes(change(dict(GOOD)))[0]


def _ids(items):
    return [item[0] for item in items]


# 정상 코드를 거르지 않는지 보는 시험은 서로 다른 분석 경로를 지나는 대표 쓰기 방식만 쓴다. (모든 조합을 돌리면 시간이 길어진다.)
REPRESENTATIVE_WRITES = [item for item in FIELD_WRITES if item[0] in (
    'attribute assignment', 'item assignment', 'setattr', '__dict__ item', 'vars() item', 'pop', 'update with a dict')]


def _failures(alias, writes, source, field, accept):
    # alias와 writes의 모든 조합을 판정해서, accept(결과)가 False인 조합들을 모아 돌려준다.
    wrong = []
    for write in writes:
        result = _verdict(alias, write[1], source, field)
        if not accept(result):
            wrong.append(f'{write[0]}: {result}')
    return wrong


@pytest.mark.parametrize('alias', ALIAS_FORMS, ids=_ids(ALIAS_FORMS))
def test_overwriting_the_password_field_is_never_a_pass(alias):
    wrong = _failures(alias, FIELD_WRITES, 'form', 'passwd', lambda result: result['REQ-005'] == 'UNVERIFIED')
    assert not wrong, f'{alias[0]}: 비밀번호 필드를 덮어썼는데 UNVERIFIED가 아니다 -> ' + '; '.join(wrong)


@pytest.mark.parametrize('alias', ALIAS_FORMS, ids=_ids(ALIAS_FORMS))
def test_writing_another_field_of_the_input_still_passes(alias):
    wrong = _failures(alias, REPRESENTATIVE_WRITES, 'form', 'nickname', lambda result: result == GOOD_EXPECTED)
    assert not wrong, f'{alias[0]}: 다른 필드만 바꿨는데 통과하지 못한다 -> ' + '; '.join(wrong)


@pytest.mark.parametrize('alias', ALIAS_FORMS, ids=_ids(ALIAS_FORMS))
def test_writing_the_password_field_of_another_object_still_passes(alias):
    # 별칭의 원래 객체가 입력(form)이 아니라 db 연결이면, 같은 이름(passwd)을 써도 입력의 비밀번호와 상관없다.
    wrong = _failures(alias, REPRESENTATIVE_WRITES, 'db', 'passwd', lambda result: result == GOOD_EXPECTED)
    assert not wrong, f'{alias[0]}: 입력과 상관없는 객체를 바꿨는데 통과하지 못한다 -> ' + '; '.join(wrong)


@pytest.mark.parametrize('alias', ALIAS_FORMS, ids=_ids(ALIAS_FORMS))
def test_writing_unknown_fields_of_the_input_is_not_a_pass(alias):
    wrong = _failures(alias, UNKNOWN_FIELD_WRITES, 'form', 'passwd', lambda result: result['REQ-005'] == 'UNVERIFIED')
    assert not wrong, f'{alias[0]}: 어느 필드인지 모르는 쓰기인데 UNVERIFIED가 아니다 -> ' + '; '.join(wrong)


@pytest.mark.parametrize('alias', ALIAS_FORMS, ids=_ids(ALIAS_FORMS))
def test_writing_unknown_fields_of_another_object(alias):
    wrong = []
    for write in UNKNOWN_FIELD_WRITES:
        result = _verdict(alias, write[1], 'db', 'passwd')
        # 이름 공간에 쓰는 코드는 해시 도구를 바꿔치기할 수 있어서 단정하지 않는다. 나머지는 입력과 상관없으므로 통과한다.
        if (result['REQ-005'] == 'UNVERIFIED') != bool(write[2]) or (not write[2] and result != GOOD_EXPECTED):
            wrong.append(f'{write[0]}: {result}')
    assert not wrong, f'{alias[0]}: ' + '; '.join(wrong)
