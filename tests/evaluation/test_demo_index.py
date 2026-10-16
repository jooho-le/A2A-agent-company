"""demo_index.py의 build_demo_index()(시연 자료 목록표 만들기)를 시험하는 파일이다.

확인하는 것: 분류별 파일 목록과 해시, 비어 있는 분류 표시, 위험한 입력 거부, 비밀값 검색.
시험용 폴더는 pytest가 임시로 만들어 주는 tmp_path를 쓴다. (시험이 끝나면 자동으로 지워진다.)
"""
from hashlib import sha256
import os
import subprocess
import sys
from uuid import UUID

import pytest

from evaluation.demo_index import build_demo_index

RUN = str(UUID(int=1, version=4))   # 시험용 가짜 Run 번호
NOW = '2026-10-14T12:00:00+09:00'   # 시험용 시각(시간대 포함)


def make(tmp_path):
    """시험용 자료 폴더를 만든다: 로그 1개, 화면 캡처 1개, 소스 1개. reports 폴더는 일부러 만들지 않는다."""
    (tmp_path / 'logs').mkdir()
    (tmp_path / 'logs' / 'run.log').write_text('started\n', encoding='utf-8')
    (tmp_path / 'screens').mkdir()
    (tmp_path / 'screens' / 'result.png').write_bytes(b'\x89PNG...')
    (tmp_path / 'source' / 'app').mkdir(parents=True)
    (tmp_path / 'source' / 'app' / 'main.py').write_text('print(1)\n', encoding='utf-8')
    return tmp_path


def test_index_lists_files_with_hash_and_marks_empty_category(tmp_path):
    """파일마다 해시·크기·종류가 기록되고, 하나도 없는 분류(reports)는 missingCategories에 표시된다."""
    index = build_demo_index(make(tmp_path), run_id=RUN, created_at=NOW, code_version=2, snapshot_sha256='a' * 64)
    assert [item['path'] for item in index['files']] == ['logs/run.log', 'source/app/main.py', 'screens/result.png']
    log = index['files'][0]
    assert log['sha256'] == sha256(b'started\n').hexdigest() and log['sizeBytes'] == 8 and log['mediaType'] == 'text/plain'
    assert index['counts'] == {'logs': 1, 'source': 1, 'screens': 1, 'reports': 0}
    assert index['missingCategories'] == ['reports']
    assert index['codeVersion'] == 2 and index['reportType'] == 'DEMO_MATERIAL_INDEX'


def test_same_input_gives_same_index(tmp_path):
    """같은 폴더로 두 번 만들면 결과가 완전히 같다."""
    root = make(tmp_path)
    assert build_demo_index(root, run_id=RUN, created_at=NOW) == build_demo_index(root, run_id=RUN, created_at=NOW)


def test_unknown_top_level_entry_and_symlink_are_rejected(tmp_path):
    """logs/source/screens/reports 외의 파일·폴더가 섞여 있거나, 바로가기(심볼릭 링크)가 있으면 거부한다."""
    root = make(tmp_path)
    (root / 'notes.txt').write_text('x', encoding='utf-8')
    with pytest.raises(ValueError):
        build_demo_index(root, run_id=RUN, created_at=NOW)
    (root / 'notes.txt').unlink()
    (root / 'logs' / 'link.log').symlink_to(root / 'logs' / 'run.log')
    with pytest.raises(ValueError):
        build_demo_index(root, run_id=RUN, created_at=NOW)


def test_known_sensitive_value_is_rejected_without_repeating_it(tmp_path):
    """알려 준 비밀값이 글자 파일에서 발견되면 거부한다. 오류 메시지에는 파일 경로만 나오고 비밀값은 다시 쓰지 않는다."""
    root = make(tmp_path)
    (root / 'logs' / 'leak.log').write_text('password=Sup3rS3cr3t\n', encoding='utf-8')
    with pytest.raises(ValueError) as error:
        build_demo_index(root, run_id=RUN, created_at=NOW, sensitive_values=['Sup3rS3cr3t'])
    assert 'logs/leak.log' in str(error.value) and 'Sup3rS3cr3t' not in str(error.value)


def test_binary_files_are_not_searched_for_secrets(tmp_path):
    """그림·영상 같은 바이너리 파일은 비밀값 검색에서 제외한다. (화면에 비밀번호가 보이는지는 사람이 직접 확인해야 한다.)"""
    root = make(tmp_path)
    (root / 'screens' / 'result.png').write_bytes(b'\x89PNG Sup3rS3cr3t')
    assert build_demo_index(root, run_id=RUN, created_at=NOW, sensitive_values=['Sup3rS3cr3t'])['counts']['screens'] == 1


@pytest.mark.parametrize('kwargs', [
    {'run_id': 'REQ-001'}, {'code_version': 5}, {'code_version': True}, {'snapshot_sha256': 'abc'},
    {'created_at': '2026-10-14T12:00:00'}, {'sensitive_values': ['']},
])
def test_invalid_arguments_are_rejected(tmp_path, kwargs):
    """잘못된 인자(UUID가 아닌 Run 번호, 범위 밖 코드 버전, 해시가 아닌 글자, 시간대 없는 시각, 빈 비밀값)는 거부한다."""
    arguments = dict(run_id=RUN, created_at=NOW) | kwargs  # | 는 두 사전을 합친다(뒤쪽 값이 앞쪽을 덮어씀).
    with pytest.raises(ValueError):
        build_demo_index(make(tmp_path), **arguments)


def test_missing_root_is_rejected(tmp_path):
    """존재하지 않는 폴더는 거부한다."""
    with pytest.raises(ValueError):
        build_demo_index(tmp_path / 'absent', run_id=RUN, created_at=NOW)


def test_secret_split_across_read_chunks_is_found_and_large_files_are_hashed(tmp_path):
    """파일을 1MiB씩 잘라 읽어도 두 조각에 걸친 비밀값을 찾아낸다. 큰 파일의 해시도 통째로 읽은 것과 같다."""
    root = make(tmp_path)
    padding = b'a' * (1024 * 1024 - 4)  # 비밀값이 첫 조각 끝 4글자와 다음 조각에 걸치도록 맞춘 채움 글자
    (root / 'logs' / 'edge.log').write_bytes(padding + b'Sup3rS3cr3t' + b'z')
    with pytest.raises(ValueError):
        build_demo_index(root, run_id=RUN, created_at=NOW, sensitive_values=['Sup3rS3cr3t'])
    big = b'x' * (3 * 1024 * 1024 + 7)
    (root / 'logs' / 'edge.log').write_bytes(big)
    entry = next(item for item in build_demo_index(root, run_id=RUN, created_at=NOW)['files'] if item['path'] == 'logs/edge.log')
    assert entry['sha256'] == sha256(big).hexdigest() and entry['sizeBytes'] == len(big)


def test_fifo_is_rejected_instead_of_blocking(tmp_path):
    """이름 있는 파이프(FIFO)는 열면 쓰는 쪽이 나타날 때까지 무기한 기다린다. 열기 전에 일반 파일이 아니라고 거부해야 한다.
    멈추는 경우를 확인하려고 별도 프로세스에서 3초 제한을 두고 실행한다."""
    (tmp_path / 'logs').mkdir()
    os.mkfifo(tmp_path / 'logs' / 'stream.log')
    code = (
        'from evaluation.demo_index import build_demo_index\n'
        'try:\n'
        f'    build_demo_index({str(tmp_path)!r}, run_id={RUN!r}, created_at={NOW!r})\n'
        'except ValueError as error:\n'
        '    raise SystemExit(0 if "regular" in str(error) else 3)\n'
        'raise SystemExit(2)\n')
    try:
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=3)
    except subprocess.TimeoutExpired:
        pytest.fail('demo index blocked while opening a FIFO')
    assert result.returncode == 0, result.stderr.decode()


def test_socket_files_are_rejected_too():
    """소켓 같은 특수 파일도 일반 파일이 아니므로 목록에 넣지 않고 거부한다.
    유닉스 소켓은 경로가 짧아야 만들 수 있어서(약 100자 제한) pytest의 긴 임시 폴더 대신 짧은 임시 폴더를 쓴다."""
    import shutil
    import socket
    import tempfile
    from pathlib import Path
    root = Path(tempfile.mkdtemp(prefix='di', dir='/tmp'))
    server = socket.socket(socket.AF_UNIX)
    try:
        (root / 'logs').mkdir()
        try:
            server.bind(str(root / 'logs' / 's.sock'))
        except OSError:
            pytest.skip('unix sockets cannot be created here')  # 권한이 제한된 실행 환경에서는 소켓 파일을 만들 수 없다.
        with pytest.raises(ValueError, match='regular'):
            build_demo_index(root, run_id=RUN, created_at=NOW)
    finally:
        server.close()
        shutil.rmtree(root, ignore_errors=True)
