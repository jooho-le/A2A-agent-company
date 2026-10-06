"""환경 확인용 테스트입니다. 서비스 기능 검증은 아직 포함하지 않습니다."""
import sys
import httpx


# 지원 파이썬 버전과 프로젝트 가상환경 사용 여부를 확인한다.
def test_python_environment():
    assert sys.version_info >= (3, 10)
    assert sys.prefix != sys.base_prefix, "프로젝트 가상환경을 선택하세요."


# 네트워크 요청 없이 HTTP 요청 객체가 올바르게 만들어지는지만 확인한다.
def test_http_client_setup():
    request = httpx.Request("GET", "http://localhost:8000/")
    assert request.method == "GET"
    assert request.url.host == "localhost"
