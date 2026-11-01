# SCN-001 순환용 QA 시험지(Ver1의 개발 순환 안에서 QA가 실행한다). 최종 시험지와 검사는 같고 입력·순서가 다르다.
import signup_link as link
import signup_qa_checks as qa

T = "user.{h}@app.example.com"


class PracticeQAChecks(link.SignupCheck):
    def test_qa_signup_001_valid_signup_creates_user(self):
        qa.valid_signup(self, {"emails": (T, "member{h}@example.io"), "length": 12})

    def test_qa_signup_002_invalid_email_is_rejected(self):
        qa.invalid_email(self, {"invalid": ("", "  ", "plainaddress", "@example.com", "user@", "user@@example.com",
                                            "user name@example.com")})

    def test_qa_signup_003_same_canonical_email_is_rejected(self):
        qa.duplicate_email(self, {"template": T, "forms": ("same", "upper", "pad", "upper_pad")})

    def test_qa_signup_004_password_minimum_length_is_eight(self):
        qa.password_length(self, {"reject": (7, 3), "accept": (8, 12), "kinds": (("lower", 9), ("digits", 8))})

    def test_qa_signup_005_concurrent_same_email_creates_one_user(self):
        qa.concurrent_signup(self, {"template": T, "workers": 5, "attempts": 3})

    def test_qa_signup_006_response_matches_actual_result(self):
        qa.response_matches_result(self, {"template": T, "steps": (
            ("short", "A"), ("new", "A"), ("invalid", "plainaddress"), ("duplicate", "A", "upper_pad"), ("new", "B"))})
