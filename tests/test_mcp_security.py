"""Actual MCP/Git/SQLite boundaries with Fake Docker; no Host scanner/code."""

import asyncio
from contextlib import redirect_stderr
from dataclasses import replace
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_mcp_security_store as store_fixture
import test_sandbox_runtime as sandbox_fixture
from mcp_tools.client import MCPChildConfiguration, MCPClientError, child_parameters, open_mcp_client
from mcp_tools.core.catalog import get_tool_contract
from mcp_tools.runtime import MCPBinding, MCPConfigurationError, MCPDispatcher, MCPProtocolError
from mcp_tools.tools.build_store import BuildOutputStore, BuildStoreError
from mcp_tools.tools.browser import BrowserTestTools
from mcp_tools.tools.browser_config import BrowserTestConfiguration, BrowserTestSuite
from mcp_tools.tools.browser_store import BrowserTestOutputStore
from mcp_tools.tools.security import SecurityScanTools
from mcp_tools.tools.security_config import decode_security_configuration
from mcp_tools.tools.security_store import SecurityScanOutputStore, SecurityStoreError
from mcp_tools.tools.unit import UnitTestTools
from mcp_tools.tools.unit_config import UnitTestConfiguration
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.sandbox.contracts import CLIResult, ExecutionProfile, SandboxError, SandboxErrorCode
from orchestrator.sandbox.runtime import SandboxRuntime


class SecurityToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = store_fixture.SecurityScanOutputStoreTests("runTest")
        self.fixture.setUp()
        for cleanup, args, kwargs in self.fixture._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        self.fixture._cleanups.clear()
        self.run = self.fixture.run
        self.artifacts = ArtifactStore(self.fixture.repository, self.fixture.registry)
        self.docker = sandbox_fixture.FakeDocker()
        self.sandbox = SandboxRuntime(self.fixture.repository, self.fixture.registry, self.artifacts, docker=self.docker)
        self.outputs = SecurityScanOutputStore(self.fixture.repository)
        self.configuration = self.fixture.configuration
        self.scanner = self.fixture.scanner_profile
        self.report = self.fixture.report.to_dict()
        self.set_report(findings=False)

    def set_report(self, *, findings=False):
        data = self.fixture.report.to_dict()
        data["findings"] = [self.fixture.finding()] if findings else []
        self.report = data
        self.docker.exit_code = int(bool(data["findings"]))
        self.docker.start_result = CLIResult(returncode=self.docker.exit_code,
                                            stdout=json.dumps(data).encode(), stderr=b"")

    def binding(self, role=AgentRole.SECURITY):
        return MCPBinding(role=role, agent_role=role, run_id=self.run.run_id, workspace_id=self.run.workspace_id)

    def tools(self, *, configuration=True, max_call_seconds=60):
        return SecurityScanTools(self.artifacts, self.sandbox, self.outputs,
            configuration=self.configuration if configuration else None, max_call_seconds=max_call_seconds)

    def dispatcher(self, role=AgentRole.SECURITY, **options):
        return MCPDispatcher(self.binding(role), self.fixture.registry, handlers=self.tools(**options).handlers(role),
                             max_call_seconds=options.get("max_call_seconds", 60))

    def arguments(self, **extra):
        return {"workspaceId": str(self.run.workspace_id), "snapshotId": str(self.fixture.source.artifact_id),
                "scannerProfile": self.scanner.name, **extra}

    def assert_no_receipts(self):
        with self.fixture.repository._connection() as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='security_scan_execution_records'").fetchone():
                self.assertEqual(connection.execute("SELECT count(*) FROM security_scan_execution_records").fetchone()[0], 0)

    def mutate_run(self, **changes):
        current = self.fixture.repository.get_run(self.run.run_id)
        updated = type(current).model_validate({**current.model_dump(), **changes})
        with self.fixture.repository._transaction() as connection:
            connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                               (updated.status.value, updated.model_dump_json(), str(updated.run_id)))

    def mutate_step(self, step, **changes):
        updated = type(step).model_validate({**step.model_dump(), **changes})
        with self.fixture.repository._transaction() as connection:
            connection.execute("UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                               (updated.status.value, updated.model_dump_json(), str(updated.workflow_step_id)))

    async def test_fixed_snapshot_scan_returns_exact_schema_and_private_report(self):
        before = self.fixture.repository.get_run(self.run.run_id)
        result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        self.assertIsNone(result.error_code)
        get_tool_contract("run_security_scan").validate_output(result.data)
        self.assertEqual(set(result.data), {"findings", "reportRef", "executionManifestId"})
        record = self.outputs.get(self.run.run_id, result.data["executionManifestId"])
        self.assertEqual(record.tool_output(), result.data)
        self.assertEqual(record.execution_manifest, self.fixture.source.execution_manifest())
        self.assertEqual(record.execution_profile.limits.timeout_seconds, 39)
        self.assertEqual(result.data["findings"], [])
        self.assertEqual(self.fixture.repository.get_run(self.run.run_id), before)
        self.assertEqual(self.fixture.repository.list_project_artifacts(self.run.run_id), [])
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assertFalse(Path(self.docker.container["Mounts"][0]["Source"]).exists())

    async def test_finding_is_successful_tool_with_suspected_not_confirmed_verdict(self):
        self.set_report(findings=True)
        self.assertTrue(self.report["findings"], "store fixture must include one suspected finding")
        result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        self.assertIsNone(result.error_code)
        self.assertEqual(result.data["findings"][0]["status"], "SUSPECTED")
        self.assertIsNone(self.fixture.repository.get_run(self.run.run_id).verdict)

    async def test_missing_config_unknown_profile_and_too_short_budget_do_not_execute(self):
        for options, args, code in (({"configuration": False}, self.arguments(), "PROFILE_NOT_FOUND"),
                                   ({}, self.arguments(scannerProfile="unknown"), "PROFILE_NOT_FOUND"),
                                   ({"max_call_seconds": 10}, self.arguments(), "TIMEOUT")):
            result = await self.dispatcher(**options).call_tool("run_security_scan", args)
            self.assertEqual(result.error_code, code)
        self.assertEqual(self.docker.calls, [])

    async def test_only_security_can_scan_or_read_report(self):
        for role in (AgentRole.PLANNER, AgentRole.DEVELOPER, AgentRole.QA):
            self.assertEqual(dict(self.tools().handlers(role)), {})
            for name, args in (("run_security_scan", self.arguments()),
                               ("read_security_report", {"workspaceId": str(self.run.workspace_id),
                                "reportRef": f"artifact://{uuid4()}/security-scan-report.json"})):
                with self.subTest(role=role, name=name), self.assertRaises(MCPProtocolError):
                    await self.dispatcher(role).call_tool(name, args)
        self.assertEqual(self.docker.calls, [])

    async def test_model_cannot_choose_rule_command_version_scope_image_or_host_path(self):
        for field in ("command", "argv", "image", "role", "hostPath", "timeout", "ruleIds", "exclude",
                      "ignoreNosec", "scannerVersion", "configFile", "baseline"):
            with self.subTest(field=field), self.assertRaises(MCPProtocolError):
                await self.dispatcher().call_tool("run_security_scan", self.arguments(**{field: "untrusted"}))
        self.assertEqual(self.docker.calls, [])

    async def test_source_workspace_and_current_security_step_checked_before_docker(self):
        for args in (self.arguments(workspaceId=str(uuid4())), self.arguments(snapshotId=str(uuid4()))):
            result = await self.dispatcher().call_tool("run_security_scan", args)
            self.assertIsNotNone(result.error_code)
        self.mutate_run(status=WorkflowStatus.IMPLEMENTING)
        result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        self.assertEqual(result.error_code, "PERMISSION_DENIED")
        self.assertEqual(self.docker.calls, [])

    async def test_profile_ref_mismatch_cannot_start_or_publish(self):
        scanner = replace(self.scanner, profile_ref="https://scanner.example.invalid/wrong/v1")
        self.configuration = replace(self.configuration, profiles=(scanner,))
        result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        self.assertEqual(result.error_code, "PERMISSION_DENIED")
        self.assertEqual(self.docker.calls, [])
        self.assert_no_receipts()

    async def test_unsupported_or_private_source_inventory_cannot_start_container(self):
        with patch.object(self.outputs, "source_inventory", side_effect=SecurityStoreError("SECURITY_SCAN_RESULT_INTEGRITY_ERROR")):
            result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        self.assertEqual(result.error_code, "SCANNER_ERROR")
        self.assertEqual(self.docker.calls, [])
        self.assert_no_receipts()

    async def scan_record(self):
        result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        self.assertIsNone(result.error_code)
        return self.outputs.get(self.run.run_id, result.data["executionManifestId"])

    def qa_context(self):
        self.mutate_step(self.fixture.step, status=WorkflowStepStatus.SUCCEEDED)
        unit_fixture = self.fixture.fixture
        unit_fixture.qa_context()
        return unit_fixture

    async def test_build_cannot_reuse_security_receipt_or_execution_id(self):
        record = await self.scan_record()
        self.mutate_step(self.fixture.step, status=WorkflowStepStatus.SUCCEEDED)
        self.mutate_step(self.fixture.developer, status=WorkflowStepStatus.RUNNING)
        self.mutate_run(status=WorkflowStatus.IMPLEMENTING)
        profile = ExecutionProfile(name="fixture-build", tool_name="run_build",
                                   argv=("/usr/local/bin/python", "-B", "/snapshot/main.py"))
        result = replace(self.fixture.result, profile_name=profile.name, tool_name=profile.tool_name,
                         execution_id=uuid4(), stdout="build\n", stderr="", exit_code=0)
        for identity in (record.execution_manifest_id, record.execution_id):
            with patch("mcp_tools.tools.build_store.uuid4", return_value=identity):
                with self.assertRaises(BuildStoreError) as raised:
                    BuildOutputStore(self.fixture.repository).publish(self.binding(AgentRole.DEVELOPER),
                        self.fixture.source, result, profile=profile)
            self.assertEqual(raised.exception.code, "BUILD_RECORD_CONFLICT")

    async def test_unit_cannot_reuse_security_receipt_or_execution_id(self):
        record = await self.scan_record()
        fixture = self.qa_context()
        tests = self.fixture.root / "outputs/qa/tests"
        tests.mkdir(parents=True, exist_ok=True)
        (tests / "test_signup.py").write_bytes(b"# captured QA tests\n")
        self.docker.exit_code = 0
        self.docker.start_result = CLIResult(returncode=0, stdout=fixture.report_json().encode(), stderr=b"")
        tools = UnitTestTools(self.artifacts, self.sandbox, fixture.store,
                             configuration=UnitTestConfiguration(scopes=(fixture.scope,)))
        dispatcher = MCPDispatcher(self.binding(AgentRole.QA), self.fixture.registry, handlers=tools.handlers(AgentRole.QA))
        for identity in (record.execution_manifest_id, record.execution_id):
            with patch("mcp_tools.tools.unit_store.uuid4", return_value=identity):
                result = await dispatcher.call_tool("run_unit_tests", {
                    "workspaceId": str(self.run.workspace_id), "snapshotId": str(self.fixture.source.artifact_id),
                    "testScope": fixture.scope.name})
            self.assertEqual(result.error_code, "TEST_RUNNER_ERROR")

    async def test_browser_cannot_reuse_security_receipt_or_execution_id(self):
        record = await self.scan_record()
        self.qa_context()
        tests = self.fixture.root / "outputs/qa/tests/browser"
        tests.mkdir(parents=True, exist_ok=True)
        (tests / "suite.json").write_text(json.dumps({"format": "browser-suite-v1", "tests": [
            {"testId": "signup.visible", "steps": [{"action": "goto", "path": "/"},
            {"action": "assert_visible", "selector": "#signup"}]}]}), encoding="utf-8")
        suite = BrowserTestSuite(name="signup-browser", kind="QA_TESTS")
        configuration = BrowserTestConfiguration(suites=(suite,), playwright_version="1.60.0",
            service_argv=("/usr/local/bin/python", "-B", "/snapshot/main.py"), image_reference=sandbox_fixture.IMAGE_ID)
        report = {"format": "browser-v1", "suiteName": suite.name, "playwrightVersion": "1.60.0",
            "browserVersion": "140.0.7339.0", "total": 1, "passed": 1, "failed": 0, "tests": [
                {"testId": "signup.visible", "outcome": "PASS", "steps": [
                    {"index": 1, "action": "goto", "outcome": "PASS", "durationMs": 2},
                    {"index": 2, "action": "assert_visible", "outcome": "PASS", "durationMs": 2}]}]}
        self.docker.exit_code = 0
        self.docker.start_result = CLIResult(returncode=0, stdout=json.dumps(report).encode(), stderr=b"")
        tools = BrowserTestTools(self.artifacts, self.sandbox, BrowserTestOutputStore(self.fixture.repository), configuration=configuration)
        dispatcher = MCPDispatcher(self.binding(AgentRole.QA), self.fixture.registry, handlers=tools.handlers(AgentRole.QA))
        for identity in (record.execution_manifest_id, record.execution_id):
            with patch("mcp_tools.tools.browser_store.uuid4", return_value=identity):
                result = await dispatcher.call_tool("run_browser_tests", {
                    "workspaceId": str(self.run.workspace_id), "snapshotId": str(self.fixture.source.artifact_id), "testSuite": suite.name})
            self.assertEqual(result.error_code, "TEST_RUNNER_ERROR")

    async def test_frozen_source_and_trusted_assets_readonly_not_working_source(self):
        (self.fixture.root / "source/main.py").write_text("# live mutable\n", encoding="utf-8")
        def inspect(container):
            mounts = {item["Destination"]: item for item in container["Mounts"]}
            self.assertFalse(mounts["/snapshot"]["RW"])
            self.assertFalse(mounts["/inputs"]["RW"])
            self.assertNotIn("live mutable", (Path(mounts["/snapshot"]["Source"]) / "main.py").read_text())
            inputs = Path(mounts["/inputs"]["Source"])
            self.assertEqual(sorted(path.name for path in inputs.iterdir()),
                             ["_security_contract.py", "_security_host.json", "_security_runner.py"])
            host = json.loads((inputs / "_security_host.json").read_text())
            self.assertTrue(host["ignore_nosec"])
            self.assertEqual(host["scan_scope"], "ALL_PYTHON")
            self.assertEqual(container["HostConfig"]["NetworkMode"], "none")
            self.assertEqual(container["Args"], ["-I", "-B", "/inputs/_security_runner.py"])
        self.docker.start_hook = inspect
        result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        self.assertIsNone(result.error_code)

    async def test_engine_error_invalid_utf8_and_unstructured_stdout_not_published(self):
        for output in (b'{"error":"SCANNER_ERROR"}', b'"0 warnings"', b"\xff", b"{}"):
            self.docker.exit_code = 2
            self.docker.start_result = CLIResult(returncode=2, stdout=output, stderr=b"")
            result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
            self.assertEqual(result.error_code, "SCANNER_ERROR")
            self.assert_no_receipts()

    async def test_wrong_profile_version_rules_inventory_or_raw_fields_not_published(self):
        for key, value in (("profileName", "wrong-profile"), ("scannerVersion", "1.0.0"),
                           ("ruleIds", ["B999"]), ("profileRef", "https://scanner.example.invalid/wrong/v1"),
                           ("scannedFiles", []), ("scannedFiles", ["invented.py"]), ("code", "private source")):
            changed = {**self.report, key: value}
            self.docker.start_result = CLIResult(returncode=0, stdout=json.dumps(changed).encode(), stderr=b"")
            result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
            self.assertEqual(result.error_code, "SCANNER_ERROR")
            self.assert_no_receipts()

    async def test_raw_stderr_is_rejected_not_retained_or_returned(self):
        self.docker.start_result = replace(self.docker.start_result, stderr=b"raw code literal api_key=never-retain\n")
        result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        self.assertEqual(result.error_code, "SCANNER_ERROR")
        self.assertNotIn("never-retain", repr(result))
        self.assert_no_receipts()

    async def test_cancelled_run_during_execution_cannot_publish_after_cleanup(self):
        self.docker.start_hook = lambda _container: self.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="USER_CANCELLED")
        result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        self.assertEqual(result.error_code, "SCANNER_ERROR")
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assert_no_receipts()

    async def test_timeout_oom_storage_cleanup_errors_not_success(self):
        self.docker.start_error = SandboxError(SandboxErrorCode.TIMEOUT)
        result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        self.assertEqual(result.error_code, "TIMEOUT")
        self.docker.start_error = None
        self.docker.after_start_mutation = lambda value: value["State"].update(OOMKilled=True)
        result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        self.assertEqual(result.error_code, "SCANNER_ERROR")
        self.docker.after_start_mutation = None
        with patch.object(self.outputs, "publish", side_effect=SecurityStoreError("private SQL")):
            result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        self.assertEqual(result.error_code, "SCANNER_ERROR")
        self.docker.rm_result = CLIResult(returncode=1, stdout=b"", stderr=b"private daemon")
        result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        self.assertEqual(result.error_code, "SCANNER_ERROR")
        self.assert_no_receipts()

    async def test_task_cancel_waits_for_owned_cleanup_and_no_receipt(self):
        self.docker.block_start = True
        task = asyncio.create_task(self.dispatcher().call_tool("run_security_scan", self.arguments()))
        await asyncio.wait_for(self.docker.start_entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assert_no_receipts()

    async def test_browser_image_env_exception_not_allowed_for_scanner(self):
        self.docker.image[0]["Config"]["Env"] = ["PLAYWRIGHT_BROWSERS_PATH=/ms-playwright"]
        result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        self.assertEqual(result.error_code, "PERMISSION_DENIED")
        self.assertEqual(self.docker.commands("create"), [])

    async def test_report_read_and_wrong_namespace_or_missing_refs(self):
        result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        read = await self.dispatcher().call_tool("read_security_report", {
            "workspaceId": str(self.run.workspace_id), "reportRef": result.data["reportRef"]})
        self.assertIsNone(read.error_code)
        get_tool_contract("read_security_report").validate_output(read.data)
        self.assertEqual(read.data["securityResult"], self.report)
        for ref, code in ((f"artifact://{uuid4()}/security-scan-report.json", "REPORT_NOT_FOUND"),
                          (f"artifact://{uuid4()}/unit-test-report.json", "PATH_DENIED"),
                          (f"artifact://{uuid4()}/browser-test-report.json", "PATH_DENIED"),
                          ("https://example.invalid/report", "PATH_DENIED")):
            response = await self.dispatcher().call_tool("read_security_report", {
                "workspaceId": str(self.run.workspace_id), "reportRef": ref})
            self.assertEqual(response.error_code, code)

    async def test_child_configuration_roundtrip_safe_repr_and_invalid_types(self):
        config = MCPChildConfiguration(binding=self.binding(), database_path=self.fixture.repository.database_path,
            workspace_root=self.fixture.registry.base_path, security_scan_configuration=self.configuration)
        args = child_parameters(config).args
        self.assertEqual(decode_security_configuration(args[args.index("--security-scan-configuration-json") + 1]), self.configuration)
        self.assertNotIn(self.scanner.profile_ref, repr(config))
        with self.assertRaises(MCPClientError):
            replace(config, security_scan_configuration={"profiles": []})

    async def test_constructor_is_inert_and_rejects_invalid_services_and_budget(self):
        with patch.object(self.fixture.repository, "_transaction", side_effect=AssertionError("inert")):
            self.assertEqual(repr(self.tools()), "SecurityScanTools()")
        for budget in (True, 0, float("nan"), 601):
            with self.assertRaises(MCPConfigurationError):
                self.tools(max_call_seconds=budget)
        with self.assertRaises(MCPConfigurationError):
            SecurityScanTools(object(), self.sandbox, self.outputs)

    async def test_real_sdk_stdio_registered_scan_and_missing_configuration_fails_closed(self):
        config = MCPChildConfiguration(binding=self.binding(), database_path=self.fixture.repository.database_path,
                                      workspace_root=self.fixture.registry.base_path)
        async with open_mcp_client(config) as client:
            tools = (await client._client.session.list_tools()).tools
            for name in ("run_security_scan", "read_security_report"):
                self.assertTrue(next(item for item in tools if item.name == name).meta["a2a-agent-company/implemented"])
            result = await client._client.session.call_tool("run_security_scan", self.arguments())
            self.assertTrue(result.is_error)
            self.assertEqual([item.text for item in result.content], ["PROFILE_NOT_FOUND"])
        self.assert_no_receipts()

    async def test_real_sdk_configured_scan_unavailable_docker_never_executes_on_host(self):
        configuration = replace(self.configuration, docker_endpoint="unix://" + str(self.fixture.directory / "absent-docker.sock"))
        config = MCPChildConfiguration(binding=self.binding(), database_path=self.fixture.repository.database_path,
            workspace_root=self.fixture.registry.base_path, security_scan_configuration=configuration)
        async with open_mcp_client(config) as client:
            with self.assertRaises(MCPClientError) as raised:
                await client.call_tool("run_security_scan", self.arguments())
            self.assertEqual(raised.exception.code, "MCP_CLIENT_TOOL_FAILED")
        self.assert_no_receipts()
        self.assertFalse((self.fixture.root / ".sandbox").exists())

    async def test_real_sdk_reads_stored_scan_report(self):
        result = await self.dispatcher().call_tool("run_security_scan", self.arguments())
        config = MCPChildConfiguration(binding=self.binding(), database_path=self.fixture.repository.database_path,
                                      workspace_root=self.fixture.registry.base_path)
        async with open_mcp_client(config) as client:
            response = await client.call_tool("read_security_report", {
                "workspaceId": str(self.run.workspace_id), "reportRef": result.data["reportRef"]})
        self.assertEqual(response["securityResult"], self.report)

    async def test_cli_different_docker_endpoints_rejected_without_starting(self):
        from mcp_tools import __main__ as launcher
        from mcp_tools.tools.unit_config import UnitTestConfiguration, UnitTestScope
        config = MCPChildConfiguration(binding=self.binding(), database_path=self.fixture.repository.database_path,
            workspace_root=self.fixture.registry.base_path, security_scan_configuration=self.configuration,
            unit_test_configuration=UnitTestConfiguration(scopes=(UnitTestScope(name="unit", kind="SNAPSHOT"),),
                docker_endpoint="unix:///tmp/different.sock"))
        args = child_parameters(config).args
        stderr = io.StringIO()
        with redirect_stderr(stderr), patch.object(launcher, "run_stdio", side_effect=AssertionError("no launch")):
            self.assertEqual(launcher.main(args[args.index("--role"):]), 2)
        self.assertEqual(stderr.getvalue(), "MCP_SERVER_FAILED\n")
