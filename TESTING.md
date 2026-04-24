# stock_agent — 직접 테스트 가이드 (PowerShell)

이 문서는 이미 부트스트랩까지 완료된 로컬 환경을 **본인이 직접 만져보며**
검증하는 절차입니다. **PowerShell** 기준이며, 모든 명령은 프로젝트 루트에서 실행합니다.

> Git Bash를 쓰시면 본 문서 맨 아래 [부록: Bash 버전](#부록-bash-버전) 참고.

## 0) 세션 초기 세팅 (한 번만)

```powershell
Set-Location C:\Users\NHWM\Desktop\AgentSquad\stock_agent

# PowerShell 스크립트 실행이 막혀 있으면 한 번만:
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned

# venv 활성화
.\.venv\Scripts\Activate.ps1

# 한글 출력 깨짐 방지 (PowerShell 5.1)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'

# 상태 확인
python -c "from stock_agent.config import CFG; print('DB:', CFG.db_path, 'model:', CFG.openai_model, 'kill_switch:', CFG.kill_switch)"
```

기대 출력: `DB: ...canonical.db  model: gpt-5.4-mini  kill_switch: False`

DB를 완전히 처음부터 다시 만들고 싶다면:

```powershell
Remove-Item data\canonical.db -ErrorAction SilentlyContinue
python -m stock_agent.scripts.bootstrap   # 수 분 소요 (LLM 호출 40회 정도)
```

---

## 1) 서버 기동

```powershell
uvicorn stock_agent.agent_int.main:app --port 8001 --reload
```

**다른 PowerShell 창**을 하나 더 열어 아래 질의들을 날립니다.
두 번째 창에서도 먼저 [0)](#0-세션-초기-세팅-한-번만)의 UTF-8 설정을 해두세요.

### 헬스체크
```powershell
Invoke-RestMethod -Uri http://localhost:8001/health | ConvertTo-Json -Depth 5
```

기대: `ticker_master: 10`, `events: 40`, `section_docs: 30`, `embedded_docs: 30`.

---

## 2) 질문 던져보기 — 다양한 의도

### 헬퍼 함수 정의 (한 번만)
```powershell
function Ask($q, [switch]$Trace) {
  $body = @{ q = $q; trace = [bool]$Trace } | ConvertTo-Json -Compress
  Invoke-RestMethod -Uri http://localhost:8001/ask `
                    -Method Post -Body $body -ContentType 'application/json; charset=utf-8'
}
```

### 최근 이슈 (`latest_issue`)
```powershell
Ask "삼성전자 오늘 왜 올랐어?" | ConvertTo-Json -Depth 10
```

### 회사 소개 (`business_model`)
```powershell
Ask "더존비즈온은 뭐하는 회사야?" | ConvertTo-Json -Depth 10
```

### 리스크 (`risk`)
```powershell
Ask "셀트리온 주요 리스크" | ConvertTo-Json -Depth 10
```

### 관련주 (`relation`)
```powershell
Ask "SK하이닉스 주요 경쟁사" | ConvertTo-Json -Depth 10
```

### 내부 trace 포함 (**추천** — 어느 섹션이 retrieval에 잡혔는지 보임)
```powershell
Ask "JYP 월드투어" -Trace | ConvertTo-Json -Depth 20
```

`trace.resolved` (종목 해석), `trace.route` (intent), `trace.section_hits` (top-K 섹션) 을 확인하세요.

### 의도적 엣지 케이스
```powershell
Ask "파운드리 특징주"        # 종목명 없음 → 거부 답변
Ask "005940 AI 시황"          # 6자리 코드만 → 정확 매칭
Ask "삼전 급등 이유"          # 별칭
Ask "포어스 뭐하는데"         # 오타·축약 → fuzzy로 리튬포어스
```

---

## 3) 감사 로그 훑어보기

```powershell
python -c @"
from stock_agent.db import tx
with tx() as c:
    rows = c.execute('SELECT called_at, prompt_id, latency_ms, status FROM llm_io_log ORDER BY id DESC LIMIT 10').fetchall()
    for r in rows: print(dict(r))
"@
```

모든 LLM 호출(prompt_id, 모델, latency, 상태)이 기록됩니다. 운영에서는
`nh_ai_prd.logs.LLM_REQUEST_RESPONSE`의 등가입니다.

---

## 4) Kill-switch 검증 — 망 긴급 차단 시뮬레이션

1. `.env` 파일에서 `LLM_KILL_SWITCH=0` → `LLM_KILL_SWITCH=1`
2. uvicorn은 `--reload` 모드라도 env는 리로드되지 않음 → **서버 재시작** 필요
3. 같은 질문을 다시 던져보기 → 답변이 스텁 문자열로 바뀜
4. `llm_io_log.status`가 `blocked`로 기록됨

```powershell
python -c @"
from stock_agent.db import tx
with tx() as c:
    for r in c.execute(\"SELECT status, COUNT(*) n FROM llm_io_log GROUP BY status\").fetchall():
        print(dict(r))
"@
```

테스트 끝난 뒤 `.env`의 `LLM_KILL_SWITCH`를 다시 `0`으로 돌려놓으세요.

---

## 5) 새 뉴스 한 건 추가해보기 (실시간 반영 시뮬레이션)

### 5-1. 새 md 파일 생성

```powershell
$md = @"
---
ticker: "005930"
source_type: news
published_at: "2026-04-23T10:00:00+09:00"
title: "삼성전자, 신규 파운드리 대형 고객 확보 보도"
source: "synthetic-demo"
---

삼성전자가 북미 AI 스타트업 대상 3nm 파운드리 대형 수주를
확보했다는 업계 전언이 있다. 회사는 공식 확인하지 않았다.
"@

$md | Set-Content -Path data\raw\news\005930-20260423-new.md -Encoding utf8
```

### 5-2. 증분 반영

```powershell
python -c @"
from stock_agent.l0_canonical.ingest import ingest_raw
from stock_agent.l0_canonical.claim_extract import run as extract
from stock_agent.compile.run import run_eager_pipeline
print('ingest :', ingest_raw())
print('claims :', extract())
print('compile:', run_eager_pipeline())
"@
```

### 5-3. 바로 재질의

```powershell
Ask "삼성전자 새 파운드리 수주" -Trace | ConvertTo-Json -Depth 20
```

방금 추가한 이벤트가 `latest_events`에 나타나고, 답변에도 인용돼야 합니다.

---

## 6) Claim 리뷰 흐름 (관리자 대시보드 등가)

```powershell
# pending 목록
python -m stock_agent.scripts.approve_claims list 20

# 특정 claim 반려 (42는 예시; 위 list에서 본 id로 교체)
python -m stock_agent.scripts.approve_claims reject 42

# 반려된 claim은 L1 섹션에 반영되지 않음
python -c "from stock_agent.compile.run import run_eager_pipeline; print(run_eager_pipeline())"
```

---

## 7) 회귀 평가

```powershell
# 빠른 버전 (resolver/router 정확도만, LLM 호출 없음)
python -m stock_agent.eval.run --no-llm

# 전체 (citation_coverage, keyword_recall 포함, 18~20회 LLM 호출)
python -m stock_agent.eval.run --out eval_report.json

# 결과 열람
Get-Content eval_report.json | ConvertFrom-Json | ConvertTo-Json -Depth 10
# 또는: notepad eval_report.json
```

새 시나리오 추가는 `src/stock_agent/eval/golden_set.jsonl`에 한 줄 JSON 추가로 끝.

---

## 8) DB 직접 조회

파일 위치: `data\canonical.db`

### sqlite3 CLI (설치돼 있으면)
```powershell
sqlite3 data\canonical.db "SELECT ticker, section_type, LENGTH(content) bytes FROM section_doc ORDER BY ticker, section_type;"
sqlite3 data\canonical.db ".tables"
```

### 파이썬으로 조회 (sqlite3 CLI 없을 때 대안)
```powershell
python -c @"
from stock_agent.db import tx
with tx() as c:
    for r in c.execute('SELECT ticker, section_type, LENGTH(content) n FROM section_doc ORDER BY ticker, section_type').fetchall():
        print(f\"{r['ticker']}  {r['section_type']:<14}  {r['n']}\")
"@
```

### GUI가 편하면
**DB Browser for SQLite** <https://sqlitebrowser.org> 설치 후 `data\canonical.db` 열기.

---

## 9) 종료 & 재현

```powershell
# uvicorn 프로세스 종료는 해당 창에서 Ctrl+C

# 완전 초기화
Remove-Item data\canonical.db -ErrorAction SilentlyContinue
python -m stock_agent.scripts.bootstrap      # 다시 처음부터 (~2분)
```

---

## 트러블슈팅

### `.\.venv\Scripts\Activate.ps1` 실행 시 `...cannot be loaded because running scripts is disabled`
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
```
이 창에서만 허용되고, 창 닫으면 원상복구.

### `Invoke-RestMethod` 한글이 `?`로 보임
세션 UTF-8 설정 누락. 상단 [0)](#0-세션-초기-세팅-한-번만) 블록 다시 실행:
```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = 'utf-8'
```

### `database is locked`
재현되면 버그. SQLite single-writer 제약으로 인한 이슈이니
어느 경로가 tx 안에서 LLM 호출을 하는지 확인.

### `[Errno 2] No such file or directory` at OpenAI call
`SSL_CERT_FILE`가 존재하지 않는 경로를 가리키는 경우.
`stock_agent.agent_int.llm_gateway`의 `_repair_ssl_env()`가 자동 보정하므로
서버 재시작으로 해결됩니다. 수동으로 점검하려면:
```powershell
Test-Path $env:SSL_CERT_FILE
python -c "import certifi; print(certifi.where())"
```

### OpenAI 호출 실패
- `.env`의 `OPENAI_API_KEY` 확인
- 외부망 차단 환경이면 `.env`의 `LLM_KILL_SWITCH=1`로 파이프라인 자체만 검증

---

## 부록: Bash 버전

Git Bash / WSL / macOS를 쓴다면 주요 치환만:

| PowerShell | Bash |
|---|---|
| `.\.venv\Scripts\Activate.ps1` | `source .venv/Scripts/activate` |
| `Invoke-RestMethod -Uri ... -Method Post -Body $b -ContentType ...` | `curl -s -X POST ... -H "Content-Type: application/json" -d '...'` |
| `$env:FOO = '1'` | `export FOO=1` (or inline `FOO=1 python ...`) |
| `Remove-Item X -ErrorAction SilentlyContinue` | `rm -f X` |
| `Get-Content X` | `cat X` |
| 여기 문자열 `@"..."@` | heredoc `<<'EOF' ... EOF` 또는 single-quoted `-c '...'` |
