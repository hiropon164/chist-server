# ---- Web UI のビルド --------------------------------------------------------
# 成果物は単一 HTML 1 枚。サーバに Node を入れずに済ませるためここでビルドする。
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---- 本体 -------------------------------------------------------------------
# chist 管理サーバ。Docker サーバ上でビルドする前提なので
# ベースイメージが arm64 / amd64 のどちらでも同じ手順で通る。
FROM python:3.12-slim

# MCP SDK のみが外部依存。取り込み側（chist ingest / push）は依存ゼロで動くが、
# 同じイメージに同梱しておくとサーバ上でも CLI として使える。
# メジャー版を跨ぐと MCPServer の API が変わるため上限を切っておく。
RUN pip install --no-cache-dir "mcp>=2.0.0,<3"

# 非 root で動かす。VOLUME 宣言より前に /data を作って所有者を変えること。
# VOLUME 宣言のあとに同じディレクトリを RUN で触っても、その変更は破棄されるため
# 実行時に root 所有のままとなり、書き込みに失敗する。
RUN useradd -u 10001 -m chist \
 && mkdir -p /data \
 && chown chist:chist /data

WORKDIR /app
COPY chist/ /app/chist/
COPY db/ /app/db/
COPY --from=web /web/dist/index.html /app/web/index.html

# SQLite は必ずローカルディスク上の volume に置く。
# NFS / SMB をマウントするとロックが壊れる。
ENV CHIST_DB=/data/history.db
VOLUME ["/data"]
EXPOSE 8848

USER chist

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8848/health',timeout=4).status==200 else 1)"

CMD ["python", "-m", "chist.server", "--host", "0.0.0.0", "--port", "8848"]
