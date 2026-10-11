"""Explicit Host launch; file/Container Tools, no Host Source execution."""

import argparse
import asyncio
import logging
from pathlib import Path
import sys

from mcp_tools.runtime import MCPBinding, MCPDispatcher
from mcp_tools.core.policy import MCPHostPrincipal
from mcp_tools.server import create_server
from mcp_tools.stdio import run_stdio
from mcp_tools.tools.files import FileTools
from mcp_tools.tools.build import BuildTools
from mcp_tools.tools.build_config import decode_build_configuration
from mcp_tools.tools.build_store import BuildOutputStore
from mcp_tools.tools.browser import BrowserTestTools
from mcp_tools.tools.browser_config import decode_browser_configuration
from mcp_tools.tools.browser_store import BrowserTestOutputStore
from mcp_tools.tools.test_reports import TestReportTools
from mcp_tools.tools.security import SecurityScanTools
from mcp_tools.tools.security_config import decode_security_configuration
from mcp_tools.tools.security_store import SecurityScanOutputStore
from mcp_tools.tools.snapshots import FrozenSourceSelection
from mcp_tools.tools.unit import UnitTestTools
from mcp_tools.tools.unit_config import decode_unit_configuration
from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.states import AgentRole
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.docker_cli import DockerCLI
from orchestrator.sandbox.runtime import SandboxRuntime
from orchestrator.workspaces.registry import WorkspaceRegistry


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse ordinarily echoes invalid input values/paths to stderr.
        raise ValueError("MCP_CONFIGURATION_REQUIRED")


def main(argv=None):
    parser = _Parser(description="Host-bound MCP stdio contract server")
    parser.add_argument("--role", required=True,
                        choices=[role.value for role in AgentRole] + [MCPHostPrincipal.ORCHESTRATOR.value])
    parser.add_argument("--agent-role", choices=[role.value for role in AgentRole])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--database-path", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--max-call-seconds", type=float, default=60.0)
    parser.add_argument("--source-artifact-id")
    parser.add_argument("--source-snapshot-sha256")
    parser.add_argument("--build-configuration-json")
    parser.add_argument("--unit-test-configuration-json")
    parser.add_argument("--browser-test-configuration-json")
    parser.add_argument("--security-scan-configuration-json")
    try:
        args = parser.parse_args(argv)
        role = (MCPHostPrincipal.ORCHESTRATOR if args.role == MCPHostPrincipal.ORCHESTRATOR.value
                else AgentRole(args.role))
        binding = MCPBinding(role=role, agent_role=None if args.agent_role is None else AgentRole(args.agent_role),
                             run_id=args.run_id, workspace_id=args.workspace_id)
        database, root = Path(args.database_path), Path(args.workspace_root)
        if not database.is_absolute() or not root.is_absolute() or database.is_symlink() or not database.is_file():
            raise ValueError("MCP_CONFIGURATION_REQUIRED")
        repository = SQLiteWorkflowRepository(database)
        registry = WorkspaceRegistry(repository, root)
        if (args.source_artifact_id is None) != (args.source_snapshot_sha256 is None):
            raise ValueError("MCP_CONFIGURATION_REQUIRED")
        selection = None if args.source_artifact_id is None else FrozenSourceSelection(
            project_artifact_id=args.source_artifact_id,
            snapshot_sha256=args.source_snapshot_sha256,
        )
        artifacts = ArtifactStore(repository, registry)
        tools = FileTools(artifacts, frozen_source=selection)
        configuration = None if args.build_configuration_json is None else decode_build_configuration(args.build_configuration_json)
        docker = None if configuration is None else DockerCLI(endpoint=configuration.docker_endpoint)
        sandbox = SandboxRuntime(repository, registry, artifacts, docker=docker)
        build = BuildTools(artifacts, sandbox, BuildOutputStore(repository), configuration=configuration,
                           max_call_seconds=args.max_call_seconds)
        unit_configuration = (None if args.unit_test_configuration_json is None
                              else decode_unit_configuration(args.unit_test_configuration_json))
        browser_configuration = (None if args.browser_test_configuration_json is None
                                 else decode_browser_configuration(args.browser_test_configuration_json))
        security_configuration = (None if args.security_scan_configuration_json is None
                                  else decode_security_configuration(args.security_scan_configuration_json))
        endpoints = {item.docker_endpoint for item in
                     (configuration, unit_configuration, browser_configuration, security_configuration) if item is not None}
        if len(endpoints) > 1:
            raise ValueError("MCP_CONFIGURATION_REQUIRED")
        unit_docker = None if unit_configuration is None else DockerCLI(endpoint=unit_configuration.docker_endpoint)
        unit_sandbox = SandboxRuntime(repository, registry, artifacts, docker=unit_docker)
        unit = UnitTestTools(artifacts, unit_sandbox, UnitTestOutputStore(repository),
                             configuration=unit_configuration, max_call_seconds=args.max_call_seconds)
        browser_docker = None if browser_configuration is None else DockerCLI(endpoint=browser_configuration.docker_endpoint)
        browser_sandbox = SandboxRuntime(repository, registry, artifacts, docker=browser_docker)
        browser = BrowserTestTools(artifacts, browser_sandbox, BrowserTestOutputStore(repository),
                                  configuration=browser_configuration, max_call_seconds=args.max_call_seconds)
        reports = TestReportTools(unit, browser)
        security_docker = None if security_configuration is None else DockerCLI(endpoint=security_configuration.docker_endpoint)
        security_sandbox = SandboxRuntime(repository, registry, artifacts, docker=security_docker)
        security = SecurityScanTools(artifacts, security_sandbox, SecurityScanOutputStore(repository),
                                     configuration=security_configuration, max_call_seconds=args.max_call_seconds)
        if binding.role is MCPHostPrincipal.ORCHESTRATOR:
            handlers = {**build.handlers(binding.role), **reports.handlers(binding.role), **security.handlers(binding.role)}
        else:
            handlers = {**tools.handlers(binding.role), **build.handlers(binding.role), **unit.handlers(binding.role),
                        **browser.handlers(binding.role), **reports.handlers(binding.role), **security.handlers(binding.role)}
        # All ten MCP Tool handlers are connected; default role executors are
        # still separate work and no Tool result creates a product verdict.
        dispatcher = MCPDispatcher(binding, registry, handlers=handlers,
                                   max_call_seconds=args.max_call_seconds)
        server = create_server(dispatcher)
        # Library diagnostics are never Source/argument Trace. Suppress SDK
        # diagnostic exception formatting at the CLI boundary altogether.
        logging.getLogger("mcp").disabled = True
        logging.getLogger("mcp").setLevel(logging.CRITICAL + 1)
        asyncio.run(run_stdio(server))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception:
        sys.stderr.write("MCP_SERVER_FAILED\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
