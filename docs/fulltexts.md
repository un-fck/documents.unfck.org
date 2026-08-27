# Document Full-Text Pipeline (Track A)

Fetch, archive, convert, and extract the full text of UN resolutions, decisions,
and presidential statements — beyond the MARC metadata the harvest already
mirrors. This document covers **Track A, stage 1**: getting native files onto
disk, normalizing them to `.docx`, and defining the raw extraction layer.

## Two-layer model

The pipeline deliberately separates **archival + low-interpretation extraction**
from **semantic interpretation**:

1. **`digitallibrary.document_files`** — a fetch/convert/extract *ledger*. One row
   per `(symbol_normalized, lang)`. Tracks format, hashes, archive paths,
   conversion, and status through the pipeline.
2. **`digitallibrary.document_paragraphs_raw`** — a *low-interpretation*
   extraction layer. Document-ordered paragraphs with just enough structure
   (numbering, styles, italics/bold, indentation, table cells, hyperlinks,
   footnotes) to reconstruct meaning later, but **no** semantic labelling.
3. **Semantic layer.** Normalized preambular/operative
   paragraphs, mandate objects, cross-references, etc. will land as
   **migration `003`**.
4. **`digitallibrary.document_text_metrics`** — a versioned, derived metric row
   for each successfully parsed English document. It is disposable and can be
   rebuilt from the semantic layer.

## Canonical text metrics

`python/fulltext_metrics.py` defines the canonical `substantive-v1` profile used
for document word length and recurrence-series comparisons. It uses only
`digitallibrary.document_paragraphs`; legacy Mandates text and raw PDF-to-text
are never inputs.

Included semantic elements are title, opening formula, heading, paragraph and
table, across main text, annexes and appendices. Frontmatter/mastheads, page
boilerplate, footnotes, dividers, vote records and signatures are excluded.
Words are deterministic Unicode letter/number runs with an optional internal
apostrophe. Numbers and meaningful one-character words are retained. The
NFKC/case-folded token stream is stored alongside its SHA-256, parser version,
profile/metric versions and timestamp; this gives downstream comparison jobs an
exact input and makes unchanged nightly upserts no-ops.

Apply migration 006, then backfill once:

```bash
psql "$DATABASE_URL" -f sql/migrations/006_document_text_metrics.sql
uv run python python/fulltext_metrics.py --all
```

The 03:00 nightly runs the same command against its exact-symbol manifest after
ordinary parses and volume-split children. Explicit repair/backfill scopes are
also supported with `--symbols ...` or `--symbols-file path`. Re-running is safe;
rollback is simply dropping the derived table and removing the metrics stage.

The re-parse philosophy: **the SSD archive is ground truth**, and
`document_paragraphs_raw` is a disposable re-parse substrate. Any extractor bug
is fixed by re-running extraction over the archived files — we never re-fetch
from ODS to fix a parsing mistake, and never treat the raw table as canonical.

## Archive location

```
/Volumes/SSDAStorage/digitallibrary-fulltexts/
  original/    native ODS files, as fetched   (e.g. A_RES_60_1.doc)
  converted/   LibreOffice-produced .docx      (e.g. A_RES_60_1.docx)
```

This is a **local SSD on David's Mac**, not cloud storage. Consequently **this
pipeline runs locally, not in CI** (unlike the nightly MARC harvest). Ledger rows
store archive paths *relative* to this root, so the root can move (override with
`FULLTEXT_ARCHIVE_ROOT`). `original/` and `converted/` already exist.

## Corpus definition

Candidate families: **8 symbol families**, English, published **1994-01-01 or
later**:

```
A/RES/  A/DEC/  S/RES/  S/PRST/  E/RES/  E/DEC/  A/HRC/RES/  A/HRC/PRST/
```

Selection query (see `python/fulltext_fetch.py`): `deleted_at IS NULL`,
`date_publication >= '1994-01-01'`, English (lenient: `languages` empty **or**
`'eng' = ANY(languages)`), `symbol_normalized` matching the family regex,
`DISTINCT ON (symbol_normalized)` keeping the highest `recid`.

**Two families are excluded from default catalog selection** because they are
structurally absent from ODS (verified by hand, 0/85 fetched in the sample run):

- **`A/DEC/*` and `E/DEC/*` (GA and ECOSOC decisions).** Decisions are not issued
  as standalone ODS documents — they are only published inside *compilation
  volumes* (e.g. the GA "Resolutions and Decisions" fascicles). Fetching them at
  the individual-symbol level cannot work. Their full text is instead recovered
  by the **volume-split pipeline** (`python/fulltext_split_volumes.py`, see the
  *Volume-split pipeline* section below): fetch the born-digital compilation
  volume, extract it, then split decisions out into child rows. They remain
  reachable via `--symbols-file` too if you list them explicitly.

Not excluded, but expect misses: **early Human Rights Council sessions** (roughly
`A/HRC/RES/1/*` through `A/HRC/RES/11/*`, 2006–2009) are mostly absent from ODS
too, while later sessions are fine. HRC is *not* excluded — the fetch retry logic
simply marks the absent early ones `unavailable`.

**Bracket pseudo-symbols.** Digital Library invents sub-record symbols like
`A/RES/50/204[A]` or `A/RES/63/108[B-IV]` for the parts of a combined resolution.
ODS only knows the parent (`A/RES/50/204`). The fetcher **never issues a request
containing `[`**: bracketed targets are collapsed onto their parent — dropped
when the parent is already a target or already in the ledger (the parent's file
covers all parts), otherwise the parent file is fetched and stored under the
parent `symbol_normalized`. Every run prints how many were collapsed.

**~17.7k pre-1994 documents are PDF-only** on ODS (no Word source). These are
handled by a separate DETERMINISTIC PDF path (no OCR re-run, no LLM) documented in
its own section below (**PDF path (pre-1994)**). Bulk fetching them is DEFERRED
until the Word backfill completes, to avoid contending for the ODS budget.

## Format eras (empirical)

ODS returns the *native* file; `t=docx` does **not** convert. What comes back
depends on when the document was produced:

| Era          | Format            | Magic bytes      | Handling                    |
|--------------|-------------------|------------------|-----------------------------|
| ~1993–2000   | WordPerfect 5.1   | `FF 57 50 43`    | convert → docx (LibreOffice)|
| ~2000s–2010s | binary `.doc`     | `D0 CF 11 E0`    | convert → docx (LibreOffice)|
| recent       | real `.docx`      | `PK` (zip)       | ready as-is                 |
| —            | PDF               | `%PDF`           | out of scope (pre-1994)     |
| failure      | HTML "not found"  | starts with `<`  | status `unavailable`        |

**Critical:** a *failed* lookup still returns **HTTP 200** with a ~1.3 KB
`text/html` page. Format is therefore sniffed from **magic bytes, never the
Content-Type header**. A curl-like `User-Agent` (`curl/8.7.1`) avoids the WAF
JS challenge.

**Transient soft-block.** In the 317-doc sample run (1 req/s), after ~250–300
consecutive requests ODS began returning that same 1.3 KB HTML page for
documents that *are* available (the same symbols succeeded again minutes later).
The block is invisible in the response — it looks exactly like a genuine
not-found. The fetcher defends against it on two levels: per-symbol soft retries
(30 s then 120 s before recording `unavailable`) and the politeness/circuit-
breaker machinery in the next section that keeps request volume far below the
threshold that triggered the block.

## Status lifecycle

`document_files.status`:

- `fetched` — original archived, format sniffed.
- `unavailable` — ODS returned HTML/unknown *even after the soft-block retries*
  (30 s + 120 s); no file saved. Because ODS transiently soft-blocks (its "not
  available" HTML page is byte-identical whether the doc is genuinely missing or
  we are just being throttled), some `unavailable` rows are false negatives —
  recover them with a later `--recheck-unavailable` pass.
- `failed` — network/HTTP error after retries; **retried** on the next fetch run.
- `converted` — a valid `.docx` exists (native, `converted_path` NULL; or produced
  from doc/wpd, `converted_path` set).
- `convert_failed` — LibreOffice produced nothing usable; `error` records why.
- `extracted` — raw paragraphs written (later stage).
- `no_text_layer` — **PDF path only.** The archived PDF is a pure image scan with
  no usable embedded text layer (triage class `none`); nothing is extracted. These
  are the corpus-wide coverage loss for the deterministic PDF path (no OCR is run).

## Runbook

All commands run **locally** (archive is on the local SSD), from the repo root,
with `DATABASE_URL` in `.env`.

### 1. Apply the migration

```bash
psql "$DATABASE_URL" -f sql/migrations/002_add_fulltexts.sql
# (from-scratch equivalent: sql/schema/fulltext_tables.sql)
```

### 2. Fetch native files from ODS

The fetcher runs **deliberately slowly to avoid ODS soft-blocking** (see Format
eras above). It targets **≲1200 requests/hour**: ~3 s between requests with ±30%
jitter, plus a **3–5 min rest break after every ~150 requests**, plus long
back-offs on any sign of throttling. **Budget roughly 8 hours of wall-clock time for
the full backfill.** This is by design — start it and leave it running.

Anti-block behaviour, in order of severity:

- **Soft retries.** An html/unknown response is retried up to 2 more times
  (30 s, then 120 s) before the symbol is recorded `unavailable`.
- **Rest breaks.** After every ~150 requests, a random 3–5 min pause regardless
  of whether anything failed.
- **Back-off.** HTTP 429/403 or a connection reset ⇒ a 10-min pause before
  retrying; it also counts toward the circuit breaker.
- **Circuit breaker (tiered).** 8 consecutive block/miss outcomes ⇒ pause
  15 min (1st trip), 60 min (2nd trip), then **exit cleanly on the 3rd** — a
  crash is worse than stopping, and the run is resumable.

Each progress line logs cumulative requests and current requests/hour so the
friendliness is auditable.

```bash
# Full corpus — expect ~a day of wall clock. Run detached (tmux/nohup).
uv run python python/fulltext_fetch.py

# Try a small batch first
uv run python python/fulltext_fetch.py --limit 100 --rate 4

# Fetch a specific priority list (one symbol per line). This is also the ONLY
# way to reach A/DEC and E/DEC decisions, which are excluded by default.
uv run python python/fulltext_fetch.py --symbols-file priority.txt

# Preview targets without fetching (also prints excluded/collapsed counts)
uv run python python/fulltext_fetch.py --dry-run
```

**Recommended two-pass workflow.** Because some `unavailable` rows are transient
soft-block false negatives, do a bulk pass first, then a slow second pass that
re-probes only the misses:

```bash
# Pass 1 — bulk backfill (a day-ish)
uv run python python/fulltext_fetch.py

# Pass 2 — re-probe everything marked 'unavailable'; rows that now fetch are
# overwritten to 'fetched'. Structurally-absent decisions are skipped here too.
uv run python python/fulltext_fetch.py --recheck-unavailable
```

Repeat pass 2 as needed; genuinely-absent documents (early HRC, etc.) stay
`unavailable` no matter how often you re-probe.

Safe to Ctrl-C and re-run: already-fetched/unavailable symbols are skipped (except
under `--recheck-unavailable`, which deliberately revisits them); only `failed`
rows are retried on a normal run. A resume watermark is persisted to
`digitallibrary.harvest_state` under key `fulltext_fetch`.

### 3. Convert legacy files to docx

```bash
# Native docx flagged ready in bulk; doc + wpd converted with 4 workers
uv run python python/fulltext_convert.py

# More parallelism / a capped run
uv run python python/fulltext_convert.py --workers 8 --limit 500
```

Requires LibreOffice (`soffice` on PATH, or set `SOFFICE_BIN`). Each worker uses
an isolated `-env:UserInstallation` profile under `/tmp/lo_profile_<n>` so
parallel instances don't collide over the shared profile.

### 5. Acceptance gate — text preservation (run before any bulk parse)

`python/fulltext_verify_text.py` is the **acceptance gate** for the semantic
parse. It is independent of `document_paragraphs_raw`: it re-reads the
ground-truth word content straight from each archived `.docx` (body + tables +
foot/endnotes) and checks, per document, that every content word survives into
the parsed JSON in `parsed_dev/`. It exits **nonzero if any document shows genuine
token loss**, so it can gate CI and the ~20k bulk run.

```bash
# whole corpus (exit 0 == every doc preserved; nonzero == investigate)
uv run python python/fulltext_verify_text.py

# a subset / one family while iterating
uv run python python/fulltext_verify_text.py --symbols A/RES/80/167 S/RES/2806(2025)
uv run python python/fulltext_verify_text.py --limit 100 --verbose
```

Two comparison artifacts are decomposed away as **known-benign** so they never
false-positive (see the module docstring): (1) **vote-JSON keys** — a vote
record's tally labels (`In favour`/`Against`/`Abstaining`/…) are structural JSON,
not element text, while the member-state names *are* compared; (2) **tokenizer
run-joins** — where python-docx fuses two words across a soft line break
(`Commissioner`+`for`, `a`+`See`, `on`+`30`) and the parser keeps them correctly
separate, so the fused docx token segments back into parsed tokens. Hyphens/
apostrophes are normalised on both sides first; bare numbers, ≤2-char fragments,
and the document's own symbol (running-header `PRST`/`RES`) are ignored.

**Current known residual (not a parser defect):** exactly **3** documents fail —
`S/PRST/2001/9`, `S/PRST/2014/3`, `S/RES/1881(2009)` — on a "Reissued for
technical reasons …" provenance note that the **raw extractor** drops before the
parser sees it (verified absent from `document_paragraphs_raw`); a `.doc→.docx`
conversion duplicates it 4×. Fix belongs upstream in extraction. Acknowledge once
triaged with `--ignore-symbols S/PRST/2001/9 S/PRST/2014/3 'S/RES/1881(2009)'`
(or raise `--max-loss`) so the gate is green on the rest. Aggregate preservation
across the 763-doc corpus is **99.998 %** (760/763 clean).

### 4. Extract raw paragraphs

`python/fulltext_extract_raw.py` reads each `converted` docx and writes the
low-interpretation rows to `digitallibrary.document_paragraphs_raw`
(`extractor_version = raw-v2`), advancing status `converted → extracted` (failures
`extract_failed`, error recorded). Re-derivable from the archive at any time.

```bash
uv run python python/fulltext_extract_raw.py            # all 'converted' docs
uv run python python/fulltext_extract_raw.py --limit 20
uv run python python/fulltext_extract_raw.py --force    # also re-extract 'extracted' rows
```

### 6. Semantic parse → JSON + semantic DB

`python/fulltext_parse.py` (currently parser_version `sem-v5`) classifies the raw rows into
semantic elements **and annotates each operative/preambular element with its action
verb** (see *Action-verb annotation (migration 004)* below). It always writes one
JSON per doc to `parsed_dev/`; with `--to-db` it *also* loads the two semantic tables
(migrations 003 + 004 columns) and advances status `extracted → parsed`. Its safe
default targets only `extracted` rows. Existing `parsed` rows are admitted only
with `--reparse` plus an explicit symbol or symbol manifest.

```bash
uv run python python/fulltext_parse.py                  # JSON only (parsed_dev/*.json)
uv run python python/fulltext_parse.py --to-db          # JSON + semantic DB
uv run python python/fulltext_parse.py --db-only        # semantic DB only, skip JSON
uv run python python/fulltext_parse.py --symbols-file tonight.txt --to-db
uv run python python/fulltext_parse.py --symbol A/RES/48/75 --reparse --to-db
```

Loading is **delete-then-insert per `(symbol_normalized, lang)`** in both tables,
batched over short-lived ~20-doc connections. Before any DB mutation, every
target's archived source must exist and its archived-original sha256 must match
the ledger. Accounting, source-conservation and overreach checks then run before
the delete/insert transaction. A rejected new extraction becomes `parse_failed`
without semantic rows; a rejected explicit reparse preserves the last good rows
and `parsed` status.

An empty `--symbols-file` means **zero work**. It never falls back to the default
queue. Corpus-wide reparsing is intentionally not a default CLI mode: construct
and review an explicit manifest on a host with the persistent archive.

### 7. Full top-up cycle (orchestrator)

After a fetch batch lands new `fetched` rows, `python/fulltext_pipeline.py` runs
the three post-fetch stages in order as subprocesses — `convert → extract_raw →
parse --to-db` — prints a per-stage summary table, and exits non-zero if any
stage fails (a failing stage aborts the rest). This is what a cron/manual top-up
calls; **fetch is deliberately not part of it** (slow, soft-block-sensitive, run
detached — step 2). `--limit N` is forwarded to every stage for smoke tests.

```bash
# after fetch batches land:
uv run python python/fulltext_pipeline.py               # full top-up cycle
uv run python python/fulltext_pipeline.py --limit 20    # smoke test
uv run python python/fulltext_pipeline.py --workers 8

# then the acceptance gate + review harness over the fresh output:
uv run python python/fulltext_verify_text.py
```

## Nightly automation

`python/fulltext_nightly.py` is the CI-driven top-to-bottom automation: it runs
the **whole** pipeline once per night, in the **same GitHub job** as (and right
after) the metadata harvest, so a freshly-adopted resolution gets its full text
the same night. It is a thin orchestrator that shells out to the exact same stage
scripts a human runs locally (`uv run python python/<stage>.py`) — **DRY: the
nightly and local runs share one implementation.** It differs from
`fulltext_pipeline.py` only in that it *includes* the fetch stages and enforces
CI failure semantics.

### The flow

| # | stage | command | targets |
|---|-------|---------|---------|
| a | fetch-new | `fulltext_fetch.py --catalog --rate 1.5` | brand-new catalog symbols (self-targeting; a no-op when the corpus is current) |
| b | recheck-recent | `fulltext_fetch.py --recheck-recent-days 45` | `unavailable` rows published — or first seen — in the last 45 days (freshly adopted docs lag ODS by days-to-weeks) |
| c | pdf-fallback | `fulltext_fetch_pdf.py --fallback-recent 45 --rate 1.8` | recent `unavailable` (word-missing) symbols, re-probed as `t=pdf`; a hit overwrites to `fetched`/`pdf` |
| d | convert | native docx flagged in-process; `fulltext_convert.py` **iff** doc/wpd waiting **and** soffice present | see below |
| e | extract | `fulltext_extract_raw.py` | `status='converted'` → `extracted` |
| f | extract-pdf | `fulltext_extract_pdf.py` | `status='fetched'` pdf → `extracted` |
| g | parse | `fulltext_parse.py --to-db --symbols-file <run manifest>` | exact symbols that changed from a non-extracted status to `extracted` in this run |
| h | gate | `fulltext_verify_text.py --symbols <tonight's docx docs>` + `fulltext_verify_pdf.py --symbols <tonight's pdf docs>` | text-preservation acceptance |

Each stage prints its own summary row; a final **night summary** reports `new`,
`rechecked-rescued`, `pdf-fallback-rescued`, `converted`/blocked, `extracted`,
`parsed`, gate pass/fail, and absences recorded.

The run manifest carries document identities, not merely a count. Pre-existing
`extracted` backlog and every `parsed` document are excluded. The same exact list
feeds parsing and the Word/PDF gates. This is required because the CI archive is
ephemeral: it contains tonight's downloads, not historical source files.
Every run also writes `run_manifests/nightly-result.json`; GitHub Actions uploads
that result, the exact manifests and any rejected parse JSON for 30 days even
when the pipeline fails.

`--recheck-recent-days N` / `--fallback-recent N` select `unavailable` rows whose
document `date_publication` is within N days — deliberately publication-date-only.
Freshly adopted documents lag ODS by days-to-weeks (this is what the window is
for); late-HARVESTED older records have no ledger row yet and are picked up by
the fetch-new stage instead. An `updated_at`-based window was rejected: right
after a backfill it would sweep thousands of historical absences into every
nightly run and blow the CI time budget.

### Native docx flagging vs. LibreOffice

`extract_raw` only takes `status='converted'`, so native docx (`fetched`) must be
flagged `converted` first. `fulltext_convert.py` does that (`flag_native_docx`) —
but it calls `find_soffice()` **first** and aborts if LibreOffice is missing,
which is the normal CI state. Since **2023+ docs are 100 % native docx**, the
nightly therefore flags native docx **in-process** (a pure SQL `UPDATE`, imported
from `fulltext_convert`) and only shells out to the converter for the rare legacy
`doc`/`wpd` files, which genuinely need LibreOffice. The convert decision
(`decide_convert(pending_docwpd, soffice)`) is a pure, unit-tested function
(`fulltext_nightly.py --self-test`).

### Conversion-blocked failure semantics

If legacy `doc`/`wpd` files are waiting **and** `soffice` is not on PATH, the
nightly records **CONVERSION-BLOCKED**: it does **not** convert, it **does** run
every remaining stage (native docx already flowed), and it prints a clear final
line and **exits non-zero at the very end** — which fails the GitHub workflow and
sends an email. It never exits early. Clear a blocked night with a **local** run
that has LibreOffice (`uv run python python/fulltext_pipeline.py`), then re-run the
gates. A **gate failure** likewise prints everything and then exits non-zero.
`fulltext_nightly.py` exits `0` **only** when no conversion was blocked, all gates
passed, and no stage subprocess failed.

### CI archive semantics (ephemeral)

The workflow sets `FULLTEXT_ARCHIVE_ROOT=${{ runner.temp }}/fulltext-archive`
(CI has no SSD). `fulltext_nightly.py` `mkdir`s `original/` and `converted/` under
it at startup. This archive is **ephemeral** — it exists only for the job. Files
fetched **tonight** are present, so tonight's docs are fetched → extracted →
parsed → **gated** end-to-end in the one job; the parsed JSON the gates read
(`parsed_dev/`) is written to the same runner-temp and consumed immediately (so
stage g does **not** pass `--db-only`). The **DB is authoritative**; the SSD
archive is authoritative-of-files. Bring the local SSD archive up to date with:

```bash
uv run python python/fulltext_fetch.py --sync-archive   # local-only; re-downloads any
                                                        # archived original missing on disk
```

`--sync-archive` is deliberately **not** part of the nightly (it would try to
re-download the whole corpus into runner-temp every night). It handles both Word
and PDF rows (routing `t=pdf` for `format='pdf'`), so the Word fetcher covers it
and `fulltext_fetch_pdf.py` needs no `--sync-archive` of its own.

> **Precondition before enabling the workflow (current state):** the nightly runs
> the frozen extract/parse stages over **all** `fetched`/`converted` rows. Because
> the CI archive is ephemeral, any such **backlog** whose files live only on the
> SSD (e.g. the in-flight pre-1994 PDF backfill) would be marked `extract_failed`
> the first time the nightly runs in CI. **Drain the backlog locally** (extract +
> parse everything, so no `fetched`/`converted` rows remain) before turning the
> workflow on. In steady state the only `fetched` rows are tonight's fetches,
> whose files are present, and this is a non-issue.

## PDF path (pre-1994)

The ~17.7k documents published before 1994 have **no Word source** on ODS — only
PDFs. This path turns those PDFs into the **same** `document_paragraphs_raw`
contract the Word path produces, so the **frozen** semantic parser
(`fulltext_parse.py`, sem-v2) consumes them unchanged through its style-less
lexical path. It is **fully deterministic — no OCR re-run, no LLM.** It only uses
the embedded text layer that is already in the PDF (born-digital text, or the OCR
layer a scan was saved with).

### The three kinds of pre-1994 PDF

- **Born-digital** (~1990–1993): a clean embedded text layer, one resolution per
  file, a UN masthead front. Parses like a modern doc.
- **Scanned compilation-volume excerpts** (older): an OCR text layer of variable
  quality, laid out as a page of a *"Resolutions adopted …"* supplement —
  **two-column**, and each file's page holds the **END of the previous
  resolution, the target, and the START of the next**, under a running page
  header. Old GA/ECOSOC and SC volumes are also frequently **bilingual**
  (French + English on the same page).
- **Pure image scans**: **no text layer at all** — unrecoverable here, excluded.

### Triage classes (this predicts corpus coverage)

`fulltext_extract_pdf.py` scores every PDF's text layer (chars/page, alphanumeric
ratio, common-word hit rate, garbage-run ratio) and classifies it:

- **`text`** — clean enough to trust (born-digital or good OCR). Extracted; the
  acceptance gate holds it to the full bar.
- **`poor`** — marginal OCR. Extracted anyway, flagged (`textlayer_score=poor` on
  every row's props); the gate holds it to a looser bar.
- **`none`** — a pure image scan, no usable text. **Skipped**, ledger status
  `no_text_layer`. **This is the coverage loss** — no OCR is run.

On the stratified 64-doc sample (families × decades, 1940s–1980s): **`text` 51 %,
`poor` 3 %, `none` 46 %.** So expect the deterministic path to yield usable text
for **roughly half** of the pre-1994 corpus; the ~46 % pure scans need a future OCR
stage to recover. `none` skews to the oldest and to ECOSOC volumes.

### What the extractor does (`pdf-v1`)

1. **pymupdf spans → lines**, with per-span font size and bold/italic flags
   (old scans usually have no italics — fine).
2. **Drop running headers/footers/page numbers**: a top/bottom band line that is a
   page number, a doc-symbol string, a separator rule, a *"…Session"* /
   *"Resolutions adopted…"* running header, or that **repeats across pages** — the
   parser only drops page artifacts while in the *front* state, so mid-body headers
   on continuation pages MUST be removed here or they poison the parse.
3. **Detect columns by a GUTTER** (a near-empty central vertical strip), so a real
   two-column supplement page splits into left-then-right reading order while a
   born-digital hanging-number layout stays single-column.
4. **Separate small-font footnote lines** (body ~9 pt, footnotes ~5 pt) and append
   them as `kind='footnote'` rows, so column-bottom footnote apparatus does not
   glue onto body text.
5. **Reconstruct paragraphs** by column left-edge indent + vertical gaps + terminal
   punctuation, repairing end-of-line hyphenation; merge a hanging marker (`1.`,
   `(a)`) into its clause so born-digital docs still yield `1. Requests …`.
6. **Repair OCR-garbled anchors, conservatively.** Three sequence/vocabulary-
   confirmed repairs, each touching only a marker or lead word (body text stays
   verbatim OCR for the acceptance gate):
   - the **opening formula** (`The General Assemb/y,` → `The General Assembly,`) —
     the parser anchors its preamble/operative state machine on that exact line, so
     this repair is what labels the whole preamble `preambular` not `frontmatter`;
   - a **mis-OCR'd leading digit marker** (`I.`/`l.`→`1.`, `S.`→`5.`) — rewritten
     **only** when the neighbouring real numeric markers at the same indent
     arithmetically confirm it (`prev+1 == candidate == next-1`), so a genuine roman
     `I.`/`II.` heading (followed by `II.`, not `2.`) is never touched. This recovers
     the first operative of a list whose `1.` was read as `I.`;
   - a **single-substitution lead-verb corruption** (`Recallinx`→`Recalling`,
     `Gravelv`→`Gravely`) — first word only, unique edit-distance-1 match against the
     preambular/operative verb vocabulary, and only when the damaged letter is an
     OCR-junk glyph (`x v z j q`), so genuine inflections (`authorized`→`authorizes`)
     are left alone.
7. **Exclude facing-language (French) lines.** Old GA/ECOSOC/SC volumes print an
   English column facing a French one; a line carrying ≥3 French function words is
   dropped from both the body and (independently) the gate's ground truth, so the
   interleaved French column neither truncates the English crop nor floods the gate.
8. **Crop to the target resolution** inside the excerpt: start at its own number
   heading (`1260 (XIII).`, `48/23.`, `Resolution 639 (1989)`), end at its adoption
   record (`Nth plenary meeting` / `Adopted …`), the next **different** resolution
   heading, or an SC `Decision(s)` block — including a **narrative** Decisions block
   (`Decisions At its Nth meeting, … the Council decided …`), whose bleed used to
   pull the following resolution into the region. If the anchor is uncertain the
   extractor keeps everything and **flags it** (`crop_anchor_not_found_*`) — it never
   silently truncates.

### Quality to expect per family/era

- **GA & ECOSOC resolutions** (old two-column and born-digital): the best case.
  Opening formula + preambular clauses + numbered operatives + adoption line parse
  cleanly; headers/footers gone; neighbours cropped away. Verify green.
- **Born-digital (~1990–1993)**: near-perfect (~100 % preservation).
- **SC resolutions from the "Resolutions and Decisions" volumes**: the resolution
  **body** (preamble + operatives) extracts and labels correctly. These pages
  interleave **Decision blocks and presidential-statement text** between
  resolutions and often adopt neighbours at the **same meeting**; the crop now ends
  at a narrative `Decisions` block so that tail no longer bleeds into the target.
- **Bilingual old volumes**: French lines are excluded, so a single-page
  English/French supplement crops cleanly. A **two-page** bilingual scan where the
  English body continues across a page behind the facing French column (e.g.
  `A/RES/221(III)`) can still be **under-cropped** — what is extracted is faithful,
  but the later operatives may be missed. This class is tiny.
- **`poor` scans**: extracted and flagged; treat as best-effort.

### Runbook

```bash
# 1. Fetch (SEPARATE backfill — run AFTER the Word backfill drains). Sample first:
uv run python python/fulltext_fetch_pdf.py --symbols-file sample.txt --rate 4

# Deferred BULK backfill of the whole pre-1994 PDF corpus (~17.7k docs). Expect
# several hours; be gentle so it never contends with anything else on ODS:
uv run python python/fulltext_fetch_pdf.py --catalog --rate 1.8   # ~1.5-2 s/req
uv run python python/fulltext_fetch_pdf.py --recheck-unavailable  # recover soft-blocks

# 2+3. Extract + parse (idempotent; --force re-extracts 'extracted'/'no_text_layer'):
uv run python python/fulltext_pipeline.py --pdf
uv run python python/fulltext_pipeline.py --pdf --limit 20        # smoke test

# 4. Acceptance gate (independent pdftotext ground truth, restricted to the crop):
uv run python python/fulltext_verify_pdf.py
```

### The acceptance gate, honestly (`fulltext_verify_pdf.py`)

Ground truth is **`pdftotext`** (poppler, default reading-order mode — independent
of the pymupdf extractor), restricted to the **cropped target region** by fuzzy-
anchoring the parse's first/last content on the pdftotext token stream. Words
outside the region — neighbour resolutions, running headers, the French column —
are an **expected, counted crop-loss category**, not a failure. A `text` doc passes
on a small absolute loss (a few OCR letter-substitutions) **or** a high in-region
preservation fraction (default ≥ 95 %); `poor` docs use a looser bar.

On the 64-doc sample: **34 / 34 extracted docs pass; aggregate in-region
preservation 99.2 %.** The residual is **inherent OCR letter-substitution**
(`securitv`, `takmg`, `tbe`, `wornen` — the word is present but mis-recognised) —
**not extraction drops.** The gate anchors its region by walking the parse's
elements from the last one backward until one anchors in the pdftotext stream (with
a token-count bound), so a lead line the OCR mangled can no longer default the
region to end-of-document and swallow the next resolution. What the gate **cannot**
prove: that a crop boundary is semantically perfect (an under-cropped bilingual
two-pager passes on the small region it does anchor), or anything about a
`none`-class pure scan (excluded before it reaches the gate).

## Volume-split pipeline (GA/ECOSOC decisions, early HRC)

Two families are structurally absent from ODS at the individual-symbol level and
so were excluded from the 8-family catalog (see *Corpus definition*): GA/ECOSOC
**decisions** (`A/DEC/*`, `E/DEC/*`) and the **early Human Rights Council** texts
of sessions 2-11 (`A/HRC/RES|PRST|DEC/2..11/*`). Their full text is only published
inside compilation **volumes** / session **reports** that ODS and DL *do* host.
The volume-split pipeline recovers them: fetch the parent, extract it as an
ordinary doc, then split the per-child paragraphs back out.

### Sources (the parent documents)

| Children | Parent | Symbol | Format |
|----------|--------|--------|--------|
| GA decisions (`A/DEC/<n>/<m>`) | GAOR Suppl. 49, Volume II | `A/<n>/49 (Vol. II)` | PDF |
| ECOSOC res+dec (`E/RES`/`E/DEC/<y>/<m>`) | ECOSOC Suppl. 1 | `E/<y>/99` | PDF |
| early HRC res/dec/PRST | per-session HRC report | see the HRC map below | Word (`.doc`) |

The **HRC report map** (session → sessional report symbol that carries that
session's adopted texts):

```
2:A/HRC/2/9  3:A/HRC/3/7  4:A/HRC/4/123  5:A/HRC/5/21  6:A/HRC/6/22
7:A/HRC/7/78  8:A/HRC/8/52  9:A/HRC/9/28  10:A/HRC/10/29  11:A/HRC/11/37
special sessions S-2 .. S-11:  A/HRC/S-<n>/2
```

### `source_symbol` semantics (migration 005)

`source_symbol` (added to `document_files` **and** `document_paragraphs_raw`)
records, on a split **child** row, the parent volume/report symbol it was carved
from. A child ledger row never gets an `archive_path` of its own (the parent
volume owns the file) and runs the normal status lifecycle from `extracted` on.
The parent volume itself is an ordinary ledger doc (`source_symbol` NULL,
`archive_path` set) that is retired to a terminal status **`split`** after a
successful split, so the resolution parser and the docx/pdf gates never treat the
227-page compilation as a single resolution. Re-split key: the split
`DELETE ... WHERE source_symbol = <volume>` then re-inserts, so it is idempotent.
`source_symbol` is NULL for every ordinary (non-split) row — the 8-family catalog
is unchanged. The pre-existing 3,233 `A/DEC`/`E/DEC` `unavailable` ledger rows are
UPDATEd in place to `extracted` + `source_symbol` on a successful split (their
prior fetch history is not needed).

### Era scope — born-digital cutoff (STEP-0 probe sweep)

Only born-digital / text-layer volumes are in scope; the pre-era volumes are pure
**image scans** (`fulltext_extract_pdf` triage class `none`) and are DEFERRED for a
future OCR pass, exactly like the pre-1994 scans. `--probe` triages the archived
volume PDFs; the measured cutoff (chars/page jumps from ~1 to ~2600-4500 at the
boundary — an unambiguous scan→born-digital transition):

| Family | In scope (`text`) | Scans (`none`, deferred) |
|--------|-------------------|--------------------------|
| GA Vol II | session **≥ 57** (`A/57/49(Vol.II)`, 2003) | ≤ 55 (2001 and earlier) |
| ECOSOC Suppl. 1 | year **≥ 2003** (`E/2003/99`) | ≤ 2002 |

This **overrides** the initial assumption (GA ≥ 49 / ECOSOC ≥ 1994 — those turned
out to be scans). In-scope children in the catalog: ~**2,461** `A/DEC` (sessions
57-80) + ~**1,843** `E/DEC` (2003-2025) + **179** early HRC. (`GA_VOL2_MIN_SESSION`,
`ECOSOC_MIN_YEAR` in `fulltext_split_volumes.py` encode the cutoff.)

### Split predicates

- **GA/ECOSOC (PDF)** — a body-heading line `^<sess>/<num>[<Letter>]. <Titlecase…>`
  (`pdf_heading`), **excluding dot-leader TOC/checklist lines** (`.... 47`). GA
  `sess` is the session (`80/506` → `A/DEC/80/506`; `80/544 A` → `A/DEC/80/544A`);
  ECOSOC `sess` is the year (`2025/201` → candidate `E/DEC/2025/201` *or*
  `E/RES/2025/201`). Each heading appears **twice** in a volume (full body + a
  bare-heading checklist entry); the split **dedupes per child, keeping the
  longest (substantive) slice**.
- **HRC (docx)** — a paragraph styled **Heading 2** whose text matches
  `^(PRST/|DEC/)?(S-<n>|<n>)/<m>.` (`hrc_heading` + `is_heading2`). Annex roman
  subheads are also Heading 2 but don't match the pattern. `PRST/6/1.` →
  `A/HRC/PRST/6/1`; `7/1.` → `A/HRC/RES/7/1` (or `A/HRC/DEC/7/1`); `S-2/1.` →
  `A/HRC/RES/S-2/1`.

**Routing / gap set.** A derived child is **written** only if its symbol is in the
DL catalog and lacks full text (the gap). ECOSOC resolutions (`E/RES/*`) that a
volume interleaves are a **free cross-check** — measured by the gate, never
overwritten (the Word path already parsed them). `RES` vs `DEC` and part-decisions
are disambiguated by catalog membership. Headings whose exact symbol is not in the
catalog (e.g. `80/408`, present only as `80/408 A`/`B`; the `2025/200` election
decisions ECOSOC does not symbol) are **reported as unmatched**, not written.

### The volume gate (`fulltext_verify_volumes.py`)

Independent re-extraction of the archived file (pymupdf plain `get_text` for PDF,
python-docx paragraph text for HRC — a **different** code path than the split's
extractor) builds a ground-truth token bag for the **decisions section** (first
routed heading → end, dropping dot-leader TOC lines and running headers). The
union of the volume's children **+ allowed-drop** (front matter, TOC/checklist,
running headers, catalog-absent decisions, cross-check/existing decisions, HRC
Part-Two proceedings — i.e. every volume row not routed into a child) must account
for it. Because a child slice always runs heading-to-next-heading, a child can
never be **truncated**, so a decisions-section token absent from the bag is a
genuine extraction loss. Plus a **boundary-leak** check (a child body must not
contain the next child's heading) and a **DB round-trip** check (written child
tokens == the split's slice tokens). Nonzero exit on failure; default bar 0.97.
Measured on the born-digital sample: **GA `A/80/49(Vol.II)` 1.000**, **ECOSOC
`E/2025/99` 0.985** (residual = foreign-language NGO names dropped by the PDF
extractor's French-facing-line filter, not a split defect).

### Runbook

All local (SSD archive), `DATABASE_URL` in `.env`. **Apply migration 005 first**
(owner privilege — the `document_files`/`document_paragraphs_raw` tables are owned
by `un80devpgadmin80`, like migrations 002-004):

```bash
psql "$DATABASE_URL" -f sql/migrations/005_source_symbol.sql   # as the table owner

# STEP 0 — probe the born-digital cutoff (reads archived volume PDFs)
uv run python python/fulltext_split_volumes.py --probe

# 1. Fetch the parents. GA/ECOSOC: ODS t=pdf first, DL English URL as fallback.
uv run python python/fulltext_split_volumes.py --fetch                 # all volumes
uv run python python/fulltext_split_volumes.py --fetch --symbols 'A/80/49(VOL.II)'
#    HRC Word reports are fetched by the same command (it shells out to
#    fulltext_fetch.py --symbols-file), then need LibreOffice to convert .doc:
uv run python python/fulltext_convert.py

# 2. Extract the parents with the SAME extractors the catalog uses (the volume
#    symbol falls outside their crop targets, so they keep the whole volume text):
uv run python python/fulltext_extract_pdf.py --symbols <ga/ecosoc vols>   # PDF
uv run python python/fulltext_extract_raw.py --symbols <hrc reports>      # docx

# 3. Split — write child raw rows + exact child/parent manifests; retire the volume.
uv run python python/fulltext_split_volumes.py --split \
  --changed-symbols-out /tmp/volume-children.txt \
  --changed-volumes-out /tmp/changed-volumes.txt
uv run python python/fulltext_split_volumes.py --split --symbols 'A/80/49(VOL.II)' --dry-run

# 4. Parse exactly the changed children. An empty manifest is a safe no-op:
uv run python python/fulltext_parse.py --to-db --symbols-file /tmp/volume-children.txt

# 5. Volume acceptance gate:
uv run python python/fulltext_verify_volumes.py
```

Iterate per the project discipline: after the first volume of a type, eyeball a
few children (`fulltext_review.py --symbols A/DEC/80/506 ...`) before the bulk.
The **nightly** runs the GA/ECOSOC track (`--nightly`, PDF-only, CI-safe) as a
late stage. The sha256-gate (`harvest_state` key `volume_splits`) writes empty
manifests and skips parse/verify until DL harvests a new supplement. When a
volume changes, only its exact child manifest is parsed and only its exact parent
manifest is verified. The HRC track is a one-time local backfill
(LibreOffice-dependent, historical set), not part of the nightly.

## Coverage snapshot (2026-07-22)

Catalog = 41,802 distinct symbols across the 8 families. Regenerate the audit
with the bucketed CASE query in this section's git history (or adapt from
`fulltext_pipeline.py --report`).

| State | Bucket | Count | Path forward |
|---|---|---:|---|
| on site | parsed & structured (sem-v4) | 25,734* | — |
| on site | bracket parts via parent resolution | 2,721 | auto-improves as parents parse |
| deferred | decisions in pre-2003 scanned volumes | ~5,200 | OCR the volumes, reuse split stage |
| deferred | scanned resolutions, no text layer | ~4,900 | OCR (see docs/_research/ocr-experiments.md: Surya on rented GPU, ~3h/$1-4) |
| absent | confirmed nothing on ODS | ~1,500 | ~400 have DL-hosted PDFs (untapped) |

*25,734 = whole corpus incl. volume-split children; 21,906 of the catalog audit
plus 3,590 split children plus non-catalog volume parents and rescues.

Recovery history: Word era 15.1k (Jul 20) -> +PDF era 7.0k (Jul 21) -> +volume
split 3.6k + fallback PDFs (Jul 22). The verification lesson that shaped the
tooling: fidelity gates (token preservation) cannot see structure-flattening or
display-invisibility; the display-coverage gate, structural invariants, TOC
verifier, and importance-weighted manual audits of the most-cited docs exist
because each caught a class the others could not. Keep auditing the head of the
citation distribution by eye after parser changes.

## Files

| File | Role |
|------|------|
| `sql/schema/fulltext_tables.sql` | Re-runnable table definitions |
| `sql/migrations/002_add_fulltexts.sql` | Idempotent delta migration |
| `python/fulltext_common.py` | Shared helpers (archive, sniff, DB, ledger, state) |
| `python/fulltext_fetch.py` | Stage 1 — fetch + archive from ODS |
| `python/fulltext_convert.py` | Stage 2 — doc/wpd → docx via LibreOffice |
| `python/fulltext_extract_raw.py` | Stage 3 — docx → `document_paragraphs_raw` (raw layer) |
| `python/fulltext_parse.py` | Stage 4 — semantic parse → `parsed_dev/*.json` and, with `--to-db`, the semantic tables |
| `python/fulltext_pipeline.py` | Top-up orchestrator: convert → extract_raw → parse `--to-db` |
| `python/fulltext_nightly.py` | **Nightly automation** (CI): fetch-new → recheck-recent → pdf-fallback → convert → extract(+pdf) → parse → acceptance gates; conversion-blocked/gate-failure → non-zero exit → email |
| `python/fulltext_parse_metrics.py` | Accounting/metrics report + cross-check vs legacy `mandates.paragraphs` |
| `python/fulltext_review.py` | Two-column raw\|parsed HTML review harness + `_flags.json` |
| `sql/schema/fulltext_tables.sql` / `sql/migrations/003_add_semantic_paragraphs.sql` | Semantic layer DDL (`document_paragraphs`, `document_parses`) |
| `sql/migrations/004_action_verbs.sql` | Action-verb annotation columns (`action_*`, `assignee_*`) — additive delta on `document_paragraphs` |
| `python/fulltext_verbs.py` | Deterministic action-verb parser (`extract_action`); stdlib-only, `__main__` self-test |
| `python/fulltext_verbs_eval.py` | Eval harness: `extract_action` vs legacy `mandates.paragraph_mandates` (coverage / verb / category / assignee agreement) |
| `python/fulltext_verify_text.py` | **Acceptance gate** — docx→parsed text-preservation check (nonzero exit on genuine loss) |
| `python/fulltext_fetch_pdf.py` | PDF path stage 1 — fetch pre-1994 PDFs from ODS (`t=pdf`); separate backfill |
| `python/fulltext_extract_pdf.py` | PDF path stage 2 — pymupdf PDF → `document_paragraphs_raw` (triage, header-drop, crop, `pdf-v1`) |
| `python/fulltext_verify_pdf.py` | PDF path **acceptance gate** — pdftotext(region)→parsed preservation check |
| `sql/migrations/005_source_symbol.sql` | Idempotent delta — `source_symbol` on `document_files` + `document_paragraphs_raw` (volume-split provenance) |
| `python/fulltext_split_volumes.py` | **Volume-split pipeline** — catalog/HRC map, `--probe` cutoff sweep, `--fetch` (ODS t=pdf → DL fallback), `--split` (write children, `split-v1`, sha256-gate), `--nightly` |
| `python/fulltext_verify_volumes.py` | **Volume acceptance gate** — independent file re-extraction vs children-union coverage + boundary-leak + DB round-trip |

## Semantic layer policies

The semantic parser (`fulltext_parse.py`, `parser_version = "sem-v1"`) classifies
every raw paragraph into one element and enforces a hard accounting invariant:
each raw position is consumed by exactly one element's `positions[]` or by
`dropped[]`. Corpus-wide the parser accounts **100 %** of positions with **0**
accounting failures. The rules below are the deliberate labelling decisions —
they are policy, not incidental behaviour, and reviewers should treat deviations
as bugs.

- **`paragraph_type` is resolution-body machinery only.** `operative` /
  `preambular` are assigned **only** in the resolution `main` section (and inside
  a scoped instrument annex, below). They answer "is this a preambular or
  operative clause of the resolution?" — a question that is only meaningful for
  the resolution body. Everywhere else `paragraph_type` is `null`, even when the
  text is numbered.

- **Non-instrument annexes keep `paragraph_type = null`.** Declarations,
  programmes of action, agendas, standard-minimum-rules, guidelines, plans,
  lists, schedules and tables that are *annexed* to a resolution are backmatter,
  not resolution operatives. Their numbered/lettered items keep their `prefix`
  and `level` and their `A. … / B. …` section headers are captured as `heading`
  elements, so the internal structure is fully recoverable — but their
  "operativeness" is the *instrument's*, not the resolution's mandate, so
  `paragraph_type` stays `null`. Rationale: labelling an annexed programme's 40
  action points as resolution "operatives" would inflate mandate counts with
  content that carries no resolution-level operative force. (Legacy
  `mandates.paragraphs` did the opposite — see the cross-check note below.)

- **Instrument-annex scoping (the one exception).** An annex that is really an
  annexed *governance instrument* — terms of reference, rules of procedure,
  statute, constitution, charter, regulations, mandate-of-the-body — gets
  **scoped** operative labelling: its numbered paragraphs are the instrument's
  own operatives, tracked with a numbering context independent of the parent
  resolution (`section = "annex"`, `paragraph_type = "operative"`, heading tagged
  `subtype = "instrument"`). Trigger: the annex carries its **own** opening
  formula (an annexed resolution/agreement), **or** its title matches an
  instrument keyword (`ANNEX_INSTRUMENT_RE`) and it has ≥2 numbered paragraphs.
  In the 1994+ RES/PRST corpus this fires on exactly one document
  (`E/RES/2020/19`, "Revised terms of reference of the Standing Working Group on
  Ageing"); it is defensive scaffolding for the broader ~20k corpus.

- **Amendment annexes get `subtype = "amendment"` (pure labelling).** An annex
  whose heading or title opens with "Amendment(s) to …" (body lines like "Amend
  paragraph N to read:") is a **diff against** an instrument, not the instrument
  itself, so it is **never** scoped — `paragraph_type` stays `null`. The annex
  heading is tagged `subtype = "amendment"` for downstream filtering only. Fires
  on `A/RES/73/124` and `A/RES/79/144` (amendments to bodies' terms of
  reference).

- **PRST statement bodies are `null`.** Presidential statements (`S/PRST`,
  `A/HRC/PRST`) have no `The <organ>,` opening formula; their quoted body
  ("The Security Council reaffirms …") is a *statement*, not a preambular/
  operative resolution structure, so body paragraphs are `type = "paragraph",
  paragraph_type = null`. (HRC PRSTs that quote a Council resolution verbatim,
  opening with `"The Human Rights Council,`, are the exception: `OPENING_RE`
  tolerates the leading quote and they get normal preambular/operative labelling.)

- **Opening formula is its own element.** `The General Assembly,` /
  `The Security Council,` etc. is emitted as `type = "opening"` with
  `paragraph_type = "preambular", level = 0`. It marks the `front → preamble`
  state transition and anchors the preamble/operative boundary (the first
  operative is the first `N.` clause, or an unnumbered finite operative verb).

- **`inferred_operative` rescue semantics.** When a resolution **drops an
  operative number at source** (missing/misplaced period, e.g. `A/RES/80/167`'s
  "6Also reaffirms …"; or an omitted `5.`/`(e)`–`(h)`), an unlabeled clause is
  relabeled operative **only** when it (a) sits inside a running operative
  sequence with a **confirmed numbering gap ahead** and (b) reads operative
  (finite lead verb, or a `To <verb>` sub-item). Such elements carry
  `inferred_operative = true` (auditable/reversible) and **no invented prefix**.
  Default on (`RESCUE_INFERRED_OPERATIVE`). The glued/period-dropped number case
  (`6Also reaffirms`, `4Calls`) is additionally rescued by a sequence-and-verb-
  gated loose matcher that reconstructs the `N.` prefix.

- **Multi-text segmentation.** A physical ODS file may hold several logical
  resolutions. They are segmented into `text_index` blocks — numbering, section
  and preamble/operative state reset at each boundary — on two confirmed signals
  only: (1) a bare capital-letter heading (`A`, `B`, …) **confirmed** by an
  opening formula within a short look-ahead (consolidated/omnibus resolutions
  such as `A/RES/48/75` A–L, or `A/RES/80/244A-C`); (2) a **repeated** opening
  formula with no preceding letter heading (e.g. an ECOSOC resolution that
  transmits a General Assembly text — `E/RES/2025/16`). Section headings *inside*
  one resolution (`I`, `A. Utilization …` followed by `1. …`, never an opening)
  are **never** mistaken for a new text.

**Cross-check note (legacy `mandates.paragraphs`).** The legacy table labels many
non-resolution paragraphs `preambular` by default: it swept the entire annexed
Aide-Memoire of `S/PRST/2015/23` (255 paragraphs) into `preambular`, and labelled
the whole `Recommends the following: (a)…(i)…` recommendation structure of
`E/RES/2024/14` (108 paragraphs) `preambular` with **zero** operatives. Our
policy leaves annex/PRST-body content `null` and correctly splits the recommend-
ation into operative sub-items, so our totals differ by design; text is preserved
either way. Outside those two documents the overlap agrees to within ±2
paragraphs (an opening-formula/chapeau boundary rounding).

## Semantic layer schema (migration 003)

The frozen semantic layer lives in two tables (`sql/schema/fulltext_tables.sql`,
delta `sql/migrations/003_add_semantic_paragraphs.sql`). Both are **rebuildable
from `document_paragraphs_raw`** — the loader (`fulltext_parse.py --to-db`) is the
only writer, and it delete-then-inserts per `(symbol_normalized, lang)`.

### `digitallibrary.document_paragraphs`

One row per parsed element, in document order. It is the JSON `elements[]` array
flattened into columns, plus loader-computed `position`/`id` and provenance.

- **`position`** — 0-based element index in parsed order (the row's ordinal in
  `elements[]`). This is **not** a raw position; the mapping back to the raw layer
  is `raw_positions`.
- **`id`** — `uuid5(NAMESPACE_URL, '<symbol_normalized>:<lang>:<position>')`,
  computed by the loader. Deterministic and stable across re-parses as long as
  element order is stable; a globally-unique handle for downstream joins (unique
  index `uq_document_paragraphs_id`).
- **`raw_positions`** (`INTEGER[]`, never empty) — the `document_paragraphs_raw.position`
  values this element consumed. This is the **provenance / accounting link**: every
  raw position is consumed by exactly one element's `raw_positions[]` or listed in
  the ledger's `dropped[]`. WP hard-broken clauses merge several raw rows into one
  element, so an array (not a scalar) is required.
- **`type`** — `frontmatter|title|opening|heading|paragraph|footnote|divider|
  vote_record|table|signature`. **`subtype`** — `masthead|subres|instrument|
  amendment|annex|appendix` where applicable, else `NULL`. Since **sem-v3** every
  annex/appendix delimiter is a `heading` carrying a subtype (`annex`/`appendix`,
  or the scoped `instrument`/`amendment`), its label in `prefix` (`Annex II`) and
  its title in `text` (folded in from the following line when the delimiter line
  had none).
- **`paragraph_type`** — `preambular|operative`, **resolution-body only** (main
  section + scoped instrument annex); `NULL` everywhere else even when numbered
  (partial index `idx_document_paragraphs_ptype`). See the labelling policies above.
  sem-v3's heading changes never alter it (a heading-styled line that used to be
  mislabelled operative/preambular simply becomes a `heading`, dropping <0.5% of
  op/pp labels corpus-wide — a correctness fix, not a semantic shift).
- **`section`** (`main|annex|appendix`), **`annex_index`**, **`text_index`**
  (omnibus/multi-text block ordinal), **`level`**/**`heading_level`**,
  **`prefix`** (literal marker as printed), **`lead_verb`**, **`text`** (cleaned).
  Since **sem-v3** every element in `section='annex'` carries the `annex_index` of
  its delimiter (delimiters set it; body elements inherit it), so per-doc
  `annex_index` runs are gapless with no NULLs; and section-heading markers
  (`I.`, `B.`, `Goal 1.`, `Action 13.`) live in `prefix`, not glued into `text`.
- **`inferred_operative`** — `true` for the source-dropped-number rescue.
- **`vote`** / **`vote_summary`** — populated on `vote_record` rows only.
  **`hyperlinks`** / **`note_ids`** — JSONB arrays carried from raw (`[]` when none).
- **`parser_version`**, **`parsed_at`**.

### `digitallibrary.document_parses`

One row per parsed `(symbol_normalized, lang)` — a parse ledger. Holds
`parser_version`, `format`, `element_count`, and the parser JSON root `dropped[]`
and `issues[]` **verbatim** (JSONB), so the accounting invariant is queryable in
SQL without re-reading the JSON files:

```sql
-- must equal the raw row count for every doc (0 violations across the corpus):
SELECT p.symbol_normalized,
       sum(cardinality(dp.raw_positions)) + jsonb_array_length(p.dropped) AS accounted,
       (SELECT count(*) FROM digitallibrary.document_paragraphs_raw r
         WHERE r.symbol_normalized = p.symbol_normalized AND r.lang = p.lang) AS raw_rows
FROM digitallibrary.document_parses p
JOIN digitallibrary.document_paragraphs dp USING (symbol_normalized, lang)
GROUP BY p.symbol_normalized, p.dropped;
```

`document_files.status = 'parsed'` marks a doc as loaded; every `document_parses`
row has a matching `parsed` ledger row (loader sets both in the same transaction).

### Annexes (sem-v3)

List a document's annexes — delimiter, label, title — from the `heading` rows:

```sql
SELECT annex_index, prefix AS label, text AS title, subtype
FROM digitallibrary.document_paragraphs
WHERE symbol_normalized = 'A/RES/69/313' AND lang = 'en'
  AND type = 'heading' AND section = 'annex' AND subtype IS NOT NULL
ORDER BY annex_index;
-- and every body element of annex N is  section='annex' AND annex_index = N.
```

Annex-index integrity (expect **0 rows**: no NULL annex_index inside an annex, and
per-doc annex_index runs are gapless 1..max):

```sql
-- (a) NULLs inside an annex
SELECT symbol_normalized, count(*)
FROM digitallibrary.document_paragraphs
WHERE section = 'annex' AND annex_index IS NULL
GROUP BY symbol_normalized;
-- (b) gaps: max(annex_index) must equal count(distinct annex_index)
SELECT symbol_normalized, max(annex_index), count(DISTINCT annex_index)
FROM digitallibrary.document_paragraphs
WHERE section = 'annex'
GROUP BY symbol_normalized
HAVING max(annex_index) <> count(DISTINCT annex_index);
```

### Idempotency & re-parse

Re-running the loader deletes and re-inserts a document's rows, so it is safe to
re-run at any time and to top-up incrementally. Because the parser reads the
**live** `document_paragraphs_raw`, a fresh load can differ slightly from an older
`parsed_dev/*.json` on disk if the raw layer was re-extracted since that JSON was
written — the DB is authoritative. (At freeze time this affected exactly the 3
`Reissued for technical reasons` docs — `S/PRST/2001/9`, `S/PRST/2014/3`,
`S/RES/1881(2009)` — an upstream extraction artifact, not a parser/loader issue.)

## Action-verb annotation (migration 004)

`parser_version = "sem-v2"` adds a **deterministic action-verb annotation** on top
of the semantic elements. A separable pass (`fulltext_parse.annotate_actions`) runs
the stdlib-only parser in `python/fulltext_verbs.py` (`extract_action`) over the
element sequence and flattens its output onto the `action_*` / `assignee_*` columns
added by `sql/migrations/004_action_verbs.sql`. It **replaces the legacy LLM
extraction** (`mandates.paragraph_mandates`) with a declarative, fully
unit-testable lexicon — no model calls, no I/O, stateless per paragraph.

The pass is **purely additive**: it never alters `text`, `positions`, or the
accounting invariant, so the text-preservation gate (`fulltext_verify_text.py`) and
the 003 accounting still hold unchanged. It runs on `A/RES`, `S/RES`, `E/RES`,
`A/HRC/RES` bodies and any scoped instrument annex.

### What gets annotated

Only rows with `paragraph_type IN ('operative','preambular')` — i.e. **resolution
body clauses**. Every other element (PRST/statement bodies, non-instrument annexes,
headings, frontmatter, votes, and **every** `paragraph_type IS NULL` element) keeps
all `action_*`/`assignee_*` columns `NULL`. Within the body, a clause that carries
no leading action (a noun-phrase budget sub-item, a chapeau-less continuation) also
stays `NULL`. Coverage on the loaded corpus: **operative 98.7 %**, **preambular
98.3 %** (excluding the `type='opening'` formula lines, which are preambular but
verb-less). Zero action columns are ever set on a `paragraph_type IS NULL` row.

### Columns

- **`action_verb`** — verbatim leading surface form as printed (`Requests`,
  `Also decides`, `Strongly condemns`).
- **`action_verb_normalized`** — legacy-compatible lemma (`request`, `decide`,
  `take note`, `call upon`, `express concern`). Partial index
  `idx_document_paragraphs_action_verb` (`WHERE ... IS NOT NULL`).
- **`action_category`** — the 5-category spine (below).
- **`action_force`**, **`action_sentiment`**, **`action_bindingness`**,
  **`action_budget_relevant`** — the orthogonal dimensions (below).
- **`action_modifiers`** (`JSONB`) — `[{kind,text}]` leading adverbs/connectives/
  qualifiers peeled off the head (`{"kind":"repetition","text":"also"}`,
  `{"kind":"intensity","text":"strongly"}`, `{"kind":"qualifier","text":"with
  concern"}`).
- **`assignee`**, **`assignee_head_noun`**, **`assignee_class`** — the addressee of
  a directive clause: the verbatim span between the verb and the ` to `-infinitive,
  its head noun, and its class. Partial index
  `idx_document_paragraphs_assignee_class` (`WHERE ... IS NOT NULL`). Classes:
  `secretary-general | special_procedure | secretariat_entity | member_states |
  un_system | un_body | ngo_other | unclear`.
- **`action_inherited`** — `true` when a sub-item inherits its chapeau's governing
  verb (below).
- **`action_context_marker`** — `'chapter_vii'` for an `Acting under Chapter VII …`
  clause; the clause is annotated but `action_verb_normalized` is `NULL` (it is a
  context marker, not a verb).

The full nested `action` object (including `compound`/`secondary_verbs`,
`infinitive_verb`, `context_dependent`) is also written into each element in the
`parsed_dev/*.json` output under an `"action"` key, kept nested to keep the JSON
tidy; the DB carries the flat subset above.

### Lexicon dimension model

The **5-category spine** (legacy-compatible, ~Searle illocutionary types) is
carried alongside **orthogonal dimensions** so callers are not forced through a
single axis. One verb = one lexicon entry with all dimensions:

| category | illocution | force (0–5) | sentiment | bindingness | budget | addressee | representative verbs |
|----------|-----------|-------------|-----------|-------------|--------|-----------|----------------------|
| `observing` | assertive / representative | 0 | 0 | contextual | no | no | note, take note, recognize, acknowledge, consider, confirm |
| `reinforcing` | anaphora / emphasis operator | 0 | 0 | contextual | no | no | reaffirm, recall, reiterate, emphasize, stress, underline |
| `evaluative` | expressive | 0–1 | +1 / −1 | contextual | no | no | welcome, commend, appreciate (+1); condemn, deplore, regret, be concerned (−1) |
| `deciding` | commissive / declarative | 3–5 | 0 / +1 | **binding** | **yes** | some | decide, approve, adopt, establish, authorize, appropriate (5); endorse, resolve, agree (4); pledge, commit (3) |
| `directive` | directive | 1–5 | 0 | hortatory* | budget on `request`/`direct` | **yes** | demand (5); urge (4); request, call upon, call on (3); recommend, appeal (2); encourage, invite (1) |

\* `directive` is `hortatory` by default; `direct` is `binding`. `force` is the
directive/deciding **spine ordinal** (higher = stronger); `observing`/`reinforcing`/
`evaluative` sit at 0 by design. `sentiment` is carried on evaluative verbs and on
carrier verbs (`express concern` → −1, `expresses appreciation` → `appreciate` +1;
`notes/takes note with appreciation/concern` sets ±1). A handful of genuinely
context-dependent verbs (`recognize`, `note`, `stress`, `emphasize`, `affirm`,
`consider`, …) carry a `context_dependent` flag in the JSON (not a DB column).

### Chapeau inheritance

Enumerated sub-items are governed by their chapeau. The annotation pass resolves
this exactly as `fulltext_verbs_eval.run_parser_over_doc` does (the two are kept in
lockstep — that eval is the single source of truth for the resolution logic):

- the chapeau context is the **most recent operative element ending in `:` at a
  shallower level**; a top-level (`level<=1`) clause NOT ending in `:` **resets** it;
- a sub-item with no finite verb of its own (a `To <verb> …` infinitive, a lowercase
  gerund continuation, a bare noun-phrase point) **inherits** the chapeau's verb and
  assignee, with `action_inherited = true`;
- **`governing_verb_for_children`** overrides the chapeau line's *own* leading verb
  for two declaration idioms: `… We decide to:` (children inherit `decide`) and the
  passive personified `Governments … are encouraged to … :` (children inherit
  `encourage`).

On the corpus this fires on **4,431** inherited sub-item rows (e.g. `A/RES/80/184`
item 2 `Urges Member States … to:` → sub-items (a)–(t) all inherit
`urge`/`directive`/`member_states`).

### PRST exclusion

Presidential statements (`S/PRST`, `A/HRC/PRST`) have **no `paragraph_type`** — their
quoted body is a *statement*, not preambular/operative resolution structure — so they
are **never annotated** by design (an HRC PRST that quotes a Council resolution
verbatim with a `"The Human Rights Council,` opening is the exception and gets normal
labelling, hence normal annotation). A bespoke PRST action path is future work.

### Evaluation vs the legacy LLM

`python/fulltext_verbs_eval.py` scores `extract_action` against the legacy
`mandates.paragraph_mandates` ground truth over the 56 overlap documents, joining by
text similarity (positions differ between corpora). Latest run:

- **coverage** — operative **97.6 %**, preambular **94.9 %** (own extracted verb,
  incl. chapeau-inherited);
- **normalized verb agreement** — **97.5 %** (exact and lemma);
- **category agreement** — **98.3 %**;
- **assignee head-noun agreement** — 82.9 % (directive rows, both sides present).

Top residual disagreements are legacy normalization choices, not errors: `note` vs
`take note` (24, the DGACM-principled split), `express` vs `appreciate` (7, legacy
collapses `expresses appreciation`), `be concerned` vs `express concern` (6, the
carrier/adjectival split) — the parser is internally consistent on each.

### Follow-up ideas

- **Mid-paragraph secondary mandates.** Today only the clause's *leading* action is
  extracted; a clause can carry a second directive later in the sentence
  (`… and requests the Secretary-General to …`). The `compound`/`secondary_verbs`
  fields already capture a leading `V1 and V2`, but not a mid-sentence second mandate.
- **PRST bespoke path.** PRST bodies are unannotated; a statement-specific extractor
  (their `The Council reaffirms … / demands …` sentences carry real illocutionary
  force) would extend coverage to the ~PRST families.

## Downstream: mandates.un.org consumption

`document_paragraphs` / `document_parses` are the digitallibrary-side frozen
output. Wiring them into the **mandates.un.org unified documents view** (joining
the semantic paragraphs to the PPB/mandate objects the product renders) is a
**separate downstream step in the `mandates` repo** — not part of this pipeline.
This repo's responsibility ends at a clean, queryable semantic layer.
