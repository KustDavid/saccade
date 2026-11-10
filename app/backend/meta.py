"""Best-effort title / authors / year from the first pages of a PDF."""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field

import pymupdf

from .extract import Line, PageData, body_font_size, extract_page
from .textutil import collapse_ws, join_lines, normalize_text

INSTITUTION_RE = re.compile(
    r"\b(universit|institut|college|school|department|dept\b|laborator|labs?\b|research|google|microsoft|facebook|"
    r"meta\b|deepmind|openai|brain\b|inc\b|corp|center|centre|bank|federal|reserve|foundation|hospital|academy|"
    r"ministry|association|society|group\b|company|business|economics|nber|cepr|iza\b|cesifo|street|road|avenue|"
    r"usa\b|u\.s\.a|united|china|germany|france|japan|canada|india|italy|spain|abstract|email|e-mail|equal "
    r"contribution|corresponding|correspondence|faculty|division|program|press|conference|proceedings|workshop|"
    r"preprint|arxiv|journal|draft|version|working paper|keywords|jel\b)",
    re.I,
)
NAME_PART = r"(?:[A-ZÀ-ɏ][A-Za-zÀ-ɏ'’\-]+|[A-Z]\.(?:-?[A-Z]\.)?)"
NAME_RE = re.compile(
    rf"^{NAME_PART}(?:\s+(?:{NAME_PART}|de|van|von|der|den|da|di|del|della|la|le|du|dos|bin|al)){{1,4}}$"
)
MONTHS = r"(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?)"


@dataclass
class Meta:
    title: str | None = None
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    title_page: int = 0
    title_boxes: list[tuple[float, float, float, float]] = field(default_factory=list)


def _plain(l: Line) -> str:
    """Line text without any superscripts (affiliation marks, footnote symbols)."""
    return normalize_text("".join(s.text for s in l.spans if not (s.sup or s.size < 0.75 * l.size)))


def _clean_name(part: str) -> str:
    p = re.sub(r"[*∗†‡§¶#⋆\d]+", " ", part)
    p = collapse_ws(p.strip(" ,;:.")) if not re.search(r"\b[A-Z]\.$", p.strip()) else collapse_ws(p.strip(" ,;:"))
    if p and p.upper() == p and sum(c.isalpha() for c in p) > 3:
        p = " ".join(w[:1] + w[1:].lower() if len(w) > 2 else w for w in p.split())
    return p


def _metadata_title(doc: pymupdf.Document) -> str | None:
    t = (doc.metadata or {}).get("title") or ""
    t = collapse_ws(normalize_text(t))
    if len(t.split()) < 2 or len(t) > 300:
        return None
    if re.search(r"(?i)untitled|microsoft word|\.dvi$|\.docx?$|\.tex$|^title$|\.pdf$", t):
        return None
    return t


def guess_year(doc: pymupdf.Document, pages: list[PageData], filename: str = "") -> int | None:
    now = _dt.date.today().year
    texts = []
    for p in pages[:2]:
        texts.append(p.vertical_text)
    joined = " ".join(texts)
    m = re.search(r"arXiv:\s?(\d{2})(\d{2})\.\d{4,5}", joined)
    if not m:
        m = re.search(r"(?<!\d)(\d{2})(0[1-9]|1[0-2])\.\d{4,5}(?:v\d+)?(?!\d)", filename)
    if m and 1 <= int(m.group(2)) <= 12:
        y = 2000 + int(m.group(1))
        if 2007 <= y <= now + 1:
            return y
    # dates / copyright lines in the front matter, running headers and first-page footnotes
    cands: list[int] = []
    for p in pages[:2]:
        for l in p.lines:
            t = l.text
            for mm in re.finditer(rf"(?:©|\(c\)|copyright|{MONTHS}\s*(?:\d{{1,2}},?\s*)?|vol\.?.*?|conference.*?|proceedings.*?|\bNIPS\s|\bICML\s|\bICLR\s|\bCVPR\s|\bACL\s|draft:?.*?)((?:19[6-9]|20[0-4])\d)\b", t, re.I):
                y = int(mm.group(1))
                if 1960 <= y <= now + 1:
                    cands.append(y)
    if cands:
        return max(cands)
    cd = (doc.metadata or {}).get("creationDate") or ""
    m = re.match(r"D:(\d{4})", cd)
    if m and 1990 <= int(m.group(1)) <= now + 1:
        return int(m.group(1))
    return None


def guess_metadata(doc: pymupdf.Document, filename: str = "", pages: list[PageData] | None = None) -> Meta:
    n = min(3, doc.page_count)
    if pages is None:
        pages = [extract_page(doc[i], use_layout=False) for i in range(n)]
    else:
        pages = pages[:n]
    meta = Meta()
    if not pages:
        return meta
    body = body_font_size(pages)
    # title page: skip cover pages with hardly any text
    tp = pages[0]
    for p in pages[:2]:
        if sum(l.n_words for l in p.lines) >= 60:
            tp = p
            break
    meta.title_page = tp.index
    cands = [
        l for l in tp.lines
        if l.y0 < 0.7 * tp.height and sum(c.isalpha() for c in l.text) >= 3
        and not re.match(r"(?i)\s*(arxiv|preprint|under review|published as|accepted)", l.text)
        and l.role in ("", "front")
    ]
    title = None
    if cands:
        top = max(l.size for l in cands)
        group: list[Line] = []
        if top >= body + 1.4:
            first = min((l for l in cands if l.size >= top - 0.6), key=lambda l: l.y0)
            group = [first]
            for l in sorted(cands, key=lambda l: l.y0):
                if l is first or l.y0 <= first.y0:
                    continue
                last = group[-1]
                if l.size >= top - 1.2 and l.y0 - last.y1 < 1.0 * top:
                    group.append(l)
                elif l.y0 > last.y1:
                    break
        else:
            # no distinct title size (Word documents): first block of bold, centred lines near the top
            mid = tp.width / 2
            for l in sorted(cands, key=lambda l: l.y0):
                if l.y0 > 0.45 * tp.height:
                    break
                centred = abs(l.cx - mid) < 0.12 * tp.width and l.w < 0.85 * tp.width
                if l.bold and centred and not re.match(r"(?i)^\s*abstract", l.text):
                    if group and l.y0 - group[-1].y1 > 1.2 * l.size:
                        break
                    group.append(l)
                elif group:
                    break
            if len(" ".join(l.text for l in group).split()) < 3:
                group = []
        if group:
            title = join_lines([_plain(l) for l in group])
            title = title.strip(" *∗†‡")
            meta.title_boxes = [l.bbox for l in group]
            title_bottom = max(l.y1 for l in group)
            # authors: name-like lines between the title and the abstract / first paragraph
            stop = tp.height * 0.75
            for l in tp.lines:
                if l.y0 > title_bottom and re.match(r"(?i)^\s*(abstract|a b s t r a c t|summary)\b", l.text):
                    stop = min(stop, l.y0)
                elif l.y0 > title_bottom and abs(l.size - body) < 0.6 and l.n_words >= 12:
                    stop = min(stop, l.y0)
            names: list[str] = []
            for l in sorted(tp.lines, key=lambda l: (round(l.y0), l.x0)):
                if not (title_bottom < l.y0 < stop):
                    continue
                txt = _plain(l)
                if "@" in txt or INSTITUTION_RE.search(txt) or len(txt) > 160:
                    continue
                for part in re.split(r",|;|\band\b|&|\s{3,}", txt):
                    name = _clean_name(part)
                    if name and NAME_RE.match(name) and not INSTITUTION_RE.search(name) and name not in names:
                        names.append(name)
            meta.authors = names[:40]
    md = _metadata_title(doc)
    if not title and md:
        title = md
    meta.title = title
    if not meta.authors:
        a = collapse_ws(normalize_text((doc.metadata or {}).get("author") or ""))
        if a and not re.search(r"(?i)unknown|admin|user|owner|microsoft|^\W*$", a):
            parts = [collapse_ws(x) for x in re.split(r";|,|\band\b|&", a)]
            meta.authors = [p for p in parts if p and 2 <= len(p.split()) <= 5 and p[0].isupper()][:40]
    meta.year = guess_year(doc, pages, filename)
    return meta
