"""Thin wrapper around the system ``synctex`` executable.

Only ``synctex edit`` (PDF -> source) is needed. The executable is always
invoked with an argument list and ``shell=False``, so nothing the user clicks
can be interpreted by a shell.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "SyncTexError",
    "SyncTexExecutableMissing",
    "SyncTexDataMissing",
    "SyncTexNoResult",
    "SyncTexLocation",
    "SyncTexBox",
    "synctex_data_file",
    "parse_synctex_edit",
    "parse_synctex_view",
    "run_synctex_edit",
    "run_synctex_view",
]

DEFAULT_TIMEOUT_SECONDS = 15.0


class SyncTexError(RuntimeError):
    """Base class for SyncTeX failures."""


class SyncTexExecutableMissing(SyncTexError):
    """The ``synctex`` executable could not be found or executed."""


class SyncTexDataMissing(SyncTexError):
    """No ``.synctex.gz`` / ``.synctex`` file next to the PDF."""


class SyncTexNoResult(SyncTexError):
    """SyncTeX ran but reported no source location for the point."""


@dataclass(frozen=True)
class SyncTexLocation:
    """A source location reported by ``synctex edit``."""

    input: str
    line: int
    column: int


@dataclass(frozen=True)
class SyncTexBox:
    """A rectangle in the PDF reported by ``synctex view``.

    ``x``/``y`` are PDF points from the **top-left** corner of the page, the
    same convention used for clicks, so the viewer can reuse one transform in
    both directions. SyncTeX reports ``v`` as the baseline, so the top edge is
    ``v - H``.
    """

    page: int
    x: float
    y: float
    width: float
    height: float

    def as_dict(self) -> dict[str, float | int]:
        return {
            "page": self.page,
            "x": round(self.x, 2),
            "y": round(self.y, 2),
            "width": round(self.width, 2),
            "height": round(self.height, 2),
        }


def synctex_data_file(pdf_path: Path) -> Path | None:
    """Return the SyncTeX data file for ``pdf_path``, or ``None`` if absent."""
    for suffix in (".synctex.gz", ".synctex"):
        candidate = pdf_path.with_name(pdf_path.stem + suffix)
        if candidate.is_file():
            return candidate
    return None


def parse_synctex_edit(output: str) -> SyncTexLocation | None:
    """Parse the stdout of ``synctex edit``.

    Typical output::

        This is SyncTeX command line utility, version 1.5
        SyncTeX result begin
        Output:build/main.pdf
        Input:/home/me/project/sections/model.tex
        Line:143
        Column:-1
        Offset:0
        Context:
        SyncTeX result end

    The first record that carries both ``Input:`` and ``Line:`` wins. SyncTeX
    reports ``Column:-1`` when it does not know the column; that is normalised
    to column 1. Returns ``None`` when the output holds no usable record.
    """
    record: dict[str, str] = {}
    records: list[dict[str, str]] = []

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("synctex result end"):
            if record:
                records.append(record)
                record = {}
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key == "input":
            # A second Input: starts a new record.
            if "input" in record:
                records.append(record)
                record = {}
            record["input"] = value.strip()
        elif key in ("line", "column"):
            record[key] = value.strip()
    if record:
        records.append(record)

    for candidate in records:
        source = candidate.get("input", "")
        if not source:
            continue
        try:
            line_number = int(candidate.get("line", ""))
        except ValueError:
            continue
        try:
            column = int(candidate.get("column", "-1"))
        except ValueError:
            column = -1
        return SyncTexLocation(
            input=source,
            line=max(1, line_number),
            column=column if column > 0 else 1,
        )
    return None


def run_synctex_edit(
    pdf_path: Path,
    page: int,
    x: float,
    y: float,
    executable: str = "synctex",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> SyncTexLocation:
    """Run ``synctex edit`` for a point on a page and return the source location.

    ``x`` and ``y`` are PDF points measured from the **top-left** corner of the
    page, which is the convention SyncTeX uses.
    """
    if not pdf_path.is_file():
        raise SyncTexDataMissing(f"PDF not found: {pdf_path}")
    if synctex_data_file(pdf_path) is None:
        raise SyncTexDataMissing(
            f"No SyncTeX data next to {pdf_path.name}. "
            "Recompile with SyncTeX enabled, e.g. "
            "`latexmk -pdf -synctex=1 -interaction=nonstopmode <root.tex>`."
        )

    spec = f"{int(page)}:{x:.3f}:{y:.3f}:{pdf_path}"
    argv = [executable, "edit", "-o", spec]
    try:
        completed = subprocess.run(  # noqa: S603 - argv list, shell=False
            argv,
            cwd=str(pdf_path.parent),
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SyncTexExecutableMissing(
            f"`{executable}` executable not found. Install TeX Live/MacTeX "
            "(the `synctex` binary ships with it) or pass --synctex /path/to/synctex."
        ) from exc
    except PermissionError as exc:
        raise SyncTexExecutableMissing(f"`{executable}` is not executable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SyncTexError(f"`{executable} edit` timed out after {timeout:g}s") from exc

    location = parse_synctex_edit(completed.stdout)
    if location is None:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        hint = f" ({detail[-1]})" if detail else ""
        raise SyncTexNoResult(
            f"SyncTeX found no source location for page {page} at "
            f"({x:.1f}, {y:.1f}){hint}"
        )
    return location


def parse_synctex_view(output: str) -> list[SyncTexBox]:
    """Parse the stdout of ``synctex view`` into page rectangles.

    One source line usually yields several boxes (one per rendered line box),
    and they may sit on different pages when a paragraph straddles a break.
    Records missing a page or a box are skipped rather than guessed at.
    """
    boxes: list[SyncTexBox] = []
    record: dict[str, float] = {}

    def flush() -> None:
        if {"page", "h", "v", "W", "H"} <= record.keys():
            width, height = record["W"], record["H"]
            if width > 0 and height > 0:
                boxes.append(
                    SyncTexBox(
                        page=int(record["page"]),
                        x=record["h"],
                        y=record["v"] - height,  # v is the baseline
                        width=width,
                        height=height,
                    )
                )
        record.clear()

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.lower().startswith("synctex result end"):
            flush()
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key == "Page":
            flush()  # a new Page starts a new record
            key = "page"
        if key in ("page", "h", "v", "W", "H"):
            try:
                record[key] = float(value.strip())
            except ValueError:
                continue
    flush()
    return boxes


def run_synctex_view(
    pdf_path: Path,
    source: Path,
    line: int,
    column: int = 1,
    root: Path | None = None,
    executable: str = "synctex",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[SyncTexBox]:
    """Map a source line forward to its rectangles in the PDF.

    SyncTeX matches the input path against the names recorded at compile time,
    which may be absolute or relative to the build directory, so both spellings
    are tried before giving up.
    """
    if not pdf_path.is_file():
        raise SyncTexDataMissing(f"PDF not found: {pdf_path}")
    if synctex_data_file(pdf_path) is None:
        raise SyncTexDataMissing(f"No SyncTeX data next to {pdf_path.name}.")

    cwd = pdf_path.parent
    spellings: list[str] = [str(source)]
    for base in (cwd, root):
        if base is None:
            continue
        try:
            spellings.append(source.relative_to(base).as_posix())
        except ValueError:
            continue

    last_error: Exception | None = None
    for spelling in dict.fromkeys(spellings):
        argv = [executable, "view", "-i", f"{int(line)}:{int(column)}:{spelling}", "-o", str(pdf_path)]
        try:
            completed = subprocess.run(  # noqa: S603 - argv list, shell=False
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SyncTexExecutableMissing(
                f"`{executable}` executable not found. Install TeX Live/MacTeX."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            last_error = exc
            continue

        boxes = parse_synctex_view(completed.stdout)
        if boxes:
            # SyncTeX often reports the same box twice for one line.
            seen: dict[tuple[int, int, int, int, int], SyncTexBox] = {}
            for box in boxes:
                key = (
                    box.page,
                    round(box.x),
                    round(box.y),
                    round(box.width),
                    round(box.height),
                )
                seen.setdefault(key, box)
            return list(seen.values())

    if isinstance(last_error, subprocess.TimeoutExpired):
        raise SyncTexError(f"`{executable} view` timed out after {timeout:g}s")
    return []
