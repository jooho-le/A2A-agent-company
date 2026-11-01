import asyncio
import hashlib
import json
import os
import secrets
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlencode, urlsplit
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
            unrelated = User(email="unrelated@example.com", password_hash=self.password_hash)
            db.add_all([submitter, assignee, unrelated])
            db.commit()
            self.submitter_id = submitter.id
            self.assignee_id = assignee.id
            self.unrelated_id = unrelated.id
        self.token = create_access_token(self.submitter_id, "submitter@example.com")
        self.assignee_token = create_access_token(self.assignee_id, "assignee@example.com")
        self.unrelated_token = create_access_token(self.unrelated_id, "unrelated@example.com")

    def tearDown(self):
        main.app.dependency_overrides.clear()
        self.storage_patch.stop()
        self.environment_patch.stop()
        self.engine.dispose()
        self.temp_directory.cleanup()

    def request(self, method, path, body=b"", headers=None, raw=False):
        """추가 테스트 패키지나 장시간 서버 실행 없이 실제 ASGI 앱을 호출합니다."""
        response_messages = []
        url = urlsplit(path)

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
                "method": method, "scheme": "http", "path": url.path,
                "raw_path": url.path.encode(), "query_string": url.query.encode(), "root_path": "",
                "headers": [
                    (name.lower().encode(), value.encode())
                    for name, value in (headers or {}).items()
                ],
                "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
            }
            await main.app(scope, receive, send)

        asyncio.run(make_request())
        response_start = next(
            message for message in response_messages
            if message["type"] == "http.response.start"
        )
        response_status = response_start["status"]
        response_body = b"".join(
            message.get("body", b"") for message in response_messages
            if message["type"] == "http.response.body"
        )
        if raw:
            response_headers = {
                name.decode("latin-1"): value.decode("latin-1")
                for name, value in response_start["headers"]
            }
            return response_status, response_headers, response_body
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

    def list_documents(self, view=None, token=None):
        path = "/api/documents"
        if view is not None:
            path += "?" + urlencode({"view": view})
        access_token = self.token if token is None else token
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        return self.request("GET", path, headers=headers)

    def download(self, document_id, token=None, query=None):
        access_token = self.token if token is None else token
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        path = f"/api/documents/{document_id}/download"
        if query:
            path += "?" + urlencode(query)
        return self.request("GET", path, headers=headers, raw=True)

    def uploaded_document(self, **kwargs):
        code, body = self.upload(**kwargs)
        self.assertEqual(code, 201)
        return body["id"]

    def stored_path(self, document_id):
        with Session(self.engine) as db:
            return Path(db.get(Document, document_id).file_path)

    def change_document(self, document_id, **changes):
        with Session(self.engine) as db:
            document = db.get(Document, document_id)
            for name, value in changes.items():
                setattr(document, name, value)
            db.commit()

    def assert_download_error(self, response, expected_status):
        code, headers, body = response
        self.assertEqual(code, expected_status)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(set(json.loads(body)), {"detail"})
        self.assertNotIn(str(self.temp_directory.name), body.decode())
        self.assertNotIn("Traceback", body.decode())
        self.assertNotIn("content-disposition", headers)

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

    def test_submitted_list_filters_current_user_and_defaults_to_submitted(self):
        code, own_document = self.upload()
        self.assertEqual(code, 201)
        self.assertEqual(self.upload(token=self.assignee_token, assignee_id=self.submitter_id)[0], 201)
        self.assertEqual(self.upload(token=self.unrelated_token)[0], 201)

        for view in (None, "submitted"):
            with self.subTest(view=view):
                code, body = self.list_documents(view)
                self.assertEqual(code, 200)
                self.assertEqual(body, {"documents": [own_document]})

    def test_assigned_list_filters_current_user(self):
        code, assigned_document = self.upload()
        self.assertEqual(code, 201)
        code, submitted_document = self.upload(token=self.assignee_token, assignee_id=self.unrelated_id)
        self.assertEqual(code, 201)

        code, body = self.list_documents("assigned", token=self.assignee_token)
        self.assertEqual(code, 200)
        self.assertEqual(body, {"documents": [assigned_document]})
        code, body = self.list_documents("submitted", token=self.assignee_token)
        self.assertEqual(code, 200)
        self.assertEqual(body, {"documents": [submitted_document]})

    def test_unrelated_user_cannot_list_documents_even_with_forged_user_ids(self):
        self.assertEqual(self.upload()[0], 201)
        self.assertEqual(self.upload(token=self.assignee_token, assignee_id=self.submitter_id)[0], 201)
        for view in ("submitted", "assigned"):
            with self.subTest(view=view):
                code, body = self.list_documents(view, token=self.unrelated_token)
                self.assertEqual((code, body), (200, {"documents": []}))

                # 다른 사람의 ID를 쿼리에 추가해도 조회 조건에는 사용되지 않습니다.
                query = urlencode({
                    "view": view, "submitter_id": self.submitter_id, "assignee_id": self.assignee_id,
                })
                code, body = self.request(
                    "GET", f"/api/documents?{query}",
                    headers={"Authorization": f"Bearer {self.unrelated_token}"},
                )
                self.assertEqual((code, body), (200, {"documents": []}))

    def test_document_list_requires_authentication(self):
        self.assertEqual(self.upload()[0], 201)
        for view in (None, "submitted", "assigned"):
            for token in ("", "invalid-token"):
                with self.subTest(view=view, token=token):
                    self.assertEqual(self.list_documents(view, token=token)[0], 401)

    def test_document_list_rejects_expired_tokens_and_absent_users(self):
        import os

        self.assertEqual(self.upload()[0], 201)
        expired = jwt.encode(
            {"sub": str(self.submitter_id), "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
            os.environ["JWT_SECRET"], algorithm="HS256",
        )
        absent = create_access_token(999999, "absent@example.com")
        for view in ("submitted", "assigned"):
            for token in (expired, absent):
                with self.subTest(view=view, token_type="expired" if token == expired else "absent"):
                    self.assertEqual(self.list_documents(view, token=token)[0], 401)

    def test_document_list_returns_empty_list(self):
        for view in (None, "submitted", "assigned"):
            with self.subTest(view=view):
                self.assertEqual(self.list_documents(view), (200, {"documents": []}))

    def test_document_list_orders_by_created_at_then_id_desc(self):
        uploaded = []
        for _ in range(3):
            code, document = self.upload()
            self.assertEqual(code, 201)
            uploaded.append(document)

        # ID 순서와 시간 순서가 다른 문서, 생성 시각이 같은 문서를 함께 검사합니다.
        with Session(self.engine) as db:
            for document, created_at in zip(uploaded, [
                datetime(2026, 10, 14, 10, 0),
                datetime(2026, 10, 13, 10, 0),
                datetime(2026, 10, 14, 10, 0),
            ]):
                db.get(Document, document["id"]).created_at = created_at
            db.commit()

        expected_ids = [uploaded[2]["id"], uploaded[0]["id"], uploaded[1]["id"]]
        for view, token in (("submitted", self.token), ("assigned", self.assignee_token)):
            with self.subTest(view=view):
                code, body = self.list_documents(view, token=token)
                self.assertEqual(code, 200)
                self.assertEqual([document["id"] for document in body["documents"]], expected_ids)

    def test_document_list_excludes_internal_storage_fields(self):
        code, uploaded = self.upload()
        self.assertEqual(code, 201)
        for view, token in (("submitted", self.token), ("assigned", self.assignee_token)):
            with self.subTest(view=view):
                code, body = self.list_documents(view, token=token)
                self.assertEqual(code, 200)
                self.assertEqual(set(body), {"documents"})
                self.assertEqual(len(body["documents"]), 1)
                document = body["documents"][0]
                self.assertEqual(set(document), {
                    "id", "original_filename", "file_size", "content_type", "submitter_id", "assignee_id", "created_at",
                })
                self.assertEqual(document, uploaded)

    def test_document_list_rejects_invalid_view(self):
        for view in ("all", "", "SUBMITTED"):
            with self.subTest(view=view):
                self.assertEqual(self.list_documents(view)[0], 422)

    def test_submitter_downloads_exact_original_bytes(self):
        # NUL 및 UTF-8이 아닌 바이트도 응답에서 변형되지 않아야 합니다.
        original = self.pdf + bytes(range(256))
        document_id = self.uploaded_document(contents=original)
        code, headers, body = self.download(document_id)
        self.assertEqual(code, 200)
        self.assertEqual(headers["content-type"], "application/pdf")
        self.assertEqual(int(headers["content-length"]), len(original))
        self.assertEqual(body, original)
        self.assertEqual(hashlib.sha256(body).digest(), hashlib.sha256(original).digest())

    def test_assignee_downloads_exact_original_bytes(self):
        document_id = self.uploaded_document()
        code, _, body = self.download(document_id, token=self.assignee_token)
        self.assertEqual(code, 200)
        self.assertEqual(body, self.pdf)

    def test_unrelated_user_cannot_download_or_forge_user_ids(self):
        document_id = self.uploaded_document()
        self.assert_download_error(self.download(document_id, token=self.unrelated_token), 403)
        self.assert_download_error(self.download(
            document_id, token=self.unrelated_token,
            query={"submitter_id": self.submitter_id, "assignee_id": self.assignee_id},
        ), 403)
        # 권한 검사 전에 파일에 접근하지 않아 파일 유무에 따라 응답이 달라지지 않습니다.
        self.stored_path(document_id).unlink()
        self.assert_download_error(self.download(document_id, token=self.unrelated_token), 403)

    def test_download_requires_authentication(self):
        document_id = self.uploaded_document()
        for token in ("", "invalid-token"):
            with self.subTest(token=token):
                self.assert_download_error(self.download(document_id, token=token), 401)
                self.assert_download_error(self.download(999999, token=token), 401)

    def test_download_rejects_expired_token_and_absent_user(self):
        document_id = self.uploaded_document()
        expired = jwt.encode(
            {"sub": str(self.submitter_id), "exp": datetime.now(timezone.utc) - timedelta(minutes=1)},
            os.environ["JWT_SECRET"], algorithm="HS256",
        )
        absent = create_access_token(999999, "absent@example.com")
        for token in (expired, absent):
            self.assert_download_error(self.download(document_id, token=token), 401)

    def test_download_nonexistent_document(self):
        self.assert_download_error(self.download(999999), 404)

    def test_download_when_submitter_and_assignee_are_same_user(self):
        document_id = self.uploaded_document(assignee_id=self.submitter_id)
        code, _, body = self.download(document_id)
        self.assertEqual(code, 200)
        self.assertEqual(body, self.pdf)

    def test_download_korean_original_filename(self):
        filename = "증빙 자료.pdf"
        document_id = self.uploaded_document(filename=filename)
        code, headers, body = self.download(document_id)
        self.assertEqual(code, 200)
        disposition = headers["content-disposition"]
        self.assertTrue(disposition.startswith('attachment; filename="document.pdf"; filename*=UTF-8\'\''))
        self.assertEqual(unquote(disposition.split("UTF-8''", 1)[1]), filename)
        self.assertTrue(disposition.isascii())
        self.assertEqual(body, self.pdf)

    def test_download_sanitizes_filename_paths_and_header_injection(self):
        document_id = self.uploaded_document()
        for original, expected in (
            ("../../증빙.pdf", "증빙.pdf"),
            ("C:\\private\\증빙.pdf", "증빙.pdf"),
            ('증빙"\r\nX-Injected: yes\x00.pdf', '증빙"X-Injected: yes.pdf'),
            ("..", "document.pdf"),
        ):
            with self.subTest(filename=original):
                self.change_document(document_id, original_filename=original)
                code, headers, body = self.download(document_id)
                self.assertEqual(code, 200)
                self.assertEqual(body, self.pdf)
                disposition = headers["content-disposition"]
                self.assertEqual(unquote(disposition.split("UTF-8''", 1)[1]), expected)
                self.assertNotIn("\r", disposition)
                self.assertNotIn("\n", disposition)
                self.assertNotIn("x-injected", headers)

    def test_download_ignores_client_supplied_paths(self):
        document_id = self.uploaded_document()
        outside = Path(self.temp_directory.name) / "private.pdf"
        outside.write_bytes(b"private file outside uploads")
        code, _, body = self.download(document_id, query={
            "file_path": str(outside), "stored_filename": "../private.pdf",
        })
        self.assertEqual(code, 200)
        self.assertEqual(body, self.pdf)

    def test_download_rejects_tampered_metadata_paths(self):
        document_id = self.uploaded_document()
        original_path = self.stored_path(document_id)
        outside = Path(self.temp_directory.name) / original_path.name
        outside.write_bytes(b"private file outside uploads")
        for path in (
            str(outside),
            str(self.storage / ".." / outside.name),
            f"uploads/{original_path.name}",
            str(self.storage / f"{uuid4()}.pdf"),
            str(original_path) + "\x00",
        ):
            with self.subTest(path=path):
                self.change_document(document_id, file_path=path)
                self.assert_download_error(self.download(document_id), 404)
        self.assertEqual(outside.read_bytes(), b"private file outside uploads")

    def test_download_rejects_tampered_stored_filenames(self):
        document_id = self.uploaded_document()
        for filename in ("../private.pdf", "..\\private.pdf", "/private.pdf", "notes.pdf", f"{uuid4()}.pdf/../private.pdf"):
            with self.subTest(filename=filename):
                self.change_document(
                    document_id, stored_filename=filename, file_path=str(self.storage / filename),
                )
                self.assert_download_error(self.download(document_id), 404)

    def test_download_rejects_symlink_to_external_file(self):
        document_id = self.uploaded_document()
        path = self.stored_path(document_id)
        outside = Path(self.temp_directory.name) / "private.pdf"
        outside.write_bytes(b"private file outside uploads")
        path.unlink()
        path.symlink_to(outside)
        self.assert_download_error(self.download(document_id), 404)
        self.assertEqual(outside.read_bytes(), b"private file outside uploads")

    def test_download_rejects_symlink_to_another_uploaded_file(self):
        document_id = self.uploaded_document()
        another_id = self.uploaded_document()
        path = self.stored_path(document_id)
        path.unlink()
        path.symlink_to(self.stored_path(another_id))
        self.assert_download_error(self.download(document_id), 404)

    def test_download_rejects_symlinked_upload_directory(self):
        document_id = self.uploaded_document()
        moved_storage = Path(self.temp_directory.name) / "outside-uploads"
        self.storage.rename(moved_storage)
        self.storage.symlink_to(moved_storage, target_is_directory=True)
        self.assert_download_error(self.download(document_id), 404)

    def test_download_rejects_hard_link_to_external_file(self):
        document_id = self.uploaded_document()
        path = self.stored_path(document_id)
        outside = Path(self.temp_directory.name) / "private.pdf"
        outside.write_bytes(self.pdf)
        path.unlink()
        os.link(outside, path)
        self.assert_download_error(self.download(document_id), 404)
        self.assertEqual(outside.read_bytes(), self.pdf)

    def test_download_handles_missing_file_without_internal_path(self):
        document_id = self.uploaded_document()
        self.stored_path(document_id).unlink()
        result = self.download(document_id)
        self.assert_download_error(result, 404)
        self.assertEqual(json.loads(result[2])["detail"], "문서 파일을 찾을 수 없습니다.")

    def test_download_handles_missing_upload_directory(self):
        document_id = self.uploaded_document()
        self.stored_path(document_id).unlink()
        self.storage.rmdir()
        self.assert_download_error(self.download(document_id), 404)

    def test_download_rejects_non_regular_files(self):
        document_id = self.uploaded_document()
        path = self.stored_path(document_id)
        path.unlink()
        path.mkdir()
        self.assert_download_error(self.download(document_id), 404)
        path.rmdir()
        os.mkfifo(path)
        self.assert_download_error(self.download(document_id), 404)

    def test_download_uses_open_file_even_if_path_is_replaced(self):
        document_id = self.uploaded_document()
        path = self.stored_path(document_id)
        outside = Path(self.temp_directory.name) / "private.pdf"
        outside.write_bytes(b"private file outside uploads")
        real_open = os.open

        def replace_after_open(name, flags, **kwargs):
            descriptor = real_open(name, flags, **kwargs)
            if name == path.name and kwargs.get("dir_fd") is not None:
                path.unlink()
                path.symlink_to(outside)
            return descriptor

        with patch.object(documents.os, "open", side_effect=replace_after_open):
            code, _, body = self.download(document_id)
        self.assertEqual(code, 200)
        self.assertEqual(body, self.pdf)

    def test_download_file_access_error_does_not_expose_exception(self):
        document_id = self.uploaded_document()
        with patch.object(documents.os, "open", side_effect=PermissionError("private server exception")):
            result = self.download(document_id)
        self.assert_download_error(result, 404)
        self.assertNotIn(b"private server exception", result[2])

    def test_download_exactly_ten_mib(self):
        original = self.pdf + b" " * (documents.MAX_FILE_SIZE - len(self.pdf))
        document_id = self.uploaded_document(contents=original)
        code, headers, body = self.download(document_id)
        self.assertEqual(code, 200)
        self.assertEqual(len(body), 10485760)
        self.assertEqual(int(headers["content-length"]), 10485760)
        self.assertEqual(body, original)

    def test_download_rejects_file_size_mismatch(self):
        document_id = self.uploaded_document()
        self.stored_path(document_id).write_bytes(b"incomplete file")
        self.assert_download_error(self.download(document_id), 404)

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
