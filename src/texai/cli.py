"""Command line entry point: ``uv run texai --root DIR --pdf FILE``."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import socket
import sys
import webbrowser
from pathlib import Path

from . import __version__
from .agent import AgentSession, sdk_status
from .build import BuildError, run_build
from .config import AppConfig
from .events import EventBus
from .paths import PathOutsideRootError
from .server import create_app
from .synctex import synctex_data_file

__all__ = ["main", "build_parser", "build_missing_pdf"]

HOST = "127.0.0.1"  # loopback only, never configurable
DEFAULT_PORT = 8765
PORT_SCAN = 20


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="texai",
        description=(
            "Serve a PDF in a local PDF.js viewer and record Cmd/Ctrl-clicked "
            "locations as LaTeX source positions for a coding agent."
        ),
    )
    parser.add_argument("--root", required=True, help="project root directory")
    parser.add_argument("--pdf", required=True, help="PDF to review (must be inside --root)")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help=f"port on {HOST} (default: {DEFAULT_PORT})"
    )
    parser.add_argument(
        "--synctex", default="synctex", help="synctex executable (default: synctex)"
    )
    parser.add_argument(
        "--build-cmd",
        default=None,
        metavar="CMD",
        help=(
            "command used to rebuild the document "
            "(default: latexmk -pdf -synctex=1 -interaction=nonstopmode <root.tex>). "
            "Run without a shell, so pipes and && are not supported — use a script."
        ),
    )
    parser.add_argument(
        "--build-dir",
        default=None,
        metavar="DIR",
        help="directory to run the build in (default: the root .tex file's directory)",
    )
    parser.add_argument(
        "--model", default=None, help="model for the agent (default: the CLI's default)"
    )
    parser.add_argument("--open", action="store_true", help="open the viewer in a browser")
    parser.add_argument("--version", action="version", version=f"texai {__version__}")
    return parser


def _find_port(preferred: int) -> int:
    """Return ``preferred`` if free, else the next free port in a small range."""
    for candidate in range(preferred, preferred + PORT_SCAN):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((HOST, candidate))
            except OSError:
                continue
            return candidate
    raise SystemExit(
        f"texai: no free port in {preferred}..{preferred + PORT_SCAN - 1} on {HOST}"
    )


def build_missing_pdf(config: AppConfig) -> str | None:
    """Compile the document when its PDF is not there yet.

    A fresh clone, or a `latexmk -C`, used to be a hard error telling you to go
    and run the build yourself — a build texai already knows how to run. The
    root .tex is the one named after the PDF (`find_root_tex`), and the command
    is the same one every turn uses, so the first build is the same build.

    Returns None once the PDF is in place, or the reason it is not.
    """
    try:
        argv = config.build_argv()
    except BuildError as exc:
        return f"PDF not found: {config.pdf_path}\n{exc}"

    print(
        f"texai: {config.pdf_rel} is not there yet — building it first:\n"
        f"         {' '.join(argv)}\n"
        f"         in {config.build_dir}",
        flush=True,
    )
    try:
        result = asyncio.run(
            run_build(argv, config.build_dir, log_path=config.pdf_path.with_suffix(".log"))
        )
    except BuildError as exc:
        return str(exc)

    if not result.ok:
        return result.summary()
    # latexmk can be told to write somewhere else entirely, so a build that
    # succeeds is not proof that this is the PDF it wrote.
    if not config.pdf_path.is_file():
        return (
            f"the build succeeded but {config.pdf_rel} is still not there — "
            "check --pdf against the directory the build writes to."
        )
    print("texai: built it.", flush=True)
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = AppConfig.create(
            args.root,
            args.pdf,
            synctex_executable=args.synctex,
            build_command=args.build_cmd,
            build_dir=args.build_dir,
        )
    except NotADirectoryError as exc:
        print(f"texai: {exc}", file=sys.stderr)
        return 2
    except PathOutsideRootError as exc:
        print(
            f"texai: --pdf {exc.path} is outside --root {exc.root}",
            file=sys.stderr,
        )
        return 2

    if not config.pdf_path.is_file():
        problem = build_missing_pdf(config)
        if problem is not None:
            print(f"texai: {problem}", file=sys.stderr)
            return 2

    if shutil.which(args.synctex) is None and not Path(args.synctex).is_file():
        print(
            f"texai: warning: `{args.synctex}` not found on PATH; "
            "clicking in the PDF will report an error until TeX Live/MacTeX is installed.",
            file=sys.stderr,
        )
    if synctex_data_file(config.pdf_path) is None:
        print(
            f"texai: warning: no .synctex.gz next to {config.pdf_path.name}; "
            "recompile with `latexmk -pdf -synctex=1 -interaction=nonstopmode <root.tex>`.",
            file=sys.stderr,
        )

    port = _find_port(args.port)
    url = f"http://{HOST}:{port}/"

    available, reason = sdk_status()
    build = config.build_description or "not configured (pass --build-cmd)"

    # flush: agents commonly start this with stdout redirected to a log.
    banner = "\n".join(
        [
            f"texai {__version__}",
            f"  root:      {config.root}",
            f"  pdf:       {config.pdf_rel}",
            f"  selection: {config.selection_file}",
            f"  build:     {build}",
            f"  agent:     {'ready' if available else f'disabled — {reason}'}",
            f"  viewer:    {url}",
            "  Cmd/Ctrl-click a passage, write an instruction, send. Ctrl-C to stop.",
        ]
    )
    print(banner, flush=True)

    if args.open:
        webbrowser.open(url)

    import uvicorn  # imported late so --help stays fast

    agent = AgentSession(config, EventBus(), model=args.model)
    uvicorn.run(
        create_app(config, agent=agent),
        host=HOST,
        port=port,
        log_level="warning",
        # The viewer holds an SSE connection open indefinitely, so a graceful
        # shutdown never completes on its own. Without a timeout the first
        # Ctrl-C waits forever; cap it so the stream is cancelled and we exit.
        timeout_graceful_shutdown=2,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
