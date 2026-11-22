from hashlib import sha256

import pytest

from agents.runtime.repair_dependencies import indirect_frame_refs


def compare(before, after, line=2):
    frame = {'toolPath':'source/app.py', 'sourceSha256':sha256(before['app.py']).hexdigest(), 'line':line}
    return indirect_frame_refs(before, after, [frame])


def test_import_only_fix_can_reach_the_same_measured_expression():
    before = {'app.py': b'# missing import\nhasher = library.Hash()\n'}
    after = {'app.py': b'import library\nhasher = library.Hash()\n'}
    assert compare(before, after) == {'source/app.py::@module'}
    assert not compare(before, {'app.py': before['app.py'] + b'import library\n'})


@pytest.mark.parametrize('declaration,operation', [
    ('from lib import make_digest', 'make_digest(value)'),
    ('import lib as provider', 'provider.make_digest(value)'),
])
def test_a_change_inside_the_used_helper_is_only_indirect_evidence(declaration, operation):
    source = f'{declaration}\ndef signup(value):\n    return {operation}\n'.encode()
    lib = b'def make_digest(value): return old(value)\ndef unused(value): return old(value)\n'
    before = {'app.py': source, 'lib.py': lib}
    assert compare(before, {**before, 'lib.py': lib.replace(b'old', b'new', 1)}, 3) == {'source/lib.py::make_digest'}
    assert not compare(before, {**before, 'lib.py': lib.replace(b'unused(value): return old', b'unused(value): return new')}, 3)


def test_unrelated_change_in_same_route_or_docstring_does_not_substantiate_priority():
    code = b'def make():\n    """before"""\n    return 7\ndef signup():\n    digest = make()\n    return store(digest)\n'
    before = {'app.py': code}
    assert not compare(before, {'app.py': code.replace(b'return store', b'return other_store')}, 5)
    assert not compare(before, {'app.py': code.replace(b'before', b'after')}, 5)
    assert compare(before, {'app.py': code.replace(b'return 7', b'return 8')}, 5) == {'source/app.py::make'}


def test_globals_shadowed_by_a_parameter_and_stale_frames_are_never_evidence():
    code = b'import binder\ndef signup(binder):\n    return binder.encode()\n'
    before = {'app.py':code}
    after = {'app.py': code.replace(b'import binder', b'import other as binder')}
    assert not compare(before, after, 3)
    assert not indirect_frame_refs(before, after, [{'toolPath':'source/app.py', 'line':3, 'sourceSha256':'f' * 64}])


def test_enclosing_parameter_shadows_a_module_import():
    code = b'from lib import make\ndef outer(make):\n    def signup(value):\n        return make(value)\n    return signup\n'
    before = {'app.py':code, 'lib.py':b'def make(value): return old(value)\n'}
    assert not compare(before, {**before, 'lib.py':b'def make(value): return new(value)\n'}, 4)


def test_import_uses_the_package_before_a_same_named_module():
    code = b'from lib import make\ndef signup(value):\n    return make(value)\n'
    helper = b'def make(value): return old(value)\n'
    before = {'app.py':code, 'lib.py':helper, 'lib/__init__.py':helper}
    new = b'def make(value): return new(value)\n'
    assert not compare(before, {**before, 'lib.py':new}, 3)
    assert compare(before, {**before, 'lib/__init__.py':new}, 3) == {'source/lib/__init__.py::make'}
