"""What texai needs from the machine, and what to do about what is missing.

texai itself arrives whole through ``uvx``. Three things it cannot bring along:
a TeX installation that can build the document (``latexmk`` and an engine),
``synctex`` to turn a click on the page into a source line, and the ``claude``
CLI the agent runs through. Each used to announce itself as whatever error it
caused, one at a time. ``texai doctor`` looks for all of them at once and says,
for the system it is running on, the command that puts each one right; the same
checks word the warnings at startup.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass

from . import __version__

__all__ = ["Check", "checks", "remedy", "report", "run", "system"]

TEX_LIVE = "https://tug.org/texlive/"
CLAUDE_SETUP = "https://code.claude.com/docs/en/setup"

# How to get a TeX installation with latexmk and synctex, per system.
TEX_FIX = {
    "macOS": [
        "brew install --cask mactex-no-gui",
        "or the MacTeX installer from https://tug.org/mactex/",
        "then open a new terminal, so /Library/TeX/texbin is on PATH",
    ],
    "Linux": [
        "Debian/Ubuntu: sudo apt install texlive-full latexmk",
        "Fedora:        sudo dnf install texlive-scheme-full latexmk",
        f"or TeX Live's own installer: {TEX_LIVE}",
    ],
    "Windows": [
        "winget install MiKTeX.MiKTeX",
        f"or TeX Live's installer: {TEX_LIVE}",
        "then open a new terminal",
    ],
}
PERL_FIX = ["winget install StrawberryPerl.StrawberryPerl", "then open a new terminal"]
CLAUDE_FIX = [
    f"install Claude Code: {CLAUDE_SETUP}",
    "then run `claude` once in a terminal and sign in",
]
GIT_FIX = {
    "macOS": ["xcode-select --install"],
    "Linux": ["sudo apt install git   (or your distribution's equivalent)"],
    "Windows": ["winget install Git.Git"],
}


@dataclass(frozen=True)
class Check:
    """One thing looked for: whether it is there, and how to get it if not."""

    name: str
    ok: bool
    detail: str  # where it was found, or what it is needed for
    required: bool = True
    fix: tuple[str, ...] = ()


def system() -> str:
    """The system's name as its users say it."""
    return {"Darwin": "macOS", "Windows": "Windows"}.get(platform.system(), "Linux")


def _version(command: str, flag: str = "--version") -> str:
    """The first line a tool says about itself, or nothing if it will not say."""
    try:
        done = subprocess.run(
            [command, flag], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = (done.stdout or done.stderr).strip().splitlines()
    return lines[0].strip() if lines else ""


def _tool(name: str, needed_for: str, fix: list[str], required: bool = True) -> Check:
    path = shutil.which(name)
    if path is None:
        return Check(name, False, needed_for, required, tuple(fix))
    return Check(name, True, path, required)


def checks(agent: str = "claude", synctex: str = "synctex", versions: bool = False) -> list[Check]:
    """Everything texai looks for on this machine. ``versions`` also asks the
    tools that are there what they are, which starts each of them once."""
    here = system()
    tex = TEX_FIX[here]
    found = [
        _tool("latexmk", "builds the document, and rebuilds it after every edit", tex),
        _tool("pdflatex", "the TeX engine latexmk runs (or xelatex, lualatex)", tex),
        _tool(synctex, "maps a click on the page to its line in the source", tex),
    ]
    if here == "Windows":
        found.append(_tool("perl", "latexmk is a Perl script and will not start without it", PERL_FIX))

    if agent == "claude":
        claude = _tool("claude", "the agent: without it the viewer works, the chat does not", CLAUDE_FIX)
        if claude.ok and versions:
            said = _version("claude")
            claude = Check("claude", True, f"{claude.detail}  ({said})" if said else claude.detail)
        found.append(claude)
    else:
        from .agent import agent_status

        available, reason = agent_status(agent)
        found.append(Check(agent, available, reason or "its SDK is installed", fix=()))

    found.append(_tool("git", "the commit, pull and push buttons", GIT_FIX[here], required=False))
    return found


def report(found: list[Check]) -> str:
    """The checks as text: one line each, and under a missing one, its remedy."""
    width = max(len(check.name) for check in found)
    lines = [f"texai {__version__} on {system()}, Python {platform.python_version()}", ""]
    told: dict[tuple[str, ...], str] = {}  # a remedy, and the check it was given under
    for check in found:
        mark = "ok" if check.ok else ("MISSING" if check.required else "missing")
        lines.append(f"  {mark:<8} {check.name:<{width}}  {check.detail}")
        if not check.ok:
            if not check.required:
                lines.append(f"  {'':<8} {'':<{width}}  (optional)")
            if check.fix in told:
                # One TeX installation brings latexmk, the engine and synctex.
                lines.append(f"  {'':<8} {'':<{width}}    comes with {told[check.fix]}, above")
            else:
                told[check.fix] = check.name
                lines.extend(f"  {'':<8} {'':<{width}}    {step}" for step in check.fix)
    lines.append("")
    missing = [check.name for check in found if check.required and not check.ok]
    if missing:
        lines.append(f"Missing: {', '.join(missing)}. Run `texai doctor` again once installed.")
    else:
        lines.append("Everything texai needs is here. To try it: texai --demo")
    return "\n".join(lines)


def remedy(check: Check) -> str:
    """One missing thing as a startup warning: what it costs, and the way out."""
    steps = "; ".join(check.fix[:1]) if check.fix else ""
    told = f"`{check.name}` was not found: {check.detail}."
    return f"{told} To install: {steps} (`texai doctor` has the rest)." if steps else told


def run(agent: str = "claude", synctex: str = "synctex") -> int:
    """``texai doctor``: print the report; 0 when nothing required is missing."""
    found = checks(agent, synctex, versions=True)
    print(report(found), file=sys.stdout, flush=True)
    return 0 if all(check.ok or not check.required for check in found) else 1
