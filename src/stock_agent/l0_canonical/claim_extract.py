"""profile·news 본문에서 claim(사실 문장) 추출.

프로덕션 등가: Silver 레이어의 LLM 기반 구조화. GPT05~08(업종분류) 자리.
review_state='pending'으로 저장 → 승인 후에만 section_doc로 진입.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from ..config import CFG
from ..db import tx
from ..agent_int.llm_gateway import chat_json


CLAIM_PROMPT = """당신은 한국 주식시장 종목 팩트 추출기다.
주어진 텍스트에서 해당 종목에 대한 **검증 가능한 사실 문장**을 추출해 JSON 배열로 반환한다.

규칙:
- claim_text는 한 문장, 50자 내외
- section_type 은 다음 중 하나: profile | business | finance | relations | theme
  · profile     : 무엇을 하는 회사인가 (3줄 소개 수준, 사업 정의·소속 산업)
  · business    : 구체적 제품·서비스·고객·시장지위·주요 상품
  · finance     : 재무상태·매출·영업이익·실적·가이던스 등 수치성 언급
  · relations   : 경쟁사·협력사·계열·자회사·관련주 등 Entity 관계
  · theme       : 소속 테마·업종·섹터·정책 카테고리
- 주가 예측/투자 권유/정성 추정은 **추출 금지**
- 확신 낮으면 confidence 0.3~0.5, 강하면 0.7~0.9

출력 JSON schema: {"claims":[{"claim_text":"...","section_type":"...","confidence":0.7}]}
"""


def extract_claims_for_source(ticker: str, title: str, body: str) -> list[dict]:
    user = f"종목코드: {ticker}\n제목: {title}\n\n본문:\n{body[:3000]}"
    try:
        resp = chat_json(
            prompt_id="claim_extract_v1",
            system=CLAIM_PROMPT,
            user=user,
            schema_hint='{"claims":[{"claim_text":"str","section_type":"profile|business|risk|fundamentals|relation","confidence":0.0}]}',
        )
        claims = resp.get("claims", [])
        # 방어적 필터
        valid = []
        for c in claims:
            if not isinstance(c, dict):
                continue
            if not c.get("claim_text") or len(c["claim_text"]) < 5:
                continue
            if c.get("section_type") not in {"profile", "business", "finance", "relations", "theme"}:
                continue
            c["confidence"] = float(c.get("confidence", 0.5))
            valid.append(c)
        return valid
    except Exception as e:
        print(f"[claim_extract] fail {ticker}: {e}")
        return []


def run(auto_approve: bool = True) -> tuple[int, int]:
    """profile + news 전수 스캔 → claim 추출 → 저장.

    LLM 호출 루프와 DB write 루프를 분리한다 (SQLite 단일 writer 제약).
    auto_approve=True 인 경우 PoC 편의상 confidence >= 0.6 인 claim을
    approved로 바로 올린다. 프로덕션에서는 반드시 False + 관리자 대시보드 리뷰.
    """
    raw_root = CFG.data_dir / "raw"
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    # --- phase 1: LLM 추출 (DB 미터치) ---
    pending: list[tuple[str, dict]] = []
    for md_path in sorted(raw_root.rglob("*.md")):
        post = frontmatter.load(md_path)
        fm = post.metadata
        ticker = fm.get("ticker")
        st = fm.get("source_type")
        if not ticker or st not in ("profile", "news"):
            continue
        claims = extract_claims_for_source(ticker, fm.get("title", ""), post.content)
        for c in claims:
            pending.append((ticker, c))

    # --- phase 2: DB 일괄 삽입 ---
    total = 0
    approved = 0
    with tx() as conn:
        for ticker, c in pending:
            existing = conn.execute(
                "SELECT 1 FROM stock_claim WHERE ticker=? AND claim_text=? LIMIT 1",
                (ticker, c["claim_text"]),
            ).fetchone()
            if existing:
                continue
            state = "approved" if (auto_approve and c["confidence"] >= 0.6) else "pending"
            conn.execute(
                """
                INSERT INTO stock_claim
                (ticker, section_type, claim_text, source_id, confidence, review_state, created_at)
                VALUES(?,?,?,?,?,?,?)
                """,
                (ticker, c["section_type"], c["claim_text"], None,
                 c["confidence"], state, now),
            )
            total += 1
            if state == "approved":
                approved += 1

    return total, approved


if __name__ == "__main__":
    total, approved = run(auto_approve=True)
    print(f"[claim_extract] inserted={total}  auto_approved={approved}")
