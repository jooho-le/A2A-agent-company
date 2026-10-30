# D2 보호 테스트를 실제로 실행해 보기 위한 최소 회원가입 서비스와 결함 변형이다(합성 코드, 실제 생성물 아님).
# 구조는 세화 backend와 같다: main.app, database.get_db, models.User(email, password_hash),
# POST /api/auth/signup. 결함 변형은 정상 서비스의 글자 일부를 바꿔 만든다.
# 실행 검사는 fastapi·httpx·sqlalchemy·argon2-cffi가 필요하다(프로젝트 dev 그룹에 있음, 2026-10-30).

GOOD = {
    'main.py': '''\
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from routers.auth import router as auth_router

app = FastAPI()
app.include_router(auth_router)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})
''',
    'database.py': '''\
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./app.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    with SessionLocal() as db:
        yield db
''',
    'models.py': '''\
from sqlalchemy import Column, Integer, String

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String, nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=False)
''',
    'schemas.py': '''\
import re

from pydantic import BaseModel, ConfigDict, field_validator

EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:[.][A-Za-z0-9-]+)*[.][A-Za-z]{2,63}")


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        value = value.strip().lower()
        if EMAIL_PATTERN.fullmatch(value) is None:
            raise ValueError("invalid email")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("password too short")
        return value
''',
    'security.py': '''\
from argon2 import PasswordHasher

password_hasher = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)
''',
    'routers/__init__.py': '',
    'routers/auth.py': '''\
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from database import get_db
from models import User
from schemas import SignupRequest
from security import hash_password

router = APIRouter(prefix="/api/auth")


@router.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, db: Session = Depends(get_db)):
    if db.scalar(select(User.id).where(User.email == payload.email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="duplicate email")
    user = User(email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="duplicate email") from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="try again later") from None
    return {"success": True}
''',
}

_PRECHECK = '''\
    if db.scalar(select(User.id).where(User.email == payload.email)) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="duplicate email")
'''
_DB_ERROR_HANDLER = '''\
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="try again later") from None
'''
_HANDLED_COMMIT = '''\
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="duplicate email") from None
''' + _DB_ERROR_HANDLER
# 앱이 시작할 때(lifespan) 자기 엔진으로 테이블을 만드는 구조(세화 backend와 같은 방식)
_LIFESPAN_APP = '''\
from contextlib import asynccontextmanager

from database import Base, engine
import models


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(lifespan=lifespan)
'''



def _concurrent_failure(error):
    # 같은 이메일 요청이 동시에 처리 중이면 모두 error로 끝나는 가입(단독 요청은 정상). 0.2초 동안 동시 처리 수를 지켜본다.
    return [('routers/auth.py', 'router = APIRouter(prefix="/api/auth")\n',
             'router = APIRouter(prefix="/api/auth")\nimport threading, time\n_ACTIVE, _LOCK = {}, threading.Lock()\n'),
            ('routers/auth.py', 'def signup(payload: SignupRequest, db: Session = Depends(get_db)):\n',
             'def signup(payload: SignupRequest, db: Session = Depends(get_db)):\n'
             '    with _LOCK:\n'
             '        _ACTIVE[payload.email] = _ACTIVE.get(payload.email, 0) + 1\n'
             '    peak = 0\n'
             '    for _ in range(20):\n'
             '        peak = max(peak, _ACTIVE[payload.email])\n'
             '        time.sleep(0.01)\n'
             '    with _LOCK:\n'
             '        _ACTIVE[payload.email] -= 1\n'
             '    if peak > 1:\n'
             '        ' + error + '\n')]


# 변형 이름: [(파일, 바꿀 글자, 새 글자)]. 바꿀 글자는 정상 서비스에 정확히 한 번 있어야 한다.
VARIANTS = {
    'good': [],
    'plaintext_storage': [('security.py', 'return password_hasher.hash(password)', 'return password')],
    'weak_argon2_memory': [('security.py', 'memory_cost=19456', 'memory_cost=1024')],
    'fixed_salt': [('security.py', 'password_hasher.hash(password)',
                    'password_hasher.hash(password, salt=b"0123456789abcdef")')],
    'seven_char_password': [('schemas.py', 'len(value) < 8', 'len(value) < 7')],
    'duplicates_allowed': [('models.py', 'unique=True, ', ''), ('routers/auth.py', _PRECHECK, '')],
    'case_sensitive_email': [('schemas.py', 'value = value.strip().lower()', 'value = value.strip()')],
    'padded_email_rejected': [('schemas.py', 'value = value.strip().lower()', 'value = value.lower()')],
    'invalid_email_accepted': [('schemas.py', '''\
        if EMAIL_PATTERN.fullmatch(value) is None:
            raise ValueError("invalid email")
''', '')],
    'validation_input_echoed': [('main.py', '''\


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})
''', '\n')],
    'password_logged': [('routers/auth.py', '    user = User(',
                         '    __import__("logging").getLogger("signup").warning("signup %s", payload.password)\n'
                         '    user = User(')],
    'hash_in_response': [('routers/auth.py', 'return {"success": True}',
                          'return {"success": True, "hash": user.password_hash}')],
    'unhandled_duplicate': [('routers/auth.py', _PRECHECK, ''),
                            ('routers/auth.py', _HANDLED_COMMIT, '    db.commit()\n')],
    # 기본 수준(WARNING)에서는 남지 않는 info·debug 로그의 비밀번호(정적 판정이 잡던 것, 2026-10-30 자체 점검)
    'password_logged_info': [('routers/auth.py', '    user = User(',
                              '    __import__("logging").getLogger("signup").info("signup %s", payload.password)\n'
                              '    user = User(')],
    'password_logged_debug': [('routers/auth.py', '    user = User(',
                               '    __import__("logging").getLogger("signup").debug("signup %s", payload.password)\n'
                               '    user = User(')],
    # 비밀번호가 없는 debug 로그는 모아도 노출이 아니다(로그를 더 모아도 정상 앱이 FAIL이 되지 않는지 확인).
    'debug_log_without_secret': [('routers/auth.py', '    user = User(',
                                  '    __import__("logging").getLogger("signup").debug("signup %s", payload.email)\n'
                                  '    user = User(')],
    # DB 오류(저장 실패)를 처리하지 않아 서버 traceback에 SQL 값(해시)이 찍힘. 오류 주입으로 찾는다(2026-10-30).
    'db_error_unhandled': [('routers/auth.py', _DB_ERROR_HANDLER, '')],
    'password_printed': [('routers/auth.py', '    user = User(', '    print("signup", payload.password)\n    user = User(')],
    # 루트로 올려 보내지 않는 로거(자체 handler)에 남긴 비밀번호. 앱 import 때 만들어진 로거도 수집해야 찾는다.
    'isolated_logger': [('routers/auth.py', 'router = APIRouter(prefix="/api/auth")\n',
                         'router = APIRouter(prefix="/api/auth")\n'
                         'import logging\naudit = logging.getLogger("audit")\naudit.propagate = False\n'
                         'audit.addHandler(logging.NullHandler())\naudit.setLevel(logging.INFO)\n'),
                        ('routers/auth.py', '    user = User(', '    audit.info("signup %s", payload.password)\n    user = User(')],
    # 가입 경로가 연결 규격과 다르면 모든 요청이 404가 된다. 거부 검사가 거저 통과하지 않고 모두 미검증이어야 한다.
    'wrong_signup_path': [('routers/auth.py', 'prefix="/api/auth"', 'prefix="/api/v2/auth"')],
    # 요청 필드가 규격과 달라 모든 가입이 422로 거부된다. 형식 오류 거부 검사가 거저 통과하면 안 된다.
    'different_request_field': [('schemas.py', '    email: str\n', '    username: str\n    email: str\n')],
    # 대문자·앞뒤 공백이 있는 이메일에서 처리하지 못한 예외(500)가 난다. 중복은 생기지 않지만 4xx 거부가 아니다(Codex F1).
    'variant_email_server_error': [('schemas.py', '        value = value.strip().lower()\n',
                                    '        if value != value.strip().lower():\n'
                                    '            raise RuntimeError("normalization failed")\n'
                                    '        value = value.strip().lower()\n')],
    # 시작할 때 테이블을 만드는 앱. DB 위치를 DATABASE_URL로 받으면 보호 테스트의 임시 DB를 써서 정상 판정된다.
    'lifespan_env_database': [('main.py', 'app = FastAPI()\n', _LIFESPAN_APP)],
    # 같은 앱이 DB 파일을 소스 폴더 옆에 고정하면, 읽기 전용 Snapshot(컨테이너)에서 시작이 실패해 모두 미검증이다.
    'lifespan_source_database': [('main.py', 'app = FastAPI()\n', _LIFESPAN_APP),
                                 ('database.py', 'DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./app.db")',
                                  'DATABASE_URL = "sqlite:///" + os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")')],
    # 단독 가입은 되지만 같은 이메일 동시 요청은 모두 500(계정 없음). 응답을 받았으므로 미검증이 아니라 FAIL이다(Codex 2차).
    'concurrent_server_error': _concurrent_failure('raise RuntimeError("concurrent signup failed")'),
    # 단독 가입은 되지만 같은 이메일 동시 요청은 모두 4xx(계정 없음). 요청 규격 문제가 아니므로 FAIL이다.
    'concurrent_rejected': _concurrent_failure('raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="busy")'),
    # 모든 가입이 500(계정 없음). 정상 가입·동시 가입은 서버 오류를 관찰했으므로 FAIL, 거부 검사는 전제 조건이 안 돼 미검증이다.
    'signup_server_error': [('routers/auth.py', 'def signup(payload: SignupRequest, db: Session = Depends(get_db)):\n',
                             'def signup(payload: SignupRequest, db: Session = Depends(get_db)):\n'
                             '    raise RuntimeError("signup failed")\n')],
    'app_missing': [('main.py', 'app.include_router(auth_router)\n', 'app.include_router(auth_router)\napplication = app\n'),
                    ('main.py', '    return JSONResponse(status_code=422, content={"detail": errors})\n',
                     '    return JSONResponse(status_code=422, content={"detail": errors})\n\n\ndel app\n')],
}


def build(variant: str) -> dict[str, str]:
    # 변형 이름으로 서비스 파일 사전({상대 경로: 내용})을 만든다.
    files = dict(GOOD)
    for name, old, new in VARIANTS[variant]:
        if files[name].count(old) != 1:
            raise ValueError(f'{variant}: replacement target must appear exactly once in {name}')
        files[name] = files[name].replace(old, new)
    return files
