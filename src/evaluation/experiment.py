# Ver1·Ver2 실험을 준비하고 결과를 모으는 운영 도구(4번 담당). 10.29 회의록의 비교 방안 1(순환 + 최종)·2(최종 중심)를
# 회의 뒤 방침으로 먼저 멀티에이전트 Ver1·Ver2에 적용한다. compare --group-by architecture면 같은 방안 안의 Single·Multi 비교.
#   python -m evaluation.experiment configure --tools <owned-tools.local.json> --out <폴더> [--source <owned-source.local.json>]
#   python -m evaluation.experiment fetch     --api http://127.0.0.1:8000 --run-id <Run> --out <폴더>   (bundle.json·trace.json)
#   python -m evaluation.experiment score     --export <snapshot_export 폴더> --exam final --image <repo@sha256:...>
#                                             --results <폴더> [--bundle <bundle.json>] [--trace <trace.json>]
#                                             [--a2a <dev a2a_export 결과>] [--pair-id <쌍 번호>] [--loop-policy <버전>]
#                                             [--architecture <구조>] [--all-versions] [--force]
#   python -m evaluation.experiment record    --bundle <bundle.json> --results <폴더> [--pair-id] [--loop-policy]
#   python -m evaluation.experiment compare   --results <폴더> [--planned 정책=N ...] [--group-by architecture] [--out <폴더>]
#
# configure: Host 도구 설정(owned-tools.local.json)에 QA·Security 자체 테스트 범위를 두고, Ver1에는 순환용 보호 범위 두 개와
#            보호 케이스 파일을 더한 설정 쌍을 만든다(ver1/·ver2/). 두 버전의 차이는 보호 테스트뿐이다. 시작 코드 설정을
#            주면 그 커밋을 dev와 같은 방식으로 꺼내 시험지·평가 코드가 섞였는지 먼저 확인하고, 섞였으면 만들지 않는다.
# fetch    : dev Run 조회 API에서 Run 기록(run_metrics.fetch_run_bundle)과 노출 검사용 기록(trace_checks.fetch_trace_documents)을
#            받아 bundle.json·trace.json으로 저장한다. A2A Message 본문은 API로 받을 수 없어 dev a2a_export로 따로 꺼낸다.
# score    : dev snapshot_export가 꺼낸 Snapshot(source.tar·manifest.json)을 D5로 채점하고 결과·요약을 Run별 폴더에 보관한다.
#            --all-versions면 중간 코드 버전도 같은 시험지로 채점해 Regression(수정으로 새로 생긴 실패)을 계산한다.
#            수정 없이 첫 버전에서 끝난 Run은 비교할 버전 쌍이 0개로 남는다.
#            버전별 결과는 개발 순환에 전달하지 않는다. 실제 생성 코드는 DockerRunner(해시로 고정한 이미지)로만 실행한다.
# record   : 채점하지 못한 Run(빌드 실패·중단 등)의 Run 기록만 결과 폴더에 남긴다. compare가 실패·비용 집계에 넣는다.
# compare  : 보관한 Run들(채점 여부와 상관없이)로 Ver1·Ver2 비교 가능 여부, 정책별 요약(상태·시간·토큰·비용·점수,
#            최종 독립 평가 PASS 기준 달성 시간), 차이의 차이(보조 지표)를 계산해 저장한다. --group-by architecture면
#            같은 방안 안의 Single·Multi를 구조별로 비교한다.
#
# [처음 읽는 분을 위한 용어]
# - 쌍 번호(pair id): 같은 조건·같은 시간대에 함께 실행한 Ver1·Ver2 한 쌍을 묶는 이름. 비교 검사가 쌍마다 둘이 있는지 본다.
# - 결과 폴더: <results>/<Run 번호>/ 아래에 output.json(D5 결과), summary.md(요약), meta.json(쌍 번호·시험지·버전), bundle.json(Run 기록),
#   (--all-versions면) versions/v<N>.json(중간 버전 보고서)·regression.json. 권장 위치는 저장소의
#   experiments/scn001/<시험지>/<날짜-이름>/ 이고(커밋), 내보낸 Snapshot tar는 커밋하지 않는다(.gitignore: experiments/**/export/).
#   결과 폴더는 시작 코드에 섞이면 안 된다(protected_tests.exam_exposure가 함께 찾는다).
import argparse
import copy
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Mapping, Optional

from . import protected_tests
from .final_evaluation import DockerRunner, evaluate_final_snapshot, read_snapshot
from .manifest import ExecutionManifest
from .run_metrics import (GROUPINGS, LOOP_POLICIES, architecture_pairs, comparison_validity, experiment_summary,
                          fetch_run_bundle, is_assisted_run, loop_policy_effect, regression_from_reports, run_metrics)
from .summary import render_summary
from .trace_checks import A2A_DOCUMENT, fetch_trace_documents
from .evidence_metrics import first_passing_snapshot, process_metrics

SECURITY_TESTS = {'name': 'security-tests', 'kind': 'QA_TESTS', 'source_directory': 'tests', 'pattern': 'test_*.py',
                  'role': 'SECURITY'}  # Security 에이전트가 쓰는 자체 테스트 범위(outputs/security/tests)


class ExperimentError(ValueError):
    # 실험 준비·채점을 멈춰야 하는 이유(코드 한 개). 경로·비밀값은 넣지 않는다.
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _contract(path=None):
    # 시나리오 정책. 파일을 주면 그 JSON, 없으면 dev에 등록된 SCN-001 정책
    if path:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario
    return get_scenario(SCN_001_ID).planner_contract()


def _check_contract(contract):
    # 시나리오 정책이 보호 테스트가 전제로 하는 정책(protected_tests.SUPPORTED_POLICY)과 같은지 본다.
    try:
        protected_tests.check_policy(contract)
    except (AttributeError, TypeError, ValueError):
        raise ExperimentError('CONTRACT_NOT_SUPPORTED') from None


def _write(path: Path, data, force: bool):
    # 파일 하나를 쓴다(사전·목록은 들여쓴 JSON, 글자·바이트는 그대로). 이미 있으면 force일 때만 덮어쓴다.
    if path.exists() and not force:
        raise ExperimentError('OUTPUT_EXISTS')
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (bytes, bytearray)):
        path.write_bytes(data)
    else:
        path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2) + '\n',
                        encoding='utf-8')


# ---------------------------------------------------------------- configure


def starting_source_files(source: Mapping, base: Path) -> dict:
    # 시작 코드 설정(owned-source.local.json 내용)이 가리키는 커밋을 dev Host와 같은 방법(GitSnapshotBuilder)으로 꺼내
    # {경로: 바이트}로 돌려준다. 에이전트 작업 폴더로 복사되는 것과 같은 파일들이다.
    from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder, GitSnapshotLimits
    path = Path(source['repositoryPath'])
    path = (path if path.is_absolute() else base / path).resolve(strict=True)
    snapshot = GitSnapshotBuilder(path, GitSnapshotLimits(timeout_seconds=float(source.get('gitTimeoutSeconds') or 30))
                                  ).build(source['commitHash'], source['lockPath'])
    return read_snapshot(snapshot.archive)


def configure(tools: Mapping, *, contract: Mapping, suite_ref: Optional[str] = None,
              starting_files: Optional[Mapping] = None, base: Optional[Path] = None) -> dict:
    # Ver1·Ver2 설정 문서를 만든다. 돌려주는 값: {'ver1/owned-tools.local.json': ..., ..., 'experiment.json': ...}
    # 시나리오 정책이 보호 테스트가 전제로 하는 정책과 다르면 만들지 않는다(CONTRACT_NOT_SUPPORTED).
    # base: 원래 도구 설정 파일이 있던 폴더. Host는 dependencyLockFile의 상대 경로를 도구 설정 파일 위치 기준으로 읽으므로,
    #       설정을 다른 폴더(ver1/·ver2/)에 쓸 때 그 경로를 원래 위치 기준의 절대 경로로 바꿔 쓴다.
    suite = protected_tests.load_suite()
    _check_contract(contract)
    if starting_files is not None:
        exposure = protected_tests.exam_exposure(starting_files, suite)
        if exposure:
            raise ExperimentError('STARTING_SOURCE_CONTAINS_EVALUATION_FILES')
    if not isinstance(tools, Mapping) or not isinstance((tools.get('unit') or {}).get('scopes'), list):
        raise ExperimentError('TOOLS_UNIT_SECTION_MISSING')
    suite_ref = suite_ref or protected_tests.loop_suite_ref(suite)
    base_scopes = [scope for scope in tools['unit']['scopes'] if scope.get('kind') != 'PROTECTED'
                   and scope.get('name') != SECURITY_TESTS['name']]
    protected = [protected_tests.unit_test_scope(scope, suite_ref=suite_ref, suite=suite)
                 for scope in (protected_tests.LOOP_SCOPES['QA'], protected_tests.LOOP_SCOPES['SECURITY'])]
    names = [scope['name'] for scope in base_scopes + [SECURITY_TESTS] + protected]
    if len(set(names)) != len(names) or not any(scope.get('kind') == 'QA_TESTS' and not scope.get('role')
                                                for scope in base_scopes):
        raise ExperimentError('TOOLS_UNIT_SCOPES_INVALID')

    lock = tools.get('dependencyLockFile')
    if isinstance(lock, str) and not Path(lock).is_absolute():
        if base is None:
            raise ExperimentError('LOCK_PATH_NEEDS_BASE')
        lock = str((Path(base) / lock).resolve())

    def variant(scopes):
        # 받은 도구 설정을 복사해 unit 범위만 scopes로 바꾼 설정 한 개(Ver1 또는 Ver2)를 만든다.
        document = copy.deepcopy(dict(tools))
        document['unit']['scopes'] = scopes
        if isinstance(lock, str):
            document['dependencyLockFile'] = lock
        # dev와 같은 검사로 unit 설정을 확인한다(크기 64KiB 등). limits가 비어 있는 예시 파일은 Host가 따로 확인한다.
        try:
            from mcp_tools.tools.unit_config import UnitTestScope
            for scope in scopes:
                UnitTestScope(**scope)
        except Exception:
            raise ExperimentError('TOOLS_UNIT_SCOPES_INVALID') from None
        return document
    cases = protected_tests.protected_case_file(contract, suite=suite)
    return {
        'ver1/owned-tools.local.json': variant(base_scopes + [SECURITY_TESTS] + protected),
        'ver1/owned-protected-cases.local.json': cases,
        'ver2/owned-tools.local.json': variant(base_scopes + [SECURITY_TESTS]),
        'experiment.json': {
            'suiteId': suite['suiteId'], 'suiteVersion': suite['suiteVersion'], 'protectedSuiteRef': suite_ref,
            'suiteSha256': {scope: protected_tests.suite_sha256(scope, suite) for scope in protected_tests.SCOPES},
            'startingSourceChecked': starting_files is not None,
            'note': ('Ver1: ver1/owned-tools.local.json과 보호 케이스 파일로 Host를 만든다(protectedTestSuiteRef가 보호 범위에서 '
                     '정해짐). Ver2: ver2/owned-tools.local.json만 쓰고 보호 케이스 파일을 주지 않는다. 시작 코드·모델·한도·'
                     '환경은 두 버전이 같아야 한다. 두 버전 모두 같은 experimentId로 실험 Run을 선언하고 executionOrder·'
                     'warmupState를 적는다(Ver2는 보호 테스트 참조 없이 받음). 시범 실행은 pilot, 공식 비교는 final 시험지로 채점한다.'),
        },
    }


# ---------------------------------------------------------------- score


def read_export(export: Path, version: Optional[int] = None):
    # dev snapshot_export 폴더에서 (Run 정보, tar 바이트, 기준 Manifest)를 읽는다. version이 없으면 최종 코드 버전.
    run = json.loads((export / 'run.json').read_text(encoding='utf-8'))
    version = version or run['finalCodeVersion']
    folder = export / f'v{version}'
    if not (folder / 'source.tar').is_file() or not (folder / 'manifest.json').is_file():
        raise ExperimentError('EXPORT_VERSION_MISSING')
    manifest = ExecutionManifest.from_payload(json.loads((folder / 'manifest.json').read_text(encoding='utf-8')))
    return run, (folder / 'source.tar').read_bytes(), manifest


def score_export(export: Path, *, runner, exam: str, results: Path, contract: Mapping,
                 bundle: Optional[Mapping] = None, trace: Optional[Mapping] = None, a2a: Optional[Mapping] = None,
                 pair_id: Optional[str] = None, loop_policy: Optional[str] = None, architecture: Optional[str] = None,
                 all_versions: bool = False, force: bool = False, known_values=(), additional_outputs=None,
                 agent_export=None, _complete=None) -> Path:
    # 최종 코드 버전을 채점해 <results>/<Run 번호>/에 보관하고 그 폴더를 돌려준다. all_versions면 중간 버전도 채점해
    # Regression을 계산한다(내보내기 폴더에 그 버전들이 있어야 한다: snapshot_export --all-versions).
    # loop_policy: 실험 계획상 이 Run의 버전(IN_LOOP_AND_FINAL=Ver1, FINAL_ONLY=Ver2). compare가 동결 설정과 대조한다.
    # architecture: 보고서에 적을 구조. 주지 않으면 Run 기록에 동결된 구조(없으면 MULTI_AGENT)를 쓰고, 주었는데 동결 구조와
    #               다르면 채점 전에 멈춘다(Single Run을 Multi로 채점해 구조별 비교가 무효가 되지 않게).
    # 같은 Run을 record로 먼저 남겨 두었으면(채점 전) 그 쌍 번호·버전과 Run 기록(bundle을 주지 않았을 때)을 이어받고,
    # 그 폴더는 채점 결과로 바꾼다. 이미 채점한 폴더(output.json 있음)에서는 아무것도 이어받지 않는다(예전 결과와 섞이지 않게).
    _check_contract(contract)  # 컨테이너 채점을 시작하기 전에 정책 일치부터 본다
    run, archive, expected = read_export(Path(export))
    run_id = run['runId']
    target = Path(results) / run_id
    if (target / 'output.json').exists() and not force:
        raise ExperimentError('OUTPUT_EXISTS')  # 컨테이너 채점을 다 돌리기 전에 멈춘다
    scored = (target / 'output.json').exists()
    recorded = {} if scored else _read_meta(target)
    boundary = _closed_automatic_boundary(target)
    if boundary is not None:
        recorded = _read_meta(target)
    if boundary is not None and bundle is None and (target / 'bundle.json').is_file():
        bundle = json.loads((target / 'bundle.json').read_text(encoding='utf-8'))
    if not scored and bundle is None and (target / 'bundle.json').is_file():  # record·fetch로 먼저 받아 둔 Run 기록
        bundle = json.loads((target / 'bundle.json').read_text(encoding='utf-8'))
    _check_automatic_boundary(target, bundle, run=run, expected=expected)
    pair_id = pair_id if pair_id is not None else recorded.get('pairId')
    loop_policy = loop_policy if loop_policy is not None else recorded.get('loopPolicy')
    if loop_policy not in (None, *LOOP_POLICIES):
        raise ExperimentError('LOOP_POLICY_INVALID')
    if bundle is not None and (bundle.get('run') or {}).get('runId') != run_id:
        raise ExperimentError('BUNDLE_RUN_MISMATCH')
    frozen = (((bundle or {}).get('configuration') or {}).get('configuration') or {}).get('architecture')
    if architecture is not None and frozen is not None and architecture != frozen:
        raise ExperimentError('ARCHITECTURE_MISMATCH')
    architecture = architecture or frozen or 'MULTI_AGENT'
    if trace is not None and trace.get('runId') != run_id or a2a is not None and (trace is None or a2a.get('runId') != run_id):
        raise ExperimentError('TRACE_RUN_MISMATCH')  # A2A 본문은 같은 Run의 노출 검사 기록과 함께만 쓴다
    trace_documents = None if trace is None else {**trace, **({A2A_DOCUMENT: a2a} if a2a is not None else {})}
    if additional_outputs is not None:
        if trace_documents is None or additional_outputs.get('runId') != run_id:
            raise ExperimentError('TRACE_RUN_MISMATCH')
        trace_documents['additionalOutputs'] = additional_outputs
    known_values = tuple(known_values)
    if any(not isinstance(value, str) or not value for value in known_values):
        raise ExperimentError('KNOWN_VALUES_INVALID')
    # 결과 한 세트를 임시 폴더에 다 만든 뒤 한꺼번에 바꿔 넣는다. 다시 채점(force)할 때 예전 회귀·버전·Run 기록 파일이
    # 남아 새 결과와 섞이지 않고, 채점 중 실패하면 예전 결과가 그대로 남는다.
    folder = Path(results) / f'.{run_id}.staging-{uuid.uuid4().hex}'
    try:
        _score_into(folder, export, run, archive, expected, runner=runner, exam=exam, contract=contract,
                    bundle=bundle, trace_documents=trace_documents, pair_id=pair_id, loop_policy=loop_policy,
                    architecture=architecture, all_versions=all_versions, known_values=known_values,
                    agent_export=agent_export)
        if _complete is not None:
            _complete(folder)  # workflow 부가 결과도 공개 전에 같은 임시 폴더 안에서 완성한다.
        boundary = boundary or _automatic_boundary(bundle, run=run)
        if boundary is not None:
            # A regrade may replace derived scores, but never erase the fact
            # that these source records close the autonomous segment.
            _write(folder / 'automatic_boundary.json', boundary, False)
        if (target / 'output.json').exists() and not force:
            raise ExperimentError('OUTPUT_EXISTS')
        _check_automatic_boundary(target, bundle, run=run, expected=expected)
        _replace(folder, target)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return target


def _read_meta(folder: Path) -> dict:
    # 보관 폴더의 meta.json(없으면 빈 사전)
    path = Path(folder) / 'meta.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def _automatic_boundary(bundle=None, *, run=None):
    """Paused source evidence closes the automatic segment through every entry point."""
    original = (bundle or {}).get('run')
    if original is None and run is not None:
        original = {'runId': run.get('runId'), 'status': run.get('status'), 'verdict': run.get('verdict'),
                    'codeVersion': run.get('finalCodeVersion')}
    if not isinstance(original, Mapping) or original.get('status') not in ('HUMAN_REVIEW', 'WAITING_INPUT'):
        return None
    boundary = {'schemaVersion': 1, 'runId': original['runId'], 'runStatus': original['status'],
                'automaticSegmentClosed': True}
    if bundle is None:
        # Direct score may have only snapshot_export/run.json. Keep that small
        # identity here so later regrading cannot substitute a resumed snapshot.
        boundary['run'] = original
    return boundary


def _closed_automatic_boundary(folder: Path):
    """Read the immutable segment marker, including older workflow results."""
    for name in ('automatic_boundary.json', 'evaluation_status.json'):
        path = folder / name
        if not path.is_file():
            continue
        record = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(record, dict):
            raise ExperimentError('AUTOMATIC_BOUNDARY_INVALID')
        if record.get('automaticSegmentClosed') is True:
            return {'schemaVersion': 1, 'runId': record['runId'],
                    'runStatus': record['runStatus'], 'automaticSegmentClosed': True,
                    **({'run': record['run']} if 'run' in record else {})}
    # Earlier direct score/record results have no marker. The preserved paused
    # bundle is still evidence of a closed boundary and must not be overwritten.
    path = folder / 'bundle.json'
    if path.is_file():
        return _automatic_boundary(json.loads(path.read_text(encoding='utf-8')))
    return None


def _check_automatic_boundary(folder: Path, bundle, *, run=None, expected=None):
    boundary = _closed_automatic_boundary(folder)
    if boundary is None:
        return None
    path = folder / 'bundle.json'
    previous = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
    if previous != bundle:
        raise ExperimentError('AUTOMATIC_SEGMENT_ALREADY_CLOSED')
    original = previous['run'] if previous is not None else boundary['run']
    if run is not None and any(run.get(key) != original.get(field) for key, field in
                              (('runId', 'runId'), ('status', 'status'), ('verdict', 'verdict'),
                               ('finalCodeVersion', 'codeVersion'))):
        raise ExperimentError('AUTOMATIC_SEGMENT_ALREADY_CLOSED')
    if expected is not None and (folder / 'output.json').is_file():
        old = json.loads((folder / 'output.json').read_text(encoding='utf-8'))['report']['expectedManifest']
        if old != expected.to_payload():
            raise ExperimentError('AUTOMATIC_SEGMENT_ALREADY_CLOSED')
    return boundary


def _replace(folder: Path, target: Path):
    # 새로 만든 결과 폴더(folder)를 target 자리에 한꺼번에 넣는다(예전 폴더는 백업 이름으로 옮겼다가 지운다).
    # 새 폴더를 옮기다 실패하면 예전 폴더를 원래 자리로 되돌리고 RESULT_REPLACE_FAILED로 멈춘다. 되돌리기도 실패하면
    # 백업 폴더(.<Run>.old-…)가 남으므로 RESULT_RESTORE_NEEDED로 멈춘다(compare도 이런 백업을 보면 멈춘다).
    if not target.exists():
        try:
            folder.rename(target)
        except OSError:
            raise ExperimentError('RESULT_REPLACE_FAILED') from None
        return
    old = target.parent / f'.{target.name}.old-{uuid.uuid4().hex}'
    target.rename(old)
    try:
        folder.rename(target)
    except OSError:
        try:
            old.rename(target)
        except OSError:
            raise ExperimentError('RESULT_RESTORE_NEEDED') from None
        raise ExperimentError('RESULT_REPLACE_FAILED') from None
    shutil.rmtree(old, ignore_errors=True)


def _checked_metrics(bundle) -> dict:
    # Run 기록 묶음으로 지표를 계산한다. 모양이 틀렸거나 다른 Run의 자료가 섞였으면 BUNDLE_INVALID로 멈춘다.
    try:
        return run_metrics(bundle)
    except (KeyError, TypeError, ValueError):
        raise ExperimentError('BUNDLE_INVALID') from None


def record_run(results: Path, bundle: Mapping, *, pair_id: Optional[str] = None, loop_policy: Optional[str] = None,
               force: bool = False, _complete=None) -> Path:
    # 채점하지 못한 Run(빌드 실패·중단·Snapshot 없음 등)의 Run 기록을 <results>/<Run 번호>/에 남긴다(bundle.json·meta.json).
    # compare가 이 Run도 실패·중단·비용·토큰 집계에 넣는다(채점 결과는 없음). 이미 채점한 Run은 덮어쓰지 않는다.
    run_id = _checked_metrics(bundle)['runId']  # 묶음 모양·Run 일치 확인
    if loop_policy not in (None, *LOOP_POLICIES):
        raise ExperimentError('LOOP_POLICY_INVALID')
    target = Path(results) / run_id
    if (target / 'output.json').exists():
        raise ExperimentError('ALREADY_SCORED')
    if target.exists() and not force:
        raise ExperimentError('OUTPUT_EXISTS')
    boundary = _check_automatic_boundary(target, bundle)
    if boundary is not None:
        old = _read_meta(target)
        if ((pair_id is not None and pair_id != old.get('pairId'))
                or (loop_policy is not None and loop_policy != old.get('loopPolicy'))):
            raise ExperimentError('AUTOMATIC_SEGMENT_ALREADY_CLOSED')
        return target  # Re-recording identical evidence must retain its closure and diagnostics.
    folder = Path(results) / f'.{run_id}.staging-{uuid.uuid4().hex}'
    try:
        _write(folder / 'bundle.json', bundle, False)
        _write(folder / 'meta.json', {'runId': run_id, 'exam': None, 'pairId': pair_id, 'loopPolicy': loop_policy}, False)
        if _complete is not None:
            _complete(folder)
        boundary = _automatic_boundary(bundle)
        if boundary is not None:
            _write(folder / 'automatic_boundary.json', boundary, False)
        _check_automatic_boundary(target, bundle)
        if target.exists() and not force:
            raise ExperimentError('OUTPUT_EXISTS')
        _replace(folder, target)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return target


def _archive_replay_baseline(results, run_id, output, contract=None):
    """Keep each completed evaluation's replay baseline outside replaceable results."""
    if not (output.get('inputRecord') or {}).get('fileName'):
        return
    evaluation_id = str(uuid.UUID(output['report']['evaluationId']))
    directory = Path(results) / '.evaluation-history' / run_id / evaluation_id
    for path in (directory.parent.parent, directory.parent, directory):
        if path.is_symlink():
            raise ExperimentError('EVALUATION_HISTORY_SYMLINK')
        path.mkdir(exist_ok=True, mode=0o700)
    documents = {'output.json': output}
    if contract is not None:
        documents['contract.json'] = dict(contract)
    for name, document in documents.items():
        path = directory / name
        data = (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
                raise ExperimentError('EVALUATION_HISTORY_CHANGED') from None
            continue
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())


def _score_into(folder, export, run, archive, expected, *, runner, exam, contract, bundle, trace_documents, pair_id,
                loop_policy, architecture, all_versions, known_values=(), agent_export=None):
    # score_export의 채점 본체. 새 폴더(folder)에 결과 한 세트를 쓴다.
    run_id = run['runId']
    # Keep evaluator-only input tapes outside replaceable Run results. A force
    # regrade must not erase the inputs needed to reproduce earlier evaluations.
    private_inputs = folder.parent / '.private-inputs' / run_id
    output = evaluate_final_snapshot(archive=archive, expected=expected, scenario_contract=contract,
                                     source_run_id=run_id, runner=runner, exam=exam, architecture=architecture,
                                     run_bundle=bundle, trace_documents=trace_documents, known_values=known_values,
                                     private_input_dir=private_inputs)
    _archive_replay_baseline(folder.parent, run_id, output, contract)
    regression = None
    reports = [output['report']]
    if all_versions:
        earlier = [version for version in run.get('exportedCodeVersions', []) if version != expected.code_version]
        # 수정 없이 첫 버전(v1)에서 끝난 Run은 비교할 앞 버전이 없는 것이 정상이다. 멈추지 않고 비교 쌍 0개로 남긴다.
        # 최종 버전이 2 이상인데 앞 버전이 하나도 없으면 내보내기가 빠진 것이므로 거부한다.
        if not earlier and expected.code_version != 1:
            raise ExperimentError('EXPORT_HAS_NO_EARLIER_VERSIONS')
        for version in sorted(earlier):
            _run, version_archive, version_expected = read_export(Path(export), version)
            version_output = evaluate_final_snapshot(archive=version_archive, expected=version_expected,
                                                     scenario_contract=contract, source_run_id=run_id, runner=runner,
                                                     exam=exam, architecture=architecture,
                                                     private_input_dir=private_inputs)
            _archive_replay_baseline(folder.parent, run_id, version_output, contract)
            reports.append(version_output['report'])
            _write(folder / 'versions' / f'v{version}.json', reports[-1], False)
            _write(folder / 'version_outputs' / f'v{version}.json', version_output, False)
            from .check_details import write_details
            write_details(folder / 'version_details' / f'v{version}', version_output, _write)
        regression = regression_from_reports(reports)
        _write(folder / 'regression.json', regression, False)
    timing = first_passing_snapshot(bundle, reports)
    _write(folder / 'timing_metrics.json', timing, False)
    from .check_details import write_details
    write_details(folder, output, _write)
    _write(folder / 'output.json', output, False)
    _write(folder / 'meta.json', {'runId': run_id, 'exam': exam, 'pairId': pair_id, 'loopPolicy': loop_policy,
                                  'codeVersion': expected.code_version}, False)
    if bundle is not None:
        _write(folder / 'bundle.json', bundle, False)
    # 버전별 QA·Security 칸은 같은 Snapshot에 연결된 실제 보고서(trace.json)에서, 결과 패키지 저장 여부는 handoff에서 해시까지
    # 확인한 package-completion에서 읽는다(Codex 점검 2026-11-19 #5). 내보내기가 없으면 예전처럼 사건만으로 적는다.
    run_trace = (agent_export or {}).get('trace') or (trace_documents if (trace_documents or {}).get('artifacts') else None)
    pipeline = None
    if agent_export is not None:
        completion = agent_export.get('packageCompletion')
        pipeline = {'runId': run_id, 'packageSaved': bool(
            agent_export.get('packageFilesVerified') is True and isinstance(completion, dict)
            and completion.get('runId') == run_id and completion.get('codeVersion') == expected.code_version)}
    summary = render_summary(output['report'], scenario_contract=contract, run_bundle=bundle, trace=output['trace'],
                             runner=output['runner'], regression=regression, reference_report=output['referenceReport'],
                             supplementary=output['supplementary'], run_trace=run_trace, pipeline=pipeline)
    summary += ('\n- 최초 통과 Snapshot 산출 시간(TIME-05): '
                + (str(timing['firstPassingSnapshotMs']) + 'ms' if timing['firstPassingSnapshotMs'] is not None
                   else str(timing['reason'])) + '\n')
    if output.get('checkDetails') is not None:
        summary += '\n- 세부 검사 결과: [기대값·실제값·항목별 상태](check_details.md) · [JSON](check_details.json)\n'
    _write(folder / 'summary.md', summary, False)
    if agent_export is not None:
        from .agent_eval import agent_tables, render_markdown
        tables = agent_tables(agent_export, contract, output['report'])
        _write(folder / 'agent_tables.json', tables, False)
        _write(folder / 'agent_tables.md', render_markdown(tables), False)


# ---------------------------------------------------------------- compare


def compare(results: Path, planned: Optional[Mapping[str, int]] = None, group_by: str = 'loopPolicy', *, run_journals=(),
            architecture: Optional[str] = None, loop_policy: Optional[str] = None,
            parity_file: Optional[Path] = None) -> dict:
    # <results>/ 아래 Run 폴더들로 Ver1·Ver2(group_by='loopPolicy')를 비교한다. group_by='architecture'면 같은 방안(보호
    # 테스트 조건이 같은 Run들)에서 Single과 Multi를 비교한다(10.29 회의록 방안 1·2의 본래 비교, Single 실행기는 1번 담당).
    # 구조별 비교에서는 차이의 차이(practiceEffect)를 계산하지 않는다. Run 기록(bundle.json)이 있는 폴더는 모두 Run으로 센다:
    #   채점한 Run(output.json 있음)은 점수까지, 채점하지 못한 Run(record_run으로 남김)은 상태·시간·비용·토큰만 집계한다.
    #   (채점 파일이 없다고 Run을 빼면 실패 Run이 "시작 안 함"으로 바뀌고 이미 쓴 비용이 사라진다.)
    # planned: 정책별 계획한 Run 수({'IN_LOOP_AND_FINAL': 5, 'FINAL_ONLY': 5}). 성공률 분모는 시작하지 못했거나 채점하지 못한
    #          Run까지 포함한 계획 Run이어야 하므로 꼭 준다. 주지 않으면 결과 폴더 수로 대신하고 plannedRunsKnown=False로 표시한다.
    # 버전 이름표: meta.json의 loopPolicy(실험 계획상 버전)를 쓰고 동결 설정과 대조한다(어긋나면 비교 무효).
    #   적혀 있지 않으면 동결 설정에서 읽고 주의(INTENDED_LOOP_POLICY_NOT_RECORDED)로 알린다.
    # 읽을 때 다시 확인하는 것(저장된 파일이 손상·혼합되었을 수 있으므로):
    #   - 보고서가 독립 평가 보고서 스키마에 맞고, 같은 Run의 것이다.
    #   - 최종 보고서는 meta의 시험지(pilot·final)를, 참고 보고서는 순환용 시험지(practice)를 지금 묶음 그대로 채점했다
    #     (지문 대조. 두 보고서를 바꿔 넣거나 다른 날의 다른 시험지로 채점한 결과가 섞이면 거부).
    #   - regression.json은 같은 Run·같은 시험지·같은 최종 코드 버전의 것이다.
    #   - Ver1 Run이 개발 중에 받은 순환용 시험지(Run 설정의 보호 테스트 참조)가 참고 채점의 순환용 시험지와 같다.
    # 2×2 결과는 원본 폴더를 옮기지 않고 반대 축 하나를 고정해 읽는다. planned는 선택한 조건의 계획 수다.
    if group_by not in GROUPINGS:
        raise ExperimentError('GROUP_BY_INVALID')
    if (architecture not in (None, *GROUPINGS['architecture']) or loop_policy not in (None, *LOOP_POLICIES)
            or group_by == 'architecture' and architecture is not None
            or group_by == 'loopPolicy' and loop_policy is not None):
        raise ExperimentError('COMPARISON_SELECTION_INVALID')
    from .reporting import validate_contract
    suite = protected_tests.load_suite()
    from .execution_journal import read_execution_journals, validate_bindings
    journal_paths = list(run_journals)
    if (Path(results) / 'runs.jsonl').is_file():
        journal_paths.append(Path(results) / 'runs.jsonl')
    try:
        slots, journal_sources = read_execution_journals(journal_paths)
    except Exception as error:
        raise ExperimentError('EXECUTION_JOURNAL_INVALID') from error
    runs, evaluations, references, entries, exams, regressions, pairs = [], [], [], [], set(), {}, {}
    exam_for_run = {}
    loop_ref = protected_tests.loop_suite_ref(suite)
    unlinked, unlabeled, unscored, rows, quarantined = [], [], [], [], []
    for path in Path(results).iterdir():
        # 결과를 바꾸다 되돌리지 못한 백업(.<Run>.old-…)만 있고 원래 폴더가 없으면, 그 Run이 비교에서 조용히 빠지지 않게 멈춘다.
        if path.name.startswith('.') and '.old-' in path.name and \
                not (Path(results) / path.name[1:].split('.old-', 1)[0]).exists():
            raise ExperimentError('RESULT_RESTORE_NEEDED')
    for folder in sorted(path for path in Path(results).iterdir() if not path.name.startswith('.') and path.is_dir()
                         and ((path / 'bundle.json').is_file() or (path / 'output.json').is_file())):
        if not (folder / 'bundle.json').is_file():
            raise ExperimentError('BUNDLE_MISSING')
        bundle = json.loads((folder / 'bundle.json').read_text(encoding='utf-8'))
        metrics = _checked_metrics(bundle)
        meta = _read_meta(folder)
        if meta.get('runId') not in (None, metrics['runId']):
            raise ExperimentError('REPORT_RUN_MISMATCH')
        intended = meta.get('loopPolicy')
        if intended not in (None, *LOOP_POLICIES):
            raise ExperimentError('LOOP_POLICY_INVALID')
        frozen = metrics['loopPolicy']
        if intended is None:
            unlabeled.append(metrics['runId'])
        # workflow가 남긴 끝 상태 원인·의미 검토 사용량·자동 완료(outcome.json). 예전 결과 폴더에는 없을 수 있다.
        outcome_record = None
        if (folder / 'outcome.json').is_file():
            outcome_record = json.loads((folder / 'outcome.json').read_text(encoding='utf-8'))
            if not isinstance(outcome_record, dict) or outcome_record.get('runId') != metrics['runId']:
                raise ExperimentError('OUTCOME_RUN_MISMATCH')
        metrics = {**metrics, 'loopPolicy': intended or frozen, 'frozenLoopPolicy': frozen, 'outcomeRecord': outcome_record}
        assisted = is_assisted_run(metrics)
        if assisted:
            quarantined.append(metrics['runId'])
        if frozen == 'IN_LOOP_AND_FINAL' and \
                (bundle['configuration'].get('configuration') or {}).get('protectedTestSuiteRef') != loop_ref:
            unlinked.append(metrics['runId'])
        report = None
        if (folder / 'output.json').is_file() and not assisted:
            output = json.loads((folder / 'output.json').read_text(encoding='utf-8'))
            report, reference = output.get('report'), output.get('referenceReport')
            try:
                validate_contract(report, 'independent_evaluation')
                validate_contract(reference, 'independent_evaluation')
            except Exception:
                raise ExperimentError('REPORT_INVALID') from None
            if not report['sourceRunId'] == reference['sourceRunId'] == metrics['runId'] == meta.get('runId'):
                raise ExperimentError('REPORT_RUN_MISMATCH')
            if meta.get('exam') not in ('pilot', 'final') or \
                    report['suite']['sha256'] != protected_tests.suite_sha256(meta['exam'], suite):
                raise ExperimentError('EXAM_SUITE_MISMATCH')
            if reference['suite']['sha256'] != protected_tests.suite_sha256('practice', suite):
                raise ExperimentError('REFERENCE_SUITE_MISMATCH')
            evaluations.append(report)
            references.append(reference)
            exams.add(meta['exam'])
            exam_for_run[metrics['runId']] = meta['exam']
            if (folder / 'regression.json').is_file():
                regression = json.loads((folder / 'regression.json').read_text(encoding='utf-8'))
                if (regression.get('runId') != metrics['runId'] or regression.get('suiteSha256') != report['suite']['sha256']
                        or regression.get('finalCodeVersion') != report['expectedManifest'].get('codeVersion')):
                    raise ExperimentError('REGRESSION_MISMATCH')
                regressions[metrics['runId']] = (metrics[group_by], regression)
        else:
            unscored.append(metrics['runId'])
        version_reports = [report] if report is not None else []
        if report is not None:
            for path in sorted((folder / 'versions').glob('v*.json')):
                version_reports.append(json.loads(path.read_text(encoding='utf-8')))
        # 파생 파일만 믿지 않고 원본 보고서·사건으로 다시 계산한다.
        timing = (first_passing_snapshot(bundle, version_reports) if not assisted else
                  {'status': 'UNVERIFIED', 'reason': 'AUTOMATIC_BOUNDARY_NOT_CAPTURED',
                   'firstPassingSnapshotMs': None})
        process_path = folder / 'process_evidence.json'
        process = process_metrics(metrics['runId'], json.loads(process_path.read_text(encoding='utf-8'))
                                  if process_path.is_file() and not assisted else None)
        metrics = {**metrics, 'firstPassTiming': timing, 'processMetrics': process}
        runs.append(metrics)
        if meta.get('pairId') is not None:
            pairs[metrics['runId']] = str(meta['pairId'])
        entries.append({'architecture': metrics['architecture'], 'loopPolicy': metrics['loopPolicy'],
                        'pairId': meta.get('pairId'), 'configuration': bundle['configuration'],
                        'evaluation': report, 'priceBook': metrics.get('priceBook'),
                        'requestTextSha256': metrics.get('requestTextSha256')})
        # Run마다 한 줄: 실행 결과(verdict·상태)와 채점 여부를 따로 적어 "시작 후 실패·실행 완료 미채점·채점 미검증"을 구분한다.
        rows.append({'runId': metrics['runId'], 'loopPolicy': metrics['loopPolicy'], 'frozenLoopPolicy': frozen,
                     'pairId': meta.get('pairId'), 'workflowStatus': metrics['workflowStatus'],
                     'verdict': metrics['verdict'], 'scoring': 'QUARANTINED_ASSISTED' if assisted else
                     'SCORED' if report is not None else 'NOT_SCORED',
                     'evaluationVerdict': report['evaluationVerdict'] if report is not None else None,
                     'cause': 'ASSISTED_CONTINUATION' if assisted else
                     ((outcome_record or {}).get('outcome') or {}).get('cause'),
                     'autoCompletion': 'FAIL' if assisted else
                     ((outcome_record or {}).get('autoCompletion') or {}).get('status')})
    # Validate the complete join before selecting a cohort: a mismatched Run ID,
    # foreign experiment or contradictory slot must not disappear behind a filter.
    try:
        validate_bindings(slots, entries)
    except Exception as error:
        raise ExperimentError('EXECUTION_JOURNAL_MISMATCH') from error
    selection = {'architecture': architecture, 'loopPolicy': loop_policy,
                 'excludedRunIds': [], 'excludedExecutionSlots': 0}
    if architecture is not None or loop_policy is not None:
        def selected(item):
            # Include contradictory labels on either side of the fixed condition;
            # comparison_validity must reject them instead of silently hiding them.
            return ((architecture is None or item['architecture'] == architecture)
                    and (loop_policy is None or loop_policy in
                         (item['loopPolicy'], item.get('frozenLoopPolicy'))))
        selection['excludedRunIds'] = sorted(item['runId'] for item in runs if not selected(item))
        runs = [item for item in runs if selected(item)]
        ids = {item['runId'] for item in runs}
        evaluations = [report for report in evaluations if report['sourceRunId'] in ids]
        references = [report for report in references if report['sourceRunId'] in ids]
        entries = [entry for entry in entries if entry['configuration']['runId'] in ids]
        rows = [row for row in rows if row['runId'] in ids]
        regressions = {run_id: value for run_id, value in regressions.items() if run_id in ids}
        pairs = {run_id: value for run_id, value in pairs.items() if run_id in ids}
        unlinked, unlabeled, unscored, quarantined = (
            [run_id for run_id in values if run_id in ids]
            for values in (unlinked, unlabeled, unscored, quarantined))
        exams = {exam_for_run[run_id] for run_id in ids if run_id in exam_for_run}
        retained_slots = [slot for slot in slots if selected(slot)]
        selection['excludedExecutionSlots'] = len(slots) - len(retained_slots)
        slots = retained_slots
    if not runs and not planned:
        raise ExperimentError('NO_RESULTS')
    if len(exams) > 1:
        raise ExperimentError('MIXED_EXAMS')  # 시범(pilot)과 공식(final) 채점을 한 비교에 섞지 않는다
    observed = {}
    for item in runs:
        observed[item[group_by]] = observed.get(item[group_by], 0) + 1
    planned_known = planned is not None
    planned = dict(planned) if planned_known else observed
    if (set(planned) - set(GROUPINGS[group_by]) or any(type(n) is not int or n < observed.get(policy, 1)
                                                 for policy, n in planned.items())
            or set(observed) - set(planned)):
        raise ExperimentError('PLANNED_RUNS_INVALID')  # 계획보다 결과가 많거나 계획에 없는 정책의 결과가 있음
    try:
        validate_bindings(slots, entries)
        if any(slot[group_by] not in planned for slot in slots):
            raise ValueError('UNPLANNED_EXECUTION_SLOT')
    except Exception as error:
        raise ExperimentError('EXECUTION_JOURNAL_MISMATCH') from error
    recorded_ids = {item['runId'] for item in runs}
    missing_slots = [slot for slot in slots if slot['runId'] not in recorded_ids]
    rows.extend({**{k: slot[k] for k in ('runId', 'architecture', 'loopPolicy', 'workflowStatus')},
                 'executionOrder': slot['order'], 'experimentId': slot['experimentId'],
                 'scoring': 'RESULT_NOT_COLLECTED' if slot['runId'] else 'SUBMISSION_UNCONFIRMED',
                 'cause': slot['reason'] or slot['state'], 'verdict': None,
                 'evaluationVerdict': None, 'autoCompletion': None} for slot in missing_slots)
    regression = {}
    for policy in planned:
        mine = [item for owner, item in regressions.values() if owner == policy]
        counts = [item['regressionCount'] for item in mine]
        regression[policy] = {'runsWithVersions': len(mine), 'totalRegressions': sum(counts) if counts else None,
                              'incompleteRuns': sum(1 for item in mine if not item.get('complete'))}
    try:
        validity = comparison_validity(entries, group_by=group_by) if len(observed) == 2 else None
        summary = experiment_summary(runs, planned, evaluations, group_by=group_by, execution_slots=slots)
        effect = (loop_policy_effect(runs, evaluations, references, pairs=pairs, unscored=unscored)
                  if group_by == 'loopPolicy' else None)
        # STAB-05: 구조 비교에서는 같은 쌍(pairId)으로 함께 실행한 Single·Multi의 차이를 따로 낸다.
        pair_comparison = architecture_pairs(runs, evaluations, pairs) if group_by == 'architecture' else None
    except ValueError:
        # 보관한 결과끼리 맞지 않음(예: Run의 최종 코드 버전이 아닌 버전을 채점한 보고서). 명령줄에 짧은 코드로 알린다.
        raise ExperimentError('RESULTS_INCONSISTENT') from None
    parity = _tool_parity(Path(results), [item['runId'] for item in runs], parity_file=parity_file)
    if validity is not None:
        _outcome_warnings(validity, runs)
        if quarantined:
            validity['warnings'].append({'field': 'automaticSegment', 'detail': 'ASSISTED_RESULTS_QUARANTINED',
                                         'runs': sorted(quarantined)})
        if parity['status'] == 'FAIL':
            # 개발정의서 11-A "Single만 Tool 적게 → 비교 무효"
            validity['violations'].append({'field': 'toolParity', 'severity': 'INVALID_COMPARISON',
                                           'values': {'failedChecks': parity.get('failedChecks', [])}})
            validity['valid'] = False
        elif parity['status'] != 'PASS':
            validity['warnings'].append({'field': 'toolParity', 'detail': parity['status'], 'runs': []})
    if validity is not None and unlinked:
        # 개발 중에 준 순환용 시험지를 Run 기록으로 확인할 수 없으면 차이의 차이(보조 지표)의 전제가 깨진다.
        validity['violations'].append({'field': 'protectedTestSuiteRef.practice', 'severity': 'INVALID_COMPARISON',
                                       'values': {'expected': loop_ref, 'runs': sorted(unlinked)}})
        validity['valid'] = False
    if validity is not None and unlabeled and group_by == 'loopPolicy':
        validity['warnings'].append({'field': 'loopPolicy', 'detail': 'INTENDED_LOOP_POLICY_NOT_RECORDED',
                                     'runs': sorted(unlabeled)})
    blockers = []
    if not planned_known:
        blockers.append('PLANNED_RUNS_UNKNOWN')
    if validity is None or not validity.get('valid') or validity.get('violations'):
        blockers.append('COMPARISON_CONDITIONS_NOT_VERIFIED')
    # 구조 비교(Single·Multi)와 버전 비교(Ver1·Ver2) 모두 실제 Host 한도·도구 설정 동일 확인과 Run별 고정 정책 확인이
    # 있어야 공식 비교로 본다(점검 5). parity.json은 이 비교 기준으로 만든 것이어야 한다.
    if parity['status'] != 'PASS':
        blockers.append('TOOL_AND_LIMIT_PARITY_NOT_VERIFIED')
    if not parity.get('pinnedRunsVerified'):
        blockers.append('RUN_POLICY_BINDINGS_NOT_VERIFIED')
    if parity['status'] != 'NOT_CHECKED' and parity.get('groupBy') != group_by:
        blockers.append('PARITY_REPORT_FOR_OTHER_COMPARISON')
    if quarantined:
        blockers.append('ASSISTED_CONTINUATION_REQUIRES_SEPARATE_EXPERIMENT')
    if any(item.get('requestTextSha256') is None for item in runs):
        blockers.append('REQUEST_TEXT_NOT_VERIFIED')
    if any(group['unrecordedRuns'] for group in summary.values()):
        blockers.append('PLANNED_SLOT_RESULTS_NOT_COLLECTED')
    execution_ready = {'status': 'PASS' if not blockers else 'UNVERIFIED', 'blockers': list(blockers)}
    if any(group['evaluation']['eligibleRunsMissingGrade'] for group in summary.values()):
        blockers.append('FINAL_CODE_NOT_GRADED')
    return {'exam': exams.pop() if exams else None, 'groupBy': group_by, 'plannedRuns': planned, 'selection': selection,
            'readiness': {'status': 'PASS' if not blockers else 'UNVERIFIED', 'blockers': blockers},
            'executionReadiness': execution_ready, 'plannedRunsKnown': planned_known,
            'validity': validity, 'runs': rows, 'unscoredRuns': sorted(unscored),
            'quarantinedAssistedRuns': sorted(quarantined),
            'executionSlots': slots, 'executionJournalSources': journal_sources,
            'summary': summary, 'practiceEffect': effect, 'pairComparison': pair_comparison,
            'toolParity': parity, 'regression': regression}


def _outcome_warnings(validity, runs):
    # 끝 상태 원인으로 비교 해석에 주의할 Run을 알린다(판정을 바꾸지 않는다).
    def runs_where(test):
        return sorted(item['runId'] for item in runs if test(item.get('outcomeRecord')))
    validity_of = lambda record: ((record or {}).get('outcome') or {}).get('comparisonValidity')
    for detail, found in (('ENVIRONMENT_FAILURE_RUNS', runs_where(lambda record: validity_of(record) == 'UNVERIFIED')),
                          ('OUTCOME_REVIEW_REQUIRED', runs_where(lambda record: validity_of(record) == 'REVIEW_REQUIRED')),
                          ('OUTCOME_NOT_RECORDED', runs_where(lambda record: record is None))):
        if found:
            validity['warnings'].append({'field': 'outcome', 'detail': detail, 'runs': found})


def _tool_parity(results: Path, run_ids=(), *, parity_file: Optional[Path] = None) -> dict:
    # <results>/parity.json(evaluation.parity가 쓴 도구·LLM 한도 동일 확인)을 읽는다. 없으면 NOT_CHECKED.
    path = Path(parity_file) if parity_file is not None else results / 'parity.json'
    if not path.is_file():
        return {'status': 'NOT_CHECKED', 'source': None}
    try:
        report = json.loads(path.read_text(encoding='utf-8'))
        status = report['status']
        if status not in ('PASS', 'FAIL', 'UNVERIFIED'):
            raise ValueError
        checks = report['checks']
        if (not isinstance(checks, list) or not checks or any(not isinstance(item, dict)
                or item.get('status') not in ('PASS', 'FAIL', 'UNVERIFIED') for item in checks)):
            raise ValueError
        states = [item['status'] for item in checks]
        if status != ('FAIL' if 'FAIL' in states else 'UNVERIFIED' if 'UNVERIFIED' in states else 'PASS'):
            raise ValueError
        if any(item.get('check') == 'pinnedPolicy' and not isinstance(item.get('runId'), str) for item in checks):
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError):
        raise ExperimentError('PARITY_REPORT_INVALID') from None
    failed = [item.get('check') for item in report.get('checks', []) if item.get('status') == 'FAIL']
    pinned = [item for item in checks if item.get('check') == 'pinnedPolicy']
    matched = [item.get('runId') for item in pinned if item['status'] == 'PASS']
    verified = (report.get('pinnedPolicyChecked') is True and bool(run_ids)
                and len(matched) == len(pinned) == len(set(matched)) == len(set(run_ids))
                and set(matched) == set(run_ids))
    group_by = report.get('groupBy', 'architecture')  # groupBy가 없는 예전 보고서는 구조 비교용이다
    return {'status': status, 'source': path.name, 'failedChecks': failed, 'pinnedRunsVerified': verified,
            'groupBy': group_by}


# ---------------------------------------------------------------- 명령줄


def main(argv=None):
    # 명령줄 진입점. 실패는 코드 한 개만 stderr에 쓰고 종료 코드 1을 돌려준다(경로·비밀값은 쓰지 않음).
    parser = argparse.ArgumentParser(prog='python -m evaluation.experiment')
    commands = parser.add_subparsers(dest='command', required=True)
    item = commands.add_parser('configure', help='Ver1·Ver2 Host 설정 쌍 만들기')
    item.add_argument('--tools', required=True, type=Path)
    item.add_argument('--source', type=Path, help='시작 코드 설정(owned-source.local.json). 주면 시험지 섞임을 먼저 확인')
    item.add_argument('--out', required=True, type=Path)
    item.add_argument('--suite-ref', help='보호 테스트 참조(기본: 순환용 시험지 지문을 담은 참조. 바꾸면 compare가 대조하지 못해 비교 무효)')
    item.add_argument('--contract')
    item.add_argument('--force', action='store_true')
    item = commands.add_parser('fetch', help='dev Run 기록·노출 검사용 기록 받기')
    item.add_argument('--api', required=True)
    item.add_argument('--run-id', required=True)
    item.add_argument('--out', required=True, type=Path, help='bundle.json·trace.json을 쓸 폴더')
    item = commands.add_parser('score', help='내보낸 Snapshot을 D5로 채점해 보관')
    item.add_argument('--export', required=True, type=Path)
    item.add_argument('--exam', required=True, choices=('pilot', 'final'))
    item.add_argument('--image', required=True, help='해시로 고정한 평가 이미지(repo@sha256:... 또는 sha256:...)')
    item.add_argument('--results', required=True, type=Path)
    item.add_argument('--bundle', type=Path)
    item.add_argument('--trace', type=Path, help='fetch가 받은 trace.json(Run 기록 노출 검사)')
    item.add_argument('--a2a', type=Path, help='dev a2a_export가 꺼낸 A2A Message 본문(--trace와 함께)')
    item.add_argument('--pair-id')
    item.add_argument('--loop-policy', choices=LOOP_POLICIES, help='실험 계획상 이 Run의 버전(Ver1·Ver2)')
    item.add_argument('--architecture', choices=GROUPINGS['architecture'],
                      help='보고서에 적을 구조(기본: Run 기록에 동결된 구조, 없으면 MULTI_AGENT)')
    item.add_argument('--all-versions', action='store_true', help='중간 코드 버전도 채점해 Regression 계산')
    item.add_argument('--secrets-file', type=Path, help='평가용 비밀값 JSON 배열 파일(결과에 복사하지 않음)')
    item.add_argument('--output-manifest', type=Path, help='추가로 검사할 출력 파일 목록 JSON')
    item.add_argument('--output-root', type=Path, help='출력 목록의 상대 경로 기준 폴더')
    item.add_argument('--contract')
    item.add_argument('--force', action='store_true')
    item = commands.add_parser('record', help='채점하지 못한 Run의 기록만 보관(실패·중단·비용 집계용)')
    item.add_argument('--bundle', required=True, type=Path, help='fetch가 받은 bundle.json')
    item.add_argument('--results', required=True, type=Path)
    item.add_argument('--pair-id')
    item.add_argument('--loop-policy', choices=LOOP_POLICIES)
    item.add_argument('--force', action='store_true')
    item = commands.add_parser('compare', help='보관한 결과로 Ver1·Ver2 비교')
    item.add_argument('--results', required=True, type=Path)
    item.add_argument('--planned', action='append', default=[], metavar='GROUP=N',
                      help='조건별 계획 Run 수(IN_LOOP_AND_FINAL=5 FINAL_ONLY=5, 구조별이면 MULTI_AGENT=5 SINGLE_AGENT=5). 성공률 분모')
    item.add_argument('--group-by', choices=tuple(GROUPINGS), default='loopPolicy',
                      help='loopPolicy(Ver1·Ver2, 기본) 또는 architecture(같은 방안의 Single·Multi)')
    item.add_argument('--architecture', choices=GROUPINGS['architecture'],
                      help='group-by loopPolicy에서 비교할 구조 하나를 고정한다')
    item.add_argument('--loop-policy', choices=LOOP_POLICIES,
                      help='group-by architecture에서 비교할 버전 하나를 고정한다')
    item.add_argument('--parity', type=Path, help='선택한 비교 조건·Run들의 evaluation.parity 보고서')
    item.add_argument('--out', type=Path)
    item.add_argument('--run-journal', action='append', default=[], type=Path,
                      help='측정 실행 runs.jsonl 또는 그 폴더. 반복 지정 가능. 미시작과 결과 미수집을 구분')
    options = parser.parse_args(argv)
    try:
        if options.command == 'configure':
            tools = json.loads(options.tools.read_text(encoding='utf-8'))
            starting = None
            if options.source:
                source = json.loads(options.source.read_text(encoding='utf-8'))
                starting = starting_source_files(source, options.source.parent)
            documents = configure(tools, contract=_contract(options.contract), suite_ref=options.suite_ref,
                                  starting_files=starting, base=options.tools.resolve().parent)
            for name, document in documents.items():
                _write(options.out / name, document, options.force)
                print(f'{name}: {options.out / name}')
        elif options.command == 'fetch':
            import httpx
            # 실제 Host(dev 83번)는 API마다 서버 전용 Bearer 토큰을 요구한다. 토큰은 환경 변수에서만 읽는다.
            token = os.environ.get('A2A_HOST_API_TOKEN')
            headers = {'Authorization': f'Bearer {token}'} if token else {}
            with httpx.Client(base_url=options.api, timeout=30, headers=headers) as client:
                def get(path, params=None):
                    # dev Run 조회 API 한 번 부르기(오류 응답이면 예외)
                    response = client.get(path, params=params)
                    response.raise_for_status()
                    return response.json()
                bundle = fetch_run_bundle(get, options.run_id)
                trace = fetch_trace_documents(get, options.run_id)
            _write(options.out / 'bundle.json', bundle, True)
            _write(options.out / 'trace.json', trace, True)
            print(f'bundle: {options.out / "bundle.json"}\ntrace: {options.out / "trace.json"}')
        elif options.command == 'score':
            from .output_evidence import read_known_values, collect_output_evidence
            read = lambda path: json.loads(path.read_text(encoding='utf-8')) if path else None
            bundle, trace, a2a = read(options.bundle), read(options.trace), read(options.a2a)
            try:
                known = read_known_values(options.secrets_file) if options.secrets_file else ()
                if bool(options.output_manifest) != bool(options.output_root):
                    raise ValueError('OUTPUT_ROOT_AND_MANIFEST_REQUIRED')
                extra = (collect_output_evidence(options.output_root, read(options.output_manifest),
                                                 read(options.export / 'run.json')['runId'])
                         if options.output_manifest else None)
            except (OSError, ValueError, KeyError):
                raise ExperimentError('OUTPUT_EVIDENCE_INPUT_INVALID') from None
            folder = score_export(options.export, runner=DockerRunner(options.image), exam=options.exam,
                                  results=options.results, contract=_contract(options.contract), bundle=bundle,
                                  trace=trace, a2a=a2a,
                                  pair_id=options.pair_id, loop_policy=options.loop_policy, architecture=options.architecture,
                                  all_versions=options.all_versions, force=options.force,
                                  known_values=known, additional_outputs=extra)
            print(f'results: {folder}')
        elif options.command == 'record':
            folder = record_run(options.results, json.loads(options.bundle.read_text(encoding='utf-8')),
                                pair_id=options.pair_id, loop_policy=options.loop_policy, force=options.force)
            print(f'recorded: {folder}')
        else:
            planned = {}
            for item in options.planned:
                policy, _separator, count = item.partition('=')
                if not count.isdigit() or policy in planned:
                    raise ExperimentError('PLANNED_ARGUMENT_INVALID')
                planned[policy] = int(count)
            result = compare(options.results, planned or None, group_by=options.group_by, run_journals=options.run_journal,
                             architecture=options.architecture, loop_policy=options.loop_policy, parity_file=options.parity)
            out = options.out or options.results
            _write(out / 'comparison.json', result, True)
            from .comparison_report import render_comparison
            _write(out / 'comparison.md', render_comparison(result), True)
            print(f'comparison: {out / "comparison.json"}')
    except ExperimentError as error:
        sys.stderr.write(error.code + '\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
