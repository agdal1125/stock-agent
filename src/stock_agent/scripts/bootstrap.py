"""end-to-end bootstrap:
  schema → ingest raw → extract claims → promote top-N → compile eager → embed.
"""
from __future__ import annotations

from .. import l0_canonical
from ..compile.run import run_eager_pipeline
from ..db import init_db
from ..l0_canonical.claim_extract import run as extract_claims
from ..l0_canonical.ingest import ingest_raw, upsert_ticker_master


def main() -> None:
    init_db()
    nt = upsert_ticker_master()
    ns, ne = ingest_raw()
    print(f"[bootstrap] ticker_master={nt}  sources={ns}  events={ne}")

    total, approved = extract_claims(auto_approve=True)
    print(f"[bootstrap] claims inserted={total} auto_approved={approved}")

    summary = run_eager_pipeline()
    print(f"[bootstrap] compile: {summary}")
    _ = l0_canonical  # silence unused


if __name__ == "__main__":
    main()
