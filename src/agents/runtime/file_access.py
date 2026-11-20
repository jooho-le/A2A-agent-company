"""Host-owned path coordinates for model tools, not an authorization grant.

The frozen contract and Security references stay repository-relative. Only this
separate presentation maps them to workspace-relative file tool arguments.
"""

from collections.abc import Mapping
from dataclasses import replace
import re

from agents.llm.contracts import JsonSchema

from orchestrator.domain.states import AgentRole
from orchestrator.workspaces.policy import WORKSPACE_PERMISSIONS, WorkspaceAccess, authorize_path, relative_parts


def constrain_workspace_file_tools(tools, role):
    """Expose workspace coordinates as model schema constraints, not just prose.

    Keep the original MCP contract and filesystem checks. This catches missing
    roots before dispatch without guessing, rewriting or retrying a model path.
    """
    if not isinstance(role, AgentRole) or role not in (
            AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
        raise ValueError("FILE_ACCESS_GUIDANCE_INVALID")
    permissions = WORKSPACE_PERMISSIONS[role]
    result = []
    for tool in tools:
        prefixes = None
        if tool.name == "read_project_file":
            prefixes = permissions.read
        elif tool.name in {"write_source_file", "edit_source_file"} and role is AgentRole.DEVELOPER:
            prefixes = permissions.write
        elif tool.name == "write_test_file" and role in (AgentRole.QA, AgentRole.SECURITY):
            prefixes = tuple(prefix + "tests/" for prefix in permissions.write)
        if prefixes:
            schema = tool.input_schema.to_dict()
            field = schema["properties"]["path"]
            if field.get("type") != "string" or "pattern" in field:
                raise ValueError("FILE_ACCESS_GUIDANCE_INVALID")
            # Root/segment shape only. ACL, traversal, secrets, file identity,
            # frozen Source and test-target enforcement remain in their owners.
            segment = r"[^/\\\x00-\x1f\x7f]+"
            field["pattern"] = "^(?:" + "|".join(re.escape(p) for p in prefixes) + ")" + segment + "(?:/" + segment + ")*$"
            field["description"] = (
                "Workspace-relative file path including an allowed root: " + ", ".join(prefixes)
                + ". Use fileAccess toolPath values for Source; a bare repository path is invalid.")
            tool = replace(tool, input_schema=JsonSchema.from_dict(schema))
        result.append(tool)
    return tuple(result)


def constrain_frozen_read_tools(tools, source_paths, report_refs=()):
    """Narrow model path choices to Host-measured files; never repair arguments.

    MCP keeps its original contract and rechecks the frozen Snapshot and ACL.
    Security report references continue to use repository-relative paths.
    """
    paths = build_file_access({}, AgentRole.SECURITY,
                              observed_source_paths=source_paths)["observedSourceFiles"]
    allowed = list(dict.fromkeys(row["toolPath"] for row in paths))
    if not allowed:
        raise ValueError("FILE_ACCESS_GUIDANCE_INVALID")
    result = []
    for tool in tools:
        if tool.name == "read_project_file":
            schema = tool.input_schema.to_dict()
            schema["properties"]["path"]["enum"] = allowed
            schema["properties"]["path"]["description"] = (
                "Exact workspace path from this enum. Repository-relative report references are not tool paths.")
            tool = replace(tool, input_schema=JsonSchema.from_dict(schema))
        elif tool.name == "read_security_report" and report_refs:
            # Same narrowing for measured scan reports (handoff P5, point 3): an
            # invented reportRef is caught by the schema and corrected before any call.
            schema = tool.input_schema.to_dict()
            schema["properties"]["reportRef"]["enum"] = list(dict.fromkeys(report_refs))
            tool = replace(tool, input_schema=JsonSchema.from_dict(schema))
        result.append(tool)
    return tuple(result)


def build_file_access(scenario_contract, role, *, observed_source_paths=()):
    """Use the admitted Run's contract/inventory; no registry lookup or file I/O.

    These lists do not assert file existence or completeness. The file tools
    still enforce permissions, frozen Snapshot binding and secret checks.
    """
    if not isinstance(role, AgentRole) or role not in (
        AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY,
    ) or not isinstance(scenario_contract, Mapping):
        raise ValueError("FILE_ACCESS_GUIDANCE_INVALID")
    external = scenario_contract.get("externalContract", {})
    if not isinstance(external, Mapping):
        raise ValueError("FILE_ACCESS_GUIDANCE_INVALID")
    starting = external.get("startingFiles", [])
    if not isinstance(starting, list) or not isinstance(observed_source_paths, (list, tuple)):
        raise ValueError("FILE_ACCESS_GUIDANCE_INVALID")

    def paths(values):
        rows = []
        for path in values:
            # Never guess the coordinate system or silently normalize bad paths.
            relative_parts(path)
            tool_path = "source/" + path
            authorize_path(role, tool_path, WorkspaceAccess.READ)
            rows.append({"repositoryPath": path, "toolPath": tool_path})
        return rows

    return {
        "pathBase": "WORKSPACE_ROOT",
        "sourceRoot": "source/",
        "sourceReadMode": "WORKING_COPY" if role is AgentRole.DEVELOPER else "FROZEN_SNAPSHOT",
        "startingFilePaths": paths(starting),
        "observedSourceFiles": paths(observed_source_paths),
        "sourceWriteRoot": "source/" if role is AgentRole.DEVELOPER else None,
        "testWriteRoot": {AgentRole.QA: "outputs/qa/tests/",
                          AgentRole.SECURITY: "outputs/security/tests/"}.get(role),
        "writesRequireOfferedTool": True,
        "listedPathsAreExistenceProof": False,
    }
