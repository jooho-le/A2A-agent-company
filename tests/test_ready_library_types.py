"""Literal READY checks need both a pinned runtime rule and an unambiguous public API binding."""
import pytest

from agents.runtime.ready_library_types import ready_library_type_mismatches

ENV = {'containerImageDigest': 'sha256:c712fdf445fe01217001440459070bf1e2b0a8d94ad3b9103bacfedddf7dae5a',
       'dependencyLockHash': 'sha256:d27679005b83257d24f6f7e86fdc7b1a47c92065bc068c4ab14e904675e6dcc0'}
BAD = b"""from argon2 import PasswordHasher as H
def signup():
    try:
        return H(type='private-value')
    except Exception:
        return 500
"""


def check(source=BAD, *, environment=ENV, other=None, changed=None):
    return ready_library_type_mismatches({'main.py': source, **(other or {})}, environment, changed_paths=changed)


def test_exact_runtime_rejects_string_and_never_repeats_its_value_or_a_solution():
    assert check() == [{'toolPath': 'source/main.py', 'line': 4, 'callable': 'argon2.PasswordHasher',
                        'parameter': 'type', 'requiredType': 'argon2.low_level.Type', 'actualType': 'str'}]


def test_qualified_module_import_and_keyword_type_violation():
    assert check(BAD.replace(b'from argon2 import PasswordHasher as H', b'import argon2 as crypto')
                    .replace(b'H(type=', b'crypto.PasswordHasher(type=')) == check()


@pytest.mark.parametrize('args', [
    b'type=Type.ID', b'type=choose_type()', b'type=config.kind', b'**options', b'',
    b'time_cost=True', b'time_cost=3, parallelism=2, memory_cost=20480',
    b'encoding="utf8"', b'type=choose_type(), encoding="utf8"',
])
def test_accepted_literal_or_dynamic_expression_remains_eligible(args):
    assert check(BAD.replace(b"type='private-value'", args)) == []


@pytest.mark.parametrize(('value', 'kw', 'required', 'actual'), [
    (b'3.0', 'time_cost', 'int', 'float'), (b'None', 'type', 'argon2.low_level.Type', 'NoneType'),
    (b'{}', 'type', 'argon2.low_level.Type', 'dict'), (b"b'private'", 'encoding', 'str', 'bytes'),
    (b'-2.2', 'memory_cost', 'int', 'float'),
])
def test_other_verifiably_strict_named_parameters(value, kw, required, actual):
    [issue] = check(BAD.replace(b"type='private-value'", kw.encode() + b'=' + value))
    assert (issue['parameter'], issue['requiredType'], issue['actualType']) == (kw, required, actual)


@pytest.mark.parametrize('scope', [{}, {'containerImageDigest': ENV['containerImageDigest']},
                                 {**ENV, 'containerImageDigest': 'sha256:other'},
                                 {**ENV, 'dependencyLockHash': 'sha256:other'}])
def test_environment_must_match_pinned_rejection_evidence(scope):
    assert check(environment=scope) == []


@pytest.mark.parametrize('source', [
    BAD.replace(b'from argon2', b'from project'),
    BAD.replace(b'def signup():', b'from elsewhere import *\ndef signup():'),
    BAD.replace(b'def signup():', b'H = CustomFactory\ndef signup():'),
    BAD.replace(b'def signup():', b'def signup(H):'), BAD.replace(b"=\'private-value\'", b"=dynamic()"),
    BAD.replace(b'H(type=', b'H(**extra, type='),
    BAD.replace(b'def signup():', b'setattr(H, "__new__", factory)\ndef signup():'),
    BAD + b'\n(\n',
])
def test_ambiguous_and_nonmatching_sites_do_not_block(source):
    assert not check(source)


def test_snapshot_files_outside_selected_diff_can_shadow_library():
    assert not check(other={'argon2.py': b''}, changed=['main.py'])
    assert not check(other={'argon2/__init__.py': b''}, changed=['main.py'])
    assert not check(changed=['other.py'])


def test_tests_and_large_modules_are_excluded():
    files = {'tests/check.py': BAD, 'test_demo.py': BAD, 'pkg/conftest.py': BAD,
             'oversized.py': BAD + b'#' * 128000}
    assert ready_library_type_mismatches(files, ENV) == []


def test_positional_arguments_use_the_same_checked_runtime_signature():
    [issue] = check(BAD.replace(b"type='private-value'", b"2, 20000, 1, 32, 16, 'utf8', 'private-value'"))
    assert issue['parameter'] == 'type' and issue['actualType'] == 'str'
    assert check(BAD.replace(b"type='private-value'", b"2, 20000, *items, type='private-value'")) == []
    assert check(BAD.replace(b"type='private-value'", b"'wrong', time_cost=2")) == []
    assert check(BAD.replace(b"type='private-value'", b"2, 20000, 1, 32, 16, 'utf-8', Type.ID")) == []


@pytest.mark.parametrize('bootstrap', [
    b'import argon2 as crypto\ncrypto.PasswordHasher = adapt\n',
    b'from argon2 import PasswordHasher as C\nC.__init__ = factory\n',
    b"import sys\nsys.modules['argon2'] = dependency\n",
    b"from unittest.mock import patch\npatch('argon2.PasswordHasher', factory).start()\n",
    b"from unittest.mock import patch as p\nimport argon2 as a\np.object(a, 'PasswordHasher', factory).start()\n",
])
def test_observable_cross_module_rebinding_suppresses_static_rejection(bootstrap):
    assert check(other={'zz_startup.py': bootstrap}, changed=['main.py']) == []
