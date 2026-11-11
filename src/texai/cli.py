"""Command line entry point: ``texai paper.tex`` (or the PDF it builds)."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import socket
import sys
import webbrowser
from pathlib import Path
from typing import Any

from . import __version__
from .agent import AGENT_KINDS, EFFORT_LEVELS, agent_status, create_agent
from .agent_tools import tools_url
from .codex_agent import CODEX_EFFORT_LEVELS
from .build import BuildError, detect_engine, run_build, running_builds
from .config import AppConfig
from .console import note, warn
from .discover import DiscoveryError, discover
from .events import EventBus
from .paths import PathOutsideRootError
from .server import create_app
from .synctex import synctex_data_file
from .quick import QuickSession
from .sessions import last_session
from .timing import timings_path
from .update import UpdateCheck

__all__ = ["main", "build_parser", "build_missing_pdf", "pending_work"]

HOST = "127.0.0.1"  # loopback only, never configurable
DEFAULT_PORT = 8765
PORT_SCAN = 20


# `--resume` on its own: whichever conversation texai had here last.
LAST_SESSION = "last"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="texai",
        description=(
            "Serve a PDF in a local PDF.js viewer and record Cmd/Ctrl-clicked "
            "locations as LaTeX source positions for a coding agent."
        ),
    )
    parser.add_argument(
        "document",
        nargs="?",
        default=None,
        help=(
            "the .tex to work on, or the .pdf it builds, or a directory holding one "
            "(default: the document in the current directory). Everything else is "
            "inferred from it."
        ),
    )
    parser.add_argument(
        "--root",
        default=None,
        help="project root; everything stays inside it (default: the document's directory)",
    )
    parser.add_argument(
        "--pdf",
        default=None,
        help="PDF to review (default: where latexmk writes it, next to the .tex)",
    )
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
        "--agent",
        choices=AGENT_KINDS,
        default=AGENT_KINDS[0],
        help=(
            "which coding agent edits the document (default: claude). codex needs the "
            "optional extra: texai[codex]"
        ),
    )
    parser.add_argument(
        "--model", default=None, help="model for the agent (default: the agent's own default)"
    )
    parser.add_argument(
        "--effort",
        default=None,
        metavar="LEVEL",
        help=(
            "how hard the agent thinks before it answers; lower is faster "
            f"(default: the agent's own). Claude: {', '.join(EFFORT_LEVELS)}; "
            f"Codex: {', '.join(CODEX_EFFORT_LEVELS)}"
        ),
    )
    parser.add_argument(
        "--no-update-check",
        action="store_true",
        help="do not ask PyPI whether a newer texai is out",
    )
    parser.add_argument(
        "--no-quick",
        action="store_true",
        help=(
            "send requests about one marked passage to the agent too, instead of "
            "the quick editor that answers them in one model call (Claude only)"
        ),
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const=LAST_SESSION,
        default=None,
        metavar="SESSION",
        help=(
            "carry on an earlier agent conversation instead of starting a new one: "
            "the last one texai had here, or the Claude Code / Codex session id given"
        ),
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

    note(f"{config.pdf_rel} is not there yet, and {config.build_dir} is where it is built")
    try:
        result = asyncio.run(
            run_build(
                argv,
                config.build_dir,
                log_path=config.pdf_path.with_suffix(".log"),
                reason="the first build",
            )
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
    return None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.resume and args.document is None and Path(args.resume).exists():
        # `texai --resume main.tex`: the optional id swallowed the document.
        args.document, args.resume = args.resume, LAST_SESSION
    levels = CODEX_EFFORT_LEVELS if args.agent == "codex" else EFFORT_LEVELS
    if args.effort and args.effort not in levels:
        parser.error(f"--effort for {args.agent} must be one of: {', '.join(levels)}")

    try:
        layout = discover(args.document, root=args.root, pdf=args.pdf)
        config = AppConfig.create(
            layout.root,
            layout.pdf,
            synctex_executable=args.synctex,
            build_command=args.build_cmd,
            build_dir=args.build_dir,
            root_tex=layout.tex,
        )
    except (DiscoveryError, NotADirectoryError) as exc:
        print(f"texai: {exc}", file=sys.stderr)
        return 2
    except PathOutsideRootError as exc:
        print(
            f"texai: {exc.path} is outside the project root {exc.root} — pass --root "
            "a directory that holds both the .tex and the PDF.",
            file=sys.stderr,
        )
        return 2

    if not config.pdf_path.is_file():
        problem = build_missing_pdf(config)
        if problem is not None:
            print(f"texai: {problem}", file=sys.stderr)
            return 2

    if shutil.which(args.synctex) is None and not Path(args.synctex).is_file():
        warn(
            f"warning: `{args.synctex}` not found on PATH; "
            "clicking in the PDF will report an error until TeX Live/MacTeX is installed."
        )
    if synctex_data_file(config.pdf_path) is None:
        warn(
            f"warning: no .synctex.gz next to {config.pdf_path.name}; "
            "recompile with `latexmk -pdf -synctex=1 -interaction=nonstopmode <root.tex>`."
        )

    port = _find_port(args.port)
    url = f"http://{HOST}:{port}/"

    available, reason = agent_status(args.agent)
    build = config.build_description or "not configured (pass --build-cmd)"
    if config.build_description and not config.build_command and config.root_tex:
        # Say why when it is not pdfLaTeX: a surprise engine should explain itself.
        _engine, why = detect_engine(config.root_tex)
        if why:
            build += f"  ({why})"

    resume = args.resume
    if resume == LAST_SESSION:
        resume = last_session(config.root, args.agent)
        if resume is None:
            warn("warning: no earlier conversation here to resume; starting a new one.")
    session = f"resuming {resume}" if resume else "new (switch from the chat panel)"

    # flush: agents commonly start this with stdout redirected to a log.
    banner = "\n".join(
        [
            f"texai {__version__}",
            f"  root:      {config.root}",
            f"  tex:       {_relative(config.root_tex, config.root)}",
            f"  pdf:       {config.pdf_rel}",
            f"  selection: {config.selection_file}",
            f"  build:     {build}",
            f"  agent:     {args.agent}{_agent_settings(args)} — "
            f"{'ready' if available else f'disabled: {reason}'}",
            f"  session:   {session}",
            f"  timings:   {timings_path(config.root)}",
            f"  viewer:    {url}",
            "  Cmd/Ctrl-click a passage, write an instruction, send. Ctrl-C to stop.",
        ]
    )
    print(banner, flush=True)

    if args.open:
        webbrowser.open(url)

    import uvicorn  # imported late so --help stays fast

    agent = create_agent(
        args.agent,
        config,
        EventBus(),
        model=args.model,
        tools_url=tools_url(HOST, port),
        effort=args.effort,
    )
    if resume:
        agent.resume(resume)
    quick = None
    if args.agent == "claude" and not args.no_quick:
        quick = QuickSession(config, agent.bus, model=args.model, effort=args.effort)
    update_check = None if args.no_update_check else UpdateCheck(__version__)
    app = create_app(config, agent=agent, quick=quick, update_check=update_check)
    server = _announcing_server(uvicorn, app, port)
    server.run()
    # Uvicorn cancels whatever request was in flight on the way out, which can
    # print a traceback of its own. A last line makes it plain that the noise
    # above was the shutdown finishing rather than something going wrong.
    note("stopped.")
    return 0


def _agent_settings(args: argparse.Namespace) -> str:
    """The model and effort asked for, when either was."""
    bits = [f"model {args.model}" if args.model else "", f"effort {args.effort}" if args.effort else ""]
    shown = ", ".join(bit for bit in bits if bit)
    return f" ({shown})" if shown else ""


def _relative(path: Path | None, root: Path) -> str:
    if path is None:
        return "not found (pass --build-cmd to rebuild)"
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def pending_work(app: Any) -> str:
    """What is in flight, phrased for the line an interrupt prints."""
    parts: list[str] = []
    builds = running_builds()
    if builds:
        parts.append(f"a build has been running for {builds[0]:.0f}s")
    controller = getattr(getattr(app, "state", None), "turns", None)
    if controller is not None and getattr(controller, "busy", False):
        parts.append("the agent is mid-turn")
    return ", ".join(parts)


def _announcing_server(uvicorn: Any, app: Any, port: int) -> Any:
    """A server that answers Ctrl-C out loud.

    Interrupting a process that has been quiet for an hour is an act of faith:
    nothing acknowledges the keystroke, and a compile in flight can hold the
    exit for a few seconds — long enough to wonder whether it was heard at all.
    So the first interrupt says it was, and what it is waiting for; the second
    stops waiting.
    """

    class AnnouncingServer(uvicorn.Server):
        def handle_exit(self, sig: int, frame: Any) -> None:
            if self.should_exit:
                note("stopping now.")
            else:
                pending = pending_work(app)
                note(
                    f"interrupt received — stopping{f' (waiting: {pending})' if pending else ''}."
                    " Ctrl-C again to stop at once."
                )
            super().handle_exit(sig, frame)

    return AnnouncingServer(
        uvicorn.Config(
            app,
            host=HOST,
            port=port,
            log_level="warning",
            # The viewer holds an SSE connection open indefinitely, so a graceful
            # shutdown never completes on its own. Without a timeout the first
            # Ctrl-C waits forever; cap it so the stream is cancelled and we exit.
            timeout_graceful_shutdown=2,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
