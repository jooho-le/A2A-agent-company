"""Server-issued workspace IDs and trusted roots for the MCP registry."""

from datetime import datetime
from pathlib import Path

from pydantic import Field, UUID4, field_validator

from orchestrator.domain.models import utc_now
from orchestrator.domain.snapshot_handoff import ImmutableDomainModel


class WorkspaceRecord(ImmutableDomainModel):
    workspace_id: UUID4 = Field(alias="workspaceId")
    run_id: UUID4 = Field(alias="runId")
    root_path: str = Field(alias="rootPath", min_length=1)
    created_at: datetime = Field(default_factory=utc_now, alias="createdAt")

    @field_validator("root_path")
    @classmethod
    def trusted_absolute_root(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("workspace roots must be server-resolved absolute paths")
        return str(Path(value).resolve())

    def public_contract(self) -> dict[str, object]:
        """Host paths are registry data, never an LLM-chosen Tool input."""
        return {
            "workspaceId": str(self.workspace_id),
            "runId": str(self.run_id),
            "permissions": {
                "PLANNER": {"write": ["planning/"]},
                "DEVELOPER": {"write": ["source/"], "snapshot": "READ_ONLY"},
                "QA": {"write": ["outputs/qa/"], "snapshot": "READ_ONLY"},
                "SECURITY": {"write": ["outputs/security/"], "snapshot": "READ_ONLY"},
            },
        }
