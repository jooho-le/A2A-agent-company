"""Ordered mount regression using handwritten public-contract fixtures only."""
from copy import deepcopy

import pytest

from agents.llm.corrections import checked_rejection, feedback
from agents.runtime.start_precheck import inspect
from agents.runtime.startup_preservation import preservation_notice_for, with_startup_context
from test_startup_preservation import CONTRACT, STARTING, checkpoint, failed_task


CHILD = b'''from fastapi import FastAPI, APIRouter
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
router = APIRouter()
@router.post('/api/auth/signup')
def signup():
    return {'created': True}
app = FastAPI()
@app.exception_handler(RequestValidationError)
async def invalid(request, exc):
    return JSONResponse({'detail': 'invalid'}, status_code=422)
app.include_router(router)
'''


def source(placement='before', child=CHILD):
    main = b'from child import app as child_app\n' + STARTING['main.py']
    if placement == 'before':
        main = main.replace(b'app = FastAPI(lifespan=lifespan)',
                            b'app = FastAPI(lifespan=lifespan)\napp.mount("/", child_app)')
    else:
        main += b'\napp.mount("/", child_app)\n'
    return {**STARTING, 'main.py': main, 'child.py': child}


def review(tmp_path, files):
    for name, content in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    return inspect(tmp_path, CONTRACT['externalContract'])


def test_known_root_mount_cannot_hide_readiness_behind_a_decorator(tmp_path):
    files = source()
    before = deepcopy(files)
    issues = review(tmp_path, files)
    shadowed = [row for row in issues if row['reasonCode'] == 'READY_ROUTE_SHADOWED']
    assert len(shadowed) == 1
    assert shadowed[0]['sourcePath'] == 'main.py' and shadowed[0]['lineNumber'] > 0
    assert files == before
    packet = feedback('START_CONTRACT_PRECHECK_FAILED',
        issues=checked_rejection({'code': 'START_CONTRACT_PRECHECK_FAILED', 'issues': shadowed})['issues'], remaining=1)
    assert 'READY_ROUTE_SHADOWED' in str(packet) and 'before' in str(packet)
    assert CHILD.decode() not in str(packet)


@pytest.mark.parametrize('variant', ['after', 'child_ready', 'nonroot_mount', 'child_custom', 'imported_router',
                                   'middleware', 'lifecycle_routes', 'alias_mutation', 'earlier_router',
                                   'earlier_api_route', 'decorator_alias', 'child_error_alias'])
def test_valid_or_unknown_route_wiring_is_left_to_actual_build(tmp_path, variant):
    files = source('after' if variant == 'after' else 'before')
    if variant == 'child_ready':
        files['child.py'] += b"\n@app.get('/')\ndef child_ready(): return {'ready': True}\n"
    elif variant == 'nonroot_mount':
        files['main.py'] = files['main.py'].replace(b'app.mount("/",', b'app.mount("/api",')
    elif variant == 'child_custom':
        files['child.py'] = b'from custom import make_app\napp=make_app()\n'
    elif variant == 'imported_router':
        files['child.py'] = CHILD.replace(b'router = APIRouter()', b'from routes import router')
    elif variant == 'middleware':
        files['main.py'] += (b'\n@app.middleware("http")\nasync def intercept(request, call_next):\n'
                             b' return await call_next(request)\n')
    elif variant == 'lifecycle_routes':
        files['main.py'] = files['main.py'].replace(b'    yield', b'    _app.add_api_route("/", health)\n    yield')
    elif variant == 'alias_mutation':
        files['main.py'] += b'\nother = app\nother.routes.insert(0, custom_route)\n'
    elif variant == 'earlier_router':
        files['main.py'] = files['main.py'].replace(b'app.mount("/",', b'app.include_router(extra)\napp.mount("/",')
    elif variant == 'earlier_api_route':
        files['main.py'] = files['main.py'].replace(b'app.mount("/",',
            b'@app.api_route("/")\ndef first_ready(): return {}\napp.mount("/",')
    elif variant == 'decorator_alias':
        files['main.py'] = files['main.py'].replace(b'app.mount("/",',
            b'register = app.get("/")\n@register\ndef first_ready(): return {}\napp.mount("/",')
    elif variant == 'child_error_alias':
        files['child.py'] = CHILD.replace(b'from fastapi.exceptions import RequestValidationError',
            b'from starlette.exceptions import HTTPException as RequestValidationError').replace(
                b'status_code=422', b'status_code=200')
    assert not any(i['reasonCode'] == 'READY_ROUTE_SHADOWED' for i in review(tmp_path, files))


def test_ready_route_placement_advice_never_claims_measured_success():
    cp = checkpoint(source())
    context = with_startup_context({}, cp, CONTRACT)['startingSourceStartup']
    assert context['readyCheck'] == 'GET /'
    assert context['observedStartingSource']['readyRouteSites']
    note = preservation_notice_for(cp, CONTRACT)(('source/main.py',))['startupPreservationNotice']
    assert note['reasonCode'] == 'STARTING_READY_ROUTE_MAY_BE_SHADOWED'
    assert note['verdict'] == 'NOT_EVALUATED'
    assert 'before' in note['instruction']
    assert CHILD.decode() not in str(note)


def test_readiness_build_failure_has_same_exact_source_and_report_binding():
    task = failed_task(diagnostics=[{'ruleId': 'SCN001_READY_ENDPOINT_REQUIRED', 'message': 'PRIVATE_ERROR'}])
    row, = with_startup_context(task, checkpoint(), CONTRACT)['activeRepairFocus']['brief']['startupRepairTargets']
    assert row['ruleId'] == 'SCN001_READY_ENDPOINT_REQUIRED'
    assert (row['sourceArtifactId'], row['reportArtifactId'], row['codeVersion']) == ('src2', 'rep2', 2)
    assert 'registration order' in row['instruction']
    assert 'PRIVATE_ERROR' not in str(row)


def test_unbound_readiness_failure_does_not_become_a_repair_instruction():
    task = failed_task(diagnostics=[{'ruleId': 'SCN001_READY_ENDPOINT_REQUIRED'}], sourceArtifactId='stale')
    assert 'startupRepairTargets' not in with_startup_context(task, checkpoint(), CONTRACT)['activeRepairFocus']['brief']
