"""FastAPI application: serves the viewer and turns clicks into source locations."""

from __future__ import annotations

import asyncio
import json
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .agent import AgentSession, AgentUnavailable, sdk_status
from .commitmsg import propose_message
from .config import AppConfig
from .events import EventBus, sse_stream
from .git import GitError
from .git import commit as git_commit
from .git import fetch_remote
from .git import probe as git_probe
from .git import pull_rebase as git_pull_rebase
from .git import push as git_push
from .git import scoped_diff
from .merge import merge3, normalize_newlines
from .models import (
    ChatRequest,
    CommitRequest,
    SelectRequest,
    SelectResponse,
    SourceMerge,
    SourceWrite,
)
from .navigate import LocateError, locate, locate_forward, locate_range
from .paths import PathOutsideRootError, resolve_source_path, to_project_relative
from .selection import atomic_write_json, build_selection
from .source import (
    SourceConflict,
    SourceError,
    list_sources,
    read_source,
    write_source,
)
from .synctex import (
    SyncTexDataMissing,
    SyncTexError,
    SyncTexExecutableMissing,
    SyncTexLocation,
    SyncTexNoResult,
    run_synctex_edit,
)
from .turns import TurnBusy, TurnController
from .words import locate as locate_word_detail

__all__ = ["create_app", "normalize_selected_text", "pdf_version_tag"]

# Browsers refuse ES modules served with a non-JavaScript MIME type, and .mjs is
# not in every platform's mime database.
mimetypes.add_type("text/javascript", ".mjs")

STATIC_DIR = Path(__file__).parent / "static"
ALLOWED_HOSTNAMES = {"127.0.0.1", "localhost", "::1", "[::1]"}
NO_STORE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}

SyncTexRunner = Callable[..., SyncTexLocation]


def normalize_selected_text(text: str | None) -> str | None:
    """Collapse the whitespace PDF.js text layers sprinkle into selections."""
    if text is None:
        return None
    collapsed = " ".join(text.split())
    return collapsed or None


def pdf_version_tag(pdf_path: Path) -> str | None:
    """A cheap change token for the PDF: mtime + size, or ``None`` if absent."""
    try:
        stat = pdf_path.stat()
    except OSError:
        return None
    return f"{int(stat.st_mtime_ns)}-{stat.st_size}"


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"error": code, "message": message})


def create_app(
    config: AppConfig,
    synctex_runner: SyncTexRunner = run_synctex_edit,
    agent: AgentSession | None = None,
    message_writer: Callable[..., Any] = propose_message,
) -> FastAPI:
    """Build the application for one review session.

    ``synctex_runner``, ``agent`` and ``message_writer`` are injectable so tests
    can exercise the API without a real TeX installation or a live agent.
    """
    # An injected agent brings its own bus; everything must publish to the same one.
    bus = agent.bus if agent is not None else EventBus()
    agent_session = agent if agent is not None else AgentSession(config, bus)
    controller = TurnController(config, bus, agent_session)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await controller.shutdown()
        await agent_session.stop()

    app = FastAPI(
        title="texai",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.config = config
    app.state.bus = bus
    app.state.agent = agent_session
    app.state.turns = controller
    class RevalidatingStatic(StaticFiles):
        """Static files the browser must check before reusing.

        Without an explicit Cache-Control, browsers fall back to heuristic
        caching and can serve a stale script without asking. That produces the
        worst possible failure for a tool that updates in place: half the page
        running new code and half running old, with errors like
        "viewer.wordAtClientPoint is not a function". `no-cache` still lets the
        browser keep a copy — it just has to revalidate, and the ETag makes that
        a 304 costing nothing.
        """

        def file_response(self, *args: Any, **kwargs: Any) -> Response:
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

    app.mount("/static", RevalidatingStatic(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def loopback_only(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Reject non-loopback Host headers (blunts DNS-rebinding attempts)."""
        hostname = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if hostname and hostname not in {h.strip("[]") for h in ALLOWED_HOSTNAMES}:
            return JSONResponse(
                status_code=403,
                content={"detail": {"error": "forbidden_host", "message": "loopback only"}},
            )
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Report validation failures without echoing the raw input.

        The default handler includes the offending value, which cannot always be
        serialised back to JSON (``NaN`` and ``Infinity`` parse in but do not
        encode out), turning a 422 into a 500.
        """
        errors = [
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers=NO_STORE)

    @app.get("/api/info")
    async def info() -> dict[str, Any]:
        available, reason = sdk_status()
        return {
            "version": __version__,
            "pdf": config.pdf_rel,
            "root": str(config.root),
            "selectionFile": to_project_relative(config.selection_file, config.root),
            "pdfVersion": pdf_version_tag(config.pdf_path),
            "rootTex": (
                to_project_relative(config.root_tex, config.root) if config.root_tex else None
            ),
            "buildCommand": config.build_description,
            "agent": {
                "available": available,
                "reason": reason,
                "running": agent_session.running,
                "busy": controller.busy,
                "sessionId": agent_session.session_id,
                # Lets you attach a real terminal to the very same conversation.
                "resumeCommand": agent_session.resume_command(),
            },
        }

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        tag = pdf_version_tag(config.pdf_path)
        return {"exists": tag is not None, "pdfVersion": tag}

    @app.get("/api/pdf")
    async def pdf() -> Response:
        if not config.pdf_path.is_file():
            raise _error(404, "pdf_missing", f"PDF not found: {config.pdf_rel}")
        return FileResponse(
            config.pdf_path,
            media_type="application/pdf",
            headers=NO_STORE,
        )

    @app.get("/api/selection")
    async def get_selection() -> dict[str, Any]:
        path = config.selection_file
        if not path.is_file():
            raise _error(404, "no_selection", "No selection recorded yet.")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise _error(500, "selection_unreadable", f"Cannot read {path}: {exc}") from exc

    def _pinpoint(
        payload: SelectRequest, source_path: Path, line: int, relative: str
    ) -> tuple[int, int, str | None, str]:
        """Sharpen SyncTeX's line to a column, when the clicked word can be found.

        SyncTeX reports Column:-1 for every engine in practice, so the precision
        comes from the browser: it knows which word was under the cursor. If the
        word cannot be found in the source with confidence, the line stands on
        its own exactly as before.
        """
        phrase = normalize_selected_text(payload.selectedText) or payload.word
        if not phrase:
            return line, 1, None, "the click was not on any text"
        # A long selection is a poor needle; its opening words are enough.
        phrase = " ".join(phrase.split()[:6])

        try:
            text = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return line, 1, None, f"could not read {relative}: {exc}"

        found = locate_word_detail(
            text.splitlines(),
            line,
            phrase,
            payload.contextBefore or [],
            payload.contextAfter or [],
        )
        if found.hit is None:
            return line, 1, None, found.explain(phrase, relative, line)
        return found.hit.line, found.hit.column, found.hit.text, ""

    def _synctex_source(payload: SelectRequest) -> dict[str, Any]:
        """Map a click to a source location, with no side effects."""
        if not config.pdf_path.is_file():
            raise _error(404, "pdf_missing", f"PDF not found: {config.pdf_rel}")
        try:
            location = synctex_runner(
                config.pdf_path,
                payload.page,
                payload.x,
                payload.y,
                executable=config.synctex_executable,
            )
        except SyncTexExecutableMissing as exc:
            raise _error(503, "synctex_missing", str(exc)) from exc
        except SyncTexDataMissing as exc:
            raise _error(409, "synctex_data_missing", str(exc)) from exc
        except SyncTexNoResult as exc:
            raise _error(404, "synctex_no_result", str(exc)) from exc
        except SyncTexError as exc:
            raise _error(500, "synctex_failed", str(exc)) from exc

        try:
            source_path = resolve_source_path(location.input, config.root, config.search_dirs)
            source_rel = to_project_relative(source_path, config.root)
        except PathOutsideRootError as exc:
            raise _error(
                422,
                "source_outside_root",
                f"SyncTeX resolved to {exc.path}, which is outside the project root "
                f"{config.root}.",
            ) from exc
        except ValueError as exc:
            raise _error(422, "source_unresolvable", str(exc)) from exc

        line, column, word, why = _pinpoint(payload, source_path, location.line, source_rel)
        # `why` is empty on success; it exists so a fallback to the line can say
        # what stopped it, instead of looking like the feature is not running.
        return {"file": source_rel, "line": line, "column": column, "word": word, "why": why}

    @app.post("/api/resolve")
    async def resolve(payload: SelectRequest) -> dict[str, Any]:
        """Where in the source a point on the page came from.

        The same lookup as /api/select but without recording anything — used to
        open the editor at a spot without disturbing the selection file.
        """
        return _synctex_source(payload)

    @app.post("/api/select", response_model=SelectResponse)
    async def select(payload: SelectRequest) -> SelectResponse:
        where = _synctex_source(payload)

        selection = build_selection(
            pdf=config.pdf_rel,
            page=payload.page,
            x=payload.x,
            y=payload.y,
            source_file=where["file"],
            line=where["line"],
            column=where["column"],
            word=where["word"],
            selected_text=normalize_selected_text(payload.selectedText),
        )
        try:
            atomic_write_json(config.selection_file, selection)
        except OSError as exc:
            raise _error(
                500,
                "selection_write_failed",
                f"Could not write {config.selection_file}: {exc}",
            ) from exc

        where_text = f"{where['file']}:{where['line']}"
        if where["word"]:
            where_text += f":{where['column']}"
        return SelectResponse(
            ok=True,
            message=f"Selected {where_text}",
            source=selection["source"],
            selection=selection,
            why=where["why"],
        )

    @app.get("/api/events")
    async def events(since: int = 0) -> StreamingResponse:
        """Server-Sent Events for agent and build activity.

        SSE has no replay, so a reconnecting client passes the last id it saw
        and gets the backlog before the live tail.
        """
        return StreamingResponse(
            sse_stream(bus, since),
            media_type="text/event-stream",
            headers={**NO_STORE, "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.post("/api/chat")
    async def chat(payload: ChatRequest) -> dict[str, Any]:
        # Re-resolve every referenced path against the root; the client is not
        # trusted just because it is local.
        for selection in payload.selections:
            try:
                resolved = config.root / selection.file
                to_project_relative(resolved, config.root)
            except PathOutsideRootError as exc:
                raise _error(
                    422,
                    "selection_outside_root",
                    f"Selection {selection.file} is outside the project root.",
                ) from exc

        try:
            turn = await controller.submit(payload.message, payload.selections)
        except TurnBusy as exc:
            raise _error(409, "agent_busy", str(exc)) from exc
        except AgentUnavailable as exc:
            raise _error(503, "agent_unavailable", str(exc)) from exc
        return {"ok": True, "turn": turn.as_dict()}

    @app.post("/api/interrupt")
    async def interrupt() -> dict[str, Any]:
        try:
            await agent_session.interrupt()
        except AgentUnavailable as exc:
            raise _error(503, "agent_unavailable", str(exc)) from exc
        return {"ok": True}

    @app.get("/api/turns")
    async def list_turns() -> dict[str, Any]:
        return {
            "busy": controller.busy,
            "turns": [turn.as_dict() for turn in controller.turns],
        }

    @app.get("/api/turns/{turn_id}")
    async def get_turn(turn_id: str) -> dict[str, Any]:
        turn = controller.get(turn_id)
        if turn is None:
            raise _error(404, "turn_not_found", f"No turn {turn_id}.")
        return turn.as_dict(include_diff=True)

    @app.get("/api/locate")
    async def locate_line(file: str, line: int = 1, scan: int = 0) -> dict[str, Any]:
        """Where a source line sits in the PDF.

        Backs the clickable `file:line` references in the chat panel: the
        browser asks where a line landed, then scrolls there itself.

        With `scan`, a line that produced nothing yields to the next one that
        did, up to that many lines further on — what scroll sync asks, since
        the line at the top of an editor is as likely as not to be blank.
        """
        try:
            if scan:
                return await asyncio.to_thread(locate_forward, config, file, line, scan)
            return await asyncio.to_thread(locate, config, file, line)
        except LocateError as exc:
            raise _error(404, "not_locatable", str(exc)) from exc

    # ------------------------------------------------------------------ git

    # One git operation at a time: two concurrent commits, or a commit racing a
    # rebase, would fight over the index.
    git_lock = asyncio.Lock()

    async def _git_status() -> dict[str, Any]:
        return (await asyncio.to_thread(git_probe, config)).as_dict()

    async def _git_action(action: Callable[[], dict[str, Any]], event: str) -> dict[str, Any]:
        if git_lock.locked():
            raise _error(409, "git_busy", "Another git operation is still running.")
        async with git_lock:
            try:
                result = await asyncio.to_thread(action)
            except GitError as exc:
                raise _error(409, "git_failed", str(exc)) from exc
        status = await _git_status()
        bus.publish("git_changed", action=event, **{k: v for k, v in status.items() if k != "files"})
        return {"ok": True, **result, "status": status}

    @app.get("/api/git/status")
    async def git_status(fetch: bool = False) -> dict[str, Any]:
        """Where git stands.

        ``fetch=1`` refreshes remote-tracking refs first, so "behind" is not
        stale. It is rate-limited inside, and a failure there is ignored: the
        local half of the answer is still worth having.
        """
        if fetch:
            try:
                await asyncio.to_thread(fetch_remote, config)
            except GitError:
                pass
        return await _git_status()

    @app.post("/api/git/message")
    async def git_message() -> dict[str, Any]:
        """Ask the agent for a commit message. Never fails: it falls back to a summary."""
        status = await asyncio.to_thread(git_probe, config)
        if not status.repo:
            raise _error(409, "git_unavailable", status.reason or "Not a git repository.")
        if not status.files:
            raise _error(409, "git_clean", "Nothing to commit.")

        diff = await asyncio.to_thread(scoped_diff, config)
        proposed = await message_writer(config, status, diff, agent_session.model)
        return {**proposed, "dirty": status.dirty}

    @app.post("/api/git/commit")
    async def git_commit_route(payload: CommitRequest) -> dict[str, Any]:
        return await _git_action(lambda: git_commit(config, payload.message), "commit")

    @app.post("/api/git/pull")
    async def git_pull_route() -> dict[str, Any]:
        return await _git_action(lambda: git_pull_rebase(config), "pull")

    @app.post("/api/git/push")
    async def git_push_route() -> dict[str, Any]:
        return await _git_action(lambda: git_push(config), "push")

    # -------------------------------------------------------------- editing

    @app.get("/api/source/files")
    async def source_files() -> dict[str, Any]:
        return {"files": list_sources(config), "rootTex": (
            to_project_relative(config.root_tex, config.root) if config.root_tex else None
        )}

    @app.get("/api/source")
    async def get_source(file: str) -> dict[str, Any]:
        try:
            return read_source(config, file)
        except SourceError as exc:
            raise _error(404, "source_unavailable", str(exc)) from exc

    @app.post("/api/source/merge")
    async def merge_source(payload: SourceMerge) -> dict[str, Any]:
        """Fold whatever is on disk now into the editor's buffer.

        Read-only, and deliberately allowed while the agent is running: this is
        exactly when the file underneath the editor is moving. The answer is a
        list of splices into the buffer the caller sent, so the editor can keep
        the cursor where it is and colour the lines that arrived.
        """
        try:
            disk = read_source(config, payload.file)
        except SourceError as exc:
            raise _error(404, "source_unavailable", str(exc)) from exc

        merged = merge3(payload.baseText, payload.text, disk["text"])
        # The base handed back is normalised, because that is what the buffer
        # holds and what the next merge will be measured against. The hash is
        # of the file as it really is on disk, since that is what a save is
        # checked against.
        base = normalize_newlines(disk["text"])
        return {
            "file": disk["file"],
            "sha": disk["sha"],
            "base": base,
            "changed": base != normalize_newlines(payload.baseText),
            "clean": merged.clean,
            "edits": [edit.as_dict() for edit in merged.edits],
        }

    @app.post("/api/source")
    async def put_source(payload: SourceWrite) -> dict[str, Any]:
        """Save an edit and recompile.

        The save is folded into the review baseline, so hand edits do not show
        up as agent changes awaiting review.
        """
        if controller.busy:
            raise _error(409, "agent_busy", "The agent is editing right now; try again in a moment.")
        try:
            written = write_source(config, payload.file, payload.text, payload.baseSha)
        except SourceConflict as exc:
            raise _error(409, "source_conflict", str(exc)) from exc
        except SourceError as exc:
            raise _error(422, "source_unwritable", str(exc)) from exc

        controller.absorb_manual_edit(written["file"])
        bus.publish("source_saved", file=written["file"])

        result = await controller.rebuild()
        build = {
            "ok": bool(result and result.ok),
            "errors": list(result.errors) if result else [],
            "summary": result.summary() if result else "No build command configured.",
        }
        bus.publish("build_finished", turnId=None, attempt=1, ok=build["ok"],
                    errors=build["errors"], summary=build["summary"])
        return {"ok": True, **written, "build": build}

    @app.get("/api/changes")
    async def session_changes() -> dict[str, Any]:
        """Every change pending review, located in the rebuilt PDF.

        Session-scoped rather than per-turn: a change made three turns ago is
        still shown, and is still described against the state the review
        started from, so accepting or rejecting it cannot disturb later work.
        """
        changes = controller.session_changes()

        def locate_all() -> list[dict[str, Any]]:
            marks: list[dict[str, Any]] = []
            for change in changes:
                boxes: list[dict[str, Any]] = []
                try:
                    boxes = locate_range(
                        config,
                        str(change["file"]),
                        int(change["newStart"]),
                        int(change.get("newEnd") or change["newStart"]),
                    )["boxes"]
                except (LocateError, SyncTexError):
                    boxes = []  # unlocatable changes still list in the panel
                marks.append({**change, "boxes": boxes, "accepted": change["status"] == "accepted"})
            return marks

        return {
            "pending": sum(1 for c in changes if c["status"] == "pending"),
            "accepted": sum(1 for c in changes if c["status"] == "accepted"),
            "marks": await asyncio.to_thread(locate_all),
        }

    @app.post("/api/changes/accept-all")
    async def accept_all_changes() -> dict[str, Any]:
        count = await controller.accept_all()
        return {"ok": True, "accepted": count}

    @app.post("/api/changes/{hunk_id}/accept")
    async def accept_change(hunk_id: str) -> dict[str, Any]:
        try:
            await controller.accept_change(hunk_id)
        except LookupError as exc:
            raise _error(404, "change_not_found", f"No pending change {hunk_id}.") from exc
        return {"ok": True}

    @app.post("/api/changes/{hunk_id}/reject")
    async def reject_change(hunk_id: str) -> dict[str, Any]:
        try:
            await controller.reject_change(hunk_id)
        except TurnBusy as exc:
            raise _error(409, "agent_busy", str(exc)) from exc
        except LookupError as exc:
            raise _error(404, "change_not_found", f"No pending change {hunk_id}.") from exc
        return {"ok": True}

    @app.post("/api/turns/{turn_id}/revert")
    async def revert_turn(turn_id: str) -> dict[str, Any]:
        try:
            turn = await controller.revert(turn_id)
        except TurnBusy as exc:
            raise _error(409, "agent_busy", str(exc)) from exc
        except KeyError as exc:
            raise _error(404, "turn_not_found", f"No turn {turn_id}.") from exc
        except FileNotFoundError as exc:
            raise _error(410, "snapshot_gone", str(exc)) from exc
        return {"ok": True, "turn": turn.as_dict()}

    return app
