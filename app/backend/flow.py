"""Reading order, paragraphs, headings and sections."""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from .extract import Box, Line, PageData, column_of, column_range
from .regions import EqRegion, Region
from .textutil import join_lines

SKIP_RE = re.compile(
    r"^(references?|bibliography|works cited|literature cited|cited literature|acknowledge?ments?|appendix|appendices|"
    r"supplementary|supplemental|online appendix|internet appendix|data availability|author contributions?|"
    r"conflicts? of interest|competing interests?|disclosures?|funding|declarations?|ethics statement)\b",
    re.I,
)
NAMED_RE = re.compile(
    r"^(abstract|introduction|references|bibliography|acknowledge?ments?|appendix|appendices|conclusions?|"
    r"concluding remarks|related work|related literature|literature review|discussion|background|methods?|"
    r"methodology|results|data|summary|preliminaries|experiments?|model|evaluation|limitations)\b",
    re.I,
)
NUM_DIGIT_RE = re.compile(r"^(?P<num>\d{1,2}(?:\.\d{1,2}){0,3})\.?\s+(?P<title>[^\s].*)$")
NUM_ROMAN_RE = re.compile(r"^(?P<num>[IVX]{1,5})\.\s+(?P<title>\S.*)$")
NUM_LETTER_RE = re.compile(r"^(?P<num>[A-Z](?:\.\d{1,2}){0,3})\.\s*(?P<title>[A-Z].*)$|^(?P<num2>[A-Z]\.\d{1,2}(?:\.\d{1,2})*)\s+(?P<title2>[A-Z].*)$")
NUM_APPX_RE = re.compile(r"^(?:Appendix|APPENDIX)\s+(?P<num>[A-Z0-9]{1,3})\b\s*[:.—–-]?\s*(?P<title>.*)$")
TERMINAL = ".!?:\"”'’)"


@dataclass(eq=False)
class Item:
    kind: str            # line | eq | asset
    page: int
    box: Box
    line: Line | None = None
    eq: EqRegion | None = None
    asset_id: str | None = None
    col: int = 0


@dataclass
class PBlock:
    """Internal block with layout info; converted to JSON at the end."""
    type: str
    page: int
    text: str = ""
    level: int = 0
    asset: str | None = None
    eq: EqRegion | None = None
    size: float = 0.0
    x0: float = 0.0
    x1: float = 0.0
    synthetic: bool = False
    numbering: str = ""
    num_kind: str = ""     # digit roman letter appendix none
    heading_style: tuple = ()


# ---------------------------------------------------------------------------
# column metrics
# ---------------------------------------------------------------------------


@dataclass
class ColMetrics:
    x0: float
    x1: float


def column_metrics(page: PageData, body_size: float) -> dict[int, ColMetrics]:
    out: dict[int, ColMetrics] = {}
    for col in ([1, 2] if page.two_col else [0]):
        ls = [
            l for l in page.lines
            if l.role == "" and abs(l.size - body_size) <= 1.2 and l.n_words >= 3
            and (not page.two_col or column_of(page, l.x0, l.x1) == col)
        ]
        if len(ls) < 3:
            ls = [
                l for l in page.lines
                if l.role == "" and l.n_words >= 3
                and (not page.two_col or column_of(page, l.x0, l.x1) == col)
            ]
        if len(ls) >= 3:
            xs0 = sorted(l.x0 for l in ls)
            xs1 = sorted(l.x1 for l in ls)
            out[col] = ColMetrics(xs0[int(len(xs0) * 0.15)], xs1[int(len(xs1) * 0.85)])
        else:
            a, b = column_range(page, col)
            out[col] = ColMetrics(a, b)
    if page.two_col:
        out[0] = ColMetrics(page.text_x0, page.text_x1)
    return out


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------


def order_items(page: PageData, items: list[Item]) -> list[Item]:
    if not page.two_col:
        return sorted(items, key=lambda it: (round(it.box[1]), it.box[0]))
    for it in items:
        it.col = column_of(page, it.box[0], it.box[2])
    items = sorted(items, key=lambda it: (it.box[1], it.box[0]))
    out: list[Item] = []
    band: list[Item] = []

    def flush() -> None:
        out.extend(sorted((b for b in band if b.col == 1), key=lambda b: (b.box[1], b.box[0])))
        out.extend(sorted((b for b in band if b.col == 2), key=lambda b: (b.box[1], b.box[0])))
        band.clear()

    for it in items:
        if it.col == 0:
            flush()
            out.append(it)
        else:
            band.append(it)
    flush()
    return out


# ---------------------------------------------------------------------------
# headings
# ---------------------------------------------------------------------------


def parse_numbering(text: str) -> tuple[str, str, str]:
    """-> (kind, number, title-without-number); kind in digit/roman/letter/appendix/none."""
    t = text.strip()
    m = NUM_APPX_RE.match(t)
    if m:
        return "appendix", m.group("num"), m.group("title").strip()
    m = NUM_ROMAN_RE.match(t)
    if m:
        return "roman", m.group("num"), m.group("title").strip()
    m = NUM_DIGIT_RE.match(t)
    if m and not re.match(r"^\d{3,}", t):
        return "digit", m.group("num"), m.group("title").strip()
    m = NUM_LETTER_RE.match(t)
    if m:
        num = m.group("num") or m.group("num2")
        title = m.group("title") or m.group("title2")
        return "letter", num, title.strip()
    return "none", "", t


def heading_candidate(l: Line, body_size: float, cm: ColMetrics) -> tuple[str, str, str] | None:
    t = l.text.strip()
    if len(t) < 2 or len(t) > 160:
        return None
    words = t.split()
    if len(words) > 18 or l.alpha_frac < 0.5:
        return None
    if t[0].islower() or t[0] in "([{,;=":
        return None
    if re.match(r"^(Figure|Fig\.|Table|Tab\.)\s", t):
        return None
    if "@" in t or "http" in t.lower():
        return None
    kind, num, title = parse_numbering(t)
    if kind == "digit" and (len(words) < 2 or not re.search(r"[A-Za-z]{2,}", title)):
        return None
    if kind == "digit" and title and title[0].islower():
        return None
    big = l.size >= body_size + 0.9
    bold = l.bold
    italic = l.italic
    caps = sum(c.isalpha() for c in t) >= 4 and t.upper() == t and any(c.isalpha() for c in t)
    named = bool(NAMED_RE.match(title)) and len(title.split()) <= 6
    width = cm.x1 - cm.x0
    if t[-1] in ".;," and kind == "none" and not named:
        return None
    if kind != "none":
        ok = big or bold or (italic and len(words) <= 14) or caps
    elif named:
        ok = big or bold or caps or (italic and len(words) <= 4)
    else:
        ok = (big and len(words) <= 14) or (bold and len(words) <= 12 and l.size >= body_size - 0.6) or (
            caps and (bold or l.size >= body_size - 0.3) and len(words) <= 10)
        if ok and l.w > 0.92 * width and len(words) > 8:
            ok = False
    if not ok:
        return None
    if l.size < body_size - 1.6 and not (bold or caps):
        return None
    return kind, num, title


# ---------------------------------------------------------------------------
# main flow builder
# ---------------------------------------------------------------------------


@dataclass
class FlowState:
    lines: list[Line] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)


def build_flow(
    pages: list[PageData],
    regions: list[Region],
    equations: list[EqRegion],
    body_size: float,
    footnote_marks: dict[int, set[str]],
    vocab,
) -> list[PBlock]:
    metrics = {p.index: column_metrics(p, body_size) for p in pages}
    page_by_index = {p.index: p for p in pages}

    # document-level line spacing and indentation style
    gaps: list[float] = []
    indents = 0
    total = 0
    for p in pages:
        ls = sorted((l for l in p.lines if l.role == "" and abs(l.size - body_size) < 0.6), key=lambda l: (l.y0, l.x0))
        for a, b in zip(ls, ls[1:]):
            if column_of(p, a.x0, a.x1) == column_of(p, b.x0, b.x1) and 0 <= b.y0 - a.y1 < 2 * body_size:
                gaps.append(b.y0 - a.y1)
        for l in ls:
            cm = metrics[p.index].get(column_of(p, l.x0, l.x1)) or metrics[p.index][0 if 0 in metrics[p.index] else 1]
            total += 1
            if 0.6 * l.size < l.x0 - cm.x0 < 4 * l.size:
                indents += 1
    typical_gap = statistics.median(gaps) if gaps else 0.25 * body_size
    indent_style = total > 0 and indents / total >= 0.04

    # ---- items in reading order -------------------------------------------
    stream: list[Item] = []
    regions_by_page: dict[int, list[Region]] = {}
    for r in regions:
        regions_by_page.setdefault(r.page, []).append(r)
    eq_by_page: dict[int, list[EqRegion]] = {}
    for e in equations:
        eq_by_page.setdefault(e.page, []).append(e)
    for p in pages:
        items: list[Item] = []
        for l in _merge_flow_fragments(p, body_size):
            items.append(Item("line", p.index, l.bbox, line=l))
        for e in eq_by_page.get(p.index, []):
            items.append(Item("eq", p.index, e.box, eq=e))
        for r in regions_by_page.get(p.index, []):
            if not r.caption.cont:
                items.append(Item("asset", p.index, r.box, asset_id=r.caption.asset_id))
        stream.extend(order_items(p, items))

    # merge a bare section number with the heading text that follows it on the same baseline
    merged: list[Item] = []
    i = 0
    while i < len(stream):
        it = stream[i]
        if it.kind == "line" and i + 1 < len(stream) and stream[i + 1].kind == "line":
            nx = stream[i + 1]
            a, b = it.line, nx.line
            if (
                re.fullmatch(r"(\d{1,2}(\.\d{1,2})*\.?|[IVX]{1,5}\.|[A-Z]\.(\d+\.?)*)", a.text.strip())
                and a.page == b.page and abs(a.cy - b.cy) < 0.4 * a.h and 0 < b.x0 - a.x1 < 4 * a.size
            ):
                from .extract import Span, _make_line

                sp = a.spans + [Span(" ", a.size, a.spans[-1].font, a.spans[-1].flags, a.x1, a.y0, b.x0, a.y1)] + b.spans
                nl = _make_line(a.page, sp)
                merged.append(Item("line", a.page, nl.bbox, line=nl, col=it.col))
                i += 2
                continue
        merged.append(it)
        i += 1
    stream = merged

    # ---- walk the stream ------------------------------------------------------
    blocks: list[PBlock] = []
    cur: list[Line] = []
    pending_assets: list[Item] = []

    def cm_for(l: Line) -> ColMetrics:
        p = page_by_index[l.page]
        m = metrics[l.page]
        c = column_of(p, l.x0, l.x1)
        return m.get(c) or m.get(0) or next(iter(m.values()))

    def flush() -> None:
        nonlocal cur
        if cur:
            texts = [l.body_text(footnote_marks.get(l.page, set())) for l in cur]
            text = join_lines(texts, vocab)
            if sum(c.isalpha() for c in text) >= 2:
                sizes = [l.size for l in cur]
                blocks.append(PBlock(
                    "paragraph", cur[0].page, text=text, size=statistics.median(sizes),
                    x0=min(l.x0 for l in cur), x1=max(l.x1 for l in cur),
                ))
            cur = []
        for a in pending_assets:
            blocks.append(PBlock("asset", a.page, asset=a.asset_id))
        pending_assets.clear()

    def reached_right(l: Line) -> bool:
        cm = cm_for(l)
        return l.x1 >= cm.x1 - 1.6 * l.size

    def is_indented(l: Line) -> bool:
        cm = cm_for(l)
        d = l.x0 - cm.x0
        return 0.6 * l.size < d < 4.5 * l.size

    def continues(prev: Line, l: Line) -> bool:
        p = page_by_index[l.page]
        same_flow = prev.page == l.page and (
            not p.two_col or column_of(p, prev.x0, prev.x1) == column_of(p, l.x0, l.x1)
        )
        gap = l.y0 - prev.y1
        if abs(l.size - prev.size) > 0.9:
            return False
        pt = prev.text.rstrip()
        terminal = bool(pt) and pt[-1] in TERMINAL
        if same_flow and -0.5 * l.size <= gap <= 2.2 * l.size:
            if gap > typical_gap + 0.5 * l.size:
                return False
            if is_indented(l) and not is_indented(prev) and (terminal or not reached_right(prev)):
                return False
            if not reached_right(prev) and (terminal or prev.x1 < cm_for(prev).x1 - 0.3 * (cm_for(prev).x1 - cm_for(prev).x0)):
                return False
            return True
        # page/column break or an interruption (figure, equation)
        if is_indented(l) and indent_style:
            # hanging-indent list item continuing on the next page keeps the same indent
            ind_prev = prev.x0 - cm_for(prev).x0
            ind_cur = l.x0 - cm_for(l).x0
            if not (reached_right(prev) and not terminal and abs(ind_prev - ind_cur) < 0.4 * l.size):
                return False
        if not reached_right(prev):
            return False
        if not terminal:
            return True
        if indent_style:
            return True
        return l.text[:1].islower()

    i = 0
    while i < len(stream):
        it = stream[i]
        if it.kind == "asset":
            if cur:
                pending_assets.append(it)
            else:
                blocks.append(PBlock("asset", it.page, asset=it.asset_id))
            i += 1
            continue
        if it.kind == "eq":
            flush()
            blocks.append(PBlock("equation", it.page, eq=it.eq))
            i += 1
            continue
        l = it.line
        cm = cm_for(l)
        h = heading_candidate(l, body_size, cm)
        if h is not None and cur and not _standalone(cur[-1], l):
            h = None
        if h is not None:
            # multi-line headings: following lines in the same style
            hl = [l]
            j = i + 1
            while j < len(stream) and stream[j].kind == "line":
                nx = stream[j].line
                if (
                    nx.page == l.page and abs(nx.size - l.size) < 0.4 and nx.bold == l.bold and nx.italic == l.italic
                    and 0 <= nx.y0 - hl[-1].y1 < 0.7 * l.size and hl[-1].text.rstrip()[-1:] not in ".!?"
                    and len(" ".join(x.text for x in hl + [nx]).split()) <= 22
                    and parse_numbering(nx.text)[0] == "none"
                    and (l.bold or l.italic or l.size >= body_size + 0.9 or nx.text.upper() == nx.text)
                ):
                    hl.append(nx)
                    j += 1
                else:
                    break
            flush()
            text = join_lines([x.text for x in hl], vocab)
            text = re.sub(r"^((?:[A-Z]|[IVX]+|\d+(?:\.\d+)*)\.)(?=[A-Z][a-z])", r"\1 ", text)  # "B.Step 2" -> "B. Step 2"
            kind, num, title = parse_numbering(text)
            if text.upper() == text and sum(c.isalpha() for c in text) > 3 and (
                    NAMED_RE.match(title) or len(title.split()) >= 3):
                text = _titlecase(text)
                title = _titlecase(title)
            blocks.append(PBlock(
                "heading", l.page, text=text, size=l.size, numbering=num, num_kind=kind,
                heading_style=(round(l.size * 2) / 2, l.bold, l.italic), x0=l.x0, x1=max(x.x1 for x in hl),
            ))
            i = j
            continue
        if cur and continues(cur[-1], l):
            cur.append(l)
        else:
            if cur:
                flush()
            cur = [l]
        i += 1
    flush()
    return blocks


def _merge_flow_fragments(p: PageData, body_size: float) -> list[Line]:
    """Body lines split at wide justified gaps ("(3) Recompute   the   sample-dependent") or
    section numbers set apart from their title. Tables are gone by now, so a generous gap is safe.
    Stray inline-math pieces (the "1" of a 1/sqrt(d) fraction) are dropped."""
    from .extract import merge_baseline_fragments

    body = [l for l in p.lines if l.role == ""]
    if not p.two_col:
        out = merge_baseline_fragments(body, max_gap_em=4.0, min_overlap=0.45)
    else:
        out = []
        for col in (0, 1, 2):
            out.extend(merge_baseline_fragments(
                [l for l in body if column_of(p, l.x0, l.x1) == col], max_gap_em=4.0, min_overlap=0.45))
    return [
        l for l in out
        if not (len(l.text.strip()) <= 3 and l.size < body_size - 1.5)
    ]


def _standalone(prev: Line, l: Line) -> bool:
    """A heading line must not be the continuation of a sentence on the previous line."""
    if prev.page != l.page:
        return True
    gap = l.y0 - prev.y1
    pt = prev.text.rstrip()
    if gap < 0.35 * l.size and pt and pt[-1] not in TERMINAL and abs(prev.size - l.size) < 0.5 and not prev.bold:
        return False
    return True


_SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "vs", "via", "with", "from"}


def _titlecase(s: str) -> str:
    out = []
    for i, w in enumerate(s.split()):
        lw = w.lower()
        if i > 0 and lw in _SMALL:
            out.append(lw)
        elif re.fullmatch(r"[IVX]+\.?", w) or re.fullmatch(r"[A-Z]\.(\d+\.?)*", w) or any(c.isdigit() for c in w):
            out.append(w)
        else:
            out.append(lw[:1].upper() + lw[1:])
    return " ".join(out)


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


def assign_levels(blocks: list[PBlock], body_size: float) -> None:
    heads = [b for b in blocks if b.type == "heading"]
    has_roman = sum(1 for h in heads if h.num_kind == "roman") >= 2
    lvl1_sizes = [h.size for h in heads if (h.num_kind == "digit" and "." not in h.numbering) or h.num_kind == "roman"]
    top_size = max(lvl1_sizes) if lvl1_sizes else (max((h.size for h in heads), default=body_size))
    for h in heads:
        k, n = h.num_kind, h.numbering
        if k == "digit":
            h.level = min(4, n.count(".") + 1)
        elif k == "roman":
            h.level = 1
        elif k == "letter":
            depth = n.count(".")
            h.level = min(4, (2 if has_roman else 1) + depth)
        elif k == "appendix":
            h.level = 1
        else:
            title = h.text
            if NAMED_RE.match(title) and len(title.split()) <= 4:
                h.level = 1
            elif h.size >= top_size - 0.4:
                h.level = 1
            elif h.heading_style and h.heading_style[1]:
                h.level = 2
            else:
                h.level = 3


def split_abstract(blocks: list[PBlock]) -> list[PBlock]:
    """Journals like JF/AER have no 'Introduction' heading: the intro follows the abstract directly."""
    for i, b in enumerate(blocks):
        if b.type == "heading" and re.match(r"^abstract\b", b.text.strip(), re.I):
            paras = []
            j = i + 1
            while j < len(blocks) and blocks[j].type != "heading":
                if blocks[j].type == "paragraph":
                    paras.append(j)
                j += 1
            if len(paras) < 2:
                return blocks
            first = blocks[paras[0]]
            nxt_head = blocks[j] if j < len(blocks) else None
            if nxt_head is not None and re.search(r"introduction", nxt_head.text, re.I):
                return blocks
            for k in paras[1:]:
                pb = blocks[k]
                rest_words = sum(len(blocks[x].text.split()) for x in paras if x >= k)
                if rest_words < 150:
                    break
                if abs(pb.size - first.size) >= 0.8 or (pb.x1 - pb.x0) > (first.x1 - first.x0) + 30:
                    intro = PBlock("heading", pb.page, text="Introduction", size=pb.size, num_kind="none",
                                   synthetic=True, heading_style=(pb.size, False, False))
                    intro.level = 1
                    return blocks[:k] + [intro] + blocks[k:]
            return blocks
    return blocks


def level_is_top(b: PBlock) -> bool:
    return b.level <= 1


def build_sections(blocks: list[PBlock]) -> tuple[list[dict], list[str]]:
    """-> (sections, section id per block)."""
    sections: list[dict] = []
    sec_of: list[str] = []
    cur_id = None
    after_refs = False
    skip_stack: list[tuple[int, bool]] = []   # (level, skip) of open ancestors
    for idx, b in enumerate(blocks):
        if b.type == "heading":
            sid = f"s{len(sections) + 1}"
            kind, num, title = parse_numbering(b.text)
            skip = bool(SKIP_RE.match(title)) or bool(SKIP_RE.match(b.text))
            if b.num_kind == "appendix":
                skip = True
            while skip_stack and skip_stack[-1][0] >= b.level:
                skip_stack.pop()
            if skip_stack and skip_stack[-1][1]:
                skip = True
            if after_refs:
                skip = True
            # back matter is sticky: everything after References / an Appendix / Acknowledgements
            # (once we are past the first third of the paper) is skipped by default
            if re.match(r"^(references?|bibliography|works cited|literature cited)\b", title, re.I):
                after_refs = True
            elif skip and level_is_top(b) and idx > len(blocks) / 3 and re.match(
                    r"^(appendix|appendices|online appendix|internet appendix|supplementary|supplemental|acknowledge?ments?)\b",
                    b.text if b.num_kind == "appendix" else title, re.I):
                after_refs = True
            skip_stack.append((b.level, skip))
            sections.append({
                "id": sid, "title": b.text, "level": b.level, "first_block": idx, "skip_default": skip,
            })
            cur_id = sid
        if cur_id is None:
            sections.append({"id": "s0", "title": "Start", "level": 1, "first_block": idx, "skip_default": False})
            cur_id = "s0"
        sec_of.append(cur_id)
    # appendix letters in a digit-numbered paper ("A. Object Detection Baselines")
    digit_doc = sum(1 for b in blocks if b.type == "heading" and b.num_kind == "digit") >= 3
    if digit_doc:
        in_appx = False
        heads = {s["first_block"]: s for s in sections}
        for idx, b in enumerate(blocks):
            if b.type != "heading" or idx not in heads:
                continue
            s = heads[idx]
            if b.num_kind == "letter" and s["level"] == 1:
                in_appx = True
            elif b.num_kind == "digit" and s["level"] == 1:
                in_appx = False
            if in_appx:
                s["skip_default"] = True
    return sections, sec_of
