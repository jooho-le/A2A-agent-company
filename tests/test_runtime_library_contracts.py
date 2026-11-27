"""A pinned, public declaration is separate from a measured runtime exception."""
import ast
from hashlib import sha256

import pytest

from agents.runtime.library_contracts import call_parameters
from agents.runtime.normal_request_repair import linkable_sites, priority_repair_evidence

IMAGE = 'sha256:c712fdf445fe01217001440459070bf1e2b0a8d94ad3b9103bacfedddf7dae5a'
LOCK = 'sha256:d27679005b83257d24f6f7e86fdc7b1a47c92065bc068c4ab14e904675e6dcc0'
ENV = {'containerImageDigest': IMAGE, 'dependencyLockHash': LOCK}
SOURCE = b'from argon2 import PasswordHasher as H\ndef f():\n    return H(type="local-secret", time_cost=91365)\n'


def contract(data=SOURCE, *, environment=ENV, files=None):
    return_call = next(n.value for n in ast.walk(ast.parse(data)) if isinstance(n, ast.Return))
    return call_parameters(return_call, data, {'main.py': data, **(files or {})}, environment)


def test_public_types_only_for_declared_arguments_in_pinned_runtime():
    expected = {'callable': 'argon2.PasswordHasher', 'imageDigest': IMAGE, 'dependencyLockHash': LOCK,
                'parameters': {'time_cost': 'int', 'type': 'argon2.low_level.Type'}}
    assert contract() == expected
    assert contract(SOURCE.replace(b'from argon2 import PasswordHasher as H', b'import argon2 as library')
                    .replace(b'return H(', b'return library.PasswordHasher(')) == expected


@pytest.mark.parametrize('environment', [None, {}, {'containerImageDigest': IMAGE, 'dependencyLockHash': 'other'},
                                         {'containerImageDigest': 'other', 'dependencyLockHash': LOCK},
                                         {'containerImageDigest': [], 'dependencyLockHash': LOCK}])
def test_unpinned_environment_cannot_borrow_local_or_other_image_annotations(environment):
    assert contract(environment=environment) is None


@pytest.mark.parametrize('data', [
    SOURCE.replace(b'from argon2', b'from shadow'),
    SOURCE.replace(b'f():', b'f(H):'),
    SOURCE.replace(b'from argon2', b'from .argon2'),
    SOURCE.replace(b'def f():', b'from elsewhere import *\ndef f():'),
    SOURCE.replace(b'def f():', b'H = some_function\ndef f():'),
    SOURCE.replace(b'def f():', b'import argon2\nargon2.PasswordHasher = other\ndef f():'),
    SOURCE.replace(b'def f():', b"setattr(H, '__init__', other)\ndef f():"),
    SOURCE.replace(b'type="local-secret", time_cost=91365', b'**options'),
    SOURCE.replace(b'type="local-secret", time_cost=91365', b'secret_arg="private"'),
    b'def f():\n    return H(type="private")\nfrom argon2 import PasswordHasher as H',
])
def test_shadowed_dynamic_and_unrelated_calls_do_not_receive_declarations(data):
    assert contract(data) is None


def test_local_shadow_and_different_snapshot_expression_are_omitted():
    assert contract(files={'pkg/argon2.py': b''}) is None
    node = next(n for n in ast.walk(ast.parse(SOURCE)) if isinstance(n, ast.Call))
    different = SOURCE.replace(b'time_cost=91365', b'encoding="private"')
    assert call_parameters(node, different, {'main.py': different}, ENV) is None


def test_current_measured_frame_and_ready_correction_preserve_contract_provenance():
    [site] = linkable_sites([{'sourceSha256': sha256(SOURCE).hexdigest(), 'line': 3}],
                            {'main.py': SOURCE}, environment=ENV)
    assert site['libraryContract'] == contract()
    assert 'observedCause' not in site
    blocker = {'sourceSites':[site], 'httpStatuses':[500], 'exceptionTypes':[], 'blockedCheckCount':0}
    evidence = priority_repair_evidence({'activeRepairFocus':{'brief':{'blockingPrerequisite':blocker}}})
    assert evidence['sourceSites'][0]['libraryContract'] == contract()
    assert 'local-secret' not in repr(evidence) and '91365' not in repr(evidence) and 'Type.ID' not in repr(evidence)
