"""Field pointers for fieldless QA/Security contract rejections.

``validate_qa_decision`` and ``validate_security_decision`` reject with one
stable reason and no field. The correction feedback was then empty, and two
different mistakes looked like the same one (live Run 3754980f; Codex review
2026-11-17 #1). These explainers re-check the common rules of the same
validators and name the field with a fixed reason code. They never decide
acceptance: the caller's validator still does, and an explainer that finds
nothing (or fails) simply reports nothing. No submitted value is echoed; only
Host-defined field names and array indexes appear in pointers.
"""
import re

from orchestrator.core.security import redact_data

_CONTROL = re.compile('[\x00-\x1f\x7f-\x9f]')
MAX_ISSUES = 16


class _Found(list):
    def add(self, pointer, code):
        if len(self) < MAX_ISSUES and {'fieldPath': pointer, 'reasonCode': code} not in self:
            self.append({'fieldPath': pointer, 'reasonCode': code})


def _texts(value, pointer, found):
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', key):
                _texts(item, f'{pointer}/{key}', found)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _texts(item, f'{pointer}/{index}', found)
    elif isinstance(value, str):
        if _CONTROL.search(value):
            found.add(pointer, 'TEXT_CONTROL_CHARACTER')
        elif redact_data(value) != value:
            found.add(pointer, 'TEXT_LOOKS_LIKE_SECRET')


def _fields(data, expected, found):
    for name in sorted(expected - set(data)):
        found.add('/' + name, 'FIELD_MISSING')
    if set(data) - expected:
        found.add('', 'FIELD_NOT_ALLOWED')


def _rows(rows, field, key, allowed, found, *, unknown, missing, duplicate):
    seen = set()
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        value = row.get(key) if isinstance(row, dict) else None
        if not isinstance(value, str) or value not in allowed:
            found.add(f'/{field}/{index}/{key}', unknown)
        elif value in seen and duplicate:
            found.add(f'/{field}/{index}/{key}', duplicate)
        else:
            seen.add(value)
    if missing and seen != set(allowed):
        found.add('/' + field, missing)
    return seen


def _references(rows, field, paths, found):
    from agents.roles.security_contract import MAX_SECURITY_LINE, MAX_SECURITY_REFERENCE_LINES, MAX_SECURITY_REFERENCES
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        references = row.get('references')
        base = f'/{field}/{index}/references'
        if not isinstance(references, list):
            continue
        if len(references) > MAX_SECURITY_REFERENCES:
            found.add(base, 'TOO_MANY_REFERENCES')
        keys = set()
        for number, reference in enumerate(references):
            if not isinstance(reference, dict):
                continue
            here = f'{base}/{number}'
            if reference.get('path') not in paths:
                found.add(here + '/path', 'REFERENCE_PATH_NOT_MEASURED')
            start, end = reference.get('startLine'), reference.get('endLine')
            if type(start) is not int or type(end) is not int or not 1 <= start <= end <= MAX_SECURITY_LINE:
                found.add(here + '/startLine', 'REFERENCE_LINES_INVALID')
            elif end - start + 1 > MAX_SECURITY_REFERENCE_LINES:
                found.add(here + '/endLine', 'REFERENCE_RANGE_TOO_LONG')
            key = (reference.get('path'), start, end)
            if key in keys:
                found.add(here, 'REFERENCE_DUPLICATE')
            keys.add(key)
        outcome = row.get('proposedOutcome', row.get('proposedDisposition'))
        if outcome in {'PASS', 'FAIL', 'CONFIRMED', 'FALSE_POSITIVE'} and not references:
            found.add(base, 'REFERENCES_REQUIRED_FOR_OUTCOME')


def _cases(rows, field, ids, allowed_pairs, found, *, tool_key):
    seen = set()
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        tool = row.get(tool_key) if tool_key else 'run_unit_tests'
        if (tool, row.get('selector')) not in allowed_pairs:
            found.add(f'/{field}/{index}/selector', 'SELECTOR_NOT_ALLOWED')
        if row.get('requirementId') not in ids:
            found.add(f'/{field}/{index}/requirementId', 'CASE_REQUIREMENT_NOT_ASSIGNED')
        key = (tool, row.get('selector'), row.get('testId'))
        if key in seen:
            found.add(f'/{field}/{index}/testId', 'CASE_DUPLICATE')
        seen.add(key)
    covered = {row.get('requirementId') for row in rows if isinstance(row, dict)} if isinstance(rows, list) else set()
    if not set(ids) <= covered:
        found.add('/' + field, 'ASSIGNED_REQUIREMENT_WITHOUT_CASE')


def qa_issues(data, requirement_ids, selectors) -> list[dict]:
    """Pointers for ``validate_qa_decision`` rejections (Multi QA and Single QA phase)."""
    found = _Found()
    if not isinstance(data, dict):
        return found
    _fields(data, {'kind', 'cases', 'questions'}, found)
    ids = {str(value) for value in requirement_ids}
    pairs = {(tool, name) for tool, names in (selectors or {}).items() for name in names}
    if data.get('kind') == 'READY':
        if data.get('questions'):
            found.add('/questions', 'QUESTIONS_NOT_ALLOWED_WHEN_READY')
        _cases(data.get('cases'), 'cases', ids, pairs, found, tool_key='toolName')
    elif data.get('kind') == 'REJECTED':
        if data.get('cases'):
            found.add('/cases', 'CASES_NOT_ALLOWED_WHEN_REJECTED')
        if data.get('questions'):
            found.add('/questions', 'QUESTIONS_NOT_ALLOWED_WHEN_REJECTED')
    elif data.get('kind') == 'INPUT_REQUIRED':
        if data.get('cases'):
            found.add('/cases', 'CASES_NOT_ALLOWED_WHEN_INPUT_REQUIRED')
        if not data.get('questions'):
            found.add('/questions', 'QUESTIONS_REQUIRED_WHEN_INPUT_REQUIRED')
    _texts(data, '', found)
    return list(found)


def security_issues(data, requirement_ids, finding_ids, source_paths, test_selectors=None) -> list[dict]:
    """Pointers for ``validate_security_decision`` rejections (Multi Security and Single SECURITY phase)."""
    found = _Found()
    if not isinstance(data, dict):
        return found
    selectors = tuple(test_selectors or ())
    _fields(data, {'kind', 'requirementReviews', 'findingReviews', 'questions'} | ({'testCases'} if selectors else set()),
            found)
    ids = {str(value) for value in requirement_ids}
    if data.get('kind') == 'READY':
        if data.get('questions'):
            found.add('/questions', 'QUESTIONS_NOT_ALLOWED_WHEN_READY')
        _rows(data.get('requirementReviews'), 'requirementReviews', 'requirementId', ids, found,
              unknown='REQUIREMENT_NOT_ASSIGNED', missing='ASSIGNED_REQUIREMENT_WITHOUT_REVIEW',
              duplicate='REQUIREMENT_DUPLICATE')
        _rows(data.get('findingReviews'), 'findingReviews', 'findingId', set(finding_ids), found,
              unknown='FINDING_NOT_MEASURED', missing='MEASURED_FINDING_WITHOUT_REVIEW', duplicate='FINDING_DUPLICATE')
        if selectors:
            _cases(data.get('testCases'), 'testCases', ids, {('run_unit_tests', name) for name in selectors}, found,
                   tool_key=None)
    paths = set(source_paths)
    _references(data.get('requirementReviews'), 'requirementReviews', paths, found)
    _references(data.get('findingReviews'), 'findingReviews', paths, found)
    _texts(data, '', found)
    return list(found)
