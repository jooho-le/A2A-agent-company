# 정규화 반환 경로·로그 팩터리 별칭·응답 모델의 실제 필터 조건을 검사한다. 생성 코드를 실행하지 않는다.
import pytest

from tests.evaluation.signup_examples import GOOD
from tests.evaluation.test_security_assess import outcomes


def normalized(body):
    files = dict(GOOD)
    start = files['schemas.py'].index('        value = value.strip().lower()')
    end = files['schemas.py'].index('\n    @field_validator("password")', start)
    files['schemas.py'] = files['schemas.py'][:start] + body + '\n' + files['schemas.py'][end:]
    return files


@pytest.mark.parametrize('body', [
    '        raw = value\n        value = value.strip().lower()\n        value = raw\n        return value\n',
    '        raw = value\n        value = value.strip().lower()\n        return raw\n',
    '        raw = value\n        value = value.strip().lower()\n        if value:\n            value = raw\n        return value\n',
    '        raw = value\n        value = value.strip().lower()\n        return value if value else raw\n',
    '        if value:\n            return value\n        return value.strip().lower()\n',
    '        clean = value.strip().lower()\n        if clean:\n            raise ValueError("invalid")\n        return value\n',
])
def test_raw_email_return_never_counts_as_normalized(body):
    assert outcomes(normalized(body))[0]['REQ-003'] != 'PASS'


@pytest.mark.parametrize('body', [
    '        clean = value.strip().lower()\n        value = clean\n        return value\n',
    '        value = value.strip()\n        value = value.lower()\n        return value\n',
    '        if value:\n            return value.strip().lower()\n        else:\n            return value.lower().strip()\n',
    '        if value:\n            raise ValueError("invalid")\n        return value.strip().lower()\n',
])
def test_all_returned_paths_keep_normalization(body):
    assert outcomes(normalized(body))[0]['REQ-003'] == 'PASS'


def test_unsupported_normalization_flow_is_unverified():
    files = normalized('        try:\n            return value.strip().lower()\n        finally:\n            value = value\n')
    assert outcomes(files)[0]['REQ-003'] == 'UNVERIFIED'


def logged(header, expression, body=''):
    files = dict(GOOD)
    files['routers.py'] = header + files['routers.py'].replace('    db.add(user)', body + f'    {expression}\n    db.add(user)')
    return files


@pytest.mark.parametrize('header,body,call', [
    ('import logging\naudit = logging.getLogger(__name__)\n', '', 'audit.info("%s", payload.password)'),
    ('from logging import getLogger as acquire\naudit = acquire(__name__)\n', '', 'audit.warning("%s", payload.password)'),
    ('import logging as journal\naudit = journal.getLogger(__name__)\nsink = audit\n', '', 'sink.error("%s", payload.password)'),
    ('import logging\n', '    audit = logging.getLogger(__name__)\n', 'audit.debug("%s", payload.password)'),
    ('import logging\n', '    audit = logging.LoggerAdapter(logging.getLogger(__name__), {})\n', 'audit.info("%s", payload.password)'),
    ('import logging\naudit = logging.getLogger(__name__)\n', '', 'audit.info("%s", hash_password(payload.password))'),
])
def test_logger_factory_alias_detects_secret_output(header, body, call):
    assert outcomes(logged(header, call, body))[0]['REQ-006'] == 'FAIL'


def test_logger_with_only_public_values_stays_safe():
    assert outcomes(logged('import logging\naudit = logging.getLogger(__name__)\n',
                           'audit.info("%s", payload.email)'))[0]['REQ-006'] == 'PASS'


def test_reassigned_factory_result_is_not_assumed_to_be_a_logger():
    files = logged('import logging\naudit = logging.getLogger(__name__)\naudit = Other()\n',
                   'audit.info("%s", payload.password)')
    assert outcomes(files)[0]['REQ-006'] == 'UNVERIFIED'


def response(schema, value='payload.model_dump() | {"id": 1}'):
    files = dict(GOOD)
    files['schemas.py'] += '\n\nfrom pydantic import ConfigDict, model_serializer\n\n' + schema
    files['routers.py'] = files['routers.py'].replace('from schemas import SignupRequest',
        'from schemas import SignupRequest, Public').replace('status_code=201)',
        'status_code=201, response_model=Public)').replace(
        '{"success": True, "message": "회원가입에 성공했습니다."}', value)
    return files


@pytest.mark.parametrize('schema', [
    'class Public(BaseModel):\n    model_config = ConfigDict(extra="allow")\n    id: int\n',
    'class Public(BaseModel):\n    model_config = {"extra": "allow"}\n    id: int\n',
    'class Public(BaseModel):\n    class Config:\n        extra = "allow"\n    id: int\n',
    'class Parent(BaseModel):\n    model_config = ConfigDict(extra="allow")\nclass Public(Parent):\n    id: int\n',
    'class Public(BaseModel, extra="allow"):\n    id: int\n',
    'class Public(BaseModel):\n    model_config = unknown_config\n    id: int\n',
    'class Public(BaseModel):\n    id: int\n    @model_serializer\n    def output(self):\n        return self.__dict__\n',
    'class Public(External):\n    id: int\n',
    'class Public:\n    id: int\n',
])
def test_unproven_response_models_cannot_hide_sensitive_return(schema):
    assert outcomes(response(schema))[0]['REQ-006'] != 'PASS'


@pytest.mark.parametrize('schema', [
    'class Public(BaseModel):\n    id: int\n',
    'class Public(BaseModel):\n    model_config = ConfigDict(extra="ignore")\n    id: int\n',
    'class Public(BaseModel):\n    model_config = {"extra": "forbid"}\n    id: int\n',
    'class Parent(BaseModel):\n    id: int\nclass Public(Parent):\n    email: str\n',
    'class Parent(BaseModel):\n    id: int\nPublic = Parent\n',
    'class Public(BaseModel):\n    class Config:\n        extra = "ignore"\n    id: int\n',
])
def test_known_default_or_closed_response_models_keep_filtering(schema):
    assert outcomes(response(schema))[0]['REQ-006'] == 'PASS'



def test_unused_normalizer_cannot_cover_a_raw_signup_validator():
    files = normalized('        return value\n')
    files['utils.py'] = 'def unused_clean_email(email):\n    return email.strip().lower()\n'
    assert outcomes(files)[0]['REQ-003'] != 'PASS'


@pytest.mark.parametrize('schema', [
    'class Public(BaseModel, **options):\n    id: int\n',
    'class Public(BaseModel):\n    class Config:\n        if enabled:\n            extra = "allow"\n    id: int\n',
    'def ConfigDict(**kwargs):\n    return {"extra": "allow"}\n\nclass Public(BaseModel):\n    model_config = ConfigDict(extra="ignore")\n    id: int\n',
])
def test_dynamic_response_model_config_is_not_trusted(schema):
    assert outcomes(response(schema))[0]['REQ-006'] != 'PASS'


def test_unknown_filter_policy_is_unverified_instead_of_assuming_a_leak():
    files = response('class Public(BaseModel):\n    model_config = unknown_config\n    id: int\n')
    assert outcomes(files)[0]['REQ-006'] == 'UNVERIFIED'
