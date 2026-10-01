"""A2A Protocol 1.0 client and project request contracts."""

from orchestrator.a2a.client import (
    A2AAgentClient,
    AgentCardContractError,
    validate_agent_card,
)
from orchestrator.a2a.requests import (
    A2AProjectContractError,
    A2AWorkflowMetadata,
    build_send_message_request,
    build_snapshot_handoff_data,
    build_snapshot_handoff_request,
)

__all__ = [
    "A2AAgentClient",
    "A2AProjectContractError",
    "A2AWorkflowMetadata",
    "AgentCardContractError",
    "build_send_message_request",
    "build_snapshot_handoff_data",
    "build_snapshot_handoff_request",
    "validate_agent_card",
]
