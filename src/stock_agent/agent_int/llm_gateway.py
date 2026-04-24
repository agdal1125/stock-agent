"""LLM Gateway — 모든 OpenAI 호출은 반드시 이 모듈을 경유.

프로덕션 등가:
  - APIM kill-switch (apim_killswitch)
  - LLM_REQUEST_RESPONSE 감사 로그
  - PROMPT_CATALOG 기반 prompt_id 기록

PoC 핵심 요구:
  1) LLM_KILL_SWITCH=1 이면 OpenAI 접근 없이 결정적 스텁 반환
  2) 모든 요청/응답이 SQLite `llm_io_log`에 기록
  3) 모델 ID는 하드코딩 금지 — CFG.openai_model 만 참조 (env로 주입)
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _repair_ssl_env() -> None:
    """Windows 사용자 환경에서 SSL_CERT_FILE이 존재하지 않는 경로를 가리키면
    certifi 번들로 교체. openai/httpx import 전에 실행되어야 함."""
    for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        p = os.environ.get(var)
        if p and not Path(p).is_file():
            try:
                import certifi
                os.environ[var] = certifi.where()
            except Exception:
                os.environ.pop(var, None)


_repair_ssl_env()

import numpy as np  # noqa: E402
from openai import OpenAI, OpenAIError  # noqa: E402

from ..config import CFG  # noqa: E402
from ..db import tx  # noqa: E402
from . import cost as cost_tracker  # noqa: E402


_client: OpenAI | None = None


def client() -> OpenAI:
    global _client
    if _client is None:
        if not CFG.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is not set (or use LLM_KILL_SWITCH=1)")
        _client = OpenAI(api_key=CFG.openai_api_key)
    return _client


def _extract_usage(resp) -> tuple[int, int, int]:
    """OpenAI chat.completions 응답에서 (prompt, completion, cached_prompt) 토큰 추출.
    스트리밍 마지막 chunk 는 usage 가 포함돼 있지 않을 수 있음 — 이 경우 (0,0,0)."""
    u = getattr(resp, "usage", None)
    if not u:
        return 0, 0, 0
    prompt = getattr(u, "prompt_tokens", 0) or 0
    completion = getattr(u, "completion_tokens", 0) or 0
    cached = 0
    details = getattr(u, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0
    return int(prompt), int(completion), int(cached)


def _record_cost(prompt_id: str, usage: tuple[int, int, int],
                 *, is_embedding: bool = False) -> float:
    p, c, cached = usage
    if p == 0 and c == 0:
        return 0.0
    try:
        return cost_tracker.record(
            prompt_id=prompt_id, model=CFG.openai_model,
            prompt_tokens=p, completion_tokens=c, cached_prompt_tokens=cached,
            is_embedding=is_embedding,
        )
    except Exception:
        return 0.0


def _log(prompt_id: str, request: dict, response: str | None, latency_ms: int,
         status: str, error: str | None = None) -> None:
    with tx() as conn:
        conn.execute(
            """INSERT INTO llm_io_log
               (called_at, prompt_id, model, request_json, response_text, latency_ms, status, error)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                prompt_id,
                CFG.openai_model,
                json.dumps(request, ensure_ascii=False)[:8000],
                (response or "")[:8000],
                latency_ms,
                status,
                error,
            ),
        )


# ----- stubs (kill switch 경로) ------------------------------------------------

_STUB_CHAT = {
    "claim_extract_v1": {
        "claims": [
            {"claim_text": "(stub) 회사 개요 주요 사실", "section_type": "profile", "confidence": 0.5}
        ]
    },
    "intent_classify_v1": {"intent": "latest_issue"},
    "answer_compose_v1": "[KILL_SWITCH on] 모의 답변입니다. 실제 응답은 OPENAI_API_KEY와 LLM_KILL_SWITCH=0로 받을 수 있습니다.",
}


def _stub_response(prompt_id: str) -> Any:
    return _STUB_CHAT.get(prompt_id, {"stub": True, "prompt_id": prompt_id})


# ----- public API --------------------------------------------------------------

def chat_text(prompt_id: str, system: str, user: str,
              temperature: float | None = None, max_tokens: int = 1200) -> str:
    req = {"prompt_id": prompt_id, "system": system[:2000], "user": user[:6000]}
    if CFG.kill_switch:
        out = _stub_response(prompt_id)
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        _log(prompt_id, req, text, 0, "blocked", "kill_switch")
        return text

    cost_tracker.ensure_budget()
    started = time.time()
    try:
        kwargs: dict[str, Any] = {
            "model": CFG.openai_model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_completion_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        resp = client().chat.completions.create(**kwargs)
        text = (resp.choices[0].message.content or "").strip()
        _log(prompt_id, req, text, int((time.time() - started) * 1000), "ok")
        _record_cost(prompt_id, _extract_usage(resp))
        return text
    except OpenAIError as e:
        _log(prompt_id, req, None, int((time.time() - started) * 1000), "error", str(e))
        raise


def chat_stream(prompt_id: str, system: str, user: str,
                max_tokens: int = 700):
    """토큰 단위로 yield 하는 스트리밍 생성기.
    kill-switch 모드면 스텁 전체를 한 번에 yield."""
    req = {"prompt_id": prompt_id, "system": system[:2000], "user": user[:6000], "stream": True}
    if CFG.kill_switch:
        text = "[KILL_SWITCH on] 모의 답변입니다. 실제 응답은 OPENAI_API_KEY와 LLM_KILL_SWITCH=0로 받을 수 있습니다."
        _log(prompt_id, req, text, 0, "blocked", "kill_switch")
        yield text
        return

    cost_tracker.ensure_budget()
    started = time.time()
    full_text = ""
    last_usage = (0, 0, 0)
    try:
        stream = client().chat.completions.create(
            model=CFG.openai_model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_completion_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        for chunk in stream:
            # 마지막 usage-only chunk 는 choices 가 빈 리스트 — usage 를 먼저 확보
            chunk_usage = _extract_usage(chunk)
            if any(chunk_usage):
                last_usage = chunk_usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content or ""
            if delta:
                full_text += delta
                yield delta
        _log(prompt_id, req, full_text, int((time.time() - started) * 1000), "ok")
        _record_cost(prompt_id, last_usage)
    except OpenAIError as e:
        _log(prompt_id, req, full_text, int((time.time() - started) * 1000),
             "error", str(e))
        raise


def chat_json(prompt_id: str, system: str, user: str, schema_hint: str | None = None) -> dict:
    sys = system
    if schema_hint:
        sys = system + f"\n\n출력은 반드시 valid JSON. 스키마 힌트:\n{schema_hint}"

    if CFG.kill_switch:
        out = _stub_response(prompt_id)
        if isinstance(out, dict):
            _log(prompt_id, {"prompt_id": prompt_id, "system": sys[:2000], "user": user[:6000]},
                 json.dumps(out, ensure_ascii=False), 0, "blocked", "kill_switch")
            return out
        return {"stub": out}

    req = {"prompt_id": prompt_id, "system": sys[:2000], "user": user[:6000]}
    cost_tracker.ensure_budget()
    started = time.time()
    try:
        resp = client().chat.completions.create(
            model=CFG.openai_model,
            messages=[{"role": "system", "content": sys},
                      {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            max_completion_tokens=1400,
        )
        text = (resp.choices[0].message.content or "").strip()
        _log(prompt_id, req, text, int((time.time() - started) * 1000), "ok")
        _record_cost(prompt_id, _extract_usage(resp))
        return json.loads(text)
    except OpenAIError as e:
        _log(prompt_id, req, None, int((time.time() - started) * 1000), "error", str(e))
        raise
    except json.JSONDecodeError as e:
        _log(prompt_id, req, text if 'text' in locals() else None,
             int((time.time() - started) * 1000), "error", f"json_decode: {e}")
        raise


def embed(texts: list[str]) -> np.ndarray:
    """텍스트 배치 → float32 임베딩 행렬 (N, D). kill switch 경로는 결정적 해시기반 벡터."""
    if CFG.kill_switch:
        import hashlib
        dim = 256
        arrs: list[np.ndarray] = []
        for t in texts:
            h = hashlib.sha256(t.encode("utf-8")).digest()
            seed = int.from_bytes(h[:8], "big") % (2**32)
            arrs.append(np.random.default_rng(seed).standard_normal(dim).astype(np.float32))
        m = np.vstack(arrs) if arrs else np.zeros((0, dim), dtype=np.float32)
        if m.size:
            norms = np.linalg.norm(m, axis=1, keepdims=True) + 1e-9
            m = m / norms
        _log("embed", {"texts_n": len(texts), "dim": dim, "mode": "stub"},
             "stub", 0, "blocked", "kill_switch")
        return m

    cost_tracker.ensure_budget()
    started = time.time()
    try:
        resp = client().embeddings.create(model=CFG.openai_embed_model, input=texts)
        vecs = np.asarray([d.embedding for d in resp.data], dtype=np.float32)
        # OpenAI 임베딩은 이미 L2 정규화 상태에 근접하지만 안전하게 재정규화
        norms = np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
        vecs = vecs / norms
        _log("embed", {"texts_n": len(texts), "dim": int(vecs.shape[1])},
             f"[{len(texts)} vecs]", int((time.time() - started) * 1000), "ok")
        u = getattr(resp, "usage", None)
        p_tok = getattr(u, "prompt_tokens", 0) or 0 if u else 0
        _record_cost("embed", (p_tok, 0, 0), is_embedding=True)
        return vecs
    except OpenAIError as e:
        _log("embed", {"texts_n": len(texts)}, None, int((time.time() - started) * 1000),
             "error", str(e))
        raise
