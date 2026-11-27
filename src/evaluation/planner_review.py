"""독립적인 사람 검토를 정확한 Run·계획 내용에 연결한다. 에이전트 자체 의미 검토와 별개다."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path

CRITERIA = ('requirementPreservation', 'taskSpecificity', 'dependencyFeasibility')


def plan_target(export):
    plans = [item for item in (export.get('trace') or {}).get('artifacts', [])
             if item.get('artifactType') == 'REQUIREMENT']
    if len(plans) != 1 or not plans[0].get('artifactId') or not (plans[0].get('payload') or {}).get('implementationPlan'):
        raise ValueError('PLAN_REVIEW_TARGET_NOT_UNIQUE')
    encoded = json.dumps(plans[0], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    return {'runId': export['bundle']['run']['runId'], 'artifactId': plans[0]['artifactId'],
            'planSha256': hashlib.sha256(encoded).hexdigest()}


def review_template(export):
    return {'schemaVersion': 1, **plan_target(export), 'reviewer': None, 'reviewedAt': None,
            'criteria': {key: {'status': 'UNVERIFIED', 'evidence': ''} for key in CRITERIA}}


def assess_review(export, review):
    if not isinstance(review, dict) or type(review.get('schemaVersion')) is not int or review['schemaVersion'] != 1:
        raise ValueError('PLANNER_REVIEW_INVALID')
    target = plan_target(export)
    if any(review.get(key) != value for key, value in target.items()):
        raise ValueError('PLANNER_REVIEW_TARGET_MISMATCH')
    if not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip():
        raise ValueError('PLANNER_REVIEWER_REQUIRED')
    try:
        moment = datetime.fromisoformat(review['reviewedAt'].replace('Z', '+00:00'))
        if moment.utcoffset() is None:
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ValueError('PLANNER_REVIEW_TIME_INVALID') from None
    criteria = review.get('criteria')
    if not isinstance(criteria, dict) or set(criteria) != set(CRITERIA):
        raise ValueError('PLANNER_REVIEW_CRITERIA_REQUIRED')
    for item in criteria.values():
        if (not isinstance(item, dict) or item.get('status') not in ('PASS', 'FAIL', 'UNVERIFIED')
                or not isinstance(item.get('evidence'), str) or not item['evidence'].strip()):
            raise ValueError('PLANNER_REVIEW_EVIDENCE_REQUIRED')
    states = [item['status'] for item in criteria.values()]
    return {'status': 'FAIL' if 'FAIL' in states else 'UNVERIFIED' if 'UNVERIFIED' in states else 'PASS',
            'value': '독립 사람 검토', 'detail': review}


def main(argv=None):
    parser = argparse.ArgumentParser(description='계획 검토 양식 생성(검토 결과를 자동으로 만들지 않음)')
    parser.add_argument('--export', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args(argv)
    from .workflow import load_handoff
    export, _run = load_handoff(args.export)
    template = review_template(export)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(template, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
