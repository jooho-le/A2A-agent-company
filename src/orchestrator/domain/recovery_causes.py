"""Host-measured causes; neither model prose nor an exception location proves causality."""
import json
from orchestrator.domain import test_not_run

POLICY = 'BOUND_EXECUTION_AND_PRODUCT_PRECONDITION_V1'


def execution_receipts(item):
    return item.execution_evidence or (() if item.tool_evidence is None else (item.tool_evidence,))


def product_precondition_blocked(item, protected_selectors=frozenset()):
    if item.outcome.value not in {'FAIL', 'UNVERIFIED'} or not execution_receipts(item):
        return False
    try:
        data = json.loads(item.details or '{}')
        rows = data.get('tests', [data])
        unresolved = [r for r in rows if r.get('outcome', 'UNVERIFIED') == 'UNVERIFIED']
        if not unresolved:
            return False
        refs = {e.evidence_ref for e in execution_receipts(item)
                if e.outcome.value == 'PASS' and e.tool_name == 'run_unit_tests'}
        blocked = False
        for row in unresolved:
            # Technical generated-test errors can coexist, but cannot establish
            # a product defect or excuse another unexplained protected skip.
            if row.get('notRunReason') in test_not_run.TECHNICAL:
                if ':'.join(row.get('testId', '').split(':')[:2]) in protected_selectors:
                    return False
                continue
            if not set(row.get('evidenceRefs', ())).intersection(refs):
                return False
            if row.get('observationStatus') != 'COMPLETE':
                return False
            observations = row.get('observations', [])
            failed = [o for o in observations if o.get('kind') == 'precondition'
                      and o.get('itemId') == 'http_status' and o.get('status') == 'FAIL'
                      and type(o.get('actual')) is int and 400 <= o['actual'] <= 599]
            blocked_cases = {o.get('caseId') for o in failed if isinstance(o.get('caseId'), str)}
            pending = [o for o in observations if o.get('status') in {'NOT_RUN', 'UNVERIFIED'}]
            if (not blocked_cases or not pending or any(o.get('status') != 'NOT_RUN'
                    or o.get('caseId') not in blocked_cases for o in pending)):
                return False
            blocked = True
        return blocked
    except (ValueError, TypeError, AttributeError):
        return False
