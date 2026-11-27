"""Host rules for binding validation issues and comparing measured repairs."""
from collections import Counter
import json

from orchestrator.domain.validation_feedback import observation_json, server_status_evidence, validated_output_checks, validation_feedback


def _issue_test_ids(issue):
    category = issue.get('category')
    if category == 'QA_ASSERTION_FAILURE':
        role = 'QA'
        tests = {issue.get('referenceId')}
    else:
        role = 'SECURITY'
        try:
            detail = json.loads(issue.get('description') or '{}')
        except (TypeError, ValueError):
            detail = {}
        if (isinstance(detail, dict) and detail.get('code') == 'MEASURED_SECURITY_TESTS'
                and isinstance(detail.get('tests'), list)):
            tests = {item.get('testId') for item in detail['tests']
                     if isinstance(item, dict) and item.get('outcome') == 'FAIL'}
        else:
            # Older scanner-only results have one requirement-level row and no unit tests.
            tests = {'security-requirement:' + str(issue.get('referenceId'))}
    return role, tests


def selected_checks(issue, feedback):
    """Bind only checks named by this Host issue on this report and requirement."""
    category = issue.get('category')
    report = issue.get('reportArtifactId')
    protected = issue.get('protectedReceiptId') if issue.get('evidenceOrigin') == 'HOST_PROTECTED_RECEIPT' else None
    if (not report and not protected) or category not in ('QA_ASSERTION_FAILURE', 'SECURITY_REQUIREMENT_FAILURE'):
        return []
    role, tests = _issue_test_ids(issue)
    if issue.get('reporter', role) != role:
        return []
    candidates = [row for row in feedback if isinstance(row, dict)
            and row.get('role', role) == role and row.get('reportArtifactId') == report
            and row.get('protectedReceiptId') == protected
            and row.get('requirementId') in issue.get('requirementIds', [])
            and row.get('testId') in tests and row.get('testId')
            and (not issue.get('sourceArtifactId') or row.get('sourceArtifactId') == issue['sourceArtifactId'])
            and (not issue.get('codeVersion') or row.get('codeVersion') == issue['codeVersion'])]
    identities = Counter((row['requirementId'], row['testId']) for row in candidates)
    return [row for row in candidates if row.get('outcome') == 'FAIL'
            and identities[row['requirementId'], row['testId']] == 1]


def _observations(row):
    items = row.get('observations') or []
    if row.get('observationStatus') != 'COMPLETE' or not items:
        return None
    try:
        observation_json([{k: v for k, v in item.items() if k != 'label'} for item in items])
    except (TypeError, ValueError):
        return None
    return {(item['caseId'], item['itemId']): item for item in items}


def _generated_observations(row):
    if (row.get('observations') or row.get('reason') == 'DETAIL_RECORD_INVALID'
            or not (row.get('testVersion') or '').startswith('inputs:')):
        return None
    try:
        marks = validated_output_checks(row.get('outputChecks'))
    except ValueError:
        return None
    if (all(value in ('INCOMPLETE', 'NO_SAMPLE') for value in marks.values())
            or row.get('outcome') == 'FAIL' and 'EXPOSED' not in marks.values()):
        return None
    grades = {'CHECKED': ('PASS', False), 'EXPOSED': ('FAIL', True), 'INCOMPLETE': ('UNVERIFIED', None),
              'NO_SAMPLE': ('UNVERIFIED', None)}
    return {('generated_output', channel): {
        'kind': 'assertion', 'status': grades[mark][0], 'actual': grades[mark][1], 'reason': mark
    } for channel, mark in marks.items()}


def _transition(old, new):
    if old['status'] == new['status']:
        # JSON distinguishes false from 0. A changed observation alone proves no improvement.
        if (old['reason'] == new['reason'] and json.dumps(old['actual']) == json.dumps(new['actual'])
                and server_status_evidence(old) == server_status_evidence(new)):
            return 'UNCHANGED'
        return 'OBSERVATION_CHANGED'
    if new['status'] == 'PASS':
        return 'NOW_PASSING'
    if old['status'] == 'PASS':
        return 'NEW_FAILURE' if new['status'] == 'FAIL' else 'VERIFICATION_LOST'
    if new['status'] == 'FAIL':
        return 'NOW_OBSERVED_FAIL'
    if old['status'] == 'FAIL':
        return 'VERIFICATION_LOST'
    return 'UNVERIFIED_CHANGED'


def _comparison(old, new):
    left, right = _observations(old), _observations(new)
    if left is None and right is None:
        left, right = _generated_observations(old), _generated_observations(new)
    if not left or not right or left.keys() != right.keys():
        return {'status': 'NOT_COMPARABLE', 'reason': 'OBSERVATION_SET_CHANGED_OR_INCOMPLETE'}
    counts = Counter()
    items = []
    for key, current in right.items():
        prior = left[key]
        if prior['kind'] != current['kind']:
            return {'status': 'NOT_COMPARABLE', 'reason': 'OBSERVATION_KIND_CHANGED'}
        transition = _transition(prior, current)
        counts[transition] += 1
        if transition == 'UNCHANGED' and current['status'] != 'PASS':
            items.append({'caseId': key[0], 'itemId': key[1], 'transition': transition, 'status': current['status']})
        elif transition != 'UNCHANGED':
            items.append({'caseId': key[0], 'itemId': key[1], 'transition': transition,
                          'before': {**{k: prior[k] for k in ('status', 'actual', 'reason')}, **server_status_evidence(prior)},
                          'after': {**{k: current[k] for k in ('status', 'actual', 'reason')}, **server_status_evidence(current)}})
    unresolved = any(row['status'] != 'PASS' for row in right.values())
    if set(counts) == {'UNCHANGED'} and old['outcome'] == new['outcome']:
        status = 'NO_PROGRESS' if unresolved or new['outcome'] != 'PASS' else 'UNCHANGED_PASS'
    elif counts['NOW_PASSING'] and not unresolved and new['outcome'] == 'PASS':
        status = 'RESOLVED'
    elif counts['NOW_PASSING'] and set(counts) <= {'NOW_PASSING', 'UNCHANGED'}:
        status = 'PARTIAL_IMPROVEMENT'
    elif counts['NEW_FAILURE'] and not counts['NOW_PASSING']:
        status = 'REGRESSION'
    else:
        status = 'CHANGED'
    return {'status': status, 'counts': dict(counts), 'items': items}


def _index(rows):
    result = {}
    for row in rows:
        key = (row['role'], row['requirementId'], row['testId'])
        result[key] = None if key in result else row
    return result


def compare_check(old, new, history, legacy_version=None):
    if not old or not new:
        return {'status': 'NOT_COMPARABLE', 'reason': 'CHECK_MISSING_OR_AMBIGUOUS'}
    if (old.get('runId') != new.get('runId')
            or old['codeVersion'] != history['beforeVersion'] or new['codeVersion'] != history['afterVersion']
            or any(row.get('executionManifest', {}).get('snapshotSha256', expected) != expected
                   for row, expected in ((old, history['beforeSnapshot']), (new, history['afterSnapshot'])))):
        return {'status': 'NOT_COMPARABLE', 'reason': 'SOURCE_MISMATCH'}
    version = old.get('testVersion', legacy_version)
    if not version or version != new.get('testVersion', legacy_version):
        return {'status': 'NOT_COMPARABLE', 'reason': 'TEST_VERSION_UNKNOWN_OR_CHANGED'}
    return _comparison(old, new)


def edit_effect(comparison):
    effect = {key: comparison[key] for key in ('status', 'counts', 'reason') if key in comparison}
    now_passing = [{key: item[key] for key in ('caseId', 'itemId')}
                   for item in comparison.get('items', []) if item['transition'] == 'NOW_PASSING']
    if now_passing:
        effect['nowPassing'] = now_passing
    return effect


def compare_repair(history, current):
    before = _index(history['previousRows'])
    after = _index(current)
    checks = []
    legacy = history.get('legacyTestVersions', {})
    for key in sorted(after):
        old, new = before.get(key), after.get(key)
        if new and new.get('hostDecision') == 'NOT_A_FIX_TARGET':
            continue
        if new and new['outcome'] == 'PASS' and (not old or old['outcome'] == 'PASS'):
            continue
        entry = dict(zip(('role', 'requirementId', 'testId'), key))
        change = compare_check(old, new, history, legacy.get(key))
        checks.append({**entry, 'beforeReportArtifactId': old['reportArtifactId'] if old else None,
                       'afterReportArtifactId': new['reportArtifactId'] if new else None, **change})
    return {**{k: history[k] for k in ('beforeVersion', 'afterVersion', 'beforeSnapshot', 'afterSnapshot',
                                       'changedFiles', 'changedFunctions', 'changedHandlerDecorators') if k in history}, 'checks': checks}


def final_rechecks(review, source, artifacts, chosen, configuration, *, issues=(), protected_rows=None, generated_lookup=None):
    """Attach measured outcomes to the exact checks named at READY, including the last fix."""
    if review is None or not review.rechecks:
        return []
    by_id = {a.artifact_id: a for a in artifacts}
    parent = by_id.get(source.previous_artifact_id)
    by_issue = {issue.issue_id: issue for issue in issues}
    protected_rows = protected_rows or {}
    versions = None
    reports = {}

    def rows_for(report, snapshot):
        if (report is None or snapshot is None or report.run_id != source.run_id
                or snapshot.run_id != source.run_id or report.execution_manifest != snapshot.execution_manifest()):
            return {}
        if report.artifact_id not in reports:
            try:
                reports[report.artifact_id] = _index(validation_feedback([snapshot, report]))
            except ValueError:
                # Optional feedback from legacy reports cannot establish a measured change.
                reports[report.artifact_id] = {}
        return reports[report.artifact_id]

    results = []
    for recheck in review.rechecks:
        key = recheck.role, str(recheck.requirement_id), recheck.test_id
        role_report = recheck.role + '_REPORT'
        prior = by_id.get(recheck.prior_report_id)
        current = chosen.get(role_report)
        new = rows_for(current, source).get(key)
        protected_id = getattr(recheck, 'prior_protected_receipt_id', None)
        old = None
        if protected_id is not None:
            issue = by_issue.get(recheck.issue_id)
            if (issue and parent and issue.run_id == source.run_id and issue.source_artifact_id == parent.artifact_id
                    and issue.code_version == parent.code_version and issue.protected_receipt is not None
                    and issue.protected_receipt.receipt_id == protected_id
                    and (issue.reporter.value, str(issue.requirement_ids[0]), issue.protected_receipt.test_id) == key):
                from orchestrator.domain.protected_feedback import protected_feedback_row
                old = protected_feedback_row(issue, parent)
            new = protected_rows.get(recheck.issue_id) if old else None
        elif prior and prior.artifact_type == role_report:
            old = rows_for(prior, parent).get(key)
            issue = by_issue.get(recheck.issue_id)
            replayed = (generated_lookup(issue, source, old) if generated_lookup and issue and old
                        and issue.report_artifact_id == prior.artifact_id and old['outcome'] == 'FAIL'
                        and old['testId'].startswith('run_unit_tests:') and old.get('testVersion','').startswith('inputs:')
                        else None)
            if replayed:
                new = replayed
        measured_unit = False
        if not current and not new:
            effect = {'status': 'NOT_COMPARABLE', 'reason': 'CURRENT_REPORT_MISSING'}
        elif (parent is None or parent.artifact_type != 'SOURCE' or parent.code_version + 1 != source.code_version
                or review.working_base_commit != parent.commit_hash or review.source_artifact_id != source.artifact_id):
            effect = {'status': 'NOT_COMPARABLE', 'reason': 'SOURCE_MISMATCH'}
        else:
            measured_unit = bool(old and new and old.get('testVersion', '').startswith('inputs:')
                                 and new.get('testVersion') == old['testVersion'] and prior
                                 and _receipt_bound(prior, old) and (new.get('recheckReceiptId')
                                 or current and _receipt_bound(current, new)))
            if measured_unit and new['outcome'] == 'PASS' and not _retains_generated_coverage(old, new):
                new = {**new, 'outcome': 'UNVERIFIED'}
            if versions is None and old and new and not (old.get('testVersion') and new.get('testVersion')):
                try:
                    versions = legacy_practice_versions(configuration)
                except (OSError, ValueError):
                    versions = {}  # An unavailable legacy catalog cannot block the final package.
            effect = edit_effect(compare_check(old, new, {
                'beforeVersion': parent.code_version, 'afterVersion': source.code_version,
                'beforeSnapshot': parent.snapshot_sha256, 'afterSnapshot': source.snapshot_sha256}, (versions or {}).get(key)))
        results.append({'issueId': str(recheck.issue_id), 'role': recheck.role, 'testId': recheck.test_id,
                        'requirementId': str(recheck.requirement_id),
                        **({'priorProtectedReceiptId': str(protected_id),
                            'currentProtectedReceiptId': new['protectedReceiptId'] if new else None}
                           if protected_id else {'priorReportId': str(recheck.prior_report_id)}),
                        'currentReportId': str(current.artifact_id) if current and not (new or {}).get('recheckReceiptId') else None,
                        **({'currentGeneratedReceiptId': new['recheckReceiptId']} if (new or {}).get('recheckReceiptId') else {}),
                        'currentOutcome': (None if not new else new['outcome'] if measured_unit
                                           or effect['status'] != 'NOT_COMPARABLE' else 'UNVERIFIED'),
                        'editEffect': effect,
                        'caseRechecks': _case_rechecks(recheck, old, new, effect, measured_unit)})
    return results


def _case_rechecks(recheck, before, after, effect, measured_unit=False):
    """Only grade the same failed case on the same test bytes and validated candidate."""
    comparable = effect['status'] != 'NOT_COMPARABLE'
    previous = _observations(before) if comparable and before else None
    current = _observations(after) if comparable and after else None
    results = []
    for case in getattr(recheck, 'unresolved_cases', ()):
        prior_items = {key: item for key, item in (previous or {}).items() if key[0] == case}
        current_items = {key: item for key, item in (current or {}).items() if key[0] == case}
        outcome = 'UNVERIFIED'
        if (prior_items and current_items.keys() == prior_items.keys()
                and all(current_items[key]['kind'] == old['kind'] for key, old in prior_items.items())):
            states = {item['status'] for item in current_items.values()}
            if 'FAIL' in states:
                outcome = 'FAIL'
            elif states == {'PASS'}:
                outcome = 'PASS'
        results.append({'caseId': case, 'outcome': outcome})
    # A generated unittest can have a trustworthy case verdict without exposing
    # its individual assertions. Never turn a similarly named, regenerated test into PASS.
    if not results and recheck.test_id.startswith('run_unit_tests:'):
        if before and after and (measured_unit or comparable and before.get('testVersion') == after.get('testVersion')):
            results.append({'caseId': recheck.test_id, 'outcome': after['outcome']})
        else:
            results.append({'caseId': recheck.test_id, 'outcome': 'UNVERIFIED'})
    return results


def legacy_practice_versions(config):
    """Resolve old, unversioned checks only from the exact frozen built-in practice catalog."""
    ref = config.configuration.protected_test_suite_ref
    if not isinstance(ref, str) or not ref.startswith('artifact://protected-tests/'):
        return {}
    from evaluation import protected_tests
    suite = protected_tests.load_suite()
    if protected_tests.loop_suite_ref(suite) != ref:
        return {}
    versions = {}
    for role in protected_tests.ROLES:
        try:
            cases = protected_tests.owned_cases(config.scenario_contract, role, suite)['cases']
        except ValueError:
            return {}
        for case in cases:
            identity = ':'.join(case[k] for k in ('toolName', 'selector', 'testId'))
            versions[role, case['requirementId'], identity] = 'practice:' + ref.rsplit('practice-', 1)[1]
    return versions


def _receipt_bound(report, row):
    entries = report.tests if report.artifact_type == 'QA_REPORT' else report.requirement_results
    matching = [entry for entry in entries if str(entry.requirement_id) == row['requirementId']
                and getattr(entry, 'test_id', row['testId']) == row['testId']]
    if len(matching) != 1:
        return False
    entry = matching[0]
    return any(proof and proof.tool_name == 'run_unit_tests' and proof.outcome.value == 'PASS'
               and proof.execution_manifest == report.execution_manifest and proof.evidence_ref in row['evidenceRefs']
               for proof in (entry.tool_evidence, *entry.execution_evidence))


def _retains_generated_coverage(before, after):
    if before.get('observations'):
        left, right = _observations(before), _observations(after)
        if (not left or not right or left.keys() != right.keys()
                or any(right[key]['kind'] != old['kind'] or right[key]['status'] != 'PASS'
                       for key, old in left.items())):
            return False
    old_marks, new_marks = before.get('outputChecks') or {}, after.get('outputChecks') or {}
    if any(mark in ('CHECKED', 'EXPOSED') and new_marks.get(channel) != 'CHECKED'
           for channel, mark in old_marks.items()):
        return False
    old_facts, new_facts = before.get('signupChecks') or {}, after.get('signupChecks') or {}
    for key, value in old_facts.items():
        if key not in new_facts:
            return False
        if isinstance(value, dict) and (not isinstance(new_facts[key], dict)
                                        or not value.keys() <= new_facts[key].keys()):
            return False
    return True


def generated_issue_recheck(issue, original_report, current_report, *, replayed=None):
    """Require the old unit case and content identity, including protected cases from role reports."""
    if (issue.category not in ('QA_ASSERTION_FAILURE', 'SECURITY_REQUIREMENT_FAILURE')
            or original_report is None or issue.report_artifact_id != original_report.artifact_id
            or issue.code_version != original_report.code_version or issue.run_id != original_report.run_id
            or issue.source_artifact_id != original_report.execution_manifest.project_artifact_id):
        return None
    _, names = _issue_test_ids(issue.model_dump(mode='json', by_alias=True))
    requirements = {str(req) for req in issue.requirement_ids}
    original = [row for row in validation_feedback([original_report])
                if row['requirementId'] in requirements and row['testId'] in names
                and row['outcome'] == 'FAIL' and row['testId'].startswith('run_unit_tests:')
                and (not row.get('testVersion') or row['testVersion'].startswith(('inputs:', 'practice:')))]
    if not original:
        return None
    if current_report is not None and (current_report.artifact_type != original_report.artifact_type
            or current_report.run_id != issue.run_id or current_report.code_version <= issue.code_version):
        return 'UNVERIFIED'

    current = _index(validation_feedback([current_report])) if current_report else {}
    outcomes = []
    for old in original:
        measured = (replayed(old) if replayed and old.get('testVersion', '').startswith('inputs:')
                    and _receipt_bound(original_report, old) else None)
        if measured is not None:
            outcomes.append(measured['outcome'])
            continue
        new = current.get((old['role'], old['requirementId'], old['testId']))
        if (not old.get('testVersion') or not new or new.get('testVersion') != old['testVersion']
                or not _receipt_bound(original_report, old) or not _receipt_bound(current_report, new)):
            outcomes.append('UNVERIFIED')
        elif old['testVersion'].startswith('practice:') and new['outcome'] == 'PASS':
            prior_items, current_items = _observations(old), _observations(new)
            outcomes.append('PASS' if prior_items and current_items and prior_items.keys() == current_items.keys()
                            and all(current_items[k]['kind'] == v['kind'] and current_items[k]['status'] == 'PASS'
                                    for k,v in prior_items.items()) else 'UNVERIFIED')
        elif new['outcome'] == 'PASS' and not _retains_generated_coverage(old, new):
            outcomes.append('UNVERIFIED')
        else:
            outcomes.append(new['outcome'])
    if 'FAIL' in outcomes:
        return 'FAIL'
    return 'PASS' if all(outcome == 'PASS' for outcome in outcomes) else 'UNVERIFIED'


def protected_issue_progress(previous, current, parent, source):
    """Compare two admitted failures of the same protected case on adjacent frozen snapshots."""
    from orchestrator.domain.protected_feedback import protected_feedback_row
    before, after = previous.protected_receipt, current.protected_receipt
    if (not before or not after or before.test_id != after.test_id or previous.reporter != current.reporter
            or previous.requirement_ids != current.requirement_ids or parent.run_id != source.run_id
            or parent.artifact_id != source.previous_artifact_id or source.code_version != parent.code_version + 1):
        return 'NOT_COMPARABLE'
    effect = compare_check(protected_feedback_row(previous, parent), protected_feedback_row(current, source), {
        'beforeVersion': parent.code_version, 'afterVersion': source.code_version,
        'beforeSnapshot': parent.snapshot_sha256, 'afterSnapshot': source.snapshot_sha256})
    status = effect['status']
    return status if status in ('NO_PROGRESS', 'PARTIAL_IMPROVEMENT', 'NOT_COMPARABLE') else 'CHANGED'


def issue_progress(previous, current, parent, source, old_report, new_report, legacy):
    """Only credit an improvement when all checks involved in this issue can be compared."""
    if (source.previous_artifact_id != parent.artifact_id or source.run_id != parent.run_id
            or source.code_version != parent.code_version + 1 or previous.category != current.category
            or set(previous.requirement_ids) != set(current.requirement_ids)):
        return 'NOT_COMPARABLE'
    targets = set()
    reports = []
    for issue, snapshot, report in ((previous, parent, old_report), (current, source, new_report)):
        payload = issue.model_dump(mode='json', by_alias=True)
        if (issue.report_artifact_id != report.artifact_id or issue.source_artifact_id != snapshot.artifact_id
                or report.execution_manifest != snapshot.execution_manifest()):
            return 'NOT_COMPARABLE'
        rows = validation_feedback([snapshot, report])
        selected = selected_checks(payload, rows)
        _, named = _issue_test_ids(payload)
        if not selected or {r['testId'] for r in selected} != named:
            return 'NOT_COMPARABLE'
        targets.update((r['role'], r['requirementId'], r['testId']) for r in selected)
        reports.append(rows)
    before, after = ([r for r in rows if (r['role'], r['requirementId'], r['testId']) in targets]
                     for rows in reports)
    if any(len(rows) != len(targets) for rows in (before, after)):
        return 'NOT_COMPARABLE'
    for rows, report in zip((before, after), (old_report, new_report)):
        entries = report.tests if report.artifact_type == 'QA_REPORT' else report.requirement_results
        for row in rows:
            # Count only observations backed by an executed tool on this exact snapshot.
            matching = [entry for entry in entries if str(entry.requirement_id) == row['requirementId']
                        and getattr(entry, 'test_id', row['testId']) == row['testId']]
            if len(matching) != 1:
                return 'NOT_COMPARABLE'
            entry = matching[0]
            refs = row['evidenceRefs']
            if report.artifact_type == 'SECURITY_REPORT':
                # The requirement's primary receipt can concern a different test.
                detail = json.loads(entry.details or '{}')
                refs = next((t.get('evidenceRefs', []) for t in detail.get('tests', [])
                             if t['testId'] == row['testId']), []) if isinstance(detail, dict) else []
            receipts = {e.evidence_ref for e in (entry.tool_evidence, *entry.execution_evidence)
                        if e and e.tool_name == 'run_unit_tests' and e.outcome.value == 'PASS'
                        and e.execution_manifest == report.execution_manifest}
            if not receipts.intersection(refs):
                return 'NOT_COMPARABLE'
    history = {'beforeVersion': parent.code_version, 'afterVersion': source.code_version,
               'beforeSnapshot': parent.snapshot_sha256, 'afterSnapshot': source.snapshot_sha256,
               'changedFiles': None, 'changedFunctions': {}, 'previousRows': before, 'legacyTestVersions': legacy}
    checks = compare_repair(history, after)['checks']
    if len(checks) != len(targets) or any(c['status'] == 'NOT_COMPARABLE' for c in checks):
        return 'NOT_COMPARABLE'
    transitions = {t for check in checks for t in check['counts']}
    if (all(c['status'] in ('PARTIAL_IMPROVEMENT', 'RESOLVED', 'NO_PROGRESS') for c in checks)
            and 'NOW_PASSING' in transitions and transitions <= {'UNCHANGED', 'NOW_PASSING'}):
        return 'PARTIAL_IMPROVEMENT'
    if all(check['status'] == 'NO_PROGRESS' for check in checks):
        return 'NO_PROGRESS'
    return 'CHANGED'
