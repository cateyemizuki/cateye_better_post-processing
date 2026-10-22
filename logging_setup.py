"""调试日志：写入插件目录 logs/ 下的文件（不进控制台、不进主进程日志）。

注意：文件日志不符合插件规范（规范要求统一走 ctx.logger），现阶段仅用于
本地调试，后续会移除。日志不会被主动清除，无相关配置项。
"""

from __future__ import annotations

import logging
from pathlib import Path

_ROOT_LOGGER_NAME = "ccr"
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_file_logging(plugin_dir: Path) -> logging.Logger:
    """在插件目录 logs/ 下建文件日志，返回 "ccr" 根 logger（子模块用 getLogger("ccr.xxx")）。"""
    logger = logging.getLogger(_ROOT_LOGGER_NAME)
    if getattr(logger, "_ccr_initialized", False):
        return logger
    log_dir = Path(plugin_dir) / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir / "cross_chat_recall.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False  # 不向上冒泡 → 不进控制台/主进程日志
        logger._ccr_initialized = True  # type: ignore[attr-defined]
        logger.info("文件日志已初始化: %s", log_dir / "cross_chat_recall.log")
    except Exception as exc:  # 目录只读等极端情况：静默降级为无文件日志
        logger.setLevel(logging.CRITICAL + 1)  # 屏蔽所有输出，避免刷进控制台
        logger.debug("文件日志初始化失败: %s", exc)
    return logger


def get_logger(name: str) -> logging.Logger:
    """取 "ccr.<name>" 子 logger（未初始化文件日志时也能安全调用）。"""
    return logging.getLogger(f"{_ROOT_LOGGER_NAME}.{name}")
