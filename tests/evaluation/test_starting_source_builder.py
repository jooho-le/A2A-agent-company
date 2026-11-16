"""공통 시작 저장소 만들기(evaluation.starting_source)가 dev Host가 받는 저장소·설정을 만드는지 시험한다.

확인하는 것:
  - 저장소에는 연결 규격의 시작 파일만 들어가고, 골격의 팀용 문서(README 등)는 빠진다.
  - 만든 시작 코드 설정을 dev 설정 검사와 dev 시작 코드 검증(verify_starting_source)이 받는다.
  - 같은 골격·lock이면 커밋 해시가 같다(두 구조·여러 Run이 같은 커밋에서 출발).
  - lock 없음·빈 폴더 아님·시험지 섞임·시작 파일 목록 없음은 만들지 않는다.
dev 코드는 읽기만 한다. 생성 코드를 실행하지 않는다.
"""
import copy
import json
import shutil
from pathlib import Path

import pytest

from evaluation import protected_tests as pt
from evaluation.starting_source import LOCK_NAME, StartingSourceError, build, main, starting_file_names
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario

SKELETON = Path(__file__).resolve().parents[2] / 'examples/starting-source/scn001-signup'
LOCK = b'fastapi==0.0.0 --hash=sha256:' + b'0' * 64 + b'\n'


def _contract():
    return copy.deepcopy(get_scenario(SCN_001_ID).planner_contract())


@pytest.fixture
def lock(tmp_path):
    path = tmp_path / 'image' / LOCK_NAME
    path.parent.mkdir()
    path.write_bytes(LOCK)
    return path


def test_repository_holds_only_the_contract_starting_files(tmp_path, lock):
    configuration, lock_hash = build(SKELETON, lock, tmp_path / 'start')
    names = starting_file_names(_contract())
    tracked = sorted(path.relative_to(tmp_path / 'start').as_posix()
                     for path in (tmp_path / 'start').rglob('*') if path.is_file() and '.git' not in path.parts)
    assert tracked == sorted(names) and 'README.md' not in tracked
    assert (tmp_path / 'start' / LOCK_NAME).read_bytes() == LOCK
    assert lock_hash.startswith('sha256:') and configuration['lockPath'] == LOCK_NAME
    assert configuration['repositoryId'] == 'scn-001-start'


def test_dev_host_accepts_the_built_source(tmp_path, lock):
    from agents.platform.starting_source import decode_source_configuration, verify_starting_source
    from orchestrator.domain.run_configuration import ExecutionBaseline
    configuration, lock_hash = build(SKELETON, lock, tmp_path / 'start')
    decoded = decode_source_configuration(json.dumps(configuration))
    baseline = ExecutionBaseline(containerImageDigest='sha256:' + 'a' * 64, dependencyLockHash=lock_hash,
                                 hardwareProfile='fixture', networkPolicy='DENY')
    verify_starting_source(decoded, base_directory=tmp_path, environment=baseline)
    other = ExecutionBaseline(containerImageDigest='sha256:' + 'a' * 64, dependencyLockHash='sha256:' + 'b' * 64,
                              hardwareProfile='fixture', networkPolicy='DENY')
    with pytest.raises(Exception):
        verify_starting_source(decoded, base_directory=tmp_path, environment=other)  # lock이 다른 이미지는 거부


def test_same_inputs_give_the_same_commit(tmp_path, lock):
    first, _ = build(SKELETON, lock, tmp_path / 'a')
    second, _ = build(SKELETON, lock, tmp_path / 'b')
    assert first['commitHash'] == second['commitHash'] and first['snapshotSha256'] == second['snapshotSha256']


def test_refuses_missing_lock_and_non_empty_output(tmp_path, lock):
    with pytest.raises(StartingSourceError, match='LOCK_MISSING'):
        build(SKELETON, tmp_path / 'none.lock', tmp_path / 'x')
    (tmp_path / 'busy').mkdir()
    (tmp_path / 'busy' / 'keep.txt').write_text('x', encoding='utf-8')
    with pytest.raises(StartingSourceError, match='OUTPUT_NOT_EMPTY'):
        build(SKELETON, lock, tmp_path / 'busy')


def test_refuses_a_skeleton_that_contains_exam_files(tmp_path, lock):
    skeleton = tmp_path / 'skeleton'
    shutil.copytree(SKELETON, skeleton)
    exam = next(iter(pt.protected_files('final').values()))
    (skeleton / 'models.py').write_text(exam, encoding='utf-8')
    with pytest.raises(StartingSourceError, match='STARTING_SOURCE_CONTAINS_EVALUATION_FILES'):
        build(skeleton, lock, tmp_path / 'start')
    assert not (tmp_path / 'start').exists()


def test_refuses_a_contract_without_starting_files(tmp_path, lock):
    contract = _contract()
    del contract['externalContract']['startingFiles']
    with pytest.raises(StartingSourceError, match='STARTING_FILES_UNDEFINED'):
        build(SKELETON, lock, tmp_path / 'start', contract=contract)


def test_cli_writes_the_source_configuration(tmp_path, lock, capsys):
    config = tmp_path / 'configs' / 'owned-source.local.json'
    assert main(['build', '--skeleton', str(SKELETON), '--lock', str(lock), '--out', str(tmp_path / 'start'),
                 '--config', str(config)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert json.loads(config.read_text(encoding='utf-8')) == printed['source']
    assert main(['build', '--skeleton', str(SKELETON), '--lock', str(lock), '--out', str(tmp_path / 'again'),
                 '--config', str(config)]) == 1  # 이미 있는 설정은 --force 없이 덮어쓰지 않는다
