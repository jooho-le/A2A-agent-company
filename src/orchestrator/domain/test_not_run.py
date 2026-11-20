"""Not-run test results: closed reason codes shared by services and the verdict.

docs/live-api/05-completion-rules.md section 3 (석민 확정 2026-11-18): a test
that could not run is kept as a declared case with a reason and the Host's
attempt evidence, never dropped and never PASS. The trusted unit runner writes
the runner-side reasons into a SKIP row's ``details``; it is stdlib-only inside
the Container, so it repeats these strings instead of importing them. The
helper-assertion reason means the body started but its assertion could not
evaluate any product condition because it used the Host return type incorrectly.

Only TECHNICAL reasons can end a Run as FINISHED UNVERIFIED once recovery is
spent. A deliberate fixture skip is the test author's choice, like any skip.
"""
import re

SETUP_FAILED = "NOT_RUN_SETUP_FAILED"
SCOPE_FAILED = "NOT_RUN_SCOPE_FAILED"
ASYNC_NOT_AWAITED = "NOT_RUN_ASYNC_NOT_AWAITED"
FIXTURE_SKIPPED = "NOT_RUN_FIXTURE_SKIPPED"
HELPER_ASSERTION = "NOT_RUN_HELPER_ASSERTION"

RUNNER_REASONS = frozenset({SETUP_FAILED, ASYNC_NOT_AWAITED, FIXTURE_SKIPPED, HELPER_ASSERTION})
TECHNICAL = frozenset({SETUP_FAILED, SCOPE_FAILED, ASYNC_NOT_AWAITED, HELPER_ASSERTION})

# Security requirement result code when every non-PASS case is a technical not-run.
SECURITY_TESTS_NOT_RUN = "SECURITY_TESTS_NOT_RUN"

_FIXTURE_ROW = re.compile(
    r"(?:(?:setUpClass|tearDownClass|setUpModule|tearDownModule) \([A-Za-z0-9_.]{1,480}\)"
    r"|setUp \([A-Za-z0-9_.]{1,504}\))")


def is_fixture_row(test_id) -> bool:
    """A unittest preparation/fixture row, which binds to no declared case."""
    return isinstance(test_id, str) and _FIXTURE_ROW.fullmatch(test_id) is not None


def runner_reason(actual):
    """The runner's not-run reason on a reported SKIP row, else None."""
    if actual is None or getattr(actual, "outcome", None) != "SKIP":
        return None
    details = getattr(actual, "details", None)
    return details if details in RUNNER_REASONS else None
