"""Host-owned Artifact capabilities, never model-selected role or filesystem URI.

This stages actual SOURCE before Build. It does not register an A2A completion,
make product verdicts, or automatically activate existing bootstrap Agents.
"""

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from orchestrator.artifacts.contracts import ArtifactAccessError, ArtifactErrorCode, StoredContent
from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder, GitSnapshotLimits
from orchestrator.artifacts.sqlite_store import SQLiteArtifactContentStore, canonical_report_content
from orchestrator.domain.snapshot_handoff import (
    CodeSnapshotArtifact, SnapshotHandoff, code_version_for_fix_attempt,
)
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.workspaces.filesystem import walk_directory
from orchestrator.workspaces.policy import WorkspaceAccessError, workspace_uuid


_REPORT_NAMES = {
    "REQUIREMENT": "requirements.json",
    "CHANGE_REPORT": "change-report.json",
    "BUILD_REPORT": "build-report.json",
    "QA_REPORT": "report.json",
    "SECURITY_REPORT": "report.json",
}
_PRODUCERS = {
    "REQUIREMENT": AgentRole.PLANNER,
    "CHANGE_REPORT": AgentRole.DEVELOPER,
    "BUILD_REPORT": AgentRole.DEVELOPER,
    "QA_REPORT": AgentRole.QA,
    "SECURITY_REPORT": AgentRole.SECURITY,
}


def _uuid(value):
    try:
        return workspace_uuid(value)
    except WorkspaceAccessError:
        raise ArtifactAccessError(ArtifactErrorCode.INVALID) from None


def _source_identity(snapshot):
    # Final A2A references are attached by the Host after the Developer Task.
    # They cannot change any staged content/execution/lineage identity.
    return snapshot.model_dump(mode="json", by_alias=True, exclude={"a2a_task_id", "a2a_artifact_id"})


class ArtifactStore:
    def __init__(self, repository, workspace_registry, *, git_limits=None):
        self._repository = repository
        self._workspaces = workspace_registry
        self._contents = SQLiteArtifactContentStore(repository)
        self._git_limits = git_limits if git_limits is not None else GitSnapshotLimits()
        if not isinstance(self._git_limits, GitSnapshotLimits):
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)

    def _run(self, run_id):
        run_id = _uuid(run_id)
        try:
            run = self._repository.get_run(run_id)
        except Exception:
            raise ArtifactAccessError(ArtifactErrorCode.IO) from None
        if run is None:
            raise ArtifactAccessError(ArtifactErrorCode.NOT_FOUND)
        if run.run_id != run_id:
            raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
        return run

    def _binding(self, run_id, role):
        if not isinstance(role, AgentRole):
            raise ArtifactAccessError(ArtifactErrorCode.DENIED)
        run = self._run(run_id)
        try:
            workspace = self._workspaces.bind(run.workspace_id, run_id=run.run_id, role=role)
        except WorkspaceAccessError:
            raise ArtifactAccessError(ArtifactErrorCode.DENIED) from None
        return run, workspace

    def bind(self, run_id, *, role):
        run, _ = self._binding(run_id, role)
        return BoundArtifactStore(run_id=run.run_id, role=role, store=self)

    def get_snapshot(self, run_id, artifact_id):
        """Trusted Orchestrator read, not an Agent-exposed bypass capability."""
        run = self._run(run_id)
        record = self._contents.get(run.run_id, _uuid(artifact_id))
        if not isinstance(record.metadata, CodeSnapshotArtifact):
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)
        self._check_source(run, record)
        return record.metadata

    def verify_candidate(self, snapshot):
        """Require final Source metadata to preserve the actual staged snapshot."""
        if not isinstance(snapshot, CodeSnapshotArtifact):
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)
        staged = self.get_snapshot(snapshot.run_id, snapshot.artifact_id)
        if _source_identity(staged) != _source_identity(snapshot):
            raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
        return staged

    def _check_source(self, run, record):
        source = record.metadata
        try:
            configuration = self._repository.get_run_configuration(run.run_id)
        except Exception:
            raise ArtifactAccessError(ArtifactErrorCode.IO) from None
        environment = configuration.configuration.environment if configuration is not None else None
        if environment is None:
            raise ArtifactAccessError(ArtifactErrorCode.CONFIGURATION)
        if (
            source.run_id != run.run_id
            or configuration.run_id != run.run_id
            or configuration.workspace_id != run.workspace_id
            or configuration.scenario_id != run.scenario_id
            or record.media_type != "application/x-tar"
            or source.artifact_uri != f"artifact://{source.artifact_id}/source.tar"
            or source.snapshot_sha256 != record.content_sha256
            or source.container_image_digest != environment.container_image_digest
            or source.dependency_lock_hash != environment.dependency_lock_hash
        ):
            raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)

    def _read(self, run_id, role, artifact_id):
        run, _ = self._binding(run_id, role)
        record = self._contents.get(run.run_id, _uuid(artifact_id))
        if isinstance(record.metadata, CodeSnapshotArtifact):
            if role is AgentRole.PLANNER:
                raise ArtifactAccessError(ArtifactErrorCode.DENIED)
            if role in (AgentRole.QA, AgentRole.SECURITY) and not self._contents.has_grant(run.run_id, record.artifact_id, role):
                raise ArtifactAccessError(ArtifactErrorCode.DENIED)
            self._check_source(run, record)
        elif role is AgentRole.PLANNER and record.metadata.artifact_type != "REQUIREMENT":
            raise ArtifactAccessError(ArtifactErrorCode.DENIED)
        return record

    def _freeze(self, run_id, role, *, workflow_step_id, commit_hash, repository_id, lock_path):
        if role is not AgentRole.DEVELOPER:
            raise ArtifactAccessError(ArtifactErrorCode.DENIED)
        run, workspace = self._binding(run_id, role)
        workflow_step_id = _uuid(workflow_step_id)
        if run.status not in (WorkflowStatus.IMPLEMENTING, WorkflowStatus.FIXING):
            raise ArtifactAccessError(ArtifactErrorCode.DENIED)
        try:
            configuration = self._repository.get_run_configuration(run.run_id)
            steps = self._repository.list_steps(run.run_id)
        except Exception:
            raise ArtifactAccessError(ArtifactErrorCode.IO) from None
        environment = configuration.configuration.environment if configuration is not None else None
        if environment is None:
            raise ArtifactAccessError(ArtifactErrorCode.CONFIGURATION)
        if (
            configuration.run_id != run.run_id or configuration.workspace_id != run.workspace_id
            or configuration.scenario_id != run.scenario_id
        ):
            raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
        step = next((item for item in steps if item.workflow_step_id == workflow_step_id), None)
        if (
            step is None or step.run_id != run.run_id or step.agent_role is not role
            or step.status not in (WorkflowStepStatus.RUNNING, WorkflowStepStatus.SUCCEEDED)
            or step.attempt != run.fix_attempt or not step.requirement_ids
            or (step.code_version is not None and step.code_version != code_version_for_fix_attempt(run.fix_attempt))
            or not isinstance(repository_id, str) or not repository_id.strip()
            or len(repository_id) > 256 or any(ord(char) < 32 for char in repository_id)
        ):
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)

        # Root comes only from the validated Registry. Keep its handle open
        # during the read-only Git operation; never accept a Host path from LLM.
        # This does NOT pin Git's subprocess pathname/cwd against same-UID
        # malicious replacement. That protection belongs to Sandbox step 22.
        try:
            with self._workspaces._open_workspace(workspace.record) as root_fd:
                with walk_directory(root_fd, ("source",)):
                    candidate = GitSnapshotBuilder(
                        Path(workspace.record.root_path) / "source", limits=self._git_limits,
                    ).build(commit_hash, lock_path)
        except WorkspaceAccessError:
            raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED) from None
        if candidate.dependency_lock_hash != environment.dependency_lock_hash:
            raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
        code_version = code_version_for_fix_attempt(run.fix_attempt)
        previous = self._contents.latest_source(run.run_id)
        if previous is not None and previous.code_version == code_version:
            existing = self._contents.get(run.run_id, previous.artifact_id)
            self._check_source(run, existing)
            if (
                previous.workflow_step_id != workflow_step_id
                or previous.repository_id != repository_id.strip()
                or previous.requirement_ids != tuple(step.requirement_ids)
                or previous.commit_hash != candidate.commit_hash
                or previous.tree_hash != candidate.tree_hash
                or previous.git_object_format.value != candidate.git_object_format
                or previous.snapshot_sha256 != candidate.snapshot_sha256
                or previous.dependency_lock_hash != candidate.dependency_lock_hash
            ):
                raise ArtifactAccessError(ArtifactErrorCode.CONFLICT)
            # Even a duplicate freeze must recheck cancellation/Step changes
            # under the publication transaction after the Git operation.
            return self._contents.put(
                previous, existing.content, existing.media_type,
                grants=(AgentRole.QA, AgentRole.SECURITY),
            ).metadata
        if (
            code_version == 1 and previous is not None
            or code_version > 1 and (previous is None or previous.code_version != code_version - 1)
        ):
            raise ArtifactAccessError(ArtifactErrorCode.CONFLICT)
        artifact_id = uuid4()
        source = CodeSnapshotArtifact(
            artifact_id=artifact_id, artifact_version=1 if previous is None else previous.artifact_version + 1,
            previous_artifact_id=None if previous is None else previous.artifact_id,
            run_id=run.run_id, workflow_step_id=workflow_step_id,
            requirement_ids=tuple(step.requirement_ids), code_version=code_version,
            repository_id=repository_id, commit_hash=candidate.commit_hash,
            git_object_format=candidate.git_object_format, tree_hash=candidate.tree_hash,
            snapshot_sha256=candidate.snapshot_sha256, artifact_uri=f"artifact://{artifact_id}/source.tar",
            container_image_digest=environment.container_image_digest,
            dependency_lock_hash=candidate.dependency_lock_hash,
        )
        stored = self._contents.put(source, candidate.archive, "application/x-tar", grants=(AgentRole.QA, AgentRole.SECURITY))
        return stored.metadata

    def _publish_report(self, run_id, role, artifact_id):
        run, _ = self._binding(run_id, role)
        artifact_id = _uuid(artifact_id)
        try:
            artifact = next((item for item in self._repository.list_project_artifacts(run.run_id) if item.artifact_id == artifact_id), None)
        except Exception:
            raise ArtifactAccessError(ArtifactErrorCode.IO) from None
        if artifact is None:
            raise ArtifactAccessError(ArtifactErrorCode.NOT_FOUND)
        if _PRODUCERS.get(artifact.artifact_type) is not role:
            raise ArtifactAccessError(ArtifactErrorCode.DENIED)
        if artifact.artifact_uri != f"artifact://{artifact_id}/{_REPORT_NAMES[artifact.artifact_type]}":
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)
        if artifact.artifact_type in {"BUILD_REPORT", "QA_REPORT", "SECURITY_REPORT"}:
            source = self.get_snapshot(run.run_id, artifact.execution_manifest.project_artifact_id)
            if source.execution_manifest() != artifact.execution_manifest:
                raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
        content = canonical_report_content(artifact)
        return self._contents.put(artifact, content, "application/json")


@dataclass(frozen=True, kw_only=True)
class BoundArtifactStore:
    run_id: UUID
    role: AgentRole
    store: ArtifactStore = field(repr=False)

    def freeze_source(self, *, workflow_step_id, commit_hash, repository_id, lock_path):
        return self.store._freeze(
            self.run_id, self.role, workflow_step_id=workflow_step_id,
            commit_hash=commit_hash, repository_id=repository_id, lock_path=lock_path,
        )

    def read(self, artifact_id):
        return self.store._read(self.run_id, self.role, artifact_id)

    def read_uri(self, uri):
        if not isinstance(uri, str):
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)
        try:
            parsed = urlsplit(uri)
            artifact_id = _uuid(parsed.netloc)
        except (ValueError, ArtifactAccessError):
            raise ArtifactAccessError(ArtifactErrorCode.INVALID) from None
        if parsed.scheme != "artifact" or parsed.query or parsed.fragment:
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)
        record = self.read(artifact_id)
        if uri != record.metadata.artifact_uri:
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)
        return record

    def publish_report(self, artifact_id):
        return self.store._publish_report(self.run_id, self.role, artifact_id)

    def verify_handoff(self, handoff):
        if self.role not in (AgentRole.QA, AgentRole.SECURITY):
            raise ArtifactAccessError(ArtifactErrorCode.DENIED)
        if not isinstance(handoff, SnapshotHandoff) or handoff.run_id != self.run_id:
            raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
        record = self.read(handoff.project_artifact_id)
        if (
            not isinstance(record.metadata, CodeSnapshotArtifact)
            or handoff.artifact_uri != record.metadata.artifact_uri
            or handoff.execution_manifest != record.metadata.execution_manifest()
        ):
            raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
        return record
