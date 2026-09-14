#!/usr/bin/env python3
"""Raw paragraph extractor for the DETERMINISTIC PDF path (Track A, pre-1994).

The Word path (`fulltext_extract_raw.py`) turns archived `.docx` into
`document_paragraphs_raw`. This is its PDF twin: it turns archived `.pdf` files
(pre-1994 documents that have no Word source on ODS) into the SAME raw-row
contract, so the FROZEN semantic parser (`fulltext_parse.py`, sem-v2) can consume
them through its style-less lexical path with no changes.

NO LLM anywhere. Everything here is deterministic geometry + lexical patterns.

The pre-1994 PDFs are of three kinds:
  * born-digital modern PDFs (~1990-1993): a clean embedded text layer, one
    resolution per file, a UN masthead front;
  * scanned compilation-volume EXCERPTS (older): an OCR text layer of variable
    quality, laid out as pages of a "Resolutions adopted ..." supplement — so a
    file's page typically shows the END of the previous resolution, the target
    resolution, and the START of the next one, under a running page header;
  * pure image scans: NO text layer at all — unrecoverable without OCR, excluded.

Pipeline per document (all deterministic):
  1. TRIAGE the text layer -> class 'text' | 'poor' | 'none'. 'none' (pure scan)
     is recorded status='no_text_layer' and skipped. The triage score rides along
     on every emitted row's props (textlayer_score) and in the ledger error field.
  2. EXTRACT spans -> lines (pymupdf), DROP running headers/footers/page numbers
     (position + repetition + pattern), analyse the page LAYOUT with a recursive
     X-Y cut (columns separated by a whitespace gutter, bands separated by
     full-width lines) so every page is read in printed reading order, then
     reconstruct PARAGRAPHS inside each region by left-edge indent + vertical
     gaps + terminal punctuation, resolving end-of-line hyphenation from corpus
     and same-document evidence.

     Text is NEVER merged across a column boundary: doing so is what produced
     sentences that exist in no document ("Convinced that all peoples have an
     inalienable right any distinction as to race, creed or colour, in order to
     to complete freedom ...", A/RES/1514(XV)). Where the geometry is genuinely
     ambiguous the extractor fragments and flags rather than inventing an order.
  3. CROP to the target resolution inside the excerpt (its own number heading ..
     its adoption record / the next resolution's heading). Never silently
     truncate: if the crop anchor is uncertain, keep everything and flag it.
  4. EMIT `document_paragraphs_raw` rows matching the docx contract
     (kind='paragraph'/'empty'; props carry size/bold/italic/indent/all_caps/
     alignment/lead_italic_text plus pdf=true and textlayer_score;
     extractor_version='pdf-v1'), advancing the ledger 'fetched' -> 'extracted'.

Then run the existing parser over these docs (uv run python python/fulltext_parse.py)
exactly as for Word docs — it is format-agnostic in its lexical patterns.

Usage:
    uv run python python/fulltext_extract_pdf.py
    uv run python python/fulltext_extract_pdf.py --limit 20
    uv run python python/fulltext_extract_pdf.py --symbols A/RES/1260(XIII),S/RES/338(1973)
    uv run python python/fulltext_extract_pdf.py --force   # re-extract 'extracted'/'no_text_layer'
    uv run python python/fulltext_extract_pdf.py --symbols ... --debug   # dump crop + rows, no DB write
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from pathlib import Path

import fitz  # pymupdf
from psycopg.types.json import Jsonb

from fulltext_common import (ARCHIVE_ROOT, get_conn, sanitize_symbol,
                             upsert_document_file)

# pdf-v2 (2026-07-28): layout-aware reading order (recursive X-Y cut), evidence-
# based end-of-line hyphenation, region-level facing-language detection. Rows
# written by pdf-v1 are NOT comparable: v1 read two-column pages by visual row
# and wove the columns together.
EXTRACTOR_VERSION = "pdf-v2"
BATCH_DOCS = 20

# pymupdf span flag bits.
FLAG_ITALIC = 1 << 1   # 2
FLAG_BOLD = 1 << 4     # 16

# ---------------------------------------------------------------------------
# Triage vocabulary — a small built-in common-word list. Deliberately tiny and
# stdlib-only: enough to tell real English OCR from garbage, not a spell checker.
# ---------------------------------------------------------------------------
COMMON_WORDS = frozenset("""
the of to and a in that is was he for it with as his on be at by i this had not
are but from or have an they which one you were her all she there would their we
him been has when who will more no if out so up said what its about into than them
can only other new some could time these two may then do first any my now such like
our over man me even most made after also did many before must through back years
where much your way well down should because each just those people mr how too little
state good very make world still see men work long get here between both life being
under general assembly security council economic social nations united resolution
resolutions decides requests recalling reaffirming noting recognizing welcoming
considering having report committee member states international peace secretary
adopted meeting plenary session document decision decisions organization
government development rights human question situation present agenda organizations
information programme co-operation cooperation
""".split())


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z]{2,}", text)


# ---------------------------------------------------------------------------
# Facing-language detection — REGION level, never line level
# ---------------------------------------------------------------------------
# The old GA/ECOSOC supplement volumes print an English column facing a French
# one on the same page, and the trilingual Official Records sheets add Spanish.
# That foreign text must not enter an English record.
#
# It used to be decided one LINE at a time: >=3 French function words meant the
# line was French. Applied unconditionally, that deleted 1,003 English lines
# from 39 monolingual documents — every one an official NGO name in French
# ('Association pour le Déploiement Rural, la Protection de …'), with its
# English continuation left orphaned. The predicate was right about French; it
# was wrong about what a line is evidence FOR.
#
# A facing language occupies a whole COLUMN. So the unit of decision is the
# layout region, and a region is only dropped when the document also holds
# English regions — a document with no English at all is kept whole and flagged,
# because deleting a document is not a language decision.

FRENCH_STOPWORDS = frozenset("""
le la les des du et aux une dans par qui que pour avec sur ses leur leurs cette
ces entre ainsi dont sont elle ils nous vous tous comme sans sous deux cet celle
ceux votre notre seance pleniere economique institutions specialisees
renseignements secretaire egalement competentes territoires autonomes assemblee
generale conseil comite novembre decembre janvier fevrier avril juin juillet
septembre octobre adoptee mondiale examine informer presenter maintenir
etant apres avoir etre fait ete cas afin lors ainsi meme tout toute
""".split())

SPANISH_STOPWORDS = frozenset("""
el los las del y en que por para con su sus como este esta estos entre sobre sin
al se es ha han sido sera cuando donde asamblea consejo economico general
resolucion informe secretario naciones unidas seguridad periodo sesiones
aprobada plenaria septiembre octubre noviembre diciembre enero febrero
""".split())

ENGLISH_STOPWORDS = frozenset("""
the of to and in that is was for it with as on be at by this which shall its
all from or has have their been are not but were they we he she there would
should may must any such other more than then when where who whom while
general assembly security council economic social nations united resolution
""".split())

_FR_TOKEN = re.compile(r"[a-zà-ÿ']+")

# A region is foreign when the foreign function words clearly beat the English
# ones. Thresholds are deliberately coarse: a mixed region stays English, since
# keeping foreign text is a blemish while deleting English text is a loss.
# Calibrated on a census of 935 regions from 200 random PDFs plus the four
# volumes whose English NGO-name lists the old per-line filter deleted:
#   genuine French regions  fr rate 0.175-0.336, fr/en ratio 6.1 - inf
#   English NGO-name lists  fr rate 0.076-0.186, fr/en ratio 2.1 - 2.6
# The RATE alone does not separate them; the RATIO does, with a 2.4x margin.
_LANG_MIN_TOKENS = 25
_LANG_MIN_LINES = 3
_LANG_MIN_HITS = 8
_LANG_MIN_RATE = 0.12
_LANG_DOMINANCE = 4.0


def french_line(text: str) -> bool:
    """True for a line that is clearly French facing-language content.

    Kept for callers that score a single line (e.g. the PDF verify gate's ground
    truth). It is NOT used to delete body text any more — see
    `classify_region_language`."""
    toks = _FR_TOKEN.findall(text.lower())
    if len(toks) < 4:
        return False
    hits = sum(1 for t in toks if t in FRENCH_STOPWORDS)
    return hits >= 3


def classify_region_language(lines: list[Line]) -> tuple[str, dict[str, int]]:
    """Return ('en'|'fr'|'es'|'unknown', hit counts) for a layout region."""
    toks: list[str] = []
    for l in lines:
        toks.extend(_FR_TOKEN.findall((l.text or "").lower()))
    counts = {
        "en": sum(1 for t in toks if t in ENGLISH_STOPWORDS),
        "fr": sum(1 for t in toks if t in FRENCH_STOPWORDS),
        "es": sum(1 for t in toks if t in SPANISH_STOPWORDS),
        "tokens": len(toks),
    }
    if len(toks) < _LANG_MIN_TOKENS or len(lines) < _LANG_MIN_LINES:
        return "unknown", counts
    foreign = max(("fr", counts["fr"]), ("es", counts["es"]), key=lambda kv: kv[1])
    if (foreign[1] >= _LANG_MIN_HITS
            and foreign[1] >= _LANG_DOMINANCE * counts["en"]
            and foreign[1] / len(toks) >= _LANG_MIN_RATE):
        return foreign[0], counts
    if counts["en"] >= 3:
        return "en", counts
    return "unknown", counts


def drop_foreign_regions(groups: list[list[Line]], flags: set[str],
                         has_english: bool | None = None,
                         ) -> tuple[list[list[Line]], list[str]]:
    """Remove whole facing-language regions. Returns (kept groups, dropped text)."""
    if not groups:
        return groups, []
    langs = [classify_region_language(g)[0] for g in groups]
    if has_english is None:
        has_english = any(l == "en" for l in langs)
    if not has_english:
        if any(l in ("fr", "es") for l in langs):
            flags.add("no_english_region_kept_whole")
        return groups, []
    kept: list[list[Line]] = []
    dropped: list[str] = []
    for g, lang in zip(groups, langs):
        if lang in ("fr", "es"):
            flags.add(f"dropped_{lang}_region")
            dropped.extend((l.text or "") for l in g)
            continue
        kept.append(g)
    return kept, dropped


# ---------------------------------------------------------------------------
# End-of-line hyphenation — decided on evidence, never on a rule of thumb
# ---------------------------------------------------------------------------
# A hyphen at a line end is ambiguous: 'self-' + 'determination' is a compound
# whose hyphen must SURVIVE, 'avoid-' + 'ing' is a typesetter's break whose
# hyphen must GO. Deleting it unconditionally (what this file used to do) put
# 'selfdetermination' into 131 rows of 109 documents and destroyed 8.12% of all
# hyphenated compounds — legal terms of art, silently rewritten.
#
# The evidence is how the SAME pages spell the compound when it happens to fall
# inside a line, where no join can have occurred. Three tiers, most specific
# first:
#   1. THIS document's own within-line spellings (house style is consistent);
#   2. a pair lexicon counted from within-line tokens of pre-1994 PDFs
#      (`data/hyphen_pairs.txt`, era-matched: these volumes write 'co-operation'
#      and 'peace-keeping' where a 2015 corpus writes neither);
#   3. a small embedded core of prefixes that the UN always hyphenates.
# With no evidence at all the hyphen is dropped, which is right for ~92% of
# breaks — but the decision is counted, so the residual is reportable.

HYPHEN_LEXICON_PATH = Path(__file__).resolve().parent / "data" / "hyphen_pairs.txt"

# Prefixes the UN hyphenates as a matter of drafting style, used only when the
# corpus has nothing to say about the pair. Kept deliberately short: every entry
# is a claim, and a wrong entry damages text just as a missing one does.
CORE_HYPHEN_PREFIXES = frozenset("""
self non ex quasi pseudo socio vice anti neo
""".split())

_MIN_DOC_EVIDENCE = 1     # one within-line spelling in the same document decides
_MIN_PAIR_EVIDENCE = 5    # …else at least this many corpus observations
_TRAIL_WORD_RE = re.compile(r"([A-Za-z]+(?:-[A-Za-z]+)*)-$")
_LEAD_WORD_RE = re.compile(r"^([A-Za-z]+)")
_INLINE_HYPH_RE = re.compile(r"[A-Za-z]{2,}(?:-[A-Za-z]{2,})+")
_INLINE_PLAIN_RE = re.compile(r"[A-Za-z]{4,}")


def load_pair_lexicon(path: Path = HYPHEN_LEXICON_PATH) -> dict[tuple[str, str], tuple[int, int]]:
    """Load '<left> <right> <hyphenated> <joined>' counts. Missing file is loud."""
    out: dict[tuple[str, str], tuple[int, int]] = {}
    try:
        text = path.read_text()
    except OSError:
        import sys as _sys
        print(f"WARNING: hyphen pair lexicon not found at {path}; falling back to "
              f"prefix rules only (hyphenated compounds may be damaged)",
              file=_sys.stderr)
        return out
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 4:
            continue
        out[(parts[0], parts[1])] = (int(parts[2]), int(parts[3]))
    return out


class HyphenPolicy:
    """Decides, per line-break hyphen, whether the hyphen is part of the word."""

    def __init__(self, pairs: dict[tuple[str, str], tuple[int, int]] | None = None):
        self.pairs = load_pair_lexicon() if pairs is None else pairs
        self.doc_h: dict[tuple[str, str], int] = {}
        self.doc_j: dict[str, int] = {}
        self.decisions: dict[str, int] = {"doc": 0, "corpus": 0, "prefix": 0, "default": 0}
        self.kept = 0
        self.dropped = 0

    def observe(self, lines: list[Line]) -> None:
        """Record this document's WITHIN-LINE spellings (never a broken word)."""
        for ln in lines:
            t = (ln.text or "").strip()
            if t.endswith("-"):
                t = t[:t.rfind(" ")] if " " in t else ""
            for m in _INLINE_HYPH_RE.finditer(t):
                segs = m.group(0).lower().split("-")
                for a, b in zip(segs, segs[1:]):
                    self.doc_h[(a, b)] = self.doc_h.get((a, b), 0) + 1
            for m in _INLINE_PLAIN_RE.finditer(t):
                w = m.group(0).lower()
                self.doc_j[w] = self.doc_j.get(w, 0) + 1

    def keep_hyphen(self, left_text: str, right_text: str) -> bool:
        m = _TRAIL_WORD_RE.search(left_text.rstrip())
        n = _LEAD_WORD_RE.match(right_text.lstrip())
        if not m or not n:
            self.dropped += 1
            self.decisions["default"] += 1
            return False
        left = m.group(1).lower().split("-")[-1]
        right = n.group(1).lower()
        keep, why = self._decide(left, right)
        self.decisions[why] += 1
        if keep:
            self.kept += 1
        else:
            self.dropped += 1
        return keep

    def _decide(self, left: str, right: str) -> tuple[bool, str]:
        if len(left) < 2 or len(right) < 2:
            return False, "default"
        h = self.doc_h.get((left, right), 0)
        j = self.doc_j.get(left + right, 0)
        if h + j >= _MIN_DOC_EVIDENCE:
            return h > j, "doc"
        h, j = self.pairs.get((left, right), (0, 0))
        if h + j >= _MIN_PAIR_EVIDENCE:
            return h > j, "corpus"
        if left in CORE_HYPHEN_PREFIXES:
            return True, "prefix"
        return False, "default"


_DEFAULT_HYPHEN: HyphenPolicy | None = None


def _default_hyphen() -> HyphenPolicy:
    """Lazy module-level policy for helpers called without a document."""
    global _DEFAULT_HYPHEN
    if _DEFAULT_HYPHEN is None:
        _DEFAULT_HYPHEN = HyphenPolicy()
    return _DEFAULT_HYPHEN


@dataclass
class Triage:
    chars_per_page: float
    alnum_ratio: float
    dict_hit_rate: float
    garbage_ratio: float
    klass: str  # 'text' | 'poor' | 'none'

    def as_props(self) -> dict:
        return {
            "class": self.klass,
            "chars_per_page": round(self.chars_per_page, 1),
            "alnum_ratio": round(self.alnum_ratio, 3),
            "dict_hit_rate": round(self.dict_hit_rate, 3),
            "garbage_ratio": round(self.garbage_ratio, 3),
        }

    def summary(self) -> str:
        return (f"class={self.klass} cpp={self.chars_per_page:.0f} "
                f"alnum={self.alnum_ratio:.2f} dict={self.dict_hit_rate:.2f} "
                f"garbage={self.garbage_ratio:.2f}")


def triage_text(pages_text: list[str]) -> Triage:
    """Score the embedded text layer and classify it.

    'none' — essentially no text (pure image scan): skip.
    'text' — clean enough to trust (born-digital or good OCR).
    'poor' — marginal OCR: extract anyway, but flag so the acceptance gate can
             hold it to a lower bar.
    """
    n_pages = max(len(pages_text), 1)
    full = "\n".join(pages_text)
    n_chars = len(full)
    chars_per_page = n_chars / n_pages

    letters = sum(c.isalpha() for c in full)
    alnum = sum(c.isalnum() or c.isspace() for c in full)
    alnum_ratio = alnum / n_chars if n_chars else 0.0

    toks = _tokens(full)
    n_tok = len(toks)
    if n_tok:
        hits = sum(1 for t in toks if t.lower() in COMMON_WORDS)
        dict_hit_rate = hits / n_tok
        # "garbage" tokens: contain no vowel, or mix letters with stray marks, or
        # have improbable long consonant/again runs — OCR debris like 'HE~(l"Tl'.
        garbage = 0
        for t in toks:
            tl = t.lower()
            if not re.search(r"[aeiouy]", tl):
                garbage += 1
            elif re.search(r"[^a-z]", tl):
                garbage += 1
            elif re.search(r"[bcdfghjklmnpqrstvwxz]{5,}", tl):
                garbage += 1
        garbage_ratio = garbage / n_tok
    else:
        dict_hit_rate = 0.0
        garbage_ratio = 1.0

    # Classification thresholds (calibrated on the stratified sample).
    if chars_per_page < 80 or n_tok < 20:
        klass = "none"
    elif dict_hit_rate >= 0.30 and alnum_ratio >= 0.80 and garbage_ratio <= 0.30:
        klass = "text"
    else:
        klass = "poor"
    return Triage(chars_per_page, alnum_ratio, dict_hit_rate, garbage_ratio, klass)


# ---------------------------------------------------------------------------
# Line model
# ---------------------------------------------------------------------------

@dataclass
class Line:
    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    size: float
    bold: bool
    italic: bool
    lead_italic_text: str | None
    page: int
    cleft: float = 0.0   # left edge of this line's column (set after column split)
    cright: float = 0.0  # right edge of this line's column
    soft_divider: float = 0.0  # x of a column boundary that could not be cut cleanly


def _span_style(span: dict) -> tuple[bool, bool]:
    font = (span.get("font") or "").lower()
    flags = span.get("flags", 0)
    bold = bool(flags & FLAG_BOLD) or "bold" in font or "black" in font
    italic = bool(flags & FLAG_ITALIC) or "italic" in font or "oblique" in font
    return bold, italic


def extract_lines(page: fitz.Page, page_no: int) -> list[Line]:
    """One Line per pymupdf text line, with geometry + majority font style."""
    d = page.get_text("dict")
    out: list[Line] = []
    for block in d.get("blocks", []):
        if "lines" not in block:
            continue  # image block
        for ln in block["lines"]:
            spans = [s for s in ln["spans"] if (s.get("text") or "").strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in ln["spans"])
            if not text.strip():
                continue
            xs0 = [s["bbox"][0] for s in spans]
            xs1 = [s["bbox"][2] for s in spans]
            ys0 = [s["bbox"][1] for s in spans]
            ys1 = [s["bbox"][3] for s in spans]
            # weighted-majority size / style by span text length
            size_weight: dict[float, int] = {}
            bold_w = italic_w = total_w = 0
            for s in spans:
                w = len(s["text"].strip()) or 1
                sz = round(s["size"] * 2) / 2
                size_weight[sz] = size_weight.get(sz, 0) + w
                b, it = _span_style(s)
                bold_w += w if b else 0
                italic_w += w if it else 0
                total_w += w
            size = max(size_weight, key=size_weight.get)
            bold = total_w and bold_w >= 0.6 * total_w
            italic = total_w and italic_w >= 0.6 * total_w
            # leading italic run text (preambular-verb signal; rare on old scans)
            lead_it = None
            if not italic:
                buf: list[str] = []
                for s in spans:
                    _, it = _span_style(s)
                    if it:
                        buf.append(s["text"])
                    elif s["text"].strip():
                        break
                lead = "".join(buf).strip()
                if lead:
                    lead_it = lead
            out.append(Line(
                text=text.strip(), x0=min(xs0), x1=max(xs1),
                y0=min(ys0), y1=max(ys1), size=size, bold=bool(bold),
                italic=bool(italic), lead_italic_text=lead_it, page=page_no))
    return out


# ---------------------------------------------------------------------------
# Header / footer / page-artifact detection
# ---------------------------------------------------------------------------

_PAGE_NUM = re.compile(r"^[\[\(]?\s*-?\s*\d{1,4}\s*-?\s*[\]\)]?\.?$")
_RULE = re.compile(r"^[\-_—–.\s·]{4,}$|^/\s*\.{2,}$")   # rules + "/..." continued marker
_DOC_SYMBOL = re.compile(r"^[\[\(]?[A-Z]{1,4}/[A-Z0-9/().,\-\[\]]+$")
_PAGE_LABEL = re.compile(r"^Page\s+\d+$", re.I)
# Running header of a compilation volume ("General Assembly—Thirteenth Session",
# "Resolutions adopted on the reports of the First Committee").
_RUN_HEADER = re.compile(
    r"(General Assembly|Security Council|Economic and Social Council|"
    r"Trusteeship Council)\b.*\bsession\b"
    r"|^\s*(?:[IVXLC]+\.?\s+)?Resolutions?\s+(adopted|and Decisions)\b"
    r"|^\s*Resolution[s]?\W+adopted\b", re.I)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\d+", "", text)).strip().lower()


def _is_static_artifact(text: str) -> bool:
    t = text.strip()
    return bool(_PAGE_NUM.match(t) or _RULE.match(t) or _DOC_SYMBOL.match(t)
                or _PAGE_LABEL.match(t) or _RUN_HEADER.search(t))


def drop_headers_footers(lines: list[Line], page_heights: dict[int, float]) -> tuple[list[Line], list[str]]:
    """Remove running headers/footers, page numbers and separator rules.

    Two signals combined: BAND (a line near the very top/bottom of its page) and
    either (a) a static artifact pattern (page number / doc symbol / rule /
    compilation running-header), or (b) REPETITION — the same de-digited text in
    the same band on >=2 pages. Body lines are never touched here (the parser owns
    body classification)."""
    top_band: dict[str, int] = {}
    bot_band: dict[str, int] = {}
    band: dict[int, str] = {}
    for idx, ln in enumerate(lines):
        H = page_heights.get(ln.page, 800.0)
        if ln.y0 < H * 0.11:
            band[idx] = "top"
            top_band[_norm(ln.text)] = top_band.get(_norm(ln.text), 0) + 1
        elif ln.y1 > H * 0.93:
            band[idx] = "bot"
            bot_band[_norm(ln.text)] = bot_band.get(_norm(ln.text), 0) + 1

    kept: list[Line] = []
    dropped: list[str] = []
    for idx, ln in enumerate(lines):
        b = band.get(idx)
        if b is not None:
            n = _norm(ln.text)
            repeated = (top_band if b == "top" else bot_band).get(n, 0) >= 2 and len(n) > 3
            if _is_static_artifact(ln.text) or repeated:
                dropped.append(f"[{b}] {ln.text[:60]}")
                continue
        kept.append(ln)
    return kept, dropped


# ---------------------------------------------------------------------------
# LAYOUT ANALYSIS — recursive X-Y cut (columns and bands)
# ---------------------------------------------------------------------------
# The Official Records supplements print two (occasionally three) columns, often
# under a full-width heading and above a full-width table, signature block or
# annex. Reading such a page by visual ROW — which is what a y-then-x sort does —
# interleaves the columns into sentences that exist in no document
# ("Convinced that all peoples have an inalienable right any distinction as to
# race, creed or colour, in order to to complete freedom, …", A/RES/1514(XV)).
# That is fabrication, and it is the single worst thing this file can do.
#
# The replacement is a recursive X-Y cut:
#
#   1. Look for a VERTICAL GUTTER: an x-strip that no line's box enters, wide
#      enough not to be a word space, with a genuine text column on each side
#      (enough lines, and lines that FILL their column — which is what tells a
#      newspaper column apart from a table's cells or a hanging-marker gutter).
#   2. Lines that STRADDLE the gutter (a full-width heading, a rule, a wide
#      table row) cut the region into horizontal BANDS instead; each band is
#      re-analysed on its own, and the straddling line is read in its place.
#   3. Recurse, so a 3-column page splits twice and a header-over-two-columns
#      page splits by band and then by column.
#
# The result is a list of line groups in READING ORDER. Text is never merged
# across a column boundary: `reconstruct_paragraphs` starts a new paragraph at
# every group boundary unless the previous group ends mid-sentence in a line
# that fills its column (the newspaper continuation rule).
#
# When the geometry is ambiguous — a gutter exists but the straddles are
# scattered through the body, so neither reading is safe — the region is left
# unsplit AND a flag is raised, because a flagged gap is a defect and a silent
# weave is a falsehood.

MIN_GUTTER_PT = 5.0        # narrower than this is a word space, not a gutter.
                           # The 1940s supplements set columns 6pt apart, so this
                           # cannot be raised; what keeps word gaps from passing
                           # is that a real gutter is empty for the whole column
                           # height (the straddle count) and has a text column on
                           # each side.
MAX_GUTTER_FRAC = 0.30     # wider than this (of the region) is not a gutter
MIN_COL_LINES = 4          # a column with fewer lines is not a column
MIN_COL_SHARE = 0.15       # …nor is one holding <15% of the region's lines
MIN_COL_WIDTH_FRAC = 0.18  # …nor one narrower than 18% of the region
MAX_CROSS_SHARE = 0.45     # more straddles than this: not a columnar region
MIN_FILL_RATIO = 0.25      # share of a column's lines that must fill it
MAX_CUT_DEPTH = 5          # guards runaway recursion (bands nest inside bands)
MAX_Y_CUT_DEPTH = 1        # horizontal cuts are tried only near the top level


def _row_sort(lines: list[Line]) -> list[Line]:
    """Order lines top-to-bottom, grouping each visual ROW left-to-right.

    Two boxes are on the same row when they overlap vertically by more than half
    a line height. A fixed 4pt band (what this used to do) misplaced a hanging
    marker whose OCR box starts 3pt below its own text, producing
    'the Elimi- (a) nation of Discrimination' — the marker welded into the
    middle of the word it labels."""
    order = sorted(lines, key=lambda l: (l.y0, l.x0))
    rows: list[list[Line]] = []
    for l in order:
        if rows:
            row = rows[-1]
            bot = min(x.y1 for x in row)
            top = max(x.y0 for x in row)
            overlap = min(l.y1, bot) - max(l.y0, top)
            if overlap > 0.5 * max(l.y1 - l.y0, 4.0):
                row.append(l)
                continue
        rows.append([l])
    out: list[Line] = []
    for row in rows:
        out.extend(sorted(row, key=lambda l: l.x0))
    return out


def _set_edges(group: list[Line]) -> None:
    """Stamp each line with its column's left/right edges (robust percentiles)."""
    if not group:
        return
    cleft = _percentile([l.x0 for l in group], 0.12)
    cright = _percentile([l.x1 for l in group], 0.88)
    for l in group:
        l.cleft, l.cright = cleft, cright


def _fills_column(group: list[Line], width: float) -> bool:
    """True when the group holds a TEXT column: a real share of its lines span
    most of the column width. A block of table cells, a column of page numbers
    or the marker gutter of a hanging-indent list does not, which is how they
    are refused. The MEDIAN is not used: a text column that also carries a table
    (common on Official Records pages, where a budget annex sits beside prose)
    has a short median and is still a column."""
    if width <= 0 or len(group) < MIN_COL_LINES:
        return False
    wide = sum(1 for l in group if (l.x1 - l.x0) >= 0.60 * width)
    return wide >= MIN_FILL_RATIO * len(group)


def _looks_single_column(lines: list[Line], span: float) -> bool:
    """True when most lines run the full width of the region — no gutter can
    exist, and saying so early keeps single-column pages cheap to analyse."""
    if span <= 0:
        return True
    wide = sum(1 for l in lines if (l.x1 - l.x0) >= 0.75 * span)
    return wide >= 0.5 * len(lines)


def _has_simple_gutter(group: list[Line]) -> bool:
    """Does this group itself hold two text columns? A COARSE, non-recursive
    test: recursing into `_gutter_candidates` here is exponential (it evaluates
    a candidate per divider position, each of which would evaluate its own), and
    on A/RES/39/246 that ran 220,000 candidate evaluations and never finished."""
    n = len(group)
    if n < 2 * MIN_COL_LINES:
        return False
    xmin = min(l.x0 for l in group)
    xmax = max(l.x1 for l in group)
    span = xmax - xmin
    if span <= 0:
        return False
    d = xmin + 0.15 * span
    stop = xmax - 0.15 * span
    while d <= stop:
        left = [l for l in group if l.x1 <= d]
        right = [l for l in group if l.x0 >= d]
        d += 6.0
        if len(left) < MIN_COL_LINES or len(right) < MIN_COL_LINES:
            continue
        a = _percentile([l.x1 for l in left], 0.95)
        b = _percentile([l.x0 for l in right], 0.05)
        if b - a < MIN_GUTTER_PT:
            continue
        lw, rw = a - xmin, xmax - b
        if lw < MIN_COL_WIDTH_FRAC * span or rw < MIN_COL_WIDTH_FRAC * span:
            continue
        if _fills_column(left, lw) and _fills_column(right, rw):
            return True
    return False


def _is_column_like(group: list[Line], width: float,
                    cache: dict | None = None) -> bool:
    """A side of a candidate split is admissible if it is a text column — or if
    it is itself several columns, which is how a 3-column page is recognised."""
    if _fills_column(group, width):
        return True
    key = (len(group), id(group[0]), id(group[-1])) if group else (0, 0, 0)
    if cache is not None and key in cache:
        return cache[key]
    val = _has_simple_gutter(group)
    if cache is not None:
        cache[key] = val
    return val


def _gutter_candidates(lines: list[Line], depth: int = 0
                       ) -> list[tuple[float, list[Line], list[Line], list[Line]]]:
    """All viable column splits of a region, as (gutter_width, left, right, straddles).

    A candidate divider is tested at every line edge in the central part of the
    region. `left`/`right` are the lines entirely on one side; `straddles` are
    the lines whose box spans the divider. The gutter is the true empty strip
    between the two sides (max left edge → min right edge), so its width is a
    measured whitespace valley, not a histogram artefact."""
    n = len(lines)
    if n < 2 * MIN_COL_LINES:
        return []
    xmin = min(l.x0 for l in lines)
    xmax = max(l.x1 for l in lines)
    span = xmax - xmin
    if span <= 0:
        return []
    # The divider is scanned continuously, not only at line edges: a single
    # stray glyph poking out of a column must not be able to close the gutter.
    # Its width is measured with percentiles for the same reason.
    lo, hi = xmin + 0.15 * span, xmax - 0.15 * span
    out: list[tuple[float, list[Line], list[Line], list[Line]]] = []
    seen: set[tuple[int, int, int]] = set()
    colcache: dict = {}
    d = lo
    while d <= hi:
        left = [l for l in lines if l.x1 <= d]
        right = [l for l in lines if l.x0 >= d]
        cross = [l for l in lines if l.x0 < d < l.x1]
        d += 2.0
        if len(left) < MIN_COL_LINES or len(right) < MIN_COL_LINES:
            continue
        if len(left) < MIN_COL_SHARE * n or len(right) < MIN_COL_SHARE * n:
            continue
        if len(cross) > MAX_CROSS_SHARE * n:
            continue
        key = (len(left), len(right), len(cross))
        if key in seen:
            continue
        a = _percentile([l.x1 for l in left], 0.95)
        b = _percentile([l.x0 for l in right], 0.05)
        gw = b - a
        if gw < MIN_GUTTER_PT or gw > MAX_GUTTER_FRAC * span:
            continue
        lw, rw = a - xmin, xmax - b
        if lw < MIN_COL_WIDTH_FRAC * span or rw < MIN_COL_WIDTH_FRAC * span:
            continue
        if not (_is_column_like(left, lw, colcache)
                and _is_column_like(right, rw, colcache)):
            continue
        seen.add(key)
        out.append((gw, left, right, cross))
    return out


def _stamp_soft_divider(lines: list[Line]) -> bool:
    """Mark the most plausible column boundary of a region the cutter could not
    split, so paragraph reconstruction still refuses to weld across it.

    This is the "refuse rather than invent" path: the region will be read by
    visual row (which may fragment paragraphs), but no paragraph will span the
    boundary, so no sentence is manufactured out of two columns."""
    n = len(lines)
    if n < 2 * MIN_COL_LINES:
        return False
    xmin = min(l.x0 for l in lines)
    xmax = max(l.x1 for l in lines)
    span = xmax - xmin
    if span <= 0 or _looks_single_column(lines, span):
        return False
    best: tuple[int, float, float] | None = None
    d = xmin + 0.30 * span
    while d <= xmin + 0.70 * span:
        left = [l for l in lines if l.x1 <= d]
        right = [l for l in lines if l.x0 >= d]
        cross = sum(1 for l in lines if l.x0 < d < l.x1)
        if (len(left) >= MIN_COL_LINES and len(right) >= MIN_COL_LINES
                and cross <= 0.5 * n):
            a = _percentile([l.x1 for l in left], 0.95)
            b = _percentile([l.x0 for l in right], 0.05)
            if b - a >= MIN_GUTTER_PT and (best is None or cross < best[0]):
                best = (cross, (a + b) / 2, b - a)
        d += 2.0
    if best is None:
        return False
    for l in lines:
        l.soft_divider = best[1]
    return True


def _y_cut_candidates(lines: list[Line]) -> list[int]:
    """Indices (in y order) after which the region has a full-width blank band.

    Used only when a columnar reading is available but dirty: a page whose top
    half is a wide table and whose bottom half is two text columns has no single
    divider, and must be cut horizontally first."""
    order = sorted(lines, key=lambda l: (l.y0, l.x0))
    heights = [l.y1 - l.y0 for l in order]
    min_gap = max(6.0, 1.4 * (_median(heights) or 10.0))
    out: list[tuple[float, int]] = []
    run_y1 = order[0].y1
    for i in range(len(order) - 1):
        run_y1 = max(run_y1, order[i].y1)
        gap = order[i + 1].y0 - run_y1
        if gap >= min_gap:
            out.append((gap, i))
    out.sort(reverse=True)
    return [i for _, i in out[:4]]


def _xy_cut(lines: list[Line], flags: set[str], depth: int = 0) -> list[list[Line]]:
    """Recursively cut a region into reading-ordered groups (see module notes)."""
    if depth >= MAX_CUT_DEPTH or len(lines) < 2 * MIN_COL_LINES:
        if depth >= MAX_CUT_DEPTH and _stamp_soft_divider(lines):
            flags.add("layout_ambiguous_columns")
        return [_row_sort(lines)]
    xmin = min(l.x0 for l in lines)
    span = max(l.x1 for l in lines) - xmin
    if span <= 0 or _looks_single_column(lines, span):
        return [_row_sort(lines)]      # nothing to cut, and cheap to say so
    cands = _gutter_candidates(lines, depth)
    if any(not c[3] for c in cands):
        pass  # a clean vertical split exists: take it below, never cut sideways
    else:
        # Every divider is straddled. Before falling back to bands, try a
        # HORIZONTAL cut at a full-width blank band: a page that is a table over
        # two text columns has no single divider, but each half has one.
        order = sorted(lines, key=lambda l: (l.y0, l.x0))
        for i in (_y_cut_candidates(lines) if depth <= MAX_Y_CUT_DEPTH else []):
            top, bot = order[:i + 1], order[i + 1:]
            if len(top) < MIN_COL_LINES or len(bot) < MIN_COL_LINES:
                continue
            rt = _xy_cut(top, flags, depth + 1)
            rb = _xy_cut(bot, flags, depth + 1)
            if len(rt) > 1 or len(rb) > 1:
                return rt + rb
    if not cands:
        if _stamp_soft_divider(lines):
            flags.add("layout_ambiguous_columns")
        return [_row_sort(lines)]
    # FEWEST straddling lines wins — a true column boundary is one almost no line
    # crosses, and a wide gap that 40 lines span is a ragged margin, not a
    # gutter. Ties break to the widest gutter, then the most balanced split.
    gw, left, right, cross = max(
        cands, key=lambda c: (-len(c[3]), round(c[0], 1),
                              min(len(c[1]), len(c[2]))))
    if not cross:
        return _xy_cut(left, flags, depth + 1) + _xy_cut(right, flags, depth + 1)

    # Straddling lines cut the region into horizontal bands, read in place.
    crossing = {id(l) for l in cross}
    bands: list[tuple[str, list[Line]]] = []
    for l in sorted(lines, key=lambda l: (l.y0, l.x0)):
        kind = "full" if id(l) in crossing else "band"
        if bands and bands[-1][0] == kind:
            bands[-1][1].append(l)
        else:
            bands.append((kind, [l]))
    if not any(k == "band" and len(g) >= 2 * MIN_COL_LINES for k, g in bands):
        # No band is large enough to be worth splitting: the straddles run
        # through the body, so neither a columnar nor a row reading is safe.
        # Refuse to choose — but remember where the boundary probably is, so
        # paragraph reconstruction still never welds the two sides together.
        flags.add("layout_ambiguous_columns")
        midpoint = (_percentile([l.x1 for l in left], 0.95)
                    + _percentile([l.x0 for l in right], 0.05)) / 2
        for l in lines:
            l.soft_divider = midpoint
        return [_row_sort(lines)]
    mid = (_percentile([l.x1 for l in left], 0.95)
           + _percentile([l.x0 for l in right], 0.05)) / 2
    out: list[list[Line]] = []
    for kind, group in bands:
        if kind == "full":
            out.append(_row_sort(group))
            continue
        if len(group) < 2 * MIN_COL_LINES:
            # A band too small to analyse on its own INHERITS the boundary its
            # parent found, so a three-line stretch of two columns is not welded
            # together just because three lines are too few to detect a gutter.
            if len({(l.x0 + l.x1) / 2 < mid for l in group}) > 1:
                for l in group:
                    l.soft_divider = mid
        out.extend(_xy_cut(group, flags, depth + 1))
    return out


def page_divider(groups: list[list[Line]]) -> float | None:
    """The x of the first side-by-side boundary between two regions of a page.

    Used to give the small-font footnote apparatus the same column geometry as
    the body it sits under: a footnote zone is often too small (fewer than eight
    lines) for column detection to run on its own, and without a boundary the
    left column's footnote is glued to the right column's."""
    for a, b in zip(groups, groups[1:]):
        ax1 = _percentile([l.x1 for l in a], 0.95)
        bx0 = _percentile([l.x0 for l in b], 0.05)
        if bx0 - ax1 >= MIN_GUTTER_PT:
            return (ax1 + bx0) / 2
    return None


def split_columns(lines: list[Line], page_width: float,
                  flags: set[str] | None = None) -> list[list[Line]]:
    """Return reading-ordered line groups for one page, with per-group edges."""
    if flags is None:
        flags = set()
    if not lines:
        return []
    groups = [g for g in _xy_cut(lines, flags) if g]
    for g in groups:
        _set_edges(g)
    return groups


# ---------------------------------------------------------------------------
# Structural-start patterns (shared vocabulary with the parser)
# ---------------------------------------------------------------------------

OPENING_RE = re.compile(
    r"^[\"“”‘’']?\s*The\s+(General Assembly|Security Council|"
    r"Economic and Social Council|Human Rights Council|Trusteeship Council)\s*,?\s*$")
OP_NUM_RE = re.compile(r"^\(?\s*(\d{1,3})\s*\.\s+\S")
OP_PAREN_RE = re.compile(r"^\(\s*([A-Za-z]{1,7}|\d{1,3})\s*\)\s+\S")
MEETING_RE = re.compile(r"^\[?\s*\d+\s*(st|nd|rd|th|II\b|d\b)?\s*(plenary\s+)?meeting\b", re.I)
# An ADOPTION RECORD, not any sentence containing the word. This used to be
# `re.I`, so a preambular clause whose paragraph happened to begin with the
# lowercase word "adopted" ("adopted at its sixtieth session a declaration …")
# ended the crop: 145 documents were stored at ~21% of their source, and
# A/RES/701(VII) — Korea, reports of the United Nations Korean Reconstruction
# Agency — was stored as ONE row of 47 characters, its title line. Case-
# sensitive, and the record is a short standalone line ("Adopted at the 1518th
# plenary meeting", "Adopted unanimously", "Adopted by 96 votes to none").
ADOPTED_RE = re.compile(
    r"^\[?\s*Adopted\b(?:\s+(?:at|by|on|unanimously|without)\b.{0,120})?\s*[.\]]?\s*$")
DATE_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]+\s+\d{4}\.?\s*$")
PREAMBULAR_FIRST = frozenset("""
recalling reaffirming noting recognizing recognising welcoming considering
convinced concerned emphasizing emphasising expressing guided having bearing
taking mindful alarmed acknowledging determined determining stressing underlining
underscoring desiring desirous aware regretting deploring affirming observing
believing conscious deeply gravely firmly fully further reiterating seeking
encouraged endorsing commending appreciating anxious cognizant remaining keeping
realizing supporting invoking highlighting confident hopeful resolved eager
grateful pleased sharing aiming inspired""".split())
# resolution number heading (old GA/ECOSOC "1260 (XIII)."; SC "338 (1973).";
# modern "48/23.") — used to find neighbour boundaries.
# The session in parentheses is whatever the printer set and OCR mangled —
# '(XIII)', '(ES-II)', '(S-IV)', '(XXI l)'. Keying on its SHAPE lost the boundary
# on every special-session volume, so A/RES/1005(ES-II) could not see that
# '1004 (ES-II).' was a different resolution and served 1004, 1007 and 1008 as
# its own. Only the NUMBER is matched; the trailing period is required so that a
# cross-reference ("resolution 1002 (ES-I) of 7 November 1956") is not a heading.
HEADING_ROMAN_RE = re.compile(
    r"^\(?\s*(?:Resolutions?\s+)?(\d{1,4})\s*[A-Z]?\s*[\(\[]([^)\]]{1,14})[\)\]]"
    r"(?:\s*[.．]|\s*$)")
HEADING_SLASH_RE = re.compile(r"^\s*([A-Z]?-?\d{1,4})/(\d{1,4}[A-Za-z]*)\s*\.")
HEADING_SC_RE = re.compile(r"^\s*Resolution\s+(\d{1,4})\s*\(\s*(\d{4})\s*\)", re.I)
DECISION_HEAD_RE = re.compile(r"^Decisions?\s*$", re.I)
# A compilation 'Decisions' NARRATIVE block header: the SC "Resolutions and
# Decisions" volumes run 'Decision(s) At its Nth meeting, ... the Council decided'
# blocks straight after a resolution's operative tail (the adoption record itself
# often lives in the small-font footnote apparatus). Ending the crop here stops the
# following Decisions narrative from bleeding into the target region.
DECISION_BLOCK_RE = re.compile(r"^Decisions?\s+(?:At|On|The|Following)\b", re.I)

# Operative lead verbs (complement PREAMBULAR_FIRST) — used only by the
# first-word lead-verb OCR repair vocabulary, never for classification.
OPERATIVE_LEAD = frozenset("""
decides requests calls demands urges reaffirms recalls invites notes expresses
condemns declares endorses authorizes approves welcomes recommends encourages
emphasizes stresses appeals appoints adopts affirms agrees commends confirms
considers deplores designates determines directs draws elects establishes
instructs proclaims regrets reiterates renews resolves supports transmits
underlines warns decides taking demanding requesting calling noting
""".split())


_ORGANS = {
    "thegeneralassembly": "The General Assembly,",
    "thesecuritycouncil": "The Security Council,",
    "theeconomicandsocialcouncil": "The Economic and Social Council,",
    "thehumanrightscouncil": "The Human Rights Council,",
    "thetrusteeshipcouncil": "The Trusteeship Council,",
}


def repair_opening(text: str) -> str:
    """Repair an OCR-garbled opening formula to its canonical form.

    The parser anchors the preamble/operative state machine on an EXACT
    'The General Assembly,' line; OCR noise ('The General Assemb/y,') breaks it
    and demotes the whole preamble to frontmatter. Only a short standalone line
    that fuzzily matches one organ formula is rewritten — body prose that merely
    starts 'The General Assembly requests ...' is far too long to match."""
    t = text.strip()
    if len(t) > 46 or not t.lower().startswith("the "):
        return text
    from difflib import SequenceMatcher
    key = re.sub(r"[^a-z]", "", t.lower())
    for canon_key, canon in _ORGANS.items():
        if SequenceMatcher(None, key, canon_key).ratio() >= 0.86:
            return canon
    return text


# ---------------------------------------------------------------------------
# Sequence-confirmed OCR marker repair + first-word lead-verb repair
# ---------------------------------------------------------------------------
# Glyphs an OCR engine commonly emits for a leading DIGIT marker. Repair fires
# ONLY when arithmetic sequence confirmation holds (the neighbouring real numeric
# markers at the same indent bracket the candidate), so a genuine roman 'I.'/'II.'
# heading — which is followed by 'II.'/'III.', not '2.' — is never rewritten.
_OCR_DIGIT = {"I": "1", "l": "1", "|": "1", "i": "1", "J": "1",
              "S": "5", "O": "0", "o": "0", "Z": "2", "B": "8"}
_CONFUSABLE_MARKER = re.compile(r"^([IlJ|iSOoZB])\.(?:\s*$|\s+\S)")
_NUM_MARKER = re.compile(r"^(\d{1,3})[.)](?:\s|$)")

# Lead-verb repair vocabulary: preambular + operative first words. First-word
# only, edit distance 1, prefix-anchored, unique — improves the parser's
# preambular/operative classification of an OCR-garbled lead verb without touching
# body text (the acceptance gate holds body text verbatim against pdftotext).
LEAD_VERB_VOCAB = PREAMBULAR_FIRST | OPERATIVE_LEAD


def _num_marker_val(text: str) -> int | None:
    m = _NUM_MARKER.match(text.strip())
    return int(m.group(1)) if m else None


# OCR-junk letters: rare in the target lead verbs, so a single substitution that
# replaces a common target letter WITH one of these is almost certainly OCR damage
# ('Recallin-g' -> 'Recallin-x', 'Gravel-y' -> 'Gravel-v'). Requiring the damaged
# letter to be junk blocks false positives on genuine inflections that differ by a
# common letter ('authorized' -> 'authorizes' d/s, 'transmis' -> 'transmits').
_JUNK_LETTERS = frozenset("xvzjq")


def repair_lead_verb(text: str) -> str | None:
    """If the FIRST word is a single-substitution OCR corruption of a UNIQUE lead
    verb — the damaged letter being an OCR-junk letter — return the text with only
    that word repaired; else None. Equal-length only, prefix-anchored (first two
    letters match), and never touches anything past the first word (body stays
    verbatim for the acceptance gate)."""
    m = re.match(r"^([A-Za-z]{5,14})(?=$|[\s,.:;])", text)
    if not m:
        return None
    w = m.group(1)
    wl = w.lower()
    if wl in LEAD_VERB_VOCAB:
        return None  # already a valid lead verb
    cands: list[str] = []
    for v in LEAD_VERB_VOCAB:
        if v[:2] != wl[:2] or len(v) != len(wl):
            continue  # equal-length single substitution only
        diffs = [(o, t) for o, t in zip(wl, v) if o != t]
        if len(diffs) != 1:
            continue
        orig, targ = diffs[0]
        if orig in _JUNK_LETTERS and targ not in _JUNK_LETTERS:
            cands.append(v)
    if len(cands) != 1:
        return None  # no unique repair -> leave verbatim
    best = cands[0]
    repl = best.capitalize() if w[0].isupper() else best
    return repl + text[len(w):]


def _repair_ocr_markers(paras: list[Para], log: list[tuple[str, str]]) -> list[Para]:
    """Rewrite a mis-OCR'd leading digit marker ('I.'->'1.', 'S.'->'5.') when the
    surrounding real numeric markers at the SAME indent arithmetically confirm it
    (previous+1 == candidate == next-1). Runs before hanging-marker merging so a
    repaired standalone '1.' merges into its clause like a native marker."""
    for i, p in enumerate(paras):
        t = p.text.strip()
        m = _CONFUSABLE_MARKER.match(t)
        if not m:
            continue
        cand = _OCR_DIGIT.get(m.group(1))
        if cand is None:
            continue
        cand_i = int(cand)
        x0 = p.x0
        nxt = prv = None
        for j in range(i + 1, min(i + 5, len(paras))):
            v = _num_marker_val(paras[j].text)
            if v is not None and abs(paras[j].x0 - x0) < 45:
                nxt = v
                break
        for j in range(i - 1, max(i - 5, -1), -1):
            v = _num_marker_val(paras[j].text)
            if v is not None and abs(paras[j].x0 - x0) < 45:
                prv = v
                break
        # Arithmetic sequence confirmation — REQUIRE a confirming next marker.
        if nxt is None or cand_i != nxt - 1:
            continue
        if prv is not None and cand_i != prv + 1:
            continue
        standalone = bool(re.fullmatch(r"[IlJ|iSOoZB]\.", t))
        after = f"{cand}." if standalone else cand + t[1:]
        p.override = after
        log.append((t, after))
    return paras


def _terminal(text: str) -> bool:
    return bool(text) and text.rstrip()[-1:] in ".,;:?!\"”’)"


def _structural_start(text: str) -> bool:
    t = text.strip()
    if OPENING_RE.match(t) or OP_NUM_RE.match(t) or OP_PAREN_RE.match(t):
        return True
    if MEETING_RE.match(t) or ADOPTED_RE.match(t) or DATE_RE.match(t):
        return True
    if HEADING_ROMAN_RE.match(t) or HEADING_SLASH_RE.match(t):
        return True
    first = re.split(r"[\s,]", t, maxsplit=1)[0].lower()
    return first in PREAMBULAR_FIRST


# ---------------------------------------------------------------------------
# Paragraph reconstruction
# ---------------------------------------------------------------------------

@dataclass
class Para:
    lines: list[Line] = field(default_factory=list)
    override: str | None = None   # forced text (hanging-marker merge)
    hyph: "HyphenPolicy | None" = None   # document's hyphenation evidence

    @property
    def text(self) -> str:
        if self.override is not None:
            return self.override
        return _join_lines(self.lines, self.hyph)

    @property
    def x0(self) -> float:
        return self.lines[0].x0


SIDE_BY_SIDE_GAP = 20.0     # pt; wider than any inter-word or marker gap


def _side_by_side(prev: Line, ln: Line) -> bool:
    """True when two lines sit on the same printed row with a wide gap between
    them — separate table cells, or the two halves of an unsplit two-column
    region. Never true for consecutive lines of a paragraph, which are on
    different rows."""
    if ln.page != prev.page:
        return False
    height = max(prev.y1 - prev.y0, 4.0)
    if abs(ln.y0 - prev.y0) > 0.5 * height:
        return False
    # Either order: reading a column back up the page lands the next line to the
    # LEFT of the previous one, which is the wrap of a failed column split.
    gap = max(ln.x0 - prev.x1, prev.x0 - ln.x1)
    if gap > SIDE_BY_SIDE_GAP:
        return True
    # Overlapping boxes on the same row are contradictory geometry (a full-width
    # line lying across a column line): not one sentence either.
    shorter = min(prev.x1 - prev.x0, ln.x1 - ln.x0, 1e9)
    return gap < -0.2 * max(shorter, 1.0)


def _continues_across_columns(prev: Line, first: Line) -> bool:
    """Newspaper continuation: may the last line of one column/page be joined to
    the first line of the next?

    The bar is deliberately high, because a wrong JOIN invents a sentence while a
    wrong SPLIT only costs a paragraph boundary. All of the following must hold:
      * the previous line ends mid-word (hyphen) or mid-clause (no terminal
        punctuation at all — not even a comma, which in UN drafting ends a
        preambular paragraph);
      * that line FILLS its column (a short last line means the paragraph ended);
      * the next line is not a structural start and is not first-line indented.
    """
    t = (prev.text or "").rstrip()
    nt = (first.text or "").strip()
    if not t or not nt:
        return False
    hyphen_break = t.endswith("-") and len(t) >= 2 and t[-2].isalpha()
    if not hyphen_break:
        if not t[-1:].isalnum():
            return False               # any punctuation ends it
        if prev.cright and prev.x1 < prev.cright - 6:
            return False               # short line => paragraph ended
    if _structural_start(nt):
        return False
    if first.cleft and first.x0 > first.cleft + 7:
        return False                   # indented => a new paragraph
    if nt[:1].isupper() and not hyphen_break:
        return False                   # a capital opens a new sentence/heading
    return True


def _join_lines(lines: list[Line], hyph: "HyphenPolicy | None" = None) -> str:
    """Join a paragraph's lines, resolving end-of-line hyphenation on evidence."""
    out = ""
    for i, ln in enumerate(lines):
        t = ln.text
        if i == 0:
            out = t
            continue
        if out.endswith("-") and len(out) >= 2 and out[-2].isalpha() and t[:1].islower():
            policy = hyph or _default_hyphen()
            if policy.keep_hyphen(out, t):
                out = out.rstrip() + t.lstrip()      # 'self-' + 'determination'
            else:
                out = out[:-1] + t.lstrip()          # 'avoid-' + 'ing'
        else:
            out = out.rstrip() + " " + t.lstrip()
    return out


def _median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(p * len(s)))]


_BARE_MARKER_RE = re.compile(r"^\(?\s*(\d{1,3})\s*[.)]?\s*$|^\(\s*[A-Za-z]{1,4}\s*\)\.?$")
# a lone resolution-number heading with NO title on the same line (born-digital
# docs split '48/23.' from its title into separate blocks)
_LONE_HEADING_RE = re.compile(
    r"^([A-Z]?-?\d{1,4}/\d{1,4}[A-Za-z]*|\(?\d{1,4}\s*\([A-Za-z0-9]{1,8}\))\s*\.\s*$")


def reconstruct_paragraphs(col_lines: list[list[Line]],
                           marker_repair_log: list[tuple[str, str]] | None = None,
                           hyph: "HyphenPolicy | None" = None,
                           ) -> tuple[list[Para], float, float]:
    """Group ordered lines into paragraphs. Returns (paras, col_left, col_right).

    A new paragraph begins on: a structural-start line; a first-line indent past
    the line's OWN column left edge; a large vertical gap; a page change; or a
    font-size jump (heading). A paragraph may flow across a column boundary
    (newspaper order) via the natural continuation rule. Finally, a bare hanging
    marker line ('1.', '(a)') is merged into the following clause, so born-digital
    UN docs that place the number in its own block still yield '1. Requests ...'."""
    all_lines = [l for col in col_lines for l in col]
    if not all_lines:
        return [], 0.0, 0.0
    col_left = _percentile([l.x0 for l in all_lines], 0.15)
    col_right = _percentile([l.x1 for l in all_lines], 0.85)
    gaps: list[float] = []
    for col in col_lines:
        for a, b in zip(col, col[1:]):
            if b.page == a.page and b.y0 >= a.y0:
                gaps.append(b.y0 - a.y1)
    med_gap = _median([g for g in gaps if g > 0]) or 3.0
    body_size = _median([l.size for l in all_lines]) or 10.0

    paras: list[Para] = []
    prev: Line | None = None
    for col in col_lines:
        first_of_group = True
        for ln in col:
            start = False
            if prev is None:
                start = True
            elif first_of_group:
                # COLUMN / REGION BOUNDARY. Default is a paragraph break: gluing
                # two columns together is how fabricated sentences were made.
                # Only the newspaper continuation rule may override it.
                start = not _continues_across_columns(prev, ln)
            elif _structural_start(ln.text):
                start = True
            elif ln.page != prev.page:
                start = True
            elif (ln.soft_divider
                  and ((prev.x0 + prev.x1) / 2 < ln.soft_divider)
                  != ((ln.x0 + ln.x1) / 2 < ln.soft_divider)):
                # An unresolved column boundary lies between these two lines.
                start = True
            elif _side_by_side(prev, ln):
                # Two boxes on the SAME printed row, separated by a wide gap:
                # table cells, or two columns of a region that could not be
                # split. They are not one sentence, and joining them is how
                # "TOTAL, PART I TOTAL, PART II" and column weaves are made.
                start = True
            elif (ln.page == prev.page
                  and (ln.y0 - prev.y1 > 1.6 * med_gap
                       or ln.y0 - prev.y1 > 2.0 * max(ln.y1 - ln.y0, 4.0))):
                # …or an absolute drop of more than two line heights: in a
                # sparse group (a handful of footnote or table lines) the median
                # gap is itself huge, and a page-tall jump would pass 1.6×med.
                start = True
            elif ln.x0 > ln.cleft + 7 and _terminal(prev.text):
                start = True  # first-line indent after a completed sentence
            elif ln.size >= body_size + 1.5 and prev.size < body_size + 1.5:
                start = True  # font jump into a heading
            first_of_group = False
            if start:
                paras.append(Para([ln], hyph=hyph))
            else:
                paras[-1].lines.append(ln)
            prev = ln

    if marker_repair_log is not None:
        paras = _repair_ocr_markers(paras, marker_repair_log)
    return _merge_hanging_markers(paras), col_left, col_right


def _marker_adjacent(marker: Line, nxt: Line) -> bool:
    """True when a bare marker line is physically attached to the line that
    follows it — same page, and either on the same row just to its left, or
    immediately above it in the same column.

    Without this test any bare number anywhere in the document could be welded
    onto an unrelated line: S/RES/661(1990) stored `'19. nationals or in their
    territories which promote …'` because the page number '19' at the foot of
    page 1 was merged into the first line of page 2. That is an operative
    paragraph number that does not exist in the resolution — an invented fact,
    not a lost one."""
    if marker.page != nxt.page:
        return False
    h = max(marker.y1 - marker.y0, 4.0)
    same_row = abs(nxt.y0 - marker.y0) <= 0.6 * h and -2.0 <= nxt.x0 - marker.x1 <= 40.0
    below = (-0.5 * h <= nxt.y0 - marker.y1 <= 1.5 * h
             and abs(nxt.x0 - marker.x0) <= 60.0)
    return same_row or below


def _merge_hanging_markers(paras: list[Para]) -> list[Para]:
    """Merge a paragraph that is just a hanging marker ('1.', '(a)') into the
    next paragraph, reconstructing 'N. <verb> ...' for the parser's lexical path."""
    out: list[Para] = []
    i = 0
    while i < len(paras):
        p = paras[i]
        t = p.text.strip()
        if (_BARE_MARKER_RE.match(t) and i + 1 < len(paras)
                and _marker_adjacent(p.lines[-1], paras[i + 1].lines[0])):
            nxt = paras[i + 1]
            # normalise "1" / "1)" -> "1."; keep "(a)" as-is
            marker = t if t.endswith((".", ")")) else t + "."
            out.append(Para(p.lines + nxt.lines, hyph=p.hyph,
                            override=f"{marker} {nxt.text.lstrip()}"))
            i += 2
            continue
        # lone number-heading ('48/23.') + its title line on the next block
        if (_LONE_HEADING_RE.match(t) and i + 1 < len(paras)
                and not _structural_start(paras[i + 1].text)
                and _marker_adjacent(p.lines[-1], paras[i + 1].lines[0])):
            nxt = paras[i + 1]
            out.append(Para(p.lines + nxt.lines, hyph=p.hyph,
                            override=f"{t} {nxt.text.lstrip()}"))
            i += 2
            continue
        out.append(p)
        i += 1
    return out


# ---------------------------------------------------------------------------
# Target cropping
# ---------------------------------------------------------------------------

@dataclass
class CropResult:
    start: int
    end: int          # exclusive
    anchor_found: bool
    flags: list[str]


def _target_matchers(symbol_normalized: str):
    """Build (target_regex, target_number) from a symbol. target_number lets the
    neighbour-boundary detector recognise a DIFFERENT resolution heading."""
    m = re.match(r"^[AES]/RES/(.+)$", symbol_normalized)
    if not m:
        return None, None
    rest = m.group(1)
    # Every form may be printed with a leading 'Resolution' — the Official
    # Records supplements set 'Resolution 1005 (ES-II)' on its own line. Without
    # that alternative the target's own heading is invisible to the crop, which
    # then falls back to a guess: A/RES/1005(ES-II) served resolutions 1004,
    # 1007 and 1008 as its own text.
    pre = r"^\(?\s*(?:Resolutions?\s+)?"
    mo = re.match(r"^(\d{1,4})\((\d{4})\)$", rest)          # S/RES/338(1973)
    if mo:
        num = mo.group(1)
        return re.compile(pre + rf"{num}\s*\(\s*{mo.group(2)}"), num
    mo = re.match(r"^(\d{1,4})\(([A-Za-z0-9\-]+)\)$", rest)  # A/RES/1260(XIII)
    if mo:
        num = mo.group(1)
        return re.compile(pre + rf"{num}\s*[A-Z]?\s*\("), num
    mo = re.match(r"^([A-Z]?-?\d{1,4})/(\d{1,4}[A-Za-z]*)$", rest)  # 48/23, 1978/6, S-15/1
    if mo:
        sess, num = mo.group(1), mo.group(2)
        return (re.compile(pre + rf"{re.escape(sess)}/{re.escape(num)}\s*[.．]"),
                f"{sess}/{num}")
    return None, None


def _heading_number(text: str) -> str | None:
    m = HEADING_SC_RE.match(text)     # SC "Resolution 650 (1990)"
    if m:
        return m.group(1)
    m = HEADING_ROMAN_RE.match(text)
    if m:
        return m.group(1)
    m = HEADING_SLASH_RE.match(text)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    return None


def crop_to_target(paras: list[Para], symbol_normalized: str) -> CropResult:
    """Locate the target resolution inside a compilation excerpt.

    The printed extent of a resolution is [ its own number heading , the next
    resolution's number heading ). Inside that, the crop may end early at the
    target's own ADOPTION RECORD — but only when no further part of the same
    resolution follows it (omnibus resolutions print A, B, C … each with its own
    record).

    Both boundaries have failed in production and both failures are recorded
    here so they cannot come back:

      * ENDING TOO EARLY. `ADOPTED_RE` was case-insensitive `^adopted`, so a
        paragraph beginning with the ordinary word "adopted" closed the crop:
        145 documents were stored at ~21% of their source and A/RES/701(VII)
        was stored as its title line alone (47 characters).

      * RUNNING PAST THE END. When the target's own heading could not be matched
        the crop kept the WHOLE file, so A/RES/1005(ES-II) carried resolutions
        1004, 1007 and 1008, and A/RES/529(VI) carried 526, 527, 528 and 530 —
        other documents' text served under this symbol. When the file opens mid
        target (its heading is on an earlier page), the target's tail is bounded
        by the FIRST heading that belongs to someone else.
    """
    flags: list[str] = []
    target_re, target_num = _target_matchers(symbol_normalized)
    texts = [p.text for p in paras]
    n = len(paras)
    foreign = [i for i, t in enumerate(texts)
               if (hn := _heading_number(t)) is not None and hn != target_num]

    start = 0
    anchor = False
    if target_re is not None:
        matches = [i for i, t in enumerate(texts) if target_re.match(t)]
        # Old SC/GA compilation pages are bilingual (French + English). Prefer a
        # heading occurrence followed by an ENGLISH opening formula within a short
        # window over one trailed by 'Le Conseil'/'L'Assemblée' (French).
        english = [i for i in matches
                   if any(OPENING_RE.match(texts[j]) for j in range(i, min(i + 6, n)))]
        if english:
            start, anchor = english[0], True
        elif matches:
            start, anchor = matches[0], True
    if not anchor:
        # The target's own heading is not in this file (it opens mid-resolution,
        # or OCR destroyed the heading). Keep the LEAD region: everything up to
        # the first heading that is demonstrably a different resolution.
        lead_end = next((i for i in foreign if i >= 2), None)
        if lead_end is not None:
            flags.append("crop_anchor_not_found_lead_region")
            return CropResult(0, lead_end, False, flags)
        flags.append("crop_anchor_not_found_whole_file")
        return CropResult(0, n, False, flags)

    # Find crop end after the start. The printed extent of a resolution ends at
    # the NEXT resolution's heading, so that boundary wins whenever it exists.
    # The document's own adoption record is only a fallback for the last (or
    # only) resolution in the file: ending there whenever it appeared cost
    # S/RES/245(1968) a third of its region, and an omnibus resolution prints one
    # record per lettered part.
    end = n
    seen_opening = False
    record_end: int | None = None
    for j in range(start + 1, n):
        t = texts[j]
        if OPENING_RE.match(t):
            seen_opening = True
            continue
        hn = _heading_number(t)
        if hn is not None and hn != target_num:
            end = j  # next (different) resolution heading
            break
        if seen_opening and (DECISION_HEAD_RE.match(t) or DECISION_BLOCK_RE.match(t)):
            end = j  # an SC 'Decision(s)' block (bare or narrative) after the body
            break
        if seen_opening and record_end is None and (MEETING_RE.match(t) or ADOPTED_RE.match(t)):
            record_end = j + 1
            if record_end < n and DATE_RE.match(texts[record_end]):
                record_end += 1
    if end == n and record_end is not None:
        end = record_end
    if not seen_opening and end == n:
        flags.append("crop_no_opening_after_anchor")
    return CropResult(start, end, True, flags)


# ---------------------------------------------------------------------------
# Raw-row emission (matches fulltext_extract_raw's contract)
# ---------------------------------------------------------------------------

def _line_props(p: Para, tri: Triage) -> dict:
    lines = p.lines
    text = p.text
    cleft = lines[0].cleft
    cright = lines[0].cright
    size = _median([l.size for l in lines])
    bold = all(l.bold for l in lines) and bool(lines)
    italic = all(l.italic for l in lines) and bool(lines)
    props: dict = {"pdf": True, "textlayer_score": tri.klass, "size": round(size, 1)}
    if bold:
        props["bold"] = True
    if italic:
        props["italic"] = True
    if lines[0].lead_italic_text and not italic:
        props["lead_italic_text"] = lines[0].lead_italic_text
    # first-line indent relative to the line's own column left edge (presence is
    # the signal the parser reads; the exact value is informational).
    indent = lines[0].x0 - cleft
    if indent > 7:
        props["indent_firstline"] = int(round(indent))
    # all-caps
    alpha = [c for c in text if c.isalpha()]
    if len(alpha) >= 2 and all(c.isupper() for c in alpha):
        props["all_caps"] = True
    # centered short line (title/heading signal)
    if len(text) <= 70 and not p.override:
        left_gap = lines[0].x0 - cleft
        right_gap = cright - lines[0].x1
        span = max(cright - cleft, 1.0)
        if left_gap > 0.12 * span and abs(left_gap - right_gap) < 0.20 * span:
            props["alignment"] = "center"
    return props


def _new_row(position: int, kind: str, text: str, props: dict | None) -> dict:
    return {
        "position": position, "kind": kind, "text": text,
        "style_id": None, "style_name": None, "numbering": None,
        "props": props, "table_cell": None, "hyperlinks": None,
        "footnote_ref": None,
    }


def build_rows(paras: list[Para], crop: CropResult, tri: Triage) -> list[dict]:
    rows: list[dict] = []
    pos = 0
    prev_line: Line | None = None
    for p in paras[crop.start:crop.end]:
        # emit an 'empty' structural marker on a large vertical gap / page break
        if prev_line is not None:
            first = p.lines[0]
            if first.page != prev_line.page or first.y0 - prev_line.y1 > 14:
                rows.append(_new_row(pos, "empty", "", None))
                pos += 1
        props = _line_props(p, tri)
        rows.append(_new_row(pos, "paragraph", p.text, props))
        pos += 1
        prev_line = p.lines[-1]
    return rows


# ---------------------------------------------------------------------------
# Whole-document extraction
# ---------------------------------------------------------------------------

@dataclass
class ExtractResult:
    triage: Triage
    rows: list[dict]
    dropped_headers: list[str]
    crop: CropResult | None
    n_columns: int
    marker_repairs: list[tuple[str, str]] = field(default_factory=list)
    leadverb_repairs: list[tuple[str, str]] = field(default_factory=list)
    french_dropped: int = 0
    foreign_dropped_lines: list[str] = field(default_factory=list)
    layout_flags: list[str] = field(default_factory=list)
    hyphen: dict[str, int] = field(default_factory=dict)

    def summary_note(self) -> str:
        """One ledger line carrying every decision that removed or altered text,
        so a drop is never counted only at run time (audit finding X3)."""
        parts = [self.triage.summary()]
        if self.hyphen:
            parts.append("hyphen kept=%d dropped=%d (doc=%d corpus=%d prefix=%d "
                         "default=%d)" % (
                             self.hyphen.get("kept", 0), self.hyphen.get("dropped", 0),
                             self.hyphen.get("doc", 0), self.hyphen.get("corpus", 0),
                             self.hyphen.get("prefix", 0), self.hyphen.get("default", 0)))
        if self.french_dropped:
            parts.append(f"lang_dropped_lines={self.french_dropped}")
        if self.layout_flags:
            parts.append("layout=" + ",".join(self.layout_flags))
        return " | ".join(parts)


def extract_pdf(path: Path, symbol_normalized: str) -> ExtractResult:
    doc = fitz.open(path)
    pages_text = [doc[i].get_text("text") for i in range(doc.page_count)]
    tri = triage_text(pages_text)
    if tri.klass == "none":
        doc.close()
        return ExtractResult(tri, [], [], None, 1)

    page_heights = {i: doc[i].rect.height for i in range(doc.page_count)}
    page_widths = {i: doc[i].rect.width for i in range(doc.page_count)}
    all_lines: list[Line] = []
    for i in range(doc.page_count):
        all_lines.extend(extract_lines(doc[i], i))
    doc.close()

    kept, dropped = drop_headers_footers(all_lines, page_heights)

    # Separate small-font footnote lines (bottom-of-column apparatus in the old
    # two-column supplements: body ~9pt, footnotes ~5pt) from the body flow, so
    # they do not glue onto body text across a column boundary. They are appended
    # after the body as kind='footnote' rows, mirroring the docx extractor.
    body_size = _median([l.size for l in kept]) or 10.0
    body_lines = [l for l in kept if l.size >= body_size - 1.5]
    foot_lines = [l for l in kept if l.size < body_size - 1.5]

    # This document's own within-line spellings decide its line-break hyphens.
    hyph = HyphenPolicy()
    hyph.observe(kept)

    # LAYOUT: per page, cut into reading-ordered regions (columns and bands).
    layout_flags: set[str] = set()
    col_groups: list[list[Line]] = []
    n_columns = 1
    for i in range(len(page_heights)):
        pls = [l for l in body_lines if l.page == i]
        cols = split_columns(pls, page_widths.get(i, 622.0), layout_flags)
        n_columns = max(n_columns, len(cols))
        col_groups.extend(cols)
        # the footnote apparatus inherits the body's column boundary
        divider = page_divider(cols)
        if divider is not None:
            for l in foot_lines:
                if l.page == i:
                    l.soft_divider = divider

    # FACING-LANGUAGE: the old GA/ECOSOC supplements print English and French
    # side by side. The French is a whole COLUMN, so the decision is made per
    # region and per document — never per line, which is how 1,003 English lines
    # naming French-titled NGOs were deleted from 39 monolingual documents.
    col_groups, foreign_lines = drop_foreign_regions(col_groups, layout_flags)

    marker_repairs: list[tuple[str, str]] = []
    paras, _col_left, _col_right = reconstruct_paragraphs(col_groups, marker_repairs, hyph)
    # Repair OCR-garbled opening formulas so the parser's state machine anchors,
    # and OCR-garbled first-word lead verbs (first word only, verbatim body).
    leadverb_repairs: list[tuple[str, str]] = []
    for p in paras:
        fixed = repair_opening(p.text)
        if fixed != p.text:
            p.override = fixed
            continue
        before_text = p.text
        lv = repair_lead_verb(before_text)
        if lv is not None and lv != before_text:
            p.override = lv
            leadverb_repairs.append((before_text.split(None, 1)[0],
                                     lv.split(None, 1)[0]))
    crop = crop_to_target(paras, symbol_normalized)
    rows = build_rows(paras, crop, tri)
    # Footnotes are appended after the cropped body, so they used to escape the
    # crop entirely and carry the neighbouring resolutions' apparatus into this
    # document. Keep only the pages the cropped body actually occupies.
    body_pages = {l.page for p in paras[crop.start:crop.end] for l in p.lines}
    if body_pages:
        foot_lines = [l for l in foot_lines if l.page in body_pages]
    rows, foot_foreign = _append_footnote_rows(rows, foot_lines, page_widths, tri,
                                               hyph, layout_flags)
    foreign_lines += foot_foreign
    return ExtractResult(tri, rows, dropped, crop, n_columns,
                         marker_repairs=marker_repairs,
                         leadverb_repairs=leadverb_repairs,
                         french_dropped=len(foreign_lines),
                         foreign_dropped_lines=foreign_lines,
                         layout_flags=sorted(layout_flags),
                         hyphen={"kept": hyph.kept, "dropped": hyph.dropped,
                                 **hyph.decisions})


def _append_footnote_rows(rows: list[dict], foot_lines: list[Line],
                          page_widths: dict[int, float], tri: Triage,
                          hyph: "HyphenPolicy | None" = None,
                          flags: set[str] | None = None,
                          ) -> tuple[list[dict], list[str]]:
    """Reconstruct footnote paragraphs from the small-font lines and append them
    as kind='footnote' rows after the cropped body (document order preserved)."""
    if flags is None:
        flags = set()
    if not foot_lines:
        return rows, []
    groups: list[list[Line]] = []
    for i in sorted(page_widths):
        pls = [l for l in foot_lines if l.page == i]
        groups.extend(split_columns(pls, page_widths.get(i, 622.0), flags))
    groups, foreign = drop_foreign_regions(groups, flags)
    fparas, _, _ = reconstruct_paragraphs(groups, None, hyph)
    pos = rows[-1]["position"] + 1 if rows else 0
    for p in fparas:
        if not p.text.strip():
            continue
        rows.append(_new_row(pos, "footnote", p.text,
                             {"pdf": True, "textlayer_score": tri.klass,
                              "size": round(_median([l.size for l in p.lines]), 1)}))
        pos += 1
    return rows, foreign


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

_INSERT = (
    "INSERT INTO digitallibrary.document_paragraphs_raw "
    "(symbol_normalized, lang, position, kind, text, style_id, style_name, "
    " numbering, props, table_cell, hyperlinks, footnote_ref, extractor_version) "
    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)


def write_document(conn, symbol: str, lang: str, rows: list[dict]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM digitallibrary.document_paragraphs_raw "
            "WHERE symbol_normalized = %s AND lang = %s",
            [symbol, lang])
        params = [
            (symbol, lang, r["position"], r["kind"], r["text"], None, None,
             None, Jsonb(r["props"]) if r["props"] is not None else None,
             None, None, None, EXTRACTOR_VERSION)
            for r in rows
        ]
        cur.executemany(_INSERT, params)


# ---------------------------------------------------------------------------
# Targets + main loop
# ---------------------------------------------------------------------------

def fetch_targets(symbols: list[str] | None, force: bool, limit: int | None):
    statuses = ["fetched", "extracted", "no_text_layer"] if force else ["fetched"]
    sql = (
        "SELECT symbol_normalized, lang, archive_path "
        "FROM digitallibrary.document_files "
        "WHERE format = 'pdf' AND status = ANY(%s) AND archive_path IS NOT NULL")
    params: list[object] = [statuses]
    if symbols:
        sql += " AND symbol_normalized = ANY(%s)"
        params.append(symbols)
    sql += " ORDER BY symbol_normalized"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser(description="Raw paragraph extractor — PDF path (pre-1994)")
    ap.add_argument("--limit", type=int, help="extract at most N documents")
    ap.add_argument("--symbols", help="comma-separated symbol_normalized list")
    ap.add_argument("--force", action="store_true",
                    help="also re-extract 'extracted'/'no_text_layer' rows")
    ap.add_argument("--debug", action="store_true",
                    help="print triage + crop + rows for each doc; do NOT write to the DB")
    ap.add_argument("--self-test", action="store_true",
                    help="run the repair-conservatism unit self-test and exit")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    targets = fetch_targets(symbols, args.force or args.debug, args.limit)
    print(f"PDF extraction targets: {len(targets)} documents")

    ok = failed = no_text = total_rows = 0
    by_class = {"text": 0, "poor": 0, "none": 0}
    flagged: list[tuple[str, list[str]]] = []
    all_marker_repairs: list[tuple[str, str, str]] = []
    all_leadverb_repairs: list[tuple[str, str, str]] = []
    all_french: list[tuple[str, int]] = []
    all_layout_flags: list[tuple[str, list[str]]] = []

    for start in range(0, len(targets), BATCH_DOCS):
        chunk = targets[start:start + BATCH_DOCS]
        conn = None if args.debug else get_conn()
        try:
            for symbol, lang, archive_path in chunk:
                path = ARCHIVE_ROOT / archive_path
                try:
                    if not path.exists():
                        raise FileNotFoundError(f"archive file missing: {archive_path}")
                    res = extract_pdf(path, symbol)
                    by_class[res.triage.klass] += 1
                    if args.debug:
                        _debug_dump(symbol, res)
                        continue
                    if res.triage.klass == "none":
                        upsert_document_file(
                            conn, symbol, lang, status="no_text_layer",
                            error=f"no usable text layer ({res.triage.summary()})")
                        conn.commit()
                        no_text += 1
                        continue
                    for b, a in res.marker_repairs:
                        all_marker_repairs.append((symbol, b, a))
                    for b, a in res.leadverb_repairs:
                        all_leadverb_repairs.append((symbol, b, a))
                    if res.french_dropped:
                        all_french.append((symbol, res.french_dropped))
                    if res.layout_flags:
                        all_layout_flags.append((symbol, res.layout_flags))
                    write_document(conn, symbol, lang, res.rows)
                    err = None
                    if res.crop and res.crop.flags:
                        err = "; ".join(res.crop.flags)[:400]
                        flagged.append((symbol, res.crop.flags))
                    note = res.summary_note()
                    upsert_document_file(conn, symbol, lang, status="extracted",
                                         error=(f"{note} | {err}" if err else note)[:500])
                    conn.commit()
                    ok += 1
                    total_rows += len(res.rows)
                except Exception as exc:
                    if conn is not None:
                        conn.rollback()
                        upsert_document_file(
                            conn, symbol, lang, status="extract_failed",
                            error=f"{type(exc).__name__}: {exc}"[:500])
                        conn.commit()
                    failed += 1
                    print(f"  ! {symbol}: {type(exc).__name__}: {exc}")
        finally:
            if conn is not None:
                conn.close()
        if not args.debug:
            done = start + len(chunk)
            print(f"  extracted {done}/{len(targets)} ok={ok} no_text={no_text} "
                  f"failed={failed} rows={total_rows}")

    print(f"\nTriage: text={by_class['text']} poor={by_class['poor']} none={by_class['none']}")
    if flagged:
        print(f"Crop-flagged {len(flagged)} doc(s):")
        for sym, fl in flagged:
            print(f"  {sym}: {fl}")
    print(f"\nOCR marker repairs fired: {len(all_marker_repairs)} (audit)")
    for sym, b, a in all_marker_repairs:
        print(f"  {sym:<22} {b!r} -> {a!r}")
    print(f"Lead-verb repairs fired: {len(all_leadverb_repairs)} (audit)")
    for sym, b, a in all_leadverb_repairs:
        print(f"  {sym:<22} {b!r} -> {a!r}")
    print(f"Facing-language regions dropped in {len(all_french)} doc(s):")
    for sym, n in all_french:
        print(f"  {sym:<22} dropped {n} line(s)")
    if all_layout_flags:
        print(f"Layout flags on {len(all_layout_flags)} doc(s):")
        for sym, fl in all_layout_flags[:40]:
            print(f"  {sym:<22} {','.join(fl)}")
    print(f"Done. ok={ok} no_text={no_text} failed={failed} rows_written={total_rows}")
    return 0 if failed == 0 else 1


def _debug_dump(symbol: str, res: ExtractResult) -> None:
    print(f"\n===== {symbol}  [{res.triage.summary()}]  cols={res.n_columns} =====")
    if res.dropped_headers:
        print(f"  dropped headers/footers ({len(res.dropped_headers)}):")
        for d in res.dropped_headers[:10]:
            print(f"     - {d}")
    if res.crop:
        print(f"  crop: start={res.crop.start} end={res.crop.end} "
              f"anchor_found={res.crop.anchor_found} flags={res.crop.flags}")
    if res.layout_flags:
        print(f"  layout flags: {res.layout_flags}")
    if res.hyphen:
        print(f"  hyphen: {res.hyphen}")
    if res.french_dropped:
        print(f"  facing-language lines dropped: {res.french_dropped}")
        for t in res.foreign_dropped_lines[:8]:
            print(f"     - {t[:80]}")
    for b, a in res.marker_repairs:
        print(f"  marker repair: {b!r} -> {a!r}")
    for b, a in res.leadverb_repairs:
        print(f"  lead-verb repair: {b!r} -> {a!r}")
    for r in res.rows:
        if r["kind"] == "empty":
            print("     ·")
            continue
        p = r["props"] or {}
        tag = "".join(c for c, k in (("B", "bold"), ("I", "italic"),
                     ("C", "alignment"), ("U", "all_caps")) if p.get(k))
        print(f"   [{r['position']:>3}] {tag:<4} {r['text'][:96]}")


_MK_PARA_Y = [0.0]


def _mk_para(text: str, x0: float = 80.0, y0: float | None = None) -> Para:
    """A one-line paragraph with realistic stacked geometry (13pt pitch)."""
    if y0 is None:
        y0 = _MK_PARA_Y[0]
        _MK_PARA_Y[0] += 13.0
    ln = Line(text=text, x0=x0, x1=x0 + 200, y0=y0, y1=y0 + 11.0, size=9.5,
              bold=False, italic=False, lead_italic_text=None, page=0)
    return Para([ln])


def _mk_line(text: str, x0: float, y0: float, width: float | None = None,
             page: int = 0, size: float = 9.5) -> Line:
    """A synthetic text line. Width defaults to ~5pt per character at 9.5pt,
    which is what these volumes actually set."""
    w = width if width is not None else 5.0 * len(text)
    return Line(text=text, x0=x0, x1=x0 + w, y0=y0, y1=y0 + 11.0, size=size,
                bold=False, italic=False, lead_italic_text=None, page=page)


def _mk_column(texts: list[str], x0: float, width: float, y0: float = 47.0,
               pitch: float = 13.0, page: int = 0) -> list[Line]:
    return [_mk_line(t, x0, y0 + i * pitch, width, page)
            for i, t in enumerate(texts)]


_LEFT_COL = [
    "Convinced that all peoples have an inalienable right",
    "to complete freedom, the exercise of their sovereignty",
    "and the integrity of their national territory,",
    "Solemnly proclaims the necessity of bringing to a",
    "speedy and unconditional end colonialism in all its forms",
    "and manifestations ;",
    "1. The subjection of peoples to alien subjugation,",
    "domination and exploitation constitutes a denial of",
    "fundamental human rights, is contrary to the Charter",
    "of the United Nations and is an impediment to the",
    "promotion of world peace and co-operation.",
    "2. All peoples have the right to self-determination;",
]
_RIGHT_COL = [
    "any distinction as to race, creed or colour, in order to",
    "enable them to enjoy complete independence and",
    "freedom.",
    "6. Any attempt aimed at the partial or total dis-",
    "ruption of the national unity and the territorial in-",
    "tegrity of a country is incompatible with the purposes",
    "and principles of the Charter of the United Nations.",
    "7. All States shall observe faithfully and strictly the",
    "provisions of the Charter of the United Nations, the",
    "Universal Declaration of Human Rights and the",
    "present Declaration on the basis of equality,",
    "non-interference in the internal affairs of all States.",
]


def _paragraph_texts(groups: list[list[Line]],
                     hyph: HyphenPolicy | None = None) -> list[str]:
    paras, _, _ = reconstruct_paragraphs(groups, None, hyph)
    return [p.text for p in paras]


def _self_test() -> int:
    """Quantify repair conservatism with adversarial cases. Exit 0 iff all pass."""
    fails: list[str] = []

    # Case 1 — a standalone 'I.' WITHOUT sequence confirmation must NOT be
    # rewritten (no following '2.' numeric marker at the same indent).
    paras = [_mk_para("The Security Council,"),
             _mk_para("I."),
             _mk_para("Decides to do the thing.")]
    log: list[tuple[str, str]] = []
    _repair_ocr_markers(paras, log)
    if log or paras[1].text != "I.":
        fails.append(f"case1: unconfirmed 'I.' was rewritten -> {paras[1].text!r} log={log}")

    # Case 1b — a standalone 'I.' WITH sequence confirmation (next markers 2., 3.)
    # MUST be rewritten to '1.'.
    paras = [_mk_para("The Security Council,"),
             _mk_para("I."),
             _mk_para("Demands withdrawal.", x0=100.0),
             _mk_para("2. Demands observance."),
             _mk_para("3. Calls upon all parties.")]
    log = []
    _repair_ocr_markers(paras, log)
    if paras[1].text != "1." or not log:
        fails.append(f"case1b: confirmed 'I.' was NOT repaired -> {paras[1].text!r}")

    # Case 2 — 'Recallinx' in BODY text position (not first word) must NOT be
    # touched by the lead-verb repair.
    body = "the Council Recallinx its earlier decisions on the matter"
    if repair_lead_verb(body) is not None:
        fails.append(f"case2: body-position 'Recallinx' was repaired -> {repair_lead_verb(body)!r}")
    # ... but as a FIRST word it should repair (edit distance 1).
    if repair_lead_verb("Recallinx its resolutions 425 (1978)") != "Recalling its resolutions 425 (1978)":
        fails.append("case2b: first-word 'Recallinx' was NOT repaired")

    # Case 3 — a genuine roman heading 'II.' between operatives must SURVIVE (it is
    # two characters; only single-glyph confusables are candidates, and its
    # neighbour is 'III.', not a confirming numeric marker).
    paras = [_mk_para("1. Decides the first thing."),
             _mk_para("II."),
             _mk_para("2. Decides the second thing."),
             _mk_para("III.")]
    log = []
    _repair_ocr_markers(paras, log)
    if paras[1].text != "II." or paras[3].text != "III.":
        fails.append(f"case3: roman heading mutated -> {paras[1].text!r},{paras[3].text!r}")

    n_cases = 4 + _layout_controls(fails) + _hyphen_controls(fails) \
        + _language_controls(fails) + _real_document_control(fails) \
        + _crop_controls(fails) + _performance_control(fails) \
        + _marker_controls(fails)

    for name, msg in ([("FAIL", m) for m in fails]):
        print(f"  {name}: {msg}")
    if fails:
        print(f"self-test: {len(fails)} of {n_cases} FAILED")
        return 1
    print(f"self-test: all {n_cases} adversarial cases passed")
    return 0


# ---------------------------------------------------------------------------
# Negative controls — each is an input the code is PROVEN to handle, including
# inputs that are deliberately pathological. A control that has never been shown
# to fail is absent, not passing, so every one of these was run against the
# pre-fix code first and reproduced the defect it now forbids.
# ---------------------------------------------------------------------------

def _layout_controls(fails: list[str]) -> int:
    """Column geometry: 2-column, 3-column, header-over-columns, table-over-
    columns, a single-column hanging-marker page that must NOT be split, and a
    page whose columns cannot be separated at all — where the requirement is
    that text from two columns is never merged into one paragraph."""
    W = 622.0

    # (1) Plain two-column page -> exactly two groups, left read first, and no
    #     paragraph mixing the columns. Pre-fix this produced
    #     'inalienable right any distinction as to race'.
    lines = _mk_column(_LEFT_COL, 62, 236) + _mk_column(_RIGHT_COL, 322, 226)
    flags: set[str] = set()
    groups = split_columns(lines, W, flags)
    if len(groups) != 2:
        fails.append(f"layout1: two-column page split into {len(groups)} groups")
    elif groups[0][0].text != _LEFT_COL[0] or groups[1][0].text != _RIGHT_COL[0]:
        fails.append("layout1: two-column page not read left column first")
    texts = _paragraph_texts(groups)
    if any("inalienable right any distinction" in t for t in texts):
        fails.append("layout1: THE A/RES/1514(XV) FABRICATION WAS REPRODUCED")

    # (2) Three columns -> three groups in left-to-right order.
    cols = [[f"column {c} line {i} of running text here" for i in range(8)]
            for c in range(3)]
    lines = (_mk_column(cols[0], 60, 150) + _mk_column(cols[1], 235, 150)
             + _mk_column(cols[2], 410, 150))
    groups = split_columns(lines, W, set())
    if len(groups) != 3:
        fails.append(f"layout2: three-column page split into {len(groups)} groups")
    elif [g[0].text for g in groups] != [c[0] for c in cols]:
        fails.append(f"layout2: three columns out of order: {[g[0].text for g in groups]}")

    # (3) Full-width heading above two columns -> [heading][left][right].
    head = [_mk_line("Resolutions adopted on the reports of the First Committee",
                     100, 20, 420)]
    lines = head + _mk_column(_LEFT_COL, 62, 236, y0=47) \
        + _mk_column(_RIGHT_COL, 322, 226, y0=47)
    groups = split_columns(lines, W, set())
    if len(groups) != 3 or groups[0][0].text != head[0].text:
        fails.append(f"layout3: header over two columns gave {len(groups)} groups, "
                     f"first={groups[0][0].text[:30]!r}")

    # (4) A wide table ABOVE two text columns: no single divider fits both, so
    #     the page must be cut horizontally first. This is A/RES/32/1, which
    #     pre-fix stored 50 characters of woven text.
    table: list[Line] = []
    for r in range(6):
        y = 20 + r * 13
        table += [_mk_line(f"32/{r}", 70, y, 30), _mk_line("Title of the resolution", 120, y, 260),
                  _mk_line("14 December 1977", 450, y, 90)]
    lines = table + _mk_column(_LEFT_COL, 62, 236, y0=140) \
        + _mk_column(_RIGHT_COL, 322, 226, y0=140)
    groups = split_columns(lines, W, set())
    texts = _paragraph_texts(groups)
    if any("inalienable right any distinction" in t for t in texts):
        fails.append("layout4: table-over-columns page wove the two text columns")

    # (5) A single-column page with hanging markers must NOT be split: the
    #     marker gutter is not a column gutter.
    lines = []
    for i in range(14):
        y = 50 + i * 13
        if i % 4 == 0:
            lines.append(_mk_line(f"{i//4 + 1}.", 100, y, 12))
            lines.append(_mk_line("Requests the Secretary-General to report on the matter", 130, y, 400))
        else:
            lines.append(_mk_line("continuation of the operative paragraph running full width", 72, y, 458))
    groups = split_columns(lines, W, set())
    if len(groups) != 1:
        fails.append(f"layout5: hanging-marker single column was split into {len(groups)}")

    # (6b) A TABLE: cells on one row, wide apart. Each cell is its own block of
    #      text; welding them produced
    #      'BUDGET APPROPRIATIONS FOR 1972 TOTAL, PART I TOTAL, PART II'.
    rows_t: list[Line] = []
    for r in range(6):
        y = 40 + r * 14
        rows_t += [_mk_line(f"Section {r}", 70, y, 90),
                   _mk_line(f"{r}00 000", 300, y, 60),
                   _mk_line(f"{r}50 000", 460, y, 60)]
    texts = _paragraph_texts([_row_sort(rows_t)])
    welded = [t for t in texts if "Section" in t and t.count("000") > 1]
    if welded:
        fails.append(f"layout6b: table cells welded into one paragraph: {welded[0][:80]!r}")

    # (6c) A hanging marker whose OCR box starts BELOW its own text line must
    #      still be read first: '(a)' landed inside the word it labels
    #      ('the Elimi- (a) nation of Discrimination').
    marker_page = [
        _mk_line("(a)", 62, 103, 14),
        _mk_line("The provisions of the Declaration on the Elimi-", 90, 100, 200),
        _mk_line("nation of Discrimination against Women;", 90, 113, 200),
        _mk_line("(b)", 62, 129, 14),
        _mk_line("The provisions of the International Covenant on", 90, 126, 200),
        _mk_line("Economic, Social and Cultural Rights;", 90, 139, 200),
    ]
    texts = _paragraph_texts(split_columns(marker_page, W, set()))
    if any("Elimi- (a)" in t or "Elimi-(a)" in t or "Eliminationof" in t for t in texts):
        fails.append(f"layout6c: hanging marker read inside its own text: {texts!r}")
    if not any(t.startswith("(a)") for t in texts):
        fails.append(f"layout6c: the marker '(a)' did not lead its paragraph: {texts!r}")

    # (6d) STAGGERED two columns that cannot be split (bridged on every row, and
    #      the two columns' lines never share a row, so the same-row rule cannot
    #      help): the boundary must still be remembered, or the columns weave.
    left_run = [f"the Council has considered the situation number {i} and has"
                for i in range(12)]
    right_run = [f"whereas the Committee established under paragraph {i} shall"
                 for i in range(12)]
    stag: list[Line] = []
    for i in range(12):
        stag.append(_mk_line(left_run[i], 62, 47 + i * 26, 236))
        stag.append(_mk_line(right_run[i], 322, 60 + i * 26, 226))
    for k in range(0, 12, 2):
        stag.append(_mk_line("a bridging line that spans the whole page width",
                             62, 47 + k * 26 + 13, 486))
    texts = _paragraph_texts(split_columns(stag, W, set()))
    bad = [t for t in texts
           if any(l in t for l in left_run[:3]) and any(r in t for r in right_run[:3])]
    if bad:
        fails.append(f"layout6d: staggered unsplittable columns merged: {bad[0][:90]!r}")

    # (7) PATHOLOGICAL: two columns whose gutter is bridged on every third row,
    #     so no clean split exists. The extractor may fragment, but it must NEVER
    #     join the two columns into one paragraph.
    lines = _mk_column(_LEFT_COL, 62, 236) + _mk_column(_RIGHT_COL, 322, 226)
    for k in range(0, 12, 3):
        lines.append(_mk_line("a bridging line that spans the whole page width",
                              62, 47 + k * 13 + 4, 486))
    flags = set()
    groups = split_columns(lines, W, flags)
    texts = _paragraph_texts(groups)
    bad = [t for t in texts
           if any(l in t for l in _LEFT_COL[:3]) and any(r in t for r in _RIGHT_COL[:3])]
    if bad:
        fails.append(f"layout7: unsplittable page merged both columns: {bad[0][:90]!r}")
    return 9


def _marker_controls(fails: list[str]) -> int:
    """A bare number is only a paragraph marker if it is physically attached to
    the line it labels. S/RES/661(1990) stored an invented operative paragraph
    '19.' because the page number at the foot of page 1 was merged into the
    first line of page 2."""
    page_num = Para([_mk_line("19", 284, 738, 12, page=0)])
    body = Para([_mk_line("nationals or in their territories which promote or are "
                          "calculated to promote such sale or supply;", 62, 58, 240,
                          page=1)])
    out = _merge_hanging_markers([page_num, body])
    if len(out) != 2 or out[1].text.startswith("19."):
        fails.append(f"marker1: a page number was welded onto another page's line: "
                     f"{out[-1].text[:70]!r}")
    # …while a genuine hanging marker on the same row still merges.
    marker = Para([_mk_line("4.", 62, 100, 12)])
    clause = Para([_mk_line("Decides to remain seized of the matter.", 90, 100, 200)])
    out = _merge_hanging_markers([marker, clause])
    if len(out) != 1 or not out[0].text.startswith("4. Decides"):
        fails.append(f"marker2: a genuine hanging marker stopped merging: "
                     f"{[p.text for p in out]!r}")
    return 2


def _hyphen_controls(fails: list[str]) -> int:
    """A line-break hyphen must survive in a compound and vanish in a broken
    word, decided on evidence rather than on the next line's case."""
    def joined(left: str, right: str, doc_lines: list[str] | None = None) -> str:
        pol = HyphenPolicy(pairs={"peace": {}} and {})
        if doc_lines:
            pol.observe([_mk_line(t, 62, 47 + 13 * i, 236) for i, t in enumerate(doc_lines)])
        return _join_lines([_mk_line(left, 62, 47, 236), _mk_line(right, 62, 60, 236)], pol)

    # (1) The exact damage the audit proved: 'self-' + 'determination'.
    if joined("the right to self-", "determination;") != "the right to self-determination;":
        fails.append(f"hyphen1: self-determination destroyed -> "
                     f"{joined('the right to self-', 'determination;')!r}")
    # (2) A genuine syllable break must still close up.
    if joined("the inter-", "national community") != "the international community":
        fails.append(f"hyphen2: broken word not rejoined -> "
                     f"{joined('the inter-', 'national community')!r}")
    # (3) The document's own spelling decides an era-specific compound…
    got = joined("economic co-", "operation among States",
                 ["international economic co-operation is essential"])
    if got != "economic co-operation among States":
        fails.append(f"hyphen3: document evidence ignored -> {got!r}")
    # (4) …in both directions.
    got = joined("economic co-", "operation among States",
                 ["international economic cooperation is essential"])
    if got != "economic cooperation among States":
        fails.append(f"hyphen4: document evidence ignored (join) -> {got!r}")
    # (5) The corpus lexicon decides when the document is silent.
    pol = HyphenPolicy(pairs={("peace", "keeping"): (500, 3)})
    got = _join_lines([_mk_line("United Nations peace-", 62, 47, 236),
                       _mk_line("keeping operations", 62, 60, 236)], pol)
    if got != "United Nations peace-keeping operations":
        fails.append(f"hyphen5: corpus lexicon ignored -> {got!r}")
    pol = HyphenPolicy(pairs={("peace", "keeping"): (3, 500)})
    got = _join_lines([_mk_line("United Nations peace-", 62, 47, 236),
                       _mk_line("keeping operations", 62, 60, 236)], pol)
    if got != "United Nations peacekeeping operations":
        fails.append(f"hyphen6: corpus lexicon ignored (join) -> {got!r}")
    return 6


def _language_controls(fails: list[str]) -> int:
    """The facing-language filter must delete a French COLUMN and must not touch
    an English column that happens to name French-titled organizations — the
    defect that removed 1,003 lines from 39 monolingual documents."""
    english = [
        "The Economic and Social Council, recalling its resolution 1996/31",
        "of 25 July 1996 on the relationship between the United Nations and",
        "non-governmental organizations, decides to grant consultative status",
        "to the following organizations, on the recommendation of the Committee",
        "Association pour le Deploiement Rural, la Protection de",
        "l'Environnement et l'Artisanat (DERPREA - Cameroon)",
        "Comite international pour le respect des droits de l'homme",
        "Organisation pour la promotion de la femme et de l'enfant",
        "Union des associations pour le developpement rural du Sahel",
        "and decides further to review the list at its next session,",
        "requests the Secretary-General to report on the implementation",
        "of the present decision to the Council at its substantive session",
    ]
    french = [
        "Le Conseil economique et social, rappelant sa resolution 1996/31 du",
        "25 juillet 1996 relative aux relations aux fins de consultations entre",
        "l'Organisation des Nations Unies et les organisations non",
        "gouvernementales, decide d'admettre les organisations ci-apres au",
        "statut consultatif, sur la recommandation du comite charge des",
        "organisations non gouvernementales, et prie le secretaire general",
        "de presenter un rapport sur l'application de la presente decision",
        "au conseil lors de sa session de fond de l'annee prochaine,",
        "et decide egalement d'examiner la liste des organisations dotees",
        "du statut consultatif lors de sa prochaine session pleniere,",
    ]
    en_group = _mk_column(english, 62, 236)
    fr_group = _mk_column(french, 322, 226)

    # (1) Facing English/French columns: the French column goes, English stays.
    flags: set[str] = set()
    kept, dropped = drop_foreign_regions([en_group, fr_group], flags)
    if len(kept) != 1 or kept[0] is not en_group:
        fails.append(f"lang1: facing French column not dropped (kept {len(kept)} regions)")
    if not any(t.startswith("Le Conseil") for t in dropped):
        fails.append("lang1: French column text not recorded as dropped")

    # (2) The SAME English column alone (a monolingual ECOSOC NGO decision):
    #     nothing may be deleted, though it names five French-titled NGOs.
    flags = set()
    kept, dropped = drop_foreign_regions([en_group], flags)
    if dropped or len(kept) != 1:
        fails.append(f"lang2: monolingual English document lost {len(dropped)} lines "
                     f"({dropped[:1]})")
    # …and the old per-line predicate is shown to fire on exactly those lines,
    # so control (2) is proven to be capable of failing.
    if sum(1 for t in english if french_line(t)) < 3:
        fails.append("lang2: control is toothless — the per-line predicate does "
                     "not fire on the NGO names it was proven to delete")

    # (3) An all-French document is kept whole and flagged, never emptied.
    flags = set()
    kept, dropped = drop_foreign_regions([fr_group], flags)
    if dropped or "no_english_region_kept_whole" not in flags:
        fails.append(f"lang3: French-only document was emptied ({len(dropped)} lines)")
    return 3


def _performance_control(fails: list[str]) -> int:
    """A real page whose geometry made the layout analysis explode.

    A/RES/39/246 ran 220,000 candidate evaluations and never finished, because
    the column-likeness test recursed into candidate generation. Bounded now,
    and this control is what will notice if it ever un-bounds."""
    path = ARCHIVE_ROOT / "original" / "A_RES_39_246.pdf"
    if not path.exists():
        return 0
    import time
    t0 = time.time()
    extract_pdf(path, "A/RES/39/246")
    dt = time.time() - t0
    if dt > 8.0:
        fails.append(f"perf: A/RES/39/246 took {dt:.1f}s (bar 8s) — the layout "
                     f"analysis is superlinear again")
    return 1


def _crop_controls(fails: list[str]) -> int:
    """The crop must cover the document's own printed extent — and only it.

    Both directions have cost real text: `ADOPTED_RE` as case-insensitive
    `^adopted` cut 145 documents to ~21% of source (A/RES/701(VII) was stored as
    47 characters), and an unmatched heading made A/RES/1005(ES-II) serve
    resolutions 1004, 1007 and 1008 as its own."""
    # (a) UNIT control, no archive needed: a preambular clause that merely uses
    #     the word "adopted", and a resolution that is the LAST in its file (so
    #     the adoption record, not a next heading, ends the crop).
    _MK_PARA_Y[0] = 0.0
    paras = [_mk_para("701 (VII). Korea: reports of the United Nations Agent General"),
             _mk_para("The General Assembly,"),
             _mk_para("Recalling the declaration on equality of opportunity"),
             _mk_para("adopted at its sixtieth session by the International Labour "
                      "Conference,"),
             _mk_para("1. Reaffirms the objective of the United Nations to provide relief;"),
             _mk_para("2. Requests the Secretary-General to report thereon;"),
             _mk_para("410th plenary meeting,"),
             _mk_para("1 December 1952.")]
    crop = crop_to_target(paras, "A/RES/701(VII)")
    kept = " ".join(p.text for p in paras[crop.start:crop.end])
    if "Reaffirms the objective" not in kept or "Requests the Secretary-General" not in kept:
        fails.append(f"crop-unit: the crop ended before the operative paragraphs "
                     f"(kept {crop.start}:{crop.end})")
    if "410th plenary meeting," not in kept:
        fails.append("crop-unit: the adoption record was dropped")

    # (b) UNIT control for the other direction: a second resolution in the same
    #     file must never be carried, whatever its session is printed as.
    _MK_PARA_Y[0] = 0.0
    paras = [_mk_para("Resolution 1005 (ES-II)"),
             _mk_para("The General Assembly,"),
             _mk_para("Noting with deep concern that the provisions of its resolution "
                      "1004 (ES-II) of 4 November 1956 have not been carried out,"),
             _mk_para("1. Calls upon the Government of the Union of Soviet Socialist "
                      "Republics to desist forthwith;"),
             _mk_para("Resolution 1007 (ES-II)"),
             _mk_para("The General Assembly,"),
             _mk_para("Considering the extreme suffering to which the Hungarian people "
                      "are subjected,")]
    crop = crop_to_target(paras, "A/RES/1005(ES-II)")
    kept = " ".join(p.text for p in paras[crop.start:crop.end])
    if "extreme suffering" in kept or "Resolution 1007" in kept:
        fails.append("crop-unit: the crop ran past this resolution into the next one")
    if "Calls upon the Government" not in kept:
        fails.append("crop-unit: the crop lost this resolution's own operative text")

    cases = [
        # symbol, min chars, phrase that must be present, phrase that must NOT be
        ("A/RES/701(VII)", 800, "Reaffirms the objective of the United Nations", None),
        ("A/RES/46/68", 10000, "American Samoa", None),
        ("A/RES/1005(ES-II)", 1500, "The General Assembly", "extreme suffering"),
        ("A/RES/357(IV)", 800, None, None),
    ]
    ran = 2
    for symbol, min_chars, phrase, forbidden in cases:
        path = ARCHIVE_ROOT / "original" / (sanitize_symbol(symbol) + ".pdf")
        if not path.exists():
            continue
        ran += 1
        res = extract_pdf(path, symbol)
        text = " ".join(r["text"] for r in res.rows)
        if len(text) < min_chars:
            fails.append(f"crop[{symbol}]: {len(text)} chars stored, expected "
                         f">= {min_chars} (crop ended early)")
        if phrase and phrase not in text:
            fails.append(f"crop[{symbol}]: {phrase!r} missing from the stored text")
        if forbidden and forbidden in text:
            fails.append(f"crop[{symbol}]: carries the NEXT resolution's text "
                         f"({forbidden!r})")
        # …and no row may print ANOTHER resolution's heading.
        _, target_num = _target_matchers(symbol)
        foreign = [r["text"][:60] for r in res.rows
                   if (hn := _heading_number(r["text"] or "")) is not None
                   and hn != target_num]
        if foreign:
            fails.append(f"crop[{symbol}]: carries another resolution's heading "
                         f"{foreign[0]!r}")
    if not ran:
        print("  SKIPPED: crop controls need the archive (not mounted)")
    return ran


def _real_document_control(fails: list[str]) -> int:
    """Run the real A/RES/1514(XV) scan if the archive is mounted: its preamble
    must come out verbatim and its known fabrication must be absent. A missing
    archive is reported as SKIPPED — silence must never read as success."""
    path = ARCHIVE_ROOT / "original" / "A_RES_1514_XV_.pdf"
    if not path.exists():
        print(f"  SKIPPED: real-document control needs {path} (archive not mounted)")
        return 0
    res = extract_pdf(path, "A/RES/1514(XV)")
    body = " ".join(r["text"] for r in res.rows if r["kind"] == "paragraph")
    want = ("Convinced that all peoples have an inalienable right to complete "
            "freedom, the exercise of their sovereignty and the integrity of "
            "their national territory,")
    if want not in body:
        fails.append("real1: A/RES/1514(XV) preamble is not verbatim in the output")
    for bad in ("inalienable right any distinction", "disspeedy", "inand"):
        if bad in body:
            fails.append(f"real1: A/RES/1514(XV) still contains {bad!r}")
    if "self-determination" not in body:
        fails.append("real2: A/RES/1514(XV) lost the hyphen in self-determination")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
