# ---- Build the web UI -------------------------------------------------------
# The output is a single HTML file. Building here keeps Node off the server.
FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

# ---- The server -------------------------------------------------------------
# Built on the Docker host, so the same steps work whether the base image is
# arm64 or amd64.
FROM python:3.12-slim

# The MCP SDK is the only external dependency. The ingest side (chist ingest / push)
# needs nothing, but bundling it here means the CLI works on the server too.
# Pinned below the next major: MCPServer's API changes across major versions.
RUN pip install --no-cache-dir "mcp>=2.0.0,<3"

# Run as non-root. Create /data and chown it *before* the VOLUME declaration:
# changes made by a RUN after VOLUME are discarded, leaving the directory owned by
# root at runtime and writes failing.
RUN useradd -u 10001 -m chist \
 && mkdir -p /data \
 && chown chist:chist /data

WORKDIR /app
COPY chist/ /app/chist/
COPY db/ /app/db/
COPY --from=web /web/dist/index.html /app/web/index.html

# Keep SQLite on a local-disk volume. Mounting NFS or SMB breaks its locking.
ENV CHIST_DB=/data/history.db
VOLUME ["/data"]
EXPOSE 8848

USER chist

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8848/health',timeout=4).status==200 else 1)"

CMD ["python", "-m", "chist.server", "--host", "0.0.0.0", "--port", "8848"]
