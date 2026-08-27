#!/usr/bin/env python3
"""Adversarial NEGATIVE-CONTROL suite for the full-text verification gates.

A check that has never been shown to fail is ABSENT, not passing. This script
damages inputs in the specific ways each gate exists to catch, runs the gate,
and records whether the gate's own failure signal (exit code) moved.

Every control also has its undamaged twin (the BASELINE controls): a check that
fires on everything is as useless as one that fires on nothing.

SAFETY
------
  * Production Postgres is read with SELECT only. Never written.
  * The SSD archive (original/, converted/, parsed_dev/) is read only. Damaged
    copies of parsed JSON live in a scratch dir; damaged archive trees are built
    from SYMLINKS to the real files.
  * All DB damage happens in a SCRATCH cluster addressed by ADV_DATABASE_URL,
    which the script refuses to use if it looks like the production URL.
  * Gates that write TSVs into <archive>/audit/ are always given an explicit
    --output/--tsv under the scratch dir.

USAGE
-----
    # one-time: a throwaway local cluster
    initdb -D /tmp/advpg -U adv --auth=trust
    pg_ctl -D /tmp/advpg -o "-p 55432" start
    createdb -h 127.0.0.1 -p 55432 -U adv advtest

    export ADV_DATABASE_URL='postgresql://adv@127.0.0.1:55432/advtest'
    uv run python python/fulltext_negative_controls.py --seed
    uv run python python/fulltext_negative_controls.py            ; echo "rc=$?"
    uv run python python/fulltext_negative_controls.py --only V-  # prefix filter

Exit code: 0 iff every control's observed verdict equals its expectation
(i.e. the suite itself is green — which is NOT the same as the gates being
good; a control whose expectation is "MISS" is a recorded hole).
Never pipe this script: `... | tail` returns tail's status.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parent.parent
PY = REPO_ROOT / "python"
sys.path.insert(0, str(PY))

from fulltext_common import ARCHIVE_ROOT, sanitize_symbol  # noqa: E402

SCRATCH = Path(os.getenv("ADV_SCRATCH", "/tmp/fulltext_adv"))
REAL_PARSED = ARCHIVE_ROOT / "parsed_dev"

# ---------------------------------------------------------------------------
# Fixtures: a small, real subset copied into the scratch DB.
# ---------------------------------------------------------------------------

DOCX_SYMS = ["S/RES/2825(2026)", "S/RES/2824(2026)", "S/RES/2823(2026)"]
# A BASELINE FIXTURE MUST BE A DOCUMENT THE GATE PASSES. The first version of
# this suite used A/RES/1000(ES-I), A/RES/1001(ES-I) and A/RES/1002(ES-I), all of
# which the repaired PDF gate fails on their own merits: the pymupdf path fuses
# text across columns into words that exist nowhere in the file
# ('i111ple111enresolution', 'asresolution'), and the crop for A/RES/1005(ES-II)
# swallows the whole preamble of the NEIGHBOURING resolution printed above it.
# Grading damage against an already-failing fixture cannot distinguish the damage
# from the pre-existing defect, so the fixtures below are three documents that
# pass the repaired gate cleanly (chosen from a full-corpus run, largest regions
# first, so a deletion control has something to delete).
PDF_SYMS = ["A/RES/1201(XII)", "A/RES/1707(XVI)", "A/RES/1013(XI)"]
VOLUME = "A/78/49(VOL.II)"
STRUCT_SYMS = ["A/RES/79/1", "A/RES/70/1", "A/RES/69/313"]

ALL_LEAF_SYMS = DOCX_SYMS + PDF_SYMS + STRUCT_SYMS


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class Control:
    name: str
    gate: str
    damage: str
    expected: str          # 'DETECT' — the gate must fail;  'quiet' — must not fail
    observed: str = ""
    verdict: str = ""      # DETECTED / MISSED / (baseline) ok / noisy
    detail: str = ""
    skipped: bool = False


RESULTS: list[Control] = []


def record(c: Control) -> Control:
    RESULTS.append(c)
    mark = {"DETECTED": "DETECTED", "MISSED": "MISSED"}.get(c.verdict, c.verdict)
    print(f"  [{mark:<9}] {c.name:<22} {c.observed}")
    if c.detail:
        print(f"                 {c.detail}")
    return c


def judge_signal(c: Control, rc: int, sig, base_rc: int, base_sig) -> Control:
    """For a gate that is ALREADY RED on undamaged input, the exit code carries no
    information — so the control is judged on whether the gate's own finding
    counts MOVED. This is the honest reading of "the check's failure signal must
    move"."""
    if base_rc == 0:
        return judge(c, rc != 0)
    moved = sig != base_sig
    c.verdict = "DETECTED" if moved else "MISSED"
    c.detail = (c.detail + f"  [baseline already red: signal {base_sig} -> {sig}]").strip()
    return c


def judge(c: Control, gate_failed: bool) -> Control:
    """gate_failed = the gate exited non-zero (its own failure signal moved)."""
    if c.expected == "DETECT":
        c.verdict = "DETECTED" if gate_failed else "MISSED"
    else:  # baseline: the gate must stay quiet on undamaged input
        c.verdict = "ok" if not gate_failed else "noisy"
    return c


# ---------------------------------------------------------------------------
# Running a gate: exit code captured directly. NEVER through a pipe.
# ---------------------------------------------------------------------------

def run_gate(script: str, args: list[str], env_extra: dict[str, str] | None = None,
             inproc_patch: str | None = None) -> tuple[int, str]:
    """Run a gate as a subprocess; return (returncode, combined output).

    inproc_patch: python source executed before main() for the two gates that
    hardcode their .env path and therefore cannot be pointed at the scratch DB
    by environment alone. Only the connection factory is replaced.
    """
    env = {**os.environ, **(env_extra or {})}
    if inproc_patch:
        mod = Path(script).stem
        code = (
            "import sys; sys.path.insert(0, 'python');\n"
            f"import {mod} as m\n"
            "import psycopg, os\n"
            f"{inproc_patch}\n"
            "raise SystemExit(m.main())\n"
        )
        cmd = ["uv", "run", "python", "-c", code, *args]
    else:
        cmd = ["uv", "run", "python", script, *args]
    p = subprocess.run(cmd, cwd=REPO_ROOT, env=env,
                       capture_output=True, text=True, timeout=1800)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ---------------------------------------------------------------------------
# Scratch DB
# ---------------------------------------------------------------------------

def adv_url() -> str:
    u = os.getenv("ADV_DATABASE_URL")
    if not u:
        sys.exit("ADV_DATABASE_URL is required (a throwaway local cluster).")
    prod = os.getenv("DATABASE_URL", "")
    if prod and u.split("?")[0] == prod.split("?")[0]:
        sys.exit("REFUSING: ADV_DATABASE_URL equals DATABASE_URL.")
    if "postgres.database.azure.com" in u or "azure" in u:
        sys.exit("REFUSING: ADV_DATABASE_URL looks like production Azure.")
    return u


def adv_conn() -> psycopg.Connection:
    c = psycopg.connect(adv_url())
    c.autocommit = True
    return c


def prod_conn() -> psycopg.Connection:
    """Read-only production connection (SELECT only, enforced by convention and
    by opening the session as read-only)."""
    from fulltext_common import get_conn
    c = get_conn()
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("SET default_transaction_read_only = on")
    return c


DDL = """
DROP SCHEMA IF EXISTS digitallibrary CASCADE;
DROP SCHEMA IF EXISTS mandates CASCADE;
CREATE SCHEMA digitallibrary;
CREATE SCHEMA mandates;

CREATE TABLE digitallibrary.documents (
  symbol_normalized TEXT PRIMARY KEY, deleted_at TIMESTAMPTZ);

CREATE TABLE digitallibrary.document_files (
  symbol_normalized TEXT NOT NULL, lang TEXT NOT NULL DEFAULT 'en',
  format TEXT, content_type TEXT, size_bytes BIGINT, sha256 TEXT, ods_url TEXT,
  archive_path TEXT, converted_path TEXT, converter TEXT, status TEXT NOT NULL,
  error TEXT, fetched_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  source_symbol TEXT, PRIMARY KEY (symbol_normalized, lang));

CREATE TABLE digitallibrary.document_paragraphs_raw (
  symbol_normalized TEXT NOT NULL, lang TEXT NOT NULL DEFAULT 'en',
  position INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
  style_id TEXT, style_name TEXT, numbering JSONB, props JSONB, table_cell JSONB,
  hyperlinks JSONB, footnote_ref JSONB, extractor_version TEXT NOT NULL,
  extracted_at TIMESTAMPTZ NOT NULL DEFAULT now(), source_symbol TEXT,
  PRIMARY KEY (symbol_normalized, lang, position));

CREATE TABLE digitallibrary.document_paragraphs (
  symbol_normalized TEXT NOT NULL, lang TEXT NOT NULL DEFAULT 'en',
  position INTEGER NOT NULL, id UUID NOT NULL, type TEXT NOT NULL, subtype TEXT,
  section TEXT NOT NULL DEFAULT 'main', annex_index SMALLINT,
  text_index SMALLINT NOT NULL DEFAULT 1, paragraph_type TEXT, level SMALLINT,
  heading_level SMALLINT, prefix TEXT, lead_verb TEXT, text TEXT NOT NULL,
  raw_positions INTEGER[] NOT NULL, inferred_operative BOOLEAN NOT NULL DEFAULT false,
  vote JSONB, vote_summary JSONB, hyperlinks JSONB, note_ids JSONB,
  parser_version TEXT NOT NULL, parsed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  action_verb TEXT, action_verb_normalized TEXT, action_category TEXT,
  action_force SMALLINT, action_sentiment SMALLINT, action_bindingness TEXT,
  action_budget_relevant BOOLEAN, action_modifiers JSONB, assignee TEXT,
  assignee_head_noun TEXT, assignee_class TEXT, action_inherited BOOLEAN,
  action_context_marker TEXT,
  PRIMARY KEY (symbol_normalized, lang, position));

CREATE TABLE digitallibrary.document_parses (
  symbol_normalized TEXT NOT NULL, lang TEXT NOT NULL DEFAULT 'en',
  parser_version TEXT NOT NULL, format TEXT, element_count INTEGER NOT NULL,
  dropped JSONB NOT NULL DEFAULT '[]'::jsonb, issues JSONB NOT NULL DEFAULT '[]'::jsonb,
  parsed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol_normalized, lang));

CREATE TABLE digitallibrary.harvest_state (
  key TEXT PRIMARY KEY, value JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE TABLE mandates.paragraphs (
  document_symbol TEXT, type TEXT, text TEXT);
"""


JSONB_OID = 3802


def _wrap_json(cur, rows: list) -> list:
    """Re-wrap jsonb columns so a fetched row can be re-inserted verbatim."""
    from psycopg.types.json import Jsonb
    ix = {i for i, d in enumerate(cur.description) if d.type_code == JSONB_OID}
    return [tuple(Jsonb(v) if (i in ix and v is not None) else v
                  for i, v in enumerate(r)) for r in rows]


def _copy(src: psycopg.Connection, dst: psycopg.Connection, table: str,
          where: str, params: list) -> int:
    from psycopg.types.json import Jsonb
    with src.cursor() as scur:
        scur.execute(f"SELECT * FROM {table} WHERE {where}", params)
        cols = [d[0] for d in scur.description]
        jsonb_ix = {i for i, d in enumerate(scur.description) if d.type_code == JSONB_OID}
        rows = scur.fetchall()
    if not rows:
        return 0
    rows = [tuple(Jsonb(v) if (i in jsonb_ix and v is not None) else v
                  for i, v in enumerate(r)) for r in rows]
    ph = ", ".join(["%s"] * len(cols))
    with dst.cursor() as dcur:
        dcur.executemany(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph}) ON CONFLICT DO NOTHING",
            rows)
    return len(rows)


def seed() -> None:
    print(f"Seeding scratch DB {adv_url().split('@')[-1]} from production (read-only) ...")
    dst = adv_conn()
    with dst.cursor() as cur:
        cur.execute(DDL)
    src = prod_conn()

    with src.cursor() as cur:
        cur.execute("SELECT symbol_normalized FROM digitallibrary.document_files "
                    "WHERE source_symbol = %s", [VOLUME])
        children = [r[0] for r in cur.fetchall()]
    print(f"  volume {VOLUME}: {len(children)} children")

    syms = ALL_LEAF_SYMS + [VOLUME] + children
    n = 0
    n += _copy(src, dst, "digitallibrary.document_files",
               "symbol_normalized = ANY(%s)", [syms])
    n += _copy(src, dst, "digitallibrary.document_paragraphs_raw",
               "symbol_normalized = ANY(%s)", [syms])
    n += _copy(src, dst, "digitallibrary.document_paragraphs",
               "symbol_normalized = ANY(%s)", [syms])
    n += _copy(src, dst, "digitallibrary.document_parses",
               "symbol_normalized = ANY(%s)", [syms])
    # catalog rows so split_volume's gap logic behaves as in production
    with src.cursor() as cur:
        cur.execute("SELECT symbol_normalized, deleted_at FROM digitallibrary.documents "
                    "WHERE symbol_normalized = ANY(%s)", [syms + _dec_variants(children)])
        rows = cur.fetchall()
    with dst.cursor() as cur:
        cur.executemany("INSERT INTO digitallibrary.documents VALUES (%s,%s) "
                        "ON CONFLICT DO NOTHING", rows)
    n += len(rows)
    src.close()

    with dst.cursor() as cur:
        cur.execute("SELECT count(*) FROM digitallibrary.document_paragraphs")
        print(f"  copied ~{n} rows; document_paragraphs={cur.fetchone()[0]}")
    dst.close()

    # pristine parsed JSON copies (read-only source)
    base = SCRATCH / "parsed_pristine"
    base.mkdir(parents=True, exist_ok=True)
    for s in ALL_LEAF_SYMS:
        p = REAL_PARSED / f"{sanitize_symbol(s)}.json"
        if p.exists():
            shutil.copy2(p, base / p.name)
    print(f"  parsed JSON pristine copies -> {base}")


def _dec_variants(children: list[str]) -> list[str]:
    return children


# ---------------------------------------------------------------------------
# Scratch parsed-dir helpers
# ---------------------------------------------------------------------------

def parsed_dir(name: str, syms: list[str] | None = None) -> Path:
    """A fresh scratch parsed dir pre-loaded with pristine copies."""
    d = SCRATCH / "parsed" / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    for s in (syms if syms is not None else ALL_LEAF_SYMS):
        src = SCRATCH / "parsed_pristine" / f"{sanitize_symbol(s)}.json"
        if src.exists():
            shutil.copy2(src, d / src.name)
    return d


def load_doc(d: Path, sym: str) -> dict:
    return json.loads((d / f"{sanitize_symbol(sym)}.json").read_text())


def save_doc(d: Path, sym: str, doc: dict) -> None:
    (d / f"{sanitize_symbol(sym)}.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1))


INVENTED = [
    "The Council authorizes the immediate deployment of twelve thousand additional "
    "peacekeepers to the Zarnovian corridor under Chapter VII of the Charter.",
    "Decides to allocate four hundred million United States dollars from the "
    "contingency reserve to the Office of the Special Coordinator for Brovania.",
    "Requests the Secretary-General to appoint a Special Envoy for the Kelmar "
    "Basin and to report thereon by 30 September 2031.",
]


def env_adv(archive: Path | None = None) -> dict[str, str]:
    e = {"DATABASE_URL": adv_url()}
    if archive:
        e["FULLTEXT_ARCHIVE_ROOT"] = str(archive)
    return e


def scratch_archive(name: str, syms: list[str], drop: list[str] | None = None) -> Path:
    """Archive root of SYMLINKS to the real files, minus `drop` (deletion control)."""
    root = SCRATCH / "arch" / name
    if root.exists():
        shutil.rmtree(root)
    (root / "original").mkdir(parents=True)
    (root / "converted").mkdir(parents=True)
    drop = drop or []
    conn = adv_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT symbol_normalized, archive_path, converted_path "
                    "FROM digitallibrary.document_files WHERE symbol_normalized = ANY(%s)",
                    [syms])
        rows = cur.fetchall()
    conn.close()
    for sym, ap, cp in rows:
        for rel in (ap, cp):
            if not rel:
                continue
            if sym in drop:
                continue
            real = ARCHIVE_ROOT / rel
            if real.exists():
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                try:
                    (root / rel).symlink_to(real)
                except FileExistsError:
                    pass
    return root


# ===========================================================================
# GATE 1 — fulltext_verify_text.py  (docx -> parsed token preservation)
# ===========================================================================

TXT = "python/fulltext_verify_text.py"
SYM = DOCX_SYMS[0]
SYM2 = DOCX_SYMS[1]


def controls_text() -> None:
    print("\n--- fulltext_verify_text.py -------------------------------------")

    # BASELINE: undamaged input must stay quiet.
    d = parsed_dir("t_base")
    rc, out = run_gate(TXT, ["--symbols", SYM, SYM2, "--parsed-dir", str(d)], env_adv())
    record(judge(Control("T-BASELINE", "verify_text", "none (pristine parse)",
                         "quiet", f"rc={rc}"), rc != 0))

    # 1. DELETION — 90% of parsed elements removed.
    d = parsed_dir("t_del90")
    doc = load_doc(d, SYM)
    els = doc["elements"]
    keep = max(1, len(els) // 10)
    doc["elements"] = els[:keep]
    save_doc(d, SYM, doc)
    rc, out = run_gate(TXT, ["--symbols", SYM, "--parsed-dir", str(d)], env_adv())
    record(judge(Control("T-DEL90", "verify_text",
                         f"kept {keep}/{len(els)} elements", "DETECT", f"rc={rc}"), rc != 0))

    # 1b. DELETION — every element removed.
    d = parsed_dir("t_delall")
    doc = load_doc(d, SYM)
    doc["elements"] = []
    save_doc(d, SYM, doc)
    rc, _ = run_gate(TXT, ["--symbols", SYM, "--parsed-dir", str(d)], env_adv())
    record(judge(Control("T-DEL-ALL", "verify_text", "elements=[]", "DETECT",
                         f"rc={rc}"), rc != 0))

    # 1c. DELETION — the whole parsed artefact is gone.
    d = parsed_dir("t_nojson", syms=[SYM2])
    rc, _ = run_gate(TXT, ["--symbols", SYM, "--parsed-dir", str(d)], env_adv())
    record(judge(Control("T-NO-ARTEFACT", "verify_text", "parsed JSON file deleted",
                         "DETECT", f"rc={rc}"), rc != 0))

    # 1d. DELETION — the SOURCE docx is gone from the archive.
    arch = scratch_archive("t_nodocx", [SYM, SYM2], drop=[SYM])
    d = parsed_dir("t_nodocx")
    rc, out = run_gate(TXT, ["--symbols", SYM, "--parsed-dir", str(d)],
                       env_adv(archive=arch))
    record(judge(Control("T-NO-SOURCE", "verify_text",
                         "archived .docx removed (ground truth absent)", "DETECT",
                         f"rc={rc}", detail=_grep(out, "skip")), rc != 0))

    # 2. FABRICATION — invented sentences added to the parse.
    d = parsed_dir("t_fab")
    doc = load_doc(d, SYM)
    for i, t in enumerate(INVENTED):
        doc["elements"].append({"type": "paragraph", "section": "main",
                                "paragraph_type": "operative", "prefix": f"{99+i}.",
                                "text": t, "positions": [10_000 + i]})
    save_doc(d, SYM, doc)
    rc, _ = run_gate(TXT, ["--symbols", SYM, "--parsed-dir", str(d)], env_adv())
    record(judge(Control("T-FABRICATE", "verify_text",
                         "3 invented operative clauses appended to the parse",
                         "DETECT", f"rc={rc}"), rc != 0))

    # 2b. FABRICATION — text lifted verbatim from a DIFFERENT resolution.
    d = parsed_dir("t_fab_x")
    doc = load_doc(d, SYM)
    other = load_doc(d, SYM2)
    doc["elements"].extend(other["elements"][: max(1, len(other["elements"]) // 2)])
    save_doc(d, SYM, doc)
    rc, _ = run_gate(TXT, ["--symbols", SYM, "--parsed-dir", str(d)], env_adv())
    record(judge(Control("T-FABRICATE-XDOC", "verify_text",
                         f"half of {SYM2}'s elements spliced into {SYM}", "DETECT",
                         f"rc={rc}"), rc != 0))

    # 3. MISATTRIBUTION — texts rotated between elements of the same document.
    d = parsed_dir("t_rotate")
    doc = load_doc(d, SYM)
    texts = [e.get("text") for e in doc["elements"]]
    for e, t in zip(doc["elements"], texts[3:] + texts[:3]):
        e["text"] = t
    save_doc(d, SYM, doc)
    rc, _ = run_gate(TXT, ["--symbols", SYM, "--parsed-dir", str(d)], env_adv())
    record(judge(Control("T-MISATTRIB", "verify_text",
                         "element texts rotated by 3 (every clause under the wrong "
                         "number/type)", "DETECT", f"rc={rc}"), rc != 0))

    # 4. TRUNCATION — the tail clause cut off the longest element.
    d = parsed_dir("t_trunc")
    doc = load_doc(d, SYM)
    before = _tokens_of(doc)
    longest = max(doc["elements"], key=lambda e: len(e.get("text") or ""))
    longest["text"] = " ".join((longest["text"] or "").split()[:-12])
    save_doc(d, SYM, doc)
    lost = _damage_size(before, doc)
    rc, _ = run_gate(TXT, ["--symbols", SYM, "--parsed-dir", str(d)], env_adv())
    record(judge(Control("T-TRUNCATE-TAIL", "verify_text",
                         f"last 12 words cut off the longest clause "
                         f"({lost} tokens actually removed)", "DETECT",
                         f"rc={rc}"), rc != 0))

    # 5. VACUOUS PASS — empty target set.
    d = parsed_dir("t_empty")
    rc, out = run_gate(TXT, ["--symbols", "Z/NO/SUCH/SYMBOL", "--parsed-dir", str(d)],
                       env_adv())
    record(judge(Control("T-EMPTY-SET", "verify_text", "0 documents selected",
                         "DETECT", f"rc={rc}",
                         detail=_grep(out, "aggregate")), rc != 0))

    # 6. LABEL CORRUPTION — every semantic label flipped/blanked.
    d = parsed_dir("t_labels")
    doc = load_doc(d, SYM)
    flip = {"operative": "preambular", "preambular": "operative"}
    for e in doc["elements"]:
        if e.get("paragraph_type"):
            e["paragraph_type"] = flip.get(e["paragraph_type"], e["paragraph_type"])
        if e.get("type") in ("heading", "title"):
            e["type"] = "paragraph"
        e["prefix"] = None
    doc["elements"].reverse()
    save_doc(d, SYM, doc)
    rc, _ = run_gate(TXT, ["--symbols", SYM, "--parsed-dir", str(d)], env_adv())
    record(judge(Control("T-LABEL-SCRAMBLE", "verify_text",
                         "operative<->preambular flipped, headings demoted, all "
                         "prefixes blanked, order reversed", "DETECT",
                         f"rc={rc}"), rc != 0))

    # 7. TOLERANCE / EXCUSE ABUSE — delete a word the run-join rule will excuse.
    ctl = _control_runjoin()
    if ctl:
        record(ctl)

    # 8. SILENT CRASH — unreadable artefact; and what a pipe would have shown.
    d = parsed_dir("t_crash")
    (d / f"{sanitize_symbol(SYM)}.json").write_text("{ this is not json")
    rc, out = run_gate(TXT, ["--symbols", SYM, "--parsed-dir", str(d)], env_adv())
    piped = subprocess.run(
        f"uv run python {TXT} --symbols '{SYM}' --parsed-dir {d} | tail -2",
        shell=True, cwd=REPO_ROOT, env={**os.environ, **env_adv()},
        capture_output=True, text=True)
    record(judge(Control("T-CRASH-ARTEFACT", "verify_text",
                         "parsed JSON corrupted to invalid JSON", "DETECT",
                         f"rc={rc}  (same run piped to `tail -2`: rc={piped.returncode})",
                         detail="pipe swallows the verdict" if piped.returncode == 0
                                else ""), rc != 0))


def _control_runjoin() -> Control | None:
    """Delete a real content word that decomposes into other present tokens.

    The gate excuses any missing token that can be segmented into >=2 pieces all
    present in the parse ('docx tokenizer run-join'). If a genuinely dropped
    content word satisfies that, real loss is excused.
    """
    sys.path.insert(0, str(PY))
    import fulltext_verify_text as vt

    victim = None
    target = None
    for min_len in (12, 8, 6):
        for sym in STRUCT_SYMS + DOCX_SYMS:
            src = SCRATCH / "parsed_pristine" / f"{sanitize_symbol(sym)}.json"
            if not src.exists():
                continue
            doc0 = json.loads(src.read_text())
            pw0, _ = vt.parsed_words(doc0)
            present0 = frozenset(pw0)
            cands = [t for t in present0
                     if len(t) >= min_len and vt._is_run_join(t, present0 - {t})]
            if cands:
                victim = max(cands, key=len)
                target = sym
                break
        if victim:
            break
    d = parsed_dir("t_runjoin")
    if victim:
        doc = load_doc(d, target)
    if not victim:
        c = Control("T-EXCUSE-RUNJOIN", "verify_text",
                    "delete a content word the run-join rule can segment",
                    "DETECT", "no decomposable content word in this document")
        c.skipped = True
        c.verdict = "n/a"
        return c
    before = _tokens_of(doc)
    # the gate strips hyphens/apostrophes before tokenising, so the printed form
    # may be 'counter-terrorism' while the token is 'counterterrorism'
    pat = re.compile("[-­‐‑’']?".join(re.escape(ch) for ch in victim),
                     re.I)
    for e in doc["elements"]:
        if e.get("text"):
            e["text"] = pat.sub("", e["text"])
    save_doc(d, target, doc)
    n = _damage_size(before, doc)
    rc, out = run_gate(TXT, ["--symbols", target, "--parsed-dir", str(d)], env_adv())
    return judge(Control("T-EXCUSE-RUNJOIN", "verify_text",
                         f"all {n} occurrences of the content word {victim!r} deleted "
                         f"from {target}'s parse", "DETECT", f"rc={rc}"), rc != 0)


def _tokens_of(doc: dict):
    """Multiset of the parse's content tokens, using the gate's own tokenizer."""
    import fulltext_verify_text as vt
    return vt.parsed_words(doc)[0]


def _damage_size(before, doc: dict) -> int:
    """How many tokens the damage actually removed. A control that removes nothing
    proves nothing, so this number is printed with every deletion control."""
    after = _tokens_of(doc)
    n = sum((before - after).values())
    if n == 0:
        print("      !! WARNING: this control removed 0 tokens — the probe, not the "
              "gate, is at fault")
    return n


def _grep(out: str, needle: str) -> str:
    for ln in out.splitlines():
        if needle in ln:
            return ln.strip()[:110]
    return ""


# ===========================================================================
# GATE 2 — fulltext_verify_pdf.py
# ===========================================================================

PDFG = "python/fulltext_verify_pdf.py"
PSYM = PDF_SYMS[0]


def controls_pdf() -> None:
    print("\n--- fulltext_verify_pdf.py --------------------------------------")

    d = parsed_dir("p_base")
    rc, out = run_gate(PDFG, ["--symbols", *PDF_SYMS, "--parsed-dir", str(d), "--verbose"],
                       env_adv())
    base_out = out
    record(judge(Control("P-BASELINE", "verify_pdf", "none (pristine parse)",
                         "quiet", f"rc={rc}", detail=_grep(out, "aggregate")), rc != 0))

    # DELETION — 90% of elements. The region the gate measures is anchored on the
    # PARSE ITSELF, so watch whether the denominator shrinks with the numerator.
    d = parsed_dir("p_del90")
    doc = load_doc(d, PSYM)
    n = len(doc["elements"])
    doc["elements"] = doc["elements"][: max(1, n // 10)]
    save_doc(d, PSYM, doc)
    rc, out = run_gate(PDFG, ["--symbols", PSYM, "--parsed-dir", str(d), "--verbose"],
                       env_adv())
    del90_out = out
    record(judge(Control("P-DEL90", "verify_pdf", f"kept {max(1, n//10)}/{n} elements",
                         "DETECT", f"rc={rc}",
                         detail=_grep(out, "region=")), rc != 0))

    # TRUNCATION — the tail half of the document dropped.
    d = parsed_dir("p_tail")
    doc = load_doc(d, PSYM)
    doc["elements"] = doc["elements"][: max(1, len(doc["elements"]) // 2)]
    save_doc(d, PSYM, doc)
    rc, out = run_gate(PDFG, ["--symbols", PSYM, "--parsed-dir", str(d), "--verbose"],
                       env_adv())
    record(judge(Control("P-TRUNCATE-TAIL", "verify_pdf",
                         "second half of the parse deleted", "DETECT", f"rc={rc}",
                         detail=_grep(out, "region=")), rc != 0))

    # DELETION — artefact gone entirely.
    d = parsed_dir("p_nojson", syms=[PDF_SYMS[1]])
    rc, out = run_gate(PDFG, ["--symbols", PSYM, "--parsed-dir", str(d)], env_adv())
    record(judge(Control("P-NO-ARTEFACT", "verify_pdf", "parsed JSON file deleted",
                         "DETECT", f"rc={rc}", detail=_grep(out, "SKIP")), rc != 0))

    # FABRICATION.
    d = parsed_dir("p_fab")
    doc = load_doc(d, PSYM)
    for i, t in enumerate(INVENTED):
        doc["elements"].append({"type": "paragraph", "text": t,
                                "positions": [9000 + i]})
    save_doc(d, PSYM, doc)
    rc, _ = run_gate(PDFG, ["--symbols", PSYM, "--parsed-dir", str(d)], env_adv())
    record(judge(Control("P-FABRICATE", "verify_pdf",
                         "3 invented clauses appended to the parse", "DETECT",
                         f"rc={rc}"), rc != 0))

    # TOLERANCE — the --max-loss band is a claim, so BOTH of its edges are
    # tested against the same damage. (The audit's original control deleted 5
    # distinct WORDS, which removed 24 TOKENS and passed only because the pass
    # rule was an OR. With the rule fixed to AND the band means what it says.)
    d = parsed_dir("p_band")
    doc = load_doc(d, PSYM)
    before = _pdf_tokens(doc)
    _delete_occurrences(doc, before, 3)
    save_doc(d, PSYM, doc)
    _pdf_damage(before, doc)
    # Read the loss the GATE measures (not the loss the probe intended: a deleted
    # parse token is only an in-region loss if the file prints it in the region).
    _, probe = run_gate(PDFG, ["--symbols", PSYM, "--parsed-dir", str(d), "--verbose",
                               "--max-loss", "100000"], env_adv())
    obs = int(re.search(r"lost=(\d+)", _grep(probe, PSYM) or "lost=0").group(1))
    rc, out = run_gate(PDFG, ["--symbols", PSYM, "--parsed-dir", str(d), "--verbose",
                              "--max-loss", str(obs)], env_adv())
    record(judge(Control("P-BAND-EDGE", "verify_pdf",
                         f"a deletion the gate measures as {obs} in-region tokens, with "
                         f"the band set to exactly {obs}", "quiet", f"rc={rc}",
                         detail=_grep(out, "region=")), rc != 0))
    rc, out = run_gate(PDFG, ["--symbols", PSYM, "--parsed-dir", str(d), "--verbose",
                              "--max-loss", str(obs - 1)], env_adv())
    record(judge(Control("P-BAND-OVER", "verify_pdf",
                         f"the SAME {obs}-token deletion, one token past the band "
                         f"({obs - 1})", "DETECT", f"rc={rc}",
                         detail=_grep(out, "region=")), rc != 0))

    # VACUOUS PASS.
    d = parsed_dir("p_empty")
    rc, out = run_gate(PDFG, ["--symbols", "Z/NO/SUCH/SYMBOL", "--parsed-dir", str(d)],
                       env_adv())
    record(judge(Control("P-EMPTY-SET", "verify_pdf", "0 documents selected",
                         "DETECT", f"rc={rc}", detail=_grep(out, "aggregate")), rc != 0))

    # THE DENOMINATOR ITSELF. The defect that made this gate detect 0/6 was that
    # the comparison region was anchored on the parse, so damage shrank the
    # denominator with the numerator. This control measures the denominator
    # directly: the region must be IDENTICAL for a pristine and a 90%-deleted
    # parse. (Before the repair: 1,676 -> 135 tokens, and the score ROSE to
    # 100.00%.)
    region_base = _region_of(base_out, PSYM)
    region_dmg = _region_of(del90_out, PSYM)
    c = Control("P-REGION-INDEPENDENCE", "verify_pdf",
                "the comparison region is measured on a pristine and on a "
                "90%-deleted parse of the same document", "DETECT",
                f"region {region_base} -> {region_dmg}")
    c.verdict = "DETECTED" if (region_base and region_base == region_dmg) else "MISSED"
    c.detail = ("the denominator is a property of the file"
                if c.verdict == "DETECTED"
                else "the denominator moved with the artefact being graded")
    record(c)

    # FABRICATION lifted verbatim from a DIFFERENT resolution (the class that
    # publishes real UN prose under the wrong symbol).
    d = parsed_dir("p_fab_x")
    doc = load_doc(d, PSYM)
    other = load_doc(d, PDF_SYMS[1])
    doc["elements"].extend(other["elements"][: max(1, len(other["elements"]) // 2)])
    save_doc(d, PSYM, doc)
    rc, out = run_gate(PDFG, ["--symbols", PSYM, "--parsed-dir", str(d), "--verbose"],
                       env_adv())
    record(judge(Control("P-FABRICATE-XDOC", "verify_pdf",
                         f"half of {PDF_SYMS[1]}'s elements spliced into {PSYM}",
                         "DETECT", f"rc={rc}", detail=_grep(out, "region=")), rc != 0))


def _pdf_tokens(doc: dict):
    import fulltext_verify_pdf as vp
    return vp.parsed_words(doc)


def _pdf_damage(before, doc: dict) -> int:
    n = sum((before - _pdf_tokens(doc)).values())
    if n == 0:
        print("      !! WARNING: this control removed 0 tokens — the probe, not the "
              "gate, is at fault")
    return n


def _delete_occurrences(doc: dict, before, target: int) -> None:
    """Delete SINGLE occurrences of content words until `target` tokens are gone.

    Deleting every occurrence of a word removes an unpredictable number of
    tokens, which is useless for testing the edge of an absolute band.
    """
    for e in doc.get("elements", []):
        t = e.get("text") or ""
        for w in re.findall(r"[A-Za-z]{6,}", t):
            e["text"] = re.sub(r"\b" + re.escape(w) + r"\b", "", e["text"], count=1)
            if sum((before - _pdf_tokens(doc)).values()) >= target:
                return
            t = e["text"]


def _region_of(out: str, symbol: str) -> str:
    """The region size the PDF gate reported for `symbol`, from its own output."""
    for ln in out.splitlines():
        if symbol in ln:
            m = re.search(r"region=(\d+)", ln)
            if m:
                return m.group(1)
    return ""


def _delete_n_content_words(doc: dict, n: int) -> list[str]:
    seen: list[str] = []
    for e in doc.get("elements", []):
        t = e.get("text") or ""
        for w in re.findall(r"[A-Za-z]{6,}", t):
            if w.lower() not in [s.lower() for s in seen]:
                seen.append(w)
            if len(seen) >= n:
                break
        if len(seen) >= n:
            break
    for e in doc.get("elements", []):
        if e.get("text"):
            for w in seen:
                e["text"] = re.sub(re.escape(w), "", e["text"], flags=re.I)
    return seen


# ===========================================================================
# GATE 3 — fulltext_verify_volumes.py   (damage lands in the SCRATCH DB)
# ===========================================================================

VOLG = "python/fulltext_verify_volumes.py"
_PARA_SNAP: dict = {}


def _restore_volume(snap: dict) -> None:
    c = adv_conn()
    with c.cursor() as cur:
        cur.execute("DELETE FROM digitallibrary.document_paragraphs_raw "
                    "WHERE symbol_normalized = ANY(%s) OR source_symbol = %s",
                    [snap["syms"], VOLUME])
        cols = snap["cols"]
        ph = ", ".join(["%s"] * len(cols))
        cur.executemany(
            f"INSERT INTO digitallibrary.document_paragraphs_raw ({', '.join(cols)}) "
            f"VALUES ({ph})", snap["rows"])
    c.close()


def _snapshot_volume() -> dict:
    c = adv_conn()
    with c.cursor() as cur:
        cur.execute("SELECT * FROM digitallibrary.document_paragraphs_raw "
                    "WHERE symbol_normalized = %s OR source_symbol = %s", [VOLUME, VOLUME])
        cols = [d[0] for d in cur.description]
        rows = _wrap_json(cur, cur.fetchall())
        cur.execute("SELECT DISTINCT symbol_normalized FROM digitallibrary.document_paragraphs_raw "
                    "WHERE source_symbol = %s", [VOLUME])
        kids = [r[0] for r in cur.fetchall()]
    c.close()
    return {"cols": cols, "rows": rows, "syms": [VOLUME] + kids, "children": kids}


def _rich_child(kids: list[str]) -> tuple[str, str, str]:
    """(child with the most text, its successor, a non-empty row of the successor)."""
    c = adv_conn()
    with c.cursor() as cur:
        cur.execute("SELECT symbol_normalized, sum(length(text)) FROM "
                    "digitallibrary.document_paragraphs_raw WHERE source_symbol = %s "
                    "GROUP BY 1 ORDER BY 2 DESC", [VOLUME])
        by_size = cur.fetchall()
        rich = by_size[0][0]
        i = kids.index(rich)
        nxt = kids[i + 1] if i + 1 < len(kids) else kids[i - 1]
        cur.execute("SELECT text FROM digitallibrary.document_paragraphs_raw "
                    "WHERE source_symbol=%s AND symbol_normalized=%s AND length(btrim(text)) > 40 "
                    "ORDER BY position OFFSET 1 LIMIT 1", [VOLUME, nxt])
        row = cur.fetchone()
        if row is None:
            cur.execute("SELECT text FROM digitallibrary.document_paragraphs_raw "
                        "WHERE source_symbol=%s AND symbol_normalized=%s "
                        "AND length(btrim(text)) > 40 ORDER BY position LIMIT 1", [VOLUME, nxt])
            row = cur.fetchone()
    c.close()
    return rich, nxt, (row[0] if row else "")


def _run_vol(extra: list[str] | None = None) -> tuple[int, str]:
    return run_gate(VOLG, ["--symbols", VOLUME, *(extra or [])], env_adv())


def _vol_signal(out: str):
    """The volume gate's own reported numbers for the fixture volume.

    The fixture volume is RED at baseline for a real reason (4 printed decision
    headings the split never routed into a child — 'unmatched', which the gate
    now fails on and used to print and ignore). Where a gate is legitimately red
    on undamaged input its exit code carries no information, so every control on
    it is judged on MOVEMENT of these numbers, exactly as the TOC controls are.
    """
    m = re.search(r"coverage=([\d.]+) children=(\d+)/(\d+) gt_tokens=(\d+) "
                  r"missing=(\d+) unmatched=(\d+) invented=(\d+) problems=(\d+)", out)
    nums = tuple(m.groups()) if m else ()
    # The problem LINES too, not only their count: a control that changes what a
    # problem says without changing how many there are (a child truncated inside
    # an already-reported category) must still move the signal.
    lines = tuple(sorted(ln.strip() for ln in out.splitlines() if ln.startswith("       - ")))
    return (nums, lines) if nums else None


def controls_volumes() -> None:
    print("\n--- fulltext_verify_volumes.py ----------------------------------")
    global _PARA_SNAP
    snap = _snapshot_volume()
    kids = snap["children"]
    if not kids:
        print("  (no volume children seeded — skipping)")
        return
    _PARA_SNAP = _snap_paragraphs(kids)
    # A control that damages nothing proves nothing. The fixture volume's first
    # child is a two-row decision whose second row is EMPTY, so "delete the
    # second half" and "append the next child's second row" both moved zero
    # tokens and blamed the gate for the probe's failure. Pick the child with the
    # most text, and its successor's first NON-EMPTY body row.
    kid_rich, kid_next, leak_row = _rich_child(kids)
    print(f"    (damage targets: {kid_rich}, leaking from {kid_next})")

    rc, out = _run_vol()
    base_rc, base_sig = rc, _vol_signal(out)
    record(judge(Control("V-BASELINE", "verify_volumes", "none (pristine split)",
                         "quiet", f"rc={rc}", detail=_grep(out, VOLUME)), rc != 0))

    def vjudge(c: Control, rc: int, out: str) -> Control:
        return judge_signal(c, rc, _vol_signal(out), base_rc, base_sig)

    def damage(sql: str, params: list) -> tuple[int, str]:
        c = adv_conn()
        with c.cursor() as cur:
            cur.execute(sql, params)
        c.close()
        r = _run_vol()
        _restore_volume(snap)
        return r

    # DELETION — every child of the volume removed from the DB.
    rc, out = damage("DELETE FROM digitallibrary.document_paragraphs_raw "
                     "WHERE source_symbol = %s", [VOLUME])
    record(vjudge(Control("V-DEL-CHILDREN", "verify_volumes",
                          f"all {len(kids)} children deleted from the raw layer",
                          "DETECT", f"rc={rc}", detail=_grep(out, VOLUME)), rc, out))

    # DELETION — one child removed.
    rc, out = damage("DELETE FROM digitallibrary.document_paragraphs_raw "
                     "WHERE source_symbol = %s AND symbol_normalized = %s",
                     [VOLUME, kids[len(kids) // 2]])
    record(vjudge(Control("V-DEL-ONE-CHILD", "verify_volumes",
                          f"child {kids[len(kids)//2]} deleted entirely", "DETECT",
                          f"rc={rc}", detail=_grep(out, VOLUME)), rc, out))

    # DELETION — one child removed from BOTH layers (so a "stored under the
    # sibling volume" rule cannot excuse it).
    victim = kids[len(kids) // 2]
    c = adv_conn()
    with c.cursor() as cur:
        cur.execute("DELETE FROM digitallibrary.document_paragraphs_raw "
                    "WHERE source_symbol = %s AND symbol_normalized = %s", [VOLUME, victim])
        cur.execute("DELETE FROM digitallibrary.document_paragraphs "
                    "WHERE symbol_normalized = %s", [victim])
    c.close()
    rc, out = _run_vol()
    _restore_volume(snap)
    _restore_paragraphs(_PARA_SNAP)
    record(vjudge(Control("V-DEL-CHILD-BOTH", "verify_volumes",
                          f"child {victim} deleted from raw AND semantic layers",
                          "DETECT", f"rc={rc}", detail=_grep(out, VOLUME)), rc, out))

    # MISATTRIBUTION — every child's text moved under its neighbour's symbol.
    c = adv_conn()
    with c.cursor() as cur:
        cur.execute("SELECT DISTINCT symbol_normalized FROM digitallibrary.document_paragraphs_raw "
                    "WHERE source_symbol = %s ORDER BY 1", [VOLUME])
        order = [r[0] for r in cur.fetchall()]
        cur.execute("UPDATE digitallibrary.document_paragraphs_raw SET symbol_normalized = "
                    "symbol_normalized || '#TMP' WHERE source_symbol = %s", [VOLUME])
        for a, b in zip(order, order[1:] + order[:1]):
            cur.execute("UPDATE digitallibrary.document_paragraphs_raw "
                        "SET symbol_normalized = %s WHERE symbol_normalized = %s "
                        "AND source_symbol = %s", [b, a + "#TMP", VOLUME])
    c.close()
    rc, out = _run_vol()
    _restore_volume(snap)
    record(vjudge(Control("V-MISATTRIB", "verify_volumes",
                          "every child's rows moved under the NEXT decision's symbol",
                          "DETECT", f"rc={rc}", detail=_grep(out, VOLUME)), rc, out))

    # TRUNCATION — a child cut short.
    rc, out = damage(
        "DELETE FROM digitallibrary.document_paragraphs_raw d WHERE d.source_symbol = %s "
        "AND d.symbol_normalized = %s AND d.position > ("
        "  SELECT min(position) + (max(position)-min(position))/2 "
        "  FROM digitallibrary.document_paragraphs_raw "
        "  WHERE source_symbol = %s AND symbol_normalized = %s)",
        [VOLUME, kid_rich, VOLUME, kid_rich])
    record(vjudge(Control("V-TRUNCATE-CHILD", "verify_volumes",
                          f"second half of child {kid_rich} deleted", "DETECT",
                          f"rc={rc}", detail=_grep(out, VOLUME)), rc, out))

    # BOUNDARY LEAK — one sentence of the next decision appended to its predecessor.
    c = adv_conn()
    with c.cursor() as cur:
        cur.execute("SELECT text FROM digitallibrary.document_paragraphs_raw "
                    "WHERE source_symbol=%s AND symbol_normalized=%s ORDER BY position LIMIT 1",
                    [VOLUME, kid_next])
        nxt_head = cur.fetchone()[0]
        cur.execute("SELECT max(position) FROM digitallibrary.document_paragraphs_raw "
                    "WHERE source_symbol=%s AND symbol_normalized=%s", [VOLUME, kid_rich])
        pos = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO digitallibrary.document_paragraphs_raw "
            "(symbol_normalized, lang, position, kind, text, extractor_version, source_symbol) "
            "VALUES (%s,'en',%s,'paragraph',%s,'adv',%s)",
            [kid_rich, pos + 1, leak_row, VOLUME])
    c.close()
    rc, out = _run_vol()
    _restore_volume(snap)
    record(vjudge(Control("V-LEAK-1SENTENCE", "verify_volumes",
                          f"one REAL sentence of the next decision appended to child "
                          f"{kid_rich} (a one-sentence boundary leak the old substring "
                          f"test could not see even in principle)", "DETECT", f"rc={rc}",
                          detail=_grep(out, VOLUME)), rc, out))

    # BOUNDARY LEAK — the whole next heading swallowed (the case the gate claims).
    c = adv_conn()
    with c.cursor() as cur:
        cur.execute("SELECT max(position) FROM digitallibrary.document_paragraphs_raw "
                    "WHERE source_symbol=%s AND symbol_normalized=%s", [VOLUME, kid_rich])
        pos = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO digitallibrary.document_paragraphs_raw "
            "(symbol_normalized, lang, position, kind, text, extractor_version, source_symbol) "
            "VALUES (%s,'en',%s,'paragraph',%s,'adv',%s)",
            [kid_rich, pos + 1, nxt_head, VOLUME])
    c.close()
    rc, out = _run_vol()
    _restore_volume(snap)
    record(vjudge(Control("V-LEAK-NEXT-HEADING", "verify_volumes",
                          f"the next decision's heading line appended to child {kid_rich}",
                          "DETECT", f"rc={rc}", detail=_grep(out, VOLUME)), rc, out))

    # FABRICATION — invented text inserted into a child.
    c = adv_conn()
    with c.cursor() as cur:
        cur.execute("SELECT max(position) FROM digitallibrary.document_paragraphs_raw "
                    "WHERE source_symbol=%s AND symbol_normalized=%s", [VOLUME, kid_rich])
        pos = cur.fetchone()[0]
        for i, t in enumerate(INVENTED):
            cur.execute(
                "INSERT INTO digitallibrary.document_paragraphs_raw "
                "(symbol_normalized, lang, position, kind, text, extractor_version, source_symbol) "
                "VALUES (%s,'en',%s,'paragraph',%s,'adv',%s)",
                [kid_rich, pos + 1 + i, t, VOLUME])
    c.close()
    rc, out = _run_vol()
    _restore_volume(snap)
    record(vjudge(Control("V-FABRICATE", "verify_volumes",
                          f"3 invented paragraphs inserted into child {kid_rich}",
                          "DETECT", f"rc={rc}", detail=_grep(out, VOLUME)), rc, out))

    # DELETION — 90% of the VOLUME's own extraction.
    rc, out = damage(
        "DELETE FROM digitallibrary.document_paragraphs_raw WHERE symbol_normalized = %s "
        "AND source_symbol IS NULL AND position %% 10 <> 0", [VOLUME])
    record(vjudge(Control("V-DEL-90-RAW", "verify_volumes",
                          "90% of the volume's own raw rows deleted", "DETECT",
                          f"rc={rc}", detail=_grep(out, VOLUME)), rc, out))

    # ---- CHILD FIDELITY (--children): the 3,590 volume-split children have no
    # archive file of their own, so both text gates skip them. Their ground truth
    # is the parent volume's PRINTED RANGE.
    rc, out = _run_vol(["--children"])
    _ = rc
    cbase = _grep(out, "Child fidelity")
    record(judge(Control("V-CHILD-BASELINE", "verify_volumes --children",
                         "none (children as stored)", "quiet",
                         f"rc={rc}", detail=cbase),
                 "Child fidelity" not in out))

    def cjudge(name: str, dmg: str, out: str) -> Control:
        c = Control(name, "verify_volumes --children", dmg, "DETECT",
                    _grep(out, "Child fidelity"))
        c.verdict = "DETECTED" if _grep(out, "Child fidelity") != cbase else "MISSED"
        return c

    # A child whose stored content is gone entirely.
    victim = kids[3]
    c = adv_conn()
    with c.cursor() as cur:
        cur.execute("DELETE FROM digitallibrary.document_paragraphs "
                    "WHERE symbol_normalized = %s", [victim])
        cur.execute("UPDATE digitallibrary.document_paragraphs_raw SET text = '' "
                    "WHERE symbol_normalized = %s AND source_symbol = %s", [victim, VOLUME])
    c.close()
    rc, out = _run_vol(["--children"])
    _restore_volume(snap)
    _restore_paragraphs(_PARA_SNAP)
    record(cjudge("V-CHILD-EMPTIED", f"child {victim} emptied in both layers", out))

    # EVERY child truncated to the first half of its stored rows. (Targeting a
    # single child proved nothing when that child happened to be one of the two
    # already below the bar — a control must be shown to move the number.)
    c = adv_conn()
    with c.cursor() as cur:
        cur.execute("DELETE FROM digitallibrary.document_paragraphs "
                    "WHERE symbol_normalized = ANY(%s)", [kids])
        cur.execute(
            "DELETE FROM digitallibrary.document_paragraphs_raw d WHERE d.source_symbol = %s "
            "AND d.position > ("
            "  SELECT min(position) + (max(position)-min(position))/2 "
            "  FROM digitallibrary.document_paragraphs_raw x "
            "  WHERE x.source_symbol = d.source_symbol "
            "    AND x.symbol_normalized = d.symbol_normalized)",
            [VOLUME])
    c.close()
    rc, out = _run_vol(["--children"])
    _restore_volume(snap)
    _restore_paragraphs(_PARA_SNAP)
    record(cjudge("V-CHILD-TRUNCATED",
                  f"all {len(kids)} children cut to half their stored rows", out))

    # VACUOUS PASS.
    rc, out = run_gate(VOLG, ["--symbols", "Z/NO/SUCH(VOL.II)"], env_adv())
    record(judge(Control("V-EMPTY-SET", "verify_volumes", "0 volumes selected",
                         "DETECT", f"rc={rc}", detail=_grep(out, "volumes passed")),
                 rc != 0))


# ===========================================================================
# GATE 4 — fulltext_verify_toc.py
# ===========================================================================

TOCG = "python/fulltext_verify_toc.py"


def controls_toc() -> None:
    print("\n--- fulltext_verify_toc.py --------------------------------------")
    tsv = SCRATCH / "toc.tsv"
    sym = STRUCT_SYMS[0]   # A/RES/79/1 — 56 bold "Action N." self-declared headings

    def run(symbols: list[str]) -> tuple[int, str]:
        return run_gate(TOCG, ["--symbols", *symbols, "--tsv", str(tsv), "--verbose"],
                        env_adv())

    def sig(out: str):
        m = re.search(r"declared headings\s+:\s+(\d+)\s+\(matched (\d+), missing (\d+), "
                      r"misclassified (\d+), split (\d+)\)", out)
        return tuple(int(x) for x in m.groups()) if m else None

    rc, out = run([sym])
    base_rc, base_sig = rc, sig(out)
    record(judge(Control("C-BASELINE", "verify_toc",
                         "none (pristine, correct structure) — a gate must be GREEN "
                         "here or it can never gate anything", "quiet", f"rc={rc}",
                         detail=_grep(out, "declared headings")), rc != 0))

    # A document that declares NO structure is silently skipped: the gate has
    # nothing to compare and reports "checked", which reads as a pass.
    rc, out = run([DOCX_SYMS[0]])
    record(judge(Control("C-NO-DECLARED", "verify_toc",
                         "a document whose docx declares no headings at all "
                         "(the corpus norm) — any structural damage to it is "
                         "unobservable by construction", "DETECT", f"rc={rc}",
                         detail=_grep(out, "with self-declared")), rc != 0))

    snap = _snap_paragraphs([sym])

    # DELETION — the document's parsed structure removed entirely.
    _exec("DELETE FROM digitallibrary.document_paragraphs WHERE symbol_normalized = %s", [sym])
    rc, out = run([sym])
    _restore_paragraphs(snap)
    record(judge_signal(Control("C-DEL-STRUCTURE", "verify_toc",
                        "every parsed element of the document deleted", "DETECT",
                        f"rc={rc}", detail=_grep(out, "declared headings")),
                        rc, sig(out), base_rc, base_sig))

    # LABEL CORRUPTION — every heading demoted to a paragraph.
    _exec("UPDATE digitallibrary.document_paragraphs SET type='paragraph' "
          "WHERE symbol_normalized = %s AND type IN ('heading','title')", [sym])
    rc, out = run([sym])
    _restore_paragraphs(snap)
    record(judge_signal(Control("C-DEMOTE-HEADINGS", "verify_toc",
                        "type='heading'/'title' rewritten to 'paragraph'", "DETECT",
                        f"rc={rc}", detail=_grep(out, "declared headings")),
                        rc, sig(out), base_rc, base_sig))

    # FABRICATION — invented headings and clauses added to the parse.
    c = adv_conn()
    with c.cursor() as cur:
        cur.execute("SELECT max(position) FROM digitallibrary.document_paragraphs "
                    "WHERE symbol_normalized=%s", [sym])
        pos = cur.fetchone()[0] or 0
        for i, t in enumerate(INVENTED):
            cur.execute(
                "INSERT INTO digitallibrary.document_paragraphs (symbol_normalized, lang, "
                "position, id, type, section, text, raw_positions, parser_version) "
                "VALUES (%s,'en',%s, gen_random_uuid(), 'heading','main',%s,'{1}','adv')",
                [sym, pos + 1 + i, t])
    c.close()
    rc, out = run([sym])
    _restore_paragraphs(snap)
    record(judge_signal(Control("C-FABRICATE", "verify_toc",
                        "3 invented headings inserted into the parsed structure",
                        "DETECT", f"rc={rc}", detail=_grep(out, "declared headings")),
                        rc, sig(out), base_rc, base_sig))

    # VACUOUS PASS.
    rc, out = run(["Z/NO/SUCH/SYMBOL"])
    record(judge(Control("C-EMPTY-SET", "verify_toc", "0 documents selected",
                         "DETECT", f"rc={rc}",
                         detail=_grep(out, "documents checked")), rc != 0))


def _exec(sql: str, params: list) -> None:
    c = adv_conn()
    with c.cursor() as cur:
        cur.execute(sql, params)
    c.close()


def _snap_paragraphs(syms: list[str]) -> dict:
    c = adv_conn()
    with c.cursor() as cur:
        cur.execute("SELECT * FROM digitallibrary.document_paragraphs "
                    "WHERE symbol_normalized = ANY(%s)", [syms])
        cols = [d[0] for d in cur.description]
        rows = _wrap_json(cur, cur.fetchall())
    c.close()
    return {"cols": cols, "rows": rows, "syms": syms}


def _restore_paragraphs(snap: dict) -> None:
    c = adv_conn()
    with c.cursor() as cur:
        cur.execute("DELETE FROM digitallibrary.document_paragraphs "
                    "WHERE symbol_normalized = ANY(%s)", [snap["syms"]])
        ph = ", ".join(["%s"] * len(snap["cols"]))
        cur.executemany(
            f"INSERT INTO digitallibrary.document_paragraphs ({', '.join(snap['cols'])}) "
            f"VALUES ({ph})", snap["rows"])
    c.close()


# ===========================================================================
# GATE 5 — fulltext_verify_display.py   (hardcoded .env -> in-process patch)
# ===========================================================================

DISPG = "python/fulltext_verify_display.py"
PATCH_DISP = "m.get_conn = lambda: psycopg.connect(os.environ['ADV_DATABASE_URL'])"


def controls_display() -> None:
    print("\n--- fulltext_verify_display.py ----------------------------------")
    out_tsv = SCRATCH / "display.tsv"
    aset = SCRATCH / "audit_set.json"
    aset.write_text(json.dumps([{"symbol": s, "citations": 10} for s in STRUCT_SYMS]))

    # a scratch archive root, so the gate's DEFAULT audit-set path is absent
    # unless a control passes one explicitly
    noarch = SCRATCH / "noarchive"
    (noarch / "audit").mkdir(parents=True, exist_ok=True)

    def run(extra: list[str]) -> tuple[int, str]:
        return run_gate(DISPG, ["--output", str(out_tsv), *extra],
                        env_adv(archive=noarch), inproc_patch=PATCH_DISP)

    rc, out = run(["--audit-set", str(aset), "--threshold", "0"])
    record(judge(Control("D-BASELINE", "verify_display",
                         "none; threshold 0% so nothing can flag", "quiet",
                         f"rc={rc}", detail=_grep(out, "flagged")), rc != 0))

    # The real bar, on documents known to render as near-nothing.
    rc, out = run(["--audit-set", str(aset)])
    base_flagged = _grep(out, "flagged")
    record(judge(Control("D-BASELINE-REAL", "verify_display",
                         "none; default 60% threshold on 3 known-bad audit docs",
                         "DETECT", f"rc={rc}", detail=base_flagged), rc != 0))

    snap = _snap_paragraphs(STRUCT_SYMS)

    # 5. VACUOUS PASS — no audit set at all.
    rc, out = run(["--audit-set", str(SCRATCH / "does_not_exist.json")])
    record(judge(Control("D-NO-AUDIT-SET", "verify_display",
                         "audit_set.json absent (e.g. SSD not mounted) while docs "
                         "are still flagged", "DETECT", f"rc={rc}",
                         detail=_grep(out, "flagged")), rc != 0))

    # 5b. VACUOUS PASS — the DEFAULT audit-set path is absent (the SSD is not
    # mounted on the machine running the gate). No audit set -> no gating symbol
    # -> exit 0 no matter how many documents are flagged.
    rc, out = run([])
    record(judge(Control("D-DEFAULT-NO-SET", "verify_display",
                         "no --audit-set given and the default audit_set.json is "
                         "absent, while documents are still flagged", "DETECT",
                         f"rc={rc}", detail=_grep(out, "flagged")), rc != 0))

    # 1. DELETION — the flagged document's rows removed entirely.
    _exec("DELETE FROM digitallibrary.document_paragraphs WHERE symbol_normalized = ANY(%s)",
          [STRUCT_SYMS])
    rc, out = run(["--audit-set", str(aset)])
    _restore_paragraphs(snap)
    record(judge(Control("D-DEL-DOCS", "verify_display",
                         "all parsed rows of the 3 audit documents deleted", "DETECT",
                         f"rc={rc}", detail=_grep(out, "flagged")), rc != 0))

    # 1b. DELETION — the text emptied but the rows kept (total tokens = 0).
    _exec("UPDATE digitallibrary.document_paragraphs SET text='' "
          "WHERE symbol_normalized = ANY(%s)", [STRUCT_SYMS])
    rc, out = run(["--audit-set", str(aset)])
    _restore_paragraphs(snap)
    record(judge(Control("D-ZERO-TOKENS", "verify_display",
                         "every element's text blanked (total_tokens = 0)", "DETECT",
                         f"rc={rc}", detail=_grep(out, "flagged")), rc != 0))


# ===========================================================================
# GATE 6 — fulltext_audit_invariants.py
# ===========================================================================

INVG = "python/fulltext_audit_invariants.py"
PATCH_INV = ("m.get_conn = lambda: (psycopg.connect(os.environ['ADV_DATABASE_URL']), "
             "os.environ.get('ADV_LEGACY') == '1')")


def controls_invariants() -> None:
    print("\n--- fulltext_audit_invariants.py --------------------------------")
    out_tsv = SCRATCH / "invariants.tsv"
    aset = SCRATCH / "audit_set.json"

    def run(extra: list[str], legacy: str = "1") -> tuple[int, str]:
        return run_gate(INVG, ["--output", str(out_tsv), "--audit-set", str(aset), *extra],
                        {**env_adv(), "ADV_LEGACY": legacy}, inproc_patch=PATCH_INV)

    rc, out = run([])
    base = _grep(out, "total findings")
    record(judge(Control("I-BASELINE", "audit_invariants", "none", "quiet",
                         f"rc={rc}", detail=base), rc != 0))

    snap = _snap_paragraphs(STRUCT_SYMS)

    # 6. LABEL CORRUPTION — every semantic label nulled (the exact defect check
    # (b)/(c) exist to catch).
    _exec("UPDATE digitallibrary.document_paragraphs SET paragraph_type = NULL "
          "WHERE symbol_normalized = ANY(%s)", [STRUCT_SYMS])
    rc, out = run([])
    _restore_paragraphs(snap)
    record(judge(Control("I-NULL-ALL-LABELS", "audit_invariants",
                         "paragraph_type nulled on every element of 3 documents",
                         "DETECT", f"rc={rc}", detail=_grep(out, "total findings")),
                 rc != 0))

    # 1. DELETION — the documents vanish from the corpus the audit iterates over.
    _exec("DELETE FROM digitallibrary.document_paragraphs WHERE symbol_normalized = ANY(%s)",
          [STRUCT_SYMS])
    rc, out = run([])
    _restore_paragraphs(snap)
    record(judge(Control("I-DEL-DOCS", "audit_invariants",
                         "the audit-set documents deleted from the corpus", "DETECT",
                         f"rc={rc}", detail=_grep(out, "docs scanned")), rc != 0))

    # 7. SILENT DEGRADATION — the legacy corpus becomes unreadable, so check (d)
    # (the only cross-corpus structure check) silently stops running.
    rc, out = run([], legacy="0")
    record(judge(Control("I-LEGACY-GONE", "audit_invariants",
                         "mandates.paragraphs unreadable -> check (d) skipped",
                         "DETECT", f"rc={rc}",
                         detail=_grep(out, "check (d) skipped")), rc != 0))


# ===========================================================================
# GATE 7 — fulltext_parse.py's accounting invariant + "0 accounting failures"
# ===========================================================================

def controls_parse_accounting() -> None:
    print("\n--- fulltext_parse.py (accounting invariant) ---------------------")
    import fulltext_parse as fp

    raw = [{"position": i} for i in range(10)]

    def result(positions: list[list[int]], dropped: list[int]) -> dict:
        return {"elements": [{"positions": p} for p in positions],
                "dropped": [{"position": d} for d in dropped]}

    good = result([[0, 1], [2, 3], [4, 5], [6, 7]], [8, 9])
    ok = fp._check_accounting(good, raw)
    record(judge(Control("A-BASELINE", "parse accounting",
                         "none (all 10 raw positions consumed once)", "quiet",
                         f"_check_accounting -> {ok!r}"), ok is not None))

    for nm, res, dmg in [
        ("A-UNACCOUNTED", result([[0, 1], [2, 3], [4, 5]], [8, 9]),
         "positions 6,7 consumed by nothing"),
        ("A-DUPLICATE", result([[0, 1], [1, 2, 3], [4, 5], [6, 7]], [8, 9]),
         "position 1 consumed twice"),
        ("A-PHANTOM", result([[0, 1], [2, 3], [4, 5], [6, 7, 99]], [8, 9]),
         "position 99 does not exist in the raw layer"),
    ]:
        err = fp._check_accounting(res, raw)
        record(judge(Control(nm, "parse accounting", dmg, "DETECT",
                             f"_check_accounting -> {err!r}"), err is not None))

    # 2. FABRICATION — element text replaced by invented prose, positions intact.
    fab = {"elements": [{"positions": [0, 1], "text": INVENTED[0]},
                        {"positions": [2, 3], "text": INVENTED[1]},
                        {"positions": [4, 5], "text": INVENTED[2]},
                        {"positions": [6, 7], "text": INVENTED[0]}],
           "dropped": [{"position": 8}, {"position": 9}]}
    err = fp._check_accounting(fab, raw)
    record(judge(Control("A-FABRICATED-TEXT", "parse accounting",
                         "every element's text replaced with invented prose; "
                         "positions untouched", "DETECT",
                         f"_check_accounting -> {err!r}"), err is not None))

    # The exit-code question: does an accounting failure fail the run?
    code = (REPO_ROOT / "python" / "fulltext_parse.py").read_text()
    exits_on_acct = "n_acct_fail" in code.split("return 0 if")[-1].split("\n")[0]
    record(judge(Control("A-EXIT-CODE", "parse accounting",
                         "an accounting failure occurs during a --to-db run",
                         "DETECT",
                         "fulltext_parse.main() returns "
                         + code.split("return 0 if")[-1].split("\n")[0].strip()
                         + "  (n_acct_fail not in the expression)"
                         if not exits_on_acct else "exit code reflects n_acct_fail"),
                 exits_on_acct))


# ===========================================================================
# GATE 8 — nightly orchestration: silence must not read as success
# ===========================================================================

def controls_nightly() -> None:
    """The orchestrator is exercised for real, with every network/subprocess stage
    stubbed out: the damage is the *state of the night*, not the code."""
    print("\n--- fulltext_nightly.py (orchestration) --------------------------")
    NIG = "python/fulltext_nightly.py"
    stub_common = (
        "m.run_stage = lambda label, script, extra: "
        "(1 if label == os.environ.get('ADV_FAIL_STAGE') else 0, 0.0)\n"
        "m.flag_native_docx = lambda conn: 0\n"
        "m.get_conn = lambda: psycopg.connect(os.environ['ADV_DATABASE_URL'])\n")
    arch = SCRATCH / "nightly_archive"

    # BASELINE: a night that DOES work and whose text gate fails must exit non-zero.
    patch = (stub_common
             + "m.snapshot = lambda: {('extracted','docx'): 5}\n"
             + "rows = iter([set(), {('S/RES/2825(2026)', 'docx')}])\n"
             + "m.extracted_rows = lambda: next(rows, set())\n")
    rc, out = run_gate(NIG, [], {**env_adv(archive=arch), "ADV_FAIL_STAGE": "gate-text"},
                       inproc_patch=patch)
    record(judge(Control("N-GATE-FAILURE", "nightly",
                         "the text acceptance gate fails on tonight's documents",
                         "DETECT", f"rc={rc}", detail=_grep(out, "FINAL")), rc != 0))

    # 5. VACUOUS PASS — a night in which nothing reached 'extracted' at all
    # (source blocked every fetch / a selection bug produced nothing).
    patch = (stub_common
             + "m.snapshot = lambda: {}\n"
             + "m.extracted_rows = lambda: set()\n")
    rc, out = run_gate(NIG, [], env_adv(archive=arch), inproc_patch=patch)
    record(judge(Control("N-ZERO-WORK-NIGHT", "nightly",
                         "0 documents reach status='extracted' — no gate runs at all",
                         "DETECT", f"rc={rc}", detail=_grep(out, "FINAL")), rc != 0))

    # 7. SILENCE — no machine-readable verdict is written, so a lost exit code
    # (a pipe, a CI step that swallows status) leaves nothing to read.
    code = (REPO_ROOT / "python" / "fulltext_nightly.py").read_text()
    writes = any(k in code for k in ("json.dump", "write_state", "gate.json"))
    record(judge(Control("N-NO-RESULT-FILE", "nightly",
                         "the run's exit code is lost (piped / swallowed by CI)",
                         "DETECT",
                         "no verdict file is written; the exit code is the only signal"
                         if not writes else "writes a verdict file"), writes))

    # 8. COVERAGE — which gates the automation actually runs.
    split_code = (REPO_ROOT / "python" / "fulltext_split_volumes.py").read_text()
    reachable = code + split_code   # split --nightly is a nightly stage
    gates_run = [g for g in ("fulltext_verify_text", "fulltext_verify_pdf",
                             "fulltext_verify_volumes", "fulltext_verify_toc",
                             "fulltext_verify_display", "fulltext_audit_invariants")
                 if g in reachable]
    record(judge(Control("N-GATE-COVERAGE", "nightly",
                         "a structural / display-visibility regression ships "
                         "overnight", "DETECT",
                         "nightly runs only: " + ", ".join(gates_run)),
                 len(gates_run) >= 5))


# ===========================================================================
# Report
# ===========================================================================

def report(require: int = 0) -> int:
    print("\n" + "=" * 78)
    dets = [c for c in RESULTS if c.expected == "DETECT" and not c.skipped]
    detected = [c for c in dets if c.verdict == "DETECTED"]
    missed = [c for c in dets if c.verdict == "MISSED"]
    bases = [c for c in RESULTS if c.expected == "quiet"]
    noisy = [c for c in bases if c.verdict == "noisy"]

    print(f"NEGATIVE CONTROLS: {len(detected)}/{len(dets)} detected")
    print(f"BASELINES (must stay quiet): {len(bases) - len(noisy)}/{len(bases)} quiet")
    if missed:
        print("\nMISSED — the gate did not notice:")
        for c in missed:
            print(f"  {c.name:<22} [{c.gate}] {c.damage}")
    if noisy:
        print("\nNOISY — the gate fired on undamaged input:")
        for c in noisy:
            print(f"  {c.name:<22} [{c.gate}]")

    if require and len(RESULTS) < require:
        print(f"\nFAIL: only {len(RESULTS)} control(s) ran, --require {require}. "
              f"A suite that checked nothing must never be indistinguishable from a "
              f"suite that checked everything.")
        RESULTS.append(Control("SUITE-COVERAGE", "suite",
                               f"only {len(RESULTS)} of >= {require} controls ran",
                               "DETECT", "under-run", "MISSED"))
    md = SCRATCH / "negative-controls.md"
    with md.open("w") as fh:
        fh.write("| control | gate | damage applied | expected | observed | verdict |\n")
        fh.write("|---|---|---|---|---|---|\n")
        for c in RESULTS:
            fh.write(f"| `{c.name}` | {c.gate} | {c.damage} | {c.expected} | "
                     f"{c.observed} | **{c.verdict}** |\n")
    print(f"\ntable -> {md}")
    if require and len(RESULTS) < require:
        return 1
    return 1 if (missed or noisy) else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", action="store_true", help="(re)build the scratch DB")
    ap.add_argument("--only", default="", help="run only controls whose name starts with this")
    ap.add_argument("--require", type=int, default=0,
                    help="fail unless at least this many controls actually ran. A suite "
                         "that silently checked nothing (archive unmounted, a group that "
                         "crashed) must not exit 0.")
    args = ap.parse_args()

    SCRATCH.mkdir(parents=True, exist_ok=True)
    if args.seed:
        seed()
        return 0

    random.seed(0)
    groups = [("T-", controls_text), ("P-", controls_pdf), ("V-", controls_volumes),
              ("C-", controls_toc), ("D-", controls_display), ("I-", controls_invariants),
              ("A-", controls_parse_accounting), ("N-", controls_nightly)]
    for prefix, fn in groups:
        if args.only and not prefix.startswith(args.only[:2]):
            continue
        try:
            fn()
        except Exception as exc:                     # a crashed group prints nothing
            print(f"  !! control group {prefix} CRASHED: {type(exc).__name__}: {exc}")
            c = Control(f"{prefix}GROUP", prefix, "the control group itself", "DETECT",
                        f"crashed: {type(exc).__name__}")
            c.verdict = "MISSED"
            RESULTS.append(c)
    return report(args.require)


if __name__ == "__main__":
    raise SystemExit(main())
