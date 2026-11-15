"""도구 권한·LLM 한도 동일 확인(parity.py)을 시험한다. 모델 호출·개발 실행은 하지 않는다.

확인하는 것:
  - 지금 dev 정책은 Single 도구 = 네 역할 도구의 합집합이고, 단계별 차이는 참고로만 남는가?
  - Single에서 도구가 빠지거나 늘면 FAIL, 정책을 읽지 못하면 미검증인가?
  - 두 구조의 실행 설정에서 모델·LLM 한도·시간 예산·도구 설정·시작 코드·보호 케이스·가격표가 다르면 FAIL이고,
    구조마다 달라야 하는 경로·포트·저장 위치는 비교하지 않는가?
  - Host DB의 Run별 고정 해시를 읽기 전용으로 읽고, 다시 만든 해시와 같을 때만 PASS인가?
  - 실제 dev 읽기 전용 검사(inspect_host_configuration)의 해시가 LLM 한도를 바꾸면 달라지는가(한도까지 고정한다는 근거)?
"""
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from types import SimpleNamespace
import unittest

import pytest

from evaluation.parity import (check, config_parity, expected_policy_sha256, main, pinned_policy_hashes,
                               static_tool_parity)

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / 'configs'


def test_current_dev_policy_keeps_the_single_union_rule():
    result = static_tool_parity()
    assert result['status'] == 'PASS' and result['missingInSingle'] == [] and result['extraInSingle'] == []
    # Single 구현·수정 단계는 보고서 읽기 도구를 더 갖는다(합집합은 같아서 위반 아님, 참고로만 남긴다).
    assert result['phaseDifferences']['IMPLEMENTING']['moreThanRole'] == ['read_security_report', 'read_test_report']


def policy(single, **override):
    roles = {'PLANNER': (), 'DEVELOPER': ('read_project_file', 'write_source_file'), 'QA': ('read_project_file', 'run_unit_tests')}
    phases = {'PLANNING': {}, 'IMPLEMENTING': {'read_project_file': 1, 'write_source_file': 1},
              'QA': {'read_project_file': 1, 'run_unit_tests': 1}}
    values = dict(ROLE_TOOL_NAMES=roles, SINGLE_TOOL_NAMES=single, SINGLE_PHASE_TOOL_ROLES=phases)
    values.update(override)
    return SimpleNamespace(**values)


@pytest.mark.parametrize('fake,key,value', [
    (policy(('read_project_file', 'write_source_file')), 'missingInSingle', ['run_unit_tests']),
    (policy(('read_project_file', 'write_source_file', 'run_unit_tests', 'run_shell')), 'extraInSingle', ['run_shell']),
    (policy(('read_project_file', 'write_source_file', 'run_unit_tests'),
            SINGLE_PHASE_TOOL_ROLES={'QA': {'run_browser_tests': 1}}), 'phaseToolsBeyondSingle', ['run_browser_tests']),
])
def test_single_with_fewer_or_other_tools_fails(fake, key, value):
    result = static_tool_parity(fake)
    assert result['status'] == 'FAIL' and result[key] == value


def test_unreadable_policy_is_unverified_not_pass():
    assert static_tool_parity(SimpleNamespace())['status'] == 'UNVERIFIED'


def config_files(tmp_path, name, *, runtime='owned-runtime.example.json', cases=None, price=None, change=None):
    folder = tmp_path / name
    folder.mkdir()
    files = {}
    for key, source in (('runtime', runtime), ('tools', 'owned-tools.example.json'), ('source', 'owned-source.example.json')):
        data = json.loads((CONFIGS / source).read_text(encoding='utf-8'))
        if change and key in change:
            change[key](data)
        path = folder / source
        path.write_text(json.dumps(data), encoding='utf-8')
        files[key] = str(path)
    for key, data in (('cases', cases), ('priceBook', price)):
        if data is not None:
            path = folder / f'{key}.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            files[key] = str(path)
    return files


def statuses(rows):
    return {row['check']: row['status'] for row in rows}


def test_single_and_multi_example_configurations_differ_only_in_locations(tmp_path):
    single = config_files(tmp_path, 'single', runtime='single-runtime.example.json',
                          change={'tools': lambda data: data.update(dependencyLockFile='../elsewhere/requirements.lock'),
                                  'source': lambda data: data.update(repositoryPath='../single-copy')})
    multi = config_files(tmp_path, 'multi')
    rows = config_parity(single, multi)
    assert statuses(rows) == {'runtimeLimits': 'PASS', 'toolConfiguration': 'PASS', 'startingSource': 'PASS',
                              'protectedCases': 'PASS', 'priceBook': 'PASS'}
    assert rows[0]['llmLimits']['max_model_calls'] == 8


@pytest.mark.parametrize('key,change,check_name,field', [
    ('runtime', lambda data: data['llmLimits'].update(max_model_calls=20), 'runtimeLimits', 'llmLimits'),
    ('runtime', lambda data: data.update(runtimeBudgetMs=1), 'runtimeLimits', 'runtimeBudgetMs'),
    ('runtime', lambda data: data['model'].update(temperature=0.7), 'runtimeLimits', 'model'),
    ('tools', lambda data: data.update(imageReference='repo@sha256:' + '1' * 64), 'toolConfiguration', 'imageReference'),
    ('tools', lambda data: data['unit']['scopes'].pop(), 'toolConfiguration', 'unit'),
    ('tools', lambda data: data.update(maxCallSeconds=1), 'toolConfiguration', 'maxCallSeconds'),
    ('source', lambda data: data.update(commitHash='f' * 40), 'startingSource', 'commitHash'),
])
def test_controlled_differences_fail(tmp_path, key, change, check_name, field):
    single = config_files(tmp_path, 'single', runtime='single-runtime.example.json', change={key: change})
    rows = {row['check']: row for row in config_parity(single, config_files(tmp_path, 'multi'))}
    assert rows[check_name]['status'] == 'FAIL' and field in rows[check_name]['differingFields']


def test_protected_cases_and_price_book_must_match_or_be_absent_on_both(tmp_path):
    cases = {'schemaVersion': 1, 'cases': []}
    single = config_files(tmp_path, 'single', runtime='single-runtime.example.json', cases=cases, price={'v': 1})
    multi = config_files(tmp_path, 'multi', cases=cases, price={'v': 2})
    assert statuses(config_parity(single, multi)) == {'runtimeLimits': 'PASS', 'toolConfiguration': 'PASS',
                                                      'startingSource': 'PASS', 'protectedCases': 'PASS', 'priceBook': 'FAIL'}
    lonely = config_files(tmp_path, 'lonely')
    rows = {row['check']: row for row in config_parity(single, lonely)}
    assert rows['protectedCases']['differingFields'] == ['presence']


def test_unreadable_configuration_is_unverified(tmp_path):
    single = config_files(tmp_path, 'single', runtime='single-runtime.example.json')
    single['runtime'] = str(tmp_path / 'missing.json')
    assert statuses(config_parity(single, config_files(tmp_path, 'multi'))) == {'hostConfiguration': 'UNVERIFIED'}


@pytest.mark.parametrize('value', [[], ['invalid'], None, 'invalid', 42])
@pytest.mark.parametrize('key', ['runtime', 'tools', 'source', 'cases', 'priceBook'])
def test_non_object_configuration_is_unverified_in_both_comparisons(tmp_path, value, key):
    from evaluation.parity import version_parity
    single = config_files(tmp_path, 'single', cases={'cases': []}, price={'v': 1})
    multi = config_files(tmp_path, 'multi')
    Path(single[key]).write_text(json.dumps(value))
    for compare in (config_parity, version_parity):
        assert compare(single, multi) == [
            {'check': 'hostConfiguration', 'status': 'UNVERIFIED', 'reason': 'CONFIGURATION_UNREADABLE'}]


@pytest.mark.parametrize('unit', [['invalid'], 'invalid', {'scopes': 42},
                                 {'scopes': [{'kind': 'PROTECTED', 'name': None},
                                             {'kind': 'PROTECTED', 'name': 'protected'}]}])
def test_malformed_protected_scope_is_not_a_pass_or_crash(tmp_path, unit):
    from evaluation.parity import version_parity
    ver1 = config_files(tmp_path, 'ver1', cases={'cases': []}, change={'tools': lambda data: data.update(unit=unit)})
    ver2 = config_files(tmp_path, 'ver2')
    assert statuses(version_parity(ver1, ver2))['protectedScopes'] == 'FAIL'


def host_database(path, rows=None, table=True):
    with sqlite3.connect(path) as connection:
        if table:
            connection.execute('CREATE TABLE run_host_policies (run_id TEXT PRIMARY KEY, policy_sha256 TEXT NOT NULL)')
            connection.executemany('INSERT INTO run_host_policies VALUES (?, ?)', rows or [])
    return path


def test_pinned_hashes_are_read_without_changing_the_host_database(tmp_path):
    path = host_database(tmp_path / 'host.sqlite3', [('run-a', 'a' * 64)])
    before = path.read_bytes()
    assert pinned_policy_hashes(path, ['run-a', 'run-b']) == {'run-a': 'a' * 64, 'run-b': None}
    assert path.read_bytes() == before
    assert pinned_policy_hashes(host_database(tmp_path / 'empty.sqlite3', table=False), ['run-a']) == {'run-a': None}
    assert pinned_policy_hashes(tmp_path / 'absent.sqlite3', ['run-a']) == {'run-a': None}
    assert not (tmp_path / 'absent.sqlite3').exists()


def fake_inspect(hashes):
    def inspect(**arguments):
        value = hashes[arguments['architecture']]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(policy_sha256=value)
    return inspect


@pytest.mark.parametrize('pinned,hashes,expected', [
    ({'s': 'a' * 64, 'm': 'b' * 64}, {'SINGLE_AGENT': 'a' * 64, 'MULTI_AGENT': 'b' * 64}, 'PASS'),
    ({'s': 'c' * 64, 'm': 'b' * 64}, {'SINGLE_AGENT': 'a' * 64, 'MULTI_AGENT': 'b' * 64}, 'FAIL'),
    ({'s': 'a' * 64}, {'SINGLE_AGENT': 'a' * 64, 'MULTI_AGENT': 'b' * 64}, 'UNVERIFIED'),
    ({'s': 'a' * 64, 'm': 'b' * 64}, {'SINGLE_AGENT': 'a' * 64, 'MULTI_AGENT': ValueError('bad')}, 'UNVERIFIED'),
])
def test_runs_must_carry_the_hash_of_the_compared_configuration(tmp_path, pinned, hashes, expected):
    database = host_database(tmp_path / 'host.sqlite3', list(pinned.items()))
    single = config_files(tmp_path, 'single', runtime='single-runtime.example.json')
    multi = config_files(tmp_path, 'multi')
    runs = [{'runId': 's', 'architecture': 'SINGLE_AGENT', 'hostDatabase': str(database)},
            {'runId': 'm', 'architecture': 'MULTI_AGENT', 'hostDatabase': str(database)}]
    report = check(single, multi, runs=runs, inspect=fake_inspect(hashes))
    pinned_rows = [row for row in report['checks'] if row['check'] == 'pinnedPolicy']
    assert report['pinnedPolicyChecked'] is True and len(pinned_rows) == 2
    assert report['status'] == expected


def test_without_runs_only_files_are_compared_and_that_is_said(tmp_path):
    report = check(config_files(tmp_path, 'single', runtime='single-runtime.example.json'), config_files(tmp_path, 'multi'))
    assert report['status'] == 'PASS' and report['pinnedPolicyChecked'] is False
    assert any('증명은 아니다' in text for text in report['limitations'])


def test_command_line_writes_the_report_and_signals_failure(tmp_path, capsys):
    single = config_files(tmp_path, 'single', runtime='single-runtime.example.json',
                          change={'runtime': lambda data: data['llmLimits'].update(max_tool_calls=99)})
    multi = config_files(tmp_path, 'multi')
    text = lambda files: ','.join(f'{key}={value}' for key, value in files.items())
    out = tmp_path / 'results' / 'parity.json'
    assert main(['--single', text(single), '--multi', text(multi), '--out', str(out)]) == 2
    assert json.loads(out.read_text())['status'] == 'FAIL'
    assert main(['--single', 'runtime=x', '--multi', text(multi)]) == 1
    assert 'PARITY_INPUT_INVALID' in capsys.readouterr().err


# ---------------------------------------------------------------- 실제 dev 읽기 전용 검사와의 연결

def _host_fixture():
    tests_dir = str(ROOT / 'tests')
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    module = pytest.importorskip('test_owned_host')
    if shutil.which('git') is None:
        pytest.skip('git이 있어야 dev Host 설정 시험 도구를 만든다')

    class Fixture(module._HostFixture, unittest.TestCase):
        def runTest(self):
            pass
    return Fixture()


def test_real_dev_inspection_hash_covers_the_llm_limits():
    fixture = _host_fixture()
    fixture.setUp()
    try:
        files = {'runtime': fixture.runtime_file, 'tools': fixture.tools_file, 'source': fixture.source_file}
        first = expected_policy_sha256(files, 'MULTI_AGENT')
        assert isinstance(first, str) and len(first) == 64
        assert expected_policy_sha256(files, 'MULTI_AGENT') == first  # 같은 파일이면 같은 해시
        fixture.runtime['llmLimits']['max_model_calls'] += 1
        fixture.write()
        changed = expected_policy_sha256(files, 'MULTI_AGENT')
        assert changed is not None and changed != first  # LLM 한도가 바뀌면 고정 해시도 바뀐다
    finally:
        fixture.doCleanups()


# ---------------------------------------------------------------- Ver1·Ver2 동일 조건(점검 5)

def version_files(tmp_path, *, ver1_change=None, ver2_change=None):
    # 평가 쪽 configure가 만든 실제 Ver1·Ver2 도구 설정 쌍으로 같은 구조의 두 설정을 만든다.
    from evaluation.experiment import configure
    from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario
    tools = json.loads((CONFIGS / 'owned-tools.example.json').read_text(encoding='utf-8'))
    documents = configure(tools, contract=get_scenario(SCN_001_ID).planner_contract(), base=CONFIGS)
    files = {}
    for version, change in (('ver1', ver1_change), ('ver2', ver2_change)):
        def tools_change(data, version=version):
            data.clear()
            data.update(documents[f'{version}/owned-tools.local.json'])
        cases = documents['ver1/owned-protected-cases.local.json'] if version == 'ver1' else None
        merged = {'tools': tools_change, **(change or {})}
        if change and 'tools' in change:
            merged['tools'] = lambda data, version=version, extra=change['tools']: (tools_change(data, version), extra(data))
        files[version] = config_files(tmp_path, version, cases=cases, change=merged)
    return files['ver1'], files['ver2']


def test_configured_ver1_and_ver2_differ_only_in_protected_tests(tmp_path):
    from evaluation.parity import check_versions, version_parity
    ver1, ver2 = version_files(tmp_path)
    assert statuses(version_parity(ver1, ver2)) == {
        'runtimeLimits': 'PASS', 'toolConfiguration': 'PASS', 'protectedScopes': 'PASS', 'startingSource': 'PASS',
        'protectedCases': 'PASS', 'priceBook': 'PASS'}
    report = check_versions(ver1, ver2, architecture='MULTI_AGENT')
    assert report['groupBy'] == 'loopPolicy' and report['status'] == 'PASS' and report['architecture'] == 'MULTI_AGENT'


@pytest.mark.parametrize('changes, failing', [
    ({'ver2_change': {'runtime': lambda data: data['llmLimits'].update(max_tool_calls=99)}}, 'runtimeLimits'),
    ({'ver2_change': {'tools': lambda data: data.update(maxCallSeconds=999)}}, 'toolConfiguration'),
    ({'ver2_change': {'tools': lambda data: data['unit']['scopes'].append(
        {'name': 'extra-protected', 'kind': 'PROTECTED', 'protected_suite_ref': 'x', 'protected_files': {'tests/a.py': ''}})}},
     'protectedScopes'),
    ({'ver1_change': {'source': lambda data: data.update(commitHash='a' * 40)}}, 'startingSource'),
])
def test_version_differences_beyond_protected_tests_fail(tmp_path, changes, failing):
    from evaluation.parity import version_parity
    ver1, ver2 = version_files(tmp_path, **changes)
    rows = statuses(version_parity(ver1, ver2))
    assert rows[failing] == 'FAIL', rows


def test_protected_cases_must_belong_to_ver1_only(tmp_path):
    from evaluation.parity import version_parity
    ver1, ver2 = version_files(tmp_path)
    assert statuses(version_parity(ver1, {**ver2, 'cases': ver1['cases']}))['protectedCases'] == 'FAIL'
    assert statuses(version_parity({key: value for key, value in ver1.items() if key != 'cases'}, ver2))[
        'protectedCases'] == 'FAIL'


def test_cli_compares_versions_with_group_by(tmp_path, capsys):
    ver1, ver2 = version_files(tmp_path)
    text = lambda files: ','.join(f'{key}={value}' for key, value in files.items())  # noqa: E731
    out = tmp_path / 'results' / 'parity.json'
    assert main(['--group-by', 'loopPolicy', '--ver1', text(ver1), '--ver2', text(ver2), '--architecture', 'MULTI_AGENT',
                 '--out', str(out)]) == 0
    assert json.loads(out.read_text(encoding='utf-8'))['groupBy'] == 'loopPolicy'
    assert main(['--group-by', 'loopPolicy', '--ver1', text(ver1), '--ver2', text(ver2)]) == 1  # 구조가 없으면 거부
    assert 'PARITY_INPUT_INVALID' in capsys.readouterr().err
