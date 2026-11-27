"""평가 담당이 지정한 출력 파일만 읽는다. 원문은 검사 중에만 사용하고 보고서에 복사하지 않는다."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat

KINDS = frozenset({'ARTIFACT_FILE_CONTENT', 'AGENT_PROCESS_OUTPUT'})
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024


def _read_file(root, relative, limit):
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or '..' in path.parts or '\\' in relative:
        raise ValueError('OUTPUT_PATH_INVALID')
    target = root
    for part in path.parts:
        target = target / part
        if target.is_symlink():
            raise ValueError('OUTPUT_LINK_REFUSED')
    if not target.resolve().is_relative_to(root):
        raise ValueError('OUTPUT_PATH_INVALID')
    fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('OUTPUT_NOT_REGULAR')
        if info.st_size > limit:
            raise ValueError('OUTPUT_TOO_LARGE')
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('OUTPUT_TOO_LARGE')
    return data


def collect_output_evidence(root, manifest, run_id):
    """manifest={runId, files:[{kind,path}]}; path는 명시된 root 아래의 상대 경로다.

    전체 시스템 출력의 완전성을 주장하지 않는다. 지정 파일의 읽기 성공/실패와 해시만 남긴다.
    """
    root = Path(root).resolve(strict=True)
    if not root.is_dir() or not isinstance(manifest, dict) or manifest.get('runId') != run_id:
        raise ValueError('OUTPUT_MANIFEST_INVALID')
    files = manifest.get('files')
    if not isinstance(files, list) or len(files) > 128:
        raise ValueError('OUTPUT_MANIFEST_INVALID')
    items, seen, used = [], set(), 0
    for row in files:
        if (not isinstance(row, dict) or row.get('kind') not in KINDS
                or not isinstance(row.get('path'), str)):
            raise ValueError('OUTPUT_MANIFEST_INVALID')
        key = row['kind'], row['path']
        if key in seen:
            raise ValueError('OUTPUT_DUPLICATE')
        seen.add(key)
        item = {'kind': row['kind'], 'sourceId': hashlib.sha256(row['path'].encode()).hexdigest(),
                'status': 'UNVERIFIED', 'error': None}
        try:
            data = _read_file(root, row['path'], min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - used))
            used += len(data)
            item.update(text=data.decode('utf-8'), sha256=hashlib.sha256(data).hexdigest(),
                        bytes=len(data), status='COLLECTED')
        except UnicodeError:
            item['error'] = 'OUTPUT_NOT_UTF8'
        except OSError:
            item['error'] = 'OUTPUT_UNREADABLE'
        except ValueError as error:
            item['error'] = str(error)
        items.append(item)
    return {'runId': run_id, 'scope': 'DECLARED_FILES_ONLY', 'items': items}


def read_known_values(path):
    """비밀값을 명령줄 인자로 받지 않는다. 파일 자체는 결과 폴더에 보관하지 않는다."""
    try:
        data = _read_file(Path(path).resolve().parent, Path(path).name, 1024 * 1024)
        values = json.loads(data)
        if (not isinstance(values, list) or len(values) > 1000
                or any(not isinstance(value, str) or not value or len(value) > 65536 for value in values)):
            raise ValueError()
        return tuple(values)
    except (OSError, ValueError, UnicodeError):
        raise ValueError('KNOWN_VALUES_FILE_INVALID') from None


def snapshot_output_evidence(run_id, snapshots):
    """Hash-verified registered snapshots, automatically supplied by handoff.

    Actual bytes remain available to the evaluator for known-value matching;
    output reports retain only hashes/findings. No generated file is executed.
    """
    items, tests, used = [], [], 0
    for version, files in sorted(snapshots.items()):
        test_files = {}
        for name, data in sorted(files.items()):
            identity = f'v{version}/{name}'
            item = {'kind': 'ARTIFACT_FILE_CONTENT', 'sourceId': hashlib.sha256(identity.encode()).hexdigest(),
                    'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data), 'error': None}
            if used + len(data) > MAX_TOTAL_BYTES:
                item.update(status='UNVERIFIED', error='OUTPUT_TOO_LARGE')
            else:
                # Binary source assets may contain plaintext secrets too. Decode
                # without executing/parsing the generated file; retain byte hash.
                item.update(status='COLLECTED', text=data.decode('utf-8', errors='replace'))
                used += len(data)
            items.append(item)
            path = PurePosixPath(name)
            if ('tests' in path.parts or path.name.startswith('test_') or path.name.endswith('_test.py')):
                test_files[name] = hashlib.sha256(data).hexdigest()
        tests.append({'codeVersion': version, 'testFilesSha256': hashlib.sha256(
            json.dumps(test_files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()})
    return {'runId': run_id, 'scope': 'DECLARED_FILES_ONLY', 'collectionScope': 'REGISTERED_SNAPSHOT_FILES',
            'items': items}, tests
