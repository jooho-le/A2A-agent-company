"""Clarifications are data, never a replacement for a frozen Run contract."""

import json
from collections.abc import Mapping

from orchestrator.core.security import redact_data


_PROTECTED = frozenset({
    "workspaceId", "scenario", "scenarioContract", "runConfiguration", "plan",
    "sourceArtifact", "outputContract", "metadata", "runId", "workflowStepId",
    "scenarioId", "requirementIds", "projectArtifactIds", "projectArtifactId",
    "artifactVersion", "model", "limits", "codeVersion", "fixRequest", "snapshot",
    "testSelectors", "testTargets", "qaRequirements", "userRequest", "protectedCases",
    "runtimeInstructions", "securityPolicy", "emailPolicy", "executionManifest",
    "sourceAccess", "taskId", "contextId", "a2aTaskId", "agentContextId", "role",
    "request", "configuration", "fixAttempt", "fix_attempt", "fixIssues", "fix_issues",
    "previousSource", "previous_source", "previousArtifacts", "previous_artifacts",
    "startingCommitHash", "parentCommitHash", "scannerProfiles", "securityRequirements",
    "sourcePaths", "measuredSecurity",
})


def validate_control_input(value):
    if value is None:
        return None
    try:
        if not isinstance(value, Mapping) or not value or set(value) & _PROTECTED:
            raise ValueError
        data = dict(value)
        if redact_data(data) != data:
            raise ValueError
        return json.loads(json.dumps(data, allow_nan=False, ensure_ascii=False))
    except (TypeError, ValueError, RecursionError):
        raise ValueError("CONTROL_INPUT_INVALID: use clarification data without credentials or frozen contract fields") from None
