import unittest
from uuid import uuid4

from orchestrator.domain import (
    ALLOWED_TRANSITIONS,
    FinalVerdict,
    TransitionError,
    WorkflowRun,
    WorkflowStatus,
    transition_run,
)


class WorkflowStateMachineTests(unittest.TestCase):
    def make_run(self) -> WorkflowRun:
        return WorkflowRun(
            scenario_id=uuid4(),
            request_text="회원가입 기능을 구현한다.",
        )

    def advance(self, run: WorkflowRun, *targets: WorkflowStatus) -> WorkflowRun:
        for target in targets:
            run = transition_run(run, target)
        return run

    def test_normal_flow_can_finish_with_success(self) -> None:
        run = self.advance(
            self.make_run(),
            WorkflowStatus.PLANNING,
            WorkflowStatus.IMPLEMENTING,
            WorkflowStatus.SNAPSHOT_READY,
            WorkflowStatus.VALIDATING,
        )
        finished = transition_run(
            run, WorkflowStatus.FINISHED, verdict=FinalVerdict.SUCCESS
        )

        self.assertEqual(finished.status, WorkflowStatus.FINISHED)
        self.assertEqual(finished.verdict, FinalVerdict.SUCCESS)

    def test_normal_edges_match_the_development_definition(self) -> None:
        expected = {
            WorkflowStatus.RECEIVED: {WorkflowStatus.PLANNING, WorkflowStatus.ABORTED},
            WorkflowStatus.PLANNING: {
                WorkflowStatus.IMPLEMENTING,
                WorkflowStatus.WAITING_INPUT,
                WorkflowStatus.HUMAN_REVIEW,
                WorkflowStatus.ABORTED,
            },
            WorkflowStatus.WAITING_INPUT: {WorkflowStatus.ABORTED},
            WorkflowStatus.IMPLEMENTING: {
                WorkflowStatus.SNAPSHOT_READY,
                WorkflowStatus.HUMAN_REVIEW,
                WorkflowStatus.ABORTED,
            },
            WorkflowStatus.SNAPSHOT_READY: {
                WorkflowStatus.VALIDATING,
                WorkflowStatus.FIX_REQUIRED,
                WorkflowStatus.HUMAN_REVIEW,
            },
            WorkflowStatus.VALIDATING: {
                WorkflowStatus.FINISHED,
                WorkflowStatus.FIX_REQUIRED,
                WorkflowStatus.HUMAN_REVIEW,
            },
            WorkflowStatus.FIX_REQUIRED: {
                WorkflowStatus.FIXING,
                WorkflowStatus.HUMAN_REVIEW,
            },
            WorkflowStatus.FIXING: {
                WorkflowStatus.REVALIDATING,
                WorkflowStatus.HUMAN_REVIEW,
                WorkflowStatus.FINISHED,
            },
            WorkflowStatus.REVALIDATING: {
                WorkflowStatus.FINISHED,
                WorkflowStatus.FIX_REQUIRED,
                WorkflowStatus.HUMAN_REVIEW,
            },
            WorkflowStatus.HUMAN_REVIEW: {
                WorkflowStatus.FINISHED,
                WorkflowStatus.ABORTED,
            },
            WorkflowStatus.FINISHED: set(),
            WorkflowStatus.ABORTED: set(),
        }
        self.assertEqual(
            {state: set(targets) for state, targets in ALLOWED_TRANSITIONS.items()},
            expected,
        )

    def test_invalid_edge_and_terminal_mutation_are_rejected(self) -> None:
        with self.assertRaises(TransitionError):
            transition_run(self.make_run(), WorkflowStatus.IMPLEMENTING)

        finished = transition_run(
            self.advance(
                self.make_run(),
                WorkflowStatus.PLANNING,
                WorkflowStatus.IMPLEMENTING,
                WorkflowStatus.SNAPSHOT_READY,
                WorkflowStatus.VALIDATING,
            ),
            WorkflowStatus.FINISHED,
            verdict=FinalVerdict.FAIL,
        )
        with self.assertRaises(TransitionError):
            transition_run(finished, WorkflowStatus.ABORTED, termination_reason="cancel")

    def test_waiting_input_and_human_review_remember_and_resume_stage(self) -> None:
        planning = transition_run(self.make_run(), WorkflowStatus.PLANNING)
        waiting = transition_run(planning, WorkflowStatus.WAITING_INPUT)
        self.assertEqual(waiting.resume_state, WorkflowStatus.PLANNING)

        resumed = transition_run(waiting, WorkflowStatus.PLANNING)
        self.assertIsNone(resumed.resume_state)

        review = transition_run(resumed, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(review.resume_state, WorkflowStatus.PLANNING)
        resumed_again = transition_run(review, WorkflowStatus.PLANNING)
        self.assertEqual(resumed_again.status, WorkflowStatus.PLANNING)
        self.assertIsNone(resumed_again.resume_state)

    def test_explicit_cancellation_is_allowed_from_any_active_state(self) -> None:
        validating = self.advance(
            self.make_run(),
            WorkflowStatus.PLANNING,
            WorkflowStatus.IMPLEMENTING,
            WorkflowStatus.SNAPSHOT_READY,
            WorkflowStatus.VALIDATING,
        )
        with self.assertRaises(TransitionError):
            transition_run(validating, WorkflowStatus.ABORTED)

        aborted = transition_run(
            validating,
            WorkflowStatus.ABORTED,
            termination_reason="USER_CANCELLED",
        )
        self.assertEqual(aborted.status, WorkflowStatus.ABORTED)
        self.assertIsNone(aborted.verdict)
        self.assertIsNone(aborted.resume_state)

    def test_each_repair_cycle_is_counted_and_third_cycle_is_terminally_bounded(self) -> None:
        run = self.advance(
            self.make_run(),
            WorkflowStatus.PLANNING,
            WorkflowStatus.IMPLEMENTING,
            WorkflowStatus.SNAPSHOT_READY,
            WorkflowStatus.VALIDATING,
            WorkflowStatus.FIX_REQUIRED,
            WorkflowStatus.FIXING,
        )
        self.assertEqual(run.fix_attempt, 1)

        run = self.advance(
            run,
            WorkflowStatus.REVALIDATING,
            WorkflowStatus.FIX_REQUIRED,
            WorkflowStatus.FIXING,
            WorkflowStatus.REVALIDATING,
            WorkflowStatus.FIX_REQUIRED,
            WorkflowStatus.FIXING,
            WorkflowStatus.REVALIDATING,
        )
        self.assertEqual(run.fix_attempt, 3)

        with self.assertRaises(TransitionError):
            transition_run(run, WorkflowStatus.FIX_REQUIRED)
        finished = transition_run(
            run, WorkflowStatus.FINISHED, verdict=FinalVerdict.FAIL
        )
        self.assertEqual(finished.verdict, FinalVerdict.FAIL)


if __name__ == "__main__":
    unittest.main()
