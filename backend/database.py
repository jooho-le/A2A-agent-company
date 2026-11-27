from pathlib import Path
import sqlite3

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_PATH = Path(__file__).resolve().parent / "app.db"
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)


def enable_sqlite_foreign_keys(connection, _record):
    """새 SQLite 연결에서 트랜잭션 밖으로 PRAGMA를 적용하고 기존 모드를 복원합니다."""
    if not isinstance(connection, sqlite3.Connection):
        return
    # Python 3.12+의 autocommit=False는 연결 직후에도 트랜잭션을 엽니다.
    # 구버전 드라이버는 isolation_level=None으로 같은 설정 구간을 만듭니다.
    setting = "autocommit" if hasattr(connection, "autocommit") else "isolation_level"
    original = getattr(connection, setting)
    try:
        setattr(connection, setting, True if setting == "autocommit" else None)
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA foreign_keys")
            if cursor.fetchone() != (1,):
                raise RuntimeError("SQLite 외래키 제약을 활성화하지 못했습니다.")
        finally:
            cursor.close()
    finally:
        setattr(connection, setting, original)


# 제품 Engine에만 등록합니다. 다른 Engine·DB의 연결 정책을 전역 변경하지 않습니다.
if engine.dialect.name == "sqlite":
    event.listen(engine, "connect", enable_sqlite_foreign_keys)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


class Base(DeclarativeBase):
    pass


def get_db():
    with SessionLocal() as db:
        yield db
