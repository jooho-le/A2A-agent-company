"""Private append-only publication reviews, beside the Host workflow database."""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from uuid import uuid4

from agents.llm.contracts import json_text, parse_json


class SemanticStoreError(ValueError):
    def __init__(self):
        super().__init__('SEMANTIC_REVIEW_STORAGE_UNAVAILABLE')


class SemanticReviewStore:
    def __init__(self, database_path):
        self.path = Path(database_path)
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS agent_semantic_reviews (
                ordinal INTEGER PRIMARY KEY AUTOINCREMENT, review_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL, step_id TEXT NOT NULL, task_id TEXT NOT NULL,
                context_id TEXT NOT NULL, created_at TEXT NOT NULL, payload_json TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS semantic_task_lookup ON agent_semantic_reviews(run_id,step_id,task_id,ordinal)')
            db.execute("CREATE TRIGGER IF NOT EXISTS semantic_review_no_update BEFORE UPDATE ON agent_semantic_reviews BEGIN SELECT RAISE(ABORT,'IMMUTABLE_SEMANTIC_REVIEW'); END")
            db.execute("CREATE TRIGGER IF NOT EXISTS semantic_review_no_delete BEFORE DELETE ON agent_semantic_reviews BEGIN SELECT RAISE(ABORT,'IMMUTABLE_SEMANTIC_REVIEW'); END")

    def append(self, *, binding, candidate, result, status, reason, resume_policy, model_sequences=()):
        try:
            review_id = str(uuid4())
            payload = {'schemaVersion': 1, 'reviewId': review_id, 'binding': binding,
                'candidate': candidate, 'result': result, 'status': status, 'reasonCode': reason,
                'resumePolicy': resume_policy, 'isRequirementApproval': False, 'isProductSuccess': False}
            payload['modelCallSequences'] = list(model_sequences)
            raw = json_text(payload)
            with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                db.execute('''INSERT INTO agent_semantic_reviews
                    (review_id,run_id,step_id,task_id,context_id,created_at,payload_json) VALUES(?,?,?,?,?,?,?)''',
                    (review_id, binding['runId'], binding['workflowStepId'], binding['a2aTaskId'],
                     binding['agentContextId'], datetime.now(timezone.utc).isoformat(), raw))
            return payload
        except Exception:
            pass
        raise SemanticStoreError() from None

    def latest(self, run_id, step_id, task_id):
        try:
            with closing(sqlite3.connect(self.path, timeout=10)) as db:
                row = db.execute('''SELECT payload_json FROM agent_semantic_reviews
                    WHERE run_id=? AND step_id=? AND task_id=? ORDER BY ordinal DESC LIMIT 1''',
                    (str(run_id), str(step_id), task_id)).fetchone()
            return None if row is None else parse_json(row[0])
        except Exception:
            pass
        raise SemanticStoreError() from None
