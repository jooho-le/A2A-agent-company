# 에이전트가 만든 회원가입 코드를 "실행하지 않고 글자 그대로 읽어서" 보안 요구사항을 확인하는 파일이다.
# 확인하는 요구사항은 세 가지이다.
#   REQ-003 : 같은 이메일로 두 번 가입할 수 없어야 한다.
#   REQ-005 : 비밀번호를 안전한 방식(Argon2id)으로 해시해서 저장해야 한다.
#   REQ-006 : 비밀번호나 그 해시가 응답·로그·오류 메시지로 새어 나가면 안 된다.
#
# [이 파일의 핵심 아이디어: AST]
# AST(추상 구문 트리)는 파이썬 코드를 "문법 구조를 나무 모양으로 정리한 것"이다. 예를 들어
#     password_hash = hasher.hash(password)
# 라는 한 줄은 "대입문 ← (왼쪽: 이름 password_hash) (오른쪽: hasher의 hash 함수를 password로 호출)" 같은 나무가 된다.
# 파이썬 기본 도구(ast 모듈)가 코드를 이 나무로 바꿔 주면, 나무의 가지를 하나씩 따라가며
# "이 코드가 비밀번호를 그냥 저장하는가?", "로그에 비밀번호를 찍는가?" 같은 질문에 답할 수 있다.
# 코드를 실제로 실행하지 않으므로 에이전트가 만든 코드가 위험한 일을 하더라도 안전하다.
#
# [판정 원칙]
#  - 이 파일이 아는 코드 모양에서만 PASS(문제 없음)나 FAIL(문제 있음)을 낸다. 처음 보는 모양이면 UNVERIFIED(모름)로 남긴다.
#    "근거 없이 PASS"를 내지 않는 것이 가장 중요한 규칙이다.
#  - 반대로 멀쩡한 코드를 FAIL로 잘못 판정(오탐)해도 불필요한 수정 작업이 생기므로 피한다.
#    그래서 이름은 "단어 단위"로 보고(예: passed ≠ password), 확실한 근거가 있을 때만 FAIL을 낸다.
#  - 판정 근거로 남기는 기록(관측값)에는 파일 경로·줄 번호·종류·설정 숫자만 담는다. 소스 글자나 비밀번호 값은 담지 않는다.
import ast  # 파이썬 코드를 AST(나무)로 바꿔 주는 기본 도구
import copy  # 코드 조각을 얕게 복사하는 도구(함수 본문 일부를 뺀 사본을 만들 때 쓴다)
from collections import deque  # 양쪽 끝에서 넣고 뺄 수 있는 목록(할 일을 차례로 처리할 때 쓴다)
from dataclasses import dataclass, field  # 값을 담는 "데이터 상자" 클래스를 쉽게 만드는 도구
import re  # 정규식(글자 모양 규칙) 도구

PASS, FAIL, UNVERIFIED = 'PASS', 'FAIL', 'UNVERIFIED'  # 판정 결과 세 가지의 이름(오타를 막으려고 변수로 만들어 둠)
MAX_OBSERVATIONS = 20  # 근거 기록을 한 판정에 최대 몇 개까지 남길지(너무 길어지지 않게)
MIN_SALT_BYTES = 8  # Argon2(RFC 9106)가 허용하는 salt의 최소 길이(바이트). 이보다 짧게 지정하면 salt를 고유하게 만들 수 없다.
# 값을 직접 보여 주지 않고 길이·형식만 알려 주는 함수들. 인자로 비밀번호 변수를 넣어도 "노출"로 보지 않는다.
# 예: len(password)는 비밀번호의 길이만 알려 주고 비밀번호 자체는 드러내지 않는다.
SAFE_CALLS = frozenset({'len', 'bool', 'type', 'isinstance', 'id', 'hasattr'})
# 객체가 가진 "모든 필드를 한꺼번에" 꺼내는 함수들. 비밀번호 필드가 있는 객체에 쓰면 비밀번호도 함께 나온다.
DUMP_METHODS = frozenset({'model_dump', 'dict', 'json', 'model_dump_json', 'copy', 'model_copy'})
# pydantic(입력 검증 라이브러리)이 이메일 같은 글자를 대신 정리해 주는 설정 이름들.
# strip = 앞뒤 공백 제거, lower = 소문자로 바꾸기
STRIP_OPTIONS = frozenset({'str_strip_whitespace', 'anystr_strip_whitespace', 'strip_whitespace'})
LOWER_OPTIONS = frozenset({'str_to_lower', 'anystr_lower', 'to_lower'})


@dataclass
class Observation:
    # "여기서 이런 것을 봤다"는 근거 기록 하나. 판정 이유를 나중에 사람이 확인할 수 있게 남긴다.
    path: str    # 어느 파일인지
    line: int    # 몇 번째 줄인지
    kind: str    # 무엇을 봤는지를 나타내는 짧은 이름(예: PLAINTEXT_STORAGE = 비밀번호 원문 저장)
    detail: dict = field(default_factory=dict)  # 추가 정보(예: 설정 숫자). 기본값은 빈 사전

    def to_dict(self):
        # JSON으로 쓰기 쉽게 사전으로 바꾼다. **는 사전의 내용을 풀어서 넣는다는 뜻이다.
        return {'path': self.path, 'line': self.line, 'kind': self.kind, **self.detail}


@dataclass
class CheckOutcome:
    # 요구사항 하나를 검사한 결과.
    outcome: str  # PASS / FAIL / UNVERIFIED 중 하나
    basis: str    # 이런 판정을 한 이유를 나타내는 짧은 코드(예: VIOLATION_FOUND)
    observations: list = field(default_factory=list)  # 근거 기록(Observation) 목록

    def to_dict(self):
        # 근거 기록은 MAX_OBSERVATIONS개까지만 담는다.
        return {'outcome': self.outcome, 'basis': self.basis,
                'observations': [item.to_dict() for item in self.observations[:MAX_OBSERVATIONS]]}


@dataclass
class Module:
    # 분석할 소스 파일 하나. (파이썬에서는 .py 파일 하나를 "모듈"이라고 부른다.)
    path: str         # 파일 경로
    tree: ast.AST     # 그 파일을 AST(나무)로 바꾼 것
    aliases: dict     # import 별칭 표. 예) import numpy as np 면 {"np": "numpy"}
    bindings: dict    # 이름이 파일 안에서 묶이는 횟수 표(_name_bindings). 여러 검사가 같이 쓰므로 한 번만 계산해 둔다.


class ModuleSet(dict):
    # 분석할 파일들의 사전({파일 경로: Module}). 한 번의 판정 안에서 여러 검사가 똑같이 필요로 하는 계산 결과(cache)를 한 번만
    # 계산하려고 함께 들고 다닌다. 판정이 끝나면 이 사전과 함께 사라지므로 판정 사이에 남는 전역 상태가 아니다.
    def __init__(self):
        super().__init__()
        self.cache = {}


# ---------------------------------------------------------------- 이름 판정

def _tokens(name):
    # 변수·함수 이름을 "소문자 단어들"로 쪼갠다.
    # snake_case(밑줄), camelCase(대문자 경계), 점(.)을 모두 단어 경계로 본다.
    # 예) "passedCount" → ["passed", "count"],  "user_password_hash" → ["user", "password", "hash"]
    spaced = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name)  # 소문자와 대문자가 붙은 자리에 밑줄을 넣는다.
    return [part for part in re.split(r'[^A-Za-z0-9]+', spaced.lower()) if part]


class NameMatcher:
    # 이름이 어떤 패턴(예: 비밀번호처럼 보이는 이름)과 맞는지 "단어 단위"로 판정하는 도구이다.
    #
    # 왜 단어 단위로 보는가?
    #   글자가 통째로 들어 있는지만 보면 "passed"(통과했다), "bypass"(우회) 같은 이름도 "pass"가 있다는 이유로
    #   비밀번호로 오해한다. 단어 하나가 패턴과 통째로 맞아야(whole_word=True) 이런 오탐을 피할 수 있다.
    # excluded(제외 단어)는 무슨 뜻인가?
    #   length(길이), message(안내문구) 같은 단어가 이름에 들어 있으면 "비밀번호 값"이 아니라
    #   설명이나 설정을 가리키는 이름으로 보고 제외한다. 예) MIN_PASSWORD_LENGTH, PASSWORD_ERROR_MESSAGE
    def __init__(self, pattern, excluded=(), whole_word=True):
        self.pattern = re.compile(pattern)
        self.excluded = frozenset(excluded)
        self.whole_word = whole_word

    def search(self, name):
        # 이름이 패턴과 맞으면 True, 아니면 False.
        if not isinstance(name, str):
            return False
        words = _tokens(name)
        if self.excluded.intersection(words):
            return False  # 제외 단어가 하나라도 들어 있으면 "해당 없음"
        if self.whole_word:
            return any(self.pattern.fullmatch(word) for word in words)  # 단어 하나가 패턴과 통째로 맞는가
        return self.pattern.search(name) is not None  # 이름 안 어딘가에 패턴이 있는가


def _sensitive(criteria):
    # "비밀번호처럼 보이는 이름"을 알아보는 도구를 만든다. 패턴과 제외 단어는 기준 파일(JSON)에서 읽는다.
    return NameMatcher(criteria['sensitiveNamePattern'], criteria.get('nonSecretNameWords', ()))


def _email_names(criteria):
    # "이메일처럼 보이는 이름"을 알아보는 도구를 만든다. 이메일은 e-mail처럼 글자 안에 들어 있는 경우도 있어서 단어 단위가 아니다.
    rules = criteria['duplicateEmail']
    return NameMatcher(rules['emailNamePattern'], rules.get('excludedNameWords', ()), whole_word=False)


# ---------------------------------------------------------------- 공통 도구

def load_modules(source_files, criteria):
    # 소스 파일들 중 분석할 .py 파일만 골라 AST(나무)로 바꾼다.
    # 테스트 파일은 제품 코드가 아니므로 제외한다. 문법 오류 등으로 읽지 못한 파일은 따로 모아 둔다.
    # 돌려주는 값: ({파일 경로: Module}, [읽지 못한 파일 경로들])
    modules, errors = ModuleSet(), []
    # 제외 규칙은 기준 파일(JSON)에서 읽는다. 값이 없으면 빈 목록으로 본다.
    prefixes = tuple(criteria.get('excludePathPrefixes', ()))
    segments = frozenset(criteria.get('excludePathSegments', ()))
    file_prefixes = tuple(criteria.get('excludeFilePrefixes', ()))
    file_names = frozenset(criteria.get('excludeFileNames', ()))
    for path in sorted(source_files):
        name = path.rsplit('/', 1)[-1]  # 경로에서 마지막 부분(파일 이름)만 꺼낸다.
        # .py 파일이 아니거나, 제외 규칙(테스트 폴더·test_로 시작하는 파일·conftest.py 등)에 걸리면 건너뛴다.
        if (not path.endswith('.py') or path.startswith(prefixes) or name.startswith(file_prefixes)
                or name in file_names or segments.intersection(path.split('/')[:-1])):
            continue
        try:
            # 'utf-8-sig'는 파일 맨 앞의 BOM(일부 편집기가 붙이는 표시)을 떼고 읽는다. 파이썬도 BOM이 붙은 소스를 그대로 실행한다.
            tree = ast.parse(bytes(source_files[path]).decode('utf-8-sig'), filename=path)  # 글자 → AST(나무)
        except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError, MemoryError):
            # 문법이 틀렸거나, 글자 인코딩이 이상하거나, 너무 깊거나 긴 식이어서 읽지 못한 파일이다.
            # 이 파일 하나만 "분석 불가"로 기록하고 나머지 파일의 판정은 그대로 이어 간다.
            errors.append(path)
            continue
        bindings = _name_bindings(tree)
        modules[path] = Module(path, tree, _aliases(tree, bindings), bindings)
    return modules, errors


def _name_bindings(tree):
    # 코드 안에서 어떤 이름에 값이 "묶이는(바인딩)" 모든 곳을 {이름: 횟수}로 센다.
    # 대입(=, +=, :=), for·with·except·comprehension의 변수, 함수·클래스 정의, 매개변수, import, match의 이름이 모두 해당한다.
    # 이 횟수가 1이면 "이름이 한 번만 정의되고 그 뒤로 바뀌지 않는다"는 뜻이다.
    # (이름이 다시 정의되거나 인자로 덮이면, 그 이름이 처음 정의한 값을 가리킨다고 믿을 수 없다.)
    counts = {}

    def add(name):
        counts[name] = counts.get(name, 0) + 1

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            add(node.id)
        elif isinstance(node, ast.arg):
            add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            add(node.name)
        elif isinstance(node, ast.alias):
            add(node.asname or node.name.split('.')[0])
        else:
            # match 문의 패턴(case [x, *rest]: 등)에서 새로 묶이는 이름. 오래된 파이썬에는 없는 노드라서 이름으로 확인한다.
            for attribute in ('name', 'rest'):
                if type(node).__name__ in ('MatchAs', 'MatchStar', 'MatchMapping') and getattr(node, attribute, None):
                    add(getattr(node, attribute))
    return counts


def _aliases(tree, bindings=None):
    # import 별칭 표를 만든다. 코드에서 "np.array"라고 써도 실제로는 "numpy.array"라는 뜻이므로,
    # 별칭을 실제 이름으로 되돌릴 수 있어야 어떤 함수를 부르는지 정확히 알 수 있다.
    # 예) import argon2 as a → {"a": "argon2"},  from argon2 import PasswordHasher → {"PasswordHasher": "argon2.PasswordHasher"}
    # 같은 폴더에서 가져오는 상대 import(from . import x)는 마지막 이름만 남긴다.
    table = {}
    for node in ast.walk(tree):  # ast.walk는 나무의 모든 가지를 하나씩 방문한다.
        if isinstance(node, ast.Import):
            for alias in node.names:
                table[alias.asname or alias.name.split('.')[0]] = alias.name if alias.asname else alias.name.split('.')[0]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ''
            for alias in node.names:
                table[alias.asname or alias.name] = f'{base}.{alias.name}' if base and not node.level else alias.name
    # import한 이름을 같은 파일 안에서 다시 정의했다면(예: from argon2 import PasswordHasher 다음에 class PasswordHasher: ...)
    # 그 이름이 진짜 라이브러리를 가리킨다고 믿을 수 없다. 그런 별칭은 표에서 뺀다.
    counts = bindings if bindings is not None else _name_bindings(tree)
    return {name: target for name, target in table.items() if counts.get(name, 0) == 1}


def qualname(node, aliases):
    # 코드의 "hasher.hash"나 "argon2.PasswordHasher" 같은 점(.)으로 이어진 이름을 하나의 글자로 바꿔 돌려준다.
    # import 별칭은 실제 이름으로 되돌린다. 이런 모양이 아니면(예: 계산식) None을 돌려준다.
    parts = []
    while isinstance(node, ast.Attribute):  # a.b.c 모양이면 c, b 순서로 모은 뒤 가장 왼쪽(a)으로 올라간다.
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Call):  # 함수 호출 결과에 이어 쓴 경우: logging.getLogger(...).info 처럼
        inner = qualname(node.func, aliases)
        return None if inner is None else '.'.join([inner, *reversed(parts)])
    if not isinstance(node, ast.Name):
        return None
    return '.'.join([aliases.get(node.id, node.id), *reversed(parts)])


def terminal_name(node):
    # 코드 조각의 "마지막 이름"을 돌려준다. 예) user.password → "password", password → "password", data["password"] → "password"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return node.slice.value
    return None


# 받은 값을 글자 그대로 옮기거나 다른 모양으로 바꿔 돌려주는 함수·메서드들. 이런 호출의 결과에는 받은 값이 그대로 들어 있다.
PASSTHROUGH_FUNCTIONS = frozenset({'str', 'repr', 'format', 'dict', 'list', 'tuple', 'set', 'frozenset', 'bytes',
                                   'bytearray', 'sorted', 'reversed', 'vars', 'copy', 'deepcopy', 'dumps'})
PASSTHROUGH_METHODS = frozenset({'format', 'join', 'encode', 'decode', 'strip', 'lstrip', 'rstrip', 'lower', 'upper',
                                 'replace', 'copy', 'items', 'values', 'keys', 'get', 'dumps', 'getvalue'})


def _passes_text_through(func):
    # 이 함수(또는 메서드)는 받은 값을 그대로 담아 돌려주는 종류인가? 예) str(x), "...".format(x), json.dumps(x)
    if isinstance(func, ast.Name):
        return func.id in PASSTHROUGH_FUNCTIONS
    if isinstance(func, ast.Attribute):
        return func.attr in PASSTHROUGH_METHODS or func.attr in PASSTHROUGH_FUNCTIONS
    return False


def sensitive_refs(node, matcher, sensitive_vars=frozenset(), strict=False):
    # 어떤 코드 조각(node) 안에서 "비밀번호 값을 사용하는 곳"을 찾아 이름 목록으로 돌려준다. 없으면 빈 목록이다.
    # 아래 세 가지는 비밀번호를 쓴 것으로 세지 않는다.
    #   - 호출하는 함수의 이름 자체  - 따옴표 안의 글자(문자열 상수)  - len() 같이 길이·형식만 알려 주는 함수
    # sensitive_vars: "비밀번호 필드를 가진 객체"를 담은 변수 이름들이다. 이런 객체를 통째로 쓰거나
    #   model_dump()/__dict__로 펼치면 비밀번호도 함께 나가므로 노출로 본다.
    #   (payload.email처럼 다른 필드만 쓰는 것은 괜찮다.)
    # strict=True는 "이 값 안에 비밀번호 글자가 그대로 들어 있는가"를 볼 때 쓴다(변수에 담긴 값이 비밀번호를 품는지 따라가는 용도).
    #   모르는 함수의 결과(예: hash_password(...), User(...)), 비교(==, >)와 not의 결과(True/False), 조건식의 조건 부분은
    #   비밀번호 글자를 담고 있다고 보지 않는다. 그렇게 보면 정상 코드가 불필요하게 FAIL이 되기 때문이다.
    #   단, and·or는 True/False가 아니라 피연산자 값을 그대로 돌려주므로(payload.password or "") 값을 따라간다.
    found, stack = [], [node]  # stack = 아직 살펴보지 않은 코드 조각들을 쌓아 둔 목록
    while stack:
        item = stack.pop()
        if strict and (isinstance(item, ast.Compare) or (isinstance(item, ast.UnaryOp) and isinstance(item.op, ast.Not))):
            continue  # 비교와 not은 True/False만 만든다. (and·or는 피연산자 값이 그대로 나오므로 여기서 건너뛰지 않는다.)
        if strict and isinstance(item, ast.IfExp):
            stack.extend([item.body, item.orelse])  # 값이 되는 쪽만 본다(조건 부분은 값이 아니다).
            continue
        if isinstance(item, ast.Subscript) and isinstance(item.value, ast.Name) and item.value.id in sensitive_vars:
            # data["email"]처럼 사전 항목을 꺼내는 경우: 비밀번호 같은 키이거나 키를 알 수 없을 때만 노출로 본다.
            key = item.slice.value if isinstance(item.slice, ast.Constant) and isinstance(item.slice.value, str) else None
            if key is None or matcher.search(key):
                found.append(item.value.id)
            continue
        if isinstance(item, ast.Call):  # 함수 호출
            func = item.func
            if isinstance(func, ast.Name) and func.id in SAFE_CALLS:
                continue  # len(password) 같은 호출은 안쪽을 보지 않고 건너뛴다.
            if isinstance(func, ast.Name) and func.id in sensitive_vars:
                found.append(func.id)  # getter = lambda: payload.password 처럼 비밀번호를 돌려주는 함수를 담은 변수를 부름
                continue
            if isinstance(func, ast.Name) and func.id in ('locals', 'vars') and not item.args and sensitive_vars:
                found.append(func.id)  # locals()는 지금 함수의 모든 변수(비밀번호를 품은 변수 포함)를 한꺼번에 꺼낸다.
                continue
            if (isinstance(func, ast.Attribute) and func.attr in DUMP_METHODS
                    and isinstance(func.value, ast.Name) and func.value.id in sensitive_vars):
                found.append(func.value.id)  # payload.model_dump() → 비밀번호를 포함한 전체 필드를 꺼내는 것
                continue
            if (isinstance(func, ast.Attribute) and func.attr == 'get' and isinstance(func.value, ast.Name)
                    and func.value.id in sensitive_vars and item.args and isinstance(item.args[0], ast.Constant)
                    and isinstance(item.args[0].value, str)):
                if matcher.search(item.args[0].value):
                    found.append(func.value.id)  # data.get("password")
                continue  # data.get("email")은 비밀번호가 아니다.
            if isinstance(func, ast.Attribute) and func.attr == 'keys' and not item.args:
                continue  # data.keys()는 항목 이름 목록일 뿐 값(비밀번호)이 아니다.
            if strict and not _passes_text_through(func):
                continue  # 모르는 함수의 결과는 비밀번호 글자 그대로라고 볼 수 없다.
            stack.extend(item.args)  # 호출에 넘긴 값들도 살펴본다.
            stack.extend(keyword.value for keyword in item.keywords)
            if isinstance(func, ast.Attribute):
                stack.append(func.value)
            continue
        if isinstance(item, ast.Name) and item.id in sensitive_vars:
            found.append(item.id)  # 비밀번호 필드를 가진 객체 자체를 그대로 사용
            continue
        if isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name) and item.value.id in sensitive_vars:
            # payload.xxx 모양: 비밀번호 필드(또는 __dict__)일 때만 노출로 본다.
            if item.attr == '__dict__' or matcher.search(item.attr):
                found.append(item.attr)
            continue
        name = terminal_name(item) if isinstance(item, (ast.Name, ast.Attribute, ast.Subscript)) else None
        if name and matcher.search(name):
            if strict and name.isupper():
                continue  # Status.PASS처럼 전부 대문자인 이름은 상수·열거값이다. 사용자가 보낸 비밀번호 값을 담고 있지 않다.
            found.append(name)  # 이름이 비밀번호처럼 보이는 값을 사용
            continue
        if isinstance(item, ast.Constant):
            continue  # 따옴표 안의 글자·숫자는 변수가 아니다.
        stack.extend(ast.iter_child_nodes(item))  # 그 밖의 모양은 안쪽 가지를 계속 살펴본다.
    return found


def _int_value(node, constants):
    # 코드 조각이 "숫자"로 정해진 값이면 그 숫자를 돌려준다. 숫자를 직접 썼거나, 파일 맨 위에 정해 둔 숫자 변수일 때만 해당한다.
    # 계산이 필요하거나 환경 변수에서 읽는 값처럼 코드만 봐서는 알 수 없는 값은 None이다.
    if isinstance(node, ast.Constant) and type(node.value) is int:
        return node.value
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    return None


def _module_constants(tree, bindings=None):
    # 파일 맨 위(함수 밖)에서 "이름 = 정수"로 정해 둔 상수들을 {이름: 값} 사전으로 모은다. 예) MEMORY = 19456
    # 이름이 파일 안에서 "딱 한 번만" 정의된 상수만 인정한다. 나중에 다시 대입하거나 함수 안에서 같은 이름을 쓰면,
    # 해시 설정을 읽는 시점의 값이 무엇인지 코드만으로 알 수 없기 때문이다.
    table = {}
    counts = bindings if bindings is not None else _name_bindings(tree)
    for statement in getattr(tree, 'body', []):  # body = 파일의 최상위 문장들
        if (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Constant) and type(statement.value.value) is int
                and counts.get(statement.targets[0].id, 0) == 1):
            table[statement.targets[0].id] = statement.value.value
    return table


def _calls(modules):
    # 모든 파일에서 "함수 호출" 코드를 하나씩 꺼내 (파일, 호출) 쌍으로 내보낸다. (yield = 하나씩 내보내는 함수)
    for module in modules.values():
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call):
                yield module, node


def _functions(modules):
    # 모든 파일에서 "함수를 정의한 곳"을 {함수 이름: [(파일, 정의), ...]} 사전으로 모은다.
    # (async def로 만든 비동기 함수도 포함한다. 같은 이름의 함수가 여러 개일 수 있어 목록으로 담는다.)
    table = {}
    for module in modules.values():
        for node in ast.walk(module.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                table.setdefault(node.name, []).append((module, node))
    return table


def _untrusted_function_names(modules):
    # "이 이름의 함수를 부르면 소스에 적힌 그 함수가 실행된다"고 믿을 수 없는 함수 이름들을 찾는다.
    # 변수 이름처럼 함수 이름도 다른 것으로 바뀔 수 있다. 아래 중 하나라도 있으면 그 이름을 믿지 않는다.
    #   · 같은 이름에 함수 정의가 아닌 값이 묶인다. 예) hash_password = lambda v: v / 같은 이름의 매개변수 / with ... as hash_password
    #   · 같은 이름을 두 번 이상 가져오거나(import), 가져온 이름을 같은 파일에서 다시 정의한다. (어느 쪽이 쓰이는지 알 수 없다)
    #   · 함수 위에 데코레이터가 붙어 있다. (staticmethod·classmethod는 함수를 바꾸지 않으므로 제외)
    #     데코레이터는 정의된 함수를 전혀 다른 것으로 바꿔 놓을 수 있다.
    #   · 다른 객체의 같은 이름 속성에 값을 넣는다. 예) security.hash_password = ..., setattr(security, "hash_password", ...)
    defined, untrusted, assigned = set(), set(), set()
    per_module = []  # (이름별 묶임 횟수, 이름별 함수 정의 수, 이름별 import 수)
    for module in modules.values():
        defs, imports = {}, {}
        for node in ast.walk(module.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defs[node.name] = defs.get(node.name, 0) + 1
                defined.add(node.name)
                if any(not (isinstance(item, ast.Name) and item.id in ('staticmethod', 'classmethod'))
                       for item in node.decorator_list):
                    untrusted.add(node.name)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.asname in (None, alias.name):  # 이름을 바꾸지 않고 그대로 가져온 것만 센다
                        imports[alias.name] = imports.get(alias.name, 0) + 1
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                assigned.add(node.attr)
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('setattr', 'delattr')
                  and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)):
                assigned.add(node.args[1].value)
        per_module.append((module.bindings, defs, imports))
    for counts, defs, imports in per_module:
        for name in defined:
            others = counts.get(name, 0) - defs.get(name, 0) - imports.get(name, 0)  # 정의·import가 아닌 다른 묶임
            if others > 0 or imports.get(name, 0) > 1 or (imports.get(name, 0) and defs.get(name, 0)):
                untrusted.add(name)
    return untrusted | (assigned & defined)


def _obs(module, node, kind, **detail):
    # 근거 기록(Observation)을 쉽게 만드는 도우미. node의 줄 번호(lineno)를 읽어 기록한다. 줄 번호가 없으면 0이다.
    return Observation(module.path, getattr(node, 'lineno', 0), kind, detail)


# ---------------------------------------------------------------- REQ-005 비밀번호 저장

def check_password_storage(modules, criteria, policy):
    # REQ-005: 비밀번호를 Argon2id로 해시해서 저장하는가?
    #   modules  : 분석할 소스 파일들
    #   criteria : 판정 기준(JSON). 어떤 함수가 해시 도구이고 어떤 해시가 금지인지 등이 들어 있다.
    #   policy   : 시나리오가 요구하는 해시 설정(알고리즘, 반복 횟수, 메모리 크기, 병렬도)
    # 과정: ① 해시 도구를 만드는 코드와 설정 숫자를 찾는다 ② 비밀번호가 어디에 저장되는지 찾아 원문인지 해시인지 구분한다
    #       ③ 위반(약한 해시, 원문 저장, 설정 미달)이 있으면 FAIL, 확인하지 못한 부분이 있으면 UNVERIFIED, 다 맞으면 PASS.
    rules = criteria['hashPolicy']
    matcher = _sensitive(criteria)
    defaults = rules['libraryDefaults']  # 라이브러리를 설정 없이 쓸 때의 기본 숫자
    # fails=위반, unresolved=확인 못 한 부분, configs=찾은 해시 도구 설정, stored=해시로 저장하는 것을 확인한 곳
    fails, unresolved, configs, stored = [], [], [], []
    if not isinstance(policy, dict) or str(policy.get('algorithm', '')).lower() != 'argon2id':
        return CheckOutcome(UNVERIFIED, 'POLICY_NOT_ARGON2ID')  # 시나리오 정책이 Argon2id가 아니면 이 검사기로 판정할 수 없다.
    # 정책이 요구하는 최소 설정: time_cost(반복 횟수), memory_cost(메모리 KiB), parallelism(병렬도)
    required = {'time_cost': policy.get('iterations'), 'memory_cost': policy.get('memoryKiB'),
                'parallelism': policy.get('parallelism')}
    if any(type(value) is not int for value in required.values()):
        return CheckOutcome(UNVERIFIED, 'POLICY_PARAMETERS_MISSING')
    orm_classes = _orm_classes(modules)   # DB 테이블을 나타내는 클래스 이름들(User 등)
    # 해시 경로를 판정할 때 쓰는 확인된 사실들: 믿을 수 있는 프로젝트 함수(재대입·데코레이터 등으로 바뀔 수 있는 함수는 뺀다),
    # 승인한 Argon2 해시 도구를 담은 변수 이름, 진짜 무작위 값을 만드는 호출 등
    ctx = _hash_context(modules, rules)
    project_names = _project_names(modules)
    passlib_using = {f'{handler}.using' for handler in rules.get('passlibHandlers', ())}
    passlib_approved = tuple(f'{handler}.' for handler in rules.get('passlibHandlers', ()))  # passlib.hash.argon2.hash 등
    bad_contexts = _unapproved_contexts(modules, rules)  # bcrypt 등을 기본으로 쓰는 CryptContext 이름들
    module_constants = {path: _module_constants(module.tree, module.bindings) for path, module in modules.items()}  # 파일별 상수표
    module_literals = {path: _module_literal_names(module.tree, module.bindings) for path, module in modules.items()}  # 파일별 "고정 글자" 상수표
    enclosing = {path: _enclosing_functions(module.tree) for path, module in modules.items()}  # 코드 조각 → 그것이 든 함수
    for module, call in _calls(modules):
        name = qualname(call.func, module.aliases) or ''  # 부르는 함수의 전체 이름. 예) "argon2.PasswordHasher"
        constants = module_constants[module.path]
        if name in rules['constructors']:
            # Argon2 해시 도구를 만드는 호출: 설정 숫자를 읽어 둔다.
            params, failed = _bind_parameters(call, rules['constructors'][name], defaults, constants, module, rules)
            configs.append((module, call, params))
            fails.extend(failed)
        elif name.endswith('PasswordHasher.from_parameters') and name.startswith('argon2'):
            # 미리 만들어진 "프로필"(권장 설정 묶음)로 만드는 호출
            profile = qualname(call.args[0], module.aliases) if call.args else None
            configs.append((module, call, dict(rules['profiles'][profile]) if profile in rules['profiles'] else None))
        elif name in rules['defaultFactories']:
            configs.append((module, call, dict(defaults)))  # 라이브러리 기본 설정을 쓰는 호출
        elif name in passlib_using and name.split('.')[0] not in project_names:
            # passlib: argon2.using(memory_cost=..., rounds=...) 로 설정을 정한 Argon2 해시 도구
            params, failed = _passlib_parameters(call, 'using', defaults, constants, module, rules)
            configs.append((module, call, params))
            fails.extend(failed)
        elif name in rules.get('passlibContexts', ()) and name.split('.')[0] not in project_names \
                and _passlib_scheme(call) == 'argon2':
            # passlib: CryptContext(schemes=["argon2"], argon2__memory_cost=...) — 기본 방식이 argon2인 컨텍스트
            params, failed = _passlib_parameters(call, 'context', defaults, constants, module, rules)
            configs.append((module, call, params))
            fails.extend(failed)
        elif name.startswith(passlib_approved) and name.endswith('.hash') and isinstance(call.func, ast.Attribute) \
                and isinstance(call.func.value, ast.Name) and _hasher_reference(call.func.value, ctx):
            configs.append((module, call, dict(defaults)))  # from passlib.hash import argon2 → argon2.hash(...): 기본 설정
        elif name in rules['forbiddenHashCalls'] and sensitive_refs(call, matcher):
            # md5·sha256처럼 비밀번호 저장에 쓰면 안 되는 빠른 해시에 비밀번호를 넣었다 → 위반
            fails.append(_obs(module, call, 'WEAK_HASH_OF_PASSWORD', function=name))
        elif name.startswith(tuple(rules['unapprovedHasherPrefixes'])) and not name.startswith(passlib_approved) \
                and sensitive_refs(call, matcher):
            # 승인되지 않은 해시 라이브러리(예: bcrypt, passlib의 argon2 외 방식)에 비밀번호를 넣었다 → 위반
            fails.append(_obs(module, call, 'UNAPPROVED_HASHER', function=name))
        if isinstance(call.func, ast.Attribute) and call.func.attr == 'hash' and sensitive_refs(call, matcher):
            scheme = next((keyword.value for keyword in call.keywords if keyword.arg == 'scheme'), None)
            if terminal_name(call.func.value) in bad_contexts:
                # CryptContext(schemes=["bcrypt"]) 처럼 argon2가 아닌 방식이 기본인 컨텍스트로 비밀번호를 해시했다 → 위반
                fails.append(_obs(module, call, 'UNAPPROVED_HASHER', function='passlib.context.CryptContext'))
            elif _hasher_reference(call.func.value, ctx) and isinstance(scheme, ast.Constant) and scheme.value != 'argon2':
                fails.append(_obs(module, call, 'UNAPPROVED_HASHER', function='scheme=' + str(scheme.value)))
        # 승인한 해시 도구의 hash()에 고정된 salt를 넘기면, 같은 비밀번호가 늘 같은 해시가 되어 salt의 의미가 없다 → 위반
        if isinstance(call.func, ast.Attribute) and call.func.attr == 'hash' and _hasher_reference(call.func.value, ctx):
            salt = _salt_argument(call)
            if policy.get('uniqueSalt', True) and salt is not None and _is_fixed_value(salt, module_literals[module.path]):
                fails.append(_obs(module, call, 'FIXED_SALT'))  # 시나리오 정책이 "고유 salt"(uniqueSalt)를 요구한다.
        # insert(User).values(password_hash=...) / update(User).values(...) 처럼 .values()로 칸 값을 정하는 호출
        if isinstance(call.func, ast.Attribute) and call.func.attr == 'values':
            for keyword in call.keywords:
                if keyword.arg in rules['storageFieldNames']:
                    _classify_storage(module, keyword.value, keyword.arg, ctx, rules, matcher, fails, stored, unresolved,
                                      enclosing[module.path].get(id(call)))
        # db.execute("UPDATE users SET password_hash = ?", (값,)) 처럼 SQL 글자로 저장 칸을 채우는 호출.
        # SQL에 저장 칸 이름이 있으면, 함께 넘긴 값 중 해시가 아닌 비밀번호가 있는지 본다.
        if terminal_name(call.func) in ('execute', 'executemany', 'exec_driver_sql') and call.args \
                and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str) \
                and any(re.search(r'\b%s\b' % re.escape(column), call.args[0].value.lower()) for column in rules['storageFieldNames']
                        if column != 'password'):
            leaves = []
            for argument in [*call.args[1:], *(keyword.value for keyword in call.keywords)]:
                if isinstance(argument, (ast.Tuple, ast.List)):
                    leaves.extend(argument.elts)
                elif isinstance(argument, ast.Dict):
                    leaves.extend(argument.values)
                else:
                    leaves.append(argument)
            scope = enclosing[module.path].get(id(call))
            for leaf in leaves:
                if isinstance(leaf, ast.Call) and _is_hash_call(leaf, ctx, scope=scope, password_matcher=matcher):
                    stored.append(_obs(module, leaf, 'STORED_VIA_HASH', field='sql'))
                elif isinstance(leaf, (ast.Name, ast.Attribute, ast.Subscript)) and matcher.search(terminal_name(leaf) or '') \
                        and not _hash_like_name(terminal_name(leaf) or ''):
                    fails.append(_obs(module, leaf, 'PLAINTEXT_STORAGE', field='sql'))
        # setattr(user, "password_hash", 값) 으로 칸 값을 정하는 호출
        if isinstance(call.func, ast.Name) and call.func.id == 'setattr' and len(call.args) >= 3 \
                and isinstance(call.args[1], ast.Constant) and call.args[1].value in rules['storageFieldNames']:
            _classify_storage(module, call.args[2], call.args[1].value, ctx, rules, matcher, fails, stored, unresolved,
                              enclosing[module.path].get(id(call)))
        # DB 테이블 객체를 만드는 호출이면, 비밀번호 칸(password_hash 등)에 무엇을 넣는지 살펴본다. 예) User(password_hash=...)
        if isinstance(call.func, (ast.Name, ast.Attribute)) and terminal_name(call.func) in orm_classes:
            for keyword in call.keywords:
                if keyword.arg in rules['storageFieldNames']:
                    _classify_storage(module, keyword.value, keyword.arg, ctx, rules, matcher, fails, stored, unresolved,
                                      enclosing[module.path].get(id(call)))
                elif keyword.arg and isinstance(keyword.value, (ast.Name, ast.Attribute, ast.Subscript)) \
                        and matcher.search(terminal_name(keyword.value) or '') and not _hash_like_name(terminal_name(keyword.value) or ''):
                    # 칸 이름이 password_hash 같은 알려진 이름이 아니어도(예: User(pw=payload.password)) 비밀번호 원문을 넣으면 평문 저장이다.
                    # (넣는 값의 이름에 hash·digest가 들어 있으면 해시일 수 있어서 원문으로 단정하지 않는다.)
                    fails.append(_obs(module, keyword.value, 'PLAINTEXT_STORAGE', field=keyword.arg))
    # 객체의 칸에 값을 직접 대입하는 코드도 살펴본다. 예) user.password_hash = ...
    # 사전으로 칸 값을 정하는 코드도 같다. 예) row = {"password_hash": ...} / row["password_hash"] = ...
    # 사전의 "password"는 요청 본문의 칸일 수도 있어서, 해시·digest가 들어간 이름만 저장 칸으로 본다.
    # 사전은 응답을 만드는 데도 쓰이므로(예: {"password_hash": user.password_hash}) 위반(원문·상수 저장)과 해시 저장 확인만 기록하고,
    # 무엇인지 모르는 값은 "확인 못 함"으로 치지 않는다.
    hash_columns = {name for name in rules['storageFieldNames'] if 'hash' in name or 'digest' in name}
    for module in modules.values():
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value in hash_columns:
                        _classify_storage(module, value, key.value, ctx, rules, matcher, fails, stored, [],
                                          enclosing[module.path].get(id(node)))
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) \
                            and target.slice.value in hash_columns:
                        _classify_storage(module, node.value, target.slice.value, ctx, rules, matcher, fails, stored,
                                          [], enclosing[module.path].get(id(node)))
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr in rules['storageFieldNames'] and node.value is not None:
                        _classify_storage(module, node.value, target.attr, ctx, rules, matcher, fails, stored, unresolved,
                                          enclosing[module.path].get(id(node)))
    if fails:
        return CheckOutcome(FAIL, 'VIOLATION_FOUND', fails)
    if not configs:
        return CheckOutcome(UNVERIFIED, 'HASHER_CONFIG_NOT_FOUND', stored + unresolved)  # 해시 도구를 만드는 코드를 못 찾음
    # 찾은 해시 도구 설정 숫자가 정책의 최소 기준을 만족하는지 확인한다.
    observations = []
    for module, call, params in configs:
        if params is None:
            unresolved.append(_obs(module, call, 'HASHER_PARAMETERS_NOT_CONSTANT'))  # 숫자가 코드에 정해져 있지 않아 확인 불가
            continue
        observations.append(_obs(module, call, 'ARGON2ID_CONFIG', **params))
        for key, minimum in required.items():
            value = params[key]
            # AT_LEAST 규칙: 기준 "이상"이면 통과. 그 외(EXACT)는 기준과 "정확히 같아야" 통과.
            below = value < minimum if rules['parameterRule'] == 'AT_LEAST' else value != minimum
            if below:
                fails.append(_obs(module, call, 'HASH_PARAMETER_NOT_MET', parameter=key, actual=value,
                                  required=minimum, rule=rules['parameterRule']))
    if fails:
        return CheckOutcome(FAIL, 'VIOLATION_FOUND', fails)
    if unresolved:
        return CheckOutcome(UNVERIFIED, 'ANALYSIS_INCOMPLETE', unresolved)
    if not stored:
        # 해시 도구는 있는데, 그것으로 만든 값을 저장하는 곳을 찾지 못했다 → PASS라고 할 근거가 부족하다.
        return CheckOutcome(UNVERIFIED, 'HASH_STORAGE_PATH_NOT_FOUND', observations)
    return CheckOutcome(PASS, 'STATIC_ARGON2ID_STORAGE', observations + stored)


def _enclosing_functions(tree):
    # 코드 나무의 모든 조각이 "가장 가까운 바깥 함수"가 무엇인지 {조각의 id: 함수}로 정리한다. 함수 밖의 조각은 표에 없다.
    result, stack = {}, [(tree, None)]
    while stack:
        node, function = stack.pop()
        if function is not None:
            result[id(node)] = function
        inner = node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
        stack.extend((child, inner) for child in ast.iter_child_nodes(node))
    return result


def _module_literal_names(tree, bindings=None):
    # 파일 맨 위에서 "이름 = 따옴표 글자/바이트 글자"로 한 번만 정한 상수들의 이름을 모은다. 예) SALT = b"0123456789abcdef"
    names = set()
    counts = bindings if bindings is not None else _name_bindings(tree)
    for statement in getattr(tree, 'body', []):
        if (isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name)
                and _is_literal_expression(statement.value) and counts.get(statement.targets[0].id, 0) == 1):
            names.add(statement.targets[0].id)
    return names


def _is_literal_expression(node):
    # 이름·호출·속성 없이 글자나 숫자만으로 이루어진 식인가? 예) b"abc", b"ab" + b"cd", "x" * 16
    return not any(isinstance(item, (ast.Name, ast.Attribute, ast.Call, ast.Subscript)) for item in ast.walk(node))


def _is_fixed_value(node, literal_names):
    # 이 값이 코드에 고정으로 적힌 글자인가? 글자·바이트를 직접 썼거나, 파일 맨 위에 그렇게 정해 둔 상수 이름이면 True이다.
    if isinstance(node, ast.Name):
        return node.id in literal_names
    return _is_literal_expression(node)


def _bind_parameters(call, order, defaults, constants, module, rules):
    # 해시 도구를 만드는 호출(예: PasswordHasher(2, 19456, 1))에서 설정 숫자를 이름별로 읽어 낸다.
    # 위치로 넘긴 값(order 순서)과 이름을 붙여 넘긴 값(time_cost=2)을 모두 처리하고,
    # 쓰지 않은 값은 라이브러리 기본값으로 채운다. 숫자가 아니거나 코드로 알 수 없는 값이면 설정 전체를 None으로 돌려준다.
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(keyword.arg is None for keyword in call.keywords):
        # f(*값들)이나 f(**설정)으로 넘긴 값은 코드만 봐서는 어떤 값인지 알 수 없다.
        # 이 값을 "안 적었으니 라이브러리 기본값"으로 취급하면, 약한 설정이 안전한 기본값으로 둔갑한다. 그래서 확인 불가로 처리한다.
        return None, []
    given = dict(zip(order, call.args))  # 위치로 넘긴 값들을 이름과 짝지음
    given.update({keyword.arg: keyword.value for keyword in call.keywords if keyword.arg})  # 이름 붙여 넘긴 값들
    failed, params = [], {}
    if 'type' in given and qualname(given['type'], module.aliases) not in rules['argon2idTypes']:
        # Argon2에는 종류(i, d, id)가 있는데 id가 아닌 것을 지정했다 → 위반
        failed.append(_obs(module, call, 'NOT_ARGON2ID_TYPE'))
    if 'salt_len' in given:
        salt_len = _int_value(given['salt_len'], constants)
        if salt_len is not None and salt_len < MIN_SALT_BYTES:
            # 너무 짧은 salt는 서로 다른 사용자에게 고유하게 만들어 주지 못한다.
            failed.append(_obs(module, call, 'SALT_TOO_SHORT', actual=salt_len, required=MIN_SALT_BYTES))
    for key in ('time_cost', 'memory_cost', 'parallelism'):
        if key not in given:
            params[key] = defaults[key]  # 안 적었으면 라이브러리 기본값
            continue
        value = _int_value(given[key], constants)
        if value is None:
            return None, failed  # 숫자를 알 수 없으면 설정을 확정할 수 없다.
        params[key] = value
    return params, failed


def _classify_storage(module, value, field_name, ctx, rules, matcher, fails, stored, unresolved, scope=None):
    # "비밀번호 칸(field_name)에 저장하려는 값(value)"이 무엇인지 분류한다.
    #   원문 비밀번호 / 고정 글자 → fails(위반)
    #   해시 함수의 결과         → stored(안전하게 저장)
    #   판단할 수 없음           → unresolved(확인 못 함)
    if isinstance(value, ast.Name) and scope is not None and _is_hashed_variable(value.id, scope, ctx, matcher):
        # hashed = hash_password(payload.password) 다음에 password_hash=hashed 처럼, 해시 결과만 담은 변수를 저장하는 경우
        stored.append(_obs(module, value, 'STORED_VIA_HASH', field=field_name))
        return
    if isinstance(value, (ast.Name, ast.Attribute, ast.Subscript)):
        # 변수나 항목을 그대로 저장하는 경우. 이름이 비밀번호처럼 보이는데 hash·digest가 들어 있지 않으면 원문 저장이다(_hash_like_name).
        name = terminal_name(value) or ''
        if matcher.search(name) and not _hash_like_name(name):
            fails.append(_obs(module, value, 'PLAINTEXT_STORAGE', field=field_name))
        else:
            unresolved.append(_obs(module, value, 'STORED_VALUE_UNKNOWN', field=field_name))
        return
    if isinstance(value, ast.Constant):
        # 따옴표 글자 같은 고정 값을 저장 → 비밀번호를 해시한 값일 수 없다. (None은 "값 없음"이라 허용)
        if value.value is not None:
            fails.append(_obs(module, value, 'CONSTANT_STORED', field=field_name))
        return
    if isinstance(value, ast.Await):
        value = value.value  # await hash_async(...)는 호출 결과를 저장하는 것과 같으므로 await를 벗겨서 본다.
    if isinstance(value, ast.Call):
        if _is_hash_call(value, ctx, scope=scope, password_matcher=matcher):
            stored.append(_obs(module, value, 'STORED_VIA_HASH', field=field_name))
        else:
            unresolved.append(_obs(module, value, 'STORED_VALUE_UNKNOWN', field=field_name))
        return
    unresolved.append(_obs(module, value, 'STORED_VALUE_UNKNOWN', field=field_name))


def _hash_like_name(name):
    # 이름이 "해시를 담은 값"을 가리키는가? password_hash, hashed_password, password_digest 처럼 hash나 digest가 들어간 이름이다.
    # 이런 이름의 값은 원문이라고 단정하지 않는다(무엇인지 확인하지 못하면 UNVERIFIED). 기준의 저장 칸 이름 목록과 같은 기준이다.
    lowered = name.lower()
    return 'hash' in lowered or 'digest' in lowered


def _is_hashed_variable(name, scope, ctx, matcher):
    # 함수(scope) 안의 변수 하나가 "입력 비밀번호를 해시한 결과만" 담고 있는가?
    # 이 이름에 값이 묶이는 곳이 모두 `이름 = 해시 호출(...)` 형태의 일반 대입이고(매개변수·with·반복문·+= 같은 다른 묶임이 없고),
    # 그 호출이 모두 입력 비밀번호의 해시로 확인될 때만 True이다. 그러면 저장하는 자리가 어디든 이 변수에는 해시만 들어 있다.
    values, captured = [], 0
    for node in ast.walk(scope):
        for target, value in _assignment_pairs(node):
            hits = sum(1 for item in ast.walk(target) if isinstance(item, ast.Name) and item.id == name and isinstance(item.ctx, ast.Store))
            if hits:
                captured += hits
                values.append((target, value, node))
    if not values or _name_bindings(scope).get(name, 0) != captured:
        return False
    for target, value, node in values:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(target, ast.Name):
            return False
        call = value.value if isinstance(value, ast.Await) else value
        if not (isinstance(call, ast.Call) and _is_hash_call(call, ctx, scope=scope, password_matcher=matcher)):
            return False
    return True


def _is_approved_hasher_factory(call, module, rules, project_names=frozenset()):
    # 이 호출이 "승인한 Argon2 해시 도구를 만드는 호출"인가?
    #   - argon2.PasswordHasher(...), pwdlib의 Argon2Hasher(...) 같은 생성자
    #   - PasswordHasher.from_parameters(...), PasswordHash.recommended() 같은 기본 설정 도구
    #   - PasswordHash((Argon2Hasher(),)) 처럼 해시 도구를 감싸는 호출. 감싼 도구 중 "첫 번째"가 승인된 도구일 때만 인정한다.
    #     (pwdlib은 목록의 첫 번째 도구로 해시를 만들기 때문이다.)
    #   - passlib: argon2.using(...), CryptContext(schemes=["argon2", ...]) 처럼 기본 방식이 argon2인 컨텍스트
    # 프로젝트에 라이브러리와 같은 이름의 파일·폴더(argon2.py 등)가 있으면 진짜 라이브러리라고 믿을 수 없어 인정하지 않는다.
    name = qualname(call.func, module.aliases) or ''
    if name.split('.')[0] in project_names:
        return False
    if name in rules['constructors'] or name in rules['defaultFactories']:
        return True
    if name.endswith('PasswordHasher.from_parameters') and name.startswith('argon2'):
        return True
    if name in {f'{handler}.using' for handler in rules.get('passlibHandlers', ())}:
        return True
    if name in rules.get('passlibContexts', ()):
        return _passlib_scheme(call) == 'argon2'
    if name in rules.get('hasherWrappers', ()):
        first = call.args[0] if call.args else None
        if isinstance(first, (ast.Tuple, ast.List)) and first.elts and isinstance(first.elts[0], ast.Call):
            return _is_approved_hasher_factory(first.elts[0], module, rules, project_names)
    return False


def _passlib_scheme(call):
    # passlib CryptContext(...)가 새 해시에 쓰는 방식 이름. default=가 있으면 그것, 없으면 schemes의 첫 번째다
    # (schemes는 목록이나 "argon2,bcrypt" 같은 쉼표 글자). 글자로 확정할 수 없으면 None.
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(keyword.arg is None for keyword in call.keywords):
        return None
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    chosen = keywords.get('default')
    if chosen is None:
        schemes = keywords.get('schemes', call.args[0] if call.args else None)
        if isinstance(schemes, (ast.List, ast.Tuple)):
            chosen = schemes.elts[0] if schemes.elts else None
        elif isinstance(schemes, ast.Constant) and isinstance(schemes.value, str):
            return schemes.value.split(',')[0].strip() or None
    if isinstance(chosen, ast.Constant) and isinstance(chosen.value, str):
        return chosen.value.strip() or None
    return None


def _passlib_parameters(call, kind, defaults, constants, module, rules):
    # passlib Argon2의 설정 숫자를 읽는다. kind="using"이면 argon2.using(...)의 이름 붙은 인자,
    # kind="context"이면 CryptContext(...)의 "argon2__설정" 인자를 읽는다. 돌려주는 값: (설정 사전 또는 None, 위반 목록)
    #   rounds·time_cost·default_rounds → time_cost, memory_cost, parallelism (안 적으면 라이브러리 기본값)
    #   type이 id가 아니면 위반, salt를 정하면(모든 해시가 같은 salt) 위반, salt_size가 8바이트 미만이면 위반
    #   모르는 설정·숫자가 아닌 값·**설정·all__ 설정은 결과를 바꿀 수 있어서 확인 불가(None)
    if (kind == 'using' and call.args) or any(keyword.arg is None for keyword in call.keywords):
        return None, []  # 위치 인자·**설정은 어느 설정인지 확정할 수 없다.
    mapping, ignored = rules['passlibParameters'], set(rules['passlibIgnoredSettings'])
    given, failed = {}, []
    for keyword in call.keywords:
        key = keyword.arg
        if kind == 'context':
            if key.startswith('all__') or key.count('__') > 1:
                # category별 설정은 기본 argon2 설정을 덮어쓴다. 지원하지 않는 정책은 증명 불가다.
                return None, failed
            if not key.startswith('argon2__'):
                continue  # schemes·default·deprecated·다른 방식의 설정
            key = key[len('argon2__'):]
        if key in ('salt_size', 'salt_len'):
            size = _int_value(keyword.value, constants)
            if size is None:
                return None, failed
            if size < MIN_SALT_BYTES:
                failed.append(_obs(module, call, 'SALT_TOO_SHORT', actual=size, required=MIN_SALT_BYTES))
        elif key == 'salt':
            failed.append(_obs(module, call, 'FIXED_SALT'))  # 설정에 넣은 salt는 모든 비밀번호에 같은 값으로 쓰인다.
        elif key == 'type':
            value = keyword.value
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                return None, failed
            if value.value not in rules['passlibArgon2idTypes']:
                failed.append(_obs(module, call, 'NOT_ARGON2ID_TYPE'))
        elif key in mapping:
            if mapping[key] in given:
                return None, failed  # 같은 설정을 두 이름으로 적으면 어느 값이 쓰이는지 단정하지 않는다.
            value = _int_value(keyword.value, constants)
            if value is None:
                return None, failed
            given[mapping[key]] = value
        elif key not in ignored:
            return None, failed
    return {key: given.get(key, defaults[key]) for key in ('time_cost', 'memory_cost', 'parallelism')}, failed


def _hasher_reference(node, ctx):
    # X.hash(...)의 X가 승인한 해시 도구인가? 이름 하나(X)이면 클래스 속성을 뺀 묶임으로, 속성(self.X 등)이면 모든 묶임으로 따진다.
    if isinstance(node, ast.Name):
        return node.id in ctx.name_hashers
    return terminal_name(node) in ctx.hashers


def _approved_hash_call(call, ctx):
    # call이 승인한 해시 도구의 hash() 호출인가? passlib 컨텍스트에 scheme=으로 다른 방식을 고르면 아니다.
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == 'hash' and _hasher_reference(call.func.value, ctx)):
        return False
    # category 또는 펼친 키워드가 실제 정책을 바꿀 수 있으므로 기본 정책으로 승인하지 않는다.
    if any(keyword.arg in (None, 'category') for keyword in call.keywords):
        return False
    scheme = next((keyword.value for keyword in call.keywords if keyword.arg == 'scheme'), None)
    return scheme is None or (isinstance(scheme, ast.Constant) and scheme.value == 'argon2')


def _hashed_argument(call):
    # hash() 호출에서 해시하는 값. 첫 번째 인자 또는 password=(argon2·pwdlib)·secret=(passlib)으로 넘긴 값.
    if call.args:
        return call.args[0]
    return next((keyword.value for keyword in call.keywords if keyword.arg in ('password', 'secret')), None)


def _unapproved_contexts(modules, rules):
    # 묶이는 곳이 모두 "argon2가 아닌 방식(bcrypt 등)을 기본으로 쓰는 passlib CryptContext"인 이름들.
    # 이런 이름의 hash()에 비밀번호를 넣으면 승인하지 않은 해시로 저장하는 것이다.
    contexts = set(rules.get('passlibContexts', ()))
    flags = {}
    for module in modules.values():
        for node in ast.walk(module.tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and getattr(node, 'value', None) is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                bad = (isinstance(value, ast.Call) and qualname(value.func, module.aliases) in contexts
                       and _passlib_scheme(value) not in (None, 'argon2'))
                for target in targets:
                    if isinstance(target, (ast.Name, ast.Attribute)):
                        flags.setdefault(terminal_name(target), []).append(bad)
            elif isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Store):
                flags.setdefault(terminal_name(node), [])
    # 대입이 아닌 묶임(for·with·매개변수 등)이 있으면 무엇을 가리킬지 몰라서 위반으로 단정하지 않는다.
    return {name for name, values in flags.items() if values and all(values)
            and _store_count(modules, name) == len(values)}


def _store_count(modules, name):
    # 모든 파일에서 name(또는 .name)에 값이 묶이는 곳의 수(대입·for·with 등의 Store, 매개변수, 정의).
    count = 0
    for module in modules.values():
        for node in ast.walk(module.tree):
            if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Store) and terminal_name(node) == name:
                count += 1
            elif (isinstance(node, ast.arg) and node.arg == name) or (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name):
                count += 1
    return count


TAMPER_DUNDERS = frozenset({'__class__', '__dict__', '__getattribute__', '__getattr__', '__setattr__', '__bases__'})  # 이 속성에 대입하면 객체의 동작이 통째로 바뀐다
NAMESPACE_WRITERS = frozenset({'update', 'setdefault', 'pop', 'popitem', 'clear'})


def _namespace_view(node):
    # obj.__dict__ / vars(obj) / globals() / locals() 처럼 이름 공간을 사전처럼 드러내는 식이면 (종류, 그 이름 공간의 주인 식)을 돌려준다. 아니면 None.
    # 종류: "object"(객체의 이름 공간) 또는 "globals"(모듈 전체의 이름 공간)
    if isinstance(node, ast.Attribute) and node.attr == '__dict__':
        return 'object', node.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ('vars', 'globals', 'locals'):
        return ('globals', None) if node.func.id != 'vars' else (('object', node.args[0]) if len(node.args) == 1 else ('object', None))
    return None


def _tampering(modules, names):
    # 소스 안에 names(예: hash, urandom)로 불리는 속성·항목을 덮어쓰는 코드가 있는가? 라이브러리의 함수를 가짜로 바꿔치기하는 방법을 모두 찾는다.
    #   · 속성 대입: PasswordHasher.hash = ..., 객체의 __class__·__dict__·__getattribute__ 같은 동작을 통째로 바꾸는 속성에 대입
    #   · setattr(X, "hash", ...), object.__setattr__(X, "hash", ...), X.__setattr__("hash", ...), delattr
    #   · 이름 공간 쓰기: X.__dict__["hash"] = ..., vars(X).update(hash=...), ns = vars(X); ns["hash"] = ...,
    #     dict.update(vars(X), ...), operator.setitem(vars(X), "hash", ...), globals()[...] = ...
    # 이름 공간에 쓰면서 어느 항목인지 알 수 없는 경우(update(다른_사전), 변수 키)도 바꿔치기일 수 있어서 포함한다.
    # 다만 자기 자신의 이름 공간을 채우는 흔한 관용구(self.__dict__.update(kwargs))는 라이브러리 객체를 바꾸는 것이 아니라서 제외한다.
    for module in modules.values():
        views = {}  # 이름 공간을 담은 변수 {이름: (종류, 주인 식)}   예) ns = vars(HASHER)
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Assign) and _namespace_view(node.value):
                views.update({target.id: _namespace_view(node.value) for target in node.targets if isinstance(target, ast.Name)})

        def view_of(expr):
            return _namespace_view(expr) or (views.get(expr.id) if isinstance(expr, ast.Name) else None)

        def own(view):
            # 자기 자신(self·cls)의 이름 공간인가?
            owner = view[1]
            return isinstance(owner, ast.Name) and owner.id in ('self', 'cls')

        def key_is_named(key):
            return isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value in names

        def key_is_known(key):
            return isinstance(key, ast.Constant) and isinstance(key.value, str)

        for node in ast.walk(module.tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)) and (node.attr in names or node.attr in TAMPER_DUNDERS):
                return True
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
                view = view_of(node.value)
                if view and (view[0] == 'globals' or key_is_named(node.slice) or not (key_is_known(node.slice) or own(view))):
                    return True
            if not isinstance(node, ast.Call):
                continue
            func, args = node.func, node.args
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
            if name in ('setattr', 'delattr', '__setattr__', '__delattr__') and any(
                    isinstance(argument, ast.Constant) and argument.value in names for argument in args[:2]):
                return True
            if not (isinstance(func, ast.Attribute) and func.attr in NAMESPACE_WRITERS | {'setitem', 'delitem'}):
                continue
            # 이름 공간에 쓰는 호출: 받는 쪽이 이름 공간이면 vars(X).update(...), 클래스·모듈 함수 형태이면 dict.update(vars(X), ...)·operator.setitem(vars(X), ...)
            if view_of(func.value):
                view, rest = view_of(func.value), args
            elif isinstance(func.value, ast.Name) and func.value.id in ('dict', 'operator') and args and view_of(args[0]):
                view, rest = view_of(args[0]), args[1:]
            else:
                continue
            if view[0] == 'globals':
                return True
            if func.attr in ('setdefault', 'pop', 'setitem', 'delitem'):
                if rest and key_is_named(rest[0]):
                    return True
                if not (rest and key_is_known(rest[0])) and not own(view):
                    return True
            elif func.attr == 'update':
                keys = _updated_keys(node, len(args) - len(rest))
                if keys is None:
                    if not own(view):
                        return True
                elif any(key in names for key in keys):
                    return True
            elif not own(view):  # popitem, clear
                return True
    return False


def _library_patched(modules, rules):
    # 해시 라이브러리의 클래스·메서드를 소스 안에서 덮어쓰는지 확인한다. 예) PasswordHasher.hash = lambda self, v: v
    # 덮어썼다면 PasswordHasher(...)로 만든 객체라도 .hash()가 진짜 해시를 만든다고 믿을 수 없다.
    symbols = {'hash', 'from_parameters', 'using'} | {
        name.rsplit('.', 1)[-1] for name in [*rules['constructors'], *rules['defaultFactories'], *rules.get('hasherWrappers', ()),
                                             *rules.get('passlibHandlers', ()), *rules.get('passlibContexts', ())]}
    return _tampering(modules, symbols)


def _known_hashers(modules, rules):
    # 승인한 해시 도구를 담고 있는 변수·속성의 "이름"들을 찾는다. 예) HASHER = PasswordHasher(...) 의 HASHER, self.hasher = ... 의 hasher
    # 이름에 값이 묶이는(바인딩) 곳을 모두 따져서, "그 이름에 묶인 값이 전부 승인한 해시 도구"일 때만 믿는다.
    #   - 승인하지 않은 값을 대입하거나, 함수 매개변수로 받거나(def f(HASHER=...)), for·with·import as 등으로 다시 묶으면
    #     그 이름이 어떤 객체를 가리키는지 알 수 없으므로 믿지 않는다.
    #   - 라이브러리 자체를 덮어쓴 흔적이 있으면 아무 이름도 믿지 않는다.
    # (이름만 보는 방식이라 파일은 구분하지 않는다. 같은 이름이 다른 곳에서 다른 값으로 쓰이면 믿지 않는 쪽으로 기운다.)
    # 돌려주는 값: (속성으로 부를 때 믿는 이름들, 이름 하나로 부를 때 믿는 이름들)
    #   클래스 본문에서 바로 정한 이름(클래스 속성, 예: SQLAlchemy 모델의 password_hash = Column(...))은 함수 안의
    #   `password_hash.hash(...)`가 가리킬 수 없다(함수 안에서 이름 하나는 클래스 속성을 찾지 않는다). 그래서 이름 하나로
    #   부르는 경우에는 클래스 속성 정의를 빼고 따진다. self.hasher처럼 속성으로 부르면 클래스 속성도 그대로 따진다.
    #   passlib의 Argon2(from passlib.hash import argon2)는 import 자체가 승인 해시 도구를 묶는 것으로 본다.
    if _library_patched(modules, rules):
        return set(), set()
    project_names = _project_names(modules)
    handlers = {name.rsplit('.', 1)[0]: name.rsplit('.', 1)[1] for name in rules.get('passlibHandlers', ())}
    flags, name_flags = {}, {}  # {이름: [묶일 때마다 "승인 해시 도구를 만든 대입인가"]}
    class_level = set()
    for module in modules.values():
        class_level |= _class_level_nodes(module.tree)

    def mark(node, name, approved):
        flags.setdefault(name, []).append(approved)
        if id(node) not in class_level:
            name_flags.setdefault(name, []).append(approved)

    for module in modules.values():
        covered = set()  # 승인 여부를 이미 따진 대입 대상 노드
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ImportFrom) and not node.level and node.module in handlers \
                    and node.module.split('.')[0] not in project_names:
                for alias in node.names:
                    bound = alias.asname or alias.name
                    if alias.name == handlers[node.module] and module.aliases.get(bound) == f'{node.module}.{alias.name}':
                        covered.add(id(alias))
                        mark(alias, bound, True)  # from passlib.hash import argon2: 기본 설정의 Argon2 해시 도구
                continue
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            else:
                continue
            approved = isinstance(value, ast.Call) and _is_approved_hasher_factory(value, module, rules, project_names)
            for target in targets:
                if isinstance(target, (ast.Name, ast.Attribute)):
                    covered.add(id(target))
                    mark(target, terminal_name(target), approved)
        for node in ast.walk(module.tree):
            # 위에서 따지지 않은 나머지 모든 "이름이 묶이는 곳"은 승인 도구를 만든 것이 아니다.
            if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Store) and id(node) not in covered:
                mark(node, terminal_name(node), False)    # +=, 튜플 풀기, for·with 변수, := 등
            elif isinstance(node, ast.arg):
                mark(node, node.arg, False)               # 함수·람다의 매개변수
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                mark(node, node.name, False)              # 같은 이름의 함수·클래스 정의
            elif isinstance(node, ast.ExceptHandler) and node.name:
                mark(node, node.name, False)
            elif isinstance(node, ast.alias) and node.asname and node.asname != node.name and id(node) not in covered:
                mark(node, node.asname, False)            # import ... as 이름 (같은 이름으로 가져오는 것은 정의한 쪽의 값과 같다)
    return ({name for name, values in flags.items() if all(values)},
            {name for name, values in name_flags.items() if all(values)})


def _class_level_nodes(tree):
    # 클래스 본문에서 바로 이름을 묶는 노드들(클래스 속성 대입, 메서드·안쪽 클래스 정의, 클래스 안의 import)의 id.
    # 안쪽 함수·람다·내포(comprehension)의 내부는 클래스 속성이 아니므로 들어가지 않는다.
    # 클래스 본문에서 global·nonlocal로 선언한 이름은 바깥 이름을 바꾸므로 클래스 속성으로 치지 않는다.
    found = set()
    for cls in (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)):
        body, stack = [], list(cls.body)
        while stack:
            node = stack.pop()
            body.append(node)
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
                                     ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                stack.extend(ast.iter_child_nodes(node))
        declared = {name for node in body if isinstance(node, (ast.Global, ast.Nonlocal)) for name in node.names}
        for node in body:
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id not in declared:
                found.add(id(node))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name not in declared:
                found.add(id(node))
            elif isinstance(node, ast.alias) and (node.asname or node.name.split('.')[0]) not in declared:
                found.add(id(node))
    return found


@dataclass
class HashContext:
    # 해시 경로를 판정하는 여러 함수가 함께 쓰는 "코드를 읽어서 확인한 사실"들을 한데 묶은 상자이다.
    functions: dict          # 이름이 다른 것으로 바뀔 염려가 없는 프로젝트 함수들 {이름: [(파일, 정의), ...]}
    hashers: set             # 승인한 해시 도구를 담은 이름들(self.hasher 같은 속성으로 부를 때. 모든 묶임으로 따짐)
    random_calls: set        # 진짜 운영체제 무작위 값(os.urandom 등)을 만드는 것으로 확인된 호출 조각의 id
    preserving_calls: dict   # 입력을 통째로 간직한 채 모양만 바꾸는 것으로 확인된 호출 조각의 id → 값이 들어 있는 인자의 위치
    defined_names: set       # 프로젝트가 직접 정의한 함수·메서드 이름들 (strip 같은 이름을 직접 만들었다면 그 메서드를 믿지 않는다)
    mutations: dict = field(default_factory=dict)  # 함수 → 그 함수가 바꾸는 입력 필드 (같은 함수를 반복해서 훑지 않으려고 기록)
    memo: dict = field(default_factory=dict)       # 이미 계산한 결과 기록. 같은 계산을 되풀이하면 시간이 지수로 늘 수 있어서 한 번만 계산한다.
    cycle_hits: set = field(default_factory=set)   # 계산 도중 "바깥에서 이미 계산 중인 이름"을 다시 만난 기록(순환 때문에 달라지는 결과는 기록하지 않으려고 쓴다)
    work: int = 0                                  # 지금까지 한 계산량(큰 식을 훑을 때는 식의 크기만큼 센다). 너무 복잡한 코드를 만나면 멈추기 위한 계수기
    name_hashers: set = field(default_factory=set)  # 이름 하나(HASHER.hash의 HASHER)로 부를 때 믿는 해시 도구 이름들(클래스 속성 정의는 빼고 따짐)


class AnalysisTooComplex(Exception):
    # 코드가 너무 복잡해서 정해진 작업량 안에 분석을 끝내지 못했다는 뜻이다. 이때는 확정하지 않고 UNVERIFIED로 답한다.
    pass


WORK_LIMIT = 100_000  # 한 번의 판정에서 허용하는 계산량. 측정값(2026-10-17): 정상 예제와 저장소 코드 391묶음은 53 이하(366묶음은 0),
#                    가장 무거운 인공 입력(같은 이름에 2000번 재대입)도 약 24,000이다. 한도에 닿기까지 걸리는 시간은 몇 초이다.
#                    REQ-003의 이메일 정리 따라가기·검증기 분석·로거 별칭 분석은 같은 크기의 한도를 따로 센다.


def _tick(ctx, cost=1):
    # 계산을 센다(큰 식을 훑을 때는 식의 크기만큼 센다). 한도를 넘으면 분석을 멈춘다. 이상한 코드가 평가기를 멈추거나 오래 붙잡지 못하게 하는 안전장치이다.
    ctx.work += cost
    if ctx.work > WORK_LIMIT:
        raise AnalysisTooComplex()


# 입력의 내용을 그대로 간직한 채 모양만 바꾸는 호출들. 값이 들어 있는 인자의 위치를 함께 적었다.
# (예: unicodedata.normalize("NFKC", password)는 두 번째 인자에 비밀번호가 있다.)
PRESERVING_FUNCTIONS = {'unicodedata.normalize': 1, 'base64.b64encode': 0}
RANDOM_SOURCES = frozenset({'os.urandom', 'secrets.token_bytes'})
LOSSY_ERROR_HANDLERS = frozenset({'ignore', 'replace', 'xmlcharrefreplace', 'backslashreplace', 'namereplace'})


def _hash_context(modules, rules):
    # 해시 경로 판정에 필요한 사실들을 모아 HashContext로 돌려준다. REQ-005·REQ-006과 REQ-003의 이메일 정리 따라가기가 함께 쓰므로
    # 한 번만 계산해 둔다(작업량 계수 work도 함께 쓴다).
    cache = getattr(modules, 'cache', None)
    if cache is not None and 'hash_context' in cache:
        return cache['hash_context']
    untrusted = _untrusted_function_names(modules)  # 다른 것으로 바뀔 수 있어서 믿을 수 없는 함수 이름들
    every = _functions(modules)
    random_calls, preserving = _verified_calls(modules)
    hashers, name_hashers = _known_hashers(modules, rules)
    context = HashContext({name: found for name, found in every.items() if name not in untrusted},
                          hashers, random_calls, preserving, set(every), name_hashers=name_hashers)
    if cache is not None:
        cache['hash_context'] = context
    return context


def _project_names(modules):
    # 프로젝트 파일·폴더 이름들. 프로젝트에 os.py나 secrets/ 같은 이름이 있으면 `import os`가 표준 라이브러리를 가리킨다고 믿을 수 없다.
    return {part for path in modules for part in path[:-3].split('/')}


def _imported_function(func, module, project_names):
    # 호출하는 대상이 "import한 라이브러리 함수"라고 확인되면 그 전체 이름(예: "os.urandom")을, 아니면 None을 돌려준다.
    # import 문으로 가져온 이름이고, 같은 이름이 다시 정의되지 않았으며(module.aliases에 남아 있음), 프로젝트가 같은 이름의
    # 파일·폴더를 갖고 있지 않을 때만 인정한다. (직접 만든 가짜 객체를 표준 라이브러리로 오인하지 않으려는 것이다.)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        target = module.aliases.get(func.value.id)
        if target and target.split('.')[0] not in project_names:
            return f'{target}.{func.attr}'
    elif isinstance(func, ast.Name):
        target = module.aliases.get(func.id)
        if target and '.' in target and target.split('.')[0] not in project_names:
            return target
    return None


def _verified_calls(modules):
    # 코드를 읽어서 "믿을 수 있는 호출"로 확인된 것들을 모은다. 돌려주는 값: (무작위 값을 만드는 호출의 id 집합, {입력을 간직하는 호출의 id: 값 인자 위치})
    #   · os.urandom(16)·secrets.token_bytes(16): 진짜 라이브러리이고, 크기가 salt로 쓸 만하며(8바이트 이상), 덮어쓴 흔적이 없을 때
    #   · unicodedata.normalize("NFKC", x)·base64.b64encode(x)·str(x)·bytes(x, "utf-8"): 입력을 통째로 간직하는 호출일 때
    # 이름이 같다고 믿지 않고, 실제로 import한 라이브러리인지와 덮어쓰기·재정의 여부를 확인한다.
    names = _project_names(modules)
    patched = _tampering(modules, frozenset({'urandom', 'token_bytes'}))
    random_calls, preserving = set(), {}
    for module in modules.values():
        star_import = any(isinstance(node, ast.ImportFrom) and any(alias.name == '*' for alias in node.names)
                          for node in ast.walk(module.tree))
        for call in (node for node in ast.walk(module.tree) if isinstance(node, ast.Call)):
            full = _imported_function(call.func, module, names)
            positional = call.args
            if full == 'os.urandom' and not patched and not call.keywords and len(positional) == 1 \
                    and isinstance(positional[0], ast.Constant) and type(positional[0].value) is int \
                    and positional[0].value >= MIN_SALT_BYTES:
                random_calls.add(id(call))
            elif full == 'secrets.token_bytes' and not patched and not call.keywords and (
                    not positional or (len(positional) == 1 and isinstance(positional[0], ast.Constant)
                                       and type(positional[0].value) is int and positional[0].value >= MIN_SALT_BYTES)):
                random_calls.add(id(call))
            elif full == 'unicodedata.normalize' and not call.keywords and len(positional) == 2 \
                    and isinstance(positional[0], ast.Constant) and positional[0].value in ('NFC', 'NFD', 'NFKC', 'NFKD'):
                preserving[id(call)] = 1
            elif full == 'base64.b64encode' and not call.keywords and len(positional) == 1:
                preserving[id(call)] = 0
            elif (isinstance(call.func, ast.Name) and call.func.id in ('str', 'bytes', 'bytearray') and not star_import
                  and module.bindings.get(call.func.id, 0) == 0 and call.func.id not in module.aliases
                  and positional and _constant_arguments(positional[1:], call.keywords)):
                preserving[id(call)] = 0  # str(x), bytes(x, "utf-8"): 내장 함수이고 이 파일에서 다시 정의하지 않았을 때
    return random_calls, preserving


def _constant_arguments(arguments, keywords):
    # 인코딩 이름처럼 "글자 상수"만 넘겼고, 일부 글자를 버리는 오류 처리 방식(ignore, replace 등)이 아닌가?
    values = [*arguments, *(keyword.value for keyword in keywords)]
    return all(isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value not in LOSSY_ERROR_HANDLERS
               for value in values) and all(keyword.arg in ('encoding', 'errors') for keyword in keywords)


def _is_hash_call(call, ctx, depth=0, scope=None, password_matcher=None):
    # 이 함수 호출의 결과가 "입력 비밀번호를 해시한 값"이라고 말할 수 있는가?
    # 인정하는 경우는 두 가지뿐이다.
    #   1) X.hash(값) 호출이면서, X가 "승인한 해시 도구"로 확인되고, 해시하는 값이 입력(함수 매개변수)에서 나온 것이며,
    #      salt를 직접 정하지 않았거나 진짜 무작위 값으로 만든 경우
    #      (.hash라는 이름의 메서드면 무엇이든 인정하면, 값을 그대로 돌려주는 가짜 hash()도 통과해 버린다.
    #       상수나 상수를 담은 변수를 해시하면 안전한 해시처럼 보이지만 비밀번호와 아무 상관이 없다.)
    #   2) 프로젝트 안의 같은 이름 함수가 "모든 반환값이 해시 호출의 결과"이고, 그 함수가 해시하는 매개변수 자리에 입력에서 나온 값을 넘긴 경우
    # 확인되지 않으면 False이고, 호출한 쪽에서 UNVERIFIED(확인 못 함)로 처리한다.
    # scope는 이 호출이 들어 있는 함수이다. 어떤 이름이 입력에서 나왔는지는 그 함수 안에서만 따질 수 있다. 모르면(None) 확인하지 못한다.
    # password_matcher가 주어지면(= 저장하는 곳에서 확인할 때) 해시하는 값의 "실제 출처"가 비밀번호 필드인지도 본다.
    #   (이메일 같은 다른 입력을 해시해 저장하는 코드를 거르기 위해서이다. 변수 이름이나 넘긴 인자 이름이 아니라,
    #    그 값이 실제로 어느 입력의 어느 필드에서 왔는지를 따라가서 본다.)
    # depth는 함수 안의 함수 안의 함수... 를 따라가는 깊이이다. 너무 깊어지면(2단계 초과) 멈춘다.
    if any(isinstance(arg, ast.Starred) for arg in call.args) or any(keyword.arg is None for keyword in call.keywords):
        return False  # f(*값들)·f(**설정)으로 넘긴 값은 무엇인지 알 수 없다.

    def acceptable(origins):
        # 출처가 하나라도 있고(입력에서 나왔고), 확인을 요구했다면 그중 비밀번호 필드가 있는가?
        return bool(origins) and (password_matcher is None or any(_is_password_origin(item, password_matcher) for item in origins))

    if isinstance(call.func, ast.Attribute) and call.func.attr == 'hash':
        hashed_value = _hashed_argument(call)
        if hashed_value is None or not _approved_hash_call(call, ctx):
            return False
        salt = _salt_argument(call)
        if salt is not None and id(salt) not in ctx.random_calls:
            return False  # salt를 직접 정했다. 고정 salt인지는 따로 검사하고, 진짜 무작위 값이라고 확인되지 않으면 해시 경로로 인정하지 않는다.
        return acceptable(_scope_sources(hashed_value, scope, ctx))
    name = terminal_name(call.func)
    if depth > 2 or not name:
        return False
    definitions = ctx.functions.get(name, [])  # 이 이름으로 정의된 함수들
    # 정의가 있어야 하고, 같은 이름의 모든 정의가 해시 결과만 반환해야 한다.
    if not (definitions and all(_returns_hash(function, ctx, depth) for _owner, function in definitions)):
        return False
    # 해시 함수에 상수(hash_password("fixed"))를 넘기면 비밀번호의 해시가 아니다. 그 함수가 "해시하는" 매개변수(와 그 안의 필드) 자리에
    # 호출한 쪽이 실제로 넘긴 값의 출처를 이어 붙여서 확인한다. (해시와 상관없는 다른 매개변수에만 입력을 넘기는 경우를 거른다.)
    return all(acceptable(_compose(call, definition, _hashed_origins(definition, ctx),
                                   lambda argument: _scope_sources(argument, scope, ctx)))
               for _owner, definition in definitions)


def _salt_argument(call):
    # 해시 호출에서 salt로 넘긴 값을 돌려준다. 이름 붙여 넘긴 salt=... 또는 두 번째 위치 인자.
    # 없거나 salt=None이면 None(라이브러리가 무작위로 만든다).
    found = next((keyword.value for keyword in call.keywords if keyword.arg == 'salt'), call.args[1] if len(call.args) >= 2 else None)
    return None if isinstance(found, ast.Constant) and found.value is None else found


def _union(sets):
    # 입력 출처들을 합친다. 하나라도 None(자기 자신에서 나온 값)이면 결과도 None이다.
    if any(item is None for item in sets):
        return None
    return frozenset().union(*sets)


def _intersection(sets):
    # 어느 갈래가 쓰일지 모르는 값(a or b, 조건식)은 "모든 갈래가 공통으로 나온 출처"만 인정한다. None(자기 자신)은 제약이 없는 값이다.
    known = [item for item in sets if item is not None]
    if not known:
        return None if sets else frozenset()
    return frozenset.intersection(*known)


def _input_sources(expr, scope, ctx, visiting=frozenset(), depth=0):
    # 이 값이 scope(함수)의 어느 매개변수, 즉 호출한 쪽이 넘긴 입력의 어느 필드에서 나왔는지를 출처의 집합으로 돌려준다.
    # 출처 하나는 (매개변수 이름, 필드 경로)이다. 예) payload.password → ("payload", ("password",)), 매개변수 password → ("password", ()).
    # 상수만으로 만든 값이나 함수 밖에서 정의된 이름(전역 상수 등)은 입력이 아니므로 빈 집합이다.
    # "입력의 내용을 통째로 간직한 채 모양만 바꾸는 것"만 인정한다. 입력을 버리거나 일부만 남길 수 있는 연산은 모두 빈 집합이다.
    #   인정: p, p.x, data["password"], p.strip(), p.lower(), p.encode(), str(p), "pepper" + p, f"{p}", 입력을 돌려주는 프로젝트 함수
    #   거부: p * 0, p[:3], p[0], p.replace(...), p.strip("abc"), f"{p:.0s}", 입력을 버리는 프로젝트 함수, 모르는 함수의 결과
    #   · a or b, a if c else b → 어느 쪽이 쓰일지 모르므로 모든 갈래에 공통인 입력만 인정한다.
    #   · None은 password = password.strip() 처럼 자기 자신에서 나온 값이라는 표시이다(다른 갈래가 입력인지에 따라 결정된다).
    _tick(ctx)

    def here(part):
        return _input_sources(part, scope, ctx, visiting, depth)

    if isinstance(expr, ast.Name):
        return _name_sources(expr.id, scope, ctx, visiting, depth) if scope is not None else frozenset()
    if isinstance(expr, ast.Attribute):
        return frozenset() if expr.attr.startswith('__') else _extend(here(expr.value), expr.attr)
    if isinstance(expr, ast.Subscript):
        # data["password"]처럼 글자 키로 꺼내는 것은 항목을 고르는 것이다. 숫자 위치·잘라내기(p[0], p[:3])는 일부만 남긴다.
        key = expr.slice
        return _extend(here(expr.value), key.value) if isinstance(key, ast.Constant) and isinstance(key.value, str) else frozenset()
    if isinstance(expr, (ast.Await, ast.Starred, ast.NamedExpr)):
        return here(expr.value)
    if isinstance(expr, ast.FormattedValue):
        return frozenset() if expr.format_spec is not None else here(expr.value)  # {p:.0s}처럼 형식을 지정하면 글자가 잘릴 수 있다.
    if isinstance(expr, ast.JoinedStr):
        return _union([here(part) for part in expr.values if isinstance(part, ast.FormattedValue)])
    if isinstance(expr, ast.BinOp):
        # 더하기만 인정한다. p * 0, p % ... 같은 연산은 입력을 지울 수 있다.
        if not isinstance(expr.op, ast.Add):
            return frozenset()
        # a + b + c + ... 처럼 길게 이어진 덧셈은 안쪽부터 되풀이해 들어가면 파이썬의 재귀 한도에 걸린다. 항들을 먼저 펼쳐서 따진다.
        terms, pending = [], [expr]
        while pending:
            node = pending.pop()
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                pending.extend((node.right, node.left))
            else:
                terms.append(node)
        return _union([here(term) for term in terms])
    if isinstance(expr, ast.IfExp):
        return _intersection([here(expr.body), here(expr.orelse)])
    if isinstance(expr, ast.BoolOp):
        return _intersection([here(part) for part in expr.values])
    if isinstance(expr, ast.Call):
        return _call_sources(expr, scope, ctx, visiting, depth)
    return frozenset()


def _call_sources(call, scope, ctx, visiting, depth):
    # 함수·메서드 호출의 결과가 어느 입력에서 나왔는지. 입력을 통째로 간직하는 것으로 확인된 호출만 인정한다.
    def here(part, extra=0):
        return _input_sources(part, scope, ctx, visiting, depth + extra)

    if id(call) in ctx.preserving_calls:
        return here(call.args[ctx.preserving_calls[id(call)]])  # str(x), unicodedata.normalize("NFKC", x) 등
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr not in ctx.defined_names:
        # 프로젝트가 같은 이름의 메서드를 직접 만들었다면(예: 입력을 버리는 strip) 표준 동작이라고 믿지 않는다.
        no_arguments = not call.args and not call.keywords
        if func.attr in ('strip', 'lstrip', 'rstrip', 'lower', 'upper', 'casefold', 'get_secret_value') and no_arguments:
            return here(func.value)  # 공백 제거·대소문자 변환·SecretStr 꺼내기 (인자를 주면 글자를 지울 수 있어 제외)
        if func.attr in ('encode', 'decode') and _constant_arguments(call.args, call.keywords):
            return here(func.value)
    name = terminal_name(func)
    if name and depth < 3:
        definitions = ctx.functions.get(name, [])
        if definitions:
            # 프로젝트 함수는 "돌려주는 값이 어느 매개변수의 어느 필드에서 나오는지"를 함수 안에서 따져 보고,
            # 그 자리에 호출한 쪽이 실제로 넘긴 값의 출처를 이어 붙인다. (다른 인자를 넘겼어도 돌려주지 않는 인자는 쓰지 않는다.)
            return _intersection([_compose(call, helper, _returned_origins(helper, ctx, depth + 1), lambda argument: here(argument, 1))
                                  for _owner, helper in definitions])
    return frozenset()  # 모르는 함수의 결과는 입력을 버렸을 수도 있어서 입력에서 나왔다고 말할 수 없다.


MUTATING_METHODS = frozenset({'update', 'pop', 'setdefault', 'clear', 'popitem', 'append', 'extend', 'insert', 'remove'})  # 받는 객체를 그 자리에서 바꾸는 메서드
OBJECT_WRITERS = frozenset({'__setattr__', '__delattr__', '__setitem__', '__delitem__', '__ior__'})  # obj.__setattr__(...) / object.__setattr__(obj, ...)
FUNCTION_WRITERS = frozenset({'setattr', 'delattr', 'setitem', 'delitem'})                           # setattr(obj, ...) / operator.setitem(obj, ...)
KEYED_GETTERS = frozenset({'get', 'pop', 'setdefault'})                                              # d.get("k")는 d["k"]와 같은 항목을 가리킨다
CONTAINER_BUILTINS = frozenset({'iter', 'reversed', 'sorted', 'list', 'tuple', 'set', 'frozenset', 'dict', 'next'})  # 항목은 그대로 두고 새 컨테이너에 담거나(list(x)) 항목 하나를 꺼내는(next(x)) 내장 함수
MAX_MUTATION_DEPTH = 5   # 입력을 넘겨받은 도우미 함수 안의 도우미 함수 ... 를 따라가는 단계의 한계
MAX_ALIAS_PATH = 4       # 별칭이 가리키는 필드 경로의 최대 길이 (더 긴 경로는 앞부분만 남긴다. 앞부분만 남기면 더 넓게 겹치므로 안전한 쪽이다.)
MAX_ALIAS_PLACES = 40    # 별칭 하나가 가리킬 수 있는 곳의 최대 개수 (넘으면 곳을 세세히 따지지 않고 그 입력 전체를 가리키는 것으로 본다)
MAX_CONTAINER_LEVEL = 3  # [[p]] 처럼 입력을 담은 컨테이너를 겹쳐 담는 단계의 한계


def _parameter_names(scope):
    # 함수(scope)의 매개변수 이름들. 함수가 아니면(모듈 등) 빈 집합이다.
    arguments = getattr(scope, 'args', None)
    if arguments is None:
        return set()
    names = {argument.arg for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]}
    names.update(argument.arg for argument in (arguments.vararg, arguments.kwarg) if argument is not None)
    return names


def _chain(node):
    # payload.profile["email"] 처럼 속성·키로 이어진 식을 (가장 안쪽 식, [단계, ...])로 푼다. 단계는 안쪽에서 바깥쪽 순서이다.
    #   단계: ("attr", 이름) / ("key", 글자 키) / ("dyn", None) = 어느 항목인지 모르는 접근(p[i], p[1:], p.get(변수))
    #   · obj.__dict__["x"], vars(obj)["x"], getattr(obj, "x"), obj.get("x") 는 obj.x·obj["x"] 와 같은 곳을 가리킨다. (이름만 바꿔 우회하지 못하게 한다.)
    steps = []
    while True:
        if isinstance(node, ast.Attribute):
            if node.attr != '__dict__':
                steps.append(('attr', node.attr))
            node = node.value
        elif isinstance(node, ast.Subscript):
            steps.append(('key', node.slice.value) if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str) else ('dyn', None))
            node = node.value
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'vars' and len(node.args) == 1 and not node.keywords:
            node = node.args[0]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'getattr' and len(node.args) >= 2 and not node.keywords:
            name = node.args[1]
            steps.append(('attr', name.value) if isinstance(name, ast.Constant) and isinstance(name.value, str) else ('dyn', None))
            node = node.args[0]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in KEYED_GETTERS and node.args:
            key = node.args[0]
            steps.append(('key', key.value) if isinstance(key, ast.Constant) and isinstance(key.value, str) else ('dyn', None))
            node = node.func.value
        else:
            return node, steps[::-1]


def _step_place(place, step):
    # 곳 하나에서 한 단계 더 들어간다. 곳은 (입력 이름, 필드 경로, 내용만인가, 컨테이너 단계)이다.
    #   · 컨테이너 단계가 1 이상이면 그 곳을 항목으로 담은 컨테이너([p], {"k": p})이다. 항목을 꺼내면(어떤 접근이든) 한 단계 내려가 그 곳 자체가 된다.
    #   · 컨테이너가 아닌 곳에서 속성·글자 키로 들어가면 경로가 한 칸 늘어난다. 어느 항목인지 모르면(내용만) 그 곳 아래 어딘가를 가리킨다.
    #   · 이미 "그 아래 어딘가"인 곳은 더 따지지 않는다.
    root, path, opaque, level = place
    if level:
        return (root, path, opaque, level - 1)
    if opaque:
        return place
    kind, name = step
    if kind == 'dyn':
        return (root, path, True, 0)
    return (root, (path + (name,))[:MAX_ALIAS_PATH], False, 0)


def _alias_places(scope, ctx, depth):
    # 함수(scope) 안에서 이름이 가리킬 수 있는 "입력의 곳"을 따진다. 돌려주는 값: (places 함수, {이름: 곳들})
    # 예) form = payload.profile → form은 ("payload", ("profile",), False, 0)을 가리킨다.
    # 이름에 값이 묶이는 방식은 모두 따진다: 대입(a = p, a, b = p, q, a = p or q, a = [p]), :=, for·with 변수, 컴프리헨션, match.
    # 입력을 돌려주는 프로젝트 함수의 결과도 같은 입력을 가리킨다.
    # 따라가지 못하는 것: 알 수 없는 라이브러리 함수가 입력을 감싸 돌려주는 경우(w = Wrapper(p)), 나중에 컨테이너에 담는 경우(h = []; h.append(p)).
    key = ('alias', id(scope), depth)
    if key in ctx.memo:
        return ctx.memo[key]
    parameters = _parameter_names(scope)
    aliases = {}

    def places(expr):
        base, steps = _chain(expr)
        found = base_places(base)
        for step in steps:
            found = {_step_place(place, step) for place in found}
        return found

    def items_of(found):
        # 컨테이너를 훑어 꺼낸 항목들의 곳 (for x in c, a, b = c)
        return {_step_place(place, ('dyn', None)) for place in found}

    def container_of(found):
        return {(root, path, opaque, min(level + 1, MAX_CONTAINER_LEVEL)) for root, path, opaque, level in found}

    def base_places(node):
        if isinstance(node, ast.Name):
            found = set(aliases.get(node.id, ()))
            if node.id in parameters:
                found.add((node.id, (), False, 0))
            return found
        if isinstance(node, ast.IfExp):
            return places(node.body) | places(node.orelse)
        if isinstance(node, ast.BoolOp):
            return set().union(*[places(part) for part in node.values])
        if isinstance(node, (ast.NamedExpr, ast.Starred, ast.Await)):
            return places(node.value)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return container_of(set().union(*[places(part) for part in node.elts])) if node.elts else set()
        if isinstance(node, ast.Dict):
            return container_of(set().union(*[places(part) for part in node.values])) if node.values else set()
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return container_of(places(node.elt))
        if isinstance(node, ast.DictComp):
            return container_of(places(node.value))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in CONTAINER_BUILTINS and node.args
                and node.func.id not in ctx.defined_names):  # 프로젝트가 같은 이름을 직접 정의했다면 내장 함수가 아니다
            if node.func.id == 'next':
                return items_of(places(node.args[0])) | (places(node.args[1]) if len(node.args) > 1 else set())
            return container_of(items_of(places(node.args[0])))  # list(x): 새 컨테이너이지만 담긴 항목은 x의 항목과 같은 객체이다.
        if isinstance(node, ast.Call) and depth < MAX_MUTATION_DEPTH:
            found = set()
            for _owner, helper in ctx.functions.get(terminal_name(node.func) or '', []):
                found |= _compose_places(node, helper, _returned_places(helper, ctx, depth + 1), places)
            return found
        return set()

    bindings = []  # [(대상 이름, 값 식, 항목을 꺼내 묶는가)]

    def bind(target, value, item):
        if isinstance(target, ast.Name):
            bindings.append((target.id, value, item))
        elif isinstance(target, (ast.Tuple, ast.List)):
            paired = (isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts)
                      and not any(isinstance(part, ast.Starred) for part in [*value.elts, *target.elts]))
            for index, part in enumerate(target.elts):
                if paired:
                    bind(part, value.elts[index], item)
                else:
                    bind(part, value, True)  # 풀어서 받은 조각은 값 안의 항목이다.
        elif isinstance(target, ast.Starred):
            bind(target.value, value, item)

    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value, False)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            bind(node.target, node.value, False)
        elif isinstance(node, ast.AugAssign):
            bind(node.target, node.value, False)
        elif isinstance(node, ast.NamedExpr):
            bind(node.target, node.value, False)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            bind(node.target, node.iter, True)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for entry in node.items:
                if entry.optional_vars is not None:
                    bind(entry.optional_vars, entry.context_expr, False)
        elif hasattr(ast, 'Match') and isinstance(node, ast.Match):
            for case in node.cases:
                for pattern in ast.walk(case.pattern):
                    for captured in (getattr(pattern, 'name', None), getattr(pattern, 'rest', None)):
                        if isinstance(captured, str):
                            bindings.append((captured, node.subject, True))

    frozen = set()  # 곳이 너무 많이 늘어나 "입력 전체"로 뭉뚱그린 이름들

    def merge(name, new):
        current = aliases.setdefault(name, set())
        if name in frozen:
            merged = {(root, (), True, 0) for root, _path, _opaque, _level in current | new}
        else:
            merged = current | new
            if len(merged) > MAX_ALIAS_PLACES:
                frozen.add(name)
                merged = {(root, (), True, 0) for root, _path, _opaque, _level in merged}
        if merged == current:
            return False
        aliases[name] = merged
        return True

    # 한 이름이 가리키는 곳이 바뀌면 그 이름을 쓰는 대입만 다시 따진다. 곳은 늘기만 하고(줄이거나 덮어쓰지 않는다) 크기에 한도가 있어서 반드시 끝난다.
    # (한 곳으로 덮어쓰는 방식은 unused = payload; unused = db 처럼 두 대입이 서로 값을 바꾸며 영원히 반복할 수 있다.)
    users, sizes = {}, []  # {이름: 그 이름을 쓰는 대입들}, 대입마다 값 식의 크기
    for index, (_target, value, _item) in enumerate(bindings):
        sizes.append(0)
        for entry in ast.walk(value):
            sizes[index] += 1
            if isinstance(entry, ast.Name):
                users.setdefault(entry.id, set()).add(index)
    queue, queued = deque(range(len(bindings))), set(range(len(bindings)))
    while queue:
        index = queue.popleft()
        queued.discard(index)
        _tick(ctx, sizes[index])
        target, value, item = bindings[index]
        new = items_of(places(value)) if item else places(value)
        if new and merge(target, new):
            for user in users.get(target, ()):
                if user not in queued:
                    queue.append(user)
                    queued.add(user)
    ctx.memo[key] = (places, aliases)
    return ctx.memo[key]


def _returned_places(function, ctx, depth):
    # 이 함수가 돌려주는 값이 가리킬 수 있는 입력의 곳을 이 함수의 매개변수 기준으로 돌려준다. 예) def ident(x): return x → {("x", (), False, 0)}
    key = ('returned places', id(function), depth)
    if key not in ctx.memo:
        ctx.memo[key] = frozenset()  # 서로 부르는 함수(재귀)를 끊는다.
        if depth <= MAX_MUTATION_DEPTH:
            places, _aliases = _alias_places(function, ctx, depth)
            found = set()
            for node in ast.walk(function):
                if isinstance(node, ast.Return) and node.value is not None:
                    found |= places(node.value)
            ctx.memo[key] = frozenset(found)
    return ctx.memo[key]


def _compose_places(call, helper, relative, places_of):
    # helper 안에서 "(자기 매개변수, 경로, ...)"로 적힌 곳(relative)을, 호출(call)이 그 매개변수 자리에 실제로 넘긴 값이 가리키는 곳으로 바꿔 쓴다.
    bound = {}
    for parameter, argument in _bound_arguments(call, helper):
        bound.setdefault(parameter, []).append(argument)
    composed = set()
    for parameter, path, opaque, level in relative:
        for argument in bound.get(parameter, []):
            for place in places_of(argument):
                for name in path:
                    place = _step_place(place, ('attr', name))
                if opaque:
                    place = _step_place(place, ('dyn', None))
                composed.add((place[0], place[1], place[2], min(place[3] + level, MAX_CONTAINER_LEVEL)))
    return composed


def _updated_keys(call, shift=0):
    # d.update({"a": 1}, b=2) 에서 바뀌는 항목 이름들 ["a", "b"]를 돌려준다. 하나라도 알 수 없으면(변수 키, **펼침, 다른 사전을 통째로 넘김) None이다.
    # shift는 앞쪽 인자를 몇 개 건너뛸지이다(dict.update(대상, {...})에서는 1).
    names = []
    for argument in call.args[shift:]:
        if not isinstance(argument, ast.Dict):
            return None
        for key in argument.keys:
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                return None
            names.append(key.value)
    for keyword in call.keywords:
        if keyword.arg is None:
            return None
        names.append(keyword.arg)
    return names


def _mutated_fields(scope, ctx, depth=0):
    # 이 함수가 입력 객체의 어느 필드를 바꾸는지 {매개변수 이름: {필드 경로, ...}}로 돌려준다. 경로 ()는 객체 전체가 바뀐다는 뜻이다.
    #   · p.x = ..., p["x"] = ..., del p.x, p.x += ...           → (p, ("x",))
    #   · p.tags.append(...) → (p, ("tags",)) / p.update({"k": v}), p.update(k=v), p.pop("k") → (p, ("k",))
    #   · p.update(다른_사전), p.clear(), vars(p).update(다른_사전) → (p, ()) (어느 항목이 바뀌는지 알 수 없다)
    #   · p.__dict__["x"] = ..., p.__dict__.update(...), object.__setattr__(p, "x", ...), p.__setattr__("x", ...) → p.x 를 바꾼 것과 같다
    #   · setattr(p, "x", ...)                                    → (p, ("x",)),  setattr(p, 이름, ...) → (p, ())
    #   · q = p.profile 처럼 별칭을 만들어 q.x = ... 로 바꾸는 경우 → (p, ("profile", "x")). 별칭을 만드는 방식은 _alias_places 가 따진다.
    #   · 입력을 넘겨받은 프로젝트 함수가 안에서 바꾸는 필드(MAX_MUTATION_DEPTH 단계까지)
    # 입력의 필드를 덮어쓰면 payload.password가 더 이상 원래 입력이 아니라서, 그 필드에서 온 값은 입력의 해시라고 믿을 수 없다.
    # 입력을 항목으로 담은 컨테이너 자체를 바꾸는 것(items.append(...), items[0] = ...)은 입력을 바꾸는 것이 아니다.
    key = (id(scope), depth)
    if key in ctx.mutations:
        return ctx.mutations[key]
    _tick(ctx)
    ctx.mutations[key] = {}  # 같은 함수를 다시 만나는 경우(재귀)를 끊는다.
    places, _aliases = _alias_places(scope, ctx, depth)
    found = {}

    def record(obj, extra=()):
        # 객체(obj)의 extra 경로 필드가 바뀐다고 기록한다. obj가 가리키는 입력이 하나도 없으면 False이다.
        hit = False
        for root, path, opaque, level in places(obj):
            hit = True
            if level == 0:
                found.setdefault(root, set()).add(path if opaque else (path + extra)[:MAX_ALIAS_PATH])
        return hit

    def written_name(argument):
        # setattr(p, "x", ...) 의 "x" 처럼 바뀌는 필드 이름을 글자로 적었으면 (이름,), 아니면 ()(어느 필드인지 모름 = 객체 전체)
        return (argument.value,) if isinstance(argument, ast.Constant) and isinstance(argument.value, str) else ()

    for node in ast.walk(scope):
        if isinstance(node, (ast.Attribute, ast.Subscript)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if isinstance(node, ast.Attribute):
                record(node.value, () if node.attr == '__dict__' else (node.attr,))
            else:
                record(node.value, written_name(node.slice))
        elif isinstance(node, ast.Call):
            func = node.func
            method = func.attr if isinstance(func, ast.Attribute) else None
            if method in MUTATING_METHODS or method in OBJECT_WRITERS:
                # 받는 쪽(p.update(...))이 입력이면 그 입력이 바뀐다. 받는 쪽이 입력이 아니면 dict.update(vars(p), ...)·object.__setattr__(p, ...)
                # 처럼 첫 인자가 바뀌는 쪽이다. (그때는 바뀌는 항목을 가리키는 인자가 하나씩 뒤로 밀린다.)
                if places(func.value):
                    target, shift = func.value, 0
                else:
                    target, shift = (node.args[0] if node.args else None), 1
                if target is not None:
                    # 바뀌는 항목 이름을 알면 그 항목만 기록한다: update({"k": v}, m=1) → k, m / pop("k")·setdefault("k", v)·__setattr__("k", v) → k. 모르면 객체 전체.
                    if method == 'update':
                        keys = _updated_keys(node, shift)
                        changed = [(name,) for name in keys] if keys is not None else [()]
                    elif method in ('pop', 'setdefault') or method in OBJECT_WRITERS:
                        changed = [written_name(node.args[shift])] if len(node.args) > shift else [()]
                    else:
                        changed = [()]
                    for extra in changed:
                        record(target, extra)
            elif (method or (func.id if isinstance(func, ast.Name) else None)) in FUNCTION_WRITERS and node.args:
                record(node.args[0], written_name(node.args[1]) if len(node.args) > 1 else ())
            if depth < MAX_MUTATION_DEPTH:
                for _owner, helper in ctx.functions.get(terminal_name(func) or '', []):
                    inner = _mutated_fields(helper, ctx, depth + 1)
                    for parameter, argument in _bound_arguments(node, helper):
                        for subpath in inner.get(parameter, ()):
                            record(argument, subpath)
    ctx.mutations[key] = found
    return found


def _scope_sources(expr, scope, ctx, depth=0):
    # _input_sources에 더해, 이 함수가 덮어쓰는 입력 필드에서 온 출처는 뺀다. (입력 필드를 바꿨다면 그 필드의 값은 원래 입력이 아니다.)
    sources = _input_sources(expr, scope, ctx, frozenset(), depth)
    if not sources:
        return sources
    mutated = _mutated_fields(scope, ctx)
    return frozenset((root, path) for root, path in sources
                     if not any(path[:len(item)] == item or item[:len(path)] == path for item in mutated.get(root, ())))


def _extend(sources, step):
    # 각 출처의 필드 경로 끝에 step(속성 이름이나 글자 키)을 덧붙인다. 예) ("payload", ()) + "password" → ("payload", ("password",))
    if sources is None:
        return None
    return frozenset((root, path + (step,)) for root, path in sources)


def _compose(call, helper, relative, sources_of):
    # helper(프로젝트 함수) 안에서 "(자기 매개변수, 그 안의 필드 경로)"로 적힌 출처(relative)를,
    # 호출(call)이 그 매개변수 자리에 실제로 넘긴 값의 출처로 바꿔 쓴다. sources_of(값)은 호출한 쪽에서 그 값의 출처를 구하는 함수이다.
    # 예) helper가 form.password를 돌려주고 호출이 helper(payload)이면 → ("payload", ("password",))
    bound = {}
    for parameter, argument in _bound_arguments(call, helper):
        bound.setdefault(parameter, []).append(argument)
    composed = []
    for parameter, subpath in relative or ():
        for argument in bound.get(parameter, []):
            part = sources_of(argument)
            composed.append(None if part is None else frozenset((root, path + subpath) for root, path in part))
    return _union(composed)


def _returned_origins(function, ctx, depth):
    # 이 함수가 돌려주는 값의 출처를 이 함수의 매개변수 기준으로 돌려준다 (모든 return에 공통인 것).
    # return이 없거나 하나라도 모르면 빈 집합이다.
    key = ('returned', id(function), depth)
    if key not in ctx.memo:
        returns = [node for node in ast.walk(function) if isinstance(node, ast.Return)]
        if depth > 3 or not returns or any(node.value is None for node in returns):
            ctx.memo[key] = frozenset()
        else:
            sets = [_scope_sources(node.value, function, ctx, depth) for node in returns]
            ctx.memo[key] = frozenset.intersection(*[item if item is not None else frozenset() for item in sets])
    return ctx.memo[key]


def _scope_index(scope, ctx):
    # 함수(scope) 안에서 이름마다 값이 묶이는 곳을 한 번만 훑어 정리해 둔다. (이름마다 함수 전체를 다시 훑지 않으려는 것이다.)
    # 돌려주는 값: (매개변수 이름들, global·nonlocal로 선언한 이름들, {이름: [묶임, ...]}, {이름: 묶이는 횟수})
    # 묶임 하나는 ("aug", 연산) / ("for", None) / ("assign", 대입 대상, 대입하는 값) 중 하나이다.
    key = ('index', id(scope))
    if key in ctx.memo:
        return ctx.memo[key]
    arguments = scope.args
    parameters = {argument.arg for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]}
    parameters.update(argument.arg for argument in (arguments.vararg, arguments.kwarg) if argument is not None)
    declared, entries = set(), {}
    for node in ast.walk(scope):
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name):
                entries.setdefault(node.target.id, []).append(('aug', node.op))
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for item in ast.walk(node.target):
                if isinstance(item, ast.Name):
                    entries.setdefault(item.id, []).append(('for', None))
        else:
            for target, value in _assignment_pairs(node):
                for item in ast.walk(target):
                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
                        entries.setdefault(item.id, []).append(('assign', target, value))
    index = (parameters, declared, entries, _name_bindings(scope))
    ctx.memo[key] = index
    return index


def _name_sources(name, scope, ctx, visiting, depth):
    # 함수(scope) 안의 이름 하나에 담긴 값의 입력 출처. 이름에 값이 묶이는 곳이 매개변수와 일반 대입뿐일 때만 따진다.
    # with·except·반복문 변수 등 모르는 방식이 섞여 있으면 빈 집합이다. 모든 묶임에 공통인 출처만 인정한다.
    # 같은 이름을 여러 식이 거듭 쓰는 코드(a2 = a1 + a1; a3 = a2 + a2; ...)는 그냥 계산하면 시간이 지수로 늘어난다.
    # 그래서 한 번 계산한 결과는 기억해 둔다. 단, 바깥에서 이미 계산 중인 이름(순환)에 걸려서 달라질 수 있는 결과는 기억하지 않는다.
    if name in visiting:
        ctx.cycle_hits.add(name)
        return None
    key = ('name', id(scope), name, depth)
    if key in ctx.memo:
        return ctx.memo[key]
    _tick(ctx)
    saved, ctx.cycle_hits = ctx.cycle_hits, set()
    try:
        result = _compute_name_sources(name, scope, ctx, visiting | {name}, depth)
    finally:
        hits, ctx.cycle_hits = ctx.cycle_hits, saved
    outer_hits = {item for item in hits if item in visiting}
    ctx.cycle_hits |= outer_hits
    if not outer_hits:
        ctx.memo[key] = result
    return result


def _compute_name_sources(name, scope, ctx, visiting, depth):
    parameters, declared, entries, bindings = _scope_index(scope, ctx)
    if name in declared:
        return frozenset()  # 함수 밖의 이름을 바꿀 수 있어서 이 함수 안에서만 따질 수 없다.
    if name in ('self', 'cls'):
        return frozenset()  # 객체의 필드(self.password)는 프로그램이 어디선가 채운 값이라 호출한 쪽이 넘긴 입력이라고 볼 수 없다.
    results = []  # 묶임마다의 출처
    for entry in entries.get(name, ()):
        if entry[0] == 'aug':
            if not isinstance(entry[1], ast.Add):
                results.append(frozenset())  # p *= 0, p -= ... 처럼 값을 지우거나 바꿀 수 있는 연산 (p += "x" 는 이전 값에 덧붙이는 것이라 입력이 그대로 남는다.)
        elif entry[0] == 'for':
            results.append(frozenset())  # 반복문 변수는 값의 일부(글자 하나 등)일 수 있다.
        else:
            # 이름 하나에 값 하나를 대입한 경우만 따진다. a, b = 값 처럼 풀어서 받으면 일부일 수 있다.
            _kind, target, value = entry
            results.append(_input_sources(value, scope, ctx, visiting, depth) if isinstance(target, ast.Name) else frozenset())
    own = 1 if name in parameters else 0
    if bindings.get(name, 0) != len(entries.get(name, ())) + own:
        return frozenset()  # 설명할 수 없는 묶임(with·except·함수 정의·안쪽 함수의 같은 이름 매개변수 등)이 있다.
    if own:
        results.append(frozenset({(name, ())}))
    if not results:
        return frozenset()  # 이 함수 안에서 묶이지 않는 이름(전역 등)은 입력이라고 볼 수 없다.
    return _intersection(results)


def _is_password_origin(origin, matcher):
    # 출처 하나((입력 이름, 필드 경로))가 "비밀번호 필드"인가? (password, new_password, pwd 등 비밀번호처럼 보이는 이름)
    #   · payload.password → 경로의 이름(password)이 비밀번호이다.
    #   · 매개변수 password → 경로가 없으니 입력 이름 자체가 비밀번호이다.
    #   · password_form.email → 입력 이름은 비밀번호처럼 보여도 꺼낸 필드가 email이라서 비밀번호가 아니다.
    # 이메일 같은 다른 입력을 해시해 저장하는 코드를 거르기 위한 확인이다.
    root, path = origin
    return any(matcher.search(part) for part in path) if path else bool(matcher.search(root))


def _bound_arguments(call, helper):
    # 함수 호출(call)에서 넘긴 값들이 helper(함수 정의)의 어느 매개변수에 들어가는지 [(매개변수 이름, 넘긴 값)]으로 짝짓는다.
    arguments = helper.args
    names = [argument.arg for argument in [*arguments.posonlyargs, *arguments.args]]
    receiver = []
    if names and names[0] in ('self', 'cls') and isinstance(call.func, ast.Attribute):
        receiver = [(names[0], call.func.value)]  # obj.method(x) 에서 self는 호출한 객체 obj가 채운다. (메서드가 self의 필드를 바꾸면 obj의 필드가 바뀐다.)
        names = names[1:]
    every = {argument.arg for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]}
    return receiver + list(zip(names, call.args)) + [(keyword.arg, keyword.value) for keyword in call.keywords if keyword.arg in every]


def _hashed_origins(function, ctx, depth=0):
    # 이 함수가 "실제로 돌려주는 해시"가 무엇을 해시한 것인지, 그 출처를 이 함수의 매개변수 기준으로 돌려준다 (모든 return에 공통인 것).
    # 예) def hash_password(password, pepper): return H.hash(password) → {("password", ())}
    #     def hash_form(form): return H.hash(form.password)        → {("form", ("password",))}
    # 계산만 하고 버리는 해시는 포함하지 않는다. (비밀번호 해시를 계산만 하고 버린 뒤 이메일 해시를 돌려주는 함수를
    # "비밀번호를 해시하는 함수"로 오인하지 않으려는 것이다.)
    key = ('hashed', id(function), depth)
    if key not in ctx.memo:
        returns = [node.value for node in ast.walk(function) if isinstance(node, ast.Return) and node.value is not None]
        sets = [_returned_hash_origins(value, function, ctx, depth, frozenset()) for value in returns]
        ctx.memo[key] = frozenset.intersection(*sets) if sets else frozenset()
    return ctx.memo[key]


def _returned_hash_origins(expr, function, ctx, depth, seen):
    # 돌려주는 식(expr)이 해시 결과일 때, 그 해시가 해시한 값의 출처. 해시 결과가 아니면 빈 집합이다.
    #   · 해시 도구의 hash(값) 호출 → 그 값의 출처
    #   · 다른 해시 함수의 호출 → 그 함수가 해시하는 자리에 넘긴 값의 출처
    #   · 해시 결과를 담은 변수 → 그 변수에 담긴 모든 해시의 공통 출처
    _tick(ctx)
    if isinstance(expr, ast.Await):
        expr = expr.value
    if isinstance(expr, ast.Call):
        if _approved_hash_call(expr, ctx):
            value = _hashed_argument(expr)
            return (_scope_sources(value, function, ctx) or frozenset()) if value is not None else frozenset()
        if depth < 3:
            parts = [_compose(expr, helper, _hashed_origins(helper, ctx, depth + 1),
                              lambda argument: _scope_sources(argument, function, ctx)) or frozenset()
                     for _owner, helper in ctx.functions.get(terminal_name(expr.func) or '', [])]
            return frozenset.intersection(*parts) if parts else frozenset()
        return frozenset()
    if isinstance(expr, ast.Name) and expr.id not in seen:
        values = [value for node in ast.walk(function) for target, value in _assignment_pairs(node)
                  if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(target, ast.Name) and target.id == expr.id]
        parts = [_returned_hash_origins(value, function, ctx, depth, seen | {expr.id}) for value in values]
        return frozenset.intersection(*parts) if parts else frozenset()
    return frozenset()


def _hash_valued_names(function, ctx, depth):
    # 함수 안에서 "값이 들어간 모든 순간에 해시 호출의 결과였던" 변수 이름들을 찾는다.
    # 같은 변수에 나중에 원문을 다시 대입하거나(result = password), 반복문·+= 등으로 값이 바뀌면 해시 변수로 보지 않는다.
    # 매개변수 이름도 제외한다. 호출한 쪽이 해시하지 않은 값을 넘겼을 수 있기 때문이다.
    arguments = function.args
    parameters = {argument.arg for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]}
    parameters.update(argument.arg for argument in (arguments.vararg, arguments.kwarg) if argument is not None)
    verdicts = {}   # {이름: [대입마다 "해시 호출의 결과인가"]}
    covered = set()  # 아래에서 이미 판정한 이름 노드
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            pairs = [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            pairs = [(node.target, node.value)]
        else:
            continue
        for target, value in pairs:
            if isinstance(target, ast.Name):
                covered.add(id(target))
                if isinstance(value, ast.Await):
                    value = value.value
                verdicts.setdefault(target.id, []).append(
                    isinstance(value, ast.Call) and _is_hash_call(value, ctx, depth + 1, scope=function))
    for node in ast.walk(function):
        # 위에서 판정하지 않은 다른 모든 대입(+=, for, 튜플 풀기 등)은 해시 결과가 아니라고 본다.
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and id(node) not in covered:
            verdicts.setdefault(node.id, []).append(False)
    return {name for name, flags in verdicts.items() if all(flags) and name not in parameters}


def _returns_hash(function, ctx, depth):
    # 이 함수가 "해시 호출의 결과만" 반환하는가? 모든 return이 해시 결과여야 True다.
    key = ('returns', id(function), depth)
    if key in ctx.memo:
        return ctx.memo[key]
    ctx.memo[key] = False  # 서로를 부르는 함수(재귀)를 만나면 일단 "아님"으로 두어 끊는다.
    hashed = _hash_valued_names(function, ctx, depth)  # 해시 결과만 담아 온 변수 이름들
    returns = [node.value for node in ast.walk(function) if isinstance(node, ast.Return) and node.value is not None]
    answer = bool(returns)  # 아무것도 반환하지 않는 함수는 해시를 돌려준다고 할 수 없다.
    for value in returns:
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call) and _is_hash_call(value, ctx, depth + 1, scope=function):
            continue  # 해시 호출의 결과를 바로 반환
        if isinstance(value, ast.Name) and value.id in hashed:
            continue  # 해시 결과만 담아 온 변수를 반환
        answer = False  # 그 밖의 값(예: 원문 비밀번호)을 반환하는 경로가 있다.
        break
    ctx.memo[key] = answer
    return answer


def _orm_classes(modules):
    # "DB 테이블을 나타내는 클래스"의 이름들을 찾는다. (ORM = 코드의 클래스를 DB 테이블과 연결해 주는 방식)
    # __tablename__ 이라는 속성이 있거나, Base·SQLModel 같은 DB 모델의 부모 클래스를 상속하면 DB 모델로 본다.
    names = set()
    for module in modules.values():
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = {terminal_name(base) for base in node.bases}  # 부모 클래스들의 이름
            has_table = any(isinstance(item, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == '__tablename__' for target in item.targets)
                for item in node.body)
            if has_table or bases & {'Base', 'DeclarativeBase', 'SQLModel', 'Model'}:
                names.add(node.name)
    return names


# ---------------------------------------------------------------- REQ-003 이메일 중복

SKIP = object()  # "사용자 표가 아닌 곳의 선언이라 근거로 쓰지 않는다"는 표시


class EmailEvidence:
    # 이메일 UNIQUE 근거를 "어느 표의 것인지"별로 모아 두는 상자이다.
    # 소유자(owner)는 표를 가리키는 이름표이다: ("class", "User") = ORM 클래스, ("table", "users") = SQL·Table 이름. None = 어느 표인지 모르는 선언.
    def __init__(self):
        self.buckets = {}         # {소유자: {"unique": [...], "plain": [...], "uncertain": [...]}}
        self.user_owners = set()  # 비밀번호 칸이 있는 표(= 사용자 표)들
        self.targets = set()      # 가입으로 새 사용자를 저장하는 데 쓰이는 표들
        self.tablenames = {}      # {__tablename__ 값(소문자): 클래스 소유자}. 같은 표를 클래스와 SQL이 따로 가리킬 때 하나로 합친다.
        self.indexes = []         # (파일, 코드 조각, 표 이름, 칸 목록): CREATE UNIQUE INDEX는 표 이름이 확인된 뒤에 판정한다.

    def bucket(self, owner):
        return self.buckets.setdefault(owner, {'unique': [], 'plain': [], 'uncertain': []})

    def canonical(self, owner):
        # ("table", "users") 처럼 SQL 이름으로 적힌 소유자를, 같은 표를 가리키는 ORM 클래스가 있으면 그 클래스로 바꾼다.
        if owner is not None and owner[0] == 'table':
            return self.tablenames.get(owner[1], owner)
        return owner

    def decide(self):
        # 근거를 표별로 합쳐서 판정한다. 돌려주는 값: (결과 종류, 근거 기록 목록)
        #   결과 종류: "unique" / "unclear"(표마다 달라 모름) / "undetermined"(칸을 읽을 수 없음) / "plain"(UNIQUE 없음) / "none"(이메일 칸을 못 찾음)
        # 어느 표가 가입에 쓰이는지 코드에서 확인되면(예: User(email=..., password_hash=...)) 그 표들만 보고,
        # 확인되지 않으면 비밀번호 칸이 있는 모든 표를 본다. 표마다 결과가 다르면 단정하지 않는다.
        for module, node, table, columns in self.indexes:
            owner = self.canonical(('table', table))
            if owner in {self.canonical(item) for item in self.user_owners}:
                kind = 'unique' if len(columns) == 1 else 'plain'
                self.bucket(owner)[kind].append(_obs(module, node, 'SQL_UNIQUE_INDEX' if kind == 'unique' else 'COMPOSITE_UNIQUE_NOT_EMAIL_ONLY'))
            else:
                self.bucket(None)['uncertain'].append(_obs(module, node, 'UNIQUE_INDEX_TABLE_UNKNOWN', table=table))
        merged = {}
        for owner, bucket in self.buckets.items():
            into = merged.setdefault(self.canonical(owner), {'unique': [], 'plain': [], 'uncertain': []})
            for kind, items in bucket.items():
                into[kind].extend(items)
        shared = merged.pop(None, {'unique': [], 'plain': [], 'uncertain': []})  # 어느 표인지 모르는 선언은 모든 후보에 적용한다.
        owners = {self.canonical(owner) for owner in self.user_owners}
        candidates = ({self.canonical(owner) for owner in self.targets} & owners) or owners
        pools = [merged.get(owner, {'unique': [], 'plain': [], 'uncertain': []}) for owner in sorted(candidates, key=str)] or [{'unique': [], 'plain': [], 'uncertain': []}]
        states, collected = [], {'unique': [], 'plain': [], 'uncertain': []}
        for pool in pools:
            merged_pool = {kind: pool[kind] + shared[kind] for kind in pool}
            for kind in collected:
                collected[kind].extend(merged_pool[kind])
            states.append('unique' if merged_pool['unique'] else 'undetermined' if merged_pool['uncertain']
                          else 'plain' if merged_pool['plain'] else 'none')
        if set(states) == {'unique'}:
            return 'unique', collected['unique']
        if 'unique' in states:
            return 'unclear', collected['plain'] + collected['uncertain']
        if 'undetermined' in states:
            return 'undetermined', collected['uncertain']
        if 'plain' in states:
            return 'plain', collected['plain']
        return 'none', []


def check_duplicate_email(modules, criteria):
    # REQ-003: 같은 이메일로 두 번 가입할 수 없게 막혀 있는가?
    # 확인하는 것 두 가지:
    #   ① 가입으로 사용자를 저장하는 표의 이메일 칸에 UNIQUE(중복 금지) 설정이 있는가
    #   ② 저장되는 이메일 값이 정리(앞뒤 공백 제거 strip, 소문자로 바꾸기 lower)를 거쳤는가
    #      근거: 이메일 필드에 등록된 검증기의 모든 반환 경로, pydantic 정규화 설정, 저장하는 곳마다 값을 거슬러 따라간 결과(_email_flow)
    # ①이 없으면 FAIL. 이메일 칸을 못 찾거나 어느 표의 것인지 확신할 수 없으면 UNVERIFIED.
    # ②는 증명되면 PASS, 정리하지 않았다고 증명되면 FAIL(아래 세 경우), 정리 코드는 있지만 저장 값과 연결되는지 모르면 UNVERIFIED.
    rules = criteria['duplicateEmail']
    email = _email_names(criteria)
    normalized = set()  # 확인한 정리 방법("strip", "lower")
    normalization_unknown = False
    validator_found = False
    # 이메일 UNIQUE는 "사용자 표"(비밀번호를 저장하는 표)에 대한 것일 때만 근거가 된다. 다른 표의 이메일 칸은 가입 중복을 막지 못한다.
    # 사용자 표인지는 같은 클래스·표 안에 비밀번호 칸(password_hash 등)이 있는지로 판단한다. 표 밖에서 선언한 것은 가리지 못해 모든 후보에 적용한다.
    matcher = _sensitive(criteria)
    storage_names = frozenset(criteria['hashPolicy']['storageFieldNames'])

    def is_password(name):
        return bool(matcher.search(name)) or name in storage_names

    evidence = EmailEvidence()
    orm_classes = _orm_classes(modules)  # DB 표를 나타내는 클래스들. 비밀번호 칸이 있어도 요청 모델(pydantic)은 DB 표가 아니다.
    # 이메일 값을 넘겨받는 프로젝트 함수들(normalize(payload.email) 의 normalize). 이 함수 안에서 매개변수에 strip·lower를 쓰면
    # 이름에 email이 없어도 이메일을 정리하는 코드로 센다. ("정리 코드가 어디에도 없다"고 잘못 단정해 FAIL로 만들지 않으려는 것이다.)
    email_helpers = {terminal_name(node.func) for module in modules.values() for node in ast.walk(module.tree)
                     if isinstance(node, ast.Call) and any(
                         isinstance(part, (ast.Name, ast.Attribute, ast.Subscript)) and email.search(terminal_name(part) or '')
                         for argument in [*node.args, *(keyword.value for keyword in node.keywords)] for part in ast.walk(argument))}
    owner_cache = {}  # 클래스·Table 호출 → 비밀번호 칸이 있는지 (같은 클래스를 반복해서 훑지 않으려고 기록)
    calls = []        # 모든 함수 호출(어느 표를 가입에 쓰는지 찾을 때 다시 훑는다)
    touched = set()   # 프로젝트 어딘가에서 이메일 값에 적용한 정리 종류("strip"/"lower"). 결과를 쓰는지와 상관없이 모은다.
    email_stores = []  # 이메일 이름의 속성에 값을 넣는 대입(user.email = ...). 사용자 표 객체에 넣는 것인지는 나중에 가린다.
    for module in modules.values():
        normalized |= _pydantic_normalization(module.tree, email)  # pydantic 설정으로 정리하는 경우도 인정
        parents = {child: parent for parent in ast.walk(module.tree) for child in ast.iter_child_nodes(parent)}
        loads = {}  # 이메일 검증기마다 정리 결과를 한 번만 계산해 두는 기록(_used_normalization이 쓴다).
        executed_sql = _executed_sql_strings(module.tree)  # 설명 글(docstring)이나 쓰이지 않는 글자는 SQL 근거로 인정하지 않는다.
        for node in ast.walk(module.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _email_validator(node, email):
                validator_found = True
            if isinstance(node, ast.ClassDef) and node.name in orm_classes and _stores_password(node, is_password):
                evidence.user_owners.add(('class', node.name))
                for item in node.body:  # __tablename__ = "users" 로 이 클래스가 가리키는 표의 이름을 알아 둔다.
                    if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__tablename__' for t in item.targets) \
                            and isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                        evidence.tablenames[item.value.value.lower()] = ('class', node.name)
            if isinstance(node, ast.Call):
                calls.append((module, node))
                if (terminal_name(node.func) or '') == 'Table' and _stores_password(node, is_password):
                    evidence.user_owners.add(('table', _table_call_name(node)))  # Table("users", metadata, Column(...), ...)
            # 비밀번호 칸이 없는 클래스·표 안의 선언(칸 정의·제약·인덱스)은 사용자 표에 대한 것이 아니므로 근거로 쓰지 않는다.
            # (strip·lower 같은 정리는 표와 상관없이 따로 본다: 검증기의 반환 경로·pydantic 설정·저장 값 따라가기로만 인정한다.)
            owner = (_declaring_owner(node, parents, is_password, owner_cache, orm_classes)
                     if isinstance(node, (ast.Assign, ast.AnnAssign, ast.Call)) else SKIP)
            if owner is not SKIP:
                bucket = evidence.bucket(owner)
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    # "email = Column(...)"처럼 이메일 이름의 변수에 DB 칸 정의를 대입한 경우
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and email.search(target.id) and isinstance(node.value, ast.Call):
                            _column_uniqueness(module, node.value, bucket['unique'], bucket['plain'])
                if isinstance(node, ast.Call):
                    name = terminal_name(node.func) or ''
                    # Column("email", ...)처럼 칸 이름을 글자로 준 경우
                    if name in ('Column', 'mapped_column') and node.args and isinstance(node.args[0], ast.Constant) \
                            and isinstance(node.args[0].value, str) and email.search(node.args[0].value):
                        _column_uniqueness(module, node, bucket['unique'], bucket['plain'])
                    # UniqueConstraint("email") : 이메일 칸 "하나"에 대한 중복 금지 제약
                    # UniqueConstraint("email", "tenant_id")나 UniqueConstraint("email", tenant_id)처럼 칸이 둘 이상인 복합 제약은
                    # tenant_id가 다르면 같은 이메일을 허용하므로 "이메일 중복 금지"의 근거가 되지 못한다.
                    # (복합이면 plain, 즉 "이메일만의 UNIQUE는 없음"으로 기록한다.) 칸은 글자뿐 아니라 칸 객체(User.email)일 수도 있어서
                    # 인자를 종류와 상관없이 전부 센다.
                    if name == 'UniqueConstraint':
                        hit, count, known = _constraint_columns(node.args, email)
                        if not known:
                            bucket['uncertain'].append(_obs(module, node, 'UNIQUE_COLUMNS_UNDETERMINED'))  # 어느 칸인지 모른다 → 단정하지 않는다
                        elif hit:
                            if count == 1:
                                bucket['unique'].append(_obs(module, node, 'UNIQUE_CONSTRAINT'))
                            else:
                                bucket['plain'].append(_obs(module, node, 'COMPOSITE_UNIQUE_NOT_EMAIL_ONLY'))
                    # Index("이름", "email", unique=True) : 이메일 칸 하나에 대한 중복 금지 인덱스 (복합 인덱스는 같은 이유로 제외)
                    if name == 'Index' and _true_keyword(node, 'unique'):
                        hit, count, known = _constraint_columns(node.args[1:], email)
                        if hit and any('where' in (keyword.arg or '') for keyword in node.keywords):
                            # postgresql_where=... 처럼 조건을 붙인 부분 인덱스는 일부 행에서만 중복을 막는다. 이메일 전체의 중복 금지가 아니다.
                            bucket['uncertain'].append(_obs(module, node, 'PARTIAL_UNIQUE_INDEX'))
                        elif not known:
                            bucket['uncertain'].append(_obs(module, node, 'UNIQUE_COLUMNS_UNDETERMINED'))
                        elif hit:
                            if count == 1:
                                bucket['unique'].append(_obs(module, node, 'UNIQUE_INDEX'))
                            else:
                                bucket['plain'].append(_obs(module, node, 'COMPOSITE_UNIQUE_NOT_EMAIL_ONLY'))
            if isinstance(node, ast.Call):
                # something.strip() / .lower() / .casefold() 호출이 이메일 값에 적용되었는지 본다.
                #   · touched: 프로젝트 어딘가에서 쓰인 정리 종류(결과를 쓰는지와 상관없음). "정리가 어디에도 없다"고 FAIL로 단정할 때만 쓴다.
                #   · 이메일 필드 검증기 안의 호출이면 그 검증기의 모든 반환 경로를 확인해 정리 근거(normalized)로 삼는다.
                if isinstance(node.func, ast.Attribute) and node.func.attr in ('strip', 'lower', 'casefold') \
                        and _touches_email(node, parents, email, email_helpers - {None}):
                    touched.add('lower' if node.func.attr == 'casefold' else node.func.attr)
                if isinstance(node.func, ast.Attribute) and node.func.attr in ('strip', 'lower', 'casefold') \
                        and _touches_email(node, parents, email):
                    steps = _used_normalization(node, parents, loads, email)
                    if steps is None:
                        normalization_unknown = True
                    else:
                        normalized |= steps
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Attribute) \
                    and isinstance(node.targets[0].value, ast.Name) and email.search(node.targets[0].attr):
                email_stores.append((module, node))
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) in executed_sql:
                # 실제로 실행하는 SQL 글자 안의 이메일 칸 정의도 살펴본다.
                _sql_email(module, node, email, evidence, is_password)
    # 가입으로 사용자를 저장하는 표 찾기: 사용자 표 클래스를 이메일·저장 칸 값과 함께 만드는 곳(User(email=..., password_hash=...))
    class_names = {name for kind, name in evidence.user_owners if kind == 'class'}
    for _module, call in calls:
        if terminal_name(call.func) in class_names and any(
                keyword.arg and (email.search(keyword.arg) or keyword.arg in storage_names) for keyword in call.keywords):
            evidence.targets.add(('class', terminal_name(call.func)))
    kind, observations = evidence.decide()
    if kind == 'unclear':
        # 비밀번호를 저장하는 표가 둘 이상인데 이메일 UNIQUE가 어떤 표에만 있다. 가입이 어느 표를 쓰는지 확신할 수 없다.
        return CheckOutcome(UNVERIFIED, 'EMAIL_UNIQUE_TABLE_UNCLEAR', observations)
    if kind == 'undetermined':
        # 칸 목록을 코드만으로 알 수 없는 중복 금지 제약이 있다. 그 안에 이메일이 있을 수도 있어서 "UNIQUE가 없다"고 단정할 수 없다.
        return CheckOutcome(UNVERIFIED, 'EMAIL_UNIQUE_UNDETERMINED', observations)
    if kind == 'plain':
        return CheckOutcome(FAIL, 'EMAIL_UNIQUE_MISSING', observations)  # 이메일 칸은 있는데 UNIQUE가 없다.
    if kind == 'none':
        return CheckOutcome(UNVERIFIED, 'EMAIL_COLUMN_NOT_FOUND')        # 이메일 칸 자체를 찾지 못했다.
    unique = observations
    required = set(rules['requireNormalization'])
    missing = [step for step in rules['requireNormalization'] if step not in normalized]  # 빠진 정리 방법
    if not missing:
        return CheckOutcome(PASS, 'STATIC_DECLARATION', unique)
    # 검증기·pydantic 설정만으로 모자라면, 가입으로 사용자를 저장하는 곳(User(email=...), user.email = ...)마다
    # 그 이메일 값이 실제로 거친 정리 단계를 따라간다. 모든 저장 곳에서 필요한 정리가 확인되면 PASS이다.
    # 예) email = payload.email.strip().lower(); User(email=email) / User(email=normalize_email(payload.email))
    sites = _email_storage_sites(class_names, calls, email_stores, email)
    flow = _email_flow(modules, _hash_context(modules, criteria['hashPolicy']), criteria['exposure']['routeMethods'])
    if sites:
        site_steps = [(module, node, flow.steps(module, node, value)) for module, node, value in sites]
        if all(required <= normalized | steps for _module, _node, steps in site_steps):
            return CheckOutcome(PASS, 'STATIC_DECLARATION', unique + [
                _obs(module, node, 'NORMALIZED_BEFORE_STORAGE') for module, node, _steps in site_steps][:MAX_OBSERVATIONS])
    if normalization_unknown:
        return CheckOutcome(UNVERIFIED, 'EMAIL_NORMALIZATION_UNDETERMINED', unique)
    # FAIL로 단정하는 경우는 세 가지뿐이다.
    #   · 이메일 필드에 등록된 검증기가 정리하지 않은 값을 돌려준다(검증기의 모든 반환 경로를 확인했다).
    #   · 빠진 정리 방법(strip 또는 lower)이 프로젝트 어디에서도 이메일 값에 쓰이지 않는다. (아무 데서도 하지 않으니 저장 값도 정리되지 않았다.)
    #   · 가입 경로 함수가 받은 입력을 그대로 저장한다(User(email=payload.email)이고 그 함수 안에서 그 필드를 바꾸는 곳이 없다).
    #     정리한 값을 계산만 하고 원래 값을 저장하는 코드가 여기에 해당한다.
    # 그 밖에는 정리 코드가 있지만 저장되는 값과 연결되는지 증명하지 못한 경우라서 UNVERIFIED로 둔다.
    raw_sites = [_obs(module, node, 'RAW_EMAIL_STORED') for module, node, value in sites if flow.raw(module, node, value)]
    if validator_found or any(step not in touched for step in missing) or raw_sites:
        return CheckOutcome(FAIL, 'EMAIL_NORMALIZATION_MISSING',
                            unique + [Observation(unique[0].path, unique[0].line, 'NORMALIZATION_MISSING',
                                                  {'missing': missing})] + raw_sites[:MAX_OBSERVATIONS])
    return CheckOutcome(UNVERIFIED, 'EMAIL_NORMALIZATION_UNDETERMINED', unique)


def _email_storage_sites(class_names, calls, email_stores, email):
    # 가입으로 사용자를 저장할 수 있는 곳의 "이메일 값"들을 [(파일, 위치 노드, 이메일 값 식)]으로 모은다.
    #   · 사용자 표 클래스를 만드는 호출의 이메일 인자: User(email=값, password_hash=...)
    #   · 이메일 이름의 속성에 값을 넣는 대입: user.email = 값. 어느 객체인지 가리지 않고 모두 넣는다
    #     (저장 곳을 빠뜨리면 정리되지 않은 저장을 놓친 채 PASS할 수 있어서, 넉넉하게 모으는 쪽을 택한다).
    # 이메일 인자 없이 펼쳐 넘기면(User(**data)) 어떤 값인지 모르므로 None(따라갈 수 없음)으로 넣는다.
    # 따옴표 글자처럼 사용자가 보낸 값이 아닌 이메일(관리자 계정 시드 등)은 가입 저장이 아니라서 뺀다.
    sites = []
    for module, call in calls:
        if terminal_name(call.func) not in class_names:
            continue
        values = [keyword.value for keyword in call.keywords if keyword.arg and email.search(keyword.arg)]
        if values:
            sites.extend((module, call, value) for value in values if not isinstance(value, ast.Constant))
        elif any(keyword.arg is None for keyword in call.keywords):
            sites.append((module, call, None))
    sites.extend((module, node, node.value) for module, node in email_stores if not isinstance(node.value, ast.Constant))
    return sites


def _email_flow(modules, ctx, route_methods=()):
    # 저장하는 이메일 값이 "확실히" 거친 정리 단계({"strip", "lower"})를 따라가는 함수를 돌려준다. PASS 근거로만 쓴다.
    # 모르면 빈 집합(정리를 증명하지 못함)이다. 그래서 틀리더라도 정리를 덜 인정하는 쪽(UNVERIFIED·FAIL 쪽)으로만 틀린다.
    #   · value.strip().lower()            → 앞의 값 + strip, lower
    #   · 이름                              → 모든 대입이 함수 본문 바로 아래에 차례로 있으면 읽는 곳 바로 앞의 마지막 대입의 값,
    #                                          그렇지 않으면 그 이름에 값이 묶이는 모든 곳(매개변수 포함)의 공통 단계
    #   · 매개변수                          → 프로젝트 안의 모든 호출에서 넘긴 값의 공통 단계 (프레임워크가 부르는 함수·값으로 넘겨진 함수는 정리되지 않은 입력)
    #   · 프로젝트 함수의 결과(normalize(x)) → 그 함수의 모든 반환 경로의 공통 단계(넘긴 값의 단계를 이어 받는다)
    #   · payload.email                    → 이 함수 본문 바로 아래에서 먼저 payload.email = 값 으로 한 번만 바꿨고 다른 쓰기가 없으면 그 값
    #   · 조건식·and·or                     → 모든 갈래의 공통 단계
    #   · 프로젝트가 strip·lower·casefold라는 이름의 메서드를 직접 정의했으면 그 호출은 정리로 인정하지 않는다.
    enclosing, call_sites, references = {}, {}, {}
    for module in modules.values():
        table = _enclosing_functions(module.tree)
        enclosing[module.path] = table
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
                call_sites.setdefault(terminal_name(node.func), []).append((node, table.get(id(node))))
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                references[node.id] = references.get(node.id, 0) + 1
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                references[node.attr] = references.get(node.attr, 0) + 1
    memo, keep_alive, parents_of, work = {}, [], {}, [0]

    def tick():
        work[0] += 1
        if work[0] > WORK_LIMIT:
            raise AnalysisTooComplex()

    def steps(expr, scope, depth=0, visiting=frozenset()):
        tick()
        if isinstance(expr, ast.Await):
            return steps(expr.value, scope, depth, visiting)
        if isinstance(expr, ast.Call):
            func = expr.func
            if isinstance(func, ast.Attribute) and func.attr in ('strip', 'lower', 'casefold') and not expr.args \
                    and not expr.keywords and func.attr not in ctx.defined_names:
                return steps(func.value, scope, depth, visiting) | {'lower' if func.attr == 'casefold' else func.attr}
            definitions = ctx.functions.get(terminal_name(func) or '', []) if depth < 4 else []
            if not definitions:
                return frozenset()  # 모르는 함수의 결과는 정리되었다고 증명할 수 없다.
            results = []
            for _owner, helper in definitions:
                start = {parameter: steps(argument, scope, depth + 1, visiting)
                         for parameter, argument in _bound_arguments(expr, helper) if parameter not in ('self', 'cls')}
                results.append(frozenset(_returned_normalization(helper, start) or ()))
            return frozenset.intersection(*results)
        if isinstance(expr, ast.IfExp):
            return steps(expr.body, scope, depth, visiting) & steps(expr.orelse, scope, depth, visiting)
        if isinstance(expr, ast.BoolOp):
            return frozenset.intersection(*[steps(part, scope, depth, visiting) for part in expr.values])
        if isinstance(expr, ast.Name):
            return name_steps(expr, scope, depth, visiting)
        if isinstance(expr, ast.Attribute):
            return field_steps(expr, scope, depth, visiting)
        return frozenset()

    def top_index(node, scope):
        # node가 들어 있는 "함수 본문 바로 아래 문장"의 순서 번호. 함수 본문 밖(안쪽 함수 등)이면 None.
        if id(scope) not in parents_of:
            parents_of[id(scope)] = {child: parent for parent in ast.walk(scope) for child in ast.iter_child_nodes(parent)}
        parents = parents_of[id(scope)]
        top = node
        while parents.get(top) is not None and parents.get(top) is not scope:
            top = parents[top]
        body = getattr(scope, 'body', [])
        return next((index for index, statement in enumerate(body) if statement is top), None)

    def name_steps(node, scope, depth, visiting):
        name = node.id
        if scope is None:
            return frozenset()
        pairs = [(target, value, statement) for statement in ast.walk(scope) for target, value in _assignment_pairs(statement)
                 if isinstance(target, ast.Name) and target.id == name]
        simple = [(value, statement) for _target, value, statement in pairs if isinstance(statement, (ast.Assign, ast.AnnAssign))]
        own = 1 if name in _parameter_names(scope) else 0
        if len(simple) != len(pairs) or _name_bindings(scope).get(name, 0) != len(simple) + own or not (simple or own):
            return frozenset()  # for·with·+= 같은 다른 묶임이 있으면 어떤 값이 들어오는지 모두 알 수 없다.
        body = getattr(scope, 'body', [])
        position = top_index(node, scope)
        if position is not None and all(statement in body for _value, statement in simple):
            # 모든 대입이 함수 본문 바로 아래(조건·반복 밖)에 차례로 있으면, 읽는 곳 바로 앞의 마지막 대입이 그 값이다.
            # 예) email = payload.email.strip(); email = email.lower() → 두 번째 대입 오른쪽의 email은 첫 번째 대입의 값이다.
            earlier = [(value, statement) for value, statement in simple if body.index(statement) < position]
            if earlier:
                return steps(earlier[-1][0], scope, depth, visiting)
            return parameter_steps(name, scope, depth, visiting) if own else frozenset()
        # 그 밖에는 이 이름에 묶이는 모든 값(매개변수 포함)의 공통 단계만 인정한다.
        key = (id(scope), name)
        if key in visiting:
            return frozenset()  # 서로를 거쳐 정해지는 이름(순환)은 정리를 증명하지 못한다.
        if key in memo:
            return memo[key]
        inner = visiting | {key}
        parts = [steps(value, scope, depth, inner) for value, _statement in simple]
        if own:
            parts.append(parameter_steps(name, scope, depth, inner))
        memo[key] = frozenset.intersection(*parts)
        return memo[key]

    def parameter_steps(name, scope, depth, visiting):
        # 매개변수의 값은 이 함수를 부르는 모든 곳에서 넘긴 값이다.
        if depth >= 4 or scope.decorator_list or not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return frozenset()  # 데코레이터가 붙은 함수(경로 함수 등)는 프레임워크가 정리되지 않은 입력으로 부른다.
        sites = call_sites.get(scope.name, [])
        if not sites or references.get(scope.name, 0) != len(sites):
            return frozenset()  # 부르는 곳이 없거나, 함수를 값으로 넘겨서(콜백·등록) 보이지 않는 곳에서도 불릴 수 있다.
        parts = []
        for call, caller in sites:
            bound = [argument for parameter, argument in _bound_arguments(call, scope) if parameter == name]
            if len(bound) != 1:
                return frozenset()  # 기본값을 쓰거나 *값들·**설정으로 넘기면 어떤 값인지 알 수 없다.
            parts.append(steps(bound[0], caller, depth + 1, visiting))
        return frozenset.intersection(*parts)

    def field_steps(expr, scope, depth, visiting):
        # payload.email처럼 매개변수의 필드를 읽는 경우. 이 함수가 그 필드를 먼저 한 번만 바꿔 두었다면 그 값을 따른다.
        base, chain = _chain(expr)
        if scope is None or not isinstance(base, ast.Name) or base.id not in _parameter_names(scope) \
                or any(kind != 'attr' for kind, _name in chain) or _name_bindings(scope).get(base.id, 0) != 1:
            return frozenset()
        path = tuple(name for _kind, name in chain)
        stores = [statement for statement in scope.body if isinstance(statement, ast.Assign) and len(statement.targets) == 1
                  and _chain(statement.targets[0])[0].__class__ is ast.Name
                  and _chain(statement.targets[0])[0].id == base.id
                  and tuple(name for _kind, name in _chain(statement.targets[0])[1]) == path]
        if len(stores) != 1:
            return frozenset()
        store = stores[0]
        position = top_index(expr, scope)
        if position is None or scope.body.index(store) >= position:
            return frozenset()  # 정리한 대입이 읽기보다 먼저 실행된다는 보장이 없다.
        # 그 대입을 뺀 함수 사본에서 같은 필드를 바꾸는 곳(도우미 함수·별칭·setattr 등)이 있으면 어느 값이 남는지 알 수 없다.
        rest = copy.copy(scope)
        rest.body = [statement for statement in scope.body if statement is not store] or [ast.Pass()]
        keep_alive.append(rest)  # 분석하는 동안 사본의 id가 다른 객체에 다시 쓰이지 않게 붙잡아 둔다.
        changed = _mutated_fields(rest, ctx).get(base.id, ())
        if any(path[:len(item)] == item or item[:len(path)] == path for item in changed):
            return frozenset()
        return steps(store.value, scope, depth, visiting)

    def flow(module, node, value):
        if value is None:
            return frozenset()
        return steps(value, enclosing[module.path].get(id(node)))

    def raw(module, node, value):
        # 이 저장 곳이 "가입 경로 함수가 받은 입력을 그대로" 저장하는가? (정리하지 않은 저장이라고 증명할 수 있는 경우만 True)
        #   · 경로 데코레이터(@router.post 등)가 붙은 함수이고, 저장하는 값이 그 함수 매개변수(또는 그 필드)를 그대로 읽은 것이다.
        #   · 매개변수를 다른 값으로 다시 묶지 않았고, 그 필드를 바꾸는 곳(대입·도우미·별칭·setattr 등)이 함수 안에 없다.
        #   · Depends(...)로 받은 매개변수는 다른 함수가 만든 값이라서 제외한다.
        scope = enclosing[module.path].get(id(node))
        if value is None or not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) or not any(
                isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr in route_methods
                for decorator in scope.decorator_list):
            return False
        base, chain = _chain(value)
        if not isinstance(base, ast.Name) or base.id not in _parameter_names(scope) or any(kind != 'attr' for kind, _name in chain) \
                or _name_bindings(scope).get(base.id, 0) != 1:
            return False
        arguments = scope.args
        positional = [*arguments.posonlyargs, *arguments.args]
        defaults = dict(zip([argument.arg for argument in reversed(positional)], reversed(arguments.defaults)))
        defaults.update({argument.arg: default for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults) if default})
        default = defaults.get(base.id)
        if isinstance(default, ast.Call) and terminal_name(default.func) in ('Depends', 'Security'):
            return False
        path = tuple(name for _kind, name in chain)
        changed = _mutated_fields(scope, ctx).get(base.id, ())
        return not any(path[:len(item)] == item or item[:len(path)] == path for item in changed)

    flow.steps, flow.raw = flow, raw
    return flow


def _constraint_columns(args, email):
    # 중복 금지 제약·인덱스에 위치로 넘긴 "칸" 인자들을 살펴본다. 돌려주는 값: (이메일 칸이 들어 있는가, 칸의 개수, 칸을 모두 알 수 있는가)
    #   칸은 글자("email")일 수도, 칸 객체(User.email, email)일 수도 있다. 종류와 상관없이 모든 위치 인자를 센다.
    #   (글자 인자만 세면 UniqueConstraint("email", tenant_id)가 "칸 1개"로 잘못 읽힌다.)
    #   *칸목록이나 func.lower(email)처럼 어떤 칸인지 읽을 수 없는 인자가 하나라도 있으면 세 번째 값이 False(모름)이다.
    #   이름 붙인 인자(name="uq")는 칸이 아니라서 args에 들어 있지 않다.
    hit, known = False, True
    for arg in args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            hit = hit or bool(email.search(arg.value))
        elif isinstance(arg, (ast.Name, ast.Attribute)):
            hit = hit or bool(email.search(terminal_name(arg) or ''))
        else:
            known = False
    return hit, len(args), known


def _email_validator(function, email):
    # 이 함수가 이메일 필드에 등록된 pydantic 검증기인가? @field_validator("email")·@validator("email")처럼 데코레이터의 필드 이름으로
    # 판단하고 함수 이름은 보지 않는다. (이름이 normalize_email이어도 등록되지 않은 함수는 검증기가 아니고, def clean(cls, v)도 등록되면 검증기다.)
    return any(isinstance(decorator, ast.Call) and terminal_name(decorator.func) in ('validator', 'field_validator')
               and any(isinstance(arg, ast.Constant) and isinstance(arg.value, str) and email.search(arg.value)
                       for arg in decorator.args) for decorator in function.decorator_list)


def _used_normalization(call, parents, cache, email):
    # 이 정리 호출이 이메일 필드 검증기 안에 있으면, 그 검증기의 모든 반환 경로가 거친 정리 단계를 돌려준다(검증기마다 한 번만 계산).
    # 정리한 뒤 원문으로 되돌리거나 검사에만 쓴 값은 근거가 아니다. 분석할 수 없는 모양이면 None(모름)이다.
    scope = parents.get(call)
    while scope is not None and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        scope = parents.get(scope)
    if scope is not None and _email_validator(scope, email):
        key = ('normalization', id(scope))
        if key not in cache:
            cache[key] = _returned_normalization(scope)
        return cache[key]
    # 검증기 밖의 정리는 여기서 세지 않는다. 경로 함수·도우미 함수의 정리는 저장 값을 따라가는 _email_flow에서 따로 확인한다.
    return set()


def _returned_normalization(function, initial=None):
    # 변수별 정리 상태를 문장 순서대로 옮긴다. 분기는 교집합으로 합치므로 한 경로라도 원문을 반환하면 정리를 보장하지 않는다.
    # 반복·예외 처리·숨은 대입은 이 분석의 지원 범위 밖이다. None은 누락 확정이 아니라 UNVERIFIED를 뜻한다.
    # initial은 {매개변수 이름: 호출한 쪽에서 이미 거친 정리 단계}이다. 없으면 매개변수는 정리되지 않은 값으로 시작한다.
    work = 0

    def tick():
        nonlocal work
        work += 1
        if work > WORK_LIMIT:
            raise AnalysisTooComplex()

    def value_steps(value, env):
        tick()
        if isinstance(value, ast.Name):
            return env.get(value.id, frozenset())
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and not value.args and not value.keywords:
            method = value.func.attr
            if method in ('strip', 'lower', 'casefold'):
                return value_steps(value.func.value, env) | {'lower' if method == 'casefold' else method}
        if isinstance(value, ast.IfExp):
            return value_steps(value.body, env) & value_steps(value.orelse, env)
        return frozenset()

    def block(statements, env):
        returned = []
        for statement in statements:
            tick()
            if isinstance(statement, ast.Return):
                return None, returned + [value_steps(statement.value, env)]
            if isinstance(statement, ast.Raise):
                return None, returned
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                pairs = _assignment_pairs(statement)
                values = [(target, value_steps(value, env)) for target, value in pairs]
                for target, steps in values:
                    if not isinstance(target, ast.Name):
                        raise ValueError('unsupported normalization assignment')
                    env[target.id] = steps
            elif isinstance(statement, ast.If):
                if any(isinstance(node, ast.NamedExpr) for node in ast.walk(statement.test)):
                    raise ValueError('unsupported normalization condition')
                left, left_returns = block(statement.body, dict(env))
                right, right_returns = block(statement.orelse, dict(env))
                returned.extend(left_returns + right_returns)
                if left is None and right is None:
                    return None, returned
                if left is None or right is None:
                    env = right if left is None else left
                else:
                    env = {key: left.get(key, frozenset()) & right.get(key, frozenset()) for key in left.keys() | right.keys()}
            elif isinstance(statement, ast.AugAssign):
                if not isinstance(statement.target, ast.Name):
                    raise ValueError('unsupported normalization mutation')
                env[statement.target.id] = frozenset()
            elif isinstance(statement, (ast.Expr, ast.Pass)):
                if any(isinstance(node, ast.NamedExpr) for node in ast.walk(statement)):
                    raise ValueError('unsupported normalization expression')
            else:
                raise ValueError('unsupported normalization statement')
        return env, returned

    try:
        remaining, returned = block(function.body, dict(initial or {}))
    except ValueError:
        return None
    if remaining is not None:
        returned.append(frozenset())  # 반환 없이 끝나는 경로도 정리된 값을 내놓지 못한다.
    return set.intersection(*(set(steps) for steps in returned)) if returned else set()


def _stores_password(owner, is_password):
    # 이 클래스(또는 Table(...) 호출) 안에 비밀번호 칸이 있는가? 예) password_hash = Column(...) / Column("password_hash", String)
    for item in ast.walk(owner):
        if isinstance(item, (ast.Assign, ast.AnnAssign)) and item is not owner:
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            if any(isinstance(target, ast.Name) and is_password(target.id) for target in targets):
                return True
        if isinstance(item, ast.Call) and (terminal_name(item.func) or '') in ('Column', 'mapped_column') and item.args \
                and isinstance(item.args[0], ast.Constant) and isinstance(item.args[0].value, str) and is_password(item.args[0].value):
            return True
    return False


def _table_call_name(call):
    # Table("users", metadata, ...) 호출에서 표 이름(소문자)을 꺼낸다. 이름이 글자가 아니면 "?"이다.
    first = call.args[0] if call.args else None
    return first.value.lower() if isinstance(first, ast.Constant) and isinstance(first.value, str) else '?'


def _declaring_owner(node, parents, is_password, cache, orm_classes):
    # 이 선언(칸 정의·제약·인덱스)이 어느 표의 것인지 돌려준다.
    #   · 가장 가까운 바깥 클래스나 Table(...) 호출에 비밀번호 칸이 있으면 그 표(사용자 표)의 소유자 이름표: ("class", 이름) / ("table", 이름)
    #   · 비밀번호 칸이 없는 클래스·표이거나, DB 표가 아닌 클래스(요청 모델 등)이면 SKIP (사용자 표가 아니라서 근거로 쓰지 않는다)
    #   · 바깥에 클래스나 Table이 없으면 어느 표인지 알 수 없으므로 None
    current = parents.get(node)
    while current is not None:
        is_class = isinstance(current, ast.ClassDef)
        if is_class or (isinstance(current, ast.Call) and (terminal_name(current.func) or '') == 'Table'):
            if id(current) not in cache:
                cache[id(current)] = (not is_class or current.name in orm_classes) and _stores_password(current, is_password)
            if not cache[id(current)]:
                return SKIP
            return ('class', current.name) if is_class else ('table', _table_call_name(current))
        current = parents.get(current)
    return None


def _executed_sql_strings(tree):
    # 이 파일에서 "실제로 실행하는 SQL"로 쓰이는 글자 상수(조각)의 id 집합을 돌려준다.
    #   · db.execute("CREATE TABLE ...") / text("...") 처럼 실행 함수의 첫 인자로 직접 넘긴 글자
    #   · SCHEMA = "CREATE TABLE ..." 로 이름에 담았고, 그 이름을 실행 함수의 첫 인자로 넘기는 경우 (conn.executescript(SCHEMA))
    # 함수 설명 글(docstring)이나 주석 대신 적어 둔 글, 어디에도 쓰이지 않는 글자는 SQL이 실행된다는 증거가 아니다.
    # 이런 글자 속의 "CREATE TABLE users (email TEXT UNIQUE ...)"를 UNIQUE 근거로 인정하면 가짜 근거로 PASS를 만들 수 있다.
    runners = {'execute', 'executemany', 'executescript', 'exec_driver_sql', 'text'}
    direct, used_names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (terminal_name(node.func) or '') in runners and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                direct.add(id(first))
            elif isinstance(first, ast.Name):
                used_names.add(first.id)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id in used_names for target in targets):
                direct.add(id(node.value))
    return direct


def _sql_columns(text):
    # "(email, tenant_id)" 안의 칸 이름들을 쉼표로 나눠 목록으로 돌려준다. 예) "email, tenant_id" → ["email", "tenant_id"]
    # 이름이 아니라 계산식인 칸(lower(email), substr(email, 1, 3))은 "?"로 돌려준다. 이메일 전체를 보호하는지 알 수 없기 때문이다.
    columns = []
    for column in (item.strip() for item in re.split(r',(?![^(]*\))', text)):
        if not column:
            continue
        simple = re.fullmatch(r'[\w."`\[\]]+(?:\s+(?:asc|desc|collate\s+\w+|nulls\s+(?:first|last)))*', column)
        columns.append(_sql_name(column.split()[0]) if simple else '?')
    return columns


def _sql_name(token):
    # SQL의 표·칸 이름에서 따옴표와 스키마 이름을 뗀다. 예) "public"."users" → users
    return token.strip().strip('`"\'[]').rsplit('.', 1)[-1].strip('`"\'[]')


def _sql_clean(text):
    # SQL 글자에서 주석(-- ..., /* ... */)을 지우고 따옴표 글자('...')의 내용을 비운다. 소문자로도 바꾼다.
    # 이렇게 하지 않으면 DEFAULT 'unique'나 -- unique 같은 주석·문자열 안의 단어를 UNIQUE 제약으로 오인한다.
    # 큰따옴표·역따옴표·대괄호로 감싼 이름(식별자)은 그대로 둔다.
    out, index, size = [], 0, len(text)
    while index < size:
        char = text[index]
        if text.startswith('--', index):
            end = text.find('\n', index)
            index = size if end < 0 else end
            out.append(' ')
        elif text.startswith('/*', index):
            end = text.find('*/', index + 2)
            index = size if end < 0 else end + 2
            out.append(' ')
        elif text.startswith('$$', index):
            end = text.find('$$', index + 2)
            index = size if end < 0 else end + 2
            out.append("''")
        elif char == "'":
            end = index + 1
            while end < size:
                if text[end] == "'":
                    if text.startswith("''", end):
                        end += 2  # '' 는 따옴표 글자 하나를 뜻하는 표기이다.
                        continue
                    break
                end += 1
            out.append("''")
            index = end + 1
        elif char in '"`[':
            end = text.find(']' if char == '[' else char, index + 1)
            end = size - 1 if end < 0 else end
            out.append(text[index:end + 1])
            index = end + 1
        else:
            out.append(char)
            index += 1
    return ''.join(out).lower()


def _sql_create_tables(text):
    # 정리된 SQL 글자에서 CREATE TABLE 문들을 읽어 [(표 이름, [(칸 이름, 칸 정의)], [(제약 종류, 칸 목록)], 끝까지 읽었는가)]로 돌려준다.
    # 괄호 안을 "맨 바깥 쉼표"로 나눠서 칸 하나하나를 따로 본다. (한 줄에 칸이 여러 개 있어도 칸별로 구분하기 위해서이다.)
    # 괄호가 맞지 않아 끝까지 읽지 못하면 마지막 값이 False이다. 이런 SQL은 단정하지 않고 확인 못 함으로 둔다.
    tables = []
    for match in re.finditer(r'create\s+(?:temp(?:orary)?\s+)?table\s+(?:if\s+not\s+exists\s+)?([^\s(]+)\s*\(', text):
        depth, index = 1, match.end()
        while index < len(text) and depth:
            depth += (text[index] == '(') - (text[index] == ')')
            index += 1
        complete = depth == 0
        body = text[match.end():index - 1 if complete else index]
        parts, depth, current = [], 0, ''
        for char in body:
            depth += (char == '(') - (char == ')')
            if char == ',' and depth == 0:
                parts.append(current)
                current = ''
            else:
                current += char
        parts.append(current)
        columns, constraints = [], []
        for part in (' '.join(item.split()) for item in parts):
            if not part:
                continue
            # UNIQUE (칸), UNIQUE KEY 이름 (칸), CONSTRAINT 이름 UNIQUE (칸), PRIMARY KEY (칸)
            kind = re.match(r'(?:constraint\s+\S+\s+)?(unique|primary\s+key)\b(?:\s+(?:key|index))?(?:\s+[^\s(]+)?\s*\(([^)]*)\)', part)
            if kind:
                constraints.append((' '.join(kind.group(1).split()), _sql_columns(kind.group(2))))
            elif re.match(r'(?:constraint\s+\S+\s+)?(unique|primary\s+key)\b', part):
                complete = False  # 칸 목록을 읽지 못하는 UNIQUE·PRIMARY KEY 제약: 이메일이 들어 있는지 알 수 없다.
            elif not re.match(r'(?:constraint\s+\S+\s+)?(foreign\s+key|check)\b', part):
                name, _, definition = part.partition(' ')
                columns.append((_sql_name(name), definition))
        tables.append((_sql_name(match.group(1)), columns, constraints, complete))
    return tables


def _sql_email(module, node, email, evidence, is_password):
    # SQL 문장이 들어 있는 글자에서 이메일 칸 정의를 찾는다. "사용자 표"(비밀번호 칸이 있는 표)의 이메일 칸 "하나"만 보호하는 UNIQUE만 인정한다.
    #   CREATE TABLE ... 안의 이메일 칸 정의(그 칸 자신의 정의)에 UNIQUE나 PRIMARY KEY가 있으면 unique, 없으면 plain으로 기록한다.
    #     다른 칸(예: id INTEGER UNIQUE)의 UNIQUE나 문자열·주석 속 단어(DEFAULT 'unique')는 이메일과 상관없다.
    #     "UNIQUE (email, tenant_id)"처럼 칸이 둘 이상인 복합 제약은 unique가 아니라 plain이다.
    #   CREATE UNIQUE INDEX ... ON 표(칸...) 문은 표 이름을 evidence.indexes에 모아 두었다가 나중에 사용자 표인지 확인한다.
    #   읽을 수 없는 SQL(괄호가 맞지 않음, ALTER TABLE로 칸을 바꿈)에 이메일이 나오면 단정하지 않고 확인 못 함으로 둔다.
    if not isinstance(node.value, str) or not re.search(r'create|alter|insert', node.value, re.IGNORECASE):
        return
    text = _sql_clean(node.value)
    for match in re.finditer(r'insert\s+(?:or\s+\w+\s+)?into\s+([^\s(]+)', text):
        evidence.targets.add(('table', _sql_name(match.group(1))))  # 이 표에 새 사용자를 저장한다.
    for match in re.finditer(r'create\s+unique\s+index[^()]*?\bon\s+(?:only\s+)?(\S+?)\s*(?:using\s+\w+\s*)?\(([^)]*(?:\([^)]*\)[^)]*)*)\)([^;]*)', text):
        columns = _sql_columns(match.group(2))
        mentions_email = any(email.search(column) for column in columns) or email.search(match.group(2))
        if re.search(r'\bwhere\b', match.group(3)) and mentions_email:
            # WHERE 조건이 붙은 부분 인덱스는 일부 행에서만 중복을 막는다. 이메일 전체의 중복 금지가 아니다.
            evidence.bucket(None)['uncertain'].append(_obs(module, node, 'PARTIAL_UNIQUE_INDEX', table=_sql_name(match.group(1))))
        elif '?' in columns and mentions_email:
            evidence.bucket(None)['uncertain'].append(_obs(module, node, 'UNIQUE_COLUMNS_UNDETERMINED', table=_sql_name(match.group(1))))
        elif any(email.search(column) for column in columns):
            evidence.indexes.append((module, node, _sql_name(match.group(1)), columns))
    for match in re.finditer(r'alter\s+table\s+(\S+)([^;]*)', text):
        if email.search(match.group(2)):
            evidence.bucket(None)['uncertain'].append(_obs(module, node, 'SQL_ALTER_NOT_ANALYZED', table=_sql_name(match.group(1))))
    for table, columns, constraints, complete in _sql_create_tables(text):
        if not any(is_password(column) for column, _definition in columns):
            continue  # 비밀번호 칸이 없는 표는 사용자 표가 아니다.
        owner = ('table', table)
        evidence.user_owners.add(owner)
        bucket = evidence.bucket(owner)
        if not complete:
            bucket['uncertain'].append(_obs(module, node, 'SQL_NOT_FULLY_READABLE', table=table))
        for column, definition in columns:
            if email.search(column):
                protected = re.search(r'\bunique\b|\bprimary\s+key\b', definition)
                bucket['unique' if protected else 'plain'].append(_obs(module, node, 'SQL_EMAIL_COLUMN', table=table))
        for kind, constrained in constraints:
            if kind in ('unique', 'primary key') and '?' in constrained:
                bucket['uncertain'].append(_obs(module, node, 'UNIQUE_COLUMNS_UNDETERMINED', table=table))  # 계산식 칸은 알 수 없다.
            elif kind in ('unique', 'primary key') and any(email.search(column) for column in constrained):
                if len(constrained) == 1:
                    bucket['unique'].append(_obs(module, node, 'SQL_UNIQUE_CONSTRAINT', table=table))
                else:
                    bucket['plain'].append(_obs(module, node, 'COMPOSITE_UNIQUE_NOT_EMAIL_ONLY', table=table))


def _pydantic_normalization(tree, email):
    # pydantic 설정(str_strip_whitespace, str_to_lower, StringConstraints 등)으로 이메일을 정리하는지 찾는다.
    # 아무 곳에나 있는 설정은 인정하지 않고, 다음 두 곳에 있는 것만 인정한다.
    #   1) 이메일 필드를 가진 클래스 안
    #   2) 이름에 email이 들어간, 파일 맨 위의 타입 별칭 선언
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and any(
                isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and email.search(item.target.id)
                for item in node.body):
            for inner in ast.walk(node):
                found |= _normalization_options(inner)
    for statement in getattr(tree, 'body', []):
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if any(isinstance(target, ast.Name) and email.search(target.id) for target in targets):
                for inner in ast.walk(statement):
                    found |= _normalization_options(inner)
    return found


def _normalization_options(node):
    # 코드 조각 하나에서 "strip/lower 설정을 True로 켠 곳"을 찾아 {"strip", "lower"} 중 해당하는 것을 돌려준다.
    # 설정은 세 가지 모양으로 쓸 수 있어서 모두 확인한다.
    #   함수 호출 인자: ConfigDict(str_strip_whitespace=True)
    #   사전:           {"str_strip_whitespace": True}
    #   클래스 안 대입: str_strip_whitespace = True
    pairs = []  # (설정 이름, 값) 쌍들
    if isinstance(node, ast.Call):
        pairs = [(keyword.arg, keyword.value) for keyword in node.keywords if keyword.arg]
    elif isinstance(node, ast.Dict):
        pairs = [(key.value, value) for key, value in zip(node.keys, node.values)
                 if isinstance(key, ast.Constant) and isinstance(key.value, str)]
    elif isinstance(node, ast.Assign):
        pairs = [(target.id, node.value) for target in node.targets if isinstance(target, ast.Name)]
    found = set()
    for key, value in pairs:
        if isinstance(value, ast.Constant) and value.value is True:
            if key in STRIP_OPTIONS:
                found.add('strip')
            if key in LOWER_OPTIONS:
                found.add('lower')
    return found


def _true_keyword(call, name):
    # 함수 호출에 "name=True"라는 인자가 있는지 확인한다. 예) Column(String, unique=True)
    return any(keyword.arg == name and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
               for keyword in call.keywords)


def _column_uniqueness(module, call, unique, plain):
    # DB 칸을 정의하는 호출(Column, mapped_column, Field)이 중복 금지(unique=True) 또는 기본 키(primary_key=True)인지 기록한다.
    # 기본 키는 값이 겹칠 수 없으므로 unique와 같은 효과가 있다.
    name = terminal_name(call.func) or ''
    if name not in ('Column', 'mapped_column', 'Field'):
        return
    if _true_keyword(call, 'unique') or _true_keyword(call, 'primary_key'):
        unique.append(_obs(module, call, 'UNIQUE_COLUMN'))
    else:
        plain.append(_obs(module, call, 'EMAIL_COLUMN_WITHOUT_UNIQUE'))


def _touches_email(call, parents, email, helpers=frozenset()):
    # `.strip()`이나 `.lower()` 같은 호출이 "이메일 값"에 적용되었는지 판단한다.
    # 이메일에 쓴 것으로 인정하는 경우:
    #   1) 호출 대상 식에 이메일처럼 보이는 이름이 있다. 예) email.strip(), payload.email.lower()
    #   2) 이름에 email이 들어간 함수(예: normalize_email)나 이메일 필드에 등록된 검증기, 또는 helpers(이메일 값을 넘겨받는 함수) 안에서
    #      그 함수가 받은 값(매개변수)에 적용했다.
    # 이메일과 무관한 호출(예: "X".lower())은 인정하지 않는다.
    stack = [call.func.value]  # 호출 대상(점 앞부분)에서 시작해 안쪽 가지를 살펴본다.
    while stack:
        item = stack.pop()
        name = terminal_name(item) if isinstance(item, (ast.Name, ast.Attribute, ast.Subscript)) else None
        if name and email.search(name):
            return True
        stack.extend(ast.iter_child_nodes(item))
    # 2번 경우를 확인하려고, 호출 대상의 가장 안쪽 변수 이름(root)을 찾는다. 예) value.strip().lower() 의 value
    root = call.func.value
    while isinstance(root, (ast.Attribute, ast.Call, ast.Subscript)):
        root = root.func if isinstance(root, ast.Call) else root.value
    if not isinstance(root, ast.Name):
        return False
    node = parents.get(call)  # 이 호출을 감싸고 있는 함수들을 안쪽부터 바깥쪽으로 올라가며 본다. (호출마다 파일 전체를 훑으면 코드가 길수록 시간이 제곱으로 늘어난다.)
    while node is not None:
        # 이름에 email이 들어간 함수이거나, 이름과 상관없이 @field_validator("email")처럼 이메일 필드에 등록된 검증기이면
        # 그 함수가 받은 값은 이메일이다. (def clean(cls, v): return v.strip().lower() 같은 정상 검증기를 놓치지 않으려는 것이다.)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                email.search(node.name) or _email_validator(node, email) or node.name in helpers):
            params = {arg.arg for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]}
            if root.id in params - {'self', 'cls'}:
                return True
        node = parents.get(node)
    return False


# ---------------------------------------------------------------- REQ-006 비밀번호·해시 노출

def check_exposure(modules, criteria):
    # REQ-006: 비밀번호나 그 해시가 밖으로 새어 나가는 길이 있는가?
    # 새어 나갈 수 있는 길 네 가지를 살펴본다.
    #   ① 로그·출력(print, logger.info, 파일 write, 응답 헤더·쿠키 등)에 찍는다   ② 오류 메시지(raise ...)에 넣는다
    #   ③ API 응답으로 돌려준다                        ④ 입력 검증 오류 응답에 입력값(비밀번호)이 그대로 실린다
    # 새는 길을 찾으면 FAIL, 확신할 수 없는 곳이 있으면 UNVERIFIED, 못 찾았으면 PASS.
    rules = criteria['exposure']
    matcher = _sensitive(criteria)
    # 검증 오류 응답에 옮겨 담아도 되는 오류 항목의 키(위치, 안내 문구, 종류). input(사용자가 보낸 원래 값)은 포함하지 않는다.
    safe_keys = frozenset(rules.get('safeValidationErrorKeys', ('loc', 'msg', 'type')))
    fails, unresolved = [], []
    sensitive_models = _models_with_sensitive_fields(modules, matcher)  # 비밀번호 칸이 있는 입력·응답 모델들
    input_models = _models_with_sensitive_fields(modules, matcher, include_secret=True)  # 검증 오류 응답에 원문이 실릴 수 있는 모델들
    functions = _functions(modules)
    untrusted_names = _untrusted_function_names(modules)  # 다른 것으로 바뀔 수 있어서 믿을 수 없는 함수 이름들
    # 해시 함수의 결과를 담은 변수도 "비밀번호를 품은 변수"로 따라가기 위한 도구들(REQ-005와 같은 방법으로 해시 호출을 알아본다)
    ctx = _hash_context(modules, criteria['hashPolicy'])
    uses_fastapi, handler_found = False, False  # FastAPI를 쓰는가 / 검증 오류 처리기를 찾았는가
    filtering_models = _response_filter_models(modules)  # 추가 필드·직렬화 사용자화까지 확인한 Pydantic 모델만 믿는다.

    def check_registered_handler(module, node, handler):
        # RequestValidationError 처리기로 등록된 값(handler)을 분석한다. 등록 방법은 세 가지다.
        #   app.add_exception_handler(RequestValidationError, 처리기) / app.exception_handlers[RequestValidationError] = 처리기
        #   FastAPI(exception_handlers={RequestValidationError: 처리기})
        # 처리기가 소스에 적힌 함수 이름이면 그 함수들을 분석한다. 람다이거나, 모르는 이름이거나, 다른 것으로 바뀔 수 있는 이름이면
        # 어떤 코드가 실행되는지 알 수 없으므로 PASS 대신 UNVERIFIED로 남긴다.
        target = terminal_name(handler) if isinstance(handler, (ast.Name, ast.Attribute)) else None
        found = functions.get(target or '', [])
        for owner, function in found:
            _check_validation_handler(owner, function, fails, unresolved, safe_keys)
        if not found or target in untrusted_names:
            unresolved.append(_obs(module, node, 'VALIDATION_HANDLER_UNRESOLVED'))

    seen = set()  # 이미 살펴본 코드 조각들(같은 곳을 두 번 판정하지 않으려고 기록)
    for module in modules.values():
        logger_names, uncertain_loggers = _logger_bindings(module)
        # 함수마다 "비밀번호 필드를 가진 객체를 담은 변수"가 다르다. 그래서 안쪽 함수부터 차례로 살펴보고,
        # 각 코드 조각은 "가장 가까운 함수의 변수 목록"을 기준으로 딱 한 번만 판정한다.
        scopes = [(node, _sensitive_vars(node, sensitive_models, matcher, ctx))
                  for node in ast.walk(module.tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        scopes.reverse()
        scopes.append((module.tree, frozenset()))  # 마지막으로 함수 밖의 코드(파일 최상위)를 살펴본다.
        for scope, svars in scopes:
            for node in ast.walk(scope):
                if id(node) in seen:
                    continue
                seen.add(id(node))
                if isinstance(node, ast.Call):
                    name = qualname(node.func, module.aliases) or ''
                    if name == 'fastapi.FastAPI':
                        uses_fastapi = True
                    # ① 로그·출력 호출에 비밀번호를 넘기는가
                    if _is_output_call(node, name, rules, logger_names) and sensitive_refs(node, matcher, svars):
                        fails.append(_obs(module, node, 'SENSITIVE_LOGGED'))
                    elif isinstance(node.func, ast.Attribute) and node.func.attr in rules['logMethods'] \
                            and qualname(node.func.value, {}) in uncertain_loggers and sensitive_refs(node, matcher, svars):
                        unresolved.append(_obs(module, node, 'LOGGER_BINDING_UNRESOLVED'))
                    # ④ app.add_exception_handler(RequestValidationError, 처리기) 로 등록한 검증 오류 처리기를 찾는다.
                    if isinstance(node.func, ast.Attribute) and node.func.attr == 'add_exception_handler' \
                            and node.args and (terminal_name(node.args[0]) or '') == 'RequestValidationError':
                        handler_found = True
                        check_registered_handler(module, node, node.args[1] if len(node.args) > 1 else None)
                    # app.add_api_route("/signup", signup, response_model=...) 처럼 데코레이터 없이 경로 함수를 등록하는 방법.
                    # 데코레이터가 없어도 같은 기준으로 그 함수의 응답을 확인한다.
                    if isinstance(node.func, ast.Attribute) and node.func.attr in ('add_api_route', 'add_url_rule'):
                        endpoints = [*node.args[1:], *(k.value for k in node.keywords if k.arg in ('endpoint', 'view_func'))]
                        for endpoint in endpoints:
                            for _owner, route in functions.get(terminal_name(endpoint) or '', []) if isinstance(endpoint, (ast.Name, ast.Attribute)) else []:
                                _check_route(module, route, node, node.keywords, matcher, sensitive_models, filtering_models,
                                             _sensitive_vars(route, sensitive_models, matcher, ctx), functions, fails, unresolved)
                    # FastAPI(exception_handlers={RequestValidationError: 처리기}) 로 등록하는 방법
                    for keyword in node.keywords:
                        if keyword.arg == 'exception_handlers' and isinstance(keyword.value, ast.Dict):
                            for key, handler in zip(keyword.value.keys, keyword.value.values):
                                if key is not None and (terminal_name(key) or '') == 'RequestValidationError':
                                    handler_found = True
                                    check_registered_handler(module, node, handler)
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if not (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Attribute)):
                            continue
                        if target.value.attr == 'exception_handlers' and (terminal_name(target.slice) or '') == 'RequestValidationError':
                            handler_found = True  # app.exception_handlers[RequestValidationError] = 처리기
                            check_registered_handler(module, node, node.value)
                        elif target.value.attr in ('headers', 'cookies') and sensitive_refs(node.value, matcher, svars):
                            fails.append(_obs(module, node, 'SENSITIVE_IN_HEADER'))  # response.headers["X"] = 비밀번호
                # ② raise ... 오류 메시지에 비밀번호를 넣는가
                elif isinstance(node, ast.Raise) and node.exc is not None and sensitive_refs(node.exc, matcher, svars):
                    fails.append(_obs(module, node, 'SENSITIVE_IN_ERROR'))
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # 함수 위에 붙은 꼬리표(@데코레이터)를 확인한다.
                    for decorator in node.decorator_list:
                        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                            continue
                        # ④ @app.exception_handler(RequestValidationError) 로 등록한 검증 오류 처리기
                        if decorator.func.attr == 'exception_handler' and decorator.args \
                                and (terminal_name(decorator.args[0]) or '') == 'RequestValidationError':
                            handler_found = True
                            _check_validation_handler(module, node, fails, unresolved, safe_keys)
                            # 다른 데코레이터가 함께 붙어 있으면 실제로 등록되는 함수가 이 함수와 다를 수 있다.
                            # (exception_handler 데코레이터끼리는 함수를 바꾸지 않으므로 괜찮다.)
                            if any(not (isinstance(other, ast.Call) and isinstance(other.func, ast.Attribute)
                                        and other.func.attr == 'exception_handler')
                                   for other in node.decorator_list):
                                unresolved.append(_obs(module, node, 'VALIDATION_HANDLER_DECORATED'))
                        # ③ @router.post(...) 같은 API 경로 함수의 응답을 확인한다.
                        if decorator.func.attr in rules['routeMethods']:
                            _check_route(module, node, decorator, decorator.keywords, matcher, sensitive_models,
                                         filtering_models, svars, functions, fails, unresolved)
    if uses_fastapi and rules['requireValidationErrorFilter'] and input_models and not handler_found:
        # FastAPI는 입력 검증에 실패하면 기본으로 "입력값(비밀번호 포함)"을 오류 응답에 담아 돌려준다.
        # 비밀번호 칸이 있는 모델을 쓰는데 이를 가려 주는 처리기가 없다면 노출 위험이 있다. 필드가 SecretStr이어도 마찬가지다.
        first = next(iter(modules.values()))
        fails.append(Observation(first.path, 0, 'VALIDATION_ERROR_ECHOES_INPUT'))
    if fails:
        return CheckOutcome(FAIL, 'EXPOSURE_PATH_FOUND', fails)
    if unresolved:
        return CheckOutcome(UNVERIFIED, 'ANALYSIS_INCOMPLETE', unresolved)
    return CheckOutcome(PASS, 'NO_STATIC_EXPOSURE_PATH')


def _logger_bindings(module):
    # 이름이 audit·sink여도 logging 팩터리(getLogger·Logger·LoggerAdapter)에서 만든 객체와 그 별칭은 로그 출력이다.
    # 돌려주는 값: (로거라고 확정한 이름들, 로거일 수도 있지만 재대입·순환 별칭 때문에 확정하지 못한 이름들)
    bindings, users, seeds = {}, {}, set()
    for node in ast.walk(module.tree):
        for target, value in _assignment_pairs(node):
            name = qualname(target, {})
            if not name:
                continue
            source = qualname(value, {}) if isinstance(value, (ast.Name, ast.Attribute)) else None
            factory = isinstance(value, ast.Call) and qualname(value.func, module.aliases) in (
                'logging.getLogger', 'logging.Logger', 'logging.LoggerAdapter')
            bindings.setdefault(name, []).append((source, factory))
            if factory:
                seeds.add(name)
            elif source:
                users.setdefault(source, set()).add(name)
    possible, pending = set(seeds), deque(seeds)
    work = 0
    while pending:
        source = pending.popleft()
        for target in users.get(source, ()):
            work += 1
            if work > WORK_LIMIT:
                raise AnalysisTooComplex()
            if target not in possible:
                possible.add(target)
                pending.append(target)
    uncertain = {name for name in possible if any(not factory and source not in possible
                 for source, factory in bindings[name]) or ('.' not in name and module.bindings.get(name, 0) > len(bindings[name]))}
    pending = deque(uncertain)
    while pending:
        for target in users.get(pending.popleft(), ()):
            if target not in uncertain:
                uncertain.add(target)
                pending.append(target)
    return frozenset(possible - uncertain), frozenset(uncertain)


def _is_output_call(call, name, rules, logger_names=frozenset()):
    # 이 호출이 "밖으로 글자를 내보내는 호출"(로그, 화면 출력, 표준 오류 출력 등)인가?
    if name in rules.get('outputCalls', ['print']):
        return True  # print(...), sys.stderr.write(...) 같은 직접 출력
    if isinstance(call.func, ast.Attribute) and call.func.attr in rules.get('writeMethods', ()):
        return True  # 파일·응답에 쓰는 file.write(...), response.set_cookie(...) 같은 호출
    if isinstance(call.func, ast.Attribute) and call.func.attr in rules['logMethods']:
        # logger.info(...)처럼 로그 함수 이름(info, error 등)으로 부른 경우.
        # 부르는 대상이 logging 모듈이거나, 이름에 log가 들어 있거나(logger, app_log 등), logging.getLogger 등으로 만든
        # 객체와 그 별칭(logger_names, 예: audit = logging.getLogger(...))이면 로그 호출로 본다.
        base = qualname(call.func.value, {}) or ''
        return name.startswith('logging.') or 'log' in base.lower() or base in logger_names
    return False


def _models_with_sensitive_fields(modules, matcher, include_secret=False):
    # "비밀번호처럼 보이는 필드를 가진 입력·응답 모델"의 클래스 이름들을 찾는다. 예) class SignupRequest: password: str
    # SecretStr 형식의 필드는 객체를 출력·로그·model_dump()해도 값이 가려지므로 기본으로는 제외한다.
    # 다만 입력 검증에 실패하면 FastAPI 기본 422 응답의 input에는 SecretStr로 바뀌기 전의 원문이 그대로 실린다
    # (pydantic의 오류 항목은 검증 전 입력값을 담는다). 그 경로를 볼 때는 include_secret=True로 SecretStr 필드도 포함한다.
    names = set()
    for module in modules.values():
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    # "이름: 형식" 모양의 필드 선언(AnnAssign)만 본다.
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) \
                            and matcher.search(item.target.id) \
                            and (include_secret or not any(isinstance(part, ast.Name) and part.id == 'SecretStr'
                                                           for part in ast.walk(item.annotation))):
                        names.add(node.name)
    # 비밀번호 칸을 가진 모델을 물려받은 하위 클래스(class SignupIn(SignupRequest))와 별칭(Req = SignupRequest)도 같은 모델이다.
    # 새로 찾은 이름이 또 다른 하위 클래스·별칭의 바탕이 될 수 있어서, 더 이상 늘어나지 않을 때까지 반복한다.
    changed = True
    while changed:
        changed = False
        for module in modules.values():
            for node in ast.walk(module.tree):
                if isinstance(node, ast.ClassDef) and node.name not in names \
                        and any((terminal_name(base) or '') in names for base in node.bases):
                    names.add(node.name)
                    changed = True
                elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None \
                        and (terminal_name(node.value) or '') in names:
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name) and target.id not in names:
                            names.add(target.id)
                            changed = True
    return names


def _sensitive_vars(function, sensitive_models, matcher=None, ctx=None):
    # 한 함수 안에서 "비밀번호(또는 그 해시)를 품고 있는 변수"의 이름들을 찾는다.
    #   1) 매개변수의 형식 표시가 비밀번호 필드를 가진 모델인 것. 예) def signup(payload: SignupRequest)  → payload
    #   2) 그 모델로 직접 만든 것. 예) request = SignupRequest(...)  → request
    #   3) 요청 본문을 통째로 읽은 것. 예) data = await request.json()  → data
    #   4) 위 변수의 값을 옮겨 담은 것. 예) copy = payload / data = {"p": payload.password} / text = f"{payload.password}"
    #      / pw = payload.password. 이름이 password처럼 보이지 않아도 값은 비밀번호이므로 따라가야 한다.
    #   5) 해시 함수의 결과를 담은 것. 예) hashed = hash_password(...)  (REQ-006은 해시 노출도 금지한다)
    # 4)·5)는 matcher가 주어졌을 때만 찾는다. 새로 찾은 변수가 또 다른 변수의 값이 될 수 있어서 더 늘어나지 않을 때까지 반복한다.
    names = set()
    arguments = function.args
    for argument in [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]:
        if argument.annotation is not None and any(
                isinstance(part, ast.Name) and part.id in sensitive_models for part in ast.walk(argument.annotation)):
            names.add(argument.arg)
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                and terminal_name(node.value.func) in sensitive_models:
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    if matcher is None:
        return frozenset(names)

    def carries(value):
        # 이 값에 비밀번호(또는 해시)가 들어 있는가?
        inner = value.value if isinstance(value, ast.Await) else value  # await는 호출 결과를 쓰는 것과 같다
        return (_reads_request_body(inner)
                or bool(sensitive_refs(inner, matcher, names, strict=True))
                or (ctx is not None and isinstance(inner, ast.Call) and _is_hash_call(inner, ctx, scope=function)))

    # 값을 어떤 이름에 담는 문장들을 (담기는 이름들, 그 값들)로 모아 둔다.
    # 안쪽에 만든 함수가 비밀번호를 돌려주면(def get(): return payload.password) 그 함수를 부른 결과도 비밀번호를 품는다.
    entries = []
    for node in ast.walk(function):
        for target, value in _assignment_pairs(node):
            entries.append((_target_names(target), [value]))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node is not function:
            returned = [item.value for item in ast.walk(node) if isinstance(item, ast.Return) and item.value is not None]
            if returned:
                entries.append(([node.name], returned))
    # 새로 알게 된 이름이 있으면, 그 이름을 쓰는 문장만 다시 본다. (전체를 되풀이해서 훑으면 대입이 거꾸로 길게 이어진 코드에서 시간이 제곱으로 늘어난다.)
    users = {}
    for index, (_targets, values) in enumerate(entries):
        for used in {item.id for value in values for item in ast.walk(value) if isinstance(item, ast.Name)}:
            users.setdefault(used, []).append(index)
    pending, queued = deque(range(len(entries))), set(range(len(entries)))
    while pending:
        index = pending.popleft()
        queued.discard(index)
        targets, values = entries[index]
        if all(name in names for name in targets) or not any(carries(value) for value in values):
            continue
        for name in targets:
            if name not in names:
                names.add(name)
                for other in users.get(name, ()):
                    if other not in queued:
                        pending.append(other)
                        queued.add(other)
    return frozenset(names)


def _assignment_pairs(node):
    # 값이 어떤 이름(들)에 담기는 문장 하나를 (담기는 곳, 담는 값) 쌍들로 풀어 돌려준다. 해당하지 않는 문장이면 빈 목록이다.
    #   a = v / a: T = v / a += v / (a := v) / for a in v  →  (a, v)
    #   a, b = x, y  →  (a, x), (b, y)  (개수가 같은 튜플이면 하나씩 짝짓고, 아니면 모든 이름에 값 전체를 짝짓는다)
    #   for k in d.keys() → 없음(키만 꺼낸다) / for k, v in d.items() → (v, d.items()) (값 쪽에만 내용이 담긴다)
    if isinstance(node, ast.Assign):
        pairs = [(target, node.value) for target in node.targets]
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        pairs = [(node.target, node.value)]
    elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
        pairs = [(node.target, node.value)]
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        method = node.iter.func.attr if isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Attribute) else None
        if method == 'keys':
            return []  # 사전의 키(항목 이름)만 꺼내므로 값이 담기지 않는다.
        if method == 'items' and isinstance(node.target, (ast.Tuple, ast.List)) and len(node.target.elts) == 2:
            pairs = [(node.target.elts[1], node.iter)]  # for 키, 값 in 사전.items(): 값 쪽에만 내용이 담긴다.
        else:
            pairs = [(node.target, node.iter)]
    else:
        return []
    result = []
    for target, value in pairs:
        if (isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List))
                and len(target.elts) == len(value.elts)
                and not any(isinstance(item, ast.Starred) for item in [*target.elts, *value.elts])):
            result.extend(zip(target.elts, value.elts))
        else:
            result.append((target, value))
    return result


def _target_names(target):
    # 대입 대상에 들어 있는 변수 이름들. 예) a → [a],  (a, b) → [a, b],  a.x · a[0] → [] (이름이 새로 묶이지 않는다)
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for item in target.elts for name in _target_names(item)]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _reads_request_body(value):
    # 요청 본문을 통째로 읽는 코드인가? 예) await request.json() / request.get_json() / request.form / await request.body()
    # 본문 안에는 비밀번호가 들어 있으므로, 그 결과를 담은 변수는 "비밀번호를 품은 변수"로 본다.
    target = value.func if isinstance(value, ast.Call) else value
    if not (isinstance(target, ast.Attribute) and target.attr in ('json', 'get_json', 'form', 'body', 'get_data', 'data', 'values')):
        return False
    base = terminal_name(target.value) or ''
    return 'request' in base.lower() or base.lower() == 'req'


def _check_validation_handler(module, function, fails, unresolved, safe_keys=frozenset({'loc', 'msg', 'type'})):
    # "입력 검증 오류 처리기" 함수가 사용자에게 돌려주는 내용에 입력값(비밀번호)이 그대로 실리는지 확인한다.
    # FastAPI에서 exc.errors()가 돌려주는 오류 목록 각 항목에는 사용자가 보낸 원래 값("input")이 들어 있다.
    #   위험: exc.errors()나 exc.body를 가공 없이 그대로 응답에 넣는다.
    #   안전: 필요한 키(loc, msg, type 등)만 새 사전에 옮겨 담거나, 하나씩 훑으면서 "input"을 지운다.
    # 이 함수는 위험하면 fails에, 모양을 알 수 없으면 unresolved에 기록한다.
    parents = {child: parent for parent in ast.walk(function) for child in ast.iter_child_nodes(parent)}  # 각 가지의 "부모" 표
    exc_names = {argument.arg for argument in function.args.args[1:2]}  # 처리기의 두 번째 인자(보통 exc) 이름

    def is_errors_call(node):
        # something.errors() 호출인가?
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'errors'

    def raw(node, tainted):
        # 이 코드 조각이 "가공하지 않은 원본 오류 목록"인가?
        # 원본인 경우: errors() 호출 / 그 결과를 담은 변수 / 그것을 감싼 호출(jsonable_encoder(...), list(...) 등)
        if is_errors_call(node):
            return True
        if isinstance(node, ast.Name):
            return node.id in tainted
        if isinstance(node, ast.Call) and node.args:
            if isinstance(node.func, ast.Name) and node.func.id in SAFE_CALLS:
                return False  # len(...) 같은 호출은 값을 드러내지 않는다.
            return raw(node.args[0], tainted)
        return False

    # tainted = "원본 오류 목록을 담고 있는 변수" 이름들. 변수를 다른 변수에 옮겨 담는 경우도 따라가려고
    # 새로 알게 된 이름을 쓰는 대입만 다시 보면서 더 이상 늘어나지 않을 때까지 찾는다.
    tainted = set()
    assignments = [node for node in ast.walk(function) if isinstance(node, ast.Assign)]
    users = {}
    for index, node in enumerate(assignments):
        for used in {item.id for item in ast.walk(node.value) if isinstance(item, ast.Name)}:
            users.setdefault(used, []).append(index)
    pending, queued = deque(range(len(assignments))), set(range(len(assignments)))
    while pending:
        index = pending.popleft()
        queued.discard(index)
        node = assignments[index]
        if not raw(node.value, tainted):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id not in tainted:
                tainted.add(target.id)
                for other in users.get(target.id, ()):
                    if other not in queued:
                        pending.append(other)
                        queued.add(other)

    # "input을 지우는 반복문"을 찾는다. 일부 항목에서만 지워질 수 있는 모양은 정리로 인정하지 않는다(아래 설명 참고).
    #   cleaned_at    : 정리가 "확실히" 끝나는 위치. {오류 목록 변수 이름: (줄, 칸)}  (이 위치 뒤에서 쓰는 목록은 안전하다)
    #   cleaned_loops : 그 반복문들이 훑는 목록 코드 조각의 id (반복문 자체를 "모르는 모양"으로 보지 않으려고 기록)
    #   attempted     : input을 지우려는 시도는 있지만 모든 항목에서 지워진다고 증명하지 못한 목록 변수 이름
    #                   (예: if 안에서만 지움, continue로 건너뛸 수 있음) → 이 목록을 응답에 쓰면 "모른다"로 처리한다.
    # 확실한 정리는 아래 조건을 모두 만족하는 반복문뿐이다.
    #   · 함수 본문 바로 아래에 있는 문장이다(if·try 안에 들어 있지 않다). 그래야 항상 실행되고, 그 뒤의 코드는 정리가 끝난 다음에 실행된다.
    #   · 오류 목록을 담은 변수를 훑고, 그 변수는 함수 안에서 딱 한 번만 대입된다(나중에 원본으로 되돌리는 대입이 없다).
    #   · 본문이 "항목에서 키를 지우는 문장"(item.pop("input", None), del item["input"])만으로 이루어져 있고 그중 하나가 input이다.
    #     (조건문·continue·break·return·다시 대입이 본문에 있으면 일부 항목에서 지우기를 건너뛸 수 있다.)
    bindings = _name_bindings(function)
    cleaned_at, cleaned_loops, attempted = {}, set(), set()
    for node in ast.walk(function):
        if not (isinstance(node, ast.For) and isinstance(node.iter, ast.Name) and node.iter.id in tainted):
            continue
        if node in function.body and _is_input_cleaning_loop(node, bindings.get(node.iter.id, 0)):
            cleaned_loops.add(id(node.iter))
            end_position = (node.end_lineno, node.end_col_offset)
            cleaned_at[node.iter.id] = min(end_position, cleaned_at.get(node.iter.id, end_position))
        elif _mentions_input_removal(node):
            attempted.add(node.iter.id)

    # 이제 원본 오류 목록을 "사용하는 곳"을 모두 찾아서 안전한 사용인지 판정한다.
    reported = set()  # 이미 판정한 코드 조각
    for node in ast.walk(function):
        if (isinstance(node, ast.Attribute) and node.attr == 'body' and isinstance(node.value, ast.Name)
                and node.value.id in exc_names):
            # exc.body는 사용자가 보낸 요청 본문 전체(비밀번호 포함)이다. 응답에 쓰면 위험하다.
            fails.append(_obs(module, node, 'VALIDATION_ERROR_ECHOES_INPUT'))
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in exc_names:
            # 예외 객체(exc) 자체도 오류 목록(input 포함)을 품고 있어서, str(exc)·exc.args·로그 출력·응답 값으로 쓰면 입력값이 나간다.
            # 허용하는 사용은 exc.errors() 호출(아래에서 따로 판정)과 type(exc)처럼 값을 드러내지 않는 호출뿐이다.
            parent = parents.get(node)
            if isinstance(parent, ast.Attribute) and parent.value is node:
                if parent.attr not in ('errors', 'body'):  # body는 위에서 이미 판정한다.
                    fails.append(_obs(module, node, 'VALIDATION_ERROR_ECHOES_INPUT'))
            elif not (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id in SAFE_CALLS):
                fails.append(_obs(module, node, 'VALIDATION_ERROR_ECHOES_INPUT'))
            continue
        is_use = is_errors_call(node) or (
            isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in tainted)
        if not is_use or id(node) in reported:
            continue
        use = node
        while True:  # jsonable_encoder(exc.errors()) 처럼 감싼 호출은 가장 바깥 식을 "사용"으로 본다.
            parent = parents.get(use)
            if (isinstance(parent, ast.Call) and parent.args and parent.args[0] is use
                    and not (isinstance(parent.func, ast.Name) and parent.func.id in SAFE_CALLS)):
                reported.add(id(use))
                use = parent
                continue
            break
        reported.add(id(use))
        parent = parents.get(use)
        if isinstance(parent, ast.Assign) and parent.value is use:
            continue  # 변수에 담기만 한다. 그 변수를 쓰는 곳에서 따로 판정하므로 여기서는 넘어간다.
        if isinstance(parent, ast.comprehension) and parent.iter is use:
            # [ {...} for e in exc.errors() ] 처럼 목록을 새로 만드는 한 줄 반복문
            comprehension = parents.get(parent)
            element = getattr(comprehension, 'elt', None)  # 새 목록의 각 항목을 만드는 식
            if isinstance(element, ast.Dict):
                keys = element.keys
                # 새 사전에 "input" 키가 있거나, 기존 사전을 통째로 펼쳐(**e) 넣으면 위험하다.
                if any(key is None for key in keys) or any(
                        isinstance(key, ast.Constant) and key.value == 'input' for key in keys):
                    fails.append(_obs(module, use, 'VALIDATION_ERROR_ECHOES_INPUT'))
                else:
                    # 키 이름이 안전해 보여도, 값에 사용자가 보낸 입력값(error["input"])을 담으면 똑같이 새어 나간다.
                    # 그래서 각 값이 오류 항목의 어느 부분에서 왔는지도 확인한다.
                    loop_names = {item.id for item in ast.walk(parent.target) if isinstance(item, ast.Name)}
                    verdict = _error_values_verdict(element.values, loop_names, parents, safe_keys)
                    if verdict == 'FAIL':
                        fails.append(_obs(module, use, 'VALIDATION_ERROR_ECHOES_INPUT'))
                    elif verdict == 'UNKNOWN':
                        unresolved.append(_obs(module, use, 'VALIDATION_ERROR_SHAPE_UNKNOWN'))
            elif isinstance(element, ast.Name) and element.id in {
                    item.id for item in ast.walk(parent.target) if isinstance(item, ast.Name)}:
                # [e for e in exc.errors()] 처럼 항목을 그대로 복사하면 input도 그대로 들어간다.
                fails.append(_obs(module, use, 'VALIDATION_ERROR_ECHOES_INPUT'))
            else:
                unresolved.append(_obs(module, use, 'VALIDATION_ERROR_SHAPE_UNKNOWN'))  # 모르는 모양
            continue
        if isinstance(parent, ast.For) and parent.iter is use:
            # 일반 반복문으로 오류 목록을 훑는 경우. 위에서 "input을 확실히 지운다"고 확인된 반복문이 아니면 모른다고 처리한다.
            if id(use) not in cleaned_loops:
                unresolved.append(_obs(module, use, 'VALIDATION_ERROR_SHAPE_UNKNOWN'))
            continue
        if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id in SAFE_CALLS \
                and parent.args and parent.args[0] is use:
            continue  # len(errors) 처럼 개수·종류만 확인하는 호출은 값을 드러내지 않는다.
        if isinstance(use, ast.Name) and use.id in cleaned_at:
            # input을 지우는 반복문이 확실히 끝난 "뒤"에서 쓰는 목록은 안전하다. 그 앞에서(예: 정리 전에 먼저 반환) 쓰면 원본이다.
            if (use.lineno, use.col_offset) > cleaned_at[use.id]:
                continue
        elif isinstance(use, ast.Name) and use.id in attempted:
            # 일부 항목에서만 지워질 수 있다. 모든 항목에서 지워진다고 증명할 수 없으므로 "모른다"로 둔다.
            unresolved.append(_obs(module, use, 'VALIDATION_ERROR_SHAPE_UNKNOWN'))
            continue
        fails.append(_obs(module, use, 'VALIDATION_ERROR_ECHOES_INPUT'))  # 그 밖의 사용은 원본을 그대로 쓰는 것


def _removed_keys(statement, item):
    # 문장 하나가 "item(오류 항목)에서 글자 키를 지우는 문장"이면 지우는 키 목록을, 아니면 None을 돌려준다.
    #   item.pop("input")  /  item.pop("input", None)  /  del item["input"]  (del은 여러 개를 한 번에 지울 수도 있다)
    #   키와 기본값은 글자·상수여야 한다(계산 중에 다른 일이 일어날 수 있는 식은 인정하지 않는다).
    if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
        call = statement.value
        func = call.func
        if (isinstance(func, ast.Attribute) and func.attr == 'pop' and isinstance(func.value, ast.Name)
                and func.value.id == item and 1 <= len(call.args) <= 2 and not call.keywords
                and all(isinstance(arg, ast.Constant) for arg in call.args)
                and isinstance(call.args[0].value, str)):
            return [call.args[0].value]
        return None
    if isinstance(statement, ast.Delete):
        keys = []
        for target in statement.targets:
            if not (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) and target.value.id == item
                    and isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str)):
                return None
            keys.append(target.slice.value)
        return keys
    return None


def _is_input_cleaning_loop(loop, list_bindings):
    # 이 반복문이 오류 목록의 "모든 항목"에서 input을 반드시 지우는가? (확실한 경우에만 True)
    #   list_bindings : 훑는 목록 변수가 함수 안에서 대입(바인딩)되는 횟수. 1이어야 한다.
    if list_bindings != 1 or loop.orelse or not isinstance(loop.target, ast.Name):
        return False
    removed = []
    for statement in loop.body:
        keys = _removed_keys(statement, loop.target.id)
        if keys is None:
            return False  # 삭제문이 아닌 문장(if, continue, 다시 대입 등)이 있으면 지우기를 건너뛸 수 있다.
        removed.extend(keys)
    return 'input' in removed


def _mentions_input_removal(loop):
    # 반복문 어딘가에서 input을 지우려는 시도가 있는가? (조건부 삭제처럼 "모든 항목에서 지운다"고 증명은 못 하는 경우를 가려낸다)
    if not isinstance(loop.target, ast.Name):
        return False
    for node in ast.walk(loop):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'pop' \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == loop.target.id \
                and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'input':
            return True
        if isinstance(node, ast.Delete) and any(
                isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                and target.value.id == loop.target.id and isinstance(target.slice, ast.Constant)
                and target.slice.value == 'input' for target in node.targets):
            return True
    return False


def _error_values_verdict(values, loop_names, parents, safe_keys):
    # 오류 목록을 새 사전으로 옮겨 담을 때, 사전의 "값"들이 오류 항목의 어느 부분을 가져오는지 판정한다.
    #   안전(OK)    : error["loc"] 처럼 허용한 키만 꺼내거나, 오류 항목과 상관없는 값
    #   위험(FAIL)  : error["input"], 또는 error 자체를 통째로 사용(str(error) 등) → 사용자가 보낸 값이 들어간다.
    #   모름(UNKNOWN): error["ctx"] 처럼 허용 목록에 없는 키. 안에 무엇이 들어 있는지 코드만으로 알 수 없다.
    # 돌려주는 값은 가장 심각한 하나이다: 'FAIL' > 'UNKNOWN' > 'OK'
    verdict = 'OK'
    for value in values:
        for node in ast.walk(value):
            if not (isinstance(node, ast.Name) and node.id in loop_names):
                continue
            parent = parents.get(node)
            key = None
            if isinstance(parent, ast.Subscript) and parent.value is node:
                if isinstance(parent.slice, ast.Constant) and isinstance(parent.slice.value, str):
                    key = parent.slice.value                       # error["loc"]
            elif isinstance(parent, ast.Attribute) and parent.attr == 'get' and parent.value is node:
                call = parents.get(parent)                         # error.get("loc")
                if isinstance(call, ast.Call) and call.args and isinstance(call.args[0], ast.Constant) \
                        and isinstance(call.args[0].value, str):
                    key = call.args[0].value
            if key is None:
                return 'FAIL'  # 오류 항목을 통째로 쓰거나 키를 알 수 없다 → 입력값이 포함될 수 있다.
            if key == 'input':
                return 'FAIL'
            if key not in safe_keys:
                verdict = 'UNKNOWN'
    return verdict


def _response_filter_models(modules):
    # 모델의 선언 필드만 내보낸다는 근거가 있는 이름을 찾는다. 사용자 serializer·동적 설정·불명확한 상속은 인정하지 않는다.
    classes, aliases = {}, {}
    for module in modules.values():
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ClassDef):
                classes.setdefault(node.name, []).append((module, node))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Name):
                for target, value in _assignment_pairs(node):
                    if isinstance(target, ast.Name) and module.bindings.get(target.id) == 1:
                        aliases[target.id] = value.id
    memo, active = {}, set()
    work = 0

    def safe_config(value, module):
        if isinstance(value, ast.Dict):
            pairs = [(key.value, item) for key, item in zip(value.keys, value.values) if isinstance(key, ast.Constant)]
            if len(pairs) != len(value.keys):
                return False
        elif isinstance(value, ast.Call) and qualname(value.func, module.aliases) == 'pydantic.ConfigDict' and not value.args:
            if any(keyword.arg is None for keyword in value.keywords):
                return False
            pairs = [(keyword.arg, keyword.value) for keyword in value.keywords]
        else:
            return False
        return all(key != 'extra' or isinstance(item, ast.Constant) and item.value in ('ignore', 'forbid')
                   for key, item in pairs)

    def safe(name, depth=0):
        nonlocal work
        work += 1
        if work > WORK_LIMIT:
            raise AnalysisTooComplex()
        if name in memo:
            return memo[name]
        if depth > 80:  # 지원 범위 밖의 깊은 상속은 필터로 신뢰하지 않는다. 다른 경로 검사까지 재귀 오류로 버리지 않는다.
            return False
        if name in active:
            return False
        active.add(name)
        definitions = classes.get(name, [])
        if not definitions and name in aliases:
            result = safe(aliases[name], depth + 1)
        else:
            result = len(definitions) == 1 and safe_class(*definitions[0], depth)
        active.remove(name)
        memo[name] = result
        return result

    def safe_class(module, node, depth):
        if not node.bases or node.decorator_list:
            return False
        for base in node.bases:
            full = qualname(base, module.aliases)
            if full not in ('pydantic.BaseModel', 'pydantic.main.BaseModel') and not safe(terminal_name(base) or '', depth + 1):
                return False
        # class Public(BaseModel, extra="allow")도 model_config와 같은 방식으로 추가 필드를 허용한다.
        if any(k.arg is None for k in node.keywords):
            return False  # 펼친 설정의 extra를 알 수 없으면 필터로 신뢰하지 않는다.
        if node.keywords and not safe_config(ast.Dict(keys=[ast.Constant(k.arg) for k in node.keywords],
                                                       values=[k.value for k in node.keywords]), module):
            return False
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 사용자 함수는 기본 직렬화와 검증을 바꿀 수 있다. 안전성을 더 추적할 수 없는 경우에는 필터로 신뢰하지 않는다.
                return False
            if isinstance(item, ast.ClassDef):
                if item.name != 'Config' or item.bases or item.decorator_list:
                    return False
                if any(not isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.Pass))
                       and not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                                and isinstance(stmt.value.value, str)) for stmt in item.body):
                    return False
                pairs = [(target.id, value) for stmt in item.body for target, value in _assignment_pairs(stmt)
                         if isinstance(target, ast.Name)]
                if not safe_config(ast.Dict(keys=[ast.Constant(key) for key, _ in pairs], values=[value for _, value in pairs]), module):
                    return False
            for target, value in _assignment_pairs(item):
                if isinstance(target, ast.Name) and target.id == 'model_config' and not safe_config(value, module):
                    return False
            if isinstance(item, (ast.If, ast.Try, ast.For, ast.While)):
                return False
        return True

    return frozenset(name for name in classes.keys() | aliases.keys() if safe(name))


def _is_response_object(call):
    # 이 호출이 "응답 객체를 직접 만드는 호출"인가? 예) JSONResponse(...), Response(...), jsonify(...)
    # 응답 객체를 직접 돌려주면 FastAPI의 response_model 걸러내기가 적용되지 않는다.
    return isinstance(call, ast.Call) and ((terminal_name(call.func) or '').endswith('Response')
                                           or (terminal_name(call.func) or '') in ('jsonify', 'make_response'))


def _check_route(module, function, anchor, keywords, matcher, sensitive_models, filtering_models, svars, helpers, fails, unresolved):
    # API 경로 함수(@router.post 등이 붙은 함수)가 응답으로 비밀번호를 돌려주는지 확인한다.
    #   anchor   = 그 함수를 경로로 등록한 곳(데코레이터나 add_api_route 호출). 결과의 위치로 쓴다.
    #   keywords = 그 등록 호출에 넘긴 인자들. 응답 모양을 정하는 response_model 같은 인자가 들어 있을 수 있다.
    declared = next((keyword.value for keyword in keywords if keyword.arg == 'response_model'), None)
    declared_names = {part.id for part in ast.walk(declared) if isinstance(part, ast.Name)} if declared is not None else set()
    if declared_names & sensitive_models:
        fails.append(_obs(module, anchor, 'SENSITIVE_RESPONSE_MODEL'))  # 응답 모양 자체가 비밀번호 칸이 있는 모델이다.
    # response_model이 실제로 응답을 걸러 준다고 인정하는 경우: 상속·추가 필드·사용자 직렬화를 확인한 모델만이다.
    #   None·임의 클래스·extra="allow"·모르는 설정·serializer가 있으면 반환 객체가 걸러진다고 단정하지 않는다.
    model_names = declared_names - {'list', 'tuple', 'set', 'List', 'Tuple', 'Set', 'Optional', 'Union', 'None'}
    filters = bool(model_names) and model_names <= filtering_models
    unproven_filter = bool(model_names) and not filters
    # 걸러 준다고 인정할 때만, 돌려주는 객체는 그 모델에 있는 필드만 응답에 나간다고 보고 객체를 통째로 반환해도 노출로 보지 않는다.
    visible_vars = frozenset() if filters else svars
    # 응답 객체를 직접 만드는 코드(JSONResponse(content=...) 등)는 response_model이 있어도 걸러지지 않는다.
    # 그래서 어디서 만들든 그 안에 비밀번호가 들어 있는지 항상 전부 확인한다.
    for node in ast.walk(function):
        if _is_response_object(node) and sensitive_refs(node, matcher, svars):
            fails.append(_obs(module, node, 'SENSITIVE_RESPONSE_VALUE'))
    for node in ast.walk(function):
        if not isinstance(node, ast.Return) or node.value is None:
            continue  # return 문만 본다.
        value = node.value
        if isinstance(value, ast.Dict):
            for key in value.keys:
                # 응답 사전의 키 이름이 비밀번호처럼 보이면 위험하다. 예) {"password_hash": ...}
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and matcher.search(key.value):
                    fails.append(_obs(module, node, 'SENSITIVE_RESPONSE_KEY'))
        if _is_response_object(value):
            continue  # 응답 객체는 위에서 이미 확인했다.
        if filters and isinstance(value, ast.Call) and any(
                _is_response_object(inner) for _owner, helper in helpers.get(terminal_name(value.func) or '', [])
                for inner in ast.walk(helper)):
            # response_model이 있어도, 응답 객체를 직접 만들어 돌려줄 수 있는 도우미 함수의 결과는 걸러지지 않을 수 있다.
            unresolved.append(_obs(module, node, 'RESPONSE_BUILT_BY_HELPER'))
            continue
        if sensitive_refs(value, matcher, visible_vars):
            if unproven_filter:
                unresolved.append(_obs(module, node, 'RESPONSE_FILTER_UNVERIFIED'))
            else:
                fails.append(_obs(module, node, 'SENSITIVE_RESPONSE_VALUE'))  # 필터가 없는 응답 값에 비밀번호를 사용
        elif not filters and _dumps_unknown_object(value):
            # 객체의 전체 필드를 펼쳐서 응답에 넣었다. 어떤 필드가 들어 있는지 코드만으로는 알 수 없다.
            unresolved.append(_obs(module, node, 'UNFILTERED_OBJECT_RESPONSE'))
        elif isinstance(value, (ast.Name, ast.Attribute)) and not filters:
            # 객체를 걸러 주는 장치 없이 그대로 반환한다. 어떤 필드가 나가는지 알 수 없다.
            unresolved.append(_obs(module, node, 'UNFILTERED_OBJECT_RESPONSE'))


def _dumps_unknown_object(value):
    # 응답에 "객체의 모든 필드"(__dict__, model_dump() 등)를 펼쳐 넣었는지 확인한다.
    # 이런 경우 어떤 필드가 들어 있는지 코드만으로는 알 수 없다.
    for node in ast.walk(value):
        if isinstance(node, ast.Attribute) and node.attr == '__dict__':
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in DUMP_METHODS:
            return True
    return False


# ---------------------------------------------------------------- Bandit 경고 판정

def dispose_findings(modules, criteria, findings):
    # Bandit(파이썬 보안 점검 도구)이 찾아낸 경고들을 "코드 근거로" 하나씩 판정한다.
    # findings: [(경고 번호, 경고 정보 사전)] 목록. 각 판정은 (경고 번호, 판정, 근거 기록) 모양으로 돌려준다.
    #   CONFIRMED      = 정말 문제가 맞다고 코드로 확인함
    #   FALSE_POSITIVE = 도구의 오해(문제가 아님)라고 코드로 확인함
    # 근거가 부족하면 판정하지 않고 그냥 둔다(결과에 포함하지 않는다). 개발정의서 10절에서
    # "경고가 있다는 이유만으로 바로 CONFIRMED 처리하지 않는다"고 정했기 때문이다.
    rules = criteria['bandit']
    matcher = _sensitive(criteria)
    forbidden = set(criteria['hashPolicy']['forbiddenHashCalls'])  # 비밀번호 저장에 쓰면 안 되는 해시 함수들
    results = []
    for finding_id, finding in findings:
        module = modules.get(finding.get('path'))
        line = finding.get('line')
        if module is None or type(line) is not int:
            continue  # 어느 파일 몇 번째 줄인지 알 수 없는 경고는 판정할 수 없다.
        rule = finding.get('ruleId')  # Bandit 규칙 번호. 예) B105
        if rule in rules['hardcodedPasswordRules']:
            # 코드에 비밀번호가 직접 적혀 있다는 경고(B105~B107) → 그 줄의 글자를 직접 살펴본다.
            decision = _judge_literal(module, line, matcher, rule)
            if decision is not None:
                results.append((finding_id, *decision))
        elif rule in rules['insecureHashRules']:
            # 약한 해시를 쓴다는 경고(B303, B324) → 그 줄의 함수 호출을 직접 살펴본다.
            for call in (node for node in ast.walk(module.tree) if isinstance(node, ast.Call) and node.lineno == line):
                name = qualname(call.func, module.aliases) or ''
                if name not in forbidden:
                    continue
                if sensitive_refs(call, matcher):
                    # 약한 해시에 비밀번호를 넣고 있다 → 진짜 문제
                    results.append((finding_id, 'CONFIRMED', _obs(module, call, 'WEAK_HASH_OF_PASSWORD', ruleId=rule)))
                elif any(keyword.arg == 'usedforsecurity' and isinstance(keyword.value, ast.Constant)
                         and keyword.value.value is False for keyword in call.keywords):
                    # usedforsecurity=False: "보안 목적이 아닌 용도(예: 파일 지문)"라고 직접 밝힌 경우 → 오탐
                    results.append((finding_id, 'FALSE_POSITIVE', _obs(module, call, 'NOT_FOR_SECURITY', ruleId=rule)))
                break
    return results


def _judge_literal(module, line, matcher, rule):
    # "코드에 비밀번호가 직접 적혀 있다"는 경고(B105~B107)가 정말 맞는지, 그 줄을 보고 판정한다.
    # 그 줄에서 (대입 대상 이름, 따옴표 글자) 쌍을 찾아 하나씩 살펴본다. 판단 순서가 중요하다.
    #   1) 빈 글자("")                                     → 값이 없으니 자격증명이 아니다(오탐)
    #   2) 대상 이름이 비밀번호처럼 보이고 값이 비어 있지 않음 → 진짜 문제(CONFIRMED)
    #        예) password = "password", password = "correct horse battery staple", DB_PASSWORD = "..."
    #        값이 안내 문구처럼 생겼는지(공백·한글이 있는지)는 근거가 아니다. 변수 이름과 용도가 먼저다.
    #   3) 그 외 이름(이름을 알 수 없는 경우 포함)에서
    #        안내 문구처럼 보이는 글자(공백·한글)              → 오탐(FALSE_POSITIVE)
    #        필드 이름 글자(예: label = "password")             → 오탐(FALSE_POSITIVE)
    #   4) 그 밖에는 근거가 부족하므로 판정하지 않는다(None) → 경고는 SUSPECTED(의심) 그대로 남는다.
    pairs = _literal_pairs(module.tree, line)
    if not pairs:
        return None
    outcomes = []
    for name, literal in pairs:
        text = literal.value
        if text == '':
            outcomes.append(('FALSE_POSITIVE', _obs(module, literal, 'EMPTY_VALUE_NOT_CREDENTIAL', ruleId=rule)))
        elif name is not None and matcher.search(name):
            outcomes.append(('CONFIRMED', _obs(module, literal, 'HARDCODED_CREDENTIAL', ruleId=rule)))
        elif _looks_like_message(text):
            outcomes.append(('FALSE_POSITIVE', _obs(module, literal, 'MESSAGE_TEXT_NOT_CREDENTIAL', ruleId=rule)))
        elif matcher.search(text):
            outcomes.append(('FALSE_POSITIVE', _obs(module, literal, 'FIELD_NAME_NOT_CREDENTIAL', ruleId=rule)))
        else:
            return None  # 하나라도 근거가 부족한 글자가 있으면 이 줄은 판정하지 않는다.
    for value, observation in outcomes:
        if value == 'CONFIRMED':
            return value, observation  # 진짜 문제가 하나라도 있으면 CONFIRMED가 우선이다.
    return outcomes[0]


def _literal_pairs(tree, line):
    # 지정한 줄에 있는 "따옴표 글자"를 모두 찾아서, 무엇에 대입·전달되는지 (이름, 글자) 쌍으로 돌려준다.
    # 대입 모양이 여러 가지라서 하나씩 확인한다.
    #   password = "abc"        (대입문)           login(password="abc")   (이름 붙여 전달)
    #   def f(password="abc")   (기본값)           {"password": "abc"}     (사전)
    #   if password == "abc"    (비교)
    pairs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.lineno == line:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                pairs.extend((terminal_name(target), value) for target in targets)
        elif isinstance(node, ast.keyword):
            if node.arg and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str) \
                    and node.value.lineno == line:
                pairs.append((node.arg, node.value))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            positional = [*arguments.posonlyargs, *arguments.args]
            # 기본값은 "뒤에서부터" 매개변수와 짝이 맞는다(기본값이 없는 매개변수는 앞쪽에 있다).
            for argument, default in [*zip(reversed(positional), reversed(arguments.defaults)),
                                      *zip(arguments.kwonlyargs, arguments.kw_defaults)]:
                if isinstance(default, ast.Constant) and isinstance(default.value, str) and default.lineno == line:
                    pairs.append((argument.arg, default))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.lineno == line:
                    pairs.append((key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None, value))
        elif isinstance(node, ast.Compare):
            for comparator in node.comparators:
                if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str) and comparator.lineno == line:
                    pairs.append((terminal_name(node.left), comparator))
    # 위 어느 모양에도 해당하지 않는 따옴표 글자(함수 인자 등)도, 안내 문구인지는 확인할 수 있도록 "이름 없이" 포함한다.
    known = {id(literal) for _name, literal in pairs}
    pairs.extend((None, node) for node in ast.walk(tree)
                 if isinstance(node, ast.Constant) and isinstance(node.value, str)
                 and getattr(node, 'lineno', None) == line and id(node) not in known)
    return pairs


def _looks_like_message(text):
    # 자격증명이 아니라 "사람이 읽는 안내 문구"로 보이는가?
    # 비어 있거나, 공백이 들어 있거나(문장), 한글 같은 영어가 아닌 글자가 있으면 안내 문구로 본다.
    # (진짜 비밀번호는 보통 공백 없는 영문·숫자·기호 조합이다.)
    return text == '' or any(char.isspace() for char in text) or not text.isascii()
