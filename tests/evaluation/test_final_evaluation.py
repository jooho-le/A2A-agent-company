"""final_evaluation.py(D5 최종 독립 채점기)를 시험한다.

확인하는 것:
  - Snapshot 압축 파일을 안전하게만 푸는가?(../·절대 경로·링크·폴더 항목·압축 tar·중복·파일/폴더 충돌 거부)
  - Snapshot 해시가 기준과 다르면 실행하지 않고, lock·이미지를 확인하지 못하면 결과를 쓰지 않는가?
  - 이미지 해시가 기준과 다르면 Manifest 불일치로 결과를 쓰지 않는가?
  - DockerRunner가 해시로 고정한 이미지만 받고, dev 샌드박스와 같은 제한으로 실행하며, 이미지 확인에 실패하면 실행하지 않는가?
  - (실행) 합성 회원가입 서비스를 채점 시험지(실행 검사 9개)로 채점해 독립 평가 보고서·점수까지 만드는가?
    순환용 시험지 참고 보고서, 보조 점검(지나간 줄 기록·정적 판정)은 판정·점수와 따로 나오는가?
    정상은 PASS, 평문 저장·약한 해시는 FAIL이고, 로컬 실행은 이미지를 확인할 수 없어 결과를 쓰지 않는가?
실제 Docker 시험은 EVAL_DOCKER_IMAGE(해시로 고정한 이미지 참조)를 주고 Docker가 켜져 있을 때만 돈다.
"""
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from uuid import UUID

import pytest

from evaluation.final_evaluation import (ArchiveError, DockerRunner, LocalRunner, RunnerOutcome, _write_tree,
                                         evaluate_final_snapshot, read_snapshot)
from evaluation.manifest import ExecutionManifest
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario
from tests.evaluation import signup_service

CONTRACT = get_scenario(SCN_001_ID).planner_contract()
LOCK = b'fastapi==0.142.2\nsqlalchemy==2.1.3\nargon2-cffi==25.1.0\n'
IMAGE = 'sha256:' + 'd' * 64
RUNTIME = all(importlib.util.find_spec(name) for name in ('fastapi', 'httpx', 'sqlalchemy', 'argon2'))
FIXTURE_RUNS = json.loads((Path(__file__).parent / 'fixtures/dev_pipeline_runs.json').read_text(encoding='utf-8'))['runs']


def uid(number):
    """시험용 가짜 UUIDv4. 같은 숫자면 같은 값이다."""
    return str(UUID(int=number, version=4))


def canonical_tar(files):
    """dev GitSnapshotBuilder와 같은 모양의 tar(PAX, 압축 없음, 경로 순 정렬, 시각·소유자 0)."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for path in sorted(files, key=lambda item: item.encode('utf-8')):
            data = files[path]
            member = tarfile.TarInfo(path)
            member.size, member.mode = len(data), 0o644
            member.uid = member.gid = member.mtime = 0
            member.uname = member.gname = ''
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def snapshot(variant='good', lock=LOCK):
    """합성 회원가입 서비스 변형(variant)의 Snapshot tar를 만든다. lock=None이면 lock 파일을 넣지 않는다."""
    files = {path: text.encode('utf-8') for path, text in signup_service.build(variant).items()}
    if lock is not None:
        files['requirements.lock'] = lock
    return canonical_tar(files)


def manifest(archive, lock=LOCK, image=IMAGE):
    """그 Snapshot이 기대값인 Manifest(Snapshot 해시·이미지 해시·lock 해시)를 만든다."""
    return ExecutionManifest('signup-repo', 2, uid(1), 'sha1', 'a' * 40, 'b' * 40,
                             hashlib.sha256(archive).hexdigest(), image, 'sha256:' + hashlib.sha256(lock).hexdigest())


class VerifiedImageRunner:
    """시험 대역: 합성 표본을 로컬에서 돌리고, 컨테이너 실행기가 이미지를 확인했다고 가정한 해시를 돌려준다.
    D5의 보고서 조립 경로를 시험하기 위한 것이며 실제 평가에는 쓰지 않는다."""
    kind = 'docker'

    def __init__(self, digest=IMAGE):
        self.local, self.digest, self.calls = LocalRunner(trusted=True), digest, 0

    def run(self, snapshot_dir, inputs_dir, directory, pattern, script='_unit_runner.py'):
        self.calls += 1
        outcome = self.local.run(snapshot_dir, inputs_dir, directory, pattern, script=script)
        return RunnerOutcome(outcome.report, outcome.exit_code, outcome.error, self.digest, self.kind, outcome.coverage)


class RecordingRunner:
    """시험 대역: 몇 번 불렸는지만 세고 실행기 오류를 돌려준다. 실행하면 안 되는 경우를 확인하는 데 쓴다."""
    kind = 'docker'

    def __init__(self):
        self.calls = 0

    def run(self, *args, **kwargs):
        self.calls += 1
        return RunnerOutcome(None, 2, 'TEST_RUNNER_ERROR', IMAGE, self.kind)


def final(archive, expected, runner, **kwargs):
    """evaluate_final_snapshot을 SCN-001 정책·고정 평가 시각으로 부른다. 나머지 인자는 그대로 넘긴다."""
    return evaluate_final_snapshot(archive=archive, expected=expected, scenario_contract=CONTRACT,
                                   source_run_id=kwargs.pop('source_run_id', uid(2)), runner=runner,
                                   exam=kwargs.pop('exam', 'final'), created_at='2026-10-28T12:00:00+09:00', **kwargs)


def statuses(output, key='report'):
    """결과 보고서(기본은 채점 보고서)의 검사 상태를 {검사 번호(QA-SIGNUP-001 등): 상태}로 꺼낸다."""
    return {check['title'].split(' ', 1)[0]: check['status'] for check in output[key]['checks']}


# ---------------------------------------------------------------- Snapshot 읽기


def raw_tar(add):
    """add(archive)로 항목을 직접 넣은 tar를 만든다. 위험한 항목을 일부러 넣는 데 쓴다."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w', format=tarfile.PAX_FORMAT) as archive:
        add(archive)
    return output.getvalue()


def add_file(name, data=b'x'):
    """이름이 name인 일반 파일 항목을 넣는 함수를 돌려준다."""
    def add(archive):
        member = tarfile.TarInfo(name)
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
    return add


def test_canonical_snapshot_is_read():
    """dev와 같은 모양의 Snapshot은 파일 내용 그대로 읽힌다."""
    files = read_snapshot(snapshot())
    assert 'main.py' in files and files['requirements.lock'] == LOCK


def add_special(name, kind, link=''):
    """폴더·링크 같은 특수 항목(kind)을 넣는 함수를 돌려준다."""
    def add(archive):
        member = tarfile.TarInfo(name)
        member.type, member.linkname = kind, link
        archive.addfile(member)
    return add


def add_both(*adders):
    """여러 항목 넣기 함수를 차례로 실행하는 함수를 돌려준다(중복·파일/폴더 충돌 만들기)."""
    def add(archive):
        for adder in adders:
            adder(archive)
    return add


@pytest.mark.parametrize('archive', [
    raw_tar(add_file('../escape.py')),
    raw_tar(add_file('/abs.py')),
    raw_tar(add_file('a/./b.py')),
    raw_tar(add_special('dir', tarfile.DIRTYPE)),
    raw_tar(add_special('link.py', tarfile.SYMTYPE, '/etc/passwd')),
    raw_tar(add_both(add_file('x.py'), add_file('x.py'))),
    raw_tar(add_both(add_file('pkg'), add_file('pkg/mod.py'))),
    b'not a tar',
], ids=['parent', 'absolute', 'dot', 'directory', 'symlink', 'duplicate', 'file-dir-clash', 'garbage'])
def test_unsafe_snapshot_is_refused(archive):
    """../·절대 경로·"./" 경로·폴더 항목·링크·중복·파일/폴더 충돌·tar가 아닌 값은 ArchiveError로 거부한다."""
    with pytest.raises(ArchiveError):
        read_snapshot(archive)


def test_compressed_snapshot_is_refused():
    """압축된 tar(gz)는 dev Snapshot 모양이 아니므로 거부한다."""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        add_file('main.py')(archive)
    with pytest.raises(ArchiveError):
        read_snapshot(output.getvalue())


# ---------------------------------------------------------------- 신분 확인


def test_wrong_snapshot_is_not_run():
    """Snapshot 해시가 기대값과 다르면 실행기를 부르지 않고, 신분 미확인(MANIFEST_MISSING) UNVERIFIED 보고서를 만든다."""
    archive, runner = snapshot(), RecordingRunner()
    output = final(archive, manifest(snapshot('plaintext_storage')), runner)
    assert runner.calls == 0 and output['snapshot']['error'] == 'SNAPSHOT_HASH_MISMATCH'
    assert output['report']['evaluationVerdict'] == 'UNVERIFIED'
    assert output['report']['manifestCheck']['errorCode'] == 'MANIFEST_MISSING'


def test_unsafe_archive_yields_unverified_report_instead_of_crash():
    """위험한 항목이 든 Snapshot은 예외로 멈추지 않고 SNAPSHOT_UNSAFE_ENTRY 미검증 보고서가 된다."""
    archive = raw_tar(add_file('../escape.py'))
    output = final(archive, manifest(archive), RecordingRunner())
    assert output['snapshot']['error'] == 'SNAPSHOT_UNSAFE_ENTRY' and output['report']['evaluationVerdict'] == 'UNVERIFIED'


def test_missing_lock_means_results_are_not_used():
    """lock 파일이 없으면 환경을 확인할 수 없으므로 결과를 판정에 쓰지 않는다(MANIFEST_MISSING)."""
    archive = snapshot(lock=None)
    output = final(archive, manifest(archive), RecordingRunner())
    assert output['snapshot']['lockFound'] is False
    assert output['report']['manifestCheck']['errorCode'] == 'MANIFEST_MISSING'


@pytest.mark.parametrize('exam', ['practice', 'in-loop-qa', None])
def test_exam_must_be_a_scored_set(exam):
    """채점 시험지는 시범 최종용(pilot)·공식 최종용(final)만 고를 수 있다(순환용은 참고 채점에만)."""
    archive = snapshot()
    with pytest.raises(ValueError, match='pilot or final'):
        final(archive, manifest(archive), RecordingRunner(), exam=exam)


def test_snapshot_with_exam_files_is_not_run_or_scored():
    """채점할 Snapshot에 시험지가 섞여 있으면(에이전트가 시험지를 볼 수 있었음) 실행하지 않고 점수도 만들지 않는다.
    요약에는 "시험지 노출"로 드러낸다."""
    from evaluation import protected_tests
    from evaluation.summary import render_summary
    files = {path: text.encode('utf-8') for path, text in signup_service.build('good').items()}
    files['requirements.lock'] = LOCK
    files['tests/test_exam_copy.py'] = protected_tests.protected_files('final')['tests/signup/exam_final.py'].encode('utf-8')
    archive, runner = canonical_tar(files), RecordingRunner()
    output = final(archive, manifest(archive), runner)
    assert runner.calls == 0 and output['snapshot']['error'] == 'EXAM_EXPOSED'
    assert [item['path'] for item in output['snapshot']['examExposure']] == ['tests/test_exam_copy.py']
    assert output['report']['evaluationVerdict'] == 'UNVERIFIED'
    assert output['score']['score'] is None and output['score']['reason'] == 'EXAM_EXPOSED'
    assert output['referenceScore']['reason'] == 'EXAM_EXPOSED'
    text = render_summary(output['report'], scenario_contract=CONTRACT, supplementary=output['supplementary'])
    assert '**시험지 노출**' in text and 'tests/test_exam_copy.py' in text


def test_local_runner_requires_explicit_trust():
    """LocalRunner는 trusted=True를 명시하지 않으면 만들 수 없다(생성 코드를 호스트에서 돌리지 않게)."""
    with pytest.raises(ValueError):
        LocalRunner()


# ---------------------------------------------------------------- DockerRunner


@pytest.mark.parametrize('image', ['python:3.13', 'python@sha256:abc', 'sha256:' + 'D' * 64, ''])
def test_docker_runner_requires_digest_pinned_image(image):
    """DockerRunner는 "sha256:" + 소문자 64자 해시로 고정한 이미지만 받는다(태그·짧은 해시·대문자·빈칸 거부)."""
    with pytest.raises(ValueError):
        DockerRunner(image)


def test_docker_command_matches_dev_sandbox_limits(tmp_path):
    """docker run 명령이 dev 샌드박스와 같은 제한(네트워크 없음·읽기 전용·일반 사용자·권한 제거 등)과
    읽기 전용 마운트를 쓰고, 마지막에 이미지와 실행기 인자가 정확히 온다."""
    args = DockerRunner(IMAGE).command('eval-x', tmp_path / 's', tmp_path / 'i', 'tests/signup', 'protected_signup_*.py')
    joined = ' '.join(args)
    for flag in ('--network none', '--read-only', '--user 10001:10001', '--cap-drop ALL', '--pull never',
                 '--security-opt no-new-privileges:true', '--ipc none', '--pids-limit 256'):
        assert flag in joined
    assert f'type=bind,src={tmp_path / "s"},dst=/snapshot,readonly' in args
    assert f'type=bind,src={tmp_path / "i"},dst=/inputs,readonly' in args
    assert args[-10:] == [IMAGE, '-I', '-B', '/inputs/_unit_runner.py', '--kind', 'PROTECTED', '--directory',
                          'tests/signup', '--pattern', 'protected_signup_*.py']
    coverage = DockerRunner(IMAGE).command('eval-x', tmp_path / 's', tmp_path / 'i', 'tests/signup', 'exam_final.py',
                                           script='_coverage_runner.py')
    assert coverage[-7] == '/inputs/_coverage_runner.py'
    with pytest.raises(ValueError):
        DockerRunner(IMAGE).command('eval-x', tmp_path / 's', tmp_path / 'i', 'tests/signup', 'x.py', script='other.py')


def test_written_tree_is_readable_by_container_user(tmp_path):
    """이 컴퓨터의 umask가 엄격해도(077) 컨테이너 안 일반 사용자가 읽을 수 있게 폴더 755·파일 644로 쓴다."""
    previous = os.umask(0o077)
    try:
        _write_tree(tmp_path / 'tree', {'main.py': b'x', 'routers/auth.py': b'y'})
    finally:
        os.umask(previous)
    modes = {path.relative_to(tmp_path).as_posix(): stat.S_IMODE(path.stat().st_mode)
             for path in [tmp_path / 'tree', *(tmp_path / 'tree').rglob('*')]}
    assert modes == {'tree': 0o755, 'tree/main.py': 0o644, 'tree/routers': 0o755, 'tree/routers/auth.py': 0o644}


def fake_docker(tmp_path, image_id, report):
    """docker 대신 쓰는 시험용 프로그램: image inspect에는 이미지 정보를, run에는 실행기 출력을 돌려준다."""
    script = tmp_path / 'docker'
    script.write_text('#!' + sys.executable + '\nimport json, sys\n'
                      f'IMAGE = {json.dumps({"Id": image_id, "Os": "linux", "RepoDigests": []})}\n'
                      f'REPORT = {json.dumps(report)}\n'
                      'if sys.argv[1:3] == ["image", "inspect"]:\n    print(json.dumps([IMAGE]))\n'
                      'elif sys.argv[1] == "run":\n    print(json.dumps(REPORT))\n    sys.exit(0)\n', encoding='utf-8')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_docker_runner_checks_image_before_running(tmp_path):
    """이미지를 먼저 확인해 해시가 맞을 때만 실행한다. 해시가 다르거나 docker가 없으면
    실행하지 않고 IMAGE_UNAVAILABLE_OR_MISMATCH다."""
    report = {'format': 'unittest-v1', 'total': 1, 'passed': 1, 'failed': 0, 'skipped': 0,
              'tests': [{'testId': 'm.C.test_a', 'outcome': 'PASS'}]}
    ok = DockerRunner(IMAGE, docker=fake_docker(tmp_path, IMAGE, report)).run(tmp_path, tmp_path, 'tests/signup', 'p.py')
    assert ok.report == report and ok.image_digest == IMAGE and ok.error is None
    other = tmp_path / 'other'
    other.mkdir()
    bad = DockerRunner(IMAGE, docker=fake_docker(other, 'sha256:' + 'e' * 64, report)).run(tmp_path, tmp_path, 't', 'p.py')
    assert bad.report is None and bad.error == 'IMAGE_UNAVAILABLE_OR_MISMATCH'
    missing = DockerRunner(IMAGE, docker=str(tmp_path / 'no-docker')).run(tmp_path, tmp_path, 't', 'p.py')
    assert missing.error == 'IMAGE_UNAVAILABLE_OR_MISMATCH'


# ---------------------------------------------------------------- 실행(합성 서비스)

needs_runtime = pytest.mark.skipif(not RUNTIME, reason='fastapi·httpx·sqlalchemy·argon2-cffi가 있어야 실행 시험을 돈다')


@needs_runtime
def test_good_service_passes_end_to_end_with_verified_image():
    """정상 서비스는 채점 시험지 검사 9개가 모두 PASS(점수 1.0·완료율 1.0)이고, 순환용 참고 보고서도 PASS다.
    보조 점검으로 지나간 줄 기록과 정적 판정이 판정과 따로 나오고, Run 기록 추적 검사도 PASS다.
    증거는 실행 보고서 두 개(채점·참고)·줄 기록·정적 판정이다."""
    archive = snapshot()
    bundle = FIXTURE_RUNS['qa_failure_actual_fix_and_revalidation_preserve_all_lineages_and_shared_budget']
    output = final(archive, manifest(archive), VerifiedImageRunner(),
                   source_run_id=bundle['run']['runId'], run_bundle=bundle)
    assert output['exam'] == 'final' and output['report']['evaluationVerdict'] == 'PASS', statuses(output)
    assert len(output['report']['checks']) == 9 and set(statuses(output).values()) == {'PASS'}
    assert output['score'] == {'score': 1.0, 'completionRate': 1.0, 'scoreIsLowerBound': False,
                               'requirementShares': output['score']['requirementShares'],
                               'securityViolations': [], 'reason': None}
    assert len(output['score']['requirementShares']) == 7
    assert output['referenceReport']['evaluationVerdict'] == 'PASS' and output['referenceScore']['score'] == 1.0
    assert output['referenceReport']['evaluationId'] != output['report']['evaluationId']
    assert output['report']['suite']['sha256'] != output['referenceReport']['suite']['sha256']
    coverage = output['supplementary']['coverage']
    assert coverage['error'] is None and 0 < coverage['loaded']['rate'] <= 1
    assert coverage['files']['routers/auth.py']['executed'] > 0 and 'requirements.lock' not in coverage['files']
    assert not any(path.startswith(('tests/', '_')) for path in coverage['files'])  # 시험지·실행기는 기록하지 않는다
    assert {item['outcome'] for item in output['supplementary']['static']['requirements'].values()} == {'PASS'}
    assert output['trace']['traceability']['status'] == 'PASS'
    kinds = sorted(item['kind'] for item in output['evidence'].values())
    assert kinds == ['LINE_COVERAGE', 'STATIC_ASSESSMENT', 'UNIT_TEST_REPORT', 'UNIT_TEST_REPORT']


@needs_runtime
def test_weak_hash_fails_runtime_check_and_static_supplement():
    """Argon2 메모리 설정이 기준 미만이면 실행 검사 SEC-SIGNUP-002가 FAIL이고 판정은 FAIL이다. 점수는 그 요구사항만
    깎이고(요구사항별 같은 비중) 보안 위반으로 따로 드러난다. 정적 보조 점검(REQ-005)도 FAIL을 적는다."""
    archive = snapshot('weak_argon2_memory')
    output = final(archive, manifest(archive), VerifiedImageRunner())
    result = statuses(output)
    assert result['SEC-SIGNUP-002'] == 'FAIL' and result['QA-SIGNUP-001'] == 'PASS'
    assert output['report']['evaluationVerdict'] == 'FAIL'
    assert output['score']['score'] == pytest.approx(6 / 7) and output['score']['securityViolations'] == ['SEC-SIGNUP-002']
    assert output['supplementary']['static']['requirements']['REQ-005']['outcome'] == 'FAIL'


@needs_runtime
def test_runtime_check_catches_what_static_cannot_prove():
    """hash_password()가 매개변수를 그대로 돌려주면 정적 보조 점검은 확인 불가(UNVERIFIED)이고, 실행 검사가 평문 저장을
    FAIL로 잡는다. 정적 보조 점검은 판정에 들어가지 않는다."""
    archive = snapshot('plaintext_storage')
    output = final(archive, manifest(archive), VerifiedImageRunner())
    assert statuses(output)['SEC-SIGNUP-002'] == 'FAIL' and output['report']['evaluationVerdict'] == 'FAIL'
    assert output['supplementary']['static']['requirements']['REQ-005']['outcome'] == 'UNVERIFIED'
    checks = {check['testId']: check['title'] for check in output['report']['checks']}
    assert any(checks[check_id].startswith('SEC-SIGNUP-002') for check_id in output['report']['confirmedFailureIds'])


@needs_runtime
def test_pilot_exam_is_scored_with_its_own_suite():
    """시범 최종용(pilot)으로 채점하면 보고서의 suite 지문이 pilot 것이고 결과는 같은 검사 9개다."""
    from evaluation import protected_tests
    archive = snapshot()
    output = final(archive, manifest(archive), VerifiedImageRunner(), exam='pilot')
    assert output['exam'] == 'pilot' and output['report']['suite']['sha256'] == protected_tests.suite_sha256('pilot')
    assert output['report']['evaluationVerdict'] == 'PASS' and len(output['report']['checks']) == 9


@needs_runtime
def test_image_digest_mismatch_blocks_results():
    """실행한 이미지 해시가 기대값과 다르면 Manifest 불일치로 결과를 판정에 쓰지 않는다(UNVERIFIED). 점수도 만들지 않는다."""
    archive = snapshot()
    output = final(archive, manifest(archive), VerifiedImageRunner(digest='sha256:' + 'e' * 64))
    assert output['report']['manifestCheck']['errorCode'] == 'MANIFEST_MISMATCH'
    assert output['report']['evaluationVerdict'] == 'UNVERIFIED' and output['score']['score'] is None


@needs_runtime
def test_local_runner_results_are_never_used_as_final_evidence():
    """LocalRunner는 이미지를 확인할 수 없으므로, 실행이 되어도 결과를 최종 판정에 쓰지 않는다."""
    archive = snapshot()
    output = final(archive, manifest(archive), LocalRunner(trusted=True))
    assert output['runner']['kind'] == 'local' and output['runner']['imageDigest'] is None
    assert output['report']['manifestCheck']['errorCode'] == 'MANIFEST_MISSING'
    unit = next(item for item in output['evidence'].values() if item['kind'] == 'UNIT_TEST_REPORT' and item['exam'] == 'final')
    assert unit['report']['passed'] == 9  # 실행 자체는 되었지만 보고서 판정에는 쓰지 않는다.


def docker_ready():
    """실제 컨테이너 시험을 돌릴 수 있는지: EVAL_DOCKER_IMAGE가 있고 docker info가 성공해야 한다."""
    image = os.environ.get('EVAL_DOCKER_IMAGE')
    if not image:
        return False
    try:
        return subprocess.run(['docker', 'info'], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not docker_ready(), reason='EVAL_DOCKER_IMAGE와 실행 중인 Docker가 있어야 실제 컨테이너 시험을 돈다')
def test_real_container_run():
    """실제 Docker로 정상 서비스를 채점하면 실행기 오류 없이 PASS다."""
    image = os.environ['EVAL_DOCKER_IMAGE']
    runner = DockerRunner(image)
    archive = snapshot()
    output = final(archive, manifest(archive, image=runner.image_digest()), runner)
    assert output['runner']['error'] is None, output['runner']
    assert output['report']['evaluationVerdict'] == 'PASS', statuses(output)
