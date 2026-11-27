"""Regressions for the live passing product blocked by a browser subdirectory.

These tests are authored for the next authorized verification, not executed as
part of the code-only repair. Generated fixtures must never run on the Host.
"""
from pathlib import Path
from uuid import uuid4

import pytest

from agents.roles.qa_contract import build_qa_output_contract
from agents.runtime.generated_test_inventory import unit_inventory, scope_root
from agents.runtime.generated_tests_precheck import inspect_generated_tests
from agents.runtime.qa_catalog import refresh_qa_output
from test_browser_draft_precheck import draft_fixture, write_suite, browser_case
from agents.runtime.qa_services import QARuntimeServices


SOURCE = ('import unittest\nraise RuntimeError("never import this fixture")\n'
          'class Signup(unittest.TestCase):\n def test_valid(self): pass\n')


def catalog(root, *, scopes=None, browser=()):
    return refresh_qa_output(build_qa_output_contract((uuid4(),), {'run_unit_tests': ('qa-unit',)}),
        root, scopes or [('qa-unit', 'test_*.py')], snapshot_sha256='a' * 64,
        unit_only=not browser, browser_suites=browser)


def test_browser_folder_does_not_hide_the_missing_class_segment(tmp_path):
    (tmp_path / 'test_signup.py').write_text(SOURCE)
    (tmp_path / 'browser').mkdir()
    (tmp_path / 'browser/suite.json').write_text('{}')
    draft = [{'selector': 'qa-unit', 'testId': 'test_signup.test_valid'}]
    issues = inspect_generated_tests(tmp_path, [('qa-unit', 'test_*.py')], draft, 'cases')
    assert {r['reasonCode'] for r in issues} == {'TEST_IN_FILE_NOT_DECLARED', 'DECLARED_TEST_NOT_IN_FILE'}
    _output, actual = catalog(tmp_path)
    assert actual['cases'] == [{'selector': 'qa-unit', 'testId': 'test_signup.Signup.test_valid'}]
    assert actual['unresolvedSelectors'] == []


def test_full_qa_precheck_checks_unit_ids_even_after_valid_browser_file(tmp_path):
    services, data = draft_fixture(tmp_path)
    write_suite(tmp_path / 'outputs/qa')
    data['cases'].append(browser_case())
    data['cases'][0]['testId'] = 'test_sample.test_ok'
    issues = QARuntimeServices.generated_test_issues(services, data)
    assert {r['reasonCode'] for r in issues} == {'TEST_IN_FILE_NOT_DECLARED', 'DECLARED_TEST_NOT_IN_FILE'}


def test_nonpackage_assets_do_not_disable_python_ids(tmp_path):
    (tmp_path / 'test_signup.py').write_text(SOURCE)
    assets = tmp_path / 'assets'
    assets.mkdir()
    for n in range(70):
        (assets / f'image-{n}.json').write_text('{}')
    assert unit_inventory(tmp_path, 'test_*.py').complete
    assert catalog(tmp_path)[1]['unresolvedSelectors'] == []


def test_simple_package_ids_are_relative_to_the_approved_discovery_root(tmp_path):
    package = tmp_path / 'account'
    package.mkdir()
    (package / '__init__.py').write_text('"""Tests."""\n')
    (package / 'test_signup.py').write_text(SOURCE)
    assert catalog(tmp_path)[1]['cases'] == [
        {'selector': 'qa-unit', 'testId': 'account.test_signup.Signup.test_valid'}]


def test_dynamic_package_initializers_do_not_fabricate_ids(tmp_path):
    package = tmp_path / 'account'
    package.mkdir()
    (package / '__init__.py').write_text('def load_tests(*args): raise RuntimeError("never run")\n')
    (package / 'test_signup.py').write_text(SOURCE)
    assert catalog(tmp_path)[1]['unresolvedSelectors'] == ['qa-unit']
    assert catalog(tmp_path)[1]['cases'] == []


def test_custom_source_directory_matches_runner_coordinates(tmp_path):
    unit = tmp_path / 'unit'
    unit.mkdir()
    (unit / 'test_signup.py').write_text(SOURCE)
    scopes = [('qa-unit', 'test_*.py', 'tests/unit')]
    # The unit scope starts inside unit/: it must not invent a unit. module prefix.
    actual = catalog(tmp_path, scopes=scopes)[1]
    assert actual['cases'][0]['testId'] == 'test_signup.Signup.test_valid'
    assert inspect_generated_tests(tmp_path, scopes, actual['cases'], 'cases') == []


@pytest.mark.parametrize('directory', ['/tmp/tests', 'tests/../escape', 'source', 'tests\\escape'])
def test_scope_paths_cannot_escape_output_root(tmp_path, directory):
    with pytest.raises(ValueError, match='GENERATED_TEST_SCOPE_INVALID'):
        scope_root(tmp_path, directory)


def test_symlink_scope_is_not_followed(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / 'unit').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        scope_root(tmp_path, 'tests/unit')


def test_missing_browser_has_an_exact_repair_path_and_independent_ids(tmp_path):
    root = tmp_path / 'tests'
    root.mkdir()
    (root / 'test_signup.py').write_text(SOURCE)
    suites = [('qa-browser', 'tests/browser/suite.json')]
    before = catalog(root, browser=suites)[1]
    assert before['missingRequiredWritePaths'] == ['outputs/qa/tests/browser/suite.json']
    assert before['cases'][0]['testId'] == 'test_signup.Signup.test_valid'
    write_suite(tmp_path)
    after = catalog(root, browser=suites)[1]
    assert after['missingRequiredWritePaths'] == []
    assert after['browserCases'][0]['testId'] == 'application.ready'
    assert after['filesSha256'] != before['filesSha256']


def test_rewriting_invalid_browser_input_updates_progress_fingerprint(tmp_path):
    root = tmp_path / 'tests'
    (root / 'browser').mkdir(parents=True)
    suite = root / 'browser/suite.json'
    suite.write_text('{bad')
    before = catalog(root, browser=[('qa-browser', 'tests/browser/suite.json')])[1]
    suite.write_text('{still-bad')
    after = catalog(root, browser=[('qa-browser', 'tests/browser/suite.json')])[1]
    assert before['filesSha256'] != after['filesSha256']
    assert before['browserCases'] == after['browserCases'] == []
