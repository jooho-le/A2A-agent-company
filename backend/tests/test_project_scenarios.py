"""Host JSON과 제품 입력이 같은 시나리오 등록 정보를 찾는지 확인합니다."""
import unittest
from uuid import uuid4

from project_scenarios import _load_registry, is_supported_project_scenario


class ProjectScenarioTests(unittest.TestCase):
    def test_registered_uuid_and_json_string_resolve_to_same_scenario(self):
        scenario_id = _load_registry().SCN_001_ID
        self.assertTrue(is_supported_project_scenario(scenario_id))
        self.assertTrue(is_supported_project_scenario(str(scenario_id)))

    def test_unknown_and_invalid_identifiers_remain_unsupported(self):
        for value in (uuid4(), str(uuid4()), None, "", "SCN-001", "invalid"):
            with self.subTest(value=value):
                self.assertFalse(is_supported_project_scenario(value))


if __name__ == "__main__":
    unittest.main()
