"""SCN-001 실험 실행 환경 준비: Host 샌드박스(= D5 채점) 공통 이미지, 도구 설정, 시작 저장소, 계획 칸.

    python -m evaluation.environment status                      # 무엇이 준비됐고 무엇이 남았는지(네트워크 없음)
    python -m evaluation.environment wheels   --allow-network    # PyPI wheel 받기 → requirements.lock
    python -m evaluation.environment browsers --allow-network    # Playwright Chromium headless shell + apt 목록
    python -m evaluation.environment image    --allow-network    # 베이스(apt만 네트워크) → 샌드박스 이미지(네트워크 없음)
    python -m evaluation.environment configure                   # 도구 설정·시작 저장소·Ver1/Ver2·계획 채우기 + 사전점검
    python -m evaluation.environment all      --allow-network    # 위 다섯 단계를 차례로

하는 일과 원칙:
- 다운로드는 --allow-network를 준 명령에서만 일어난다(운영자 승인 뒤). 받은 파일은 git에 올리지 않는
  docker/sandbox/prepared/에만 둔다. 이미지에 생성 코드·시험지·API 키를 넣지 않는다.
- Host 샌드박스와 D5 최종 채점은 같은 이미지·같은 lock을 써야 한다(D5는 Manifest의 이미지 해시와 실제 이미지를 대조한다).
  시작 저장소의 requirements.lock도 같은 바이트다(dev Host가 lock 해시를 대조한다).
- 정한 값(4번 결정, 2026-11-15): Python 3.13 slim(Debian 13, linux/arm64) 고정 베이스, Playwright 1.65.2, Bandit 1.9.4,
  email-validator 포함, 도구 한도·argv는 아래 상수. 바꾸면 lock·이미지·도구 설정을 다시 만들고 계획의 값이 따라 바뀐다.
- 모델 호출이나 Run 제출은 하지 않는다. API 키는 읽지 않고 환경 변수가 있는지만 status에 표시한다.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
PREPARED = ROOT / 'docker/sandbox/prepared'
SANDBOX_CONTEXT = ROOT / 'docker/sandbox'
BASE_CONTEXT = ROOT / 'docker/experiment'
CONFIGS = ROOT / 'configs'
SKELETON = ROOT / 'examples/starting-source/scn001-signup'
STARTING_REPOSITORY = ROOT / '.data/starting-source/scn001'
PLANS = (ROOT / 'examples/evaluation/scn001-pilot.ver1.plan.json', ROOT / 'examples/evaluation/scn001-pilot.ver2.plan.json')

PYTHON_IMAGE = 'python@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90'  # python:3.13-slim
BASE_NAME, IMAGE_NAME, TAG = 'capstone-sandbox-base', 'capstone-sandbox', 'scn001'
PLAYWRIGHT_VERSION, BANDIT_VERSION = '1.65.2', '1.9.4'
# 생성 코드가 쓸 수 있는 패키지(골격 requirements.in + email-validator)와 순환 도구(Playwright·Bandit). 전이 의존성은 pip가 정하고
# 받은 wheel 전부를 버전·해시로 lock에 고정한다. 앞 다섯 개는 D5 Docker 검증(2026-11-15)에서 쓴 버전과 같다.
REQUIREMENTS = ('fastapi==0.142.2', 'uvicorn==0.54.0', 'sqlalchemy==2.1.6', 'argon2-cffi==25.1.0', 'httpx==0.28.1',
                'email-validator==2.3.0', f'playwright=={PLAYWRIGHT_VERSION}', f'bandit=={BANDIT_VERSION}')
# 평가 정적 보조 판정(scn001_security_criteria.json)과 같은 규칙: 코드 속 비밀번호(B105~107), 약한 해시(B303·B324).
BANDIT_RULES = ('B105', 'B106', 'B107', 'B303', 'B324')
BANDIT_PROFILE_REF = 'artifact://evaluation/scn-001/bandit-password-storage-v1'
MAX_CALL_SECONDS = 120  # 계획 limits.toolTimeoutSeconds와 같아야 한다(dev Host 검사)
# Playwright 드라이버(내장 node)가 실행할 때 필요한 라이브러리. python:3.13-slim에 없고 install-deps 목록에도
# 없을 수 있어 준비 컨테이너와 베이스 이미지에 항상 먼저 넣는다(2026-11-15 실제 준비에서 확인).
DRIVER_PACKAGES = ('libatomic1',)
# Playwright가 Debian 13에서 Chromium 실행에 직접 요구하는 공유 라이브러리와 기본 글꼴 하나. `install-deps`의 전체 목록은
# 화면용 xvfb·mesa·LLVM·여러 글꼴까지 109개라 headless shell에는 과하다. apt가 이들의 필수 의존성만 더하고(--no-install-recommends),
# 빌드 뒤 실제 브라우저 실행 파일에 빠진 라이브러리가 없는지 ldd로 확인한다.
CHROMIUM_PACKAGES = ('libasound2t64', 'libatk-bridge2.0-0t64', 'libatk1.0-0t64', 'libatspi2.0-0t64', 'libcairo2',
                     'libcups2t64', 'libdbus-1-3', 'libdrm2', 'libgbm1', 'libglib2.0-0t64', 'libnspr4', 'libnss3',
                     'libpango-1.0-0', 'libx11-6', 'libxcb1', 'libxcomposite1', 'libxdamage1', 'libxext6', 'libxfixes3',
                     'libxkbcommon0', 'libxrandr2', 'fonts-liberation')
PYTHON = '/usr/local/bin/python'


class EnvironmentError_(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _limits(timeout, memory_gib=1):
    # 도구 한 번의 자원 한도. maxCallSeconds ≥ timeout + 2×control + 1 (dev tool_configuration 규칙)을 지킨다.
    return {'cpus': 2.0, 'memory_bytes': memory_gib * 1024 ** 3, 'pids': 256, 'timeout_seconds': timeout,
            'tmpfs_bytes': 256 * 1024 ** 2, 'max_stdout_bytes': 1024 ** 2, 'max_stderr_bytes': 1024 ** 2,
            'control_timeout_seconds': 10}


def tools_document(*, image_id, lock_file, lock_hash, hardware_profile, unit_scopes):
    """Ver1·Ver2의 바탕이 되는 Host 도구 설정(owned-tools.local.json). experiment configure가 보호 범위만 바꿔 두 벌을 만든다."""
    return {
        'schemaVersion': 1, 'imageReference': image_id, 'dependencyLockFile': lock_file,
        'dependencyLockHash': lock_hash, 'hardwareProfile': hardware_profile,
        'dockerEndpoint': 'unix:///var/run/docker.sock', 'maxCallSeconds': MAX_CALL_SECONDS,
        # 빌드 = 모든 파이썬 파일 문법 확인. /snapshot은 읽기 전용이라 바이트코드는 /work(임시 쓰기)에 둔다.
        'build': {'profile': {'name': 'scn001-compile',
                              'argv': [PYTHON, '-I', '-X', 'pycache_prefix=/work/pycache', '-m', 'compileall', '-q',
                                       '/snapshot'],
                              'limits': _limits(60)}},
        'unit': {'scopes': list(unit_scopes), 'python_executable': PYTHON, 'limits': _limits(90)},
        # 브라우저 시험의 서버: 연결 규격대로 `uvicorn main:app`, 준비 확인은 GET /.
        'browser': {'suites': [{'name': 'qa-browser', 'kind': 'QA_TESTS', 'suite_path': 'tests/browser/suite.json'}],
                    'service_argv': [PYTHON, '-m', 'uvicorn', '--app-dir', '/snapshot', 'main:app',
                                     '--host', '127.0.0.1', '--port', '8765'],
                    'playwright_version': PLAYWRIGHT_VERSION, 'python_executable': PYTHON,
                    'base_url': 'http://127.0.0.1:8765', 'ready_path': '/', 'startup_timeout_seconds': 20,
                    'action_timeout_ms': 5000, 'limits': _limits(90, memory_gib=2)},
        'security': {'profiles': [{'name': 'scn001-bandit', 'scanner_version': BANDIT_VERSION,
                                   'rule_ids': list(BANDIT_RULES), 'profile_ref': BANDIT_PROFILE_REF}],
                     'python_executable': PYTHON, 'limits': _limits(60)},
    }


def _project(filename):
    # wheel 파일 이름 → (정규화한 프로젝트 이름, 버전). PEP 427: {name}-{version}(-{build})?-{py}-{abi}-{platform}.whl
    parts = filename[:-4].split('-')
    if not filename.endswith('.whl') or len(parts) < 5:
        raise EnvironmentError_('WHEEL_NAME_INVALID')
    return re.sub(r'[-_.]+', '-', parts[0]).lower(), parts[1]


def lock_from_wheelhouse(wheelhouse):
    """받은 wheel 전부를 `이름==버전 --hash=sha256:…`로 고정한 lock 글. 한 프로젝트에 wheel이 둘 이상이면 거부한다."""
    rows = {}
    for path in sorted(Path(wheelhouse).glob('*.whl')):
        name, version = _project(path.name)
        if name in rows:
            raise EnvironmentError_('WHEELHOUSE_HAS_DUPLICATE_PROJECT')
        rows[name] = f'{name}=={version} --hash=sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}'
    if not rows:
        raise EnvironmentError_('WHEELHOUSE_EMPTY')
    for requirement in REQUIREMENTS:
        name, version = requirement.split('==')
        if not rows.get(re.sub(r'[-_.]+', '-', name).lower(), '').startswith(f'{name.lower()}=={version} '):
            raise EnvironmentError_('WHEELHOUSE_MISSING_' + name.upper())
    header = '# SCN-001 Host 샌드박스·D5 채점·시작 저장소 공통 lock (linux/arm64, CPython 3.13). evaluation.environment가 만듦\n'
    return header + '\n'.join(rows[name] for name in sorted(rows)) + '\n'


def apt_packages(dry_run_text):
    """`playwright install-deps --dry-run` 출력에서 apt 패키지 이름만 뽑는다(순서 유지, 중복 제거).

    예전 형식은 `sh -c "apt-get update&& apt-get install -y ... 패키지"` 명령 한 줄이고, Playwright 1.65는
    `Missing system dependencies (N):` 아래에 패키지를 한 줄씩 적는다. 둘 다 읽는다(기록·비교용).
    """
    found, listing = [], False

    def add(token):
        token = token.strip('"\'')
        if not token or token.startswith('-') or token in found:
            return
        if not re.fullmatch(r'[a-z0-9][a-z0-9+.-]+', token):
            raise EnvironmentError_('APT_PACKAGE_NAME_INVALID')
        found.append(token)
    for line in dry_run_text.splitlines():
        if 'apt-get install' in line:
            for token in line.split('apt-get install', 1)[1].split():
                add(token)
        elif re.match(r'^Missing system dependencies \(\d+\):\s*$', line):
            listing = True
        elif listing and re.match(r'^\s+\S+\s*$', line):
            add(line.strip())
        else:
            listing = False
    if not found:
        raise EnvironmentError_('APT_PACKAGES_NOT_FOUND')
    return found


def _run(args, *, env=None, capture=False, quiet=False):
    try:
        done = subprocess.run(args, check=True, text=True, env=env,
                              stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.DEVNULL if quiet else None)
    except (OSError, subprocess.CalledProcessError) as error:
        raise EnvironmentError_('COMMAND_FAILED: ' + ' '.join(args[:3])) from error
    return done.stdout if capture else None


def _image_id(reference):
    return _run(['docker', 'image', 'inspect', '--format', '{{.Id}}', reference], capture=True, quiet=True).strip()


def fetch_wheels():
    PREPARED.mkdir(parents=True, exist_ok=True)
    wheelhouse = PREPARED / 'wheelhouse'
    if wheelhouse.exists() and any(wheelhouse.iterdir()):
        raise EnvironmentError_('WHEELHOUSE_NOT_EMPTY')  # 다른 버전 wheel이 섞이지 않게 빈 폴더에서만 받는다
    _run(['docker', 'run', '--rm', '-v', f'{PREPARED}:/out', PYTHON_IMAGE, 'python', '-m', 'pip', 'download',
          '--only-binary=:all:', '--no-cache-dir', '--disable-pip-version-check', '-d', '/out/wheelhouse', *REQUIREMENTS])
    (PREPARED / 'requirements.lock').write_text(lock_from_wheelhouse(wheelhouse), encoding='utf-8')


def fetch_browsers():
    if not (PREPARED / 'requirements.lock').is_file():
        raise EnvironmentError_('LOCK_MISSING_RUN_WHEELS_FIRST')
    script = (f'apt-get update >/dev/null && apt-get install -y --no-install-recommends {" ".join(DRIVER_PACKAGES)} '
              f'>/dev/null && python -m pip install --disable-pip-version-check --root-user-action=ignore '
              f'--no-index --find-links /out/wheelhouse '
              f'playwright=={PLAYWRIGHT_VERSION} >/dev/null && python -m playwright install chromium-headless-shell '
              f'&& (python -m playwright install-deps --dry-run chromium-headless-shell > /out/apt-dry-run.txt; true)')
    _run(['docker', 'run', '--rm', '-e', 'PLAYWRIGHT_BROWSERS_PATH=/out/ms-playwright', '-v', f'{PREPARED}:/out',
          PYTHON_IMAGE, 'sh', '-c', script])
    apt_packages((PREPARED / 'apt-dry-run.txt').read_text(encoding='utf-8'))  # 목록을 못 뽑으면 여기서 멈춘다


def browser_binary():
    found = sorted((PREPARED / 'ms-playwright').glob('chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell'))
    if len(found) != 1:
        raise EnvironmentError_('BROWSER_BINARY_NOT_FOUND')
    return found[0].relative_to(PREPARED / 'ms-playwright').as_posix()


def missing_libraries(ldd_text):
    # ldd 출력에서 찾지 못한 공유 라이브러리 이름(`libX.so.N => not found`)
    return sorted({line.split()[0] for line in ldd_text.splitlines() if 'not found' in line})


def build_image():
    packages = list(DRIVER_PACKAGES) + [name for name in CHROMIUM_PACKAGES if name not in DRIVER_PACKAGES]
    binary = browser_binary()
    env = {**os.environ, 'DOCKER_BUILDKIT': '1'}
    _run(['docker', 'build', '-t', f'{BASE_NAME}:{TAG}', '--build-arg', f'PYTHON_IMAGE={PYTHON_IMAGE}',
          '--build-arg', 'PACKAGES=' + ' '.join(packages), '-f', str(BASE_CONTEXT / 'base.Dockerfile'), str(BASE_CONTEXT)],
         env=env)
    base_id = _image_id(f'{BASE_NAME}:{TAG}')
    # 실제 브라우저 실행 파일이 이 베이스에서 모든 공유 라이브러리를 찾는지 네트워크 없이 확인한다.
    ldd = _run(['docker', 'run', '--rm', '--network', 'none', '-v', f'{PREPARED / "ms-playwright"}:/ms-playwright:ro',
                base_id, 'ldd', f'/ms-playwright/{binary}'], capture=True)
    missing = missing_libraries(ldd)
    if missing:
        raise EnvironmentError_('BROWSER_LIBRARIES_MISSING: ' + ','.join(missing))
    # dev 레시피는 BASE_IMAGE가 `이름@sha256:…` 모양이어야 한다(가변 태그 금지). 로컬에서 만든 베이스는 레지스트리에 없으므로
    # 그 참조 문자열 자체를 키로 한 빌드 컨텍스트로 방금 만든 로컬 이미지에 연결한다(BuildKit이 Docker Hub를 찾지 않게).
    reference = f'{BASE_NAME}@{base_id}'
    if _image_id(f'{BASE_NAME}:{TAG}') != base_id:
        raise EnvironmentError_('BASE_IMAGE_CHANGED_BEFORE_BUILD')
    _run(['docker', 'buildx', 'build', '--load', '-t', f'{IMAGE_NAME}:{TAG}', '--build-arg', f'BASE_IMAGE={reference}',
          '--build-context', f'{reference}=docker-image://{BASE_NAME}:{TAG}',
          '-f', str(SANDBOX_CONTEXT / 'Dockerfile'), str(SANDBOX_CONTEXT)], env=env)
    record = {'schemaVersion': 1, 'pythonImage': PYTHON_IMAGE, 'baseImage': f'{BASE_NAME}@{base_id}',
              'imageId': _image_id(f'{IMAGE_NAME}:{TAG}'), 'imageTag': f'{IMAGE_NAME}:{TAG}',
              'lockSha256': 'sha256:' + hashlib.sha256((PREPARED / 'requirements.lock').read_bytes()).hexdigest(),
              'playwrightVersion': PLAYWRIGHT_VERSION, 'banditVersion': BANDIT_VERSION, 'aptPackages': packages}
    (PREPARED / 'image.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return record


def hardware_profile():
    """실험 컴퓨터와 Docker VM을 한 줄로 적는다. 계획(hardwareProfile)과 도구 설정이 같은 값을 쓴다."""
    def sysctl(name):
        try:
            return subprocess.run(['sysctl', '-n', name], check=True, text=True, capture_output=True).stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return 'unknown'
    info = _run(['docker', 'info', '--format', '{{.ServerVersion}} {{.OSType}}/{{.Architecture}} {{.NCPU}} {{.MemTotal}}'],
                capture=True).split()
    host = re.sub(r'[^A-Za-z0-9.,+-]+', '-', f"{sysctl('hw.model')}-{sysctl('machdep.cpu.brand_string')}").strip('-')
    memory = round(int(sysctl('hw.memsize')) / 1024 ** 3) if sysctl('hw.memsize').isdigit() else 'unknown'
    return (f"{host}-{sysctl('hw.ncpu')}cpu-{memory}gib/docker-{info[0]}-{info[1]}-{info[2]}cpu-"
            f"{round(int(info[3]) / 1024 ** 3, 1)}gib/{platform.system().lower()}")


def check_bandit_rules(image_id):
    """도구 설정의 Bandit 규칙이 이미지의 Bandit에 실제로 있는지 본다(없는 규칙은 dev 실행기가 거부해 보안 검사가 매번 실패한다)."""
    code = ('import json; from bandit.core import extension_loader as e; m = e.MANAGER; '
            'print(json.dumps(sorted(set(m.plugins_by_id) | set(m.blacklist_by_id))))')
    available = set(json.loads(_run(['docker', 'run', '--rm', '--network', 'none', image_id, PYTHON, '-I', '-c', code],
                                    capture=True)))
    missing = sorted(set(BANDIT_RULES) - available)
    if missing:
        raise EnvironmentError_('BANDIT_RULES_NOT_IN_IMAGE: ' + ','.join(missing))


def update_plan(plan, *, image_id, hardware, commit_hash, repository_path, lock_path='requirements.lock'):
    """시범 계획의 환경 칸을 실제 준비 값으로 채운다. 나머지(모델·한도·반복·순서)는 그대로 둔다.

    repository_path는 계획 파일 폴더 기준 상대 경로로 적는다(저장소에 올리는 계획에 개인 홈 경로를 남기지 않게).
    """
    plan = dict(plan)
    # 이미지 ID(sha256:…) 그대로: D5 DockerRunner·사전점검·Host 도구 설정이 모두 이 형식을 받고, 로컬 이미지를 가리킨다.
    plan['image'] = image_id
    plan['hardwareProfile'] = hardware
    plan['source'] = {**(plan.get('source') or {}), 'repositoryPath': repository_path, 'commitHash': commit_hash,
                      'lockPath': lock_path}
    return plan


def configure():
    from .experiment import configure as experiment_configure, starting_source_files
    from .starting_source import build as build_source
    image = json.loads((PREPARED / 'image.json').read_text(encoding='utf-8'))
    lock = PREPARED / 'requirements.lock'
    if image['lockSha256'] != 'sha256:' + hashlib.sha256(lock.read_bytes()).hexdigest():
        raise EnvironmentError_('LOCK_CHANGED_AFTER_IMAGE_BUILD')
    if _image_id(image['imageTag']) != image['imageId']:
        raise EnvironmentError_('IMAGE_CHANGED_AFTER_BUILD')
    check_bandit_rules(image['imageId'])
    hardware = hardware_profile()
    example = json.loads((CONFIGS / 'owned-tools.example.json').read_text(encoding='utf-8'))
    tools = tools_document(image_id=image['imageId'], lock_file=os.path.relpath(lock, CONFIGS),
                           lock_hash=image['lockSha256'], hardware_profile=hardware,
                           unit_scopes=example['unit']['scopes'])
    _write_json(CONFIGS / 'owned-tools.local.json', tools)
    source_config = CONFIGS / 'owned-source.local.json'
    if not source_config.exists():
        configuration, lock_hash = build_source(SKELETON, lock, STARTING_REPOSITORY)
        if lock_hash != image['lockSha256']:
            raise EnvironmentError_('STARTING_SOURCE_LOCK_MISMATCH')
        _write_json(source_config, configuration)
    source = json.loads(source_config.read_text(encoding='utf-8'))
    starting = starting_source_files(source, source_config.parent)
    if starting.get('requirements.lock') != lock.read_bytes():
        raise EnvironmentError_('STARTING_SOURCE_LOCK_MISMATCH')
    for name, document in experiment_configure(tools, contract=_contract(), starting_files=starting,
                                               base=CONFIGS).items():
        _write_json(CONFIGS / 'experiment' / name, document)
    commit = source.get('commitHash') or source.get('commit_hash')
    for path in PLANS:
        _write_json(path, update_plan(json.loads(path.read_text(encoding='utf-8')), image_id=image['imageId'],
                                      hardware=hardware, commit_hash=commit, lock_path=source['lockPath'],
                                      repository_path=os.path.relpath(source['repositoryPath'], path.parent)))
    from .preflight import main as preflight_main
    return {path.name: preflight_main(['--plan', str(path)]) for path in PLANS}


def _contract():
    from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario
    return get_scenario(SCN_001_ID).planner_contract()


def _write_json(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def status():
    """준비 단계별 상태. 네트워크·모델을 쓰지 않고 파일·로컬 이미지 존재만 본다(API 키 값은 읽지 않음)."""
    def image_present(reference):
        try:
            return bool(_image_id(reference))
        except EnvironmentError_:
            return False
    wheels = sorted((PREPARED / 'wheelhouse').glob('*.whl')) if (PREPARED / 'wheelhouse').is_dir() else []
    browsers = (PREPARED / 'ms-playwright').is_dir() and any((PREPARED / 'ms-playwright').iterdir())
    rows = {
        'pythonBaseImage(local)': image_present(PYTHON_IMAGE),
        'wheelhouse': len(wheels), 'requirementsLock': (PREPARED / 'requirements.lock').is_file(),
        'playwrightBrowsers': browsers, 'aptPackageList': (PREPARED / 'apt-dry-run.txt').is_file(),
        'sandboxBaseImage': image_present(f'{BASE_NAME}:{TAG}'), 'sandboxImage': image_present(f'{IMAGE_NAME}:{TAG}'),
        'toolsConfig': (CONFIGS / 'owned-tools.local.json').is_file(),
        'ver1Tools': (CONFIGS / 'experiment/ver1/owned-tools.local.json').is_file(),
        'ver2Tools': (CONFIGS / 'experiment/ver2/owned-tools.local.json').is_file(),
        'ver1ProtectedCases': (CONFIGS / 'experiment/ver1/owned-protected-cases.local.json').is_file(),
        'startingSourceConfig': (CONFIGS / 'owned-source.local.json').is_file(),
        'runtimeConfigs': all((CONFIGS / name).is_file() for name in ('owned-runtime.local.json', 'single-runtime.local.json')),
        'priceBook': (CONFIGS / 'owned-price-book.scn001.json').is_file(),
        'apiKeyEnvSet(A2A_PLATFORM_LLM_API_KEY)': bool(os.environ.get('A2A_PLATFORM_LLM_API_KEY')),
    }
    for path in PLANS:
        plan = json.loads(path.read_text(encoding='utf-8'))
        rows[path.name] = {key: plan.get(key) is not None for key in ('image', 'hardwareProfile')} | {
            'commitHash': (plan.get('source') or {}).get('commitHash') is not None}
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m evaluation.environment', description=__doc__.split('\n')[0])
    parser.add_argument('command', choices=('status', 'wheels', 'browsers', 'image', 'configure', 'all'))
    parser.add_argument('--allow-network', action='store_true', help='PyPI·Playwright CDN·Debian 저장소에서 받기를 허락한다')
    args = parser.parse_args(argv)
    try:
        if args.command == 'status':
            print(json.dumps(status(), ensure_ascii=False, indent=2))
            return 0
        if args.command in ('wheels', 'browsers', 'image', 'all') and not args.allow_network:
            raise EnvironmentError_('NETWORK_STEP_REQUIRES_--allow-network')
        steps = {'wheels': (fetch_wheels,), 'browsers': (fetch_browsers,), 'image': (build_image,),
                 'configure': (configure,), 'all': (fetch_wheels, fetch_browsers, build_image, configure)}[args.command]
        for step in steps:
            result = step()
            if result is not None:
                print(json.dumps(result, ensure_ascii=False, indent=2))
    except EnvironmentError_ as error:
        sys.stderr.write(error.code + '\n')
        return 1
    except Exception as error:
        # 시작 저장소·실험 설정 단계의 정해진 오류 코드(예: OUTPUT_NOT_EMPTY)는 그대로 알린다.
        code = getattr(error, 'code', None)
        if not isinstance(code, str):
            raise
        sys.stderr.write(code + '\n')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
