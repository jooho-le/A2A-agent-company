"""Offline, multi-stage report. Failed calibrations remain visible."""

import argparse
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
from statistics import median

from diagnostics.communication.reporting import ROLE_LABELS, CASE_LABELS, _kst, _number, _table, _text
from diagnostics.communication.natural import transport_passed


CASES = {**CASE_LABELS, '04-office-page': '회사 소개 화면'}
STATES = {'SUPPORTED': '모델 지지', 'REJECTED': '거절', 'UNVERIFIED': '미검증·보류'}
GATES = {'INPUT_REQUIRED': '질문 대기', 'CONTENT_CONTRACT_REJECTED': '구조 거절',
    'DIAGNOSTIC_CONTRACT_AND_MODEL_REVIEW_ADMITTED': '진단 인계 허용',
    'SEMANTIC_REVIEW_UNVERIFIED': '의미 검토 보류', 'SEMANTIC_REVIEW_REJECTED': '의미 검토 거절',
    'UPSTREAM_INPUT_REQUIRED': '앞단 질문 대기', 'UPSTREAM_CONTRACT_REJECTED': '앞단 구조 거절',
    'UPSTREAM_SEMANTIC_REVIEW_UNVERIFIED': '앞단 검토 보류', 'UPSTREAM_SEMANTIC_REVIEW_REJECTED': '앞단 검토 거절'}


def _rows(evidence):
    return [row for group in evidence.get('runs', []) for row in group['rows']]


def _median(values):
    values = [value for value in values if type(value) in (int, float) and isfinite(value) and value >= 0]
    return median(values) if values else None


def summary(initial, calibrations, final):
    role_rows = _rows(initial) + _rows(final)
    completed = [row for row in role_rows if row['phase'] == 'COMPLETED']
    calls = [call for item in (initial, *calibrations, final) for call in item.get('calls', [])]
    if len(calls) > 40:
        raise ValueError('REPORT_COMBINED_CALL_CAP_EXCEEDED')
    return {'calls': len(calls), 'http200': sum(call.get('httpStatus') == 200 for call in calls),
        'roleCalls': len(initial['roleCalls']) + len(final['roleCalls']),
        'pipelineReviewCalls': len(initial['reviewCalls']) + len(final['reviewCalls']),
        'calibrationCalls': sum(len(item.get('calls', [])) for item in calibrations),
        'completedRows': len(completed), 'skippedRows': sum(row['phase'] == 'SKIPPED' for row in role_rows),
        'matchedRows': sum(transport_passed(row) for row in completed),
        'inputTokens': sum((call.get('usage') or {}).get('input_tokens', 0) for call in calls),
        'outputTokens': sum((call.get('usage') or {}).get('output_tokens', 0) for call in calls),
        'finalAllowed': sum(row['handoff']['eligible'] for row in _rows(final)),
        'finalCompleted': sum(row['phase'] == 'COMPLETED' for row in _rows(final)),
        'actualTransport': all(item.get('provenance', {}).get('transportMode') == 'DEFAULT_HTTPS_TRANSPORT'
            for item in (initial, *calibrations, final))}


def _card(title, text):
    return '<article><h2>' + _text(title) + '</h2><p>' + _text(text) + '</p></article>'


def render(initial, calibrations, final, sources):
    counts = summary(initial, calibrations, final)
    pages = []
    def page(title, subtitle, content):
        n = len(pages) + 1
        pages.append('<section class="page"><header><span>ATELIER / GUARDED COMMUNICATION</span><b>' +
            _text(_kst(initial.get('startedAt')).split(' ')[0]) + '</b></header><h1>' + _text(title) +
            '</h1><p class="subtitle">' + _text(subtitle) + '</p>' + content +
            '<footer><span>실제 API 진단 / 제품 코드·MCP·Docker·실제 테스트는 실행하지 않음</span><b>' + str(n) + ' / 8</b></footer></section>')

    first = initial['summary']
    body = '<div class="lead">' + ('실제 OpenAI API 실행 근거' if counts['actualTransport'] else '합성/미확인 Transport · 실제 API로 확정하지 않음') + '</div>'
    body += _card('1. 보내고 제대로 받았나', f'실제 역할 응답 {counts["completedRows"]}건 중 송수신·DB·metadata·API 입력 {counts["matchedRows"]}건 일치. 후속 미호출은 전송 유실과 분리했습니다.')
    body += _card('2. 할루시네이션은 없어졌나', '그렇게 단정할 수 없습니다. 초기 의미 검토는 개인정보 범위 확대를 놓쳤습니다. 실패 결과를 보존하고 필드별 근거·인용 고정·보수적 보류 규칙을 보강했습니다.')
    body += _card('3. 속도는 어떤가', '의미 검토를 위해 API를 추가 호출합니다. Agent 처리 시간과 역할 API·검토 API 시간을 구분했으며, 호출이 줄어든 차단 케이스를 성능 향상으로 계산하지 않았습니다.')
    body += _table(['호출 구분', '실제 횟수'], [['역할 응답', counts['roleCalls']], ['파이프라인 의미 검토', counts['pipelineReviewCalls']],
        ['정상/고의 오류 대조 검토', counts['calibrationCalls']], ['합계 / HTTP 200 / 전체 상한', f'{counts["calls"]} / {counts["http200"]} / 40']])
    body += '<p class="note">보완 후 회사 소개 재실행: 응답 ' + str(counts['finalCompleted']) + '건, 인계 허용 ' + str(counts['finalAllowed']) + '건. 이 값은 제품 웹페이지가 만들어졌거나 보안 검사를 통과했다는 뜻이 아닙니다.</p>'
    page('75~80번, 무엇을 보강하고 확인했나', '초기 실패와 보완 후 결과를 함께 남긴 실제 통신 진단입니다.', body)

    body = ''.join(_card(CASES.get(case, case), text) for case, text in initial['caseRequests'].items())
    body += _table(['조건', '방법'], [['모델 / 온도', initial['modelId'] + ' / ' + str(initial['temperature'])],
        ['역할 답변 출력 상한', str(initial['maxOutputTokens']) + ' tokens'],
        ['초기 검토 / 최종 검토 상한', str(initial['reviewMaxOutputTokens']) + ' / ' + str(final['reviewMaxOutputTokens']) + ' tokens'],
        ['경로', '기존 A2A·SQLite 기반 진단 실행기. 제품 Host·승인 원장·MCP 실행 경로와 구분'],
        ['모델 입력', '자연어 원문 + 역할 지시 + 이전 실제 답변. 보호된 기준·정답은 제외'],
        ['재실험', '4요청 초기 실행 → 동일 대조 입력의 검토 보강 → 회사 소개만 재실행. Retry·모델 교체 없음']])
    page('어떤 자연어로 실행했나', '아래 문장을 그대로 입력했습니다. 초기 4개와 보완 후 1개의 범위를 구분합니다.', body)

    table_rows = []
    for label, evidence in [('초기', initial), ('보완 후', final)]:
        for group in evidence['runs']:
            for row in group['rows']:
                dispatched = row['phase'] == 'COMPLETED'
                gate = row.get('error') if row['phase'] == 'SKIPPED' else row['handoff']['reason']
                table_rows.append([label, CASES.get(group['caseId'], group['caseId']) + (' / M' if group['architecture'] == 'MULTI_AGENT' else ' / S'),
                    ROLE_LABELS[row['role']], '응답 완료' if dispatched else '미호출' if row['phase'] == 'SKIPPED' else row['phase'],
                    '일치' if dispatched and row['checks']['response']['status'] == 'PASS' else '미관측', GATES.get(gate, gate)])
    body = '<div class="dense">' + _table(['시점', '요청 / 구조', '역할', '실행', '송수신', '후속 처리'], table_rows) + '</div>'
    body += '<p class="note">M=멀티, S=싱글. ‘미호출’은 앞 역할의 질문·구조 오류·검토 보류 때문에 요청을 보내지 않은 상태입니다. 송수신 성공률의 분모에 전송하지 않은 요청을 넣지 않습니다. API 검토 호출은 별도이며 이 A2A 표의 역할 수와 같지 않습니다.</p>'
    page('1. 실제 전달과 후속 미호출', '초기 20개 예정 역할과 최종 회사 소개 5개 예정 역할을 표시합니다.', body)

    body = _table(['초기 4요청 판정', '행 수'], [['실제 응답 / 송수신 일치', f'{first["completedRows"]} / {first["transportMatchedRows"]}'],
        ['정상 질문 대기', first['inputRequiredRows']], ['구조 계약 거절', first['literalContractRejected']],
        ['인계 허용', first['handoffEligibleRows']], ['앞단 차단으로 후속 미호출', first['skippedRows']]])
    body += _card('블로그·전자문서: 질문하고 기다림', '소통 기능·접근 범위나 삭제 기간을 임의로 확정하지 않고 질문 대기로 남겼습니다. 블로그 멀티와 싱글, 전자문서 멀티가 이 분기였습니다.')
    body += _card('전자문서 싱글: 실제 인용 오타', '검사 인용에 원문 “기간을” 대신 “기간를”이 들어갔습니다. 원문과 다른 인용이어서 차단했으며, 이를 데이터 전송 오류로 세지 않았습니다. 의미 검토 API도 추가 호출하지 않았습니다.')
    body += _card('추리게임 멀티·싱글: 검사 연결 누락', '요구사항 목록에는 조건이 있었지만 참가자·금지 기능·대사 등의 일부 조건이 검사 계획에 연결되지 않았습니다. READY라고 답했어도 후속 실행을 허용하지 않았습니다.')
    body += '<p class="note">인용 오타·검사 연결 누락은 확인된 계약 오류입니다. 이를 모두 사실 환각이라고 묶거나, 인용만 맞으면 의미까지 정확하다고 판정하지 않습니다. 세 거절 응답의 자유 서술은 의미 검토 API를 거치지 않아 검증되지 않았습니다.</p>'
    page('차단된 이유를 나눠 읽기', '잘못된 기대값·없는 인용·빠진 연결을 구조 단계에서 거절합니다.', body)

    control_names = {'control-supported': '정상 회사 소개 응답', 'injected-privacy-expansion': '고의 개인정보 범위 확대',
        'injected-unapproved-features': '고의 접근 제한·기능 추가'}
    values = []
    for n, calibration in enumerate(calibrations, 1):
        for item in calibration['results']:
            status = item['review']['status']
            safety = status == 'SUPPORTED' if item['expected'] == 'SUPPORTED' else status in {'REJECTED', 'UNVERIFIED'}
            values.append([n, control_names[item['caseId']], STATES.get(status, status),
                '일치' if item['matchedExpected'] else '불일치', '맞음' if safety else '잘못 허용/보류'])
    body = _table(['검토판', '대조 입력', '실제 처리', '예상 의미 판정', '허용·보류 정책'], values)
    body += _card('초기 검토가 놓친 실제 한계', '첫 검토기는 위치정보 제외를 모든 개인정보 미수집으로 바꾼 고의 오류를 SUPPORTED로 판단했습니다. 이 실패를 삭제하거나 성공으로 바꾸지 않았습니다.')
    body += _card('필드별 검토의 근거도 검증', '중간판에서는 검사 이름 대신 기대값을 인용하여 정상 응답도 미검증 처리됐습니다. 최종판은 각 필드의 실제 문장을 Schema에 고정하고, 원문 인용·전체 필드 판정·응답 해시를 확인합니다.')
    body += '<p class="note">고의 오류 2개는 Host가 만든 검토기 대조 자료입니다. 실제 사용자 요청에서 모델이 새로 만든 오류 2건이라는 뜻이 아닙니다. ‘의미 판정 일치’와 ‘미검증 보류로 잘못된 인계를 막음’은 서로 다른 지표이며, 샘플 3개로 일반 검출률을 발표하지 않습니다.</p>'
    page('2. 의미 검토기 자체도 틀렸다', '같은 정상 1개·고의 오류 2개를 단계별로 대조했습니다. 최초 실패도 표시합니다.', body)

    body = _card('원문 인용과 의미 정확성', '원문을 그대로 인용하고 같은 요구사항 ID를 유지해도, 요약이나 역할 설명이 과장될 수 있습니다. 모델의 SUPPORTED는 보조 판단이며 사용자 승인·제품 성공이 아닙니다.')
    body += _card('알려진 개인정보 확대는 보수적으로 보류', '일부 정보만 제외한 원문에 모든 개인정보 미수집을 단정하는 문구가 나오면 보류하는 보조 규칙을 추가했습니다. 이 규칙은 제한된 텍스트 단서 기반이며 동의어·문맥 전체를 이해하는 범용 판정기가 아닙니다. 잘못 보류하는 경우도 확인해야 합니다.')
    body += _card('검토 근거가 부족하면 후속 실행 중단', '모델이 SUPPORTED라고 해도 필드 누락·위조 인용·검토 뒤 응답 변경·API 입력 대조 누락이 있으면 인계하지 않습니다. 원래 응답과 검토 모델의 원래 판정은 함께 보존합니다.')
    body += _card('이번에 증명하지 않은 것', '실제 코드 생성·MCP 실행·Docker 빌드·보호된 QA/보안 테스트·수정 반복·프론트 Trace 연결은 실행하지 않았습니다. 명확한 회사 소개 요청의 진단 인계가 허용된 것과 실제 웹페이지 완성은 별개입니다.')
    body += '<p class="note">검토 보강에 사용한 기존 실패 패턴을 다시 확인한 표적 회귀 실험입니다. 독립 사람 평가, 새로운 대규모 평가셋, 완전한 할루시네이션 제거, 제품의 안전성 인증으로 해석하지 않습니다. 실제 제품의 승인 원장과 별도 검증이 필요합니다.</p>'
    page('남는 위험과 해석의 범위', '판정을 과장하지 않고 후속 확인이 필요한 부분을 남깁니다.', body)

    timing_rows = []
    for label, evidence in [('초기', initial), ('최종', final)]:
        for role in ROLE_LABELS:
            rows = [row for row in _rows(evidence) if row['role'] == role and row['phase'] == 'COMPLETED']
            reviewed = [row for row in rows if (row.get('semanticReview') or {}).get('model')]
            timing_rows.append([label, ROLE_LABELS[role], len(rows),
                _number(_median([row['timings'].get('agentProcessingMs') for row in rows]), seconds=True),
                _number(_median([row['timings'].get('providerExchangeMs') for row in rows]), seconds=True), len(reviewed),
                _number(_median([row['semanticReview']['model']['timings'].get('providerExchangeMs') for row in reviewed]), seconds=True)])
    body = '<div class="dense">' + _table(['시점', '역할', '응답 수', 'Agent 중앙값', '역할 API 중앙값', '검토 수', '검토 API 중앙값'], timing_rows) + '</div>'
    body += '<div class="dense">' + _table(['시점', '요청 / 구조', '진단 실행 시간'], [[label,
        CASES[group['caseId']] + (' / M' if group['architecture'] == 'MULTI_AGENT' else ' / S'), _number(group['elapsedMs'], seconds=True)]
        for label, evidence in [('초기', initial), ('최종', final)] for group in evidence['runs']]) + '</div>'
    body += _table(['전체 API 소비', '관측 합계'], [['입력 / 출력 토큰', f'{counts["inputTokens"]:,} / {counts["outputTokens"]:,}'],
        ['HTTP 200 / 전체 호출', f'{counts["http200"]} / {counts["calls"]}']])
    body += '<p class="note">Agent 시간에는 역할 API·검토·구조 검사·서버 처리가 포함됩니다. 두 API 구간은 네트워크·서버 처리·본문 수신을 포함하며 순수 추론 시간이 아닙니다. 표본 1개인 역할도 있으므로 우열·장기 p95·속도 개선율을 주장하지 않습니다. 초기/최종은 범위·검토 계약이 다릅니다. 병렬 QA·Security를 전체시간에 더하지 않았습니다.</p>'
    page('3. 추가 검토에 시간이 얼마나 들었나', '초기 4요청과 최종 회사 소개 1요청의 시간을 분리했습니다.', body)

    body = '<div class="sources">' + ''.join('<p><b>' + _text(label) + '</b><br>' + _text(path) +
        '<br>SHA-256 ' + _text(digest) + '</p>' for label, path, digest in sources) + '</div>'
    body += _table(['다음 번호', '해야 할 작업'], [['81', '제품 Host·Agent·MCP·Docker를 실제 실행 환경에서 가동하고 Configuration·인증·경로 확인'],
        ['82', '실제 생성→빌드→QA·보안→실패 수정→재검증 전체 흐름과 저장된 결과 확인']])
    body += '<p class="note">원자료와 실행 당시 소스는 Git 제외 .data에 보존했습니다. 버전별 실패를 덮어쓰지 않았습니다. 공유 보고서는 PDF만 추가하며 키·인증 헤더를 포함하지 않습니다. 프론트 Trace 연결은 세화와, 정식 비교 실험은 석민과 별도 진행합니다.</p>'
    page('재현 근거와 다음 작업', '원자료의 호출·출력·정책 판정은 소급 수정하지 않았습니다.', body)
    css = '''@page{size:A4;margin:0}*{box-sizing:border-box}body{margin:0;color:#172331;font-family:-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif;font-size:11px;line-height:1.55}.page{width:210mm;height:297mm;padding:12mm 13mm 16mm;position:relative;break-after:page;overflow:hidden}.page:last-child{break-after:auto}header{display:flex;justify-content:space-between;font-size:9px;color:#526478;border-bottom:2px solid #1558c9;padding-bottom:9px}h1{font-size:26px;line-height:1.25;letter-spacing:-1px;margin:18px 0 8px}h2{font-size:14px;margin:0 0 7px}p{margin:7px 0}.subtitle{color:#617087;margin-bottom:14px}.lead{background:#1558c9;color:white;padding:13px 15px;border-radius:9px;font-size:15px;font-weight:700}article{border:1px solid #dce4ef;border-radius:9px;padding:11px 14px;margin:11px 0}table{width:100%;border-collapse:collapse;font-size:10px;margin:12px 0}td,th{padding:6px;text-align:left;border-bottom:1px solid #dce4ef;vertical-align:top}th{background:#eef3fb;color:#233d63}tr{break-inside:avoid}.dense table{font-size:9.5px}.dense td,.dense th{padding:5px 6px}.note{background:#f1f5fa;padding:11px 13px;border-radius:8px;color:#405168;font-size:10.5px;margin-top:12px}.sources{font-family:monospace;font-size:8.6px;line-height:1.65;overflow-wrap:anywhere}.sources p{border-bottom:1px solid #dce4ef;padding:7px 0}footer{position:absolute;bottom:8mm;left:13mm;right:13mm;display:flex;justify-content:space-between;font-size:8.2px;color:#718095;border-top:1px solid #dce4ef;padding-top:7px}'''
    css += 'body{font-size:13px}h2{font-size:15px}table{font-size:11px}.dense table{font-size:10.5px}.note{font-size:11.5px}.sources{font-size:9.3px}'
    return '<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>75~80 실제 통신 보완 보고서</title><style>' + css + '</style></head><body>' + ''.join(pages) + '</body></html>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--initial', required=True, type=Path)
    parser.add_argument('--calibration', required=True, action='append', type=Path)
    parser.add_argument('--final', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    paths = [args.initial, *args.calibration, args.final]
    raw = [path.read_bytes() for path in paths]
    data = [json.loads(value) for value in raw]
    labels = ['초기 4요청', *['검토기 대조 ' + str(n + 1) for n in range(len(args.calibration))], '최종 회사 소개']
    sources = [(label, str(path), sha256(value).hexdigest()) for label, path, value in zip(labels, paths, raw)]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(data[0], data[1:-1], data[-1], sources), encoding='utf-8')
    print(json.dumps(summary(data[0], data[1:-1], data[-1]), ensure_ascii=False))


if __name__ == '__main__':
    main()
