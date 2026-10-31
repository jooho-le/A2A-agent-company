# D2 보호 테스트를 실제로 실행해 보기 위한 최소 회원가입 서비스와 결함 변형이다(합성 코드, 실제 생성물 아님).
# 구조는 세화 backend와 같다: main.app, database.get_db, models.User(email, password_hash),
# POST /api/auth/signup. 변형은 정상 서비스의 글자 일부를 바꾸거나 파일을 더해 만든다(결함, 그리고 판정이 바뀌면
# 안 되는 정상 경계).
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


# 시작할 때 새 이름으로 만드는, 루트로 올려 보내지 않는 감사 로거(앱 import 뒤에 생김). 가입 때 값을 파일에 남긴다(Codex 10.30 R1).
# 파일은 os.devnull(실제로 쓰는 출력 경로이지만 시험 뒤 남는 파일이 없음)이다.
_AUDIT_STARTUP = '''\
import logging
import os
import uuid

logger = None


def configure():
    global logger
    logger = logging.getLogger("audit." + uuid.uuid4().hex)
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.FileHandler(os.devnull))


def record(value):
    logger.info("%s", value)
'''
# 가입 때 값을 모아 두었다가 종료할 때 로그로 남기는 감사 모듈(검사가 끝난 뒤에 노출됨, Codex 10.30 R1).
_AUDIT_SHUTDOWN = '''\
import logging

seen = []


def record(value):
    seen.append(value)


def flush():
    for value in seen:
        logging.getLogger("audit.shutdown").warning("%s", value)
    seen.clear()
'''

# 비밀번호를 가린 뒤 파일로 내보내는 감사 로거. FILTER_TARGET이 logger면 로거 필터, handler면 handler 필터로 가린다.
# 실제로 나가는 글에는 원문이 없으므로 노출이 아니다(Codex 10.30 3차 2).
_AUDIT_MASKED = '''\
import logging
import os
import uuid


class Redact(logging.Filter):
    def filter(self, record):
        record.msg = "signup credential [REDACTED]"
        record.args = ()
        return True


logger = logging.getLogger("safe.audit." + uuid.uuid4().hex)
logger.setLevel(logging.INFO)
logger.propagate = False
handler = logging.FileHandler(os.devnull)
logger.addHandler(handler)
FILTER_TARGET.addFilter(Redact())


def record(value):
    logger.warning("signup credential %s", value)
'''

def _lifespan(start='', end='', state=''):
    # main.py의 app을 lifespan이 있는 앱으로 바꾼다. start·end는 시작·종료 때 할 일, state는 요청에 넘길 상태.
    return [('main.py', 'app = FastAPI()\n',
             'from contextlib import asynccontextmanager\n\nfrom database import Base, engine\nimport audit\nimport models\n\n\n'
             '@asynccontextmanager\nasync def lifespan(app):\n    Base.metadata.create_all(bind=engine)\n' + start
             + '    yield' + state + '\n' + end + '\n\napp = FastAPI(lifespan=lifespan)\n'),
            ('audit.py', None, _AUDIT_SHUTDOWN.replace('    for value in seen:', '    for value in []:'))]


def _audit(module, value):
    # 가입 경로가 감사 모듈(audit.py, module 내용)에 value를 남기게 한다.
    return [('audit.py', None, module),
            ('routers/auth.py', 'from security import hash_password\n', 'from security import hash_password\nfrom audit import record\n'),
            ('routers/auth.py', '    db.add(user)\n', '    record(' + value + ')\n    db.add(user)\n')]


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


# 변형 이름: [(파일, 바꿀 글자, 새 글자)]. 바꿀 글자는 정상 서비스에 정확히 한 번 있어야 한다(None이면 새 파일).
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
    # 루트로 올려 보내지 않는 로거(자체 파일 handler)에 남긴 비밀번호. 앱 import 때 만들어진 로거도 수집해야 찾는다.
    'isolated_logger': [('routers/auth.py', 'router = APIRouter(prefix="/api/auth")\n',
                         'router = APIRouter(prefix="/api/auth")\n'
                         'import logging, os\naudit = logging.getLogger("audit")\naudit.propagate = False\n'
                         'audit.addHandler(logging.FileHandler(os.devnull))\naudit.setLevel(logging.INFO)\n'),
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
    # 시작할 때 만든 독립 로거에 남긴 비밀번호(앱 import 때는 없던 로거). 수집하지 못하면 노출을 놓친다(Codex 10.30 R1).
    'startup_isolated_logger': _lifespan(start='    audit.configure()\n')[:1] + _audit(_AUDIT_STARTUP, 'payload.password'),
    # 비밀번호를 모았다가 종료할 때 로그로 남김. 종료까지 모은 뒤 판정해야 찾는다(Codex 10.30 R1).
    'shutdown_log_flush': _lifespan(end='    audit.flush()\n')[:1] + _audit(_AUDIT_SHUTDOWN, 'payload.password'),
    # 종료할 때 남기는 로그에 비밀번호가 없으면 노출이 아니다(종료 로그를 모아도 정상 앱이 FAIL이 되지 않는지).
    'shutdown_log_without_secret': _lifespan(end='    audit.flush()\n')[:1] + _audit(_AUDIT_SHUTDOWN, 'payload.email'),
    # lifespan이 요청에 상태를 넘기는 정상 앱. 동시 요청도 같은 lifespan 상태로 보내야 한다(Codex 10.30 R2).
    'lifespan_request_state': _lifespan(state=' {"ready": True}') + [
        ('routers/auth.py', 'from fastapi import APIRouter,', 'from fastapi import Request, APIRouter,'),
        ('routers/auth.py', 'def signup(payload: SignupRequest, db: Session = Depends(get_db)):\n',
         'def signup(request: Request, payload: SignupRequest, db: Session = Depends(get_db)):\n'
         '    if not request.state.ready:\n        raise RuntimeError("not ready")\n')],
    # 종료할 때 자기 엔진을 정리하는 정상 앱. 검사마다 앱을 새 임시 DB로 다시 불러와야 한다(Codex 10.30 R3).
    'lifespan_engine_dispose': _lifespan(end='    engine.dispose()\n'),
    # 중복 가입은 409로 거부하지만 기존 계정의 비밀번호 해시를 바꿔 버림(Codex 10.30 추가).
    'duplicate_overwrites_password': [('routers/auth.py', _PRECHECK, '''\
    existing = db.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        existing.password_hash = hash_password(payload.password)
        db.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="duplicate email")
''')],
    # DB 저장이 실패한 요청에서 새로 만든 해시를 base64로 바꿔 로그에 남김(저장되지 않은 해시, Codex 10.30 추가 점검 4).
    'db_error_new_hash_base64': [('routers/auth.py', _DB_ERROR_HANDLER, _DB_ERROR_HANDLER.replace(
        '        db.rollback()\n',
        '        db.rollback()\n        __import__("logging").getLogger("signup").warning(\n'
        '            "store failed %s", __import__("base64").b64encode(user.password_hash.encode()).decode())\n'))],
    # base64로 바꾼 값을 로그에 남기더라도 비밀번호·해시가 아니면 노출이 아니다(인코딩 검색이 오탐을 내지 않는지).
    'base64_log_without_secret': [('routers/auth.py', '    user = User(',
                                   '    __import__("logging").getLogger("signup").warning(\n'
                                   '        "signup %s", __import__("base64").b64encode((payload.email * 3).encode()).decode())\n'
                                   '    user = User(')],
    # 최소 길이 외에 대문자·소문자·숫자를 모두 요구하는 승인되지 않은 조건(소문자만·숫자만 8자 이상을 거부, Codex 10.30 추가 점검 5).
    'password_complexity_required': [('schemas.py', '            raise ValueError("password too short")\n',
                                      '            raise ValueError("password too short")\n'
                                      '        if not (any(c.islower() for c in value) and any(c.isupper() for c in value)\n'
                                      '                and any(c.isdigit() for c in value)):\n'
                                      '            raise ValueError("password too simple")\n')],
    # Gmail 점 제거·+태그 제거를 더한 이메일 정리. 정책(gmailDotRemoval·plusTagRemoval 모두 False)상 다른 주소를 합친다
    # (Codex 10.30 3차 1).
    'email_alias_merged': [('schemas.py', '        value = value.strip().lower()\n',
                            '        value = value.strip().lower()\n'
                            '        local, _, domain = value.partition("@")\n'
                            '        local = local.split("+", 1)[0]\n'
                            '        if domain == "gmail.com":\n'
                            '            local = local.replace(".", "")\n'
                            '        value = local + "@" + domain\n')],
    # 정책 칸·완료 조건 대응표(2026-10-30)에서 비어 있던 결함과 시험지 세 벌의 입력 종류를 맞추는 변형.
    # 앞 공백만·뒤 공백만 지우는 정리(앞뒤 공백 제거 정책 위반): 어느 시험지에서나 정규화 저장(SEC-003)이 FAIL이어야 한다.
    'strip_leading_only': [('schemas.py', 'value = value.strip().lower()', 'value = value.lstrip().lower()')],
    'strip_trailing_only': [('schemas.py', 'value = value.strip().lower()', 'value = value.rstrip().lower()')],
    # 최상위 도메인 3글자 이상만 받는 이메일 검사(유효한 2글자 최상위 도메인 거부): 어느 시험지에서나 QA-001이 FAIL이어야 한다.
    'email_tld_min3': [('schemas.py', '[.][A-Za-z]{2,63}', '[.][A-Za-z]{3,63}')],
    # Argon2 반복 횟수 미달(t=1)·Argon2id가 아닌 argon2i 종류: 해시 저장값 검사(SEC-002)가 FAIL이어야 한다.
    'weak_argon2_iterations': [('security.py', 'time_cost=2', 'time_cost=1')],
    'argon2i_type': [('security.py', 'from argon2 import PasswordHasher', 'from argon2 import PasswordHasher, Type'),
                     ('security.py', 'parallelism=1)', 'parallelism=1, type=Type.I)')],
    # 일반 SHA 계열로 저장(REQ-005 "평문/일반 SHA 계열 저장 없음"): 해시 저장값 검사(SEC-002)가 FAIL이어야 한다.
    'sha256_storage': [('security.py', 'return password_hasher.hash(password)',
                        'return __import__("hashlib").sha256(password.encode()).hexdigest()')],
    # 도메인만 소문자로 바꾸고 로컬 부분 대소문자는 그대로 둠(localPartCase 위반): 대소문자만 다른 주소가 중복으로 생긴다.
    'local_case_kept': [('schemas.py', '        value = value.strip().lower()\n',
                         '        local, _, domain = value.strip().partition("@")\n'
                         '        value = local + "@" + domain.lower()\n')],
    # 중복 가입에 성공 응답(REQ-007 응답·결과 불일치): 계정은 하나지만 응답이 성공이다.
    'duplicate_success_response': [
        ('routers/auth.py', _PRECHECK, '    if db.scalar(select(User.id).where(User.email == payload.email)) is not None:\n'
                                       '        return {"success": True}\n'),
        ('routers/auth.py', '        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="duplicate email") from None\n',
         '        return {"success": True}\n')],
    # 비밀번호를 로거 필터·handler 필터로 가린 뒤 내보내는 정상 앱(필터 전 원문을 노출로 보면 오탐, Codex 10.30 3차 2).
    'masked_logger_filter': _audit(_AUDIT_MASKED.replace('FILTER_TARGET', 'logger'), 'payload.password'),
    'masked_handler_filter': _audit(_AUDIT_MASKED.replace('FILTER_TARGET', 'handler'), 'payload.password'),
    'app_missing': [('main.py', 'app.include_router(auth_router)\n', 'app.include_router(auth_router)\napplication = app\n'),
                    ('main.py', '    return JSONResponse(status_code=422, content={"detail": errors})\n',
                     '    return JSONResponse(status_code=422, content={"detail": errors})\n\n\ndel app\n')],
}


def build(variant: str) -> dict[str, str]:
    # 변형 이름으로 서비스 파일 사전({상대 경로: 내용})을 만든다.
    files = dict(GOOD)
    for name, old, new in VARIANTS[variant]:
        if old is None:  # 새 파일
            if name in files:
                raise ValueError(f'{variant}: {name} already exists')
            files[name] = new
            continue
        if files[name].count(old) != 1:
            raise ValueError(f'{variant}: replacement target must appear exactly once in {name}')
        files[name] = files[name].replace(old, new)
    return files
