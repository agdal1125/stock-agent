"""FastAPI app for stock-agent (internal zone equivalent)."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

import markdown as md
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from ..config import CFG
from ..db import tx
from ..compile.run import lazy_compile
from . import cost as cost_tracker
from ..l1_index.section_builder import (
    SECTION_DESCRIPTIONS, SECTION_ORDER, SECTION_TYPES,
)
from ..l1_index.wiki_loader import TICKERS_ROOT, WIKI_ROOT, load_section_file
from . import cache as answer_cache
from .answer import answer as answer_query, answer_stream
from .shell import TAB_CHAT, TAB_EXPLORER, TAB_HOW, inject_shell


class UTF8JSONResponse(JSONResponse):
    """PowerShell 5.1의 Invoke-RestMethod는 Content-Type에 charset=utf-8이
    없으면 ISO-8859-1로 디코딩한다. 명시적으로 선언 + ensure_ascii=False."""
    media_type = "application/json; charset=utf-8"

    def render(self, content: Any) -> bytes:
        return json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


app = FastAPI(
    title="stock-agent (int)",
    version="0.1.0",
    default_response_class=UTF8JSONResponse,
)

# ---- Rate limit (P8) -------------------------------------------------------
# 기본: 분당 20회, 시간당 200회. RATE_LIMIT_* env 로 오버라이드 가능.
_RL_PER_MIN = os.getenv("RATE_LIMIT_PER_MIN", "20")
_RL_PER_HOUR = os.getenv("RATE_LIMIT_PER_HOUR", "200")
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[f"{_RL_PER_HOUR}/hour", f"{_RL_PER_MIN}/minute"],
    # headers_enabled=True 로 하면 slowapi 가 response 에 헤더 주입을 시도하는데
    # /ask 가 AskOut(Pydantic) 을 반환해 Response 객체가 아니어서 예외가 남.
    # PoC 용도로는 헤더 주입 끄고 단순 카운팅만 사용.
    headers_enabled=False,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


_STATIC_DIR = Path(__file__).parent / "static"


class AskIn(BaseModel):
    q: str = Field(..., description="사용자 자연어 질의")
    top_k_sections: int = 5
    trace: bool = False
    stream: bool = False


class AskOut(BaseModel):
    answer: str
    tickers: list[str]
    intent: str
    lazy_compiled: list[str]
    trace: dict[str, Any] | None = None


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home() -> HTMLResponse:
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(
        content=inject_shell(html, TAB_CHAT),
        media_type="text/html; charset=utf-8",
    )


@app.get("/how", response_class=HTMLResponse, include_in_schema=False)
def how_page() -> HTMLResponse:
    """작동 원리 — 대형 워크플로 다이어그램 + RAG 비교 + 비용 계산 + Wiki 탐색 로직."""
    body = r"""
<style>
  main.how-wrap {
    max-width: 1020px; margin: 0 auto; padding: 36px 28px 100px;
    font-family: -apple-system, "Pretendard", "Segoe UI", "Malgun Gothic", sans-serif;
    color: #e2e8f0;
  }
  main.how-wrap h1 {
    font-size: 28px; margin: 0 0 6px; letter-spacing: -0.015em;
  }
  main.how-wrap .lead { color: #94a3b8; margin: 0 0 32px; font-size: 14px; line-height: 1.6; }

  main.how-wrap .section-title {
    font-size: 15px; color: #fbbf24; margin: 36px 0 12px;
    text-transform: uppercase; letter-spacing: 0.08em; font-weight: 700;
  }

  main.how-wrap .card {
    background: #1e293b; border: 1px solid #334155; border-radius: 10px;
    padding: 20px 24px; margin-bottom: 14px;
  }
  main.how-wrap .card h2 {
    font-size: 16px; color: #38bdf8; margin: 0 0 10px;
    display: flex; align-items: center; gap: 8px;
  }
  main.how-wrap .card p, main.how-wrap .card li { font-size: 13px; color: #cbd5e1; line-height: 1.7; }
  main.how-wrap .card code {
    background: #0b1220; color: #fbbf24; padding: 1px 6px; border-radius: 4px;
    font-size: 12px;
  }

  /* ---------- 워크플로 다이어그램 ---------- */
  .flow {
    background: #0b1220; border: 1px solid #334155; border-radius: 12px;
    padding: 26px 22px; margin-bottom: 20px;
  }
  .flow-grid {
    display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px;
    align-items: stretch;
  }
  .flow-step {
    background: linear-gradient(180deg, #1e293b, #172032);
    border: 1px solid #334155; border-radius: 10px;
    padding: 14px 12px 16px; position: relative;
    transition: transform .15s, border-color .15s;
    cursor: pointer;
  }
  .flow-step:hover {
    transform: translateY(-2px);
    border-color: #38bdf8;
  }
  .flow-step.active {
    border-color: #38bdf8; box-shadow: 0 0 0 2px rgba(56,189,248,.2);
  }
  .flow-step .step-num {
    display: inline-block; width: 22px; height: 22px; line-height: 22px;
    text-align: center; background: #38bdf8; color: #0b1220;
    border-radius: 50%; font-weight: 700; font-size: 11px; margin-bottom: 6px;
  }
  .flow-step .step-title {
    font-size: 13px; color: #e2e8f0; font-weight: 600; margin-bottom: 4px;
  }
  .flow-step .step-sub {
    font-size: 10.5px; color: #94a3b8; line-height: 1.45;
  }
  .flow-step .step-cost {
    position: absolute; top: 10px; right: 10px;
    font-size: 9.5px; color: #fbbf24; font-family: Consolas, Menlo, monospace;
  }
  .flow-arrow {
    color: #64748b; font-size: 18px;
    align-self: center; text-align: center;
  }
  .flow-panel {
    background: #0b1220; border: 1px solid #38bdf8;
    border-radius: 10px; padding: 18px 22px; margin-top: 14px;
    min-height: 80px;
  }
  .flow-panel h3 {
    margin: 0 0 10px; font-size: 14px; color: #38bdf8;
    display: flex; align-items: center; gap: 8px;
  }
  .flow-panel p, .flow-panel li { font-size: 13px; color: #cbd5e1; line-height: 1.65; }
  .flow-panel code {
    background: #1e293b; color: #fbbf24; padding: 1px 6px;
    border-radius: 4px; font-size: 12px;
  }

  @media (max-width: 860px) {
    .flow-grid { grid-template-columns: repeat(2, 1fr); }
  }

  /* ---------- RAG 비교 카드 ---------- */
  .compare-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 8px;
  }
  .compare-col {
    background: #1e293b; border: 1px solid #334155;
    border-radius: 10px; padding: 18px 22px;
  }
  .compare-col.rag { border-left: 3px solid #64748b; }
  .compare-col.wiki { border-left: 3px solid #22c55e; }
  .compare-col h3 {
    margin: 0 0 12px; font-size: 15px; font-weight: 700;
    display: flex; justify-content: space-between; align-items: center;
  }
  .compare-col.rag h3 { color: #94a3b8; }
  .compare-col.wiki h3 { color: #86efac; }
  .compare-col ul { margin: 0; padding-left: 20px; }
  .compare-col li { font-size: 13px; color: #cbd5e1; margin: 6px 0; line-height: 1.6; }
  .compare-col .tag {
    font-size: 10px; padding: 2px 8px; border-radius: 999px;
    font-weight: 600; letter-spacing: .04em;
  }
  .compare-col.rag .tag { background: #334155; color: #94a3b8; }
  .compare-col.wiki .tag { background: rgba(34,197,94,.18); color: #86efac; }

  @media (max-width: 720px) {
    .compare-grid { grid-template-columns: 1fr; }
  }

  /* ---------- 비용 테이블 ---------- */
  table.cost {
    width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 4px;
  }
  table.cost th, table.cost td {
    border: 1px solid #334155; padding: 8px 12px;
    text-align: left; color: #cbd5e1;
  }
  table.cost th { background: #0b1220; color: #fbbf24; font-weight: 600; }
  table.cost tr.cached td { color: #86efac; }
  table.cost tr.cached td:first-child::before {
    content: "⚡ "; color: #fbbf24;
  }
  table.cost .num { font-family: Consolas, Menlo, monospace; text-align: right; }

  /* ---------- Wiki 섹션 주기 ---------- */
  .section-table {
    width: 100%; border-collapse: collapse; font-size: 13px;
  }
  .section-table th, .section-table td {
    padding: 7px 12px; border-bottom: 1px solid #334155; text-align: left;
  }
  .section-table th { color: #fbbf24; font-weight: 600; font-size: 12px; }
  .section-table code {
    background: #0b1220; color: #38bdf8; padding: 1px 6px; border-radius: 4px;
    font-size: 11px;
  }
  .section-table .freq.fast { color: #86efac; }
  .section-table .freq.slow { color: #94a3b8; }

  /* 범례 */
  .legend {
    display: flex; gap: 10px; font-size: 11px; color: #94a3b8;
    margin: 8px 0 18px; flex-wrap: wrap;
  }
  .legend span::before {
    content: ""; display: inline-block; width: 10px; height: 10px;
    border-radius: 50%; vertical-align: 1px; margin-right: 5px;
  }
  .legend .cold::before { background: #38bdf8; }
  .legend .warm::before { background: #fbbf24; }
  .legend .cached::before { background: #22c55e; }
</style>

<main class="how-wrap">
  <h1>작동 원리</h1>
  <p class="lead">
    NH Stock-Agent가 질문을 해석해서 <b>종목 Wiki</b>에서 근거를 찾고 답을 합성하기까지의
    과정입니다. 각 단계를 클릭하면 아래 패널에 상세 설명이 표시됩니다.
  </p>

  <!-- ============================ 워크플로 ============================ -->
  <div class="section-title">📊 답변 워크플로</div>

  <div class="flow">
    <div class="flow-grid">
      <div class="flow-step" data-step="1">
        <div class="step-num">1</div>
        <div class="step-title">질문 이해</div>
        <div class="step-sub">종목 · 의도 · 태그 추출</div>
        <div class="step-cost">~$0.0008</div>
      </div>
      <div class="flow-step" data-step="2">
        <div class="step-num">2</div>
        <div class="step-title">캐시 조회</div>
        <div class="step-sub">L1 → L2 → L3 3단</div>
        <div class="step-cost">⚡ $0</div>
      </div>
      <div class="flow-step" data-step="3">
        <div class="step-num">3</div>
        <div class="step-title">Wiki 탐색</div>
        <div class="step-sub">섹션 + 태그 + wikilink</div>
        <div class="step-cost">임베딩</div>
      </div>
      <div class="flow-step" data-step="4">
        <div class="step-num">4</div>
        <div class="step-title">컨텍스트</div>
        <div class="step-sub">토큰 한도 내 조립</div>
        <div class="step-cost">-</div>
      </div>
      <div class="flow-step" data-step="5">
        <div class="step-num">5</div>
        <div class="step-title">답변 생성</div>
        <div class="step-sub">근거 섹션 인용 포함</div>
        <div class="step-cost">~$0.003</div>
      </div>
      <div class="flow-step" data-step="6">
        <div class="step-num">6</div>
        <div class="step-title">저장·감사</div>
        <div class="step-sub">캐시 + llm_io_log</div>
        <div class="step-cost">-</div>
      </div>
    </div>

    <div class="flow-panel" id="flowPanel">
      <h3>1단계 · 질문 이해 (Query Understanding)</h3>
      <p>
        <code>gpt-5.4-mini</code>에 한 번의 LLM 호출로
        <b>ticker / intent / related_tags / related_tickers</b>를 한꺼번에 추출합니다.
        기존의 entity resolver + intent router를 1회 호출로 통합해 지연과 비용을 줄입니다.
      </p>
      <ul>
        <li>입력 ~400 토큰 · 출력 ~120 토큰 · 비용 ≈ <code>$0.0008 / 질의</code></li>
        <li>실패 시 규칙 기반 resolver/router로 fallback</li>
        <li>결과는 캐시 키 정규화에 쓰임 (표현이 달라도 같은 의도 ≈ 같은 키)</li>
      </ul>
    </div>
    <div class="legend">
      <span class="cold">cold 경로 — 실제 LLM 호출</span>
      <span class="warm">warm — 프롬프트 캐시 적용</span>
      <span class="cached">⚡ cached — diskcache 히트</span>
    </div>
  </div>

  <!-- ============================ RAG 비교 ============================ -->
  <div class="section-title">🔀 일반 RAG와의 차이</div>

  <div class="compare-grid">
    <section class="compare-col rag">
      <h3>일반 RAG <span class="tag">typical</span></h3>
      <ul>
        <li>벡터 유사도로 상위 K개 chunk만 가져옴</li>
        <li>chunk 단위 — 문맥이 토막나기 쉬움</li>
        <li>질의 간 구조 없음 — 매번 재검색</li>
        <li>같은 질문도 매번 LLM 호출</li>
        <li>지식 갱신은 전체 재임베딩</li>
      </ul>
    </section>
    <section class="compare-col wiki">
      <h3>Karpathy Wiki 방식 <span class="tag">this agent</span></h3>
      <ul>
        <li>종목별로 <b>누적되는 섹션 페이지</b>가 영구 artifact</li>
        <li>태그·[[wikilink]] 로 종목 간 지식망 연결</li>
        <li>hybrid: BM25 + 벡터 + RRF + 태그 확장 + 1-hop traversal</li>
        <li>3단 캐시로 반복 질의는 <b>대부분 $0</b></li>
        <li>섹션별 갱신 주기 차등 — 재무 일 1회, 뉴스 15분</li>
      </ul>
    </section>
  </div>

  <!-- ============================ 비용 ============================ -->
  <div class="section-title">💸 비용 (질의당 · 월)</div>

  <section class="card">
    <h2>단가 기준</h2>
    <p>
      <code>gpt-5.4-mini</code>: 입력 <code>$0.75/M</code> · 출력 <code>$4.5/M</code> ·
      OpenAI 프롬프트 캐시 적용 입력 <code>$0.075/M</code> (10× 할인)
    </p>
    <table class="cost">
      <tr>
        <th>경로</th><th>입력 토큰</th><th>출력 토큰</th><th class="num">비용 / 질의</th>
      </tr>
      <tr>
        <td>Query Understanding</td><td>~400</td><td>~120</td>
        <td class="num">$0.00084</td>
      </tr>
      <tr>
        <td>Answer — cold</td><td>~1,500</td><td>~400</td>
        <td class="num">$0.00293</td>
      </tr>
      <tr>
        <td>Answer — warm (OpenAI prompt cache 적용)</td>
        <td>300 fresh + 1,200 cached</td><td>~400</td>
        <td class="num">$0.00212</td>
      </tr>
      <tr class="cached">
        <td>diskcache 히트</td><td colspan="2">LLM 호출 없음</td>
        <td class="num">$0.00000</td>
      </tr>
    </table>
  </section>

  <section class="card">
    <h2>월 예상 (diskcache 히트율 70%)</h2>
    <table class="cost">
      <tr>
        <th>트래픽</th><th>월 질의 수</th><th class="num">월 비용</th>
      </tr>
      <tr><td>1,000/일</td><td>~30,000</td><td class="num">~$34</td></tr>
      <tr><td>5,000/일</td><td>~150,000</td><td class="num">~$171</td></tr>
      <tr><td>10,000/일</td><td>~300,000</td><td class="num">~$342</td></tr>
    </table>
    <p style="margin-top:10px">
      <b>보호 장치:</b> 일일 USD 상한을 <code>.env</code>에 두고 초과 시
      <code>LLM_KILL_SWITCH</code>로 자동 전환 (P7).
    </p>
  </section>

  <!-- ============================ Wiki 섹션 주기 ============================ -->
  <div class="section-title">📚 Wiki 섹션과 갱신 주기</div>

  <section class="card">
    <table class="section-table">
      <tr>
        <th>섹션</th><th>내용</th><th>갱신 주기</th>
      </tr>
      <tr><td><code>profile</code></td><td>종목 3줄 소개</td><td class="freq slow">일 1회</td></tr>
      <tr><td><code>latest_events</code></td><td>뉴스·공시</td><td class="freq fast">10~30분</td></tr>
      <tr><td><code>sns_events</code></td><td>SNS·종토방</td><td class="freq fast">15분</td></tr>
      <tr><td><code>business</code></td><td>사업 개요·제품</td><td class="freq slow">일 1회</td></tr>
      <tr><td><code>finance</code></td><td>재무·실적·가이던스 (외부: FnGuide)</td><td class="freq slow">일 1회</td></tr>
      <tr><td><code>relations</code></td><td>연관 기업·Entity</td><td class="freq slow">일 1회</td></tr>
      <tr><td><code>theme</code></td><td>테마·업종·섹터</td><td class="freq slow">일 1회</td></tr>
    </table>
  </section>

  <!-- ============================ 품질·감사 ============================ -->
  <div class="section-title">🛡️ 품질·감사 게이트</div>

  <section class="card">
    <ul>
      <li>사람이 <code>approved</code>한 claim만 검색 인덱스(section_doc)에 반영</li>
      <li>모든 LLM 요청/응답은 <code>llm_io_log</code> 감사 테이블에 기록</li>
      <li><code>LLM_KILL_SWITCH=1</code>이면 외부 호출 즉시 차단, 준비된 안내로 응답</li>
      <li>모델 ID 하드코딩 없음 — <code>OPENAI_MODEL</code> env로만 주입</li>
      <li>일일 USD 상한 초과 시 자동 kill-switch 전환 (P7 에서 연동 예정)</li>
    </ul>
  </section>
</main>

<script>
(function () {
  const STEPS = {
    '1': {
      title: '1단계 · 질문 이해 (Query Understanding)',
      html: `<p><code>gpt-5.4-mini</code> 한 번의 호출로 <b>ticker / intent / related_tags / related_tickers</b>를 한 번에 추출합니다. 기존 entity resolver + intent router를 통합 (P5).</p>
             <ul><li>입력 ~400 · 출력 ~120 토큰 · <code>$0.00084/질의</code></li>
             <li>실패 시 규칙 기반 fallback</li>
             <li>결과는 캐시 키 정규화에 쓰임</li></ul>`,
    },
    '2': {
      title: '2단계 · 3단 캐시 조회 (L1 → L3)',
      html: `<p>답변 생성 전 <b>캐시만 3단 조회</b>. 히트 시 밀리초 단위로 즉시 응답, ⚡cached 배지 표시.</p>
             <ul><li><b>L1</b> — 정규화 문자열 해시 (같은 질문)</li>
             <li><b>L2</b> — <code>(ticker, intent)</code> — 같은 종목·의도</li>
             <li><b>L3</b> — <code>(tag[], intent)</code> — 의미 확장</li>
             <li>미스 시에만 cold 경로 진입</li></ul>`,
    },
    '3': {
      title: '3단계 · Wiki 탐색 (3채널 병합)',
      html: `<p>단순 벡터 검색이 아닙니다. 3가지 채널을 병합합니다:</p>
             <ul>
               <li><b>(a) SKILL.md</b> — intent → 우선 섹션 1차 지목</li>
               <li><b>(b) #tag 교집합</b> — 1차 섹션의 상위 태그가 붙은 <u>다른 종목 섹션</u>을 흡수</li>
               <li><b>(c) [[wikilink]] 1-hop traversal</b> — 1차 섹션에서 outgoing link 따라 다른 종목 페이지 흡수</li>
             </ul>
             <p>최종 스코어는 BM25 + 벡터 유사도의 RRF (Reciprocal Rank Fusion). 이게 종목 간 지식망이 검색 엔진에도 배선된 이유입니다 (P5.5).</p>`,
    },
    '4': {
      title: '4단계 · 컨텍스트 조립',
      html: `<p>토큰 한도 내에서 구조화된 컨텍스트를 만듭니다.</p>
             <ul><li>상위 섹션 N개 원문 (score 순)</li>
             <li>최근 이벤트 타임라인 5건</li>
             <li>각 섹션 출처는 <code>ticker:section_type</code> 로 라벨링</li>
             <li>"참고한 문서" 패널에도 같은 라벨로 노출</li></ul>`,
    },
    '5': {
      title: '5단계 · 답변 생성 (LLM)',
      html: `<p>이 단계에서만 답변용 LLM을 호출합니다. 각 문장 뒤에 <code>(섹션)</code> 인용을 강제합니다.</p>
             <ul><li>입력 ~1,500 · 출력 ~400 토큰</li>
             <li>cold: <code>$0.00293/질의</code></li>
             <li>warm (OpenAI prompt cache): <code>$0.00212/질의</code></li>
             <li>토큰 스트리밍으로 즉시 반응 (<b>first token &lt; 1s</b>)</li>
             <li>투자 권유/예측 금지, 근거 부족 시 "확인 어렵습니다" 고지</li></ul>`,
    },
    '6': {
      title: '6단계 · 캐시 저장 + 감사 로그',
      html: `<p>완성된 답변은 3단 캐시에 저장됩니다. TTL은 intent 성격에 맞춰 차등.</p>
             <ul><li><code>latest_issue</code>/<code>sns_buzz</code>: 15분</li>
             <li><code>business</code>/<code>finance</code>/<code>relations</code>/<code>theme</code>: 24시간</li>
             <li><code>generic</code>: 30분</li>
             <li>모든 호출의 요청/응답/지연/에러는 <code>llm_io_log</code>에 기록 — NH Production의 LLM_REQUEST_RESPONSE 등가</li></ul>`,
    },
  };

  const panel = document.getElementById('flowPanel');
  function show(step) {
    const s = STEPS[step];
    if (!s) return;
    panel.innerHTML = `<h3>${s.title}</h3>${s.html}`;
    document.querySelectorAll('.flow-step').forEach(el => {
      el.classList.toggle('active', el.dataset.step === step);
    });
  }
  document.querySelectorAll('.flow-step').forEach(el => {
    el.addEventListener('click', () => show(el.dataset.step));
  });
  // 초기 강조
  const first = document.querySelector('.flow-step[data-step="1"]');
  if (first) first.classList.add('active');
})();
</script>
"""
    html = f"""<!DOCTYPE html>
<html lang="ko"><head>
<meta charset="utf-8"><title>작동 원리 — NH Stock-Agent</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>body {{ background: #0f172a; color: #e2e8f0; }}</style>
</head><body>{body}</body></html>"""
    return HTMLResponse(
        content=inject_shell(html, TAB_HOW),
        media_type="text/html; charset=utf-8",
    )


@app.get("/health")
def health() -> dict[str, Any]:
    with tx() as conn:
        n_tick = conn.execute("SELECT COUNT(*) AS n FROM ticker_master").fetchone()["n"]
        n_evt = conn.execute("SELECT COUNT(*) AS n FROM stock_event_timeline").fetchone()["n"]
        n_sec = conn.execute("SELECT COUNT(*) AS n FROM section_doc").fetchone()["n"]
        n_emb = conn.execute(
            "SELECT COUNT(*) AS n FROM section_doc WHERE embedding IS NOT NULL"
        ).fetchone()["n"]
    return {
        "model": CFG.openai_model,
        "kill_switch": CFG.kill_switch,
        "ticker_master": n_tick,
        "events": n_evt,
        "section_docs": n_sec,
        "embedded_docs": n_emb,
    }


@app.post("/ask")
@limiter.limit(os.getenv("RATE_LIMIT_ASK", "10/minute"))
def ask(request: Request, req: AskIn):
    if req.stream:
        def generate():
            for event in answer_stream(req.q, top_k_sections=req.top_k_sections):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson; charset=utf-8",
            headers={"X-Accel-Buffering": "no"},  # nginx buffering 방지
        )

    # non-streaming (eval / backward compat)
    t = answer_query(req.q, top_k_sections=req.top_k_sections)
    return AskOut(
        answer=t.answer,
        tickers=[r["ticker"] for r in t.resolved if r["score"] >= 80.0],
        intent=t.route.get("intent", "generic"),
        lazy_compiled=t.lazy_compiled,
        trace=asdict(t) if req.trace else None,
    )


@app.get("/cache/stats")
def cache_stats() -> dict[str, Any]:
    return answer_cache.stats()


@app.get("/cost")
def cost_summary() -> dict[str, Any]:
    """일일/월간 사용량 + 상한 + kill-switch 상태."""
    return cost_tracker.summary()


@app.post("/cache/invalidate/{ticker}")
def cache_invalidate(ticker: str) -> dict[str, Any]:
    n = answer_cache.invalidate_ticker(ticker)
    return {"ticker": ticker, "invalidated": n}


@app.get("/refresh/{ticker}")
def refresh(ticker: str) -> dict[str, Any]:
    """Redis refresh API 등가 — 특정 ticker 섹션만 재컴파일/재임베딩."""
    from ..compile.run import lazy_compile
    n = lazy_compile(ticker)
    return {"ticker": ticker, "recompiled_sections": n}


# ============================================================================
# Wiki browser (Karpathy LLM Wiki — .md는 파일시스템이 source of truth)
# ============================================================================

_TICKER_RE = re.compile(r"^\d{6}$")
_WIKI_TMPL = (Path(__file__).parent / "static" / "wiki.html").read_text(encoding="utf-8")

_MD = md.Markdown(extensions=["tables", "fenced_code", "toc"])

_WIKILINK_RE = re.compile(r"\[\[(\d{6})(?:\|([^\]]+))?\]\]")


def _preprocess_wikilinks(text: str) -> str:
    """Obsidian-style [[code|display]] → [display](/wiki/code)."""
    def repl(m: re.Match) -> str:
        code, display = m.group(1), m.group(2) or m.group(1)
        return f"[{display}](/wiki/{code})"
    return _WIKILINK_RE.sub(repl, text)


def _render_md(body: str) -> str:
    body = _preprocess_wikilinks(body)
    _MD.reset()
    return _MD.convert(body)


def _tag_chip(tag: str, count: int | None = None) -> str:
    suffix = f" <span class='tag-count'>{count}</span>" if count is not None else ""
    return (f'<a class="tag-chip" href="/wiki/tags/{tag}">'
            f'<span class="tag-hash">#</span>{tag}{suffix}</a>')


def _render_wiki_tmpl(title: str, header_title: str, badges_html: str,
                      nav_html: str, body_html: str) -> str:
    html = (_WIKI_TMPL
            .replace("{{TITLE}}", title)
            .replace("{{HEADER_TITLE}}", header_title)
            .replace("{{BADGES}}", badges_html)
            .replace("{{NAV_LINKS}}", nav_html)
            .replace("{{BODY}}", body_html))
    return inject_shell(html, TAB_EXPLORER)


@app.get("/wiki/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/wiki", response_class=HTMLResponse, include_in_schema=False)
def wiki_index() -> HTMLResponse:
    """Wiki 루트 — 파일 브라우저 스타일 트리 (지연 펼침).

    좌측 Explorer 트리(shell 주입)와 동일한 데이터를 본문에도 풍성하게 보여줘
    "LLM Wiki + File Browser" 컨셉을 한눈에 이해하게 한다.
    """
    with tx() as conn:
        rows = conn.execute(
            """SELECT tm.ticker, tm.name_ko, tm.market, tm.sector,
                      COALESCE(tt.tier,'lazy') AS tier,
                      COUNT(sd.doc_id) AS sec_n,
                      MAX(sd.updated_at) AS updated_at
               FROM ticker_master tm
               LEFT JOIN ticker_tier tt ON tt.ticker = tm.ticker
               LEFT JOIN section_doc sd ON sd.ticker = tm.ticker
               GROUP BY tm.ticker
               ORDER BY tt.tier='eager' DESC, sec_n DESC, tm.ticker""",
        ).fetchall()
        sec_rows = conn.execute(
            """SELECT ticker, section_type, updated_at
               FROM section_doc ORDER BY ticker, section_type"""
        ).fetchall()

    by_ticker: dict[str, list[dict]] = {}
    for r in sec_rows:
        stype = r["section_type"]
        order = SECTION_ORDER.get(stype, "99")
        by_ticker.setdefault(r["ticker"], []).append({
            "section_type": stype,
            "filename": f"{order}_{stype}.md",
            "label": SECTION_DESCRIPTIONS.get(stype, stype),
            "updated_at": r["updated_at"] or "",
        })
    for lst in by_ticker.values():
        lst.sort(key=lambda s: s["filename"])

    total_eager = sum(1 for r in rows if r["tier"] == "eager")
    total_compiled = sum(1 for r in rows if r["sec_n"])
    total_ghost = len(rows) - total_compiled

    # ---- HTML 생성 -------------------------------------------------------
    def ticker_block(r) -> str:
        tk = r["ticker"]
        compiled = bool(r["sec_n"])
        secs = by_ticker.get(tk, [])
        name = r["name_ko"]
        market = r["market"] or ""
        sector = r["sector"] or ""
        tier = r["tier"]
        updated = (r["updated_at"] or "")[:19]

        if compiled:
            head_cls = "folder compiled"
            meta = (f'<span class="folder-tier tier-{tier}">{tier}</span>'
                    f'<span class="folder-ext">·</span>'
                    f'<span class="folder-sec">{market}</span>'
                    f'<span class="folder-ext">·</span>'
                    f'<span class="folder-sec">{sector}</span>'
                    f'<span class="folder-ext"></span>'
                    f'<span class="folder-date">{updated}</span>')
            files_inner = "".join(
                f'<li class="file-line">'
                f'<a class="file-link" href="/wiki/{tk}#sec-{s["section_type"]}">'
                f'<span class="file-icon">📄</span>'
                f'<span class="file-name">{s["filename"]}</span>'
                f'<span class="file-desc">{s["label"]}</span>'
                f'</a></li>'
                for s in secs
            )
            # SKILL.md 는 별도 표기
            tdir_path = TICKERS_ROOT / tk
            if (tdir_path / "SKILL.md").is_file():
                files_inner += (
                    f'<li class="file-line">'
                    f'<a class="file-link skill" href="/wiki/{tk}#sec-skill">'
                    f'<span class="file-icon">🧭</span>'
                    f'<span class="file-name">SKILL.md</span>'
                    f'<span class="file-desc">Agent 정책</span>'
                    f'</a></li>'
                )
            body = f'<ul class="file-list">{files_inner}</ul>'
        else:
            head_cls = "folder ghost"
            meta = (f'<span class="folder-tier ghost-tag">ghost</span>'
                    f'<span class="folder-ext">·</span>'
                    f'<span class="folder-sec">{market or "-"}</span>'
                    f'<span class="folder-ext">·</span>'
                    f'<span class="folder-sec">{sector or "-"}</span>')
            body = (f'<ul class="file-list"><li class="file-line ghost-hint">'
                    f'<a class="file-link ghost-link" href="/wiki/{tk}">'
                    f'<span class="file-icon">✨</span>'
                    f'<span class="file-name">→ 클릭해서 자동 생성</span>'
                    f'<span class="file-desc">첫 방문 시 Wiki 섹션 파일 6개 + SKILL.md 생성</span>'
                    f'</a></li></ul>')

        return f"""
        <details class="{head_cls}">
          <summary class="folder-sum">
            <span class="folder-caret">▸</span>
            <span class="folder-icon">{'📁' if compiled else '📂'}</span>
            <span class="folder-code">{tk}/</span>
            <span class="folder-name">{name}</span>
            <span class="folder-meta">{meta}</span>
          </summary>
          {body}
        </details>"""

    ticker_blocks = "\n".join(ticker_block(r) for r in rows)

    # 상단 global files
    global_files_html = """
      <li class="file-line"><a class="file-link" href="/wiki/AGENTS.md">
        <span class="file-icon">📘</span>
        <span class="file-name">AGENTS.md</span>
        <span class="file-desc">전역 Agent 정책</span>
      </a></li>
      <li class="file-line"><a class="file-link" href="/wiki/log.md">
        <span class="file-icon">📝</span>
        <span class="file-name">log.md</span>
        <span class="file-desc">컴파일·ingest 로그</span>
      </a></li>
    """

    body_html = f"""
    <style>
      .wiki-hero {{
        background: linear-gradient(180deg, #132033, #0d1727);
        border: 1px solid #334155; border-radius: 12px;
        padding: 18px 22px; margin-bottom: 18px;
        display: flex; gap: 24px; align-items: center; flex-wrap: wrap;
      }}
      .wiki-hero .hero-text {{ flex: 1; min-width: 220px; }}
      .wiki-hero h2 {{
        margin: 0 0 4px; color: #e2e8f0; font-size: 18px; letter-spacing: -0.01em;
      }}
      .wiki-hero p {{ margin: 0; color: #94a3b8; font-size: 12.5px; line-height: 1.6; }}
      .wiki-hero .hero-stats {{ display: flex; gap: 18px; }}
      .wiki-hero .stat {{
        background: #0b1220; border: 1px solid #334155; border-radius: 8px;
        padding: 8px 14px; min-width: 72px; text-align: center;
      }}
      .wiki-hero .stat .num {{ color: #38bdf8; font-size: 20px; font-weight: 700; }}
      .wiki-hero .stat .lbl {{ color: #64748b; font-size: 10px; letter-spacing: .06em;
                               text-transform: uppercase; }}

      .path-heading {{
        font-family: Consolas, Menlo, monospace; font-size: 13px; color: #64748b;
        padding: 8px 2px; border-bottom: 1px dashed #334155; margin-bottom: 8px;
      }}
      .path-heading .crumb {{ color: #38bdf8; }}

      .folder {{
        border-radius: 8px; margin-bottom: 3px;
        background: transparent;
      }}
      .folder > summary {{
        list-style: none; cursor: pointer; padding: 8px 10px;
        display: flex; align-items: center; gap: 10px;
        border-radius: 8px; border-left: 2px solid transparent;
        font-family: Consolas, Menlo, monospace;
      }}
      .folder > summary::-webkit-details-marker {{ display: none; }}
      .folder .folder-caret {{
        color: #475569; font-size: 10px; width: 12px; display: inline-block;
        transition: transform .12s;
      }}
      .folder[open] > summary .folder-caret {{ transform: rotate(90deg); color: #94a3b8; }}
      .folder > summary:hover {{ background: #1a2336; }}
      .folder.compiled > summary .folder-code {{ color: #38bdf8; font-weight: 600; }}
      .folder.compiled > summary:hover {{ border-left-color: #38bdf8; }}
      .folder.ghost > summary {{ opacity: 0.7; }}
      .folder.ghost > summary .folder-code {{ color: #64748b; font-style: italic; }}
      .folder.ghost > summary:hover {{ background: rgba(251,191,36,.06); opacity: 1; }}
      .folder .folder-icon {{ font-size: 13px; }}
      .folder .folder-name {{
        color: #e2e8f0; font-family: -apple-system, "Pretendard", sans-serif;
      }}
      .folder .folder-meta {{
        margin-left: auto; display: flex; gap: 6px; align-items: center;
        font-family: -apple-system, "Pretendard", sans-serif; font-size: 11px;
        color: #64748b;
      }}
      .folder .folder-tier {{ padding: 1px 7px; border-radius: 999px;
        font-weight: 600; letter-spacing: .02em; }}
      .folder .tier-eager {{ background: rgba(34,197,94,.15); color: #86efac; }}
      .folder .tier-lazy  {{ background: rgba(100,116,139,.18); color: #cbd5e1; }}
      .folder .ghost-tag  {{ background: rgba(245,158,11,.12); color: #fbbf24; }}
      .folder .folder-sec {{ color: #94a3b8; }}
      .folder .folder-ext {{ color: #334155; }}
      .folder .folder-date {{ color: #475569; font-family: Consolas, Menlo, monospace; }}

      .file-list {{
        list-style: none; margin: 0; padding: 2px 0 8px 32px;
        border-left: 1px dashed #334155; margin-left: 16px;
      }}
      .file-line {{ margin: 1px 0; }}
      .file-link {{
        display: flex; align-items: center; gap: 10px;
        padding: 4px 10px; border-radius: 6px; text-decoration: none;
        color: #cbd5e1; font-family: Consolas, Menlo, monospace; font-size: 12.5px;
      }}
      .file-link:hover {{ background: #1e293b; color: #38bdf8; }}
      .file-link .file-icon {{ font-size: 12px; width: 16px; }}
      .file-link .file-name {{ color: #e2e8f0; }}
      .file-link:hover .file-name {{ color: #38bdf8; }}
      .file-link .file-desc {{
        margin-left: auto; color: #64748b; font-size: 11px;
        font-family: -apple-system, "Pretendard", sans-serif;
      }}
      .file-link.skill .file-name {{ color: #a7f3d0; }}
      .file-link.ghost-link {{ color: #f59e0b; }}
      .file-link.ghost-link .file-name {{ color: #fbbf24; }}

      .section-heading {{
        color: #fbbf24; font-size: 11px; letter-spacing: .08em;
        text-transform: uppercase; font-weight: 700;
        padding: 18px 4px 6px;
      }}
      .global-files {{
        list-style: none; margin: 0 0 14px 0; padding: 4px 0 4px 20px;
        border-left: 1px dashed #334155; margin-left: 16px;
      }}
    </style>

    <div class="wiki-hero">
      <div class="hero-text">
        <h2>🗂️ LLM Wiki · File Browser</h2>
        <p>
          종목별 문서는 <b>Markdown 파일</b>로 디스크에 저장되고, 그 파일이 답변의 원천입니다.
          폴더를 클릭해 펼치면 섹션 파일(<code>.md</code>)을 바로 볼 수 있어요.
        </p>
      </div>
      <div class="hero-stats">
        <div class="stat"><div class="num">{len(rows)}</div><div class="lbl">tickers</div></div>
        <div class="stat"><div class="num">{total_compiled}</div><div class="lbl">compiled</div></div>
        <div class="stat"><div class="num">{total_ghost}</div><div class="lbl">ghost</div></div>
        <div class="stat"><div class="num">{total_eager}</div><div class="lbl">eager</div></div>
      </div>
    </div>

    <div class="path-heading">
      <span class="crumb">wiki/</span>
    </div>

    <div class="section-heading">전역 파일</div>
    <ul class="global-files">{global_files_html}</ul>

    <div class="section-heading">종목별 폴더 (<code>wiki/tickers/</code>)</div>
    <div class="folders">
      {ticker_blocks}
    </div>
    """

    html = _render_wiki_tmpl(
        title="Wiki · File Browser",
        header_title="🗂️ <span style='color:#64748b;font-weight:400'>wiki/</span>",
        badges_html=('<span class="badge sector">Markdown 저장소</span>'
                     f'<span class="badge sector">{len(rows)} tickers</span>'),
        nav_html=('<a href="/wiki/AGENTS.md">📘 AGENTS.md</a> '
                  '<a href="/wiki/log.md">📝 log.md</a> '
                  '<a href="/wiki/tags/">🏷️ tags</a>'),
        body_html=body_html,
    )
    return HTMLResponse(html, media_type="text/html; charset=utf-8")


@app.get("/wiki/AGENTS",    response_class=HTMLResponse, include_in_schema=False)
@app.get("/wiki/AGENTS.md", response_class=HTMLResponse, include_in_schema=False)
@app.get("/wiki/log",       response_class=HTMLResponse, include_in_schema=False)
@app.get("/wiki/log.md",    response_class=HTMLResponse, include_in_schema=False)
def wiki_global(request: Request) -> HTMLResponse:
    path = request.url.path
    name = "AGENTS.md" if "AGENTS" in path else "log.md"
    p = WIKI_ROOT / name
    if not p.is_file():
        raise HTTPException(404, f"{name} not found")
    body_md = p.read_text(encoding="utf-8")
    body_md = re.sub(r"^---.*?---\s*", "", body_md, count=1, flags=re.DOTALL)
    body_html = (
        f'<div class="file-breadcrumb"><span class="path-seg">wiki</span>'
        f'<span class="path-sep">/</span><span class="path-file">{name}</span></div>'
        '<article class="section md-file"><div class="md">'
        + _render_md(body_md)
        + "</div></article>"
    )
    return HTMLResponse(
        _render_wiki_tmpl(
            title=name,
            header_title=f"📄 {name}",
            badges_html='<span class="badge sector">전역 파일 · .md</span>',
            nav_html='<a href="/wiki/">← wiki/</a>',
            body_html=body_html,
        ),
        media_type="text/html; charset=utf-8",
    )


@app.get("/wiki/api/tree.json", include_in_schema=False)
def wiki_tree() -> dict[str, Any]:
    """Explorer 사이드바용 트리 JSON — 전체 ticker_master + 컴파일 상태.

    각 ticker에 `compiled` 플래그, 컴파일된 경우 sections 목록 포함.
    """
    with tx() as conn:
        ticker_rows = conn.execute(
            """SELECT tm.ticker, tm.name_ko, tm.market, tm.sector,
                      COALESCE(tt.tier, 'lazy') AS tier,
                      COUNT(sd.doc_id) AS sec_n
               FROM ticker_master tm
               LEFT JOIN ticker_tier tt ON tt.ticker = tm.ticker
               LEFT JOIN section_doc sd ON sd.ticker = tm.ticker
               GROUP BY tm.ticker
               ORDER BY tt.tier='eager' DESC, sec_n DESC, tm.ticker""",
        ).fetchall()
        sec_rows = conn.execute(
            "SELECT ticker, section_type FROM section_doc ORDER BY ticker, section_type"
        ).fetchall()

    by_ticker: dict[str, list[dict]] = {}
    for r in sec_rows:
        t = r["ticker"]
        stype = r["section_type"]
        order = SECTION_ORDER.get(stype, "99")
        by_ticker.setdefault(t, []).append({
            "section_type": stype,
            "filename": f"{order}_{stype}.md",
            "label": SECTION_DESCRIPTIONS.get(stype, stype),
        })
    for lst in by_ticker.values():
        lst.sort(key=lambda s: s["filename"])

    tickers_out = []
    for r in ticker_rows:
        tk = r["ticker"]
        tdir = TICKERS_ROOT / tk
        compiled = bool(r["sec_n"]) and tdir.is_dir()
        tickers_out.append({
            "ticker": tk,
            "name_ko": r["name_ko"],
            "market": r["market"] or "",
            "sector": r["sector"] or "",
            "tier": r["tier"],
            "compiled": compiled,
            "sections": by_ticker.get(tk, []) if compiled else [],
        })
    return {"tickers": tickers_out}


def _ensure_ticker_compiled(ticker: str) -> bool:
    """ticker_master 에 등록된 ghost 종목이면 lazy compile 발동.
    반환: True = 이미 컴파일됐거나 이번에 성공, False = master에 없음."""
    tdir = TICKERS_ROOT / ticker
    if tdir.is_dir():
        with tx() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM section_doc WHERE ticker=?", (ticker,)
            ).fetchone()["n"]
        if n:
            return True
    with tx() as conn:
        row = conn.execute(
            "SELECT 1 FROM ticker_master WHERE ticker=?", (ticker,)
        ).fetchone()
    if not row:
        return False
    lazy_compile(ticker)
    return True


@app.get("/wiki/{ticker}", response_class=HTMLResponse, include_in_schema=False)
def wiki_ticker(ticker: str) -> HTMLResponse:
    if not _TICKER_RE.match(ticker):
        raise HTTPException(400, "ticker must be 6-digit code")
    tdir = TICKERS_ROOT / ticker
    if not tdir.is_dir():
        # ghost 티커 자동 컴파일 — ticker_master 에 등록되어 있으면 on-demand 생성
        if not _ensure_ticker_compiled(ticker):
            raise HTTPException(
                404,
                f"{ticker}는 ticker_master에 등록되어 있지 않습니다."
            )

    # metadata + tags + backlinks
    with tx() as conn:
        tm = conn.execute(
            "SELECT name_ko, sector, market FROM ticker_master WHERE ticker=?",
            (ticker,),
        ).fetchone()
        tag_rows = conn.execute(
            """SELECT tag, COUNT(*) n FROM section_tag
               WHERE doc_id LIKE ?
               GROUP BY tag ORDER BY n DESC, tag""",
            (f"{ticker}:%",),
        ).fetchall()
        # 섹션별 태그 (Obsidian 노트별 태그 표시)
        section_tags_rows = conn.execute(
            """SELECT doc_id, tag FROM section_tag
               WHERE doc_id LIKE ?
               ORDER BY doc_id, tag""",
            (f"{ticker}:%",),
        ).fetchall()
        section_wl_rows = conn.execute(
            """SELECT src_doc_id, target_ticker, display_text
               FROM section_wikilink
               WHERE src_doc_id LIKE ?
               ORDER BY src_doc_id""",
            (f"{ticker}:%",),
        ).fetchall()
        backlink_rows = conn.execute(
            """SELECT sd.ticker AS src_ticker, sd.section_type AS src_section,
                      tm.name_ko, COUNT(*) AS n
               FROM section_wikilink wl
               JOIN section_doc sd ON sd.doc_id = wl.src_doc_id
               JOIN ticker_master tm ON tm.ticker = sd.ticker
               WHERE wl.target_ticker = ? AND sd.ticker != ?
               GROUP BY sd.ticker, sd.section_type
               ORDER BY sd.ticker, sd.section_type""",
            (ticker, ticker),
        ).fetchall()
    name_ko = tm["name_ko"] if tm else ticker
    sector = tm["sector"] if tm else "-"
    market = tm["market"] if tm else ""

    badges_parts = [f'<span class="badge ticker">{ticker}</span>']
    if market:
        badges_parts.append(f'<span class="badge market">{market}</span>')
    badges_parts.append(f'<span class="badge sector">{sector}</span>')
    badges = "".join(badges_parts)

    # 섹션별 tag/wikilink 맵
    section_tags: dict[str, list[str]] = {}
    for r in section_tags_rows:
        stype = r["doc_id"].split(":", 1)[1] if ":" in r["doc_id"] else ""
        section_tags.setdefault(stype, []).append(r["tag"])

    section_links: dict[str, list[tuple[str, str]]] = {}
    for r in section_wl_rows:
        stype = r["src_doc_id"].split(":", 1)[1] if ":" in r["src_doc_id"] else ""
        section_links.setdefault(stype, []).append(
            (r["target_ticker"], r["display_text"] or r["target_ticker"])
        )

    # 디스크 경로 베이스 (시각화용)
    path_base = f"wiki/tickers/{ticker}"

    # ---------- 파일 카드 빌더 ------------------------------------------------
    def file_card(
        filename: str,
        anchor: str,
        icon: str,
        card_kind: str,
        raw_text: str,
        rendered_body_md: str,
        updated_at: str | None,
        tags: list[str],
        wikilinks: list[tuple[str, str]],
        subtitle: str,
    ) -> str:
        raw_escaped = (raw_text.replace("&", "&amp;")
                               .replace("<", "&lt;").replace(">", "&gt;"))
        rendered_html = _render_md(rendered_body_md)

        tag_row = ""
        if tags:
            tag_row = ('<div class="mdf-tags">'
                       + "".join(_tag_chip(t) for t in tags)
                       + "</div>")
        link_row = ""
        if wikilinks:
            chips = " ".join(
                f'<a class="wl-chip" href="/wiki/{code}">'
                f'<span class="wl-arrow">→</span> <code>{code}</code> {name}</a>'
                for code, name in wikilinks
            )
            link_row = f'<div class="mdf-links">{chips}</div>'

        updated_span = (f'<span class="mdf-updated">updated {updated_at[:19]}</span>'
                        if updated_at else "")

        return f"""
        <article class="md-file" id="{anchor}" data-kind="{card_kind}">
          <div class="mdf-tabbar">
            <div class="mdf-tab active">
              <span class="mdf-tab-icon">{icon}</span>
              <span class="mdf-tab-name">{filename}</span>
            </div>
            <div class="mdf-tab-spacer"></div>
            {updated_span}
          </div>
          <div class="mdf-pathbar">
            <span class="mdf-path">{path_base}/<b>{filename}</b></span>
            <span class="mdf-sub">{subtitle}</span>
          </div>
          {tag_row}{link_row}
          <div class="mdf-body md">{rendered_html}</div>
          <details class="mdf-raw">
            <summary>📑 원본 .md 보기 (frontmatter 포함)</summary>
            <pre class="mdf-raw-pre">{raw_escaped}</pre>
          </details>
        </article>"""

    # ---------- 상단: 폴더 헤더 + 태그 클라우드 -------------------------------
    file_names_in_dir: list[str] = []  # 폴더 리스트에 넣을 파일명
    if (tdir / "SKILL.md").is_file():
        file_names_in_dir.append("SKILL.md")
    for stype in SECTION_TYPES:
        fname = f"{SECTION_ORDER[stype]}_{stype}.md"
        if (tdir / fname).is_file():
            file_names_in_dir.append(fname)

    dir_listing_html = "".join(
        f'<a class="dir-file" href="#sec-{f.split(".")[0].split("_", 1)[-1] if "_" in f else "skill"}">'
        f'<span class="mdf-tab-icon">{"🧭" if f == "SKILL.md" else "📄"}</span>'
        f'{f}</a>'
        for f in file_names_in_dir
    )

    tag_cloud_html = ""
    if tag_rows:
        chips = "".join(_tag_chip(r["tag"], r["n"]) for r in tag_rows)
        tag_cloud_html = (
            '<aside class="ticker-tags">'
            f'<div class="ticker-tags-head">🏷️ 이 종목 관련 태그 · {len(tag_rows)}개</div>'
            f'<div class="tag-cloud">{chips}</div>'
            '</aside>'
        )

    dir_header_html = f"""
    <div class="dir-header">
      <div class="dir-breadcrumb">
        <span class="crumb">📁</span>
        <a href="/wiki/">wiki</a>
        <span class="sep">/</span>
        <span>tickers</span>
        <span class="sep">/</span>
        <span class="crumb-current">{ticker}/</span>
      </div>
      <div class="dir-listing">{dir_listing_html}</div>
    </div>"""

    # ---------- 섹션 파일 카드들 ---------------------------------------------
    nav_items: list[str] = []
    cards_html: list[str] = []

    # SKILL.md
    skill_path = tdir / "SKILL.md"
    if skill_path.is_file():
        skill_sec = load_section_file(skill_path)
        body = skill_sec.body if skill_sec else ""
        body = re.sub(r"^#.*?\n", "", body, count=1)
        raw_text = skill_sec.raw if skill_sec else ""
        nav_items.append('<a href="#sec-skill">SKILL.md</a>')
        cards_html.append(file_card(
            filename="SKILL.md",
            anchor="sec-skill",
            icon="🧭",
            card_kind="skill",
            raw_text=raw_text,
            rendered_body_md=body,
            updated_at=(skill_sec.meta.get("updated_at") if skill_sec else None),
            tags=[],
            wikilinks=[],
            subtitle="Agent 정책 · intent→sections 매핑",
        ))

    for stype in SECTION_TYPES:
        fname = f"{SECTION_ORDER[stype]}_{stype}.md"
        fpath = tdir / fname
        if not fpath.is_file():
            continue
        friendly = SECTION_DESCRIPTIONS.get(stype, stype)
        nav_items.append(f'<a href="#sec-{stype}">{fname}</a>')
        sec = load_section_file(fpath)
        if sec is None:
            continue
        body = sec.body.strip()
        body = re.sub(r"^#.*?\n", "", body, count=1)
        cards_html.append(file_card(
            filename=fname,
            anchor=f"sec-{stype}",
            icon="📄",
            card_kind=stype,
            raw_text=sec.raw,
            rendered_body_md=body,
            updated_at=sec.meta.get("updated_at"),
            tags=section_tags.get(stype, []),
            wikilinks=section_links.get(stype, []),
            subtitle=friendly,
        ))

    # Backlinks — 별도 카드
    backlinks_html = ""
    if backlink_rows:
        rows_html = "".join(
            f'<li><a href="/wiki/{r["src_ticker"]}#sec-{r["src_section"]}">'
            f'<code>{r["src_ticker"]}</code> {r["name_ko"]} — {r["src_section"]}'
            f'</a>  <span class="meta-small">({r["n"]})</span></li>'
            for r in backlink_rows
        )
        backlinks_html = (
            '<article class="md-file backlinks" id="sec-backlinks">'
            '<div class="mdf-tabbar">'
            '<div class="mdf-tab active">'
            '<span class="mdf-tab-icon">⟵</span>'
            '<span class="mdf-tab-name">backlinks</span>'
            '</div></div>'
            '<div class="mdf-pathbar">'
            '<span class="mdf-sub">이 종목을 언급하는 다른 페이지</span>'
            '</div>'
            f'<ul class="backlink-list">{rows_html}</ul>'
            '</article>'
        )

    # ---------- 페이지 최종 조립 ---------------------------------------------
    styles = """
    <style>
      .dir-header {
        background: #0b1220; border: 1px solid #334155; border-radius: 10px;
        padding: 12px 16px; margin-bottom: 18px;
      }
      .dir-breadcrumb {
        font-family: Consolas, Menlo, monospace; font-size: 13px;
        color: #94a3b8; display: flex; align-items: center; gap: 6px;
        padding-bottom: 10px; border-bottom: 1px dashed #334155; margin-bottom: 10px;
      }
      .dir-breadcrumb a { color: #38bdf8; text-decoration: none; }
      .dir-breadcrumb a:hover { text-decoration: underline; }
      .dir-breadcrumb .sep { color: #475569; }
      .dir-breadcrumb .crumb-current { color: #e2e8f0; font-weight: 600; }
      .dir-listing {
        display: flex; flex-wrap: wrap; gap: 4px;
      }
      .dir-listing .dir-file {
        display: inline-flex; align-items: center; gap: 6px;
        padding: 4px 10px; border-radius: 6px;
        color: #cbd5e1; text-decoration: none;
        font-family: Consolas, Menlo, monospace; font-size: 12px;
        background: #132033; border: 1px solid #1e293b;
        transition: all .1s;
      }
      .dir-listing .dir-file:hover {
        color: #38bdf8; border-color: #38bdf8; background: #152b42;
      }

      .ticker-tags {
        background: #0b1220; border: 1px solid #334155; border-radius: 10px;
        padding: 10px 14px; margin-bottom: 18px;
      }
      .ticker-tags .ticker-tags-head {
        color: #94a3b8; font-size: 11px; margin-bottom: 8px;
        text-transform: uppercase; letter-spacing: .06em;
      }

      /* 파일 카드 — 코드 에디터 탭 느낌 */
      .md-file {
        border: 1px solid #334155; border-radius: 8px;
        margin-bottom: 20px; background: #1e293b;
        overflow: hidden;
      }
      .md-file .mdf-tabbar {
        display: flex; align-items: center;
        background: #0b1220; border-bottom: 1px solid #334155;
        padding: 0 0 0 10px; height: 34px;
      }
      .md-file .mdf-tab {
        display: flex; align-items: center; gap: 8px;
        background: #1e293b; color: #e2e8f0;
        padding: 8px 18px 8px 14px; height: 100%;
        border-right: 1px solid #334155;
        font-family: Consolas, Menlo, monospace; font-size: 12.5px;
        border-top: 2px solid #38bdf8;
        margin-top: 2px;
      }
      .md-file .mdf-tab-icon { font-size: 12px; }
      .md-file .mdf-tab-spacer { flex: 1; }
      .md-file .mdf-updated {
        color: #64748b; font-size: 11px; padding-right: 14px;
        font-family: Consolas, Menlo, monospace;
      }
      .md-file .mdf-pathbar {
        padding: 8px 16px;
        background: #132033; border-bottom: 1px solid #334155;
        display: flex; justify-content: space-between; align-items: center;
        font-size: 12px; flex-wrap: wrap; gap: 6px;
      }
      .md-file .mdf-path {
        font-family: Consolas, Menlo, monospace; color: #64748b;
      }
      .md-file .mdf-path b { color: #38bdf8; font-weight: 600; }
      .md-file .mdf-sub { color: #94a3b8; }
      .md-file .mdf-tags,
      .md-file .mdf-links {
        padding: 8px 16px;
        background: #172032; border-bottom: 1px dashed #334155;
        display: flex; flex-wrap: wrap; gap: 6px;
      }
      .md-file .mdf-body {
        padding: 18px 22px 20px;
      }
      .md-file details.mdf-raw {
        background: #0b1220; border-top: 1px solid #334155;
        margin: 0; padding: 0; border-radius: 0;
      }
      .md-file details.mdf-raw > summary {
        padding: 8px 16px; color: #94a3b8; font-size: 12px;
        font-family: Consolas, Menlo, monospace; cursor: pointer;
        user-select: none; list-style: none;
      }
      .md-file details.mdf-raw[open] > summary { color: #fbbf24; }
      .md-file details.mdf-raw .mdf-raw-pre {
        margin: 0; padding: 12px 16px 16px;
        background: #0b1220; color: #cbd5e1;
        font-family: Consolas, Menlo, monospace; font-size: 11.5px;
        line-height: 1.55; white-space: pre-wrap;
        border-top: 1px dashed #334155;
      }
      .md-file[data-kind="skill"] .mdf-tab { border-top-color: #22c55e; }
      .md-file[data-kind="latest_events"] .mdf-tab { border-top-color: #fbbf24; }
      .md-file[data-kind="sns_events"]    .mdf-tab { border-top-color: #f472b6; }
      .md-file[data-kind="finance"]       .mdf-tab { border-top-color: #8b5cf6; }
      .md-file[data-kind="relations"]     .mdf-tab { border-top-color: #a78bfa; }
      .md-file[data-kind="theme"]         .mdf-tab { border-top-color: #f59e0b; }
      .md-file.backlinks .mdf-tab { border-top-color: #fbbf24; }

      .backlink-list { list-style: none; margin: 0; padding: 12px 16px; }
      .backlink-list li { padding: 6px 0; border-bottom: 1px dashed #334155; }
      .backlink-list li:last-child { border-bottom: 0; }
      .backlink-list a { color: #cbd5e1; text-decoration: none; }
      .backlink-list a:hover { color: #38bdf8; }
    </style>
    """

    body_html = (styles + dir_header_html + tag_cloud_html
                 + "\n".join(cards_html) + backlinks_html)

    nav_html = ('<a href="/wiki/">← wiki/</a> '
                + '<a href="/wiki/tags/">🏷️ tags</a>  '
                + '  '.join(nav_items)
                + (' <a href="#sec-backlinks">⟵ backlinks</a>' if backlinks_html else ''))

    html = _render_wiki_tmpl(
        title=f"{name_ko} ({ticker}) — wiki/tickers/{ticker}/",
        header_title=(f'<span style="font-family:Consolas,Menlo,monospace;color:#64748b;font-weight:400;font-size:14px">'
                      f'wiki/tickers/</span>'
                      f'<span style="font-family:Consolas,Menlo,monospace;color:#38bdf8;font-weight:700">{ticker}</span>'
                      f'<span style="font-family:Consolas,Menlo,monospace;color:#64748b;font-weight:400;font-size:14px">/</span>'
                      f' &nbsp; <span style="font-size:15px;font-weight:500">{name_ko}</span>'),
        badges_html=badges,
        nav_html=nav_html,
        body_html=body_html,
    )
    return HTMLResponse(html, media_type="text/html; charset=utf-8")


# ---- Tag pages -------------------------------------------------------------

@app.get("/wiki/tags/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/wiki/tags", response_class=HTMLResponse, include_in_schema=False)
def tag_index() -> HTMLResponse:
    with tx() as conn:
        rows = conn.execute(
            """SELECT tag, COUNT(DISTINCT doc_id) n
               FROM section_tag
               GROUP BY tag ORDER BY n DESC, tag"""
        ).fetchall()

    chips = "".join(_tag_chip(r["tag"], r["n"]) for r in rows)
    body_html = (
        '<article class="section">'
        '<div class="section-head"><h2>전체 태그</h2>'
        f'<div class="meta-small">{len(rows)}개</div></div>'
        f'<div class="tag-cloud">{chips}</div>'
        '</article>'
    )
    return HTMLResponse(
        _render_wiki_tmpl(
            title="전체 태그",
            header_title="전체 태그",
            badges_html='<span class="badge sector">tags</span>',
            nav_html='<a href="/wiki/">← Index</a>',
            body_html=body_html,
        ),
        media_type="text/html; charset=utf-8",
    )


@app.get("/wiki/tags/{tag}", response_class=HTMLResponse, include_in_schema=False)
def tag_page(tag: str) -> HTMLResponse:
    with tx() as conn:
        rows = conn.execute(
            """SELECT sd.ticker, sd.section_type, tm.name_ko
               FROM section_tag st
               JOIN section_doc sd ON sd.doc_id = st.doc_id
               JOIN ticker_master tm ON tm.ticker = sd.ticker
               WHERE st.tag = ?
               ORDER BY sd.ticker, sd.section_type""",
            (tag,),
        ).fetchall()

    if not rows:
        raise HTTPException(404, f"tag '{tag}' not found")

    # ticker별로 그룹핑
    from collections import defaultdict
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {"name": "", "sections": []})
    for r in rows:
        g = grouped[r["ticker"]]
        g["name"] = r["name_ko"]
        g["sections"].append(r["section_type"])

    items = []
    for code, g in sorted(grouped.items()):
        sec_chips = " ".join(
            f'<a class="mini-chip" href="/wiki/{code}#sec-{s}">{s}</a>'
            for s in g["sections"]
        )
        items.append(
            f'<li><a class="big-link" href="/wiki/{code}">'
            f'<code>{code}</code> {g["name"]}</a>  {sec_chips}</li>'
        )

    body_html = (
        '<article class="section">'
        f'<div class="section-head"><h2><span class="tag-hash">#</span>{tag}</h2>'
        f'<div class="meta-small">{len(rows)}개 섹션 · {len(grouped)}개 종목</div></div>'
        f'<ul class="tag-result-list">{"".join(items)}</ul>'
        '</article>'
    )
    return HTMLResponse(
        _render_wiki_tmpl(
            title=f"#{tag}",
            header_title=f"<span class='tag-hash'>#</span>{tag}",
            badges_html=f'<span class="badge sector">{len(rows)} sections</span>',
            nav_html='<a href="/wiki/">← Index</a> <a href="/wiki/tags/">전체 태그</a>',
            body_html=body_html,
        ),
        media_type="text/html; charset=utf-8",
    )
