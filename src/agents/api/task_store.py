"""Temporary memory-only Task storage with redaction at save/response boundaries."""

from a2a.server.context import ServerCallContext
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import Task
from google.protobuf.json_format import MessageToDict, ParseDict

from orchestrator.core.security import redact_data


def sanitize_task(task: Task) -> Task:
    """Redact content in a copy, preserving Agent-owned opaque routing IDs."""
    sanitized = ParseDict(redact_data(MessageToDict(task)), Task())
    sanitized.id = task.id
    sanitized.context_id = task.context_id
    for source, target in zip(task.artifacts, sanitized.artifacts):
        target.artifact_id = source.artifact_id
    return sanitized


class RedactingInMemoryTaskStore(InMemoryTaskStore):
    """SDK copying semantics; contents disappear when this process exits."""

    async def save(self, task: Task, context: ServerCallContext) -> None:
        await super().save(sanitize_task(task), context)
