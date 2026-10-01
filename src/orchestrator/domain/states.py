from enum import Enum


class WorkflowStatus(str, Enum):
    """Project-level state for one complete user-request run."""

    RECEIVED = "RECEIVED"
    PLANNING = "PLANNING"
    WAITING_INPUT = "WAITING_INPUT"
    IMPLEMENTING = "IMPLEMENTING"
    SNAPSHOT_READY = "SNAPSHOT_READY"
    VALIDATING = "VALIDATING"
    FIX_REQUIRED = "FIX_REQUIRED"
    FIXING = "FIXING"
    REVALIDATING = "REVALIDATING"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    FINISHED = "FINISHED"
    ABORTED = "ABORTED"


class FinalVerdict(str, Enum):
    """Final product outcome; deliberately separate from workflow status."""

    SUCCESS = "SUCCESS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class A2ATaskState(str, Enum):
    """A2A task state as received from an Agent Server."""

    UNSPECIFIED = "TASK_STATE_UNSPECIFIED"
    SUBMITTED = "TASK_STATE_SUBMITTED"
    WORKING = "TASK_STATE_WORKING"
    COMPLETED = "TASK_STATE_COMPLETED"
    FAILED = "TASK_STATE_FAILED"
    CANCELED = "TASK_STATE_CANCELED"
    INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
    REJECTED = "TASK_STATE_REJECTED"
    AUTH_REQUIRED = "TASK_STATE_AUTH_REQUIRED"


class AgentRole(str, Enum):
    """Known agent roles that can own a workflow step."""

    PLANNER = "PLANNER"
    DEVELOPER = "DEVELOPER"
    QA = "QA"
    SECURITY = "SECURITY"


class WorkflowStepStatus(str, Enum):
    """Orchestrator's logical step status, not an A2A task state."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    WAITING_INPUT = "WAITING_INPUT"
    CANCELED = "CANCELED"
