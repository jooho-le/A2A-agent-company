"""Immutable private scanner receipts, never confirmed vulnerabilities/verdicts.

Publication and historical reads verify actual frozen Source bytes, all-Python
inventory, Security grants and the Run's frozen scanner reference. Report and
Tool output contain only locations, rule identities, ranks and SUSPECTED status.
Raw Bandit output, source snippets and scanner stderr are never retained.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
import io
import json
import re
import sqlite3
import tokenize
from types import MappingProxyType
from uuid import UUID, uuid4

from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.build_config import _copy_profile as _copy_execution_profile
from mcp_tools.tools.security_config import (
    SecurityScanConfiguration, SecurityScannerProfile, _copy_configuration, _copy_profile,
    security_host_payload,
)
from mcp_tools.tools.security_contract import canonical_json, parse_json, validate_security_host_payload
from mcp_tools.tools.security_inputs import SecurityScanInputs
from mcp_tools.tools.security_report import SecurityScanReport, parse_security_report
from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.artifacts.sqlite_store import SQLiteArtifactContentStore
from orchestrator.core.security import redact_text
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact, ExecutionManifest
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxLimits, SandboxResult
from orchestrator.sandbox.materialization import _archive_entries
from orchestrator.workspaces.policy import WorkspaceAccessError, workspace_uuid


MAX_SECURITY_OUTPUT_BYTES = 4 * 1024 * 1024
_MAX_METADATA_BYTES = 512 * 1024
_MAX_REPORT_BYTES = 1024 * 1024
_REQUIRED = frozenset({"_security_runner.py", "_security_contract.py", "_security_host.json"})
_CODES = frozenset({"SECURITY_SCAN_" + suffix for suffix in (
    "STORAGE_ERROR", "RECORD_CONFLICT", "RESULT_INVALID", "RESULT_INTEGRITY_ERROR", "CONTEXT_DENIED", "OUTPUT_LIMIT", "RECORD_NOT_FOUND",
)})
_METADATA_KEYS = frozenset({
    "executionManifestId", "executionId", "runId", "workspaceId", "workflowStepId", "sourceArtifactId",
    "executionManifest", "role", "profileName", "toolName", "imageId", "containerId", "exitCode", "durationMs",
    "stdoutSha256", "stderrSha256", "stdoutSizeBytes", "stderrSizeBytes", "executionProfile", "scannerProfile", "inputs",
    "hostConfiguration", "hostPolicySha256", "sourceFiles", "sourceFilesSha256", "reportSha256", "reportSizeBytes",
})


class SecurityStoreError(RuntimeError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in _CODES else "SECURITY_SCAN_STORAGE_ERROR"
        super().__init__(self.code)


def _uuid(value):
    try:
        return workspace_uuid(value)
    except WorkspaceAccessError:
        raise SecurityStoreError("SECURITY_SCAN_RESULT_INVALID") from None


def _json(value):
    return canonical_json(value)


def _freeze(value):
    if type(value) is dict:
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if type(value) is list:
        return tuple(_freeze(child) for child in value)
    return value


@dataclass(frozen=True, kw_only=True)
class SecurityScanExecutionRecord:
    execution_manifest_id: UUID
    execution_id: UUID
    run_id: UUID = field(repr=False)
    workspace_id: UUID = field(repr=False)
    workflow_step_id: UUID = field(repr=False)
    source_artifact_id: UUID = field(repr=False)
    execution_manifest: ExecutionManifest = field(repr=False)
    execution_profile: ExecutionProfile = field(repr=False)
    scanner_profile: SecurityScannerProfile = field(repr=False)
    inputs: Mapping = field(repr=False)
    host_configuration: Mapping = field(repr=False)
    source_files: tuple = field(repr=False)
    report: SecurityScanReport = field(repr=False)
    role: AgentRole
    profile_name: str
    image_id: str
    container_id: str
    exit_code: int
    duration_ms: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)
    stdout_sha256: str
    stderr_sha256: str
    report_sha256: str
    metadata_sha256: str

    @property
    def tool_name(self):
        return "run_security_scan"

    @property
    def report_ref(self):
        return f"artifact://{self.execution_manifest_id}/security-scan-report.json"

    def tool_output(self):
        return {"findings": [finding.to_dict() for finding in self.report.findings],
                "reportRef": self.report_ref, "executionManifestId": str(self.execution_manifest_id)}


class SecurityScanOutputStore:
    def __init__(self, repository):
        if not isinstance(repository, SQLiteWorkflowRepository):
            raise SecurityStoreError("SECURITY_SCAN_RESULT_INVALID")
        self._repository = repository

    def __repr__(self):
        return "SecurityScanOutputStore()"

    @staticmethod
    def _ensure_schema(connection):
        for statement in (
            """CREATE TABLE IF NOT EXISTS security_scan_execution_records (
                execution_manifest_id TEXT PRIMARY KEY,execution_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                workflow_step_id TEXT NOT NULL REFERENCES workflow_steps(workflow_step_id),
                source_artifact_id TEXT NOT NULL REFERENCES artifact_contents(artifact_id),
                metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),metadata_sha256 TEXT NOT NULL CHECK(length(metadata_sha256)=64),
                report BLOB NOT NULL CHECK(typeof(report)='blob' AND length(report)<=1048576),
                stdout BLOB NOT NULL CHECK(typeof(stdout)='blob' AND length(stdout)<=4194304),
                stderr BLOB NOT NULL CHECK(typeof(stderr)='blob' AND length(stderr)=0),
                CHECK(execution_manifest_id != execution_id))""",
            "CREATE INDEX IF NOT EXISTS security_scan_records_by_run ON security_scan_execution_records(run_id,execution_manifest_id)",
            """CREATE TRIGGER IF NOT EXISTS security_scan_records_no_update BEFORE UPDATE ON security_scan_execution_records
                BEGIN SELECT RAISE(ABORT,'immutable Security Scan execution'); END""",
            """CREATE TRIGGER IF NOT EXISTS security_scan_records_no_delete BEFORE DELETE ON security_scan_execution_records
                BEGIN SELECT RAISE(ABORT,'immutable Security Scan execution'); END""",
            """CREATE TRIGGER IF NOT EXISTS security_scan_records_no_replace BEFORE INSERT ON security_scan_execution_records
                WHEN EXISTS(SELECT 1 FROM security_scan_execution_records WHERE execution_manifest_id IN(NEW.execution_manifest_id,NEW.execution_id)
                    OR execution_id IN(NEW.execution_manifest_id,NEW.execution_id))
                BEGIN SELECT RAISE(ABORT,'immutable Security Scan execution'); END""",
            """CREATE TRIGGER IF NOT EXISTS security_scan_records_ownership BEFORE INSERT ON security_scan_execution_records
                WHEN NOT EXISTS(SELECT 1 FROM workspaces WHERE workspace_id=NEW.workspace_id AND run_id=NEW.run_id)
                    OR NOT EXISTS(SELECT 1 FROM workflow_steps WHERE workflow_step_id=NEW.workflow_step_id AND run_id=NEW.run_id)
                    OR NOT EXISTS(SELECT 1 FROM artifact_contents WHERE artifact_id=NEW.source_artifact_id
                        AND run_id=NEW.run_id AND artifact_type='SOURCE')
                BEGIN SELECT RAISE(ABORT,'Security Scan ownership mismatch'); END""",
        ):
            connection.execute(statement)

    @staticmethod
    def _binding(binding):
        if (not isinstance(binding, MCPBinding) or binding.role is not AgentRole.SECURITY or binding.agent_role is not AgentRole.SECURITY):
            raise SecurityStoreError("SECURITY_SCAN_CONTEXT_DENIED")

    @staticmethod
    def _source(connection, binding, source):
        try:
            stored = UnitTestOutputStore._source(connection, binding, source)
            if connection.execute("SELECT 1 FROM snapshot_read_grants WHERE artifact_id=? AND role='SECURITY' AND access='READ_ONLY'",
                                  (str(source.artifact_id),)).fetchone() is None:
                raise ValueError
            return stored
        except Exception:
            raise SecurityStoreError("SECURITY_SCAN_RESULT_INTEGRITY_ERROR") from None

    @staticmethod
    def source_inventory(content):
        """Pure canonical tar inspection, never extraction, AST evaluation or import."""
        try:
            if type(content) is not bytes or len(content) > 20 * 1024 * 1024:
                raise ValueError
            files = {name: value for name, value, _mode in _archive_entries(content) if name.endswith(".py")}
            if not files or any(redact_text(name) != name for name in files):
                raise ValueError
            inventory = [{"path": name, "sha256": sha256(value).hexdigest(), "sizeBytes": len(value)}
                         for name, value in sorted(files.items())]
            return inventory, files
        except Exception:
            raise SecurityStoreError("SECURITY_SCAN_RESULT_INTEGRITY_ERROR") from None

    @staticmethod
    def _configuration(connection, binding, source, scanner_profile):
        rows = [connection.execute(query, (str(binding.run_id),)).fetchone() for query in (
            "SELECT payload_json FROM run_configurations WHERE run_id=?", "SELECT payload_json,status FROM workflow_runs WHERE run_id=?",
        )]
        if any(row is None for row in rows):
            raise SecurityStoreError("SECURITY_SCAN_CONTEXT_DENIED")
        config = RunConfigurationArtifact.model_validate_json(rows[0]["payload_json"])
        run = WorkflowRun.model_validate_json(rows[1]["payload_json"])
        env = config.configuration.environment
        if (config.run_id != binding.run_id or config.workspace_id != binding.workspace_id or run.run_id != binding.run_id
                or run.workspace_id != binding.workspace_id or rows[1]["status"] != run.status.value or config.scenario_id != run.scenario_id
                or env is None or env.network_policy != "DENY" or source.container_image_digest != env.container_image_digest
                or source.dependency_lock_hash != env.dependency_lock_hash
                or config.configuration.scanner_profile_ref != scanner_profile.profile_ref):
            raise SecurityStoreError("SECURITY_SCAN_CONTEXT_DENIED")
        return run

    @staticmethod
    def _context(connection, binding, source, scanner_profile):
        SecurityScanOutputStore._binding(binding)
        try:
            if not isinstance(source, CodeSnapshotArtifact) or source.run_id != binding.run_id:
                raise SecurityStoreError("SECURITY_SCAN_CONTEXT_DENIED")
            run = SecurityScanOutputStore._configuration(connection, binding, source, scanner_profile)
            row = connection.execute("SELECT run_id,payload_json FROM workspaces WHERE workspace_id=?", (str(binding.workspace_id),)).fetchone()
            if row is None:
                raise SecurityStoreError("SECURITY_SCAN_CONTEXT_DENIED")
            workspace = WorkspaceRecord.model_validate_json(row["payload_json"])
            if (run.status not in {WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING} or source.code_version != run.fix_attempt + 1
                    or workspace.workspace_id != binding.workspace_id or workspace.run_id != binding.run_id or row["run_id"] != str(binding.run_id)):
                raise SecurityStoreError("SECURITY_SCAN_CONTEXT_DENIED")
            active = []
            for row in connection.execute("SELECT * FROM workflow_steps WHERE run_id=?", (str(binding.run_id),)):
                step = WorkflowStep.model_validate_json(row["payload_json"])
                if (str(step.workflow_step_id) != row["workflow_step_id"] or step.run_id != run.run_id or step.status.value != row["status"]):
                    raise SecurityStoreError("SECURITY_SCAN_RESULT_INTEGRITY_ERROR")
                if step.agent_role is AgentRole.SECURITY and step.status is WorkflowStepStatus.RUNNING and step.attempt == run.fix_attempt:
                    active.append(step)
            if len(active) != 1:
                raise SecurityStoreError("SECURITY_SCAN_CONTEXT_DENIED")
            step = active[0]
            if (not step.requirement_ids or not set(step.requirement_ids) <= set(source.requirement_ids)
                    or source.artifact_id not in step.input_artifact_ids or step.code_version is not None and step.code_version != source.code_version):
                raise SecurityStoreError("SECURITY_SCAN_CONTEXT_DENIED")
            return step, SecurityScanOutputStore._source(connection, binding, source)
        except SecurityStoreError:
            raise
        except Exception:
            raise SecurityStoreError("SECURITY_SCAN_RESULT_INTEGRITY_ERROR") from None

    @staticmethod
    def _profile(profile):
        if type(profile) is not ExecutionProfile or profile.tool_name != "run_security_scan":
            raise SecurityStoreError("SECURITY_SCAN_RESULT_INVALID")
        return replace(_copy_execution_profile(replace(profile, tool_name="run_build")), tool_name="run_security_scan")

    @staticmethod
    def _scanner_payload(profile):
        checked = _copy_profile(profile)
        return {"name": checked.name, "scannerVersion": checked.scanner_version, "ruleIds": list(sorted(checked.rule_ids)), "profileRef": checked.profile_ref}

    @staticmethod
    def _host_policy(configuration, scanner_profile):
        checked = _copy_configuration(configuration)
        return {"pythonExecutable": checked.python_executable, "limits": asdict(checked.limits), "imageReference": checked.image_reference,
                "runner": security_host_payload(checked, scanner_profile)}

    @staticmethod
    def _verify_host_policy(host, profile, scanner_profile):
        if (type(host) is not dict or set(host) != {"pythonExecutable", "limits", "imageReference", "runner"}
                or type(host["limits"]) is not dict or set(host["limits"]) != set(asdict(SandboxLimits()))):
            raise ValueError
        limits = SandboxLimits(**host["limits"])
        checked = _copy_execution_profile(ExecutionProfile(name=scanner_profile.name, tool_name="run_build", argv=(host["pythonExecutable"],),
            limits=limits, image_reference=host["imageReference"]))
        runner = validate_security_host_payload(host["runner"])
        if (runner != host["runner"] or runner["profile_name"] != scanner_profile.name
                or runner["scanner_version"] != scanner_profile.scanner_version or runner["profile_ref"] != scanner_profile.profile_ref
                or runner["rule_ids"] != sorted(scanner_profile.rule_ids) or profile.name != scanner_profile.name
                or profile.argv != (checked.argv[0], "-I", "-B", "/inputs/_security_runner.py") or profile.image_reference != checked.image_reference):
            raise ValueError
        for name, value in asdict(limits).items():
            actual = getattr(profile.limits, name)
            if (name == "timeout_seconds" and actual > value) or (name != "timeout_seconds" and actual != value):
                raise ValueError

    @staticmethod
    def _inputs_payload(inputs, configuration, scanner_profile):
        if type(inputs) is not SecurityScanInputs:
            raise ValueError
        verified = SecurityScanInputs(files=inputs.files, inputs_sha256=inputs.inputs_sha256, runner_sha256=inputs.runner_sha256,
            contract_sha256=inputs.contract_sha256, host_configuration_sha256=inputs.host_configuration_sha256)
        host = security_host_payload(configuration, scanner_profile)
        if verified.files["_security_host.json"] != _json(host).encode("utf-8"):
            raise ValueError
        payload = {"inputsSha256": verified.inputs_sha256, "runnerSha256": verified.runner_sha256, "contractSha256": verified.contract_sha256,
            "hostConfigurationSha256": verified.host_configuration_sha256,
            "files": [{"path": name, "sha256": sha256(content).hexdigest(), "sizeBytes": len(content)} for name, content in verified.files.items()]}
        SecurityScanOutputStore._verify_inputs_payload(payload, host)
        return payload

    @staticmethod
    def _verify_inputs_payload(payload, host):
        fields = {"inputsSha256", "runnerSha256", "contractSha256", "hostConfigurationSha256", "files"}
        if (type(payload) is not dict or set(payload) != fields or type(payload["files"]) is not list or len(payload["files"]) != 3
                or any(type(payload[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", payload[key]) is None for key in fields - {"files"})):
            raise ValueError
        rows = payload["files"]
        for row in rows:
            if (type(row) is not dict or set(row) != {"path", "sha256", "sizeBytes"} or row["path"] not in _REQUIRED
                    or type(row["sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None
                    or type(row["sizeBytes"]) is not int or not 1 <= row["sizeBytes"] <= 1024 * 1024):
                raise ValueError
        if [row["path"] for row in rows] != sorted(_REQUIRED):
            raise ValueError
        by_name = {row["path"]: row for row in rows}
        digest = sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        host_bytes = _json(host).encode("utf-8")
        if (payload["inputsSha256"] != digest or payload["runnerSha256"] != by_name["_security_runner.py"]["sha256"]
                or payload["contractSha256"] != by_name["_security_contract.py"]["sha256"]
                or payload["hostConfigurationSha256"] != by_name["_security_host.json"]["sha256"]
                or payload["hostConfigurationSha256"] != sha256(host_bytes).hexdigest()
                or by_name["_security_host.json"]["sizeBytes"] != len(host_bytes)):
            raise ValueError

    @staticmethod
    def _complete(report, scanner_profile, files):
        if (report.profile_name != scanner_profile.name or report.scanner_version != scanner_profile.scanner_version
                or report.rule_ids != tuple(sorted(scanner_profile.rule_ids)) or report.profile_ref != scanner_profile.profile_ref
                or report.scanned_files != tuple(sorted(files))):
            raise ValueError
        line_cache = {}
        for finding in report.findings:
            if finding.path not in line_cache:
                content = files[finding.path]
                encoding, _lines = tokenize.detect_encoding(io.BytesIO(content).readline)
                # AST offsets use UTF-8 byte columns of decoded Python text,
                # not the original byte count of a latin-1 declared source.
                text = content.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
                lines = text.split("\n")
                if lines[-1] == "":
                    lines.pop()
                # str.splitlines also treats Unicode U+2028/U+0085 inside
                # valid string literals as line breaks; Python AST does not.
                line_cache[finding.path] = lines
            lines = line_cache[finding.path]
            if not 1 <= finding.line <= len(lines) or finding.column > len(lines[finding.line - 1].encode("utf-8")):
                raise ValueError

    @staticmethod
    def _result_metadata(binding, source, result, manifest_id, step_id, profile, scanner_profile, inputs, host_policy, inventory, files, report):
        try:
            SecurityScanOutputStore._binding(binding)
            if type(result) is not SandboxResult or not isinstance(source, CodeSnapshotArtifact) or type(report) is not SecurityScanReport:
                raise ValueError
            manifest_id, execution_id, step_id = _uuid(manifest_id), _uuid(result.execution_id), _uuid(step_id)
            if manifest_id == execution_id:
                raise SecurityStoreError("SECURITY_SCAN_RECORD_CONFLICT")
            profile = SecurityScanOutputStore._profile(profile)
            scanner_profile = _copy_profile(scanner_profile)
            SecurityScanOutputStore._verify_host_policy(host_policy, profile, scanner_profile)
            SecurityScanOutputStore._verify_inputs_payload(inputs, host_policy["runner"])
            manifest = ExecutionManifest.model_validate(result.execution_manifest.model_dump(mode="json", by_alias=True))
            ref = profile.image_reference
            image_matches = (result.image_id == manifest.container_image_digest if ref is None else
                ref == manifest.container_image_digest == result.image_id if ref.startswith("sha256:") else
                ref.rsplit("@", 1)[-1] == manifest.container_image_digest)
            if (result.run_id != binding.run_id or result.source_artifact_id != source.artifact_id or source.run_id != binding.run_id
                    or result.tool_name != "run_security_scan" or manifest != source.execution_manifest()
                    or result.profile_name != scanner_profile.name or not image_matches or type(result.image_id) is not str
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", result.image_id) is None or type(result.container_id) is not str
                    or re.fullmatch(r"[0-9a-f]{64}", result.container_id) is None or type(result.duration_ms) is not int
                    or not 0 <= result.duration_ms <= 2**63 - 1):
                raise ValueError
            if type(result.stderr) is not str:
                raise ValueError
            if len(result.stderr.encode("utf-8")) > min(MAX_SECURITY_OUTPUT_BYTES, profile.limits.max_stderr_bytes):
                raise SecurityStoreError("SECURITY_SCAN_OUTPUT_LIMIT")
            if result.stderr:
                # Issue text can expose unlabeled Source literals. Masking is
                # not sufficient: the trusted harness emits no diagnostics.
                raise ValueError
            parsed = parse_security_report(result.stdout, result.exit_code)
            if report != parsed:
                raise ValueError
            SecurityScanOutputStore._complete(parsed, scanner_profile, files)
            report_bytes = _json(parsed.to_dict()).encode("utf-8")
            if (len(result.stdout.encode("utf-8")) > min(MAX_SECURITY_OUTPUT_BYTES, profile.limits.max_stdout_bytes)
                    or len(report_bytes) > min(_MAX_REPORT_BYTES, profile.limits.max_stdout_bytes)):
                raise SecurityStoreError("SECURITY_SCAN_OUTPUT_LIMIT")
            metadata = {
                "executionManifestId": str(manifest_id), "executionId": str(execution_id), "runId": str(binding.run_id),
                "workspaceId": str(binding.workspace_id), "workflowStepId": str(step_id), "sourceArtifactId": str(source.artifact_id),
                "executionManifest": manifest.model_dump(mode="json", by_alias=True), "role": "SECURITY",
                "executionProfile": {"name": profile.name, "toolName": profile.tool_name, "argv": list(profile.argv),
                    "limits": asdict(profile.limits), "imageReference": profile.image_reference},
                "scannerProfile": SecurityScanOutputStore._scanner_payload(scanner_profile), "inputs": inputs,
                "hostConfiguration": host_policy, "hostPolicySha256": sha256(_json(host_policy).encode("utf-8")).hexdigest(),
                "sourceFiles": inventory, "sourceFilesSha256": sha256(_json(inventory).encode("utf-8")).hexdigest(),
                "profileName": result.profile_name, "toolName": result.tool_name, "imageId": result.image_id, "containerId": result.container_id,
                "exitCode": result.exit_code, "durationMs": result.duration_ms,
            }
            for name, content in (("report", report_bytes), ("stdout", report_bytes), ("stderr", b"")):
                metadata[name + "Sha256"] = sha256(content).hexdigest()
                metadata[name + "SizeBytes"] = len(content)
            raw = _json(metadata)
            if len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
                raise ValueError
            return metadata, raw, report_bytes
        except SecurityStoreError:
            raise
        except Exception:
            raise SecurityStoreError("SECURITY_SCAN_RESULT_INVALID") from None

    @staticmethod
    def _collision(connection, identities):
        for identity in identities:
            for query in ("SELECT 1 FROM artifact_contents WHERE artifact_id=?", "SELECT 1 FROM project_artifacts WHERE artifact_id=?",
                "SELECT 1 FROM run_configurations WHERE COALESCE(json_extract(payload_json,'$.artifact_id'),json_extract(payload_json,'$.artifactId'))=?"):
                if connection.execute(query, (identity,)).fetchone() is not None:
                    raise SecurityStoreError("SECURITY_SCAN_RECORD_CONFLICT")
            for table in ("security_scan_execution_records", "browser_test_execution_records", "unit_test_execution_records", "build_execution_records"):
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None:
                    if connection.execute(f"SELECT 1 FROM {table} WHERE execution_manifest_id=? OR execution_id=?", (identity, identity)).fetchone() is not None:
                        raise SecurityStoreError("SECURITY_SCAN_RECORD_CONFLICT")
            for table, column in (("tool_execution_calls", "logical_call_id"),
                                  ("tool_execution_attempt_starts", "attempt_id")):
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None:
                    if connection.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (identity,)).fetchone() is not None:
                        raise SecurityStoreError("SECURITY_SCAN_RECORD_CONFLICT")

    def publish(self, binding, source, result, *, profile, scanner_profile, inputs, report, configuration):
        try:
            self._binding(binding)
            try:
                scanner_profile = _copy_profile(scanner_profile)
                configuration = _copy_configuration(configuration)
                host = self._host_policy(configuration, scanner_profile)
                inputs_payload = self._inputs_payload(inputs, configuration, scanner_profile)
            except Exception:
                raise SecurityStoreError("SECURITY_SCAN_RESULT_INVALID") from None
            manifest_id = _uuid(uuid4())
            with self._repository._transaction() as connection:
                step, stored = self._context(connection, binding, source, scanner_profile)
                inventory, files = self.source_inventory(stored.content)
                metadata, raw, report_bytes = self._result_metadata(binding, source, result, manifest_id, step.workflow_step_id,
                    profile, scanner_profile, inputs_payload, host, inventory, files, report)
                self._ensure_schema(connection)
                self._collision(connection, (str(manifest_id), metadata["executionId"]))
                connection.execute("""INSERT INTO security_scan_execution_records(execution_manifest_id,execution_id,run_id,workspace_id,
                    workflow_step_id,source_artifact_id,metadata_json,metadata_sha256,report,stdout,stderr) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(manifest_id), metadata["executionId"], str(binding.run_id), str(binding.workspace_id), str(step.workflow_step_id),
                     str(source.artifact_id), raw, sha256(raw.encode("utf-8")).hexdigest(), sqlite3.Binary(report_bytes), sqlite3.Binary(report_bytes), sqlite3.Binary(b"")))
                row = connection.execute("SELECT * FROM security_scan_execution_records WHERE execution_manifest_id=?", (str(manifest_id),)).fetchone()
                return self._decode(connection, row)
        except SecurityStoreError:
            raise
        except sqlite3.IntegrityError:
            raise SecurityStoreError("SECURITY_SCAN_RECORD_CONFLICT") from None
        except Exception:
            raise SecurityStoreError("SECURITY_SCAN_STORAGE_ERROR") from None

    @staticmethod
    def _decode(connection, row):
        try:
            raw = row["metadata_json"]
            metadata = parse_json(raw, max_bytes=_MAX_METADATA_BYTES)
            if (type(metadata) is not dict or set(metadata) != _METADATA_KEYS or _json(metadata) != raw
                    or sha256(raw.encode("utf-8")).hexdigest() != row["metadata_sha256"] or metadata["role"] != "SECURITY"):
                raise ValueError
            ids = {name: _uuid(metadata[name]) for name in ("executionManifestId", "executionId", "runId", "workspaceId", "workflowStepId", "sourceArtifactId")}
            for name, column in (("executionManifestId", "execution_manifest_id"), ("executionId", "execution_id"), ("runId", "run_id"),
                ("workspaceId", "workspace_id"), ("workflowStepId", "workflow_step_id"), ("sourceArtifactId", "source_artifact_id")):
                if metadata[name] != row[column] or str(ids[name]) != row[column]:
                    raise ValueError
            binding = MCPBinding(role=AgentRole.SECURITY, agent_role=AgentRole.SECURITY, run_id=ids["runId"], workspace_id=ids["workspaceId"])
            scanner = metadata["scannerProfile"]
            if type(scanner) is not dict or set(scanner) != {"name", "scannerVersion", "ruleIds", "profileRef"} or type(scanner["ruleIds"]) is not list:
                raise ValueError
            scanner_profile = SecurityScannerProfile(name=scanner["name"], scanner_version=scanner["scannerVersion"], rule_ids=tuple(scanner["ruleIds"]), profile_ref=scanner["profileRef"])
            profile_data = metadata["executionProfile"]
            if (type(profile_data) is not dict or set(profile_data) != {"name", "toolName", "argv", "limits", "imageReference"}
                    or type(profile_data["argv"]) is not list or type(profile_data["limits"]) is not dict):
                raise ValueError
            profile = ExecutionProfile(name=profile_data["name"], tool_name=profile_data["toolName"], argv=tuple(profile_data["argv"]),
                limits=SandboxLimits(**profile_data["limits"]), image_reference=profile_data["imageReference"])
            source_row = connection.execute("SELECT * FROM artifact_contents WHERE artifact_id=? AND run_id=?", (row["source_artifact_id"], row["run_id"])).fetchone()
            if source_row is None:
                raise ValueError
            source = SQLiteArtifactContentStore._decode(connection, source_row).metadata
            stored = SecurityScanOutputStore._source(connection, binding, source)
            inventory, files = SecurityScanOutputStore.source_inventory(stored.content)
            SecurityScanOutputStore._configuration(connection, binding, source, scanner_profile)
            workspace_row = connection.execute("SELECT run_id,payload_json FROM workspaces WHERE workspace_id=?", (row["workspace_id"],)).fetchone()
            step_row = connection.execute("SELECT run_id,payload_json FROM workflow_steps WHERE workflow_step_id=?", (row["workflow_step_id"],)).fetchone()
            if workspace_row is None or step_row is None:
                raise ValueError
            workspace = WorkspaceRecord.model_validate_json(workspace_row["payload_json"])
            step = WorkflowStep.model_validate_json(step_row["payload_json"])
            if (workspace_row["run_id"] != row["run_id"] or str(workspace.workspace_id) != row["workspace_id"] or str(workspace.run_id) != row["run_id"]
                    or step_row["run_id"] != row["run_id"] or str(step.workflow_step_id) != row["workflow_step_id"] or str(step.run_id) != row["run_id"]
                    or step.agent_role is not AgentRole.SECURITY or step.attempt != source.code_version - 1
                    or not step.requirement_ids or not set(step.requirement_ids) <= set(source.requirement_ids)
                    or source.artifact_id not in step.input_artifact_ids or step.code_version is not None and step.code_version != source.code_version
                    or metadata["sourceFiles"] != inventory or metadata["sourceFilesSha256"] != sha256(_json(inventory).encode("utf-8")).hexdigest()):
                raise ValueError
            streams = {}
            for name in ("report", "stdout", "stderr"):
                content = row[name]
                maximum = _MAX_REPORT_BYTES if name == "report" else MAX_SECURITY_OUTPUT_BYTES
                if (type(content) is not bytes or len(content) > maximum or type(metadata[name + "SizeBytes"]) is not int
                        or metadata[name + "SizeBytes"] != len(content) or metadata[name + "Sha256"] != sha256(content).hexdigest()):
                    raise ValueError
                streams[name] = content.decode("utf-8")
            report = parse_security_report(streams["report"], metadata["exitCode"])
            host = metadata["hostConfiguration"]
            if (streams["stderr"] or _json(report.to_dict()) != streams["report"] or streams["stdout"] != streams["report"]
                    or metadata["hostPolicySha256"] != sha256(_json(host).encode("utf-8")).hexdigest()):
                raise ValueError
            result = SandboxResult(execution_id=ids["executionId"], run_id=ids["runId"], source_artifact_id=ids["sourceArtifactId"],
                profile_name=metadata["profileName"], tool_name=metadata["toolName"], execution_manifest=ExecutionManifest.model_validate(metadata["executionManifest"]),
                image_id=metadata["imageId"], container_id=metadata["containerId"], exit_code=metadata["exitCode"], duration_ms=metadata["durationMs"],
                stdout=streams["stdout"], stderr=streams["stderr"])
            _metadata, expected, _report = SecurityScanOutputStore._result_metadata(binding, source, result, ids["executionManifestId"], ids["workflowStepId"],
                profile, scanner_profile, metadata["inputs"], host, inventory, files, report)
            if expected != raw:
                raise ValueError
            return SecurityScanExecutionRecord(execution_manifest_id=ids["executionManifestId"], execution_id=ids["executionId"], run_id=ids["runId"],
                workspace_id=ids["workspaceId"], workflow_step_id=ids["workflowStepId"], source_artifact_id=ids["sourceArtifactId"], execution_manifest=result.execution_manifest,
                execution_profile=profile, scanner_profile=scanner_profile, inputs=_freeze(metadata["inputs"]), host_configuration=_freeze(host), source_files=_freeze(inventory),
                report=report, role=AgentRole.SECURITY, profile_name=result.profile_name, image_id=result.image_id, container_id=result.container_id, exit_code=result.exit_code,
                duration_ms=result.duration_ms, stdout=streams["stdout"], stderr=streams["stderr"], stdout_sha256=metadata["stdoutSha256"], stderr_sha256=metadata["stderrSha256"],
                report_sha256=metadata["reportSha256"], metadata_sha256=row["metadata_sha256"])
        except Exception:
            raise SecurityStoreError("SECURITY_SCAN_RESULT_INTEGRITY_ERROR") from None

    @staticmethod
    def _get(connection, run_id, manifest_id):
        SQLiteArtifactContentStore._ensure_schema(connection)
        SecurityScanOutputStore._ensure_schema(connection)
        row = connection.execute("SELECT * FROM security_scan_execution_records WHERE run_id=? AND execution_manifest_id=?", (str(run_id), str(manifest_id))).fetchone()
        if row is None:
            raise SecurityStoreError("SECURITY_SCAN_RECORD_NOT_FOUND")
        return SecurityScanOutputStore._decode(connection, row)

    def get(self, run_id, execution_manifest_id):
        run_id, manifest_id = _uuid(run_id), _uuid(execution_manifest_id)
        try:
            with self._repository._transaction() as connection:
                return self._get(connection, run_id, manifest_id)
        except SecurityStoreError:
            raise
        except Exception:
            raise SecurityStoreError("SECURITY_SCAN_STORAGE_ERROR") from None

    def read_report(self, binding, report_ref):
        self._binding(binding)
        if type(report_ref) is not str:
            raise SecurityStoreError("SECURITY_SCAN_RESULT_INVALID")
        match = re.fullmatch(r"artifact://([0-9a-f-]{36})/security-scan-report\.json", report_ref)
        if match is None:
            raise SecurityStoreError("SECURITY_SCAN_RESULT_INVALID")
        manifest_id = _uuid(match[1])
        if str(manifest_id) != match[1]:
            raise SecurityStoreError("SECURITY_SCAN_RESULT_INVALID")
        try:
            with self._repository._transaction() as connection:
                record = self._get(connection, binding.run_id, manifest_id)
                if record.workspace_id != binding.workspace_id:
                    raise SecurityStoreError("SECURITY_SCAN_CONTEXT_DENIED")
                return record.report.to_dict()
        except SecurityStoreError:
            raise
        except Exception:
            raise SecurityStoreError("SECURITY_SCAN_STORAGE_ERROR") from None
