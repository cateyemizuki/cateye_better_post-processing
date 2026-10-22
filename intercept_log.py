"""拦截行为独立日志。

插件的拦截行为**不复用宿主主日志**：每条拦截记录按 JSON Lines 追加写入插件
``data_dir`` 下的独立文件（默认 ``data/plugins/<plugin_id>/intercept.jsonl``），
便于单独 grep / 统计 / 归档，也不会因为宿主日志级别调整而丢记录。

文件超限时按 ``base -> .1 -> .2 ...`` 轮转，最多保留 ``backup_count`` 份历史。

对外只暴露 :class:`InterceptLogger`：``record(event, **fields)`` 写一条记录。
所有 IO 异常都被吞掉并通过 ``on_error`` 回调上报，**绝不让日志故障影响拦截逻辑**。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional


class InterceptLogger:
    """把拦截事件写成独立文件的 JSON Lines 日志。

    Args:
        directory: 日志目录（通常为 ``ctx.paths.data_dir``）。
        file_name: 日志文件名。
        max_bytes: 单文件字节上限，超过即轮转；``<= 0`` 表示不轮转。
        backup_count: 轮转保留的历史文件份数；``0`` 表示直接截断当前文件。
        on_error: 写盘失败时的回调（本实例生命周期内只上报第一次错误，之后一律静默，
            避免刷屏——不区分错误类型）。
    """

    def __init__(
        self,
        *,
        directory: Path,
        file_name: str = "intercept.jsonl",
        max_bytes: int = 1024 * 1024,
        backup_count: int = 3,
        on_error: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._directory = Path(directory)
        #: 路径防护：只取文件名部分，并拒绝 `''` / `.` / `..` 这类"仍是目录"的名字。
        #: 配置里写成 `../x.jsonl` / `sub/x.jsonl` / 绝对路径（如 `D:\x.jsonl`）/ `..` 时，
        #: 都不会把日志写到插件数据目录之外。
        normalized = Path(str(file_name or "")).name
        self._file_name = normalized if normalized not in ("", ".", "..") else "intercept.jsonl"
        self._max_bytes = int(max_bytes)
        self._backup_count = max(0, int(backup_count))
        self._on_error = on_error
        self._lock = threading.Lock()
        self._error_reported = False

    @property
    def path(self) -> Path:
        """当前主日志文件的完整路径。"""

        return self._directory / self._file_name

    def record(self, event: str, **fields: Any) -> None:
        """写入一条拦截记录；任何异常都不会向外抛出。"""

        payload: Dict[str, Any] = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ts": round(time.time(), 3),
            "event": str(event),
        }
        for key, value in fields.items():
            if value is None or value == "":
                continue
            payload[str(key)] = value

        try:
            line = json.dumps(payload, ensure_ascii=False) + "\n"
        except (TypeError, ValueError):
            line = json.dumps(
                {"time": payload["time"], "ts": payload["ts"], "event": payload["event"],
                 "detail": "记录序列化失败，字段已丢弃"},
                ensure_ascii=False,
            ) + "\n"

        with self._lock:
            try:
                self._write(line)
            except Exception as exc:  # noqa: BLE001 - 日志故障绝不影响拦截逻辑
                self._report_error(f"拦截日志写入失败（{type(exc).__name__}: {exc}）")

    def close(self) -> None:
        """关闭日志（当前实现无需持有句柄，保留接口以便将来扩展）。"""

        return None

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _write(self, line: str) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        encoded_size = len(line.encode("utf-8"))
        target = self.path
        if self._max_bytes > 0 and target.exists() and target.stat().st_size + encoded_size > self._max_bytes:
            self._rotate()
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def _rotate(self) -> None:
        """把当前文件轮转为 ``.1``，历史文件依次后移，丢弃超出份数的旧文件。"""

        target = self.path
        if self._backup_count <= 0:
            target.unlink(missing_ok=True)
            return
        oldest = self._backup_path(self._backup_count)
        oldest.unlink(missing_ok=True)
        for index in range(self._backup_count - 1, 0, -1):
            source = self._backup_path(index)
            if source.exists():
                source.replace(self._backup_path(index + 1))
        if target.exists():
            target.replace(self._backup_path(1))

    def _backup_path(self, index: int) -> Path:
        return self._directory / f"{self._file_name}.{index}"

    def _report_error(self, message: str) -> None:
        if self._error_reported or self._on_error is None:
            return
        self._error_reported = True
        try:
            self._on_error(message)
        except Exception:  # noqa: BLE001 - 上报失败保持静默
            pass
