"""Host-side read-only export of one Run's A2A message bodies for evaluation.

The Orchestrator records that a message was sent, not its body. Each Agent's
durable task store keeps the received requests, task history, final Task
(status messages and Artifacts) and update events. This trusted operator tool
reads those rows for one Run from each role's store, read-only, so the
independent evaluation can search the A2A outputs named by REQ-006.

It never opens a store for writing, never takes the Agent process lease, and
sends nothing anywhere. Stored rows are already redacted by the Agent store.

    python -m agents.platform.a2a_export --run-id UUID --out FILE --store QA=PATH [--store ROLE=PATH ...]
"""

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import quote
from uuid import UUID

from orchestrator.domain.states import AgentRole


class A2AExportError(ValueError):
    """Stable code only; no paths, message bodies or underlying exception."""

    def __init__(self, code="A2A_EXPORT_FAILED"):
        super().__init__(code)
        self.code = code


def _rows(connection, query, arguments):
    return [dict(row) for row in connection.execute(query, arguments).fetchall()]


def _json(value):
    return None if value is None else json.loads(value)


def _read_store(path: Path, role: AgentRole, run_id: str) -> dict:
    try:
        # Encode the path so '?', '#' or '%' in a directory name cannot change the URI.
        connection = sqlite3.connect(f"file:{quote(str(path.resolve(strict=True)))}?mode=ro", uri=True, timeout=5)
    except (OSError, sqlite3.Error):
        raise A2AExportError("STORE_UNAVAILABLE") from None
    try:
        connection.row_factory = sqlite3.Row
        identity = connection.execute("SELECT role FROM agent_store_identity WHERE singleton=1").fetchone()
        if identity is None or identity["role"] != role.value:
            raise A2AExportError("STORE_ROLE_MISMATCH")
        contexts = [row["context_id"] for row in connection.execute(
            "SELECT context_id FROM agent_contexts WHERE run_id=? ORDER BY created_at, context_id", (run_id,))]
        marks = ",".join("?" * len(contexts))
        if not contexts:
            return {"contexts": [], "tasks": [], "history": [], "receipts": [], "events": []}
        tasks = _rows(connection, f"SELECT task_id, context_id, state, version, metadata_json, payload_json "
                                  f"FROM agent_tasks WHERE context_id IN ({marks}) ORDER BY task_id", contexts)
        task_ids = [task["task_id"] for task in tasks]
        task_marks = ",".join("?" * len(task_ids)) or "NULL"
        history = _rows(connection, f"SELECT task_id, position, payload_json FROM agent_task_history "
                                    f"WHERE task_id IN ({task_marks}) ORDER BY task_id, position", task_ids)
        events = _rows(connection, f"SELECT task_id, version, reason, event_type, event_json FROM agent_task_revisions "
                                   f"WHERE task_id IN ({task_marks}) AND event_json IS NOT NULL ORDER BY task_id, version",
                       task_ids)
        receipts = _rows(connection, f"SELECT message_id, task_id, context_id, metadata_json, request_json "
                                     f"FROM agent_message_receipts WHERE context_id IN ({marks}) ORDER BY created_at, message_id",
                         contexts)
    except A2AExportError:
        raise
    except (sqlite3.Error, ValueError):
        raise A2AExportError("STORE_READ_FAILED") from None
    finally:
        connection.close()
    return {
        "contexts": contexts,
        "tasks": [{"taskId": row["task_id"], "contextId": row["context_id"], "state": row["state"],
                   "version": row["version"], "metadata": _json(row["metadata_json"]), "task": _json(row["payload_json"])}
                  for row in tasks],
        "history": [{"taskId": row["task_id"], "position": row["position"], "message": _json(row["payload_json"])}
                    for row in history],
        "receipts": [{"messageId": row["message_id"], "taskId": row["task_id"], "contextId": row["context_id"],
                      "metadata": _json(row["metadata_json"]), "request": _json(row["request_json"])} for row in receipts],
        "events": [{"taskId": row["task_id"], "version": row["version"], "reason": row["reason"],
                    "eventType": row["event_type"], "event": _json(row["event_json"])} for row in events],
    }


def export_a2a_messages(stores, run_id) -> dict:
    """Return {'runId', 'roles': {ROLE: rows}, 'missingRoles': [...]} for the four Agent roles.

    A role whose store was not given is listed in missingRoles so the evaluator
    reports the A2A check as incomplete instead of silently passing it.
    """
    try:
        run_id = str(UUID(str(run_id)))
    except ValueError:
        raise A2AExportError("RUN_ID_INVALID") from None
    roles = {}
    for role, path in dict(stores).items():
        role = role if isinstance(role, AgentRole) else AgentRole(str(role))
        roles[role.value] = _read_store(Path(path), role, run_id)
    return {"runId": run_id, "roles": roles,
            "missingRoles": sorted(role.value for role in AgentRole if role.value not in roles)}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m agents.platform.a2a_export",
                                     description="Export one Run's A2A message bodies (read-only) for evaluation.")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--store", action="append", default=[], metavar="ROLE=PATH",
                        help="Agent task store database per role (PLANNER, DEVELOPER, QA, SECURITY)")
    parser.add_argument("--force", action="store_true")
    options = parser.parse_args(argv)
    try:
        stores = {}
        for item in options.store:
            role, separator, path = item.partition("=")
            if not separator or role not in AgentRole.__members__ or role in stores:
                raise A2AExportError("STORE_ARGUMENT_INVALID")
            stores[role] = path
        if options.out.exists() and not options.force:
            raise A2AExportError("EXPORT_EXISTS")
        document = export_a2a_messages(stores, options.run_id)
        options.out.parent.mkdir(parents=True, exist_ok=True)
        options.out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except A2AExportError as error:
        sys.stderr.write(error.code + "\n")
        return 1
    print(f"a2a messages: {options.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
