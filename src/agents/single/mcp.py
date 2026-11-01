"""Host-only Single MCP configuration, not an Agent or Tool argument parser."""

from dataclasses import replace
from contextlib import asynccontextmanager

from agents.core.identity import SingleAgentIdentity, SinglePhase
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration, MCPClientError, MCPDeliveryState
from mcp_tools.core.policy import SINGLE_PHASE_TOOL_ROLES
from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.unit_config import UnitTestConfiguration


def build_single_mcp_configuration(
    *, phase: SinglePhase, run_id, workspace_id, database_path, workspace_root,
    frozen_source=None, build_configuration=None, unit_test_configuration=None,
    browser_test_configuration=None, security_scan_configuration=None,
    max_call_seconds=60,
) -> MCPChildConfiguration:
    """Narrow approved Host profiles to one immutable Single phase.

    In-loop protected suites remain on the Host's separate configuration;
    only agent-generated tests reach this model-callable child. The original
    configuration is neither mutated nor discarded by this pure factory.
    Phase-specific child policies do not reset a Run's budget/deadline.
    """
    try:
        if not isinstance(phase, SinglePhase):
            raise ValueError
        unit = None
        if unit_test_configuration is not None:
            if not isinstance(unit_test_configuration, UnitTestConfiguration):
                raise ValueError
            role = SINGLE_PHASE_TOOL_ROLES[phase].get("run_unit_tests")
            scopes = tuple(scope for scope in unit_test_configuration.scopes
                           if role in scope.roles and scope.kind != "PROTECTED")
            if scopes:
                unit = replace(unit_test_configuration, scopes=scopes)
        development = phase in (SinglePhase.IMPLEMENTING, SinglePhase.FIXING)
        return MCPChildConfiguration(
            binding=MCPBinding(role=SingleAgentIdentity.SINGLE_AGENT, agent_role=SingleAgentIdentity.SINGLE_AGENT,
                               single_phase=phase, run_id=run_id, workspace_id=workspace_id),
            database_path=database_path, workspace_root=workspace_root, max_call_seconds=max_call_seconds,
            frozen_source=frozen_source,
            build_configuration=build_configuration if development else None,
            unit_test_configuration=unit,
            browser_test_configuration=browser_test_configuration if phase is SinglePhase.QA else None,
            security_scan_configuration=security_scan_configuration if phase is SinglePhase.SECURITY else None,
        )
    except Exception:
        raise MCPClientError("MCP_CLIENT_CONFIGURATION_INVALID", delivery_state=MCPDeliveryState.NOT_SENT) from None


def single_phase_tool_definitions(client: BoundMCPClient):
    """LLM discovery subset; client and server also independently enforce it."""
    if (not isinstance(client, BoundMCPClient)
            or client.configuration.binding.role is not SingleAgentIdentity.SINGLE_AGENT):
        raise MCPClientError("MCP_CLIENT_CONFIGURATION_INVALID", delivery_state=MCPDeliveryState.NOT_SENT)
    names = SINGLE_PHASE_TOOL_ROLES[client.configuration.binding.single_phase]
    return tuple(tool for tool in client.list_tools() if tool.name in names)


@asynccontextmanager
async def open_single_model_client(host_configuration, phase, client_factory):
    """Reuse role-scoped receipt APIs while retaining the Single wire principal.

    Host build/protected checks use their original separate capabilities.
    This view does not start another Agent or copy any model conversation.
    """
    host = host_configuration
    actual = build_single_mcp_configuration(phase=phase,
        run_id=host.binding.run_id, workspace_id=host.binding.workspace_id,
        database_path=host.database_path, workspace_root=host.workspace_root,
        frozen_source=host.frozen_source, build_configuration=host.build_configuration,
        unit_test_configuration=host.unit_test_configuration, browser_test_configuration=host.browser_test_configuration,
        security_scan_configuration=host.security_scan_configuration, max_call_seconds=host.max_call_seconds)
    async with client_factory(actual) as wire:
        if type(wire) is not BoundMCPClient or wire.configuration != actual:
            raise MCPClientError("MCP_CLIENT_CONFIGURATION_INVALID", delivery_state=MCPDeliveryState.NOT_SENT)
        yield BoundMCPClient(configuration=host, _client=wire._client, _state=wire._state,
                             principal_configuration=actual)
