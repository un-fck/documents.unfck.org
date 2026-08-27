#!/usr/bin/env python3
"""Persist deterministic metrics for successful semantic fulltexts.

The ``substantive-v1`` profile uses only
``digitallibrary.document_paragraphs``. It includes titles, opening formulae,
headings, body paragraphs, annexes/appendices and semantic tables. It excludes
frontmatter (including mastheads/page boilerplate), footnotes, dividers, vote
records and signatures. All sections are included, so annex-heavy instruments
are measured rather than silently truncated.

Words are Unicode letter/number runs with an optional internal apostrophe.
Numbers and one-character words are retained. NFKC/casefold normalization makes
the stored ``token_text`` the exact, reproducible input for series similarity.

Examples:
  uv run python python/fulltext_metrics.py --symbols-file tonight.txt
  uv run python python/fulltext_metrics.py --symbols A/RES/70/1 S/RES/2722
  uv run python python/fulltext_metrics.py --all
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from fulltext_common import get_conn

TEXT_PROFILE_VERSION = "substantive-v1"
METRIC_VERSION = "unicode-word-v1"
INCLUDED_TYPES = frozenset({"title", "opening", "heading", "paragraph", "table"})
WORD_RE = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*", re.UNICODE)


@dataclass(frozen=True)
class SemanticElement:
    type: str
    text: str


@dataclass(frozen=True)
class TextMetric:
    token_text: str
    word_count: int
    character_count: int
    element_count: int
    content_sha256: str


def normalize_tokens(text: str) -> list[str]:
    """Return deterministic Unicode-aware tokens, retaining numbers/1-char words."""
    normalized = unicodedata.normalize("NFKC", text).replace("\u2019", "'")
    return [match.group(0).casefold() for match in WORD_RE.finditer(normalized)]


def build_metric(elements: Iterable[SemanticElement]) -> TextMetric | None:
    """Build the current profile; return None when no substantive element exists."""
    included = [element for element in elements if element.type in INCLUDED_TYPES]
    if not included:
        return None
    tokens: list[str] = []
    character_count = 0
    for element in included:
        text = unicodedata.normalize("NFKC", element.text).strip()
        if not text:
            continue
        tokens.extend(normalize_tokens(text))
        character_count += len(text)
    if not tokens:
        return None
    token_text = " ".join(tokens)
    return TextMetric(
        token_text=token_text,
        word_count=len(tokens),
        character_count=character_count,
        element_count=len(included),
        content_sha256=hashlib.sha256(token_text.encode("utf-8")).hexdigest(),
    )


def read_symbols_file(path: Path) -> list[str]:
    if not path.exists():
        raise ValueError(f"symbols manifest does not exist: {path}")
    return sorted({line.strip().upper() for line in path.read_text(encoding="utf-8").splitlines()
                   if line.strip() and not line.lstrip().startswith("#")})


def fetch_documents(conn, symbols: list[str] | None) -> dict[str, tuple[str, list[SemanticElement]]]:
    where = ""
    params: list[object] = []
    if symbols is not None:
        if not symbols:
            return {}
        where = "AND f.symbol_normalized = ANY(%s)"
        params.append(symbols)
    sql = f"""
      SELECT f.symbol_normalized, p.parser_version,
             jsonb_agg(jsonb_build_object('type', e.type, 'text', e.text)
                       ORDER BY e.position) AS elements
      FROM digitallibrary.document_files f
      JOIN digitallibrary.document_parses p
        ON p.symbol_normalized = f.symbol_normalized AND p.lang = f.lang
      JOIN digitallibrary.document_paragraphs e
        ON e.symbol_normalized = f.symbol_normalized AND e.lang = f.lang
      WHERE f.lang = 'en' AND f.status = 'parsed' {where}
      GROUP BY f.symbol_normalized, p.parser_version
      ORDER BY f.symbol_normalized
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return {
            symbol: (parser_version, [SemanticElement(x["type"], x["text"])
                                      for x in elements])
            for symbol, parser_version, elements in cur.fetchall()
        }


UPSERT_SQL = """
INSERT INTO digitallibrary.document_text_metrics
  (symbol_normalized, lang, text_profile_version, metric_version,
   parser_version, content_sha256, word_count, character_count, element_count,
   token_text, computed_at)
VALUES (%s, 'en', %s, %s, %s, %s, %s, %s, %s, %s, now())
ON CONFLICT (symbol_normalized, lang, text_profile_version) DO UPDATE SET
  metric_version = EXCLUDED.metric_version,
  parser_version = EXCLUDED.parser_version,
  content_sha256 = EXCLUDED.content_sha256,
  word_count = EXCLUDED.word_count,
  character_count = EXCLUDED.character_count,
  element_count = EXCLUDED.element_count,
  token_text = EXCLUDED.token_text,
  computed_at = EXCLUDED.computed_at
WHERE digitallibrary.document_text_metrics.metric_version IS DISTINCT FROM EXCLUDED.metric_version
   OR digitallibrary.document_text_metrics.parser_version IS DISTINCT FROM EXCLUDED.parser_version
   OR digitallibrary.document_text_metrics.content_sha256 IS DISTINCT FROM EXCLUDED.content_sha256
"""


def run(symbols: list[str] | None, *, prune_all: bool = False) -> dict[str, int]:
    with get_conn() as conn:
        documents = fetch_documents(conn, symbols)
        inserted_or_changed = 0
        unchanged = 0
        omitted = 0
        with conn.cursor() as cur:
            for symbol, (parser_version, elements) in documents.items():
                metric = build_metric(elements)
                if metric is None:
                    omitted += 1
                    cur.execute(
                        "DELETE FROM digitallibrary.document_text_metrics "
                        "WHERE symbol_normalized=%s AND lang='en' AND text_profile_version=%s",
                        [symbol, TEXT_PROFILE_VERSION],
                    )
                    continue
                cur.execute(UPSERT_SQL, [
                    symbol, TEXT_PROFILE_VERSION, METRIC_VERSION, parser_version,
                    metric.content_sha256, metric.word_count, metric.character_count,
                    metric.element_count, metric.token_text,
                ])
                if cur.rowcount:
                    inserted_or_changed += 1
                else:
                    unchanged += 1

            if symbols is not None:
                missing = sorted(set(symbols) - set(documents))
                if missing:
                    cur.execute(
                        "DELETE FROM digitallibrary.document_text_metrics "
                        "WHERE lang='en' AND text_profile_version=%s "
                        "AND symbol_normalized = ANY(%s)",
                        [TEXT_PROFILE_VERSION, missing],
                    )
            elif prune_all:
                cur.execute(
                    "DELETE FROM digitallibrary.document_text_metrics m "
                    "WHERE m.lang='en' AND m.text_profile_version=%s AND NOT EXISTS ("
                    " SELECT 1 FROM digitallibrary.document_files f "
                    " JOIN digitallibrary.document_parses p "
                    " ON p.symbol_normalized=f.symbol_normalized AND p.lang=f.lang "
                    " WHERE f.symbol_normalized=m.symbol_normalized AND f.lang=m.lang "
                    " AND f.status='parsed')",
                    [TEXT_PROFILE_VERSION],
                )
                missing = []
            else:
                missing = []
        conn.commit()
    return {
        "eligible": len(documents),
        "updated": inserted_or_changed,
        "unchanged": unchanged,
        "omitted": omitted,
        "ineligible_requested": len(missing),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="backfill every successful English parse")
    target.add_argument("--symbols-file", type=Path, help="exact symbol manifest")
    target.add_argument("--symbols", nargs="+", help="explicit normalized symbols")
    parser.add_argument("--summary-json", type=Path, help="write machine-readable run summary")
    args = parser.parse_args()

    symbols = None if args.all else (
        read_symbols_file(args.symbols_file) if args.symbols_file
        else sorted({s.strip().upper() for s in args.symbols})
    )
    summary = run(symbols, prune_all=args.all)
    summary.update({
        "text_profile_version": TEXT_PROFILE_VERSION,
        "metric_version": METRIC_VERSION,
        "scope": "all" if args.all else "explicit",
    })
    rendered = json.dumps(summary, indent=2, sort_keys=True)
    print(rendered)
    if args.summary_json:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
