"""planner 上下文尾部的「文本规则拦截」通知（纯逻辑，不依赖 SDK，可离线单测）。

宿主 1.3.x 的 reply 工具在 ``send_service.before_send`` 被插件 abort 后，工具结果
只保留 ``success: false`` 与生成的分段，**拿不到 abort_message**；planner 会把它
当成普通发送失败并反复重试 reply。这里构造一条 system-reminder 风格的
``UserMessageItem``，插入到「最新消息之后、工具/人物画像/时间/分析指令等尾部合成项
之前」，明确告诉 planner：这是插件文本规则拦截，属于预期行为，不要重试。
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

# 尾部合成项的特征：宿主的工具提示、人物画像、时间、分析指令。
_TAIL_PREFIXES = ("<system-reminder>", "【人物画像")
_TAIL_TIME_PATTERN = re.compile(r"^时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*$")
_TAIL_MARKERS = (
    "请你进行分析",
    "你需要输出对",
    "思考是否进行工具调用",
)


def build_rule_block_notice(rule_label: str) -> str:
    """构造文本规则拦截通知正文。"""
    rule = str(rule_label or "").strip()[:120] or "文本规则"
    return (
        "<system-reminder>\n"
        "【插件文本规则拦截】你上一轮调用 reply 生成的整条消息命中了插件规则 "
        f"`{rule}`，已在发送前被插件拦截，**没有发送**。\n"
        "这是预期行为，不是发送故障，也不是需要你修复的错误；不要重复调用 reply 重试，"
        "也不要换措辞重发同一条内容。如果还需要回应，请基于最新消息重新判断；"
        "否则直接结束本轮。\n"
        "</system-reminder>"
    )


def _item_text(item: Any) -> str:
    """取出上下文项里所有 text part 的拼接文本。"""
    if not isinstance(item, dict):
        return ""
    parts = item.get("parts")
    if not isinstance(parts, list):
        return ""
    return "".join(
        str(part.get("text") or "")
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    )


def is_tail_synthetic_item(item: Any) -> bool:
    """该上下文项是否是宿主的尾部合成项（不是真实聊天消息/工具结果）。"""
    if not isinstance(item, dict):
        return False
    if str(item.get("item_type") or "") not in {"UserMessageItem", "SystemMessageItem"}:
        return False
    text = _item_text(item).lstrip()
    if not text:
        return False
    if text.startswith(_TAIL_PREFIXES):
        return True
    if _TAIL_TIME_PATTERN.match(text):
        return True
    return any(marker in text for marker in _TAIL_MARKERS)


def insert_planner_tail_notice(items: Any, text: str) -> Optional[List[Any]]:
    """把通知插到尾部合成项之前、最新消息之后；items 非列表时返回 None。"""
    if not isinstance(items, list):
        return None
    insert_at = len(items)
    while insert_at > 0 and is_tail_synthetic_item(items[insert_at - 1]):
        insert_at -= 1
    notice_item: Dict[str, Any] = {
        "item_type": "UserMessageItem",
        "meta": {
            "item_id": uuid.uuid4().hex,
            "logical_turn_id": None,
            "timestamp": datetime.now().isoformat(),
        },
        "parts": [{"type": "text", "text": text}],
    }
    return [*items[:insert_at], notice_item, *items[insert_at:]]
