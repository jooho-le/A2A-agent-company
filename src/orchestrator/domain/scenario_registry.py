"""Versioned authoritative scenario and requirement definitions for the MVP."""

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Sequence
from uuid import UUID


class RequirementValidator(str, Enum):
    QA = "QA"
    SECURITY = "SECURITY"
    ORCHESTRATOR = "ORCHESTRATOR"


@dataclass(frozen=True)
class ScenarioRequirement:
    requirement_id: UUID
    key: str
    category: str
    description: str
    acceptance_criteria: tuple[str, ...]
    validators: tuple[RequirementValidator, ...]


@dataclass(frozen=True)
class ScenarioDefinition:
    scenario_id: UUID
    key: str
    name: str
    requirements: tuple[ScenarioRequirement, ...]
    excluded_features: tuple[str, ...]

    @property
    def requirement_ids(self) -> tuple[UUID, ...]:
        return tuple(requirement.requirement_id for requirement in self.requirements)

    def requirement_ids_for(self, validator: RequirementValidator) -> tuple[UUID, ...]:
        return tuple(
            requirement.requirement_id
            for requirement in self.requirements
            if validator in requirement.validators
        )

    def planner_contract(self) -> dict[str, object]:
        """Return immutable acceptance criteria as the Planner's trusted input."""
        return {
            "scenarioId": str(self.scenario_id),
            "scenarioKey": self.key,
            "name": self.name,
            "requirements": [
                {
                    "requirementId": str(requirement.requirement_id),
                    "key": requirement.key,
                    "category": requirement.category,
                    "description": requirement.description,
                    "acceptanceCriteria": list(requirement.acceptance_criteria),
                    "validators": [validator.value for validator in requirement.validators],
                }
                for requirement in self.requirements
            ],
            "excludedFeatures": list(self.excluded_features),
        }

    def validate_planner_requirements(self, requirements: Sequence[object]) -> None:
        """Reject any omitted, added, or weakened requirement in Planner output."""
        received = {getattr(item, "requirement_id", None): item for item in requirements}
        expected_ids = set(self.requirement_ids)
        if len(received) != len(requirements) or set(received) != expected_ids:
            raise ValueError("Planner requirements must exactly match the Scenario Registry")
        expected = {item.requirement_id: item for item in self.requirements}
        for requirement_id, actual in received.items():
            canonical = expected[requirement_id]
            if (
                getattr(actual, "key", None) != canonical.key
                or getattr(actual, "description", None) != canonical.description
                or tuple(getattr(actual, "acceptance_criteria", ()))
                != canonical.acceptance_criteria
            ):
                raise ValueError(
                    f"Planner changed canonical Requirement {canonical.key} or its acceptance criteria"
                )


SCN_001_ID = UUID("f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76")
_SCN_001 = ScenarioDefinition(
    scenario_id=SCN_001_ID,
    key="SCN-001",
    name="회원가입 기능 자동 개발·검증",
    requirements=(
        ScenarioRequirement(
            UUID("f2bf2881-d327-4e67-aa24-45fda73e34a9"),
            "REQ-001",
            "FUNCTIONAL",
            "유효한 이메일과 비밀번호로 계정을 생성할 수 있어야 함",
            ("정상 입력 시 사용자 1건 생성 및 성공 응답",),
            (RequirementValidator.QA,),
        ),
        ScenarioRequirement(
            UUID("b5e5c035-a4d1-43e6-9f97-d51b84f022dd"),
            "REQ-002",
            "FUNCTIONAL",
            "잘못된 이메일 형식을 거부해야 함",
            ("합의한 이메일 파서에서 invalid인 값이 DB에 저장되지 않고 오류 응답",),
            (RequirementValidator.QA,),
        ),
        ScenarioRequirement(
            UUID("c74fdf11-fe18-4664-9e19-db2e43d0127e"),
            "REQ-003",
            "FUNCTIONAL_DATA",
            "동일한 canonical email로 중복 계정을 만들 수 없어야 함",
            (
                "애플리케이션 사전검사와 별개로 DB UNIQUE 제약이 존재하며 "
                "동시 요청에서도 중복 생성 불가",
            ),
            (RequirementValidator.QA, RequirementValidator.SECURITY),
        ),
        ScenarioRequirement(
            UUID("c1f65c66-a2b6-47f0-8c76-d7ff06133ce3"),
            "REQ-004",
            "FUNCTIONAL",
            "비밀번호는 최소 8자 이상이어야 함",
            ("7자 이하는 거부, 8자 이상 정상 처리",),
            (RequirementValidator.QA,),
        ),
        ScenarioRequirement(
            UUID("e3d8975a-5a3f-433e-8da5-e98b9c559166"),
            "REQ-005",
            "SECURITY",
            "비밀번호는 승인된 Password Hash 정책으로 저장해야 함",
            ("승인한 알고리즘·파라미터 충족, 평문/일반 SHA 계열 저장 없음",),
            (RequirementValidator.SECURITY,),
        ),
        ScenarioRequirement(
            UUID("df6ac4a0-7f90-4783-939f-4d3b568b5c39"),
            "REQ-006",
            "SECURITY",
            "평문 비밀번호와 Password Hash가 API 응답, A2A Message/Artifact, 일반 로그, Trace에 노출되지 않아야 함",
            ("지정 출력 전체에서 비밀번호 및 저장 Hash 미노출",),
            (RequirementValidator.SECURITY,),
        ),
        ScenarioRequirement(
            UUID("ac22dde4-83ac-45ab-b6dc-dd51b8984531"),
            "REQ-007",
            "FUNCTIONAL",
            "실제 가입 처리 결과와 사용자 응답이 일치해야 함",
            ("성공은 성공으로, 검증/중복 실패는 실패로 응답",),
            (RequirementValidator.QA,),
        ),
        ScenarioRequirement(
            UUID("4876a535-23e1-4059-941f-bf49441f2276"),
            "REQ-008",
            "OBSERVABILITY",
            "요구사항→개발→검증→Issue→수정→재검증을 추적할 수 있어야 함",
            ("Trace를 통해 전체 연결 관계 복원 가능",),
            (RequirementValidator.ORCHESTRATOR,),
        ),
    ),
    excluded_features=(
        "로그인", "OAuth", "이메일 인증", "비밀번호 찾기", "프로필 관리",
    ),
)

SCENARIO_REGISTRY: Mapping[UUID, ScenarioDefinition] = MappingProxyType(
    {_SCN_001.scenario_id: _SCN_001}
)


def get_scenario(scenario_id: UUID) -> ScenarioDefinition | None:
    return SCENARIO_REGISTRY.get(scenario_id)
