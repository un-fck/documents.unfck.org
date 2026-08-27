-- UN Digital Library: document full-text pipeline schema (Track A).
-- Run: psql "$DATABASE_URL" -f sql/schema/fulltext_tables.sql
-- Prereqs:
--   - Schema "digitallibrary" must exist (see documents_tables.sql)
--   - digitallibrary.documents must exist (join key: symbol_normalized)
--
-- Re-runnable: every object uses IF NOT EXISTS so this file is safe to apply
-- repeatedly. The numbered delta lives in sql/migrations/002_add_fulltexts.sql
-- (keep the two in sync by hand — same convention as documents_tables.sql /
-- migrations/001_add_display_title.sql).
--
-- Two-layer model:
--   1. document_files        — fetch/convert/extract ledger (one row per symbol+lang)
--   2. document_paragraphs_raw — low-interpretation extraction layer
-- A THIRD, semantic layer (normalized operative/preambular paragraphs, mandate
-- objects, etc.) is deliberately NOT created here. It arrives later as migration
-- 003 once the raw layer has been iterated on. Until then, document_paragraphs_raw
-- is the re-parse substrate and the SSD archive is ground truth.

-- ---------------------------------------------------------------------------
-- Ledger: one row per (symbol, language) tracking fetch → convert → extract.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS digitallibrary.document_files (
  symbol_normalized TEXT NOT NULL,               -- joins digitallibrary.documents.symbol_normalized
  lang              TEXT NOT NULL DEFAULT 'en',
  format            TEXT,                         -- 'docx'|'doc'|'wpd'|'pdf'|'html'|'unknown' (sniffed from magic bytes, NOT content-type)
  content_type      TEXT,                         -- as reported by the server (Content-Type header)
  size_bytes        BIGINT,
  sha256            TEXT,
  ods_url           TEXT,                         -- source URL on documents.un.org
  archive_path      TEXT,                         -- relative to archive root, e.g. 'original/A_RES_60_1.doc'
  converted_path    TEXT,                         -- e.g. 'converted/A_RES_60_1.docx' (NULL if native docx or not yet converted)
  converter         TEXT,                         -- e.g. 'libreoffice 25.2'
  status            TEXT NOT NULL,                -- 'fetched'|'unavailable'|'failed'|'converted'|'convert_failed'|'extracted'
  error             TEXT,
  fetched_at        TIMESTAMPTZ,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  source_symbol     TEXT,                         -- volume-split children: the parent volume/report symbol they were carved from (migration 005); NULL for ordinary docs. A child row never gets an archive_path (the parent volume owns the file) and runs the normal status lifecycle from 'extracted' on.
  PRIMARY KEY (symbol_normalized, lang)
);

CREATE INDEX IF NOT EXISTS idx_document_files_status ON digitallibrary.document_files (status);
CREATE INDEX IF NOT EXISTS idx_document_files_format ON digitallibrary.document_files (format);
CREATE INDEX IF NOT EXISTS idx_document_files_source_symbol
  ON digitallibrary.document_files (source_symbol) WHERE source_symbol IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Raw paragraph extraction: preserves document order + low-level formatting
-- with minimal interpretation. Re-derivable from the archive at any time.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS digitallibrary.document_paragraphs_raw (
  symbol_normalized TEXT NOT NULL,
  lang              TEXT NOT NULL DEFAULT 'en',
  position          INTEGER NOT NULL,             -- 0-based document order
  kind              TEXT NOT NULL,                -- 'paragraph'|'table_cell'|'footnote'|'section_break'|'empty'
  text              TEXT NOT NULL,                -- may be '' for markers/empty
  style_id          TEXT,
  style_name        TEXT,
  numbering         JSONB,                        -- {"num_id":int,"ilvl":int,"num_fmt":str,"lvl_text":str} when numbered
  props             JSONB,                        -- {"italic":bool,"bold":bool,"alignment":str,"indent_left":int,"indent_hanging":int,"lead_italic_text":str, ...} only non-defaults
  table_cell        JSONB,                        -- {"table":int,"row":int,"col":int} for table cells
  hyperlinks        JSONB,                        -- [{"text":str,"url":str}] incl. field-code links
  footnote_ref      JSONB,                        -- kind='footnote': {"ref_position":int,"note_id":int}; paragraphs w/ refs: list of note ids
  extractor_version TEXT NOT NULL,
  extracted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  source_symbol     TEXT,                         -- volume-split children: the parent volume/report symbol this child paragraph was carved from (migration 005); NULL for ordinary extractions. Re-split key: DELETE ... WHERE source_symbol = <volume>.
  PRIMARY KEY (symbol_normalized, lang, position)
);

CREATE INDEX IF NOT EXISTS idx_document_paragraphs_raw_symbol
  ON digitallibrary.document_paragraphs_raw (symbol_normalized, lang);
CREATE INDEX IF NOT EXISTS idx_document_paragraphs_raw_source_symbol
  ON digitallibrary.document_paragraphs_raw (source_symbol) WHERE source_symbol IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Semantic layer (migration 003). ONE row per parsed element in document order.
--
-- Produced by python/fulltext_parse.py (parser_version 'sem-v1') from
-- document_paragraphs_raw. The parser classifies every raw paragraph into
-- exactly one semantic element and enforces a hard accounting invariant: every
-- raw position is consumed by one element's raw_positions[] or appears in the
-- per-document parse ledger's dropped[] (see document_parses). Loading is
-- delete-then-insert per (symbol_normalized, lang), so it is idempotent and
-- re-parses cleanly. The SSD archive -> document_paragraphs_raw remains the
-- re-derivable substrate; this table is disposable and can be rebuilt from raw.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS digitallibrary.document_paragraphs (
  symbol_normalized TEXT    NOT NULL,             -- joins document_files / document_paragraphs_raw
  lang              TEXT    NOT NULL DEFAULT 'en',
  position          INTEGER NOT NULL,             -- 0-based element index in parsed order (NOT the raw position)
  id                UUID    NOT NULL,             -- uuid5(NAMESPACE_URL, '<symbol_normalized>:<lang>:<position>'), computed by the loader; stable across re-parses while element order is stable
  type              TEXT    NOT NULL,             -- frontmatter|title|opening|heading|paragraph|footnote|divider|vote_record|table|signature
  subtype           TEXT,                         -- masthead|subres|instrument|amendment (element-type-specific; NULL otherwise)
  section           TEXT    NOT NULL DEFAULT 'main',  -- main|annex|appendix
  annex_index       SMALLINT,                     -- 1-based annex ordinal (only on section='annex' elements)
  text_index        SMALLINT NOT NULL DEFAULT 1,  -- multi-text/omnibus block ordinal within one physical file (1 = single text)
  paragraph_type    TEXT,                         -- preambular|operative — resolution-body machinery ONLY (main section + scoped instrument annex); NULL everywhere else even when numbered
  level             SMALLINT,                     -- clause nesting: opening=0, top-level=1, subparagraph 2/3
  heading_level     SMALLINT,                     -- heading depth (H1..H4 / annex/subres headings)
  prefix            TEXT,                         -- literal marker as printed ('1.', '(a)', '(iv)'); NULL for inferred/unnumbered
  lead_verb         TEXT,                         -- preambular participle ('Recalling also') or operative verb ('Requests')
  text              TEXT    NOT NULL,             -- cleaned element text (tabs/NBSP collapsed); table cells joined with ' | '
  raw_positions     INTEGER[] NOT NULL,           -- provenance: document_paragraphs_raw.position values consumed by this element (>=1, never empty)
  inferred_operative BOOLEAN NOT NULL DEFAULT false,  -- true when a source-dropped operative number was rescued (auditable/reversible; no invented prefix)
  vote              JSONB,                        -- vote_record only: {in_favour:[country],against:[],abstaining:[],...}
  vote_summary      JSONB,                        -- vote_record only: {in_favour:int,against:int,abstaining:int} when a tally line was parsed
  hyperlinks        JSONB,                        -- [{text,url}] carried from raw; [] when none
  note_ids          JSONB,                        -- [int] footnote ids referenced/defined; [] when none
  parser_version    TEXT    NOT NULL,             -- e.g. 'sem-v1'
  parsed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol_normalized, lang, position)
);

CREATE INDEX IF NOT EXISTS idx_document_paragraphs_symbol
  ON digitallibrary.document_paragraphs (symbol_normalized, lang);
CREATE INDEX IF NOT EXISTS idx_document_paragraphs_ptype
  ON digitallibrary.document_paragraphs (paragraph_type) WHERE paragraph_type IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_document_paragraphs_id
  ON digitallibrary.document_paragraphs (id);

-- ---------------------------------------------------------------------------
-- Action-verb annotation (migration 004). Flattens the nested `action` object
-- produced by python/fulltext_verbs.extract_action (the deterministic action-verb
-- parser) onto each element. Written by fulltext_parse.py --to-db (parser_version
-- 'sem-v2'). Populated ONLY on paragraph_type IN ('operative','preambular') rows
-- (resolution-body clauses); NULL everywhere else, and NULL on body clauses that
-- carry no action (noun-phrase budget sub-items, chapeau-less continuations).
-- ---------------------------------------------------------------------------
ALTER TABLE digitallibrary.document_paragraphs
  ADD COLUMN IF NOT EXISTS action_verb            TEXT,     -- verbatim leading surface form ('Requests', 'Also decides')
  ADD COLUMN IF NOT EXISTS action_verb_normalized TEXT,     -- legacy-compatible lemma ('request', 'take note', 'call upon')
  ADD COLUMN IF NOT EXISTS action_category        TEXT,     -- observing|reinforcing|evaluative|deciding|directive
  ADD COLUMN IF NOT EXISTS action_force           SMALLINT, -- 0-5 ordinal on the directive/deciding spine
  ADD COLUMN IF NOT EXISTS action_sentiment       SMALLINT, -- +1 / 0 / -1
  ADD COLUMN IF NOT EXISTS action_bindingness     TEXT,     -- binding|hortatory|contextual
  ADD COLUMN IF NOT EXISTS action_budget_relevant BOOLEAN,  -- clause type carries budgetary implications
  ADD COLUMN IF NOT EXISTS action_modifiers       JSONB,    -- [{kind,text}] leading adverbs/connectives/qualifiers stripped off the head
  ADD COLUMN IF NOT EXISTS assignee               TEXT,     -- verbatim assignee span (directive verbs)
  ADD COLUMN IF NOT EXISTS assignee_head_noun      TEXT,     -- head noun of the assignee span
  ADD COLUMN IF NOT EXISTS assignee_class          TEXT,     -- addressee class (secretary-general|member_states|un_body|...)
  ADD COLUMN IF NOT EXISTS action_inherited        BOOLEAN,  -- true when a sub-item inherits its chapeau's governing verb
  ADD COLUMN IF NOT EXISTS action_context_marker   TEXT;     -- 'chapter_vii' for 'Acting under Chapter VII ...'

CREATE INDEX IF NOT EXISTS idx_document_paragraphs_action_verb
  ON digitallibrary.document_paragraphs (action_verb_normalized)
  WHERE action_verb_normalized IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_document_paragraphs_assignee_class
  ON digitallibrary.document_paragraphs (assignee_class)
  WHERE assignee_class IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Per-document parse ledger: one row per (symbol_normalized, lang) parsed.
-- Keeps the accounting invariant queryable in SQL — dropped[]/issues[] are the
-- parser JSON root arrays verbatim, so
--   (count of raw positions) = (sum of raw_positions lengths in document_paragraphs)
--                              + jsonb_array_length(dropped)
-- can be checked without re-reading the JSON files.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS digitallibrary.document_parses (
  symbol_normalized TEXT    NOT NULL,
  lang              TEXT    NOT NULL DEFAULT 'en',
  parser_version    TEXT    NOT NULL,
  format            TEXT,                         -- docx|doc|wpd (source format the parse ran against)
  element_count     INTEGER NOT NULL,             -- number of document_paragraphs rows for this doc
  dropped           JSONB   NOT NULL DEFAULT '[]'::jsonb,  -- [{position:int,reason:str}] raw positions intentionally dropped (empties, page artifacts, layout cells)
  issues            JSONB   NOT NULL DEFAULT '[]'::jsonb,  -- [{position:int,problem:str,text_head:str}] parser-flagged anomalies incl. accounting failures
  parsed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (symbol_normalized, lang)
);

-- ---------------------------------------------------------------------------
-- Versioned deterministic metrics over the successful semantic parse.
-- Populated by python/fulltext_metrics.py. token_text is the exact normalized
-- word-token stream counted here and consumed by downstream series similarity.
-- ---------------------------------------------------------------------------
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
