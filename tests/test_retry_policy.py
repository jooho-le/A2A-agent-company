import unittest

from orchestrator.domain import (
    MAX_MCP_TOOL_RETRIES,
    RetryDecision,
    ToolErrorKind,
    decide_tool_retry,
    make_issue_fingerprint,
    next_consecutive_repeat_count,
    requires_human_review,
)


class McpRetryPolicyTests(unittest.TestCase):
    def test_retryable_process_errors_allow_two_retries_only(self) -> None:
        self.assertEqual(
            decide_tool_retry(ToolErrorKind.PROCESS_STARTUP_FAILURE, 0),
            RetryDecision.RETRY,
        )
        self.assertEqual(
            decide_tool_retry(ToolErrorKind.RESOURCE_BUSY, MAX_MCP_TOOL_RETRIES),
            RetryDecision.DO_NOT_RETRY,
        )

    def test_timeout_and_transport_errors_require_known_safe_replay(self) -> None:
        self.assertEqual(
            decide_tool_retry(ToolErrorKind.TOOL_TIMEOUT, 0),
            RetryDecision.INSPECT_STATE,
        )
        self.assertEqual(
            decide_tool_retry(ToolErrorKind.TOOL_TIMEOUT, 0, side_effect_safe=True),
            RetryDecision.RETRY,
        )
        self.assertEqual(
            decide_tool_retry(ToolErrorKind.MCP_TRANSPORT_INTERRUPTED, 0),
            RetryDecision.INSPECT_STATE,
        )
        self.assertEqual(
            decide_tool_retry(
                ToolErrorKind.MCP_TRANSPORT_INTERRUPTED,
                0,
                result_known_not_applied=True,
            ),
            RetryDecision.RETRY,
        )
        self.assertEqual(
            decide_tool_retry(ToolErrorKind.WRITE_RESULT_UNKNOWN, 0),
            RetryDecision.INSPECT_STATE,
        )

    def test_input_policy_and_product_failures_are_not_retried(self) -> None:
        for kind in (
            ToolErrorKind.INPUT_SCHEMA_ERROR,
            ToolErrorKind.PERMISSION_DENIED,
            ToolErrorKind.PATH_TRAVERSAL,
            ToolErrorKind.UNSUPPORTED_TOOL,
            ToolErrorKind.BUILD_CODE_FAILURE,
            ToolErrorKind.QA_ASSERTION_FAILURE,
            ToolErrorKind.SECURITY_FINDING,
        ):
            with self.subTest(kind=kind):
                self.assertEqual(
                    decide_tool_retry(kind, 0), RetryDecision.DO_NOT_RETRY
                )

    def test_retry_count_must_be_non_negative(self) -> None:
        with self.assertRaises(ValueError):
            decide_tool_retry(ToolErrorKind.RESOURCE_BUSY, -1)


class IssueRecurrenceTests(unittest.TestCase):
    def test_fingerprint_is_deterministic_and_uses_each_identity_field(self) -> None:
        original = make_issue_fingerprint("REQ-001", "test-1", "ASSERTION", "api.py:10")
        self.assertEqual(
            original,
            make_issue_fingerprint("REQ-001", "test-1", "ASSERTION", "api.py:10"),
        )
        self.assertNotEqual(
            original,
            make_issue_fingerprint("REQ-002", "test-1", "ASSERTION", "api.py:10"),
        )
        with self.assertRaises(ValueError):
            make_issue_fingerprint("REQ-001", "test-1", " ", "api.py:10")

    def test_same_issue_repeat_escalates_after_two_fix_cycles(self) -> None:
        fingerprint = make_issue_fingerprint("REQ-001", "test-1", "ASSERTION", "api.py:10")
        repeats = next_consecutive_repeat_count(None, 0, fingerprint)
        self.assertFalse(requires_human_review(repeats))

        repeats = next_consecutive_repeat_count(fingerprint, repeats, fingerprint)
        self.assertFalse(requires_human_review(repeats))
        repeats = next_consecutive_repeat_count(fingerprint, repeats, fingerprint)
        self.assertTrue(requires_human_review(repeats))

        changed = next_consecutive_repeat_count("different", repeats, fingerprint)
        self.assertEqual(changed, 0)


if __name__ == "__main__":
    unittest.main()
