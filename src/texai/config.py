"""Runtime configuration shared by the CLI, the server and the agent loop."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .build import find_root_tex, resolve_command
from .paths import resolve_root, resolve_user_path, to_project_relative
from .selection import SELECTION_DIRNAME, selection_path

__all__ = ["AppConfig"]

SNAPSHOTS_DIRNAME = "snapshots"


@dataclass(frozen=True)
class AppConfig:
    """Validated, root-confined paths and build settings for one review session."""

    root: Path
    pdf_path: Path
    pdf_rel: str
    synctex_executable: str = "synctex"
    build_command: str | None = None
    build_dir_override: Path | None = None
    root_tex: Path | None = None

    @classmethod
    def create(
        cls,
        root: str | Path,
        pdf: str | Path,
        synctex_executable: str = "synctex",
        build_command: str | None = None,
        build_dir: str | Path | None = None,
    ) -> "AppConfig":
        resolved_root = resolve_root(root)
        pdf_path = resolve_user_path(pdf, resolved_root)
        resolved_build_dir = (
            resolve_user_path(build_dir, resolved_root) if build_dir is not None else None
        )
        return cls(
            root=resolved_root,
            pdf_path=pdf_path,
            pdf_rel=to_project_relative(pdf_path, resolved_root),
            synctex_executable=synctex_executable,
            build_command=build_command,
            build_dir_override=resolved_build_dir,
            root_tex=find_root_tex(pdf_path, resolved_root),
        )

    # ------------------------------------------------------------------ paths

    @property
    def state_dir(self) -> Path:
        return self.root / SELECTION_DIRNAME

    @property
    def selection_file(self) -> Path:
        return selection_path(self.root)

    @property
    def snapshots_dir(self) -> Path:
        return self.state_dir / SNAPSHOTS_DIRNAME

    @property
    def search_dirs(self) -> list[Path]:
        """Directories used to resolve relative SyncTeX ``Input:`` paths."""
        return list(dict.fromkeys([self.pdf_path.parent, self.root]))

    # ------------------------------------------------------------------ build

    @property
    def build_dir(self) -> Path:
        """Where the build runs: next to the root .tex unless overridden."""
        if self.build_dir_override is not None:
            return self.build_dir_override
        if self.root_tex is not None:
            return self.root_tex.parent
        return self.pdf_path.parent

    def build_argv(self) -> list[str]:
        """The build command as an argv list (never a shell string)."""
        return resolve_command(self.build_command, self.root_tex)

    @property
    def build_description(self) -> str:
        try:
            return " ".join(self.build_argv())
        except Exception:  # noqa: BLE001 - shown in the UI as "not configured"
            return ""
