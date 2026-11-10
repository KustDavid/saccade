"""Page-level cleanup and figure/table/equation region detection.

Roles assigned to ``Line.role``:
  noise     rotated/marginal junk, line numbers
  header    running headers/footers, page numbers, journal banners
  caption   figure/table caption text (lives on the asset)
  asset     text inside a figure/table crop
  figtext   text inside an uncaptioned graphic
  footnote  footnote text at the bottom of a page/column
  equation  display-equation lines (rendered as image blocks)
  front     title/author/affiliation block before the abstract
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .extract import (
    Box,
    Line,
    PageData,
    area,
    column_of,
    column_range,
    contains_point,
    expand,
    inter,
    union,
    x_overlap,
)
from .textutil import (
    header_key,
    is_page_number,
    join_lines,
    norm_label_number,
    number_slug,
    repeated_margin_keys,
)

# ---------------------------------------------------------------------------
# Headers, footers, margin noise
# ---------------------------------------------------------------------------


def mark_margins(pages: list[PageData], body_size: float) -> None:
    top_frac, bot_frac = 0.11, 0.90
    zone_keys: list[set[str]] = []
    for p in pages:
        keys = set()
        for l in p.lines:
            if l.y1 < p.height * top_frac or l.y0 > p.height * bot_frac:
                k = header_key(l.text)
                if k:
                    keys.add(k)
        zone_keys.append(keys)
    repeated = repeated_margin_keys(zone_keys)
    for p in pages:
        for l in p.lines:
            t = l.text.strip()
            if not t or not any(c.isalnum() for c in t):
                if not t:
                    l.role = "noise"
                continue
            in_top = l.y1 < p.height * top_frac
            in_bot = l.y0 > p.height * bot_frac
            if in_top or in_bot:
                alone = not any(
                    o is not l and min(o.y1, l.y1) - max(o.y0, l.y0) > 0.5 * min(o.h, l.h)
                    and (0 <= o.x0 - l.x1 < 3 * l.size or 0 <= l.x0 - o.x1 < 3 * l.size)
                    for o in p.lines
                )
                key = header_key(t)
                numberish = is_page_number(t) or not key.replace("#", "").strip()
                if (numberish and alone) or (not numberish and key in repeated):
                    l.role = "header"
                    continue
                if in_top and l.y1 < p.height * 0.085 and l.size <= body_size * 0.82 and l.n_words <= 30:
                    l.role = "header"  # journal banner / "Preprint. Under review." etc.
                    continue
                if in_bot and l.y0 > p.height * 0.94 and l.size <= body_size * 0.82 and l.n_words <= 30:
                    l.role = "header"
                    continue
            if l.size < 3.5:
                l.role = "noise"


def mark_margin_numbers(p: PageData) -> None:
    """Line numbers in review copies sit outside the text block."""
    for l in p.lines:
        if l.role == "" and re.fullmatch(r"\d{1,4}", l.text.strip()):
            if l.x1 < p.text_x0 - 4 or l.x0 > p.text_x1 + 4:
                l.role = "noise"


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------

CAPTION_RE = re.compile(
    r"^(?P<word>Figure|FIGURE|Fig\.|FIG\.|Fig|Table|TABLE|Tab\.|TAB\.|Exhibit|EXHIBIT)\s*"
    r"(?P<num>(?:[A-Z]\.?\s?)?\d+(?:\.\d+)?[a-z]?|[IVXL]+(?![a-z]))(?P<rest>.*)$"
)
CONT_RE = re.compile(r"^\s*[(\[]?\s*[—–\-:.,]?\s*[(\[]?\s*(continued|cont\.?|cont'd|contd\.?)\b", re.I)
NOTES_RE = re.compile(
    r"^(notes?|sources?|data sources?|standard errors|robust standard|t-statistics|t statistics|z-statistics|"
    r"\*+\s*p\s*[<>]|\*\*\*|this (table|figure)|the (table|figure)|each (bar|row|column|cell|panel|point)|"
    r"significance levels?|clustered standard)\b",
    re.I,
)


@dataclass(eq=False)
class Caption:
    page: int
    kind: str          # figure | table
    number: str        # as printed: "1", "A.1", "IV"
    key: str           # normalised number
    cont: bool
    lines: list[Line]
    col: int
    band: tuple[float, float]
    asset_id: str = ""
    rot_box: Box | None = None      # set for captions found in rotated (landscape) content
    rot_text: str = ""

    @property
    def bbox(self) -> Box:
        if not self.lines and self.rot_box is not None:
            return self.rot_box
        b = self.lines[0].bbox
        for l in self.lines[1:]:
            b = union(b, l.bbox)
        return b

    @property
    def label(self) -> str:
        return ("Figure " if self.kind == "figure" else "Table ") + self.number

    def caption_text(self, vocab=None) -> str:
        if not self.lines:
            m = CAPTION_RE.match(self.rot_text)
            rest = m.group("rest") if m else self.rot_text
            return re.sub(r"^\s*[:.|\u2014\u2013\-]+\s*", "", rest).strip()
        texts = []
        for i, l in enumerate(self.lines):
            t = l.text.strip()
            if i + 1 < len(self.lines):
                nxt = self.lines[i + 1]
                if l.bold and not nxt.bold and t and t[-1] not in ".:!?—-" and nxt.text[:1].isupper():
                    t += "."
            texts.append(t)
        full = join_lines(texts, vocab)
        m = CAPTION_RE.match(full)
        if m:
            rest = m.group("rest")
            rest = CONT_RE.sub("", rest) if self.cont else rest
            rest = re.sub(r"^\s*[:.|—–\-]+\s*", "", rest)
            return rest.strip()
        return full


def _prev_line_above(lines: list[Line], l: Line) -> Line | None:
    best = None
    for o in lines:
        if o is l or o.role not in ("",):
            continue
        if o.y1 <= l.y0 + 1 and x_overlap(o.x0, o.x1, l.x0, l.x1) > 0.3 * min(o.w, l.w):
            if best is None or o.y1 > best.y1:
                best = o
    return best


def find_captions(page: PageData, body_size: float) -> list[Caption]:
    cands = [l for l in page.lines if l.role == ""]
    caps: list[Caption] = []
    taken: set[int] = set()
    for l in sorted(cands, key=lambda x: (x.y0, x.x0)):
        if id(l) in taken:
            continue
        text = l.text.strip()
        m = CAPTION_RE.match(text)
        if not m:
            continue
        rest = m.group("rest")
        r = rest.strip()
        cont = bool(CONT_RE.match(rest))
        score = 0
        if cont:
            score += 3
        elif r == "":
            score += 1
        elif r[0] in ":.|—–-":
            score += 2
        elif r[0].islower() or r[0] in ",;)]=":
            score -= 4
        elif r[0] == "(":
            score -= 1
        if l.first_bold:
            score += 2
        elif l.spans and l.spans[0].italic and not l.italic:
            score += 1
        if l.size < body_size - 0.4:
            score += 1
        col = column_of(page, l.x0, l.x1)
        cx0, cx1 = column_range(page, col)
        if r == "" and abs(((l.x0 + l.x1) / 2) - ((cx0 + cx1) / 2)) < 0.06 * (cx1 - cx0) and l.w < 0.5 * (cx1 - cx0):
            score += 1  # centred label line ("Table I")
        prev = _prev_line_above(cands, l)
        if prev is not None and l.y0 - prev.y1 < max(0.5 * l.size, 1.5 * l.h) and abs(prev.size - l.size) < 0.6:
            pt = prev.text.rstrip()
            pcol = column_range(page, column_of(page, prev.x0, prev.x1))
            wide = prev.w >= 0.6 * (pcol[1] - pcol[0])
            if pt and pt[-1] not in ".:!?" and prev.n_words >= 4 and wide and not CAPTION_RE.match(prev.text.strip()):
                score -= 4  # the label just continues a wrapped sentence
        if score < 2:
            continue
        kind = "figure" if m.group("word").lower().startswith("fig") else "table"
        if m.group("word").lower().startswith("exhibit"):
            kind = "figure" if page.images or page.graphics else "table"
        number = re.sub(r"\s+", "", m.group("num"))
        # caption band: the column the caption sits in
        if l.w > 0.6 * (page.text_x1 - page.text_x0) or not page.two_col:
            col = 0
        band = column_range(page, col) if col else (min(page.text_x0, l.x0), max(page.text_x1, l.x1))
        block = [l]
        taken.add(id(l))
        cur = l
        while True:
            below = [
                o for o in cands
                if id(o) not in taken and o.y0 >= cur.y0 + 0.5 * cur.h
                and x_overlap(o.x0, o.x1, band[0], band[1]) > 0.5 * min(o.w, band[1] - band[0])
            ]
            if not below:
                break
            ny = min(o.y0 for o in below)
            row = [o for o in below if o.y0 <= ny + 0.5 * o.h]
            nxt = min(row, key=lambda o: o.y0)
            gap = nxt.y0 - cur.y1
            if gap > max(2.5, 0.55 * cur.size):
                break
            if len(row) > 1:
                break
            if CAPTION_RE.match(nxt.text.strip()) and len(block) >= 1 and nxt.first_bold == l.first_bold and not CAPTION_RE.match(nxt.text.strip()).group("rest").strip()[:1].islower():
                break
            if nxt.size > cur.size + 3:
                break
            if len(block) >= 2 and abs(nxt.size - cur.size) > 1.2 and not nxt.bold:
                break
            block.append(nxt)
            taken.add(id(nxt))
            cur = nxt
        caps.append(Caption(page.index, kind, number, norm_label_number(number), cont, block, col, band))
    for c in caps:
        for l in c.lines:
            l.role = "caption"
    return caps


# ---------------------------------------------------------------------------
# Figure / table regions
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class Elem:
    kind: str                      # img gfx lpic ltab rule text cap
    box: Box
    line: Line | None = None
    barrier: bool = False
    cap: Caption | None = None


@dataclass(eq=False)
class Region:
    caption: Caption
    page: int
    box: Box
    elems: list[Elem] = field(default_factory=list)


GRAPHIC_KINDS = ("img", "gfx", "lpic")


def _same_row(a: Line, b: Line) -> bool:
    vo = min(a.y1, b.y1) - max(a.y0, b.y0)
    return vo > 0.5 * min(a.h, b.h)


def _is_heading_like(l: Line, body_size: float) -> bool:
    t = l.text.strip()
    if not t or l.n_words > 14:
        return False
    numbered = bool(re.match(r"^(\d+(\.\d+)*\.?|[IVX]+\.|[A-Z]\.(\d+\.?)*)\s+[A-Z]", t))
    return (l.size >= body_size + 1.0 and l.n_words >= 1 and l.alpha_frac > 0.6) or (numbered and (l.bold or l.italic))


def page_elements(page: PageData, body_size: float, captions: list[Caption]) -> list[Elem]:
    elems: list[Elem] = []
    gfx_boxes: list[Box] = []
    for b in page.images:
        elems.append(Elem("img", b))
        gfx_boxes.append(b)
    for b in page.graphics:
        elems.append(Elem("gfx", b))
        gfx_boxes.append(b)
    ltabs: list[Box] = []
    for x0, y0, x1, y1, cls in page.layout:
        if cls == "picture":
            elems.append(Elem("lpic", (x0, y0, x1, y1)))
            gfx_boxes.append((x0, y0, x1, y1))
        elif cls == "table":
            elems.append(Elem("ltab", (x0, y0, x1, y1)))
            ltabs.append((x0, y0, x1, y1))
    for r in page.rules:
        elems.append(Elem("rule", r))
    textlines = [l for l in page.lines if l.role == ""]
    for l in textlines:
        col = column_of(page, l.x0, l.x1)
        cx0, cx1 = column_range(page, col)
        inside_gfx = any(contains_point(expand(g, 2), l.cx, l.cy) for g in gfx_boxes)
        inside_tab = any(contains_point(expand(t, 2), l.cx, l.cy) for t in ltabs)
        siblings = [
            o for o in textlines
            if o is not l and _same_row(o, l) and column_of(page, o.x0, o.x1) == col
            and (o.x0 > l.x1 + 0.4 * l.size or o.x1 < l.x0 - 0.4 * l.size)
        ]
        full_width = l.w >= 0.85 * (cx1 - cx0)
        body_like = (
            l.size >= body_size - 0.9
            and l.n_words >= 3
            and l.alpha_frac >= 0.6
            and not inside_gfx
            and not inside_tab
            and (not siblings or full_width)
        )
        barrier = body_like or (_is_heading_like(l, body_size) and not inside_gfx and not inside_tab and not siblings)
        elems.append(Elem("text", l.bbox, line=l, barrier=barrier))
    for c in captions:
        elems.append(Elem("cap", c.bbox, barrier=True, cap=c))
    return elems


_GAP = {"img": 40.0, "gfx": 40.0, "lpic": 40.0, "ltab": 24.0, "rule": 22.0}


def _grow(page: PageData, cap: Caption, direction: int, elems: list[Elem]) -> list[Elem]:
    x_lo, x_hi = cap.band
    cb = cap.bbox
    edge = cb[1] if direction < 0 else cb[3]
    cands = []
    for e in elems:
        if e.cap is cap:
            continue
        bx = e.box
        ow = bx[2] - bx[0]
        ov = x_overlap(bx[0], bx[2], x_lo, x_hi)
        if ov <= 0 or ov < 0.4 * min(max(ow, 1.0), x_hi - x_lo):
            continue
        ccy = (cb[1] + cb[3]) / 2
        ecy = (bx[1] + bx[3]) / 2
        if direction < 0:
            # above the caption (a graphic may reach down beside/behind the caption)
            if bx[1] >= cb[1] or (bx[3] > edge + 2 and (e.kind == "text" or ecy >= ccy)):
                continue
        else:
            if bx[3] <= cb[3] or (bx[1] < edge - 2 and (e.kind == "text" or ecy <= ccy)):
                continue
        cands.append(e)
    cands.sort(key=lambda e: -e.box[3] if direction < 0 else e.box[1])
    chosen: list[Elem] = []
    frontier = edge
    for e in cands:
        near = (frontier - e.box[3]) if direction < 0 else (e.box[1] - frontier)
        if e.kind == "text":
            limit = 1.9 * (e.line.size if e.line else 10.0)
        else:
            limit = _GAP.get(e.kind, 24.0)
        if near > limit:
            break
        if e.barrier:
            if near > -2:
                break
            continue  # barrier overlapping already-covered area (side by side); skip it
        chosen.append(e)
        frontier = min(frontier, e.box[1]) if direction < 0 else max(frontier, e.box[3])
    return chosen


def _graphic_area(es: list[Elem]) -> float:
    return sum(area(e.box) for e in es if e.kind in GRAPHIC_KINDS)


def _table_score(es: list[Elem]) -> float:
    n_text = sum(1 for e in es if e.kind == "text")
    n_rules = sum(1 for e in es if e.kind == "rule")
    has_ltab = any(e.kind == "ltab" for e in es)
    score = n_text + 2 * n_rules + (8 if has_ltab else 0)
    if es and not has_ltab:
        u = es[0].box
        for e in es[1:]:
            u = union(u, e.box)
        if _graphic_area(es) > 0.45 * max(area(u), 1.0) and n_rules < 3:
            score *= 0.2  # mostly a picture: belongs to a figure caption
    return score


def _nearest(cap: Caption, es: list[Elem], direction: int, kinds: tuple[str, ...]) -> float:
    cb = cap.bbox
    ds = [(cb[1] - e.box[3]) if direction < 0 else (e.box[1] - cb[3]) for e in es if e.kind in kinds]
    return min(ds) if ds else 1e9


def _side_scores(cap: Caption, up: list[Elem], down: list[Elem]) -> tuple[float, float]:
    if cap.kind == "figure":
        return _graphic_area(up), _graphic_area(down)
    return _table_score(up), _table_score(down)


def vote_caption_sides(page: PageData, captions: list[Caption], body_size: float) -> list[tuple[str, int]]:
    """Unambiguous cases ("only one side has content") tell us the document's caption convention."""
    if not captions:
        return []
    elems = page_elements(page, body_size, captions)
    votes = []
    for cap in captions:
        if cap.cont:
            continue
        su, sd = _side_scores(cap, _grow(page, cap, -1, elems), _grow(page, cap, +1, elems))
        thr = 1.0 if cap.kind == "figure" else 3.0
        if su >= thr and sd < 0.3 * thr:
            votes.append((cap.kind, -1))
        elif sd >= thr and su < 0.3 * thr:
            votes.append((cap.kind, +1))
    return votes


def side_preferences(votes: list[tuple[str, int]]) -> dict[str, int]:
    prefs = {"figure": -1, "table": +1}   # usual convention: figure captions below, table captions above
    for kind in prefs:
        ups = sum(1 for k, d in votes if k == kind and d < 0)
        downs = sum(1 for k, d in votes if k == kind and d > 0)
        if ups > downs:
            prefs[kind] = -1
        elif downs > ups:
            prefs[kind] = +1
    return prefs


def build_regions(page: PageData, captions: list[Caption], body_size: float,
                  prefs: dict[str, int] | None = None) -> list[Region]:
    if not captions:
        return []
    prefs = prefs or {"figure": -1, "table": +1}
    elems = page_elements(page, body_size, captions)
    choice: dict[int, list[Elem]] = {}
    for cap in captions:
        up = _grow(page, cap, -1, elems)
        down = _grow(page, cap, +1, elems)
        su, sd = _side_scores(cap, up, down)
        pref = prefs.get(cap.kind, -1)
        if cap.kind == "figure":
            kinds = GRAPHIC_KINDS
            thr = 1.0
        else:
            kinds = ("text", "rule", "ltab")
            thr = 3.0
        if cap.cont and cap.kind == "table":
            side = down if sd >= 2 or su < 2 else up
        elif su >= thr and sd >= thr:
            nu = _nearest(cap, up, -1, kinds)
            nd = _nearest(cap, down, +1, kinds)
            # follow the document's convention unless the other side is clearly the closer one
            if pref < 0:
                side = up if nu <= nd + 20 else down
            else:
                side = down if nd <= nu + 20 else up
        elif su >= thr:
            side = up
        elif sd >= thr:
            side = down
        elif cap.kind == "figure":
            tu = sum(1 for e in up if e.kind in ("text", "rule", "ltab"))
            td = sum(1 for e in down if e.kind in ("text", "rule", "ltab"))
            side = up if (tu > td or (tu == td and pref < 0)) else down
        else:
            side = down if (sd > su or (sd == su and pref > 0)) else up
        choice[id(cap)] = side
    # resolve elements claimed by several captions: nearest caption wins
    owner: dict[int, Caption] = {}
    for cap in captions:
        for e in choice[id(cap)]:
            prev = owner.get(id(e))
            if prev is None:
                owner[id(e)] = cap
            else:
                def dist(c: Caption) -> float:
                    cb = c.bbox
                    return min(abs(cb[1] - e.box[3]), abs(e.box[1] - cb[3]))
                if dist(cap) < dist(prev):
                    owner[id(e)] = cap
    regions: list[Region] = []
    for cap in captions:
        mine = [e for e in choice[id(cap)] if owner.get(id(e)) is cap]
        box = cap.bbox
        for e in mine:
            box = union(box, e.box)
        regions.append(Region(cap, page.index, box, mine))
    _absorb_lines(page, regions, elems, body_size)
    return regions


def _absorb_lines(page: PageData, regions: list[Region], elems: list[Elem], body_size: float) -> None:
    """Pull in text that sits inside a region, plus adjacent 'Notes:' paragraphs; then mark roles."""
    text_elems = [e for e in elems if e.kind == "text"]
    for reg in regions:
        # lines largely inside the region box
        changed = True
        rounds = 0
        while changed and rounds < 4:
            changed = False
            rounds += 1
            for e in text_elems:
                l = e.line
                if l is None or l.role not in ("",):
                    continue
                if inter(l.bbox, reg.box) >= 0.5 * max(area(l.bbox), 1.0):
                    if e.barrier and inter(l.bbox, reg.box) < 0.9 * area(l.bbox):
                        continue
                    nb = union(reg.box, l.bbox)
                    if nb != reg.box:
                        changed = True
                    reg.box = nb
        # notes paragraphs directly below (or above, for captions-below layouts)
        for direction in (+1, -1):
            edge = reg.box[3] if direction > 0 else reg.box[1]
            while True:
                cands = [
                    e.line for e in text_elems
                    if e.line is not None and e.line.role == ""
                    and x_overlap(e.line.x0, e.line.x1, reg.box[0], reg.box[2]) > 0.5 * e.line.w
                    and ((direction > 0 and e.line.y0 >= edge - 1) or (direction < 0 and e.line.y1 <= edge + 1))
                    and not contains_point(reg.box, e.line.cx, e.line.cy)
                ]
                if not cands:
                    break
                nxt = min(cands, key=lambda l: l.y0) if direction > 0 else max(cands, key=lambda l: l.y1)
                gap = (nxt.y0 - edge) if direction > 0 else (edge - nxt.y1)
                if gap > 1.4 * nxt.size:
                    break
                if not NOTES_RE.match(nxt.text.strip()):
                    break
                # take this line and the rest of its paragraph
                para = [nxt]
                cur = nxt
                while True:
                    more = [
                        e.line for e in text_elems
                        if e.line is not None and e.line.role == "" and e.line not in para
                        and e.line.y0 >= cur.y1 - 1 and e.line.y0 - cur.y1 < 0.6 * cur.size
                        and x_overlap(e.line.x0, e.line.x1, reg.box[0], reg.box[2]) > 0.5 * e.line.w
                    ]
                    if not more:
                        break
                    cur = min(more, key=lambda l: l.y0)
                    para.append(cur)
                for l in para:
                    reg.box = union(reg.box, l.bbox)
                    l.role = "asset"
                    l.asset = reg.caption.asset_id
                edge = reg.box[3] if direction > 0 else reg.box[1]
                if direction < 0:
                    break
    # final marking: everything whose centre is inside a region belongs to it
    for reg in regions:
        for l in page.lines:
            if l.role in ("", "equation") and contains_point(reg.box, l.cx, l.cy, tol=0.5):
                l.role = "asset"
                l.asset = reg.caption.asset_id
            elif l.role == "" and inter(l.bbox, reg.box) > 0.6 * max(area(l.bbox), 1.0):
                l.role = "asset"
                l.asset = reg.caption.asset_id


def mark_orphan_tables(page: PageData, regions: list[Region]) -> None:
    """Uncaptioned tables found by the layout model: drop their cell text from the reading flow."""
    claimed = [r.box for r in regions]
    for x0, y0, x1, y1, cls in page.layout:
        if cls != "table":
            continue
        b = (x0, y0, x1, y1)
        if any(inter(b, c) > 0.5 * area(b) for c in claimed):
            continue
        inside = [l for l in page.lines if l.role == "" and contains_point(expand(b, 1), l.cx, l.cy)]
        cells = []
        for l in inside:
            sib = any(o is not l and _same_row(o, l) for o in inside)
            t = l.text.strip()
            if sib or (l.n_words <= 3 and t[-1:] not in ".!?"):
                cells.append(l)
        if len(cells) >= 4 and len(cells) >= 0.6 * len(inside):
            for l in cells:
                l.role = "figtext"


def mark_orphan_graphics(page: PageData, regions: list[Region], body_size: float) -> None:
    """Text inside big uncaptioned graphics (diagrams, logos, layout-model pictures) is not body text."""
    mark_orphan_tables(page, regions)
    claimed = [r.box for r in regions]
    boxes: list[Box] = []
    page_area = page.width * page.height
    for b in list(page.images) + list(page.graphics) + [l[:4] for l in page.layout if l[4] == "picture"]:
        if area(b) < 0.01 * page_area:
            continue
        if any(inter(b, c) > 0.6 * area(b) for c in claimed):
            continue
        boxes.append(b)
    if not boxes:
        return
    for l in page.lines:
        if l.role != "":
            continue
        for b in boxes:
            if contains_point(expand(b, 1), l.cx, l.cy):
                body_like = l.size >= body_size - 0.9 and l.n_words >= 6 and l.alpha_frac > 0.7 and l.w > 0.5 * (b[2] - b[0])
                if not body_like:
                    l.role = "figtext"
                break


# ---------------------------------------------------------------------------
# Footnotes
# ---------------------------------------------------------------------------

_FN_START_RE = re.compile(r"^\s*(\d{1,3}|[*∗†‡§¶#⋆]+|[a-z])(?=[\s.):A-Za-z])")


def _footnote_marker(l: Line) -> str | None:
    """Marker of a footnote's first line: a small/superscript number or symbol at the start."""
    sp = [s for s in l.spans if s.text.strip()]
    if not sp:
        return None
    first = sp[0]
    st = first.text.strip()
    small = first.sup or first.size < 0.85 * l.size
    m = re.match(r"^(\d{1,3}|[*∗†‡§¶#⋆]+)", st)
    if m and (small or st == m.group(1) or re.match(r"^\d{1,3}[\s.]", first.text.lstrip())):
        return m.group(1)
    m = _FN_START_RE.match(l.text)
    if m and m.group(1).isdigit() and len(sp) >= 1 and small:
        return m.group(1)
    return None


@dataclass
class Footnote:
    page: int
    marker: str
    text: str


def mark_footnotes(pages: list[PageData], body_size: float) -> tuple[list[Footnote], dict[int, set[str]]]:
    notes: list[Footnote] = []
    marks: dict[int, set[str]] = {}
    prev_had = False
    for p in pages:
        page_marks: set[str] = set()
        had = False
        cols = [1, 2] if p.two_col else [0]
        for col in cols:
            flow = [l for l in p.lines if l.role == "" and (not p.two_col or column_of(p, l.x0, l.x1) in (col, 0))]
            flow.sort(key=lambda l: (l.y0, l.x0))
            run: list[Line] = []
            for l in reversed(flow):
                if l.size <= body_size - 0.7 and l.y0 > 0.35 * p.height:
                    run.insert(0, l)
                else:
                    break
            if not run:
                continue
            # the run must be separated from the body text above it (gap or rule) or start with a marker
            starts = [(_footnote_marker(l), l) for l in run]
            has_marker = any(m for m, _ in starts)
            if not has_marker and not prev_had:
                continue
            # avoid eating a whole small-font page (e.g. a references page set in small type)
            if len(run) > 0.8 * len(flow) and len(flow) > 12:
                continue
            had = True
            cur: Footnote | None = None
            for m, l in starts:
                if m is not None and (cur is None or l.x0 >= run[0].x0 - 2):
                    if cur is not None:
                        notes.append(cur)
                    cur = Footnote(p.index + 1, m, l.text)
                    page_marks.add(m)
                elif cur is None:
                    cur = Footnote(p.index + 1, "", l.text)
                else:
                    cur.text = join_lines([cur.text, l.text])
                l.role = "footnote"
            if cur is not None:
                notes.append(cur)
        marks[p.index] = page_marks
        prev_had = had
    for n in notes:
        if n.marker and n.text.startswith(n.marker):
            n.text = n.text[len(n.marker):].strip()
    return notes, marks


# ---------------------------------------------------------------------------
# Display equations
# ---------------------------------------------------------------------------

EQNUM_RE = re.compile(r"^\(\s*[A-Z]?\.?\d+[a-z]?\s*\)$")


@dataclass(eq=False)
class EqRegion:
    page: int
    box: Box
    lines: list[Line]
    alt: str = ""
    image: str = ""
    width: int = 0
    height: int = 0


def _is_math_line(l: Line, page: PageData, body_size: float) -> bool:
    t = l.text.strip()
    if not t:
        return False
    if EQNUM_RE.match(t):
        return True
    col = column_of(page, l.x0, l.x1)
    cx0, cx1 = column_range(page, col)
    cw = max(cx1 - cx0, 1.0)
    indented = (l.x0 - cx0) > 0.08 * cw and (cx1 - l.x1) > 0.04 * cw
    ends_num = bool(re.search(r"\(\s*[A-Z]?\.?\d+[a-z]?\s*\)$", t)) and (cx1 - l.x1) < 0.05 * cw
    mf = l.math_frac
    words = re.findall(r"[A-Za-z]{4,}", "".join(s.text for s in l.spans if not s.math))
    if mf >= 0.45 and (indented or ends_num) and len(words) <= 4:
        return True
    if mf >= 0.25 and ends_num and len(words) <= 3:
        return True
    return False


def detect_equations(page: PageData, body_size: float) -> list[EqRegion]:
    formula_boxes = [b[:4] for b in page.layout if b[4] == "formula"]
    cand: list[Line] = []
    for l in page.lines:
        if l.role != "":
            continue
        in_formula = any(inter(l.bbox, fb) > 0.6 * max(area(l.bbox), 1.0) for fb in formula_boxes)
        if in_formula:
            # a layout 'formula' that is really a sentence or a heading: keep as text
            prose = len(re.findall(r"[A-Za-z]{4,}", "".join(s.text for s in l.spans if not s.math)))
            mf = l.math_frac
            if (prose >= 7 and mf < 0.3) or (prose >= 3 and mf < 0.12):
                continue
            cand.append(l)
        elif _is_math_line(l, page, body_size):
            cand.append(l)
    if not cand:
        return []
    cand.sort(key=lambda l: (l.y0, l.x0))
    groups: list[list[Line]] = []
    for l in cand:
        placed = False
        for g in groups:
            gb = g[0].bbox
            for o in g[1:]:
                gb = union(gb, o.bbox)
            same_col = column_of(page, l.x0, l.x1) == column_of(page, gb[0], gb[2]) or not page.two_col
            if same_col and l.y0 - gb[3] < 0.45 * l.size and l.y1 > gb[1] - 2:
                g.append(l)
                placed = True
                break
        if not placed:
            groups.append([l])
    regs: list[EqRegion] = []
    for g in groups:
        texts = [l.text.strip() for l in g]
        if all(EQNUM_RE.match(t) for t in texts):
            continue  # a lone "(3)" next to text that was not recognised: leave it
        box = g[0].bbox
        for o in g[1:]:
            box = union(box, o.bbox)
        # stray fragments of the same equation (sub/superscripts, big operators) sitting just around it
        for l in page.lines:
            if l.role == "" and l not in g and contains_point(expand(box, 3), l.cx, l.cy) and l.n_words <= 3:
                g.append(l)
                box = union(box, l.bbox)
        alt = " ".join(l.text.strip() for l in g)
        if len(re.sub(r"\s+", "", alt)) <= 5 and "=" not in alt:
            # stray sub/superscripts or a lone big operator: not worth an image, not readable text either
            for l in g:
                l.role = "noise"
            continue
        for l in g:
            l.role = "equation"
        regs.append(EqRegion(page.index, box, g, alt=alt))
    return regs


# ---------------------------------------------------------------------------
# Asset ids
# ---------------------------------------------------------------------------


def assign_asset_ids(captions: list[Caption]) -> None:
    """fig-1 / tab-a-1 / tab-iv; continuation captions share the id of their parent."""
    seen: dict[tuple[str, str], str] = {}
    used: set[str] = set()
    last_by_key: dict[tuple[str, str], int] = {}
    for c in captions:
        k = (c.kind, c.key)
        prefix = "fig" if c.kind == "figure" else "tab"
        if k in seen and (c.cont or c.page - last_by_key.get(k, -99) <= 1):
            c.asset_id = seen[k]
            c.cont = True
            last_by_key[k] = c.page
            continue
        base = f"{prefix}-{number_slug(c.number)}"
        aid = base
        i = 2
        while aid in used:
            aid = f"{base}-{i}"
            i += 1
        used.add(aid)
        seen[k] = aid
        last_by_key[k] = c.page
        c.asset_id = aid
        c.cont = False


# ---------------------------------------------------------------------------
# Rotated (landscape) pages
# ---------------------------------------------------------------------------


def rotated_page_region(page: PageData, prev: "Region | None") -> tuple[Caption | None, Region | None]:
    """A sideways table/figure page becomes one asset covering all of the page's content."""
    if not page.rotated:
        return None, None
    vert = [v for v in page.vlines if abs(v[1][1]) > 0.9]
    box = vert[0][0]
    for v in vert[1:]:
        box = union(box, v[0])
    for b in list(page.images) + list(page.graphics) + list(page.rules):
        box = union(box, b)
    # reading order of sideways lines: text running upwards stacks left->right
    order = sorted(vert, key=lambda v: v[0][0] if page.rotated == 90 else -v[0][2])
    cap: Caption | None = None
    for vb, _d, txt in order[:12]:
        m = CAPTION_RE.match(txt.strip())
        if m and not m.group("rest").strip()[:1].islower():
            kind = "figure" if m.group("word").lower().startswith("fig") else "table"
            number = re.sub(r"\s+", "", m.group("num"))
            cont = bool(CONT_RE.match(m.group("rest")))
            cap = Caption(page.index, kind, number, norm_label_number(number), cont, [], 0,
                          (page.text_x0, page.text_x1), rot_box=vb, rot_text=txt.strip())
            break
    if cap is None:
        if prev is None:
            return None, None
        cap = Caption(page.index, prev.caption.kind, prev.caption.number, prev.caption.key, True, [], 0,
                      (page.text_x0, page.text_x1), rot_box=box, rot_text=prev.caption.rot_text)
    for l in page.lines:
        if l.role == "":
            l.role = "asset"
    return cap, Region(cap, page.index, box, [])
