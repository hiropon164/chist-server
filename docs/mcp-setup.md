# MCP サーバのセットアップ

chist の MCP サーバは **MCP 2026-07-28** に対応している（実測で `protocol_version = 2026-07-28`
をネゴシエート）。読み取り専用・完全ステートレス。

## 前提

MCP 公式 Python SDK は Python 3.10 以上を要求する。macOS のシステム Python は 3.9 なので、
別途 3.12 以上を用意する。

```bash
cd ~/Projects/chist-server
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python mcp      # mcp 2.0.0 で確認
```

取り込み側（`python3 -m chist ingest`）はシステム Python 3.9 のまま依存ゼロで動く。
この venv は MCP サーバ専用。

## 起動

```bash
# stdio（各ツールから子プロセスとして起動される。通常はこちら）
.venv/bin/python -m chist.mcp_server

# streamable HTTP（Docker 化したときはこちら）
.venv/bin/python -m chist.mcp_server --transport streamable-http --host 0.0.0.0 --port 8848
```

DB は `CHIST_DB` か `--db` で指定。既定は `~/.local/share/chist/history.db`。

## 各ツールへの登録

`<REPO>` は `/path/to/chist-server` に読み替える。

### Claude Code

```bash
claude mcp add chist -- <REPO>/.venv/bin/python -m chist.mcp_server
# HTTP で動かす場合
claude mcp add --transport http chist http://127.0.0.1:8848/mcp
```

### Codex

```bash
codex mcp add chist -- <REPO>/.venv/bin/python -m chist.mcp_server
# HTTP で動かす場合（--url は streamable HTTP を指す）
codex mcp add chist --url http://127.0.0.1:8848/mcp
```

### Cursor

`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "chist": {
      "command": "<REPO>/.venv/bin/python",
      "args": ["-m", "chist.mcp_server"]
    }
  }
}
```

HTTP の場合は `{"url": "http://127.0.0.1:8848/mcp"}`。既存の `MS Learn Docs` /
`FigmaRemoteMcp` エントリが同じ形式を使っている。

### Antigravity

`~/.gemini/config/mcp_config.json`（`~/.gemini/antigravity/mcp_config.json` からのシンボリックリンク）。
現状このファイルは 0 バイトで、リモート MCP に対応しているかは未確認。
まず stdio 形式で試すこと。

```json
{
  "mcpServers": {
    "chist": {
      "command": "<REPO>/.venv/bin/python",
      "args": ["-m", "chist.mcp_server"]
    }
  }
}
```

## 提供するツール

| ツール | 用途 | 既定 / 上限 |
|---|---|---|
| `search_history(query, source?, cwd?, role?, since?, until?, limit)` | 全文検索。抜粋を返す | limit 20 / 100 |
| `list_sessions(cwd?, source?, since?, limit)` | セッション一覧 | limit 20 / 100 |
| `get_session(session_id, offset?, limit?, thinking?)` | 会話本文 | limit 50 / 100、本文計 40,000 字 |

いずれも `read_only_hint=True` / `destructive_hint=False` / `idempotent_hint=True` を宣言。
出力は TypedDict による構造化スキーマ付き。

## 2026-07-28 対応で効いている点

| 仕様 | 実装 |
|---|---|
| セッション廃止・ステートレス | 3 ツールとも副作用のない読み取りクエリ。HTTP は `stateless_http=True` で起動 |
| `server/discover`（MUST） | SDK が自動実装。modern peer にのみ公開される（legacy には非公開） |
| `resultType` 必須 | SDK が `complete` を付与 |
| `ttlMs` / `cacheScope` | `tools/list` と `server/discover` に 1 時間・`public` を宣言（ツール一覧は静的） |
| Roots 非推奨 | 移行先の推奨どおり cwd をツール引数で受ける |
| Sampling / Logging 非推奨 | 未使用。ログは stderr |
| HTTP+SSE 非推奨 | streamable HTTP のみ使用。`--transport sse` は用意しない |

## セキュリティ

履歴には認証情報が混入しうる。

- 3 ツールとも **read-only**。書き込み系のツールは持たせていない
- 出力は既定でマスク済み（`sk-` / `ghp_` / `AKIA` / `AIza` / `Bearer` / JWT / PEM 秘密鍵）
- `get_session` は範囲指定必須で、1 応答あたり 40,000 字で打ち切る
- DB ファイルは `600`

**HTTP で公開する場合は追加の対策が必須。** Tailscale / WireGuard 越しに限定し、
bearer token を付ける。ネットワークに出す構成へ移るときは、マスキングを
取り込み時に前倒しすることも検討する。

## 既知の注意点

- **クライアント側の対応バージョンは未確認。** 仕様公開が 2026-07-28、SDK 2.0.0 は
  `LATEST_PROTOCOL_VERSION = 2026-07-28` / `DEFAULT_NEGOTIATED_VERSION = 2025-03-26` で
  後方互換を持つ。手元の Claude Code は `--transport` に `sse` も受け付けるため、
  まだ旧版を話す可能性がある。SDK が両対応するので実害はない見込み。
- **`server/discover` は legacy peer には見えない。** 生の JSON-RPC で `_meta` に
  プロトコルバージョンを載せずに叩くと `-32601 Method not found` が返る。仕様どおりの挙動。
- **自分の履歴を検索すると、その検索自体が履歴に残る。** 取り込みは冪等なので壊れないが、
  ノイズにはなる。
