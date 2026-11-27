"""골격 자체 점검: 앱이 뜨고 준비 확인 경로가 응답한다. 요구사항 시험이 아니다."""
import os
import tempfile
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///" + os.path.join(tempfile.mkdtemp(), "health.db"))

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402


class HealthTests(unittest.TestCase):
    def test_root_answers(self):
        with TestClient(app) as client:
            self.assertEqual(client.get("/").status_code, 200)


if __name__ == "__main__":
    unittest.main()
