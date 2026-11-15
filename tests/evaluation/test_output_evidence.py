"""지정 출력만 읽고, 읽지 못한 출력·실제 노출·원문 비보관을 구분한다."""
import json
import os

import pytest

from evaluation.output_evidence import collect_output_evidence, read_known_values
from evaluation.trace_checks import check_trace_exposure
from tests.evaluation.test_trace_checks import CRITERIA, documents, uid


def collect(root, paths):
    return collect_output_evidence(root, {'runId': uid(1), 'files': [
        {'kind': 'AGENT_PROCESS_OUTPUT', 'path': path} for path in paths]}, uid(1))


def test_declared_log_is_scanned_without_copying_secret_or_path_to_report(tmp_path):
    secret = 'fictional-evaluation-canary-789'
    (tmp_path / 'agent.txt').write_text('received ' + secret)
    extra = collect(tmp_path, ['agent.txt'])
    result = check_trace_exposure(documents(runId=uid(1), additionalOutputs=extra), CRITERIA,
                                  known_values=[secret])
    assert result.status == 'FAIL'
    payload = result.to_dict()
    encoded = json.dumps(payload)
    assert secret not in encoded and 'agent.txt' not in encoded
    assert payload['coverage']['knownValues'] == 1
    assert payload['coverage']['additionalOutputs']['scope'] == 'DECLARED_FILES_ONLY'
    # 지정한 파일을 검사했어도 모든 프로세스 출력을 봤다고 과장하지 않는다.
    assert 'AGENT_PROCESS_OUTPUT' in payload['coverage']['notCollectedOutputs']


def test_unread_output_is_unverified_and_never_silently_clean(tmp_path):
    (tmp_path / 'binary').write_bytes(b'\xff\xfe')
    extra = collect(tmp_path, ['missing', 'binary'])
    assert [item['error'] for item in extra['items']] == ['OUTPUT_UNREADABLE', 'OUTPUT_NOT_UTF8']
    result = check_trace_exposure(documents(runId=uid(1), additionalOutputs=extra), CRITERIA)
    assert result.status == 'UNVERIFIED'


def test_paths_links_special_files_and_size_limit_cannot_escape_collection(tmp_path, monkeypatch):
    from evaluation import output_evidence
    (tmp_path / 'safe').write_text('0123456789')
    (tmp_path / 'link').symlink_to(tmp_path / 'safe')
    os.mkfifo(tmp_path / 'fifo')
    monkeypatch.setattr(output_evidence, 'MAX_FILE_BYTES', 4)
    result = collect(tmp_path, ['../outside', '/etc/passwd', 'link', 'fifo', 'safe'])
    assert all(item['status'] == 'UNVERIFIED' and 'text' not in item for item in result['items'])
    assert result['items'][-1]['error'] == 'OUTPUT_TOO_LARGE'


def test_wrong_run_and_duplicate_file_are_rejected(tmp_path):
    with pytest.raises(ValueError, match='OUTPUT_MANIFEST_INVALID'):
        collect_output_evidence(tmp_path, {'runId': uid(2), 'files': []}, uid(1))
    with pytest.raises(ValueError, match='OUTPUT_DUPLICATE'):
        collect(tmp_path, ['same', 'same'])


def test_known_values_file_is_validated_and_errors_never_include_values(tmp_path):
    path = tmp_path / 'canaries.json'
    path.write_text(json.dumps(['fictional-1', 'fictional-2']))
    assert read_known_values(path) == ('fictional-1', 'fictional-2')
    path.write_text(json.dumps({'password': 'fictional-1'}))
    with pytest.raises(ValueError, match='^KNOWN_VALUES_FILE_INVALID$'):
        read_known_values(path)
