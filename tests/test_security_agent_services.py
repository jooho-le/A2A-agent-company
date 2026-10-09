"""Security service trust boundaries with real storage/MCP and Fake Docker.

No Bandit, browser, container, or generated product code execution is claimed.
The optional verifier fixtures inspect inert bytes; they are not SCN-001 proof.
"""

import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid1, uuid4

from google.protobuf.json_format import MessageToDict

from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.roles.security_contract import (
    SecurityCodeReference, SecurityDecision, SecurityFindingReview, SecurityRequirementReview,
)
from agents.runtime.security_services import SecurityRuntimeServices, SecuritySemanticProof, SecurityServicesError
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration
from mcp_tools.execution_runtime import TrackedMCPError
from mcp_tools.runtime import MCPDispatcher
from mcp_tools.tools.files import FileTools
from mcp_tools.tools.security import SecurityScanTools
from mcp_tools.tools.security_config import SecurityScanConfiguration
from mcp_tools.tools.snapshots import FrozenSourceSelection
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.models import WorkflowStep
from orchestrator.domain.scenario_registry import SCENARIO_REGISTRY, RequirementValidator
from orchestrator.domain.states import AgentRole, WorkflowStatus
from orchestrator.domain.validation_artifacts import SecurityReportArtifact, FindingDisposition, ValidationOutcome
from orchestrator.sandbox.contracts import CLIResult, SandboxLimits
from orchestrator.sandbox.runtime import SandboxRuntime
from test_mcp_execution_runtime import DispatcherSession
import test_mcp_security_store as store_fixture
from test_sandbox_runtime import FakeDocker, IMAGE_ID


class SecurityAgentServicesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = store_fixture.SecurityScanOutputStoreTests("test_constructor_is_inert_and_safe")
        self.fixture.setUp()
        for cleanup, args, kwargs in self.fixture._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        self.fixture._cleanups.clear()
        self.repository, self.registry = self.fixture.repository, self.fixture.registry
        self.artifacts = ArtifactStore(self.repository, self.registry)
        self.ids = SCENARIO_REGISTRY[self.fixture.run.scenario_id].requirement_ids_for(RequirementValidator.SECURITY)
        self.step = WorkflowStep.model_validate({**self.fixture.step.model_dump(), "requirement_ids": list(self.ids)})
        self.fixture.mutate_step(self.step)
        self.source = type(self.fixture.source).model_validate({**self.fixture.source.model_dump(),
            "a2a_task_id": "developer-task", "a2a_artifact_id": "source-artifact"})
        with self.repository._transaction() as connection:
            connection.execute("INSERT INTO project_artifacts VALUES(?,?,?,?,?)", (
                str(self.source.artifact_id), str(self.source.run_id), "SOURCE", 1, self.source.model_dump_json()))
        self.docker = FakeDocker()
        self.sandbox = SandboxRuntime(self.repository, self.registry, self.artifacts, docker=self.docker)
        policy = SecurityScanConfiguration(profiles=(self.fixture.scanner_profile,),
            image_reference=IMAGE_ID, limits=SandboxLimits(timeout_seconds=60, control_timeout_seconds=.5))
        self.configuration = MCPChildConfiguration(binding=self.fixture.binding,
            database_path=self.repository.database_path, workspace_root=self.registry.base_path,
            frozen_source=FrozenSourceSelection(project_artifact_id=self.source.artifact_id,
                snapshot_sha256=self.source.snapshot_sha256), security_scan_configuration=policy, max_call_seconds=5)
        self.metadata = A2AWorkflowMetadata(run_id=self.fixture.run.run_id,
            workflow_step_id=self.step.workflow_step_id, scenario_id=self.fixture.run.scenario_id,
            attempt=0, code_version=1, requirement_ids=self.ids, project_artifact_ids=(self.source.artifact_id,))
        self.execution = SimpleNamespace(metadata=self.metadata, configuration=self.fixture.run_configuration,
            source=self.source, request_text="회원가입 보안 검토", budget=ExecutionBudget(runtime_budget_ms=15000,
                limits=LLMLimits(max_tool_calls=10, tool_timeout_seconds=5)))
        self.reference = SecurityCodeReference(path="main.py", start_line=1, end_line=1)
        self.set_scan_report()

    def services(self, **options):
        return SecurityRuntimeServices(self.repository, self.registry, self.artifacts,
            mcp_configuration=self.configuration, **options)

    def tracked(self, services):
        scans = SecurityScanTools(self.artifacts, self.sandbox, self.fixture.store,
            configuration=self.configuration.security_scan_configuration, max_call_seconds=5)
        files = FileTools(self.artifacts, frozen_source=self.configuration.frozen_source)
        dispatcher = MCPDispatcher(self.fixture.binding, self.registry,
            handlers={**scans.handlers(AgentRole.SECURITY), **files.handlers(AgentRole.SECURITY)}, max_call_seconds=5)
        self.session = DispatcherSession(dispatcher)
        client = BoundMCPClient(configuration=self.configuration,
            _client=type("SDKPeerAdapter", (), {"session": self.session})())
        return services.tracked(client, self.execution)

    def set_scan_report(self, findings=()):
        self.docker.exit_code = int(bool(findings))
        self.docker.start_result = CLIResult(returncode=self.docker.exit_code, stderr=b"",
            stdout=self.fixture.report_json(findings=list(findings)).encode())

    async def scan(self, services=None):
        services = services or self.services()
        await services.prepare(self.execution)
        tracked = self.tracked(services)
        return services, tracked, await services.scan(self.execution, tracked)

    def decision(self, measured, *, outcome="UNVERIFIED", disposition="SUSPECTED", references=()):
        return SecurityDecision(kind="READY", questions=(), requirement_reviews=tuple(SecurityRequirementReview(
            requirement_id=identifier, proposed_outcome=outcome, rationale="별도 의미 검증 근거가 필요합니다.",
            references=references) for identifier in self.ids), finding_reviews=tuple(SecurityFindingReview(
                finding_id=identifier, proposed_disposition=disposition,
                rationale="정적 분석 후보를 독립적으로 검토해야 합니다.", references=references)
                for identifier in measured.finding_ids))

    async def read(self, services, tracked, measured, *, path="source/main.py"):
        self.execution.budget.reserve_tool_call()
        return await services.invoke(tracked, self.execution, "read_project_file", {
            "workspaceId": str(self.execution.configuration.workspace_id), "path": path}, measured)

    async def finish(self, services, tracked, measured, decision=None):
        artifacts = await services.finalize(self.execution, decision or self.decision(measured), tracked, measured,
            task_id="security-task", context_id="security-context")
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].name, "security-report.json")
        return SecurityReportArtifact.model_validate(MessageToDict(artifacts[0].parts[0].data))

    def proof(self, measured, *, outcomes=(), dispositions=(), **extra):
        return SecuritySemanticProof(run_id=self.metadata.run_id, workflow_step_id=self.metadata.workflow_step_id,
            source_artifact_id=self.source.artifact_id, snapshot_sha256=self.source.snapshot_sha256,
            requirement_outcomes=outcomes, finding_dispositions=dispositions, **extra)

    async def test_constructor_is_inert_and_private_repr(self):
        with patch.object(self.repository, "_connection", side_effect=AssertionError("inert")), \
                patch.object(self.repository, "_transaction", side_effect=AssertionError("inert")):
            services = self.services()
        self.assertEqual(repr(services), "SecurityRuntimeServices()")

    async def test_missing_frozen_source_or_profile_rejected(self):
        for configuration in (replace(self.configuration, frozen_source=None),
                              replace(self.configuration, security_scan_configuration=None)):
            with self.subTest(configuration=configuration), self.assertRaises(SecurityServicesError):
                SecurityRuntimeServices(self.repository, self.registry, self.artifacts, mcp_configuration=configuration)

    async def test_zero_findings_is_not_requirement_pass_and_does_not_write_verdict(self):
        before = self.repository.get_run(self.metadata.run_id)
        services, tracked, measured = await self.scan()
        self.assertEqual(measured.finding_ids, ())
        self.assertEqual(measured.source_paths, ("main.py", "requirements.lock"))
        self.assertEqual(self.execution.budget.tool_calls, 1)
        report = await self.finish(services, tracked, measured)
        self.assertEqual({result.outcome for result in report.requirement_results}, {ValidationOutcome.UNVERIFIED})
        self.assertTrue(all(result.tool_evidence is None for result in report.requirement_results))
        self.assertEqual(report.execution_manifest, self.source.execution_manifest())
        self.assertIsNone(report.previous_artifact_id)
        self.assertEqual((report.code_version, report.artifact_version), (1, 1))
        self.assertEqual(self.repository.get_run(self.metadata.run_id), before)
        self.assertEqual(len(self.repository.list_project_artifacts(self.metadata.run_id)), 1)

    async def test_all_scanner_warnings_retained_as_suspected(self):
        self.set_scan_report((self.fixture.finding(),))
        services, tracked, measured = await self.scan()
        report = await self.finish(services, tracked, measured)
        self.assertEqual(len(report.findings), 1)
        self.assertEqual(report.findings[0].finding_id, measured.finding_ids[0])
        self.assertEqual(report.findings[0].disposition, FindingDisposition.SUSPECTED)
        self.assertEqual(report.findings[0].severity.value, "LOW")
        self.assertEqual(report.findings[0].normalized_location, "main.py:1")
        self.assertEqual(report.findings[0].evidence_ref, measured.scans[0][1].report_ref)
        self.assertTrue(all(result.outcome is ValidationOutcome.UNVERIFIED for result in report.requirement_results))

    async def test_proposed_unverified_warning_stays_unverified(self):
        self.set_scan_report((self.fixture.finding(),))
        services, tracked, measured = await self.scan()
        report = await self.finish(services, tracked, measured, self.decision(measured, disposition="UNVERIFIED"))
        self.assertEqual(report.findings[0].disposition, FindingDisposition.UNVERIFIED)

    async def test_measured_summary_detached_immutable_and_repr_excludes_source(self):
        services, tracked, measured = await self.scan()
        summary = measured.analysis_input
        summary["profiles"][0]["profileName"] = "fake"
        self.assertEqual(measured.analysis_input["profiles"][0]["profileName"], "python-security")
        with self.assertRaises(TypeError):
            measured.source_files["main.py"] = b"changed"
        self.assertNotIn("RuntimeError", repr(measured))
        with self.assertRaises(SecurityServicesError):
            await services.scan(self.execution, tracked)

    async def test_exact_snapshot_read_uses_frozen_bytes_not_working_copy(self):
        services, tracked, measured = await self.scan()
        (self.fixture.root / "source/main.py").write_text("# mutable candidate\n", encoding="utf-8")
        result = await self.read(services, tracked, measured)
        self.assertEqual(result.data["content"].encode(), measured.source_files["main.py"])
        self.assertEqual(result.data["sha256"], sha256(measured.source_files["main.py"]).hexdigest())

    async def test_snapshot_alias_read_grounds_same_source(self):
        services, tracked, measured = await self.scan()
        result = await self.read(services, tracked, measured,
            path=f"snapshots/{self.source.artifact_id}/source/main.py")
        self.assertEqual(result.data["content"].encode(), measured.source_files["main.py"])

    async def test_model_pass_and_confirmed_proposals_cannot_upgrade_without_host_proof(self):
        self.set_scan_report((self.fixture.finding(),))
        services, tracked, measured = await self.scan()
        await self.read(services, tracked, measured)
        report = await self.finish(services, tracked, measured,
            self.decision(measured, outcome="PASS", disposition="CONFIRMED", references=(self.reference,)))
        self.assertEqual({result.outcome for result in report.requirement_results}, {ValidationOutcome.UNVERIFIED})
        self.assertEqual(report.findings[0].disposition, FindingDisposition.SUSPECTED)
        details = json.loads(report.requirement_results[0].details)
        self.assertEqual(details["code"], "SEMANTIC_PROOF_REQUIRED")
        anchor = details["codeReferences"][0]
        self.assertEqual(anchor["fileSha256"], sha256(measured.source_files["main.py"]).hexdigest())
        self.assertIn(f"artifact://{self.source.artifact_id}/source.tar#", anchor["sourceRef"])
        self.assertNotIn("RuntimeError", report.model_dump_json())

    async def test_unread_code_reference_is_rejected(self):
        services, tracked, measured = await self.scan()
        with self.assertRaises(SecurityServicesError) as raised:
            await self.finish(services, tracked, measured,
                self.decision(measured, outcome="PASS", references=(self.reference,)))
        self.assertEqual(raised.exception.code, "SECURITY_CODE_REFERENCE_INVALID")

    async def test_out_of_range_code_reference_is_rejected(self):
        services, tracked, measured = await self.scan()
        await self.read(services, tracked, measured)
        reference = SecurityCodeReference(path="main.py", start_line=2, end_line=2)
        with self.assertRaises(SecurityServicesError) as raised:
            await self.finish(services, tracked, measured,
                self.decision(measured, outcome="PASS", references=(reference,)))
        self.assertEqual(raised.exception.code, "SECURITY_CODE_REFERENCE_INVALID")

    async def test_independent_host_proof_can_admit_grounded_pass_and_confirmed(self):
        self.set_scan_report((self.fixture.finding(),))
        def verifier(execution, decision, measured):
            self.assertEqual(execution.configuration, self.fixture.run_configuration)
            self.assertIn(b"Do not execute", measured.source_files["main.py"])
            return self.proof(measured, outcomes=tuple((identifier, "PASS") for identifier in self.ids),
                dispositions=tuple((identifier, "CONFIRMED") for identifier in measured.finding_ids))
        services, tracked, measured = await self.scan(self.services(proof_verifier=verifier))
        await self.read(services, tracked, measured)
        report = await self.finish(services, tracked, measured,
            self.decision(measured, outcome="PASS", disposition="CONFIRMED", references=(self.reference,)))
        self.assertEqual({result.outcome for result in report.requirement_results}, {ValidationOutcome.PASS})
        self.assertTrue(all(result.tool_evidence.outcome.value == "PASS" for result in report.requirement_results))
        self.assertEqual(report.findings[0].disposition, FindingDisposition.CONFIRMED)
        self.assertIsNone(self.repository.get_run(self.metadata.run_id).verdict)

    async def test_host_semantic_failure_remains_measured_tool_pass(self):
        services = self.services(proof_verifier=lambda _execution, _decision, measured:
            self.proof(measured, outcomes=tuple((identifier, "FAIL") for identifier in self.ids)))
        services, tracked, measured = await self.scan(services)
        await self.read(services, tracked, measured)
        report = await self.finish(services, tracked, measured,
            self.decision(measured, outcome="FAIL", references=(self.reference,)))
        self.assertEqual({result.outcome for result in report.requirement_results}, {ValidationOutcome.FAIL})
        self.assertTrue(all(result.tool_evidence.outcome.value == "PASS" for result in report.requirement_results))

    async def test_false_positive_requires_independent_grounded_host_proof(self):
        self.set_scan_report((self.fixture.finding(),))
        services = self.services(proof_verifier=lambda _execution, _decision, measured:
            self.proof(measured, dispositions=tuple((identifier, "FALSE_POSITIVE") for identifier in measured.finding_ids)))
        services, tracked, measured = await self.scan(services)
        await self.read(services, tracked, measured)
        report = await self.finish(services, tracked, measured,
            self.decision(measured, disposition="FALSE_POSITIVE", references=(self.reference,)))
        self.assertEqual(report.findings[0].disposition, FindingDisposition.FALSE_POSITIVE)

    async def test_unrelated_read_anchor_cannot_confirm_real_scanner_location(self):
        self.set_scan_report((self.fixture.finding(),))
        services = self.services(proof_verifier=lambda _execution, _decision, measured:
            self.proof(measured, dispositions=tuple((identifier, "CONFIRMED") for identifier in measured.finding_ids)))
        services, tracked, measured = await self.scan(services)
        await self.read(services, tracked, measured, path="source/requirements.lock")
        unrelated = SecurityCodeReference(path="requirements.lock", start_line=1, end_line=1)
        with self.assertRaises(SecurityServicesError) as raised:
            await self.finish(services, tracked, measured,
                self.decision(measured, disposition="CONFIRMED", references=(unrelated,)))
        self.assertEqual(raised.exception.code, "SECURITY_PROOF_INVALID")

    async def test_verifier_budget_and_timeout_codes_remain_distinct(self):
        for code in (LLMErrorCode.BUDGET, LLMErrorCode.TIMEOUT):
            def verifier(_execution, _decision, _measured):
                raise LLMRuntimeError(code)
            services, tracked, measured = await self.scan(self.services(proof_verifier=verifier))
            with self.subTest(code=code), self.assertRaises(LLMRuntimeError) as raised:
                await self.finish(services, tracked, measured)
            self.assertEqual(raised.exception.code, code)

    async def test_all_approved_profiles_scanned_and_all_candidates_retained(self):
        other = replace(self.fixture.scanner_profile, name="second-security")
        self.configuration = replace(self.configuration, security_scan_configuration=replace(
            self.configuration.security_scan_configuration, profiles=(self.fixture.scanner_profile, other)))
        def report_for_profile(container):
            from pathlib import Path
            inputs = next(item for item in container["Mounts"] if item["Destination"] == "/inputs")
            host = json.loads((Path(inputs["Source"]) / "_security_host.json").read_text())
            self.docker.exit_code = 1
            self.docker.start_result = CLIResult(returncode=1, stderr=b"", stdout=self.fixture.report_json(
                profileName=host["profile_name"], findings=[self.fixture.finding()]).encode())
        self.docker.start_hook = report_for_profile
        services, tracked, measured = await self.scan()
        self.assertEqual(len(measured.scans), 2)
        self.assertEqual(len(measured.finding_ids), 2)
        self.assertEqual(len(set(measured.finding_ids)), 2)
        self.assertEqual(self.execution.budget.tool_calls, 2)
        report = await self.finish(services, tracked, measured)
        self.assertEqual(len(report.findings), 2)
        self.assertTrue(all(finding.disposition is FindingDisposition.SUSPECTED for finding in report.findings))

    async def test_wrong_proof_source_or_requirement_scope_is_rejected(self):
        for changed in ({"source_artifact_id": uuid4()}, {"snapshot_sha256": "e" * 64},
                        {"requirement_outcomes": ((uuid4(), "PASS"),)}):
            def verifier(execution, decision, measured):
                values = dict(run_id=self.metadata.run_id, workflow_step_id=self.metadata.workflow_step_id,
                    source_artifact_id=self.source.artifact_id, snapshot_sha256=self.source.snapshot_sha256)
                return SecuritySemanticProof(**{**values, **changed})
            services, tracked, measured = await self.scan(self.services(proof_verifier=verifier))
            with self.subTest(changed=changed), self.assertRaises(SecurityServicesError) as raised:
                await self.finish(services, tracked, measured)
            self.assertEqual(raised.exception.code, "SECURITY_PROOF_INVALID")
        for invalid_outcomes in ([(self.ids[0], "PASS")],
                                 ((self.ids[0], "PASS"), (self.ids[0], "FAIL"))):
            def mutated_verifier(execution, decision, measured):
                proof = self.proof(measured)
                object.__setattr__(proof, "requirement_outcomes", invalid_outcomes)
                return proof
            services, tracked, measured = await self.scan(self.services(proof_verifier=mutated_verifier))
            with self.subTest(invalid_outcomes=invalid_outcomes), self.assertRaises(SecurityServicesError) as raised:
                await self.finish(services, tracked, measured)
            self.assertEqual(raised.exception.code, "SECURITY_PROOF_INVALID")

    async def test_proof_cannot_upgrade_unverified_model_proposal(self):
        services = self.services(proof_verifier=lambda _execution, _decision, measured:
            self.proof(measured, outcomes=tuple((identifier, "PASS") for identifier in self.ids)))
        services, tracked, measured = await self.scan(services)
        with self.assertRaises(SecurityServicesError) as raised:
            await self.finish(services, tracked, measured)
        self.assertEqual(raised.exception.code, "SECURITY_PROOF_INVALID")

    async def test_measured_bundle_from_another_services_instance_is_rejected(self):
        services, tracked, measured = await self.scan()
        other = self.services()
        with self.assertRaises(SecurityServicesError):
            await self.finish(other, tracked, measured)

    async def test_missing_scan_receipt_is_not_zero_warning_report(self):
        services = self.services()
        with patch.object(services._scans, "get", side_effect=ValueError("raw private source")):
            with self.assertRaises(SecurityServicesError) as raised:
                await self.scan(services)
        self.assertEqual(raised.exception.code, "SECURITY_SCAN_EVIDENCE_INVALID")
        self.assertNotIn("raw private source", str(raised.exception))

    async def test_scanner_failure_does_not_publish_or_retry_product_result(self):
        self.docker.exit_code = 2
        self.docker.start_result = CLIResult(returncode=2, stderr=b"", stdout=b'{"error":"SCANNER_ERROR"}')
        services = self.services()
        with self.assertRaises(TrackedMCPError):
            await self.scan(services)
        self.assertEqual(len(self.docker.commands("start")), 1)

    async def test_cancel_during_scan_drains_container_cleanup(self):
        self.docker.block_start = True
        task = asyncio.create_task(self.scan())
        await asyncio.wait_for(self.docker.start_entered.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_scan_honors_shared_tool_budget(self):
        self.execution.budget = ExecutionBudget(runtime_budget_ms=10000, limits=LLMLimits(max_tool_calls=0))
        with self.assertRaises(LLMRuntimeError):
            await self.scan()
        self.assertEqual(self.docker.calls, [])

    async def test_clean_continuation_attempt_is_independent_of_fix(self):
        self.step = type(self.step).model_validate({**self.step.model_dump(), "attempt": 1})
        self.fixture.mutate_step(self.step)
        self.metadata = type(self.metadata).model_validate({**self.metadata.model_dump(), "attempt": 1})
        self.execution.metadata = self.metadata
        services, tracked, measured = await self.scan()
        report = await self.finish(services, tracked, measured)
        self.assertEqual(report.code_version, 1)

    async def test_aborted_and_revalidating_runs_are_rejected(self):
        for state in (WorkflowStatus.ABORTED, WorkflowStatus.REVALIDATING):
            self.fixture.mutate_run(status=state, termination_reason="USER_CANCELLED" if state is WorkflowStatus.ABORTED else None)
            with self.subTest(state=state), self.assertRaises(SecurityServicesError):
                await self.services().prepare(self.execution)
        self.assertEqual(self.docker.calls, [])

    async def test_profile_reference_and_image_match_frozen_run(self):
        original = self.configuration
        for profile in (replace(self.fixture.scanner_profile, profile_ref="https://scanner.example.invalid/other/v1"),):
            self.configuration = replace(original, security_scan_configuration=replace(
                original.security_scan_configuration, profiles=(profile,)))
            with self.assertRaises(SecurityServicesError):
                await self.services().prepare(self.execution)
        self.configuration = replace(original, security_scan_configuration=replace(
            original.security_scan_configuration, image_reference="sha256:" + "e" * 64))
        with self.assertRaises(SecurityServicesError):
            await self.services().prepare(self.execution)
        self.assertEqual(self.docker.calls, [])

    async def test_proof_type_bounded_and_no_model_fields(self):
        values = dict(run_id=self.metadata.run_id, workflow_step_id=self.metadata.workflow_step_id,
            source_artifact_id=self.source.artifact_id, snapshot_sha256=self.source.snapshot_sha256)
        for changed in ({"run_id": uuid1()}, {"snapshot_sha256": "sha256:" + "a" * 64},
                        {"requirement_outcomes": ((self.ids[0], "PASS"), (self.ids[0], "FAIL"))},
                        {"finding_dispositions": (("warning", "SAFE"),)}):
            with self.subTest(changed=changed), self.assertRaises(SecurityServicesError):
                SecuritySemanticProof(**{**values, **changed})
        proof = SecuritySemanticProof(**values)
        self.assertNotIn(str(self.metadata.run_id), repr(proof))


if __name__ == "__main__":
    unittest.main()
