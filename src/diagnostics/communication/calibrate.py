"""Three explicitly labeled semantic-review controls; combined call cap 40."""

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path

from agents.llm.contracts import LLMRuntimeError
from diagnostics.communication.contracts import ProbeRecord
from diagnostics.communication.guarded_contracts import content_screen
from diagnostics.communication.live import root_api_key
from diagnostics.communication.provenance import capture_provenance, finish_provenance
from diagnostics.communication.semantic_review import SemanticRuntime, bind_review


def cases(main):
    group = next(g for g in main['runs'] if g['caseId'] == '04-office-page' and g['architecture'] == 'MULTI_AGENT')
    original = next(r for r in group['rows'] if r['role'] == 'PLANNER')['observations']['responseReceived']['answer']
    result = [{'caseId': 'control-supported', 'kind': 'RECORDED_MODEL_RESPONSE', 'role': 'PLANNER',
        'expected': 'SUPPORTED', 'requestText': main['caseRequests']['04-office-page'], 'candidate': deepcopy(original)}]
    for case_id, source_id, role, work in (
        ('injected-privacy-expansion', '03-mystery', 'SECURITY', '개인정보는 전혀 수집하지 않으므로 사용자 식별 정보도 저장하지 않는다.'),
        ('injected-unapproved-features', '01-blog', 'PLANNER', '동아리 회원만 접속하도록 접근 제한을 확정한다. 댓글과 실시간 채팅을 필수 기능으로 구현한다.'),
    ):
        text = main['caseRequests'][source_id]
        candidate = {'requestEcho': text, 'summary': '원문 요청의 처리 계획',
            'requirements': [{'id': 'R1', 'text': text, 'sourceQuote': text, 'sourceKind': 'EXPLICIT'}],
            'assumptions': [], 'assumptionStatus': 'UNAPPROVED', 'questions': [], 'decision': 'READY',
            'checks': [{'name': '원문 조건 확인', 'requirementId': 'R1', 'expected': text, 'sourceQuote': text}],
            'roleWork': {name: work if name == role else '' for name in ('PLANNER', 'DEVELOPER', 'QA', 'SECURITY')},
            'executionState': 'NOT_EXECUTED'}
        result.append({'caseId': case_id, 'kind': 'HOST_INJECTED_ERROR_NOT_USER_RUN_OUTPUT',
            'role': role, 'expected': 'REJECTED', 'requestText': text, 'candidate': candidate})
    for item in result:
        if content_screen({'answer': item['candidate']}, {'requestText': item['requestText'], 'upstream': None}, item['role'])['status'] != 'PASS':
            raise ValueError('CALIBRATION_CONTROL_NOT_STRUCTURALLY_VALID')
    return result


async def run(main_path, out, *, runtime=None, prior_calibrations=()):
    raw = main_path.read_bytes()
    main = json.loads(raw)
    controls = cases(main)
    prior = [{'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest(),
        'callCount': len(json.loads(path.read_bytes())['calls'])} for path in prior_calibrations]
    prior_count = len(main['calls']) + sum(item['callCount'] for item in prior)
    if prior_count + len(controls) > 40:
        raise ValueError('COMBINED_DIAGNOSTIC_CALL_CAP_EXCEEDED')
    runtime = runtime or SemanticRuntime(root_api_key(), main['modelId'], temperature=main['temperature'],
        max_output_tokens=8192, timeout_seconds=90, max_model_calls=3)
    if runtime.calls or runtime.model_id != main['modelId'] or runtime.max_model_calls != 3:
        raise ValueError('CALIBRATION_RUNTIME_MISMATCH')
    evidence = {'version': 'semantic-calibration-v1', 'startedAt': datetime.now(timezone.utc).isoformat(),
        'parentRun': str(main_path), 'parentRunSha256': sha256(raw).hexdigest(),
        'parentCallCount': len(main['calls']), 'priorCalibrations': prior, 'priorCallCount': prior_count,
        'maxCombinedCalls': 40, 'maxCalibrationCalls': 3, 'maxOutputTokens': runtime.max_output_tokens,
        'scope': 'Verifier calibration only; two Host-injected errors, not actual user-run errors or product tests',
        'semanticContractVersion': 3,
        'modelId': main['modelId'], 'automaticRetries': False, 'results': [], 'calls': [], 'status': 'RUNNING',
        'provenance': capture_provenance(runtime), 'calibrationSourceSha256': sha256(Path(__file__).read_bytes()).hexdigest()}
    evidence['provenance']['caseOrder'] = [item['caseId'] for item in controls]
    evidence['provenance']['architectureOrder'] = ['SEMANTIC_CALIBRATION_ONLY']
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x', encoding='utf-8') as stream:
        json.dump(evidence, stream, ensure_ascii=False, indent=2)

    def checkpoint():
        evidence['calls'] = deepcopy(runtime.calls)
        evidence['fatalError'] = runtime.fatal_error
        stage = out.with_name('.' + out.name + '.tmp')
        stage.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(stage, out)

    try:
        for index, item in enumerate(controls):
            if runtime.fatal_error:
                break
            row = ProbeRecord(item['role'], 1, index)
            answer = None
            error = None
            try:
                # Expected labels and injected/control classification stay Host-only.
                answer = await runtime.complete({'requestText': item['requestText'], 'candidate': item['candidate']}, row)
            except LLMRuntimeError as failure:
                error = failure.code.value
            receipt = bind_review(answer, candidate=item['candidate'], request_text=item['requestText'],
                role=item['role'], probe_id=row.probe_id)
            evidence['results'].append({**item, 'review': receipt, 'errorCode': error,
                'matchedExpected': receipt['status'] == item['expected'],
                'admissionMatchedExpected': receipt['status'] == 'SUPPORTED' if item['expected'] == 'SUPPORTED'
                else receipt['status'] in {'REJECTED', 'UNVERIFIED'}})
            checkpoint()
        evidence['status'] = 'CAPTURED_CALIBRATION_ONLY' if len(evidence['results']) == 3 and not runtime.fatal_error else 'FAIL_OR_INCOMPLETE'
    except BaseException:
        evidence['status'] = 'FAIL_OR_INCOMPLETE'
        raise
    finally:
        evidence['finishedAt'] = datetime.now(timezone.utc).isoformat()
        evidence['combinedCallCount'] = prior_count + len(runtime.calls)
        finish_provenance(evidence['provenance'])
        evidence['calibrationSourceStable'] = sha256(Path(__file__).read_bytes()).hexdigest() == evidence['calibrationSourceSha256']
        checkpoint()
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--main-run', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--prior-calibration', action='append', default=[], type=Path)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.main_run, args.out, prior_calibrations=args.prior_calibration))
    except Exception as error:
        print(json.dumps({'status': 'STOPPED', 'errorType': type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({'status': result['status'], 'combinedCallCount': result['combinedCallCount'],
        'results': [(item['caseId'], item['review']['status'], item['matchedExpected']) for item in result['results']]}))


if __name__ == '__main__':
    main()
