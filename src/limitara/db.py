"""SQLite plumbing shared by the stand-ins. Each stand-in owns exactly one file.

Money is stored as TEXT and dates as ISO TEXT: SQLite REAL is a float and would
undo the exact-money rule. Connections are autocommit; writers open an explicit
BEGIN IMMEDIATE so a check-then-change is one atomic step.
"""

import os
import sqlite3
from pathlib import Path

DATA_DIR_ENV = "LIMITARA_DATA_DIR"


def data_dir() -> Path:
    path = Path(os.environ.get(DATA_DIR_ENV, "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path(name: str) -> Path:
    return data_dir() / f"{name}.db"


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def reset(path: Path | str) -> None:
    """Delete a database and its WAL side files so a seed starts from nothing."""
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
