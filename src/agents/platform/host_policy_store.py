"""Immutable, private Run bindings for the explicitly approved Host policy.

The caller supplies a fingerprint of validated Host configuration, never its
credentials, commands or paths. Construction is inert. Only the first claimed
Planner dispatch may create a binding; an existing binding is never replaced.
"""

from contextlib import closing
import re
import sqlite3
from uuid import RFC_4122, UUID

from agents.platform.composition import PlatformConfigurationError
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository


_TABLE = "run_host_policies"


def _arguments(run_id, policy_sha256):
    if (type(run_id) is not UUID or run_id.version != 4 or run_id.variant != RFC_4122
            or type(policy_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", policy_sha256) is None):
        raise ValueError


class HostPolicyStore:
    """Bind policy to the same private database as the registered Workflow Run."""

    def __init__(self, repository):
        if type(repository) is not SQLiteWorkflowRepository:
            raise PlatformConfigurationError()
        self._repository = repository

    def __repr__(self):
        return "HostPolicyStore()"

    @staticmethod
    def _registered_run(connection, run_id):
        row = connection.execute(
            "SELECT run_id,status,payload_json FROM workflow_runs WHERE run_id=?",
            (str(run_id),),
        ).fetchone()
        if row is None:
            raise ValueError
        run = WorkflowRun.model_validate_json(row[2])
        if row[0] != str(run_id) or run.run_id != run_id or row[1] != run.status.value:
            raise ValueError
        return run

    @staticmethod
    def _existing_policy(connection, run_id):
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,),
        ).fetchone() is None:
            return None
        row = connection.execute(
            "SELECT run_id,policy_sha256 FROM run_host_policies WHERE run_id=?", (str(run_id),),
        ).fetchone()
        if row is None:
            return None
        if (row[0] != str(run_id) or type(row[1]) is not str
                or re.fullmatch(r"[0-9a-f]{64}", row[1]) is None):
            raise ValueError
        return row[1]

    @staticmethod
    def _initial_claim(connection, run):
        if (run.status is not WorkflowStatus.PLANNING or run.fix_attempt != 0
                or run.code_version is not None):
            raise ValueError
        rows = connection.execute(
            "SELECT workflow_step_id,run_id,status,payload_json FROM workflow_steps WHERE run_id=?",
            (str(run.run_id),),
        ).fetchall()
        if len(rows) != 1:
            raise ValueError
        row = rows[0]
        step = WorkflowStep.model_validate_json(row[3])
        if (row[0] != str(step.workflow_step_id) or row[1] != str(run.run_id)
                or row[2] != step.status.value or step.run_id != run.run_id
                or step.agent_role is not AgentRole.PLANNER
                or step.status is not WorkflowStepStatus.RUNNING or step.attempt != 0
                or step.a2a_task_id is not None or step.a2a_task_state is not None
                or step.agent_context_id is not None or step.code_version is not None
                or step.requirement_ids or step.a2a_artifact_ids
                or step.input_artifact_ids or step.output_artifact_ids):
            raise ValueError
        # Initial budget telemetry is allowed; prior Agent context, generated
        # artifacts or Tool activity is not a fresh admission opportunity.
        for table in ("agent_contexts", "project_artifacts", "tool_attempts"):
            if connection.execute(
                f"SELECT 1 FROM {table} WHERE run_id=? LIMIT 1", (str(run.run_id),),
            ).fetchone() is not None:
                raise ValueError
        # A Send may have happened without a remote Task ID ever arriving.
        # Query all events, not the bounded public Trace pagination window.
        if connection.execute(
            "SELECT 1 FROM trace_events WHERE run_id=? AND "
            "(event_type='A2A_MESSAGE_SENT' OR "
            "json_extract(payload_json,'$.eventType')='A2A_MESSAGE_SENT' OR "
            "json_extract(payload_json,'$.event_type')='A2A_MESSAGE_SENT') LIMIT 1",
            (str(run.run_id),),
        ).fetchone() is not None:
            raise ValueError

    @staticmethod
    def _schema(connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS run_host_policies (
            run_id TEXT PRIMARY KEY REFERENCES workflow_runs(run_id),
            policy_sha256 TEXT NOT NULL CHECK (
                typeof(policy_sha256)='text' AND length(policy_sha256)=64
                AND policy_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        )""")
        for operation in ("UPDATE", "DELETE"):
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS run_host_policies_no_{operation.lower()} "
                f"BEFORE {operation} ON run_host_policies "
                "BEGIN SELECT RAISE(ABORT,'immutable Host policy'); END",
            )
        # SQLite REPLACE can bypass DELETE triggers when recursive_triggers
        # is off. Reject its INSERT phase before an old binding is removed.
        connection.execute("""CREATE TRIGGER IF NOT EXISTS run_host_policies_no_replace
            BEFORE INSERT ON run_host_policies
            WHEN EXISTS(SELECT 1 FROM run_host_policies WHERE run_id=NEW.run_id)
            BEGIN SELECT RAISE(ABORT,'immutable Host policy'); END""")

    def pin(self, run_id: UUID, policy_sha256: str) -> None:
        """Atomically insert only at the initial claim, or match the old hash.

        A later preparation failure does not undo this committed binding.
        Repeated matches are valid after continuation or process restart.
        """
        try:
            _arguments(run_id, policy_sha256)
            with self._repository._transaction() as connection:
                run = self._registered_run(connection, run_id)
                existing = self._existing_policy(connection, run_id)
                if existing is not None:
                    if existing != policy_sha256:
                        raise ValueError
                    return
                self._initial_claim(connection, run)
                self._schema(connection)
                connection.execute(
                    "INSERT INTO run_host_policies(run_id,policy_sha256) VALUES(?,?)",
                    (str(run_id), policy_sha256),
                )
        except Exception:
            raise PlatformConfigurationError() from None

    def require(self, run_id: UUID, policy_sha256: str) -> None:
        """Read an existing binding, without creating a DB, table or row."""
        try:
            _arguments(run_id, policy_sha256)
            # Repository._connection uses SQLite's creating open mode. A
            # service validation must not recreate a missing private DB.
            uri = self._repository.database_path.absolute().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=10.0)) as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                self._registered_run(connection, run_id)
                if self._existing_policy(connection, run_id) != policy_sha256:
                    raise ValueError
        except Exception:
            raise PlatformConfigurationError() from None
