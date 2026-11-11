"""PDF -> papers/library/<id>/{paper.json, summary.json, assets/*.png}.

CLI:
    uv run python -m backend.ingest path/to/paper.pdf [more.pdf ...]   # add to library + convert
    uv run python -m backend.ingest https://a-quarto-book.example/     # import a web book (see webbook.py)
    uv run python -m backend.ingest --reingest <id>                     # re-run conversion
    uv run python -m backend.ingest --reingest-all
    uv run python -m backend.ingest --dir papers/library/<id>           # (used by the server)
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

import pymupdf

from . import config, library, webbook
from .extract import union as union_box
from .extract import PageData, analyse_columns, body_font_size, extract_page, harmonise_columns
from .flow import assign_levels, build_flow, build_sections, split_abstract
from .meta import Meta, guess_metadata
from .regions import (
    Caption,
    EqRegion,
    Region,
    assign_asset_ids,
    build_regions,
    detect_equations,
    find_captions,
    mark_footnotes,
    mark_margin_numbers,
    mark_margins,
    mark_orphan_graphics,
    rotated_page_region,
    side_preferences,
    vote_caption_sides,
)
from .textutil import build_vocab, find_refs, norm_label_number

log = logging.getLogger("ingest")

_layout_ok: bool | None = None


class IngestError(Exception):
    """A conversion failure with a message meant for the user."""


def _enable_layout() -> bool:
    """Activate PyMuPDF's layout model (used as a hint for tables/pictures/formulas) if available."""
    global _layout_ok
    if _layout_ok is None:
        try:
            import pymupdf.layout  # noqa: F401

            _layout_ok = True
        except Exception as e:  # pragma: no cover - depends on install
            log.warning("layout model unavailable (%s); using heuristics only", e)
            _layout_ok = False
    return _layout_ok


# ---------------------------------------------------------------------------
# front matter
# ---------------------------------------------------------------------------


def mark_front_matter(pages: list[PageData], meta: Meta, body_size: float) -> None:
    tp = meta.title_page
    for p in pages[:tp]:
        for l in p.lines:
            if l.role == "":
                l.role = "front"
    if tp >= len(pages):
        return
    p = pages[tp]
    title_boxes = meta.title_boxes
    if not title_boxes:
        return
    title_top = min(b[1] for b in title_boxes)
    title_bottom = max(b[3] for b in title_boxes)
    stop = None
    for l in sorted(p.lines, key=lambda l: l.y0):
        if l.role != "" or l.y0 <= title_bottom:
            continue
        t = l.text.strip()
        if re.match(r"(?i)^(abstract|a b s t r a c t|summary)\b", t):
            stop = l.y0 - 0.5
            break
    if stop is None:
        # first real paragraph: >= 3 lines of (near) body size with many words
        cand = sorted((l for l in p.lines if l.role == "" and l.y0 > title_bottom), key=lambda l: l.y0)
        for i, l in enumerate(cand):
            run = cand[i:i + 3]
            if len(run) == 3 and all(abs(x.size - body_size) <= 1.6 and x.n_words >= 6 for x in run):
                stop = l.y0 - 0.5
                break
    if stop is None or stop > 0.8 * p.height:
        stop = title_bottom + 0.5
    for l in p.lines:
        if l.role == "" and (l.y1 <= stop or any(_inside(l.bbox, b) for b in title_boxes)):
            l.role = "front"


def _inside(a, b) -> bool:
    return a[0] >= b[0] - 1 and a[1] >= b[1] - 1 and a[2] <= b[2] + 1 and a[3] <= b[3] + 1


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _clip(page: pymupdf.Page, box, pad: float) -> pymupdf.Rect:
    r = pymupdf.Rect(box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)
    return r & page.rect


def render_box(doc: pymupdf.Document, page_index: int, box, dpi: int, pad: float = 4.0,
               rotate: int = 0) -> pymupdf.Pixmap:
    page = doc[page_index]
    z = dpi / 72.0
    m = pymupdf.Matrix(z, z)
    if rotate:
        m = m.prerotate(rotate)
    return page.get_pixmap(matrix=m, clip=_clip(page, box, pad), alpha=False)


def stitch(pixes: list[pymupdf.Pixmap], gap: int = 24) -> pymupdf.Pixmap:
    if len(pixes) == 1:
        return pixes[0]
    w = max(p.width for p in pixes)
    h = sum(p.height for p in pixes) + gap * (len(pixes) - 1)
    out = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, w, h), False)
    out.clear_with(255)
    y = 0
    for p in pixes:
        if p.colorspace.n != 3:
            p = pymupdf.Pixmap(pymupdf.csRGB, p)
        p.set_origin(0, y)
        out.copy(p, p.irect)
        y += p.height + gap
    return out


# ---------------------------------------------------------------------------
# main conversion
# ---------------------------------------------------------------------------


def convert(pdf_path: Path, out_dir: Path, source_filename: str = "") -> dict:
    """Convert one PDF. Writes PNGs into ``out_dir/assets`` and returns the Paper dict (no summary fields
    that belong to the library such as id/added)."""
    t0 = time.time()
    try:
        doc = pymupdf.open(pdf_path)
    except Exception as e:
        raise IngestError(f"Could not open this file as a PDF ({e}).") from e
    if doc.needs_pass:
        raise IngestError("This PDF is password-protected.")
    if doc.page_count == 0:
        raise IngestError("This PDF has no pages.")
    use_layout = _enable_layout()
    pages: list[PageData] = []
    for i in range(doc.page_count):
        try:
            pages.append(extract_page(doc[i], use_layout=use_layout))
        except Exception as e:  # one bad page must not kill the paper
            log.warning("page %d failed: %s", i + 1, e)
            pages.append(PageData(i, doc[i].rect.width, doc[i].rect.height, []))
    total_words = sum(l.n_words for p in pages for l in p.lines)
    if total_words < 30:
        raise IngestError("No text found in this PDF (is it a scan?). Scanned PDFs are not supported yet.")
    body = body_font_size(pages)
    mark_margins(pages, body)
    for p in pages:
        analyse_columns(p, body)
    harmonise_columns(pages, body)
    for p in pages:
        mark_margin_numbers(p)
    meta = guess_metadata(doc, source_filename or pdf_path.name)
    mark_front_matter(pages, meta, body)

    # figures & tables
    captions_by_page: dict[int, list[Caption]] = {}
    rotated_regions: dict[int, Region] = {}
    all_caps: list[Caption] = []
    prev_rot: Region | None = None
    for p in pages:
        caps = find_captions(p, body)
        if p.rotated:
            # landscape table/figure printed sideways: one asset for the whole page
            hcap = next((c for c in caps if not c.cont), caps[0] if caps else None)
            rcap, rreg = rotated_page_region(p, prev_rot)
            if hcap is not None:
                if rreg is not None:
                    rreg.caption = hcap
                    rreg.box = union_box(rreg.box, hcap.bbox)
                    for c in caps:
                        rreg.box = union_box(rreg.box, c.bbox)
                    rcap = hcap
                    caps = [hcap]
                for l in p.lines:
                    if l.role == "":
                        l.role = "asset"
            elif rcap is not None:
                caps = [rcap]
            if rreg is not None:
                rotated_regions[p.index] = rreg
            prev_rot = rreg
        else:
            prev_rot = None
        captions_by_page[p.index] = caps
        all_caps.extend(caps)
    assign_asset_ids(all_caps)
    votes = []
    for p in pages:
        if not p.rotated:
            votes.extend(vote_caption_sides(p, captions_by_page[p.index], body))
    prefs = side_preferences(votes)
    regions: list[Region] = []
    for p in pages:
        if p.index in rotated_regions:
            regs = [rotated_regions[p.index]]
        else:
            regs = build_regions(p, captions_by_page[p.index], body, prefs)
        regions.extend(regs)
        mark_orphan_graphics(p, regs, body)

    footnotes, marks = mark_footnotes(pages, body)
    equations: list[EqRegion] = []
    for p in pages:
        equations.extend(detect_equations(p, body))

    _debug_dump(pages, regions, equations)
    vocab = build_vocab(l.text for p in pages for l in p.lines if l.role in ("", "caption", "footnote"))
    pblocks = build_flow(pages, regions, equations, body, marks, vocab)
    assign_levels(pblocks, body)
    pblocks = split_abstract(pblocks)
    sections, sec_of = build_sections(pblocks)

    # ---- render assets -------------------------------------------------------
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    by_id: dict[str, list[Region]] = {}
    order: list[str] = []
    for r in regions:
        aid = r.caption.asset_id
        if aid not in by_id:
            by_id[aid] = []
            order.append(aid)
        by_id[aid].append(r)
    assets: list[dict] = []
    for aid in order:
        regs = by_id[aid]
        first = next((r for r in regs if not r.caption.cont), regs[0])
        cap = first.caption
        pixes = []
        for r in regs:
            try:
                pixes.append(render_box(doc, r.page, r.box, config.RENDER_DPI, rotate=pages[r.page].rotated))
            except Exception as e:
                log.warning("render %s failed: %s", aid, e)
        if not pixes:
            continue
        pix = stitch(pixes)
        fname = f"{aid}.png"
        pix.save(assets_dir / fname)
        assets.append({
            "id": aid,
            "kind": cap.kind,
            "number": cap.number,
            "label": cap.label,
            "caption": cap.caption_text(vocab),
            "page": cap.page + 1,
            "pages": sorted({r.page + 1 for r in regs}),
            "image": fname,
            "width": pix.width,
            "height": pix.height,
            "first_ref_block": None,
        })

    # ---- equations -----------------------------------------------------------
    eq_n = 0
    for b in pblocks:
        if b.type == "equation" and b.eq is not None:
            eq_n += 1
            try:
                pix = render_box(doc, b.eq.page, b.eq.box, config.EQUATION_DPI, pad=3.0)
                b.eq.image = f"eq-{eq_n}.png"
                pix.save(assets_dir / b.eq.image)
                b.eq.width, b.eq.height = pix.width, pix.height
            except Exception as e:
                log.warning("equation render failed: %s", e)

    # ---- JSON blocks + refs ------------------------------------------------------
    label_index: dict[tuple[str, str], str] = {}
    for a in assets:
        label_index.setdefault((a["kind"], norm_label_number(a["number"])), a["id"])
    asset_by_id = {a["id"]: a for a in assets}
    blocks: list[dict] = []
    for idx, b in enumerate(pblocks):
        sec = sec_of[idx]
        if b.type == "paragraph":
            refs = find_refs(b.text, label_index)
            blocks.append({"type": "paragraph", "text": b.text, "section": sec, "page": b.page + 1, "refs": refs})
            for aid in refs:
                if asset_by_id[aid]["first_ref_block"] is None:
                    asset_by_id[aid]["first_ref_block"] = idx
        elif b.type == "heading":
            hb = {"type": "heading", "text": b.text, "section": sec, "page": b.page + 1, "level": b.level}
            if b.synthetic:
                hb["synthetic"] = True
            blocks.append(hb)
        elif b.type == "equation":
            e = b.eq
            if e is None or not e.image:
                blocks.append({"type": "paragraph", "text": e.alt if e else "", "section": sec, "page": b.page + 1, "refs": []})
                continue
            blocks.append({
                "type": "equation", "text": "", "image": e.image, "width": e.width, "height": e.height,
                "section": sec, "page": b.page + 1, "alt": e.alt,
            })
        elif b.type == "asset":
            if b.asset in asset_by_id:
                blocks.append({"type": "asset", "asset": b.asset, "section": sec, "page": b.page + 1})
            else:
                blocks.append({"type": "paragraph", "text": "", "section": sec, "page": b.page + 1, "refs": []})

    # assets in reading order (the order their blocks appear), unplaced ones last
    pos = {b["asset"]: i for i, b in enumerate(blocks) if b["type"] == "asset"}
    assets.sort(key=lambda a: pos.get(a["id"], 10**9))
    skip = {s["id"] for s in sections if s["skip_default"]}
    word_count = sum(
        len(b["text"].split()) for b in blocks if b["type"] in ("paragraph", "heading") and b["section"] not in skip
    )
    log.info("converted %s: %d pages, %d blocks, %d assets, %.1fs", pdf_path.name, doc.page_count, len(blocks),
             len(assets), time.time() - t0)
    paper = {
        "title": meta.title,
        "authors": meta.authors,
        "year": meta.year,
        "page_count": doc.page_count,
        "word_count": word_count,
        "num_figures": sum(1 for a in assets if a["kind"] == "figure"),
        "num_tables": sum(1 for a in assets if a["kind"] == "table"),
        "sections": sections,
        "blocks": blocks,
        "assets": assets,
        "footnotes": [{"page": f.page, "marker": f.marker, "text": f.text} for f in footnotes],
        "ingest_seconds": round(time.time() - t0, 2),
    }
    doc.close()
    return paper


def _debug_dump(pages: list[PageData], regions: list[Region], equations: list[EqRegion]) -> None:
    """READING_TOOL_DEBUG_PAGES=3,4 prints every line with its role for those (1-based) pages."""
    import os

    spec = os.environ.get("READING_TOOL_DEBUG_PAGES")
    if not spec:
        return
    want = {int(x) - 1 for x in spec.split(",") if x.strip().isdigit()}
    for p in pages:
        if p.index not in want:
            continue
        print(f"=== page {p.index + 1} two_col={p.two_col} gutter={p.gutter:.0f} text=[{p.text_x0:.0f},{p.text_x1:.0f}]", file=sys.stderr)
        for r in regions:
            if r.page == p.index:
                print(f"  REGION {r.caption.asset_id} {[round(v) for v in r.box]} elems={[e.kind for e in r.elems]}", file=sys.stderr)
        for e in equations:
            if e.page == p.index:
                print(f"  EQ {[round(v) for v in e.box]} {e.alt[:60]!r}", file=sys.stderr)
        for l in sorted(p.lines, key=lambda l: (l.y0, l.x0)):
            print(f"  {l.role or '-':9s} {[round(v) for v in l.bbox]} {l.size:4.1f} {'B' if l.bold else ' '}{'I' if l.italic else ' '} | {l.text[:100]}", file=sys.stderr)


def ingest_dir(d: Path) -> dict:
    """Convert the paper stored in library folder ``d`` in place; never raises."""
    pid = d.name
    if not d.is_dir():
        return {"id": pid, "status": "error", "error": "paper folder does not exist"}
    summary = library.read_json(d / "summary.json") or {}
    summary.setdefault("id", pid)
    summary.setdefault("source_filename", "original.pdf")
    summary.setdefault("added", library.now_iso())
    summary.setdefault("authors", [])
    pdf = d / "original.pdf"
    summary["status"] = "processing"
    summary["error"] = None
    library.atomic_write_json(d / "summary.json", summary)
    work = d / ".assets.tmp"
    shutil.rmtree(work, ignore_errors=True)
    try:
        if summary.get("source_url"):
            paper = webbook.convert(summary["source_url"], work, lang=summary.get("web_lang") or "stata")
        elif not pdf.exists():
            raise IngestError("original.pdf is missing from the paper folder.")
        else:
            paper = convert(pdf, work, summary.get("source_filename") or pdf.name)
        if not d.exists():  # deleted while we were working
            shutil.rmtree(work, ignore_errors=True)
            return summary
        final_assets = d / "assets"
        old = d / ".assets.old"
        shutil.rmtree(old, ignore_errors=True)
        if final_assets.exists():
            final_assets.rename(old)
        (work / "assets").rename(final_assets)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
        for k in ("title", "authors", "year", "page_count", "word_count", "num_figures", "num_tables"):
            if k == "title" and not paper.get("title"):
                continue
            summary[k] = paper[k]
        summary["status"] = "ready"
        summary["error"] = None
        full = {k: summary.get(k) for k in library.SUMMARY_KEYS}
        full.update({k: v for k, v in paper.items() if k not in ("title", "authors", "year")})
        full["title"] = summary["title"]
        full["authors"] = summary.get("authors") or []
        full["year"] = summary.get("year")
        if d.is_dir():
            library.atomic_write_json(d / "paper.json", full)
            library.atomic_write_json(d / "summary.json", summary)
    except Exception as e:
        shutil.rmtree(work, ignore_errors=True)
        expected = isinstance(e, (IngestError, webbook.WebBookError))
        msg = str(e) if expected else f"{type(e).__name__}: {e}"
        if expected:
            log.error("ingest of %s failed: %s", pid, msg)
        else:
            log.error("ingest of %s failed: %s\n%s", pid, msg, traceback.format_exc())
        if d.is_dir():
            summary["status"] = "error"
            summary["error"] = msg
            library.atomic_write_json(d / "summary.json", summary)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_summary(s: dict) -> None:
    status = s.get("status")
    line = f"[{status}] {s.get('id')}  -  {s.get('title')!r}"
    if status == "ready":
        line += f"  ({s.get('page_count')} pages, {s.get('word_count')} words, {s.get('num_figures')} figures, {s.get('num_tables')} tables)"
    if s.get("error"):
        line += f"\n    error: {s['error']}"
    print(line)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m backend.ingest", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdfs", nargs="*", metavar="PDF_OR_URL",
                    help="PDF files, or the address of a Quarto book site, to add to the library and convert")
    ap.add_argument("--lang", default=None, metavar="LANGUAGE",
                    help="web books: which tab of multi-language code listings to keep (default: stata)")
    ap.add_argument("--reingest", metavar="ID", action="append", default=[], help="re-run conversion for a paper id")
    ap.add_argument("--reingest-all", action="store_true", help="re-run conversion for every paper in the library")
    ap.add_argument("--dir", help=argparse.SUPPRESS)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    config.ensure_dirs()

    if args.dir:
        s = ingest_dir(Path(args.dir))
        return 0 if s.get("status") == "ready" else 1

    rc = 0
    targets: list[str] = list(args.reingest)
    if args.reingest_all:
        targets += [s["id"] for s in library.list_summaries()]
    for pid in targets:
        if not library.valid_id(pid) or not (config.LIBRARY_DIR / pid).exists():
            print(f"no such paper: {pid}", file=sys.stderr)
            rc = 1
            continue
        s = ingest_dir(config.LIBRARY_DIR / pid)
        _print_summary(s)
        rc |= 0 if s.get("status") == "ready" else 1
    for p in args.pdfs:
        if re.match(r"https?://", p):
            summary, _created = library.add_web(p, args.lang)
            s = ingest_dir(config.LIBRARY_DIR / summary["id"])
            _print_summary(s)
            rc |= 0 if s.get("status") == "ready" else 1
            continue
        path = Path(p).expanduser()
        if not path.is_file():
            print(f"not a file: {p}", file=sys.stderr)
            rc = 1
            continue
        summary, created = library.add_pdf(path, path.name, move=False)
        if not created and summary.get("status") == "ready":
            print(f"already in library (same bytes): {summary['id']} - use --reingest {summary['id']} to convert again")
            _print_summary(summary)
            continue
        s = ingest_dir(config.LIBRARY_DIR / summary["id"])
        _print_summary(s)
        rc |= 0 if s.get("status") == "ready" else 1
    if not targets and not args.pdfs:
        ap.print_help()
    return rc


if __name__ == "__main__":
    sys.exit(main())
