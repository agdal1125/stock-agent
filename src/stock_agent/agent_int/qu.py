"""Query Understanding — resolver + router를 1회 LLM 호출로 통합.

P5 목표:
  - 종목 · 의도 · 연관 태그 · 연관 종목을 한 번에 추출
  - 규칙 기반 fast-path (ticker code/alias 확정 + intent rule match) 에서는 LLM 미호출
  - LLM 경로는 `gpt-5.4-mini` 한 번만 호출 (입력 ~400 / 출력 ~120 토큰)

P5.5 가 쓰는 `related_tags` / `related_tickers` 를 여기서 생산한다 —
hybrid_search 에서 태그 확장·wikilink traversal 에 쓰인다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..db import tx
from ..entity.resolver import ResolveHit, master, resolve
from .llm_gateway import chat_json
from .router import (
    DEFAULT_INTENT_TO_SECTIONS,
    FRESHNESS_SLA,
    classify_rule,
)


QU_SYSTEM = """당신은 한국 주식 질의 의도 해석기다. 다음 JSON으로만 응답한다:
{"tickers":["005930"], "intent":"finance",
 "related_tags":["HBM"], "related_tickers":["000660"]}

필수 키:
- tickers: 질의에서 확실히 특정되는 6자리 코드 배열 (없으면 [])
- intent: 아래 중 하나
    latest_issue | sns_buzz | business_model | finance | relations | theme | generic
- related_tags: 이 질의를 뒷받침할 만한 검색 태그 (최대 5개, 한국어/영문 키워드)
- related_tickers: 함께 고려하면 유용한 다른 6자리 코드 (최대 3개)

의도 라벨 정의:
  latest_issue   : 최근 주가 변동/뉴스/공시 (왜/지금/최근/이슈)
  sns_buzz       : SNS·종토방·커뮤니티 여론/분위기
  business_model : 회사가 무엇을 하는지 (개요·제품·서비스)
  finance        : 실적·재무·영업이익·가이던스·리스크
  relations      : 경쟁사·협력사·관련주·자회사
  theme          : 테마·섹터·업종·정책 카테고리
  generic        : 위에 명확히 속하지 않음

규칙:
- 임의로 코드를 만들어내지 말고, 제공된 ticker_master에 있는 코드만 사용.
- ticker가 불명확하면 tickers=[] 로 반환 (추측 금지).
- related_tags는 섹터명·키워드(HBM, SMR, 배당, 바이오시밀러 등) 만, 완전문 문장 금지.
"""


@dataclass
class QueryPlan:
    tickers: list[str]
    intent: str
    sections: list[str]
    freshness_sla_sec: int
    related_tags: list[str] = field(default_factory=list)
    related_tickers: list[str] = field(default_factory=list)
    classified_by: str = "rule"      # rule_fast | llm_unified | rule_fallback
    resolved: list[dict] = field(default_factory=list)
    ticker_names: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tickers": self.tickers,
            "intent": self.intent,
            "sections": self.sections,
            "freshness_sla_sec": self.freshness_sla_sec,
            "related_tags": self.related_tags,
            "related_tickers": self.related_tickers,
            "classified_by": self.classified_by,
            "resolved": self.resolved,
        }


def _resolved_to_dicts(hits: list[ResolveHit]) -> list[dict]:
    return [
        {
            "ticker": h.ticker.code,
            "name_ko": h.ticker.name_ko,
            "score": h.score,
            "matched_via": h.matched_via,
        }
        for h in hits
    ]


def _known_codes() -> set[str]:
    return {t.code for t in master()}


def _known_names() -> dict[str, str]:
    return {t.code: t.name_ko for t in master()}


def _known_tags(limit: int = 60) -> list[str]:
    """DB에 존재하는 태그 상위 N개 — LLM 에게 어휘 힌트로 제공 가능."""
    try:
        with tx() as conn:
            rows = conn.execute(
                """SELECT tag, COUNT(*) n FROM section_tag
                   GROUP BY tag ORDER BY n DESC, tag LIMIT ?""",
                (limit,),
            ).fetchall()
        return [r["tag"] for r in rows]
    except Exception:
        return []


def _llm_plan(query: str) -> dict:
    """LLM 1회 호출 — JSON 스키마로 반환. 실패 시 {} 반환."""
    hint_tags = _known_tags()
    tickers_hint = ", ".join(
        f"{t.code}({t.name_ko})" for t in master()
    )
    user = (
        f"질의: {query}\n\n"
        f"[ticker_master 힌트] {tickers_hint}\n"
        f"[자주 쓰는 태그] {', '.join(hint_tags) if hint_tags else '(없음)'}\n"
    )
    try:
        out = chat_json(
            prompt_id="qu_unified_v1",
            system=QU_SYSTEM,
            user=user,
            schema_hint=(
                '{"tickers":["005930"],"intent":"finance",'
                '"related_tags":["HBM"],"related_tickers":["000660"]}'
            ),
        )
    except Exception:
        return {}
    if not isinstance(out, dict):
        return {}
    return out


def _sanitize(out: dict) -> dict:
    """LLM 반환을 방어적으로 정규화."""
    codes = _known_codes()

    tickers = out.get("tickers") or ([out["ticker"]] if out.get("ticker") else [])
    tickers = [str(t).zfill(6)[-6:] for t in tickers if t]
    tickers = [t for t in tickers if t in codes]

    intent = out.get("intent") or "generic"
    if intent not in DEFAULT_INTENT_TO_SECTIONS:
        intent = "generic"

    related_tags = out.get("related_tags") or []
    related_tags = [str(t).strip() for t in related_tags if t]
    # 최대 5개, 중복 제거
    related_tags = list(dict.fromkeys(related_tags))[:5]

    related_tickers = out.get("related_tickers") or []
    related_tickers = [str(t).zfill(6)[-6:] for t in related_tickers if t]
    related_tickers = [t for t in related_tickers if t in codes and t not in tickers][:3]

    return {
        "tickers": tickers,
        "intent": intent,
        "related_tags": related_tags,
        "related_tickers": related_tickers,
    }


def understand(query: str, *, allow_llm: bool = True) -> QueryPlan:
    """질의 이해 진입점.

    - 1) resolver 로 ticker 후보 추출
    - 2) 규칙 intent 분류
    - 3) (ticker 확정 + intent 확정) 이면 LLM 생략 (fast-path)
    - 4) 그 외 1회 LLM 호출로 tickers/intent/related_* 통합 획득
    """
    hits = resolve(query, top_k=3)
    resolved = _resolved_to_dicts(hits)
    rule_tickers = [h.ticker.code for h in hits if h.score >= 80.0]
    rule_intent = classify_rule(query)
    names_map = _known_names()

    has_strong_ticker = any(h.score >= 95.0 for h in hits)
    if rule_tickers and has_strong_ticker and rule_intent:
        return QueryPlan(
            tickers=rule_tickers,
            intent=rule_intent,
            sections=DEFAULT_INTENT_TO_SECTIONS[rule_intent],
            freshness_sla_sec=FRESHNESS_SLA[rule_intent],
            related_tags=[],
            related_tickers=[],
            classified_by="rule_fast",
            resolved=resolved,
            ticker_names={t: names_map.get(t, t) for t in rule_tickers},
        )

    if not allow_llm:
        intent = rule_intent or "generic"
        return QueryPlan(
            tickers=rule_tickers,
            intent=intent,
            sections=DEFAULT_INTENT_TO_SECTIONS[intent],
            freshness_sla_sec=FRESHNESS_SLA[intent],
            classified_by="rule_fallback",
            resolved=resolved,
            ticker_names={t: names_map.get(t, t) for t in rule_tickers},
        )

    raw = _llm_plan(query)
    s = _sanitize(raw)
    # 병합: rule 기반 ticker 가 확보돼 있으면 앞에 두고, LLM 결과 보강
    merged_tickers = list(dict.fromkeys([*rule_tickers, *s["tickers"]]))
    intent = s["intent"] if s["intent"] != "generic" else (rule_intent or s["intent"])
    all_tickers_for_names = [*merged_tickers, *s["related_tickers"]]
    return QueryPlan(
        tickers=merged_tickers,
        intent=intent,
        sections=DEFAULT_INTENT_TO_SECTIONS[intent],
        freshness_sla_sec=FRESHNESS_SLA[intent],
        related_tags=s["related_tags"],
        related_tickers=s["related_tickers"],
        classified_by="llm_unified",
        resolved=resolved,
        ticker_names={t: names_map.get(t, t) for t in all_tickers_for_names},
    )


if __name__ == "__main__":
    for q in [
        "삼전 최근 영업이익 얼마야?",
        "하이닉스 소식",
        "HBM 관련 종목",
        "리튬포어스 주요 제품/서비스가 뭐야?",
        "우리기술 뭐하는 기업?",
        "파운드리 특징주",
    ]:
        p = understand(q, allow_llm=False)  # rule-only in __main__
        print(q, "->", p.classified_by, p.tickers, p.intent, p.related_tags)
