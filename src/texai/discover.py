"""Working out a whole project from the one file you name.

``texai paper.tex``, ``texai build/paper.pdf`` or a bare ``texai`` in the
paper's directory is enough; ``--root`` and ``--pdf`` remain as overrides for a
layout these rules guess wrong.

* **The .tex.** Named outright; or, from a PDF, the ``<stem>.tex`` beside it or
  a few directories up (``build/main.pdf`` is built from ``../main.tex``); or,
  from a directory, the one ``.tex`` in it that opens with ``\\documentclass``.
* **The PDF.** Wherever ``latexmk`` will write it: under the ``$out_dir`` a
  ``latexmkrc`` beside the .tex names, else next to the .tex — unless a built
  ``<stem>.pdf`` with its SyncTeX data already sits in a subdirectory, which is
  where some other build setup put it. It need not exist yet; the first build
  makes it.
* **The root.** The narrowest directory holding both: the .tex's own directory
  in the usual layout. Everything texai and the agent touch stays inside it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .build import find_root_tex
from .paths import ensure_inside_root, resolve_root, resolve_user_path
from .synctex import synctex_data_file

__all__ = ["DiscoveryError", "Layout", "discover"]

# How far above a PDF to look for the .tex it was built from: ``build/`` and
# ``out/pdf/`` are common, anything deeper is a layout worth naming by hand.
SEARCH_UP = 3

# ``$out_dir = 'build';`` in a latexmkrc. Perl, so either quote; only a literal
# is understood, which is what nearly every paper's latexmkrc has.
_OUT_DIR = re.compile(r"""\$out_dir\s*=\s*(['"])(.+?)\1""")
_LATEXMKRC = ("latexmkrc", ".latexmkrc")
_DOCUMENTCLASS = re.compile(r"^[^%\n]*\\documentclass", re.MULTILINE)


class DiscoveryError(ValueError):
    """The arguments do not pin down a document, and why."""


@dataclass(frozen=True)
class Layout:
    root: Path
    pdf: Path
    tex: Path | None  # None: no source found; the build must be named by hand


def _is_root_tex(path: Path) -> bool:
    try:
        return bool(_DOCUMENTCLASS.search(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return False


def _root_tex_in(directory: Path) -> Path:
    """The one document in a directory, or a clear refusal to guess."""
    found = [path for path in sorted(directory.glob("*.tex")) if _is_root_tex(path)]
    if len(found) == 1:
        return found[0]
    if not found:
        raise DiscoveryError(
            f"no LaTeX document (a .tex with \\documentclass) in {directory} — "
            "name the .tex or .pdf to review."
        )
    main = [path for path in found if path.name == "main.tex"]
    if main:
        return main[0]
    names = ", ".join(path.name for path in found)
    raise DiscoveryError(f"several documents in {directory} ({names}) — name the one to review.")


def _tex_for_pdf(pdf: Path, root: Path | None) -> Path | None:
    """The source a PDF was built from: ``<stem>.tex`` beside it or just above."""
    directory = pdf.parent
    for _ in range(SEARCH_UP + 1):
        candidate = directory / f"{pdf.stem}.tex"
        if candidate.is_file():
            return candidate
        if directory == root or directory.parent == directory:
            break
        directory = directory.parent
    # Given a root, anything inside it is fair game, as it always was.
    return find_root_tex(pdf, root) if root is not None else None


def _pdf_for_tex(tex: Path) -> Path:
    """Where ``latexmk`` run beside ``tex`` puts the PDF."""
    for name in _LATEXMKRC:
        try:
            match = _OUT_DIR.search((tex.parent / name).read_text(encoding="utf-8"))
        except OSError:
            continue
        if match:
            return (tex.parent / match.group(2) / f"{tex.stem}.pdf").resolve()

    sibling = tex.with_suffix(".pdf")
    if sibling.is_file():
        return sibling
    # Built somewhere below by a setup we cannot read (a Makefile, -outdir on
    # the command line): trust a PDF that carries SyncTeX data, newest first.
    built = [
        path
        for pattern in (f"*/{tex.stem}.pdf", f"*/*/{tex.stem}.pdf")
        for path in tex.parent.glob(pattern)
        if synctex_data_file(path) is not None
    ]
    if built:
        return max(built, key=lambda path: path.stat().st_mtime)
    return sibling


def _common_root(*paths: Path) -> Path:
    parts = [path.parts for path in paths]
    shared: list[str] = []
    for column in zip(*parts):
        if any(part != column[0] for part in column):
            break
        shared.append(column[0])
    return Path(*shared)


def discover(
    document: str | Path | None = None,
    *,
    root: str | Path | None = None,
    pdf: str | Path | None = None,
) -> Layout:
    """The project behind ``document`` (a .tex, a .pdf, or a directory).

    With no document, ``pdf`` stands in for it, then ``root``, then the current
    directory — so the old ``--root R --pdf P`` spelling means what it did.
    """
    root_dir = resolve_root(root) if root is not None else None

    def locate(value: str | Path) -> Path:
        # Under a given root, a path may be relative to it or to the shell.
        if root_dir is not None:
            return resolve_user_path(value, root_dir)
        return Path(value).expanduser().resolve()

    target = locate(document if document is not None else pdf or root_dir or Path.cwd())
    pdf_path = locate(pdf) if pdf is not None else None

    if target.is_dir():
        tex: Path | None = _root_tex_in(target)
    elif target.suffix.lower() == ".tex":
        if not target.is_file():
            raise DiscoveryError(f"no such file: {target}")
        tex = target
    elif target.suffix.lower() == ".pdf":
        pdf_path = pdf_path or target
        tex = _tex_for_pdf(pdf_path, root_dir)
    else:
        raise DiscoveryError(f"expected a .tex or .pdf file, or a directory: {target}")

    if pdf_path is None:
        assert tex is not None  # every branch without a PDF found a .tex
        pdf_path = _pdf_for_tex(tex)

    if root_dir is None:
        root_dir = _common_root(pdf_path.parent, *([tex.parent] if tex else []))
    # Raises PathOutsideRootError for a .tex outside an explicit --root.
    if tex is not None:
        tex = ensure_inside_root(tex, root_dir)
    return Layout(root=root_dir, pdf=pdf_path, tex=tex)
