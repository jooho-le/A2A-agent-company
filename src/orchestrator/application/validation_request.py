"""One exact frozen validation request for initial sends and unsent recovery."""

import json

from orchestrator.domain.states import AgentRole


def build_validation_request(configuration, plan, scenario, requirement_ids, role):
    if role not in (AgentRole.QA, AgentRole.SECURITY):
        raise ValueError("VALIDATION_ROLE_INVALID")
    run_configuration = configuration.to_artifact_json()
    run_configuration.pop("scenarioContract", None)
    run_configuration.pop("frozenScenarioContractJson", None)
    assigned = {str(value) for value in requirement_ids}
    requirements = [item for item in plan.model_dump(mode="json", by_alias=True)["requirements"]
                    if item["requirementId"] in assigned]
    policies = {key: value for key, value in scenario.planner_contract().items()
                if key in {"securityPolicy", "emailPolicy"}}
    duty = ("기능 테스트를 수행하고 QA 결과를 보고한다." if role is AgentRole.QA else
            "보안 취약점을 점검하고 Security 결과를 보고한다.")
    encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return (
        f"workspaceId={configuration.workspace_id}\n"
        + "Run Configuration: " + encode(run_configuration) + "\n"
        + "검증 대상은 전달된 불변 Source Snapshot이다. 파일을 수정하지 말고 "
        "READ_ONLY로 접근한다. 요구사항과 Acceptance Criteria를 확인해 "
        + duty + " 요구사항: " + encode(requirements)
        + "\n\n동결 보안/이메일 정책: " + encode(policies)
        + "\n\n완료 시 A2A Task Artifact를 정확히 하나 반환한다. "
        "QA는 qa-report.json, Security는 security-report.json을 사용하고, "
        "각 Artifact는 application/json Data Part 하나와 project metadata를 "
        "가져야 한다. Report Schema는 "
        "schemas/project/qa_report.schema.json 또는 "
        "schemas/project/security_report.schema.json을 따른다. "
        "Task COMPLETED는 업무 완료일 뿐 결과 PASS를 뜻하지 않는다."
    )
