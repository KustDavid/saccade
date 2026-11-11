"""Highlights and notes: stored per paper in annotations.json and written into
annotated.pdf (a copy of original.pdf with yellow highlight annotations; a note
becomes the highlight's comment). original.pdf is never modified.
"""

from __future__ import annotations

import logging
import os
import unicodedata
import uuid
from pathlib import Path

import pymupdf

from . import library

log = logging.getLogger("annotate")

YELLOW = (1.0, 0.92, 0.23)
PROBE = 24          # letters used to (re)find our place in the page text
MIN_COVERAGE = 0.6  # share of the sentence's letters that must be found on the page


def letters(s: str) -> str:
    """Letters only, case-folded: immune to spacing, hyphenation, ligatures, footnote digits."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s.casefold() if c.isalpha())


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


def _path(pid: str) -> Path:
    return library.paper_dir(pid) / "annotations.json"


def _has_pdf(pid: str) -> bool:
    return (library.paper_dir(pid) / "original.pdf").exists()


def list_annotations(pid: str) -> list[dict]:
    data = library.read_json(_path(pid), default=[])
    return data if isinstance(data, list) else []


def _save(pid: str, items: list[dict]) -> None:
    items.sort(key=lambda a: (a.get("block", 0), a.get("start", 0)))
    library.atomic_write_json(_path(pid), items)
    if not _has_pdf(pid):   # a web book: highlights live in the reader only
        return
    try:
        matched = build_pdf(pid, items)
        for a in items:
            a["in_pdf"] = bool(matched.get(a["id"]))
        library.atomic_write_json(_path(pid), items)
    except Exception:  # the highlight still exists in the reader even if the PDF copy fails
        log.exception("could not write annotated.pdf for %s", pid)


def create(pid: str, block: int, start: int, end: int, text: str, page: int | None, note: str = "") -> dict:
    items = list_annotations(pid)
    for a in items:  # one annotation per sentence
        if a.get("block") == block and a.get("start") == start:
            if note:
                a["note"] = note
                a["updated"] = library.now_iso()
                _save(pid, items)
            return a
    a = {
        "id": uuid.uuid4().hex[:12], "block": block, "start": start, "end": end, "text": text,
        "page": page, "note": note or "", "created": library.now_iso(), "updated": library.now_iso(),
    }
    if _has_pdf(pid):
        a["in_pdf"] = False
    items.append(a)
    _save(pid, items)
    return a


def update(pid: str, aid: str, note: str) -> dict | None:
    items = list_annotations(pid)
    for a in items:
        if a.get("id") == aid:
            a["note"] = note
            a["updated"] = library.now_iso()
            _save(pid, items)
            return a
    return None


def delete(pid: str, aid: str) -> bool:
    items = list_annotations(pid)
    kept = [a for a in items if a.get("id") != aid]
    if len(kept) == len(items):
        return False
    _save(pid, kept)
    return True


def pdf_path(pid: str) -> Path:
    """The PDF to show: the annotated copy when there is one, else the original."""
    d = library.paper_dir(pid)
    ann = d / "annotated.pdf"
    return ann if ann.exists() else d / "original.pdf"


# ---------------------------------------------------------------------------
# locating a sentence on the page
# ---------------------------------------------------------------------------


def _page_stream(doc, pages: list[int]):
    """Concatenated letters of the pages' words, with each letter's owner (page, word index)."""
    chars: list[str] = []
    owner: list[tuple[int, int]] = []
    words_by_page: dict[int, list] = {}
    for p in pages:
        words = doc[p].get_text("words")
        words_by_page[p] = words
        for wi, w in enumerate(words):
            for c in letters(w[4]):
                chars.append(c)
                owner.append((p, wi))
    return "".join(chars), owner, words_by_page


def locate(doc, text: str, page: int | None):
    """Find `text` in the PDF near 1-based `page`. Returns ({page_index: [Rect, ...]}, coverage)."""
    target = letters(text)
    if len(target) < 4:
        return {}, 0.0
    n = doc.page_count
    hint = min(max((page or 1) - 1, 0), n - 1)
    # a paragraph's page is where it starts; figure pages can push its later sentences further on
    pages = [p for p in range(hint - 1, hint + 4) if 0 <= p < n]
    stream, owner, words_by_page = _page_stream(doc, pages)

    def occurrences(probe: str) -> list[int]:
        out, j = [], stream.find(probe)
        while j >= 0 and len(out) < 12:
            out.append(j)
            j = stream.find(probe, j + 1)
        return out

    def walk(pos: int) -> list[int]:
        matched: list[int] = []
        i = 0
        while i < len(target):
            while i < len(target) and pos < len(stream) and stream[pos] == target[i]:
                matched.append(pos)
                i += 1
                pos += 1
            if i >= len(target):
                break
            # Lost our place (footnote block, running head, page break, margin stamp): find where it resumes.
            found = -1
            for skip in range(0, 41):
                chunk = target[i + skip: i + skip + PROBE]
                if len(chunk) < 6:
                    break
                found = stream.find(chunk, pos, pos + 12000)
                if found >= 0:
                    i += skip
                    break
            if found < 0:
                break
            pos = found
        return matched

    # Start at the sentence's opening letters; a page or figure break right after the first
    # words defeats a long probe, so fall back to shorter ones and keep the best-covered start.
    rank = {hint: 0, hint + 1: 1, hint + 2: 2, hint + 3: 3, hint - 1: 4}
    matched: list[int] = []
    for size in (40, 16, 8):
        starts = occurrences(target[: min(len(target), size)])
        starts.sort(key=lambda s: (rank.get(owner[s][0], 5), s))
        for st in starts:
            m = walk(st)
            if len(m) > len(matched):
                matched = m
        if len(matched) >= 0.98 * len(target):
            break
    if not matched:
        return {}, 0.0
    coverage = len(matched) / len(target)

    hit: dict[int, set[int]] = {}
    for m in matched:
        p, wi = owner[m]
        hit.setdefault(p, set()).add(wi)
    rects: dict[int, list] = {}
    for p, idxs in hit.items():
        words = words_by_page[p]
        order = sorted(idxs)
        # numbers and symbols sitting between two matched words belong to the sentence too
        for a, b in zip(order, order[1:]):
            if 1 < b - a <= 8 and all(not letters(words[k][4]) for k in range(a + 1, b)):
                idxs.update(range(a + 1, b))
        lines: dict[tuple[int, int], pymupdf.Rect] = {}
        for wi in sorted(idxs):
            w = words[wi]
            r = pymupdf.Rect(w[:4])
            key = (w[5], w[6])
            if key not in lines:
                lines[key] = r
            elif letters(w[4]):
                lines[key] |= r
            else:  # superscripts etc.: widen, don't heighten
                lines[key].x0 = min(lines[key].x0, r.x0)
                lines[key].x1 = max(lines[key].x1, r.x1)
        rects[p] = [r for r in lines.values() if not r.is_empty]
    return rects, coverage


def build_pdf(pid: str, items: list[dict] | None = None) -> dict[str, bool]:
    """(Re)write annotated.pdf from original.pdf + annotations. Returns {annotation id: found in PDF}."""
    d = library.paper_dir(pid)
    src = d / "original.pdf"
    out = d / "annotated.pdf"
    items = list_annotations(pid) if items is None else items
    if not items or not src.exists():
        out.unlink(missing_ok=True)
        return {}
    matched: dict[str, bool] = {}
    doc = pymupdf.open(src)
    try:
        for a in items:
            rects, coverage = locate(doc, a.get("text", ""), a.get("page"))
            ok = bool(rects) and coverage >= MIN_COVERAGE
            matched[a["id"]] = ok
            if not ok:
                continue
            first = True
            for p in sorted(rects):
                page = doc[p]  # keep the page object alive: an annotation dies with its page
                annot = page.add_highlight_annot(quads=[r.quad for r in rects[p]])
                annot.set_colors(stroke=YELLOW)
                # the note is the comment of the first highlight piece only
                annot.set_info(title="Saccade", content=(a.get("note") or "") if first else "")
                annot.update()
                first = False
        tmp = d / "annotated.pdf.tmp"
        doc.save(tmp, garbage=0, deflate=True)
    finally:
        doc.close()
    os.replace(tmp, out)
    return matched
