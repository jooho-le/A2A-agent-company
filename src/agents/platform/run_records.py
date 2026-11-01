"""Host read-only record collection and factual per-Run metrics, not grading."""

from datetime import datetime, timezone
from uuid import UUID

from agents.platform.export_readonly import ReadOnlyWorkflowRepository, ReadOnlyTelemetryStore
from orchestrator.api.schemas.runs import RunStatusResponse


class RunRecordsError(ValueError):
    def __init__(self, code="RUN_RECORDS_UNAVAILABLE"):
        self.code = code
        super().__init__(code)


def _paged(loader, *, key_limit=50000):
    items, offset = [], 0
    while True:
        page, total = loader(limit=500, offset=offset)
        items.extend(page)
        if len(items) > key_limit:
            raise RunRecordsError("RUN_RECORDS_TOO_LARGE")
        offset += len(page)
        if offset == total:
            return items, total
        if not page or offset > total:
            raise RunRecordsError("RUN_RECORDS_INCOMPLETE")


def collect_records(repository, run_id):
    """Caller pins a read-only repository transaction for a consistent bundle."""
    run_id = UUID(str(run_id))
    run = repository.get_run(run_id)
    if run is None:
        raise RunRecordsError("RUN_NOT_FOUND")
    events, total = _paged(lambda **page: repository.list_events(run_id, **page))
    configuration = repository.get_run_configuration(run_id)
    if configuration is None:
        raise RunRecordsError("RUN_CONFIGURATION_MISSING")
    with repository._connection() as connection:
        available = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_runtime_events'").fetchone() is not None
    store = ReadOnlyTelemetryStore(repository)
    if available:
        records, record_total = _paged(lambda **page: store.list_usage(run_id, **page))
        telemetry, telemetry_total = _paged(lambda **page: store.list_events(run_id, **page))
        usage = {"runId": str(run_id), "summary": store.usage_summary(run_id), "records": records,
                 "total": record_total, "limit": 500, "offset": 0}
    else:
        telemetry, telemetry_total, usage = [], None, None
    artifacts = [item.model_dump(mode="json", by_alias=True) for item in repository.list_project_artifacts(run_id)]
    bundle = {"run": RunStatusResponse.from_run(run).model_dump(mode="json", by_alias=True),
              "configuration": configuration.to_artifact_json(),
              "events": [event.to_trace_json() for event in events], "eventsTotal": total,
              "issues": [item.model_dump(mode="json", by_alias=True) for item in repository.list_issue_records(run_id)],
              "toolAttempts": repository.list_tool_attempts(run_id), "usage": usage, "snapshotConsistent": True}
    trace = {"runId": str(run_id), "events": bundle["events"], "eventsTotal": total,
             "issues": bundle["issues"], "artifacts": [configuration.to_artifact_json(), *artifacts],
             "toolAttempts": bundle["toolAttempts"], "telemetry": telemetry if available else None,
             "telemetryTotal": telemetry_total}
    return bundle, trace


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise RunRecordsError("RUN_TIMESTAMP_INVALID")
    return parsed


def _ms(start, end):
    return None if start is None or end is None or end < start else int((end - start).total_seconds() * 1000)


def metrics_from_records(bundle, trace, *, now=None):
    run, events = bundle["run"], bundle["events"]
    now = now or datetime.now(timezone.utc)
    def times(kind):
        return [_time(event["occurredAt"]) for event in events if event["eventType"] == kind]
    starts, verdicts, builds, aborts = times("RUN_STARTED"), times("VERDICT_CREATED"), times("BUILD_PASSED"), times("RUN_ABORTED")
    start = min(starts) if starts else None
    end = max(aborts) if run["status"] == "ABORTED" and aborts else (max(verdicts) if run["verdict"] is not None and verdicts else None)
    paused, waits = None, 0
    boundary = end or now
    transitions = sorted((event for event in events if event["eventType"] in {
        "RUN_STARTED", "WORKFLOW_STATE_CHANGED", "RUN_FINISHED", "RUN_ABORTED"}), key=lambda event: _time(event["occurredAt"]))
    for event in transitions:
        stamp = _time(event["occurredAt"])
        if stamp > boundary:
            continue
        waiting = event.get("workflowState") in {"WAITING_INPUT", "HUMAN_REVIEW"}
        if waiting and paused is None:
            paused = stamp
        elif not waiting and paused is not None:
            waits += _ms(paused, stamp) or 0
            paused = None
    if paused is not None:
        waits += _ms(paused, boundary) or 0
    usage = bundle["usage"]
    summary = {} if usage is None else usage["summary"]
    records = [] if usage is None else usage["records"]
    retry_keys = {(event["detail"]["logicalCallId"], event["detail"]["toolAttempt"])
                  for event in (trace["telemetry"] or [])
                  if event["binding"].get("eventType") == "MCP_TOOL_CALLED" and (event["detail"].get("toolAttempt") or 0) > 0}
    return {"schemaVersion": 1, "runId": run["runId"],
        "architecture": bundle["configuration"]["configuration"]["architecture"],
        "status": run["status"], "verdict": run["verdict"], "isTerminal": run["status"] in {"FINISHED", "ABORTED"},
        "runStartedAt": start.isoformat() if start else None, "verdictAt": end.isoformat() if end else None,
        "firstBuildPassAt": min(builds).isoformat() if builds else None,
        "wallClockMs": _ms(start, end), "firstBuildPassMs": _ms(start, min(builds)) if builds else None,
        "humanWaitMs": waits if _ms(start, end or now) is not None else None,
        "humanWaitOpen": run["status"] in {"WAITING_INPUT", "HUMAN_REVIEW"}, "observedElapsedMs": _ms(start, end or now),
        "timingComplete": _ms(start, end) is not None,
        "fixAttempts": run["fixAttempt"], "codeVersion": run["codeVersion"], "issuesFound": len(bundle["issues"]),
        "recordedToolRetries": len(retry_keys) if trace["telemetry"] is not None else None,
        "modelCalls": summary.get("modelCalls"), "toolCalls": summary.get("toolCalls"),
        "usageComplete": summary.get("usageComplete", False), "totalTokens": summary.get("totalTokens"),
        "knownTotalTokens": summary.get("knownTotalTokens"),
        "inputTokens": sum(record["inputTokens"] or 0 for record in records) if summary.get("usageComplete") else None,
        "outputTokens": sum(record["outputTokens"] or 0 for record in records) if summary.get("usageComplete") else None,
        "costUsd": summary.get("costUsd"), "costComplete": summary.get("costComplete", False),
        "priceBookSha256": summary.get("priceBookSha256"), "collectedAt": now.isoformat()}


def collect_run_metrics(repository, run_id):
    try:
        view = ReadOnlyWorkflowRepository(repository.database_path)
        with view.snapshot():
            bundle, trace = collect_records(view, run_id)
            return metrics_from_records(bundle, trace)
    except RunRecordsError:
        raise
    except Exception:
        raise RunRecordsError() from None
