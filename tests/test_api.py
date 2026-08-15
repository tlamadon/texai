import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from texai.config import AppConfig
from texai.paths import PathOutsideRootError
from texai.server import create_app, normalize_selected_text, pdf_version_tag
from texai.synctex import (
    SyncTexDataMissing,
    SyncTexExecutableMissing,
    SyncTexLocation,
    SyncTexNoResult,
)

PDF_BYTES = b"%PDF-1.4\n%%EOF\n"


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "sections").mkdir(parents=True)
    (root / "build").mkdir()
    (root / "sections" / "model.tex").write_text("\\section{Model}\n")
    (root / "build" / "main.pdf").write_bytes(PDF_BYTES)
    (root / "build" / "main.synctex.gz").write_bytes(b"\x1f\x8b")
    return root.resolve()


@pytest.fixture()
def config(project: Path) -> AppConfig:
    return AppConfig.create(project, project / "build" / "main.pdf")


def make_client(config: AppConfig, runner) -> TestClient:
    # base_url matters: the app only answers to loopback Host headers.
    return TestClient(create_app(config, synctex_runner=runner), base_url="http://127.0.0.1")


def fake_runner(source: str = "sections/model.tex", line: int = 143, column: int = 1):
    calls: list[dict] = []

    def runner(pdf_path, page, x, y, executable="synctex"):
        calls.append({"pdf": pdf_path, "page": page, "x": x, "y": y, "executable": executable})
        return SyncTexLocation(input=source, line=line, column=column)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# ---------------------------------------------------------------- basics


def test_info_and_status(config: AppConfig):
    client = make_client(config, fake_runner())
    info = client.get("/api/info").json()
    assert info["pdf"] == "build/main.pdf"
    assert info["selectionFile"] == ".texai/current-selection.json"

    status = client.get("/api/status").json()
    assert status["exists"] is True
    assert status["pdfVersion"] == info["pdfVersion"] == pdf_version_tag(config.pdf_path)


def test_pdf_is_served_without_caching(config: AppConfig):
    client = make_client(config, fake_runner())
    response = client.get("/api/pdf")
    assert response.status_code == 200
    assert response.content == PDF_BYTES
    assert response.headers["content-type"] == "application/pdf"
    assert "no-store" in response.headers["cache-control"]


def test_pdf_missing_returns_clear_404(config: AppConfig):
    config.pdf_path.unlink()
    client = make_client(config, fake_runner())
    response = client.get("/api/pdf")
    assert response.status_code == 404
    assert response.json()["detail"]["error"] == "pdf_missing"
    assert client.get("/api/status").json() == {"exists": False, "pdfVersion": None}


def test_non_loopback_host_is_rejected(config: AppConfig):
    client = make_client(config, fake_runner())
    response = client.get("/api/info", headers={"Host": "evil.example.com"})
    assert response.status_code == 403
    assert client.get("/api/info", headers={"Host": "127.0.0.1:8765"}).status_code == 200
    assert client.get("/api/info", headers={"Host": "localhost:8765"}).status_code == 200


def test_index_and_static_assets_are_served(config: AppConfig):
    client = make_client(config, fake_runner())
    assert "texai" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    # PDF.js is bundled locally: no CDN needed at runtime.
    for asset in ("pdf.mjs", "pdf.worker.mjs"):
        response = client.get(f"/static/vendor/pdfjs/{asset}")
        assert response.status_code == 200
        # ES modules must be served as JavaScript or the browser refuses them.
        assert "javascript" in response.headers["content-type"]


# ---------------------------------------------------------------- /api/select


def test_select_writes_selection_file(config: AppConfig):
    runner = fake_runner()
    client = make_client(config, runner)

    response = client.post("/api/select", json={"page": 7, "x": 241.3, "y": 418.2})
    assert response.status_code == 200
    body = response.json()
    assert body["message"] == "Selected sections/model.tex:143"
    assert body["source"] == {
        "file": "sections/model.tex",
        "line": 143,
        "column": 1,
        "word": None,
    }

    written = json.loads(config.selection_file.read_text())
    assert written["version"] == 1
    assert written["pdf"] == "build/main.pdf"
    assert written["page"] == 7
    assert written["pdfPosition"] == {"x": 241.3, "y": 418.2}
    assert written["source"] == {
        "file": "sections/model.tex",
        "line": 143,
        "column": 1,
        "word": None,
    }
    assert written["selectedText"] is None
    assert written["updatedAt"].startswith("20")

    assert runner.calls[0]["page"] == 7
    assert runner.calls[0]["pdf"] == config.pdf_path


def test_select_records_rendered_text(config: AppConfig):
    client = make_client(config, fake_runner())
    response = client.post(
        "/api/select",
        json={"page": 1, "x": 10, "y": 20, "selectedText": "  the estimated\n  elasticity  "},
    )
    assert response.status_code == 200
    written = json.loads(config.selection_file.read_text())
    assert written["selectedText"] == "the estimated elasticity"


def test_select_resolves_absolute_synctex_paths(config: AppConfig, project: Path):
    absolute = str(project / "sections" / "model.tex")
    client = make_client(config, fake_runner(source=absolute))
    response = client.post("/api/select", json={"page": 1, "x": 1, "y": 2})
    assert response.json()["source"]["file"] == "sections/model.tex"


@pytest.mark.parametrize(
    "payload",
    [
        {"page": 0, "x": 1, "y": 2},  # pages are 1-based
        {"page": -3, "x": 1, "y": 2},
        {"page": 1.5, "x": 1, "y": 2},
        {"page": "one", "x": 1, "y": 2},
        {"page": 1, "x": -1, "y": 2},  # negative coordinates
        {"page": 1, "x": 1, "y": -2},
        {"page": 1, "x": 1e9, "y": 2},  # off-page
        {"page": 1, "x": 1, "y": 2, "selectedText": "x" * 20_001},
        {"page": 1, "x": 1},  # missing y
        {"x": 1, "y": 2},  # missing page
        {},
        {"page": 1, "x": 1, "y": 2, "extra": "nope"},  # unexpected field
        {"page": 1, "x": None, "y": 2},
    ],
)
def test_select_rejects_invalid_coordinates(config: AppConfig, payload):
    runner = fake_runner()
    client = make_client(config, runner)
    response = client.post("/api/select", json=payload)
    assert response.status_code == 422
    assert runner.calls == []
    assert not config.selection_file.exists()


def test_select_rejects_non_finite_coordinates(config: AppConfig):
    runner = fake_runner()
    client = make_client(config, runner)
    response = client.post(
        "/api/select",
        content=b'{"page": 1, "x": NaN, "y": 2}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert runner.calls == []


def test_select_accepts_boundary_values(config: AppConfig):
    client = make_client(config, fake_runner())
    assert client.post("/api/select", json={"page": 1, "x": 0, "y": 0}).status_code == 200


def test_select_rejects_source_outside_root(config: AppConfig):
    client = make_client(config, fake_runner(source="/usr/share/texmf/article.cls"))
    response = client.post("/api/select", json={"page": 1, "x": 1, "y": 2})
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "source_outside_root"
    assert not config.selection_file.exists()


@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (SyncTexExecutableMissing("synctex not found"), 503, "synctex_missing"),
        (SyncTexDataMissing("no synctex.gz"), 409, "synctex_data_missing"),
        (SyncTexNoResult("nothing there"), 404, "synctex_no_result"),
    ],
)
def test_synctex_failures_map_to_clear_errors(config: AppConfig, exc, status, code):
    def runner(*args, **kwargs):
        raise exc

    client = make_client(config, runner)
    response = client.post("/api/select", json={"page": 1, "x": 1, "y": 2})
    assert response.status_code == status
    assert response.json()["detail"] == {"error": code, "message": str(exc)}


def test_select_on_missing_pdf(config: AppConfig):
    config.pdf_path.unlink()
    runner = fake_runner()
    client = make_client(config, runner)
    response = client.post("/api/select", json={"page": 1, "x": 1, "y": 2})
    assert response.status_code == 404
    assert runner.calls == []


# ---------------------------------------------------------------- /api/selection


def test_get_selection_roundtrip(config: AppConfig):
    client = make_client(config, fake_runner())
    assert client.get("/api/selection").status_code == 404
    client.post("/api/select", json={"page": 2, "x": 3, "y": 4})
    assert client.get("/api/selection").json()["page"] == 2


def test_config_rejects_pdf_outside_root(tmp_path: Path, project: Path):
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(PDF_BYTES)
    with pytest.raises(PathOutsideRootError):
        AppConfig.create(project, outside)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), ("", None), ("   ", None), ("a\n b\tc  ", "a b c")],
)
def test_normalize_selected_text(raw, expected):
    assert normalize_selected_text(raw) == expected


# ---------------------------------------------------------------- chat + turns


def test_info_reports_build_and_agent_state(config: AppConfig):
    client = make_client(config, fake_runner())
    info = client.get("/api/info").json()
    assert info["rootTex"] is None or isinstance(info["rootTex"], str)
    assert isinstance(info["agent"]["available"], bool)
    assert info["agent"]["busy"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {},  # neither a message nor a selection
        {"message": "   ", "selections": []},
        {"message": "hi", "selections": [{"file": "../escape.tex", "line": 1}]},
        {"message": "hi", "selections": [{"file": "/etc/passwd", "line": 1}]},
        {"message": "hi", "selections": [{"file": "a.tex", "line": 0}]},
        {"message": "hi", "selections": [{"file": "", "line": 3}]},
        {"message": "hi", "selections": [{"file": "a.tex"}]},  # no line
        {"message": "hi", "extra": "nope"},
        {"message": "x" * 20_001},
        {"message": "hi", "selections": [{"file": "a.tex", "line": 1, "bogus": 1}]},
    ],
)
def test_chat_rejects_bad_payloads(config: AppConfig, payload):
    client = make_client(config, fake_runner())
    response = client.post("/api/chat", json=payload)
    assert response.status_code == 422


def test_chat_reports_agent_unavailable(config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    """With no SDK installed the composer must say why, not fail obscurely."""
    monkeypatch.setattr(
        "texai.agent.sdk_status", lambda: (False, "The Claude Agent SDK is not installed.")
    )
    client = make_client(config, fake_runner())
    response = client.post("/api/chat", json={"message": "tighten this"})
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "agent_unavailable"


def test_turns_start_empty(config: AppConfig):
    client = make_client(config, fake_runner())
    body = client.get("/api/turns").json()
    assert body == {"busy": False, "turns": []}


def test_unknown_turn_is_404(config: AppConfig):
    client = make_client(config, fake_runner())
    assert client.get("/api/turns/t9999").status_code == 404
    assert client.post("/api/turns/t9999/revert", json={}).status_code == 404


def test_event_route_is_registered(config: AppConfig):
    app = create_app(config, synctex_runner=fake_runner())
    routes = {getattr(route, "path", None) for route in app.routes}
    assert "/api/events" in routes


# ---------------------------------------------------------------- editing


def test_source_files_lists_the_project(config: AppConfig):
    client = make_client(config, fake_runner())
    body = client.get("/api/source/files").json()
    assert "sections/model.tex" in body["files"]
    assert not any(f.endswith(".pdf") for f in body["files"])


def test_source_round_trip(config: AppConfig, project: Path):
    client = make_client(config, fake_runner())

    loaded = client.get("/api/source", params={"file": "sections/model.tex"}).json()
    assert loaded["text"] == "\\section{Model}\n"

    response = client.post(
        "/api/source",
        json={"file": "sections/model.tex", "text": "\\section{Edited}\n", "baseSha": loaded["sha"]},
    )
    assert response.status_code == 200, response.text
    assert (project / "sections" / "model.tex").read_text() == "\\section{Edited}\n"
    # No build command is configured in tests, so the rebuild is a no-op, not a crash.
    assert "build" in response.json()


@pytest.mark.parametrize(
    "file",
    ["../escape.tex", "/etc/passwd", "build/main.pdf"],
)
def test_source_read_refuses_paths_it_should_not_serve(config: AppConfig, file: str):
    client = make_client(config, fake_runner())
    assert client.get("/api/source", params={"file": file}).status_code in (404, 422)


@pytest.mark.parametrize("file", ["../escape.tex", "sections/../../escape.tex"])
def test_source_write_refuses_to_escape_the_root(config: AppConfig, project: Path, file: str):
    client = make_client(config, fake_runner())
    response = client.post("/api/source", json={"file": file, "text": "pwned", "baseSha": None})
    assert response.status_code == 422
    assert not (project.parent / "escape.tex").exists()


def test_source_write_refuses_stale_saves(config: AppConfig, project: Path):
    client = make_client(config, fake_runner())
    client.get("/api/source", params={"file": "sections/model.tex"})
    (project / "sections" / "model.tex").write_text("someone else got here first\n")

    response = client.post(
        "/api/source",
        json={"file": "sections/model.tex", "text": "mine\n", "baseSha": "0" * 16},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "source_conflict"
    assert (project / "sections" / "model.tex").read_text() == "someone else got here first\n"


def test_resolve_maps_a_point_without_recording_it(config: AppConfig):
    client = make_client(config, fake_runner())
    response = client.post("/api/resolve", json={"page": 1, "x": 100.0, "y": 200.0})

    assert response.status_code == 200
    body = response.json()
    assert body["file"] == "sections/model.tex"
    assert (body["line"], body["column"], body["word"]) == (143, 1, None)
    assert "not on any text" in body["why"]  # nothing was clicked to match
    # Unlike /api/select, it leaves no trace on disk.
    assert not config.selection_file.exists()


def test_resolve_rejects_bad_coordinates(config: AppConfig):
    client = make_client(config, fake_runner())
    assert client.post("/api/resolve", json={"page": 0, "x": 1.0, "y": 1.0}).status_code == 422


# ---------------------------------------------------------------- git


def git_repo(root: Path) -> None:
    import subprocess

    def run(*args: str):
        subprocess.run(list(args), cwd=root, capture_output=True, text=True, check=False)

    run("git", "init", "-q", "-b", "main", ".")
    run("git", "config", "user.email", "test@example.com")
    run("git", "config", "user.name", "Test")
    run("git", "config", "commit.gpgsign", "false")
    run("git", "add", "-A")
    run("git", "commit", "-qm", "initial")


def fake_writer(message: str = "Tighten the model section", source: str = "agent"):
    async def writer(config, status, diff, model=None):  # noqa: ARG001
        writer.calls.append({"diff": diff, "dirty": status.dirty})
        return {"message": message, "source": source}

    writer.calls = []  # type: ignore[attr-defined]
    return writer


def test_git_status_without_a_repository(config: AppConfig):
    client = make_client(config, fake_runner())
    body = client.get("/api/git/status").json()
    assert body["repo"] is False
    assert body["dirty"] == 0


def test_git_status_reports_uncommitted_work(config: AppConfig, project: Path):
    git_repo(project)
    (project / "sections" / "model.tex").write_text("\\section{Changed}\n")
    client = make_client(config, fake_runner())

    body = client.get("/api/git/status").json()

    assert body["repo"] is True
    assert body["branch"] == "main"
    assert body["dirty"] == 1
    assert body["files"][0]["path"] == "sections/model.tex"


def test_git_message_asks_the_agent_with_a_diff(config: AppConfig, project: Path):
    git_repo(project)
    (project / "sections" / "model.tex").write_text("\\section{Changed}\n")
    writer = fake_writer()
    client = TestClient(
        create_app(config, synctex_runner=fake_runner(), message_writer=writer),
        base_url="http://127.0.0.1",
    )

    body = client.post("/api/git/message", json={}).json()

    assert body["message"] == "Tighten the model section"
    assert body["source"] == "agent"
    assert len(writer.calls) == 1
    assert "section{Changed}" in writer.calls[0]["diff"]


def test_git_message_needs_something_to_describe(config: AppConfig, project: Path):
    git_repo(project)
    client = TestClient(
        create_app(config, synctex_runner=fake_runner(), message_writer=fake_writer()),
        base_url="http://127.0.0.1",
    )
    response = client.post("/api/git/message", json={})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "git_clean"


def test_git_commit_records_the_message(config: AppConfig, project: Path):
    import subprocess

    git_repo(project)
    (project / "sections" / "model.tex").write_text("\\section{Changed}\n")
    client = make_client(config, fake_runner())

    body = client.post("/api/git/commit", json={"message": "Rework the model"}).json()

    assert body["ok"] is True
    assert body["subject"] == "Rework the model"
    assert body["status"]["dirty"] == 0
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"], cwd=project, capture_output=True, text=True
    )
    assert log.stdout.strip() == "Rework the model"


def test_git_commit_reports_failure_clearly(config: AppConfig, project: Path):
    git_repo(project)
    client = make_client(config, fake_runner())

    response = client.post("/api/git/commit", json={"message": "nothing to do"})

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "git_failed"
    assert "Nothing to commit" in response.json()["detail"]["message"]


@pytest.mark.parametrize("payload", [{}, {"message": ""}, {"message": "x", "extra": 1}])
def test_git_commit_validates_its_input(config: AppConfig, payload: dict):
    client = make_client(config, fake_runner())
    assert client.post("/api/git/commit", json=payload).status_code == 422


def test_git_pull_and_push_report_their_failures(config: AppConfig, project: Path):
    git_repo(project)
    client = make_client(config, fake_runner())

    for route in ("/api/git/pull", "/api/git/push"):
        response = client.post(route, json={})
        assert response.status_code == 409, route
        assert response.json()["detail"]["error"] == "git_failed"


# ---------------------------------------------------------------- precision


WORDY_LINE_2 = "Let the \\emph{elasticity} of substitution be one."
WORDY_LINE_3 = "The elasticity governs the response of output."


def column_of(line: str, text: str) -> int:
    """The 1-based column a word sits at, so the tests never count by eye."""
    return line.index(text) + 1


@pytest.fixture()
def wordy(project: Path) -> Path:
    """A source file with a word worth pinpointing, and a repeat to confuse it."""
    (project / "sections" / "model.tex").write_text(
        f"\\section{{Model}}\n{WORDY_LINE_2}\n{WORDY_LINE_3}\n"
    )
    return project


def test_a_click_on_a_word_resolves_to_its_column(config: AppConfig, wordy: Path):
    client = make_client(config, fake_runner(line=2))
    body = client.post(
        "/api/select",
        json={"page": 1, "x": 100.0, "y": 200.0, "word": "elasticity"},
    ).json()

    assert body["source"]["line"] == 2
    expected = column_of(WORDY_LINE_2, "elasticity")  # inside \emph{...}
    assert body["source"]["column"] == expected
    assert body["source"]["word"] == "elasticity"
    assert body["message"] == f"Selected sections/model.tex:2:{expected}"


def test_context_decides_between_repeats(config: AppConfig, wordy: Path):
    client = make_client(config, fake_runner(line=2))
    body = client.post(
        "/api/select",
        json={
            "page": 1,
            "x": 100.0,
            "y": 200.0,
            "word": "elasticity",
            "contextBefore": ["The"],
            "contextAfter": ["governs", "the", "response"],
        },
    ).json()

    # The one on the next line, not the one SyncTeX pointed at.
    assert body["source"]["line"] == 3
    assert body["source"]["column"] == column_of(WORDY_LINE_3, "elasticity")


def test_a_word_that_is_not_there_leaves_the_line_alone(config: AppConfig, wordy: Path):
    client = make_client(config, fake_runner(line=2))
    body = client.post(
        "/api/select",
        json={"page": 1, "x": 100.0, "y": 200.0, "word": "chartreuse"},
    ).json()

    assert body["source"]["line"] == 2
    assert body["source"]["column"] == 1
    assert body["source"]["word"] is None
    assert body["message"] == "Selected sections/model.tex:2"
    # The reason travels in the payload, for the caller that needs to explain
    # itself — opening the editor at column 1 without a word, say.
    assert "chartreuse" in body["why"]


def test_a_selection_pinpoints_its_opening_words(config: AppConfig, wordy: Path):
    client = make_client(config, fake_runner(line=2))
    body = client.post(
        "/api/select",
        json={"page": 1, "x": 100.0, "y": 200.0, "selectedText": "of substitution be one"},
    ).json()

    assert body["source"]["line"] == 2
    assert body["source"]["column"] == column_of(WORDY_LINE_2, "of substitution")


def test_resolve_pinpoints_too(config: AppConfig, wordy: Path):
    """Alt-click opens the editor, and should land on the word, not the line."""
    client = make_client(config, fake_runner(line=2))
    body = client.post(
        "/api/resolve",
        json={"page": 1, "x": 100.0, "y": 200.0, "word": "substitution"},
    ).json()

    assert (body["line"], body["column"]) == (2, column_of(WORDY_LINE_2, "substitution"))
    assert body["word"] == "substitution"


def test_the_word_is_recorded_in_the_selection_file(config: AppConfig, wordy: Path):
    client = make_client(config, fake_runner(line=2))
    client.post("/api/select", json={"page": 1, "x": 1.0, "y": 2.0, "word": "elasticity"})

    written = json.loads(config.selection_file.read_text())
    assert written["source"] == {
        "file": "sections/model.tex",
        "line": 2,
        "column": column_of(WORDY_LINE_2, "elasticity"),
        "word": "elasticity",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"word": "x" * 500},
        {"contextBefore": ["w"] * 20},
        {"contextAfter": ["x" * 500]},
        {"contextBefore": "not a list"},
    ],
)
def test_word_context_is_bounded(config: AppConfig, payload: dict):
    client = make_client(config, fake_runner())
    response = client.post("/api/select", json={"page": 1, "x": 1.0, "y": 2.0, **payload})
    assert response.status_code == 422
