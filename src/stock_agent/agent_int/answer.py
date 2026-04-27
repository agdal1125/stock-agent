"""질의 답변 합성 — L0 snapshot + L1 hybrid search → LLM compose."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from ..compile.run import lazy_compile
from ..db import tx
from ..entity.resolver import ResolveHit, resolve
from ..l1_index.hybrid_search import SectionHit, search
from . import cache
from .cost import BudgetExceeded
from .llm_gateway import chat_stream, chat_text
from .qu import QueryPlan, understand
from .router import RoutedQuery, route


ANSWER_SYSTEM = """당신은 한국 주식 종목 설명 에이전트다.
제공된 '컨텍스트'만 근거로 사용자 질문에 답한다. 컨텍스트에 없는 사실은 말하지 않는다.

규칙:
- 한국어로 3~6문장 이내로 답한다.
- 투자 권유/매수매도 의견/주가 예측은 **하지 않는다**.
- 각 주장 뒤에 괄호로 근거 섹션을 명시: (profile), (latest_events) 등.
- 컨텍스트가 부족하면 "현재 확인된 정보로는 답하기 어렵습니다."라고 말한다.
- 원문이 합성 예시라는 점을 굳이 언급할 필요는 없다.
- '대화 기록' 이 함께 제공되면 이전 턴의 흐름을 자연스럽게 잇되, 사실 주장은
  반드시 컨텍스트의 근거 섹션에서만 끌어온다.
"""


def _format_history(history: list[dict] | None, max_turns: int = 6) -> str:
    """이전 대화 턴을 LLM 프롬프트용 plain text 로 직렬화. 최근 N턴만 사용."""
    if not history:
        return ""
    role_label = {"user": "사용자", "assistant": "에이전트", "system": "시스템"}
    lines: list[str] = []
    for m in history[-max_turns:]:
        role = (m.get("role") or "user").lower()
        content = (m.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"{role_label.get(role, role.upper())}: {content}")
    return "\n".join(lines)


def _carryover_tickers_from_history(
    history: list[dict] | None, top_k: int = 3,
) -> list[dict]:
    """이전 user 턴에서 가장 최근에 확정된 ticker 매칭을 끌어옴.

    Multi-turn 에서 latest message ('그래서 실적은?') 만으로는 종목이 안 잡힐 때,
    바로 이전 user 메시지에서 매칭된 종목을 캐리오버해 같은 대상에 대해 답하게 한다.
    반환값은 ResolveHit 와 호환되는 `dict` 리스트 (matched_via 에 '@history' 표시).
    """
    if not history:
        return []
    for prev in reversed(history):
        if (prev.get("role") or "").lower() != "user":
            continue
        text = (prev.get("content") or "").strip()
        if not text:
            continue
        prev_hits = resolve(text, top_k=top_k)
        strong = [h for h in prev_hits if h.score >= 80.0]
        if strong:
            return [
                {
                    "ticker": h.ticker.code,
                    "name_ko": h.ticker.name_ko,
                    "score": h.score,
                    "matched_via": f"{h.matched_via}@history",
                }
                for h in strong
            ]
    return []


@dataclass
class AnswerTrace:
    query: str
    resolved: list[dict] = field(default_factory=list)
    route: dict[str, Any] = field(default_factory=dict)
    section_hits: list[dict] = field(default_factory=list)
    lazy_compiled: list[str] = field(default_factory=list)
    answer: str = ""
    used_model: str = ""


def _fetch_event_headlines(tickers: list[str], limit: int = 5) -> list[dict]:
    if not tickers:
        return []
    placeholders = ",".join(["?"] * len(tickers))
    with tx() as conn:
        rows = conn.execute(
            f"""SELECT ticker, occurred_at, event_type, headline, impact_score
                FROM stock_event_timeline
                WHERE ticker IN ({placeholders})
                ORDER BY occurred_at DESC
                LIMIT ?""",
            (*tickers, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _ensure_compiled(tickers: list[str]) -> list[str]:
    """section_doc 이 없는 종목이 있으면 lazy compile 발동."""
    if not tickers:
        return []
    lazy_done: list[str] = []
    with tx() as conn:
        for t in tickers:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM section_doc WHERE ticker=?", (t,)
            ).fetchone()["n"]
            if n == 0:
                lazy_done.append(t)
    for t in lazy_done:
        lazy_compile(t)
    return lazy_done


def compose_context(tickers: list[str], section_hits: list[SectionHit],
                    events: list[dict]) -> str:
    parts: list[str] = []
    parts.append(f"## 질의 관련 종목: {', '.join(tickers) if tickers else '(해석 실패)'}\n")
    if events:
        parts.append("## 최근 이벤트 (timeline)")
        for e in events:
            parts.append(f"- [{e['occurred_at'][:16]}] {e['ticker']} "
                         f"{e['event_type']}: {e['headline']} (impact={e['impact_score']:.2f})")
        parts.append("")
    if section_hits:
        parts.append("## 섹션 컨텍스트")
        for h in section_hits:
            parts.append(f"### {h.ticker}:{h.section_type}  (score={h.score:.3f})")
            parts.append(h.content.strip())
            parts.append("")
    return "\n".join(parts)


def answer(query: str, top_k_sections: int = 5,
           history: list[dict] | None = None) -> AnswerTrace:
    """단일 호출(non-streaming) 답변 합성.

    `history` 가 주어지면 이전 대화 턴을 LLM 프롬프트의 '대화 기록' 블록으로
    포함시킨다. resolver/router/search 는 항상 latest `query` 만 사용한다.
    """
    trace = AnswerTrace(query=query)
    from ..config import CFG
    trace.used_model = CFG.openai_model

    # 1) entity resolve
    hits = resolve(query, top_k=3)
    trace.resolved = [
        {"ticker": h.ticker.code, "name_ko": h.ticker.name_ko,
         "score": h.score, "matched_via": h.matched_via}
        for h in hits
    ]
    tickers = [h.ticker.code for h in hits if h.score >= 80.0]

    # 1-bis) multi-turn carryover: latest 메시지로 종목이 안 잡혔다면 직전 user 턴 참조
    if not tickers:
        carry = _carryover_tickers_from_history(history)
        if carry:
            trace.resolved.extend(carry)
            tickers = [c["ticker"] for c in carry]

    # 2) intent route
    r: RoutedQuery = route(query)
    trace.route = {"intent": r.intent, "sections": r.sections,
                   "freshness_sla_sec": r.freshness_sla_sec,
                   "classified_by": r.classified_by}

    # 3) lazy compile 필요 시
    trace.lazy_compiled = _ensure_compiled(tickers)

    # 4) L1 hybrid search (ticker + section namespace 필터)
    section_hits: list[SectionHit] = []
    if tickers:
        section_hits = search(
            query=query,
            tickers=tickers,
            section_types=r.sections,
            top_k=top_k_sections,
        )
    trace.section_hits = [
        {"doc_id": h.doc_id, "ticker": h.ticker, "section_type": h.section_type,
         "score": h.score, "content_preview": h.content[:200]}
        for h in section_hits
    ]

    # 5) L0 event timeline 스냅샷
    events = _fetch_event_headlines(tickers, limit=5)

    # 6) 컴포즈
    if not tickers:
        trace.answer = "질의에서 종목을 특정하지 못했습니다. 종목명 또는 6자리 코드를 포함해 다시 질문해주세요."
        return trace

    ctx = compose_context(tickers, section_hits, events)
    history_block = _format_history(history)
    if history_block:
        user = f"대화 기록:\n{history_block}\n\n질문: {query}\n\n{ctx}"
    else:
        user = f"질문: {query}\n\n{ctx}"
    try:
        text = chat_text(
            prompt_id="answer_compose_v1",
            system=ANSWER_SYSTEM,
            user=user,
            max_tokens=700,
        )
    except Exception as e:
        text = f"[답변 생성 실패: {e}]"
    trace.answer = text.strip()
    return trace


# ============================================================================
# Streaming variant — 같은 파이프라인을 토큰 스트림 + 캐시 분기로 제공
# ============================================================================

def _section_hit_dicts(hits: list[SectionHit]) -> list[dict]:
    return [
        {
            "doc_id": h.doc_id,
            "ticker": h.ticker,
            "section_type": h.section_type,
            "score": round(h.score, 4),
            "content_preview": h.content[:200],
            "source": getattr(h, "source", "primary"),
        }
        for h in hits
    ]


def answer_stream(query: str, top_k_sections: int = 5,
                  history: list[dict] | None = None) -> Iterator[dict]:
    """NDJSON 스트림용 이벤트 제너레이터.

    이벤트 종류:
      - meta     : {tickers, intent, cached, lazy_compiled, related_tags, related_tickers, classified_by}
      - sections : {hits: [...]}
      - answer   : {text}                (캐시 히트 시 전체 답변)
      - token    : {text}                (cold 경로 토큰 단위)
      - done     : {latency_ms, cached}

    `history` 가 주어지면 multi-turn 대화로 처리: latest `query` 로 검색·라우팅
    을 하되 LLM 프롬프트에 이전 턴들을 포함한다. 이 경로는 답변 캐시를 우회한다
    (대화 맥락이 다르면 같은 last-query 라도 답이 달라야 하므로).
    """
    started = time.time()
    has_history = bool(history)

    # 1) 통합 Query Understanding (1회 LLM, fast-path는 LLM 생략)
    plan: QueryPlan = understand(query)
    tickers = plan.tickers
    intent = plan.intent

    # 1-bis) multi-turn carryover — latest query 로 종목이 안 잡혔다면 직전 user 턴 참조
    carry_resolved: list[dict] = []
    if not tickers and has_history:
        carry_resolved = _carryover_tickers_from_history(history)
        if carry_resolved:
            tickers = [c["ticker"] for c in carry_resolved]

    primary = tickers[0] if tickers else None

    # 2) 4계층 캐시 조회 (L1 normhash → L2 ticker+intent → L3 tag+intent)
    #    history 가 있으면 캐시 우회 — 대화 맥락이 다른데 같은 답변을 주면 안 됨
    cached_entry = None
    if not has_history:
        cached_entry = cache.get_answer(
            primary, intent,
            query=query,
            tags=plan.related_tags,
        )
    if cached_entry:
        yield {
            "type": "meta",
            "tickers": tickers,
            "intent": intent,
            "cached": True,
            "cache_level": cached_entry.get("cache_level"),
            "lazy_compiled": [],
            "related_tags": plan.related_tags,
            "related_tickers": plan.related_tickers,
            "classified_by": plan.classified_by,
        }
        yield {
            "type": "sections",
            "hits": cached_entry.get("section_hits", []),
        }
        yield {"type": "answer", "text": cached_entry["answer"]}
        yield {
            "type": "done",
            "latency_ms": int((time.time() - started) * 1000),
            "cached": True,
        }
        return

    # 3) cold path — lazy compile이 필요하면 수행 (related_tickers 포함)
    all_candidate_tickers = list(dict.fromkeys([*tickers, *plan.related_tickers]))
    lazy_done = _ensure_compiled(all_candidate_tickers)

    # 4) 섹션 검색 — tickers 가 없어도 related_tags 로 fallback
    section_hits: list[SectionHit] = search(
        query=query,
        tickers=tickers,
        section_types=plan.sections,
        top_k=top_k_sections,
        expand_tags=plan.related_tags,
        expand_tickers=plan.related_tickers,
    ) if (tickers or plan.related_tickers or plan.related_tags) else []

    # 5) 종목 미지정 시 hits 에서 effective_tickers 도출 (태그 폴백)
    if not tickers:
        derived = []
        for h in section_hits:
            if h.ticker not in derived:
                derived.append(h.ticker)
        effective_tickers = derived[:3] or plan.related_tickers
    else:
        effective_tickers = tickers

    yield {
        "type": "meta",
        "tickers": effective_tickers,
        "intent": intent,
        "cached": False,
        "lazy_compiled": lazy_done,
        "related_tags": plan.related_tags,
        "related_tickers": plan.related_tickers,
        "classified_by": plan.classified_by,
        "tag_fallback": bool(not tickers and effective_tickers),
        "history_carryover": [c["ticker"] for c in carry_resolved],
    }
    yield {
        "type": "sections",
        "hits": _section_hit_dicts(section_hits),
    }

    # 6) 종목도 없고 hits도 비었으면 최종 거부
    if not effective_tickers and not section_hits:
        msg = ("질의에서 종목을 특정하지 못했습니다. "
               "종목명 또는 6자리 코드를 포함하거나, 테마 키워드(예: HBM, SMR)로 다시 질문해 주세요.")
        yield {"type": "answer", "text": msg}
        yield {
            "type": "done",
            "latency_ms": int((time.time() - started) * 1000),
            "cached": False,
        }
        return

    # 7) 컨텍스트 조립 + 스트리밍 답변
    events = _fetch_event_headlines(effective_tickers, limit=5)
    ctx = compose_context(effective_tickers, section_hits, events)
    if not tickers:
        ctx = (f"## 참고: 질의에서 특정 종목이 잡히지 않아 태그 "
               f"{plan.related_tags or ['관련']} 로 후보를 찾았습니다.\n\n" + ctx)
    history_block = _format_history(history)
    if history_block:
        user_msg = f"대화 기록:\n{history_block}\n\n질문: {query}\n\n{ctx}"
    else:
        user_msg = f"질문: {query}\n\n{ctx}"

    full_answer = ""
    try:
        for chunk in chat_stream(
            prompt_id="answer_compose_v1",
            system=ANSWER_SYSTEM,
            user=user_msg,
            max_tokens=700,
        ):
            if chunk:
                full_answer += chunk
                yield {"type": "token", "text": chunk}
    except BudgetExceeded as e:
        msg = (f"\n\n🛑 일일 사용 상한에 도달해 LLM 호출이 자동 차단되었습니다. "
               f"관리자에게 문의하거나 내일 다시 시도해 주세요.\n({e})")
        full_answer += msg
        yield {"type": "token", "text": msg}
    except Exception as e:
        err = f"\n[답변 생성 실패: {e}]"
        full_answer += err
        yield {"type": "token", "text": err}

    # 7) 4계층 캐시 저장 (L1 normhash + L2 ticker+intent + L3 tag+intent)
    #    history 동반 호출은 같은 last-query 라도 답이 달라야 하므로 캐시에 넣지 않음
    if full_answer.strip() and not has_history:
        cache.set_answer(
            primary, intent,
            {
                "answer": full_answer,
                "section_hits": _section_hit_dicts(section_hits),
            },
            query=query,
            tags=plan.related_tags,
        )

    yield {
        "type": "done",
        "latency_ms": int((time.time() - started) * 1000),
        "cached": False,
    }
