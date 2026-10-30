# SCN-001 회원가입 Security 검사 내용(입력은 시험지 파일이 정한다). QA 검사와 같은 방식(실행 테스트)이다.
# 해시 저장값(REQ-005), DB UNIQUE·이메일 정규화 저장(REQ-003), 비밀번호·해시 노출(REQ-006).
import base64
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


def parse_argon2id(encoded):
    # Argon2id encoded hash("$"로 나뉜 6조각)를 읽어 ({m, t, p}, salt)를 돌려준다. 모양이 다르면 None.
    parts = encoded.split("$") if isinstance(encoded, str) else []
    if len(parts) != 6 or parts[0] or parts[1] != "argon2id" or not parts[2].startswith("v="):
        return None
    try:
        params = dict(item.split("=", 1) for item in parts[3].split(","))
        values = {name: int(params[name]) for name in ARGON2_MINIMUM}
    except (KeyError, ValueError):
        return None
    if not parts[4] or not parts[5]:
        return None
    return values, parts[4]


def stored_hash(test, data):
    # SEC-SIGNUP-002: 같은 비밀번호로 계정 여러 개를 만들고 저장값의 형식·설정값·salt·검증 결과를 본다.
    def body(service):
        try:
            from argon2 import PasswordHasher, exceptions
        except ImportError:
            raise link.Unverified("ARGON2_UNAVAILABLE") from None
        rejected = (exceptions.VerificationError, exceptions.InvalidHashError)
        password = link.new_password(data["length"])
        emails = [link.new_email(data["template"]) for _ in range(data["accounts"])]
        for email in emails:
            if service.signup(email, password) not in link.SUCCESS:
                raise link.Unverified("PRECONDITION_SIGNUP_FAILED")
        stored = [service.stored_hash(email) for email in emails]
        test.assertFalse(any(not isinstance(value, str) or password in value for value in stored),
                         "저장값이 글자가 아니거나 비밀번호 원문을 담고 있음")
        parsed = [parse_argon2id(value) for value in stored]
        test.assertTrue(all(parsed), "저장값이 Argon2id encoded hash 형식이 아님")
        for params, _salt in parsed:
            for name, minimum in ARGON2_MINIMUM.items():
                test.assertGreaterEqual(params[name], minimum, "Argon2 " + name + " 값이 기준 미만")
        test.assertEqual(len({salt for _params, salt in parsed}), len(parsed), "같은 비밀번호인데 계정의 salt가 같음")
        hasher = PasswordHasher()
        for value in stored:
            try:
                hasher.verify(value, password)
            except rejected:
                test.fail("가입한 비밀번호로 저장 해시 검증이 실패함")
            try:
                hasher.verify(value, link.new_password())
            except rejected:
                pass
            else:
                test.fail("다른 비밀번호로 저장 해시 검증이 성공함")
    test.check(body)


def unique_and_canonical(test, data):
    # SEC-SIGNUP-003: 대소문자·공백 변형으로 가입하면 정리된(canonical) 값으로 저장되고, 앱의 사전 검사를 거치지 않고
    # 같은 이메일 행을 DB에 직접 넣으면 DB가 이메일 칸 UNIQUE로 거부해야 한다.
    def body(service):
        if service.signup(link.new_email(), link.new_password()) not in link.SUCCESS:
            raise link.Unverified("PRECONDITION_SIGNUP_FAILED")
        for form in data["forms"]:
            with test.case(variant=form):
                email = link.new_email(data["template"])
                status = service.signup(link.variant(email, form), link.new_password())
                test.assertIn(status, link.SUCCESS, "대소문자·공백만 다른 유효한 이메일을 정리하지 않고 거부함")
                test.assertEqual(service.stored_email(email), link.canonical(email),
                                 "저장된 이메일이 앞뒤 공백 제거·소문자 형태가 아님")
                test.assertTrue(service.db_rejects_duplicate(email), "DB에 이메일 UNIQUE 제약이 없음")
    test.check(body)


def _forms(value):
    # 값 그대로와 흔히 바꿔 쓰는 형태(base64·URL용 base64·16진수)
    raw = value.encode("utf-8")
    return {value, base64.b64encode(raw).decode().rstrip("="), base64.urlsafe_b64encode(raw).decode().rstrip("="),
            raw.hex()}


def not_exposed(test, data):
    # SEC-SIGNUP-005: 성공·형식 오류·짧은 비밀번호·중복·형식이 틀린 본문·DB 오류 요청의 응답(본문·헤더)과
    # 서버 로그·출력에서 비밀번호와 해시를 찾는다(그대로·base64·16진수). 거부된 요청이나 DB 오류 때 계산한
    # 해시도 있으므로 해시 모양 자체도 찾는다(예: 처리하지 못한 DB 오류의 traceback에 SQL 값으로 찍힌 해시).
    def body(service):
        email, used = link.new_email(data["template"]), []

        def attempt(address, password):
            used.append(password)
            return service.signup(address, password)

        if attempt(email, link.new_password()) not in link.SUCCESS:
            raise link.Unverified("PRECONDITION_SIGNUP_FAILED")
        stored = service.stored_hash(email)
        attempt(data["invalid"], link.new_password())
        attempt(link.new_email(), link.new_password(data["short"]))
        attempt(link.variant(email, data["duplicate"]), link.new_password())
        password = link.new_password()
        used.append(password)
        service.send(MALFORMED[data["malformed"]](link.new_email(), password))
        service.fail_next_insert()
        attempt(link.new_email(), link.new_password())
        secrets = list(used)
        if isinstance(stored, str) and stored:
            secrets.append(stored)
            digest = stored.rsplit("$", 1)[-1]
            if len(digest) >= 16:
                secrets.append(digest)
        found = {form for value in secrets for form in _forms(value)}
        responses, logs = service.responses, service.log_text()
        test.assertFalse(any(value in text for value in found for text in responses)
                         or any(HASH_FORMAT.search(text) for text in responses),
                         "응답에 비밀번호 또는 비밀번호 해시가 들어 있음")
        test.assertFalse(any(value in logs for value in found) or bool(HASH_FORMAT.search(logs)),
                         "서버 로그·출력에 비밀번호 또는 비밀번호 해시가 들어 있음")
    test.check(body)
