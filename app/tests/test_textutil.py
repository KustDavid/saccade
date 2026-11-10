from collections import Counter

from backend.textutil import (
    build_vocab,
    find_ref_labels,
    find_refs,
    join_lines,
    make_paper_id,
    norm_label_number,
    normalize_text,
    roman_to_int,
    should_drop_hyphen,
    slugify,
    surname,
)

INDEX = {
    ("figure", "1"): "fig-1",
    ("figure", "2"): "fig-2",
    ("figure", "3"): "fig-3",
    ("figure", "4"): "fig-4",
    ("figure", "E2"): "fig-e-2",
    ("table", "1"): "tab-1",
    ("table", "2"): "tab-2",
    ("table", "A1"): "tab-a-1",
    ("table", "IV"): "tab-iv",
}


# --- reference detection -------------------------------------------------------


def test_refs_basic_forms():
    assert find_refs("as shown in Figure 1 and Table 2.", INDEX) == ["fig-1", "tab-2"]
    assert find_refs("see Fig. 2 (left)", INDEX) == ["fig-2"]
    assert find_refs("(Fig.3)", INDEX) == ["fig-3"]
    assert find_refs("Tab. 1 lists", INDEX) == ["tab-1"]


def test_refs_lists_and_ranges():
    assert find_refs("Figs. 2 and 3 show", INDEX) == ["fig-2", "fig-3"]
    assert find_refs("Figures 1, 2, and 4", INDEX) == ["fig-1", "fig-2", "fig-4"]
    assert find_refs("Figures 2–4", INDEX) == ["fig-2", "fig-3", "fig-4"]
    assert find_refs("Tables 1 and 2", INDEX) == ["tab-1", "tab-2"]


def test_refs_panels_appendix_and_roman():
    assert find_refs("Panel (a) of Figure 2(b)", INDEX) == ["fig-2"]
    assert find_refs("Figure 3a", INDEX) == ["fig-3"]
    assert find_refs("Appendix Figure E.2 shows", INDEX) == ["fig-e-2"]
    assert find_refs("Table A.1 summarizes", INDEX) == ["tab-a-1"]
    assert find_refs("Table IV shows", INDEX) == ["tab-iv"]
    assert find_refs("Table I shows", INDEX) == ["tab-1"]  # roman in text, arabic numbering in paper


def test_refs_ignore_lowercase_and_unknown():
    # lower-case "figure 2 in Smith (2011)" refers to someone else's paper
    assert find_refs("relative to figure 2 in Malmendier and Nagel", INDEX) == []
    assert find_refs("see Figure 9", INDEX) == []
    assert find_refs("Table In the", INDEX) == []
    assert find_ref_labels("Tabular data") == []


def test_norm_label_number():
    assert norm_label_number("A.1") == "A1"
    assert norm_label_number("2(a)") == "2"
    assert norm_label_number("3b") == "3"
    assert norm_label_number("iv") == "IV"
    assert roman_to_int("XIV") == 14
    assert roman_to_int("IIII") is None


# --- dehyphenation / normalisation ------------------------------------------------


def test_dehyphenate_syllable_break():
    assert join_lines(["the atten-", "tion mechanism"]) == "the attention mechanism"


def test_dehyphenate_keeps_real_compounds_seen_in_document():
    vocab = build_vocab(["we study stock-market participation", "the stock-market crash"])
    assert join_lines(["experienced stock-", "market returns"], vocab) == "experienced stock-market returns"
    assert should_drop_hyphen("decision", "making", Counter({"decision-making": 3})) is False
    assert should_drop_hyphen("infor", "mation", Counter({"information": 2})) is True


def test_dehyphenate_numbers_and_capitals():
    assert join_lines(["from 1960–", "2022 the"]) == "from 1960–2022 the"
    assert join_lines(["non-", "Bayesian agents"]) == "non-Bayesian agents"
    assert join_lines(["a soft­", "hyphen"]) == "a softhyphen"


def test_normalize_text_ligatures_and_accents():
    assert normalize_text("ﬁnancial eﬃcient ﬂow") == "financial efficient flow"
    assert normalize_text("B¨orschlein") == "Börschlein"
    assert normalize_text("a  b​ c") == "a b c"


# --- ids ------------------------------------------------------------------------------


def test_slugify():
    assert slugify("Attention Is All You Need") == "attention-is-all-you-need"
    assert slugify("Ünïcödé & Co.: 2nd ed.") == "unicode-co-2nd-ed"
    s = slugify("Presidential Address: Human Finance: Incorporating Insights from the Life Sciences", 44)
    assert len(s) <= 44 and not s.endswith("-") and s.startswith("presidential-address-human-finance")


def test_make_paper_id():
    sha = "3f9a1c" + "0" * 58
    pid = make_paper_id(["Ashish Vaswani", "Noam Shazeer"], 2017, "Attention Is All You Need", sha)
    assert pid == "vaswani-2017-attention-is-all-you-need-3f9a1c"
    # nothing known: fall back to the file name (spaces, double spaces)
    pid = make_paper_id([], None, None, sha, fallback="The Journal of Finance - 2026 - X  Y.pdf")
    assert pid == "the-journal-of-finance-2026-x-y-3f9a1c"
    assert surname("Malmendier, Ulrike") == "Malmendier"
    assert surname("Martin Luther King Jr.") == "King"
