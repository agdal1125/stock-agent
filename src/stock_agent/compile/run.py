"""Compile orchestrator — page_touch_queue를 소비해 eager/lazy 정책에 따라
L1 섹션 렌더 + 임베딩.

프로덕션 등가: Silver write → Service Bus → ACA Job consumer → AI Search push.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..config import CFG
from ..db import tx
from ..l1_index.section_builder import (
    append_log, compile_ticker, ensure_global_files, regenerate_index,
)
from ..l1_index.embedder import embed_pending


def promote_top_n() -> int:
    """이벤트 수(=impact_score 합계) 기준 상위 N 종목을 eager로 승격."""
    n = CFG.eager_top_n
    with tx() as conn:
        # top-N 산정
        rows = conn.execute(
            """SELECT ticker, COALESCE(SUM(impact_score),0) AS total_impact,
                             COUNT(*) AS evt_n
               FROM stock_event_timeline
               GROUP BY ticker
               ORDER BY total_impact DESC, evt_n DESC
               LIMIT ?""",
            (n,),
        ).fetchall()
        eager = [r["ticker"] for r in rows]
        conn.execute("UPDATE ticker_tier SET tier='lazy'")
        for t in eager:
            conn.execute(
                "INSERT INTO ticker_tier(ticker, tier) VALUES(?, 'eager') "
                "ON CONFLICT(ticker) DO UPDATE SET tier='eager'",
                (t,),
            )
    return len(eager)


def consume_touch_queue(mode: str = "eager_only") -> list[str]:
    """page_touch_queue에서 대상 ticker 리스트 수집 후 consumed 마킹.

    mode:
      - 'eager_only' : tier=eager 종목만 처리
      - 'all'        : 모든 종목 처리
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with tx() as conn:
        rows = conn.execute(
            """SELECT DISTINCT q.ticker
               FROM page_touch_queue q
               LEFT JOIN ticker_tier tt ON tt.ticker = q.ticker
               WHERE q.consumed_at IS NULL
                 AND (? = 'all' OR tt.tier = 'eager')""",
            (mode,),
        ).fetchall()
        tickers = [r["ticker"] for r in rows]
        if tickers:
            placeholders = ",".join(["?"] * len(tickers))
            conn.execute(
                f"UPDATE page_touch_queue SET consumed_at=? "
                f"WHERE consumed_at IS NULL AND ticker IN ({placeholders})",
                (now, *tickers),
            )
    return tickers


def lazy_compile(ticker: str) -> int:
    """첫 질의 시 해당 종목만 온디맨드 컴파일 + 임베딩."""
    ensure_global_files()
    n = compile_ticker(ticker)
    embed_pending()
    with tx() as conn:
        conn.execute(
            "INSERT INTO ticker_tier(ticker, tier) VALUES(?, 'lazy') "
            "ON CONFLICT(ticker) DO UPDATE SET last_query_at=?, "
            "query_count=ticker_tier.query_count+1",
            (ticker, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
    regenerate_index()
    append_log(f"lazy compile: {ticker} ({n} sections)")
    return n


def run_eager_pipeline() -> dict:
    ensure_global_files()
    eager_n = promote_top_n()
    tickers = consume_touch_queue(mode="eager_only")
    docs_n = 0
    for t in tickers:
        docs_n += compile_ticker(t)
    embeds_n = embed_pending()
    regenerate_index()
    append_log(f"eager pipeline: eager={eager_n}, compiled={len(tickers)}, sections={docs_n}, embeds={embeds_n}")
    return {"eager_tickers": eager_n, "compiled_tickers": len(tickers),
            "section_docs": docs_n, "embeddings": embeds_n}


if __name__ == "__main__":
    print(run_eager_pipeline())
