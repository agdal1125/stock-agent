"""비용 집계·일일 USD 상한 · kill-switch 자동 전환 (P7).

컴포넌트:
  - record(...)        : 호출당 토큰·비용 저장 (llm_cost_log)
  - compute_cost(...)  : 단가 × 토큰으로 USD 환산
  - today_usd/month_usd: 누적 사용량
  - over_daily_cap()   : 상한 초과 여부
  - ensure_budget()    : llm_gateway 호출 직전 게이트 (초과 시 RuntimeError)
"""
from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any

from ..config import CFG
from ..db import tx


_INIT_LOCK = Lock()
_INITED = False


def _ensure_table() -> None:
    global _INITED
    if _INITED:
        return
    with _INIT_LOCK:
        if _INITED:
            return
        with tx() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_cost_log (
                  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                  called_at            TEXT NOT NULL,
                  day                  TEXT NOT NULL,
                  month                TEXT NOT NULL,
                  prompt_id            TEXT,
                  model                TEXT,
                  prompt_tokens        INTEGER NOT NULL DEFAULT 0,
                  completion_tokens    INTEGER NOT NULL DEFAULT 0,
                  cached_prompt_tokens INTEGER NOT NULL DEFAULT 0,
                  cost_usd             REAL NOT NULL DEFAULT 0
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cost_day ON llm_cost_log(day)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cost_month ON llm_cost_log(month)")
        _INITED = True


def compute_cost(
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_prompt_tokens: int = 0,
    *,
    is_embedding: bool = False,
) -> float:
    if is_embedding:
        return (prompt_tokens / 1_000_000) * CFG.price_embed_per_m
    fresh_in = max(0, prompt_tokens - cached_prompt_tokens)
    return (
        (fresh_in / 1_000_000) * CFG.price_input_per_m
        + (cached_prompt_tokens / 1_000_000) * CFG.price_cached_input_per_m
        + (completion_tokens / 1_000_000) * CFG.price_output_per_m
    )


def _now_parts() -> tuple[str, str, str]:
    now = datetime.now(timezone.utc)
    return (
        now.isoformat(timespec="seconds"),
        now.strftime("%Y-%m-%d"),
        now.strftime("%Y-%m"),
    )


def record(
    *,
    prompt_id: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_prompt_tokens: int = 0,
    is_embedding: bool = False,
) -> float:
    """호출당 사용량을 저장하고 비용(USD)을 반환."""
    _ensure_table()
    cost = compute_cost(
        prompt_tokens, completion_tokens, cached_prompt_tokens,
        is_embedding=is_embedding,
    )
    if prompt_tokens == 0 and completion_tokens == 0:
        return 0.0
    called_at, day, month = _now_parts()
    with tx() as conn:
        conn.execute(
            """INSERT INTO llm_cost_log
               (called_at, day, month, prompt_id, model,
                prompt_tokens, completion_tokens, cached_prompt_tokens, cost_usd)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (called_at, day, month, prompt_id, model,
             int(prompt_tokens), int(completion_tokens), int(cached_prompt_tokens),
             float(cost)),
        )
    return cost


def _usd_between(col: str, value: str) -> float:
    _ensure_table()
    with tx() as conn:
        row = conn.execute(
            f"SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_cost_log WHERE {col}=?",
            (value,),
        ).fetchone()
    return float(row["s"] or 0.0)


def today_usd() -> float:
    _, day, _ = _now_parts()
    return _usd_between("day", day)


def month_usd() -> float:
    _, _, month = _now_parts()
    return _usd_between("month", month)


def summary() -> dict[str, Any]:
    _, day, month = _now_parts()
    t = today_usd()
    m = month_usd()
    cap = CFG.daily_usd_cap
    remaining = max(0.0, cap - t) if cap > 0 else None
    pct = (t / cap * 100) if cap > 0 else None
    return {
        "today_usd": round(t, 4),
        "month_usd": round(m, 4),
        "daily_cap_usd": cap,
        "daily_remaining_usd": round(remaining, 4) if remaining is not None else None,
        "daily_pct_used": round(pct, 2) if pct is not None else None,
        "kill_switch": CFG.kill_switch,
        "over_cap": bool(cap > 0 and t >= cap),
        "day": day,
        "month": month,
        "pricing": {
            "input_per_m_usd":        CFG.price_input_per_m,
            "output_per_m_usd":       CFG.price_output_per_m,
            "cached_input_per_m_usd": CFG.price_cached_input_per_m,
            "embed_per_m_usd":        CFG.price_embed_per_m,
        },
    }


def over_daily_cap() -> bool:
    if CFG.daily_usd_cap <= 0:
        return False
    return today_usd() >= CFG.daily_usd_cap


class BudgetExceeded(RuntimeError):
    """일일 상한 초과 — LLM 호출 차단 (kill-switch 자동 전환)."""


def ensure_budget() -> None:
    """LLM 호출 직전 호출 — 상한 초과면 예외."""
    if over_daily_cap():
        raise BudgetExceeded(
            f"daily_usd_cap {CFG.daily_usd_cap} 초과: 오늘 사용액 {today_usd():.4f} USD. "
            "LLM 호출이 자동 차단되었습니다."
        )
