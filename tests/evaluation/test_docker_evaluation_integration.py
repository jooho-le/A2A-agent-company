"""Opt-in real Docker checks; only trusted synthetic signup code is executed.

Set EVAL_DOCKER_IMAGE to a locally prepared immutable image. These checks never
build/pull images, call a model, or use the application's shared database.
"""
from dataclasses import replace
import hashlib
import os
from uuid import uuid4

import pytest

from evaluation.final_evaluation import DockerRunner, read_snapshot
from evaluation.replay import confirm_regression, repeat_evaluation
from tests.evaluation.test_final_evaluation import (
    CONTRACT, canonical_tar, docker_ready, final, manifest, snapshot, statuses,
)


pytestmark = pytest.mark.skipif(
    not docker_ready(), reason='Requires EVAL_DOCKER_IMAGE and running local Docker',
)


@pytest.fixture(scope='module')
def real_environment():
    runner = DockerRunner(os.environ['EVAL_DOCKER_IMAGE'])
    observed = runner.image_dependency_lock()
    assert observed['status'] == 'OBSERVED', observed
    command = runner._base_command('eval-integration-lock-' + uuid4().hex[:12]) + [
        '--entrypoint', runner.python, runner.image, '-I', '-B', '-c',
        'from pathlib import Path; import sys; sys.stdout.write(Path(sys.argv[1]).read_text())',
        observed['paths'][0],
    ]
    done = runner._call(command, 60)
    assert done.returncode == 0, done.stderr
    lock = done.stdout.encode('utf-8')
    assert 'sha256:' + hashlib.sha256(lock).hexdigest() == observed['sha256']
    return runner, lock


@pytest.fixture(scope='module')
def recorded_real_run(real_environment, tmp_path_factory):
    runner, lock = real_environment
    archive = snapshot(lock=lock)
    expected = manifest(archive, lock=lock, image=runner.image_digest())
    private = tmp_path_factory.mktemp('real-docker-private-inputs')
    previous_umask = os.umask(0o077)
    try:
        output = final(archive, expected, runner, private_input_dir=private)
    finally:
        os.umask(previous_umask)
    assert output['report']['evaluationVerdict'] == 'PASS', statuses(output)
    assert output['snapshot']['imageDependencyLock']['status'] == 'OBSERVED'
    assert output['inputRecord']['status'] == 'RECORDED'
    return output, private / output['inputRecord']['fileName'], archive, expected


@pytest.mark.parametrize('variant,verdict,failed_check', [
    ('plaintext_storage', 'FAIL', 'SEC-SIGNUP-002'),
    ('weak_argon2_memory', 'FAIL', 'SEC-SIGNUP-002'),
    # Without the database, other checks cannot establish their preconditions;
    # preserve their unknowns as well as the confirmed signup failure.
    ('missing_bootstrap', 'UNVERIFIED', 'QA-SIGNUP-001'),
])
def test_real_docker_rejects_defective_signup(real_environment, variant, verdict, failed_check):
    runner, lock = real_environment
    archive = snapshot(variant, lock=lock)
    output = final(archive, manifest(archive, lock=lock, image=runner.image_digest()), runner)
    assert output['runner']['error'] is None, output['runner']
    assert output['report']['evaluationVerdict'] == verdict, statuses(output)
    assert statuses(output)[failed_check] == 'FAIL'


def test_real_docker_pilot_suite(real_environment):
    runner, lock = real_environment
    archive = snapshot(lock=lock)
    output = final(archive, manifest(archive, lock=lock, image=runner.image_digest()), runner, exam='pilot')
    assert output['report']['evaluationVerdict'] == 'PASS', statuses(output)
    assert len(output['report']['checks']) == 9


def test_real_docker_image_lock_mismatch_blocks_exam(real_environment):
    runner, lock = real_environment
    different_lock = lock + b'\n# different approved source lock\n'
    archive = snapshot(lock=different_lock)
    output = final(archive, manifest(archive, lock=different_lock, image=runner.image_digest()), runner)
    assert output['snapshot']['error'] == 'IMAGE_DEPENDENCY_LOCK_MISMATCH'
    assert output['runner']['error'] == 'NOT_RUN'
    assert set(statuses(output).values()) == {'NOT_RUN'}
    assert output['report']['evaluationVerdict'] == 'UNVERIFIED'


def test_real_docker_replays_private_inputs_under_restrictive_umask(real_environment, recorded_real_run):
    runner, _lock = real_environment
    baseline, path, archive, expected = recorded_real_run
    before = path.read_bytes()
    previous_umask = os.umask(0o077)
    try:
        repeated = repeat_evaluation(baseline=baseline, input_path=path, archive=archive,
                                     expected=expected, scenario_contract=CONTRACT, runner=runner)
    finally:
        os.umask(previous_umask)
    assert repeated['result']['status'] == 'PASS', repeated['result']
    assert path.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_real_docker_confirms_regression_with_two_replays_per_version(real_environment, recorded_real_run):
    runner, lock = real_environment
    baseline, path, archive, expected = recorded_real_run
    candidate = snapshot('plaintext_storage', lock=lock)
    candidate_expected = replace(manifest(candidate, lock=lock, image=runner.image_digest()),
                                 code_version=expected.code_version + 1)
    result = confirm_regression(baseline=baseline, input_path=path, baseline_archive=archive,
                                candidate_archive=candidate, candidate_expected=candidate_expected,
                                scenario_contract=CONTRACT, runner=runner)
    confirmed = [check for check in result['result']['checks'] if check['status'] == 'CONFIRMED']
    assert confirmed, result['result']
    assert all(check['observed'] == {'previous': ['PASS', 'PASS'], 'candidate': ['FAIL', 'FAIL']}
               for check in confirmed)
    assert sum(len(runs) for runs in result['outputs'].values()) == 4


def test_real_docker_enforces_execution_restrictions(real_environment):
    runner, _lock = real_environment
    code = '''
import os, socket
from pathlib import Path
assert os.getuid() == 10001 and os.getgid() == 10001
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
assert int(status['CapEff'].strip(), 16) == 0
assert status['NoNewPrivs'].strip() == '1'
# Linux may list inactive tunnel devices even in an isolated namespace.
active = {name for _index, name in socket.if_nameindex()
          if int(Path('/sys/class/net', name, 'flags').read_text().strip(), 16) & 1}
assert active == {'lo'}
assert len(Path('/proc/net/route').read_text().splitlines()) == 1
root_mount = next(line.split() for line in Path('/proc/mounts').read_text().splitlines()
                  if line.split()[1] == '/')
assert 'ro' in root_mount[3].split(',')
try:
    Path('/root-write-probe').write_text('must not succeed')
except OSError:
    pass
else:
    raise AssertionError('root filesystem writable')
Path('/work/allowed-write-probe').write_text('ok')
print('ISOLATION_OK')
'''
    command = runner._base_command('eval-isolation-' + uuid4().hex[:12]) + [
        '--entrypoint', runner.python, runner.image, '-I', '-B', '-c', code,
    ]
    done = runner._call(command, 60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == 'ISOLATION_OK'


def test_real_docker_timeout_removes_its_containers(real_environment, monkeypatch):
    environment, lock = real_environment
    runner = DockerRunner(environment.image)
    names = []
    base_command = runner._base_command
    execute = runner.run

    def short_execution(*args, **kwargs):
        # This test targets a running exam container, not Docker's image-lock
        # preparation. Keep the real metadata probe's ordinary deadline so
        # a busy daemon cannot stop the test before that container is created.
        previous = runner.timeout
        runner.timeout = 3
        try:
            return execute(*args, **kwargs)
        finally:
            runner.timeout = previous

    def recording_command(name):
        names.append(name)
        return base_command(name)

    monkeypatch.setattr(runner, '_base_command', recording_command)
    monkeypatch.setattr(runner, 'run', short_execution)
    files = read_snapshot(snapshot(lock=lock))
    files['main.py'] = b'import time\ntime.sleep(20)\n' + files['main.py']
    archive = canonical_tar(files)
    output = final(archive, manifest(archive, lock=lock, image=runner.image_digest()), runner)
    assert output['runner']['error'] == 'TIMEOUT', output['runner']
    assert output['report']['evaluationVerdict'] == 'UNVERIFIED'
    assert any(name.startswith('eval-protected-') for name in names)
    # Inspect only the unique names made by this test; other projects are untouched.
    for name in names:
        inspected = runner._call(['container', 'inspect', name], 20)
        assert inspected.returncode != 0
        assert 'No such' in inspected.stderr
