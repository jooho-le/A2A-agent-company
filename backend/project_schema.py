"""명시적인 제품 DB 초기화와 기존 접수 테이블의 데이터 보존 업그레이드."""

from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from sqlalchemy import inspect

from database import Base
from models import Project, ProjectRun, RetiredSubmissionKey, RunSubmission


def create_project_tables(bind):
    """기존 users/documents를 유지하고 없는 프로젝트 테이블만 생성합니다.

    실제 app.db에 자동 적용하지 않습니다. 호출자는 적용할 Engine을 명시해야 합니다.
    기존 테이블의 컬럼/제약 변경이나 데이터 마이그레이션은 수행하지 않습니다.
    SQLite foreign_keys 정책도 바꾸지 않으므로 활성화 여부는 별도로 관리해야 합니다.
    """
    with bind.begin() as connection:
        existing = set(inspect(connection).get_table_names())
        if not {"users", "documents"}.issubset(existing):
            raise ValueError("기존 User·Document 테이블을 먼저 준비해야 합니다.")
        Base.metadata.create_all(
            bind=connection,
            tables=[Project.__table__, ProjectRun.__table__, RunSubmission.__table__, RetiredSubmissionKey.__table__],
            checkfirst=True,
        )


def _backup_sqlite_database(bind, backup_directory):
    database = bind.url.database
    if not database or database == ":memory:" or bind.url.query.get("mode") == "memory":
        return None
    source = Path(database).resolve()
    directory = Path(backup_directory) if backup_directory is not None else (
        Path(__file__).resolve().parents[1] / ".data" / "backups" / "product-schema"
    )
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"app-before-product-upgrade-{stamp}-{uuid4().hex[:8]}.sqlite3"
    # SQLite backup API includes committed WAL contents; never copy an open DB file.
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(target)) as backup:
            os.chmod(target, 0o600)
            original.backup(backup)
            if backup.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("제품 DB 백업의 무결성을 확인하지 못했습니다.")
    return target


def upgrade_project_tables(bind, *, backup_directory=None):
    """기존 접수·재시도 기록을 유지하며 누락 컬럼·미접수 키 기록 테이블을 추가합니다.

    FastAPI lifespan에서는 호출하지 않습니다. 파일 SQLite는 변경 전에 비공개
    백업을 만들고, 백업이 실패하면 업그레이드도 중단합니다. 반복 호출은 무변경입니다.
    """
    column = "manual_retry_claimed_at"
    tables = set(inspect(bind).get_table_names())
    existing_receipts = "run_submissions" in tables
    needs_column = existing_receipts and column not in {
        item["name"] for item in inspect(bind).get_columns("run_submissions")
    }
    needs_key_table = existing_receipts and "retired_submission_keys" not in tables
    if (needs_column or needs_key_table) and bind.dialect.name != "sqlite":
        raise ValueError("기존 접수 테이블 업그레이드는 SQLite에서만 지원합니다.")
    backup = _backup_sqlite_database(bind, backup_directory) if needs_column or needs_key_table else None
    create_project_tables(bind)
    if column in {item["name"] for item in inspect(bind).get_columns("run_submissions")}:
        return backup
    with bind.begin() as connection:
        # Serialize schema changes and make the SQLite DDL explicitly transactional.
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        if column not in {item["name"] for item in inspect(connection).get_columns("run_submissions")}:
            connection.exec_driver_sql(
                "ALTER TABLE run_submissions ADD COLUMN manual_retry_claimed_at DATETIME"
            )
    return backup


if __name__ == "__main__":
    from database import engine
    from models import User, Document
    Base.metadata.create_all(bind=engine, tables=[User.__table__, Document.__table__])
    backup = upgrade_project_tables(engine)
    if backup is not None:
        print("Existing product DB backed up; RunSubmission schema upgraded.")
    print("Project, ProjectRun, RunSubmission schema ready.")
