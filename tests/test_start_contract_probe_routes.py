"""The SCN-001 build probe must accept every correct way to serve POST /api/auth/signup.

Regression (live Run c2a3f5e0, 2026-11-17): FastAPI 0.142 keeps an included
router as one ``_IncludedRouter`` entry without a ``path``, so the old
``app.routes`` scan reported SCN001_SIGNUP_ROUTE_REQUIRED for correct apps that
use APIRouter/include_router. The Developer then "fixed" a correct route and
broke it. The probe now asks over HTTP. Synthetic trusted fixtures only; the
probe text is the exact Host probe with only the snapshot directory swapped.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from orchestrator.sandbox.scn001_start_contract import SCN001_BUILD_PROBE

DATABASE = '''import os
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./app.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False)
class Base(DeclarativeBase):
    pass
def get_db():
    with SessionLocal() as db:
        yield db
'''
MODELS = '''from sqlalchemy import Column, Integer, String
from database import Base
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
'''
MAIN_HEAD = '''from contextlib import asynccontextmanager
from fastapi import APIRouter, FastAPI
import models  # noqa: F401
from database import Base, engine
@asynccontextmanager
async def lifespan(_app):
    Base.metadata.create_all(bind=engine)
    yield
app = FastAPI(lifespan=lifespan)
@app.get("/")
def health():
    return {"status": "ok"}
'''
VARIANTS = {
    'router_prefix_included': ('router = APIRouter(prefix="/api/auth")\n@router.post("/signup")\ndef signup():\n'
                               '    return {}\napp.include_router(router)\n', 'SCN001_START_CONTRACT_OK'),
    'include_prefix': ('router = APIRouter()\n@router.post("/signup")\ndef signup():\n    return {}\n'
                       'app.include_router(router, prefix="/api/auth")\n', 'SCN001_START_CONTRACT_OK'),
    'route_on_app': ('@app.post("/api/auth/signup")\ndef signup():\n    return {}\n', 'SCN001_START_CONTRACT_OK'),
    'handler_raises_on_probe': ('@app.post("/api/auth/signup")\ndef signup():\n    raise RuntimeError("x")\n',
                                'SCN001_START_CONTRACT_OK'),
    'router_not_included': ('router = APIRouter(prefix="/api/auth")\n@router.post("/signup")\ndef signup():\n'
                            '    return {}\n', 'SCN001_SIGNUP_ROUTE_REQUIRED'),
    'prefix_twice': ('router = APIRouter(prefix="/api/auth")\n@router.post("/signup")\ndef signup():\n    return {}\n'
                     'app.include_router(router, prefix="/api/auth")\n', 'SCN001_SIGNUP_ROUTE_REQUIRED'),
    'get_only': ('@app.get("/api/auth/signup")\ndef signup():\n    return {}\n', 'SCN001_SIGNUP_ROUTE_REQUIRED'),
}


MODELS_WITHOUT_PRIMARY_KEY = MODELS.replace('    id = Column(Integer, primary_key=True)\n', '')


def run_probe(main_tail, models=MODELS, *, main_head=MAIN_HEAD, database=DATABASE):
    with tempfile.TemporaryDirectory() as snapshot, tempfile.TemporaryDirectory() as work:
        root = Path(snapshot)
        (root / 'database.py').write_text(database)
        (root / 'models.py').write_text(models)
        (root / 'main.py').write_text(main_head + main_tail)
        probe = SCN001_BUILD_PROBE.replace("'/snapshot'", repr(snapshot)).replace("'/snapshot/'", repr(snapshot + '/'))
        done = subprocess.run([sys.executable, '-I', '-c', probe], cwd=work, capture_output=True, text=True, timeout=120)
    return done.returncode, done.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize('name', sorted(VARIANTS))
def test_probe_judges_the_served_path_not_the_route_table(name):
    tail, expected = VARIANTS[name]
    code, marker = run_probe(tail)
    assert marker == expected
    assert code == (0 if expected == 'SCN001_START_CONTRACT_OK' else 1)


@pytest.mark.parametrize('ready_first', [False, True])
def test_root_mount_registration_order_is_verified_by_the_unchanged_build_probe(ready_first):
    """Trusted fixture: preserving GET / first also preserves the mounted signup route."""
    head, ready = MAIN_HEAD.split('@app.get("/")', 1)
    ready = '@app.get("/")' + ready
    mounted = ('child = FastAPI()\n@child.post("/api/auth/signup")\ndef signup(): return {}\n'
               'app.mount("/", child)\n')
    tail = ready + mounted if ready_first else mounted + ready
    result = run_probe(tail, main_head=head)
    assert result == ((0, 'SCN001_START_CONTRACT_OK') if ready_first else (1, 'SCN001_READY_ENDPOINT_REQUIRED'))


def test_a_model_without_a_primary_key_gets_its_own_code_and_location():
    tail = VARIANTS['route_on_app'][0]
    code, marker = run_probe(tail, models=MODELS_WITHOUT_PRIMARY_KEY)
    assert (code, marker) == (1, 'SCN001_MODEL_PRIMARY_KEY_REQUIRED')


@pytest.mark.parametrize('main_head', [
    MAIN_HEAD.replace('app = FastAPI(lifespan=lifespan)', 'app = FastAPI()'),
    MAIN_HEAD.replace('    Base.metadata.create_all(bind=engine)\n', ''),
])
def test_ready_route_without_product_database_initialization_fails_the_build(main_head):
    # Both routes exist and the mapper is valid, but the application forgot to
    # create its table. The Host must not create it on the product's behalf.
    assert run_probe(VARIANTS['route_on_app'][0], main_head=main_head) == (1, 'SCN001_USER_TABLE_REQUIRED')


@pytest.mark.parametrize('main_head', [
    MAIN_HEAD,
    MAIN_HEAD.replace('app = FastAPI(lifespan=lifespan)',
                      'Base.metadata.create_all(bind=engine)\napp = FastAPI()'),
    MAIN_HEAD.replace('app = FastAPI(lifespan=lifespan)',
                      'app = FastAPI()\n@app.on_event("startup")\ndef prepare():\n    Base.metadata.create_all(bind=engine)'),
])
def test_database_readiness_accepts_actual_initialization_without_requiring_one_syntax(main_head):
    assert run_probe(VARIANTS['route_on_app'][0], main_head=main_head) == (0, 'SCN001_START_CONTRACT_OK')


def test_table_name_is_observed_from_the_model_not_hardcoded():
    assert run_probe(VARIANTS['route_on_app'][0], models=MODELS.replace('"users"', '"accounts"')) == (
        0, 'SCN001_START_CONTRACT_OK')


def test_direct_session_dependency_is_supported():
    database = DATABASE.replace('    with SessionLocal() as db:\n        yield db', '    return SessionLocal()')
    assert run_probe(VARIANTS['route_on_app'][0], database=database) == (0, 'SCN001_START_CONTRACT_OK')


@pytest.mark.parametrize('body', ['    return None\n', '    if False:\n        yield None\n'])
def test_broken_dependency_does_not_pass_because_a_separate_engine_has_the_table(body):
    database = DATABASE.split('def get_db():')[0] + 'def get_db():\n' + body
    assert run_probe(VARIANTS['route_on_app'][0], database=database) == (1, 'SCN001_DATABASE_SESSION_REQUIRED')


def test_table_in_an_unrelated_database_cannot_mask_wrong_dependency_binding():
    database = DATABASE.replace('    with SessionLocal() as db:',
        '    with sessionmaker(bind=create_engine("sqlite:///./different.db"))() as db:')
    assert run_probe(VARIANTS['route_on_app'][0], database=database) == (1, 'SCN001_USER_TABLE_REQUIRED')


@pytest.mark.parametrize('fragment,expected', [
    # Handwritten trusted reproductions of this diagnostic's two import failures.
    ('from argon2 import PasswordHasher\nph = PasswordHasher(type=argon2.low_level.Type.ID)\n',
     'SCN001_NAME_UNDEFINED'),
    ('from argon2 import PasswordHasher\nph = PasswordHasher(type="argon2id")\n',
     'SCN001_ARGUMENT_TYPE_INVALID'),
    ('raise ValueError("fixture-private-value-not-for-feedback")\n', 'SCN001_ARGUMENT_VALUE_INVALID'),
    ('raise RuntimeError("fixture-private-value-not-for-feedback")\n', 'SCN001_ENTRYPOINT_START_FAILED'),
])
def test_start_exceptions_have_closed_categories_not_arbitrary_messages(fragment, expected):
    code, marker = run_probe(fragment + VARIANTS['route_on_app'][0])
    assert (code, marker) == (1, expected)


@pytest.mark.parametrize('rule', ['SCN001_MODEL_PRIMARY_KEY_REQUIRED', 'SCN001_MODEL_DEFINITION_INVALID'])
def test_new_model_codes_decode_to_value_free_feedback_at_models_py(rule):
    from orchestrator.sandbox.scn001_start_contract import scn001_build_argv, scn001_build_diagnostics
    result = scn001_build_diagnostics(argv=scn001_build_argv(), exit_code=1, stdout='private-trace-text\n' + rule + '\n')
    assert len(result) == 1 and result[0].rule_id == rule and result[0].normalized_location == 'models.py'
    assert 'private-trace-text' not in result[0].model_dump_json()
