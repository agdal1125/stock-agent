from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import CFG


SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(CFG.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def _apply_lightweight_migrations(conn: sqlite3.Connection) -> None:
    """Small additive migrations for existing local PoC databases.

    The schema is mostly `CREATE TABLE IF NOT EXISTS`, so new columns need
    explicit ALTERs when a developer keeps an older `data/canonical.db`.
    """
    if not _has_column(conn, "ticker_master", "asset_type"):
        conn.execute(
            "ALTER TABLE ticker_master "
            "ADD COLUMN asset_type TEXT NOT NULL DEFAULT 'stock'"
        )


def init_db() -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect()
    try:
        conn.executescript(sql)
        _apply_lightweight_migrations(conn)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
