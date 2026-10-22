"""注入器：把私聊概括块插入到 planner / replyer 的 items 首条 SystemMessageItem 之后。

⚠️ 宿主会把 Hook 返回的 items 重新反序列化（pydantic 校验），每条 item 的
``meta`` 必须是字典（缺失时整份 items 被忽略并报
"快照中的 item.meta 必须是字典"）。因此新条目必须带结构完整的 meta。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from .helpers import fmt_ts


def build_injection_block(entries: List[Dict[str, Any]]) -> str:
    """把若干用户的私聊概括合并为一个注入文本块。

    entries 每项：nickname / user_id / start_ts / end_ts / summary
    """
    lines = [
        "【私聊记忆】以下是最近一段时间你在私聊中与部分群友的聊天内容概括"
        "（仅供你自己参考，帮你记起与对方私下聊过的内容；请自然地运用，不要生硬复述）："
    ]
    for idx, entry in enumerate(entries, 1):
        lines.append(
            f"{idx}. 与 {entry.get('nickname') or '未知用户'}（{entry.get('user_id')}）"
            f"在 {fmt_ts(float(entry.get('start_ts') or 0))} 至 {fmt_ts(float(entry.get('end_ts') or 0))}"
            "的私聊内容概括："
        )
        summary = str(entry.get("summary") or "").strip()
        lines.append(summary if summary else "（概括为空）")
    return "\n".join(lines)


def build_recall_content(entry: Dict[str, Any]) -> str:
    """构造工具主动召回返回给模型的单用户文本（与注入块同风格，可直接转述）。

    entry 每项：nickname / user_id / start_ts / end_ts / summary
    """
    nickname = entry.get("nickname") or "未知用户"
    user_id = str(entry.get("user_id") or "").strip()
    who = f"{nickname}（{user_id}）" if user_id else str(nickname)
    head = (
        f"【私聊记忆】你与 {who} 在 {fmt_ts(float(entry.get('start_ts') or 0))}"
        f" 至 {fmt_ts(float(entry.get('end_ts') or 0))} 的私聊内容概括："
    )
    summary = str(entry.get("summary") or "").strip()
    return f"{head}\n{summary if summary else '（概括为空）'}"


def _build_meta(first_item: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """构造新条目的 meta：优先复制首条 meta 的其余字段，再覆盖标识与时间。"""
    meta: Dict[str, Any] = {}
    if isinstance(first_item, dict) and isinstance(first_item.get("meta"), dict):
        meta = dict(first_item["meta"])
    meta["item_id"] = uuid.uuid4().hex
    meta["logical_turn_id"] = None
    meta["timestamp"] = datetime.now().isoformat()
    return meta


def make_system_item(text: str, first_item: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造一条 SystemMessageItem（与宿主 items 序列化结构一致，含必需的 meta）。"""
    return {
        "item_type": "SystemMessageItem",
        "meta": _build_meta(first_item),
        "parts": [{"type": "text", "text": text}],
    }


def inject_after_first_system(
    items: Any,
    text: str,
    logger: Optional[logging.Logger] = None,
) -> Optional[List[Any]]:
    """返回在首条条目（应为 SystemMessageItem）之后插入新条目的新 items 列表。

    - items 为空 → 只含新条目；
    - 首条不是 SystemMessageItem → 记警告后仍插在其后（位置 1），不丢弃内容；
    - items 不是列表 → 返回 None（调用方放弃注入）。
    """
    if not isinstance(items, list):
        if logger is not None:
            logger.warning("items 不是列表（%s），放弃注入", type(items).__name__)
        return None
    first = items[0] if items else None
    new_item = make_system_item(text, first if isinstance(first, dict) else None)
    if not items:
        return [new_item]
    if not (isinstance(first, dict) and first.get("item_type") == "SystemMessageItem"):
        if logger is not None:
            logger.warning(
                "items 首条不是 SystemMessageItem（%s），仍按位置 1 插入",
                first.get("item_type") if isinstance(first, dict) else type(first).__name__,
            )
    return [first, new_item, *items[1:]]
