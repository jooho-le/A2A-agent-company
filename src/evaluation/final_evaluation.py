# D5 최종 독립 채점기. 개발 실행(Run)이 끝난 뒤 최종 코드 Snapshot을 받아 같은 보호 테스트로 채점하고
# 독립 평가 보고서(INDEPENDENT_EVALUATION)를 만든다.
#
# 순서
#   1) Snapshot 확인: 받은 압축 파일(dev canonical tar)의 SHA-256이 기준 Manifest의 snapshotSha256과 같은지,
#      기준 Manifest의 dependencyLockHash와 같은 파일이 안에 있는지 확인한다.
#   2) 안전하게 풀기: 일반 파일만, 상대 경로만(../·절대 경로·링크·장치 파일 거부), 크기 한도 안에서 푼다.
#   3) 보호 테스트(최종 범위 8개): dev의 신뢰된 unittest 실행기(unit_runner.py)로 실행한다.
#      실제 생성 코드는 반드시 컨테이너(DockerRunner)에서 실행한다. LocalRunner는 합성 표본 시험용이다.
#   4) 정적 판정(SEC-SIGNUP-001·003·004): 같은 소스로 security.assess.evaluate()를 부른다(생성 코드를 실행하지 않음).
#   5) 보고서: 결과 어댑터로 검사 결과를 만들고 build_evaluation_report로 묶는다. 실제로 확인한 Snapshot·lock·이미지가
#      기준과 다르면 Manifest 불일치로 결과를 쓰지 않는다(UNVERIFIED). 이미지를 확인할 수 없는 실행(로컬)도 결과를 쓰지 않는다.
#   6) (선택) Run 기록이 있으면 trace_checks로 노출(REQ-006 Orchestrator 부분)과 추적(REQ-008)을 함께 본다.
#
# 이 모듈이 하지 않는 일: Snapshot을 시스템에서 받아 오는 일(인계 방법은 팀장 결정 대기), 증거 저장, A2A 전송.
#
# [처음 읽는 분을 위한 용어]
# - Snapshot: 개발 실행이 고정(동결)한 코드 묶음. dev는 이것을 압축하지 않은 tar 파일 하나로 만들고, 그 파일 전체의
#   SHA-256을 snapshotSha256으로 기록한다. 같은 해시면 같은 코드다.
# - Manifest(신분증): "어떤 코드(Snapshot 해시)를 어떤 환경(이미지 해시·의존성 lock 해시)에서 검사했는가"를 적은 기록.
#   기준(expected)과 실제로 확인한 것(observed)이 같을 때만 결과를 믿는다(manifest.py).
# - 컨테이너 이미지 해시(digest): 실행 환경 이미지의 지문. "sha256:..." 모양이며 태그(:latest)와 달리 내용이 바뀌지 않는다.
# - 실행기(runner): 보호 테스트를 실제로 돌리는 부품. 실제 평가는 DockerRunner, 합성 표본 시험은 LocalRunner.
import hashlib   # SHA-256 지문 계산
import io        # 메모리 안의 바이트를 파일처럼 다루는 도구(압축 파일을 디스크에 쓰지 않고 읽기 위해)
import json      # JSON 읽기·쓰기
import os        # 폴더 권한 바꾸기
import re        # 이미지 참조 모양 검사
import shutil    # 지금은 쓰지 않는 import(정리 대상)
import subprocess  # 다른 프로그램(파이썬·docker)을 따로 실행하는 도구
import sys       # 지금 쓰는 파이썬 실행 파일 위치
import tarfile   # tar 압축 파일 읽기
import tempfile  # 쓰고 나면 지워지는 임시 폴더
import uuid      # 겹치지 않는 번호(UUID) 만들기
from dataclasses import dataclass, replace  # 데이터 상자, 상자의 일부 값만 바꾼 복사본 만들기
from datetime import datetime, timezone     # 보고서 시각
from pathlib import Path, PurePosixPath     # 파일 경로 다루기(PurePosixPath는 "/"로 나뉜 tar 안 경로용)
from typing import Mapping, Optional        # 자료형 이름표(실행에는 영향 없음)
from urllib.parse import quote              # 증거 주소에 넣을 경로 글자를 안전하게 바꾸기

from . import protected_tests                                  # 보호 테스트 묶음(D2)
from .check_adapters import _valid_report                      # unittest-v1 보고서 모양 검사
from .independent import SourceRunSummary, build_evaluation_report  # 독립 평가 보고서 만들기
from .manifest import ExecutionManifest                        # 코드·환경 신분증
from .security.assess import evaluate as evaluate_static       # Security 정적 판정(D1)
from .trace_checks import check_trace_exposure, check_traceability  # Run 기록 검사(D4)

CRITERIA_FILE = Path(__file__).resolve().parent / 'security' / 'scn001_security_criteria.json'  # Security 기준 파일
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024  # 받는 Snapshot tar의 최대 크기(64MiB)
MAX_ARCHIVE_FILES = 4096              # tar 안 파일 수의 최대값
MAX_FILE_BYTES = 8 * 1024 * 1024      # 파일 하나의 최대 크기(8MiB)
MAX_STATIC_BYTES = 4 * 1024 * 1024   # dev Security 승인 함수에 넘기는 구현 크기 한도와 같은 값
RUNNER_FILE = '_unit_runner.py'      # dev가 컨테이너에 읽기 전용으로 넣는 실행기 이름과 같다


class ArchiveError(ValueError):
    # Snapshot 압축 파일을 안전하게 풀 수 없다(형식·경로·크기·파일 종류 문제).
    pass


@dataclass(frozen=True)
class RunnerOutcome:
    # 보호 테스트 실행 결과.
    report: Optional[dict]            # unittest-v1 보고서. 실행기 오류면 None
    exit_code: Optional[int]          # 실행기의 종료 코드(0 통과, 1 실패 있음, 2 실행기 오류)
    error: Optional[str]              # 실행 자체의 오류 코드(TIMEOUT, DOCKER_UNAVAILABLE, IMAGE_DIGEST_MISMATCH 등)
    image_digest: Optional[str]       # 실제로 쓴 컨테이너 이미지 해시. 확인할 수 없는 실행(로컬)은 None
    kind: str                         # 'docker' 또는 'local'

    def to_dict(self):
        # 증거·결과에 남길 실행 정보(보고서 본문은 따로 담는다).
        return {'kind': self.kind, 'exitCode': self.exit_code, 'error': self.error, 'imageDigest': self.image_digest}


def _checked_outcome(outcome: RunnerOutcome) -> RunnerOutcome:
    # 실행 결과를 판정 근거로 써도 되는지 확인한다. 쓸 수 없으면 보고서를 버리고(report=None) 오류 코드를 남긴다.
    # JSON이 있어도 실행 오류·신호 종료·건수/종료 코드 불일치는 검증 근거가 아니다.
    #   종료 코드 규칙(dev 실행기): 0 = 모두 통과, 1 = 실패가 있음, 2 = 실행기 오류. 음수는 신호로 강제 종료된 경우다.
    if outcome.error is not None:
        return replace(outcome, report=None)
    if type(outcome.exit_code) is not int or outcome.exit_code not in (0, 1):
        return replace(outcome, report=None, error='RUNNER_EXIT_INVALID')
    try:
        valid = _valid_report(outcome.report)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        return replace(outcome, report=None, error='UNIT_REPORT_INVALID')
    expected_exit = 1 if outcome.report['failed'] else 0  # 보고서에 실패가 있으면 1, 없으면 0이어야 앞뒤가 맞는다
    if outcome.exit_code != expected_exit:
        return replace(outcome, report=None, error='RUNNER_EXIT_REPORT_MISMATCH')
    return outcome


def _parse_runner_output(stdout: str):
    # 실행기는 마지막 줄에 JSON 하나만 쓴다. 보고서면 (보고서, None), 오류면 (None, 오류 코드).
    lines = [line for line in stdout.splitlines() if line.strip()]  # 빈 줄을 뺀 출력 줄들
    try:
        payload = json.loads(lines[-1]) if lines else None
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get('format') == 'unittest-v1':
        return payload, None
    return None, 'TEST_RUNNER_ERROR'


class LocalRunner:
    # 합성 표본(우리가 만든 회원가입 서비스) 시험용 실행기. 같은 컴퓨터의 새 파이썬 프로세스에서 dev 실행기를 돌린다.
    # 실제 생성 코드를 호스트에서 실행하면 안 되므로 trusted=True를 명시해야만 쓸 수 있다.
    # 컨테이너 이미지를 확인할 수 없어서 image_digest는 None이고, 그래서 보고서는 결과를 쓰지 않는다(Manifest 없음).
    kind = 'local'

    def __init__(self, *, trusted: bool = False, python: str = sys.executable, timeout: float = 600):
        # trusted: 실행할 코드가 우리가 만든 합성 표본이라고 부르는 쪽이 명시하는 값. True가 아니면 만들지 않는다.
        # python : 테스트를 돌릴 파이썬(기본은 지금 쓰는 파이썬), timeout: 최대 실행 시간(초)
        if trusted is not True:
            raise ValueError('LocalRunner runs code on this host; use it only for trusted synthetic fixtures')
        self.python, self.timeout = python, timeout

    def run(self, snapshot: Path, inputs: Path, directory: str, pattern: str) -> RunnerOutcome:
        # snapshot: 풀어 둔 소스 폴더, inputs: 실행기와 보호 테스트를 넣은 폴더,
        # directory·pattern: 보호 테스트 폴더(tests/signup)와 파일 이름 패턴(protected_signup_*.py)
        # 새 파이썬 프로세스(-I: 바깥 환경 무시, -B: 캐시 파일 안 만듦)에서 dev 실행기의 run_tests를 부르는 짧은 스크립트다.
        script = ('import importlib.util, json, sys\n'
                  'spec = importlib.util.spec_from_file_location("trusted_unit_runner", sys.argv[1])\n'
                  'runner = importlib.util.module_from_spec(spec)\n'
                  'spec.loader.exec_module(runner)\n'
                  'report, code = runner.run_tests(sys.argv[2], sys.argv[3], sys.argv[4])\n'
                  'sys.__stdout__.write(json.dumps(report if report is not None else {"error": "TEST_RUNNER_ERROR"}) + "\\n")\n'
                  'raise SystemExit(code)\n')
        with tempfile.TemporaryDirectory(prefix='eval-local-') as work:  # 작업 폴더는 끝나면 지워진다
            try:
                done = subprocess.run([self.python, '-I', '-B', '-c', script, str(inputs / RUNNER_FILE),
                                       str(inputs / directory), str(snapshot), pattern],
                                      capture_output=True, text=True, timeout=self.timeout, cwd=work)
            except subprocess.TimeoutExpired:
                return RunnerOutcome(None, None, 'TIMEOUT', None, self.kind)
        report, error = _parse_runner_output(done.stdout)
        return _checked_outcome(RunnerOutcome(report, done.returncode, error, None, self.kind))


class DockerRunner:
    # 실제 생성 코드를 채점하는 실행기. dev 샌드박스(runtime.py)와 같은 제한으로 컨테이너를 띄운다:
    # 네트워크 차단, 읽기 전용 루트, 권한 제거, 일반 사용자, CPU·메모리·프로세스 수 제한, /tmp·/work만 임시 쓰기,
    # Snapshot과 보호 테스트는 읽기 전용으로 붙인다. 이미지는 해시로 고정한 참조만 받는다
    # ("sha256:<이미지 ID>" 또는 "저장소@sha256:<저장소 해시>"). 태그(:latest 등)는 내용이 바뀔 수 있어 거부한다.
    kind = 'docker'

    def __init__(self, image: str, *, docker: str = 'docker', python: str = '/usr/local/bin/python',
                 cpus: float = 1.0, memory_bytes: int = 1024 * 1024 * 1024, pids: int = 256,
                 tmpfs_bytes: int = 256 * 1024 * 1024, timeout: float = 600):
        # image: 해시로 고정한 이미지 참조, docker: docker 프로그램 위치, python: 이미지 안 파이썬 위치,
        # cpus·memory_bytes·pids·tmpfs_bytes: CPU 수·메모리·프로세스 수·임시 폴더 크기 제한, timeout: 최대 실행 시간(초)
        if not isinstance(image, str) or not (re.fullmatch(r'sha256:[0-9a-f]{64}', image) or re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}', image)):
            raise ValueError('Image must be pinned by digest')
        self.image, self.docker, self.python = image, docker, python
        self.cpus, self.memory_bytes, self.pids, self.tmpfs_bytes, self.timeout = cpus, memory_bytes, pids, tmpfs_bytes, timeout

    def _call(self, args, timeout):
        # docker 명령 하나를 실행하고 결과(종료 코드·출력)를 돌려준다. 시간을 넘기면 TimeoutExpired 예외가 난다.
        return subprocess.run([self.docker, *args], capture_output=True, text=True, timeout=timeout)

    def image_digest(self) -> Optional[str]:
        # 이미지가 실제로 있고 참조와 같은 내용인지 확인한 뒤 그 해시를 돌려준다(dev runtime.py의 확인 규칙과 같음).
        try:
            done = self._call(('image', 'inspect', self.image), 60)
            image = json.loads(done.stdout)[0] if done.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, ValueError, IndexError, KeyError):
            return None
        if not isinstance(image, dict) or image.get('Os') != 'linux':
            return None
        # 이미지 ID로 지정했으면 Id가 같아야 하고, "저장소@해시"로 지정했으면 RepoDigests 목록에 그 참조가 있어야 한다.
        if self.image.startswith('sha256:'):
            return self.image if image.get('Id') == self.image else None
        return self.image.rsplit('@', 1)[1] if self.image in (image.get('RepoDigests') or []) else None

    def command(self, name: str, snapshot: Path, inputs: Path, directory: str, pattern: str) -> list:
        # 컨테이너를 띄우는 docker run 명령(인자 목록)을 만든다. 시험에서 제한이 빠지지 않았는지 확인할 수 있게 따로 둔다.
        #   --network none: 인터넷 차단, --read-only: 루트 파일 시스템 읽기 전용, --user 10001: 관리자 아닌 사용자,
        #   --cap-drop ALL·no-new-privileges: 특수 권한 제거, --pull never: 이미지를 새로 받아 오지 않음,
        #   --tmpfs: 쓰기는 메모리 위 임시 폴더(/tmp·/work)에만, --mount ...readonly: Snapshot·보호 테스트는 읽기만
        tmpfs = f'rw,nosuid,nodev,size={self.tmpfs_bytes},mode=1777'
        args = ['run', '--rm', '--name', name, '--pull', 'never', '--read-only', '--network', 'none',
                '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                '--cpus', str(self.cpus), '--memory', str(self.memory_bytes), '--memory-swap', str(self.memory_bytes),
                '--pids-limit', str(self.pids), '--ipc', 'none', '--workdir', '/work',
                '--tmpfs', f'/tmp:{tmpfs}', '--tmpfs', f'/work:{tmpfs}']
        for source, target in ((snapshot, '/snapshot'), (inputs, '/inputs')):
            # --mount 값은 쉼표로 항목을 나누므로, 경로에 쉼표·줄바꿈·따옴표가 있으면 다른 설정으로 끼어들 수 있어 거부한다.
            if any(char in str(source) for char in (',', '\n', '\r', '"')):
                raise ValueError('Mount path contains separator characters')
            args += ['--mount', f'type=bind,src={source},dst={target},readonly']
        args += ['--entrypoint', self.python, self.image, '-I', '-B', f'/inputs/{RUNNER_FILE}',
                 '--kind', 'PROTECTED', '--directory', directory, '--pattern', pattern]
        return args

    def run(self, snapshot: Path, inputs: Path, directory: str, pattern: str) -> RunnerOutcome:
        # 1) 이미지를 확인하고 2) 이름을 붙인 컨테이너로 실행한다. 시간을 넘기면 그 이름의 컨테이너를 강제로 지운다.
        digest = self.image_digest()
        if digest is None:
            return RunnerOutcome(None, None, 'IMAGE_UNAVAILABLE_OR_MISMATCH', None, self.kind)
        name = f'eval-protected-{uuid.uuid4().hex[:12]}'
        try:
            done = self._call(self.command(name, snapshot, inputs, directory, pattern), self.timeout)
        except subprocess.TimeoutExpired:
            try:
                self._call(('rm', '-f', name), 60)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return RunnerOutcome(None, None, 'TIMEOUT', digest, self.kind)
        except OSError:
            return RunnerOutcome(None, None, 'DOCKER_UNAVAILABLE', None, self.kind)
        report, error = _parse_runner_output(done.stdout)
        return _checked_outcome(RunnerOutcome(report, done.returncode, error, digest, self.kind))


def _sha256(data: bytes) -> str:
    # 바이트의 SHA-256 지문(16진수 64글자)
    return hashlib.sha256(data).hexdigest()


def read_snapshot(archive: bytes) -> dict:
    # dev canonical Snapshot tar를 메모리에서 읽어 {경로: 바이트}로 돌려준다. 압축된 tar·안전하지 않은 항목은 ArchiveError.
    if not isinstance(archive, (bytes, bytearray)) or len(archive) > MAX_ARCHIVE_BYTES:
        raise ArchiveError('SNAPSHOT_TOO_LARGE')
    try:
        # mode='r:'은 "압축하지 않은 tar만" 연다는 뜻이다. dev Snapshot은 압축하지 않으므로 압축된 파일은 거부된다.
        with tarfile.open(fileobj=io.BytesIO(bytes(archive)), mode='r:') as tar:
            members = tar.getmembers()
            if not members or len(members) > MAX_ARCHIVE_FILES:
                raise ArchiveError('SNAPSHOT_FILE_COUNT')
            files = {}
            for member in members:
                name = member.name
                parts = name.split('/')
                # 일반 파일만, 상대 경로만 받는다. 폴더 항목·링크·장치 파일, "/"로 시작하는 경로, 역슬래시, "C:" 같은 드라이브,
                # 빈 조각·"."·".."(폴더 밖으로 나가는 경로)는 모두 거부한다.
                if (not member.isfile() or name.startswith('/') or '\\' in name or ':' in parts[0]
                        or any(part in ('', '.', '..') for part in parts)):
                    raise ArchiveError('SNAPSHOT_UNSAFE_ENTRY')
                if member.size > MAX_FILE_BYTES or name in files:
                    raise ArchiveError('SNAPSHOT_UNSAFE_ENTRY')
                handle = tar.extractfile(member)  # 디스크에 풀지 않고 메모리에서 내용만 읽는다
                files[name] = handle.read() if handle is not None else b''
    except tarfile.TarError:
        raise ArchiveError('SNAPSHOT_ARCHIVE_INVALID') from None
    # 파일이 다른 파일의 상위 폴더 자리를 차지하면(a와 a/b) 풀 수 없다.
    for name in files:
        if any('/'.join(name.split('/')[:index]) in files for index in range(1, len(name.split('/')))):
            raise ArchiveError('SNAPSHOT_UNSAFE_ENTRY')
    return files


def _write_tree(root: Path, files: Mapping[str, bytes]):
    # {상대 경로: 바이트}를 root 폴더 아래에 파일로 쓴다(경로 확인은 read_snapshot이 이미 했다).
    for name, data in files.items():
        path = root.joinpath(*PurePosixPath(name).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


@dataclass(frozen=True)
class _StaticRequest:
    # security.assess.evaluate()가 읽는 항목만 담은 요청(dev Host 없이 최종 평가에서 부르기 위함).
    request_id: uuid.UUID
    request_sha256: str
    approval_sha256: str
    criteria: dict
    scenario_contract: dict
    source_files: Mapping
    requirement_ids: tuple
    permitted_evidence_refs: tuple
    scans: tuple = ()
    finding_ids: tuple = ()


def _static_assessment(files: Mapping[str, bytes], criteria: dict, contract: dict, evidence_id: str):
    # 같은 Snapshot 소스로 정적 판정을 한다. 돌려주는 값: (요구사항별 결과 사전 또는 None, 증거 JSON 또는 None, 오류 코드)
    sources = {path: data for path, data in files.items() if path.endswith('.py')}  # 파이썬 파일만 판정한다
    if sum(len(data) for data in sources.values()) > MAX_STATIC_BYTES:
        return None, None, 'STATIC_SOURCE_TOO_LARGE'
    requirement_ids = tuple(uuid.UUID(str(item['requirementId'])) for item in contract.get('requirements', [])
                            if 'SECURITY' in item.get('validators', ()))
    # 판정기는 근거로 인용할 수 있는 주소 목록에서만 골라 쓴다. 파일마다 이 평가의 증거 주소를 하나씩 만든다.
    refs = tuple(f'evidence://{evidence_id}/source.tar#path=' + quote(path, safe='/') for path in sorted(sources))
    canonical = json.dumps(criteria, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    request = _StaticRequest(uuid.uuid4(), _sha256(b''.join(sorted(sources.values()))), _sha256(canonical),
                             criteria, contract, sources, requirement_ids, refs)
    try:
        outcome = evaluate_static(request)
        evidence = json.loads(outcome['evidence_json'])
    except Exception:  # 정적 판정기 내부 오류는 확인 실패로 남기고 PASS를 만들지 않는다.
        return None, None, 'STATIC_ASSESSMENT_ERROR'
    return evidence.get('requirements'), evidence, None


def evaluate_final_snapshot(*, archive: bytes, expected: ExecutionManifest, scenario_contract: Mapping,
                            source_run_id: str, runner, architecture: str = 'MULTI_AGENT',
                            criteria: Optional[dict] = None, evaluation_id: Optional[str] = None,
                            created_at: Optional[str] = None, source_run: Optional[SourceRunSummary] = None,
                            run_bundle: Optional[Mapping] = None, trace_documents: Optional[Mapping] = None) -> dict:
    # archive        : 최종 Snapshot의 dev canonical tar 바이트
    # expected       : 신뢰할 수 있는 경로로 받은 기준 Manifest(어떤 코드·환경을 채점해야 하는지)
    # runner         : DockerRunner(실제 평가) 또는 LocalRunner(trusted=True, 합성 표본 시험)
    # run_bundle     : (선택) run_metrics.fetch_run_bundle 결과. 있으면 REQ-008 추적 검사를 함께 한다.
    # trace_documents: (선택) trace_checks.fetch_trace_documents 결과. 있으면 Run 기록 노출 검사를 함께 한다.
    # 돌려주는 값: {'report': 독립 평가 보고서, 'evidence': {증거 번호: 증거 문서}, 'snapshot': 확인 결과,
    #              'runner': 실행 정보, 'trace': Trace 검사 결과 또는 None}
    # 0) 함께 넘긴 Run 기록이 채점 대상 Run·최종 코드 버전의 것인지 확인한다(다른 Run 기록을 섞지 않게).
    if run_bundle is not None:
        run = run_bundle.get('run', {})
        if run.get('runId') != source_run_id or run.get('codeVersion') != expected.code_version:
            raise ValueError('Run bundle differs from the evaluated Run or final code version')
    if trace_documents is not None and trace_documents.get('runId') != source_run_id:
        raise ValueError('Trace documents differ from the evaluated Run')
    # 1) 준비: 기준·검사 계획·증거 번호·평가 시각
    criteria = criteria if criteria is not None else json.loads(CRITERIA_FILE.read_text(encoding='utf-8'))
    inputs = protected_tests.evaluation_inputs(scenario_contract)
    unit_evidence_id, static_evidence_id = str(uuid.uuid4()), str(uuid.uuid4())
    evaluation_id = evaluation_id or str(uuid.uuid4())
    created_at = created_at or datetime.now(timezone.utc).isoformat()
    snapshot_sha256 = _sha256(bytes(archive)) if isinstance(archive, (bytes, bytearray)) else None
    snapshot_info = {'sha256': snapshot_sha256, 'matchesExpected': snapshot_sha256 == expected.snapshot_sha256,
                     'lockFound': False, 'files': 0, 'error': None}
    unit_report = static_requirements = None
    static_evidence, static_error = None, None
    outcome = RunnerOutcome(None, None, 'NOT_RUN', None, getattr(runner, 'kind', 'unknown'))  # 실행 전 기본값
    runner_error = 'TEST_RUNNER_ERROR'
    # 2) Snapshot 확인과 안전한 읽기
    try:
        files = read_snapshot(archive)
    except ArchiveError as error:
        files, snapshot_info['error'] = None, str(error)
        runner_error = str(error)
    if files is not None and not snapshot_info['matchesExpected']:
        # 받은 코드가 채점하기로 한 Snapshot이 아니다. 실행하지 않고 모든 검사를 미검증으로 남긴다.
        files, snapshot_info['error'] = None, 'SNAPSHOT_HASH_MISMATCH'
        runner_error = 'SNAPSHOT_HASH_MISMATCH'
    # 3) 실행: 임시 폴더에 소스(snapshot/)와 실행기·보호 테스트(inputs/)를 쓰고 실행기로 돌린다. 이어서 정적 판정.
    if files is not None:
        snapshot_info['files'] = len(files)
        snapshot_info['lockFound'] = any('sha256:' + _sha256(data) == expected.dependency_lock_hash for data in files.values())
        with tempfile.TemporaryDirectory(prefix='eval-final-') as directory:
            root = Path(directory)
            _write_tree(root / 'snapshot', files)
            protected = {path: content.encode('utf-8') for path, content in protected_tests.protected_files('final').items()}
            protected[RUNNER_FILE] = _runner_source()
            _write_tree(root / 'inputs', protected)
            os.chmod(root, 0o755)  # 컨테이너 안 일반 사용자(10001)도 읽을 수 있게 임시 폴더 권한을 연다
            suite = protected_tests.load_suite()
            outcome = runner.run(root / 'snapshot', root / 'inputs', suite['sourceDirectory'],
                                 suite['scopes']['final']['pattern'])
        outcome = _checked_outcome(outcome)  # 사용자가 넘긴 실행기도 같은 규칙으로 확인한다.
        unit_report = outcome.report
        runner_error = outcome.error or 'TEST_RUNNER_ERROR'
        static_requirements, static_evidence, static_error = _static_assessment(files, criteria, dict(scenario_contract),
                                                                                static_evidence_id)
    # 4) 검사 결과 만들기(실행 테스트 8개 + 정적 판정 3개)
    results = protected_tests.final_check_results(
        inputs, unit_report=unit_report, unit_evidence_id=unit_evidence_id,
        assessment_requirements=static_requirements, static_evidence_id=static_evidence_id, runner_error=runner_error)
    # 실제로 확인한 신분: Snapshot 해시(계산값, 기준과 같을 때만 실행), lock(같은 해시의 파일이 들어 있음),
    # 이미지(실행기가 확인한 해시). 하나라도 확인할 수 없으면 Manifest 없음으로 두어 결과를 쓰지 않는다.
    # 이미지 해시가 기준과 다르면 불일치로 결과를 쓰지 않는다. 확인하지 않은 값을 기준값으로 채우지 않는다.
    observed = None
    if files is not None and snapshot_info['lockFound'] and outcome.image_digest is not None:
        observed = replace(expected, snapshot_sha256=snapshot_sha256, container_image_digest=outcome.image_digest)
    report = build_evaluation_report(
        evaluation_id=evaluation_id, source_run_id=source_run_id, architecture=architecture,
        suite_id=inputs['suite_id'], suite_version=inputs['suite_version'], suite_sha256=inputs['suite_sha256'],
        created_at=created_at, required_requirement_ids=inputs['required_requirement_ids'],
        expected=expected, observed=observed, plan=inputs['plan'], results=results, bindings=inputs['bindings'],
        source_run=source_run)
    # 5) (선택) Run 기록 검사와 증거 문서 정리
    trace = None
    if run_bundle is not None or trace_documents is not None:
        trace = {'traceability': check_traceability(run_bundle, scenario_contract).to_dict() if run_bundle is not None else None,
                 'exposure': check_trace_exposure(trace_documents, criteria).to_dict() if trace_documents is not None else None}
    evidence = {unit_evidence_id: {'kind': 'UNIT_TEST_REPORT', 'runner': outcome.to_dict(), 'report': unit_report}}
    if static_evidence is not None or static_error is not None:
        evidence[static_evidence_id] = {'kind': 'STATIC_ASSESSMENT', 'error': static_error, 'evidence': static_evidence}
    return {'report': report, 'evidence': evidence, 'snapshot': snapshot_info, 'runner': outcome.to_dict(), 'trace': trace}


def _runner_source() -> bytes:
    # dev의 신뢰된 unittest 실행기 파일(mcp_tools/tools/unit_runner.py)을 그대로 읽는다(고치지 않음).
    import importlib.util  # 모듈을 실행하지 않고 파일 위치만 찾기 위한 도구
    spec = importlib.util.find_spec('mcp_tools.tools.unit_runner')
    if spec is None or spec.origin is None:
        raise RuntimeError('dev unit runner is not available')
    return Path(spec.origin).read_bytes()
