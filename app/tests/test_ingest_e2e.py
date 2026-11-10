"""End-to-end conversion of real papers in samples/ (skipped if a sample is missing)."""

import re
from pathlib import Path

import pytest

from backend.ingest import convert

ROOT = Path(__file__).resolve().parent.parent
ATTENTION = ROOT / "samples" / "1706.03762.pdf"
RESNET = ROOT / "samples" / "1512.03385.pdf"
JF = ROOT / "samples" / "jf-malmendier-2026.pdf"


def _text(paper) -> str:
    return "\n".join(b["text"] for b in paper["blocks"] if b["type"] in ("paragraph", "heading"))


@pytest.fixture(scope="module")
def attention(tmp_path_factory):
    if not ATTENTION.exists():
        pytest.skip("samples/1706.03762.pdf not present")
    out = tmp_path_factory.mktemp("attention")
    return convert(ATTENTION, out, ATTENTION.name), out


def test_attention_metadata(attention):
    paper, _ = attention
    assert paper["title"] == "Attention Is All You Need"
    assert paper["authors"][:2] == ["Ashish Vaswani", "Noam Shazeer"]
    assert paper["year"] == 2017
    assert paper["page_count"] == 15


def test_attention_structure(attention):
    paper, out = attention
    titles = [s["title"] for s in paper["sections"]]
    assert "3 Model Architecture" in titles and "3.2.1 Scaled Dot-Product Attention" in titles
    refs = next(s for s in paper["sections"] if s["title"] == "References")
    assert refs["skip_default"] is True
    assert not next(s for s in paper["sections"] if s["title"] == "3 Model Architecture")["skip_default"]
    labels = {a["label"] for a in paper["assets"]}
    assert {"Figure 1", "Figure 2", "Table 1", "Table 2", "Table 3", "Table 4"} <= labels
    for a in paper["assets"]:
        png = out / "assets" / a["image"]
        assert png.exists() and a["width"] > 100 and a["height"] > 50
    fig1 = next(a for a in paper["assets"] if a["id"] == "fig-1")
    assert fig1["caption"].startswith("The Transformer - model architecture")
    assert fig1["page"] == 3
    assert fig1["first_ref_block"] is not None
    assert "fig-1" in paper["blocks"][fig1["first_ref_block"]]["refs"]
    assert any(b["type"] == "equation" for b in paper["blocks"])
    assert any(b["type"] == "asset" and b["asset"] == "tab-3" for b in paper["blocks"])


def test_attention_text_is_clean(attention):
    paper, _ = attention
    text = _text(paper)
    assert "arXiv:1706.03762" not in text                 # margin stamp
    assert "Provided proper attribution" not in text       # front-matter notice
    assert "Figure 1: The Transformer" not in text         # caption lives on the asset
    assert "ﬁ" not in text and "ﬂ" not in text    # ligatures
    assert "ByteNet [18]" in text
    assert re.search(r"\batten- tion\b", text) is None
    # table cells must not leak into paragraphs
    assert "Layer Type Complexity per Layer" not in text


def test_two_column_reading_order():
    if not RESNET.exists():
        pytest.skip("samples/1512.03385.pdf not present")
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        paper = convert(RESNET, Path(td), RESNET.name)
    paras = [b["text"] for b in paper["blocks"] if b["type"] == "paragraph"]
    i_intro = next(i for i, t in enumerate(paras) if t.startswith("Deep convolutional neural networks"))
    i_next = next(i for i, t in enumerate(paras) if t.startswith("Driven by the significance of depth"))
    assert i_intro < i_next
    # the left-column paragraph that continues into the right column is joined, not split
    assert any("vanishing/exploding gradients" in t and "normalized initialization" in t for t in paras)
    labels = {a["label"] for a in paper["assets"]}
    assert {"Figure 1", "Figure 3", "Table 1", "Table 3"} <= labels
    text = "\n".join(paras)
    assert "3x3 conv, 64" not in text  # figure 3 labels


def test_journal_paper_watermark_and_footnotes():
    if not JF.exists():
        pytest.skip("samples/jf-malmendier-2026.pdf not present")
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        paper = convert(JF, Path(td), JF.name)
    text = _text(paper)
    assert paper["title"].startswith("Presidential Address: Human Finance")
    assert paper["authors"] == ["Ulrike Malmendier"]
    assert "onlinelibrary.wiley.com" not in text.lower() or "Downloaded from" not in text
    assert "Downloaded from" not in text
    assert "The Journal of Finance®" not in text
    assert "Correspondence:" not in text          # first-page footnote block
    assert paper["footnotes"], "footnotes should be collected separately"
    assert "7.9 pp. I extend" in text             # footnote marker 8 removed
    labels = {a["label"] for a in paper["assets"]}
    assert {"Figure 1", "Figure 18", "Table I", "Table IV", "Table A.1"} <= labels
    tab_a1 = next(a for a in paper["assets"] if a["label"] == "Table A.1")
    assert len(tab_a1["pages"]) >= 5               # continued table stitched into one image
    skipped = {s["title"] for s in paper["sections"] if s["skip_default"]}
    assert "References" in skipped and any(t.startswith("Appendix A") for t in skipped)
