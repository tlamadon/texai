"""The demo documents: a paper and a beamer deck, to try texai on.

``texai --demo`` copies one of them somewhere fresh and opens it, so the first
thing a new user points the agent at is not their own paper. A copy, always:
the agent edits what it is shown, and the ones that ship stay as they shipped.

In an installed package they sit under ``texai/demos/`` (put there by the
wheel build, see ``pyproject.toml``); in a checkout they are the ``example``
directories at the top of the repository.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

__all__ = ["DEMOS", "DemoError", "demo_source", "unpack_demo"]

# name -> (directory in the package, directory in a checkout, the root .tex)
DEMOS: dict[str, tuple[str, str, str]] = {
    "paper": ("paper", "example", "main.tex"),
    "slides": ("slides", "example-slides", "talk.tex"),
}

# What a build leaves behind, and texai's own state: a demo starts clean.
_LEFTOVERS = shutil.ignore_patterns(
    ".texai", "*.aux", "*.log", "*.out", "*.toc", "*.nav", "*.snm", "*.vrb",
    "*.fls", "*.fdb_latexmk", "*.synctex.gz", "*.synctex", "*.pdf", "*.bbl", "*.blg",
)


class DemoError(RuntimeError):
    """The demo could not be found or copied."""


def demo_source(name: str) -> Path:
    """Where the demo's files are, in this installation."""
    packaged, checkout, tex = DEMOS[name]
    here = Path(__file__).resolve().parent
    for candidate in (here / "demos" / packaged, here.parents[1] / checkout):
        if (candidate / tex).is_file():
            return candidate
    raise DemoError(f"the {name} demo is not part of this installation of texai")


def unpack_demo(name: str, parent: Path | None = None) -> Path:
    """Copy a demo into a new directory and return its root ``.tex`` there."""
    source = demo_source(name)
    try:
        target = Path(tempfile.mkdtemp(prefix=f"texai-demo-{name}-", dir=parent))
        shutil.copytree(source, target, ignore=_LEFTOVERS, dirs_exist_ok=True)
    except OSError as exc:
        raise DemoError(f"could not copy the {name} demo: {exc}") from exc
    return target / DEMOS[name][2]
