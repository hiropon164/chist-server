-- chist-server — チャット履歴統合検索DB スキーマ
-- SQLite 3.34+ (trigram トークナイザ必須 / 実機は 3.51.0 で確認済み)
--
-- 対象ソース: CLI 版 4 種のみ（Claude Code / Codex / Cursor CLI / Antigravity CLI）
--             GUI 版（Cursor GUI / Antigravity GUI）は今後検討。
--             ui 列と 'gui' の CHECK は将来の追加に備えて残してある。

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;

-- スキーマ版。chist/db.py が既存 DB との差分を見て移行する。
--   1: 単一マシン
--   2: 複数マシン集約（hosts / sessions.host / sessions.project_key）
PRAGMA user_version = 2;

--------------------------------------------------------------------------------
-- 1. ソース定義
--------------------------------------------------------------------------------

CREATE TABLE sources (
  source    TEXT PRIMARY KEY,                       -- 'claude-code' 等
  ui        TEXT NOT NULL CHECK (ui IN ('cli','gui')),
  label     TEXT NOT NULL,
  root_path TEXT NOT NULL,                          -- 走査の起点

  -- モデル名をどの粒度で記録しているか。NULL が「未使用」なのか
  -- 「記録なし」なのかを集計時に区別するために持つ。
  model_capture TEXT NOT NULL CHECK (model_capture IN ('per-message','per-turn','none'))
);

INSERT INTO sources (source, ui, label, root_path, model_capture) VALUES
  ('claude-code',     'cli', 'Claude Code',     '~/.claude/projects',                     'per-message'),
  ('codex',           'cli', 'Codex',           '~/.codex/sessions',                      'per-turn'),
  ('cursor-cli',      'cli', 'Cursor Agent',    '~/.cursor/projects',                     'none'),
  ('antigravity-cli', 'cli', 'Antigravity CLI', '~/.gemini/antigravity-cli/conversations','per-turn');

-- 今後検討（GUI 版）。有効化するときはこの 2 行を INSERT に戻す。
--   ('cursor-gui',      'gui', 'Cursor',      '~/Library/Application Support/Cursor/User/globalStorage', 'none'),
--   ('antigravity-gui', 'gui', 'Antigravity', '~/.gemini/antigravity',                                   'none');

--------------------------------------------------------------------------------
-- 2. ホスト（複数マシン集約用）
--------------------------------------------------------------------------------

CREATE TABLE hosts (
  host      TEXT PRIMARY KEY,    -- socket.gethostname()
  os        TEXT,                -- 'darwin' | 'win32' | 'linux'
  last_seen TEXT
);

--------------------------------------------------------------------------------
-- 3. セッション（会話単位）
--------------------------------------------------------------------------------

CREATE TABLE sessions (
  id         INTEGER PRIMARY KEY,
  source     TEXT NOT NULL REFERENCES sources(source),
  ext_id     TEXT NOT NULL,      -- sessionId / composerId / trajectory_id
  title      TEXT,               -- ai-title / thread_name / conversations.title / history.display
  cwd        TEXT,               -- そのマシン上での作業ディレクトリ
  git_branch TEXT,

  -- どのマシンで交わされた会話か。ログ側に記録がないためエージェントが付与する。
  host       TEXT REFERENCES hosts(host),

  -- マシンを跨いでプロジェクトを同定する鍵。git remote URL を正規化したもの。
  -- remote がなければ basename(cwd)。`git -C <cwd> remote get-url origin` は
  -- そのリポジトリがあるマシンでしか実行できないため、必ず取り込み側で解決する。
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

-- host / project_key を sessions のみに持たせ、messages には置かない。
-- UNIQUE (source, ext_id, part) により、外付けドライブ経由で同じログが複数マシンから
-- 見えても 1 行に収束する。その 1 行が属するセッションは 1 つなので、
-- host は本質的にセッション単位の属性になる。

--------------------------------------------------------------------------------
-- 4. メッセージ（検索の主対象）
--------------------------------------------------------------------------------

CREATE TABLE messages (
  id         INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,

  -- 非正規化（FTS 結果からの絞り込みを 1 テーブルで完結させるため）
  source     TEXT NOT NULL,
  cwd        TEXT,

  -- 同一メッセージの再取り込みを弾く自然キー。
  --   claude-code     : uuid
  --   codex           : <session_id>:<行番号>
  --   cursor-cli      : <transcript_id>:<行番号>
  --   antigravity-cli : <trajectory_id>:<steps.idx>
  -- いずれも UUID 由来でグローバルに一意。将来マシンを跨いで集約する場合も
  -- この鍵に host を含めなければ重複が自動で吸収される（外付けドライブ対策）。
  ext_id     TEXT NOT NULL,
  part       INTEGER NOT NULL DEFAULT 0,  -- 1レコードが複数ブロックに分かれる場合の連番

  seq        INTEGER,            -- 会話内の表示順
  role       TEXT CHECK (role IN ('user','assistant','system','tool')),
  kind       TEXT NOT NULL DEFAULT 'message'
             CHECK (kind IN ('message','thinking','tool_call','tool_result','artifact')),
  ts         TEXT,               -- ISO8601 (UTC)

  -- 生のモデル名（claude-opus-5 / gpt-5.5 / gemini-3.5-flash-low 等）。
  -- cursor-cli は記録がないため常に NULL（sources.model_capture='none'）。
  model      TEXT,
  -- 'cursor' は Composer（Anysphere 自社モデル）。Cursor は multi-provider で
  -- かつ履歴にモデル名を残さないため、実データ上は 'unknown' になる。
  provider   TEXT CHECK (provider IN ('anthropic','openai','google','cursor','unknown')),

  text       TEXT NOT NULL,

  -- 再取り込み・原文参照用のポインタ（本文の重複保存はしない）
  src_path   TEXT,
  src_pos    INTEGER,            -- JSONL: バイトオフセット / SQLite: steps.idx

  UNIQUE (source, ext_id, part)
);

CREATE INDEX ix_messages_session ON messages(session_id, seq);
CREATE INDEX ix_messages_ts      ON messages(ts DESC);
CREATE INDEX ix_messages_cwd_ts  ON messages(cwd, ts DESC);
CREATE INDEX ix_messages_src_ts  ON messages(source, ts DESC);
CREATE INDEX ix_messages_model   ON messages(model) WHERE model IS NOT NULL;

--------------------------------------------------------------------------------
-- 5. 全文検索インデックス
--------------------------------------------------------------------------------
-- trigram: 日本語を扱うため必須。既定の unicode61 は空白区切りのため
--          「チャット履歴」が 1 トークンになり部分一致しない。
-- content=: 外部コンテンツ方式。本文を二重に持たないためサイズを抑える。

CREATE VIRTUAL TABLE messages_fts USING fts5(
  text,
  content      = 'messages',
  content_rowid= 'id',
  tokenize     = 'trigram'
);

-- tool_call / tool_result は巨大かつノイズになりやすいので索引対象から外す。
-- 検索したくなったら WHEN 句を広げて 'rebuild' すればよい。
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
-- 6. 取り込み状態（差分取り込み用）
--------------------------------------------------------------------------------

CREATE TABLE ingest_state (
  source     TEXT NOT NULL REFERENCES sources(source),
  path       TEXT NOT NULL,

  cursor     INTEGER NOT NULL DEFAULT 0,  -- 次に読む位置
                                          --   JSONL : 読み終えたバイトオフセット
                                          --   AG-CLI: 取り込み済み steps.idx の最大値
  size       INTEGER,                     -- 前回の st_size
  mtime      REAL,                        -- 前回の st_mtime
  head_sha   TEXT,                        -- 先頭4KBのSHA256。書き換え検知 → 全再取り込み

  ok_count   INTEGER NOT NULL DEFAULT 0,
  err_count  INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  PRIMARY KEY (source, path)
);

-- パースに失敗した行の記録（握り潰さない）
CREATE TABLE ingest_errors (
  id       INTEGER PRIMARY KEY,
  source   TEXT NOT NULL,
  path     TEXT NOT NULL,
  pos      INTEGER,
  reason   TEXT,
  seen_at  TEXT
);

--------------------------------------------------------------------------------
-- 7. 検索用ビュー
--------------------------------------------------------------------------------

CREATE VIEW v_messages AS
SELECT m.id, m.source, s.ui, s.label AS source_label,
       s.title AS session_title, m.cwd, m.role, m.kind, m.ts, m.text,
       m.src_path, m.src_pos
FROM messages m
JOIN sessions s ON s.id = m.session_id;

--------------------------------------------------------------------------------
-- 8. クエリ例
--------------------------------------------------------------------------------
--
-- 【重要】trigram は 3 文字未満のクエリを MATCH できない（実測で確認済み）。
--   '履歴'   (2文字) -> 0 件
--   'チャット' (4文字) -> ヒット
-- 2 文字以下の検索語はアプリ側で LIKE にフォールバックさせること。
--
--   -- 全文検索（3文字以上）
--   SELECT m.id, m.source, m.role, m.ts, s.title,
--          snippet(messages_fts, 0, '[', ']', '…', 12) AS hit
--   FROM messages_fts f
--   JOIN messages m ON m.id = f.rowid
--   JOIN sessions s ON s.id = m.session_id
--   WHERE messages_fts MATCH ?
--   ORDER BY bm25(messages_fts)
--   LIMIT 50;
--
--   -- 2文字以下のフォールバック
--   SELECT id, source, role, ts, text FROM messages
--   WHERE text LIKE '%' || ? || '%' ORDER BY ts DESC LIMIT 50;
--
--   -- プロジェクト横断（どのツールで何を話したか）
--   SELECT source, COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last
--   FROM messages WHERE cwd = ? GROUP BY source ORDER BY n DESC;
--
--   -- 期間 + ソース絞り込み付きの全文検索
--   SELECT m.id, m.ts, snippet(messages_fts, 0, '[', ']', '…', 12)
--   FROM messages_fts f JOIN messages m ON m.id = f.rowid
--   WHERE messages_fts MATCH ? AND m.source IN ('claude-code','codex') AND m.ts >= ?
--   ORDER BY bm25(messages_fts) LIMIT 50;
--
--   -- 索引対象 kind を広げた場合の再構築
--   INSERT INTO messages_fts(messages_fts) VALUES ('rebuild');
--   -- ※ rebuild は content テーブルの全行を索引するため、
--   --    kind による選別は効かなくなる点に注意。

--------------------------------------------------------------------------------
-- 9. 送信状態（エージェント側のみ使用）
--------------------------------------------------------------------------------
-- 各マシンのローカル DB が、どの宛先へどこまで送ったかを覚えておく。
-- サーバ側の DB では使わない。

CREATE TABLE push_state (
  endpoint   TEXT PRIMARY KEY,   -- 'http://192.168.1.10:8848'
  last_id    INTEGER NOT NULL DEFAULT 0,  -- 送信済み messages.id の最大値
  sent       INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT
);

--------------------------------------------------------------------------------
-- 10. 今後検討
--------------------------------------------------------------------------------
--
-- GUI 版の追加:
--   sources に 'cursor-gui' / 'antigravity-gui' を INSERT するだけで足りる。
--   ext_id の規約:
--     cursor-gui      : bubbleId:<composerId>:<bubbleId>  ← cursorDiskKV のキーそのまま
--     antigravity-gui : <conv-id>:<brain ファイル名>       ← 本文は暗号化のため要約のみ
