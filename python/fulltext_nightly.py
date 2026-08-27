#!/usr/bin/env python3
"""Nightly full-text automation for the document pipeline (Track A).

A thin orchestrator that runs the WHOLE pipeline once per night, top to bottom,
as `uv run python python/<stage>.py` subprocesses (same pattern as
fulltext_pipeline.py), and fails the process — so the GitHub workflow emails —
when a conversion is blocked or an acceptance gate fails. It is meant to run in
CI immediately after the metadata harvest (digitallibrary.documents is fresh),
so freshly adopted resolutions get their full text the same night.

Stages, in order (each is its own summary row):

  a. fetch-new     fulltext_fetch.py --catalog --rate 1.5
  b. recheck-recent  fulltext_fetch.py --recheck-recent-days 45
  c. pdf-fallback  fulltext_fetch_pdf.py --fallback-recent 45 --rate 1.8
  d. convert       flag native docx (in-process SQL, no soffice needed); then, if
                   any status='fetched' doc/wpd rows exist, run fulltext_convert.py
                   IF soffice is on PATH, else record CONVERSION-BLOCKED (continue,
                   fail at the very end).
  e. extract       fulltext_extract_raw.py   (status='converted' -> 'extracted')
  f. extract-pdf   fulltext_extract_pdf.py   (status='fetched' pdf -> 'extracted')
  g. parse         fulltext_parse.py --to-db --symbols-file <exact run manifest>
  h. gate          fulltext_verify_text.py --symbols <tonight's docx docs>
                   fulltext_verify_pdf.py  --symbols <tonight's pdf docs>

Exit code: 0 only if NO conversion was blocked, ALL gates passed, and no stage
subprocess failed. A blocked conversion NEVER exits early — every remaining
stage still runs and the process exits non-zero at the very end (so the human
gets one email and can clear it with a local run that has LibreOffice).

Design notes (see docs/fulltexts.md → Nightly automation):
  * `extract_raw` only takes status='converted', so native docx must be flagged
    'converted' first. `fulltext_convert.py` does that (flag_native_docx) but
    aborts at find_soffice() when LibreOffice is missing — which is the normal
    CI state. So the nightly flags native docx IN-PROCESS (a pure SQL UPDATE,
    imported from fulltext_convert) and only shells out to convert for the rare
    legacy doc/wpd files, which genuinely need LibreOffice.
  * Stage g does NOT pass --db-only: the acceptance gates read the parsed JSON
    from ARCHIVE_ROOT/parsed_dev, so the JSON must be written (it is ephemeral
    runner-temp in CI, consumed immediately by stage h in the same job).
  * The archive on CI is runner-temp and ephemeral. Files fetched tonight ARE
    present, so tonight's exact symbol manifest can be parsed and gated. Existing
    parsed docs and pre-existing extracted backlog are never selected implicitly.
    The DB is authoritative; the SSD archive is brought up to date locally with
    `fulltext_fetch.py --sync-archive`.

Usage:
    uv run python python/fulltext_nightly.py
    uv run python python/fulltext_nightly.py --limit 30   # bounded smoke test
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from fulltext_common import ARCHIVE_ROOT, get_conn
from fulltext_convert import flag_native_docx

REPO_ROOT = Path(__file__).resolve().parent.parent

WORD_FORMATS = ("docx", "doc", "wpd")
RECENT_DAYS = 45
FETCH_NEW_RATE = "1.5"
PDF_FALLBACK_RATE = "1.8"


# ---------------------------------------------------------------------------
# Subprocess stage runner (mirrors fulltext_pipeline.run_stage)
# ---------------------------------------------------------------------------

def run_stage(label: str, script: str, extra: list[str]) -> tuple[int, float]:
    cmd = ["uv", "run", "python", script, *extra]
    print(f"\n=== stage: {label} ===\n$ {' '.join(cmd)}", flush=True)
    t0 = time.monotonic()
    proc = subprocess.run(cmd, cwd=REPO_ROOT)
    return proc.returncode, time.monotonic() - t0


# ---------------------------------------------------------------------------
# Ledger snapshots / metrics
# ---------------------------------------------------------------------------

def snapshot() -> dict[tuple[str, str | None], int]:
    """{(status, format): count} over digitallibrary.document_files (lang='en')."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, format, count(*) FROM digitallibrary.document_files "
            "WHERE lang = 'en' GROUP BY status, format"
        )
        return {(r[0], r[1]): r[2] for r in cur.fetchall()}


def n_status(snap: dict, status: str, formats: tuple[str, ...] | None = None) -> int:
    return sum(c for (s, f), c in snap.items()
              if s == status and (formats is None or f in formats))


def extracted_rows() -> set[tuple[str, str]]:
    """English (symbol, format) rows currently at status='extracted'."""
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT symbol_normalized, format FROM digitallibrary.document_files "
            "WHERE lang = 'en' AND status = 'extracted'"
        )
        return {(s, f) for s, f in cur.fetchall()}


def write_symbols_manifest(path: Path, symbols: list[str]) -> None:
    """Atomically write exact targets; an empty manifest means no work."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(f"{symbol}\n" for symbol in sorted(set(symbols))),
                   encoding="utf-8")
    tmp.replace(path)


def newly_extracted(before: set[tuple[str, str]],
                    after: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Identity-based run delta; pre-existing backlog is intentionally excluded."""
    return after - before


# ---------------------------------------------------------------------------
# Convert-stage decision (pure + unit-testable — see __main__ self-test)
# ---------------------------------------------------------------------------

def soffice_available() -> bool:
    """True if LibreOffice's soffice is invokable (SOFFICE_BIN or on PATH)."""
    env = os.getenv("SOFFICE_BIN")
    if env and Path(env).exists():
        return True
    return shutil.which("soffice") is not None


def decide_convert(pending_docwpd: int, soffice: bool) -> str:
    """Decide the doc/wpd convert action. Native docx flagging is separate and
    always runs in-process (no soffice), so this concerns ONLY legacy doc/wpd.

    Returns:
      'skip'    — no legacy doc/wpd waiting (the normal 2023+ case).
      'run'     — legacy files waiting AND soffice available: run the converter.
      'blocked' — legacy files waiting but soffice missing: DO NOT convert; the
                  caller records CONVERSION-BLOCKED, continues, and exits non-zero
                  at the very end (fails the workflow -> email; clear with a local
                  run that has LibreOffice).
    """
    if pending_docwpd == 0:
        return "skip"
    return "run" if soffice else "blocked"


def _self_test() -> int:
    cases = [
        ((0, True), "skip"),
        ((0, False), "skip"),
        ((5, True), "run"),
        ((5, False), "blocked"),
    ]
    ok = True
    for (pending, soffice), want in cases:
        got = decide_convert(pending, soffice)
        flag = "ok" if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"  decide_convert(pending_docwpd={pending}, soffice={soffice}) "
              f"-> {got!r} (want {want!r}) [{flag}]")
    before = {("OLD", "pdf")}
    after = before | {("NEW-WORD", "docx"), ("NEW-PDF", "pdf")}
    got_delta = newly_extracted(before, after)
    want_delta = {("NEW-WORD", "docx"), ("NEW-PDF", "pdf")}
    if got_delta != want_delta:
        ok = False
        print(f"  FAIL exact extraction delta: got {got_delta}, want {want_delta}")

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        manifest = Path(tmp) / "symbols.txt"
        write_symbols_manifest(manifest, ["S/RES/2", "A/RES/1", "S/RES/2"])
        got_manifest = manifest.read_text(encoding="utf-8")
        if got_manifest != "A/RES/1\nS/RES/2\n":
            ok = False
            print(f"  FAIL exact manifest: {got_manifest!r}")
        write_symbols_manifest(manifest, [])
        if manifest.read_text(encoding="utf-8") != "":
            ok = False
            print("  FAIL empty manifest was not empty")
    print("self-test:", "PASS" if ok else "FAILED")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Nightly full-text automation")
    ap.add_argument("--limit", type=int,
                    help="cap targets on the fetch stages (bounded smoke test; "
                         "CI runs unbounded)")
    ap.add_argument("--self-test", action="store_true",
                    help="run the decide_convert unit self-test and exit")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    # CI archive root is runner-temp and may be empty: the fetch/convert scripts
    # assume these dirs exist, so create them up front.
    for sub in ("original", "converted"):
        (ARCHIVE_ROOT / sub).mkdir(parents=True, exist_ok=True)

    print(f"Nightly full-text run | ARCHIVE_ROOT={ARCHIVE_ROOT}")
    limit = ["--limit", str(args.limit)] if args.limit is not None else []

    results: list[tuple[str, str, float]] = []   # (label, result, seconds)
    stage_failed = False
    blocked = False
    metrics: dict[str, object] = {}

    def record(label: str, rc: int | None, dt: float, *, ok_label: str = "ok") -> None:
        nonlocal stage_failed
        if rc is None:
            results.append((label, "skipped", dt))
            return
        good = rc == 0
        if not good:
            stage_failed = True
        results.append((label, ok_label if good else "FAILED", dt))

    # -- a. fetch-new --------------------------------------------------------
    s0 = snapshot()
    rc, dt = run_stage("fetch-new", "python/fulltext_fetch.py",
                       ["--catalog", "--rate", FETCH_NEW_RATE, *limit])
    record("fetch-new", rc, dt)
    s_a = snapshot()
    metrics["new"] = n_status(s_a, "fetched", WORD_FORMATS) - n_status(s0, "fetched", WORD_FORMATS)
    metrics["absences_recorded"] = n_status(s_a, "unavailable") - n_status(s0, "unavailable")

    # -- b. recheck-recent ---------------------------------------------------
    rc, dt = run_stage("recheck-recent", "python/fulltext_fetch.py",
                       ["--recheck-recent-days", str(RECENT_DAYS), *limit])
    record("recheck-recent", rc, dt)
    s_b = snapshot()
    metrics["rechecked_rescued"] = (
        n_status(s_b, "fetched", WORD_FORMATS) - n_status(s_a, "fetched", WORD_FORMATS))

    # -- c. pdf-fallback -----------------------------------------------------
    rc, dt = run_stage("pdf-fallback", "python/fulltext_fetch_pdf.py",
                       ["--fallback-recent", str(RECENT_DAYS), "--rate", PDF_FALLBACK_RATE, *limit])
    record("pdf-fallback", rc, dt)
    s_c = snapshot()
    metrics["pdf_fallback_rescued"] = (
        n_status(s_c, "fetched", ("pdf",)) - n_status(s_b, "fetched", ("pdf",)))

    # -- d. convert (native docx flag in-process; doc/wpd via subprocess) -----
    with get_conn() as conn:
        flagged = flag_native_docx(conn)
    pending_docwpd = n_status(s_c, "fetched", ("doc", "wpd"))
    action = decide_convert(pending_docwpd, soffice_available())
    print(f"\n=== stage: convert ===\nnative docx flagged 'converted': {flagged} | "
          f"pending doc/wpd: {pending_docwpd} | soffice: {soffice_available()} | "
          f"decision: {action}")
    converted_docwpd = 0
    if action == "run":
        rc, dt = run_stage("convert", "python/fulltext_convert.py", [])
        record("convert", rc, dt)
        after = snapshot()
        converted_docwpd = max(0, n_status(after, "converted") - n_status(s_c, "converted") - flagged)
    elif action == "blocked":
        blocked = True
        print("!! CONVERSION-BLOCKED: legacy doc/wpd files are waiting but LibreOffice "
              "(soffice) is not on PATH. Skipping conversion; native docx still flowed. "
              "The run will exit non-zero at the end.")
        results.append(("convert", "BLOCKED", 0.0))
    else:  # skip
        results.append(("convert", "skipped", 0.0))
    metrics["converted"] = flagged + converted_docwpd
    metrics["blocked_docwpd"] = pending_docwpd if blocked else 0

    # -- e. extract (docx) ---------------------------------------------------
    extracted_before = extracted_rows()
    rc, dt = run_stage("extract", "python/fulltext_extract_raw.py", [])
    record("extract", rc, dt)

    # -- f. extract-pdf ------------------------------------------------------
    rc, dt = run_stage("extract-pdf", "python/fulltext_extract_pdf.py", [])
    record("extract-pdf", rc, dt)
    after_ext = snapshot()
    extracted_after = extracted_rows()
    run_rows = newly_extracted(extracted_before, extracted_after)
    run_symbols = sorted({symbol for symbol, _fmt in run_rows})
    word_syms = sorted(symbol for symbol, fmt in run_rows if fmt in WORD_FORMATS)
    pdf_syms = sorted(symbol for symbol, fmt in run_rows if fmt == "pdf")
    manifest = ARCHIVE_ROOT / "run_manifests" / "nightly-symbols.txt"
    write_symbols_manifest(manifest, run_symbols)
    metrics["extracted"] = len(run_symbols)

    # -- g. parse ------------------------------------------------------------
    parsed_before = n_status(after_ext, "parsed")
    if not run_symbols:
        print("\n=== stage: parse ===\nno newly-extracted docs — skipping parse and gates.")
        results.append(("parse", "skipped", 0.0))
        metrics["parsed"] = 0
        gate_ran = False
        gate_failed = False
    else:
        rc, dt = run_stage("parse", "python/fulltext_parse.py",
                           ["--to-db", "--symbols-file", str(manifest)])
        record("parse", rc, dt)
        after_parse = snapshot()
        metrics["parsed"] = max(0, n_status(after_parse, "parsed") - parsed_before)

        # -- h. gates --------------------------------------------------------
        gate_ran = True
        gate_failed = False
        if word_syms:
            rc, dt = run_stage("gate-text", "python/fulltext_verify_text.py",
                               ["--symbols", *word_syms])
            results.append(("gate-text", "pass" if rc == 0 else "FAIL", dt))
            gate_failed = gate_failed or rc != 0
        else:
            results.append(("gate-text", "skipped", 0.0))
        if pdf_syms:
            rc, dt = run_stage("gate-pdf", "python/fulltext_verify_pdf.py",
                               ["--symbols", *pdf_syms])
            results.append(("gate-pdf", "pass" if rc == 0 else "FAIL", dt))
            gate_failed = gate_failed or rc != 0
        else:
            results.append(("gate-pdf", "skipped", 0.0))

    # -- i. volume-split (late stage) ----------------------------------------
    # Recover GA/ECOSOC decisions from the born-digital compilation volumes that DL
    # harvests ~yearly. Self-contained subprocess sequence (fetch -> extract-pdf ->
    # split -> parse children -> volume gate); the fetch mode skips volumes already
    # present and the split's sha256-gate skips unchanged volumes, so this is a
    # cheap no-op on a night with no new supplement. Early-HRC Word reports are a
    # one-time local backfill (LibreOffice-dependent), not part of the nightly.
    vol_before = n_status(snapshot(), "parsed")
    rc, dt = run_stage("volume-split", "python/fulltext_split_volumes.py", ["--nightly"])
    record("volume-split", rc, dt)
    metrics["volume_children_parsed"] = max(0, n_status(snapshot(), "parsed") - vol_before)

    # -- stage summary table -------------------------------------------------
    print("\n=== stage summary ===")
    print(f"{'stage':<15} {'result':<9} {'seconds':>9}")
    print(f"{'-' * 15} {'-' * 9} {'-' * 9}")
    for label, result, dt in results:
        print(f"{label:<15} {result:<9} {dt:>9.1f}")

    # -- night summary -------------------------------------------------------
    gate_str = ("pass" if gate_ran and not gate_failed
                else "FAIL" if gate_failed else "n/a")
    print("\n=== night summary ===")
    print(f"  new (fetched)          : {metrics['new']}")
    print(f"  rechecked-rescued      : {metrics['rechecked_rescued']}")
    print(f"  pdf-fallback-rescued   : {metrics['pdf_fallback_rescued']}")
    print(f"  converted (docx+legacy): {metrics['converted']}"
          + (f"  (BLOCKED: {metrics['blocked_docwpd']} doc/wpd)" if blocked else ""))
    print(f"  extracted              : {metrics['extracted']}")
    print(f"  parsed                 : {metrics['parsed']}")
    print(f"  volume children parsed : {metrics.get('volume_children_parsed', 0)}")
    print(f"  gates                  : {gate_str}")
    print(f"  absences recorded      : {metrics['absences_recorded']}")

    ok = not blocked and not gate_failed and not stage_failed
    result_path = ARCHIVE_ROOT / "run_manifests" / "nightly-result.json"
    result_tmp = result_path.with_suffix(".json.tmp")
    result_tmp.write_text(json.dumps({
        "run_id": os.getenv("GITHUB_RUN_ID"),
        "ok": ok,
        "blocked": blocked,
        "gate_failed": gate_failed,
        "stage_failed": stage_failed,
        "targets": run_symbols,
        "metrics": metrics,
        "stages": [{"stage": label, "result": result, "seconds": round(dt, 3)}
                   for label, result, dt in results],
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result_tmp.replace(result_path)
    print(f"  result                  : {result_path}")
    if blocked:
        print("\nFINAL: CONVERSION-BLOCKED — legacy doc/wpd need LibreOffice. Run "
              "`uv run python python/fulltext_pipeline.py` locally (with soffice) to "
              "clear, then re-run the gates. Exiting non-zero.")
    elif gate_failed:
        print("\nFINAL: an acceptance gate FAILED — investigate before trusting tonight's "
              "parse. Exiting non-zero.")
    elif stage_failed:
        print("\nFINAL: a stage subprocess returned non-zero — see the table above. "
              "Exiting non-zero.")
    else:
        print("\nFINAL: clean night. Exiting 0.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
