"""Known build markers become safe fix feedback only for the approved probe."""
import pytest

from orchestrator.sandbox.scn001_start_contract import scn001_build_argv, scn001_build_diagnostics


@pytest.mark.parametrize('code,location', [
    ('SCN001_MAIN_APP_REQUIRED', 'main.py'),
    ('SCN001_DATABASE_DEPENDENCY_REQUIRED', 'database.py'),
    ('SCN001_DATABASE_SESSION_REQUIRED', 'database.py'),
    ('SCN001_USER_MODEL_REQUIRED', 'models.py'),
    ('SCN001_USER_TABLE_REQUIRED', 'main.py'),
    ('SCN001_SIGNUP_ROUTE_REQUIRED', 'main.py'),
    ('SCN001_READY_ENDPOINT_REQUIRED', 'main.py'),
    ('SCN001_ENTRYPOINT_START_FAILED', 'main.py'),
    ('SCN001_DEPENDENCY_IMPORT_FAILED', 'main.py'),
    ('SCN001_IMPORT_FAILED', 'main.py'),
    ('SCN001_NAME_UNDEFINED', 'main.py'),
    ('SCN001_ATTRIBUTE_UNAVAILABLE', 'main.py'),
    ('SCN001_ARGUMENT_TYPE_INVALID', 'main.py'),
    ('SCN001_ARGUMENT_VALUE_INVALID', 'main.py'),
    ('SCN001_SOURCE_SYNTAX_INVALID', 'source'),
])
def test_fixed_failure_codes_have_concrete_feedback_without_raw_output(code, location):
    output = 'synthetic-private-output-must-not-be-published\n' + code + '\n'
    result = scn001_build_diagnostics(argv=scn001_build_argv(), exit_code=1, stdout=output)
    assert len(result) == 1 and result[0].rule_id == code
    assert result[0].normalized_location == location
    assert 'synthetic-private-output' not in result[0].model_dump_json()


@pytest.mark.parametrize('exit_code,output', [
    (0, 'SCN001_MAIN_APP_REQUIRED'), (2, 'SCN001_MAIN_APP_REQUIRED'),
    (1, 'SCN001_MAIN_APP_REQUIRED\nunrecognized-final-line'),
    (1, 'unknown-private-error'), (1, 'SCN001_START_CONTRACT_OK'),
])
def test_other_exit_codes_and_unknown_final_markers_are_not_invented_diagnostics(exit_code, output):
    assert scn001_build_diagnostics(argv=scn001_build_argv(), exit_code=exit_code, stdout=output) == ()


def test_other_host_commands_cannot_claim_probe_diagnostics():
    argv = scn001_build_argv()
    argv[-1] = 'print("SCN001_MAIN_APP_REQUIRED")'
    assert scn001_build_diagnostics(argv=argv, exit_code=1, stdout='SCN001_MAIN_APP_REQUIRED') == ()


def test_import_failure_location_is_bound_to_frozen_source_without_exception_text():
    output = ('private exception message\nSCN001_LOCATION:["routers/signup.py", 2]\n'
              'SCN001_DEPENDENCY_IMPORT_FAILED\n')
    result = scn001_build_diagnostics(argv=scn001_build_argv(), exit_code=1, stdout=output,
        source_files={'routers/signup.py': b'# test\nfrom unavailable import x\n'})
    assert result[0].normalized_location == 'routers/signup.py:2'
    assert 'ModuleNotFoundError' in result[0].message
    assert 'private exception' not in result[0].model_dump_json()
    assert 'from unavailable' not in result[0].model_dump_json()


@pytest.mark.parametrize('location', [
    '["/private/secret.py", 1]', '["../secret.py", 1]', '["absent.py", 1]',
    '["main.py", 0]', '["main.py", 200]', '["main.py", true]',
    '["main.py", 1, "private"]', '{"path":"main.py","line":1}', 'invalid json',
])
def test_invalid_or_unverified_frame_never_becomes_public_diagnostic(location):
    result = scn001_build_diagnostics(argv=scn001_build_argv(), exit_code=1,
        stdout=f'SCN001_LOCATION:{location}\nSCN001_DEPENDENCY_IMPORT_FAILED\n',
        source_files={'main.py': b'import example\n'})
    assert result[0].normalized_location == 'main.py'
    assert 'secret' not in result[0].model_dump_json()


@pytest.mark.parametrize('line,name,expression,reported', [
    ('f(missing.low_level.Type.ID, "PRIVATE_SAMPLE")', 'missing', 'missing.low_level.Type.ID', True),
    ('f(missing.a, missing.b)', 'missing', None, True),
    ('f("PRIVATE_SAMPLE")', 'absent', None, False),
    ('f(missing.x)', 'not-an-identifier', None, False),
])
def test_name_diagnostics_follow_only_identifiers_in_the_snapshot(line, name, expression, reported):
    import json
    marker = json.dumps(['main.py', 2, name])
    diagnostic, = scn001_build_diagnostics(argv=scn001_build_argv(), exit_code=1,
        stdout=f'SCN001_LOCATION:{marker}\nSCN001_NAME_UNDEFINED\n', source_files={'main.py': ('\n' + line).encode()})
    assert diagnostic.normalized_location == 'main.py:2'
    assert diagnostic.missing_name == (name if reported else None)
    assert diagnostic.source_expression == expression
    assert 'PRIVATE_SAMPLE' not in diagnostic.model_dump_json()


def test_developer_build_brief_uses_only_active_snapshot_and_report():
    from agents.runtime.developer_input import compact_fix_input
    diag, = scn001_build_diagnostics(argv=scn001_build_argv(), exit_code=1,
        stdout='SCN001_LOCATION:["main.py",1,"missing"]\nSCN001_NAME_UNDEFINED', source_files={'main.py': b'missing.part'})
    issue = dict(category='BUILD_CODE_FAILURE', issueId='build', codeVersion=2, sourceArtifactId='src2', reportArtifactId='rep2')
    record = dict(artifactId='rep2', codeVersion=2, sourceArtifactId='src2', executionOutcome='FAIL',
                  failureKind='PRODUCT', diagnostics=[diag.model_dump(by_alias=True)])
    def brief(**changes):
        artifact = {'artifactId': 'rep2', 'artifactType': 'BUILD_REPORT', 'record': {**record, **changes}}
        task = {'fixRequest': {'issues': [issue], 'inputArtifacts': [artifact]}}
        return compact_fix_input(task)['activeRepairFocus']['brief']['targets'][0]
    assert brief()['buildDiagnostics'] == [dict(ruleId='SCN001_NAME_UNDEFINED', normalizedLocation='main.py:1',
                                                 missingName='missing', sourceExpression='missing.part')]
    for mismatch in (dict(artifactId='other'), dict(sourceArtifactId='src1'), dict(codeVersion=1),
                     dict(failureKind='INFRASTRUCTURE'), dict(executionOutcome='PASS')):
        assert 'buildDiagnostics' not in brief(**mismatch)


@pytest.mark.parametrize('line,expression', [
    ('router.exception_handler("PRIVATE_SAMPLE")', 'router.exception_handler'),
    ('router.exception_handler or app.exception_handler', None),
    ('factory("PRIVATE_SAMPLE").exception_handler()', None),
    ('router.exception_handler or factory().exception_handler', None),
])
def test_attribute_context_is_only_an_unambiguous_identifier_chain(line, expression):
    diagnostic, = scn001_build_diagnostics(argv=scn001_build_argv(), exit_code=1,
        stdout='SCN001_LOCATION:["auth.py",1,"exception_handler"]\nSCN001_ATTRIBUTE_UNAVAILABLE',
        source_files={'auth.py': line.encode()})
    assert diagnostic.normalized_location == 'auth.py:1'
    assert diagnostic.source_expression == expression
    assert diagnostic.missing_name is None and 'PRIVATE_SAMPLE' not in diagnostic.model_dump_json()
