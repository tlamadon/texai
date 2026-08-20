"""Compiling the document.

The harness owns the build rather than the agent. That is what makes the rest
of the loop deterministic: we know exactly when a build started, we can diff
against the snapshot taken just before it, and we can hand compilation errors
back to the agent as a follow-up turn instead of watching it guess.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path

from .console import note

__all__ = [
    "BuildResult",
    "BuildError",
    "default_build_command",
    "find_root_tex",
    "parse_log_errors",
    "run_build",
    "running_builds",
]

BUILD_TIMEOUT_SECONDS = 300.0
MAX_LOG_ERRORS = 12
MAX_OUTPUT_CHARS = 20_000


# Builds in flight, by an id of their own, so an interrupt can say what it is
# waiting for rather than appearing to hang.
_RUNNING: dict[int, float] = {}
_BUILD_IDS = itertools.count(1)


def running_builds() -> list[float]:
    """How long each build currently running has been going, in seconds."""
    now = time.monotonic()
    return sorted((now - started for started in _RUNNING.values()), reverse=True)


class BuildError(RuntimeError):
    """The build could not be started at all (bad command, missing binary)."""


@dataclass(frozen=True)
class BuildResult:
    ok: bool
    command: list[str]
    returncode: int | None
    output: str
    errors: list[str] = field(default_factory=list)
    timed_out: bool = False

    def summary(self) -> str:
        if self.ok:
            return "Build succeeded."
        if self.timed_out:
            return f"Build timed out after {BUILD_TIMEOUT_SECONDS:g}s."
        head = "\n".join(self.errors[:MAX_LOG_ERRORS]) or self.output[-2000:]
        return f"Build failed (exit {self.returncode}).\n{head}"


def find_root_tex(pdf_path: Path, root: Path) -> Path | None:
    """Guess the root LaTeX file for a PDF.

    A sibling ``<stem>.tex`` wins; otherwise the first file under the project
    root that declares ``\\begin{document}``.
    """
    sibling = pdf_path.with_suffix(".tex")
    if sibling.is_file():
        return sibling

    candidates = sorted(root.rglob("*.tex"))
    for candidate in candidates:
        if candidate.name == f"{pdf_path.stem}.tex":
            return candidate
    for candidate in candidates:
        try:
            if "\\begin{document}" in candidate.read_text(encoding="utf-8", errors="replace"):
                return candidate
        except OSError:
            continue
    return None


def default_build_command(root_tex: Path) -> list[str]:
    return [
        "latexmk",
        "-pdf",
        "-synctex=1",
        "-interaction=nonstopmode",
        root_tex.name,
    ]


def parse_log_errors(log_text: str) -> list[str]:
    """Pull the useful error lines out of a LaTeX log.

    TeX errors start with ``!`` and the offending source line follows a few
    lines later as ``l.<number>``; we stitch those together so the agent gets
    "what went wrong" and "where" in one string.
    """
    errors: list[str] = []
    lines = log_text.splitlines()

    for index, line in enumerate(lines):
        if not line.startswith("!"):
            continue
        message = line.strip()
        location = ""
        for following in lines[index + 1 : index + 12]:
            match = re.match(r"^l\.(\d+)(.*)", following)
            if match:
                location = f" (line {match.group(1)}){match.group(2).rstrip()}"
                break
        errors.append(f"{message}{location}")
        if len(errors) >= MAX_LOG_ERRORS:
            break

    if not errors:
        for line in lines:
            if "LaTeX Error" in line or "Emergency stop" in line:
                errors.append(line.strip())
                if len(errors) >= MAX_LOG_ERRORS:
                    break
    return errors


def resolve_command(command: str | None, root_tex: Path | None) -> list[str]:
    """Turn a configured build command into an argv list.

    Parsed with ``shlex`` and run without a shell, so pipes and ``&&`` are not
    supported by design — point ``--build-cmd`` at a script if you need them.
    """
    if command:
        argv = shlex.split(command)
        if not argv:
            raise BuildError("--build-cmd is empty")
        return argv
    if root_tex is None:
        raise BuildError(
            "No build command configured and no root .tex file found. "
            "Pass --build-cmd, e.g. "
            "--build-cmd 'latexmk -pdf -synctex=1 -interaction=nonstopmode main.tex'."
        )
    return default_build_command(root_tex)


async def run_build(
    argv: list[str],
    cwd: Path,
    log_path: Path | None = None,
    reason: str = "the document",
) -> BuildResult:
    """Run the build, returning its output plus any parsed LaTeX errors.

    Announces itself on the console. A compile is the slowest thing texai does
    and the only one that happens without the user asking directly — after a
    turn, after a save — so the terminal says when one starts, and how it went.
    ``reason`` is what set it off, in a few words.
    """
    note(f"building — {reason}…")
    build_id = next(_BUILD_IDS)
    _RUNNING[build_id] = time.monotonic()
    started = time.monotonic()

    try:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise BuildError(f"Build command not found: {argv[0]}") from exc
        except PermissionError as exc:
            raise BuildError(f"Build command is not executable: {argv[0]}") from exc

        timed_out = False
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=BUILD_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            stdout, _ = await process.communicate()
    finally:
        _RUNNING.pop(build_id, None)

    output = (stdout or b"").decode("utf-8", errors="replace")[-MAX_OUTPUT_CHARS:]
    ok = not timed_out and process.returncode == 0

    errors: list[str] = []
    if not ok:
        errors = parse_log_errors(output)
        if not errors and log_path is not None and log_path.is_file():
            errors = parse_log_errors(log_path.read_text(encoding="utf-8", errors="replace"))

    elapsed = time.monotonic() - started
    if ok:
        note(f"built in {elapsed:.1f}s")
    elif timed_out:
        note(f"build timed out after {elapsed:.0f}s")
    else:
        first = f" — {errors[0]}" if errors else ""
        note(f"build failed in {elapsed:.1f}s (exit {process.returncode}){first}")

    return BuildResult(
        ok=ok,
        command=argv,
        returncode=process.returncode,
        output=output,
        errors=errors,
        timed_out=timed_out,
    )
