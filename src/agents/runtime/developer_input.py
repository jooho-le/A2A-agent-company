"""Lossless measured-check projection for the model, not a replacement journal.

Keep every check observation, including passing siblings and unknowns, once.
Large duplicate report bodies stay in immutable Host storage behind IDs/hashes.
Do not mix instructions from a rejected candidate into the active repair.
"""
from copy import deepcopy
from hashlib import sha256
import json

from orchestrator.domain.repair_progress import compare_repair, edit_effect, selected_checks
from orchestrator.domain.constants import MAX_CODE_FIX_ATTEMPTS, MAX_CONSECUTIVE_SAME_ISSUE_REPEATS
from orchestrator.domain.validation_feedback import server_status_evidence
from agents.runtime.repair_call_history import attach_unchanged_measurements
from agents.runtime.repair_locations import read_locations, diagnostic_sites
from agents.runtime.normal_request_repair import NORMAL_REQUEST, measured_prerequisite, linkable_sites
from agents.runtime.repair_input_failures import INVALID_INPUT, measured_invalid_input, attach_input_source_sites
from agents.runtime.email_normalization import measured_rejections, attach_source_sites as attach_email_sites

POLICY = 'ACTIVE_REPAIR_FOCUS_FIRST_V20'
# V3 (Codex review 2026-11-19 #1·#3, 4e2b1fe8): the Host's own decision leads the input. The first
# failing row the model saw was a REQ-005 generated test that the Ver1 policy had excluded, while the
# only fix target was REQ-006. Every feedback row now carries hostDecision (FIX_TARGET /
# NOT_A_FIX_TARGET / KEEP_PASSING) and detailRecorded (whether sub-observations exist; nothing is
# inferred when they do not), fix targets come first, and `activeRepairFocus` (sorted first among the
# task-input keys) lists the targets with their failing observations and unverified reasons.
# V2 (Codex review 2026-11-18 #4): failing, unverified and not-run checks keep every observation;
# a check that PASSED with every observation PASS is listed by identifiers only (to keep passing).
# No lookup Tool is added (each extra Tool has been a new failure point); the original A2A request
# and reports stay unchanged in the Host journal.


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _compact_observations(detail, row):
    # IDs alone are not proof of duplication. Preserve anything that differs or
    # lacks a feedback counterpart, including unfamiliar diagnostic fields.
    fields = ('observations', 'observationStatus', 'reason')
    if not isinstance(detail, dict) or not detail.get('observations'):
        return detail
    if not all(key in row and detail.get(key) == row[key] for key in fields):
        return detail
    return {**{k:v for k,v in detail.items() if k not in fields},
            'checksLocation': 'fixRequest.validationFeedback'}


def _compact_details(encoded, rows, test_id=None):
    try:
        detail = json.loads(encoded)
    except (TypeError, ValueError):
        return encoded
    if not isinstance(detail, dict):
        return encoded
    if test_id is not None:
        matches = [r for r in rows if r.get('testId') == test_id]
        compact = _compact_observations(detail, matches[0]) if len(matches) == 1 else detail
    elif isinstance(detail.get('tests'), list):
        compact = {**detail, 'tests': []}
        for test in detail['tests']:
            if not isinstance(test, dict):
                return encoded
            matches = [r for r in rows if r.get('testId') == test.get('testId')]
            compact['tests'].append(_compact_observations(test, matches[0]) if len(matches) == 1 else test)
    else:
        return encoded
    return encoded if compact == detail else json.dumps(compact, ensure_ascii=False, separators=(',', ':'))


def _fully_passing(row):
    observations = row.get('observations')
    return (row.get('outcome') == 'PASS' and row.get('observationStatus') == 'COMPLETE'
            and isinstance(observations, list) and bool(observations)
            and all(isinstance(item, dict) and item.get('status') == 'PASS' for item in observations))


def _passing_summary(row):
    summary = {key: row[key] for key in ('role', 'reportArtifactId', 'requirementId', 'testId', 'outcome',
                                          'observationStatus') if key in row}  # hostDecision set below
    return {**summary, 'observationCount': len(row['observations']),
            'observationsSha256': _digest(row['observations']), 'keepPassing': True,
            'hostDecision': 'KEEP_PASSING', 'detailRecorded': True}


def _requirement_keys(value):
    contract = (value.get('runConfiguration') or {}).get('scenarioContract') or value.get('scenario') or {}
    return {str(item.get('requirementId')): item.get('key') for item in contract.get('requirements', [])
            if isinstance(item, dict) and item.get('requirementId') and item.get('key')}


def focus_observations(observations):
    """Carry a failing case's measured preconditions along with its unresolved observations."""
    cases = {item.get('caseId') for item in observations if item.get('status') != 'PASS' and item.get('caseId')}
    return [item for item in observations if item.get('status') != 'PASS' or
            (item.get('kind') == 'precondition' and item.get('caseId') in cases)]


def _case_feedback(observations):
    """Small case-by-case reading of the raw observations retained in failingObservations."""
    sections = {'PASS': 'confirmedConditions', 'FAIL': 'failed', 'UNVERIFIED': 'unverified', 'NOT_RUN': 'notRun'}
    cases = {}
    for item in observations:
        status = item.get('status')
        if (status not in sections or not item.get('caseId') or not item.get('itemId')
                or 'actual' not in item or 'reason' not in item):
            continue  # Retain incomplete legacy observations only in their original shape.
        case = cases.setdefault(item['caseId'], {})
        case.update(server_status_evidence(item))
        if item.get('kind') == 'precondition' and item['itemId'] == 'response_status':
            case['handlerEntry'] = item.get('handlerEntry', 'UNKNOWN')
        if item.get('diagnostic'):
            case.setdefault('diagnostics', {})[item['itemId']] = item['diagnostic']
        case.setdefault(sections[status], {})[item['itemId']] = (
            item['actual'] if status in ('PASS', 'FAIL') else item.get('reason'))
    return cases


def _observed_paths(cases):
    """Group actual signals. Handler entry localizes a request, never a culprit function."""
    paths = {}
    for case_id, case in cases.items():
        failed = case.get('failed', {})
        unverified = case.get('unverified', {})
        if (case.get('handlerEntry') == 'NOT_ENTERED'
                and any(failed.get(key) is True for key in ('password_exposed', 'hash_exposed'))):
            paths.setdefault('RESPONSE_BEFORE_SIGNUP_HANDLER', []).append(case_id)
        if (case.get('handlerEntry') == 'ENTERED' and case.get('confirmedConditions', {}).get('injected') is True
                and unverified.get('response_status') == 'UNHANDLED_APP_EXCEPTION'):
            paths.setdefault('UNHANDLED_DATABASE_ERROR_AFTER_ENTRY', []).append(case_id)
        if case_id == 'application_logs' and any(failed.get(key) is True for key in (
                'password_exposed', 'hash_exposed', 'password_in_crash', 'hash_in_crash')):
            paths.setdefault('EMITTED_APPLICATION_LOG_OR_TRACEBACK', []).append(case_id)
    return paths


def _focus_check(row, effects):
    observations = focus_observations(row.get('observations') or [])
    check = {key: row.get(key) for key in (
        'testId', 'reportArtifactId', 'role', 'requirementId', 'outcome', 'detailRecorded', 'reason')}
    if row.get('protectedReceiptId'):
        check.update({'protectedReceiptId': row['protectedReceiptId'], 'evidenceOrigin': row['evidenceOrigin']})
    brief = {'testId': row['testId'], 'role': row.get('role'), 'requirementId': row.get('requirementId'),
             'caseFeedback': _case_feedback(observations)}
    if row.get('role') == 'SECURITY' and row.get('observationStatus') == 'COMPLETE':
        paths = _observed_paths(brief['caseFeedback'])
        if paths:
            brief['observedFailurePaths'] = paths
    if row.get('signupChecks'):
        brief['signupChecks'] = row['signupChecks']
    if row.get('outputChecks'):
        brief['outputChecks'] = row['outputChecks']
        # Generated tests supply channel measurements without protected case IDs.
        channels = {'REJECTION_RESPONSE': 'RESPONSE_BEFORE_SIGNUP_HANDLER',
                    'SUCCESS_RESPONSE': 'SIGNUP_SUCCESS_RESPONSE', 'STORAGE_ERROR_RESPONSE': 'DATABASE_ERROR_RESPONSE',
                    'REQUEST_LOGS': 'EMITTED_APPLICATION_LOG_OR_TRACEBACK',
                    'STORAGE_ERROR_REQUEST_LOGS': 'EMITTED_APPLICATION_LOG_OR_TRACEBACK'}
        diagnostics = row.get('outputDiagnostics', {})
        if diagnostics:
            brief['outputDiagnostics'] = diagnostics
        if any(d.get('escapedToClient') for d in diagnostics.get('STORAGE_ERROR_RESPONSE', [])):
            brief.setdefault('observedFailurePaths', {}).setdefault('UNHANDLED_DATABASE_ERROR_AFTER_ENTRY', []).append('STORAGE_ERROR_RESPONSE')
        if row.get('role') in ('QA', 'SECURITY'):
            for channel, path in channels.items():
                if row['outputChecks'].get(channel) == 'EXPOSED':
                    brief.setdefault('observedFailurePaths', {}).setdefault(path, []).append(channel)
    if row.get('reason'):
        brief['reason'] = row['reason']
    effect = effects.get((row.get('role'), row.get('requirementId'), row['testId'], row['reportArtifactId']))
    if effect is not None:
        brief['previousEditEffect'] = edit_effect(effect)
    return {**check, 'failingObservations': observations}, brief


def _build_failure(issue, artifacts):
    if issue.get('category') != 'BUILD_CODE_FAILURE':
        return None
    report_id, source_id = issue.get('reportArtifactId'), issue.get('sourceArtifactId')
    matches = [a['record'] for a in artifacts if a.get('artifactType') == 'BUILD_REPORT'
               and a.get('artifactId') == report_id and isinstance(a.get('record'), dict)]
    if len(matches) != 1 or not report_id or not source_id:
        return None
    build, = matches
    if (build.get('artifactId') != report_id or build.get('sourceArtifactId') != source_id or type(build.get('codeVersion')) is not int
            or build.get('codeVersion') != issue.get('codeVersion')
            or build.get('failureKind') != 'PRODUCT' or build.get('executionOutcome') != 'FAIL'):
        return None
    fields = ('ruleId', 'normalizedLocation', 'missingName', 'sourceExpression')
    return {'codeVersion': build['codeVersion'], 'sourceArtifactId': source_id, 'reportArtifactId': report_id,
            'buildDiagnostics': [{k: d[k] for k in fields if k in d} for d in build.get('diagnostics', [])]}


def _repair_focus(selected, feedback, keys, previous, issue_records, attempt, artifacts):
    effects = {(c['role'], c['requirementId'], c['testId'], c['afterReportArtifactId']): c
               for c in (previous or {}).get('checks', [])}
    records = {(str(i.issue_id), str(i.source_artifact_id), str(i.report_artifact_id) if i.report_artifact_id else None,
                i.code_version): i
               for i in issue_records}
    targets = []
    brief_targets = []
    for issue, rows in selected:
        ids = issue.get('requirementIds') or []
        checks = [_focus_check(row, effects) for row in rows]
        target = {'issueId': issue.get('issueId'), 'category': issue.get('category'),
                  'requirementIds': ids, 'requirementKeys': [keys.get(i) for i in ids],
                  'failingChecks': [detail for detail, _ in checks]}
        brief_target = {'issueId': issue.get('issueId'), 'category': issue.get('category'),
                        'checks': [summary for _, summary in checks]}
        build = _build_failure(issue, artifacts)
        if build is not None:
            brief_target['buildDiagnostics'] = build['buildDiagnostics']
        record = records.get((issue.get('issueId'), issue.get('sourceArtifactId'),
                              issue.get('reportArtifactId'), issue.get('codeVersion')))
        if record is not None:
            brief_target['hostIssueProgress'] = {'codeVersion': record.code_version, 'measuredProgress': record.measured_progress,
                'consecutiveRepeatCount': record.consecutive_repeat_count, 'stopRepeatCount': record.repeats_for_stop}
        targets.append(target)
        brief_targets.append(brief_target)
    not_selected = [{key: row.get(key) for key in ('testId', 'reportArtifactId', 'role', 'requirementId', 'outcome', 'detailRecorded',
                                                    'reason')}
                    for row in feedback if row.get('hostDecision') == 'NOT_A_FIX_TARGET']
    # The prompt serializer sorts keys. Keep the short brief ahead of excluded checks and raw observations.
    brief = {'targets': brief_targets}
    focus = {'brief': brief, 'instruction': ('Repair exactly the Host-selected targets below, on the code path that produced their '
                            'failing observations. notSelectedFailures are failures the Host did not select for this '
                            'repair (e.g. a generated test overridden by passing protected tests); do not change code '
                            'for them. Keep every KEEP_PASSING check passing. detailRecorded=false means the test '
                            'reported no sub-observations; do not guess its actual values or cause. '
                            'Read caseFeedback with previousEditEffect. Keep newly passing behavior and address '
                            'remaining failures. NO_PROGRESS means the listed edit left these observations unchanged. '
                            'Compare observedFailurePaths with previousEdit and its effect. Before editing, read the code that handles each measured path. When there was no progress, explain which path the last edit could reach and what different boundary you will inspect. A changed function name is not proof of execution. An unverified or unexecuted item has no measured value.'),
            'targets': targets, 'notSelectedFailures': not_selected,
            'keepPassingCount': sum(1 for row in feedback if row.get('hostDecision') == 'KEEP_PASSING')}
    if previous is not None:
        brief['previousEdit'] = {key: previous[key] for key in (
            'beforeVersion', 'afterVersion', 'changedFiles', 'changedFunctions', 'changedHandlerDecorators') if key in previous}
        if previous.get('measuredCallEdits'):
            brief['previousEdit']['callEvidenceLocation'] = 'previousRepairResult.measuredCallEdits'
            brief['previousEdit']['callEvidenceInstruction'] = (
                'Compare the changed argument shapes with the unchanged checks. The exception site was observed '
                'on the previous source. The candidate site is a static match. Inspect any pinned library '
                'parameter contract; only a new successful request establishes recovery.')
    if type(attempt) is int and 1 <= attempt <= MAX_CODE_FIX_ATTEMPTS:
        brief['repairRound'] = {'currentAttempt': attempt, 'maxCodeFixAttempts': MAX_CODE_FIX_ATTEMPTS,
                                'maxAdditionalAttemptsAfterThis': MAX_CODE_FIX_ATTEMPTS - attempt,
                                'sameIssueRepeatLimit': MAX_CONSECUTIVE_SAME_ISSUE_REPEATS}
    return focus


def _shared_receipt(receipt, path, seen):
    """Keep the first complete Host receipt in place and link exact repeats to it."""
    if not isinstance(receipt, dict) or not receipt.get('evidenceRef') or not receipt.get('executionId'):
        return receipt
    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if encoded in seen:
        return {'sameReceiptAs': seen[encoded]}
    seen[encoded] = path
    return receipt


def compact_fix_input(task_input, *, repair_history=None, issue_records=(), source_files=None):
    if 'fixRequest' not in task_input:
        return task_input
    value = deepcopy(task_input)
    fix = value['fixRequest']
    feedback = fix.get('validationFeedback', [])
    selected = [(issue, selected_checks(issue, feedback))
                for issue in fix.get('issues', []) if isinstance(issue, dict)]
    selected_rows = {id(row) for _, rows in selected for row in rows}
    for row in feedback:
        if isinstance(row, dict):
            if row.get('outcome') == 'PASS':
                row['hostDecision'] = 'KEEP_PASSING'
            else:
                row['hostDecision'] = 'FIX_TARGET' if id(row) in selected_rows else 'NOT_A_FIX_TARGET'
            row['detailRecorded'] = bool(row.get('observations'))
    blocker, normal_frames = measured_prerequisite(feedback)
    primary_checks = {(fact['reportArtifactId'], fact['testId']) for fact in (blocker or {}).get('failedCreationCases', ())}
    def primary(row):
        return (row.get('reportArtifactId'), row.get('testId')) in primary_checks

    if primary_checks:
        selected = [(issue, sorted(rows, key=primary, reverse=True)) for issue, rows in selected]
        selected.sort(key=lambda pair: any(primary(row) for row in pair[1]), reverse=True)
    # Stable ordering: Host fix targets, then failures the Host did not select, then passing checks.
    order = {'FIX_TARGET': 0, 'NOT_A_FIX_TARGET': 1, 'KEEP_PASSING': 2}
    feedback.sort(key=lambda row: (order.get(row.get('hostDecision'), 3), not primary(row)))
    seen_receipts = {}
    for artifact_index, artifact in enumerate(fix.get('inputArtifacts', [])):
        record = artifact.get('record')
        if artifact.get('artifactType') not in ('QA_REPORT', 'SECURITY_REPORT') or not isinstance(record, dict):
            continue
        original_digest = _digest(record)
        field = 'tests' if 'tests' in record else 'requirementResults'
        for row_index, result in enumerate(record.get(field, [])):
            rows = [r for r in feedback if r.get('reportArtifactId') == artifact['artifactId']
                    and r.get('requirementId') == result.get('requirementId')]
            if 'details' in result:
                details = _compact_details(result['details'], rows, result.get('testId'))
                if details != result['details']:
                    result['details'] = details
                    artifact['checksLocation'] = 'fixRequest.validationFeedback'
            path = f'#/fixRequest/inputArtifacts/{artifact_index}/record/{field}/{row_index}'
            evidence = result.get('executionEvidence')
            if isinstance(evidence, list):
                result['executionEvidence'] = [
                    _shared_receipt(item, f'{path}/executionEvidence/{index}', seen_receipts)
                    for index, item in enumerate(evidence)]
            if 'toolEvidence' in result:
                result['toolEvidence'] = _shared_receipt(result['toolEvidence'], f'{path}/toolEvidence', seen_receipts)
        if _digest(record) != original_digest:
            artifact['recordSha256'] = original_digest
    for issue in fix.get('issues', []):
        if issue.get('category') not in ('QA_ASSERTION_FAILURE', 'SECURITY_REQUIREMENT_FAILURE'):
            continue
        matching = [r for r in feedback if r.get('reportArtifactId') == issue.get('reportArtifactId')
                    and r.get('protectedReceiptId') == issue.get('protectedReceiptId')
                    and r.get('requirementId') in issue.get('requirementIds', [])]
        if not matching:
            continue
        try:
            detail = json.loads(issue.get('description', ''))
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict) or detail.get('code') not in (
                'MEASURED_UNIT_TEST', 'MEASURED_SECURITY_TESTS', 'SECURITY_TESTS_UNVERIFIED', 'SECURITY_TESTS_NOT_RUN'):
            continue  # Preserve free-form or unfamiliar diagnostic information.
        compact = _compact_details(issue['description'], matching,
                                   issue.get('referenceId') if detail['code'] == 'MEASURED_UNIT_TEST' else None)
        if compact != issue['description']:
            issue['descriptionSha256'] = _digest(issue['description'])
            issue['description'] = compact
            issue['checksLocation'] = 'fixRequest.validationFeedback'
    baseline = value.get('repairBaseline')
    rejected_build = None
    if baseline is not None:
        historical = baseline.pop('rejectedCandidateFixRequest', None)
        if historical is not None:
            failures = [issue for issue in historical.get('issues', []) if issue.get('category') == 'BUILD_CODE_FAILURE'
                        and issue.get('sourceArtifactId') == baseline['rejectedSourceArtifactId']]
            if len(failures) == 1:
                rejected_build = _build_failure(failures[0], historical.get('inputArtifacts', []))
            baseline['rejectedCandidateHistory'] = {
                'sourceArtifactId': baseline['rejectedSourceArtifactId'],
                'requestSha256': _digest(historical),
                'issueIds': [i['issueId'] for i in historical.get('issues', []) if 'issueId' in i],
                'instruction': 'Audit history only; not the current repair task. Original request remains in the Host journal.'}
        baseline.pop('validationFeedback', None)
        baseline['checksLocation'] = 'fixRequest.validationFeedback'
        baseline['instruction'] = ('Working files are restored to this measured Snapshot. Repair only the active '
            'fixRequest issues. The rejected candidate is audit history, not an edit target. Build PASS does not '
            'resolve QA or Security failures. Preserve passing checks and revalidate unresolved checks after changes.')
    config = value.get('runConfiguration')
    if isinstance(config, dict) and 'scenarioContract' in config and 'frozenScenarioContractJson' in config:
        frozen = config['frozenScenarioContractJson']
        try: identical = json.loads(frozen) == config['scenarioContract']
        except (TypeError, ValueError): identical = False
        if identical:
            config['frozenScenarioContractSha256'] = sha256(frozen.encode()).hexdigest()
            del config['frozenScenarioContractJson']
    # Last, after the report/issue de-duplication above compared full observations.
    if repair_history is not None:
        value['previousRepairResult'] = compare_repair(repair_history, feedback)
        attach_unchanged_measurements(value['previousRepairResult'], repair_history, feedback)
    summarized = 0
    for index, row in enumerate(feedback):
        if isinstance(row, dict) and _fully_passing(row):
            feedback[index] = _passing_summary(row)
            summarized += 1
    value['activeRepairFocus'] = _repair_focus(selected, feedback, _requirement_keys(value),
                                               value.get('previousRepairResult'), issue_records, fix.get('attempt'),
                                               fix.get('inputArtifacts', []))
    brief = value['activeRepairFocus']['brief']
    unit_failures = []
    for row in feedback:
        diagnostic = row.get('failureDiagnostic')
        if row.get('hostDecision') != 'FIX_TARGET' or not diagnostic:
            continue
        from orchestrator.domain.unit_failure_diagnostics import validate_failure_diagnostic
        unit_failures.append({'testId': row.get('testId'),
                              'failureDiagnostic': validate_failure_diagnostic(diagnostic)})
        if len(unit_failures) == 16:
            break
    if unit_failures:
        brief['unitFailureDiagnostics'] = unit_failures
        brief['unitFailureInstruction'] = (
            'These are value-free observed symptoms on active fix targets, not confirmed root causes. '
            'For an actualStatus/expectedStatus mismatch, preserve serviceDesign and repair the implementation. '
            'If invalid input received 2xx where 4xx was expected, repair that validation path before hashing '
            'or writing data. A required Pydantic str field rejects missing input but still accepts an empty '
            'string unless the model or handler validates it. Preserve the failed negative case and its '
            'agreed status; do not turn its input into a valid request or change the test to expect success. '
            'FastAPI can return 422 before entering a request-model endpoint; when 400 is agreed, inspect '
            'the application RequestValidationError handler rather than adding a missing-field check inside '
            'that endpoint. For DATABASE_TABLE_MISSING, check application startup and test lifespan first; '
            'do not create tables in tests or add unrelated initialization just to hide a fixture defect. '
            'Read the actual Source and explain which failure path your edit reaches. Preserve passing checks.')
    runtime = config.get('configuration') if isinstance(config, dict) else None
    runtime = runtime if isinstance(runtime, dict) else {}
    invalid_input = measured_invalid_input(fix, runtime)
    email_contract = (config.get('scenarioContract') if isinstance(config, dict) else None) or value.get('scenario') or {}
    email_rejections = measured_rejections(fix, runtime, email_contract)
    paths = {path for target in brief['targets'] for check in target['checks']
             for path in check.get('observedFailurePaths', {})}
    if invalid_input:
        paths.add(INVALID_INPUT)
    files = source_files() if source_files is not None and (paths or normal_frames or email_rejections) else None
    if invalid_input:
        attach_input_source_sites(invalid_input, files, value['activeRepairFocus'])
        brief['invalidInputServerError'] = invalid_input
    if email_rejections:
        brief['emailNormalizationRepair'] = email_rejections
        attach_email_sites(email_rejections, files or {}, email_contract)
    if blocker:
        brief['blockingPrerequisite'] = blocker
        blocker['sourceSites'] = (linkable_sites(normal_frames, files, environment=runtime.get('environment'))
                                  if files is not None else [])
        if blocker['sourceSites']:
            paths.add(NORMAL_REQUEST)
    if paths:
        brief['readyReportPaths'] = sorted(paths)
        stalled = {path for target in brief['targets'] for check in target['checks']
                   if check.get('previousEditEffect', {}).get('status') == 'NO_PROGRESS'
                   for path in check.get('observedFailurePaths', {})}
        if NORMAL_REQUEST in paths:
            brief['priorityRepairPaths'] = [NORMAL_REQUEST]
            brief['priorityInstruction'] = blocker['instruction']
        elif stalled and not blocker:
            brief['priorityRepairPaths'] = sorted(stalled - {'EMITTED_APPLICATION_LOG_OR_TRACEBACK'} or stalled)
            brief['priorityInstruction'] = ('Start with a measured priority path whose previous edit left the outcome unchanged. '
                'Inspect where the request is rejected or the exception escapes. A response-start event does not mean '
                'the client received the response. Recheck logs independently after changing the producer.')
        brief['readyReportInstruction'] = (
            'For each listed path, cite a changed source/file.py::function or source/file.py::@module in sourceRef '
            'and explain how the edit reaches this particular response, database failure or emitted log. '
            'A general error handler is not evidence for every path. A shared helper can be cited on multiple paths '
            'if connected to each. If the path was untouched or its connection cannot be established, set both '
            'edit and sourceRef to null and describe what remains open in recheck. State the behavior still '
            'to recheck for every path. These are Developer claims pending validation.')
    if paths and files is not None:
        locations = read_locations(paths, files)
        if invalid_input and invalid_input['sourceSites']:
            related_sites = [{'toolPath': site['toolPath'], 'line': site['line'], 'operation': 'RELATED_EXCEPTION_FRAME'}
                             for site in invalid_input['sourceSites']]
            locations[INVALID_INPUT] = related_sites + locations.get(INVALID_INPUT, [])
        frames = []
        for target in brief['targets']:
            for check in target['checks']:
                for case in check['caseFeedback'].values():
                    frames.extend(d['appFrame'] for d in case.get('diagnostics', {}).values() if 'appFrame' in d)
                for observations in check.get('outputDiagnostics', {}).values():
                    frames.extend(d['appFrame'] for d in observations if 'appFrame' in d)
        frame_sites = diagnostic_sites(frames, files)
        if frame_sites:
            brief['observedAppFrames'] = frame_sites
        coverage = {}
        for sites in locations.values():
            for tool_path in {site['toolPath'] for site in sites}:
                coverage[tool_path] = coverage.get(tool_path, 0) + 1
        measured_paths = {site['toolPath'] for site in frame_sites}
        blocker_paths = {site['toolPath'] for site in (blocker or {}).get('sourceSites', ())}
        first_paths = sorted(blocker_paths | measured_paths | coverage.keys(), key=lambda path: (
            path not in blocker_paths, path not in measured_paths, -coverage.get(path, 0), path))
        brief['sourceInspection'] = {
            'byObservedPath': locations,
            # Suggest a small first batch; byObservedPath keeps every other candidate accessible.
            'firstReadBatch': first_paths[:2],
            'instruction': 'Read the distinct firstReadBatch files together with read_project_file calls in one response. '
                'One file read covers every listed site in that file. Use its returned sha256 for an edit and the '
                'new edit receipt sha256 for consecutive edits. An unchangedSinceReadCallId points to the earlier '
                'full contents in this conversation. Read additional files only for a dependency of the repair. '
                'These are candidate read locations. RELATED_EXCEPTION_FRAME was observed only for the separately '
                'named exception case. ROUTE_BOUNDARY_ONLY marks the handler the request did not enter. '
                'registeredException names the written registration; it does not prove dispatch. Inspect registrations, '
                'callers and local callees, including framework defaults when a registration is absent. Trace the '
                'observed response or log back to its producer and edit that path. A listed call is not evidence it '
                'ran. An empty list means no recognized syntax. The Host measures the effect on the new Snapshot.'}
    if email_rejections and email_rejections.get('sourceSites'):
        inspection = brief.setdefault('sourceInspection', {'byObservedPath': {}, 'firstReadBatch': [],
            'instruction': email_rejections['instruction']})
        email_paths = list(dict.fromkeys(site['toolPath'] for site in email_rejections['sourceSites']))
        inspection['firstReadBatch'] = list(dict.fromkeys(inspection['firstReadBatch'] + email_paths))[:2]
        inspection['emailNormalizationSites'] = email_rejections['sourceSites']
    if rejected_build is not None:
        value['activeRepairFocus']['brief']['lastRejectedBuild'] = {
            **rejected_build, 'context': 'Last failed candidate. Its line numbers refer to that candidate. '
                                         'Working files were restored to the last passing build.'}
    value['developerInputProjection'] = {'policy': POLICY, 'originalInputSha256': _digest(task_input),
        'checkCount': len(feedback), 'failingCheckObservationsPreserved': True,
        'passingChecksSummarized': summarized,
        'instruction': ('Start from activeRepairFocus: the Host-selected repair targets. fixRequest is the active '
                        'task. previousRepairResult, when present, records the measured effect of the preceding edit. '
                        'validationFeedback lists every measured check with hostDecision: FIX_TARGET rows with '
                        'all observations first, then NOT_A_FIX_TARGET failures (do not repair them), then '
                        'KEEP_PASSING checks (fully passing ones by identifiers only). A sameReceiptAs pointer refers '
                        'to the identical full tool receipt in this input. '
                        'For a runtime failure, change the implementation on the failing path before READY. '
                        'Adding ordinary Python comments about the desired behavior does not repair it.')}
    return value
