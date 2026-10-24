# One container: FalkorDB (from the official image) + the Wingman API on :8080.
FROM falkordb/falkordb:latest
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON=python3 UV_LINK_MODE=copy FASTEMBED_CACHE_PATH=/app/models
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
RUN uv run --no-sync python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5', cache_dir='/app/models')"
COPY wingman ./wingman
COPY web ./web
COPY config ./config
COPY start.sh ./start.sh
ENV DATA_DIR=/data FALKORDB_HOST=127.0.0.1 FALKORDB_PORT=6379 WINGMAN_ME_FILE=/data/me.yaml
EXPOSE 8080
ENTRYPOINT []
CMD ["/app/start.sh"]
