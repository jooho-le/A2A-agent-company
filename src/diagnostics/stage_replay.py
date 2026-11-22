"""Security-only diagnostic replay; prepare is offline, run requires explicit approval.

Keep the original A2A input and immutable Source lineage in a new private
database. Never resume the measured Run, dispatch other roles, or publish a
measurement export. Generated code is executed only by the existing MCP/Docker
services after live approval. No credentials are loaded during preparation.
"""
import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from time import monotonic
from urllib.parse import quote
from uuid import UUID, uuid4

from a2a.helpers import new_task
from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.types import TaskState
from google.protobuf.json_format import MessageToDict

from agents.api.validation import parse_project_request, parse_workflow_metadata
from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.llm.contracts import parse_json
from agents.platform.host import _load_protected_case_file
from agents.platform.telemetry import RuntimeTelemetry
from agents.platform.telemetry_store import AgentTelemetryStore
from agents.platform.tool_configuration import _read_regular, load_tool_configuration
from agents.roles.qa_contract import QACaseBinding
from agents.runtime.security import SecurityAgentExecutor, _prepare_input
from agents.runtime.security_context import SQLiteSecurityContextLoader
from agents.runtime.security_services import SecurityRuntimeServices
from agents.runtime.evaluation_policy_store import security_policy_sha256
from agents.semantic_review import ProductSemanticReviewer
from agents.semantic_store import SemanticReviewStore
from mcp_tools.client import MCPChildConfiguration, open_mcp_client
from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.security_config import decode_security_configuration, encode_security_configuration
from mcp_tools.tools.unit_config import decode_unit_configuration, encode_unit_configuration
from mcp_tools.tools.snapshots import FrozenSourceSelection
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.api.schemas.runs import RunStatusResponse
from orchestrator.artifacts.sqlite_store import SQLiteArtifactContentStore
from orchestrator.core.security import redact_data, register_secret_values
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus, A2ATaskState
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.workspaces.registry import WorkspaceRegistry


ROOT = Path(__file__).resolve().parents[2]
MAX_INPUT = 32 * 1024 * 1024
PURPOSE = "DIAGNOSTIC_SECURITY_STAGE_ONLY_NOT_FOR_EVALUATION"
# Copy inputs/lineage, never old usage, model outputs, leases or tool receipts.
TABLES = ("workflow_runs", "workflow_steps", "run_configurations", "workspaces",
          "agent_contexts", "project_artifacts", "artifact_contents", "snapshot_read_grants")


class ReplayError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _read_json(path):
    return parse_json(_read_regular(Path(path), MAX_INPUT).decode(), max_bytes=MAX_INPUT)


def _write(path, value):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def _readonly(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ReplayError("REPLAY_DATABASE_INVALID")
    connection = sqlite3.connect(f"file:{quote(str(path.resolve()))}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def _code_hashes():
    return {str(path.relative_to(ROOT)): sha256(path.read_bytes()).hexdigest()
            for path in sorted((ROOT / "src").rglob("*.py"))}


def policy_from_files(tools_file, cases_file=None):
    tools = load_tool_configuration(tools_file)
    unit = tools.unit_configuration_for(AgentRole.SECURITY)
    selectors = set() if unit is None else {s.name for s in unit.scopes if s.kind == "PROTECTED"}
    cases, roles = _load_protected_case_file(cases_file)
    if selectors and (roles is None or any(role != "SECURITY" for case, role in zip(cases, roles)
            if case.tool_name == "run_unit_tests" and case.selector in selectors)):
        raise ReplayError("REPLAY_PROTECTED_CASES_INVALID")
    selected = [asdict(case) for case in cases if case.tool_name == "run_unit_tests" and case.selector in selectors]
    for case in selected:
        case["requirement_id"] = str(case["requirement_id"])
    return {"security": encode_security_configuration(tools.security_configuration),
            "unit": None if unit is None else encode_unit_configuration(unit),
            "protectedCases": selected, "maxCallSeconds": tools.max_call_seconds}


def restore_captured_policy(evidence_dir, policy):
    """Recover shared protected files from the selected Source's measured captures.

    This is only a candidate reconstruction. prepare_replay must still match
    the *entire* original Security policy hash, including uncaptured files,
    scanner rules, runners and bindings. Never guess missing test bytes.
    """
    evidence_dir = Path(evidence_dir)
    bundle = _read_json(evidence_dir / "export/bundle.json")
    run_id, version = bundle["run"]["runId"], bundle["run"]["codeVersion"]
    reference = bundle["configuration"]["configuration"]["protectedTestSuiteRef"]
    files = {}
    with _readonly(evidence_dir / "orchestrator.sqlite3") as connection:
        rows = connection.execute("""SELECT f.path,f.content,f.content_sha256 FROM qa_test_input_files f
            JOIN qa_test_input_captures c ON c.capture_id=f.capture_id
            JOIN project_artifacts a ON a.artifact_id=c.source_artifact_id
            WHERE c.run_id=? AND a.artifact_type='SOURCE' AND a.artifact_version=?""", (run_id, version))
        for path, content, digest in rows:
            if not path.startswith("tests/"):
                continue
            if sha256(content).hexdigest() != digest:
                raise ReplayError("REPLAY_CAPTURE_HASH_MISMATCH")
            text = content.decode("utf-8")
            if path in files and files[path] != text:
                raise ReplayError("REPLAY_CAPTURE_CONFLICT")
            files[path] = text
    result = json.loads(_json(policy))
    if result["unit"] is not None:
        unit = json.loads(result["unit"])
        for scope in unit["scopes"]:
            if scope["kind"] == "PROTECTED":
                scope["protected_suite_ref"] = reference
                scope["protected_files"] = {path: files.get(path, content)
                                           for path, content in scope["protected_files"].items()}
        result["unit"] = encode_unit_configuration(decode_unit_configuration(_json(unit)))
    return result


def _context(request, task_id, context_id):
    params = parse_project_request(request)
    task = new_task(task_id=task_id, context_id=context_id, state=TaskState.TASK_STATE_SUBMITTED,
                    history=[params.message])
    task.metadata.update(request["metadata"])
    return RequestContext(call_context=ServerCallContext(), request=params,
                          task_id=task_id, context_id=context_id, task=task)


def _copy_inputs(source_db, target_db, workspace_root, *, run_id, step_id, role=AgentRole.SECURITY,
                 project_row=None):
    """Fresh schema and fixed table names; no SQL or filesystem paths from exports."""
    repository = SQLiteWorkflowRepository(target_db)
    with _readonly(source_db) as source, repository._transaction() as target:
        SQLiteArtifactContentStore._ensure_schema(target)
        runs = source.execute("SELECT run_id FROM workflow_runs").fetchall()
        if len(runs) != 1 or runs[0][0] != run_id:
            raise ReplayError("REPLAY_RUN_SCOPE_INVALID")
        if role not in (AgentRole.SECURITY, AgentRole.DEVELOPER):
            raise ReplayError('REPLAY_ROLE_INVALID')
        tables = TABLES + (('issue_records', 'issue_events') if role is AgentRole.DEVELOPER else ())
        for table in tables:
            columns = [row[1] for row in target.execute(f'PRAGMA table_info("{table}")')]
            source_columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
            if columns != source_columns:
                # An initial Developer failure may precede the first frozen
                # source/read-grant store. It has no records to replay.
                if role is AgentRole.DEVELOPER and not source_columns and table in ('artifact_contents', 'snapshot_read_grants'):
                    continue
                raise ReplayError("REPLAY_DATABASE_SCHEMA_MISMATCH_" + table.upper())
            rows = source.execute(f'SELECT * FROM "{table}"').fetchall()
            if len(rows) > 10000:
                raise ReplayError("REPLAY_INPUT_TOO_LARGE")
            for raw in rows:
                row = dict(raw)
                if project_row is not None:
                    row = project_row(table, row)
                    if row is None:
                        continue
                if table == "workflow_runs":
                    run = WorkflowRun.model_validate_json(row["payload_json"])
                    run = WorkflowRun.model_validate({**run.model_dump(),
                        "status": ((WorkflowStatus.IMPLEMENTING if run.fix_attempt == 0 else WorkflowStatus.FIXING)
                                   if role is AgentRole.DEVELOPER else
                                   (WorkflowStatus.VALIDATING if run.fix_attempt == 0 else WorkflowStatus.REVALIDATING)),
                        "resume_state": None, "verdict": None, "termination_reason": None})
                    row.update(status=run.status.value, payload_json=run.model_dump_json())
                elif table == "workflow_steps" and row["workflow_step_id"] == step_id:
                    step = WorkflowStep.model_validate_json(row["payload_json"])
                    if step.agent_role is not role:
                        raise ReplayError('REPLAY_ROLE_MISMATCH')
                    if step.output_artifact_ids or step.a2a_artifact_ids:
                        raise ReplayError("REPLAY_EXPECTS_UNPUBLISHED_SECURITY_STEP")
                    step = WorkflowStep.model_validate({**step.model_dump(),
                        "status": WorkflowStepStatus.RUNNING, "a2a_task_state": A2ATaskState.SUBMITTED})
                    row.update(status=step.status.value, payload_json=step.model_dump_json())
                elif table == "workspaces":
                    workspace = WorkspaceRecord.model_validate_json(row["payload_json"])
                    workspace = WorkspaceRecord.model_validate({**workspace.model_dump(),
                        "root_path": str(workspace_root / str(workspace.workspace_id))})
                    row["payload_json"] = workspace.model_dump_json()
                marks = ",".join("?" for _ in columns)
                target.execute(f'INSERT INTO "{table}" VALUES({marks})', [row[c] for c in columns])
    os.chmod(target_db, 0o600)
    return repository


def _services(repository, registry, artifacts, execution, policy, *, client_factory=open_mcp_client):
    cases = tuple(QACaseBinding(**{**c, "requirement_id": UUID(c["requirement_id"])})
                  for c in policy["protectedCases"])
    configuration = MCPChildConfiguration(
        binding=MCPBinding(role=AgentRole.SECURITY, agent_role=AgentRole.SECURITY,
            run_id=execution.metadata.run_id, workspace_id=execution.configuration.workspace_id),
        database_path=repository.database_path, workspace_root=registry.base_path,
        frozen_source=FrozenSourceSelection(project_artifact_id=execution.source.artifact_id,
                                            snapshot_sha256=execution.source.snapshot_sha256),
        security_scan_configuration=decode_security_configuration(policy["security"]),
        unit_test_configuration=None if policy["unit"] is None else decode_unit_configuration(policy["unit"]),
        max_call_seconds=policy["maxCallSeconds"])
    return SecurityRuntimeServices(repository, registry, artifacts, mcp_configuration=configuration,
                                    protected_cases=cases, client_factory=client_factory)


def _assemble(directory, manifest, *, client_factory=open_mcp_client):
    repository = SQLiteWorkflowRepository(directory / "diagnostic.sqlite3")
    registry = WorkspaceRegistry(repository, directory / "workspaces")
    registry.provision(manifest["workspaceId"], run_id=manifest["sourceRunId"])
    artifacts = ArtifactStore(repository, registry)
    budget = ExecutionBudget(runtime_budget_ms=manifest["runtimeBudgetMs"],
                             limits=LLMLimits.model_validate(manifest["llmLimits"]))
    context = _context(_read_json(directory / "request.json"), manifest["sourceTaskId"], manifest["sourceContextId"])
    loader = SQLiteSecurityContextLoader(repository, lambda _: budget)
    execution = loader(context)
    _prepare_input(context, execution)  # Check exact original A2A input, not an approximation.
    services = _services(repository, registry, artifacts, execution,
                         _read_json(directory / "policy.json"), client_factory=client_factory)
    return repository, context, execution, services


async def prepare_replay(evidence_dir, *, policy, limits, runtime_budget_ms, step_id=None):
    """Read original evidence only. Creates an inert, one-use private capsule."""
    evidence_dir = Path(evidence_dir).resolve(strict=True)
    bundle = _read_json(evidence_dir / "export/bundle.json")
    a2a = _read_json(evidence_dir / "export/a2a-messages.json")
    run_id = bundle["run"]["runId"]
    if a2a["runId"] != run_id or bundle.get("snapshotConsistent") is not True:
        raise ReplayError("REPLAY_EXPORT_INCONSISTENT")
    with _readonly(evidence_dir / "orchestrator.sqlite3") as source:
        rows = source.execute("SELECT payload_json FROM workflow_steps WHERE run_id=?", (run_id,)).fetchall()
        candidates = [WorkflowStep.model_validate_json(row[0]) for row in rows]
        candidates = [s for s in candidates if s.agent_role is AgentRole.SECURITY
                      and (step_id is None or str(s.workflow_step_id) == step_id)]
        if len(candidates) != 1:
            raise ReplayError("REPLAY_SELECT_EXACTLY_ONE_SECURITY_STEP")
        step = candidates[0]
        original_run = source.execute("SELECT payload_json FROM workflow_runs WHERE run_id=?", (run_id,)).fetchone()
        if original_run is None or RunStatusResponse.from_run(WorkflowRun.model_validate_json(original_run[0])) != RunStatusResponse.model_validate(bundle["run"]):
            raise ReplayError("REPLAY_RUN_EXPORT_MISMATCH")
        original_policy = source.execute("SELECT policy_sha256 FROM run_evaluation_policies WHERE run_id=? AND role='SECURITY'",
                                         (run_id,)).fetchone()
    role = a2a["roles"]["SECURITY"]
    receipts = [r for r in role["receipts"] if r["taskId"] == step.a2a_task_id]
    tasks = [t for t in role["tasks"] if t["taskId"] == step.a2a_task_id]
    if len(receipts) != 1 or len(tasks) != 1:
        raise ReplayError("REPLAY_REQUIRES_ONE_INITIAL_REQUEST")
    receipt, task = receipts[0], tasks[0]
    request = receipt["request"]
    metadata = parse_workflow_metadata(request["metadata"])
    if (receipt["metadata"] != request["metadata"] or task["metadata"] != request["metadata"]
            or metadata.run_id != step.run_id or metadata.workflow_step_id != step.workflow_step_id
            or metadata.code_version != step.code_version or receipt["contextId"] != step.agent_context_id
            or task["contextId"] != step.agent_context_id or bundle["run"]["codeVersion"] != step.code_version):
        raise ReplayError("REPLAY_INPUT_BINDING_MISMATCH")
    limits = LLMLimits.model_validate(limits.model_dump())
    if limits.max_total_tokens is None or type(runtime_budget_ms) is not int or runtime_budget_ms <= 0:
        raise ReplayError("REPLAY_BOUNDED_LIMITS_REQUIRED")
    directory = Path(tempfile.mkdtemp(prefix="capstone-security-replay-" )).resolve()
    manifest = {"schemaVersion": 1, "purpose": PURPOSE, "replayId": str(uuid4()), "status": "AWAITING_APPROVAL",
        "maxExecutions": 1, "sourceRunId": run_id, "sourceStepId": str(step.workflow_step_id),
        "sourceTaskId": step.a2a_task_id, "sourceContextId": step.agent_context_id,
        "workspaceId": bundle["run"]["workspaceId"], "codeVersion": step.code_version,
        "runtimeBudgetMs": runtime_budget_ms, "llmLimits": limits.model_dump(),
        "sourceEvidenceDirectory": str(evidence_dir), "codeHashes": _code_hashes(),
        "originalSecurityPolicySha256": None if original_policy is None else original_policy[0],
        "measurementEligible": False, "overallVerdict": None}
    try:
        _copy_inputs(evidence_dir / "orchestrator.sqlite3", directory / "diagnostic.sqlite3", directory / "workspaces",
                     run_id=run_id, step_id=str(step.workflow_step_id))
        _write(directory / "request.json", request)
        _write(directory / "policy.json", policy)
        repository, context, execution, services = _assemble(directory, manifest)
        if execution.configuration.configuration.architecture != "MULTI_AGENT":
            raise ReplayError("REPLAY_MULTI_SECURITY_ONLY")
        await services.prepare(execution)  # File/schema/hash/policy checks only; no scan/model/MCP startup.
        record = services._artifacts.read_registered_source(execution.metadata.run_id, execution.source.artifact_id)
        exported = _read_regular(evidence_dir / f"export/v{step.code_version}/source.tar", MAX_INPUT)
        if exported != record.content:
            raise ReplayError("REPLAY_SNAPSHOT_EXPORT_MISMATCH")
        manifest.update(snapshotSha256=execution.source.snapshot_sha256, sourceArtifactId=str(execution.source.artifact_id),
            model=execution.model.model_dump(mode="json", by_alias=True),
            replaySecurityPolicySha256=security_policy_sha256(services.configuration, services._protected_cases))
        if manifest["originalSecurityPolicySha256"] != manifest["replaySecurityPolicySha256"]:
            raise ReplayError("REPLAY_SECURITY_POLICY_CHANGED")
        # Flush WAL before pinning the prepared database. Original DB is never opened writable.
        with sqlite3.connect(directory / "diagnostic.sqlite3") as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        manifest["inputHashes"] = {name: sha256((directory / name).read_bytes()).hexdigest()
                                   for name in ("request.json", "policy.json", "diagnostic.sqlite3")}
        # Approval binds limits, original input and current code, not just a reusable UUID.
        manifest["approvalSha256"] = sha256(_json(manifest).encode()).hexdigest()
        _write(directory / "manifest.json", manifest)
        _write(directory / "offline-check.json", {"status": "PASS", "apiCalls": 0, "dockerExecutions": 0,
            "sourceAndA2AValidated": True, "securityPolicyMatchesOriginal": True, "purpose": PURPOSE})
        return directory
    except BaseException:
        _write(directory / "preparation-failed.json", {"status": "FAILED", "apiCalls": 0})
        raise


def verify_prepared(directory):
    directory = Path(directory).resolve(strict=True)
    # Only tool-created temp capsules can be executed; never operate in the repo.
    temp_roots = {Path(tempfile.gettempdir()).resolve(), Path("/private/tmp").resolve(), Path("/tmp").resolve()}
    if directory.parent not in temp_roots or not directory.name.startswith("capstone-security-replay-"):
        raise ReplayError("REPLAY_PRIVATE_TEMP_REQUIRED")
    manifest = _read_json(directory / "manifest.json")
    if (manifest.get("purpose") != PURPOSE or manifest.get("status") != "AWAITING_APPROVAL"
            or manifest.get("measurementEligible") is not False or manifest.get("maxExecutions") != 1):
        raise ReplayError("REPLAY_MANIFEST_INVALID")
    if manifest.get("approvalSha256") != sha256(_json({k: v for k, v in manifest.items()
                                                      if k != "approvalSha256"}).encode()).hexdigest():
        raise ReplayError("REPLAY_APPROVAL_PLAN_CHANGED")
    if manifest["codeHashes"] != _code_hashes():
        raise ReplayError("REPLAY_CODE_CHANGED_PREPARE_AGAIN")
    if set(manifest["inputHashes"]) != {"request.json", "policy.json", "diagnostic.sqlite3"}:
        raise ReplayError("REPLAY_INPUT_HASHES_INVALID")
    for name, digest in manifest["inputHashes"].items():
        if sha256(_read_regular(directory / name, MAX_INPUT)).hexdigest() != digest:
            raise ReplayError("REPLAY_PREPARED_INPUT_CHANGED")
    return manifest


class _DiagnosticQueue(EventQueue):
    def __init__(self, directory):
        self.directory, self.events = directory, []

    async def enqueue_event(self, event):
        data = redact_data(MessageToDict(event))
        self.events.append(data)
        _write(self.directory / f"event-{len(self.events):04d}.json", data)


def _all_records(reader, run_id):
    rows, total = reader(run_id, limit=500)
    while len(rows) < total:
        batch, observed_total = reader(run_id, limit=500, offset=len(rows))
        if not batch or observed_total != total:
            raise ReplayError("REPLAY_TELEMETRY_INCOMPLETE")
        rows.extend(batch)
    return rows, total


async def execute_replay(directory, *, approval_id, provider_factory, client_factory=open_mcp_client):
    """One approved Security execution. Factories remain inert until all guards pass."""
    directory = Path(directory).resolve(strict=True)
    manifest = verify_prepared(directory)
    if not approval_id or approval_id != manifest["approvalSha256"]:
        raise ReplayError("REPLAY_EXPLICIT_APPROVAL_REQUIRED")
    try:
        _write(directory / "execution-claimed.json", {"replayId": manifest["replayId"],
                                                      "approvalSha256": approval_id, "maxExecutions": 1})
    except FileExistsError:
        raise ReplayError("REPLAY_ALREADY_ATTEMPTED_PREPARE_AGAIN") from None
    attempt = directory / "attempt"
    attempt.mkdir(mode=0o700)
    _copy_inputs(directory / "diagnostic.sqlite3", attempt / "diagnostic.sqlite3", attempt / "workspaces",
                 run_id=manifest["sourceRunId"], step_id=manifest["sourceStepId"])
    for name in ("request.json", "policy.json"):
        _write(attempt / name, _read_json(directory / name))
    repository, context, execution, services = _assemble(attempt, manifest, client_factory=client_factory)
    store = AgentTelemetryStore(repository.database_path)
    telemetry = RuntimeTelemetry(repository, store)
    queue = _DiagnosticQueue(attempt)
    previous_raw = os.environ.get("A2A_DIAGNOSTIC_RAW_DIR")
    os.environ["A2A_DIAGNOSTIC_RAW_DIR"] = str(attempt / "raw")
    provider, error = None, None
    started = monotonic()
    try:
        # No original Host runner/dispatcher is constructed. Only this executor can run.
        provider = provider_factory()
        executor = SecurityAgentExecutor(provider=provider, context_factory=lambda _: execution,
            services_factory=lambda _: services, telemetry_factory=telemetry.bind,
            semantic_reviewer=ProductSemanticReviewer(provider, SemanticReviewStore(repository.database_path)))
        await asyncio.wait_for(executor.execute(context, queue), manifest["runtimeBudgetMs"] / 1000)
    except asyncio.CancelledError:
        error = "REPLAY_CANCELED"
        raise
    except TimeoutError:
        error = "REPLAY_TIME_LIMIT"
    except ReplayError as failure:
        error = failure.code
    except Exception:
        error = "REPLAY_EXECUTION_FAILED"
    finally:
        if provider is not None:
            try:
                await provider.aclose()
            except Exception:
                error = error or "REPLAY_PROVIDER_CLOSE_FAILED"
        if previous_raw is None:
            os.environ.pop("A2A_DIAGNOSTIC_RAW_DIR", None)
        else:
            os.environ["A2A_DIAGNOSTIC_RAW_DIR"] = previous_raw
        usage, total = _all_records(store.list_usage, execution.metadata.run_id)
        events, event_total = _all_records(store.list_events, execution.metadata.run_id)
        _write(attempt / "usage.json", {"records": usage, "total": total})
        _write(attempt / "telemetry.json", {"events": events, "total": event_total})
        _write(attempt / "artifacts.json", {"purpose": PURPOSE,
                                            "artifacts": [e["artifact"] for e in queue.events if "artifact" in e]})
        statuses = [e["status"] for e in queue.events if "status" in e]
        final_status = statuses[-1] if statuses else None
        result = {"purpose": PURPOSE, "replayId": manifest["replayId"], "sourceRunId": manifest["sourceRunId"],
            "sourceStepId": manifest["sourceStepId"], "snapshotSha256": manifest["snapshotSha256"],
            "codeVersion": manifest["codeVersion"], "role": "SECURITY", "error": error, "taskStatus": final_status,
            "modelCalls": execution.budget.model_calls, "toolCalls": execution.budget.tool_calls,
            "knownTotalTokens": execution.budget.known_total_tokens, "totalTokens": execution.budget.total_tokens,
            "usageComplete": execution.budget.total_tokens is not None,
            "elapsedMs": int((monotonic() - started) * 1000), "measurementEligible": False, "overallVerdict": None,
            "sourceCodeUnchanged": _code_hashes() == manifest["codeHashes"]}
        result.update(costUsd=None, costComplete=False)  # No price book was admitted for this diagnostic.
        _write(attempt / "result.json", result)
    return result


def _live_provider():
    from pydantic import SecretStr
    from agents.llm.openai_responses import OpenAIResponsesProvider
    key = os.environ.get("A2A_PLATFORM_LLM_API_KEY", "")
    if not key or "=" in key or any(c.isspace() for c in key):
        raise ReplayError("REPLAY_API_KEY_REQUIRED")
    register_secret_values(key)
    return OpenAIResponsesProvider(SecretStr(key))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare", help="Offline only: validate original input and create a private capsule")
    prepare.add_argument("--evidence-dir", required=True, type=Path)
    prepare.add_argument("--tools-file", required=True, type=Path)
    prepare.add_argument("--protected-cases-file", type=Path)
    prepare.add_argument("--restore-captured-protected-inputs", action="store_true",
                         help="Reconstruct old shared test bytes; accept only an exact original policy hash")
    prepare.add_argument("--step-id")
    prepare.add_argument("--max-model-calls", type=int, default=50)
    prepare.add_argument("--max-tool-calls", type=int, default=100)
    prepare.add_argument("--max-total-tokens", type=int, default=500000)
    prepare.add_argument("--runtime-seconds", type=int, default=900)
    run = commands.add_parser("run", help="Paid API and Docker execution: explicit user approval required")
    run.add_argument("--prepared", required=True, type=Path)
    run.add_argument("--approval-id", required=True, help="Prepared approvalSha256, only after explicit user approval")
    options = parser.parse_args(argv)
    try:
        if options.command == "prepare":
            # Preserve the recorded per-response output cap; P3 is not changed here.
            runtime = _read_json(options.evidence_dir / "runtime.json")
            limits = LLMLimits.model_validate({**runtime["llmLimits"], "max_model_calls": options.max_model_calls,
                "max_tool_calls": options.max_tool_calls, "max_total_tokens": options.max_total_tokens})
            policy = policy_from_files(options.tools_file, options.protected_cases_file)
            if options.restore_captured_protected_inputs:
                policy = restore_captured_policy(options.evidence_dir, policy)
            directory = asyncio.run(prepare_replay(options.evidence_dir,
                policy=policy, limits=limits,
                runtime_budget_ms=options.runtime_seconds * 1000, step_id=options.step_id))
            print(json.dumps({"status": "AWAITING_APPROVAL", "prepared": str(directory), "apiCalls": 0}))
        else:
            result = asyncio.run(execute_replay(options.prepared, approval_id=options.approval_id,
                                                provider_factory=_live_provider))
            status = None if result["taskStatus"] is None else result["taskStatus"].get("state")
            print(json.dumps({**{k: result[k] for k in ("replayId", "role", "error", "modelCalls", "knownTotalTokens")},
                              "taskState": status}))
            return 0 if result["error"] is None and status == "TASK_STATE_COMPLETED" else 2
    except ReplayError as error:
        print(error.code, file=sys.stderr)
        return 1
    except Exception:
        print("REPLAY_PREPARATION_OR_EXECUTION_FAILED", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
