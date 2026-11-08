"""Reproducible rich-fixture matrix; no LLM, deployment or arbitrary commands.

Run: python -m diagnostics.communication.strict_suite --out NEW_DIRECTORY
Full raw observations are streamed into gzip JSONL. A small summary and fixture
references remain readable without loading the raw payload repeatedly.
"""

import argparse
import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass
import gzip
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
from uuid import uuid4

import httpx
from a2a.types import TaskState
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError

from diagnostics.communication.contracts import CheckOptions, ProbeRecord, SCENARIOS
from diagnostics.communication.lab import running_lab
from diagnostics.communication.workloads import fixture_statistics, load_request, PROFILES
from orchestrator.a2a import A2AAgentClient, A2AWorkflowMetadata


SEMANTIC_FAULTS = {"response_requirement_drop", "response_limit_change", "permission_swap", "invented_evidence", "invented_decision"}


@dataclass(frozen=True)
class Case:
    name: str
    category: str
    options: CheckOptions


def plan(quick=False):
    result = []
    for profile in PROFILES[1:]:
        for architecture in ("MULTI_AGENT", "SINGLE_AGENT"):
            for batch in range(1 if quick else 4):
                result.append(Case(f"normal.{profile}.{architecture}.{batch+1}", "positive",
                                   CheckOptions(profile=profile, architecture=architecture, rounds=2 if quick else 5, processing_ms=0)))
            result.append(Case(f"large.{profile}.{architecture}", "positive_large",
                               CheckOptions(profile=profile, architecture=architecture, rounds=1 if quick else 2, processing_ms=0, padding_kib=32)))
            result.append(Case(f"oversize.{profile}.{architecture}", "boundary",
                               CheckOptions(profile=profile, architecture=architecture, rounds=1, processing_ms=0, padding_kib=96)))
    for scenario in SCENARIOS:
        if scenario in {"normal", "slow_sender", "slow_agent"}:
            continue
        for architecture in ("MULTI_AGENT", "SINGLE_AGENT"):
            result.append(Case(f"fault.documents.{scenario}.{architecture}", "negative",
                               CheckOptions(profile="documents", architecture=architecture, scenario=scenario, rounds=1 if quick else 2, processing_ms=0)))
    for profile in PROFILES[1:]:
        for role in ("PLANNER", "DEVELOPER", "QA", "SECURITY"):
            result.append(Case(f"role.{profile}.{role}", "negative_role",
                               CheckOptions(profile=profile, scenario="response_limit_change", fault_target=role, rounds=1, processing_ms=0)))
    if not quick:
        for scenario in ("slow_sender", "slow_agent"):
            result.append(Case(f"delay.{scenario}", "delay", CheckOptions(profile="documents", scenario=scenario, rounds=1, processing_ms=0)))
    return result


def row_clean(row):
    c = row["checks"]
    return (row["phase"] == "COMPLETED" and c["identity"] == "PASS"
            and all(item["status"] == "PASS" for item in c["request"].values())
            and all(c[key]["status"] == "PASS" for key in ("response", "metadata", "responseMetadata", "grounding", "sourceGrounding")))


def judge(case, report):
    rows = report["rows"]
    target = [r for r in rows if r["role"] in {case.options.fault_target, "SINGLE_AGENT"}]
    issues = []
    if case.category in {"positive", "positive_large", "delay"}:
        for row in rows:
            if not row_clean(row):
                issues.append(row["role"] + ": 정상 조건에서 실패 또는 미관측")
        if case.options.architecture == "SINGLE_AGENT":
            if len({r["contextId"] for r in rows}) != 1 or len({r["taskId"] for r in rows}) != case.options.rounds:
                issues.append("Single Context/Task 반복 계약 위반")
    elif case.category == "boundary":
        active = [row for row in rows if row["phase"] != "SKIPPED"]
        if len(active) != case.options.rounds:
            issues.append("경계 실험 요청 수 불일치")
        for row in active:
            if not (row["httpStatus"] == 413 and row["observations"]["executorReceived"] is None
                    and row["observations"]["responseReceived"] is None and row["taskId"] is None):
                issues.append("64KiB 초과 요청의 413 거절·실행기 미실행 확인 실패")
    else:
        for row in target:
            checks, scenario = row["checks"], case.options.scenario
            if scenario == "no_response":
                valid = row["phase"] == "TIMEOUT" and row["cleanupState"] == "TASK_STATE_CANCELED"
            elif scenario == "foreign_artifact":
                result = row["observations"]["responseSent"]
                valid = row["phase"] == "ERROR" and result is not None and result["probeId"] != row["id"]
            elif scenario in {"context_spoof", "message_id_change"}:
                valid = checks["identity"] == "FAIL"
            elif scenario == "metadata_change":
                valid = checks["metadata"]["status"] == checks["responseMetadata"]["status"] == "FAIL"
            elif scenario == "response_change":
                valid = checks["response"]["status"] == "FAIL" and checks["grounding"]["status"] == "PASS"
            elif scenario in SEMANTIC_FAULTS or scenario == "grounding_error":
                valid = (checks["grounding"]["status"] == "FAIL" and checks["response"]["status"] == "PASS"
                         and checks["request"]["endToEnd"]["status"] == "PASS")
            else:
                valid = checks["request"]["endToEnd"]["status"] == "FAIL"
            if not valid:
                issues.append(row["role"] + ": 주입 오류 미탐지 또는 경계 분류 오류")
        for row in rows:
            if row in target or row["phase"] == "SKIPPED":
                continue
            if not row_clean(row):
                issues.append(row["role"] + ": 오류 비주입 역할에서 오탐/실패")
    return {"passed": not issues, "issues": issues, "targetCount": len(target)}


def compact(case, report, verdict):
    return {"case": case.name, "category": case.category, "options": report["options"],
            "fixture": report["fixture"], "runId": report["runId"],
            "elapsedMs": report["elapsedMs"], "summary": report["summary"], "verdict": verdict,
            "rows": [{"id": r["id"], "role": r["role"], "round": r["round"], "phase": r["phase"],
                      "taskId": r["taskId"], "contextId": r["contextId"], "wireBytes": r["wireBytes"],
                      "httpStatus": r["httpStatus"], "error": r["error"], "cleanupState": r["cleanupState"],
                      "injection": r["injection"], "timings": r["timings"], "observerCallbackMs": r["observerCallbackMs"],
                      "checks": r["checks"]} for r in report["rows"]]}


async def protocol_checks(lab):
    """Real duplicate delivery, conflicting reuse, cross-Run/role isolation."""
    row = ProbeRecord("DEVELOPER", 1, 0, "normal")
    lab.records[row.probe_id] = row
    doc = load_request("documents")
    payload = {"probeId": row.probe_id, "profile": "documents", "workRequest": doc,
               "sample": {"value": 100, "label": "중복 전송 검사", "requirementIds": [r["id"] for r in doc["requirements"]]},
               "upstream": None, "request": "같은 메시지의 재전송은 같은 Task를 반환해야 합니다."}
    meta = A2AWorkflowMetadata(runId=uuid4(), workflowStepId=uuid4(), scenarioId=uuid4(), attempt=0,
                               requirementIds=[uuid4() for _ in doc["requirements"]])
    message_id = uuid4()
    results = []
    async with httpx.AsyncClient(trust_env=False, timeout=5,
                                 headers={"Authorization": "Bearer " + lab.tokens["DEVELOPER"]}) as transport:
        client = A2AAgentClient(lab.endpoints["DEVELOPER"], httpx_client=transport)
        first = await client.send_task(payload, meta, message_id=message_id)
        for _ in range(100):
            if first.status.state == TaskState.TASK_STATE_COMPLETED:
                break
            await asyncio.sleep(.02)
            first = await client.get_task(first.id)
        duplicate = await client.send_task(payload, meta, message_id=message_id)
        results.append({"name": "duplicate_same_message", "passed": first.id == duplicate.id and first.context_id == duplicate.context_id and row.executor_invocations == 1,
                        "firstTaskId": first.id, "duplicateTaskId": duplicate.id, "executorInvocations": row.executor_invocations})
        changed = {**payload, "sample": {**payload["sample"], "value": 101}}
        rejected = False
        try:
            await client.send_task(changed, meta, message_id=message_id)
        except InvalidParamsError:
            rejected = True
        results.append({"name": "duplicate_conflicting_body", "passed": rejected and row.executor_invocations == 1,
                        "expected": "InvalidParamsError without second execution"})
        rejected = False
        foreign = meta.model_copy(update={"run_id": uuid4(), "workflow_step_id": uuid4()})
        try:
            await client.send_task(payload, foreign, context_id=first.context_id)
        except InvalidParamsError:
            rejected = True
        results.append({"name": "foreign_run_context", "passed": rejected and row.executor_invocations == 1,
                        "expected": "InvalidParamsError without new execution"})
    async with httpx.AsyncClient(trust_env=False, timeout=5,
                                 headers={"Authorization": "Bearer " + lab.tokens["QA"]}) as transport:
        client = A2AAgentClient(lab.endpoints["QA"], httpx_client=transport)
        rejected = False
        try:
            await client.get_task(first.id)
        except TaskNotFoundError:
            rejected = True
        results.append({"name": "foreign_role_task", "passed": rejected, "expected": "TaskNotFoundError"})
    return results


async def run(out, quick=False):
    out.mkdir(parents=True, exist_ok=False)
    cases = plan(quick)
    results = []
    started = datetime.now(timezone.utc).isoformat()
    raw_path = out / "observations.jsonl.gz"
    async with running_lab() as lab:
        protocol = await protocol_checks(lab)
        print("Protocol checks: " + str(sum(p["passed"] for p in protocol)) + "/" + str(len(protocol)), flush=True)
        with gzip.open(raw_path, "wt", encoding="utf-8", compresslevel=6) as archive:
            for index, case in enumerate(cases, 1):
                report = await lab.execute(case.options, lambda event: None)
                verdict = judge(case, report)
                archive.write(json.dumps({"case": case.name, "category": case.category,
                                          "verdict": verdict, "report": report}, ensure_ascii=False, separators=(",", ":")) + "\n")
                results.append(compact(case, report, verdict))
                print(f"{index:02}/{len(cases)} {'PASS' if verdict['passed'] else 'FAIL'} {case.name}" + (" " + "; ".join(verdict["issues"]) if verdict["issues"] else ""), flush=True)
    assets = Path(__file__).parent
    hashes = {str(p.relative_to(assets)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in sorted(assets.rglob("*")) if p.suffix in {".py", ".json", ".html"}}
    output = {"version": "strict-v2", "synthetic": True, "llmUsed": False, "quick": quick,
              "startedAt": started, "finishedAt": datetime.now(timezone.utc).isoformat(),
              "environment": {"python": platform.python_version(), **{key: version(key) for key in ("a2a-sdk", "httpx", "uvicorn", "fastapi")}},
              "fixtures": [fixture_statistics(p) for p in PROFILES[1:]], "sourceSha256": hashes,
              "protocolChecks": protocol,
              "rawEvidence": {"file": raw_path.name, "sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
                              "bytes": raw_path.stat().st_size},
              "totals": {"cases": len(results), "casesPassed": sum(r["verdict"]["passed"] for r in results),
                         "planned": sum(r["summary"]["planned"] for r in results),
                         "sent": sum(r["summary"]["sent"] for r in results),
                         "received": sum(r["summary"]["received"] for r in results),
                         "responses": sum(r["summary"]["responses"] for r in results)}, "results": results}
    (out / "summary.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    return output


def main():
    parser = argparse.ArgumentParser(description="복합 사용자 요청·분리 정답·오류 주입을 사용하는 엄격 A2A 검증")
    parser.add_argument("--out", type=Path, required=True, help="새 출력 폴더. 기존 증거를 덮어쓰지 않습니다.")
    parser.add_argument("--quick", action="store_true", help="발견용 축소 행렬; 최종 검증 표본으로 대체하지 않습니다.")
    args = parser.parse_args()
    output = asyncio.run(run(args.out, args.quick))
    print(json.dumps(output["totals"], ensure_ascii=False), flush=True)
    raise SystemExit(0 if output["totals"]["casesPassed"] == output["totals"]["cases"] and all(c["passed"] for c in output["protocolChecks"]) else 1)


if __name__ == "__main__":
    main()
