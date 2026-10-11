"""Actual MCP/Git/SQLite boundaries with Fake Docker, never Host product code."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_mcp_unit_store as store_fixture
import test_sandbox_runtime as sandbox_fixture
from mcp_tools.client import MCPChildConfiguration, MCPClientError, child_parameters, open_mcp_client
from mcp_tools.core.catalog import get_tool_contract
from mcp_tools.runtime import MCPBinding, MCPConfigurationError, MCPDispatcher, MCPProtocolError
from mcp_tools.tools.build_store import BuildOutputStore, BuildStoreError
from mcp_tools.tools.browser import BrowserTestTools
from mcp_tools.tools.browser_config import BrowserTestConfiguration, BrowserTestSuite, decode_browser_configuration
from mcp_tools.tools.browser_store import BrowserTestOutputStore, BrowserStoreError
from mcp_tools.tools.test_reports import TestReportTools
from mcp_tools.tools.unit import UnitTestTools
from mcp_tools.tools.unit_config import UnitTestConfiguration
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.sandbox.contracts import CLIResult, ExecutionProfile, SandboxError, SandboxErrorCode
from orchestrator.sandbox.runtime import SandboxRuntime


class BrowserToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = store_fixture.UnitTestOutputStoreTests("test_constructor_is_inert")
        self.fixture.setUp()
        for cleanup, args, kwargs in self.fixture._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        self.fixture._cleanups.clear()
        self.fixture.qa_context()
        self.run = self.fixture.run
        self.artifacts = ArtifactStore(self.fixture.repository, self.fixture.registry)
        self.docker = sandbox_fixture.FakeDocker()
        self.sandbox = SandboxRuntime(self.fixture.repository, self.fixture.registry, self.artifacts, docker=self.docker)
        self.outputs = BrowserTestOutputStore(self.fixture.repository)
        self.suite = BrowserTestSuite(name="signup-browser", kind="QA_TESTS")
        self.configuration = BrowserTestConfiguration(suites=(self.suite,),
            service_argv=("/usr/local/bin/python", "-B", "/snapshot/main.py"),
            playwright_version="1.60.0", image_reference=sandbox_fixture.IMAGE_ID)
        tests = self.fixture.root / "outputs/qa/tests/browser"
        tests.mkdir(parents=True, exist_ok=True)
        self.suite_file = tests / "suite.json"
        self.suite_data = {"format": "browser-suite-v1", "tests": [{"testId": "signup.visible", "steps": [
            {"action": "goto", "path": "/"}, {"action": "assert_visible", "selector": "#signup"}]}]}
        self.suite_file.write_text(json.dumps(self.suite_data), encoding="utf-8")
        self.set_report()

    def set_report(self, *, fail=False, details="ASSERTION_FAILED"):
        steps = [{"index": index, "action": step["action"], "outcome": "PASS", "durationMs": 2}
                 for index, step in enumerate(self.suite_data["tests"][0]["steps"], 1)]
        if fail:
            steps[-1]["outcome"] = "FAIL"
        case = {"testId": self.suite_data["tests"][0]["testId"], "outcome": "FAIL" if fail else "PASS", "steps": steps}
        if fail:
            case["details"] = details
        self.report = {"format": "browser-v1", "suiteName": self.suite.name, "playwrightVersion": "1.60.0",
            "browserVersion": "140.0.7339.0", "total": 1, "passed": int(not fail), "failed": int(fail), "tests": [case]}
        self.docker.exit_code = int(fail)
        self.docker.start_result = CLIResult(returncode=int(fail), stdout=json.dumps(self.report).encode(), stderr=b"")

    def binding(self, role=AgentRole.QA):
        return MCPBinding(role=role, agent_role=role, run_id=self.run.run_id, workspace_id=self.run.workspace_id)

    def tools(self, *, configuration=True, max_call_seconds=60):
        return BrowserTestTools(self.artifacts, self.sandbox, self.outputs,
            configuration=self.configuration if configuration else None, max_call_seconds=max_call_seconds)

    def dispatcher(self, role=AgentRole.QA, **options):
        browser = self.tools(**options)
        unit = UnitTestTools(self.artifacts, self.sandbox, self.fixture.store)
        handlers = {**browser.handlers(role), **TestReportTools(unit, browser).handlers(role)}
        return MCPDispatcher(self.binding(role), self.fixture.registry, handlers=handlers,
                             max_call_seconds=options.get("max_call_seconds", 60))

    def arguments(self, **extra):
        return {"workspaceId": str(self.run.workspace_id), "snapshotId": str(self.fixture.source.artifact_id),
                "testSuite": self.suite.name, **extra}

    def assert_no_receipts(self):
        with self.fixture.repository._connection() as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='browser_test_execution_records'").fetchone():
                self.assertEqual(connection.execute("SELECT count(*) FROM browser_test_execution_records").fetchone()[0], 0)

    async def test_fixed_snapshot_handler_returns_exact_schema_and_stored_trace_refs(self):
        before = self.fixture.repository.get_run(self.run.run_id)
        outcome = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertIsNone(outcome.error_code)
        get_tool_contract("run_browser_tests").validate_output(outcome.data)
        self.assertEqual(set(outcome.data), {"total", "passed", "failed", "traceRefs", "executionManifestId"})
        record = self.outputs.get(self.run.run_id, outcome.data["executionManifestId"])
        self.assertEqual(record.tool_output(), outcome.data)
        self.assertEqual(record.execution_manifest, self.fixture.source.execution_manifest())
        self.assertEqual(record.execution_profile.limits.timeout_seconds, 39)
        self.assertEqual((outcome.data["total"], outcome.data["passed"]), (1, 1))
        self.assertEqual(len(outcome.data["traceRefs"]), 1)
        trace = self.outputs.read_trace(self.binding(), outcome.data["traceRefs"][0])
        self.assertIn("assert_visible", json.dumps(trace))
        self.assertEqual(self.fixture.repository.get_run(self.run.run_id), before)
        self.assertEqual(self.fixture.repository.list_project_artifacts(self.run.run_id), [])
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assertFalse(Path(self.docker.container["Mounts"][0]["Source"]).exists())

    async def test_browser_assertion_fail_is_successful_tool_not_project_verdict(self):
        self.set_report(fail=True)
        outcome = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertIsNone(outcome.error_code)
        self.assertEqual((outcome.data["passed"], outcome.data["failed"]), (0, 1))
        self.assertIsNone(self.fixture.repository.get_run(self.run.run_id).verdict)

    async def test_unit_cannot_reuse_browser_receipt_or_execution_id(self):
        outcome = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        record = self.outputs.get(self.run.run_id, outcome.data["executionManifestId"])
        self.docker.start_result = CLIResult(returncode=0, stdout=self.fixture.report_json().encode(), stderr=b"")
        unit = UnitTestTools(self.artifacts, self.sandbox, self.fixture.store,
            configuration=UnitTestConfiguration(scopes=(self.fixture.scope,)))
        dispatcher = MCPDispatcher(self.binding(), self.fixture.registry, handlers=unit.handlers(AgentRole.QA))
        for identity in (record.execution_manifest_id, record.execution_id):
            with patch("mcp_tools.tools.unit_store.uuid4", return_value=identity):
                result = await dispatcher.call_tool("run_unit_tests", {
                    "workspaceId": str(self.run.workspace_id), "snapshotId": str(self.fixture.source.artifact_id),
                    "testScope": self.fixture.scope.name})
            self.assertEqual(result.error_code, "TEST_RUNNER_ERROR")

    async def test_build_cannot_reuse_browser_receipt_or_execution_id(self):
        outcome = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        record = self.outputs.get(self.run.run_id, outcome.data["executionManifestId"])
        for step in self.fixture.repository.list_steps(self.run.run_id):
            self.fixture.mutate_step(step, status=(WorkflowStepStatus.RUNNING if step.agent_role is AgentRole.DEVELOPER
                                                  else WorkflowStepStatus.SUCCEEDED))
        self.fixture.mutate_run(status=WorkflowStatus.IMPLEMENTING)
        profile = ExecutionProfile(name="fixture-build", tool_name="run_build", argv=("/usr/local/bin/python", "-B", "/snapshot/main.py"))
        result = replace(self.fixture.result, profile_name=profile.name, tool_name=profile.tool_name, execution_id=uuid4(), stdout="build\n")
        for identity in (record.execution_manifest_id, record.execution_id):
            with patch("mcp_tools.tools.build_store.uuid4", return_value=identity):
                with self.assertRaises(BuildStoreError) as raised:
                    BuildOutputStore(self.fixture.repository).publish(self.binding(AgentRole.DEVELOPER), self.fixture.source, result, profile=profile)
            self.assertEqual(raised.exception.code, "BUILD_RECORD_CONFLICT")

    async def test_missing_dependency_start_error_is_distinct_from_runner_error(self):
        for code, exit_code in (("BROWSER_START_FAILED", 3), ("TEST_RUNNER_ERROR", 2)):
            with self.subTest(code=code):
                self.docker.exit_code = exit_code
                self.docker.start_result = CLIResult(returncode=exit_code, stdout=json.dumps({"error": code}).encode(), stderr=b"")
                outcome = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
                self.assertEqual(outcome.error_code, code)
                self.assert_no_receipts()
        self.assertEqual(len(self.docker.commands("rm")), 2)

    async def test_missing_host_config_unknown_suite_and_too_short_deadline_do_not_execute(self):
        for options, args, code in (({"configuration": False}, self.arguments(), "BROWSER_START_FAILED"),
                                   ({}, self.arguments(testSuite="unknown"), "TEST_RUNNER_ERROR"),
                                   ({"max_call_seconds": 10}, self.arguments(), "TIMEOUT")):
            result = await self.dispatcher(**options).call_tool("run_browser_tests", args)
            self.assertEqual(result.error_code, code)
        self.assertEqual(self.docker.calls, [])

    async def test_model_cannot_choose_url_command_browser_image_or_host_files(self):
        for field in ("baseUrl", "command", "argv", "image", "role", "hostPath", "headless", "browserExecutable", "timeout"):
            with self.subTest(field=field), self.assertRaises(MCPProtocolError):
                await self.dispatcher().call_tool("run_browser_tests", self.arguments(**{field: "untrusted"}))
        self.assertEqual(self.docker.calls, [])

    async def test_only_qa_has_browser_handler_and_direct_permission(self):
        for role in (AgentRole.PLANNER, AgentRole.DEVELOPER, AgentRole.SECURITY):
            self.assertEqual(dict(self.tools().handlers(role)), {})
            with self.assertRaises(MCPProtocolError):
                await self.dispatcher(role).call_tool("run_browser_tests", self.arguments())
        self.assertEqual(self.docker.calls, [])

    async def test_source_version_workspace_and_current_qa_step_are_checked_before_docker(self):
        for args in (self.arguments(workspaceId=str(uuid4())), self.arguments(snapshotId=str(uuid4()))):
            result = await self.dispatcher().call_tool("run_browser_tests", args)
            self.assertIsNotNone(result.error_code)
        self.fixture.mutate_run(status=WorkflowStatus.IMPLEMENTING)
        result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertEqual(result.error_code, "PERMISSION_DENIED")
        self.assertEqual(self.docker.calls, [])

    async def test_missing_malformed_and_zero_case_suite_fail_before_container(self):
        self.suite_file.unlink()
        result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertEqual(result.error_code, "FILE_NOT_FOUND")
        for text in ("not json", '{"format":"browser-suite-v1","tests":[]}',
                     json.dumps({**self.suite_data, "command": "untrusted"})):
            self.suite_file.write_text(text, encoding="utf-8")
            result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
            self.assertEqual(result.error_code, "TEST_RUNNER_ERROR")
        self.assertEqual(self.docker.calls, [])

    async def test_frozen_source_and_qa_suite_are_readonly_separate_from_working_copy(self):
        (self.fixture.root / "source/main.py").write_text("# live mutable\n", encoding="utf-8")
        def inspect(container):
            mounts = {item["Destination"]: item for item in container["Mounts"]}
            self.assertFalse(mounts["/snapshot"]["RW"])
            self.assertFalse(mounts["/inputs"]["RW"])
            self.assertIn("Do not execute", (Path(mounts["/snapshot"]["Source"]) / "main.py").read_text())
            inputs = Path(mounts["/inputs"]["Source"])
            self.assertEqual(json.loads((inputs / "tests/browser/suite.json").read_text()), self.suite_data)
            self.suite_file.write_text("{}", encoding="utf-8")
            self.assertEqual(json.loads((inputs / "tests/browser/suite.json").read_text()), self.suite_data)
            self.assertTrue((inputs / "_browser_runner.py").is_file())
            self.assertTrue((inputs / "_browser_contract.py").is_file())
            self.assertEqual(container["HostConfig"]["NetworkMode"], "none")
            self.assertNotIn("/snapshot/main.py", container["Args"])
        self.docker.start_hook = inspect
        outcome = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertIsNone(outcome.error_code)

    async def test_host_protected_suite_not_qa_scratch_and_ref_must_match_frozen_configuration(self):
        suite = BrowserTestSuite(name=self.suite.name, kind="PROTECTED", protected_files={
            self.suite.suite_path: json.dumps(self.suite_data)},
            protected_suite_ref=self.fixture.configuration.configuration.protected_test_suite_ref)
        self.configuration = replace(self.configuration, suites=(suite,))
        self.suite_file.write_text("{}", encoding="utf-8")
        outcome = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertIsNone(outcome.error_code)
        suite = replace(suite, protected_suite_ref="https://criteria.example.invalid/different/v1")
        self.configuration = replace(self.configuration, suites=(suite,))
        calls = len(self.docker.calls)
        outcome = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertEqual(outcome.error_code, "PERMISSION_DENIED")
        self.assertEqual(len(self.docker.calls), calls)

    async def test_suite_case_mismatch_or_incomplete_pass_is_not_published(self):
        for field, value in (("suiteName", "wrong-suite"), ("playwrightVersion", "1.59.0"), ("total", 0)):
            changed = {**self.report, field: value}
            self.docker.start_result = CLIResult(returncode=0, stdout=json.dumps(changed).encode(), stderr=b"")
            result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
            self.assertEqual(result.error_code, "TEST_RUNNER_ERROR")
            self.assert_no_receipts()
        changed = json.loads(json.dumps(self.report))
        changed["tests"][0]["testId"] = "invented.case"
        self.docker.start_result = CLIResult(returncode=0, stdout=json.dumps(changed).encode(), stderr=b"")
        result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertEqual(result.error_code, "TEST_RUNNER_ERROR")
        self.assert_no_receipts()

    async def test_result_trace_never_contains_selectors_input_text_or_raw_console(self):
        self.suite_data["tests"][0]["steps"].insert(1, {"action": "fill", "selector": "#private-field", "value": "fixture-only-value"})
        self.suite_file.write_text(json.dumps(self.suite_data), encoding="utf-8")
        self.set_report()
        self.docker.start_result = replace(self.docker.start_result, stderr=b"api_key=never-store-key\n")
        result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertIsNone(result.error_code)
        record = self.outputs.get(self.run.run_id, result.data["executionManifestId"])
        trace = self.outputs.read_trace(self.binding(), result.data["traceRefs"][0])
        for secret in ("fixture-only-value", "#private-field", "never-store-key"):
            self.assertNotIn(secret, json.dumps(trace))
            self.assertNotIn(secret, record.stdout + record.stderr + repr(record))

    async def test_run_cancelled_during_execution_cannot_publish_after_cleanup(self):
        self.docker.start_hook = lambda _container: self.fixture.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="USER_CANCELLED")
        result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertEqual(result.error_code, "TEST_RUNNER_ERROR")
        self.assert_no_receipts()
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_timeout_oom_storage_and_cleanup_errors_do_not_return_success(self):
        self.docker.start_error = SandboxError(SandboxErrorCode.TIMEOUT)
        result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertEqual(result.error_code, "TIMEOUT")
        self.docker.start_error = None
        self.docker.after_start_mutation = lambda value: value["State"].update(OOMKilled=True)
        result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertEqual(result.error_code, "TEST_RUNNER_ERROR")
        self.docker.after_start_mutation = None
        with patch.object(self.outputs, "publish", side_effect=BrowserStoreError("private SQL")):
            result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertEqual(result.error_code, "TEST_RUNNER_ERROR")
        self.docker.rm_result = CLIResult(returncode=1, stdout=b"", stderr=b"private daemon")
        result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertEqual(result.error_code, "TEST_RUNNER_ERROR")
        self.assert_no_receipts()

    async def test_cancel_waits_for_owned_cleanup_and_does_not_publish(self):
        self.docker.block_start = True
        task = asyncio.create_task(self.dispatcher().call_tool("run_browser_tests", self.arguments()))
        await asyncio.wait_for(self.docker.start_entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assert_no_receipts()

    async def test_approved_browser_cache_path_only_exact_value_for_browser_tool(self):
        self.docker.image[0]["Config"]["Env"] = ["PLAYWRIGHT_BROWSERS_PATH=/ms-playwright"]
        result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertIsNone(result.error_code)
        self.docker.image[0]["Config"]["Env"] = ["PLAYWRIGHT_BROWSERS_PATH=/Users/private"]
        result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        self.assertEqual(result.error_code, "PERMISSION_DENIED")

    async def test_browser_cache_path_exception_does_not_expand_unit_environment(self):
        self.docker.image[0]["Config"]["Env"] = ["PLAYWRIGHT_BROWSERS_PATH=/ms-playwright"]
        self.docker.start_result = CLIResult(returncode=0, stdout=self.fixture.report_json().encode(), stderr=b"")
        unit = UnitTestTools(self.artifacts, self.sandbox, self.fixture.store,
            configuration=UnitTestConfiguration(scopes=(self.fixture.scope,)))
        dispatcher = MCPDispatcher(self.binding(), self.fixture.registry, handlers=unit.handlers(AgentRole.QA))
        result = await dispatcher.call_tool("run_unit_tests", {
            "workspaceId": str(self.run.workspace_id), "snapshotId": str(self.fixture.source.artifact_id),
            "testScope": self.fixture.scope.name})
        self.assertEqual(result.error_code, "PERMISSION_DENIED")
        self.assertEqual(self.docker.commands("create"), [])

    async def test_report_router_preserves_unit_reports_and_reads_browser_reports(self):
        result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        record = self.outputs.get(self.run.run_id, result.data["executionManifestId"])
        report_args = {"workspaceId": str(self.run.workspace_id), "reportRef": record.report_ref}
        read = await self.dispatcher().call_tool("read_test_report", report_args)
        self.assertIsNone(read.error_code)
        get_tool_contract("read_test_report").validate_output(read.data)
        self.assertEqual(read.data["testResult"]["format"], "browser-v1")
        self.docker.exit_code = 0
        self.docker.start_result = CLIResult(returncode=0, stdout=self.fixture.report_json().encode(), stderr=b"")
        unit = UnitTestTools(self.artifacts, self.sandbox, self.fixture.store,
            configuration=UnitTestConfiguration(scopes=(self.fixture.scope,)))
        dispatcher = MCPDispatcher(self.binding(), self.fixture.registry, handlers=unit.handlers(AgentRole.QA))
        unit_result = await dispatcher.call_tool("run_unit_tests", {
            "workspaceId": str(self.run.workspace_id), "snapshotId": str(self.fixture.source.artifact_id), "testScope": self.fixture.scope.name})
        self.assertIsNone(unit_result.error_code)
        read = await self.dispatcher().call_tool("read_test_report", {**report_args, "reportRef": unit_result.data["reportRef"]})
        self.assertIsNone(read.error_code)
        self.assertEqual(read.data["testResult"]["format"], "unittest-v1")

    async def test_missing_wrong_name_and_external_report_ref_rejected(self):
        for ref, code in ((f"artifact://{uuid4()}/browser-test-report.json", "REPORT_NOT_FOUND"),
                          (f"artifact://{uuid4()}/unknown.json", "PATH_DENIED"),
                          ("https://example.invalid/report", "PATH_DENIED")):
            read = await self.dispatcher().call_tool("read_test_report", {"workspaceId": str(self.run.workspace_id), "reportRef": ref})
            self.assertEqual(read.error_code, code)

    async def test_child_configuration_roundtrip_safe_repr_and_invalid_types(self):
        config = MCPChildConfiguration(binding=self.binding(), database_path=self.fixture.repository.database_path,
            workspace_root=self.fixture.registry.base_path, browser_test_configuration=self.configuration)
        args = child_parameters(config).args
        self.assertEqual(decode_browser_configuration(args[args.index("--browser-test-configuration-json") + 1]), self.configuration)
        self.assertNotIn("/snapshot/main.py", repr(config))
        with self.assertRaises(MCPClientError):
            replace(config, browser_test_configuration={"suites": []})

    async def test_real_sdk_stdio_registers_browser_with_missing_config_fail_closed(self):
        config = MCPChildConfiguration(binding=self.binding(), database_path=self.fixture.repository.database_path,
                                      workspace_root=self.fixture.registry.base_path)
        async with open_mcp_client(config) as client:
            tool = next(item for item in (await client._client.session.list_tools()).tools if item.name == "run_browser_tests")
            self.assertTrue(tool.meta["a2a-agent-company/implemented"])
            response = await client._client.session.call_tool("run_browser_tests", self.arguments())
            self.assertTrue(response.is_error)
            self.assertEqual([item.text for item in response.content], ["BROWSER_START_FAILED"])
        self.assert_no_receipts()

    async def test_real_sdk_configured_browser_unavailable_docker_never_runs_on_host(self):
        configuration = replace(self.configuration, docker_endpoint="unix://" + str(self.fixture.directory / "absent-docker.sock"))
        config = MCPChildConfiguration(binding=self.binding(), database_path=self.fixture.repository.database_path,
            workspace_root=self.fixture.registry.base_path, browser_test_configuration=configuration)
        async with open_mcp_client(config) as client:
            with self.assertRaises(MCPClientError) as raised:
                await client.call_tool("run_browser_tests", self.arguments())
            self.assertEqual(raised.exception.code, "MCP_CLIENT_TOOL_FAILED")
        self.assert_no_receipts()
        self.assertFalse((self.fixture.root / ".sandbox").exists())

    async def test_real_sdk_stdio_reads_browser_report_through_existing_tool(self):
        result = await self.dispatcher().call_tool("run_browser_tests", self.arguments())
        record = self.outputs.get(self.run.run_id, result.data["executionManifestId"])
        config = MCPChildConfiguration(binding=self.binding(), database_path=self.fixture.repository.database_path,
                                      workspace_root=self.fixture.registry.base_path)
        async with open_mcp_client(config) as client:
            response = await client._client.session.call_tool("read_test_report", {
                "workspaceId": str(self.run.workspace_id), "reportRef": record.report_ref})
            self.assertFalse(response.is_error)
            self.assertEqual(response.structured_content["testResult"], record.report.to_dict())

    async def test_construction_listing_and_router_are_inert(self):
        with patch.object(self.artifacts, "bind", side_effect=AssertionError("inert")), \
                patch.object(self.sandbox, "bind", side_effect=AssertionError("inert")):
            browser = self.tools()
            unit = UnitTestTools(self.artifacts, self.sandbox, self.fixture.store)
            self.assertEqual(repr(browser), "BrowserTestTools()")
            self.assertEqual(repr(TestReportTools(unit, browser)), "TestReportTools()")
            self.assertIn("run_browser_tests", browser.handlers(AgentRole.QA))
        with self.assertRaises(MCPConfigurationError):
            self.tools(max_call_seconds=True)
        self.assertEqual(self.docker.calls, [])
