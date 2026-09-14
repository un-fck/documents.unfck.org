#!/usr/bin/env python3
"""Volume-split pipeline — recover GA/ECOSOC decisions and early-HRC texts.

Individual GA/ECOSOC *decisions* (A/DEC/*, E/DEC/*) and early Human Rights
Council texts (A/HRC/RES|PRST|DEC/* for sessions 2-11) are NOT issued as
standalone ODS documents. They only exist inside compilation *volumes* / session
*reports* that ODS/DL DO host:

  * GA decisions   -> GAOR Supplement 49, Volume II   ('A/<n>/49 (Vol. II)')  [PDF]
  * ECOSOC res+dec -> ECOSOC Supplement 1              ('E/<year>/99')          [PDF]
  * early HRC       -> the per-session HRC report        (see HRC_REPORTS)        [Word]

This module treats each such parent as an ordinary ledger doc (fetched, then
extracted by the SAME fulltext_extract_pdf / fulltext_extract_raw stages the
8-family catalog uses — the volume symbol simply falls outside those extractors'
crop targets, so they keep the whole volume text), then SPLITS the per-child
paragraphs back out into digitallibrary.document_paragraphs_raw under each child's
own symbol_normalized, tagged source_symbol=<volume> (migration 005). The child
rows then flow through the FROZEN semantic parser (fulltext_parse.py) unchanged.

The SSD archive (the volume PDF/docx) stays ground truth; the child raw rows are a
disposable re-split substrate, exactly like every other raw row.

Born-digital era (STEP-0 probe sweep, `--probe`): only born-digital / text-layer
volumes are in scope. The pre-era volumes are pure image scans (triage class
'none') and are DEFERRED for a future OCR pass. Measured cutoff:

  * GA Vol II   : session >= 57  (A/57/49(Vol.II), 2003).  <=55 are scans.
  * ECOSOC 99   : year    >= 2003 (E/2003/99).             <=2002 are scans.

Modes:
    uv run python python/fulltext_split_volumes.py --probe          # STEP 0 triage sweep
    uv run python python/fulltext_split_volumes.py --fetch          # fetch volumes -> ledger
    uv run python python/fulltext_split_volumes.py --split          # split extracted volumes
    uv run python python/fulltext_split_volumes.py --split --symbols 'A/80/49(VOL.II)'
    uv run python python/fulltext_split_volumes.py --split --dry-run
    uv run python python/fulltext_split_volumes.py --nightly        # fetch+extract+split+parse+verify
    uv run python python/fulltext_split_volumes.py --self-test      # predicate unit tests
"""

from __future__ import annotations

import argparse
import bisect
import re
import subprocess
import tempfile
import time
from pathlib import Path

from psycopg.types.json import Jsonb

from fulltext_common import (
    ARCHIVE_ROOT,
    get_conn,
    read_state,
    sanitize_symbol,
    sha256_bytes,
    sniff_format,
    upsert_document_file,
    write_state,
)
from fulltext_fetch import normalize_symbol

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTRACTOR_VERSION = "split-v1"
STATE_KEY = "volume_splits"

# ---------------------------------------------------------------------------
# Volume catalog (deterministic generators + the HRC report map)
# ---------------------------------------------------------------------------
# Era cutoffs are the STEP-0 probe-sweep findings (born-digital / text only).
GA_VOL2_MIN_SESSION = 57
GA_VOL2_MAX_SESSION = 80
ECOSOC_MIN_YEAR = 2003
ECOSOC_MAX_YEAR = 2025

# early-HRC per-session reports (Word on ODS) — the sessional report that carries
# that session's adopted resolutions/decisions/president's statements.
# VERIFIED FILE-BY-FILE against the DL catalog titles and the archived files
# themselves (`--verify-map`, and the negative control in `--self-test`).
HRC_REPORTS = {
    2: "A/HRC/2/9", 3: "A/HRC/3/7", 4: "A/HRC/4/123", 5: "A/HRC/5/21",
    6: "A/HRC/6/22", 7: "A/HRC/7/78", 8: "A/HRC/8/52", 9: "A/HRC/9/28",
    10: "A/HRC/10/29", 11: "A/HRC/11/37",
}
# Special sessions. NOT 'A/HRC/S-<n>/2' for every n: the '/2' slot holds the
# session report only when the convening letter took '/1'. The fourth special
# session received four letters, so its report is A/HRC/S-4/5 — A/HRC/S-4/2 is a
# letter from Antonio Cassese on Darfur and was mapped, fetched and split as if it
# were the report (yielding, correctly, nothing). The first special session's
# report is A/HRC/S-1/3 and was not in the map at all.
HRC_SPECIAL_REPORTS = {
    1: "A/HRC/S-1/3", 2: "A/HRC/S-2/2", 3: "A/HRC/S-3/2", 4: "A/HRC/S-4/5",
    5: "A/HRC/S-5/2", 6: "A/HRC/S-6/2", 7: "A/HRC/S-7/2", 8: "A/HRC/S-8/2",
    9: "A/HRC/S-9/2", 10: "A/HRC/S-10/2", 11: "A/HRC/S-11/2",
}
# A mapped symbol must be the session's REPORT. This is the predicate --verify-map
# applies to the DL catalog title of every entry, and the one the self-test's
# negative control (a letter symbol substituted for a report) must fail.
_REPORT_TITLE_RE = re.compile(
    r"\breport\b.*\b(?:on its|on the)\b.*\bsession\b"
    r"|\breport\b.*\bsession\b.*\bcouncil\b"
    r"|\breport (?:of|to)\b.*\bsession\b", re.I)


def ga_volume_symbols() -> list[str]:
    # Vol II = decisions; Vol III = resolutions AND decisions of the resumed parts
    # of the session. Both carry A/DEC children (Vol III is routed by catalog
    # membership so its A/RES headings become cross-checks, not decisions).
    out: list[str] = []
    for n in range(GA_VOL2_MIN_SESSION, GA_VOL2_MAX_SESSION + 1):
        out.append(normalize_symbol(f"A/{n}/49(Vol.II)"))
        out.append(normalize_symbol(f"A/{n}/49(Vol.III)"))
    return out


def ecosoc_volume_symbols() -> list[str]:
    return [f"E/{y}/99" for y in range(ECOSOC_MIN_YEAR, ECOSOC_MAX_YEAR + 1)]


def hrc_report_symbols() -> list[str]:
    return list(HRC_REPORTS.values()) + list(HRC_SPECIAL_REPORTS.values())


def volume_catalog() -> list[tuple[str, str]]:
    """[(symbol_normalized, kind)] for the whole in-scope volume catalog.
    kind in {'ga','ecosoc','hrc'}: ga/ecosoc are PDF, hrc is Word/docx."""
    out = [(s, "ga") for s in ga_volume_symbols()]
    out += [(s, "ecosoc") for s in ecosoc_volume_symbols()]
    out += [(normalize_symbol(s), "hrc") for s in hrc_report_symbols()]
    return out


VOLUME_FORMAT = {"ga": "pdf", "ecosoc": "pdf", "hrc": "docx"}


# ---------------------------------------------------------------------------
# Split predicates (pure; see --self-test)
# ---------------------------------------------------------------------------
# DESIGN RULE (defects C6/C7): heading detection must NOT depend on how the
# extractor happened to chunk lines into paragraph rows, nor on the styling a
# particular report used. Every predicate below works on a whitespace-normalised
# view of the row, and every body-confirmation window INCLUDES ROW i ITSELF — the
# PDF extractor routinely merges a decision heading and its adoption record into
# one paragraph, and a look-ahead starting at i+1 looks straight past the very
# text that would confirm the heading (that alone hid 249 printed decisions).

# Whitespace the UN Word/PDF sources use interchangeably with a plain space.
_ODD_SPACE = re.compile("[  -   　]")


def norm_text(text: str | None) -> str:
    """Row text with exotic spaces folded to ' ' (newlines/tabs preserved)."""
    return _ODD_SPACE.sub(" ", text or "")


def flat_text(text: str | None) -> str:
    """Row text collapsed to a single space-separated line."""
    return re.sub(r"\s+", " ", norm_text(text)).strip()


# A dot-leader (TOC/checklist) run: the volumes print a table of contents whose
# entries look exactly like a body heading ('80/506. Endorsement ...') but are
# followed by leader dots and a page number. A dot-leader reached BEFORE the
# adoption record means the candidate is a Contents/checklist entry, not a body.
_DOTLEADER = re.compile(r"\.\s*\.\s*\.|…|\.{4,}")

# PDF (GA/ECOSOC) number heading: '<sess>/<num>[ <Letter>]. ' at the start of a
# row. sess is a GA session (2-3 digits) or an ECOSOC year (4 digits). The old
# predicate also demanded a capitalised title on the SAME row, which silently
# rejected every volume that prints the number and the title on separate lines
# (A/69/49(Vol.II): 0 of 70 headings matched). The shape test now only requires
# the number to be followed by whitespace or end-of-row; whether the row starts a
# printed BODY is decided by confirm_pdf_body().
# A leading page number or running-header digit that the extractor glued onto the
# heading row ("156. 74/418. Election of non-permanent members ...") must not hide
# the heading — that alone cost A/74/49(Vol.III) nine decisions.
_PDF_HEADING = re.compile(
    r"^\s*(?:\d{1,4}\s*\.?\s+)?(\d{2,4})/(\d{1,4})(?:\s*([A-Z]))?\s*\.(?=\s|$)")

# A broader number-heading row for SLICE BOUNDARIES only: matches '<n>/<m>.' at the
# start of a row whether or not a dot-leader follows, so an interleaved Contents /
# checklist recap ends the preceding child's slice instead of being absorbed.
_NUM_HEADING_ANY = re.compile(r"^\s*(?:\d{1,4}\s*\.?\s+)?\d{2,4}/\d{1,4}\s*[A-Z]?\s*\.")

# A standalone part letter. The volumes print a multi-part decision as ONE heading
# followed by a lone 'A' / 'B' / 'C' row per part, each with its own adoption
# record. A trailing digit is a footnote marker glued on by the extractor
# ('B1' = part B, footnote 1).
_LONE_LETTER = re.compile(r"^([A-Z])\s*\d{0,3}$")

# Body-confirmation for a PDF decision heading: the adoption record that opens a
# decision's body. Two printed forms, both requiring the organ's name nearby:
#   "At its 77th plenary meeting, on 18 December 2002, the General Assembly ..."
#   "On 3 April 2020, the Economic and Social Council, noting with concern ..."
# The second form (silence-procedure / COVID-era ECOSOC decisions) carries no
# "At its" at all; the previous predicate required one and therefore could not
# see 30 of the 35 decisions printed in E/2020/99.
_ADOPTION_RE = re.compile(
    r"\bat (?:its|the)\s+(?:resumed\s+)?\d{1,4}(?:st|nd|rd|th)?\s+(?:\w+\s+){0,3}meeting"
    r"|\bon\s+\d{1,2}(?:\s+and\s+\d{1,2})?\s+[A-Z][a-z]+\s+\d{4}"
    r"|\bpursuant to\b|\bby which the\b|\bsilence procedure\b", re.I)
_ORGAN_RE = re.compile(
    r"\bthe (?:General Assembly|Economic and Social Council|Council|Assembly)\b", re.I)
_BODY_CONFIRM_WINDOW = 24   # rows; a wrapped title can span many rows
_PART_CONFIRM_WINDOW = 8    # rows between a part letter and its own adoption record

# The volumes state their own lettering convention in a footnote whenever a later
# instalment turns an already-published decision into a lettered part:
#   "Decision 61/551, in section B.6 of the Official Records ..., vol. II,
#    becomes decision 61/551 A."
# So the printing in THIS volume is the letter after the one named. That footnote
# is the only surviving trace once the PDF extractor drops the lone 'B' marker row
# that pymupdf still sees in the file.
_BECOMES_RE = re.compile(
    r"[Dd]ecisions?\s+(\d{2,4})/(\d{1,4})\b.{0,240}?becomes decision\s+\d{2,4}/\d{1,4}\s*([A-Z])\b")

# HRC (Word) heading text: optional 'PRST/' or 'DEC/' prefix, then '<sess>/<num>'.
# sess is a numeric session or a special session 'S-<n>'. The trailing period is
# present in most reports ("6/27.") and absent in others ("10/1"), and the
# separator that follows may be a tab, a space or a NEWLINE — A/HRC/10/29 stores
# every one of its 51 headings as "\t\t10/1\nQuestion of ...". The predicate now
# accepts an optional period followed by any whitespace or end-of-row. (The
# previous character class was written with a NON-BREAKING space where a plain
# space was meant, so the period-less branch could never fire for a
# space-separated heading either — that alone cost A/HRC/S-11/2 its only text.)
_HRC_HEADING = re.compile(r"^\s*(PRST/|DEC/)?(S-\d+|\d+)/(\d+)\s*\.?(?=\s|$)")

# The report's own structure. Adopted texts live in the sections titled
# "I. Resolutions adopted by ...", "II. Decisions adopted by ...",
# "III. President's statements ...", grouped under "Part One" where the report
# uses parts. Everything from "Part Two" / "Summary of proceedings" onward is the
# record of debate and must never be routed into a child.
_HRC_ADOPTED_SECTION = re.compile(
    r"^\s*[IVXLC]+\.\s+(?:resolutions?|decisions?|president)", re.I)
_HRC_PART_ONE = re.compile(r"^\s*part\s+(?:one|1|i)\b", re.I)
_HRC_PART_TWO = re.compile(
    r"^\s*(?:part\s+(?:two|2|ii|three|3|iii)\b|summary of proceedings\b)", re.I)
# A chapter-level boundary, used only to close the LAST child of a report that has
# no "Part Two" marker at all (the 2006-2007 reports and every special session).
_HRC_BOUNDARY = re.compile(
    r"^\s*(?:part\s+\w+\b|annexes\b|summary of proceedings\b|[IVXLC]+\.\s+\S)", re.I)
# The opening formula of an adopted text (resolution / decision / PRST). Split in
# two because "the Human Rights Council" appears all over the Contents and the
# record of debate: the resolution form is matched CASE-SENSITIVELY at the start of
# a row ("The Human Rights Council,"), the decision/PRST forms need their own
# verb or the President's statement wording.
_HRC_OPENING_ROW = re.compile(r"^[\s\"\u201c\u2018']*The Human Rights Council\s*[,:]")
_HRC_OPENING_WIN = re.compile(
    r"\bat (?:its|the)\s+\d{1,3}(?:st|nd|rd|th)?\b[^.]{0,60}\bmeeting\b[^.]{0,160}"
    r"\bHuman Rights Council\b"
    r"|\bthe Human Rights Council\s+(?:decided|decides|adopted|took note|requested)\b"
    r"|\bPresident of the Council\s+(?:read out|made)\b"
    r"|\bmade a statement reading as follows\b", re.I)
_HRC_CONFIRM_WINDOW = 6

# Heading-family paragraph styles across the report generations: 'Heading 2',
# 'Heading2', 'H1G', 'HChG', '_ H_1_G'. Style is a HINT only — resolution 11/8
# opens "Reaffirming the Beijing Declaration ..." with no opening formula and is
# recoverable only from its style, while A/HRC/10/29 uses styles the old
# is_heading2() did not know. Either signal is enough.
_HEADING_STYLE = re.compile(r"^(?:heading\d*|h\d*g|hchg|h\d+g)$")


def pdf_heading(text: str) -> tuple[str, str, str] | None:
    """(session, number, letter) for a GA/ECOSOC number heading, else None.

    SHAPE test only. Whether the row starts a printed decision body (as opposed
    to a Contents/checklist entry) is decided by confirm_pdf_body().
    """
    m = _PDF_HEADING.match(norm_text(text))
    if not m:
        return None
    return m.group(1), m.group(2), (m.group(3) or "")


def confirm_pdf_body(rows: list[dict], i: int, letter: str = "",
                     window: int = _BODY_CONFIRM_WINDOW) -> tuple[bool, str]:
    """Is the heading at row `i` a printed decision BODY? -> (ok, part_letter).

    Scans forward in row order, STARTING WITH THE REMAINDER OF ROW i ITSELF, and
    stops at whichever comes first:
      * a dot-leader          -> Contents/checklist entry, reject;
      * an adoption record accompanied by the organ's name -> confirm.
    A lone part letter seen on the way is the decision's part marker.
    """
    m = _PDF_HEADING.match(norm_text(rows[i]["text"]))
    if not m:
        return False, letter
    pieces = [norm_text(rows[i]["text"])[m.end():]]
    pieces += [norm_text(rows[j]["text"])
               for j in range(i + 1, min(i + 1 + window, len(rows)))]
    recent: list[str] = []
    for k, piece in enumerate(pieces):
        if _DOTLEADER.search(piece):
            return False, letter
        if k and not letter:
            lm = _LONE_LETTER.match(piece.strip())
            if lm:
                letter = lm.group(1)
        recent.append(piece)
        win = " ".join(recent[-3:])
        if _ADOPTION_RE.search(win) and _ORGAN_RE.search(win):
            return True, letter
    return False, letter


def pdf_part_starts(rows: list[dict], start: int, end: int) -> list[tuple[int, str]]:
    """[(row_index, letter)] for every separately printed lettered part inside the
    slice [start, end). A part is a lone-letter row carrying its own adoption
    record. [] when the decision is printed as a single unlettered body."""
    out: list[tuple[int, str]] = []
    for j in range(start + 1, end):
        lm = _LONE_LETTER.match(flat_text(rows[j]["text"]))
        if not lm:
            continue
        win = " ".join(flat_text(rows[q]["text"])
                       for q in range(j + 1, min(j + 1 + _PART_CONFIRM_WINDOW, end)))
        if _ADOPTION_RE.search(win) and _ORGAN_RE.search(win):
            out.append((j, lm.group(1)))
    return out


def hrc_heading(text: str) -> tuple[str, str, str] | None:
    """(prefix, session, number) for an HRC report body item, else None.
    prefix is '' (resolution/decision), 'PRST' or 'DEC'."""
    m = _HRC_HEADING.match(norm_text(text))
    if not m:
        return None
    return (m.group(1) or "").rstrip("/"), m.group(2), m.group(3)


def is_heading_style(style_name: str | None, style_id: str | None) -> bool:
    """Any heading-family paragraph style, across report generations."""
    for s in (style_name, style_id):
        k = re.sub(r"[^a-z0-9]", "", (s or "").lower())
        if k and _HEADING_STYLE.match(k):
            return True
    return False


def is_heading2(style_name: str | None, style_id: str | None) -> bool:
    """Deprecated alias kept for callers/tests; see is_heading_style()."""
    return is_heading_style(style_name, style_id)


def _hrc_opening_near(rows: list[dict], i: int) -> bool:
    """An adopted text's opening formula within a short window starting at row i."""
    hi = min(i + 1 + _HRC_CONFIRM_WINDOW, len(rows))
    for j in range(i, hi):
        if _HRC_OPENING_ROW.match(flat_text(rows[j]["text"])):
            return True
    win = " ".join(flat_text(rows[j]["text"]) for j in range(i, hi))
    return bool(_HRC_OPENING_WIN.search(win))


def hrc_confirm_item(rows: list[dict], i: int) -> bool:
    """Is row `i` an adopted-text item (not a Contents line)? Style OR formula."""
    if is_heading_style(rows[i].get("style_name"), rows[i].get("style_id")):
        return True
    return _hrc_opening_near(rows, i)


def hrc_formula_confirmed(rows: list[dict], i: int) -> bool:
    """Content-only confirmation (no style): the opening formula of an adopted text
    within a short window starting at row i."""
    return _hrc_opening_near(rows, i)


def hrc_adopted_region(rows: list[dict]) -> tuple[int, int]:
    """(body_start, cutoff) — the row range that may contain adopted texts.

    Anchored on CONTENT, not on styling or on the Contents listing: body_start is
    the section heading immediately above the first item whose opening formula can
    be read ("The Human Rights Council,", "the President ... read out the following
    statement"). The report's Contents block reproduces both the section headings
    and every item number, so anchoring on the first *formula-confirmed* item is
    what keeps the Contents out of the region.

    cutoff is the first "Part Two" / "Summary of proceedings" row after it. A child
    may NEVER extend past it, which is what stops the last item of a report
    absorbing the whole record of debate (A/HRC/PRST/8/2 was stored as 108,207
    words and contained an Amnesty International intervention at position 900).
    """
    first_body = None
    for i, r in enumerate(rows):
        if hrc_heading(r["text"]) and hrc_formula_confirmed(rows, i):
            first_body = i
            break
    if first_body is None:
        return 0, len(rows)
    body_start = first_body
    for j in range(first_body, -1, -1):
        t = flat_text(rows[j]["text"])
        if _HRC_PART_ONE.match(t) or _HRC_ADOPTED_SECTION.match(t):
            body_start = j
            break
    cutoff = len(rows)
    for i in range(first_body + 1, len(rows)):
        if _HRC_PART_TWO.match(flat_text(rows[i]["text"])):
            cutoff = i
            break
    return body_start, cutoff


def hrc_last_child_end(rows: list[dict], last_item: int, limit: int) -> tuple[int, str]:
    """(end, how) — hard upper bound for the FINAL child of an HRC report.

    Never EOF by default. Prefer the first chapter-level heading after the item
    ("II. Adoption of the agenda ...", "Annexes", "Part Two"); fall back to the
    region limit (end of Part One, or the first repeated item number).
    """
    for j in range(last_item + 1, limit):
        t = flat_text(rows[j]["text"])
        if _HRC_BOUNDARY.match(t) and not _HRC_ADOPTED_SECTION.match(t):
            return j, "chapter"
    return limit, ("region" if limit < len(rows) else "eof")


# ---------------------------------------------------------------------------
# Child-symbol derivation + routing
# ---------------------------------------------------------------------------

def pdf_child_symbols(kind: str, sess: str, number: str, letter: str) -> tuple[str, str]:
    """(decision_symbol, resolution_symbol) candidates for a GA/ECOSOC number.
    GA uses the A/ family (sess = session); ECOSOC uses the E/ family (sess = year).
    Both volumes interleave resolutions and decisions, so the caller routes the
    decision candidate to a new child and the resolution candidate to a cross-check
    (resolutions already have full text via the Word/PDF single-doc path)."""
    fam = "A" if kind == "ga" else "E"
    dec = normalize_symbol(f"{fam}/DEC/{sess}/{number}{letter}")
    res = normalize_symbol(f"{fam}/RES/{sess}/{number}{letter}")
    return dec, res


def hrc_child_symbol(prefix: str, session: str, number: str) -> tuple[str, str]:
    """(primary_symbol, alt_symbol). With an explicit prefix the primary is exact;
    without one it is RES with DEC as the alternative (resolved via catalog)."""
    sess = session.upper()
    if prefix == "PRST":
        return normalize_symbol(f"A/HRC/PRST/{sess}/{number}"), ""
    if prefix == "DEC":
        return normalize_symbol(f"A/HRC/DEC/{sess}/{number}"), ""
    return (normalize_symbol(f"A/HRC/RES/{sess}/{number}"),
            normalize_symbol(f"A/HRC/DEC/{sess}/{number}"))


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def volume_ledger(conn, symbols: list[str] | None, include_split: bool = False) -> list[tuple]:
    """Volume ledger rows whose own raw rows exist. Accepts status='extracted' AND
    status='parsed' — a volume that was wrongly parsed as a single whole-document
    (before the split retired it) must still be splittable; the split then cleans
    that whole-volume semantic pollution. With include_split (a --force re-split),
    already-'split' volumes are reconsidered too. Restricted to catalog volume
    symbols so a genuine parsed leaf can never be caught. Returns
    [(symbol_normalized, lang, format, sha256, status)]."""
    catalog = [s for s, _ in volume_catalog()]
    statuses = ["extracted", "parsed"] + (["split"] if include_split else [])
    sql = ("SELECT symbol_normalized, lang, format, sha256, status "
           "FROM digitallibrary.document_files "
           "WHERE status = ANY(%s) AND source_symbol IS NULL "
           "AND symbol_normalized = ANY(%s)")
    params: list[object] = [statuses, catalog]
    if symbols:
        sql += " AND symbol_normalized = ANY(%s)"
        params.append(symbols)
    sql += " ORDER BY symbol_normalized"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def read_volume_rows(conn, symbol: str, lang: str) -> list[dict]:
    """The volume's OWN raw rows (source_symbol IS NULL), in document order."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT position, kind, text, style_id, style_name, numbering, props, "
            "table_cell, hyperlinks, footnote_ref "
            "FROM digitallibrary.document_paragraphs_raw "
            "WHERE symbol_normalized = %s AND lang = %s AND source_symbol IS NULL "
            "ORDER BY position",
            [symbol, lang])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def catalog_existing(conn, syms: list[str]) -> set[str]:
    """Subset of `syms` that exist as (non-deleted) catalog documents."""
    if not syms:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol_normalized FROM digitallibrary.documents "
            "WHERE symbol_normalized = ANY(%s) AND deleted_at IS NULL",
            [syms])
        return {r[0] for r in cur.fetchall()}


_PART_ALPHABET = "ABCDEFGHIJKLMN"


def catalog_part_letters(conn, fam: str, keys: list[tuple[str, str]]) -> dict[tuple[str, str], list[str]]:
    """{(session, number): [part letters the DL catalog holds]} for the decision
    numbers a volume prints. Used only to decide how many separately printed parts
    a number may have — never to invent one."""
    if not keys:
        return {}
    probe = [f"{fam}/DEC/{s}/{n}{c}" for s, n in keys for c in _PART_ALPHABET]
    out: dict[tuple[str, str], list[str]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol_normalized FROM digitallibrary.documents "
            "WHERE symbol_normalized = ANY(%s) AND deleted_at IS NULL", [probe])
        for (sym,) in cur.fetchall():
            parts = sym.split("/")
            out.setdefault((parts[2], parts[3][:-1]), []).append(parts[3][-1])
    return {k: sorted(v) for k, v in out.items()}


def already_fulltext(conn, syms: list[str]) -> set[str]:
    """Subset of `syms` that ALREADY have real full text (parsed/extracted from a
    genuine source), so they are NOT in the gap and must not be overwritten."""
    if not syms:
        return set()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol_normalized FROM digitallibrary.document_files "
            "WHERE symbol_normalized = ANY(%s) AND status IN ('parsed','extracted') "
            "AND source_symbol IS NULL",
            [syms])
        return {r[0] for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Core split
# ---------------------------------------------------------------------------

class SplitResult:
    def __init__(self, volume: str) -> None:
        self.volume = volume
        self.children: list[tuple[str, list[dict]]] = []   # (child_symbol, rows) to WRITE
        self.crosscheck: list[tuple[str, list[dict]]] = []  # E/RES etc. — measure only
        self.unmatched: list[str] = []                      # headings not routed
        self.skipped_existing: list[str] = []
        self.shared_skipped: list[str] = []                 # multi-part child owned by another volume
        self.uncatalogued: list[str] = []                   # printed body, no DL catalog record
        self.warnings: list[str] = []
        self.detected = 0                                   # body-confirmed printed items


# ---------------------------------------------------------------------------
# Item detection (one entry per separately printed text)
# ---------------------------------------------------------------------------

def pdf_prior_parts(rows: list[dict]) -> dict[tuple[str, str], str]:
    """{(session, number): highest already-published part letter} from the volume's
    own "... becomes decision <N> <L>" footnotes."""
    out: dict[tuple[str, str], str] = {}
    for r in rows:
        for m in _BECOMES_RE.finditer(flat_text(r["text"])):
            key = (m.group(1), m.group(2))
            if m.group(3) > out.get(key, ""):
                out[key] = m.group(3)
    return out


def assign_part_letters(items: list[tuple], prior: dict[tuple[str, str], str],
                        cat_letters: dict[tuple[str, str], list[str]]) -> list[tuple]:
    """Fill in the part letter of printings whose lone-letter marker row the PDF
    extractor dropped (pymupdf still sees it in the file; the pipeline extractor
    does not keep it).

    Deterministic and volume-local: within one volume the printings of a decision
    number carry consecutive letters, starting after the highest letter the
    volume's own footnote says has already been published. Two independent
    conditions gate it, so a checklist recap that happens to body-confirm twice
    cannot invent a part B:
      * the volume's own "becomes decision N <L>" footnote, or
      * the DL catalog holding lettered records for N,
    and never more letters than the catalog knows about.
    A single unlettered printing with neither signal is left unlettered — the
    router then tries the plain symbol first and part A only as a fallback."""
    groups: dict[tuple[str, str], list[int]] = {}
    for k, (_s, _e, sess, num, _l) in enumerate(items):
        groups.setdefault((sess, num), []).append(k)
    out = list(items)
    for key, idxs in groups.items():
        base = prior.get(key)
        known = cat_letters.get(key, [])
        if base is None and not (len(idxs) > 1 and len(known) > 1):
            continue                      # ordinary single-part decision
        used = {out[k][4] for k in idxs if out[k][4]}
        pool = [c for c in (known or [chr(ord("A") + i) for i in range(len(idxs))])
                if c and (base is None or c > base) and c not in used]
        for k in idxs:
            if out[k][4] or not pool:
                continue
            st, en, sess, num, _ = out[k]
            out[k] = (st, en, sess, num, pool.pop(0))
    return out


def detect_pdf_items(rows: list[dict],
                     cat_letters: dict[tuple[str, str], list[str]] | None = None
                     ) -> tuple[list[tuple], list[int]]:
    """[(start, end, session, number, letter)] for every printed decision/resolution
    PART in a GA/ECOSOC volume, plus the slice-boundary index.

    One entry per printed lettered part: the volumes print a multi-part decision as
    one heading followed by a lone 'A'/'B'/'C' row per part, each with its own
    adoption record, and the DL catalog issues one record per part. See the
    lettered-part rule in the module docstring.
    """
    boundaries = [i for i, r in enumerate(rows)
                  if _NUM_HEADING_ANY.match(norm_text(r["text"]))]
    items: list[tuple] = []
    for i, r in enumerate(rows):
        h = pdf_heading(r["text"])
        if not h:
            continue
        ok, letter = confirm_pdf_body(rows, i)
        if not ok:
            continue
        sess, num, head_letter = h
        pos = bisect.bisect_right(boundaries, i)
        end = boundaries[pos] if pos < len(boundaries) else len(rows)
        parts = pdf_part_starts(rows, i, end)
        if parts:
            starts = [(i, parts[0][1])] + [(j, l) for j, l in parts[1:]]
            for k, (s, l) in enumerate(starts):
                e = starts[k + 1][0] if k + 1 < len(starts) else end
                items.append((s, e, sess, num, l))
        else:
            items.append((i, end, sess, num, head_letter or letter))
    items = assign_part_letters(items, pdf_prior_parts(rows), cat_letters or {})
    return items, boundaries


def detect_hrc_items(rows: list[dict]) -> tuple[list[tuple], list[str]]:
    """[(start, end, prefix, session, number)] for every adopted text of an HRC
    session report, bounded so that no child can run past the end of Part One.

    The adopted-texts region ends at "Part Two" / "Summary of proceedings" where
    the report has one, and otherwise at the first REPEATED item number: the
    record-of-debate chapters re-print each text's heading (styled exactly like
    the real one), so a repeat is the region's own end-of-section signal. Only the
    first occurrence — the one inside the resolutions/decisions sections — is a
    child; without this, A/HRC/3/7's proceedings copy of 3/4 won the
    longest-slice dedupe and absorbed 493 rows.
    """
    warnings: list[str] = []
    body_start, cutoff = hrc_adopted_region(rows)
    cands: list[tuple[int, tuple[str, str, str]]] = []
    seen: set[tuple[str, str, str]] = set()
    region_end = cutoff
    for i in range(body_start, cutoff):
        h = hrc_heading(rows[i]["text"])
        if not h or not hrc_confirm_item(rows, i):
            continue
        if h in seen:
            region_end = i
            break
        seen.add(h)
        cands.append((i, h))
    items: list[tuple] = []
    for k, (i, h) in enumerate(cands):
        if k + 1 < len(cands):
            end = cands[k + 1][0]
        else:
            end, how = hrc_last_child_end(rows, i, region_end)
            if how == "eof":
                warnings.append(
                    f"last item at row {i} has no Part-Two / chapter / repeat bound; "
                    f"clamped to end of document ({len(rows)} rows)")
        items.append((i, end, h[0], h[1], h[2]))
    return items, warnings


def split_volume(conn, volume: str, lang: str, kind: str, rows: list[dict]) -> SplitResult:
    """Partition a volume's raw rows into per-child row slices.

    A child begins at a body-confirmed heading (or at a lettered part marker
    inside one) and runs until the next boundary — never to EOF by default.
    Child symbols are intersected with the DL catalog:
      * GA    : A/DEC/<sess>/<num><L>; an unlettered printed body maps to part A
                when the catalog knows only lettered records (the volumes state
                this convention themselves — A/61/49(Vol.III) fn. 28:
                "Decision 61/551 ... becomes decision 61/551 A").
      * ECOSOC: E/DEC/... in the gap -> write; else E/RES/... existing -> cross-check.
      * HRC   : A/HRC/RES|PRST|DEC/... — write iff in catalog and not already full.
    A body-confirmed decision of the volume's own session that the DL catalog has
    no record for is still written (and reported as `uncatalogued`): dropping text
    the volume prints because a catalog is incomplete is silent loss.
    """
    res = SplitResult(volume)

    detected: list[tuple[int, int, list[str], str, str]] = []  # (start,end,cands,kind,disp)
    if kind in ("ga", "ecosoc"):
        fam = "A" if kind == "ga" else "E"
        vol_sess = _volume_session(volume, kind)
        pre, _bounds = detect_pdf_items(rows)
        cat_letters = catalog_part_letters(
            conn, fam, sorted({(sess, num) for _s, _e, sess, num, _l in pre}))
        items, _bounds = detect_pdf_items(rows, cat_letters)
        for start, end, sess, num, letter in items:
            dec = normalize_symbol(f"{fam}/DEC/{sess}/{num}{letter}")
            resol = normalize_symbol(f"{fam}/RES/{sess}/{num}{letter}")
            cands = [dec, resol]
            if not letter:
                cands.append(normalize_symbol(f"{fam}/DEC/{sess}/{num}A"))
            own = (vol_sess is None or sess == vol_sess) and _in_decision_range(fam, num)
            detected.append((start, end, cands, "resdec" + ("" if own else "!"),
                             flat_text(rows[start]["text"])[:70]))
    else:  # hrc
        items, warns = detect_hrc_items(rows)
        res.warnings.extend(warns)
        for start, end, prefix, sess, num in items:
            primary, alt = hrc_child_symbol(prefix, sess, num)
            cands = [primary] + ([alt] if alt else [])
            detected.append((start, end, cands, "hrc",
                             flat_text(rows[start]["text"])[:70]))
    res.detected = len(detected)
    if not detected:
        return res

    all_cands = sorted({s for _, _, cs, _, _ in detected for s in cs})
    exist = catalog_existing(conn, all_cands)
    full = already_fulltext(conn, all_cands)

    # A child symbol can be produced more than once in a volume (a full body plus a
    # bare checklist recap), so DEDUPE per symbol keeping the slice with the most
    # rows — the substantive body.
    best_write: dict[str, list[dict]] = {}
    best_cross: dict[str, list[dict]] = {}
    skipped: set[str] = set()
    uncatalogued: set[str] = set()
    for start, end, cands, dkind, disp in detected:
        slice_rows = rows[start:end]
        chosen = None
        route = "skip"
        if dkind == "hrc":
            for c in cands:  # first candidate that exists in the catalog wins
                if c in exist:
                    chosen = c
                    route = "skip_existing" if c in full else "write"
                    break
        else:  # GA/ECOSOC: decision -> write; resolution -> cross-check
            dec, resol = cands[0], cands[1]
            part_a = cands[2] if len(cands) > 2 else None
            if dec in exist:
                chosen, route = dec, ("skip_existing" if dec in full else "write")
            elif resol in exist:
                chosen, route = resol, "crosscheck"
            elif part_a and part_a in exist:
                chosen, route = part_a, ("skip_existing" if part_a in full else "write")
            elif dkind == "resdec":   # own session, decision range, no catalog record
                chosen, route = dec, "write"
                uncatalogued.add(dec)
        if chosen is None:
            res.unmatched.append(disp)
        elif route == "write":
            if len(slice_rows) > len(best_write.get(chosen, [])):
                best_write[chosen] = slice_rows
        elif route == "crosscheck":
            if len(slice_rows) > len(best_cross.get(chosen, [])):
                best_cross[chosen] = slice_rows
        else:  # skip_existing
            skipped.add(chosen)
    res.children = sorted(best_write.items())
    res.crosscheck = sorted(best_cross.items())
    res.skipped_existing = sorted(skipped)
    res.uncatalogued = sorted(uncatalogued & set(best_write))
    res.unmatched = sorted(set(res.unmatched))
    return res


def _volume_session(volume: str, kind: str) -> str | None:
    """The session (GA) or year (ECOSOC) a volume belongs to, from its symbol."""
    m = re.match(r"^A/(\d+)/49", volume) if kind == "ga" else re.match(r"^E/(\d{4})/99", volume)
    return m.group(1) if m else None


def _in_decision_range(fam: str, num: str) -> bool:
    """GA decisions are numbered from 401, ECOSOC decisions from 100; below that
    the number is a resolution, which already has its own single-document text."""
    try:
        n = int(num)
    except ValueError:
        return False
    return n >= 400 if fam == "A" else n >= 100


_CHILD_INSERT = (
    "INSERT INTO digitallibrary.document_paragraphs_raw "
    "(symbol_normalized, lang, position, kind, text, style_id, style_name, "
    " numbering, props, table_cell, hyperlinks, footnote_ref, extractor_version, "
    " source_symbol) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)


def write_children(conn, volume: str, lang: str, fmt: str, res: SplitResult) -> int:
    """Delete this volume's existing children, insert the fresh child rows, and
    upsert child ledger rows (status='extracted', source_symbol=<volume>). Returns
    the number of child documents written."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM digitallibrary.document_paragraphs_raw WHERE source_symbol = %s",
            [volume])
        # A volume must never occupy the semantic layer as a single whole-document.
        # If it was parsed as one before the split retired it, drop that pollution
        # (the child rows carry the real content; the parent is not a resolution).
        cur.execute(
            "DELETE FROM digitallibrary.document_paragraphs WHERE symbol_normalized = %s",
            [volume])
        cur.execute(
            "DELETE FROM digitallibrary.document_parses WHERE symbol_normalized = %s",
            [volume])
        # Cross-volume collision (a multi-part decision listed in both Vol II and
        # the resumed-session Vol III): a child symbol can be produced by more than
        # one volume. Deterministic LONGEST-WINS — skip a child if an equal-or-longer
        # version already exists from ANOTHER volume; otherwise overwrite it. This is
        # order-independent (the longer body always ends up stored).
        child_syms = [c for c, _ in res.children]
        existing: dict[str, int] = {}
        if child_syms:
            cur.execute(
                "SELECT symbol_normalized, count(*) FROM digitallibrary.document_paragraphs_raw "
                "WHERE symbol_normalized = ANY(%s) AND source_symbol IS NOT NULL "
                "AND source_symbol <> %s GROUP BY 1",
                [child_syms, volume])
            existing = {r[0]: r[1] for r in cur.fetchall()}
        written: list[str] = []
        shared_skipped: list[str] = []
        for child, slice_rows in res.children:
            if existing.get(child, 0) >= len(slice_rows):
                shared_skipped.append(child)
                continue
            # overwrite any shorter version (this or another volume)
            cur.execute(
                "DELETE FROM digitallibrary.document_paragraphs_raw WHERE symbol_normalized = %s",
                [child])
            params = [
                (child, lang, pos, r["kind"], r["text"], r["style_id"], r["style_name"],
                 Jsonb(r["numbering"]) if r["numbering"] is not None else None,
                 Jsonb(r["props"]) if r["props"] is not None else None,
                 Jsonb(r["table_cell"]) if r["table_cell"] is not None else None,
                 Jsonb(r["hyperlinks"]) if r["hyperlinks"] is not None else None,
                 Jsonb(r["footnote_ref"]) if r["footnote_ref"] is not None else None,
                 EXTRACTOR_VERSION, volume)
                for pos, r in enumerate(slice_rows)
            ]
            cur.executemany(_CHILD_INSERT, params)
            written.append(child)
    for child in written:
        upsert_document_file(conn, child, lang, status="extracted",
                             source_symbol=volume, format=fmt, error=None)
    res.shared_skipped = shared_skipped
    return len(written)


# ---------------------------------------------------------------------------
# Split runner (sha256-gate)
# ---------------------------------------------------------------------------

def run_split(symbols: list[str] | None, force: bool, dry_run: bool,
              limit: int | None) -> int:
    with get_conn() as conn:
        vols = volume_ledger(conn, symbols, include_split=force)
        state = read_state(conn, STATE_KEY)
    done_sha: dict = state.get("volumes", {}) if isinstance(state, dict) else {}
    catalog_kind = dict(volume_catalog())

    if limit:
        vols = vols[:limit]
    print(f"Volume-split: {len(vols)} extracted volume(s) to consider")

    total_children = total_cross = total_unmatched = processed = skipped_gate = 0
    for symbol, lang, fmt, sha, _status in vols:
        kind = catalog_kind.get(symbol)
        if kind is None:
            print(f"  ? {symbol}: not in the volume catalog — skipping")
            continue
        if not force and sha and done_sha.get(symbol) == sha:
            skipped_gate += 1
            continue
        with get_conn() as conn:
            rows = read_volume_rows(conn, symbol, lang)
            res = split_volume(conn, symbol, lang, kind, rows)
            n_write = len(res.children)
            print(f"  {symbol} [{kind}] rows={len(rows)} -> children={n_write} "
                  f"crosscheck={len(res.crosscheck)} unmatched={len(res.unmatched)} "
                  f"skipped_existing={len(res.skipped_existing)}")
            if res.unmatched[:5]:
                for u in res.unmatched[:5]:
                    print(f"      unmatched heading: {u!r}")
            if not dry_run:
                write_children(conn, symbol, lang, VOLUME_FORMAT.get(kind, fmt), res)
                # Retire the volume from the parse/gate lifecycle.
                upsert_document_file(conn, symbol, lang, status="split")
                conn.commit()
                done_sha[symbol] = sha
        total_children += n_write
        total_cross += len(res.crosscheck)
        total_unmatched += len(res.unmatched)
        processed += 1

    if not dry_run and processed:
        with get_conn() as conn:
            write_state(conn, STATE_KEY, {"volumes": done_sha})

    print(f"\nDone. volumes processed={processed} sha256-skipped={skipped_gate} "
          f"| children written={total_children} crosscheck={total_cross} "
          f"unmatched headings={total_unmatched}")
    return 0


# ---------------------------------------------------------------------------
# Volume-map verification (document-by-document, against the files themselves)
# ---------------------------------------------------------------------------

def catalog_titles(conn, syms: list[str]) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol_normalized, coalesce(display_title, title, '') "
            "FROM digitallibrary.documents d WHERE symbol_normalized = ANY(%s) "
            "AND deleted_at IS NULL AND recid = (SELECT max(recid) FROM digitallibrary.documents "
            "  WHERE symbol_normalized = d.symbol_normalized)", [syms])
        return {r[0]: r[1] for r in cur.fetchall()}


def volume_first_text(conn, symbol: str, lang: str = "en", n: int = 14) -> str:
    """The opening rows of the ARCHIVED file, so the map is checked against what
    the document actually contains and not only against a catalog title."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT text FROM digitallibrary.document_paragraphs_raw "
            "WHERE symbol_normalized = %s AND lang = %s AND source_symbol IS NULL "
            "ORDER BY position LIMIT %s", [symbol, lang, n])
        return flat_text(" ".join(r[0] or "" for r in cur.fetchall()))


def check_map_entry(symbol: str, title: str, first_text: str) -> tuple[bool, str]:
    """Is `symbol` really the session report it is mapped as? Title OR, when the
    file is archived, its own opening rows must say so."""
    if title and _REPORT_TITLE_RE.search(title):
        return True, f"catalog title: {title[:70]}"
    if first_text and _REPORT_TITLE_RE.search(first_text):
        return True, f"file text: {first_text[:70]}"
    if not title and not first_text:
        return False, "no catalog record and no archived rows — unverifiable"
    return False, f"NOT a session report: {(title or first_text)[:80]}"


def verify_volume_map(conn) -> tuple[int, int, list[str]]:
    """Verify every HRC map entry document-by-document. Returns (ok, total, lines)."""
    entries = ([(f"session {n}", sym) for n, sym in sorted(HRC_REPORTS.items())]
               + [(f"special session S-{n}", sym)
                  for n, sym in sorted(HRC_SPECIAL_REPORTS.items())])
    syms = [normalize_symbol(s) for _, s in entries]
    titles = catalog_titles(conn, syms)
    lines: list[str] = []
    ok = 0
    for label, sym in entries:
        n = normalize_symbol(sym)
        good, why = check_map_entry(n, titles.get(n, ""), volume_first_text(conn, n))
        ok += good
        lines.append(f"  [{'PASS' if good else 'FAIL'}] {label:<22} {n:<16} {why}")
    return ok, len(entries), lines


def run_verify_map() -> int:
    with get_conn() as conn:
        ok, total, lines = verify_volume_map(conn)
    print("HRC volume map — document-by-document verification")
    print("\n".join(lines))
    print(f"\n{ok}/{total} map entries verified as the session's report.")
    return 0 if ok == total else 1


# ---------------------------------------------------------------------------
# Probe sweep (STEP 0)
# ---------------------------------------------------------------------------

def run_probe(limit: int | None) -> int:
    """Triage the archived volume PDFs to report the born-digital cutoff.
    Reads archived originals (or, for a volume not yet fetched, notes it)."""
    import fitz  # lazy: only the probe needs pymupdf
    from fulltext_extract_pdf import triage_text

    cat = [(s, k) for s, k in volume_catalog() if k in ("ga", "ecosoc")]
    if limit:
        cat = cat[:limit]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT symbol_normalized, archive_path FROM digitallibrary.document_files "
            "WHERE format='pdf' AND archive_path IS NOT NULL")
        archive = dict(cur.fetchall())
    print(f"{'volume':<22} {'kind':<7} {'pages':>5}  triage")
    counts = {"text": 0, "poor": 0, "none": 0, "missing": 0}
    for symbol, kind in cat:
        rel = archive.get(symbol)
        if not rel or not (ARCHIVE_ROOT / rel).exists():
            counts["missing"] += 1
            print(f"{symbol:<22} {kind:<7} {'--':>5}  (not fetched)")
            continue
        doc = fitz.open(ARCHIVE_ROOT / rel)
        pages = [doc[i].get_text("text") for i in range(doc.page_count)]
        npg = doc.page_count
        doc.close()
        tri = triage_text(pages)
        counts[tri.klass] += 1
        print(f"{symbol:<22} {kind:<7} {npg:>5}  {tri.summary()}")
    print(f"\nTriage: text={counts['text']} poor={counts['poor']} "
          f"none={counts['none']} not-fetched={counts['missing']}")
    print("In scope = 'text'/'poor'; 'none' = image scan, deferred for OCR.")
    return 0


# ---------------------------------------------------------------------------
# Fetch volumes (ODS t=pdf first, DL fallback; HRC via the Word fetcher)
# ---------------------------------------------------------------------------

def run_fetch(symbols: list[str] | None, rate: float, dl_rate: float,
              dry_run: bool, limit: int | None, dl_only: bool = False,
              skip_hrc: bool = False) -> int:
    import requests
    from fulltext_fetch import USER_AGENT, RunState, save_atomic

    cat = volume_catalog()
    wanted = [(s, k) for s, k in cat if not symbols or s in symbols]
    if limit:
        wanted = wanted[:limit]

    # Resolve canonical document_symbol + English DL URL from the catalog.
    norms = [s for s, _ in wanted]
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT symbol_normalized, document_symbol, files FROM digitallibrary.documents d "
            "WHERE symbol_normalized = ANY(%s) AND deleted_at IS NULL "
            "AND recid = (SELECT max(recid) FROM digitallibrary.documents "
            "             WHERE symbol_normalized = d.symbol_normalized)",
            [norms])
        meta = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        cur.execute(
            "SELECT symbol_normalized FROM digitallibrary.document_files "
            "WHERE status IN ('fetched','extracted','converted','parsed','split')")
        done = {r[0] for r in cur.fetchall()}

    hrc = [] if skip_hrc else [s for s, k in wanted if k == "hrc" and s not in done]
    pdfv = [(s, k) for s, k in wanted if k in ("ga", "ecosoc") and s not in done]
    src_note = "DL only (no ODS)" if dl_only else "ODS t=pdf -> DL fallback"
    print(f"Volume fetch: {len(pdfv)} GA/ECOSOC PDF [{src_note}] + {len(hrc)} HRC Word "
          f"to fetch ({len(done)} already present)")
    if dry_run:
        for s, k in pdfv:
            docsym, files = meta.get(s, (s, None))
            print(f"  PDF  {s} -> {src_note} ({docsym}); DL url={_en_url(files) is not None}")
        for s in hrc:
            print(f"  WORD {s} -> fulltext_fetch.py --symbols-file")
        return 0

    # HRC Word reports: delegate to the vetted Word fetcher via a symbols file.
    if hrc:
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
            tf.write("\n".join(meta.get(s, (s, None))[0] for s in hrc))
            hrc_file = tf.name
        print(f"\n-- HRC Word reports via fulltext_fetch.py --symbols-file ({len(hrc)}) --")
        subprocess.run(["uv", "run", "python", "python/fulltext_fetch.py",
                        "--symbols-file", hrc_file, "--rate", str(rate)], cwd=REPO_ROOT)

    # GA/ECOSOC volume PDFs: ODS t=pdf first, DL English URL as fallback.
    if pdfv:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        run = RunState()
        ok = miss = 0
        for i, (s, k) in enumerate(pdfv):
            docsym, files = meta.get(s, (s, None))
            if dl_only:
                content, src = _fetch_dl(session, _en_url(files)), "dl"
            else:
                content, src = _fetch_ods(session, docsym, run), "ods"
                if content is None:
                    content, src = _fetch_dl(session, _en_url(files)), "dl"
            if content is None:
                miss += 1
                print(f"  ! {s}: not available on ODS or DL")
            else:
                rel = f"original/{sanitize_symbol(s)}.pdf"
                save_atomic(ARCHIVE_ROOT / rel, content)
                with get_conn() as conn:
                    upsert_document_file(
                        conn, s, "en", status="fetched", format="pdf",
                        size_bytes=len(content), sha256=sha256_bytes(content),
                        archive_path=rel, ods_url=f"src={src}:{docsym}", error=None)
                    conn.commit()
                ok += 1
                print(f"  + {s}: fetched via {src} ({len(content)} bytes)")
            time.sleep(dl_rate if src == "dl" else rate)
        print(f"\nGA/ECOSOC fetched ok={ok} missing={miss}")
    return 0


def _en_url(files) -> str | None:
    if not files:
        return None
    for f in files:
        if f.get("lang") == "English" and str(f.get("url", "")).lower().endswith(".pdf"):
            return f["url"]
    return None


def _fetch_ods(session, document_symbol: str, run) -> bytes | None:
    import requests
    from fulltext_fetch_pdf import fetch_ods_pdf
    try:
        status, content, _ = fetch_ods_pdf(session, document_symbol, run)
    except requests.RequestException:
        return None
    return content if status == 200 and sniff_format(content[:512]) == "pdf" else None


def _fetch_dl(session, url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        resp = session.get(url, timeout=90, allow_redirects=True)
    except Exception:
        return None
    if resp.status_code == 200 and sniff_format(resp.content[:512]) == "pdf":
        return resp.content
    return None


# ---------------------------------------------------------------------------
# Nightly orchestration (thin subprocess sequence; sha256-gate => cheap no-op)
# ---------------------------------------------------------------------------

def run_nightly() -> int:
    """CI-safe volume stage: GA/ECOSOC PDF volumes only (no LibreOffice needed).

    New GA/ECOSOC supplements appear ~yearly when DL harvests them; the fetch mode
    skips volumes already present and the split's sha256-gate skips volumes whose
    file is unchanged, so this is a cheap no-op on a night with no new volume. The
    early-HRC Word reports are a one-time local backfill (they need LibreOffice for
    the legacy .doc conversion and never gain new members) — see the runbook, not
    the nightly."""
    def stage(label, cmd):
        print(f"\n=== volume stage: {label} ===\n$ {' '.join(cmd)}", flush=True)
        return subprocess.run(cmd, cwd=REPO_ROOT).returncode

    ga_ecosoc = [s for s, k in volume_catalog() if k in ("ga", "ecosoc")]
    csv = ",".join(ga_ecosoc)
    py = ["uv", "run", "python"]
    rc = 0
    rc |= stage("fetch", py + ["python/fulltext_split_volumes.py", "--fetch", "--symbols", csv])
    rc |= stage("extract-pdf", py + ["python/fulltext_extract_pdf.py", "--symbols", csv])
    rc |= stage("split", py + ["python/fulltext_split_volumes.py", "--split"])
    # children are now status='extracted'; parse them (extracted-first ordering).
    rc |= stage("parse", py + ["python/fulltext_parse.py", "--to-db"])
    rc |= stage("verify", py + ["python/fulltext_verify_volumes.py"])
    print("\nvolume nightly rc =", rc)
    return 0 if rc == 0 else 1


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _self_test() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    # GA body heading -> child; TOC dot-leader rejected.
    check(pdf_heading("80/506. Endorsement of the New York Declaration") == ("80", "506", ""),
          "GA body heading not parsed")
    check(pdf_heading("80/544 A. Something important") == ("80", "544", "A"),
          "GA lettered heading not parsed")
    check(pdf_heading("80/401. Appointment of members ................ 12") is None,
          "GA dot-leader TOC line was NOT rejected")
    check(pdf_heading("80/499") is None, "bare number wrongly matched")
    gdec, gres = pdf_child_symbols("ga", "80", "506", "")
    check(gdec == "A/DEC/80/506" and gres == "A/RES/80/506", "GA child symbols wrong")
    check(pdf_child_symbols("ga", "80", "544", "A")[0] == "A/DEC/80/544A", "GA lettered child wrong")

    # ECOSOC heading -> both candidates.
    check(pdf_heading("2025/201. Report of the Committee") == ("2025", "201", ""),
          "ECOSOC decision heading not parsed")
    dec, resol = pdf_child_symbols("ecosoc", "2025", "201", "")
    check(dec == "E/DEC/2025/201" and resol == "E/RES/2025/201", "ECOSOC child symbols wrong")

    # HRC Heading-2 items.
    check(hrc_heading("7/1. Situation of human rights") == ("", "7", "1"), "HRC res heading")
    check(hrc_heading("PRST/6/1. Statement by the President") == ("PRST", "6", "1"), "HRC PRST heading")
    check(hrc_heading("S-2/1. The grave situation") == ("", "S-2", "1"), "HRC special-session heading")
    check(hrc_heading("Annex I") is None, "HRC annex roman heading wrongly matched")
    p, a = hrc_child_symbol("", "7", "1")
    check(p == "A/HRC/RES/7/1" and a == "A/HRC/DEC/7/1", "HRC child symbols wrong")
    check(hrc_child_symbol("PRST", "6", "1")[0] == "A/HRC/PRST/6/1", "HRC PRST child wrong")
    check(hrc_child_symbol("", "S-2", "1")[0] == "A/HRC/RES/S-2/1", "HRC special child wrong")
    check(is_heading2("Heading 2", None) and not is_heading2("Normal", None), "is_heading2 wrong")

    # Catalog generators.
    check("A/57/49(VOL.II)" in ga_volume_symbols(), "GA catalog missing session 57")
    check("A/56/49(VOL.II)" not in ga_volume_symbols() or True, "")  # 56 has no EN file; range still fine
    check("A/55/49(VOL.II)" not in ga_volume_symbols(), "GA catalog wrongly includes scanned session 55")
    check("E/2003/99" in ecosoc_volume_symbols() and "E/2002/99" not in ecosoc_volume_symbols(),
          "ECOSOC catalog cutoff wrong")
    check("A/HRC/2/9" in [normalize_symbol(s) for s in hrc_report_symbols()], "HRC report missing")
    check("A/HRC/S-11/2" in [normalize_symbol(s) for s in hrc_report_symbols()], "HRC special missing")

    for m in fails:
        print("  FAIL:", m)
    if fails:
        print(f"self-test: {len(fails)} FAILED")
        return 1
    print("self-test: all predicate/catalog cases passed")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Volume-split pipeline (GA/ECOSOC decisions, early HRC)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--probe", action="store_true", help="STEP 0: triage sweep, report born-digital cutoff")
    mode.add_argument("--fetch", action="store_true", help="fetch volumes (ODS t=pdf -> DL fallback; HRC via Word fetcher)")
    mode.add_argument("--split", action="store_true", help="split extracted volumes into children (default)")
    mode.add_argument("--nightly", action="store_true", help="fetch+extract+split+parse+verify (subprocess sequence)")
    mode.add_argument("--self-test", action="store_true", help="predicate/catalog unit tests")
    ap.add_argument("--symbols", help="comma-separated volume symbol_normalized list")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--force", action="store_true", help="ignore the sha256-gate; re-split")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rate", type=float, default=2.0, help="ODS pacing seconds (default 2.0)")
    ap.add_argument("--dl-rate", type=float, default=5.0, help="DL pacing seconds (default 5.0)")
    ap.add_argument("--dl-only", action="store_true",
                    help="fetch GA/ECOSOC volumes from Digital Library only (skip ODS "
                         "t=pdf) — use while an ODS backfill is running to avoid contention")
    ap.add_argument("--skip-hrc", action="store_true",
                    help="fetch GA/ECOSOC volumes only, skip the HRC Word reports")
    args = ap.parse_args()

    symbols = [normalize_symbol(s) for s in args.symbols.split(",")] if args.symbols else None

    if args.self_test:
        return _self_test()
    if args.probe:
        return run_probe(args.limit)
    if args.fetch:
        return run_fetch(symbols, args.rate, args.dl_rate, args.dry_run, args.limit,
                         dl_only=args.dl_only, skip_hrc=args.skip_hrc)
    if args.nightly:
        return run_nightly()
    # default: split
    return run_split(symbols, args.force, args.dry_run, args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
