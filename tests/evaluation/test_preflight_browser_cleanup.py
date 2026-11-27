"""A passing browser report cannot hide an unconfirmed container cleanup."""
import json
from pathlib import Path
import subprocess

import pytest

from evaluation import preflight
from tests.evaluation.test_preflight import fake_docker, plan


def _browser_cleanup_failure(monkeypatch, failure):
    calls = fake_docker(monkeypatch)
    run = preflight.subprocess.run

    def damaged(arguments, **kwargs):
        result = run(arguments, **kwargs)
        if arguments[0] == 'fixture-docker' and arguments[1:4] == ['container', 'rm', '-f']:
            if failure == 'TIMEOUT':
                raise subprocess.TimeoutExpired(arguments, kwargs['timeout'])
            if failure == 'UNAVAILABLE':
                raise OSError('synthetic cleanup unavailable')
            return subprocess.CompletedProcess(arguments, 1, b'', b'synthetic cleanup failure')
        return result

    monkeypatch.setattr(preflight.subprocess, 'run', damaged)
    return calls


@pytest.mark.parametrize('failure', ['FAILED', 'TIMEOUT', 'UNAVAILABLE'])
def test_passing_browser_report_with_failed_cleanup_is_not_ready(tmp_path, monkeypatch, failure):
    data = plan(tmp_path)
    calls = _browser_cleanup_failure(monkeypatch, failure)
    result = preflight.check_preparation(data, docker='fixture-docker')
    browsers = [row for row in result['checks'] if row['check'].startswith('hostBrowser.')]
    assert result['ready'] is False
    assert len(browsers) == 2
    assert all(row['status'] == 'UNVERIFIED' and row['reason'] == 'HOST_BROWSER_CLEANUP_' + failure
               for row in browsers)
    assert len([args for args in calls if args[1:3] == ['container', 'create']]) == 1


def test_unconfirmed_cleanup_prevents_next_distinct_browser_container(tmp_path, monkeypatch):
    data = plan(tmp_path)
    tools_path = Path(data['hostConfigurations']['MULTI_AGENT']['tools'])
    tools = json.loads(tools_path.read_text())
    tools['browser']['ready_path'] = '/other-ready'
    tools_path.write_text(json.dumps(tools))
    calls = _browser_cleanup_failure(monkeypatch, 'FAILED')
    result = preflight.check_preparation(data, docker='fixture-docker')
    browsers = {row['check']: row for row in result['checks'] if row['check'].startswith('hostBrowser.')}
    assert browsers['hostBrowser.SINGLE_AGENT']['reason'] == 'HOST_BROWSER_CLEANUP_FAILED'
    assert browsers['hostBrowser.MULTI_AGENT']['reason'] == 'HOST_BROWSER_NOT_RUN_AFTER_UNVERIFIED_CLEANUP'
    assert all(row['status'] == 'UNVERIFIED' for row in browsers.values())
    assert len([args for args in calls if args[1:3] == ['container', 'create']]) == 1


def test_browser_timeout_with_confirmed_cleanup_keeps_timeout_reason(tmp_path, monkeypatch):
    data = plan(tmp_path)
    calls = fake_docker(monkeypatch)
    run = preflight.subprocess.run

    def timeout(arguments, **kwargs):
        result = run(arguments, **kwargs)
        if arguments[0] == 'fixture-docker' and arguments[1:3] == ['container', 'start']:
            raise subprocess.TimeoutExpired(arguments, kwargs['timeout'])
        return result

    monkeypatch.setattr(preflight.subprocess, 'run', timeout)
    result = preflight.check_preparation(data, docker='fixture-docker')
    assert result['ready'] is False
    assert {row['reason'] for row in result['checks'] if row['check'].startswith('hostBrowser.')} == {
        'HOST_BROWSER_CHECK_TIMEOUT'}
    assert len([args for args in calls if args[1:4] == ['container', 'rm', '-f']]) == 1


def test_explicitly_absent_browser_is_not_reported_as_browser_execution(tmp_path, monkeypatch):
    data = plan(tmp_path)
    for value in data['hostConfigurations'].values():
        path = Path(value['tools'])
        tools = json.loads(path.read_text())
        tools['browser'] = None
        path.write_text(json.dumps(tools))
    calls = fake_docker(monkeypatch)
    result = preflight.check_preparation(data, docker='fixture-docker')
    browsers = [row for row in result['checks'] if row['check'].startswith('hostBrowser.')]
    assert len(browsers) == 2
    assert all(row['check'].endswith('.notConfigured') and row['status'] == 'PASS' for row in browsers)
    assert not [args for args in calls if args[1:3] == ['container', 'create']]
