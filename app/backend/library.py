"""On-disk library: papers/library/<id>/{original.pdf, paper.json, summary.json, progress.json, assets/}.

``summary.json`` is the PaperSummary (minus ``progress``) plus ``sha256``; it exists from the moment a
PDF is accepted, so ``status: processing`` is visible before ``paper.json`` is written.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from . import config
from .textutil import make_paper_id, slugify

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,150}$")
_pdf_lock = threading.Lock()      # PyMuPDF is not thread-safe; the server only uses it for id guessing
_add_lock = threading.Lock()      # serialise "is this hash already here?" + mkdir

DEFAULT_PROGRESS = {"block": 0, "word": 0, "wpm": 350, "percent": 0.0, "updated": "1970-01-01T00:00:00Z"}
SUMMARY_KEYS = (
    "id", "title", "authors", "year", "source_filename", "added", "status", "error",
    "page_count", "word_count", "num_figures", "num_tables", "source_url",
)


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_json(path: Path, obj: Any) -> None:
    """Write via a temp file + rename. The parent folder must exist (a deleted paper stays deleted)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def valid_id(pid: str) -> bool:
    return bool(ID_RE.match(pid or ""))


def paper_dir(pid: str) -> Path:
    if not valid_id(pid):
        raise KeyError(pid)
    return config.LIBRARY_DIR / pid


def get_summary(pid: str) -> dict | None:
    try:
        d = paper_dir(pid)
    except KeyError:
        return None
    s = read_json(d / "summary.json")
    if not isinstance(s, dict):
        return None
    return s


def public_summary(s: dict) -> dict:
    out = {k: s.get(k) for k in SUMMARY_KEYS}
    out["authors"] = out["authors"] or []
    for k in ("page_count", "word_count", "num_figures", "num_tables"):
        out[k] = out[k] or 0
    prog = read_json(config.LIBRARY_DIR / s["id"] / "progress.json")
    out["progress"] = prog if isinstance(prog, dict) else None
    return out


def list_summaries() -> list[dict]:
    out = []
    if not config.LIBRARY_DIR.exists():
        return out
    for d in config.LIBRARY_DIR.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue
        s = read_json(d / "summary.json")
        if isinstance(s, dict) and s.get("id") == d.name:
            out.append(s)
    out.sort(key=lambda s: (s.get("added") or "", s.get("id")), reverse=True)
    return out


def find_by_hash(sha: str) -> str | None:
    for s in list_summaries():
        if s.get("sha256") == sha:
            return s["id"]
    return None


def write_summary(pid: str, summary: dict) -> None:
    atomic_write_json(paper_dir(pid) / "summary.json", summary)


def set_status(pid: str, status: str, error: str | None = None) -> dict | None:
    s = get_summary(pid)
    if s is None:
        return None
    s["status"] = status
    s["error"] = error
    write_summary(pid, s)
    return s


def guess_id(pdf_path: Path, source_filename: str, sha: str) -> tuple[str, dict]:
    """Paper id from a quick look at the first pages (falls back to the file name)."""
    info: dict = {"title": None, "authors": [], "year": None, "page_count": 0}
    try:
        import pymupdf  # noqa: WPS433 (lazy: keeps server import light)

        from .meta import guess_metadata

        with _pdf_lock:
            doc = pymupdf.open(pdf_path)
            try:
                info["page_count"] = doc.page_count
                m = guess_metadata(doc, source_filename)
                info.update(title=m.title, authors=m.authors, year=m.year)
            finally:
                doc.close()
    except Exception:
        pass
    pid = make_paper_id(info["authors"], info["year"], info["title"], sha, fallback=source_filename)
    return pid, info


def add_pdf(src: Path, source_filename: str, move: bool = False) -> tuple[dict, bool]:
    """Put a PDF into the library. Returns (summary, created). Same bytes -> existing entry."""
    config.ensure_dirs()
    sha = sha256_file(src)
    with _add_lock:
        existing = find_by_hash(sha)
        if existing:
            if move:
                try:
                    src.unlink()
                except OSError:
                    pass
            return get_summary(existing), False
        pid, info = guess_id(src, source_filename, sha)
        d = config.LIBRARY_DIR / pid
        n = 2
        while d.exists():
            d = config.LIBRARY_DIR / f"{pid}-{n}"
            n += 1
        pid = d.name
        d.mkdir(parents=True)
        dest = d / "original.pdf"
        if move:
            shutil.move(str(src), dest)
        else:
            shutil.copyfile(src, dest)
        title = info["title"] or re.sub(r"\.pdf$", "", source_filename, flags=re.I).strip() or pid
        summary = {
            "id": pid,
            "title": title,
            "authors": info["authors"] or [],
            "year": info["year"],
            "source_filename": source_filename,
            "added": now_iso(),
            "status": "processing",
            "error": None,
            "page_count": info["page_count"],
            "word_count": 0,
            "num_figures": 0,
            "num_tables": 0,
            "sha256": sha,
        }
        write_summary(pid, summary)
    return summary, True


def add_web(url: str, lang: str | None = None) -> tuple[dict, bool]:
    """Put a web book (see webbook.py) into the library. Returns (summary, created). Same address ->
    existing entry, so importing again refreshes it in place."""
    from urllib.parse import urlsplit

    config.ensure_dirs()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"Not a web address: {url}")
    sha = hashlib.sha256(url.encode()).hexdigest()
    with _add_lock:
        for s in list_summaries():
            if s.get("source_url") == url:
                if lang and s.get("web_lang") != lang:
                    s["web_lang"] = lang
                    write_summary(s["id"], s)
                return s, False
        name = (parts.netloc + parts.path).strip("/")
        pid = make_paper_id([], None, name, sha, fallback=name)
        d = config.LIBRARY_DIR / pid
        n = 2
        while d.exists():
            d = config.LIBRARY_DIR / f"{pid}-{n}"
            n += 1
        pid = d.name
        d.mkdir(parents=True)
        summary = {
            "id": pid, "title": name, "authors": [], "year": None, "source_filename": name,
            "added": now_iso(), "status": "processing", "error": None, "page_count": 0, "word_count": 0,
            "num_figures": 0, "num_tables": 0, "sha256": sha, "source_url": url,
        }
        if lang:
            summary["web_lang"] = lang
        write_summary(pid, summary)
    return summary, True


def delete_paper(pid: str) -> bool:
    d = paper_dir(pid)
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


def get_progress(pid: str) -> dict:
    p = read_json(paper_dir(pid) / "progress.json")
    if not isinstance(p, dict):
        return dict(DEFAULT_PROGRESS)
    out = dict(DEFAULT_PROGRESS)
    out.update(p)
    return out


def put_progress(pid: str, data: dict) -> dict:
    out = dict(DEFAULT_PROGRESS)
    out.update({k: v for k, v in data.items() if v is not None})
    if not data.get("updated"):
        out["updated"] = now_iso()
    atomic_write_json(paper_dir(pid) / "progress.json", out)
    return out


def safe_filename(name: str) -> str:
    base = os.path.basename(name or "") or "upload.pdf"
    base = base.replace("\x00", "")
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base


def tmp_upload_path(name: str) -> Path:
    config.ensure_dirs()
    fd, p = tempfile.mkstemp(dir=config.TMP_DIR, prefix="upload-", suffix="-" + (slugify(name, 40) or "file") + ".pdf")
    os.close(fd)
    return Path(p)
