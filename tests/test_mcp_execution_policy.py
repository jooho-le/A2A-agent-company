"""Inert retry policy tests; no child, Container, Source execution or writes."""

from dataclasses import FrozenInstanceError
from hashlib import sha256
import json
import math
import unittest
from unittest.mock import patch
from uuid import UUID

from mcp_tools.client import MCPClientError, MCPDeliveryState
from mcp_tools.execution_policy import (
    EXECUTION_TOOL_NAMES, WRITE_TOOL_NAMES, ExecutionPolicyError, FailureClassification,
    RetrySafetyConfirmation, arguments_sha256, classify_failure, classify_success,
)
from orchestrator.domain.retry_policy import RetryDecision, ToolErrorKind
from orchestrator.domain.tool_evidence import ToolExecutionOutcome


CALL_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_ID = UUID("22222222-2222-4222-8222-222222222222")
DIGEST = "a" * 64
RUN_ID = "33333333-3333-4333-8333-333333333333"


def confirmation(**changes):
    values = {"logical_call_id": CALL_ID, "attempt": 0, "input_sha256": DIGEST,
        "cleanup_complete": True, "result_known_not_applied": True}
    values.update(changes)
    return RetrySafetyConfirmation(**values)


def failure(code="MCP_CLIENT_TOOL_FAILED", tool_code=None, delivery="REPLIED", protocol=None):
    return MCPClientError(code, tool_error_code=tool_code, delivery_state=delivery, protocol_error_code=protocol)


def classify(error=None, name="run_build", retries_used=0, **changes):
    return classify_failure(name, error or failure(tool_code="SCANNER_ERROR"), retries_used, **changes)


def safe_classify(error, name="run_build", retries_used=0, **changes):
    values = {"safety": confirmation(attempt=retries_used), "logical_call_id": CALL_ID, "input_sha256": DIGEST}
    values.update(changes)
    return classify(error, name, retries_used, **values)


def output(name, **changes):
    values = {
        "run_build": {"exitCode": 0, "durationMs": 10, "executionManifestId": RUN_ID},
        "run_unit_tests": {"total": 1, "passed": 1, "failed": 0, "skipped": 0,
            "reportRef": "artifact://tests/report", "executionManifestId": RUN_ID},
        "run_browser_tests": {"total": 1, "passed": 1, "failed": 0,
            "traceRefs": ["artifact://browser/trace"], "executionManifestId": RUN_ID},
        "run_security_scan": {"findings": [], "reportRef": "artifact://security/report", "executionManifestId": RUN_ID},
        "read_project_file": {"path": "src/main.py", "content": "password = request.password\n",
            "sha256": DIGEST, "sizeBytes": 28},
        "write_source_file": {"path": "src/main.py", "sha256": DIGEST, "sizeBytes": 0, "changed": True},
        "write_test_file": {"path": "tests/qa/test_main.py", "sha256": DIGEST, "changed": True},
        "apply_patch": {"changedFiles": ["src/main.py"], "newHashes": {"src/main.py": DIGEST}},
        "read_test_report": {"testResult": {"failed": 1}},
        "read_security_report": {"securityResult": {"findings": [{"ruleId": "B307"}]}},
    }[name]
    values.update(changes)
    return values


class PolicyTestCase(unittest.TestCase):
    def assert_invalid(self, operation):
        with self.assertRaises(ExecutionPolicyError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "MCP_EXECUTION_POLICY_INVALID")
        self.assertEqual(caught.exception.args, ("MCP_EXECUTION_POLICY_INVALID",))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def assert_classification(self, result, kind, decision, safe=False, unknown=False):
        self.assertEqual(result.error_kind, kind.value if isinstance(kind, ToolErrorKind) else kind)
        self.assertIs(result.retry_decision, decision)
        self.assertIs(result.retry_safe, safe)
        self.assertIs(result.result_unknown, unknown)


class ArgumentsHashTests(PolicyTestCase):
    def test_fixed_name_sets_immutable_and_exact(self):
        self.assertEqual(EXECUTION_TOOL_NAMES, {"run_build", "run_unit_tests", "run_browser_tests", "run_security_scan"})
        self.assertEqual(WRITE_TOOL_NAMES, {"write_source_file", "write_test_file", "apply_patch"})
        self.assertIsInstance(EXECUTION_TOOL_NAMES, frozenset)
        self.assertIsInstance(WRITE_TOOL_NAMES, frozenset)

    def test_hash_matches_canonical_original_json(self):
        arguments = {"snapshotId": RUN_ID, "workspaceId": RUN_ID}
        raw = json.dumps({"toolName": "run_build", "arguments": arguments}, sort_keys=True,
            ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        self.assertEqual(arguments_sha256("run_build", arguments), sha256(raw.encode()).hexdigest())
        self.assertEqual(arguments_sha256("run_build", arguments),
            arguments_sha256("run_build", dict(reversed(list(arguments.items())))))

    def test_name_and_each_argument_change_identity(self):
        arguments = {"workspaceId": RUN_ID, "path": "src/main.py", "content": "print('안녕')\n"}
        original = arguments_sha256("write_source_file", arguments)
        for changed in ({**arguments, "content": arguments["content"] + "\n"},
                {**arguments, "path": "src/next.py"}, {**arguments, "workspaceId": str(CALL_ID)}):
            self.assertNotEqual(original, arguments_sha256("write_source_file", changed))
        self.assertNotEqual(original, arguments_sha256("write_test_file", arguments))
        self.assertEqual(arguments["content"], "print('안녕')\n")

    def test_ordinary_password_variables_remain_byte_exact(self):
        arguments = {"workspaceId": RUN_ID, "path": "src/main.py", "content": "password = request.password\n"}
        raw = json.dumps({"toolName": "write_source_file", "arguments": arguments}, sort_keys=True,
            ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(arguments_sha256("write_source_file", arguments), sha256(raw.encode()).hexdigest())

    def test_secret_literal_rejected_without_source_or_error_context(self):
        source = "password = 'never-print-this-test-credential'\n"
        arguments = {"workspaceId": RUN_ID, "path": "src/main.py", "content": source}
        self.assert_invalid(lambda: arguments_sha256("write_source_file", arguments))

    def test_fixed_closed_schema_and_json_types_enforced(self):
        good = {"workspaceId": RUN_ID, "snapshotId": RUN_ID}
        for arguments in (None, [], (), {**good, "command": "private-command"},
                {"workspaceId": RUN_ID}, {**good, "snapshotId": "not-a-uuid"},
                {**good, "snapshotId": math.nan}, {1: "private-value"}, {**good, "extra": object()}):
            self.assert_invalid(lambda: arguments_sha256("run_build", arguments))
        for name in ("unknown_tool", None, 3, "run_build\n"):
            self.assert_invalid(lambda: arguments_sha256(name, good))

    def test_source_utf8_size_limit_not_only_characters(self):
        arguments = {"workspaceId": RUN_ID, "path": "src/main.py", "content": "한" * 350_000}
        self.assert_invalid(lambda: arguments_sha256("write_source_file", arguments))

    def test_validation_does_not_call_custom_object_repr(self):
        class NeverStringify:
            def __repr__(self):
                raise AssertionError("source repr was called")
        self.assert_invalid(lambda: arguments_sha256("run_build", NeverStringify()))


class ConfirmationTests(PolicyTestCase):
    def test_confirmation_is_frozen_with_hidden_identity_and_hash(self):
        selected = confirmation()
        self.assertEqual(repr(selected), "RetrySafetyConfirmation()")
        self.assertNotIn(str(CALL_ID), repr(selected))
        self.assertNotIn(DIGEST, repr(selected))
        with self.assertRaises(FrozenInstanceError):
            selected.attempt = 1

    def test_only_uuid4_native_identity_is_accepted(self):
        for value in (str(CALL_ID), None, "private-identity", UUID(int=0),
                UUID("11111111-1111-1111-8111-111111111111")):
            self.assert_invalid(lambda: confirmation(logical_call_id=value))

    def test_attempt_strict_integer_bounded_zero_through_two(self):
        for value in (True, False, -1, 3, 0.0, "0", None):
            self.assert_invalid(lambda: confirmation(attempt=value))
        for value in (0, 1, 2):
            self.assertEqual(confirmation(attempt=value).attempt, value)

    def test_input_digest_strict_lowercase_hex(self):
        for value in (None, "", "A" * 64, "g" * 64, "a" * 63, "a" * 65, "a" * 64 + "\n", 4):
            self.assert_invalid(lambda: confirmation(input_sha256=value))

    def test_cleanup_and_not_applied_require_actual_booleans(self):
        for field in ("cleanup_complete", "result_known_not_applied"):
            for value in (0, 1, "true", None, []):
                self.assert_invalid(lambda: confirmation(**{field: value}))

    def test_failure_classification_has_frozen_stable_only_repr(self):
        selected = FailureClassification(error_kind="UNKNOWN_ERROR", retry_decision=RetryDecision.INSPECT_STATE,
            retry_safe=False, result_unknown=True)
        self.assertIn("UNKNOWN_ERROR", repr(selected))
        with self.assertRaises(FrozenInstanceError):
            selected.error_kind = "private"
        cancelled = FailureClassification(error_kind="CANCELLED", retry_decision=RetryDecision.DO_NOT_RETRY,
            retry_safe=False, result_unknown=True)
        self.assertTrue(cancelled.result_unknown)

    def test_failure_classification_rejects_raw_text_and_invalid_flags(self):
        good = {"error_kind": "UNKNOWN_ERROR", "retry_decision": RetryDecision.DO_NOT_RETRY,
            "retry_safe": False, "result_unknown": False}
        for changes in ({"error_kind": "private-source"}, {"error_kind": 1}, {"retry_decision": "RETRY"},
                {"retry_safe": 1}, {"result_unknown": "false"}, {"retry_decision": RetryDecision.RETRY},
                {"retry_decision": RetryDecision.RETRY, "retry_safe": True, "result_unknown": True}):
            self.assert_invalid(lambda: FailureClassification(**{**good, **changes}))

    def test_budget_or_deadline_override_can_keep_safety_true(self):
        selected = FailureClassification(error_kind="RESOURCE_BUSY", retry_decision=RetryDecision.DO_NOT_RETRY,
            retry_safe=True, result_unknown=False)
        self.assertTrue(selected.retry_safe)


class FailureClassificationTests(PolicyTestCase):
    def test_permanent_client_refusals_never_retry_even_with_confirmation(self):
        for code, kind in (("MCP_CLIENT_CONFIGURATION_INVALID", ToolErrorKind.INPUT_SCHEMA_ERROR),
                ("MCP_CLIENT_ARGUMENTS_INVALID", ToolErrorKind.INPUT_SCHEMA_ERROR),
                ("MCP_CLIENT_PERMISSION_DENIED", ToolErrorKind.PERMISSION_DENIED)):
            for delivery in MCPDeliveryState:
                self.assert_classification(safe_classify(failure(code, delivery=delivery)), kind, RetryDecision.DO_NOT_RETRY)

    def test_permanent_tool_refusals_never_retry_for_any_role(self):
        for code, kind in (("PERMISSION_DENIED", ToolErrorKind.PERMISSION_DENIED),
                ("SECRET_DENIED", ToolErrorKind.PERMISSION_DENIED), ("PATH_DENIED", ToolErrorKind.PATH_TRAVERSAL),
                ("TOOL_NOT_IMPLEMENTED", ToolErrorKind.UNSUPPORTED_TOOL)):
            for name in ("run_build", *WRITE_TOOL_NAMES):
                self.assert_classification(safe_classify(failure(tool_code=code), name), kind, RetryDecision.DO_NOT_RETRY)

    def test_protocol_codes_not_exception_words_decide_permanent_failure(self):
        for code in (-32700, -32600, -32601, -32602):
            error = failure("MCP_CLIENT_PROTOCOL_INVALID", protocol=code)
            kind = ToolErrorKind.UNSUPPORTED_TOOL if code == -32601 else ToolErrorKind.INPUT_SCHEMA_ERROR
            self.assert_classification(classify(error), kind, RetryDecision.DO_NOT_RETRY)

    def test_definitely_not_sent_startup_can_retry_initial_plus_two_attempts(self):
        error = failure("MCP_CLIENT_UNAVAILABLE", delivery="NOT_SENT")
        for retries in (0, 1, 2):
            self.assert_classification(classify(error, retries_used=retries), ToolErrorKind.PROCESS_STARTUP_FAILURE,
                RetryDecision.RETRY if retries < 2 else RetryDecision.DO_NOT_RETRY, safe=True)

    def test_replied_approved_startup_and_busy_retry_only_nonwrites(self):
        for code in ("PROCESS_STARTUP_FAILURE", "RESOURCE_BUSY"):
            for name in ("run_build", "run_unit_tests", "run_browser_tests", "run_security_scan", "read_project_file"):
                for retries in (0, 1, 2):
                    self.assert_classification(classify(failure(tool_code=code), name, retries), code,
                        RetryDecision.RETRY if retries < 2 else RetryDecision.DO_NOT_RETRY, safe=True)
            for name in WRITE_TOOL_NAMES:
                self.assert_classification(safe_classify(failure(tool_code=code), name),
                    ToolErrorKind.WRITE_RESULT_UNKNOWN, RetryDecision.INSPECT_STATE, unknown=True)

    def test_not_sent_startup_is_only_automatic_write_retry(self):
        for name in WRITE_TOOL_NAMES:
            self.assert_classification(classify(failure("MCP_CLIENT_UNAVAILABLE", delivery="NOT_SENT"), name),
                ToolErrorKind.PROCESS_STARTUP_FAILURE, RetryDecision.RETRY, safe=True)
            self.assert_classification(classify(failure(delivery="NOT_SENT"), name),
                "UNKNOWN_ERROR", RetryDecision.DO_NOT_RETRY)

    def test_unknown_approved_code_does_not_prove_stopped_execution(self):
        for code in ("PROCESS_STARTUP_FAILURE", "RESOURCE_BUSY"):
            self.assert_classification(classify(failure(tool_code=code, delivery="UNKNOWN")),
                code, RetryDecision.INSPECT_STATE, unknown=True)

    def test_unknown_transport_inspects_before_replaying(self):
        for code in ("MCP_CLIENT_FAILED", "MCP_CLIENT_UNAVAILABLE"):
            self.assert_classification(classify(failure(code, delivery="UNKNOWN")),
                ToolErrorKind.MCP_TRANSPORT_INTERRUPTED, RetryDecision.INSPECT_STATE, unknown=True)

    def test_confirmed_unknown_transport_allows_nonwrite_two_retry_limit(self):
        error = failure("MCP_CLIENT_FAILED", delivery="UNKNOWN")
        for retries in (0, 1, 2):
            self.assert_classification(safe_classify(error, retries_used=retries),
                ToolErrorKind.MCP_TRANSPORT_INTERRUPTED,
                RetryDecision.RETRY if retries < 2 else RetryDecision.DO_NOT_RETRY, safe=True)

    def test_timeout_without_host_confirmation_requires_inspection(self):
        for error in (failure("MCP_CLIENT_TIMEOUT", delivery="UNKNOWN"), failure(tool_code="TIMEOUT")):
            self.assert_classification(classify(error), ToolErrorKind.TOOL_TIMEOUT, RetryDecision.INSPECT_STATE, unknown=True)

    def test_timeout_matching_confirmation_allows_nonwrite_retries_only(self):
        for error in (failure("MCP_CLIENT_TIMEOUT", delivery="UNKNOWN"), failure(tool_code="TIMEOUT")):
            self.assert_classification(safe_classify(error), ToolErrorKind.TOOL_TIMEOUT, RetryDecision.RETRY, safe=True)
            self.assert_classification(safe_classify(error, retries_used=2),
                ToolErrorKind.TOOL_TIMEOUT, RetryDecision.DO_NOT_RETRY, safe=True)

    def test_confirmations_must_match_identity_attempt_hash_and_both_flags(self):
        error = failure("MCP_CLIENT_FAILED", delivery="UNKNOWN")
        for changes in ({"logical_call_id": OTHER_ID}, {"input_sha256": "b" * 64}, {"attempt": 1},
                {"cleanup_complete": False}, {"result_known_not_applied": False}):
            self.assert_classification(safe_classify(error, safety=confirmation(**changes)),
                ToolErrorKind.MCP_TRANSPORT_INTERRUPTED, RetryDecision.INSPECT_STATE, unknown=True)

    def test_confirmation_without_current_host_identity_does_not_authorize_retry(self):
        error = failure("MCP_CLIENT_FAILED", delivery="UNKNOWN")
        for changes in ({"logical_call_id": None}, {"input_sha256": None},
                {"logical_call_id": None, "input_sha256": None}):
            self.assert_classification(safe_classify(error, **changes),
                ToolErrorKind.MCP_TRANSPORT_INTERRUPTED, RetryDecision.INSPECT_STATE, unknown=True)

    def test_boolean_confirmation_never_authorizes_uncertain_writes(self):
        for name in WRITE_TOOL_NAMES:
            for error in (failure("MCP_CLIENT_FAILED", delivery="UNKNOWN"),
                    failure("MCP_CLIENT_TIMEOUT", delivery="UNKNOWN"), failure(tool_code="TIMEOUT"),
                    failure(tool_code="WRITE_FAILED"), failure(tool_code="PATCH_FAILED")):
                self.assert_classification(safe_classify(error, name),
                    ToolErrorKind.WRITE_RESULT_UNKNOWN, RetryDecision.INSPECT_STATE, unknown=True)

    def test_known_prewrite_state_rejection_does_not_loop(self):
        for name in WRITE_TOOL_NAMES:
            for code in ("WRITE_CONFLICT", "BASE_MISMATCH", "FILE_TOO_LARGE"):
                self.assert_classification(safe_classify(failure(tool_code=code), name), code, RetryDecision.DO_NOT_RETRY)

    def test_generic_execution_failures_do_not_retry_when_replied(self):
        for code in ("SCANNER_ERROR", "TEST_RUNNER_ERROR", "BROWSER_START_FAILED", "BUILD_EXECUTION_ERROR",
                "SANDBOX_ERROR", "PROFILE_NOT_FOUND", "TOOL_EXECUTION_FAILED", "TOOL_OUTPUT_INVALID"):
            self.assert_classification(safe_classify(failure(tool_code=code)), code, RetryDecision.DO_NOT_RETRY)

    def test_generic_unknown_execution_failures_still_inspect_with_confirmation(self):
        for code in ("SCANNER_ERROR", "TEST_RUNNER_ERROR", "SANDBOX_ERROR", "PROFILE_NOT_FOUND"):
            self.assert_classification(safe_classify(failure(tool_code=code, delivery="UNKNOWN")),
                code, RetryDecision.INSPECT_STATE, unknown=True)

    def test_invalid_output_reply_is_not_trusted_as_a_known_execution_result(self):
        for delivery in ("REPLIED", "UNKNOWN"):
            for name in EXECUTION_TOOL_NAMES:
                error = failure("MCP_CLIENT_OUTPUT_INVALID", delivery=delivery)
                self.assert_classification(safe_classify(error, name),
                    "UNKNOWN_ERROR", RetryDecision.INSPECT_STATE, unknown=True)

    def test_invalid_write_output_reply_never_permits_replay(self):
        for name in WRITE_TOOL_NAMES:
            error = failure("MCP_CLIENT_OUTPUT_INVALID", delivery="REPLIED")
            self.assert_classification(safe_classify(error, name),
                ToolErrorKind.WRITE_RESULT_UNKNOWN, RetryDecision.INSPECT_STATE, unknown=True)

    def test_error_message_is_never_used_as_retry_or_permission_evidence(self):
        error = failure()
        error.args = ("RESOURCE_BUSY password=never-print-this-test-credential",)
        result = classify(error)
        self.assert_classification(result, "UNKNOWN_ERROR", RetryDecision.DO_NOT_RETRY)
        self.assertNotIn("password", repr(result))
        self.assertNotIn("never-print", repr(result))

    def test_tampered_metadata_stays_safe_conservative_and_does_not_echo(self):
        error = failure()
        error.delivery_state = "REPLIED"
        error.tool_error_code = "private-source-bytes"
        error.code = "private-client-code"
        error.protocol_error_code = True
        result = classify(error)
        self.assert_classification(result, "UNKNOWN_ERROR", RetryDecision.INSPECT_STATE, unknown=True)
        self.assertNotIn("private", repr(result))

    def test_invalid_call_failure_and_optional_identity_arguments_are_closed(self):
        for value in (True, -1, 3, 0.0, "0", None):
            self.assert_invalid(lambda: classify(retries_used=value))
        self.assert_invalid(lambda: classify_failure("unknown", failure(), 0))
        self.assert_invalid(lambda: classify_failure("run_build", RuntimeError("private-error"), 0))
        self.assert_invalid(lambda: classify(safety=True))
        self.assert_invalid(lambda: classify(logical_call_id=str(CALL_ID)))
        self.assert_invalid(lambda: classify(input_sha256="private-hash"))

    def test_forged_frozen_confirmation_is_revalidated(self):
        selected = confirmation()
        object.__setattr__(selected, "cleanup_complete", "true")
        self.assert_invalid(lambda: safe_classify(failure("MCP_CLIENT_FAILED", delivery="UNKNOWN"), safety=selected))


class SuccessClassificationTests(PolicyTestCase):
    def test_product_build_failure_is_tool_pass_not_tool_retry(self):
        self.assertEqual(classify_success("run_build", output("run_build", exitCode=7)),
            (ToolExecutionOutcome.PASS, "BUILD_CODE_FAILURE"))
        self.assertEqual(classify_success("run_build", output("run_build")), (ToolExecutionOutcome.PASS, None))

    def test_qa_assertion_failures_are_successfully_executed_tools(self):
        for name in ("run_unit_tests", "run_browser_tests"):
            self.assertEqual(classify_success(name, output(name, passed=0, failed=1)),
                (ToolExecutionOutcome.PASS, "QA_ASSERTION_FAILURE"))
            self.assertEqual(classify_success(name, output(name)), (ToolExecutionOutcome.PASS, None))

    def test_security_findings_do_not_make_scanner_execution_fail(self):
        self.assertEqual(classify_success("run_security_scan", output("run_security_scan", findings=[{"ruleId": "B307"}])),
            (ToolExecutionOutcome.PASS, "SECURITY_FINDING"))
        self.assertEqual(classify_success("run_security_scan", output("run_security_scan")), (ToolExecutionOutcome.PASS, None))

    def test_reads_and_successful_writes_pass_without_final_product_verdict(self):
        for name in ("read_project_file", "write_source_file", "write_test_file", "apply_patch",
                "read_test_report", "read_security_report"):
            self.assertEqual(classify_success(name, output(name)), (ToolExecutionOutcome.PASS, None))

    def test_all_skipped_unit_result_is_execution_pass_not_fabricated_qa_pass(self):
        self.assertEqual(classify_success("run_unit_tests", output("run_unit_tests", passed=0, skipped=1)),
            (ToolExecutionOutcome.PASS, None))

    def test_build_exit_code_strict_bounds_and_boolean_rejected(self):
        for value in (True, -1, 256, 0.0, "0", None):
            self.assert_invalid(lambda: classify_success("run_build", output("run_build", exitCode=value)))

    def test_test_counts_strict_bounds_sum_and_no_empty_suite(self):
        for name in ("run_unit_tests", "run_browser_tests"):
            for changes in ({"total": 0, "passed": 0}, {"total": 2}, {"failed": True},
                    {"passed": -1}, {"passed": 1.0}, {"total": 1001, "passed": 1001}):
                self.assert_invalid(lambda: classify_success(name, output(name, **changes)))
        self.assert_invalid(lambda: classify_success("run_browser_tests", output("run_browser_tests", total=101, passed=101)))

    def test_tool_output_schema_and_extra_final_verdict_are_rejected(self):
        for name in EXECUTION_TOOL_NAMES:
            self.assert_invalid(lambda: classify_success(name, output(name, finalVerdict="PASS")))
            data = output(name)
            data.pop("executionManifestId")
            self.assert_invalid(lambda: classify_success(name, data))
        self.assert_invalid(lambda: classify_success("run_security_scan", output("run_security_scan", findings=True)))
        self.assert_invalid(lambda: classify_success("run_build", {"content": "private-source"}))
        self.assert_invalid(lambda: classify_success("unknown", {}))

    def test_policy_helpers_are_pure_no_files_processes_or_clock(self):
        with patch("builtins.open", side_effect=AssertionError("Host file access")), \
                patch("subprocess.Popen", side_effect=AssertionError("Host execution")), \
                patch("time.time", side_effect=AssertionError("clock access")):
            arguments_sha256("run_build", {"workspaceId": RUN_ID, "snapshotId": RUN_ID})
            classify(failure(tool_code="RESOURCE_BUSY"))
            classify_success("run_build", output("run_build"))


if __name__ == "__main__":
    unittest.main()
