"""在宿主入库和构建聊天历史前，将表情包含义附在原消息组件中。"""

from __future__ import annotations

import asyncio
import copy
import re
from typing import Any, Dict, Iterator

from maibot_sdk import HookHandler
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from ..emoji_meanings import (
    EMOJI_CONTENT_SUFFIX_PREFIX,
    EMOJI_LABEL_PATTERN,
    build_emoji_content_suffix,
    extract_emoji_refs_from_message,
)


def _components(segments: list) -> Iterator[Dict[str, Any]]:
    """按显示顺序遍历消息与合并转发中的组件，不改动转发节点信息。"""
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        if segment.get("type") == "forward" and isinstance(segment.get("data"), list):
            for node in segment["data"]:
                if isinstance(node, dict) and isinstance(node.get("content"), list):
                    yield from _components(node["content"])
        else:
            yield segment


class EmojiStreamMixin:
    """改写真实消息；不注册 planner/replyer 请求上下文的含义注入 Hook。"""

    @HookHandler(
        "chat.receive.after_process",
        name="inbound_emoji_stream_meaning",
        description="blocking：表情包含义附加入站消息，随原消息进入数据库与聊天流",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_inbound_emoji_stream_meaning(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._append_stream_emoji_meanings(kwargs)

    @HookHandler(
        "send_service.after_build_message",
        name="outbound_emoji_stream_meaning",
        description="blocking：表情包含义附加到出站消息记录，与入站聊天流保持一致",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_outbound_emoji_stream_meaning(self, **kwargs: Any) -> Dict[str, Any]:
        return await self._append_stream_emoji_meanings(kwargs)

    async def _append_stream_emoji_meanings(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self.config.emoji_meaning
        if not self._plugin_enabled() or not cfg.enabled or not cfg.inject_into_chat_stream or self._meaning_store is None:
            return {"action": "continue"}
        message = kwargs.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("raw_message"), list):
            return {"action": "continue"}
        modified_message = copy.deepcopy(message)
        plain_text = str(message.get("processed_plain_text") or "")
        cursor, hits = 0, 0
        deadline = asyncio.get_running_loop().time() + cfg.stream_generation_timeout_seconds
        for component in _components(modified_message["raw_message"]):
            data = component.get("data")
            old_text = data if isinstance(data, str) else ""
            if component.get("type") == "emoji" and not old_text:
                label = EMOJI_LABEL_PATTERN.search(plain_text, cursor)
                old_text = label.group(0) if label else "[表情包]"
            start = plain_text.find(old_text, cursor) if old_text else -1
            if start >= 0:
                cursor = start + len(old_text)
            if component.get("type") != "emoji" or EMOJI_CONTENT_SUFFIX_PREFIX in old_text:
                continue
            refs = extract_emoji_refs_from_message({"raw_message": [component]})
            if not refs:
                continue
            image_hash, description = refs[0]
            if not re.fullmatch(r"[0-9a-fA-F]{64}", image_hash):
                continue
            self._remember_inline_emoji_bytes({"raw_message": [component]})
            used_hash, meaning, image_format = self._meaning_store.content_for_hash(image_hash)
            if not meaning:
                self._enqueue_emoji_meaning(image_hash, description)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining > 0 and image_hash not in self._meaning_abandoned:
                    try:
                        await asyncio.wait_for(self._ensure_meaning_generation(image_hash, description), remaining)
                    except asyncio.TimeoutError:
                        self.ctx.logger.warning("聊天流表情识别等待超时，保留原消息，后台继续（hash=%s）", image_hash[:12])
                    used_hash, meaning, image_format = self._meaning_store.content_for_hash(image_hash)
            if not meaning:
                if start >= 0:
                    cursor = start + len(old_text)
                continue
            new_text = (old_text or "[表情包]") + build_emoji_content_suffix(meaning, image_format)
            component["data"] = new_text
            if start >= 0:
                plain_text = plain_text[:start] + new_text + plain_text[start + len(old_text):]
                cursor = start + len(new_text)
            else:
                plain_text += new_text
                cursor = len(plain_text)
            self._meaning_used.add(used_hash)
            hits += 1
        if not hits:
            return {"action": "continue"}
        modified_message["processed_plain_text"] = plain_text
        modified = dict(kwargs)
        modified["message"] = modified_message
        self.ctx.logger.debug("表情包含义已附加到聊天流原消息：%d 处（message_id=%s）", hits, message.get("message_id", ""))
        return {"action": "continue", "modified_kwargs": modified}
