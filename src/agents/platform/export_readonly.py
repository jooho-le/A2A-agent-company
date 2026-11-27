"""Read-only, transaction-pinned Host export views; no schema installation."""

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from urllib.parse import quote

from agents.platform.pricing_store import RunPricingLedger
from agents.platform.telemetry_store import AgentTelemetryStore
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository


class ReadOnlyWorkflowRepository(SQLiteWorkflowRepository):
    def __init__(self, path):
        self._database_path = Path(path).resolve(strict=True)
        self._pinned = None

    @contextmanager
    def _connection(self):
        if self._pinned is not None:
            yield self._pinned
            return
        connection = sqlite3.connect(f"file:{quote(str(self._database_path))}?mode=ro", uri=True, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def snapshot(self):
        if self._pinned is not None:
            raise ValueError("EXPORT_SNAPSHOT_ALREADY_OPEN")
        with self._connection() as connection:
            self._pinned = connection
            try:
                yield self
            finally:
                self._pinned = None

    @contextmanager
    def _transaction(self):
        raise ValueError("EXPORT_IS_READ_ONLY")
        yield  # pragma: no cover


class ReadOnlyTelemetryStore(AgentTelemetryStore):
    def __init__(self, repository):
        self._repository = repository
        self._database_path = repository.database_path
        self._clock = lambda: datetime.now(timezone.utc)
        self._price_book = None
        self._pricing = RunPricingLedger()

    @contextmanager
    def _connection(self):
        with self._repository._connection() as connection:
            yield connection

    @contextmanager
    def _transaction(self):
        raise ValueError("EXPORT_IS_READ_ONLY")
        yield  # pragma: no cover
