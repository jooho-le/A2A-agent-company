"""Offline report derivation. Never calls a model or rewrites raw evidence."""

from datetime import datetime
from hashlib import sha256
from html import escape
import json
from math import isfinite
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo


ROLE_LABELS = {"PLANNER": "기획", "DEVELOPER": "개발", "QA": "QA", "SECURITY": "보안", "SINGLE_AGENT": "싱글"}
CASE_LABELS = {"01-blog": "블로그", "02-documents": "전자문서", "03-mystery": "추리게임"}


def _all(rows, key):
    if key == "modelInput":
        return [(row.get("model") or {}).get("apiInputMatches") for row in rows]
    if key == "receipt":
        return [(row.get("delivery") or {}).get("check", {}).get("status") for row in rows]
    return [row.get("checks", {}).get(key, {}).get("status") for row in rows]


def _counts(values, pass_value="PASS"):
    return {"passed": sum(value == pass_value for value in values),
            "failed": sum(value in ("FAIL", False) for value in values if value != pass_value),
            "unverified": sum(value not in (pass_value, "FAIL", False) for value in values)}


def summarize_evidence(evidence):
    rows = [row for run in evidence.get("runs", []) for row in run.get("rows", [])]
    calls = evidence.get("calls", [])
    answers = [(row, (row.get("observations", {}).get("responseReceived") or {}).get("answer")) for row in rows]
    boundaries = {name: _counts([row.get("checks", {}).get("request", {}).get(name, {}).get("status") for row in rows])
                  for name in ("serialized", "network", "admission", "executor", "endToEnd")}
    boundaries.update({name: _counts(_all(rows, name)) for name in ("receipt", "response", "metadata", "responseMetadata", "sourceGrounding")})
    boundaries["modelInput"] = _counts(_all(rows, "modelInput"), True)
    roles = {}
    for role in ROLE_LABELS:
        group = [row for row in rows if row["role"] == role]
        timings = {}
        for name in ("agentProcessingMs", "providerExchangeMs", "requestTransferMs", "admissionAndSchedulingMs", "resultObservationMs"):
            values = [row.get("timings", {}).get(name) for row in group if row.get("phase") == "COMPLETED"]
            values = [value for value in values if type(value) in (int, float) and isfinite(value) and value >= 0]
            timings[name] = {"samples": len(values), "min": min(values) if values else None,
                             "median": median(values) if values else None, "max": max(values) if values else None}
        roles[role] = timings
    execution_claims = [answer.get("executionState") for _, answer in answers if isinstance(answer, dict)]
    foreign = sum(bool(text.strip()) for row, answer in answers if isinstance(answer, dict) and row["role"] != "SINGLE_AGENT"
                  for name, text in answer.get("roleWork", {}).items() if name != row["role"] and isinstance(text, str))
    violations = [item for row in rows for item in row.get("checks", {}).get("sourceGrounding", {}).get("differences", [])]
    return {"plannedCalls": evidence.get("maxModelCalls"), "calls": len(calls), "rows": len(rows),
        "http200": sum(call.get("httpStatus") == 200 for call in calls),
        "providerRequestIds": sum(bool(call.get("providerRequestId")) for call in calls),
        "completedRows": sum(row.get("phase") == "COMPLETED" for row in rows),
        "skippedRows": sum(row.get("phase") == "SKIPPED" for row in rows),
        "eligibleRows": sum(row.get("handoff", {}).get("eligible") is True for row in rows),
        "boundaries": boundaries, "roles": roles, "parsedAnswers": len(execution_claims),
        "notExecutedClaims": sum(value == "NOT_EXECUTED" for value in execution_claims),
        "foreignRoleFields": foreign, "invariantViolations": len(violations),
        "realTransport": evidence.get("provenance", {}).get("transportMode") == "DEFAULT_HTTPS_TRANSPORT",
        "inputTokens": sum((call.get("usage") or {}).get("input_tokens", 0) for call in calls),
        "outputTokens": sum((call.get("usage") or {}).get("output_tokens", 0) for call in calls),
        "reusedClients": sum((row.get("transport") or {}).get("clientReused") is True for row in rows),
        "cachedCards": sum((row.get("transport") or {}).get("agentCardCached") is True for row in rows)}


def compare_baseline(current, baseline):
    def recorded_equal(left, right):
        return None if left is None or right is None else left == right
    return {"sameModel": recorded_equal(current.get("modelId"), baseline.get("modelId")),
            "sameRequests": recorded_equal(current.get("caseRequests"), baseline.get("caseRequests")),
            "sameTemperature": recorded_equal(current.get("temperature"), baseline.get("temperature")),
            "sameOutputCap": recorded_equal(current.get("maxOutputTokens"), baseline.get("maxOutputTokens")),
            "samePrompts": recorded_equal(current.get("rolePrompts"), baseline.get("rolePrompts")),
            "sameSchemas": recorded_equal(current.get("schemaSha256ByRole", current.get("schemaSha256")), baseline.get("schemaSha256ByRole", baseline.get("schemaSha256"))),
            "causalSpeedupAllowed": False,
            "reason": "SINGLE_TRIAL_DIAGNOSTIC_WITH_CHANGED_CONTRACTS_AND_UNCONTROLLED_PROVIDER_LOAD"}


def validate_review(evidence, review):
    """Catch accidental invented excerpts in the offline editorial review."""
    for item in review.get("caseReviews", []):
        source = evidence.get("caseRequests", {}).get(item.get("caseId"))
        if source is None or item.get("sourceQuote") and item["sourceQuote"] not in source:
            raise ValueError("REPORT_REVIEW_SOURCE_QUOTE_NOT_FOUND")
        quote = item.get("answerQuote")
        if not quote:
            continue
        answers = [(row.get("observations", {}).get("responseReceived") or {}).get("answer")
            for run in evidence.get("runs", [])
            if run.get("caseId") == item["caseId"] and run.get("architecture") == item.get("architecture")
            for row in run.get("rows", []) if row.get("role") == item.get("role")]
        def texts(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for part in value.values():
                    yield from texts(part)
            elif isinstance(value, list):
                for part in value:
                    yield from texts(part)
        if not any(quote in text for answer in answers for text in texts(answer)):
            raise ValueError("REPORT_REVIEW_ANSWER_QUOTE_NOT_FOUND")


def _text(value):
    return escape(str(value), quote=True)


def _number(value, *, seconds=False):
    if value is None:
        return "미관측"
    return f"{value / 1000:.3f}초" if seconds else f"{value:.3f}ms"


def _status(value):
    return "일치" if value in ("PASS", True) else "불일치" if value in ("FAIL", False) else "미관측"


def _kst(value):
    return datetime.fromisoformat(value).astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M:%S KST") if value else "미관측"


def _table(headers, rows):
    return "<table><thead><tr>" + "".join("<th>" + _text(value) + "</th>" for value in headers) + "</tr></thead><tbody>" + "".join(
        "<tr>" + "".join("<td>" + _text(value) + "</td>" for value in row) + "</tr>" for row in rows) + "</tbody></table>"


def render_html(evidence, *, raw_path, raw_sha256, review, baseline=None, baseline_path=None, baseline_sha256=None):
    """Six printable pages; the caller may retain only the resulting PDF."""
    validate_review(evidence, review)
    summary = summarize_evidence(evidence)
    groups = evidence.get("runs", [])
    rows = [row for group in groups for row in group.get("rows", [])]
    pages = []
    def page(title, subtitle, body):
        index = len(pages) + 1
        date = _kst(evidence.get('startedAt')).split(' ')[0].replace('-', '.')
        pages.append(f'<section class="page"><header><span>ATELIER / COMMUNICATION REVALIDATION</span><b>{_text(date)}</b></header><h1>{_text(title)}</h1><p class="subtitle">{_text(subtitle)}</p>{body}<footer><span>실제 통신 진단 · 제품 개발 성공/일반 할루시네이션 부재를 뜻하지 않음</span><b>{index} / 6</b></footer></section>')
    total = summary["rows"]
    cards = [("01 · 보내고 받은 내용", f'원문→실행기 {summary["boundaries"]["endToEnd"]["passed"]}/{total} · DB 접수 {summary["boundaries"]["receipt"]["passed"]}/{total}',
              "전송 불일치와 관측 부족을 구분합니다. 응답 내용의 정확성과는 별도입니다."),
             ("02 · 할루시네이션/역할", f'자동 불변조건 위반 {summary["invariantViolations"]}건 · 다른 역할 필드 {summary["foreignRoleFields"]}개',
              review.get("conclusion", "자동 형식·인용 검사 통과만으로 의미 오류가 없다고 판단하지 않습니다.")),
             ("03 · 속도 차이", "로컬 전달 · API 교환 · 결과 확인을 분리", "역할별 작업량·출력 길이가 다르므로 단순한 배율을 구조 성능 차이로 해석하지 않습니다.")]
    body = '<div class="lead">'+_text("실제 OpenAI API 실행 결과" if summary["realTransport"] else "사용자 지정 Transport 자료 · 실제 API 결과로 확정하지 않음")+'</div>'
    body += ''.join('<article class="card"><span>'+_text(title)+'</span><h2>'+_text(value)+'</h2><p>'+_text(note)+'</p></article>' for title,value,note in cards)
    body += _table(["확인 항목", "관측 결과"], [
        ["호출 / HTTP 200", f'{summary["calls"]}회 / {summary["http200"]}회'],
        ["서버 Request ID / 후속 전달 허용", f'{summary["providerRequestIds"]}개 / {summary["eligibleRows"]}행'],
        ["미실행 상태 선언", f'{summary["notExecutedClaims"]}/{summary["parsedAnswers"]}개 응답이 NOT_EXECUTED'],
        ["실행 구간", _kst(evidence.get("startedAt"))+" ~ "+_kst(evidence.get("finishedAt"))]])
    body += '<p class="note">이번 실험은 자연어 해석·인계 진단입니다. 제품 Host 전체 흐름, 코드 생성·MCP·Docker·빌드·실제 QA/보안 스캔은 실행하지 않았습니다.</p>'
    page("보강 후, 다시 확인한 세 가지", "먼저 결론을 읽고 다음 페이지에서 요청·근거·시간을 확인하세요.", body)

    body = ''.join('<article class="request"><h2>'+_text(CASE_LABELS.get(key,key))+'</h2><p>'+_text(value)+'</p></article>' for key,value in evidence.get("caseRequests",{}).items())
    body += _table(["설정", "실행 방법"], [
        ["모델", evidence.get("modelId")], ["온도 / 출력 상한 / 호출 제한", f'{evidence.get("temperature")} / {evidence.get("maxOutputTokens")} tokens / 최대 {evidence.get("maxModelCalls")}회'],
        ["구조", "케이스마다 Multi 4역할 + Single 1역할. Multi는 기획→개발→QA·보안 병렬"],
        ["모델에 준 자료", "위 자연어 원문 + 일반 역할 지시 + 이전 역할의 실제 결과. 평가 기준/정답은 제외"],
        ["재호출·순서", "자동 Retry 없음. 실행 순서 Multi→Single 고정, 추가 유료 평가 모델 없음"]])
    page("어떤 문장으로 실행했나", "실험에 사용한 자연어 원문 전체입니다. JSON 요구사항 목록을 대신 입력하지 않았습니다.", body)

    transport_rows=[]
    for group in groups:
        for row in group.get("rows",[]):
            checks=row.get("checks",{})
            transport_rows.append([CASE_LABELS.get(group.get("caseId"),group.get("caseId")),
                "M" if group.get("architecture")=="MULTI_AGENT" else "S", ROLE_LABELS.get(row["role"],row["role"]),row.get("phase"),
                _status(checks.get("request",{}).get("endToEnd",{}).get("status")),
                _status((row.get("delivery") or {}).get("check",{}).get("status")),
                _status((row.get("model") or {}).get("apiInputMatches")),
                _status(checks.get("response",{}).get("status"))])
    body=_table(["케이스","구조","역할","상태","입력→실행기","DB 수신","API 입력","응답"],transport_rows)
    body+='<h2>세부 지점: 일치 / 불일치 / 미관측</h2><div class="boundaries">'+''.join(
        '<div><b>'+_text(name)+'</b><span>'+f'{value["passed"]} / {value["failed"]} / {value["unverified"]}'+'</span></div>'
        for name,value in summary["boundaries"].items())+'</div>'
    body+='<p class="note">DB 수신은 실제 저장된 Message와 송신 요청의 식별자·해시를 대조한 값입니다. SKIPPED는 이전 결과의 계약 거절로 보내지 않은 요청이며, 데이터 유실로 세지 않습니다. API HTTP 200만으로 결과를 통과시키지 않습니다.</p>'
    page("1. 전부 보내고, 제대로 받았나", "M=멀티 · S=싱글. 식별자·metadata·API 실제 입력까지 각각 확인합니다.",body)

    body='<div class="lead">'+_text(review.get("conclusion","의미 정확성은 별도 검토가 필요합니다."))+'</div>'
    for item in review.get("caseReviews",[]):
        body+='<article class="finding"><h2>'+_text(CASE_LABELS.get(item.get("caseId"),item.get("caseId","")))+' · '+_text(item.get("judgment","검토"))+'</h2><p>'+_text(item.get("finding",""))+'</p>'
        if item.get("sourceQuote"):
            body+='<p class="quote"><b>사용자 원문</b> '+_text(item["sourceQuote"])+'</p>'
        if item.get("answerQuote"):
            body+='<p class="quote"><b>'+_text(item.get("architecture",""))+' / '+_text(item.get("role","응답"))+'</b> '+_text(item["answerQuote"])+'</p>'
        body+='</article>'
    body+=_table(["검사","이번 값 / 의미"],[["출력·인용·실행 주장 불변조건",f'{summary["invariantViolations"]}개 위반 / 일반적 의미 정확성 판정과 구분'],
        ["다른 역할의 roleWork",f'{summary["foreignRoleFields"]}개 채움 / 역할별 닫힌 Schema와 실제 응답을 확인'],
        ["실행 상태",f'{summary["notExecutedClaims"]}/{summary["parsedAnswers"]}개 NOT_EXECUTED / 실행하지 않은 제품 성공 주장과 분리']])
    body+='<p class="note">검토 방법: '+_text(review.get("method","Codex의 원문 사후 검토. 독립 사람 채점·추가 API 판정 아님"))+' 자동 인용 일치만으로 해석의 타당성이나 모든 요구사항 충족을 증명하지 않습니다.</p>'
    page("2. 할루시네이션은 없었나", "전송 오류, 선언된 불변조건 위반, 의미상의 위험을 섞지 않습니다.",body)

    speed_rows=[]
    for role,timing in summary["roles"].items():
        agent,api=timing["agentProcessingMs"],timing["providerExchangeMs"]
        agent_range = f'{agent["min"] / 1000:.3f}–{agent["max"] / 1000:.3f}' if agent['samples'] else '미관측'
        speed_rows.append([ROLE_LABELS[role], agent["samples"],_number(agent["median"],seconds=True),agent_range,_number(api["median"],seconds=True),
                           _number(timing["requestTransferMs"]["median"]),_number(timing["admissionAndSchedulingMs"]["median"]),_number(timing["resultObservationMs"]["median"])])
    body=_table(["역할","완료 표본","Agent 중앙값","Agent 범위(초)","API 교환 중앙값","로컬 전달","접수·대기","결과 확인"],speed_rows)
    body+='<h2>케이스별 진단 실행시간</h2>'+_table(["케이스","구조","관측 시간"],[[CASE_LABELS.get(group.get("caseId"),""),group.get("architecture"),_number(group.get("elapsedMs"),seconds=True)] for group in groups])
    body+=_table(["함께 읽을 정보","관측 값"],[["입력 / 출력 토큰 합",f'{summary["inputTokens"]:,} / {summary["outputTokens"]:,}'],
        ["Client / Card 재사용",f'{summary["reusedClients"]}행 / {summary["cachedCards"]}행'],
        ["관측 모드",', '.join(sorted({group.get("measurement",{}).get("observerMode","미기록") for group in groups}))]])
    body+='<p class="note">API 교환은 네트워크·서버 처리·본문 소비를 포함합니다. 순수 모델 추론 시간은 분리하지 못했습니다. 역할별 작업량·출력량이 다르고 표본이 적어 단순 배율로 Agent 우열을 판단하지 않습니다. 병렬 QA·보안 시간은 전체시간에 더하지 않았습니다. 속도 차이 자체가 데이터 유실의 증거는 아닙니다. 전달 여부는 3페이지의 실제 대조 결과로 확인하세요.</p>'
    page("3. 속도는 얼마나 달랐나", "초(s)와 밀리초(ms)를 분리했습니다. 작은 값은 반올림으로 0ms가 될 수 있습니다.",body)

    provenance=evidence.get("provenance",{})
    if baseline is not None:
        comparison=compare_baseline(evidence,baseline)
        previous=summarize_evidence(baseline)
        labels = lambda keys: ' / '.join('미기록' if comparison[key] is None else '같음' if comparison[key] else '다름' for key in keys)
        body=_table(["이전 원자료와 대조","결과"],[["같은 모델 / 원문 / 온도 / 출력 상한",labels(('sameModel','sameRequests','sameTemperature','sameOutputCap'))],
            ["같은 Prompt / Schema",labels(('samePrompts','sameSchemas'))],
            ["다른 역할 필드 수",f'이전 {previous["foreignRoleFields"]} → 이번 {summary["foreignRoleFields"]}'],
            ["성능 개선 인과 판정","불가. 계약·관측 방식 변경, 서버 부하·Cache·실행 순서 통제 부족"]])
    else:
        body='<p class="note">이전 원자료를 제공하지 않아 변경 전후 수치 비교는 하지 않았습니다.</p>'
    body+='<h2>남은 확인</h2><ul>'+''.join('<li>'+_text(value)+'</li>' for value in review.get("remaining",[
        "제품 Host·역할 Prompt·MCP·Docker를 포함한 실제 생성→수정→검증 연결",
        "미승인 가정이 후속 역할의 필수 조건으로 바뀌는지 독립 기준으로 반복 검증",
        "같은 Source·환경·예산·실행 순서 조건을 고정한 정식 Single/Multi 비교"]))+'</ul>'
    body+='<h2>원자료와 재현 근거</h2><div class="provenance">'+_text(raw_path)+'<br>SHA-256 '+_text(raw_sha256)+'<br>Source manifest '+_text(provenance.get("sourceManifestSha256","미기록"))+'<br>Git '+_text(provenance.get("gitCommit","미기록"))+' / 작업트리 변경 '+_text(provenance.get("worktreeDirty"))+' / 선택 소스 안정성 '+_text(provenance.get("sourceStability","미검증"))
    if baseline_path:
        body+='<br>이전 자료 '+_text(baseline_path)+'<br>SHA-256 '+_text(baseline_sha256)
    body+='</div><p class="note">원자료는 .data의 Git 제외 경로에 보존했습니다. PDF는 요약이며 모든 원문 응답을 싣지는 않습니다. 키·인증 헤더는 보고서에 포함하지 않습니다. 기존 보고서는 변경하지 않았습니다.</p>'
    page("비교의 한계와 다음 확인", "관측된 결과만 정리했습니다. 성능 우열·서비스 완성·보안 안전성을 선언하지 않습니다.",body)
    css='''@page{size:A4;margin:0}*{box-sizing:border-box}body{margin:0;color:#172331;font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif;font-size:11px;line-height:1.55}.page{width:210mm;height:297mm;padding:13mm 13mm 16mm;position:relative;break-after:page;overflow:hidden}.page:last-child{break-after:auto}header{display:flex;justify-content:space-between;font-size:9px;letter-spacing:.7px;color:#526478;border-bottom:2px solid #1558c9;padding-bottom:10px}h1{font-size:27px;line-height:1.25;margin:20px 0 8px;letter-spacing:-1px}h2{font-size:15px;margin:9px 0 5px}p{margin:7px 0}.subtitle{color:#5a6a7d;margin-bottom:16px}.lead{background:#1558c9;color:white;padding:13px 16px;border-radius:10px;font-size:14px;font-weight:700;margin:12px 0}.card,.request,.finding{border:1px solid #dce4ef;border-radius:10px;padding:12px 15px;margin:11px 0}.card span{font-weight:700;color:#1558c9}.card h2{font-size:18px}.request p{font-size:11.4px;line-height:1.7}.finding{padding:10px 13px}.quote{font-size:10.5px;background:#f3f6fa;padding:8px 10px;border-left:3px solid #1558c9;white-space:pre-wrap}table{width:100%;border-collapse:collapse;font-size:10px;margin:12px 0}td,th{padding:7px 7px;text-align:left;border-bottom:1px solid #dce4ef;vertical-align:top}th{background:#eef3fb;color:#233d63}tr{break-inside:avoid}.note{background:#f1f5fa;border-radius:8px;padding:11px 13px;font-size:10.5px;color:#405168;margin-top:12px}.provenance{font-family:monospace;font-size:9px;line-height:1.8;overflow-wrap:anywhere;background:#f4f6f9;padding:12px;border-radius:8px}footer{position:absolute;bottom:9mm;left:13mm;right:13mm;display:flex;justify-content:space-between;font-size:8.5px;color:#718095;border-top:1px solid #dce4ef;padding-top:7px}ul{padding-left:18px}li{margin:6px 0}'''
    css+=' .boundaries{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin:12px 0}.boundaries div{border:1px solid #dce4ef;border-radius:6px;padding:6px 9px;font-size:10px}.boundaries b,.boundaries span{display:block}'
    return '<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>A2A 보강 후 실제 통신 재검증</title><style>'+css+'</style></head><body>'+''.join(pages)+'</body></html>'


def main():
    """Create a temporary printable source from saved evidence, without API calls."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    raw = args.evidence.read_bytes()
    before = args.baseline.read_bytes() if args.baseline else None
    html = render_html(json.loads(raw), raw_path=str(args.evidence), raw_sha256=sha256(raw).hexdigest(),
        review=json.loads(args.review.read_bytes()), baseline=json.loads(before) if before is not None else None,
        baseline_path=str(args.baseline) if args.baseline else None,
        baseline_sha256=sha256(before).hexdigest() if before is not None else None)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(html, encoding='utf-8')
    print(f'Offline printable source: {args.out}')


if __name__ == '__main__':
    main()
