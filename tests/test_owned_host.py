"""Owned Host factory admission/cleanup, private Git + fake providers only.

No listener, Docker, MCP child or external LLM/A2A request is started. ASGI
submission checks are in-process and reject before scheduling a workflow.
"""

from copy import deepcopy
import asyncio
from dataclasses import FrozenInstanceError
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx

from agents.platform.composition import OwnedAgentPlatform, PlatformPreparationError
from agents.platform import host as host_module
from agents.platform.host import (
    HostFactoryError, create_host, inspect_host_configuration,
)
from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder
from orchestrator.domain import (
    AgentRole, SCN_001_ID, WorkflowRun, WorkflowStep,
)
from orchestrator.domain.run_configuration import (
    ExecutionBaseline, ExecutionLimits, RunConfiguration, RunConfigurationArtifact,
)
from orchestrator.domain.workspaces import WorkspaceRecord
from test_runtime_configuration import configuration_data as runtime_data, fake_environment
from test_tool_configuration import (
    IMAGE, LOCK, LOCK_HASH, SUITE_REF, configuration_data as tools_data,
    protected_browser, protected_unit,
)


_CONFIG_INVALID = "OWNED_HOST_CONFIGURATION_INVALID"
_INIT_FAILED = "OWNED_HOST_INITIALIZATION_FAILED"
_SECRET = "private-host-fixture-secret-never-a-real-key"


class _Provider:
    name = "openai"

    def __init__(self, settings):
        self.role = settings.role
        self.validated = []
        self.closed = 0
        self.completed = 0

    def validate_configuration(self, model, output):
        self.validated.append((model, output))

    async def complete(self, request):
        self.completed += 1
        raise AssertionError("Host unit tests never invoke an LLM")

    async def aclose(self):
        self.closed += 1


class _HostFixture:
    def setUp(self):
        clean = patch.dict(os.environ, {}, clear=True)
        clean.start()
        self.addCleanup(clean.stop)
        temporary = TemporaryDirectory(prefix="a2a-owned-host-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.base = self.directory / "config"
        self.base.mkdir()
        self.runtime_file = self.base / "owned-runtime.json"
        self.tools_file = self.base / "owned-tools.json"
        self.source_file = self.base / "owned-source.json"
        self.cases_file = self.base / "owned-qa-cases.json"
        self.lock_file = self.directory / "templates" / "requirements.lock"
        self.lock_file.parent.mkdir()
        self.lock_file.write_bytes(LOCK)
        self.seed = self.directory / "reference-repository"
        self.seed.mkdir()
        (self.seed / "requirements.lock").write_bytes(LOCK)
        (self.seed / "signup.py").write_bytes(b"# inert approved starting Source\n")
        self.git("init", "-q", "--template=")
        self.git("add", "--all")
        self.git("commit", "-qm", "owned Host inert fixture")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.snapshot = GitSnapshotBuilder(self.seed).build(self.commit, "requirements.lock")
        self.runtime = runtime_data()
        self.tools = tools_data()
        self.source = {
            "schemaVersion": 1, "repositoryPath": "../reference-repository",
            "repositoryId": "owned-host-inert-fixture", "commitHash": self.commit,
            "snapshotSha256": self.snapshot.snapshot_sha256,
            "lockPath": "requirements.lock", "gitTimeoutSeconds": 30,
        }
        self.cases = {"schemaVersion": 1, "cases": []}
        self.environment = fake_environment()
        self.providers = []
        self.write()

    def git(self, *arguments):
        result = subprocess.run([
            "git", "-c", "user.name=Owned Host Fixture", "-c", "user.email=host@example.invalid",
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *arguments,
        ], cwd=self.seed, env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        return result.stdout

    def write(self):
        for path, data in ((self.runtime_file, self.runtime), (self.tools_file, self.tools),
                           (self.source_file, self.source), (self.cases_file, self.cases)):
            path.write_text(json.dumps(data), encoding="utf-8")

    def arguments(self, **overrides):
        result = {"runtime_file": self.runtime_file, "tools_file": self.tools_file,
                  "source_file": self.source_file}
        result.update(overrides)
        return result

    def inspect(self, **overrides):
        return inspect_host_configuration(**self.arguments(**overrides))

    def provider_factory(self, settings):
        provider = _Provider(settings)
        self.providers.append(provider)
        return provider

    async def host(self, **overrides):
        values = {"environ": self.environment, "provider_factory": self.provider_factory,
                  "mcp_client_factory": Mock(side_effect=AssertionError("No MCP child during Host creation")),
                  "a2a_client_factory": Mock(side_effect=AssertionError("No A2A call during Host creation"))}
        values.update(overrides)
        return await create_host(**self.arguments(**values))

    def assert_safe(self, error, expected=_CONFIG_INVALID):
        self.assertIs(type(error), HostFactoryError)
        self.assertEqual(error.code, expected)
        self.assertEqual(str(error), expected)
        self.assertNotIn(_SECRET, str(error))
        self.assertNotIn(str(self.directory), str(error))

    def protected(self, *, browser=False):
        self.tools["unit"]["scopes"].append(protected_unit())
        self.cases["cases"].append({"toolName": "run_unit_tests", "selector": "protected-unit",
            "testId": "protected.signup", "requirementId": str(uuid4()), "title": "Protected signup fixture",
            "expectedResult": "Approved signup criterion"})
        if browser:
            self.tools["browser"]["suites"].append(protected_browser())
            self.cases["cases"].append({"toolName": "run_browser_tests", "selector": "protected-browser",
                "testId": "signup.visible", "requirementId": str(uuid4()), "title": "Protected UI fixture",
                "expectedResult": "Signup form visible"})
        self.write()


class OwnedHostInspectionTests(_HostFixture, unittest.TestCase):
    def test_inspect_validates_three_inputs_and_exact_baseline_without_runtime_side_effects(self):
        before = {path.relative_to(self.seed): path.read_bytes() for path in self.seed.rglob("*") if path.is_file()}
        with patch("agents.platform.host.SQLiteWorkflowRepository", side_effect=AssertionError("No DB during inspection")), patch("agents.platform.host.provider_from_settings", side_effect=AssertionError("No provider during inspection")):
            plan = self.inspect()
        approved = plan.approved_configuration
        self.assertIs(type(approved), RunConfiguration)
        self.assertEqual(approved.starting_commit_hash, self.commit)
        self.assertEqual(approved.starting_snapshot_sha256, self.snapshot.snapshot_sha256)
        self.assertEqual(approved.environment.container_image_digest, IMAGE)
        self.assertEqual(approved.environment.dependency_lock_hash, LOCK_HASH)
        self.assertEqual(approved.limits.runtime_budget_ms, self.runtime["runtimeBudgetMs"])
        self.assertEqual(approved.model.model_id, self.runtime["model"]["modelId"])
        self.assertIsNone(approved.protected_test_suite_ref)
        self.assertEqual(approved.scanner_profile_ref, self.tools["security"]["profiles"][0]["profile_ref"])
        self.assertFalse((self.directory / ".data").exists())
        self.assertEqual(before, {path.relative_to(self.seed): path.read_bytes() for path in self.seed.rglob("*") if path.is_file()})

    def test_inspection_needs_no_credentials_and_plan_is_private_frozen_with_fresh_configuration(self):
        self.assertEqual(dict(os.environ), {})
        plan = self.inspect()
        self.assertIsNot(plan.approved_configuration, plan.approved_configuration)
        self.assertNotIn(str(self.directory), repr(plan))
        with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
            plan.approved_configuration = RunConfiguration()

    def test_policy_fingerprint_is_stable_canonical_and_not_a_credential(self):
        approved = self.inspect().policy_sha256
        self.assertRegex(approved, r"^[0-9a-f]{64}$")
        for path in (self.runtime_file, self.tools_file, self.source_file):
            data = json.loads(path.read_text(encoding="utf-8"))
            path.write_text(json.dumps(dict(reversed(tuple(data.items()))), indent=2), encoding="utf-8")
        self.assertEqual(self.inspect().policy_sha256, approved)
        with patch.dict(os.environ, self.environment):
            self.assertEqual(self.inspect().policy_sha256, approved)
        self.assertFalse((self.directory / ".data").exists())

    def test_execution_policies_not_expressed_by_image_baseline_change_the_fingerprint(self):
        approved = self.inspect().policy_sha256
        original_tools, original_runtime, original_source = deepcopy(self.tools), deepcopy(self.runtime), deepcopy(self.source)
        changes = (
            lambda: self.tools["build"]["profile"]["argv"].append("--inert-approved-option"),
            lambda: self.tools["unit"]["scopes"].append({"name": "additional-source", "kind": "SNAPSHOT"}),
            lambda: self.tools["browser"]["service_argv"].append("--inert-approved-option"),
            lambda: self.tools["security"]["profiles"][0]["rule_ids"].append("B102"),
            lambda: self.runtime["llmLimits"].update(max_model_calls=7),
            lambda: self.runtime.update(runtimeBudgetMs=299_000),
            lambda: self.source.update(repositoryId="second-owned-reference"),
            lambda: self.source.update(gitTimeoutSeconds=29),
        )
        for index, change in enumerate(changes):
            self.tools, self.runtime, self.source = deepcopy(original_tools), deepcopy(original_runtime), deepcopy(original_source)
            change()
            self.write()
            with self.subTest(policy=index):
                self.assertNotEqual(self.inspect().policy_sha256, approved)
        self.tools, self.runtime, self.source = original_tools, original_runtime, original_source
        self.write()

    def test_protected_case_binding_is_fingerprinted_and_unapproved_verifier_is_rejected(self):
        self.protected()
        approved = self.inspect(protected_cases_file=self.cases_file).policy_sha256
        self.cases["cases"][0]["expectedResult"] = "Another approved protected expectation"
        self.write()
        self.assertNotEqual(self.inspect(protected_cases_file=self.cases_file).policy_sha256, approved)
        with self.assertRaises(HostFactoryError):
            self.inspect(protected_cases_file=self.cases_file, proof_verifier=lambda evidence: False)

    def test_privileged_model_copy_cannot_use_equal_but_noncanonical_primitives(self):
        plan = self.inspect()
        approved = plan.approved_configuration
        variants = (
            approved.model_copy(update={"model": approved.model.model_copy(update={"temperature": False})}),
            approved.model_copy(update={"limits": approved.limits.model_copy(update={"max_fix_attempts": 3.0})}),
            approved.model_copy(update={"limits": approved.limits.model_copy(update={"max_tool_retries": 2.0})}),
            approved.model_copy(update={"limits": approved.limits.model_copy(update={"runtime_budget_ms": float(approved.limits.runtime_budget_ms)})}),
        )
        for index, configuration in enumerate(variants):
            with self.subTest(forgery=index):
                with self.assertRaises(HostFactoryError) as caught:
                    plan.validate_configuration(configuration)
                self.assert_safe(caught.exception)

    def test_final_only_and_in_loop_arms_both_accept_a_declared_experiment(self):
        # Ver2 (no in-loop protected tests) and Ver1 (protected tests) declare the same
        # experiment; Ver2 keeps no protected reference and no protected selector.
        final_only = self.inspect()
        self.protected()
        in_loop = self.inspect(protected_cases_file=self.cases_file)
        experiment = {"experimentId": str(uuid4()), "warmupState": "COLD", "executionOrder": 0}
        for name, plan in (("final_only", final_only), ("in_loop", in_loop)):
            with self.subTest(arm=name):
                submitted = RunConfiguration.model_validate(
                    {**plan.approved_configuration.model_dump(mode="json", by_alias=True), **experiment})
                plan.validate_configuration(submitted)
        self.assertIsNone(final_only.approved_configuration.protected_test_suite_ref)
        self.assertEqual(in_loop.approved_configuration.protected_test_suite_ref, SUITE_REF)
        self.assertFalse(any(scope.kind == "PROTECTED" for scope in final_only.tools.unit_configuration_for(AgentRole.QA).scopes))

    def test_tool_and_runtime_timeout_must_match_exactly(self):
        self.runtime["llmLimits"]["tool_timeout_seconds"] = 59
        self.write()
        with self.assertRaises(HostFactoryError) as caught:
            self.inspect()
        self.assert_safe(caught.exception)
        self.assertFalse((self.directory / ".data").exists())

    def test_invalid_source_commit_snapshot_and_actual_lock_hash_are_rejected(self):
        for field, value in (("commitHash", "f" * 40), ("snapshotSha256", "f" * 64), ("lockPath", "missing.lock")):
            original = deepcopy(self.source)
            self.source[field] = value
            self.write()
            with self.subTest(field=field), self.assertRaises(HostFactoryError) as caught:
                self.inspect()
            self.assert_safe(caught.exception)
            self.source = original
        self.write()
        self.lock_file.write_bytes(b"not approved LOCK\n")
        with self.assertRaises(HostFactoryError) as caught:
            self.inspect()
        self.assert_safe(caught.exception)

    def test_multiple_scanner_profile_references_are_rejected(self):
        duplicate = deepcopy(self.tools["security"]["profiles"][0])
        duplicate["name"] = "second-scanner"
        duplicate["profile_ref"] = "https://criteria.example.invalid/security/different/v1"
        self.tools["security"]["profiles"].append(duplicate)
        self.write()
        with self.assertRaises(HostFactoryError) as caught:
            self.inspect()
        self.assert_safe(caught.exception)

    def test_multiple_profiles_can_share_one_approved_reference(self):
        duplicate = deepcopy(self.tools["security"]["profiles"][0])
        duplicate["name"] = "second-scanner"
        self.tools["security"]["profiles"].append(duplicate)
        self.write()
        self.assertEqual(self.inspect().approved_configuration.scanner_profile_ref, duplicate["profile_ref"])

    def test_protected_selectors_require_cases_and_one_suite_reference(self):
        self.protected(browser=True)
        with self.assertRaises(HostFactoryError) as caught:
            self.inspect()
        self.assert_safe(caught.exception)
        plan = self.inspect(protected_cases_file=self.cases_file)
        self.assertEqual(plan.approved_configuration.protected_test_suite_ref, SUITE_REF)
        self.tools["browser"]["suites"][-1]["protected_suite_ref"] = "https://criteria.example.invalid/different/v1"
        self.write()
        with self.assertRaises(HostFactoryError) as caught:
            self.inspect(protected_cases_file=self.cases_file)
        self.assert_safe(caught.exception)

    def test_protected_case_coverage_is_exact_no_missing_or_unapproved_selector(self):
        self.protected(browser=True)
        original = deepcopy(self.cases)
        for cases in ([], self.cases["cases"][:1], self.cases["cases"] + [{**self.cases["cases"][0], "selector": "unapproved-scope", "testId": "different"}]):
            self.cases = {"schemaVersion": 1, "cases": cases}
            self.write()
            with self.subTest(case_count=len(cases)), self.assertRaises(HostFactoryError) as caught:
                self.inspect(protected_cases_file=self.cases_file)
            self.assert_safe(caught.exception)
        self.cases = original

    def test_duplicate_protected_case_identity_is_rejected(self):
        self.protected()
        self.cases["cases"].append(deepcopy(self.cases["cases"][0]))
        self.write()
        with self.assertRaises(HostFactoryError) as caught:
            self.inspect(protected_cases_file=self.cases_file)
        self.assert_safe(caught.exception)

    def test_protected_case_file_alias_only_closed_schema_and_uuid4(self):
        self.protected()
        original = deepcopy(self.cases)
        for changed in (
            {**original, "unapproved": _SECRET},
            {**original, "schemaVersion": True},
            {"schemaVersion": 1, "cases": [{**original["cases"][0], "requirementId": "bad-id"}]},
            {"schemaVersion": 1, "cases": [{**original["cases"][0], "tool_name": "run_unit_tests"}]},
            {"schemaVersion": 1, "cases": [{key: value for key, value in original["cases"][0].items() if key != "title"}]},
        ):
            self.cases = changed
            self.write()
            with self.subTest(keys=tuple(changed)), self.assertRaises(HostFactoryError) as caught:
                self.inspect(protected_cases_file=self.cases_file)
            self.assert_safe(caught.exception)

    def test_all_inputs_and_host_lock_must_stay_outside_agent_workspace(self):
        workspace = self.directory / ".data" / "workspaces"
        workspace.mkdir(parents=True)
        for field, original in (("tools_file", self.tools_file), ("source_file", self.source_file), ("protected_cases_file", self.cases_file)):
            inside = workspace / original.name
            inside.write_bytes(original.read_bytes())
            with self.subTest(field=field), self.assertRaises(HostFactoryError) as caught:
                self.inspect(**{field: inside})
            self.assert_safe(caught.exception)
        lock = workspace / "requirements.lock"
        lock.write_bytes(LOCK)
        self.tools["dependencyLockFile"] = str(lock)
        self.write()
        with self.assertRaises(HostFactoryError) as caught:
            self.inspect()
        self.assert_safe(caught.exception)

    def test_config_inputs_cannot_be_workflow_or_agent_database(self):
        for role in (None, *AgentRole):
            original = deepcopy(self.runtime)
            target = self.runtime["orchestrator"] if role is None else self.runtime["agents"][role.value]
            target["databasePath"] = str(self.tools_file)
            self.write()
            before = self.tools_file.read_bytes()
            with self.subTest(role=role), self.assertRaises(HostFactoryError) as caught:
                self.inspect()
            self.assert_safe(caught.exception)
            self.assertEqual(self.tools_file.read_bytes(), before)
            self.runtime = original
        self.write()

    def test_config_input_cannot_be_database_sidecar(self):
        database = self.base / "reserved.sqlite3"
        self.runtime["orchestrator"]["databasePath"] = str(database)
        self.write()
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(database) + suffix)
            sidecar.write_bytes(self.tools_file.read_bytes())
            with self.subTest(suffix=suffix), self.assertRaises(HostFactoryError) as caught:
                self.inspect(tools_file=sidecar)
            self.assert_safe(caught.exception)
            self.assertFalse(database.exists())

    def test_starting_repository_cannot_overlap_database_or_workspace_roots(self):
        for target in (str(self.seed / "host.sqlite3"), str(self.seed / ".git" / "host.sqlite3")):
            self.runtime["orchestrator"]["databasePath"] = target
            self.write()
            with self.subTest(target=target), self.assertRaises(HostFactoryError) as caught:
                self.inspect()
            self.assert_safe(caught.exception)
        self.runtime = runtime_data()
        self.runtime["workspaceRoot"] = str(self.seed)
        self.write()
        with self.assertRaises(HostFactoryError) as caught:
            self.inspect()
        self.assert_safe(caught.exception)


class OwnedHostCreationTests(_HostFixture, unittest.IsolatedAsyncioTestCase):
    def claimed_run(self, platform):
        repository = platform.orchestrator_app.state.workflow_repository
        registry = platform.orchestrator_app.state.workspace_registry
        run = WorkflowRun(scenario_id=SCN_001_ID, request_text="Inert approved Host Run")
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER)
        frozen = RunConfigurationArtifact(run_id=run.run_id, scenario_id=run.scenario_id,
            workspace_id=run.workspace_id, configuration=self.inspect().approved_configuration)
        workspace = WorkspaceRecord(workspace_id=run.workspace_id, run_id=run.run_id,
            root_path=str(registry.base_path / str(run.workspace_id)))
        repository.create_run(run, (step,), (), run_configuration=frozen, workspace=workspace)
        repository.claim_planner_dispatch(run.run_id)
        return run, frozen, Path(workspace.root_path)

    async def test_create_host_compose_exact_existing_platform_shared_storage_no_execution(self):
        platform = await self.host()
        self.addAsyncCleanup(platform.aclose)
        self.assertIs(type(platform), OwnedAgentPlatform)
        self.assertEqual(set(platform.agent_apps), set(AgentRole))
        self.assertEqual(len(platform.endpoints), 5)
        self.assertEqual([provider.role for provider in self.providers], list(AgentRole))
        self.assertTrue(all(provider.completed == 0 for provider in self.providers))
        repository = platform.orchestrator_app.state.workflow_repository
        registry = platform.orchestrator_app.state.workspace_registry
        self.assertIs(registry._repository, repository)
        self.assertEqual(repository.database_path, self.directory / ".data" / "orchestrator.sqlite3")
        self.assertFalse(registry.base_path.exists())
        for role in AgentRole:
            self.assertFalse((self.directory / ".data" / "agents" / (role.value.lower() + ".sqlite3")).exists())

    async def test_exact_approved_configuration_is_accepted_and_provider_close_once(self):
        configuration = self.inspect().approved_configuration
        platform = await self.host()
        self.assertIsNone(platform.orchestrator_app.state.submission_validator(configuration))
        self.assertTrue(all(provider.completed == 0 for provider in self.providers))
        await platform.aclose()
        await platform.aclose()
        self.assertEqual([provider.closed for provider in self.providers], [1, 1, 1, 1])

    async def test_claimed_first_run_prepares_exact_source_and_pins_policy_without_any_agent_call(self):
        before = {path.relative_to(self.seed): path.read_bytes() for path in self.seed.rglob("*") if path.is_file()}
        platform = await self.host()
        self.addAsyncCleanup(platform.aclose)
        run, frozen, root = self.claimed_run(platform)
        self.assertFalse(root.exists())
        await platform.dispatcher._before_dispatch(run.run_id)
        seeded = GitSnapshotBuilder(root / "source").build(self.commit, "requirements.lock")
        self.assertEqual(seeded, self.snapshot)
        self.assertEqual((root / "source" / "signup.py").read_bytes(), b"# inert approved starting Source\n")
        self.assertEqual((root / "source" / ".git" / "HEAD").read_text(encoding="ascii").strip(), self.commit)
        receipt = json.loads((root / ".source-preparation.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "DONE")
        self.assertEqual(receipt["runId"], str(run.run_id))
        self.assertEqual(receipt["snapshotSha256"], self.snapshot.snapshot_sha256)
        budget = platform.budgets.resolve(frozen)
        deadline = budget.deadline_monotonic
        await platform.dispatcher._before_dispatch(run.run_id)
        self.assertIs(platform.budgets.resolve(frozen), budget)
        self.assertEqual(platform.budgets.resolve(frozen).deadline_monotonic, deadline)
        self.assertEqual(before, {path.relative_to(self.seed): path.read_bytes() for path in self.seed.rglob("*") if path.is_file()})
        self.assertTrue(all(provider.completed == 0 for provider in self.providers))
        events, _ = platform.orchestrator_app.state.workflow_repository.list_events(run.run_id, limit=1000, offset=0)
        self.assertFalse(any(event.event_type == "A2A_MESSAGE_SENT" for event in events))

    async def test_restarted_host_cannot_change_unrecorded_build_policy_for_already_pinned_run(self):
        first = await self.host()
        self.addAsyncCleanup(first.aclose)
        run, _frozen, root = self.claimed_run(first)
        await first.dispatcher._before_dispatch(run.run_id)
        before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        await first.aclose()
        self.tools["build"]["profile"]["argv"].append("--different-private-host-policy")
        self.write()
        second = await self.host()
        self.addAsyncCleanup(second.aclose)
        self.assertNotEqual(first.orchestrator_app.state.host_policy_sha256, second.orchestrator_app.state.host_policy_sha256)
        with self.assertRaises(PlatformPreparationError) as caught:
            await second.dispatcher._before_dispatch(run.run_id)
        self.assertEqual(str(caught.exception), "OWNED_AGENT_PREPARATION_FAILED")
        self.assertEqual(before, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})
        self.assertTrue(all(provider.completed == 0 for provider in self.providers))

    async def test_submission_rejects_model_budget_source_environment_and_profile_switches(self):
        approved = self.inspect().approved_configuration
        platform = await self.host()
        self.addAsyncCleanup(platform.aclose)
        variants = (
            approved.model_copy(update={"model": approved.model.model_copy(update={"model_id": "unapproved-model"})}),
            approved.model_copy(update={"limits": ExecutionLimits(runtime_budget_ms=approved.limits.runtime_budget_ms + 1)}),
            approved.model_copy(update={"starting_commit_hash": "f" * 40}),
            approved.model_copy(update={"starting_snapshot_sha256": "f" * 64}),
            approved.model_copy(update={"scanner_profile_ref": "https://criteria.example.invalid/unapproved"}),
            approved.model_copy(update={"protected_test_suite_ref": SUITE_REF}),
            approved.model_copy(update={"environment": approved.environment.model_copy(update={"container_image_digest": "sha256:" + "f" * 64})}),
            approved.model_copy(update={"environment": approved.environment.model_copy(update={"dependency_lock_hash": "sha256:" + "f" * 64})}),
            approved.model_copy(update={"environment": approved.environment.model_copy(update={"hardware_profile": "unapproved-host"})}),
            approved.model_copy(update={"environment": ExecutionBaseline(container_image_digest=IMAGE, dependency_lock_hash=LOCK_HASH, hardware_profile=approved.environment.hardware_profile, network_policy="ALLOWLIST", allowed_hosts=("example.invalid",))}),
            RunConfiguration(),
        )
        for index, changed in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(HostFactoryError) as caught:
                platform.orchestrator_app.state.submission_validator(changed)
            self.assert_safe(caught.exception)
        with platform.orchestrator_app.state.workflow_repository._connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM workflow_runs").fetchone()[0], 0)

    async def test_protected_host_cannot_drop_or_change_approved_suite_on_submission(self):
        self.protected(browser=True)
        plan = self.inspect(protected_cases_file=self.cases_file)
        platform = await self.host(protected_cases_file=self.cases_file)
        self.addAsyncCleanup(platform.aclose)
        approved = plan.approved_configuration
        self.assertIsNone(platform.orchestrator_app.state.submission_validator(approved))
        for reference in (None, "https://criteria.example.invalid/unapproved"):
            with self.subTest(reference=reference), self.assertRaises(HostFactoryError) as caught:
                platform.orchestrator_app.state.submission_validator(approved.model_copy(update={"protected_test_suite_ref": reference}))
            self.assert_safe(caught.exception)

    async def test_bad_submission_returns_422_before_run_persistence_or_dispatch(self):
        platform = await self.host()
        self.addAsyncCleanup(platform.aclose)
        approved = self.inspect().approved_configuration
        changed = approved.model_copy(update={"starting_snapshot_sha256": "f" * 64})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=platform.orchestrator_app), base_url="http://inprocess") as client:
            response = await client.post("/api/v1/runs", json={"scenarioId": str(SCN_001_ID), "requestText": "inert fixture request", "configuration": changed.model_dump(mode="json", by_alias=True)})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(str(self.directory), response.text)
        repository = platform.orchestrator_app.state.workflow_repository
        with repository._connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM workflow_runs").fetchone()[0], 0)
        self.assertFalse(platform.orchestrator_app.state.workspace_registry.base_path.exists())
        self.assertTrue(all(provider.completed == 0 for provider in self.providers))

    async def test_missing_named_credentials_never_fall_back_to_ambient_agent_environment(self):
        with patch.dict(os.environ, {"AGENT_LLM_API_KEY": _SECRET, "AGENT_ROLE": "DEVELOPER"}):
            with self.assertRaises(HostFactoryError) as caught:
                await self.host(environ={})
        self.assertIn(caught.exception.code, {_CONFIG_INVALID, _INIT_FAILED})
        self.assertNotIn(_SECRET, str(caught.exception))
        self.assertEqual(self.providers, [])

    async def test_bad_configuration_fails_before_any_provider_or_database_creation(self):
        self.runtime["llmLimits"]["tool_timeout_seconds"] = 59
        self.write()
        with self.assertRaises(HostFactoryError) as caught:
            await self.host()
        self.assert_safe(caught.exception)
        self.assertEqual(self.providers, [])
        self.assertFalse((self.directory / ".data").exists())

    async def test_partial_provider_creation_failure_closes_created_once_and_preserves_source(self):
        before = {path.relative_to(self.seed): path.read_bytes() for path in self.seed.rglob("*") if path.is_file()}
        def factory(settings):
            if len(self.providers) == 2:
                raise RuntimeError(_SECRET + " provider body " + str(self.directory))
            return self.provider_factory(settings)
        with self.assertRaises(HostFactoryError) as caught:
            await self.host(provider_factory=factory)
        self.assert_safe(caught.exception, _INIT_FAILED)
        self.assertEqual([provider.closed for provider in self.providers], [1, 1])
        self.assertEqual(before, {path.relative_to(self.seed): path.read_bytes() for path in self.seed.rglob("*") if path.is_file()})

    async def test_composition_failure_closes_all_created_providers_once(self):
        with patch("agents.platform.host.create_platform", side_effect=RuntimeError(_SECRET + " composition body")):
            with self.assertRaises(HostFactoryError) as caught:
                await self.host()
        self.assert_safe(caught.exception, _INIT_FAILED)
        self.assertEqual([provider.closed for provider in self.providers], [1, 1, 1, 1])

    async def test_failure_after_composition_uses_transferred_platform_close_owner_once(self):
        platforms = []
        original = host_module.create_platform
        def compose(**kwargs):
            platform = original(**kwargs)
            platforms.append(platform)
            state = platform.orchestrator_app.state
            original_setter = type(state).__setattr__
            def fail_metadata_assignment(selected, name, value):
                if selected is state and name == "host_policy_sha256":
                    raise RuntimeError(_SECRET + " " + str(self.directory))
                return original_setter(selected, name, value)
            setter = patch.object(type(state), "__setattr__", new=fail_metadata_assignment)
            setter.start()
            self.addCleanup(setter.stop)
            return platform
        with patch.object(host_module, "create_platform", side_effect=compose):
            with self.assertRaises(HostFactoryError) as caught:
                await self.host()
        self.assert_safe(caught.exception, _INIT_FAILED)
        self.assertEqual(len(platforms), 1)
        self.assertEqual([provider.closed for provider in self.providers], [1, 1, 1, 1])
        await platforms[0].aclose()
        self.assertEqual([provider.closed for provider in self.providers], [1, 1, 1, 1])

    async def test_failed_provider_close_does_not_skip_other_providers_or_leak(self):
        def factory(settings):
            provider = self.provider_factory(settings)
            if settings.role is AgentRole.PLANNER:
                async def bad_close():
                    provider.closed += 1
                    raise RuntimeError(_SECRET + " close body")
                provider.aclose = bad_close
            return provider
        with patch("agents.platform.host.create_platform", side_effect=RuntimeError("composition failed")):
            with self.assertRaises(HostFactoryError) as caught:
                await self.host(provider_factory=factory)
        self.assert_safe(caught.exception, _INIT_FAILED)
        self.assertEqual([provider.closed for provider in self.providers], [1, 1, 1, 1])

    async def test_async_factory_result_is_rejected_and_coroutine_closed(self):
        results = []
        async def async_provider(settings):
            raise AssertionError("Async provider factory must not run")
        def factory(settings):
            coroutine = async_provider(settings)
            results.append(coroutine)
            return coroutine
        with self.assertRaises(HostFactoryError) as caught:
            await self.host(provider_factory=factory)
        self.assert_safe(caught.exception, _INIT_FAILED)
        self.assertEqual(len(results), 1)
        self.assertEqual(inspect.getcoroutinestate(results[0]), inspect.CORO_CLOSED)

    async def test_canceled_initialization_does_not_abandon_allocated_provider_cleanup(self):
        def factory(settings):
            if len(self.providers) == 2:
                raise asyncio.CancelledError()
            return self.provider_factory(settings)
        with self.assertRaises(asyncio.CancelledError):
            await self.host(provider_factory=factory)
        self.assertEqual([provider.closed for provider in self.providers], [1, 1])

    async def test_repeated_cancellation_waits_for_cleanup_of_every_created_provider(self):
        entered, release = asyncio.Event(), asyncio.Event()
        def factory(settings):
            if len(self.providers) == 2:
                raise asyncio.CancelledError()
            provider = self.provider_factory(settings)
            if settings.role is AgentRole.PLANNER:
                async def slow_close():
                    provider.closed += 1
                    entered.set()
                    await release.wait()
                provider.aclose = slow_close
            return provider
        task = asyncio.create_task(self.host(provider_factory=factory))
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual([provider.closed for provider in self.providers], [1, 1])

    async def test_platform_and_plan_reprs_never_expose_named_credentials(self):
        platform = await self.host()
        self.addAsyncCleanup(platform.aclose)
        text = repr(platform) + repr(platform.endpoints) + repr(self.inspect())
        for secret in self.environment.values():
            self.assertNotIn(secret, text)
        self.assertNotIn(str(self.directory), text)
