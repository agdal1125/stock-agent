# stock_agent — local PoC

Karpathy-style LLM Wiki for a Korean stock agent, scaled-down for a laptop.
Mirrors the production architecture proposed for NH: canonical Delta → section
index → LLM-compiled wiki → query agent, but everything runs locally.

**참고 문서**
- [DIAGRAMS.md](DIAGRAMS.md) — **기획·경영진용 쉬운 설명서** (기술 배경 없이도 이해 가능)
- [ARCHITECTURE.md](ARCHITECTURE.md) — 개발자·아키텍트용 상세 구조도
- [TESTING.md](TESTING.md) — 로컬에서 직접 만져보는 검증 가이드 (PowerShell)

## 프로덕션 ↔ 로컬 치환

| 프로덕션 | 로컬 PoC |
|---|---|
| Databricks Delta Lake | SQLite (`data/canonical.db`) |
| ADLS landing | `data/raw/**/*.md` (50개 시드 파일) |
| Azure AI Search (hybrid) | `rank_bm25` + numpy cosine + RRF over `section_doc.embedding` |
| Azure OpenAI + APIM kill-switch | `openai` SDK + `LLM_KILL_SWITCH` env flag |
| `LLM_REQUEST_RESPONSE` audit | `llm_io_log` table |
| Redis per-ticker cache | (간략화: 재컴파일 = refresh) |
| ACA `app-stock-agent-int` | FastAPI on `:8001` |
| 관리자 대시보드 review | `python -m stock_agent.scripts.approve_claims` CLI |
| EPDIW_STK_IEM 종목 마스터 | `seed/tickers.csv` (10종목) |
| Service Bus (page_touch) | `page_touch_queue` 테이블 |
| Eager/Lazy tiering | `ticker_tier` 테이블 + `EAGER_TOP_N` |

## 레이어 대응

- **L0 (Canonical)** — `stock_event_timeline`, `stock_claim`, `ticker_master`,
  `stock_snapshot`. Wiki는 **이 테이블에서 렌더되는 derived artifact**.
- **L1 (Section Index)** — `section_doc` (BM25 + vector hybrid + RRF).
- **L2 (Query Agent)** — `/ask` 엔드포인트. entity resolve → intent classify →
  L0 snapshot + L1 shortlist → LLM gateway로 답변.
- **Tiering** — eager 상위 N만 즉시 컴파일, 나머지는 첫 질의 시 lazy compile.
- **Gated learning** — claim은 `review_state='approved'` 이어야 section_doc에 반영.

## 실행

### 1. 환경 설정

```bash
python -m venv .venv
source .venv/Scripts/activate            # Windows bash
pip install -e .
cp .env.example .env                     # OPENAI_API_KEY 채우기
```

`.env` 주요 항목:
- `OPENAI_API_KEY` — OpenAI 키
- `OPENAI_MODEL=gpt-5.4-mini` — 모델 ID는 env로만 지정 (하드코딩 금지)
- `LLM_KILL_SWITCH=1` — 스텁 모드 (외부 호출 없음, 감사 로그만 기록)
- `EAGER_TOP_N=5` — 상위 N 종목만 eager compile

### 2. 데이터 적재 + L1 빌드 (한 방)

```bash
python -m stock_agent.scripts.bootstrap
#   → schema init
#   → ticker_master upsert (10종목)
#   → raw .md ingest (50 sources, 40 events)
#   → LLM claim 추출 (KILL_SWITCH=1 이면 스텁)
#   → top-N eager promote + 섹션 렌더 + 임베딩
```

### 3. (선택) 미승인 claim 리뷰

```bash
python -m stock_agent.scripts.approve_claims list
python -m stock_agent.scripts.approve_claims approve <claim_id>
python -m stock_agent.scripts.approve_claims approve-all     # PoC 편의
```

### 4. 서버 기동 + 질의

```bash
uvicorn stock_agent.agent_int.main:app --port 8001

curl -s localhost:8001/health
curl -s -X POST localhost:8001/ask -H "Content-Type: application/json" \
     -d '{"q":"삼성전자 HBM 관련 소식","trace":true}'
```

### 5. 회귀 평가

```bash
# 외부 호출 없이 resolver/router 정확도만
python -m stock_agent.eval.run --no-llm

# 전체 (LLM 호출 포함, citation_coverage / keyword_recall 까지)
python -m stock_agent.eval.run --out eval_report.json
```

## Smoke test 결과 (KILL_SWITCH=1, Windows/Python 3.13)

```
ticker_master: 10
ingest: 50 sources / 40 events
claims: 10 pending (stub)
compile: 5 eager × 6 sections = 30 section_docs, all embedded

eval --no-llm:
  ticker_accuracy:   95.0 %
  intent_accuracy:   90.0 %
```

실제 OpenAI API 키로 실행 시 claim 추출 품질과 답변 품질이 크게 개선됩니다
(kill-switch 스텁은 모든 claim을 동일 텍스트로 반환 → dedup에 의해 ticker당 1개만 남음).

## 디자인에서 의도적으로 *하지 않은* 것

- Wiki markdown을 편집 가능한 source of truth로 두지 않음 — L0에서 렌더됨
- 10종목 전부 eager compile하지 않음 — `EAGER_TOP_N`만 eager
- AOAI 직호출 아님 — `llm_gateway`를 **반드시** 경유 (kill-switch + 감사)
- QA 자동 wiki 편입 없음 — `review_state=approved`만 section_doc 진입
- 모델 ID 하드코딩 없음 — `OPENAI_MODEL` env로만 주입

## 파일 맵

```
src/stock_agent/
├── schema.sql                  # L0 + L1 + audit 테이블 정의
├── config.py                   # .env 로드
├── db.py                       # sqlite 연결 + tx 컨텍스트
├── entity/resolver.py          # 종목 해석 (code / alias / fuzzy)
├── l0_canonical/
│   ├── ingest.py               # raw .md → source_registry + event_timeline
│   └── claim_extract.py        # LLM 기반 claim 추출 (review_state='pending')
├── l1_index/
│   ├── section_builder.py      # L0 → Jinja2 섹션 md (derived)
│   ├── embedder.py             # section_doc 임베딩
│   └── hybrid_search.py        # BM25 + vector + RRF
├── compile/run.py              # tiering + page_touch 소비
├── agent_int/
│   ├── llm_gateway.py          # kill-switch + audit 감사 로깅 (필수 경유)
│   ├── router.py               # intent classifier + section shortlist
│   ├── answer.py               # 최종 답변 합성
│   └── main.py                 # FastAPI app (:8001)
├── scripts/
│   ├── init_db.py
│   ├── bootstrap.py            # end-to-end 초기화
│   └── approve_claims.py       # 관리자 대시보드 CLI
└── eval/
    ├── golden_set.jsonl        # 20문 regression set
    └── run.py                  # ticker/intent/citation/keyword 메트릭
```
