"""Guarded natural-language A2A diagnostics: at most 20 answers + 20 critiques."""

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from agents.llm.contracts import LLMRuntimeError
from diagnostics.communication.contracts import ProbeRecord, fingerprint, now
from diagnostics.communication.guarded_contracts import content_screen, output_contract, role_prompt
from diagnostics.communication.lab import ROLES, running_lab
from diagnostics.communication.live import root_api_key
from diagnostics.communication.natural import (
    ASSETS, CASE_IDS as ORIGINAL_CASE_IDS, NaturalLab, NaturalRecord, NaturalRuntime, transport_passed,
)
from diagnostics.communication.probe import ProbeExecutor
from diagnostics.communication.provenance import capture_provenance, finish_provenance
from diagnostics.communication.semantic_review import (
    REVIEW_PROMPT, SemanticRuntime, bind_review, review_contract, review_matches,
)


CASE_IDS = (*ORIGINAL_CASE_IDS, '04-office-page')


def load_case(case_id):
    if case_id not in CASE_IDS:
        raise ValueError('UNKNOWN_GUARDED_CASE')
    return ASSETS.joinpath(case_id + '.txt').read_text(encoding='utf-8').strip()


class GuardedRecord(NaturalRecord):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.semantic_review = None

    def content_checks(self, response, payload):
        return content_screen(response, payload, self.role)

    def view(self):
        result = super().view()
        review = deepcopy(self.semantic_review)
        result['semanticReview'] = review
        result['handoff']['authorization'] = 'DIAGNOSTIC_ONLY_NOT_PRODUCT_APPROVAL'
        if not result['handoff']['eligible']:
            return result
        answer = self.response_received['answer']
        binding_valid = review_matches(review, candidate=answer, request_text=self.payload['requestText'],
            role=self.role, probe_id=self.probe_id)
        review_model = (review or {}).get('model') or {}
        if not (binding_valid and review_model.get('status') == 'COMPLETED'
                and review_model.get('httpStatus') == 200 and review_model.get('apiInputMatches') is True):
            result['handoff'] = {**result['handoff'], 'eligible': False,
                'reason': 'SEMANTIC_REVIEW_REJECTED' if review and review.get('status') == 'REJECTED'
                else 'SEMANTIC_REVIEW_UNVERIFIED'}
        elif answer['decision'] != 'READY':
            result['handoff'] = {**result['handoff'], 'eligible': False,
                'reason': 'INPUT_REQUIRED' if answer['decision'] == 'INPUT_REQUIRED' else 'MODEL_REJECTED'}
        else:
            result['handoff']['reason'] = 'DIAGNOSTIC_CONTRACT_AND_MODEL_REVIEW_ADMITTED'
        return result


class GuardedRuntime(NaturalRuntime):
    def __post_init__(self):
        super().__post_init__()
        self.semantic_runtime = SemanticRuntime(self.api_key, self.model_id,
            temperature=self.temperature, timeout_seconds=self.timeout_seconds,
            max_output_tokens=8192, max_model_calls=self.max_model_calls,
            transport_factory=self.transport_factory)

    def admit(self, options):
        if self.semantic_runtime.fatal_error:
            self.fatal_error = self.semantic_runtime.fatal_error
        return super().admit(options)

    def output_contract(self, payload, role):
        return output_contract(role)

    def role_prompt(self, role):
        return role_prompt(role)

    async def review(self, payload, row, answer):
        # A deterministic failure does not need another paid interpretation.
        if content_screen({'answer': answer}, payload, row.role)['status'] != 'PASS':
            row.semantic_review = {'status': 'UNVERIFIED', 'reason': 'STRUCTURAL_CONTRACT_REJECTED',
                'isProductApproval': False, 'elapsedMs': 0, 'model': None}
            return
        started = now()
        review_row = ProbeRecord(row.role, row.round, row.position)
        review_payload = {'requestText': payload['requestText'], 'candidate': deepcopy(answer)}
        raw = None
        error_code = None
        try:
            if self.fatal_error or self.semantic_runtime.fatal_error:
                raise ValueError('MODEL_CALLS_STOPPED')
            raw = await self.semantic_runtime.complete(review_payload, review_row)
        except asyncio.CancelledError:
            error_code = 'REVIEW_CANCELED'
            raise
        except LLMRuntimeError as error:
            error_code = error.code.value
        except Exception:
            error_code = 'REVIEW_UNAVAILABLE'
        finally:
            review = bind_review(raw, candidate=answer, request_text=payload['requestText'],
                role=row.role, probe_id=row.probe_id)
            review['elapsedMs'] = round((now() - started) / 1_000_000, 3)
            review['model'] = deepcopy(review_row.model_observation)
            review['errorCode'] = error_code
            row.semantic_review = review
            if self.semantic_runtime.fatal_error:
                self.fatal_error = self.semantic_runtime.fatal_error
            if review_row.model_observation is not None:
                review_row.model_observation['purpose'] = 'SEMANTIC_REVIEW'
                review_row.model_observation['candidateProbeId'] = row.probe_id
                # Snapshot again after the safe provenance labels are added.
                review['model'] = deepcopy(review_row.model_observation)
            self.evidence['semanticReviews'].append(deepcopy(review))
            self.checkpoint()


class GuardedExecutor(ProbeExecutor):
    def __init__(self, records, runtime):
        super().__init__(records)
        self.runtime = runtime

    async def make_result(self, payload, row):
        answer = await self.runtime.complete(payload, row)
        if row.model_observation is not None:
            row.model_observation['purpose'] = 'ROLE_RESPONSE'
        await self.runtime.review(payload, row, answer)
        return {'probeId': row.probe_id, 'answer': answer, 'runtime': 'OPENAI_GUARDED_NATURAL_V3'}


class GuardedLab(NaturalLab):
    record_type = GuardedRecord

    def blocked_reason(self, rows):
        original = super().blocked_reason(rows)
        if original != 'PREVIOUS_RESULT_UNAVAILABLE':
            return original
        for row in rows:
            reason = row.view()['handoff']['reason']
            if reason in {'SEMANTIC_REVIEW_REJECTED', 'SEMANTIC_REVIEW_UNVERIFIED', 'INPUT_REQUIRED', 'MODEL_REJECTED'}:
                return 'UPSTREAM_' + reason
        return original


def summarize_outcome(evidence):
    rows = [row for group in evidence['runs'] for row in group['rows']]
    completed = [row for row in rows if row['phase'] == 'COMPLETED']
    skipped = [row for row in rows if row['phase'] == 'SKIPPED']
    failures = [row for row in rows if row['phase'] not in {'COMPLETED', 'SKIPPED'}]
    review_states = [(row.get('semanticReview') or {}).get('status', 'NOT_REVIEWED') for row in completed]
    return {'plannedRoleResponses': len(evidence['caseRequests']) * 5, 'plannedModelCallCap': evidence['maxModelCalls'],
        'roleCalls': len(evidence.get('roleCalls', [])), 'reviewCalls': len(evidence.get('reviewCalls', [])),
        'completedRows': len(completed), 'skippedRows': len(skipped), 'failedRows': len(failures),
        'transportMatchedRows': sum(transport_passed(row) for row in completed),
        'literalContractPassed': sum(row['checks']['sourceGrounding']['status'] == 'PASS' for row in completed),
        'literalContractRejected': sum(row['checks']['sourceGrounding']['status'] == 'FAIL' for row in completed),
        'modelReviewSupported': review_states.count('SUPPORTED'), 'modelReviewRejected': review_states.count('REJECTED'),
        'modelReviewUnverified': sum(state not in {'SUPPORTED', 'REJECTED'} for state in review_states),
        'handoffEligibleRows': sum(row['handoff']['eligible'] for row in rows),
        'inputRequiredRows': sum(row['handoff']['reason'] == 'INPUT_REQUIRED' for row in rows),
        'isProductSuccess': False}


async def run(args, runtime=None):
    selected = tuple(getattr(args, 'case_ids', None) or CASE_IDS)
    if not selected or len(set(selected)) != len(selected) or any(case not in CASE_IDS for case in selected):
        raise ValueError('INVALID_GUARDED_CASE_SELECTION')
    cases = {case: load_case(case) for case in selected}
    planned = len(cases) * 5
    prior_runs = []
    prior_paths = tuple(getattr(args, 'prior_runs', None) or ())
    if len({path.resolve() for path in prior_paths}) != len(prior_paths):
        raise ValueError('DUPLICATE_PRIOR_EVIDENCE')
    for path in prior_paths:
        prior_raw = path.read_bytes()
        prior_runs.append({'path': str(path), 'sha256': hashlib.sha256(prior_raw).hexdigest(),
            'callCount': len(json.loads(prior_raw)['calls'])})
    prior_count = sum(item['callCount'] for item in prior_runs)
    if prior_count + planned * 2 > 40:
        raise ValueError('COMBINED_DIAGNOSTIC_CALL_CAP_EXCEEDED')
    runtime = runtime or GuardedRuntime(root_api_key(), args.model, max_model_calls=planned,
        max_output_tokens=8192, timeout_seconds=90)
    if (runtime.model_id != args.model or runtime.max_model_calls != planned
            or runtime.semantic_runtime.max_model_calls != planned
            or runtime.semantic_runtime.model_id != args.model
            or runtime.semantic_runtime.temperature != runtime.temperature):
        raise ValueError('GUARDED_RUNTIME_CONFIGURATION_MISMATCH')
    if runtime.calls or runtime.semantic_runtime.calls:
        raise ValueError('GUARDED_RUNTIME_ALREADY_USED')
    evidence = {'version': 'guarded-natural-v3-semantic-v3', 'startedAt': datetime.now(timezone.utc).isoformat(),
        'modelId': args.model, 'maxModelCalls': planned * 2, 'maxRoleCalls': planned, 'maxReviewCalls': planned,
        'priorRuns': prior_runs, 'priorCallCount': prior_count, 'maxCombinedCalls': 40,
        'automaticRetries': False, 'callListOrder': 'ROLE_THEN_REVIEW_NOT_CHRONOLOGICAL',
        'handoffPolicy': 'LITERAL_BINDINGS_AND_BOUND_MODEL_REVIEW_AND_READY;NOT_PRODUCT_APPROVAL',
        'maxOutputTokens': runtime.max_output_tokens,
        'reviewMaxOutputTokens': runtime.semantic_runtime.max_output_tokens,
        'temperature': runtime.temperature, 'timeoutSeconds': runtime.timeout_seconds,
        'scope': 'Diagnostic handoff only; model critique is not human approval; no product Host/Tools/Docker/code/tests',
        'caseRequests': cases, 'requestHashes': {key: fingerprint(value) for key, value in cases.items()},
        'rolePrompts': {role: role_prompt(role) for role in ROLES}, 'semanticReviewPrompt': REVIEW_PROMPT,
        'schemaSha256ByRole': {role: hashlib.sha256(output_contract(role).schema.schema_json.encode()).hexdigest() for role in ROLES},
        'semanticReviewSchemaSha256': hashlib.sha256(review_contract().schema.schema_json.encode()).hexdigest(),
        'semanticReviewSchemaMode': 'PER_CANDIDATE_FREE_TEXT_POINTERS',
        'attempts': [], 'semanticReviews': [], 'runs': [], 'status': 'RUNNING', 'notStarted': []}
    args.out.mkdir(parents=True, exist_ok=False)

    def checkpoint():
        evidence['roleCalls'] = deepcopy(runtime.calls)
        evidence['reviewCalls'] = deepcopy(runtime.semantic_runtime.calls)
        evidence['calls'] = [*evidence['roleCalls'], *evidence['reviewCalls']]
        evidence['combinedCallCount'] = prior_count + len(evidence['calls'])
        evidence['fatalError'] = runtime.fatal_error or runtime.semantic_runtime.fatal_error
        stage = args.out / '.run.json.tmp'
        stage.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(stage, args.out / 'run.json')

    runtime.evidence, runtime.checkpoint = evidence, checkpoint
    evidence['provenance'] = capture_provenance(runtime)
    evidence['provenance']['caseOrder'] = list(cases)
    evidence['provenance']['semanticReviewTransportMode'] = (
        'DEFAULT_HTTPS_TRANSPORT' if runtime.semantic_runtime.transport_factory is None else 'CUSTOM_TRANSPORT')
    checkpoint()
    try:
        async with running_lab(runtime, lab=GuardedLab(runtime), executor_factory=GuardedExecutor) as lab:
            for case_id, request in cases.items():
                for architecture in ('MULTI_AGENT', 'SINGLE_AGENT'):
                    if runtime.fatal_error or runtime.semantic_runtime.fatal_error:
                        evidence['notStarted'].append({'caseId': case_id, 'architecture': architecture, 'reason': 'FATAL_MODEL_ERROR'})
                        continue
                    group = await lab.execute_case(case_id, request, architecture)
                    evidence['runs'].append(group)
                    checkpoint()
        evidence['summary'] = summarize_outcome(evidence)
        evidence['status'] = ('FAIL_OR_INCOMPLETE' if evidence['fatalError'] or evidence['summary']['failedRows']
            or evidence['summary']['transportMatchedRows'] != evidence['summary']['completedRows']
            else 'CAPTURED_WITH_POLICY_BLOCKS' if evidence['summary']['skippedRows']
            or evidence['summary']['handoffEligibleRows'] != evidence['summary']['completedRows']
            else 'CAPTURED_REVIEW_REQUIRED')
    except BaseException:
        evidence['status'] = 'FAIL_OR_INCOMPLETE'
        raise
    finally:
        evidence['finishedAt'] = datetime.now(timezone.utc).isoformat()
        finish_provenance(evidence['provenance'])
        checkpoint()
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', required=True)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--case-id', dest='case_ids', action='append', choices=CASE_IDS)
    parser.add_argument('--prior-run', dest='prior_runs', action='append', type=Path)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
    except Exception as error:
        print(json.dumps({'status': 'STOPPED', 'errorType': type(error).__name__}), flush=True)
        raise SystemExit(1) from None
    print(json.dumps({'status': report['status'], 'summary': report['summary'], 'evidence': str(args.out / 'run.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
