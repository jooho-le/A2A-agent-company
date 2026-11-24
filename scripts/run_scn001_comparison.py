"""Bounded sequential four-condition API comparison; never resume or replace a paused Run."""
import argparse
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

import run_scn001_smoke as smoke

CONDITIONS = (
    ("multi-ver1", "MULTI_AGENT", "IN_LOOP_AND_FINAL"),
    ("single-ver1", "SINGLE_AGENT", "IN_LOOP_AND_FINAL"),
    ("single-ver2", "SINGLE_AGENT", "FINAL_ONLY"),
    ("multi-ver2", "MULTI_AGENT", "FINAL_ONLY"),
)


def implementation():
    files = {}
    for folder in ("src", "schemas"):
        for path in sorted((smoke.ROOT / folder).rglob("*")):
            if path.suffix not in (".py", ".json") or path.name.endswith(" 2.py"):
                continue
            files[str(path.relative_to(smoke.ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def summarize(out, condition, repetition, order):
    from evaluation.experiment import _checked_metrics
    from evaluation.scoring import product_accuracy
    bundle = json.loads((out / "export/bundle.json").read_text())
    metrics = _checked_metrics(bundle)
    run_id = bundle["run"]["runId"]
    path = out / "evaluation" / run_id / "output.json"
    report = json.loads(path.read_text())["report"] if path.exists() else None
    accuracy = product_accuracy(report) if report is not None else None
    codes = []

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("errorCode", "reasonCode", "code") and isinstance(item, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{3,100}", item):
                    if item not in codes:
                        codes.append(item)
                elif isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(bundle.get("events", []))
    row = {"condition": condition, "repetition": repetition, "order": order, "runId": run_id,
        "runStatus": bundle["run"]["status"], "verdict": bundle["run"].get("verdict"),
        "scored": report is not None, "accuracy": accuracy,
        "metrics": {key: metrics.get(key) for key in (
            "verdictMs", "totalMs", "firstBuildPassMs", "fixAttempts", "modelCalls", "toolCalls",
            "inputTokens", "outputTokens", "totalTokens", "costUsd", "costComplete")},
        "checkOutcomes": [] if report is None else [{"testId": r["testId"], "status": r["status"]} for r in report["checks"]],
        "codes": codes, "directory": str(out)}
    smoke.write_json(out / "diagnostic-summary.json", row)
    return row


def means(rows, planned):
    def mean(values):
        selected = [v for v in values if v is not None]
        return None if not selected else float(sum(Decimal(str(v)) for v in selected) / len(selected))
    groups = {}
    for name, architecture, loop_policy in CONDITIONS:
        selected = [row for row in rows if row["condition"] == name]
        scored = [row for row in selected if row["accuracy"] is not None and row["accuracy"].get("reason") is None]
        passed = [row for row in scored if row["accuracy"]["productPass"]]
        host_succeeded = [row for row in selected if row['runStatus'] == 'FINISHED' and row['verdict'] == 'SUCCESS']
        host_passed = [row for row in passed if row in host_succeeded]
        failed = [row for row in selected if any(c['status'] == 'FAIL' for c in row['checkOutcomes'])]
        unavailable = [row for row in selected if not row['scored']]
        unverified = [row for row in selected if row['scored'] and not any(
            c['status'] == 'FAIL' for c in row['checkOutcomes']) and row not in passed]
        complete_cost = [row["metrics"]["costUsd"] for row in selected if row["metrics"]["costComplete"]]
        groups[name] = {"architecture": architecture, "loopPolicy": loop_policy,
            "planned": planned, "executed": len(selected), "scored": len(scored),
            "productPasses": len(passed), "productPassRate": len(passed) / len(selected) if selected else None,
            "confirmedProductFailureRuns": len(failed), "evaluationUnavailableRuns": len(unavailable),
            "evaluationUnverifiedRuns": len(unverified),
            "hostSuccesses": len(host_succeeded),
            "productPassedWithoutHostSuccess": len(passed) - len(host_passed),
            "meanRequirementSatisfaction": mean([r["accuracy"]["fullSatisfactionLower"] for r in scored]),
            "meanPassingVerdictSeconds": mean([None if r["metrics"]["verdictMs"] is None else r["metrics"]["verdictMs"] / 1000 for r in host_passed]),
            "passingVerdictTimeSamples": sum(r["metrics"]["verdictMs"] is not None for r in host_passed),
            "meanElapsedSeconds": mean([None if r["metrics"]["totalMs"] is None else r["metrics"]["totalMs"] / 1000 for r in selected]),
            "meanCostUsd": mean(complete_cost), "completeCostSamples": len(complete_cost),
            "meanFixAttempts": mean([r["metrics"]["fixAttempts"] for r in selected]),
            "pausedRuns": sum(r["runStatus"] in ("WAITING_INPUT", "HUMAN_REVIEW") for r in selected)}
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--repetitions", type=int, default=10, choices=range(1, 11))
    parser.add_argument("--run", action="store_true")
    options = parser.parse_args()
    os.umask(0o077)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    root = options.out.resolve()
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    frozen = implementation()
    smoke.write_json(root / "implementation-sha256.json", frozen)
    experiment_id, queue = str(uuid4()), []
    baseline = None
    for rep in range(1, options.repetitions + 1):
        ordering = CONDITIONS if rep % 2 else tuple(reversed(CONDITIONS))
        for name, architecture, loop_policy in ordering:
            order = len(queue) + 1
            out = root / f"{order:02d}-{name}-r{rep:02d}"
            prepared = smoke.prepare(out, architecture=architecture, loop_policy=loop_policy,
                experiment_id=experiment_id, execution_order=order)
            runtime, tools, body, files = prepared
            # Only architecture and practice scopes may differ between conditions.
            common = {"model": runtime["model"], "limits": runtime["llmLimits"],
                "runtimeBudgetMs": runtime["runtimeBudgetMs"], "image": tools["imageReference"],
                "lock": tools["dependencyLockHash"], "hardware": tools["hardwareProfile"],
                "source": body["configuration"]["startingCommitHash"], "request": body["requestText"],
                "validationTargetPolicy": smoke.VALIDATION_TARGET_POLICY,
                "tools": {k: v for k, v in tools.items() if k != "unit"},
                "unit": {**tools["unit"], "scopes": [s for s in tools["unit"]["scopes"] if s["kind"] != "PROTECTED"]}}
            if baseline is not None and baseline != common:
                raise RuntimeError("COMPARISON_PARITY_MISMATCH")
            baseline = common
            queue.append((out, prepared, name, rep, order))
    smoke.write_json(root / "plan.json", {"experimentId": experiment_id, "repetitions": options.repetitions,
        "exam": "final", "common": baseline, "maximumSubmissions": len(queue),
        "schedule": [{"condition": name, "repetition": rep, "order": order, "directory": str(out)}
                     for out, _prepared, name, rep, order in queue]})
    print(json.dumps({"status": "PREPARED", "runs": len(queue), "out": str(root)}), flush=True)
    if not options.run:
        return 0
    # Credentials stay in process environments; neither values nor headers are written.
    smoke.runtime_environment()
    subprocess.run(["docker", "image", "inspect", baseline["image"]], capture_output=True, check=True, timeout=20)
    rows, consecutive_pauses, stopped = [], 0, None
    for out, prepared, name, rep, order in queue:
        if implementation() != frozen:
            stopped = "IMPLEMENTATION_CHANGED"
            break
        print(json.dumps({"status": "STARTING", "order": order, "condition": name, "repetition": rep}), flush=True)
        try:
            smoke.run(out, prepared, exam="final")
            row = summarize(out, name, rep, order)
        except Exception as error:
            code = getattr(error, "code", None)
            stopped = code if isinstance(code, str) and re.fullmatch(r"[A-Z_0-9]+", code) else type(error).__name__
            smoke.write_json(out / "failure.json", {"code": stopped, "automaticResubmission": False})
            break
        rows.append(row)
        consecutive_pauses = consecutive_pauses + 1 if row["runStatus"] in ("WAITING_INPUT", "HUMAN_REVIEW") else 0
        smoke.write_json(root / "results.json", {"plannedRuns": len(queue), "completedRuns": len(rows),
            "rows": rows, "groups": means(rows, options.repetitions), "stoppedReason": None})
        print(json.dumps({"status": "RUN_COMPLETE", "condition": name, "repetition": rep,
            "runStatus": row["runStatus"], "satisfaction": (row["accuracy"] or {}).get("fullSatisfactionLower"),
            "productPass": (row["accuracy"] or {}).get("productPass"), "costUsd": row["metrics"]["costUsd"]}), flush=True)
        if consecutive_pauses >= 3:
            stopped = "THREE_CONSECUTIVE_PAUSED_RUNS"
            break
    smoke.write_json(root / "results.json", {"plannedRuns": len(queue), "completedRuns": len(rows),
        "rows": rows, "groups": means(rows, options.repetitions), "stoppedReason": stopped})
    print(json.dumps({"status": "STOPPED" if stopped else "COMPLETE", "reason": stopped,
                      "completed": len(rows), "planned": len(queue)}), flush=True)
    return 1 if stopped else 0


if __name__ == "__main__":
    raise SystemExit(main())
