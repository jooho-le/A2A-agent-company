"""Offline configuration check, deliberately separate from the execution CLI."""

import argparse
import json
from pathlib import Path
import sys

from agents.platform.configuration import (
    RuntimeConfigurationError, load_runtime_configuration, resolve_runtime_configuration,
)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Standard argparse errors may echo submitted secrets or filenames.
        raise RuntimeConfigurationError()


def main(argv=None):
    parser = _Parser(description="Validate trusted Host settings without starting any Agent or calling APIs.")
    parser.add_argument("--file", required=True, help="Operator-selected JSON configuration file.")
    parser.add_argument("--check-secrets", action="store_true", help="Also check referenced process environment credentials locally.")
    try:
        options = parser.parse_args(argv)
        path = Path(options.file).absolute()
        configuration = load_runtime_configuration(path)
        if options.check_secrets:
            resolve_runtime_configuration(configuration, base_directory=path.parent)
        sys.stdout.write(json.dumps({
            "status": "RUNTIME_CONFIGURATION_VALID",
            "credentialCheck": "PRESENT" if options.check_secrets else "NOT_REQUESTED",
            "executionReady": False,
        }, separators=(",", ":")) + "\n")
        return 0
    except RuntimeConfigurationError as error:
        sys.stderr.write(error.code + "\n")
        return 1
    except Exception:
        sys.stderr.write("RUNTIME_CONFIGURATION_INVALID\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
