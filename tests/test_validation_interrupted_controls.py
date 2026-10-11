"""Actual QA/Security interrupted Task controls with trusted fixture seams.

Uses SDK HTTP, SQLite, Git, measured MCP and FakeDocker; no cloud provider,
container daemon or generated Source runs on the Host. These tests do not
assert signup security-policy or product success.
"""

import asyncio
from contextlib import asynccontextmanager
import threading
import unittest
from uuid import UUID

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.runtime.security_services import SecurityRuntimeServices
from orchestrator.domain.states import AgentRole
from orchestrator.domain.validation_artifacts import FindingDisposition, ValidationOutcome
import test_qa_agent as qa_fixture
import test_security_agent as security_fixture
from test_llm_runtime import FakeProvider


class ValidationInterruptedControlsTests(unittest.IsolatedAsyncioTestCase):
    async def borrow(self, role):
        case = (qa_fixture.QAAgentTests("test_executor_constructor_inert_safe_repr_and_bad_capabilities")
                if role is AgentRole.QA else
                security_fixture.SecurityAgentTests("test_constructor_inert_safe_repr_and_bad_capabilities"))
        try:
            await case.asyncSetUp()
        finally:
            for cleanup, args, kwargs in case._cleanups:
                self.addCleanup(cleanup, *args, **kwargs)
            case._cleanups.clear()
        return case

    @staticmethod
    def resume_step(case, interrupted):
        case.metadata = case.metadata.model_copy(update={"attempt": case.metadata.attempt + 1})
        case.step = case.step.model_copy(update={"attempt": case.metadata.attempt,
            "a2a_task_id": interrupted["id"], "agent_context_id": interrupted["contextId"]})
        case._update_step(case.step)

    async def resume(self, case, client, interrupted, *, answer=None):
        self.resume_step(case, interrupted)
        return await case.send(client, case.wire(payload={"answer": "동결된 기준으로 계속하세요."}
            if answer is None else answer, task_id=interrupted["id"], context_id=interrupted["contextId"]))

    async def poll(self, case, client, identity, state):
        return await case.fixture.poll(client, identity, state)

    async def test_qa_auth_resume_keeps_one_task_source_and_budget(self):
        case = await self.borrow(AgentRole.QA)
        provider = FakeProvider(LLMRuntimeError(LLMErrorCode.AUTH), case.write_response(), case.draft_response())
        deadline, calls, tools = case.budget.deadline_monotonic, case.budget.model_calls, case.budget.tool_calls
        async with case.client_for(case.executor(provider)) as (_, client):
            first = await case.send(client)
            auth = await self.poll(case, client, first["id"], "TASK_STATE_AUTH_REQUIRED")
            self.assertFalse(auth.get("artifacts"))
            await self.resume(case, client, auth)
            completed = await self.poll(case, client, auth["id"], "TASK_STATE_COMPLETED")
        report = case.parse_completed(completed)
        self.assertEqual((completed["id"], completed["contextId"]), (auth["id"], auth["contextId"]))
        self.assertEqual(report.execution_manifest, case.source.execution_manifest())
        self.assertEqual(completed["metadata"]["attempt"], 1)
        self.assertEqual(case.budget.model_calls - calls, 3)
        self.assertEqual(case.budget.tool_calls - tools, 2)
        self.assertEqual(case.budget.deadline_monotonic, deadline)
        self.assertEqual(len(case.docker.commands("start")), 1)
        self.assertEqual(case.repository.get_run(case.run.run_id).fix_attempt, 0)

    async def test_security_auth_resume_reruns_approved_scan_with_new_measured_receipt(self):
        case = await self.borrow(AgentRole.SECURITY)
        case.set_scan_report(findings=True)
        provider = FakeProvider(LLMRuntimeError(LLMErrorCode.AUTH), case.draft_response())
        deadline, calls, tools = case.budget.deadline_monotonic, case.budget.model_calls, case.budget.tool_calls
        async with case.client_for(case.executor(provider)) as (_, client):
            first = await case.send(client)
            auth = await self.poll(case, client, first["id"], "TASK_STATE_AUTH_REQUIRED")
            await self.resume(case, client, auth)
            completed = await self.poll(case, client, auth["id"], "TASK_STATE_COMPLETED")
        report = case.parse_completed(completed)
        self.assertEqual((completed["id"], completed["contextId"]), (auth["id"], auth["contextId"]))
        self.assertEqual(report.execution_manifest, case.source.execution_manifest())
        self.assertEqual(case.budget.deadline_monotonic, deadline)
        self.assertEqual((case.budget.model_calls - calls, case.budget.tool_calls - tools), (2, 2))
        self.assertEqual(len(case.docker.commands("start")), 2)
        self.assertTrue(all(item.outcome is ValidationOutcome.UNVERIFIED and item.tool_evidence is None
                            for item in report.requirement_results))
        self.assertEqual([item.disposition for item in report.findings], [FindingDisposition.SUSPECTED])
        with case.repository._connection() as connection:
            rows = connection.execute("SELECT execution_manifest_id FROM security_scan_execution_records WHERE run_id=?",
                                      (str(case.run.run_id),)).fetchall()
        self.assertEqual(len(rows), 2)
        receipts = [case.scan_store.get(case.run.run_id, UUID(row[0])) for row in rows]
        self.assertEqual(len({receipt.execution_id for receipt in receipts}), 2)
        self.assertTrue(all(receipt.execution_manifest == case.source.execution_manifest()
                            and receipt.workflow_step_id == case.step.workflow_step_id for receipt in receipts))
        self.assertEqual(case.repository.get_run(case.run.run_id).fix_attempt, 0)

    async def test_auth_resume_cannot_reset_unknown_token_usage_under_cap(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            with self.subTest(role=role):
                case = await self.borrow(role)
                case.budget._limits = case.budget.limits.model_copy(update={"max_total_tokens": 100000})
                ready = case.draft_response()
                provider = FakeProvider(LLMRuntimeError(LLMErrorCode.AUTH), ready)
                deadline = case.budget.deadline_monotonic
                async with case.client_for(case.executor(provider)) as (_, client):
                    first = await case.send(client)
                    auth = await self.poll(case, client, first["id"], "TASK_STATE_AUTH_REQUIRED")
                    before = (case.budget.model_calls, case.budget.tool_calls, len(case.docker.calls))
                    await self.resume(case, client, auth)
                    failed = await self.poll(case, client, auth["id"], "TASK_STATE_FAILED")
                self.assertEqual(failed["status"]["message"]["parts"][0]["data"]["code"], LLMErrorCode.BUDGET.value)
                self.assertEqual((case.budget.model_calls, case.budget.tool_calls, len(case.docker.calls)), before)
                self.assertEqual(case.budget.deadline_monotonic, deadline)
                self.assertIsNone(case.budget.total_tokens)
                self.assertFalse(failed.get("artifacts"))
                self.assertEqual(len(provider.requests), 1)

    async def test_qa_input_resume_keeps_successful_test_write_and_frozen_source(self):
        case = await self.borrow(AgentRole.QA)
        provider = FakeProvider(case.write_response(),
            case.draft_response(kind="INPUT_REQUIRED", questions=["추가 설명이 필요합니다."]), case.draft_response())
        deadline, calls, tools = case.budget.deadline_monotonic, case.budget.model_calls, case.budget.tool_calls
        async with case.client_for(case.executor(provider)) as (_, client):
            first = await case.send(client)
            asked = await self.poll(case, client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            self.assertEqual(len(case.docker.commands("start")), 0)
            self.assertEqual(case.budget.tool_calls - tools, 1)
            (case.fixture.root / "source/signup.py").write_text("# unrelated mutable working copy\n", encoding="utf-8")
            await self.resume(case, client, asked)
            completed = await self.poll(case, client, asked["id"], "TASK_STATE_COMPLETED")
        report = case.parse_completed(completed)
        self.assertEqual(report.execution_manifest, case.source.execution_manifest())
        self.assertEqual((case.budget.model_calls - calls, case.budget.tool_calls - tools), (3, 2))
        self.assertEqual(case.budget.deadline_monotonic, deadline)
        self.assertEqual([name for name, _arguments in case.tool_calls()], ["write_test_file", "run_unit_tests"])

    async def test_resume_cannot_replace_frozen_snapshot_or_configuration(self):
        for role, replacement in ((AgentRole.QA, {"snapshot": {"snapshotSha256": "a" * 64}}),
                                  (AgentRole.SECURITY, {"runConfiguration": {"limits": {"runtimeBudgetMs": 9999999}}})):
            with self.subTest(role=role):
                case = await self.borrow(role)
                question = (case.draft_response(kind="INPUT_REQUIRED", questions=["추가 설명이 필요합니다."])
                            if role is AgentRole.QA else case.draft_response(kind="INPUT_REQUIRED"))
                provider = FakeProvider(question, case.draft_response())
                async with case.client_for(case.executor(provider)) as (_, client):
                    first = await case.send(client)
                    asked = await self.poll(case, client, first["id"], "TASK_STATE_INPUT_REQUIRED")
                    before = (len(provider.requests), case.budget.tool_calls, len(case.docker.calls))
                    await self.resume(case, client, asked, answer={"answer": "변경 요청", **replacement})
                    rejected = await self.poll(case, client, asked["id"], "TASK_STATE_REJECTED")
                expected = "QA_INPUT_INVALID" if role is AgentRole.QA else "SECURITY_INPUT_INVALID"
                self.assertEqual(rejected["status"]["message"]["parts"][0]["data"]["code"], expected)
                self.assertEqual((len(provider.requests), case.budget.tool_calls, len(case.docker.calls)), before)
                self.assertFalse(rejected.get("artifacts"))

    async def cancel_after_gate(self, case, client, first, entered, release, finished):
        cancellation = asyncio.create_task(client.post(f"/tasks/{first['id']}:cancel", json={},
                                                       headers=case.fixture.headers()))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            current = (await client.get(f"/tasks/{first['id']}", headers=case.fixture.headers())).json()
            self.assertNotEqual(current["status"]["state"], "TASK_STATE_CANCELED")
            self.assertFalse(current.get("artifacts"))
            self.assertFalse(cancellation.done())
            self.assertFalse(finished.is_set())
        finally:
            release.set()
        response = await asyncio.wait_for(cancellation, 5)
        self.assertEqual(response.status_code, 200, response.text)
        canceled = await self.poll(case, client, first["id"], "TASK_STATE_CANCELED")
        self.assertTrue(finished.is_set())
        self.assertFalse(canceled.get("artifacts"))
        self.assertIsNone(case.repository.get_run(case.run.run_id).verdict)
        return canceled

    async def assert_model_drain(self, role):
        case = await self.borrow(role)
        started, cleanup, release, finished = (asyncio.Event() for _ in range(4))
        async def blocked(_request):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await release.wait()
                finished.set()
        provider = FakeProvider(blocked)
        async with case.client_for(case.executor(provider)) as (_, client):
            first = await case.send(client)
            await asyncio.wait_for(started.wait(), 3)
            await self.cancel_after_gate(case, client, first, cleanup, release, finished)
        self.assertEqual(case.usages[-1].outcome, "canceled")

    async def test_qa_cancel_waits_for_model_finally_before_canceled(self):
        await self.assert_model_drain(AgentRole.QA)

    async def test_security_cancel_waits_for_model_finally_before_canceled(self):
        await self.assert_model_drain(AgentRole.SECURITY)

    async def test_qa_cancel_drains_sync_context_factory_before_canceled(self):
        case = await self.borrow(AgentRole.QA)
        entered, release, finished = (threading.Event() for _ in range(3))
        def blocked(context):
            entered.set()
            try:
                if not release.wait(5):
                    raise RuntimeError("bounded fixture timeout")
                return case.loader(context)
            finally:
                finished.set()
        provider = case.ready_provider()
        async with case.client_for(case.executor(provider, context_factory=blocked)) as (_, client):
            first = await case.send(client)
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            cancellation = asyncio.create_task(client.post(f"/tasks/{first['id']}:cancel", json={},
                                                           headers=case.fixture.headers()))
            try:
                await asyncio.sleep(.03)
                current = (await client.get(f"/tasks/{first['id']}", headers=case.fixture.headers())).json()
                self.assertNotEqual(current["status"]["state"], "TASK_STATE_CANCELED")
                self.assertFalse(cancellation.done())
                self.assertFalse(finished.is_set())
            finally:
                release.set()
            response = await asyncio.wait_for(cancellation, 5)
            self.assertEqual(response.status_code, 200, response.text)
            canceled = await self.poll(case, client, first["id"], "TASK_STATE_CANCELED")
        self.assertTrue(finished.is_set())
        self.assertFalse(canceled.get("artifacts"))
        self.assertEqual(provider.requests, [])
        self.assertEqual(case.docker.calls, [])

    async def test_security_cancel_drains_sync_semantic_verifier_before_canceled(self):
        case = await self.borrow(AgentRole.SECURITY)
        entered, release, finished = (threading.Event() for _ in range(3))
        def verifier(_execution, _decision, _measured):
            entered.set()
            try:
                if not release.wait(5):
                    raise RuntimeError("bounded fixture timeout")
                return None
            finally:
                finished.set()
        def services(_execution):
            return SecurityRuntimeServices(case.repository, case.registry, case.artifacts,
                mcp_configuration=case.mcp_configuration, client_factory=case.peer_client, proof_verifier=verifier)
        async with case.client_for(case.executor(FakeProvider(case.draft_response()), services_factory=services)) as (_, client):
            first = await case.send(client)
            self.assertTrue(await asyncio.to_thread(entered.wait, 3))
            cancellation = asyncio.create_task(client.post(f"/tasks/{first['id']}:cancel", json={},
                                                           headers=case.fixture.headers()))
            try:
                await asyncio.sleep(.03)
                current = (await client.get(f"/tasks/{first['id']}", headers=case.fixture.headers())).json()
                self.assertNotEqual(current["status"]["state"], "TASK_STATE_CANCELED")
                self.assertFalse(cancellation.done())
                self.assertFalse(finished.is_set())
            finally:
                release.set()
            response = await asyncio.wait_for(cancellation, 5)
            self.assertEqual(response.status_code, 200, response.text)
            canceled = await self.poll(case, client, first["id"], "TASK_STATE_CANCELED")
        self.assertTrue(finished.is_set())
        self.assertFalse(canceled.get("artifacts"))
        self.assertEqual(len(case.docker.commands("rm")), 1)

    async def test_qa_cancel_waits_for_mcp_teardown_before_canceled(self):
        case = await self.borrow(AgentRole.QA)
        started, cleanup, release, finished = (asyncio.Event() for _ in range(4))
        async def blocked(_request):
            started.set()
            await asyncio.Event().wait()
        @asynccontextmanager
        async def slow_peer(configuration):
            async with case.peer_client(configuration) as client:
                try:
                    yield client
                finally:
                    cleanup.set()
                    await release.wait()
                    finished.set()
        def services(execution):
            configured = case.services_factory(execution)
            configured.client_factory = slow_peer
            return configured
        async with case.client_for(case.executor(FakeProvider(blocked), services_factory=services)) as (_, client):
            first = await case.send(client)
            await asyncio.wait_for(started.wait(), 3)
            await self.cancel_after_gate(case, client, first, cleanup, release, finished)


if __name__ == "__main__":
    unittest.main()
