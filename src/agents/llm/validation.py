"""Closed, value-free diagnostics for common LLM JSON-schema boundaries."""
from hashlib import sha256
from itertools import islice
from types import MappingProxyType
from typing import Literal, get_args

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ConfigDict, Field, model_validator


FIELDS = frozenset('workspaceId path content oldText newText expectedSha256 patch baseSnapshotSha256 snapshotId testScope testSuite scannerProfile reportRef answer summary questions kind implementationPlan taskId title description requirementIds dependsOn unapprovedAssumptions cases testCases toolName selector testId requirementId expectedResult sourceRefs quote sourceRef evidence reasonCode status findings requirementResults findingId severity disposition rationale ruleId ruleIds location references file line column codeVersion schemaVersion fieldReviews candidateSha256 basisSha256 sha256 sizeBytes changed changedFiles newHashes exitCode stdoutRef stderrRef durationMs executionManifestId total passed failed skipped traceRefs testResult securityResult'.split())
FIELDS = FIELDS | frozenset({'requirementReviews', 'findingReviews', 'proposedOutcome',
    'proposedDisposition', 'anchorId', 'startLine', 'endLine'})
PATH = r'^(?:/(?:' + '|'.join(sorted(FIELDS)) + r'|[0-9]{1,6}))*$'
STAGES = Literal['TOOL_ARGUMENTS_WIRE', 'TOOL_ARGUMENTS', 'TOOL_RESULT',
                 'TOOL_RESULT_REDACTED', 'MODEL_OUTPUT', 'MODEL_OUTPUT_REDACTED']
TOOLS = Literal['read_project_file', 'write_source_file', 'edit_source_file', 'write_test_file', 'apply_patch',
                'run_build', 'run_unit_tests', 'run_browser_tests', 'run_security_scan',
                'read_test_report', 'read_security_report']
TYPES = Literal['object', 'array', 'string', 'integer', 'number', 'boolean', 'null', 'unknown']
REASONS = MappingProxyType({
    **{code: text for code, text in (
        ('TEST_CLIENT_NOT_STARTED', '검사 코드가 앱의 시작·종료 절차를 실행하지 않았습니다.'),
        ('GENERATED_TEST_SYNTAX_INVALID', '검사 코드의 Python 문법을 수정해야 합니다.'),
        ('GENERATED_TEST_NONE', '실제로 실행 가능한 검사 메서드가 없습니다.'),
        ('GENERATED_TEST_FILE_MISSING', '필수 검사 파일이 없습니다.'),
        ('TEST_IN_FILE_NOT_DECLARED', '파일에 있는 검사가 결과서 목록에 없습니다.'),
        ('DECLARED_TEST_NOT_IN_FILE', '결과서에 적힌 검사가 실제 파일에 없습니다.'),
        ('ASYNC_TEST_IN_SYNC_CASE', '비동기 검사를 동기 TestCase로 실행할 수 없습니다.'),
        ('TEST_IMPORT_NAME_MISSING', '제품에 없는 이름을 검사 코드에서 불러왔습니다.'),
        ('TEST_HOST_FIXTURE_REQUIRED', '승인된 검사 준비 기능을 사용해야 합니다.'),
        ('TEST_SERVICE_FIXTURE_REQUIRED', '일반 서비스의 공통 검사 준비 기능을 사용해야 합니다.'),
        ('TEST_OUTPUT_ARGUMENT_INVALID', '출력 검사에 실제 합성 문자열과 올바른 인자를 전달해야 합니다.'),
        ('SERVICE_JSON_REQUIRED', '합의된 JSON 준비 응답 대신 다른 형식을 반환합니다.'),
        ('ASSIGNED_REQUIREMENT_WITHOUT_CASE', '할당된 요구사항의 검사 항목이 빠졌습니다.'),
        ('ASSIGNED_REQUIREMENT_WITHOUT_REVIEW', '할당된 요구사항의 검토 항목이 빠졌습니다.'),
        ('CASE_DUPLICATE', '동일한 검사 식별자가 중복됐습니다.'),
        ('CASE_REQUIREMENT_NOT_ASSIGNED', '할당되지 않은 요구사항을 검사 항목에 사용했습니다.'),
        ('REQUIREMENT_DUPLICATE', '요구사항 검토가 중복됐습니다.'),
        ('REQUIREMENT_NOT_ASSIGNED', '할당되지 않은 요구사항을 사용했습니다.'),
        ('FINDING_DUPLICATE', '보안 발견 항목의 검토가 중복됐습니다.'),
        ('FINDING_NOT_MEASURED', '실제 검사기에 없는 발견 항목을 사용했습니다.'),
        ('MEASURED_FINDING_WITHOUT_REVIEW', '실제 보안 발견 항목의 검토가 빠졌습니다.'),
        ('FIELD_MISSING', '필수 결과서 필드가 없습니다.'),
        ('FIELD_NOT_ALLOWED', '허용되지 않은 결과서 필드가 있습니다.'),
        ('QUESTIONS_NOT_ALLOWED_WHEN_READY', '완료 결과서에 입력 요청을 함께 넣을 수 없습니다.'),
        ('SELECTOR_NOT_ALLOWED', '승인되지 않은 검사 범위를 사용했습니다.'),
        ('REFERENCE_PATH_NOT_MEASURED', '현재 Source에 없는 참조를 사용했습니다.'),
        ('REFERENCE_LINES_INVALID', '소스 참조의 줄 범위가 올바르지 않습니다.'),
        ('REFERENCES_REQUIRED_FOR_OUTCOME', '검토 판단의 소스 근거가 없습니다.'),
        ('REFERENCE_FILE_NOT_READ', '현재 단계에서 읽지 않은 소스를 참조했습니다.'),
        ('REFERENCE_PAST_END_OF_FILE', '파일 끝을 넘는 소스 범위를 참조했습니다.'),
        ('REFERENCE_DUPLICATE', '소스 참조가 중복됐습니다.'),
        ('REFERENCE_RANGE_TOO_LONG', '소스 참조 범위가 너무 깁니다.'),
        ('TOO_MANY_REFERENCES', '허용된 소스 참조 수를 초과했습니다.'),
        ('TEXT_CONTROL_CHARACTER', '결과서 문자열에 제어문자가 있습니다.'),
        ('TEXT_LOOKS_LIKE_SECRET', '결과서 문자열에서 인증정보 패턴이 감지됐습니다.'),
        ('PENDING_STATE_REQUIRED', '실행 전 보안 검토는 Host 검사 대기 상태여야 합니다.'),
        ('SOURCE_ANCHOR_INVALID', '현재 Snapshot의 소스 참조 식별자가 아닙니다.'),
    )},
    'JSON_INVALID': '완전하고 유효한 JSON 문서가 아닙니다.',
    'TYPE_INVALID': '필드의 자료형이 규격과 다릅니다.',
    'REQUIRED_MISSING': '필수 필드가 없습니다.',
    'UNEXPECTED_FIELD': '허용되지 않은 필드가 있습니다.',
    'VALUE_NOT_ALLOWED': '허용된 값 또는 선택 분기와 일치하지 않습니다.',
    'PATTERN_INVALID': '문자열 패턴 또는 형식이 규격과 다릅니다.',
    'BOUNDS_INVALID': '길이·개수·수치 제한을 충족하지 못했습니다.',
    'SCHEMA_INVALID': 'JSON 스키마를 충족하지 못했습니다.',
    'DIAGNOSTIC_UNAVAILABLE': '검증 실패의 세부 조건을 안전하게 수집하지 못했습니다.',
})

POLICY_REASONS = MappingProxyType({
    'TOOL_NOT_OFFERED': '이 단계에서 제공되지 않은 도구를 요청했습니다.',
    'CALL_ID_INVALID': '도구 호출 식별자가 올바르지 않습니다.',
    'CALL_ID_REUSED': '이미 사용한 도구 호출 식별자입니다.',
    'WORKSPACE_MISMATCH': '도구의 작업공간이 현재 Run과 다릅니다.',
    'ARGUMENT_CREDENTIAL_DENIED': '일반 도구 인자에 인증정보 패턴이 감지됐습니다.',
    'SOURCE_CREDENTIAL_DENIED': '코드 또는 테스트 내용에 인증정보 리터럴 패턴이 감지됐습니다.',
    'TEST_SOURCE_INVALID': '생성 시험 코드의 문법 또는 Host 보조 기능 사용 규격이 올바르지 않습니다.',
})


class LLMPolicyReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', populate_by_name=True, hide_input_in_errors=True)
    stage: Literal['TOOL_POLICY'] = 'TOOL_POLICY'
    sequence: int = Field(strict=True, ge=1)
    tool_name: TOOLS | None = Field(default=None, alias='toolName')
    boundary: Literal['TOOL_SELECTION', 'TOOL_ARGUMENTS', 'TOOL_RESULT']
    reason_code: str = Field(alias='reasonCode')
    field_path: Literal['', '/workspaceId', '/content', '/oldText', '/newText', '/patch', '/callId', '/toolName'] = Field(default='', alias='fieldPath')
    source_rule: Literal['REGISTERED_SECRET', 'CREDENTIAL_ASSIGNMENT', 'CREDENTIAL_PATTERN', 'URI_CREDENTIAL'] | None = Field(default=None, alias='sourceRule')
    source_line: int | None = Field(default=None, alias='sourceLine', strict=True, ge=1, le=1000000)

    @model_validator(mode='after')
    def closed_reason(self):
        if self.reason_code not in POLICY_REASONS:
            raise ValueError('unknown policy reason')
        if self.reason_code != 'SOURCE_CREDENTIAL_DENIED' and (self.source_rule is not None or self.source_line is not None):
            raise ValueError('inconsistent source diagnostic')
        return self

    def public(self):
        return {**self.model_dump(mode='json', by_alias=True), 'schemaVersion': 1,
                'reason': POLICY_REASONS[self.reason_code]}


def policy_error(reason, *, sequence, tool_name=None, boundary='TOOL_ARGUMENTS', field_path='',
                 source_rule=None, source_line=None):
    from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
    return LLMRuntimeError(LLMErrorCode.TOOL_POLICY, validation=LLMPolicyReport(
        sequence=sequence, toolName=tool_name if tool_name in get_args(TOOLS) else None,
        boundary=boundary, reasonCode=reason, fieldPath=field_path,
        sourceRule=source_rule, sourceLine=source_line))


class ValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', populate_by_name=True, hide_input_in_errors=True)
    field_path: str = Field(alias='fieldPath', pattern=PATH, max_length=512)
    reason_code: str = Field(alias='reasonCode')
    actual_type: TYPES = Field(alias='actualType')

    @model_validator(mode='after')
    def closed_reason(self):
        if self.reason_code not in REASONS:
            raise ValueError('unknown validation reason')
        return self

    def public(self):
        return {**self.model_dump(mode='json', by_alias=True), 'reason': REASONS[self.reason_code]}


class LLMValidationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid', populate_by_name=True, hide_input_in_errors=True)
    stage: STAGES
    sequence: int = Field(strict=True, ge=1)
    tool_name: TOOLS | None = Field(default=None, alias='toolName')
    schema_sha256: str = Field(alias='schemaSha256', pattern=r'^[0-9a-f]{64}$')
    candidate_sha256: str | None = Field(default=None, alias='candidateSha256', pattern=r'^[0-9a-f]{64}$')
    issues: tuple[ValidationIssue, ...] = Field(min_length=1, max_length=64)
    issues_truncated: bool = Field(default=False, alias='issuesTruncated', strict=True)

    @model_validator(mode='after')
    def consistent_boundary(self):
        if self.stage.startswith('TOOL_') != (self.tool_name is not None):
            raise ValueError('inconsistent validation boundary')
        return self

    def public(self):
        return {**self.model_dump(mode='json', by_alias=True, exclude={'issues'}),
                'schemaVersion': 1, 'issues': [issue.public() for issue in self.issues]}


def _path(parts):
    safe = []
    for part in parts:
        if type(part) is str and part in FIELDS:
            safe.append(part)
        elif type(part) is int and 0 <= part <= 999999:
            safe.append(str(part))
        else:
            break  # No unknown property names, source paths or submitted IDs.
    return ''.join('/' + part for part in safe)[:512]


def role_report(schema, issues, *, sequence):
    """Persist Host reason codes without copying candidate IDs or values."""
    import json
    rows = []
    for item in issues[:64]:
        path, code = item.get('fieldPath', ''), item.get('reasonCode')
        if code in REASONS and isinstance(path, str) and re_full_path(path):
            rows.append(ValidationIssue(fieldPath=path, reasonCode=code, actualType='unknown'))
    if not rows:
        return None
    return LLMValidationReport(stage='MODEL_OUTPUT', sequence=sequence,
        schemaSha256=sha256(json.dumps(schema.to_dict(), sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        issues=tuple(rows), issuesTruncated=len(issues) > 64)


def re_full_path(path):
    import re
    return len(path) <= 512 and re.fullmatch(PATH, path) is not None


def _type(value):
    return {dict:'object', list:'array', str:'string', int:'integer', float:'number',
            bool:'boolean', type(None):'null'}.get(type(value), 'unknown')


def diagnose(schema, data, *, stage, sequence, tool_name=None):
    from agents.llm.contracts import json_text, parse_json
    issues = []
    candidate_sha = None
    truncated = False
    try:
        raw = json_text(data)
        candidate_sha = sha256(raw.encode('utf-8')).hexdigest()
        copied = parse_json(raw)
        errors = Draft202012Validator(schema.to_dict(), format_checker=FormatChecker()).iter_errors(copied)
        for error_index, error in enumerate(islice(errors, 65)):
            if len(issues) >= 64 or error_index == 64:
                truncated = True
                break
            keyword = error.validator
            reason = ('TYPE_INVALID' if keyword == 'type' else 'REQUIRED_MISSING' if keyword == 'required'
                else 'UNEXPECTED_FIELD' if keyword == 'additionalProperties'
                else 'VALUE_NOT_ALLOWED' if keyword in ('enum', 'const', 'anyOf', 'oneOf')
                else 'PATTERN_INVALID' if keyword in ('pattern', 'format')
                else 'BOUNDS_INVALID' if keyword in ('minLength', 'maxLength', 'minItems', 'maxItems',
                    'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'uniqueItems')
                else 'SCHEMA_INVALID')
            paths = [list(error.absolute_path)]
            if keyword == 'required' and type(error.instance) is dict:
                paths = [[*error.absolute_path, name] for name in error.validator_value if name not in error.instance]
            for path in paths:
                item = ValidationIssue(fieldPath=_path(path), reasonCode=reason,
                                       actualType='unknown' if keyword == 'required' else _type(error.instance))
                if item not in issues:
                    if len(issues) == 64:
                        truncated = True
                        break
                    issues.append(item)
        if not issues:
            raise ValueError()
    except Exception:
        issues = [ValidationIssue(fieldPath='', reasonCode='DIAGNOSTIC_UNAVAILABLE', actualType='unknown')]
        truncated = False
    return LLMValidationReport(stage=stage, sequence=sequence, toolName=tool_name,
        schemaSha256=sha256(schema.schema_json.encode('utf-8')).hexdigest(), candidateSha256=candidate_sha,
        issues=tuple(issues), issuesTruncated=truncated)


def validate(schema, data, *, stage, sequence, tool_name=None):
    from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
    rejected = False
    try:
        schema.validate(data)
    except LLMRuntimeError as error:
        if error.code is not LLMErrorCode.SCHEMA:
            raise
        rejected = True
    if rejected:
        report = diagnose(schema, data, stage=stage, sequence=sequence, tool_name=tool_name)
        raise LLMRuntimeError(LLMErrorCode.SCHEMA, validation=report) from None


def parse_candidate(value, schema, *, max_bytes, stage, sequence, tool_name=None):
    """Only model-owned, unexecuted arguments/final output may be regenerated.

    Provider envelopes, continuation items and Tool results still use parse_json
    directly. An invalid envelope must never be mistaken for a model typo.
    """
    from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, parse_json
    try:
        return parse_json(value, max_bytes=max_bytes)
    except LLMRuntimeError:
        report = LLMValidationReport(stage=stage, sequence=sequence, toolName=tool_name,
            schemaSha256=sha256(schema.schema_json.encode('utf-8')).hexdigest(),
            issues=(ValidationIssue(fieldPath='', reasonCode='JSON_INVALID', actualType='unknown'),))
        raise LLMRuntimeError(LLMErrorCode.RESPONSE, validation=report) from None
