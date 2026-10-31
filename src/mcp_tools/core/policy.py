"""Immutable role policy enforced by the Host-bound MCP dispatcher."""

from types import MappingProxyType
from enum import Enum

from agents.core.identity import SingleAgentIdentity, SinglePhase
from orchestrator.domain.states import AgentRole


MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_TRANSPORT = "stdio"


class MCPHostPrincipal(str, Enum):
    """Trusted Host capabilities, never an additional LLM Agent role."""

    ORCHESTRATOR = "ORCHESTRATOR"


# Narrow internal delegation preserves all existing Workspace, Snapshot,
# Sandbox and report ACLs. This is selected by the server for a fixed Tool,
# never from request metadata or model-supplied arguments.
ORCHESTRATOR_TOOL_ROLES = MappingProxyType({
    "run_build": AgentRole.DEVELOPER,
    "read_test_report": AgentRole.QA,
    "read_security_report": AgentRole.SECURITY,
})

# Planner receives no product-source Tool. A document-only Tool can be added
# later with its own schema and permissions if planning actually needs it.
ROLE_TOOL_NAMES = MappingProxyType({
    AgentRole.PLANNER: (),
    AgentRole.DEVELOPER: (
        "read_project_file", "write_source_file", "apply_patch", "run_build",
        "run_unit_tests",
    ),
    AgentRole.QA: (
        "read_project_file", "write_test_file", "run_unit_tests",
        "run_browser_tests", "read_test_report",
    ),
    AgentRole.SECURITY: (
        "read_project_file", "write_test_file", "run_unit_tests", "run_security_scan", "read_security_report",
    ),
})

# Complete capabilities of Multi, without adding shell/network/Host powers.
SINGLE_TOOL_NAMES = tuple(dict.fromkeys(name for names in ROLE_TOOL_NAMES.values() for name in names))
_SINGLE_DEVELOPMENT = {
    **{name: AgentRole.DEVELOPER for name in ROLE_TOOL_NAMES[AgentRole.DEVELOPER]},
    "read_test_report": AgentRole.QA, "read_security_report": AgentRole.SECURITY,
}
SINGLE_PHASE_TOOL_ROLES = MappingProxyType({
    SinglePhase.PLANNING: MappingProxyType({}),
    SinglePhase.IMPLEMENTING: MappingProxyType(_SINGLE_DEVELOPMENT),
    SinglePhase.FIXING: MappingProxyType(_SINGLE_DEVELOPMENT),
    SinglePhase.QA: MappingProxyType({name: AgentRole.QA for name in ROLE_TOOL_NAMES[AgentRole.QA]}),
    SinglePhase.SECURITY: MappingProxyType({name: AgentRole.SECURITY for name in ROLE_TOOL_NAMES[AgentRole.SECURITY]}),
})


# ROLE_TOOL_NAMES remains the four LLM roles' immutable contract. The Host
# principal is intentionally absent from Agent role contracts and prompts.
MCP_TOOL_NAMES = MappingProxyType({
    **ROLE_TOOL_NAMES,
    MCPHostPrincipal.ORCHESTRATOR: tuple(ORCHESTRATOR_TOOL_ROLES),
    SingleAgentIdentity.SINGLE_AGENT: SINGLE_TOOL_NAMES,
})
