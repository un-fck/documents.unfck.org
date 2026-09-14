#!/usr/bin/env python3
"""Evaluation harness for the PDF extractor's LAYOUT and LEXICAL fidelity.

Two independent instruments, both TOTAL over whatever document set is given
(no sampling inside a document, no denominator derived from the extractor):

  1. PRESENCE (fabrication detector).  Every emitted paragraph of >=10 words is
     cut into 8-grams; each 8-gram is looked up in a REFERENCE rendering of the
     same PDF produced by a DIFFERENT tool.  A paragraph whose 8-grams are <25%
     findable is 'absent from source' — i.e. the extractor produced a word
     sequence that is in no reading of the page.  This is the instrument the
     2026-07-27 content audit used to measure 6.96% on the PDF path; it is
     reimplemented here so the same number can be recomputed before/after.

     References (a paragraph counts as present if EITHER reading contains it):
       * `pdftotext` (poppler) default mode — a wholly independent codebase with
         its own reading-order/column logic;
       * PyMuPDF block order — the library's own block segmentation.
     Both are column-aware, so a paragraph woven across a column boundary
     appears in NEITHER.  The union is what keeps the instrument from punishing
     a *correct* reading that one reference happens to order differently.

  2. HYPHEN (lexical-damage detector).  Every line-break hyphen decision the
     extractor makes is scored against corpus evidence: a produced token X+Y
     (hyphen deleted) is DAMAGE if the clean Word-path corpus writes 'x-y' and
     essentially never writes 'xy'.  Ships with a fixed probe list of UN terms
     of art ('self-determination', 'non-proliferation', ...) so the check has a
     stable floor that cannot drift with the corpus.

Nothing here writes to the database or the archive.  Extraction is run in
memory; --old additionally runs a snapshot of the pre-fix extractor for a
before/after comparison.

Usage:
    uv run python python/fulltext_eval_layout.py --sample 400 --seed 1
    uv run python python/fulltext_eval_layout.py --sample 400 --seed 1 --old /tmp/extract_pdf_old.py
    uv run python python/fulltext_eval_layout.py --symbols 'A/RES/1514(XV)' --dump
    uv run python python/fulltext_eval_layout.py --self-test
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import fitz

from fulltext_common import ARCHIVE_ROOT, get_conn

import fulltext_extract_pdf as NEW

NGRAM = 8
MIN_WORDS = 10
PRESENT_AT = 0.25          # audit's bar: <25% of 8-grams findable => absent


# ---------------------------------------------------------------------------
# Normalisation — identical on both sides, so neither side can be advantaged.
# ---------------------------------------------------------------------------

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
_QUOTES = {ord(c): "'" for c in "‘’‚‛´`"}
_QUOTES.update({ord(c): '"' for c in "“”„«»"})


def normalize(text: str) -> str:
    """Fold case/accents/quotes/dashes, DELETE every hyphen (so a hyphen
    decision is invisible to the presence test — hyphens are measured by the
    separate hyphen instrument), collapse whitespace."""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.translate(_DASHES).translate(_QUOTES)
    t = t.replace("­", "")
    # Every hyphen and the whitespace that may follow it disappears, so
    # 'self-\ndetermination', 'self- determination' and 'selfdetermination'
    # all fold to the same string on BOTH sides.
    t = re.sub(r"-\s*", "", t)
    t = t.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tokens(text: str) -> list[str]:
    return normalize(text).split()


def ngrams(toks: list[str], n: int = NGRAM) -> list[str]:
    if len(toks) < n:
        return [" ".join(toks)] if toks else []
    return [" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)]


# ---------------------------------------------------------------------------
# Reference renderings (independent of the extractor under test)
# ---------------------------------------------------------------------------

def ref_pdftotext(path: Path) -> str:
    try:
        out = subprocess.run(["pdftotext", "-q", str(path), "-"],
                             capture_output=True, timeout=180)
        return out.stdout.decode("utf-8", "replace")
    except Exception:
        return ""


def ref_pymupdf_blocks(path: Path) -> str:
    parts: list[str] = []
    with fitz.open(path) as doc:
        for page in doc:
            parts.append(page.get_text("text"))
    return "\n".join(parts)


@dataclass
class Reference:
    haystacks: list[str]

    @classmethod
    def build(cls, path: Path) -> "Reference":
        hs = []
        for raw in (ref_pdftotext(path), ref_pymupdf_blocks(path)):
            if raw.strip():
                hs.append(" " + normalize(raw) + " ")
        return cls(hs)

    def findable(self, gram: str) -> bool:
        return any(gram in h for h in self.haystacks)

    def score(self, text: str) -> tuple[float, int]:
        toks = tokens(text)
        gs = ngrams(toks)
        if not gs:
            return 1.0, 0
        hit = sum(1 for g in gs if self.findable(g))
        return hit / len(gs), len(toks)


# ---------------------------------------------------------------------------
# Hyphen instrument
# ---------------------------------------------------------------------------

# UN terms of art whose printed form is hyphenated.  A produced token equal to
# the de-hyphenated concatenation is proven damage (the audit's own probes).
HYPHEN_PROBES = [
    "self-determination", "self-government", "self-governing", "self-defence",
    "self-reliance", "self-help", "non-proliferation", "non-aligned",
    "non-nuclear", "non-governmental", "non-self-governing", "non-compliance",
    "non-discrimination", "non-interference", "non-intervention",
    "co-operation", "co-ordination", "co-operate", "co-ordinate",
    "co-sponsors", "co-chairs", "long-term", "short-term", "mid-term",
    "medium-term", "high-level", "low-income", "well-being", "follow-up",
    "policy-making", "decision-making", "peace-keeping", "peace-building",
    "cease-fire", "world-wide", "in-depth", "so-called", "vice-chairman",
    "vice-president", "ad-hoc", "one-time", "third-country", "anti-personnel",
    "inter-agency", "inter-governmental", "socio-economic", "post-conflict",
    "pre-session", "re-establish", "cross-border", "multi-year", "two-thirds",
]
DAMAGED_FORMS = {p.replace("-", ""): p for p in HYPHEN_PROBES}

# The probe list above was written from the audit's findings, before any corpus
# evidence existed. Some of its entries are genuinely CONTESTED in the pre-1994
# corpus: those volumes print both 'peace-keeping' (358 within-line occurrences)
# and 'peacekeeping' (2,048), and no rule can be right about every instance.
# Splitting the list by the era's own printed evidence keeps the check honest in
# both directions — the unambiguous compounds must never be damaged (a hard
# bar), and the contested ones are reported with their ratio rather than scored.
STRICT_MIN_RATIO = 0.90


def split_probes(lexicon: dict[tuple[str, str], tuple[int, int]],
                 ) -> tuple[dict[str, str], dict[str, tuple[str, float]]]:
    strict: dict[str, str] = {}
    contested: dict[str, tuple[str, float]] = {}
    for probe in HYPHEN_PROBES:
        segs = probe.split("-")
        pair = (segs[-2], segs[-1])
        h, j = lexicon.get(pair, (0, 0))
        joined = probe.replace("-", "")
        if h + j >= 5:
            ratio = h / (h + j)
            (strict.__setitem__(joined, probe) if ratio >= STRICT_MIN_RATIO
             else contested.__setitem__(joined, (probe, ratio)))
        elif segs[-2] in ("self", "non", "ex", "quasi", "pseudo", "socio", "vice"):
            strict[joined] = probe          # a prefix the UN always hyphenates
        else:
            contested[joined] = (probe, float("nan"))
    return strict, contested


def hyphen_damage(rows: list[dict], strict: dict[str, str],
                  contested: dict[str, tuple[str, float]],
                  ) -> tuple[dict[str, int], dict[str, int]]:
    """Count de-hyphenated occurrences, split into strict damage and contested."""
    hard: dict[str, int] = {}
    soft: dict[str, int] = {}
    for r in rows:
        for w in re.findall(r"[A-Za-z]+", r.get("text") or ""):
            wl = w.lower()
            if wl in strict:
                hard[strict[wl]] = hard.get(strict[wl], 0) + 1
            elif wl in contested:
                name = contested[wl][0]
                soft[name] = soft.get(name, 0) + 1
    return hard, soft


def adjudicate(counts: dict[str, int], h: dict[tuple[str, str], int],
               j: dict[str, int]) -> dict[str, int]:
    """Judge each produced joined form against THIS document's own printed
    spelling, counted from tokens that lie wholly inside one line (where no join
    can have happened). A joined form the document also prints inside a line is
    correct; one the document only ever prints hyphenated is damage."""
    out = {"contradicted": 0, "confirmed": 0, "unknown": 0}
    for probe, n in counts.items():
        segs = probe.split("-")
        pair = (segs[-2], segs[-1])
        hyphenated = h.get(pair, 0)
        joined = j.get(probe.replace("-", ""), 0)
        if hyphenated and not joined:
            out["contradicted"] += n
        elif joined:
            out["confirmed"] += n
        else:
            out["unknown"] += n
    return out


# ---------------------------------------------------------------------------
# Hyphen lexicon builder (era-matched, contamination-free)
# ---------------------------------------------------------------------------
# The question a line-break hyphen poses — did the page print 'self-determination'
# or 'selfdetermination'? — is answered from how the SAME era's pages spell the
# compound WITHIN a line, where no join ever happened. Counting stored corpus
# text instead would be circular: every token this extractor damaged is stored
# as the joined form, so the joined counts include our own errors. Counting only
# tokens that sit wholly inside one physical PDF line cannot be contaminated.

_HYPH_TOKEN = re.compile(r"[A-Za-z]{2,}(?:-[A-Za-z]{2,})+")
_PLAIN_TOKEN = re.compile(r"[A-Za-z]{4,}")


def count_inline_tokens(lines, h: dict, j: dict) -> None:
    """Count hyphenated pairs and plain words that lie WITHIN a single line."""
    for ln in lines:
        text = ln.text if hasattr(ln, "text") else str(ln)
        t = text.strip()
        if t.endswith("-"):
            # the final token is a line-break fragment: never count it
            t = t[:t.rfind(" ")] if " " in t else ""
        for m in _HYPH_TOKEN.finditer(t):
            segs = m.group(0).lower().split("-")
            for a, b in zip(segs, segs[1:]):
                h[(a, b)] = h.get((a, b), 0) + 1
        for m in _PLAIN_TOKEN.finditer(t):
            w = m.group(0).lower()
            j[w] = j.get(w, 0) + 1


def build_hyphen_lexicon(sample: int, seed: int, out_path: Path,
                         word_vocab: dict[str, int] | None = None) -> None:
    targets = load_targets(None, sample, seed)
    print(f"lexicon: reading {len(targets)} PDFs", flush=True)
    h: dict[tuple[str, str], int] = {}
    j: dict[str, int] = {}
    for i, (symbol, lang, rel) in enumerate(targets):
        try:
            with fitz.open(ARCHIVE_ROOT / rel) as doc:
                for pno in range(doc.page_count):
                    count_inline_tokens(NEW.extract_lines(doc[pno], pno), h, j)
        except Exception as exc:
            print(f"  ! {symbol}: {exc}")
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(targets)} pairs={len(h)}", flush=True)
    lines_out = ["# pair lexicon: <left> <right> <hyphenated> <joined>",
                 f"# source: {len(targets)} pre-1994 PDFs, within-line tokens only, seed={seed}"]
    for (a, b), n in sorted(h.items(), key=lambda kv: -kv[1]):
        if n < 3:
            continue
        joined = j.get(a + b, 0)
        if word_vocab is not None:
            joined += 0
        lines_out.append(f"{a} {b} {n} {joined}")
    out_path.write_text("\n".join(lines_out) + "\n")
    print(f"wrote {out_path} ({len(lines_out) - 2} pairs)")


# ---------------------------------------------------------------------------
# Per-document scoring
# ---------------------------------------------------------------------------

@dataclass
class DocScore:
    symbol: str
    paras: int = 0
    absent: int = 0
    words: int = 0
    hyphen: dict[str, int] = field(default_factory=dict)
    hyphen_contested: dict[str, int] = field(default_factory=dict)
    hyphen_verdict: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    flagged: list[tuple[str, float]] = field(default_factory=list)
    cols: int = 1
    dropped_lang: int = 0


def score_rows(symbol: str, rows: list[dict], ref: Reference,
               probes: tuple[dict, dict], keep_flagged: int = 3) -> DocScore:
    ds = DocScore(symbol)
    for r in rows:
        if r["kind"] == "empty":
            continue
        text = r["text"] or ""
        toks = tokens(text)
        if len(toks) < MIN_WORDS:
            continue
        frac, nw = ref.score(text)
        ds.paras += 1
        ds.words += nw
        if frac < PRESENT_AT:
            ds.absent += 1
            if len(ds.flagged) < keep_flagged:
                ds.flagged.append((text[:220], round(frac, 3)))
    ds.hyphen, ds.hyphen_contested = hyphen_damage(rows, probes[0], probes[1])
    return ds


# ---------------------------------------------------------------------------
# CROP instrument — does the output cover its own printed extent, and only that?
# ---------------------------------------------------------------------------
# The region is read from the SOURCE FILE alone: the line printing this
# document's own resolution number, up to the next line printing a different
# one. Written independently of the extractor's crop (different regexes, a
# different text rendering — poppler lines rather than pymupdf geometry), so a
# bug shared with the extractor cannot hide here.

_HEAD_SLASH = re.compile(r"^\s*(S-\d{1,2}|\d{1,4})\s*/\s*(\d{1,4})\s*[A-Z]?\s*[.．]")
_HEAD_PAREN = re.compile(
    r"^\s*(?:Resolutions?\s+)?(\d{1,4})\s*[A-Z]?\s*[\(\[][^)\]]{1,14}[\)\]]"
    r"(?:\s*[.．,]\s*[\"“'(]?[A-Z]|\s*$)")
_SYM_SLASH = re.compile(r"(?:^|/)(S-\d{1,2}|\d{1,4})/(\d{1,4})\s*[A-Z]?$")
_SYM_PAREN = re.compile(r"(?:^|/)(\d{1,4})\s*[A-Z]?\s*\([^()]+\)$")


def _sym_key(symbol: str) -> tuple[str, str] | None:
    s = (symbol or "").upper().replace(" ", "")
    m = _SYM_SLASH.search(s)
    if m:
        return ("slash", f"{m.group(1)}/{m.group(2)}")
    m = _SYM_PAREN.search(s)
    if m:
        return ("paren", m.group(1))
    return None


def _line_key(line: str) -> tuple[str, str] | None:
    m = _HEAD_SLASH.match(line)
    if m:
        return ("slash", f"{m.group(1)}/{m.group(2)}")
    m = _HEAD_PAREN.match(line)
    if m:
        return ("paren", m.group(1))
    return None


def _region_of(lines: list[str], symbol: str) -> tuple[int, int, str]:
    heads = [(i, k) for i, k in ((j, _line_key(ln)) for j, ln in enumerate(lines)) if k]
    key = _sym_key(symbol)
    own = [i for i, k in heads if key is not None and k == key]
    if own:
        # Several occurrences: the table of contents lists every resolution, so
        # prefer the one that opens a BODY (an organ formula follows within a few
        # lines); otherwise the last, which is never the contents page.
        body = [i for i in own
                if any(ln.strip().startswith("The ") and ln.strip().endswith(",")
                       for ln in lines[i:i + 10])]
        start = body[0] if body else own[-1]
        nxt = [i for i, k in heads if i > start and k != key]
        return start, (nxt[0] if nxt else len(lines)), "heading"
    if heads and heads[0][0] > 0:
        return 0, heads[0][0], "lead"
    if heads:
        return 0, 0, "unlocatable"
    return 0, len(lines), "whole"


def crop_scores(path: Path, symbol: str, rows: list[dict]) -> dict:
    """Recall of the printed region, and contamination from outside it.

    TWO renderings of the source are tried — poppler's reading order and
    PyMuPDF's block order — because on a two-column page one of them can
    interleave the columns, which puts the NEIGHBOURING column's heading
    immediately after the target's and shrinks the region to four lines. The
    rendering that finds a real next heading and yields the larger region is the
    one that read the columns; a region of a handful of tokens is an instrument
    failure, not a finding, and is reported as `mode='degenerate'`.
    """
    best: tuple[int, list[str], int, int, str] | None = None
    for raw in (ref_pdftotext(path), ref_pymupdf_blocks(path)):
        lines = raw.splitlines()
        if not lines:
            continue
        st, en, md = _region_of(lines, symbol)
        size = len(tokens(" ".join(lines[st:en])))
        bounded = en < len(lines)
        cand = (size + (10 ** 6 if bounded else 0), lines, st, en, md)
        if best is None or cand[0] > best[0]:
            best = cand
    if best is None:
        return {"mode": "unreadable"}
    _, lines, start, end, mode = best
    if mode == "unlocatable":
        return {"mode": "unlocatable"}

    inside = set(tokens(" ".join(lines[start:end])))
    outside = set(tokens(" ".join(lines[:start] + lines[end:]))) - inside
    got = tokens(" ".join(r["text"] for r in rows if r["kind"] != "empty"))
    if len(inside) < 40:
        return {"mode": "degenerate", "region_types": len(inside)}
    covered = sum(1 for t in set(got) if t in inside)
    foreign = [t for t in got if t in outside]
    return {
        "mode": mode,
        "region_types": len(inside),
        "recall": round(covered / len(inside), 4),
        "out_tokens": len(foreign),
        "out_share": round(len(foreign) / max(len(got), 1), 4),
    }


# ---------------------------------------------------------------------------
# Corpus loading
# ---------------------------------------------------------------------------

def load_targets(symbols: list[str] | None, sample: int | None, seed: int):
    sql = ("SELECT symbol_normalized, lang, archive_path "
           "FROM digitallibrary.document_files "
           "WHERE format = 'pdf' AND archive_path IS NOT NULL "
           "AND status <> 'no_text_layer'")
    params: list[object] = []
    if symbols:
        sql += " AND symbol_normalized = ANY(%s)"
        params.append(symbols)
    sql += " ORDER BY symbol_normalized"
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    rows = [r for r in rows if (ARCHIVE_ROOT / r[2]).exists()]
    if sample and sample < len(rows):
        rows = random.Random(seed).sample(rows, sample)
        rows.sort()
    return rows


def load_module(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Self-test — negative controls for the INSTRUMENT itself.
# ---------------------------------------------------------------------------

def _self_test() -> int:
    fails: list[str] = []

    class FakeRef(Reference):
        pass

    src = ("Convinced that all peoples have an inalienable right to complete "
           "freedom, the exercise of their sovereignty and the integrity of "
           "their national territory, any distinction as to race, creed or "
           "colour, in order to enable them to enjoy complete independence "
           "and freedom.")
    ref = Reference([" " + normalize(src) + " "])

    # (a) A verbatim paragraph must score present.
    good = "Convinced that all peoples have an inalienable right to complete freedom, the exercise of their sovereignty and the integrity of their national territory,"
    frac, _ = ref.score(good)
    if frac < 0.999:
        fails.append(f"self-test a: verbatim paragraph scored {frac:.3f} (<1.0)")

    # (b) The real A/RES/1514(XV) weave must score ABSENT — the instrument is
    #     proven to fail on damaged input, not merely to stay quiet on clean.
    woven = ("Convinced that all peoples have an inalienable right any distinction "
             "as to race, creed or colour, in order to to complete freedom, the "
             "exercise of their sovereignty enable them to enjoy complete independence and")
    frac, _ = ref.score(woven)
    if frac >= PRESENT_AT:
        fails.append(f"self-test b: known-fabricated weave scored {frac:.3f} (>= {PRESENT_AT})")

    # (c) Hyphen normalisation must NOT hide a weave, and must hide a hyphen.
    if normalize("self-determination") != normalize("self- determination"):
        fails.append("self-test c: hyphen normalisation asymmetric")

    # (d) The hyphen instrument must fire on damage and stay quiet on the
    #     correct form.
    probes = split_probes(NEW.load_pair_lexicon())
    if "selfdetermination" not in probes[0]:
        fails.append("self-test d0: 'self-determination' is not a STRICT probe")
    dmg, _ = hyphen_damage([{"kind": "paragraph", "text": "the right to selfdetermination"}],
                           *probes)
    if dmg.get("self-determination") != 1:
        fails.append(f"self-test d: hyphen probe missed damage -> {dmg}")
    clean, _ = hyphen_damage([{"kind": "paragraph", "text": "the right to self-determination"}],
                             *probes)
    if clean:
        fails.append(f"self-test d2: hyphen probe fired on correct text -> {clean}")

    for m in fails:
        print(f"  FAIL: {m}")
    if fails:
        print(f"eval self-test: {len(fails)} FAILED")
        return 1
    print("eval self-test: 4/4 passed (verbatim present; known weave absent; "
          "hyphen folding symmetric; hyphen probe fires on damage only)")
    return 0


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="PDF extractor layout/lexical evaluation")
    ap.add_argument("--sample", type=int, help="random N documents")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--symbols", help="comma-separated symbols instead of a sample")
    ap.add_argument("--old", help="path to a snapshot extractor to score alongside")
    ap.add_argument("--dump", action="store_true", help="print every flagged paragraph")
    ap.add_argument("--out", help="write per-document JSON here")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--build-hyphen-lexicon", metavar="PATH",
                    help="scan --sample PDFs and write the pair lexicon here")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    if args.build_hyphen_lexicon:
        build_hyphen_lexicon(args.sample or 1500, args.seed,
                             Path(args.build_hyphen_lexicon))
        return 0

    probes = split_probes(NEW.load_pair_lexicon())
    print(f"hyphen probes: {len(probes[0])} strict (era evidence >= {STRICT_MIN_RATIO:.0%} "
          f"hyphenated), {len(probes[1])} contested: "
          + ", ".join(f"{n}={r:.2f}" for n, r in sorted(probes[1].values())[:8]))
    old_mod = load_module(args.old, "extract_pdf_old") if args.old else None
    symbols = [s.strip() for s in args.symbols.split(",")] if args.symbols else None
    targets = load_targets(symbols, args.sample, args.seed)
    print(f"scoring {len(targets)} documents"
          f"{' (new + old)' if old_mod else ''}", flush=True)

    results: dict[str, list[DocScore]] = {"new": [], "old": []}
    for i, (symbol, lang, rel) in enumerate(targets):
        path = ARCHIVE_ROOT / rel
        ref = Reference.build(path)
        for tag, mod in (("new", NEW), ("old", old_mod)):
            if mod is None:
                continue
            try:
                res = mod.extract_pdf(path, symbol)
                ds = score_rows(symbol, res.rows, ref, probes)
                ds.cols = getattr(res, "n_columns", 1)
                ds.dropped_lang = getattr(res, "french_dropped", 0)
                if ds.hyphen or ds.hyphen_contested:
                    h: dict = {}
                    j: dict = {}
                    with fitz.open(path) as doc:
                        for pno in range(doc.page_count):
                            count_inline_tokens(NEW.extract_lines(doc[pno], pno), h, j)
                    ds.hyphen_verdict = adjudicate({**ds.hyphen, **ds.hyphen_contested},
                                                   h, j)
            except Exception as exc:
                ds = DocScore(symbol, error=f"{type(exc).__name__}: {exc}")
            results[tag].append(ds)
        if (i + 1) % 25 == 0:
            print(f"  {i+1}/{len(targets)}", flush=True)

    for tag in ("new", "old"):
        rs = results[tag]
        if not rs:
            continue
        paras = sum(r.paras for r in rs)
        absent = sum(r.absent for r in rs)
        errs = [r for r in rs if r.error]
        docs_bad = sum(1 for r in rs if r.absent)
        hyph: dict[str, int] = {}
        soft: dict[str, int] = {}
        for r in rs:
            for k, v in r.hyphen.items():
                hyph[k] = hyph.get(k, 0) + v
            for k, v in r.hyphen_contested.items():
                soft[k] = soft.get(k, 0) + v
        print(f"\n=== {tag.upper()} ===")
        print(f"documents            {len(rs)} (errors {len(errs)})")
        print(f"paragraphs >=10w     {paras}")
        print(f"absent from source   {absent}  = {100*absent/max(paras,1):.2f}%")
        print(f"documents with >=1   {docs_bad} = {100*docs_bad/max(len(rs),1):.2f}%")
        print(f"hyphen damage (strict) {sum(hyph.values())} "
              f"{dict(sorted(hyph.items(), key=lambda kv: -kv[1])[:12])}")
        print(f"hyphen contested       {sum(soft.values())} "
              f"{dict(sorted(soft.items(), key=lambda kv: -kv[1])[:12])}")
        verdict: dict[str, int] = {}
        for r in rs:
            for k, v in r.hyphen_verdict.items():
                verdict[k] = verdict.get(k, 0) + v
        print(f"…judged against each document's own within-line spelling: {verdict}")
        for r in errs[:10]:
            print(f"  ERROR {r.symbol}: {r.error}")
        if args.dump:
            for r in rs:
                for text, frac in r.flagged:
                    print(f"  [{tag}] {r.symbol} ({frac}): {text}")

    if args.out:
        payload = {tag: [vars(r) for r in rs] for tag, rs in results.items() if rs}
        Path(args.out).write_text(json.dumps(payload, indent=1))
        print(f"\nwrote {args.out}")

    new = results["new"]
    paras = sum(r.paras for r in new)
    absent = sum(r.absent for r in new)
    rate = 100 * absent / max(paras, 1)
    hyph_total = sum(sum(r.hyphen.values()) for r in new)
    soft_total = sum(sum(r.hyphen_contested.values()) for r in new)
    contradicted = sum(r.hyphen_verdict.get("contradicted", 0) for r in new)
    verdict = "PASS" if (rate <= 0.60 and hyph_total == 0 and contradicted == 0) else "FAIL"
    print(f"\n{verdict} — {paras} paragraphs, {absent} absent ({rate:.2f}%), "
          f"{hyph_total} strictly hyphen-damaged tokens "
          f"({soft_total} contested-spelling tokens, of which {contradicted} are "
          f"contradicted by their own document) "
          f"(bar: <=0.60% absent [the Word-path noise floor], 0 strict hyphen "
          f"damage, 0 contradicted)")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
