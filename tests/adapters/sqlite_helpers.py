"""Helpers for the SQLite-backed adapter tests (OpenCode, Goose, Crush)."""

import shutil
import sqlite3
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def install_fixture(source: str, name: str, dest: Path) -> Path:
    """Copy tests/adapters/fixtures/<source>/<name> (a database a real run of the harness wrote) to `dest`."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIXTURES / source / name, dest)
    return dest


def wal_snapshot(build, dst: Path) -> Path:
    """A database whose rows exist only in its -wal file, as a live app leaves it between checkpoints.

    `build(con)` fills a WAL-mode database with checkpointing off; the db and -wal files are copied while the writer is
    still open (no -shm), so reading `dst` alone finds no tables and reading it with its -wal finds everything."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    live = dst.parent / "_live.db"
    con = sqlite3.connect(live)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    build(con)
    con.commit()
    shutil.copyfile(live, dst)
    shutil.copyfile(str(live) + "-wal", str(dst) + "-wal")
    con.close()
    for p in live.parent.glob("_live.db*"):
        p.unlink()
    return dst


def main_file_tables(path: Path) -> list[str]:
    """Tables visible in the main database file alone, without its -wal."""
    alone = path.parent / "_alone.db"
    shutil.copyfile(path, alone)
    con = sqlite3.connect(alone)
    try:
        return [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    finally:
        con.close()
        alone.unlink()
