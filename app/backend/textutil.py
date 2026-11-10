"""Pure text helpers: normalisation, dehyphenation, slugs/ids, figure/table reference detection.

Nothing in here touches PyMuPDF, so it is cheap to import and easy to unit-test.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Iterable, Mapping

# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

LIGATURES = {
    "ﬀ": "ff",
    "ﬁ": "fi",
    "ﬂ": "fl",
    "ﬃ": "ffi",
    "ﬄ": "ffl",
    "ﬅ": "st",
    "ﬆ": "st",
    "Ĳ": "IJ",
    "ĳ": "ij",
    "Œ": "OE",
    "œ": "oe",
}

_SPACE_CHARS = "              　\t\r\n\f\v"
_ZERO_WIDTH = "​‌‍⁠﻿"
HYPHENS = "-‐‑­"

# LaTeX often emits accents as a separate spacing character placed before the letter
# ("B¨orschlein", "Sch¨ olkopf").  Map them onto combining marks.
_SPACING_ACCENTS = {
    "¨": "̈",  # diaeresis
    "´": "́",  # acute
    "`": "̀",       # grave
    "ˆ": "̂",  # circumflex
    "˜": "̃",  # tilde
    "¸": "̧",  # cedilla
    "ˇ": "̌",  # caron
}
_ACCENT_RE = re.compile("([" + "".join(re.escape(k) for k in _SPACING_ACCENTS) + r"])\s?([A-Za-z])")

_TRANSLATE = {ord(k): v for k, v in LIGATURES.items()}
_TRANSLATE.update({ord(c): " " for c in _SPACE_CHARS})
_TRANSLATE.update({ord(c): None for c in _ZERO_WIDTH})
_TRANSLATE[ord("‐")] = "-"
_TRANSLATE[ord("‑")] = "-"
_TRANSLATE[ord("−")] = "-"   # minus sign -> ascii hyphen-minus (reads better in RSVP)
_TRANSLATE[ord("∗")] = "*"   # asterisk operator


def _fix_accents(s: str) -> str:
    if not any(k in s for k in _SPACING_ACCENTS):
        return s
    # only treat the accent as combining when it is glued to a word (letter before it or no space)
    def repl(m: re.Match) -> str:
        return m.group(2) + _SPACING_ACCENTS[m.group(1)]

    out = _ACCENT_RE.sub(repl, s)
    return unicodedata.normalize("NFC", out)


def normalize_text(s: str) -> str:
    """Ligatures, odd spaces, zero-width chars, LaTeX accents; collapse whitespace."""
    if not s:
        return ""
    s = s.translate(_TRANSLATE)
    s = _fix_accents(s)
    s = s.replace("­", "")  # soft hyphens inside a line are invisible
    return re.sub(r" {2,}", " ", s).strip()


def collapse_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Dehyphenation / line joining
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-zÀ-ɏ]+(?:[-'’][A-Za-zÀ-ɏ]+)*")


def build_vocab(texts: Iterable[str]) -> Counter:
    """Lower-cased word counts (hyphenated compounds kept as one token).

    Words that end a line with a hyphen are skipped, so the vocabulary reflects how words are
    spelled mid-line, which is what the dehyphenation decision needs.
    """
    vocab: Counter = Counter()
    for t in texts:
        t = t.rstrip()
        if t and t[-1] in HYPHENS:
            t = t.rsplit(" ", 1)[0] if " " in t else ""
        for w in _WORD_RE.findall(t):
            vocab[w.lower().replace("’", "'")] += 1
    return vocab


def should_drop_hyphen(left: str, right: str, vocab: Mapping[str, int] | None = None) -> bool:
    """Decide whether a line-final hyphen between ``left-`` and ``right`` is a syllable break.

    ``left`` is the fragment before the hyphen (without it), ``right`` the first word of the next line.
    Returns True to join as one word ("atten" + "tion" -> "attention"), False to keep the hyphen
    ("stock" + "market" -> "stock-market").
    """
    lm = re.search(r"([A-Za-zÀ-ɏ]+)$", left)
    rm = re.match(r"([A-Za-zÀ-ɏ]+)", right)
    if not lm or not rm:
        return False  # "1960-" + "2022", "x-" + "(", ...
    lw, rw = lm.group(1), rm.group(1)
    if rw[0].isupper():
        return False  # "non-" + "Bayesian", "Anglo-" + "Saxon"
    if vocab:
        joined = (lw + rw).lower()
        hyph = f"{lw}-{rw}".lower()
        jc = vocab.get(joined, 0)
        hc = vocab.get(hyph, 0)
        if hc > jc:
            return False
        if jc > 0:
            return True
    return True


def join_lines(lines: Iterable[str], vocab: Mapping[str, int] | None = None) -> str:
    """Join the text lines of one paragraph, repairing hyphenated line breaks."""
    out = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not out:
            out = line
            continue
        last = out[-1]
        if last == "­":
            out = out[:-1] + line
        elif last in HYPHENS and len(out) >= 2 and out[-2].isalpha():
            left = out[:-1].rsplit(" ", 1)[-1]
            if should_drop_hyphen(left, line, vocab):
                out = out[:-1] + line
            else:
                out = out[:-1] + "-" + line
        elif last in "–—/" and len(out) >= 2 and out[-2].isalnum() and line[:1].isalnum():
            out = out + line  # "1960–" + "2022", "and/" + "or"
        else:
            out = out + " " + line
    return collapse_ws(out)


# ---------------------------------------------------------------------------
# Slugs and ids
# ---------------------------------------------------------------------------


def ascii_fold(s: str) -> str:
    s = normalize_text(s)
    s = unicodedata.normalize("NFKD", s)
    return s.encode("ascii", "ignore").decode("ascii")


def slugify(text: str, max_len: int = 48) -> str:
    s = ascii_fold(text).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if len(s) <= max_len:
        return s
    cut = s[:max_len]
    if "-" in cut and s[max_len] != "-":
        cut = cut.rsplit("-", 1)[0]
    return cut.strip("-")


_NAME_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}


def surname(name: str) -> str:
    name = collapse_ws(name)
    if not name:
        return ""
    if "," in name:
        return name.split(",", 1)[0].strip()
    toks = [t for t in name.split(" ") if t.lower().strip(",") not in _NAME_SUFFIXES]
    return toks[-1] if toks else name


def make_paper_id(
    authors: list[str] | None,
    year: int | None,
    title: str | None,
    sha256: str,
    fallback: str = "paper",
) -> str:
    """``vaswani-2017-attention-is-all-you-need-3f9a1c``: slug + 6 hex chars of the file hash."""
    parts: list[str] = []
    if authors:
        last = slugify(surname(authors[0]), 24)
        if last:
            parts.append(last)
    if year:
        parts.append(str(year))
    t = slugify(title or "", 44)
    if not t:
        t = slugify(re.sub(r"\.pdf$", "", fallback, flags=re.I), 44)
    if t:
        parts.append(t)
    parts.append(sha256[:6].lower())
    return "-".join(parts) if len(parts) > 1 else f"paper-{sha256[:6].lower()}"


# ---------------------------------------------------------------------------
# Roman numerals / label numbers
# ---------------------------------------------------------------------------

_ROMAN_RE = re.compile(r"^M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")
_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def roman_to_int(s: str) -> int | None:
    s = s.strip().upper()
    if not s or not _ROMAN_RE.match(s):
        return None
    total = 0
    prev = 0
    for ch in reversed(s):
        v = _ROMAN_VALUES[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total


def norm_label_number(num: str) -> str:
    """Canonical key for a figure/table number: 'A.1' == 'A1', '2(a)' -> '2', 'iv' -> 'IV'."""
    n = num.strip().rstrip(".:")
    n = re.sub(r"\s*\([a-z]\)$", "", n)
    n = re.sub(r"(?<=\d)[a-z]$", "", n)
    n = n.replace(" ", "").upper()
    n = re.sub(r"(?<=[A-Z])\.(?=\d)", "", n)
    return n


def number_slug(num: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", num.lower()).strip("-") or "x"


# ---------------------------------------------------------------------------
# Figure / table reference detection
# ---------------------------------------------------------------------------

# one label number: 3, 3a, 3(b), 2.1, A.1, A1, E.10, IV
_NUM = r"(?:[A-Z]\.?\d+(?:\.\d+)?|\d+(?:\.\d+)?|[IVXL]+(?![A-Za-z0-9]))(?:[a-z](?![A-Za-z])|\s?\([a-z]\))?"
_SEP = r"\s*(?:,\s*and|,\s*or|,|and|or|&|–|—|-|to|through)\s*"
REF_RE = re.compile(
    r"(?<![A-Za-z])(?P<kind>Figures|Figure|Figs\.?|Fig\.?|FIGURES|FIGURE|FIGS?\.|Tables|Table|Tabs?\.|TABLES|TABLE|TAB\.)"
    r"\s*(?P<nums>" + _NUM + r"(?:" + _SEP + _NUM + r")*)"
)
_NUM_RE = re.compile(_NUM)


def ref_kind(word: str) -> str:
    return "figure" if word.lower().startswith("fig") else "table"


def _expand_numbers(nums_text: str) -> list[str]:
    tokens = [m.group(0) for m in _NUM_RE.finditer(nums_text)]
    # ranges like "2-4" / "2–4" / "2 to 4"
    rng = re.match(r"^\s*(\d+)\s*(?:–|-|to|through)\s*(\d+)\s*$", nums_text)
    if rng:
        a, b = int(rng.group(1)), int(rng.group(2))
        if a < b <= a + 20:
            return [str(i) for i in range(a, b + 1)]
    return tokens


def find_ref_labels(text: str) -> list[tuple[str, str]]:
    """All (kind, normalised number) mentions in ``text``, in order of appearance."""
    found: list[tuple[str, str]] = []
    for m in REF_RE.finditer(text):
        kind = ref_kind(m.group("kind"))
        for num in _expand_numbers(m.group("nums")):
            key = norm_label_number(num)
            if key and (kind, key) not in found:
                found.append((kind, key))
    return found


def find_refs(text: str, label_index: Mapping[tuple[str, str], str]) -> list[str]:
    """Asset ids mentioned in ``text``. ``label_index`` maps (kind, normalised number) -> asset id."""
    ids: list[str] = []
    for kind, key in find_ref_labels(text):
        aid = label_index.get((kind, key))
        if aid is None and key.isdigit():
            # "Table 1" in the text but the paper numbers tables with roman numerals (or vice versa)
            for (k2, n2), cand in label_index.items():
                if k2 == kind and roman_to_int(n2) == int(key):
                    aid = cand
                    break
        if aid is None:
            r = roman_to_int(key)
            if r is not None:
                aid = label_index.get((kind, str(r)))
        if aid and aid not in ids:
            ids.append(aid)
    return ids


# ---------------------------------------------------------------------------
# Running headers / footers
# ---------------------------------------------------------------------------

_PAGE_NUM_RE = re.compile(
    r"^(?:page\s*)?(?:\d{1,4}|[ivxlc]{1,7})(?:\s*(?:of|/)\s*\d{1,4})?$|^[-–—]\s*\d{1,4}\s*[-–—]$",
    re.I,
)


def is_page_number(text: str) -> bool:
    return bool(_PAGE_NUM_RE.match(text.strip()))


def header_key(text: str) -> str:
    """Key under which running headers/footers repeat: digits wildcarded, case/space-insensitive."""
    t = normalize_text(text).lower()
    t = re.sub(r"\d+", "#", t)
    t = re.sub(r"[^\w#]+", " ", t)
    return collapse_ws(t)


def repeated_margin_keys(page_keys: list[set[str]], min_pages: int | None = None) -> set[str]:
    """Keys that occur in the header/footer zone of many pages.

    ``page_keys`` holds, for each page, the set of keys of lines found in that page's margin zones.
    """
    n = len(page_keys)
    if n < 3:
        return set()
    if min_pages is None:
        min_pages = max(3, int(round(n * 0.2)))
    counts: Counter = Counter()
    for keys in page_keys:
        counts.update(keys)
    return {k for k, c in counts.items() if c >= min_pages and k}
