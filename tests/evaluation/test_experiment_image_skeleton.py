"""공식 시작 코드로 실험 이미지에서 회원가입 시험이 실제로 통과 가능한지(2026-11-15 최종 점검).

EVAL_DOCKER_IMAGE에 실험용 공통 이미지(Host 샌드박스 = D5 채점, evaluation.environment로 준비)를 주면 돈다.
이미지 안의 lock을 그대로 시작 코드에 넣어 Manifest가 맞는 조건에서 채점한다. 이미지를 받거나 만들지 않는다.
"""
import hashlib
import os

import pytest

from evaluation.final_evaluation import DockerRunner
from tests.evaluation.test_final_evaluation import canonical_tar, docker_ready, final, manifest, statuses
from tests.evaluation.test_starting_skeleton import skeleton_files, with_minimal_signup

pytestmark = pytest.mark.skipif(not docker_ready(), reason='EVAL_DOCKER_IMAGE와 실행 중인 Docker가 있어야 실제 이미지로 채점한다')


@pytest.fixture(scope='module')
def experiment_image():
    runner = DockerRunner(os.environ['EVAL_DOCKER_IMAGE'])
    observed = runner.image_dependency_lock()
    assert observed['error'] is None
    return runner, observed


def _archive(files, runner):
    # 이미지에 실제로 든 lock 바이트를 읽어 시작 코드의 requirements.lock으로 쓴다(dev Host·D5가 대조하는 값).
    command = runner._base_command('eval-skeleton-lock-' + os.urandom(6).hex()) + [
        '--entrypoint', '/bin/cat', runner.image, runner.image_dependency_lock()['paths'][0]]
    import subprocess
    done = subprocess.run([runner.docker, *command], capture_output=True, timeout=60)
    assert done.returncode == 0
    lock = done.stdout
    data = {path: text.encode('utf-8') for path, text in files.items()}
    data['requirements.lock'] = lock
    return canonical_tar(data), lock


@pytest.mark.parametrize('exam', ['pilot', 'final'])
def test_official_skeleton_with_a_correct_signup_passes_every_check_in_the_experiment_image(experiment_image, exam):
    runner, observed = experiment_image
    archive, lock = _archive(with_minimal_signup(), runner)
    assert 'sha256:' + hashlib.sha256(lock).hexdigest() == observed['sha256']
    output = final(archive, manifest(archive, lock=lock, image=runner.image_digest()), runner, exam=exam)
    assert output['report']['manifestCheck']['metadataMatches'] is True
    assert set(statuses(output).values()) == {'PASS'} and len(statuses(output)) == 9
    assert output['report']['evaluationVerdict'] == 'PASS' and output['score']['score'] == pytest.approx(1.0)
    assert set(statuses(output, 'referenceReport').values()) == {'PASS'}  # 순환용 시험지 참고 채점도 같은 결과


def test_official_skeleton_alone_is_unverified_not_failed_in_the_experiment_image(experiment_image):
    runner, _observed = experiment_image
    archive, lock = _archive(skeleton_files(), runner)
    output = final(archive, manifest(archive, lock=lock, image=runner.image_digest()), runner)
    assert output['report']['manifestCheck']['metadataMatches'] is True
    assert set(statuses(output).values()) == {'ERROR'} and output['report']['evaluationVerdict'] == 'UNVERIFIED'
