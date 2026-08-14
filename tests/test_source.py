"""Reading and writing source files for the editor."""

from __future__ import annotations

import pytest

from texai.config import AppConfig
from texai.source import (
    SourceConflict,
    SourceError,
    content_hash,
    list_sources,
    read_source,
    write_source,
)


@pytest.fixture()
def project(tmp_path) -> AppConfig:
    (tmp_path / "main.tex").write_text("\\documentclass{article}\n\\begin{document}\nhi\n")
    (tmp_path / "sections").mkdir()
    (tmp_path / "sections" / "model.tex").write_text("\\section{Model}\nbody\n")
    (tmp_path / "refs.bib").write_text("@book{a, title={A}}\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "main.pdf").write_bytes(b"%PDF-1.4\n")
    return AppConfig.create(tmp_path, tmp_path / "build" / "main.pdf")


def test_lists_only_source_files(project):
    files = list_sources(project)
    assert "main.tex" in files
    assert "sections/model.tex" in files
    assert "refs.bib" in files
    assert not any(f.endswith(".pdf") for f in files)


def test_read_returns_text_and_hash(project):
    data = read_source(project, "sections/model.tex")
    assert data["file"] == "sections/model.tex"
    assert data["text"] == "\\section{Model}\nbody\n"
    assert data["sha"] == content_hash(data["text"])
    assert data["lines"] == 3


def test_read_rejects_paths_outside_the_root(project):
    with pytest.raises(SourceError, match="outside the project root"):
        read_source(project, "../secrets.tex")


def test_read_rejects_absolute_paths(project, tmp_path):
    outside = tmp_path.parent / "elsewhere.tex"
    outside.write_text("nope")
    with pytest.raises(SourceError, match="outside the project root"):
        read_source(project, str(outside))


def test_read_rejects_files_that_are_not_source(project):
    with pytest.raises(SourceError, match="not an editable source file"):
        read_source(project, "build/main.pdf")


def test_read_reports_a_missing_file(project):
    with pytest.raises(SourceError, match="No such file"):
        read_source(project, "sections/ghost.tex")


def test_write_replaces_the_file(project):
    before = read_source(project, "sections/model.tex")
    result = write_source(project, "sections/model.tex", "new body\n", before["sha"])

    assert (project.root / "sections" / "model.tex").read_text() == "new body\n"
    assert result["sha"] == content_hash("new body\n")
    assert result["file"] == "sections/model.tex"


def test_write_refuses_when_the_file_moved_underneath(project):
    before = read_source(project, "sections/model.tex")
    # Someone else — the agent, say — rewrites it first.
    (project.root / "sections" / "model.tex").write_text("the agent's version\n")

    with pytest.raises(SourceConflict, match="changed since you opened it"):
        write_source(project, "sections/model.tex", "my version\n", before["sha"])

    # And their work is still there.
    assert (project.root / "sections" / "model.tex").read_text() == "the agent's version\n"


def test_write_without_a_base_hash_is_allowed(project):
    write_source(project, "sections/model.tex", "forced\n", None)
    assert (project.root / "sections" / "model.tex").read_text() == "forced\n"


def test_write_refuses_a_vanished_file(project):
    before = read_source(project, "sections/model.tex")
    (project.root / "sections" / "model.tex").unlink()

    with pytest.raises(SourceConflict, match="no longer exists"):
        write_source(project, "sections/model.tex", "text\n", before["sha"])


def test_write_can_create_a_new_source_file(project):
    write_source(project, "sections/new.tex", "\\section{New}\n", None)
    assert (project.root / "sections" / "new.tex").read_text() == "\\section{New}\n"


def test_write_rejects_paths_outside_the_root(project, tmp_path):
    target = tmp_path.parent / "escaped.tex"
    with pytest.raises(SourceError, match="outside the project root"):
        write_source(project, "../escaped.tex", "pwned\n", None)
    assert not target.exists()


def test_write_rejects_files_that_are_not_source(project):
    with pytest.raises(SourceError, match="not an editable source file"):
        write_source(project, "build/main.pdf", "not a pdf\n", None)
    assert (project.root / "build" / "main.pdf").read_bytes() == b"%PDF-1.4\n"


def test_write_is_atomic(project, monkeypatch):
    """A failed write must not leave the file truncated."""
    import texai.selection as selection

    original = (project.root / "sections" / "model.tex").read_text()
    real_replace = selection.os.replace

    def explode(src, dst):  # noqa: ARG001
        raise OSError("disk full")

    monkeypatch.setattr(selection.os, "replace", explode)
    with pytest.raises(OSError):
        write_source(project, "sections/model.tex", "half a file", None)
    monkeypatch.setattr(selection.os, "replace", real_replace)

    assert (project.root / "sections" / "model.tex").read_text() == original


def test_hash_changes_with_content():
    assert content_hash("a") != content_hash("b")
    assert content_hash("a") == content_hash("a")
