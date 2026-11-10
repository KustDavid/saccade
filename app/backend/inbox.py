"""Watch papers/inbox/: every PDF that lands there (or is already there at startup) is moved into the
library and converted. Files still being copied are left alone until their size stops changing."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

from . import config, library

log = logging.getLogger("inbox")

SETTLE_SECONDS = 1.5      # size + mtime must be unchanged this long
NO_EOF_GRACE = 60.0       # accept a PDF without a trailing %%EOF after this long without changes
PARTIAL_SUFFIXES = (".crdownload", ".download", ".part", ".partial", ".tmp")


def _looks_complete(path: Path, waited: float) -> bool:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            head = f.read(1024)
            f.seek(max(0, size - 4096))
            tail = f.read()
    except OSError:
        return False
    if b"%PDF" not in head:
        return False
    return b"%%EOF" in tail or waited >= NO_EOF_GRACE


class InboxWatcher:
    def __init__(self, on_added: Callable[[str], None], inbox: Path | None = None) -> None:
        self.inbox = inbox or config.INBOX_DIR
        self.on_added = on_added
        self._seen: dict[Path, tuple[int, float, float]] = {}   # path -> (size, mtime, unchanged since)
        self._failed: dict[Path, float] = {}                    # path -> mtime at failure
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop = threading.Event()
        self._seen.clear()
        self._failed.clear()
        self.inbox.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._run, name="inbox-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            from watchfiles import watch
        except Exception:  # pragma: no cover - fallback if watchfiles is missing
            watch = None
        self.scan()
        if watch is None:
            while not self._stop.wait(1.0):
                self.scan()
            return
        while not self._stop.is_set():
            try:
                # yields on file events and at least every second, so settling files get re-checked
                for _changes in watch(self.inbox, stop_event=self._stop, rust_timeout=1000,
                                      yield_on_timeout=True, debounce=400, raise_interrupt=False):
                    self.scan()
            except Exception as e:
                log.warning("watch failed (%s); falling back to polling", e)
                while not self._stop.wait(1.0):
                    self.scan()

    def scan(self) -> list[str]:
        """Process every settled PDF in the inbox. Returns ids that were added."""
        added: list[str] = []
        now = time.monotonic()
        try:
            entries = list(self.inbox.iterdir())
        except OSError:
            return added
        present = set()
        for path in entries:
            name = path.name
            if name.startswith(".") or not path.is_file():
                continue
            if name.lower().endswith(PARTIAL_SUFFIXES) or not name.lower().endswith(".pdf"):
                continue
            present.add(path)
            try:
                st = path.stat()
            except OSError:
                continue
            if self._failed.get(path) == st.st_mtime:
                continue
            prev = self._seen.get(path)
            if prev is None or prev[0] != st.st_size or prev[1] != st.st_mtime:
                self._seen[path] = (st.st_size, st.st_mtime, now)
                continue
            waited = now - prev[2]
            if st.st_size == 0 or waited < SETTLE_SECONDS or not _looks_complete(path, waited):
                continue
            try:
                summary, created = library.add_pdf(path, name, move=True)
                self._seen.pop(path, None)
                if created:
                    log.info("inbox: added %s as %s", name, summary["id"])
                    added.append(summary["id"])
                    self.on_added(summary["id"])
                else:
                    log.info("inbox: %s is already in the library as %s (duplicate removed)", name, summary["id"])
            except Exception as e:
                log.warning("inbox: could not add %s: %s", name, e)
                self._failed[path] = st.st_mtime
        for p in list(self._seen):
            if p not in present:
                self._seen.pop(p, None)
        return added
