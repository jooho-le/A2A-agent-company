"""Offline adapters to dev 2b4fc60 report data payloads.

Caller owns registered context, trusted test bindings, redaction and actual
execution/evidence collection. This module neither executes tools nor sends A2A.
"""
from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
import json
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .aggregation import Status, _uuid4, aggregate
from .manifest import ExecutionManifest


@dataclass(frozen=True)
class TestBinding:
    external_test_id: str
    requirement_id: str
    title: str


def validate_contract(payload: dict, name: str) -> None:
    """Validate with pinned local schemas; remote retrieval is disabled."""
    documents = [json.loads(path.read_text()) for path in
                 files('evaluation').joinpath('contracts').iterdir()
                 if path.name.endswith('.schema.json')]
    registry = Registry().with_resources(
        (doc['$id'], Resource.from_contents(doc)) for doc in documents
    )
    schema = next(doc for doc in documents if doc['$id'].endswith('/' + name + '.schema.json'))
    Draft202012Validator(schema, registry=registry, format_checker=FormatChecker()).validate(payload)


def _artifact_uri(value):
    parsed = urlsplit(value)
    if (not parsed.scheme or parsed.scheme.lower() == 'file' or len(parsed.scheme) == 1
            or not (parsed.netloc or parsed.path) or '\\' in value
            or any(c.isspace() for c in value)
            or (parsed.scheme.lower() in {'http', 'https', 's3', 'gs'} and not parsed.netloc)):
        raise ValueError('Expected a non-local Artifact Registry URI')


def _tool_evidence(evidence, manifest, role, outcome):
    if evidence is None:
        if outcome != 'UNVERIFIED':
            raise ValueError('PASS/FAIL requires actual Tool execution evidence')
        return None
    validate_contract(evidence, 'tool_execution_evidence')
    if ExecutionManifest.from_payload(evidence['executionManifest']) != manifest:
        raise ValueError('Tool evidence belongs to another Manifest')
    allowed = {'run_unit_tests', 'run_browser_tests'} if role == 'QA' else {'run_security_scan'}
    if evidence['toolName'] not in allowed:
        raise ValueError('Tool evidence does not match the report role')
    _artifact_uri(evidence['evidenceRef'])
    attempts = evidence['attempts']
    for item in attempts:
        _artifact_uri(item['evidenceRef'])
    if [item['attempt'] for item in attempts] != list(range(len(attempts))):
        raise ValueError('Tool attempts must start at zero and be contiguous')
    for item in attempts[:-1]:
        safe = item.get('errorKind') in {'PROCESS_STARTUP_FAILURE', 'RESOURCE_BUSY'} or (
            item.get('retrySafe') is True and item.get('errorKind') in
            {'TIMEOUT', 'TOOL_TIMEOUT', 'MCP_TRANSPORT_INTERRUPTED'})
        if item['outcome'] != 'UNVERIFIED' or not safe:
            raise ValueError('Retry follows a non-retryable attempt')
    expected = 'UNVERIFIED' if outcome == 'UNVERIFIED' else 'PASS'
    if attempts[-1]['outcome'] != expected:
        raise ValueError('Tool completion and product outcome are inconsistent')
    return deepcopy(evidence)


def build_report(*, role, context, expected, observed, plan, results,
                 bindings, tool_evidence, findings=None) -> dict:
    """Build QA_REPORT or SECURITY_REPORT using a trusted complete required plan.

    No automatic ID generation, final verdict or N/A policy. ERROR/NOT_RUN map
    to UNVERIFIED while original status, reason and evidence IDs stay in details.
    Security currently requires one pre-aggregated check per requirement; reject
    multi-check groups instead of silently discarding their execution evidence.
    """
    if role not in ('QA', 'SECURITY'):
        raise ValueError('Unsupported report role')
    if not isinstance(expected, ExecutionManifest) or observed != expected:
        raise ValueError('Cannot publish results from a missing/mismatched Manifest')
    required_context = {'artifactId', 'artifactVersion', 'previousArtifactId', 'runId',
                        'workflowStepId', 'a2aTaskId', 'a2aArtifactId', 'createdAt',
                        'requirementIds'}
    if not isinstance(context, dict) or not required_context <= context.keys() or context.keys() - required_context - {'artifactUri'}:
        raise ValueError('Report context has missing or unknown fields')
    checks, results = tuple(plan), tuple(results)
    if any(check.required is not True for check in checks):
        raise ValueError('Only the complete required plan can be exported')
    summary = aggregate(checks, results)
    ids = {check.test_id for check in checks}
    if set(bindings) != ids or set(tool_evidence) - ids:
        raise ValueError('Bindings/evidence do not match the fixed plan')
    external_ids, requirement_ids = [], []
    for binding in bindings.values():
        if not isinstance(binding, TestBinding):
            raise ValueError('Expected explicit trusted TestBinding')
        _uuid4(binding.requirement_id)
        for text in (binding.external_test_id, binding.title):
            if not isinstance(text, str) or not text.strip() or text != text.strip():
                raise ValueError('Test binding text must not be blank')
        external_ids.append(binding.external_test_id)
        requirement_ids.append(binding.requirement_id)
    if len(external_ids) != len(set(external_ids)):
        raise ValueError('Duplicate external test ID')
    if set(requirement_ids) != set(context['requirementIds']):
        raise ValueError('Bindings do not cover the exact Step requirement set')
    if role == 'SECURITY' and len(requirement_ids) != len(set(requirement_ids)):
        raise ValueError('Security needs one pre-aggregated check per requirement')
    # N/A has no lossless representation in the current common contract.
    if any(result.status == Status.NOT_APPLICABLE for result in results):
        raise ValueError('NOT_APPLICABLE requires an agreed external contract')
    rows = []
    executions = {}
    received_status = {result.test_id: result.status.value for result in results}
    for row in summary['results']:
        binding = bindings[row['testId']]
        outcome = row['status'] if row['status'] in ('PASS', 'FAIL') else 'UNVERIFIED'
        item = {
            'requirementId': binding.requirement_id, 'outcome': outcome,
            'details': json.dumps({'localTestId': row['testId'], 'localStatus': row['status'],
                                   'reportedStatus': received_status.get(row['testId']),
                                   'errorCode': row['errorCode'], 'evidenceIds': row['evidenceIds']},
                                  sort_keys=True),
            'toolEvidence': _tool_evidence(tool_evidence.get(row['testId']), expected, role, outcome),
        }
        evidence = item['toolEvidence']
        if evidence is not None:
            execution_id = evidence['executionId']
            if execution_id in executions and executions[execution_id] != evidence:
                raise ValueError('Contradictory evidence for the same execution ID')
            executions[execution_id] = evidence
        if role == 'QA':
            item.update(testId=binding.external_test_id, title=binding.title)
        rows.append(item)
    report = deepcopy(context)
    report.update(artifactType=role + '_REPORT', createdBy=role,
                  codeVersion=expected.code_version, executionManifest=expected.to_payload())
    report['tests' if role == 'QA' else 'requirementResults'] = rows
    if role == 'SECURITY':
        if findings is None:
            raise ValueError('Security findings must be explicitly supplied, including an empty list')
        report['findings'] = deepcopy(findings)
    elif findings is not None:
        raise ValueError('QA reports cannot carry Security findings')
    validate_contract(report, 'qa_report' if role == 'QA' else 'security_report')
    if (report['artifactVersion'] == 1) != (report['previousArtifactId'] is None):
        raise ValueError('Artifact lineage version and predecessor disagree')
    if 'artifactUri' in report:
        _artifact_uri(report['artifactUri'])
    if report['artifactId'] == report['previousArtifactId']:
        raise ValueError('Artifact cannot be its own predecessor')
    for key in ('a2aTaskId', 'a2aArtifactId'):
        if not report[key].strip():
            raise ValueError('A2A references must not be blank')
    if role == 'SECURITY':
        finding_ids = [finding['findingId'] for finding in report['findings']]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError('Duplicate finding ID')
        for finding in report['findings']:
            for key in ('findingId', 'title', 'description'):
                if not finding[key].strip():
                    raise ValueError('Finding text must not be blank')
            if finding.get('requirementId') is not None and finding['requirementId'] not in report['requirementIds']:
                raise ValueError('Finding refers to a requirement outside the Step')
    return report
