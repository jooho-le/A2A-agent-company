"""Explicit Host launch; no dotenv, Agent Key, arbitrary imports, or Tool bodies."""

import argparse
import asyncio
import logging
from pathlib import Path
import sys

from mcp_tools.runtime import MCPBinding, MCPDispatcher
from mcp_tools.server import create_server
from mcp_tools.stdio import run_stdio
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
    try:
        args = parser.parse_args(argv)
        binding = MCPBinding(role=AgentRole(args.role), agent_role=AgentRole(args.agent_role),
                             run_id=args.run_id, workspace_id=args.workspace_id)
        database, root = Path(args.database_path), Path(args.workspace_root)
        if not database.is_absolute() or not root.is_absolute() or database.is_symlink() or not database.is_file():
            raise ValueError("MCP_CONFIGURATION_REQUIRED")
        repository = SQLiteWorkflowRepository(database)
        registry = WorkspaceRegistry(repository, root)
        # No actual Tool handlers before24–28. Each authorized contract call
        # revalidates the existing Registry and reports TOOL_NOT_IMPLEMENTED.
        dispatcher = MCPDispatcher(binding, registry, max_call_seconds=args.max_call_seconds)
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
