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


def init_db() -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = connect()
    try:
        conn.executescript(sql)
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
