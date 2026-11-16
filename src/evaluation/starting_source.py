"""공통 시작 코드 저장소 만들기 (D0 생성물 규격의 시작 Repository·Commit).

골격(examples/starting-source/<시나리오>)과 실행 이미지의 requirements.lock으로 Single·Multi가 함께 쓰는 시작 저장소를 만든다.

    python -m evaluation.starting_source build --skeleton examples/starting-source/scn001-signup \\
        --lock <실행 이미지 requirements.lock> --out <새 저장소 폴더> [--config configs/owned-source.local.json] [--force]

- 저장소에는 dev 시나리오 계약의 연결 규격(externalContract.startingFiles)에 적힌 파일만 넣는다. 골격의 README 같은
  팀용 문서는 넣지 않는다. Host가 시작 커밋의 파일 트리 전체를 에이전트 작업 폴더로 복사하기 때문이다.
- 시험지·평가 코드·결과 파일이 섞였으면 만들지 않는다(protected_tests.exam_exposure).
- 커밋은 고정한 작성자·시각으로 만든다. 같은 골격·lock이면 같은 커밋 해시가 나온다(개발정의서 11-A: 시작 Source 동일 Commit).
- dev Host와 같은 방법(GitSnapshotBuilder)으로 커밋을 다시 꺼내 파일이 같은지 확인하고, Snapshot 해시를 담은
  Host 시작 코드 설정(owned-source, schemaVersion 1)과 도구 설정에 적을 lock 해시(dependencyLockHash)를 돌려준다.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import protected_tests

LOCK_NAME = 'requirements.lock'
# 개인 정보가 들어가지 않는 고정 작성자·시각. 커밋 해시가 입력 파일에만 달려 있게 한다.
AUTHOR_NAME, AUTHOR_EMAIL = 'Evaluation Starting Source', 'starting-source@example.invalid'
COMMIT_DATE = '2026-11-14T00:00:00+09:00'
COMMIT_MESSAGE = 'Common starting source'


class StartingSourceError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _registry_contract() -> dict:
    from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario
    return get_scenario(SCN_001_ID).planner_contract()


def starting_file_names(contract: Mapping) -> list:
    # 연결 규격에 적힌 시작 파일 목록. lock이 빠져 있거나 목록이 없으면 시작 저장소를 정할 수 없다.
    names = ((contract or {}).get('externalContract') or {}).get('startingFiles')
    if (not isinstance(names, list) or not names or LOCK_NAME not in names
            or any(not isinstance(name, str) or not name or name.startswith('/') or '..' in Path(name).parts
                   for name in names) or len(set(names)) != len(names)):
        raise StartingSourceError('STARTING_FILES_UNDEFINED')
    return names


def _git(repository: Path, *arguments: str, timeout: float = 30) -> str:
    # 사용자 전역 설정·훅·서명을 쓰지 않는 git 호출
    environment = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LANG': 'C', 'LC_ALL': 'C',
                   'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
                   'GIT_AUTHOR_NAME': AUTHOR_NAME, 'GIT_AUTHOR_EMAIL': AUTHOR_EMAIL, 'GIT_AUTHOR_DATE': COMMIT_DATE,
                   'GIT_COMMITTER_NAME': AUTHOR_NAME, 'GIT_COMMITTER_EMAIL': AUTHOR_EMAIL,
                   'GIT_COMMITTER_DATE': COMMIT_DATE}
    try:
        result = subprocess.run(['git', '-c', 'core.hooksPath=' + os.devnull, '-c', 'commit.gpgsign=false',
                                 '-c', 'init.defaultBranch=main', *arguments], cwd=repository, env=environment,
                                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise StartingSourceError('GIT_FAILED') from None
    return result.stdout.strip()


def build(skeleton: Path, lock: Path, out: Path, *, contract: Optional[Mapping] = None,
          repository_id: Optional[str] = None, git_timeout_seconds: float = 30) -> tuple[dict, str]:
    # 시작 저장소를 out에 만들고 (Host 시작 코드 설정, lock 해시 "sha256:...")를 돌려준다.
    contract = contract if contract is not None else _registry_contract()
    names = starting_file_names(contract)
    skeleton, lock, out = Path(skeleton), Path(lock), Path(out)
    if not lock.is_file() or lock.is_symlink() or lock.stat().st_size == 0:
        raise StartingSourceError('LOCK_MISSING')
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise StartingSourceError('OUTPUT_NOT_EMPTY')
    files = {}
    for name in names:
        source = lock if name == LOCK_NAME else skeleton / name
        if not source.is_file() or source.is_symlink():
            raise StartingSourceError('SKELETON_FILE_MISSING')
        files[name] = source.read_bytes()
    if protected_tests.exam_exposure(files):
        raise StartingSourceError('STARTING_SOURCE_CONTAINS_EVALUATION_FILES')
    out.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        target = out / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    _git(out, 'init', '-q', '--template=', timeout=git_timeout_seconds)
    _git(out, 'add', '--', *names, timeout=git_timeout_seconds)
    _git(out, 'commit', '-q', '-m', COMMIT_MESSAGE, timeout=git_timeout_seconds)
    commit = _git(out, 'rev-parse', 'HEAD', timeout=git_timeout_seconds)
    from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder, GitSnapshotLimits
    from .final_evaluation import read_snapshot
    snapshot = GitSnapshotBuilder(out.resolve(), GitSnapshotLimits(timeout_seconds=float(git_timeout_seconds))
                                  ).build(commit, LOCK_NAME)
    if read_snapshot(snapshot.archive) != files:
        raise StartingSourceError('SNAPSHOT_MISMATCH')
    configuration = {
        'schemaVersion': 1, 'repositoryPath': str(out.resolve()),
        'repositoryId': repository_id or str(contract.get('scenarioKey', 'scenario')).lower() + '-start',
        'commitHash': commit, 'snapshotSha256': snapshot.snapshot_sha256, 'lockPath': LOCK_NAME,
        'gitTimeoutSeconds': git_timeout_seconds,
    }
    return configuration, 'sha256:' + hashlib.sha256(files[LOCK_NAME]).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m evaluation.starting_source',
                                     description='공통 시작 코드 저장소와 Host 시작 코드 설정 만들기')
    commands = parser.add_subparsers(dest='command', required=True)
    item = commands.add_parser('build', help='골격 + 실행 이미지 lock으로 시작 저장소 만들기')
    item.add_argument('--skeleton', required=True, type=Path)
    item.add_argument('--lock', required=True, type=Path, help='실행 이미지·평가 이미지와 같은 requirements.lock')
    item.add_argument('--out', required=True, type=Path, help='새 저장소 폴더(비어 있어야 함)')
    item.add_argument('--config', type=Path, help='시작 코드 설정을 쓸 경로(owned-source.local.json)')
    item.add_argument('--repository-id')
    item.add_argument('--force', action='store_true', help='--config 파일이 있으면 덮어쓰기')
    args = parser.parse_args(argv)
    try:
        configuration, lock_hash = build(args.skeleton, args.lock, args.out, repository_id=args.repository_id)
        if args.config is not None:
            if args.config.exists() and not args.force:
                raise StartingSourceError('CONFIG_EXISTS')
            args.config.parent.mkdir(parents=True, exist_ok=True)
            args.config.write_text(json.dumps(configuration, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except StartingSourceError as error:
        sys.stderr.write(error.code + '\n')
        return 1
    print(json.dumps({'source': configuration, 'dependencyLockHash': lock_hash}, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
