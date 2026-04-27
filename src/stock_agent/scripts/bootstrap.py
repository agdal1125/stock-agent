"""end-to-end bootstrap:
  schema → ingest raw → extract claims → promote top-N → compile eager → embed.

추가 단계: ticker_master 에 등록됐지만 아직 wiki 파일이 없는 종목(예: 새로 추가된
ETF — raw .md 가 없어 page_touch_queue 에 안 올라가는 케이스)을 마지막에
보강 컴파일한다. 이 덕분에 seed/tickers.csv + seed/wiki_facts.csv 만 추가하면
새 종목/ETF 가 자동으로 wiki 에 반영된다.
"""
from __future__ import annotations

from .. import l0_canonical
from ..compile.run import lazy_compile, run_eager_pipeline
from ..db import init_db, tx
from ..l0_canonical.claim_extract import run as extract_claims
from ..l0_canonical.ingest import ingest_raw, upsert_ticker_master


def _compile_missing_wiki() -> list[str]:
    """ticker_master 에 있지만 section_doc 가 비어 있는 종목을 lazy_compile.
    raw .md 이 없는 ETF 같은 케이스를 위한 안전망."""
    with tx() as conn:
        rows = conn.execute(
            """SELECT tm.ticker FROM ticker_master tm
               LEFT JOIN section_doc sd ON sd.ticker = tm.ticker
               GROUP BY tm.ticker
               HAVING COUNT(sd.doc_id) = 0"""
        ).fetchall()
    missing = [r["ticker"] for r in rows]
    for t in missing:
        lazy_compile(t)
    return missing


def main() -> None:
    init_db()
    nt = upsert_ticker_master()
    ns, ne = ingest_raw()
    print(f"[bootstrap] ticker_master={nt}  sources={ns}  events={ne}")

    total, approved = extract_claims(auto_approve=True)
    print(f"[bootstrap] claims inserted={total} auto_approved={approved}")

    summary = run_eager_pipeline()
    print(f"[bootstrap] compile: {summary}")

    missing = _compile_missing_wiki()
    if missing:
        print(f"[bootstrap] backfill compiled (no raw .md): {missing}")

    _ = l0_canonical  # silence unused


if __name__ == "__main__":
    main()
