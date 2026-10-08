"""Fixed Docker lifecycle for verified Source; no model Shell or Host fallback."""

import asyncio
from dataclasses import dataclass, field
import json
import re
import time
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from mcp_tools.core.policy import ROLE_TOOL_NAMES
from orchestrator.artifacts.contracts import ArtifactAccessError
from orchestrator.core.security import redact_data, redact_text
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxError, SandboxErrorCode, SandboxResult
from orchestrator.sandbox.docker_cli import DockerCLI
from orchestrator.sandbox.materialization import SnapshotMaterializer
from orchestrator.workspaces.policy import WorkspaceAccessError, workspace_uuid


_SAFE_IMAGE_ENV = frozenset({
    "PATH", "LANG", "LC_ALL", "HOME", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256",
    "PYTHON_PIP_VERSION", "PYTHON_SETUPTOOLS_VERSION", "PYTHON_GET_PIP_URL",
    "PYTHON_GET_PIP_SHA256", "PYTHON_GET_PIP_VERSION", "PIP_NO_CACHE_DIR",
    "NODE_VERSION", "YARN_VERSION", "NPM_CONFIG_LOGLEVEL",
})
_ENV = {
    "HOME": "/work", "TMPDIR": "/tmp", "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONPATH": "/snapshot", "PIP_CACHE_DIR": "/tmp/pip",
    "npm_config_cache": "/tmp/npm", "XDG_CACHE_HOME": "/tmp/cache",
}
_LABEL_PREFIX = "a2a.sandbox."


def _uuid(value):
    try:
        return workspace_uuid(value)
    except WorkspaceAccessError:
        raise SandboxError(SandboxErrorCode.INVALID) from None


def _json(result):
    if result.returncode != 0:
        raise SandboxError(SandboxErrorCode.EXECUTION)
    try:
        def unique(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError
                value[key] = item
            return value
        def nonfinite(_value):
            raise ValueError
        return json.loads(result.stdout.decode("utf-8"), object_pairs_hook=unique, parse_constant=nonfinite)
    except Exception:
        raise SandboxError(SandboxErrorCode.INTEGRITY) from None


def _one(result):
    value = _json(result)
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise SandboxError(SandboxErrorCode.INTEGRITY)
    return value[0]


def _environment(values):
    if not isinstance(values, list):
        raise SandboxError(SandboxErrorCode.DENIED)
    result = {}
    for value in values:
        if not isinstance(value, str) or "=" not in value or len(value) > 8192:
            raise SandboxError(SandboxErrorCode.DENIED)
        key, text = value.split("=", 1)
        if key in result or key not in _SAFE_IMAGE_ENV or any(ord(c) < 32 for c in value) or redact_text(text) != text:
            raise SandboxError(SandboxErrorCode.DENIED)
        result[key] = text
    return result


def _structured_stdout(content, exit_code, decoder, maximum):
    """Host-only pure report decoder before JSON-aware secret checking.

    Applying prose regex redaction to raw JSON can corrupt its escaping or
    discard duplicate-field evidence. The Tool first validates raw bytes and
    returns sanitized JSON; never accept a decoder from Model arguments.
    """
    text = decoder(content, exit_code)
    if type(text) is not str or len(text.encode("utf-8")) > maximum:
        raise SandboxError(SandboxErrorCode.OUTPUT_LIMIT)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result
    def nonfinite(_value):
        raise ValueError
    data = json.loads(text, object_pairs_hook=unique, parse_constant=nonfinite)
    if type(data) is not dict or redact_data(data) != data:
        raise SandboxError(SandboxErrorCode.DENIED)
    # Validation/sanitization belongs to the trusted Tool parser, not a
    # rewritten string with no relationship to the actual runner result.
    return text


class SandboxRuntime:
    def __init__(self, repository, workspace_registry, artifact_store, *, docker=None, materializer=None):
        self._repository = repository
        self._workspaces = workspace_registry
        self._artifacts = artifact_store
        self._docker = docker if docker is not None else DockerCLI()
        self._materializer = materializer if materializer is not None else SnapshotMaterializer(workspace_registry)

    def bind(self, run_id, *, role):
        if not isinstance(role, AgentRole) or role is AgentRole.PLANNER:
            raise SandboxError(SandboxErrorCode.DENIED)
        run_id = _uuid(run_id)
        try:
            self._artifacts.bind(run_id, role=role)
        except (ArtifactAccessError, WorkspaceAccessError):
            raise SandboxError(SandboxErrorCode.DENIED) from None
        return BoundSandbox(run_id=run_id, role=role, runtime=self)

    def _context(self, run_id, role, source_id, profile):
        if not isinstance(profile, ExecutionProfile) or profile.tool_name not in ROLE_TOOL_NAMES.get(role, ()):
            raise SandboxError(SandboxErrorCode.DENIED)
        try:
            run = self._repository.get_run(run_id)
            configuration = self._repository.get_run_configuration(run_id)
            content = self._artifacts.bind(run_id, role=role).read(source_id)
            workspace = self._workspaces.get_record(run.workspace_id, run_id=run_id) if run else None
            steps = self._repository.list_steps(run_id)
        except (ArtifactAccessError, WorkspaceAccessError):
            raise SandboxError(SandboxErrorCode.DENIED) from None
        except Exception:
            raise SandboxError(SandboxErrorCode.EXECUTION) from None
        if run is None or configuration is None or workspace is None or not isinstance(content.metadata, CodeSnapshotArtifact):
            raise SandboxError(SandboxErrorCode.INTEGRITY)
        environment = configuration.configuration.environment
        if environment is None or environment.network_policy != "DENY":
            # ALLOWLIST requires an egress proxy/preparation design, not bridge.
            raise SandboxError(SandboxErrorCode.CONFIGURATION)
        active_states = {
            AgentRole.DEVELOPER: {WorkflowStatus.IMPLEMENTING, WorkflowStatus.FIXING},
            AgentRole.QA: {WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING},
            AgentRole.SECURITY: {WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING},
        }
        if run.status not in active_states.get(role, set()):
            raise SandboxError(SandboxErrorCode.DENIED)
        source = content.metadata
        active = [step for step in steps if step.agent_role is role and step.status is WorkflowStepStatus.RUNNING and step.attempt == run.fix_attempt]
        if len(active) != 1 or source.code_version != run.fix_attempt + 1:
            raise SandboxError(SandboxErrorCode.DENIED)
        if role is AgentRole.DEVELOPER:
            if active[0].workflow_step_id != source.workflow_step_id:
                raise SandboxError(SandboxErrorCode.DENIED)
        elif source.artifact_id not in active[0].input_artifact_ids:
            raise SandboxError(SandboxErrorCode.DENIED)
        if active[0].code_version is not None and active[0].code_version != source.code_version:
            raise SandboxError(SandboxErrorCode.INTEGRITY)
        return run, configuration, workspace, content

    async def _call(self, args, limits, *, execute=False, deadline=None):
        timeout = limits.timeout_seconds if execute else limits.control_timeout_seconds
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
        if timeout < 0.01:
            raise SandboxError(SandboxErrorCode.TIMEOUT)
        return await self._docker.run(
            tuple(args), timeout_seconds=timeout,
            max_stdout_bytes=limits.max_stdout_bytes if execute else 1024 * 1024,
            max_stderr_bytes=limits.max_stderr_bytes if execute else 64 * 1024,
        )

    async def _image(self, profile, digest, deadline):
        endpoint = getattr(self._docker, "endpoint", "unix:///var/run/docker.sock")
        try:
            parsed = urlsplit(endpoint)
            if parsed.scheme != "unix" or parsed.netloc or not parsed.path.startswith("/") or parsed.query or parsed.fragment or any(ord(c) < 32 for c in endpoint):
                raise ValueError
        except Exception:
            raise SandboxError(SandboxErrorCode.DENIED) from None
        info = _json(await self._call(("info", "--format", "{{json .}}"), profile.limits, deadline=deadline))
        security = info.get("SecurityOptions", []) if isinstance(info, dict) else []
        if not isinstance(info, dict) or info.get("OSType") != "linux" or not isinstance(security, list) or not any(isinstance(item, str) and "name=seccomp" in item and "unconfined" not in item for item in security):
            raise SandboxError(SandboxErrorCode.DENIED)
        reference = profile.image_reference or digest
        if (reference if reference.startswith("sha256:") else reference.rsplit("@", 1)[-1]) != digest:
            raise SandboxError(SandboxErrorCode.IMAGE)
        image = _one(await self._call(("image", "inspect", reference), profile.limits, deadline=deadline))
        image_id = image.get("Id")
        if not isinstance(image_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
            raise SandboxError(SandboxErrorCode.IMAGE)
        if reference.startswith("sha256:"):
            if image_id != digest:
                raise SandboxError(SandboxErrorCode.IMAGE)
        elif reference not in (image.get("RepoDigests") or []):
            raise SandboxError(SandboxErrorCode.IMAGE)
        config = image.get("Config")
        if image.get("Os") != "linux" or not isinstance(config, dict) or config.get("Volumes") or config.get("OnBuild"):
            raise SandboxError(SandboxErrorCode.DENIED)
        env = _environment(config.get("Env") or [])
        env.update(_ENV)
        return image_id, env

    @staticmethod
    def _create(name, labels, prepared, profile, image_id):
        limits = profile.limits
        args = [
            "container", "create", "--name", name, "--pull", "never", "--read-only",
            "--network", "none", "--user", "10001:10001", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--cpus", str(limits.cpus),
            "--memory", str(limits.memory_bytes), "--memory-swap", str(limits.memory_bytes),
            "--pids-limit", str(limits.pids), "--ipc", "none", "--cgroupns", "private",
            "--restart", "no", "--log-driver", "none", "--no-healthcheck",
            "--workdir", "/work", "--entrypoint", profile.argv[0],
        ]
        for key, value in labels.items():
            args.extend(("--label", f"{key}={value}"))
        for key, value in _ENV.items():
            args.extend(("--env", f"{key}={value}"))
        for target in ("/work", "/output", "/tmp"):
            args.extend(("--tmpfs", f"{target}:rw,nosuid,nodev,size={limits.tmpfs_bytes},mode=1777"))
        for source, destination in ((prepared.source_root, "/snapshot"), (prepared.inputs_root, "/inputs")):
            if source is not None:
                # Docker --mount uses CSV; reject separator syntax in Host path.
                if any(char in str(source) for char in (",", "\n", "\r", '"')):
                    raise SandboxError(SandboxErrorCode.PATH)
                args.extend(("--mount", f"type=bind,src={source},dst={destination},readonly,bind-propagation=rprivate"))
        return (*args, image_id, *profile.argv[1:])

    @staticmethod
    def _validate_container(container, *, container_id, name, labels, prepared, profile, image_id, env):
        try:
            config, host = container["Config"], container["HostConfig"]
            expected_mounts = {(str(prepared.source_root), "/snapshot")}
            if prepared.inputs_root is not None:
                expected_mounts.add((str(prepared.inputs_root), "/inputs"))
            mounts = container["Mounts"]
            if not isinstance(mounts, list) or len(mounts) != len(expected_mounts) or {
                (item.get("Source"), item.get("Destination")) for item in mounts
            } != expected_mounts or any(item.get("Type") != "bind" or item.get("RW") is not False or item.get("Propagation") != "rprivate" for item in mounts):
                raise ValueError
            actual_env = config["Env"]
            if not isinstance(actual_env, list) or len(actual_env) != len(env) or set(actual_env) != {f"{key}={value}" for key, value in env.items()}:
                raise ValueError
            if (
                container["Id"] != container_id or container.get("Name") != "/" + name or container["Image"] != image_id
                or any(config["Labels"].get(key) != value for key, value in labels.items())
                or config["User"] != "10001:10001" or config["WorkingDir"] != "/work"
                or config["Entrypoint"] != [profile.argv[0]] or (config.get("Cmd") or []) != list(profile.argv[1:])
                or config.get("Tty") or config.get("OpenStdin") or config.get("Volumes")
                or host["ReadonlyRootfs"] is not True or host["Privileged"] is not False
                or host["NetworkMode"] != "none" or host.get("CapAdd")
                or set(host["CapDrop"]) != {"ALL"} or set(host["SecurityOpt"]) != {"no-new-privileges:true"}
                or host["NanoCpus"] != int(profile.limits.cpus * 1_000_000_000)
                or host["Memory"] != profile.limits.memory_bytes or host["MemorySwap"] != profile.limits.memory_bytes
                or host["PidsLimit"] != profile.limits.pids or host["IpcMode"] != "none" or host["CgroupnsMode"] != "private"
                or host.get("PidMode") not in (None, "") or host.get("UTSMode") not in (None, "")
                or host.get("UsernsMode") not in (None, "")
                or host.get("Devices") or host.get("DeviceRequests") or host.get("VolumesFrom")
                or host.get("Binds") or host.get("Links") or host.get("ExtraHosts")
                or host.get("PortBindings") or host.get("PublishAllPorts") or host.get("AutoRemove")
                or host["LogConfig"]["Type"] != "none" or host["RestartPolicy"]["Name"] != "no"
                or config.get("Healthcheck", {}).get("Test") != ["NONE"]
            ):
                raise ValueError
            expected_tmpfs = f"rw,nosuid,nodev,size={profile.limits.tmpfs_bytes},mode=1777"
            if host["Tmpfs"] != {target: expected_tmpfs for target in ("/work", "/output", "/tmp")}:
                raise ValueError
            if container["State"]["Status"] != "created" or container["State"]["Running"] is not False:
                raise ValueError
        except Exception:
            raise SandboxError(SandboxErrorCode.INTEGRITY) from None

    async def _run(self, run_id, role, source_id, profile, inputs, stdout_decoder=None):
        if stdout_decoder is not None and (not callable(stdout_decoder) or not isinstance(profile, ExecutionProfile)
                                           or profile.tool_name != "run_unit_tests"):
            raise SandboxError(SandboxErrorCode.INVALID)
        source_id = _uuid(source_id)
        started = time.monotonic()
        run, configuration, workspace, content = self._context(run_id, role, source_id, profile)
        limits = profile.limits
        remaining_run = configuration.configuration.limits.runtime_budget_ms
        deadline = started + limits.timeout_seconds
        if remaining_run is not None:
            # Host must also enforce the persistent shared Run budget in34/37.
            # This cap cannot measure time consumed before this invocation.
            deadline = min(deadline, started + remaining_run / 1000)
        image_id, env = await self._image(profile, content.metadata.container_image_digest, deadline)
        execution_id = uuid4()
        name = "a2a-sandbox-" + execution_id.hex
        labels = {
            _LABEL_PREFIX + "execution": str(execution_id), _LABEL_PREFIX + "run": str(run_id),
            _LABEL_PREFIX + "artifact": str(source_id), _LABEL_PREFIX + "role": role.value,
        }
        prepared = None
        attempted = False
        container_id = None
        result = None
        error = None
        try:
            self._context(run_id, role, source_id, profile)
            prepared = self._materializer.prepare(workspace, content, execution_id, inputs=inputs)
            self._materializer.validate(prepared, content)
            args = self._create(name, labels, prepared, profile, image_id)
            attempted = True
            created = await self._call(args, limits, deadline=deadline)
            if created.returncode != 0:
                raise SandboxError(SandboxErrorCode.EXECUTION)
            container_id = created.stdout.decode("ascii").strip()
            if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
                raise SandboxError(SandboxErrorCode.INTEGRITY)
            container = _one(await self._call(("container", "inspect", container_id), limits, deadline=deadline))
            self._validate_container(container, container_id=container_id, name=name, labels=labels, prepared=prepared, profile=profile, image_id=image_id, env=env)
            self._context(run_id, role, source_id, profile)
            self._materializer.validate(prepared, content)
            attached = await self._call(("container", "start", "--attach", container_id), limits, execute=True, deadline=deadline)
            finished = _one(await self._call(("container", "inspect", container_id), limits, deadline=deadline))
            state = finished.get("State", {})
            if (
                finished.get("Id") != container_id or finished.get("Image") != image_id
                or any(finished.get("Config", {}).get("Labels", {}).get(key) != value for key, value in labels.items())
                or state.get("Status") != "exited" or state.get("Running") is not False
                or state.get("OOMKilled") is not False or state.get("Error")
                or type(state.get("ExitCode")) is not int or not 0 <= state["ExitCode"] <= 255
                or attached.returncode != state["ExitCode"]
            ):
                raise SandboxError(SandboxErrorCode.EXECUTION)
            result = SandboxResult(
                execution_id=execution_id, run_id=run_id, source_artifact_id=source_id,
                profile_name=profile.name, tool_name=profile.tool_name,
                execution_manifest=content.metadata.execution_manifest(), image_id=image_id,
                container_id=container_id, exit_code=state["ExitCode"], duration_ms=int((time.monotonic() - started) * 1000),
                stdout=(redact_text(attached.stdout.decode("utf-8", errors="replace")) if stdout_decoder is None
                        else _structured_stdout(attached.stdout, state["ExitCode"], stdout_decoder, limits.max_stdout_bytes)),
                stderr=redact_text(attached.stderr.decode("utf-8", errors="replace")),
            )
        except BaseException as caught:
            error = caught
        # A CLI timeout/cancel does not stop a daemon-owned Container. Always
        # remove only this execution's owned name/ID, shielded from cancellation.
        async def cleanup():
            if attempted:
                owned = _one(await self._call(("container", "inspect", name), limits))
                owned_id = owned.get("Id")
                if (
                    owned.get("Name") != "/" + name or not isinstance(owned_id, str)
                    or re.fullmatch(r"[0-9a-f]{64}", owned_id) is None
                    or any(owned.get("Config", {}).get("Labels", {}).get(key) != value for key, value in labels.items())
                ):
                    raise SandboxError(SandboxErrorCode.CLEANUP, execution_id=execution_id)
                removed = await self._call(("container", "rm", "--force", owned_id), limits)
                if removed.returncode != 0:
                    raise SandboxError(SandboxErrorCode.CLEANUP, execution_id=execution_id)
            if prepared is not None:
                self._materializer.cleanup(prepared)
        task = asyncio.create_task(cleanup())
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        try:
            task.result()
        except BaseException:
            raise SandboxError(SandboxErrorCode.CLEANUP, execution_id=execution_id) from None
        if cancelled:
            raise asyncio.CancelledError
        if error is not None:
            if isinstance(error, (SandboxError, asyncio.CancelledError)):
                raise error
            raise SandboxError(SandboxErrorCode.EXECUTION, execution_id=execution_id) from None
        return result


@dataclass(frozen=True, kw_only=True)
class BoundSandbox:
    run_id: UUID
    role: AgentRole
    runtime: SandboxRuntime = field(repr=False)

    async def run(self, source_artifact_id, profile, *, inputs=None, stdout_decoder=None):
        return await self.runtime._run(self.run_id, self.role, source_artifact_id, profile, inputs, stdout_decoder)
