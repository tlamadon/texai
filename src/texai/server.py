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
from .config import AppConfig
from .events import EventBus, sse_stream
from .models import ChatRequest, SelectRequest, SelectResponse
from .navigate import LocateError, locate
from .paths import PathOutsideRootError, resolve_source_path, to_project_relative
from .selection import atomic_write_json, build_selection
from .synctex import (
    SyncTexDataMissing,
    SyncTexError,
    SyncTexExecutableMissing,
    SyncTexLocation,
    SyncTexNoResult,
    run_synctex_edit,
    run_synctex_view,
)
from .turns import TurnBusy, TurnController

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
) -> FastAPI:
    """Build the application for one review session.

    ``synctex_runner`` and ``agent`` are injectable so tests can exercise the
    API without a real TeX installation or a live agent session.
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
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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

    @app.post("/api/select", response_model=SelectResponse)
    async def select(payload: SelectRequest) -> SelectResponse:
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

        selection = build_selection(
            pdf=config.pdf_rel,
            page=payload.page,
            x=payload.x,
            y=payload.y,
            source_file=source_rel,
            line=location.line,
            column=location.column,
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

        return SelectResponse(
            ok=True,
            message=f"Selected {source_rel}:{location.line}",
            source=selection["source"],
            selection=selection,
        )

    # ------------------------------------------------------------------ agent

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
    async def locate_line(file: str, line: int = 1) -> dict[str, Any]:
        """Where a source line sits in the PDF.

        Backs the clickable `file:line` references in the chat panel: the
        browser asks where a line landed, then scrolls there itself.
        """
        try:
            return await asyncio.to_thread(locate, config, file, line)
        except LocateError as exc:
            raise _error(404, "not_locatable", str(exc)) from exc

    @app.get("/api/turns/{turn_id}/marks")
    async def turn_marks(turn_id: str) -> dict[str, Any]:
        """Each change in the turn, located in the rebuilt PDF.

        This is the click direction run backwards: ``synctex view`` maps the
        new source line to the boxes it produced, so the viewer can draw the
        marker over the text that actually changed.
        """
        turn = controller.get(turn_id)
        if turn is None:
            raise _error(404, "turn_not_found", f"No turn {turn_id}.")

        def locate() -> list[dict[str, Any]]:
            marks: list[dict[str, Any]] = []
            for hunk in turn.hunks:
                source = config.root / str(hunk["file"])
                boxes: list[dict[str, Any]] = []
                try:
                    boxes = [
                        box.as_dict()
                        for box in run_synctex_view(
                            config.pdf_path,
                            source,
                            int(hunk["newStart"]),
                            root=config.root,
                            executable=config.synctex_executable,
                        )
                    ]
                except SyncTexError:
                    boxes = []  # unlocatable changes still list in the panel
                marks.append(
                    {
                        **hunk,
                        "boxes": boxes,
                        "accepted": hunk["id"] in turn.accepted_hunks,
                    }
                )
            return marks

        return {"turnId": turn.id, "status": turn.status, "marks": await asyncio.to_thread(locate)}

    @app.post("/api/turns/{turn_id}/hunks/{hunk_id}/accept")
    async def accept_hunk(turn_id: str, hunk_id: str) -> dict[str, Any]:
        try:
            turn = await controller.accept_hunk(turn_id, hunk_id)
        except KeyError as exc:
            raise _error(404, "turn_not_found", f"No turn {turn_id}.") from exc
        except LookupError as exc:
            raise _error(404, "hunk_not_found", f"No change {hunk_id} in {turn_id}.") from exc
        return {"ok": True, "turn": turn.as_dict()}

    @app.post("/api/turns/{turn_id}/hunks/{hunk_id}/reject")
    async def reject_hunk(turn_id: str, hunk_id: str) -> dict[str, Any]:
        try:
            turn = await controller.reject_hunk(turn_id, hunk_id)
        except TurnBusy as exc:
            raise _error(409, "agent_busy", str(exc)) from exc
        except KeyError as exc:
            raise _error(404, "turn_not_found", f"No turn {turn_id}.") from exc
        except LookupError as exc:
            raise _error(404, "hunk_not_found", f"No change {hunk_id} in {turn_id}.") from exc
        except FileNotFoundError as exc:
            raise _error(410, "snapshot_gone", str(exc)) from exc
        return {"ok": True, "turn": turn.as_dict()}

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
