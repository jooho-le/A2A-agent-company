"""Bounded diagnostic inputs, independent oracles, and observation summaries."""

from dataclasses import dataclass, field
from math import ceil
from time import perf_counter_ns
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestrator.core.security import redact_text
from orchestrator.domain.json_fingerprint import json_fingerprint
from diagnostics.communication.timing import phase_values, span, timing_view


class CheckOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    architecture: Literal["MULTI_AGENT", "SINGLE_AGENT"] = "MULTI_AGENT"
    profile: Literal["signup", "documents", "mystery"] = "signup"
    value: int = Field(default=100, strict=True, ge=-1_000_000, le=1_000_000)
    label: str = Field(default="실제 OpenAI · 한글과 숫자 전달", max_length=120)
    rounds: int = Field(default=1, strict=True, ge=1, le=5)

    @field_validator("label")
    @classmethod
    def safe_label(cls, value: str) -> str:
        return redact_text(value)


def now() -> int:
    return perf_counter_ns()


def normalized(value):
    """Project comparison: Struct's 100.0 and JSON's 100 are the same number.

    Not an RFC canonicalization implementation. Booleans remain distinct from
    numbers, strings stay exact, and lists preserve order. Lab inputs have only
    bounded integers, so protobuf's double range cannot lose integer precision.
    """
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalized(item) for item in value]
    if type(value) is float and value.is_integer():
        return int(value)
    return value


def fingerprint(value) -> str:
    return json_fingerprint(value)


def differences(expected, actual, path: str = "$") -> list[dict]:
    return _differences_normalized(normalized(expected), normalized(actual), path)


def _differences_normalized(expected, actual, path):
    if type(expected) is not type(actual):
        return [{"path": path, "kind": "TYPE_CHANGED", "expected": expected, "actual": actual}]
    result = []
    if isinstance(expected, dict):
        for key in sorted(expected.keys() | actual.keys()):
            pointer = path + "/" + key.replace("~", "~0").replace("/", "~1")
            if key not in actual:
                result.append({"path": pointer, "kind": "MISSING", "expected": expected[key]})
            elif key not in expected:
                result.append({"path": pointer, "kind": "ADDED", "actual": actual[key]})
            else:
                result.extend(_differences_normalized(expected[key], actual[key], pointer))
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            result.append({"path": path, "kind": "LENGTH_CHANGED", "expected": len(expected), "actual": len(actual)})
        for index, (left, right) in enumerate(zip(expected, actual)):
            result.extend(_differences_normalized(left, right, path + "/" + str(index)))
    elif expected != actual:
        result.append({"path": path, "kind": "VALUE_CHANGED", "expected": expected, "actual": actual})
    return result


def compare(expected, actual) -> dict:
    if expected is None or actual is None:
        return {"status": "UNVERIFIED", "differences": []}
    diff = differences(expected, actual)
    return {"status": "FAIL" if diff else "PASS", "differences": diff,
            "expectedHash": fingerprint(expected), "actualHash": fingerprint(actual)}


def grounding(response, payload, role=None) -> dict:
    """Only declared claims, against an independent observed-input oracle.

    This is not a general natural-language/LLM hallucination detector. A valid
    schema, matching hashes or the model's self-assessment cannot establish it.
    """
    if response is None or payload is None:
        return {"status": "UNVERIFIED", "differences": [], "scope": "DECLARED_FIELDS_ONLY"}
    sample = payload["sample"]
    expected = {
        "value": sample["value"], "label": sample["label"],
        "requirementCount": len(sample["requirementIds"]),
        "upstreamHash": fingerprint(payload["upstream"]) if payload["upstream"] is not None else None,
    }
    if payload.get("profile", "basic") != "basic" and role is not None:
        from diagnostics.communication.workloads import expected_specification
        expected["specification"] = expected_specification(payload["profile"], role)
    actual = response.get("claims")
    if not isinstance(actual, dict):
        return {"status": "FAIL", "differences": [{"path": "$/claims", "kind": "MISSING_CLAIMS"}], "scope": "DECLARED_FIELDS_ONLY"}
    verdict = compare(expected, actual)
    verdict["scope"] = "FROZEN_FIXTURE_CONTRACT" if "specification" in expected else "DECLARED_FIELDS_ONLY"
    verdict["freeTextStatus"] = "UNVERIFIED"
    for item in verdict["differences"]:
        if item["kind"] == "ADDED":
            item["kind"] = "UNSUPPORTED_CLAIM"
    return verdict


@dataclass
class ProbeRecord:
    role: str
    round: int
    position: int
    probe_id: str = field(default_factory=lambda: str(uuid4()))
    phase: str = "PLANNED"
    payload: dict | None = None
    wire_sent: dict | None = None
    wire_received: dict | None = None
    admitted: dict | None = None
    received: dict | None = None
    metadata_sent: dict | None = None
    metadata_received: dict | None = None
    metadata_response: dict | None = None
    response_sent: dict | None = None
    response_received: dict | None = None
    task_id: str | None = None
    context_id: str | None = None
    message_id: str | None = None
    receiver_task_id: str | None = None
    receiver_context_id: str | None = None
    receiver_message_id: str | None = None
    wire_bytes: int | None = None
    response_http_status: int | None = None
    points: dict = field(default_factory=dict)
    error: str | None = None
    cleanup_state: str | None = None
    identity_valid: bool | None = None
    executor_invocations: int = 0
    observer_overhead_ns: int = 0
    model_observation: dict | None = None
    transport_observation: dict | None = None
    clock_origin_ns: int | None = field(default=None, repr=False)
    clock_id: str | None = field(default=None, repr=False)
    wire_request: dict | None = field(default=None, repr=False)
    delivery_receipt: dict | None = field(default=None, repr=False)
    delivery_check: dict = field(default_factory=lambda: {
        "status": "UNVERIFIED", "differences": ["RECEIPT_NOT_OBSERVED"]})
    notify: object = field(default=None, repr=False)

    def changed(self):
        if self.notify is not None:
            started = now()
            self.notify(self)
            self.observer_overhead_ns += now() - started

    def mark(self, key, phase=None):
        self.points[key] = now()
        if phase is not None:
            self.phase = phase
        self.changed()

    def ms(self, start, end):
        return span(self.points, start, end)["durationMs"]

    def mark_time(self, key):
        # Internal timing points do not call the potentially expensive UI
        # observer, change the workflow phase, or enter model input.
        self.points.setdefault(key, now())

    def content_checks(self, response, payload):
        return grounding(response, payload, self.role)

    def view(self) -> dict:
        request_checks = {
            "serialized": compare(self.payload, self.wire_sent),
            "network": compare(self.wire_sent, self.wire_received),
            "admission": compare(self.wire_received, self.admitted),
            "executor": compare(self.admitted, self.received),
            "endToEnd": compare(self.payload, self.received),
        }
        transfer_checks = (compare(self.response_sent, self.response_received),
                           compare(self.metadata_sent, self.metadata_received),
                           compare(self.metadata_sent, self.metadata_response))
        mismatch = (self.delivery_check["status"] == "FAIL" or self.identity_valid is False
                    or any(value["status"] == "FAIL" for value in (*request_checks.values(), *transfer_checks)))
        stage = ("NOT_DISPATCHED" if self.phase in {"PLANNED", "SKIPPED"} else
                 "SEND_NOT_OBSERVED" if self.wire_sent is None else
                 "INTEGRITY_MISMATCH" if mismatch else
                 "DELIVERY_UNVERIFIED" if self.delivery_check["status"] != "PASS" else
                 "PERSISTED_ONLY" if self.received is None else
                 "EXECUTOR_OBSERVED_RESULT_UNVERIFIED" if self.response_received is None else
                 "RESPONSE_VERIFIED" if self.identity_valid is True and all(value["status"] == "PASS"
                     for value in (*request_checks.values(), *transfer_checks)) else "RESPONSE_OBSERVED_UNVERIFIED")
        return {
            "id": self.probe_id, "role": self.role, "round": self.round,
            "position": self.position, "phase": self.phase,
            "taskId": self.task_id, "contextId": self.context_id, "messageId": self.message_id,
            "receiverIdentity": {"taskId": self.receiver_task_id, "contextId": self.receiver_context_id,
                                 "messageId": self.receiver_message_id},
            "wireBytes": self.wire_bytes, "httpStatus": self.response_http_status,
            "executorInvocations": self.executor_invocations,
            "observerCallbackMs": round(self.observer_overhead_ns / 1_000_000, 3),
            "model": self.model_observation,
            "transport": self.transport_observation,
            "delivery": {"stage": stage, "receipt": self.delivery_receipt, "check": self.delivery_check},
            "error": self.error,
            "cleanupState": self.cleanup_state,
            "observations": {"intended": self.payload, "wireSent": self.wire_sent,
                             "wireReceived": self.wire_received, "admitted": self.admitted,
                             "executorReceived": self.received, "responseSent": self.response_sent,
                             "responseReceived": self.response_received},
            "checks": {"request": request_checks,
                       "response": transfer_checks[0],
                       "metadata": transfer_checks[1],
                       "responseMetadata": transfer_checks[2],
                       "identity": "UNVERIFIED" if self.identity_valid is None else "PASS" if self.identity_valid else "FAIL",
                       "grounding": self.content_checks(self.response_sent, self.received),
                       "sourceGrounding": self.content_checks(self.response_received, self.payload)},
            "timings": phase_values(self.points),
            "timing": timing_view(self.points, origin_ns=self.clock_origin_ns, clock_id=self.clock_id),
        }


def summarize(rows: list[ProbeRecord]) -> dict:
    return summarize_views([row.view() for row in rows])


def summarize_views(views) -> dict:
    issues = []
    for view in views:
        checks = view["checks"]
        failures = ["request" for check in checks["request"].values() if check["status"] == "FAIL"]
        failures += [key for key in ("response", "metadata", "responseMetadata", "grounding", "sourceGrounding") if checks[key]["status"] == "FAIL"]
        if checks["identity"] == "FAIL":
            failures.append("identity")
        if view["delivery"]["check"]["status"] == "FAIL":
            failures.append("deliveryReceipt")
        elif view["phase"] not in {"PLANNED", "SKIPPED"} and view["delivery"]["check"]["status"] == "UNVERIFIED":
            failures.append("deliveryReceiptUnverified")
        if failures or view["phase"] in {"TIMEOUT", "ERROR", "CANCELED"}:
            issues.append({"id": view["id"], "role": view["role"], "checks": sorted(set(failures)), "phase": view["phase"]})
    roles = {}
    for role in dict.fromkeys(view["role"] for view in views):
        role_views = [view for view in views if view["role"] == role]
        values = sorted(view["timings"]["agentProcessingMs"] for view in role_views if view["timings"]["agentProcessingMs"] is not None)
        roles[role] = {"completed": sum(v["phase"] == "COMPLETED" for v in role_views),
                       "samples": len(values),
                       "p50Ms": values[max(0, ceil(len(values) * .5) - 1)] if values else None,
                       "p95Ms": values[max(0, ceil(len(values) * .95) - 1)] if values else None}
    verified = sum(
        all(check["status"] == "PASS" for check in view["checks"]["request"].values())
        and all(view["checks"][key]["status"] == "PASS" for key in ("response", "metadata", "responseMetadata", "grounding", "sourceGrounding"))
        and view["checks"]["identity"] == "PASS" and view["delivery"]["check"]["status"] == "PASS" for view in views
    )
    unverified = sum(
        any(check["status"] == "UNVERIFIED" for check in view["checks"]["request"].values())
        or any(view["checks"][key]["status"] == "UNVERIFIED" for key in ("response", "metadata", "responseMetadata", "grounding", "sourceGrounding"))
        or view["checks"]["identity"] == "UNVERIFIED" or view["delivery"]["check"]["status"] == "UNVERIFIED" for view in views
    )
    return {"planned": len(views), "verifiedRequests": verified, "unverifiedRequests": unverified,
            "deliveryStages": {stage: sum(view["delivery"]["stage"] == stage for view in views)
                               for stage in dict.fromkeys(view["delivery"]["stage"] for view in views)},
            "sent": sum(view["observations"]["wireSent"] is not None for view in views),
            "received": sum(view["observations"]["executorReceived"] is not None for view in views),
            "responses": sum(view["observations"]["responseReceived"] is not None for view in views),
            "issueCount": len(issues), "issues": issues, "roles": roles}


def row_clean(row: dict) -> bool:
    """A result is clean only when every observed boundary/contract passed."""
    checks = row["checks"]
    return (row["phase"] == "COMPLETED" and checks["identity"] == "PASS"
            and row["delivery"]["check"]["status"] == "PASS"
            and all(item["status"] == "PASS" for item in checks["request"].values())
            and all(checks[key]["status"] == "PASS" for key in (
                "response", "metadata", "responseMetadata", "grounding", "sourceGrounding")))
