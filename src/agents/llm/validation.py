"""Closed, value-free diagnostics for common LLM JSON-schema boundaries."""
from hashlib import sha256
from itertools import islice
from types import MappingProxyType
from typing import Literal, get_args

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ConfigDict, Field, model_validator


FIELDS = frozenset('workspaceId path content oldText newText expectedSha256 patch baseSnapshotSha256 snapshotId testScope testSuite scannerProfile reportRef answer summary questions kind implementationPlan taskId title description requirementIds dependsOn unapprovedAssumptions cases testCases toolName selector testId requirementId expectedResult sourceRefs quote sourceRef evidence reasonCode status findings requirementResults findingId severity disposition rationale ruleId ruleIds location references file line column codeVersion schemaVersion fieldReviews candidateSha256 basisSha256 sha256 sizeBytes changed changedFiles newHashes exitCode stdoutRef stderrRef durationMs executionManifestId total passed failed skipped traceRefs testResult securityResult'.split())
PATH = r'^(?:/(?:' + '|'.join(sorted(FIELDS)) + r'|[0-9]{1,6}))*$'
STAGES = Literal['TOOL_ARGUMENTS_WIRE', 'TOOL_ARGUMENTS', 'TOOL_RESULT',
                 'TOOL_RESULT_REDACTED', 'MODEL_OUTPUT', 'MODEL_OUTPUT_REDACTED']
TOOLS = Literal['read_project_file', 'write_source_file', 'edit_source_file', 'write_test_file', 'apply_patch',
                'run_build', 'run_unit_tests', 'run_browser_tests', 'run_security_scan',
                'read_test_report', 'read_security_report']
TYPES = Literal['object', 'array', 'string', 'integer', 'number', 'boolean', 'null', 'unknown']
REASONS = MappingProxyType({
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
