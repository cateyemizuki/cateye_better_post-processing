"""插件自带的**文件日志**：把本插件的全部日志落到 ``<插件目录>/logs/`` 下。

用途：部署到服务器跑几小时，直接拿 ``logs/`` 里的文件复查"每个功能有没有异常"。

设计要点
--------
1. **不改业务代码就能全量落盘**：宿主给的 ``self.ctx.logger`` 是 stdlib Logger
   （名字 ``plugin.<plugin_id>``，见 ``maibot_sdk/context.py``），这里直接给它挂一个
   ``RotatingFileHandler`` —— 插件里所有 ``self.ctx.logger.xxx(...)``（现有 100+ 处）
   自动进文件，不需要在每个功能里再写一行。
2. **把 Logger 自身级别压到 DEBUG**，让 ``debug`` 级也进文件；宿主自己的 handler 有各自
   的级别（通常 INFO），所以**宿主日志不会被 debug 刷屏**。
3. **文件按大小滚动**：默认 8MB × 6 份（约 48MB 上限），跑几小时绰绰有余，也不会撑爆磁盘。
4. **全程不抛异常**：插件目录只读 / 磁盘满 / handler 重复挂载 / 环境异常，都只记一条
   warning，绝不影响插件本身运行。

文件布局::

    <插件目录>/logs/plugin.log          # 当前日志
    <插件目录>/logs/plugin.log.1        # 滚动的历史（越新编号越小）

单行格式（便于 grep / awk）::

    2026-09-24 01:05:10.123 [INFO] [含义库] planner 按需补录：本轮 6 个表情包，其中 1 个缺含义已入队
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional

# 日志目录名（插件目录下）与文件名
LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "plugin.log"

# 单个文件上限 8MB、保留 5 份历史（约 48MB 上限）
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5

# 挂在 handler 上的标记属性名（用于幂等判断）
_HANDLER_MARKER = "_bpp_trace_handler"


def install(
    logger: logging.Logger,
    plugin_dir: Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> Optional[Path]:
    """给 ``logger`` 挂一个写 ``<plugin_dir>/logs/plugin.log`` 的滚动文件 handler。

    Returns:
        Optional[Path]: 日志文件路径；安装失败返回 ``None``（只记一条 warning，不抛异常）。
    """
    try:
        # 幂等：已经挂过就不重复挂（热重载会再次调用）
        for handler in list(logger.handlers):
            if getattr(handler, _HANDLER_MARKER, False):
                # 幂等分支：返回 **Path**（与首次安装的返回类型一致）
                existing = getattr(handler, "baseFilename", None)
                return Path(existing) if existing else None

        log_dir = Path(plugin_dir) / LOG_DIR_NAME
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / LOG_FILE_NAME

        handler = logging.handlers.RotatingFileHandler(
            str(log_path),
            maxBytes=int(max_bytes),
            backupCount=int(backup_count),
            encoding="utf-8",
            delay=True,
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        setattr(handler, _HANDLER_MARKER, True)
        logger.addHandler(handler)
        # 让 debug 也进文件；宿主自己的 handler 有各自的级别，不受影响。
        if logger.level == logging.NOTSET or logger.level > logging.DEBUG:
            logger.setLevel(logging.DEBUG)
        return log_path
    except Exception:
        try:
            logger.warning("安装插件文件日志失败（不影响插件运行）", exc_info=True)
        except Exception:
            pass
        return None


def uninstall(logger: logging.Logger) -> None:
    """摘掉本模块挂的 handler（插件卸载时调用）；失败忽略。"""
    try:
        for handler in list(logger.handlers):
            if getattr(handler, _HANDLER_MARKER, False):
                logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass
    except Exception:
        pass


def current_log_path(plugin_dir: Path) -> Path:
    """返回日志文件的预期路径（不保证已创建）。"""
    return Path(plugin_dir) / LOG_DIR_NAME / LOG_FILE_NAME
