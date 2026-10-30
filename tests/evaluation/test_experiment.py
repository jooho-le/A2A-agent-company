"""experiment.py(Ver1·Ver2 실험 준비·채점 보관·비교 도구)를 시험한다.

확인하는 것:
  - configure가 Ver1(자체 테스트 + 순환용 보호 범위 두 개 + 보호 케이스)과 Ver2(자체 테스트만) 설정 쌍을 만들고,
    dev가 그 unit 설정과 보호 케이스 짝을 받아 주는가? 두 버전의 차이는 보호 테스트뿐인가?
  - 시작 코드 커밋에 시험지·평가 코드가 섞였으면(dev와 같은 방식으로 꺼내 확인) 설정을 만들지 않는가?
  - score가 snapshot_export 결과를 D5로 채점해 Run별 폴더에 보관하고, compare가 Ver1·Ver2 비교·차이의 차이(보조 지표)를 내는가?
  - 모든 버전 채점으로 Regression(수정으로 새로 생긴 실패)을 계산하는가? 저장소에 보관한 결과(experiments/)의 모양이 맞는가?
  - 다른 폴더에 쓰는 설정의 lock 경로, 계획 Run 수(성공률 분모), 이미 있는 결과를 채점 전에 거부하는가?
  - 채점하지 못한 Run을 record로 남기고 나중에 채점해도 쌍 번호·버전·Run 기록을 이어받는가? compare가 보관 결과(보고서
    스키마·Run·시험지 지문·Regression)를 다시 대조해 손상·섞임을 거부하고, 계획 버전 이름표가 동결 설정과 다르면 막는가?
  - 보호 테스트가 전제로 하는 정책과 다른 시나리오, Run 기록과 다른 구조(architecture)를 채점 전에 멈추는가?
    같은 방안 안에서 구조별로 비교하는가? 결과 교체가 실패하면 예전 결과를 되돌리거나 복구 필요를 알리는가?
"""
import json
import subprocess

import pytest

from evaluation import protected_tests as pt
from pathlib import Path

from evaluation.experiment import ExperimentError, compare, configure, record_run, score_export, starting_source_files
from evaluation.manifest import ExecutionManifest
from mcp_tools.tools.unit_config import decode_unit_configuration
from orchestrator.domain.states import AgentRole
from tests.evaluation import signup_service
from tests.evaluation.test_final_evaluation import (CONTRACT, FIXTURE_RUNS, LOCK, RUNTIME, VerifiedImageRunner,
                                                    canonical_tar, manifest)
from tests.evaluation.test_run_metrics import bundle as run_bundle, configuration, uid

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



def test_configure_rewrites_a_relative_lock_path_against_the_original_tools_folder(tmp_path):
    """Host는 dependencyLockFile의 상대 경로를 도구 설정 파일 위치 기준으로 읽는다. ver1/·ver2/에 따로 쓰는 설정이
    같은 lock을 가리키도록 원래 폴더 기준 절대 경로로 바꾸고, 원래 폴더를 모르면 만들지 않는다. 절대 경로는 그대로 둔다."""
    tools = {**TOOLS, 'dependencyLockFile': '../locks/requirements.lock'}
    documents = configure(tools, contract=CONTRACT, base=tmp_path / 'configs')
    expected = str((tmp_path / 'locks/requirements.lock').resolve())
    assert {documents[f'{version}/owned-tools.local.json']['dependencyLockFile'] for version in ('ver1', 'ver2')} == {expected}
    assert tools['dependencyLockFile'] == '../locks/requirements.lock'  # 받은 설정은 바꾸지 않는다
    with pytest.raises(ExperimentError) as caught:
        configure(tools, contract=CONTRACT)
    assert caught.value.code == 'LOCK_PATH_NEEDS_BASE'
    absolute = configure({**TOOLS, 'dependencyLockFile': expected}, contract=CONTRACT)
    assert absolute['ver2/owned-tools.local.json']['dependencyLockFile'] == expected


def test_dev_accepts_both_arms_as_declared_experiment_runs():
    """같은 experimentId로 Ver1(보호 테스트 참조 있음)과 Ver2(참조 없음)를 모두 실험 Run으로 선언할 수 있다
    (dev RunConfiguration, 2026-10-30 수정 전에는 Ver2가 거부됨). 다른 비교 조건이 빠지면 여전히 거부한다."""
    from pydantic import ValidationError
    from orchestrator.domain.run_configuration import RunConfiguration
    for ref in (pt.loop_suite_ref(), None):
        settings = configuration(uid(1), protectedTestSuiteRef=ref)['configuration']
        assert RunConfiguration.model_validate(settings).experiment_id is not None
    with pytest.raises(ValidationError):
        RunConfiguration.model_validate({**configuration(uid(1), protectedTestSuiteRef=None)['configuration'],
                                         'executionOrder': None})

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



def test_score_refuses_an_existing_result_before_running_containers(tmp_path):
    """같은 Run의 결과가 이미 있으면 force 없이는 채점(컨테이너 실행)을 시작하기 전에 OUTPUT_EXISTS로 멈춘다."""
    class NoRunner:
        # 무엇이든 쓰려고 하면 실패하는 실행기 대역(채점이 시작되지 않았음을 확인)
        def __getattr__(self, name):
            raise AssertionError('runner must not be used')

    bundle = FIXTURE_RUNS['qa_failure_actual_fix_and_revalidation_preserve_all_lineages_and_shared_budget']
    files = {path: text.encode('utf-8') for path, text in signup_service.build('good').items()}
    archive = canonical_tar({**files, 'requirements.lock': LOCK})
    export = tmp_path / 'export'
    (export / 'v2').mkdir(parents=True)
    (export / 'run.json').write_text(json.dumps({'runId': bundle['run']['runId'], 'finalCodeVersion': 2}))
    (export / 'v2/source.tar').write_bytes(archive)
    (export / 'v2/manifest.json').write_text(json.dumps(manifest(archive).to_payload()))
    kept = tmp_path / 'results' / bundle['run']['runId']
    kept.mkdir(parents=True)
    (kept / 'output.json').write_text('{}')
    with pytest.raises(ExperimentError) as caught:
        score_export(export, runner=NoRunner(), exam='pilot', results=tmp_path / 'results', contract=CONTRACT, bundle=bundle)
    assert caught.value.code == 'OUTPUT_EXISTS' and (kept / 'output.json').read_text() == '{}'

def real_report(run_id, exam, failed=(), skipped=(), architecture='MULTI_AGENT'):
    """그 시험지(exam)로 채점한 실제 모양의 독립 평가 보고서(스키마·지문이 맞음). failed·skipped는 검사 Key 목록.
    환경 지문은 시험용 Run 설정(configuration)과 같고 코드 버전은 2다."""
    from evaluation.independent import build_evaluation_report
    from tests.evaluation.test_protected_signup import report as unit_report
    inputs = pt.evaluation_inputs(CONTRACT, exam)
    keys = {check['checkId']: check['key'] for check in pt.load_suite()['checks']}
    rows = [(test_id, 'FAIL', 'AssertionError: x') if keys[check_id] in failed
            else (test_id, 'SKIP', 'UNVERIFIED:APP_IMPORT_FAILED') if keys[check_id] in skipped else (test_id, 'PASS')
            for test_id, check_id in inputs['unit_cases'].items()]
    results = pt.final_check_results(inputs, unit_report=unit_report(*rows), unit_evidence_id=uid(50))
    expected = ExecutionManifest('repo', 2, uid(1), 'sha1', 'a' * 40, 'b' * 40, 'f' * 64,
                                 'sha256:' + 'c' * 64, 'sha256:' + 'd' * 64)
    return build_evaluation_report(
        evaluation_id=uid(60), source_run_id=run_id, architecture=architecture, suite_id=inputs['suite_id'],
        suite_version=inputs['suite_version'], suite_sha256=inputs['suite_sha256'], created_at='2026-10-30T12:00:00+09:00',
        required_requirement_ids=inputs['required_requirement_ids'], expected=expected, observed=expected,
        plan=inputs['plan'], results=results, bindings=inputs['bindings'])


def keep(results, run_id, ref, final, practice, exam='final', pair='p1', label='auto', architecture='MULTI_AGENT'):
    """score가 남기는 모양(output.json·meta.json·bundle.json)으로 Run 하나의 결과를 보관한다.
    label: 실험 계획상 버전(기본은 참조 유무에 맞춘 값, None이면 적지 않음)."""
    item = run_bundle(run_id, architecture=architecture)
    item['configuration'] = configuration(run_id, architecture, protectedTestSuiteRef=ref)
    if label == 'auto':
        label = 'IN_LOOP_AND_FINAL' if ref else 'FINAL_ONLY'
    folder = results / str(run_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'output.json').write_text(json.dumps({'report': final, 'referenceReport': practice}))
    (folder / 'meta.json').write_text(json.dumps({'runId': run_id, 'exam': exam, 'pairId': pair, 'loopPolicy': label}))
    (folder / 'bundle.json').write_text(json.dumps(item))
    return folder


def test_compare_reads_kept_results_and_reports_practice_effect(tmp_path):
    """보관한 Run 폴더들로 Ver1·Ver2를 비교한다: 비교 가능 여부, 정책별 평균 점수, 차이의 차이.
    Ver1은 최종에서 QA-SIGNUP-004 하나만 FAIL(순환용은 모두 PASS), Ver2는 둘 다 QA-SIGNUP-004 FAIL이다."""
    from evaluation.scoring import exam_score
    ver1 = (real_report(uid(1), 'final', failed={'QA-SIGNUP-004'}), real_report(uid(1), 'practice'))
    ver2 = (real_report(uid(2), 'final', failed={'QA-SIGNUP-004'}), real_report(uid(2), 'practice', failed={'QA-SIGNUP-004'}))
    keep(tmp_path, uid(1), pt.loop_suite_ref(), *ver1)
    keep(tmp_path, uid(2), None, *ver2)
    result = compare(tmp_path)
    assert result['exam'] == 'final' and result['validity']['valid'] is True, result['validity']
    assert not [item for item in result['validity']['warnings'] if item.get('detail') == 'RUN_IS_NOT_A_DECLARED_EXPERIMENT']
    final_score = exam_score(ver1[0])['score']
    assert result['summary']['IN_LOOP_AND_FINAL']['evaluation']['meanExamScore'] == final_score
    assert result['practiceEffect']['practiceEffect'] == pytest.approx(1.0 - final_score)
    assert result['plannedRunsKnown'] is False and result['plannedRuns'] == {'IN_LOOP_AND_FINAL': 1, 'FINAL_ONLY': 1}
    # 성공률 분모는 계획 Run 수다. 채점하지 못한 Run이 있으면 결과 폴더 수보다 크다.
    planned = compare(tmp_path, {'IN_LOOP_AND_FINAL': 2, 'FINAL_ONLY': 1})
    assert planned['plannedRunsKnown'] is True and planned['summary']['IN_LOOP_AND_FINAL']['plannedRuns'] == 2
    for wrong in ({'IN_LOOP_AND_FINAL': 1}, {'IN_LOOP_AND_FINAL': 0, 'FINAL_ONLY': 1},
                  {'IN_LOOP_AND_FINAL': 1, 'FINAL_ONLY': 1, 'MULTI_AGENT': 1}):
        with pytest.raises(ExperimentError) as caught:
            compare(tmp_path, wrong)  # 계획에 없는 정책의 결과, 계획보다 많은 결과, 모르는 정책
        assert caught.value.code == 'PLANNED_RUNS_INVALID'



def test_compare_keeps_runs_that_could_not_be_scored(tmp_path):
    """채점하지 못한 실패 Run(record로 Run 기록만 남김)도 비교에 들어간다(Codex 10.30 추가 점검 2): 정책마다 성공 1·실패 1이면
    기록된 Run 2, 시작 안 함 0, 실패 1이고 실패 Run의 비용·토큰도 합친다. 채점 못 한 Run의 짝은 차이의 차이 확정 계산에서
    빠지고, Run마다 실행 결과와 채점 여부를 따로 적는다. 이미 채점한 Run은 record로 덮어쓰지 않는다."""
    from tests.evaluation.test_run_metrics import bundle as failed_bundle
    keep(tmp_path, uid(1), pt.loop_suite_ref(), real_report(uid(1), 'final'), real_report(uid(1), 'practice'))
    keep(tmp_path, uid(2), None, real_report(uid(2), 'final'), real_report(uid(2), 'practice'))
    for run_id, ref, label in ((uid(3), pt.loop_suite_ref(), 'IN_LOOP_AND_FINAL'), (uid(4), None, 'FINAL_ONLY')):
        item = failed_bundle(run_id, verdict='FAIL', cost='2.5')
        item['configuration'] = configuration(run_id, protectedTestSuiteRef=ref)
        record_run(tmp_path, item, pair_id='p2' if run_id == uid(3) else 'p1', loop_policy=label)
    result = compare(tmp_path, {'IN_LOOP_AND_FINAL': 2, 'FINAL_ONLY': 2})
    for policy in ('IN_LOOP_AND_FINAL', 'FINAL_ONLY'):
        group = result['summary'][policy]
        assert (group['recordedRuns'], group['notStarted'], group['fail'], group['success']) == (2, 0, 1, 1)
        assert group['knownCostUsd'] == '3.0' and group['knownTotalTokens'] == 2000
    assert result['unscoredRuns'] == sorted([uid(3), uid(4)])
    assert {row['runId']: row['scoring'] for row in result['runs']}[uid(4)] == 'NOT_SCORED'
    # uid(2)의 짝(p1)인 uid(4)가 채점되지 않았으므로 uid(2)는 확정 계산에서 빠진다.
    assert result['practiceEffect']['policies']['FINAL_ONLY']['excludedRuns'] == {'PAIR_PARTNER_EXCLUDED': 1}
    with pytest.raises(ExperimentError) as caught:
        record_run(tmp_path, run_bundle(uid(1)))
    assert caught.value.code == 'ALREADY_SCORED'
    with pytest.raises(ExperimentError) as caught:  # 모양이 틀린 Run 기록은 짧은 코드로 멈춘다(명령줄에서 traceback 대신)
        record_run(tmp_path, {'run': {'runId': uid(8)}})
    assert caught.value.code == 'BUNDLE_INVALID'


def test_compare_checks_the_intended_version_label(tmp_path):
    """meta에 적은 실험 계획상 버전이 동결 설정(보호 테스트 참조 유무)과 다르면 비교가 무효다(Ver1으로 계획한 Run이
    보호 테스트 없이 돌았는데 Ver2로 집계되는 일을 막음). 적지 않았으면 동결 설정으로 읽되 주의로 알린다."""
    keep(tmp_path, uid(1), pt.loop_suite_ref(), real_report(uid(1), 'final'), real_report(uid(1), 'practice'))
    keep(tmp_path, uid(2), None, real_report(uid(2), 'final'), real_report(uid(2), 'practice'), label='IN_LOOP_AND_FINAL')
    keep(tmp_path, uid(3), None, real_report(uid(3), 'final'), real_report(uid(3), 'practice'), pair='p2')
    wrong = compare(tmp_path)
    assert wrong['validity']['valid'] is False
    assert any(item['field'] == 'loopPolicy' for item in wrong['validity']['violations'])
    keep(tmp_path, uid(2), None, real_report(uid(2), 'final'), real_report(uid(2), 'practice'), label=None)
    unlabeled = compare(tmp_path)
    assert any(item.get('detail') == 'INTENDED_LOOP_POLICY_NOT_RECORDED' for item in unlabeled['validity']['warnings'])


def test_unsupported_policy_is_refused_before_configuring_or_scoring(tmp_path):
    """보호 테스트가 전제로 하는 정책과 다른 시나리오 정책(Argon2 메모리 기준 65536)으로는 설정 쌍을 만들지 않고,
    채점도 컨테이너를 돌리기 전에 멈춘다(CONTRACT_NOT_SUPPORTED, Codex 10.30 3차 5)."""
    import copy

    class NoRunner:
        # 무엇이든 쓰려고 하면 실패하는 실행기 대역(채점이 시작되지 않았음을 확인)
        def __getattr__(self, name):
            raise AssertionError('runner must not be used')

    stronger = copy.deepcopy(CONTRACT)
    stronger['securityPolicy']['passwordHashPolicy']['memoryKiB'] = 65536
    for call in (lambda: configure(TOOLS, contract=stronger),
                 lambda: score_export(tmp_path / 'missing-export', runner=NoRunner(), exam='final',
                                      results=tmp_path / 'results', contract=stronger)):
        with pytest.raises(ExperimentError) as caught:
            call()
        assert caught.value.code == 'CONTRACT_NOT_SUPPORTED'


def test_compare_by_architecture_within_one_method(tmp_path):
    """10.29 회의록 방안 1·2의 본래 비교(Single vs Multi)를 같은 방안 안에서 할 수 있다: 같은 보호 테스트 조건(여기서는
    방안 2 = 순환용 보호 테스트 없음)의 Multi·Single Run을 구조별로 묶으면 비교가 유효하고, 차이의 차이는 계산하지 않는다.
    방안이 다른 Run(Single은 순환용 보호 테스트 있음)을 섞으면 비교가 무효다."""
    keep(tmp_path, uid(1), None, real_report(uid(1), 'final'), real_report(uid(1), 'practice'))
    keep(tmp_path, uid(2), None, real_report(uid(2), 'final', architecture='SINGLE_AGENT'),
         real_report(uid(2), 'practice', architecture='SINGLE_AGENT'), architecture='SINGLE_AGENT')
    result = compare(tmp_path, group_by='architecture')
    assert result['groupBy'] == 'architecture' and result['validity']['valid'] is True, result['validity']
    assert set(result['summary']) == {'MULTI_AGENT', 'SINGLE_AGENT'} and result['practiceEffect'] is None
    keep(tmp_path, uid(2), pt.loop_suite_ref(), real_report(uid(2), 'final', architecture='SINGLE_AGENT'),
         real_report(uid(2), 'practice', architecture='SINGLE_AGENT'), architecture='SINGLE_AGENT')
    assert compare(tmp_path, group_by='architecture')['validity']['valid'] is False


@pytest.mark.skipif(not RUNTIME, reason='fastapi·httpx·sqlalchemy·argon2-cffi가 있어야 실행 시험을 돈다')
def test_score_labels_the_frozen_architecture(tmp_path):
    """Single Run을 채점하면 보고서의 구조가 Run 기록에 동결된 SINGLE_AGENT다(예전에는 늘 MULTI_AGENT로 적어 구조별 비교가
    무효가 됐다). 명령에 다른 구조를 주면 채점 전에 멈춘다(ARCHITECTURE_MISMATCH)."""
    class NoRunner:
        # 무엇이든 쓰려고 하면 실패하는 실행기 대역(채점이 시작되지 않았음을 확인)
        def __getattr__(self, name):
            raise AssertionError('runner must not be used')

    bundle = run_bundle(uid(1), architecture='SINGLE_AGENT')
    bundle['configuration'] = configuration(uid(1), 'SINGLE_AGENT', protectedTestSuiteRef=None)
    files = {path: text.encode('utf-8') for path, text in signup_service.build('good').items()}
    archive = canonical_tar({**files, 'requirements.lock': LOCK})
    export = tmp_path / 'export'
    (export / 'v2').mkdir(parents=True)
    (export / 'run.json').write_text(json.dumps({'runId': uid(1), 'finalCodeVersion': 2}))
    (export / 'v2/source.tar').write_bytes(archive)
    (export / 'v2/manifest.json').write_text(json.dumps(manifest(archive).to_payload()))
    with pytest.raises(ExperimentError) as caught:
        score_export(export, runner=NoRunner(), exam='pilot', results=tmp_path / 'results', contract=CONTRACT,
                     bundle=bundle, architecture='MULTI_AGENT')
    assert caught.value.code == 'ARCHITECTURE_MISMATCH'
    folder = score_export(export, runner=VerifiedImageRunner(), exam='pilot', results=tmp_path / 'results',
                          contract=CONTRACT, bundle=bundle)
    assert json.loads((folder / 'output.json').read_text())['report']['architecture'] == 'SINGLE_AGENT'

@pytest.mark.parametrize('damage, code', [
    ('swap', 'EXAM_SUITE_MISMATCH'),            # 최종·참고 보고서를 바꿔 넣음
    ('other_practice', 'REFERENCE_SUITE_MISMATCH'),  # 지금 묶음과 다른 순환용 시험지로 참고 채점
    ('pilot_meta', 'EXAM_SUITE_MISMATCH'),       # meta는 pilot인데 final 시험지로 채점
    ('no_manifest_check', 'REPORT_INVALID'),     # 저장된 보고서가 손상됨(Manifest 확인 칸 없음)
    ('other_run', 'REPORT_RUN_MISMATCH'),        # 다른 Run의 보고서
    ('stale_regression', 'REGRESSION_MISMATCH'),  # 다른 시험지로 계산한 regression.json이 남음
    ('wrong_version', 'RESULTS_INCONSISTENT'),    # Run의 최종 코드 버전이 아닌 버전을 채점한 보고서
])
def test_compare_refuses_damaged_or_mixed_results(tmp_path, damage, code):
    """읽을 때 다시 확인한다(Codex 10.30 R4·R6·R9): 보고서 스키마·Run, 최종·참고 시험지 지문, regression.json의 Run·시험지·
    최종 버전이 맞지 않으면 비교하지 않는다."""
    final, practice = real_report(uid(1), 'final'), real_report(uid(1), 'practice')
    exam = 'final'
    if damage == 'swap':
        final, practice = practice, final
    elif damage == 'other_practice':
        practice['suite']['sha256'] = 'a' * 64
    elif damage == 'pilot_meta':
        exam = 'pilot'
    elif damage == 'no_manifest_check':
        del final['manifestCheck']
    elif damage == 'other_run':
        final = real_report(uid(3), 'final')
    folder = keep(tmp_path, uid(1), pt.loop_suite_ref(), final, practice, exam=exam)
    keep(tmp_path, uid(2), None, real_report(uid(2), 'final'), real_report(uid(2), 'practice'))
    if damage == 'wrong_version':
        item = json.loads((folder / 'bundle.json').read_text())
        item['run']['codeVersion'] = 3
        (folder / 'bundle.json').write_text(json.dumps(item))
    if damage == 'stale_regression':
        (folder / 'regression.json').write_text(json.dumps({'runId': uid(1), 'suiteSha256': pt.suite_sha256('pilot'),
                                                            'finalCodeVersion': 2, 'regressionCount': 1}))
    with pytest.raises(ExperimentError) as caught:
        compare(tmp_path)
    assert caught.value.code == code


def test_compare_needs_the_practice_given_in_the_loop(tmp_path):
    """Ver1 Run 설정의 보호 테스트 참조가 지금 순환용 시험지 지문을 담은 참조(loop_suite_ref)가 아니면, 개발 중에 준
    시험지와 참고 채점의 시험지가 같은지 알 수 없어 비교가 무효다(Codex 10.30 R6). 섞인 pilot·final은 거부한다."""
    keep(tmp_path, uid(1), 'artifact://protected-tests/scn-001/signup-0.1.0', real_report(uid(1), 'final'),
         real_report(uid(1), 'practice'))
    keep(tmp_path, uid(2), None, real_report(uid(2), 'final'), real_report(uid(2), 'practice'))
    result = compare(tmp_path)
    assert result['validity']['valid'] is False
    assert any(item['field'] == 'protectedTestSuiteRef.practice' for item in result['validity']['violations'])
    keep(tmp_path, uid(2), None, real_report(uid(2), 'pilot'), real_report(uid(2), 'practice'), exam='pilot')
    with pytest.raises(ExperimentError) as caught:
        compare(tmp_path)
    assert caught.value.code == 'MIXED_EXAMS'

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



@pytest.mark.skipif(not RUNTIME, reason='fastapi·httpx·sqlalchemy·argon2-cffi가 있어야 실행 시험을 돈다')
def test_forced_rescore_replaces_the_whole_result_set(tmp_path):
    """pilot으로 버전별 채점(regression.json·versions/·bundle.json)을 남긴 뒤 final로 다시 채점(force)하면 예전 파일이
    하나도 남지 않는다(Codex 10.30 R4). 다시 채점하다 실패하면 예전 결과 한 세트가 그대로 남고 임시 폴더도 없다."""
    bundle = FIXTURE_RUNS['qa_failure_actual_fix_and_revalidation_preserve_all_lineages_and_shared_budget']
    export = tmp_path / 'export'
    for version in (1, 2):
        files = {path: text.encode('utf-8') for path, text in signup_service.build('good').items()}
        archive = canonical_tar({**files, 'requirements.lock': LOCK})
        payload = manifest(archive).to_payload()
        payload['codeVersion'] = version
        (export / f'v{version}').mkdir(parents=True)
        (export / f'v{version}/source.tar').write_bytes(archive)
        (export / f'v{version}/manifest.json').write_text(json.dumps(ExecutionManifest.from_payload(payload).to_payload()))
    (export / 'run.json').write_text(json.dumps({'runId': bundle['run']['runId'], 'finalCodeVersion': 2,
                                                 'exportedCodeVersions': [1, 2]}))
    results = tmp_path / 'results'
    folder = score_export(export, runner=VerifiedImageRunner(), exam='pilot', results=results, contract=CONTRACT,
                          bundle=bundle, all_versions=True)
    assert {path.name for path in folder.iterdir()} >= {'regression.json', 'versions', 'bundle.json'}

    class BrokenRunner(VerifiedImageRunner):
        # 채점 도중 실패하는 실행기 대역
        def run(self, *args, **kwargs):
            raise RuntimeError('runner failed')

    before = (folder / 'output.json').read_text()
    with pytest.raises(Exception):
        score_export(export, runner=BrokenRunner(), exam='final', results=results, contract=CONTRACT, force=True)
    assert (folder / 'output.json').read_text() == before and (folder / 'regression.json').is_file()
    assert [path.name for path in results.iterdir()] == [bundle['run']['runId']]  # 임시 폴더가 남지 않음
    score_export(export, runner=VerifiedImageRunner(), exam='final', results=results, contract=CONTRACT, force=True)
    assert {path.name for path in folder.iterdir()} == {'output.json', 'meta.json', 'summary.md'}
    assert json.loads((folder / 'meta.json').read_text())['exam'] == 'final'
    assert [path.name for path in results.iterdir()] == [bundle['run']['runId']]


@pytest.mark.skipif(not RUNTIME, reason='fastapi·httpx·sqlalchemy·argon2-cffi가 있어야 실행 시험을 돈다')
def test_scoring_a_recorded_run_keeps_its_record(tmp_path):
    """record로 먼저 남긴 Run을 나중에 채점하면(--bundle 없이) 그 Run 기록·쌍 번호·버전을 이어받는다. 이어받지 않으면
    채점 결과 폴더에 Run 기록이 없어 compare가 멈춘다(BUNDLE_MISSING)."""
    bundle = run_bundle(uid(1))
    bundle['configuration'] = configuration(uid(1), protectedTestSuiteRef=None)
    files = {path: text.encode('utf-8') for path, text in signup_service.build('good').items()}
    archive = canonical_tar({**files, 'requirements.lock': LOCK})
    export = tmp_path / 'export'
    (export / 'v2').mkdir(parents=True)
    (export / 'run.json').write_text(json.dumps({'runId': bundle['run']['runId'], 'finalCodeVersion': 2}))
    (export / 'v2/source.tar').write_bytes(archive)
    (export / 'v2/manifest.json').write_text(json.dumps(manifest(archive).to_payload()))
    record_run(tmp_path / 'results', bundle, pair_id='p9', loop_policy='FINAL_ONLY')
    folder = score_export(export, runner=VerifiedImageRunner(), exam='pilot', results=tmp_path / 'results',
                          contract=CONTRACT)
    meta = json.loads((folder / 'meta.json').read_text())
    assert (meta['pairId'], meta['loopPolicy'], meta['exam']) == ('p9', 'FINAL_ONLY', 'pilot')
    assert json.loads((folder / 'bundle.json').read_text())['run']['runId'] == bundle['run']['runId']
    # fetch로 결과 폴더에 Run 기록만 받아 둔 경우(meta 없음)도 채점 뒤 Run 기록이 남는다.
    import shutil
    shutil.rmtree(folder)
    folder.mkdir()
    (folder / 'bundle.json').write_text(json.dumps(bundle))
    score_export(export, runner=VerifiedImageRunner(), exam='pilot', results=tmp_path / 'results', contract=CONTRACT)
    assert (folder / 'bundle.json').is_file() and (folder / 'output.json').is_file()


def test_failed_replacement_restores_or_flags_the_previous_result(tmp_path, monkeypatch):
    """결과를 바꾸는 단계에서 새 폴더 옮기기가 실패하면 예전 결과가 원래 자리로 돌아온다(RESULT_REPLACE_FAILED).
    되돌리기까지 실패하면 RESULT_RESTORE_NEEDED로 멈추고, compare도 백업만 남은 Run을 조용히 빼지 않고 멈춘다
    (Codex 10.30 3차 4)."""
    import pathlib
    from tests.evaluation.test_run_metrics import bundle as failed_bundle
    item = failed_bundle(uid(1), verdict='FAIL')
    item['configuration'] = configuration(uid(1), protectedTestSuiteRef=None)
    folder = record_run(tmp_path, item, loop_policy='FINAL_ONLY')
    original = pathlib.Path.rename

    def failing(blocked):
        def rename(self, target):
            if any(part in self.name for part in blocked):
                raise OSError('injected rename failure')
            return original(self, target)
        return rename

    monkeypatch.setattr(pathlib.Path, 'rename', failing(('.staging-',)))
    with pytest.raises(ExperimentError) as caught:
        record_run(tmp_path, item, loop_policy='FINAL_ONLY', force=True)
    assert caught.value.code == 'RESULT_REPLACE_FAILED' and (folder / 'bundle.json').is_file()
    monkeypatch.setattr(pathlib.Path, 'rename', failing(('.staging-', '.old-')))
    with pytest.raises(ExperimentError) as caught:
        record_run(tmp_path, item, loop_policy='FINAL_ONLY', force=True)
    assert caught.value.code == 'RESULT_RESTORE_NEEDED' and not folder.exists()
    monkeypatch.setattr(pathlib.Path, 'rename', original)
    with pytest.raises(ExperimentError) as caught:
        compare(tmp_path)
    assert caught.value.code == 'RESULT_RESTORE_NEEDED'

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
