"""Host-only replay of an already measured generated unit scope on a new Source.

The regular run_unit_tests call, role ACL, Sandbox, budget and receipts are used.
The short-lived claim selects immutable previous test inputs for that call. A
later validator draft is free to change its own tests in the normal workspace.
"""
import json
from hashlib import sha256
from pathlib import Path

from mcp_tools.tools.unit_inputs import UnitTestInputs, UnitTestInputsError
from orchestrator.domain.validation_feedback import case_feedback, measured_test_version


def _table(c, name):
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()


def _schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS generated_recheck_claims (
        workflow_step_id TEXT NOT NULL REFERENCES workflow_steps(workflow_step_id),
        prior_receipt_id TEXT NOT NULL REFERENCES unit_test_execution_records(execution_manifest_id),
        source_artifact_id TEXT NOT NULL REFERENCES project_artifacts(artifact_id),
        role TEXT NOT NULL CHECK(role IN ('QA','SECURITY')), selector TEXT NOT NULL,
        PRIMARY KEY(workflow_step_id,prior_receipt_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS generated_recheck_results (
        workflow_step_id TEXT NOT NULL, prior_receipt_id TEXT NOT NULL,
        execution_manifest_id TEXT UNIQUE REFERENCES unit_test_execution_records(execution_manifest_id),
        PRIMARY KEY(workflow_step_id,prior_receipt_id),
        FOREIGN KEY(workflow_step_id,prior_receipt_id) REFERENCES generated_recheck_claims)""")
    for table in ('generated_recheck_claims', 'generated_recheck_results'):
        for operation in ('UPDATE', 'DELETE'):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation} BEFORE {operation} ON {table}
                          BEGIN SELECT RAISE(ABORT,'immutable generated recheck'); END""")
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} WHERE workflow_step_id=NEW.workflow_step_id
                         AND prior_receipt_id=NEW.prior_receipt_id)
            BEGIN SELECT RAISE(ABORT,'generated recheck already recorded'); END""")


def store_failed_files(c, manifest_id, inputs):
    c.execute("""CREATE TABLE IF NOT EXISTS generated_failed_test_files (
        execution_manifest_id TEXT NOT NULL REFERENCES unit_test_execution_records(execution_manifest_id),
        path TEXT NOT NULL, content BLOB NOT NULL, PRIMARY KEY(execution_manifest_id,path))""")
    for operation in ('UPDATE', 'DELETE'):
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS generated_files_no_{operation} BEFORE {operation}
                      ON generated_failed_test_files BEGIN SELECT RAISE(ABORT,'immutable generated input'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS generated_files_no_replace BEFORE INSERT ON generated_failed_test_files
        WHEN EXISTS (SELECT 1 FROM generated_failed_test_files WHERE execution_manifest_id=NEW.execution_manifest_id
                     AND path=NEW.path) BEGIN SELECT RAISE(ABORT,'immutable generated input'); END""")
    # The immutable unit receipt authenticates the complete file list and every
    # byte (including the trusted runner); never accept mutable workspace bytes
    # or substitute a new test with the same name.
    c.executemany("INSERT INTO generated_failed_test_files VALUES(?,?,?)",
                  [(str(manifest_id), path, content) for path, content in inputs.files.items()])


def _files(c, receipt):
    key = (str(receipt.execution_manifest_id),)
    if _table(c, 'generated_failed_test_files'):
        rows = c.execute("SELECT path,content FROM generated_failed_test_files WHERE execution_manifest_id=?", key).fetchall()
        if rows:
            return dict(rows)
    # QA already archived the exact bound input bytes in older databases.
    if receipt.role.value == 'QA' and _table(c, 'qa_test_input_receipts'):
        return dict(c.execute("""SELECT f.path,f.content FROM qa_test_input_files f JOIN qa_test_input_receipts r
            USING(capture_id) WHERE r.execution_manifest_id=?""", key))
    return {}


def claim(repository, step_id, source, receipt):
    if source.run_id != receipt.run_id or source.previous_artifact_id != receipt.source_artifact_id or receipt.scope.kind != 'QA_TESTS':
        raise ValueError('GENERATED_RECHECK_EVIDENCE_INVALID')
    if receipt.inputs['runnerSha256'] != sha256(Path(__file__).with_name('unit_runner.py').read_bytes()).hexdigest():
        return False  # A different runner is not the prior test. Never run archived runner code.
    from agents.runtime.evaluation_policy_store import EvaluationPolicyStore
    from mcp_tools.runtime import MCPBinding
    binding = MCPBinding(role=receipt.role, agent_role=receipt.role, run_id=source.run_id, workspace_id=receipt.workspace_id)
    with repository._transaction() as c:
        EvaluationPolicyStore._context(c, binding, step_id, source.artifact_id)
        if not _files(c, receipt):
            return False
        _schema(c)
        key = str(step_id), str(receipt.execution_manifest_id)
        existing = c.execute("SELECT r.prior_receipt_id FROM generated_recheck_claims q LEFT JOIN generated_recheck_results r "
                             "USING(workflow_step_id,prior_receipt_id) WHERE q.workflow_step_id=? AND q.prior_receipt_id=?", key).fetchone()
        if existing is not None:
            if existing[0] is None:
                raise ValueError('GENERATED_RECHECK_INTERRUPTED')
            return False
        c.execute("INSERT INTO generated_recheck_claims VALUES(?,?,?,?,?)",
                  (*key, str(source.artifact_id), receipt.role.value, receipt.scope.name))
    return True


def replay_inputs(repository, context, scope, source, runner):
    """Resolve only a Host-admitted claim for this role, scope and snapshot."""
    from mcp_tools.tools.unit_store import UnitTestOutputStore
    with repository._connection() as c:
        c.execute('BEGIN')
        if not _table(c, 'generated_recheck_claims'):
            return None
        rows = c.execute("""SELECT u.* FROM generated_recheck_claims q
            JOIN unit_test_execution_records u ON u.execution_manifest_id=q.prior_receipt_id
            JOIN workflow_steps step ON step.workflow_step_id=q.workflow_step_id AND step.status='RUNNING'
            LEFT JOIN generated_recheck_results r ON r.workflow_step_id=q.workflow_step_id AND r.prior_receipt_id=q.prior_receipt_id
            WHERE q.source_artifact_id=? AND q.role=? AND q.selector=? AND r.prior_receipt_id IS NULL
            AND u.run_id=? AND u.workspace_id=?""",
            (str(source.artifact_id), context.binding.role.value, scope.name,
             str(context.binding.run_id), str(context.binding.workspace_id))).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise UnitTestInputsError("PERMISSION_DENIED")
        old = UnitTestOutputStore._decode(c, rows[0])
        if (old.role is not context.binding.role or old.scope != scope or scope.kind != 'QA_TESTS'
                or old.source_artifact_id != source.previous_artifact_id
                or old.inputs['runnerSha256'] != sha256(runner).hexdigest()):
            raise UnitTestInputsError("PERMISSION_DENIED")
        return UnitTestInputs(files=_files(c, old), inputs_sha256=old.inputs['inputsSha256'],
                              runner_sha256=old.inputs['runnerSha256'], test_files_sha256=old.inputs['testFilesSha256'])


def complete(repository, step_id, source, old, new):
    if new is not None and (
        new.run_id != source.run_id or new.workspace_id != old.workspace_id or new.role != old.role
        or new.source_artifact_id != source.artifact_id or new.execution_manifest != source.execution_manifest()
        or new.workflow_step_id != step_id or new.scope != old.scope or new.execution_profile != old.execution_profile
        or new.inputs != old.inputs):
        raise ValueError('GENERATED_RECHECK_EVIDENCE_INVALID')
    with repository._transaction() as c:
        c.execute("INSERT INTO generated_recheck_results VALUES(?,?,?)",
                  (str(step_id), str(old.execution_manifest_id), str(new.execution_manifest_id) if new else None))


def completed_count(repository, step):
    with repository._connection() as c:
        if not _table(c, 'generated_recheck_results'):
            return 0
        return c.execute("SELECT COUNT(*) FROM generated_recheck_results WHERE workflow_step_id=?", (str(step),)).fetchone()[0]


def case_outcome(previous, current, contract):
    """Case-level verdict. Losing a previously observed channel never establishes recovery."""
    from agents.runtime.security_test_scope import measured_hash_complies
    if current.outcome not in ('PASS', 'FAIL'):
        return 'UNVERIFIED'
    if current.outcome == 'FAIL':
        return 'FAIL'
    if previous.get('observations'):
        feedback = case_feedback(current)
        if previous.get('observationStatus') != 'COMPLETE' or feedback['observationStatus'] != 'COMPLETE':
            return 'UNVERIFIED'
        prior = {(row['caseId'], row['itemId'], row['kind']) for row in previous['observations']}
        latest = {(row['caseId'], row['itemId'], row['kind']) for row in feedback['observations']}
        if prior != latest:
            return 'UNVERIFIED'
    old_facts = previous.get('signupChecks') or {}
    facts = json.loads(current.signup_checks_json or '{}')
    if previous['role'] == 'QA':
        if facts.get('concurrentCanonicalSignup') is False:
            return 'FAIL'
        if 'concurrentCanonicalSignup' in old_facts and facts.get('concurrentCanonicalSignup') is not True:
            return 'UNVERIFIED'
    if previous['role'] == 'SECURITY':
        marks = dict(current.output_checks)
        if 'EXPOSED' in marks.values():
            return 'FAIL'
        if any(mark in ('EXPOSED','CHECKED') and marks.get(channel) != 'CHECKED'
               for channel,mark in (previous.get('outputChecks') or {}).items()):
            return 'UNVERIFIED'
        key = next((r['key'] for r in contract.get('requirements', ()) if r['requirementId'] == previous['requirementId']), None)
        if contract.get('scenarioKey') == 'SCN-001' and key == 'REQ-005':
            old_hash = old_facts.get('storedHashParameters')
            current_hash = facts.get('storedHashParameters')
            policy = contract.get('securityPolicy', {}).get('passwordHashPolicy')
            if current_hash and policy and not measured_hash_complies(current_hash, policy):
                return 'FAIL'
            if old_hash and (not current_hash or not policy or ('distinctSalts' in old_hash and 'distinctSalts' not in current_hash)):
                return 'UNVERIFIED'
        if key == 'REQ-003' and 'canonicalEmailUniqueKey' in old_facts and facts.get('canonicalEmailUniqueKey') is not True:
            return 'UNVERIFIED'
    return 'PASS'


def measured_row(c, issue, source, previous):
    """Reconcile immutable candidate and previous receipts for this exact prior failure."""
    from mcp_tools.tools.unit_store import UnitTestOutputStore
    if (not _table(c, 'generated_recheck_results') or source.run_id != issue.run_id
            or source.previous_artifact_id != issue.source_artifact_id or source.code_version != issue.code_version + 1
            or previous.get('role') != issue.reporter.value or previous.get('runId') != str(issue.run_id)
            or previous.get('codeVersion') != issue.code_version or previous.get('reportArtifactId') != str(issue.report_artifact_id)
            or previous.get('outcome') != 'FAIL' or previous.get('requirementId') not in {str(r) for r in issue.requirement_ids}
            or not previous.get('testId', '').startswith('run_unit_tests:')):
        return None
    parts = previous['testId'].split(':', 2)
    if len(parts) != 3:
        return None
    _, selector, test = parts
    rows = c.execute("""SELECT u.* FROM generated_recheck_claims q JOIN generated_recheck_results r
            USING(workflow_step_id,prior_receipt_id)
            JOIN unit_test_execution_records before ON before.execution_manifest_id=q.prior_receipt_id
            JOIN unit_test_execution_records u ON u.execution_manifest_id=r.execution_manifest_id
            WHERE q.source_artifact_id=? AND q.role=? AND q.selector=? AND before.source_artifact_id=?
              AND u.source_artifact_id=? AND u.run_id=? AND u.workflow_step_id=q.workflow_step_id""",
            (str(source.artifact_id), issue.reporter.value, selector, str(issue.source_artifact_id),
             str(source.artifact_id), str(issue.run_id))).fetchall()
    records = []
    for row in rows:
        record = UnitTestOutputStore._decode(c, row)
        if (record.scope.kind != 'QA_TESTS' or record.role != issue.reporter or record.scope.name != selector
                or record.execution_manifest != source.execution_manifest()
                or measured_test_version(record).get('testVersion') != previous.get('testVersion')):
            continue
        cases = [case for case in record.report.tests if case.test_id == test]
        if len(cases) == 1:
            records.append((record, cases[0]))
    if len(records) == 1:
        record, case = records[0]
        from orchestrator.domain.run_configuration import RunConfigurationArtifact
        value = c.execute("SELECT payload_json FROM run_configurations WHERE run_id=?", (str(issue.run_id),)).fetchone()
        if value is None:
            return None
        outcome = case_outcome(previous, case, RunConfigurationArtifact.model_validate_json(value[0]).scenario_contract)
        return {**case_feedback(case), **measured_test_version(record),
                'role': issue.reporter.value, 'requirementId': previous['requirementId'], 'testId': previous['testId'],
                'outcome': outcome, 'runId': str(source.run_id), 'codeVersion': source.code_version,
                'sourceArtifactId': str(source.artifact_id), 'reportArtifactId': None,
                'executionManifest': source.execution_manifest().model_dump(mode='json', by_alias=True),
                'recheckReceiptId': str(record.execution_manifest_id)}
    return None
