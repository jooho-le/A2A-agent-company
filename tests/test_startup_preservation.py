"""Public startup facts are guidance; only the actual build establishes readiness."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.runtime.startup_preservation import with_startup_context, preservation_notice_for
from agents.runtime.developer_input import compact_fix_input
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario


ROOT = Path(__file__).resolve().parents[1]
STARTING = {p.name: p.read_bytes() for p in (ROOT / 'examples/starting-source/scn001-signup').glob('*.py')}
CONTRACT = get_scenario(SCN_001_ID).planner_contract()


def checkpoint(files=None):
    return SimpleNamespace(starting_source_files=lambda: dict(STARTING),
                           review_source_files=lambda **kw: dict(STARTING if files is None else files))


def failed_task(**changes):
    issue = dict(issueId='build', category='BUILD_CODE_FAILURE', codeVersion=2,
                 sourceArtifactId='src2', reportArtifactId='rep2')
    record = dict(artifactId='rep2', sourceArtifactId='src2', codeVersion=2,
                  executionOutcome='FAIL', failureKind='PRODUCT', diagnostics=[
                      dict(ruleId='SCN001_USER_TABLE_REQUIRED', normalizedLocation='main.py',
                           message='DO_NOT_COPY_RAW_SQL_OR_SECRET')])
    task = {'fixRequest': {'issues': [issue], 'inputArtifacts': [
        dict(artifactId='rep2', artifactType='BUILD_REPORT', record={**record, **changes})]}}
    return compact_fix_input(task)


def test_initial_context_describes_immutable_starting_source_without_copying_it():
    original = {'request': 'Implement signup'}
    before = deepcopy(original)
    value = with_startup_context(original, checkpoint(), CONTRACT)
    context = value['startingSourceStartup']
    assert original == before
    assert context['bindings']['application'] == {'toolPath': 'source/main.py', 'attribute': 'app'}
    assert context['bindings']['database']['dependency'] == 'get_db'
    assert context['bindings']['database']['urlEnv'] == 'DATABASE_URL'
    observed = context['observedStartingSource']
    assert observed['metadataInitializationSites'][0]['call'] == 'Base.metadata.create_all'
    assert observed['metadataInitializationSites'][0]['bindName'] == 'engine'
    assert observed['applicationLifecycleSites'][0]['lifespanName'] == 'lifespan'
    assert 'sqlite:///' not in str(context)
    assert 'startupRepairTargets' not in value


def test_measured_missing_table_has_exact_bound_actionable_target():
    value = with_startup_context(failed_task(), checkpoint(), CONTRACT)
    target, = value['activeRepairFocus']['brief']['startupRepairTargets']
    assert target['ruleId'] == 'SCN001_USER_TABLE_REQUIRED'
    assert (target['issueId'], target['codeVersion'], target['sourceArtifactId'], target['reportArtifactId']) == (
        'build', 2, 'src2', 'rep2')
    assert target['bindings']['userModel']['class'] == 'User'
    assert target['bindings']['database']['toolPath'] == 'source/database.py'
    assert target['readBeforeEditing'] == ['source/main.py', 'source/database.py', 'source/models.py']
    assert 'get_db' in target['instruction'] and 'QA' in target['instruction']
    assert 'DO_NOT_COPY_RAW' not in str(value['activeRepairFocus']['brief'])


@pytest.mark.parametrize('changes', [dict(artifactId='other'), dict(sourceArtifactId='src1'),
    dict(codeVersion=1), dict(executionOutcome='PASS'), dict(failureKind='INFRASTRUCTURE'),
    dict(diagnostics=[dict(ruleId='SCN001_NAME_UNDEFINED', message='private')])])
def test_unbound_or_unrelated_failure_never_gets_table_repair_targets(changes):
    value = with_startup_context(failed_task(**changes), checkpoint(), CONTRACT)
    assert 'startupRepairTargets' not in value['activeRepairFocus']['brief']


def test_no_selected_issue_no_target_even_if_old_failure_is_in_history():
    task = failed_task()
    task['activeRepairFocus']['brief']['targets'] = []
    assert 'startupRepairTargets' not in with_startup_context(task, checkpoint(), CONTRACT)['activeRepairFocus']['brief']


def test_full_rewrite_losing_bootstrap_gets_advice_without_mutation():
    files = {**STARTING, 'main.py': b'from fastapi import FastAPI\napp = FastAPI()\n'}
    before = deepcopy(files)
    notice = preservation_notice_for(checkpoint(files), CONTRACT, deadline_monotonic=1)
    value = notice(('source/main.py',))
    assert value['startupPreservationNotice']['reasonCode'] == 'STARTING_DATABASE_INITIALIZATION_NOT_OBSERVED'
    assert value['startupPreservationNotice']['verdict'] == 'NOT_EVALUATED'
    assert value['startupPreservationNotice']['readBeforeEditing'] == [
        'source/main.py', 'source/database.py', 'source/models.py']
    assert files == before
    assert notice(('source/tests/test_health.py',)) is None


def test_disconnected_lifespan_is_not_silenced_by_unused_create_all():
    files = {**STARTING, 'main.py': STARTING['main.py'].replace(b'FastAPI(lifespan=lifespan)', b'FastAPI()')}
    row = preservation_notice_for(checkpoint(files), CONTRACT) (('source/main.py',))
    assert row['startupPreservationNotice']['reasonCode'] == 'STARTING_LIFECYCLE_CONNECTION_NOT_OBSERVED'


def test_unchanged_or_equivalent_top_level_initialization_does_not_warn():
    assert preservation_notice_for(checkpoint(), CONTRACT)(('source/main.py',)) is None
    changed = STARTING['main.py'].replace(b'app = FastAPI(lifespan=lifespan)',
        b'Base.metadata.create_all(bind=engine)\napp = FastAPI()')
    assert preservation_notice_for(checkpoint({**STARTING, 'main.py': changed}), CONTRACT)(('source/main.py',)) is None


def test_no_baseline_or_non_fastapi_contract_is_left_to_actual_build():
    assert with_startup_context({}, SimpleNamespace(), CONTRACT) == {}
    assert preservation_notice_for(SimpleNamespace(), CONTRACT) is None
    other = deepcopy(CONTRACT)
    other['externalContract']['app']['framework'] = 'Other'
    assert with_startup_context({}, checkpoint(), other) == {}


def test_read_failures_or_unknown_ast_do_not_crash_or_invent_a_verdict():
    files = {**STARTING, 'main.py': b'not valid Python['}
    assert preservation_notice_for(checkpoint(files), CONTRACT)(('source/main.py',)) is None
    def denied(**kw):
        raise OSError('SECRET_ERROR')
    cp = checkpoint()
    cp.review_source_files = denied
    assert preservation_notice_for(cp, CONTRACT)(('source/main.py',)) is None


def test_contract_bindings_support_packages_and_never_render_invalid_paths():
    other = deepcopy(CONTRACT)
    other['externalContract']['app']['module'] = '../private'
    assert with_startup_context({}, checkpoint(), other) == {}
    assert preservation_notice_for(checkpoint(), other) is None


def test_package_binding_uses_the_existing_init_file_for_read_and_write_notices():
    contract = deepcopy(CONTRACT)
    contract['externalContract']['app']['module'] = 'company.api'
    initial = {**{n: b for n, b in STARTING.items() if n != 'main.py'},
               'company/api/__init__.py': STARTING['main.py']}
    cp = checkpoint()
    cp.starting_source_files = lambda: initial
    value = with_startup_context({}, cp, contract)
    assert value['startingSourceStartup']['bindings']['application']['toolPath'] == 'source/company/api/__init__.py'
    cp.review_source_files = lambda **kw: {**initial, 'company/api/__init__.py': b'from fastapi import FastAPI\napp=FastAPI()\n'}
    assert preservation_notice_for(cp, contract)(('source/company/api/__init__.py',))


def test_snapshot_review_does_not_execute_source_or_render_literal_values():
    files = {**STARTING, 'main.py': b'raise RuntimeError("SENSITIVE_LITERAL")\n' + STARTING['main.py']}
    cp = checkpoint()
    cp.starting_source_files = lambda: files
    result = with_startup_context({}, cp, CONTRACT)
    assert result['startingSourceStartup']['observedStartingSource']['metadataInitializationSites']
    assert 'SENSITIVE_LITERAL' not in str(result)


@pytest.mark.parametrize('single_phase', [None, 'IMPLEMENTING', 'FIXING'],
                         ids=['multi', 'single-implementing', 'single-fixing'])
def test_notice_reaches_model_after_real_rewrite_without_spending_correction(tmp_path, single_phase):
    import asyncio
    from hashlib import sha256
    import json
    import test_llm_runtime as fixtures
    from test_llm_runtime import FakeProvider, FakeToolExecutor, object_schema, text_response, tool_response
    from agents.core.identity import SingleAgentIdentity, SinglePhase
    from agents.llm.engine import LLMEngine
    from agents.llm.corrections import development_check
    from agents.roles.developer_contract import build_developer_output_contract, validate_developer_decision
    from agents.single.contracts import build_single_draft_contract
    from agents.single.prompts import PreparedSinglePrompt, build_single_system_prompt
    f = fixtures.LLMRuntimeTests()
    f.setUp()
    for name, content in STARTING.items():
        (tmp_path / name).write_bytes(content)
    path = tmp_path / 'main.py'
    cp = checkpoint()
    cp.review_source_files = lambda **kw: {p.name: p.read_bytes() for p in tmp_path.glob('*.py')}
    bad = 'from fastapi import FastAPI\napp = FastAPI()\n'
    good = STARTING['main.py'].decode() + '\n@app.post("/api/auth/signup")\ndef signup():\n    return {}\n'
    ready = {'kind': 'READY', 'summary': 'Candidate for actual build.', 'questions': [], 'rejection': None}
    notices = []

    def write(content, call_id):
        return tool_response(f.call(call_id, name='write_source_file', path='source/main.py', content=content,
                                   expectedSha256=sha256(path.read_bytes()).hexdigest()))

    def read_after_notice(request):
        rows = [json.loads(item['content']) for item in json.loads(request.input_items_json)
                if item.get('role') == 'user' and isinstance(item.get('content'), str)]
        notices.extend(r['startupPreservationNotice'] for r in rows if 'startupPreservationNotice' in r)
        assert len(notices) == 1
        assert notices[0]['verdict'] == 'NOT_EVALUATED'
        assert notices[0]['observedStartingSource']['metadataInitializationSites']
        return tool_response(f.call('read-rewritten-main', path='source/main.py'))

    def execute(call, args, _context):
        assert args['path'] == 'source/main.py'
        if call.name == 'read_project_file':
            return {'content': path.read_text(), 'sha256': sha256(path.read_bytes()).hexdigest()}
        assert args['expectedSha256'] == sha256(path.read_bytes()).hexdigest()
        path.write_text(args['content'])
        return {'changed': True}

    tools = (f.tool('read_project_file', output_schema=object_schema({
        'content': {'type': 'string'}, 'sha256': {'type': 'string'}}), source_output_fields=('content',)),
        f.tool('write_source_file', input_schema=object_schema({k: {'type': 'string'} for k in
            ('workspaceId', 'path', 'content', 'expectedSha256')}),
            output_schema=object_schema({'changed': {'type': 'boolean'}}), source_argument_fields=('content',)))
    task = with_startup_context({}, cp, CONTRACT)
    if single_phase:
        phase, role = SinglePhase(single_phase), SingleAgentIdentity.SINGLE_AGENT
        draft = build_single_draft_contract(phase, scenario=get_scenario(SCN_001_ID))
        prompt = PreparedSinglePrompt(phase, draft, build_single_system_prompt(), json.dumps({
            'trusted': {'phase': phase.value}, 'contextId': 'offline', 'history': [], 'taskInput': task}), 'offline')
        output, validate = draft.output, draft.validate
    else:
        phase, role = None, f.role
        from agents.roles.prompts import prepare_role_prompt
        prompt = prepare_role_prompt(role, task_input=task, metadata=f.metadata)
        output, validate = build_developer_output_contract(), validate_developer_decision
    provider = FakeProvider(tool_response(f.call('read-starting-main', path='source/main.py')),
        lambda req: write(bad, 'drop-startup'), read_after_notice,
        lambda req: write(good, 'restore-startup'), text_response(json.dumps(ready)))
    budget = f.budget(max_model_calls=5, max_tool_calls=4, max_output_corrections=0)
    engine = LLMEngine(role=role, single_phase=phase, provider=provider, tools=tools,
                       tool_executor=FakeToolExecutor(execute, execute, execute, execute))
    result = asyncio.run(engine.run(prompt=prompt, model=f.model, output=output, workspace_id=f.workspace_id,
        budget=budget, source_progress_check=lambda: True, output_check=development_check(validate),
        source_preservation_check=preservation_notice_for(cp, CONTRACT)))
    assert result.data == ready and path.read_text() == good
    assert (budget.model_calls, budget.tool_calls) == (5, 4)
    assert preservation_notice_for(cp, CONTRACT)(('source/main.py',)) is None
