import re

from pydantic import BaseModel, ConfigDict, field_validator


EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,63}"
)


class SignupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    password: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        if (
            len(value) > 254
            or len(value.split("@", 1)[0]) > 64
            or EMAIL_PATTERN.fullmatch(value) is None
        ):
            raise ValueError("올바른 이메일 형식이 아닙니다.")
        return value.lower()

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if len(value) < 8:
            raise ValueError("비밀번호는 최소 8자 이상이어야 합니다.")
        return value
