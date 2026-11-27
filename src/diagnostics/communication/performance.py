"""Descriptive diagnostic timings, without an architecture/quality verdict."""

from math import ceil, isfinite

from diagnostics.communication.timing import CLOCK, MODEL_PHASES, PHASES, PRIMARY_PHASES


def _number(value):
    try:
        return type(value) in (int, float) and isfinite(value) and value >= 0
    except OverflowError:
        return False


def _distribution(values, *, unverified=0, invalid=0):
    values = sorted(values)
    return {"samples": len(values), "unverified": unverified, "invalid": invalid,
            "minMs": round(values[0], 3) if values else None,
            "maxMs": round(values[-1], 3) if values else None,
            "p50Ms": round(values[ceil(len(values) * .5) - 1], 3) if values else None,
            "p95Ms": round(values[ceil(len(values) * .95) - 1], 3) if values else None}


def _phase(rows, name):
    values, unverified, invalid = [], 0, 0
    for row in rows:
        timing = row.get("timing") or {}
        item = timing.get("phases", {}).get(name, {})
        if timing.get("schemaVersion") != 1 or timing.get("clock") != CLOCK:
            unverified += 1
        elif timing.get("ordering") == "FAIL" or item.get("status") == "INVALID":
            invalid += 1
        elif item.get("status") != "MEASURED":
            unverified += 1
        elif not _number(item.get("durationMs")):
            invalid += 1
        else:
            values.append(item["durationMs"])
    return _distribution(values, unverified=unverified, invalid=invalid)


def _dominant(phases, names):
    measured = [name for name in names if phases[name]["p50Ms"] is not None]
    return max(measured, key=lambda name: phases[name]["p50Ms"]) if measured else None


def timeline_summary(rows):
    active = [row for row in rows if row["phase"] not in {"PLANNED", "SKIPPED"}]
    measured, unknown, invalid = [], 0, 0
    clock_ids = set()
    for row in active:
        timing = row.get("timing") or {}
        item = timing.get("interval") or {}
        if (timing.get("schemaVersion") != 1 or timing.get("clock") != CLOCK
                or type(timing.get("clockId")) is not str or not timing["clockId"]):
            unknown += 1
        elif timing.get("ordering") == "FAIL" or item.get("status") == "INVALID":
            invalid += 1
        elif item.get("status") != "MEASURED":
            unknown += 1
        elif (not _number(item.get("startMs")) or not _number(item.get("endMs"))
              or item["endMs"] < item["startMs"]):
            invalid += 1
        else:
            clock_ids.add(timing["clockId"])
            measured.append((item["startMs"], item["endMs"]))
    result = {"status": "UNVERIFIED", "observedSamples": len(measured), "unverifiedSamples": unknown,
              "invalidSamples": invalid, "probeWorkSumMs": None, "observedUnionMs": None,
              "observedEnvelopeMs": None, "concurrentMs": None, "maxConcurrency": None}
    if len(clock_ids) > 1 or invalid:
        result["status"] = "INVALID"
        return result
    if not measured:
        return result
    events = {}
    for start, end in measured:
        if end > start:
            events[start] = events.get(start, 0) + 1
            events[end] = events.get(end, 0) - 1
    union = concurrent = count = maximum = 0
    previous = None
    for point, delta in sorted(events.items()):
        if previous is not None:
            union += (point - previous) if count > 0 else 0
            concurrent += (point - previous) if count > 1 else 0
        count += delta
        maximum = max(maximum, count)
        previous = point
    result.update(status="PARTIAL" if unknown else "MEASURED",
                  probeWorkSumMs=round(sum(end - start for start, end in measured), 3),
                  observedUnionMs=round(union, 3), observedEnvelopeMs=round(max(end for _, end in measured) - min(start for start, _ in measured), 3),
                  concurrentMs=round(concurrent, 3), maxConcurrency=maximum)
    return result


def performance_summary(rows, *, elapsed_ms=None):
    roles = {}
    for role in dict.fromkeys(row["role"] for row in rows):
        group = [row for row in rows if row["role"] == role]
        completed = [row for row in group if row["phase"] == "COMPLETED" and (row.get("model") or {}).get("status") == "COMPLETED"]
        phases = {name: _phase(completed, name) for name in PHASES}
        normalized, unavailable = [], 0
        for row in completed:
            usage = (row.get("model") or {}).get("usage") or {}
            tokens = usage.get("output_tokens")
            exchange = _phase([row], "providerExchangeMs")["p50Ms"]
            if type(tokens) is int and tokens > 0 and exchange is not None:
                normalized.append(exchange / tokens)
            else:
                unavailable += 1
        normalized_stats = _distribution(normalized, unverified=unavailable)
        roles[role] = {"completedSamples": len(completed),
            "incompleteSamples": sum(row["phase"] not in {"PLANNED", "SKIPPED"} for row in group) - len(completed),
            "notExecutedSamples": sum(row["phase"] in {"PLANNED", "SKIPPED"} for row in group),
            "deliveryVerifiedSamples": sum((row.get("delivery") or {}).get("check", {}).get("status") == "PASS" for row in completed),
            "contentContractPassedSamples": sum(row["checks"]["sourceGrounding"]["status"] == "PASS" for row in completed),
            "phases": phases, "dominantPhase": _dominant(phases, PRIMARY_PHASES),
            "dominantModelPhase": _dominant(phases, MODEL_PHASES),
            "providerMsPerOutputToken": {key.replace("Ms", "MsPerToken"): value for key, value in normalized_stats.items()}}
    measured = [(role, group["phases"]["agentProcessingMs"]["p50Ms"]) for role, group in roles.items()
                if group["phases"]["agentProcessingMs"]["p50Ms"] is not None]
    spread = {"status": "UNVERIFIED", "fastestRole": None, "slowestRole": None, "deltaMs": None, "ratio": None}
    if len(measured) >= 2:
        fastest, slowest = min(measured, key=lambda item: item[1]), max(measured, key=lambda item: item[1])
        spread.update(status="OBSERVED", fastestRole=fastest[0], slowestRole=slowest[0],
                      deltaMs=round(slowest[1] - fastest[1], 3), ratio=round(slowest[1] / fastest[1], 3) if fastest[1] > 0 else None)
    return {"schemaVersion": 1, "scope": "LOCAL_DIAGNOSTIC_DESCRIPTIVE_ONLY",
            "statistics": "completed executions only; nearest-rank percentiles; quality reported separately",
            "diagnosticElapsedMs": elapsed_ms if _number(elapsed_ms) else None,
            "roles": roles, "roleSpread": spread, "timeline": timeline_summary(rows),
            "limitations": ["ROLE_WORKLOADS_DIFFER", "PROVIDER_EXCHANGE_INCLUDES_NETWORK_AND_SERVER_TIME",
                            "NO_PRODUCT_VERDICT_OR_ARCHITECTURE_SPEEDUP_INFERENCE", "NESTED_PHASES_MUST_NOT_BE_SUMMED"]}
