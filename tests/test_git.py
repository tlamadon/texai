"""Git integration, against real repositories.

Mocking git here would test the mock. These build actual repositories in
``tmp_path`` — including the case texai is usually in, where the project root is
a subdirectory of a larger repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from texai import git as G
from texai.config import AppConfig

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is not installed",
)


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, check=False)


def init_bare(path: Path, cwd: Path) -> None:
    """A bare remote whose HEAD is on main, whatever git defaults to here.

    Without this the remote's HEAD names init.defaultBranch — "master" on a
    stock git, "main" on many developers' machines — and cloning a repo whose
    HEAD points at a branch that was never pushed checks out nothing at all.
    """
    run("git", "init", "-q", "--bare", str(path), cwd=cwd)
    run("git", "symbolic-ref", "HEAD", "refs/heads/main", cwd=path)


def init_repo(path: Path) -> None:
    run("git", "init", "-q", "-b", "main", ".", cwd=path)
    run("git", "config", "user.email", "test@example.com", cwd=path)
    run("git", "config", "user.name", "Test", cwd=path)
    run("git", "config", "commit.gpgsign", "false", cwd=path)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A repository whose root holds both the paper and unrelated files."""
    repo = tmp_path / "repo"
    (repo / "paper" / "sections").mkdir(parents=True)
    (repo / "elsewhere").mkdir()
    init_repo(repo)

    (repo / "paper" / "main.tex").write_text("\\documentclass{article}\n")
    (repo / "paper" / "main.pdf").write_bytes(b"%PDF-1.4\n")
    (repo / "elsewhere" / "notes.txt").write_text("not part of the paper\n")
    run("git", "add", "-A", cwd=repo)
    run("git", "commit", "-qm", "initial", cwd=repo)
    return repo


@pytest.fixture()
def config(workspace: Path) -> AppConfig:
    paper = workspace / "paper"
    return AppConfig.create(paper, paper / "main.pdf")


# ---------------------------------------------------------------- reading


def test_reports_a_clean_repository(config: AppConfig):
    status = G.probe(config)
    assert status.repo is True
    assert status.branch == "main"
    assert status.dirty == 0
    assert status.scoped is True  # the root is a subdirectory here
    assert status.upstream is None


def test_reports_no_repository(tmp_path: Path):
    solo = tmp_path / "solo"
    solo.mkdir()
    (solo / "main.pdf").write_bytes(b"%PDF")
    status = G.probe(AppConfig.create(solo, solo / "main.pdf"))

    assert status.repo is False
    assert "not inside a git repository" in (status.reason or "")
    assert status.dirty == 0


def test_counts_only_changes_under_the_root(config: AppConfig, workspace: Path):
    (workspace / "paper" / "main.tex").write_text("\\documentclass{book}\n")
    (workspace / "paper" / "sections" / "model.tex").write_text("\\section{Model}\n")
    (workspace / "elsewhere" / "notes.txt").write_text("edited, but not mine to report\n")

    status = G.probe(config)

    assert status.dirty == 2
    assert sorted(f.path for f in status.files) == ["main.tex", "sections/model.tex"]
    assert all(not f.path.startswith("..") for f in status.files)


def test_untracked_directories_count_as_their_files(config: AppConfig, workspace: Path):
    new = workspace / "paper" / "appendix"
    new.mkdir()
    (new / "a.tex").write_text("a\n")
    (new / "b.tex").write_text("b\n")

    status = G.probe(config)

    assert status.dirty == 2
    assert sorted(f.path for f in status.files) == ["appendix/a.tex", "appendix/b.tex"]


def test_recognises_the_kinds_of_change(config: AppConfig, workspace: Path):
    paper = workspace / "paper"
    (paper / "keep.tex").write_text("keep\n")
    (paper / "gone.tex").write_text("gone\n")
    run("git", "add", "-A", cwd=workspace)
    run("git", "commit", "-qm", "add files", cwd=workspace)

    (paper / "keep.tex").write_text("changed\n")
    (paper / "gone.tex").unlink()
    (paper / "fresh.tex").write_text("fresh\n")

    states = {f.path: f.state for f in G.probe(config).files}
    assert states == {"keep.tex": "modified", "gone.tex": "deleted", "fresh.tex": "untracked"}


def test_a_rename_is_reported_under_its_new_name(config: AppConfig, workspace: Path):
    run("git", "mv", "paper/main.tex", "paper/renamed.tex", cwd=workspace)
    files = {f.path: f.state for f in G.probe(config).files}
    assert files == {"renamed.tex": "renamed"}


def test_the_root_being_the_repository_is_not_scoped(tmp_path: Path):
    repo = tmp_path / "flat"
    repo.mkdir()
    init_repo(repo)
    (repo / "main.tex").write_text("doc\n")
    (repo / "main.pdf").write_bytes(b"%PDF")
    run("git", "add", "-A", cwd=repo)
    run("git", "commit", "-qm", "initial", cwd=repo)

    status = G.probe(AppConfig.create(repo, repo / "main.pdf"))
    assert status.scoped is False


def test_the_diff_stays_inside_the_root(config: AppConfig, workspace: Path):
    (workspace / "paper" / "main.tex").write_text("\\documentclass{book}\n")
    (workspace / "elsewhere" / "notes.txt").write_text("a secret change\n")
    (workspace / "paper" / "new.tex").write_text("new\n")

    diff = G.scoped_diff(config)

    assert "documentclass{book}" in diff
    assert "a secret change" not in diff
    assert "notes.txt" not in diff
    assert "new.tex" in diff  # untracked files appear by name


def test_the_diff_is_capped(config: AppConfig, workspace: Path):
    (workspace / "paper" / "main.tex").write_text("x\n" * 20000)
    diff = G.scoped_diff(config, max_chars=500)
    assert len(diff) < 600
    assert "truncated" in diff


# ---------------------------------------------------------------- committing


def test_commit_takes_only_what_is_under_the_root(config: AppConfig, workspace: Path):
    (workspace / "paper" / "main.tex").write_text("\\documentclass{book}\n")
    (workspace / "paper" / "sections" / "model.tex").write_text("\\section{Model}\n")
    # Staged, deliberately, outside the project root.
    (workspace / "elsewhere" / "notes.txt").write_text("mine, not yours\n")
    run("git", "add", "elsewhere/notes.txt", cwd=workspace)

    result = G.commit(config, "Rework the model")

    assert result["subject"] == "Rework the model"
    assert sorted(result["files"]) == ["main.tex", "sections/model.tex"]

    shown = run("git", "show", "--stat", "--name-only", "HEAD", cwd=workspace).stdout
    assert "paper/main.tex" in shown
    assert "paper/sections/model.tex" in shown
    assert "elsewhere/notes.txt" not in shown

    # And the outside work is still staged, exactly as it was left.
    assert "M  elsewhere/notes.txt" in run("git", "status", "--porcelain", cwd=workspace).stdout
    assert G.probe(config).dirty == 0


def test_commit_keeps_a_multi_line_message(config: AppConfig, workspace: Path):
    (workspace / "paper" / "main.tex").write_text("changed\n")
    G.commit(config, "Tighten the argument\n\nDrop the hedging in the intro.")

    body = run("git", "log", "-1", "--pretty=%B", cwd=workspace).stdout
    assert "Tighten the argument" in body
    assert "Drop the hedging in the intro." in body


def test_commit_refuses_an_empty_message(config: AppConfig, workspace: Path):
    (workspace / "paper" / "main.tex").write_text("changed\n")
    with pytest.raises(G.GitError, match="needs a message"):
        G.commit(config, "   ")


def test_commit_refuses_when_there_is_nothing_to_do(config: AppConfig):
    with pytest.raises(G.GitError, match="Nothing to commit"):
        G.commit(config, "empty")


def test_commit_refuses_an_absurd_message(config: AppConfig, workspace: Path):
    (workspace / "paper" / "main.tex").write_text("changed\n")
    with pytest.raises(G.GitError, match="too long"):
        G.commit(config, "x" * (G.MAX_MESSAGE_CHARS + 1))


def test_a_message_is_never_interpreted_as_a_shell_command(config: AppConfig, workspace: Path):
    """Messages go through argv, so shell metacharacters are just text."""
    (workspace / "paper" / "main.tex").write_text("changed\n")
    canary = workspace / "pwned.txt"

    G.commit(config, f"Fix $(touch {canary}) and `touch {canary}` and ; rm -rf /")

    assert not canary.exists()
    assert "rm -rf /" in run("git", "log", "-1", "--pretty=%B", cwd=workspace).stdout


def test_commit_refuses_while_conflicted(config: AppConfig, workspace: Path, monkeypatch):
    status = G.GitStatus(repo=True, files=[G.GitFile("main.tex", "conflicted", False)])
    monkeypatch.setattr(G, "probe", lambda _config: status)

    with pytest.raises(G.GitError, match="conflicts"):
        G.commit(config, "anything")


# ---------------------------------------------------------------- remotes


@pytest.fixture()
def with_remote(config: AppConfig, workspace: Path, tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    init_bare(remote, tmp_path)
    run("git", "remote", "add", "origin", str(remote), cwd=workspace)
    run("git", "push", "-q", "-u", "origin", "main", cwd=workspace)
    return remote


def test_push_publishes_and_clears_the_ahead_count(config: AppConfig, workspace: Path, with_remote):
    (workspace / "paper" / "main.tex").write_text("changed\n")
    G.commit(config, "A change worth pushing")
    assert G.probe(config).ahead == 1

    G.push(config)

    assert G.probe(config).ahead == 0


def test_push_sets_an_upstream_when_there_is_one_remote(config: AppConfig, workspace: Path, tmp_path: Path):
    remote = tmp_path / "solo.git"
    init_bare(remote, tmp_path)
    run("git", "remote", "add", "origin", str(remote), cwd=workspace)
    assert G.probe(config).upstream is None

    G.push(config)

    assert G.probe(config).upstream == "origin/main"


def test_push_will_not_guess_between_remotes(config: AppConfig, workspace: Path, tmp_path: Path):
    for name in ("origin", "backup"):
        target = tmp_path / f"{name}.git"
        init_bare(target, tmp_path)
        run("git", "remote", "add", name, str(target), cwd=workspace)

    with pytest.raises(G.GitError, match="several remotes"):
        G.push(config)


def test_push_without_a_remote_says_so(config: AppConfig):
    with pytest.raises(G.GitError, match="no remote"):
        G.push(config)


def test_pull_rebase_brings_in_upstream_work(config: AppConfig, workspace: Path, with_remote, tmp_path: Path):
    # A second clone publishes a commit.
    other = tmp_path / "other"
    run("git", "clone", "-q", str(with_remote), str(other), cwd=tmp_path)
    run("git", "config", "user.email", "other@example.com", cwd=other)
    run("git", "config", "user.name", "Other", cwd=other)
    (other / "paper" / "theirs.tex").write_text("theirs\n")
    run("git", "add", "-A", cwd=other)
    run("git", "commit", "-qm", "their work", cwd=other)
    run("git", "push", "-q", "origin", "main", cwd=other)

    # Meanwhile, a local commit.
    (workspace / "paper" / "mine.tex").write_text("mine\n")
    G.commit(config, "my work")

    # Nothing updates remote-tracking refs on its own, so until a fetch the
    # panel would honestly report 0 behind.
    assert G.probe(config).behind == 0
    assert G.fetch_remote(config, force=True) is True
    assert G.probe(config).behind == 1

    G.pull_rebase(config)

    status = G.probe(config)
    assert status.behind == 0
    assert status.ahead == 1  # mine, replayed on top
    assert (workspace / "paper" / "theirs.tex").exists()
    assert (workspace / "paper" / "mine.tex").exists()


def test_pull_refuses_to_run_over_uncommitted_work(config: AppConfig, workspace: Path, with_remote):
    (workspace / "paper" / "main.tex").write_text("work in progress\n")

    with pytest.raises(G.GitError, match="Commit your changes first"):
        G.pull_rebase(config)

    assert (workspace / "paper" / "main.tex").read_text() == "work in progress\n"


def test_pull_without_an_upstream_says_so(config: AppConfig):
    with pytest.raises(G.GitError, match="no upstream"):
        G.pull_rebase(config)


def test_network_operations_never_wait_on_a_prompt(config: AppConfig, workspace: Path):
    """A remote needing credentials must fail fast, not hang on an invisible prompt."""
    run("git", "remote", "add", "origin", "https://example.invalid/nope.git", cwd=workspace)
    (workspace / "paper" / "main.tex").write_text("changed\n")
    G.commit(config, "something to push")

    with pytest.raises(G.GitError):
        G.push(config)


# ---------------------------------------------------------------- summary


def test_the_fallback_message_names_the_files():
    status = G.GitStatus(repo=True, files=[G.GitFile("sections/model.tex", "modified", False)])
    assert G.summarize(status) == "Update sections/model.tex"


def test_the_fallback_message_stays_short():
    files = [G.GitFile(f"f{i}.tex", "modified", False) for i in range(9)]
    message = G.summarize(G.GitStatus(repo=True, files=files))
    assert "and 6 more" in message
    assert len(message) < 80


# ---------------------------------------------------------------- fetching


def test_fetch_is_rate_limited(config: AppConfig, with_remote):
    """Opening the panel repeatedly must not hammer the remote."""
    assert G.fetch_remote(config, force=True) is True
    assert G.fetch_remote(config) is False  # too soon


def test_fetch_without_a_remote_does_nothing(config: AppConfig):
    assert G.fetch_remote(config, force=True) is False


def test_fetch_survives_an_unreachable_remote(config: AppConfig, workspace: Path):
    run("git", "remote", "add", "origin", "https://example.invalid/nope.git", cwd=workspace)
    assert G.fetch_remote(config, force=True) is False  # reported, never raised
