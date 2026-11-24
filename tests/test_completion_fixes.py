"""Fixes for Runs stopping midway (docs/live-api problems P1·P2·P4·P5, 2026-11-17).

P1 — read-only start-contract precheck before READY. Live Run 4f7ec809 spent two
of three fix attempts on start failures the model could only learn from the Host
build after READY; Run 8b49bebd v1 had both main.py without app and a User
without a primary key. The precheck must never be stricter than the Host build
probe, so every variant below is also run through the real probe (synthetic,
trusted fixtures only; generated code never runs on the host).

P2 — one bounded re-send after a provider failure with no model output (no
connection, HTTP 429/5xx). Off by default; 400s and timeouts after sending stay
fail-closed. P4 — the same starting-file sentence in Multi and Single prompts.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from fixture_cleanup import set_up_borrowed

from agents.llm import engine as engine_module
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.llm.corrections import checked_rejection, development_check, feedback, source_write_notice, wrote_handler_registration
from agents.llm.engine import LLMEngine
from agents.llm.openai_responses import OpenAIResponsesProvider
from agents.runtime.start_precheck import inspect, start_precheck_for
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.scn001_start_contract import SCN001_BUILD_PROBE
import test_llm_runtime as runtime_fixture
from test_llm_runtime import FakeProvider, text_response
from test_openai_responses import KEY, make_request
from test_start_contract_probe_routes import DATABASE, MAIN_HEAD, MODELS

CONTRACT = get_scenario(SCN_001_ID).planner_contract()['externalContract']
ROUTE = '@app.post("/api/auth/signup")\ndef signup():\n    return {}\n'
GOOD_MAIN = MAIN_HEAD + ROUTE
NO_PK_MODELS = MODELS.replace('    id = Column(Integer, primary_key=True)\n', '')
ROUTER_ONLY_MAIN = ('from fastapi import APIRouter\nimport models  # noqa: F401\nrouter = APIRouter()\n'
                    '@router.post("/api/auth/signup")\ndef signup():\n    return {}\n')
APPLICATION = GOOD_MAIN  # a valid app module that main.py may re-export


def test_self_import_cannot_substitute_for_creating_the_app(tmp_path):
    for name, text in {'main.py': ROUTER_ONLY_MAIN + '\nfrom main import app\n',
                       'database.py': DATABASE, 'models.py': MODELS}.items():
        (tmp_path / name).write_text(text)
    found = inspect(tmp_path, CONTRACT)
    assert any(i['reasonCode'] == 'ENTRYPOINT_SELF_IMPORT' for i in found)
    packet = feedback('START_CONTRACT_PRECHECK_FAILED', issues=found, remaining=0)
    assert 'imports its own not-yet-defined binding' in packet['hostCorrection']['instruction']

# variant -> (files overriding the good app, precheck issues). Flagged variants must fail the probe;
# variants with no issues are valid apps that the probe accepts and the precheck must not block.
VARIANTS = {
    'good': ({}, []),
    'router_only_main': ({'main.py': ROUTER_ONLY_MAIN}, [('main.py', 'APP_ATTRIBUTE_MISSING')]),
    'no_primary_key': ({'models.py': NO_PK_MODELS}, [('models.py', 'MODEL_PRIMARY_KEY_MISSING')]),
    'run3_v1_shape': ({'main.py': ROUTER_ONLY_MAIN, 'models.py': NO_PK_MODELS},
                      [('main.py', 'APP_ATTRIBUTE_MISSING'), ('models.py', 'MODEL_PRIMARY_KEY_MISSING')]),
    'syntax_error_in_tests': ({'tests/test_x.py': 'def broken(:\n    pass\n'},
                              [('tests/test_x.py', 'SOURCE_SYNTAX_INVALID')]),
    'missing_get_db': ({'database.py': DATABASE.replace('def get_db():', 'def get_session():')},
                       [('database.py', 'DB_DEPENDENCY_MISSING')]),
    'missing_models_file': ({'models.py': None}, [('models.py', 'USER_MODEL_MISSING')]),
    'app_from_module_attribute': ({'application.py': APPLICATION,
                                   'main.py': 'import application\napp = application.app\n'}, []),
    'app_via_global': ({'main.py': GOOD_MAIN.replace('app = FastAPI(lifespan=lifespan)\n',
                                                     'def _init():\n    global app\n    app = FastAPI(lifespan=lifespan)\n'
                                                     '_init()\n')}, []),
    'app_in_try': ({'main.py': GOOD_MAIN.replace('app = FastAPI(lifespan=lifespan)\n',
                                                 'try:\n    app = FastAPI(lifespan=lifespan)\nexcept Exception:\n    raise\n')}, []),
    'app_reexported': ({'application.py': APPLICATION, 'main.py': 'from application import app  # noqa: F401\n'}, []),
    'wildcard_main': ({'application.py': APPLICATION, 'main.py': 'from application import *  # noqa: F401,F403\n'}, []),
    'mixin_primary_key': ({'models.py': NO_PK_MODELS.replace(
        'class User(Base):', 'class IdMixin:\n    id = Column(Integer, primary_key=True)\nclass User(IdMixin, Base):')}, []),
    # 2026-11-18, live Run 1825a436: v1 and v2 rewrote main.py without the starting GET /, and v1 also
    # passed a string to Depends. Both builds failed and spent two of the three fix attempts.
    'ready_route_dropped': ({'main.py': GOOD_MAIN.replace('@app.get("/")\ndef health():\n    return {"status": "ok"}\n', '')},
                            [('main.py', 'READY_ROUTE_MISSING')]),
    'string_depends': ({'main.py': GOOD_MAIN.replace(ROUTE, 'from fastapi import Depends\n@app.post("/api/auth/signup")\n'
                                                     'def signup(db=Depends("get_db")):\n    return {}\n')},
                       [('main.py', 'DEPENDS_TARGET_INVALID')]),
    'run26_v1_shape': ({'main.py': GOOD_MAIN.replace('@app.get("/")\ndef health():\n    return {"status": "ok"}\n', '').replace(
        ROUTE, 'from fastapi import Depends\n@app.post("/api/auth/signup")\ndef signup(db=Depends("get_db")):\n    return {}\n')},
                       [('main.py', 'DEPENDS_TARGET_INVALID'), ('main.py', 'READY_ROUTE_MISSING')]),
    'ready_route_with_arguments': ({'main.py': GOOD_MAIN.replace('@app.get("/")', '@app.get("/", status_code=200)')}, []),
    'ready_route_added_without_decorator': ({'main.py': GOOD_MAIN.replace(
        '@app.get("/")\ndef health():\n    return {"status": "ok"}\n',
        'def health():\n    return {"status": "ok"}\napp.add_api_route("/", health, methods=["GET"])\n')}, []),
    'ready_route_in_included_router': ({'main.py': GOOD_MAIN.replace(
        '@app.get("/")\ndef health():\n    return {"status": "ok"}\n',
        'ready = APIRouter()\n@ready.get("/")\ndef health():\n    return {"status": "ok"}\napp.include_router(ready)\n')}, []),
    # 2026-11-18 gpt-4.1 diagnostic v1: only models.py was written, so main.py had no signup route.
    'signup_route_missing': ({'main.py': MAIN_HEAD}, [('main.py', 'SIGNUP_ROUTE_MISSING')]),
    'signup_route_get_only': ({'main.py': GOOD_MAIN.replace('@app.post("/api/auth/signup")', '@app.get("/api/auth/signup")')},
                              [('main.py', 'SIGNUP_ROUTE_MISSING')]),
    'signup_route_router_prefix': ({'main.py': MAIN_HEAD + 'auth = APIRouter(prefix="/api/auth")\n@auth.post("/signup")\n'
                                    'def signup():\n    return {}\napp.include_router(auth)\n'}, []),
    'string_depends_outside_route': ({'main.py': GOOD_MAIN + 'from fastapi import Depends\n'
                                      'def helper(db=Depends("get_db")):\n    return db\n'}, []),
    'primary_key_on_base': ({'database.py': DATABASE.replace(
        'class Base(DeclarativeBase):\n    pass\n',
        'from sqlalchemy import Column, Integer\nclass Base(DeclarativeBase):\n'
        '    __abstract__ = True\n    id = Column(Integer, primary_key=True)\n'),
        'models.py': NO_PK_MODELS}, []),
}


def build(files):
    tree = {'database.py': DATABASE, 'models.py': MODELS, 'main.py': GOOD_MAIN}
    tree.update(files)
    return {path: text for path, text in tree.items() if text is not None}


def write(root, tree):
    for path, text in tree.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)


def probe(tree):
    with tempfile.TemporaryDirectory() as snapshot, tempfile.TemporaryDirectory() as work:
        write(Path(snapshot), tree)
        script = SCN001_BUILD_PROBE.replace("'/snapshot'", repr(snapshot)).replace("'/snapshot/'", repr(snapshot + '/'))
        done = subprocess.run([sys.executable, '-I', '-c', script], cwd=work, capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout.strip().splitlines()[-1]


def precheck(tree):
    with tempfile.TemporaryDirectory() as directory:
        write(Path(directory), tree)
        return [(item['fieldPath'], item['reasonCode']) for item in inspect(directory, CONTRACT)]


_HANDLER = '@router.exception_handler(ValueError)\nasync def handle(request, exc):\n    pass\n'
_ROUTER = 'from fastapi import APIRouter as Router\nrouter = Router()\n'
_LOCK = 'fastapi==0.142.2\n'


def test_router_handler_at_startup_reaches_the_ready_correction(tmp_path):
    files = build({'main.py': GOOD_MAIN + 'from auth import router\n',
                   'auth.py': _ROUTER + _HANDLER, 'requirements.lock': _LOCK})
    write(tmp_path, files)
    assert probe(files) == (1, 'SCN001_ATTRIBUTE_UNAVAILABLE')
    check = development_check(lambda _: type('Decision', (), {'kind': 'READY'})(),
                              precheck=lambda: inspect(tmp_path, CONTRACT))
    rejection = checked_rejection(check({'kind': 'READY'}, ('edit_source_file',)))
    message = feedback(rejection['code'], issues=rejection['issues'], remaining=0)['hostCorrection']
    assert message['issues'] == [{'fieldPath': 'auth.py', 'reasonCode': 'APIROUTER_EXCEPTION_HANDLER_UNAVAILABLE',
                                  'lineNumber': 3, 'sourceExpression': 'router.exception_handler'}]
    assert 'standard FastAPI APIRouter' in message['instruction']
    # Both files are valid with the decorator on the actual FastAPI application.
    files.update({'auth.py': _ROUTER, 'main.py': GOOD_MAIN + _HANDLER.replace('@router.', '@app.')})
    write(tmp_path, files)
    assert check({'kind': 'READY'}, ('edit_source_file',)) is None
    assert probe(files) == (0, 'SCN001_START_CONTRACT_OK')


@pytest.mark.parametrize('entry, module', [
    ('import auth', 'from fastapi import FastAPI\nrouter = FastAPI()\n' + _HANDLER),
    ('import auth', _ROUTER + 'from fastapi import FastAPI\nrouter = FastAPI()\n' + _HANDLER),
    ('import auth', _ROUTER + 'class Custom(Router):\n    def exception_handler(self, err):\n'
                              '        return lambda fn: fn\nrouter = Custom()\n' + _HANDLER),
    ('import auth', _ROUTER + 'def install(r):\n    r.exception_handler = lambda err: lambda fn: fn\n'
                              'install(router)\n' + _HANDLER),
    ('if False:\n    import auth', _ROUTER + _HANDLER),
    ('import auth', _ROUTER + 'if False:\n' + ''.join('    ' + line for line in _HANDLER.splitlines(True))),
])
def test_start_precheck_accepts_working_handler_variants(entry, module):
    files = build({'main.py': GOOD_MAIN + entry + '\n', 'auth.py': module, 'requirements.lock': _LOCK})
    assert precheck(files) == []
    assert probe(files) == (0, 'SCN001_START_CONTRACT_OK')


def test_router_guard_defers_unknown_framework_version_and_shadowed_package():
    files = build({'main.py': GOOD_MAIN + 'import auth\n', 'auth.py': _ROUTER + _HANDLER,
                   'requirements.lock': 'fastapi==1.0.0\n'})
    assert precheck(files) == []
    files.update({'requirements.lock': _LOCK, 'fastapi/__init__.py': ''})
    assert precheck(files) == []


def test_router_feedback_accepts_only_a_host_line_number_and_dotted_identifier():
    base = {'fieldPath': 'auth.py', 'reasonCode': 'APIROUTER_EXCEPTION_HANDLER_UNAVAILABLE'}
    def normalized(kind, line, expression):
        return checked_rejection({'code': kind, 'issues': [
            {**base, 'lineNumber': line, 'sourceExpression': expression}]})['issues']
    assert normalized('START_CONTRACT_PRECHECK_FAILED', 0, 'router.exception_handler') == [base]
    assert normalized('START_CONTRACT_PRECHECK_FAILED', 3, 'router.exception_handler(ValueError)') == [base]
    assert normalized('OUTPUT_CONTRACT_INVALID', 3, 'router.exception_handler') == [base]


def test_early_router_notice_is_scoped_to_successful_source_operations():
    assert wrote_handler_registration('write_source_file', {'path': 'auth.py', 'content': _HANDLER})
    assert wrote_handler_registration('edit_source_file', {'path': 'auth.py', 'newText': _HANDLER})
    assert not wrote_handler_registration('edit_source_file', {'path': 'auth.py', 'oldText': _HANDLER, 'newText': ''})
    assert not wrote_handler_registration('write_test_file', {'path': 'test.py', 'content': _HANDLER})
    assert not wrote_handler_registration('write_source_file', {'path': 'notes.md', 'content': _HANDLER})
    other_file = {'reasonCode': 'APIROUTER_EXCEPTION_HANDLER_UNAVAILABLE', 'fieldPath': 'other.py'}
    assert source_write_notice(lambda: [other_file], {'auth.py'}) is None
    assert source_write_notice(mock.Mock(side_effect=OSError), {'auth.py'}) is None


@pytest.mark.parametrize('content, observed', [
    ('{"detail": err.errors()}', True), ('err.errors()', True),
    ('{"detail": len(err.errors())}', False), ('{"detail": "invalid"}', False),
    ('{"detail": other.errors()}', False)])
def test_router_notice_reports_only_the_direct_response_shape(tmp_path, content, observed):
    handler = '@router.exception_handler(ValueError)\nasync def handle(request, err):\n'
    handler += f'    return JSONResponse(status_code=422, content={content})\n'
    write(tmp_path, build({'main.py': GOOD_MAIN + 'import auth\n', 'auth.py': _ROUTER + handler, 'requirements.lock': _LOCK}))
    note = source_write_notice(lambda: inspect(tmp_path, CONTRACT), {'auth.py'})['hostSourceCheck']
    assert (note['issues'][0].get('handlerReturnShape') == 'ERRORS_CALL_IN_JSONRESPONSE_CONTENT') == observed
    assert ('Validation errors can contain original inputs' in note['instruction']) == observed


@pytest.mark.parametrize('name', sorted(VARIANTS))
def test_precheck_reports_only_what_the_build_probe_certainly_rejects(name):
    files, expected = VARIANTS[name]
    tree = build(files)
    found = precheck(tree)
    code, marker = probe(tree)
    assert found == (expected or [])
    if found:
        assert code == 1, marker          # never stricter than the Host build
    else:
        assert (code, marker) == (0, 'SCN001_START_CONTRACT_OK')


def test_precheck_never_runs_the_source_and_survives_odd_trees():
    marker = Path(tempfile.gettempdir()) / 'precheck-must-not-run-this'
    tree = build({'main.py': GOOD_MAIN + f'open({str(marker)!r}, "w").write("ran")\n',
                  'notes.txt': 'not python', 'bad_encoding.py': None})
    with tempfile.TemporaryDirectory() as directory:
        write(Path(directory), tree)
        (Path(directory) / 'latin.py').write_bytes(b'x = "\xff"\n')  # not UTF-8, no coding cookie
        (Path(directory) / 'link.py').symlink_to('/etc/hosts')
        found = inspect(directory, CONTRACT)
    assert not marker.exists()
    assert found == [{'fieldPath': 'latin.py', 'reasonCode': 'SOURCE_SYNTAX_INVALID'}]
    assert inspect('relative/path', CONTRACT) == [] and inspect('/nonexistent-dir', CONTRACT) == []
    assert start_precheck_for(object(), {'externalContract': CONTRACT}) is None


def test_development_check_runs_the_precheck_only_for_ready_and_fails_open():
    calls = []

    def fake_precheck():
        calls.append(1)
        return [{'fieldPath': 'main.py', 'reasonCode': 'APP_ATTRIBUTE_MISSING'}]

    ready = type('D', (), {'kind': 'READY'})()
    check = development_check(lambda data: ready, precheck=fake_precheck)
    written = ('write_source_file',)
    assert check({'kind': 'READY'}, written) == {
        'code': 'START_CONTRACT_PRECHECK_FAILED', 'issues': [{'fieldPath': 'main.py', 'reasonCode': 'APP_ATTRIBUTE_MISSING'}]}
    assert checked_rejection(check({'kind': 'READY'}, written))['code'] == 'START_CONTRACT_PRECHECK_FAILED'
    assert check({'kind': 'READY'}, ()) == {'code': 'READY_WITHOUT_CHANGES', 'issues': []}  # existing rule first
    assert len(calls) == 2

    def broken():
        raise OSError('disk')
    assert development_check(lambda data: ready, precheck=broken)({'kind': 'READY'}, written) is None
    assert development_check(lambda data: ready)({'kind': 'READY'}, written) is None


class EngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = runtime_fixture.LLMRuntimeTests('runTest')
        self.f.setUp()
        patcher = mock.patch.object(engine_module, 'PROVIDER_RETRY_DELAY_SECONDS', 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def run_engine(self, provider, *, check=None, routes=None, **limits):
        return await LLMEngine(role=self.f.role, provider=provider).run(
            prompt=self.f.prompt, model=self.f.model, output=self.f.output,
            budget=self.f.budget(max_model_calls=6, **limits), workspace_id=self.f.workspace_id,
            output_check=check, recovery_sink=None if routes is None else routes.append)

    async def test_settled_source_write_exposes_router_error_before_ready_without_a_correction(self):
        from agents.roles.developer_contract import build_developer_output_contract, validate_developer_decision
        from test_development_progress import READY
        f = self.f
        tool = f.tool('write_source_file', source_argument_fields=('content',),
                      input_schema=runtime_fixture.object_schema({k: {'type': 'string'} for k in ('workspaceId', 'path', 'content')}),
                      output_schema=runtime_fixture.object_schema({'changed': {'type': 'boolean'}}))
        def change(call_id, path, content):
            return f.call(call_id, 'write_source_file', path=path, content=content)
        def notices(request):
            items = json.loads(request.input_items_json)
            return [d['hostSourceCheck'] for item in items if item.get('role') == 'user'
                    and 'hostSourceCheck' in (d := json.loads(item['content']))]
        def fix(request):
            note, = notices(request)
            self.assertEqual(note['issues'], [{'fieldPath': 'auth.py', 'reasonCode': 'APIROUTER_EXCEPTION_HANDLER_UNAVAILABLE',
                                                'lineNumber': 3, 'sourceExpression': 'router.exception_handler'}])
            return runtime_fixture.tool_response(change('remove', 'auth.py', _ROUTER),
                change('register', 'main.py', GOOD_MAIN + _HANDLER.replace('@router.', '@app.')))
        broken = _ROUTER + _HANDLER
        provider = FakeProvider(runtime_fixture.tool_response(change('bad', 'auth.py', broken)),
            runtime_fixture.tool_response(change('no-op', 'auth.py', broken)),
            runtime_fixture.tool_response(change('same-error', 'auth.py', broken + '\n')), fix, READY)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, build({'main.py': GOOD_MAIN + 'from auth import router\n', 'auth.py': _ROUTER, 'requirements.lock': _LOCK}))
            def apply(call, args, context):
                path = root / args['path']
                changed = path.read_text() != args['content']
                path.write_text(args['content'])
                return {'changed': changed}
            check = lambda: inspect(root, CONTRACT)
            early = mock.Mock(side_effect=check)
            budget = f.budget(max_output_corrections=0, max_model_calls=5)
            result = await LLMEngine(role=f.role, provider=provider, tools=(tool,),
                tool_executor=runtime_fixture.FakeToolExecutor(*([apply] * 5))).run(
                prompt=f.prompt, model=f.model, output=build_developer_output_contract(), budget=budget, workspace_id=f.workspace_id,
                source_write_precheck=early, output_check=development_check(validate_developer_decision, precheck=check))
            self.assertEqual(early.call_count, 3)
            self.assertFalse(check())
        self.assertEqual(result.data['kind'], 'READY')
        self.assertEqual([len(notices(req)) for req in provider.requests], [0, 1, 1, 1, 1])
        self.assertEqual(budget.model_calls, 5)

    async def test_precheck_rejection_buys_one_regeneration_then_returns_to_the_caller(self):
        issues = [{'fieldPath': 'main.py', 'reasonCode': 'APP_ATTRIBUTE_MISSING'}]
        check = lambda data, executed=(): {'code': 'START_CONTRACT_PRECHECK_FAILED', 'issues': issues}
        provider = FakeProvider(text_response(), text_response(), text_response())
        result = await self.run_engine(provider, check=check, max_output_corrections=2)
        self.assertEqual(len(provider.requests), 2)       # same issues twice -> no second correction
        feedback = json.loads(json.loads(provider.requests[1].input_items_json)[-1]['content'])['hostCorrection']
        self.assertEqual((feedback['code'], feedback['issues']), ('START_CONTRACT_PRECHECK_FAILED', issues))
        self.assertEqual(result.data, {'answer': 'done'})

    async def test_a_transient_provider_failure_is_sent_once_more_when_enabled(self):
        routes = []
        provider = FakeProvider(LLMRuntimeError(LLMErrorCode.PROVIDER, transient=True), text_response())
        result = await self.run_engine(provider, routes=routes, max_provider_retries=1)
        self.assertEqual(result.data, {'answer': 'done'})
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(provider.requests[0].input_items_json, provider.requests[1].input_items_json)  # same request
        self.assertEqual([record.outcome for record in result.records], ['LLM_PROVIDER_ERROR', 'completed'])
        calls = [route for route in routes if route.kind == 'CALL']
        self.assertEqual((calls[1].mode, calls[1].code), ('REGENERATION', 'PROVIDER_TRANSIENT_RETRY'))
        decision = [route for route in routes if route.kind == 'DECISION'][0]
        self.assertEqual((decision.category, decision.action), ('EXECUTION_UNCERTAIN', 'REGENERATE'))

    async def test_off_by_default_non_transient_and_second_failure_stay_fail_closed(self):
        cases = (({}, (LLMRuntimeError(LLMErrorCode.PROVIDER, transient=True), text_response()), 1),
                 ({'max_provider_retries': 1}, (LLMRuntimeError(LLMErrorCode.PROVIDER), text_response()), 1),
                 ({'max_provider_retries': 1}, (LLMRuntimeError(LLMErrorCode.TIMEOUT), text_response()), 1),
                 ({'max_provider_retries': 1}, (LLMRuntimeError(LLMErrorCode.PROVIDER, transient=True),
                                                LLMRuntimeError(LLMErrorCode.PROVIDER, transient=True), text_response()), 2))
        for limits, script, sent in cases:
            routes = []
            provider = FakeProvider(*script)
            with self.subTest(limits=limits, sent=sent), self.assertRaises(LLMRuntimeError) as raised:
                await self.run_engine(provider, routes=routes, **limits)
            self.assertIn(raised.exception.code, (LLMErrorCode.PROVIDER, LLMErrorCode.TIMEOUT))
            self.assertEqual(len(provider.requests), sent)
            self.assertEqual(routes[-1].code, raised.exception.code.value)   # the original failure is the final record

    def test_transient_is_only_kept_for_provider_and_timeout_codes(self):
        self.assertFalse(LLMRuntimeError(LLMErrorCode.SCHEMA, transient=True).transient)
        self.assertFalse(LLMRuntimeError(LLMErrorCode.PROVIDER, transient='yes').transient)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def error(self, handler):
        provider = OpenAIResponsesProvider(SecretStr(KEY), transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(LLMRuntimeError) as caught:
                await provider.complete(make_request())
        finally:
            await provider.aclose()
        return caught.exception.code, caught.exception.transient

    async def test_only_failures_with_no_model_output_are_marked_transient(self):
        def status(code):
            return lambda request: httpx.Response(code, text='SYNTHETIC_ERROR_BODY_SECRET')

        def raises(error):
            def handler(request):
                raise error('synthetic', request=request)
            return handler
        expected = {
            'rate_limit': (status(429), (LLMErrorCode.PROVIDER, True)),
            'server_error': (status(500), (LLMErrorCode.PROVIDER, True)),
            'unavailable': (status(503), (LLMErrorCode.PROVIDER, True)),
            'bad_request': (status(400), (LLMErrorCode.PROVIDER, False)),
            'not_found': (status(404), (LLMErrorCode.PROVIDER, False)),
            'auth': (status(401), (LLMErrorCode.AUTH, False)),
            'connect_error': (raises(httpx.ConnectError), (LLMErrorCode.PROVIDER, True)),
            'connect_timeout': (raises(httpx.ConnectTimeout), (LLMErrorCode.TIMEOUT, True)),
            'read_timeout': (raises(httpx.ReadTimeout), (LLMErrorCode.TIMEOUT, False)),
            'disconnected': (raises(httpx.RemoteProtocolError), (LLMErrorCode.PROVIDER, False)),
        }
        for name, (handler, outcome) in expected.items():
            with self.subTest(name=name):
                self.assertEqual(await self.error(handler), outcome)


def test_multi_and_single_prompts_carry_the_same_starting_file_rule():
    from agents.roles.prompts import build_system_prompt
    from agents.single.prompts import build_single_system_prompt
    from orchestrator.domain.states import AgentRole
    multi = build_system_prompt(AgentRole.DEVELOPER)
    single = build_single_system_prompt()
    for marker in ('NO_CHANGE_REQUIRED', '실패 경로에 필요한 파일을 한 응답에서 함께 읽고'):
        multi_rules = [line for line in multi.splitlines() if marker in line]
        single_rules = [line for line in single.splitlines() if marker in line]
        assert len(multi_rules) == 1
        assert single_rules == multi_rules
    assert '수정 단계에는 기존 파일을 읽고' not in single


def test_contract_feedback_points_at_a_control_character_without_echoing_text():
    """Live Run 3754980f: Security wrote U+0000 for the middle dot of an acceptance criterion. The
    role contract raised one fieldless reason, the feedback listed nothing, the model repeated the same
    output and the Run ended at v3. The feedback now names the field and a fixed reason code."""
    from agents.llm.corrections import contract_check
    from agents.roles.security_contract import validate_security_decision
    from test_security_agent_contract import FINDING_IDS, REQUIREMENT_IDS, SOURCE_PATHS, decision, requirement
    check = contract_check(lambda data: validate_security_decision(data, REQUIREMENT_IDS, FINDING_IDS, SOURCE_PATHS))
    broken = decision(requirementReviews=[requirement(0, rationale='승인한 알고리즘\x00파라미터 충족'),
                                          requirement(1), requirement(2)])
    rejection = check(broken)
    assert rejection == {'code': 'OUTPUT_CONTRACT_INVALID',
                         'issues': [{'fieldPath': '/requirementReviews/0/rationale', 'reasonCode': 'TEXT_CONTROL_CHARACTER'}]}
    assert '파라미터' not in json.dumps(checked_rejection(rejection), ensure_ascii=False)
    assert check(decision()) is None                                        # valid output: nothing to report
    other = decision(requirementReviews=[requirement(0)])                  # a different violation: still fieldless
    assert check(other) == {'code': 'OUTPUT_CONTRACT_INVALID', 'issues': []}


# ---------------------------------------------------------------- P5 예상 지점 1: 기대 결과 문장 검사를 교정 안으로

class CaseExpectationIssueTests(unittest.TestCase):
    """The early pointer check agrees with ``validate_generated_case_expectations`` on every case the
    existing scope tests deny or admit (same fixtures), and names the field without echoing values."""

    def setUp(self):
        import test_requirement_scope as scope_fixture
        self.f = scope_fixture.RequirementScopeTests('test_exact_generated_criteria_are_admitted_for_both_validation_roles')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def rows(self, cases):
        return [{'requirementId': str(case.requirement_id), 'expectedResult': case.expected_result} for case in cases]

    def judge(self, cases, role, field='cases'):
        from agents.runtime.requirement_scope import RequirementScopeError, generated_case_expectation_issues
        issues = generated_case_expectation_issues({field: self.rows(cases)}, field=field,
            configuration=self.f.configuration, metadata=self.f.role_metadata(role), role=role)
        try:
            self.f.validate_cases(tuple(cases), role)
            authoritative = []
        except RequirementScopeError:
            authoritative = ['DENIED']
        self.assertEqual(bool(issues), bool(authoritative), (issues, cases))
        return [(item['fieldPath'], item['reasonCode']) for item in issues]

    def test_agrees_with_the_authoritative_check_and_points_at_the_case(self):
        from dataclasses import replace as swap
        from uuid import uuid4
        for role in (AgentRole.QA, AgentRole.SECURITY):
            self.assertEqual(self.judge(self.f.cases(role), role), [])
        cases = self.f.cases(AgentRole.QA)
        middle_dot = swap(cases[0], expected_result=cases[0].expected_result.replace('·', '・') + ' ')
        self.assertEqual(self.judge((middle_dot, *cases[1:]), AgentRole.QA),
                         [('/cases/0/expectedResult', 'EXPECTED_RESULT_NOT_ACCEPTANCE_CRITERION')])
        self.assertEqual(self.judge((swap(cases[1], expected_result=cases[0].expected_result), *cases[2:], cases[0]),
                                    AgentRole.QA)[0][1], 'EXPECTED_RESULT_NOT_ACCEPTANCE_CRITERION')
        self.assertEqual(self.judge(cases[:-1], AgentRole.QA), [('/cases', 'ASSIGNED_REQUIREMENT_WITHOUT_CASE')])
        self.assertIn(('/cases/0/requirementId', 'CASE_REQUIREMENT_NOT_ASSIGNED'),
                      self.judge((swap(cases[0], requirement_id=uuid4()), *cases[1:]), AgentRole.QA))
        self.assertIn(('/cases', 'CASE_COUNT_INVALID'), self.judge(cases * 257, AgentRole.QA))

    def test_cannot_judge_means_no_report_and_the_rule_wraps_the_contract(self):
        from agents.llm.corrections import case_expectation_check
        from agents.runtime.requirement_scope import generated_case_expectation_issues
        assert generated_case_expectation_issues({'cases': []}, field='cases', configuration={'approvalStatus': 'APPROVED'},
            metadata=self.f.role_metadata(AgentRole.QA), role=AgentRole.QA) == []
        ready = type('D', (), {'kind': 'READY'})()
        issue = [{'fieldPath': '/cases/0/expectedResult', 'reasonCode': 'EXPECTED_RESULT_NOT_ACCEPTANCE_CRITERION'}]
        check = case_expectation_check(lambda data: ready, lambda data: issue)
        assert check({'kind': 'READY'}) == {'code': 'CASE_EXPECTATION_INVALID', 'issues': issue}
        assert checked_rejection(check({'kind': 'READY'}))['code'] == 'CASE_EXPECTATION_INVALID'
        assert check({'kind': 'INPUT_REQUIRED'}) is None
        # A Host inspector crash cannot hide a prerequisite or grant admission.
        with self.assertRaises(ZeroDivisionError):
            case_expectation_check(lambda data: ready, lambda data: 1 / 0)({'kind': 'READY'})
        assert case_expectation_check(lambda data: ready)({'kind': 'READY'}) is None


def _bad_first_case(final):
    """The same final answer with the first case's middle dot swapped for a look-alike."""
    def response(request):
        data = json.loads(final(request).output_text)
        key = 'cases' if 'cases' in data else 'testCases'
        data[key][0]['expectedResult'] = data[key][0]['expectedResult'].replace('·', '・') + ' '
        return text_response(json.dumps(data, ensure_ascii=False))
    return response


def test_borrowed_async_fixture_restores_environment_after_partial_setup(monkeypatch):
    import os
    key = 'CAPSTONE_TEST_FIXTURE_CLEANUP_PROBE'
    monkeypatch.setenv(key, 'initial')
    class Borrowed(unittest.IsolatedAsyncioTestCase):
        def setUp(self):
            cleared = mock.patch.dict(os.environ, {}, clear=True)
            cleared.start()
            self.addCleanup(cleared.stop)
            raise ValueError('incomplete setup')
    owner = unittest.TestCase()
    try:
        with pytest.raises(ValueError, match='incomplete setup'):
            set_up_borrowed(owner, Borrowed())
        assert key not in os.environ
    finally:
        assert owner.doCleanups()
    assert os.environ[key] == 'initial'


class CaseExpectationPipelineTests(unittest.IsolatedAsyncioTestCase):
    """Full offline pipeline (real A2A/Host/Git/SQLite; fake model): one look-alike character in an
    expectedResult used to fail the validator step outright. Now it costs one correction and the Run
    reaches its final verdict — for Multi QA, Multi Security and Single QA alike.

    The shared fixture's seed has no main.py/database.py/models.py (its build is faked), so the P1
    start precheck would rightly reject its Developer READY; it is switched off here and tested above."""

    def setUp(self):
        for target in ('agents.runtime.developer.start_precheck_for', 'agents.single.runtime.start_precheck_for'):
            patcher = mock.patch(target, lambda *args, **kwargs: None)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def run_multi(self, role, corrections):
        from orchestrator.domain import WorkflowStatus
        import test_owned_agent_pipeline as multi_fixture
        base = multi_fixture.OwnedAgentPipelineTests('runTest')
        set_up_borrowed(self, base)
        base.security_tests = True
        providers = base.providers()
        script = providers[role].script
        script.insert(len(script) - 1, _bad_first_case(script[-1]))
        async with base.running_platform(providers, model_cap=32, corrections=corrections) as (client, _):
            response = await client.post('/api/v1/runs', json=base.submission())
            self.assertEqual(response.status_code, 201, response.text)
            run = base.repository.get_run(UUID(response.json()['run']['runId']))
        return run.status is WorkflowStatus.FINISHED and getattr(run.verdict, 'value', None) == 'SUCCESS'

    async def test_multi_qa_and_security_recover_with_one_correction(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            with self.subTest(role=role.value):
                self.assertTrue(await self.run_multi(role, corrections=2))

    async def test_without_corrections_the_same_slip_still_stops_the_run(self):
        self.assertFalse(await self.run_multi(AgentRole.QA, corrections=0))

    async def run_single(self, *, inject, corrections):
        from semantic_helpers import SemanticFakeProvider
        import test_single_agent_pipeline as single_fixture
        base = single_fixture.SingleAgentPipelineTests('runTest')
        set_up_borrowed(self, base)
        f = base.f
        providers = f.providers()
        if inject:
            script = providers[AgentRole.QA].script
            script.insert(len(script) - 1, _bad_first_case(script[-1]))
        provider = SemanticFakeProvider(*(action for role in AgentRole for action in providers[role].script))
        async with base.running(cap=32, provider=provider, corrections=corrections) as (client, _):
            response = await client.post('/api/v1/runs', json=f.submission())
            self.assertEqual(response.status_code, 201, response.text)
            run = f.repository.get_run(UUID(response.json()['run']['runId']))
            steps = {step.agent_role: step.status.value for step in f.repository.list_steps(run.run_id)}
        return (run.status, getattr(run.verdict, 'value', None)), steps[AgentRole.QA], len(provider.requests)

    async def test_single_qa_phase_recovers_with_one_correction(self):
        # This Single fixture's normal path ends at its own terminal state; the slip must reach that
        # same end with exactly one extra model call, and stop the QA phase when corrections are off.
        baseline, qa, calls = await self.run_single(inject=False, corrections=2)
        self.assertEqual(qa, 'SUCCEEDED')
        self.assertEqual(await self.run_single(inject=True, corrections=2), (baseline, 'SUCCEEDED', calls + 1))
        self.assertEqual((await self.run_single(inject=True, corrections=0))[1], 'FAILED')
