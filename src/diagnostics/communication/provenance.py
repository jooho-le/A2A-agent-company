"""Selected execution-source fingerprints for a bounded diagnostic capture."""

import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import subprocess


PROJECT_ROOT = Path(__file__).resolve().parents[3]
EXECUTION_PATHS = tuple("src/diagnostics/communication/" + name + ".py" for name in (
    "natural", "live", "lab", "probe", "contracts", "performance", "timing", "peer_sessions", "workloads", "provenance")) + (
    "src/agents/llm/openai_responses.py", "src/agents/llm/contracts.py", "src/agents/main.py",
    "src/agents/api/routes.py", "src/agents/api/handler.py", "src/agents/api/sqlite_task_store.py",
    "src/agents/api/validation.py", "src/agents/api/task_store.py", "src/agents/core/config.py",
    "src/orchestrator/a2a/client.py", "src/orchestrator/a2a/requests.py", "src/orchestrator/a2a/delivery.py",
    "src/orchestrator/domain/json_fingerprint.py", "pyproject.toml", "uv.lock")


def source_hashes(root=PROJECT_ROOT):
    invalid = False
    try:
        hashes = {}
        for name in EXECUTION_PATHS:
            path = root / name
            if path.is_symlink():
                raise ValueError
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        invalid = True
    if invalid:
        raise ValueError("DIAGNOSTIC_SOURCE_MANIFEST_UNAVAILABLE") from None
    return hashes


def _git(root, arguments):
    try:
        result = subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True, timeout=3, check=True)
        return result.stdout.strip()
    except Exception:
        return None


def capture_provenance(runtime, *, root=PROJECT_ROOT):
    sources = source_hashes(root)
    revision = _git(root, ["rev-parse", "HEAD"])
    status = _git(root, ["status", "--porcelain", "--untracked-files=normal"])
    packages = {}
    for name in ("a2a-sdk", "httpx", "pydantic", "jsonschema"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {"schemaVersion": 1, "scope": "SELECTED_EXECUTION_FILES_NOT_FULL_ENVIRONMENT",
        "gitCommit": revision, "worktreeDirty": None if status is None else bool(status),
        "pythonVersion": platform.python_version(), "packages": packages,
        "transportMode": "DEFAULT_HTTPS_TRANSPORT" if runtime.transport_factory is None else "CUSTOM_TRANSPORT",
        "sources": sources, "sourceManifestSha256": hashlib.sha256(json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "sourceStability": "UNVERIFIED", "changedSourceFiles": [],
        "caseOrder": ["01-blog", "02-documents", "03-mystery"],
        "architectureOrder": ["MULTI_AGENT", "SINGLE_AGENT"], "explicitWarmupCalls": 0,
        "providerCacheState": "UNVERIFIED", "comparison": "DESCRIPTIVE_NOT_CAUSAL"}


def finish_provenance(provenance, *, root=PROJECT_ROOT):
    try:
        after = source_hashes(root)
    except ValueError:
        provenance["sourceStability"] = "UNVERIFIED"
        return
    changed = sorted(name for name, value in provenance["sources"].items() if after.get(name) != value)
    provenance["changedSourceFiles"] = changed
    provenance["sourceStability"] = "CHANGED" if changed else "STABLE"
