# 최종 시험 점수를 독립 평가 보고서에서 계산한다(Ver1·Ver2 비교의 주 점수).
#   exam_score(report) → 요구사항별 같은 비중 점수, 검사 완료율, 확인된 보안 위반
#   practice_effect(groups) → 연습 효과(순환용 시험지 점수 − 최종 시험지 점수의 버전 간 차이)
#
# 점수 규칙(실험 전에 고정)
#   - 요구사항마다 "통과한 필수 검사 비율"을 구하고, 요구사항들의 단순 평균을 점수로 쓴다. 검사가 많은 요구사항이
#     더 큰 비중을 갖지 않게 한다(REQ-003은 검사 3개, REQ-001은 1개여도 같은 비중).
#   - 미검증(ERROR·NOT_RUN)은 통과로 세지 않고 분모에서도 빼지 않는다. 그래서 미검증이 있으면 점수는 "잠정 하한"이고,
#     검사 완료율(PASS·FAIL로 확인한 비율)과 함께 읽는다. 승인된 적용 제외(NOT_APPLICABLE)만 분모에서 뺀다.
#   - 코드·환경 신분(Manifest)을 확인하지 못한 보고서는 점수를 만들지 않는다(None).
#   - 비밀번호 평문 저장·노출 같은 Security 검사 FAIL은 점수와 별도로 목록으로 드러낸다(높은 점수가 가리지 않게).
#
# [처음 읽는 분을 위한 용어]
# - 연습 효과: Ver1은 개발 중에 순환용 시험지 결과를 보고 고친다. 그래서 순환용 점수가 최종 점수보다 높게 나올 수 있다.
#   Ver2는 두 시험지 모두 처음 보므로 Ver2의 (순환용 − 최종) 차이는 두 시험지의 난이도 차이다. Ver1의 차이에서
#   Ver2의 차이를 빼면 난이도 차이가 지워지고 "같은 문제를 연습한 효과"만 남는다(차이의 차이).
from typing import Mapping, Optional


def exam_score(report: Mapping) -> dict:
    # 돌려주는 값: {'score', 'completionRate', 'scoreIsLowerBound', 'requirementShares', 'securityViolations', 'reason'}
    manifest = report.get('manifestCheck') or {}
    if manifest.get('errorCode') or manifest.get('metadataMatches') is False:
        return {'score': None, 'completionRate': None, 'scoreIsLowerBound': None, 'requirementShares': {},
                'securityViolations': [], 'reason': 'MANIFEST_NOT_CONFIRMED'}
    checks = {row['testId']: row for row in report.get('checks', [])}
    shares, verified, total = {}, 0, 0
    for requirement in report.get('requirements', []):
        rows = [checks[check_id] for check_id in requirement.get('checkIds', [])
                if check_id in checks and checks[check_id].get('required', True)
                and checks[check_id].get('status') != 'NOT_APPLICABLE']
        if not rows:
            continue
        shares[str(requirement['requirementId'])] = sum(row['status'] == 'PASS' for row in rows) / len(rows)
        verified += sum(row['status'] in ('PASS', 'FAIL') for row in rows)
        total += len(rows)
    completion = verified / total if total else None
    violations = sorted((row.get('title') or row['testId']).split(' ', 1)[0] for row in checks.values()
                        if row.get('perspective') == 'SECURITY' and row.get('status') == 'FAIL')
    return {'score': sum(shares.values()) / len(shares) if shares else None, 'completionRate': completion,
            'scoreIsLowerBound': None if completion is None else completion < 1,
            'requirementShares': shares, 'securityViolations': violations, 'reason': None}


def _mean(values) -> Optional[float]:
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def practice_effect(groups: Mapping[str, list]) -> dict:
    # groups: {'IN_LOOP_AND_FINAL': [(최종 보고서, 순환용 참고 보고서), ...], 'FINAL_ONLY': [...]} (Run마다 한 쌍)
    # 돌려주는 값: 버전별 평균 점수·평균 차이(순환용 − 최종)와 연습 효과(Ver1 차이 − Ver2 차이).
    # 한쪽이라도 점수를 낼 수 없는 Run은 그 Run만 빼고, 빼낸 수를 함께 적는다.
    result = {}
    for policy, pairs in groups.items():
        exam, practice, gaps, skipped = [], [], [], 0
        for final_report, reference_report in pairs:
            final_value, practice_value = exam_score(final_report)['score'], exam_score(reference_report)['score']
            if final_value is None or practice_value is None:
                skipped += 1
                continue
            exam.append(final_value)
            practice.append(practice_value)
            gaps.append(practice_value - final_value)
        result[policy] = {'runs': len(gaps), 'skippedRuns': skipped, 'meanExamScore': _mean(exam),
                          'meanPracticeScore': _mean(practice), 'meanGap': _mean(gaps)}
    loop, final_only = result.get('IN_LOOP_AND_FINAL', {}), result.get('FINAL_ONLY', {})
    effect = (loop['meanGap'] - final_only['meanGap']
              if loop.get('meanGap') is not None and final_only.get('meanGap') is not None else None)
    return {'policies': result, 'practiceEffect': effect}
