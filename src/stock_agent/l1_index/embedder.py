"""section_doc 인덱스 중 embedding=NULL 인 항목을 wiki 파일에서 읽어 임베딩.

content_hash가 바뀌면 section_builder가 embedding=NULL로 리셋하므로
이 모듈이 다시 임베딩한다.
"""
from __future__ import annotations

import numpy as np

from ..agent_int.llm_gateway import embed
from ..db import tx
from .wiki_loader import load_by_file_path


def _body_for_embed(rel_path: str) -> str | None:
    """임베딩 대상 = 본문(헤더·리스트)만. frontmatter·주석은 제거."""
    sec = load_by_file_path(rel_path)
    if sec is None:
        return None
    return sec.body.strip() or None


def embed_pending(batch: int = 32) -> int:
    with tx() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT doc_id, file_path FROM section_doc WHERE embedding IS NULL ORDER BY doc_id"
        ).fetchall()]
    if not rows:
        return 0

    # phase 1: 파일에서 본문 읽고 embed 호출 (DB 미터치)
    payloads: list[tuple[str, str]] = []   # (doc_id, body)
    for r in rows:
        body = _body_for_embed(r["file_path"])
        if body:
            payloads.append((r["doc_id"], body))
    if not payloads:
        return 0

    embedded: list[tuple[str, bytes]] = []
    for i in range(0, len(payloads), batch):
        chunk = payloads[i:i + batch]
        texts = [b for _, b in chunk]
        vecs = embed(texts)
        for (doc_id, _), v in zip(chunk, vecs):
            embedded.append((doc_id, v.astype(np.float32).tobytes()))

    # phase 2: bulk update
    with tx() as conn:
        for doc_id, blob in embedded:
            conn.execute(
                "UPDATE section_doc SET embedding=? WHERE doc_id=?",
                (blob, doc_id),
            )
    return len(embedded)


def load_matrix(where_sql: str = "", params: tuple = ()) -> tuple[list[dict], np.ndarray]:
    """section_doc 인덱스 로드 + 대응 본문을 파일에서 읽어 dict에 채워 반환.

    반환 rows 각각에 'content' 키가 추가됨 (BM25·프롬프트 조립용)."""
    sql = "SELECT doc_id, ticker, section_type, file_path, embedding FROM section_doc"
    if where_sql:
        sql += " WHERE " + where_sql
    with tx() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if not rows:
        return [], np.zeros((0, 1), dtype=np.float32)

    # 본문을 파일에서 채움 + 임베딩 파싱
    keep_rows: list[dict] = []
    vecs: list[np.ndarray] = []
    for r in rows:
        blob = r.pop("embedding", None)
        if blob is None:
            continue
        v = np.frombuffer(blob, dtype=np.float32)
        sec = load_by_file_path(r["file_path"])
        if sec is None:
            continue
        r["content"] = sec.body
        keep_rows.append(r)
        vecs.append(v)

    if not keep_rows:
        return [], np.zeros((0, 1), dtype=np.float32)
    m = np.vstack(vecs)
    return keep_rows, m


if __name__ == "__main__":
    n = embed_pending()
    print(f"[embedder] embedded {n} section_docs")
