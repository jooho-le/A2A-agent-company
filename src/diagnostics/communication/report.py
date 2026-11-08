"""Render the sealed strict-suite evidence as a printable Korean report."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo


STYLE = """
:root{--ink:#182330;--muted:#627286;--line:#d9e1e9;--blue:#1558c9;--lime:#d4ff24}
*{box-sizing:border-box}body{margin:0;background:#e6ebef;color:var(--ink);font:11.5px/1.8 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Malgun Gothic",sans-serif}
.page{width:210mm;height:297mm;padding:13mm 15mm 11mm;background:white;margin:18px auto;display:flex;flex-direction:column;position:relative;overflow:hidden}
header{display:flex;justify-content:space-between;align-items:center;border-bottom:2px solid #182a3d;padding-bottom:10px;gap:12px;color:var(--muted);font-size:8px;letter-spacing:.6px;flex-shrink:0}.brand{font-size:22px;font-weight:800;letter-spacing:-1.2px;color:var(--ink)}.brand i{color:#638916;font-style:normal}header b{color:var(--blue);font-size:10px}
.eyebrow{font-size:9px;letter-spacing:1.3px;color:var(--blue);font-weight:750;margin-top:18px}h1,h2,h3,p{margin:0}h1{font-size:29px;line-height:1.4;letter-spacing:-1.1px;margin-top:8px}h2{font-size:24px;line-height:1.4;letter-spacing:-.8px;margin-top:8px}h3{font-size:13px;line-height:1.6}.lead{font-size:12px;color:var(--muted);margin-top:8px;line-height:1.85}.meta,.small{color:var(--muted);font-size:9px;line-height:1.85}.meta{margin-top:10px}.blue{color:var(--blue)}.section-label{font-size:11px;font-weight:750;margin:16px 0 7px}.space{margin-top:14px}.space-sm{margin-top:8px}.cards{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}.cards.four{grid-template-columns:repeat(4,1fr)}.cards.three{grid-template-columns:repeat(3,1fr)}.card{border:1px solid var(--line);background:#f8fafc;border-radius:8px;padding:12px}.card .label{font-size:9px;color:var(--blue);font-weight:750}.big{display:block;font-size:26px;line-height:1.4;letter-spacing:-.7px;margin:5px 0}.card p{font-size:10px;line-height:1.85;color:#586b80;margin-top:5px}.success{background:#f2f7ea;border-color:#c7d8b3}.alert{background:#fff4eb;border-color:#edccb0}
table{width:100%;border-collapse:collapse;font-size:10px;line-height:1.7}th{font-size:9px;font-weight:750;color:#53697f;text-align:left;background:#edf2f7;border-top:1px solid #cad6e2;border-bottom:1px solid #cad6e2;padding:8px 7px}td{padding:8px 7px;border-bottom:1px solid #e0e7ef;vertical-align:top}.compact td{padding:6px 7px}.numeric td:not(:first-child){font-variant-numeric:tabular-nums}.good{color:#426716;font-weight:750}.bad{color:#9b352e;font-weight:750}.highlight td{background:#f3f7eb}.muted{color:var(--muted)}.nowrap{white-space:nowrap}.callout{padding:12px 15px;border-left:3px solid var(--blue);background:#eef4ff;margin-top:14px;font-size:10.5px;line-height:1.9}.stamp{padding:13px 16px;border-radius:8px;background:#182a3d;color:#eef4fc;margin-top:14px;font-size:11px;line-height:1.9}.stamp b{color:var(--lime)}.definitions{display:grid;grid-template-columns:1fr 1fr;gap:10px 18px;margin-top:13px}.definition{border-top:1px solid #d5dfe9;padding-top:8px}.definition b{font-size:10.5px}.definition p{font-size:10px;color:var(--muted);margin-top:3px}.source-box{border:1px solid var(--line);background:#f4f7fa;padding:11px 13px;border-radius:7px;margin-top:12px;font-size:9.5px;line-height:1.9}.hash{font:8px/1.8 ui-monospace,monospace;word-break:break-all;color:#64788c}.bullet{font-size:10.5px;line-height:1.9;color:#53677f;padding-left:18px;margin:8px 0 0}.bullet li+li{margin-top:5px}code{font:9px/1.8 ui-monospace,monospace;overflow-wrap:anywhere}pre{font:9px/1.9 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere;margin:8px 0 0;padding:12px;background:#eef2f6;border-radius:7px}.quote{font-size:11px;color:#354d65;padding:10px 13px;background:#f2f5f8;border-radius:7px;margin-top:9px}.tag{display:inline-block;font-size:8px;color:#426716;background:#edf4e6;padding:2px 7px;border-radius:12px}.dense{font-size:9px}.dense td{padding:5px 6px}.dense th{padding:7px 6px}.break{overflow-wrap:anywhere}.row-label{background:#f4f7fa;font-weight:750}footer{margin-top:auto;border-top:1px solid #d2dce6;padding-top:10px;display:flex;justify-content:space-between;gap:14px;font-size:8px;color:#7a8a9e;flex-shrink:0}
@page{size:A4;margin:0}@media print{body{background:white;print-color-adjust:exact;-webkit-print-color-adjust:exact}.page{margin:0;break-after:page;page-break-after:always;break-inside:avoid}.page:last-child{break-after:auto;page-break-after:auto}a{color:inherit;text-decoration:none}}
"""

PROFILE_NAMES = {"signup": "가입·조직 권한", "documents": "전자문서·공유", "mystery": "추리게임·상태"}
ROLE_NAMES = {"PLANNER": "Planner", "DEVELOPER": "Developer", "QA": "QA", "SECURITY": "Security", "SINGLE_AGENT": "Single"}
FAULTS = [
    ("request_change", "입력 수신 후 숫자 +1", "수신 처리 경계 / 원본 근거"),
    ("response_change", "응답 송출 중 숫자 +1", "응답 전후 값 불일치"),
    ("grounding_error", "틀린 수치·배포 성공 주장", "근거 없는 필드·추가 주장"),
    ("no_response", "결과를 보내지 않음", "1.5초 제한시간 / 실제 취소"),
    ("request_truncate", "요청 설명 마지막 120자 삭제", "문서 무결성 / 원본 fingerprint"),
    ("requirement_drop", "요구사항 한 항목만 제거", "고정 참조와 요구사항 누락 대조"),
    ("array_reorder", "합격 기준 배열 순서 뒤집기", "순서 있는 배열의 위치 대조"),
    ("type_change", "false를 문자열 false로 변경", "타입까지 포함한 정확 비교"),
    ("unicode_change", "한글 완성형을 자모형으로 변경", "겉모양 대신 실제 Unicode 대조"),
    ("metadata_change", "A2A 요구사항 UUID 하나 교체", "본문과 별도로 metadata 대조"),
    ("response_requirement_drop", "개수는 유지하고 수행 범위 누락", "고정된 coverageIds 대조"),
    ("response_limit_change", "업로드 한도 1바이트 추가", "20MiB 정책값의 정확 대조"),
    ("permission_swap", "허용 개수 유지·권한 대상 교환", "역할·자원·행위 관계 단위 대조"),
    ("invented_evidence", "미실행 빌드를 PASS라고 주장", "NOT_EXECUTED와 가짜 영수증 대조"),
    ("context_spoof", "ACK·조회 결과 Context 함께 위장", "수신 실행기가 기록한 실제 ID 대조"),
    ("foreign_artifact", "다른 요청의 Probe ID 결과 전송", "상관관계 거절 / 후속 호출 생략"),
    ("invented_decision", "미결정 항목을 임의로 해소", "미결정 질문 ID 보존 대조"),
    ("message_id_change", "수신 직전 Message UUID 교체", "송신·수신 Message ID 대조"),
]


def _table(headers: list[str], rows: list[list[str]], css: str = "compact") -> str:
    return '<table class="' + css + '"><thead><tr>' + ''.join(f'<th>{h}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{x}</td>' for x in row) + '</tr>' for row in rows) + '</tbody></table>'


def _pct(values: list[float], percentile: float) -> float:
    return sorted(values)[max(0, math.ceil(len(values) * percentile) - 1)]


def build_report(directory: Path) -> Path:
    summary_file = directory / "summary.json"
    d = json.loads(summary_file.read_text(encoding="utf-8"))
    v = json.loads((directory / "verification.json").read_text(encoding="utf-8"))
    raw = directory / d["rawEvidence"]["file"]
    if hashlib.sha256(raw.read_bytes()).hexdigest() != d["rawEvidence"]["sha256"]:
        raise ValueError("원본 관측 파일의 SHA-256이 summary와 다릅니다.")
    if d["quick"] or d["llmUsed"] or not d["synthetic"]:
        raise ValueError("이 보고서는 전체 합성 목업 행렬 전용입니다.")
    results = d["results"]
    baseline = [r for case in results if case["category"] == "positive" for r in case["rows"]]
    positive = [case for case in results if case["category"] in {"positive", "positive_large", "delay"}]
    positive_requests = sum(x["summary"]["planned"] for x in positive)
    targets = sum(x["verdict"]["targetCount"] for x in results if x["category"] in {"negative", "negative_role"})
    totals = d["totals"]
    groups = Counter(x["category"] for x in results)
    kst = ZoneInfo("Asia/Seoul")
    start = datetime.fromisoformat(d["startedAt"]).astimezone(kst)
    end = datetime.fromisoformat(d["finishedAt"]).astimezone(kst)
    span = f'{start:%Y.%m.%d %H:%M:%S}–{end:%H:%M:%S} KST'
    pages: list[tuple[str, str]] = []

    overview_rows = []
    for key, label, desc in [
        ("positive", "정상 기준선", "3자료 × 2구조 × 4묶음 × 5회"),
        ("positive_large", "큰 본문 정상 전달", "3자료 × 2구조 × 2회 / +32KiB"),
        ("boundary", "크기 초과 거절", "3자료 × 2구조 / +96KiB"),
        ("negative", "18종 오류 주입", "18종 × 2구조 × 2회"),
        ("negative_role", "전체 역할 교차 확인", "3자료 × 4역할 / 한도 1바이트 변형"),
        ("delay", "구간 지연 확인", "송신 10초 / 처리 1초 주입"),
    ]:
        cases = [x for x in results if x["category"] == key]
        overview_rows.append([label, desc, str(groups[key]), str(sum(x["summary"]["planned"] for x in cases)), f'{sum(x["verdict"]["passed"] for x in cases)}/{len(cases)}'])
    pages.append(("SUMMARY", f"""
<div class="eyebrow">실제 HTTP · 합성 사용자 요청 · 목업 실행기 · LLM 미사용</div>
<h1>Agent 통신 엄격 검증 v2<br><span class="blue">복잡한 요구사항도 정확히 전달하는가?</span></h1>
<p class="lead">숫자 하나의 왕복에서 확장해 요구사항·권한·정책·미결정 사항과<br>응답의 근거를 대조했습니다. 의도적인 오류는 발견되어야 통과입니다.</p>
<div class="meta">최종 수집 {span} · 기본 인위적 처리 대기 0ms</div>
<div class="cards four">
<article class="card success"><div class="label">정상 조건 요청</div><strong class="big">{positive_requests}건</strong><p>송수신·구조·고정 근거 대조<br>모두 통과</p></article>
<article class="card success"><div class="label">의도한 오류 대상</div><strong class="big">{targets}건</strong><p>변형·허위 주장·무응답<br>기대한 위치에서 탐지</p></article>
<article class="card"><div class="label">큰 본문 거절</div><strong class="big">6건</strong><p>HTTP 413<br>Task·실행기 생성 없음</p></article>
<article class="card"><div class="label">별도 프로토콜 확인</div><strong class="big">4 / 4</strong><p>중복 메시지·내용 충돌<br>다른 Run·역할 격리</p></article></div>
<div class="section-label">전체 행렬 {totals['casesPassed']}/{totals['cases']} 조건 충족 · 조건별 집계</div>
{_table(['분류', '구성', '조건 수', '예정 요청', '기대 결과'], overview_rows)}
<div class="source-box"><b>예정 {totals['planned']} → 송신 {totals['sent']} → 실행기 수신 {totals['received']} → 완료 결과 수신 {totals['responses']}</b><br>
후속 생략 17건, 크기 초과 거절 6건, 무응답 4건·다른 요청 결과 거절 4건을 구분했습니다.<br>
집계는 A2A 업무 메시지 기준이며 Card 조회·Task 폴링·취소 HTTP와 별도 프로토콜 4개 검사는 포함하지 않습니다. ‘완료 결과’에는 고의로 틀리게 만든 응답도 포함됩니다.</div>
<div class="stamp"><b>확인한 결론:</b> 이 합성 문서와 주입 오류에 대해 전달 무결성·구조화된 주장 대조·시간 구간 분리가 동작했습니다. <b>실제 LLM의 할루시네이션이나 개발 품질은 미검증</b>입니다.</div>
"""))

    fixture_rows = [[PROFILE_NAMES[x['profile']], str(x['requirements']), str(x['cases']), str(x['accessRules']), str(x['questions']), f"{x['fixtureBytes']:,} B"] for x in d['fixtures']]
    fixture_rows.append(['합계', '42', '54', '19', '6', '원본 compact JSON 기준'])
    pages.append(("REALISTIC SYNTHETIC REQUESTS", f"""
<div class="eyebrow">01 / 긴 텍스트뿐 아니라 서로 연결된 조건을 포함</div>
<h2>사용자가 맡길 법한 요청서<br>3개를 고정 자료로 만들었습니다.</h2>
<p class="lead">실제 고객 자료를 사용한 것은 아닙니다. 합성 요청마다 자연어 설명,<br>요구사항 ID·의존 관계·합격 기준·권한표·미결정 질문을 함께 넣었습니다.</p>
<div class="section-label">자료 규모</div>{_table(['합성 업무 요청', '요구사항', '합격 기준', '권한 규칙', '미결정', '요청서 크기'], fixture_rows)}
<div class="cards">
<article class="card"><div class="label">가입·조직 권한 / 707자 설명</div><h3>가입만 되는 화면 이상의 요청</h3><p>이메일 검증·중복·인증 만료·초대·조직 역할·탈퇴·개인정보를 연결했습니다. 요구사항별 합격 기준과 아직 확정하지 않은 질문을 따로 보존합니다.</p></article>
<article class="card"><div class="label">전자문서·공유 / 759자 설명</div><h3>파일 크기와 접근 관계까지</h3><p>업로드·공유·만료·철회·다른 조직 접근을 포함합니다. 정확히 20MiB와 ±1바이트를 합격 기준에 넣고, 파일명·경로 입력은 실행하지 않는 데이터로 전달합니다.</p></article>
<article class="card"><div class="label">추리게임·상태 / 656자 설명</div><h3>이전 상태가 다음 조건을 바꾸는 요청</h3><p>퍼즐 단계·정답 경로·힌트 조건을 연결했습니다. 정답 “007”, 남은 횟수 0, 힌트 false, 제한시간 null을 서로 다른 타입으로 보존합니다.</p></article>
<article class="card"><div class="label">공통 경계 데이터</div><h3>겉보기에는 같아도 다른 값</h3><p>완성형·자모형 한글, 악센트·이모지, 줄바꿈·따옴표·역슬래시를 포함합니다. 큰 식별자 “9007199254740993”은 규격대로 문자열로 전달합니다.</p></article></div>
<div class="callout"><b>같게 취급하면 안 되는 사례</b><br><code>false ≠ "false" · 0 ≠ "0" · null ≠ "" ≠ [] ≠ {{}} · "007" ≠ 7</code><br>Unicode 변형은 의미 동등성 판단 대신 전송된 코드 포인트의 보존을 확인합니다. 20MiB 파일을 실제 업로드하거나 게임을 실행한 결과는 아닙니다.</div>
<div class="source-box"><b>직접 열어 수정할 자료</b><br><code>src/diagnostics/communication/fixtures/signup.request.json</code><br><code>src/diagnostics/communication/fixtures/documents.request.json</code><br><code>src/diagnostics/communication/fixtures/mystery.request.json</code><br>합격 기준 54개는 전달하는 기대 사례입니다. 실제 서비스 테스트 54개를 실행했다는 의미가 아닙니다.</div>
"""))

    proof = v['sameBasicFieldsSemanticProof']
    pages.append(("INDEPENDENT REFERENCE & CHECK BOUNDARIES", f"""
<div class="eyebrow">02 / Agent가 받은 내용을 정답으로 다시 정의하지 않음</div>
<h2>송수신 무결성과<br>응답의 근거를 따로 대조합니다.</h2>
<p class="lead">기대 결과는 Agent 입력 밖의 <code>*.golden.json</code>에 고정했습니다.<br>실행기는 실제 받은 요청으로만 목업 응답을 만들며 정답 파일을 읽지 않습니다.</p>
<div class="section-label">관측 경계</div>
{_table(['대조 항목', '양쪽에서 따로 기록하는 값'], [
['요청', 'Orchestrator 원본 → 직렬화 → 서버 본문 → 접수 → 실행기 입력'],
['응답', '실행기가 만든 결과 → Orchestrator가 읽은 완료 Artifact'],
['메타데이터', 'Run·요구사항 연결 값과 송신·수신 metadata'],
['식별자', '송신 Message·ACK/조회 Task/Context ↔ 실행기에서 관측한 실제 ID'],
['구조화된 근거', '역할별 coverage·제약·권한 관계·미결정 질문·실행 증거 ↔ 고정 참조'],
])}
<div class="cards"><article class="card success"><div class="label">기본 값·개수가 같아도 검출</div><strong class="big">{proof['count']}건</strong><p>value·label·requirementCount·upstreamHash가 모두 같고 요청·응답 전달도 일치했습니다. 더 상세한 고정 계약을 대조해 숨은 변형을 잡았습니다.</p></article><article class="card alert"><div class="label">대표 사례 / 실제 주입</div><h3>“16개 처리”라는 숫자만 믿지 않음</h3><p>coverage에서 한 항목만 빼거나, 허용 개수는 그대로 두고 권한 대상을 바꿨습니다. 한도 1바이트 변경·가짜 PASS 영수증·미결정 질문 삭제도 구분했습니다.</p></article></div>
<div class="definitions">
<div class="definition"><b>Planner</b><p>요구사항·의존 관계·설계 범위·미결정 질문의 보존을 확인합니다.</p></div>
<div class="definition"><b>Developer</b><p>수행 범위·정책값·대상 파일과 미실행 빌드 상태를 확인합니다.</p></div>
<div class="definition"><b>QA / Security</b><p>사례와 요구사항의 연결, 기대 결과, 권한 관계·미실행 증거를 확인합니다.</p></div>
<div class="definition"><b>Single</b><p>한 Context를 유지하면서 한 응답에 포함된 네 역할의 구조화 계약을 확인합니다.</p></div></div>
<div class="callout"><b>정답 파일의 의미와 한계</b><br>합성 자료에 대해 별도로 고정한 참조이며 고객사에서 승인한 사업 명세는 아닙니다. 요청과 참조는 같은 프로세스의 다른 파일로 관리합니다. 자연어의 의미를 LLM으로 해석하거나 물리적으로 격리된 검증기를 실행한 것은 아닙니다.</div>
"""))

    fault_rows = []
    for name, label, boundary in FAULTS:
        arch_counts = []
        for arch in ('MULTI_AGENT', 'SINGLE_AGENT'):
            cases = [x for x in results if x['category']=='negative' and x['options']['scenario']==name and x['options']['architecture']==arch]
            assert len(cases)==1
            case=cases[0]
            arch_counts.append(f"{case['verdict']['targetCount']}/{case['options']['rounds']}" if case['verdict']['passed'] else '실패')
        fault_rows.append([label, boundary, *arch_counts])
    pages.append(("FAULT INJECTION MATRIX", f"""
<div class="eyebrow">03 / 단순히 오류가 난 것만으로 탐지 성공이라 판정하지 않음</div>
<h2>18종 오류를 양쪽 구조에서<br>반복 주입해 확인했습니다.</h2>
<p class="lead">각 종류는 멀티 2회·싱글 2회입니다. 지정한 오류 위치가 잡히고,<br>변형하지 않은 역할이 불필요하게 실패하지 않는지도 함께 검사했습니다.</p>
<div class="space-sm">{_table(['의도한 변형·누락', '기대 탐지 경계', '멀티', '싱글'], fault_rows, 'dense')}</div>
<div class="source-box"><b>위 표 72개 오류 대상 + 전체 역할 교차 12개 = 총 84개</b><br>한도 1바이트 변경을 3개 자료 × Planner·Developer·QA·Security에 추가했습니다. 특정 Developer 역할에서만 동작하는 검사인지 교차 확인했습니다.<br>무응답 때 후속 단계를 보내지 않는 동작, 다른 Probe ID의 완료 결과를 거절하는 동작도 기대 결과에 포함합니다.</div>
<p class="small space-sm">배열 순서는 계약상 순서가 있는 경우 정확 비교합니다. 일반 문장이나 집합의 의미까지 자동으로 판정하는 검사가 아닙니다. 미신뢰 첨부의 지시문은 데이터로 운반했으며 실제 LLM 프롬프트 공격 방어를 검증한 것은 아닙니다.</p>
"""))

    timing_rows = []
    for profile in PROFILE_NAMES:
        for role in ROLE_NAMES:
            rows = [r for case in results if case['category']=='positive' and case['options']['profile']==profile for r in case['rows'] if r['role']==role]
            assert len(rows)==20
            times = [r['timings']['agentProcessingMs'] for r in rows]
            timing_rows.append([PROFILE_NAMES[profile],ROLE_NAMES[role],'20',f"{median(r['wireBytes'] for r in rows)/1024:.2f}",f"{_pct(times,.5):.2f}",f"{_pct(times,.95):.2f}",f"{median(r['timings']['totalMs'] for r in rows):.2f}"])
    pages.append(("BASELINE LATENCY", f"""
<div class="eyebrow">04 / 기본 인위적 처리 대기 0ms · 정상 기준선 300건만 사용</div>
<h2>자료·역할마다 20개 표본으로<br>처리시간과 전체시간을 나눴습니다.</h2>
<p class="lead">실제 전송 본문은 요청서 외에 A2A 메타데이터와 이전 결과를 포함합니다.<br>추가 패딩·오류 주입·의도적 지연은 아래 기준선에 넣지 않았습니다.</p>
<div class="space-sm">{_table(['자료', '역할', 'n', '본문 KiB<br>중앙값', '처리 ms<br>p50', '처리 ms<br>p95', '전체 ms<br>중앙값'], timing_rows, 'compact numeric')}</div>
<p class="small space-sm">처리 p50·p95는 nearest-rank 방식입니다. 전체·본문은 중앙값입니다. n=20의 p95는 19번째 순서 통계이며 성능 보증이나 통계적 유의성 판단을 위한 표본은 아닙니다. 1KiB=1,024바이트입니다.</p>
<div class="callout"><b>이 표의 Agent 처리시간은 LLM 응답시간이 아닙니다.</b><br>합성 응답 생성·계측·같은 이벤트 루프의 스케줄링을 포함합니다. QA와 Security는 병렬 실행하므로 다른 작업의 영향도 받습니다. Single은 네 역할 계약을 한 결과로 생성하므로 이 표로 역할이나 구조의 속도 순위를 매기지 않습니다.</div>
<div class="source-box"><b>측정 환경</b><br>로컬 loopback / 한 Python 프로세스 / 역할별 TCP 포트 / 인증된 임시 Agent / 일회용 SQLite<br>Python {escape(d['environment']['python'])} · a2a-sdk {escape(d['environment']['a2a-sdk'])} · HTTPX {escape(d['environment']['httpx'])} · Uvicorn {escape(d['environment']['uvicorn'])} · FastAPI {escape(d['environment']['fastapi'])}<br>A2A 1.0 HTTP+JSON · 외부 API 키·LLM·MCP·Docker 미사용</div>
"""))

    delay_rows=[]
    for case in results:
        if case['category']=='delay':
            target='PLANNER' if case['options']['scenario']=='slow_sender' else 'DEVELOPER'
            row=next(r for r in case['rows'] if r['role']==target)
            t=row['timings']
            delay_rows.append(['준비 10초 주입' if target=='PLANNER' else '처리 1초 주입', ROLE_NAMES[target],f"{t['orchestratorPrepareMs']:,.2f}",f"{t['agentProcessingMs']:,.2f}",f"{t['totalMs']:,.2f}"])
    stage_rows=[]
    for key,label in [('orchestratorPrepareMs','요청 준비'),('cardLookupMs','Card 조회'),('serializationMs','직렬화·전송 준비'),('requestTransferMs','본문 전달'),('queueMs','실행 대기'),('agentProcessingMs','목업 처리'),('resultObservationMs','완료 관측')]:
        stage_rows.append([label,f"{median(r['timings'][key] for r in baseline):.2f}"])
    large=[r for c in results if c['category']=='positive_large' for r in c['rows']]
    pages.append(("STAGES, SIZE & CONTROLLED DELAYS", f"""
<div class="eyebrow">05 / 크게 보내는 정상 조건과 제한 초과를 따로 확인</div>
<h2>전송 크기와 계측 비용까지<br>시간 해석에 포함했습니다.</h2>
<div class="cards"><article class="card success"><div class="label">+32KiB 데이터 / 6조건·30요청</div><h3>큰 본문도 정확 대조 통과</h3><p>실제 HTTP 본문 {min(r['wireBytes'] for r in large)/1024:.2f}–{max(r['wireBytes'] for r in large)/1024:.2f}KiB.<br>요구사항·결과·식별자·타입까지 모두 보존했습니다.</p></article><article class="card"><div class="label">+96KiB 데이터 / 6조건</div><h3>64KiB 진단 제한에서 거절</h3><p>HTTP 413, Task·실행기 생성 0건.<br>64KiB는 이 진단 서버의 본문 제한이며 공식 A2A 한도나 제품 업로드 한도가 아닙니다.</p></article></div>
<div class="section-label">지연을 실제로 넣은 요청의 실측 · 각 1회 · ms</div>
{_table(['조건', '역할', '준비', '처리', '전체'],delay_rows)}
<div class="definitions"><div>
<h3>정상 300건의 구간별 중앙값 · ms</h3>
{_table(['구간','중앙값'],stage_rows,'dense numeric')}
</div><div class="definition"><b>구간을 더할 때 주의</b><p>각 구간 중앙값의 합은 전체 중앙값과 같지 않습니다. ACK 왕복은 처리와 중첩하므로 다시 더하지 않습니다.</p><b>관측 자체의 비용</b><p>관측 콜백 합계 중앙값 {median(r['observerCallbackMs'] for r in baseline):.2f}ms. 기록된 일부 계측 비용일 뿐 전체 부가 비용은 아닙니다. 순수 네트워크 시간처럼 빼서 해석하지 않습니다.</p><b>측정 시계</b><p>같은 프로세스의 perf_counter_ns()를 사용합니다. 분산 서버 시계 오차나 운영 네트워크 SLA 검증은 범위 밖입니다.</p></div></div>
<p class="small space-sm">준비에는 fixture·payload 생성, 직렬화에는 JSON/protobuf 변환과 전송 전 준비가 포함됩니다. 결과 관측에는 게시·DB 저장·20ms 폴링·결과 역직렬화가 포함됩니다. 무응답의 미관측 시간은 0으로 채우지 않습니다. 파일의 20MiB 정책과 HTTP JSON 본문의 64KiB 진단 한도는 별개의 제한입니다.</p>
"""))

    protocol_rows=[]
    labels={
        'duplicate_same_message':('같은 Message ID·같은 본문 재전송','같은 Task·Context 반환 / 실행기 호출 1회'),
        'duplicate_conflicting_body':('같은 Message ID·다른 본문','InvalidParamsError / 두 번째 실행 없음'),
        'foreign_run_context':('다른 Run이 기존 Context 사용','InvalidParamsError / 새 실행 없음'),
        'foreign_role_task':('QA가 Developer Task 조회','TaskNotFoundError'),
    }
    for check in d['protocolChecks']:
        label,expected=labels[check['name']]
        protocol_rows.append([label,expected,'PASS' if check['passed'] else 'FAIL'])
    pages.append(("PROTOCOL & REGRESSION CHECKS", f"""
<div class="eyebrow">06 / 전송 내용 외에도 중복과 실행 영역을 확인</div>
<h2>같은 요청이 두 번 실행되거나<br>다른 작업의 결과가 섞이지 않는가?</h2>
<p class="lead">행렬 집계와 별도로 실제 인증된 A2A 경로에서 확인했습니다.<br>단순한 Mock 함수 호출만으로 프로토콜 결과를 대신하지 않았습니다.</p>
<div class="space">{_table(['검사', '기대 결과', '결과'],protocol_rows)}</div>
<div class="source-box"><b>중복 전송의 실제 관측 근거</b><br>첫 Task ID와 중복 응답 Task ID가 같고, Context도 동일했습니다. 실행기 호출은 1회였습니다.<br><code>{escape(d['protocolChecks'][0]['firstTaskId'])}</code></div>
<div class="cards"><article class="card success"><div class="label">통신 단위·회귀 검사</div><strong class="big">{v['unittest']['count']}개 통과</strong><p>기존 통신 12개 + 엄격 검사 11개.<br>정답 파일 미사용·관계 변형·기준 오염 방지·큰 본문 거절·식별자 위장 등을 확인했습니다.</p></article><article class="card success"><div class="label">실제 Chrome 화면 확인</div><strong class="big">{v['browser']['checks']}항목 통과</strong><p>자료 미리보기·복잡한 멀티·싱글 반복·역할 지정·권한 교환·가짜 증거·Context 위장을 확인했습니다. 모바일 가로 넘침과 실행 오류도 확인했습니다.</p></article></div>
<div class="section-label">원본과 요약을 다시 대조한 결과</div>
<ul class="bullet"><li>압축 원본에서 86개 조건을 다시 읽어 중복 조건·행 개수·판정이 summary와 일치하는지 확인했습니다.</li><li>정상 기준선의 3자료 × 5역할에서 각 20개 표본이 있는지 확인했습니다.</li><li>숫자·개수 등 기본 네 필드가 그대로인 응답 오류 32건을 원본에서 다시 대조했습니다.</li><li>원본 압축 파일의 SHA-256을 재확인했습니다. 검증 내역은 verification.json에 보관했습니다.</li></ul>
<div class="callout"><b>이번에 재실행한 검사만 현재 근거로 표시했습니다.</b><br>이전 v1의 테스트·브라우저 결과를 이번 결과에 합산하지 않았습니다. 기존 제품의 전체 테스트나 실제 회원가입·파일 업로드·보안 스캐너 검증을 수행한 결과도 아닙니다.</div>
"""))

    summary_hash=hashlib.sha256(summary_file.read_bytes()).hexdigest()
    pages.append(("SCOPE, EVIDENCE & REPRODUCTION", f"""
<div class="eyebrow">07 / 데이터가 복잡해졌다고 검증 범위까지 확대해 말하지 않음</div>
<h2>결과를 어디까지 믿을 수 있는가?</h2>
<div class="space-sm">{_table(['질문', '현재 확인한 범위'],[
['보낸 데이터가 그대로 도착하는가?','합성 3자료의 정상 조건 338건에서 정확 대조 통과'],
['숫자만 맞고 내용이 틀린 응답을 찾는가?','고정 계약 안에서 32건 탐지 / 자유 서술은 미검증'],
['실제 LLM의 할루시네이션이 없는가?','미검증 — 이번 실험에서 LLM을 호출하지 않음'],
['속도 구간을 나누는가?','로컬 공통 시계에서 준비·직렬화·전달·처리·완료 확인'],
['실제 개발·QA·보안 품질이 좋은가?','미검증 — 코드 생성·MCP·빌드·제품 테스트·스캔 미실행'],
])}</div>
<p class="small space-sm">원본 경로는 실제 Agent 서버·A2A 클라이언트를 재사용하지만 업무 실행기는 진단용 목업입니다. NOT_EXECUTED를 PASS로 바꾼 주장을 탐지한 것이며, 실제 빌드나 보안 검사에 통과했다는 의미는 아닙니다.</p>
<div class="section-label">다음 실제 모델 검증에서 유지할 것</div>
<p>같은 요청과 고정 근거를 실제 LLM 실행 경계에 연결하고, 도구 실행 영수증·코드·테스트 로그로 주장을 대조해야 합니다. 자연어는 별도 평가 기준과 수동 검토를 병행해야 합니다.</p>
<div class="source-box"><b>현재 폴더의 근거 파일</b><br>
<code>summary.json</code> — 86조건 요약·조건·시간·환경·수집 시점 소스 해시<br>
<code>observations.jsonl.gz</code> — 전체 송수신·실행기·차이·ID·시간 원본 / {d['rawEvidence']['bytes']:,}바이트<br>
<code>verification.json</code> — 23개 회귀·16개 화면 검사·원본 재대조 근거<br>
<code>report.html / report.pdf</code> — 편집·재인쇄용 원본 및 공유용 보고서<br>
<span class="muted">원본 압축 SHA-256</span><div class="hash">{d['rawEvidence']['sha256']}</div>
<span class="muted">summary SHA-256</span><div class="hash">{summary_hash}</div></div>
<div class="section-label">같은 행렬을 다시 실행 / 기존 근거 폴더는 덮어쓰지 않음</div>
<pre>PYTHONPATH=src .venv/bin/python -m diagnostics.communication.strict_suite \\
  --out /tmp/a2a-strict-new-run

PYTHONPATH=src .venv/bin/python -m diagnostics.communication.report \\
  docs/reports/agent-communication-strict-v2-2026-11-08</pre>
<p class="small space-sm">첫 명령은 새 폴더에 행렬·프로토콜 근거를 수집합니다. 두 번째는 회귀·브라우저 검사 내역 verification.json이 있는 이 폴더의 HTML을 재생성합니다. Chrome 인쇄에서 A4·배경 그래픽·여백 없음을 선택하면 PDF로 저장할 수 있습니다. 표본 수와 측정 환경이 같아도 시간값이 완전히 같지는 않습니다. --quick 결과는 이번 전체 행렬의 대체 근거가 아닙니다.</p>
"""))

    sections=[]
    for number,(title,body) in enumerate(pages,1):
        sections.append(f'<section class="page"><header><span class="brand">atelier<i>.</i></span><span>AGENT COMMUNICATION · STRICT VALIDATION v2</span><b>{number:02d} / {escape(title)}</b></header>{body}<footer><span>합성 사용자 요청 · 실제 로컬 A2A · 목업 실행기 · 2026.11.08</span><span>{number:02d} / {len(pages):02d}</span></footer></section>')
    html='<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Agent 통신 엄격 검증 v2 — 2026.11.08</title><style>'+STYLE+'</style></head><body>'+''.join(sections)+'</body></html>'
    target=directory/'report.html'
    target.write_text(html,encoding='utf-8')
    return target


def main() -> None:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    print(build_report(args.directory))


if __name__=='__main__':
    main()
