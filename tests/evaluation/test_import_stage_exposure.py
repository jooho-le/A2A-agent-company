"""Protect the application import phase as well as requests and lifespan.

These locally executed services are authored synthetic fixtures. No model,
Docker connection, real credential, or user service is used.
"""
import pytest

from evaluation import protected_tests as pt
from tests.evaluation import signup_service
from tests.evaluation.test_protected_signup import RUNTIME, run_protected

pytestmark = pytest.mark.skipif(not RUNTIME, reason='signup runtime dependencies are required')

# An intentionally synthetic encoded-hash shape, not a usable user credential.
SAMPLE_HASH = '$argon2id$v=19$m=19456,t=2,p=1$MTIzNDU2Nzg5MGFiY2RlZg$MTIzNDU2Nzg5MGFiY2RlZjEyMzQ1Njc4OTBhYmNkZWY'


def with_import_log(monkeypatch, source):
    original = signup_service.build

    def build(variant):
        files = original(variant)
        files['main.py'] = source + '\n' + files['main.py']
        return files

    monkeypatch.setattr(signup_service, 'build', build)


@pytest.mark.parametrize('scope', pt.SETS)
@pytest.mark.parametrize('level', ['info', 'debug'])
def test_hash_logged_while_importing_the_app_fails_in_every_exam(monkeypatch, scope, level):
    with_import_log(monkeypatch,
                    f'import logging\nlogging.getLogger("module_startup").{level}({SAMPLE_HASH!r})')
    result = run_protected('good', scope)
    failed = [row['testId'] for row in result['report']['tests'] if row['outcome'] == 'FAIL']
    assert result['report']['passed'] == 8
    assert result['report']['skipped'] == 0
    assert len(failed) == 1 and failed[0].endswith('test_sec_signup_005_password_and_hash_not_exposed')
    assert SAMPLE_HASH not in str(result)


@pytest.mark.parametrize('source', [
    'import logging\nlogging.getLogger("module_startup").debug("service initialization")',
    'import logging\n'
    'class MaskHash(logging.Filter):\n'
    '    def filter(self, record):\n'
    '        record.msg, record.args = "[REDACTED]", ()\n'
    '        return True\n'
    'logger = logging.getLogger("module_startup")\n'
    'logger.addFilter(MaskHash())\n'
    f'logger.info({SAMPLE_HASH!r})',
])
def test_safe_import_logs_remain_pass(monkeypatch, source):
    with_import_log(monkeypatch, source)
    result = run_protected('good', 'final')
    assert result['report']['passed'] == 9
    assert result['report']['failed'] == 0
    assert result['report']['skipped'] == 0
