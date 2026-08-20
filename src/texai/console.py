"""What texai says on the terminal it was started from.

The browser gets the whole story — the turn, the diff, the compile errors. The
console gets the few lines that answer "is it doing anything?": a build starting,
a build finishing, and an interrupt being heard. A long silence there is
indistinguishable from a hang, which is the only reason this exists.

Flushed on every line, because this output is usually a log file: agents start
texai with stdout redirected, and a block-buffered pipe would hold everything
back until the process ended.
"""

from __future__ import annotations

import sys

__all__ = ["note", "warn"]


def note(message: str) -> None:
    """A line of ordinary progress, prefixed like the startup banner."""
    print(f"texai: {message}", flush=True)


def warn(message: str) -> None:
    """Something the user may need to act on."""
    print(f"texai: {message}", file=sys.stderr, flush=True)
