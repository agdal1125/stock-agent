# stock_agent — Architecture (개발자용)

개발자·아키텍트 대상 기술 문서. 비기획자용 설명은 [DIAGRAMS.md](DIAGRAMS.md).

> 이 문서의 그림은 모두 **Mermaid** 문법입니다.
> GitHub / Notion / Confluence / Obsidian / VS Code는 바로 렌더링됩니다.
> 렌더링 환경이 없으면 <https://mermaid.live> 에 붙여넣어 보시면 됩니다.

---

## 1. 전체 파이프라인 (L0/L1/L2)

```mermaid
flowchart LR
  subgraph RAW["① 원본 (.md)<br/>뉴스·공시·프로필"]
    direction TB
    N[뉴스]
    D[공시]
    P[회사 프로필]
  end

  subgraph L0["② L0 — Canonical Store<br/>(진리의 원천)"]
    direction TB
    EVT[(이벤트 타임라인)]
    CLM[(추출된 사실 claim)]
    TKR[(종목 마스터 + alias)]
  end

  subgraph L1["③ L1 — Section Index<br/>(검색용)"]
    direction TB
    SEC[섹션 문서 × 임베딩]
  end

  subgraph L2["④ L2 — Query Agent<br/>(답변)"]
    direction TB
    Q[사용자 질문] --> R[종목 해석]
    R --> I[의도 분류]
    I --> S[섹션 검색]
    S --> A[답변 합성<br/>+ 섹션 인용]
  end

  RAW -->|ingest| L0
  L0 -->|render + embed<br/>승인된 claim만| L1
  L1 -->|top-K| S
  L0 -->|최근 이벤트 스냅샷| A
  TKR --> R
```

## 2. 레이어 원칙

```mermaid
flowchart TB
  L2["<b>L2 · Query Agent</b><br/>entity resolve · intent route<br/>hybrid search · answer compose"]
  L1["<b>L1 · Section Index</b><br/>BM25 + 벡터 cosine + RRF<br/>종목×섹션타입 namespace 필터"]
  L0["<b>L0 · Canonical Store</b><br/>이벤트 타임라인 · 추출 claim · 종목 마스터<br/><i>진리의 원천</i>"]
  RAW["<b>Raw Sources</b><br/>뉴스 · 공시 · 리서치 · 전사 (markdown)"]

  RAW -->|ingest + claim extract| L0
  L0 -->|render + embed| L1
  L1 -->|retrieve top-K| L2
  L0 -->|최근 이벤트 snapshot| L2
```

핵심 원칙 3가지:
1. **L0(구조화 테이블)만이 진리의 원천.** Wiki markdown은 파생 산출물 → 이중 진실 방지
2. **L1은 섹션 단위 인덱싱.** 뉴스 본문·Wiki 통째로 LLM에 넣지 않고 의도에 맞는 K개 섹션만
3. **L0의 claim은 `review_state='approved'` 된 것만 L1 반영** → 규제산업용 게이트

## 3. 질의 1건 처리 순서

```mermaid
sequenceDiagram
  actor U as 사용자
  participant R as Resolver
  participant T as Router
  participant L0 as L0 Canonical
  participant L1 as L1 Index
  participant G as LLM Gateway<br/>(kill-switch + 감사)
  participant LLM as OpenAI

  U->>R: "삼성전자 오늘 왜 올랐어?"
  R->>R: 코드/이름/별칭/오타 매칭
  R-->>T: 005930 (score 95)

  T->>T: 규칙 우선 매칭<br/>("오늘·왜" → latest_issue)
  Note over T: 규칙 실패 시에만<br/>LLM 분류 (비용 절감)
  T-->>L1: intent + 섹션 shortlist

  L1->>L1: BM25 + 벡터 hybrid<br/>(종목 × 섹션 namespace)
  L1-->>G: 섹션 K개
  L0-->>G: 최근 이벤트 스냅샷

  G->>G: kill-switch 체크
  G->>LLM: system+user 프롬프트
  LLM-->>G: 답변 (근거 인용 포함)
  G->>L0: 요청/응답 감사 기록
  G-->>U: 답변 반환
```

## 4. 망분리 배치 (프로덕션)

```mermaid
flowchart LR
  subgraph EXT["외부망 (아웃바운드 허용)"]
    direction TB
    S1[app-stock-source-ext<br/>외부 수집]
    S2[app-stock-harness-ext<br/>claim 추출·컴파일]
    DBX[Databricks<br/>Silver/Gold]
    ADLS[(ADLS<br/>raw sources)]
  end

  subgraph GAP["망 경계"]
    direction TB
    BUNDLE["서명된 bundle<br/>(wiki + 섹션 + 임베딩 + claim diff)"]
  end

  subgraph INT["내부망 (PE only)"]
    direction TB
    A1[app-stock-agent-int<br/>FastAPI /ask]
    A2[app-stock-refresh-int<br/>bundle importer]
    AIS[Azure AI Search]
    REDIS[Redis]
    ADM[관리자 대시보드<br/>claim review]
  end

  S1 --> ADLS --> DBX --> S2 --> BUNDLE
  BUNDLE --> A2 --> AIS
  A2 --> REDIS
  A1 -->|retrieve| AIS
  A1 -->|snapshot| REDIS
  ADM -->|승인 게이트| A2
```

## 5. 프로덕션 ↔ PoC 매핑

```mermaid
flowchart LR
  subgraph P["프로덕션 (NH Azure)"]
    direction TB
    P1[Databricks Delta Lake]
    P2[ADLS landing]
    P3[Azure AI Search]
    P4[Azure OpenAI + APIM kill-switch]
    P5[LLM_REQUEST_RESPONSE 감사]
    P6[Redis per-ticker cache]
    P7[관리자 대시보드]
  end

  subgraph L["로컬 PoC"]
    direction TB
    L1[SQLite canonical.db]
    L2["data/raw/**/*.md"]
    L3[rank_bm25 + numpy cosine + RRF]
    L4[openai SDK + LLM_KILL_SWITCH]
    L5[llm_io_log 테이블]
    L6["재컴파일=refresh"]
    L7[approve_claims.py CLI]
  end

  P1 -.-> L1
  P2 -.-> L2
  P3 -.-> L3
  P4 -.-> L4
  P5 -.-> L5
  P6 -.-> L6
  P7 -.-> L7
```

## 6. 주요 설계 결정

| 설계 결정 | 근거 |
|---|---|
| Wiki를 L0에서 렌더되는 파생 산출물로 둠 | 편집 충돌·lint 회피, Delta/테이블과 이중 진실 방지 |
| Top-N만 eager, 나머지는 lazy | 2,700 종목 전수 유지 비용 차단 |
| LLM 호출은 반드시 Gateway 경유 | kill-switch + 감사 강제 (NH 운영 규율) |
| Claim은 승인 전까지 답변 근거 불가 | 규제산업에서 자동 학습 리스크 차단 |
| Intent 분류는 규칙 → LLM 순 | 자주 쓰이는 질의는 LLM 호출 없이 처리 |
| 종목 해석은 code → alias → fuzzy | 삼성전자/삼성전자우/삼성 그룹 중의성 명시적 해소 |
