"""Offline Security replay: real input/SQLite/A2A/executor, fake provider/Docker."""
from contextlib import asynccontextmanager
from dataclasses import replace, asdict
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import Mock, patch

from agents.llm.budget import LLMLimits
from agents.llm.contracts import LLMResponse, TokenUsage, ToolCall
from agents.platform.evaluation_export import write_evaluation_export
from agents.roles.qa_contract import QACaseBinding
from agents.runtime.security_services import SecurityRuntimeServices
from diagnostics import stage_replay as replay
from mcp_tools.client import BoundMCPClient
from mcp_tools.runtime import MCPDispatcher
from mcp_tools.tools.files import FileTools
from mcp_tools.tools.security import SecurityScanTools
from mcp_tools.tools.security_config import encode_security_configuration
from mcp_tools.tools.security_store import SecurityScanOutputStore
from mcp_tools.tools.unit import UnitTestTools
from mcp_tools.tools.unit_config import UnitTestConfiguration, UnitTestScope, encode_unit_configuration
from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.models import WorkflowStep
from orchestrator.domain.states import AgentRole, WorkflowStepStatus, A2ATaskState
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.runtime import SandboxRuntime
from orchestrator.sandbox.contracts import CLIResult, SandboxLimits
from orchestrator.workspaces.registry import WorkspaceRegistry
import test_security_agent as security_fixture
from test_llm_runtime import FakeProvider, text_response, tool_response
from test_mcp_execution_runtime import DispatcherSession
from signup_receipt_fixture import SignupFixtureDocker, generated_signup_evidence
from test_sandbox_runtime import IMAGE_ID
from semantic_helpers import SemanticFakeProvider


class ClosingProvider(SemanticFakeProvider):
    closed = False

    async def aclose(self):
        self.closed = True


class StageReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        f = security_fixture.SecurityAgentTests("test_constructor_inert_safe_repr_and_bad_capabilities")
        self.protected = self._testMethodName == "test_protected_security_tests_run_in_addition_to_generated_tests"
        reference = "artifact://protected-tests/fixture/security-v1"
        if self.protected:
            real = security_fixture.RunConfiguration
            with patch.object(security_fixture, "RunConfiguration", side_effect=lambda **kw:
                              real(protected_test_suite_ref=reference, **kw)):
                await f.asyncSetUp()
        else:
            await f.asyncSetUp()
        for cleanup, args, kwargs in f._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        f._cleanups.clear()
        self.f = f
        self.generated = self.protected or self._testMethodName == "test_generated_security_tests_execute_only_in_fake_container"
        cases = ()
        if self.generated:
            scopes = (UnitTestScope(name="security-unit", kind="QA_TESTS", role="SECURITY"),)
            if self.protected:
                scopes += (UnitTestScope(name="protected-security", kind="PROTECTED", role="SECURITY",
                    protected_suite_ref=reference, protected_files={'tests/test_security.py':'# inert protected fixture\n'}),)
                cases = tuple(QACaseBinding(tool_name="run_unit_tests", selector="protected-security",
                    test_id=f"test_security.Security.test_req_{i}", requirement_id=identifier,
                    title="Protected fixture", expected_result=next(r.acceptance_criteria[0]
                        for r in f.fixture.scenario.requirements if r.requirement_id == identifier))
                    for i, identifier in enumerate(f.metadata.requirement_ids, 1))
            unit = UnitTestConfiguration(scopes=scopes,
                image_reference=IMAGE_ID, limits=SandboxLimits(timeout_seconds=2, control_timeout_seconds=.5))
            f.mcp_configuration = replace(f.mcp_configuration, unit_test_configuration=unit)
            f.services_factory = lambda execution: SecurityRuntimeServices(f.repository, f.registry, f.artifacts,
                mcp_configuration=f.mcp_configuration, client_factory=f.peer_client, protected_cases=cases)
        failed, _ = await f.run_to(FakeProvider(text_response(status="incomplete")), "TASK_STATE_FAILED")
        step = WorkflowStep.model_validate({**f.step.model_dump(), "status": WorkflowStepStatus.FAILED,
            "a2a_task_state": A2ATaskState.FAILED, "a2a_task_id": failed["id"], "agent_context_id": failed["contextId"]})
        f._update_step(step)
        self.evidence = f.fixture.directory
        write_evaluation_export(f.repository, f.run.run_id, self.evidence / "export",
                                stores={AgentRole.SECURITY: f.agent_database})
        self.policy = {"security": encode_security_configuration(f.scan_configuration),
                       "unit": encode_unit_configuration(unit) if self.generated else None,
                       "protectedCases": [{**asdict(c), "requirement_id": str(c.requirement_id)} for c in cases],
                       "maxCallSeconds": f.mcp_configuration.max_call_seconds}
        self.limits = LLMLimits(max_model_calls=10, max_tool_calls=20, max_total_tokens=10000,
                                max_output_tokens=2048, max_output_corrections=2)
        self.original = self.evidence / "orchestrator.sqlite3"
        self.original_hash = sha256(self.original.read_bytes()).hexdigest()
        self.original_state = f.repository.get_run(f.run.run_id)
        self.sessions = []

    async def prepare(self, **changes):
        args = dict(policy=self.policy, limits=self.limits, runtime_budget_ms=10000)
        args.update(changes)
        root = await replay.prepare_replay(self.evidence, **args)
        self.addCleanup(shutil.rmtree, root)
        return root

    @asynccontextmanager
    async def client(self, configuration):
        repository = SQLiteWorkflowRepository(configuration.database_path)
        registry = WorkspaceRegistry(repository, configuration.workspace_root)
        artifacts = ArtifactStore(repository, registry)
        sandbox = SandboxRuntime(repository, registry, artifacts, docker=self.f.docker)
        scans = SecurityScanTools(artifacts, sandbox, SecurityScanOutputStore(repository),
                                  configuration=configuration.security_scan_configuration, max_call_seconds=5)
        files = FileTools(artifacts, frozen_source=configuration.frozen_source)
        handlers = {**files.handlers(AgentRole.SECURITY), **scans.handlers(AgentRole.SECURITY)}
        if configuration.unit_test_configuration is not None:
            docker = SignupFixtureDocker()
            ids = [f"test_security.Security.test_req_{i}" for i in range(1, len(self.f.metadata.requirement_ids) + 1)]
            docker.start_result = CLIResult(returncode=0, stderr=b"", stdout=json.dumps({"format": "unittest-v1",
                "total": len(ids), "passed": len(ids), "failed": 0, "skipped": 0,
                "tests": [{"testId": test_id, "outcome": "PASS", **generated_signup_evidence("SECURITY", i)}
                          for i, test_id in enumerate(ids, 1)]}).encode())
            unit = UnitTestTools(artifacts, SandboxRuntime(repository, registry, artifacts, docker=docker),
                UnitTestOutputStore(repository), configuration=configuration.unit_test_configuration, max_call_seconds=5)
            handlers.update(unit.handlers(AgentRole.SECURITY))
        dispatcher = MCPDispatcher(configuration.binding, registry, handlers=handlers, max_call_seconds=5)
        session = DispatcherSession(dispatcher)
        self.sessions.append(session)
        client = BoundMCPClient(configuration=configuration,
                                _client=type("Peer", (), {"session": session})())
        try:
            yield client
        finally:
            client._state.active = False

    async def execute(self, root, provider):
        return await replay.execute_replay(root, approval_id=replay._read_json(root / "manifest.json")["approvalSha256"],
                                          provider_factory=lambda: provider, client_factory=self.client)

    async def test_prepare_inert_exact_input_private_files_and_no_old_usage(self):
        with patch("httpx.AsyncClient.post", side_effect=AssertionError("no HTTP")), \
             patch("asyncio.create_subprocess_exec", side_effect=AssertionError("no process")):
            root = await self.prepare()
        manifest = replay.verify_prepared(root)
        self.assertEqual(manifest["status"], "AWAITING_APPROVAL")
        self.assertFalse(manifest["measurementEligible"])
        self.assertEqual(manifest["snapshotSha256"], self.f.source.snapshot_sha256)
        original = replay._read_json(self.evidence / "export/a2a-messages.json")["roles"]["SECURITY"]["receipts"][0]["request"]
        self.assertEqual(replay._read_json(root / "request.json"), original)
        self.assertEqual(sha256(self.original.read_bytes()).hexdigest(), self.original_hash)
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        for name in ("manifest.json", "request.json", "policy.json", "diagnostic.sqlite3"):
            self.assertEqual((root / name).stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(root / "diagnostic.sqlite3") as c:
            self.assertEqual(c.execute('SELECT count(*) FROM trace_events').fetchone()[0], 0)
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='agent_budget_ledger'").fetchone())

    async def test_approval_gate_precedes_provider_tools_and_claim(self):
        root = await self.prepare()
        factory = Mock(side_effect=AssertionError("provider must not be created"))
        for approval in (None, "unapproved"):
            with self.subTest(approval=approval), self.assertRaisesRegex(replay.ReplayError, "EXPLICIT_APPROVAL"):
                await replay.execute_replay(root, approval_id=approval, provider_factory=factory)
        factory.assert_not_called()
        self.assertFalse((root / "execution-claimed.json").exists())

    async def test_only_security_runs_to_completion_and_original_unchanged(self):
        root = await self.prepare()
        provider = ClosingProvider(self.f.draft_response())
        result = await self.execute(root, provider)
        self.assertIsNone(result["error"])
        self.assertEqual(result["taskStatus"]["state"], "TASK_STATE_COMPLETED", result)
        self.assertEqual(result["modelCalls"], 2)  # Security + its semantic publication review.
        self.assertTrue(provider.closed)
        self.assertFalse(result["measurementEligible"])
        self.assertIsNone(result["overallVerdict"])
        self.assertEqual(result["snapshotSha256"], self.f.source.snapshot_sha256)
        self.assertEqual(self.f.repository.get_run(self.f.run.run_id), self.original_state)
        self.assertEqual(sha256(self.original.read_bytes()).hexdigest(), self.original_hash)
        usage = replay._read_json(root / "attempt/usage.json")
        self.assertEqual(usage["total"], 2)
        self.assertEqual({r["binding"]["role"] for r in usage["records"]}, {"SECURITY"})
        names = [name for session in self.sessions for name, _, _ in session.calls]
        self.assertEqual(names, ["run_security_scan"])
        # No full-Run continuation, planner, developer, QA or evaluation export.
        self.assertFalse((root / "attempt/export").exists())
        with self.assertRaisesRegex(replay.ReplayError, "ALREADY_ATTEMPTED"):
            await self.execute(root, ClosingProvider())

    async def test_output_limit_capture_and_bounded_recovery(self):
        root = await self.prepare()
        cut = LLMResponse(status="incomplete", model_id="fake-model", usage=TokenUsage(
            input_tokens=5, output_tokens=2048, total_tokens=2053), output_text='{"truncated":"fragment')
        provider = ClosingProvider(cut, self.f.draft_response())
        result = await self.execute(root, provider)
        self.assertEqual(result["taskStatus"]["state"], "TASK_STATE_COMPLETED", result)
        raws = list((root / "attempt/raw").glob("*.json"))
        self.assertTrue(raws)
        captured = [replay._read_json(f) for f in raws]
        self.assertTrue(any(r["kind"] == "OUTPUT_LIMIT_REACHED" for r in captured))
        telemetry = replay._read_json(root / "attempt/telemetry.json")["events"]
        routes = [r["detail"]["recovery"] for r in telemetry if "recovery" in r["detail"]]
        self.assertEqual(sum(r.get("mode") == "REGENERATION" for r in routes), 1)
        self.assertEqual(result["modelCalls"], 3)

    async def test_generated_security_tests_execute_only_in_fake_container(self):
        root = await self.prepare()
        def ready(request):
            draft = self.f.draft(request)
            for row in draft['requirementReviews']:
                row['rationale'] = 'HOST_TESTS_PENDING'
            requirements = self.f.fixture.scenario.requirements
            draft["testCases"] = [{"selector": "security-unit", "testId": f"test_security.Security.test_req_{i}",
                "requirementId": str(identifier), "title": "Synthetic security case",
                "expectedResult": next(r.acceptance_criteria[0] for r in requirements if r.requirement_id == identifier)}
                for i, identifier in enumerate(self.f.metadata.requirement_ids, 1)]
            return text_response(json.dumps(draft))
        write = tool_response(ToolCall(call_id="diagnostic-write", name="write_test_file", arguments_json=json.dumps({
            "workspaceId": str(self.f.run.workspace_id), "path": "outputs/security/tests/test_security.py",
            "content": "# inert generated fixture; never executed on Host\n"})))
        provider = ClosingProvider(write, ready)
        result = await self.execute(root, provider)
        self.assertEqual(result["taskStatus"]["state"], "TASK_STATE_COMPLETED", result)
        names = [name for session in self.sessions for name, _, _ in session.calls]
        self.assertEqual(names, (["run_unit_tests"] if self.protected else [])
                         + ["run_security_scan", "write_test_file", "run_unit_tests"])
        artifact_events = [replay._read_json(p) for p in sorted((root / "attempt").glob('event-*.json'))]
        reports = [e["artifact"] for e in artifact_events if "artifact" in e]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["name"], "security-report.json")

    async def test_protected_security_tests_run_in_addition_to_generated_tests(self):
        await self.test_generated_security_tests_execute_only_in_fake_container()

    async def test_provider_failure_records_stop_and_does_not_retry(self):
        from agents.llm.contracts import LLMRuntimeError, LLMErrorCode
        root = await self.prepare()
        provider = ClosingProvider(LLMRuntimeError(LLMErrorCode.PROVIDER))
        result = await self.execute(root, provider)
        self.assertEqual(result["taskStatus"]["state"], "TASK_STATE_FAILED")
        self.assertEqual(result["modelCalls"], 1)
        self.assertIsNone(result["totalTokens"])
        self.assertFalse(result["usageComplete"])
        self.assertTrue(provider.closed)

    async def test_budget_stop_does_not_restart_the_stage_or_lose_usage(self):
        root = await self.prepare(limits=self.limits.model_copy(update={"max_model_calls": 1}))
        result = await self.execute(root, ClosingProvider(self.f.draft_response()))
        self.assertEqual(result["taskStatus"]["state"], "TASK_STATE_FAILED")
        self.assertEqual(result["modelCalls"], 1)
        self.assertEqual(result["taskStatus"]["message"]["parts"][0]["data"]["code"], "LLM_BUDGET_EXHAUSTED")
        self.assertEqual(replay._read_json(root / "attempt/usage.json")["total"], 1)

    async def test_changing_approved_limits_requires_preparing_again(self):
        root = await self.prepare()
        manifest = replay._read_json(root / "manifest.json")
        manifest["llmLimits"]["max_model_calls"] += 1
        (root / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(replay.ReplayError, "APPROVAL_PLAN_CHANGED"):
            replay.verify_prepared(root)

    async def test_captured_test_bytes_restore_only_when_hashes_match(self):
        reference = "artifact://protected-tests/fixture/frozen-v1"
        bundle_path = self.evidence / "export/bundle.json"
        bundle = replay._read_json(bundle_path)
        bundle["configuration"]["configuration"]["protectedTestSuiteRef"] = reference
        bundle_path.write_text(json.dumps(bundle))
        with sqlite3.connect(self.original) as c:
            c.execute("CREATE TABLE qa_test_input_captures (capture_id TEXT,run_id TEXT,source_artifact_id TEXT)")
            c.execute("CREATE TABLE qa_test_input_files (capture_id TEXT,path TEXT,content BLOB,content_sha256 TEXT)")
            c.execute("INSERT INTO qa_test_input_captures VALUES(?,?,?)", ('capture',str(self.f.run.run_id),str(self.f.source.artifact_id)))
            c.execute("INSERT INTO qa_test_input_files VALUES(?,?,?,?)", ('capture','tests/shared.py',b'# old\n',sha256(b'# old\n').hexdigest()))
        unit = UnitTestConfiguration(scopes=(UnitTestScope(name="protected-security", kind="PROTECTED", role="SECURITY",
            protected_suite_ref="artifact://protected-tests/fixture/new-v2", protected_files={'tests/shared.py':'# new\n'}),))
        policy = {**self.policy, "unit": encode_unit_configuration(unit)}
        restored = replay.restore_captured_policy(self.evidence, policy)
        scope = json.loads(restored["unit"])["scopes"][0]
        self.assertEqual(scope["protected_suite_ref"], reference)
        self.assertEqual(scope["protected_files"]["tests/shared.py"], '# old\n')
        self.assertIn('# new', policy['unit'])
        with sqlite3.connect(self.original) as c:
            c.execute("UPDATE qa_test_input_files SET content_sha256=?",('0'*64,))
        with self.assertRaisesRegex(replay.ReplayError, "CAPTURE_HASH_MISMATCH"):
            replay.restore_captured_policy(self.evidence, policy)

    async def test_changed_prepared_input_or_code_blocked_before_calls(self):
        root = await self.prepare()
        with patch.object(replay, "_code_hashes", return_value={}):
            with self.assertRaisesRegex(replay.ReplayError, "CODE_CHANGED"):
                replay.verify_prepared(root)
        (root / "request.json").write_text("{}")
        with self.assertRaisesRegex(replay.ReplayError, "INPUT_CHANGED"):
            replay.verify_prepared(root)

    async def test_modified_snapshot_and_wrong_role_rejected(self):
        path = self.evidence / "export/v1/source.tar"
        path.write_bytes(b"wrong")
        with self.assertRaisesRegex(replay.ReplayError, "SNAPSHOT_EXPORT_MISMATCH"):
            await self.prepare()
        with self.assertRaisesRegex(replay.ReplayError, "EXACTLY_ONE_SECURITY"):
            await self.prepare(step_id=str(self.f.fixture.step.workflow_step_id))

    async def test_different_security_policy_cannot_be_silently_replayed(self):
        policy = {**self.policy, "maxCallSeconds": 4}
        with self.assertRaisesRegex(replay.ReplayError, "SECURITY_POLICY_CHANGED"):
            await self.prepare(policy=policy)

    async def test_duplicate_request_and_forged_task_binding_rejected(self):
        path = self.evidence / "export/a2a-messages.json"
        data = replay._read_json(path)
        row = data["roles"]["SECURITY"]["receipts"][0]
        data["roles"]["SECURITY"]["receipts"].append(row.copy())
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(replay.ReplayError, "ONE_INITIAL_REQUEST"):
            await self.prepare()
        data["roles"]["SECURITY"]["receipts"].pop()
        row["contextId"] = "wrong-context"
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(replay.ReplayError, "BINDING_MISMATCH"):
            await self.prepare()


if __name__ == "__main__":
    unittest.main()
