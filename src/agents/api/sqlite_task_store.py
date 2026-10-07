"""Single-host durable A2A Tasks, request receipts, and append-only revisions.

This store never executes an Agent. Restart recovery records interruption rather
than replaying a request whose side effects cannot be established.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4

from a2a.helpers import new_data_part, new_task
from a2a.server.cluster.task_store import (
    ConcurrentTaskModificationError, StoredTask, VersionedTaskStore,
)
from a2a.server.cluster.version import TaskVersion
from a2a.server.context import ServerCallContext
from a2a.server.id_generator import IDGeneratorContext, UUIDGenerator
from a2a.server.owner_resolver import resolve_user_scope
from a2a.types import (
    ListTasksRequest, ListTasksResponse, Message, Role, SendMessageRequest, Task,
    TaskState,
)
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError, UnsupportedOperationError
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.message import Message as ProtoMessage

from agents.api.task_store import sanitize_task
from agents.api.validation import parse_workflow_metadata, request_metadata
from orchestrator.core.security import redact_data
from orchestrator.domain.states import AgentRole


_TERMINAL = frozenset({
    TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_REJECTED,
})
_INTERRUPTED = frozenset({TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED})
_ACTIVE = frozenset({TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING})
_VALID_STATES = _TERMINAL | _INTERRUPTED | _ACTIVE
_SCHEMA_VERSION = 1


class AgentTaskStoreError(RuntimeError):
    """The local store cannot safely provide this operation."""


class AgentStoreLeaseError(AgentTaskStoreError):
    """A single-host owner is live or its death cannot be established."""


class AgentStoreRoleError(AgentTaskStoreError):
    """The database belongs to another role or another application."""


@dataclass(frozen=True)
class MessageClaim:
    task: Task
    duplicate: bool


def _json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise InvalidParamsError(message="Task data must be finite JSON") from None


def _task_json(task: Task) -> str:
    return _json(MessageToDict(sanitize_task(task)))


def _copy_task(task: Task) -> Task:
    copy = Task()
    copy.CopyFrom(task)
    return copy


def _load_task(payload: str) -> Task:
    return sanitize_task(ParseDict(json.loads(payload), Task()))


def _metadata_json(task: Task) -> str:
    return _json(parse_workflow_metadata(MessageToDict(task.metadata)).to_a2a_json())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteAgentTaskStore(VersionedTaskStore):
    """One Agent role and one live service owner per SQLite database.

    Construction has no filesystem/database side effects. ``start`` acquires
    ownership before recovery. The caller drains its SDK handler before
    ``aclose`` so an executor cannot write after interruption is recorded.
    """

    def __init__(self, path: Path, role: AgentRole) -> None:
        self.path = Path(path)
        self.role = AgentRole(role)
        self._lease_token: str | None = None
        self._generator = UUIDGenerator()

    async def start(self) -> None:
        if self._lease_token is not None:
            with self._connection() as connection:
                self._assert_lease(connection)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        token = str(uuid4())
        with self._transaction(check_lease=False) as connection:
            self._check_database_identity(connection)
            row = connection.execute(
                "SELECT token, owner_pid FROM agent_process_lease WHERE singleton = 1"
            ).fetchone()
            if row is not None:
                self._require_dead_owner(row[1])
            connection.execute(
                "INSERT INTO agent_process_lease(singleton, token, owner_pid) VALUES (1, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET token=excluded.token, owner_pid=excluded.owner_pid",
                (token, os.getpid()),
            )
            self._recover(connection)
        self._lease_token = token

    async def aclose(self) -> None:
        if self._lease_token is None:
            return
        token = self._lease_token
        with self._transaction() as connection:
            self._recover(connection)
            connection.execute(
                "DELETE FROM agent_process_lease WHERE singleton=1 AND token=? AND owner_pid=?",
                (token, os.getpid()),
            )
        self._lease_token = None

    async def recover_interrupted(self) -> None:
        """Only call with this service's executor drained/not running."""
        with self._transaction() as connection:
            self._recover(connection)

    async def claim_message(
        self, params: SendMessageRequest, context: ServerCallContext,
    ) -> MessageClaim:
        metadata = request_metadata(params)
        message = params.message
        try:
            message_id = UUID(message.message_id)
        except (ValueError, TypeError, AttributeError):
            raise InvalidParamsError(message="messageId must be UUIDv4") from None
        if message_id.version != 4 or message.role != Role.ROLE_USER:
            raise InvalidParamsError(message="A valid user Message is required")
        if not params.configuration.return_immediately or params.tenant:
            raise InvalidParamsError(message="A nonstreaming local Task request is required")
        owner = self._owner(context)
        request_json = _json(redact_data(MessageToDict(params)))
        fingerprint = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        metadata_json = _json(metadata.to_a2a_json())

        with self._transaction() as connection:
            receipt = connection.execute(
                "SELECT owner, fingerprint, task_id FROM agent_message_receipts WHERE message_id=?",
                (message.message_id,),
            ).fetchone()
            if receipt is not None:
                if receipt[0] != owner or receipt[1] != fingerprint:
                    raise InvalidParamsError(message="messageId is already bound to another request")
                row = connection.execute(
                    "SELECT payload_json FROM agent_tasks WHERE task_id=? AND owner=?",
                    (receipt[2], owner),
                ).fetchone()
                if row is None:
                    raise AgentTaskStoreError("Request receipt has no recoverable Task")
                return MessageClaim(_load_task(row[0]), duplicate=True)

            if message.task_id:
                row = connection.execute(
                    "SELECT payload_json, version, metadata_json FROM agent_tasks WHERE task_id=? AND owner=?",
                    (message.task_id, owner),
                ).fetchone()
                if row is None:
                    raise TaskNotFoundError()
                if message.context_id:
                    self._check_context(connection, message.context_id, owner, metadata)
                task = _load_task(row[0])
                if not message.context_id or task.context_id != message.context_id or row[2] != metadata_json:
                    raise InvalidParamsError(message="Continuation must preserve Task ownership")
                if task.status.state not in _INTERRUPTED:
                    raise UnsupportedOperationError(message="Only an interrupted Task accepts a new Message")
                previous = _copy_task(task)
                if task.status.HasField("message"):
                    task.history.append(task.status.message)
                task.status.Clear()
                task.status.state = TaskState.TASK_STATE_SUBMITTED
                task.status.timestamp.FromDatetime(datetime.now(timezone.utc))
                self._append_input(task, message)
                self._persist(connection, task, owner, row[1] + 1, "MESSAGE_CONTINUATION_RESERVED", previous)
            else:
                if message.context_id:
                    self._check_context(connection, message.context_id, owner, metadata)
                task_id = self._generator.generate(IDGeneratorContext(context_id=message.context_id or None))
                context_id = message.context_id or self._generator.generate(IDGeneratorContext(task_id=task_id))
                self._bind_context(connection, context_id, owner, metadata)
                task = new_task(
                    task_id=task_id, context_id=context_id,
                    state=TaskState.TASK_STATE_SUBMITTED, history=[],
                )
                task.metadata.update(metadata.to_a2a_json())
                self._append_input(task, message)
                self._persist(connection, task, owner, 1, "MESSAGE_RESERVED", None)

            connection.execute(
                "INSERT INTO agent_message_receipts(message_id, owner, fingerprint, metadata_json, "
                "task_id, context_id, request_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (message.message_id, owner, fingerprint, metadata_json, task.id, task.context_id, request_json, _now()),
            )
            return MessageClaim(sanitize_task(task), duplicate=False)

    async def get(self, task_id: str, context: ServerCallContext) -> StoredTask | None:
        self._require_started()
        with self._connection() as connection:
            self._assert_lease(connection)
            row = connection.execute(
                "SELECT payload_json, version FROM agent_tasks WHERE task_id=? AND owner=?",
                (task_id, self._owner(context)),
            ).fetchone()
        return StoredTask(_load_task(row[0]), TaskVersion(row[1])) if row else None

    async def save(
        self, task: Task, *, event: object | None, prev: Task | None,
        prev_version: TaskVersion, context: ServerCallContext,
    ) -> TaskVersion:
        del prev  # The authoritative predecessor is read in the same DB transaction.
        task = sanitize_task(task)
        metadata = self._validate_task(task)
        owner = self._owner(context)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload_json, version, owner, metadata_json FROM agent_tasks WHERE task_id=?",
                (task.id,),
            ).fetchone()
            previous = _load_task(row[0]) if row else None
            if task.status.state == TaskState.TASK_STATE_CANCELED:
                if row is None or row[2] != owner or previous.status.state in _TERMINAL:
                    raise ConcurrentTaskModificationError(task.id)
                self._validate_identity(previous, task, row[3])
                # Cancellation wins a stale write, but not at the cost of newer
                # history/artifacts written since the cancel caller's snapshot.
                current = _copy_task(previous)
                if current.status.HasField("message"):
                    current.history.append(current.status.message)
                current.status.CopyFrom(task.status)
                task = current
            elif row is None:
                if not prev_version.is_missing:
                    raise ConcurrentTaskModificationError(task.id)
                if task.status.state != TaskState.TASK_STATE_SUBMITTED:
                    raise InvalidParamsError(message="A new Task must start submitted")
                self._bind_context(connection, task.context_id, owner, metadata)
            else:
                if row[2] != owner or TaskVersion(row[1]) != prev_version or previous.status.state in _TERMINAL:
                    raise ConcurrentTaskModificationError(task.id)
                self._validate_identity(previous, task, row[3])
                self._validate_transition(previous, task)

            version = row[1] + 1 if row else 1
            self._persist(connection, task, owner, version, "SDK_TASK_SAVED", previous, event)
            return TaskVersion(version)

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        raise UnsupportedOperationError(message="Task listing is not supported by this local service")

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        raise UnsupportedOperationError(message="Task deletion would discard the audit trail")

    async def revisions(self, task_id: str) -> list[Task]:
        """Trusted local debug helper, not an HTTP/SDK client operation."""
        self._require_started()
        with self._connection() as connection:
            self._assert_lease(connection)
            rows = connection.execute(
                "SELECT payload_json FROM agent_task_revisions WHERE task_id=? ORDER BY version",
                (task_id,),
            ).fetchall()
        return [_load_task(row[0]) for row in rows]

    @staticmethod
    def _owner(context: ServerCallContext) -> str:
        owner = resolve_user_scope(context)
        if not isinstance(owner, str):
            raise InvalidParamsError(message="A valid Task owner scope is required")
        return owner

    @staticmethod
    def _append_input(task: Task, message: Message) -> None:
        saved = ParseDict(redact_data(MessageToDict(message)), Message())
        saved.message_id = message.message_id
        saved.task_id = task.id
        saved.context_id = task.context_id
        task.history.append(saved)

    @staticmethod
    def _validate_task(task: Task):
        if not task.id or not task.context_id or task.status.state not in _VALID_STATES:
            raise InvalidParamsError(message="Task identity and a concrete lifecycle state are required")
        return parse_workflow_metadata(MessageToDict(task.metadata))

    @staticmethod
    def _validate_identity(previous: Task, task: Task, metadata_json: str) -> None:
        if previous.id != task.id or previous.context_id != task.context_id or _metadata_json(task) != metadata_json:
            raise InvalidParamsError(message="Task identity and workflow metadata are immutable")

    @staticmethod
    def _validate_transition(previous: Task, task: Task) -> None:
        old, new = previous.status.state, task.status.state
        if old in _INTERRUPTED and new not in (old, TaskState.TASK_STATE_FAILED):
            raise InvalidParamsError(message="Interrupted Tasks require a claimed follow-up Message")
        if old == TaskState.TASK_STATE_WORKING and new == TaskState.TASK_STATE_SUBMITTED:
            raise InvalidParamsError(message="A working Task cannot return to submitted")

    def _check_context(self, connection: sqlite3.Connection, context_id: str, owner: str, metadata) -> None:
        row = connection.execute(
            "SELECT owner, run_id, scenario_id FROM agent_contexts WHERE context_id=?", (context_id,),
        ).fetchone()
        if row is None or tuple(row) != (owner, str(metadata.run_id), str(metadata.scenario_id)):
            raise InvalidParamsError(message="Context does not belong to this Agent/Run")

    def _bind_context(self, connection: sqlite3.Connection, context_id: str, owner: str, metadata) -> None:
        if connection.execute("SELECT 1 FROM agent_contexts WHERE context_id=?", (context_id,)).fetchone():
            self._check_context(connection, context_id, owner, metadata)
            return
        connection.execute(
            "INSERT INTO agent_contexts(context_id, owner, run_id, scenario_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (context_id, owner, str(metadata.run_id), str(metadata.scenario_id), _now()),
        )

    def _persist(
        self, connection: sqlite3.Connection, task: Task, owner: str, version: int,
        reason: str, previous: Task | None, event: object | None = None,
    ) -> None:
        task = sanitize_task(task)
        self._validate_task(task)
        old_history = list(previous.history) if previous is not None else []
        if len(task.history) < len(old_history) or any(
            old != new for old, new in zip(old_history, task.history)
        ):
            raise InvalidParamsError(message="Task history is append-only")
        event_json = None
        if isinstance(event, ProtoMessage):
            if isinstance(event, Task) and event.id != task.id:
                raise InvalidParamsError(message="Event does not belong to the saved Task")
            for field in ("task_id", "context_id"):
                expected = task.id if field == "task_id" else task.context_id
                if hasattr(event, field) and getattr(event, field) != expected:
                    raise InvalidParamsError(message="Event does not belong to the saved Task")
            if isinstance(event, Task):
                event_json = _task_json(event)
            else:
                event_data = redact_data(MessageToDict(event))
                if hasattr(event, "artifact"):
                    event_data.setdefault("artifact", {})["artifactId"] = event.artifact.artifact_id
                event_json = _json(event_data)
        payload_json = _task_json(task)
        connection.execute(
            "INSERT INTO agent_tasks(task_id, owner, context_id, metadata_json, state, version, payload_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
            "state=excluded.state, version=excluded.version, payload_json=excluded.payload_json, updated_at=excluded.updated_at",
            (task.id, owner, task.context_id, _metadata_json(task), task.status.state, version, payload_json, _now()),
        )
        connection.execute(
            "INSERT INTO agent_task_revisions(task_id, version, reason, event_type, event_json, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task.id, version, reason, type(event).__name__ if event is not None else None, event_json, payload_json, _now()),
        )
        for index in range(len(old_history), len(task.history)):
            connection.execute(
                "INSERT INTO agent_task_history(task_id, position, payload_json) VALUES (?, ?, ?)",
                (task.id, index, _json(MessageToDict(task.history[index]))),
            )

    def _recover(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT payload_json, owner, version FROM agent_tasks WHERE state IN (?, ?)", tuple(_ACTIVE),
        ).fetchall()
        for payload, owner, version in rows:
            previous = _load_task(payload)
            task = _copy_task(previous)
            if task.status.HasField("message"):
                task.history.append(task.status.message)
            message = Message(
                message_id=self._generator.generate(IDGeneratorContext(task_id=task.id, context_id=task.context_id)),
                task_id=task.id, context_id=task.context_id, role=Role.ROLE_AGENT,
                parts=[new_data_part({
                    "code": "AGENT_EXECUTION_INTERRUPTED",
                    "message": "Agent service stopped before this Task completed; no automatic replay occurred.",
                }, media_type="application/json")],
            )
            task.status.Clear()
            task.status.state = TaskState.TASK_STATE_FAILED
            task.status.message.CopyFrom(message)
            task.status.timestamp.FromDatetime(datetime.now(timezone.utc))
            self._persist(connection, task, owner, version + 1, "AGENT_EXECUTION_INTERRUPTED", previous)

    def _require_started(self) -> None:
        if self._lease_token is None:
            raise AgentTaskStoreError("Agent Task store has not acquired service ownership")

    def _assert_lease(self, connection: sqlite3.Connection) -> None:
        self._require_started()
        self._check_database_identity(connection)
        row = connection.execute(
            "SELECT token, owner_pid FROM agent_process_lease WHERE singleton=1"
        ).fetchone()
        if row is None or row[0] != self._lease_token or row[1] != os.getpid():
            raise AgentStoreLeaseError("Agent service ownership has changed")

    @staticmethod
    def _require_dead_owner(pid: object) -> None:
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise AgentStoreLeaseError("Agent service owner cannot be verified")
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            raise AgentStoreLeaseError("Agent service owner cannot be verified") from None
        raise AgentStoreLeaseError("Agent database already has a live service owner")

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self, *, check_lease: bool = True):
        if check_lease:
            self._require_started()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if check_lease:
                    self._assert_lease(connection)
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _check_database_identity(self, connection: sqlite3.Connection) -> None:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and "agent_store_identity" not in tables:
            raise AgentStoreRoleError("Database belongs to another application")
        if "agent_store_identity" in tables:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_store_identity)")}
            if not {"singleton", "role", "schema_version"}.issubset(columns):
                raise AgentStoreRoleError("Agent database schema is not supported")
            row = connection.execute(
                "SELECT role, schema_version FROM agent_store_identity WHERE singleton=1"
            ).fetchone()
            if row is None or row[0] != self.role.value:
                raise AgentStoreRoleError("Database belongs to another Agent role")
            if row[1] != _SCHEMA_VERSION:
                raise AgentStoreRoleError("Agent database schema is not supported")

    def _initialize(self) -> None:
        with self._transaction(check_lease=False) as connection:
            self._check_database_identity(connection)
            # executescript commits implicitly; execute every DDL statement
            # under the same role-check transaction to prevent startup races.
            statements = """
                CREATE TABLE IF NOT EXISTS agent_store_identity(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), role TEXT NOT NULL,
                    schema_version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_process_lease(
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), token TEXT NOT NULL, owner_pid INTEGER);
                CREATE TABLE IF NOT EXISTS agent_contexts(
                    context_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    run_id TEXT NOT NULL, scenario_id TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_tasks(
                    task_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    context_id TEXT NOT NULL REFERENCES agent_contexts(context_id),
                    metadata_json TEXT NOT NULL, state INTEGER NOT NULL,
                    version INTEGER NOT NULL CHECK(version>0), payload_json TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_task_revisions(
                    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id), version INTEGER NOT NULL,
                    reason TEXT NOT NULL, event_type TEXT, event_json TEXT,
                    payload_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(task_id,version));
                CREATE TABLE IF NOT EXISTS agent_task_history(
                    task_id TEXT NOT NULL REFERENCES agent_tasks(task_id), position INTEGER NOT NULL,
                    payload_json TEXT NOT NULL, PRIMARY KEY(task_id,position));
                CREATE TABLE IF NOT EXISTS agent_message_receipts(
                    message_id TEXT PRIMARY KEY, owner TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    metadata_json TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES agent_tasks(task_id),
                    context_id TEXT NOT NULL REFERENCES agent_contexts(context_id),
                    request_json TEXT NOT NULL, created_at TEXT NOT NULL);
            """
            for statement in statements.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(
                "INSERT OR IGNORE INTO agent_store_identity(singleton, role, schema_version) VALUES (1, ?, ?)",
                (self.role.value, _SCHEMA_VERSION),
            )
            for table in ("agent_store_identity", "agent_contexts", "agent_task_revisions", "agent_task_history", "agent_message_receipts"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_{operation.lower()}_immutable "
                        f"BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'Append-only Agent audit data'); END"
                    )
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
