"""Local monotonic observations. Missing durations are never synthetic zeroes."""

CLOCK = "SHARED_PROCESS_PERF_COUNTER_NS"
PHASES = {
    "orchestratorPrepareMs": ("prepare", "cardStart"),
    "cardLookupMs": ("cardStart", "cardEnd"),
    "serializationMs": ("cardEnd", "clientSend"),
    "requestTransferMs": ("clientSend", "serverReceived"),
    "queueMs": ("serverReceived", "executorStart"),  # Historical admission + scheduling alias.
    "admissionAndSchedulingMs": ("serverReceived", "executorStart"),
    "agentProcessingMs": ("executorStart", "responseReady"),
    "ackRoundTripMs": ("clientSend", "ack"),
    "receiptObservationMs": ("ack", "receiptObserved"),
    "resultObservationMs": ("responseReady", "observed"),
    "totalMs": ("prepare", "finish"),
    "executorBeforeModelMs": ("executorStart", "modelStart"),
    "modelPrepareMs": ("modelStart", "modelReady"),
    "providerPrepareMs": ("modelReady", "apiDispatched"),
    "providerHeadersMs": ("apiDispatched", "apiHeaders"),
    "providerExchangeMs": ("apiDispatched", "apiBodyRead"),
    "providerDecodeMs": ("apiBodyRead", "providerReturned"),
    "modelValidationMs": ("providerReturned", "modelChecked"),
    "modelCleanupMs": ("modelCallEnd", "modelFinish"),
    "modelTotalMs": ("modelStart", "modelFinish"),
    "executorAfterModelMs": ("modelFinish", "responseReady"),
}
# Only these coarse phases can be compared without counting nested/parallel
# ACK, receipt and model spans twice. They are observations, not CPU profiles.
PRIMARY_PHASES = ("orchestratorPrepareMs", "cardLookupMs", "serializationMs", "requestTransferMs",
                  "admissionAndSchedulingMs", "agentProcessingMs", "resultObservationMs")
MODEL_PHASES = ("modelPrepareMs", "providerPrepareMs", "providerExchangeMs",
                "providerDecodeMs", "modelValidationMs", "modelCleanupMs")
ORDERED_POINTS = ("prepare", "cardStart", "cardEnd", "clientSend", "serverReceived", "executorStart",
                  "modelStart", "modelReady", "apiDispatched", "apiHeaders", "apiBodyRead",
                  "providerReturned", "modelChecked", "modelCallEnd", "modelFinish", "responseReady",
                  "observed", "finish")


def span(points, start, end):
    present = [points[key] for key in (start, end) if key in points]
    if any(type(value) is not int or value < 0 for value in present):
        return {"status": "INVALID", "durationMs": None}
    if start not in points or end not in points:
        return {"status": "UNVERIFIED", "durationMs": None}
    delta = points[end] - points[start]
    if delta < 0:
        return {"status": "INVALID", "durationMs": None}
    return {"status": "MEASURED", "durationMs": round(delta / 1_000_000, 3)}


def phase_values(points):
    return {key: span(points, *bounds)["durationMs"] for key, bounds in PHASES.items()}


def timing_view(points, *, origin_ns=None, clock_id=None):
    observed = [points[key] for key in ORDERED_POINTS if key in points]
    valid = all(type(value) is int and value >= 0 for value in observed)
    ordered = valid and all(first <= second for first, second in zip(observed, observed[1:]))
    ordering = "FAIL" if not ordered else "PASS" if len(observed) >= 2 else "UNVERIFIED"
    interval = {"status": "UNVERIFIED", "startMs": None, "endMs": None}
    if origin_ns is not None and type(clock_id) is str and clock_id:
        values = {**points, "origin": origin_ns}
        first, last, total = span(values, "origin", "prepare"), span(values, "origin", "finish"), span(values, "prepare", "finish")
        if ordering == "FAIL" or any(item["status"] == "INVALID" for item in (first, last, total)):
            interval["status"] = "INVALID"
        elif all(item["status"] == "MEASURED" for item in (first, last, total)):
            interval = {"status": "MEASURED", "startMs": first["durationMs"], "endMs": last["durationMs"]}
    return {"schemaVersion": 1, "clock": CLOCK, "clockId": clock_id, "ordering": ordering,
            "interval": interval, "phases": {key: span(points, *bounds) for key, bounds in PHASES.items()}}
