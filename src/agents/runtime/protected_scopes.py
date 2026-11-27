"""Host-only, once-per-validation-step admissions for frozen protected tests.

The journal points to the regular tool and sandbox receipts; it contains no
model claims or substitute test outcomes. An interrupted claim cannot execute
the same protected scope a second time. A completed claim is reusable.
"""

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from agents.runtime.evaluation_policy_store import EvaluationPolicyStore
from mcp_tools.execution_store import ToolExecutionStore
from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.browser_store import BrowserTestOutputStore
from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.domain.tool_evidence import ToolExecutionOutcome


@dataclass(frozen=True)
class ProtectedScopeReceipt:
    tool_name: str
    selector: str
    logical_call_id: UUID
    execution_manifest_id: UUID
    policy_sha256: str


def _schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS protected_scope_claims (
        workflow_step_id TEXT NOT NULL REFERENCES workflow_steps(workflow_step_id),
        tool_name TEXT NOT NULL CHECK(tool_name IN ('run_unit_tests','run_browser_tests')),
        selector TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
        source_artifact_id TEXT NOT NULL REFERENCES project_artifacts(artifact_id), policy_sha256 TEXT NOT NULL,
        PRIMARY KEY(workflow_step_id,tool_name,selector))""")
    c.execute("""CREATE TABLE IF NOT EXISTS protected_scope_executions (
        workflow_step_id TEXT NOT NULL, tool_name TEXT NOT NULL, selector TEXT NOT NULL,
        logical_call_id TEXT NOT NULL UNIQUE REFERENCES tool_execution_calls(logical_call_id),
        execution_manifest_id TEXT NOT NULL UNIQUE, PRIMARY KEY(workflow_step_id,tool_name,selector),
        FOREIGN KEY(workflow_step_id,tool_name,selector)
        REFERENCES protected_scope_claims(workflow_step_id,tool_name,selector))""")
    for table in ('protected_scope_claims', 'protected_scope_executions'):
        for operation in ('UPDATE', 'DELETE'):
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_{operation} BEFORE {operation} ON {table}
                          BEGIN SELECT RAISE(ABORT,'immutable protected scope'); END""")
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table}
                  WHEN EXISTS(SELECT 1 FROM {table} WHERE workflow_step_id=NEW.workflow_step_id
                  AND tool_name=NEW.tool_name AND selector=NEW.selector)
                  BEGIN SELECT RAISE(ABORT,'protected scope already claimed'); END""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS protected_scope_no_rebound_receipt
        BEFORE INSERT ON protected_scope_executions WHEN EXISTS(
            SELECT 1 FROM protected_scope_executions WHERE logical_call_id=NEW.logical_call_id
            OR execution_manifest_id=NEW.execution_manifest_id)
        BEGIN SELECT RAISE(ABORT,'protected receipt already bound'); END""")


def _finished(c, run_id, workflow_step_id, source_artifact_id):
    rows = c.execute("""SELECT c.tool_name,c.selector,r.logical_call_id,r.execution_manifest_id,c.policy_sha256
        FROM protected_scope_claims c JOIN protected_scope_executions r USING(workflow_step_id,tool_name,selector)
        WHERE c.run_id=? AND c.workflow_step_id=? AND c.source_artifact_id=? ORDER BY c.tool_name,c.selector""",
        (str(run_id), str(workflow_step_id), str(source_artifact_id)))
    return tuple(ProtectedScopeReceipt(row[0], row[1], UUID(row[2]), UUID(row[3]), row[4]) for row in rows)


def completed_protected_scopes(repository, *, run_id, workflow_step_id, source_artifact_id):
    """Look up completed admissions, including on archived databases predating the journal."""
    with repository._connection() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='protected_scope_executions'").fetchone():
            return ()
        return _finished(c, run_id, workflow_step_id, source_artifact_id)


def claim_protected_scope(repository, binding, step, source_id, tool, selector):
    """Return None for the owner of a new admission or a previously finished receipt."""
    with repository._transaction() as c:
        _schema(c)
        EvaluationPolicyStore._context(c, binding, step, source_id)
        policy = c.execute("SELECT policy_sha256 FROM run_evaluation_policies WHERE run_id=? AND role=?",
                           (str(binding.run_id), binding.role.value)).fetchone()
        if not policy:
            raise ValueError("PROTECTED_SCOPE_POLICY_MISSING")
        for row in _finished(c, binding.run_id, step, source_id):
            if (tool, selector) == (row.tool_name, row.selector):
                if row.policy_sha256 != policy[0]:
                    raise ValueError("PROTECTED_SCOPE_POLICY_CHANGED")
                return row
        c.execute("INSERT INTO protected_scope_claims VALUES(?,?,?,?,?,?)",
                  (str(step), tool, selector, str(binding.run_id), str(source_id), policy[0]))
    return None


def commit_protected_scope(repository, step, selector, record, receipt):
    kind = receipt.scope.kind if receipt.tool_name == "run_unit_tests" else receipt.suite["kind"]
    if (kind != "PROTECTED" or record.workflow_step_id != step or receipt.workflow_step_id != step
            or receipt.source_artifact_id != record.source_artifact_id or receipt.run_id != record.run_id
            or receipt.workspace_id != record.workspace_id or receipt.role is not record.role
            or receipt.execution_manifest != record.execution_manifest
            or record.to_tool_evidence().outcome is not ToolExecutionOutcome.PASS
            or record.attempts[-1].execution_manifest_id != receipt.execution_manifest_id
            or record.selector_sha256 != sha256(selector.encode()).hexdigest()
            or receipt.profile_name != selector or record.tool_name != receipt.tool_name):
        raise ValueError("PROTECTED_SCOPE_RECEIPT_INVALID")
    binding = MCPBinding(role=record.role, agent_role=record.role, run_id=record.run_id, workspace_id=record.workspace_id)
    with repository._transaction() as c:
        EvaluationPolicyStore._context(c, binding, step, record.source_artifact_id)
        c.execute("INSERT INTO protected_scope_executions VALUES(?,?,?,?,?)",
                  (str(step), record.tool_name, selector, str(record.logical_call_id), str(receipt.execution_manifest_id)))


def load_protected_receipt(repository, binding, execution, saved):
    """Reconcile the immutable pair again; the lookup key by itself authorizes no evidence."""
    record = ToolExecutionStore(repository).get(binding, saved.logical_call_id)
    store = UnitTestOutputStore if saved.tool_name == 'run_unit_tests' else BrowserTestOutputStore
    receipt = store(repository).get(binding.run_id, saved.execution_manifest_id)
    evidence = record.to_tool_evidence()
    if (record.attempts[-1].execution_manifest_id != saved.execution_manifest_id
            or evidence.outcome is not ToolExecutionOutcome.PASS
            or record.execution_manifest != execution.source.execution_manifest()
            or receipt.execution_manifest != record.execution_manifest
            or record.role is not binding.role or receipt.role is not binding.role
            or record.source_artifact_id != execution.source.artifact_id
            or receipt.source_artifact_id != execution.source.artifact_id
            or record.workflow_step_id != execution.metadata.workflow_step_id
            or receipt.workflow_step_id != record.workflow_step_id
            or record.tool_name != saved.tool_name or receipt.tool_name != saved.tool_name
            or record.selector_sha256 != sha256(saved.selector.encode()).hexdigest()
            or receipt.profile_name != saved.selector):
        raise ValueError("PROTECTED_SCOPE_RECEIPT_INVALID")
    return receipt, evidence
