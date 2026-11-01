"""회원가입 코드의 "정상 예제"와 "결함 예제" 모음이다. (D3 작업)

보안 판정기(security/)가 제대로 판정하는지 확인하려면 "답을 이미 아는 코드"가 필요하다. 이 파일이 그 재료이다.
  - GOOD            : 요구사항을 모두 지키는 정상 회원가입 코드(파일별 글자로 적어 둠)
  - DEFECTS         : GOOD에서 "결함 하나만" 일부러 넣은 변형들. 판정기가 그 결함을 잡아내야 한다.
  - SAFE_VARIANTS   : GOOD과 다르게 쓰였지만 여전히 안전한 코드. 판정기가 이것을 결함으로 오해(오탐)하면 안 된다.
GOOD의 기준은 registry(정책) 값이다: Argon2id(메모리 19456, 반복 2, 병렬 1), 이메일 공백 제거·소문자 변환·UNIQUE.
이 예제들은 평가기가 맞게 판정하는지 확인하기 위한 가짜 코드이다. 에이전트의 성공률이나 실제 시작 코드와는 관계가 없다.
"""

# 정상 예제: {파일 이름: 그 파일의 소스 코드(글자)}. 아래 글자들은 판정기가 읽는 "가짜 프로젝트"의 내용이다.
# (글자 안의 내용은 코드로 실행되지 않고 분석 대상일 뿐이므로, 이 안에는 설명 주석을 넣지 않았다.)
GOOD = {
    # main.py  : FastAPI 앱 본체. 입력 검증 오류가 나면 loc·msg·type만 골라 응답한다(입력값 input은 빼서 비밀번호가 새지 않는다).
    'main.py': '''from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from database import Base, engine
from routers import router

app = FastAPI()
app.include_router(router)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})
''',
    # database.py : DB 연결(SQLite).
    'database.py': '''from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

engine = create_engine("sqlite:///./app.db")
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    with SessionLocal() as db:
        yield db
''',
    # models.py : users 테이블. email에 unique=True(중복 금지)가 있고, 비밀번호는 password_hash 칸에만 저장한다.
    'models.py': '''from sqlalchemy import Column, Integer, String

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String, nullable=False, unique=True, index=True)
    password_hash = Column(String, nullable=False)
''',
    # schemas.py : 요청 모양. 이메일은 앞뒤 공백 제거 + 소문자로 정리하고, 비밀번호는 8자 이상이어야 한다.
    'schemas.py': '''import re

from pydantic import BaseModel, field_validator

EMAIL_PATTERN = re.compile(r"[^@\\s]+@[^@\\s]+\\.[A-Za-z]{2,}")


class SignupRequest(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.strip().lower()
        if EMAIL_PATTERN.fullmatch(value) is None:
            raise ValueError("올바른 이메일 형식이 아닙니다.")
        return value

    @field_validator("password")
    @classmethod
    def check_length(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("비밀번호는 최소 8자 이상이어야 합니다.")
        return value
''',
    # security.py : Argon2id로 비밀번호를 해시한다(registry가 정한 설정 숫자 그대로).
    'security.py': '''from argon2 import PasswordHasher

HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)


def hash_password(password: str) -> str:
    return HASHER.hash(password)
''',
    # routers.py : 가입 API. 비밀번호는 해시해서 저장하고, 응답에는 성공 메시지만 담는다. 이메일 중복은 409로 응답한다.
    'routers.py': '''from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError

from database import get_db
from models import User
from schemas import SignupRequest
from security import hash_password

router = APIRouter()


@router.post("/api/signup", status_code=201)
def signup(payload: SignupRequest, db=Depends(get_db)):
    user = User(email=payload.email, password_hash=hash_password(payload.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="이미 등록된 이메일입니다.")
    return {"success": True, "message": "회원가입에 성공했습니다."}
''',
}

# 결함 예제 목록. 각 항목은 아래 여섯 칸으로 이루어진 튜플이다.
#   (예제 이름, 바꿀 파일, 바꿀 부분(None이면 파일 전체 교체), 새로 넣을 내용, 기대하는 판정 {요구사항: 결과}, 어떤 결함인지 설명)
# 예: ('plaintext', 'routers.py', A, B, {'REQ-005': 'FAIL'}, ...) = routers.py에서 A를 B로 바꾸면 REQ-005가 FAIL이어야 한다.
DEFECTS = [
    ('plaintext', 'routers.py', 'password_hash=hash_password(payload.password)', 'password_hash=payload.password',
     {'REQ-005': 'FAIL'}, '비밀번호 원문 저장'),
    ('md5', 'security.py', 'return HASHER.hash(password)', 'return hashlib.md5(password.encode()).hexdigest()',
     {'REQ-005': 'FAIL'}, '빠른 일반 해시(MD5) 사용'),
    ('weak_params', 'security.py', 'memory_cost=19456', 'memory_cost=8192',
     {'REQ-005': 'FAIL'}, 'Argon2 메모리 기준 미달'),
    ('no_unique', 'models.py', 'unique=True, index=True', 'index=True',
     {'REQ-003': 'FAIL'}, '이메일 UNIQUE 누락'),
    ('no_strip', 'schemas.py', 'value.strip().lower()', 'value.lower()',
     {'REQ-003': 'FAIL'}, '이메일 앞뒤 공백 정규화 누락'),
    ('log_leak', 'routers.py', '    db.add(user)\n', '    logger.info("signup %s %s", payload.email, payload.password)\n    db.add(user)\n',
     {'REQ-006': 'FAIL'}, '로그에 비밀번호 출력'),
    ('response_leak', 'routers.py', '"message": "회원가입에 성공했습니다."}', '"password_hash": user.password_hash}',
     {'REQ-006': 'FAIL'}, '응답에 저장 해시 포함'),
    ('validation_echo', 'main.py', '@app.exception_handler(RequestValidationError)\nasync def validation_error_handler',
     'async def unused_handler', {'REQ-006': 'FAIL'}, '검증 오류 응답에 입력값(비밀번호) 노출'),
    ('unanalyzable', 'security.py', 'memory_cost=19456', 'memory_cost=int(os.environ["MEM"])',
     {'REQ-005': 'UNVERIFIED'}, '설정이 상수가 아니라 정적 분석 불가'),
    # 아래는 점검에서 찾은 "놓치기 쉬운 결함"이다. 예전에는 결함인데도 PASS가 나오던 것들을 다시는 놓치지 않도록 넣었다.
    ('log_whole_payload', 'routers.py', '    db.add(user)\n', '    logger.info("signup %s", payload)\n    db.add(user)\n',
     {'REQ-006': 'FAIL'}, '요청 모델을 통째로 로그에 출력(비밀번호 필드 포함)'),
    ('print_model_dump', 'routers.py', '    db.add(user)\n', '    print(payload.model_dump())\n    db.add(user)\n',
     {'REQ-006': 'FAIL'}, 'model_dump() 결과 출력'),
    ('stderr_write', 'routers.py', '    db.add(user)\n', '    sys.stderr.write(payload.password)\n    db.add(user)\n',
     {'REQ-006': 'FAIL'}, '표준 오류로 비밀번호 출력'),
    ('response_dict_dump', 'routers.py', '{"success": True, "message": "회원가입에 성공했습니다."}', '{"user": user.__dict__}',
     {'REQ-006': 'UNVERIFIED'}, '객체의 전체 필드를 응답에 펼침(어떤 필드인지 정적으로 알 수 없음)'),
    ('validation_body_echo', 'main.py', '"detail": errors}', '"detail": errors, "body": exc.body}',
     {'REQ-006': 'FAIL'}, '검증 오류 응답에 요청 본문 포함'),
    ('validation_raw_errors', 'main.py',
     'errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()]',
     'errors = [error for error in exc.errors()]', {'REQ-006': 'FAIL'}, '검증 오류 목록을 그대로 반환(input 포함)'),
    ('hash_not_returned', 'security.py', 'return HASHER.hash(password)', 'HASHER.hash("dummy")\n    return password',
     {'REQ-005': 'UNVERIFIED'}, '해시 호출은 있으나 반환값은 원문(해시 경로 확인 불가)'),
    ('email_token_unique', 'models.py', 'unique=True, index=True',
     'index=True)\n    email_token = Column(String, unique=True',
     {'REQ-003': 'FAIL'}, '이메일 인증 토큰에만 UNIQUE(이메일 컬럼에는 없음)'),
    ('unrelated_strip', 'schemas.py', 'value = value.strip().lower()', 'label = "X".strip().lower()',
     {'REQ-003': 'FAIL'}, '이메일과 무관한 문자열만 정규화'),
]
# 안전한 변형 목록. 위 결함 예제와 반대로, 모두 "GOOD과 같은 판정(전부 PASS)"이어야 한다.
# 멀쩡한 코드를 결함으로 오해하는 "오탐"이 생기면 불필요한 수정 작업이 생기므로, 그런 일이 없는지 확인하는 용도이다.
# 칸의 의미는 DEFECTS와 같다. 기대 판정이 {}이면 "GOOD과 다르지 않다(모두 PASS)"는 뜻이다.
SAFE_VARIANTS = [
    ('safe_passed_name', 'routers.py', '    db.add(user)\n', '    passed = True\n    logger.info("ok %s", passed)\n    db.add(user)\n',
     {}, '"passed" 같은 변수 이름은 password가 아니다'),
    ('safe_constant_in_message', 'schemas.py', 'raise ValueError("비밀번호는 최소 8자 이상이어야 합니다.")',
     'raise ValueError(f"min {MIN_PASSWORD_LENGTH}")', {}, '길이 상수 이름은 비밀번호 값이 아니다'),
    ('safe_message_constant_response', 'routers.py', '"message": "회원가입에 성공했습니다."',
     '"message": PASSWORD_OK_MESSAGE', {}, '안내 문구 상수 이름은 비밀번호 값이 아니다'),
    ('safe_log_length', 'routers.py', '    db.add(user)\n', '    logger.info("length %d", len(payload.password))\n    db.add(user)\n',
     {}, '길이만 로그에 남기는 것은 노출이 아니다'),
    ('safe_log_other_field', 'routers.py', '    db.add(user)\n', '    logger.info("signup %s", payload.email)\n    db.add(user)\n',
     {}, '비밀번호가 아닌 필드 로깅'),
    ('safe_handler_variable', 'main.py',
     'errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()]',
     'raw = exc.errors()\n    errors = [{"loc": e["loc"], "msg": e["msg"]} for e in raw]', {}, '변수에 담은 뒤 필요한 키만 복사'),
    ('safe_handler_pop_input', 'main.py',
     'errors = [{"loc": error["loc"], "msg": error["msg"], "type": error["type"]} for error in exc.errors()]',
     'errors = exc.errors()\n    for error in errors:\n        error.pop("input", None)', {}, '순회하며 input 제거'),
    ('safe_pydantic_strip_lower', 'schemas.py', 'value = value.strip().lower()',
     'value = value.lower()', {}, 'strip은 pydantic 설정이 한다(별도 파일 수정으로 구성)'),
    ('safe_unique_index_sql', 'models.py', None,
     'import sqlite3\n\nSCHEMA = "CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL, password_hash TEXT NOT NULL)"\n'
     'INDEX = "CREATE UNIQUE INDEX ux_users_email ON users(email)"\n'
     'DB = sqlite3.connect(":memory:")\nDB.execute(SCHEMA)\nDB.execute(INDEX)\n', {'REQ-005': 'UNVERIFIED'},
     'UNIQUE를 CREATE UNIQUE INDEX로 선언(이 예제는 모델 파일을 SQL로 바꿔 REQ-005 저장 경로가 사라짐)'),
]
# 변형마다 필요한 import 같은 "앞부분 보충 코드"를 정해 두는 표. {예제 이름: (파일, 파일 맨 앞에 붙일 글자)}
# 예: 'md5'는 hashlib.md5를 쓰므로 security.py 맨 앞에 "import hashlib"을 붙여야 코드가 성립한다.
PRELUDES = {
    'md5': ('security.py', 'import hashlib\n'),
    'unanalyzable': ('security.py', 'import os\n'),
    'log_leak': ('routers.py', 'import logging\nlogger = logging.getLogger(__name__)\n'),
    'log_whole_payload': ('routers.py', 'import logging\nlogger = logging.getLogger(__name__)\n'),
    'stderr_write': ('routers.py', 'import sys\n'),
    'safe_passed_name': ('routers.py', 'import logging\nlogger = logging.getLogger(__name__)\n'),
    'safe_log_length': ('routers.py', 'import logging\nlogger = logging.getLogger(__name__)\n'),
    'safe_log_other_field': ('routers.py', 'import logging\nlogger = logging.getLogger(__name__)\n'),
    'safe_constant_in_message': ('schemas.py', 'MIN_PASSWORD_LENGTH = 8\n'),
    'safe_message_constant_response': ('routers.py', 'PASSWORD_OK_MESSAGE = "ok"\n'),
    'safe_pydantic_strip_lower': ('schemas.py', 'from pydantic import ConfigDict\n'),
}
# 정상 예제(GOOD)에서 기대하는 판정: 보안 요구사항 세 가지 모두 PASS
GOOD_EXPECTED = {'REQ-003': 'PASS', 'REQ-005': 'PASS', 'REQ-006': 'PASS'}


def _find(name):
    # 이름으로 결함 예제 또는 안전한 변형 하나를 찾아 돌려준다. 없으면 KeyError(이름을 잘못 쓴 것).
    for item in DEFECTS + SAFE_VARIANTS:
        if item[0] == name:
            return item
    raise KeyError(name)


def variant(name):
    # 이름에 해당하는 변형 예제의 "프로젝트 전체 소스"({파일 이름: 소스 글자})를 만든다.
    # GOOD을 복사한 뒤 지정한 파일의 지정한 부분만 바꾼다. 바꿀 부분을 못 찾으면 예제를 잘못 정의한 것이므로 오류로 멈춘다.
    _name, path, old, new, _expected, _why = _find(name)
    files = dict(GOOD)  # dict(...)로 복사해서 원본 GOOD은 건드리지 않는다.
    if old is None:
        files[path] = new  # None이면 그 파일을 통째로 새 내용으로 교체한다.
    else:
        assert old in GOOD[path], f'{name}: replacement target missing'
        files[path] = GOOD[path].replace(old, new, 1)  # 첫 번째 일치 부분 하나만 바꾼다.
    if name == 'safe_pydantic_strip_lower':
        # 이 예제는 strip(공백 제거)을 pydantic 설정이 대신 하게 하고, 코드에는 lower(소문자 변환)만 남긴다.
        files[path] = files[path].replace('class SignupRequest(BaseModel):',
                                         'class SignupRequest(BaseModel):\n    model_config = ConfigDict(str_strip_whitespace=True)', 1)
    if name in PRELUDES:
        prelude_path, text = PRELUDES[name]
        files[prelude_path] = text + files[prelude_path]  # 파일 맨 앞에 보충 코드를 붙인다.
    return files


def expected(name):
    # 변형 예제가 받아야 하는 판정을 돌려준다. GOOD의 판정(모두 PASS)에서, 그 변형이 바꾼 요구사항만 다르게 덮어쓴다.
    return {**GOOD_EXPECTED, **_find(name)[4]}
