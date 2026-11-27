"""Host 공개 응답 검증만 수행합니다. 설정 기본값이나 승인 식별자를 발급하지 않습니다."""

import importlib
import json
import re
import sys
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote

from host_reads import HostUnavailable


@lru_cache(maxsize=1)
def _host_contract():
    # 같은 체크아웃의 순수 도메인 스키마·마스킹 함수만 재사용합니다.
    # Host factory, API 앱, DB, Provider는 import/실행하지 않습니다.
    source = Path(__file__).resolve().parents[1] / "src"
    added = str(source) not in sys.path
    if added:
        sys.path.insert(0, str(source))
    try:
        schema = importlib.import_module("orchestrator.domain.run_configuration")
        security = importlib.import_module("orchestrator.core.security")
        for module in (schema, security):
            if not Path(module.__file__).resolve().is_relative_to(source):
                raise ValueError()
        return schema.RunConfiguration, security.contains_credential_reference
    finally:
        if added:
            sys.path.remove(str(source))


_SECRET_VALUE = re.compile(r"\bsk-[\w-]+|\bAKIA[0-9A-Z]{16}\b|-----BEGIN .*PRIVATE KEY", re.I)
_LOCAL_PATH = re.compile(r"(?:^|[\s=\"'])+(?:/|~[/\\]|\.{1,2}[/\\]|[A-Za-z]:[/\\]|\\\\)|file:", re.I)


def _check_values(value, contains_credentials):
    if isinstance(value, dict):
        for item in value.values():
            _check_values(item, contains_credentials)
    elif isinstance(value, list):
        for item in value:
            _check_values(item, contains_credentials)
    elif isinstance(value, str):
        decoded = value
        for _ in range(3):
            decoded = unquote(decoded)
        if (contains_credentials(value) or _SECRET_VALUE.search(decoded)
                or _LOCAL_PATH.search(decoded) or any(ord(char) < 32 for char in decoded)):
            raise ValueError()


def public_approved_configuration(result, scenario_id):
    """원본 전체 객체의 해시를 대조합니다. 마스킹/필드 삭제 후 반환하지 않습니다."""
    try:
        if not isinstance(result, dict) or set(result) != {"scenarioId", "configurationId", "configuration"}:
            raise ValueError()
        if result["scenarioId"] != str(scenario_id):
            raise ValueError()
        identifier = result["configurationId"]
        if not isinstance(identifier, str) or re.fullmatch(r"[0-9a-f]{64}", identifier) is None:
            raise ValueError()
        configuration = result["configuration"]
        raw = json.dumps(configuration, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if sha256(raw.encode("utf-8")).hexdigest() != identifier:
            raise ValueError()
        schema, contains_credentials = _host_contract()
        parsed = schema.model_validate_json(raw)
        public = parsed.model_dump(mode="json", by_alias=True)
        # Host는 null·기본값까지 반환합니다. 누락/타입 변환/추가 필드도 차단합니다.
        if parsed.architecture != "MULTI_AGENT" or json.dumps(
                public, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) != raw:
            raise ValueError()
        _check_values(configuration, contains_credentials)
        return {"scenarioId": result["scenarioId"], "configurationId": identifier, "configuration": configuration}
    except Exception:
        # Pydantic 입력값·내부 경로·원본 설정은 오류 응답/로그에 포함하지 않습니다.
        raise HostUnavailable() from None
