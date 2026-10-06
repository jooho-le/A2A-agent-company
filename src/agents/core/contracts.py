"""Reuse the official A2A SDK constants and existing project role identifiers."""

from types import MappingProxyType

from a2a.utils.constants import (
    A2A_JSON_MEDIA_TYPE,
    PROTOCOL_VERSION_1_0,
    TransportProtocol,
)

from orchestrator.domain.states import AgentRole


A2A_PROTOCOL_VERSION = PROTOCOL_VERSION_1_0
A2A_PROTOCOL_BINDING = TransportProtocol.HTTP_JSON.value
A2A_MEDIA_TYPE = A2A_JSON_MEDIA_TYPE

AGENT_PORTS = MappingProxyType({
    AgentRole.PLANNER: 8101,
    AgentRole.DEVELOPER: 8102,
    AgentRole.QA: 8103,
    AgentRole.SECURITY: 8104,
})
