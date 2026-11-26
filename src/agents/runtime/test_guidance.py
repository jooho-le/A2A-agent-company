"""Shared runner coordinates and fixture guidance; no test answers or verdicts."""

UNIT_TEST_ID_DESCRIPTION = (
    "Exact unittest TestCase.id(): module.Class.test_method, relative to the discovery directory. "
    "For tests/test_signup.py defining SignupTests.test_create, use "
    "test_signup.SignupTests.test_create (not just test_create, and no tests. prefix). "
    "For a browser case, use its exact JSON testId."
)

PASSWORD_FIXTURE_EXAMPLE = (
    'from uuid import uuid4\n'
    'from _host_test_data import new_password\n'
    'fresh_email = f"case-{uuid4().hex}@example.org"\n'
    'sample = new_password(length=12)\n'
    'payload = {"email": fresh_email}\n'
    'payload["password"] = sample'
)

CREDENTIAL_TEST_AUTHORING_POLICY = "RUNTIME_ONLY_CREDENTIAL_TEST_AUTHORING_V1"
PASSWORD_BOUNDARY_EXAMPLE = (
    'from _host_test_data import new_password\n'
    'empty_sample = new_password(length=0)\n'
    'short_sample = new_password(length=7)\n'
    'sample = new_password(length=8)\n'
    'payload = {}\n'
    'payload["password"] = short_sample'
)

PYTHON_TEST_CREDENTIAL_WRITE_GUIDANCE = (
    "Python unittest: use testTargets.testData, the Host runtime factory. Import "
    "from _host_test_data import new_password; create sample = new_password(length=12) "
    "inside the test and assign payload['password'] = sample. Empty, short and invalid "
    "string cases also use this factory with the case's length/kind. Never embed any "
    "credential string, including defaults, helper returns or dictionary values. "
    "Choose case lengths from the assigned criteria; examples are not acceptance rules. "
    "Read Source first, then submit one write_test_file call per response so a rejected "
    "write can use the bounded pre-execution correction without replaying other tools. "
    "This Python module is unavailable in browser JSON; use that target's format."
)

PASSWORD_FIXTURE_GUIDANCE = (
    "For Host Python unittest, fresh_email is your local variable, NOT self.fresh_email or a Host method. "
    "new_password is a module function, NOT self.new_password. Build the request with the testData factory:\n"
    + PASSWORD_FIXTURE_EXAMPLE
    + "\nChoose the length from the public requirements or your case, including 0. Use this same factory for every "
    "password and reuse the variable for repeated requests. The Host supplies the module only inside the "
    "unit runner; do not read or write a _host_test_data.py file through project tools. "
    "Negative and boundary string cases must use the same factory, not another fixed string:\n"
    + PASSWORD_BOUNDARY_EXAMPLE
    + "\nThe lengths above are examples, not a new minimum. Preserve the assigned case intent, "
    "including wrong types and nulls when required; do not convert such values into valid strings. "
    "Do not set credential strings in helper defaults or helper return values. Generate per test. "
    "Use actual application responses for valid access tokens and runtime synthetic data for invalid "
    "tokens; never copy server/API credentials or stored hash literals into tests."
)

CONCURRENT_SIGNUP_GUIDANCE = (
    "self.concurrent_signup_statuses(first_payload, second_payload) returns tuple[int, int], the two HTTP "
    "status codes in payload order. Use statuses = self.concurrent_signup_statuses(first_payload, second_payload) "
    "to check both codes. Separately call state = self.signup_email_state(expected_canonical_email) to check "
    "the committed row counts. Do not use an individual integer status as a container. Use a fresh identity "
    "that has not already been submitted when measuring simultaneous creation. Derive success/rejection "
    "ranges from the public contract, not an assumed exact 200 or 400."
)

APP_FIXTURE_USAGE_GUIDANCE = (
    "testTargets.appFixture.methods declares the exact Host parameters and return shapes. "
    "state = self.signup_email_state(expected_canonical_email) is a dict: read state['totalRows'], "
    "state['exactEmailRows'] and state['hasDirectEmailUniqueKey'] by key. Never unpack that dict as row values. "
    "Pass the password variable itself, never [sample] or a dict, as the password keyword argument. For check_signup_output "
    "create that string inside the current test body with new_password(length=12), and use the same string in "
    "the payload. Do not call this output helper with a password shorter than 8 characters or absent from "
    "the body; keep those negative cases using self.client.post and normal response/DB assertions. "
    "signup_hash_parameters returns argon2id/verified/memoryKiB/iterations/parallelism and optional distinctSalts; "
    "there is no algorithm or memory_cost result key. Each returned dict is an immutable-in-time snapshot: "
    "the first observation has no distinctSalts key and a later call NEVER fills in that earlier dict. "
    "Create two different committed accounts in the same test, observe each, then assert "
    "second_params['distinctSalts'] from the SECOND call only. Check the numeric hash settings with "
    "assertGreaterEqual against the public minimums; assertEqual wrongly rejects stronger settings. "
    "concurrent_signup_statuses returns two integer HTTP codes, not status containers or row metadata. "
    "Use the success/rejection ranges in the public contract; do not narrow a 2xx/4xx range to only 200/400. "
    "For simultaneous creation use an identity not already created by a preceding request. "
    "Measure signup_hash_parameters on two distinct committed accounts within one test and assert the "
    "public policy; a helper call alone is not an assertion. Write runnable Python, never a REDACTED placeholder "
    "or an actual NUL byte in the source. Construct control-character request data at runtime, for example chr(0). "
    "This is public fixture usage, not a product verdict or permission to change assigned criteria."
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
    + PASSWORD_FIXTURE_GUIDANCE + " " + APP_FIXTURE_USAGE_GUIDANCE + " "
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
            "httpOutput": {"function": "check_http_output", "availability": "QA/Security generated unit scopes only",
                "parameters": ["client", "method", "path"], "keywordOnly": ["sensitive"],
                "requestArguments": "json/data/headers/params follow the actual TestClient request API",
                "returns": "httpx.Response; raises REQUEST_OUTPUT_EXPOSURE when a synthetic secret or encoded hash is exposed",
                "instruction": "Import check_http_output from _host_test_data. Pass your started TestClient and real serviceDesign route. sensitive must be a current-test new_password() value present in the request. The Host observes response, headers and Python request logs without publishing values. This does not inspect DB storage or establish authorization correctness."},
            "example": PASSWORD_FIXTURE_EXAMPLE,
            "authoringPolicy": CREDENTIAL_TEST_AUTHORING_POLICY,
            "boundaryExample": PASSWORD_BOUNDARY_EXAMPLE,
            "instruction": PYTHON_TEST_CREDENTIAL_WRITE_GUIDANCE}


def credential_test_instructions(task_input):
    """Place the same credential recipe next to the current validation task.

    Only configured Python test targets advertise the Host Python module.
    This adds instructions, never credentials, permissions or test verdicts.
    """
    original = task_input.get("runtimeInstructions", "")
    targets = task_input.get("testTargets", ())
    if not isinstance(original, str) or not isinstance(targets, (list, tuple)):
        return original
    if not any(isinstance(target, dict) and target.get("toolName") == "run_unit_tests"
               and target.get("format") == "python-unittest" for target in targets):
        return original
    return original + "\n" + CREDENTIAL_TEST_AUTHORING_POLICY + ": " + PASSWORD_FIXTURE_GUIDANCE


def unit_test_targets(configuration, role, *, scenario_key=None):
    from mcp_tools.tools.unit_runner import app_fixture_contract, service_fixture_contract
    if scenario_key == "SERVICE-WEB":
        return [{"toolName": "run_unit_tests", "selector": scope.name,
                 "directory": f"outputs/{role}/" + scope.source_directory,
                 "pattern": scope.pattern, "format": "python-unittest", "required": True,
                 "testIdRule": UNIT_TEST_ID_DESCRIPTION,
                 "fixtureGuidance": "HTTP tests must import ServiceTestCase from _host_service_test and subclass it. Use self.client in test methods. Do not construct TestClient, override setup/teardown/run, import main/database/models at module level, create missing tables, or override product dependencies. The Host starts the actual main.app lifespan on an isolated DB and cleans up. Read actual Source and serviceDesign. SCN-001 AppTestCase/User/signup helpers do not describe this application. " + PASSWORD_FIXTURE_GUIDANCE,
                 "appFixture": service_fixture_contract(),
                 "minimalExample": "from _host_service_test import ServiceTestCase\nfrom _host_test_data import new_password\nclass ApiTests(ServiceTestCase):\n    def test_request(self):\n        response = self.client.get('/')\n        self.assertEqual(response.status_code, 200)\n",
                 "testData": unit_test_data_contract(),
                 "testDataFactory": unit_test_data_contract()}
                for scope in configuration.scopes if scope.kind == "QA_TESTS"]
    return [{"toolName": "run_unit_tests", "selector": scope.name,
             "directory": f"outputs/{role}/" + scope.source_directory,
             "pattern": scope.pattern, "format": "python-unittest", "required": True,
             "testIdRule": UNIT_TEST_ID_DESCRIPTION, "fixtureGuidance": UNIT_FIXTURE_GUIDANCE,
             "appFixture": app_fixture_contract(),
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
                 "DB constraints from sequential duplicate requests. For two distinct committed accounts in one test, use signup_hash_parameters(canonical_email, password=sample) to measure the algorithm, verification result, memoryKiB, iterations, parallelism and distinctSalts. Assert distinctSalts only on the second return; the first return does not update. Assert the public minimums with >=."}
                if role == "security" else {"outputEvidence":
                    "When an assigned functional test inspects signup output, self.check_signup_output(payload, "
                    "password=sample) records what the response exposed or whether an exception escaped. Use a "
                    "runtime-generated sample of at least 8 characters in that request. Keep normal functional assertions."}),
             "testData": unit_test_data_contract()}
            for scope in configuration.scopes if scope.kind == "QA_TESTS"]
