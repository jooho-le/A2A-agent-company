"""READY corrections describe baseline measurements separately from the submitted source diff."""
from agents.llm.corrections import CorrectionAllowance, checked_rejection, feedback, signature


INPUT = 'RESPONSE_BEFORE_SIGNUP_HANDLER'
INVALID = 'INVALID_INPUT_SERVER_ERROR'
MISMATCH = 'READY_PATH_EVIDENCE_MISMATCH'
ROUTE = 'source/main.py::signup'
VALIDATOR = 'source/email.py::normalize'


def claim(path, candidates, changed):
    return {'fieldPath': f'/repairPaths/{path}/sourceRef', 'reasonCode': 'NO_LINKED_CODE_CHANGE',
            'candidateRefs': candidates, 'changedRefs': changed}


def correction(rows, brief):
    checked = checked_rejection({'code': MISMATCH, 'issues': rows})
    return feedback(MISMATCH, issues=checked['issues'], remaining=1, repair_brief=brief)['hostCorrection']


def test_mixed_ready_issues_have_distinct_actions_and_baseline_evidence():
    brief = {
        'invalidInputServerError': {
            'failedCases': [{'httpStatus': 500}, {'httpStatus': 500}],
            'sourceSites': [{'sourceRef': VALIDATOR, 'line': 38, 'statementExcerpt': 'PRIVATE'}]},
        'targets': [{'checks': [{'observedFailurePaths': {INPUT: ['measured-pre-entry']},
                                'caseFeedback': {'measured-pre-entry': {'handlerEntry': 'NOT_ENTERED'}}}]}]}
    answer = correction([claim(INVALID, [VALIDATOR], [ROUTE, VALIDATOR]), claim(INPUT, [], [ROUTE, VALIDATOR])], brief)
    citation, source = answer['issues']
    assert citation['action'] == 'CORRECT_CITATION'
    assert citation['candidateRefs'] == [VALIDATOR]
    assert citation['observedOnBaseline'] == {'statusEvidence': [{'recordedStatus': 500, 'statusOrigin': 'UNKNOWN'}], 'relatedExceptionSites': [
        {'sourceRef': VALIDATOR, 'line': 38}]}
    assert source['action'] == 'EDIT_SOURCE_OR_REPORT_OPEN'
    assert source['candidateRefs'] == [] and source['changedRefs'] == [ROUTE, VALIDATOR]
    assert source['observedOnBaseline'] == {'handlerEntry': 'NOT_ENTERED'}
    assert 'did not reach the signup handler' in answer['instruction']
    assert 'related requests' in answer['instruction']
    assert 'PRIVATE' not in str(answer)


def test_unobserved_boundary_cannot_be_inferred_from_route_name_or_unrelated_case():
    check = {'observedFailurePaths': {INPUT: ['unknown']},
             'caseFeedback': {'different': {'handlerEntry': 'NOT_ENTERED'}}}
    answer = correction([claim(INPUT, [], [ROUTE])], {'targets': [{'checks': [check]}]})
    assert 'observedOnBaseline' not in answer['issues'][0]
    assert 'did not reach the signup handler' not in answer['instruction']


def test_generated_exposed_rejection_channel_can_supply_entry_evidence_without_protected_cases():
    check = {'observedFailurePaths': {INPUT: ['REJECTION_RESPONSE']},
             'outputChecks': {'REJECTION_RESPONSE': 'EXPOSED'}}
    answer = correction([claim(INPUT, [], [])], {'targets': [{'checks': [check]}]})
    assert answer['issues'][0]['observedOnBaseline'] == {'handlerEntry': 'NOT_ENTERED'}


def test_feedback_limits_coordinates_and_rewording_does_not_buy_another_retry():
    private = 'source/../private.py::foo'
    first = correction([claim(INPUT, [], [private, 'source/main.py::invalid\n', ROUTE])], {})
    second = correction([claim(INPUT, [VALIDATOR], ['source/elsewhere.py::@module'])], {})
    assert first['issues'][0]['changedRefs'] == [ROUTE]
    assert 'private.py' not in str(first)
    budget = CorrectionAllowance(2)
    assert budget.take(signature(MISMATCH, None, None, first['issues']))
    assert not budget.take(signature(MISMATCH, None, None, second['issues']))
    assert budget.last_reason == 'REPEATED'


def test_status_origin_is_per_case_and_related_escape_is_not_assigned_to_qa_cases():
    brief = {'invalidInputServerError': {'failedCases': [
        {'caseId': 'invalid_email_1', 'httpStatus': 500, 'statusOrigin': 'HTTP_RESPONSE'},
        {'caseId': 'invalid_email_2', 'httpStatus': 500, 'statusOrigin': 'ESCAPED_EXCEPTION'},
        {'caseId': 'invalid_email_3', 'httpStatus': 500}],
        'relatedExceptions': [{'caseId': 'invalid_email', 'responseStarted': True, 'escapedToClient': True,
                               'privateExtra': 'PRIVATE'}]}}
    result = correction([claim(INVALID, [], [ROUTE])], brief)['issues'][0]
    obs = result['observedOnBaseline']
    assert [(s['caseId'],s['statusOrigin']) for s in obs['statusEvidence']] == [
        ('invalid_email_1','HTTP_RESPONSE'), ('invalid_email_2','ESCAPED_EXCEPTION'), ('invalid_email_3','UNKNOWN')]
    assert obs['relatedEscapedExceptions'] == [{'caseId': 'invalid_email', 'responseStarted': True,
                                               'escapedToClient': True}]
    assert 'PRIVATE' not in str(result)


def test_comparison_facts_and_persisted_values_are_host_bound():
    from agents.llm.corrections import ready_path_trace
    from agents.runtime.ready_path_diff import ReadySourceComparison, changed_path_refs, claim_issues
    from test_invalid_input_ready_paths import BASE, ENTRY, RELATED, ROUTE as ENDPOINT, ROUTE_REF, SITES
    generic = 'source/main.py::handle_errors'
    after = BASE + b"\n@app.exception_handler(Exception)\ndef handle_errors(request, exc):\n    return make_response()\n"
    comparison, changed = ReadySourceComparison(), set()
    links = changed_path_refs({'main.py':BASE}, {'main.py':after}, (INPUT,INVALID), ENDPOINT, ENTRY,
                              invalid_input_sites=SITES, changed_refs=changed, comparison=comparison)
    rows = {INPUT: {'sourceRef': generic, 'edit': 'private prose'},
            INVALID: {'sourceRef': 'source/main.py::unverified_PRIVATE', 'edit': 'private prose'}}
    issues = claim_issues(rows, links, changed, comparison)
    corrected = correction(issues, {})
    records = {r.path:r.model_dump(mode='json') for r in ready_path_trace(corrected)}
    assert 'private prose' not in str(corrected) and 'PRIVATE' not in str(records)
    assert records[INPUT]['attemptedSourceRef'] == generic
    assert records[INPUT]['comparisonReason'] == 'NO_LINK_TO_PRE_ENTRY_RESPONSE'
    assert records[INVALID]['attemptedRefStatus'] == 'NOT_VERIFIED_IN_SOURCE'
    assert records[INVALID]['unchangedRelatedFunctions'] == [RELATED]
    assert records[INVALID]['unchangedRouteFunctions'] == [ROUTE_REF]
    assert all(r['changedExceptionDecorators'] == [generic] for r in records.values())
    assert all(r['candidateRefs'] == [] and r['changedRefs'] == [generic] for r in records.values())
    # The same source comparison cannot describe an edited call order as unchanged.
    reordered = after.replace(b'    canonical = canonicalize_email(data.email)',
                              b'    canonical = canonicalize_email(data.email.strip())')
    cmp2 = ReadySourceComparison()
    changed_path_refs({'main.py':BASE}, {'main.py':reordered}, (INPUT,INVALID), ENDPOINT, ENTRY,
                      invalid_input_sites=SITES, comparison=cmp2)
    assert ROUTE_REF not in cmp2.unchanged_route_functions
