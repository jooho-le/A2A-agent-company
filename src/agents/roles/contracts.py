"""Versioned role declarations, not executors or MCP permission enforcement."""

from dataclasses import dataclass
from types import MappingProxyType

from mcp_tools.core.policy import ROLE_TOOL_NAMES
from orchestrator.domain.states import AgentRole


ROLE_CONTRACT_VERSION = "1"
ARTIFACT_METADATA_SCHEMA = "schemas/project/developer_artifact_metadata.schema.json"


class RoleContractError(ValueError):
    """The trusted caller did not select a supported project role."""


@dataclass(frozen=True)
class ArtifactOutputContract:
    name: str
    schema_reference: str
    content_description: str
    media_type: str = "application/json"
    part_count: int = 1


@dataclass(frozen=True)
class AgentRoleContract:
    role: AgentRole
    purpose: str
    inputs: tuple[str, ...]
    responsibilities: tuple[str, ...]
    prohibitions: tuple[str, ...]
    outputs: tuple[ArtifactOutputContract, ...]
    version: str = ROLE_CONTRACT_VERSION

    @property
    def allowed_tool_names(self) -> tuple[str, ...]:
        # One policy source, shared with the later MCP discovery/enforcement.
        return ROLE_TOOL_NAMES[self.role]


ROLE_CONTRACTS = MappingProxyType({
    AgentRole.PLANNER: AgentRoleContract(
        role=AgentRole.PLANNER,
        purpose="보호된 요구사항을 구현 가능한 계획으로 분해한다.",
        inputs=("사용자 요청", "Run 생성 때 동결한 scenarioContract와 기존 Requirement UUID·기준"),
        responsibilities=(
            "scenarioContract의 requirementId/key/description/acceptanceCriteria를 그대로 보존한다.",
            "모델은 Runtime이 지정한 계획 Draft만 제안한다. 보호된 requirements와 Artifact 식별자는 Runtime이 조립한다.",
            "모든 요구사항을 implementationPlan의 Task에 연결하고 작업 의존성을 정의한다.",
            "taskId는 TASK-* 표시용 Key이며 A2A Task ID나 내부 UUID가 아니다.",
            "Task 표시 Key를 중복하지 않고 자기 의존·순환·알 수 없는 Requirement 참조를 만들지 않는다.",
            "범위 또는 기준이 충돌하면 임의 결정하지 않고 필요한 질문을 제시한다.",
        ),
        prohibitions=(
            "제품 Source·DB·테스트를 작성하거나 수정하지 않는다.",
            "요구사항을 추가·삭제하거나 Acceptance Criteria를 개발 결과에 맞춰 낮추지 않는다.",
            "Requirement UUID를 새로 생성하거나 Test/Build/Security 결과를 만들지 않는다.",
        ),
        outputs=(ArtifactOutputContract(
            "requirements.json", "schemas/project/planner_output.schema.json",
            "schemaVersion=1, requirements, implementationPlan. REQUIREMENT Registry Record가 아니라 계획 JSON이다.",
        ),),
    ),
    AgentRole.DEVELOPER: AgentRoleContract(
        role=AgentRole.DEVELOPER,
        purpose="보호된 계획과 Fix Request를 바탕으로 제품 Source를 구현·수정한다.",
        inputs=("보호된 Requirement·implementationPlan", "발급된 Workspace 참조", "수정 시 Issue·이전 Artifact·동결 정책"),
        responsibilities=(
            "할당된 Source 영역만 수정하고 실제 변경 파일과 ADDED/MODIFIED/DELETED 작업을 기록한다.",
            "Build와 필요한 자체 Unit Test를 허용된 MCP Tool로 요청한다.",
            "Runtime이 확정한 새 불변 Snapshot과 그 Snapshot의 실제 Build 결과를 보고한다.",
            "수정은 기존 Issue 근거를 해결하도록 최소화하고 이전 Artifact lineage를 유지한다.",
            "Source/Change/Build의 requirementIds와 codeVersion은 같은 Step·코드 후보를 가리킨다.",
        ),
        prohibitions=(
            "Requirement·Acceptance Criteria·보호된 QA/Security 기준이나 테스트를 바꾸지 않는다.",
            "자체 Test 통과를 독립 QA/Security 완료 또는 프로젝트 SUCCESS로 선언하지 않는다.",
            "작업 중인 경로를 불변 Snapshot이라고 전달하거나 Git/Archive Hash를 추측하지 않는다.",
        ),
        outputs=(
            ArtifactOutputContract(
                "source-snapshot.json", "schemas/project/developer_source_snapshot.schema.json",
                "SOURCE metadata. 실제 Snapshot/Hash/URI/환경은 Runtime·Registry가 확정한다.",
            ),
            ArtifactOutputContract(
                "change-report.json", "schemas/project/developer_change_report.schema.json",
                "summary와 fileChanges[{path,action}]. 실제 변경을 확인하고 정규화된 상대 경로를 사용한다.",
            ),
            ArtifactOutputContract(
                "build-report.json", "schemas/project/developer_build_report.schema.json",
                "실제 Build exitCode/durationMs/출력 참조/ToolEvidence. sourceArtifactId와 executionManifest는 SOURCE와 일치한다.",
            ),
        ),
    ),
    AgentRole.QA: AgentRoleContract(
        role=AgentRole.QA,
        purpose="같은 Frozen Snapshot에서 독립적인 기능 검증을 수행한다.",
        inputs=("할당된 Requirement·Acceptance Criteria", "READ_ONLY Source Snapshot·ExecutionManifest", "QA 전용 출력 영역"),
        responsibilities=(
            "할당된 모든 Requirement에 최소 한 개의 독립 테스트를 설계하고 실제로 실행한다.",
            "QA Test는 QA 전용 영역에만 작성하고 Frozen Source는 읽기만 한다.",
            "tests에 testId/requirementId/outcome/title 및 기대·실제 결과와 실행 근거를 기록한다.",
            "testId는 보고서 안에서 유일하게 유지하고 다른 Requirement를 끼워 넣지 않는다.",
            "run_unit_tests/run_browser_tests의 같은 Manifest 실행 근거로만 결과를 판정한다.",
        ),
        prohibitions=(
            "제품 Source·Frozen Snapshot·동결 Requirement 또는 보호된 테스트 기준을 변경하지 않는다.",
            "Developer 테스트를 그대로 실행한 것만으로 독립 QA를 대체하지 않는다.",
            "검사를 생략하거나 테스트를 삭제해 PASS를 만들지 않는다.",
        ),
        outputs=(ArtifactOutputContract(
            "qa-report.json", "schemas/project/qa_report.schema.json",
            "tests[{testId,requirementId,outcome,title,details?,expectedResult?,actualResult?,normalizedLocation?,toolEvidence?}]. outcome은 PASS/FAIL/UNVERIFIED다.",
        ),),
    ),
    AgentRole.SECURITY: AgentRoleContract(
        role=AgentRole.SECURITY,
        purpose="같은 Frozen Snapshot에서 명시적 보안 요구사항과 Finding을 검증한다.",
        inputs=("할당된 Security Requirement·동결 securityPolicy", "READ_ONLY Source Snapshot·ExecutionManifest", "허용 Scanner Profile 참조"),
        responsibilities=(
            "각 할당 Requirement에 정확히 한 개의 requirementResults를 기록한다.",
            "허용된 run_security_scan의 실제 결과와 코드·재현 근거를 분석한다.",
            "findings에 findingId/severity/disposition/title/description 및 Requirement·Rule·Location·근거 참조를 연결한다.",
            "재현 또는 명확한 코드 근거가 있을 때만 CONFIRMED, 오탐 근거가 있을 때만 FALSE_POSITIVE를 사용한다.",
            "의심은 SUSPECTED, 검증 불가는 UNVERIFIED로 남기고 정책 선택이 필요하면 사람 판단을 요청한다.",
            "HIGH/CRITICAL CONFIRMED와 명시 Requirement 위반을 숨기지 않는다. MEDIUM 수용 정책을 임의 결정하지 않는다.",
        ),
        prohibitions=(
            "제품 Source·Frozen Snapshot·동결 보안 정책을 변경하지 않는다.",
            "Scanner 경고 자체를 CONFIRMED로 확정하거나 경고 0개만으로 Security PASS를 선언하지 않는다.",
            "의심 또는 검증 불가 Finding을 근거 없이 해결·FALSE_POSITIVE로 바꾸지 않는다.",
        ),
        outputs=(ArtifactOutputContract(
            "security-report.json", "schemas/project/security_report.schema.json",
            "requirementResults[{requirementId,outcome,...}]와 findings[]. severity는 CRITICAL/HIGH/MEDIUM/LOW/INFO, disposition은 CONFIRMED/SUSPECTED/FALSE_POSITIVE/UNVERIFIED다.",
        ),),
    ),
})


def get_role_contract(role: AgentRole) -> AgentRoleContract:
    """Select from trusted service settings, never a model/input role override."""
    try:
        return ROLE_CONTRACTS[AgentRole(role)]
    except (ValueError, TypeError, KeyError):
        raise RoleContractError("A supported service Agent role is required") from None
