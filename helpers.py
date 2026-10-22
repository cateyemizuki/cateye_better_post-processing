"""消息 dict 解析与通用小工具（兼容宿主不同版本的消息序列化形态）。"""

from __future__ import annotations

import time as _time
from datetime import datetime
from typing import Any, Dict, Mapping, Optional

_TZ_WARNED = {"flag": False}


def _sub(message: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = message.get(key)
    return value if isinstance(value, Mapping) else {}


def extract_message_fields(message: Any) -> Dict[str, Any]:
    """从消息 dict 提取常用字段，键：

    user_id / nickname / group_id / session_id / platform / self_id / text /
    is_notify / is_command / is_synthetic / is_self
    """
    if not isinstance(message, Mapping):
        return {}
    message_info = _sub(message, "message_info")
    user_info = message_info.get("user_info") if isinstance(message_info.get("user_info"), Mapping) else _sub(message, "user_info")
    group_info = message_info.get("group_info") if isinstance(message_info.get("group_info"), Mapping) else _sub(message, "group_info")
    additional = message_info.get("additional_config") if isinstance(message_info.get("additional_config"), Mapping) else _sub(message, "additional_config")

    user_id = str(user_info.get("user_id") or "").strip()
    nickname = str(user_info.get("user_nickname") or user_info.get("user_cardname") or "").strip()
    self_id = ""
    for key in ("self_id", "platform_io_account_id", "bot_id", "account_id"):
        value = str(additional.get(key) or "").strip()
        if value:
            self_id = value
            break
    injected_mark = any("injected" in str(key).lower() for key in additional)
    is_notify = bool(message.get("is_notify", False))
    is_command = bool(message.get("is_command", False))
    text = str(message.get("processed_plain_text") or message.get("plain_text") or "").strip()

    return {
        "user_id": user_id,
        "nickname": nickname,
        "group_id": str(group_info.get("group_id") or message.get("group_id") or "").strip(),
        "session_id": str(message.get("session_id") or message.get("chat_id") or message.get("chat_stream_id") or "").strip(),
        "platform": str(message.get("platform") or message_info.get("platform") or "qq"),
        "self_id": self_id,
        "text": text,
        "is_notify": is_notify,
        "is_command": is_command,
        # 合成记录（通知/插件注入）不是真实用户消息
        "is_synthetic": is_notify or injected_mark,
        "is_self": bool(user_id and self_id and user_id == self_id) or injected_mark,
    }


def is_bot_message(message: Mapping[str, Any]) -> bool:
    """是否 bot 自身消息 / 合成记录（通知、插件注入）。"""
    fields = extract_message_fields(message)
    return bool(fields.get("is_self")) or bool(fields.get("is_synthetic"))


def msg_timestamp(message: Mapping[str, Any]) -> float:
    """取消息时间戳（UNIX 秒）。兼容 timestamp / time 的数值与 ISO 字符串形态；解析失败返回 0。"""
    for key in ("timestamp", "time"):
        value = message.get(key)
        if value is None:
            continue
        # 数值或数字字符串
        try:
            ts = float(value)
            if ts > 0:
                return ts
        except (TypeError, ValueError):
            pass
        # ISO 字符串
        if isinstance(value, str):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if dt.tzinfo is not None:
                    return dt.timestamp()
                return _time.mktime(dt.timetuple())
            except ValueError:
                if not _TZ_WARNED["flag"]:
                    _TZ_WARNED["flag"] = True
                    from .logging_setup import get_logger

                    get_logger("helpers").warning("无法解析消息时间字段 %s=%r，按 0 处理（仅警告一次）", key, value)
    # 宿主 Message 模型的时间可能藏在 message_info.time
    message_info = _sub(message, "message_info")
    inner = message_info.get("time")
    if inner is not None:
        try:
            return float(inner)
        except (TypeError, ValueError):
            pass
    return 0.0


def fmt_ts(ts: float) -> str:
    """UNIX 秒 → "YYYY-MM-DD HH:MM:SS"（本地时区，精确到秒）。"""
    if not ts or ts <= 0:
        return "未知时间"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def allowed_by_list(value: str, name_list: Any, mode: str = "blacklist") -> bool:
    """共用名单判定：同一张名单按 ``mode`` 解释为黑名单或白名单。

    * 黑名单模式（``mode="blacklist"``，默认）：在名单内 → 拒绝；名单为空 → 全部放行；
    * 白名单模式（``mode="whitelist"``）：在名单内 → 放行；名单为空 → 全部拒绝。

    空值（空群号 / 空用户号）一律拒绝——名单判定只对"有值"的对象有意义。
    """
    value = str(value or "").strip()
    if not value:
        return False
    names = [str(x).strip() for x in (name_list or []) if str(x).strip()]
    if str(mode or "").strip().lower() == "whitelist":
        return value in names
    return value not in names


def unwrap_messages(result: Any, logger: Any = None, source: str = "") -> list:
    """解包能力返回的消息列表（SDK 一般直接给 list，防御性兼容 dict 包装）。"""
    messages = result
    if isinstance(result, Mapping):
        messages = result.get("messages")
    if isinstance(messages, list):
        return [m for m in messages if isinstance(m, Mapping)]
    if logger is not None:
        logger.warning("%s：消息返回形态异常（%s），按空处理", source or "unwrap_messages", type(result).__name__)
    return []
