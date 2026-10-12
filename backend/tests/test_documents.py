import asyncio
import json
import secrets
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import jwt
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

# backend에서 사용하는 기존 절대 import 구조에 맞춥니다.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
from database import get_db
from models import Document, User
from routers import documents
from security import create_access_token, hash_password


def sample_pdf():
    """외부 PDF 라이브러리 없이 테스트용 빈 페이지 PDF를 만듭니다."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>",
    ]
    contents = b"%PDF-1.4\n"
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(contents))
        contents += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(contents)
    contents += b"xref\n0 4\n0000000000 65535 f \n"
    for offset in offsets[1:]:
        contents += f"{offset:010d} 00000 n \n".encode()
    contents += f"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode()
    return contents


class DocumentAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = hash_password("Password123")
        cls.pdf = sample_pdf()

    def setUp(self):
        self.temp_directory = tempfile.TemporaryDirectory()
        self.storage = Path(self.temp_directory.name) / "uploads"
        self.engine = create_engine(
            f"sqlite:///{Path(self.temp_directory.name) / 'test.db'}",
            connect_args={"check_same_thread": False},
        )
        self.environment_patch = patch.dict(
            "os.environ", {"JWT_SECRET": secrets.token_urlsafe(32)}
        )
        self.environment_patch.start()
        self.storage_patch = patch.object(documents, "UPLOAD_DIRECTORY", self.storage)
        self.storage_patch.start()

        async def start_backend():
            async with main.app.router.lifespan_context(main.app):
                pass

        # 서버 시작 시 테이블 생성도 실제 코드로 실행하되 운영 DB는 사용하지 않습니다.
        with patch.object(main, "engine", self.engine):
            asyncio.run(start_backend())

        def test_db():
            with Session(self.engine) as db:
                yield db

        main.app.dependency_overrides[get_db] = test_db
        with Session(self.engine) as db:
            submitter = User(email="submitter@example.com", password_hash=self.password_hash)
            assignee = User(email="assignee@example.com", password_hash=self.password_hash)
            db.add_all([submitter, assignee])
            db.commit()
            self.submitter_id = submitter.id
            self.assignee_id = assignee.id
        self.token = create_access_token(self.submitter_id, "submitter@example.com")

    def tearDown(self):
        main.app.dependency_overrides.clear()
        self.storage_patch.stop()
        self.environment_patch.stop()
        self.engine.dispose()
        self.temp_directory.cleanup()

    def request(self, method, path, body=b"", headers=None):
        """추가 테스트 패키지나 장시간 서버 실행 없이 실제 ASGI 앱을 호출합니다."""
        response_messages = []

        async def make_request():
            delivered = False

            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await asyncio.Event().wait()

            async def send(message):
                response_messages.append(message)

            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": method, "scheme": "http", "path": path,
                "raw_path": path.encode(), "query_string": b"", "root_path": "",
                "headers": [
                    (name.lower().encode(), value.encode())
                    for name, value in (headers or {}).items()
                ],
                "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
            }
            await main.app(scope, receive, send)

        asyncio.run(make_request())
        response_status = next(
            message["status"] for message in response_messages
            if message["type"] == "http.response.start"
        )
        response_body = b"".join(
            message.get("body", b"") for message in response_messages
            if message["type"] == "http.response.body"
        )
        return response_status, json.loads(response_body)

    def json_request(self, path, payload):
        return self.request(
            "POST", path, json.dumps(payload).encode(),
            {"Content-Type": "application/json"},
        )

    def upload(self, contents=None, assignee_id=None, token=None, filename="증빙자료.pdf",
               content_type="application/pdf", extra_files=None, extra_fields=None):
        boundary = f"test-{uuid4().hex}"
        fields = [("assignee_id", str(self.assignee_id if assignee_id is None else assignee_id))]
        fields.extend(extra_fields or [])
        files = [("file", filename, content_type, self.pdf if contents is None else contents)]
        files.extend(extra_files or [])
        body = b""
        for name, value in fields:
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, original_name, mime, content in files:
            body += (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{original_name}"\r\n'
                f'Content-Type: {mime}\r\n\r\n'
            ).encode() + content + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        access_token = self.token if token is None else token
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return self.request("POST", "/api/documents", body, headers)

    def document_count(self):
        with Session(self.engine) as db:
            return db.scalar(select(func.count()).select_from(Document))

    def assert_no_upload(self):
        self.assertEqual(self.document_count(), 0)
        self.assertEqual(list(self.storage.glob("*.pdf")), [])

    def test_startup_and_document_foreign_keys(self):
        code, body = self.request("GET", "/")
        self.assertEqual((code, body), (200, {"message": "A2A backend is running"}))
        inspector = inspect(self.engine)
        self.assertEqual(set(inspector.get_table_names()), {"users", "documents"})
        keys = inspector.get_foreign_keys("documents")
        self.assertEqual(
            {(key["constrained_columns"][0], key["referred_table"], key["referred_columns"][0]) for key in keys},
            {("submitter_id", "users", "id"), ("assignee_id", "users", "id")},
        )

    def test_authentication_required(self):
        self.assertEqual(self.upload(token="")[0], 401)
        self.assertEqual(self.upload(token="invalid-token")[0], 401)
        self.assert_no_upload()

    def test_expired_token_and_deleted_user_rejected(self):
        import os

        expired = jwt.encode(
            {"sub": str(self.submitter_id), "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
            os.environ["JWT_SECRET"], algorithm="HS256",
        )
        self.assertEqual(self.upload(token=expired)[0], 401)
        absent = create_access_token(999999, "absent@example.com")
        self.assertEqual(self.upload(token=absent)[0], 401)
        self.assert_no_upload()

    def test_success_stores_metadata_and_pdf_separately(self):
        login_code, login = self.json_request(
            "/api/auth/login", {"email": "submitter@example.com", "password": "Password123"}
        )
        self.assertEqual(login_code, 200)
        code, body = self.upload(token=login["access_token"])
        self.assertEqual(code, 201)
        self.assertEqual(set(body), {
            "id", "original_filename", "file_size", "content_type", "submitter_id", "assignee_id", "created_at",
        })
        self.assertEqual(body["original_filename"], "증빙자료.pdf")
        self.assertEqual(body["file_size"], len(self.pdf))
        self.assertEqual(body["content_type"], "application/pdf")
        self.assertEqual(body["submitter_id"], self.submitter_id)
        self.assertEqual(body["assignee_id"], self.assignee_id)
        self.assertIsNotNone(datetime.fromisoformat(body["created_at"]))
        with Session(self.engine) as db:
            document = db.get(Document, body["id"])
            self.assertEqual(Path(document.file_path).read_bytes(), self.pdf)
            self.assertEqual(Path(document.file_path).parent, self.storage)
            self.assertEqual(UUID(Path(document.stored_filename).stem).version, 4)
            self.assertTrue(all(not isinstance(getattr(document, column.name), bytes) for column in Document.__table__.columns))

    def test_exactly_ten_mib_allowed(self):
        contents = self.pdf + b" " * (documents.MAX_FILE_SIZE - len(self.pdf))
        code, body = self.upload(contents=contents)
        self.assertEqual(code, 201)
        self.assertEqual(body["file_size"], 10485760)
        self.assertEqual(next(self.storage.glob("*.pdf")).stat().st_size, 10485760)

    def test_more_than_ten_mib_rejected(self):
        contents = self.pdf + b" " * (documents.MAX_FILE_SIZE + 1 - len(self.pdf))
        self.assertEqual(self.upload(contents=contents)[0], 413)
        self.assert_no_upload()

    def test_non_pdf_and_empty_file_rejected_despite_name_and_mime(self):
        for contents in (b"not a PDF", b""):
            self.assertEqual(self.upload(contents=contents)[0], 415)
        self.assert_no_upload()

    def test_signature_is_used_instead_of_extension_or_client_mime(self):
        code, body = self.upload(filename="document.txt", content_type="text/plain")
        self.assertEqual(code, 201)
        self.assertEqual(body["content_type"], "application/pdf")

    def test_nonexistent_assignee_rejected(self):
        self.assertEqual(self.upload(assignee_id=999999)[0], 400)
        self.assert_no_upload()

    def test_invalid_assignee_id_rejected(self):
        for user_id in (0, -1, "invalid"):
            self.assertEqual(self.upload(assignee_id=user_id)[0], 422)
        self.assert_no_upload()

    def test_duplicate_and_additional_file_fields_rejected(self):
        for field in ("file", "another_file"):
            code, _ = self.upload(extra_files=[(field, "extra.pdf", "application/pdf", self.pdf)])
            self.assertEqual(code, 400)
        self.assert_no_upload()

    def test_client_cannot_choose_submitter(self):
        code, body = self.upload(extra_fields=[("submitter_id", str(self.assignee_id))])
        self.assertEqual(code, 201)
        self.assertEqual(body["submitter_id"], self.submitter_id)

    def test_same_original_name_does_not_overwrite(self):
        code1, body1 = self.upload()
        code2, body2 = self.upload()
        self.assertEqual((code1, code2), (201, 201))
        self.assertNotEqual(body1["id"], body2["id"])
        paths = list(self.storage.glob("*.pdf"))
        self.assertEqual(len(paths), 2)
        self.assertTrue(all(path.read_bytes() == self.pdf for path in paths))
        self.assertEqual(self.document_count(), 2)

    def test_storage_failure_leaves_no_record(self):
        self.storage.write_text("디렉터리를 만들 수 없는 상태")
        with self.assertLogs(documents.logger, level="ERROR"):
            self.assertEqual(self.upload()[0], 500)
        self.assertEqual(self.document_count(), 0)

    def test_partial_file_is_removed_on_write_failure(self):
        real_open = Path.open

        class BrokenOutput:
            def __init__(self, path, mode):
                self.output = real_open(path, mode)

            def __enter__(self):
                return self

            def write(self, contents):
                self.output.write(contents[:5])
                raise OSError("테스트용 파일 쓰기 실패")

            def __exit__(self, *args):
                self.output.close()

        with patch.object(Path, "open", autospec=True, side_effect=BrokenOutput):
            with self.assertLogs(documents.logger, level="ERROR"):
                self.assertEqual(self.upload()[0], 500)
        self.assert_no_upload()

    def test_database_failure_rolls_back_and_removes_file(self):
        with patch.object(Session, "commit", side_effect=SQLAlchemyError("테스트용 DB 저장 실패")):
            with self.assertLogs(documents.logger, level="ERROR"):
                self.assertEqual(self.upload()[0], 500)
        self.assert_no_upload()

    def test_uuid_collision_does_not_delete_or_overwrite_existing_file(self):
        self.storage.mkdir()
        existing_id = uuid4()
        existing_path = self.storage / f"{existing_id}.pdf"
        existing_path.write_bytes(b"existing PDF")
        with patch.object(documents, "uuid4", return_value=existing_id):
            with self.assertLogs(documents.logger, level="ERROR"):
                self.assertEqual(self.upload()[0], 500)
        self.assertEqual(existing_path.read_bytes(), b"existing PDF")
        self.assertEqual(self.document_count(), 0)

    def test_existing_signup_login_and_me_still_work(self):
        payload = {"email": "new@example.com", "password": "Password123"}
        self.assertEqual(self.json_request("/api/auth/signup", payload)[0], 201)
        self.assertEqual(self.json_request("/api/auth/signup", payload)[0], 409)
        self.assertEqual(self.json_request("/api/auth/signup", {
            "email": "invalid", "password": "Password123",
        })[0], 422)
        self.assertEqual(self.json_request("/api/auth/signup", {
            "email": "short@example.com", "password": "short",
        })[0], 422)
        code, body = self.json_request("/api/auth/login", payload)
        self.assertEqual(code, 200)
        self.assertEqual(body["token_type"], "bearer")
        me_code, me = self.request(
            "GET", "/api/auth/me", headers={"Authorization": f'Bearer {body["access_token"]}'},
        )
        self.assertEqual(me_code, 200)
        self.assertEqual(me["email"], payload["email"])
        self.assertNotIn("password_hash", me)
        self.assertEqual(self.json_request("/api/auth/login", {
            "email": payload["email"], "password": "wrong-password",
        })[0], 401)
        self.assertEqual(self.json_request("/api/auth/login", {
            "email": "missing@example.com", "password": "Password123",
        })[0], 401)


if __name__ == "__main__":
    unittest.main()
