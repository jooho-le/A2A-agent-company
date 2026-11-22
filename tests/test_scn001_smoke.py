"""Offline checks for credentials, frozen inputs and one-run isolation."""
import importlib.util
import json
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "scn001_smoke", Path(__file__).resolve().parents[1] / "scripts/run_scn001_smoke.py")
smoke = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(smoke)


def test_key_alias_stays_in_child_environment_and_env_file_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("A2A_PLATFORM_LLM_API_KEY", raising=False)
    path = tmp_path / ".env"
    path.write_text("OPENAI_API_KEY=dummy-offline-test-key\n")
    before = path.read_bytes()
    result = smoke.runtime_environment(tmp_path)
    assert result["A2A_PLATFORM_LLM_API_KEY"] == "dummy-offline-test-key"
    assert len(result["A2A_HOST_API_TOKEN"]) >= 32
    assert path.read_bytes() == before
    assert "A2A_PLATFORM_LLM_API_KEY" not in smoke.os.environ


def test_explicit_process_credential_wins_over_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("OPENAI_API_KEY=dummy-file-key\n")
    monkeypatch.setenv("A2A_PLATFORM_LLM_API_KEY", "dummy-process-key")
    assert smoke.runtime_environment(tmp_path)["A2A_PLATFORM_LLM_API_KEY"] == "dummy-process-key"


def test_missing_key_stops_before_host_or_submission(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("A2A_PLATFORM_LLM_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY_MISSING"):
        smoke.runtime_environment(tmp_path)


def test_prepare_pins_current_practice_inputs_and_separate_storage(tmp_path):
    runtime_path = smoke.ROOT / "configs/experiment/ver1/owned-runtime.local.json"
    if not runtime_path.is_file():
        pytest.skip("operator local runtime not configured")
    before = runtime_path.read_bytes()
    out = tmp_path / "new-run"
    runtime, tools, body, _ = smoke.prepare(out, runtime_seconds=120)
    assert runtime_path.read_bytes() == before
    assert runtime["orchestrator"]["databasePath"] == str(out / "host.sqlite3")
    assert runtime["runtimeBudgetMs"] == body["configuration"]["limits"]["runtimeBudgetMs"] == 120_000
    assert body["requestText"] == "이메일과 비밀번호로 회원가입 기능을 만들어줘."
    assert runtime["model"]["modelId"] == body["configuration"]["model"]["modelId"]
    scopes = [s for s in tools["unit"]["scopes"] if s["kind"] == "PROTECTED"]
    assert len(scopes) == 2
    assert all("exam_final.py" not in " ".join(s["protected_files"]) for s in scopes)
    assert not (out / "submission.json").exists()
    assert not (out / "host.sqlite3").exists()
    request_bytes = (out / "request.json").read_bytes()
    assert b"Authorization" not in request_bytes and b"OPENAI_API_KEY" not in request_bytes
    with pytest.raises(FileExistsError):
        smoke.prepare(out)
    assert (out / "request.json").read_bytes() == request_bytes
