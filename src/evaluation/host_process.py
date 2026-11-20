"""현재 Host의 원본 사건/보고서를 과정 집계 입력으로 변환한다. 없는 종류의 0건을 추측하지 않는다."""
from itertools import product

from .evidence_metrics import _unique


def derive_process_evidence(export):
    bundle, trace = export['bundle'], export.get('trace') or {}
    run_id = bundle['run']['runId']
    evidence = {'schemaVersion': 1, 'runId': run_id, 'completeKinds': [], 'completeRoutes': [],
                'completeChannels': [], 'contradictionsComplete': False, 'events': [],
                'source': 'HOST_RECORDS', 'limitations': []}
    def add(identity, kind, **fields):
        evidence['events'].append({'eventId': identity, 'runId': run_id, 'kind': kind, **fields})

    events = bundle.get('events')
    complete = (isinstance(events, list) and bundle.get('eventsTotal') == len(events)
                and bundle.get('snapshotConsistent') is True)
    try:
        unique = _unique(events or [], 'eventId')
        if any(row.get('runId') != run_id for row in unique): raise ValueError()
    except (ValueError, TypeError):
        unique, complete = [], False
    fixes = []
    for row in unique:
        if row.get('eventType') not in ('FIX_ATTEMPT_STARTED', 'FIX_COMPLETED'):
            continue
        attempt = row.get('attempt')
        if type(attempt) is not int or attempt < 1:
            complete = False
            continue
        fixes.append(row)
        add('host:' + row['eventId'], 'REPAIR', route='IMPLEMENTATION', operationId=f'fix:{attempt}',
            roundId=f'repair:{attempt}', action='STARTED' if row['eventType'] == 'FIX_ATTEMPT_STARTED' else 'COMPLETED')
    starts = {row['attempt'] for row in fixes if row['eventType'] == 'FIX_ATTEMPT_STARTED'}
    expected = bundle['run'].get('fixAttempt')
    if complete and type(expected) is int and 0 <= expected <= 3 and starts == set(range(1, expected + 1)):
        evidence['completeRoutes'].append('IMPLEMENTATION')

    telemetry = trace.get('telemetry')
    tool_complete = isinstance(telemetry, list) and trace.get('telemetryTotal') == len(telemetry)
    tool_attempts = {}
    for row in telemetry or []:
        binding, detail = row.get('binding') or {}, row.get('detail') or {}
        if binding.get('eventType') != 'MCP_TOOL_CALLED': continue
        operation, attempt = detail.get('logicalCallId'), detail.get('toolAttempt')
        if binding.get('runId') != run_id or not isinstance(operation, str) or type(attempt) is not int or attempt < 0:
            tool_complete = False
            continue
        if attempt:
            add(f'tool:{operation}:{attempt}', 'RETRY', channel='TOOL', operationId=operation, attempt=attempt)
        tool_attempts.setdefault(operation, set()).add(attempt)
    if any(attempts != set(range(max(attempts) + 1)) for attempts in tool_attempts.values()):
        tool_complete = False
    if tool_complete: evidence['completeChannels'].append('TOOL')

    observations = trace.get('observations') or {}
    policy = observations.get('policy') or {}
    from .evidence_metrics import _time
    from orchestrator.observability import scopes_complete
    policy_complete = False
    try:
        calls = [row for row in unique if row.get('eventType') in ('A2A_MESSAGE_SENT', 'LLM_MODEL_CALLED')]
        policy_complete = (complete and observations.get('runId') == run_id
            and observations.get('snapshotConsistent') is True and policy.get('schemaVersion') == 1
            and observations.get('scopesComplete') is True
            and scopes_complete(observations.get('scopes'))
            and any(row['scope'] == 'HOST_DISPATCH' for row in observations['scopes'])
            and policy.get('runId') == run_id and policy.get('producer') == 'OWNED_HOST_OBSERVATION_V1'
            and policy.get('architecture') == bundle['configuration']['configuration']['architecture']
            and policy.get('scope') == 'OWNED_AUTOMATIC_SEGMENT'
            and bundle['run'].get('status') in ('FINISHED', 'ABORTED', 'WAITING_INPUT', 'HUMAN_REVIEW')
            and not any(row.get('eventType') == 'WORKFLOW_RESUMED' for row in unique)
            and all(_time(policy['pinnedAt']) <= _time(row['occurredAt']) for row in calls))
    except (ValueError, KeyError, TypeError):
        pass
    evidence['observationPolicy'] = {'verified': policy_complete, 'scope': 'OWNED_AUTOMATIC_SEGMENT'}
    if policy_complete:
        # Historical LLM=DISABLED receipts predate correction instrumentation
        # (including the one-shot credential correction). They cannot prove 0.
        for channel in ('A2A',):
            if policy.get('automaticRetries', {}).get(channel) == 'DISABLED':
                evidence['completeChannels'].append(channel)
        from agents.llm.recovery import POLICY
        if policy.get('automaticRetries', {}).get('LLM') == POLICY:
            from .recovery_evidence import recovery_assessment
            recovery = recovery_assessment(bundle, trace)
            evidence['llmRecovery'] = recovery
            if recovery['status'] == 'PASS':
                evidence['completeChannels'].append('LLM')
                for call in recovery['regenerations']:
                    add(f"llm-regeneration:{call['sequence']}", 'RETRY', channel='LLM',
                        operationId=f"engine:{call['operationSequence']}", attempt=call['correctionAttempt'])
        if policy.get('repairRoutes', {}).get('REPLAN') == 'UNSUPPORTED':
            evidence['completeRoutes'].append('REPLAN')
        evidence['limitations'].append('LLM 재생성은 원인·호출 연결 기록이 있는 실제 재호출만 집계한다. '
                                      '일반 도구 후속 호출·polling·예산 소진으로 실행되지 않은 복구 안내는 재시도가 아니다. '
                                      '자동 A2A 재전송·재계획은 지원하지 않는다.')
    else:
        evidence['limitations'].append('실행 전 관찰 정책 또는 전체 자동 구간 사건이 없어 A2A/LLM 재시도·재계획의 0건을 확정하지 않음.')

    # Central admission/output rejection and canonical build/verdict gates.
    gates = {'PROCESS_GATE_REJECTED': 'ORCHESTRATOR_OUTPUT', 'BUILD_FAILED': 'BUILD',
             'BUILD_UNVERIFIED': 'BUILD', 'BUILD_FAILED_FIX_LIMIT_REACHED': 'BUILD',
             'VALIDATION_DECISION_FIX_REQUIRED': 'VALIDATION', 'VALIDATION_DECISION_HUMAN_REVIEW': 'VALIDATION',
             'SAME_ISSUE_REQUIRES_REVIEW': 'REPAIR', 'SAME_ISSUE_REPEAT_LIMIT_REACHED': 'REPAIR',
             'FIX_DISPATCH_NOT_CONFIGURED': 'REPAIR',
             # docs/live-api/05 22절: 모델 행동으로 멈춰 마지막 측정 결과로 끝남(Host 관문의 거부로 센다).
             'AUTOMATIC_STOP_FINISHED': 'ORCHESTRATOR_OUTPUT'}
    for row in unique:
        kind = row.get('eventType')
        final_rejected = kind == 'VALIDATION_DECISION_FINISHED' and bundle['run'].get('verdict') == 'FAIL'
        if kind not in gates and not final_rejected:
            continue
        add('gate:' + row['eventId'], 'GATE_REJECTION', rejectionId=row['eventId'], stage=gates.get(kind, 'VALIDATION'),
            operationId=row.get('workflowStepId') or f"version:{row.get('codeVersion')}:{row.get('attempt')}")
    if policy_complete:
        evidence['completeKinds'].append('GATE_REJECTION')

    # Test changes are measured from immutable captured QA inputs and registered
    # snapshot test-file bytes, not inferred from model prose or extra calls.
    steps = (export.get('handoff') or {}).get('steps') or []
    by_step = {row.get('workflowStepId'): row for row in steps}

    def validation_round(step, source_id=None):
        # WorkflowStep.attempt는 A2A 재개 횟수다. 수정 차수는 Host가 만든
        # STEP_CREATED와 (RE)VALIDATION_STARTED 사건의 attempt에서만 읽는다.
        if step is None or step.get('phase') not in ('QA', 'SECURITY'):
            return None
        rounds = set()
        for created in unique:
            if (created.get('eventType') != 'WORKFLOW_STEP_CREATED'
                    or created.get('workflowStepId') != step.get('workflowStepId')
                    or created.get('codeVersion') != step.get('codeVersion')):
                continue
            attempt = created.get('attempt')
            if type(attempt) is not int or attempt < 0 or attempt > 0 and attempt not in starts:
                continue
            if attempt > 0 and not any(candidate.get('codeVersion') == step.get('codeVersion')
                                       and implementation_round(candidate) == attempt for candidate in steps):
                continue
            sources = created.get('inputArtifactIds') or []
            if len(sources) != 1 or source_id is not None and source_id != sources[0]:
                continue
            kind = 'REVALIDATION_STARTED' if attempt else 'VALIDATION_STARTED'
            if any(row.get('eventType') == kind and row.get('attempt') == attempt
                   and row.get('codeVersion') == step.get('codeVersion')
                   and row.get('inputArtifactIds') == sources for row in unique):
                rounds.add(attempt)
        return next(iter(rounds)) if len(rounds) == 1 else None

    def implementation_round(step):
        if step is None or step.get('phase') != 'FIXING':
            return None
        rounds = {row['attempt'] for row in fixes if row.get('eventType') == 'FIX_ATTEMPT_STARTED'
                  and row.get('workflowStepId') == step.get('workflowStepId')
                  and row.get('codeVersion') == step.get('codeVersion')}
        return next(iter(rounds)) if len(rounds) == 1 else None

    captures = observations.get('qaTestInputs')
    test_complete = policy_complete and isinstance(captures, list) and export.get('snapshotTestsComplete') is True
    changes, test_repairs = [], {}

    def test_change(origin, attempt, completed, **identity):
        nonlocal test_complete
        linked = type(attempt) is int and attempt >= 1 and attempt in starts
        changes.append({'source': origin, **identity, 'repairRoundId': f'repair:{attempt}' if linked else None,
                        'status': 'LINKED' if linked else 'UNVERIFIED',
                        'reason': None if linked else 'TEST_CHANGE_REPAIR_START_NOT_LINKED'})
        if not linked:
            # 바이트 변화는 관찰했지만, 승인된 수정 주기의 시작/완료를 추측하지 않는다.
            test_complete = False
            return
        # 같은 수정 주기의 Snapshot과 여러 QA 캡처는 같은 테스트 수정 경로의 증거다.
        # 파일 저장·재실행마다 시도 수나 한도용 repairRoundCount를 늘리지 않는다.
        test_repairs[attempt] = test_repairs.get(attempt, True) and completed

    previous = {}
    for row in captures or []:
        if not all(isinstance(row.get(key), str) and row[key] for key in
                   ('captureId', 'workflowStepId', 'toolName', 'selector', 'testFilesSha256')):
            test_complete = False
            continue
        key = row['toolName'], row['selector']
        changed = key in previous and previous[key] != row['testFilesSha256']
        previous[key] = row['testFilesSha256']
        if changed:
            step = by_step.get(row['workflowStepId'])
            if step is not None and step.get('phase') != 'QA':
                step = None
            attempt = validation_round(step, row.get('sourceArtifactId')) if row.get('sourceArtifactId') else None
            test_change('QA_INPUT', attempt, bool(row.get('executionManifestId')), captureId=row['captureId'])
    prior_tests = None
    for row in export.get('snapshotTests') or []:
        if prior_tests is not None and prior_tests != row['testFilesSha256']:
            matched = [step for step in steps if step.get('phase') == 'FIXING'
                       and step.get('codeVersion') == row['codeVersion']]
            step = matched[0] if len(matched) == 1 else None
            test_change('SNAPSHOT', implementation_round(step), True, codeVersion=row['codeVersion'])
        prior_tests = row['testFilesSha256']
    for attempt, completed in sorted(test_repairs.items()):
        operation = f'test-change:{attempt}'
        for action in ('STARTED', 'COMPLETED') if completed else ('STARTED',):
            add(operation + ':' + action, 'REPAIR', route='TEST_CHANGE', operationId=operation,
                roundId=f'repair:{attempt}', action=action)
    evidence['observedTestChanges'] = changes
    if test_complete:
        evidence['completeRoutes'].append('TEST_CHANGE')
    evidence['limitations'].append('TEST_CHANGE는 Snapshot·QA 입력의 변화를 실제 수정 시작·단계 생성·재검증 사건의 수정 차수에 연결한 경로 수임. '
                                  '같은 수정 주기의 반복 캡처·Snapshot은 합치며, 연결할 수 없는 변화는 미검증으로 남김.')
    if set(evidence['completeRoutes']) == {'IMPLEMENTATION', 'REPLAN', 'TEST_CHANGE'}:
        evidence['completeKinds'].append('REPAIR')
    if set(evidence['completeChannels']) == {'TOOL', 'A2A', 'LLM'}:
        evidence['completeKinds'].append('RETRY')

    # 동일 검증 시도의 QA/Security 보고서 쌍. 판정 모순은 시험 입력까지 같다는 근거가 필요해 자동 확정하지 않는다.
    groups, conflict_complete = {}, complete and bool(steps)
    from orchestrator.domain.validation_artifacts import QAReportArtifact, SecurityReportArtifact
    report_types = {'QA_REPORT': QAReportArtifact, 'SECURITY_REPORT': SecurityReportArtifact}
    for artifact in trace.get('artifacts') or []:
        kind = artifact.get('artifactType')
        if kind not in report_types: continue
        try:
            report = report_types[kind].model_validate(artifact)
            step = by_step[str(report.workflow_step_id)]
            attempt = validation_round(step)
            if str(report.run_id) != run_id or attempt is None: raise ValueError()
            group = groups.setdefault(attempt, {'QA_REPORT': {}, 'SECURITY_REPORT': {}})
            identity = str(report.artifact_id)
            if identity in group[kind] and group[kind][identity] != report:
                raise ValueError()
            group[kind][identity] = report
        except Exception:
            conflict_complete = False
    for attempt, group in groups.items():
        if not group['QA_REPORT'] or not group['SECURITY_REPORT']:
            conflict_complete = False
        for qa, security in product(group['QA_REPORT'].values(), group['SECURITY_REPORT'].values()):
            reasons = []
            if (qa.code_version != security.code_version or qa.execution_manifest != security.execution_manifest):
                reasons.append('VERSION_MISMATCH')
            qa_complete = set(qa.requirement_ids) == {r.requirement_id for r in qa.tests}
            sec_complete = set(security.requirement_ids) == {r.requirement_id for r in security.requirement_results}
            qa_pass = qa_complete and all(r.outcome.value == 'PASS' for r in qa.tests)
            sec_block = (any(r.outcome.value == 'FAIL' for r in security.requirement_results)
                         or any(f.disposition.value == 'CONFIRMED' and f.severity.value in ('HIGH', 'CRITICAL')
                                for f in security.findings))
            if qa_pass and sec_block: reasons.append('QA_PASS_SECURITY_BLOCK')
            if not qa_complete or not sec_complete or any(r.outcome.value == 'UNVERIFIED' for r in [*qa.tests, *security.requirement_results]):
                conflict_complete = False
            if reasons:
                add(f'conflict:{qa.artifact_id}:{security.artifact_id}', 'CONFLICT', qaReportId=str(qa.artifact_id),
                    securityReportId=str(security.artifact_id), reasons=reasons)
    # 보고서 없는 검증 단계가 있으면 누락된 쌍이 있을 수 있다.
    for step in steps:
        role = step.get('phase')
        if role not in ('QA', 'SECURITY'): continue
        kind = 'QA_REPORT' if role == 'QA' else 'SECURITY_REPORT'
        if not any(str(r.workflow_step_id) == step.get('workflowStepId') for group in groups.values() for r in group[kind].values()):
            conflict_complete = False
    if conflict_complete: evidence['completeKinds'].append('CONFLICT')
    return evidence
