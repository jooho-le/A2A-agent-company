"""Trusted operator export for independent evaluation; no scoring/execution.

Snapshot paths and bundle/trace payloads preserve dev's evaluator contracts.
Single A2A uses its real one-store schema v2, never four fabricated Agents.
"""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sys
from uuid import UUID

from agents.platform.a2a_export import export_a2a_messages, A2AExportError
from agents.platform.export_readonly import ReadOnlyWorkflowRepository
from agents.platform.run_records import collect_records, metrics_from_records, RunRecordsError
from agents.platform.snapshot_export import export_snapshots, SnapshotExportError
from orchestrator.domain.workspaces import WorkspaceRecord


class EvaluationExportError(ValueError):
    def __init__(self, code="EVALUATION_EXPORT_FAILED"):
        self.code = code
        super().__init__(code)


def _json(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def _coverage(repository, run, a2a):
    single = a2a.get("architecture") == "SINGLE_AGENT"
    missing, mismatched, expected = [], [], set()
    steps = repository.list_steps(run.run_id)
    for step in steps:
        if not step.a2a_task_id:
            continue
        identity = "SINGLE_AGENT" if single else step.agent_role.value
        expected.add((identity, step.a2a_task_id))
        rows = a2a["roles"].get(identity, {})
        task = next((item for item in rows.get("tasks", []) if item["taskId"] == step.a2a_task_id), None)
        if task is None:
            missing.append(step.a2a_task_id)
        elif (task["contextId"] != step.agent_context_id
              or task["metadata"].get("workflowStepId") != str(step.workflow_step_id)
              or task["metadata"].get("scenarioId") != str(run.scenario_id)
              or task["metadata"].get("attempt") != step.attempt
              or task["metadata"].get("codeVersion") != step.code_version):
            mismatched.append(step.a2a_task_id)
    unbound = [item["taskId"] for identity, rows in a2a["roles"].items() for item in rows["tasks"]
               if (identity, item["taskId"]) not in expected]
    return {"missingStores": a2a["missingRoles"], "missingTaskIds": missing, "mismatchedTaskIds": mismatched,
            "unboundTaskIds": unbound,
            "a2aComplete": not (a2a["missingRoles"] or missing or mismatched or unbound)}


def collect_evaluation_export(repository, run_id, *, stores, all_versions=True):
    """Collect exact bytes under a pinned Host read transaction; returns files.

    Agent stores have their own read transactions. Cross-store lag/missing
    records are exposed as incomplete coverage, never silently passed.
    """
    try:
        parsed = UUID(str(run_id))
        if parsed.version != 4:
            raise EvaluationExportError("RUN_ID_INVALID")
        view = ReadOnlyWorkflowRepository(repository.database_path)
        with view.snapshot():
            bundle, trace = collect_records(view, parsed)
            run = view.get_run(parsed)
            mode = bundle["configuration"]["configuration"]["architecture"]
            a2a = export_a2a_messages(stores, parsed, architecture=mode)
            metrics = metrics_from_records(bundle, trace)
            registered = [item for item in view.list_project_artifacts(parsed) if item.artifact_type == "SOURCE"]
            snapshots = export_snapshots(view, parsed, all_versions=all_versions) if registered else ()
            # No-source runs retain their real outcome; no tar/manifest is invented.
            snapshot_run = {"runId": str(parsed), "status": run.status.value,
                "verdict": None if run.verdict is None else run.verdict.value, "finalCodeVersion": run.code_version,
                "exportedCodeVersions": [item.code_version for item in snapshots]}
            files = {"run.json": _json(snapshot_run), "bundle.json": _json(bundle), "trace.json": _json(trace),
                     "a2a-messages.json": _json(a2a), "metrics.json": _json(metrics),
                     "run-configuration.json": _json(bundle["configuration"])}
            for item in snapshots:
                files[f"v{item.code_version}/source.tar"] = item.archive
                files[f"v{item.code_version}/manifest.json"] = _json(item.manifest)
            steps = view.list_steps(parsed)
            phase = {"PLANNER": "PLANNING", "DEVELOPER": "IMPLEMENTING", "QA": "QA", "SECURITY": "SECURITY"}
            with view._connection() as connection:
                workspace_roots = tuple(Path(WorkspaceRecord.model_validate_json(row[0]).root_path).resolve()
                    for row in connection.execute("SELECT payload_json FROM workspaces"))
            coverage = _coverage(view, run, a2a)
            final_available = any(item.code_version == run.code_version for item in snapshots)
            handoff = {"schemaVersion": 1, "runId": str(parsed), "architecture": mode,
                "runStatus": run.status.value, "orchestratorVerdict": snapshot_run["verdict"],
                "sourceStatus": "EXPORTED" if final_available else ("FINAL_SNAPSHOT_NOT_FOUND" if snapshots else "SNAPSHOT_NOT_FOUND"),
                "sourceAvailable": bool(snapshots), "finalSourceAvailable": final_available, "isTerminal": metrics["isTerminal"],
                "readyForFinalEvaluation": final_available and metrics["isTerminal"] and coverage["a2aComplete"],
                "a2aSchemaVersion": a2a.get("schemaVersion", 1),
                "consistency": {"host": "PINNED_SQLITE_TRANSACTION", "agentStores": "PER_STORE_READ_TRANSACTION",
                    "crossDatabaseAtomic": False},
                "coverage": coverage,
                "steps": [{"workflowStepId": str(step.workflow_step_id), "agentIdentity": "SINGLE_AGENT" if mode == "SINGLE_AGENT" else step.agent_role.value,
                    "phase": "FIXING" if step.agent_role.value == "DEVELOPER" and (step.code_version or 0) > 1 else phase[step.agent_role.value],
                    "a2aTaskId": step.a2a_task_id, "agentContextId": step.agent_context_id,
                    "attempt": step.attempt, "codeVersion": step.code_version, "status": step.status.value} for step in steps],
                "consumerRequirements": ["D4_A2A_READER_MUST_ACCEPT_SINGLE_AGENT_SCHEMA_V2"] if mode == "SINGLE_AGENT" else [],
                "files": {name: {"sha256": sha256(data).hexdigest(), "sizeBytes": len(data)} for name, data in files.items()}}
            files["handoff.json"] = _json(handoff)
            return files, workspace_roots
    except EvaluationExportError:
        raise
    except (A2AExportError, SnapshotExportError, RunRecordsError) as error:
        raise EvaluationExportError(error.code) from None
    except Exception:
        raise EvaluationExportError() from None


def write_evaluation_export(repository, run_id, out, *, stores, all_versions=True):
    files, workspace_roots = collect_evaluation_export(repository, run_id, stores=stores, all_versions=all_versions)
    out = Path(out).resolve()
    if any(out == root or root in out.parents for root in workspace_roots):
        raise EvaluationExportError("EXPORT_INSIDE_AGENT_WORKSPACE")
    created = False
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.mkdir(mode=0o700)
        created = True
        for name, data in files.items():
            path = out / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
        return {name: out / name for name in files}
    except FileExistsError:
        if created:
            shutil.rmtree(out)
        raise EvaluationExportError("EXPORT_EXISTS") from None
    except Exception:
        if created:
            shutil.rmtree(out)
        raise EvaluationExportError("EXPORT_WRITE_FAILED") from None


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m agents.platform.evaluation_export")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--store", action="append", default=[], metavar="IDENTITY=PATH")
    parser.add_argument("--final-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        stores = {}
        for item in args.store:
            identity, separator, path = item.partition("=")
            if not separator or not path or identity in stores:
                raise EvaluationExportError("STORE_ARGUMENT_INVALID")
            stores[identity] = path
        write_evaluation_export(ReadOnlyWorkflowRepository(args.database), args.run_id, args.out,
            stores=stores, all_versions=not args.final_only)
    except EvaluationExportError as error:
        sys.stderr.write(error.code + "\n")
        return 1
    except Exception:
        sys.stderr.write("EVALUATION_EXPORT_FAILED\n")
        return 1
    print("evaluation export completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
