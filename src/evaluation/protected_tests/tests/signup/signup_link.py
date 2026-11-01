# 회원가입 보호 테스트의 연결부. 생성물 규격(시작 코드)이 바뀌면 이 파일의 상수만 고친다.
# 지금은 세화 backend 구조를 가정한다: main.app, database.get_db, models.User(email, password_hash),
# POST /api/auth/signup {email, password}.
# 연결·전제 조건을 확인하지 못하면 제품 결함(FAIL)이 아니라 Unverified를 내고,
# 테스트는 이를 SKIP("UNVERIFIED:코드")로 바꾼다. 비밀번호는 실행할 때 만들고(dev 19번)
# 실패 메시지에 비밀번호·해시 값을 넣지 않는다.
# 표준 라이브러리만 미리 불러온다. fastapi·sqlalchemy는 _open에서 불러오고 없으면 Unverified다.
import contextlib
import importlib
import importlib.util
import inspect
import io
import logging
import os
import secrets
import shutil
import string
import sys
import sysconfig
import tempfile
import threading
import traceback
import unittest

# 연결 규격: (모듈, 이름) 위치, User 필드 이름, 가입 경로
APP = ("main", "app")
DB_DEPENDENCY = ("database", "get_db")
USER_MODEL = ("models", "User")
EMAIL_FIELD = "email"
HASH_FIELD = "password_hash"
SIGNUP_PATH = "/api/auth/signup"
SUCCESS = range(200, 300)   # 성공 응답
REJECTED = range(400, 500)  # 입력 검증·중복 실패 응답(5xx는 처리하지 못한 서버 오류)


def signup_body(email, password):
    # 가입 요청 본문(JSON)
    return {"email": email, "password": password}


class Unverified(Exception):
    # 확인 불가 신호. code는 SKIP 사유 "UNVERIFIED:코드"가 된다.
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# 비밀번호 문자 구성. mixed는 대문자·소문자·숫자를 하나씩 넣어 길이 말고 다른 규칙 때문에 거부되지 않게 한다.
# 계약(REQ-004)은 길이만 정하므로 lower(소문자만)·digits(숫자만)도 8자 이상이면 받아야 한다.
PASSWORD_KINDS = {"mixed": string.ascii_letters + string.digits, "lower": string.ascii_lowercase, "digits": string.digits}


def new_password(length=12, kind="mixed"):
    # 그 길이·문자 구성(kind)의 시험용 비밀번호를 실행할 때 만든다(코드에 값을 적지 않는다, dev 19번).
    alphabet = PASSWORD_KINDS[kind]
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if kind != "mixed" or length < 3 or (any(c.islower() for c in value) and any(c.isupper() for c in value)
                                             and any(c.isdigit() for c in value)):
            return value


def new_email(template="user{h}@example.com"):
    # 매번 다른 가입용 이메일. 시험지마다 모양(template, {h}는 무작위 16진수)을 다르게 쓴다.
    return template.format(h=secrets.token_hex(6))


def new_emails(*templates):
    # 같은 무작위 값({h})을 넣은 이메일 여러 개. 점·+태그만 다른 주소 짝을 만들 때 쓴다.
    h = secrets.token_hex(6)
    return [template.format(h=h) for template in templates]


def variant(email, form):
    # 같은 canonical 이메일의 변형(대소문자·앞뒤 공백). 시험지마다 다른 변형을 고른다.
    local, domain = email.split("@")
    return {"same": email, "upper": email.upper(), "swapcase": email.swapcase(),
            "title": local.capitalize() + "@" + domain.upper(), "lead": " " + email, "trail": email + " ",
            "pad": "  " + email + "  ", "upper_pad": " " + email.upper() + " ",
            "swap_lead": "  " + email.swapcase(), "title_trail": local.capitalize() + "@" + domain.upper() + "  ",
            "swap_pad": " " + email.swapcase() + "  "}[form]


def canonical(email):
    # 시나리오 정책(emailPolicy): 앞뒤 공백 제거, 전체 소문자.
    return email.strip().lower()


def _project_root():
    # APP 모듈이 있는 생성물 폴더(끝에 경로 구분자). import하지 않고 위치만 찾는다. 못 찾으면 None.
    try:
        spec = importlib.util.find_spec(APP[0])
    except Exception:
        return None
    if spec is None or not spec.origin or not os.path.isfile(spec.origin):
        return None
    root = os.path.dirname(os.path.abspath(spec.origin))
    for _ in range(APP[0].count(".")):
        root = os.path.dirname(root)
    return root + os.sep


def _forget_project_modules():
    # 앞 검사에서 불러온 생성물 모듈(생성물 폴더 안의 파일)을 잊게 해, 이번 검사가 새 임시 DB(DATABASE_URL)로
    # 앱·엔진을 처음부터 다시 만들게 한다. 라이브러리·표준 모듈과 보호 테스트 자신은 건드리지 않는다.
    root = _project_root()
    if root is None:
        return
    keep = [os.path.dirname(os.path.abspath(__file__)) + os.sep]
    keep += [os.path.abspath(path) + os.sep for path in {sys.prefix, sys.base_prefix, sys.exec_prefix}
             | {sysconfig.get_paths()[name] for name in ("stdlib", "platstdlib", "purelib", "platlib")}]
    if any(path.startswith(root) for path in keep):
        return  # 생성물 폴더가 라이브러리·보호 테스트를 품고 있으면(예: 루트 폴더) 지우지 않는다.
    for name, module in list(sys.modules.items()):
        location = getattr(module, "__file__", None)
        if isinstance(location, str) and os.path.abspath(location).startswith(root):
            del sys.modules[name]
    importlib.invalidate_caches()


def _load(location, code):
    # (모듈, 이름) 위치의 값을 불러온다. 실패하면 code로 Unverified.
    module_name, attribute = location
    try:
        value = getattr(importlib.import_module(module_name), attribute)
    except Exception:
        raise Unverified(code) from None
    return value


_FALLBACK_FORMAT = logging.Formatter("%(name)s %(levelname)s %(message)s")


def _output(handler, record):
    # handler 하나가 내보내는 글(그 handler의 형식을 적용). 형식을 만들다 실패하면 기본 형식, 그것도 안 되면 메시지만.
    for formatter in (handler, _FALLBACK_FORMAT):
        try:
            return formatter.format(record)
        except Exception:
            pass
    return str(record.msg)


def _route(logger):
    # 그 로거의 기록이 가는 handler들(로거 → 부모 로거 순서, 올려 보내지 않는 로거에서 멈춤). Logger.callHandlers와 같은 길.
    found, current = [], logger
    while current is not None:
        found.extend(current.handlers)
        if not current.propagate:
            break
        current = current.parent
    return found


class Service:
    # 테스트 하나마다 새 임시 DB로 앱을 연결한다(앞 검사에서 불러온 생성물 모듈은 잊고 다시 불러온다).
    # open()에서 실패하면 Unverified다. 노출 검사는 stop()으로 앱 종료(shutdown)까지 마친 뒤 모은 출력을 본다.
    # 모으는 출력: 표준 logging handler가 실제로 내보내는 글(필터·형식 적용 뒤), 수준 때문에 지금은 나가지 않는 앱 코드
    # 기록(수준만 바꾸면 나가는 글), sys.stdout·sys.stderr 쓰기. 버리기만 하는 handler(NullHandler)로만 가는 기록은
    # 출력이 아니다. 파일에 직접 쓰기, 다른 로깅 라이브러리가 앱보다 먼저 잡아 둔 출력 통로, 종료 뒤 남는 스레드의
    # 출력은 모으지 못한다(노출 검사의 범위 밖).
    def __init__(self):
        self._undo = []
        self.responses = []
        self.server_log = []
        self._stdout = io.StringIO()
        self._stderr = io.StringIO()

    def open(self):
        # 임시 DB로 앱을 연결하고 시작한다. 연결·시작에 실패하면 지금까지 바꾼 것을 되돌리고 Unverified로 알린다.
        try:
            self._open()
        except Unverified:
            self.close()
            raise
        except Exception:
            self.close()
            raise Unverified("SERVICE_SETUP_FAILED") from None
        return self

    def _open(self):
        # 연결 순서: 임시 폴더·DATABASE_URL → 출력 수집 → 앞 검사의 생성물 모듈 잊기 → 앱·get_db·User 불러오기 →
        # 임시 DB 엔진·테이블 → get_db 바꿔 끼우기 → 실제 서버처럼 시작(lifespan).
        try:
            directory = tempfile.mkdtemp(prefix="signup-eval-")
        except OSError:
            raise Unverified("TEMP_DIRECTORY_UNAVAILABLE") from None
        # 정리 작업은 _undo에 쌓고 close()가 거꾸로 실행한다.
        self._undo.append(lambda: shutil.rmtree(directory, ignore_errors=True))
        url = "sqlite:///" + os.path.join(directory, "signup.db")
        previous_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = url  # 이 값을 읽는 앱이면 import할 때도 임시 DB를 쓴다.
        self._undo.append(lambda: os.environ.pop("DATABASE_URL", None) if previous_url is None
                          else os.environ.__setitem__("DATABASE_URL", previous_url))
        self._capture()
        _forget_project_modules()
        try:
            from fastapi.testclient import TestClient
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
        except Exception:
            raise Unverified("TEST_LIBRARY_UNAVAILABLE") from None
        # 생성물의 앱·DB 의존성·User 모델을 불러오고 규격에 맞는지 본다.
        app = _load(APP, "APP_IMPORT_FAILED")
        self._log_all(_project_root())
        get_db = _load(DB_DEPENDENCY, "DB_DEPENDENCY_MISSING")
        self.User = _load(USER_MODEL, "USER_MODEL_MISSING")
        if inspect.isasyncgenfunction(get_db) or inspect.iscoroutinefunction(get_db):
            raise Unverified("ASYNC_DATABASE_UNSUPPORTED")
        if not all(hasattr(self.User, name) for name in (EMAIL_FIELD, HASH_FIELD)):
            raise Unverified("USER_MODEL_FIELDS_MISSING")
        if not hasattr(app, "dependency_overrides"):
            raise Unverified("APP_NOT_FASTAPI")
        try:
            engine = create_engine(url, connect_args={"check_same_thread": False})
            self._undo.append(engine.dispose)
            self.User.metadata.create_all(engine)
            self._engine = engine
        except Exception:
            raise Unverified("TEST_DATABASE_SETUP_FAILED") from None
        self.Session = sessionmaker(bind=engine, autoflush=False)

        # 앱의 get_db를 임시 DB 세션으로 바꿔 끼운다.
        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self._undo.append(lambda: app.dependency_overrides.pop(get_db, None))
        # 실제 서버처럼 startup/lifespan을 실행하고 종료도 cleanup에 포함한다.
        self._lifecycle = contextlib.ExitStack()
        self._undo.append(self.stop)
        self.client = self._lifecycle.enter_context(TestClient(app))

    def stop(self):
        # 앱을 정상 종료한다(shutdown·lifespan 종료). 종료 중 예외는 실제 서버처럼 traceback을 서버 로그에 남긴다.
        # 두 번 불러도 한 번만 종료한다(close()도 부른다).
        lifecycle, self._lifecycle = getattr(self, "_lifecycle", None), None
        if lifecycle is None:
            return
        try:
            lifecycle.close()
        except Exception as error:
            self.server_log.append("".join(traceback.format_exception(type(error), error, error.__traceback__)))

    def _capture(self):
        # 앱이 실제로 내보내는 로그와 print 출력을 모은다. 필터 전 원문이 아니라 필터·형식을 적용해 나가는 글이 증거다
        # (비밀번호를 가린 뒤 내보내는 정상 앱을 노출로 오판하지 않게, 2026-10-30 외부 점검 3차).
        #   1) handler가 내보낸 글: Handler.handle을 감싸, handler 필터를 통과해 내보낸 기록을 그 handler 형식으로 모은다.
        #      로거마다 새로 만든 handler·루트로 올려 보내지 않는 로거·앱이 시작·요청 중에 만든 로거도 모두 이 길을 지난다.
        #   2) 수준 때문에 아무 handler도 내보내지 않은 앱 코드 기록: Logger.callHandlers를 감싸, 그 기록이 갈 handler들
        #      (없으면 기본 handler)의 필터·형식을 적용한 글을 모은다. 운영 수준만 바꾸면 그대로 나가는 글이기 때문이다.
        sink, root, state = self.server_log, _project_root(), threading.local()
        original_handle, original_call = logging.Handler.handle, logging.Logger.callHandlers

        def handle(handler, record):
            # 원래 처리(필터 → 내보내기)를 한 뒤, 실제로 내보냈으면 그 글을 모은다. 필터가 기록 사본을 돌려주면(3.12부터)
            # 내보내는 것은 그 사본이므로 사본으로 글을 만든다.
            emitted = original_handle(handler, record)
            if emitted:
                state.emitted = True
                sink.append(_output(handler, emitted if isinstance(emitted, logging.LogRecord) else record))
            return emitted

        def call_handlers(logger, record):
            # 원래대로 handler들에 보낸 뒤, 아무도 내보내지 않은 앱 코드 기록이면 수준을 무시하고 갈 handler의 글을 모은다.
            state.emitted = False
            original_call(logger, record)
            if state.emitted or not root or not os.path.abspath(record.pathname or "").startswith(root):
                return
            for handler in _route(logger) or [logging.lastResort]:
                if handler is None or isinstance(handler, logging.NullHandler):
                    continue
                passed = handler.filter(record)
                if passed:
                    sink.append(_output(handler, passed if isinstance(passed, logging.LogRecord) else record))
        logging.Handler.handle, logging.Logger.callHandlers = handle, call_handlers
        self._undo.append(lambda: (setattr(logging.Handler, "handle", original_handle),
                                   setattr(logging.Logger, "callHandlers", original_call)))
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self._stdout, self._stderr
        self._undo.append(lambda: (setattr(sys, "stdout", saved[0]), setattr(sys, "stderr", saved[1])))

    def _log_all(self, root):
        # 앱 폴더(root) 안의 코드가 부른 로그는 수준·logging.disable과 상관없이 모은다.
        # 기본 수준(WARNING)에 막힌 info·debug 로그도 노출로 본다. 라이브러리 로그는 원래 규칙대로 둔다
        # (SQLAlchemy가 SQL과 해시 값을 로그로 남기지 않게).
        if not root:
            return
        original = logging.Logger.isEnabledFor

        def enabled(logger, level):
            frame = sys._getframe(1)
            while frame is not None and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
            return (frame is not None and os.path.abspath(frame.f_code.co_filename).startswith(root)
                    or original(logger, level))
        logging.Logger.isEnabledFor = enabled
        self._undo.append(lambda: setattr(logging.Logger, "isEnabledFor", original))

    def close(self):
        # 정리 작업을 거꾸로 실행한다. 하나가 실패해도 나머지는 계속한다.
        while self._undo:
            try:
                self._undo.pop()()
            except Exception:
                pass

    def signup(self, email, password, client=None):
        # 이메일·비밀번호로 가입 요청을 보내고 응답 코드를 돌려준다.
        return self.send(signup_body(email, password), client)

    def send(self, body, client=None):
        # 가입 요청(본문은 JSON 값). 앱이 처리하지 못한 예외는 실제 서버처럼 500으로 보고 traceback을 서버 로그에 남긴다.
        try:
            response = (client or self.client).post(SIGNUP_PATH, json=body)
        except Exception as error:
            if isinstance(error, Unverified):
                raise
            self.server_log.append("".join(traceback.format_exception(type(error), error, error.__traceback__)))
            return 500
        headers = "\n".join(k + ": " + v for k, v in response.headers.items())
        self.responses.append(response.text + "\n" + headers)
        if response.status_code in (404, 405):
            # 가입 경로·메서드가 연결 규격과 다르다. 모든 요청이 4xx가 되어 거부 검사가 거저 통과하지 않게 막는다.
            raise Unverified("SIGNUP_ROUTE_NOT_FOUND")
        return response.status_code

    def emails(self):
        # DB에 저장된 이메일 목록(가입 결과 확인용)
        try:
            with self.Session() as db:
                rows = db.query(getattr(self.User, EMAIL_FIELD)).all()
        except Exception:
            raise Unverified("TEST_DATABASE_READ_FAILED") from None
        return [row[0] for row in rows]

    def count(self):
        # 저장된 사용자 수
        return len(self.emails())

    def count_canonical(self, email):
        # 그 이메일과 canonical 값(앞뒤 공백 제거·소문자)이 같은 사용자 수
        return sum(1 for value in self.emails() if isinstance(value, str) and canonical(value) == canonical(email))

    def rows(self):
        # 사용자 표의 모든 행·모든 칸 값(정렬). 거부된 요청이 기존 계정을 바꾸지 않았는지 비교하는 데 쓴다.
        from sqlalchemy import inspect as sa_inspect
        try:
            mapper = sa_inspect(self.User)
            with self.Session() as db:
                found = [tuple(repr(getattr(row, attribute.key)) for attribute in mapper.column_attrs)
                         for row in db.query(self.User).all()]
        except Exception:
            raise Unverified("TEST_DATABASE_READ_FAILED") from None
        return sorted(found)

    def stored_hash(self, email):
        # 그 이메일 사용자의 저장 해시. 정확히 1명이 아니면 Unverified.
        try:
            with self.Session() as db:
                rows = db.query(getattr(self.User, EMAIL_FIELD), getattr(self.User, HASH_FIELD)).all()
        except Exception:
            raise Unverified("TEST_DATABASE_READ_FAILED") from None
        found = [stored for address, stored in rows
                 if isinstance(address, str) and canonical(address) == canonical(email)]
        if len(found) != 1:
            raise Unverified("STORED_USER_NOT_FOUND")
        return found[0]

    def stored_email(self, email):
        # 그 canonical 이메일로 저장된 이메일 값 그대로. 정확히 1명이 아니면 Unverified.
        found = [value for value in self.emails() if isinstance(value, str) and canonical(value) == canonical(email)]
        if len(found) != 1:
            raise Unverified("STORED_USER_NOT_FOUND")
        return found[0]

    def fail_next_insert(self):
        # 다음 INSERT 한 번을 SQLite가 실제로 거부하게 한다(권한 함수 set_authorizer). DB 단계에서 난 오류라
        # SQLAlchemy 버전과 상관없이 SQL·값을 담은 DB 오류로 감싸진다. 그다음 실행에서 권한 함수를 풀어 준다.
        import sqlite3
        from sqlalchemy import event
        armed, denied = [True], []

        def deny(action, *args):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT else sqlite3.SQLITE_OK

        def release():
            while denied:
                denied.pop().set_authorizer(None)

        def fail(conn, cursor, statement, parameters, context, many):
            release()
            if armed[0] and statement.lstrip().upper().startswith("INSERT"):
                armed[0] = False
                raw = conn.connection
                raw = getattr(raw, "dbapi_connection", None) or getattr(raw, "connection", raw)
                raw.set_authorizer(deny)
                denied.append(raw)
        event.listen(self._engine, "before_cursor_execute", fail)
        self._undo.append(lambda: (release(), event.remove(self._engine, "before_cursor_execute", fail)))

    def db_rejects_duplicate(self, email):
        # 저장된 사용자 행을 복사해 DB에 직접 한 번 더 넣는다(앱의 사전 검사를 거치지 않음).
        # 이메일 칸 UNIQUE로 거부되면 True, 들어가면 False. 다른 이유로 실패하거나 UNIQUE 대상이 이메일 칸 하나가
        # 아니면 Unverified.
        from sqlalchemy import inspect as sa_inspect
        from sqlalchemy.exc import IntegrityError
        try:
            mapper = sa_inspect(self.User)
            column = mapper.column_attrs[EMAIL_FIELD].columns[0]
            with self.Session() as db:
                rows = [row for row in db.query(self.User).all()
                        if isinstance(getattr(row, EMAIL_FIELD), str) and canonical(getattr(row, EMAIL_FIELD)) == canonical(email)]
                if len(rows) != 1:
                    raise Unverified("STORED_USER_NOT_FOUND")
                copy = mapper.class_manager.new_instance()
                for attribute in mapper.column_attrs:
                    if not attribute.columns[0].primary_key:
                        setattr(copy, attribute.key, getattr(rows[0], attribute.key))
                db.add(copy)
                try:
                    db.commit()
                except IntegrityError as error:
                    if str(error.orig).strip() == "UNIQUE constraint failed: " + column.table.name + "." + column.name:
                        return True
                    raise Unverified("UNIQUE_CONSTRAINT_UNCLEAR") from None
                return False
        except Unverified:
            raise
        except Exception:
            raise Unverified("TEST_DATABASE_WRITE_FAILED") from None

    def log_text(self):
        # 서버 로그와 stdout·stderr 출력을 합친 글(노출 검사용)
        return "\n".join(self.server_log) + "\n" + self._stdout.getvalue() + "\n" + self._stderr.getvalue()

    def concurrent_signups(self, email, password, workers=5):
        # 같은 이메일 가입을 스레드 여러 개로 동시에 보낸다. 실행 자체가 안 되면 Unverified다.
        # 모든 요청을 같은 client로 보내 같은 lifespan 상태·같은 이벤트 루프에서 처리되게 한다(실제 서버 한 대와 같음).
        barrier = threading.Barrier(workers)
        statuses, unverified = [None] * workers, []

        # 스레드마다 barrier에서 함께 출발해 요청한다.
        def run(index):
            barrier.wait(timeout=30)
            try:
                statuses[index] = self.signup(email, password)
            except Unverified as error:
                unverified.append(error.code)

        threads = [threading.Thread(target=run, args=(index,), daemon=True) for index in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        if unverified:
            raise Unverified(unverified[0])
        if any(thread.is_alive() for thread in threads) or None in statuses:
            raise Unverified("CONCURRENCY_HARNESS_ERROR")
        return statuses


class SignupCheck(unittest.TestCase):
    # 보호 테스트의 공통 틀. check(body)는 새 서비스로 body를 실행하고 Unverified를 SKIP으로 바꾼다.
    # subTest 안에서 난 예외는 unittest가 실패로 기록하므로, 하위 사례는 case()로 감싸 SKIP으로 바꾼다.
    @contextlib.contextmanager
    def case(self, **params):
        with self.subTest(**params):
            try:
                yield
            except Unverified as error:
                self.skipTest("UNVERIFIED:" + error.code)

    def check(self, body):
        service = Service()
        try:
            service.open()
        except Unverified as error:
            self.skipTest("UNVERIFIED:" + error.code)
        self.addCleanup(service.close)
        try:
            body(service)
        except Unverified as error:
            self.skipTest("UNVERIFIED:" + error.code)
