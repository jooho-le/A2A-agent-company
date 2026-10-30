"""experiment.py(Ver1·Ver2 실험 준비·채점 보관·비교 도구)를 시험한다.

확인하는 것:
  - configure가 Ver1(자체 테스트 + 순환용 보호 범위 두 개 + 보호 케이스)과 Ver2(자체 테스트만) 설정 쌍을 만들고,
    dev가 그 unit 설정과 보호 케이스 짝을 받아 주는가? 두 버전의 차이는 보호 테스트뿐인가?
  - 시작 코드 커밋에 시험지·평가 코드가 섞였으면(dev와 같은 방식으로 꺼내 확인) 설정을 만들지 않는가?
  - score가 snapshot_export 결과를 D5로 채점해 Run별 폴더에 보관하고, compare가 Ver1·Ver2 비교·연습 효과를 내는가?
  - 모든 버전 채점으로 Regression(수정으로 새로 생긴 실패)을 계산하는가? 저장소에 보관한 결과(experiments/)의 모양이 맞는가?
"""
import json
import subprocess

import pytest

from evaluation import protected_tests as pt
from pathlib import Path

from evaluation.experiment import ExperimentError, compare, configure, score_export, starting_source_files
from evaluation.manifest import ExecutionManifest
from mcp_tools.tools.unit_config import decode_unit_configuration
from orchestrator.domain.states import AgentRole
from tests.evaluation import signup_service
from tests.evaluation.test_final_evaluation import (CONTRACT, FIXTURE_RUNS, LOCK, RUNTIME, VerifiedImageRunner,
                                                    canonical_tar, manifest)
from tests.evaluation.test_run_metrics import bundle as run_bundle, configuration, scored_report, uid

TOOLS = {'schemaVersion': 1, 'unit': {'scopes': [
    {'name': 'developer-unit', 'kind': 'SNAPSHOT', 'source_directory': 'tests', 'pattern': 'test_*.py'},
    {'name': 'qa-unit', 'kind': 'QA_TESTS', 'source_directory': 'tests', 'pattern': 'test_*.py'}]}}


def test_configure_builds_a_ver1_ver2_pair_that_differs_only_in_protected_tests():
    """Ver1에는 Security 자체 테스트 범위와 순환용 보호 범위 두 개(QA·SECURITY)·보호 케이스 9개가, Ver2에는 자체 테스트
    범위만 있다. dev가 두 unit 설정을 받아 주고, 보호 범위의 역할과 보호 케이스 짝이 맞는다."""
    documents = configure(TOOLS, contract=CONTRACT)
    ver1, ver2 = documents['ver1/owned-tools.local.json'], documents['ver2/owned-tools.local.json']
    assert [scope['name'] for scope in ver2['unit']['scopes']] == ['developer-unit', 'qa-unit', 'security-tests']
    assert [scope['name'] for scope in ver1['unit']['scopes']] == [
        'developer-unit', 'qa-unit', 'security-tests', 'protected-signup-qa', 'protected-signup-security']
    for document in (ver1, ver2):
        decoded = decode_unit_configuration(json.dumps(document['unit']))
        assert {scope.name: scope.roles for scope in decoded.scopes}['security-tests'] == (AgentRole.SECURITY,)
    protected = {scope.name: scope.roles[0] for scope in decode_unit_configuration(json.dumps(ver1['unit'])).scopes
                 if scope.kind == 'PROTECTED'}
    cases = documents['ver1/owned-protected-cases.local.json']['cases']
    assert {case['selector'] for case in cases} == set(protected) and len(cases) == 9
    assert {key: value for key, value in ver1.items() if key != 'unit'} == {key: value for key, value in ver2.items() if key != 'unit'}
    assert documents['experiment.json']['suiteSha256'] == {scope: pt.suite_sha256(scope) for scope in pt.SCOPES}
    with pytest.raises(ExperimentError):
        configure({'unit': {'scopes': [TOOLS['unit']['scopes'][0]]}}, contract=CONTRACT)  # QA 자체 테스트 범위가 없음


def git(repo, *args):
    subprocess.run(['git', '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', '-c', 'commit.gpgsign=false',
                    *args], cwd=repo, check=True, capture_output=True)


def starting_repo(tmp_path, extra=None):
    """회원가입 서비스와 lock 파일을 커밋한 시작 코드 저장소와 그 설정(owned-source 내용)을 만든다."""
    repo = tmp_path / 'starter'
    files = {**signup_service.build('good'), 'requirements.lock': LOCK.decode(), **(extra or {})}
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text, encoding='utf-8')
    git(repo, 'init', '-q')
    git(repo, 'add', '-A')
    git(repo, 'commit', '-q', '-m', 'starter')
    commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    return {'repositoryPath': str(repo), 'commitHash': commit, 'lockPath': 'requirements.lock', 'gitTimeoutSeconds': 30}


def test_starting_source_with_exam_files_is_refused(tmp_path):
    """시작 코드 커밋을 dev와 같은 방식으로 꺼내 확인한다. 깨끗하면 설정을 만들고, 시험지 사본이나 평가 코드 경로가
    있으면 STARTING_SOURCE_CONTAINS_EVALUATION_FILES로 멈춘다."""
    clean = starting_source_files(starting_repo(tmp_path / 'a'), tmp_path)
    assert 'routers/auth.py' in clean and configure(TOOLS, contract=CONTRACT, starting_files=clean)
    leaked = starting_source_files(starting_repo(tmp_path / 'b', {
        'tests/test_copy.py': pt.protected_files('final')['tests/signup/exam_final.py']}), tmp_path)
    with pytest.raises(ExperimentError) as caught:
        configure(TOOLS, contract=CONTRACT, starting_files=leaked)
    assert caught.value.code == 'STARTING_SOURCE_CONTAINS_EVALUATION_FILES'


@pytest.mark.skipif(not RUNTIME, reason='fastapi·httpx·sqlalchemy·argon2-cffi가 있어야 실행 시험을 돈다')
def test_score_export_keeps_output_summary_and_meta(tmp_path):
    """snapshot_export 모양의 폴더(run.json·v2/source.tar·v2/manifest.json)를 채점해 Run 폴더에 결과·요약·쌍 번호를
    보관한다. 같은 폴더에 다시 쓰려면 force가 필요하다."""
    bundle = FIXTURE_RUNS['qa_failure_actual_fix_and_revalidation_preserve_all_lineages_and_shared_budget']
    files = {path: text.encode('utf-8') for path, text in signup_service.build('good').items()}
    archive = canonical_tar({**files, 'requirements.lock': LOCK})
    export = tmp_path / 'export'
    (export / 'v2').mkdir(parents=True)
    (export / 'run.json').write_text(json.dumps({'runId': bundle['run']['runId'], 'finalCodeVersion': 2}))
    (export / 'v2/source.tar').write_bytes(archive)
    (export / 'v2/manifest.json').write_text(json.dumps(manifest(archive).to_payload()))
    folder = score_export(export, runner=VerifiedImageRunner(), exam='pilot', results=tmp_path / 'results',
                          contract=CONTRACT, bundle=bundle, pair_id='p1')
    output = json.loads((folder / 'output.json').read_text())
    assert output['exam'] == 'pilot' and output['report']['evaluationVerdict'] == 'PASS' and output['score']['score'] == 1.0
    assert json.loads((folder / 'meta.json').read_text())['pairId'] == 'p1'
    assert '최종 시험 점수: 1.000' in (folder / 'summary.md').read_text()
    with pytest.raises(ExperimentError):
        score_export(export, runner=VerifiedImageRunner(), exam='pilot', results=tmp_path / 'results', contract=CONTRACT,
                     bundle=bundle)


def test_compare_reads_kept_results_and_reports_practice_effect(tmp_path):
    """보관한 Run 폴더들로 Ver1·Ver2를 비교한다: 비교 가능 여부, 정책별 평균 점수, 연습 효과(차이의 차이).
    시범·공식 채점이 섞이면 거부한다."""
    rows = ((uid(1), 'suite-1', [['PASS'], ['PASS', 'FAIL']], [['PASS'], ['PASS', 'PASS']]),
            (uid(2), None, [['PASS'], ['FAIL']], [['PASS'], ['FAIL']]))
    for run_id, ref, final_rows, practice_rows in rows:
        item = run_bundle(run_id)
        item['configuration'] = configuration(run_id, protectedTestSuiteRef=ref)
        folder = tmp_path / str(run_id)
        folder.mkdir()
        (folder / 'output.json').write_text(json.dumps({'report': scored_report(run_id, final_rows),
                                                        'referenceReport': scored_report(run_id, practice_rows)}))
        (folder / 'meta.json').write_text(json.dumps({'runId': run_id, 'exam': 'final', 'pairId': 'p1'}))
        (folder / 'bundle.json').write_text(json.dumps(item))
    result = compare(tmp_path)
    assert result['exam'] == 'final' and result['validity']['valid'] is True
    assert result['summary']['IN_LOOP_AND_FINAL']['evaluation']['meanExamScore'] == 0.75
    assert result['practiceEffect']['practiceEffect'] == pytest.approx(0.25)
    (tmp_path / str(uid(2)) / 'meta.json').write_text(json.dumps({'runId': uid(2), 'exam': 'pilot', 'pairId': 'p1'}))
    with pytest.raises(ExperimentError):
        compare(tmp_path)


@pytest.mark.skipif(not RUNTIME, reason='fastapi·httpx·sqlalchemy·argon2-cffi가 있어야 실행 시험을 돈다')
def test_all_versions_scoring_counts_regressions(tmp_path):
    """v1(정상)에서 v2(7자 비밀번호 허용)로 고치며 깨진 검사 2개(QA-SIGNUP-004·006)가 Regression으로 세어지고,
    중간 버전 보고서·regression.json이 보관되며 요약에도 적힌다."""
    bundle = FIXTURE_RUNS['qa_failure_actual_fix_and_revalidation_preserve_all_lineages_and_shared_budget']
    export = tmp_path / 'export'
    for version, variant in ((1, 'good'), (2, 'seven_char_password')):
        files = {path: text.encode('utf-8') for path, text in signup_service.build(variant).items()}
        archive = canonical_tar({**files, 'requirements.lock': LOCK})
        payload = manifest(archive).to_payload()
        payload['codeVersion'] = version
        (export / f'v{version}').mkdir(parents=True)
        (export / f'v{version}/source.tar').write_bytes(archive)
        (export / f'v{version}/manifest.json').write_text(json.dumps(ExecutionManifest.from_payload(payload).to_payload()))
    (export / 'run.json').write_text(json.dumps({'runId': bundle['run']['runId'], 'finalCodeVersion': 2,
                                                 'exportedCodeVersions': [1, 2]}))
    folder = score_export(export, runner=VerifiedImageRunner(), exam='pilot', results=tmp_path / 'results',
                          contract=CONTRACT, bundle=bundle, all_versions=True)
    regression = json.loads((folder / 'regression.json').read_text())
    assert regression['regressionCount'] == 2 and (folder / 'versions/v1.json').is_file()
    assert 'Regression' in (folder / 'summary.md').read_text()
    (export / 'run.json').write_text(json.dumps({'runId': bundle['run']['runId'], 'finalCodeVersion': 2,
                                                 'exportedCodeVersions': [2]}))
    with pytest.raises(ExperimentError):
        score_export(export, runner=VerifiedImageRunner(), exam='pilot', results=tmp_path / 'other',
                     contract=CONTRACT, bundle=bundle, all_versions=True)


def test_kept_results_in_repository_are_well_formed():
    """저장소에 보관한 실험 결과(experiments/)는 Run 폴더마다 output.json·meta.json·summary.md가 있고, 시험지가
    pilot·final 중 하나이며, 내보낸 Snapshot tar를 담지 않는다(.gitignore의 export/ 밖). 결과가 없으면 확인할 것도 없다."""
    root = Path(__file__).resolve().parents[2] / 'experiments'
    if not root.is_dir():
        return
    for output in root.rglob('output.json'):
        folder = output.parent
        if 'versions' in folder.parts:
            continue
        assert (folder / 'meta.json').is_file() and (folder / 'summary.md').is_file(), folder
        assert json.loads((folder / 'meta.json').read_text(encoding='utf-8'))['exam'] in ('pilot', 'final'), folder
    assert not [path for path in root.rglob('*.tar') if 'export' not in path.parts]


@pytest.mark.skipif(not RUNTIME, reason='fastapi·httpx·sqlalchemy·argon2-cffi가 있어야 실행 시험을 돈다')
def test_score_passes_trace_and_a2a_bodies_to_the_exposure_check(tmp_path):
    """score에 Run 기록(trace.json)과 dev a2a_export 결과를 주면 노출 검사가 A2A Message 본문까지 보고 결과에 남는다.
    다른 Run의 A2A 문서는 받지 않는다."""
    from tests.evaluation.test_trace_checks import a2a_document, documents
    bundle = FIXTURE_RUNS['qa_failure_actual_fix_and_revalidation_preserve_all_lineages_and_shared_budget']
    run_id = bundle['run']['runId']
    files = {path: text.encode('utf-8') for path, text in signup_service.build('good').items()}
    archive = canonical_tar({**files, 'requirements.lock': LOCK})
    export = tmp_path / 'export'
    (export / 'v2').mkdir(parents=True)
    (export / 'run.json').write_text(json.dumps({'runId': run_id, 'finalCodeVersion': 2}))
    (export / 'v2/source.tar').write_bytes(archive)
    (export / 'v2/manifest.json').write_text(json.dumps(manifest(archive).to_payload()))
    trace = documents(runId=run_id)
    folder = score_export(export, runner=VerifiedImageRunner(), exam='pilot', results=tmp_path / 'r1', contract=CONTRACT,
                          bundle=bundle, trace=trace, a2a=a2a_document(run_id))
    coverage = json.loads((folder / 'output.json').read_text())['trace']['exposure']['coverage']
    assert 'a2aMessages' in coverage['checkedDocuments'] and 'A2A_MESSAGE_BODY' not in coverage['notCollectedOutputs']
    with pytest.raises(ExperimentError):
        score_export(export, runner=VerifiedImageRunner(), exam='pilot', results=tmp_path / 'r2', contract=CONTRACT,
                     bundle=bundle, trace=trace, a2a=a2a_document('00000000-0000-4000-8000-000000000099'))
