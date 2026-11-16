"""File-only preparation checks must not contact Docker, even with a valid image."""
import json
import subprocess

from evaluation import preflight
from tests.evaluation.test_preflight import plan


def forbid_docker(monkeypatch):
    original = subprocess.run

    def guarded(arguments, *args, **kwargs):
        assert arguments[0] != 'docker', 'Offline check must not invoke Docker'
        return original(arguments, *args, **kwargs)

    monkeypatch.setattr(preflight.subprocess, 'run', guarded)
    monkeypatch.setattr(preflight.DockerRunner, 'image_digest',
                        lambda self: (_ for _ in ()).throw(AssertionError('Image lookup must be skipped')))


def test_offline_checks_files_but_cannot_claim_environment_ready(tmp_path, monkeypatch):
    data = plan(tmp_path)
    forbid_docker(monkeypatch)
    result = preflight.check_preparation(data, connect_docker=False)
    assert result['ready'] is False and result['dockerChecksEnabled'] is False
    unknown = {r['check']: r['reason'] for r in result['checks'] if r['status'] != 'PASS'}
    assert unknown == {k: 'DOCKER_CHECK_DISABLED' for k in ('docker', 'imageAvailable', 'evaluationPackages',
                                                           'hostBrowser.SINGLE_AGENT', 'hostBrowser.MULTI_AGENT')}


def test_offline_cli_never_probes_docker(tmp_path, monkeypatch):
    data = plan(tmp_path)
    path, output = tmp_path / 'plan.json', tmp_path / 'result.json'
    path.write_text(json.dumps(data))
    forbid_docker(monkeypatch)
    assert preflight.main(['--plan', str(path), '--out', str(output), '--no-docker']) == 2
    assert json.loads(output.read_text())['dockerChecksEnabled'] is False
