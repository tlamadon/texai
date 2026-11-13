"""Noticing that the PDF is behind what it is built from."""

from __future__ import annotations

import os
import shlex
import time
from pathlib import Path

import pytest

from texai.build import forced
from texai.cli import rebuild_stale_pdf
from texai.config import AppConfig
from texai.freshness import Freshness

NOW = time.time()


def at(path: Path, seconds_ago: float) -> None:
    """Set a file's modification time to ``seconds_ago`` before now."""
    os.utime(path, (NOW - seconds_ago, NOW - seconds_ago))


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A paper built an hour ago, with its SyncTeX data, and not touched since."""
    root = tmp_path / "paper"
    (root / "sections").mkdir(parents=True)
    for name in ("main.tex", "sections/model.tex", "annotations.jsonl", "main.pdf", "main.synctex.gz"):
        (root / name).write_text("x")
    at(root / "main.tex", 7200)
    at(root / "sections/model.tex", 7200)
    at(root / "annotations.jsonl", 7200)
    at(root / "main.pdf", 3600)
    at(root / "main.synctex.gz", 3600)
    # No build has run in this process, whatever other tests did.
    monkeypatch.setattr("texai.freshness.last_build_finished_ns", lambda: 0)
    return root.resolve()


def config_for(project: Path, build: str | None = None) -> AppConfig:
    return AppConfig.create(project, project / "main.pdf", build_command=build)


def test_a_pdf_newer_than_its_sources_is_current(project: Path):
    assert Freshness(config_for(project)).stale() is None


def test_a_source_changed_since_the_build_makes_it_stale(project: Path):
    at(project / "sections/model.tex", 60)
    assert Freshness(config_for(project)).stale() == "sections/model.tex"


def test_the_records_file_is_no_part_of_the_build(project: Path):
    at(project / "annotations.jsonl", 60)
    assert Freshness(config_for(project)).stale() is None


def test_a_source_still_being_written_is_left_to_settle(project: Path):
    os.utime(project / "main.tex")  # written this instant
    fresh = Freshness(config_for(project))
    assert fresh.stale() is None
    assert fresh.stale(quiet=0) == "main.tex"


def test_a_change_is_raised_once_and_a_later_one_again(project: Path):
    fresh = Freshness(config_for(project))
    at(project / "main.tex", 60)
    assert fresh.stale() == "main.tex"
    fresh.handled()  # the build it set off may fail, or leave the PDF alone
    assert fresh.stale() is None
    # Later than now, not than when this file was imported: on a slow machine
    # the two are far enough apart to matter.
    later = time.time() + 5
    os.utime(project / "main.tex", (later, later))
    assert fresh.stale(quiet=0) == "main.tex"


def test_a_build_since_the_change_has_dealt_with_it(project: Path, monkeypatch):
    at(project / "main.tex", 60)
    monkeypatch.setattr("texai.freshness.last_build_finished_ns", lambda: int((NOW - 30) * 1e9))
    assert Freshness(config_for(project)).stale() is None


def test_a_pdf_without_synctex_data_is_out_of_step(project: Path):
    (project / "main.synctex.gz").unlink()
    fresh = Freshness(config_for(project))
    assert fresh.unsynced() == "the PDF has no SyncTeX data"
    # Once for this PDF: a build that writes none will not write it next time.
    assert fresh.unsynced() is None


def test_synctex_data_from_an_older_build_is_out_of_step(project: Path):
    at(project / "main.synctex.gz", 7000)
    assert Freshness(config_for(project)).unsynced() == "the PDF is newer than its SyncTeX data"
    at(project / "main.synctex.gz", 3605)  # the same run, moments apart
    assert Freshness(config_for(project)).unsynced() is None


def test_only_latexmk_is_told_to_build_regardless():
    assert forced(["latexmk", "-pdf", "main.tex"]) == ["latexmk", "-g", "-pdf", "main.tex"]
    assert forced(["/usr/bin/latexmk", "-g", "main.tex"]) == ["/usr/bin/latexmk", "-g", "main.tex"]
    assert forced(["make", "paper"]) == ["make", "paper"]


def fake_build(tmp_path: Path, body: str) -> str:
    script = tmp_path / "build.sh"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return shlex.quote(str(script))


def test_startup_rebuilds_a_pdf_that_is_behind(project: Path, tmp_path: Path):
    build = fake_build(tmp_path, "echo built >> ran.log; touch main.pdf main.synctex.gz")
    config = config_for(project, build)

    assert rebuild_stale_pdf(config) is False  # current: nothing to do
    assert not (project / "ran.log").exists()

    at(project / "sections/model.tex", 60)
    assert rebuild_stale_pdf(config) is True
    assert (project / "ran.log").read_text() == "built\n"


def test_startup_rebuilds_a_pdf_with_no_synctex_data(project: Path, tmp_path: Path):
    (project / "main.synctex.gz").unlink()
    build = fake_build(tmp_path, "echo built >> ran.log; touch main.pdf main.synctex.gz")
    assert rebuild_stale_pdf(config_for(project, build)) is True
    assert (project / "main.synctex.gz").is_file()


def test_a_failed_rebuild_at_startup_does_not_stop_texai(project: Path, tmp_path: Path, capsys):
    at(project / "main.tex", 60)
    assert rebuild_stale_pdf(config_for(project, fake_build(tmp_path, "exit 1"))) is True
    assert "showing the PDF as it was before" in capsys.readouterr().err
