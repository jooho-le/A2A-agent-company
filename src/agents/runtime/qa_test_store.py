"""Private immutable copies of the exact QA inputs, not a project verdict.

Capture happens before a Tool call. A separate immutable receipt binding is
added only after the Host has verified the actual private execution receipt.
Orphan captures are deliberately retained after interruption or failure.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import json
import re
from types import MappingProxyType
from uuid import UUID, uuid4

from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.unit_config import MAX_UNIT_FILE_BYTES, MAX_UNIT_FILES, MAX_UNIT_TOTAL_BYTES, _test_path
from mcp_tools.tools.unit_inputs import UnitTestInputs, _files_hash, _validate_content
from mcp_tools.tools.browser_inputs import BrowserTestInputs
from mcp_tools.tools.browser_store import BrowserTestOutputStore
from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
from orchestrator.sandbox.materialization import _check_names
from orchestrator.workspaces.policy import workspace_uuid


class QATestInputStoreError(ValueError):
    code = "QA_TEST_INPUT_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _json(data):
    return json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _plain(value):
    """Normalize immutable private receipt containers without changing data."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True, kw_only=True)
class QATestInputRecord:
    capture_id: UUID
    run_id: UUID = field(repr=False)
    workspace_id: UUID = field(repr=False)
    workflow_step_id: UUID = field(repr=False)
    source_artifact_id: UUID = field(repr=False)
    tool_name: str
    selector: str = field(repr=False)
    files: Mapping = field(repr=False)
    inputs: Mapping = field(repr=False)


class QATestInputStore:
    """Construction does not create tables, read files, or execute tests."""

    def __init__(self, repository):
        if not isinstance(repository, SQLiteWorkflowRepository):
            raise QATestInputStoreError()
        self._repository = repository

    def __repr__(self):
        return "QATestInputStore()"

    @staticmethod
    def _schema(connection):
        for statement in (
            """CREATE TABLE IF NOT EXISTS qa_test_input_captures (
                capture_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                workflow_step_id TEXT NOT NULL REFERENCES workflow_steps(workflow_step_id),
                source_artifact_id TEXT NOT NULL REFERENCES artifact_contents(artifact_id),
                tool_name TEXT NOT NULL CHECK(tool_name IN ('run_unit_tests','run_browser_tests')),
                selector TEXT NOT NULL,
                metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
                metadata_sha256 TEXT NOT NULL CHECK(length(metadata_sha256)=64)
            )""",
            """CREATE TABLE IF NOT EXISTS qa_test_input_files (
                capture_id TEXT NOT NULL REFERENCES qa_test_input_captures(capture_id),
                path TEXT NOT NULL,
                content BLOB NOT NULL CHECK(typeof(content)='blob' AND length(content)<=1048576),
                content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
                PRIMARY KEY(capture_id,path)
            )""",
            """CREATE TABLE IF NOT EXISTS qa_test_input_receipts (
                capture_id TEXT PRIMARY KEY REFERENCES qa_test_input_captures(capture_id),
                execution_manifest_id TEXT NOT NULL UNIQUE
            )""",
        ):
            connection.execute(statement)
        for table in ("qa_test_input_captures", "qa_test_input_files", "qa_test_input_receipts"):
            for operation in ("UPDATE", "DELETE"):
                connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} "
                    f"BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable QA test input'); END")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS qa_test_input_captures_no_replace
            BEFORE INSERT ON qa_test_input_captures
            WHEN EXISTS(SELECT 1 FROM qa_test_input_captures WHERE capture_id=NEW.capture_id)
            BEGIN SELECT RAISE(ABORT,'immutable QA test input'); END""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS qa_test_input_files_no_replace
            BEFORE INSERT ON qa_test_input_files
            WHEN EXISTS(SELECT 1 FROM qa_test_input_files WHERE capture_id=NEW.capture_id AND path=NEW.path)
            BEGIN SELECT RAISE(ABORT,'immutable QA test input'); END""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS qa_test_input_receipts_no_replace
            BEFORE INSERT ON qa_test_input_receipts
            WHEN EXISTS(SELECT 1 FROM qa_test_input_receipts WHERE capture_id=NEW.capture_id
                OR execution_manifest_id=NEW.execution_manifest_id)
            BEGIN SELECT RAISE(ABORT,'immutable QA test input'); END""")

    @staticmethod
    def _context(connection, binding, step_id, source_id):
        if (type(binding) is not MCPBinding or binding.role is not AgentRole.QA
                or binding.agent_role is not AgentRole.QA):
            raise ValueError
        run_row = connection.execute("SELECT payload_json,status FROM workflow_runs WHERE run_id=?",
                                     (str(binding.run_id),)).fetchone()
        step_row = connection.execute("SELECT payload_json,status FROM workflow_steps WHERE workflow_step_id=?",
                                      (str(step_id),)).fetchone()
        if run_row is None or step_row is None:
            raise ValueError
        run, step = WorkflowRun.model_validate_json(run_row[0]), WorkflowStep.model_validate_json(step_row[0])
        if (run.run_id != binding.run_id or run.workspace_id != binding.workspace_id
                or run.status is not WorkflowStatus.VALIDATING or run.fix_attempt != 0
                or run_row[1] != run.status.value
                or step.run_id != run.run_id or step.workflow_step_id != step_id
                or step.agent_role is not AgentRole.QA or step.status is not WorkflowStepStatus.RUNNING
                or step_row[1] != step.status.value or step.code_version != 1
                or source_id not in step.input_artifact_ids):
            raise ValueError
        active = []
        for row in connection.execute("SELECT payload_json,status FROM workflow_steps WHERE run_id=?", (str(run.run_id),)):
            candidate = WorkflowStep.model_validate_json(row[0])
            if row[1] != candidate.status.value or candidate.run_id != run.run_id:
                raise ValueError
            if candidate.agent_role is AgentRole.QA and candidate.status is WorkflowStepStatus.RUNNING:
                active.append(candidate.workflow_step_id)
        if active != [step_id]:
            raise ValueError
        if connection.execute("SELECT 1 FROM workspaces WHERE workspace_id=? AND run_id=?",
                              (str(binding.workspace_id), str(binding.run_id))).fetchone() is None:
            raise ValueError
        if connection.execute("SELECT 1 FROM artifact_contents WHERE artifact_id=? AND run_id=? AND artifact_type='SOURCE'",
                              (str(source_id), str(binding.run_id))).fetchone() is None:
            raise ValueError
        if connection.execute("SELECT 1 FROM snapshot_read_grants WHERE artifact_id=? AND role='QA' AND access='READ_ONLY'",
                              (str(source_id),)).fetchone() is None:
            raise ValueError

    @staticmethod
    def _files(files, inputs, tool):
        if not isinstance(files, Mapping) or not isinstance(inputs, Mapping):
            raise ValueError
        files = dict(files)
        required = {"_unit_runner.py"} if tool == "run_unit_tests" else {
            "_browser_runner.py", "_browser_contract.py", "_browser_host.json"}
        if not required <= set(files) or not 1 <= len(files) <= MAX_UNIT_FILES + 3:
            raise ValueError
        total = 0
        for path, content in files.items():
            if path not in required:
                _test_path(path)
            if type(content) is not bytes or len(content) > MAX_UNIT_FILE_BYTES:
                raise ValueError
            _validate_content(content)
            total += len(content)
        if total > MAX_UNIT_TOTAL_BYTES:
            raise ValueError
        _check_names(list(files))
        expected = [{"path": path, "sha256": sha256(content).hexdigest(), "sizeBytes": len(content)}
                    for path, content in sorted(files.items())]
        if (inputs.get("files") != expected or inputs.get("inputsSha256") != _files_hash(files)):
            raise ValueError
        return dict(sorted(files.items()))

    def stage(self, binding, *, workflow_step_id, source_artifact_id, tool_name, selector, captured, inputs):
        try:
            step_id, source_id = workspace_uuid(workflow_step_id), workspace_uuid(source_artifact_id)
            if (tool_name not in {"run_unit_tests", "run_browser_tests"}
                    or type(selector) is not str or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", selector) is None
                    or tool_name == "run_unit_tests" and type(captured) is not UnitTestInputs
                    or tool_name == "run_browser_tests" and type(captured) is not BrowserTestInputs):
                raise ValueError
            files = self._files(captured.files, inputs, tool_name)
            metadata = dict(inputs)
            metadata_json = _json(metadata)
            if len(metadata_json.encode("utf-8")) > 256 * 1024:
                raise ValueError
            capture_id = uuid4()
            with self._repository._transaction() as connection:
                self._schema(connection)
                self._context(connection, binding, step_id, source_id)
                connection.execute("INSERT INTO qa_test_input_captures VALUES(?,?,?,?,?,?,?,?,?)", (
                    str(capture_id), str(binding.run_id), str(binding.workspace_id), str(step_id),
                    str(source_id), tool_name, selector, metadata_json, sha256(metadata_json.encode()).hexdigest()))
                connection.executemany("INSERT INTO qa_test_input_files VALUES(?,?,?,?)", (
                    (str(capture_id), path, content, sha256(content).hexdigest()) for path, content in files.items()))
            return self.get(binding, capture_id)
        except Exception:
            raise QATestInputStoreError() from None

    def get(self, binding, capture_id):
        try:
            capture_id = workspace_uuid(capture_id)
            if type(binding) is not MCPBinding or binding.role is not AgentRole.QA:
                raise ValueError
            with self._repository._connection() as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                row = connection.execute("SELECT * FROM qa_test_input_captures WHERE capture_id=? AND run_id=? AND workspace_id=?",
                    (str(capture_id), str(binding.run_id), str(binding.workspace_id))).fetchone()
                if row is None:
                    raise ValueError
                entries = connection.execute("SELECT * FROM qa_test_input_files WHERE capture_id=? ORDER BY path LIMIT 68",
                                             (str(capture_id),)).fetchall()
                if sha256(row["metadata_json"].encode()).hexdigest() != row["metadata_sha256"]:
                    raise ValueError
                inputs = json.loads(row["metadata_json"])
                if _json(inputs) != row["metadata_json"]:
                    raise ValueError
                files = {}
                for entry in entries:
                    content = entry["content"]
                    if type(content) is not bytes or sha256(content).hexdigest() != entry["content_sha256"]:
                        raise ValueError
                    files[entry["path"]] = content
                self._files(files, inputs, row["tool_name"])
                return QATestInputRecord(capture_id=capture_id, run_id=workspace_uuid(row["run_id"]),
                    workspace_id=workspace_uuid(row["workspace_id"]), workflow_step_id=workspace_uuid(row["workflow_step_id"]),
                    source_artifact_id=workspace_uuid(row["source_artifact_id"]), tool_name=row["tool_name"],
                    selector=row["selector"], files=MappingProxyType(files), inputs=MappingProxyType(inputs))
        except Exception:
            raise QATestInputStoreError() from None

    def bind_receipt(self, binding, capture_id, receipt):
        try:
            capture = self.get(binding, capture_id)
            if (receipt.run_id != capture.run_id or receipt.workspace_id != capture.workspace_id
                    or receipt.workflow_step_id != capture.workflow_step_id
                    or receipt.source_artifact_id != capture.source_artifact_id
                    or receipt.tool_name != capture.tool_name or receipt.profile_name != capture.selector
                    or _json(_plain(receipt.inputs)) != _json(_plain(capture.inputs))):
                raise ValueError
            manifest_id = workspace_uuid(receipt.execution_manifest_id)
            store = UnitTestOutputStore(self._repository) if capture.tool_name == "run_unit_tests" else BrowserTestOutputStore(self._repository)
            if store.get(binding.run_id, manifest_id) != receipt:
                raise ValueError
            with self._repository._transaction() as connection:
                self._context(connection, binding, capture.workflow_step_id, capture.source_artifact_id)
                table = {"run_unit_tests": "unit_test_execution_records",
                         "run_browser_tests": "browser_test_execution_records"}[capture.tool_name]
                if connection.execute(f"SELECT 1 FROM {table} WHERE execution_manifest_id=? AND run_id=? AND workflow_step_id=?",
                    (str(manifest_id), str(capture.run_id), str(capture.workflow_step_id))).fetchone() is None:
                    raise ValueError
                connection.execute("INSERT INTO qa_test_input_receipts VALUES(?,?)", (str(capture.capture_id), str(manifest_id)))
            return capture
        except Exception:
            raise QATestInputStoreError() from None
