# syntax=docker/dockerfile:1.7
#
# stock-agent — Karpathy-style LLM Wiki (internal zone)
#
# Build:   docker build -t stock-agent:latest .
# Run:     docker run -p 8001:8001 --env-file .env -v $(pwd)/data:/app/data -v $(pwd)/wiki:/app/wiki stock-agent:latest
# Compose: docker compose up -d
#
# Runtime:
#   - 컨테이너 기동 시 data/canonical.db 가 없으면 bootstrap 자동 실행
#   - data/ · wiki/ 는 호스트 볼륨으로 마운트 권장 (상태 유지)

# ---------- Stage 1: 의존성 빌드 ----------
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 시스템 패키지 (gcc 등은 slim 에 없음; rank-bm25/numpy wheel 로 커버)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

# editable 아님 — 빌드된 .whl 로 런타임 이미지에 복사
RUN pip install --upgrade pip build \
 && pip wheel --wheel-dir /wheels . \
 && pip wheel --wheel-dir /wheels fastapi "uvicorn[standard]" openai python-dotenv \
      pydantic jinja2 rank-bm25 numpy python-frontmatter rapidfuzz diskcache \
      markdown slowapi certifi

# ---------- Stage 2: 런타임 ----------
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # 앱 환경 기본값
    AGENT_INT_PORT=8001 \
    STOCK_AGENT_DATA_DIR=/app/data \
    STOCK_AGENT_SEED_DIR=/app/seed \
    OPENAI_MODEL=gpt-5.4-mini \
    OPENAI_EMBED_MODEL=text-embedding-3-small \
    LLM_KILL_SWITCH=1 \
    EAGER_TOP_N=5 \
    RATE_LIMIT_PER_MIN=30 \
    RATE_LIMIT_PER_HOUR=400 \
    RATE_LIMIT_ASK=10/minute \
    DAILY_USD_CAP=0 \
    WIKI_EDIT_SESSION_TTL=28800
# WIKI_EDIT_PASSWORD 가 설정되지 않으면 /api/v1/admin/* 는 503 응답.
# 운영 시 .env 또는 docker run -e WIKI_EDIT_PASSWORD=... 로 주입.

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 + 프로젝트 설치
COPY --from=builder /wheels /wheels
RUN pip install --no-index --find-links=/wheels stock-agent \
 && rm -rf /wheels

# 시드·위키 템플릿·스키마는 이미지에 동봉 (볼륨이 비면 이게 초기값)
COPY seed /app/seed
COPY wiki /app/wiki_seed
COPY src/stock_agent/schema.sql /app/src/stock_agent/schema.sql
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# 볼륨 마운트 지점 미리 생성
RUN mkdir -p /app/data /app/wiki /app/data/cache

# 비루트 유저로 실행
RUN useradd -u 1000 -m -s /bin/bash app \
 && chown -R app:app /app
USER app

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request as r; r.urlopen('http://127.0.0.1:8001/health', timeout=3)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "stock_agent.agent_int.main:app", "--host", "0.0.0.0", "--port", "8001"]
