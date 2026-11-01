# SCN-001 순환용 Security 시험지(Ver1의 개발 순환 안에서 Security가 실행한다). 최종 시험지와 검사는 같고 입력이 다르다.
import signup_link as link
import signup_security_checks as sec

T = "user.{h}@app.example.com"


class PracticeSecurityChecks(link.SignupCheck):
    def test_sec_signup_002_password_stored_as_argon2id_hash(self):
        sec.stored_hash(self, {"template": T, "length": 12, "accounts": 2})

    def test_sec_signup_003_email_unique_and_canonical(self):
        sec.unique_and_canonical(self, {"template": T, "forms": ("upper", "pad"), "aliases": (
            ("news{h}+promo@example.com", "news{h}@example.com"), ("first.last{h}@gmail.com", "firstlast{h}@gmail.com"))})

    def test_sec_signup_005_password_and_hash_not_exposed(self):
        sec.not_exposed(self, {"template": T, "invalid": "plainaddress", "short": 7, "duplicate": "upper_pad",
                               "malformed": ("missing_email", "list", "misspelled")})
