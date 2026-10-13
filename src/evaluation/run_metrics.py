# 팀 시스템(dev Orchestrator)이 알려 주는 "개발 실행(Run) 기록"을 평가용 숫자(지표)로 바꾸는 파일이다.
# 또 Single(에이전트 1개)과 Multi(에이전트 4개) 두 구조를 "공정하게 비교해도 되는지" 확인하고, 구조별로 요약한다.
#
# 입력: 시스템 API가 돌려준 JSON(파이썬에서는 사전 dict)
#       /api/v1/runs/{id}, 그리고 그 아래 /events, /issues, /tool-attempts, /usage, /configuration
# 이 파일은 인터넷 요청을 직접 보내지 않는다. 실제로 가져와야 한다면 fetch_run_bundle에 "가져오는 함수(get)"를 넘겨 준다.
#
# 이 파일 전체에 적용되는 두 가지 원칙:
#   1) 측정하지 못한 값은 0이 아니라 None(모름)이다. 비용·토큰·시간 모두 마찬가지다.
#      "0원"과 "얼마인지 모름"은 전혀 다른 뜻이기 때문이다.
#   2) 걸린 시간은 "시작 사건 시각"과 "끝 사건 시각"의 차이로 잰다. 병렬로 돈 작업 시간을 더하지 않는다.
#      (QA 10초와 보안 10초가 동시에 돌았다면 전체 시간은 20초가 아니라 10초다.)
from decimal import Decimal  # 돈 계산용 정확한 소수 도구(0.1+0.2 같은 오차가 없다)
import json
from statistics import median  # 중앙값(가운데 값)을 구하는 도구

from .aggregation import _uuid4
from .common import ARCHITECTURES, aware_time

PAGE_LIMIT = 500  # 시스템 API가 한 번에 돌려주는 최대 개수(그보다 많으면 여러 번에 나눠 받아야 한다)
# 두 구조가 "똑같아야 하는" Run Configuration 항목들. 하나라도 다르면 공정한 비교가 아니다(개발정의서 11-A).
# 'model.provider'처럼 점(.)으로 이어 쓴 이름은 "model 안의 provider"를 뜻한다.
CONTROLLED_FIELDS = (
    'scenarioId', 'startingCommitHash', 'startingSnapshotSha256',
    'model.provider', 'model.modelId', 'model.modelRevision', 'model.temperature', 'model.seed',
    'limits.maxFixAttempts', 'limits.maxToolRetries', 'limits.runtimeBudgetMs',
    'environment.hardwareProfile', 'environment.networkPolicy', 'environment.allowedHosts',
    'protectedTestSuiteRef', 'scannerProfileRef',
)
# 아래 항목만 다를 때는 "비교 전체"가 무효가 아니라, 그 Run만 따로 표시하고 다시 실행한다
# (개발정의서 11-A의 "환경 digest 불일치" 규칙).
ENVIRONMENT_FIELDS = ('environment.containerImageDigest', 'environment.dependencyLockHash')


def fetch_run_bundle(get, run_id: str) -> dict:
    # 한 Run에 대한 자료를 시스템 API에서 모아 하나의 묶음(사전)으로 만든다.
    # get은 "get(경로, 쿼리 사전 또는 None) → 응답 JSON" 모양의 함수이다. (호출하는 쪽이 실제 HTTP 요청을 담당한다.)
    # /events는 한 번에 최대 500건만 주므로, 전체 개수(total)에 닿을 때까지 이어서 받는다.
    # 가져오다 실패하면 그 오류를 그대로 위로 올려 보낸다(일부만 받은 채 계속하지 않는다).
    _uuid4(run_id)
    base = f'/api/v1/runs/{run_id}'

    def paged(path, key):
        # 한 페이지씩 받아서 items에 계속 이어 붙이고, 다 받으면 (전체 목록, 전체 개수)를 돌려준다.
        items, offset = [], 0  # offset = 지금까지 받은 개수(다음에 어디서부터 받을지)
        while True:
            page = get(f'{base}/{path}', {'limit': PAGE_LIMIT, 'offset': offset})
            items.extend(page[key])
            offset += len(page[key])
            if offset >= page['total'] or not page[key]:
                return items, page['total']

    events, events_total = paged('events', 'events')
    return {
        'run': get(base, None),
        'events': events, 'eventsTotal': events_total,
        'issues': get(f'{base}/issues', None)['issues'],
        'toolAttempts': get(f'{base}/tool-attempts', None)['attempts'],
        # usage 요약은 "Run 전체"의 합계라서 기록 1건만 요청해도 요약값은 전체 기준으로 온다.
        'usage': get(f'{base}/usage', {'limit': 1, 'offset': 0}),
        'configuration': get(f'{base}/configuration', None),
    }


def _millis(start, end):
    # 두 시각의 차이를 밀리초(1/1000초)로 돌려준다. 둘 중 하나라도 모르면(None) 시간도 모르는 것이므로 None.
    # max(0, ...)는 혹시 끝이 시작보다 빠르게 기록돼도 음수가 나오지 않게 한다.
    return None if start is None or end is None else max(0, int((end - start).total_seconds() * 1000))


def run_metrics(bundle: dict, *, architecture: str | None = None) -> dict:
    # 한 Run의 자료 묶음(fetch_run_bundle의 결과 모양)에서 지표를 계산한다.
    # architecture는 평소엔 Run Configuration에서 읽는다. 거기에 구조 정보가 없을 때(예: Single 실행기)만 직접 지정한다.
    run = bundle['run']
    run_id = run['runId']
    _uuid4(run_id)
    notes = []  # 계산하면서 알게 된 "주의할 점"을 모아 두는 목록(결과에 같이 담아 돌려준다)
    configuration = bundle.get('configuration')
    # 묶음 안의 자료가 모두 "같은 Run의 것"인지 확인한다. 다른 Run의 자료가 섞이면 지표가 엉뚱해진다.
    for name, part in (('configuration', configuration), ('usage', bundle.get('usage'))):
        if part is not None and part.get('runId') != run_id:
            raise ValueError(f'{name} belongs to another run')
    for event in bundle['events']:
        if event.get('runId') != run_id:
            raise ValueError('event belongs to another run')
    for issue in bundle['issues']:
        if issue.get('runId') != run_id:
            raise ValueError('issue belongs to another run')

    settings = (configuration or {}).get('configuration') or {}
    architecture = architecture or settings.get('architecture')
    if architecture not in ARCHITECTURES:
        raise ValueError('architecture must be SINGLE_AGENT or MULTI_AGENT')

    # 시간 계산 준비: 사건(event) 기록의 시각 차이로 잰다.
    # 사건이 빠졌거나 사건 목록이 전부 오지 않았다면 시간을 믿을 수 없으니 None으로 둔다.
    events = bundle['events']
    total = bundle.get('eventsTotal')  # 시스템이 알려 준 "전체 사건 개수"
    if total is None:
        notes.append('EVENTS_COMPLETENESS_UNKNOWN')   # 전체 개수를 몰라서 다 받았는지 알 수 없음
    elif total != len(events):
        notes.append('EVENTS_INCOMPLETE')             # 받은 개수가 전체보다 적음

    def times(kind):
        # 특정 종류(eventType)의 사건들이 일어난 시각 목록
        return [aware_time(event['occurredAt'], 'occurredAt') for event in events if event['eventType'] == kind]

    started = times('RUN_STARTED')          # 실행 시작 사건
    start = started[0] if started else None
    if 'EVENTS_INCOMPLETE' in notes:
        start = None  # 사건이 일부만 왔다면 시작 시각도 믿지 않는다.
    builds, verdicts = times('BUILD_PASSED'), times('VERDICT_CREATED')   # 첫 빌드 성공 / 판정이 만들어진 사건들
    ends = sorted(times('RUN_FINISHED') + times('RUN_ABORTED'))          # 끝남 또는 중단 사건 (시간순 정렬)
    if start is None:
        notes.append('RUN_START_UNKNOWN')

    # 사용량·비용: 값이 없으면 None으로 둔다. 비용을 0으로 바꾸지 않는다.
    usage = bundle.get('usage')
    # usage 응답에는 "summary"(전체 요약)가 들어 있다. summary가 없는 모양이면 usage 자체를 요약으로 본다.
    summary = (usage or {}).get('summary', usage) if usage else None
    if summary is None:
        notes.append('USAGE_UNAVAILABLE')
    keys = ('modelCalls', 'toolCalls', 'knownTotalTokens', 'totalTokens', 'costUsd', 'knownCostUsd', 'costComplete')
    spent = {key: (summary or {}).get(key) for key in keys}
    spent['costComplete'] = bool(spent['costComplete'])  # "비용이 전부 확인됐는가"는 True/False로 확정한다.

    attempts = bundle['toolAttempts']
    # attempt 번호가 1 이상인 기록 = "다시 시도한" 횟수(0번이 최초 호출이다)
    retries = sum(1 for row in attempts if row['attempt']['attempt'] >= 1)
    issues = bundle['issues']
    reporters, categories = {}, {}  # 문제(issue)를 "누가 보고했나", "어떤 종류인가"별로 센다
    for issue in issues:
        reporters[issue['reporter']] = reporters.get(issue['reporter'], 0) + 1
        categories[issue['category']] = categories.get(issue['category'], 0) + 1
    # "수정 시작" 사건의 개수가 run의 수정 횟수(fixAttempt)와 같은지 교차 확인한다. (사건을 다 받았을 때만)
    fix_events = sum(1 for event in events if event['eventType'] == 'FIX_ATTEMPT_STARTED')
    if total == len(events) and fix_events != run['fixAttempt']:
        notes.append('FIX_ATTEMPT_COUNT_DIFFERS')

    return {
        'runId': run_id, 'scenarioId': run.get('scenarioId'), 'experimentId': settings.get('experimentId'),
        'architecture': architecture,
        'workflowStatus': run['status'], 'verdict': run.get('verdict'), 'codeVersion': run.get('codeVersion'),
        'terminationReason': run.get('terminationReason'),
        'fixAttempts': run['fixAttempt'], 'toolRetries': retries, 'toolAttemptRecords': len(attempts),
        'firstBuildPassMs': _millis(start, builds[0] if builds else None),    # 시작 ~ 첫 빌드 성공
        'verdictMs': _millis(start, verdicts[-1] if verdicts else None),      # 시작 ~ (마지막) 판정 생성
        'totalMs': _millis(start, ends[-1] if ends else None),                # 시작 ~ 끝남/중단
        'humanWaitMs': None,  # 사람을 기다린 시간. 시스템이 따로 기록하지 않아서 모른다(있으면 부르는 쪽이 채운다).
        **spent,  # 사용량·비용 항목들을 여기에 풀어서 넣는다
        'issues': len(issues), 'issuesByReporter': reporters, 'issuesByCategory': categories,
        'issuesFixed': sum(1 for issue in issues if issue.get('newCodeVersion') is not None),  # 수정으로 해결된 문제 수
        'notes': notes,
    }


def _distribution(values):
    # 숫자 목록의 개수(n), 중앙값, 최솟값, 최댓값을 한꺼번에 돌려준다. 목록이 비면 모두 None.
    return ({'n': 0, 'median': None, 'min': None, 'max': None} if not values
            else {'n': len(values), 'median': median(values), 'min': min(values), 'max': max(values)})


def _mean(values):
    # 평균. 목록이 비면 0으로 나눌 수 없으니 None.
    return sum(values) / len(values) if values else None


def experiment_summary(runs, planned_runs: dict, evaluations=()) -> dict:
    # 구조(SINGLE_AGENT / MULTI_AGENT)별로 실험 결과를 요약한다.
    # 성공률의 분모는 "시작하지 못한 Run까지 포함한 계획한 전체 Run 수"이다.
    #   (성공한 것만 세면 성공률이 부풀려지기 때문이다.)
    # 입력:
    #   runs         : run_metrics가 돌려준 결과들의 목록
    #   planned_runs : 구조별 계획한 Run 수. 예) {"SINGLE_AGENT": 5, "MULTI_AGENT": 5}
    #   evaluations  : 독립 평가 보고서 목록. sourceRunId로 해당 Run과 연결한다.
    runs = list(runs)
    if set(planned_runs) - set(ARCHITECTURES) or any(type(n) is not int or n < 1 for n in planned_runs.values()):
        raise ValueError('planned_runs must map architectures to positive integers')
    by_run = {}  # {Run 번호: 지표} 사전
    for item in runs:
        if item['runId'] in by_run:
            raise ValueError('duplicate run in experiment')
        by_run[item['runId']] = item
    by_evaluation = {}  # {Run 번호: 독립 평가 보고서} 사전
    for report in evaluations:
        run = by_run.get(report['sourceRunId'])
        if run is None:
            raise ValueError('evaluation refers to a run that is not in this experiment')
        if report['sourceRunId'] in by_evaluation:
            raise ValueError('more than one evaluation for the same run')
        if report['architecture'] != run['architecture']:
            raise ValueError('evaluation architecture differs from the run')
        by_evaluation[report['sourceRunId']] = report

    result = {}
    for architecture, planned in planned_runs.items():
        mine = [item for item in runs if item['architecture'] == architecture]  # 이 구조의 Run들
        if len(mine) > planned:
            raise ValueError('more runs than planned')
        # count("SUCCESS")처럼 부르면 "판정이 SUCCESS인 Run이 몇 개인지" 센다.
        count = lambda verdict: sum(1 for item in mine if item['verdict'] == verdict)
        aborted = sum(1 for item in mine if item['workflowStatus'] == 'ABORTED')                      # 중단된 Run
        in_progress = sum(1 for item in mine if item['verdict'] is None and item['workflowStatus'] != 'ABORTED')  # 판정 전(진행 중·일시정지)
        success, failed = count('SUCCESS'), count('FAIL')
        # 비용·토큰은 "계획한 Run 전부가 확인되었을 때만" 전체 합계를 낸다. 아니면 확인된 부분의 합만 따로 낸다.
        known_cost = [Decimal(item['knownCostUsd']) for item in mine if item['knownCostUsd'] is not None]
        cost_complete = len(mine) == planned and all(item['costComplete'] for item in mine)
        total_cost = sum(known_cost, Decimal(0)) if known_cost else None
        tokens_complete = len(mine) == planned and all(item['totalTokens'] is not None for item in mine)
        known_tokens = sum(item['knownTotalTokens'] or 0 for item in mine)
        reports = [by_evaluation[item['runId']] for item in mine if item['runId'] in by_evaluation]
        # 통과율이 None(측정 못 함)인 보고서는 평균에서 뺀다. 0%로 취급하면 평균이 낮아지기 때문이다.
        requirement_rates = [r['requiredRequirementPassRate'] for r in reports if r['requiredRequirementPassRate'] is not None]
        check_rates = [r['requiredCheckPassRate'] for r in reports if r['requiredCheckPassRate'] is not None]
        result[architecture] = {
            'plannedRuns': planned, 'recordedRuns': len(mine), 'notStarted': planned - len(mine),
            'success': success, 'fail': failed, 'unverified': count('UNVERIFIED'), 'humanReview': count('HUMAN_REVIEW'),
            'aborted': aborted, 'inProgress': in_progress,
            # 성공률 = 성공 ÷ 계획 전체 / Verified Rate = (성공+실패) ÷ 계획 전체 / 미검증률 = 미검증 ÷ 계획 전체
            'successRate': success / planned, 'verifiedRate': (success + failed) / planned,
            'unverifiedRate': count('UNVERIFIED') / planned,
            # 성공한 Run들이 판정까지 걸린 시간의 분포(개수·중앙값·최소·최대)
            'successVerdictMs': _distribution([item['verdictMs'] for item in mine
                                               if item['verdict'] == 'SUCCESS' and item['verdictMs'] is not None]),
            'meanFixAttempts': _mean([item['fixAttempts'] for item in mine]),
            'meanToolRetries': _mean([item['toolRetries'] for item in mine]),
            'modelCalls': sum(item['modelCalls'] or 0 for item in mine),
            'runsWithoutUsage': sum(1 for item in mine if item['modelCalls'] is None),  # 사용량을 모르는 Run 수
            'totalTokens': known_tokens if tokens_complete else None, 'knownTotalTokens': known_tokens,
            'costUsd': format(total_cost, 'f') if cost_complete and total_cost is not None else None,
            'knownCostUsd': format(total_cost, 'f') if total_cost is not None else None,
            'runsWithUnknownCost': sum(1 for item in mine if not item['costComplete']),
            'evaluation': {
                'evaluatedRuns': len(reports), 'unevaluatedRuns': len(mine) - len(reports),
                'meanRequirementPassRate': _mean(requirement_rates), 'meanCheckPassRate': _mean(check_rates),
                'unmeasuredRuns': len(reports) - len(requirement_rates),  # 통과율을 측정하지 못한 보고서 수
                'evaluationVerdicts': {verdict: sum(1 for r in reports if r['evaluationVerdict'] == verdict)
                                       for verdict in ('PASS', 'FAIL', 'UNVERIFIED')},
                'missedDefects': sum(1 for r in reports if (r.get('sourceRun') or {}).get('missedDefect')),
            },
        }
    return result


def _lookup(document, dotted):
    # 'model.provider'처럼 점으로 이어 쓴 이름을 따라 사전 안쪽 값을 꺼낸다. 중간에 없으면 None.
    value = document
    for part in dotted.split('.'):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def comparison_validity(entries) -> dict:
    # Single과 Multi 결과를 나란히 놓고 비교해도 되는지 확인한다.
    # entries: 목록. 각 항목 = {"architecture": 구조 이름,
    #                          "configuration": GET .../configuration 응답,
    #                          "evaluation": 독립 평가 보고서 또는 None}
    #
    # "같은 조건, 같은 시험지, 같은 환경"에서 나온 결과여야 공정한 비교다.
    # 구조별로 하나씩만 보지 않고 "모든 Run"을 서로 비교한다. 같은 구조의 반복 실행끼리 달라도 위반이다.
    #
    # 위반의 종류(severity):
    #   INVALID_COMPARISON = 비교 자체가 무효(모델이나 한도가 다르다 등)
    #   RERUN_RUN          = 환경 digest만 다름. 비교 전체가 아니라 그 Run만 따로 표시하고 다시 실행해야 한다.
    entries = list(entries)
    if len({item['architecture'] for item in entries}) < 2:
        raise ValueError('comparison needs both architectures')
    violations, warnings = [], []  # violations = 위반 목록, warnings = 주의 목록

    def distinct(label, read, severity, subset=entries):
        # subset의 모든 Run에서 read(항목)로 값을 읽어 모은다. 서로 다른 값이 둘 이상이면 위반으로 기록한다.
        # 구조별로 어떤 값들이 나왔는지 함께 적는다. (입력 순서가 달라도 결과가 같다.)
        seen = {}  # {구조: {값의 JSON 글자: 값}}
        for item in subset:
            value = read(item)
            seen.setdefault(item['architecture'], {})[json.dumps(value, sort_keys=True, default=str)] = value
        if len({key for values in seen.values() for key in values}) > 1:
            violations.append({'field': label, 'severity': severity, 'values': {
                architecture: (list(values.values())[0] if len(values) == 1
                               else [values[key] for key in sorted(values)])
                for architecture, values in sorted(seen.items())}})

    def settings(item):
        # Run Configuration 응답 안의 "configuration"(실제 설정 묶음). 없으면 빈 사전.
        return item['configuration'].get('configuration') or {}

    for field in CONTROLLED_FIELDS:
        if field == 'scenarioId':
            distinct(field, lambda item: item['configuration'].get('scenarioId'), 'INVALID_COMPARISON')
        else:
            # f=field로 지금 field 값을 함수에 붙잡아 둔다. (안 그러면 반복이 끝난 뒤의 마지막 값만 쓰게 되는 파이썬의 함정이 있다.)
            distinct(field, lambda item, f=field: _lookup(settings(item), f), 'INVALID_COMPARISON')
    for field in ENVIRONMENT_FIELDS:
        distinct(field, lambda item, f=field: _lookup(settings(item), f), 'RERUN_RUN')
    distinct('scenarioContractSha256', lambda item: item['configuration'].get('scenarioContractSha256'),
             'INVALID_COMPARISON')
    distinct('experimentId', lambda item: settings(item).get('experimentId'), 'INVALID_COMPARISON')

    # 평가 보고서가 있는 Run들만 따로 모아서 시험지(suite) 해시와 환경 지문을 비교한다.
    evaluated = [item for item in entries if item.get('evaluation') is not None]
    if len(evaluated) != len(entries):
        warnings.append({'field': 'evaluation', 'detail': 'SOME_RUNS_HAVE_NO_EVALUATION'})
    distinct('suite.sha256', lambda item: item['evaluation']['suite']['sha256'], 'INVALID_COMPARISON', evaluated)
    for field in ('containerImageDigest', 'dependencyLockHash'):
        distinct(f'evaluation.{field}', lambda item, f=field: item['evaluation']['expectedManifest'][f],
                 'RERUN_RUN', evaluated)
    # 실험으로 선언되지 않았거나, 실행 순서·준비 상태를 기록하지 않은 Run은 재현성이 떨어지므로 주의로 알린다.
    for item in entries:
        if settings(item).get('experimentId') is None:
            warnings.append({'field': 'experimentId', 'detail': 'RUN_IS_NOT_A_DECLARED_EXPERIMENT',
                             'runId': item['configuration'].get('runId')})
        elif settings(item).get('executionOrder') is None or settings(item).get('warmupState') is None:
            warnings.append({'field': 'executionOrder', 'detail': 'ORDER_OR_WARMUP_NOT_RECORDED',
                             'runId': item['configuration'].get('runId')})
    # valid가 True가 되려면 INVALID_COMPARISON 위반이 하나도 없어야 한다. (RERUN_RUN은 그 Run만 다시 하면 된다.)
    return {'valid': not any(item['severity'] == 'INVALID_COMPARISON' for item in violations),
            'violations': violations, 'warnings': warnings}
