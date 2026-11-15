# SCN-001 회원가입 Security 검사 내용(입력은 시험지 파일이 정한다). QA 검사와 같은 방식(실행 테스트)이다.
# 해시 저장값(REQ-005), DB UNIQUE·이메일 정규화 저장(REQ-003), 비밀번호·해시 노출(REQ-006).
import base64
import binascii
import itertools
import re

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
                stored[index] = service.stored_hash(email)
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
                test.observe(case, 'canonical', lambda: service.stored_email(email) == link.canonical(email))
                test.observe(case, 'unique', lambda: service.db_rejects_duplicate(email))
        for index, pair in enumerate(data["aliases"]):
            with test.case(alias=index):
                addresses = link.new_emails(*pair)
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


def not_exposed(test, data):
    test.prepare("SEC-SIGNUP-005", data)
    # SEC-SIGNUP-005: 성공·형식 오류·짧은 비밀번호·중복·형식이 틀린 본문·DB 오류 요청의 응답(본문·헤더)과
    # 앱 시작부터 종료까지의 서버 로그·출력에서 비밀번호와 해시를 찾는다(그대로·base64·16진수). 거부된 요청이나 DB 오류 때 계산한
    # 해시도 있으므로 해시 모양 자체도 찾는다(예: 처리하지 못한 DB 오류의 traceback에 SQL 값으로 찍힌 해시). 그런 해시를
    # base64·16진수로 바꿔 남긴 경우도 후보를 풀어서 찾는다.
    def body(service):
        email, used = link.new_email(data["template"]), []
        captured = {}

        def attempt(case, address, password):
            # 가입 요청을 보내고, 쓴 비밀번호를 찾을 값 목록에 넣는다.
            used.append(password)
            before = len(service.responses)
            result = service.signup(address, password)
            captured[case] = service.responses[before:]
            return result

        if not test.observe('precondition', 'http_status', lambda: attempt('success', email, link.new_password()), required=False):
            raise link.Unverified("PRECONDITION_SIGNUP_FAILED")
        stored = service.stored_hash(email)
        attempt("invalid_email", data["invalid"], link.new_password())
        attempt("short_password", link.new_email(), link.new_password(data["short"]))
        attempt("duplicate", link.variant(email, data["duplicate"]), link.new_password())
        for kind in data["malformed"]:  # 형식이 틀린 본문 세 종류(필드 누락·목록 본문·필드 이름 오타)
            password = link.new_password()
            used.append(password)
            before = len(service.responses)
            service.send(MALFORMED[kind](link.new_email(), password))
            captured["malformed_" + kind] = service.responses[before:]
        service.fail_next_insert()
        attempt("db_error", link.new_email(), link.new_password())
        test.observe("db_error", "injected", lambda: (service.require_insert_failure(), True)[1])
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
        captured['application_logs'] = [logs]
        for case, texts in captured.items():
            if not texts:
                for item in ('password_exposed', 'hash_exposed'):
                    test.detail_recorder.index[(case, item)].update(status='UNVERIFIED', reason='RESPONSE_NOT_CAPTURED')
                continue
            test.observe(case, 'password_exposed', any(value in text for value in password_forms for text in texts))
            test.observe(case, 'hash_exposed', any(value in text for value in hash_forms for text in texts)
                         or any(_hash_shown(text) for text in texts))
    test.check(body)
