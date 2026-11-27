"""Closed, value-free diagnostics for a rejected Planner decision.

This explains a rejection; it never admits, repairs or replays a candidate.
Only field positions and approved requirement keys leave this boundary.
"""

from collections import Counter
from hashlib import sha256
import re
from types import MappingProxyType
from typing import Literal

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, model_validator

from orchestrator.core.security import redact_data


REASONS = MappingProxyType({
    "CONTRACT_SETUP_FAILED": "Host의 Planner 출력 계약을 준비하지 못했습니다.",
    "JSON_INVALID": "검증 입력의 JSON 형식 또는 크기 제한을 충족하지 못했습니다.",
    "SCHEMA_INVALID": "필드의 형식·필수 여부·허용 값 또는 길이 제한을 충족하지 못했습니다.",
    "TEXT_INVALID": "빈 문자열·금지 제어 문자 또는 민감정보로 인식되는 텍스트가 포함됐습니다.",
    "SECRET_DETECTED": "검증 입력에서 민감정보로 인식되는 내용이 발견됐습니다.",
    "PLAN_REQUIRED": "PLAN에는 하나 이상의 구현 작업이 필요합니다.",
    "PLAN_FORBIDDEN": "INPUT_REQUIRED 또는 REJECTED에 구현 작업을 포함할 수 없습니다.",
    "QUESTIONS_REQUIRED": "INPUT_REQUIRED에는 하나 이상의 질문이 필요합니다.",
    "QUESTIONS_FORBIDDEN": "PLAN 또는 REJECTED에 질문을 포함할 수 없습니다.",
    "ASSUMPTIONS_FORBIDDEN": "미승인 가정은 INPUT_REQUIRED에서만 제출할 수 있습니다.",
    "ASSUMPTION_DUPLICATE": "같은 미승인 가정을 중복 제출했습니다.",
    "TASK_ID_DUPLICATE": "다른 작업과 taskId가 중복됐습니다.",
    "REQUIREMENT_DUPLICATE": "같은 작업 안에 requirementId가 중복됐습니다.",
    "REQUIREMENT_NOT_COVERED": "승인된 요구사항이 어떤 구현 작업에도 연결되지 않았습니다.",
    "DEPENDENCY_DUPLICATE": "같은 의존 작업을 중복 지정했습니다.",
    "DEPENDENCY_UNKNOWN": "존재하지 않는 작업을 의존 대상으로 지정했습니다.",
    "DEPENDENCY_SELF": "작업이 자기 자신을 의존 대상으로 지정했습니다.",
    "DEPENDENCY_CYCLE": "이 작업이 순환 의존관계에 포함됐습니다.",
    "PLAN_ASSEMBLY_INVALID": "Host의 계획 모델 검증·조립에 실패했습니다.",
    "REQUIREMENT_BINDING_INVALID": "조립된 요구사항이 동결된 Host 기준과 일치하지 않습니다.",
    "HOST_SCHEMA_UNAVAILABLE": "Host의 최종 Artifact 스키마를 읽거나 검증하지 못했습니다.",
    "ARTIFACT_SCHEMA_INVALID": "조립된 계획이 최종 Artifact 스키마를 충족하지 못했습니다.",
    "DECISION_INVALID": "Planner 결정 객체의 일관성 검증에 실패했습니다.",
    "VALIDATION_INTERNAL_ERROR": "검증 처리 중 내부 오류가 발생해 세부 조건을 판별하지 못했습니다.",
})
STAGES = Literal["CONTRACT_SETUP", "JSON", "MODEL_SCHEMA", "TEXT", "DECISION", "PLAN_MODEL",
                 "REQUIREMENTS", "ARTIFACT_SAFETY", "ARTIFACT_SCHEMA_LOAD", "ARTIFACT_SCHEMA",
                 "DECISION_MODEL", "UNKNOWN"]
_FIELDS = frozenset({"kind", "implementationPlan", "taskId", "title", "description", "requirementIds",
                     "dependsOn", "questions", "unapprovedAssumptions", "requirements", "schemaVersion",
                     "requirementId", "key", "acceptanceCriteria"})
_PATH = r"^(?:/(?:kind|implementationPlan|taskId|title|description|requirementIds|dependsOn|questions|unapprovedAssumptions|requirements|schemaVersion|requirementId|key|acceptanceCriteria|[0-9]{1,6}))*$"


class PlannerValidationIssue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True, hide_input_in_errors=True)
    field_path: str = Field(alias="fieldPath", pattern=_PATH, max_length=512)
    reason_code: str = Field(alias="reasonCode")
    requirement_key: str | None = Field(default=None, alias="requirementKey", pattern=r"^REQ-[0-9]{3,}$", max_length=64)

    @model_validator(mode="after")
    def known_reason(self):
        if self.reason_code not in REASONS:
            raise ValueError("unknown Planner reason")
        return self

    def public(self):
        return {**self.model_dump(mode="json", by_alias=True, exclude_none=True), "reason": REASONS[self.reason_code]}


class PlannerValidationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True, hide_input_in_errors=True)
    stage: STAGES
    candidate_sha256: str | None = Field(default=None, alias="candidateSha256", pattern=r"^[0-9a-f]{64}$")
    issues: tuple[PlannerValidationIssue, ...] = Field(min_length=1, max_length=64)
    issue_count: int = Field(alias="issueCount", ge=1, strict=True)

    @model_validator(mode="after")
    def bounded(self):
        if len(self.issues) != min(self.issue_count, 64):
            raise ValueError("inconsistent Planner issue count")
        return self

    def public(self):
        return {"schemaVersion": 1, "stage": self.stage, "candidateSha256": self.candidate_sha256,
                "issueCount": self.issue_count, "issuesTruncated": self.issue_count > len(self.issues),
                "issues": [item.public() for item in self.issues]}


def safe_path(parts):
    values = []
    for part in parts:
        if type(part) is int and 0 <= part <= 999999:
            values.append(str(part))
        elif type(part) is str and part in _FIELDS:
            values.append(part)
        else:
            break  # Never echo an unknown property name from model data.
    return "".join("/" + value for value in values)[:512]


def diagnose_planner_failure(data, scenario, *, stage="UNKNOWN", schema=None, candidate_json=None,
                             artifact_error_paths=()):
    """Collect independent failures when shape permits, otherwise safe schema paths.

    The original validator remains the acceptance authority. A diagnostic error
    only produces a closed internal-error reason, never turns rejection to PASS.
    """
    issues, seen = [], set()

    def add(path, code, requirement=None):
        key = (path, code, requirement)
        if key not in seen:
            seen.add(key)
            issues.append(PlannerValidationIssue(fieldPath=path, reasonCode=code, requirementKey=requirement))

    digest = None
    if candidate_json is not None:
        digest = sha256(candidate_json.encode("utf-8")).hexdigest()
    try:
        errors = list(Draft202012Validator(schema).iter_errors(data)) if schema is not None and data is not None else []
        for error in errors:
            path = list(error.absolute_path)
            if error.validator == "required" and type(error.instance) is dict:
                for name in error.validator_value:
                    if name not in error.instance:
                        add(safe_path([*path, name]), "SCHEMA_INVALID")
            else:
                add(safe_path(path), "SCHEMA_INVALID")
        if not errors and schema is not None and type(data) is dict:
            if redact_data(data) != data:
                add("", "SECRET_DETECTED")
            tasks, questions, assumptions = (data[name] for name in ("implementationPlan", "questions", "unapprovedAssumptions"))
            texts = [(f"/implementationPlan/{i}/{key}", task[key])
                     for i, task in enumerate(tasks) for key in ("title", "description")]
            texts += [(f"/{key}/{i}", text) for key in ("questions", "unapprovedAssumptions") for i, text in enumerate(data[key])]
            for path, text in texts:
                if not text.strip() or any(ord(c) < 32 and c not in "\n\t" or 127 <= ord(c) <= 159 for c in text) or redact_data(text) != text:
                    add(path, "TEXT_INVALID")
            kind = data["kind"]
            if kind == "PLAN" and not tasks:
                add("/implementationPlan", "PLAN_REQUIRED")
            if kind != "PLAN" and tasks:
                add("/implementationPlan", "PLAN_FORBIDDEN")
            if kind == "INPUT_REQUIRED" and not questions:
                add("/questions", "QUESTIONS_REQUIRED")
            if kind != "INPUT_REQUIRED" and questions:
                add("/questions", "QUESTIONS_FORBIDDEN")
            if assumptions and kind != "INPUT_REQUIRED":
                add("/unapprovedAssumptions", "ASSUMPTIONS_FORBIDDEN")
            counts = Counter(assumptions)
            for i, value in enumerate(assumptions):
                if counts[value] > 1:
                    add(f"/unapprovedAssumptions/{i}", "ASSUMPTION_DUPLICATE")
            if kind == "PLAN":
                ids = Counter(task["taskId"] for task in tasks)
                covered = {value for task in tasks for value in task["requirementIds"]}
                for requirement in scenario.requirements:
                    if str(requirement.requirement_id) not in covered:
                        key = requirement.key if re.fullmatch(r"REQ-[0-9]{3,}", requirement.key) and len(requirement.key) <= 64 else None
                        # Use only the trusted canonical key, never a submitted UUID/text.
                        add("/implementationPlan", "REQUIREMENT_NOT_COVERED", key)
                for i, task in enumerate(tasks):
                    prefix = f"/implementationPlan/{i}"
                    if ids[task["taskId"]] > 1:
                        add(prefix + "/taskId", "TASK_ID_DUPLICATE")
                    for field, code in (("requirementIds", "REQUIREMENT_DUPLICATE"), ("dependsOn", "DEPENDENCY_DUPLICATE")):
                        counts = Counter(task[field])
                        for j, value in enumerate(task[field]):
                            if counts[value] > 1:
                                add(f"{prefix}/{field}/{j}", code)
                    for j, value in enumerate(task["dependsOn"]):
                        if value == task["taskId"]:
                            add(f"{prefix}/dependsOn/{j}", "DEPENDENCY_SELF")
                        elif value not in ids:
                            add(f"{prefix}/dependsOn/{j}", "DEPENDENCY_UNKNOWN")
                if all(count == 1 for count in ids.values()):
                    graph = {task["taskId"]: set(task["dependsOn"]) & ids.keys() for task in tasks}
                    for i, task in enumerate(tasks):
                        start = task["taskId"]
                        pending, visited = list(graph[start]), set()
                        while pending:
                            node = pending.pop()
                            if node == start:
                                add(f"/implementationPlan/{i}/dependsOn", "DEPENDENCY_CYCLE")
                                break
                            if node not in visited:
                                visited.add(node)
                                pending.extend(graph[node] - visited)
        for path in artifact_error_paths:
            add(safe_path(path), "ARTIFACT_SCHEMA_INVALID")
        if not issues:
            fallback = {"CONTRACT_SETUP": "CONTRACT_SETUP_FAILED", "JSON": "JSON_INVALID",
                "MODEL_SCHEMA": "SCHEMA_INVALID", "TEXT": "TEXT_INVALID", "DECISION": "DECISION_INVALID",
                "PLAN_MODEL": "PLAN_ASSEMBLY_INVALID", "REQUIREMENTS": "REQUIREMENT_BINDING_INVALID",
                "ARTIFACT_SAFETY": "SECRET_DETECTED", "ARTIFACT_SCHEMA_LOAD": "HOST_SCHEMA_UNAVAILABLE",
                "ARTIFACT_SCHEMA": "ARTIFACT_SCHEMA_INVALID", "DECISION_MODEL": "DECISION_INVALID"}
            add("", fallback.get(stage, "VALIDATION_INTERNAL_ERROR"))
    except Exception:
        issues = [PlannerValidationIssue(fieldPath="", reasonCode="VALIDATION_INTERNAL_ERROR")]
    return PlannerValidationReport(stage=stage, candidateSha256=digest, issues=tuple(issues[:64]), issueCount=len(issues))
