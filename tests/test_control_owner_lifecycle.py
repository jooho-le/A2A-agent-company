"""Owned control cancellation, AnyIO drain, and real unsent Developer recovery.

No production LLM/container is used. The Developer fixture uses actual SDK
ASGI, private SQLite/Git and the MCP dispatcher with fake model/daemon adapters.
"""

import asyncio
import threading
import unittest

import anyio

from orchestrator.a2a import A2AAgentClient, A2AAgentRegistry
from orchestrator.application.dispatch import PlannerRunDispatcher
from orchestrator.application.workflow_controls import WorkflowControlService
from orchestrator.core.async_control import await_owned
from orchestrator.domain import A2ATaskState, AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.infrastructure import RunDispatchConflict
import test_developer_agent as developer_fixture
import test_dispatch as dispatch_fixture
import test_owned_dispatch_controls as owner_fixture
import test_workflow_controls as control_fixture


class AwaitOwnedLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def drain_case(self, *, synchronous, anyio_scope):
        entered, cleanup, finished = (asyncio.Event() for _ in range(3))
        release, thread_finished = threading.Event(), threading.Event()
        async_release = asyncio.Event()
        effects, scopes = [], []
        loop = asyncio.get_running_loop()

        def bounded_worker():
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(3):
                raise AssertionError("fixture cleanup barrier timed out")
            effects.append("bounded transaction finished")
            thread_finished.set()

        async def bounded_async():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await async_release.wait()
                effects.append("async cleanup finished")

        worker = asyncio.create_task(asyncio.to_thread(bounded_worker) if synchronous else bounded_async())

        async def requester():
            try:
                if anyio_scope:
                    with anyio.CancelScope() as scope:
                        scopes.append(scope)
                        await await_owned(worker, cancel_on_interrupt=not synchronous)
                    self.assertTrue(scope.cancelled_caught)
                else:
                    await await_owned(worker, cancel_on_interrupt=not synchronous)
            finally:
                finished.set()

        caller = asyncio.create_task(requester())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            if anyio_scope:
                self.assertEqual(len(scopes), 1)
                scopes[0].cancel()
            else:
                caller.cancel()
            if not synchronous:
                await asyncio.wait_for(cleanup.wait(), 1)
            # Level cancellation must not spin/block this independent task,
            # and repeated asyncio cancellation must not detach owned cleanup.
            for _ in range(3):
                await asyncio.sleep(.01)
                if anyio_scope:
                    scopes[0].cancel()
                else:
                    caller.cancel()
                self.assertFalse(caller.done())
                self.assertFalse(finished.is_set())
                self.assertEqual(effects, [])
        finally:
            release.set()
            async_release.set()
            if anyio_scope:
                await asyncio.wait_for(caller, 2)
            else:
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(caller, 2)
            await asyncio.gather(worker, return_exceptions=True)
        self.assertTrue(finished.is_set())
        self.assertEqual(effects, ["bounded transaction finished" if synchronous else "async cleanup finished"])
        if synchronous:
            self.assertTrue(thread_finished.is_set())

    async def test_anyio_level_cancel_drains_thread_without_busy_loop_or_replay(self):
        await self.drain_case(synchronous=True, anyio_scope=True)

    async def test_anyio_level_cancel_drains_async_finally_before_leaving_scope(self):
        await self.drain_case(synchronous=False, anyio_scope=True)

    async def test_repeated_asyncio_cancel_drains_thread_before_propagation(self):
        await self.drain_case(synchronous=True, anyio_scope=False)

    async def test_repeated_asyncio_cancel_does_not_cancel_async_finally_twice(self):
        await self.drain_case(synchronous=False, anyio_scope=False)


class DispatcherOwnerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    setUp = owner_fixture.OwnedDispatchControlsTests.setUp
    dispatcher = owner_fixture.OwnedDispatchControlsTests.dispatcher

    def assert_owner_released(self, dispatcher):
        self.assertFalse(dispatcher.is_run_active(self.run.run_id))
        self.assertFalse(dispatcher._dispatch_tasks)
        self.assertFalse(dispatcher._dispatch_tokens)
        self.assertFalse(dispatcher._control_stops)
        token = self.repository.acquire_control(self.run.run_id)
        self.repository.release_control(self.run.run_id, token)

    async def test_spontaneous_preparation_cancellation_propagates_not_control_success(self):
        client = dispatch_fixture.FakePlannerClient("TASK_STATE_COMPLETED")

        async def prepare(_):
            raise asyncio.CancelledError()

        dispatcher = self.dispatcher(client, before_dispatch=prepare)
        with self.assertRaises(asyncio.CancelledError):
            await dispatcher.dispatch_planner(self.run.run_id)
        self.assert_owner_released(dispatcher)
        self.assertFalse(client.sent)

    async def test_explicit_control_stop_drains_child_before_lease_can_be_reclaimed(self):
        entered, cleanup, release, finished = (asyncio.Event() for _ in range(4))
        client = dispatch_fixture.FakePlannerClient("TASK_STATE_COMPLETED")

        async def prepare(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup.set()
                await release.wait()
                finished.set()

        dispatcher = self.dispatcher(client, before_dispatch=prepare)
        owner = asyncio.create_task(dispatcher.dispatch_planner(self.run.run_id))
        stopper = None
        try:
            await asyncio.wait_for(entered.wait(), 1)
            stopper = asyncio.create_task(dispatcher.stop_active_dispatch(self.run.run_id))
            await asyncio.wait_for(cleanup.wait(), 1)
            self.assertTrue(dispatcher.is_run_active(self.run.run_id))
            self.assertFalse(stopper.done())
            self.assertFalse(finished.is_set())
            with self.assertRaises(RunDispatchConflict):
                self.repository.acquire_control(self.run.run_id)
        finally:
            release.set()
            if stopper is not None:
                await asyncio.wait_for(stopper, 2)
            await asyncio.wait_for(owner, 2)
        self.assertTrue(finished.is_set())
        self.assert_owner_released(dispatcher)
        self.assertFalse(client.card_resolved)
        self.assertFalse(client.sent)


class ResumedOwnerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = control_fixture.WorkflowControlTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    async def test_resumed_known_working_task_can_be_canceled_after_local_poller_drain(self):
        fixture = self.fixture
        entered, release, drained = (asyncio.Event() for _ in range(3))

        async def poll(task_id):
            entered.set()
            try:
                await release.wait()
                return fixture.client.task(task_id, "TASK_STATE_INPUT_REQUIRED")
            finally:
                drained.set()

        fixture.client.state = "TASK_STATE_WORKING"
        fixture.client.get_task = poll
        run, step = fixture.create(paused=True, state=A2ATaskState.INPUT_REQUIRED,
                                  step_status=WorkflowStepStatus.WAITING_INPUT)
        owner = asyncio.create_task(fixture.service.resume(run.run_id, input_data={"answer": "same frozen scope"}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            observed = fixture.repository.list_steps(run.run_id)[0]
            self.assertEqual((observed.status, observed.a2a_task_state),
                             (WorkflowStepStatus.RUNNING, A2ATaskState.WORKING))
            self.assertTrue(fixture.dispatcher.is_run_active(run.run_id))
            real_cancel = fixture.client.cancel_task

            async def cancel_remote(task_id):
                self.assertTrue(drained.is_set(), "remote cancellation raced the local observer")
                return await real_cancel(task_id)

            fixture.client.cancel_task = cancel_remote
            result = await fixture.service.cancel(run.run_id, "USER_CANCELLED")
        finally:
            release.set()
            with self.assertRaises(RunDispatchConflict):
                await asyncio.wait_for(owner, 2)
        self.assertEqual(result.status, WorkflowStatus.ABORTED)
        self.assertIsNone(result.verdict)
        self.assertEqual(fixture.client.canceled, [step.a2a_task_id])
        self.assertEqual(len(fixture.client.continued), 1)
        self.assertFalse(fixture.dispatcher.is_run_active(run.run_id))


class UnsentDeveloperRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = developer_fixture.DeveloperAgentTests()
        # Borrow its fixture only, not an uninitialized IsolatedAsyncio runner.
        # Register cleanups with this running TestCase so env/temp cleanup is
        # not silently swallowed by the borrowed object's doCleanups().
        self.fixture.addCleanup = self.addCleanup
        self.fixture.setUp()

    async def test_canonical_initial_unsent_payload_completes_actual_sdk_developer_once(self):
        fixture = self.fixture
        provider = fixture.ready_provider()
        deadline = fixture.budget.deadline_monotonic
        async with fixture.client_for(fixture.executor(provider)) as (_, http_client):
            url = str(http_client.base_url)
            # QA/Security aren't present in this fixture; no full Run SUCCESS
            # is inferred from the completed measured Developer candidate.
            registry = A2AAgentRegistry({role: url if role is AgentRole.DEVELOPER else None for role in AgentRole})
            factory = lambda _: A2AAgentClient(url, httpx_client=http_client)
            dispatcher = PlannerRunDispatcher(fixture.repository, registry, client_factory=factory)
            service = WorkflowControlService(fixture.repository, registry, dispatcher, client_factory=factory)
            result = await service.resume(fixture.run.run_id, recover=True)
            step = next(item for item in fixture.repository.list_steps(fixture.run.run_id)
                        if item.agent_role is AgentRole.DEVELOPER)
            task = await fixture.poll(http_client, step.a2a_task_id, "TASK_STATE_COMPLETED")
            output = fixture.parse_completed(task)
        self.assertEqual(step.status, WorkflowStepStatus.SUCCEEDED)
        self.assertEqual(step.a2a_task_state, A2ATaskState.COMPLETED)
        self.assertEqual(step.attempt, 0)
        self.assertEqual(output.source.code_version, 1)
        self.assertEqual(fixture.budget.deadline_monotonic, deadline)
        self.assertEqual(fixture.budget.model_calls, 2)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual([name for name, _ in fixture.tool_calls()], ["write_source_file", "run_build"])
        self.assertEqual(len(fixture.docker.commands("start")), 1)
        self.assertEqual(set(step.output_artifact_ids),
            {output.source.artifact_id, output.change_report.artifact_id, output.build_report.artifact_id})
        self.assertIs(result.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertFalse(dispatcher.is_run_active(fixture.run.run_id))
        events, _ = fixture.repository.list_events(fixture.run.run_id, limit=100, offset=0)
        self.assertEqual(sum(event.event_type == "A2A_MESSAGE_SENT"
            and event.workflow_step_id == step.workflow_step_id for event in events), 1)


if __name__ == "__main__":
    unittest.main()
