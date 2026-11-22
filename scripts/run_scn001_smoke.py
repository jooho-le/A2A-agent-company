"""Prepare, or explicitly run, one isolated Multi Ver1 diagnostic with real APIs.

No existing configuration, source baseline, project commit or previous Run is
changed. This is a smoke experiment, not a four-condition comparison.
"""
import argparse
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
REQUEST = "이메일과 비밀번호로 회원가입 기능을 만들어줘."
VALIDATION_TARGET_POLICY = 'SCN001_API_DB_SECURITY_NO_UNSPECIFIED_UI_V1'


def api_only_tools(tools, contract):
    """Build a new API experiment configuration; never mutate an approved Run.

    SCN-001's public contract declares an API, not an HTML signup page. New
    runs therefore do not inherit a demonstration browser scope from local
    tools. Protected suites must never be removed by this normalization.
    """
    if (contract.get('scenarioKey') != 'SCN-001'
            or not contract.get('externalContract', {}).get('signupApi')
            or any(k in contract.get('externalContract', {}) for k in ('ui', 'frontend', 'pages', 'browser'))):
        raise ValueError('SCN001_API_CONTRACT_REQUIRED')
    copied = json.loads(json.dumps(tools))
    browser = copied.get('browser')
    if browser is not None and any(s.get('kind') != 'QA_TESTS' for s in browser.get('suites', ())):
        raise ValueError('SCN001_BROWSER_PROTECTED_POLICY_CONFLICT')
    copied['browser'] = None
    return copied


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def runtime_environment(root=ROOT):
    environment = {**{k: v for k, v in dotenv_values(root / ".env").items() if v is not None}, **os.environ}
    if not environment.get("A2A_PLATFORM_LLM_API_KEY"):
        environment["A2A_PLATFORM_LLM_API_KEY"] = environment.get("OPENAI_API_KEY", "")
    if not environment["A2A_PLATFORM_LLM_API_KEY"]:
        raise RuntimeError("OPENAI_API_KEY_MISSING")
    for name in ("A2A_HOST_API_TOKEN", *(f"A2A_PLATFORM_{role}_TOKEN" for role in
                                           ("PLANNER", "DEVELOPER", "QA", "SECURITY", "SINGLE_AGENT"))):
        if not environment.get(name):
            environment[name] = secrets.token_hex(32)
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def prepare(out, *, runtime_seconds=1200, total_tokens=2_000_000, architecture="MULTI_AGENT",
            loop_policy="IN_LOOP_AND_FINAL", experiment_id=None, execution_order=1):
    from agents.platform.host import inspect_host_configuration
    from evaluation import protected_tests
    from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario

    if architecture not in ("MULTI_AGENT", "SINGLE_AGENT") or loop_policy not in ("IN_LOOP_AND_FINAL", "FINAL_ONLY"):
        raise ValueError("UNSUPPORTED_EXPERIMENT_CONDITION")
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    runtime_name = "single-runtime.local.json" if architecture == "SINGLE_AGENT" else "owned-runtime.local.json"
    runtime = json.loads((ROOT / "configs/experiment/ver1" / runtime_name).read_text())
    runtime["logLevel"] = "WARNING"
    runtime["orchestrator"] = {"port": 18002, "databasePath": str(out / "host.sqlite3")}
    runtime["workspaceRoot"] = str(out / "workspaces")
    runtime["runtimeBudgetMs"] = runtime_seconds * 1000
    runtime["apiKeyEnv"] = "A2A_PLATFORM_LLM_API_KEY"
    runtime["llmLimits"].update(max_total_tokens=total_tokens, max_output_corrections=2,
                                max_question_reminders=1, max_provider_retries=1)
    for index, (role, entry) in enumerate(runtime["agents"].items()):
        entry.update(port=18101 + index, databasePath=str(out / f"{role.lower()}.sqlite3"))
    scenario_contract = get_scenario(SCN_001_ID).planner_contract()
    tools = api_only_tools(json.loads((ROOT / "configs/experiment/ver1/owned-tools.local.json").read_text()),
                           scenario_contract)
    # Regenerate operator-owned practice inputs from this exact source edition.
    # Old local tools files contain embedded historical copies of these tests.
    scopes = [scope for scope in tools["unit"]["scopes"] if scope["kind"] != "PROTECTED"]
    suite_ref = protected_tests.loop_suite_ref()
    if loop_policy == "IN_LOOP_AND_FINAL":
        scopes += [protected_tests.unit_test_scope(name, suite_ref=suite_ref)
                   for name in ("in-loop-qa", "in-loop-security")]
    tools["unit"]["scopes"] = scopes
    write_json(out / "runtime.json", runtime)
    write_json(out / "tools.json", tools)
    write_json(out / "cases.json", protected_tests.protected_case_file(get_scenario(SCN_001_ID).planner_contract()))
    files = {"runtime_file": out / "runtime.json", "tools_file": out / "tools.json",
             "source_file": ROOT / "configs/owned-source.local.json",
             "protected_cases_file": out / "cases.json" if loop_policy == "IN_LOOP_AND_FINAL" else None,
             "price_book_file": ROOT / "configs/owned-price-book.scn001.json"}
    inspected = inspect_host_configuration(**files, architecture=architecture)
    configuration = inspected.approved_configuration.model_dump(mode="json", by_alias=True)
    configuration.update(experimentId=str(experiment_id or uuid4()), executionOrder=execution_order,
                         warmupState="FRESH_WORKSPACE_COLD_START")
    body = {"scenarioId": str(SCN_001_ID), "requestText": REQUEST, "configuration": configuration}
    write_json(out / "request.json", body)
    write_json(out / "setup.json", {"kind": "ONE_RUN_SMOKE_NOT_COMPARISON", "architecture": architecture,
               "loopPolicy": loop_policy, "runtimeSeconds": runtime_seconds,
               "totalTokenLimit": total_tokens, "requestText": REQUEST,
               "validationTargetPolicy": VALIDATION_TARGET_POLICY,
               "image": tools["imageReference"], "practiceSuiteRef": suite_ref,
               "workingSourceDiffSha256": hashlib.sha256(subprocess.check_output(
                   ["git", "diff", "--binary", "HEAD", "--", "src", "schemas"], cwd=ROOT)).hexdigest()})
    return runtime, tools, body, files


def run(out, prepared, *, exam="pilot"):
    import httpx
    from evaluation.experiment_run import export
    from evaluation.final_evaluation import DockerRunner
    from evaluation.workflow import ingest

    runtime, tools, body, files = prepared
    environment = runtime_environment()
    subprocess.run(["docker", "image", "inspect", tools["imageReference"]],
                   capture_output=True, check=True, timeout=20)
    for port in (18002, *(agent["port"] for agent in runtime["agents"].values())):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
    architecture = body["configuration"]["architecture"]
    command = [sys.executable, "-m", "agents.platform.run_host", "--architecture", architecture]
    for key, option in (("runtime_file", "--runtime"), ("tools_file", "--tools"),
                        ("source_file", "--source"), ("protected_cases_file", "--qa-cases"),
                        ("price_book_file", "--price-book")):
        if files[key] is not None:
            command += [option, str(files[key])]
    command.append("--start")
    with (out / "host.log").open("w", encoding="utf-8") as log:
        host = subprocess.Popen(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
        write_json(out / "host-process.json", {"pid": host.pid})
        try:
            headers = {"Authorization": "Bearer " + environment["A2A_HOST_API_TOKEN"]}
            with httpx.Client(base_url="http://127.0.0.1:18002", headers=headers, timeout=30) as client:
                for _ in range(60):
                    if host.poll() is not None:
                        raise RuntimeError("SMOKE_HOST_START_FAILED")
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(1)
                else:
                    raise RuntimeError("SMOKE_HOST_START_TIMEOUT")
                # Exactly one POST. An ambiguous response is recorded and never retried here.
                key = str(uuid4())
                write_json(out / "submission.json", {"idempotencyKey": key, "status": "INTENT_RECORDED"})
                response = client.post("/api/v1/runs", json=body, headers={"Idempotency-Key": key})
                if response.status_code not in (200, 201):
                    write_json(out / "submission-error.json", {"status": response.status_code,
                               "response": response.json()})
                    raise RuntimeError(f"SMOKE_SUBMISSION_REJECTED_{response.status_code}")
                run_id = response.json()["run"]["runId"]
                write_json(out / "submission.json", {"idempotencyKey": key, "runId": run_id, "status": "CREATED"})
                print(json.dumps({"runId": run_id, "status": "SUBMITTED"}), flush=True)
                previous = None
                deadline = time.monotonic() + runtime["runtimeBudgetMs"] / 1000 + 60
                while time.monotonic() < deadline:
                    response = client.get(f"/api/v1/runs/{run_id}")
                    response.raise_for_status()
                    current = response.json()
                    signature = current.get("status"), current.get("codeVersion"), current.get("fixAttempt")
                    if signature != previous:
                        print(json.dumps({"status": signature[0], "codeVersion": signature[1], "fixAttempt": signature[2]}), flush=True)
                        previous = signature
                    if current.get("status") in ("FINISHED", "ABORTED", "WAITING_INPUT", "HUMAN_REVIEW"):
                        break
                    time.sleep(3)
                else:
                    raise RuntimeError("SMOKE_RUN_TIMEOUT_NO_RESUBMISSION")
                write_json(out / "run.json", current)
            plan = {"hostConfigurations": {architecture: {
                "api": "http://127.0.0.1:18002", "runtime": str(files["runtime_file"]),
                "tools": str(files["tools_file"]), "source": str(files["source_file"]),
                "priceBook": str(files["price_book_file"]),
                **({"protectedCases": str(files["protected_cases_file"])} if files["protected_cases_file"] else {})}}}
            exported = export(plan, architecture, run_id, out / "export", ROOT)
            target = ingest(exported, results=out / "evaluation", runner=DockerRunner(tools["imageReference"]), exam=exam)
            print(json.dumps({"status": "EVALUATED", "results": str(target)}), flush=True)
            return target
        finally:
            if host.poll() is None:
                host.send_signal(signal.SIGINT)
                try:
                    host.wait(timeout=90)
                except subprocess.TimeoutExpired:
                    # Keep ownership and wait for the Host's cleanup; never leave a paid Run detached.
                    host.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="실제 OpenAI API를 호출하는 한 번의 실행")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--runtime-seconds", type=int, default=1200, choices=range(60, 3601))
    args = parser.parse_args()
    os.umask(0o077)
    suffix = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d-%H%M%S")
    out = (args.out or ROOT / ".data/experiments" / f"multi-ver1-smoke-{suffix}").resolve()
    prepared = None
    try:
        prepared = prepare(out, runtime_seconds=args.runtime_seconds)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        print(json.dumps({"status": "PREPARED", "out": str(out), "paidExecutionRequested": args.run}), flush=True)
        if args.run:
            run(out, prepared)
    except Exception as error:
        # Do not expose provider responses, credentials or nested validation input.
        code = getattr(error, "code", None)
        if not isinstance(code, str) or not code.replace("_", "").isalnum():
            code = str(error) if isinstance(error, RuntimeError) and str(error).startswith("SMOKE_") else type(error).__name__
        if prepared is not None:
            write_json(out / "failure.json", {"code": code})
        print(json.dumps({"status": "FAILED", "code": code, "out": str(out)}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
