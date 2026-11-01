# 보호 테스트가 생성물 코드의 어느 줄을 실제로 지나갔는지 기록하는 실행기(D5 최종 평가 전용, 판정에는 쓰지 않음).
# 실행 검사는 지나간 코드만 보므로, PASS여도 "보지 못한 줄"이 얼마나 남았는지 드러내려고 쓴다.
# dev의 신뢰된 unittest 실행기(_unit_runner.py, 같은 폴더)를 그대로 부르고, 그 동안 파이썬 기본 추적 기능
# (sys.settrace·threading.settrace)으로 소스 폴더 안 파일의 실행 줄만 모은다. 표준 라이브러리만 쓴다(컨테이너 안에서 돈다).
#   컨테이너: python -I -B /inputs/_coverage_runner.py --kind PROTECTED --directory tests/signup --pattern exam_final.py
# 출력: 줄 두 개. 앞 줄은 {"coverage-v1": {...}}, 마지막 줄은 dev 실행기와 같은 unittest-v1 보고서(또는 오류).
#
# [처음 읽는 분을 위한 용어]
# - 실행 가능한 줄: 파이썬이 코드로 바꾼 줄(빈 줄·주석 제외). 컴파일한 코드 객체의 줄 표(co_lines)에서 얻는다.
# - 추적 함수(trace): 파이썬이 함수 호출·줄 실행마다 불러 주는 함수. 소스 폴더 밖 코드는 추적하지 않아 느려지지 않게 한다.
import importlib.util
import json
import os
import sys
import threading
import types
from pathlib import Path

MAX_FILES = 512          # 기록할 소스 파일 수 한도
MAX_FILE_BYTES = 1 << 20  # 줄 수를 셀 파일 하나의 크기 한도(1MiB)


def _dev_runner():
    # 같은 폴더의 dev 실행기(_unit_runner.py)를 불러온다.
    spec = importlib.util.spec_from_file_location("trusted_unit_runner", Path(__file__).with_name("_unit_runner.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _executable_lines(path):
    # 파일의 실행 가능한 줄 번호들. 읽거나 컴파일할 수 없으면 None.
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        code = compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
    except (OSError, SyntaxError, ValueError):
        return None
    lines, stack = set(), [code]
    while stack:
        item = stack.pop()
        lines.update(line for _start, _end, line in item.co_lines() if line is not None)
        stack.extend(const for const in item.co_consts if isinstance(const, types.CodeType))
    return lines


def run_tests_with_coverage(test_root, source_root, pattern, runner=None):
    # dev run_tests를 추적하며 실행한다. 돌려주는 값: (보고서 또는 None, 종료 코드, 줄 기록)
    runner = runner or _dev_runner()
    source = os.path.abspath(str(source_root)) + os.sep
    executed = {}

    def local(frame, event, arg):
        if event == "line":
            executed.setdefault(frame.f_code.co_filename, set()).add(frame.f_lineno)
        return local

    def tracer(frame, event, arg):
        # 소스 폴더 안 코드의 함수만 줄 단위로 따라간다.
        if frame.f_code.co_filename.startswith(source):
            executed.setdefault(frame.f_code.co_filename, set()).add(frame.f_lineno)
            return local
        return None

    threading.settrace(tracer)
    sys.settrace(tracer)
    try:
        report, code = runner.run_tests(test_root, source_root, pattern)
    finally:
        sys.settrace(None)
        threading.settrace(None)
    files = {}
    for path in sorted(Path(source).rglob("*.py"))[:MAX_FILES]:
        lines = _executable_lines(path)
        if lines is None:
            continue
        hit = executed.get(str(path), set()) & lines
        files[path.relative_to(source).as_posix()] = {
            "lines": len(lines), "executed": len(hit), "missing": sorted(lines - hit)}
    return report, code, {"files": files}


def main(argv=None):
    # dev 실행기와 같은 명령줄(--kind/--directory/--pattern)을 받아 컨테이너의 /inputs·/snapshot으로 실행한다.
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--kind", choices=("PROTECTED",), required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--pattern", required=True)
    coverage = None
    try:
        options = parser.parse_args(argv)
        runner = _dev_runner()
        with runner._discard_file_descriptors():  # 테스트 중 os.write(1·2) 출력이 결과 줄에 섞이지 않게
            report, code, coverage = run_tests_with_coverage(Path("/inputs") / options.directory, Path("/snapshot"),
                                                             options.pattern, runner)
    except BaseException:
        report, code = None, 2
    sys.stdout.write(json.dumps({"coverage-v1": coverage}, separators=(",", ":")) + "\n")
    sys.stdout.write(json.dumps(report if report is not None else {"error": "TEST_RUNNER_ERROR"},
                                ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
