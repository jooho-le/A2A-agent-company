"""Controlled faults at explicit boundaries; never applied to production work."""

from uuid import uuid4


def is_target(row):
    return row.role in {row.fault_target, "SINGLE_AGENT"}


def alter_request(wire, row):
    if not is_target(row):
        return None
    payload = wire["message"]["parts"][0]["data"]
    scenario = row.scenario
    doc = payload.get("workRequest")
    if scenario == "request_change":
        payload["sample"]["value"] += 1
        return "수신 미들웨어: sample.value + 1"
    if scenario == "metadata_change":
        wire["metadata"]["requirementIds"][0] = str(uuid4())
        return "수신 미들웨어: A2A requirementIds 한 개 변경"
    if scenario == "message_id_change":
        wire["message"]["messageId"] = str(uuid4())
        return "수신 미들웨어: messageId를 다른 유효 UUID로 변경"
    if doc is None:
        return None
    if scenario == "request_truncate":
        doc["brief"] = doc["brief"][:-120]
        return "수신 미들웨어: 긴 사용자 요청 마지막 120자 삭제"
    if scenario == "requirement_drop":
        doc["requirements"].pop(len(doc["requirements"]) // 2)
        return "수신 미들웨어: 중간 요구사항 1개 삭제 (sample 개수는 유지)"
    if scenario == "array_reorder":
        doc["acceptanceCases"].reverse()
        return "수신 미들웨어: 합격 기준 배열 순서 역전"
    if scenario == "type_change":
        doc["constraints"]["deployAllowed"] = "false"
        return "수신 미들웨어: boolean false → 문자열 false"
    if scenario == "unicode_change":
        doc["project"]["customerNote"] = doc["project"]["customerNote"].replace("가", "가", 1)
        return "수신 미들웨어: 시각적으로 비슷한 한글 조합 코드포인트 변경"
    return None


def alter_agent_claims(result, row):
    if not is_target(row):
        return
    claims, scenario = result["claims"], row.scenario
    spec = claims.get("specification")
    if scenario == "grounding_error":
        claims["value"] += 1
        claims["deploymentSucceeded"] = True
        row.injection = "목업 실행기: 틀린 수치와 근거 없는 배포 성공 주장"
    elif scenario == "foreign_artifact":
        result["probeId"] = str(uuid4())
        row.injection = "목업 실행기: 다른 Probe ID의 Artifact 생성"
    elif spec is not None:
        if scenario == "response_requirement_drop":
            spec["coverageIds"].pop(len(spec["coverageIds"]) // 2)
            row.injection = "목업 실행기: 응답에서 요구사항 누락 (기본 숫자·개수 주장은 유지)"
        elif scenario == "response_limit_change":
            spec["constraints"]["maxUploadBytes"] += 1
            row.injection = "목업 실행기: 20MiB 제한을 1바이트 증가"
        elif scenario == "permission_swap":
            matrix = spec["accessMatrix"]
            allowed = next(item for item in matrix if item["allowed"] is True)
            denied = next(item for item in matrix if item["allowed"] is False)
            allowed["allowed"], denied["allowed"] = False, True
            row.injection = "목업 실행기: 허용·거절 대상 교환 (전체 허용 개수는 같음)"
        elif scenario == "invented_evidence":
            spec["executionEvidence"]["build"] = "PASS"
            spec["executionEvidence"]["receiptId"] = "fabricated-build-receipt"
            row.injection = "목업 실행기: 실제 미실행 Build를 PASS로 주장하고 가짜 영수증 추가"
        elif scenario == "invented_decision":
            spec["unresolvedQuestionIds"] = []
            row.injection = "목업 실행기: 미결정 질문을 임의로 해결한 것처럼 누락"
