"""Workflow domain models and state definitions."""

from orchestrator.domain.models import AgentContext, WorkflowRun, WorkflowStep
from orchestrator.domain.trace import TraceEvent
from orchestrator.domain.constants import (
    MAX_CODE_FIX_ATTEMPTS,
    MAX_CONSECUTIVE_SAME_ISSUE_REPEATS,
    MAX_MCP_TOOL_RETRIES,
)
from orchestrator.domain.states import (
    A2ATaskState,
    AgentRole,
    FinalVerdict,
    WorkflowStatus,
    WorkflowStepStatus,
)
from orchestrator.domain.retry_policy import (
    RetryDecision,
    ToolErrorKind,
    decide_tool_retry,
    make_issue_fingerprint,
    next_consecutive_repeat_count,
    requires_human_review,
)
from orchestrator.domain.state_machine import (
    ALLOWED_TRANSITIONS,
    TransitionError,
    transition_run,
)
from orchestrator.domain.snapshot_handoff import (
    CodeSnapshotArtifact,
    ExecutionManifest,
    GitObjectFormat,
    SnapshotHandoff,
    SnapshotIntegrityError,
    SnapshotMismatchError,
    SnapshotReadGrant,
    assert_same_execution_snapshot,
    code_version_for_fix_attempt,
    verify_snapshot_archive,
)

__all__ = [
    "A2ATaskState",
    "AgentContext",
    "AgentRole",
    "FinalVerdict",
    "MAX_CODE_FIX_ATTEMPTS",
    "MAX_CONSECUTIVE_SAME_ISSUE_REPEATS",
    "MAX_MCP_TOOL_RETRIES",
    "ALLOWED_TRANSITIONS",
    "CodeSnapshotArtifact",
    "ExecutionManifest",
    "GitObjectFormat",
    "RetryDecision",
    "SnapshotHandoff",
    "SnapshotIntegrityError",
    "SnapshotMismatchError",
    "SnapshotReadGrant",
    "ToolErrorKind",
    "TraceEvent",
    "TransitionError",
    "WorkflowRun",
    "WorkflowStatus",
    "WorkflowStep",
    "WorkflowStepStatus",
    "decide_tool_retry",
    "assert_same_execution_snapshot",
    "code_version_for_fix_attempt",
    "make_issue_fingerprint",
    "next_consecutive_repeat_count",
    "requires_human_review",
    "transition_run",
    "verify_snapshot_archive",
]
