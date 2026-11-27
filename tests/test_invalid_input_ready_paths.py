from hashlib import sha256

import pytest

from agents.runtime.normal_request_repair import linkable_sites
from agents.runtime.ready_path_diff import INPUT, INVALID_INPUT, changed_path_refs, claim_issues
from agents.runtime.repair_locations import read_locations
from agents.llm.contracts import LLMRuntimeError
from agents.roles.developer_contract import build_developer_output_contract, corrective_ready_output


ENTRY = ('main', 'app')
ROUTE = ('POST', '/signup')
BASE = b"""from fastapi import FastAPI
from email_validator import validate_email, EmailNotValidError
app = FastAPI()
def canonicalize_email(email):
    local, domain = email.rsplit('@', 1)
    return local + '@' + domain
@app.post('/signup')
def signup(data):
    canonical = canonicalize_email(data.email)
    try:
        validate_email(canonical)
    except EmailNotValidError:
        return invalid_response()
    try:
        db.commit()
    except Exception:
        return storage_error()
    return success()
"""
CALL = b'    canonical = canonicalize_email(data.email)'
FIX = BASE.replace(b"    local, domain = email.rsplit('@', 1)", b"    local, _, domain = email.partition('@')")
LEAK_FIX = b"""
from fastapi.exceptions import RequestValidationError
@app.exception_handler(RequestValidationError)
def sanitized_response(request, exc):
    return public_response()
"""
RELATED = 'source/main.py::canonicalize_email'
ROUTE_REF = 'source/main.py::signup'
ERROR_REF = 'source/main.py::sanitized_response'
SITES = linkable_sites([{'sourceSha256': sha256(BASE).hexdigest(), 'line': 5}], {'main.py': BASE})


def links(candidate, sites=SITES, changed_refs=None, original=BASE):
    return changed_path_refs({'main.py': original}, {'main.py': candidate}, (INPUT, INVALID_INPUT), ROUTE, ENTRY,
                             invalid_input_sites=sites, changed_refs=changed_refs)


@pytest.mark.parametrize('candidate,invalid_ref', [
    (FIX, RELATED),
    (BASE.replace(CALL, b'    validate_email(data.email)\n' + CALL), ROUTE_REF),
    (BASE.replace(CALL, b'    try:\n    ' + CALL + b'\n    except ValueError:\n        return invalid_response()'), ROUTE_REF),
    (BASE.replace(CALL, b"    if '@' not in data.email:\n        return invalid_response()\n" + CALL), ROUTE_REF),
    (BASE.replace(b'    local,', b"    if '@' not in email:\n        raise ValueError()\n    local,"), RELATED),
])
def test_observation_bound_fix_links_only_to_the_invalid_request(candidate, invalid_ref):
    evidence = links(candidate)
    assert evidence == {INPUT: set(), INVALID_INPUT: {invalid_ref}}
    rows = {INPUT: {'edit': None, 'sourceRef': None}, INVALID_INPUT: {'edit': 'Changed request handling.', 'sourceRef': invalid_ref}}
    assert not claim_issues(rows, evidence)


def test_changed_locations_do_not_prove_a_repair_for_an_unrelated_path():
    after = BASE.replace(b'except Exception:', b'except Exception as unused:  # Describe a change')
    changed = set()
    evidence = links(after, changed_refs=changed)
    assert evidence == {INPUT: set(), INVALID_INPUT: set()}
    rows = {path: {'edit': 'Claimed.', 'sourceRef': ROUTE_REF} for path in evidence}
    issues = claim_issues(rows, evidence, changed)
    assert len(issues) == 2
    assert all(i['changedRefs'] == [ROUTE_REF] and i['candidateRefs'] == [] for i in issues)
    assert not any(links(BASE.replace(CALL, b'    if data.password:\n        return password_response()\n' + CALL)).values())


def test_partial_repair_open_or_incorrect_citation_is_distinct_for_each_path():
    security_only = links(BASE + LEAK_FIX)
    assert security_only == {INPUT: {ERROR_REF}, INVALID_INPUT: set()}
    rows = {INPUT: {'edit': 'Updated validation response.', 'sourceRef': ROUTE_REF},
            INVALID_INPUT: {'edit': None, 'sourceRef': None}}
    [issue] = claim_issues(rows, security_only)
    assert issue['fieldPath'].endswith(INPUT + '/sourceRef')
    assert issue['candidateRefs'] == [ERROR_REF]
    rows[INPUT]['sourceRef'] = ERROR_REF
    assert not claim_issues(rows, security_only)
    assert links(FIX + LEAK_FIX) == {INPUT: {ERROR_REF}, INVALID_INPUT: {RELATED}}


def test_stale_trace_and_detached_function_do_not_confirm_the_measured_location():
    assert not links(FIX, [{**SITES[0], 'sourceSha256': '0' * 64}])[INVALID_INPUT]
    removed_call = FIX.replace(CALL, b'    canonical = data.email')
    assert RELATED not in links(removed_call)[INVALID_INPUT]


def test_route_is_only_a_boundary_for_the_request_that_never_entered_it():
    locations = read_locations([INPUT, INVALID_INPUT], {'main.py': BASE})
    assert [site['operation'] for site in locations[INPUT]] == ['ROUTE_BOUNDARY_ONLY']
    assert [site['operation'] for site in locations[INVALID_INPUT]] == ['SIGNUP_ROUTE']


def test_host_edit_links_control_each_corrective_wire_claim_independently():
    paths = (INVALID_INPUT, INPUT)
    base = build_developer_output_contract(paths)
    def ready(email, response):
        return {'kind': 'READY', 'summary': 'Submit for remeasurement.', 'questions': [], 'rejection': None,
                'repairPaths': {path: {'sourceRef': ref, 'edit': 'Changed this path.' if ref else None,
                                      'recheck': 'Remeasure.'} for path, ref in zip(paths, (email, response))}}
    partial = corrective_ready_output(base, links(FIX)).schema
    partial.validate(ready(RELATED, None))
    partial.validate(ready(None, None))
    for bad in (ready(RELATED, ERROR_REF), ready(ROUTE_REF, None)):
        with pytest.raises(LLMRuntimeError):
            partial.validate(bad)
    # Reporting a reason without citing a change is not an open path.
    inconsistent = ready(None, None)
    inconsistent['repairPaths'][INVALID_INPUT]['edit'] = 'Claimed.'
    with pytest.raises(LLMRuntimeError):
        partial.validate(inconsistent)
    completed = corrective_ready_output(base, links(FIX + LEAK_FIX)).schema
    completed.validate(ready(RELATED, ERROR_REF))
    response_only = corrective_ready_output(base, links(BASE + LEAK_FIX)).schema
    response_only.validate(ready(None, ERROR_REF))
    with pytest.raises(LLMRuntimeError):
        response_only.validate(ready(RELATED, ERROR_REF))
