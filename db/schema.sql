-- chist — schema for the unified chat-history search database
-- SQLite 3.34+ (the trigram tokenizer is required; verified on 3.51.0)
--
-- Sources: the four CLI tools only (Claude Code / Codex / Cursor CLI / Antigravity CLI).
--          The GUI variants (Cursor GUI / Antigravity GUI) are under consideration;
--          the ui column and its 'gui' CHECK are kept for that future addition.

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

-- Schema version. chist/db.py migrates an existing database up to this.
--   1: single machine
--   2: multi-machine aggregation (hosts / sessions.host / sessions.project_key)
PRAGMA user_version = 2;

--------------------------------------------------------------------------------
-- 1. Source definitions
--------------------------------------------------------------------------------

CREATE TABLE sources (
  source    TEXT PRIMARY KEY,                       -- e.g. 'claude-code'
  ui        TEXT NOT NULL CHECK (ui IN ('cli','gui')),
  label     TEXT NOT NULL,
  root_path TEXT NOT NULL,                          -- where scanning starts

  -- At what granularity the source records a model name. Lets aggregation tell
  -- "not used" apart from "not recorded" when the value is NULL.
  model_capture TEXT NOT NULL CHECK (model_capture IN ('per-message','per-turn','none'))
);

INSERT INTO sources (source, ui, label, root_path, model_capture) VALUES
  ('claude-code',     'cli', 'Claude Code',     '~/.claude/projects',                     'per-message'),
  ('codex',           'cli', 'Codex',           '~/.codex/sessions',                      'per-turn'),
  ('cursor-cli',      'cli', 'Cursor Agent',    '~/.cursor/projects',                     'none'),
  ('antigravity-cli', 'cli', 'Antigravity CLI', '~/.gemini/antigravity-cli/conversations','per-turn');

-- Under consideration (GUI variants). Move these two rows back into the INSERT to enable.
--   ('cursor-gui',      'gui', 'Cursor',      '~/Library/Application Support/Cursor/User/globalStorage', 'none'),
--   ('antigravity-gui', 'gui', 'Antigravity', '~/.gemini/antigravity',                                   'none');

--------------------------------------------------------------------------------
-- 2. Hosts (for multi-machine aggregation)
--------------------------------------------------------------------------------

CREATE TABLE hosts (
  host      TEXT PRIMARY KEY,    -- socket.gethostname()
  os        TEXT,                -- 'darwin' | 'win32' | 'linux'
  last_seen TEXT
);

--------------------------------------------------------------------------------
-- 3. Sessions (one conversation each)
--------------------------------------------------------------------------------

CREATE TABLE sessions (
  id         INTEGER PRIMARY KEY,
  source     TEXT NOT NULL REFERENCES sources(source),
  ext_id     TEXT NOT NULL,      -- sessionId / composerId / trajectory_id
  title      TEXT,               -- ai-title / thread_name / conversations.title / history.display
  cwd        TEXT,               -- working directory on that machine
  git_branch TEXT,

  -- Which machine the conversation happened on. The logs do not record it, so the
  -- agent supplies it.
  host       TEXT REFERENCES hosts(host),

  -- Key that identifies a project across machines: the git remote URL, normalised.
  -- Falls back to basename(cwd) when there is no remote. `git -C <cwd> remote get-url
  -- origin` only works on the machine holding the repository, so it must be resolved
  -- on the ingest side.
  project_key TEXT,

  started_at TEXT,               -- ISO8601 (UTC)
  ended_at   TEXT,
  msg_count  INTEGER NOT NULL DEFAULT 0,
  UNIQUE (source, ext_id)
);

CREATE INDEX ix_sessions_cwd     ON sessions(cwd);
CREATE INDEX ix_sessions_started ON sessions(started_at DESC);
CREATE INDEX ix_sessions_title   ON sessions(title);
CREATE INDEX ix_sessions_project ON sessions(project_key);
CREATE INDEX ix_sessions_host    ON sessions(host);

-- host and project_key live on sessions only, never on messages.
-- UNIQUE (source, ext_id, part) collapses the same log seen from several machines
-- (e.g. via an external drive) into one row. That row belongs to exactly one session,
-- so host is inherently a per-session attribute.

--------------------------------------------------------------------------------
-- 4. Messages (the main search target)
--------------------------------------------------------------------------------

CREATE TABLE messages (
  id         INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,

  -- Denormalised so filtering an FTS result never needs a second table
  source     TEXT NOT NULL,
  cwd        TEXT,

  -- Natural key that rejects re-ingesting the same message.
  --   claude-code     : uuid
  --   codex           : <session_id>:<line number>
  --   cursor-cli      : <transcript_id>:<line number>
  --   antigravity-cli : <trajectory_id>:<steps.idx>
  -- All are UUID-derived and globally unique. Keeping host out of this key means
  -- duplicates are absorbed automatically when aggregating across machines.
  ext_id     TEXT NOT NULL,
  part       INTEGER NOT NULL DEFAULT 0,  -- sequence when one record splits into blocks

  seq        INTEGER,            -- display order within the conversation
  role       TEXT CHECK (role IN ('user','assistant','system','tool')),
  kind       TEXT NOT NULL DEFAULT 'message'
             CHECK (kind IN ('message','thinking','tool_call','tool_result','artifact')),
  ts         TEXT,               -- ISO8601 (UTC)

  -- Raw model name (claude-opus-5 / gpt-5.5 / gemini-3.5-flash-low, ...).
  -- Always NULL for cursor-cli, which records none (sources.model_capture='none').
  model      TEXT,
  -- 'cursor' means Composer (Anysphere's own model). Cursor is multi-provider and
  -- records no model name, so real data ends up as 'unknown'.
  provider   TEXT CHECK (provider IN ('anthropic','openai','google','cursor','unknown')),

  text       TEXT NOT NULL,

  -- Pointer back to the source, for re-ingest and source lookup (the body is not duplicated)
  src_path   TEXT,
  src_pos    INTEGER,            -- JSONL: byte offset / SQLite: steps.idx

  UNIQUE (source, ext_id, part)
);

CREATE INDEX ix_messages_session ON messages(session_id, seq);
CREATE INDEX ix_messages_ts      ON messages(ts DESC);
CREATE INDEX ix_messages_cwd_ts  ON messages(cwd, ts DESC);
CREATE INDEX ix_messages_src_ts  ON messages(source, ts DESC);
CREATE INDEX ix_messages_model   ON messages(model) WHERE model IS NOT NULL;

--------------------------------------------------------------------------------
-- 5. Full-text index
--------------------------------------------------------------------------------
-- trigram: required for CJK. The default unicode61 splits on whitespace, so a phrase
--          with no spaces becomes a single token and substrings never match.
-- content=: external-content form, so the body is not stored twice.

CREATE VIRTUAL TABLE messages_fts USING fts5(
  text,
  content      = 'messages',
  content_rowid= 'id',
  tokenize     = 'trigram'
);

-- tool_call / tool_result are large and noisy, so they stay out of the index.
-- To search them, widen the WHEN clauses and 'rebuild'.
CREATE TRIGGER messages_fts_ai AFTER INSERT ON messages
WHEN new.kind IN ('message','thinking','artifact')
BEGIN
  INSERT INTO messages_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TRIGGER messages_fts_ad AFTER DELETE ON messages
WHEN old.kind IN ('message','thinking','artifact')
BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;

CREATE TRIGGER messages_fts_au AFTER UPDATE OF text, kind ON messages
BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO messages_fts(rowid, text)
    SELECT new.id, new.text WHERE new.kind IN ('message','thinking','artifact');
END;

--------------------------------------------------------------------------------
-- 6. Ingest state (for incremental ingest)
--------------------------------------------------------------------------------

CREATE TABLE ingest_state (
  source     TEXT NOT NULL REFERENCES sources(source),
  path       TEXT NOT NULL,

  cursor     INTEGER NOT NULL DEFAULT 0,  -- where to resume
                                          --   JSONL : byte offset already read
                                          --   AG-CLI: highest steps.idx ingested
  size       INTEGER,                     -- st_size last time
  mtime      REAL,                        -- st_mtime last time
  head_sha   TEXT,                        -- SHA256 of the first 4 KB; a change means
                                          -- the file was rewritten -> full re-ingest

  ok_count   INTEGER NOT NULL DEFAULT 0,
  err_count  INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  PRIMARY KEY (source, path)
);

-- Lines that failed to parse (never silently dropped)
CREATE TABLE ingest_errors (
  id       INTEGER PRIMARY KEY,
  source   TEXT NOT NULL,
  path     TEXT NOT NULL,
  pos      INTEGER,
  reason   TEXT,
  seen_at  TEXT
);

--------------------------------------------------------------------------------
-- 7. Search views
--------------------------------------------------------------------------------

CREATE VIEW v_messages AS
SELECT m.id, m.source, s.ui, s.label AS source_label,
       s.title AS session_title, m.cwd, m.role, m.kind, m.ts, m.text,
       m.src_path, m.src_pos
FROM messages m
JOIN sessions s ON s.id = m.session_id;

--------------------------------------------------------------------------------
-- 8. Example queries
--------------------------------------------------------------------------------
--
-- IMPORTANT: trigram cannot MATCH a query shorter than 3 characters (measured).
--   a 2-character term -> 0 rows
--   a 4-character term -> matches
-- The application must fall back to LIKE for anything shorter.
--
--   -- full-text search (3+ characters)
--   SELECT m.id, m.source, m.role, m.ts, s.title,
--          snippet(messages_fts, 0, '[', ']', '…', 12) AS hit
--   FROM messages_fts f
--   JOIN messages m ON m.id = f.rowid
--   JOIN sessions s ON s.id = m.session_id
--   WHERE messages_fts MATCH ?
--   ORDER BY bm25(messages_fts)
--   LIMIT 50;
--
--   -- fallback for shorter queries
--   SELECT id, source, role, ts, text FROM messages
--   WHERE text LIKE '%' || ? || '%' ORDER BY ts DESC LIMIT 50;
--
--   -- across a project (which tool discussed what)
--   SELECT source, COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last
--   FROM messages WHERE cwd = ? GROUP BY source ORDER BY n DESC;
--
--   -- full-text search filtered by time range and source
--   SELECT m.id, m.ts, snippet(messages_fts, 0, '[', ']', '…', 12)
--   FROM messages_fts f JOIN messages m ON m.id = f.rowid
--   WHERE messages_fts MATCH ? AND m.source IN ('claude-code','codex') AND m.ts >= ?
--   ORDER BY bm25(messages_fts) LIMIT 50;
--
--   -- rebuild after widening the indexed kinds
--   INSERT INTO messages_fts(messages_fts) VALUES ('rebuild');
--   -- NOTE: rebuild indexes every row of the content table, so the kind
--   --       filtering no longer applies.

--------------------------------------------------------------------------------
-- 9. Push state (agent side only)
--------------------------------------------------------------------------------
-- Each machine's local database remembers how far it has pushed to each endpoint.
-- Unused in the server-side database.

CREATE TABLE push_state (
  endpoint   TEXT PRIMARY KEY,   -- 'http://192.168.1.10:8848'
  last_id    INTEGER NOT NULL DEFAULT 0,  -- highest messages.id already sent
  sent       INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT
);

--------------------------------------------------------------------------------
-- 10. Under consideration
--------------------------------------------------------------------------------
--
-- Adding the GUI variants:
--   Inserting 'cursor-gui' / 'antigravity-gui' into sources is all that is needed.
--   ext_id conventions:
--     cursor-gui      : bubbleId:<composerId>:<bubbleId>  (the cursorDiskKV key as-is)
--     antigravity-gui : <conv-id>:<brain filename>  (bodies are encrypted; summary only)
