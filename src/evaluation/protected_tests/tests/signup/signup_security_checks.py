# SCN-001 회원가입 Security 검사 내용(입력은 시험지 파일이 정한다). QA 검사와 같은 방식(실행 테스트)이다.
# 해시 저장값(REQ-005), DB UNIQUE·이메일 정규화 저장(REQ-003), 비밀번호·해시 노출(REQ-006).
import base64
import binascii
import itertools
import re
import secrets
import sys
from contextlib import nullcontext
from contextvars import ContextVar
from types import CodeType, FunctionType

import signup_link as link

# 시나리오 정책 passwordHashPolicy 값. 비교 규칙은 "이상"(AT_LEAST, 2026-10-28 4번 결정).
ARGON2_MINIMUM = {"m": 19456, "t": 2, "p": 1}
# 실제 비밀번호 해시 값의 모양(Argon2·bcrypt·PBKDF2·scrypt). 형식 이름만 적힌 글은 노출이 아니므로
# salt·해시까지 갖춘 모양만 찾는다(D4 trace_checks와 같은 규칙).
HASH_FORMAT = re.compile(
    r"[$]argon2(id|i|d)[$](v=[0-9]+[$])?m=[0-9]+,t=[0-9]+,p=[0-9]+[$][A-Za-z0-9+/.=-]{8,}[$][A-Za-z0-9+/.=-]{16,}"
    r"|[$]2[abxy][$][0-9]{2}[$][./A-Za-z0-9]{53}"
    r"|[$]pbkdf2(-[a-z0-9]+)?[$][0-9]+[$][A-Za-z0-9+/.=-]{4,}[$][A-Za-z0-9+/.=-]{16,}"
    r"|[$]scrypt[$]ln=[0-9]+,r=[0-9]+,p=[0-9]+[$][A-Za-z0-9+/.=-]{4,}[$][A-Za-z0-9+/.=-]{16,}")
# 형식이 틀린 요청 본문. 기본 검증 오류 응답이 입력을 그대로 돌려주면 비밀번호가 실린다.
MALFORMED = {"missing_email": lambda email, password: {"password": password},
             "list": lambda email, password: [email, password],
             "misspelled": lambda email, password: {"email": email, "passwd": password}}


def new_emails(*templates):
    # 같은 무작위 값({h})을 넣은 이메일 여러 개. 점·+태그만 다른 주소 짝을 만들 때 쓴다.
    def produce():
        h = secrets.token_hex(6)
        return [template.format(h=h) for template in templates]
    return link._input("emails", {"templates": list(templates)}, produce)


def parse_argon2(encoded):
    # 구조 해석과 Argon2id 정책 판정을 분리한다. 다른 Argon2 방식도 읽을 수 있으면 계속 검증한다.
    parts = encoded.split("$") if isinstance(encoded, str) else []
    if len(parts) != 6 or parts[0] or parts[1] not in ('argon2id', 'argon2i', 'argon2d') or not parts[2].startswith("v="):
        return None
    try:
        params = dict(item.split("=", 1) for item in parts[3].split(","))
        values = {name: int(params[name]) for name in ARGON2_MINIMUM}
    except (KeyError, ValueError):
        return None
    if not parts[4] or not parts[5]:
        return None
    return values, parts[4], parts[1]


def read_stored_hash(service, email):
    # 그 이메일 사용자의 저장 해시. 정확히 1명이 아니면 Unverified.
    try:
        with service.Session() as db:
            rows = db.query(getattr(service.User, link.EMAIL_FIELD), getattr(service.User, link.HASH_FIELD)).all()
    except Exception:
        raise link.Unverified("TEST_DATABASE_READ_FAILED") from None
    found = [stored for address, stored in rows
             if isinstance(address, str) and link.canonical(address) == link.canonical(email)]
    if len(found) != 1:
        raise link.Unverified("STORED_USER_NOT_FOUND")
    return found[0]

def read_stored_email(service, email):
    # 그 canonical 이메일로 저장된 이메일 값 그대로. 정확히 1명이 아니면 Unverified.
    found = [value for value in service.emails() if isinstance(value, str) and link.canonical(value) == link.canonical(email)]
    if len(found) != 1:
        raise link.Unverified("STORED_USER_NOT_FOUND")
    return found[0]

def db_rejects_duplicate(service, email):
    # 저장된 사용자 행을 복사해 DB에 직접 한 번 더 넣는다(앱의 사전 검사를 거치지 않음).
    # 이메일 칸 UNIQUE로 거부되면 True, 들어가면 False. 다른 이유로 실패하거나 UNIQUE 대상이 이메일 칸 하나가
    # 아니면 Unverified.
    from sqlalchemy import create_engine, inspect as sa_inspect
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import sessionmaker
    # 이 검사는 UNIQUE 제약을 직접 확인하므로 관찰 세션과 별개의 쓰기 세션만 잠깐 연다.
    engine = create_engine(service._database_url, connect_args={"check_same_thread": False})
    service._owned_engines.add(engine)
    try:
        mapper = sa_inspect(service.User)
        column = mapper.column_attrs[link.EMAIL_FIELD].columns[0]
        with sessionmaker(bind=engine, autoflush=False)() as db:
            # 0.7.2: 글자가 아닌 값은 str()로 바꿔도 이메일과 같아질 수 없어 isinstance 검사를 뺐다(묶음 크기 여유 확보).
            rows = [row for row in db.query(service.User).all() if link.canonical(str(getattr(row, link.EMAIL_FIELD))) == link.canonical(email)]
            if len(rows) != 1:
                raise link.Unverified("STORED_USER_NOT_FOUND")
            copy = mapper.class_manager.new_instance()
            # 0.7.2: 이메일 칸이 기본 키여도 그 값은 복사한다(빼면 이메일 없는 행이 들어가 판단 불가였다).
            # 같은 기본 키 객체가 세션에 둘이 되지 않게 원래 행은 세션에서 뗀다.
            for prop in mapper.column_attrs:
                if prop.key == link.EMAIL_FIELD or not prop.columns[0].primary_key:
                    setattr(copy, prop.key, getattr(rows[0], prop.key))
            db.expunge(rows[0])
            db.add(copy)
            try:
                db.commit()
            except IntegrityError as error:
                if str(error.orig).strip() == "UNIQUE constraint failed: " + column.table.name + "." + column.name:
                    return True
                raise link.Unverified("UNIQUE_CONSTRAINT_UNCLEAR") from None
            return False
    except link.Unverified:
        raise
    except Exception:
        raise link.Unverified("TEST_DATABASE_WRITE_FAILED") from None
    finally:
        engine.dispose()
        service._owned_engines.discard(engine)


def stored_hash(test, data):
    test.prepare("SEC-SIGNUP-002", data)
    # SEC-SIGNUP-002: 같은 비밀번호로 계정 여러 개를 만들고 저장값의 형식·설정값·salt·검증 결과를 본다.
    def body(service):
        try:
            from argon2 import PasswordHasher, exceptions
        except ImportError:
            raise link.Unverified("ARGON2_UNAVAILABLE") from None
        rejected = (exceptions.VerificationError, exceptions.InvalidHashError)
        password = link.new_password(data["length"])
        emails = [link.new_email(data["template"]) for _ in range(data["accounts"])]
        stored = {}
        for index, email in enumerate(emails):
            ready = None
            with test.case(account=index):
                ready = test.observe(f'account_{index + 1}_setup', 'http_status', lambda: service.signup(email, password), required=False)
                if not ready:
                    raise link.Unverified("PRECONDITION_SIGNUP_FAILED")
            if ready is None:
                continue
            with test.case(account=index):
                stored[index] = read_stored_hash(service, email)
        parsed = {index: parse_argon2(value) for index, value in stored.items()}
        complete = len(stored) == len(emails)
        block = test.detail_recorder.block
        need = 'HASH_VALUES_REQUIRED'
        for item, passed in [('no_plaintext', all(isinstance(v, str) and password not in v for v in stored.values())),
                             ('argon2id', all(p and p[2] == 'argon2id' for p in parsed.values()))]:
            if complete or not passed:
                test.observe('storage', item, passed)
            else:
                block('storage', [item], need)
        hasher = PasswordHasher()

        def verifies(value, candidate):
            try:
                return bool(hasher.verify(value, candidate))
            except rejected:
                return False

        for index in range(len(emails)):
            case = f'account_{index + 1}'
            if not parsed.get(index):
                reason = 'HASH_FORMAT_REQUIRED' if index in stored else need
                block(case, [*ARGON2_MINIMUM, 'correct_password', 'wrong_password'], reason)
                continue
            for name in ARGON2_MINIMUM:
                test.observe(case, name, parsed[index][0][name])
            test.observe(case, 'correct_password', lambda: verifies(stored[index], password))
            test.observe(case, 'wrong_password', lambda: verifies(stored[index], link.new_password()))
        if complete and all(parsed.values()):
            test.observe('storage', 'distinct_salts', len({p[1] for p in parsed.values()}))
        else:
            block('storage', ['distinct_salts'], 'HASH_FORMAT_REQUIRED' if complete else need)
    test.check(body)


def unique_and_canonical(test, data):
    test.prepare("SEC-SIGNUP-003", data)
    # SEC-SIGNUP-003: 대소문자·공백 변형으로 가입하면 정리된(canonical) 값으로 저장되고, 앱의 사전 검사를 거치지 않고
    # 같은 이메일 행을 DB에 직접 넣으면 DB가 이메일 칸 UNIQUE로 거부해야 한다. 정리는 앞뒤 공백 제거·소문자만이다
    # (정책: Gmail 점 제거·+태그 제거 안 함). 그래서 점·+태그만 다른 두 주소(aliases)는 각각 가입되고 그대로 저장돼야 한다.
    def body(service):
        if not test.observe('precondition', 'http_status', lambda: service.signup(link.new_email(), link.new_password()), required=False):
            raise link.Unverified("PRECONDITION_SIGNUP_FAILED")
        for form in data["forms"]:
            case = "variant_" + form
            with test.case(variant=form):
                email = link.new_email(data["template"])
                status = service.signup(link.variant(email, form), link.new_password())
                test.observe(case, 'http_status', status)
                test.observe(case, 'canonical', lambda: read_stored_email(service, email) == link.canonical(email))
                test.observe(case, 'unique', lambda: db_rejects_duplicate(service, email))
        for index, pair in enumerate(data["aliases"]):
            with test.case(alias=index):
                addresses = new_emails(*pair)
                for position, address in enumerate(addresses):
                    test.observe(f'alias_{index + 1}_address_{position + 1}', 'http_status',
                                 lambda: service.signup(address, link.new_password()))
                stored = service.emails()
                for position, address in enumerate(addresses):
                    test.observe(f'alias_{index + 1}_address_{position + 1}', 'account_count', stored.count(link.canonical(address)))
    test.check(body)


# 글 속에서 base64·16진수로 바꿔 쓴 값의 후보. 풀어서 해시 모양이 나오면 노출이다(실패·거부 요청에서 새로 만들어
# 저장되지 않은 해시처럼 미리 알 수 없는 값). 후보 길이·개수에 상한을 두고 한 번만 푼다(중첩 풀기 없음).
_BASE64_TOKEN = re.compile(r"[A-Za-z0-9+/_-]{40,4096}={0,2}")
_HEX_TOKEN = re.compile(r"(?:[0-9a-fA-F]{2}){32,2048}")
MAX_DECODED_TOKENS = 2000


def _decoded_texts(text):
    # text 안의 base64·16진수 후보를 풀어 글자(UTF-8로 읽히는 것만)로 돌려준다.
    found = []
    tokens = [match.group(0) for pattern in (_BASE64_TOKEN, _HEX_TOKEN)
              for match in itertools.islice(pattern.finditer(text), MAX_DECODED_TOKENS)]
    for token in tokens:
        candidates = []
        try:
            candidates.append(bytes.fromhex(token))
        except ValueError:
            pass
        padded = token.rstrip("=") + "=" * (-len(token.rstrip("=")) % 4)
        for decode in (base64.b64decode, base64.urlsafe_b64decode):
            try:
                candidates.append(decode(padded))
            except (binascii.Error, ValueError):
                pass
        for data in candidates:
            try:
                found.append(data.decode("utf-8"))
            except UnicodeDecodeError:
                pass
    return found


def _hash_shown(text):
    # 글에 해시 값 모양이 그대로 있거나, base64·16진수로 바꾼 해시가 있는가
    return bool(HASH_FORMAT.search(text)) or any(HASH_FORMAT.search(value) for value in _decoded_texts(text))


def _forms(value):
    # 값 그대로와 흔히 바꿔 쓰는 형태(base64·URL용 base64·16진수)
    raw = value.encode("utf-8")
    return {value, base64.b64encode(raw).decode().rstrip("="), base64.urlsafe_b64encode(raw).decode().rstrip("="),
            raw.hex()}


# 0.7.3(2026-11-18): DB 오류 주입은 Security 검사(SEC-SIGNUP-005)만 쓰므로 signup_link.Service에서 이 파일로 옮겼다.
# 보호 파일은 범위마다 Unit 설정(dev 한도 64KiB)에 그대로 들어가 QA 범위에서 빠지는 만큼 여유가 생긴다. 코드는 같다.
def fail_next_insert(service):
    # 다음 INSERT 한 번을 SQLite가 실제로 거부하게 한다(권한 함수 set_authorizer). DB 단계에서 난 오류라
    # SQLAlchemy 버전과 상관없이 SQL·값을 담은 DB 오류로 감싸진다. 그다음 실행에서 권한 함수를 풀어 준다.
    import sqlite3
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    armed, denied = [True], []
    service._insert_failure = armed

    def deny(action, *args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT else sqlite3.SQLITE_OK

    def release():
        while denied:
            denied.pop().set_authorizer(None)

    def fail(conn, cursor, statement, parameters, context, many):
        if conn.engine in service._owned_engines or not service._same_database(conn.engine):
            return
        release()
        if armed[0] and statement.lstrip().upper().startswith("INSERT"):
            armed[0] = False
            raw = conn.connection
            raw = getattr(raw, "dbapi_connection", None) or getattr(raw, "connection", raw)
            raw.set_authorizer(deny)
            denied.append(raw)
    event.listen(Engine, "before_cursor_execute", fail)

    def cleanup():
        try:
            release()
        finally:
            event.remove(Engine, "before_cursor_execute", fail)

    service._undo.append(cleanup)


def require_insert_failure(service):
    if service._insert_failure[0]:
        raise link.Unverified("DATABASE_ERROR_NOT_INJECTED")


def observed_signup(request):
    # Observe one request without replacing the route, dependencies, or response.
    from fastapi import routing
    monitor = getattr(sys, 'monitoring', None)
    route = [c for c in routing.get_request_handler.__code__.co_consts
             if isinstance(c, CodeType) and c.co_name == 'app']
    if not monitor or len(route) != 1:
        return request(), 'UNKNOWN'
    route = route[0]
    dispatch = routing.run_endpoint_function.__code__
    active = ContextVar('signup_observation', default=False)
    watched = set()
    matched = 0
    endpoint = route_request = None
    dispatched = entered = rejected = other_entry = False

    def start(code, offset):
        nonlocal matched, endpoint, route_request, dispatched, entered, other_entry
        if not active.get():
            return
        if code is route:
            local = sys._getframe(1).f_locals
            scope = getattr(local.get('request'), 'scope', {})
            if scope.get('method') == 'POST' and scope.get('path') == link.SIGNUP_PATH:
                matched += 1
                candidate = scope.get('endpoint')
                if type(candidate) is FunctionType and getattr(local.get('dependant'), 'call', None) is candidate:
                    endpoint = candidate
                    route_request = local['request']
                    watch(candidate.__code__, monitor.events.PY_START)
        elif code is dispatch:
            dependant = sys._getframe(1).f_locals.get('dependant')
            if endpoint is not None and getattr(dependant, 'call', None) is endpoint:
                dispatched = True
        elif endpoint is not None and code is endpoint.__code__:
            if dispatched:
                entered = True
            else:
                other_entry = True

    def call(code, offset, function, arg):
        nonlocal rejected
        if (active.get() and code is route and function is routing.RequestValidationError
                and route_request is not None and sys._getframe(1).f_locals.get('request') is route_request):
            rejected = True

    def watch(code, events):
        monitor.set_local_events(tool, code, events)
        watched.add(code)

    for tool in (3, 4):
        try:
            monitor.use_tool_id(tool, 'signup_entry')
            break
        except ValueError:
            continue
    else:
        return request(), 'UNKNOWN'
    token = active.set(True)
    try:
        monitor.register_callback(tool, monitor.events.PY_START, start)
        monitor.register_callback(tool, monitor.events.CALL, call)
        watch(route, monitor.events.PY_START | monitor.events.CALL)
        watch(dispatch, monitor.events.PY_START)
        result = request()
        state = 'UNKNOWN'
        if matched == 1 and endpoint is not None:
            if entered:
                state = 'ENTERED'
            elif rejected and not dispatched and not other_entry:
                state = 'NOT_ENTERED'
        return result, state
    finally:
        active.reset(token)
        for code in watched:
            monitor.set_local_events(tool, code, 0)
        monitor.register_callback(tool, monitor.events.CALL, None)
        monitor.register_callback(tool, monitor.events.PY_START, None)
        monitor.free_tool_id(tool)


def not_exposed(test, data):
    test.prepare("SEC-SIGNUP-005", data)
    # SEC-SIGNUP-005: 성공·형식 오류·짧은 비밀번호·중복·형식이 틀린 본문·DB 오류 요청의 응답(본문·헤더)과
    # 앱 시작부터 종료까지의 서버 로그·출력에서 비밀번호와 해시를 찾는다(그대로·base64·16진수). 거부된 요청이나 DB 오류 때 계산한
    # 해시도 있으므로 해시 모양 자체도 찾는다(예: 처리하지 못한 DB 오류의 traceback에 SQL 값으로 찍힌 해시). 그런 해시를
    # base64·16진수로 바꿔 남긴 경우도 후보를 풀어서 찾는다.
    # 0.7.3(2026-11-18): 합격 기준은 그대로 두고, 고칠 위치를 알 수 있게 관찰만 더 남긴다(실제 Run d3a18c1c에서
    # Developer가 "비밀번호 노출"만 보고 로그 단어 필터를 넣고, 실제 경로인 형식 오류 응답·DB 예외를 놓쳤다).
    # - 요청 사례마다 받은 응답의 상태 코드(준비 조건, 100~599면 PASS).
    # - 응답이 없는 사례: link.Service.send는 앱이 처리하지 못한 예외일 때만 응답을 남기지 않는다(500, traceback은
    #   서버 로그). 그래서 그 사례의 상태 코드·노출 항목은 미검증 UNHANDLED_APP_EXCEPTION이다(전에는 RESPONSE_NOT_CAPTURED).
    # - 로그 노출 중 처리하지 못한 예외의 traceback(서버 로그에서 "Traceback"으로 시작하는 기록)에도 있는 노출.
    #   로그 노출이 있을 때만 참이라 FAIL 조건이 늘지 않는다.
    # 보호 파일은 Unit 설정(64KiB 한도)에 그대로 들어가므로 코드를 짧게 쓴다.
    def body(service):
        email, used = link.new_email(data["template"]), []
        observer = getattr(sys.modules.get('_host_app_test'), 'observe_request', None)
        index = test.detail_recorder.index
        captured, statuses, probes = {}, {}, {}

        def send(case, request):
            # 요청을 보내고 그 사례의 응답·상태 코드를 남긴다.
            before = len(service.responses)
            with observer(service.client) if observer else nullcontext() as probe:
                statuses[case], entry = observed_signup(request)
            if probe is not None:
                probes[case] = probe
            index[case, 'response_status']['handlerEntry'] = entry
            captured[case] = service.responses[before:]
            return statuses[case]

        def attempt(case, address, password):
            # 가입 요청을 보내고, 쓴 비밀번호를 찾을 값 목록에 넣는다.
            used.append(password)
            return send(case, lambda: service.signup(address, password))

        stored, seeded = None, False
        with test.case(case='seed_account'):
            if not test.observe('precondition', 'http_status', lambda: attempt('success', email, link.new_password()), required=False):
                raise link.Unverified("PRECONDITION_SIGNUP_FAILED")
        with test.case(case='stored_hash'):
            stored = read_stored_hash(service, email)
            seeded = True
            if not isinstance(stored, str) or not stored:
                raise link.Unverified('HASH_VALUES_REQUIRED')
        with test.case(case='invalid_email'):
            attempt("invalid_email", data["invalid"], link.new_password())
        with test.case(case='short_password'):
            attempt("short_password", link.new_email(), link.new_password(data["short"]))
        if seeded:
            with test.case(case='duplicate'):
                attempt("duplicate", link.variant(email, data["duplicate"]), link.new_password())
        else:
            test.detail_recorder.block('duplicate', ('response_status', 'password_exposed', 'hash_exposed'),
                                       'STORED_USER_REQUIRED')
        for kind in data["malformed"]:  # 형식이 틀린 본문 세 종류(필드 누락·목록 본문·필드 이름 오타)
            password = link.new_password()
            used.append(password)
            with test.case(case=kind):
                send("malformed_" + kind, lambda: service.send(MALFORMED[kind](link.new_email(), password)))
        with test.case(case='db_error'):
            fail_next_insert(service)
            attempt("db_error", link.new_email(), link.new_password())
            test.observe("db_error", "injected", lambda: (require_insert_failure(service), True)[1])
        if index['db_error', 'injected']['status'] != 'PASS':
            captured.pop('db_error', None)
            test.detail_recorder.block('db_error', ('response_status', 'password_exposed', 'hash_exposed'),
                                       'PRECONDITION_OR_OBSERVATION_UNAVAILABLE')
        secrets = list(used)
        if isinstance(stored, str) and stored:
            secrets.append(stored)
            digest = stored.rsplit("$", 1)[-1]
            if len(digest) >= 16:
                secrets.append(digest)
        service.stop()  # 종료할 때 남기는 로그까지 모은 뒤 판정한다.
        logs = service.log_text()
        password_forms = {form for value in used for form in _forms(value)}
        hash_forms = {form for value in secrets[len(used):] for form in _forms(value)}

        def leaks(forms, texts, shape=False):
            # 값 그대로·바꾼 형태가 있는가. 해시는 해시 모양 자체도 찾는다(shape). 항상 bool이다.
            return any(value in text for value in forms for text in texts) or shape and any(_hash_shown(text) for text in texts)

        captured['application_logs'] = [logs]
        for case, texts in captured.items():
            if not texts:
                for item in ('response_status', 'password_exposed', 'hash_exposed'):
                    index[case, item].update(status='UNVERIFIED', reason='UNHANDLED_APP_EXCEPTION')
                if case in probes:
                    probes[case].annotate(index[case, 'response_status'])
                continue
            if case in statuses:
                test.observe(case, 'response_status', statuses[case], required=False)
            for item, forms, shape in (('password_exposed', password_forms, False), ('hash_exposed', hash_forms, True)):
                exposed = leaks(forms, texts, shape)
                if shape and not hash_forms and not exposed:
                    test.detail_recorder.block(case, [item], 'HASH_VALUES_REQUIRED')
                    continue
                test.observe(case, item, exposed)
                if case in probes:
                    probes[case].annotate(index[case, item], lambda text: leaks(forms, [text], shape))
        crashes = [text for text in service.server_log if text.startswith('Traceback')]
        for item, forms, shape in (('password_in_crash', password_forms, False), ('hash_in_crash', hash_forms, True)):
            exposed = leaks(forms, [logs], shape) and leaks(forms, crashes, shape)
            if shape and not hash_forms and not exposed:
                test.detail_recorder.block('application_logs', [item], 'HASH_VALUES_REQUIRED')
            else:
                test.observe('application_logs', item, exposed, required=False)
    test.check(body)
