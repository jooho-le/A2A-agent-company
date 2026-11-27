"""Real service-preparation failure reproduced without LLM, Docker or Source execution."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace as N
from unittest.mock import patch
from uuid import uuid4

from a2a.types import Task
from google.protobuf.json_format import ParseDict
import pytest

from mcp_tools.tools.build_config import BuildConfiguration, BuildConfigurationError, encode_build_configuration
from orchestrator.application.work_failures import read_work_failures
from orchestrator.core.security import redact_text
from orchestrator.domain import AgentRole, WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus
from orchestrator.sandbox.contracts import ExecutionProfile
from orchestrator.sandbox.service_start_contract import service_build_argv, service_build_diagnostics
from test_service_autonomy import service_design, ServiceHostRecipeTests


@pytest.mark.parametrize('prose', [
    'User(email: str unique, password_hash: str)',
    'Store a hash in password_hash: TEXT; never store plaintext.',
])
def test_plan_prose_does_not_enter_executable_arguments(prose):
    design = service_design(); design['dataModel'] = prose
    before = deepcopy(design)
    # Reproduce the old failure: raw JSON containing type-description prose
    # looks like a credential assignment to the unchanged argv validator.
    full_json = json.dumps(design, separators=(',', ':'), ensure_ascii=False)
    assert redact_text(full_json) != full_json
    args = service_build_argv(design)
    profile = ExecutionProfile(name='service-start', tool_name='run_build', argv=args)
    with pytest.raises(BuildConfigurationError):
        BuildConfiguration(profile=replace(profile, argv=(*args[:-1], full_json)))
    configuration = BuildConfiguration(profile=profile)
    assert encode_build_configuration(configuration)
    machine = json.loads(args[-1])
    assert machine == {key: design[key] for key in ('delivery', 'storage', 'endpoints')}
    assert design == before
    assert all(redact_text(arg) == arg and len(arg.encode()) <= 8192 for arg in args)
    assert prose not in args[-1]


def test_actual_host_developer_services_accept_annotated_data_model():
    owner = ServiceHostRecipeTests('runTest')
    try:
        owner.setUp()
        host, execution, binding = owner.configured(AgentRole.DEVELOPER, 'API')
        execution.requirement_artifact.payload['serviceDesign']['dataModel'] = 'User(email: str unique, password_hash: str)'
        with patch.object(host, '_binding', return_value=binding):
            services = host.developer(execution)
        assert services.configuration.binding == binding
        assert services.configuration.build_configuration.profile.name == 'service-start'
    finally:
        owner.doCleanups()


def test_executable_credential_guard_is_not_relaxed():
    profile = ExecutionProfile(name='service-start', tool_name='run_build',
        argv=('/usr/local/bin/python', '-c', 'pass', '--password=fixture-value'))
    with pytest.raises(BuildConfigurationError): BuildConfiguration(profile=profile)


def test_probe_diagnostics_remain_bound_to_machine_contract():
    design = service_design(); design['dataModel'] = 'password_hash: str'
    args = service_build_argv(design)
    marker = 'SERVICE_START:'+json.dumps({'code': 'SERVICE_READY_FAILED', 'path': 'main.py', 'line': 1, 'missing': None})
    assert service_build_diagnostics(argv=args, exit_code=1, stdout=marker,
        design=design, source_files={'main.py': b'pass\n'})
    tampered = (*args[:-1], args[-1].replace('"SQLITE"', '"NONE"'))
    assert not service_build_diagnostics(argv=tampered, exit_code=1, stdout=marker,
        design=design, source_files={'main.py': b'pass\n'})


@pytest.mark.parametrize(('context', 'code', 'role', 'refined'), [
    ('context', 'DEVELOPER_SERVICES_DENIED', AgentRole.DEVELOPER, True),
    ('another-context', 'DEVELOPER_SERVICES_DENIED', AgentRole.DEVELOPER, False),
    ('context', 'unapproved-private-body', AgentRole.DEVELOPER, False),
    ('context', 'DEVELOPER_SERVICES_DENIED', AgentRole.QA, False),
])
def test_old_generic_failure_is_refined_only_from_bound_developer_task(context, code, role, refined):
    task = ParseDict({'id': 'task', 'contextId': context, 'status': {'state': 'TASK_STATE_REJECTED',
        'message': {'parts': [{'data': {'code': code, 'privateDetail': 'never-copy-this-body'}}]}}}, Task())
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def get_task(self, _): return task
    controls = N(registry=N(require_base_url=lambda _: 'http://test'), client_factory=lambda _: Client())
    run = WorkflowRun(scenario_id=uuid4(), request_text='API', status=WorkflowStatus.HUMAN_REVIEW,
        resume_state=WorkflowStatus.IMPLEMENTING)
    step = WorkflowStep(run_id=run.run_id, agent_role=role, status=WorkflowStepStatus.FAILED,
        failure_code='AGENT_EXECUTION_FAILED', a2a_task_id='task', agent_context_id='context')
    data = asyncio.run(read_work_failures(None, controls, run, [step]))[0]
    assert data['code'] == ('DEVELOPER_SERVICES_DENIED' if refined else 'AGENT_EXECUTION_FAILED')
    assert 'never-copy-this-body' not in json.dumps(data)
