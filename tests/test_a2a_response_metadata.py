"""A peer cannot contradict supplied project identity on Send/Get/Continue."""

import unittest
from uuid import uuid4

from a2a.utils.errors import InvalidParamsError
from agents.api.validation import parse_workflow_metadata
from orchestrator.a2a import A2AWorkflowMetadata
from orchestrator.application import A2ATaskProtocolError, TaskRunDisposition
from orchestrator.domain import A2ATaskState, AgentContext, WorkflowStepStatus
from tests import test_a2a_tasks as fixtures


class A2AResponseMetadataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.helper = fixtures.A2ATaskRunnerTests()
        self.run = self.helper.make_run()
        self.step = self.helper.make_step(self.run, code_version=1, input_artifact_ids=[uuid4()])
        self.metadata = A2AWorkflowMetadata(
            run_id=self.run.run_id, workflow_step_id=self.step.workflow_step_id,
            scenario_id=self.run.scenario_id, attempt=self.step.attempt,
            code_version=self.step.code_version,
            requirement_ids=tuple(self.step.requirement_ids),
            project_artifact_ids=tuple(self.step.input_artifact_ids),
        ).to_a2a_json()

    def task(self, metadata=None, state="TASK_STATE_COMPLETED"):
        task = fixtures.make_task("opaque-task", state, context_id="opaque-context")
        if metadata is not None:
            task.metadata.update(metadata)
        return task

    async def submit(self, task):
        client = fixtures.FakeAgentClient((task,), ())
        return await self.helper.make_runner(client).submit_and_wait(
            self.run, self.step, agent_id="planner", payload={"request": "implement"})

    async def test_matching_complete_metadata_accepts_protojson_integral_numbers(self):
        result = await self.submit(self.task(self.metadata))
        self.assertEqual(result.disposition, TaskRunDisposition.COMPLETED)

    async def test_optional_metadata_can_be_absent(self):
        self.assertEqual((await self.submit(self.task())).disposition, TaskRunDisposition.COMPLETED)

    async def test_unrelated_metadata_extensions_remain_compatible(self):
        value = {**self.metadata, "vendorExtension": {"generation": "supported"}}
        self.assertEqual((await self.submit(self.task(value))).disposition, TaskRunDisposition.COMPLETED)

    async def test_partial_matching_echo_does_not_require_all_project_fields(self):
        self.assertEqual((await self.submit(self.task({"runId": str(self.run.run_id)}))).disposition,
                         TaskRunDisposition.COMPLETED)

    async def test_identity_echo_must_be_valid_and_match_current_step(self):
        for name in ("runId", "workflowStepId", "scenarioId"):
            for value in ("not-a-uuid", str(uuid4()), None):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(A2ATaskProtocolError, "Task project metadata"):
                        await self.submit(self.task({name: value}))

    async def test_attempt_echo_rejects_bool_string_fraction_and_wrong_integer(self):
        for value in (True, "0", .5, -1, 1, None):
            with self.subTest(value=value):
                with self.assertRaises(A2ATaskProtocolError):
                    await self.submit(self.task({"attempt": value}))

    async def test_code_version_echo_must_match_without_coercion(self):
        for value in (True, "1", 1.5, 0, 2, None):
            with self.subTest(value=value):
                with self.assertRaises(A2ATaskProtocolError):
                    await self.submit(self.task({"codeVersion": value}))

    async def test_requirement_and_artifact_echo_cannot_change_or_duplicate_ids(self):
        for name in ("requirementIds", "projectArtifactIds"):
            for value in ([str(uuid4())], [self.metadata[name][0]] * 2, [], None):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(A2ATaskProtocolError):
                        await self.submit(self.task({name: value}))

    async def test_absent_optional_id_lists_cannot_be_echoed_as_null(self):
        self.step = self.helper.make_step(self.run, requirement_ids=[])
        for name in ("requirementIds", "projectArtifactIds"):
            with self.subTest(name=name), self.assertRaises(A2ATaskProtocolError):
                await self.submit(self.task({name: None}))

    async def test_get_task_echo_is_validated_before_observer_update(self):
        client = fixtures.FakeAgentClient(
            (self.task(self.metadata, "TASK_STATE_WORKING"),),
            (self.task({"runId": str(uuid4())}),),
        )
        updates = []

        async def observer(step, context, event):
            updates.append(event.event_type)

        with self.assertRaises(A2ATaskProtocolError):
            await self.helper.make_runner(client).submit_and_wait(
                self.run, self.step, agent_id="planner", payload={"request": "implement"},
                observer=observer)
        self.assertEqual(updates, ["A2A_MESSAGE_SENT", "A2A_TASK_RECEIVED"])

    async def test_continuation_rejects_previous_attempt_echo(self):
        step = self.step.model_copy(update={
            "status": WorkflowStepStatus.WAITING_INPUT,
            "a2a_task_state": A2ATaskState.INPUT_REQUIRED,
            "a2a_task_id": "opaque-task", "agent_context_id": "opaque-context",
        })
        context = AgentContext(run_id=self.run.run_id, agent_id="planner",
                               agent_context_id="opaque-context", latest_a2a_task_id="opaque-task")
        client = fixtures.FakeAgentClient((self.task(self.metadata),), ())
        with self.assertRaises(A2ATaskProtocolError):
            await self.helper.make_runner(client).continue_after_input(
                self.run, step, agent_id="planner", agent_context=context,
                payload={"input": "continue"})
        self.assertEqual(len(client.continue_calls), 1)


class A2ARequestMetadataSchemaTests(unittest.TestCase):
    def setUp(self):
        self.value = A2AWorkflowMetadata(run_id=uuid4(), workflow_step_id=uuid4(),
                                        scenario_id=uuid4(), attempt=0).to_a2a_json()

    def test_optional_fields_are_omitted_not_null(self):
        for name in ("requirementIds", "projectArtifactIds", "codeVersion"):
            with self.subTest(name=name), self.assertRaises(InvalidParamsError):
                parse_workflow_metadata({**self.value, name: None})

    def test_metadata_requires_canonical_project_field_names(self):
        for name, alias in (("runId", "run_id"), ("workflowStepId", "workflow_step_id"),
                            ("scenarioId", "scenario_id")):
            value = dict(self.value)
            value[alias] = value.pop(name)
            with self.subTest(name=name), self.assertRaises(InvalidParamsError):
                parse_workflow_metadata(value)

    def test_supplied_id_lists_must_be_json_arrays(self):
        for name in ("requirementIds", "projectArtifactIds"):
            for value in (str(uuid4()), (str(uuid4()),), {}, True):
                with self.subTest(name=name, value=value), self.assertRaises(InvalidParamsError):
                    parse_workflow_metadata({**self.value, name: value})

    def test_valid_optional_arrays_and_protojson_integers_remain_supported(self):
        value = {**self.value, "attempt": 0.0, "codeVersion": 1.0,
                 "requirementIds": [str(uuid4())], "projectArtifactIds": [str(uuid4())]}
        metadata = parse_workflow_metadata(value)
        self.assertEqual(metadata.to_a2a_json(), value)


if __name__ == "__main__":
    unittest.main()
