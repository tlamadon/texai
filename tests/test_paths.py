import os
from pathlib import Path

import pytest

from texai.paths import (
    PathOutsideRootError,
    ensure_inside_root,
    resolve_root,
    resolve_source_path,
    resolve_user_path,
    to_project_relative,
)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "sections").mkdir(parents=True)
    (root / "build").mkdir()
    (root / "main.tex").write_text("\\documentclass{article}\n")
    (root / "sections" / "model.tex").write_text("text\n")
    (root / "build" / "main.pdf").write_bytes(b"%PDF-1.4\n")
    return root.resolve()


def test_resolve_root_rejects_files(project: Path):
    with pytest.raises(NotADirectoryError):
        resolve_root(project / "main.tex")


def test_resolve_root_rejects_missing(tmp_path: Path):
    with pytest.raises(NotADirectoryError):
        resolve_root(tmp_path / "nope")


def test_relative_paths_are_resolved_against_root(project: Path):
    assert ensure_inside_root("sections/model.tex", project) == project / "sections" / "model.tex"


def test_to_project_relative_uses_posix_separators(project: Path):
    assert to_project_relative(project / "sections" / "model.tex", project) == "sections/model.tex"
    assert to_project_relative(project / "build" / "main.pdf", project) == "build/main.pdf"


def test_root_itself_is_inside_root(project: Path):
    assert ensure_inside_root(project, project) == project


def test_dotdot_escape_is_rejected(project: Path):
    with pytest.raises(PathOutsideRootError):
        ensure_inside_root("../outside.tex", project)
    with pytest.raises(PathOutsideRootError):
        ensure_inside_root("sections/../../outside.tex", project)


def test_absolute_path_outside_root_is_rejected(project: Path):
    with pytest.raises(PathOutsideRootError):
        ensure_inside_root("/etc/passwd", project)


def test_sibling_prefix_directory_is_rejected(project: Path):
    """`/tmp/x/project-evil` must not pass as inside `/tmp/x/project`."""
    sibling = project.parent / f"{project.name}-evil"
    sibling.mkdir()
    with pytest.raises(PathOutsideRootError):
        ensure_inside_root(sibling / "main.tex", project)


def test_symlink_escape_is_rejected(project: Path, tmp_path: Path):
    secret = tmp_path / "secret.tex"
    secret.write_text("secret\n")
    link = project / "link.tex"
    link.symlink_to(secret)
    with pytest.raises(PathOutsideRootError):
        ensure_inside_root(link, project)


def test_nonexistent_path_inside_root_is_allowed(project: Path):
    assert ensure_inside_root("sections/new.tex", project) == project / "sections" / "new.tex"


def test_resolve_source_path_absolute(project: Path):
    absolute = project / "sections" / "model.tex"
    resolved = resolve_source_path(str(absolute), project, [project])
    assert resolved == absolute


def test_resolve_source_path_relative_to_pdf_dir(project: Path):
    """SyncTeX often reports paths relative to the directory holding the PDF."""
    (project / "build" / "gen.tex").write_text("generated\n")
    resolved = resolve_source_path("gen.tex", project, [project / "build", project])
    assert resolved == project / "build" / "gen.tex"


def test_resolve_source_path_falls_back_to_root(project: Path):
    resolved = resolve_source_path("./sections/model.tex", project, [project / "build", project])
    assert resolved == project / "sections" / "model.tex"


def test_resolve_source_path_missing_file_still_returns_candidate(project: Path):
    resolved = resolve_source_path("sections/ghost.tex", project, [project])
    assert resolved == project / "sections" / "ghost.tex"


def test_resolve_source_path_outside_root_is_rejected(project: Path):
    with pytest.raises(PathOutsideRootError):
        resolve_source_path("/usr/share/texmf/tex/latex/base/article.cls", project, [project])


def test_resolve_source_path_empty(project: Path):
    with pytest.raises(ValueError):
        resolve_source_path("   ", project, [project])


def test_user_path_relative_to_root(project: Path, monkeypatch: pytest.MonkeyPatch):
    """`--root /p --pdf build/main.pdf`: the root-relative reading."""
    monkeypatch.chdir(project.parent)
    assert resolve_user_path("build/main.pdf", project) == project / "build" / "main.pdf"


def test_user_path_relative_to_cwd(project: Path, monkeypatch: pytest.MonkeyPatch):
    """`--root ./project --pdf ./project/build/main.pdf`: the CWD-relative reading."""
    monkeypatch.chdir(project.parent)
    assert resolve_user_path("project/build/main.pdf", project) == project / "build" / "main.pdf"


def test_user_path_absolute(project: Path):
    absolute = project / "build" / "main.pdf"
    assert resolve_user_path(str(absolute), project) == absolute


def test_user_path_missing_falls_back_to_root_relative(
    project: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(project.parent)
    assert resolve_user_path("build/ghost.pdf", project) == project / "build" / "ghost.pdf"


def test_user_path_ignores_cwd_match_outside_root(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A same-named file next to the CWD must not shadow the in-root one."""
    decoy = tmp_path / "build"
    decoy.mkdir()
    (decoy / "main.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.chdir(tmp_path)
    assert resolve_user_path("build/main.pdf", project) == project / "build" / "main.pdf"


def test_user_path_reports_escape_rather_than_not_found(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`--root ./project --pdf ./other.pdf`: the file exists, just not in the root."""
    (tmp_path / "other.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PathOutsideRootError):
        resolve_user_path("other.pdf", project)


def test_user_path_outside_root_is_rejected(project: Path, tmp_path: Path):
    outside = tmp_path / "elsewhere.pdf"
    outside.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(PathOutsideRootError):
        resolve_user_path(str(outside), project)


def test_root_is_normalised_through_symlink(tmp_path: Path):
    """A symlinked root resolves to its real path, so containment stays consistent."""
    real = tmp_path / "real"
    (real / "sections").mkdir(parents=True)
    (real / "sections" / "model.tex").write_text("x\n")
    link = tmp_path / "linked"
    os.symlink(real, link)

    root = resolve_root(link)
    assert root == real.resolve()
    assert to_project_relative(link / "sections" / "model.tex", root) == "sections/model.tex"
