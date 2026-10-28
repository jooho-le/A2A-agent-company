# 회원가입 보호 테스트의 연결부. 생성물 규격(시작 코드)이 바뀌면 이 파일의 상수만 고친다.
# 지금은 세화 backend 구조를 가정한다: main.app, database.get_db, models.User(email, password_hash),
# POST /api/auth/signup {email, password}.
# 연결·전제 조건을 확인하지 못하면 제품 결함(FAIL)이 아니라 Unverified를 내고,
# 테스트는 이를 SKIP("UNVERIFIED:코드")로 바꾼다. 비밀번호는 실행할 때 만들고(dev 19번)
# 실패 메시지에 비밀번호·해시 값을 넣지 않는다.
import contextlib
import importlib
import inspect
import io
import logging
import os
import secrets
import shutil
import string
import sys
import tempfile
import threading
import traceback
import unittest

APP = ("main", "app")
DB_DEPENDENCY = ("database", "get_db")
USER_MODEL = ("models", "User")
EMAIL_FIELD = "email"
HASH_FIELD = "password_hash"
SIGNUP_PATH = "/api/auth/signup"
SUCCESS = range(200, 300)   # 성공 응답
REJECTED = range(400, 500)  # 입력 검증·중복 실패 응답(5xx는 처리하지 못한 서버 오류)


def signup_body(email, password):
    return {"email": email, "password": password}


class Unverified(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def new_password(length=12):
    # 대문자·소문자·숫자를 하나씩 넣어 길이 말고 다른 규칙 때문에 거부되지 않게 한다.
    alphabet = string.ascii_letters + string.digits
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in value) and any(c.isupper() for c in value)
                and any(c.isdigit() for c in value)):
            return value


def new_email():
    return "user" + secrets.token_hex(6) + "@example.com"


def canonical(email):
    # 시나리오 정책(emailPolicy): 앞뒤 공백 제거, 전체 소문자.
    return email.strip().lower()


def _load(location, code):
    module_name, attribute = location
    try:
        value = getattr(importlib.import_module(module_name), attribute)
    except Exception:
        raise Unverified(code) from None
    return value


class _LogCapture(logging.Handler):
    # 앱이 남긴 로그를 서버 로그로 모은다. 로그 수준은 앱 설정 그대로 두고 바꾸지 않는다.
    def __init__(self, sink):
        super().__init__(logging.NOTSET)
        self.sink = sink
        self.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    def emit(self, record):
        try:
            self.sink.append(self.format(record))
        except Exception:
            self.sink.append(str(record.msg))


class Service:
    # 테스트 하나마다 새 임시 DB로 앱을 연결한다. open()에서 실패하면 Unverified다.
    def __init__(self):
        self._undo = []
        self.responses = []
        self.server_log = []
        self._stdout = io.StringIO()
        self._stderr = io.StringIO()

    def open(self):
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
        try:
            directory = tempfile.mkdtemp(prefix="signup-eval-")
        except OSError:
            raise Unverified("TEMP_DIRECTORY_UNAVAILABLE") from None
        self._undo.append(lambda: shutil.rmtree(directory, ignore_errors=True))
        url = "sqlite:///" + os.path.join(directory, "signup.db")
        previous_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = url  # 이 값을 읽는 앱이면 import할 때도 임시 DB를 쓴다.
        self._undo.append(lambda: os.environ.pop("DATABASE_URL", None) if previous_url is None
                          else os.environ.__setitem__("DATABASE_URL", previous_url))
        self._capture()
        try:
            from fastapi.testclient import TestClient
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
        except Exception:
            raise Unverified("TEST_LIBRARY_UNAVAILABLE") from None
        app = _load(APP, "APP_IMPORT_FAILED")
        get_db = _load(DB_DEPENDENCY, "DB_DEPENDENCY_MISSING")
        self.User = _load(USER_MODEL, "USER_MODEL_MISSING")
        self._attach_isolated_loggers()  # import하면서 새로 생긴 로거도 모은다.
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
        except Exception:
            raise Unverified("TEST_DATABASE_SETUP_FAILED") from None
        self.Session = sessionmaker(bind=engine, autoflush=False)

        def override():
            with self.Session() as db:
                yield db

        app.dependency_overrides[get_db] = override
        self._undo.append(lambda: app.dependency_overrides.pop(get_db, None))
        self._app, self._client_type = app, TestClient
        # 실제 서버처럼 startup/lifespan을 실행하고 종료도 cleanup에 포함한다.
        lifecycle = contextlib.ExitStack()
        self._undo.append(lifecycle.close)
        self.client = lifecycle.enter_context(TestClient(app))

    def _capture(self):
        # 루트 로거와, 루트로 올려 보내지 않는(propagate=False) 로거에 수집기를 단다. print는 stdout·stderr로 모은다.
        self._handler = _LogCapture(self.server_log)
        self._attached = set()
        self._attach(logging.getLogger())
        self._attach_isolated_loggers()
        saved = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = self._stdout, self._stderr
        self._undo.append(lambda: (setattr(sys, "stdout", saved[0]), setattr(sys, "stderr", saved[1])))

    def _attach(self, logger):
        if id(logger) not in self._attached:
            self._attached.add(id(logger))
            logger.addHandler(self._handler)
            self._undo.append(lambda: logger.removeHandler(self._handler))

    def _attach_isolated_loggers(self):
        for item in list(logging.Logger.manager.loggerDict.values()):
            if isinstance(item, logging.Logger) and not item.propagate:
                self._attach(item)

    def close(self):
        while self._undo:
            try:
                self._undo.pop()()
            except Exception:
                pass

    def signup(self, email, password, client=None):
        # 앱이 처리하지 못한 예외는 실제 서버처럼 500으로 보고 traceback을 서버 로그에 남긴다.
        try:
            response = (client or self.client).post(SIGNUP_PATH, json=signup_body(email, password))
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
        try:
            with self.Session() as db:
                rows = db.query(getattr(self.User, EMAIL_FIELD)).all()
        except Exception:
            raise Unverified("TEST_DATABASE_READ_FAILED") from None
        return [row[0] for row in rows]

    def count(self):
        return len(self.emails())

    def count_canonical(self, email):
        return sum(1 for value in self.emails() if isinstance(value, str) and canonical(value) == canonical(email))

    def stored_hash(self, email):
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

    def log_text(self):
        return "\n".join(self.server_log) + "\n" + self._stdout.getvalue() + "\n" + self._stderr.getvalue()

    def concurrent_signups(self, email, password, workers=5):
        # 같은 이메일 가입을 스레드 여러 개로 동시에 보낸다. 실행 자체가 안 되면 Unverified다.
        barrier = threading.Barrier(workers)
        statuses, unverified = [None] * workers, []

        def run(index):
            client = self._client_type(self._app)
            barrier.wait(timeout=30)
            try:
                statuses[index] = self.signup(email, password, client)
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
