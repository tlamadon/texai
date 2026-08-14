"""Git for the project under review.

Every operation is scoped to ``--root`` with an explicit pathspec, because the
project root is often a subdirectory of a larger repository and texai's standing
promise is that it touches nothing outside it. A commit made here cannot pick up
your unrelated staged work elsewhere in the tree.

Nothing runs through a shell, and network operations run with prompting turned
off so a missing credential fails in seconds instead of hanging on a password
prompt no one can see.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import AppConfig

__all__ = [
    "GitError",
    "GitFile",
    "GitStatus",
    "probe",
    "fetch_remote",
    "scoped_diff",
    "commit",
    "pull_rebase",
    "push",
]

LOCAL_TIMEOUT = 20
NETWORK_TIMEOUT = 120
FETCH_TIMEOUT = 30
# How often a background fetch may go to the network, per project.
FETCH_INTERVAL = 60.0
MAX_MESSAGE_CHARS = 4000
MAX_LISTED_FILES = 200

# Prompting off: without this a push to a repo needing credentials blocks on a
# terminal prompt that the browser user will never see.
NON_INTERACTIVE = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "SSH_ASKPASS_REQUIRE": "never",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
}

# Two-letter status codes from porcelain v2, in the order git reports them.
_STATE_NAMES = {
    "M": "modified",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "typechange",
}


class GitError(RuntimeError):
    """A git command failed; the message is meant to be shown to the user."""


@dataclass
class GitFile:
    path: str
    state: str
    staged: bool

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "state": self.state, "staged": self.staged}


@dataclass
class GitStatus:
    """What git knows about the project root, as far as the root is concerned."""

    repo: bool = False
    reason: str | None = None
    toplevel: str | None = None
    scoped: bool = False  # the root is a subdirectory of the repository
    branch: str | None = None
    detached: bool = False
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    files: list[GitFile] = field(default_factory=list)
    truncated: bool = False

    @property
    def dirty(self) -> int:
        return len(self.files)

    @property
    def conflicted(self) -> bool:
        return any(f.state == "conflicted" for f in self.files)

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "reason": self.reason,
            "toplevel": self.toplevel,
            "scoped": self.scoped,
            "branch": self.branch,
            "detached": self.detached,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "dirty": self.dirty,
            "conflicted": self.conflicted,
            "truncated": self.truncated,
            "files": [f.as_dict() for f in self.files[:MAX_LISTED_FILES]],
        }


def _run(
    config: AppConfig,
    args: list[str],
    *,
    timeout: int = LOCAL_TIMEOUT,
    network: bool = False,
    check: bool = False,
) -> subprocess.CompletedProcess:
    """Run one git command in the project root. Never through a shell."""
    env = dict(os.environ)
    if network:
        env.update(NON_INTERACTIVE)

    try:
        result = subprocess.run(  # noqa: S603 - argv list, shell=False
            ["git", *args],
            cwd=config.root,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed, or not on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            f"git {args[0]} timed out after {timeout}s. If it needs a password, "
            "run it once in a terminal so the credential is cached."
        ) from exc

    if check and result.returncode != 0:
        raise GitError(_message(result))
    return result


def _message(result: subprocess.CompletedProcess) -> str:
    """The most useful line git produced, for showing in the UI."""
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    return text or f"git exited with status {result.returncode}."


# ------------------------------------------------------------------ reading


def probe(config: AppConfig) -> GitStatus:
    """Where the project root stands: branch, upstream, and what is uncommitted.

    Never raises for the ordinary "not a repository" case — that is a state the
    UI shows, not an error.
    """
    try:
        top = _run(config, ["rev-parse", "--show-toplevel"])
    except GitError as exc:
        return GitStatus(repo=False, reason=str(exc))

    if top.returncode != 0:
        return GitStatus(repo=False, reason="This project is not inside a git repository.")

    toplevel = Path(top.stdout.strip())
    status = GitStatus(
        repo=True,
        toplevel=str(toplevel),
        scoped=toplevel.resolve() != config.root.resolve(),
    )

    # relativePaths=false pins paths to the repository root regardless of the
    # user's config, so the offset below is the only place paths are rebased.
    result = _run(
        config,
        [
            "-c",
            "status.relativePaths=false",
            "status",
            "--porcelain=v2",
            "--branch",
            # "all", not "normal": a new directory should count as the files
            # in it, which is what the number in the UI claims to be.
            "--untracked-files=all",
            "--",
            ".",
        ],
    )
    if result.returncode != 0:
        status.reason = _message(result)
        return status

    _parse_status(result.stdout, toplevel, config.root, status)
    return status


def _parse_status(text: str, toplevel: Path, root: Path, status: GitStatus) -> None:
    for line in text.splitlines():
        if line.startswith("# branch.head "):
            head = line[len("# branch.head ") :].strip()
            status.detached = head == "(detached)"
            status.branch = None if status.detached else head
        elif line.startswith("# branch.upstream "):
            status.upstream = line[len("# branch.upstream ") :].strip()
        elif line.startswith("# branch.ab "):
            for part in line[len("# branch.ab ") :].split():
                if part.startswith("+"):
                    status.ahead = int(part[1:])
                elif part.startswith("-"):
                    status.behind = int(part[1:])
        elif line.startswith(("1 ", "2 ", "u ", "? ")):
            entry = _parse_entry(line, toplevel, root)
            if entry is not None:
                status.files.append(entry)

    status.truncated = len(status.files) > MAX_LISTED_FILES


def _parse_entry(line: str, toplevel: Path, root: Path) -> GitFile | None:
    kind, _, rest = line.partition(" ")

    if kind == "?":
        return _make_file(rest.strip(), "untracked", staged=False, toplevel=toplevel, root=root)

    # Porcelain v2 field counts, path last:
    #   1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
    #   2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <score> <path>\t<origPath>
    fields = rest.split(" ", 8 if kind == "2" else 7)
    if len(fields) < 2:
        return None
    xy = fields[0]

    if kind == "u":
        return _make_file(fields[-1], "conflicted", staged=False, toplevel=toplevel, root=root)

    # A rename carries "<path>\t<original>"; the new name is what matters here.
    path = fields[-1].split("\t")[0]
    staged = xy[0] != "."
    code = xy[0] if staged else xy[1]
    return _make_file(
        path, _STATE_NAMES.get(code, "modified"), staged=staged, toplevel=toplevel, root=root
    )


def _make_file(path: str, state: str, *, staged: bool, toplevel: Path, root: Path) -> GitFile | None:
    """Rebase a repository-relative path onto the project root, dropping outsiders."""
    absolute = (toplevel / path).resolve()
    try:
        relative = absolute.relative_to(root.resolve())
    except ValueError:
        return None  # outside --root; not ours to report or commit
    return GitFile(path=relative.as_posix(), state=state, staged=staged)


# Last background fetch per project root, so opening the panel repeatedly does
# not hammer the remote.
_LAST_FETCH: dict[str, float] = {}


def fetch_remote(config: AppConfig, *, force: bool = False) -> bool:
    """Refresh remote-tracking refs so "behind" means something.

    Nothing else updates them, so without this the panel would report 0 behind
    forever and the Pull button would never show a count. Best-effort by design:
    being offline is not an error worth showing anyone.
    """
    key = str(config.root)
    now = time.monotonic()
    if not force and now - _LAST_FETCH.get(key, -FETCH_INTERVAL) < FETCH_INTERVAL:
        return False

    remotes = _run(config, ["remote"])
    if remotes.returncode != 0 or not remotes.stdout.strip():
        _LAST_FETCH[key] = now
        return False

    _LAST_FETCH[key] = now
    result = _run(
        config, ["fetch", "--quiet"], timeout=FETCH_TIMEOUT, network=True
    )
    return result.returncode == 0


def scoped_diff(config: AppConfig, max_chars: int = 12000) -> str:
    """Everything uncommitted under the root, as a diff, for writing a message.

    Untracked files appear as a name-only listing: their whole contents would
    swamp the diff, and their names carry most of the signal.
    """
    parts: list[str] = []

    tracked = _run(config, ["diff", "HEAD", "--", "."])
    if tracked.returncode != 0:  # no commits yet, so HEAD does not resolve
        tracked = _run(config, ["diff", "--", "."])
    if tracked.stdout.strip():
        parts.append(tracked.stdout)

    untracked = _run(config, ["ls-files", "--others", "--exclude-standard", "--", "."])
    names = [n for n in untracked.stdout.splitlines() if n.strip()]
    if names:
        listing = "\n".join(f"  {n}" for n in names[:50])
        parts.append(f"New files (contents not shown):\n{listing}")

    diff = "\n\n".join(parts)
    if len(diff) > max_chars:
        diff = diff[:max_chars] + "\n\n[diff truncated]"
    return diff


def summarize(status: GitStatus) -> str:
    """A plain fallback commit message, used when the agent cannot write one."""
    if not status.files:
        return "Update project files"
    names = [f.path for f in status.files]
    if len(names) == 1:
        return f"Update {names[0]}"
    head = ", ".join(names[:3])
    if len(names) > 3:
        head += f" and {len(names) - 3} more"
    return f"Update {head}"


# ------------------------------------------------------------------ writing


def commit(config: AppConfig, message: str) -> dict[str, Any]:
    """Stage and commit everything under the project root.

    The pathspec is what keeps this honest: work staged elsewhere in a larger
    repository stays staged and uncommitted.
    """
    text = message.strip()
    if not text:
        raise GitError("A commit needs a message.")
    if len(text) > MAX_MESSAGE_CHARS:
        raise GitError(f"That commit message is too long ({len(text)} characters).")

    before = probe(config)
    if not before.repo:
        raise GitError(before.reason or "This project is not inside a git repository.")
    if before.conflicted:
        raise GitError("Resolve the merge conflicts first — some files are still conflicted.")
    if not before.files:
        raise GitError("Nothing to commit.")

    _run(config, ["add", "-A", "--", "."], check=True)
    result = _run(config, ["commit", "-m", text, "--", "."])
    if result.returncode != 0:
        raise GitError(_message(result))

    sha = _run(config, ["rev-parse", "--short", "HEAD"]).stdout.strip()
    return {
        "commit": sha,
        "subject": text.splitlines()[0],
        "files": [f.path for f in before.files],
        "output": (result.stdout or "").strip(),
    }


def pull_rebase(config: AppConfig) -> dict[str, Any]:
    """Bring in upstream work, replaying local commits on top."""
    status = probe(config)
    if not status.repo:
        raise GitError(status.reason or "This project is not inside a git repository.")
    if not status.upstream:
        raise GitError(
            f"The branch {status.branch or 'HEAD'} has no upstream to pull from. "
            "Set one with: git push -u origin <branch>"
        )
    if status.files:
        raise GitError(
            "Commit your changes first — a rebase cannot run with uncommitted work "
            "in the way."
        )

    result = _run(config, ["pull", "--rebase"], timeout=NETWORK_TIMEOUT, network=True)
    if result.returncode != 0:
        # A conflicted rebase leaves the tree mid-operation; say so plainly.
        text = _message(result)
        if _rebase_in_progress(config):
            text += (
                "\n\nThe rebase stopped on a conflict and is still in progress. "
                "Finish it in a terminal (git rebase --continue) or abort it "
                "(git rebase --abort)."
            )
        raise GitError(text)

    return {"output": (result.stdout or result.stderr or "").strip()}


def push(config: AppConfig) -> dict[str, Any]:
    """Publish local commits.

    With no upstream configured this sets one, but only when the choice is
    unambiguous — a single remote.
    """
    status = probe(config)
    if not status.repo:
        raise GitError(status.reason or "This project is not inside a git repository.")
    if status.detached:
        raise GitError("HEAD is detached, so there is no branch to push.")

    args = ["push"]
    if not status.upstream:
        remotes = [
            r for r in _run(config, ["remote"]).stdout.split() if r
        ]
        if not remotes:
            raise GitError("This repository has no remote to push to.")
        if len(remotes) > 1:
            raise GitError(
                f"The branch {status.branch} has no upstream and this repository has "
                f"several remotes ({', '.join(remotes)}). Set one with: "
                f"git push -u <remote> {status.branch}"
            )
        args += ["--set-upstream", remotes[0], status.branch or "HEAD"]

    result = _run(config, args, timeout=NETWORK_TIMEOUT, network=True)
    if result.returncode != 0:
        raise GitError(_message(result))

    # git reports a push on stderr even when it works.
    return {"output": (result.stderr or result.stdout or "").strip() or "Pushed."}


def _rebase_in_progress(config: AppConfig) -> bool:
    git_dir = _run(config, ["rev-parse", "--git-dir"]).stdout.strip()
    if not git_dir:
        return False
    base = (config.root / git_dir).resolve()
    return (base / "rebase-merge").exists() or (base / "rebase-apply").exists()
