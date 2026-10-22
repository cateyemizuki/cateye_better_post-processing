"""总结库：插件自建 SQLite（data/plugins/<plugin_id>/cross_chat_recall.db）。

每用户一条总结记录（全量重写覆盖语义）；pending_resummarize 为"可被重新总结"
队列标记：推进数达标后置位，再次被扫描到时触发总结并清零。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logging_setup import get_logger

_COLS = (
    "user_id", "platform", "private_stream_id", "user_nickname",
    "summary", "start_ts", "end_ts", "user_msg_count",
    "pending_resummarize", "updated_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS private_summaries (
    user_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL DEFAULT 'qq',
    private_stream_id TEXT NOT NULL DEFAULT '',
    user_nickname TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    start_ts REAL NOT NULL DEFAULT 0,
    end_ts REAL NOT NULL DEFAULT 0,
    user_msg_count INTEGER NOT NULL DEFAULT 0,
    pending_resummarize INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT 0
);
"""


class SummaryStore:
    def __init__(self, db_path: Path) -> None:
        self._logger = get_logger("store")
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        with self._lock, self._conn:
            self._conn.execute(_SCHEMA)
        self._logger.info("总结库已打开: %s", db_path)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
        self._logger.info("总结库已关闭")

    @staticmethod
    def _row_to_dict(row: Optional[tuple]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return dict(zip(_COLS, row))

    def get(self, user_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {', '.join(_COLS)} FROM private_summaries WHERE user_id = ?", (str(user_id),)
            ).fetchone()
        return self._row_to_dict(row)

    def get_many(self, user_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        ids = [str(u) for u in user_ids if str(u).strip()]
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {', '.join(_COLS)} FROM private_summaries WHERE user_id IN ({placeholders})", ids
            ).fetchall()
        return {row[0]: self._row_to_dict(row) for row in rows}  # type: ignore[index]

    def upsert(
        self,
        *,
        user_id: str,
        platform: str,
        private_stream_id: str,
        user_nickname: str,
        summary: str,
        start_ts: float,
        end_ts: float,
        user_msg_count: int,
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO private_summaries
                    (user_id, platform, private_stream_id, user_nickname, summary,
                     start_ts, end_ts, user_msg_count, pending_resummarize, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    platform = excluded.platform,
                    private_stream_id = excluded.private_stream_id,
                    user_nickname = excluded.user_nickname,
                    summary = excluded.summary,
                    start_ts = excluded.start_ts,
                    end_ts = excluded.end_ts,
                    user_msg_count = excluded.user_msg_count,
                    pending_resummarize = 0,
                    updated_at = excluded.updated_at
                """,
                (str(user_id), str(platform), str(private_stream_id), str(user_nickname),
                 str(summary), float(start_ts), float(end_ts), int(user_msg_count), time.time()),
            )
        self._logger.info("总结已入库 user=%s 窗口=%s ~ %s 消息数=%s", user_id, start_ts, end_ts, user_msg_count)

    def set_pending(self, user_id: str, pending: bool) -> bool:
        """置位/清除重总结队列标记；返回是否存在该用户的记录。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE private_summaries SET pending_resummarize = ? WHERE user_id = ?",
                (1 if pending else 0, str(user_id)),
            )
        exists = cur.rowcount > 0
        if exists:
            self._logger.info("用户 %s 的重总结队列标记已%s", user_id, "置位" if pending else "清除")
        return exists

    def cleanup(self, ttl_hours: float) -> int:
        """删除截止时间超出 TTL 的总结，返回删除条数。"""
        cutoff = time.time() - max(ttl_hours, 0.0) * 3600
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM private_summaries WHERE end_ts < ?", (cutoff,))
        deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        if deleted:
            self._logger.info("TTL 淘汰：删除 %d 条过期总结（end_ts < %s）", deleted, cutoff)
        return deleted

    def clear(self) -> int:
        """清空全部总结（/ccr clean），返回删除条数。"""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM private_summaries")
        deleted = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        self._logger.info("已清空总结库：删除 %d 条总结", deleted)
        return deleted

    def dump(self) -> List[Dict[str, Any]]:
        """调试用：返回全部记录。"""
        with self._lock:
            rows = self._conn.execute(f"SELECT {', '.join(_COLS)} FROM private_summaries").fetchall()
        return [d for d in (self._row_to_dict(r) for r in rows) if d]
