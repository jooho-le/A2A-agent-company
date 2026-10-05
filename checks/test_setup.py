"""환경 확인용 테스트입니다. 서비스 기능 검증은 아직 포함하지 않습니다."""
import sys
import httpx


def test_python_environment():
    assert sys.version_info >= (3, 10)
    assert sys.prefix != sys.base_prefix, "프로젝트 가상환경을 선택하세요."


def test_http_client_setup():
    request = httpx.Request("GET", "http://localhost:8000/")
    assert request.method == "GET"
    assert request.url.host == "localhost"
