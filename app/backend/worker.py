"""Background ingest queue. Each conversion runs in its own subprocess so a crash (or a hang) in the
PDF library can never take the server down."""

from __future__ import annotations

import logging
import queue
import subprocess
import sys
import threading

from . import config, library

log = logging.getLogger("worker")


class IngestWorker:
    def __init__(self) -> None:
        self._q: queue.Queue[str | None] = queue.Queue()
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._current: str | None = None
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stopping = False

    # -- public ---------------------------------------------------------------
    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stopping = False
            self._q = queue.Queue()
            with self._lock:
                self._pending.clear()
            self._thread = threading.Thread(target=self._run, name="ingest-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        self._q.put(None)
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.kill()

    def enqueue(self, pid: str) -> None:
        with self._lock:
            if pid in self._pending:
                return
            self._pending.add(pid)
        self._q.put(pid)

    def cancel(self, pid: str) -> None:
        """Forget a queued paper and kill it if it is being converted right now (used by DELETE)."""
        proc = None
        with self._lock:
            self._pending.discard(pid)
            if self._current == pid and self._proc is not None and self._proc.poll() is None:
                proc = self._proc
                proc.kill()
        if proc is not None:
            try:
                proc.wait(timeout=10)
            except Exception:
                pass

    def busy_with(self, pid: str) -> bool:
        with self._lock:
            return pid in self._pending or self._current == pid

    # -- internals ------------------------------------------------------------
    def _run(self) -> None:
        while not self._stopping:
            pid = self._q.get()
            if pid is None:
                break
            with self._lock:
                if pid not in self._pending:
                    continue  # cancelled while queued
                self._pending.discard(pid)
                self._current = pid
            try:
                self._ingest(pid)
            except Exception as e:  # never let the worker thread die
                log.exception("ingest of %s crashed the worker: %s", pid, e)
                try:
                    library.set_status(pid, "error", f"internal error: {e}")
                except Exception:
                    pass
            finally:
                with self._lock:
                    self._current = None
                    self._proc = None

    def _ingest(self, pid: str) -> None:
        d = config.LIBRARY_DIR / pid
        if not d.exists():
            return
        log.info("converting %s", pid)
        cmd = [sys.executable, "-m", "backend.ingest", "--dir", str(d)]
        with self._lock:
            self._proc = subprocess.Popen(
                cmd, cwd=str(config.ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            proc = self._proc
        try:
            _out, err = proc.communicate(timeout=config.INGEST_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            library.set_status(pid, "error", f"conversion timed out after {config.INGEST_TIMEOUT_S} s")
            return
        if not d.exists():
            return  # deleted meanwhile
        s = library.get_summary(pid) or {}
        if proc.returncode != 0 and s.get("status") == "processing":
            tail = (err or "").strip().splitlines()[-3:]
            msg = " | ".join(tail) or f"converter exited with code {proc.returncode}"
            library.set_status(pid, "error", msg[-500:])
        log.info("finished %s: %s", pid, (library.get_summary(pid) or {}).get("status"))
