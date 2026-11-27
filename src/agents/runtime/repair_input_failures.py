"""Associate invalid-input server responses with related, separately observed exception frames."""
import re

from agents.runtime.normal_request_repair import linkable_sites
from agents.runtime.repair_locations import INVALID_INPUT
from orchestrator.domain.constants import MAX_CODE_FIX_ATTEMPTS
from orchestrator.domain.validation_feedback import server_status_evidence, validate_diagnostic


_INVALID_CASE = re.compile(r'invalid_email(?:_\d+)?|step_\d+_invalid')
_MAX_CASE_EXAMPLES = 16
_MAX_EXCEPTION_EXAMPLES = 8


def _bound_row(row, source, protected_suite):
    manifest = row.get('executionManifest') or {}
    version = row.get('testVersion') or ''
    if not isinstance(manifest, dict) or not isinstance(version, str):
        return False
    if re.fullmatch('practice:[a-f0-9]{64}', version):
        if not isinstance(protected_suite, str) or not protected_suite.endswith('/practice-' + version[9:]):
            return False
    elif not re.fullmatch('inputs:[a-f0-9]{64}', version):
        return False
    return (row.get('hostDecision') == 'FIX_TARGET' and row.get('outcome') == 'FAIL'
            and row.get('observationStatus') == 'COMPLETE' and bool(source.get('runId'))
            and row.get('runId') == source['runId']
            and row.get('sourceArtifactId') == manifest.get('projectArtifactId') == source['artifactId']
            and row.get('codeVersion') == manifest.get('codeVersion') == source.get('codeVersion')
            and all(source.get(k) and manifest.get(k) == source[k] for k in
                    ('snapshotSha256', 'commitHash', 'treeHash', 'gitObjectFormat', 'repositoryId',
                     'containerImageDigest', 'dependencyLockHash')))


def _case_reference(row, case_id):
    return {**{k: row[k] for k in ('role', 'testId', 'requirementId', 'reportArtifactId', 'protectedReceiptId')
               if row.get(k)}, 'caseId': case_id}


def _check_identity(record):
    return tuple(record.get(k) for k in ('role', 'requirementId', 'testId', 'reportArtifactId', 'protectedReceiptId'))


def _related_exception(obs):
    if (obs.get('kind') != 'precondition' or obs.get('itemId') != 'response_status'
            or obs.get('handlerEntry') != 'ENTERED' or obs.get('status') != 'UNVERIFIED'
            or obs.get('actual') is not None or obs.get('reason') != 'UNHANDLED_APP_EXCEPTION'):
        return None
    diagnostic = obs.get('diagnostic')
    if not isinstance(diagnostic, dict):
        return None
    frame = diagnostic.get('appFrame')
    if isinstance(frame, dict):
        # The A2A protobuf Struct can decode a JSON integer as a float.
        line = frame.get('line')
        if type(line) is float and line.is_integer():
            diagnostic = {**diagnostic, 'appFrame': {**frame, 'line': int(line)}}
    try:
        validate_diagnostic(diagnostic)
    except ValueError:
        return None
    if not diagnostic.get('escapedToClient'):
        return None
    return {k: diagnostic[k] for k in ('exceptionType', 'escapedToClient', 'responseStarted', 'appFrame')
            if k in diagnostic}


def measured_invalid_input(fix, configuration):
    """Use only selected, current-source observations, independent of the architecture and suite edition."""
    sources = [a['record'] for a in fix.get('inputArtifacts', ()) if a.get('artifactType') == 'SOURCE'
               and isinstance(a.get('record'), dict) and a.get('artifactId')
               and a['record'].get('artifactId') == a['artifactId']]
    if len(sources) != 1:
        return None
    source = sources[0]
    code_version = source.get('codeVersion')
    if (type(code_version) not in (int, float) or not 1 <= code_version <= MAX_CODE_FIX_ATTEMPTS + 1
            or int(code_version) != code_version):
        return None
    failed, related = [], []
    for row in fix.get('validationFeedback', ()):
        if not _bound_row(row, source, configuration.get('protectedTestSuiteRef')):
            continue
        for obs in row.get('observations') or ():
            case = obs.get('caseId') or ''
            if not isinstance(case, str) or not _INVALID_CASE.fullmatch(case):
                continue
            actual = obs.get('actual')
            if (row.get('role') == 'QA' and obs.get('kind') == 'assertion' and obs.get('itemId') == 'http_status'
                    and obs.get('status') == 'FAIL' and type(actual) in (int, float)
                    and 500 <= actual < 600 and int(actual) == actual):
                failed.append({**_case_reference(row, case), 'httpStatus': int(actual), **server_status_evidence(obs)})
            elif row.get('role') in ('QA', 'SECURITY') and (diag := _related_exception(obs)):
                related.append({**_case_reference(row, case), 'handlerEntry': 'ENTERED', **diag})
    if not failed:
        return None
    return {'sourceArtifactId': source['artifactId'], 'codeVersion': int(code_version),
            'snapshotSha256': source['snapshotSha256'], 'failedCases': failed,
            'relatedExceptions': related,
            'instruction': ('Each failedCases status is the recorded QA value. HTTP_RESPONSE means the client received '
                            'that status; ESCAPED_EXCEPTION means the client raised and the check assigned 500. '
                            'UNKNOWN means the original observation did not distinguish them. A related exception or source site belongs '
                            'to its named test and case on this snapshot. Inspect the operation before assuming that '
                            'it caused another request to fail. Do not apply an after-entry frame to a before-entry response.')}


def attach_input_source_sites(group, files, focus):
    # The raw measured observations remain in fixRequest. Bound the duplicate, prompt-leading brief.
    by_check = {}
    for failed in group['failedCases']:
        by_check.setdefault(_check_identity(failed), []).append(failed['caseId'])
    for target, brief_target in zip(focus['targets'], focus['brief']['targets']):
        for detail, check in zip(target['failingChecks'], brief_target['checks']):
            cases = by_check.get(_check_identity(detail))
            if cases:
                check.setdefault('observedFailurePaths', {})[INVALID_INPUT] = cases
    group['failedCaseCount'] = len(group['failedCases'])
    group['relatedExceptionCount'] = len(group['relatedExceptions'])
    group['failedCases'] = group['failedCases'][:_MAX_CASE_EXAMPLES]
    group['relatedExceptions'] = group['relatedExceptions'][:_MAX_EXCEPTION_EXAMPLES]
    frames = [case['appFrame'] for case in group['relatedExceptions'] if 'appFrame' in case]
    group['sourceSites'] = linkable_sites(frames, files) if files and frames else []
