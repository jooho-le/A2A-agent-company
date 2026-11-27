from copy import deepcopy
from hashlib import sha256

from agents.runtime.developer_input import compact_fix_input
from agents.runtime.normal_request_repair import (NORMAL_REQUEST, changed_frame_refs, linkable_sites,
                                                     measured_prerequisite, priority_repair_evidence)


def observation(case, item, status, actual, *, kind='assertion', reason=None, **extra):
    return dict(caseId=case, itemId=item, status=status, actual=actual, kind=kind, reason=reason, **extra)


def feedback(name, observations):
    return dict(role='QA', requirementId='req', reportArtifactId='qa', testId=name, outcome='FAIL',
                observationStatus='COMPLETE', observations=observations)


def projected(rows, source=None):
    issues = [dict(issueId=row['testId'], category='QA_ASSERTION_FAILURE', referenceId=row['testId'],
                   requirementIds=['req'], reportArtifactId='qa') for row in rows]
    value = {'fixRequest': {'issues': issues, 'validationFeedback': rows}}
    return compact_fix_input(value, source_files=(lambda: source) if source else None)


def test_signup_creation_failure_ranks_first_without_a_dependency_or_stack_frame():
    secondary = feedback('secondary', [observation('malformed', 'password_exposed', 'FAIL', True)])
    normal = feedback('normal', [observation('valid_email_1', 'http_status', 'PASS', 201),
                                 observation('valid_email_1', 'user_delta', 'FAIL', 0),
                                 observation('valid_email_1', 'account_count', 'FAIL', 0)])
    source = [secondary, normal]
    unchanged = deepcopy(source)
    result = projected(source)
    brief = result['activeRepairFocus']['brief']
    assert brief['blockingPrerequisite']['httpStatuses'] == [201]
    facts, = brief['blockingPrerequisite']['failedCreationCases']
    assert (facts['user_delta'], facts['account_count']) == (0, 0)
    assert brief['blockingPrerequisite']['blockedCheckCount'] == 0
    assert brief['blockingPrerequisite']['sourceSites'] == []
    assert 'priorityRepairPaths' not in brief
    assert brief['targets'][0]['checks'][0]['testId'] == 'normal'
    assert [r['testId'] for r in result['fixRequest']['validationFeedback']] == ['normal','secondary']
    assert source == unchanged


def test_explicit_normal_rejection_is_prioritized_when_database_observation_is_missing():
    normal = feedback('normal', [observation('valid_email_2', 'http_status', 'FAIL', 422),
                                 observation('valid_email_2', 'user_delta', 'UNVERIFIED', None)])
    assert projected([normal])['activeRepairFocus']['brief']['blockingPrerequisite']['httpStatuses'] == [422]


def test_escaped_exception_binds_the_active_source_frame_and_counts_blocked_checks_once():
    source = b'def create():\n    digest = hash_input()\n    store(digest)\n    return 1\n'
    frame = {'line': 2, 'sourceSha256': sha256(source).hexdigest()}
    diagnostic = {'appFrame':frame, 'exceptionType':'builtins.TypeError', 'escapedToClient':True}
    obs = [observation('success','response_status','UNVERIFIED',None,kind='precondition',
                       reason='UNHANDLED_APP_EXCEPTION',diagnostic=diagnostic),
           observation('precondition','http_status','FAIL',500,kind='precondition'),
           observation('dependent','check_a','NOT_RUN',None,reason='PRECONDITION_OR_OBSERVATION_UNAVAILABLE'),
           observation('dependent','check_b','NOT_RUN',None,reason='PRECONDITION_OR_OBSERVATION_UNAVAILABLE')]
    r = projected([feedback('blocked',obs)], {'service.py':source})
    brief = r['activeRepairFocus']['brief']
    assert brief['priorityRepairPaths'] == [NORMAL_REQUEST]
    assert NORMAL_REQUEST in brief['readyReportPaths']
    blocker = brief['blockingPrerequisite']
    assert blocker['exceptionTypes'] == ['builtins.TypeError'] and blocker['blockedCheckCount'] == 1
    assert blocker['sourceSites'] == [{**frame, 'toolPath':'source/service.py', 'sourceRef':'source/service.py::create',
                                       'statementExcerpt':'<target> = <call>()'}]
    assert brief['sourceInspection']['firstReadBatch'][0] == 'source/service.py'

    stale = projected([feedback('blocked',obs)], {'service.py':source + b'# different baseline\n'})
    assert not stale['activeRepairFocus']['brief']['blockingPrerequisite']['sourceSites']
    assert 'priorityRepairPaths' not in stale['activeRepairFocus']['brief']


def test_negative_cases_injected_failures_and_excluded_checks_are_not_normal_failures():
    escaped = {'escapedToClient': True, 'exceptionType':'builtins.ValueError'}
    rows = [feedback('other', [
        observation('malformed', 'response_status', 'UNVERIFIED', None, kind='precondition',
                    reason='UNHANDLED_APP_EXCEPTION', diagnostic=escaped),
        observation('db_error','response_status','UNVERIFIED',None,kind='precondition',
                    reason='UNHANDLED_APP_EXCEPTION',diagnostic=escaped),
        observation('invalid_email_1','http_status','PASS',422),
        observation('invalid_email_1','user_delta','PASS',0),
        observation('duplicate_email','http_status','FAIL',500),
        observation('duplicate_email','account_count','FAIL',0),
        observation('malformed','hash_exposed','FAIL',True),
    ])]
    rows[0]['hostDecision'] = 'FIX_TARGET'
    excluded = feedback('excluded',[observation('valid_email_1','http_status','FAIL',500)])
    excluded['hostDecision'] = 'NOT_A_FIX_TARGET'
    assert measured_prerequisite([*rows, excluded]) == (None, [])


def test_ready_needs_a_change_at_the_measured_statement_not_just_any_edit_in_the_same_function():
    old = {'service.py': b'def create():\n    digest = encode(mode="bad")\n    store(digest)\n    return 1\n'}
    src = old['service.py']
    sites = linkable_sites([dict(line=2,sourceSha256=sha256(src).hexdigest())],old)
    for new in (src, b'# moved\n'+src, src.replace(b'store(digest)',b'save(digest)'),src.replace(b'"bad"', b"'bad'")):
        assert not changed_frame_refs(old, {'service.py':new}, sites)
    assert changed_frame_refs(old, {'service.py':src.replace(b'mode="bad"', b'mode=MODE')}, sites) == {'source/service.py::create'}


def test_correction_repeats_only_observed_facts_and_masked_source_shape():
    code = b'''def signup():
    private_id = private_function(type="NeverCopyValue", date=20261120, value=f"{private_key}")  # private comment
'''
    frame = {'line':2, 'sourceSha256':sha256(code).hexdigest()}
    cause = {'code':'ARGUMENT_TYPE_MISMATCH','actualType':'str'}
    old = {'appFrame':frame,'exceptionType':'builtins.TypeError','escapedToClient':True}
    def evidence(diagnostic, source=code):
        row = feedback('normal', [
            observation('valid_email_1','http_status','FAIL',500),
            observation('valid_email_1','account_count','FAIL',0),
            observation('success','response_status','UNVERIFIED',None,kind='precondition',
                        reason='UNHANDLED_APP_EXCEPTION',diagnostic=diagnostic)])
        return priority_repair_evidence(projected([row], {'main.py':source}))

    result = evidence({**old,'observedCause':cause})
    site, = result['sourceSites']
    assert site['observedCause'] == cause
    assert site['statementExcerpt'] == '<target> = <call>(type=<str>, <keyword>=<int>, <keyword>=<f-string>)'
    assert result['httpStatuses']==[500] and result['observedCreation']=={'httpStatus':500,'statusOrigin':'UNKNOWN','account_count':0}
    assert not any(v in repr(result) for v in ('NeverCopyValue', 'private_', '20261120', 'date=', 'value='))
    assert 'observedCause' not in evidence(old)['sourceSites'][0]
    assert evidence({**old,'observedCause':cause}, source=code+b'# changed') is None


def test_excerpt_keeps_confirmed_library_export_only():
    # Trusted installed package has an explicit public export. No source code is imported.
    def excerpt(code, other=None):
        src = code.encode()
        return linkable_sites([{'sourceSha256':sha256(src).hexdigest(), 'line':len(src.splitlines())}],
                              {'service.py':src, **(other or {})})[0]['statementExcerpt']
    head = 'from argon2 import PasswordHasher as SomeLocalAlias\n'
    statement = '    worker = SomeLocalAlias(type="opaque")\n'
    assert excerpt(head+'def run():\n'+statement) == '<target> = PasswordHasher(type=<str>)'
    assert excerpt('import argon2 as alias\ndef run():\n    return alias.PasswordHasher()') == 'return PasswordHasher()'
    assert excerpt(head+'def run(SomeLocalAlias):\n'+statement) == '<target> = <call>(type=<str>)'
    assert excerpt(head+'def run():\n'+statement, {'argon2/__init__.py':b'forged=1'}) == '<target> = <call>(type=<str>)'
    assert excerpt('from argon2 import PrivateValue as SomeLocalAlias\ndef run():\n'+statement) == '<target> = <call>(type=<str>)'
