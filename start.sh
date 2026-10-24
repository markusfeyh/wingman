#!/bin/sh
set -e
mkdir -p /data/falkordb /data/cache
[ -f /data/me.yaml ] || cp /app/config/me.yaml /data/me.yaml
redis-server --bind 127.0.0.1 --port 6379 --dir /data/falkordb --appendonly yes --save 60 1 \
  --loadmodule /var/lib/falkordb/bin/falkordb.so MAX_QUEUED_QUERIES 25 TIMEOUT 10000 RESULTSET_SIZE 10000 &
until redis-cli -h 127.0.0.1 ping >/dev/null 2>&1; do sleep 0.2; done
exec uv run --no-sync python -m wingman.cli serve --host 0.0.0.0 --port 8080
