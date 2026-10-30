# Ver1·Ver2 실험을 준비하고 결과를 모으는 운영 도구(4번 담당, 10.29 회의 결정).
#   python -m evaluation.experiment configure --tools <owned-tools.local.json> --out <폴더> [--source <owned-source.local.json>]
#   python -m evaluation.experiment fetch     --api http://127.0.0.1:8000 --run-id <Run> --out <폴더>   (bundle.json·trace.json)
#   python -m evaluation.experiment score     --export <snapshot_export 폴더> --exam final --image <repo@sha256:...>
#                                             --results <폴더> [--bundle <bundle.json>] [--trace <trace.json>]
#                                             [--a2a <dev a2a_export 결과>] [--pair-id <쌍 번호>] [--all-versions]
#   python -m evaluation.experiment compare   --results <폴더> [--out <폴더>]
#
# configure: Host 도구 설정(owned-tools.local.json)에 QA·Security 자체 테스트 범위를 두고, Ver1에는 순환용 보호 범위 두 개와
#            보호 케이스 파일을 더한 설정 쌍을 만든다(ver1/·ver2/). 두 버전의 차이는 보호 테스트뿐이다. 시작 코드 설정을
#            주면 그 커밋을 dev와 같은 방식으로 꺼내 시험지·평가 코드가 섞였는지 먼저 확인하고, 섞였으면 만들지 않는다.
# fetch    : dev Run 조회 API에서 Run 기록(run_metrics.fetch_run_bundle)과 노출 검사용 기록(trace_checks.fetch_trace_documents)을
#            받아 bundle.json·trace.json으로 저장한다. A2A Message 본문은 API로 받을 수 없어 dev a2a_export로 따로 꺼낸다.
# score    : dev snapshot_export가 꺼낸 Snapshot(source.tar·manifest.json)을 D5로 채점하고 결과·요약을 Run별 폴더에 보관한다.
#            --all-versions면 중간 코드 버전도 같은 시험지로 채점해 Regression(수정으로 새로 생긴 실패)을 계산한다.
#            버전별 결과는 개발 순환에 전달하지 않는다. 실제 생성 코드는 DockerRunner(해시로 고정한 이미지)로만 실행한다.
# compare  : 보관한 Run들로 Ver1·Ver2 비교 가능 여부, 정책별 요약(점수·시간·토큰), 연습 효과를 계산해 저장한다.
#
# [처음 읽는 분을 위한 용어]
# - 쌍 번호(pair id): 같은 조건·같은 시간대에 함께 실행한 Ver1·Ver2 한 쌍을 묶는 이름. 비교 검사가 쌍마다 둘이 있는지 본다.
# - 결과 폴더: <results>/<Run 번호>/ 아래에 output.json(D5 결과), summary.md(요약), meta.json(쌍 번호·시험지), bundle.json(Run 기록),
#   (--all-versions면) versions/v<N>.json(중간 버전 보고서)·regression.json. 권장 위치는 저장소의
#   experiments/scn001/<시험지>/<날짜-이름>/ 이고(커밋), 내보낸 Snapshot tar는 커밋하지 않는다(.gitignore: experiments/**/export/).
#   결과 폴더는 시작 코드에 섞이면 안 된다(protected_tests.exam_exposure가 함께 찾는다).
import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import protected_tests
from .final_evaluation import DockerRunner, evaluate_final_snapshot, read_snapshot
from .manifest import ExecutionManifest
from .run_metrics import (comparison_validity, experiment_summary, fetch_run_bundle, loop_policy_effect, regression_from_reports,
                          run_metrics)
from .summary import render_summary
from .trace_checks import A2A_DOCUMENT, fetch_trace_documents

DEFAULT_SUITE_REF = 'artifact://protected-tests/scn-001/signup-{version}'
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


def _write(path: Path, data, force: bool):
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
              starting_files: Optional[Mapping] = None) -> dict:
    # Ver1·Ver2 설정 문서를 만든다. 돌려주는 값: {'ver1/owned-tools.local.json': ..., ..., 'experiment.json': ...}
    suite = protected_tests.load_suite()
    if starting_files is not None:
        exposure = protected_tests.exam_exposure(starting_files, suite)
        if exposure:
            raise ExperimentError('STARTING_SOURCE_CONTAINS_EVALUATION_FILES')
    if not isinstance(tools, Mapping) or not isinstance((tools.get('unit') or {}).get('scopes'), list):
        raise ExperimentError('TOOLS_UNIT_SECTION_MISSING')
    suite_ref = suite_ref or DEFAULT_SUITE_REF.format(version=suite['suiteVersion'])
    base_scopes = [scope for scope in tools['unit']['scopes'] if scope.get('kind') != 'PROTECTED'
                   and scope.get('name') != SECURITY_TESTS['name']]
    protected = [protected_tests.unit_test_scope(scope, suite_ref=suite_ref, suite=suite)
                 for scope in (protected_tests.LOOP_SCOPES['QA'], protected_tests.LOOP_SCOPES['SECURITY'])]
    names = [scope['name'] for scope in base_scopes + [SECURITY_TESTS] + protected]
    if len(set(names)) != len(names) or not any(scope.get('kind') == 'QA_TESTS' and not scope.get('role')
                                                for scope in base_scopes):
        raise ExperimentError('TOOLS_UNIT_SCOPES_INVALID')

    def variant(scopes):
        document = copy.deepcopy(dict(tools))
        document['unit']['scopes'] = scopes
        # dev와 같은 검사로 unit 설정을 확인한다(크기 64KiB 등). limits가 비어 있는 예시 파일은 Host가 따로 확인한다.
        try:
            from mcp_tools.tools.unit_config import UnitTestScope
            for scope in scopes:
                UnitTestScope(**scope)
        except Exception:
            raise ExperimentError('TOOLS_UNIT_SCOPES_INVALID') from None
        return document
    cases = {'schemaVersion': 1, 'cases': [case for role in ('QA', 'SECURITY')
                                           for case in protected_tests.owned_cases(contract, role, suite)['cases']]}
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
                     '환경은 두 버전이 같아야 한다. 시범 실행은 pilot, 공식 비교는 final 시험지로 채점한다.'),
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
                 pair_id: Optional[str] = None, architecture: str = 'MULTI_AGENT', all_versions: bool = False,
                 force: bool = False) -> Path:
    # 최종 코드 버전을 채점해 <results>/<Run 번호>/에 보관하고 그 폴더를 돌려준다. all_versions면 중간 버전도 채점해
    # Regression을 계산한다(내보내기 폴더에 그 버전들이 있어야 한다: snapshot_export --all-versions).
    run, archive, expected = read_export(Path(export))
    run_id = run['runId']
    if bundle is not None and (bundle.get('run') or {}).get('runId') != run_id:
        raise ExperimentError('BUNDLE_RUN_MISMATCH')
    if trace is not None and trace.get('runId') != run_id or a2a is not None and (trace is None or a2a.get('runId') != run_id):
        raise ExperimentError('TRACE_RUN_MISMATCH')  # A2A 본문은 같은 Run의 노출 검사 기록과 함께만 쓴다
    trace_documents = None if trace is None else {**trace, **({A2A_DOCUMENT: a2a} if a2a is not None else {})}
    output = evaluate_final_snapshot(archive=archive, expected=expected, scenario_contract=contract,
                                     source_run_id=run_id, runner=runner, exam=exam, architecture=architecture,
                                     run_bundle=bundle, trace_documents=trace_documents)
    folder = Path(results) / run_id
    regression = None
    if all_versions:
        earlier = [version for version in run.get('exportedCodeVersions', []) if version != expected.code_version]
        if not earlier:
            raise ExperimentError('EXPORT_HAS_NO_EARLIER_VERSIONS')
        reports = [output['report']]
        for version in sorted(earlier):
            _run, version_archive, version_expected = read_export(Path(export), version)
            reports.append(evaluate_final_snapshot(archive=version_archive, expected=version_expected,
                                                   scenario_contract=contract, source_run_id=run_id, runner=runner,
                                                   exam=exam, architecture=architecture)['report'])
            _write(folder / 'versions' / f'v{version}.json', reports[-1], force)
        regression = regression_from_reports(reports)
        _write(folder / 'regression.json', regression, force)
    _write(folder / 'output.json', output, force)
    _write(folder / 'meta.json', {'runId': run_id, 'exam': exam, 'pairId': pair_id,
                                  'codeVersion': expected.code_version}, force)
    if bundle is not None:
        _write(folder / 'bundle.json', bundle, force)
    summary = render_summary(output['report'], scenario_contract=contract, run_bundle=bundle, trace=output['trace'],
                             runner=output['runner'], regression=regression, reference_report=output['referenceReport'],
                             supplementary=output['supplementary'])
    _write(folder / 'summary.md', summary, force)
    return folder


# ---------------------------------------------------------------- compare


def compare(results: Path) -> dict:
    # <results>/ 아래 Run 폴더들(output.json·meta.json·bundle.json)로 Ver1·Ver2를 비교한다.
    runs, evaluations, references, entries, exams, regressions = [], [], [], [], set(), {}
    for folder in sorted(path for path in Path(results).iterdir() if (path / 'output.json').is_file()):
        output = json.loads((folder / 'output.json').read_text(encoding='utf-8'))
        meta = json.loads((folder / 'meta.json').read_text(encoding='utf-8'))
        if not (folder / 'bundle.json').is_file():
            raise ExperimentError('BUNDLE_MISSING')
        bundle = json.loads((folder / 'bundle.json').read_text(encoding='utf-8'))
        metrics = run_metrics(bundle)
        runs.append(metrics)
        evaluations.append(output['report'])
        references.append(output['referenceReport'])
        exams.add(meta['exam'])
        if (folder / 'regression.json').is_file():
            regressions[metrics['runId']] = (metrics['loopPolicy'],
                                             json.loads((folder / 'regression.json').read_text(encoding='utf-8')))
        entries.append({'architecture': metrics['architecture'], 'loopPolicy': metrics['loopPolicy'],
                        'pairId': meta.get('pairId'), 'configuration': bundle['configuration'],
                        'evaluation': output['report']})
    if not runs:
        raise ExperimentError('NO_RESULTS')
    if len(exams) != 1:
        raise ExperimentError('MIXED_EXAMS')  # 시범(pilot)과 공식(final) 채점을 한 비교에 섞지 않는다
    planned = {}
    for item in runs:
        planned[item['loopPolicy']] = planned.get(item['loopPolicy'], 0) + 1
    regression = {}
    for policy in planned:
        mine = [item for owner, item in regressions.values() if owner == policy]
        counts = [item['regressionCount'] for item in mine]
        regression[policy] = {'runsWithVersions': len(mine), 'totalRegressions': sum(counts) if counts else None,
                              'incompleteRuns': sum(1 for item in mine if not item.get('complete'))}
    return {'exam': exams.pop(),
            'validity': comparison_validity(entries, group_by='loopPolicy') if len(planned) == 2 else None,
            'summary': experiment_summary(runs, planned, evaluations, group_by='loopPolicy'),
            'practiceEffect': loop_policy_effect(runs, evaluations, references),
            'regression': regression}


# ---------------------------------------------------------------- 명령줄


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m evaluation.experiment')
    commands = parser.add_subparsers(dest='command', required=True)
    item = commands.add_parser('configure', help='Ver1·Ver2 Host 설정 쌍 만들기')
    item.add_argument('--tools', required=True, type=Path)
    item.add_argument('--source', type=Path, help='시작 코드 설정(owned-source.local.json). 주면 시험지 섞임을 먼저 확인')
    item.add_argument('--out', required=True, type=Path)
    item.add_argument('--suite-ref')
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
    item.add_argument('--all-versions', action='store_true', help='중간 코드 버전도 채점해 Regression 계산')
    item.add_argument('--contract')
    item.add_argument('--force', action='store_true')
    item = commands.add_parser('compare', help='보관한 결과로 Ver1·Ver2 비교')
    item.add_argument('--results', required=True, type=Path)
    item.add_argument('--out', type=Path)
    options = parser.parse_args(argv)
    try:
        if options.command == 'configure':
            tools = json.loads(options.tools.read_text(encoding='utf-8'))
            starting = None
            if options.source:
                source = json.loads(options.source.read_text(encoding='utf-8'))
                starting = starting_source_files(source, options.source.parent)
            documents = configure(tools, contract=_contract(options.contract), suite_ref=options.suite_ref,
                                  starting_files=starting)
            for name, document in documents.items():
                _write(options.out / name, document, options.force)
                print(f'{name}: {options.out / name}')
        elif options.command == 'fetch':
            import httpx
            with httpx.Client(base_url=options.api, timeout=30) as client:
                def get(path, params=None):
                    response = client.get(path, params=params)
                    response.raise_for_status()
                    return response.json()
                bundle = fetch_run_bundle(get, options.run_id)
                trace = fetch_trace_documents(get, options.run_id)
            _write(options.out / 'bundle.json', bundle, True)
            _write(options.out / 'trace.json', trace, True)
            print(f'bundle: {options.out / "bundle.json"}\ntrace: {options.out / "trace.json"}')
        elif options.command == 'score':
            read = lambda path: json.loads(path.read_text(encoding='utf-8')) if path else None
            bundle, trace, a2a = read(options.bundle), read(options.trace), read(options.a2a)
            folder = score_export(options.export, runner=DockerRunner(options.image), exam=options.exam,
                                  results=options.results, contract=_contract(options.contract), bundle=bundle,
                                  trace=trace, a2a=a2a,
                                  pair_id=options.pair_id, all_versions=options.all_versions, force=options.force)
            print(f'results: {folder}')
        else:
            result = compare(options.results)
            out = options.out or options.results
            _write(out / 'comparison.json', result, True)
            print(f'comparison: {out / "comparison.json"}')
    except ExperimentError as error:
        sys.stderr.write(error.code + '\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
