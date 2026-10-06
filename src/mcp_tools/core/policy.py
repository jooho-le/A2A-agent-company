"""Project contract declarations; Tool discovery/enforcement arrives in step 23."""

from types import MappingProxyType

from orchestrator.domain.states import AgentRole


MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_TRANSPORT = "stdio"

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
        "read_project_file", "run_security_scan", "read_security_report",
    ),
})
