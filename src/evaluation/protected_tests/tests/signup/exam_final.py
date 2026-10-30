# SCN-001 공식 최종 시험지(Ver1·Ver2 공식 비교 채점용). 개발 순환·시범 실행에 쓰지 않고, 결과를 보고 에이전트를
# 조정하지 않는다. 순환용과 검사는 같고 입력·순서가 다르다.
import signup_link as link
import signup_qa_checks as qa
import signup_security_checks as sec

T = "{h}.name@sub.example.net"


class FinalExam(link.SignupCheck):
    def test_qa_signup_001_valid_signup_creates_user(self):
        qa.valid_signup(self, {"emails": (T, "a{h}@example.co"), "length": 14})

    def test_qa_signup_002_invalid_email_is_rejected(self):
        qa.invalid_email(self, {"invalid": ("", " ", "firstlast", "@sub.example.net", "first.last@",
                                            "first@@sub.example.net", "first last@sub.example.net")})

    def test_qa_signup_003_same_canonical_email_is_rejected(self):
        qa.duplicate_email(self, {"template": T, "forms": ("same", "swapcase", "trail", "title_trail")})

    def test_qa_signup_004_password_minimum_length_is_eight(self):
        qa.password_length(self, {"reject": (7, 5), "accept": (8, 16)})

    def test_qa_signup_005_concurrent_same_email_creates_one_user(self):
        qa.concurrent_signup(self, {"template": T, "workers": 5, "attempts": 3})

    def test_qa_signup_006_response_matches_actual_result(self):
        qa.response_matches_result(self, {"template": T, "steps": (
            ("new", "A"), ("duplicate", "A", "title_trail"), ("short", "B"), ("new", "B"),
            ("invalid", "@sub.example.net"))})

    def test_sec_signup_002_password_stored_as_argon2id_hash(self):
        sec.stored_hash(self, {"template": T, "length": 16, "accounts": 2})

    def test_sec_signup_003_email_unique_and_canonical(self):
        sec.unique_and_canonical(self, {"template": T, "forms": ("swapcase", "trail")})

    def test_sec_signup_005_password_and_hash_not_exposed(self):
        sec.not_exposed(self, {"template": T, "invalid": "first last@sub.example.net", "short": 3,
                               "duplicate": "title_trail", "malformed": "misspelled"})
