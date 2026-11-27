# 시연 때 모은 자료(실행 로그, 생성된 파일, 화면 캡처·영상)의 "목록표"를 만드는 파일이다.
# 각 파일의 이름·크기·해시(지문)를 적고, 어느 개발 실행(Run)과 코드 버전의 자료인지도 함께 적는다.
#
# 자료를 담아 둘 폴더 구조:
#     <root>/logs/...      실행 로그
#     <root>/source/...    생성된 소스 파일
#     <root>/screens/...   화면 캡처, 시연 영상
#     <root>/reports/...   QA·보안·독립 평가 보고서
# 이 파일은 각 파일을 읽어서 크기와 SHA-256 해시만 기록하고, 파일 내용을 복사하지는 않는다.
#
# 안전 장치:
#   - 비어 있는 분류(예: 로그가 하나도 없음)는 missingCategories에 따로 적는다. 자료가 없는데 있는 것처럼 보이지 않게 하려는 것이다.
#   - 미리 알려 준 비밀값(시험용 비밀번호 등)이 글자 파일에서 발견되면, 그 값은 말하지 않고 "어느 파일인지"만 알리며 목록 만들기를 거부한다.
#     원래 글자뿐 아니라 JSON·URL·HTML에 바뀌어 적힌 모양도 찾는다. 그림·영상·압축 파일은 검사하지 않고 sensitiveScan에 생략 이유를 남긴다.
#   - 바로가기(심볼릭 링크), 파이프·소켓 같은 일반 파일이 아닌 것, 256MiB를 넘는 파일, 최상위의 모르는 항목은 거부한다.
#   - macOS가 저절로 만드는 .DS_Store는 시연 자료가 아니므로 목록에서 빼고 거부하지도 않는다.
from hashlib import sha256  # 파일 내용의 지문(해시)을 만드는 도구
from html import escape  # HTML 특수문자를 &amp; 같은 표기로 바꾸는 도구(비밀값이 HTML 안에 바뀐 모양으로 들어 있는지 찾을 때 쓴다)
import json
import os  # 파일을 안전하게 여는 도구
from pathlib import Path  # 파일·폴더 경로를 다루는 도구
import re  # 글자 모양 검사(정규식)
import stat  # 파일의 종류(일반 파일, 폴더, 파이프 등)를 판별하는 도구
from typing import Iterable

from .aggregation import _uuid4
from .common import aware_time

CATEGORIES = ('logs', 'source', 'screens', 'reports')  # 허용하는 분류 폴더 이름 네 가지
MAX_FILE_BYTES = 256 * 1024 * 1024  # 파일 하나의 최대 크기: 256MiB. 허용 크기 안의 글자 파일은 끝까지 나눠 읽으며 비밀값을 찾는다.
# 운영체제가 폴더에 저절로 만드는 정보 파일. 시연 자료가 아니므로 목록에서 빼고, 최상위에 있어도 거부하지 않는다.
IGNORED_NAMES = frozenset({'.DS_Store'})
BINARY_SUFFIXES = frozenset({'.png', '.jpg', '.jpeg', '.gif', '.webp', '.mp4', '.mov', '.webm', '.pdf', '.zip', '.tar', '.gz'})
# 확장자로 파일 종류(미디어 타입)를 알려 주는 표. 표에 없는 확장자는 거부하지 않고 "알 수 없는 바이너리"로 적는다.
MEDIA_TYPES = {'.log': 'text/plain', '.txt': 'text/plain', '.md': 'text/markdown', '.json': 'application/json',
               '.py': 'text/x-python', '.html': 'text/html', '.png': 'image/png', '.jpg': 'image/jpeg',
               '.jpeg': 'image/jpeg', '.gif': 'image/gif', '.webp': 'image/webp', '.mp4': 'video/mp4',
               '.mov': 'video/quicktime', '.webm': 'video/webm', '.pdf': 'application/pdf'}


def build_demo_index(root, *, run_id: str, created_at: str, code_version: int | None = None,
                     snapshot_sha256: str | None = None, sensitive_values: Iterable[str] = ()) -> dict:
    # root 폴더 아래 분류 폴더들의 파일을 목록(사전)으로 만들어 돌려준다.
    # 같은 폴더·같은 입력이면 항상 같은 결과가 나온다(경로 순서로 정렬하기 때문).
    #   run_id          : 이 자료가 어느 개발 실행의 것인지
    #   created_at      : 목록을 만든 시각(시간대 포함)
    #   code_version    : (선택) 코드 버전 1~4
    #   snapshot_sha256 : (선택) 그 코드 묶음의 해시
    #   sensitive_values: 자료 안에 들어 있으면 안 되는 글자들(시험용 비밀번호 등)

    # 1) 입력 확인
    _uuid4(run_id)
    if code_version is not None and (type(code_version) is not int or not 1 <= code_version <= 4):
        raise ValueError('code_version must be an integer from 1 to 4')
    if snapshot_sha256 is not None and not re.fullmatch(r'[0-9a-f]{64}', snapshot_sha256):
        raise ValueError('snapshot_sha256 must be a SHA-256 hex digest')
    aware_time(created_at, 'created_at')  # 올바른 시각인지만 확인한다.
    values = list(sensitive_values)
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError('Sensitive values must be nonempty strings')
    secrets = _secret_forms(values)  # 파일 내용(바이트)과 비교할 모양들. 원래 글자뿐 아니라 JSON·URL·HTML에 바뀌어 적힌 모양도 찾는다.
    base = Path(root)
    # 폴더가 아니거나 바로가기(심볼릭 링크)면 거부한다. 바로가기는 폴더 밖의 파일을 몰래 가리킬 수 있기 때문이다.
    if not base.is_dir() or base.is_symlink():
        raise ValueError('root must be an existing directory')
    base = base.resolve()  # 절대 경로로 바꾼다.
    # 최상위에는 logs/source/screens/reports 네 폴더만 있어야 한다. 다른 파일이나 폴더가 섞여 있으면 거부한다.
    unknown = sorted(entry.name for entry in base.iterdir() if entry.name not in CATEGORIES and entry.name not in IGNORED_NAMES)
    if unknown:
        raise ValueError(f'unknown top-level entries: {unknown}')

    # 2) 분류 폴더를 하나씩 돌며 파일 목록 만들기
    files = []
    for category in CATEGORIES:
        folder = base / category  # "/" 연산자로 경로를 이어 붙인다.
        if not folder.exists():
            continue  # 이 분류 폴더가 없으면 건너뛴다(나중에 missingCategories에 나타난다).
        if folder.is_symlink() or not folder.is_dir():
            raise ValueError(f'{category} must be a directory')
        for path in sorted(folder.rglob('*')):  # rglob('*') = 하위 폴더까지 모든 항목
            if path.is_symlink():
                raise ValueError(f'symbolic links are not allowed: {path.relative_to(base).as_posix()}')
            if path.is_dir() or (path.name in IGNORED_NAMES and not path.is_symlink()):
                continue  # 폴더 자체와 운영체제 정보 파일(.DS_Store)은 목록에 넣지 않는다.
            relative = path.relative_to(base).as_posix()  # root 기준의 상대 경로(슬래시 / 로 통일)
            # 일반 파일만 받는다. 이름 있는 파이프(FIFO)·소켓·장치 파일을 열면 쓰는 쪽이 나타날 때까지 무기한 기다리게 되어
            # 목록 만들기가 멈출 수 있다. 그래서 열기 전에 파일 종류부터 확인한다.
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError(f'only regular files are allowed: {relative}')
            # 실제 위치가 root 밖으로 빠져나가지 않는지 한 번 더 확인한다.
            if path.resolve().parent != path.parent.resolve() or base not in path.resolve().parents:
                raise ValueError(f'path escapes the material folder: {relative}')
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValueError(f'file is too large: {relative}')
            # 텍스트 크기로 검색을 생략하지 않는다. 바이너리·비밀값 목록 없음은 출력에 생략 이유를 남긴다.
            binary = path.suffix.lower() in BINARY_SUFFIXES
            scan = bool(secrets) and not binary
            skipped = 'BINARY_FORMAT' if binary else 'NO_SENSITIVE_VALUES' if not secrets else None
            digest, found = _digest_and_scan(path, secrets, scan, relative)
            if found:
                # 값 자체는 오류 메시지에 쓰지 않고 파일 경로만 알린다(오류 메시지에서 또 새어 나가지 않게).
                raise ValueError(f'known sensitive value found in: {relative}')
            files.append({'category': category, 'path': relative, 'sizeBytes': size,
                          'sha256': digest,
                          'sensitiveScan': {'status': 'CHECKED' if scan else 'SKIPPED', 'reason': skipped},
                          'mediaType': MEDIA_TYPES.get(path.suffix.lower(), 'application/octet-stream')})
    # 3) 분류별 파일 개수를 세고, 하나도 없는 분류는 따로 적는다.
    counts = {category: sum(1 for item in files if item['category'] == category) for category in CATEGORIES}
    return {'schemaVersion': '1.0', 'reportType': 'DEMO_MATERIAL_INDEX', 'runId': run_id,
            'codeVersion': code_version, 'snapshotSha256': snapshot_sha256, 'createdAt': created_at,
            'files': files, 'counts': counts,
            'missingCategories': [category for category in CATEGORIES if counts[category] == 0]}


def _secret_forms(values):
    # 비밀값 하나가 파일 안에 적힐 수 있는 모양들을 바이트로 돌려준다. 같은 값이라도 저장 형식에 따라 글자가 바뀌어 적힌다.
    #   원래 글자(UTF-8) / JSON 안("\\"·\\uXXXX로 바뀐 글자, 한글을 그대로 둔 경우와 바꾼 경우) / URL 인코딩(%XX, 공백은 + 또는 %20) / HTML(&amp; 등)
    # 이 밖의 변환(base64, 압축, 다른 인코딩)은 찾지 못한다(문서에 적은 한계).
    from urllib.parse import quote, quote_plus
    forms = set()
    for value in values:
        for text in (value, json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1],
                     quote(value, safe=''), quote_plus(value, safe=''), escape(value)):
            forms.add(text.encode('utf-8'))
    return sorted(forms)


def _digest_and_scan(path, secrets, scan, relative):
    # 파일의 해시(지문)를 계산하고, scan이 True면 비밀값이 들어 있는지도 함께 찾는다.
    # 파일을 1MiB씩 잘라 읽는다. 영상처럼 큰 파일을 통째로 메모리에 올리면 컴퓨터가 느려질 수 있기 때문이다.
    # 조각(chunk)을 나눠 읽으면 비밀값이 두 조각에 걸쳐 있을 수 있으므로,
    # 직전 조각의 끝부분(tail)을 다음 조각 앞에 붙여서 검사한다.
    # 돌려주는 값: (해시 글자, 비밀값을 찾았는가)
    digest, found, tail = sha256(), False, b''
    # 가장 긴 비밀값보다 1바이트 짧은 길이만큼만 이어 붙이면 어떤 경계에 걸쳐 있어도 찾을 수 있다.
    keep = max((len(value) for value in secrets), default=1) - 1
    # 열기 직전에 파일이 파이프로 바뀌는 경우까지 막으려고, 기다리지 않는 방식(O_NONBLOCK)으로 열고 열린 파일의 종류를 다시 확인한다.
    # 바로가기(심볼릭 링크)는 따라가지 않는다(O_NOFOLLOW).
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f'only regular files are allowed: {relative}')
    with os.fdopen(descriptor, 'rb') as handle:  # 'rb' = 바이트로 읽기. with는 다 쓰면 파일을 자동으로 닫아 준다.
        while chunk := handle.read(1024 * 1024):  # 더 읽을 내용이 없을 때까지 1MiB씩 읽는다.
            digest.update(chunk)  # 해시 계산에 이 조각을 보탠다.
            if scan and not found:
                window = tail + chunk
                found = any(value in window for value in secrets)
                tail = window[-keep:] if keep else b''
    return digest.hexdigest(), found
