"""편집(admin) 엔드포인트용 단순 토큰 인증.

설계
-----
- PoC 수준: env `WIKI_EDIT_PASSWORD` 와 평문 비교 (HTTPS 종단은 상위 프록시
  에서 책임). 운영에선 IdP (OIDC) 로 교체할 자리.
- 토큰 발급: `secrets.token_urlsafe(32)`. 프로세스 메모리 dict 에 보관 +
  TTL (`WIKI_EDIT_SESSION_TTL`, 기본 8시간). 프로세스 재시작 시 만료.
- `WIKI_EDIT_PASSWORD` 미설정이면 admin 라우트 전체가 503 으로 응답 →
  실수로 비번 없이 노출되는 일을 막음.
"""
from __future__ import annotations

import hmac
import os
import secrets
import time
from typing import NamedTuple

from fastapi import Depends, Header, HTTPException, status


_DEFAULT_TTL_SEC = int(os.getenv("WIKI_EDIT_SESSION_TTL", "28800"))   # 8h


def _password() -> str | None:
    """편집 비번. None 이면 admin 기능 비활성화."""
    pw = os.getenv("WIKI_EDIT_PASSWORD", "").strip()
    return pw or None


class _Session(NamedTuple):
    expires_at: float
    label: str


# token → Session
_SESSIONS: dict[str, _Session] = {}


def _purge_expired(now: float | None = None) -> None:
    now = now if now is not None else time.time()
    expired = [t for t, s in _SESSIONS.items() if s.expires_at < now]
    for t in expired:
        _SESSIONS.pop(t, None)


def is_admin_enabled() -> bool:
    return _password() is not None


def login(password: str, label: str = "admin",
          ttl_sec: int | None = None) -> tuple[str, float]:
    """비밀번호 검증 → 토큰 발급.

    Returns: (token, expires_at_unix_seconds)
    """
    expected = _password()
    if expected is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "admin 편집 기능이 활성화되지 않았습니다 (WIKI_EDIT_PASSWORD 미설정)",
        )
    # 길이만 같지 않더라도 compare_digest 는 안전. UTF-8 인코딩 후 비교.
    if not hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "비밀번호가 올바르지 않습니다")

    ttl = ttl_sec if ttl_sec is not None else _DEFAULT_TTL_SEC
    token = secrets.token_urlsafe(32)
    expires_at = time.time() + ttl
    _SESSIONS[token] = _Session(expires_at=expires_at, label=label)
    _purge_expired()
    return token, expires_at


def logout(token: str) -> bool:
    return _SESSIONS.pop(token, None) is not None


def session_count() -> int:
    _purge_expired()
    return len(_SESSIONS)


def _extract_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.strip().split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None


def require_admin(
    authorization: str | None = Header(default=None),
) -> _Session:
    """FastAPI dependency. Authorization: Bearer <token> 검증."""
    if _password() is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "admin 편집 기능이 활성화되지 않았습니다",
        )
    token = _extract_token(authorization)
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Authorization: Bearer <token> 헤더가 필요합니다",
        )
    sess = _SESSIONS.get(token)
    if sess is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "유효하지 않은 토큰입니다 (재로그인 필요)",
        )
    if sess.expires_at < time.time():
        _SESSIONS.pop(token, None)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "세션이 만료되었습니다 (재로그인 필요)",
        )
    return sess


__all__ = [
    "is_admin_enabled", "login", "logout", "session_count",
    "require_admin",
]
