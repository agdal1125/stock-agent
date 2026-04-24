"""회귀 평가 러너.

3가지 결정적 메트릭 + 1가지 선택적 메트릭:
  1) ticker_accuracy      — resolver가 expected ticker를 top-1로 뽑았는가
  2) intent_accuracy      — router가 expected intent를 맞췄는가
  3) citation_coverage    — section_hits 중 최소 1개가 expected ticker에 걸렸는가
  4) keyword_recall       — 최종 답변 안에 expect_any_keywords 중 최소 1개 등장 (optional)

프로덕션에서는 여기에 numeric accuracy / freshness check / LLM-judge 추가.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from ..agent_int.answer import answer
from ..agent_int.router import route
from ..entity.resolver import resolve


GOLDEN_PATH = Path(__file__).parent / "golden_set.jsonl"


@dataclass
class CaseResult:
    id: str
    q: str
    expected_ticker: str | None
    got_ticker: str | None
    ticker_ok: bool
    expected_intent: str
    got_intent: str
    intent_ok: bool
    citation_ok: bool
    keyword_ok: bool
    answer: str


def load_cases() -> list[dict]:
    with GOLDEN_PATH.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def eval_one(case: dict, run_llm: bool) -> CaseResult:
    q = case["q"]
    # resolver
    hits = resolve(q)
    got_ticker = hits[0].ticker.code if hits and hits[0].score >= 80.0 else None
    ticker_ok = got_ticker == case.get("expect_ticker")

    # router
    got_intent = route(q).intent
    intent_ok = got_intent == case.get("expect_intent", "generic")

    citation_ok = False
    keyword_ok = False
    answer_text = ""

    if run_llm and case.get("expect_ticker"):
        trace = answer(q)
        answer_text = trace.answer
        cited_tickers = {h["ticker"] for h in trace.section_hits}
        citation_ok = case["expect_ticker"] in cited_tickers
        kws = case.get("expect_any_keywords") or []
        keyword_ok = any(kw in answer_text for kw in kws) if kws else True
    elif not case.get("expect_ticker"):
        # negative case: ticker 미확인이 기대 동작
        citation_ok = got_ticker is None
        keyword_ok = True

    return CaseResult(
        id=case["id"], q=q,
        expected_ticker=case.get("expect_ticker"),
        got_ticker=got_ticker,
        ticker_ok=ticker_ok,
        expected_intent=case.get("expect_intent", "generic"),
        got_intent=got_intent,
        intent_ok=intent_ok,
        citation_ok=citation_ok,
        keyword_ok=keyword_ok,
        answer=answer_text[:200],
    )


def run(run_llm: bool = True) -> dict[str, Any]:
    cases = load_cases()
    results = [eval_one(c, run_llm=run_llm) for c in cases]
    total = len(results)
    def pct(n: int) -> float:
        return round(100.0 * n / total, 1) if total else 0.0

    summary = {
        "total": total,
        "ticker_accuracy": pct(sum(r.ticker_ok for r in results)),
        "intent_accuracy": pct(sum(r.intent_ok for r in results)),
        "citation_coverage": pct(sum(r.citation_ok for r in results)),
        "keyword_recall": pct(sum(r.keyword_ok for r in results)),
    }
    return {"summary": summary, "cases": [asdict(r) for r in results]}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true",
                    help="resolver/router만 평가 (답변 생성 생략)")
    ap.add_argument("--out", default=None, help="결과 JSON 저장 경로")
    args = ap.parse_args()

    report = run(run_llm=not args.no_llm)
    print("=== summary ===")
    for k, v in report["summary"].items():
        print(f"  {k}: {v}")
    print("\n=== failures ===")
    for c in report["cases"]:
        if not (c["ticker_ok"] and c["intent_ok"] and c["citation_ok"] and c["keyword_ok"]):
            print(f"[{c['id']}] {c['q']}")
            print(f"   ticker: {c['got_ticker']} (expected {c['expected_ticker']}) ok={c['ticker_ok']}")
            print(f"   intent: {c['got_intent']} (expected {c['expected_intent']}) ok={c['intent_ok']}")
            print(f"   citation={c['citation_ok']} keyword={c['keyword_ok']}")
            if c["answer"]:
                print(f"   ans> {c['answer']}")

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[eval] report saved: {args.out}")


if __name__ == "__main__":
    main()
