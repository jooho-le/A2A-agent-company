from copy import deepcopy
from dataclasses import replace
import json
from uuid import UUID

import pytest
from jsonschema import ValidationError

from evaluation.aggregation import CheckPlan, CheckResult, Status
from evaluation.manifest import ExecutionManifest
from evaluation.reporting import TestBinding as Binding, build_report, validate_contract


def uid(n):
    return str(UUID(int=n, version=4))


def inputs(role='QA', status=Status.PASS):
    manifest = ExecutionManifest('repo', 1, uid(1), 'sha1', 'a'*40, 'b'*40,
                                 'c'*64, 'sha256:'+'d'*64, 'sha256:'+'e'*64)
    return dict(
        role=role, context=dict(artifactId=uid(2), artifactVersion=1,
            previousArtifactId=None, runId=uid(3), workflowStepId=uid(4),
            a2aTaskId='task/opaque', a2aArtifactId='artifact/opaque',
            createdAt='2026-10-04T12:00:00Z', requirementIds=[uid(5)]),
        expected=manifest, observed=manifest, plan=[CheckPlan(uid(6))],
        results=[CheckResult(uid(6), status, (uid(7),))],
        bindings={uid(6): Binding('QA-SIGNUP-001', uid(5), 'Signup')},
        tool_evidence={uid(6): dict(toolName='run_unit_tests' if role == 'QA' else 'run_security_scan',
            executionId=uid(8), executionManifest=manifest.to_payload(),
            evidenceRef='artifact://evidence/report.json', attempts=[dict(attempt=0,
                outcome='UNVERIFIED' if status in (Status.ERROR, Status.NOT_RUN) else 'PASS',
                evidenceRef='artifact://evidence/attempt.json')])},
        findings=[] if role == 'SECURITY' else None,
    )


@pytest.mark.parametrize('role', ['QA', 'SECURITY'])
@pytest.mark.parametrize('status', [Status.PASS, Status.FAIL, Status.ERROR, Status.NOT_RUN])
def test_reports_preserve_product_status_and_tool_provenance(role, status):
    args = inputs(role, status)
    report = build_report(**args)
    row = report['tests' if role == 'QA' else 'requirementResults'][0]
    assert row['outcome'] == (status.value if status in (Status.PASS, Status.FAIL) else 'UNVERIFIED')
    assert json.loads(row['details'])['localStatus'] == status.value
    assert row['toolEvidence'] == args['tool_evidence'][uid(6)]
    assert 'finalVerdict' not in report
    if role == 'QA':
        assert row['testId'] == 'QA-SIGNUP-001'
    validate_contract(report, 'qa_report' if role == 'QA' else 'security_report')


def test_missing_result_is_unverified_without_invented_execution():
    args = inputs(); args['results'] = []; args['tool_evidence'] = {}
    row = build_report(**args)['tests'][0]
    assert row['outcome'] == 'UNVERIFIED'
    assert row['toolEvidence'] is None
    assert json.loads(row['details'])['errorCode'] == 'RESULT_MISSING'


@pytest.mark.parametrize('status', [Status.PASS, Status.FAIL])
def test_product_verdict_without_tool_evidence_rejected(status):
    args = inputs(status=status); args['tool_evidence'] = {}
    with pytest.raises(ValueError, match='actual Tool'):
        build_report(**args)


@pytest.mark.parametrize('alteration', ['manifest', 'tool', 'gap', 'retry', 'outcome', 'uri'])
def test_bad_provenance_rejected(alteration):
    args = inputs(); evidence = args['tool_evidence'][uid(6)]
    if alteration == 'manifest': evidence['executionManifest']['snapshotSha256'] = 'f'*64
    if alteration == 'tool': evidence['toolName'] = 'run_security_scan'
    if alteration == 'gap': evidence['attempts'][0]['attempt'] = 1
    if alteration == 'retry':
        evidence['attempts'].append(dict(evidence['attempts'][0], attempt=1))
    if alteration == 'outcome': evidence['attempts'][0]['outcome'] = 'FAIL'
    if alteration == 'uri': evidence['evidenceRef'] = 'file:///tmp/report'
    with pytest.raises((ValueError, ValidationError)):
        build_report(**args)


def test_safe_retry_exhaustion_is_preserved():
    args = inputs(status=Status.ERROR); evidence = args['tool_evidence'][uid(6)]
    evidence['attempts'] = [dict(attempt=i, outcome='UNVERIFIED',
        evidenceRef=f'artifact://evidence/{i}', errorKind='RESOURCE_BUSY') for i in range(3)]
    report = build_report(**args)
    assert len(report['tests'][0]['toolEvidence']['attempts']) == 3


@pytest.mark.parametrize('alteration', ['binding_missing', 'requirement', 'optional', 'na',
                                      'context', 'lineage', 'snapshot', 'task'])
def test_ambiguous_input_rejected(alteration):
    args = inputs()
    if alteration == 'binding_missing': args['bindings'] = {}
    if alteration == 'requirement': args['context']['requirementIds'] = [uid(99)]
    if alteration == 'optional': args['plan'] = [CheckPlan(uid(6), required=False)]
    if alteration == 'na': args['results'] = [CheckResult(uid(6), Status.NOT_APPLICABLE)]
    if alteration == 'context': args['context']['finalVerdict'] = 'SUCCESS'
    if alteration == 'lineage': args['context']['artifactVersion'] = 2
    if alteration == 'snapshot': args['observed'] = replace(args['expected'], code_version=2)
    if alteration == 'task': args['context']['a2aTaskId'] = ' '
    with pytest.raises((ValueError, ValidationError)):
        build_report(**args)


def test_security_findings_preserved_without_mutating_input():
    args = inputs('SECURITY')
    finding = dict(findingId='finding-1', severity='HIGH', disposition='CONFIRMED',
                   title='Storage', description='Synthetic defect', requirementId=uid(5))
    args['findings'] = [finding]
    before = deepcopy(args)
    report = build_report(**args)
    assert report['findings'] == [finding]
    assert args == before
    report['findings'][0]['title'] = 'changed'
    assert finding['title'] == 'Storage'


@pytest.mark.parametrize('alteration', ['missing', 'duplicate', 'foreign_requirement', 'multiple_checks'])
def test_security_ambiguous_evidence_or_findings_rejected(alteration):
    args = inputs('SECURITY')
    if alteration == 'missing': args['findings'] = None
    if alteration in ('duplicate', 'foreign_requirement'):
        f = dict(findingId='f', severity='LOW', disposition='SUSPECTED', title='t',
                 description='d', requirementId=uid(999) if alteration == 'foreign_requirement' else uid(5))
        args['findings'] = [f, f] if alteration == 'duplicate' else [f]
    if alteration == 'multiple_checks':
        args['plan'].append(CheckPlan(uid(9)))
        args['bindings'][uid(9)] = Binding('SEC-2', uid(5), 'Second check')
    with pytest.raises(ValueError):
        build_report(**args)


def test_manifest_roundtrip_and_unknown_fields():
    manifest = inputs()['expected']
    assert ExecutionManifest.from_payload(manifest.to_payload()) == manifest
    with pytest.raises(ValueError):
        ExecutionManifest.from_payload(dict(manifest.to_payload(), snapshotId=uid(90)))


@pytest.mark.parametrize('uri', ['artifact:', 'https:/missing-host', 'FILE:///tmp/result'])
def test_schema_uri_is_also_compatible_with_dev_semantics(uri):
    args = inputs(); args['tool_evidence'][uid(6)]['evidenceRef'] = uri
    with pytest.raises((ValueError, ValidationError)):
        build_report(**args)


def test_same_execution_id_cannot_have_conflicting_evidence():
    args = inputs()
    args['plan'].append(CheckPlan(uid(9)))
    args['bindings'][uid(9)] = Binding('QA-2', uid(5), 'Second')
    args['results'].append(CheckResult(uid(9), Status.PASS, (uid(7),)))
    args['tool_evidence'][uid(9)] = deepcopy(args['tool_evidence'][uid(6)])
    args['tool_evidence'][uid(9)]['evidenceRef'] = 'artifact://conflicting/report'
    with pytest.raises(ValueError, match='Contradictory'):
        build_report(**args)
