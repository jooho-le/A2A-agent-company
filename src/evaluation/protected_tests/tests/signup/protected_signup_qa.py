# SCN-001 회원가입 QA 보호 테스트. 개발 순환 안(PROTECTED)과 최종 평가에서 함께 쓴다.
# 검사: QA-SIGNUP-001·002·003·004·006 (docs/evaluation/scn001_criteria.md 2절).
# 잘못된 입력은 4xx로 거부해야 하고, 5xx는 처리하지 못한 서버 오류로 본다.
import signup_link as link

# 어떤 이메일 파서에서도 형식 오류인 값만 쓴다(파서 종류는 아직 미정).
INVALID_EMAILS = ("", "plainaddress", "user@", "@example.com", "user@@example.com",
                  "user name@example.com", "user@exa mple.com")


class SignupQAChecks(link.SignupCheck):
    # 001: 유효한 가입 → 성공 응답, 사용자 1명 증가
    def test_qa_signup_001_valid_signup_creates_one_user(self):
        def body(service):
            email, before = link.new_email(), service.count()
            status = service.signup(email, link.new_password())
            self.assertIn(status, link.SUCCESS, "유효한 가입 요청이 성공 응답을 받지 못함")
            self.assertEqual(service.count(), before + 1, "사용자 수가 1 늘지 않음")
            self.assertEqual(service.count_canonical(email), 1, "가입한 이메일의 사용자가 1명이 아님")
        self.check(body)

    # 002: 형식 오류 이메일 → 4xx 거부, 사용자 생성 없음
    def test_qa_signup_002_invalid_email_is_rejected(self):
        def body(service):
            # 정상 가입이 먼저 되어야 한다. 모든 요청을 거부하는 서비스에서 거부 검사가 거저 통과하지 않게 한다.
            if service.signup(link.new_email(), link.new_password()) not in link.SUCCESS:
                raise link.Unverified("PRECONDITION_SIGNUP_FAILED")
            for index, email in enumerate(INVALID_EMAILS):
                with self.case(email_case=index):
                    before = service.count()
                    status = service.signup(email, link.new_password())
                    self.assertIn(status, link.REJECTED, "형식 오류 이메일이 4xx로 거부되지 않음")
                    self.assertEqual(service.count(), before, "형식 오류 이메일로 사용자가 생성됨")
        self.check(body)

    # 003: 대소문자·공백만 다른 이메일 재가입 → 거부, 사용자 1명 유지
    def test_qa_signup_003_same_canonical_email_cannot_register_twice(self):
        def body(service):
            email = link.new_email()
            if service.signup(email, link.new_password()) not in link.SUCCESS:
                raise link.Unverified("PRECONDITION_SIGNUP_FAILED")
            local, domain = email.split("@")
            variants = (email, email.upper(), local.capitalize() + "@" + domain.upper(),
                        "  " + email + "  ", " " + email.upper() + " ")
            for index, variant in enumerate(variants):
                with self.case(variant=index):
                    status = service.signup(variant, link.new_password())
                    self.assertNotIn(status, link.SUCCESS, "같은 canonical 이메일 재가입이 성공 응답을 받음")
            self.assertEqual(service.count_canonical(email), 1, "같은 canonical 이메일의 사용자가 1명이 아님")
        self.check(body)

    # 004: 비밀번호 7자는 거부, 8자는 가입
    def test_qa_signup_004_password_minimum_length_is_eight(self):
        def body(service):
            with self.case(length=7):
                before = service.count()
                status = service.signup(link.new_email(), link.new_password(7))
                self.assertIn(status, link.REJECTED, "7자 비밀번호가 4xx로 거부되지 않음")
                self.assertEqual(service.count(), before, "7자 비밀번호로 사용자가 생성됨")
            with self.case(length=8):
                before = service.count()
                status = service.signup(link.new_email(), link.new_password(8))
                self.assertIn(status, link.SUCCESS, "8자 비밀번호가 정상 처리되지 않음")
                self.assertEqual(service.count(), before + 1, "8자 비밀번호로 사용자가 1명 생성되지 않음")
        self.check(body)

    # 006: 성공 응답이면 실제로 1명 생성, 실패 응답이면 생성 없음
    def test_qa_signup_006_response_matches_actual_result(self):
        def body(service):
            email = link.new_email()
            cases = (("new", email, link.new_password(), True),
                     ("invalid-email", "plainaddress", link.new_password(), False),
                     ("short-password", link.new_email(), link.new_password(7), False),
                     ("duplicate", email, link.new_password(), False))
            for name, address, password, should_create in cases:
                with self.case(request=name):
                    before = service.count()
                    status = service.signup(address, password)
                    created = service.count() - before
                    self.assertEqual(status in link.SUCCESS, created == 1, "응답과 실제 가입 결과가 다름")
                    if should_create:
                        self.assertIn(status, link.SUCCESS, "정상 가입이 성공 응답을 받지 못함")
                    else:
                        self.assertIn(status, link.REJECTED, "실패해야 할 요청이 4xx 실패 응답을 받지 못함")
                        self.assertEqual(created, 0, "실패해야 할 요청으로 사용자가 생성됨")
        self.check(body)
