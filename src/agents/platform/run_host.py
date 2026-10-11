"""Safe-by-default CLI: inspect approved files unless --start is explicit."""

import argparse
import asyncio
import json
import math
import sys

from agents.platform.configuration import resolve_runtime_configuration
from agents.platform.host import HostFactoryError, create_host, inspect_host_configuration
from agents.platform.runner import PlatformRunnerError, run_platform


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise HostFactoryError()


async def _start(options):
    platform = await create_host(runtime_file=options.runtime, tools_file=options.tools,
        source_file=options.source, protected_cases_file=options.qa_cases)
    failure = None
    try:
        await run_platform(platform, startup_timeout_seconds=options.startup_timeout,
            shutdown_timeout_seconds=options.shutdown_timeout)
    except BaseException as error:
        failure = error
        raise
    finally:
        # Runner also closes; the platform's once-only owner makes this safe
        # even if argument validation/listener admission fails before lifespans.
        try:
            await platform.aclose()
        except Exception:
            if failure is None:
                raise PlatformRunnerError("PLATFORM_CLEANUP_FAILED") from None
            # A failed existing close_task must not replace the Runner's
            # original safe error (or cancellation) with initialization error.


def main(argv=None):
    parser = _Parser(description="Check approved Host configuration; explicitly --start to allocate/run all five apps.")
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--tools", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--qa-cases")
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--check-secrets", action="store_true")
    parser.add_argument("--startup-timeout", type=float, default=30)
    parser.add_argument("--shutdown-timeout", type=float, default=60)
    try:
        options = parser.parse_args(argv)
        if any(not math.isfinite(value) or not .01 <= value <= maximum for value, maximum in (
                (options.startup_timeout, 300), (options.shutdown_timeout, 600))) or options.start and options.check_secrets:
            raise HostFactoryError()
        if options.start:
            asyncio.run(_start(options))
        else:
            plan = inspect_host_configuration(runtime_file=options.runtime, tools_file=options.tools,
                source_file=options.source, protected_cases_file=options.qa_cases)
            if options.check_secrets:
                try:
                    resolve_runtime_configuration(plan.runtime, base_directory=plan.runtime_directory)
                except Exception:
                    raise HostFactoryError() from None
            sys.stdout.write(json.dumps({"status": "OWNED_HOST_CONFIGURATION_VALID",
                "credentialCheck": "PRESENT" if options.check_secrets else "NOT_REQUESTED",
                "executionReady": False, "DockerChecked": False, "workspacePrepared": False}, separators=(",", ":")) + "\n")
        return 0
    except (HostFactoryError, PlatformRunnerError) as error:
        sys.stderr.write(error.code + "\n")
        return 1
    except (KeyboardInterrupt, asyncio.CancelledError):
        return 130
    except Exception:
        sys.stderr.write("OWNED_HOST_INITIALIZATION_FAILED\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
