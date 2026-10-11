"""Host-bound accounting/provenance, never model input or product verdicts."""

from orchestrator.domain import TraceEvent
from mcp_tools.execution_store import ToolCallRecord


class RuntimeTelemetryError(RuntimeError):
    def __init__(self):
        super().__init__("RUNTIME_TELEMETRY_UNAVAILABLE")


class RuntimeTelemetry:
    """Bind trusted execution context before the first model or MCP call.

    SDK admission issued Task/Context IDs may precede their first Orchestrator
    observation. A conflicting already observed ID is never accepted.
    """

    def __init__(self, repository, store):
        self.repository = repository
        self.store = store

    def bind(self, role, context, execution, *, usage_sink=None):
        try:
            metadata = execution.metadata
            run = self.repository.get_run(metadata.run_id)
            step = next(item for item in self.repository.list_steps(metadata.run_id)
                        if item.workflow_step_id == metadata.workflow_step_id)
            if (run is None or step.agent_role is not role
                    or step.attempt != metadata.attempt
                    or step.code_version != metadata.code_version
                    or step.requirement_ids != list(metadata.requirement_ids or ())
                    or not context.task_id or not context.context_id
                    or step.a2a_task_id not in (None, context.task_id)
                    or step.agent_context_id not in (None, context.context_id)):
                raise ValueError
            source = getattr(execution, "source", None) or getattr(execution, "previous_source", None)
            binding = {
                "runId": str(metadata.run_id), "workflowStepId": str(metadata.workflow_step_id),
                "a2aTaskId": context.task_id, "agentContextId": context.context_id,
                "role": role.value, "attempt": metadata.attempt,
                "codeVersion": metadata.code_version,
                "requirementIds": [str(item) for item in step.requirement_ids],
                "inputArtifactIds": [str(item) for item in step.input_artifact_ids],
                "snapshotSha256": None if source is None else source.snapshot_sha256,
            }
            return BoundRuntimeTelemetry(self.repository, self.store, binding, execution.model, usage_sink)
        except Exception:
            raise RuntimeTelemetryError() from None


class BoundRuntimeTelemetry:
    def __init__(self, repository, store, binding, model, usage_sink):
        self.repository, self.store, self.binding = repository, store, binding
        self.model, self._external_sink = model, usage_sink

    def __repr__(self):
        return "BoundRuntimeTelemetry()"

    def _event(self, event_type, *, attempt=None, duration=None, record=None):
        run = self.repository.get_run(self.binding["runId"])
        if run is None:
            raise RuntimeTelemetryError()
        manifest = None if record is None else record.execution_manifest
        return TraceEvent(
            run_id=self.binding["runId"], workflow_step_id=self.binding["workflowStepId"],
            a2a_task_id=self.binding["a2aTaskId"], agent_context_id=self.binding["agentContextId"],
            event_type=event_type, actor=self.binding["role"],
            attempt=self.binding["attempt"] if attempt is None else attempt,
            code_version=self.binding["codeVersion"],
            requirement_ids=self.binding["requirementIds"],
            input_artifact_ids=self.binding["inputArtifactIds"],
            snapshot_sha256=self.binding["snapshotSha256"] if manifest is None else manifest.snapshot_sha256,
            workflow_state=run.status, duration_ms=duration,
        )

    def model_started(self, sequence):
        self.store.append_event(self._event("LLM_MODEL_CALLED"), kind="LLM", detail={
            "sequence": sequence, "requestedModel": self.model.model_dump(mode="json", by_alias=True),
        }, idempotency_key=f"model-start:{sequence}")

    def usage(self, record):
        # Persist first. An optional Host observer cannot disable the ledger.
        self.store.append_usage(self.binding, record)
        if self._external_sink is not None:
            self._external_sink(record)

    def tool_event(self, event_type, record, attempt, duration_ms):
        try:
            if (type(record) is not ToolCallRecord
                    or str(record.run_id) != self.binding["runId"]
                    or str(record.workflow_step_id) != self.binding["workflowStepId"]
                    or record.role.value != self.binding["role"]):
                raise ValueError
            measured = next(item for item in record.attempts if item.attempt == attempt)
            detail = {
                "toolName": record.tool_name, "logicalCallId": str(record.logical_call_id),
                "toolAttempt": attempt, "attemptId": str(measured.attempt_id),
                "workflowAttempt": self.binding["attempt"],
                "status": measured.status,
                "outcome": None if measured.outcome is None else measured.outcome.value,
                "errorKind": measured.error_kind, "retryDecision": measured.retry_decision.value,
                "deliveryState": measured.delivery_state, "resultUnknown": measured.result_unknown,
                "executionId": None if measured.execution_id is None else str(measured.execution_id),
                "executionManifestId": None if measured.execution_manifest_id is None else str(measured.execution_manifest_id),
                "evidenceRef": measured.evidence_ref,
                "inputSha256": record.input_sha256, "configurationSha256": record.configuration_sha256,
            }
            self.store.append_event(self._event(event_type, attempt=attempt,
                duration=duration_ms, record=record), kind="MCP", detail=detail,
                idempotency_key=f"mcp:{record.logical_call_id}:{attempt}:{event_type}")
        except Exception:
            raise RuntimeTelemetryError() from None


def bind_runtime_telemetry(factory, role, context, execution, usage_sink):
    if factory is None:
        return None, usage_sink, None
    bound = factory(role, context, execution, usage_sink=usage_sink)
    if type(bound) is not BoundRuntimeTelemetry:
        raise RuntimeTelemetryError()
    return bound, bound.usage, bound.model_started
