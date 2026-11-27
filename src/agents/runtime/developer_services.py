"""Host-owned Developer checkpoint, frozen build, and measured Artifact assembly.

No default wiring, model-selected command/path, product verdict, or registry
report insertion. A missing private Build receipt cannot become a completion.
"""

import asyncio
from dataclasses import replace
from functools import partial
from pathlib import Path
import re
from uuid import uuid4

from a2a.helpers import new_data_part
from a2a.types import Artifact, Task, TaskStatus, TaskState
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from agents.llm.content import sanitize_content
from agents.llm.contracts import json_text, parse_json
from agents.roles.outputs import validate_completed_role_output
from agents.runtime.developer_workspace import GitDeveloperCheckpoint
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration, open_mcp_client
from mcp_tools.execution_runtime import TrackedMCPExecutor
from mcp_tools.execution_store import ToolExecutionStore
from mcp_tools.tools.build_store import BuildOutputStore
from mcp_tools.tools.files import _run_file_operation
from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.sandbox.scn001_start_contract import scn001_build_diagnostics
from orchestrator.sandbox.materialization import _archive_entries
from orchestrator.domain.developer_artifacts import BuildReportArtifact, ChangeReportArtifact, ReadyRecheck, ReadyReview
from orchestrator.domain.repair_progress import selected_checks
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.states import AgentRole, A2ATaskState, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.tool_evidence import ToolExecutionOutcome
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
from orchestrator.workspaces.policy import relative_parts
from orchestrator.workspaces.registry import WorkspaceRegistry


class DeveloperServicesError(ValueError):
    def __init__(self, code="DEVELOPER_SERVICES_INVALID"):
        self.code = code if code in {
            "DEVELOPER_SERVICES_INVALID", "DEVELOPER_BUILD_EVIDENCE_INVALID",
            "DEVELOPER_ARTIFACT_INVALID",
        } else "DEVELOPER_SERVICES_INVALID"
        super().__init__(self.code)


def _schemas():
    # Fixed repository assets only. The registry has no network retriever.
    root = Path(__file__).resolve().parents[3] / "schemas" / "project"
    names = ("developer_source_snapshot.schema.json", "developer_change_report.schema.json",
             "developer_build_report.schema.json", "snapshot_execution_manifest.schema.json",
             "tool_execution_evidence.schema.json")
    result = {}
    for name in names:
        schema = parse_json((root / name).read_text(encoding="utf-8"), max_bytes=1_048_576)
        Draft202012Validator.check_schema(schema)
        result[name] = schema
    resources = []
    for name, schema in result.items():
        resource = Resource.from_contents(schema)
        resources.append((schema["$id"], resource))
        # Some canonical assets use URN $id while reports refer to their fixed
        # sibling filename. Register that local alias too, without fetching it.
        resources.append(("https://a2a-agent-company.local/schemas/project/" + name, resource))
    registry = Registry().with_resources(resources)
    return result, registry


class DeveloperRuntimeServices:
    """One explicitly configured Host Run capability. Construction is inert.

    client_factory is a trusted transport seam, not a model callback. The real
    default starts the existing role-bound local stdio MCP child. Shared SQLite
    receipts, rather than the peer's response alone, authenticate the Build.
    """

    def __init__(self, repository, workspace_registry, artifact_store, *, mcp_configuration,
                 baseline_commit_hash, repository_id, lock_path, client_factory=open_mcp_client):
        try:
            if (not isinstance(repository, SQLiteWorkflowRepository)
                    or not isinstance(workspace_registry, WorkspaceRegistry)
                    or not isinstance(artifact_store, ArtifactStore)
                    or type(mcp_configuration) is not MCPChildConfiguration
                    or not callable(client_factory)
                    or workspace_registry._repository is not repository
                    or artifact_store._repository is not repository
                    or artifact_store._workspaces is not workspace_registry
                    or mcp_configuration.database_path != repository.database_path
                    or mcp_configuration.workspace_root != workspace_registry.base_path
                    or mcp_configuration.binding.role is not AgentRole.DEVELOPER
                    or mcp_configuration.binding.agent_role is not AgentRole.DEVELOPER
                    or mcp_configuration.frozen_source is not None
                    or mcp_configuration.build_configuration is None
                    or not isinstance(baseline_commit_hash, str)
                    or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", baseline_commit_hash) is None
                    or not isinstance(repository_id, str) or not repository_id.strip()
                    or len(repository_id) > 256
                    or any(ord(char) < 32 or ord(char) == 127 for char in repository_id)):
                raise ValueError
            relative_parts(lock_path)
            sanitize_content({"repositoryId": repository_id}, reject_secrets=True)
            profile = mcp_configuration.build_configuration.profile
            timeout = min(profile.limits.timeout_seconds,
                          mcp_configuration.max_call_seconds - 2 * profile.limits.control_timeout_seconds - 1)
            if timeout < 0.01:
                raise ValueError
            effective_profile = replace(profile, limits=replace(profile.limits, timeout_seconds=timeout))
        except Exception:
            raise DeveloperServicesError() from None
        self.configuration = mcp_configuration
        self.client_factory = client_factory
        self._repository, self._workspaces, self._artifacts = repository, workspace_registry, artifact_store
        self._tools, self._builds = ToolExecutionStore(repository), BuildOutputStore(repository)
        self._baseline = baseline_commit_hash
        self._repository_id, self._lock_path = repository_id, lock_path
        self._effective_profile = effective_profile

    def __repr__(self):
        return "DeveloperRuntimeServices()"

    async def prepare(self, execution):
        binding = self.configuration.binding
        if (binding.run_id != execution.metadata.run_id
                or binding.workspace_id != execution.configuration.workspace_id):
            raise DeveloperServicesError()
        baseline = execution.configuration.configuration
        environment = baseline.environment
        image = self.configuration.build_configuration.profile.image_reference
        if (environment is None or environment.network_policy != "DENY"
                or baseline.starting_commit_hash is not None and baseline.starting_commit_hash != self._baseline
                or image is not None and image.rsplit("@", 1)[-1] != environment.container_image_digest):
            raise DeveloperServicesError()
        execution.budget.check()
        workspace = await _run_file_operation(partial(
            self._workspaces.bind, binding.workspace_id, run_id=binding.run_id, role=AgentRole.DEVELOPER))
        initial_files = None
        if execution.previous_source is None:
            from orchestrator.infrastructure.service_continuations import read_continuation
            inherited = await _run_file_operation(partial(read_continuation, self._repository, binding.run_id, files=True))
            if inherited is not None:
                initial_files = inherited[1]
        from orchestrator.domain.service_design import service_policy
        checkpoint = GitDeveloperCheckpoint(workspace, baseline_commit_hash=self._baseline,
                                            allow_unchanged_source=bool(service_policy(execution.configuration.scenario_contract)),
                                            repository_id=self._repository_id, lock_path=self._lock_path,
                                            library_environment={
                                                'containerImageDigest': environment.container_image_digest,
                                                'dependencyLockHash': environment.dependency_lock_hash},
                                            runtime_repair=any(issue.category in {
                                                'BUILD_CODE_FAILURE', 'QA_ASSERTION_FAILURE', 'SECURITY_REQUIREMENT_FAILURE'
                                            } for issue in execution.fix_issues),
                                            limits=self._artifacts._git_limits,
                                            initial_source_files=initial_files,
                                            parent_commit_hash=(None if execution.previous_source is None
                                                                else execution.previous_source.commit_hash))
        await _run_file_operation(partial(checkpoint.prepare,
                                         deadline_monotonic=execution.budget.deadline_monotonic))
        # Do not let an unapproved starting lock/environment reach a model that
        # can write files. The later Source Store repeats this integrity check.
        snapshot = await _run_file_operation(partial(
            GitSnapshotBuilder(Path(workspace.record.root_path) / "source", limits=replace(
                self._artifacts._git_limits, timeout_seconds=min(
                    self._artifacts._git_limits.timeout_seconds, execution.budget.remaining_seconds()))).build,
            self._baseline, self._lock_path))
        if (snapshot.dependency_lock_hash != environment.dependency_lock_hash
                or baseline.starting_snapshot_sha256 is not None
                and snapshot.snapshot_sha256 != baseline.starting_snapshot_sha256):
            raise DeveloperServicesError()
        if execution.previous_source is not None:
            previous = execution.previous_source
            frozen = await _run_file_operation(partial(
                self._artifacts.bind(binding.run_id, role=AgentRole.DEVELOPER).read,
                previous.artifact_id))
            metadata = frozen.metadata
            parent = checkpoint._parent
            if (type(metadata) is not type(previous)
                    or metadata.model_copy(update={"a2a_task_id": previous.a2a_task_id,
                        "a2a_artifact_id": previous.a2a_artifact_id}) != previous
                    or previous.repository_id != self._repository_id
                    or parent.commit_hash != previous.commit_hash or parent.tree_hash != previous.tree_hash
                    or parent.git_object_format != previous.git_object_format.value
                    or parent.snapshot_sha256 != previous.snapshot_sha256
                    or parent.dependency_lock_hash != previous.dependency_lock_hash
                    or parent.archive != frozen.content):
                raise DeveloperServicesError()
        execution.budget.check()
        from agents.runtime.repair_baseline import restore_baseline
        await _run_file_operation(restore_baseline, self, execution, checkpoint)
        if service_policy(execution.configuration.scenario_contract):
            from agents.runtime.service_capabilities import runtime_capabilities
            checkpoint.runtime_capabilities = await _run_file_operation(runtime_capabilities,
                checkpoint.source_root / self._lock_path, environment.dependency_lock_hash)
        execution.budget.check()
        return checkpoint

    def tracked(self, client, execution):
        if type(client) is not BoundMCPClient or client.configuration != self.configuration:
            raise DeveloperServicesError()
        return TrackedMCPExecutor(client, self._tools, workflow_step_id=execution.metadata.workflow_step_id)

    async def finalize(self, execution, checkpoint, decision, tracked, *, task_id, context_id):
        execution.budget.check()
        candidate = await _run_file_operation(partial(
            checkpoint.checkpoint, deadline_monotonic=execution.budget.deadline_monotonic,
            reported_path_edits=decision.reported_path_edits))
        if (candidate.repository_id != self._repository_id or candidate.lock_path != self._lock_path
                or candidate.baseline_commit_hash != self._baseline
                or candidate.parent_commit_hash != (self._baseline if execution.previous_source is None
                                                     else execution.previous_source.commit_hash)):
            raise DeveloperServicesError()
        source = await _run_file_operation(self._freeze_candidate, execution, candidate)
        execution.budget.check()
        tool_sequence, timeout = execution.budget.reserve_tracked_tool_call()
        measured = await asyncio.wait_for(tracked.invoke(
            "run_build", {"workspaceId": str(execution.configuration.workspace_id),
                          "snapshotId": str(source.artifact_id)},
            deadline_monotonic=execution.budget.deadline_monotonic), timeout)
        execution.budget.account_tool_call(tool_sequence)
        execution.budget.check()
        try:
            binding = self.configuration.binding
            record = await _run_file_operation(self._tools.get, binding, measured.record.logical_call_id)
            receipt = await _run_file_operation(self._builds.get, binding.run_id,
                                                measured.data["executionManifestId"])
            evidence = record.to_tool_evidence()
            if (record != measured.record or measured.data != receipt.tool_output()
                    or record.workflow_step_id != execution.metadata.workflow_step_id
                    or record.source_artifact_id != source.artifact_id
                    or evidence.outcome is not ToolExecutionOutcome.PASS
                    or record.execution_manifest != source.execution_manifest()
                    or receipt.workflow_step_id != execution.metadata.workflow_step_id
                    or receipt.workspace_id != execution.configuration.workspace_id
                    or receipt.source_artifact_id != source.artifact_id
                    or receipt.execution_manifest != source.execution_manifest()
                    or receipt.execution_profile != self._effective_profile):
                raise ValueError
            fix = (checkpoint.repair_baseline['fixRequest'] if checkpoint.repair_baseline is not None
                   else execution.initial_payload.get('fixRequest', {}))
            rechecks = tuple(ReadyRecheck(
                issue_id=issue['issueId'], prior_report_id=row['reportArtifactId'],
                prior_protected_receipt_id=row.get('protectedReceiptId'), role=row['role'],
                requirement_id=row['requirementId'], test_id=row['testId'],
                unresolved_cases=sorted({o['caseId'] for o in row['observations'] if o['status'] != 'PASS'}))
                for issue in fix.get('issues', []) for row in selected_checks(issue, fix.get('validationFeedback', [])))
            return await _run_file_operation(self._assemble, execution, source, candidate, rechecks,
                                             decision, receipt, evidence, task_id, context_id)
        except asyncio.CancelledError:
            raise
        except DeveloperServicesError:
            raise
        except Exception:
            raise DeveloperServicesError("DEVELOPER_BUILD_EVIDENCE_INVALID") from None

    def _freeze_candidate(self, execution, candidate):
        # Narrow the existing Host export bound; never reset the Run deadline.
        limited = ArtifactStore(self._repository, self._workspaces, git_limits=replace(
            self._artifacts._git_limits, timeout_seconds=min(
                self._artifacts._git_limits.timeout_seconds, execution.budget.remaining_seconds())))
        return limited.bind(execution.metadata.run_id, role=AgentRole.DEVELOPER).freeze_source(
            workflow_step_id=execution.metadata.workflow_step_id, commit_hash=candidate.commit_hash,
            repository_id=self._repository_id, lock_path=self._lock_path)

    def _assemble(self, execution, staged, candidate, rechecks, decision, receipt, evidence, task_id, context_id):
        try:
            a2a_ids = tuple(str(uuid4()) for _ in range(3))
            source = type(staged).model_validate({**staged.model_dump(),
                "a2a_task_id": task_id, "a2a_artifact_id": a2a_ids[0]})
            self._artifacts.verify_candidate(source)
            previous = {item.artifact_type: item for item in execution.previous_artifacts}
            if (source.code_version != execution.metadata.code_version
                    or source.previous_artifact_id != (None if execution.previous_source is None
                                                       else execution.previous_source.artifact_id)
                    or source.artifact_version != (1 if execution.previous_source is None
                                                    else execution.previous_source.artifact_version + 1)):
                raise ValueError
            def lineage(kind):
                item = previous.get(kind)
                return dict(artifact_version=1 if item is None else item.artifact_version + 1,
                            previous_artifact_id=None if item is None else item.artifact_id)
            common = dict(run_id=source.run_id,
                          workflow_step_id=source.workflow_step_id, a2a_task_id=task_id,
                          requirement_ids=source.requirement_ids, code_version=source.code_version)
            source_files = None
            if receipt.exit_code != 0:
                frozen = self._artifacts.bind(source.run_id, role=AgentRole.DEVELOPER).read(source.artifact_id)
                source_files = {path: content for path, content, _mode in _archive_entries(frozen.content)}
            diagnostics = scn001_build_diagnostics(argv=receipt.execution_profile.argv,
                exit_code=receipt.exit_code, stdout=receipt.stdout, source_files=source_files)
            from orchestrator.domain.service_design import service_policy, design_for_execution
            if service_policy(execution.configuration.scenario_contract) and receipt.exit_code != 0:
                from orchestrator.sandbox.service_start_contract import service_build_diagnostics
                diagnostics = service_build_diagnostics(argv=receipt.execution_profile.argv,
                    exit_code=receipt.exit_code, stdout=receipt.stdout,
                    design=design_for_execution(execution), source_files=source_files or {})
            build = BuildReportArtifact(artifact_id=uuid4(), a2a_artifact_id=a2a_ids[2],
                source_artifact_id=source.artifact_id, exit_code=receipt.exit_code,
                duration_ms=receipt.duration_ms, execution_manifest_id=receipt.execution_manifest_id,
                execution_manifest=receipt.execution_manifest, stdout_ref=receipt.stdout_ref,
                stderr_ref=receipt.stderr_ref,
                diagnostics=diagnostics,
                execution_outcome=ToolExecutionOutcome.PASS if receipt.exit_code == 0 else ToolExecutionOutcome.FAIL,
                failure_kind=None if receipt.exit_code == 0 else "PRODUCT", tool_evidence=evidence,
                **lineage("BUILD_REPORT"), **common)
            review = ReadyReview(working_base_commit=candidate.working_base_commit, edits=candidate.edits, rechecks=rechecks,
                                 reported_path_edits=candidate.reported_path_edits,
                                 source_artifact_id=source.artifact_id, build_report_id=build.artifact_id,
                                 build_outcome="PASS" if build.passed else "FAIL",
                                 validation_status="PENDING_QA_SECURITY" if build.passed else "BLOCKED_BY_BUILD")
            change = ChangeReportArtifact(artifact_id=uuid4(), a2a_artifact_id=a2a_ids[1],
                                          summary=decision.summary, changes=candidate.changes, ready_review=review,
                                          **lineage("CHANGE_REPORT"), **common)
            schemas, registry = _schemas()
            names = ("source-snapshot.json", "change-report.json", "build-report.json")
            schema_names = ("developer_source_snapshot.schema.json", "developer_change_report.schema.json",
                            "developer_build_report.schema.json")
            artifacts = []
            for name, schema_name, item in zip(names, schema_names, (source, change, build)):
                payload = item.model_dump(mode="json", by_alias=True)
                sanitize_content(payload, reject_secrets=True)
                payload = parse_json(json_text(payload, max_bytes=1_048_576), max_bytes=1_048_576)
                Draft202012Validator(schemas[schema_name], registry=registry,
                                     format_checker=FormatChecker()).validate(payload)
                artifacts.append(Artifact(artifact_id=item.a2a_artifact_id, name=name,
                    parts=[new_data_part(payload, media_type="application/json")], metadata={
                        "runId": str(source.run_id), "workflowStepId": str(source.workflow_step_id),
                        "projectArtifactId": str(item.artifact_id), "artifactVersion": item.artifact_version}))
            task = Task(id=task_id, context_id=context_id, status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
                        artifacts=artifacts, metadata=execution.metadata.model_dump(mode="json", by_alias=True, exclude_none=True))
            run = WorkflowRun(run_id=source.run_id, workspace_id=execution.configuration.workspace_id,
                scenario_id=execution.metadata.scenario_id, request_text=execution.request_text,
                status=WorkflowStatus.IMPLEMENTING if execution.fix_attempt == 0 else WorkflowStatus.FIXING,
                fix_attempt=execution.fix_attempt,
                code_version=None if execution.previous_source is None else execution.previous_source.code_version)
            step = WorkflowStep(run_id=run.run_id, workflow_step_id=source.workflow_step_id,
                agent_role=AgentRole.DEVELOPER, status=WorkflowStepStatus.SUCCEEDED,
                a2a_task_id=task_id, agent_context_id=context_id, a2a_task_state=A2ATaskState.COMPLETED,
                attempt=execution.metadata.attempt, code_version=source.code_version,
                requirement_ids=list(source.requirement_ids),
                input_artifact_ids=list(execution.metadata.project_artifact_ids or ()))
            validate_completed_role_output(AgentRole.DEVELOPER, task=task, run=run, step=step)
            return tuple(artifacts)
        except Exception:
            raise DeveloperServicesError("DEVELOPER_ARTIFACT_INVALID") from None
