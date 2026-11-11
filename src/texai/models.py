"""Request/response schemas for the local API."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "SelectRequest",
    "SourceWrite",
    "SourceMerge",
    "CompleteRequest",
    "CommitRequest",
    "SourceLocation",
    "SelectResponse",
    "SelectionRef",
    "ChatRequest",
    "PickRequest",
    "AgentSettings",
]

# A PDF page cannot exceed 200in = 14400pt; allow a little slack and reject the
# rest so a malformed client cannot push nonsense into the synctex argv.
MAX_PDF_POINT = 20_000.0
MAX_SELECTED_TEXT = 20_000
MAX_WORD_TEXT = 200
MAX_CONTEXT_WORDS = 8

# Rendered words either side of a click, used to tell repeats apart.
ContextWords = Annotated[
    list[Annotated[str, Field(max_length=MAX_WORD_TEXT)]],
    Field(max_length=MAX_CONTEXT_WORDS),
]
MAX_INSTRUCTION = 8_000
MAX_MESSAGE = 20_000
MAX_SELECTIONS = 25


class SelectRequest(BaseModel):
    """A Cmd/Ctrl-click location in the rendered PDF."""

    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1, le=100_000, description="1-based page number")
    x: float = Field(
        ge=0.0,
        le=MAX_PDF_POINT,
        allow_inf_nan=False,
        description="PDF points from the left edge of the page",
    )
    y: float = Field(
        ge=0.0,
        le=MAX_PDF_POINT,
        allow_inf_nan=False,
        description="PDF points from the top edge of the page (SyncTeX convention)",
    )
    selectedText: str | None = Field(
        default=None,
        max_length=MAX_SELECTED_TEXT,
        description="Rendered text the user had selected, if any",
    )
    word: str | None = Field(
        default=None,
        max_length=MAX_WORD_TEXT,
        description="The rendered word under the cursor, for resolving past the line",
    )
    contextBefore: ContextWords | None = Field(
        default=None, description="Rendered words just before the clicked one"
    )
    contextAfter: ContextWords | None = Field(
        default=None, description="Rendered words just after the clicked one"
    )


class SelectionRef(BaseModel):
    """One marked passage attached to a chat message.

    ``file`` must be a project-relative POSIX path; the server re-resolves it
    against the project root before it reaches the agent.
    """

    model_config = ConfigDict(extra="forbid")

    file: str = Field(min_length=1, max_length=1024)
    line: int = Field(ge=1, le=10_000_000)
    column: int = Field(default=1, ge=1, le=10_000)
    page: int | None = Field(default=None, ge=1, le=100_000)
    selectedText: str | None = Field(default=None, max_length=MAX_SELECTED_TEXT)
    word: str | None = Field(default=None, max_length=MAX_WORD_TEXT)
    instruction: str | None = Field(default=None, max_length=MAX_INSTRUCTION)
    # How many versions the user wants: 1 is "just make the best edit", more is
    # "offer that many to pick from", none leaves it to the agent.
    alternatives: int | None = Field(default=None, ge=1, le=4)

    @field_validator("file")
    @classmethod
    def _reject_traversal(cls, value: str) -> str:
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("file must be a relative path inside the project")
        return value


class ChatRequest(BaseModel):
    """A composer submission: free text plus the marked passages attached to it."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(default="", max_length=MAX_MESSAGE)
    selections: list[SelectionRef] = Field(default_factory=list, max_length=MAX_SELECTIONS)

    @model_validator(mode="after")
    def _needs_content(self) -> "ChatRequest":
        if not self.message.strip() and not self.selections:
            raise ValueError("provide a message, one or more selections, or both")
        return self


class ProposalRequest(BaseModel):
    """A ``%@propose`` choice in the source, and what to do with it: put
    version ``option`` in place, or with no option, dismiss the choice."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(min_length=1, max_length=1000)
    id: str = Field(default="", max_length=100)
    line: int | None = Field(default=None, ge=1)  # for a choice written without an id
    option: int | None = Field(default=None, ge=0, le=10)


class CommentRequest(BaseModel):
    """A comment written from the page.

    ``add`` puts a new comment above source line ``line``. The others act on the
    annotation the page showed at ``line``, which said ``said`` and was of kind
    ``kind``: ``reply`` answers it, ``resolve`` and ``reopen`` set its status,
    ``delete`` removes it with its replies.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal["add", "reply", "resolve", "reopen", "delete"]
    file: str = Field(min_length=1, max_length=1000)
    line: int = Field(ge=1)
    text: str = Field(default="", max_length=20000)
    kind: str = Field(default="comment", max_length=100)
    said: str = Field(default="", max_length=20000)
    # A comment kept in annotations.jsonl: its record, and its ID there.
    record: str = Field(default="", max_length=32)
    target: str = Field(default="", max_length=32)


class ProvenanceRequest(BaseModel):
    """A ``%@src`` to resolve: taken apart already, as the page has it from
    ``/api/paragraphs``, or as the text after ``%@src``."""

    model_config = ConfigDict(extra="forbid")

    source: dict[str, Any] | None = None
    spec: str = Field(default="", max_length=4000)


class KeepRequest(BaseModel):
    """An offer to keep in the source as a proposal, among those a turn made."""

    model_config = ConfigDict(extra="forbid")

    offer: int = Field(default=0, ge=0, le=100)


class PickRequest(BaseModel):
    """The version the user clicked, among those a turn offered."""

    model_config = ConfigDict(extra="forbid")

    offer: int = Field(default=0, ge=0, le=100)
    option: int = Field(ge=0, le=10)


class SessionSwitch(BaseModel):
    """The agent conversation to talk to from the next turn; null for a new one."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = Field(default=None, alias="sessionId", max_length=200)


class AgentSettings(BaseModel):
    """The model and effort for the agent's next turns; null for its own default."""

    model_config = ConfigDict(extra="forbid")

    model: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]*$")
    effort: str | None = Field(default=None, max_length=20)


class SourceLocation(BaseModel):
    file: str
    line: int
    column: int
    word: str | None = None


class SelectResponse(BaseModel):
    ok: bool
    message: str
    source: SourceLocation
    selection: dict[str, Any]
    why: str = ""  # why no column, when there is none


MAX_SOURCE_TEXT = 5_000_000


class SourceWrite(BaseModel):
    """An editor save: the new text plus the hash it was based on."""

    model_config = ConfigDict(extra="forbid")

    file: str = Field(min_length=1, max_length=1024)
    text: str = Field(max_length=MAX_SOURCE_TEXT)
    baseSha: str | None = Field(default=None, max_length=64)

    @field_validator("file")
    @classmethod
    def _reject_traversal(cls, value: str) -> str:
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("file must be a relative path inside the project")
        return value


class ParagraphWrite(BaseModel):
    """An in-place edit from the page: one paragraph's new text.

    ``start``/``end`` and ``original`` are what the box was opened on; the server
    checks that paragraph is still there before replacing it.
    """

    model_config = ConfigDict(extra="forbid")

    file: str = Field(min_length=1, max_length=1024)
    start: int = Field(ge=1)
    end: int = Field(ge=1)
    original: str = Field(max_length=MAX_SOURCE_TEXT)
    text: str = Field(max_length=MAX_SOURCE_TEXT)

    @field_validator("file")
    @classmethod
    def _reject_traversal(cls, value: str) -> str:
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("file must be a relative path inside the project")
        return value


class SourceMerge(BaseModel):
    """An editor buffer offered up for a three-way merge with the file on disk.

    ``baseText`` is what the editor loaded (or last saved); ``text`` is what it
    holds now. The third side is read from disk by the server.
    """

    model_config = ConfigDict(extra="forbid")

    file: str = Field(min_length=1, max_length=1024)
    text: str = Field(max_length=MAX_SOURCE_TEXT)
    baseText: str = Field(max_length=MAX_SOURCE_TEXT)

    @field_validator("file")
    @classmethod
    def _reject_traversal(cls, value: str) -> str:
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("file must be a relative path inside the project")
        return value


class CompleteRequest(BaseModel):
    """A cursor position in the editor's buffer, offered up for a completion.

    ``text`` is the live buffer (unsaved edits and all — completing against what
    is being typed is the whole point) and ``offset`` is the cursor's character
    index into it. Read-only: nothing is written, so no base hash is needed.
    """

    model_config = ConfigDict(extra="forbid")

    file: str = Field(min_length=1, max_length=1024)
    text: str = Field(max_length=MAX_SOURCE_TEXT)
    offset: int = Field(ge=0, le=MAX_SOURCE_TEXT)

    @field_validator("file")
    @classmethod
    def _reject_traversal(cls, value: str) -> str:
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("file must be a relative path inside the project")
        return value


class CommitRequest(BaseModel):
    """A commit message, as edited by the user before they pressed Commit."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
