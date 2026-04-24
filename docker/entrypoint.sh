#!/usr/bin/env bash
# 컨테이너 기동 시:
#  1) 호스트 볼륨 data/ 가 비어 있으면 bootstrap (schema init + ingest + 컴파일)
#  2) 이미지 동봉 wiki 시드(wiki_seed)가 있고 마운트된 wiki/ 가 비어 있으면 복사
#  3) 이후 CMD 실행 (기본: uvicorn)
set -euo pipefail

DATA_DIR="${STOCK_AGENT_DATA_DIR:-/app/data}"
DB="${DATA_DIR}/canonical.db"

mkdir -p "${DATA_DIR}" "${DATA_DIR}/raw" "${DATA_DIR}/cache"

# wiki 마운트가 비어 있으면 이미지 동봉 시드 복사 (AGENTS.md·log.md·기존 tickers)
if [ -d "/app/wiki_seed" ] && [ -z "$(ls -A /app/wiki 2>/dev/null || true)" ]; then
  echo "[entrypoint] wiki/ 비어 있음 → 이미지 시드 복사"
  cp -r /app/wiki_seed/. /app/wiki/ || true
fi

if [ ! -f "${DB}" ]; then
  echo "[entrypoint] canonical.db 없음 → bootstrap 실행"
  python -m stock_agent.scripts.bootstrap
else
  echo "[entrypoint] canonical.db 존재 → bootstrap 생략"
fi

exec "$@"
