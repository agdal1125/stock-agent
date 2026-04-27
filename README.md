# stock_agent — local PoC

Karpathy-style LLM Wiki for a Korean stock/ETF agent, scaled-down for a laptop.
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
| ADLS landing | `data/raw/**/*.md` + curated facts in `seed/wiki_facts.csv` |
| Azure AI Search (hybrid) | `rank_bm25` + numpy cosine + RRF over `section_doc.embedding` |
| Azure OpenAI + APIM kill-switch | `openai` SDK + `LLM_KILL_SWITCH` env flag |
| `LLM_REQUEST_RESPONSE` audit | `llm_io_log` table |
| Redis per-ticker cache | (간략화: 재컴파일 = refresh) |
| ACA `app-stock-agent-int` | FastAPI on `:8001` |
| 관리자 대시보드 review | `python -m stock_agent.scripts.approve_claims` CLI |
| EPDIW_STK_IEM 종목 마스터 | `seed/tickers.csv` (10 stocks + 5 ETFs) |
| Service Bus (page_touch) | `page_touch_queue` 테이블 |
| Eager/Lazy tiering | `ticker_tier` 테이블 + `EAGER_TOP_N` |

## 레이어 대응

- **L0 (Canonical)** — `stock_event_timeline`, `stock_claim`, `ticker_master`,
  `stock_snapshot`. Wiki는 **이 테이블에서 렌더되는 derived artifact**.
- **L1 (Section Index)** — `section_doc` (BM25 + vector hybrid + RRF).
- **L2 (Query Agent)** — `/ask` 엔드포인트. entity resolve → intent classify →
  L0 snapshot + L1 shortlist → LLM gateway로 답변.
- **Tiering** — eager 상위 N만 즉시 컴파일, 나머지는 첫 질의 시 lazy compile.
- **Gated learning** — claim은 `review_state='approved'` 이거나 `seed/wiki_facts.csv`의 curated fact여야 section_doc에 반영.

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
- `EAGER_TOP_N=5` — 상위 N 종목/ETF만 eager compile

### 2. 데이터 적재 + L1 빌드 (한 방)

```bash
python -m stock_agent.scripts.bootstrap
#   → schema init
#   → ticker_master upsert (15 instruments: 10 stocks + 5 ETFs)
#   → raw .md ingest + curated facts merge
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

### 4-bis. REST API (`/api/v1/...`)

외부 클라이언트·대시보드용 JSON 데이터 surface. Swagger UI 는 `/docs` 에서 확인.

| Method | Path | 설명 |
|---|---|---|
| GET  | `/api/v1/health`                                   | 인덱스 카운트 (ticker, sections, embedded, events) |
| GET  | `/api/v1/tickers?asset_type=&market=&sector=&q=`   | 종목·ETF 마스터 리스트 (필터) |
| GET  | `/api/v1/tickers/{ticker}`                         | 단일 종목 메타 + tier + section_count |
| GET  | `/api/v1/tickers/{ticker}/sections`                | 섹션 인덱스 목록 |
| GET  | `/api/v1/tickers/{ticker}/sections/{section_type}` | 섹션 본문 + tags + wikilinks + frontmatter |
| GET  | `/api/v1/tickers/{ticker}/events?types=&limit=&since=` | 이벤트 타임라인 |
| GET  | `/api/v1/tickers/{ticker}/backlinks`               | 이 종목을 가리키는 다른 섹션 (incoming wikilinks) |
| GET  | `/api/v1/etfs`                                     | ETF 만 필터한 ticker 리스트 |
| GET  | `/api/v1/etfs/{ticker}/constituents`               | ETF 구성종목 (링크된 마스터 등록분 + plain mention) |
| GET  | `/api/v1/tags?limit=`                              | 전체 태그 + count |
| GET  | `/api/v1/tags/{tag}`                               | 태그 매칭 섹션 |
| GET  | `/api/v1/search?q=&top_k=&tickers=&section_types=&expand_tags=` | Hybrid search (JSON) |
| POST | `/api/v1/chat`                                     | 챗 답변 (single/multi-turn JSON) |
| POST | `/api/v1/chat/stream`                              | 챗 답변 (NDJSON 토큰 스트림) |
| POST | `/api/v1/refresh/{ticker}`                         | 단일 종목 lazy_compile 트리거 |
| GET  | `/api/v1/auth/status`                              | admin 활성 여부 + 세션 수 |
| POST | `/api/v1/auth/login`                               | 비번 → 세션 토큰 발급 |
| POST | `/api/v1/auth/logout`                              | 토큰 무효화 |
| GET  | `/api/v1/admin/facts?ticker=&section_type=`        | curated facts 행 리스트 (🔐) |
| POST | `/api/v1/admin/facts`                              | 새 fact 추가 + 자동 recompile (🔐) |
| PUT  | `/api/v1/admin/facts`                              | fact 수정 (자연키로 식별, 🔐) |
| DELETE | `/api/v1/admin/facts`                            | fact 삭제 (🔐) |
| GET  | `/api/v1/admin/claims?state=&ticker=`              | DB stock_claim 리스트 (🔐) |
| POST | `/api/v1/admin/claims/{id}/approve`                | claim 승인 (선택: 본문 수정 동시) (🔐) |
| POST | `/api/v1/admin/claims/{id}/reject`                 | claim 거절 (🔐) |
| PUT  | `/api/v1/admin/claims/{id}`                        | claim 본문/신뢰도 수정 (🔐) |

```bash
curl -s localhost:8001/api/v1/tickers?asset_type=etf | jq
curl -s localhost:8001/api/v1/etfs/091160/constituents | jq '.items[]'
curl -s 'localhost:8001/api/v1/search?q=HBM&top_k=3' | jq

# Chat — single-turn
curl -s -X POST localhost:8001/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"q":"삼성전자 HBM 실적 어때?","top_k_sections":3}' | jq

# Chat — multi-turn (마지막 user 메시지 = latest query, 이전 턴은 LLM 컨텍스트)
curl -s -X POST localhost:8001/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"messages":[
        {"role":"user","content":"삼성전자 어떤 회사야?"},
        {"role":"assistant","content":"메모리·파운드리 IT 기업입니다."},
        {"role":"user","content":"그러면 최근 실적은?"}]}' | jq
# → multi-turn 호출은 답변 캐시를 우회하고, latest 메시지로 종목이 안 잡히면
#   직전 user 턴의 ticker(005930)를 자동 carry-over (matched_via=alias_exact@history)

# Chat — NDJSON 스트림
curl -N -X POST localhost:8001/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"q":"SK하이닉스 HBM 공급"}'
```

### 4-ter. Wiki 편집 UI (`/wiki/admin`)

`WIKI_EDIT_PASSWORD` env 가 설정되면 활성화. 미설정시 admin 라우트 전부 503.

- **A) Curated Facts 편집**: `seed/wiki_facts.csv` 행을 추가/수정/삭제. 각 변경 후 해당 ticker 의 `lazy_compile` 자동 트리거 → wiki/*.md 즉시 갱신.
- **B) Claim Approval**: LLM 추출된 `stock_claim.review_state='pending'` 항목을 본문 수정 후 승인/거절. 승인 시 wiki 에 자동 반영.

```bash
# 1) 비번 설정 후 서버 기동
echo 'WIKI_EDIT_PASSWORD=changeme' >> .env
uvicorn stock_agent.agent_int.main:app --port 8001

# 2) 브라우저로 http://localhost:8001/wiki/admin 접속 → 비번 입력
#    토큰은 localStorage 에 저장 (기본 8h TTL).

# 3) curl 로도 사용 가능 (CI 등):
TOKEN=$(curl -s -X POST localhost:8001/api/v1/auth/login \
  -H "Content-Type: application/json" -d '{"password":"changeme"}' | jq -r .token)

curl -s -X POST localhost:8001/api/v1/admin/facts \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"ticker":"005930","section_type":"finance",
       "claim_text":"새 fact 한 줄.","confidence":0.9,
       "source_label":"Curated","source_url":""}'
```

운영 노트:
- 토큰은 in-memory dict 에 보관 → 서버 재시작 시 모두 만료. multi-process 배포 시 외부 세션 스토어로 교체 필요.
- HTTPS 종단은 상위 프록시(Nginx/CF) 가 책임. 비번은 평문 POST.
- `WIKI_EDIT_PASSWORD` 미설정 → 모든 `/api/v1/admin/*` 가 503 → 실수 노출 차단.

### 5. 회귀 평가

```bash
# 외부 호출 없이 resolver/router 정확도만
python -m stock_agent.eval.run --no-llm

# 전체 (LLM 호출 포함, citation_coverage / keyword_recall 까지)
python -m stock_agent.eval.run --out eval_report.json
```

## Smoke test 결과 (KILL_SWITCH=1, Windows/Python 3.13)

```
ticker_master: 15
events: 33
section_docs: 105 (= 15 instruments × 7 sections), all embedded
curated facts: `seed/wiki_facts.csv` drives concrete finance/ETF figures
                + ETF 구성종목 wikilinks ([[005930|삼성전자]] …)

eval --no-llm:
  ticker_accuracy:  100.0 %
  intent_accuracy:  100.0 %
```

실제 OpenAI API 키로 실행 시 claim 추출 품질과 답변 품질이 크게 개선됩니다
(kill-switch 스텁은 모든 claim을 동일 텍스트로 반환 → dedup에 의해 ticker당 1개만 남음).

## 디자인에서 의도적으로 *하지 않은* 것

- Wiki markdown을 편집 가능한 source of truth로 두지 않음 — L0에서 렌더됨
- 전체 종목/ETF를 전부 eager compile하지 않음 — `EAGER_TOP_N`만 eager
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

```
seed/
├── tickers.csv                 # stock/ETF 마스터 + alias
└── wiki_facts.csv              # 검수된 재무 수치·ETF 보수/순자산 curated facts

wiki/
├── tickers/{code}/             # 개별 주식 섹션
└── etfs/{code}/                # ETF 섹션
```
