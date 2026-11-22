"""Shared runner coordinates and fixture guidance; no test answers or verdicts."""

UNIT_TEST_ID_DESCRIPTION = (
    "Exact unittest TestCase.id(): module.Class.test_method, relative to the discovery directory. "
    "For tests/test_signup.py defining SignupTests.test_create, use "
    "test_signup.SignupTests.test_create (not just test_create, and no tests. prefix). "
    "For a browser case, use its exact JSON testId."
)

PASSWORD_FIXTURE_EXAMPLE = (
    'from _host_test_data import new_password\n'
    'sample = new_password(length=12)\n'
    'payload = {"email": fresh_email}\n'
    'payload["password"] = sample'
)

PASSWORD_FIXTURE_GUIDANCE = (
    "For Host Python unittest, choose your own fresh_email and build the request with the testData factory:\n"
    + PASSWORD_FIXTURE_EXAMPLE
    + "\nChoose the length from the public requirements or your case, including 0. Use this same factory for every "
    "password and reuse the variable for repeated requests. The Host supplies the module; do not write it."
)

CONCURRENT_SIGNUP_GUIDANCE = (
    "self.concurrent_signup_statuses(first_payload, second_payload) returns tuple[int, int], the two HTTP "
    "status codes in payload order. Use statuses = self.concurrent_signup_statuses(first_payload, second_payload) "
    "to check both codes. Separately call state = self.signup_email_state(expected_canonical_email) to check "
    "the committed row counts."
)

UNIT_FIXTURE_GUIDANCE = (
    "Read the public connection contract before writing tests. For SCN-001, import "
    "from _host_app_test import AppTestCase and subclass AppTestCase. Use self.client, self.db "
    "and self.User inside test methods. The Host sets an isolated DATABASE_URL before importing "
    "the app, starts its lifespan and cleans up per test. Do not import main/database/models at "
    "module level, override lifecycle methods, create tables, replace dependencies, or create "
    "another TestClient. If the product does not initialize tables, report that failure. "
    "For other contracts use their declared lifecycle. Use fresh synthetic emails unless testing "
    "duplicates. Generate all test "
    "passwords at execution time, including short/long/invalid cases; never embed credential literals. "
    + PASSWORD_FIXTURE_GUIDANCE + " "
    "Do not print password/hash values in assertions or reports. Group independent source reads "
    "in one model response when multiple tool calls are supported."
)


def unit_test_data_contract():
    from mcp_tools.tools.unit_runner import TEST_DATA_POLICY, TEST_DATA_MODULE, MAX_TEST_PASSWORD_LENGTH
    return {"policy": TEST_DATA_POLICY, "module": TEST_DATA_MODULE, "function": "new_password",
            "parameters": {"length": {"type": "integer", "minimum": 0,
                                       "maximum": MAX_TEST_PASSWORD_LENGTH, "default": 12},
                           "kind": {"enum": ["ascii", "unicode", "digits", "spaces"], "default": "ascii"}},
            "availability": "Host Python unittest discovery and execution only",
            "returns": "Runtime synthetic string, exactly length characters; no acceptance verdict",
            "example": PASSWORD_FIXTURE_EXAMPLE}


def unit_test_targets(configuration, role):
    return [{"toolName": "run_unit_tests", "selector": scope.name,
             "directory": f"outputs/{role}/" + scope.source_directory,
             "pattern": scope.pattern, "format": "python-unittest", "required": True,
             "testIdRule": UNIT_TEST_ID_DESCRIPTION, "fixtureGuidance": UNIT_FIXTURE_GUIDANCE,
             "appFixture": {"module": "_host_app_test", "class": "AppTestCase",
                            "contract": "SCN-001", "attributes": ["client", "db", "User", "app"],
                            "createsProductTables": False},
             "signupEmailEvidence": (
                 "For an assigned SCN-001 REQ-003, select your own fresh synthetic identities using the public emailPolicy. "
                 "self.signup_email_state(expected_canonical_email) reads the committed database and returns totalRows, "
                 "exactEmailRows (case-sensitive), and hasDirectEmailUniqueKey (single-column PK, unconditional UNIQUE "
                 "constraint or unique index). Missing direct metadata alone does not prove duplicates can be stored. "
                 + (CONCURRENT_SIGNUP_GUIDANCE + " Exercise simultaneous submissions of the same identity. "
                    "Assert one creation, a duplicate rejection, and the committed row counts. "
                    "A clean sequential test does not establish the concurrent result." if role == "qa" else
                    "Submit a fresh valid mixed-case, whitespace-padded identity before its canonical duplicate, "
                    "and check creation, exact stored spelling, duplicate rejection and the deployed DB key. "
                    "If the schema uses another mechanism, verify enforcement directly on the isolated database.")),
             **({"outputEvidence": "For SCN-001 REQ-006 use self.check_signup_output(your_json_payload, "
                 "password=sample) for the signup calls you design, using a runtime synthetic value at least 8 "
                 "characters long that is in the payload. It returns the observed response and records channel states "
                 "and value-free failure coordinates. Exercise success, input rejection, and a real non-duplicate database failure. The last "
                 "requires an observed DB engine error and an app error response; inspect request logs as well. "
                 "A field-name assertion alone does not measure exposure. For the other requirements distinguish "
                 "DB constraints from sequential duplicate requests. For two distinct committed accounts in one test, use signup_hash_parameters(canonical_email, password=sample) to measure the algorithm, verification result, memoryKiB, iterations, parallelism and distinctSalts, then assert the public policy."}
                if role == "security" else {"outputEvidence":
                    "When an assigned functional test inspects signup output, self.check_signup_output(payload, "
                    "password=sample) records what the response exposed or whether an exception escaped. Use a "
                    "runtime-generated sample of at least 8 characters in that request. Keep normal functional assertions."}),
             "testData": unit_test_data_contract()}
            for scope in configuration.scopes if scope.kind == "QA_TESTS"]
