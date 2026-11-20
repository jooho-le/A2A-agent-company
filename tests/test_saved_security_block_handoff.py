"""Opt-in offline replay of Single/Multi Ver1/Ver2 using the exported Single Ver1 snapshot.

Set CAPSTONE_HANDOFF_ARCHIVE to the single-ver1/Host기록 export directory (next to runtime.json). Requires
local prepared experiment profiles and their Docker image. The Host, A2A, tool
processes, generated/protected tests, budgets, and receiver stores are real.
Only model output is scripted. All authentication values are disposable.
"""
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import tarfile
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
import pytest

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, ToolCall
from agents.platform.host import approved_run_configuration, create_host
from agents.platform.evaluation_export import write_evaluation_export
from orchestrator.a2a.client import A2AAgentClient
from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario
from semantic_helpers import SemanticFakeProvider
from test_llm_runtime import text_response, tool_response
from test_runtime_test_data import BAD_SOURCE

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = os.getenv("CAPSTONE_HANDOFF_ARCHIVE")
pytestmark = pytest.mark.skipif(not ARCHIVE, reason="requires archived source and its local Docker image")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def load_snapshot(archive, version):
    folder = archive / version
    source = folder / "source.tar"
    manifest = read_json(folder / "manifest.json")
    assert hashlib.sha256(source.read_bytes()).hexdigest() == manifest["snapshotSha256"]
    with tarfile.open(source) as stream:
        members = stream.getmembers()
        assert all(m.isfile() and not Path(m.name).is_absolute() and ".." not in Path(m.name).parts for m in members)
        files = {m.name: stream.extractfile(m).read() for m in members}
        assert len(files) == len(members)
    return manifest, files


def prepare_source(directory, archive):
    manifest, files = load_snapshot(archive, "v1")
    v2, candidate = load_snapshot(archive, "v2")
    assert files.keys() == candidate.keys()
    assert [name for name in files if files[name] != candidate[name]] == ["main.py"]
    seed = directory / "baseline"
    seed.mkdir()
    for name, content in files.items():
        target = seed / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def git(*args):
        return subprocess.check_output(["git", "-c", "user.name=Offline Fixture", "-c",
            "user.email=replay@example.invalid", "-c", "commit.gpgsign=false", "-c",
            "core.hooksPath=/dev/null", *args], cwd=seed, text=True).strip()

    git("init", "-q", "--template=")
    git("add", ".")
    git("commit", "-qm", "Archived v1 baseline for v2 handoff replay")
    commit = git("rev-parse", "HEAD")
    snapshot = GitSnapshotBuilder(seed).build(commit, "requirements.lock")
    assert snapshot.snapshot_sha256 == manifest["snapshotSha256"]
    source_config = directory / "source.json"
    write_json(source_config, {"schemaVersion": 1, "repositoryPath": str(seed),
        "repositoryId": manifest["repositoryId"], "commitHash": commit, "snapshotSha256": snapshot.snapshot_sha256,
        "lockPath": "requirements.lock", "gitTimeoutSeconds": 30})
    return source_config, v2["snapshotSha256"], candidate["main.py"].decode()


def archived_qa(archive):
    report, = [a for a in read_json(archive / "trace.json")["artifacts"] if a["artifactType"] == "QA_REPORT"]
    cases = [{"toolName": "run_unit_tests", "selector": "qa-unit", "testId": t["normalizedLocation"],
              "requirementId": t["requirementId"], "title": t["title"], "expectedResult": t["expectedResult"]}
             for t in report["tests"] if t["testId"].startswith("run_unit_tests:qa-unit:")]
    contents = {p.read_text() for p in (archive / "observed-files").glob("qa-*/*.txt")
                if "def test_duplicate_canonical_email(" in p.read_text()}
    source, = contents
    return cases, source


class OfflineResponses(SemanticFakeProvider):
    # Satisfy the production Host's provider-identity check. complete() never sends HTTP.
    name = "openai"

    def __init__(self, candidate, cases, qa_source):
        super().__init__()
        self.candidate, self.cases, self.qa_source = candidate, cases, qa_source
        self.counts = Counter()
        self.developer_fix_received = False
        self.model_feedback = []

    async def aclose(self):
        pass

    async def complete(self, request):
        kind = request.output.name
        if kind == "product_semantic_review_v5":
            return await super().complete(request)
        self.counts[kind] += 1
        count = self.counts[kind]
        data = json.loads(json.loads(request.input_items_json)[0]["content"])["taskInput"]

        def write(path, source, tool):
            return tool_response(ToolCall(call_id=f"offline-{kind}-{count}", name=tool,
                arguments_json=json.dumps({"workspaceId": data["workspaceId"], "path": path, "content": source})))

        if kind.startswith("planner_decision"):
            ids = get_scenario(SCN_001_ID).requirement_ids
            return text_response(json.dumps({"kind": "PLAN", "questions": [], "unapprovedAssumptions": [],
                "implementationPlan": [{"taskId": "TASK-001", "title": "회원가입 구현",
                    "description": "동결된 회원가입 수용 기준대로 구현하고 검증한다.",
                    "requirementIds": [str(i) for i in ids], "dependsOn": []}]}))
        if kind == "developer_decision":
            if data.get("fixRequest"):
                self.developer_fix_received = True
                self.model_feedback.extend(data["fixRequest"].get("validationFeedback", []))
                # The experiment ends at delivery. Do not script or claim a product repair.
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
            if count == 1:
                return write("source/main.py", self.candidate, "write_source_file")
            return text_response(json.dumps({"kind": "READY", "questions": [], "rejection": None,
                                            "summary": "회원가입 경로와 입력 검증을 구현했습니다."}))
        if kind == "qa_decision":
            if count == 1 or data.get("testRunFeedback") and count == 3:
                source = self.qa_source
                if count == 3:
                    # Deterministic QA test-only repair, on a fresh identity. No change to candidate or assertions.
                    source = source.replace("statuses, row_info = self.concurrent_signup_statuses(payload, payload)",
                        "fresh = dict(payload, email='another' + canonical_email)\n"
                        "        statuses = tuple(s // 100 for s in self.concurrent_signup_statuses(fresh, fresh))\n"
                        "        row_info = self.signup_email_state(fresh['email'])")
                    source = source.replace("row_info['totalRows']", "row_info['exactEmailRows']")
                return write("outputs/qa/tests/test_signup.py", source, "write_test_file")
            return text_response(json.dumps({"kind": "READY", "questions": [], "cases": self.cases}))
        if kind == "security_decision":
            assert count <= 2, "unexpected third attempt after the blocked rewrite"
            return write("outputs/security/tests/test_security.py", BAD_SOURCE, "write_test_file")
        raise AssertionError(f"unexpected offline response contract: {kind}")


@pytest.mark.parametrize("single", (True, False), ids=("single", "multi"))
@pytest.mark.parametrize("ver1", (True, False), ids=("ver1", "ver2"))
def test_stopped_security_receipt_delivery(tmp_path, single, ver1):
    asyncio.run(exercise(tmp_path.resolve(), single, ver1))


async def exercise(directory, single, ver1):
    archive = Path(ARCHIVE).resolve()
    config = ROOT / "configs" / "experiment"
    mode = ("single" if single else "multi") + ("-ver1" if ver1 else "-ver2")
    source_file, digest, candidate = prepare_source(directory, archive)
    cases, qa_source = archived_qa(archive)
    runtime = read_json(config / ("single-runtime.local.json" if single else "owned-runtime.local.json"))
    runtime["orchestrator"]["databasePath"] = "state/host.sqlite3"
    runtime["workspaceRoot"] = "state/workspaces"
    runtime["logLevel"] = "WARNING"
    # Do not touch the original Host/agent databases or use local API keys.
    env = {key: secrets.token_hex(24) for key in ["A2A_HOST_API_TOKEN", runtime["apiKeyEnv"]]}
    stores = {}
    for role, settings in runtime["agents"].items():
        settings["databasePath"] = f"state/{role}.sqlite3"
        env[settings["bearerTokenEnv"]] = secrets.token_hex(24)
        stores[role] = directory / settings["databasePath"]
    historical = read_json(archive.parent / "runtime.json")
    for key in ("model", "llmLimits", "runtimeBudgetMs"):
        runtime[key] = historical[key]
    runtime_file = directory / "runtime.json"
    write_json(runtime_file, runtime)
    profile = config / ("ver1" if ver1 else "ver2")
    providers = []
    def scripted(_settings):
        provider = OfflineResponses(candidate, cases, qa_source)
        providers.append(provider)
        return provider

    clients = []
    def a2a(url):
        role = next(k for k, v in runtime["agents"].items() if v["port"] == urlsplit(url).port)
        app = next(app for identity, app in host.agent_apps.items() if identity.value == role)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=url,
            headers={"Authorization": "Bearer " + env[runtime["agents"][role]["bearerTokenEnv"]]})
        clients.append(client)
        return A2AAgentClient(url, httpx_client=client)

    host = await create_host(runtime_file=runtime_file, tools_file=profile / "owned-tools.local.json",
        source_file=source_file, protected_cases_file=profile / "owned-protected-cases.local.json" if ver1 else None,
        provider_factory=scripted, environ=env, a2a_client_factory=a2a,
        architecture="SINGLE_AGENT" if single else "MULTI_AGENT")
    try:
        async with AsyncExitStack() as stack:
            for app in (*host.agent_apps.values(), host.orchestrator_app):
                await stack.enter_async_context(app.router.lifespan_context(app))
            client = await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(
                app=host.orchestrator_app), base_url="http://127.0.0.1"))
            config_value = approved_run_configuration(host).model_dump(mode="json", by_alias=True)
            response = await client.post("/api/v1/runs", json={"scenarioId": str(SCN_001_ID),
                "requestText": "회원가입 기능을 구현하고 검증해 주세요.", "configuration": config_value},
                headers={"Authorization": "Bearer " + env["A2A_HOST_API_TOKEN"], "Idempotency-Key": str(uuid4())})
            assert response.status_code == 201, response.text
            run_id = UUID(response.json()["run"]["runId"])
            repo = host.orchestrator_app.state.workflow_repository
            write_evaluation_export(repo, run_id, directory / "export", stores=stores)
    finally:
        for client in clients:
            await client.aclose()
        await host.aclose()

    exported = directory / "export"
    trace = read_json(exported / "trace.json")
    events = [e["eventType"] for e in trace["events"]]
    sources = [a for a in trace["artifacts"] if a["artifactType"] == "SOURCE"]
    assert len(sources) == 1 and sources[0]["snapshotSha256"] == digest, events[-15:]
    assert "SECURITY_REPORT" not in {a["artifactType"] for a in trace["artifacts"]}
    assert "SECURITY_MODEL_VALIDATION_STOP" in events, events[-15:]
    blocked = [row["detail"]["llmValidation"] for row in trace["telemetry"]
               if row["binding"].get("eventType") == "LLM_POLICY_REJECTED"
               and row["detail"].get("llmValidation", {}).get("reasonCode") == "SOURCE_CREDENTIAL_DENIED"]
    assert [(r["toolName"], r["sourceRule"]) for r in blocked] == [("write_test_file", "CREDENTIAL_ASSIGNMENT")] * 2
    assert not list((directory / "state/workspaces").glob("*/outputs/security/tests/test_security.py"))

    with sqlite3.connect(f"file:{directory}/state/host.sqlite3?mode=ro", uri=True) as db:
        units = [(json.loads(m), json.loads(r)["tests"]) for m, r in db.execute(
            "SELECT metadata_json,report FROM unit_test_execution_records WHERE run_id=? ORDER BY rowid", (str(run_id),))]
    assert all(m["executionManifest"]["snapshotSha256"] == digest for m, _ in units)
    protected = [(m, rows) for m, rows in units if m["scope"]["kind"] == "PROTECTED"]
    assert sorted(m["role"] for m, _ in protected) == (["QA", "SECURITY"] if ver1 else [])
    generated_qa = [case for m, cases in units if m["scope"]["kind"] == "QA_TESTS" and m["role"] == "QA"
                    for case in cases if case["testId"].endswith(".test_duplicate_canonical_email")]
    assert len(generated_qa) == 2 and generated_qa[0].get("details") == "NOT_RUN_HELPER_ASSERTION"
    assert generated_qa[1]["outcome"] == "PASS"

    receiver = "SINGLE_AGENT" if single else "DEVELOPER"
    messages = read_json(exported / "a2a-messages.json")["roles"][receiver]["receipts"]
    fixes = [(row["messageId"], part["data"]["fixRequest"]) for row in messages
             for part in row["request"]["message"]["parts"] if "fixRequest" in part.get("data", {})]
    req = next(c["requirementId"] for c in cases if c["testId"].endswith("test_duplicate_canonical_email"))
    assert any(p.developer_fix_received for p in providers) == ver1, events[-15:]
    assert len(fixes) == int(ver1), events[-15:]
    summary = {"condition": mode, "replayRunId": str(run_id), "snapshotSha256": digest, "receiver": receiver,
               "blockedSecurityWrites": len(blocked), "protectedReceipts": [m["executionManifestId"] for m, _ in protected],
               "protectedRoles": sorted(m["role"] for m, _ in protected), "generatedQAHelperCase": ["NOT_RUN_HELPER_ASSERTION", "PASS"],
               "fixMessageIds": [m for m, _ in fixes], "req003FailureFeedback": [], "modelTransport": "SCRIPTED_OFFLINE"}
    if ver1:
        security, = [(m, rows) for m, rows in protected if m["role"] == "SECURITY"]
        meta, rows = security
        measured, = [r for r in rows if ".test_sec_signup_003_" in r["testId"]]
        _, fix = fixes[0]
        issue, = [i for i in fix["issues"] if req in i["requirementIds"]]
        assert issue["protectedReceiptId"] == meta["executionManifestId"] and not issue["reportArtifactId"]
        feedback, = [i for i in fix["validationFeedback"] if i["requirementId"] == req and i["role"] == "SECURITY"
                     and i["evidenceOrigin"] == "HOST_PROTECTED_RECEIPT"]
        assert feedback["protectedReceiptId"] == meta["executionManifestId"] and not feedback["reportArtifactId"]
        assert feedback["testId"].endswith(":" + measured["testId"])
        assert feedback["outcome"] == measured["outcome"] == "FAIL"
        observed = [item for item in measured["observations"] if item["status"] == "FAIL"]
        by_key = {(i["caseId"], i["itemId"]): i["actual"] for i in observed}
        delivered = {(i["caseId"], i["itemId"]): i["actual"] for i in feedback["observations"] if i["status"] == "FAIL"}
        assert by_key == delivered and delivered[("variant_pad", "http_status")] == 400
        assert feedback["executionManifest"]["snapshotSha256"] == digest
        visible, = [row for p in providers for row in p.model_feedback if row.get("testId") == feedback["testId"]]
        assert visible["hostDecision"] == "FIX_TARGET"
        assert visible["protectedReceiptId"] == meta["executionManifestId"]
        assert [(r["caseId"], r["itemId"], r["actual"]) for r in visible["observations"] if r["status"] == "FAIL"] == [
            (c, i, value) for (c, i), value in delivered.items()]
        summary["modelInputDecision"] = visible["hostDecision"]
        summary["req003FailureFeedback"] = [{"case": c, "item": i, "actual": v} for (c, i), v in delivered.items()]
    report = read_json(exported / "final-report.json")
    assert report["verdict"] == ("FAIL" if ver1 else "UNVERIFIED"), report["verdict"]
    summary["hostVerdict"] = report["verdict"]
    summary["scopeChecks"] = [{"role": m["role"], "passed": sum(t["outcome"] == "PASS" for t in rows),
                               "failed": sum(t["outcome"] == "FAIL" for t in rows),
                               "cases": len(rows)} for m, rows in protected]
    write_json(directory / "handoff-result.json", summary)
