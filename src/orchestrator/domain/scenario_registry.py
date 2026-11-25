"""Versioned authoritative scenario and requirement definitions for the MVP."""

from dataclasses import dataclass
from enum import Enum
import json
from types import MappingProxyType
from typing import Mapping, Sequence
from uuid import UUID, uuid4
from hashlib import sha256


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
    security_policy_json: str | None = None
    email_policy_json: str | None = None
    # Generated-code connection contract (app, DB dependency, model, API) used by
    # the hidden evaluation suite. Absent in contracts frozen before it existed.
    external_contract_json: str | None = None

    @classmethod
    def from_contract(cls, contract: Mapping[str, object]) -> "ScenarioDefinition":
        """Restore the exact trusted scenario baseline frozen at Run creation."""
        scenario_id = UUID(str(contract["scenarioId"]))
        if scenario_id.version != 4:
            raise ValueError("scenarioId must be UUIDv4")
        requirements = []
        for item in contract["requirements"]:
            requirement_id = UUID(item["requirementId"])
            if requirement_id.version != 4:
                raise ValueError("Requirement IDs must be UUIDv4")
            requirements.append(ScenarioRequirement(
                requirement_id, item["key"], item["category"], item["description"],
                tuple(item["acceptanceCriteria"]),
                tuple(RequirementValidator(value) for value in item["validators"]),
            ))
        if not requirements or len({item.requirement_id for item in requirements}) != len(requirements):
            raise ValueError("frozen scenario requirements must be present and unique")
        return cls(
            scenario_id=scenario_id, key=str(contract["scenarioKey"]),
            name=str(contract["name"]), requirements=tuple(requirements),
            excluded_features=tuple(contract["excludedFeatures"]),
            security_policy_json=json.dumps(contract["securityPolicy"], ensure_ascii=False),
            email_policy_json=json.dumps(contract["emailPolicy"], ensure_ascii=False),
            external_contract_json=(json.dumps(contract["externalContract"], ensure_ascii=False)
                                    if "externalContract" in contract else None),
        )

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
        contract = {
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
            "securityPolicy": {
                "passwordHashPolicy": {
                    "algorithm": "argon2id", "memoryKiB": 19456,
                    "iterations": 2, "parallelism": 1,
                    "uniqueSalt": True, "storage": "encoded-library-hash",
                },
                "secretExposure": "Passwords and password hashes must not appear in API, A2A, logs or Trace.",
            },
            "emailPolicy": {
                "trimWhitespace": True, "domainCase": "lowercase",
                "localPartCase": "lowercase", "gmailDotRemoval": False,
                "plusTagRemoval": False, "canonicalEmailUniqueConstraint": True,
            },
        }
        if self.security_policy_json is not None:
            contract["securityPolicy"] = json.loads(self.security_policy_json)
        if self.email_policy_json is not None:
            contract["emailPolicy"] = json.loads(self.email_policy_json)
        if self.external_contract_json is not None:
            contract["externalContract"] = json.loads(self.external_contract_json)
        return contract

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
# Values are strings or string lists so semantic review can quote them as policy.
# Field names are written as values: secret-looking keys such as "password" are
# redacted by redact_data and would invalidate the frozen contract.
_SCN_001_EXTERNAL_CONTRACT = {
    "app": {
        "module": "main", "attribute": "app", "framework": "FastAPI",
        "serve": "uvicorn main:app", "readyCheck": "GET / 요청에 2xx 응답",
    },
    "database": {
        "module": "database", "dependency": "get_db", "kind": "동기 함수(async 아님)",
        "access": "요청 처리의 DB 접근은 모두 Depends(get_db)로 한다",
        "urlEnv": "DATABASE_URL", "engine": "SQLite에서 동작해야 한다",
    },
    "userModel": {
        "module": "models", "class": "User", "orm": "SQLAlchemy",
        "columns": ["email", "password_hash"], "uniqueInModel": ["email"],
    },
    "signupApi": {
        "method": "POST", "path": "/api/auth/signup", "bodyFormat": "JSON",
        "bodyFields": ["email", "password"], "success": "2xx",
        "rejected": "4xx (입력 검증 실패, 중복)",
    },
    "responseRule": "응답 본문에 password와 password_hash 값을 넣지 않는다",
    "startingFiles": ["main.py", "database.py", "models.py", "tests/test_health.py", "requirements.lock"],
    "packages": "실행 이미지에 설치된 패키지만 사용한다",
}
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
    external_contract_json=json.dumps(_SCN_001_EXTERNAL_CONTRACT, ensure_ascii=False),
)

# Product requests have their own contract. SCN-001 stays the unchanged,
# reproducible experiment; product prose must never be interpreted as signup.
SERVICE_WEB_ID = UUID("03d94bc5-1a31-473b-8527-c4acf8ea58a1")
SERVICE_WEB_KEY = "SERVICE-WEB"
_SERVICE_EXTERNAL = {
    "app": {"module": "main", "attribute": "app", "framework": "FastAPI",
            "serve": "uvicorn main:app", "readyCheck": "GET / returns 2xx",
            "interface": "GET / serves the usable HTML application, not just a health JSON response"},
    "database": {"module": "database", "dependency": "get_db", "urlEnv": "DATABASE_URL",
                 "access": "Use the existing isolated SQLite connection when server persistence is required"},
    "startingFiles": ["main.py", "database.py", "models.py", "tests/test_health.py", "requirements.lock"],
    "packages": "Only installed packages and the standard library. Serve local HTML/CSS/JavaScript with FastAPI; no CDN or network installs.",
    "scope": "Implement the user's requested application. Do not add signup, login or another product unless requested.",
    "verification": "Read the actual source before selecting routes, fields and DOM selectors. Never invent an existing UI.",
    "clarification": "Ask INPUT_REQUIRED when the product goal conflicts or needs a material user decision. Routine implementation details may be chosen within this contract.",
}
_SERVICE_SECURITY = {
    "secretExposure": "Never expose credentials, private data or stored credential hashes in responses, messages or logs.",
    "inputSafety": "Validate user input and test the actual application's access and injection boundaries.",
    "credentialStorage": "If account storage was requested, use the approved argon2id policy; otherwise do not invent accounts.",
}
_SERVICE_WEB = ScenarioDefinition(
    SERVICE_WEB_ID, SERVICE_WEB_KEY, "사용자 요청 웹 애플리케이션 개발",
    (ScenarioRequirement(UUID("80be1440-90c2-4c32-8626-aa2be785a555"), "REQ-001", "FUNCTIONAL",
        "사용자 원문의 기능과 사용자 흐름을 구현한다", ("원문에 명시된 기능이 실제로 동작한다",),
        (RequirementValidator.QA,)),), (),
    security_policy_json=json.dumps(_SERVICE_SECURITY, ensure_ascii=False),
    email_policy_json="{}", external_contract_json=json.dumps(_SERVICE_EXTERNAL, ensure_ascii=False),
)


def service_request_contract(request_text: str) -> dict[str, object]:
    """Freeze the exact user scope before any model runs; no model grants authority.

    Host IDs and bounded verbatim chunks retain every part of the request.
    Planner decomposes these into tasks; all other roles get the same frozen
    criteria. No mutable registry, keyword guess, or later clarification can
    replace this baseline. A new scope needs a new explicitly submitted Run.
    """
    if not isinstance(request_text, str) or not 1 <= len(request_text.strip()) <= 20000:
        raise ValueError("SERVICE_REQUEST_INVALID")
    text = request_text.strip()
    if any((ord(char) < 32 and char not in '\n\t') or 127 <= ord(char) <= 159 for char in text):
        raise ValueError("SERVICE_REQUEST_INVALID")
    requirements = []
    def add(description, criterion, validators, category="FUNCTIONAL"):
        requirements.append({"requirementId": str(uuid4()), "key": f"REQ-{len(requirements)+1:03d}",
            "category": category, "description": description, "acceptanceCriteria": [criterion],
            "validators": [v.value for v in validators]})
    # The context loader caps each criterion at 4096 UTF-8 bytes. One thousand
    # Unicode characters (including four-byte emoji) leave room for the prefix.
    for offset in range(0, len(text), 1000):
        chunk = text[offset:offset+1000].strip()
        if not chunk:
            continue
        add("사용자 원문 요구사항", "요청사항 구현: " + chunk, (RequirementValidator.QA,))
    add("사용할 수 있는 웹 화면 제공", "GET /에서 HTML 화면을 제공하고 요청한 기능을 실제 화면에서 조작할 수 있다",
        (RequirementValidator.QA,))
    add("사용자 입력과 상태 처리", "요청한 기능의 정상·잘못된 입력과 상태 변경이 일관되며 실제 처리 결과를 화면에 표시한다",
        (RequirementValidator.QA,))
    add("요청 범위의 보안 검증", "실제 구현의 입력·데이터 접근 경계를 검사하고 비밀정보를 응답·메시지·로그에 노출하지 않는다",
        (RequirementValidator.SECURITY,), "SECURITY")
    add("실행 근거와 변경 이력", "요구사항·소스 버전·검사·문제·수정·재검증의 연결 관계를 기록한다",
        (RequirementValidator.ORCHESTRATOR,), "OBSERVABILITY")
    contract = _SERVICE_WEB.planner_contract()
    contract["requirements"] = requirements
    contract["externalContract"]["requestIdentity"] = sha256(text.encode("utf-8")).hexdigest()
    # JSON strings remain task data, never system instructions or permissions.
    contract["externalContract"]["userRequest"] = text
    return contract


def validate_service_request_binding(contract: Mapping[str, object], request_text: str) -> None:
    """A saved service scope must belong to this Run's actual user request."""
    if contract.get("scenarioKey") != SERVICE_WEB_KEY and contract.get("scenarioId") != str(SERVICE_WEB_ID):
        return
    external = contract.get("externalContract", {})
    text = request_text.strip()
    if (contract.get("scenarioKey") != SERVICE_WEB_KEY or contract.get("scenarioId") != str(SERVICE_WEB_ID)
            or not isinstance(external, dict) or external.get("userRequest") != text
            or external.get("requestIdentity") != sha256(text.encode("utf-8")).hexdigest()):
        raise ValueError("SERVICE_REQUEST_BINDING_INVALID")


SCENARIO_REGISTRY: Mapping[UUID, ScenarioDefinition] = MappingProxyType(
    {_SCN_001.scenario_id: _SCN_001, _SERVICE_WEB.scenario_id: _SERVICE_WEB}
)


def get_scenario(scenario_id: UUID) -> ScenarioDefinition | None:
    return SCENARIO_REGISTRY.get(scenario_id)
