"""No provider, application imports or generated-code execution."""
import pytest

from agents.roles.developer_contract import validate_developer_decision
from agents.runtime.ready_path_diff import changed_path_refs, claim_issues, DATABASE, INPUT, LOG
from orchestrator.domain.developer_artifacts import ReportedPathEdit


PATHS = (INPUT, DATABASE[0], LOG)
ENTRY = ('api', 'application')
ROUTE = ('POST', '/api/signup')
GENERIC = b"""from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError
application = FastAPI()
@application.post('/api/signup')
def signup():
    try:
        db.commit()
    except IntegrityError:
        return duplicate()
@application.exception_handler(Exception)
def generic_exception_handler(request, exc):
    return fixed_error()
"""
GENERIC_REF = 'source/api.py::generic_exception_handler'


def links(before, after):
    return changed_path_refs(before, after, PATHS, ROUTE, ENTRY)


def rows(ref):
    return {p: {'edit': 'Changed the handler.', 'sourceRef': ref, 'recheck': 'Check the output.'} for p in PATHS}


def test_archived_generic_pattern_is_only_a_log_change_and_reports_two_precise_mismatches():
    before = {'api.py': GENERIC}
    after = {'api.py': GENERIC.replace(b'    return fixed_error()', b'    logger.addFilter(redactor)\n    return fixed_error()')}
    marked = links(before, after)
    assert marked == {INPUT: set(), DATABASE[0]: set(), LOG: {GENERIC_REF}}
    report = rows(GENERIC_REF)
    issues = claim_issues(report, marked)
    assert [(i['fieldPath'], i['candidateRefs']) for i in issues] == [
        ('/repairPaths/' + p + '/sourceRef', []) for p in PATHS[:2]]
    for p in PATHS[:2]:
        report[p].update(edit=None, sourceRef=None)
    assert claim_issues(report, marked) == []


def test_two_paths_can_cite_one_changed_helper_when_both_return_its_result():
    entry = b"""from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError as InputError
from routes.signup import router
from presentation import detail
application = FastAPI()
application.include_router(router, prefix='/api')
@application.exception_handler(InputError)
def invalid(request, exc):
    value = detail()
    return value
"""
    router = b"""from fastapi import APIRouter
from sqlalchemy.exc import SQLAlchemyError
from presentation import detail
router = APIRouter()
@router.post('/signup')
def submit():
    try:
        db.commit()
    except SQLAlchemyError:
        return detail()
"""
    before = {'api.py': entry, 'routes/signup.py': router, 'presentation.py': b'def detail():\n    return 1\n'}
    after = {**before, 'presentation.py': b'def detail():\n    return 2\n'}
    ref = 'source/presentation.py::detail'
    assert links(before, after) == {INPUT: {ref}, DATABASE[0]: {ref}, LOG: set()}
    report = rows('source/api.py::invalid')
    report[LOG].update(edit=None, sourceRef=None)
    assert [i['candidateRefs'] for i in claim_issues(report, links(before, after))] == [[ref], [ref]]


@pytest.mark.parametrize('extra', [
    b"@other.exception_handler(RequestValidationError)\ndef wrong_app(request, exc):\n    return 1\n",
    b"@application.exception_handler(422)\ndef status_handler(request, exc):\n    return 1\n",
    b"@application.get('/health')\ndef health():\n    try:\n        db.commit()\n    except IntegrityError:\n        return 1\n",
    b"def unused():\n    logger.info('unused')\n",
    b"@application.post('/api/signup')\ndef submit():\n    def unused():\n        logger.info(1)\n    return 1\n",
    b"# changed comment\n",
])
def test_unrelated_or_inactive_edits_have_no_failure_path_link(extra):
    base = GENERIC + b'from fastapi.exceptions import RequestValidationError\n'
    assert not any(links({'api.py': base}, {'api.py': base + extra}).values())


def test_module_registration_and_disconnected_module_with_same_entry_name():
    base = GENERIC + b'from fastapi.exceptions import RequestValidationError as E\ndef convert(req, exc):\n    return 1\n'
    before = {'api.py': base, 'unused.py': base}
    added = base + b'application.add_exception_handler(E, convert)\n'
    assert links(before, {'api.py': added, 'unused.py': added})[INPUT] == {'source/api.py::@module'}
    assert not any(links(before, before).values())


def test_legacy_ready_review_stays_readable_while_new_draft_requires_source_ref():
    item = {'path': INPUT, 'edit': 'Changed the handler.', 'recheck': 'Check again.'}
    legacy = ReportedPathEdit.model_validate(item)
    assert legacy.model_dump(mode='json', by_alias=True) == item
    draft = {'kind': 'READY', 'summary': 'Submitted.', 'questions': [], 'rejection': None,
             'repairPaths': {INPUT: {k: v for k, v in item.items() if k != 'path'}}}
    with pytest.raises(ValueError):
        validate_developer_decision(draft, [INPUT])
    for edit, ref in [('Changed.', None), (None, GENERIC_REF)]:
        draft['repairPaths'][INPUT].update(edit=edit, sourceRef=ref)
        with pytest.raises(ValueError):
            validate_developer_decision(draft, [INPUT])


def test_changing_existing_duplicate_key_response_does_not_cover_an_escaping_db_exception():
    before = {'api.py': GENERIC}
    after = {'api.py': GENERIC.replace(b'return duplicate()', b'return other_duplicate_response()')}
    evidence = changed_path_refs(before, after, DATABASE, ROUTE, ENTRY)
    assert evidence == {DATABASE[0]: set(), DATABASE[1]: {'source/api.py::signup'}}


def test_unbounded_expression_is_left_open_without_blocking_other_files():
    deep = b'x = ' + b'v.' * 1200 + b'v'
    before = {'api.py': GENERIC, 'large.py': deep}
    after = {**before, 'api.py': GENERIC.replace(b'return fixed_error()', b'print(exc)\n    return fixed_error()')}
    assert links(before, after)[LOG] == {GENERIC_REF}


@pytest.mark.parametrize(('allowance', 'decisions', 'last_reason'), [(0, 1, 'DISABLED'), (2, 2, 'REPEATED')])
def test_persistent_unsupported_ready_never_bypasses_correction_budget(allowance, decisions, last_reason):
    import asyncio
    import json
    from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
    from agents.llm.corrections import development_check
    from agents.llm.engine import LLMEngine
    from agents.roles.developer_contract import build_developer_output_contract
    from test_llm_runtime import FakeProvider, LLMRuntimeTests, text_response

    fixture = LLMRuntimeTests()
    fixture.setUp()
    after = GENERIC.replace(b'return fixed_error()', b'logger.addFilter(redactor)\n    return fixed_error()')
    changed = links({'api.py': GENERIC}, {'api.py': after})
    draft = dict(kind='READY', summary='Submitted.', questions=[], rejection=None, repairPaths=rows(GENERIC_REF))
    provider = FakeProvider(*[text_response(json.dumps(draft)) for _ in range(decisions)])
    events = []
    check = development_check(lambda value: validate_developer_decision(value, PATHS),
                              path_check=lambda value: claim_issues(value['repairPaths'], changed))
    engine = LLMEngine(role=fixture.role, provider=provider)
    with pytest.raises(LLMRuntimeError) as caught:
        asyncio.run(engine.run(prompt=fixture.prompt, workspace_id=fixture.workspace_id, model=fixture.model,
                               output=build_developer_output_contract(PATHS),
                               budget=fixture.budget(max_output_corrections=allowance),
                               source_progress_check=lambda: True, output_check=check, recovery_sink=events.append))
    assert caught.value.code is LLMErrorCode.SCHEMA
    assert len(provider.requests) == decisions
    last = events[-1].public()
    assert (last['code'], last['reason'], last['action']) == ('READY_PATH_EVIDENCE_MISMATCH', last_reason, 'STOP')
    assert last['unlinkedRepairPaths'] == list(PATHS[:2])
    # The safe structural comparison is durable for both the regeneration and the stop.
    from agents.llm.recovery import FailureRoute
    decisions = [r.public() for r in events if r.kind == 'DECISION']
    for decision in decisions:
        assert FailureRoute.model_validate(decision).public() == decision
        assert [r['path'] for r in decision['repairPathEvidence']] == list(PATHS[:2])
        assert all(r['attemptedSourceRef'] == GENERIC_REF and not r['candidateRefs']
                   and r['action'] == 'EDIT_SOURCE_OR_REPORT_OPEN' for r in decision['repairPathEvidence'])


def test_replacing_validation_registration_with_catch_all_cannot_claim_pre_route_response():
    after = GENERIC + b'from fastapi.exceptions import RequestValidationError\n'
    before = after.replace(b'@application.exception_handler(Exception)', b'@application.exception_handler(RequestValidationError)')
    assert not links({'api.py': before}, {'api.py': after})[INPUT]


def test_removing_last_log_write_from_an_active_handler_can_be_reported():
    before = GENERIC.replace(b'return fixed_error()', b'logger.error(exc)\n    return fixed_error()')
    assert links({'api.py': before}, {'api.py': GENERIC})[LOG] == {GENERIC_REF}
