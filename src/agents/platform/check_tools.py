"""Check Host tool policy and lock bytes, never Docker or product success."""

import argparse
import json
import sys

from agents.platform.tool_configuration import ToolConfigurationError, load_tool_configuration


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ToolConfigurationError()


def main(argv=None):
    parser = _Parser(description="Check operator tool configuration offline; no Docker/API/build/test/scan.")
    parser.add_argument("--file", required=True)
    try:
        options = parser.parse_args(argv)
        load_tool_configuration(options.file)
        sys.stdout.write(json.dumps({
            "status": "TOOL_CONFIGURATION_VALID", "executionReady": False, "DockerChecked": False,
        }, separators=(",", ":")) + "\n")
        return 0
    except ToolConfigurationError as error:
        sys.stderr.write(error.code + "\n")
        return 1
    except Exception:
        sys.stderr.write("TOOL_CONFIGURATION_INVALID\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
