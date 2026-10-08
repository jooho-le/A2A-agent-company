"""Explicit Host launch; actual file Tools, no Source execution or Agent Key."""

import argparse
import asyncio
import logging
from pathlib import Path
import sys

from mcp_tools.runtime import MCPBinding, MCPDispatcher
from mcp_tools.server import create_server
from mcp_tools.stdio import run_stdio
from mcp_tools.tools.files import FileTools
from mcp_tools.tools.snapshots import FrozenSourceSelection
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.states import AgentRole
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.workspaces.registry import WorkspaceRegistry


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse ordinarily echoes invalid input values/paths to stderr.
        raise ValueError("MCP_CONFIGURATION_REQUIRED")


def main(argv=None):
    parser = _Parser(description="Host-bound MCP stdio contract server")
    parser.add_argument("--role", required=True, choices=[role.value for role in AgentRole])
    parser.add_argument("--agent-role", required=True, choices=[role.value for role in AgentRole])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--database-path", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--max-call-seconds", type=float, default=60.0)
    parser.add_argument("--source-artifact-id")
    parser.add_argument("--source-snapshot-sha256")
    try:
        args = parser.parse_args(argv)
        binding = MCPBinding(role=AgentRole(args.role), agent_role=AgentRole(args.agent_role),
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
        tools = FileTools(ArtifactStore(repository, registry), frozen_source=selection)
        # File handlers are real. Build/Test/Scan/Report remain explicit stubs.
        dispatcher = MCPDispatcher(binding, registry, handlers=tools.handlers(binding.role),
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
