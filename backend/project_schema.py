"""명시적으로 전달한 DB에 프로젝트 테이블만 추가하는 초기 스키마 확장입니다."""

from sqlalchemy import inspect

from database import Base
from models import Project, ProjectRun


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
            tables=[Project.__table__, ProjectRun.__table__],
            checkfirst=True,
        )
