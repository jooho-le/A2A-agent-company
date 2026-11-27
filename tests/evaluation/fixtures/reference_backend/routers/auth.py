import jwt
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from models import User
from schemas import CurrentUserResponse, LoginRequest, SignupRequest
from security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


router = APIRouter(prefix="/api/auth")
bearer_scheme = HTTPBearer(auto_error=False)


@router.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, db: Session = Depends(get_db)):
    email_query = select(User.id).where(func.lower(User.email) == payload.email)
    if db.scalar(email_query) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이미 등록된 이메일입니다.",
        )

    user = User(
        email=payload.email,
        password_hash=hash_password(payload.password),
        role="user",
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # 동시에 같은 이메일로 가입해도 DB의 unique 조건이 중복을 막습니다.
        if db.scalar(email_query) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="이미 등록된 이메일입니다.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="회원가입 처리 중 오류가 발생했습니다.",
        ) from exc

    return {"success": True, "message": "회원가입에 성공했습니다."}


@router.post("/login")
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.scalar(
        select(User).where(func.lower(User.email) == payload.email.lower())
    )
    stored_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    password_valid = verify_password(payload.password, stored_hash)
    if user is None or not password_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="이메일 또는 비밀번호가 올바르지 않습니다.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        access_token = create_access_token(user.id, user.email)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/me", response_model=CurrentUserResponse)
def read_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
):
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="유효한 인증 정보가 필요합니다.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None:
        raise unauthorized

    try:
        payload = decode_access_token(credentials.credentials)
    except (jwt.InvalidTokenError, ValueError, TypeError, OverflowError) as exc:
        raise unauthorized from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc

    try:
        user_id = int(payload["sub"])
        user = db.get(User, user_id)
    except (ValueError, TypeError, OverflowError) as exc:
        raise unauthorized from exc

    if user is None:
        raise unauthorized

    return user
