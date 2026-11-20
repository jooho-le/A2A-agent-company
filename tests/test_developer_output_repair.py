"""Host source admission, model claims and practice-only rechecks. No provider calls."""
from copy import deepcopy
from pathlib import Path

import pytest

from agents.core.identity import SinglePhase
from agents.llm.contracts import SourceEditStatus
from agents.roles.developer_contract import build_developer_output_contract, validate_developer_decision
from agents.runtime.developer_workspace import _repair_edit_status
from agents.single.contracts import build_single_draft_contract
from orchestrator.domain.scenario_registry import get_scenario, SCN_001_ID

HANDLER = b"""import re
from fastapi.responses import JSONResponse

def errors(request, exc):
    msg = str(exc)
    msg = re.sub("first", "masked", msg, flags=re.IGNORECASE)
    return JSONResponse(status_code=500, content={"detail": "failure"})
"""
PATHS = ('EMITTED_APPLICATION_LOG_OR_TRACEBACK', 'RESPONSE_BEFORE_SIGNUP_HANDLER')


def inventory(text):
    return {'app.py': (text, 0o644)}


def test_unused_rewrite_and_ordinary_comments_do_not_qualify_for_ready():
    before = inventory(HANDLER)
    after = inventory(b'# diagnostic note\n' + HANDLER.replace(b'"first"', b'"second"'))
    assert _repair_edit_status(before, after) is SourceEditStatus.UNUSED_TEXT_REWRITE
    assert _repair_edit_status(before, after, check_unused_rewrite=False) is SourceEditStatus.CHANGED
    assert _repair_edit_status(before, {**after, 'routes.py': (b'return_value = 1\n', 0o644)}) is SourceEditStatus.CHANGED


@pytest.mark.parametrize(('old', 'new'), [
    (b'"detail": "failure"', b'"detail": msg'),  # used by the actual response
    (b'    return JSONResponse', b'    print(msg)\n    return JSONResponse'),
    (b'    return JSONResponse', b'    report(locals())\n    return JSONResponse'),
    (b're.sub(', b'custom_sub('),
    (b'"masked"', b'make_replacement()'),
    (b'import re', b'import re\nre.sub = custom_sub'),
])
def test_uncertain_or_used_edits_reach_build_and_rechecks(old, new):
    source = HANDLER.replace(old, new)
    assert _repair_edit_status(inventory(source), inventory(source.replace(b'first', b'second'))) is SourceEditStatus.CHANGED


@pytest.mark.parametrize('invalid', [b'"["', br'r"\g<missing>"', br'r"\8"'])
def test_repairing_an_invalid_regex_or_template_may_change_behavior(invalid):
    token = b'"first"' if invalid == b'"["' else b'"masked"'
    source = HANDLER.replace(token, invalid)
    assert _repair_edit_status(inventory(source), inventory(HANDLER)) is SourceEditStatus.CHANGED


def test_full_and_partial_ready_account_uses_only_selected_paths_for_both_architectures():
    output = build_developer_output_contract(PATHS)
    single = build_single_draft_contract(SinglePhase.FIXING, scenario=get_scenario(SCN_001_ID), repair_paths=PATHS)
    assert output.schema.to_dict() == single.output.schema.to_dict()
    payload = {'kind':'READY', 'summary':'Submitted for validation.', 'questions':[], 'rejection':None,
               'repairPaths':{PATHS[0]:{'edit':None, 'sourceRef':None, 'recheck':'Inspect the emitted logs.'},
                              PATHS[1]:{'edit':'The response builder now returns a fixed detail.',
                                        'sourceRef':'source/app.py::errors',
                                        'recheck':'Inspect rejected request responses.'}}}
    decision = single.validate(payload)
    assert decision == validate_developer_decision(payload, PATHS)
    assert [row.edit for row in decision.reported_path_edits] == [None, payload['repairPaths'][PATHS[1]]['edit']]
    for key in (PATHS[0], 'UNSELECTED'):
        wrong = deepcopy(payload)
        if key in PATHS:
            del wrong['repairPaths'][key]
        else:
            wrong['repairPaths'][key] = payload['repairPaths'][PATHS[0]]
        with pytest.raises(ValueError):
            validate_developer_decision(wrong, PATHS)
    for wrong in (None, {**payload['repairPaths'], PATHS[0]:{'edit':' ', 'recheck':'still pending'}}):
        with pytest.raises(ValueError):
            validate_developer_decision({**payload,'repairPaths':wrong}, PATHS)
    question = {**payload,'kind':'INPUT_REQUIRED','questions':['Which requirement?'],'summary':'','repairPaths':None}
    assert single.validate(question).kind == 'INPUT_REQUIRED'
    with pytest.raises(ValueError):
        single.validate({**question,'repairPaths':payload['repairPaths']})
    assert 'repairPaths' not in build_developer_output_contract().schema.to_dict()['properties']


def test_four_output_repair_cases_with_practice_measurements(tmp_path):
    from collections import Counter
    from evaluation import check_details, final_evaluation, protected_tests, replay_runner
    from orchestrator.domain.repair_progress import compare_check
    from tests.evaluation import signup_service
    from tests.evaluation.test_protected_signup import RUNTIME
    if not RUNTIME:
        pytest.skip('requires the signup fixture runtime')

    suite = protected_tests.load_suite()
    key = 'SEC-SIGNUP-005'
    case = next(c for c in suite['checks'] if c['key'] == key)
    test_id = protected_tests.unit_test_id(case, 'practice', suite)
    inputs = {k:v.encode() for k,v in protected_tests.protected_files('in-loop-security', suite).items()}
    inputs['tests/signup/only_output.py'] = (
        f'def load_tests(loader, cases, pattern):\n    return loader.loadTestsFromName({test_id!r})\n').encode()
    inputs.update({final_evaluation.RUNNER_FILE:final_evaluation._runner_source(),
                   final_evaluation.REPLAY_FILE:Path(replay_runner.__file__).read_bytes(),
                   '_input_replay.json':b'{}'})
    final_evaluation._write_tree(tmp_path / 'inputs', inputs)
    plan = check_details.expected_plans('practice', suite)[key]
    # All product code here comes from the repository's hand-written synthetic fixtures.
    original = signup_service.build('good')
    prior = None
    # The synthetic schema also rejects a short password before the route runs.
    for label, input_fix, store_fix, expected in (
            ('original', False, False, (22, 6, 3, None)),
            ('discarded', False, False, (22, 6, 3, 'NO_PROGRESS')),
            ('input', True, False, (26, 2, 3, 'PARTIAL_IMPROVEMENT')),
            ('storage', False, True, (27, 4, 0, 'PARTIAL_IMPROVEMENT')),
            ('both', True, True, (31, 0, 0, 'RESOLVED'))):
        files = dict(original)
        if not input_fix:
            files['main.py'] = signup_service.build('validation_input_echoed')['main.py']
        if not store_fix:
            files['routers/auth.py'] = signup_service.build('db_error_unhandled')['routers/auth.py']
        handler = HANDLER.replace(b'"first"', b'"second"') if label == 'discarded' else HANDLER
        files['main.py'] += '\n' + handler.decode()
        final_evaluation._write_tree(tmp_path/label, {k:v.encode() for k,v in files.items()})
        receipt = final_evaluation.LocalRunner(trusted=True).run(
            tmp_path/label, tmp_path/'inputs', 'tests/signup', 'only_output.py', final_evaluation.REPLAY_FILE)
        assert receipt.error is None
        row, = receipt.report['tests']
        assert row['testId'] == test_id
        details = check_details._validated(receipt.detail_results['checks'][key], plan)
        observations = [{k:d[k] for k in ('caseId','itemId','status','actual','kind','reason','handlerEntry') if k in d}
                        for d in details]
        counts = Counter(d['status'] for d in observations)
        assert len(observations) == 31
        assert (counts['PASS'], counts['FAIL'], counts['UNVERIFIED']) == expected[:3], label
        current = {'codeVersion': 2 if prior is None else 3, 'testVersion':protected_tests.loop_suite_ref(suite),
                   'observationStatus':'COMPLETE', 'observations':observations, 'outcome':row['outcome']}
        if prior is None:
            prior = current
        else:
            comparison = compare_check(prior, current, {'beforeVersion':2, 'afterVersion':3,
                                                        'beforeSnapshot':'original', 'afterSnapshot':label})
            assert comparison['status'] == expected[3], label
            assert comparison['counts'].get('NEW_FAILURE', 0) == 0
