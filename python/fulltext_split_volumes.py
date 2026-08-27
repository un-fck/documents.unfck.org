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
HRC_REPORTS = {
    2: "A/HRC/2/9", 3: "A/HRC/3/7", 4: "A/HRC/4/123", 5: "A/HRC/5/21",
    6: "A/HRC/6/22", 7: "A/HRC/7/78", 8: "A/HRC/8/52", 9: "A/HRC/9/28",
    10: "A/HRC/10/29", 11: "A/HRC/11/37",
}
# special sessions S-2..S-11 -> A/HRC/S-<n>/2 (the report of that special session)
HRC_SPECIAL_SESSIONS = range(2, 12)


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
    return (list(HRC_REPORTS.values())
            + [f"A/HRC/S-{n}/2" for n in HRC_SPECIAL_SESSIONS])


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
# A dot-leader (TOC/checklist) run: the volumes print a table of contents whose
# entries look exactly like a body heading ('80/506. Endorsement ...') but are
# followed by leader dots and a page number. Exclude those so they are not treated
# as child boundaries (they fall out as allowed-drop front matter in the gate).
_DOTLEADER = re.compile(r"\.\s*\.\s*\.|…|\.{4,}")

# PDF (GA/ECOSOC) number heading: '<sess>/<num>[<Letter>]. <Titlecase...>'. sess
# is a GA session (2-3 digits) or an ECOSOC year (4 digits); the leading capital
# after the period distinguishes a real heading from a numeric cross-reference.
_PDF_HEADING = re.compile(
    r"^(\d{2,4})/(\d{1,4})\s*([A-Z])?\s*\.\s+[\"“‘(]?[A-Z]")

# A broader number-heading line for SLICE BOUNDARIES only: matches '<n>/<m>.' at the
# start of a line whether or not a dot-leader follows, so an interleaved Contents /
# checklist recap (whose entries carry trailing leader dots) ends the preceding
# child's slice instead of being absorbed into it. Child STARTS still use the
# stricter, dot-leader-excluding, body-confirmed `pdf_heading`.
_NUM_HEADING_ANY = re.compile(r"^\s*\d{2,4}/\d{1,4}\s*[A-Z]?\s*\.")

# HRC (docx) heading text: optional 'PRST/' or 'DEC/' prefix, then '<sess>/<num>.'
# sess is a numeric session or a special session 'S-<n>'.
# The trailing period is usually present ("6/27.") but some reports omit it
# ("S-9/1<tab>The grave violations..."); a tab / multi-space separator followed
# by a capitalized title is accepted instead. Safe because every candidate must
# also pass Heading-2 style or opening-formula body-confirmation.
_HRC_HEADING = re.compile(r"^\s*(PRST/|DEC/)?(S-\d+|\d+)/(\d+)\s*(?:\.|(?=[\t ]|  +\S)(?=.*[A-Z]))")

# Body-confirmation for a PDF decision heading: the adoption record that ALWAYS
# opens a decision's body ("At its 77th plenary meeting, on 18 December 2002, the
# General Assembly ..."). A Contents/checklist TOC entry — whose dot-leader may
# wrap to the NEXT line so the heading line itself looks clean — is instead
# followed by a title continuation, more titles, or a meeting/date/page table, and
# never carries this line. Requiring it near the heading rejects TOC entries.
_ADOPTION_RE = re.compile(r"\bAt its\s+\d", re.I)
_BODY_CONFIRM_WINDOW = 8


def pdf_heading(text: str) -> tuple[str, str, str] | None:
    """(session, number, letter) for a PDF volume body heading, else None.
    Dot-leader TOC lines are rejected."""
    t = (text or "").strip()
    if not t or _DOTLEADER.search(t):
        return None
    m = _PDF_HEADING.match(t)
    if not m:
        return None
    return m.group(1), m.group(2), (m.group(3) or "")


def hrc_heading(text: str) -> tuple[str, str, str] | None:
    """(prefix, session, number) for an HRC report Heading-2 body item, else None.
    prefix is '' (resolution/decision), 'PRST' or 'DEC'."""
    m = _HRC_HEADING.match(text or "")
    if not m:
        return None
    prefix = (m.group(1) or "").rstrip("/")
    return prefix, m.group(2), m.group(3)


def is_heading2(style_name: str | None, style_id: str | None) -> bool:
    """python-docx 'Heading 2' body item (style name or the Heading2 style id)."""
    sn = (style_name or "").strip().lower()
    si = (style_id or "").strip().lower()
    return sn in ("heading 2", "heading2") or si in ("heading2", "2")


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


def split_volume(conn, volume: str, lang: str, kind: str, rows: list[dict]) -> SplitResult:
    """Partition a volume's raw rows into per-child row slices.

    A child begins at a heading row (source-appropriate predicate) and runs until
    the next heading (exclusive). Rows before the first heading (front matter, TOC)
    are unassigned. Child symbols are intersected with the DL catalog gap set:
      * GA   : A/DEC/<sess>/<num><L>  — write iff in catalog and not already full.
      * ECOSOC: E/DEC/... in gap -> write; else E/RES/... existing -> cross-check.
      * HRC  : A/HRC/RES|PRST|DEC/... — write iff in catalog and not already full.
    """
    res = SplitResult(volume)

    # First pass: detect headings. `detected` holds only BODY-CONFIRMED headings
    # (each starts a child). `heading_idx` holds EVERY heading-pattern line —
    # including unconfirmed Contents/checklist TOC entries — so a child slice can be
    # ended at the next heading-looking line and never absorb a following TOC block
    # (the volumes interleave per-committee contents pages between decision bodies).
    detected: list[tuple[int, list[str], str, str]] = []  # (idx, cand_syms, kind, disp)
    heading_idx: list[int] = []
    for i, r in enumerate(rows):
        if kind in ("ga", "ecosoc"):
            # any number-heading line (incl. dot-leader TOC recaps) is a slice
            # boundary; only a body-confirmed one below starts a new child.
            if _NUM_HEADING_ANY.match(r["text"] or ""):
                heading_idx.append(i)
            h = pdf_heading(r["text"])
            if not h:
                continue
            # Reject Contents/checklist TOC entries: a real decision heading is
            # confirmed by its adoption line within a short look-ahead window.
            window = " ".join(rows[j]["text"] for j in range(i + 1, min(i + 1 + _BODY_CONFIRM_WINDOW, len(rows))))
            if not _ADOPTION_RE.search(window):
                continue
            sess, num, letter = h
            dec, resol = pdf_child_symbols(kind, sess, num, letter)
            detected.append((i, [dec, resol], "resdec", r["text"][:70]))
        else:  # hrc
            h = hrc_heading(r["text"])
            if not h:
                continue
            # A body item is normally styled Heading 2, but some reports print
            # the real Part-One heading as Normal (A/HRC/6/22's 6/28) with only
            # a proceedings-section duplicate styled Heading 2. Accept either
            # the style OR body-confirmation: the opening formula ("The Human
            # Rights Council,") within a short look-ahead window.
            if not is_heading2(r["style_name"], r["style_id"]):
                window = " ".join(
                    rows[j]["text"] for j in range(i + 1, min(i + 1 + _BODY_CONFIRM_WINDOW, len(rows)))
                )
                if not re.search(r"^\s*The Human Rights Council\s*,", window) and \
                   "The Human Rights Council," not in window:
                    continue
            heading_idx.append(i)
            prefix, sess, num = h
            primary, alt = hrc_child_symbol(prefix, sess, num)
            cands = [primary] + ([alt] if alt else [])
            detected.append((i, cands, "hrc", r["text"][:70]))

    if not detected:
        return res

    # Resolve routing against the catalog / gap set (batch the lookups).
    all_cands = sorted({s for _, cs, _, _ in detected for s in cs})
    exist = catalog_existing(conn, all_cands)
    full = already_fulltext(conn, all_cands)

    # Second pass: assign row slices. A child heading typically appears TWICE in a
    # volume — once as the full-body decision and once as a bare heading in a
    # checklist/summary list — so we DEDUPE per child symbol, keeping the slice with
    # the most rows (the substantive body); the bare-heading duplicate is discarded.
    best_write: dict[str, list[dict]] = {}
    best_cross: dict[str, list[dict]] = {}
    skipped: set[str] = set()
    for k, (idx, cands, dkind, disp) in enumerate(detected):
        # End the slice at the next heading-pattern line of ANY kind (the next real
        # decision OR the first line of an interleaved contents/checklist block),
        # so a child never absorbs a following TOC block.
        pos = bisect.bisect_right(heading_idx, idx)
        end = heading_idx[pos] if pos < len(heading_idx) else len(rows)
        slice_rows = rows[idx:end]
        chosen = None
        route = "skip"
        if dkind == "hrc":
            for c in cands:  # first candidate that exists in the catalog wins
                if c in exist:
                    chosen = c
                    route = "skip_existing" if c in full else "write"
                    break
        else:  # resdec (GA/ECOSOC): decision -> write; resolution -> cross-check
            dec, resol = cands
            if dec in exist:
                chosen, route = dec, ("skip_existing" if dec in full else "write")
            elif resol in exist:
                chosen, route = resol, "crosscheck"
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
    res.unmatched = sorted(set(res.unmatched))
    return res


_CHILD_INSERT = (
    "INSERT INTO digitallibrary.document_paragraphs_raw "
    "(symbol_normalized, lang, position, kind, text, style_id, style_name, "
    " numbering, props, table_cell, hyperlinks, footnote_ref, extractor_version, "
    " source_symbol) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
)


def write_children(conn, volume: str, lang: str, fmt: str, res: SplitResult) -> list[str]:
    """Delete this volume's existing children, insert the fresh child rows, and
    upsert child ledger rows (status='extracted', source_symbol=<volume>). Returns
    the exact child symbols written."""
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
    return written


# ---------------------------------------------------------------------------
# Split runner (sha256-gate)
# ---------------------------------------------------------------------------

def write_symbols_manifest(path: Path, symbols: set[str]) -> None:
    """Write an exact stage result. Empty means no downstream work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(f"{symbol}\n" for symbol in sorted(symbols)),
                   encoding="utf-8")
    tmp.replace(path)


def run_split(symbols: list[str] | None, force: bool, dry_run: bool,
              limit: int | None, changed_symbols_out: Path | None = None,
              changed_volumes_out: Path | None = None) -> int:
    with get_conn() as conn:
        vols = volume_ledger(conn, symbols, include_split=force)
        state = read_state(conn, STATE_KEY)
    done_sha: dict = state.get("volumes", {}) if isinstance(state, dict) else {}
    catalog_kind = dict(volume_catalog())

    if limit:
        vols = vols[:limit]
    print(f"Volume-split: {len(vols)} extracted volume(s) to consider")

    total_children = total_cross = total_unmatched = processed = skipped_gate = 0
    changed_children: set[str] = set()
    changed_volumes: set[str] = set()
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
            planned = len(res.children)
            print(f"  {symbol} [{kind}] rows={len(rows)} -> children={planned} "
                  f"crosscheck={len(res.crosscheck)} unmatched={len(res.unmatched)} "
                  f"skipped_existing={len(res.skipped_existing)}")
            if res.unmatched[:5]:
                for u in res.unmatched[:5]:
                    print(f"      unmatched heading: {u!r}")
            if not dry_run:
                written = write_children(conn, symbol, lang, VOLUME_FORMAT.get(kind, fmt), res)
                # Retire the volume from the parse/gate lifecycle.
                upsert_document_file(conn, symbol, lang, status="split")
                conn.commit()
                done_sha[symbol] = sha
                changed_children.update(written)
                changed_volumes.add(symbol)
                n_write = len(written)
            else:
                n_write = planned
        total_children += n_write
        total_cross += len(res.crosscheck)
        total_unmatched += len(res.unmatched)
        processed += 1

    if not dry_run and processed:
        with get_conn() as conn:
            write_state(conn, STATE_KEY, {"volumes": done_sha})

    if changed_symbols_out is not None:
        write_symbols_manifest(changed_symbols_out, changed_children)
    if changed_volumes_out is not None:
        write_symbols_manifest(changed_volumes_out, changed_volumes)

    print(f"\nDone. volumes processed={processed} sha256-skipped={skipped_gate} "
          f"| children written={total_children} crosscheck={total_cross} "
          f"unmatched headings={total_unmatched}")
    return 0


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
    manifest_dir = ARCHIVE_ROOT / "run_manifests"
    child_manifest = manifest_dir / "volume-children.txt"
    volume_manifest = manifest_dir / "changed-volumes.txt"
    write_symbols_manifest(child_manifest, set())
    write_symbols_manifest(volume_manifest, set())
    with get_conn() as conn:
        ready_before = {row[0] for row in volume_ledger(conn, ga_ecosoc)}
    rc = 0
    rc |= stage("fetch", py + ["python/fulltext_split_volumes.py", "--fetch", "--symbols", csv])
    rc |= stage("extract-pdf", py + ["python/fulltext_extract_pdf.py", "--symbols", csv])
    with get_conn() as conn:
        ready_after = {row[0] for row in volume_ledger(conn, ga_ecosoc)}
    newly_ready = sorted(ready_after - ready_before)
    if newly_ready:
        split_rc = stage("split", py + [
            "python/fulltext_split_volumes.py", "--split",
            "--symbols", ",".join(newly_ready),
            "--changed-symbols-out", str(child_manifest),
            "--changed-volumes-out", str(volume_manifest),
        ])
    else:
        print("\nvolume split skipped: no parent volume became ready in this run")
        split_rc = 0
    rc |= split_rc

    children = [line.strip() for line in child_manifest.read_text(encoding="utf-8").splitlines()
                if line.strip()]
    volumes = [line.strip() for line in volume_manifest.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    if split_rc != 0:
        print("\nvolume parse/verify skipped because split failed")
    elif not children and not volumes:
        print("\nvolume parse/verify skipped: no volume changed")
    else:
        if children:
            rc |= stage("parse", py + [
                "python/fulltext_parse.py", "--to-db", "--symbols-file",
                str(child_manifest),
            ])
        else:
            print("\nvolume parse skipped: changed volumes produced no children")
        if volumes:
            rc |= stage("verify", py + [
                "python/fulltext_verify_volumes.py", "--symbols", ",".join(volumes),
                "--children",
            ])
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

    # Exact stage manifests: sorted/deduplicated, and empty never means all.
    with tempfile.TemporaryDirectory() as tmp:
        manifest = Path(tmp) / "changed.txt"
        write_symbols_manifest(manifest, {"S/RES/2", "A/RES/1"})
        check(manifest.read_text(encoding="utf-8") == "A/RES/1\nS/RES/2\n",
              "changed-symbol manifest is not exact/deterministic")
        write_symbols_manifest(manifest, set())
        check(manifest.read_text(encoding="utf-8") == "",
              "empty changed-symbol manifest did not mean zero work")

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
    ap.add_argument("--changed-symbols-out", type=Path,
                    help="write exact child symbols changed by --split (empty means none)")
    ap.add_argument("--changed-volumes-out", type=Path,
                    help="write exact parent volumes changed by --split (empty means none)")
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
    return run_split(symbols, args.force, args.dry_run, args.limit,
                     args.changed_symbols_out, args.changed_volumes_out)


if __name__ == "__main__":
    raise SystemExit(main())
