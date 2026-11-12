"""Project role Artifact sets; these are not restrictions of A2A itself."""

from types import MappingProxyType

from orchestrator.domain.states import AgentRole


ROLE_OUTPUT_ENVELOPE_VERSION = 1
ROLE_ARTIFACT_NAMES = MappingProxyType({
    AgentRole.PLANNER: ("requirements.json",),
    AgentRole.DEVELOPER: ("source-snapshot.json", "change-report.json", "build-report.json"),
    AgentRole.QA: ("qa-report.json",),
    AgentRole.SECURITY: ("security-report.json",),
})
