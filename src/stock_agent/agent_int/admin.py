"""편집(admin) REST API — `/api/v1/auth/...`, `/api/v1/admin/...`.

A+B 조합 구현
-------------
- **A) Curated Facts CRUD** — `seed/wiki_facts.csv` 행을 추가/수정/삭제.
  편집 후 해당 ticker 의 `lazy_compile` 자동 트리거 → wiki/*.md 즉시 갱신.
- **B) Claim Approval** — DB `stock_claim` 테이블의 review_state 를
  `pending → approved/rejected` 로 전환, claim_text 수정 후 승인도 가능.

키 (curated facts 행 식별자) 는 **(ticker, section_type, claim_text)** 복합키.
CSV 에 stable id 가 없어 가장 자연스러운 자연키를 사용. 동일 텍스트 중복 행은
허용하지 않음 (renderer 가 어차피 dedup).
"""
from __future__ import annotations

import csv
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError

from ..compile.run import lazy_compile
from ..config import CFG
from ..db import tx
from ..l1_index.section_builder import (
    SECTION_TYPES, _load_curated_facts,
)
from . import auth as auth_mod


# ============================================================================
# 라우터
# ============================================================================

auth_router = APIRouter(prefix="/api/v1/auth", tags=["admin-auth"])
admin_router = APIRouter(
    prefix="/api/v1/admin", tags=["admin"],
    dependencies=[Depends(auth_mod.require_admin)],
)


# ============================================================================
# Pydantic 모델
# ============================================================================

class LoginRequest(BaseModel):
    password: str = Field(..., min_length=1)


class LoginResponse(BaseModel):
    token: str
    expires_at: float
    expires_at_iso: str


class LogoutResponse(BaseModel):
    revoked: bool


class FactRow(BaseModel):
    ticker: str = Field(..., pattern=r"^\d{6}$")
    section_type: str
    claim_text: str = Field(..., min_length=2, max_length=400)
    confidence: float = Field(0.95, ge=0.0, le=1.0)
    source_label: str = "Curated"
    source_url: str = ""


class FactRowOut(FactRow):
    row_index: int = Field(..., description="CSV 내부 0-base 행 번호 (현재 시점 기준)")


class FactListResponse(BaseModel):
    count: int
    items: list[FactRowOut]


class FactWriteResponse(BaseModel):
    ok: bool = True
    ticker: str
    recompiled_sections: int
    operation: str            # "added" | "updated" | "deleted"
    row: FactRowOut | None = None


class ClaimItem(BaseModel):
    claim_id: int
    ticker: str
    section_type: str
    claim_text: str
    confidence: float
    review_state: str          # pending | approved | rejected
    source_id: str | None = None
    created_at: str | None = None


class ClaimListResponse(BaseModel):
    count: int
    items: list[ClaimItem]


class ClaimUpdateRequest(BaseModel):
    """승인 시 본문 수정도 허용 (LLM 추출이 약간 어색할 때)."""
    claim_text: str | None = None
    confidence: float | None = Field(None, ge=0.0, le=1.0)


class ClaimWriteResponse(BaseModel):
    ok: bool = True
    claim_id: int
    new_state: str
    ticker: str
    recompiled_sections: int


# ============================================================================
# 인증
# ============================================================================

@auth_router.get("/status",
                 summary="admin 활성화 여부 + 현재 활성 세션 수")
def auth_status() -> dict[str, Any]:
    return {
        "enabled": auth_mod.is_admin_enabled(),
        "active_sessions": auth_mod.session_count(),
    }


@auth_router.post("/login", response_model=LoginResponse,
                  summary="비밀번호 → 세션 토큰 발급")
def auth_login(req: LoginRequest) -> LoginResponse:
    token, expires_at = auth_mod.login(req.password)
    return LoginResponse(
        token=token,
        expires_at=expires_at,
        expires_at_iso=datetime.fromtimestamp(
            expires_at, tz=timezone.utc).isoformat(timespec="seconds"),
    )


@auth_router.post("/logout", response_model=LogoutResponse,
                  summary="현재 토큰 즉시 무효화")
def auth_logout(
    body: dict = Body(..., examples=[{"token": "..."}]),
) -> LogoutResponse:
    token = (body or {}).get("token", "").strip()
    if not token:
        raise HTTPException(400, "token 필드가 필요합니다")
    return LogoutResponse(revoked=auth_mod.logout(token))


# ============================================================================
# Helpers
# ============================================================================

_FACTS_FIELDS = ["ticker", "section_type", "claim_text",
                 "confidence", "source_label", "source_url"]


def _facts_path() -> Path:
    return CFG.seed_dir / "wiki_facts.csv"


def _read_facts() -> list[dict]:
    p = _facts_path()
    if not p.is_file():
        return []
    with p.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _atomic_write_facts(rows: list[dict]) -> None:
    """tmp → rename 으로 안전 저장. 동일 디스크 안에서만 동작."""
    p = _facts_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=".wiki_facts.", suffix=".csv",
                                        dir=str(p.parent))
    try:
        with open(tmp_fd, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=_FACTS_FIELDS,
                               quoting=csv.QUOTE_MINIMAL)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in _FACTS_FIELDS})
        shutil.move(tmp_name, p)
    except Exception:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except Exception:
            pass
        raise
    # renderer 의 lru_cache 를 비워야 다음 compile_ticker 가 새 CSV 를 읽음
    try:
        _load_curated_facts.cache_clear()
    except Exception:
        pass


def _validate_section_type(section_type: str) -> None:
    if section_type not in SECTION_TYPES:
        raise HTTPException(
            400,
            f"section_type 은 다음 중 하나여야 합니다: {list(SECTION_TYPES)}",
        )


def _validate_ticker_known(ticker: str) -> None:
    with tx() as conn:
        row = conn.execute(
            "SELECT 1 FROM ticker_master WHERE ticker=?", (ticker,),
        ).fetchone()
    if not row:
        raise HTTPException(404, f"ticker_master 에 없는 종목입니다: {ticker}")


def _row_match(row: dict, ticker: str, section_type: str, claim_text: str) -> bool:
    return (
        (row.get("ticker") or "").strip() == ticker
        and (row.get("section_type") or "").strip() == section_type
        and (row.get("claim_text") or "").strip() == claim_text.strip()
    )


def _recompile(ticker: str) -> int:
    return lazy_compile(ticker)


# ============================================================================
# A) Curated Facts CRUD
# ============================================================================

@admin_router.get("/facts", response_model=FactListResponse,
                  summary="curated facts 행 리스트 (필터)")
def admin_list_facts(
    ticker: str | None = None,
    section_type: str | None = None,
) -> FactListResponse:
    rows = _read_facts()
    out: list[FactRowOut] = []
    for i, r in enumerate(rows):
        if ticker and (r.get("ticker") or "") != ticker:
            continue
        if section_type and (r.get("section_type") or "") != section_type:
            continue
        try:
            out.append(FactRowOut(
                row_index=i,
                ticker=(r.get("ticker") or "").strip(),
                section_type=(r.get("section_type") or "").strip(),
                claim_text=(r.get("claim_text") or "").strip(),
                confidence=float(r.get("confidence") or 0.95),
                source_label=(r.get("source_label") or "").strip() or "Curated",
                source_url=(r.get("source_url") or "").strip(),
            ))
        except (ValidationError, ValueError):
            continue
    return FactListResponse(count=len(out), items=out)


@admin_router.post("/facts", response_model=FactWriteResponse,
                   summary="curated fact 추가 (해당 ticker 자동 recompile)")
def admin_add_fact(req: FactRow) -> FactWriteResponse:
    _validate_section_type(req.section_type)
    _validate_ticker_known(req.ticker)
    rows = _read_facts()
    for r in rows:
        if _row_match(r, req.ticker, req.section_type, req.claim_text):
            raise HTTPException(
                409, "이미 동일한 (ticker, section_type, claim_text) 행이 존재합니다",
            )
    new_row = {
        "ticker": req.ticker,
        "section_type": req.section_type,
        "claim_text": req.claim_text.strip(),
        "confidence": f"{req.confidence:.2f}".rstrip("0").rstrip(".") or "0",
        "source_label": req.source_label or "Curated",
        "source_url": req.source_url or "",
    }
    rows.append(new_row)
    _atomic_write_facts(rows)
    n = _recompile(req.ticker)
    return FactWriteResponse(
        ok=True, ticker=req.ticker, recompiled_sections=n,
        operation="added",
        row=FactRowOut(row_index=len(rows) - 1, **req.model_dump()),
    )


class FactUpdateRequest(BaseModel):
    """기존 행을 (ticker, section_type, claim_text) 복합키로 찾아 수정.

    `new_*` 필드가 None 이면 기존 값 유지.
    """
    ticker: str = Field(..., pattern=r"^\d{6}$")
    section_type: str
    claim_text: str = Field(..., description="수정 *대상* 행을 찾는 키")
    new_claim_text: str | None = None
    new_confidence: float | None = Field(None, ge=0.0, le=1.0)
    new_source_label: str | None = None
    new_source_url: str | None = None


@admin_router.put("/facts", response_model=FactWriteResponse,
                  summary="curated fact 수정 (자연키로 식별)")
def admin_update_fact(req: FactUpdateRequest) -> FactWriteResponse:
    _validate_section_type(req.section_type)
    _validate_ticker_known(req.ticker)
    rows = _read_facts()
    target_idx: int | None = None
    for i, r in enumerate(rows):
        if _row_match(r, req.ticker, req.section_type, req.claim_text):
            target_idx = i
            break
    if target_idx is None:
        raise HTTPException(404, "수정할 fact 행을 찾지 못했습니다 (자연키 불일치)")

    row = dict(rows[target_idx])
    if req.new_claim_text is not None:
        new_text = req.new_claim_text.strip()
        if len(new_text) < 2:
            raise HTTPException(400, "new_claim_text 는 2자 이상이어야 합니다")
        # 동일 키로 중복 안 생기도록 체크
        for j, r in enumerate(rows):
            if j == target_idx:
                continue
            if _row_match(r, req.ticker, req.section_type, new_text):
                raise HTTPException(
                    409, "수정 결과가 다른 기존 행과 키가 같아집니다",
                )
        row["claim_text"] = new_text
    if req.new_confidence is not None:
        row["confidence"] = f"{req.new_confidence:.2f}".rstrip("0").rstrip(".") or "0"
    if req.new_source_label is not None:
        row["source_label"] = req.new_source_label.strip() or "Curated"
    if req.new_source_url is not None:
        row["source_url"] = req.new_source_url.strip()
    rows[target_idx] = row

    _atomic_write_facts(rows)
    n = _recompile(req.ticker)
    return FactWriteResponse(
        ok=True, ticker=req.ticker, recompiled_sections=n,
        operation="updated",
        row=FactRowOut(
            row_index=target_idx,
            ticker=row["ticker"],
            section_type=row["section_type"],
            claim_text=row["claim_text"],
            confidence=float(row.get("confidence") or 0.95),
            source_label=row.get("source_label") or "Curated",
            source_url=row.get("source_url") or "",
        ),
    )


class FactDeleteRequest(BaseModel):
    ticker: str = Field(..., pattern=r"^\d{6}$")
    section_type: str
    claim_text: str


@admin_router.delete("/facts", response_model=FactWriteResponse,
                     summary="curated fact 삭제 (자연키로 식별)")
def admin_delete_fact(req: FactDeleteRequest) -> FactWriteResponse:
    _validate_section_type(req.section_type)
    _validate_ticker_known(req.ticker)
    rows = _read_facts()
    new_rows = [r for r in rows
                if not _row_match(r, req.ticker, req.section_type, req.claim_text)]
    if len(new_rows) == len(rows):
        raise HTTPException(404, "삭제할 fact 행을 찾지 못했습니다")
    _atomic_write_facts(new_rows)
    n = _recompile(req.ticker)
    return FactWriteResponse(
        ok=True, ticker=req.ticker, recompiled_sections=n,
        operation="deleted", row=None,
    )


# ============================================================================
# B) Claim Approval (DB)
# ============================================================================

@admin_router.get("/claims", response_model=ClaimListResponse,
                  summary="stock_claim 리스트 (state 필터)")
def admin_list_claims(
    state: str = "pending",
    ticker: str | None = None,
    limit: int = 200,
) -> ClaimListResponse:
    if state not in {"pending", "approved", "rejected", "all"}:
        raise HTTPException(400, "state 는 pending|approved|rejected|all")
    sql = (
        "SELECT claim_id, ticker, section_type, claim_text, confidence, "
        "review_state, source_id, created_at FROM stock_claim WHERE 1=1"
    )
    params: list[Any] = []
    if state != "all":
        sql += " AND review_state=?"
        params.append(state)
    if ticker:
        sql += " AND ticker=?"
        params.append(ticker)
    sql += " ORDER BY created_at DESC, claim_id DESC LIMIT ?"
    params.append(int(limit))
    with tx() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    items = [ClaimItem(**dict(r)) for r in rows]
    return ClaimListResponse(count=len(items), items=items)


def _set_claim_state(claim_id: int, new_state: str,
                     update: ClaimUpdateRequest | None = None) -> tuple[str, str]:
    """리턴: (ticker, current_text). 없는 claim 이면 404."""
    with tx() as conn:
        row = conn.execute(
            "SELECT ticker, claim_text FROM stock_claim WHERE claim_id=?",
            (claim_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"claim_id={claim_id} 가 존재하지 않습니다")

        new_text: str | None = None
        new_conf: float | None = None
        if update:
            if update.claim_text is not None:
                txt = update.claim_text.strip()
                if len(txt) < 2:
                    raise HTTPException(400, "claim_text 는 2자 이상")
                new_text = txt
            if update.confidence is not None:
                new_conf = update.confidence

        if new_text is not None and new_conf is not None:
            conn.execute(
                "UPDATE stock_claim SET review_state=?, claim_text=?, confidence=? "
                "WHERE claim_id=?",
                (new_state, new_text, new_conf, claim_id),
            )
        elif new_text is not None:
            conn.execute(
                "UPDATE stock_claim SET review_state=?, claim_text=? WHERE claim_id=?",
                (new_state, new_text, claim_id),
            )
        elif new_conf is not None:
            conn.execute(
                "UPDATE stock_claim SET review_state=?, confidence=? WHERE claim_id=?",
                (new_state, new_conf, claim_id),
            )
        else:
            conn.execute(
                "UPDATE stock_claim SET review_state=? WHERE claim_id=?",
                (new_state, claim_id),
            )
    return row["ticker"], (new_text if new_text is not None else row["claim_text"])


@admin_router.post("/claims/{claim_id}/approve",
                   response_model=ClaimWriteResponse,
                   summary="claim 승인 (선택: 본문/신뢰도 동시 수정)")
def admin_approve_claim(
    claim_id: int,
    update: ClaimUpdateRequest | None = None,
) -> ClaimWriteResponse:
    ticker, _ = _set_claim_state(claim_id, "approved", update)
    n = _recompile(ticker)
    return ClaimWriteResponse(
        ok=True, claim_id=claim_id, new_state="approved",
        ticker=ticker, recompiled_sections=n,
    )


@admin_router.post("/claims/{claim_id}/reject",
                   response_model=ClaimWriteResponse,
                   summary="claim 거절")
def admin_reject_claim(claim_id: int) -> ClaimWriteResponse:
    ticker, _ = _set_claim_state(claim_id, "rejected", None)
    n = _recompile(ticker)
    return ClaimWriteResponse(
        ok=True, claim_id=claim_id, new_state="rejected",
        ticker=ticker, recompiled_sections=n,
    )


@admin_router.put("/claims/{claim_id}",
                  response_model=ClaimWriteResponse,
                  summary="claim 본문/신뢰도만 수정 (state 유지)")
def admin_update_claim_text(
    claim_id: int, update: ClaimUpdateRequest,
) -> ClaimWriteResponse:
    if update.claim_text is None and update.confidence is None:
        raise HTTPException(400, "claim_text 또는 confidence 중 최소 하나가 필요합니다")
    with tx() as conn:
        row = conn.execute(
            "SELECT ticker, review_state FROM stock_claim WHERE claim_id=?",
            (claim_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"claim_id={claim_id} 없음")
    ticker, _ = _set_claim_state(claim_id, row["review_state"], update)
    n = _recompile(ticker) if row["review_state"] == "approved" else 0
    return ClaimWriteResponse(
        ok=True, claim_id=claim_id, new_state=row["review_state"],
        ticker=ticker, recompiled_sections=n,
    )


__all__ = ["auth_router", "admin_router"]
