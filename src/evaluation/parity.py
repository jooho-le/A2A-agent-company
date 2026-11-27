"""Single·Multi 실행 설정의 도구 권한·LLM 한도 동일 확인(개발정의서 11-A). 모델 호출·개발 실행은 하지 않는다.

개발정의서 11-A: Tool Set은 "Single 전체 Capability, Multi 역할별 분리하되 Union은 Single과 동일"이어야 하고, Single만 도구가
적으면 비교가 무효다. LLM 한도는 Run Configuration에 없고 Host 실행 설정에만 있어 Run 설정 비교(comparison_validity)로는
보이지 않는다. 그래서 세 가지를 따로 본다.

1. 도구 이름(정적): dev 정책 코드에서 Single 도구 = 네 역할 도구의 합집합인가. Single 단계별 도구와 같은 역할 도구의 차이는
   참고로 남긴다(합집합 규칙의 위반은 아니다).
2. 실행 설정 파일: 두 구조의 Host 설정 중 통제해야 하는 부분(모델·LLM 한도·시간 예산·도구 설정·시작 코드·보호 케이스·가격표)이
   같은가. 경로·포트·저장 위치처럼 구조마다 달라야 하는 값은 비교하지 않는다.
3. Run별 고정 해시(선택): Host DB의 run_host_policies에 Run마다 고정된 설정 해시가, 그 구조의 설정 파일로 dev 읽기 전용 검사
   (inspect_host_configuration)가 다시 만든 해시와 같은가. 같으면 그 Run이 바로 그 설정 파일로 돌았다는 뜻이다.

결과는 <results>/parity.json으로 두면 `experiment compare --group-by architecture`가 비교 조건에 반영한다.

Ver1·Ver2 비교(`--group-by loopPolicy`, 2026-11-14 점검 5)는 같은 구조의 두 설정을 비교한다. 순환용 보호 범위
(unit.scopes의 PROTECTED)와 보호 케이스 파일만 달라야 하고, 실행 설정(모델·LLM 한도·시간 예산)·나머지 도구 설정·
시작 코드·가격표는 같아야 한다. Run별 고정 해시는 Run의 버전(loopPolicy)에 맞는 설정 파일로 다시 만든 해시와 대조한다.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

RUNTIME_FIELDS = ('model', 'llmLimits', 'runtimeBudgetMs')      # 두 구조가 같아야 하는 실행 설정
TOOL_LOCATION_FIELDS = ('dependencyLockFile', 'dockerEndpoint')  # 위치 값(내용은 dependencyLockHash·imageReference로 비교)
SOURCE_LOCATION_FIELDS = ('repositoryPath',)
PHASE_ROLES = {'PLANNING': 'PLANNER', 'IMPLEMENTING': 'DEVELOPER', 'FIXING': 'DEVELOPER', 'QA': 'QA', 'SECURITY': 'SECURITY'}
FILE_KEYS = ('runtime', 'tools', 'source', 'cases', 'priceBook')


def _row(check, status, **values):
    return {'check': check, 'status': status, **values}


def _name(value):
    # dev Enum(AgentRole·SinglePhase)과 글자를 같은 이름으로 다룬다.
    return getattr(value, 'value', value)


def static_tool_parity(policy=None) -> dict:
    """dev 정책 코드의 도구 이름 합집합 규칙. 정책 코드를 읽지 못하면 미검증이다."""
    try:
        if policy is None:
            from mcp_tools.core import policy  # dev 정책(읽기만 한다)
        roles = {_name(role): tuple(names) for role, names in policy.ROLE_TOOL_NAMES.items()}
        single = tuple(policy.SINGLE_TOOL_NAMES)
        phases = {_name(phase): tuple(tools) for phase, tools in policy.SINGLE_PHASE_TOOL_ROLES.items()}
    except Exception:
        return _row('staticToolUnion', 'UNVERIFIED', reason='DEV_POLICY_UNAVAILABLE')
    union = set(name for names in roles.values() for name in names)
    missing, extra = sorted(union - set(single)), sorted(set(single) - union)
    beyond = sorted({name for tools in phases.values() for name in tools} - set(single))
    differences = {}
    for phase, tools in phases.items():
        expected = set(roles.get(PHASE_ROLES.get(phase, ''), ()))
        more, fewer = sorted(set(tools) - expected), sorted(expected - set(tools))
        if more or fewer:
            differences[phase] = {'moreThanRole': more, 'fewerThanRole': fewer}
    status = 'FAIL' if missing or extra or beyond else 'PASS'
    return _row('staticToolUnion', status, missingInSingle=missing, extraInSingle=extra, phaseToolsBeyondSingle=beyond,
                phaseDifferences=differences)


def _load(path):
    if path is None:
        return None
    document = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(document, dict):
        raise ValueError('CONFIGURATION_OBJECT_REQUIRED')
    return document


def _without(data, keys):
    return None if data is None else {key: value for key, value in data.items() if key not in keys}


def _differing(left, right):
    # 위쪽 키 단위로 다른 항목 이름(어느 한쪽에만 있는 키 포함)
    return sorted(key for key in set(left) | set(right) if left.get(key) != right.get(key))


def config_parity(single: dict, multi: dict) -> list:
    """두 구조의 Host 설정 파일 비교. 파일을 읽지 못하면 그 부분은 미검증이다."""
    try:
        loaded = {side: {key: _load(files.get(key)) for key in FILE_KEYS} for side, files in (('single', single), ('multi', multi))}
    except (OSError, ValueError, TypeError):
        return [_row('hostConfiguration', 'UNVERIFIED', reason='CONFIGURATION_UNREADABLE')]
    left, right = loaded['single'], loaded['multi']
    rows = []
    if left['runtime'] is None or right['runtime'] is None:
        rows.append(_row('runtimeLimits', 'UNVERIFIED', reason='RUNTIME_CONFIGURATION_MISSING'))
    else:
        fields = [field for field in RUNTIME_FIELDS if left['runtime'].get(field) != right['runtime'].get(field)]
        rows.append(_row('runtimeLimits', 'FAIL' if fields else 'PASS', differingFields=fields,
                         llmLimits=left['runtime'].get('llmLimits') if not fields else None))
    for check, key, ignored in (('toolConfiguration', 'tools', TOOL_LOCATION_FIELDS),
                                ('startingSource', 'source', SOURCE_LOCATION_FIELDS)):
        if left[key] is None or right[key] is None:
            rows.append(_row(check, 'UNVERIFIED', reason='CONFIGURATION_MISSING'))
            continue
        fields = _differing(_without(left[key], ignored), _without(right[key], ignored))
        rows.append(_row(check, 'FAIL' if fields else 'PASS', differingFields=fields))
    for check, key in (('protectedCases', 'cases'), ('priceBook', 'priceBook')):
        if left[key] is None and right[key] is None:
            rows.append(_row(check, 'PASS', note='BOTH_ABSENT'))
        elif left[key] is None or right[key] is None:
            rows.append(_row(check, 'FAIL', differingFields=['presence']))
        else:
            rows.append(_row(check, 'PASS' if left[key] == right[key] else 'FAIL',
                             differingFields=[] if left[key] == right[key] else _differing(left[key], right[key])))
    return rows


def _without_protected_scopes(tools):
    # Ver1·Ver2 도구 설정에서 순환용 보호 범위를 뺀 사본(나머지 도구 설정은 같아야 한다)
    if tools is None:
        return None
    copied = json.loads(json.dumps(tools))
    unit = copied.get('unit') if isinstance(copied.get('unit'), dict) else None
    if unit is not None and isinstance(unit.get('scopes'), list):
        unit['scopes'] = [scope for scope in unit['scopes']
                          if not (isinstance(scope, dict) and scope.get('kind') == 'PROTECTED')]
    return copied


def _protected_scope_names(tools):
    unit = (tools or {}).get('unit')
    if not isinstance(unit, dict) or not isinstance(unit.get('scopes'), list):
        return []
    scopes = [scope for scope in unit['scopes'] if isinstance(scope, dict) and scope.get('kind') == 'PROTECTED']
    if any(not isinstance(scope.get('name'), str) or not scope['name'] for scope in scopes):
        return []
    return sorted(scope['name'] for scope in scopes)


def version_parity(ver1: dict, ver2: dict) -> list:
    """같은 구조의 Ver1·Ver2 Host 설정 비교. 순환용 보호 범위와 보호 케이스 파일만 달라야 한다."""
    try:
        loaded = {side: {key: _load(files.get(key)) for key in FILE_KEYS} for side, files in (('ver1', ver1), ('ver2', ver2))}
    except (OSError, ValueError, TypeError):
        return [_row('hostConfiguration', 'UNVERIFIED', reason='CONFIGURATION_UNREADABLE')]
    left, right = loaded['ver1'], loaded['ver2']
    rows = []
    if left['runtime'] is None or right['runtime'] is None:
        rows.append(_row('runtimeLimits', 'UNVERIFIED', reason='RUNTIME_CONFIGURATION_MISSING'))
    else:
        fields = [field for field in RUNTIME_FIELDS if left['runtime'].get(field) != right['runtime'].get(field)]
        rows.append(_row('runtimeLimits', 'FAIL' if fields else 'PASS', differingFields=fields,
                         llmLimits=left['runtime'].get('llmLimits') if not fields else None))
    if left['tools'] is None or right['tools'] is None:
        rows.append(_row('toolConfiguration', 'UNVERIFIED', reason='CONFIGURATION_MISSING'))
        rows.append(_row('protectedScopes', 'UNVERIFIED', reason='CONFIGURATION_MISSING'))
    else:
        fields = _differing(_without(_without_protected_scopes(left['tools']), TOOL_LOCATION_FIELDS),
                            _without(_without_protected_scopes(right['tools']), TOOL_LOCATION_FIELDS))
        rows.append(_row('toolConfiguration', 'FAIL' if fields else 'PASS', differingFields=fields))
        ver1_scopes, ver2_scopes = _protected_scope_names(left['tools']), _protected_scope_names(right['tools'])
        rows.append(_row('protectedScopes', 'PASS' if ver1_scopes and not ver2_scopes else 'FAIL',
                         ver1=ver1_scopes, ver2=ver2_scopes))
    if left['source'] is None or right['source'] is None:
        rows.append(_row('startingSource', 'UNVERIFIED', reason='CONFIGURATION_MISSING'))
    else:
        fields = _differing(_without(left['source'], SOURCE_LOCATION_FIELDS), _without(right['source'], SOURCE_LOCATION_FIELDS))
        rows.append(_row('startingSource', 'FAIL' if fields else 'PASS', differingFields=fields))
    # 보호 케이스는 Ver1에만 있어야 한다(Ver2는 보호 테스트 없이 돈다).
    rows.append(_row('protectedCases', 'PASS' if left['cases'] is not None and right['cases'] is None else 'FAIL',
                     ver1Present=left['cases'] is not None, ver2Present=right['cases'] is not None))
    if left['priceBook'] is None and right['priceBook'] is None:
        rows.append(_row('priceBook', 'PASS', note='BOTH_ABSENT'))
    elif left['priceBook'] is None or right['priceBook'] is None:
        rows.append(_row('priceBook', 'FAIL', differingFields=['presence']))
    else:
        same = left['priceBook'] == right['priceBook']
        rows.append(_row('priceBook', 'PASS' if same else 'FAIL',
                         differingFields=[] if same else _differing(left['priceBook'], right['priceBook'])))
    return rows


def pinned_policy_hashes(database, run_ids) -> dict:
    """Host DB에서 Run별로 고정된 설정 해시를 읽기 전용으로 읽는다. 표나 Run이 없으면 None."""
    found = {str(run_id): None for run_id in run_ids}
    try:
        connection = sqlite3.connect(f'file:{Path(database).resolve()}?mode=ro', uri=True)
    except sqlite3.Error:
        return found
    try:
        table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_host_policies'").fetchone()
        if table is None:
            return found
        for run_id in found:
            row = connection.execute('SELECT policy_sha256 FROM run_host_policies WHERE run_id=?', (run_id,)).fetchone()
            found[run_id] = row[0] if row else None
    except sqlite3.Error:
        return {run_id: None for run_id in found}
    finally:
        connection.close()
    return found


def expected_policy_sha256(files: dict, architecture: str, inspect=None):
    """dev 읽기 전용 검사로 그 설정 파일 묶음의 해시를 다시 만든다. 검사에 실패하면 None."""
    try:
        if inspect is None:
            from agents.platform.host import inspect_host_configuration as inspect
        plan = inspect(runtime_file=files['runtime'], tools_file=files['tools'], source_file=files['source'],
                       protected_cases_file=files.get('cases'), price_book_file=files.get('priceBook'),
                       architecture=architecture)
        return plan.policy_sha256
    except Exception:
        return None


def _pinned_rows(runs, expected, label):
    # Run별 고정 해시를 그 Run의 이름표(구조 또는 버전)에 맞는 설정 파일 해시와 대조한다.
    rows = []
    for run in runs:
        pinned = pinned_policy_hashes(run['hostDatabase'], [run['runId']])[str(run['runId'])]
        want = expected.get(run.get(label))
        if pinned is None or want is None:
            rows.append(_row('pinnedPolicy', 'UNVERIFIED', runId=str(run['runId']),
                             reason='PINNED_POLICY_NOT_FOUND' if pinned is None else 'EXPECTED_POLICY_UNAVAILABLE'))
        else:
            rows.append(_row('pinnedPolicy', 'PASS' if pinned == want else 'FAIL', runId=str(run['runId'])))
    return rows


def _report(rows, runs, group_by, **extra):
    statuses = [row['status'] for row in rows]
    return {'schemaVersion': 1, 'groupBy': group_by,
            'status': 'FAIL' if 'FAIL' in statuses else 'UNVERIFIED' if 'UNVERIFIED' in statuses else 'PASS',
            'checks': rows, 'pinnedPolicyChecked': bool(runs), **extra,
            'limitations': ['Run별 고정 해시를 주지 않으면 설정 파일 비교만 한 것이며, 각 Run이 그 파일로 돌았다는 증명은 아니다.',
                            '단계별 도구 차이(phaseDifferences)는 합집합 규칙 위반이 아니라 참고 정보다.']}


def check_versions(ver1: dict, ver2: dict, *, architecture: str, runs=(), inspect=None) -> dict:
    """같은 구조(architecture)의 Ver1·Ver2 동일 조건 확인. runs의 각 항목은 loopPolicy를 가진다."""
    if architecture not in ('SINGLE_AGENT', 'MULTI_AGENT'):
        raise ValueError('ARCHITECTURE_INVALID')
    rows = version_parity(ver1, ver2)
    if runs:
        expected = {'IN_LOOP_AND_FINAL': expected_policy_sha256(ver1, architecture, inspect),
                    'FINAL_ONLY': expected_policy_sha256(ver2, architecture, inspect)}
        rows += _pinned_rows(runs, expected, 'loopPolicy')
    return _report(rows, runs, 'loopPolicy', architecture=architecture)


def check(single: dict, multi: dict, *, runs=(), inspect=None, policy=None) -> dict:
    rows = [static_tool_parity(policy), *config_parity(single, multi)]
    if runs:
        expected = {'SINGLE_AGENT': expected_policy_sha256(single, 'SINGLE_AGENT', inspect),
                    'MULTI_AGENT': expected_policy_sha256(multi, 'MULTI_AGENT', inspect)}
        rows += _pinned_rows(runs, expected, 'architecture')
    return _report(rows, runs, 'architecture')


def _files(text):
    # "runtime=PATH,tools=PATH,source=PATH[,cases=PATH][,priceBook=PATH]"
    files = {}
    for part in text.split(','):
        key, separator, value = part.partition('=')
        if not separator or key not in FILE_KEYS or not value or key in files:
            raise ValueError('FILES_ARGUMENT_INVALID')
        files[key] = value
    if not {'runtime', 'tools', 'source'} <= files.keys():
        raise ValueError('FILES_ARGUMENT_INVALID')
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--group-by', choices=('architecture', 'loopPolicy'), default='architecture')
    parser.add_argument('--single', help='runtime=..,tools=..,source=..[,cases=..][,priceBook=..] (architecture)')
    parser.add_argument('--multi', help='같은 형식 (architecture)')
    parser.add_argument('--ver1', help='같은 형식, Ver1 설정 (loopPolicy)')
    parser.add_argument('--ver2', help='같은 형식, Ver2 설정 (loopPolicy)')
    parser.add_argument('--architecture', choices=('SINGLE_AGENT', 'MULTI_AGENT'), help='Ver1·Ver2를 비교할 구조 (loopPolicy)')
    parser.add_argument('--runs', type=Path, help='[{"runId":..., "architecture"|"loopPolicy":..., "hostDatabase":...}] JSON 파일(선택)')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    try:
        label = 'architecture' if args.group_by == 'architecture' else 'loopPolicy'
        runs = json.loads(args.runs.read_text(encoding='utf-8')) if args.runs else ()
        if not isinstance(runs, (list, tuple)) or any(not isinstance(item, dict) or not {'runId', label, 'hostDatabase'}
                                                      <= item.keys() for item in runs):
            raise ValueError('RUNS_ARGUMENT_INVALID')
        if args.group_by == 'architecture':
            if not args.single or not args.multi:
                raise ValueError('FILES_ARGUMENT_INVALID')
            report = check(_files(args.single), _files(args.multi), runs=runs)
        else:
            if not args.ver1 or not args.ver2 or not args.architecture:
                raise ValueError('FILES_ARGUMENT_INVALID')
            report = check_versions(_files(args.ver1), _files(args.ver2), architecture=args.architecture, runs=runs)
    except (OSError, ValueError, TypeError):
        sys.stderr.write('PARITY_INPUT_INVALID\n')
        return 1
    text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding='utf-8')
    else:
        print(text, end='')
    return 0 if report['status'] == 'PASS' else 2


if __name__ == '__main__':
    sys.exit(main())
