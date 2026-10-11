"""Read-only approved Source validation; no Workspace/DB/Docker/model writes."""

import argparse
import json
from pathlib import Path
import sys

from agents.platform.starting_source import SourcePreparationError, load_source_configuration, verify_starting_source
from agents.platform.tool_configuration import ToolConfigurationError, load_tool_configuration


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SourcePreparationError()


def main(argv=None):
    parser = _Parser(description="Read exact local Git baseline/lock only; do not prepare or execute a Run.")
    parser.add_argument("--file", required=True)
    parser.add_argument("--tools", required=True)
    try:
        options = parser.parse_args(argv)
        path = Path(options.file).absolute()
        tools = load_tool_configuration(options.tools)
        verify_starting_source(load_source_configuration(path), base_directory=path.parent, environment=tools.baseline)
        sys.stdout.write(json.dumps({
            "status": "SOURCE_BASELINE_VALID", "executionReady": False, "workspacePrepared": False,
        }, separators=(",", ":")) + "\n")
        return 0
    except (SourcePreparationError, ToolConfigurationError) as error:
        sys.stderr.write(error.code + "\n")
        return 1
    except Exception:
        sys.stderr.write("SOURCE_CONFIGURATION_INVALID\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
