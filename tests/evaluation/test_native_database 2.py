"""Native DB integration: read-only observation, real-engine faults and cleanup.

Only authored synthetic services run locally. The production protected runner
still executes generated apps in its pinned, network-isolated container.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from evaluation import protected_tests as pt
from tests.evaluation import signup_service
from tests.evaluation.test_protected_signup import RUNTIME

pytestmark = pytest.mark.skipif(not RUNTIME, reason='signup runtime dependencies are required')


PREFIX = '''
import json, os, sys
sys.path[:0] = [sys.argv[1], sys.argv[2]]
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from signup_link import Service, Unverified, new_email, new_password
saved_env = os.environ.get('DATABASE_URL')
saved_out = sys.stdout
registered = []
original_listen = event.listen
def tracked_listen(target, name, callback, *args, **kwargs):
    original_listen(target, name, callback, *args, **kwargs)
    if target is Engine and name == 'before_cursor_execute' and callback.__module__ == 'signup_link':
        registered.append((target, name, callback))
event.listen = tracked_listen
def assert_clean():
    assert all(not event.contains(*item) for item in registered)
    assert os.environ.get('DATABASE_URL') == saved_env
    assert sys.stdout is saved_out
'''


def run_native(body, *, startup_failure=False):
    with tempfile.TemporaryDirectory(prefix='native-db-regression-') as directory:
        root = Path(directory)
        files = signup_service.build('good')
        if startup_failure:
            files['main.py'] = files['main.py'].replace('    Base.metadata.create_all(bind=engine)',
                                                       '    raise RuntimeError("startup fixture failure")')
        for name, content in files.items():
            path = root / 'snapshot' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        for name, content in pt.protected_files('final').items():
            path = root / 'inputs' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding='utf-8')
        result = subprocess.run([sys.executable, '-I', '-B', '-c', PREFIX + body,
                                 str(root / 'snapshot'), str(root / 'inputs/tests/signup')],
                                capture_output=True, text=True, timeout=30, cwd=root)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)


def test_observer_is_read_only_and_fault_reaches_only_actual_app_database():
    result = run_native('''
service = Service().open()
try:
    assert service.count() == 0
    with service._engine.connect() as observer:
        try:
            observer.exec_driver_sql("CREATE TABLE observer_must_not_write (id INTEGER)")
        except OperationalError:
            pass
        else:
            raise AssertionError('observer wrote into the application DB')
    service.fail_next_insert()
    other = create_engine('sqlite:///:memory:')
    try:
        with other.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE unrelated (id INTEGER)')
            connection.exec_driver_sql('INSERT INTO unrelated VALUES (1)')
    finally:
        other.dispose()
    assert service.signup(new_email(), new_password()) == 503
    service.require_insert_failure()
    assert service.count() == 0
    assert service.signup(new_email(), new_password()) == 201
    assert service.count() == 1
finally:
    service.close()
assert_clean()
print(json.dumps({'readOnly': True, 'nativeFault': True, 'unrelatedUnaffected': True, 'recovered': True}))
''')
    assert all(result.values())


def test_unapplied_fault_is_unverified_and_pending_fault_cleanup_is_complete():
    assert run_native('''
service = Service().open()
try:
    service.fail_next_insert()
    try:
        service.require_insert_failure()
    except Unverified as error:
        assert error.code == 'DATABASE_ERROR_NOT_INJECTED'
    else:
        raise AssertionError('unapplied fault was accepted')
finally:
    service.close()
assert_clean()
next_service = Service().open()
try:
    assert next_service.signup(new_email(), new_password()) == 201
finally:
    next_service.close()
assert_clean()
print(json.dumps({'clean': True}))
''') == {'clean': True}


def test_failed_startup_removes_observation_listeners_and_restores_process_state():
    assert run_native('''
try:
    Service().open()
except Unverified as error:
    assert error.code == 'SERVICE_SETUP_FAILED'
else:
    raise AssertionError('failed startup was accepted')
assert_clean()
print(json.dumps({'clean': True}))
''', startup_failure=True) == {'clean': True}
