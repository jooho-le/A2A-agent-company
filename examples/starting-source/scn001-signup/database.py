"""DB 연결(시작 코드 골격).

연결 규격:
- DB 위치는 환경 변수 DATABASE_URL로 받는다. 없으면 작업 폴더의 app.db를 쓴다(읽기 전용 소스 폴더에 고정하지 않는다).
- 요청 처리에서 DB는 `get_db` 의존성(동기 SQLAlchemy 세션)으로만 쓴다. 평가는 격리된 DATABASE_URL을 주고 원래 연결을 실행한다.
"""
import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./app.db")
engine = create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
)
SessionLocal = sessionmaker(bind=engine, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    with SessionLocal() as db:
        yield db
