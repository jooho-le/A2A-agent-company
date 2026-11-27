"""로컬 서비스 실행. 비밀값은 프로세스 환경에만 전달하며 출력하지 않습니다."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from dotenv import dotenv_values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=("product", "host", "check-host"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    environment = {**{k: v for k, v in dotenv_values(root / ".env").items() if v is not None}, **os.environ}
    os.chdir(root)
    if args.service == "product":
        if not environment.get("JWT_SECRET") or not environment.get("A2A_HOST_API_TOKEN"):
            raise SystemExit("JWT_SECRET and A2A_HOST_API_TOKEN are required.")
        executable = root / "backend/.venv/bin/python"
        subprocess.run([str(executable), "backend/project_schema.py"], env=environment, check=True)
        command = [str(executable), "-m", "uvicorn", "main:app", "--app-dir", "backend",
                   "--host", "127.0.0.1", "--port", "8000"]
    else:
        runtime = root / "configs/owned-runtime.local.json"
        key_name = json.loads(runtime.read_text())["apiKeyEnv"]
        if not environment.get(key_name) and environment.get("OPENAI_API_KEY"):
            environment[key_name] = environment["OPENAI_API_KEY"]
        environment["PYTHONPATH"] = str(root / "src")
        executable = root / ".venv/bin/python"
        command = [str(executable), "-m", "agents.platform.run_host",
            "--runtime", str(runtime), "--tools", str(root / "configs/owned-tools.local.json"),
            "--source", str(root / "configs/owned-source.local.json"),
            "--check-secrets" if args.service == "check-host" else "--start"]
    os.execve(executable, command, environment)


if __name__ == "__main__":
    main()
