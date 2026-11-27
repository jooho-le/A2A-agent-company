"""Opt-in owned Host CLI; a trusted factory is always explicitly required."""

import argparse
import asyncio
import inspect
import sys

from agents.platform.composition import OwnedAgentPlatform
from agents.platform.runner import PlatformRunnerError, load_platform_factory, run_platform


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal error includes submitted option/argument text.
        raise PlatformRunnerError("PLATFORM_FACTORY_INVALID")


async def _run(specification, startup, shutdown):
    invalid = False
    try:
        factory = load_platform_factory(specification)
        platform = factory()
        if type(platform) is not OwnedAgentPlatform:
            if inspect.iscoroutine(platform):
                platform.close()
            raise ValueError
    except (Exception, SystemExit):
        invalid = True
    if invalid:
        raise PlatformRunnerError("PLATFORM_FACTORY_INVALID") from None
    await run_platform(platform, startup_timeout_seconds=startup, shutdown_timeout_seconds=shutdown)


def main(argv=None):
    parser = _Parser(description="Run one explicitly configured Orchestrator and four Agents in one Host process.")
    parser.add_argument("--factory", required=True,
                        help="Trusted operator module:callable returning OwnedAgentPlatform (no filepath or model input).")
    parser.add_argument("--startup-timeout", type=float, default=30)
    parser.add_argument("--shutdown-timeout", type=float, default=60)
    try:
        options = parser.parse_args(argv)
        asyncio.run(_run(options.factory, options.startup_timeout, options.shutdown_timeout))
    except PlatformRunnerError as error:
        sys.stderr.write(error.code + "\n")
        return 1
    except KeyboardInterrupt:
        # SIGINT is normally consumed by the coordinated graceful handler.
        return 130
    except Exception:
        sys.stderr.write("PLATFORM_STARTUP_FAILED\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
