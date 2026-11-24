"""Stage36 real SDK continuation and bounded Host factory cancellation.

The LLM and Docker adapters are fake; Git/SQLite/ASGI and the MCP dispatcher
operate on inert private fixtures. No production provider/container is used.
"""

import asyncio
import inspect
import json
import threading
import unittest

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.runtime.developer import _host_factory
from agents.runtime.planner import _host_factory as planner_host_factory
import test_developer_agent as developer_fixture
import test_developer_fix_agent as fix_fixture
import test_planner_agent as planner_fixture
from test_llm_runtime import FakeProvider


class HostFactoryCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_source_roles_and_planner_share_one_drained_factory(self):
        from agents.runtime.qa import _host_factory as qa_factory
        from agents.runtime.security import _host_factory as security_factory
        self.assertIs(_host_factory, planner_host_factory)
        self.assertIs(_host_factory, qa_factory)
        self.assertIs(_host_factory, security_factory)
        sentinel = object()
        self.assertIs(await _host_factory(lambda argument: argument, sentinel), sentinel)

    async def test_async_factory_and_sync_returned_awaitable_remain_supported(self):
        sentinel = object()

        async def load(argument):
            await asyncio.sleep(0)
            return argument

        self.assertIs(await _host_factory(load, sentinel), sentinel)
        self.assertIs(await _host_factory(lambda argument: load(argument), sentinel), sentinel)

    async def cancellation_case(self, *, error=False, coroutine=False):
        entered, release, finished = (threading.Event() for _ in range(3))
        invoked, awaitables, effects = [], [], []

        async def deferred():
            effects.append("must not begin after cancellation")

        def factory(argument):
            invoked.append(argument)
            entered.set()
            if not release.wait(3):
                raise AssertionError("fixture cleanup barrier timed out")
            try:
                effects.append("bounded worker finished")
                if error:
                    raise ValueError("PRIVATE_HOST_FACTORY_ERROR")
                if coroutine:
                    result = deferred()
                    awaitables.append(result)
                    return result
                return object()
            finally:
                finished.set()

        task = asyncio.create_task(_host_factory(factory, "host-only-argument"))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            task.cancel()
            for _ in range(3):
                await asyncio.sleep(.01)
                self.assertFalse(task.done(), "cancellation detached a still running Host worker")
                task.cancel()
            self.assertFalse(finished.is_set())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertTrue(finished.is_set())
        self.assertEqual(invoked, ["host-only-argument"])
        self.assertEqual(effects, ["bounded worker finished"])
        if coroutine:
            self.assertEqual(inspect.getcoroutinestate(awaitables[0]), inspect.CORO_CLOSED)

    async def test_repeated_cancel_drains_sync_factory_before_propagation(self):
        await self.cancellation_case()

    async def test_worker_failure_during_cancel_cannot_replace_cancellation_or_retry(self):
        await self.cancellation_case(error=True)

    async def test_cancel_closes_unstarted_sync_returned_loader_without_running_it(self):
        await self.cancellation_case(coroutine=True)

    async def test_repeated_cancel_also_drains_async_factory_cleanup_once(self):
        entered, cleanup_entered, release, finished = (asyncio.Event() for _ in range(4))
        calls = []

        async def factory(argument):
            calls.append(argument)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_entered.set()
                await release.wait()
                finished.set()

        task = asyncio.create_task(_host_factory(factory, "bounded-async-reader"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.wait_for(cleanup_entered.wait(), 1)
            for _ in range(3):
                task.cancel()
                await asyncio.sleep(.01)
                self.assertFalse(task.done())
                self.assertFalse(finished.is_set())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertTrue(finished.is_set())
        self.assertEqual(calls, ["bounded-async-reader"])


class PlannerContinuationTests(unittest.IsolatedAsyncioTestCase):
    setUp = planner_fixture.PlannerAgentTests.setUp
    context_factory = planner_fixture.PlannerAgentTests.context_factory
    host_context = planner_fixture.PlannerAgentTests.host_context
    executor = planner_fixture.PlannerAgentTests.executor
    headers = staticmethod(planner_fixture.PlannerAgentTests.headers)
    client_for = planner_fixture.PlannerAgentTests.client_for
    payload = planner_fixture.PlannerAgentTests.payload
    wire = planner_fixture.PlannerAgentTests.wire
    send = planner_fixture.PlannerAgentTests.send
    poll = planner_fixture.PlannerAgentTests.poll
    draft = planner_fixture.PlannerAgentTests.draft
    response = planner_fixture.PlannerAgentTests.response
    parse_completed = planner_fixture.PlannerAgentTests.parse_completed

    async def test_input_then_out_of_band_auth_continuations_keep_frozen_task_and_budget(self):
        provider = FakeProvider(
            self.response(self.draft(kind="INPUT_REQUIRED", questions=["화면 종류를 알려주세요."])),
            LLMRuntimeError(LLMErrorCode.AUTH), self.response())
        deadline = self.budget.deadline_monotonic
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            waiting = await self.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            metadata = self.metadata.model_copy(update={"attempt": 1})
            second = await self.send(client, self.wire(payload={"answer": "기본 화면"}, metadata=metadata,
                task_id=waiting["id"], context_id=waiting["contextId"]))
            auth = await self.poll(client, second["id"], "TASK_STATE_AUTH_REQUIRED")
            self.assertEqual(auth["status"]["message"]["parts"][0]["data"], {"code": "LLM_AUTH_REQUIRED"})
            self.assertFalse(auth.get("artifacts"))
            metadata = self.metadata.model_copy(update={"attempt": 2})
            third = await self.send(client, self.wire(payload={"authenticationConfigured": True}, metadata=metadata,
                task_id=auth["id"], context_id=auth["contextId"]))
            completed = await self.poll(client, third["id"], "TASK_STATE_COMPLETED")
        self.assertEqual((completed["id"], completed["contextId"]), (first["id"], first["contextId"]))
        self.assertEqual(self.budget.deadline_monotonic, deadline)
        self.assertEqual(self.budget.model_calls, 3)
        self.assertEqual([item.attempt for item in self.context_calls], [0, 1, 2])
        self.assertEqual(self.parse_completed(completed, metadata).project_artifact_id, self.artifact_id)
        envelope = json.loads(json.loads(provider.requests[2].input_items_json)[0]["content"])
        self.assertEqual(envelope["taskInput"]["runConfiguration"], self.configuration.to_artifact_json())
        self.assertEqual(envelope["taskInput"]["clarifications"],
            [{"answer": "기본 화면"}, {"authenticationConfigured": True}])

    async def test_answer_cannot_supply_candidate_ids_or_fix_control_fields(self):
        provider = FakeProvider(self.response(self.draft(kind="INPUT_REQUIRED", questions=["추가 설명?"])))
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            waiting = await self.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            second = await self.send(client, self.wire(payload={"answer": "진행", "projectArtifactIds": [],
                "codeVersion": 2, "fixRequest": {}, "fixAttempt": 1},
                metadata=self.metadata.model_copy(update={"attempt": 1}),
                task_id=waiting["id"], context_id=waiting["contextId"]))
            rejected = await self.poll(client, second["id"], "TASK_STATE_REJECTED")
        self.assertEqual(rejected["status"]["message"]["parts"][0]["data"], {"code": "PLANNER_INPUT_INVALID"})
        self.assertEqual(len(provider.requests), 1)
        self.assertFalse(rejected.get("artifacts"))


class DeveloperContinuationTests(unittest.IsolatedAsyncioTestCase):
    setUp = developer_fixture.DeveloperAgentTests.setUp
    git = developer_fixture.DeveloperAgentTests.git
    peer_client = developer_fixture.DeveloperAgentTests.peer_client
    context_factory = developer_fixture.DeveloperAgentTests.context_factory
    services_factory = developer_fixture.DeveloperAgentTests.services_factory
    executor = developer_fixture.DeveloperAgentTests.executor
    headers = staticmethod(developer_fixture.DeveloperAgentTests.headers)
    client_for = developer_fixture.DeveloperAgentTests.client_for
    payload = developer_fixture.DeveloperAgentTests.payload
    wire = developer_fixture.DeveloperAgentTests.wire
    send = developer_fixture.DeveloperAgentTests.send
    poll = developer_fixture.DeveloperAgentTests.poll
    draft_response = developer_fixture.DeveloperAgentTests.draft_response
    write_response = developer_fixture.DeveloperAgentTests.write_response
    ready_provider = developer_fixture.DeveloperAgentTests.ready_provider
    run_to = developer_fixture.DeveloperAgentTests.run_to
    parse_completed = developer_fixture.DeveloperAgentTests.parse_completed
    tool_calls = developer_fixture.DeveloperAgentTests.tool_calls
    status_code = developer_fixture.DeveloperAgentTests.status_code
    update_step_for_resume = developer_fixture.DeveloperAgentTests.update_step_for_resume

    async def test_auth_after_source_write_preserves_bytes_and_blocks_model_replay(self):
        provider = FakeProvider(self.write_response(), LLMRuntimeError(LLMErrorCode.AUTH),
                                self.write_response(call_id="must-not-replay"), self.draft_response())
        deadline = self.budget.deadline_monotonic
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            auth = await self.poll(client, first["id"], "TASK_STATE_AUTH_REQUIRED")
            written = (self.source / "signup.py").read_bytes()
            metadata = self.metadata.model_copy(update={"attempt": 1})
            self.update_step_for_resume(metadata, auth)
            second = await self.send(client, self.wire(payload={"authenticationConfigured": True}, metadata=metadata,
                task_id=auth["id"], context_id=auth["contextId"]))
            failed = await self.poll(client, second["id"], "TASK_STATE_FAILED")
        self.assertEqual((failed["id"], failed["contextId"]), (first["id"], first["contextId"]))
        self.assertEqual(self.status_code(failed), "DEVELOPER_CHECKPOINT_BASELINE_MISMATCH")
        self.assertEqual((self.source / "signup.py").read_bytes(), written)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.baseline_commit)
        self.assertEqual(self.budget.deadline_monotonic, deadline)
        self.assertEqual(self.budget.model_calls, 2)
        self.assertEqual([name for name, _ in self.tool_calls()], ["write_source_file"])
        self.assertFalse(failed.get("artifacts"))
        self.assertEqual(self.docker.calls, [])


class DeveloperFixContinuationTests(unittest.IsolatedAsyncioTestCase):
    # The Fix fixture borrows helpers, not TestCase inheritance/test methods.
    setUp = fix_fixture.DeveloperFixTests.setUp
    git = fix_fixture.DeveloperFixTests.git
    peer_client = fix_fixture.DeveloperFixTests.peer_client
    context_factory = fix_fixture.DeveloperFixTests.context_factory
    services_factory = fix_fixture.DeveloperFixTests.services_factory
    executor = fix_fixture.DeveloperFixTests.executor
    headers = staticmethod(fix_fixture.DeveloperFixTests.headers)
    client_for = fix_fixture.DeveloperFixTests.client_for
    payload = fix_fixture.DeveloperFixTests.payload
    wire = fix_fixture.DeveloperFixTests.wire
    send = fix_fixture.DeveloperFixTests.send
    poll = fix_fixture.DeveloperFixTests.poll
    draft_response = fix_fixture.DeveloperFixTests.draft_response
    write_response = fix_fixture.DeveloperFixTests.write_response
    ready_provider = fix_fixture.DeveloperFixTests.ready_provider
    run_to = fix_fixture.DeveloperFixTests.run_to
    parse_completed = fix_fixture.DeveloperFixTests.parse_completed
    tool_calls = fix_fixture.DeveloperFixTests.tool_calls
    status_code = fix_fixture.DeveloperFixTests.status_code
    prepare_fix = fix_fixture.DeveloperFixTests.prepare_fix
    observe_completed = fix_fixture.DeveloperFixTests.observe_completed
    start_fix = fix_fixture.DeveloperFixTests.start_fix
    update_step_for_resume = developer_fixture.DeveloperAgentTests.update_step_for_resume

    async def test_fix_input_then_auth_resume_changes_only_attempt_not_candidate_or_budget(self):
        await self.prepare_fix()
        provider = FakeProvider(
            self.draft_response(kind="INPUT_REQUIRED", questions=["수정 범위 설명?"]),
            LLMRuntimeError(LLMErrorCode.AUTH), self.write_response(content="def signup():\n    return 'fixed-two'\n"),
            self.draft_response())
        deadline, calls = self.budget.deadline_monotonic, self.budget.model_calls
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            waiting = await self.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            metadata = self.metadata.model_copy(update={"attempt": 1})
            self.update_step_for_resume(metadata, waiting)
            second = await self.send(client, self.wire(payload={"answer": "보호된 Issue만 수정"}, metadata=metadata,
                task_id=waiting["id"], context_id=waiting["contextId"]))
            auth = await self.poll(client, second["id"], "TASK_STATE_AUTH_REQUIRED")
            metadata = self.metadata.model_copy(update={"attempt": 2})
            self.update_step_for_resume(metadata, auth)
            third = await self.send(client, self.wire(payload={"authenticationConfigured": True}, metadata=metadata,
                task_id=auth["id"], context_id=auth["contextId"]))
            completed = await self.poll(client, third["id"], "TASK_STATE_COMPLETED")
        output = self.parse_completed(completed, metadata)
        self.assertEqual((completed["id"], completed["contextId"]), (first["id"], first["contextId"]))
        self.assertNotEqual(completed["id"], self.previous_task["id"])
        self.assertEqual(completed["contextId"], self.previous_task["contextId"])
        self.assertEqual((metadata.attempt, output.source.code_version, self.run.fix_attempt), (2, 2, 1))
        self.assertEqual(output.source.previous_artifact_id, self.previous.source.artifact_id)
        self.assertEqual(self.budget.deadline_monotonic, deadline)
        self.assertEqual(self.budget.model_calls - calls, 4)
        self.assertEqual([item.attempt for item in self.context_calls], [0, 1, 2])
        envelope = json.loads(json.loads(provider.requests[2].input_items_json)[0]["content"])
        self.assertEqual(envelope["taskInput"]["fixRequest"], self.payload()["fixRequest"])
        self.assertEqual(envelope["taskInput"]["runConfiguration"], self.configuration.to_artifact_json())

    async def test_fix_source_write_then_input_preserves_uncommitted_progress_without_replay(self):
        await self.prepare_fix()
        provider = FakeProvider(self.write_response(content="def signup():\n    return 'fix-partial'\n"),
            self.draft_response(kind="INPUT_REQUIRED", questions=["추가 확인?"]), self.draft_response())
        previous_count = len(self.docker.calls)
        deadline, calls = self.budget.deadline_monotonic, self.budget.model_calls
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            waiting = await self.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            written = (self.source / "signup.py").read_bytes()
            metadata = self.metadata.model_copy(update={"attempt": 1})
            self.update_step_for_resume(metadata, waiting)
            second = await self.send(client, self.wire(payload={"answer": "진행"}, metadata=metadata,
                task_id=waiting["id"], context_id=waiting["contextId"]))
            failed = await self.poll(client, second["id"], "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(failed), "DEVELOPER_CHECKPOINT_BASELINE_MISMATCH")
        self.assertEqual((self.source / "signup.py").read_bytes(), written)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.baseline_commit)
        self.assertEqual(self.budget.deadline_monotonic, deadline)
        self.assertEqual(self.budget.model_calls - calls, 2)
        self.assertEqual([name for name, _ in self.tool_calls()], ["write_source_file"])
        self.assertEqual(len(self.docker.calls), previous_count)
        self.assertFalse(failed.get("artifacts"))
        self.assertEqual((self.run.fix_attempt, self.run.code_version), (1, 1))


if __name__ == "__main__":
    unittest.main()
