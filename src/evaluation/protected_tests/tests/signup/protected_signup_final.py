# SCN-001 회원가입 최종 평가 전용 보호 테스트(개발 순환 안에서는 쓰지 않는다).
# 검사: QA-SIGNUP-005(동시 가입), SEC-SIGNUP-002(해시 저장), SEC-SIGNUP-005(비밀번호·해시 노출).
import re

import signup_link as link

# 시나리오 정책 passwordHashPolicy 값. 비교 규칙은 "이상"(AT_LEAST, 2026-10-28 4번 결정).
ARGON2_MINIMUM = {"m": 19456, "t": 2, "p": 1}
# 실제 비밀번호 해시 값의 모양(Argon2·bcrypt·PBKDF2·scrypt). 응답·로그에 있으면 해시 노출이다.
# 형식 이름만 적힌 안내 글은 노출이 아니므로 salt·해시까지 갖춘 모양만 찾는다(D4 trace_checks와 같은 규칙).
HASH_FORMAT = re.compile(
    r"[$]argon2(id|i|d)[$](v=[0-9]+[$])?m=[0-9]+,t=[0-9]+,p=[0-9]+[$][A-Za-z0-9+/.=-]{8,}[$][A-Za-z0-9+/.=-]{16,}"
    r"|[$]2[abxy][$][0-9]{2}[$][./A-Za-z0-9]{53}"
    r"|[$]pbkdf2(-[a-z0-9]+)?[$][0-9]+[$][A-Za-z0-9+/.=-]{4,}[$][A-Za-z0-9+/.=-]{16,}"
    r"|[$]scrypt[$]ln=[0-9]+,r=[0-9]+,p=[0-9]+[$][A-Za-z0-9+/.=-]{4,}[$][A-Za-z0-9+/.=-]{16,}")


def parse_argon2id(encoded):
    # Argon2id encoded hash("$"로 나뉜 6조각: 빈칸, argon2id, v=버전, m·t·p 설정, salt, 해시)를 읽어
    # ({m, t, p}, salt)를 돌려준다. 모양이 다르면 None.
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


class SignupFinalChecks(link.SignupCheck):
    def test_qa_signup_005_concurrent_same_email_creates_one_user(self):
        # 동시 요청에서 중복 생성이 없는지 본다. 이번 시도에서 경쟁이 안 드러날 수도 있어 3번 반복한다.
        def body(service):
            for attempt in range(3):
                with self.case(attempt=attempt):
                    email = link.new_email()
                    statuses = service.concurrent_signups(email, link.new_password())
                    created = service.count_canonical(email)
                    successes = sum(1 for status in statuses if status in link.SUCCESS)
                    # 아무도 생성되지 않았을 때: 5xx 응답은 관찰한 서버 오류라 FAIL(아래 검사)이다. 모두 4xx면
                    # 단독 가입을 다시 해 보고, 단독 가입도 안 되면 요청 규격이 다른 것으로 보고 미검증이다.
                    if (created == 0 and successes == 0 and all(status in link.REJECTED for status in statuses)
                            and service.signup(link.new_email(), link.new_password()) not in link.SUCCESS):
                        raise link.Unverified("CONCURRENT_SIGNUP_NOT_CREATED")
                    self.assertEqual(created, 1, "동시 요청으로 같은 이메일 사용자가 1명이 아님")
                    self.assertEqual(successes, 1, "동시 요청 중 성공 응답이 정확히 1건이 아님")
                    # 나머지는 중복 실패이므로 4xx여야 한다(5xx는 처리하지 못한 서버 오류).
                    self.assertTrue(all(status in link.SUCCESS or status in link.REJECTED for status in statuses),
                                    "동시 요청의 중복 실패가 4xx로 거부되지 않음")
        self.check(body)

    def test_sec_signup_002_password_stored_as_argon2id_hash(self):
        # 같은 비밀번호로 두 계정을 만들고 저장값의 형식·설정값·salt·검증 결과를 본다.
        def body(service):
            try:
                from argon2 import PasswordHasher, exceptions
            except ImportError:
                raise link.Unverified("ARGON2_UNAVAILABLE") from None
            rejected = (exceptions.VerificationError, exceptions.InvalidHashError)
            password, emails = link.new_password(), (link.new_email(), link.new_email())
            for email in emails:
                if service.signup(email, password) not in link.SUCCESS:
                    raise link.Unverified("PRECONDITION_SIGNUP_FAILED")
            stored = [service.stored_hash(email) for email in emails]
            self.assertFalse(any(not isinstance(value, str) or password in value for value in stored),
                             "저장값이 글자가 아니거나 비밀번호 원문을 담고 있음")
            parsed = [parse_argon2id(value) for value in stored]
            self.assertTrue(all(parsed), "저장값이 Argon2id encoded hash 형식이 아님")
            for params, _salt in parsed:
                for name, minimum in ARGON2_MINIMUM.items():
                    self.assertGreaterEqual(params[name], minimum, "Argon2 " + name + " 값이 기준 미만")
            self.assertFalse(parsed[0][1] == parsed[1][1], "같은 비밀번호인데 두 계정의 salt가 같음")
            # 가입한 비밀번호로는 검증 성공, 다른 비밀번호로는 실패해야 한다.
            hasher = PasswordHasher()
            for value in stored:
                try:
                    hasher.verify(value, password)
                except rejected:
                    self.fail("가입한 비밀번호로 저장 해시 검증이 실패함")
                try:
                    hasher.verify(value, link.new_password())
                except rejected:
                    pass
                else:
                    self.fail("다른 비밀번호로 저장 해시 검증이 성공함")
        self.check(body)

    def test_sec_signup_005_password_and_hash_not_exposed(self):
        # 성공·형식 오류·짧은 비밀번호·중복 요청의 응답(본문·헤더)과 서버 로그·출력에서 찾는다.
        # 저장된 해시뿐 아니라 거부된 요청이 계산한 해시도 있으므로 비밀번호 해시 형식 자체도 찾는다
        # (예: 처리하지 못한 DB 오류의 traceback에 SQL 파라미터로 해시가 찍히는 경우).
        def body(service):
            email, used = link.new_email(), []

            def attempt(address, password):
                used.append(password)
                return service.signup(address, password)

            if attempt(email, link.new_password()) not in link.SUCCESS:
                raise link.Unverified("PRECONDITION_SIGNUP_FAILED")
            stored = service.stored_hash(email)
            attempt("plainaddress", link.new_password())
            attempt(link.new_email(), link.new_password(7))
            attempt(email, link.new_password())
            secrets_seen = list(used)
            if isinstance(stored, str) and stored:
                secrets_seen.append(stored)
                digest = stored.rsplit("$", 1)[-1]
                if len(digest) >= 16:
                    secrets_seen.append(digest)
            responses, logs = service.responses, service.log_text()
            self.assertFalse(any(value in text for value in secrets_seen for text in responses)
                             or any(bool(HASH_FORMAT.search(text)) for text in responses),
                             "응답에 비밀번호 또는 비밀번호 해시가 들어 있음")
            self.assertFalse(any(value in logs for value in secrets_seen) or bool(HASH_FORMAT.search(logs)),
                             "서버 로그·출력에 비밀번호 또는 비밀번호 해시가 들어 있음")
        self.check(body)
