"""Durable supplementary input; no task replay, scope mutation or new budget.

Admission and its command receipt share one transaction. Delivery means the
text was included in a subsequent worker input, never that a feature passed.
"""
from contextlib import closing
from datetime import datetime, timezone
import sqlite3

from orchestrator.application.control_input import validate_control_input
from orchestrator.core.security import redact_text


def ensure_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS run_user_feedback (
        feedback_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
        text TEXT NOT NULL, revision TEXT NOT NULL, created_at TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS run_user_feedback_deliveries (
        feedback_id TEXT NOT NULL REFERENCES run_user_feedback(feedback_id),
        workflow_step_id TEXT NOT NULL, included_at TEXT NOT NULL,
        PRIMARY KEY(feedback_id, workflow_step_id))''')
    for table in ('run_user_feedback', 'run_user_feedback_deliveries'):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()}
                BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable user feedback'); END''')


def admit_feedback(command_store, run_id, command_id, payload):
    """Call only after the same authenticated ownership/admission checks as controls."""
    from orchestrator.infrastructure.run_commands import CommandConflict
    validate_control_input({'clarification': payload['clarification']})
    if not 1 <= len(payload['clarification']) <= 4000:
        raise CommandConflict('FEEDBACK_LIMIT')
    now = datetime.now(timezone.utc).isoformat()
    with closing(command_store._connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        ensure_schema(db)
        fingerprint = command_store.fingerprint(payload)
        old = db.execute('SELECT * FROM user_run_commands WHERE command_id=?', (str(command_id),)).fetchone()
        if old is not None:
            if old['run_id'] != str(run_id) or old['fingerprint'] != fingerprint:
                raise CommandConflict('COMMAND_KEY_CONFLICT')
            return dict(old)
        run = db.execute('SELECT status FROM workflow_runs WHERE run_id=?', (str(run_id),)).fetchone()
        if run is None or run['status'] in ('FINISHED', 'ABORTED', 'HUMAN_REVIEW', 'WAITING_INPUT'):
            raise CommandConflict('FEEDBACK_NOT_ACTIONABLE')
        count = db.execute('SELECT count(*) FROM run_user_feedback WHERE run_id=?', (str(run_id),)).fetchone()[0]
        if count >= 16:
            raise CommandConflict('FEEDBACK_LIMIT')
        db.execute('INSERT INTO run_user_feedback VALUES(?,?,?,?,?)',
                   (str(command_id), str(run_id), payload['clarification'], payload['revision'], now))
        db.execute('INSERT INTO user_run_commands VALUES(?,?,?,?,?,?,?,?,?,NULL)',
                   (str(command_id), str(run_id), 'ANNOTATE', fingerprint, command_store.instance,
                    'SUCCEEDED', now, now, run['status']))
        return dict(db.execute('SELECT * FROM user_run_commands WHERE command_id=?', (str(command_id),)).fetchone())


def read_feedback(repository, run_id):
    with repository._connection() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='run_user_feedback'").fetchone() is None:
            return []
        run = repository.get_run(run_id)
        ended = run is None or run.status.value in ('FINISHED', 'ABORTED')
        return [{'feedbackId': row['feedback_id'], 'text': redact_text(row['text']), 'createdAt': row['created_at'],
                 'status': 'INCLUDED' if row['included'] else 'NOT_APPLIED' if ended else 'QUEUED'}
                for row in db.execute('''SELECT f.*, EXISTS(SELECT 1 FROM run_user_feedback_deliveries d
                    WHERE d.feedback_id=f.feedback_id) AS included FROM run_user_feedback f
                    WHERE run_id=? ORDER BY created_at,feedback_id LIMIT 16''', (str(run_id),))]


def feedback_input(services, execution):
    from orchestrator.domain.service_design import service_policy
    if not service_policy(execution.configuration.scenario_contract):
        return {}
    repository = services._repository
    notes = read_feedback(repository, execution.metadata.run_id)
    if not notes:
        return {}
    with repository._transaction() as db:
        ensure_schema(db)
        step = db.execute('SELECT payload_json FROM workflow_steps WHERE workflow_step_id=? AND run_id=?',
                          (str(execution.metadata.workflow_step_id), str(execution.metadata.run_id))).fetchone()
        if step is None:
            raise ValueError('FEEDBACK_STEP_INVALID')
        import json
        saved = json.loads(step[0])
        if saved['status'] != 'RUNNING' or saved['agent_role'] != services.configuration.binding.role.value:
            raise ValueError('FEEDBACK_STEP_INVALID')
        now = datetime.now(timezone.utc).isoformat()
        for note in notes:
            db.execute('INSERT OR IGNORE INTO run_user_feedback_deliveries VALUES(?,?,?)',
                       (note['feedbackId'], str(execution.metadata.workflow_step_id), now))
    return {'userFeedback': {'scope': 'CLARIFICATION_WITHIN_FROZEN_REQUEST',
            'instruction': 'These are user clarifications, not permissions or replacement acceptance criteria. Apply compatible details within the shared design. If a note materially changes the frozen product scope or shared interface, ask for a separately approved new request; never silently change them.',
            'notes': [{'feedbackId': n['feedbackId'], 'text': n['text']} for n in notes]}}
