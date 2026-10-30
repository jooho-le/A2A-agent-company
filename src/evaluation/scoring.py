# 최종 시험 점수를 독립 평가 보고서에서 계산한다(Ver1·Ver2 비교의 주 점수).
#   exam_score(report) → 요구사항별 같은 비중 점수, 검사 완료율, 확인된 보안 위반
#   practice_effect(groups) → (순환용 시험지 점수 − 최종 시험지 점수)의 Ver1·Ver2 간 차이(보조 지표)
#
# 점수 규칙(실험 전에 고정)
#   - 요구사항마다 "통과한 필수 검사 비율"을 구하고, 요구사항들의 단순 평균을 점수로 쓴다. 검사가 많은 요구사항이
#     더 큰 비중을 갖지 않게 한다(REQ-003은 검사 3개, REQ-001은 1개여도 같은 비중).
#   - 미검증(ERROR·NOT_RUN)은 통과로 세지 않고 분모에서도 빼지 않는다. 그래서 미검증이 있으면 점수는 "잠정 하한"이고,
#     검사 완료율(PASS·FAIL로 확인한 비율)과 함께 읽는다. 승인된 적용 제외(NOT_APPLICABLE)만 분모에서 뺀다.
#   - 코드·환경 신분(Manifest)을 확인했다고 적혀 있지 않은 보고서(metadataMatches가 True가 아님)는 점수를 만들지 않는다(None).
#   - 요구사항이 가리키는 검사의 결과 행이 보고서에 없으면 실행하지 않은 검사(NOT_RUN)로 센다(분모에서 빠지지 않게).
#   - 비밀번호 평문 저장·노출 같은 Security 검사 FAIL은 점수와 별도로 목록으로 드러낸다(높은 점수가 가리지 않게).
#
# [처음 읽는 분을 위한 용어]
# - 차이의 차이(practiceEffect): (Ver1의 순환용 − 최종 점수 차이) − (Ver2의 순환용 − 최종 점수 차이). Ver1은 개발 중에
#   순환용 시험지 결과를 보고 고치므로 연습한 문제에서 점수가 더 오를 수 있다는 것을 보려는 값이다. 다만 계산식만으로
#   "순수한 연습 효과"가 보장되지는 않는다(시험지 난이도가 실제 생성 코드에서도 같다는 증명이 없고, Run마다 변동이 있다).
#   그래서 주 결과는 같은 최종 시험지의 점수·완료율·보안 위반·시간이고, 이 값은 보조 지표로만 쓴다.
#   미검증이 섞인 점수는 하한이라 두 하한의 차이는 아무 쪽의 한계도 아니다. 그래서 확정 값은 두 시험지 모두 완료된
#   Run으로만 계산하고, 미검증을 포함한 Run은 가능한 범위(practiceEffectRange)로 따로 적는다.
from collections import Counter
from typing import Mapping, Optional


def exam_score(report: Mapping) -> dict:
    # 돌려주는 값: {'score', 'scoreUpperBound', 'completionRate', 'scoreIsLowerBound', 'requirementShares',
    #              'securityViolations', 'missingCheckIds', 'reason'}
    # scoreUpperBound: 미검증을 모두 통과로 볼 때의 점수(가능한 범위의 위쪽). 미검증이 없으면 score와 같다.
    manifest = report.get('manifestCheck') or {}
    if manifest.get('errorCode') or manifest.get('metadataMatches') is not True:
        return {'score': None, 'scoreUpperBound': None, 'completionRate': None, 'scoreIsLowerBound': None,
                'requirementShares': {}, 'securityViolations': [], 'missingCheckIds': [], 'reason': 'MANIFEST_NOT_CONFIRMED'}
    checks = {row['testId']: row for row in report.get('checks', [])}
    shares, upper, verified, total, missing = {}, {}, 0, 0, []
    for requirement in report.get('requirements', []):
        statuses = []
        for check_id in requirement.get('checkIds', []):
            row = checks.get(check_id)
            if row is None:  # 결과 행이 없는 검사: 실행하지 않은 필수 검사로 센다
                missing.append(check_id)
                statuses.append('NOT_RUN')
            elif row.get('required', True) and row.get('status') != 'NOT_APPLICABLE':
                statuses.append(row.get('status'))
        if not statuses:
            continue
        key = str(requirement['requirementId'])
        shares[key] = sum(status == 'PASS' for status in statuses) / len(statuses)
        upper[key] = sum(status != 'FAIL' for status in statuses) / len(statuses)
        verified += sum(status in ('PASS', 'FAIL') for status in statuses)
        total += len(statuses)
    completion = verified / total if total else None
    violations = sorted((row.get('title') or row['testId']).split(' ', 1)[0] for row in checks.values()
                        if row.get('perspective') == 'SECURITY' and row.get('status') == 'FAIL')
    return {'score': sum(shares.values()) / len(shares) if shares else None,
            'scoreUpperBound': sum(upper.values()) / len(upper) if upper else None, 'completionRate': completion,
            'scoreIsLowerBound': None if completion is None else completion < 1,
            'requirementShares': shares, 'securityViolations': violations, 'missingCheckIds': sorted(missing),
            'reason': None if shares else 'NO_SCORED_REQUIREMENTS'}


def _mean(values) -> Optional[float]:
    # None을 뺀 평균. 남는 값이 없으면 None.
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def practice_effect(groups: Mapping[str, list], pairs: Optional[Mapping[str, str]] = None, unscored=()) -> dict:
    # groups: {'IN_LOOP_AND_FINAL': [(최종 보고서, 순환용 참고 보고서), ...], 'FINAL_ONLY': [...]} (Run마다 한 쌍)
    # pairs : {Run 번호: 쌍 번호}(선택). 짝지은 실험이면 한쪽 Run이 빠질 때 그 쌍의 다른 Run도 확정 계산에서 뺀다.
    # unscored: 채점 결과가 아예 없는 Run 번호들(groups에 없음). 짝이면 그 쌍도 깨진 것으로 본다.
    # 돌려주는 값: 버전별 평균 점수·평균 차이(순환용 − 최종)와 차이의 차이(Ver1 차이 − Ver2 차이).
    #   확정 값(meanGap·practiceEffect): 두 시험지 모두 완료(미검증 없음)된 Run만. 뺀 Run 수와 사유를 excludedRuns에 적는다
    #     (NO_SCORE: 점수 없음, INCOMPLETE: 미검증 있음, PAIR_PARTNER_EXCLUDED: 짝 Run이 빠짐).
    #   가능한 범위(gapRange·practiceEffectRange): 점수가 있는 Run에서 미검증을 FAIL·PASS로 볼 때의 아래·위 끝.
    #     짝지은 실험이면 확정 값과 같은 쌍 기준으로, 점수가 아예 없거나 채점되지 않은 Run의 짝도 범위에서 뺀다
    #     (두 버전의 범위가 서로 다른 Run 묶음으로 계산되지 않게). 뺀 Run과 사유는 rangeExcludedRuns에 적는다
    #     (NO_SCORE: 점수 없음, PAIR_PARTNER_NO_SCORE: 짝 Run이 점수 없음·미채점).
    rows = {}
    for policy, items in groups.items():
        for final_report, reference_report in items:
            final, practice = exam_score(final_report), exam_score(reference_report)
            if final['score'] is None or practice['score'] is None:
                reason = 'NO_SCORE'
            elif final['scoreIsLowerBound'] or practice['scoreIsLowerBound']:
                reason = 'INCOMPLETE'
            else:
                reason = None
            rows.setdefault(policy, []).append([final_report.get('sourceRunId'), final, practice, reason])
    unscored_pairs = {pairs[run_id] for run_id in unscored if run_id in pairs} if pairs else set()
    no_score_pairs = unscored_pairs | ({pairs[row[0]] for items in rows.values() for row in items
                                        if row[3] == 'NO_SCORE' and row[0] in pairs} if pairs else set())
    if pairs:
        broken = {pairs[row[0]] for items in rows.values() for row in items if row[3] and row[0] in pairs} | unscored_pairs
        for items in rows.values():
            for row in items:
                if row[3] is None and pairs.get(row[0]) in broken:
                    row[3] = 'PAIR_PARTNER_EXCLUDED'
    result = {}
    for policy in groups:
        items = rows.get(policy, [])
        confirmed = [(final, practice) for _run, final, practice, reason in items if reason is None]
        bounds, range_excluded = [], Counter()
        for run, final, practice, reason in items:
            if reason == 'NO_SCORE':
                range_excluded['NO_SCORE'] += 1
            elif pairs and pairs.get(run) in no_score_pairs:
                range_excluded['PAIR_PARTNER_NO_SCORE'] += 1
            else:
                bounds.append((practice['score'] - final['scoreUpperBound'], practice['scoreUpperBound'] - final['score']))
        excluded = Counter(reason for *_rest, reason in items if reason)
        result[policy] = {'runs': len(confirmed), 'skippedRuns': sum(excluded.values()), 'excludedRuns': dict(excluded),
                          'meanExamScore': _mean(final['score'] for final, _practice in confirmed),
                          'meanPracticeScore': _mean(practice['score'] for _final, practice in confirmed),
                          'meanGap': _mean(practice['score'] - final['score'] for final, practice in confirmed),
                          'gapRange': [_mean(low for low, _high in bounds), _mean(high for _low, high in bounds)]
                          if bounds else None, 'rangeRuns': len(bounds), 'rangeExcludedRuns': dict(range_excluded)}
    loop, final_only = result.get('IN_LOOP_AND_FINAL', {}), result.get('FINAL_ONLY', {})
    effect = (loop['meanGap'] - final_only['meanGap']
              if loop.get('meanGap') is not None and final_only.get('meanGap') is not None else None)
    effect_range = ([loop['gapRange'][0] - final_only['gapRange'][1], loop['gapRange'][1] - final_only['gapRange'][0]]
                    if loop.get('gapRange') and final_only.get('gapRange') else None)
    return {'policies': result, 'practiceEffect': effect, 'practiceEffectRange': effect_range,
            'meaning': 'Ver1·Ver2 간 (순환용 − 최종) 점수 차이의 차이. 보조 지표이며 순수한 연습 효과로 단정하지 않는다'}
