"""같은 조건의 판정 비교. 실제 입력 저장·재실행은 evaluation.replay에 연결한다.

compare_reports/compare_files는 관찰한 보고서 비교일 뿐이다. repeat_snapshot은
보호 입력 원본의 해시를 확인하고 실제 시험을 재실행한 뒤 이 비교를 적용한다.
"""
import argparse
import hashlib
import json
from pathlib import Path

from .reporting import validate_contract


def repeat_snapshot(**kwargs):
    from .replay import repeat_evaluation
    return repeat_evaluation(**kwargs)


def compare_reports(first, second):
    result = {'schemaVersion': 1, 'status': 'UNVERIFIED', 'reason': None, 'differences': []}
    try:
        for report in (first, second):
            validate_contract(report, 'independent_evaluation')
    except Exception:
        return {**result, 'reason': 'REPORT_INVALID'}
    if first['evaluationId'] == second['evaluationId']:
        return {**result, 'reason': 'TWO_SEPARATE_EVALUATIONS_REQUIRED'}
    fields = ('sourceRunId', 'architecture', 'suite', 'expectedManifest', 'observedManifest')
    mismatch = [key for key in fields if first.get(key) != second.get(key)]
    if mismatch:
        return {**result, 'reason': 'REPEAT_CONDITIONS_DIFFER', 'mismatchedFields': mismatch}
    result.update(runId=first['sourceRunId'], manifest=first['expectedManifest'], suite=first['suite'])
    result['unverifiedChecks'] = {name: [{key: row.get(key) for key in ('testId', 'status', 'errorCode', 'reason')}
                                       for row in report['checks'] if row['status'] not in ('PASS', 'FAIL')]
                                  for name, report in (('first', first), ('second', second))}
    for report in (first, second):
        if not report['manifestCheck']['metadataMatches'] or report['manifestCheck']['errorCode']:
            return {**result, 'reason': 'MANIFEST_UNVERIFIED'}
    left, right = {}, {}
    for report, rows in ((first, left), (second, right)):
        for item in report['checks']:
            if item['testId'] in rows:
                return {**result, 'reason': 'DUPLICATE_CHECK'}
            rows[item['testId']] = item
    if set(left) != set(right):
        return {**result, 'reason': 'CHECK_SET_DIFFER'}
    for key in sorted(left):
        a, b = left[key], right[key]
        if any(a.get(field) != b.get(field) for field in ('requirementId', 'perspective', 'required')):
            return {**result, 'reason': 'CHECK_DEFINITION_DIFFER'}
        if a['status'] != b['status']:
            result['differences'].append({'testId': key, 'field': 'status', 'first': a['status'], 'second': b['status']})
        for field in ('errorCode', 'reason'):
            if a['status'] not in ('PASS', 'FAIL') or b['status'] not in ('PASS', 'FAIL'):
                if a.get(field) != b.get(field):
                    result['differences'].append({'testId': key, 'field': field, 'first': a.get(field), 'second': b.get(field)})
    if first['evaluationVerdict'] != second['evaluationVerdict']:
        result['differences'].append({'field': 'evaluationVerdict', 'first': first['evaluationVerdict'],
                                      'second': second['evaluationVerdict']})
    if result['differences']:
        # 미검증과의 전환은 제품 동작 변동으로 단정하지 않는다.
        known_change = any(d['field'] in ('status', 'evaluationVerdict') and d['first'] in ('PASS', 'FAIL') and d['second'] in ('PASS', 'FAIL')
                           for d in result['differences'])
        return {**result, 'status': 'FAIL' if known_change else 'UNVERIFIED', 'reason': 'REPEAT_RESULTS_DIFFER'}
    if not left or any(item['status'] not in ('PASS', 'FAIL') for item in [*left.values(), *right.values()]) \
            or first['evaluationVerdict'] not in ('PASS', 'FAIL'):
        return {**result, 'reason': 'REPEAT_CHECKS_UNVERIFIED'}
    return {**result, 'status': 'PASS', 'reason': 'OBSERVED_RESULTS_MATCH'}


def compare_files(first, second, out):
    paths = [Path(first), Path(second)]
    if paths[0].resolve() == paths[1].resolve():
        raise ValueError('TWO_SEPARATE_EVALUATIONS_REQUIRED')
    data = [path.read_bytes() for path in paths]
    loaded = [json.loads(value) for value in data]
    reports = [value.get('report', value) for value in loaded]
    result = compare_reports(*reports)
    result['inputSha256'] = [hashlib.sha256(value).hexdigest() for value in data]
    result['limitations'] = ['같은 판정의 재현 여부만 확인한다. 정확한 시험 입력 재현이나 평가기의 정확성을 증명하지 않는다.']
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--first', required=True, type=Path)
    parser.add_argument('--second', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    result = compare_files(args.first, args.second, args.out)
    return 0 if result['status'] == 'PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
