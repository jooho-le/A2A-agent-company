"""Opt-in Host-owned MCP execution journal and bounded safe retry adapter.

No default Agent wiring, product verdict, automatic write reconciliation,
transport re-negotiation, Shell, or generated-code execution is added here.
"""

import asyncio
from dataclasses import dataclass, field, replace
from functools import partial
from hashlib import sha256
import inspect
import math
import time

from agents.llm.content import sanitize_content
from agents.llm.contracts import ToolCall, ToolContext, json_text, parse_json
from mcp_tools.client import BoundMCPClient, MCPClientError, MCPDeliveryState, child_parameters
from mcp_tools.core.catalog import MAX_JSON_BYTES, get_tool_contract
from mcp_tools.core.policy import MCP_PROTOCOL_VERSION, ROLE_TOOL_NAMES
from mcp_tools.execution_policy import (
    EXECUTION_TOOL_NAMES, ExecutionPolicyError, RetrySafetyConfirmation, arguments_sha256,
    classify_failure, classify_success,
)
from mcp_tools.execution_store import ToolExecutionStore, ToolEvidenceStoreError
from mcp_tools.tools.files import _run_file_operation
from orchestrator.domain.constants import MAX_MCP_TOOL_RETRIES
from orchestrator.domain.retry_policy import RetryDecision
from orchestrator.domain.tool_evidence import ToolExecutionOutcome
from orchestrator.workspaces.policy import workspace_uuid


class TrackedMCPError(RuntimeError):
    """Host-readable pointers and stable codes; never peer prose or Source."""

    def __init__(self, code="MCP_EXECUTION_FAILED", *, logical_call_id=None,
                 evidence_ref=None, retry_decision=RetryDecision.DO_NOT_RETRY):
        if code not in {
            "MCP_EXECUTION_FAILED", "MCP_EXECUTION_CONFIGURATION_INVALID",
            "MCP_EXECUTION_ARGUMENTS_INVALID", "MCP_EXECUTION_CONTEXT_DENIED",
            "MCP_EXECUTION_EVIDENCE_FAILED", "MCP_EXECUTION_REPLAY_DENIED",
            "MCP_EXECUTION_TIMEOUT",
        }:
            code = "MCP_EXECUTION_FAILED"
        self.code = code
        self.logical_call_id = logical_call_id
        self.evidence_ref = evidence_ref
        self.retry_decision = retry_decision
        super().__init__(code)


@dataclass(frozen=True)
class TrackedToolResult:
    data: dict = field(repr=False)
    record: object = field(repr=False)

    def to_tool_evidence(self):
        return self.record.to_tool_evidence()


class TrackedMCPExecutor:
    """One logical call, zero-to-two retries, one shared monotonic deadline.

    The optional safety verifier is a trusted Host inspection capability, not
    model/peer metadata. No confirmation means no uncertain timeout replay.
    A journal claim is durable before calling the SDK. An unfinished claim
    cannot be resumed automatically, including after a process crash.
    """

    def __init__(self, client, store, *, workflow_step_id,
                 safety_verifier=None, retry_delay_seconds=0.05, event_sink=None):
        try:
            if (not isinstance(client, BoundMCPClient) or not isinstance(store, ToolExecutionStore)
                    or safety_verifier is not None and not callable(safety_verifier)
                    or event_sink is not None and not callable(event_sink)
                    or isinstance(retry_delay_seconds, bool)
                    or not isinstance(retry_delay_seconds, (int, float))
                    or not math.isfinite(retry_delay_seconds) or not 0 <= retry_delay_seconds <= 5):
                raise ValueError
            step_id = workspace_uuid(workflow_step_id)
            parameters = child_parameters(client.configuration)
            policy = {"protocolVersion": MCP_PROTOCOL_VERSION, "arguments": parameters.args,
                      "environment": parameters.env, "retryPolicy": "tracked-mcp-v1",
                      "maxRetries": MAX_MCP_TOOL_RETRIES, "retryDelaySeconds": float(retry_delay_seconds)}
            configuration_hash = sha256(json_text(policy, max_bytes=MAX_JSON_BYTES).encode("utf-8")).hexdigest()
        except Exception:
            raise TrackedMCPError("MCP_EXECUTION_CONFIGURATION_INVALID") from None
        self._client, self._store = client, store
        self._step_id = step_id
        self._configuration_hash = configuration_hash
        self._verifier = safety_verifier
        self._retry_delay = float(retry_delay_seconds)
        self._logical_call_ids = []
        self._model_call_ids = set()
        self._event_sink = event_sink
        self._invocation_started = False
        self._finished_events = set()

    def __repr__(self):
        return "TrackedMCPExecutor()"

    @property
    def logical_call_ids(self):
        return tuple(self._logical_call_ids)

    def list_tools(self):
        return self._client.list_tools()

    def set_event_sink(self, event_sink):
        """Bind one trusted Host sink before this capability is invoked.

        The sink receives only journal provenance, never arguments, output
        bodies or peer error text. It may be synchronous or asynchronous.
        Changing it after an invocation (even a rejected one) is denied.
        """
        if self._invocation_started or event_sink is not None and not callable(event_sink):
            raise TrackedMCPError("MCP_EXECUTION_CONFIGURATION_INVALID")
        self._event_sink = event_sink

    async def _emit(self, event_type, record, attempt, duration_ms):
        if self._event_sink is None:
            return
        from mcp_tools.runtime import _finish_handler
        sink = self._event_sink

        async def publish():
            if inspect.iscoroutinefunction(sink):
                result = sink(event_type, record, attempt, duration_ms)
            else:
                # SQLite publication must not block the SDK event loop, and
                # its worker remains owned until the append has completed.
                result = await asyncio.to_thread(sink, event_type, record, attempt, duration_ms)
            if inspect.isawaitable(result):
                await result
            if event_type == "MCP_TOOL_FINISHED":
                self._finished_events.add(record.attempts[attempt].attempt_id)

        task = asyncio.create_task(publish())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Neither direct repeated Task.cancel nor AnyIO level cancellation
            # may detach a Trace append after the request has relinquished it.
            await _finish_handler(task)
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                raise ToolEvidenceStoreError("TOOL_EVIDENCE_STORAGE_ERROR") from None
            raise
        except Exception:
            raise ToolEvidenceStoreError("TOOL_EVIDENCE_STORAGE_ERROR") from None

    def _arguments(self, name, arguments):
        binding = self._client.configuration.binding
        try:
            contract = get_tool_contract(name)
            if contract is None or name not in ROLE_TOOL_NAMES[binding.role]:
                raise ValueError
            contract.validate_input(arguments)
            copied = parse_json(json_text(arguments, max_bytes=MAX_JSON_BYTES), max_bytes=MAX_JSON_BYTES)
            if workspace_uuid(copied["workspaceId"]) != binding.workspace_id:
                raise ValueError
            copied["workspaceId"] = str(binding.workspace_id)
            copied = sanitize_content(copied, source_fields=contract.source_argument_fields, reject_secrets=True)
            return copied, arguments_sha256(name, copied)
        except Exception:
            raise TrackedMCPError("MCP_EXECUTION_ARGUMENTS_INVALID") from None

    def _deadline(self, supplied):
        now = time.monotonic()
        if supplied is not None and (isinstance(supplied, bool) or not isinstance(supplied, (int, float))
                                     or not math.isfinite(supplied)):
            raise TrackedMCPError("MCP_EXECUTION_ARGUMENTS_INVALID")
        deadline = now + self._client.configuration.max_call_seconds
        if supplied is not None:
            deadline = min(deadline, supplied)
        if deadline <= now:
            raise TrackedMCPError("MCP_EXECUTION_TIMEOUT")
        return deadline

    def _selector_hash(self, name, arguments):
        field_name = {"run_unit_tests": "testScope", "run_browser_tests": "testSuite",
                      "run_security_scan": "scannerProfile"}.get(name)
        if field_name is not None:
            selector = arguments[field_name]
        elif name == "run_build" and self._client.configuration.build_configuration is not None:
            selector = self._client.configuration.build_configuration.profile.name
        else:
            return None
        return sha256(selector.encode("utf-8")).hexdigest()

    async def _confirmation(self, record, token, error, deadline):
        if self._verifier is None or deadline <= time.monotonic():
            return None
        try:
            if inspect.iscoroutinefunction(self._verifier):
                proof = self._verifier(record, token, error)
            else:
                # Inspection is read-only by contract. A slow Host inspector
                # must not block SDK cancellation or reset the call deadline.
                proof = await asyncio.wait_for(asyncio.to_thread(self._verifier, record, token, error),
                                              timeout=max(0.001, deadline - time.monotonic()))
            if inspect.isawaitable(proof):
                proof = await asyncio.wait_for(proof, timeout=max(0.001, deadline - time.monotonic()))
            return proof if type(proof) is RetrySafetyConfirmation else None
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    async def _finish(self, binding, token, **fields):
        record = await _run_file_operation(partial(self._store.finish, binding, token, **fields))
        await self._emit("MCP_TOOL_FINISHED", record, token.attempt,
                         record.attempts[token.attempt].duration_ms)
        return record

    async def _cancel(self, binding, token, started):
        # Shield both direct asyncio cancellation and the SDK/AnyIO caller
        # scope while recording uncertainty. Never perform another Tool call.
        from mcp_tools.runtime import _finish_handler
        async def complete():
            record = await _run_file_operation(self._store.get, binding, token.logical_call_id)
            attempt = record.attempts[token.attempt]
            if attempt.status == "FINISHED":
                # A cancelled Store.finish worker may have committed already.
                # Preserve its receipt; only finish the missing Trace append.
                if attempt.attempt_id not in self._finished_events:
                    await self._emit("MCP_TOOL_FINISHED", record, token.attempt, attempt.duration_ms)
                return
            await self._finish(
                binding, token, outcome=ToolExecutionOutcome.UNVERIFIED, error_kind="CANCELLED",
                duration_ms=self._duration(started), delivery_state="UNKNOWN",
                result_unknown=True, retry_safe=False, retry_decision=RetryDecision.DO_NOT_RETRY,
            )

        task = asyncio.create_task(complete())
        await _finish_handler(task)
        task.result()

    @staticmethod
    def _duration(started):
        return max(0, int((time.monotonic() - started) * 1000))

    @staticmethod
    def _error(record, decision, code="MCP_EXECUTION_FAILED"):
        return TrackedMCPError(code, logical_call_id=record.logical_call_id,
                               evidence_ref=record.evidence_ref, retry_decision=decision)

    async def invoke(self, name, arguments, *, deadline_monotonic=None, logical_call_id=None):
        self._invocation_started = True
        binding = self._client.configuration.binding
        copied, input_hash = self._arguments(name, arguments)
        deadline = self._deadline(deadline_monotonic)
        try:
            if logical_call_id is None:
                record = await _run_file_operation(partial(
                    self._store.create, binding, self._step_id, name, input_hash,
                    source_artifact_id=copied.get("snapshotId") if name in EXECUTION_TOOL_NAMES else None,
                    configuration_sha256=self._configuration_hash,
                    selector_sha256=self._selector_hash(name, copied),
                ))
                self._logical_call_ids.append(record.logical_call_id)
            else:
                record = await _run_file_operation(self._store.get, binding, logical_call_id)
                if (record.workflow_step_id != self._step_id or record.tool_name != name
                        or record.input_sha256 != input_hash or record.configuration_sha256 != self._configuration_hash
                        or record.selector_sha256 != self._selector_hash(name, copied)):
                    raise TrackedMCPError("MCP_EXECUTION_REPLAY_DENIED")
                # Existing IDs are for inspection, not request replay. Only
                # this invocation's internal loop owns its automatic retries.
                raise self._error(record, RetryDecision.INSPECT_STATE, "MCP_EXECUTION_REPLAY_DENIED")
        except ToolEvidenceStoreError:
            raise TrackedMCPError("MCP_EXECUTION_EVIDENCE_FAILED") from None
        for _ in range(MAX_MCP_TOOL_RETRIES + 1):
            if time.monotonic() >= deadline:
                raise self._error(record, RetryDecision.DO_NOT_RETRY, "MCP_EXECUTION_TIMEOUT")
            try:
                token = await _run_file_operation(self._store.claim, binding, record.logical_call_id)
            except ToolEvidenceStoreError:
                raise self._error(record, RetryDecision.DO_NOT_RETRY, "MCP_EXECUTION_REPLAY_DENIED") from None
            started = time.monotonic()
            try:
                record = await _run_file_operation(self._store.get, binding, record.logical_call_id)
                await self._emit("MCP_TOOL_CALLED", record, token.attempt, None)
                # Every physical attempt receives a fresh copy of the same
                # bounded logical arguments and the unchanged total deadline.
                data = await self._client.call_tool(
                    name, parse_json(json_text(copied, max_bytes=MAX_JSON_BYTES), max_bytes=MAX_JSON_BYTES),
                    deadline_monotonic=deadline,
                )
                try:
                    outcome, product_kind = classify_success(name, data)
                except ExecutionPolicyError:
                    raise MCPClientError("MCP_CLIENT_OUTPUT_INVALID", delivery_state=MCPDeliveryState.REPLIED) from None
                record = await self._finish(binding, token, outcome=outcome, output=data,
                                            product_failure_kind=product_kind, duration_ms=self._duration(started))
                return TrackedToolResult(data=data, record=record)
            except asyncio.CancelledError:
                try:
                    await self._cancel(binding, token, started)
                except ToolEvidenceStoreError:
                    raise self._error(record, RetryDecision.INSPECT_STATE, "MCP_EXECUTION_EVIDENCE_FAILED") from None
                raise
            except ToolEvidenceStoreError:
                # Tool/receipt publication may already have happened. Keep
                # the actual STARTED or FINISHED journal unchanged; a failed
                # Trace append is never classified as a retryable Tool error.
                raise self._error(record, RetryDecision.INSPECT_STATE, "MCP_EXECUTION_EVIDENCE_FAILED") from None
            except Exception as caught:
                error = caught if isinstance(caught, MCPClientError) else MCPClientError()
                try:
                    proof = await self._confirmation(record, token, error, deadline)
                    failure = classify_failure(name, error, token.attempt, safety=proof,
                                               logical_call_id=record.logical_call_id, input_sha256=input_hash)
                    if failure.retry_decision is RetryDecision.RETRY and time.monotonic() + self._retry_delay >= deadline:
                        failure = replace(failure, retry_decision=RetryDecision.DO_NOT_RETRY)
                    record = await self._finish(
                        binding, token, outcome=ToolExecutionOutcome.UNVERIFIED,
                        error_kind=failure.error_kind, retry_safe=failure.retry_safe,
                        retry_decision=failure.retry_decision, duration_ms=self._duration(started),
                        delivery_state=error.delivery_state.value, result_unknown=failure.result_unknown,
                    )
                except asyncio.CancelledError:
                    try:
                        await self._cancel(binding, token, started)
                    except ToolEvidenceStoreError:
                        raise self._error(record, RetryDecision.INSPECT_STATE, "MCP_EXECUTION_EVIDENCE_FAILED") from None
                    raise
                except ToolEvidenceStoreError:
                    raise self._error(record, RetryDecision.INSPECT_STATE, "MCP_EXECUTION_EVIDENCE_FAILED") from None
                if failure.retry_decision is not RetryDecision.RETRY:
                    raise self._error(record, failure.retry_decision) from None
                await asyncio.sleep(self._retry_delay)
        raise self._error(record, RetryDecision.DO_NOT_RETRY)

    async def call_tool(self, name, arguments, *, deadline_monotonic=None, logical_call_id=None):
        result = await self.invoke(name, arguments, deadline_monotonic=deadline_monotonic, logical_call_id=logical_call_id)
        return result.data

    async def execute(self, call: ToolCall, arguments: dict, context: ToolContext):
        binding = self._client.configuration.binding
        try:
            if (not isinstance(call, ToolCall) or not isinstance(context, ToolContext)
                    or context.role is not binding.agent_role or workspace_uuid(context.workspace_id) != binding.workspace_id
                    or type(call.call_id) is not str or not 1 <= len(call.call_id.encode("utf-8")) <= 4096
                    or call.call_id in self._model_call_ids or parse_json(call.arguments_json) != arguments):
                raise ValueError
            if len(self._model_call_ids) >= 1000:
                raise ValueError
            self._model_call_ids.add(call.call_id)
        except Exception:
            raise TrackedMCPError("MCP_EXECUTION_CONTEXT_DENIED") from None
        return await self.call_tool(call.name, arguments, deadline_monotonic=context.deadline_monotonic)
