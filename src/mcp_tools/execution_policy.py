"""Pure Tool retry classification; no calls, files, rollback or product verdict.

Delivery metadata is supplied by the single-call MCP client, never guessed from
exception text. A confirmation is Host evidence about one exact logical call
and attempt, not a general permission to replay writes. The caller owns the
remaining deadline, real cleanup and durable attempt ledger.
"""

from dataclasses import dataclass, field
from hashlib import sha256
import re
from uuid import RFC_4122, UUID

from agents.llm.content import sanitize_content
from mcp_tools.client import MCPClientError, MCPDeliveryState
from mcp_tools.core.catalog import _canonical_json, get_tool_contract
from mcp_tools.runtime import MCPExecutionError
from orchestrator.domain.constants import MAX_MCP_TOOL_RETRIES
from orchestrator.domain.retry_policy import RetryDecision, ToolErrorKind, decide_tool_retry
from orchestrator.domain.tool_evidence import ToolExecutionOutcome


EXECUTION_TOOL_NAMES = frozenset({
    "run_build", "run_unit_tests", "run_browser_tests", "run_security_scan",
})
WRITE_TOOL_NAMES = frozenset({"write_source_file", "write_test_file", "apply_patch"})
_KNOWN_KINDS = frozenset(kind.value for kind in ToolErrorKind) | frozenset(
    kind.value for kind in MCPExecutionError
) | {"UNKNOWN_ERROR", "CANCELLED"}
_PERMANENT_CLIENT = {
    "MCP_CLIENT_CONFIGURATION_INVALID": ToolErrorKind.INPUT_SCHEMA_ERROR,
    "MCP_CLIENT_ARGUMENTS_INVALID": ToolErrorKind.INPUT_SCHEMA_ERROR,
    "MCP_CLIENT_PERMISSION_DENIED": ToolErrorKind.PERMISSION_DENIED,
}
_PERMANENT_TOOL = {
    "PERMISSION_DENIED": ToolErrorKind.PERMISSION_DENIED,
    "SECRET_DENIED": ToolErrorKind.PERMISSION_DENIED,
    "PATH_DENIED": ToolErrorKind.PATH_TRAVERSAL,
    "TOOL_NOT_IMPLEMENTED": ToolErrorKind.UNSUPPORTED_TOOL,
}
# These replies explicitly reject input/state before applying a write. They
# remain nonretryable; they do not assert that arbitrary handler failures roll
# back. A WRITE_FAILED/PATCH_FAILED or an interrupted reply is not in this set.
_KNOWN_REJECTION_CODES = frozenset({
    "FILE_NOT_FOUND", "FILE_TOO_LARGE", "FILE_ENCODING_ERROR", "WRITE_CONFLICT",
    "BASE_MISMATCH", "SNAPSHOT_REQUIRED", "SNAPSHOT_INTEGRITY_ERROR",
    "WORKSPACE_UNAVAILABLE", "PROFILE_NOT_FOUND", "REPORT_NOT_FOUND",
})


class ExecutionPolicyError(ValueError):
    """Stable validation failure without arguments, Source or client messages."""

    code = "MCP_EXECUTION_POLICY_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _attempt(value):
    if type(value) is not int or not 0 <= value <= MAX_MCP_TOOL_RETRIES:
        raise ExecutionPolicyError()
    return value


def _logical_id(value):
    if type(value) is not UUID or value.version != 4 or value.variant != RFC_4122:
        raise ExecutionPolicyError()
    return value


def _digest(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ExecutionPolicyError()
    return value


def _contract(name):
    result = get_tool_contract(name)
    if result is None:
        raise ExecutionPolicyError()
    return result


def arguments_sha256(name, arguments) -> str:
    """Hash byte-exact bounded JSON arguments after the fixed input contract.

    Source variable assignments remain unchanged; credential literals are
    refused rather than redacted into a different call identity. No value is
    included in error messages or the returned digest's representation.
    """
    invalid = False
    try:
        contract = _contract(name)
        contract.validate_input(arguments)
        sanitize_content(arguments, source_fields=contract.source_argument_fields, reject_secrets=True)
        canonical = _canonical_json({"toolName": name, "arguments": arguments})
    except Exception:
        invalid = True
    if invalid:
        # Raise outside the except block so raw schema/credential errors do not
        # survive in __context__ even when a consumer inspects the exception.
        raise ExecutionPolicyError() from None
    return sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, kw_only=True)
class RetrySafetyConfirmation:
    logical_call_id: UUID = field(repr=False)
    attempt: int = field(repr=False)
    input_sha256: str = field(repr=False)
    cleanup_complete: bool = field(repr=False)
    result_known_not_applied: bool = field(repr=False)

    def __post_init__(self):
        _logical_id(self.logical_call_id)
        _attempt(self.attempt)
        _digest(self.input_sha256)
        if type(self.cleanup_complete) is not bool or type(self.result_known_not_applied) is not bool:
            raise ExecutionPolicyError()


@dataclass(frozen=True, kw_only=True)
class FailureClassification:
    error_kind: str
    retry_decision: RetryDecision
    retry_safe: bool
    result_unknown: bool

    def __post_init__(self):
        if (type(self.error_kind) is not str or self.error_kind not in _KNOWN_KINDS
                or type(self.retry_decision) is not RetryDecision
                or type(self.retry_safe) is not bool or type(self.result_unknown) is not bool
                or self.retry_decision is RetryDecision.RETRY and (not self.retry_safe or self.result_unknown)):
            raise ExecutionPolicyError()


def _classification(kind, decision, *, safe=False, unknown=False):
    return FailureClassification(
        error_kind=kind.value if isinstance(kind, (ToolErrorKind, MCPExecutionError)) else kind,
        retry_decision=decision, retry_safe=safe, result_unknown=unknown,
    )


def _confirmation_matches(safety, retries_used, logical_call_id, input_sha256):
    if safety is None:
        return False
    if type(safety) is not RetrySafetyConfirmation:
        raise ExecutionPolicyError()
    # Revalidate even frozen dataclasses: privileged object.__setattr__ is not
    # authorization for caller-injected or deserialized confirmation evidence.
    safety.__post_init__()
    return (safety.logical_call_id == logical_call_id and safety.attempt == retries_used
        and safety.input_sha256 == input_sha256 and safety.cleanup_complete and safety.result_known_not_applied)


def classify_failure(name, error, retries_used, *, safety=None, logical_call_id=None,
                     input_sha256=None) -> FailureClassification:
    """Classify a single attempted call without retrying or inspecting state.

    Result uncertainty wins over replay convenience. For a write, only a
    definitely NOT_SENT startup can be automatically retried; even matching
    cleanup/not-applied booleans cannot replace a real Host write-state check.
    """
    _contract(name)
    _attempt(retries_used)
    if not isinstance(error, MCPClientError):
        raise ExecutionPolicyError()
    if logical_call_id is not None:
        _logical_id(logical_call_id)
    if input_sha256 is not None:
        _digest(input_sha256)
    confirmed = _confirmation_matches(safety, retries_used, logical_call_id, input_sha256)
    # Whitelists only. Mutable metadata must not become a leaked error string or
    # an invented permission for replay; unknown metadata stays conservative.
    delivery = error.delivery_state
    if type(delivery) is not MCPDeliveryState:
        delivery = MCPDeliveryState.UNKNOWN
    tool_code = error.tool_error_code
    if type(tool_code) is not str or tool_code not in {kind.value for kind in MCPExecutionError}:
        tool_code = None
    client_code = error.code if type(error.code) is str else None
    protocol_code = error.protocol_error_code if type(error.protocol_error_code) is int else None
    write = name in WRITE_TOOL_NAMES

    permanent = _PERMANENT_CLIENT.get(client_code) or _PERMANENT_TOOL.get(tool_code)
    if protocol_code in {-32700, -32600, -32601, -32602}:
        permanent = (ToolErrorKind.UNSUPPORTED_TOOL if protocol_code == -32601 else ToolErrorKind.INPUT_SCHEMA_ERROR)
    if permanent is not None:
        return _classification(permanent, decide_tool_retry(permanent, retries_used))

    if client_code == "MCP_CLIENT_OUTPUT_INVALID":
        # A reply arrived but its contents cannot establish an execution
        # result. Do not mistake transport delivery for trustworthy evidence,
        # nor let a boolean replay confirmation bypass receipt validation.
        kind = ToolErrorKind.WRITE_RESULT_UNKNOWN if write else "UNKNOWN_ERROR"
        return _classification(kind, RetryDecision.INSPECT_STATE, unknown=True)

    startup = (tool_code == "PROCESS_STARTUP_FAILURE" or
        client_code == "MCP_CLIENT_UNAVAILABLE" and delivery is MCPDeliveryState.NOT_SENT)
    busy = tool_code == "RESOURCE_BUSY"
    if startup or busy:
        kind = ToolErrorKind.PROCESS_STARTUP_FAILURE if startup else ToolErrorKind.RESOURCE_BUSY
        approved = delivery is MCPDeliveryState.NOT_SENT or delivery is MCPDeliveryState.REPLIED and not write
        if approved:
            return _classification(kind, decide_tool_retry(kind, retries_used), safe=True)
        if write:
            return _classification(ToolErrorKind.WRITE_RESULT_UNKNOWN, RetryDecision.INSPECT_STATE, unknown=True)
        # A transport-unknown startup label cannot prove that execution stopped.
        return _classification(kind, RetryDecision.INSPECT_STATE, unknown=True)

    timeout = tool_code == "TIMEOUT" or client_code == "MCP_CLIENT_TIMEOUT"
    transport = (tool_code is None and protocol_code is None and
        client_code in {"MCP_CLIENT_UNAVAILABLE", "MCP_CLIENT_FAILED"} and delivery is MCPDeliveryState.UNKNOWN)
    if timeout or transport:
        if write:
            return _classification(ToolErrorKind.WRITE_RESULT_UNKNOWN, RetryDecision.INSPECT_STATE,
                unknown=delivery is not MCPDeliveryState.NOT_SENT)
        kind = ToolErrorKind.TOOL_TIMEOUT if timeout else ToolErrorKind.MCP_TRANSPORT_INTERRUPTED
        decision = decide_tool_retry(kind, retries_used, side_effect_safe=confirmed,
            result_known_not_applied=confirmed)
        return _classification(kind, decision, safe=confirmed, unknown=not confirmed)

    kind = tool_code if tool_code is not None else "UNKNOWN_ERROR"
    if write and delivery is not MCPDeliveryState.NOT_SENT and (
            delivery is MCPDeliveryState.UNKNOWN or tool_code not in _KNOWN_REJECTION_CODES):
        return _classification(ToolErrorKind.WRITE_RESULT_UNKNOWN, RetryDecision.INSPECT_STATE, unknown=True)
    if delivery is MCPDeliveryState.UNKNOWN:
        return _classification(kind, RetryDecision.INSPECT_STATE, unknown=True)
    return _classification(kind, RetryDecision.DO_NOT_RETRY)


def classify_success(name, data) -> tuple[ToolExecutionOutcome, str | None]:
    """A valid Tool report is PASS even when the product failed its check.

    This returns a classification hint, not the QA/Security/Run final verdict.
    Report authenticity, Artifact ownership and Manifest identity are verified
    separately by the Host receipt validator before accepting this result.
    """
    invalid = False
    try:
        contract = _contract(name)
        contract.validate_output(data)
        product_failure = None
        if name == "run_build":
            if type(data["exitCode"]) is not int or not 0 <= data["exitCode"] <= 255:
                raise ValueError
            if data["exitCode"] != 0:
                product_failure = ToolErrorKind.BUILD_CODE_FAILURE.value
        elif name in {"run_unit_tests", "run_browser_tests"}:
            count_names = ("passed", "failed", "skipped") if name == "run_unit_tests" else ("passed", "failed")
            maximum = 1000 if name == "run_unit_tests" else 100
            if (any(type(data[key]) is not int or not 0 <= data[key] <= maximum for key in ("total", *count_names))
                    or not 1 <= data["total"] <= maximum
                    or data["total"] != sum(data[key] for key in count_names)):
                raise ValueError
            if data["failed"] > 0:
                product_failure = ToolErrorKind.QA_ASSERTION_FAILURE.value
        elif name == "run_security_scan" and data["findings"]:
            product_failure = ToolErrorKind.SECURITY_FINDING.value
    except Exception:
        invalid = True
    if invalid:
        raise ExecutionPolicyError() from None
    return ToolExecutionOutcome.PASS, product_failure
