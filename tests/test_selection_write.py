import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from texai.selection import (
    SELECTION_FILENAME,
    atomic_write_json,
    build_selection,
    selection_path,
)


def test_selection_path_layout(tmp_path: Path):
    assert selection_path(tmp_path) == tmp_path / ".texai" / SELECTION_FILENAME


def test_build_selection_matches_documented_shape():
    stamp = datetime(2026, 8, 13, 18, 20, 31, tzinfo=timezone(timedelta(hours=-5)))
    selection = build_selection(
        pdf="build/main.pdf",
        page=7,
        x=241.34,
        y=418.23,
        source_file="sections/model.tex",
        line=143,
        column=1,
        selected_text=None,
        updated_at=stamp,
    )
    assert selection == {
        "version": 1,
        "updatedAt": "2026-08-13T18:20:31-05:00",
        "pdf": "build/main.pdf",
        "page": 7,
        "pdfPosition": {"x": 241.3, "y": 418.2},
        "source": {"file": "sections/model.tex", "line": 143, "column": 1},
        "selectedText": None,
    }


def test_build_selection_keeps_selected_text():
    selection = build_selection(
        pdf="main.pdf",
        page=1,
        x=1,
        y=2,
        source_file="main.tex",
        line=3,
        column=4,
        selected_text="the estimated elasticity",
    )
    assert selection["selectedText"] == "the estimated elasticity"


def test_atomic_write_creates_parents_and_valid_json(tmp_path: Path):
    target = tmp_path / ".texai" / "current-selection.json"
    payload = {"version": 1, "source": {"file": "sections/modèle.tex", "line": 12}}

    atomic_write_json(target, payload)

    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert target.read_text(encoding="utf-8").endswith("\n")
    # Non-ASCII is stored as-is, not escaped.
    assert "modèle" in target.read_text(encoding="utf-8")


def test_atomic_write_replaces_and_leaves_no_temp_files(tmp_path: Path):
    target = tmp_path / "out" / "current-selection.json"
    atomic_write_json(target, {"n": 1})
    atomic_write_json(target, {"n": 2})

    assert json.loads(target.read_text()) == {"n": 2}
    assert [p.name for p in target.parent.iterdir()] == [target.name]


def test_atomic_write_uses_same_directory_for_temp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The temp file must be a rename away from the target (same filesystem)."""
    target = tmp_path / "sub" / "current-selection.json"
    seen: dict[str, str] = {}
    real_mkstemp = __import__("tempfile").mkstemp

    def spy(*args, **kwargs):
        seen["dir"] = kwargs["dir"]
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr("texai.selection.tempfile.mkstemp", spy)
    atomic_write_json(target, {"n": 1})
    assert Path(seen["dir"]) == target.parent


def test_atomic_write_leaves_previous_file_intact_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "current-selection.json"
    atomic_write_json(target, {"n": 1})

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("texai.selection.os.replace", boom)
    with pytest.raises(OSError):
        atomic_write_json(target, {"n": 2})

    assert json.loads(target.read_text()) == {"n": 1}
    assert [p.name for p in tmp_path.iterdir()] == [target.name]


def test_atomic_write_survives_concurrent_readers(tmp_path: Path):
    """Every read during a rewrite sees a complete document."""
    target = tmp_path / "current-selection.json"
    atomic_write_json(target, {"n": 0})
    for n in range(1, 25):
        atomic_write_json(target, {"n": n, "pad": "x" * 4096})
        assert json.loads(target.read_text())["n"] == n


def test_written_file_is_readable_by_other_tools(tmp_path: Path):
    """mkstemp defaults to 0600; the selection file must be plainly readable."""
    target = tmp_path / "current-selection.json"
    atomic_write_json(target, {"n": 1})
    assert os.access(target, os.R_OK)
    assert target.stat().st_mode & 0o777 == 0o644
