import hashlib
import json
import unittest
from pathlib import Path
from uuid import uuid4

from a2a.types import Task
from google.protobuf.json_format import ParseDict

from orchestrator.application import (
    A2ATaskProtocolError,
    A2ATaskRunner,
    TaskPollingPolicy,
    TaskRunDisposition,
)
from orchestrator.domain import (
    A2ATaskState,
    AgentContext,
    AgentRole,
    CodeSnapshotArtifact,
    GitObjectFormat,
    SnapshotHandoff,
    TraceEvent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
)


def make_task(
    task_id: str,
    state: str,
    *,
    context_id: str | None = None,
    artifact_ids: tuple[str, ...] = (),
) -> Task:
    value: dict[str, object] = {
        "id": task_id,
        "status": {"state": state},
        "artifacts": [{"artifactId": artifact_id} for artifact_id in artifact_ids],
    }
    if context_id is not None:
        value["contextId"] = context_id
    return ParseDict(value, Task())


class FakeAgentClient:
    def __init__(self, submitted: tuple[Task, ...], polled: tuple[Task, ...]) -> None:
        self.submitted = list(submitted)
        self.polled = list(polled)
        self.send_calls: list[dict[str, object]] = []
        self.continue_calls: list[dict[str, object]] = []
        self.snapshot_calls: list[dict[str, object]] = []
        self.get_task_ids: list[str] = []

    async def send_task(self, payload, metadata, *, context_id=None):
        self.send_calls.append(
            {"payload": payload, "metadata": metadata, "context_id": context_id}
        )
        return self.submitted.pop(0)

    async def continue_task(self, task_id, payload, metadata, *, context_id=None):
        self.continue_calls.append(
            {
                "task_id": task_id,
                "payload": payload,
                "metadata": metadata,
                "context_id": context_id,
            }
        )
        return self.submitted.pop(0)

    async def send_snapshot_handoff(self, handoff, recipient, request_text, **kwargs):
        self.snapshot_calls.append(
            {
                "handoff": handoff,
                "recipient": recipient,
                "request_text": request_text,
                **kwargs,
            }
        )
        return self.submitted.pop(0)

    async def get_task(self, task_id: str):
        self.get_task_ids.append(task_id)
        return self.polled.pop(0)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


class A2ATaskRunnerTests(unittest.IsolatedAsyncioTestCase):
    def make_run(self) -> WorkflowRun:
        return WorkflowRun(
            scenario_id=uuid4(),
            request_text="Implement and validate the requested feature",
            status=WorkflowStatus.PLANNING,
        )

    def make_step(
        self,
        run: WorkflowRun,
        role: AgentRole = AgentRole.PLANNER,
        **updates: object,
    ) -> WorkflowStep:
        values: dict[str, object] = {
            "run_id": run.run_id,
            "agent_role": role,
            "requirement_ids": [uuid4()],
        }
        values.update(updates)
        return WorkflowStep(**values)

    def make_runner(
        self,
        client: FakeAgentClient,
        clock: FakeClock | None = None,
        *,
        interval: float = 0.1,
        timeout: float = 2.0,
    ) -> A2ATaskRunner:
        clock = clock or FakeClock()
        return A2ATaskRunner(
            client,  # type: ignore[arg-type]
            policy=TaskPollingPolicy(
                interval_seconds=interval, timeout_seconds=timeout
            ),
            sleep=clock.sleep,
            clock=clock.monotonic,
        )

    async def test_submit_polls_to_terminal_and_records_step_context_and_trace(self) -> None:
        task_id = "planner/task?opaque#id"
        context_id = "planner-context::opaque"
        client = FakeAgentClient(
            submitted=(make_task(task_id, "TASK_STATE_SUBMITTED", context_id=context_id),),
            polled=(
                make_task(task_id, "TASK_STATE_WORKING", context_id=context_id),
                make_task(
                    task_id,
                    "TASK_STATE_WORKING",
                    context_id=context_id,
                    artifact_ids=("planner-artifact-opaque",),
                ),
                make_task(
                    task_id,
                    "TASK_STATE_COMPLETED",
                    context_id=context_id,
                    artifact_ids=("planner-artifact-opaque",),
                ),
            ),
        )
        run = self.make_run()
        step = self.make_step(run)
        persisted: list[tuple[WorkflowStep, AgentContext, TraceEvent]] = []

        async def observer(
            changed_step: WorkflowStep,
            context: AgentContext,
            event: TraceEvent,
        ) -> None:
            persisted.append((changed_step, context, event))

        result = await self.make_runner(client).submit_and_wait(
            run,
            step,
            agent_id="planner-agent",
            payload={"request": "Plan the work"},
            observer=observer,
        )

        self.assertEqual(result.disposition, TaskRunDisposition.COMPLETED)
        self.assertEqual(result.step.status, WorkflowStepStatus.SUCCEEDED)
        self.assertEqual(result.step.a2a_task_id, task_id)
        self.assertEqual(result.step.a2a_task_state, A2ATaskState.COMPLETED)
        self.assertEqual(result.step.agent_context_id, context_id)
        self.assertEqual(result.step.a2a_artifact_ids, ["planner-artifact-opaque"])
        self.assertEqual(result.step.output_artifact_ids, [])
        self.assertEqual(result.agent_context.agent_id, "planner-agent")
        self.assertEqual(result.agent_context.agent_context_id, context_id)
        self.assertEqual(result.agent_context.latest_a2a_task_id, task_id)
        self.assertEqual(run.status, WorkflowStatus.PLANNING)
        self.assertEqual(result.poll_count, 3)
        self.assertEqual(
            [event.event_type for event in result.events],
            [
                "A2A_MESSAGE_SENT",
                "A2A_TASK_RECEIVED",
                "A2A_TASK_STATE_CHANGED",
                "A2A_TASK_UPDATED",
                "A2A_TASK_STATE_CHANGED",
            ],
        )
        self.assertEqual(len(persisted), 5)
        self.assertIsNone(client.send_calls[0]["context_id"])
        sent_metadata = client.send_calls[0]["metadata"].to_a2a_json()
        self.assertEqual(sent_metadata["runId"], str(run.run_id))
        self.assertEqual(sent_metadata["workflowStepId"], str(step.workflow_step_id))
        self.assertEqual(client.get_task_ids, [task_id, task_id, task_id])

    async def test_agent_context_is_reused_only_within_its_agent_mapping(self) -> None:
        run = self.make_run()
        client = FakeAgentClient(
            submitted=(make_task("qa-task", "TASK_STATE_COMPLETED"),), polled=()
        )
        runner = self.make_runner(client)
        step = self.make_step(run, AgentRole.QA)
        known_context = AgentContext(
            run_id=run.run_id,
            agent_id="qa-agent",
            agent_context_id="qa-context/opaque",
            latest_a2a_task_id="previous-qa-task",
        )

        result = await runner.submit_and_wait(
            run,
            step,
            agent_id="qa-agent",
            payload={"request": "Run QA"},
            agent_context=known_context,
        )
        self.assertEqual(client.send_calls[0]["context_id"], "qa-context/opaque")
        self.assertEqual(result.step.agent_context_id, "qa-context/opaque")

        another_step = self.make_step(run, AgentRole.SECURITY)
        with self.assertRaises(A2ATaskProtocolError):
            await runner.submit_and_wait(
                run,
                another_step,
                agent_id="security-agent",
                payload={"request": "Scan"},
                agent_context=known_context,
            )
        self.assertEqual(len(client.send_calls), 1)

    async def test_failed_agent_task_is_not_mapped_to_product_verdict(self) -> None:
        run = self.make_run()
        client = FakeAgentClient(
            submitted=(make_task("developer-task", "TASK_STATE_FAILED"),), polled=()
        )
        result = await self.make_runner(client).submit_and_wait(
            run,
            self.make_step(run, AgentRole.DEVELOPER),
            agent_id="developer-agent",
            payload={"request": "Implement"},
        )

        self.assertEqual(result.disposition, TaskRunDisposition.AGENT_FAILED)
        self.assertEqual(result.step.status, WorkflowStepStatus.FAILED)
        self.assertIsNone(run.verdict)
        self.assertEqual(run.status, WorkflowStatus.PLANNING)

    async def test_interrupted_and_rejected_states_keep_their_distinct_meanings(self) -> None:
        cases = (
            ("TASK_STATE_INPUT_REQUIRED", TaskRunDisposition.WAITING_INPUT, WorkflowStepStatus.WAITING_INPUT),
            ("TASK_STATE_AUTH_REQUIRED", TaskRunDisposition.HUMAN_REVIEW, WorkflowStepStatus.WAITING_INPUT),
            ("TASK_STATE_REJECTED", TaskRunDisposition.HUMAN_REVIEW, WorkflowStepStatus.FAILED),
        )
        for index, (state, disposition, step_status) in enumerate(cases):
            with self.subTest(state=state):
                run = self.make_run()
                client = FakeAgentClient(
                    submitted=(make_task(f"task-{index}", state),), polled=()
                )
                result = await self.make_runner(client).submit_and_wait(
                    run,
                    self.make_step(run),
                    agent_id="planner-agent",
                    payload={"request": "Plan"},
                )
                self.assertEqual(result.disposition, disposition)
                self.assertEqual(result.step.status, step_status)
                self.assertEqual(result.step.a2a_task_state.value, state)

    async def test_input_required_continues_same_agent_task_with_incremented_attempt(self) -> None:
        task_id = "planner-task-opaque"
        context_id = "planner-context-opaque"
        client = FakeAgentClient(
            submitted=(
                make_task(
                    task_id,
                    "TASK_STATE_INPUT_REQUIRED",
                    context_id=context_id,
                ),
                make_task(
                    task_id,
                    "TASK_STATE_WORKING",
                    context_id=context_id,
                ),
            ),
            polled=(make_task(task_id, "TASK_STATE_COMPLETED", context_id=context_id),),
        )
        run = self.make_run()
        step = self.make_step(run)
        runner = self.make_runner(client)
        waiting = await runner.submit_and_wait(
            run,
            step,
            agent_id="planner-agent",
            payload={"request": "Plan"},
        )
        self.assertEqual(waiting.disposition, TaskRunDisposition.WAITING_INPUT)
        self.assertEqual(waiting.step.status, WorkflowStepStatus.WAITING_INPUT)

        completed = await runner.continue_after_input(
            run,
            waiting.step,
            agent_id="planner-agent",
            payload={"userInput": "Use PostgreSQL"},
            agent_context=waiting.agent_context,
        )
        self.assertEqual(completed.disposition, TaskRunDisposition.COMPLETED)
        self.assertEqual(completed.step.a2a_task_id, task_id)
        self.assertEqual(completed.step.attempt, 1)
        self.assertEqual(waiting.step.attempt, 0)
        self.assertEqual(len(client.continue_calls), 1)
        self.assertEqual(client.continue_calls[0]["task_id"], task_id)
        self.assertEqual(client.continue_calls[0]["context_id"], context_id)
        self.assertEqual(client.continue_calls[0]["metadata"].attempt, 1)
        self.assertEqual(client.get_task_ids, [task_id])

    async def test_timeout_does_not_forge_a_terminal_a2a_state_and_can_resume(self) -> None:
        clock = FakeClock()
        task_id = "worker-task-opaque"
        client = FakeAgentClient(
            submitted=(make_task(task_id, "TASK_STATE_SUBMITTED"),),
            polled=(
                make_task(task_id, "TASK_STATE_WORKING"),
                make_task(task_id, "TASK_STATE_WORKING"),
                make_task(task_id, "TASK_STATE_COMPLETED"),
            ),
        )
        run = self.make_run()
        initial_step = self.make_step(run)
        runner = self.make_runner(client, clock, interval=0.1, timeout=0.25)
        timed_out = await runner.submit_and_wait(
            run,
            initial_step,
            agent_id="planner-agent",
            payload={"request": "Plan"},
        )

        self.assertEqual(timed_out.disposition, TaskRunDisposition.POLLING_TIMEOUT)
        self.assertEqual(timed_out.step.status, WorkflowStepStatus.RUNNING)
        self.assertEqual(timed_out.step.a2a_task_state, A2ATaskState.WORKING)
        self.assertEqual(timed_out.events[-1].event_type, "A2A_POLL_TIMED_OUT")

        resumed = await self.make_runner(
            client, clock, interval=0.1, timeout=1.0
        ).resume_polling(
            run,
            timed_out.step,
            agent_id="planner-agent",
            agent_context=timed_out.agent_context,
        )
        self.assertEqual(resumed.disposition, TaskRunDisposition.COMPLETED)
        self.assertEqual(resumed.step.status, WorkflowStepStatus.SUCCEEDED)
        self.assertEqual(len(client.send_calls), 1)
        self.assertEqual(client.get_task_ids[-2:], [task_id, task_id])

    async def test_snapshot_handoff_checks_same_run_step_artifact_and_code_version(self) -> None:
        archive = b"frozen"
        snapshot = CodeSnapshotArtifact(
            artifact_version=1,
            run_id=uuid4(),
            workflow_step_id=uuid4(),
            requirement_ids=(uuid4(),),
            code_version=1,
            repository_id="a2a-agent-company",
            commit_hash="a" * 40,
            git_object_format=GitObjectFormat.SHA1,
            tree_hash="b" * 40,
            snapshot_sha256=hashlib.sha256(archive).hexdigest(),
            artifact_uri="registry://source/snapshot/1",
            container_image_digest="sha256:" + "c" * 64,
            dependency_lock_hash="sha256:" + "d" * 64,
        )
        run = WorkflowRun(
            run_id=snapshot.run_id,
            scenario_id=uuid4(),
            request_text="Verify frozen snapshot",
            status=WorkflowStatus.VALIDATING,
        )
        step = self.make_step(
            run,
            AgentRole.QA,
            code_version=snapshot.code_version,
            requirement_ids=list(snapshot.requirement_ids),
            input_artifact_ids=[snapshot.artifact_id],
        )
        client = FakeAgentClient(
            submitted=(make_task("qa-task", "TASK_STATE_COMPLETED"),), polled=()
        )

        result = await self.make_runner(client).submit_snapshot_and_wait(
            run,
            step,
            SnapshotHandoff.from_snapshot(snapshot),
            agent_id="qa-agent",
            recipient=AgentRole.QA,
            request_text="Check the acceptance tests",
        )
        self.assertEqual(result.disposition, TaskRunDisposition.COMPLETED)
        self.assertEqual(client.snapshot_calls[0]["recipient"], AgentRole.QA)
        self.assertEqual(client.snapshot_calls[0]["workflow_step_id"], step.workflow_step_id)
        self.assertEqual(
            client.snapshot_calls[0]["requirement_ids"], snapshot.requirement_ids
        )

        invalid_step = self.make_step(
            run,
            AgentRole.QA,
            code_version=2,
            input_artifact_ids=[snapshot.artifact_id],
        )
        with self.assertRaises(A2ATaskProtocolError):
            await self.make_runner(client).submit_snapshot_and_wait(
                run,
                invalid_step,
                SnapshotHandoff.from_snapshot(snapshot),
                agent_id="qa-agent",
                recipient=AgentRole.QA,
                request_text="Check",
            )

    def test_trace_event_serialization_matches_shared_schema_shape(self) -> None:
        root = Path(__file__).resolve().parents[1]
        schema = json.loads(
            (root / "schemas/project/trace_event.schema.json").read_text(
                encoding="utf-8"
            )
        )
        event = TraceEvent(
            run_id=uuid4(),
            event_type="A2A_TASK_RECEIVED",
            actor="ORCHESTRATOR",
            attempt=0,
            a2a_task_state=A2ATaskState.SUBMITTED,
        )
        wire = event.to_trace_json()
        self.assertEqual(set(wire) - set(schema["properties"]), set())
        self.assertTrue(set(schema["required"]).issubset(wire))
        self.assertFalse(schema["additionalProperties"])

    def test_poll_policy_requires_finite_positive_bounds(self) -> None:
        with self.assertRaises(ValueError):
            TaskPollingPolicy(interval_seconds=0)
        with self.assertRaises(ValueError):
            TaskPollingPolicy(timeout_seconds=float("inf"))
        with self.assertRaises(ValueError):
            TaskPollingPolicy(interval_seconds=True)


if __name__ == "__main__":
    unittest.main()
