"""Step 22 runtime policy with real snapshots and a fake Docker transport.

No container execution is claimed by these tests. Git, SQLite, Workspace
ownership and immutable artifact reads use real isolated temporary fixtures.
"""

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain import (
    AgentContext, AgentRole, SCN_001_ID, SCENARIO_REGISTRY, TraceEvent, WorkflowRun, WorkflowStatus,
    WorkflowStep, WorkflowStepStatus,
)
from orchestrator.domain.run_configuration import (
    ExecutionBaseline, RunConfiguration, RunConfigurationArtifact,
)
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.contracts import (
    CLIResult, ExecutionProfile, SandboxError, SandboxErrorCode, SandboxLimits,
)
from orchestrator.sandbox.runtime import SandboxRuntime
from orchestrator.workspaces.registry import WorkspaceRegistry


IMAGE_ID = "sha256:" + "d" * 64
CONTAINER_ID = "c" * 64


def _json_result(value):
    return CLIResult(returncode=0, stdout=json.dumps(value).encode(), stderr=b"")


class FakeDocker:
    """Deterministic CLI adapter; inspected state reflects requested flags."""

    def __init__(self):
        self.endpoint = "unix:///var/run/docker.sock"
        self.calls = []
        self.context = [{"Name": "default", "Endpoints": {
            "docker": {"Host": "unix:///var/run/docker.sock", "SkipTLSVerify": False},
        }}]
        self.info = {"OSType": "linux", "ServerVersion": "27.5.0",
                     "SecurityOptions": ["name=seccomp,profile=builtin", "name=cgroupns"]}
        self.image = [{"Id": IMAGE_ID, "RepoDigests": [], "Os": "linux",
                       "Architecture": "amd64", "Config": {"Env": [], "Volumes": None}}]
        self.container = None
        self.before_start_mutation = None
        self.after_start_mutation = None
        self.start_result = CLIResult(returncode=0, stdout=b"build output\n", stderr=b"")
        self.exit_code = 0
        self.create_result = None
        self.rm_result = CLIResult(returncode=0, stdout=b"removed\n", stderr=b"")
        self.start_error = None
        self.block_start = False
        self.start_entered = asyncio.Event()
        self.started = False
        self.start_hook = None
        self.foreign_id = None
        self.rm_entered = asyncio.Event()
        self.release_rm = None

    def _create(self, args):
        values = {}
        multiples = {}
        bare = set()
        index = 1
        while index < len(args):
            item = args[index]
            if not item.startswith("--"):
                break
            if "=" in item:
                key, value = item.split("=", 1)
            elif item in {"--read-only", "--init", "--no-healthcheck"}:
                bare.add(item)
                index += 1
                continue
            else:
                key, value = item, args[index + 1]
                index += 1
            values[key] = value
            multiples.setdefault(key, []).append(value)
            index += 1
        image, *argv = args[index:]
        mounts = []
        for mount in multiples.get("--mount", ()):
            attributes = dict((part.split("=", 1) + [True])[:2]
                              for part in mount.split(","))
            mounts.append({"Type": attributes.get("type", "bind"),
                           "Source": attributes.get("source", attributes.get("src")),
                           "Destination": attributes.get("target", attributes.get("dst")),
                           "RW": "readonly" not in attributes and "ro" not in attributes,
                           "Propagation": "rprivate"})
        tmpfs = {}
        for item in multiples.get("--tmpfs", ()):
            destination, _, options = item.partition(":")
            tmpfs[destination] = options
        labels = dict(item.split("=", 1) for item in multiples.get("--label", ()))
        env = dict(item.split("=", 1) for item in self.image[0]["Config"].get("Env", ()))
        env.update(dict(item.split("=", 1) for item in multiples.get("--env", ())))
        self.container = {
            "Id": CONTAINER_ID, "Name": "/" + values.get("--name", "fixture"),
            "Image": self.image[0]["Id"], "Path": values.get("--entrypoint", argv[0] if argv else ""),
            "Args": argv if "--entrypoint" in values else argv[1:],
            "Config": {"Image": image, "User": values.get("--user", ""),
                       "WorkingDir": values.get("--workdir", ""), "Labels": labels,
                       "Env": [f"{key}={value}" for key, value in env.items()], "Entrypoint": [values["--entrypoint"]] if "--entrypoint" in values else None,
                       "Cmd": argv, "Healthcheck": {"Test": ["NONE"]}},
            "HostConfig": {
                "NetworkMode": values.get("--network", "default"),
                "ReadonlyRootfs": "--read-only" in bare,
                "Privileged": False, "CapDrop": multiples.get("--cap-drop", []),
                "CapAdd": [], "SecurityOpt": multiples.get("--security-opt", []),
                "Memory": int(values.get("--memory", "0")),
                "MemorySwap": int(values.get("--memory-swap", "0")),
                "NanoCpus": int(float(values.get("--cpus", "0")) * 1_000_000_000),
                "PidsLimit": int(values.get("--pids-limit", "0")),
                "Tmpfs": tmpfs, "LogConfig": {"Type": values.get("--log-driver", "json-file")},
                "AutoRemove": False, "Devices": [], "DeviceRequests": None,
                "Binds": None, "Init": "--init" in bare,
                "PidMode": "", "IpcMode": values.get("--ipc", "private"),
                "CgroupnsMode": values.get("--cgroupns", ""),
                "RestartPolicy": {"Name": values.get("--restart", "no")},
                "UTSMode": "", "UsernsMode": "", "ExtraHosts": None,
                "Dns": [], "PortBindings": {}, "PublishAllPorts": False,
            },
            "Mounts": mounts,
            "State": {"Status": "created", "Running": False, "ExitCode": 0,
                      "OOMKilled": False, "Error": "", "Dead": False},
        }

    async def run(self, args, *, timeout_seconds, max_stdout_bytes, max_stderr_bytes):
        args = tuple(args)
        self.calls.append((args, timeout_seconds, max_stdout_bytes, max_stderr_bytes))
        if args[0] == "container":
            args = args[1:]
        if args[:2] == ("context", "inspect"):
            return _json_result(self.context)
        if args[0] == "info":
            return _json_result(self.info)
        if args[:2] == ("image", "inspect"):
            return _json_result(self.image)
        if args[0] == "create":
            self._create(args)
            return self.create_result or CLIResult(returncode=0, stdout=(CONTAINER_ID + "\n").encode(), stderr=b"")
        if args[0] == "inspect":
            value = copy.deepcopy(self.container)
            if args[-1] == self.foreign_id:
                value.update(Id=self.foreign_id, Name="/foreign-container")
                value["Config"]["Labels"] = {}
                return _json_result([value])
            mutation = self.after_start_mutation if self.started else self.before_start_mutation
            if mutation:
                mutation(value)
            return _json_result([value])
        if args[0] == "start":
            self.start_entered.set()
            if self.start_hook:
                self.start_hook(self.container)
            if self.block_start:
                await asyncio.Event().wait()
            if self.start_error:
                raise self.start_error
            self.started = True
            self.container["State"].update(Status="exited", ExitCode=self.exit_code)
            return self.start_result
        if args[0] == "rm":
            self.rm_entered.set()
            if self.release_rm:
                await self.release_rm.wait()
            return self.rm_result
        raise AssertionError(f"Unexpected fixed Docker lifecycle operation: {args[0]}")

    def commands(self, command):
        normalized = [args[1:] if args[0] == "container" else args for args, *_ in self.calls]
        return [args for args in normalized if args[0] == command]


class SandboxRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="a2a-sandbox-runtime-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.repository = SQLiteWorkflowRepository(self.directory / "registry.sqlite3")
        self.registry = WorkspaceRegistry(self.repository, self.directory / "workspaces")
        self.store = ArtifactStore(self.repository, self.registry)
        self.docker = FakeDocker()
        self.runtime = SandboxRuntime(self.repository, self.registry, self.store, docker=self.docker)
        self.make_fixture()
        self.profile = ExecutionProfile(name="fixture-build", tool_name="run_build",
                                        argv=("/usr/local/bin/python3", "-c", "print(1)"))

    def make_fixture(self, *, image=IMAGE_ID, network="DENY", environment=True):
        self.run_record = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입 구현",
                                      status=WorkflowStatus.IMPLEMENTING)
        self.step = WorkflowStep(run_id=self.run_record.run_id, agent_role=AgentRole.DEVELOPER,
                                 status=WorkflowStepStatus.RUNNING,
                                 requirement_ids=list(SCENARIO_REGISTRY[SCN_001_ID].requirement_ids),
                                 code_version=1)
        self.lock_bytes = b"isolated-test-dependency==1.0\n"
        baseline = ExecutionBaseline(container_image_digest=image,
            dependency_lock_hash="sha256:" + hashlib.sha256(self.lock_bytes).hexdigest(),
            hardware_profile="isolated-runtime-fixture", network_policy=network,
            allowed_hosts=("approved.example.invalid",) if network == "ALLOWLIST" else ()) if environment else None
        configuration = RunConfigurationArtifact(run_id=self.run_record.run_id,
            scenario_id=self.run_record.scenario_id, workspace_id=self.run_record.workspace_id,
            configuration=RunConfiguration(environment=baseline))
        self.root = self.registry.base_path / str(self.run_record.workspace_id)
        workspace = WorkspaceRecord(workspace_id=self.run_record.workspace_id,
                                    run_id=self.run_record.run_id, root_path=str(self.root))
        self.repository.create_run(self.run_record, (self.step,), (),
                                   run_configuration=configuration, workspace=workspace)
        self.registry.provision(self.run_record.workspace_id, run_id=self.run_record.run_id)
        self.source = self.root / "source"
        (self.source / "requirements.lock").write_bytes(self.lock_bytes)
        (self.source / "signup.py").write_text("print('fixture signup')\n", encoding="utf-8")
        self.git("init", "--object-format=sha1")
        self.git("add", "requirements.lock", "signup.py")
        self.git("commit", "-m", "fixture initial")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.snapshot = None
        if environment:
            self.snapshot = self.store.bind(self.run_record.run_id, role=AgentRole.DEVELOPER).freeze_source(
                workflow_step_id=self.step.workflow_step_id, commit_hash=self.commit,
                repository_id="sandbox-fixture", lock_path="requirements.lock")

    def git(self, *args):
        result = subprocess.run([
            "git", "-c", "user.name=Sandbox Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args,
        ], cwd=self.source, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, text=True, timeout=20)
        return result.stdout

    async def execute(self, *, role=AgentRole.DEVELOPER, profile=None, source_id=None, inputs=None):
        bound = self.runtime.bind(self.run_record.run_id, role=role)
        return await bound.run(source_id or self.snapshot.artifact_id,
                               profile or self.profile, inputs=inputs)

    async def assert_error(self, code, **kwargs):
        with self.assertRaises(SandboxError) as raised:
            await self.execute(**kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn(str(self.directory), str(raised.exception))
        return raised.exception

    async def test_success_carries_actual_source_and_image_identity_without_product_verdict(self):
        previous = self.repository.get_run(self.run_record.run_id)
        result = await self.execute()
        self.assertEqual(result.execution_manifest, self.snapshot.execution_manifest())
        self.assertEqual(result.source_artifact_id, self.snapshot.artifact_id)
        self.assertEqual(result.run_id, self.run_record.run_id)
        self.assertEqual(result.image_id, IMAGE_ID)
        self.assertEqual(result.container_id, CONTAINER_ID)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout, "build output\n")
        self.assertIsNone(self.repository.get_run(self.run_record.run_id).verdict)
        self.assertEqual(self.repository.get_run(self.run_record.run_id).status, previous.status)
        self.assertFalse(hasattr(result, "verdict"))
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_product_nonzero_exit_is_a_result_not_infrastructure_error(self):
        self.docker.exit_code = 1
        self.docker.start_result = CLIResult(returncode=1, stdout=b"", stderr=b"product test failed\n")
        result = await self.execute()
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stderr, "product test failed\n")
        self.assertIsNone(self.repository.get_run(self.run_record.run_id).verdict)

    async def test_create_is_bounded_nonroot_networkless_and_snapshot_readonly(self):
        await self.execute()
        args = self.docker.commands("create")[0]
        self.assertIn("--read-only", args)
        for option, value in (("--network", "none"), ("--user", "10001:10001"),
                              ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges:true"),
                              ("--log-driver", "none")):
            self.assertEqual(args[args.index(option) + 1], value)
        self.assertNotIn("--privileged", args)
        self.assertNotIn("--publish", args)
        self.assertNotIn("--volume", args)
        self.assertNotIn("docker.sock", " ".join(args))
        self.assertIn(IMAGE_ID, args)
        self.assertEqual(self.docker.container["HostConfig"]["Memory"], self.profile.limits.memory_bytes)
        self.assertEqual(self.docker.container["HostConfig"]["PidsLimit"], self.profile.limits.pids)
        self.assertTrue(all(not item["RW"] for item in self.docker.container["Mounts"]))
        self.assertEqual(len(self.docker.container["Mounts"]), 1)
        source = Path(self.docker.container["Mounts"][0]["Source"])
        self.assertFalse(source.exists(), "Owned execution staging is cleaned after container removal")

    async def test_constructor_and_binding_do_not_start_docker(self):
        self.runtime.bind(self.run_record.run_id, role=AgentRole.QA)
        self.assertEqual(self.docker.calls, [])

    async def test_host_environment_is_never_forwarded_and_safe_image_env_is_inspected(self):
        self.docker.image[0]["Config"]["Env"] = ["PATH=/usr/local/bin:/usr/bin", "HOME=/root"]
        with patch.dict(os.environ, {"OPENAI_API_KEY": "host-only-fixture-secret"}):
            await self.execute()
        environment = self.docker.container["Config"]["Env"]
        self.assertIn("PATH=/usr/local/bin:/usr/bin", environment)
        self.assertIn("HOME=/work", environment)
        self.assertNotIn("HOME=/root", environment)
        self.assertNotIn("host-only-fixture-secret", " ".join(environment))

    async def test_host_trusted_inputs_are_separate_readonly_mount_and_cleaned(self):
        inputs = {"tests/check.py": b"print('isolated approved tests')\n"}
        def assert_inputs(container):
            self.assertEqual(len(container["Mounts"]), 2)
            mounts = {item["Destination"]: item for item in container["Mounts"]}
            self.assertIn("/snapshot", mounts)
            self.assertIn("/inputs", mounts)
            self.assertFalse(mounts["/inputs"]["RW"])
            self.assertEqual((Path(mounts["/inputs"]["Source"]) / "tests/check.py").read_bytes(),
                             inputs["tests/check.py"])
        self.docker.start_hook = assert_inputs
        await self.execute(inputs=inputs)
        self.assertTrue(all(not Path(item["Source"]).exists() for item in self.docker.container["Mounts"]))

    async def test_role_tool_pair_is_checked_before_docker(self):
        for role in (AgentRole.PLANNER, AgentRole.QA, AgentRole.SECURITY):
            with self.subTest(role=role):
                await self.assert_error(SandboxErrorCode.DENIED, role=role)
        self.assertEqual(self.docker.calls, [])

    async def test_completed_workflow_step_no_longer_grants_execution(self):
        run = self.repository.get_run(self.run_record.run_id)
        step = self.step.model_copy(update={"status": WorkflowStepStatus.SUCCEEDED})
        self.repository.save_task_update(run, step,
            AgentContext(run_id=run.run_id, agent_id="sandbox-fixture-developer"),
            TraceEvent(run_id=run.run_id, workflow_step_id=step.workflow_step_id,
                       event_type="FIXTURE", actor="test", attempt=0))
        await self.assert_error(SandboxErrorCode.DENIED)
        self.assertEqual(self.docker.calls, [])

    async def test_validation_role_requires_active_step_source_handoff_not_just_artifact_acl(self):
        run = self.repository.get_run(self.run_record.run_id).model_copy(update={"status": WorkflowStatus.VALIDATING})
        self.repository.save_run_update(run, ())
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.QA,
            status=WorkflowStepStatus.RUNNING, requirement_ids=list(self.snapshot.requirement_ids),
            input_artifact_ids=[], code_version=1)
        self.repository.save_task_update(run, step,
            AgentContext(run_id=run.run_id, agent_id="sandbox-fixture-qa"),
            TraceEvent(run_id=run.run_id, workflow_step_id=step.workflow_step_id,
                       event_type="FIXTURE", actor="test", attempt=0))
        profile = ExecutionProfile(name="fixture-qa", tool_name="run_unit_tests", argv=self.profile.argv)
        await self.assert_error(SandboxErrorCode.DENIED, role=AgentRole.QA, profile=profile)
        self.assertEqual(self.docker.calls, [])

    async def test_run_aborted_after_preparation_is_rechecked_before_start(self):
        def abort_run(_container):
            run = self.repository.get_run(self.run_record.run_id).model_copy(update={
                "status": WorkflowStatus.ABORTED, "termination_reason": "fixture aborted"})
            self.repository.save_run_update(run, ())
        self.docker.before_start_mutation = abort_run
        await self.assert_error(SandboxErrorCode.DENIED)
        self.assertEqual(self.docker.commands("start"), [])
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_qa_and_security_can_execute_only_their_host_profiles(self):
        run = self.repository.get_run(self.run_record.run_id).model_copy(update={"status": WorkflowStatus.VALIDATING})
        self.repository.save_run_update(run, ())
        for role in (AgentRole.QA, AgentRole.SECURITY):
            step = WorkflowStep(run_id=run.run_id, agent_role=role,
                status=WorkflowStepStatus.RUNNING, requirement_ids=list(self.snapshot.requirement_ids),
                input_artifact_ids=[self.snapshot.artifact_id], code_version=1)
            self.repository.save_task_update(run, step,
                AgentContext(run_id=run.run_id, agent_id="sandbox-fixture-" + role.value),
                TraceEvent(run_id=run.run_id, workflow_step_id=step.workflow_step_id,
                           event_type="FIXTURE", actor="test", attempt=0))
        for role, tool in ((AgentRole.QA, "run_unit_tests"), (AgentRole.SECURITY, "run_security_scan")):
            with self.subTest(role=role):
                result = await self.execute(role=role, profile=ExecutionProfile(
                    name="fixture-check", tool_name=tool, argv=self.profile.argv))
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(result.source_artifact_id, self.snapshot.artifact_id)

    async def test_unregistered_source_is_rejected_without_docker(self):
        await self.assert_error(SandboxErrorCode.DENIED, source_id=uuid4())
        self.assertEqual(self.docker.calls, [])

    async def test_cross_run_source_is_rejected_without_docker(self):
        previous_id = self.snapshot.artifact_id
        self.make_fixture()
        await self.assert_error(SandboxErrorCode.DENIED, source_id=previous_id)
        self.assertEqual(self.docker.calls, [])

    async def test_live_source_changes_are_not_mounted_instead_of_frozen_snapshot(self):
        (self.source / "signup.py").write_text("print('live uncommitted change')\n", encoding="utf-8")
        (self.source / "untracked.py").write_text("live-only\n", encoding="utf-8")
        def assert_frozen(container):
            root = Path(container["Mounts"][0]["Source"])
            self.assertEqual((root / "signup.py").read_text(), "print('fixture signup')\n")
            self.assertFalse((root / "untracked.py").exists())
            self.assertNotEqual(root, self.source)
            self.assertFalse((root / ".git").exists())
        self.docker.start_hook = assert_frozen
        await self.execute()

    async def test_remote_docker_endpoint_is_denied_before_image_or_create(self):
        self.docker.endpoint = "tcp://remote.example.invalid:2375"
        await self.assert_error(SandboxErrorCode.DENIED)
        self.assertEqual(self.docker.commands("create"), [])

    async def test_nonlinux_daemon_is_denied(self):
        self.docker.info["OSType"] = "windows"
        await self.assert_error(SandboxErrorCode.DENIED)
        self.assertEqual(self.docker.commands("create"), [])

    async def test_daemon_without_default_seccomp_is_denied(self):
        self.docker.info["SecurityOptions"] = ["name=cgroupns"]
        await self.assert_error(SandboxErrorCode.DENIED)
        self.assertEqual(self.docker.commands("create"), [])

    async def test_image_digest_mismatch_is_not_pulled_or_executed(self):
        self.docker.image[0]["Id"] = "sha256:" + "e" * 64
        await self.assert_error(SandboxErrorCode.IMAGE)
        self.assertEqual(self.docker.commands("create"), [])
        self.assertEqual(self.docker.commands("pull"), [])

    async def test_repo_digest_and_image_config_id_are_checked_as_distinct_identities(self):
        reference = "localfixture@" + IMAGE_ID
        actual_id = "sha256:" + "e" * 64
        self.docker.image[0].update(Id=actual_id, RepoDigests=[reference])
        profile = ExecutionProfile(name="fixture-build", tool_name="run_build",
                                   argv=self.profile.argv, image_reference=reference)
        result = await self.execute(profile=profile)
        self.assertEqual(result.image_id, actual_id)
        self.assertEqual(result.execution_manifest.container_image_digest, IMAGE_ID)
        args = self.docker.commands("create")[0]
        self.assertIn(actual_id, args)
        self.assertNotIn(reference, args, "Execution uses the inspected immutable image config ID")

    async def test_repo_reference_must_appear_in_local_inspected_repo_digests(self):
        profile = ExecutionProfile(name="fixture-build", tool_name="run_build",
            argv=self.profile.argv, image_reference="localfixture@" + IMAGE_ID)
        await self.assert_error(SandboxErrorCode.IMAGE, profile=profile)
        self.assertEqual(self.docker.commands("create"), [])

    async def test_image_declared_volumes_are_rejected(self):
        self.docker.image[0]["Config"]["Volumes"] = {"/hidden-write-volume": {}}
        await self.assert_error(SandboxErrorCode.DENIED)
        self.assertEqual(self.docker.commands("create"), [])

    async def test_image_embedded_secret_environment_is_rejected(self):
        self.docker.image[0]["Config"]["Env"] = ["OPENAI_API_KEY=fixture-secret"]
        await self.assert_error(SandboxErrorCode.DENIED)
        self.assertEqual(self.docker.commands("create"), [])

    async def test_allowlisted_network_is_explicitly_unsupported_not_silently_relaxed(self):
        self.make_fixture(network="ALLOWLIST")
        await self.assert_error(SandboxErrorCode.CONFIGURATION)
        self.assertEqual(self.docker.calls, [])

    async def test_inspected_source_rw_tamper_prevents_start_and_removes_owned_container(self):
        self.docker.before_start_mutation = lambda value: value["Mounts"][0].update(RW=True)
        await self.assert_error(SandboxErrorCode.INTEGRITY)
        self.assertEqual(self.docker.commands("start"), [])
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_inspected_container_privilege_tamper_prevents_start(self):
        self.docker.before_start_mutation = lambda value: value["HostConfig"].update(Privileged=True)
        await self.assert_error(SandboxErrorCode.INTEGRITY)
        self.assertEqual(self.docker.commands("start"), [])
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_inspected_container_extra_host_bind_prevents_start(self):
        self.docker.before_start_mutation = lambda value: value["Mounts"].append({
            "Type": "bind", "Source": "/etc", "Destination": "/host", "RW": False})
        await self.assert_error(SandboxErrorCode.INTEGRITY)
        self.assertEqual(self.docker.commands("start"), [])

    async def test_inspected_container_identity_and_command_tamper_prevents_start(self):
        for mutate in (
            lambda value: value.update(Image="sha256:" + "a" * 64),
            lambda value: value["Config"].update(User="0"),
            lambda value: value["Config"].update(Cmd=["hostile-command"]),
            lambda value: value["Config"].update(Env=["API_KEY=unexpected"]),
        ):
            with self.subTest(mutation=mutate):
                self.docker.before_start_mutation = mutate
                await self.assert_error(SandboxErrorCode.INTEGRITY)
        self.assertEqual(self.docker.commands("start"), [])

    async def test_missing_ownership_labels_blocks_removal_and_preserves_staging(self):
        self.docker.before_start_mutation = lambda value: value["Config"].update(Labels={})
        await self.assert_error(SandboxErrorCode.CLEANUP)
        self.assertEqual(self.docker.commands("start"), [])
        self.assertEqual(self.docker.commands("rm"), [])
        self.assertTrue(Path(self.docker.container["Mounts"][0]["Source"]).exists())

    async def test_create_stdout_cannot_redirect_cleanup_to_a_foreign_container(self):
        self.docker.foreign_id = "f" * 64
        self.docker.create_result = CLIResult(returncode=0,
            stdout=(self.docker.foreign_id + "\n").encode(), stderr=b"")
        await self.assert_error(SandboxErrorCode.INTEGRITY)
        self.assertEqual(self.docker.commands("start"), [])
        self.assertEqual(self.docker.commands("rm"), [("rm", "--force", CONTAINER_ID)])

    async def test_foreign_container_name_blocks_start_and_cleanup(self):
        self.docker.before_start_mutation = lambda value: value.update(Name="/foreign-container")
        await self.assert_error(SandboxErrorCode.CLEANUP)
        self.assertEqual(self.docker.commands("start"), [])
        self.assertEqual(self.docker.commands("rm"), [])

    async def test_oom_is_infrastructure_error_not_product_failure(self):
        self.docker.after_start_mutation = lambda value: value["State"].update(OOMKilled=True, ExitCode=137)
        await self.assert_error(SandboxErrorCode.EXECUTION)
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_daemon_reported_state_error_is_infrastructure_error(self):
        self.docker.after_start_mutation = lambda value: value["State"].update(Error="Bearer private-daemon-token")
        await self.assert_error(SandboxErrorCode.EXECUTION)
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_timeout_attempts_owned_container_cleanup(self):
        self.docker.start_error = SandboxError(SandboxErrorCode.TIMEOUT)
        await self.assert_error(SandboxErrorCode.TIMEOUT)
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_cancelled_execution_attempts_owned_container_cleanup(self):
        self.docker.block_start = True
        task = asyncio.create_task(self.execute())
        await asyncio.wait_for(self.docker.start_entered.wait(), timeout=5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_repeated_cancellation_during_cleanup_does_not_abandon_owned_container(self):
        self.docker.block_start = True
        self.docker.release_rm = asyncio.Event()
        task = asyncio.create_task(self.execute())
        await asyncio.wait_for(self.docker.start_entered.wait(), timeout=5)
        task.cancel()
        await asyncio.wait_for(self.docker.rm_entered.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        self.docker.release_rm.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assertFalse(Path(self.docker.container["Mounts"][0]["Source"]).exists())

    async def test_cleanup_failure_is_explicit_and_preserves_owned_staging(self):
        self.docker.rm_result = CLIResult(returncode=1, stdout=b"", stderr=b"daemon unavailable")
        await self.assert_error(SandboxErrorCode.CLEANUP)
        source = Path(self.docker.container["Mounts"][0]["Source"])
        self.assertTrue(source.exists(), "Never remove bind staging while container removal is uncertain")

    async def test_returned_output_and_repr_redact_known_credentials_and_never_emit_source(self):
        self.docker.start_result = CLIResult(returncode=0,
            stdout=b"password=fixture-password\nnormal result\n",
            stderr=b"Authorization: Bearer fixture-token\n")
        result = await self.execute()
        self.assertNotIn("fixture-password", result.stdout)
        self.assertNotIn("fixture-token", result.stderr)
        self.assertIn("normal result", result.stdout)
        self.assertNotIn("normal result", repr(result))
        self.assertNotIn(str(self.directory), repr(result))
        events, _ = self.repository.list_events(self.run_record.run_id, limit=1000, offset=0)
        serialized = json.dumps([item.model_dump(mode="json") for item in events])
        self.assertNotIn("fixture signup", serialized)
        self.assertNotIn("fixture-password", serialized)
        self.assertNotIn("fixture-token", serialized)


if __name__ == "__main__":
    unittest.main()
