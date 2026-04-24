"""Intent classifier + section shortlist policy.

프로덕션 SKILL.md 의 PoC 등가: ticker 단위로 intent→preferred_sections 를 Python dict로 표현.
규칙 기반 + (fallback) LLM 으로 intent를 결정한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .llm_gateway import chat_json


Intent = str
# latest_issue | sns_buzz | business_model | finance | relations | theme | generic


# ticker-agnostic 기본 매핑 (SKILL.md.supported_intents 등가)
# 새 섹션: profile / latest_events / sns_events / business / finance / relations / theme
DEFAULT_INTENT_TO_SECTIONS: dict[Intent, list[str]] = {
    "latest_issue":   ["latest_events", "business"],
    "sns_buzz":       ["sns_events", "latest_events"],
    "business_model": ["profile", "business"],
    "finance":        ["finance", "latest_events"],
    "relations":      ["relations", "business"],
    "theme":          ["theme", "business"],
    "generic":        ["profile", "latest_events", "business"],
}


# freshness SLA (초 단위)
FRESHNESS_SLA: dict[Intent, int] = {
    "latest_issue":   15 * 60,
    "sns_buzz":       15 * 60,
    "business_model": 24 * 3600,
    "finance":        24 * 3600,
    "relations":      24 * 3600,
    "theme":          24 * 3600,
    "generic":        30 * 60,
}


# ---- 규칙 ----
# 주의: 리스크 관련 질의(리스크/위험/하방)는 finance 섹션에 귀속
#      (별도 risk 섹션이 없음 → 재무 가이던스·경쟁 관계로 커버)
_RULES: list[tuple[re.Pattern, Intent]] = [
    (re.compile(r"(종토방|종목토론|리딩방|커뮤니티|SNS|트윗|텔레그램|분위기|분위기어때)"), "sns_buzz"),
    (re.compile(r"(오늘|지금|방금|최근|왜|급등|급락|특징주|이슈|뉴스|공시)"), "latest_issue"),
    (re.compile(r"(실적|매출|영업이익|PER|PBR|EPS|배당|재무|가이던스|수익성|리스크|위험|하방|부담)"), "finance"),
    (re.compile(r"(경쟁|협력|관련주|밸류체인|고객|납품|자회사|계열)"), "relations"),
    (re.compile(r"(테마|섹터|업종|정책|사이클)"), "theme"),
    (re.compile(r"(뭐하는|무슨 회사|어떤 회사|사업|비즈니스|제품|개요|소개)"), "business_model"),
]


INTENT_PROMPT = """사용자 질의의 의도를 아래 라벨 중 하나로 분류하라.
latest_issue | sns_buzz | business_model | finance | relations | theme | generic

latest_issue   : 최근 주가 변동·뉴스·공시·이슈 설명 요청
sns_buzz       : SNS·종토방·커뮤니티 분위기/여론
business_model : 회사가 무엇을 하는지 (개요·사업·제품)
finance        : 실적·재무·영업이익·가이던스·리스크·수익성
relations      : 경쟁사·협력사·관련주·자회사·계열
theme          : 테마·섹터·업종·정책 카테고리 귀속
generic        : 위 어디에도 명확히 속하지 않음

JSON으로만 응답: {"intent":"latest_issue"}"""


@dataclass
class RoutedQuery:
    intent: Intent
    sections: list[str]
    freshness_sla_sec: int
    classified_by: str  # "rule" | "llm"


def classify_rule(query: str) -> Intent | None:
    for pat, intent in _RULES:
        if pat.search(query):
            return intent
    return None


def classify_llm(query: str) -> Intent:
    try:
        out = chat_json(
            prompt_id="intent_classify_v1",
            system=INTENT_PROMPT,
            user=query,
            schema_hint='{"intent":"latest_issue|business_model|fundamentals|risk|relation|generic"}',
        )
        intent = out.get("intent", "generic")
        if intent not in DEFAULT_INTENT_TO_SECTIONS:
            intent = "generic"
        return intent
    except Exception:
        return "generic"


def route(query: str) -> RoutedQuery:
    intent = classify_rule(query)
    source = "rule"
    if intent is None:
        intent = classify_llm(query)
        source = "llm"
    return RoutedQuery(
        intent=intent,
        sections=DEFAULT_INTENT_TO_SECTIONS[intent],
        freshness_sla_sec=FRESHNESS_SLA[intent],
        classified_by=source,
    )


if __name__ == "__main__":
    for q in ["삼성전자 오늘 왜 올랐어?",
              "삼성전자 종토방 분위기 어때",
              "더존비즈온 뭐하는 회사야",
              "셀트리온 실적 가이던스",
              "셀트리온 리스크 뭐 있어",
              "SK하이닉스 경쟁사 알려줘",
              "우리기술 SMR 테마",
              "한국석유 PER 얼마",
              "리튬포어스 ???"]:
        print(q, "->", route(q))
