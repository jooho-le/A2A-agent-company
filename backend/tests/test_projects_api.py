"""Project API만 ASGI와 임시 SQLite에서 검사합니다. 실제 DB/Host는 사용하지 않습니다."""

import asyncio
import json
import os
import secrets
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import jwt
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import project_scenarios
from database import Base, get_db
from models import Document, Project, ProjectRun, User
from project_schema import create_project_tables
from security import create_access_token


class ProjectAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = create_engine(f"sqlite:///{Path(self.directory.name) / 'api.db'}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)

        @event.listens_for(self.engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        Base.metadata.create_all(self.engine, tables=[User.__table__, Document.__table__])
        with Session(self.engine) as db:
            db.add_all([User(id=n, email=f"user{n}@example.test", password_hash="test-only-password-hash") for n in (1, 2)])
            db.flush()
            db.add(Document(id=1, submitter_id=1, assignee_id=2, original_filename="existing.pdf",
                            stored_filename="existing-stored.pdf", file_path="test-only/path.pdf", file_size=100,
                            content_type="application/pdf"))
            db.commit()
        self.legacy_before = self.legacy_snapshot()
        create_project_tables(self.engine)
        self.secret = secrets.token_urlsafe(32)
        self.env = patch.dict(os.environ, {"JWT_SECRET": self.secret})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.tokens = {n: create_access_token(n, f"user{n}@example.test") for n in (1, 2)}
        self.scenario_id = str(project_scenarios._load_registry().SCN_001_ID)

        def temporary_db():
            with Session(self.engine) as db:
                yield db

        main.app.dependency_overrides[get_db] = temporary_db
        self.addCleanup(main.app.dependency_overrides.pop, get_db)

    def legacy_snapshot(self):
        with self.engine.connect() as db:
            return (
                db.exec_driver_sql("SELECT name, sql FROM sqlite_master WHERE tbl_name IN ('users','documents') ORDER BY name").all(),
                db.exec_driver_sql("SELECT * FROM users ORDER BY id").all(),
                db.exec_driver_sql("SELECT * FROM documents ORDER BY id").all(),
            )

    def request(self, method, path, payload=None, user=1, token=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        headers = [(b"content-type", b"application/json")]
        access_token = token if token is not None else self.tokens.get(user)
        if access_token:
            headers.append((b"authorization", f"Bearer {access_token}".encode()))
        url = urlsplit(path)
        messages = []

        async def invoke():
            delivered = False

            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Event().wait()

            async def send(message):
                messages.append(message)

            await main.app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                            "method": method, "scheme": "http", "path": url.path, "raw_path": url.path.encode(),
                            "query_string": url.query.encode(), "root_path": "", "headers": headers,
                            "client": ("127.0.0.1", 12345), "server": ("testserver", 80)}, receive, send)

        asyncio.run(invoke())
        status = next(message["status"] for message in messages if message["type"] == "http.response.start")
        content = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
        return status, json.loads(content)

    def create(self, name="회원가입 웹", user=1):
        status, body = self.request("POST", "/api/projects", {"name": name, "scenarioId": self.scenario_id}, user=user)
        self.assertEqual(status, 201, body)
        return body

    def test_authenticated_creation_public_contract_and_legacy_preservation(self):
        first = self.create("  회원가입 웹  ")
        second = self.create("두 번째 업무")
        self.assertEqual(first["name"], "회원가입 웹")
        self.assertEqual(UUID(first["projectId"]).version, 4)
        self.assertNotEqual(first["projectId"], second["projectId"])
        self.assertEqual(set(first), {"projectId", "name", "scenarioId", "createdAt", "updatedAt"})
        self.assertEqual(first["scenarioId"], self.scenario_id)
        self.assertTrue(first["createdAt"] and first["updatedAt"])
        with Session(self.engine) as db:
            project = db.get(Project, first["projectId"])
            self.assertEqual(project.owner_user_id, 1)
            self.assertEqual(db.scalars(select(ProjectRun)).all(), [])
        self.assertEqual(self.legacy_snapshot(), self.legacy_before)

    def test_owned_list_detail_and_other_user_not_found(self):
        first = self.create()
        self.create("다른 사용자 업무", user=2)
        status, body = self.request("GET", "/api/projects?owner_user_id=2")
        self.assertEqual(status, 200)
        self.assertEqual([item["projectId"] for item in body["projects"]], [first["projectId"]])
        path = f'/api/projects/{first["projectId"]}'
        status, detail = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(detail, {**first, "runs": []})
        for method, payload in [("GET", None), ("PATCH", {"name": "소유권 침범"})]:
            with self.subTest(method=method):
                denied = self.request(method, path, payload, user=2)
                missing = self.request(method, f"/api/projects/{uuid4()}", payload, user=2)
                self.assertEqual(denied, missing)
                self.assertEqual(denied[0], 404)
        self.assertEqual(self.request("GET", path)[1]["name"], first["name"])

    def test_patch_updates_only_name_and_timestamp(self):
        created = self.create()
        old = datetime(2000, 1, 1)
        with Session(self.engine) as db:
            project = db.get(Project, created["projectId"])
            project.updated_at = old
            db.commit()
        status, result = self.request("PATCH", f'/api/projects/{created["projectId"]}', {"name": "  변경한 프로젝트  "})
        self.assertEqual(status, 200)
        self.assertEqual(result["name"], "변경한 프로젝트")
        self.assertEqual(result["projectId"], created["projectId"])
        self.assertEqual(result["scenarioId"], created["scenarioId"])
        self.assertEqual(result["createdAt"], created["createdAt"])
        self.assertGreater(datetime.fromisoformat(result["updatedAt"]), old)
        with Session(self.engine) as db:
            self.assertEqual(db.get(Project, created["projectId"]).owner_user_id, 1)
        self.assertEqual(self.legacy_snapshot(), self.legacy_before)

    def test_inputs_cannot_override_owner_ids_or_change_scenario(self):
        valid = {"name": "등록", "scenarioId": self.scenario_id}
        for extra in [{"owner_user_id": 2}, {"ownerUserId": 2}, {"projectId": str(uuid4())}, {"configuration": {}}]:
            self.assertEqual(self.request("POST", "/api/projects", {**valid, **extra})[0], 422)
        for name in ["", "   ", None, "가" * 61]:
            self.assertEqual(self.request("POST", "/api/projects", {**valid, "name": name})[0], 422)
        created = self.create()
        for payload in [{}, {"name": " "}, {"name": None}, {"name": "수정", "scenarioId": str(uuid4())},
                        {"name": "수정", "owner_user_id": 2}, {"name": "수정", "runId": str(uuid4())}]:
            self.assertEqual(self.request("PATCH", f'/api/projects/{created["projectId"]}', payload)[0], 422)
        self.assertEqual(self.request("GET", "/api/projects/not-a-uuid")[0], 422)

    def test_existing_jwt_verification_is_required_for_all_routes(self):
        created = self.create()
        path = f'/api/projects/{created["projectId"]}'
        expired = jwt.encode({"sub": "1", "exp": datetime.now(timezone.utc) - timedelta(minutes=1)}, self.secret, algorithm="HS256")
        missing_user = create_access_token(999, "missing@example.test")
        for method, url, payload in [("POST", "/api/projects", {"name": "업무", "scenarioId": self.scenario_id}),
                                     ("GET", "/api/projects", None), ("GET", path, None), ("PATCH", path, {"name": "수정"})]:
            for token in ["", "invalid", expired, missing_user]:
                with self.subTest(method=method, url=url):
                    self.assertEqual(self.request(method, url, payload, user=None, token=token)[0], 401)

    def test_only_local_registered_mvp_is_accepted_and_unavailable_registry_blocks(self):
        for scenario_id in [str(uuid4()), "SCN-001", "not-a-uuid"]:
            self.assertEqual(self.request("POST", "/api/projects", {"name": "업무", "scenarioId": scenario_id})[0], 422)
        project_scenarios._load_registry.cache_clear()
        try:
            with patch.object(project_scenarios, "REGISTRY_PATH", Path(self.directory.name) / "missing.py"):
                status, body = self.request("POST", "/api/projects", {"name": "업무", "scenarioId": self.scenario_id})
                self.assertEqual(status, 503)
                self.assertNotIn(self.directory.name, json.dumps(body))
        finally:
            project_scenarios._load_registry.cache_clear()
        self.assertEqual(self.request("GET", "/api/projects"), (200, {"projects": []}))

    def test_run_history_returns_only_stored_public_links(self):
        first, second = self.create(), self.create("다른 업무", user=2)
        run_id = str(uuid4())
        with Session(self.engine) as db:
            db.add_all([ProjectRun(project_id=first["projectId"], run_id=run_id, configuration_id="test-config-ref"),
                        ProjectRun(project_id=second["projectId"], run_id=str(uuid4()), configuration_id="other-config-ref")])
            db.commit()
        status, body = self.request("GET", f'/api/projects/{first["projectId"]}')
        self.assertEqual(status, 200)
        self.assertEqual(len(body["runs"]), 1)
        connection = body["runs"][0]
        self.assertEqual(set(connection), {"hostKey", "runId", "configurationId", "linkedAt"})
        self.assertEqual(connection["hostKey"], "local-multi")
        self.assertEqual(connection["runId"], run_id)
        self.assertEqual(connection["configurationId"], "test-config-ref")
        self.assertNotIn("other-config-ref", json.dumps(body))
        self.assertEqual(self.request("GET", f'/api/projects/{first["projectId"]}', user=2)[0], 404)

    def test_unapplied_schema_returns_safe_error_without_creating_tables(self):
        # 이 테스트의 임시 DB에만 legacy-only 상태를 재현합니다.
        ProjectRun.__table__.drop(self.engine)
        Project.__table__.drop(self.engine)
        status, body = self.request("GET", "/api/projects")
        self.assertEqual((status, body), (503, {"detail": "프로젝트 저장소를 사용할 수 없습니다."}))
        with self.engine.connect() as db:
            self.assertEqual(set(db.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars()), {"users", "documents"})
        self.assertEqual(self.legacy_snapshot(), self.legacy_before)


if __name__ == "__main__":
    unittest.main()
