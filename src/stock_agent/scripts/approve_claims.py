"""CLI surrogate for 관리자 대시보드 claim review.

용법:
  python -m stock_agent.scripts.approve_claims list          # pending 보기
  python -m stock_agent.scripts.approve_claims approve <id>  # 승인
  python -m stock_agent.scripts.approve_claims reject  <id>  # 반려
  python -m stock_agent.scripts.approve_claims approve-all   # 모든 pending 승인 (PoC 편의)
"""
from __future__ import annotations

import sys

from ..db import tx


def cmd_list(limit: int = 20) -> None:
    with tx() as conn:
        rows = conn.execute(
            """SELECT claim_id, ticker, section_type, confidence, claim_text
               FROM stock_claim WHERE review_state='pending'
               ORDER BY ticker, section_type, confidence DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    for r in rows:
        print(f"{r['claim_id']:>5}  {r['ticker']}  [{r['section_type']:<13}] "
              f"conf={r['confidence']:.2f}  {r['claim_text']}")
    print(f"-- {len(rows)} pending (cap={limit}) --")


def cmd_set(claim_id: int, state: str) -> None:
    with tx() as conn:
        conn.execute("UPDATE stock_claim SET review_state=? WHERE claim_id=?",
                     (state, claim_id))
    print(f"claim {claim_id} -> {state}")


def cmd_approve_all() -> None:
    with tx() as conn:
        n = conn.execute(
            "UPDATE stock_claim SET review_state='approved' WHERE review_state='pending'"
        ).rowcount
    print(f"approved {n} claims")


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    cmd = argv[0]
    if cmd == "list":
        cmd_list(int(argv[1]) if len(argv) > 1 else 20)
    elif cmd == "approve" and len(argv) == 2:
        cmd_set(int(argv[1]), "approved")
    elif cmd == "reject" and len(argv) == 2:
        cmd_set(int(argv[1]), "rejected")
    elif cmd == "approve-all":
        cmd_approve_all()
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
