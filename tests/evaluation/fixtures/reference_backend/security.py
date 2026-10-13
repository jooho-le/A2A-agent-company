import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher


password_hasher = PasswordHash((Argon2Hasher(),))
# 없는 사용자도 해시 검증을 수행하여 응답 시간 차이를 줄입니다.
DUMMY_PASSWORD_HASH = password_hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return password_hasher.verify(password, password_hash)
    except UnknownHashError:
        return False


def create_access_token(user_id: int, email: str) -> str:
    secret = os.getenv("JWT_SECRET")
    if not secret or not secret.strip():
        raise RuntimeError("JWT_SECRET 환경변수가 설정되지 않았습니다.")

    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(minutes=30),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    secret = os.getenv("JWT_SECRET")
    if not secret or not secret.strip():
        raise RuntimeError("JWT_SECRET 환경변수가 설정되지 않았습니다.")

    return jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        options={"require": ["sub", "exp"], "verify_exp": True},
    )
