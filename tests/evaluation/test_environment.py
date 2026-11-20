"""실험 환경 준비 도구(evaluation.environment)의 순수 부분. Docker·네트워크를 쓰지 않는다."""
import hashlib
import json
from pathlib import Path

import pytest

from evaluation import environment as env
from evaluation.experiment import configure as experiment_configure
from evaluation.experiment_run import _digest
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario

ROOT = Path(__file__).resolve().parents[2]
IMAGE = 'sha256:' + 'a' * 64


def _wheel(folder, name, version, tail='py3-none-any'):
    path = folder / f'{name}-{version}-{tail}.whl'
    path.write_bytes(f'{name}-{version}'.encode())
    return path


def _requirement_wheels(folder):
    for requirement in env.REQUIREMENTS:
        name, version = requirement.split('==')
        _wheel(folder, name.replace('-', '_'), version)


def test_lock_pins_every_downloaded_wheel_with_its_hash(tmp_path):
    _requirement_wheels(tmp_path)
    extra = _wheel(tmp_path, 'greenlet', '3.5.6', 'cp313-cp313-manylinux_2_28_aarch64')
    lock = env.lock_from_wheelhouse(tmp_path)
    lines = [line for line in lock.splitlines() if not line.startswith('#')]
    assert len(lines) == len(env.REQUIREMENTS) + 1 and lines == sorted(lines)
    assert f'greenlet==3.5.6 --hash=sha256:{hashlib.sha256(extra.read_bytes()).hexdigest()}' in lines
    assert any(line.startswith('argon2-cffi==25.1.0 --hash=sha256:') for line in lines)  # 이름 정규화(_ → -)


def test_lock_refuses_ambiguous_or_incomplete_wheelhouses(tmp_path):
    _requirement_wheels(tmp_path)
    _wheel(tmp_path, 'fastapi', '0.1.0')
    with pytest.raises(env.EnvironmentError_, match='DUPLICATE'):
        env.lock_from_wheelhouse(tmp_path)
    other = tmp_path / 'other'
    other.mkdir()
    _wheel(other, 'fastapi', '0.142.2')
    with pytest.raises(env.EnvironmentError_, match='WHEELHOUSE_MISSING'):
        env.lock_from_wheelhouse(other)


def test_apt_packages_come_only_from_the_install_command():
    text = ('BEGIN\nsu root /bin/bash -c "apt-get update && apt-get install -y --no-install-recommends '
            'libnss3 libnspr4 libatk1.0-0 libgbm1 libasound2t64 libnss3"\nEND\n')
    assert env.apt_packages(text) == ['libnss3', 'libnspr4', 'libatk1.0-0', 'libgbm1', 'libasound2t64']
    with pytest.raises(env.EnvironmentError_):
        env.apt_packages('nothing to install')
    with pytest.raises(env.EnvironmentError_):
        env.apt_packages('apt-get install -y libnss3;rm')


def test_tools_document_is_accepted_by_dev_and_yields_ver1_ver2(tmp_path):
    from agents.platform.tool_configuration import load_tool_configuration
    lock = tmp_path / 'requirements.lock'
    lock.write_text('fastapi==0.142.2 --hash=sha256:' + 'b' * 64 + '\n', encoding='utf-8')
    lock_hash = 'sha256:' + hashlib.sha256(lock.read_bytes()).hexdigest()
    example = json.loads((ROOT / 'configs/owned-tools.example.json').read_text(encoding='utf-8'))
    tools = env.tools_document(image_id=IMAGE, lock_file='requirements.lock', lock_hash=lock_hash,
                               hardware_profile='fixture-host/docker-fixture', unit_scopes=example['unit']['scopes'])
    path = tmp_path / 'owned-tools.local.json'
    path.write_text(json.dumps(tools), encoding='utf-8')
    configuration = load_tool_configuration(path)
    assert configuration.max_call_seconds == env.MAX_CALL_SECONDS
    assert configuration.browser_configuration is None  # SCN-001 has no UI acceptance criteria
    documents = experiment_configure(tools, contract=get_scenario(SCN_001_ID).planner_contract(), base=tmp_path)
    for version in ('ver1', 'ver2'):
        written = tmp_path / version / 'owned-tools.local.json'
        written.parent.mkdir()
        written.write_text(json.dumps(documents[f'{version}/owned-tools.local.json']), encoding='utf-8')
        loaded = load_tool_configuration(written)  # Ver1은 보호 범위 두 개가 들어가도 dev 한도 안이다
        protected = [scope for scope in documents[f'{version}/owned-tools.local.json']['unit']['scopes']
                     if scope['kind'] == 'PROTECTED']
        assert len(protected) == (2 if version == 'ver1' else 0)
        assert loaded.baseline.container_image_digest == IMAGE
        assert loaded.browser_configuration is None


def test_plan_environment_fields_match_what_preflight_and_submission_compare():
    plan = json.loads((ROOT / 'examples/evaluation/scn001-pilot.ver1.plan.json').read_text(encoding='utf-8'))
    updated = env.update_plan(plan, image_id=IMAGE, hardware='fixture-host', commit_hash='c' * 40,
                              repository_path='../../.data/starting-source/scn001')
    assert _digest(updated['image']) == IMAGE and updated['hardwareProfile'] == 'fixture-host'
    assert updated['source'] == {'repositoryPath': '../../.data/starting-source/scn001', 'commitHash': 'c' * 40,
                                 'lockPath': 'requirements.lock'}
    assert {key: updated[key] for key in ('model', 'limits', 'plannedRuns')} == \
        {key: plan[key] for key in ('model', 'limits', 'plannedRuns')}


@pytest.mark.parametrize('command', ['wheels', 'browsers', 'image', 'all'])
def test_network_steps_never_run_without_explicit_permission(command, monkeypatch, capsys):
    monkeypatch.setattr(env.subprocess, 'run', lambda *args, **kwargs: pytest.fail('no command may run'))
    assert env.main([command]) == 1
    assert 'NETWORK_STEP_REQUIRES_--allow-network' in capsys.readouterr().err
