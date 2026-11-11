"""Quarto book site -> the same Paper dict that the PDF conversion produces.

    uv run python -m backend.ingest https://example.org/some-quarto-book/

The site is read once, page by page (following each page's "next" link), with a pause between
requests; pages are kept under papers/.webcache/ and only re-validated on later runs. Text comes
from the HTML. Everything the reader shows as a picture (figures, tables, displayed equations, code
listings, and inline math too involved to spell with plain characters) is photographed from the
rendered page with the installed Google Chrome, and those pictures are cached per page as well.

Chapters play the role of PDF pages: block["page"] is the chapter's position in the book.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import shutil
import struct
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from . import config, library
from .textutil import number_slug

log = logging.getLogger("webbook")

MARK_A, MARK_B = "\ue000", "\ue001"     # wrap the key of an inline image inside paragraph text
MAX_PAGES = 300
MAX_INLINE_CHARS = 16                    # longer inline math is shown as an image, not spelled out
RENDER_VERSION = "3"                     # bump to invalidate cached page images
USER_AGENT = "reading-tool-web-import"
BLOCKED_HOSTS = ("googletagmanager.com", "google-analytics.com", "doubleclick.net")
CODE_LINK_RE = re.compile(r"\.(do|r|py|ipynb|csv|dta|jl)$", re.I)
SKIP_TITLE_RE = re.compile(r"^(references?|bibliography|works cited|acknowledge?ments?)\b", re.I)
CAPTION_RE = re.compile(r"^(Figure|Table)\s+([\w.\-]+?)\s*[:.]\s+(.*)$", re.S)
CAPTION_BARE_RE = re.compile(r"^(Figure|Table)\s+([\w.\-]*\d[\w.\-]*)\s*$")
BLOCK_TAGS = {
    "p", "div", "section", "ul", "ol", "dl", "pre", "table", "figure", "blockquote", "details",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "img", "article", "aside", "hr",
}
SKIP_TAGS = {"script", "style", "nav", "button", "hr", "summary", "colgroup", "noscript", "template"}


class WebBookError(Exception):
    """An import failure with a message meant for the user."""


@dataclass
class Shot:
    """One picture taken from a rendered page. Sizes in pixels; em_* relative to the text size."""
    file: str
    width: int
    height: int
    em_w: float = 0.0
    em_h: float = 0.0
    descent: float = 0.0     # how far (em) the picture hangs below the text baseline


@dataclass
class Page:
    url: str
    page_no: int
    title: str = ""
    next_url: str | None = None
    items: list[dict] = field(default_factory=list)
    footnotes: list[dict] = field(default_factory=list)
    shots: list[tuple[str, str]] = field(default_factory=list)   # (key, role); role: asset|equation|code|inline
    marked_html: str = ""                                        # the page with data-rt="<key>" on every shot
    is_quarto: bool = False
    book_title: str = ""
    authors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# inline math -> plain characters
# ---------------------------------------------------------------------------

_GREEK = dict(
    alpha="α", beta="β", gamma="γ", delta="δ", epsilon="ε", varepsilon="ε", zeta="ζ", eta="η", theta="θ",
    vartheta="ϑ", iota="ι", kappa="κ", mu="μ", nu="ν", xi="ξ", pi="π", rho="ρ", sigma="σ", tau="τ",
    upsilon="υ", phi="φ", varphi="φ", chi="χ", psi="ψ", omega="ω", Gamma="Γ", Delta="Δ", Theta="Θ",
    Lambda="Λ", Xi="Ξ", Pi="Π", Sigma="Σ", Phi="Φ", Psi="Ψ", Omega="Ω",
)
_GREEK["lambda"] = "λ"
_SYMBOLS = {
    **_GREEK,
    "times": "×", "cdot": "·", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "equiv": "≡", "sim": "∼", "rightarrow": "→", "to": "→", "leftarrow": "←",
    "Rightarrow": "⇒", "infty": "∞", "pm": "±", "in": "∈", "notin": "∉", "perp": "⊥", "mid": "|",
    "partial": "∂", "ldots": "…", "dots": "…", "cdots": "⋯", "prime": "′", "ell": "ℓ", "neg": "¬",
    "cap": "∩", "cup": "∪", "subset": "⊂", "forall": "∀", "exists": "∃", "nabla": "∇", "propto": "∝",
    "%": "%", "$": "$", "&": "&", "#": "#", "_": "_", "{": "{", "}": "}", "|": "‖",
    ",": "", ";": "", ":": "", "!": "", " ": "", "quad": "", "qquad": "",
    "left": "", "right": "", "big": "", "Big": "", "bigg": "", "Bigg": "", "displaystyle": "", "textstyle": "",
    "lbrace": "{", "rbrace": "}", "langle": "⟨", "rangle": "⟩", "vert": "|",
}
_WRAPPERS = {"text", "mathrm", "mathit", "mathbf", "textit", "textbf", "textrm", "operatorname", "mathsf", "boldsymbol", "bm"}
_ACCENTS = {"hat": "\u0302", "widehat": "\u0302", "bar": "\u0304", "overline": "\u0304", "tilde": "\u0303",
            "widetilde": "\u0303", "dot": "\u0307", "vec": "\u20d7"}
_BLACKBOARD = {"E": "𝔼", "R": "ℝ", "N": "ℕ", "P": "ℙ", "Z": "ℤ", "Q": "ℚ", "1": "𝟙"}
_SUP = dict(zip("0123456789+-=()abcdefghijklmnoprstuvwxyzT*′'",
                "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ᵃᵇᶜᵈᵉᶠᵍʰⁱʲᵏˡᵐⁿᵒᵖʳˢᵗᵘᵛʷˣʸᶻᵀ*′′"))
_SUB = dict(zip("0123456789+-=()aehijklmnoprstuvxβγρφχ",
                "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓᵦᵧᵨᵩᵪ"))


def _tex_arg(s: str, i: int) -> tuple[str | None, int]:
    """The argument starting at s[i]: a {group}, a \\command, or one character."""
    while i < len(s) and s[i] == " ":
        i += 1
    if i >= len(s):
        return None, i
    if s[i] == "{":
        depth = 0
        for j in range(i, len(s)):
            if s[j] == "{" and (j == 0 or s[j - 1] != "\\"):
                depth += 1
            elif s[j] == "}" and s[j - 1] != "\\":
                depth -= 1
                if depth == 0:
                    return s[i + 1:j], j + 1
        return None, len(s)
    if s[i] == "\\":
        m = re.match(r"\\([A-Za-z]+|.)", s[i:])
        return (m.group(0), i + len(m.group(0))) if m else (None, len(s))
    return s[i], i + 1


def _tex_conv(s: str) -> str | None:
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+|.)", s[i:], re.S)
            if not m:
                return None
            name = m.group(1)
            i += len(m.group(0))
            if name in _SYMBOLS:
                out.append(_SYMBOLS[name])
            elif name in _WRAPPERS or name in _ACCENTS or name == "mathbb":
                arg, i = _tex_arg(s, i)
                inner = _tex_conv(arg) if arg is not None else None
                if inner is None:
                    return None
                if name in _ACCENTS:
                    if len(inner) != 1:
                        return None
                    inner += _ACCENTS[name]
                elif name == "mathbb":
                    if inner not in _BLACKBOARD:
                        return None
                    inner = _BLACKBOARD[inner]
                out.append(inner)
            else:
                return None
        elif c in "_^":
            arg, i = _tex_arg(s, i + 1)
            inner = _tex_conv(arg) if arg is not None else None
            table = _SUB if c == "_" else _SUP
            if not inner or any(ch not in table for ch in inner):
                return None
            out.append("".join(table[ch] for ch in inner))
        elif c == "{":
            arg, i = _tex_arg(s, i)
            inner = _tex_conv(arg) if arg is not None else None
            if inner is None:
                return None
            out.append(inner)
        elif c in "}&$#":
            return None
        elif c == "~":
            i += 1
        elif c == "'":
            out.append("′")
            i += 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def tex_to_text(tex: str) -> str | None:
    """Inline TeX spelled with plain characters ("β₁"), or None when that would not be readable."""
    s = (tex or "").strip()
    if not s or len(s) > 120:
        return None
    out = _tex_conv(s)
    if out is None:
        return None
    out = re.sub(r"\s+", "", out)
    if not out or len(out) > MAX_INLINE_CHARS:
        return None
    return out


def _only_definitions(tex: str) -> bool:
    """True for an empty block or one made of nothing but \\newcommand-style definitions."""
    i = 0
    while True:
        while i < len(tex) and tex[i].isspace():
            i += 1
        if i >= len(tex):
            return True
        m = re.match(r"\\(?:(?:re|provide)?newcommand|DeclareMathOperator|def)\*?", tex[i:])
        if not m:
            return False
        name, i = _tex_arg(tex, i + len(m.group(0)))
        if name is None:
            return False
        while tex[i:i + 1] == "[":
            j = tex.find("]", i)
            if j < 0:
                return False
            i = j + 1
        body, i = _tex_arg(tex, i)
        if body is None:
            return False


def _strip_math_delims(tex: str) -> str:
    t = tex.strip()
    for a, b in (("\\(", "\\)"), ("\\[", "\\]"), ("$$", "$$"), ("$", "$")):
        if t.startswith(a) and t.endswith(b) and len(t) >= len(a) + len(b):
            return t[len(a):len(t) - len(b)].strip()
    return t


# ---------------------------------------------------------------------------
# one page
# ---------------------------------------------------------------------------


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def _classes(el: Tag) -> list[str]:
    c = el.get("class") or []
    return c.split() if isinstance(c, str) else list(c)


def _tags(el: Tag) -> list[Tag]:
    return [c for c in el.children if isinstance(c, Tag)]


class _PageParser:
    def __init__(self, soup: BeautifulSoup, url: str, page_no: int, lang: str) -> None:
        self.soup = soup
        self.page = Page(url=url, page_no=page_no)
        self.lang = (lang or "stata").lower()
        self.n = 0
        self.unnumbered = 0
        # the paragraph being collected
        self.parts: list[str] = []
        self.targets: list[str] = []
        self.images: dict[str, str] = {}

    # -- shots -----------------------------------------------------------------
    def shot(self, el: Tag, role: str) -> str:
        self.n += 1
        key = f"p{self.page.page_no}-{self.n}"
        el["data-rt"] = key
        self.page.shots.append((key, role))
        return key

    # -- inline text ---------------------------------------------------------------
    def plain(self, node) -> str:
        """Text of ``node`` without creating any shots (captions, headings, footnotes)."""
        out: list[str] = []

        def rec(n) -> None:
            for c in n.children:
                if isinstance(c, NavigableString):
                    out.append(str(c))
                elif isinstance(c, Tag):
                    cls = _classes(c)
                    if c.name in SKIP_TAGS or "footnote-ref" in cls or "footnote-back" in cls:
                        continue
                    if c.name == "br":
                        out.append(" ")
                    elif "math" in cls:
                        tex = _strip_math_delims(c.get_text())
                        out.append(tex_to_text(tex) or tex)
                    else:
                        rec(c)

        rec(node)
        return _clean("".join(out))

    def flush(self) -> None:
        text = _clean("".join(self.parts))
        if text:
            item = {"type": "paragraph", "text": text, "targets": self.targets}
            if self.images:
                item["images"] = self.images
            self.page.items.append(item)
        self.parts, self.targets, self.images = [], [], {}

    def flow(self, node) -> None:
        """Collect the inline content of ``node`` into the current paragraph."""
        for c in node.children:
            if isinstance(c, NavigableString):
                self.parts.append(str(c))
                continue
            if not isinstance(c, Tag):
                continue
            cls = _classes(c)
            if c.name in SKIP_TAGS or "footnote-ref" in cls or "footnote-back" in cls:
                continue
            if c.name == "br":
                self.parts.append(" ")
            elif "math" in cls and "display" in cls:
                self.flush()
                self.equation(c)
            elif "math" in cls:
                tex = _strip_math_delims(c.get_text())
                text = tex_to_text(tex)
                if text is None and tex:
                    key = self.shot(c, "inline")
                    self.images[key] = tex
                    text = f"{MARK_A}{key}{MARK_B}"
                self.parts.append(text or "")
            elif c.name == "img":
                self.flush()
                self.loose_asset(c, "figure")
            elif c.name in BLOCK_TAGS and c.name not in ("img",):
                self.flush()
                self.block(c)
            else:
                if c.name == "a":
                    frag = urldefrag(c.get("href") or "").fragment
                    if frag.startswith(("fig-", "tbl-")) and frag not in self.targets:
                        self.targets.append(frag)
                self.flow(c)

    # -- blocks ----------------------------------------------------------------
    def heading(self, text: str, level: int, minor: bool = False) -> None:
        text = _clean(text)
        if not text:
            return
        last = next((it for it in reversed(self.page.items) if it["type"] == "heading"), None)
        if last and last["text"] == text and self.page.items[-1] is last:
            return
        if not self.page.title:
            self.page.title = text
        item = {"type": "heading", "level": level, "text": text}
        if minor:   # h4 and deeper: a title card in the text, but not an entry in the contents
            item["minor"] = True
        self.page.items.append(item)

    def equation(self, el: Tag) -> None:
        tex = _strip_math_delims(el.get_text())
        if _only_definitions(tex):
            return                          # a block that only defines macros prints nothing
        self.page.items.append({"type": "equation", "key": self.shot(el, "equation"), "role": "equation", "alt": tex})

    def code(self, el: Tag) -> None:
        text = el.get_text().strip("\n")
        if text.strip():
            self.page.items.append({"type": "equation", "key": self.shot(el, "code"), "role": "code", "alt": text})

    def float_asset(self, el: Tag) -> None:
        dom_id = el.get("id") or ""
        fig = el if el.name == "figure" else (el.find("figure") or el)
        cap_el = fig.find("figcaption")
        cap = self.plain(cap_el) if cap_el else ""
        kind = "table" if dom_id.startswith("tbl-") or "quarto-float-tbl" in _classes(fig) else "figure"
        number, caption = "", cap
        m = CAPTION_RE.match(cap) or CAPTION_BARE_RE.match(cap)
        if m:
            kind = m.group(1).lower()
            number = m.group(2)
            caption = m.group(3).strip() if m.re is CAPTION_RE else ""
        self.asset(fig, kind, number, caption, dom_id)

    def loose_asset(self, el: Tag, kind: str) -> None:
        cap_el = el.find("figcaption") if el.name != "img" else None
        caption = self.plain(cap_el) if cap_el else _clean(el.get("alt") or "")
        self.asset(el, kind, "", caption, el.get("id") or "")

    def asset(self, el: Tag, kind: str, number: str, caption: str, dom_id: str) -> None:
        word = "Figure" if kind == "figure" else "Table"
        self.page.items.append({
            "type": "asset", "key": self.shot(el, "asset"), "kind": kind, "number": number,
            "label": f"{word} {number}".strip(), "caption": caption, "dom_id": dom_id,
        })

    def tabset(self, el: Tag) -> None:
        labels = [_clean(a.get_text()).lower() for a in el.select("ul.nav a.nav-link")]
        content = el.find("div", class_="tab-content")
        panes = [p for p in _tags(content) if "tab-pane" in _classes(p)] if content else []
        if not panes:
            return self.walk(el)
        pick = next((i for i, l in enumerate(labels) if l.startswith(self.lang)), 0)
        pick = min(pick, len(panes) - 1)
        for i, pane in enumerate(panes):
            cls = [c for c in _classes(pane) if c not in ("active", "show")]
            pane["class"] = cls + (["active", "show"] if i == pick else [])
        self.walk(panes[pick])

    def footnotes(self, el: Tag) -> None:
        for n, li in enumerate(el.find_all("li"), 1):
            text = self.plain(li)
            if text:
                m = re.search(r"(\d+)$", li.get("id") or "")
                self.page.footnotes.append({"marker": m.group(1) if m else str(n), "text": text})

    def title_block(self, el: Tag) -> None:
        h1 = el.find("h1")
        if h1 is None:
            return
        num, title = h1.find(class_="chapter-number"), h1.find(class_="chapter-title")
        if num is not None and title is not None:
            self.heading(f"{self.plain(num)} {self.plain(title)}", 1)
        else:
            self.heading(self.plain(h1), 1)

    def block(self, el: Tag) -> None:
        name, cls, eid = el.name, _classes(el), el.get("id") or ""
        if name in SKIP_TAGS or "quarto-title-meta" in cls or "hidden" in cls:
            return
        if name == "header" and eid == "title-block-header":
            return self.title_block(el)
        if eid == "footnotes" or "footnotes" in cls:
            return self.footnotes(el)
        if re.fullmatch(r"h[1-6]", name):
            return self.heading(self.plain(el), min(3, int(name[1])), minor=int(name[1]) > 3)
        if "panel-tabset" in cls:
            return self.tabset(el)
        if "quarto-float" in cls or (name == "div" and eid.startswith(("fig-", "tbl-")) and el.find("figure")):
            return self.float_asset(el)
        if name == "figure":
            return self.loose_asset(el, "table" if el.find("table") and not el.find("img") else "figure")
        if name == "table":
            return self.loose_asset(el, "table")
        if name == "img":
            return self.loose_asset(el, "figure")
        if name == "pre":
            return self.code(el)
        if eid == "refs" or "references" in cls:
            entries = el.find_all(class_="csl-entry")
            if entries:
                for e in entries:
                    self.flow(e)
                    self.flush()
                return
        if name == "details":
            el["open"] = ""
        if name == "p":
            kids = [c for c in el.children if not (isinstance(c, NavigableString) and not str(c).strip())]
            if len(kids) == 1 and isinstance(kids[0], Tag) and kids[0].name == "a" and CODE_LINK_RE.search(kids[0].get("href") or ""):
                return                      # a bare link to a script file above a code listing
        if name in ("p", "li", "dt", "dd") or not any(c.name in BLOCK_TAGS for c in _tags(el)):
            self.flow(el)
            return self.flush()
        self.walk(el)

    def walk(self, node: Tag) -> None:
        for c in node.children:
            if isinstance(c, NavigableString):
                if str(c).strip() and c.__class__ is NavigableString:
                    self.parts.append(str(c))
                    self.flush()
            elif isinstance(c, Tag):
                self.block(c)


def parse_page(html: str, url: str, page_no: int, lang: str = "stata") -> Page:
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        if img.has_attr("loading"):
            del img["loading"]
    p = _PageParser(soup, url, page_no, lang)
    page = p.page
    gen = soup.find("meta", attrs={"name": "generator"})
    page.is_quarto = bool(gen and str(gen.get("content") or "").lower().startswith("quarto"))
    page.authors = [_clean(m.get("content") or "") for m in soup.find_all("meta", attrs={"name": "author"}) if m.get("content")]
    nxt = soup.find("link", rel="next")
    if nxt is not None and nxt.get("href"):
        page.next_url = urldefrag(urljoin(url, nxt["href"])).url
    main = soup.find("main") or soup.body or soup
    head = main.find("header", id="title-block-header") if isinstance(main, Tag) else None
    h1 = head.find("h1") if head else None
    page.book_title = p.plain(h1) if h1 else _clean(soup.title.get_text() if soup.title else "")
    p.walk(main)
    p.flush()
    page.marked_html = str(soup)
    return page


# ---------------------------------------------------------------------------
# fetching (polite, cached, re-validated)
# ---------------------------------------------------------------------------


class Fetcher:
    """url -> (final url, html). Each page is asked for at most once per run; later runs send the
    saved ETag / Last-Modified so an unchanged page is not downloaded again."""

    def __init__(self, cache_dir: Path, pause: float = 1.5) -> None:
        self.dir = Path(cache_dir)
        self.pause = pause
        self._memo: dict[str, tuple[str, str]] = {}

    def __call__(self, url: str) -> tuple[str, str]:
        if url in self._memo:
            return self._memo[url]
        if urlsplit(url).scheme not in ("http", "https"):
            raise WebBookError(f"Not a web address: {url}")
        self.dir.mkdir(parents=True, exist_ok=True)
        stem = hashlib.sha256(url.encode()).hexdigest()[:24]
        f_html, f_meta = self.dir / f"{stem}.html", self.dir / f"{stem}.json"
        meta = library.read_json(f_meta) if f_html.exists() else None
        headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
        if isinstance(meta, dict):
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]
        else:
            meta = None
        try:
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
                    html = resp.read().decode("utf-8", errors="replace")
                    meta = {"url": url, "final_url": resp.geturl(), "etag": resp.headers.get("ETag"),
                            "last_modified": resp.headers.get("Last-Modified")}
                f_html.write_text(html, encoding="utf-8")
                library.atomic_write_json(f_meta, meta)
            except urllib.error.HTTPError as e:
                if e.code != 304 or meta is None:
                    raise
                html = f_html.read_text(encoding="utf-8")
            finally:
                if self.pause:
                    time.sleep(self.pause)
        except (urllib.error.URLError, OSError) as e:
            if meta is None:
                reason = f"HTTP {e.code}" if isinstance(e, urllib.error.HTTPError) else str(getattr(e, "reason", e))
                raise WebBookError(f"Could not download {url} ({reason}).") from e
            log.warning("could not re-check %s (%s); using the saved copy", url, e)
            html = f_html.read_text(encoding="utf-8")
        out = (meta.get("final_url") or url, html)
        self._memo[url] = out
        return out


def cache_root() -> Path:
    return config.PAPERS_DIR / ".webcache"


def default_fetcher() -> Callable[[str], tuple[str, str]]:
    return Fetcher(cache_root() / "pages")


# ---------------------------------------------------------------------------
# rendering (Google Chrome, headless)
# ---------------------------------------------------------------------------

RENDER_CSS = """
#quarto-header, #quarto-sidebar, #quarto-margin-sidebar, .nav-footer, footer, .code-copy-button,
#quarto-search-results, .quarto-secondary-nav, #quarto-back-to-top, .code-copy-outer-scaffold > button { display: none !important; }
html { scroll-behavior: auto !important; }
html, body, #quarto-content, main, .page-columns, .content { background: transparent !important; }
figure[data-rt] > figcaption { display: none !important; }
figure[data-rt], [data-rt] { margin-top: 0 !important; margin-bottom: 0 !important; }
"""

_MEASURE_JS = """(items) => {
  const out = {};
  for (const [key, role] of items) {
    let el = document.querySelector(`[data-rt="${key}"]`);
    if (!el) continue;
    if (role === 'inline' || role === 'equation') el = el.querySelector('mjx-container') || el;
    el.setAttribute('data-rt-shot', key);
    const r = el.getBoundingClientRect();
    const fs = parseFloat(getComputedStyle(el.parentElement || el).fontSize) || 16;
    let descent = 0;
    if (role === 'inline') {
      const probe = document.createElement('span');
      probe.style.cssText = 'display:inline-block;width:0;height:0';
      el.after(probe);
      descent = Math.max(0, r.bottom - probe.getBoundingClientRect().bottom);
      probe.remove();
    }
    out[key] = { w: r.width, h: r.height, fs, descent };
  }
  return out;
}"""

# Math is drawn outside the box of its own element (fractions, limits, tall brackets), so the picture
# is taken of the area covered by everything inside it. MathJax's hidden screen-reader copy is left out.
_INK_JS = """([key, padX, padY]) => {
  const el = document.querySelector(`[data-rt-shot="${key}"]`);
  if (!el) return null;
  el.scrollIntoView({ block: 'center', inline: 'nearest' });
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const n of [el, ...el.querySelectorAll('*')]) {
    if (n.closest('mjx-assistive-mml')) continue;
    const r = n.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top); x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom);
  }
  if (!(x1 > x0 && y1 > y0)) return null;
  x0 = Math.floor(x0) - padX; y0 = Math.floor(y0) - padY; x1 = Math.ceil(x1) + padX; y1 = Math.ceil(y1) + padY;
  const probe = document.createElement('span');
  probe.style.cssText = 'display:inline-block;width:0;height:0';
  el.after(probe);
  const base = probe.getBoundingClientRect().bottom;
  probe.remove();
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0, descent: Math.max(0, y1 - base),
           fits: x0 >= 0 && y0 >= 0 && x1 <= window.innerWidth && y1 <= window.innerHeight };
}"""

_SETTLE_JS = """async () => {
  if (document.fonts && document.fonts.ready) await document.fonts.ready;
  await Promise.all(Array.from(document.images).map((img) => img.complete ? 0
    : new Promise((done) => { img.addEventListener('load', done); img.addEventListener('error', done); })));
  return 1;
}"""


@contextlib.contextmanager
def launch_chrome():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - dependency is declared
        raise WebBookError("The playwright package is missing; run `uv sync` in the app folder.") from e
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="chrome", headless=True)
        except Exception as e:
            raise WebBookError("Importing a web book needs Google Chrome to draw its tables and equations, "
                               "and Chrome could not be started.") from e
        try:
            yield browser
        finally:
            browser.close()


def _png_size(path: Path) -> tuple[int, int]:
    with open(path, "rb") as f:
        head = f.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return 0, 0
    return struct.unpack(">II", head[16:24])


def _page_digest(page: Page) -> str:
    return hashlib.sha256((RENDER_VERSION + "\n" + page.marked_html).encode()).hexdigest()[:24]


def _render_one(browser, page: Page, dest: Path, wait_for_math: bool) -> dict[str, Shot]:
    ctx = browser.new_context(device_scale_factor=2, viewport={"width": 1280, "height": 1600})
    try:
        tab = ctx.new_page()

        def handle(route) -> None:
            req = route.request
            host = urlsplit(req.url).netloc
            if req.resource_type == "document" and urldefrag(req.url).url == urldefrag(page.url).url:
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=page.marked_html)
            elif any(host == h or host.endswith("." + h) for h in BLOCKED_HOSTS):
                route.abort()
            else:
                route.continue_()

        tab.route("**/*", handle)
        tab.goto(page.url, wait_until="load", timeout=120_000)
        tab.add_style_tag(content=RENDER_CSS)
        if wait_for_math:
            try:
                tab.wait_for_function("() => window.MathJax && window.MathJax.startup && window.MathJax.startup.promise",
                                      timeout=20_000)
                tab.evaluate("() => window.MathJax.startup.promise.then(() => 1)")
            except Exception:
                log.info("no MathJax on %s", page.url)
        try:
            tab.evaluate(_SETTLE_JS)
        except Exception as e:
            log.warning("page did not settle: %s", e)
        metrics = tab.evaluate(_MEASURE_JS, [list(s) for s in page.shots])
        out: dict[str, Shot] = {}
        for key, role in page.shots:
            m = metrics.get(key)
            if not m or m["w"] < 2 or m["h"] < 2:
                continue
            name = f"{key}.png"
            try:
                ink = tab.evaluate(_INK_JS, [key, *((0, 1) if role == "inline" else (4, 4))]) if role in ("inline", "equation") else None
                if ink and ink["fits"]:
                    tab.screenshot(path=str(dest / name), omit_background=(role == "inline"), animations="disabled",
                                   clip={"x": ink["x"], "y": ink["y"], "width": ink["w"], "height": ink["h"]})
                    m = {**m, "w": ink["w"], "h": ink["h"], "descent": ink["descent"]}
                else:
                    tab.locator(f'[data-rt-shot="{key}"]').first.screenshot(
                        path=str(dest / name), omit_background=(role == "inline"), animations="disabled", timeout=30_000)
            except Exception as e:
                log.warning("could not picture %s on %s: %s", key, page.url, e)
                continue
            w, h = _png_size(dest / name)
            if not w or not h:
                continue
            fs = m["fs"] or 16
            out[key] = Shot(file=name, width=w, height=h, em_w=round(m["w"] / fs, 3), em_h=round(m["h"] / fs, 3),
                            descent=round(m["descent"] / fs, 3))
        return out
    finally:
        ctx.close()


def render_pages(pages: list[Page], assets_dir: Path, cache_dir: Path | None = None, wait_for_math: bool = True,
                 launch=launch_chrome) -> dict[str, Shot]:
    """Take every picture the pages ask for. Returns {key: Shot}; files land in ``assets_dir``.
    With a ``cache_dir`` a page whose HTML has not changed is not rendered again."""
    assets_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, Shot] = {}
    todo: list[tuple[Page, Path | None]] = []
    for page in pages:
        if not page.shots:
            continue
        cdir = cache_dir / "shots" / _page_digest(page) if cache_dir else None
        manifest = library.read_json(cdir / "manifest.json") if cdir else None
        if isinstance(manifest, dict) and all((cdir / s["file"]).exists() for s in manifest.values()):
            for key, s in manifest.items():
                shutil.copyfile(cdir / s["file"], assets_dir / s["file"])
                out[key] = Shot(**s)
        else:
            todo.append((page, cdir))
    if not todo:
        return out
    if launch is None:
        raise WebBookError("Some pages have not been rendered yet and no browser was provided.")
    with launch() as browser:
        for i, (page, cdir) in enumerate(todo, 1):
            log.info("rendering %d/%d: %s (%d pictures)", i, len(todo), page.url, len(page.shots))
            dest = assets_dir
            if cdir is not None:
                shutil.rmtree(cdir, ignore_errors=True)
                cdir.mkdir(parents=True)
                dest = cdir
            shots = _render_one(browser, page, dest, wait_for_math)
            if cdir is not None:
                library.atomic_write_json(cdir / "manifest.json", {k: asdict(s) for k, s in shots.items()})
                for s in shots.values():
                    shutil.copyfile(cdir / s.file, assets_dir / s.file)
            out.update(shots)
    return out


# ---------------------------------------------------------------------------
# the whole book
# ---------------------------------------------------------------------------


def crawl(url: str, fetch: Callable[[str], tuple[str, str]], lang: str) -> tuple[Page, list[Page]]:
    """-> (front page, content pages in reading order)."""
    final, html = fetch(url)
    front = parse_page(html, final, 0, lang)
    if not front.is_quarto:
        raise WebBookError("This does not look like a Quarto book site, which is the only kind of site "
                           "the importer understands so far.")
    host = urlsplit(final).netloc
    seen = {url, final}
    pages: list[Page] = []
    nxt = front.next_url
    while nxt and nxt not in seen and urlsplit(nxt).netloc == host and len(pages) < MAX_PAGES:
        seen.add(nxt)
        f, h = fetch(nxt)
        seen.add(f)
        page = parse_page(h, f, len(pages) + 1, lang)
        pages.append(page)
        nxt = page.next_url
    if not pages:  # a one-page site: the front page is the text
        pages = [parse_page(html, final, 1, lang)]
    return front, pages


def _unique(base: str, used: set[str]) -> str:
    out, n = base, 2
    while out in used:
        out, n = f"{base}-{n}", n + 1
    used.add(out)
    return out


def build_paper(url: str, front: Page, pages: list[Page], shots: dict[str, Shot], seconds: float = 0.0) -> dict:
    sections: list[dict] = []
    blocks: list[dict] = []
    assets: list[dict] = []
    footnotes: list[dict] = []
    by_dom: dict[str, str] = {}
    used_ids: set[str] = set()
    pending_refs: list[tuple[dict, list[str]]] = []
    skipped: set[str] = set()
    skip_level: int | None = None
    sec = None
    for page in pages:
        for it in page.items:
            kind = it["type"]
            if kind == "heading" and it.get("minor") and sec is not None:
                blocks.append({"type": "heading", "text": it["text"], "section": sec, "page": page.page_no, "level": it["level"]})
                continue
            if kind == "heading":
                level = it["level"]
                sid = f"s{len(sections) + 1}"
                if skip_level is not None and level > skip_level:
                    skip = True
                else:
                    skip_level = None
                    skip = level == 1 and bool(SKIP_TITLE_RE.match(re.sub(r"^[\d.]+\s+", "", it["text"])))
                    if skip:
                        skip_level = level
                if skip:
                    skipped.add(sid)
                sections.append({"id": sid, "title": it["text"], "level": level, "first_block": len(blocks),
                                 "skip_default": skip})
                sec = sid
                blocks.append({"type": "heading", "text": it["text"], "section": sid, "page": page.page_no, "level": level})
                continue
            if sec is None:
                sections.append({"id": "s0", "title": "Start", "level": 1, "first_block": len(blocks), "skip_default": False})
                sec = "s0"
            if kind == "paragraph":
                text = it["text"]
                images = {}
                for key, tex in (it.get("images") or {}).items():
                    s = shots.get(key)
                    if s is None:
                        text = text.replace(f"{MARK_A}{key}{MARK_B}", tex)
                    else:
                        images[key] = {"image": s.file, "w": s.em_w, "h": s.em_h, "d": s.descent, "alt": tex}
                blk = {"type": "paragraph", "text": text, "section": sec, "page": page.page_no, "refs": []}
                if images:
                    blk["images"] = images
                blocks.append(blk)
                if it.get("targets"):
                    pending_refs.append((blk, it["targets"]))
            elif kind == "equation":
                s = shots.get(it["key"])
                if s is None:
                    continue
                blocks.append({"type": "equation", "text": "", "image": s.file, "width": s.width, "height": s.height,
                               "section": sec, "page": page.page_no, "alt": it["alt"], "role": it["role"]})
            elif kind == "asset":
                s = shots.get(it["key"])
                if s is None:
                    continue
                prefix = "fig" if it["kind"] == "figure" else "tab"
                tail = number_slug(it["number"]) if it["number"] else f"p{page.page_no}-u{len(assets) + 1}"
                aid = _unique(f"{prefix}-{tail}", used_ids)
                if it["dom_id"]:
                    by_dom.setdefault(it["dom_id"], aid)
                assets.append({
                    "id": aid, "kind": it["kind"], "number": it["number"], "label": it["label"],
                    "caption": it["caption"], "page": page.page_no, "pages": [page.page_no], "image": s.file,
                    "width": s.width, "height": s.height, "first_ref_block": None,
                })
                blocks.append({"type": "asset", "asset": aid, "section": sec, "page": page.page_no})
        footnotes.extend({"page": page.page_no, **f} for f in page.footnotes)

    asset_by_id = {a["id"]: a for a in assets}
    index_of = {id(b): i for i, b in enumerate(blocks)}
    for blk, targets in pending_refs:
        for t in targets:
            aid = by_dom.get(t)
            if aid and aid not in blk["refs"]:
                blk["refs"].append(aid)
                if asset_by_id[aid]["first_ref_block"] is None:
                    asset_by_id[aid]["first_ref_block"] = index_of[id(blk)]
    word_count = sum(len(b["text"].split()) for b in blocks
                     if b["type"] in ("paragraph", "heading") and b["section"] not in skipped)
    return {
        "title": front.book_title or (pages[0].title if pages else "") or url,
        "authors": front.authors,
        "year": None,
        "page_count": len(pages),
        "word_count": word_count,
        "num_figures": sum(1 for a in assets if a["kind"] == "figure"),
        "num_tables": sum(1 for a in assets if a["kind"] == "table"),
        "sections": sections,
        "blocks": blocks,
        "assets": assets,
        "footnotes": footnotes,
        "source_url": url,
        "page_urls": [p.url for p in pages],
        "ingest_seconds": round(seconds, 2),
    }


def convert(url: str, out_dir: Path, fetch=None, render=None, lang: str = "stata") -> dict:
    """Import the book at ``url``. Writes PNGs into ``out_dir/assets`` and returns the Paper dict."""
    t0 = time.time()
    if urlsplit(url).scheme not in ("http", "https"):
        raise WebBookError(f"Not a web address: {url}")
    fetch = fetch or default_fetcher()
    render = render or render_pages
    front, pages = crawl(url, fetch, lang)
    shots = render(pages, Path(out_dir) / "assets", cache_dir=cache_root())
    paper = build_paper(url, front, pages, shots, time.time() - t0)
    if paper["word_count"] < 30:
        raise WebBookError("No readable text was found on this site.")
    log.info("imported %s: %d pages, %d blocks, %d assets, %.1fs", url, len(pages), len(paper["blocks"]),
             len(paper["assets"]), time.time() - t0)
    return paper
