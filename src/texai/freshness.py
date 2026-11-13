"""Is the PDF older than what it is built from?

texai rebuilds after everything it does to the source itself: a turn, a save,
an edit on the page. What it did not notice is the source changing behind its
back. A ``git pull``, a co-author's commit, an hour in another editor, a paper
opened the morning after it was last compiled: the PDF on screen is then of a
document that no longer exists, clicks resolve to lines that have moved, and
nothing says so.

So the question is asked, at startup and every couple of seconds after: is any
source newer than the PDF? If one is, and nothing else is already building, the
document is rebuilt.

It is asked of modification times, which is cheap and slightly generous: a
``.tex`` in the project that the document never reads counts too. The cost of
that is one build that ``latexmk`` finds nothing to do in. What must not happen
is the same change setting off a build every two seconds, which a build that
fails, or one that leaves the PDF alone, would do if the PDF's own time were
the only thing compared against. Hence the rule below: a change is acted on
once, and anything that built since it happened has dealt with it.
"""

from __future__ import annotations

import time
from pathlib import Path

from .build import last_build_finished_ns
from .config import AppConfig
from .paths import to_project_relative
from .snapshots import SOURCE_SUFFIXES, iter_source_files
from .synctex import synctex_data_file

__all__ = ["Freshness", "POLL_SECONDS"]

POLL_SECONDS = 2.0
# A source written this recently is still being written: an editor saving
# several files, a checkout halfway through. Wait for it to settle.
QUIET_SECONDS = 1.0
# annotations.jsonl is kept beside the paper but is no part of the build.
_BUILT_FROM = SOURCE_SUFFIXES - {".jsonl"}
# How much older than its PDF a SyncTeX file may be and still be the same run's.
SYNC_SLACK_SECONDS = 30.0


class Freshness:
    """Whether the PDF is behind its sources, asked as often as you like."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        # The newest source time already dealt with, by a build of ours or by
        # an edit texai knows needs none (a comment's marker moves no line).
        self._handled_ns = 0
        # The PDF, by its time, whose SyncTeX data was last found wanting.
        self._unsynced_for = 0

    def _newest(self) -> tuple[int, Path | None]:
        newest, which = 0, None
        for path in iter_source_files(self.config.root):
            if path.suffix.lower() not in _BUILT_FROM:
                continue
            try:
                modified = path.stat().st_mtime_ns
            except OSError:
                continue
            if modified > newest:
                newest, which = modified, path
        return newest, which

    def stale(self, quiet: float = QUIET_SECONDS) -> str | None:
        """The source that makes the PDF out of date, or ``None`` when it is
        current, or its newest source is still settling.

        Asking does not change the answer; :meth:`handled` does.
        """
        newest, which = self._newest()
        if which is None or newest <= self._handled_ns:
            return None
        # A time in the future (another machine's clock, a restored backup) is
        # not "still being written", and must not hold the build off for good.
        if 0 <= time.time_ns() - newest < quiet * 1e9:
            return None
        if newest <= last_build_finished_ns():
            return None  # something has built since: it saw this change
        try:
            built = self.config.pdf_path.stat().st_mtime_ns
        except OSError:
            built = 0  # no PDF at all is as stale as it gets
        if newest <= built:
            return None
        return to_project_relative(which, self.config.root)

    def unsynced(self, quiet: float = QUIET_SECONDS) -> str | None:
        """Why the PDF's SyncTeX data cannot be trusted, in a few words, or
        ``None`` when it can.

        Every click on the page goes through that data. A PDF with none
        resolves nothing; one compiled again somewhere else, without
        ``-synctex``, keeps the data of an older build beside it, and clicks
        then land on lines that have moved. Raised once for each PDF: a build
        command that writes no SyncTeX data will not write it the second time
        either.
        """
        try:
            built = self.config.pdf_path.stat().st_mtime_ns
        except OSError:
            return None  # no PDF: that is the first build's business
        if built == self._unsynced_for or time.time_ns() - built < quiet * 1e9:
            return None
        data = synctex_data_file(self.config.pdf_path)
        if data is None:
            self._unsynced_for = built
            return "the PDF has no SyncTeX data"
        try:
            synced = data.stat().st_mtime_ns
        except OSError:
            return None
        # The two are written by one run, moments apart.
        if built - synced > SYNC_SLACK_SECONDS * 1e9:
            self._unsynced_for = built
            return "the PDF is newer than its SyncTeX data"
        return None

    def handled(self) -> None:
        """Everything on disk now has been dealt with: do not raise it again.

        Called before a build this set off, whatever comes of it, and after an
        edit of texai's own that changes no output.
        """
        self._handled_ns = max(self._handled_ns, self._newest()[0], time.time_ns())
