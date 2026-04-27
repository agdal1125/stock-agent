"""Entity resolver — 사용자 질의 문자열에서 종목 코드를 해석.

프로덕션 등가: EPDIW_STK_IEM 마스터 + A400의 종목명 정규화 로직.
PoC는 tickers.csv를 로드해 3단 매칭: ticker_literal → alias_exact → fuzzy.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from rapidfuzz import fuzz, process

from ..config import CFG


TICKER_CODE_RE = re.compile(r"\b(\d{6})\b")


@dataclass(frozen=True)
class Ticker:
    code: str
    name_ko: str
    name_en: str
    aliases: tuple[str, ...]
    market: str
    sector: str
    asset_type: str = "stock"


@dataclass(frozen=True)
class ResolveHit:
    ticker: Ticker
    score: float            # 0~100
    matched_via: str        # "code" | "alias_exact" | "fuzzy"
    matched_span: str       # 질의에서 실제 매칭된 부분 문자열


def _load_master() -> list[Ticker]:
    path = CFG.seed_dir / "tickers.csv"
    out: list[Ticker] = []
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            aliases = tuple(a.strip() for a in row["aliases"].split("|") if a.strip())
            out.append(
                Ticker(
                    code=row["ticker"],
                    name_ko=row["name_ko"],
                    name_en=row.get("name_en", ""),
                    aliases=aliases,
                    market=row["market"],
                    sector=row["sector"],
                    asset_type=(row.get("asset_type") or "stock").strip().lower(),
                )
            )
    return out


@lru_cache(maxsize=1)
def master() -> tuple[Ticker, ...]:
    return tuple(_load_master())


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, Ticker]:
    idx: dict[str, Ticker] = {}
    for t in master():
        # canonical
        idx[t.name_ko] = t
        if t.name_en:
            idx[t.name_en] = t
        for a in t.aliases:
            idx[a] = t
    return idx


def resolve(query: str, top_k: int = 3, fuzzy_min: int = 78) -> list[ResolveHit]:
    """질의에서 후보 종목들을 반환 (score 내림차순).

    매칭 우선순위:
      1. 6자리 숫자 코드 직접 매칭 (score=100)
      2. alias/이름 exact substring (score=95)
      3. rapidfuzz partial_ratio (score>=fuzzy_min)
    """
    hits: list[ResolveHit] = []
    seen: set[str] = set()
    q = query.strip()

    # 1) ticker code 직접
    for m in TICKER_CODE_RE.finditer(q):
        code = m.group(1)
        for t in master():
            if t.code == code and code not in seen:
                hits.append(ResolveHit(t, 100.0, "code", code))
                seen.add(code)

    # 2) alias / name exact substring
    for alias, t in _alias_index().items():
        if alias and alias in q and t.code not in seen:
            hits.append(ResolveHit(t, 95.0, "alias_exact", alias))
            seen.add(t.code)

    # 3) fuzzy (초성/오타 흡수)
    if len(hits) == 0:
        choices = list(_alias_index().keys())
        matches = process.extract(q, choices, scorer=fuzz.partial_ratio, limit=top_k * 2)
        for alias, score, _ in matches:
            if score < fuzzy_min:
                continue
            t = _alias_index()[alias]
            if t.code in seen:
                continue
            hits.append(ResolveHit(t, float(score), "fuzzy", alias))
            seen.add(t.code)

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:top_k]


def best(query: str, min_score: float = 80.0) -> ResolveHit | None:
    hits = resolve(query)
    if not hits:
        return None
    return hits[0] if hits[0].score >= min_score else None


def get_by_code(code: str) -> Ticker | None:
    for t in master():
        if t.code == code:
            return t
    return None


if __name__ == "__main__":
    for q in [
        "삼성전자 오늘 왜 올랐어?",
        "SK하이닉스 HBM",
        "005940 AI 시황",
        "셀트리온과 삼성전자 비교",
        "삼전 급등 이유",  # 축약
        "JYP 투어",
        "포어스 뭐하는데?",  # 부분 alias
    ]:
        hits = resolve(q)
        print(q, "->", [(h.ticker.code, h.ticker.name_ko, h.score, h.matched_via) for h in hits])
