"""Request/response schemas for the local API."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "SelectRequest",
    "SourceWrite",
    "CommitRequest",
    "SourceLocation",
    "SelectResponse",
    "SelectionRef",
    "ChatRequest",
]

# A PDF page cannot exceed 200in = 14400pt; allow a little slack and reject the
# rest so a malformed client cannot push nonsense into the synctex argv.
MAX_PDF_POINT = 20_000.0
MAX_SELECTED_TEXT = 20_000
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
    instruction: str | None = Field(default=None, max_length=MAX_INSTRUCTION)

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


class SourceLocation(BaseModel):
    file: str
    line: int
    column: int


class SelectResponse(BaseModel):
    ok: bool
    message: str
    source: SourceLocation
    selection: dict[str, Any]


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


class CommitRequest(BaseModel):
    """A commit message, as edited by the user before they pressed Commit."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
