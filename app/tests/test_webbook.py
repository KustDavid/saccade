"""Web-book import (Quarto book sites): parsing, assembly, fetching, library wiring.

All HTML here is made up; nothing is downloaded and no browser is started (the renderer is faked),
except for the one test at the bottom, which is skipped when Google Chrome is not installed.
"""

import http.server
import threading
from pathlib import Path

import pytest

from backend import annotate, config, ingest, library, webbook
from backend.webbook import MARK_A, MARK_B, Shot

BASE = "https://book.test/"

INDEX = """<!DOCTYPE html><html><head>
<meta name="generator" content="quarto-1.8.25"><meta name="author" content="Ada Example">
<title>A Made-Up Book</title><link href="./01-first.html" rel="next"></head>
<body><main class="content" id="quarto-document-content">
<header id="title-block-header"><h1 class="title">A Made-Up Book<br><em>Second Try</em></h1></header>
<section id="welcome" class="level1"><h1>Welcome</h1><p>Front page blurb that is not part of the text.</p></section>
</main></body></html>"""

CH1 = """<!DOCTYPE html><html><head><meta name="generator" content="quarto-1.8.25">
<title>1&nbsp; First Chapter – A Made-Up Book</title>
<link href="./02-second.html" rel="next"><link href="./index.html" rel="prev"></head>
<body><nav id="quarto-sidebar"><ul><li><a href="/">Sidebar junk</a></li></ul></nav>
<main class="content" id="quarto-document-content">
<header id="title-block-header" class="quarto-title-block default"><div class="quarto-title">
<h1 class="title"><span id="ch1" class="quarto-section-identifier"><span class="chapter-number">1</span>&nbsp; <span class="chapter-title">First Chapter</span></span></h1>
</div><div class="quarto-title-meta">meta junk</div></header>
<p><span class="math display">\\[\\newcommand{\\E}{\\mathbb{E}}\\DeclareMathOperator*{\\cov}{cov}\\]</span></p>
<section id="intro" class="level2" data-number="1.1">
<h2 data-number="1.1" class="anchored"><span class="header-section-number">1.1</span> Getting Started</h2>
<p>Plain words with <em>emphasis</em> and a note.<a href="#fn1" class="footnote-ref" id="fnref1" role="doc-noteref"><sup>1</sup></a>
The slope <span class="math inline">\\(\\beta_1\\)</span> matters, unlike
<span class="math inline">\\(\\frac{a}{b}\\)</span>, as <a href="#fig-plot" class="quarto-xref">Figure&nbsp;<span>1.1</span></a> shows.</p>
<p><span id="eq-main"><span class="math display">\\[ y = \\alpha + \\beta x \\tag{1.1}\\]</span></span></p>
<div id="fig-plot" class="quarto-float quarto-figure quarto-figure-center anchored">
<figure class="quarto-float quarto-float-fig figure">
<div aria-describedby="fig-plot-caption"><img src="graphics/plot.png" class="img-fluid figure-img" loading="lazy"></div>
<figcaption class="quarto-float-caption-bottom quarto-float-caption quarto-float-fig" id="fig-plot-caption">Figure&nbsp;1.1: A plot of <em>nothing</em> much</figcaption>
</figure></div>
<div id="tbl-nums" class="quarto-float quarto-figure quarto-figure-center anchored">
<figure class="quarto-float quarto-float-tbl figure">
<figcaption class="quarto-float-caption-top quarto-float-caption quarto-float-tbl" id="tbl-nums-caption">Table&nbsp;1.1: Some numbers</figcaption>
<div aria-describedby="tbl-nums-caption"><table class="table"><thead><tr><th>a</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table></div>
</figure></div>
<p>See <a href="#tbl-nums" class="quarto-xref">Table&nbsp;<span>1.1</span></a> for the numbers.</p>
<div class="panel-tabset">
<ul class="nav nav-tabs" role="tablist"><li class="nav-item"><a class="nav-link active" id="tabset-1-1-tab" data-bs-target="#tabset-1-1">Stata</a></li>
<li class="nav-item"><a class="nav-link" id="tabset-1-2-tab" data-bs-target="#tabset-1-2">R</a></li></ul>
<div class="tab-content">
<div id="tabset-1-1" class="tab-pane active" role="tabpanel"><div class="cell"><details class="code-fold"><summary>Code</summary>
<div class="sourceCode cell-code" id="cb1"><pre class="sourceCode stata code-with-copy"><code class="sourceCode stata">regress y x</code></pre></div></details></div></div>
<div id="tabset-1-2" class="tab-pane" role="tabpanel"><div class="cell">
<div class="sourceCode cell-code" id="cb2"><pre class="sourceCode r code-with-copy"><code class="sourceCode r">lm(y ~ x)</code></pre></div></div></div>
</div></div>
<ul><li>First point.</li><li><p>Second point.</p></li></ul>
<section id="deeper" class="level3"><h3 class="anchored">A Deeper Heading</h3><p>Deep text here.</p>
<section id="tiny" class="level4"><h4 class="anchored">A Run-In Heading</h4><p>Tiny text here.</p></section></section>
</section>
<section id="footnotes" class="footnotes footnotes-end-of-document" role="doc-endnotes"><hr><ol>
<li id="fn1"><p>The note text.<a href="#fnref1" class="footnote-back" role="doc-backlink">↩︎</a></p></li></ol></section>
</main></body></html>"""

CH2 = """<!DOCTYPE html><html><head><meta name="generator" content="quarto-1.8.25">
<title>References – A Made-Up Book</title></head><body><main class="content" id="quarto-document-content">
<header id="title-block-header"><h1 class="title">References</h1></header>
<div id="refs" class="references csl-bib-body" role="list">
<div id="ref-a" class="csl-entry" role="listitem">Example, Ada. 2020. <em>A Title</em>.</div>
<div id="ref-b" class="csl-entry" role="listitem">Sample, Bob. 2021. <em>Another</em>.</div></div>
</main></body></html>"""

SITE = {BASE: INDEX, BASE + "01-first.html": CH1, BASE + "02-second.html": CH2}


def fake_fetch(url):
    return url, SITE[url]


def fake_render(pages, assets_dir, cache_dir=None):
    """Stands in for Chrome: one tiny 'image' per requested shot, except the one named in skip."""
    out = {}
    assets_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for page in pages:
        for key, role in page.shots:
            n += 1
            name = f"shot-{n}.png"
            (assets_dir / name).write_bytes(b"\x89PNG fake")
            out[key] = Shot(file=name, width=200, height=40, em_w=6.0, em_h=1.2, descent=0.3)
    return out


def kinds(page):
    return [it["type"] for it in page.items]


@pytest.fixture()
def ch1():
    return webbook.parse_page(CH1, BASE + "01-first.html", page_no=1)


# ---------------------------------------------------------------- inline math


@pytest.mark.parametrize("tex,want", [
    ("\\beta_1", "β₁"),
    ("Y_i^1", "Yᵢ¹"),
    ("x^2", "x²"),
    ("\\hat{\\beta}", "β̂"),
    ("E[Y^1 \\mid D=1]", "E[Y¹|D=1]"),
    ("\\delta \\times 2", "δ×2"),
    ("\\text{ATE}", "ATE"),
    ("N", "N"),
    ("5\\%", "5%"),
])
def test_simple_inline_math_becomes_plain_characters(tex, want):
    assert webbook.tex_to_text(tex) == want


@pytest.mark.parametrize("tex", [
    "\\frac{a}{b}",                # needs two-dimensional layout
    "Y_{ATE}",                     # no subscript capitals in Unicode
    "\\sum_{i=1}^n x_i",
    "\\begin{matrix}a\\end{matrix}",
    "\\somethingunknown{x}",
    "a+b+c+d+e+f+g+h+i+j+k+l+m",   # too long to read as one word
])
def test_complex_inline_math_is_left_for_an_image(tex):
    assert webbook.tex_to_text(tex) is None


# ---------------------------------------------------------------- one page


def test_page_title_and_next_link(ch1):
    assert ch1.title == "1 First Chapter"
    assert ch1.next_url == BASE + "02-second.html"


def test_headings_carry_numbers_and_levels(ch1):
    heads = [(it["level"], it["text"]) for it in ch1.items if it["type"] == "heading"]
    assert heads == [(1, "1 First Chapter"), (2, "1.1 Getting Started"), (3, "A Deeper Heading"), (3, "A Run-In Heading")]
    assert [it.get("minor", False) for it in ch1.items if it["type"] == "heading"] == [False, False, False, True]


def test_navigation_and_title_meta_are_not_text(ch1):
    text = " ".join(it.get("text", "") for it in ch1.items)
    assert "Sidebar junk" not in text and "meta junk" not in text


def test_paragraph_text_drops_footnote_marker_and_converts_simple_math(ch1):
    para = next(it for it in ch1.items if it["type"] == "paragraph")
    assert para["text"].startswith("Plain words with emphasis and a note. The slope β₁ matters, unlike ")
    assert "Figure 1.1 shows." in para["text"]          # non-breaking space became a normal one
    assert "1 The slope" not in para["text"]


def test_complex_inline_math_becomes_a_placeholder_with_a_shot(ch1):
    para = next(it for it in ch1.items if it["type"] == "paragraph")
    assert list(para["images"].values()) == ["\\frac{a}{b}"]
    key = next(iter(para["images"]))
    assert f"{MARK_A}{key}{MARK_B}," in para["text"]
    assert (key, "inline") in ch1.shots
    assert f'data-rt="{key}"' in ch1.marked_html


def test_cross_references_are_recorded_by_target(ch1):
    paras = [it for it in ch1.items if it["type"] == "paragraph"]
    assert paras[0]["targets"] == ["fig-plot"]
    assert paras[1]["targets"] == ["tbl-nums"]


def test_display_equation_is_a_shot_and_definition_blocks_are_dropped(ch1):
    eqs = [it for it in ch1.items if it["type"] == "equation" and it["role"] == "equation"]
    assert len(eqs) == 1
    assert "\\alpha + \\beta x" in eqs[0]["alt"]
    assert (eqs[0]["key"], "equation") in ch1.shots


def test_figure_and_table_floats(ch1):
    assets = [it for it in ch1.items if it["type"] == "asset"]
    assert [(a["kind"], a["number"], a["label"], a["caption"], a["dom_id"]) for a in assets] == [
        ("figure", "1.1", "Figure 1.1", "A plot of nothing much", "fig-plot"),
        ("table", "1.1", "Table 1.1", "Some numbers", "tbl-nums"),
    ]
    assert all((a["key"], "asset") in ch1.shots for a in assets)


def test_code_tabs_keep_only_the_chosen_language(ch1):
    code = [it for it in ch1.items if it["type"] == "equation" and it["role"] == "code"]
    assert [c["alt"] for c in code] == ["regress y x"]
    r_page = webbook.parse_page(CH1, BASE + "01-first.html", page_no=1, lang="r")
    assert [c["alt"] for c in r_page.items if c.get("role") == "code"] == ["lm(y ~ x)"]


def test_marked_html_shows_the_chosen_tab_and_opens_folded_code(ch1):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(ch1.marked_html, "html.parser")
    assert "active" in soup.find(id="tabset-1-1")["class"]
    assert soup.find("details").has_attr("open")
    r_soup = BeautifulSoup(webbook.parse_page(CH1, BASE, page_no=1, lang="r").marked_html, "html.parser")
    assert "active" in r_soup.find(id="tabset-1-2")["class"]
    assert "active" not in r_soup.find(id="tabset-1-1")["class"]
    assert not soup.find("img").has_attr("loading")      # lazy images would never load off-screen


def test_list_items_become_paragraphs(ch1):
    texts = [it["text"] for it in ch1.items if it["type"] == "paragraph"]
    assert "First point." in texts and "Second point." in texts


def test_footnotes_are_collected_without_the_back_arrow(ch1):
    assert ch1.footnotes == [{"marker": "1", "text": "The note text."}]
    assert "The note text." not in " ".join(it.get("text", "") for it in ch1.items)


def test_reference_list_entries_become_paragraphs():
    page = webbook.parse_page(CH2, BASE + "02-second.html", page_no=2)
    assert [it["text"] for it in page.items if it["type"] == "paragraph"] == [
        "Example, Ada. 2020. A Title.", "Sample, Bob. 2021. Another.",
    ]


# ---------------------------------------------------------------- whole book


@pytest.fixture()
def book(tmp_path):
    return webbook.convert(BASE, tmp_path, fetch=fake_fetch, render=fake_render), tmp_path


def test_book_metadata(book):
    paper, _ = book
    assert paper["title"] == "A Made-Up Book Second Try"
    assert paper["authors"] == ["Ada Example"]
    assert paper["source_url"] == BASE
    assert paper["page_urls"] == [BASE + "01-first.html", BASE + "02-second.html"]
    assert paper["page_count"] == 2


def test_front_page_is_used_for_metadata_only(book):
    paper, _ = book
    assert "Welcome" not in [s["title"] for s in paper["sections"]]
    assert paper["blocks"][0] == {"type": "heading", "text": "1 First Chapter", "section": "s1", "page": 1, "level": 1}


def test_sections_nest_and_back_matter_is_skipped(book):
    paper, _ = book
    assert [(s["title"], s["level"], s["skip_default"]) for s in paper["sections"]] == [
        ("1 First Chapter", 1, False), ("1.1 Getting Started", 2, False),
        ("A Deeper Heading", 3, False), ("References", 1, True),
    ]
    for s in paper["sections"]:
        assert paper["blocks"][s["first_block"]]["text"] == s["title"]


def test_small_headings_stay_in_the_text_but_out_of_the_contents(book):
    paper, _ = book
    assert "A Run-In Heading" not in [s["title"] for s in paper["sections"]]
    head = next(b for b in paper["blocks"] if b["type"] == "heading" and b["text"] == "A Run-In Heading")
    deeper = next(s for s in paper["sections"] if s["title"] == "A Deeper Heading")
    assert head["section"] == deeper["id"] and head["level"] == 3
    after = paper["blocks"][paper["blocks"].index(head) + 1]
    assert after["text"] == "Tiny text here." and after["section"] == deeper["id"]


def test_word_count_leaves_out_skipped_sections(book):
    paper, _ = book
    words = sum(len(b["text"].split()) for b in paper["blocks"]
                if b["type"] in ("paragraph", "heading") and b["section"] != "s4")
    assert paper["word_count"] == words > 20


def test_assets_have_images_and_are_linked_from_the_text(book):
    paper, out = book
    assert [(a["id"], a["kind"], a["label"], a["page"]) for a in paper["assets"]] == [
        ("fig-1-1", "figure", "Figure 1.1", 1), ("tab-1-1", "table", "Table 1.1", 1),
    ]
    assert paper["num_figures"] == 1 and paper["num_tables"] == 1
    for a in paper["assets"]:
        assert (out / "assets" / a["image"]).exists() and a["width"] == 200
        blk = paper["blocks"][a["first_ref_block"]]
        assert a["id"] in blk["refs"]
    assert any(b["type"] == "asset" and b["asset"] == "fig-1-1" for b in paper["blocks"])


def test_equation_and_code_blocks_carry_images(book):
    paper, out = book
    eqs = [b for b in paper["blocks"] if b["type"] == "equation"]
    assert [b.get("role") for b in eqs] == ["equation", "code"]
    for b in eqs:
        assert (out / "assets" / b["image"]).exists() and b["width"] == 200 and b["height"] == 40


def test_inline_images_are_described_on_their_paragraph(book):
    paper, out = book
    para = next(b for b in paper["blocks"] if b["type"] == "paragraph" and b.get("images"))
    (key, info), = para["images"].items()
    assert f"{MARK_A}{key}{MARK_B}" in para["text"]
    assert info["alt"] == "\\frac{a}{b}" and info["w"] == 6.0 and info["h"] == 1.2 and info["d"] == 0.3
    assert (out / "assets" / info["image"]).exists()


def test_inline_math_that_could_not_be_rendered_falls_back_to_its_source(tmp_path):
    def render_nothing(pages, assets_dir, cache_dir=None):
        assets_dir.mkdir(parents=True, exist_ok=True)
        return {}

    paper = webbook.convert(BASE, tmp_path, fetch=fake_fetch, render=render_nothing)
    text = " ".join(b.get("text", "") for b in paper["blocks"])
    assert MARK_A not in text and "\\frac{a}{b}" in text
    assert not any(b["type"] == "equation" for b in paper["blocks"])    # nothing to show, so no empty stops
    assert paper["assets"] == []


def test_footnotes_are_tagged_with_their_chapter(book):
    paper, _ = book
    assert paper["footnotes"] == [{"page": 1, "marker": "1", "text": "The note text."}]


def test_a_site_that_is_not_a_quarto_book_is_refused(tmp_path):
    def fetch(url):
        return url, "<html><head><title>Blog</title></head><body><p>hello</p></body></html>"

    with pytest.raises(webbook.WebBookError, match="Quarto"):
        webbook.convert("https://blog.test/", tmp_path, fetch=fetch, render=fake_render)


def test_crawl_does_not_leave_the_site_or_loop(tmp_path):
    pages = {
        BASE: INDEX,
        BASE + "01-first.html": CH1.replace("./02-second.html", "https://elsewhere.test/x.html"),
    }
    seen = []

    def fetch(url):
        seen.append(url)
        return url, pages[url]

    webbook.convert(BASE, tmp_path, fetch=fetch, render=fake_render)
    assert seen == [BASE, BASE + "01-first.html"]


# ---------------------------------------------------------------- fetching


class _Handler(http.server.BaseHTTPRequestHandler):
    hits: list = []
    body = b"<html><body>v1</body></html>"

    def do_GET(self):
        type(self).hits.append((self.path, self.headers.get("If-None-Match")))
        if self.headers.get("If-None-Match") == '"v1"' and type(self).body.endswith(b"v1</body></html>"):
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("ETag", '"v1"' if b"v1" in type(self).body else '"v2"')
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def site():
    _Handler.hits = []
    _Handler.body = b"<html><body>v1</body></html>"
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/page.html"
    srv.shutdown()


def test_fetcher_revalidates_instead_of_downloading_again(site, tmp_path):
    f = webbook.Fetcher(tmp_path, pause=0)
    assert "v1" in f(site)[1]
    assert "v1" in webbook.Fetcher(tmp_path, pause=0)(site)[1]         # a later run
    assert [etag for _, etag in _Handler.hits] == [None, '"v1"']       # second request was conditional (304)


def test_fetcher_picks_up_a_changed_page(site, tmp_path):
    webbook.Fetcher(tmp_path, pause=0)(site)
    _Handler.body = b"<html><body>v2</body></html>"
    assert "v2" in webbook.Fetcher(tmp_path, pause=0)(site)[1]


def test_fetcher_asks_for_each_page_only_once_per_run(site, tmp_path):
    f = webbook.Fetcher(tmp_path, pause=0)
    f(site)
    f(site)
    assert len(_Handler.hits) == 1


# ---------------------------------------------------------------- library wiring


@pytest.fixture()
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PAPERS_DIR", tmp_path)
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(config, "TMP_DIR", tmp_path / ".tmp")
    monkeypatch.setattr(webbook, "default_fetcher", lambda: fake_fetch)
    monkeypatch.setattr(webbook, "render_pages", fake_render)
    return tmp_path


def test_adding_a_web_book_creates_one_library_entry(lib):
    s1, created1 = library.add_web(BASE)
    s2, created2 = library.add_web(BASE)
    assert created1 and not created2 and s1["id"] == s2["id"]
    assert s1["source_url"] == BASE and s1["status"] == "processing"
    assert library.public_summary(s1)["source_url"] == BASE


def test_ingest_converts_a_web_book_in_its_library_folder(lib):
    s, _ = library.add_web(BASE)
    d = config.LIBRARY_DIR / s["id"]
    out = ingest.ingest_dir(d)
    assert out["status"] == "ready", out.get("error")
    assert out["title"] == "A Made-Up Book Second Try" and out["num_figures"] == 1
    paper = library.read_json(d / "paper.json")
    assert paper["source_url"] == BASE and paper["id"] == s["id"]
    assert (d / "assets" / paper["assets"][0]["image"]).exists()


def test_ingest_cli_accepts_a_web_address(lib, capsys):
    assert ingest.main([BASE]) == 0
    assert "[ready]" in capsys.readouterr().out
    assert len(library.list_summaries()) == 1


def test_web_book_ingest_reports_a_readable_error(lib, monkeypatch):
    def boom():
        def fetch(url):
            raise webbook.WebBookError("Could not download https://book.test/ (offline?)")
        return fetch

    monkeypatch.setattr(webbook, "default_fetcher", boom)
    s, _ = library.add_web(BASE)
    out = ingest.ingest_dir(config.LIBRARY_DIR / s["id"])
    assert out["status"] == "error" and out["error"] == "Could not download https://book.test/ (offline?)"


def test_highlights_on_a_web_book_are_not_flagged_as_missing_from_a_pdf(lib):
    s, _ = library.add_web(BASE)
    ingest.ingest_dir(config.LIBRARY_DIR / s["id"])
    a = annotate.create(s["id"], 1, 0, 3, "Plain words with emphasis", 1)
    assert "in_pdf" not in a
    assert "in_pdf" not in annotate.list_annotations(s["id"])[0]


# ---------------------------------------------------------------- the real renderer


CHROME = Path("/Applications/Google Chrome.app")

RENDER_DOC = """<!DOCTYPE html><html><head><meta name="generator" content="quarto-1.8.25"><title>T</title></head>
<body style="margin:0"><main class="content" id="quarto-document-content">
<header id="title-block-header"><h1 class="title">Only Page</h1></header>
<p style="font-size:20px">Text <span class="math inline" style="display:inline-block;width:60px;height:30px;background:#000">\\(\\frac{a}{b}\\)</span> more.</p>
<pre style="width:300px;height:80px;margin:0"><code>x = 1</code></pre>
</main></body></html>"""


@pytest.mark.skipif(not CHROME.exists(), reason="Google Chrome is not installed")
def test_chrome_renderer_produces_sized_images(tmp_path):
    url = "https://render.test/"
    page = webbook.parse_page(RENDER_DOC, url, page_no=1)
    shots = webbook.render_pages([page], tmp_path / "assets", cache_dir=tmp_path / "cache", wait_for_math=False)
    inline = next(shots[k] for k, role in page.shots if role == "inline")
    code = next(shots[k] for k, role in page.shots if role == "code")
    assert (tmp_path / "assets" / inline.file).read_bytes().startswith(b"\x89PNG")
    # 60x30 CSS pixels at 2x, plus a one-pixel margin above and below (none sideways: the
    # neighbouring punctuation must not leak into the picture)
    assert abs(inline.width - 120) <= 2 and abs(inline.height - 64) <= 2
    assert inline.em_w == pytest.approx(3.0, abs=0.05) and inline.em_h == pytest.approx(1.6, abs=0.05)
    assert abs(code.width - 600) <= 2 and abs(code.height - 160) <= 2
    # a second run with the same page reuses the saved images instead of opening the browser
    again = webbook.render_pages([page], tmp_path / "assets2", cache_dir=tmp_path / "cache",
                                 wait_for_math=False, launch=None)
    assert again[next(k for k, r in page.shots if r == "code")] == code
    assert (tmp_path / "assets2" / code.file).exists()


OVERFLOW_DOC = """<!DOCTYPE html><html><head><meta name="generator" content="quarto-1.8.25"><title>T</title></head>
<body style="margin:0"><main class="content" id="quarto-document-content">
<header id="title-block-header"><h1 class="title">Only Page</h1></header>
<p style="font-size:20px;line-height:1.2;margin:200px 0">Text <span class="math inline">\\(\\frac{a}{b}\\)<span
 style="display:inline-block;width:20px;height:80px;background:#000;vertical-align:middle"></span></span> more.</p>
</main></body></html>"""


@pytest.mark.skipif(not CHROME.exists(), reason="Google Chrome is not installed")
def test_inline_math_taller_than_its_line_is_not_clipped(tmp_path):
    page = webbook.parse_page(OVERFLOW_DOC, "https://render.test/", page_no=1)
    shots = webbook.render_pages([page], tmp_path / "assets", wait_for_math=False)
    (key, _), = page.shots
    assert shots[key].height >= 160                    # the 80px-tall part is all there, at 2x
    assert shots[key].em_h >= 4.0 and shots[key].descent > 1.0
