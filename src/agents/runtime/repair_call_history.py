"""Tie a previous exception site and a source edit to a comparable, measured recheck.

Source syntax is not runtime evidence. Only the original report supplies the
exception location. The new location is a match in the submitted source.
"""
import ast
import re
from collections import defaultdict
from hashlib import sha256

from agents.runtime.library_contracts import call_parameters, public_import
from agents.runtime.normal_request_repair import _public_callee, measured_prerequisite
from agents.runtime.ready_path_diff import FUNCTION, _bounded_tree, _name, _nodes
from agents.runtime.repair_locations import diagnostic_sites
from orchestrator.core.security import redact_text
from orchestrator.domain.repair_progress import _generated_observations, _receipt_bound
from orchestrator.domain.validation_feedback import validated_output_diagnostics, validation_feedback


def _key(row):
    return tuple(row.get(k) for k in ('role', 'requirementId', 'testId', 'reportArtifactId'))


def _call_pair(source, path, line, newer, environment):
    if max(len(source[path]), len(newer[path])) > 128000:
        return None
    try:
        trees = [ast.parse(files[path]) for files in (source, newer)]
        if not all(_bounded_tree(tree) for tree in trees):
            return None
        old_scopes = [node for node in trees[0].body if isinstance(node, FUNCTION)
                      and node.lineno <= line <= node.end_lineno]
        if len(old_scopes) != 1:
            return None
        name = old_scopes[0].name
        scopes = [[node for node in tree.body if isinstance(node, FUNCTION) and node.name == name] for tree in trees]
        if any(len(group) != 1 for group in scopes) or len(name) > 128 or redact_text(name) != name:
            return None
        calls = [[node for node in _nodes(group[0].body) if isinstance(node, ast.Call)] for group in scopes]
        old = [node for node in calls[0] if node.lineno <= line <= node.end_lineno]
        if len(old) != 1:
            return None
        symbol = _name(old[0].func)
        if not symbol or any(sum(_name(call.func) == symbol for call in nodes) != 1 for nodes in calls):
            return None
        new = next(node for node in calls[1] if _name(node.func) == symbol)
        origin = public_import(old[0], trees[0], source)
        if not origin or origin != public_import(new, trees[1], newer):
            return None
        contract = call_parameters(new, newer[path], newer, environment)
        if contract:
            exported = origin[1]
        else:
            exported = _public_callee(old[0], source[path], source)
            if exported == '<call>':
                return None
        return name, exported, old[0], new, contract
    except (SyntaxError, ValueError, RecursionError):
        return None


def _operand_shape(node):
    if isinstance(node, ast.Constant):
        return type(node.value).__name__
    return 'expression'


def _changed_arguments(old, new, contract):
    # Only emit argument names verified by the pinned signature, or the existing
    # public 'type' label. Unknown names, payloads and literal values stay private.
    names = {'type'} | set((contract or {}).get('parameters', ()))
    if any(kw.arg is None for call in (old, new) for kw in call.keywords):
        return []
    left, right = ({kw.arg: kw.value for kw in call.keywords} for call in (old, new))
    result = []
    for name in sorted(names & left.keys() & right.keys()):
        a, b = left[name], right[name]
        if ast.dump(a) != ast.dump(b):
            result.append({'parameter': name, 'beforeShape': _operand_shape(a), 'afterShape': _operand_shape(b)})
    return result[:6]


def _channel_marks(row):
    # Missing per-assertion detail can coexist with valid, independently captured
    # output-channel measurements. Other missing/invalid reasons stay unknown.
    if (row.get('observationStatus') != 'UNVERIFIED' or row.get('reason') != 'DETAIL_RECORD_MISSING'
            or not re.fullmatch(r'inputs:[0-9a-f]{64}', row.get('testVersion') or '')
            or row.get('outcome') != 'FAIL' or not _generated_observations(row)):
        return None
    marks = row['outputChecks']
    if 'outputDiagnostics' in row:
        try:
            validated_output_diagnostics(row['outputDiagnostics'], marks)
        except ValueError:
            return None
    return marks


def channel_receipts(reports, snapshot, rows):
    reports_by_id = {str(report.artifact_id): report for report in reports
                     if report.artifact_type in ('QA_REPORT', 'SECURITY_REPORT')
                     and report.run_id == snapshot.run_id and report.execution_manifest == snapshot.execution_manifest()}
    return {_key(row) for row in rows if _channel_marks(row)
            and (report := reports_by_id.get(row['reportArtifactId'])) is not None and _receipt_bound(report, row)}


def current_channel_receipts(reports, snapshot):
    current = [r for r in reports if r.artifact_type in ('QA_REPORT', 'SECURITY_REPORT')
               and r.run_id == snapshot.run_id and r.execution_manifest == snapshot.execution_manifest()]
    return channel_receipts(current, snapshot, validation_feedback([snapshot, *current]))


def _eligible_rows(rows, review, measured_channels):
    admitted = {(r.role, str(r.requirement_id), r.test_id, str(r.prior_report_id))
                for r in review.rechecks if r.prior_report_id is not None}
    return [{**row, 'hostDecision': 'FIX_TARGET'} for row in rows
            if _key(row) in admitted and row['outcome'] == 'FAIL' and row.get('testVersion')
            and (row.get('observationStatus') == 'COMPLETE' or _key(row) in measured_channels and _channel_marks(row))]


def recorded_call_edits(before, after, rows, review, environment, *, measured_channels=()):
    """A cross-check link is allowed only for the same measured normal request on one snapshot."""
    selected = _eligible_rows(rows, review, measured_channels)
    if len({(row.get('runId'), row.get('sourceArtifactId'), row.get('codeVersion'))
            for row in selected}) != 1:
        return []
    records = defaultdict(list)
    normal = {}
    for row in selected:
        blocker, frames = measured_prerequisite([row])
        if blocker and blocker['failedCreationCases']:
            normal[_key(row)] = blocker['failedCreationCases']
        for f in frames:
            records[(f['sourceSha256'], f['line'])].append((_key(row), True))
        for diagnostics in (row.get('outputDiagnostics') or {}).values():
            for item in diagnostics:
                frame = item.get('appFrame')
                if item.get('escapedToClient') is True and frame:
                    records[(frame['sourceSha256'], frame['line'])].append((_key(row), False))
    if len(records) > 8:
        return []
    frames = [{'sourceSha256': h, 'line': line} for h, line in records]
    normal_sites = {coord for coord, witnesses in records.items() if any(is_normal for _, is_normal in witnesses)}
    results = []
    for site in diagnostic_sites(frames, before):
        path = site['toolPath'].removeprefix('source/')
        if path not in after or before[path] == after[path]:
            continue
        pair = _call_pair(before, path, site['line'], after, environment)
        if pair is None:
            continue
        scope, callable_name, old, new, contract = pair
        edits = _changed_arguments(old, new, contract)
        if not edits:
            continue
        coord = site['sourceSha256'], site['line']
        witnesses = sorted({key for key, _ in records[coord]})
        eligible = {key: [] for key in witnesses}
        if normal_sites == {coord}:
            eligible.update(normal)
        entry = {'sourceRef': site['toolPath'] + '::' + scope, 'callable': callable_name,
                 'priorExceptionSite': site,
                 'candidateSourceSite': {'toolPath': site['toolPath'], 'line': new.lineno,
                                         'sourceSha256': sha256(after[path]).hexdigest()},
                 'argumentEdits': edits,
                 'priorEvidence': [dict(zip(('role','requirementId','testId','reportArtifactId'), key))
                                   for key in witnesses]}
        if contract:
            entry['libraryContract'] = contract
        results.append((entry, eligible))
    return results


def attach_unchanged_measurements(result, history, current):
    """Use only Host-selected, byte-comparable failed checks on this exact new candidate."""
    data = history.get('recordedCallEdits', ())
    if not data:
        return
    rows = {_key(row): row for row in current if row.get('hostDecision') == 'FIX_TARGET'
            and row.get('sourceArtifactId') == history['afterSourceId']
            and row.get('executionManifest', {}).get('snapshotSha256') == history['afterSnapshot']
            and row.get('testVersion') and row.get('outcome') == 'FAIL'}
    output = []
    for edit, eligible in data:
        measured = []
        for check in result['checks']:
            identity = tuple(check[k] for k in ('role','requirementId','testId'))
            old_key = (*identity, check['beforeReportArtifactId'])
            new_key = (*identity, check['afterReportArtifactId'])
            if check['status'] != 'NO_PROGRESS' or old_key not in eligible or new_key not in rows:
                continue
            row = rows[new_key]
            channels = {}
            if row.get('observationStatus') != 'COMPLETE':
                if new_key not in history.get('afterChannelReceipts', ()) or not (marks := _channel_marks(row)):
                    continue
                channels = {'unchangedOutputChannels': marks}
            facts = [{k:v for k,v in case.items() if k in ('caseId','httpStatus','statusOrigin','user_delta','account_count')}
                     for case in eligible[old_key][:6]]
            measured.append({'role': check['role'], 'requirementId': check['requirementId'], 'testId': check['testId'],
                             'beforeReportArtifactId': old_key[-1], 'afterReportArtifactId': new_key[-1],
                             'testVersion': row['testVersion'], 'status': 'NO_PROGRESS', **channels,
                             **({'unchangedCases': facts} if facts else {})})
        if measured:
            output.append({**edit, 'unchangedChecks': measured})
    if output:
        result['measuredCallEdits'] = output
