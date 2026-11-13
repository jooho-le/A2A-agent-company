"""Closed project Artifact admission before any role result is consumed.

Validating an envelope proves neither payload correctness nor Tool execution.
The existing role parsers, frozen requirements and receipt checks still apply.
"""

import math
from uuid import UUID

from a2a.types import Task, TaskState
from google.protobuf.json_format import MessageToDict

from orchestrator.domain.role_outputs import ROLE_ARTIFACT_NAMES
from orchestrator.domain.states import AgentRole


class OutputEnvelopeError(ValueError):
    code = "ROLE_ARTIFACT_ENVELOPE_INVALID"

    def __init__(self):
        super().__init__(self.code)


def validate_output_envelope(task, role, *, run_id, workflow_step_id):
    """Accept only the complete role set; never drop an unexpected Artifact.

    Protocol-issued Artifact IDs stay opaque and unchanged. Project IDs are
    UUIDv4 and must be distinct even with different textual UUID spellings.
    No store, schema file, network, model or source access occurs here.
    """
    invalid = False
    try:
        if (type(task) is not Task or type(role) is not AgentRole
                or task.status.state != TaskState.TASK_STATE_COMPLETED
                or any(type(value) is not UUID or value.version != 4 for value in (run_id, workflow_step_id))):
            raise ValueError
        names = ROLE_ARTIFACT_NAMES[role]
        if len(task.artifacts) != len(names):
            raise ValueError
        observed, a2a_ids, project_ids = set(), set(), set()
        for artifact in task.artifacts:
            if (artifact.name not in names or artifact.name in observed
                    or not artifact.artifact_id.strip() or artifact.artifact_id in a2a_ids
                    or len(artifact.parts) != 1):
                raise ValueError
            part = artifact.parts[0]
            if part.WhichOneof("content") != "data" or part.media_type != "application/json":
                raise ValueError
            metadata = MessageToDict(artifact.metadata)
            if (set(metadata) != {"runId", "workflowStepId", "projectArtifactId", "artifactVersion"}
                    or metadata["runId"] != str(run_id) or metadata["workflowStepId"] != str(workflow_step_id)
                    or type(metadata["projectArtifactId"]) is not str):
                raise ValueError
            identifier = UUID(metadata["projectArtifactId"])
            version = metadata["artifactVersion"]
            if (identifier.version != 4 or identifier in project_ids or isinstance(version, bool)
                    or type(version) not in (int, float) or not math.isfinite(version)
                    or version < 1 or not float(version).is_integer()):
                raise ValueError
            observed.add(artifact.name)
            a2a_ids.add(artifact.artifact_id)
            project_ids.add(identifier)
        if observed != set(names):
            raise ValueError
    except Exception:
        invalid = True
    if invalid:
        # Unknown Artifact names, credentials in metadata and SDK exceptions
        # must not be retained in an exception cause or context.
        raise OutputEnvelopeError() from None
