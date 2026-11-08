"""Run: PYTHONPATH=src .venv/bin/python -m diagnostics.communication."""

import argparse
import asyncio
import json
import signal

import uvicorn

from diagnostics.communication.contracts import CheckOptions, SCENARIOS
from diagnostics.communication.lab import EmbeddedServer, create_dashboard, running_lab


async def run(arguments):
    async with running_lab() as lab:
        if arguments.smoke:
            outputs = []
            for scenario in SCENARIOS:
                options = CheckOptions(scenario=scenario, rounds=1, processing_ms=10)
                report = await lab.execute(options, lambda event: None)
                detected = report["summary"]["issueCount"]
                expected_fault = scenario not in {"normal", "slow_sender", "slow_agent"}
                valid = detected > 0 if expected_fault else detected == 0 and report["summary"]["responses"] == 4
                outputs.append({"scenario": scenario, "valid": valid, "summary": report["summary"],
                                "timings": [{"role": row["role"], **row["timings"]} for row in report["rows"]]})
            print(json.dumps(outputs, ensure_ascii=False, indent=2))
            return 0 if all(item["valid"] for item in outputs) else 1
        print(f"\nA2A 통신 검증실: http://127.0.0.1:{arguments.port}\n실제 로컬 HTTP + 목업 실행기 / API 키·Docker 불필요 / 종료 Ctrl+C\n", flush=True)
        server = EmbeddedServer(uvicorn.Config(create_dashboard(lab, arguments.port), host="127.0.0.1",
                                               port=arguments.port, log_level="warning", access_log=False,
                                               timeout_graceful_shutdown=5))
        loop = asyncio.get_running_loop()
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}

        def request_exit():
            if server.should_exit:
                server.force_exit = True
            server.should_exit = True

        for sig in previous:
            loop.add_signal_handler(sig, request_exit)
        try:
            await server.serve()
        finally:
            for sig, handler in previous.items():
                loop.remove_signal_handler(sig)
                signal.signal(sig, handler)
        return 0


def main():
    parser = argparse.ArgumentParser(description="프로젝트 A2A 클라이언트·서버 통신 검증실")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--smoke", action="store_true", help="모든 진단 시나리오를 실제 HTTP로 검사하고 종료")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port는 1024~65535여야 합니다.")
    try:
        raise SystemExit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
