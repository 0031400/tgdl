"""``data/tgdl.db``：只存断点续传的位图。

这是唯一写入频繁的东西（每下完一片就变），所以用 SQLite 而不是 JSON。

    progress(key, path, size, chunk, done, updated_at)

``done`` 是一个位图：第 i 位为 1 表示第 i 片已经落盘。恢复时按
size/chunk/path 三者匹配才算数——文件被删了或者换了分片大小就重下。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path


class ProgressDb:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS progress (
                key        TEXT PRIMARY KEY,
                path       TEXT NOT NULL,
                size       INTEGER NOT NULL,
                chunk      INTEGER NOT NULL,
                done       BLOB NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- 断点续传 ----
    def load(self, key: str, *, path: Path, size: int, chunk: int) -> bytes | None:
        """拿到上次的位图；path / size / chunk 对不上就当没有。"""
        row = self._conn.execute(
            "SELECT path, size, chunk, done FROM progress WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        old_path, old_size, old_chunk, done = row
        if old_size != size or old_chunk != chunk:
            return None
        if Path(old_path) != Path(path) or not Path(path).exists():
            return None
        return bytes(done)

    def save(self, key: str, *, path: Path, size: int, chunk: int, done: bytes) -> None:
        self._conn.execute(
            "INSERT INTO progress (key, path, size, chunk, done, updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "path=excluded.path, size=excluded.size, chunk=excluded.chunk, "
            "done=excluded.done, updated_at=excluded.updated_at",
            (key, str(path), size, chunk, done, int(time.time())),
        )
        self._conn.commit()

    def clear(self, key: str) -> None:
        self._conn.execute("DELETE FROM progress WHERE key = ?", (key,))
        self._conn.commit()

    def exists(self, key: str) -> bool:
        """这条 key 有没有未完成的进度记录（用来判断文件是不是下了一半）。"""
        return self._conn.execute(
            "SELECT 1 FROM progress WHERE key = ? LIMIT 1", (key,)
        ).fetchone() is not None

    def keys(self) -> list[str]:
        return [r[0] for r in self._conn.execute("SELECT key FROM progress")]
