"""Durable user-command receipts. Admission and replay never repeat execution."""
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import json
import sqlite3
from uuid import uuid4


class CommandConflict(ValueError):
    pass


class RunCommandStore:
    def __init__(self, database_path):
        self.path = database_path
        self.instance = str(uuid4())
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS user_run_commands (
                command_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                action TEXT NOT NULL, fingerprint TEXT NOT NULL, owner_instance TEXT NOT NULL,
                state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                result_status TEXT, error_code TEXT)''')
            db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS uq_active_user_command
                ON user_run_commands(run_id,action) WHERE state IN ('QUEUED','RUNNING','UNKNOWN')''')

    @staticmethod
    def fingerprint(body):
        return sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def get(self, run_id, command_id):
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT * FROM user_run_commands WHERE command_id=? AND run_id=?",
                             (str(command_id), str(run_id))).fetchone()
            if row is None:
                return None
            if row["state"] in ("QUEUED", "RUNNING") and row["owner_instance"] != self.instance:
                db.execute("UPDATE user_run_commands SET state='UNKNOWN',error_code='HOST_RESTARTED',updated_at=? WHERE command_id=?",
                           (datetime.now(timezone.utc).isoformat(), str(command_id)))
                row = db.execute("SELECT * FROM user_run_commands WHERE command_id=?", (str(command_id),)).fetchone()
            return dict(row)

    def admit(self, run_id, command_id, body):
        fingerprint = self.fingerprint(body)
        now = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM user_run_commands WHERE command_id=?", (str(command_id),)).fetchone()
            if old is not None:
                if old["run_id"] != str(run_id) or old["fingerprint"] != fingerprint:
                    raise CommandConflict("COMMAND_KEY_CONFLICT")
                return dict(old), False
            try:
                db.execute("INSERT INTO user_run_commands VALUES(?,?,?,?,?,?,?,?,NULL,NULL)",
                           (str(command_id), str(run_id), body["action"], fingerprint, self.instance,
                            "QUEUED", now, now,))
            except sqlite3.IntegrityError:
                raise CommandConflict("COMMAND_ALREADY_PENDING") from None
            return dict(db.execute("SELECT * FROM user_run_commands WHERE command_id=?", (str(command_id),)).fetchone()), True

    def claim(self, command_id):
        with closing(self._connect()) as db, db:
            result = db.execute("UPDATE user_run_commands SET state='RUNNING',updated_at=? WHERE command_id=? AND state='QUEUED' AND owner_instance=?",
                                (datetime.now(timezone.utc).isoformat(), str(command_id), self.instance))
            return result.rowcount == 1

    def finish(self, command_id, state, *, result_status=None, error_code=None):
        with closing(self._connect()) as db, db:
            db.execute("UPDATE user_run_commands SET state=?,updated_at=?,result_status=?,error_code=? WHERE command_id=? AND owner_instance=?",
                       (state, datetime.now(timezone.utc).isoformat(), result_status, error_code, str(command_id), self.instance))

    @staticmethod
    def public(row):
        return {"commandId": row["command_id"], "runId": row["run_id"], "action": row["action"],
                "status": row["state"], "resultStatus": row["result_status"], "errorCode": row["error_code"],
                "createdAt": row["created_at"], "updatedAt": row["updated_at"]}
