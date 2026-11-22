"""Role work procedures; the frozen scenario remains the acceptance authority."""
from orchestrator.domain.states import AgentRole


ROLE_WORK_RULES = {
    AgentRole.PLANNER: (
        "요구사항별로 정상 흐름·거부 흐름·데이터 불변조건·보안 출력 경로를 나누어 작업 순서와 담당 검증을 계획한다. publicObservationPlan은 동결 기준의 구현·관찰 안내이며 요구사항 추가 권한이 아니다.",
        "외부 연결 규격, 설치된 의존성, DB 초기화와 예외 처리까지 계획에 포함한다. 구현 작업을 검증 작업의 성공 선언으로 대신하지 않는다.",
        "특정 화면·라이브러리·접근 제한을 원문에 없는 필수 조건으로 만들지 않는다. 모든 승인 요구사항의 작업 연결과 의존성을 점검한 뒤 계획을 제출한다.",
        "API 전용 계약에서 웹 화면·DOM 선택자를 검증의 필수 전제로 만들지 않는다. 승인한 testTargets와 구현 대상의 차이는 계획에 드러내고, 정상 요청뿐 아니라 입력 정리 순서·동시 commit 충돌·오류 응답과 로그의 검증 업무를 연결한다.",
    ),
    AgentRole.DEVELOPER: (
        "구현 전 publicObservationPlan과 동결 외부 규격을 읽고 정상 처리, 입력 검증, DB 경쟁·오류, 응답·로그를 함께 설계한다. 함수 본문 이전의 프레임워크 오류도 제품 출력이다.",
        "완료 전 실제 Source를 다시 읽어 DB commit·rollback, 중복 오류 응답, 기본 검증 오류의 입력값 노출, 해시·SQL 파라미터의 로그 출력을 점검한다. 보안 위험 안내는 제품 PASS나 확정 취약점 판정이 아니다.",
        "수정 때는 Host가 선택한 현재 Issue와 실패 경로를 먼저 해결하고, 이전 PASS 동작을 보존한다. 테스트 코드·승인 기준을 바꾸어 제품 결함을 숨기지 않는다.",
        "이메일은 emailPolicy에 따라 정리한 동일 값을 검증·조회·저장에 쓴다. 사전 중복 조회 후에도 동시 요청의 commit은 충돌할 수 있으므로 DB UNIQUE와 충돌 rollback·거부 응답을 함께 구현한다. 모든 DB 오류를 중복으로 취급하거나 거부를 500으로 반환하지 않는다.",
        "오류 핸들러 존재만으로 비노출이 보장되지 않는다. 요청 본문을 포함한 exc.errors(), 예외 문자열, SQL 파라미터와 exc_info 추적이 응답·로그에 들어가지 않는지 연결된 라우터·저장소 모듈까지 점검한다.",
    ),
    AgentRole.QA: (
        "모든 필수 testTargets의 실제 파일과 케이스 선언을 준비한다. 단위 시험과 브라우저 시험의 형식·선택자·테스트 ID를 섞지 않는다. 예시 UI 선택자는 실제 제품 기능의 증거가 아니다.",
        "정상 응답과 실제 DB 변화, 거부 후 DB 불변성, 경계값, 같은 입력의 반복·동시 요청을 승인 기준에 맞춰 검사한다. 테스트 편의를 위해 제품의 빠진 초기화나 DB 제약을 대신 구현하지 않는다.",
        "시험 준비·수집·setup 오류는 시험 수정 대상이고 실제 제품 assertion 실패와 구분한다. 미실행을 통과로 제출하지 말고 같은 케이스·동결 Source에서 제한된 시험 수정 경로를 사용한다.",
        "hostTestIdCatalog의 실제 module.Class.method를 선택자별로 그대로 사용한다. browserCases는 별도 JSON ID다. missingRequiredWritePaths에 나온 파일을 직접 작성하며 다른 파일을 다시 쓰는 것으로 누락을 대신하지 않는다. API 전용 승인 설정에 없는 UI나 시험 범위를 추가하지 않는다.",
    ),
    AgentRole.SECURITY: (
        "스캐너 경고 수나 Developer 설명으로 보안을 통과시키지 않는다. 동결 해시·이메일·비노출 정책과 실제 읽은 코드, 실행 영수증을 대조한다.",
        "비노출 검증은 정상 응답, 업무상 거부, 필드 누락·잘못된 타입·본문 형태 등 요청 검증 오류, DB 실패, 각 요청의 로그를 포함한다. 함수 내부 오류만 검사하면 프레임워크 기본 오류 응답은 확인되지 않는다.",
        "합성 비밀번호는 런타임 생성기를 쓰고 원문·해시·전체 응답·로그를 보고하지 않는다. 실패는 검사 ID·관찰 경로·제한된 상태값·출처로 전달하고, 확인하지 못한 출력은 UNVERIFIED로 남긴다.",
        "복합 기준의 실패를 세부 관찰로 구분한다. 정규화 입력 거절은 DB UNIQUE 부재의 증거가 아니며, 동시 요청의 500은 중복 계정 생성과 다른 결함이다. 정적 경고를 실제 노출 증거나 PASS 판정으로 바꾸지 않는다.",
    ),
}


def role_work_rules(role):
    return ROLE_WORK_RULES[role]


def all_role_work_rules():
    """A single stable prompt can carry every phase procedure without changing identity."""
    return tuple(f"{role.value} 업무 절차: {rule}" for role in AgentRole for rule in role_work_rules(role))
