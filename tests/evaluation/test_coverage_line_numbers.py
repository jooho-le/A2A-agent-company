"""Coverage counts real source lines, excluding synthetic interpreter entries."""
import pytest

from evaluation.coverage_runner import run_tests_with_coverage


@pytest.mark.parametrize('source, expected', [('ANSWER = 42\n', 1), ('# Only a comment.\n', 0)])
def test_synthetic_module_entry_does_not_add_a_source_line(tmp_path, source, expected):
    path = tmp_path / 'example.py'
    path.write_text(source, encoding='utf-8')

    class TrustedFixtureRunner:
        @staticmethod
        def run_tests(test_root, source_root, pattern):
            # This authored fixture exercises the same trace used for imports.
            exec(compile(path.read_bytes(), str(path), 'exec'), {})
            return {'fixture': True}, 0

    report, exit_code, coverage = run_tests_with_coverage(
        tmp_path, tmp_path, '*.py', runner=TrustedFixtureRunner())

    assert report == {'fixture': True} and exit_code == 0
    assert coverage['files']['example.py'] == {'lines': expected, 'executed': expected, 'missing': []}
