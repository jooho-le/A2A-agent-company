"""Bounded, value-free correction of pre-side-effect model mistakes.

A model response that fails a Host check *before any Tool runs or any result is
published* may receive a fixed feedback message and regenerate, at most
``LLMLimits.max_output_corrections`` times per engine run (default 0 = off) and
never twice for the same mistake. Every regeneration is a normal model call on
the same budget. Single and Multi use the same engine, so the same limit and the
same rules apply to both; the limit is part of ``llmLimits`` and therefore of the
Host policy fingerprint compared by evaluation parity checks.

An incomplete response that used the whole output cap (``max_output_tokens``) is
also pre-side-effect: the Host discards it, including any partial Tool call, so
it gets the same bounded regeneration (``OUTPUT_LIMIT_REACHED``, once per run).
Live Run 4f7ec809 (2026-11-17): every other call stayed under 1,500 output
tokens while one Security final answer ran to the 8,192 cap, i.e. a runaway
answer, not a task that needs more room.

Never correctable: provider/API rejections (an invalid request schema fails the
same way on every retry), refusals, other incomplete responses (for example a
content filter stop below the cap), executed Tool failures, credential findings
outside the existing test-source rule, and genuine semantic-review judgments.
Those keep their original fail-closed handling.

Feedback carries closed Host codes, field pointers and instructions. A verified
router decorator can also include a source line number and a bounded dotted identifier.
"""
import re
from orchestrator.core.security import redact_text

from agents.llm.contracts import LLMErrorCode
from agents.llm.recovery import ReadyPathEvidence, ready_comparison_reason, safe_source_ref

CORRECTABLE_SCHEMA_STAGES = frozenset({'TOOL_ARGUMENTS_WIRE', 'TOOL_ARGUMENTS', 'MODEL_OUTPUT', 'MODEL_OUTPUT_REDACTED'})
CORRECTABLE_POLICY_REASONS = frozenset({'TOOL_NOT_OFFERED', 'WORKSPACE_MISMATCH'})
DRAFT_PRECHECK_POLICY = 'SHARED_INVENTORY_AND_TYPED_SOURCE_REPAIR_V8'

VALIDATION_ERROR_GUIDANCE = (
    'HTTPException does not catch RequestValidationError: FastAPI rejects invalid request bodies before '
    'the signup endpoint runs. Import RequestValidationError from fastapi.exceptions and register its '
    'handler on the approved FastAPI application in repairSourcePath. Keep the existing HTTPException '
    'handler and its business rejection behavior. Changing only that handler, catching inside signup, '
    'or adding Pydantic field validators does not protect this earlier error path. A broad Exception handler '
    'does not replace the framework-specific RequestValidationError handler either. A parameter annotated '
    'dict/list, a Body parameter, and a SecretStr request model still require this protection: a malformed '
    'whole body can be echoed before any endpoint code or field validation runs. '
    'Return a value-free response using the approved validation rejection status and schema. '
    'Do not serialize unfiltered exc.errors(), exc.body, str(exc), or a request body containing credentials, '
    'and do not log those values. Do not import main.app into a router or from main itself. '
    'This is public framework API guidance, not a measured finding, hidden test or permission to change requirements.')

INSTRUCTIONS = {
    'READY_SERVICE_INTERFACE_REVIEW': (
        'The Host read your service source and found the listed startup, dependency or interface issues. '
        'Read each sourcePath at source/<sourcePath>, fix the actual reasonCode and return READY again. '
        'Follow the shared serviceDesign: API work needs its API and readiness response, not a new HTML UI. '
        'Preserve main.app, lifespan, approved dependencies and the original database connection. '
        'Do not change user requirements or tests to hide defects. This static check is not a PASS verdict.'),
    'READY_SIGNUP_QUALITY_REVIEW': (
        'The Host found reviewable source paths against the public signup contract; these are static warnings, '
        'not measured security findings or proof of product success. Read the listed sourcePath and lineNumber. '
        'sourcePath is repository-relative; read it using fileAccess coordinates such as source/<sourcePath>, '
        'never a Host absolute path. '
        'repairSourcePath is the file to edit; it may differ from sourcePath where the risk was observed. '
        'For default request validation, install protection on the FastAPI app in that entrypoint; '
        'do not import main.app back into a router or import main from itself. The Host requires a fresh '
        'read and a changed source write at each repair target before accepting another READY. '
        'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS / VALIDATION_ERRORS_FORWARD_INPUT: protect request validation '
        'errors before the endpoint runs, including dict/list or Body parameters and SecretStr models. '
        'HTTPException or a broad Exception handler alone does not override RequestValidationError. '
        'Do not return exc.errors() with its input fields, exc.body, or raw request bodies. '
        'RAW_EXCEPTION_IN_ERROR_RESPONSE / EXCEPTION_LOG_MAY_EXPOSE_DB_PARAMETERS: inspect database exceptions, '
        'SQL parameters and exc_info/tracebacks in responses and logs. Use value-free error codes/exception types '
        'rather than exception strings or credential values. A registered logging filter alone does not prove '
        'traceback or cached exception text is scrubbed. DB_ERROR_PATH_NOT_HANDLED: an IntegrityError handler '
        'covers duplicates, not other SQLAlchemy failures. Keep its duplicate rejection, add rollback and a '
        'value-free server-error response for other SQLAlchemy errors at the commit boundary, and prevent '
        'uncaught database exceptions from reaching framework traceback logs. Configure create_engine with '
        'hide_parameters=True as defense in depth; it does not replace safe error handling. Do not disable '
        'all logging, suppress real failures, or change test criteria. Register handlers on the application, not APIRouter. '
        'EMAIL_VALIDATED_BEFORE_CANONICAL_TRIM: apply the approved emailPolicy before validating; use the same '
        'canonical value for lookup, insert and UNIQUE enforcement. Do not invent dot/plus alias removal. '
        'COMMIT_CONFLICT_MAPPED_TO_SERVER_ERROR: a duplicate may occur after the initial lookup during commit. '
        'Handle the database uniqueness conflict with rollback and a public-contract rejection, not 500; preserve '
        'genuine unrelated storage errors and do not label every exception a duplicate. Keep the source/build '
        'contract and all test criteria. Inspect all listed paths and make the necessary source changes before READY.'),
    'READY_OUTPUT_SAFETY_REVIEW': (
        'The Host found a simple FastAPI signup request model that can use default validation errors containing '
        'the submitted input. Review the frozen non-exposure criterion before READY. Protect errors raised before '
        'the signup function, as well as normal responses and database errors. Use an appropriate application-level '
        'validation handler or equivalent safe request handling; never serialize the full exception, exc.errors() '
        'with input values, or the submitted body. Preserve approved error behavior and the signup interface. '
        'This is a source-risk correction, not a proven product verdict or permission to change tests or requirements.'),
    'DEVELOPER_REJECTION_RECOVERABLE': (
        'For NO_CHANGE_REQUIRED the Host found unchanged source with a still-measured failure on that same candidate. '
        'Use the active fixRequest, not rejectedCandidateFixRequest history. Repair the measured issue with a minimal edit; '
        'a passing Build does not resolve QA or Security failures. '
        'The Host reviewed your explained rejection. The Host, not Developer, runs Build after READY. '
        'requirementScope is the authenticated assignment. Use the existing requirements, allowed tools and '
        'settled tool results to finish the assigned implementation if possible. Do not replay successful writes. '
        'READY submits actual written work for Host verification; it does not claim Build or product success. '
        'If implementation remains impossible, return REJECTED with its precise reasonCode, explanation and '
        'requirementIds. Genuine policy or requirement conflicts must remain explicit; do not relabel them as '
        'technical inability. Do not invent new requirements, permissions, evidence or tools.'),
    'TOOL_ARGUMENTS_INVALID': (
        'No tool call from your previous response was executed. The listed argument fields did not match the '
        'tool input schema. Re-issue only the tool calls you still need, with valid arguments. File tool paths '
        'use the workspace coordinates in fileAccess (the toolPath values such as source/main.py), not '
        'repository-relative paths.'),
    'TOOL_REQUEST_REJECTED': (
        'No tool call from your previous response was executed. Use only the tools offered in this step and the '
        'current workspaceId, then re-issue the tool calls you still need.'),
    'MODEL_OUTPUT_INVALID': (
        'Your final JSON did not match the required output schema at the listed fields. Return a corrected final '
        'JSON. Tool results you already received remain valid; do not repeat writes that already succeeded. Do '
        'not include credential-like literals.'),
    'OUTPUT_CONTRACT_INVALID': (
        'REJECTED must have empty cases and questions. INPUT_REQUIRED must have empty cases and nonempty questions. '
        'The Host contract check rejected your final JSON for the listed reasons. Return a corrected final JSON '
        'without changing the approved requirement IDs, scope or policies. TEXT_CONTROL_CHARACTER means the '
        'listed text field contains an invisible control character (for example U+0000, a tab or a newline) '
        'that text fields do not allow. Where a field must equal an acceptanceCriteria sentence (such as '
        'expectedResult), copy that sentence exactly, character by character, including the middle dot (·).'
        ' Other codes: ASSIGNED_REQUIREMENT_WITHOUT_CASE / ASSIGNED_REQUIREMENT_WITHOUT_REVIEW - add a case / '
        'review for every assigned requirement; REQUIREMENT_NOT_ASSIGNED / CASE_REQUIREMENT_NOT_ASSIGNED - use only '
        'assigned requirement IDs; REFERENCE_PATH_NOT_MEASURED / REFERENCE_LINES_INVALID - cite measured source '
        'paths with 1 <= startLine <= endLine; REFERENCES_REQUIRED_FOR_OUTCOME - a PASS/FAIL (CONFIRMED/'
        'FALSE_POSITIVE) proposal needs at least one reference; SELECTOR_NOT_ALLOWED - use only the offered '
        'selectors; QUESTIONS_NOT_ALLOWED_WHEN_READY - READY has no questions; TEXT_LOOKS_LIKE_SECRET - remove '
        'credential-like text.'),
    'CASE_EXPECTATION_INVALID': (
        'Your generated test cases were rejected before running. Each case must set expectedResult to one '
        'acceptanceCriteria sentence of its own assigned requirement, copied exactly (character by character, '
        'including spaces, commas and the middle dot ·), and the cases together must cover every assigned '
        'acceptance sentence in each assigned requirement. Fix the listed fields and return the corrected final JSON; keep the test file you wrote '
        'unless a listed case needs a different test.'),
    'GENERATED_TESTS_INVALID': (
        'Your generated tests do not match your cases yet; nothing has run. GENERATED_TEST_FILE_MISSING: write '
        'the test file for each generated test scope with write_test_file (use testTargets) before READY. '
        'This includes every required browser suite JSON, even when all signup cases use unit tests. '
        'BROWSER_SUITE_INVALID: write valid browser-suite-v1 JSON using only testTargets.allowedActions. '
        'Declare every browser testId under its exact run_browser_tests selector. '
        'BROWSER_SUITE_PATH_INVALID: use only the approved testTargets path inside your QA scratch; never symlinks. '
        'TEST_IN_FILE_NOT_DECLARED: every test method in your test files is run and must be declared as one case '
        'whose testId is module.Class.method exactly; declare it or remove the extra method. '
        'DECLARED_TEST_NOT_IN_FILE: choose a matching selector and exact testId from hostTestIdCatalog; '
        'never guess or add a tests. prefix. Do not remove required coverage. '
        'GENERATED_TEST_SYNTAX_INVALID: a test file is not valid Python; rewrite it. Never place an actual '
        'NUL byte in source. Construct control-character request data at execution time, for example chr(0), '
        'while preserving the negative test. GENERATED_TEST_NONE: the '
        'scope has no test method; write the declared tests. TEST_CLIENT_NOT_STARTED: open the app only as '
        '"with TestClient(app) as client:" so its lifespan creates the tables, as tests/test_health.py does; a '
        'TestClient made without with fails even on a correct product. ASYNC_TEST_IN_SYNC_CASE: a unittest.TestCase '
        'never awaits async def tests, so they pass without running; use def. TEST_IMPORT_NAME_MISSING: a test imports '
        'a name the product module does not define (database.get_db is not in main); import each name from the '
        'module that defines it. TEST_HOST_FIXTURE_REQUIRED: for SCN-001 subclass the Host AppTestCase '
        'from _host_app_test, use self.client/self.db/self.User in test methods, and remove product imports '
        'at module level and custom setup/teardown. The Host handles isolated DB and lifespan; it does not '
        'create missing product tables. TEST_HOST_MEMBER_MISSING: self.fresh_email and self.new_password are not '
        'Host members; create a local fresh_email (for example with uuid4) and import new_password from '
        '_host_test_data. Respect explicitly defined custom helpers. TEST_HOST_RETURN_MISUSED: '
        'signup_email_state returns a dict; read totalRows/exactEmailRows/hasDirectEmailUniqueKey by key, '
        'never as unpacked numeric row values. Use testTargets.appFixture.methods for actual signatures '
        'and return types. concurrent_signup_statuses returns two integer HTTP codes; an individual code '
        'cannot be the container argument of assertIn. TEST_HOST_ARGUMENT_INVALID: use the exact parameter '
        'names and keywordOnly arguments in appFixture.methods; do not pass keyword-only values positionally. '
        'The password keyword requires the '
        'runtime synthetic string itself, not [sample], a tuple or a dict. check_signup_output requires '
        'a string of at least 8 characters generated inside the current test body and present in the body. '
        'Keep short/missing-password negative cases using self.client.post and their normal response/DB '
        'assertions; do not remove them. TEST_HOST_RESULT_KEY_UNKNOWN: use only the helper result keys in '
        'testTargets.appFixture.methods; hash parameters expose argon2id/verified/memoryKiB/iterations/'
        'parallelism and optional distinctSalts, not algorithm or memory_cost. '
        'TEST_HOST_OPTIONAL_RESULT_UNAVAILABLE: each hash result is a snapshot. The first return has no '
        'distinctSalts and does not update after a later observation. Observe two different committed accounts '
        'in one test and assert second_params["distinctSalts"] on the SECOND return, never first_params. '
        'Do not use a default True, skip, constant or removed assertion. Check numeric hash settings with >= '
        'the public minimums, not exact equality. Keep all assigned expectations. '
        'Then return READY.'),
    'CODE_REFERENCE_INVALID': (
        'A code reference in your review cannot be anchored yet. REFERENCE_FILE_NOT_READ: read that file with '
        'read_project_file in this step before citing it. When sourceReferenceCatalog is present, find the '
        'referenced anchorId there to identify its file; keep only IDs from that Snapshot catalog. '
        'REFERENCE_PAST_END_OF_FILE: cite only lines that exist '
        'in the file you read. Fix the listed references and return the corrected final JSON.'),
    'QUESTIONS_NOT_ANSWERED': (
        'No human will answer questions in this run. Do not return INPUT_REQUIRED for points you can decide. '
        'The scenario contract already fixes externalContract (file locations, signup path, request fields), '
        'securityPolicy and emailPolicy; use them as the answers. Decide remaining implementation details '
        'yourself within those rules (file layout, response body shape, error messages, column lengths) and '
        'continue the task with the tools offered in this step. Return INPUT_REQUIRED again only if two '
        'requirements truly contradict each other.'),
    'READY_WITHOUT_CHANGES': (
        'You returned READY, but the Host found no qualifying change from the working baseline. For a measured '
        'runtime defect, editing only ordinary Python comments does not repair the failing behavior. '
        'Use the failing observations to change the implementation. If you have not yet read, in this step, '
        'the source file you must change, your next response must be exactly one read_project_file call of that '
        'file (the only offered tool on that turn); the Host then requires the change. Otherwise your next '
        'response must make exactly one concrete source correction with one tool call: edit_source_file (when '
        'offered; replace one exact fragment of a file you already read, preferred for a small repair) or '
        'write_source_file (whole file), using the supplied task, feedback and file contents already read. Do not '
        'return READY or questions in that response. After the Host confirms a real change, normal tool use resumes '
        'and you may return READY when the implementation is complete. Existing fixRequest and '
        'buildDiagnostics failures remain unresolved until the changed candidate is validated.'),
    'OUTPUT_LIMIT_REACHED': (
        'Your previous response reached the output token limit and was discarded; no tool call from it was '
        'executed. Respond again within the limit: keep every required item (one review per assigned requirement '
        'and at least one test case per assigned requirement), but make each text value short. Never repeat the '
        'same item, sentence or code block, and never emit NUL or other control characters. Choose '
        'expectedResult values from the allowed sentences exactly as given. If you were writing files, write '
        'only one or two files in this response and continue the remaining files in the following responses; '
        'never drop required code to make a file shorter.'),
    'READY_PRIORITY_REPAIR_NEEDED': (
        'The current source diff has no linked edit on the measured priority path. If blockingPrerequisite is '
        'present, start with its failing normal request and inspect the observed application frame. Otherwise '
        'start with the measured path that showed no progress. Edit the producer in the working source. '
        'A wording change will not count as implementation progress. If no safe edit can be established, submit '
        'an accurate partial READY for build and revalidation; do not invent a sourceRef.'),
    'READY_RUNTIME_REPAIR_REQUIRED': (
        'The measured normal request still has no supported source edit. Focus this repair on the observed '
        'failure first. Inspect the failing operation and its dependencies, then edit the implementation that '
        'can change that outcome. Edits to other paths are preserved. If you cannot establish a repair, return '
        'a truthful partial READY with edit and sourceRef null for this path and mark it open for Host testing. '
        'Rewording the same claim or citing another unrelated edit does not establish a repair.'),
    'READY_LIBRARY_TYPE_MISMATCH': (
        'The Host matched a call in the submitted source to its installed library contract. '
        'At the indicated file and line the argument has actualType, while the parameter requires requiredType. '
        'Inspect that call and change the source before submitting READY. Changing the wording of READY '
        'or supplying a different literal of the same incompatible type leaves the issue open. '
        'This is the only library-type correction in this stage. Another READY with a definite library-type '
        'mismatch stops the stage even if correctionsRemaining is positive for other issues.'),
    'READY_PATH_EVIDENCE_MISMATCH': (
        'The Host compared the submitted source against the working baseline. Follow each issue’s action. '
        'For CORRECT_CITATION, check candidateRefs and cite the matching changed location. For '
        'EDIT_SOURCE_OR_REPORT_OPEN, the Host could not link a change to this path. changedRefs, when present, '
        'lists observed changes only; those entries alone do not establish a link. '
        'Use the measured failure in activeRepairFocus to edit the code that produces the result. '
        'If a path remains unaddressed or its link is uncertain, set both edit and sourceRef to null for that path, '
        'leave it open in recheck, and retain supported edits for the other paths. Host rechecks decide the outcome.'),
    'START_CONTRACT_PRECHECK_FAILED': (
        'You returned READY, but the Host read your source files (without running them) and found start-contract '
        'problems at the listed files. The Host build would fail for exactly these reasons. Read the listed files, '
        'fix them with write_source_file and return READY again. Keep externalContract: the FastAPI instance '
        'main.app, the synchronous database.get_db dependency and the SQLAlchemy model models.User with a primary '
        'key column; rewrite an existing starting file only together with what it already provides. '
        'READY_ROUTE_MISSING: keep the starting code\'s ready route (GET / answering 2xx) on main.app. '
        'READY_ROUTE_SHADOWED: an earlier mount at / captures the ready request before its handler and the '
        'known child application has no GET / route. Preserve the ready handler before the catch-all mount, '
        'or include a compatible APIRouter on the main application; keep all existing signup routes, startup '
        'initialization and error handling. A ready decorator hidden behind the mount is not sufficient. '
        'DEPENDS_TARGET_INVALID: pass the dependency function itself, e.g. Depends(get_db), never a string. '
        'SIGNUP_ROUTE_MISSING: implement the signupApi route (method and path in externalContract) on main.app or '
        'on a router included in it.'),
    'PROVIDER_TRANSIENT_RETRY': (
        'Not sent to the model: the Host re-sends the same request once after a provider failure with no model '
        'output.'),
    'SEMANTIC_REVIEW_FORMAT_INVALID': (
        'Your review was rejected for format or evidence-reference reasons, not for its judgment. Return a '
        'corrected review: review every listed field, copy candidateSha256 and basisSha256 exactly, and cite only '
        'evidence IDs that evidenceCatalog lists for the cited sourceRef.'),
}

_CODE = re.compile(r'^[A-Z][A-Z0-9_]{2,63}$')
_POINTER = re.compile(r'^[A-Za-z0-9_/.\-]{0,256}$')
_REQUIREMENT_KEY = re.compile(r'^REQ-[0-9]{3,}$')
MAX_ISSUES = 16


class CorrectionAllowance:
    """Per engine run: total cap plus 'same mistake only once'."""

    def __init__(self, limit: int):
        self.remaining = limit if type(limit) is int and limit > 0 else 0
        self.enabled = self.remaining > 0
        self.last_reason = None
        self._seen = set()

    def take(self, signature) -> bool:
        self.last_reason = ('DISABLED' if not self.enabled else 'REPEATED' if signature in self._seen
                            else 'LIMIT_REACHED' if self.remaining <= 0 else 'ALLOWED')
        if self.last_reason != 'ALLOWED':
            return False
        self.remaining -= 1
        self._seen.add(signature)
        return True


def classify(error) -> str | None:
    """Correction kind for a pre-execution LLMRuntimeError, or None (fail closed)."""
    report = getattr(error, 'validation', None)
    if error.code in (LLMErrorCode.SCHEMA, LLMErrorCode.RESPONSE) and getattr(report, 'stage', None) in CORRECTABLE_SCHEMA_STAGES:
        return 'TOOL_ARGUMENTS_INVALID' if report.stage.startswith('TOOL_') else 'MODEL_OUTPUT_INVALID'
    if error.code is LLMErrorCode.TOOL_POLICY and getattr(report, 'reason_code', None) in CORRECTABLE_POLICY_REASONS:
        return 'TOOL_REQUEST_REJECTED'
    return None


def _issue(item, *, start_precheck=False) -> dict | None:
    if not isinstance(item, dict):
        return None
    pointer = item.get('fieldPath', item.get('fieldPointer', ''))
    code = item.get('reasonCode')
    if not isinstance(pointer, str) or not _POINTER.match(pointer) or not isinstance(code, str) or not _CODE.match(code):
        return None
    issue = {'fieldPath': pointer, 'reasonCode': code}
    if start_precheck and code == 'APIROUTER_EXCEPTION_HANDLER_UNAVAILABLE':
        line, expression = item.get('lineNumber'), item.get('sourceExpression')
        if (type(line) is int and 0 < line <= 1048576 and isinstance(expression, str)
                and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}\.exception_handler', expression)
                and redact_text(expression) == expression):
            issue.update(lineNumber=line, sourceExpression=expression)
        if item.get('handlerReturnShape') == 'ERRORS_CALL_IN_JSONRESPONSE_CONTENT':
            issue['handlerReturnShape'] = 'ERRORS_CALL_IN_JSONRESPONSE_CONTENT'
    source_path, line = item.get('sourcePath'), item.get('lineNumber')
    if (isinstance(source_path, str) and 0 < len(source_path) <= 256
            and re.fullmatch(r'[A-Za-z0-9_./-]+\.py', source_path)
            and not source_path.startswith('/') and all(p not in ('', '.', '..') for p in source_path.split('/'))
            and redact_text(source_path) == source_path and type(line) is int and 0 < line <= 1048576):
        issue.update(sourcePath=source_path, lineNumber=line)
    repair_path = item.get('repairSourcePath')
    if _safe_review_file(repair_path):
        issue['repairSourcePath'] = repair_path
    revision = item.get('reviewRevision')
    if isinstance(revision, str) and re.fullmatch('[a-f0-9]{64}', revision):
        issue['reviewRevision'] = revision
    if code in ('DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS', 'VALIDATION_ERRORS_FORWARD_INPUT'):
        context = _validation_error_context(item.get('validationErrorContext'))
        if context is not None:
            issue['validationErrorContext'] = context
    if code == 'EMAIL_VALIDATED_BEFORE_CANONICAL_TRIM':
        from agents.runtime.email_normalization import valid_context
        context = item.get('emailNormalizationContext')
        if valid_context(context):
            issue['emailNormalizationContext'] = {k: context[k] for k in (
                'sourceRef', 'parser', 'trimBeforeValidationObserved')}
    key = item.get('requirementKey')
    if isinstance(key, str) and _REQUIREMENT_KEY.match(key):
        issue['requirementKey'] = key
    return issue


def _safe_review_file(path):
    return (isinstance(path, str) and 0 < len(path) <= 256
            and re.fullmatch(r'[A-Za-z0-9_./-]+\.py', path) is not None
            and not path.startswith('/') and all(p not in ('', '.', '..') for p in path.split('/'))
            and redact_text(path) == path)


def source_review_paths(issues):
    """Only files from the trusted checker, still subject to MCP ACL and CAS."""
    return tuple(dict.fromkeys('source/' + path for row in issues
                              if _safe_review_file(path := row.get('repairSourcePath', row.get('sourcePath')))))


def _validation_error_context(value):
    """Closed Host observations only; no model explanations or source body."""
    if not isinstance(value, dict):
        return None
    name = value.get('applicationAttribute')
    lines = value.get('httpExceptionHandlerLines')
    if (not isinstance(name, str) or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name) is None
            or redact_text(name) != name
            or type(value.get('httpExceptionHandlerDetected')) is not bool
            or type(value.get('requestValidationHandlerDetected')) is not bool
            or not isinstance(lines, list) or len(lines) > 8
            or any(type(line) is not int or not 0 < line <= 1048576 for line in lines)):
        return None
    return {'applicationAttribute': name, 'httpExceptionHandlerDetected': value['httpExceptionHandlerDetected'],
            'requestValidationHandlerDetected': value['requestValidationHandlerDetected'],
            'httpExceptionHandlerLines': sorted(set(lines))}


def validation_error_repair_targets(issues):
    targets = []
    for row in issues:
        if row.get('reasonCode') not in ('DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS', 'VALIDATION_ERRORS_FORWARD_INPUT'):
            continue
        context = _validation_error_context(row.get('validationErrorContext'))
        path = row.get('repairSourcePath')
        if context is None or not _safe_review_file(path):
            continue
        target = {'toolPath': 'source/' + path, 'applicationAttribute': context['applicationAttribute'],
                  'exceptionType': 'fastapi.exceptions.RequestValidationError',
                  'importExample': 'from fastapi.exceptions import RequestValidationError',
                  'registrationExample': '@' + context['applicationAttribute'] + '.exception_handler(RequestValidationError)',
                  'preserveExistingHttpExceptionHandler': context['httpExceptionHandlerDetected'],
                  'observed': context}
        if target not in targets:
            targets.append(target)
    return targets[:MAX_ISSUES]


def report_issues(report) -> list[dict]:
    """Closed issues from an LLM validation/policy report (both are value-free models)."""
    try:
        public = report.public()
    except Exception:
        return []
    if 'issues' in public:
        found = public['issues']
    else:  # policy report: one reason at one field
        found = [{'fieldPath': public.get('fieldPath', ''), 'reasonCode': public.get('reasonCode')}]
    return [issue for issue in (_issue(item) for item in found[:MAX_ISSUES]) if issue is not None]


def signature(kind: str, stage, tool_name, issues: list[dict], fallback=None):
    # Reordering or duplicating the same issues cannot buy another correction.
    # ``fallback`` (a digest of the rejected output) applies only when the Host
    # found no field to name: two different fieldless rejections are different
    # mistakes, the very same output twice is the same one (Codex 2026-11-17 #1).
    named = tuple(sorted({(item['fieldPath'], item['reasonCode'], item.get('requirementKey', '')) for item in issues}))
    return (kind, stage, tool_name, named, None if named else fallback)


UNEXPLAINED_KINDS = frozenset({'OUTPUT_CONTRACT_INVALID'})


def output_digest(text) -> str | None:
    """Digest used only inside a fieldless rejection's signature; never sent anywhere."""
    if not isinstance(text, str):
        return None
    import hashlib
    return hashlib.sha256(text.encode('utf-8', 'surrogatepass')).hexdigest()


def _baseline_path_observation(path, brief):
    """Echo only closed measurements for this path; never send source, test inputs or output bodies."""
    if path == 'INVALID_INPUT_SERVER_ERROR':
        group = brief.get('invalidInputServerError') or {}
        statuses = []
        for row in group.get('failedCases', ())[:16]:
            value, origin = row.get('httpStatus'), row.get('statusOrigin')
            if type(value) is not int or not 500 <= value < 600:
                continue
            observed = {'recordedStatus': value, 'statusOrigin': origin if origin in
                        ('HTTP_RESPONSE', 'ESCAPED_EXCEPTION') else 'UNKNOWN'}
            case = row.get('caseId')
            if isinstance(case, str) and re.fullmatch(r'invalid_email(?:_\d+)?|step_\d+_invalid', case):
                observed['caseId'] = case
            if observed not in statuses:
                statuses.append(observed)
        if statuses:
            observation = {'statusEvidence': statuses}
            # These frames belong to related executions. The QA case itself may have no stack.
            sites = [{'sourceRef': s['sourceRef'], 'line': s['line']} for s in group.get('sourceSites', ())[:3]
                     if safe_source_ref(s.get('sourceRef')) and type(s.get('line')) is int and 0 < s['line'] <= 1048576]
            if sites:
                observation['relatedExceptionSites'] = sites
            # These flags come from another named case; do not assign them to the failed QA requests.
            related = [{'caseId': row['caseId'], 'responseStarted': row['responseStarted'],
                        'escapedToClient': row['escapedToClient']} for row in group.get('relatedExceptions', ())[:8]
                       if isinstance(row.get('caseId'), str)
                       and re.fullmatch(r'invalid_email(?:_\d+)?|step_\d+_invalid', row['caseId'])
                       and type(row.get('responseStarted')) is bool and row.get('escapedToClient') is True]
            if related:
                observation['relatedEscapedExceptions'] = related
            return observation
    if path == 'RESPONSE_BEFORE_SIGNUP_HANDLER':
        for target in brief.get('targets', ()):
            for check in target.get('checks', ()):
                cases = check.get('observedFailurePaths', {}).get(path, ())
                for case in cases:
                    row = check.get('caseFeedback', {}).get(case, {})
                    if row.get('handlerEntry') == 'NOT_ENTERED' or (
                            case == 'REJECTION_RESPONSE' and check.get('outputChecks', {}).get(case) == 'EXPOSED'):
                        return {'handlerEntry': 'NOT_ENTERED'}
    return None


def ready_path_trace(feedback):
    """Only structured source evidence may enter the long-lived recovery event."""
    records = []
    for row in feedback.get('issues', ()):
        if row.get('reasonCode') != 'NO_LINKED_CODE_CHANGE' or 'comparisonReason' not in row:
            continue
        parts = row['fieldPath'].split('/')
        if len(parts) != 4 or parts[1] != 'repairPaths' or parts[3] != 'sourceRef':
            continue
        data = {key: row[key] for key in ReadyPathEvidence.model_fields if key in row}
        data.update(path=parts[2], changedRefs=row.get('changedRefs', []))
        records.append(ReadyPathEvidence.model_validate(data))
    return tuple(records)


def feedback(kind: str, *, issues: list[dict], remaining: int, stage=None, tool_name=None, repair_brief=None) -> dict:
    reasons = {item['reasonCode'] for item in issues}
    instruction = INSTRUCTIONS[kind]
    if kind == 'READY_SERVICE_INTERFACE_REVIEW':
        if 'WEB_UI_NOT_IMPLEMENTED' in reasons:
            instruction += ' The requested WEB delivery is missing its usable local HTML/CSS/JavaScript screen. Implement it and connect its actions to the real application.'
        if 'SERVICE_DEPENDENCY_UNAVAILABLE' in reasons:
            instruction += ' An import or parser is absent from runtimeCapabilities. Use approved installed packages; editing the dependency lock or adding a screen cannot install it.'
        if 'MODEL_PRIMARY_KEY_MISSING' in reasons:
            instruction += ' The reported SQLAlchemy model lacks a primary key and fails while being imported. Define an appropriate primary key on that model, preserving its existing fields and constraints. An engine import or create_tables() function cannot fix mapper configuration.'
    if kind == 'GENERATED_TESTS_INVALID' and stage == 'TOOL_ARGUMENTS':
        instruction += (' No tool call in this rejected batch was executed. Previously successful writes remain. '
                        'Fix all listed source issues together and reissue only the still-needed calls. '
                        'Source has not been automatically escaped, normalized or modified by the Host.')
    if (kind in ('READY_SIGNUP_QUALITY_REVIEW', 'READY_OUTPUT_SAFETY_REVIEW')
            and 'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS' in reasons):
        instruction += ' ' + VALIDATION_ERROR_GUIDANCE
    if kind in ('READY_PATH_EVIDENCE_MISMATCH', 'READY_RUNTIME_REPAIR_REQUIRED'):
        issues = [dict(row) for row in issues]
        for row in issues:
            if row['reasonCode'] != 'NO_LINKED_CODE_CHANGE':
                continue
            row['action'] = 'CORRECT_CITATION' if row.get('candidateRefs') else 'EDIT_SOURCE_OR_REPORT_OPEN'
            pointer = row['fieldPath'].split('/')
            path = pointer[2] if len(pointer) == 4 and pointer[1] == 'repairPaths' else None
            row['comparisonReason'] = ready_comparison_reason(path, row.get('candidateRefs'))
            if isinstance(repair_brief, dict) and path:
                observation = _baseline_path_observation(path, repair_brief)
                if observation:
                    row['observedOnBaseline'] = observation
        if kind == 'READY_RUNTIME_REPAIR_REQUIRED' and any(row.get('action') for row in issues):
            instruction += ' ' + INSTRUCTIONS['READY_PATH_EVIDENCE_MISMATCH']
        if issues and all(row.get('action') == 'CORRECT_CITATION' for row in issues):
            instruction = ('The Host found changed code for every listed path. Check candidateRefs against '
                           'your edit and correct the READY sourceRef and explanation, or set edit and sourceRef '
                           'to null and keep the path open in recheck. No new source edit is required to fix a citation.')
        observations = [row.get('observedOnBaseline', {}) for row in issues]
        if any(row.get('unchangedRelatedFunctions') or row.get('unchangedRouteFunctions') for row in issues):
            instruction += (' The listed unchangedRelatedFunctions and unchangedRouteFunctions are unchanged '
                            'in the submitted source, including the order of their operations. Trace the cause '
                            'on each affected path. A related frame belongs only to its observed case.')
        if any(row.get('changedExceptionDecorators') for row in issues):
            instruction += (' changedExceptionDecorators lists edited functions whose decorator names Exception. '
                            'That registration alone does not show which handler produced the earlier response.')
        if any(obs.get('statusEvidence') for obs in observations):
            instruction += (' statusOrigin distinguishes a received HTTP response from an escaped exception '
                            'recorded as 500. UNKNOWN means the prior execution did not establish the origin.')
        if any(obs.get('handlerEntry') == 'NOT_ENTERED' for obs in observations):
            instruction += (' For cases marked NOT_ENTERED, the request did not reach the signup handler. '
                            'Changing only code that runs inside it cannot change that earlier response.')
        if any(obs.get('relatedExceptionSites') for obs in observations):
            instruction += (' relatedExceptionSites are frames from related requests on the baseline; '
                            'they do not establish where every failing request raised.')
    if kind == 'READY_WITHOUT_CHANGES' and 'UNUSED_TEXT_REWRITE' in reasons:
        instruction = ('The only executable edit changes a string substitution assigned to a local value that '
                       'is never returned or logged. The handler still produces the same response. ' + instruction)
    if kind == 'START_CONTRACT_PRECHECK_FAILED' and 'APIROUTER_EXCEPTION_HANDLER_UNAVAILABLE' in reasons:
        instruction += ' ' + _router_handler_guidance(issues)
    if kind == 'START_CONTRACT_PRECHECK_FAILED' and 'ENTRYPOINT_SELF_IMPORT' in reasons:
        instruction += (' The entry module imports its own not-yet-defined binding. Preserve the existing '
                        'FastAPI app in the entrypoint; do not import main.app from main itself or introduce '
                        'a router-to-entrypoint circular import. Host Build will actually import and start the app.')
    # One bounded correction may contain independent draft defects. Explain
    # every included class without copying model text or adding retry budgets.
    supplements = []
    if reasons & {'REFERENCE_FILE_NOT_READ', 'REFERENCE_PAST_END_OF_FILE'}:
        supplements.append('CODE_REFERENCE_INVALID')
    if any(reason.startswith(('GENERATED_TEST_', 'TEST_', 'DECLARED_TEST_', 'ASYNC_TEST_')) for reason in reasons):
        supplements.append('GENERATED_TESTS_INVALID')
    if (any(reason.startswith(('CASE_EXPECTATION_', 'EXPECTED_RESULT_')) for reason in reasons)
            or reasons & {'CASE_COUNT_INVALID', 'CASE_REQUIREMENT_NOT_ASSIGNED', 'ASSIGNED_REQUIREMENT_WITHOUT_CASE', 'ACCEPTANCE_CRITERION_WITHOUT_CASE'}):
        supplements.append('CASE_EXPECTATION_INVALID')
    for supplement in supplements:
        if supplement != kind:
            instruction += ' Also address these independent Host checks: ' + INSTRUCTIONS[supplement]
    body = {'code': kind, 'executed': False, 'issues': issues[:MAX_ISSUES], 'correctionsRemaining': remaining,
            'instruction': instruction}
    if kind == 'READY_SIGNUP_QUALITY_REVIEW':
        from agents.runtime.email_normalization import repair_targets as email_targets
        targets = validation_error_repair_targets(issues) + email_targets(issues)
        if targets:
            body['sourceRepairTargets'] = targets
    if stage is not None:
        body['stage'] = stage
    if tool_name is not None:
        body['toolName'] = tool_name
    return {'hostCorrection': body}


def _router_handler_guidance(issues):
    text = ('The identified decorator runs when the module is imported. Its receiver is a standard FastAPI APIRouter, '
            'which has no exception_handler method in this runtime. Inspect the application entrypoint in externalContract '
            'and the reported registration before READY. Changing only the decorated function body leaves the startup error in place.')
    if any(i.get('handlerReturnShape') == 'ERRORS_CALL_IN_JSONRESPONSE_CONTENT' for i in issues):
        text += (' The handler also passes the result of errors() directly into JSONResponse content. '
                 'Validation errors can contain original inputs. Review the public response against the task requirements.')
    return text


def wrote_handler_registration(tool_name, arguments):
    # A cheap filter for writes worth inspecting. Only the Host AST check can report a finding.
    field = {'write_source_file': 'content', 'edit_source_file': 'newText'}.get(tool_name)
    return bool(field and isinstance(arguments.get(field), str) and 'exception_handler' in arguments[field]
                and isinstance(arguments.get('path'), str) and arguments['path'].endswith('.py'))


def source_write_notice(precheck, paths):
    """Inspect the settled batch. Only bounded findings about files just written enter the transcript."""
    if not precheck or not paths:
        return None
    try:
        issues = [_issue(row, start_precheck=True) for row in precheck()
                  if row.get('fieldPath') in paths and row.get('reasonCode') == 'APIROUTER_EXCEPTION_HANDLER_UNAVAILABLE']
    except Exception:
        return None  # An optional observation cannot replace READY or the frozen Host build.
    issues = [issue for issue in issues if issue is not None][:MAX_ISSUES]
    if issues:
        return {'hostSourceCheck': {'issues': issues, 'instruction': _router_handler_guidance(issues)}}
    return None


def checked_rejection(value) -> dict | None:
    """Normalize an ``output_check`` result; anything malformed means 'no correction'."""
    if not isinstance(value, dict):
        return None
    code = value.get('code')
    if code not in ('OUTPUT_CONTRACT_INVALID', 'SEMANTIC_REVIEW_FORMAT_INVALID', 'QUESTIONS_NOT_ANSWERED',
                    'READY_WITHOUT_CHANGES', 'START_CONTRACT_PRECHECK_FAILED', 'CASE_EXPECTATION_INVALID',
                    'GENERATED_TESTS_INVALID', 'CODE_REFERENCE_INVALID', 'READY_PATH_EVIDENCE_MISMATCH', 'READY_PRIORITY_REPAIR_NEEDED',
                    'READY_RUNTIME_REPAIR_REQUIRED', 'READY_LIBRARY_TYPE_MISMATCH', 'READY_OUTPUT_SAFETY_REVIEW',
                    'READY_SIGNUP_QUALITY_REVIEW', 'READY_SERVICE_INTERFACE_REVIEW'):
        return None
    raw = value.get('issues') or []
    issues = []
    if isinstance(raw, list):
        for item in raw[:MAX_ISSUES]:
            issue = _issue(item, start_precheck=code == 'START_CONTRACT_PRECHECK_FAILED')
            if issue is not None:
                if code == 'READY_LIBRARY_TYPE_MISMATCH':
                    # Report source coordinates and type names, never argument values or source text.
                    path, line = item.get('toolPath'), item.get('line')
                    names = ('callable', 'parameter', 'requiredType', 'actualType')
                    if (not isinstance(path, str) or not path.startswith('source/') or len(path) > 512
                            or any(part in ('', '.', '..') for part in path.split('/'))
                            or any(ord(ch) < 32 or ord(ch) == 127 for ch in path) or redact_text(path) != path
                            or type(line) is not int or not 0 < line <= 1048576 or any(
                                not isinstance(item.get(key), str) or len(item[key]) > 128 or
                                not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', item[key], flags=re.ASCII)
                                for key in names)):
                        raise ValueError('Invalid Host library type finding')
                    issue.update(toolPath=path, line=line, **{key: item[key] for key in names})
                if code in ('READY_PATH_EVIDENCE_MISMATCH', 'READY_RUNTIME_REPAIR_REQUIRED'):
                    if safe_source_ref(item.get('attemptedSourceRef')):
                        issue['attemptedSourceRef'] = item['attemptedSourceRef']
                    elif item.get('attemptedRefStatus') == 'NOT_VERIFIED_IN_SOURCE':
                        issue['attemptedRefStatus'] = 'NOT_VERIFIED_IN_SOURCE'
                    for field in ('candidateRefs', 'changedRefs', 'unchangedRelatedFunctions',
                                  'unchangedRouteFunctions', 'changedExceptionDecorators'):
                        refs = item.get(field)
                        if field == 'candidateRefs' or refs is not None:
                            issue[field] = [ref for ref in refs[:4] if safe_source_ref(ref)] if isinstance(refs, (list, tuple)) else []
                issues.append(issue)
    return {'code': code, 'issues': issues}


_CONTROL_CHARACTER = re.compile('[\x00-\x1f\x7f-\x9f]')


def text_control_issues(data) -> list[dict]:
    """Field pointers of strings holding control characters; never the strings themselves.

    Live Run 3754980f (2026-11-17): Security wrote U+0000 where an acceptance
    criterion has a middle dot. The contract rejected it with no field, the
    feedback listed nothing, the model repeated the same output and the Run ended.
    """
    found = []

    def walk(value, pointer):
        if len(found) >= MAX_ISSUES:
            return
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(key, str):
                    walk(item, pointer + '/' + key)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, pointer + '/' + str(index))
        elif isinstance(value, str) and _CONTROL_CHARACTER.search(value):
            found.append({'fieldPath': pointer, 'reasonCode': 'TEXT_CONTROL_CHARACTER'})

    walk(data, '')
    return found


def contract_check(validate, explain=None):
    """Wrap an existing role/phase decision validator as an engine ``output_check``.

    The caller still runs the same validator after the engine returns, so the
    final acceptance and its error codes stay unchanged. Multi roles and Single
    phases map to the same neutral code; planner diagnostics are passed on for
    both because both use ``validate_planner_decision``. A valid INPUT_REQUIRED
    decision is reported as ``QUESTIONS_NOT_ANSWERED`` and governed by the
    separate ``max_question_reminders`` limit.
    """
    def check(data, executed_tools=()):
        try:
            decision = validate(data)
        except Exception as error:
            planner = getattr(error, 'validation', None) or getattr(error, 'planner_validation', None)
            issues = []
            if planner is not None:
                try:
                    issues = list(planner.public().get('issues') or [])
                except Exception:
                    issues = []
            if not isinstance(error, ValueError):
                raise  # Host checker bugs must not be treated as accepted model output.
            # Role contracts (QA/Security/Developer, Single phases) raise one stable reason
            # without a field. Point at the one cause the Host can name without values.
            if not issues and explain is not None:
                try:
                    issues = list(explain(data) or [])
                except Exception:
                    issues = []
            return {'code': 'OUTPUT_CONTRACT_INVALID', 'issues': issues or text_control_issues(data)}
        if getattr(decision, 'kind', None) == 'INPUT_REQUIRED':
            # A valid question. The engine acts on it only when
            # llmLimits.max_question_reminders > 0 (autonomous measurement Runs).
            return {'code': 'QUESTIONS_NOT_ANSWERED', 'issues': []}
        return None
    return check


SOURCE_WRITE_TOOLS = frozenset({'write_source_file', 'edit_source_file', 'apply_patch'})


class ExecutedTools(tuple):
    """Tool names plus minimal Host-validated progress; never raw Tool values."""
    def __new__(cls, names, *, source_changed, unused_rewrite=False):
        result = super().__new__(cls, names)
        result.source_changed = source_changed is True
        result.unused_rewrite = unused_rewrite is True
        return result


def source_change_check(validate):
    """READY requires Host-confirmed net change in Multi and Single development.

    Production engines populate ExecutedTools.source_changed from the checkpoint
    inventory against the immutable parent. Legacy tuple-only checks are for
    isolated callers; they do not provide evidence for workflow termination.
    The engine forces a corrective write or stops before semantic publication.
    """
    contract = contract_check(validate)

    def check(data, executed_tools=()):
        rejection = contract(data, executed_tools)
        if rejection is not None:
            return rejection
        changed = (executed_tools.source_changed if isinstance(executed_tools, ExecutedTools)
                   else bool(set(executed_tools) & SOURCE_WRITE_TOOLS))
        if isinstance(data, dict) and data.get('kind') == 'READY' and not changed:
            issues = ([{'fieldPath': '/source', 'reasonCode': 'UNUSED_TEXT_REWRITE'}]
                      if getattr(executed_tools, 'unused_rewrite', False) else [])
            return {'code': 'READY_WITHOUT_CHANGES', 'issues': issues}
        return None
    return check


def case_expectation_check(validate, expectation_issues=None, *, explain=None, extra_checks=()):
    """``contract_check`` plus the generated-case expectation rule for QA/Security drafts.

    ``expectation_issues(data)`` is ``requirement_scope.generated_case_expectation_issues``
    bound to the step. Multi QA/Security and the Single QA/SECURITY phases use the
    same rule. The caller still runs ``validate_generated_case_expectations``.
    """
    contract = contract_check(validate, explain)

    def check(data, executed_tools=()):
        rejection = contract(data, executed_tools)
        if rejection is not None:
            return rejection
        if isinstance(data, dict) and data.get('kind') == 'READY':
            groups = []
            issues = list(expectation_issues(data) or []) if expectation_issues is not None else []
            if issues:
                groups.append(('CASE_EXPECTATION_INVALID', issues))
            # Further Host checks that would otherwise fail the step after the
            # engine returns (generated test files, Security code references).
            for code, inspect in extra_checks:
                # A checker exception is a Host failure, never evidence that a
                # prerequisite passed. LLMEngine stops CONFIGURATION on it.
                issues = list(inspect(data) or [])
                if issues:
                    groups.append((code, issues))
            if groups:
                # Missing files retain the forced-write route. After files exist,
                # unread/invalid references take precedence over runnable test
                # defects, which can use the later test-only repair route.
                missing = next((i for i, (code, rows) in enumerate(groups)
                                if code == 'GENERATED_TESTS_INVALID'
                                and any(row.get('reasonCode') == 'GENERATED_TEST_FILE_MISSING' for row in rows)), None)
                reference = next((i for i, (code, _) in enumerate(groups) if code == 'CODE_REFERENCE_INVALID'), None)
                primary = missing if missing is not None else reference if reference is not None else 0
                ordered = [groups[primary], *(group for i, group in enumerate(groups) if i != primary)]
                return {'code': groups[primary][0], 'issues': [row for _, rows in ordered for row in rows]}
        return None
    return check


def development_check(validate, precheck=None, path_check=None, priority_check=None, library_type_check=None,
                      output_safety_check=None, signup_quality_check=None, service_interface_check=None):
    """``source_change_check`` plus the Host's read-only start-contract precheck for READY.

    ``precheck`` returns closed issues for start failures that the Host build would
    report for certain (``agents.runtime.start_precheck``). Multi Developer and the
    Single IMPLEMENTING/FIXING phases use the same rule and the same limit. The
    precheck is advisory: if it fails, the existing Host build decides as before.
    """
    change = source_change_check(validate)

    def check(data, executed_tools=()):
        rejection = change(data, executed_tools)
        if rejection is None and isinstance(data, dict) and data.get('kind') == 'READY' and service_interface_check is not None:
            issues = service_interface_check()
            if issues:
                return {'code': 'READY_SERVICE_INTERFACE_REVIEW', 'issues': issues}
        if (library_type_check is not None and isinstance(data, dict) and data.get('kind') == 'READY'
                and (not rejection or rejection['code'] == 'READY_WITHOUT_CHANGES')):
            issues = library_type_check()
            if issues:
                return {'code': 'READY_LIBRARY_TYPE_MISMATCH', 'issues': [
                    {'fieldPath': '/source', 'reasonCode': 'LIBRARY_ARGUMENT_TYPE_MISMATCH', **issue}
                    for issue in issues]}
        if rejection is not None or not isinstance(data, dict) or data.get('kind') != 'READY':
            return rejection
        if path_check is not None:
            issues = path_check(data)
            if issues:
                # A false claim for an unresolved normal request needs implementation work.
                # A located change with the wrong citation needs only a corrected report.
                pending = priority_check() if priority_check else ()
                normal = '/repairPaths/NORMAL_REQUEST_RUNTIME_FAILURE'
                if 'NORMAL_REQUEST_RUNTIME_FAILURE' in pending and any(
                        issue['fieldPath'] == normal + '/sourceRef' and not issue.get('candidateRefs')
                        for issue in issues):
                    return {'code': 'READY_RUNTIME_REPAIR_REQUIRED', 'issues': [
                        {'fieldPath': normal, 'reasonCode': 'NO_PRIORITY_PATH_EDIT'}, *issues]}
                return {'code': 'READY_PATH_EVIDENCE_MISMATCH', 'issues': issues}
        if precheck is not None:
            try:
                issues = precheck()
            except Exception:
                issues = []
            if issues:
                return {'code': 'START_CONTRACT_PRECHECK_FAILED', 'issues': issues}
        if output_safety_check is not None:
            issues = output_safety_check()
            if issues:
                return {'code': 'READY_OUTPUT_SAFETY_REVIEW', 'issues': issues}
        if signup_quality_check is not None:
            issues = signup_quality_check()
            if issues:
                return {'code': 'READY_SIGNUP_QUALITY_REVIEW', 'issues': issues}
        if priority_check is not None:
            pending = priority_check()
            if pending:
                return {'code': 'READY_PRIORITY_REPAIR_NEEDED', 'issues': [
                    {'fieldPath': '/repairPaths/' + path, 'reasonCode': 'NO_PRIORITY_PATH_EDIT'} for path in pending]}
        return None
    return check


# Read-only Tools whose settled rejections leave nothing behind (handoff P5, point 3).
READ_ONLY_TOOLS = frozenset({'read_project_file', 'read_test_report', 'read_security_report'})
# A write the tool rejected before applying anything (2026-11-18 gpt-4.1 diagnostic: a stale
# expectedSha256 made write_source_file fail with WRITE_CONFLICT and ended the Developer step).
WRITE_TOOLS = frozenset({'write_source_file', 'edit_source_file', 'write_test_file'})
RETURNABLE_WRITE_ERRORS = frozenset({'WRITE_CONFLICT'})
RETURNABLE_EDIT_ERRORS = frozenset({'EDIT_READ_REQUIRED', 'EDIT_STALE_READ',
                                    'EDIT_TARGET_MISSING', 'EDIT_TARGET_AMBIGUOUS'})
# Recorded in the Host policy: settled read errors and a known WRITE_CONFLICT go back to the model.
TOOL_ERROR_RETURN_POLICY = 'SETTLED_READ_ERRORS_BOUND_EDIT_AND_ATOMIC_CREATE_V6'
RETURNABLE_TOOL_ERRORS = frozenset({'FILE_NOT_FOUND', 'REPORT_NOT_FOUND', 'FILE_TOO_LARGE', 'FILE_ENCODING_ERROR',
                                    'PATH_TRAVERSAL'})
_TOOL_ERROR_HINTS = {
    'EDIT_READ_REQUIRED': 'Nothing was written. Read this source file with read_project_file in this step before editing it. Use the returned sha256 and a unique fragment from that content.',
    'EDIT_STALE_READ': 'Nothing was written. The edit is not bound to the current Host-observed read. Read the file again and use that returned sha256 and current content; do not guess hashes or reuse a rejected candidate.',
    'EDIT_TARGET_MISSING': 'Nothing was written. The Host now requires read_project_file for this file, then write_source_file with the complete current file plus your correction and that read sha256. Partial edit_source_file is not offered on this recovery turn. Preserve unrelated code and whitespace; never guess another oldText or omit expectedSha256.',
    'EDIT_TARGET_AMBIGUOUS': 'Nothing was written. oldText is not unique. The Host now requires a fresh read followed by write_source_file with the complete current file plus your correction and that read sha256. Partial edits are not offered on this recovery turn; preserve all unrelated code.',
    'FILE_NOT_FOUND': ('That file does not exist; do not repeat the same read. Use fileAccess for existing files. '
                       'If this is an intended NEW source file and write_source_file is offered to your role, '
                       'create it with complete content and omit expectedSha256 (wire null). The Tool atomically '
                       'requires absence. Existing files require their current read sha256; never guess one.'),
    'REPORT_NOT_FOUND': 'That report does not exist. Use a report reference exactly as given in the task input.',
    'FILE_TOO_LARGE': 'That file is too large to read with this tool.',
    'FILE_ENCODING_ERROR': 'That file is not UTF-8 text.',
    'PATH_TRAVERSAL': 'That path is not readable for your role. Use the workspace paths in fileAccess.',
    'WRITE_CONFLICT': ('Nothing was written: expectedSha256 did not match the file as it is now. Read the file again '
                       'and pass its current sha256. Never bypass the conflict by omitting expectedSha256. '
                       'If an earlier edit in this step already applied the intended bytes, preserve them and '
                       'submit the complete current content with write_source_file and the fresh sha256; do '
                       'not undo that edit or invent a cosmetic change. Source progress and READY checks still apply.'),
}


def returnable_tool_error(tool_name, failure) -> str | None:
    """The settled error code of a Tool failure that may be shown to the model, else None.

    Read-only Tools return their settled rejections; write Tools only a rejection that
    certainly applied nothing (WRITE_CONFLICT). An unknown result never qualifies.
    """
    if getattr(failure, 'result_unknown', True) is not False:
        return None
    code = getattr(failure, 'error_kind', None)
    if tool_name == 'edit_source_file' and code in RETURNABLE_EDIT_ERRORS:
        return code
    if tool_name in READ_ONLY_TOOLS:
        return code if code in RETURNABLE_TOOL_ERRORS else None
    if tool_name in WRITE_TOOLS:
        return code if code in RETURNABLE_WRITE_ERRORS else None
    return None


def tool_error_output(tool_name, code, remaining) -> dict:
    """Value-free Tool output that replaces a settled read rejection."""
    hint = _TOOL_ERROR_HINTS[code]
    if tool_name == 'edit_source_file' and code == 'WRITE_CONFLICT':
        hint = ('Nothing was written. Read the current file again: expectedSha256 must match its returned '
                'sha256, and oldText must occur exactly once. Reconstruct a unique fragment from the '
                'current content. This edit tool always requires expectedSha256; do not guess a hash '
                'or reuse text from the rejected candidate before Host restoration.')
    return {'toolError': {'toolName': tool_name, 'code': code, 'executed': True, 'changedState': False,
                          'errorsRemaining': remaining, 'instruction': hint}}
