"""wiki/*.md 파일을 읽는 유틸.

Wiki 본체는 파일이 source of truth. 이 모듈은 읽기 전용 헬퍼 집합.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from ..config import CFG


WIKI_ROOT = CFG.data_dir.parent / "wiki"     # <proj>/wiki/
TICKERS_ROOT = WIKI_ROOT / "tickers"
ETFS_ROOT = WIKI_ROOT / "etfs"


def wiki_root() -> Path:
    WIKI_ROOT.mkdir(parents=True, exist_ok=True)
    TICKERS_ROOT.mkdir(parents=True, exist_ok=True)
    ETFS_ROOT.mkdir(parents=True, exist_ok=True)
    return WIKI_ROOT


def instrument_root(asset_type: str | None = None) -> Path:
    return ETFS_ROOT if (asset_type or "").lower() == "etf" else TICKERS_ROOT


def ticker_dir(ticker: str, asset_type: str | None = None) -> Path:
    d = instrument_root(asset_type) / ticker
    d.mkdir(parents=True, exist_ok=True)
    return d


def hash_body(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def relpath_from_data(p: Path) -> str:
    """저장용 경로. data_dir 부모 기준 상대 경로 문자열."""
    return str(p.relative_to(CFG.data_dir.parent)).replace("\\", "/")


@dataclass
class WikiSection:
    doc_id: str
    ticker: str
    section_type: str
    file_path: Path
    body: str          # frontmatter 제외 본문
    raw: str           # frontmatter 포함 전체
    meta: dict


def load_section_file(path: Path) -> WikiSection | None:
    if not path.is_file():
        return None
    post = frontmatter.load(path)
    meta = dict(post.metadata)
    ticker = meta.get("ticker")
    stype = meta.get("section_type")
    doc_id = meta.get("doc_id") or (f"{ticker}:{stype}" if ticker and stype else path.stem)
    return WikiSection(
        doc_id=doc_id,
        ticker=str(ticker) if ticker else "",
        section_type=str(stype) if stype else "",
        file_path=path,
        body=post.content,
        raw=path.read_text(encoding="utf-8"),
        meta=meta,
    )


def iter_section_files() -> list[Path]:
    out: list[Path] = []
    for root in (TICKERS_ROOT, ETFS_ROOT):
        if not root.exists():
            continue
        for tdir in sorted(root.iterdir()):
            if not tdir.is_dir():
                continue
            for p in sorted(tdir.glob("*.md")):
                # SKILL.md는 정책 파일 — 검색 인덱스에서 제외
                if p.name.upper() == "SKILL.MD":
                    continue
                out.append(p)
    return out


def load_by_doc_id(doc_id: str) -> WikiSection | None:
    """doc_id → 파일 위치 규약: ticker/{NN}_{section_type}.md.
    file_path를 DB에서 받은 경우 load_section_file() 직접 쓸 것."""
    try:
        ticker, stype = doc_id.split(":", 1)
    except ValueError:
        return None
    # 파일명은 NN_section.md 형태로 저장되므로 suffix 일치로 탐색
    for root in (TICKERS_ROOT, ETFS_ROOT):
        tdir = root / ticker
        if not tdir.exists():
            continue
        for p in tdir.glob("*.md"):
            if p.name.upper() == "SKILL.MD":
                continue
            if p.stem.endswith(f"_{stype}"):
                return load_section_file(p)
    return None


def load_by_file_path(relpath: str) -> WikiSection | None:
    """section_doc.file_path 문자열로부터 로드."""
    p = CFG.data_dir.parent / relpath
    return load_section_file(p)
