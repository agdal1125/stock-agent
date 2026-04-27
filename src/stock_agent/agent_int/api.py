"""REST API (`/api/v1/...`) — JSON 데이터 액세스 엔드포인트.

설계 원칙
---------
- 기존 HTML 위키(`/wiki/...`) 와 `/ask` 답변 합성 엔드포인트는 그대로 두고,
  **순수 데이터 액세스**(외부 클라이언트·대시보드·모바일 앱 용도) 를 별도 surface
  로 분리한다.
- 모든 응답은 `application/json; charset=utf-8` 로 직렬화 (한글 그대로).
- Pydantic 모델로 응답 스키마를 명시 → `/docs` (Swagger UI) 와
  `/openapi.json` 에 자동 노출.
- 데이터 source-of-truth 는 `wiki/*.md` (파일) + `data/canonical.db` (인덱스).
  API 는 두 source 를 join 해서 reading-side 에 친화적인 형태로 가공.
- 쓰기 작업 (claim 승인 등) 은 본 라우터 범위 외 (`scripts/approve_claims` CLI).

엔드포인트 맵 (별도 표기 없으면 GET)
-----------------------------------
- /api/v1/health                                  — 가벼운 상태/메타 (운영용)
- /api/v1/tickers                                 — 종목 + ETF 리스트 (필터)
- /api/v1/tickers/{ticker}                        — 단일 종목 메타
- /api/v1/tickers/{ticker}/sections               — 섹션 목록
- /api/v1/tickers/{ticker}/sections/{section}     — 섹션 본문(JSON)
- /api/v1/tickers/{ticker}/events                 — 이벤트 타임라인
- /api/v1/tickers/{ticker}/backlinks              — 이 종목을 가리키는 섹션
- /api/v1/etfs                                    — ETF 만 필터한 alias
- /api/v1/etfs/{ticker}/constituents              — ETF 구성종목(링크된 + plain)
- /api/v1/tags                                    — 태그 + count (top N)
- /api/v1/tags/{tag}                              — 태그 매칭 섹션
- /api/v1/search                                  — hybrid search (JSON)
- POST /api/v1/chat                               — 챗 답변 (JSON, single/multi-turn)
- POST /api/v1/chat/stream                        — 챗 답변 (NDJSON 토큰 스트림)
- POST /api/v1/refresh/{ticker}                   — 단일 종목 lazy_compile 트리거
"""
from __future__ import annotations

import json as _json_serializer
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Iterable

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..compile.run import lazy_compile
from ..db import tx
from .answer import answer as answer_query, answer_stream as answer_stream_fn
from ..l1_index.hybrid_search import search as hybrid_search
from ..l1_index.section_builder import (
    SECTION_DESCRIPTIONS,
    SECTION_ORDER,
    SECTION_TYPES,
)
from ..l1_index.wiki_loader import (
    ETFS_ROOT,
    TICKERS_ROOT,
    load_section_file,
)


router = APIRouter(prefix="/api/v1", tags=["data"])

_TICKER_RE = re.compile(r"^\d{6}$")
_WIKILINK_INLINE_RE = re.compile(r"\[\[(\d{6})\|([^\]]+)\]\]")


# ============================================================================
# Pydantic 응답 모델
# ============================================================================

class TickerMeta(BaseModel):
    ticker: str = Field(..., examples=["005930"])
    name_ko: str
    name_en: str | None = None
    aliases: list[str] = Field(default_factory=list)
    market: str | None = None
    sector: str | None = None
    asset_type: str = "stock"           # stock | etf
    tier: str = "lazy"                  # eager | lazy
    section_count: int = 0
    last_updated_at: str | None = None


class TickerListResponse(BaseModel):
    count: int
    items: list[TickerMeta]


class SectionRef(BaseModel):
    """섹션 인덱스 레벨 정보 (본문 미포함)."""
    doc_id: str
    ticker: str
    section_type: str
    description: str | None = None
    file_path: str
    tokens: int | None = None
    updated_at: str | None = None


class SectionDetail(BaseModel):
    """섹션 본문 + 메타 (frontmatter, tags, wikilinks)."""
    doc_id: str
    ticker: str
    section_type: str
    description: str | None = None
    file_path: str
    body: str
    tags: list[str] = Field(default_factory=list)
    wikilinks: list[dict[str, str]] = Field(default_factory=list)
    updated_at: str | None = None
    frontmatter: dict[str, Any] = Field(default_factory=dict)


class EventItem(BaseModel):
    event_id: int
    ticker: str
    event_type: str
    occurred_at: str
    headline: str | None = None
    summary: str | None = None
    impact_score: float | None = None
    source_id: str | None = None


class EventListResponse(BaseModel):
    count: int
    items: list[EventItem]


class TagItem(BaseModel):
    tag: str
    count: int


class TagListResponse(BaseModel):
    count: int
    items: list[TagItem]


class TagSectionsResponse(BaseModel):
    tag: str
    count: int
    sections: list[SectionRef]


class BacklinkItem(BaseModel):
    src_ticker: str
    src_name_ko: str
    src_section: str
    display_text: str | None = None


class BacklinksResponse(BaseModel):
    ticker: str
    count: int
    items: list[BacklinkItem]


class ConstituentItem(BaseModel):
    """ETF 구성종목 — 링크된(=ticker_master에 존재) 또는 plain text 멘션."""
    name: str
    ticker: str | None = None       # 등록된 ticker 면 6자리 코드, 아니면 None
    linked: bool = False             # True 면 ticker가 우리 마스터에 등록됨
    mentioned_in_sections: list[str] = Field(default_factory=list)


class ConstituentsResponse(BaseModel):
    ticker: str
    name_ko: str
    asset_type: str
    linked_count: int
    plain_count: int
    items: list[ConstituentItem]


class SearchHit(BaseModel):
    doc_id: str
    ticker: str
    section_type: str
    score: float
    source: str = "primary"
    content_preview: str


class SearchResponse(BaseModel):
    query: str
    count: int
    hits: list[SearchHit]


class HealthResponse(BaseModel):
    status: str = "ok"
    server_time: str
    ticker_master: int
    section_docs: int
    embedded_docs: int
    events: int


# ----- Chat / 대화 -----

class ChatMessage(BaseModel):
    role: str = Field(..., description="user | assistant | system",
                      examples=["user"])
    content: str = Field(..., examples=["삼성전자 HBM 실적 어때?"])


class ChatRequest(BaseModel):
    """챗 요청.

    - **single-turn**: `q` 만 보내면 됨.
    - **multi-turn**:  `messages` 배열을 보냄. 마지막 user 메시지가 검색·라우팅
      대상이 되고, 그 이전 턴들은 LLM 프롬프트의 '대화 기록' 으로 함께 전달됨.
    - 동시에 보내면 `messages` 가 우선.
    """
    q: str | None = Field(
        None, description="single-turn 편의: 한 줄 질의 (messages 와 같이 쓰면 무시)",
        examples=["삼성전자 HBM 실적 어때?"],
    )
    messages: list[ChatMessage] | None = Field(
        None, description="multi-turn 대화 기록. 마지막 user 메시지가 latest query.",
    )
    top_k_sections: int = Field(
        5, ge=1, le=20, description="hybrid search 에서 가져올 섹션 수",
    )
    trace: bool = Field(
        False, description="True 면 응답에 internal trace(resolver/route/section_hits) 포함",
    )

    def latest_query(self) -> str:
        if self.messages:
            for m in reversed(self.messages):
                if (m.role or "").lower() == "user" and m.content.strip():
                    return m.content.strip()
            return ""
        return (self.q or "").strip()

    def history(self) -> list[dict]:
        """latest user 메시지를 제외한 직전 턴들 (시간 순)."""
        if not self.messages:
            return []
        last_user_idx: int | None = None
        for i in range(len(self.messages) - 1, -1, -1):
            if (self.messages[i].role or "").lower() == "user":
                last_user_idx = i
                break
        if last_user_idx is None:
            return []
        return [
            {"role": m.role, "content": m.content}
            for m in self.messages[:last_user_idx]
            if (m.content or "").strip()
        ]


class ChatHit(BaseModel):
    doc_id: str
    ticker: str
    section_type: str
    score: float
    content_preview: str


class ChatResponse(BaseModel):
    """단일 응답(non-streaming) 챗 결과."""
    answer: str
    tickers: list[str]
    intent: str
    lazy_compiled: list[str]
    section_hits: list[ChatHit]
    used_model: str
    history_turns: int = 0
    trace: dict[str, Any] | None = None


# ============================================================================
# 헬퍼
# ============================================================================

def _validate_ticker(ticker: str) -> None:
    if not _TICKER_RE.match(ticker):
        raise HTTPException(400, "ticker must be a 6-digit code")


def _ticker_meta_row(conn, ticker: str | None = None) -> list[dict]:
    where = ""
    params: tuple = ()
    if ticker:
        where = "WHERE tm.ticker = ?"
        params = (ticker,)
    return [dict(r) for r in conn.execute(
        f"""SELECT tm.ticker, tm.name_ko, tm.name_en, tm.aliases_json,
                   tm.market, tm.sector,
                   COALESCE(tm.asset_type, 'stock') AS asset_type,
                   COALESCE(tt.tier, 'lazy')        AS tier,
                   COUNT(sd.doc_id)                 AS section_count,
                   MAX(sd.updated_at)               AS last_updated_at
           FROM ticker_master tm
           LEFT JOIN ticker_tier  tt ON tt.ticker = tm.ticker
           LEFT JOIN section_doc  sd ON sd.ticker = tm.ticker
           {where}
           GROUP BY tm.ticker
           ORDER BY tm.ticker""",
        params,
    ).fetchall()]


def _row_to_ticker_meta(r: dict) -> TickerMeta:
    import json as _json
    aliases: list[str] = []
    try:
        aliases = _json.loads(r.get("aliases_json") or "[]")
    except Exception:
        aliases = []
    return TickerMeta(
        ticker=r["ticker"],
        name_ko=r["name_ko"],
        name_en=r.get("name_en"),
        aliases=aliases,
        market=r.get("market"),
        sector=r.get("sector"),
        asset_type=r.get("asset_type") or "stock",
        tier=r.get("tier") or "lazy",
        section_count=r.get("section_count") or 0,
        last_updated_at=r.get("last_updated_at"),
    )


def _section_detail(conn, ticker: str, section_type: str) -> SectionDetail:
    row = conn.execute(
        """SELECT doc_id, ticker, section_type, file_path, tokens, updated_at
           FROM section_doc WHERE ticker=? AND section_type=?""",
        (ticker, section_type),
    ).fetchone()
    if not row:
        raise HTTPException(404, f"section not found: {ticker}:{section_type}")
    sec = load_section_file((TICKERS_ROOT.parent.parent / row["file_path"]))
    if sec is None:
        # 파일이 사라진 경우 — DB 인덱스만 있는 비정상 상태
        raise HTTPException(500, f"section file missing on disk: {row['file_path']}")
    tag_rows = conn.execute(
        "SELECT tag FROM section_tag WHERE doc_id=? ORDER BY tag",
        (row["doc_id"],),
    ).fetchall()
    wl_rows = conn.execute(
        """SELECT target_ticker, display_text
           FROM section_wikilink WHERE src_doc_id=?""",
        (row["doc_id"],),
    ).fetchall()
    return SectionDetail(
        doc_id=row["doc_id"],
        ticker=row["ticker"],
        section_type=row["section_type"],
        description=SECTION_DESCRIPTIONS.get(section_type),
        file_path=row["file_path"],
        body=sec.body,
        tags=[r["tag"] for r in tag_rows],
        wikilinks=[
            {"target_ticker": r["target_ticker"],
             "display_text": r["display_text"] or r["target_ticker"]}
            for r in wl_rows
        ],
        updated_at=row["updated_at"],
        frontmatter=sec.meta,
    )


# 구성종목으로 자주 등장하지만 회사가 아닌 도메인/테마 어휘 — plain mention 에서 컷
_NON_CONSTITUENT_TOKENS: frozenset[str] = frozenset({
    # 메타·구조 어휘
    "구성종목", "주요", "비중", "포트폴리오", "기본", "분류", "관련", "시장",
    "업종", "섹터", "소재", "지수", "기초지수", "달러", "환율", "환노출",
    "상위", "하위",
    # 인덱스·자산군 라벨
    "코스피", "코스피200", "코스닥", "S&P500", "S&P 500", "S&P",
    "나스닥", "나스닥100", "NASDAQ", "KRX", "Total", "TR",
    # 운용사·brand prefix
    "KODEX", "TIGER", "ARIRANG", "SOL", "ACE", "PLUS", "RISE",
    # 데이터 출처 라벨 (frontmatter 인용에서 파생)
    "Curated", "Full",
    # 도메인 일반 어휘
    "AI", "AI 가속기", "EV", "IRA", "ETF", "메모리", "반도체", "후공정",
    "장비주", "셀", "양극재", "음극재", "전구체", "전해질", "전해액", "분리막",
    "리튬", "니켈", "코발트",
    # 지역/접속어
    "글로벌", "미국", "중국", "일본", "약", "등", "한", "소",
})


# claim 본문에 붙는 인용 footer 패턴 — 회사명이 아니라 출처 라벨이 잡히는 걸 막음
# 예: `_([Curated](https://...) · conf:0.92)_` 또는 `_(src:Curated · conf:0.85)_`
_CITATION_FOOTER_RE = re.compile(r"_\([^)]*\)_")
_MARKDOWN_HEADER_RE = re.compile(r"^#+\s.*$", re.MULTILINE)

# 회사명으로 보일 가능성이 높은 한글/영문 토큰 (단순 휴리스틱)
_CONSTITUENT_TOKEN_RE = re.compile(
    r"[가-힣A-Z][가-힣A-Za-z0-9.&]{1,29}"
)


def _extract_constituent_mentions(
    body: str,
    self_ticker: str | None = None,
    self_name: str | None = None,
) -> list[tuple[str | None, str]]:
    """본문에서 (ticker_or_None, display_text) 후보를 추출.

    1) `[[code|name]]` 위키링크 → (code, name)
    2) 위키링크 외 평문에서 콤마·중점·괄호 구분된 회사명 후보 → (None, name)

    ETF relations/profile/business 섹션의 '구성종목은 A, B, C ...' 패턴에 최적화된
    가벼운 NER. 도메인 어휘(AI 가속기, 메모리 등)와 자기 자신은 제외한다.
    """
    out: list[tuple[str | None, str]] = []
    seen: set[str] = set()

    self_name_norm = (self_name or "").strip()

    # 1) 위키링크 (가장 신뢰도 높음)
    for m in _WIKILINK_INLINE_RE.finditer(body):
        code, name = m.group(1), m.group(2).strip()
        if self_ticker and code == self_ticker:
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append((code, name))

    # 2) plain text — 위키링크/인용footer/마크다운 헤더 제거 후 콤마/괄호 분할
    stripped = _WIKILINK_INLINE_RE.sub("", body)
    stripped = _CITATION_FOOTER_RE.sub("", stripped)
    stripped = _MARKDOWN_HEADER_RE.sub("", stripped)
    # ETF 구성종목 라인만 plain mention 으로 사용
    # (이 키워드를 가진 라인이 없으면 wikilink 만으로 결과 구성)
    matched_lines: list[str] = []
    for line in stripped.splitlines():
        if re.search(r"(구성종목|주요 구성|상위 구성|상위 holdings|holdings|편입종목)",
                     line):
            matched_lines.append(line)
    if not matched_lines:
        return out
    pool = "\n".join(matched_lines)

    candidates = re.split(r"[,，·•()\[\]]| 등 |/| 와 | 과 |\n", pool)
    for c in candidates:
        c = c.strip().strip("()[]{}<>「」『』\"'·•")
        if not c or len(c) < 2 or len(c) > 30:
            continue
        # 수치·퍼센트·단위 노이즈
        if re.search(r"\d+\s*(%|조|억|원|배|차|위|위권)", c):
            continue
        # 흔한 동사·연결어 컷
        if re.search(r"(이다|한다|된다|있다|이며|받는다|운영|영향|밸류체인|진입|국면|변동성)", c):
            continue
        token_match = _CONSTITUENT_TOKEN_RE.search(c)
        if not token_match:
            continue
        name = token_match.group(0).strip(" .")
        if len(name) < 2 or name in seen:
            continue
        if name in _NON_CONSTITUENT_TOKENS:
            continue
        if self_name_norm and name == self_name_norm:
            continue
        seen.add(name)
        out.append((None, name))
    return out


# ============================================================================
# 엔드포인트
# ============================================================================

@router.get("/health", response_model=HealthResponse,
            summary="API 헬스 + 인덱스 카운트")
def api_health() -> HealthResponse:
    with tx() as conn:
        n_tick = conn.execute("SELECT COUNT(*) AS n FROM ticker_master").fetchone()["n"]
        n_sec = conn.execute("SELECT COUNT(*) AS n FROM section_doc").fetchone()["n"]
        n_emb = conn.execute(
            "SELECT COUNT(*) AS n FROM section_doc WHERE embedding IS NOT NULL"
        ).fetchone()["n"]
        n_evt = conn.execute("SELECT COUNT(*) AS n FROM stock_event_timeline").fetchone()["n"]
    return HealthResponse(
        status="ok",
        server_time=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ticker_master=n_tick,
        section_docs=n_sec,
        embedded_docs=n_emb,
        events=n_evt,
    )


def _list_tickers(
    asset_type: str | None = None,
    market: str | None = None,
    sector: str | None = None,
    q: str | None = None,
) -> TickerListResponse:
    with tx() as conn:
        rows = _ticker_meta_row(conn)
    items: list[TickerMeta] = []
    q_low = q.lower() if q else None
    for r in rows:
        meta = _row_to_ticker_meta(r)
        if asset_type and meta.asset_type != asset_type:
            continue
        if market and (meta.market or "").upper() != market.upper():
            continue
        if sector and (meta.sector or "") != sector:
            continue
        if q_low:
            haystack = " ".join(
                [meta.ticker, meta.name_ko, meta.name_en or "", *meta.aliases]
            ).lower()
            if q_low not in haystack:
                continue
        items.append(meta)
    return TickerListResponse(count=len(items), items=items)


@router.get("/tickers", response_model=TickerListResponse,
            summary="종목·ETF 마스터 리스트")
def api_tickers(
    asset_type: str | None = Query(None, description="stock | etf"),
    market: str | None = Query(None, description="KOSPI | KOSDAQ ..."),
    sector: str | None = Query(None),
    q: str | None = Query(None, description="이름·alias 부분 매칭"),
) -> TickerListResponse:
    return _list_tickers(asset_type=asset_type, market=market, sector=sector, q=q)


@router.get("/tickers/{ticker}", response_model=TickerMeta,
            summary="단일 종목 메타")
def api_ticker(ticker: str) -> TickerMeta:
    _validate_ticker(ticker)
    with tx() as conn:
        rows = _ticker_meta_row(conn, ticker)
    if not rows:
        raise HTTPException(404, f"ticker not found: {ticker}")
    return _row_to_ticker_meta(rows[0])


@router.get("/tickers/{ticker}/sections", response_model=list[SectionRef],
            summary="종목의 모든 섹션 인덱스")
def api_ticker_sections(ticker: str) -> list[SectionRef]:
    _validate_ticker(ticker)
    with tx() as conn:
        rows = conn.execute(
            """SELECT doc_id, ticker, section_type, file_path, tokens, updated_at
               FROM section_doc WHERE ticker=? ORDER BY section_type""",
            (ticker,),
        ).fetchall()
    if not rows:
        raise HTTPException(404, f"no sections compiled yet for {ticker}")
    # SECTION_ORDER 순서로 정렬
    rows_sorted = sorted(
        rows,
        key=lambda r: int(SECTION_ORDER.get(r["section_type"], "99")),
    )
    return [
        SectionRef(
            doc_id=r["doc_id"],
            ticker=r["ticker"],
            section_type=r["section_type"],
            description=SECTION_DESCRIPTIONS.get(r["section_type"]),
            file_path=r["file_path"],
            tokens=r["tokens"],
            updated_at=r["updated_at"],
        )
        for r in rows_sorted
    ]


@router.get("/tickers/{ticker}/sections/{section_type}", response_model=SectionDetail,
            summary="섹션 본문 + 태그 + 위키링크")
def api_ticker_section(ticker: str, section_type: str) -> SectionDetail:
    _validate_ticker(ticker)
    if section_type not in SECTION_TYPES:
        raise HTTPException(400, f"unknown section_type: {section_type}")
    with tx() as conn:
        return _section_detail(conn, ticker, section_type)


@router.get("/tickers/{ticker}/events", response_model=EventListResponse,
            summary="종목 이벤트 타임라인")
def api_ticker_events(
    ticker: str,
    types: str | None = Query(
        None,
        description="콤마구분: news,disclosure,sns (생략시 전체)",
    ),
    limit: int = Query(20, ge=1, le=200),
    since: str | None = Query(None, description="ISO8601 lower bound"),
) -> EventListResponse:
    _validate_ticker(ticker)
    type_list: list[str] | None = None
    if types:
        type_list = [t.strip() for t in types.split(",") if t.strip()]
    sql = (
        "SELECT event_id, ticker, event_type, occurred_at, headline, summary, "
        "impact_score, source_id "
        "FROM stock_event_timeline WHERE ticker = ?"
    )
    params: list = [ticker]
    if type_list:
        sql += " AND event_type IN (" + ",".join(["?"] * len(type_list)) + ")"
        params.extend(type_list)
    if since:
        sql += " AND occurred_at >= ?"
        params.append(since)
    sql += " ORDER BY occurred_at DESC LIMIT ?"
    params.append(limit)
    with tx() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    items = [EventItem(**dict(r)) for r in rows]
    return EventListResponse(count=len(items), items=items)


@router.get("/tickers/{ticker}/backlinks", response_model=BacklinksResponse,
            summary="이 종목을 가리키는 다른 섹션 (incoming wikilinks)")
def api_ticker_backlinks(ticker: str) -> BacklinksResponse:
    _validate_ticker(ticker)
    with tx() as conn:
        rows = conn.execute(
            """SELECT sd.ticker AS src_ticker, sd.section_type AS src_section,
                      tm.name_ko, wl.display_text
               FROM section_wikilink wl
               JOIN section_doc    sd ON sd.doc_id = wl.src_doc_id
               JOIN ticker_master  tm ON tm.ticker = sd.ticker
               WHERE wl.target_ticker = ? AND sd.ticker != ?
               ORDER BY sd.ticker, sd.section_type""",
            (ticker, ticker),
        ).fetchall()
    items = [
        BacklinkItem(
            src_ticker=r["src_ticker"],
            src_name_ko=r["name_ko"],
            src_section=r["src_section"],
            display_text=r["display_text"],
        )
        for r in rows
    ]
    return BacklinksResponse(ticker=ticker, count=len(items), items=items)


# ----- ETF 전용 -----
@router.get("/etfs", response_model=TickerListResponse,
            summary="ETF 만 필터한 ticker 리스트")
def api_etfs() -> TickerListResponse:
    return _list_tickers(asset_type="etf")


@router.get("/etfs/{ticker}/constituents", response_model=ConstituentsResponse,
            summary="ETF 구성종목 — 링크된(=마스터 등록) + plain text 멘션")
def api_etf_constituents(ticker: str) -> ConstituentsResponse:
    _validate_ticker(ticker)
    with tx() as conn:
        meta_rows = _ticker_meta_row(conn, ticker)
        if not meta_rows:
            raise HTTPException(404, f"ticker not found: {ticker}")
        meta = meta_rows[0]
        if meta["asset_type"] != "etf":
            raise HTTPException(400, f"{ticker} is not an ETF (asset_type={meta['asset_type']})")

        # ETF 구성종목 후보는 주로 relations + profile + business 섹션 본문에 노출됨
        target_sections = ("relations", "profile", "business", "theme")
        sec_rows = conn.execute(
            f"""SELECT section_type, file_path FROM section_doc
                WHERE ticker = ? AND section_type IN
                ({",".join(["?"] * len(target_sections))})""",
            (ticker, *target_sections),
        ).fetchall()

        # name → known ticker 매핑 (등록된 마스터 alias 기반)
        master_rows = conn.execute(
            "SELECT ticker, name_ko, aliases_json FROM ticker_master"
        ).fetchall()

    import json as _json
    name_to_ticker: dict[str, str] = {}
    for mr in master_rows:
        name_to_ticker[mr["name_ko"]] = mr["ticker"]
        try:
            for a in _json.loads(mr["aliases_json"] or "[]"):
                a = (a or "").strip()
                if a:
                    name_to_ticker.setdefault(a, mr["ticker"])
        except Exception:
            pass

    by_name: dict[str, ConstituentItem] = {}
    for sr in sec_rows:
        sec = load_section_file(TICKERS_ROOT.parent.parent / sr["file_path"])
        if sec is None:
            continue
        mentions = _extract_constituent_mentions(
            sec.body, self_ticker=ticker, self_name=meta["name_ko"],
        )
        for code, name in mentions:
            resolved_code = code or name_to_ticker.get(name)
            # 자기 자신(ETF) 은 구성종목이 아님
            if resolved_code == ticker:
                continue
            key = resolved_code or name
            existing = by_name.get(key)
            if existing is None:
                existing = ConstituentItem(
                    name=name,
                    ticker=resolved_code,
                    linked=resolved_code is not None,
                )
                by_name[key] = existing
            if sr["section_type"] not in existing.mentioned_in_sections:
                existing.mentioned_in_sections.append(sr["section_type"])

    items = sorted(
        by_name.values(),
        key=lambda x: (not x.linked, x.ticker or "9" + x.name),
    )
    linked_count = sum(1 for i in items if i.linked)
    return ConstituentsResponse(
        ticker=ticker,
        name_ko=meta["name_ko"],
        asset_type=meta["asset_type"],
        linked_count=linked_count,
        plain_count=len(items) - linked_count,
        items=items,
    )


# ----- 태그 -----
@router.get("/tags", response_model=TagListResponse,
            summary="전체 태그 + count (top N)")
def api_tags(limit: int = Query(100, ge=1, le=500)) -> TagListResponse:
    with tx() as conn:
        rows = conn.execute(
            """SELECT tag, COUNT(*) AS n
               FROM section_tag GROUP BY tag
               ORDER BY n DESC, tag LIMIT ?""",
            (limit,),
        ).fetchall()
    items = [TagItem(tag=r["tag"], count=r["n"]) for r in rows]
    return TagListResponse(count=len(items), items=items)


@router.get("/tags/{tag}", response_model=TagSectionsResponse,
            summary="태그 매칭 섹션 (tag-based discovery)")
def api_tag_sections(tag: str) -> TagSectionsResponse:
    with tx() as conn:
        rows = conn.execute(
            """SELECT sd.doc_id, sd.ticker, sd.section_type, sd.file_path,
                      sd.tokens, sd.updated_at
               FROM section_tag st
               JOIN section_doc sd ON sd.doc_id = st.doc_id
               WHERE st.tag = ?
               ORDER BY sd.ticker, sd.section_type""",
            (tag,),
        ).fetchall()
    sections = [
        SectionRef(
            doc_id=r["doc_id"],
            ticker=r["ticker"],
            section_type=r["section_type"],
            description=SECTION_DESCRIPTIONS.get(r["section_type"]),
            file_path=r["file_path"],
            tokens=r["tokens"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]
    return TagSectionsResponse(tag=tag, count=len(sections), sections=sections)


# ----- 검색 -----
@router.get("/search", response_model=SearchResponse,
            summary="Hybrid 검색 (BM25 + dense + RRF) — JSON 결과")
def api_search(
    q: str = Query(..., min_length=1, description="검색어"),
    top_k: int = Query(5, ge=1, le=30),
    tickers: str | None = Query(
        None, description="콤마 구분 (예: 005930,000660). 생략시 전체"
    ),
    section_types: str | None = Query(
        None, description="콤마 구분. 생략시 전체"
    ),
    expand_tags: str | None = Query(
        None, description="콤마 구분. 태그 fallback/확장"
    ),
) -> SearchResponse:
    tk_list = [t.strip() for t in (tickers or "").split(",") if t.strip()] or None
    sec_list = [s.strip() for s in (section_types or "").split(",") if s.strip()] or None
    tag_list = [t.strip() for t in (expand_tags or "").split(",") if t.strip()] or None
    hits = hybrid_search(
        query=q,
        tickers=tk_list,
        section_types=sec_list,
        top_k=top_k,
        expand_tags=tag_list,
    )
    return SearchResponse(
        query=q,
        count=len(hits),
        hits=[
            SearchHit(
                doc_id=h.doc_id,
                ticker=h.ticker,
                section_type=h.section_type,
                score=round(h.score, 4),
                source=h.source,
                content_preview=h.content[:240],
            )
            for h in hits
        ],
    )


# ----- 챗 / 대화 -----

def _validate_chat_request(req: ChatRequest) -> tuple[str, list[dict]]:
    q = req.latest_query()
    if not q:
        raise HTTPException(
            400,
            "비어있지 않은 q 또는 적어도 하나의 user 메시지가 필요합니다",
        )
    return q, req.history()


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="챗 답변 (single-turn 또는 multi-turn) — JSON",
)
def api_chat(req: ChatRequest) -> ChatResponse:
    """기존 `/ask` 와 같은 답변 합성 파이프라인을 JSON 으로 호출.

    `messages` 가 있으면 마지막 user 메시지가 latest query 가 되고, 그 이전
    턴들은 LLM 프롬프트의 '대화 기록' 으로 합쳐진다. multi-turn 호출은 답변
    캐시를 우회한다 (같은 last-query 라도 이전 턴이 다르면 답이 달라야 함).
    """
    q, history = _validate_chat_request(req)
    trace = answer_query(q, top_k_sections=req.top_k_sections, history=history or None)

    return ChatResponse(
        answer=trace.answer,
        tickers=[r["ticker"] for r in trace.resolved if r["score"] >= 80.0],
        intent=trace.route.get("intent", "generic"),
        lazy_compiled=trace.lazy_compiled,
        section_hits=[
            ChatHit(
                doc_id=h["doc_id"],
                ticker=h["ticker"],
                section_type=h["section_type"],
                score=round(float(h["score"]), 4),
                content_preview=h.get("content_preview", "")[:240],
            )
            for h in trace.section_hits
        ],
        used_model=trace.used_model,
        history_turns=len(history),
        trace=asdict(trace) if req.trace else None,
    )


@router.post(
    "/chat/stream",
    summary="챗 답변 (NDJSON 토큰 스트림)",
    responses={
        200: {
            "content": {
                "application/x-ndjson": {
                    "example": (
                        '{"type":"meta","tickers":["005930"],"intent":"finance","cached":false,...}\n'
                        '{"type":"sections","hits":[...]}\n'
                        '{"type":"token","text":"삼성전자"}\n'
                        '{"type":"token","text":"의 2025년"}\n'
                        '{"type":"done","latency_ms":820,"cached":false}\n'
                    )
                }
            },
            "description": (
                "NDJSON: 한 줄에 하나의 이벤트. type ∈ {meta, sections, token, "
                "answer(캐시 히트 시 전체 답변), done}."
            ),
        }
    },
)
def api_chat_stream(req: ChatRequest):
    q, history = _validate_chat_request(req)

    def generate() -> Iterable[bytes]:
        for event in answer_stream_fn(
            q, top_k_sections=req.top_k_sections, history=history or None,
        ):
            yield (_json_serializer.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")

    return StreamingResponse(
        generate(),
        media_type="application/x-ndjson; charset=utf-8",
        headers={"X-Accel-Buffering": "no"},
    )


# ----- 운영 -----
@router.post("/refresh/{ticker}",
             summary="단일 종목 lazy_compile (섹션 재생성 + 재임베딩)")
def api_refresh(ticker: str) -> dict[str, Any]:
    _validate_ticker(ticker)
    n = lazy_compile(ticker)
    return {"ticker": ticker, "recompiled_sections": n,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds")}
