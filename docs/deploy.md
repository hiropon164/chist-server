# 管理サーバのデプロイ（複数マシン集約）

各マシンでローカル取り込み → 正規化済みデータを管理サーバへ送信 → 各ツールは
サーバの MCP を叩く、という構成。前提は **同一 LAN 内のみ** からの到達で、
既存の **Traefik** 経由で公開する。

```
┌─ Mac / PC（エージェント）──┐         ┌─ Docker サーバ ───────────────────────┐
│ chist ingest → chist push  │  HTTPS  │ Traefik :443（letsencrypt）           │
│   依存ゼロ / Python 3.9    │────────▶│   Host(`chist.example.com`)          │
│                            │         │   ipwhitelist → forwardAuth(/auth)    │
│ Claude Code / Codex /      │   MCP   │              │                        │
│ Cursor / Antigravity       │────────▶│              ▼                        │
└────────────────────────────┘         │ chist-server :8848（ホストポート無し）│
                                        │   POST /ingest   POST /mcp            │
                                        │   GET  /health   /stats   /auth       │
                                        │   SQLite（volume: chist-data）        │
                                        └───────────────────────────────────────┘
```

ホスト側にポートは開けない。Traefik と同じ docker network に参加し、
Traefik がコンテナの 8848 へ直接繋ぐ。

**取り込みは必ずエージェント側で行う。** チャットログにはホスト名も git remote も
記録されておらず、`git -C <cwd> remote get-url origin` を実行できるのはリポジトリが
存在するマシンだけだから。

## サーバ側の手順

Docker サーバにこのリポジトリを置いて、その場でビルドする。イメージレジストリは不要で、
arm64 / amd64 の差もビルド時に吸収される。

```bash
cd /srv/chist-server
cp .env.example .env
vi .env          # CHIST_TOKEN を埋める

docker compose up -d --build
docker compose logs -f chist
```

`.env` の中身:

```
CHIST_TOKEN=<openssl rand -hex 32 の出力>
CHIST_HOST=chist.example.com
CHIST_ALLOW=192.168.1.0/24,203.0.113.10/32
CHIST_BASICAUTH=<htpasswd -nbB の出力>
CHIST_NETWORK=traefik_traefik
CHIST_ENTRYPOINT=websecure
CHIST_CERTRESOLVER=letsencrypt
```

`docker-compose.yml` は環境依存の値をすべて `.env` から読む。別のサーバへ移すときも
compose には触らない。

確認:

```bash
curl -s -H "Authorization: Bearer $CHIST_TOKEN" https://chist.example.com/health
# {"ok":true,"messages":0}
```

`.env` に書いたトークンは各エージェントにも配る。**未設定だとサーバは起動を拒否する。**

### Traefik 側で押さえること

**1. ネットワーク名を合わせる。** `docker network ls` で既存 Traefik のネットワークを確認し
`CHIST_NETWORK` に入れる（compose プロジェクト名が付いて `<project>_traefik` のような名前に
なっていることが多い。たとえば `traefik_traefik`）。compose 側は `external: true` で参加する
だけなので、Traefik の設定ファイルには手を入れない。ラベルだけの構成なので
`docker restart traefik` も不要。

**2. buffering ミドルウェアを当てない。** MCP の streamable HTTP は POST のレスポンスを
ストリームで返す。Traefik は既定でバッファしないのでそのまま通るが、
`buffering` ミドルウェアを付けると応答が返らなくなる。

**3. 名前解決。** `*.example.com` はワイルドカードで `203.0.113.10`（ルータの固定
グローバル IP）に解決され、LAN 内からはヘアピン NAT で戻ってくる。`chist.example.com`
も追加設定なしで引けるため、`/etc/hosts` への追記は不要。

**4. `/mcp` を必ず forwardAuth で保護する（重要）。** `/ingest` と `/stats` はアプリ内で
bearer token を検証しているが、`/mcp` は MCP SDK 側のルートなのでその検証を通らない。
プロキシを素通しにすると**トークンなしで全履歴が読める**（実測で `tools/list` が 200 を返す）。
compose のラベルで `/auth` を forwardAuth 先に指定してある。

```yaml
- traefik.http.routers.chist.middlewares=chist-allow,chist-auth
- traefik.http.middlewares.chist-auth.forwardauth.address=http://chist-server:8848/auth
```

`/auth` は Authorization ヘッダだけを見て 200 / 401 を返す。Traefik のヘルスチェックは
ミドルウェアを通らないので `/health` が弾かれることはない。

**5. Web UI は basicauth で守る。** `/` と `/api/*` はブラウザから使うため bearer token を
付けられない。ルータを分けて basicauth を当てている。ハッシュの生成は次のとおり
（出力を `.env` の `CHIST_BASICAUTH` にそのまま貼る。`.env` 内では `$` のエスケープは不要）。

```bash
docker run --rm httpd:2-alpine htpasswd -nbB <user> <pass>
```

結果としてルータは 3 つになる。優先度は明示してあるので、パスの重なりで取り違えない。

| ルータ | 対象 | 認証 | priority |
|---|---|---|---|
| `chist-health` | `/health` | なし | 300 |
| `chist-agent` | `/ingest` `/mcp` `/stats` | forwardAuth（bearer） | 200 |
| `chist-web` | それ以外（`/` `/api/*`） | basicauth | 100 |

**6. 到達範囲を絞る。** `CHIST_ALLOW` に許可する送信元 CIDR を並べる。ワイルドカード DNS で
グローバル IP に向けている環境では、LAN 内からのアクセスもヘアピン NAT でルータの外側 IP
として届くため、**そのグローバル IP を外すと LAN からも 403 になる**。

```yaml
- traefik.http.middlewares.chist-allow.ipallowlist.sourcerange=${CHIST_ALLOW}
```

Traefik v3 では `ipallowlist` が正式名（v2 の `ipwhitelist` は非推奨）。

**7. TLS。** `websecure`（:443）+ `certresolver=letsencrypt`（Route53 DNS-01）で張る。
`push` の bearer token が平文で流れるため、平文 HTTP のまま運用しない。

### 履歴の削除と容量

取り込みは追記のみなので DB は増え続ける。実測で 1 件あたり 3.8〜5.9KB
（FTS の trigram 索引が本文の約 1.9 倍）。日常利用なら年 150〜300MB 程度で、
容量が逼迫することは当面ないが、誤取り込みや機微な会話を消す手段としてコマンドがある。

```bash
docker exec chist-server python -m chist prune --before 2026-01-01 --dry-run
docker exec chist-server python -m chist prune --before 2026-01-01 --yes --vacuum
```

条件（`--before` / `--after` / `--source` / `--session` / `--match`）を 1 つ以上
指定しないと何もしない。`--vacuum` を付けないとファイルサイズは縮まない。

なお `docker-backup.sh` は volume を丸ごと tar して restic に渡すため、DB が
大きくなると日次バックアップの効率が落ちる。GB 級になったら prune を検討する。

### DB の置き場所

named volume (`chist-data`) を使う。**SQLite を NFS / SMB に置かないこと**（ロックが壊れる）。

```bash
# バックアップ
docker run --rm -v chist-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/chist-$(date +%Y%m%d).tar.gz -C /data .
```

## エージェント側の手順

各マシンで。**MCP SDK は不要**、システムの Python 3.9 以上があれば動く。

```bash
export CHIST_TOKEN=<サーバと同じトークン>

python3 -m chist ingest                    # ローカル取り込み
python3 -m chist push https://chist.example.com     # 差分を送信
```

cron / launchd で定期実行するなら 2 行をそのまま並べればよい。どちらも冪等。

### 送信されるもの

既定では **`message` / `thinking` / `artifact` のみ**。`tool_call` / `tool_result` は
件数で全体の 7 割を占めるうえ FTS 索引の対象外なので送らない。必要なら
`--include-tools` を付ける。

実測（この Mac 1 台分）:

| | 件数 | DB サイズ |
|---|---:|---:|
| ローカル DB（tool 含む全件） | 51,386 | 125MB |
| サーバへ送信した分 | 13,631 | 50MB |

初回 13,631 件の送信が **約 6.6 秒**（26 バッチ）。以降は差分のみなので日次で数百 KB。

### シークレットのマスキング

送信前（エージェント側）でマスクする。ローカル完結なら表示時マスクで足りるが、
複数マシンから到達できるサーバに生の認証情報を集約するリスクの方が大きいため。受信側（`/ingest`）でも保存前に必ずマスクを通すので、
`--raw` を使ってもサーバ DB に生の認証情報は残らない。

## 各ツールから MCP で繋ぐ

```bash
claude mcp add --transport http chist https://chist.example.com/mcp
codex  mcp add chist --url https://chist.example.com/mcp
```

Cursor は `~/.cursor/mcp.json` に `{"chist": {"url": "https://chist.example.com/mcp"}}`。
Antigravity のリモート MCP 対応は未確認（`docs/mcp-setup.md` 参照）。

2026-07-28 でプロトコルレベルのセッションが廃止されたため、サーバは
`stateless_http=True` で動く。将来ロードバランサへ載せてもリクエストの振り分けは自由。

## プロジェクトの横断

`sessions.project_key` でマシンを跨いで同じプロジェクトをまとめる。

```
git remote あり : github.com/you/example-project     ← マシンを跨いで一致する
git remote なし : local:example-app               ← basename での近似
```

この Mac の実測では 33 個の cwd のうち git remote が取れたのは 2 個だけだった。
残りは `local:<basename>` になるため、**別マシンで同名ディレクトリがあると誤って
同一視される**点に注意。厳密に分けたいプロジェクトは git remote を設定しておく。

```bash
curl -s -H "Authorization: Bearer $CHIST_TOKEN" https://chist.example.com/stats
```

## 認証と公開範囲

- `/ingest` `/stats` `/auth` は bearer token 必須。`/health` のみ認証なし（ヘルスチェック用）
- **`/mcp` と `/` `/api/*` はアプリ内では認証されない。** 前者は MCP SDK 側のルートだから、
  後者はブラウザが bearer token を送れないから。それぞれ Traefik の forwardAuth と
  basicauth で保護している。プロキシを外して直接公開してはいけない
- `CHIST_TOKEN` 未設定なら**起動を拒否する**（exit 2）。無認証で立ち上がる経路はない
- ホスト側にポートを開けていないので、到達経路は Traefik だけ。さらに ipwhitelist で
  `192.168.1.0/24,203.0.113.10/32` に限定
- コンテナは非 root（uid 10001）で動く
- 受信側でも `mask()` を通してから保存する。送信側が `push --raw` を使っても
  生のシークレットはサーバ DB に残らない
- **インターネットへ出す場合は追加対策が必要。** レート制限ミドルウェア（`ratelimit`）を
  入れること。履歴は最も機微なデータの部類なので、公開面に置くなら token だけでは足りない

## 動作確認済みの挙動

Docker なしでサーバとエージェントを直結して検証した結果:

| 確認項目 | 結果 |
|---|---|
| 認証なしの `/ingest` | 401 |
| 初回 push | 13,631 件受理 / 6.6 秒 |
| 再 push（差分なし） | 0 件送信 |
| `--reset` で全件再送 | 12,631 件送信 / **受理 0 件**（`UNIQUE (source, ext_id, part)` が重複を吸収） |
| サーバ DB への MCP 接続 | `protocol_version = 2026-07-28` / 検索成功 |
| `project_key` 集計 | `github.com/you/example-project` 374 件 など |

なお `list_sessions` が返す `messages` 件数は、ローカルの `chist sessions` より小さくなる
（tool 系を送っていないため）。仕様どおり。
