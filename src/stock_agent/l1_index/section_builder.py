"""L0 canonical → wiki/*.md 파일 작성 + SQLite 인덱스(section_doc) 업데이트.

설계:
  - 본체는 **파일** (wiki/tickers/{ticker}/{NN}_{section}.md)
  - section_doc 테이블은 검색 가속용 인덱스 (file_path + content_hash + embedding)
  - content_hash가 바뀌면 embedding=NULL로 리셋해 재임베딩 유도

글로벌 파일:
  - wiki/AGENTS.md   — 전사 공통 schema·정책 (정적)
  - wiki/index.md    — 종목 카탈로그 (컴파일 시 재생성)
  - wiki/log.md      — 컴파일·ingest 로그 (append)
  - wiki/tickers/{t}/SKILL.md — 종목별 정책 파일
"""
from __future__ import annotations

import json as _json
import re
from datetime import datetime, timezone

from jinja2 import Template

from ..db import tx
from .wiki_loader import (
    WIKI_ROOT, hash_body, relpath_from_data, ticker_dir, wiki_root,
)


# ----- Obsidian-style auto tagging / linking ---------------------------------

# 섹터명 자체는 tag로 그대로 사용 (ticker_master.sector).
# 아래는 claim 본문에서 감지할 도메인 키워드 → 태그 매핑.
KEYWORD_TAGS: dict[str, str] = {
    "HBM": "HBM",
    "HBM4": "HBM",
    "HBM3": "HBM",
    "DRAM": "DRAM",
    "NAND": "NAND",
    "SSD": "SSD",
    "파운드리": "파운드리",
    "3nm": "3nm",
    "GAA": "GAA",
    "바이오시밀러": "바이오시밀러",
    "스텔라라": "바이오시밀러",
    "임상": "임상",
    "SMR": "SMR",
    "원자력": "원자력",
    "원전": "원자력",
    "SaaS": "SaaS",
    "클라우드": "클라우드",
    "ERP": "ERP",
    "특징주": "특징주",
    "급등": "급등락",
    "급락": "급등락",
    "배당": "배당",
    "자기주식": "자사주",
    "자사주": "자사주",
    "리튬": "2차전지",
    "2차전지": "2차전지",
    "광케이블": "광케이블",
    "데이터센터": "데이터센터",
    "AI ": "AI",
    "K-Pop": "K-Pop",
    "월드투어": "K-Pop",
    "아스팔트": "아스팔트",
    "SOC": "SOC",
    "유가": "유가",
    "공시": "공시",
    "실적": "실적",
    "환율": "환율",
    "수주": "수주",
}


def _build_alias_map(conn) -> list[tuple[str, str, str]]:
    """(alias, ticker, name_ko) 리스트를 길이 내림차순으로 반환.
    길이 순 정렬은 greedy 치환 시 'SK하이닉스'가 '하이닉스'보다 먼저 매치되도록."""
    out: list[tuple[str, str, str]] = []
    rows = conn.execute("SELECT ticker, name_ko, aliases_json FROM ticker_master").fetchall()
    for r in rows:
        aliases = _json.loads(r["aliases_json"] or "[]")
        seen = set()
        for a in [r["name_ko"]] + aliases:
            a = (a or "").strip()
            if not a or a in seen or len(a) < 2:
                continue
            seen.add(a)
            out.append((a, r["ticker"], r["name_ko"]))
    out.sort(key=lambda x: -len(x[0]))
    return out


def _linkify_claim(text: str, self_ticker: str,
                   alias_map: list[tuple[str, str, str]],
                   linked_targets: set[tuple[str, str | None]]) -> str:
    """claim 본문에서 다른 종목 alias를 [[code|display]] 로 치환.
    self_ticker는 건너뜀 (자기 참조 방지). 첫 매칭 1회만 치환 (가독성)."""
    replaced_targets: set[str] = set()
    for alias, target, name_ko in alias_map:
        if target == self_ticker:
            continue
        if target in replaced_targets:
            continue
        # 단어 경계 기반 대체 — 한글 조사에 유연하게 대응하기 위해 lookahead 최소화
        pattern = re.compile(re.escape(alias))
        new_text, n = pattern.subn(f"[[{target}|{alias}]]", text, count=1)
        if n > 0:
            text = new_text
            replaced_targets.add(target)
            linked_targets.add((target, None))
    return text


def _extract_tags(text: str, section_type: str, sector: str | None) -> list[str]:
    tags: set[str] = {section_type}
    if sector:
        tags.add(sector)
    low = text
    for kw, tag in KEYWORD_TAGS.items():
        if kw in low:
            tags.add(tag)
    # 과다 태깅 방지
    return sorted(tags)[:10]


SECTION_TYPES = (
    "profile",           # 00 — 종목 3줄 소개
    "latest_events",     # 10 — 최근 이슈 (뉴스·공시)
    "sns_events",        # 11 — SNS 및 종토방 이슈
    "business",          # 20 — 사업 개요, 주요 상품
    "finance",           # 30 — 재무 상태, 영업이익, 실적, 가이던스
    "relations",         # 40 — 연관 기업·Entity
    "theme",             # 50 — 테마·업종·섹터
)
SECTION_ORDER = {
    "profile":       "00",
    "latest_events": "10",
    "sns_events":    "11",
    "business":      "20",
    "finance":       "30",
    "relations":     "40",
    "theme":         "50",
}
SECTION_DESCRIPTIONS = {
    "profile":       "종목 3줄 소개",
    "latest_events": "최근 이슈 (뉴스·공시)",
    "sns_events":    "SNS 및 종토방 이슈",
    "business":      "사업 개요·주요 상품",
    "finance":       "재무·실적·가이던스",
    "relations":     "연관 기업·Entity",
    "theme":         "테마·업종·섹터",
}
# claim_extract 대상 섹션 (event 기반 섹션은 제외)
CLAIM_SECTION_TYPES = ("profile", "business", "finance", "relations", "theme")


# --- templates ----------------------------------------------------------------

_TMPL_STATIC = Template("""---
doc_id: "{{ ticker }}:{{ section_type }}"
ticker: "{{ ticker }}"
section_type: {{ section_type }}
name_ko: "{{ name_ko }}"
tags: [{{ tags | join(', ') }}]
updated_at: "{{ updated_at }}"
source: "derived from L0 stock_claim (review_state=approved)"
---

# {{ name_ko }} ({{ ticker }}) — {{ section_type }}

{% if claims %}
{% for c in claims %}- {{ c.linked }}{% if c.source_id %}  _(src:{{ c.source_id }} · conf:{{ '%.2f' % c.confidence }})_{% endif %}
{% endfor %}
{% else %}
_해당 섹션에 승인된 claim이 없습니다._
{% endif %}

{% if related_tickers %}

## 관련 종목
{% for t in related_tickers %}- [[{{ t.code }}|{{ t.name }}]]
{% endfor %}
{% endif %}
""".strip() + "\n")

_TMPL_EVENTS = Template("""---
doc_id: "{{ ticker }}:latest_events"
ticker: "{{ ticker }}"
section_type: latest_events
name_ko: "{{ name_ko }}"
tags: [{{ tags | join(', ') }}]
updated_at: "{{ updated_at }}"
source: "derived from L0 stock_event_timeline (news + disclosure)"
---

# {{ name_ko }} ({{ ticker }}) — 최근 이슈 (뉴스·공시)

{% if events %}
{% for e in events %}- **[{{ e.occurred_at[:16] }}]** `{{ e.event_type }}` — {{ e.linked_headline }}  _(src:{{ e.source_id }} · impact:{{ '%.2f' % e.impact_score }})_
{% endfor %}
{% else %}
_최근 이벤트 없음._
{% endif %}

{% if related_tickers %}

## 관련 종목
{% for t in related_tickers %}- [[{{ t.code }}|{{ t.name }}]]
{% endfor %}
{% endif %}
""".strip() + "\n")

_TMPL_SNS_EVENTS = Template("""---
doc_id: "{{ ticker }}:sns_events"
ticker: "{{ ticker }}"
section_type: sns_events
name_ko: "{{ name_ko }}"
tags: [{{ tags | join(', ') }}]
updated_at: "{{ updated_at }}"
source: "derived from L0 stock_event_timeline (sns)"
---

# {{ name_ko }} ({{ ticker }}) — SNS · 종토방 이슈

{% if events %}
{% for e in events %}- **[{{ e.occurred_at[:16] }}]** `{{ e.event_type }}` — {{ e.linked_headline }}  _(src:{{ e.source_id }} · signal:{{ '%.2f' % e.impact_score }})_
{% endfor %}
{% else %}
_현재 수집된 SNS/종토방 데이터가 없습니다._
{% endif %}

{% if related_tickers %}

## 관련 종목
{% for t in related_tickers %}- [[{{ t.code }}|{{ t.name }}]]
{% endfor %}
{% endif %}
""".strip() + "\n")

_TMPL_FINANCE = Template("""---
doc_id: "{{ ticker }}:finance"
ticker: "{{ ticker }}"
section_type: finance
name_ko: "{{ name_ko }}"
tags: [{{ tags | join(', ') }}]
updated_at: "{{ updated_at }}"
source: "derived from L0 stock_claim (finance) + FnGuide external"
external_refs:
  fnguide_finance: "https://comp.fnguide.com/SVO2/asp/SVD_Finance.asp?pGB=1&gicode=A{{ ticker }}&cID=&MenuYn=Y&ReportGB=&NewMenuID=103&stkGb=701"
---

# {{ name_ko }} ({{ ticker }}) — 재무·실적·가이던스

## 외부 참조
- 🔗 **FnGuide 재무제표**: [comp.fnguide.com · A{{ ticker }}](https://comp.fnguide.com/SVO2/asp/SVD_Finance.asp?pGB=1&gicode=A{{ ticker }}&cID=&MenuYn=Y&ReportGB=&NewMenuID=103&stkGb=701)
  _연결·별도 손익·재무상태·현금흐름 최신 요약 (외부 원천)_

{% if claims %}

## 주요 요약 (L0 claim)
{% for c in claims %}- {{ c.linked }}{% if c.source_id %}  _(src:{{ c.source_id }} · conf:{{ '%.2f' % c.confidence }})_{% endif %}
{% endfor %}
{% else %}

_추출된 finance claim이 아직 없습니다._
{% endif %}

{% if related_tickers %}

## 관련 종목
{% for t in related_tickers %}- [[{{ t.code }}|{{ t.name }}]]
{% endfor %}
{% endif %}
""".strip() + "\n")

_TMPL_THEME = Template("""---
doc_id: "{{ ticker }}:theme"
ticker: "{{ ticker }}"
section_type: theme
name_ko: "{{ name_ko }}"
tags: [{{ tags | join(', ') }}]
updated_at: "{{ updated_at }}"
source: "derived from L0 stock_claim (theme) + ticker_master"
---

# {{ name_ko }} ({{ ticker }}) — 테마·업종·섹터

## 기본 분류
- **시장**: {{ market }}
- **섹터**: {{ sector }}

{% if claims %}

## 주요 테마
{% for c in claims %}- {{ c.linked }}{% if c.source_id %}  _(src:{{ c.source_id }} · conf:{{ '%.2f' % c.confidence }})_{% endif %}
{% endfor %}
{% else %}

_추출된 테마 claim이 아직 없습니다._
{% endif %}

{% if related_tickers %}

## 관련 종목
{% for t in related_tickers %}- [[{{ t.code }}|{{ t.name }}]]
{% endfor %}
{% endif %}
""".strip() + "\n")

_TMPL_SKILL = Template("""---
ticker: "{{ ticker }}"
name_ko: "{{ name_ko }}"
kind: skill
updated_at: "{{ updated_at }}"
---

# {{ name_ko }} ({{ ticker }}) — SKILL

종목 Agent가 이 종목 관련 질의를 처리할 때 참조하는 **정책 파일**입니다.
(router.py 의 intent→sections 기본값과 동일)

## 별칭 (aliases)
{% for a in aliases %}- {{ a }}
{% endfor %}

## 지원 intent → 우선 섹션

| intent | 우선 섹션 |
|---|---|
| latest_issue   | 10_latest_events.md, 20_business.md |
| sns_buzz       | 11_sns_events.md, 10_latest_events.md |
| business_model | 00_profile.md, 20_business.md |
| finance        | 30_finance.md, 10_latest_events.md |
| relations      | 40_relations.md, 20_business.md |
| theme          | 50_theme.md, 20_business.md |
| generic        | 00_profile.md, 10_latest_events.md, 20_business.md |

## Freshness SLA

| 섹션 | 갱신 주기 |
|---|---|
| latest_events  | 15분 (뉴스·공시 ingest 시) |
| sns_events     | 15분 (SNS/종토방 피드 ingest 시) |
| profile / business / finance / relations / theme | 일 1회 배치 |

## 섹션 목록 (링크)
{% for s in sections %}- [`{{ '%02d' % s.order }}_{{ s.name }}.md`]({{ '%02d' % s.order }}_{{ s.name }}.md)
{% endfor %}
""".strip() + "\n")

_TMPL_AGENTS = Template("""---
kind: agents_global_policy
updated_at: "{{ updated_at }}"
---

# NH Stock-Agent — 전역 정책

이 파일은 종목 Agent가 **모든 질의에서 공통적으로 따르는 규칙**을 정의합니다.
Karpathy의 LLM Wiki 제안 중 `AGENTS.md` 개념을 차용했습니다.

## 답변 원칙
- 제공된 Wiki 섹션과 정형 스냅샷만 근거로 사용합니다.
- 각 문장 끝에 근거 섹션을 괄호로 명시합니다 (예: `(latest_events)`, `(finance)`).
- 투자 권유·매수/매도 의견·주가 예측은 하지 않습니다.
- 근거가 부족하면 "현재 확인된 정보로는 답하기 어렵습니다"로 분명히 밝힙니다.

## 섹션과 갱신 주기

| 섹션 | 내용 | 갱신 주기 |
|---|---|---|
| profile       | 종목 3줄 소개         | 일 1회 |
| latest_events | 최근 이슈 (뉴스·공시) | 10~30분 |
| sns_events    | SNS·종토방 이슈       | 15분 |
| business      | 사업 개요·주요 상품    | 일 1회 |
| finance       | 재무·실적·가이던스    | 일 1회 (외부 원천: FnGuide) |
| relations     | 연관 기업·Entity      | 일 1회 |
| theme         | 테마·업종·섹터        | 일 1회 |

## 품질 게이트
- 사람이 검수한 내용만 답변의 근거로 사용됩니다.
- 모든 LLM 호출은 감사 로그로 기록됩니다.
- 비상 차단(`LLM_KILL_SWITCH`) 가 켜지면 외부 LLM 접근을 즉시 멈추고 미리 준비된 안내 응답으로 전환합니다.
""".strip() + "\n")

_TMPL_INDEX = Template("""---
kind: catalog
updated_at: "{{ updated_at }}"
---

# NH Stock-Agent — 종목 Wiki 카탈로그

현재 수집된 종목 목록입니다. 종목명을 클릭하면 해당 종목 페이지로 이동합니다.

| ticker | 종목명 | 섹션 수 | 마지막 갱신 |
|---|---|---|---|
{% for r in rows %}| [{{ r.ticker }}]({{ r.ticker }}/) | {{ r.name_ko }} | {{ r.sec_n }} | {{ r.updated_at[:19] if r.updated_at else '-' }} |
{% endfor %}

> 전역 정책: [AGENTS.md](AGENTS.md) · 로그: [log.md](log.md)
""".strip() + "\n")


# --- rendering helpers --------------------------------------------------------

def _fetch_ticker(conn, ticker: str) -> dict:
    r = conn.execute(
        "SELECT ticker, name_ko, aliases_json FROM ticker_master WHERE ticker=?",
        (ticker,),
    ).fetchone()
    if not r:
        return {"ticker": ticker, "name_ko": ticker, "aliases_json": "[]"}
    return dict(r)


def _fetch_approved_claims(conn, ticker: str, section_type: str) -> list[dict]:
    rows = conn.execute(
        """SELECT claim_text, source_id, confidence
           FROM stock_claim
           WHERE ticker=? AND section_type=? AND review_state='approved'
           ORDER BY confidence DESC LIMIT 20""",
        (ticker, section_type),
    ).fetchall()
    return [dict(r) for r in rows]


def _fetch_events(conn, ticker: str, limit: int = 10,
                  include_types: tuple[str, ...] = ("news", "disclosure")) -> list[dict]:
    placeholders = ",".join(["?"] * len(include_types))
    rows = conn.execute(
        f"""SELECT occurred_at, event_type, headline, source_id, impact_score
            FROM stock_event_timeline
            WHERE ticker=? AND event_type IN ({placeholders})
            ORDER BY occurred_at DESC LIMIT ?""",
        (ticker, *include_types, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _fetch_ticker_meta(conn, ticker: str) -> dict:
    r = conn.execute(
        "SELECT market, sector FROM ticker_master WHERE ticker=?", (ticker,),
    ).fetchone()
    return dict(r) if r else {"market": "-", "sector": "-"}


# --- file writers -------------------------------------------------------------

def _section_filename(section_type: str) -> str:
    return f"{SECTION_ORDER[section_type]}_{section_type}.md"


def _write_file(path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _upsert_section_index(conn, ticker: str, section_type: str,
                          file_path: str, content_hash: str, tokens: int,
                          updated_at: str) -> None:
    existing = conn.execute(
        "SELECT content_hash FROM section_doc WHERE doc_id=?",
        (f"{ticker}:{section_type}",),
    ).fetchone()
    if existing and existing["content_hash"] == content_hash:
        return  # 변경 없음
    conn.execute(
        """INSERT INTO section_doc
           (doc_id, ticker, section_type, file_path, content_hash, tokens, updated_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(doc_id) DO UPDATE SET
             file_path=excluded.file_path,
             content_hash=excluded.content_hash,
             tokens=excluded.tokens,
             updated_at=excluded.updated_at,
             embedding=NULL""",
        (f"{ticker}:{section_type}", ticker, section_type, file_path,
         content_hash, tokens, updated_at),
    )


def compile_ticker(ticker: str) -> int:
    """ticker에 대한 모든 섹션 md를 렌더하여 디스크에 저장 + 인덱스 업데이트.
    리턴: 디스크에 쓴 섹션 개수 (SKILL.md 제외)."""
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tdir = ticker_dir(ticker)
    n = 0

    with tx() as conn:
        tm = _fetch_ticker(conn, ticker)
        name_ko = tm["name_ko"]
        sector = tm.get("sector") if isinstance(tm, dict) else None
        # ticker_master 에 sector가 있으면 가져오기
        s_row = conn.execute(
            "SELECT sector FROM ticker_master WHERE ticker=?", (ticker,)
        ).fetchone()
        if s_row:
            sector = s_row["sector"]

        alias_map = _build_alias_map(conn)
        # 이름 조회 헬퍼 (related_tickers용)
        name_by_code = {t: n for _, t, n in alias_map}

        meta = _fetch_ticker_meta(conn, ticker)

        # 각 섹션 렌더 → 파일 쓰기 → 인덱스/태그/위키링크 업서트
        for stype in SECTION_TYPES:
            doc_id = f"{ticker}:{stype}"
            linked_targets: set[tuple[str, str | None]] = set()
            corpus_text = ""    # 태그 추출용

            if stype == "latest_events":
                events = _fetch_events(conn, ticker,
                                       include_types=("news", "disclosure"))
                for e in events:
                    e["linked_headline"] = _linkify_claim(
                        e["headline"] or "", ticker, alias_map, linked_targets,
                    )
                    corpus_text += " " + (e["headline"] or "")
                related = [{"code": c, "name": name_by_code.get(c, c)}
                           for c, _ in sorted(linked_targets)]
                tags = _extract_tags(corpus_text, "latest_events", sector)
                raw = _TMPL_EVENTS.render(
                    ticker=ticker, name_ko=name_ko, events=events,
                    tags=tags, updated_at=updated_at, related_tickers=related,
                )
            elif stype == "sns_events":
                events = _fetch_events(conn, ticker, include_types=("sns",))
                for e in events:
                    e["linked_headline"] = _linkify_claim(
                        e["headline"] or "", ticker, alias_map, linked_targets,
                    )
                    corpus_text += " " + (e["headline"] or "")
                related = [{"code": c, "name": name_by_code.get(c, c)}
                           for c, _ in sorted(linked_targets)]
                tags = _extract_tags(corpus_text, "sns_events", sector)
                # SNS 성격 태그 강제 추가
                if "SNS" not in tags:
                    tags = sorted(set(tags) | {"SNS"})[:10]
                raw = _TMPL_SNS_EVENTS.render(
                    ticker=ticker, name_ko=name_ko, events=events,
                    tags=tags, updated_at=updated_at, related_tickers=related,
                )
            elif stype == "finance":
                claims = _fetch_approved_claims(conn, ticker, "finance")
                for c in claims:
                    c["linked"] = _linkify_claim(
                        c["claim_text"], ticker, alias_map, linked_targets,
                    )
                    corpus_text += " " + c["claim_text"]
                related = [{"code": c, "name": name_by_code.get(c, c)}
                           for c, _ in sorted(linked_targets)]
                tags = _extract_tags(corpus_text, "finance", sector)
                # 외부 데이터 소스 태그 추가
                if "FnGuide" not in tags:
                    tags = sorted(set(tags) | {"FnGuide"})[:10]
                raw = _TMPL_FINANCE.render(
                    ticker=ticker, name_ko=name_ko, claims=claims,
                    tags=tags, updated_at=updated_at, related_tickers=related,
                )
            elif stype == "theme":
                claims = _fetch_approved_claims(conn, ticker, "theme")
                for c in claims:
                    c["linked"] = _linkify_claim(
                        c["claim_text"], ticker, alias_map, linked_targets,
                    )
                    corpus_text += " " + c["claim_text"]
                related = [{"code": c, "name": name_by_code.get(c, c)}
                           for c, _ in sorted(linked_targets)]
                tags = _extract_tags(corpus_text, "theme", sector)
                raw = _TMPL_THEME.render(
                    ticker=ticker, name_ko=name_ko, claims=claims,
                    market=meta.get("market", "-"),
                    sector=meta.get("sector", "-"),
                    tags=tags, updated_at=updated_at, related_tickers=related,
                )
            else:
                claims = _fetch_approved_claims(conn, ticker, stype)
                for c in claims:
                    c["linked"] = _linkify_claim(
                        c["claim_text"], ticker, alias_map, linked_targets,
                    )
                    corpus_text += " " + c["claim_text"]
                related = [{"code": c, "name": name_by_code.get(c, c)}
                           for c, _ in sorted(linked_targets)]
                tags = _extract_tags(corpus_text, stype, sector)
                raw = _TMPL_STATIC.render(
                    ticker=ticker, name_ko=name_ko, section_type=stype,
                    claims=claims, tags=tags, updated_at=updated_at,
                    related_tickers=related,
                )

            fname = _section_filename(stype)
            fpath = tdir / fname
            _write_file(fpath, raw)

            rel = relpath_from_data(fpath)
            body = raw.split("---", 2)[-1] if raw.startswith("---") else raw
            h = hash_body(body)
            _upsert_section_index(
                conn, ticker, stype, rel, h,
                tokens=len(body.split()), updated_at=updated_at,
            )

            # 태그 재기록 (기존 auto 태그 삭제 후 삽입)
            conn.execute("DELETE FROM section_tag WHERE doc_id=? AND source='auto'",
                         (doc_id,))
            for tag in tags:
                conn.execute(
                    "INSERT OR IGNORE INTO section_tag(doc_id, tag, source) VALUES(?,?,?)",
                    (doc_id, tag, "auto"),
                )
            # 위키링크 재기록
            conn.execute("DELETE FROM section_wikilink WHERE src_doc_id=?", (doc_id,))
            for target, target_sec in linked_targets:
                conn.execute(
                    """INSERT OR IGNORE INTO section_wikilink
                       (src_doc_id, target_ticker, target_section, display_text)
                       VALUES(?,?,?,?)""",
                    (doc_id, target, target_sec, name_by_code.get(target, target)),
                )
            n += 1

        # SKILL.md (정책)
        aliases = _json.loads(tm.get("aliases_json") or "[]")
        skill = _TMPL_SKILL.render(
            ticker=ticker, name_ko=name_ko, aliases=aliases,
            updated_at=updated_at,
            sections=[{"order": int(SECTION_ORDER[s]), "name": s} for s in SECTION_TYPES],
        )
        _write_file(tdir / "SKILL.md", skill)

    return n


# --- global files -------------------------------------------------------------

def ensure_global_files() -> None:
    wiki_root()
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    agents = WIKI_ROOT / "AGENTS.md"
    if not agents.exists():
        _write_file(agents, _TMPL_AGENTS.render(updated_at=updated_at))
    log = WIKI_ROOT / "log.md"
    if not log.exists():
        _write_file(log, "# stock_agent — Wiki Log\n\n")


def regenerate_index() -> None:
    """wiki/index.md를 현재 DB 상태로 재생성."""
    updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows: list[dict] = []
    with tx() as conn:
        for r in conn.execute("""
            SELECT tm.ticker, tm.name_ko, COALESCE(tt.tier,'lazy') AS tier,
                   COUNT(sd.doc_id) AS sec_n, MAX(sd.updated_at) AS updated_at
            FROM ticker_master tm
            LEFT JOIN ticker_tier tt ON tt.ticker = tm.ticker
            LEFT JOIN section_doc sd ON sd.ticker = tm.ticker
            GROUP BY tm.ticker
            ORDER BY tm.ticker
        """).fetchall():
            rows.append(dict(r))
    _write_file(WIKI_ROOT / "index.md",
                _TMPL_INDEX.render(rows=rows, updated_at=updated_at))


def append_log(message: str) -> None:
    WIKI_ROOT.mkdir(parents=True, exist_ok=True)
    log = WIKI_ROOT / "log.md"
    if not log.exists():
        log.write_text("# stock_agent — Wiki Log\n\n", encoding="utf-8")
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with log.open("a", encoding="utf-8") as f:
        f.write(f"- `{ts}` — {message}\n")


if __name__ == "__main__":
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "005930"
    ensure_global_files()
    n = compile_ticker(t)
    regenerate_index()
    append_log(f"manual compile: {t} ({n} sections)")
    print(f"[section_builder] compiled {t}: {n} files")
