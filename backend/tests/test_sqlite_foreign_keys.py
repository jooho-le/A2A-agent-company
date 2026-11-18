"""제품 연결 이벤트와 실제 모델을 임시 SQLite에만 적용합니다."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

from sqlalchemy import create_engine, delete, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
from models import Document, Project, ProjectRun, RunSubmission, User
from project_schema import create_project_tables


class SQLiteForeignKeyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.engine = self.make_engine("models.db")
        database.Base.metadata.create_all(self.engine, tables=[User.__table__, Document.__table__])
        create_project_tables(self.engine)

    def make_engine(self, filename, **connect_args):
        engine = create_engine(f"sqlite:///{self.directory / filename}", connect_args=connect_args)
        event.listen(engine, "connect", database.enable_sqlite_foreign_keys)
        self.addCleanup(engine.dispose)
        return engine

    def test_each_new_connection_enforces_fk_and_restores_transaction_mode(self):
        self.assertTrue(event.contains(database.engine, "connect", database.enable_sqlite_foreign_keys))
        modes = ([{"autocommit": False}, {"autocommit": sqlite3.LEGACY_TRANSACTION_CONTROL}]
                 if hasattr(sqlite3.Connection, "autocommit") else [{"isolation_level": "DEFERRED"}])
        for index, options in enumerate(modes):
            engine = self.make_engine(f"connections-{index}.db", **options)
            setting, original = next(iter(options.items()))
            # 동시에 체크아웃하여 서로 다른 물리 연결의 적용 여부를 확인합니다.
            with engine.connect() as first, engine.connect() as second:
                for connection in (first, second):
                    self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one(), 1)
                    self.assertEqual(getattr(connection.connection.driver_connection, setting), original)
            engine.dispose()
            with engine.connect() as replacement:
                self.assertEqual(replacement.exec_driver_sql("PRAGMA foreign_keys").scalar_one(), 1)
        non_sqlite = Mock()
        database.enable_sqlite_foreign_keys(non_sqlite, None)
        non_sqlite.cursor.assert_not_called()

    def test_missing_parent_insert_is_rejected(self):
        missing = str(uuid4())
        with Session(self.engine) as db:
            db.add(User(id=1, email="owner@example.test", password_hash="test-only"))
            project = Project(owner_user_id=1, name="Mock", scenario_id=str(uuid4()))
            db.flush()
            db.add(project)
            db.commit()
            project_id = project.project_id
        rows = [
            Project(owner_user_id=999, name="Orphan", scenario_id=str(uuid4())),
            ProjectRun(project_id=missing, run_id=str(uuid4()), configuration_id="mock"),
            self.submission(missing, owner=1),
            self.submission(project_id, owner=999),
        ]
        for row in rows:
            with self.subTest(model=type(row).__name__), Session(self.engine) as db:
                db.add(row)
                with self.assertRaisesRegex(IntegrityError, "FOREIGN KEY constraint failed"):
                    db.commit()
                db.rollback()

    @staticmethod
    def submission(project_id, owner=1):
        return RunSubmission(owner_user_id=owner, project_id=project_id, client_key=str(uuid4()),
                             request_sha256="a" * 64, host_request_sha256="b" * 64,
                             host_request_json="{}", configuration_id="c" * 64)

    def test_restrict_preserves_parent_and_children(self):
        with Session(self.engine) as db:
            db.add(User(id=1, email="owner@example.test", password_hash="test-only"))
            db.commit()
            for child_type in (ProjectRun, RunSubmission):
                project = Project(owner_user_id=1, name="Mock", scenario_id=str(uuid4()))
                db.add(project)
                db.flush()
                child = (ProjectRun(project_id=project.project_id, run_id=str(uuid4()), configuration_id="mock")
                         if child_type is ProjectRun else self.submission(project.project_id))
                db.add(child)
                db.commit()
                project_id = project.project_id
                child_key = child.id if child_type is ProjectRun else child.internal_key
                for statement in (delete(User).where(User.id == 1),
                                  delete(Project).where(Project.project_id == project_id)):
                    with self.assertRaisesRegex(IntegrityError, "FOREIGN KEY constraint failed"):
                        db.execute(statement)
                        db.commit()
                    db.rollback()
                    self.assertIsNotNone(db.get(User, 1))
                    self.assertIsNotNone(db.get(Project, project_id))
                    self.assertIsNotNone(db.get(child_type, child_key))


if __name__ == "__main__":
    unittest.main()
