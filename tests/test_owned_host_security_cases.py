"""Ver1 Security protected cases: the explicit role contract (case file schema 2).

Schema 1 files carry no roles, so a Security protected scope stays closed and
cases are never inferred from the QA suite. Schema 2 names every case's role, and
the Host admits a Security protected scope only when each declared role equals
the role of the protected scope the case targets.
"""

from copy import deepcopy
import json
import unittest
from uuid import uuid4

from test_owned_host import _HostFixture
from test_tool_configuration import SUITE_REF, protected_unit

from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario


def _security_scope():
    return {"name": "protected-security", "kind": "PROTECTED", "role": "SECURITY",
            "protected_suite_ref": SUITE_REF,
            "protected_files": {"tests/test_security.py": "# approved independent fixture; never executed\n"}}


_QA_CASE = {"toolName": "run_unit_tests", "selector": "protected-unit", "testId": "protected.signup",
            "title": "Protected signup fixture", "expectedResult": "Approved signup criterion"}
_SECURITY_CASE = {"toolName": "run_unit_tests", "selector": "protected-security", "testId": "protected.hash",
                  "title": "Protected hash fixture", "expectedResult": "Approved hash criterion"}


class SecurityProtectedCaseContractTests(_HostFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.base_tools = deepcopy(self.tools)

    def ver1(self, *, roles=("QA", "SECURITY"), schema=2, security_scope=True):
        self.tools = deepcopy(self.base_tools)
        self.tools["unit"]["scopes"].append(protected_unit())
        cases = [dict(_QA_CASE, requirementId=str(uuid4()))]
        if security_scope:
            self.tools["unit"]["scopes"] += [{"name": "security-tests", "kind": "QA_TESTS", "role": "SECURITY"},
                                             _security_scope()]
            cases.append(dict(_SECURITY_CASE, requirementId=str(uuid4())))
        if schema == 2:
            cases = [dict(case, role=role) for case, role in zip(cases, roles)]
        self.cases = {"schemaVersion": schema, "cases": cases}
        self.write()

    def rejected(self):
        with self.assertRaises(Exception) as caught:
            self.inspect(protected_cases_file=self.cases_file)
        self.assert_safe(caught.exception)

    def test_schema_1_keeps_security_protected_scope_closed(self):
        self.ver1(schema=1)
        self.rejected()

    def test_schema_2_with_declared_roles_admits_security_protected_scope(self):
        self.ver1()
        plan = self.inspect(protected_cases_file=self.cases_file)
        self.assertEqual(plan.approved_configuration.protected_test_suite_ref, SUITE_REF)
        self.assertEqual({case.selector for case in plan.protected_cases}, {"protected-unit", "protected-security"})

    def test_declared_role_must_equal_the_target_scope_role(self):
        for roles in (("QA", "QA"), ("SECURITY", "SECURITY"), ("SECURITY", "QA")):
            with self.subTest(roles=roles):
                self.ver1(roles=roles)
                self.rejected()

    def test_schema_2_rejects_unknown_or_missing_roles(self):
        self.ver1()
        original = deepcopy(self.cases)
        for changed in (
            {"schemaVersion": 2, "cases": [dict(original["cases"][0], role="ADMIN"), original["cases"][1]]},
            {"schemaVersion": 2, "cases": [{key: value for key, value in original["cases"][0].items() if key != "role"},
                                           original["cases"][1]]},
            {"schemaVersion": 3, "cases": original["cases"]},
        ):
            with self.subTest(changed=json.dumps(changed)[:60]):
                self.cases = changed
                self.write()
                self.rejected()

    def test_schema_2_without_security_scope_requires_qa_roles(self):
        self.ver1(roles=("QA",), security_scope=False)
        self.assertEqual(len(self.inspect(protected_cases_file=self.cases_file).protected_cases), 1)
        self.ver1(roles=("SECURITY",), security_scope=False)
        self.rejected()

    def test_evaluation_ver1_configuration_passes_the_host(self):
        from evaluation.experiment import configure
        documents = configure(self.base_tools, contract=get_scenario(SCN_001_ID).planner_contract(),
                              base=self.tools_file.parent)
        tools_file, cases_file = self.base / "ver1-tools.json", self.base / "ver1-cases.json"
        tools_file.write_text(json.dumps(documents["ver1/owned-tools.local.json"]), encoding="utf-8")
        cases_file.write_text(json.dumps(documents["ver1/owned-protected-cases.local.json"]), encoding="utf-8")
        plan = self.inspect(tools_file=tools_file, protected_cases_file=cases_file)
        self.assertEqual(len(plan.protected_cases), 9)
        self.assertEqual(plan.approved_configuration.protected_test_suite_ref,
                         documents["experiment.json"]["protectedSuiteRef"])
        ver2_file = self.base / "ver2-tools.json"
        ver2_file.write_text(json.dumps(documents["ver2/owned-tools.local.json"]), encoding="utf-8")
        self.assertIsNone(self.inspect(tools_file=ver2_file).approved_configuration.protected_test_suite_ref)


if __name__ == "__main__":
    unittest.main()
