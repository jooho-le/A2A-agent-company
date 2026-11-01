# SCN-001 시범 최종 시험지(시범 실행·조정용 최종 평가). 결과를 보고 에이전트를 조정했다면 이 시험지는 더 이상
# 처음 보는 시험이 아니다. 공식 비교에는 exam_final을 쓴다. 순환용과 검사는 같고 입력·순서가 다르다.
import signup_link as link
import signup_qa_checks as qa
import signup_security_checks as sec

T = "first.{h}@mail.example.org"


class PilotExam(link.SignupCheck):
    def test_qa_signup_001_valid_signup_creates_user(self):
        qa.valid_signup(self, {"emails": (T, "{h}@example.us"), "length": 10})

    def test_qa_signup_002_invalid_email_is_rejected(self):
        qa.invalid_email(self, {"invalid": ("", "   ", "username.example.org", "@mail.example.org", "name@",
                                            "a@b@example.org", "user@exa mple.org")})

    def test_qa_signup_003_same_canonical_email_is_rejected(self):
        qa.duplicate_email(self, {"template": T, "forms": ("same", "title", "lead", "title_trail")})

    def test_qa_signup_004_password_minimum_length_is_eight(self):
        qa.password_length(self, {"reject": (7, 1), "accept": (8, 20), "kinds": (("digits", 10), ("lower", 8))})

    def test_qa_signup_005_concurrent_same_email_creates_one_user(self):
        qa.concurrent_signup(self, {"template": T, "workers": 5, "attempts": 3})

    def test_qa_signup_006_response_matches_actual_result(self):
        qa.response_matches_result(self, {"template": T, "steps": (
            ("invalid", "username.example.org"), ("new", "A"), ("short", "B"), ("duplicate", "A", "swap_pad"), ("new", "B"))})

    def test_sec_signup_002_password_stored_as_argon2id_hash(self):
        sec.stored_hash(self, {"template": T, "length": 10, "accounts": 2})

    def test_sec_signup_003_email_unique_and_canonical(self):
        sec.unique_and_canonical(self, {"template": T, "forms": ("title", "lead", "title_trail"), "aliases": (
            ("jane.q.doe{h}@gmail.com", "janeqdoe{h}@gmail.com"), ("team{h}+alerts@example.org", "team{h}@example.org"))})

    def test_sec_signup_005_password_and_hash_not_exposed(self):
        sec.not_exposed(self, {"template": T, "invalid": "username.example.org", "short": 5, "duplicate": "swap_pad",
                               "malformed": ("list", "misspelled", "missing_email")})
