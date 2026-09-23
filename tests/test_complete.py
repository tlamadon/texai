from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from texai import complete, server
from texai.complete import CompletionUnavailable
from texai.config import AppConfig

PDF_BYTES = b"%PDF-1.4\n%%EOF\n"


@pytest.fixture()
def config(tmp_path: Path) -> AppConfig:
    root = tmp_path / "project"
    (root / "sections").mkdir(parents=True)
    (root / "build").mkdir()
    (root / "sections" / "model.tex").write_text("\\section{Model}\n")
    (root / "build" / "main.pdf").write_bytes(PDF_BYTES)
    return AppConfig.create(root.resolve(), root.resolve() / "build" / "main.pdf")


def client(config: AppConfig) -> TestClient:
    return TestClient(server.create_app(config), base_url="http://127.0.0.1")


# ---------------------------------------------------------------- helpers


def test_clean_strips_a_fenced_block():
    assert complete.clean("```latex\n\\alpha\n```") == "\\alpha"


def test_clean_keeps_a_leading_space_but_drops_trailing_blank_lines():
    # A continuation mid-sentence needs its leading space; the trailing newline
    # the model likes to add is noise.
    assert complete.clean(" and then\n\n") == " and then"


def test_completion_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TEXAI_AUTOCOMPLETE", raising=False)
    ok, reason = complete.completion_status()
    assert ok is False
    assert reason == complete.DISABLED_HINT


def test_completion_gate_opens_with_the_env_flag(monkeypatch):
    monkeypatch.setenv("TEXAI_AUTOCOMPLETE", "1")
    # Past the gate it depends on the SDK and credentials, but it is no longer
    # the "off by default" refusal.
    _, reason = complete.completion_status()
    assert reason != complete.DISABLED_HINT


def test_build_window_bounds_around_the_cursor(monkeypatch):
    monkeypatch.setattr(complete, "PREFIX_WINDOW", 3)
    monkeypatch.setattr(complete, "SUFFIX_WINDOW", 2)
    prefix, suffix = complete.build_window("0123456789", 5)
    assert prefix == "234"
    assert suffix == "56"


# ---------------------------------------------------------------- endpoint


def test_complete_returns_the_suggestion(config: AppConfig, monkeypatch):
    async def fake(file, text, offset):
        assert offset == 4
        return " world"

    monkeypatch.setattr(server, "complete_text", fake)
    resp = client(config).post(
        "/api/complete", json={"file": "sections/model.tex", "text": "hell", "offset": 4}
    )
    assert resp.status_code == 200
    assert resp.json() == {"text": " world"}


def test_complete_reports_unavailable_as_503(config: AppConfig, monkeypatch):
    async def fake(file, text, offset):
        raise CompletionUnavailable("no credentials")

    monkeypatch.setattr(server, "complete_text", fake)
    resp = client(config).post(
        "/api/complete", json={"file": "sections/model.tex", "text": "x", "offset": 1}
    )
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "completion_unavailable"


def test_complete_rejects_path_traversal(config: AppConfig):
    resp = client(config).post(
        "/api/complete", json={"file": "../escape.tex", "text": "x", "offset": 1}
    )
    assert resp.status_code == 422


def test_info_reports_completion_availability(config: AppConfig, monkeypatch):
    monkeypatch.setattr(server, "completion_status", lambda: (True, None))
    info = client(config).get("/api/info").json()
    assert info["completion"] == {"available": True, "reason": None}
