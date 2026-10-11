"""Opt-in Host assembly from approved runtime, Tool and starting Source files.

Inspection is read-only and secret-free. Explicit async creation owns newly
allocated providers until composition transfers their close ownership. No
import, inspection or creation starts listeners, MCP, Docker or model calls.
"""

import asyncio
from dataclasses import dataclass, field
import hashlib
import inspect
import json
from pathlib import Path
import stat
from uuid import UUID

from agents.llm.configuration import provider_from_settings
from agents.llm.content import sanitize_content
from agents.llm.contracts import parse_json
from agents.platform.composition import (
    OwnedAgentPlatform, _close_providers, _host_call, create_platform,
)
from agents.platform.configuration import (
    _comparison_path, inspect_runtime_configuration, load_runtime_configuration,
    resolve_runtime_configuration,
)
from agents.platform.host_policy_store import HostPolicyStore
from agents.platform.host_services import HostRuntimeServices, _model_json
from agents.platform.pricing import load_price_book
from agents.platform.security_approval import ApprovedSecurityAssessor
from agents.platform.security_verifier import TrustedSecurityProofVerifier
from agents.platform.starting_source import (
    RegisteredSourcePreparer, load_source_configuration, verify_starting_source,
)
from agents.platform.tool_configuration import _read_regular, load_tool_configuration
from agents.roles.qa_contract import QACaseBinding
from mcp_tools.client import open_mcp_client
from orchestrator.artifacts.git_snapshot import GitSnapshotLimits
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.run_configuration import ExecutionLimits, RunConfiguration
from orchestrator.domain.states import AgentRole
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.workspaces.registry import WorkspaceRegistry


_INVALID = "OWNED_HOST_CONFIGURATION_INVALID"
_FAILED = "OWNED_HOST_INITIALIZATION_FAILED"
_CASE_FIELDS = frozenset({"toolName", "selector", "testId", "requirementId", "title", "expectedResult"})


class HostFactoryError(ValueError):
    """Only a stable reason; never retain JSON, Source, Host paths or keys."""

    def __init__(self, code=_INVALID):
        self.code = code if type(code) is str and code in {_INVALID, _FAILED} else _INVALID
        super().__init__(self.code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _case_data(case):
    return {
        "toolName": case.tool_name, "selector": case.selector, "testId": case.test_id,
        "requirementId": str(case.requirement_id), "title": case.title, "expectedResult": case.expected_result,
    }


def _load_protected_cases(path):
    if path is None:
        return ()
    raw = _read_regular(Path(path).absolute(), 1_048_576)
    data = parse_json(raw.decode("utf-8"), max_bytes=1_048_576)
    if (type(data) is not dict or set(data) != {"schemaVersion", "cases"}
            or type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1
            or type(data["cases"]) is not list or len(data["cases"]) > 256):
        raise HostFactoryError()
    result = []
    for case in data["cases"]:
        if (type(case) is not dict or set(case) != _CASE_FIELDS
                or any(type(value) is not str for value in case.values())):
            raise HostFactoryError()
        result.append(QACaseBinding(tool_name=case["toolName"], selector=case["selector"], test_id=case["testId"],
            requirement_id=UUID(case["requirementId"]), title=case["title"], expected_result=case["expectedResult"]))
    return tuple(result)


def _policy_references(tools, cases):
    profiles = {profile.profile_ref for profile in tools.security_configuration.profiles}
    if len(profiles) != 1:
        raise HostFactoryError()
    selectors = [
        (tool, selector)
        for tool, selected in (
            ("run_unit_tests", tools.unit_configuration_for(AgentRole.QA).scopes),
            ("run_browser_tests", tools.browser_configuration.suites),
        ) for selector in selected if selector.kind == "PROTECTED"
    ]
    references = {selector.protected_suite_ref for _tool, selector in selectors}
    keys = {(tool, selector.name) for tool, selector in selectors}
    case_keys = [(case.tool_name, case.selector, case.test_id) for case in cases]
    if (len(references) > 1 or len(set(case_keys)) != len(case_keys)
            or {(case.tool_name, case.selector) for case in cases} != keys):
        raise HostFactoryError()
    return next(iter(profiles)), next(iter(references)) if references else None


def _layout_guard(layout, source, input_paths, lock_file):
    """Refuse Workspace/input/reference/storage aliases before DB creation.

    Host files may reside outside Source, with the approved lock optionally in
    the reference repository. DB hardlinks are refused conservatively: another
    alias might be an otherwise unobserved baseline file.
    """
    workspace = _comparison_path(layout.workspace_root)
    reference = _comparison_path(source.reference_repository_path)
    if reference.is_relative_to(workspace) or workspace.is_relative_to(reference):
        raise HostFactoryError()
    configuration_paths = tuple(Path(path).resolve(strict=True) for path in input_paths)
    files = (*configuration_paths, lock_file.resolve(strict=True))
    for path in files:
        if _comparison_path(path).is_relative_to(workspace):
            raise HostFactoryError()
    # Host settings may contain private protected bytes/paths. They must not
    # become committed starting Source, even if the operator chose that repo.
    if any(_comparison_path(path).is_relative_to(reference) for path in configuration_paths):
        raise HostFactoryError()
    databases = (layout.orchestrator_database_path, *layout.agent_database_paths.values())
    reserved = tuple(Path(str(path) + suffix).resolve()
        for path in databases for suffix in ("", "-wal", "-shm", "-journal"))
    file_ids = {(path.stat().st_dev, path.stat().st_ino) for path in files}
    for path in reserved:
        key = _comparison_path(path)
        if key.is_relative_to(reference) or reference.is_relative_to(key):
            raise HostFactoryError()
        for input_path in files:
            other = _comparison_path(input_path)
            if key.is_relative_to(other) or other.is_relative_to(key):
                raise HostFactoryError()
        if path.exists():
            info = path.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) in file_ids):
                raise HostFactoryError()


@dataclass(frozen=True, kw_only=True, repr=False)
class _HostConfiguration:
    runtime: object = field(repr=False)
    runtime_directory: Path = field(repr=False)
    layout: object = field(repr=False)
    tools: object = field(repr=False)
    source: object = field(repr=False)
    protected_cases: tuple = field(repr=False)
    _configuration_json: str = field(repr=False)
    policy_sha256: str
    price_book: object = field(default=None, repr=False)

    def __repr__(self):
        return "HostConfiguration()"

    @property
    def approved_configuration(self):
        # Fresh canonical immutable type; never expose Secrets or Host paths.
        return RunConfiguration.model_validate(parse_json(self._configuration_json))

    def validate_configuration(self, configuration):
        try:
            if self.price_book is not None:
                self.price_book.verify_integrity()
            if type(configuration) is not RunConfiguration:
                raise ValueError
            copied = RunConfiguration.model_validate(parse_json(_model_json(configuration)))
            approved = self.approved_configuration
            if (_model_json(copied) != _model_json(configuration)
                    or copied.architecture != approved.architecture
                    or copied.model != approved.model or copied.limits != approved.limits
                    or copied.environment != approved.environment
                    or copied.starting_commit_hash != approved.starting_commit_hash
                    or copied.starting_snapshot_sha256 != approved.starting_snapshot_sha256
                    or copied.scanner_profile_ref != approved.scanner_profile_ref
                    or copied.protected_test_suite_ref != approved.protected_test_suite_ref):
                raise ValueError
            sanitize_content(copied.model_dump(mode="json", by_alias=True), reject_secrets=True)
        except Exception:
            raise HostFactoryError() from None


def inspect_host_configuration(*, runtime_file, tools_file, source_file, protected_cases_file=None,
                               proof_verifier=None, price_book_file=None):
    """Read-only settings/Git cross-check; never resolve credentials or stores."""
    try:
        if proof_verifier is not None:
            if type(proof_verifier) is not ApprovedSecurityAssessor:
                raise HostFactoryError()
            proof_verifier.verify_integrity()
        price_book = None if price_book_file is None else load_price_book(price_book_file)
        runtime_path, tools_path, source_path = (Path(value).absolute() for value in (runtime_file, tools_file, source_file))
        runtime = load_runtime_configuration(runtime_path)
        layout = inspect_runtime_configuration(runtime, base_directory=runtime_path.parent)
        tools = load_tool_configuration(tools_path)
        if runtime.llm_limits.tool_timeout_seconds != tools.max_call_seconds:
            raise HostFactoryError()
        source = verify_starting_source(load_source_configuration(source_path),
            base_directory=source_path.parent, environment=tools.baseline)
        cases = _load_protected_cases(protected_cases_file)
        scanner_ref, protected_ref = _policy_references(tools, cases)
        lock = Path(tools.dependency_lock_file)
        lock = lock if lock.is_absolute() else tools_path.parent / lock
        inputs = (runtime_path, tools_path, source_path)
        if protected_cases_file is not None:
            inputs += (Path(protected_cases_file).absolute(),)
        if proof_verifier is not None:
            inputs += (proof_verifier.approval_path, *proof_verifier.implementation_paths)
        if price_book is not None:
            inputs += (price_book.approval_path,)
        _layout_guard(layout, source, inputs, lock)
        approved = RunConfiguration(
            model=runtime.model.model_dump(by_alias=True),
            limits=ExecutionLimits(runtime_budget_ms=runtime.runtime_budget_ms),
            environment=tools.baseline, starting_commit_hash=source.configuration.commit_hash,
            starting_snapshot_sha256=source.configuration.snapshot_sha256,
            scanner_profile_ref=scanner_ref, protected_test_suite_ref=protected_ref,
        )
        source_identity = source.configuration.model_dump(mode="json", by_alias=True)
        source_identity.pop("repositoryPath")
        policy = {
            "schemaVersion": 1, "runConfiguration": approved.model_dump(mode="json", by_alias=True),
            "llmLimits": runtime.llm_limits.model_dump(mode="json"),
            "source": source_identity, "build": parse_json(tools._build_json),
            "unit": parse_json(tools._unit_json), "browser": parse_json(tools._browser_json),
            "security": parse_json(tools._security_json), "maxCallSeconds": tools.max_call_seconds,
            "protectedCases": [_case_data(case) for case in cases],
            "semanticProof": "UNVERIFIED" if proof_verifier is None else proof_verifier.descriptor,
        }
        if price_book is not None:
            policy["pricing"] = price_book.descriptor
        return _HostConfiguration(runtime=runtime, runtime_directory=runtime_path.parent, layout=layout,
            tools=tools, source=source, protected_cases=cases,
            _configuration_json=approved.model_dump_json(by_alias=True),
            policy_sha256=hashlib.sha256(_json(policy).encode("utf-8")).hexdigest(), price_book=price_book)
    except Exception:
        raise HostFactoryError() from None


async def _close_allocated(providers, platform=None):
    # Failure must not orphan HTTP clients, including repeated cancellation.
    unique = tuple({id(provider): provider for provider in providers}.values())
    closing = asyncio.create_task(platform.aclose() if platform is not None else _close_providers(unique))
    canceled = False
    while not closing.done():
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            canceled = True
        except Exception:
            break
    try:
        closing.result()
    except Exception:
        pass  # Original initialization failure remains a stable public code.
    return canceled


async def create_host(*, runtime_file, tools_file, source_file, protected_cases_file=None,
                      environ=None, provider_factory=provider_from_settings,
                      mcp_client_factory=open_mcp_client, a2a_client_factory=None, proof_verifier=None,
                      price_book_file=None):
    """Explicit allocation/composition only; returned platform owns shutdown.

    Only trusted Python callers may inject transport/proof factories. JSON,
    model/Tool inputs and the CLI never import arbitrary plugin specifications.
    """
    if (not callable(provider_factory) or not callable(mcp_client_factory)
            or a2a_client_factory is not None and not callable(a2a_client_factory)):
        raise HostFactoryError()
    plan = await _host_call(lambda _argument: inspect_host_configuration(
        runtime_file=runtime_file, tools_file=tools_file, source_file=source_file,
        protected_cases_file=protected_cases_file, proof_verifier=proof_verifier,
        price_book_file=price_book_file), None)
    try:
        resolved = resolve_runtime_configuration(plan.runtime,
            base_directory=plan.runtime_directory, environ=environ)
    except Exception:
        raise HostFactoryError() from None
    providers = {}
    platform = None
    try:
        repository = await _host_call(SQLiteWorkflowRepository, plan.layout.orchestrator_database_path)
        workspaces = WorkspaceRegistry(repository, plan.layout.workspace_root)
        artifacts = ArtifactStore(repository, workspaces, git_limits=GitSnapshotLimits(
            timeout_seconds=plan.source.configuration.git_timeout_seconds))
        preparer = RegisteredSourcePreparer(repository, workspaces, plan.source)
        policies = HostPolicyStore(repository)

        verifier = None if proof_verifier is None else TrustedSecurityProofVerifier(
            repository, proof_verifier, artifact_store=artifacts)

        def validate_execution(configuration, run_id):
            plan.validate_configuration(configuration)
            policies.require(run_id, plan.policy_sha256)
            if verifier is not None:
                verifier.verify_integrity()

        services = HostRuntimeServices(repository, workspaces, artifacts, tools=plan.tools,
            source_configuration=plan.source.configuration, validate_configuration=validate_execution,
            client_factory=mcp_client_factory, protected_cases=plan.protected_cases, proof_verifier=verifier)

        async def prepare_workspace(run_id):
            frozen = await _host_call(repository.get_run_configuration, run_id)
            plan.validate_configuration(frozen.configuration)
            if verifier is not None:
                verifier.verify_integrity()
            budget = platform.budgets.resolve(frozen)
            budget.check()
            await _host_call(lambda selected: policies.pin(selected, plan.policy_sha256), run_id)
            await _host_call(platform.telemetry_store.pin_pricing, run_id)
            await _host_call(lambda selected: preparer.prepare(selected,
                deadline_monotonic=budget.deadline_monotonic), run_id)
            budget.check()

        for role in AgentRole:
            provider = provider_factory(resolved.agent_settings[role])
            if inspect.isawaitable(provider):
                if inspect.iscoroutine(provider):
                    provider.close()
                raise HostFactoryError(_FAILED)
            providers[role] = provider
        platform = create_platform(repository=repository, workspace_registry=workspaces, artifact_store=artifacts,
            orchestrator_settings=resolved.orchestrator_settings, agent_settings=resolved.agent_settings,
            providers=providers, developer_services_factory=services.developer,
            qa_services_factory=services.qa, security_services_factory=services.security,
            prepare_workspace=prepare_workspace, limits=resolved.limits,
            orchestrator_host=resolved.orchestrator_host, orchestrator_port=resolved.orchestrator_port,
            a2a_client_factory=a2a_client_factory, price_book=plan.price_book)
        original_validator = platform.orchestrator_app.state.submission_validator

        def validate_submission(configuration):
            plan.validate_configuration(configuration)
            if verifier is not None:
                verifier.verify_integrity()
            original_validator(configuration)

        original_control = platform.orchestrator_app.state.control_preflight

        def control_preflight(run_id):
            frozen = repository.get_run_configuration(run_id)
            validate_execution(frozen.configuration, run_id)
            original_control(run_id)

        platform.orchestrator_app.state.submission_validator = validate_submission
        platform.orchestrator_app.state.control_preflight = control_preflight
        # Host-only assembly metadata, not a new HTTP/Artifact/Trace contract.
        platform.orchestrator_app.state.approved_host_configuration_json = plan._configuration_json
        platform.orchestrator_app.state.host_policy_sha256 = plan.policy_sha256
        return platform
    except BaseException as error:
        canceled = await _close_allocated(providers.values(), platform)
        if isinstance(error, KeyboardInterrupt):
            raise
        if isinstance(error, asyncio.CancelledError) or canceled:
            raise asyncio.CancelledError() from None
        raise HostFactoryError(_FAILED) from None


def approved_run_configuration(platform):
    """Fresh public configuration template; no keys/Host paths/live Source."""
    try:
        if type(platform) is not OwnedAgentPlatform:
            raise ValueError
        return RunConfiguration.model_validate(parse_json(platform.orchestrator_app.state.approved_host_configuration_json))
    except Exception:
        raise HostFactoryError() from None
