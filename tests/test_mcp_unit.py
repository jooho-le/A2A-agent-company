"""Step26 MCP plumbing: real Git/SQLite, fake Docker, real SDK child.

No product or Agent-generated tests are executed on the Host. Fake Docker
checks boundaries only; it is not evidence of a successful Container test.
"""

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
from mcp_tools.tools.unit import UnitTestTools
from mcp_tools.tools.unit_config import (
    UnitTestConfiguration, UnitTestConfigurationError, UnitTestScope, decode_unit_configuration,
)
from mcp_tools.tools.unit_store import UnitTestStoreError
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain import AgentRole, WorkflowStatus
from orchestrator.sandbox.contracts import CLIResult, ExecutionProfile, SandboxError, SandboxErrorCode
from orchestrator.sandbox.runtime import SandboxRuntime


class UnitToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = store_fixture.UnitTestOutputStoreTests("test_constructor_is_inert")
        self.fixture.setUp()
        for cleanup, args, kwargs in self.fixture._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        self.fixture._cleanups.clear()
        self.run = self.fixture.run
        self.artifacts = ArtifactStore(self.fixture.repository, self.fixture.registry)
        self.docker = sandbox_fixture.FakeDocker()
        self.sandbox = SandboxRuntime(self.fixture.repository, self.fixture.registry, self.artifacts, docker=self.docker)
        self.configuration = UnitTestConfiguration(scopes=(self.fixture.scope,), image_reference=sandbox_fixture.IMAGE_ID)
        self.set_report()

    def set_report(self, *, outcome="PASS", details=None):
        self.docker.exit_code = 1 if outcome == "FAIL" else 0
        self.docker.start_result = CLIResult(returncode=self.docker.exit_code,
            stdout=self.fixture.report_json(outcome=outcome, details=details).encode(), stderr=b"")

    def binding(self, role=AgentRole.DEVELOPER):
        return MCPBinding(role=role, agent_role=role, run_id=self.run.run_id, workspace_id=self.run.workspace_id)

    def tools(self, *, configuration=True, max_call_seconds=60):
        return UnitTestTools(self.artifacts, self.sandbox, self.fixture.store,
            configuration=self.configuration if configuration else None, max_call_seconds=max_call_seconds)

    def dispatcher(self, role=AgentRole.DEVELOPER, **options):
        return MCPDispatcher(self.binding(role), self.fixture.registry, handlers=self.tools(**options).handlers(role),
                             max_call_seconds=options.get("max_call_seconds", 60))

    def arguments(self, **extra):
        return {"workspaceId": str(self.run.workspace_id), "snapshotId": str(self.fixture.source.artifact_id),
                "testScope": self.configuration.scopes[0].name, **extra}

    def qa_context(self, kind="QA_TESTS", protected_ref=None):
        self.fixture.qa_context(kind=kind, protected_ref=protected_ref)
        self.configuration = UnitTestConfiguration(scopes=(self.fixture.scope,), image_reference=sandbox_fixture.IMAGE_ID)
        if kind == "QA_TESTS":
            tests = self.fixture.root / "outputs/qa/tests"
            tests.mkdir(parents=True, exist_ok=True)
            (tests / "test_signup.py").write_text("# captured QA tests\n", encoding="utf-8")

    def assert_no_receipts(self):
        with self.fixture.repository._connection() as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='unit_test_execution_records'").fetchone():
                self.assertEqual(connection.execute("SELECT count(*) FROM unit_test_execution_records").fetchone()[0], 0)

    async def test_real_handler_exact_contract_manifest_private_refs_and_cleanup(self):
        before = self.fixture.repository.get_run(self.run.run_id)
        outcome = await self.dispatcher().call_tool("run_unit_tests", self.arguments())
        self.assertIsNone(outcome.error_code)
        get_tool_contract("run_unit_tests").validate_output(outcome.data)
        record = self.fixture.store.get(self.run.run_id, outcome.data["executionManifestId"])
        self.assertEqual(record.execution_manifest, self.fixture.source.execution_manifest())
        self.assertEqual(record.workflow_step_id, self.fixture.step.workflow_step_id)
        self.assertEqual(record.tool_output(), outcome.data)
        self.assertEqual(record.execution_profile.limits.timeout_seconds, 39)
        self.assertNotEqual(str(record.execution_id), outcome.data["executionManifestId"])
        self.assertEqual((outcome.data["total"], outcome.data["passed"]), (1, 1))
        self.assertEqual(self.fixture.repository.get_run(self.run.run_id), before)
        self.assertEqual(self.fixture.repository.list_project_artifacts(self.run.run_id), [])
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assertFalse(Path(self.docker.container["Mounts"][0]["Source"]).exists())

    async def test_assertion_failure_is_successful_tool_call_not_verdict(self):
        self.set_report(outcome="FAIL", details="assertion did not match")
        outcome = await self.dispatcher().call_tool("run_unit_tests", self.arguments())
        self.assertIsNone(outcome.error_code)
        self.assertEqual((outcome.data["passed"], outcome.data["failed"]), (0, 1))
        self.assertEqual(self.fixture.store.get(self.run.run_id, outcome.data["executionManifestId"]).exit_code, 1)
        self.assertIsNone(self.fixture.repository.get_run(self.run.run_id).verdict)

    async def test_build_cannot_reuse_unit_manifest_or_execution_uuid(self):
        outcome = await self.dispatcher().call_tool("run_unit_tests", self.arguments())
        record = self.fixture.store.get(self.run.run_id, outcome.data["executionManifestId"])
        profile = ExecutionProfile(name="fixture-build", tool_name="run_build",
                                   argv=("/usr/local/bin/python", "-B", "/snapshot/main.py"))
        result = replace(self.fixture.result, profile_name=profile.name, tool_name=profile.tool_name,
                         execution_id=uuid4(), stdout="build fixture\n")
        outputs = BuildOutputStore(self.fixture.repository)
        for identity in (record.execution_manifest_id, record.execution_id):
            with self.subTest(identity=identity), patch("mcp_tools.tools.build_store.uuid4", return_value=identity):
                with self.assertRaises(BuildStoreError) as raised:
                    outputs.publish(self.binding(), self.fixture.source, result, profile=profile)
                self.assertEqual(raised.exception.code, "BUILD_RECORD_CONFLICT")
            with self.assertRaises(BuildStoreError) as raised:
                outputs.publish(self.binding(), self.fixture.source, replace(result, execution_id=identity), profile=profile)
            self.assertEqual(raised.exception.code, "BUILD_RECORD_CONFLICT")

    async def test_skip_is_not_a_pass(self):
        self.set_report(outcome="SKIP")
        outcome = await self.dispatcher().call_tool("run_unit_tests", self.arguments())
        self.assertIsNone(outcome.error_code)
        self.assertEqual((outcome.data["passed"], outcome.data["skipped"]), (0, 1))

    async def test_raw_json_is_validated_before_credential_redaction(self):
        self.set_report(outcome="FAIL", details='password="fixture-secret"\nquote="ok"\\newline')
        outcome = await self.dispatcher().call_tool("run_unit_tests", self.arguments())
        self.assertIsNone(outcome.error_code)
        record = self.fixture.store.get(self.run.run_id, outcome.data["executionManifestId"])
        self.assertNotIn("fixture-secret", record.stdout)
        self.assertEqual(json.loads(record.stdout), record.report.to_dict())
        self.assertNotIn("fixture-secret", repr(record))

    async def test_malformed_duplicate_zero_and_mismatched_reports_fail_closed(self):
        valid = self.fixture.report_json()
        reports = (b"runner failed", b"\xff", b'{"error":"TEST_RUNNER_ERROR"}',
                   valid.replace('"total": 1', '"total": 0').encode(),
                   valid.replace('"total": 1', '"total": 1, "total": 1').encode(),
                   valid.replace('"passed": 1', '"passed": 0').encode())
        for raw in reports:
            with self.subTest(raw=raw):
                self.docker.start_result = CLIResult(returncode=0, stdout=raw, stderr=b"")
                outcome = await self.dispatcher().call_tool("run_unit_tests", self.arguments())
                self.assertEqual(outcome.error_code, "TEST_RUNNER_ERROR")
                self.assertIsNone(outcome.data)
                self.assert_no_receipts()
        self.assertEqual(len(self.docker.commands("rm")), len(reports))

    async def test_runner_discovery_exit_two_is_not_test_failure_receipt(self):
        self.docker.exit_code = 2
        outcome = await self.dispatcher().call_tool("run_unit_tests", self.arguments())
        self.assertEqual(outcome.error_code, "TEST_RUNNER_ERROR")
        self.assert_no_receipts()
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_frozen_source_and_host_runner_are_readonly_not_working_copy(self):
        (self.fixture.root / "source/main.py").write_text("# mutable version\n", encoding="utf-8")
        def inspect(container):
            mounts = {item["Destination"]: item for item in container["Mounts"]}
            self.assertFalse(mounts["/snapshot"]["RW"])
            self.assertFalse(mounts["/inputs"]["RW"])
            frozen = Path(mounts["/snapshot"]["Source"])
            self.assertIn("Do not execute", (frozen / "main.py").read_text())
            inputs = Path(mounts["/inputs"]["Source"])
            self.assertIn("Stdlib-only trusted harness", (inputs / "_unit_runner.py").read_text())
            self.assertFalse((inputs / "tests").exists())
            self.assertIn("/inputs/_unit_runner.py", container["Args"])
        self.docker.start_hook = inspect
        outcome = await self.dispatcher().call_tool("run_unit_tests", self.arguments())
        self.assertIsNone(outcome.error_code)

    async def test_qa_inputs_captured_hash_bound_and_report_read(self):
        self.qa_context()
        def inspect(container):
            mounts = {item["Destination"]: item for item in container["Mounts"]}
            inputs = Path(mounts["/inputs"]["Source"])
            self.assertEqual((inputs / "tests/test_signup.py").read_text(), "# captured QA tests\n")
            (self.fixture.root / "outputs/qa/tests/test_signup.py").write_text("# changed\n", encoding="utf-8")
            self.assertEqual((inputs / "tests/test_signup.py").read_text(), "# captured QA tests\n")
        self.docker.start_hook = inspect
        dispatcher = self.dispatcher(AgentRole.QA)
        outcome = await dispatcher.call_tool("run_unit_tests", self.arguments())
        self.assertIsNone(outcome.error_code)
        record = self.fixture.store.get(self.run.run_id, outcome.data["executionManifestId"])
        self.assertEqual(record.role, AgentRole.QA)
        self.assertIn("tests/test_signup.py", [item["path"] for item in record.inputs["files"]])
        result = await dispatcher.call_tool("read_test_report", {
            "workspaceId": str(self.run.workspace_id), "reportRef": outcome.data["reportRef"]})
        self.assertIsNone(result.error_code)
        get_tool_contract("read_test_report").validate_output(result.data)
        self.assertEqual(result.data, {"testResult": record.report.to_dict()})

    async def test_protected_tests_use_only_host_bytes_not_qa_scratch(self):
        self.qa_context("PROTECTED")
        tests = self.fixture.root / "outputs/qa/tests"
        tests.mkdir(parents=True, exist_ok=True)
        (tests / "test_signup.py").write_text("# not protected tests\n", encoding="utf-8")
        def inspect(container):
            inputs = next(item for item in container["Mounts"] if item["Destination"] == "/inputs")
            self.assertEqual((Path(inputs["Source"]) / "tests/test_signup.py").read_text(), "# protected criteria\n")
        self.docker.start_hook = inspect
        outcome = await self.dispatcher(AgentRole.QA).call_tool("run_unit_tests", self.arguments())
        self.assertIsNone(outcome.error_code)

    async def test_protected_suite_ref_must_match_frozen_run_before_docker(self):
        self.qa_context("PROTECTED", "https://criteria.example.invalid/another/v1")
        outcome = await self.dispatcher(AgentRole.QA).call_tool("run_unit_tests", self.arguments())
        self.assertEqual(outcome.error_code, "PERMISSION_DENIED")
        self.assertEqual(self.docker.calls, [])

    async def test_qa_cannot_execute_developer_snapshot_scope(self):
        self.fixture.qa_context()
        outcome = await self.dispatcher(AgentRole.QA).call_tool("run_unit_tests", self.arguments())
        self.assertEqual(outcome.error_code, "PERMISSION_DENIED")
        self.assertEqual(self.docker.calls, [])

    async def test_missing_profile_and_unknown_scope_are_execution_errors(self):
        for options, arguments in (({"configuration": False}, self.arguments()), ({}, self.arguments(testScope="unknown"))):
            outcome = await self.dispatcher(**options).call_tool("run_unit_tests", arguments)
            self.assertEqual(outcome.error_code, "TEST_RUNNER_ERROR")
        self.assertTrue(self.dispatcher(configuration=False).is_implemented("run_unit_tests"))
        self.assertEqual(self.docker.calls, [])

    async def test_host_fields_and_other_roles_rejected_at_protocol_boundary(self):
        for field in ("command", "argv", "role", "hostPath", "pattern", "image", "dockerEndpoint"):
            with self.subTest(field=field), self.assertRaises(MCPProtocolError):
                await self.dispatcher().call_tool("run_unit_tests", self.arguments(**{field: "untrusted"}))
        for role in (AgentRole.PLANNER, AgentRole.SECURITY):
            self.assertEqual(dict(self.tools().handlers(role)), {})
            with self.assertRaises(MCPProtocolError):
                await self.dispatcher(role).call_tool("run_unit_tests", self.arguments())
        self.assertEqual(self.docker.calls, [])

    async def test_wrong_workspace_and_unregistered_source_stop_before_docker(self):
        for arguments in (self.arguments(workspaceId=str(uuid4())), self.arguments(snapshotId=str(uuid4()))):
            outcome = await self.dispatcher().call_tool("run_unit_tests", arguments)
            self.assertIsNotNone(outcome.error_code)
        self.assertEqual(self.docker.calls, [])

    async def test_no_active_qa_step_cannot_start_container(self):
        scope = UnitTestScope(name="qa-unit", kind="QA_TESTS")
        self.configuration = UnitTestConfiguration(scopes=(scope,))
        outcome = await self.dispatcher(AgentRole.QA).call_tool("run_unit_tests", self.arguments())
        self.assertEqual(outcome.error_code, "PERMISSION_DENIED")
        self.assertEqual(self.docker.calls, [])

    async def test_run_state_changed_during_execution_cannot_publish(self):
        self.docker.start_hook = lambda _container: self.fixture.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="USER_CANCELLED")
        outcome = await self.dispatcher().call_tool("run_unit_tests", self.arguments())
        self.assertEqual(outcome.error_code, "TEST_RUNNER_ERROR")
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assert_no_receipts()

    async def test_timeout_and_oom_are_errors_without_report(self):
        self.docker.start_error = SandboxError(SandboxErrorCode.TIMEOUT)
        outcome = await self.dispatcher().call_tool("run_unit_tests", self.arguments())
        self.assertEqual(outcome.error_code, "TIMEOUT")
        self.assert_no_receipts()
        self.docker.start_error = None
        self.docker.after_start_mutation = lambda value: value["State"].update(OOMKilled=True)
        outcome = await self.dispatcher().call_tool("run_unit_tests", self.arguments())
        self.assertEqual(outcome.error_code, "TEST_RUNNER_ERROR")
        self.assert_no_receipts()

    async def test_store_and_cleanup_errors_never_return_success(self):
        with patch.object(self.fixture.store, "publish", side_effect=UnitTestStoreError("private SQL")):
            outcome = await self.dispatcher().call_tool("run_unit_tests", self.arguments())
        self.assertEqual(outcome.error_code, "TEST_RUNNER_ERROR")
        self.assertIsNone(outcome.data)
        self.assert_no_receipts()
        self.docker.rm_result = CLIResult(returncode=1, stdout=b"", stderr=b"private daemon")
        outcome = await self.dispatcher().call_tool("run_unit_tests", self.arguments())
        self.assertEqual(outcome.error_code, "TEST_RUNNER_ERROR")
        self.assert_no_receipts()

    async def test_cancel_waits_for_owned_cleanup_without_receipt(self):
        self.docker.block_start = True
        task = asyncio.create_task(self.dispatcher().call_tool("run_unit_tests", self.arguments()))
        await asyncio.wait_for(self.docker.start_entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assert_no_receipts()

    async def test_report_missing_external_uri_and_developer_read_denied(self):
        args = {"workspaceId": str(self.run.workspace_id), "reportRef": f"artifact://{uuid4()}/unit-test-report.json"}
        outcome = await self.dispatcher(AgentRole.QA).call_tool("read_test_report", args)
        self.assertEqual(outcome.error_code, "REPORT_NOT_FOUND")
        with self.assertRaises(MCPProtocolError):
            await self.dispatcher().call_tool("read_test_report", args)
        external = await self.dispatcher(AgentRole.QA).call_tool("read_test_report", {
            **args, "reportRef": "https://external.example.invalid/report"})
        self.assertEqual(external.error_code, "PATH_DENIED")
        wrong_name = await self.dispatcher(AgentRole.QA).call_tool("read_test_report", {
            **args, "reportRef": f"artifact://{uuid4()}/not-unit-report.json"})
        self.assertEqual(wrong_name.error_code, "PATH_DENIED")
        self.assertEqual(self.docker.calls, [])

    async def test_child_configuration_roundtrip_and_safe_repr(self):
        config = MCPChildConfiguration(binding=self.binding(), database_path=self.fixture.repository.database_path,
            workspace_root=self.fixture.registry.base_path, unit_test_configuration=self.configuration)
        args = child_parameters(config).args
        decoded = decode_unit_configuration(args[args.index("--unit-test-configuration-json") + 1])
        self.assertEqual(decoded, self.configuration)
        self.assertNotIn("/usr/local/bin/python", repr(config))
        with self.assertRaises(MCPClientError):
            replace(config, unit_test_configuration={"scopes": []})

    async def test_real_sdk_stdio_registers_unit_without_configuration_fail_closed(self):
        config = MCPChildConfiguration(binding=self.binding(), database_path=self.fixture.repository.database_path,
                                      workspace_root=self.fixture.registry.base_path)
        async with open_mcp_client(config) as client:
            listing = await client._client.session.list_tools()
            tool = next(item for item in listing.tools if item.name == "run_unit_tests")
            self.assertTrue(tool.meta["a2a-agent-company/implemented"])
            response = await client._client.session.call_tool("run_unit_tests", self.arguments())
            self.assertTrue(response.is_error)
            self.assertIsNone(response.structured_content)
            self.assertEqual([item.text for item in response.content], ["TEST_RUNNER_ERROR"])

    async def test_real_sdk_configured_unit_without_docker_cannot_fall_back_to_host(self):
        configuration = replace(self.configuration,
            docker_endpoint="unix://" + str(self.fixture.directory / "absent-docker.sock"))
        config = MCPChildConfiguration(binding=self.binding(), database_path=self.fixture.repository.database_path,
            workspace_root=self.fixture.registry.base_path, unit_test_configuration=configuration)
        async with open_mcp_client(config) as client:
            with self.assertRaises(MCPClientError) as raised:
                await client.call_tool("run_unit_tests", self.arguments())
            self.assertEqual(raised.exception.code, "MCP_CLIENT_TOOL_FAILED")
        self.assert_no_receipts()
        self.assertFalse((self.fixture.root / ".sandbox").exists())
        self.assert_no_receipts()

    async def test_real_sdk_stdio_reads_saved_report_without_container_execution(self):
        outcome = await self.dispatcher().call_tool("run_unit_tests", self.arguments())
        config = MCPChildConfiguration(binding=self.binding(AgentRole.QA), database_path=self.fixture.repository.database_path,
                                      workspace_root=self.fixture.registry.base_path)
        async with open_mcp_client(config) as client:
            result = await client._client.session.call_tool("read_test_report", {
                "workspaceId": str(self.run.workspace_id), "reportRef": outcome.data["reportRef"]})
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["testResult"]["total"], 1)

    async def test_constructor_is_inert_and_too_short_timeout_is_safe(self):
        with patch.object(self.artifacts, "bind", side_effect=AssertionError("inert")), \
                patch.object(self.sandbox, "bind", side_effect=AssertionError("inert")):
            self.assertEqual(repr(self.tools()), "UnitTestTools()")
        with self.assertRaises(MCPConfigurationError):
            self.tools(max_call_seconds=True)
        with self.assertRaises(UnitTestConfigurationError):
            UnitTestScope(name="bad-pattern", kind="SNAPSHOT", pattern="-test.py")
        outcome = await self.dispatcher(max_call_seconds=10).call_tool("run_unit_tests", self.arguments())
        self.assertEqual(outcome.error_code, "TEST_RUNNER_ERROR")
        self.assertEqual(self.docker.calls, [])
