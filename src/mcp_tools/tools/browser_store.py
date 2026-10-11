"""Immutable browser execution receipts and privacy-safe per-case JSON traces.

Receipts are private Tool outputs, not A2A QA Artifacts or final verdicts.
Raw suites, selectors, values, DOM, screenshots and browser ZIPs are not stored.
Source bytes and QA grants are checked in the publication/read transaction.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
import json
import re
import sqlite3
from types import MappingProxyType
from uuid import UUID, uuid4

from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.browser_config import (
    BrowserTestConfiguration, BrowserTestSuite, _copy_configuration, _copy_suite,
    browser_host_payload, validate_browser_host_payload,
)
from mcp_tools.tools.browser_contract import parse_browser_suite
from mcp_tools.tools.browser_inputs import BrowserTestInputs
from mcp_tools.tools.browser_report import ACTIONS, BrowserTestReport, parse_browser_report
from mcp_tools.tools.build_config import _copy_profile
from mcp_tools.tools.unit_config import MAX_UNIT_FILES, MAX_UNIT_FILE_BYTES, MAX_UNIT_TOTAL_BYTES, _test_path, _suite_reference
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
from orchestrator.sandbox.materialization import _check_names
from orchestrator.workspaces.policy import WorkspaceAccessError, workspace_uuid


MAX_BROWSER_OUTPUT_BYTES = 4 * 1024 * 1024
_MAX_REPORT_BYTES = 1024 * 1024
_MAX_METADATA_BYTES = 512 * 1024
_REQUIRED = frozenset({"_browser_runner.py", "_browser_contract.py", "_browser_host.json"})
_CODES = frozenset({
    "BROWSER_TEST_STORAGE_ERROR", "BROWSER_TEST_RECORD_CONFLICT", "BROWSER_TEST_RESULT_INVALID",
    "BROWSER_TEST_RESULT_INTEGRITY_ERROR", "BROWSER_TEST_CONTEXT_DENIED", "BROWSER_TEST_OUTPUT_LIMIT",
    "BROWSER_TEST_RECORD_NOT_FOUND",
})
_METADATA_KEYS = frozenset({
    "executionManifestId", "executionId", "runId", "workspaceId", "workflowStepId", "sourceArtifactId",
    "executionManifest", "role", "profileName", "toolName", "imageId", "containerId", "exitCode", "durationMs",
    "stdoutSha256", "stderrSha256", "stdoutSizeBytes", "stderrSizeBytes", "executionProfile", "suite", "inputs",
    "hostConfiguration", "hostPolicySha256", "reportSha256", "reportSizeBytes", "tracesSha256", "tracesSizeBytes",
})


class BrowserStoreError(RuntimeError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in _CODES else "BROWSER_TEST_STORAGE_ERROR"
        super().__init__(self.code)


def _uuid(value):
    try:
        return workspace_uuid(value)
    except WorkspaceAccessError:
        raise BrowserStoreError("BROWSER_TEST_RESULT_INVALID") from None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _freeze(value):
    if type(value) is dict:
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze(item) for item in value)
    return value


def _output(value):
    if type(value) is not str:
        raise BrowserStoreError("BROWSER_TEST_RESULT_INVALID")
    if len(value.encode("utf-8")) > MAX_BROWSER_OUTPUT_BYTES:
        raise BrowserStoreError("BROWSER_TEST_OUTPUT_LIMIT")
    encoded = redact_text(value).encode("utf-8")
    if len(encoded) > MAX_BROWSER_OUTPUT_BYTES:
        raise BrowserStoreError("BROWSER_TEST_OUTPUT_LIMIT")
    return encoded


@dataclass(frozen=True, kw_only=True)
class BrowserTestExecutionRecord:
    execution_manifest_id: UUID
    execution_id: UUID
    run_id: UUID = field(repr=False)
    workspace_id: UUID = field(repr=False)
    workflow_step_id: UUID = field(repr=False)
    source_artifact_id: UUID = field(repr=False)
    execution_manifest: ExecutionManifest = field(repr=False)
    execution_profile: ExecutionProfile = field(repr=False)
    role: AgentRole
    profile_name: str
    suite: Mapping = field(repr=False)
    inputs: Mapping = field(repr=False)
    host_configuration: Mapping = field(repr=False)
    report: BrowserTestReport = field(repr=False)
    image_id: str
    container_id: str
    exit_code: int
    duration_ms: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)
    stdout_sha256: str
    stderr_sha256: str
    report_sha256: str
    traces_sha256: str
    metadata_sha256: str

    @property
    def tool_name(self):
        return "run_browser_tests"

    @property
    def report_ref(self):
        return f"artifact://{self.execution_manifest_id}/browser-test-report.json"

    @property
    def trace_refs(self):
        return tuple(f"artifact://{self.execution_manifest_id}/browser-trace-{index:04}.json"
                     for index in range(self.report.total))

    def tool_output(self):
        return {"total": self.report.total, "passed": self.report.passed, "failed": self.report.failed,
                "traceRefs": list(self.trace_refs), "executionManifestId": str(self.execution_manifest_id)}


class BrowserTestOutputStore:
    def __init__(self, repository):
        if not isinstance(repository, SQLiteWorkflowRepository):
            raise BrowserStoreError("BROWSER_TEST_RESULT_INVALID")
        self._repository = repository

    def __repr__(self):
        return "BrowserTestOutputStore()"

    @staticmethod
    def _ensure_schema(connection):
        for statement in (
            """CREATE TABLE IF NOT EXISTS browser_test_execution_records (
                execution_manifest_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                workflow_step_id TEXT NOT NULL REFERENCES workflow_steps(workflow_step_id),
                source_artifact_id TEXT NOT NULL REFERENCES artifact_contents(artifact_id),
                metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json)),
                metadata_sha256 TEXT NOT NULL CHECK(length(metadata_sha256)=64),
                report BLOB NOT NULL CHECK(typeof(report)='blob' AND length(report)<=1048576),
                traces BLOB NOT NULL CHECK(typeof(traces)='blob' AND length(traces)<=1048576),
                stdout BLOB NOT NULL CHECK(typeof(stdout)='blob' AND length(stdout)<=4194304),
                stderr BLOB NOT NULL CHECK(typeof(stderr)='blob' AND length(stderr)<=4194304),
                CHECK(execution_manifest_id != execution_id))""",
            """CREATE INDEX IF NOT EXISTS browser_test_records_by_run
                ON browser_test_execution_records(run_id,execution_manifest_id)""",
            """CREATE TRIGGER IF NOT EXISTS browser_test_records_no_update BEFORE UPDATE ON browser_test_execution_records
                BEGIN SELECT RAISE(ABORT,'immutable Browser Test execution'); END""",
            """CREATE TRIGGER IF NOT EXISTS browser_test_records_no_delete BEFORE DELETE ON browser_test_execution_records
                BEGIN SELECT RAISE(ABORT,'immutable Browser Test execution'); END""",
            """CREATE TRIGGER IF NOT EXISTS browser_test_records_no_replace BEFORE INSERT ON browser_test_execution_records
                WHEN EXISTS (SELECT 1 FROM browser_test_execution_records
                WHERE execution_manifest_id IN(NEW.execution_manifest_id,NEW.execution_id)
                    OR execution_id IN(NEW.execution_manifest_id,NEW.execution_id))
                BEGIN SELECT RAISE(ABORT,'immutable Browser Test execution'); END""",
            """CREATE TRIGGER IF NOT EXISTS browser_test_records_ownership BEFORE INSERT ON browser_test_execution_records
                WHEN NOT EXISTS(SELECT 1 FROM workspaces WHERE workspace_id=NEW.workspace_id AND run_id=NEW.run_id)
                    OR NOT EXISTS(SELECT 1 FROM workflow_steps WHERE workflow_step_id=NEW.workflow_step_id AND run_id=NEW.run_id)
                    OR NOT EXISTS(SELECT 1 FROM artifact_contents WHERE artifact_id=NEW.source_artifact_id
                        AND run_id=NEW.run_id AND artifact_type='SOURCE')
                BEGIN SELECT RAISE(ABORT,'Browser Test ownership mismatch'); END""",
        ):
            connection.execute(statement)

    @staticmethod
    def _source(connection, binding, source):
        try:
            return UnitTestOutputStore._source(connection, binding, source)
        except Exception:
            raise BrowserStoreError("BROWSER_TEST_RESULT_INTEGRITY_ERROR") from None

    @staticmethod
    def _binding(binding):
        if (not isinstance(binding, MCPBinding) or binding.role is not AgentRole.QA
                or binding.agent_role is not AgentRole.QA):
            raise BrowserStoreError("BROWSER_TEST_CONTEXT_DENIED")

    @staticmethod
    def _configuration(connection, binding, source, suite):
        row = connection.execute("SELECT payload_json FROM run_configurations WHERE run_id=?", (str(binding.run_id),)).fetchone()
        if row is None:
            raise BrowserStoreError("BROWSER_TEST_CONTEXT_DENIED")
        config = RunConfigurationArtifact.model_validate_json(row["payload_json"])
        run_row = connection.execute("SELECT payload_json,status FROM workflow_runs WHERE run_id=?", (str(binding.run_id),)).fetchone()
        if run_row is None:
            raise BrowserStoreError("BROWSER_TEST_CONTEXT_DENIED")
        run = WorkflowRun.model_validate_json(run_row["payload_json"])
        environment = config.configuration.environment
        if (config.run_id != binding.run_id or config.workspace_id != binding.workspace_id
                or run.run_id != binding.run_id or run.workspace_id != binding.workspace_id
                or run_row["status"] != run.status.value or config.scenario_id != run.scenario_id
                or environment is None or environment.network_policy != "DENY"
                or source.container_image_digest != environment.container_image_digest
                or source.dependency_lock_hash != environment.dependency_lock_hash
                or suite["kind"] == "PROTECTED" and suite["protectedSuiteRef"] != config.configuration.protected_test_suite_ref):
            raise BrowserStoreError("BROWSER_TEST_CONTEXT_DENIED")
        return config

    @staticmethod
    def _context(connection, binding, source, suite):
        BrowserTestOutputStore._binding(binding)
        if not isinstance(source, CodeSnapshotArtifact) or source.run_id != binding.run_id:
            raise BrowserStoreError("BROWSER_TEST_CONTEXT_DENIED")
        try:
            rows = [connection.execute(query, (str(identity),)).fetchone() for query, identity in (
                ("SELECT * FROM workflow_runs WHERE run_id=?", binding.run_id),
                ("SELECT * FROM workspaces WHERE workspace_id=?", binding.workspace_id),
            )]
            if any(row is None for row in rows):
                raise BrowserStoreError("BROWSER_TEST_CONTEXT_DENIED")
            run = WorkflowRun.model_validate_json(rows[0]["payload_json"])
            workspace = WorkspaceRecord.model_validate_json(rows[1]["payload_json"])
            config = BrowserTestOutputStore._configuration(connection, binding, source, suite)
            if (run.run_id != binding.run_id or run.workspace_id != binding.workspace_id
                    or rows[0]["status"] != run.status.value
                    or run.status not in {WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING}
                    or workspace.workspace_id != binding.workspace_id or workspace.run_id != binding.run_id
                    or rows[1]["run_id"] != str(binding.run_id) or config.scenario_id != run.scenario_id
                    or source.code_version != run.fix_attempt + 1):
                raise BrowserStoreError("BROWSER_TEST_CONTEXT_DENIED")
            active = []
            for row in connection.execute("SELECT * FROM workflow_steps WHERE run_id=?", (str(binding.run_id),)):
                step = WorkflowStep.model_validate_json(row["payload_json"])
                if (str(step.workflow_step_id) != row["workflow_step_id"] or step.run_id != run.run_id
                        or step.status.value != row["status"]):
                    raise BrowserStoreError("BROWSER_TEST_RESULT_INTEGRITY_ERROR")
                if step.agent_role is AgentRole.QA and step.status is WorkflowStepStatus.RUNNING:
                    active.append(step)
            if len(active) != 1:
                raise BrowserStoreError("BROWSER_TEST_CONTEXT_DENIED")
            step = active[0]
            if (not step.requirement_ids or not set(step.requirement_ids) <= set(source.requirement_ids)
                    or source.artifact_id not in step.input_artifact_ids
                    or step.code_version is not None and step.code_version != source.code_version):
                raise BrowserStoreError("BROWSER_TEST_CONTEXT_DENIED")
            return step, BrowserTestOutputStore._source(connection, binding, source)
        except BrowserStoreError:
            raise
        except Exception:
            raise BrowserStoreError("BROWSER_TEST_RESULT_INTEGRITY_ERROR") from None

    @staticmethod
    def _profile(profile):
        if type(profile) is not ExecutionProfile or profile.tool_name != "run_browser_tests":
            raise BrowserStoreError("BROWSER_TEST_RESULT_INVALID")
        return replace(_copy_profile(replace(profile, tool_name="run_build")), tool_name="run_browser_tests")

    @staticmethod
    def _host_policy(configuration, suite):
        config = _copy_configuration(configuration)
        return {"pythonExecutable": config.python_executable, "limits": asdict(config.limits),
                "imageReference": config.image_reference, "runner": browser_host_payload(config, suite)}

    @staticmethod
    def _verify_host_policy(host, profile, suite):
        if (type(host) is not dict or set(host) != {"pythonExecutable", "limits", "imageReference", "runner"}
                or type(host["limits"]) is not dict or set(host["limits"]) != set(asdict(SandboxLimits()))):
            raise ValueError
        limits = SandboxLimits(**host["limits"])
        checked = _copy_profile(ExecutionProfile(name=suite["name"], tool_name="run_build",
            argv=(host["pythonExecutable"],), limits=limits, image_reference=host["imageReference"]))
        runner = validate_browser_host_payload(host["runner"])
        if (runner != host["runner"] or runner["suite_name"] != suite["name"]
                or runner["suite_path"] != suite["suitePath"] or profile.name != suite["name"]
                or profile.argv != (checked.argv[0], "-I", "-B", "/inputs/_browser_runner.py")
                or profile.image_reference != checked.image_reference):
            raise ValueError
        for name, value in asdict(limits).items():
            actual = getattr(profile.limits, name)
            if (name == "timeout_seconds" and actual > value) or (name != "timeout_seconds" and actual != value):
                raise ValueError

    @staticmethod
    def _inputs_payload(inputs, suite, configuration):
        if type(inputs) is not BrowserTestInputs:
            raise BrowserStoreError("BROWSER_TEST_RESULT_INVALID")
        verified = BrowserTestInputs(files=inputs.files, inputs_sha256=inputs.inputs_sha256,
            runner_sha256=inputs.runner_sha256, test_files_sha256=inputs.test_files_sha256,
            host_configuration_sha256=inputs.host_configuration_sha256, contract_sha256=inputs.contract_sha256)
        host = browser_host_payload(configuration, suite)
        if verified.files["_browser_host.json"] != _json(host).encode("utf-8"):
            raise BrowserStoreError("BROWSER_TEST_RESULT_INVALID")
        tests = {path: value for path, value in verified.files.items() if path not in _REQUIRED}
        if suite.kind == "PROTECTED" and tests != {path: value.encode("utf-8") for path, value in suite.protected_files.items()}:
            raise BrowserStoreError("BROWSER_TEST_RESULT_INVALID")
        parsed = parse_browser_suite(tests[suite.suite_path].decode("utf-8"), suite.name)
        inventory = {"name": suite.name, "kind": suite.kind, "suitePath": suite.suite_path,
            "protectedSuiteRef": suite.protected_suite_ref,
            "testSuiteSha256": sha256(tests[suite.suite_path]).hexdigest(),
            "tests": [{"testId": case["testId"], "actions": [step["action"] for step in case["steps"]]}
                      for case in parsed["tests"]]}
        payload = {"inputsSha256": verified.inputs_sha256, "runnerSha256": verified.runner_sha256,
            "testFilesSha256": verified.test_files_sha256, "hostConfigurationSha256": verified.host_configuration_sha256,
            "contractSha256": verified.contract_sha256,
            "files": [{"path": path, "sha256": sha256(content).hexdigest(), "sizeBytes": len(content)}
                      for path, content in verified.files.items()]}
        BrowserTestOutputStore._verify_inputs_payload(payload, inventory, host)
        return payload, inventory

    @staticmethod
    def _verify_suite(suite):
        if (type(suite) is not dict or set(suite) != {"name", "kind", "suitePath", "protectedSuiteRef", "testSuiteSha256", "tests"}
                or type(suite["name"]) is not str or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", suite["name"]) is None
                or suite["kind"] not in {"QA_TESTS", "PROTECTED"}
                or type(suite["testSuiteSha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", suite["testSuiteSha256"]) is None):
            raise ValueError
        _test_path(suite["suitePath"])
        if not suite["suitePath"].endswith(".json"):
            raise ValueError
        if suite["kind"] == "PROTECTED":
            _suite_reference(suite["protectedSuiteRef"])
        elif suite["protectedSuiteRef"] is not None:
            raise ValueError
        if type(suite["tests"]) is not list or not 1 <= len(suite["tests"]) <= 100:
            raise ValueError
        seen, total = set(), 0
        for case in suite["tests"]:
            if (type(case) is not dict or set(case) != {"testId", "actions"}
                    or type(case["testId"]) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", case["testId"]) is None
                    or case["testId"] in seen or type(case["actions"]) is not list
                    or not 1 <= len(case["actions"]) <= 100 or case["actions"][0] != "goto"
                    or any(type(action) is not str or action not in ACTIONS for action in case["actions"])
                    or not any(action.startswith("assert_") for action in case["actions"])):
                raise ValueError
            seen.add(case["testId"])
            total += len(case["actions"])
        if total > 1000:
            raise ValueError

    @staticmethod
    def _verify_inputs_payload(payload, suite, host):
        BrowserTestOutputStore._verify_suite(suite)
        fields = {"inputsSha256", "runnerSha256", "testFilesSha256", "hostConfigurationSha256", "contractSha256", "files"}
        if (type(payload) is not dict or set(payload) != fields or type(payload["files"]) is not list
                or not 4 <= len(payload["files"]) <= MAX_UNIT_FILES + 3
                or any(type(payload[key]) is not str or re.fullmatch(r"[0-9a-f]{64}", payload[key]) is None for key in fields - {"files"})):
            raise ValueError
        rows = payload["files"]
        for row in rows:
            if (type(row) is not dict or set(row) != {"path", "sha256", "sizeBytes"}
                    or type(row["sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None
                    or type(row["sizeBytes"]) is not int or not 0 <= row["sizeBytes"] <= MAX_UNIT_FILE_BYTES):
                raise ValueError
            if row["path"] not in _REQUIRED:
                _test_path(row["path"])
        names = [row["path"] for row in rows]
        if (names != sorted(names) or len(names) != len(set(names)) or not _REQUIRED <= set(names)
                or suite["suitePath"] not in names or sum(row["sizeBytes"] for row in rows) > MAX_UNIT_TOTAL_BYTES):
            raise ValueError
        _check_names(names)
        # Input manifests intentionally preserve _files_hash's key ordering.
        def digest(entries):
            return sha256(json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        tests = [row for row in rows if row["path"] not in _REQUIRED]
        assets = {row["path"]: row for row in rows}
        if (payload["inputsSha256"] != digest(rows) or payload["testFilesSha256"] != digest(tests)
                or payload["runnerSha256"] != assets["_browser_runner.py"]["sha256"]
                or payload["contractSha256"] != assets["_browser_contract.py"]["sha256"]
                or payload["hostConfigurationSha256"] != assets["_browser_host.json"]["sha256"]
                or suite["testSuiteSha256"] != assets[suite["suitePath"]]["sha256"]
                or payload["hostConfigurationSha256"] != sha256(_json(host).encode("utf-8")).hexdigest()
                or assets["_browser_host.json"]["sizeBytes"] != len(_json(host).encode("utf-8"))
                or assets["_browser_runner.py"]["sizeBytes"] == 0 or assets["_browser_contract.py"]["sizeBytes"] == 0):
            raise ValueError

    @staticmethod
    def _complete(report, suite, host):
        if (report.suite_name != suite["name"] or report.playwright_version != host["playwright_version"]
                or [case.test_id for case in report.tests] != [case["testId"] for case in suite["tests"]]):
            raise ValueError
        for case, expected in zip(report.tests, suite["tests"]):
            actual = [step.action for step in case.steps]
            if actual != expected["actions"][:len(actual)] or (case.outcome == "PASS" and actual != expected["actions"]):
                raise ValueError

    @staticmethod
    def _traces(report, manifest_id):
        return [{"format": "browser-trace-v1", "executionManifestId": str(manifest_id), "suiteName": report.suite_name,
                 **case.to_dict()} for case in report.tests]

    @staticmethod
    def _result_metadata(binding, source, result, manifest_id, step_id, profile, suite, inputs, host_policy, report):
        try:
            BrowserTestOutputStore._binding(binding)
            if type(result) is not SandboxResult or not isinstance(source, CodeSnapshotArtifact) or type(report) is not BrowserTestReport:
                raise ValueError
            manifest_id, execution_id, step_id = _uuid(manifest_id), _uuid(result.execution_id), _uuid(step_id)
            if manifest_id == execution_id:
                raise BrowserStoreError("BROWSER_TEST_RECORD_CONFLICT")
            profile = BrowserTestOutputStore._profile(profile)
            BrowserTestOutputStore._verify_host_policy(host_policy, profile, suite)
            host = host_policy["runner"]
            BrowserTestOutputStore._verify_inputs_payload(inputs, suite, host)
            manifest = ExecutionManifest.model_validate(result.execution_manifest.model_dump(mode="json", by_alias=True))
            reference = profile.image_reference
            image_matches = (result.image_id == manifest.container_image_digest if reference is None else
                reference == manifest.container_image_digest == result.image_id if reference.startswith("sha256:") else
                reference.rsplit("@", 1)[-1] == manifest.container_image_digest)
            if (result.run_id != binding.run_id or result.source_artifact_id != source.artifact_id
                    or source.run_id != binding.run_id or result.tool_name != "run_browser_tests"
                    or manifest != source.execution_manifest() or result.profile_name != suite["name"]
                    or not image_matches or type(result.image_id) is not str
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", result.image_id) is None
                    or type(result.container_id) is not str or re.fullmatch(r"[0-9a-f]{64}", result.container_id) is None
                    or type(result.duration_ms) is not int or not 0 <= result.duration_ms <= 2**63 - 1):
                raise ValueError
            parsed = parse_browser_report(result.stdout, result.exit_code)
            if parsed != report:
                raise ValueError
            BrowserTestOutputStore._complete(parsed, suite, host)
            report_bytes = _json(parsed.to_dict()).encode("utf-8")
            trace_bytes = _json(BrowserTestOutputStore._traces(parsed, manifest_id)).encode("utf-8")
            stderr = _output(result.stderr)
            if (len(result.stdout.encode("utf-8")) > min(MAX_BROWSER_OUTPUT_BYTES, profile.limits.max_stdout_bytes)
                    or len(report_bytes) > min(_MAX_REPORT_BYTES, profile.limits.max_stdout_bytes)
                    or len(result.stderr.encode("utf-8")) > profile.limits.max_stderr_bytes
                    or len(stderr) > profile.limits.max_stderr_bytes or len(trace_bytes) > _MAX_REPORT_BYTES):
                raise BrowserStoreError("BROWSER_TEST_OUTPUT_LIMIT")
            metadata = {
                "executionManifestId": str(manifest_id), "executionId": str(execution_id), "runId": str(binding.run_id),
                "workspaceId": str(binding.workspace_id), "workflowStepId": str(step_id), "sourceArtifactId": str(source.artifact_id),
                "executionManifest": manifest.model_dump(mode="json", by_alias=True), "role": "QA",
                "executionProfile": {"name": profile.name, "toolName": profile.tool_name, "argv": list(profile.argv),
                    "limits": asdict(profile.limits), "imageReference": profile.image_reference},
                "profileName": result.profile_name, "toolName": result.tool_name, "imageId": result.image_id,
                "containerId": result.container_id, "exitCode": result.exit_code, "durationMs": result.duration_ms,
                "suite": suite, "inputs": inputs, "hostConfiguration": host_policy,
                "hostPolicySha256": sha256(_json(host_policy).encode("utf-8")).hexdigest(),
            }
            for name, content in (("report", report_bytes), ("stdout", report_bytes), ("stderr", stderr), ("traces", trace_bytes)):
                metadata[name + "Sha256"] = sha256(content).hexdigest()
                metadata[name + "SizeBytes"] = len(content)
            raw = _json(metadata)
            if len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
                raise ValueError
            return metadata, raw, report_bytes, trace_bytes, report_bytes, stderr
        except BrowserStoreError:
            raise
        except Exception:
            raise BrowserStoreError("BROWSER_TEST_RESULT_INVALID") from None

    @staticmethod
    def _collision(connection, identities):
        for identity in identities:
            for query in (
                "SELECT 1 FROM artifact_contents WHERE artifact_id=?",
                "SELECT 1 FROM project_artifacts WHERE artifact_id=?",
                "SELECT 1 FROM run_configurations WHERE COALESCE(json_extract(payload_json,'$.artifact_id'),json_extract(payload_json,'$.artifactId'))=?",
            ):
                if connection.execute(query, (identity,)).fetchone() is not None:
                    raise BrowserStoreError("BROWSER_TEST_RECORD_CONFLICT")
            for name in ("browser_test_execution_records", "unit_test_execution_records", "build_execution_records", "security_scan_execution_records"):
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None:
                    if connection.execute(f"SELECT 1 FROM {name} WHERE execution_manifest_id=? OR execution_id=?", (identity, identity)).fetchone() is not None:
                        raise BrowserStoreError("BROWSER_TEST_RECORD_CONFLICT")
            for table, column in (("tool_execution_calls", "logical_call_id"),
                                  ("tool_execution_attempt_starts", "attempt_id")):
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None:
                    if connection.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (identity,)).fetchone() is not None:
                        raise BrowserStoreError("BROWSER_TEST_RECORD_CONFLICT")

    def publish(self, binding, source, result, *, profile, suite, inputs, report, configuration):
        try:
            self._binding(binding)
            try:
                suite = _copy_suite(suite)
                configuration = _copy_configuration(configuration)
                host_policy = self._host_policy(configuration, suite)
                inputs_payload, inventory = self._inputs_payload(inputs, suite, configuration)
            except Exception:
                raise BrowserStoreError("BROWSER_TEST_RESULT_INVALID") from None
            manifest_id = _uuid(uuid4())
            with self._repository._transaction() as connection:
                step, _stored = self._context(connection, binding, source, inventory)
                metadata, raw, report_bytes, traces, stdout, stderr = self._result_metadata(
                    binding, source, result, manifest_id, step.workflow_step_id, profile, inventory, inputs_payload, host_policy, report)
                self._ensure_schema(connection)
                self._collision(connection, (str(manifest_id), metadata["executionId"]))
                connection.execute("""INSERT INTO browser_test_execution_records (
                    execution_manifest_id,execution_id,run_id,workspace_id,workflow_step_id,source_artifact_id,
                    metadata_json,metadata_sha256,report,traces,stdout,stderr) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (str(manifest_id), metadata["executionId"], str(binding.run_id), str(binding.workspace_id),
                     str(step.workflow_step_id), str(source.artifact_id), raw, sha256(raw.encode("utf-8")).hexdigest(),
                     sqlite3.Binary(report_bytes), sqlite3.Binary(traces), sqlite3.Binary(stdout), sqlite3.Binary(stderr)))
                row = connection.execute("SELECT * FROM browser_test_execution_records WHERE execution_manifest_id=?", (str(manifest_id),)).fetchone()
                return self._decode(connection, row)
        except BrowserStoreError:
            raise
        except sqlite3.IntegrityError:
            raise BrowserStoreError("BROWSER_TEST_RECORD_CONFLICT") from None
        except Exception:
            raise BrowserStoreError("BROWSER_TEST_STORAGE_ERROR") from None

    @staticmethod
    def _decode(connection, row):
        try:
            raw = row["metadata_json"]
            if type(raw) is not str or len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
                raise ValueError
            metadata = json.loads(raw)
            if (type(metadata) is not dict or set(metadata) != _METADATA_KEYS or _json(metadata) != raw
                    or sha256(raw.encode("utf-8")).hexdigest() != row["metadata_sha256"]):
                raise ValueError
            ids = {name: _uuid(metadata[name]) for name in ("executionManifestId", "executionId", "runId", "workspaceId", "workflowStepId", "sourceArtifactId")}
            for name, column in (("executionManifestId", "execution_manifest_id"), ("executionId", "execution_id"), ("runId", "run_id"),
                                 ("workspaceId", "workspace_id"), ("workflowStepId", "workflow_step_id"), ("sourceArtifactId", "source_artifact_id")):
                if metadata[name] != row[column] or str(ids[name]) != row[column]:
                    raise ValueError
            if metadata["role"] != "QA":
                raise ValueError
            binding = MCPBinding(role=AgentRole.QA, agent_role=AgentRole.QA, run_id=ids["runId"], workspace_id=ids["workspaceId"])
            suite, host = metadata["suite"], metadata["hostConfiguration"]
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
            BrowserTestOutputStore._source(connection, binding, source)
            BrowserTestOutputStore._configuration(connection, binding, source, suite)
            workspace_row = connection.execute("SELECT run_id,payload_json FROM workspaces WHERE workspace_id=?", (row["workspace_id"],)).fetchone()
            step_row = connection.execute("SELECT run_id,payload_json FROM workflow_steps WHERE workflow_step_id=?", (row["workflow_step_id"],)).fetchone()
            if workspace_row is None or step_row is None:
                raise ValueError
            workspace = WorkspaceRecord.model_validate_json(workspace_row["payload_json"])
            step = WorkflowStep.model_validate_json(step_row["payload_json"])
            if (workspace_row["run_id"] != row["run_id"] or str(workspace.workspace_id) != row["workspace_id"]
                    or str(workspace.run_id) != row["run_id"] or step_row["run_id"] != row["run_id"]
                    or str(step.workflow_step_id) != row["workflow_step_id"] or str(step.run_id) != row["run_id"]
                    or step.agent_role is not AgentRole.QA
                    or not step.requirement_ids or not set(step.requirement_ids) <= set(source.requirement_ids)
                    or source.artifact_id not in step.input_artifact_ids
                    or step.code_version is not None and step.code_version != source.code_version):
                raise ValueError
            streams = {}
            for name in ("report", "traces", "stdout", "stderr"):
                content = row[name]
                maximum = _MAX_REPORT_BYTES if name in {"report", "traces"} else MAX_BROWSER_OUTPUT_BYTES
                if (type(content) is not bytes or len(content) > maximum
                        or type(metadata[name + "SizeBytes"]) is not int or metadata[name + "SizeBytes"] != len(content)
                        or metadata[name + "Sha256"] != sha256(content).hexdigest()):
                    raise ValueError
                streams[name] = content.decode("utf-8")
            report = parse_browser_report(streams["report"], metadata["exitCode"])
            if (_json(report.to_dict()) != streams["report"] or streams["stdout"] != streams["report"]
                    or _json(BrowserTestOutputStore._traces(report, ids["executionManifestId"])) != streams["traces"]
                    or redact_text(streams["stderr"]) != streams["stderr"]
                    or metadata["hostPolicySha256"] != sha256(_json(host).encode("utf-8")).hexdigest()):
                raise ValueError
            result = SandboxResult(execution_id=ids["executionId"], run_id=ids["runId"], source_artifact_id=ids["sourceArtifactId"],
                profile_name=metadata["profileName"], tool_name=metadata["toolName"], execution_manifest=ExecutionManifest.model_validate(metadata["executionManifest"]),
                image_id=metadata["imageId"], container_id=metadata["containerId"], exit_code=metadata["exitCode"], duration_ms=metadata["durationMs"],
                stdout=streams["stdout"], stderr=streams["stderr"])
            _metadata, expected, *_contents = BrowserTestOutputStore._result_metadata(
                binding, source, result, ids["executionManifestId"], ids["workflowStepId"], profile, suite, metadata["inputs"], host, report)
            if raw != expected:
                raise ValueError
            return BrowserTestExecutionRecord(execution_manifest_id=ids["executionManifestId"], execution_id=ids["executionId"],
                run_id=ids["runId"], workspace_id=ids["workspaceId"], workflow_step_id=ids["workflowStepId"], source_artifact_id=ids["sourceArtifactId"],
                execution_manifest=result.execution_manifest, execution_profile=profile, role=AgentRole.QA, profile_name=result.profile_name,
                suite=_freeze(suite), inputs=_freeze(metadata["inputs"]), host_configuration=_freeze(host), report=report,
                image_id=result.image_id, container_id=result.container_id, exit_code=result.exit_code, duration_ms=result.duration_ms,
                stdout=streams["stdout"], stderr=streams["stderr"], stdout_sha256=metadata["stdoutSha256"], stderr_sha256=metadata["stderrSha256"],
                report_sha256=metadata["reportSha256"], traces_sha256=metadata["tracesSha256"], metadata_sha256=row["metadata_sha256"])
        except Exception:
            raise BrowserStoreError("BROWSER_TEST_RESULT_INTEGRITY_ERROR") from None

    @staticmethod
    def _get(connection, run_id, manifest_id):
        SQLiteArtifactContentStore._ensure_schema(connection)
        BrowserTestOutputStore._ensure_schema(connection)
        row = connection.execute("SELECT * FROM browser_test_execution_records WHERE run_id=? AND execution_manifest_id=?", (str(run_id), str(manifest_id))).fetchone()
        if row is None:
            raise BrowserStoreError("BROWSER_TEST_RECORD_NOT_FOUND")
        return BrowserTestOutputStore._decode(connection, row)

    def get(self, run_id, execution_manifest_id):
        run_id, manifest_id = _uuid(run_id), _uuid(execution_manifest_id)
        try:
            with self._repository._transaction() as connection:
                return self._get(connection, run_id, manifest_id)
        except BrowserStoreError:
            raise
        except Exception:
            raise BrowserStoreError("BROWSER_TEST_STORAGE_ERROR") from None

    def _read(self, binding, reference, *, trace):
        self._binding(binding)
        if type(reference) is not str:
            raise BrowserStoreError("BROWSER_TEST_RESULT_INVALID")
        pattern = r"artifact://([0-9a-f-]{36})/browser-trace-([0-9]{4})\.json" if trace else r"artifact://([0-9a-f-]{36})/browser-test-report\.json"
        match = re.fullmatch(pattern, reference)
        if match is None:
            raise BrowserStoreError("BROWSER_TEST_RESULT_INVALID")
        manifest_id = _uuid(match[1])
        if str(manifest_id) != match[1]:
            raise BrowserStoreError("BROWSER_TEST_RESULT_INVALID")
        try:
            with self._repository._transaction() as connection:
                record = self._get(connection, binding.run_id, manifest_id)
                if record.workspace_id != binding.workspace_id:
                    raise BrowserStoreError("BROWSER_TEST_CONTEXT_DENIED")
                if not trace:
                    return record.report.to_dict()
                index = int(match[2])
                if index >= record.report.total:
                    raise BrowserStoreError("BROWSER_TEST_RECORD_NOT_FOUND")
                return self._traces(record.report, manifest_id)[index]
        except BrowserStoreError:
            raise
        except Exception:
            raise BrowserStoreError("BROWSER_TEST_STORAGE_ERROR") from None

    def read_report(self, binding, report_ref):
        return self._read(binding, report_ref, trace=False)

    def read_trace(self, binding, trace_ref):
        return self._read(binding, trace_ref, trace=True)
