"""Paths and constants shared by the server, the inbox watcher and the CLI."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # the app/ folder: code, web UI, tests
# Papers live next to app/, at the top of the project, so they are easy to find in Finder.
PAPERS_DIR = Path(os.environ.get("READING_TOOL_PAPERS", ROOT.parent / "papers"))
INBOX_DIR = PAPERS_DIR / "inbox"
LIBRARY_DIR = PAPERS_DIR / "library"
TMP_DIR = PAPERS_DIR / ".tmp"
WEB_DIR = ROOT / "web"

HOST = "127.0.0.1"
PORT = int(os.environ.get("READING_TOOL_PORT", "8377"))

RENDER_DPI = 200          # figure / table crops
EQUATION_DPI = 200        # display-equation crops
INGEST_TIMEOUT_S = 900    # hard limit for one ingest subprocess


def ensure_dirs() -> None:
    for d in (INBOX_DIR, LIBRARY_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)
