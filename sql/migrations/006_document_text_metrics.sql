-- Deterministic metrics over successful English semantic fulltext parses.
--
-- The normalized token stream is persisted so downstream series analytics can
-- compare exactly the input that was counted, without rebuilding a subtly
-- different text profile.  Rows are versioned by text_profile_version; a new
-- profile can be backfilled alongside the old one and switched atomically.

BEGIN;

CREATE TABLE IF NOT EXISTS digitallibrary.document_text_metrics (
  symbol_normalized    TEXT        NOT NULL,
  lang                 TEXT        NOT NULL DEFAULT 'en',
  text_profile_version TEXT        NOT NULL,
  metric_version       TEXT        NOT NULL,
  parser_version       TEXT        NOT NULL,
  content_sha256       TEXT        NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
  word_count           INTEGER     NOT NULL CHECK (word_count >= 0),
  character_count      INTEGER     NOT NULL CHECK (character_count >= 0),
  element_count        INTEGER     NOT NULL CHECK (element_count >= 0),
  token_text           TEXT        NOT NULL,
  computed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol_normalized, lang, text_profile_version)
);

CREATE INDEX IF NOT EXISTS idx_document_text_metrics_current
  ON digitallibrary.document_text_metrics
    (text_profile_version, lang, symbol_normalized);

COMMIT;
