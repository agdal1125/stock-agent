"""4계층 답변 캐시 — diskcache 기반 (P6).

계층 정의:
  L1  normhash(query)       → 같은 질문(정규화 후 해시) = 밀리초 응답
  L2  (primary_ticker, intent)
                             → 같은 종목·의도의 최근 답변 재사용
  L3  (sorted tag tuple, intent)
                             → 표현은 달라도 같은 태그 집합 + 의도면 재사용
  L4  cold                   → LLM 호출 (캐시 미스)

TTL 은 intent 성격에 맞춰 차등:
  latest_issue / sns_buzz  : 15분
  business / finance / relations / theme : 24시간
  generic                  : 30분
L1 은 L2/L3 보다 짧은 TTL 을 공유한다 (정확히 같은 입력이므로).
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Iterable

from diskcache import Cache

from ..config import CFG


INTENT_TTL = {
    "latest_issue":   15 * 60,
    "sns_buzz":       15 * 60,
    "business_model": 24 * 3600,
    "finance":        24 * 3600,
    "relations":      24 * 3600,
    "theme":          24 * 3600,
    "generic":        30 * 60,
}
DEFAULT_TTL = 15 * 60


_answers_dir = CFG.cache_dir / "answers"
_answers_dir.mkdir(parents=True, exist_ok=True)
_cache = Cache(str(_answers_dir))

_STATS = {"hits_L1": 0, "hits_L2": 0, "hits_L3": 0, "miss": 0, "sets": 0}


# ---------------------------------------------------------------------------
# 정규화·키 생성
# ---------------------------------------------------------------------------

_NORMALIZE_RE = re.compile(r"\s+")
_STRIP_PUNCT_RE = re.compile(r"[\.\?!,;:'\"\(\)\[\]\{\}/\\~`]")


def normalize_query(q: str) -> str:
    """같은 뜻의 표현을 같은 문자열로 수렴시키는 얕은 정규화."""
    s = unicodedata.normalize("NFKC", q).strip().lower()
    s = _STRIP_PUNCT_RE.sub(" ", s)
    s = _NORMALIZE_RE.sub(" ", s).strip()
    return s


def _norm_hash(q: str) -> str:
    n = normalize_query(q)
    return hashlib.sha256(n.encode("utf-8")).hexdigest()[:20]


def _tag_tuple_key(tags: Iterable[str]) -> str:
    cleaned = [t.strip().lower() for t in tags if t and t.strip()]
    return "|".join(sorted(set(cleaned)))


def _l1_key(query: str) -> str:
    return f"ans:L1:{_norm_hash(query)}"


def _l2_key(ticker: str | None, intent: str) -> str:
    return f"ans:L2:{ticker or 'NONE'}:{intent}"


def _l3_key(tags: Iterable[str], intent: str) -> str:
    tagkey = _tag_tuple_key(tags)
    if not tagkey:
        return ""
    return f"ans:L3:{tagkey}:{intent}"


# ---------------------------------------------------------------------------
# 조회·저장
# ---------------------------------------------------------------------------

def get_answer(
    ticker: str | None,
    intent: str,
    *,
    query: str | None = None,
    tags: Iterable[str] | None = None,
) -> dict | None:
    """3단 캐시 조회. 히트 시 value dict 에 'cache_level' 추가해 반환.

    호출처 호환을 위해 `ticker`+`intent` 만 주면 L2 만 조회한다 (기존 동작).
    query/tags 를 함께 주면 L1→L2→L3 순으로 탐색한다.
    """
    # L1 — 같은 질문 정규화 해시
    if query:
        v = _cache.get(_l1_key(query))
        if v:
            _STATS["hits_L1"] += 1
            v = dict(v)
            v["cache_level"] = "L1"
            return v

    # L2 — (ticker, intent)
    if ticker:
        v = _cache.get(_l2_key(ticker, intent))
        if v:
            _STATS["hits_L2"] += 1
            v = dict(v)
            v["cache_level"] = "L2"
            return v

    # L3 — (tag tuple, intent)
    if tags:
        k3 = _l3_key(tags, intent)
        if k3:
            v = _cache.get(k3)
            if v:
                _STATS["hits_L3"] += 1
                v = dict(v)
                v["cache_level"] = "L3"
                return v

    _STATS["miss"] += 1
    return None


def set_answer(
    ticker: str | None,
    intent: str,
    value: dict,
    *,
    query: str | None = None,
    tags: Iterable[str] | None = None,
) -> None:
    ttl = INTENT_TTL.get(intent, DEFAULT_TTL)
    stored_any = False

    if query:
        _cache.set(_l1_key(query), value, expire=min(ttl, 30 * 60))
        stored_any = True

    if ticker:
        _cache.set(_l2_key(ticker, intent), value, expire=ttl)
        stored_any = True

    if tags:
        k3 = _l3_key(tags, intent)
        if k3:
            _cache.set(k3, value, expire=ttl)
            stored_any = True

    if stored_any:
        _STATS["sets"] += 1


# ---------------------------------------------------------------------------
# 유지보수 · 통계
# ---------------------------------------------------------------------------

def invalidate_ticker(ticker: str) -> int:
    """해당 티커의 L2 캐시 전부 삭제. 삭제된 항목 수 반환.
    (L1/L3 는 티커 키가 없어 선별 삭제 불가 — TTL 만료에 위임)."""
    n = 0
    for intent in INTENT_TTL:
        if _cache.delete(_l2_key(ticker, intent)):
            n += 1
    return n


def stats() -> dict[str, Any]:
    total = _STATS["hits_L1"] + _STATS["hits_L2"] + _STATS["hits_L3"] + _STATS["miss"]
    hit_rate = (
        (_STATS["hits_L1"] + _STATS["hits_L2"] + _STATS["hits_L3"]) / total
        if total else 0.0
    )
    return {
        "path": str(_answers_dir),
        "size": len(_cache),
        "hits_L1": _STATS["hits_L1"],
        "hits_L2": _STATS["hits_L2"],
        "hits_L3": _STATS["hits_L3"],
        "miss": _STATS["miss"],
        "sets": _STATS["sets"],
        "hit_rate": round(hit_rate, 4),
    }


def clear() -> None:
    """디버그용 — 캐시 전체 비우기."""
    _cache.clear()
    for k in list(_STATS):
        _STATS[k] = 0
