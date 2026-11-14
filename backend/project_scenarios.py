"""같은 체크아웃의 정적 시나리오 레지스트리만 사용합니다. Host를 실행/조회하지 않습니다."""

import importlib.util
import sys
from functools import lru_cache
from pathlib import Path


REGISTRY_PATH = Path(__file__).resolve().parents[1] / "src/orchestrator/domain/scenario_registry.py"


class ScenarioRegistryUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _load_registry():
    # 이 파일은 stdlib만 사용하는 정적 정의입니다. 제품 백엔드에서 Host 패키지의
    # __init__·실행기·DB 설정을 import하지 않도록 고정된 파일만 로드합니다.
    module_name = "_product_project_scenario_registry"
    try:
        spec = importlib.util.spec_from_file_location(module_name, REGISTRY_PATH)
        if spec is None or spec.loader is None:
            raise ScenarioRegistryUnavailable()
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module  # dataclass의 모듈 메타데이터 처리에 필요합니다.
        spec.loader.exec_module(module)
        return module
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise ScenarioRegistryUnavailable("시나리오 등록 정보를 확인할 수 없습니다.") from exc


def is_supported_project_scenario(scenario_id):
    try:
        scenario = _load_registry().get_scenario(scenario_id)
        return scenario is not None and scenario.key == "SCN-001"
    except ScenarioRegistryUnavailable:
        raise
    except Exception as exc:
        raise ScenarioRegistryUnavailable("시나리오 등록 정보를 확인할 수 없습니다.") from exc
