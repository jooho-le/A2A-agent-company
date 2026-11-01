# 팀 시스템(dev Orchestrator)이 알려 주는 "개발 실행(Run) 기록"을 평가용 숫자(지표)로 바꾸는 파일이다.
# 또 두 실험 조건을 "공정하게 비교해도 되는지" 확인하고, 조건별로 요약한다. 비교 기준(group_by)은 두 가지다.
#   - 'architecture': Single(에이전트 1개)과 Multi(에이전트 4개) 구조 비교(D6, Single 실행기는 주호 개발). 보호 테스트
#                     조건(protectedTestSuiteRef)이 같은 Run들, 즉 같은 방안 안에서만 비교한다.
#   - 'loopPolicy'  : 같은 Multi 구조에서 Ver1(순환 안 보호 테스트 + 최종, IN_LOOP_AND_FINAL)과
#                     Ver2(최종만, FINAL_ONLY) 비교(10.29 회의록의 방안 1·2를 회의 뒤 방침으로 멀티에 적용). 두 조건의 의도한 차이는 순환 안 보호 테스트
#                     (protectedTestSuiteRef) 하나뿐이고, 정책은 이름표가 아니라 동결 설정의 그 값으로 확인한다.
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
from .scoring import exam_score, practice_effect

LOOP_POLICIES = ('IN_LOOP_AND_FINAL', 'FINAL_ONLY')  # Ver1(순환 안 보호 테스트 + 최종), Ver2(최종만)
GROUPINGS = {'architecture': ARCHITECTURES, 'loopPolicy': LOOP_POLICIES}  # 비교 기준별로 허용하는 이름표


def loop_policy(settings) -> str:
    # 동결 Run 설정(configuration)에서 순환 안 테스트 정책을 읽는다. 보호 테스트 참조가 있으면 Ver1, 없으면 Ver2.
    return 'IN_LOOP_AND_FINAL' if (settings or {}).get('protectedTestSuiteRef') else 'FINAL_ONLY'

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
# 값이 없어도 되는 선택 항목. (모델 버전·seed를 제공하지 않는 모델이 있고, 네트워크를 막은 환경은 허용 주소가 비어 있다.
# 보호 테스트 참조가 없으면 "순환 안 보호 테스트 없음"이라는 분명한 조건이다(10.29 회의록 방안 2, Ver2). 선택 항목이어도
# 비교하는 Run들 사이에서 같아야 한다는 규칙은 그대로라, 한쪽만 있으면 비교가 무효다.)
OPTIONAL_FIELDS = frozenset({'model.modelRevision', 'model.seed', 'environment.allowedHosts', 'protectedTestSuiteRef'})
# 아래 항목만 다를 때는 "비교 전체"가 무효가 아니라, 그 Run만 따로 표시하고 다시 실행한다
# (개발정의서 11-A의 "환경 digest 불일치" 규칙).
ENVIRONMENT_FIELDS = ('environment.containerImageDigest', 'environment.dependencyLockHash')


def fetch_run_bundle(get, run_id: str) -> dict:
    # 한 Run에 대한 자료를 시스템 API에서 모아 하나의 묶음(사전)으로 만든다.
    # get은 "get(경로, 쿼리 사전 또는 None) → 응답 JSON" 모양의 함수이다. (호출하는 쪽이 실제 HTTP 요청을 담당한다.)
    # /events는 한 번에 최대 500건만 주므로, 전체 개수(total)에 닿을 때까지 이어서 받는다.
    # get이 예외를 내면 그대로 위로 올려 보낸다. total에 닿기 전에 빈 페이지가 오면 더 진행할 수 없어
    # 받은 목록과 total을 반환한다. run_metrics가 이를 EVENTS_INCOMPLETE로 표시하고 시간 계산을 막는다.
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

    # 받는 도중에 Run이 바뀌면(재개·종료) 예전 사건 목록과 새 상태가 섞인다. 그래서 Run 상태를 처음과 끝에 두 번 받고,
    # 사건 전체 개수도 끝에 다시 확인한다. 셋이 같을 때만 한 시점의 자료로 본다. 다르면 FETCH_ATTEMPTS번까지 다시 받고,
    # 끝내 같지 않으면 snapshotConsistent=False로 표시해 run_metrics가 시간을 계산하지 않게 한다.
    for _attempt in range(FETCH_ATTEMPTS):
        before = get(base, None)
        events, events_total = paged('events', 'events')
        collected = {
            'issues': get(f'{base}/issues', None)['issues'],
            'toolAttempts': get(f'{base}/tool-attempts', None)['attempts'],
            # usage 요약은 "Run 전체"의 합계라서 기록 1건만 요청해도 요약값은 전체 기준으로 온다.
            'usage': get(f'{base}/usage', {'limit': 1, 'offset': 0}),
            'configuration': get(f'{base}/configuration', None),
        }
        after = get(base, None)
        tail = get(f'{base}/events', {'limit': 1, 'offset': 0})['total']
        consistent = _run_state(before) == _run_state(after) and tail == events_total
        if consistent:
            break
    return {'run': after, 'events': events, 'eventsTotal': events_total, **collected, 'snapshotConsistent': consistent}


FETCH_ATTEMPTS = 3  # Run이 받는 도중에 바뀌었을 때 다시 받는 최대 횟수
# 사용량 응답에서 비용 계산 조건(단가표·계산 방식·통화)을 나타내는 항목
PRICE_BOOK_KEYS = ('currency', 'priceBookId', 'priceBookVersion', 'priceBookSha256', 'calculationVersion')


def _run_state(run):
    # 받는 도중에 바뀌었는지 비교할 Run 상태 값들
    return tuple(run.get(key) for key in ('status', 'verdict', 'codeVersion', 'fixAttempt', 'updatedAt'))


def _terminal_events_match(run, events):
    # Run의 최종 상태가 받은 사건 목록의 끝과 이어지는지 본다(다른 시점의 상태와 사건이 섞이지 않았는지).
    #   - 판정이 있으면 마지막 VERDICT_CREATED 사건이 있고, 그 사건의 코드 버전·상태(사건에 적혀 있으면)가 Run과 같다.
    #   - Run이 FINISHED면 그 판정 사건 뒤에 RUN_FINISHED 사건이 있다.
    if run.get('verdict') is None:
        return True
    positions = [index for index, event in enumerate(events) if event.get('eventType') == 'VERDICT_CREATED']
    if not positions:
        return False
    last = events[positions[-1]]
    for field, value in (('codeVersion', run.get('codeVersion')), ('workflowState', run.get('status'))):
        if last.get(field) is not None and last[field] != value:
            return False
    return run.get('status') != 'FINISHED' or any(
        event.get('eventType') == 'RUN_FINISHED' for event in events[positions[-1] + 1:])


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
    # 구조(Single/Multi)는 실행을 시작할 때 동결한 설정이 기준이다. 인자로 다른 구조를 주면 결과가 거짓이 되므로 거부한다.
    # 인자(architecture)는 동결 설정에 구조 정보가 없을 때만 쓸 수 있다.
    frozen = settings.get('architecture')
    if frozen is not None and architecture is not None and architecture != frozen:
        raise ValueError('architecture differs from the frozen run configuration')
    architecture = frozen or architecture
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
    if total is None or total != len(events):
        start = None  # 사건이 일부만 왔거나 전체 개수를 모르면 시작·끝 사건이 모두 왔다고 확정하지 않는다.
    if bundle.get('snapshotConsistent') is False:
        notes.append('RUN_CHANGED_DURING_FETCH')  # 받는 도중에 Run이 바뀌어 한 시점의 자료가 아님
        start = None
    elif not _terminal_events_match(run, events):
        notes.append('TERMINAL_EVENTS_DO_NOT_MATCH_RUN')  # 최종 상태와 판정·종료 사건이 이어지지 않음(다른 시점의 자료)
        start = None
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
    # 비용을 계산한 단가표. 단가표가 다르면 같은 토큰도 비용이 달라지므로 비교 때 함께 본다(PRICE_BOOK_KEYS).
    price_book = {key: (summary or {}).get(key) for key in PRICE_BOOK_KEYS}
    spent['priceBook'] = price_book if any(value is not None for value in price_book.values()) else None

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
    # 협업 시간(전달·병렬·에이전트 밖 시간·사람 대기)은 사건을 모두 받았을 때만 계산한다.
    collaboration = collaboration_metrics(events) if start is not None else None

    return {
        'runId': run_id, 'scenarioId': run.get('scenarioId'), 'experimentId': settings.get('experimentId'),
        'architecture': architecture,
        # 순환 안 테스트 정책(Ver1·Ver2). 동결 설정을 받지 못했으면 모른다(None).
        'loopPolicy': loop_policy(settings) if configuration is not None else None,
        'workflowStatus': run['status'], 'verdict': run.get('verdict'), 'codeVersion': run.get('codeVersion'),
        'terminationReason': run.get('terminationReason'),
        'fixAttempts': run['fixAttempt'], 'toolRetries': retries, 'toolAttemptRecords': len(attempts),
        'firstBuildPassMs': _millis(start, builds[0] if builds else None),    # 시작 ~ 첫 빌드 성공
        'verdictMs': _millis(start, verdicts[-1] if verdicts else None),      # 시작 ~ (마지막) 판정 생성
        'totalMs': _millis(start, ends[-1] if ends else None),                # 시작 ~ 끝남/중단
        # 사람을 기다린 시간(HUMAN_REVIEW·WAITING_INPUT에 머문 끝난 구간의 합). 상태 변화 사건이 없거나 사건이 모자라면 None.
        'humanWaitMs': collaboration['humanWaitMs'] if collaboration else None,
        'collaboration': collaboration,
        **spent,  # 사용량·비용 항목들을 여기에 풀어서 넣는다
        'issues': len(issues), 'issuesByReporter': reporters, 'issuesByCategory': categories,
        # newCodeVersion은 Developer에게 "수정을 요청한 시점"에 기록된다. 요청했다고 해결된 것은 아니므로 둘을 나눠 센다.
        #   issuesFixRequested : 수정을 요청한 문제 수
        #   issuesFixed        : 수정한 코드를 다시 검증해서 통과(PASS)로 확인된 문제 수 (재검증 결과가 없거나 FAIL·UNVERIFIED이면 세지 않는다)
        'issuesFixRequested': sum(1 for issue in issues if issue.get('newCodeVersion') is not None),
        'issuesFixed': sum(1 for issue in issues if issue.get('revalidationResult') == 'PASS'),
        'notes': notes,
    }


def _distribution(values):
    # 숫자 목록의 개수(n), 중앙값, 최솟값, 최댓값을 한꺼번에 돌려준다. 목록이 비면 모두 None.
    return ({'n': 0, 'median': None, 'min': None, 'max': None} if not values
            else {'n': len(values), 'median': median(values), 'min': min(values), 'max': max(values)})


def _mean(values):
    # 평균. 목록이 비면 0으로 나눌 수 없으니 None.
    return sum(values) / len(values) if values else None


def experiment_summary(runs, planned_runs: dict, evaluations=(), *, group_by: str = 'architecture') -> dict:
    # 실험 조건별로 결과를 요약한다. group_by='architecture'면 구조(SINGLE_AGENT / MULTI_AGENT)별,
    # 'loopPolicy'면 순환 안 테스트 정책(IN_LOOP_AND_FINAL=Ver1 / FINAL_ONLY=Ver2)별이다.
    # 성공률의 분모는 "시작하지 못한 Run까지 포함한 계획한 전체 Run 수"이다.
    #   (성공한 것만 세면 성공률이 부풀려지기 때문이다.)
    # 입력:
    #   runs         : run_metrics가 돌려준 결과들의 목록
    #   planned_runs : 조건별 계획한 Run 수. 예) {"SINGLE_AGENT": 5, "MULTI_AGENT": 5} 또는 {"IN_LOOP_AND_FINAL": 5, "FINAL_ONLY": 5}
    #   evaluations  : 독립 평가 보고서 목록(최종 시험지 채점). sourceRunId로 해당 Run과 연결한다.
    runs = list(runs)
    if group_by not in GROUPINGS:
        raise ValueError('group_by must be architecture or loopPolicy')
    if (set(planned_runs) - set(GROUPINGS[group_by])
            or any(type(n) is not int or n < 1 for n in planned_runs.values())):
        raise ValueError('planned_runs must map experiment groups to positive integers')
    unplanned = {item.get(group_by) for item in runs} - set(planned_runs)
    if unplanned:
        # 계획에 적지 않은 구조의 Run이 섞여 있으면 요약에서 조용히 빠지게 된다(성공·실패·비용이 사라진다). 그래서 거부한다.
        raise ValueError(f'runs belong to architectures that are not planned: {sorted(unplanned)}')
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
        # 같은 Run이라도 코드는 수정할 때마다 버전이 바뀐다. 이전 버전의 코드를 채점한 보고서를 최종 코드의 평가로 집계하면
        # 고치기 전 결과가 최종 결과처럼 보인다. Run의 최종 코드 버전과 보고서가 채점한 코드 버전이 같고 둘 다 확인될 때만 받는다.
        evaluated_version = (report.get('expectedManifest') or {}).get('codeVersion')
        if type(run.get('codeVersion')) is not int or evaluated_version != run['codeVersion']:
            raise ValueError('evaluation was not made for the final code version of the run')
        by_evaluation[report['sourceRunId']] = report

    result = {}
    for group, planned in planned_runs.items():
        mine = [item for item in runs if item.get(group_by) == group]  # 이 조건의 Run들
        if len(mine) > planned:
            raise ValueError('more runs than planned')
        # count("SUCCESS")처럼 부르면 "판정이 SUCCESS인 Run이 몇 개인지" 센다.
        count = lambda verdict: sum(1 for item in mine if item['verdict'] == verdict)
        aborted = sum(1 for item in mine if item['workflowStatus'] == 'ABORTED')                      # 중단된 Run
        in_progress = sum(1 for item in mine if item['verdict'] is None and item['workflowStatus'] != 'ABORTED')  # 판정 전(진행 중·일시정지)
        success, failed = count('SUCCESS'), count('FAIL')
        # 비용·토큰·모델 호출 수는 계획한 Run 전부가 확인되었을 때만 전체 합계를 낸다.
        # 모델 호출 수는 확인된 부분 합을 knownModelCalls로 구분하며, 한 건도 측정하지 못했으면 None이다.
        known_cost = [Decimal(item['knownCostUsd']) for item in mine if item['knownCostUsd'] is not None]
        cost_complete = len(mine) == planned and all(item['costComplete'] for item in mine)
        total_cost = sum(known_cost, Decimal(0)) if known_cost else None
        tokens_complete = len(mine) == planned and all(item['totalTokens'] is not None for item in mine)
        # 확인된 토큰 수의 합. 한 Run도 사용량을 모르면 0이 아니라 None이다(원칙 1). 모델 호출 수·비용과 같은 기준이다.
        measured_tokens = [item['knownTotalTokens'] for item in mine if item['knownTotalTokens'] is not None]
        known_tokens = sum(measured_tokens) if measured_tokens else None
        measured_calls = [item['modelCalls'] for item in mine if item['modelCalls'] is not None]
        known_calls = sum(measured_calls) if measured_calls else None
        calls_complete = len(mine) == planned and len(measured_calls) == len(mine)
        reports = [by_evaluation[item['runId']] for item in mine if item['runId'] in by_evaluation]
        # 통과율이 None(측정 못 함)인 보고서는 평균에서 뺀다. 0%로 취급하면 평균이 낮아지기 때문이다.
        requirement_rates = [r['requiredRequirementPassRate'] for r in reports if r['requiredRequirementPassRate'] is not None]
        check_rates = [r['requiredCheckPassRate'] for r in reports if r['requiredCheckPassRate'] is not None]
        scores = [exam_score(r) for r in reports]  # 요구사항별 같은 비중 점수(코드·환경 신분을 확인한 보고서만 점수가 있음)
        qualified = [item for item in mine if item['runId'] in by_evaluation
                     and by_evaluation[item['runId']]['evaluationVerdict'] == 'PASS']  # 최종 독립 평가 PASS인 Run
        result[group] = {
            'plannedRuns': planned, 'recordedRuns': len(mine), 'notStarted': planned - len(mine),
            'success': success, 'fail': failed, 'unverified': count('UNVERIFIED'), 'humanReview': count('HUMAN_REVIEW'),
            'aborted': aborted, 'inProgress': in_progress,
            # 성공률 = 성공 ÷ 계획 전체 / Verified Rate = (성공+실패) ÷ 계획 전체 / 미검증률 = 미검증 ÷ 계획 전체
            'successRate': success / planned, 'verifiedRate': (success + failed) / planned,
            'unverifiedRate': count('UNVERIFIED') / planned,
            # 성공한 Run들이 판정까지 걸린 시간의 분포(개수·중앙값·최소·최대)
            'successVerdictMs': _distribution([item['verdictMs'] for item in mine
                                               if item['verdict'] == 'SUCCESS' and item['verdictMs'] is not None]),
            # 성공했지만 시간을 믿을 수 없는 Run 수(사건 일부 누락·받는 도중 변경 등). 위 분포에서 빠진 수다.
            'successRunsWithUnknownTime': sum(1 for item in mine if item['verdict'] == 'SUCCESS' and item['verdictMs'] is None),
            'meanFixAttempts': _mean([item['fixAttempts'] for item in mine]),
            'meanToolRetries': _mean([item['toolRetries'] for item in mine]),
            'modelCalls': known_calls if calls_complete else None,
            'knownModelCalls': known_calls, 'modelCallsComplete': calls_complete,
            'runsWithoutUsage': sum(1 for item in mine if item['modelCalls'] is None),  # 사용량을 모르는 Run 수
            'totalTokens': known_tokens if tokens_complete else None, 'knownTotalTokens': known_tokens,
            'costUsd': format(total_cost, 'f') if cost_complete and total_cost is not None else None,
            'knownCostUsd': format(total_cost, 'f') if total_cost is not None else None,
            'runsWithUnknownCost': sum(1 for item in mine if not item['costComplete']),
            # 비용을 계산한 단가표들(다르면 비용 차이에 단가 차이가 섞인다. 토큰·호출 수는 단가와 무관하다).
            'priceBooks': [dict(book) for book in sorted({tuple(sorted(item['priceBook'].items()))
                                                          for item in mine if item.get('priceBook')})],
            'evaluation': {
                'evaluatedRuns': len(reports), 'unevaluatedRuns': len(mine) - len(reports),
                'meanRequirementPassRate': _mean(requirement_rates), 'meanCheckPassRate': _mean(check_rates),
                'unmeasuredRuns': len(reports) - len(requirement_rates),  # 통과율을 측정하지 못한 보고서 수
                'evaluationVerdicts': {verdict: sum(1 for r in reports if r['evaluationVerdict'] == verdict)
                                       for verdict in ('PASS', 'FAIL', 'UNVERIFIED')},
                # sourceRun은 선택 참고 정보이므로 실제 연결된 Run의 판정과 확인된 필수 FAIL로 계산한다.
                # sourceRun이 없거나 그 안의 missedDefect·finalVerdict가 오래된 값이어도 집계 결과를 바꾸지 않는다.
                'missedDefects': sum(1 for r in reports
                                     if by_run[r['sourceRunId']]['verdict'] == 'SUCCESS'
                                     and r.get('confirmedFailureIds')),
                # 최종 시험 점수의 평균(점수를 못 낸 보고서는 빼고 그 수를 따로 적는다). 미검증이 있던 Run의 점수는
                # 잠정 하한이라 lowerBoundRuns로 함께 보인다. 보안 위반이 확인된 Run 수는 점수와 따로 센다.
                'meanExamScore': _mean([s['score'] for s in scores if s['score'] is not None]),
                'meanCompletionRate': _mean([s['completionRate'] for s in scores if s['completionRate'] is not None]),
                'unscoredRuns': sum(1 for s in scores if s['score'] is None),
                'lowerBoundRuns': sum(1 for s in scores if s['scoreIsLowerBound']),
                'securityViolationRuns': sum(1 for s in scores if s['securityViolations']),
                # 같은 품질 기준을 만족한 결과에 도달한 시간(10.29 회의록 방안 1의 비교 지표). 기준은 최종 독립 평가 PASS
                # (채점 시험지의 필수 검사 모두 통과·코드·환경 신분 확인)이고, 공식 비교 전에 고정해 두고 바꾸지 않는다.
                # 위 successVerdictMs(Orchestrator가 SUCCESS라고 한 Run)와 달리, 최종 평가에서 FAIL·미검증인 Run의 시간은
                # 넣지 않는다. 비율의 분모는 계획한 전체 Run이다(시작 못 함·채점 못 함·FAIL·미검증 모두 기준 미달로 셈).
                'qualifiedRuns': len(qualified),
                'qualifiedRate': len(qualified) / planned,
                'qualifiedVerdictMs': _distribution([item['verdictMs'] for item in qualified if item['verdictMs'] is not None]),
                'qualifiedRunsWithUnknownTime': sum(1 for item in qualified if item['verdictMs'] is None),
            },
        }
    return result


A2A_STEP_END_STATES = frozenset({'TASK_STATE_COMPLETED', 'TASK_STATE_FAILED', 'TASK_STATE_CANCELED', 'TASK_STATE_REJECTED',
                                 'TASK_STATE_INPUT_REQUIRED', 'TASK_STATE_AUTH_REQUIRED'})
A2A_ACTIVE_STATES = frozenset({'TASK_STATE_SUBMITTED', 'TASK_STATE_WORKING'})  # 에이전트가 일하는 중인 상태
A2A_STATE_EVENTS = ('A2A_TASK_RECEIVED', 'A2A_TASK_STATE_CHANGED', 'A2A_TASK_UPDATED')  # A2A 상태를 싣는 사건
HUMAN_WAIT_STATES = frozenset({'HUMAN_REVIEW', 'WAITING_INPUT'})
AGENT_ROLES = ('PLANNER', 'DEVELOPER', 'QA', 'SECURITY')


def _merged_length(intervals):
    # 시간 구간들의 합집합 길이(겹치는 부분은 한 번만 센다). 병렬로 일한 시간을 두 번 세지 않으려고 쓴다.
    total, end = 0, None
    for start, finish in sorted(intervals):
        if end is None or start > end:
            total, end = total + (finish - start), finish
        elif finish > end:
            total, end = total + (finish - end), finish
    return total


def collaboration_metrics(events) -> dict:
    # 협업 시간 지표(개발정의서 11-A "Agent Coordination Overhead"와 사람 대기 human_wait_ms). 사건 기록이 모두 있을 때만 부른다.
    #   llmMs·toolMs        : 역할별 LLM 호출(LLM_CALL_FINISHED)·도구 호출(MCP_TOOL_FINISHED)의 durationMs 합과 호출 수.
    #                         병렬로 겹친 시간도 각각 더한 "작업 시간 합"이라 전체 경과 시간으로 쓰지 않는다.
    #   handoffMs           : Step마다 일을 넘긴 시각(A2A_MESSAGE_SENT) ~ 에이전트가 받은 시각(첫 A2A_TASK_RECEIVED)의 합
    #   agentStepSumMs      : Step 구간 길이의 단순 합. 구간은 일을 넘기기 시작(첫 시작·재개 요청 A2A_MESSAGE_SENT)부터
    #                         끝 상태(완료·실패·취소·거절) 또는 중단 상태(입력·인증 대기)까지다. 첫 응답(A2A_TASK_RECEIVED)이
    #                         이미 끝 상태여도 그 시각에 닫는다(dev는 이때 더 폴링하지 않는다). 사람을 기다리는 동안은 구간 밖이고,
    #                         재개하면(같은 Step, attempt+1) 새 구간이 열린다. 폴링 시간 초과는 에이전트가 계속 일하므로 끊지 않는다.
    #   agentActiveMs       : 같은 구간들의 합집합 길이. agentStepSumMs - agentActiveMs = 병렬 실행으로 겹친 시간(parallelOverlapMs)
    #   outsideAgentMs      : (실행 시작 ~ 마지막 판정) - agentActiveMs. 어느 에이전트도 일하지 않은 시간
    #                         (Orchestrator의 판단·Snapshot 고정·빌드·Issue 처리, 사람 대기 등)
    #   humanWaitMs         : HUMAN_REVIEW·WAITING_INPUT 상태에 들어갔다가 벗어난 구간의 합. 아직 그 상태로 끝났으면
    #                         그 구간은 더하지 않고 humanWaitOpen=True. 상태 변화 사건이 하나도 없으면 None(알 수 없음)
    # 마지막 구간이 끝 상태 없이 열려 있는 Step(멈춤·진행 중)은 stepsWithoutEnd로 세고 그 구간은 계산에서 뺀다.
    stamped = sorted(((aware_time(event['occurredAt'], 'occurredAt'), index, event) for index, event in enumerate(events)),
                     key=lambda item: (item[0], item[1]))

    def millis(delta):
        # 시간 차이(timedelta)를 정수 밀리초로 바꾼다.
        return int(delta.total_seconds() * 1000)

    llm = {role: {'calls': 0, 'ms': 0} for role in AGENT_ROLES}
    tools = {role: {'calls': 0, 'ms': 0} for role in AGENT_ROLES}
    steps = {}
    for moment, _index, event in stamped:
        kind, actor = event.get('eventType'), event.get('actor')
        bucket = llm if kind == 'LLM_CALL_FINISHED' else tools if kind == 'MCP_TOOL_FINISHED' else None
        if bucket is not None and actor in bucket and isinstance(event.get('durationMs'), int):
            bucket[actor]['calls'] += 1
            bucket[actor]['ms'] += event['durationMs']
        step_id = event.get('workflowStepId')
        if not step_id:
            continue
        # open: 지금 열린 구간의 시작 시각(없으면 None), segments: 닫힌 구간 [(시작, 끝)] 목록
        step = steps.setdefault(step_id, {'open': None, 'segments': [], 'sent': None, 'received': None, 'role': None})
        state = event.get('a2aTaskState')
        # 구간 열기: 일을 넘기기 시작했거나(첫 시작·재개 요청), 중단 뒤 폴링에서 다시 일하는 상태가 보였을 때
        if step['open'] is None and (kind in ('WORKFLOW_STEP_DISPATCH_STARTED', 'A2A_MESSAGE_SENT')
                                     or (kind in A2A_STATE_EVENTS and state in A2A_ACTIVE_STATES and step['segments'])):
            step['open'] = moment
        if kind == 'A2A_MESSAGE_SENT' and step['sent'] is None:
            step['sent'] = moment
        if kind == 'A2A_TASK_RECEIVED' and step['received'] is None and step['sent'] is not None:
            step['received'] = moment
        # 구간 닫기: 첫 응답·상태 변화·갱신이 끝 상태나 중단 상태를 실었을 때
        if kind in A2A_STATE_EVENTS and state in A2A_STEP_END_STATES and step['open'] is not None:
            step['segments'].append((step['open'], moment))
            step['open'] = None
        if actor in AGENT_ROLES and step['role'] is None:
            step['role'] = actor
    intervals = [(start, finish) for step in steps.values() for start, finish in step['segments'] if finish >= start]
    by_role = {}
    for step in steps.values():
        if step['segments']:
            role = by_role.setdefault(step['role'] or 'UNKNOWN', {'steps': 0, 'ms': 0})
            role['steps'] += 1
            role['ms'] += sum(millis(finish - start) for start, finish in step['segments'])
    step_sum = sum(millis(finish - start) for start, finish in intervals)
    active = int(_merged_length([(start.timestamp() * 1000, finish.timestamp() * 1000) for start, finish in intervals]))
    starts = [moment for moment, _index, event in stamped if event.get('eventType') == 'RUN_STARTED']
    verdicts = [moment for moment, _index, event in stamped if event.get('eventType') == 'VERDICT_CREATED']
    wall = millis(verdicts[-1] - starts[0]) if starts and verdicts else None

    changes = [(moment, event.get('workflowState')) for moment, _index, event in stamped
               if event.get('eventType') == 'WORKFLOW_STATE_CHANGED' and event.get('workflowState')]
    human, waiting_since = (0 if changes else None), None
    for moment, state in changes:
        if state in HUMAN_WAIT_STATES and waiting_since is None:
            waiting_since = moment
        elif state not in HUMAN_WAIT_STATES and waiting_since is not None:
            human += millis(moment - waiting_since)
            waiting_since = None
    return {
        'llm': llm, 'tools': tools, 'stepsByRole': by_role,
        'handoffMs': sum(millis(step['received'] - step['sent']) for step in steps.values() if step['received'] is not None),
        'agentStepSumMs': step_sum, 'agentActiveMs': active, 'parallelOverlapMs': step_sum - active,
        'outsideAgentMs': wall - active if wall is not None else None, 'wallToVerdictMs': wall,
        'stepsWithoutEnd': sum(1 for step in steps.values() if step['open'] is not None),
        'humanWaitMs': human, 'humanWaitOpen': waiting_since is not None,
    }


def regression_count(version_results: dict) -> dict:
    # Regression Count(개발정의서 11-A): 수정으로 새롭게 발생한 공통 테스트 실패 수.
    # version_results: {코드 버전(1부터): {검사 번호: 상태}}. 같은 공통 보호 테스트를 버전마다 돌린 결과다.
    #   상태는 독립 평가 보고서의 검사 상태(PASS·FAIL·ERROR·NOT_RUN·NOT_APPLICABLE)를 그대로 쓴다.
    # 버전 k에서 PASS였던 검사가 바로 다음 버전 k+1에서 FAIL이면 Regression 1건이다.
    # 둘 중 한 버전이라도 PASS·FAIL이 아닌 검사는 비교할 수 없어 uncomparedChecks로 센다. 그런 검사가 있으면
    # 숨은 Regression이 있을 수 있으므로 complete=False이고 regressionCount는 확인된 건수(하한)다.
    # 버전이 1부터 빈틈없이 이어지지 않으면 ValueError로 멈춘다(중간 버전을 건너뛰면 원인 버전을 알 수 없다).
    versions = sorted(version_results)
    if not versions or any(type(v) is not int for v in versions) or versions != list(range(1, len(versions) + 1)):
        raise ValueError('Version results must cover consecutive code versions from 1')
    regressions, uncompared = [], 0
    for before, after in zip(versions, versions[1:]):
        old, new = version_results[before], version_results[after]
        for check_id in sorted(set(old) | set(new)):
            pair = (old.get(check_id), new.get(check_id))
            if not all(status in ('PASS', 'FAIL') for status in pair):
                uncompared += 1
            elif pair == ('PASS', 'FAIL'):
                regressions.append({'fromVersion': before, 'toVersion': after, 'checkId': check_id})
    return {'regressionCount': len(regressions), 'regressions': regressions,
            'comparedVersionPairs': len(versions) - 1, 'uncomparedChecks': uncompared,
            'complete': uncompared == 0}


def regression_from_reports(reports) -> dict:
    # 코드 버전마다 같은 보호 묶음으로 채점한 독립 평가 보고서들에서 Regression Count를 바로 계산한다.
    #   (D5 evaluate_final_snapshot을 버전별 Snapshot마다 돌린 결과의 'report'들을 넣는다.)
    # 버전은 보고서의 expectedManifest.codeVersion(채점한 Snapshot의 코드 버전)이고, 검사 상태는 보고서의 checks에서 읽는다.
    # 보고서들은 같은 Run·같은 보호 묶음(suite 지문)이어야 하고 버전마다 하나여야 한다. 어기면 ValueError로 멈춘다
    # (다른 묶음으로 채점한 버전끼리 비교하면 Regression이 아니라 시험 변경이 섞인다).
    # 돌려주는 값: regression_count 결과 + runId·suiteSha256·finalCodeVersion(요약·비교에서 다른 Run·시험지·버전의 결과와
    #             섞이지 않았는지 대조용)
    results, run_ids, suites = {}, set(), set()
    for report in reports:
        version = (report.get('expectedManifest') or {}).get('codeVersion')
        if type(version) is not int or version in results:
            raise ValueError('Each report needs its own code version')
        run_ids.add(report.get('sourceRunId'))
        suites.add((report.get('suite') or {}).get('sha256'))
        results[version] = {check['testId']: check['status'] for check in report.get('checks', [])}
    if len(run_ids) != 1 or len(suites) != 1 or None in run_ids | suites:
        raise ValueError('Reports must come from one Run and one protected suite')
    return {**regression_count(results), 'runId': run_ids.pop(), 'suiteSha256': suites.pop(),
            'finalCodeVersion': max(results)}


def _lookup(document, dotted):
    # 'model.provider'처럼 점으로 이어 쓴 이름을 따라 사전 안쪽 값을 꺼낸다. 중간에 없으면 None.
    value = document
    for part in dotted.split('.'):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def loop_policy_effect(runs, evaluations, references, pairs=None, unscored=()) -> dict:
    # Ver1·Ver2의 차이의 차이를 계산한다(scoring.practice_effect). Run마다 최종 시험지 보고서(evaluations)와 같은 코드를
    # 순환용 시험지로 참고 채점한 보고서(references, D5 결과의 referenceReport)를 짝지어 정책별로 모은다.
    # runs: run_metrics 결과 목록(loopPolicy가 있어야 한다). pairs: {Run 번호: 쌍 번호}(선택).
    # unscored: 채점하지 못한 Run 번호들(짝지은 실험이면 그 짝 Run도 확정 계산에서 뺀다).
    # 모든 최종 보고서는 같은 시험지(suite 지문), 모든 참고 보고서도 같은 시험지여야 하고 둘은 서로 달라야 한다
    # (다른 날의 다른 순환용 시험지로 채점한 결과가 섞이거나, 두 보고서를 바꿔 넣으면 거부).
    policies = {item['runId']: item.get('loopPolicy') for item in runs}
    exam_suites = {(report.get('suite') or {}).get('sha256') for report in evaluations}
    reference_suites = {(reference.get('suite') or {}).get('sha256') for reference in references}
    if len(exam_suites) > 1 or len(reference_suites) > 1 or None in exam_suites | reference_suites:
        raise ValueError('evaluations and references must each use one protected suite')
    if exam_suites & reference_suites:
        raise ValueError('reference reports must use the practice suite, not the scored exam')
    reference_by_run = {}
    for reference in references:
        if reference['sourceRunId'] in reference_by_run:
            raise ValueError('more than one reference report for the same run')
        reference_by_run[reference['sourceRunId']] = reference
    groups = {policy: [] for policy in LOOP_POLICIES}
    for report in evaluations:
        policy, reference = policies.get(report['sourceRunId']), reference_by_run.get(report['sourceRunId'])
        if policy not in groups or reference is None:
            raise ValueError('every evaluation needs a run with a loop policy and its reference report')
        if reference.get('expectedManifest') != report.get('expectedManifest'):
            raise ValueError('reference report scored different code than the evaluation')
        groups[policy].append((report, reference))
    return practice_effect(groups, pairs, unscored)


def comparison_validity(entries, *, group_by: str = 'architecture') -> dict:
    # 두 실험 조건의 결과를 나란히 놓고 비교해도 되는지 확인한다.
    #   group_by='architecture': Single과 Multi 비교(조건은 구조 하나만 달라야 한다)
    #   group_by='loopPolicy'  : Ver1과 Ver2 비교(같은 구조, 순환 안 보호 테스트 참조 하나만 달라야 한다)
    # entries: 목록. 각 항목 = {"architecture": 구조 이름, ("loopPolicy": Ver1·Ver2 이름표,)
    #                          ("pairId": 같은 조건으로 함께 실행한 Ver1·Ver2 한 쌍의 번호,)
    #                          "configuration": GET .../configuration 응답,
    #                          "evaluation": 독립 평가 보고서 또는 None}
    #
    # "같은 조건, 같은 시험지, 같은 환경"에서 나온 결과여야 공정한 비교다.
    # 구조별로 하나씩만 보지 않고 "모든 Run"을 서로 비교한다. 같은 구조의 반복 실행끼리 달라도 위반이다.
    #
    # 위반의 종류(severity):
    #   INVALID_COMPARISON = 비교 자체가 무효(모델이나 한도가 다르다 등)
    #   RERUN_RUN          = 환경 digest만 다르거나, 평가 보고서가 그 Run이 동결한 환경이 아닌 곳에서 채점되었다.
    #                        비교 전체가 아니라 그 Run만 따로 표시하고 다시 실행해야 한다.
    entries = list(entries)
    if group_by not in GROUPINGS:
        raise ValueError('group_by must be architecture or loopPolicy')
    if len({item.get(group_by) for item in entries}) < 2:
        raise ValueError('comparison needs both groups')
    violations, warnings = [], []  # violations = 위반 목록, warnings = 주의 목록
    # Ver1·Ver2 비교에서 보호 테스트 참조는 의도한 차이라서 "같아야 하는 조건"과 "빠지면 안 되는 조건"에서 뺀다.
    controlled = tuple(field for field in CONTROLLED_FIELDS
                       if not (group_by == 'loopPolicy' and field == 'protectedTestSuiteRef'))

    def distinct(label, read, severity, subset=entries):
        # subset의 모든 Run에서 read(항목)로 값을 읽어 모은다. 서로 다른 값이 둘 이상이면 위반으로 기록한다.
        # 구조별로 어떤 값들이 나왔는지 함께 적는다. (입력 순서가 달라도 결과가 같다.)
        seen = {}  # {조건: {값의 JSON 글자: 값}}
        for item in subset:
            value = read(item)
            seen.setdefault(item.get(group_by), {})[json.dumps(value, sort_keys=True, default=str)] = value
        if len({key for values in seen.values() for key in values}) > 1:
            violations.append({'field': label, 'severity': severity, 'values': {
                architecture: (list(values.values())[0] if len(values) == 1
                               else [values[key] for key in sorted(values)])
                for architecture, values in sorted(seen.items())}})

    def settings(item):
        # Run Configuration 응답 안의 "configuration"(실제 설정 묶음). 없으면 빈 사전.
        return item['configuration'].get('configuration') or {}

    def flag(field, severity, values):
        # 어긋난 조건 하나를 기록한다(설정 이름, 심각도, Run별 값).
        violations.append({'field': field, 'severity': severity, 'values': values})

    def reported(item, field):
        # 평가 보고서의 "기준 신분증(expectedManifest)"에 적힌 환경 지문. 보고서에 그 칸이 없으면 None(모름).
        return ((item['evaluation'].get('expectedManifest') or {}).get(field))

    # 0) 입력 자체가 서로 맞는지 먼저 확인한다. 이름표(구조)와 실제 자료가 다르면 무엇을 비교하는지 알 수 없다.
    wrong_environment, failed_manifest = {}, {}  # 평가 환경이 어긋난 Run들 / 신분증 확인에 실패한 Run들 (Run 번호 → 상세)
    for item in entries:
        label = item['architecture']
        if label not in ARCHITECTURES:
            raise ValueError('entry architecture must be SINGLE_AGENT or MULTI_AGENT')
        if group_by == 'loopPolicy':
            # Ver1·Ver2 이름표는 동결 설정의 보호 테스트 참조와 맞아야 한다(이름표만 바꿔 붙이지 못하게).
            if item.get('loopPolicy') not in LOOP_POLICIES:
                raise ValueError('entry loopPolicy must be IN_LOOP_AND_FINAL or FINAL_ONLY')
            if item['loopPolicy'] != loop_policy(settings(item)):
                flag('loopPolicy', 'INVALID_COMPARISON', {str(item['configuration'].get('runId')): {
                    'label': item['loopPolicy'], 'frozen': loop_policy(settings(item))}})
        run_id = item['configuration'].get('runId')
        frozen = settings(item).get('architecture')
        if frozen is None:
            warnings.append({'field': 'architecture', 'detail': 'ARCHITECTURE_NOT_FROZEN', 'runId': run_id})
        elif frozen != label:
            flag('architecture', 'INVALID_COMPARISON', {str(run_id): {'label': label, 'frozen': frozen}})
        evaluation = item.get('evaluation')
        if evaluation is not None:
            if evaluation.get('architecture') != label:
                flag('evaluation.architecture', 'INVALID_COMPARISON',
                     {str(run_id): {'label': label, 'evaluation': evaluation.get('architecture')}})
            if evaluation.get('sourceRunId') != run_id:
                # 다른 Run의 평가 보고서를 붙이면 점수가 엉뚱한 실행의 것이 된다.
                flag('evaluation.sourceRunId', 'INVALID_COMPARISON',
                     {str(run_id): {'run': run_id, 'evaluation': evaluation.get('sourceRunId')}})
            # 평가 보고서가 "이 Run이 동결한 환경"을 기준으로 채점되었는지 확인한다.
            # 두 구조의 보고서가 서로 같은 환경 지문을 적었더라도, Run 설정에 동결된 환경과 다르면 엉뚱한 환경에서 채점된 것이다.
            # (동결된 값이 아예 없는 경우는 아래 "필수 조건 누락" 검사에서 따로 잡는다.)
            differing = {}
            for field in ('containerImageDigest', 'dependencyLockHash'):
                frozen_value = _lookup(settings(item), f'environment.{field}')
                if frozen_value is not None and reported(item, field) != frozen_value:
                    differing[field] = {'frozen': frozen_value, 'evaluation': reported(item, field)}
            if differing:
                wrong_environment[str(run_id)] = differing
            # 보고서 스스로 "결과가 나온 코드·환경이 기준과 다르다"고 밝힌 경우(metadataMatches가 False).
            # 이때 보고서는 모든 검사를 NOT_RUN으로 처리했으므로 비교에 쓸 측정값이 없다. 그 Run의 평가를 다시 해야 한다.
            manifest_check = evaluation.get('manifestCheck')
            if isinstance(manifest_check, dict) and manifest_check.get('metadataMatches') is False:
                failed_manifest[str(run_id)] = {'errorCode': manifest_check.get('errorCode'),
                                                'mismatchedFields': manifest_check.get('mismatchedFields')}
    # 같은 종류의 위반은 Run마다 따로 적지 않고, 해당하는 Run들을 한 항목에 모아 적는다.
    if wrong_environment:
        flag('evaluation.environment', 'RERUN_RUN', wrong_environment)
    if failed_manifest:
        flag('evaluation.manifestCheck', 'RERUN_RUN', failed_manifest)
    # 비교에 꼭 필요한 조건이 하나라도 비어 있으면 "같은 조건"인지 알 수 없다.
    # (양쪽이 모두 비어 있다고 해서 같은 조건은 아니다. 확인된 것이 없을 뿐이다.)
    for item in entries:
        run_id = item['configuration'].get('runId')
        missing = [field for field in controlled + ENVIRONMENT_FIELDS
                   if field not in OPTIONAL_FIELDS
                   and (item['configuration'].get('scenarioId') if field == 'scenarioId'
                        else _lookup(settings(item), field)) is None]
        if item['configuration'].get('scenarioContractSha256') is None:
            missing.append('scenarioContractSha256')
        if missing:
            flag('missing required conditions', 'INVALID_COMPARISON', {str(run_id): missing})

    if group_by == 'loopPolicy':
        # 구조는 두 조건이 같아야 한다. Ver1 Run끼리는 같은 보호 테스트 묶음을 써야 한다.
        distinct('architecture', lambda item: item['architecture'], 'INVALID_COMPARISON')
        distinct('protectedTestSuiteRef', lambda item: settings(item).get('protectedTestSuiteRef'), 'INVALID_COMPARISON',
                 [item for item in entries if item['loopPolicy'] == 'IN_LOOP_AND_FINAL'])
        # 같은 조건으로 함께 실행한 쌍(pairId)마다 Ver1·Ver2가 하나씩 있어야 한다(시간대·순서 차이가 한쪽에 몰리지 않게).
        pairs = {}
        for item in entries:
            if item.get('pairId') is None:
                warnings.append({'field': 'pairId', 'detail': 'RUN_IS_NOT_PAIRED',
                                 'runId': item['configuration'].get('runId')})
            else:
                pairs.setdefault(item['pairId'], []).append(item['loopPolicy'])
        broken = {str(pair): sorted(policies) for pair, policies in pairs.items() if sorted(policies) != sorted(LOOP_POLICIES)}
        if broken:
            flag('pairId', 'INVALID_COMPARISON', broken)
    for field in controlled:
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

    # 비용 계산 단가표가 Run마다 다르면 비용(달러) 비교에 단가 차이가 섞인다. 비교 전체를 막지는 않고 주의로 알린다
    # (정확도·시간·토큰 비교에는 영향이 없다). 항목에 priceBook(run_metrics 결과의 값)을 넣었을 때만 본다.
    books = {json.dumps(item.get('priceBook'), sort_keys=True) for item in entries if item.get('priceBook')}
    if len(books) > 1:
        warnings.append({'field': 'priceBook', 'detail': 'PRICE_BOOK_DIFFERS',
                         'values': [json.loads(book) for book in sorted(books)]})
    # 평가 보고서가 있는 Run들만 따로 모아서 시험지(suite) 해시와 환경 지문을 비교한다.
    evaluated = [item for item in entries if item.get('evaluation') is not None]
    if len(evaluated) != len(entries):
        warnings.append({'field': 'evaluation', 'detail': 'SOME_RUNS_HAVE_NO_EVALUATION'})
    distinct('suite.sha256', lambda item: item['evaluation']['suite']['sha256'], 'INVALID_COMPARISON', evaluated)
    for field in ('containerImageDigest', 'dependencyLockHash'):
        distinct(f'evaluation.{field}', lambda item, f=field: reported(item, f), 'RERUN_RUN', evaluated)
    # 준비 상태(warm-up)가 Run마다 다르면 처리시간·비용 비교에 캐시·초기화 비용의 차이가 섞인다. 비교를 막지는 않지만 주의로 알린다.
    # (실행 순서 숫자는 Run마다 달라야 정상이라서 이 검사에 넣지 않는다.)
    states = {}
    for item in entries:
        state = settings(item).get('warmupState')
        if state is not None:
            states.setdefault(item.get(group_by), set()).add(state)
    if len(set().union(*states.values()) if states else ()) > 1:
        warnings.append({'field': 'warmupState', 'detail': 'WARMUP_STATE_DIFFERS',
                         'values': {architecture: sorted(values) for architecture, values in sorted(states.items())}})
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
