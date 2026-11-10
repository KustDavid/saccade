"""Locating a sentence in the PDF and writing it as a highlight with a comment."""

from pathlib import Path

import pymupdf
import pytest

from backend import annotate

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
ATTENTION = SAMPLES / "1706.03762.pdf"


def test_letters_ignores_spacing_hyphens_ligatures_and_digits():
    assert annotate.letters("expe-\nriences ﬁnd 3 “Things”.") == "experiencesfindthings"


@pytest.mark.skipif(not ATTENTION.exists(), reason="sample PDF missing")
def test_locate_finds_sentence_and_misses_nonsense():
    doc = pymupdf.open(ATTENTION)
    sentence = ("We propose a new simple network architecture, the Transformer, based solely on "
                "attention mechanisms, dispensing with recurrence and convolutions entirely.")
    rects, coverage = annotate.locate(doc, sentence, 1)
    assert coverage == 1.0
    assert list(rects) == [0] and len(rects[0]) >= 2      # page 1, wraps over several lines
    rects, coverage = annotate.locate(doc, "Zebras quietly juggle seventeen purple accordions on Tuesdays.", 1)
    assert coverage < annotate.MIN_COVERAGE


@pytest.mark.skipif(not ATTENTION.exists(), reason="sample PDF missing")
def test_build_pdf_writes_highlight_with_note(tmp_path, monkeypatch):
    d = tmp_path / "p1"
    d.mkdir()
    (d / "original.pdf").write_bytes(ATTENTION.read_bytes())
    monkeypatch.setattr(annotate.library, "paper_dir", lambda pid: d)
    items = [{"id": "a1", "page": 1, "note": "my note",
              "text": "The dominant sequence transduction models are based on complex recurrent or "
                      "convolutional neural networks that include an encoder and a decoder."}]
    assert annotate.build_pdf("p1", items) == {"a1": True}
    out = pymupdf.open(d / "annotated.pdf")
    notes = [a.info["content"] for a in out[0].annots() if a.type[1] == "Highlight"]
    assert notes == ["my note"]
    assert annotate.build_pdf("p1", []) == {} and not (d / "annotated.pdf").exists()
