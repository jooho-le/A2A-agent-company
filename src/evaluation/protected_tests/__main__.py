# 팀장(Host 운영자)에게 넘길 보호 테스트 설정 파일을 만든다.
#   python -m evaluation.protected_tests export --out <폴더> [--suite-ref <참조>] [--contract <시나리오 JSON>] [--force]
# 만드는 파일
#   owned-qa-cases.local.json  dev configs/owned-qa-cases.local.json에 그대로 쓰는 순환 안 보호 QA 케이스(5개)
#   unit-protected-scope.json  dev configs/owned-tools.local.json의 "unit" → "scopes" 목록에 추가할 PROTECTED 범위 한 개
#   handoff.json               묶음 이름·버전·지문(suite SHA-256)과 suite 참조. RunConfiguration의 protected_test_suite_ref를 이 값과 맞춘다
# 시나리오 정책을 따로 주지 않으면 dev에 등록된 SCN-001 정책(scenario_registry)을 쓴다. 이미 있는 파일은 --force 없이 덮어쓰지 않는다.
#
# [처음 읽는 분을 위한 용어]
# - suite 참조(suite_ref): 보호 테스트 묶음을 가리키는 이름표. Run 설정(RunConfiguration)에 같은 값을 적어 두어야
#   dev가 "이 Run은 이 보호 묶음을 쓴다"고 맞춰 볼 수 있다.
# - 시나리오 정책(contract): 요구사항 이름(REQ-001)과 요구사항 UUID를 짝지어 둔 dev 문서.
import argparse  # 명령줄 옵션(--out 등) 읽기
import json  # 설정 파일을 JSON으로 쓰기·읽기
import sys  # 종료 코드 돌려주기
from pathlib import Path  # 파일 경로 다루기

from . import load_suite, owned_qa_cases, suite_sha256, unit_test_scope  # 같은 폴더 __init__.py의 함수들

DEFAULT_SUITE_REF = 'artifact://protected-tests/scn-001/signup-{version}'  # --suite-ref가 없을 때 쓰는 기본 참조({version}은 묶음 버전)


def _contract(path):
    # 시나리오 정책을 읽는다. 파일 경로를 주면 그 JSON을, 없으면 dev에 등록된 SCN-001 정책을 쓴다.
    # dev 모듈은 필요할 때만 불러온다(파일을 주는 경우에는 dev 코드 없이도 동작하게).
    if path:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario
    return get_scenario(SCN_001_ID).planner_contract()


def export(out: Path, *, suite_ref=None, contract=None, force=False) -> dict:
    # 세 파일을 out 폴더에 쓰고 {파일 이름: 경로}를 돌려준다. 내용 확인(dev 검증기 통과)은 tests/evaluation에서 한다.
    suite = load_suite()
    suite_ref = suite_ref or DEFAULT_SUITE_REF.format(version=suite['suiteVersion'])
    documents = {
        'owned-qa-cases.local.json': owned_qa_cases(contract, suite),
        'unit-protected-scope.json': unit_test_scope('in-loop', suite_ref=suite_ref, suite=suite),
        'handoff.json': {'suiteId': suite['suiteId'], 'suiteVersion': suite['suiteVersion'],
                         'inLoopSuiteSha256': suite_sha256('in-loop', suite),
                         'finalSuiteSha256': suite_sha256('final', suite), 'protectedSuiteRef': suite_ref,
                         'note': 'unit-protected-scope.json을 owned-tools.local.json의 unit.scopes에 추가하고, '
                                 'RunConfiguration의 protected_test_suite_ref를 protectedSuiteRef와 같게 둔다.'},
    }
    # 폴더를 만들고 파일을 쓴다. 같은 이름 파일이 있으면 --force 없이는 멈춘다(실수로 덮어쓰지 않게).
    out.mkdir(parents=True, exist_ok=True)
    written = {}
    for name, document in documents.items():
        path = out / name
        if path.exists() and not force:
            raise FileExistsError(f'{path} already exists (use --force)')
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        written[name] = path
    return written


def main(argv=None):
    # 명령줄 입구. "export" 명령 하나만 있다. 만든 파일 이름과 경로를 출력하고 0을 돌려준다.
    parser = argparse.ArgumentParser(prog='python -m evaluation.protected_tests')
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('export', help='팀장 전달용 보호 테스트 설정 파일 만들기')
    command.add_argument('--out', required=True, type=Path)
    command.add_argument('--suite-ref')
    command.add_argument('--contract', help='시나리오 정책 JSON 파일(생략하면 dev 등록 SCN-001)')
    command.add_argument('--force', action='store_true')
    options = parser.parse_args(argv)
    written = export(options.out, suite_ref=options.suite_ref, contract=_contract(options.contract), force=options.force)
    for name, path in written.items():
        print(f'{name}: {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
