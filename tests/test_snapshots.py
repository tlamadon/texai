from pathlib import Path

import pytest

from texai.snapshots import diff_against, iter_source_files, restore, take_snapshot


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "sections").mkdir(parents=True)
    (root / "build").mkdir()
    (root / "main.tex").write_text("\\documentclass{article}\n")
    (root / "sections" / "model.tex").write_text("alpha\nbeta\ngamma\n")
    (root / "refs.bib").write_text("@book{x, title={X}}\n")
    (root / "build" / "main.pdf").write_bytes(b"%PDF-1.4\n")
    (root / "build" / "main.log").write_text("log output\n")
    return root.resolve()


def rel(paths, root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in paths}


def test_iter_source_files_picks_up_text_sources_only(project: Path):
    found = rel(iter_source_files(project), project)
    assert found == {"main.tex", "sections/model.tex", "refs.bib"}


def test_iter_source_files_skips_tool_state(project: Path):
    state = project / ".texai" / "snapshots" / "t0001"
    state.mkdir(parents=True)
    (state / "main.tex").write_text("snapshot copy\n")
    assert "main.tex" in rel(iter_source_files(project), project)
    assert not any(".texai" in p for p in rel(iter_source_files(project), project))


def test_snapshot_copies_sources(project: Path, tmp_path: Path):
    snapshot = take_snapshot(project, tmp_path / "snaps", "t0001")
    assert snapshot.files == {"main.tex", "sections/model.tex", "refs.bib"}
    assert (snapshot.directory / "sections" / "model.tex").read_text() == "alpha\nbeta\ngamma\n"


def test_diff_reports_nothing_when_unchanged(project: Path, tmp_path: Path):
    snapshot = take_snapshot(project, tmp_path / "snaps", "t0001")
    assert diff_against(snapshot, project) == []


def test_diff_reports_modifications(project: Path, tmp_path: Path):
    snapshot = take_snapshot(project, tmp_path / "snaps", "t0001")
    (project / "sections" / "model.tex").write_text("alpha\nBETA\ngamma\ndelta\n")

    changes = diff_against(snapshot, project)
    assert len(changes) == 1
    change = changes[0]
    assert change["file"] == "sections/model.tex"
    assert change["status"] == "modified"
    assert change["added"] == 2
    assert change["removed"] == 1
    assert "-beta" in change["diff"]
    assert "+BETA" in change["diff"]


def test_diff_reports_added_and_deleted(project: Path, tmp_path: Path):
    snapshot = take_snapshot(project, tmp_path / "snaps", "t0001")
    (project / "sections" / "new.tex").write_text("fresh\n")
    (project / "refs.bib").unlink()

    statuses = {c["file"]: c["status"] for c in diff_against(snapshot, project)}
    assert statuses == {"sections/new.tex": "added", "refs.bib": "deleted"}


def test_restore_undoes_edits_additions_and_deletions(project: Path, tmp_path: Path):
    snapshot = take_snapshot(project, tmp_path / "snaps", "t0001")

    (project / "sections" / "model.tex").write_text("wrecked\n")
    (project / "sections" / "extra.tex").write_text("should not survive\n")
    (project / "refs.bib").unlink()

    touched = restore(snapshot, project)

    assert (project / "sections" / "model.tex").read_text() == "alpha\nbeta\ngamma\n"
    assert (project / "refs.bib").exists()
    assert not (project / "sections" / "extra.tex").exists()
    assert set(touched) == {"sections/model.tex", "sections/extra.tex", "refs.bib"}
    assert diff_against(snapshot, project) == []


def test_restore_leaves_build_output_alone(project: Path, tmp_path: Path):
    """Snapshots cover sources; latexmk owns everything under build/."""
    snapshot = take_snapshot(project, tmp_path / "snaps", "t0001")
    (project / "build" / "main.pdf").write_bytes(b"%PDF-1.4 rebuilt\n")
    restore(snapshot, project)
    assert (project / "build" / "main.pdf").read_bytes() == b"%PDF-1.4 rebuilt\n"


def test_second_snapshot_with_same_id_replaces_the_first(project: Path, tmp_path: Path):
    snaps = tmp_path / "snaps"
    take_snapshot(project, snaps, "t0001")
    (project / "refs.bib").unlink()
    snapshot = take_snapshot(project, snaps, "t0001")
    assert "refs.bib" not in snapshot.files
    assert not (snapshot.directory / "refs.bib").exists()
