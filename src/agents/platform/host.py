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
import os
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
    resolve_runtime_configuration, OwnedSingleRuntimeConfiguration,
)
from agents.core.identity import SingleAgentIdentity
from agents.single.platform import create_single_platform
from agents.platform.host_policy_store import HostPolicyStore
from agents.platform.package_service import install_package_service
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
from orchestrator.api.host_access import host_token_from_environment, install_host_api_documentation
from orchestrator.core.security import register_secret_values
from orchestrator.observability import ObservationStore
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


def _load_protected_case_file(path):
    """Return (cases, roles). Schema 1 has no roles; schema 2 names each case's role."""
    if path is None:
        return (), None
    raw = _read_regular(Path(path).absolute(), 1_048_576)
    data = parse_json(raw.decode("utf-8"), max_bytes=1_048_576)
    if (type(data) is not dict or set(data) != {"schemaVersion", "cases"}
            or type(data["schemaVersion"]) is not int or data["schemaVersion"] not in (1, 2)
            or type(data["cases"]) is not list or len(data["cases"]) > 256):
        raise HostFactoryError()
    explicit = data["schemaVersion"] == 2
    fields = _CASE_FIELDS | {"role"} if explicit else _CASE_FIELDS
    result, roles = [], []
    for case in data["cases"]:
        if (type(case) is not dict or set(case) != fields
                or any(type(value) is not str for value in case.values())
                or explicit and case["role"] not in ("QA", "SECURITY")):
            raise HostFactoryError()
        result.append(QACaseBinding(tool_name=case["toolName"], selector=case["selector"], test_id=case["testId"],
            requirement_id=UUID(case["requirementId"]), title=case["title"], expected_result=case["expectedResult"]))
        if explicit:
            roles.append(case["role"])
    return tuple(result), tuple(roles) if explicit else None


def _load_protected_cases(path):
    return _load_protected_case_file(path)[0]


def _protected_selectors(tools):
    """``tool:scope`` pairs of every Host in-loop protected suite (Ver1); empty for Ver2.

    The verdict lets these settle generated-test results on the same
    Requirement (orchestrator.application.validation_output.ProtectedSupersession).
    """
    selected = set()
    for role in (AgentRole.QA, AgentRole.SECURITY):
        unit = tools.unit_configuration_for(role)
        selected.update("run_unit_tests:" + scope.name for scope in (() if unit is None else unit.scopes)
                        if scope.kind == "PROTECTED")
    browser = tools.browser_configuration
    selected.update("run_browser_tests:" + suite.name for suite in (() if browser is None else browser.suites)
                    if suite.kind == "PROTECTED")
    return frozenset(selected)


def _policy_references(tools, cases, roles=None):
    # This Host's case file is QA's in-loop suite. Security self-tests can be
    # enabled explicitly; a separate protected Security case contract is not
    # implemented here and must never be silently inferred from QA cases.
    security_unit = tools.unit_configuration_for(AgentRole.SECURITY)
    security_protected = set() if security_unit is None else {
        scope.name for scope in security_unit.scopes if scope.kind == "PROTECTED"}
    # Protected case file schema 2 is the explicit Security case contract: every
    # case states its role, and the role must equal the role of the protected
    # scope it targets. Schema 1 (no roles) keeps Security protected scopes closed.
    if roles is None:
        if security_protected:
            raise HostFactoryError()
    elif len(roles) != len(cases) or any(
            role != ("SECURITY" if case.tool_name == "run_unit_tests" and case.selector in security_protected
                     else "QA")
            for case, role in zip(cases, roles)):
        raise HostFactoryError()
    profiles = {profile.profile_ref for profile in tools.security_configuration.profiles}
    if len(profiles) != 1:
        raise HostFactoryError()
    security = tools.unit_configuration_for(AgentRole.SECURITY)
    selectors = [
        (tool, selector)
        for tool, selected in (
            ("run_unit_tests", tools.unit_configuration_for(AgentRole.QA).scopes),
            ("run_unit_tests", () if security is None else security.scopes),
            ("run_browser_tests", () if tools.browser_configuration is None else tools.browser_configuration.suites),
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
                               proof_verifier=None, price_book_file=None, architecture="MULTI_AGENT"):
    """Read-only settings/Git cross-check; never resolve credentials or stores."""
    try:
        if proof_verifier is not None:
            if type(proof_verifier) is not ApprovedSecurityAssessor:
                raise HostFactoryError()
            proof_verifier.verify_integrity()
        price_book = None if price_book_file is None else load_price_book(price_book_file)
        runtime_path, tools_path, source_path = (Path(value).absolute() for value in (runtime_file, tools_file, source_file))
        # Preserve the default Multi call signature for existing trusted callers.
        runtime = (load_runtime_configuration(runtime_path) if architecture == "MULTI_AGENT"
                   else load_runtime_configuration(runtime_path, architecture=architecture))
        layout = inspect_runtime_configuration(runtime, base_directory=runtime_path.parent)
        tools = load_tool_configuration(tools_path)
        if runtime.llm_limits.tool_timeout_seconds != tools.max_call_seconds:
            raise HostFactoryError()
        if proof_verifier is not None and tools.unit_configuration_for(AgentRole.SECURITY) is not None:
            # Security Requirements are decided by measured tests or by an
            # approved proof, never both in one Run.
            raise HostFactoryError()
        source = verify_starting_source(load_source_configuration(source_path),
            base_directory=source_path.parent, environment=tools.baseline)
        cases, roles = _load_protected_case_file(protected_cases_file)
        scanner_ref, protected_ref = _policy_references(tools, cases, roles)
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
            architecture=architecture,
            model=runtime.model.model_dump(by_alias=True),
            limits=ExecutionLimits(runtime_budget_ms=runtime.runtime_budget_ms),
            environment=tools.baseline, starting_commit_hash=source.configuration.commit_hash,
            starting_snapshot_sha256=source.configuration.snapshot_sha256,
            scanner_profile_ref=scanner_ref, protected_test_suite_ref=protected_ref,
        )
        source_identity = source.configuration.model_dump(mode="json", by_alias=True)
        source_identity.pop("repositoryPath")
        from agents.llm.recovery import DEVELOPMENT_PROGRESS_POLICY, GENERATED_TEST_READY_POLICY
        from agents.llm.development_rejection import POLICY as DEVELOPMENT_REJECTION_POLICY
        from agents.roles.security_draft import SECURITY_DRAFT_POLICY
        from agents.runtime.qa_catalog import QA_DRAFT_POLICY, SECURITY_ID_POLICY
        from agents.llm.corrections import TOOL_ERROR_RETURN_POLICY, DRAFT_PRECHECK_POLICY
        from agents.llm import corrections as correction_rules
        from agents.llm import failure_diagnostics, contracts as llm_contracts, openai_responses
        from agents.llm.engine import UNKNOWN_USAGE_POLICY
        from agents.llm.tool_wire import WORKSPACE_ARGUMENT_POLICY
        from agents.runtime.start_precheck import START_PRECHECK_POLICY
        from agents.runtime.semantic_revision import POLICY as SEMANTIC_REVISION_POLICY
        from orchestrator.infrastructure.sqlite_workflows import DEVELOPMENT_STOP_POLICY, REPEATED_ISSUE_POLICY
        from orchestrator.infrastructure.automatic_stop import POLICY as AUTOMATIC_STOP_POLICY
        from agents.platform import package_service
        from agents.roles import prompts as role_prompts
        from agents.roles import work_rules
        from agents.single import prompts as single_prompts
        from mcp_tools.tools import generated_recheck, unit, unit_runner, unit_store
        from orchestrator.sandbox import docker_cli
        from agents.runtime import (repair_baseline, development_failure, developer_input, repair_locations, normal_request_repair,
                                    developer_workspace, test_guidance, unused_rewrite, ready_path_diff, repair_dependencies)
        from agents.runtime import developer, developer_services, library_contracts, ready_library_types, repair_call_history
        from agents.runtime import (output_safety_precheck, signup_quality_precheck, start_precheck, generated_tests_precheck,
                                    generated_test_inventory, qa_catalog, test_fix, test_write_precheck)
        from agents.runtime import repair_input_failures, startup_preservation, email_normalization, repair_progress as candidate_repair_history
        from agents.single import runtime as single_runtime
        from agents.roles import developer_contract
        from agents.llm.engine import INPUT_RESERVATION_POLICY
        from agents.llm import engine as llm_engine, budget as llm_budget, tool_wire
        from agents.llm import development_rejection, developer_continuation, recovery
        from mcp_tools.tools import files as file_tools
        from mcp_tools.tools import file_io
        from mcp_tools.core import catalog as tool_catalog
        from orchestrator.domain import developer_artifacts, recovery_causes, repair_progress, validation_feedback, protected_feedback
        from agents.runtime import generated_revalidation, qa_services, security_services, security_test_scope, requirement_scope, protected_scopes
        from agents.runtime import service_scope, service_interface
        from agents.runtime import service_capabilities
        from orchestrator.domain import service_design
        from orchestrator.sandbox import service_start_contract
        from orchestrator.infrastructure import run_feedback
        from orchestrator.infrastructure import service_continuations, repair_reproduction
        from agents.runtime import developer_context
        from orchestrator.application import work_failures
        from agents.runtime import test_credential_repair
        from orchestrator.core import source_credentials
        from orchestrator.domain import scenario_registry
        from orchestrator.application import dispatch as validation_dispatch, validation_output
        from orchestrator.infrastructure import automatic_stop, sqlite_workflows, validation_stop, protected_stop, protected_recheck
        policy = {
            'developerReadyReportPolicy': 'REPORTED_PATH_EDITS_CHECKED_AGAINST_FROZEN_DIFF_V7',
            'repairBaselinePolicy': repair_baseline.POLICY,
            'startingSourceStartupPolicy': startup_preservation.POLICY,
            'emailNormalizationRepairPolicy': email_normalization.POLICY,
            'developerInputProjectionPolicy': developer_input.POLICY,
            'inputTokenReservationPolicy': INPUT_RESERVATION_POLICY,
            'productPreconditionRecoveryPolicy': recovery_causes.POLICY,
            'hostAppFixturePolicy': unit_runner.APP_FIXTURE_POLICY,
            'partialEditPolicy': 'HOST_READ_BOUND_CAS_UNIQUE_FRAGMENT_V2',
            'sourceWritePolicy': 'HASH_FOR_REPLACE_ATOMIC_ABSENCE_FOR_CREATE_V1',
            'boundaryReportPolicy': 'IMMUTABLE_AUTOMATIC_BOUNDARY_V1',
            'recoveryImplementationSha256': {name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                for name, module in (('baseline', repair_baseline), ('causes', recovery_causes), ('repairProgress', repair_progress),
                                     ('developmentFailure', development_failure), ('rejection', development_rejection),
                                     ('fileTools', file_tools), ('fileIo', file_io), ('toolCatalog', tool_catalog),
                                     ('developerInput', developer_input),
                                     ('startupPreservation', startup_preservation),
                                     ('startPrecheck', start_precheck),
                                     ('emailNormalization', email_normalization),
                                     ('repairLocations', repair_locations), ('normalRequestRepair', normal_request_repair),
                                     ('invalidInputEvidence', repair_input_failures),
                                     ('candidateRepairHistory', candidate_repair_history), ('repairCallHistory', repair_call_history),
                                     ('libraryContracts', library_contracts), ('readyLibraryTypes', ready_library_types),
                                     ('outputSafetyPrecheck', output_safety_precheck),
                                     ('signupQualityPrecheck', signup_quality_precheck),
                                     ('serviceRequestScope', service_scope), ('serviceInterface', service_interface),
                                     ('scenarioRegistry', scenario_registry),
                                     ('serviceDesign', service_design), ('serviceRuntimeCapabilities', service_capabilities),
                                     ('serviceStartContract', service_start_contract), ('serviceFeedback', run_feedback),
                                     ('serviceContinuations', service_continuations), ('repairReproduction', repair_reproduction),
                                     ('developerContextAdmission', developer_context),
                                     ('workFailureDescriptions', work_failures),
                                     ('generatedTestInventory', generated_test_inventory), ('qaIdCatalog', qa_catalog),
                                     ('toolWire', tool_wire),
                                     ('generatedTestsPrecheck', generated_tests_precheck), ('generatedTestRepair', test_fix),
                                     ('generatedTestWritePrecheck', test_write_precheck),
                                     ('developerRuntime', developer), ('developerServices', developer_services),
                                     ('singleRuntime', single_runtime), ('recoveryRouting', recovery),
                                     ('readyPathDiff', ready_path_diff), ('repairDependencies', repair_dependencies),
                                     ('testGuidance', test_guidance), ('requirements', requirement_scope),
                                     ('testCredentialRepair', test_credential_repair),
                                     ('sourceCredentialPolicy', source_credentials),
                                     ('dockerControl', docker_cli),
                                     ('qaValidation', qa_services), ('securityValidation', security_services),
                                     ('securityTestScope', security_test_scope), ('validationFeedback', validation_feedback),
                                     ('validationDispatch', validation_dispatch), ('validationOutput', validation_output),
                                     ('automaticStop', automatic_stop), ('validationStop', validation_stop),
                                     ('workflowDecisions', sqlite_workflows), ('protectedScopeAdmissions', protected_scopes),
                                     ('blockedValidatorReceipts', protected_stop), ('independentFixFeedback', protected_feedback),
                                     ('protectedRecheck', protected_recheck), ('generatedRecheck', generated_recheck),
                                     ('generatedRevalidation', generated_revalidation), ('unitInvocation', unit),
                                     ('unitEvidenceStore', unit_store), ('unitRunner', unit_runner),
                                     ('readyArtifact', developer_artifacts), ('resultPackage', package_service),
                                     ('readySourceCheck', developer_workspace), ('unusedRewrite', unused_rewrite),
                                     ('developerDecision', developer_contract),
                                     ('llmEngine', llm_engine), ('llmBudget', llm_budget),
                                     ('llmFailureDiagnostics', failure_diagnostics), ('llmContracts', llm_contracts),
                                     ('openaiResponses', openai_responses),
                                     ('developerContinuation', developer_continuation))},
            "developmentProgressPolicy": DEVELOPMENT_PROGRESS_POLICY,
            "developmentRejectionPolicy": DEVELOPMENT_REJECTION_POLICY,
            "generatedTestReadyPolicy": GENERATED_TEST_READY_POLICY,
            "securityDraftPolicy": SECURITY_DRAFT_POLICY,
            "qaDraftPolicy": QA_DRAFT_POLICY,
            # Codex review 2026-11-18: every Run-changing recovery rule is pinned by version,
            # and the instruction texts by hash, so two Runs compare only under the same rules.
            "securityTestIdPolicy": SECURITY_ID_POLICY,
            "workspaceArgumentPolicy": WORKSPACE_ARGUMENT_POLICY,
            "providerUnknownUsagePolicy": UNKNOWN_USAGE_POLICY,
            "providerFailurePolicy": failure_diagnostics.POLICY,
            "toolErrorReturnPolicy": TOOL_ERROR_RETURN_POLICY,
            "draftPrecheckPolicy": DRAFT_PRECHECK_POLICY,
            "startPrecheckPolicy": START_PRECHECK_POLICY,
            "outputSafetyPrecheckPolicy": output_safety_precheck.POLICY,
            "signupQualityPrecheckPolicy": signup_quality_precheck.POLICY,
            "semanticRevisionPolicy": SEMANTIC_REVISION_POLICY,
            "repeatedIssuePolicy": REPEATED_ISSUE_POLICY,
            "developmentStopPolicy": DEVELOPMENT_STOP_POLICY,
            "automaticStopPolicy": AUTOMATIC_STOP_POLICY,  # docs/live-api/05 22절
            "instructionSha256": {name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                                  for name, module in (("roles", role_prompts), ("roleWorkRules", work_rules), ("single", single_prompts),
                                                       ("corrections", correction_rules))},
            "testDataPolicy": {"version": unit_runner.TEST_DATA_POLICY,
                               "runnerSha256": hashlib.sha256(Path(unit_runner.__file__).read_bytes()).hexdigest()},
            "schemaVersion": 1, "runConfiguration": approved.model_dump(mode="json", by_alias=True),
            "llmLimits": runtime.llm_limits.model_dump(mode="json"),
            "source": source_identity, "build": parse_json(tools._build_json),
            "unit": parse_json(tools._unit_json), "browser": parse_json(tools._browser_json),
            "security": parse_json(tools._security_json), "maxCallSeconds": tools.max_call_seconds,
            "protectedCases": [_case_data(case) for case in cases],
            # Security Requirements come from an approved proof, from measured
            # Security unit tests (role SECURITY scopes), or stay UNVERIFIED.
            "semanticProof": (proof_verifier.descriptor if proof_verifier is not None
                              else "MEASURED_TESTS" if tools.unit_configuration_for(AgentRole.SECURITY) is not None
                              else "UNVERIFIED"),
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
                      price_book_file=None, architecture="MULTI_AGENT"):
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
        price_book_file=price_book_file, architecture=architecture), None)
    try:
        resolved = resolve_runtime_configuration(plan.runtime,
            base_directory=plan.runtime_directory, environ=environ)
        host_token = host_token_from_environment(os.environ if environ is None else environ,
            other_credentials=(secret for settings in resolved.agent_settings.values()
                               for secret in (settings.bearer_token, settings.llm_api_key)))
        register_secret_values(host_token.get_secret_value())
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
            services.require_scenario(frozen.scenario_contract)
            await _host_call(ObservationStore(repository).pin_policy, run_id)
            await _host_call(platform.orchestrator_app.state.package_service.enroll, run_id)
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

        for role in resolved.agent_settings:
            provider = provider_factory(resolved.agent_settings[role])
            if inspect.isawaitable(provider):
                if inspect.iscoroutine(provider):
                    provider.close()
                raise HostFactoryError(_FAILED)
            providers[role] = provider
        single = type(plan.runtime) is OwnedSingleRuntimeConfiguration
        factory = create_single_platform if single else create_platform
        identities = ({"agent_settings": resolved.agent_settings[SingleAgentIdentity.SINGLE_AGENT],
                       "provider": providers[SingleAgentIdentity.SINGLE_AGENT]} if single else
                      {"agent_settings": resolved.agent_settings, "providers": providers})
        platform = factory(repository=repository, workspace_registry=workspaces, artifact_store=artifacts,
            orchestrator_settings=resolved.orchestrator_settings, **identities, developer_services_factory=services.developer,
            qa_services_factory=services.qa, security_services_factory=services.security,
            prepare_workspace=prepare_workspace, limits=resolved.limits,
            orchestrator_host=resolved.orchestrator_host, orchestrator_port=resolved.orchestrator_port,
            a2a_client_factory=a2a_client_factory, price_book=plan.price_book,
            protected_selectors=_protected_selectors(plan.tools), protected_cases=plan.protected_cases,
            # docs/live-api/05 22절 일반 종료 규칙: 모델 행동으로 멈추면 마지막 측정 결과로 끝낸다(주호 확인 필요).
            automatic_stop=True)
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
        platform.orchestrator_app.state.service_source_configuration = plan.source.configuration
        platform.orchestrator_app.state.control_preflight = control_preflight
        install_package_service(platform, repository)
        # Host-only assembly metadata, not a new HTTP/Artifact/Trace contract.
        platform.orchestrator_app.state.approved_host_configuration_json = plan._configuration_json
        platform.orchestrator_app.state.host_policy_sha256 = plan.policy_sha256
        platform.orchestrator_app.state.host_api_token = host_token
        platform.orchestrator_app.state.host_api_auth_required = True
        platform.orchestrator_app.state.require_idempotency_key = True
        platform.orchestrator_app.state.approved_configuration_provider = lambda: approved_run_configuration(platform)
        install_host_api_documentation(platform.orchestrator_app)
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
        if platform._lifecycle.closed:
            raise ValueError
        return RunConfiguration.model_validate(parse_json(platform.orchestrator_app.state.approved_host_configuration_json))
    except Exception:
        raise HostFactoryError() from None
