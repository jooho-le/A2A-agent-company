"""REQ-005 정적 판정이 생성 코드에 흔히 나오는 해시 도구 사용법을 맞게 판정하는지 시험한다. (D1, 2026-10-28 보강)

확인하는 것:
  - FastAPI 튜토리얼 방식(password_hash = PasswordHash.recommended())이 모델의 password_hash 칸과 이름이 겹쳐도 PASS인가?
    (클래스 본문에서 정한 속성은 함수 안의 이름 하나가 가리킬 수 없다.)
  - passlib Argon2(from passlib.hash import argon2, argon2.using(...), CryptContext(schemes=["argon2"]))를 승인 도구로
    인정하고 설정 숫자·type·salt를 판정하는가? bcrypt 등이 기본인 CryptContext나 scheme="bcrypt" 지정은 FAIL인가?
  - 가짜 라이브러리 파일, import 뒤 덮어쓰기, 지역 변수 가림, 클래스의 global 선언, 다른 파일의 모듈 속성 덮어쓰기는
    PASS가 되지 않는가?
"""
import json

import pytest

from evaluation.security.assess import evaluate
from tests.evaluation.signup_examples import GOOD
from tests.evaluation.test_security_assess import make_request

HASH_FUNCTION = 'def hash_password(password: str) -> str:\n    return {name}.hash(password)\n'


def module(header, name):
    return header + '\n\n\n' + HASH_FUNCTION.format(name=name)


CASES = [
    ('pwdlib_recommended_named_like_column', module(
        'from pwdlib import PasswordHash\n\npassword_hash = PasswordHash.recommended()', 'password_hash'), {}, 'PASS'),
    ('passlib_context_argon2', module(
        'from passlib.context import CryptContext\n\npwd_context = CryptContext(schemes=["argon2"], deprecated="auto")',
        'pwd_context'), {}, 'PASS'),
    ('passlib_handler', module('from passlib.hash import argon2', 'argon2'), {}, 'PASS'),
    ('passlib_handler_alias', module('from passlib.hash import argon2 as pw', 'pw'), {}, 'PASS'),
    ('passlib_using_policy_values', module(
        'from passlib.hash import argon2\n\nhasher = argon2.using(memory_cost=19456, rounds=2, parallelism=1)', 'hasher'),
     {}, 'PASS'),
    ('passlib_context_default_argon2', module(
        'from passlib.context import CryptContext\n\npwd_context = CryptContext(schemes=["bcrypt", "argon2"], default="argon2")',
        'pwd_context'), {}, 'PASS'),
    ('passlib_using_weak_memory', module(
        'from passlib.hash import argon2\n\nhasher = argon2.using(memory_cost=1024)', 'hasher'), {}, 'FAIL'),
    ('passlib_using_type_i', module('from passlib.hash import argon2\n\nhasher = argon2.using(type="i")', 'hasher'),
     {}, 'FAIL'),
    ('passlib_using_fixed_salt', module(
        'from passlib.hash import argon2\n\nhasher = argon2.using(salt=b"0123456789abcdef")', 'hasher'), {}, 'FAIL'),
    ('passlib_context_weak_memory', module(
        'from passlib.context import CryptContext\n\npwd_context = CryptContext(schemes=["argon2"], argon2__memory_cost=4096)',
        'pwd_context'), {}, 'FAIL'),
    ('passlib_context_bcrypt', module(
        'from passlib.context import CryptContext\n\npwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")',
        'pwd_context'), {}, 'FAIL'),
    ('passlib_context_bcrypt_text', module(
        'from passlib.context import CryptContext\n\npwd_context = CryptContext(schemes="bcrypt,argon2")', 'pwd_context'),
     {}, 'FAIL'),
    ('passlib_context_scheme_override',
     'from passlib.context import CryptContext\n\npwd_context = CryptContext(schemes=["argon2", "bcrypt"])\n\n\n'
     'def hash_password(password: str) -> str:\n    return pwd_context.hash(password, scheme="bcrypt")\n', {}, 'FAIL'),
    ('passlib_bcrypt_handler', module('from passlib.hash import bcrypt', 'bcrypt'), {}, 'FAIL'),
    ('passlib_context_all_settings', module(
        'from passlib.context import CryptContext\n\npwd_context = CryptContext(schemes=["argon2"], all__rounds=1)',
        'pwd_context'), {}, 'UNVERIFIED'),
    ('passlib_context_spread_settings', module(
        'from passlib.context import CryptContext\n\nOPTS = {"argon2__memory_cost": 1024}\n'
        'pwd_context = CryptContext(schemes=["argon2"], **OPTS)', 'pwd_context'), {}, 'UNVERIFIED'),
    ('fake_passlib_package', module('from passlib.hash import argon2', 'argon2'),
     {'passlib/hash.py': 'class argon2:\n    @staticmethod\n    def hash(value):\n        return value\n'}, 'UNVERIFIED'),
    ('fake_argon2_module', module('from argon2 import PasswordHasher\n\nHASHER = PasswordHasher()', 'HASHER'),
     {'argon2.py': 'class PasswordHasher:\n    def hash(self, value):\n        return value\n'}, 'UNVERIFIED'),
    ('handler_rebound_after_import', module('from passlib.hash import argon2\n\nargon2 = object()', 'argon2'),
     {}, 'UNVERIFIED'),
    ('local_name_shadows_hasher',
     'from pwdlib import PasswordHash\n\npassword_hash = PasswordHash.recommended()\n\n\n'
     'def hash_password(password: str) -> str:\n    password_hash = Plain()\n    return password_hash.hash(password)\n',
     {}, 'UNVERIFIED'),
    ('class_global_rebinds_hasher', module(
        'from pwdlib import PasswordHash\n\npassword_hash = PasswordHash.recommended()\n\n\n'
        'class Patch:\n    global password_hash\n    password_hash = object()', 'password_hash'), {}, 'UNVERIFIED'),
    ('module_attribute_rebinds_hasher', module(
        'from pwdlib import PasswordHash\n\npassword_hash = PasswordHash.recommended()', 'password_hash'),
     {'patch.py': 'import security\nsecurity.password_hash = object()\n'}, 'UNVERIFIED'),
]


@pytest.mark.parametrize('name, security, extra, expected', CASES, ids=[case[0] for case in CASES])
def test_hasher_forms(name, security, extra, expected):
    files = dict(GOOD, **{'security.py': security}, **extra)
    requirement = json.loads(evaluate(make_request(files))['evidence_json'])['requirements']['REQ-005']
    assert requirement['outcome'] == expected, requirement['basis']
