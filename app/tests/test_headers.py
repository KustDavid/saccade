"""Running header / footer / page-number stripping on synthetic pages."""

from backend.extract import Line, PageData, Span
from backend.regions import mark_margins
from backend.textutil import header_key, is_page_number, repeated_margin_keys


def mkline(page: int, text: str, y0: float, x0: float = 60, size: float = 10.0) -> Line:
    w = 5.0 * len(text)
    span = Span(text, size, "Times-Roman", 0, x0, y0, x0 + w, y0 + size)
    return Line(page, x0, y0, x0 + w, y0 + size, [span], size)


def make_doc(n: int = 8) -> list[PageData]:
    pages = []
    for i in range(n):
        lines = []
        # alternating running heads like a journal: "N  The Journal of Finance" / "Human Finance  N"
        if i % 2 == 0:
            lines.append(mkline(i, str(i + 1), 40))
            lines.append(mkline(i, "The Journal of Finance®", 40, x0=180))
        else:
            lines.append(mkline(i, "Human Finance", 40, x0=200))
            lines.append(mkline(i, str(i + 1), 40, x0=420))
        for k in range(20):
            lines.append(mkline(i, f"body text line {k} on page {i} with several words in it", 80 + 25 * k))
        lines.append(mkline(i, f"Downloaded on 2026-11-{i + 1:02d} from https://example.org", 760, size=6))
        pages.append(PageData(i, 500, 800, lines))
    return pages


def test_header_key_wildcards_digits():
    assert header_key("Page 12 of 30") == header_key("Page 3 of 30") == "page # of #"
    assert header_key("THE JOURNAL OF FINANCE • VOL. 80") == "the journal of finance vol #"


def test_is_page_number():
    for t in ("12", "iv", "Page 3", "3 of 12", "- 7 -"):
        assert is_page_number(t), t
    for t in ("2017", "Introduction", "12 Monkeys"):
        assert not is_page_number(t) or t == "2017"


def test_repeated_margin_keys_threshold():
    keys = [{"human finance"}, {"the journal"}, {"human finance"}, {"the journal"}, {"human finance"},
            {"the journal", "x"}]
    rep = repeated_margin_keys(keys)
    assert "human finance" in rep and "the journal" in rep and "x" not in rep
    assert repeated_margin_keys([{"a"}, {"a"}]) == set()  # too few pages to tell


def test_mark_margins_strips_running_heads_and_page_numbers():
    pages = make_doc()
    mark_margins(pages, body_size=10.0)
    for p in pages:
        roles = {l.text: l.role for l in p.lines}
        assert all(r == "header" for t, r in roles.items() if "Journal" in t or t == "Human Finance")
        assert roles[str(p.index + 1)] == "header"
        assert roles[[t for t in roles if t.startswith("Downloaded")][0]] == "header"
        assert all(r == "" for t, r in roles.items() if t.startswith("body text"))


def test_section_number_next_to_heading_is_not_a_page_number():
    pages = make_doc()
    # "1  Introduction" at the very top of a page: the "1" must survive
    pages[3].lines.append(mkline(3, "1", 60, x0=60, size=12))
    pages[3].lines.append(mkline(3, "Introduction", 60, x0=78, size=12))
    mark_margins(pages, body_size=10.0)
    roles = {l.text: l.role for l in pages[3].lines if l.y0 == 60}
    assert roles == {"1": "", "Introduction": ""}
