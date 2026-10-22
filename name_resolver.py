"""主动召回工具的名称解析（纯函数、无副作用，可离线单测）。

工具只在**当前聊流最近 N 条消息**（N = `tool.context_window_messages`）里解析候选
用户昵称，不做全库/全群搜索。解析五分支（合并计划书 §5.2，含阶段 2.5 裁决取舍①）：

1. 候选：过滤 bot 自身 / 通知 / 合成 / 命令消息；昵称为空者不参与匹配；
   同一 user_id 保留最新昵称。
2. 精准匹配：``昵称完全相等 或 与 user_id（QQ 号）相等``（字符串等值比较，
   入参 strip）。
3. 精准无命中时正则降级（仍只匹配昵称）：``re.search(user_name, nickname)``；
   传入名称为非法正则时按字面量匹配。
4. 命中 ≥2 个 → 返回「昵称（QQ 号）」候选名单（按最近发言新→旧排列），
   并提示可传昵称或 QQ 号重新查询，不附带私聊内容。
5. 0 命中（含入参 strip 后为空）→ 返回"名称有误"。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Tuple

from .helpers import extract_message_fields, msg_timestamp

# (user_id, nickname)
Candidate = Tuple[str, str]

NAME_NOT_FOUND_TEMPLATE = "名称有误：当前会话中没有找到名称为「{name}」的用户。"
NAME_AMBIGUOUS_TEMPLATE = "匹配到多个用户，请从以下名单中选一个并传昵称或 QQ 号重新查询：{names}"


def collect_candidates(messages: Any, limit: int = 0) -> List[Candidate]:
    """从消息列表汇总候选用户（按最近发言新→旧，同一 user_id 保留最新昵称）。

    - 过滤 bot 自身 / 通知 / 合成 / 命令消息（复用 helpers.extract_message_fields）；
    - 昵称为空的用户不参与名称匹配；
    - ``limit > 0`` 时只取最近 limit 条有效消息（调用方拉取消息时已按窗口限制，
      这里做防御性二次收口）。
    """
    if not isinstance(messages, (list, tuple)):
        return []
    found: List[Tuple[float, str, str]] = []
    for msg in messages:
        if not isinstance(msg, Mapping):
            continue
        fields = extract_message_fields(msg)
        if fields.get("is_self") or fields.get("is_synthetic") or fields.get("is_command"):
            continue
        user_id = str(fields.get("user_id") or "").strip()
        nickname = str(fields.get("nickname") or "").strip()
        if not user_id or not nickname:
            continue
        found.append((msg_timestamp(msg), user_id, nickname))
    # 时间不可解析（0）时保持宿主返回顺序（get_recent 为新→旧）
    found.sort(key=lambda item: item[0], reverse=True)
    if limit and limit > 0:
        found = found[:limit]

    candidates: List[Candidate] = []
    seen: set = set()
    for _, user_id, nickname in found:
        if user_id in seen:
            continue
        seen.add(user_id)
        candidates.append((user_id, nickname))
    return candidates


def _format_candidates(candidates: List[Candidate]) -> List[str]:
    """候选名单统一格式化为「昵称（QQ 号）」（同昵称不同 QQ 号不再去重，避免无法分辨）。"""
    return [f"{nickname}（{user_id}）" for user_id, nickname in candidates]


def resolve_user_name(candidates: List[Candidate], user_name: str) -> Dict[str, Any]:
    """按五分支语义把 user_name 解析到唯一候选用户。

    返回 dict：
    - ``{"status": "hit", "user_id": ..., "nickname": ...}``
    - ``{"status": "ambiguous", "names": [...], "message": ...}``（names 为「昵称（QQ 号）」条目）
    - ``{"status": "not_found", "message": ...}``
    """
    wanted = str(user_name or "").strip()
    if not wanted:
        return {"status": "not_found", "message": NAME_NOT_FOUND_TEMPLATE.format(name=wanted)}

    # 精准匹配（裁决取舍①）：昵称完全相等 或 与 user_id（QQ 号）相等（字符串等值比较）
    exact = [c for c in candidates if c[1] == wanted or c[0] == wanted]
    if exact:
        matched = exact
    else:
        # 正则降级只匹配昵称
        try:
            pattern = re.compile(wanted)
        except re.error:
            pattern = re.compile(re.escape(wanted))
        matched = [c for c in candidates if pattern.search(c[1])]

    if not matched:
        return {"status": "not_found", "message": NAME_NOT_FOUND_TEMPLATE.format(name=wanted)}
    if len(matched) == 1:
        user_id, nickname = matched[0]
        return {"status": "hit", "user_id": user_id, "nickname": nickname}

    entries = _format_candidates(matched)
    return {
        "status": "ambiguous",
        "names": entries,
        "message": NAME_AMBIGUOUS_TEMPLATE.format(names="、".join(entries)),
    }
