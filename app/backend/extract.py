"""Low-level page extraction: spans/lines, graphics, rules, optional layout-model boxes.

Everything downstream (captions, regions, reading order) works on the plain dataclasses
defined here, so PyMuPDF is only touched in this module and in the renderer.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections import Counter
from dataclasses import dataclass, field

import pymupdf

from .textutil import normalize_text

log = logging.getLogger(__name__)

MATH_FONT_RE = re.compile(
    r"CMMI|CMSY|CMEX|CMBSY|MSAM|MSBM|RMTMI|MTSY|MTEX|MTMI|Math|Symbol|STIX|rsfs|eufm|esint|wasy|"
    r"TXMI|TXSY|TXEX|PXMI|PXSY|LMMath|NewCMMath|XITS|Euclid|MT-?Extra|Mathematica|AMSSymbol|txsy|txmi",
    re.I,
)
BOLD_FONT_RE = re.compile(r"Bold|Black|Heavy|Semibold|SemiBold|Demi|Medi(?:um)?\b|-Medi|CMBX|CMB10|\bBd\b|-Bd|,Bold")
ITALIC_FONT_RE = re.compile(r"Ital|Oblique|CMTI|CMSL|-It\b|Slant|-Obl")
MONO_FONT_RE = re.compile(r"Mono|Courier|CMTT|Consol|Menlo", re.I)
MATH_CHARS = set("=+−×·∑∏∫∂∇√∞≤≥≈≠∈∉⊂⊆∪∩∀∃→←↔⇒⇔±∼∝⊤⊥⟨⟩‖|^_αβγδεζηθικλμνξπρστυφχψωΓΔΘΛΞΠΣΦΨΩϵϕ")

TEXT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP


@dataclass(eq=False)
class Span:
    text: str
    size: float
    font: str
    flags: int
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def bold(self) -> bool:
        return bool(self.flags & 16) or bool(BOLD_FONT_RE.search(self.font))

    @property
    def italic(self) -> bool:
        return bool(self.flags & 2) or bool(ITALIC_FONT_RE.search(self.font))

    @property
    def math(self) -> bool:
        return bool(MATH_FONT_RE.search(self.font))

    @property
    def sup(self) -> bool:
        return bool(self.flags & 1)


@dataclass(eq=False)
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    spans: list[Span]
    size: float
    role: str = ""          # '', noise, header, front, caption, asset, figtext, footnote, equation
    col: int = 0            # 0 = full width / single column, 1 = left, 2 = right
    asset: str | None = None
    _text: str | None = None

    # -- geometry -------------------------------------------------------
    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)

    # -- style ----------------------------------------------------------
    def _main_spans(self) -> list[Span]:
        sp = [s for s in self.spans if s.text.strip() and not s.sup]
        return sp or [s for s in self.spans if s.text.strip()]

    @property
    def bold(self) -> bool:
        sp = [s for s in self._main_spans() if any(c.isalnum() for c in s.text)]
        return bool(sp) and all(s.bold for s in sp)

    @property
    def italic(self) -> bool:
        sp = [s for s in self._main_spans() if any(c.isalpha() for c in s.text)]
        return bool(sp) and all(s.italic or s.math for s in sp) and not all(s.math for s in sp)

    @property
    def first_bold(self) -> bool:
        sp = self._main_spans()
        return bool(sp) and sp[0].bold

    @property
    def math_frac(self) -> float:
        total = 0
        mathy = 0
        for s in self.spans:
            n = len(s.text.strip())
            total += n
            if s.math:
                mathy += n
            else:
                mathy += sum(1 for c in s.text if c in MATH_CHARS)
        return mathy / total if total else 0.0

    # -- text -----------------------------------------------------------
    @property
    def text(self) -> str:
        if self._text is None:
            self._text = render_spans(self.spans, self.size, drop_numeric=False)
        return self._text

    def body_text(self, footnote_marks: set[str]) -> str:
        t = render_spans(self.spans, self.size, drop_numeric=True, marks=footnote_marks)
        return re.sub(r"(?<=[A-Za-z\)]) (?=[.,;:](?:\s|$))", "", t)

    @property
    def n_words(self) -> int:
        return len(self.text.split())

    @property
    def alpha_frac(self) -> float:
        t = self.text.replace(" ", "")
        return sum(c.isalpha() for c in t) / len(t) if t else 0.0


_MARK_SYMBOLS = set("*∗†‡§¶#⋆✝⁎")
_NUMERIC_SUP_RE = re.compile(r"^[\d,\-–\s]+$")


def render_spans(spans: list[Span], size: float, drop_numeric: bool, marks: set[str] | None = None) -> str:
    """Concatenate spans; drop footnote markers that sit in superscript position."""
    out: list[str] = []
    prev_text = ""
    for s in spans:
        t = s.text
        st = t.strip()
        is_sup = s.sup or (st and s.size < 0.72 * size)
        if is_sup and st:
            if all(c in _MARK_SYMBOLS or c in ", " for c in st):
                out.append(" " if t.endswith(" ") else "")
                continue
            if _NUMERIC_SUP_RE.match(st) and drop_numeric:
                before = "".join(out).rstrip()
                prev_char = before[-1:] if before else ""
                nums = {n for n in re.split(r"[,\s\-–]+", st) if n}
                if (marks and nums and nums <= marks) or (prev_char and prev_char in ".,;:)]\"'”’"):
                    out.append(" " if t.endswith(" ") else "")
                    continue
            # keep (ordinal "th", exponents, letters): glue to the previous text
        out.append(t)
        prev_text = t
    return normalize_text("".join(out))


@dataclass
class PageData:
    index: int
    width: float
    height: float
    lines: list[Line]
    images: list[tuple[float, float, float, float]] = field(default_factory=list)
    graphics: list[tuple[float, float, float, float]] = field(default_factory=list)   # clustered vector drawings
    rules: list[tuple[float, float, float, float]] = field(default_factory=list)      # thin h/v lines
    layout: list[tuple[float, float, float, float, str]] = field(default_factory=list)
    vertical_text: str = ""   # rotated text (arXiv stamp, publisher watermark) kept for metadata sniffing
    vlines: list[tuple[Box, tuple[float, float], str]] = field(default_factory=list)  # rotated lines
    rotated: int = 0          # +90/-90 when the page content is a landscape table/figure turned sideways
    two_col: bool = False
    gutter: float = 0.0
    text_x0: float = 0.0
    text_x1: float = 0.0


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

Box = tuple[float, float, float, float]


def union(a: Box, b: Box) -> Box:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def area(b: Box) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def inter(a: Box, b: Box) -> float:
    return area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def x_overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def expand(b: Box, d: float) -> Box:
    return (b[0] - d, b[1] - d, b[2] + d, b[3] + d)


def contains_point(b: Box, x: float, y: float, tol: float = 0.0) -> bool:
    return b[0] - tol <= x <= b[2] + tol and b[1] - tol <= y <= b[3] + tol


def cluster_boxes(boxes: list[Box], tol: float = 4.0) -> list[Box]:
    """Merge boxes that touch (within ``tol``) into cluster bounding boxes."""
    if not boxes:
        return []
    clusters: list[Box] = []
    for b in sorted(boxes, key=lambda r: r[1]):
        merged = True
        cur = b
        while merged:
            merged = False
            keep = []
            for c in clusters:
                if (cur[0] - tol <= c[2] and c[0] - tol <= cur[2] and cur[1] - tol <= c[3] and c[1] - tol <= cur[3]):
                    cur = union(cur, c)
                    merged = True
                else:
                    keep.append(c)
            clusters = keep
        clusters.append(cur)
    return clusters


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _spans_of(line: dict) -> list[Span]:
    out = []
    for s in line["spans"]:
        if not s["text"]:
            continue
        x0, y0, x1, y1 = s["bbox"]
        out.append(Span(s["text"], float(s["size"]), s.get("font", ""), int(s.get("flags", 0)), x0, y0, x1, y1))
    return out


def _dominant_size(spans: list[Span]) -> float:
    c: Counter = Counter()
    for s in spans:
        n = len(s.text.strip())
        if n and not s.sup:
            c[round(s.size * 2) / 2] += n
    if not c:
        for s in spans:
            c[round(s.size * 2) / 2] += max(1, len(s.text.strip()))
    return c.most_common(1)[0][0] if c else 10.0


def _make_line(page_no: int, spans: list[Span]) -> Line:
    x0 = min(s.x0 for s in spans)
    y0 = min(s.y0 for s in spans if not s.sup) if any(not s.sup for s in spans) else min(s.y0 for s in spans)
    x1 = max(s.x1 for s in spans)
    y1 = max(s.y1 for s in spans)
    return Line(page_no, x0, y0, x1, y1, spans, _dominant_size(spans))


def merge_baseline_fragments(lines: list[Line], max_gap_em: float = 0.6, min_overlap: float = 0.6) -> list[Line]:
    """Join raw lines that sit on the same baseline with only a word-sized gap between them."""
    chains: list[Line] = []
    for b in sorted(lines, key=lambda l: l.x0):
        best = -1
        gap = 0.0
        for idx, cur in enumerate(chains):
            vo = min(cur.y1, b.y1) - max(cur.y0, b.y0)
            if vo < min_overlap * min(cur.h, b.h):
                continue
            size = max(cur.size, b.size)
            g = b.x0 - cur.x1
            if -0.6 * size <= g <= max_gap_em * size:
                best, gap = idx, g
                break
        if best < 0:
            chains.append(b)
            continue
        cur = chains[best]
        size = max(cur.size, b.size)
        sep = []
        if gap > 0.12 * size and not cur.spans[-1].text.endswith(" ") and not b.spans[0].text.startswith(" "):
            sep = [Span(" ", b.size, b.spans[0].font, 0, cur.x1, b.y0, b.x0, b.y1)]
        chains[best] = _make_line(cur.page, cur.spans + sep + b.spans)
    chains.sort(key=lambda l: (l.y0, l.x0))
    return chains


def extract_page(page: pymupdf.Page, use_layout: bool = True) -> PageData:
    W, H = page.rect.width, page.rect.height
    pr = page.rect
    d = page.get_text("dict", flags=TEXT_FLAGS)
    raw_lines: list[Line] = []
    vertical: list[str] = []
    vlines: list[tuple[Box, tuple[float, float], str]] = []
    for b in d.get("blocks", []):
        if b.get("type", 0) != 0:
            continue
        for l in b.get("lines", []):
            spans = _spans_of(l)
            if not spans or not "".join(s.text for s in spans).strip():
                continue
            dx, dy = l.get("dir", (1.0, 0.0))
            if abs(dy) > 0.08 or dx < 0:
                txt = "".join(s.text for s in spans)
                vertical.append(txt)
                vlines.append((tuple(l["bbox"]), (dx, dy), normalize_text(txt)))
                continue
            line = _make_line(page.number, spans)
            # outside the visible page
            if line.x1 < pr.x0 or line.x0 > pr.x1 or line.y1 < pr.y0 or line.y0 > pr.y1:
                continue
            raw_lines.append(line)
    lines = merge_baseline_fragments(raw_lines)

    images: list[Box] = []
    try:
        for info in page.get_image_info():
            x0, y0, x1, y1 = info["bbox"]
            x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, W), min(y1, H)
            if x1 - x0 < 12 or y1 - y0 < 12:
                continue
            if (x1 - x0) * (y1 - y0) > 0.92 * W * H:
                continue  # full-page background / scanned page
            images.append((x0, y0, x1, y1))
    except Exception as e:  # pragma: no cover - defensive
        log.debug("image info failed on page %s: %s", page.number, e)

    graphics: list[Box] = []
    rules: list[Box] = []
    try:
        paths = page.get_drawings()
    except Exception as e:  # pragma: no cover
        log.debug("get_drawings failed on page %s: %s", page.number, e)
        paths = []
    for p in paths:
        r = p.get("rect")
        if r is None:
            continue
        x0, y0, x1, y1 = max(r.x0, 0), max(r.y0, 0), min(r.x1, W), min(r.y1, H)
        w, h = x1 - x0, y1 - y0
        if w < 0 or h < 0:
            continue
        if w * h > 0.6 * W * H:
            continue  # page background
        fill = p.get("fill")
        stroke = p.get("color")
        if p.get("type") == "f" and fill is not None and min(fill) > 0.97 and stroke is None:
            continue  # white box, invisible
        if (h <= 2.5 and w >= 6) or (w <= 2.5 and h >= 6):
            rules.append((x0, y0, x1, y1))
        elif w < 1.5 and h < 1.5:
            continue
        else:
            graphics.append((x0, y0, x1, y1))
    if len(graphics) > 1500:
        # pre-merge on a coarse grid so pathological pages (scatter plots, outlined text) stay fast
        cells: dict[tuple[int, int], Box] = {}
        for g in graphics:
            k = (int((g[0] + g[2]) / 32), int((g[1] + g[3]) / 32))
            cells[k] = union(cells[k], g) if k in cells else g
        graphics = list(cells.values())
    graphics = cluster_boxes(graphics, tol=4.0)
    # tiny isolated clusters (bullets, underline dots) are not figures
    graphics = [g for g in graphics if (g[2] - g[0]) > 6 and (g[3] - g[1]) > 6]

    layout: list[tuple[float, float, float, float, str]] = []
    if use_layout:
        try:
            page.get_layout()
            info = getattr(page, "layout_information", None) or []
            for item in info:
                if len(item) >= 5:
                    layout.append((float(item[0]), float(item[1]), float(item[2]), float(item[3]), str(item[4])))
        except Exception as e:
            log.debug("layout failed on page %s: %s", page.number, e)

    return PageData(
        index=page.number,
        width=W,
        height=H,
        lines=lines,
        images=images,
        graphics=graphics,
        rules=rules,
        layout=layout,
        vertical_text=" ".join(vertical),
        vlines=vlines,
        rotated=_rotation_of(vlines, lines),
    )


def _rotation_of(vlines, lines: list[Line]) -> int:
    """Landscape content printed sideways on a portrait page (common for wide regression tables)."""
    vert = [v for v in vlines if abs(v[1][1]) > 0.9 and len(v[2].strip()) > 0]
    nh = sum(1 for l in lines if l.n_words >= 1)
    if len(vert) < 12 or len(vert) < 3 * max(nh, 1):
        return 0
    up = sum(1 for v in vert if v[1][1] < 0)
    return 90 if up >= len(vert) / 2 else -90


def body_font_size(pages: list[PageData]) -> float:
    c: Counter = Counter()
    for p in pages:
        for l in p.lines:
            if l.n_words >= 4:
                c[l.size] += len(l.text)
    return c.most_common(1)[0][0] if c else 10.0


def analyse_columns(page: PageData, body_size: float) -> None:
    """Decide whether a page is set in two columns and where the gutter is."""
    W = page.width
    cand = [
        l for l in page.lines
        if l.role == "" and abs(l.size - body_size) <= 1.2 and l.n_words >= 3 and l.alpha_frac > 0.5
    ]
    if len(cand) < 6:
        cand = [l for l in page.lines if l.role == "" and l.n_words >= 3]
    if not cand:
        page.text_x0, page.text_x1 = 0.08 * W, 0.92 * W
        return
    xs0 = sorted(l.x0 for l in cand)
    xs1 = sorted(l.x1 for l in cand)
    page.text_x0 = xs0[max(0, int(len(xs0) * 0.05) - 0)]
    page.text_x1 = xs1[min(len(xs1) - 1, int(len(xs1) * 0.95))]
    mid = (page.text_x0 + page.text_x1) / 2
    left = [l for l in cand if l.x1 < mid + 4]
    right = [l for l in cand if l.x0 > mid - 4]
    cross = [l for l in cand if l.x0 < mid - 4 and l.x1 > mid + 4]
    n = len(cand)
    if len(left) >= 0.2 * n and len(right) >= 0.2 * n and len(cross) <= 0.35 * n and len(left) >= 3 and len(right) >= 3:
        page.two_col = True
        lmax = statistics.median(l.x1 for l in left)
        rmin = statistics.median(l.x0 for l in right)
        page.gutter = (lmax + rmin) / 2 if lmax < rmin else mid
    else:
        page.two_col = False
        page.gutter = mid


def harmonise_columns(pages: list[PageData], body_size: float) -> None:
    """Pages dominated by a figure can look single-column; borrow the document's gutter for them."""
    texty = [p for p in pages if sum(1 for l in p.lines if l.role == "" and l.n_words >= 3) >= 6]
    tc = [p for p in texty if p.two_col]
    if not texty or len(tc) < max(2, 0.4 * len(texty)):
        return
    g = statistics.median(p.gutter for p in tc)
    x0 = statistics.median(p.text_x0 for p in tc)
    x1 = statistics.median(p.text_x1 for p in tc)
    for p in pages:
        if p.two_col:
            continue
        ls = [l for l in p.lines if l.role == "" and l.n_words >= 3 and l.size >= body_size - 1.5]
        if len(ls) < 3:
            continue
        crossing = [l for l in ls if l.x0 < g - 6 and l.x1 > g + 6]
        if len(crossing) <= 0.15 * len(ls):
            p.two_col = True
            p.gutter = g
            p.text_x0 = min(p.text_x0, x0)
            p.text_x1 = max(p.text_x1, x1)


def column_of(page: PageData, x0: float, x1: float) -> int:
    if not page.two_col:
        return 0
    g = page.gutter
    if x1 <= g + 6:
        return 1
    if x0 >= g - 6:
        return 2
    # mostly on one side?
    left = max(0.0, min(x1, g) - x0)
    right = max(0.0, x1 - max(x0, g))
    if left > 0 and right / max(1.0, left + right) < 0.12:
        return 1
    if right > 0 and left / max(1.0, left + right) < 0.12:
        return 2
    return 0


def column_range(page: PageData, col: int) -> tuple[float, float]:
    if col == 1:
        return (page.text_x0, page.gutter)
    if col == 2:
        return (page.gutter, page.text_x1)
    return (page.text_x0, page.text_x1)
