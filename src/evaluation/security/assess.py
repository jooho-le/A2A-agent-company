# 팀 시스템(dev 42번 문서)의 "승인된 보안 검증기" 진입 파일이다.
# 시스템(Host)이 요청 하나(SecurityAssessmentRequest)를 넘기면, 이 파일이 판정 결과(SecurityAssessment)를 돌려준다.
#
# 이 파일이 지키는 규칙:
#  - 요구사항을 판정할 때는 "승인된 기준(request.criteria)"과 "동결된 시나리오 정책(request.scenario_contract)"만 쓴다.
#    (판정 기준이 몰래 바뀌지 않도록, 요청에 담겨 온 승인된 값만 믿는다.)
#  - 에이전트가 만든 코드는 실행하거나 import하지 않는다. 글자 그대로 읽기만 한다(static_analysis.py가 담당).
#  - 증거로 가리킬 수 있는 곳은 시스템이 허용해 준 artifact:// 주소 목록뿐이다. 그 밖의 주소는 쓰지 않는다.
import json
from urllib.parse import quote  # 경로에 들어 있는 특수문자를 주소에 쓸 수 있는 글자로 바꾸는 도구

from . import static_analysis as sa  # 같은 폴더의 static_analysis.py(실제 코드 분석)를 sa라는 짧은 이름으로 쓴다.

# 요구사항 번호 → 그 요구사항을 검사하는 함수. (lambda는 이름 없는 한 줄짜리 함수)
#   REQ-003(이메일 중복 방지)   → check_duplicate_email
#   REQ-005(비밀번호 저장 방식) → check_password_storage (시나리오 정책의 해시 기준을 함께 넘긴다)
#   REQ-006(비밀번호 노출 금지) → check_exposure
CHECKERS = {
    'REQ-003': lambda modules, criteria, contract: sa.check_duplicate_email(modules, criteria),
    'REQ-005': lambda modules, criteria, contract: sa.check_password_storage(
        modules, criteria, (contract.get('securityPolicy') or {}).get('passwordHashPolicy')),
    'REQ-006': lambda modules, criteria, contract: sa.check_exposure(modules, criteria),
}
MAX_REFS = 256  # 한 번에 돌려줄 수 있는 증거 주소의 최대 개수(시스템이 정한 한도)


def evaluate(request):
    # 시스템의 보안 확인 절차 없이도 쓸 수 있는 "순수 판정 함수"이다. 오류가 나면 그대로 예외를 낸다(디버깅에 편함).
    # 돌려주는 값은 SecurityAssessment를 만들 때 쓰는 인자들을 담은 사전(dict)이다.
    criteria, contract = request.criteria, request.scenario_contract
    # {요구사항 UUID: 표시 이름(REQ-005 등)} 표를 만든다.
    keys = {str(item['requirementId']): item['key'] for item in contract.get('requirements', [])}
    if (not isinstance(criteria, dict) or criteria.get('schemaVersion') != 1
            or criteria.get('scenarioKey') != contract.get('scenarioKey')):
        # 승인 기준이 이 시나리오의 것이 아니면 아무 요구사항도 판정하지 않고 "기준 불일치"만 기록해서 돌려준다.
        return _result(request, {}, [], {'criteriaId': None, 'error': 'CRITERIA_SCENARIO_MISMATCH'}, [])
    # 소스 파일들을 읽어서 분석할 수 있는 형태(modules)로 바꾼다. 문법 오류 파일은 parse_errors에 따로 모인다.
    modules, parse_errors = sa.load_modules(request.source_files, criteria)
    checks, outcomes = {}, []
    for requirement_id in request.requirement_ids:
        key = keys.get(str(requirement_id))
        if key not in CHECKERS:
            continue  # 이 검증기가 다루지 않는 요구사항(예: 기능 검사)은 건너뛴다.
        result = CHECKERS[key](modules, criteria, contract)
        if parse_errors and result.outcome == sa.PASS:
            # 분석하지 못한 파일이 있었다면 "문제 없음"이라고 단정할 수 없으므로 PASS를 UNVERIFIED로 낮춘다.
            result = sa.CheckOutcome(sa.UNVERIFIED, 'SOURCE_PARSE_ERROR', result.observations)
        checks[key] = result
        outcomes.append((requirement_id, key, result))
    # Bandit(보안 점검 도구)이 찾아낸 경고들을 코드 근거로 하나씩 판정한다. 경고가 없으면 건너뛴다.
    findings = _scan_findings(request)
    dispositions = sa.dispose_findings(modules, criteria, findings) if findings else []
    # 증거로 남길 "관찰 기록". 소스 글자나 비밀값은 넣지 않고 경로·줄·종류·설정 숫자만 담는다.
    observations = {
        'criteriaId': criteria.get('criteriaId'), 'analyzer': criteria.get('analyzer'),
        'parseErrors': parse_errors[:sa.MAX_OBSERVATIONS], 'analyzedFiles': len(modules),
        'requirements': {key: {'checkId': criteria.get('checks', {}).get(key), **result.to_dict()}
                         for key, result in checks.items()},
        'findings': [{'findingId': finding_id, 'disposition': value, **observation.to_dict()}
                     for finding_id, value, observation in dispositions],
    }
    return _result(request, outcomes, dispositions, observations, list(modules))


def _scan_findings(request):
    # Bandit 경고 목록을 시스템이 준 경고 번호(finding_ids)와 짝지어 돌려준다.
    # 시스템이 준 번호의 순서는 scans 보고서의 findings 순서와 같다(dev의 security_verifier가 그렇게 만든다).
    items = [finding for scan in request.scans for finding in scan.get('report', {}).get('findings', [])]
    if len(items) != len(request.finding_ids):
        return []  # 개수가 맞지 않으면 짝을 지을 수 없으므로 하나도 판정하지 않는다.
    return list(zip(request.finding_ids, items))  # zip: 두 목록을 같은 순서끼리 묶는다.


def _result(request, outcomes, dispositions, observations, analyzed_paths):
    # 판정 결과를 시스템이 요구하는 모양으로 조립한다. 특히 "증거 주소"를 붙이는 일을 한다.
    # 규칙: PASS나 FAIL 같은 확정 판정에는 시스템이 허용한 증거 주소가 반드시 하나 이상 붙어야 한다.
    #       붙일 수 있는 증거가 없으면 확정하지 않고 UNVERIFIED로 낮춘다.
    permitted = list(request.permitted_evidence_refs)  # 시스템이 허용한 증거 주소 목록
    requirement_outcomes, refs = [], []
    for requirement_id, _key, result in outcomes:
        # 관찰 기록에 나온 파일들의 증거 주소를 찾는다. 관찰된 파일이 없으면 분석한 모든 파일을 증거로 삼는다.
        cited = _refs_for(permitted, [item.path for item in result.observations] or analyzed_paths)
        if result.outcome != sa.UNVERIFIED and not cited:
            requirement_outcomes.append((requirement_id, sa.UNVERIFIED))
            continue
        requirement_outcomes.append((requirement_id, result.outcome))
        refs.extend(cited)
    finding_dispositions = []
    for finding_id, value, observation in dispositions:
        cited = _refs_for(permitted, [observation.path])
        if cited:  # 증거 주소를 붙일 수 있는 경고만 판정에 포함한다.
            finding_dispositions.append((finding_id, value))
            refs.extend(cited)
    # dict.fromkeys(...)로 중복을 없애고(순서는 유지), 한도(MAX_REFS)까지만 남긴다.
    unique_refs = tuple(dict.fromkeys(refs))[:MAX_REFS]
    return {'request_id': request.request_id, 'request_sha256': request.request_sha256,
            'approval_sha256': request.approval_sha256,
            'requirement_outcomes': tuple(requirement_outcomes),
            'finding_dispositions': tuple(finding_dispositions),
            'evidence_refs': unique_refs,
            'evidence_json': json.dumps(observations, ensure_ascii=False, sort_keys=True)}


def _refs_for(permitted, paths):
    # 소스 파일 경로들을, 시스템이 허용한 증거 주소("artifact://<번호>/source.tar#path=<파일 경로>") 중 해당하는 것으로 바꾼다.
    # 허용 목록에 없는 주소를 우리가 새로 만들어 내지 않고, 목록에서 "골라서" 쓴다.
    wanted = {'#path=' + quote(path, safe='/') for path in paths}
    return [ref for ref in permitted if any(ref.endswith(suffix) for suffix in wanted)]


def assess(request):
    # 시스템이 호출하도록 승인 등록되는 "최상위 함수"이다(시스템 규칙: 인자 하나를 받는 최상위 함수여야 한다).
    # 내부에서 어떤 오류가 나도 판정을 지어내지 않고 None(= 미검증)을 돌려준다.
    # 원인을 알고 싶을 때는 같은 요청으로 위의 evaluate()를 직접 호출해서 예외 메시지를 확인한다.
    # SecurityAssessment 클래스는 시스템 쪽 파일에 있어서, 필요한 순간에 함수 안에서 가져온다.
    from agents.platform.security_verifier import SecurityAssessment
    try:
        return SecurityAssessment(**evaluate(request))  # ** 는 사전의 내용을 이름 붙은 인자로 풀어서 넘긴다.
    except Exception:
        return None
