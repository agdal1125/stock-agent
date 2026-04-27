#!/usr/bin/env bash
# 컨테이너 기동 시:
#  1) 호스트 볼륨 data/ 가 비어 있으면 full bootstrap
#  2) 이미지 동봉 wiki 시드(wiki_seed)가 있고 마운트된 wiki/ 가 비어 있으면 복사
#  3) DB 가 이미 있어도 — seed 변경 흡수 (ticker_master upsert + 새 ETF compile + 임베딩)
#  4) 이후 CMD 실행 (기본: uvicorn)
#
# 설계 목적:
#   seed/tickers.csv 또는 seed/wiki_facts.csv 만 바꾸고 컨테이너를 재기동하면
#   bootstrap 까지 다 돌릴 필요 없이 새 종목·curated fact 가 자동으로 wiki 에 반영.
#   재컴파일은 idempotent (content_hash 동일하면 스킵) 하므로 부팅 비용은 작음.
set -euo pipefail

DATA_DIR="${STOCK_AGENT_DATA_DIR:-/app/data}"
DB="${DATA_DIR}/canonical.db"

mkdir -p "${DATA_DIR}" "${DATA_DIR}/raw" "${DATA_DIR}/cache"

# wiki 마운트가 비어 있으면 이미지 동봉 시드 복사
if [ -d "/app/wiki_seed" ] && [ -z "$(ls -A /app/wiki 2>/dev/null || true)" ]; then
  echo "[entrypoint] wiki/ 비어 있음 → 이미지 시드 복사"
  cp -r /app/wiki_seed/. /app/wiki/ || true
fi

if [ ! -f "${DB}" ]; then
  echo "[entrypoint] canonical.db 없음 → full bootstrap"
  python -m stock_agent.scripts.bootstrap
else
  echo "[entrypoint] canonical.db 존재 → seed reconcile (idempotent)"
  # seed 의 ticker_master 변경분을 DB 에 반영하고, 새 종목(예: 새 ETF) 의 wiki 가
  # 비어 있으면 lazy_compile 로 채움. 기존 wiki 는 content_hash 일치하면 노터치.
  python - <<'PY'
from stock_agent.db import init_db
from stock_agent.l0_canonical.ingest import upsert_ticker_master
from stock_agent.l1_index.embedder import embed_pending
from stock_agent.scripts.bootstrap import _compile_missing_wiki

init_db()
n = upsert_ticker_master()
print(f"[reconcile] ticker_master upsert: {n}")
missing = _compile_missing_wiki()
print(f"[reconcile] backfill compiled (no wiki yet): {missing}")
m = embed_pending()
print(f"[reconcile] embeddings: {m}")
PY
fi

exec "$@"
