"""Raw .md → L0 canonical tables.

프로덕션 등가: ADLS landing → Databricks Bronze/Silver → Gold 파이프라인.
여기서는 frontmatter 기반 md 파일을 읽어
  - source_registry (manifest)
  - stock_event_timeline (뉴스/공시 = event)
  - ticker_master (seed에서 upsert)
  - page_touch_queue (compile 트리거용)
에 적재한다.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from ..config import CFG
from ..db import init_db, tx


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _source_id(path: Path) -> str:
    try:
        key = str(path.resolve().relative_to(CFG.data_dir.resolve()))
    except ValueError:
        key = str(path.resolve())
    return hashlib.sha1(key.replace("\\", "/").encode("utf-8")).hexdigest()[:16]


def _impact_heuristic(text: str, source_type: str) -> float:
    """프로덕션의 GPT03 market_post_score 자리. 여기선 간단 규칙.
    - [특징주] 포함 → 0.8
    - 공시 → 0.6
    - SNS (sns) → 0.5 (신호 강도)
    - 급등/급락/상승/하락 키워드 → +0.05씩
    """
    score = 0.3
    if "[특징주]" in text:
        score = max(score, 0.8)
    if source_type == "disclosure":
        score = max(score, 0.6)
    if source_type == "sns":
        score = max(score, 0.5)
    for kw in ("급등", "급락", "상승", "하락", "강세", "약세"):
        if kw in text:
            score = min(1.0, score + 0.05)
    return round(score, 3)


def upsert_ticker_master() -> int:
    # ticker_master 가 바뀌면 section_builder 의 alias 캐시도 무효화
    from ..l1_index.section_builder import reset_alias_cache
    reset_alias_cache()

    n = 0
    with tx() as conn:
        with (CFG.seed_dir / "tickers.csv").open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                aliases = [a.strip() for a in row["aliases"].split("|") if a.strip()]
                asset_type = (row.get("asset_type") or "stock").strip().lower()
                if asset_type not in {"stock", "etf"}:
                    asset_type = "stock"
                conn.execute(
                    """
                    INSERT INTO ticker_master
                    (ticker, name_ko, name_en, aliases_json, market, sector, asset_type, is_preferred)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(ticker) DO UPDATE SET
                        name_ko=excluded.name_ko,
                        name_en=excluded.name_en,
                        aliases_json=excluded.aliases_json,
                        market=excluded.market,
                        sector=excluded.sector,
                        asset_type=excluded.asset_type,
                        is_preferred=excluded.is_preferred
                    """,
                    (
                        row["ticker"],
                        row["name_ko"],
                        row.get("name_en") or None,
                        json.dumps(aliases, ensure_ascii=False),
                        row["market"],
                        row["sector"],
                        asset_type,
                        int(row.get("is_preferred", 0)),
                    ),
                )
                # tier: 시드에서는 일단 전부 lazy로 두고, compile 단계가 top-N 승격
                conn.execute(
                    "INSERT OR IGNORE INTO ticker_tier(ticker, tier) VALUES(?, 'lazy')",
                    (row["ticker"],),
                )
                n += 1
    return n


def ingest_raw() -> tuple[int, int]:
    """data/raw/**/*.md 전수 스캔 → source_registry + stock_event_timeline."""
    raw_root = CFG.data_dir / "raw"
    ingested_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    src_n = 0
    evt_n = 0
    touched: set[str] = set()

    with tx() as conn:
        for md_path in sorted(raw_root.rglob("*.md")):
            post = frontmatter.load(md_path)
            fm = post.metadata
            body = post.content
            ticker = fm.get("ticker")
            source_type = fm.get("source_type", "news")
            published_at = fm.get("published_at")
            if isinstance(published_at, datetime):
                published_at = published_at.isoformat()

            sid = _source_id(md_path)
            conn.execute(
                """
                INSERT OR REPLACE INTO source_registry
                (source_id, source_type, path, ticker, published_at, ingested_at, title, checksum)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    sid,
                    source_type,
                    str(md_path.relative_to(CFG.data_dir)),
                    ticker,
                    published_at,
                    ingested_at,
                    fm.get("title", ""),
                    _checksum(body),
                ),
            )
            src_n += 1

            # news/disclosure/sns는 모두 timeline 이벤트로 기록 (profile은 claim 입력만)
            if source_type in ("news", "disclosure", "sns") and ticker:
                event_payload = (
                    ticker,
                    source_type,
                    published_at or ingested_at,
                    fm.get("title", ""),
                    body[:500],
                    _impact_heuristic(body, source_type),
                    sid,
                )
                existing_evt = conn.execute(
                    "SELECT event_id FROM stock_event_timeline WHERE source_id=?",
                    (sid,),
                ).fetchone()
                if existing_evt:
                    conn.execute(
                        """
                        UPDATE stock_event_timeline
                        SET ticker=?, event_type=?, occurred_at=?, headline=?,
                            summary=?, impact_score=?
                        WHERE source_id=?
                        """,
                        event_payload,
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO stock_event_timeline
                        (ticker, event_type, occurred_at, headline, summary, impact_score, source_id)
                        VALUES(?,?,?,?,?,?,?)
                        """,
                        event_payload,
                    )
                evt_n += 1
                touched.add(ticker)

        # compile 트리거 큐
        for t in touched:
            conn.execute(
                "INSERT INTO page_touch_queue(ticker, reason, enqueued_at) VALUES(?,?,?)",
                (t, "ingest", ingested_at),
            )

    return src_n, evt_n


def main() -> None:
    init_db()
    nt = upsert_ticker_master()
    ns, ne = ingest_raw()
    print(f"[ingest] ticker_master upsert={nt}  sources={ns}  events={ne}")


if __name__ == "__main__":
    main()
