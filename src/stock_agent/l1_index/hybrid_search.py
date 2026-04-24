"""BM25 + dense cosine → RRF 병합 hybrid search (P5.5 확장).

프로덕션 등가: Azure AI Search hybrid search + semantic ranker.

P5.5 확장:
  - expand_tags     : 1차 섹션의 태그 또는 Query Understanding 이 준 태그로
                      같은 태그가 붙은 다른 종목 섹션을 편입
  - expand_tickers  : wikilink/연관 종목 집합 — 해당 종목 섹션도 후보에 포함
  - 각 SectionHit 에 `source` 라벨 부여 (primary / tag:HBM / wikilink:005930)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
from rank_bm25 import BM25Okapi

from ..agent_int.llm_gateway import embed
from ..db import tx
from .embedder import load_matrix


_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[가-힣]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


@dataclass
class SectionHit:
    doc_id: str
    ticker: str
    section_type: str
    content: str
    score: float
    source: str = "primary"            # primary | tag:<tag> | wikilink:<code>
    sources: list[str] = field(default_factory=list)  # 중복 유입 시 전체 기록


def _rrf(ranks_a: dict[str, int], ranks_b: dict[str, int], k: int = 60) -> dict[str, float]:
    """Reciprocal Rank Fusion."""
    keys = set(ranks_a) | set(ranks_b)
    out: dict[str, float] = {}
    for key in keys:
        s = 0.0
        if key in ranks_a:
            s += 1.0 / (k + ranks_a[key])
        if key in ranks_b:
            s += 1.0 / (k + ranks_b[key])
        out[key] = s
    return out


# ---------------------------------------------------------------------------
# 후보 수집 — 다중 소스 병합
# ---------------------------------------------------------------------------

def _primary_candidate_sql(
    tickers: list[str] | None,
    section_types: list[str] | None,
) -> tuple[str, list]:
    where: list[str] = []
    params: list = []
    if tickers:
        where.append("ticker IN (" + ",".join(["?"] * len(tickers)) + ")")
        params.extend(tickers)
    if section_types:
        where.append("section_type IN (" + ",".join(["?"] * len(section_types)) + ")")
        params.extend(section_types)
    return " AND ".join(where), params


def _tag_expansion_docs(
    tags: list[str],
    exclude_tickers: set[str],
    section_types: list[str] | None = None,
    limit: int = 8,
) -> dict[str, str]:
    """태그 기반 확장 — 주어진 tags 중 하나라도 붙은 섹션 doc_id → source 라벨.

    exclude_tickers 는 이미 primary 로 포함된 종목 (중복 라벨링 방지).
    """
    if not tags:
        return {}
    with tx() as conn:
        placeholders = ",".join(["?"] * len(tags))
        sec_filter = ""
        params: list = list(tags)
        if section_types:
            sec_filter = " AND sd.section_type IN (" + \
                         ",".join(["?"] * len(section_types)) + ")"
            params.extend(section_types)
        rows = conn.execute(
            f"""SELECT sd.doc_id, st.tag
                FROM section_tag st
                JOIN section_doc sd ON sd.doc_id = st.doc_id
                WHERE st.tag IN ({placeholders}){sec_filter}
                ORDER BY st.tag""",
            tuple(params),
        ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        doc_id = r["doc_id"]
        t = doc_id.split(":", 1)[0] if ":" in doc_id else ""
        if t in exclude_tickers:
            continue
        if doc_id in out:
            continue
        out[doc_id] = f"tag:{r['tag']}"
        if len(out) >= limit:
            break
    return out


def _wikilink_expansion_tickers(
    primary_doc_ids: list[str],
    exclude_tickers: set[str],
    limit_tickers: int = 3,
) -> dict[str, str]:
    """1차 섹션의 outgoing wikilink → 연결된 다른 종목 집합."""
    if not primary_doc_ids:
        return {}
    placeholders = ",".join(["?"] * len(primary_doc_ids))
    with tx() as conn:
        rows = conn.execute(
            f"""SELECT wl.target_ticker, wl.src_doc_id
                FROM section_wikilink wl
                WHERE wl.src_doc_id IN ({placeholders})""",
            tuple(primary_doc_ids),
        ).fetchall()
    out: dict[str, str] = {}  # ticker → source label
    for r in rows:
        tgt = r["target_ticker"]
        if tgt in exclude_tickers or tgt in out:
            continue
        src_tkr = r["src_doc_id"].split(":", 1)[0]
        out[tgt] = f"wikilink:{src_tkr}"
        if len(out) >= limit_tickers:
            break
    return out


def _ticker_expansion_docs(
    expand_tickers: dict[str, str],
    section_types: list[str] | None,
) -> dict[str, str]:
    """expand_tickers 에 들어온 종목들의 섹션 doc_id 전부 후보 편입."""
    if not expand_tickers:
        return {}
    codes = list(expand_tickers.keys())
    placeholders = ",".join(["?"] * len(codes))
    sec_filter = ""
    params: list = list(codes)
    if section_types:
        sec_filter = " AND section_type IN (" + \
                     ",".join(["?"] * len(section_types)) + ")"
        params.extend(section_types)
    with tx() as conn:
        rows = conn.execute(
            f"""SELECT doc_id, ticker FROM section_doc
                WHERE ticker IN ({placeholders}){sec_filter}""",
            tuple(params),
        ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        out[r["doc_id"]] = expand_tickers[r["ticker"]]
    return out


# ---------------------------------------------------------------------------
# Search 진입점
# ---------------------------------------------------------------------------

def search(
    query: str,
    tickers: list[str] | None = None,
    section_types: list[str] | None = None,
    top_k: int = 5,
    *,
    expand_tags: list[str] | None = None,
    expand_tickers: list[str] | None = None,
) -> list[SectionHit]:
    """Hybrid search — P5.5 확장.

    Parameters
    ----------
    expand_tags : Query Understanding 이 제시한 보조 태그 (None=미사용)
    expand_tickers : 이미 아는 연관 종목 (wikilink traversal 결과와 합쳐짐)
    """
    # ------------------------------------------------------------------
    # 1) primary 후보 — ticker namespace + section_types
    #    (tickers 없으면 tag-only 경로로 전환)
    # ------------------------------------------------------------------
    tag_primary_docs: dict[str, str] = {}
    if not tickers and expand_tags:
        # 태그 기반 primary — 종목 지정 없이 태그로만 후보 수집
        tag_primary_docs = _tag_expansion_docs(
            expand_tags, exclude_tickers=set(), section_types=section_types, limit=24,
        )

    if tag_primary_docs:
        placeholders = ",".join(["?"] * len(tag_primary_docs))
        primary_rows, primary_vecs = load_matrix(
            f"doc_id IN ({placeholders})", tuple(tag_primary_docs.keys()),
        )
        doc_source: dict[str, str] = dict(tag_primary_docs)  # source: tag:<tag>
    else:
        where_sql, params = _primary_candidate_sql(tickers, section_types)
        primary_rows, primary_vecs = load_matrix(where_sql, tuple(params))
        doc_source = {r["doc_id"]: "primary" for r in primary_rows}

    primary_tickers_set = set(tickers or [])
    if tag_primary_docs:
        primary_tickers_set |= {r["ticker"] for r in primary_rows}
    primary_doc_ids = [r["doc_id"] for r in primary_rows]

    # ------------------------------------------------------------------
    # 2) expand_tickers — 명시적으로 준 연관 종목 섹션을 편입
    # ------------------------------------------------------------------
    ticker_exp: dict[str, str] = {}
    if expand_tickers:
        ticker_exp = {t: f"related:{t}" for t in expand_tickers
                      if t not in primary_tickers_set}

    # ------------------------------------------------------------------
    # 3) wikilink traversal (1-hop) — primary 섹션이 링크하는 다른 종목
    # ------------------------------------------------------------------
    wl_tickers = _wikilink_expansion_tickers(
        primary_doc_ids, primary_tickers_set | set(ticker_exp.keys()),
    )
    # ticker_exp 에 없는 것만 추가
    for tk, src in wl_tickers.items():
        ticker_exp.setdefault(tk, src)

    # 섹션 레벨로 확장
    ticker_exp_docs = _ticker_expansion_docs(ticker_exp, section_types)
    for doc_id, src in ticker_exp_docs.items():
        doc_source.setdefault(doc_id, src)

    # ------------------------------------------------------------------
    # 4) 태그 확장 — expand_tags + primary 섹션이 가진 상위 태그
    # ------------------------------------------------------------------
    tags_pool: list[str] = list(expand_tags or [])
    # primary 섹션의 태그 상위도 수집
    if primary_doc_ids:
        with tx() as conn:
            placeholders = ",".join(["?"] * len(primary_doc_ids))
            top_tags = conn.execute(
                f"""SELECT tag, COUNT(*) n FROM section_tag
                    WHERE doc_id IN ({placeholders})
                    GROUP BY tag ORDER BY n DESC LIMIT 3""",
                tuple(primary_doc_ids),
            ).fetchall()
        for r in top_tags:
            if r["tag"] not in tags_pool:
                tags_pool.append(r["tag"])
    # 과도 확장 방지
    tags_pool = tags_pool[:5]

    tag_docs = _tag_expansion_docs(
        tags_pool,
        primary_tickers_set | set(ticker_exp.keys()),
        section_types,
    )
    for doc_id, src in tag_docs.items():
        doc_source.setdefault(doc_id, src)

    # ------------------------------------------------------------------
    # 5) 확장 후보가 primary rows 외에 있으면 별도 로드
    # ------------------------------------------------------------------
    expansion_doc_ids = [d for d in doc_source if d not in set(primary_doc_ids)]
    if expansion_doc_ids:
        placeholders = ",".join(["?"] * len(expansion_doc_ids))
        exp_rows, exp_vecs = load_matrix(
            f"doc_id IN ({placeholders})", tuple(expansion_doc_ids),
        )
    else:
        exp_rows, exp_vecs = [], np.zeros((0, primary_vecs.shape[1] if primary_vecs.size else 1),
                                          dtype=np.float32)

    all_rows = primary_rows + exp_rows
    if not all_rows:
        return []

    if exp_rows:
        all_vecs = np.vstack([primary_vecs, exp_vecs]) if primary_rows else exp_vecs
    else:
        all_vecs = primary_vecs

    # ------------------------------------------------------------------
    # 6) 스코어링 — BM25 + dense + RRF (확장 후보는 rank 패널티 대신 source weight 유지)
    # ------------------------------------------------------------------
    corpus_tokens = [tokenize(r["content"]) for r in all_rows]
    bm25 = BM25Okapi(corpus_tokens)
    bm_scores = bm25.get_scores(tokenize(query))
    bm_order = np.argsort(-bm_scores)
    bm_ranks = {all_rows[i]["doc_id"]: rank for rank, i in enumerate(bm_order)}

    qvec = embed([query])[0]
    dense_scores = all_vecs @ qvec
    dense_order = np.argsort(-dense_scores)
    dense_ranks = {all_rows[i]["doc_id"]: rank for rank, i in enumerate(dense_order)}

    fused = _rrf(bm_ranks, dense_ranks)

    # primary 후보에 소폭 부스트 (질의 본 목적 우선)
    for doc_id in primary_doc_ids:
        if doc_id in fused:
            fused[doc_id] *= 1.1

    ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]

    by_id = {r["doc_id"]: r for r in all_rows}
    hits: list[SectionHit] = []
    for doc_id, s in ranked:
        r = by_id[doc_id]
        src = doc_source.get(doc_id, "primary")
        hits.append(SectionHit(
            doc_id=doc_id,
            ticker=r["ticker"],
            section_type=r["section_type"],
            content=r["content"],
            score=float(s),
            source=src,
            sources=[src],
        ))
    return hits


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "삼성전자 HBM"
    for h in search(q, top_k=5, expand_tags=["HBM"]):
        print(f"{h.score:.4f}  [{h.source}]  {h.doc_id:40s}  {h.content[:60]}")
