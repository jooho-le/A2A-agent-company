"""MCP Tool retry and recurring-issue policy helpers."""

from enum import Enum
from hashlib import sha256

from orchestrator.domain.constants import (
    MAX_CONSECUTIVE_SAME_ISSUE_REPEATS,
    MAX_MCP_TOOL_RETRIES,
)


class ToolErrorKind(str, Enum):
    PROCESS_STARTUP_FAILURE = "PROCESS_STARTUP_FAILURE"
    RESOURCE_BUSY = "RESOURCE_BUSY"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    MCP_TRANSPORT_INTERRUPTED = "MCP_TRANSPORT_INTERRUPTED"
    INPUT_SCHEMA_ERROR = "INPUT_SCHEMA_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PATH_TRAVERSAL = "PATH_TRAVERSAL"
    UNSUPPORTED_TOOL = "UNSUPPORTED_TOOL"
    BUILD_CODE_FAILURE = "BUILD_CODE_FAILURE"
    QA_ASSERTION_FAILURE = "QA_ASSERTION_FAILURE"
    SECURITY_FINDING = "SECURITY_FINDING"
    WRITE_RESULT_UNKNOWN = "WRITE_RESULT_UNKNOWN"


class RetryDecision(str, Enum):
    RETRY = "RETRY"
    DO_NOT_RETRY = "DO_NOT_RETRY"
    INSPECT_STATE = "INSPECT_STATE"


_RETRYABLE_WITHOUT_SIDE_EFFECT = frozenset(
    {ToolErrorKind.PROCESS_STARTUP_FAILURE, ToolErrorKind.RESOURCE_BUSY}
)
_NON_RETRYABLE = frozenset(
    {
        ToolErrorKind.INPUT_SCHEMA_ERROR,
        ToolErrorKind.PERMISSION_DENIED,
        ToolErrorKind.PATH_TRAVERSAL,
        ToolErrorKind.UNSUPPORTED_TOOL,
        ToolErrorKind.BUILD_CODE_FAILURE,
        ToolErrorKind.QA_ASSERTION_FAILURE,
        ToolErrorKind.SECURITY_FINDING,
    }
)


def decide_tool_retry(
    kind: ToolErrorKind,
    retries_used: int,
    *,
    side_effect_safe: bool = False,
    result_known_not_applied: bool = False,
) -> RetryDecision:
    """Choose a retry action; ``retries_used`` excludes the initial call."""
    try:
        kind = ToolErrorKind(kind)
    except ValueError as exc:
        raise ValueError(f"unknown Tool error kind: {kind}") from exc
    if retries_used < 0:
        raise ValueError("retries_used must be non-negative")

    if kind == ToolErrorKind.WRITE_RESULT_UNKNOWN:
        return RetryDecision.INSPECT_STATE
    if kind == ToolErrorKind.TOOL_TIMEOUT and not side_effect_safe:
        return RetryDecision.INSPECT_STATE
    if (
        kind == ToolErrorKind.MCP_TRANSPORT_INTERRUPTED
        and not result_known_not_applied
    ):
        return RetryDecision.INSPECT_STATE
    if kind in _NON_RETRYABLE:
        return RetryDecision.DO_NOT_RETRY

    retryable = kind in _RETRYABLE_WITHOUT_SIDE_EFFECT or (
        kind == ToolErrorKind.TOOL_TIMEOUT and side_effect_safe
    ) or (
        kind == ToolErrorKind.MCP_TRANSPORT_INTERRUPTED and result_known_not_applied
    )
    if not retryable or retries_used >= MAX_MCP_TOOL_RETRIES:
        return RetryDecision.DO_NOT_RETRY
    return RetryDecision.RETRY


def make_issue_fingerprint(
    requirement_id: str,
    test_id: str,
    issue_category: str,
    normalized_location: str,
) -> str:
    """SHA-256 of the four normalized issue identity fields in contract order."""
    fields = (requirement_id, test_id, issue_category, normalized_location)
    if any(not isinstance(field, str) or not field.strip() for field in fields):
        raise ValueError("issue fingerprint fields must be non-empty strings")
    raw = "".join(fields).encode("utf-8")
    return sha256(raw).hexdigest()


def next_consecutive_repeat_count(
    previous_fingerprint: str | None,
    previous_repeat_count: int,
    current_fingerprint: str,
) -> int:
    """Return repeats after a fix cycle; a changed issue resets the counter."""
    if previous_repeat_count < 0:
        raise ValueError("previous_repeat_count must be non-negative")
    if not current_fingerprint:
        raise ValueError("current_fingerprint must not be empty")
    if previous_fingerprint == current_fingerprint:
        return previous_repeat_count + 1
    return 0


def requires_human_review(consecutive_repeat_count: int) -> bool:
    if consecutive_repeat_count < 0:
        raise ValueError("consecutive_repeat_count must be non-negative")
    return consecutive_repeat_count >= MAX_CONSECUTIVE_SAME_ISSUE_REPEATS
