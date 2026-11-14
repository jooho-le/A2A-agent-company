"""프로젝트 모델만 임시 SQLite에서 검증합니다. 실제 app.db/Host/API는 사용하지 않습니다."""

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import create_engine, delete, event, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from database import Base
from models import Document, Project, ProjectRun, User
from project_schema import create_project_tables


class ProjectModelTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.engines = []
        self.engine = self.make_engine("projects.db")
        self.prepare_legacy(self.engine)

    def tearDown(self):
        for engine in self.engines:
            engine.dispose()
        self.directory.cleanup()

    def make_engine(self, filename, foreign_keys=True):
        engine = create_engine(f"sqlite:///{Path(self.directory.name) / filename}")
        if foreign_keys:
            @event.listens_for(engine, "connect")
            def enable_foreign_keys(connection, _record):
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        self.engines.append(engine)
        return engine

    def prepare_legacy(self, engine):
        Base.metadata.create_all(engine, tables=[User.__table__, Document.__table__])
        with Session(engine) as db:
            db.add(User(id=1, email="owner@example.test", password_hash="test-only-hash"))
            db.flush()
            db.add(Document(id=1, submitter_id=1, assignee_id=1, original_filename="existing.pdf",
                            stored_filename="existing-stored.pdf", file_path="test-only/existing.pdf",
                            file_size=100, content_type="application/pdf"))
            db.commit()

    def insert_project(self, owner=1, **values):
        with Session(self.engine) as db:
            project = Project(owner_user_id=owner, name="제품 프로젝트", scenario_id=str(uuid4()), **values)
            db.add(project)
            db.flush()
            project_id = project.project_id
            db.commit()
            return project_id

    def insert_run(self, project_id, **values):
        with Session(self.engine) as db:
            connection = ProjectRun(project_id=project_id, run_id=values.pop("run_id", str(uuid4())),
                                    configuration_id="test-configuration-reference", **values)
            db.add(connection)
            db.flush()
            result = (connection.id, connection.host_key, connection.run_id, connection.linked_at)
            db.commit()
            return result

    def test_additive_schema_preserves_existing_data_and_foreign_key_policy(self):
        # 기존 제품 DB처럼 외래키 강제가 꺼진 환경에서도 정책을 바꾸거나 테이블을 재작성하지 않습니다.
        engine = self.make_engine("legacy.db", foreign_keys=False)
        self.prepare_legacy(engine)
        with engine.connect() as db:
            schema = db.exec_driver_sql("SELECT type, name, sql FROM sqlite_master ORDER BY name").all()
            users = db.exec_driver_sql("SELECT * FROM users").all()
            documents = db.exec_driver_sql("SELECT * FROM documents").all()
            self.assertEqual(db.exec_driver_sql("PRAGMA foreign_keys").scalar(), 0)
        create_project_tables(engine)
        create_project_tables(engine)
        self.assertEqual(set(inspect(engine).get_table_names()), {"users", "documents", "projects", "project_runs"})
        with engine.connect() as db:
            after = db.exec_driver_sql("SELECT type, name, sql FROM sqlite_master ORDER BY name").all()
            self.assertTrue(all(row in after for row in schema))
            self.assertEqual(db.exec_driver_sql("SELECT * FROM users").all(), users)
            self.assertEqual(db.exec_driver_sql("SELECT * FROM documents").all(), documents)
            self.assertEqual(db.exec_driver_sql("PRAGMA foreign_keys").scalar(), 0)
        self.assertEqual(list(User.__table__.columns.keys()), ["id", "email", "password_hash", "role", "created_at"])
        self.assertEqual(list(Document.__table__.columns.keys()), ["id", "submitter_id", "assignee_id", "original_filename", "stored_filename", "file_path", "file_size", "content_type", "created_at"])

    def test_owner_has_multiple_projects_and_foreign_key_rejects_orphan(self):
        create_project_tables(self.engine)
        with Session(self.engine) as db:
            db.add(User(id=2, email="second@example.test", password_hash="test-only-hash"))
            db.commit()
        first = self.insert_project(owner=2)
        second = self.insert_project(owner=2)
        self.assertNotEqual(first, second)
        with self.assertRaises(IntegrityError):
            self.insert_project(owner=999)
        with Session(self.engine) as db:
            self.assertEqual(len(db.scalars(select(Project).where(Project.owner_user_id == 2)).all()), 2)
            with self.assertRaises(IntegrityError):
                db.execute(delete(User).where(User.id == 2))
            db.rollback()

    def test_project_has_multiple_runs_and_foreign_key_rejects_orphan(self):
        create_project_tables(self.engine)
        project_id = self.insert_project()
        first = self.insert_run(project_id)
        second = self.insert_run(project_id)
        self.assertNotEqual(first[0], second[0])
        self.assertEqual(first[1], "local-multi")
        self.assertIsNotNone(first[3])
        with self.assertRaises(IntegrityError):
            self.insert_run(str(uuid4()))
        with Session(self.engine) as db:
            with self.assertRaises(IntegrityError):
                db.execute(delete(Project).where(Project.project_id == project_id))
            db.rollback()
        create_project_tables(self.engine)
        with Session(self.engine) as db:
            self.assertEqual(len(db.scalars(select(ProjectRun)).all()), 2)

    def test_uuid_v4_primary_key_and_host_run_identity_are_unique(self):
        create_project_tables(self.engine)
        ids = [self.insert_project() for _ in range(3)]
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(all(UUID(value).version == 4 for value in ids))
        with self.assertRaises(IntegrityError):
            self.insert_project(project_id=ids[0])
        host_run = str(uuid4())
        self.insert_run(ids[0], run_id=host_run)
        with self.assertRaises(IntegrityError):
            self.insert_run(ids[0], run_id=host_run)
        with self.assertRaises(IntegrityError):
            self.insert_run(ids[1], run_id=host_run)
        # ID는 Host 범위에서 식별합니다. 이 값은 테스트 식별자이며 실행 설정이 아닙니다.
        self.insert_run(ids[1], run_id=host_run, host_key="test-other-host")

    def test_project_timestamps_use_existing_sqlalchemy_convention(self):
        create_project_tables(self.engine)
        old = datetime(2000, 1, 1)
        project_id = self.insert_project(created_at=old, updated_at=old)
        with Session(self.engine) as db:
            project = db.get(Project, project_id)
            project.name = "변경된 표시명"
            db.commit()
            self.assertEqual(project.created_at, old)
            self.assertGreater(project.updated_at, old)

    def test_server_startup_keeps_extension_explicit(self):
        async def start_backend():
            async with main.app.router.lifespan_context(main.app):
                pass
        with patch.object(main, "engine", self.engine):
            asyncio.run(start_backend())
        self.assertEqual(set(inspect(self.engine).get_table_names()), {"users", "documents"})
        empty = self.make_engine("empty.db")
        with self.assertRaisesRegex(ValueError, "User·Document"):
            create_project_tables(empty)
        self.assertEqual(inspect(empty).get_table_names(), [])


if __name__ == "__main__":
    unittest.main()
