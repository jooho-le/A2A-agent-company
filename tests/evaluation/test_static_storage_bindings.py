"""D1 must bind each normalization/hash claim to the value actually stored.

These are trusted source fixtures, inspected as AST only. They do not invoke
Docker, generated service code, or an external model.
"""
from copy import deepcopy

import pytest

from tests.evaluation.signup_examples import GOOD, GOOD_EXPECTED
from tests.evaluation.test_security_assess import outcomes


def before_add(lines, *, imports=''):
    files = deepcopy(GOOD)
    files['routers.py'] = imports + files['routers.py'].replace('    db.add(user)\n', lines + '    db.add(user)\n')
    return files


@pytest.mark.parametrize('statement', [
    'text("UPDATE users SET password_hash = :credential")',
    'statement',
])
def test_sqlalchemy_text_plaintext_update_is_not_covered_by_an_earlier_hash(statement):
    setup = '    statement = text("UPDATE users SET password_hash = :credential")\n' if statement == 'statement' else ''
    files = before_add(setup + '    db.add(user)\n    db.flush()\n'
                       + f'    db.execute({statement}, {{"credential": payload.password}})\n',
                       imports='from sqlalchemy import text\n')
    assert outcomes(files)[0]['REQ-005'] == 'FAIL'


@pytest.mark.parametrize('declaration', ['    field = "password_hash"\n', ''])
def test_setattr_password_write_resolves_fixed_keys_and_rejects_unknown_keys(declaration):
    files = before_add(declaration + '    setattr(user, field, payload.password)\n')
    assert outcomes(files)[0]['REQ-005'] == ('FAIL' if declaration else 'UNVERIFIED')


@pytest.mark.parametrize('value', ['credential', '"constant-password"'])
def test_unknown_setattr_field_does_not_hide_alias_or_constant_password_writes(value):
    files = before_add('    credential = payload.password\n    field = "password_" + "hash"\n'
                       + f'    setattr(user, field, {value})\n')
    assert outcomes(files)[0]['REQ-005'] == 'UNVERIFIED'


def test_unknown_sql_target_with_password_parameter_is_unverified():
    files = before_add('    db.execute(statement, {"credential": payload.password})\n')
    assert outcomes(files)[0]['REQ-005'] == 'UNVERIFIED'


@pytest.mark.parametrize('statement', [
    'text("UPDATE users SET password_hash = :credential")', 'statement',
])
def test_sql_password_alias_cannot_borrow_an_earlier_safe_hash(statement):
    files = before_add('    credential = payload.password\n'
                       + f'    db.execute({statement}, {{"credential": credential}})\n',
                       imports='from sqlalchemy import text\n')
    assert outcomes(files)[0]['REQ-005'] == 'UNVERIFIED'


@pytest.mark.parametrize('statement', ['text("UPDATE users SET password_hash = \'literal\'")', 'statement'])
def test_sql_without_bind_parameters_does_not_hide_unverified_writes(statement):
    files = before_add(f'    db.execute({statement})\n', imports='from sqlalchemy import text\n')
    assert outcomes(files)[0]['REQ-005'] == 'UNVERIFIED'


def test_fixed_password_field_storing_an_actual_hash_still_passes():
    files = before_add('    field = "password_hash"\n    setattr(user, field, hash_password(payload.password))\n')
    assert outcomes(files)[0] == GOOD_EXPECTED


@pytest.mark.parametrize('sql,parameters', [
    ('SELECT 1', ''),
    ('SELECT password_hash FROM users WHERE password_hash = :credential', ', {"credential": payload.password}'),
    ('SELECT password_hash FROM users WHERE email = :email', ', {"email": payload.email}'),
    ("SELECT 'UPDATE password_hash'", ', {"credential": payload.password}'),
])
def test_unrelated_sql_read_does_not_invalidate_password_storage(sql, parameters):
    files = before_add(f'    db.execute(text({sql!r}){parameters})\n', imports='from sqlalchemy import text\n')
    assert outcomes(files)[0] == GOOD_EXPECTED


def raw_model_files():
    files = deepcopy(GOOD)
    files['schemas.py'] += '\nclass RawSignupRequest(BaseModel):\n    email: str\n    password: str\n'
    files['routers.py'] = files['routers.py'].replace('from schemas import SignupRequest',
        'from schemas import SignupRequest, RawSignupRequest').replace('payload: SignupRequest', 'payload: RawSignupRequest')
    return files


def test_unrelated_normalized_model_is_not_proof_for_the_raw_model_used_by_signup():
    assert outcomes(raw_model_files())[0]['REQ-003'] == 'FAIL'


def test_explicit_normalization_of_raw_model_input_still_passes():
    files = raw_model_files()
    files['routers.py'] = files['routers.py'].replace('email=payload.email,', 'email=payload.email.strip().lower(),')
    assert outcomes(files)[0] == GOOD_EXPECTED


@pytest.mark.parametrize('write', [
    '    payload.email = payload.email.upper()\n',
    '    setattr(payload, "email", payload.email.upper())\n',
    '    def change(item):\n        item.email = item.email.upper()\n    change(payload)\n',
])
def test_input_validator_is_not_proof_after_the_email_value_is_overwritten(write):
    files = deepcopy(GOOD)
    files['routers.py'] = files['routers.py'].replace('    user = User(', write + '    user = User(')
    assert outcomes(files)[0]['REQ-003'] != 'PASS'


def test_normalized_input_and_unrelated_extra_model_preserve_good_contract():
    files = deepcopy(GOOD)
    files['schemas.py'] += '\nclass UnusedRawModel(BaseModel):\n    email: str\n'
    assert outcomes(files)[0] == GOOD_EXPECTED


def test_model_wide_post_validator_cannot_be_ignored_as_an_email_write():
    files = deepcopy(GOOD)
    files['schemas.py'] = 'from pydantic import model_validator\n' + files['schemas.py']
    files['schemas.py'] += ('\n    @model_validator(mode="after")\n'
                           '    def change_email(self):\n'
                           '        self.email = self.email.upper()\n        return self\n')
    assert outcomes(files)[0]['REQ-003'] != 'PASS'
