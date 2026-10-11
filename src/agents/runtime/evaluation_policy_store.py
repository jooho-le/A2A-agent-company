"""Run-scoped, immutable fingerprints of trusted evaluation policy.

Only hashes are persisted: protected test source, Host commands and paths are
not copied into this ledger. Construction performs no database or file I/O.
The first verified evaluation admission pins policy before a Tool can start.
"""

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
from uuid import UUID

from agents.runtime.qa_context import validation_cycle
from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.browser_config import encode_browser_configuration
from mcp_tools.tools.security_config import encode_security_configuration
from mcp_tools.tools.unit_config import encode_unit_configuration
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.states import AgentRole, WorkflowStepStatus
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
from orchestrator.workspaces.policy import workspace_uuid


class EvaluationPolicyError(ValueError):
    code = "EVALUATION_POLICY_INVALID"

    def __init__(self):
        super().__init__(self.code)


@dataclass(frozen=True, kw_only=True)
class EvaluationPolicyRecord:
    run_id: UUID = field(repr=False)
    workspace_id: UUID = field(repr=False)
    role: AgentRole
    policy_sha256: str


def _digest(payload):
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    if len(raw) > 256 * 1024:
        raise EvaluationPolicyError()
    return sha256(raw).hexdigest()


def _policy(encoded, selector_key):
    payload = json.loads(encoded)
    # The daemon endpoint is transport, not an evaluation rule. Do not make
    # moving a Run to another approved daemon change the protected criteria.
    payload.pop("docker_endpoint", None)
    if selector_key is not None:
        for selector in payload[selector_key]:
            files = selector.get("protected_files")
            if files is not None:
                selector["protected_files"] = [
                    {"path": path, "sha256": sha256(text.encode("utf-8")).hexdigest(),
                     "sizeBytes": len(text.encode("utf-8"))}
                    for path, text in sorted(files.items())
                ]
        payload[selector_key].sort(key=lambda item: item["name"])
    else:
        payload["profiles"].sort(key=lambda item: item["name"])
    return payload


def _runner_hashes(names):
    # These are trusted platform assets, never generated project files. This
    # read is reached only during explicit prepare/capture, not construction.
    root = Path(__file__).resolve().parents[2] / "mcp_tools" / "tools"
    result = {}
    for name in names:
        content = (root / name).read_bytes()
        if not 1 <= len(content) <= 1024 * 1024:
            raise EvaluationPolicyError()
        result[name] = sha256(content).hexdigest()
    return result


def qa_policy_sha256(configuration, protected_cases):
    """Hash actual protected bytes, approved bindings, selectors and runners.

    Generated QA test contents and candidate identity deliberately do not
    belong here: agents may improve those tests while fixing product Source.
    """
    unit, browser = configuration.unit_test_configuration, configuration.browser_test_configuration
    runners = []
    if unit is not None:
        runners.append("unit_runner.py")
    if browser is not None:
        runners.extend(("browser_runner.py", "browser_contract.py"))
    cases = [{"toolName": case.tool_name, "selector": case.selector, "testId": case.test_id,
              "requirementId": str(case.requirement_id), "title": case.title,
              "expectedResult": case.expected_result} for case in protected_cases]
    cases.sort(key=lambda item: (item["toolName"], item["selector"], item["testId"]))
    return _digest({"version": 1,
        "unit": None if unit is None else _policy(encode_unit_configuration(unit), "scopes"),
        "browser": None if browser is None else _policy(encode_browser_configuration(browser), "suites"),
        "protectedCases": cases, "runners": _runner_hashes(runners),
        "maxCallSeconds": configuration.max_call_seconds})


def security_policy_sha256(configuration):
    """Hash scanner version/rules/profile plus the approved execution policy."""
    return _digest({"version": 1,
        "scanner": _policy(encode_security_configuration(configuration.security_scan_configuration), None),
        "runners": _runner_hashes(("security_runner.py", "security_contract.py")),
        "maxCallSeconds": configuration.max_call_seconds})


class EvaluationPolicyStore:
    def __init__(self, repository):
        if not isinstance(repository, SQLiteWorkflowRepository):
            raise EvaluationPolicyError()
        self._repository = repository

    def __repr__(self):
        return "EvaluationPolicyStore()"

    def list_for_run(self, run_id):
        """Read typed comparison evidence without creating tables or policy.

        An existing Run which has not started evaluation returns an empty
        tuple. Unknown Runs and inconsistent persisted identities fail closed.
        This remains available after completion for independent evaluation.
        """
        try:
            run_id = workspace_uuid(run_id)
            with self._repository._connection() as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                row = connection.execute("SELECT payload_json FROM workflow_runs WHERE run_id=?",
                                         (str(run_id),)).fetchone()
                if row is None:
                    raise ValueError
                run = WorkflowRun.model_validate_json(row[0])
                if run.run_id != run_id:
                    raise ValueError
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_evaluation_policies'").fetchone() is None:
                    return ()
                rows = connection.execute("SELECT run_id,workspace_id,role,policy_sha256 FROM run_evaluation_policies "
                    "WHERE run_id=? ORDER BY role LIMIT 3", (str(run_id),)).fetchall()
                if len(rows) > 2:
                    raise ValueError
                records = []
                for row in rows:
                    role = AgentRole(row[2])
                    workspace_id = workspace_uuid(row[1])
                    if (workspace_uuid(row[0]) != run_id or workspace_id != run.workspace_id
                            or role not in {AgentRole.QA, AgentRole.SECURITY}
                            or type(row[3]) is not str or re.fullmatch(r"[0-9a-f]{64}", row[3]) is None
                            or connection.execute("SELECT 1 FROM workspaces WHERE workspace_id=? AND run_id=?",
                                (str(workspace_id), str(run_id))).fetchone() is None):
                        raise ValueError
                    records.append(EvaluationPolicyRecord(run_id=run_id, workspace_id=workspace_id,
                        role=role, policy_sha256=row[3]))
                return tuple(records)
        except Exception:
            raise EvaluationPolicyError() from None

    @staticmethod
    def _schema(connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS run_evaluation_policies (
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            role TEXT NOT NULL CHECK(role IN ('QA','SECURITY')),
            policy_sha256 TEXT NOT NULL CHECK(length(policy_sha256)=64),
            PRIMARY KEY(run_id,role)
        )""")
        for operation in ("UPDATE", "DELETE"):
            connection.execute(f"CREATE TRIGGER IF NOT EXISTS run_evaluation_policies_no_{operation.lower()} "
                f"BEFORE {operation} ON run_evaluation_policies "
                "BEGIN SELECT RAISE(ABORT,'immutable evaluation policy'); END")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS run_evaluation_policies_no_replace
            BEFORE INSERT ON run_evaluation_policies
            WHEN EXISTS(SELECT 1 FROM run_evaluation_policies WHERE run_id=NEW.run_id AND role=NEW.role)
            BEGIN SELECT RAISE(ABORT,'immutable evaluation policy'); END""")

    @staticmethod
    def _context(connection, binding, step_id, source_id):
        run_row = connection.execute("SELECT payload_json,status FROM workflow_runs WHERE run_id=?",
                                     (str(binding.run_id),)).fetchone()
        step_row = connection.execute("SELECT payload_json,status FROM workflow_steps WHERE workflow_step_id=?",
                                      (str(step_id),)).fetchone()
        if run_row is None or step_row is None:
            raise ValueError
        run, step = WorkflowRun.model_validate_json(run_row[0]), WorkflowStep.model_validate_json(step_row[0])
        validation_cycle(run)
        if (run.run_id != binding.run_id or run.workspace_id != binding.workspace_id
                or run_row[1] != run.status.value or step.workflow_step_id != step_id
                or step.run_id != run.run_id or step.agent_role is not binding.role
                or step.status is not WorkflowStepStatus.RUNNING or step_row[1] != step.status.value
                or step.code_version != run.code_version or step.input_artifact_ids != [source_id]
                or connection.execute("SELECT 1 FROM workspaces WHERE workspace_id=? AND run_id=?",
                    (str(binding.workspace_id), str(binding.run_id))).fetchone() is None):
            raise ValueError
        active = []
        for row in connection.execute("SELECT payload_json,status FROM workflow_steps WHERE run_id=?", (str(run.run_id),)):
            candidate = WorkflowStep.model_validate_json(row[0])
            if candidate.run_id != run.run_id or row[1] != candidate.status.value:
                raise ValueError
            if candidate.agent_role is binding.role and candidate.status is WorkflowStepStatus.RUNNING:
                active.append(candidate.workflow_step_id)
        if active != [step_id]:
            raise ValueError

    @staticmethod
    def _legacy_evidence_exists(connection, binding):
        # Never declare today's policy to be an old Run's baseline when its
        # earlier evaluation was performed before this ledger was present.
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = ("unit_test_execution_records", "browser_test_execution_records", "qa_test_input_captures") \
            if binding.role is AgentRole.QA else ("security_scan_execution_records",)
        for table in tables:
            if table not in names:
                continue
            if table == "qa_test_input_captures":
                query = f"SELECT 1 FROM {table} WHERE run_id=? LIMIT 1"
                values = (str(binding.run_id),)
            else:
                # Developer may already have run unit tests. That receipt
                # does not establish (or prevent) the QA evaluation baseline.
                query = f"SELECT 1 FROM {table} WHERE run_id=? AND json_extract(metadata_json,'$.role')=? LIMIT 1"
                values = (str(binding.run_id), binding.role.value)
            if connection.execute(query, values).fetchone() is not None:
                return True
        artifact_type = "QA_REPORT" if binding.role is AgentRole.QA else "SECURITY_REPORT"
        return connection.execute("SELECT 1 FROM project_artifacts WHERE run_id=? AND artifact_type=? LIMIT 1",
                                  (str(binding.run_id), artifact_type)).fetchone() is not None

    def pin(self, binding, *, workflow_step_id, source_artifact_id, policy_sha256):
        try:
            if (type(binding) is not MCPBinding or binding.role not in {AgentRole.QA, AgentRole.SECURITY}
                    or binding.agent_role is not binding.role or type(policy_sha256) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", policy_sha256) is None):
                raise ValueError
            step_id, source_id = workspace_uuid(workflow_step_id), workspace_uuid(source_artifact_id)
            with self._repository._transaction() as connection:
                self._schema(connection)
                self._context(connection, binding, step_id, source_id)
                row = connection.execute("SELECT workspace_id,policy_sha256 FROM run_evaluation_policies "
                    "WHERE run_id=? AND role=?", (str(binding.run_id), binding.role.value)).fetchone()
                if row is not None:
                    if row[0] != str(binding.workspace_id) or row[1] != policy_sha256:
                        raise ValueError
                else:
                    if self._legacy_evidence_exists(connection, binding):
                        raise ValueError
                    connection.execute("INSERT INTO run_evaluation_policies VALUES(?,?,?,?)",
                        (str(binding.run_id), str(binding.workspace_id), binding.role.value, policy_sha256))
        except Exception:
            raise EvaluationPolicyError() from None
