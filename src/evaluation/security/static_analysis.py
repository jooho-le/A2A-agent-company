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
from dataclasses import dataclass, field  # 값을 담는 "데이터 상자" 클래스를 쉽게 만드는 도구
import re  # 정규식(글자 모양 규칙) 도구

PASS, FAIL, UNVERIFIED = 'PASS', 'FAIL', 'UNVERIFIED'  # 판정 결과 세 가지의 이름(오타를 막으려고 변수로 만들어 둠)
MAX_OBSERVATIONS = 20  # 근거 기록을 한 판정에 최대 몇 개까지 남길지(너무 길어지지 않게)
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
    modules, errors = {}, []
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
            tree = ast.parse(bytes(source_files[path]).decode('utf-8'), filename=path)  # 글자 → AST(나무)
        except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError, MemoryError):
            # 문법이 틀렸거나, 글자 인코딩이 이상하거나, 너무 깊거나 긴 식이어서 읽지 못한 파일이다.
            # 이 파일 하나만 "분석 불가"로 기록하고 나머지 파일의 판정은 그대로 이어 간다.
            errors.append(path)
            continue
        modules[path] = Module(path, tree, _aliases(tree))
    return modules, errors


def _aliases(tree):
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
    return table


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


def sensitive_refs(node, matcher, sensitive_vars=frozenset()):
    # 어떤 코드 조각(node) 안에서 "비밀번호 값을 사용하는 곳"을 찾아 이름 목록으로 돌려준다. 없으면 빈 목록이다.
    # 아래 세 가지는 비밀번호를 쓴 것으로 세지 않는다.
    #   - 호출하는 함수의 이름 자체  - 따옴표 안의 글자(문자열 상수)  - len() 같이 길이·형식만 알려 주는 함수
    # sensitive_vars: "비밀번호 필드를 가진 객체"를 담은 변수 이름들이다. 이런 객체를 통째로 쓰거나
    #   model_dump()/__dict__로 펼치면 비밀번호도 함께 나가므로 노출로 본다.
    #   (payload.email처럼 다른 필드만 쓰는 것은 괜찮다.)
    found, stack = [], [node]  # stack = 아직 살펴보지 않은 코드 조각들을 쌓아 둔 목록
    while stack:
        item = stack.pop()
        if isinstance(item, ast.Call):  # 함수 호출
            func = item.func
            if isinstance(func, ast.Name) and func.id in SAFE_CALLS:
                continue  # len(password) 같은 호출은 안쪽을 보지 않고 건너뛴다.
            if (isinstance(func, ast.Attribute) and func.attr in DUMP_METHODS
                    and isinstance(func.value, ast.Name) and func.value.id in sensitive_vars):
                found.append(func.value.id)  # payload.model_dump() → 비밀번호를 포함한 전체 필드를 꺼내는 것
                continue
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


def _module_constants(tree):
    # 파일 맨 위(함수 밖)에서 "이름 = 정수"로 정해 둔 상수들을 {이름: 값} 사전으로 모은다. 예) MEMORY = 19456
    table = {}
    for statement in getattr(tree, 'body', []):  # body = 파일의 최상위 문장들
        if (isinstance(statement, ast.Assign) and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Constant) and type(statement.value.value) is int):
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
    functions = _functions(modules)       # 프로젝트 안에서 정의된 함수들
    module_constants = {path: _module_constants(module.tree) for path, module in modules.items()}  # 파일별 상수표
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
        elif name in rules['forbiddenHashCalls'] and sensitive_refs(call, matcher):
            # md5·sha256처럼 비밀번호 저장에 쓰면 안 되는 빠른 해시에 비밀번호를 넣었다 → 위반
            fails.append(_obs(module, call, 'WEAK_HASH_OF_PASSWORD', function=name))
        elif name.startswith(tuple(rules['unapprovedHasherPrefixes'])) and sensitive_refs(call, matcher):
            # 승인되지 않은 해시 라이브러리(예: bcrypt, passlib)에 비밀번호를 넣었다 → 위반
            fails.append(_obs(module, call, 'UNAPPROVED_HASHER', function=name))
        # DB 테이블 객체를 만드는 호출이면, 비밀번호 칸(password_hash 등)에 무엇을 넣는지 살펴본다. 예) User(password_hash=...)
        if isinstance(call.func, (ast.Name, ast.Attribute)) and terminal_name(call.func) in orm_classes:
            for keyword in call.keywords:
                if keyword.arg in rules['storageFieldNames']:
                    _classify_storage(module, keyword.value, keyword.arg, functions, rules, matcher, fails, stored, unresolved)
    # 객체의 칸에 값을 직접 대입하는 코드도 살펴본다. 예) user.password_hash = ...
    for module in modules.values():
        for node in ast.walk(module.tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr in rules['storageFieldNames'] and node.value is not None:
                        _classify_storage(module, node.value, target.attr, functions, rules, matcher, fails, stored, unresolved)
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


def _bind_parameters(call, order, defaults, constants, module, rules):
    # 해시 도구를 만드는 호출(예: PasswordHasher(2, 19456, 1))에서 설정 숫자를 이름별로 읽어 낸다.
    # 위치로 넘긴 값(order 순서)과 이름을 붙여 넘긴 값(time_cost=2)을 모두 처리하고,
    # 쓰지 않은 값은 라이브러리 기본값으로 채운다. 숫자가 아니거나 코드로 알 수 없는 값이면 설정 전체를 None으로 돌려준다.
    given = dict(zip(order, call.args))  # 위치로 넘긴 값들을 이름과 짝지음
    given.update({keyword.arg: keyword.value for keyword in call.keywords if keyword.arg})  # 이름 붙여 넘긴 값들
    failed, params = [], {}
    if 'type' in given and qualname(given['type'], module.aliases) not in rules['argon2idTypes']:
        # Argon2에는 종류(i, d, id)가 있는데 id가 아닌 것을 지정했다 → 위반
        failed.append(_obs(module, call, 'NOT_ARGON2ID_TYPE'))
    for key in ('time_cost', 'memory_cost', 'parallelism'):
        if key not in given:
            params[key] = defaults[key]  # 안 적었으면 라이브러리 기본값
            continue
        value = _int_value(given[key], constants)
        if value is None:
            return None, failed  # 숫자를 알 수 없으면 설정을 확정할 수 없다.
        params[key] = value
    return params, failed


def _classify_storage(module, value, field_name, functions, rules, matcher, fails, stored, unresolved):
    # "비밀번호 칸(field_name)에 저장하려는 값(value)"이 무엇인지 분류한다.
    #   원문 비밀번호 / 고정 글자 → fails(위반)
    #   해시 함수의 결과         → stored(안전하게 저장)
    #   판단할 수 없음           → unresolved(확인 못 함)
    if isinstance(value, (ast.Name, ast.Attribute, ast.Subscript)):
        # 변수나 항목을 그대로 저장하는 경우. 이름이 비밀번호처럼 보이는데 hash가 들어 있지 않으면 원문 저장이다.
        name = terminal_name(value) or ''
        if matcher.search(name) and 'hash' not in name.lower():
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
        if _is_hash_call(value, functions):
            stored.append(_obs(module, value, 'STORED_VIA_HASH', field=field_name))
        else:
            unresolved.append(_obs(module, value, 'STORED_VALUE_UNKNOWN', field=field_name))
        return
    unresolved.append(_obs(module, value, 'STORED_VALUE_UNKNOWN', field=field_name))


def _is_hash_call(call, functions, depth=0):
    # 이 함수 호출의 결과가 "해시된 값"이라고 말할 수 있는가?
    # 인정하는 경우는 두 가지뿐이다.
    #   1) hasher.hash(...)처럼 .hash(...)를 직접 부른 경우
    #   2) 프로젝트 안의 같은 이름 함수가 "모든 반환값이 해시 호출의 결과"인 경우
    # 함수 안에서 .hash()를 어딘가에서 한 번 부른 것만으로는 인정하지 않는다. (그러고도 원문을 반환할 수 있기 때문이다.)
    # depth는 함수 안의 함수 안의 함수... 를 따라가는 깊이이다. 너무 깊어지면(2단계 초과) 멈춘다.
    if isinstance(call.func, ast.Attribute) and call.func.attr == 'hash':
        return True
    name = terminal_name(call.func)
    if depth > 2 or not name:
        return False
    definitions = functions.get(name, [])  # 이 이름으로 정의된 함수들
    # 정의가 있어야 하고, 같은 이름의 모든 정의가 해시 결과만 반환해야 한다.
    return bool(definitions) and all(_returns_hash(function, functions, depth) for _owner, function in definitions)


def _returns_hash(function, functions, depth):
    # 이 함수가 "해시 호출의 결과만" 반환하는가? 모든 return이 해시 결과여야 True다.
    hashed = set()  # 해시 결과를 담아 둔 변수 이름들. 예) h = hasher.hash(pw) 의 h
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Call, ast.Await)):
            value = node.value.value if isinstance(node.value, ast.Await) else node.value
            if isinstance(value, ast.Call) and _is_hash_call(value, functions, depth + 1):
                hashed.update(target.id for target in node.targets if isinstance(target, ast.Name))
    returns = [node.value for node in ast.walk(function) if isinstance(node, ast.Return) and node.value is not None]
    if not returns:
        return False  # 아무것도 반환하지 않는 함수는 해시를 돌려준다고 할 수 없다.
    for value in returns:
        if isinstance(value, ast.Await):
            value = value.value
        if isinstance(value, ast.Call) and _is_hash_call(value, functions, depth + 1):
            continue  # 해시 호출의 결과를 바로 반환
        if isinstance(value, ast.Name) and value.id in hashed:
            continue  # 해시 결과를 담은 변수를 반환
        return False  # 그 밖의 값(예: 원문 비밀번호)을 반환하는 경로가 있다.
    return True


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

def check_duplicate_email(modules, criteria):
    # REQ-003: 같은 이메일로 두 번 가입할 수 없게 막혀 있는가?
    # 확인하는 것 두 가지:
    #   ① 이메일 칸에 UNIQUE(중복 금지) 설정이 있는가
    #   ② 이메일을 비교하기 전에 정리(앞뒤 공백 제거 strip, 소문자로 바꾸기 lower)하는가
    # ①이 없으면 FAIL, ①은 있는데 ②가 빠졌으면 FAIL, 이메일 칸 자체를 못 찾으면 UNVERIFIED, 모두 있으면 PASS.
    rules = criteria['duplicateEmail']
    email = _email_names(criteria)
    # unique=UNIQUE 설정을 찾은 곳, plain=UNIQUE 없는 이메일 칸을 찾은 곳, normalized=확인한 정리 방법("strip", "lower")
    unique, plain, normalized = [], [], set()
    for module in modules.values():
        normalized |= _pydantic_normalization(module.tree, email)  # pydantic 설정으로 정리하는 경우도 인정
        for node in ast.walk(module.tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                # "email = Column(...)"처럼 이메일 이름의 변수에 DB 칸 정의를 대입한 경우
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and email.search(target.id) and isinstance(node.value, ast.Call):
                        _column_uniqueness(module, node.value, unique, plain)
            if isinstance(node, ast.Call):
                name = terminal_name(node.func) or ''
                # Column("email", ...)처럼 칸 이름을 글자로 준 경우
                if name in ('Column', 'mapped_column') and node.args and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.args[0].value, str) and email.search(node.args[0].value):
                    _column_uniqueness(module, node, unique, plain)
                # UniqueConstraint("email") : 이메일 칸에 대한 중복 금지 제약
                if name == 'UniqueConstraint' and any(
                        isinstance(arg, ast.Constant) and isinstance(arg.value, str) and email.search(arg.value)
                        for arg in node.args):
                    unique.append(_obs(module, node, 'UNIQUE_CONSTRAINT'))
                # Index("이름", "email", unique=True) : 중복 금지 인덱스
                if name == 'Index' and _true_keyword(node, 'unique') and any(
                        isinstance(arg, ast.Constant) and isinstance(arg.value, str) and email.search(arg.value)
                        for arg in node.args[1:]):
                    unique.append(_obs(module, node, 'UNIQUE_INDEX'))
                # something.strip() / .lower() / .casefold() 호출이 이메일 값에 적용되었는지 확인한다.
                if isinstance(node.func, ast.Attribute) and node.func.attr in ('strip', 'lower', 'casefold') \
                        and _touches_email(node, module, email):
                    normalized.add('lower' if node.func.attr == 'casefold' else node.func.attr)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                _sql_email(module, node, email, unique, plain)  # SQL 글자 안의 이메일 칸 정의도 살펴본다.
    if not unique:
        if plain:
            return CheckOutcome(FAIL, 'EMAIL_UNIQUE_MISSING', plain)  # 이메일 칸은 있는데 UNIQUE가 없다.
        return CheckOutcome(UNVERIFIED, 'EMAIL_COLUMN_NOT_FOUND')     # 이메일 칸 자체를 찾지 못했다.
    missing = [step for step in rules['requireNormalization'] if step not in normalized]  # 빠진 정리 방법
    if missing:
        return CheckOutcome(FAIL, 'EMAIL_NORMALIZATION_MISSING',
                            unique + [Observation(unique[0].path, unique[0].line, 'NORMALIZATION_MISSING',
                                                  {'missing': missing})])
    return CheckOutcome(PASS, 'STATIC_DECLARATION', unique)


def _sql_email(module, node, email, unique, plain):
    # SQL 문장이 들어 있는 글자에서 이메일 칸 정의를 찾는다.
    #   CREATE TABLE ... 안의 이메일 칸 줄에 UNIQUE가 있으면 unique, 없으면 plain으로 기록한다.
    #   CREATE UNIQUE INDEX ... 문이 이메일 칸에 대한 것이면 unique로 기록한다.
    text = node.value.lower()
    if 'create unique index' in text and any(email.search(line) for line in text.splitlines()):
        unique.append(_obs(module, node, 'SQL_UNIQUE_INDEX'))
    if 'create table' in text:
        for line in text.splitlines():
            if email.search(line):
                (unique if 'unique' in line else plain).append(_obs(module, node, 'SQL_EMAIL_COLUMN'))


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


def _touches_email(call, module, email):
    # `.strip()`이나 `.lower()` 같은 호출이 "이메일 값"에 적용되었는지 판단한다.
    # 이메일에 쓴 것으로 인정하는 경우:
    #   1) 호출 대상 식에 이메일처럼 보이는 이름이 있다. 예) email.strip(), payload.email.lower()
    #   2) 이름에 email이 들어간 함수(예: normalize_email) 안에서, 그 함수가 받은 값(매개변수)에 적용했다.
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
    for node in ast.walk(module.tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and email.search(node.name) \
                and any(child is call for child in ast.walk(node)):
            params = {arg.arg for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]}
            if root.id in params - {'self', 'cls'}:
                return True
    return False


# ---------------------------------------------------------------- REQ-006 비밀번호·해시 노출

def check_exposure(modules, criteria):
    # REQ-006: 비밀번호나 그 해시가 밖으로 새어 나가는 길이 있는가?
    # 새어 나갈 수 있는 길 네 가지를 살펴본다.
    #   ① 로그·출력(print, logger.info 등)에 찍는다   ② 오류 메시지(raise ...)에 넣는다
    #   ③ API 응답으로 돌려준다                        ④ 입력 검증 오류 응답에 입력값(비밀번호)이 그대로 실린다
    # 새는 길을 찾으면 FAIL, 확신할 수 없는 곳이 있으면 UNVERIFIED, 못 찾았으면 PASS.
    rules = criteria['exposure']
    matcher = _sensitive(criteria)
    fails, unresolved = [], []
    sensitive_models = _models_with_sensitive_fields(modules, matcher)  # 비밀번호 칸이 있는 입력·응답 모델들
    functions = _functions(modules)
    uses_fastapi, handler_found = False, False  # FastAPI를 쓰는가 / 검증 오류 처리기를 찾았는가
    seen = set()  # 이미 살펴본 코드 조각들(같은 곳을 두 번 판정하지 않으려고 기록)
    for module in modules.values():
        # 함수마다 "비밀번호 필드를 가진 객체를 담은 변수"가 다르다. 그래서 안쪽 함수부터 차례로 살펴보고,
        # 각 코드 조각은 "가장 가까운 함수의 변수 목록"을 기준으로 딱 한 번만 판정한다.
        scopes = [(node, _sensitive_vars(node, sensitive_models)) for node in ast.walk(module.tree)
                  if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
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
                    if _is_output_call(node, name, rules) and sensitive_refs(node, matcher, svars):
                        fails.append(_obs(module, node, 'SENSITIVE_LOGGED'))
                    # ④ app.add_exception_handler(RequestValidationError, 처리기) 로 등록한 검증 오류 처리기를 찾는다.
                    if isinstance(node.func, ast.Attribute) and node.func.attr == 'add_exception_handler' \
                            and node.args and (terminal_name(node.args[0]) or '') == 'RequestValidationError':
                        handler_found = True
                        target = terminal_name(node.args[1]) if len(node.args) > 1 else None
                        for owner, function in functions.get(target or '', []):
                            _check_validation_handler(owner, function, fails, unresolved)
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
                            _check_validation_handler(module, node, fails, unresolved)
                        # ③ @router.post(...) 같은 API 경로 함수의 응답을 확인한다.
                        if decorator.func.attr in rules['routeMethods']:
                            _check_route(module, node, decorator, matcher, sensitive_models, svars, fails, unresolved)
    if uses_fastapi and rules['requireValidationErrorFilter'] and sensitive_models and not handler_found:
        # FastAPI는 입력 검증에 실패하면 기본으로 "입력값(비밀번호 포함)"을 오류 응답에 담아 돌려준다.
        # 비밀번호 칸이 있는 모델을 쓰는데 이를 가려 주는 처리기가 없다면 노출 위험이 있다.
        first = next(iter(modules.values()))
        fails.append(Observation(first.path, 0, 'VALIDATION_ERROR_ECHOES_INPUT'))
    if fails:
        return CheckOutcome(FAIL, 'EXPOSURE_PATH_FOUND', fails)
    if unresolved:
        return CheckOutcome(UNVERIFIED, 'ANALYSIS_INCOMPLETE', unresolved)
    return CheckOutcome(PASS, 'NO_STATIC_EXPOSURE_PATH')


def _is_output_call(call, name, rules):
    # 이 호출이 "밖으로 글자를 내보내는 호출"(로그, 화면 출력, 표준 오류 출력 등)인가?
    if name in rules.get('outputCalls', ['print']):
        return True  # print(...), sys.stderr.write(...) 같은 직접 출력
    if isinstance(call.func, ast.Attribute) and call.func.attr in rules['logMethods']:
        # logger.info(...)처럼 로그 함수 이름(info, error 등)으로 부른 경우.
        # 부르는 대상이 logging 모듈이거나, 이름에 log가 들어 있으면(logger, app_log 등) 로그 호출로 본다.
        base = qualname(call.func.value, {}) or ''
        return name.startswith('logging.') or 'log' in base.lower()
    return False


def _models_with_sensitive_fields(modules, matcher):
    # "비밀번호처럼 보이는 필드를 가진 입력·응답 모델"의 클래스 이름들을 찾는다. 예) class SignupRequest: password: str
    # SecretStr 형식의 필드는 출력해도 값이 가려져서 안전하므로 제외한다.
    names = set()
    for module in modules.values():
        for node in ast.walk(module.tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    # "이름: 형식" 모양의 필드 선언(AnnAssign)만 본다.
                    if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) \
                            and matcher.search(item.target.id) \
                            and not any(isinstance(part, ast.Name) and part.id == 'SecretStr'
                                        for part in ast.walk(item.annotation)):
                        names.add(node.name)
    return names


def _sensitive_vars(function, sensitive_models):
    # 한 함수 안에서 "비밀번호 필드를 가진 모델의 객체"를 담고 있는 변수 이름들을 찾는다.
    #   1) 매개변수의 형식 표시가 그 모델인 것. 예) def signup(payload: SignupRequest)  → payload
    #   2) 그 모델로 직접 만든 것. 예) request = SignupRequest(...)  → request
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
    return frozenset(names)


def _check_validation_handler(module, function, fails, unresolved):
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
    # 더 이상 늘어나지 않을 때까지 반복해서 찾는다.
    tainted, changed = set(), True
    while changed:
        changed = False
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and raw(node.value, tainted):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id not in tainted:
                        tainted.add(target.id)
                        changed = True

    def removes_input(loop):
        # 반복문이 각 항목에서 "input"을 지우는가? (item.pop("input") 또는 del item["input"])
        names = {item.id for item in ast.walk(loop.target) if isinstance(item, ast.Name)}  # 반복문이 꺼내 쓰는 변수 이름
        for item in ast.walk(loop):
            if (isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr == 'pop'
                    and item.args and isinstance(item.args[0], ast.Constant) and item.args[0].value == 'input'
                    and isinstance(item.func.value, ast.Name) and item.func.value.id in names):
                return True
            if isinstance(item, ast.Delete):
                for target in item.targets:
                    if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                            and target.value.id in names and isinstance(target.slice, ast.Constant)
                            and target.slice.value == 'input'):
                        return True
        return False

    # "input을 지우는 반복문"으로 정리한 오류 목록(또는 그 변수)은 안전하다고 기록해 둔다.
    cleaned_loops, cleaned_names = set(), set()
    for node in ast.walk(function):
        if isinstance(node, ast.For) and raw(node.iter, tainted) and removes_input(node):
            cleaned_loops.add(id(node.iter))
            if isinstance(node.iter, ast.Name):
                cleaned_names.add(node.iter.id)

    # 이제 원본 오류 목록을 "사용하는 곳"을 모두 찾아서 안전한 사용인지 판정한다.
    reported = set()  # 이미 판정한 코드 조각
    for node in ast.walk(function):
        if (isinstance(node, ast.Attribute) and node.attr == 'body' and isinstance(node.value, ast.Name)
                and node.value.id in exc_names):
            # exc.body는 사용자가 보낸 요청 본문 전체(비밀번호 포함)이다. 응답에 쓰면 위험하다.
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
            elif isinstance(element, ast.Name) and element.id in {
                    item.id for item in ast.walk(parent.target) if isinstance(item, ast.Name)}:
                # [e for e in exc.errors()] 처럼 항목을 그대로 복사하면 input도 그대로 들어간다.
                fails.append(_obs(module, use, 'VALIDATION_ERROR_ECHOES_INPUT'))
            else:
                unresolved.append(_obs(module, use, 'VALIDATION_ERROR_SHAPE_UNKNOWN'))  # 모르는 모양
            continue
        if isinstance(parent, ast.For) and parent.iter is use:
            # 일반 반복문으로 오류 목록을 훑는 경우. 위에서 "input을 지운다"고 확인된 반복문이 아니면 모른다고 처리한다.
            if id(use) not in cleaned_loops:
                unresolved.append(_obs(module, use, 'VALIDATION_ERROR_SHAPE_UNKNOWN'))
            continue
        if isinstance(use, ast.Name) and use.id in cleaned_names:
            continue  # input을 지운 뒤의 목록을 쓰는 것은 안전하다.
        fails.append(_obs(module, use, 'VALIDATION_ERROR_ECHOES_INPUT'))  # 그 밖의 사용은 원본을 그대로 쓰는 것


def _check_route(module, function, decorator, matcher, sensitive_models, svars, fails, unresolved):
    # API 경로 함수(@router.post 등이 붙은 함수)가 응답으로 비밀번호를 돌려주는지 확인한다.
    #   decorator = 그 함수 위에 붙은 꼬리표. 응답 모양을 정하는 response_model 같은 인자가 들어 있을 수 있다.
    for keyword in decorator.keywords:
        if keyword.arg == 'response_model' and (terminal_name(keyword.value) or '') in sensitive_models:
            # 응답 모양 자체가 비밀번호 칸이 있는 모델이다.
            fails.append(_obs(module, decorator, 'SENSITIVE_RESPONSE_MODEL'))
    has_response_model = any(keyword.arg == 'response_model' for keyword in decorator.keywords)
    # response_model이 지정되어 있으면 돌려주는 객체는 그 모델에 있는 필드만 응답에 나간다.
    # 그래서 객체를 통째로 반환해도 노출로 보지 않는다(visible_vars를 비운다).
    visible_vars = frozenset() if has_response_model else svars
    for node in ast.walk(function):
        if not isinstance(node, ast.Return) or node.value is None:
            continue  # return 문만 본다.
        value = node.value
        if isinstance(value, ast.Dict):
            for key in value.keys:
                # 응답 사전의 키 이름이 비밀번호처럼 보이면 위험하다. 예) {"password_hash": ...}
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and matcher.search(key.value):
                    fails.append(_obs(module, node, 'SENSITIVE_RESPONSE_KEY'))
        if sensitive_refs(value, matcher, visible_vars):
            fails.append(_obs(module, node, 'SENSITIVE_RESPONSE_VALUE'))  # 응답 값에 비밀번호를 사용
        elif not has_response_model and _dumps_unknown_object(value):
            # 객체의 전체 필드를 펼쳐서 응답에 넣었다. 어떤 필드가 들어 있는지 코드만으로는 알 수 없다.
            unresolved.append(_obs(module, node, 'UNFILTERED_OBJECT_RESPONSE'))
        elif isinstance(value, (ast.Name, ast.Attribute)) and not has_response_model:
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
    # "코드에 비밀번호가 직접 적혀 있다"는 경고가 정말 맞는지, 그 줄을 보고 판정한다.
    # 그 줄에서 (대입 대상 이름, 따옴표 글자) 쌍을 찾아 하나씩 살펴본다.
    #   안내 문구(공백·한글이 들어 있는 문장) → 오탐(FALSE_POSITIVE)
    #   필드 이름 글자(예: "password")        → 오탐(FALSE_POSITIVE)
    #   비밀번호처럼 보이는 이름에 자격증명 같은 값을 넣음(예: DB_PASSWORD에 실제 비밀번호처럼 보이는 글자를 대입) → 진짜 문제(CONFIRMED)
    #   그 외                                  → 판정하지 않음(None)
    pairs = _literal_pairs(module.tree, line)
    if not pairs:
        return None
    outcomes = []
    for name, literal in pairs:
        text = literal.value
        if _looks_like_message(text):
            outcomes.append(('FALSE_POSITIVE', _obs(module, literal, 'MESSAGE_TEXT_NOT_CREDENTIAL', ruleId=rule)))
        elif matcher.search(text):
            outcomes.append(('FALSE_POSITIVE', _obs(module, literal, 'FIELD_NAME_NOT_CREDENTIAL', ruleId=rule)))
        elif name is not None and matcher.search(name):
            outcomes.append(('CONFIRMED', _obs(module, literal, 'HARDCODED_CREDENTIAL', ruleId=rule)))
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
