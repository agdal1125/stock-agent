-- L0 Canonical store (SQLite ~ Databricks Gold stand-in)
-- Design principle: structured facts/events are the source of truth;
-- wiki .md files are derived artifacts rendered from here.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ticker master (EPDIW_STK_IEM stand-in)
CREATE TABLE IF NOT EXISTS ticker_master (
  ticker         TEXT PRIMARY KEY,
  name_ko        TEXT NOT NULL,
  name_en        TEXT,
  aliases_json   TEXT NOT NULL DEFAULT '[]',
  market         TEXT,                     -- KOSPI / KOSDAQ
  sector         TEXT,
  asset_type     TEXT NOT NULL DEFAULT 'stock', -- stock | etf
  is_preferred   INTEGER NOT NULL DEFAULT 0
);

-- raw source registry (NH: landing manifest)
CREATE TABLE IF NOT EXISTS source_registry (
  source_id      TEXT PRIMARY KEY,         -- hash of path
  source_type    TEXT NOT NULL,            -- news | disclosure | research | profile | transcript
  path           TEXT NOT NULL,
  ticker         TEXT,                     -- nullable (some sources are multi-ticker)
  published_at   TEXT,                     -- ISO8601
  ingested_at    TEXT NOT NULL,
  title          TEXT,
  checksum       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_src_ticker ON source_registry(ticker, published_at DESC);

-- canonical event timeline (per-ticker, append-only)
CREATE TABLE IF NOT EXISTS stock_event_timeline (
  event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker         TEXT NOT NULL,
  event_type     TEXT NOT NULL,            -- news | disclosure | price_shock | broker_call | earnings
  occurred_at    TEXT NOT NULL,
  headline       TEXT,
  summary        TEXT,
  source_id      TEXT,
  impact_score   REAL,                     -- 0~1 (proxy for GPT03 market_post_score)
  FOREIGN KEY(ticker) REFERENCES ticker_master(ticker)
);
CREATE INDEX IF NOT EXISTS idx_evt_ticker_time ON stock_event_timeline(ticker, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_evt_source_id ON stock_event_timeline(source_id);

-- extracted claims (factual statements with source pointer + review gate)
CREATE TABLE IF NOT EXISTS stock_claim (
  claim_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker         TEXT NOT NULL,
  section_type   TEXT NOT NULL,            -- profile | business | risk | fundamentals | relation
  claim_text     TEXT NOT NULL,
  source_id      TEXT,
  confidence     REAL NOT NULL DEFAULT 0.5,
  review_state   TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected
  created_at     TEXT NOT NULL,
  FOREIGN KEY(ticker) REFERENCES ticker_master(ticker)
);
CREATE INDEX IF NOT EXISTS idx_claim_ticker ON stock_claim(ticker, section_type, review_state);

-- pre-rendered per-ticker snapshots (short TTL, rebuilt on touch)
CREATE TABLE IF NOT EXISTS stock_snapshot (
  ticker         TEXT NOT NULL,
  intent         TEXT NOT NULL,            -- latest_issue | business_model | fundamentals | risk | relation
  content_md     TEXT NOT NULL,
  rendered_at    TEXT NOT NULL,
  freshness_sla  INTEGER NOT NULL,         -- seconds
  PRIMARY KEY (ticker, intent)
);

-- page_touch_queue (silver-write → compile trigger, Service Bus stand-in)
CREATE TABLE IF NOT EXISTS page_touch_queue (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ticker         TEXT NOT NULL,
  reason         TEXT NOT NULL,
  enqueued_at    TEXT NOT NULL,
  consumed_at    TEXT
);

-- section index (L1; pointer to wiki/.md file + embedding)
-- Wiki 본체는 파일시스템(wiki/tickers/{ticker}/{section}.md)이 source of truth.
-- 이 테이블은 검색 가속(벡터) + 변경 감지(hash)용 인덱스.
CREATE TABLE IF NOT EXISTS section_doc (
  doc_id         TEXT PRIMARY KEY,         -- f"{ticker}:{section_type}"
  ticker         TEXT NOT NULL,
  section_type   TEXT NOT NULL,
  file_path      TEXT NOT NULL,            -- wiki/.md 상대경로
  content_hash   TEXT NOT NULL,            -- sha256[:16] of md body
  embedding      BLOB,                     -- float32 array (hash 바뀌면 NULL로 리셋)
  tokens         INTEGER,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sec_ticker ON section_doc(ticker, section_type);

-- Audit log (NH: nh_ai_prd.logs.LLM_REQUEST_RESPONSE stand-in)
CREATE TABLE IF NOT EXISTS llm_io_log (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  called_at      TEXT NOT NULL,
  prompt_id      TEXT,
  model          TEXT,
  request_json   TEXT,
  response_text  TEXT,
  latency_ms     INTEGER,
  status         TEXT,                     -- ok | blocked | error
  error          TEXT
);

-- Cost ledger (P7) — 호출당 사용량·비용. llm_io_log 와 별도로 두어
-- 기존 DB 에서도 ALTER 없이 바로 사용 가능.
CREATE TABLE IF NOT EXISTS llm_cost_log (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  called_at            TEXT NOT NULL,           -- ISO8601 UTC
  day                  TEXT NOT NULL,           -- YYYY-MM-DD (UTC)
  month                TEXT NOT NULL,           -- YYYY-MM
  prompt_id            TEXT,
  model                TEXT,
  prompt_tokens        INTEGER NOT NULL DEFAULT 0,
  completion_tokens    INTEGER NOT NULL DEFAULT 0,
  cached_prompt_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd             REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cost_day   ON llm_cost_log(day);
CREATE INDEX IF NOT EXISTS idx_cost_month ON llm_cost_log(month);

-- Tier tracking (eager vs lazy compile)
CREATE TABLE IF NOT EXISTS ticker_tier (
  ticker         TEXT PRIMARY KEY,
  tier           TEXT NOT NULL,            -- eager | lazy
  last_query_at  TEXT,
  query_count    INTEGER NOT NULL DEFAULT 0
);

-- Obsidian-style: tags attached to sections
CREATE TABLE IF NOT EXISTS section_tag (
  doc_id    TEXT NOT NULL,
  tag       TEXT NOT NULL,
  source    TEXT NOT NULL DEFAULT 'auto',  -- auto | frontmatter | inline
  PRIMARY KEY(doc_id, tag, source)
);
CREATE INDEX IF NOT EXISTS idx_tag ON section_tag(tag);

-- Obsidian-style: wikilinks between sections
CREATE TABLE IF NOT EXISTS section_wikilink (
  src_doc_id     TEXT NOT NULL,
  target_ticker  TEXT NOT NULL,
  target_section TEXT,                     -- NULL = 종목 페이지 전체
  display_text   TEXT,
  PRIMARY KEY(src_doc_id, target_ticker, target_section)
);
CREATE INDEX IF NOT EXISTS idx_wl_target ON section_wikilink(target_ticker);
CREATE INDEX IF NOT EXISTS idx_wl_src ON section_wikilink(src_doc_id);
