"""空回复兜底（1.1.3 新增）：replyer 生成可见回复失败时，补一条兜底消息。

## 要解决的场景

宿主 ``src/maisaka/builtin_tool/reply.py`` 在**生成可见回复失败**时（模型返回空正文、
或后处理前 Hook 把正文清空），工具直接返回 ``build_failure_result``——**一条消息都不发**，
用户那头就是"bot 已读不回"。两种情况：

1. ``reply.py:418-431``：``generate_reply_with_context`` 返回空文本
   → 返回 ``"生成可见回复失败。"``，**此时 ``maisaka.reply.before_post_process`` 还没被调用**
   （它在第 434 行，前面第 420 行已 ``return``），所以插件的后处理接管**看不到这一轮**。
2. ``reply.py:448-455``：后处理前 Hook 把正文清空 → ``"回复文本后处理前 Hook 返回了空回复。"``

## 为什么挂在 ``maisaka.replyer.after_response``

宿主在这一步允许 Hook **改写回复正文**（``maisaka_generator_base.py:1317-1337``）：

```python
elif "response" in after_response_kwargs:
    hook_modified_response = str(after_response_kwargs.get("response") or "").strip()
    ...
    response_text = hook_modified_response      # 被采纳，写回 generation_result
```

因此当这一轮模型吐出的正文是空的时候，我们在 ``after_response`` 里把 ``response``
改成一条兜底消息，宿主就会照常往下走（``reply.py`` 拿到非空文本 → 正常发送），
用户看到的就是兜底句而不是"已读不回"。

**探测范围**：只在 ``response`` 与 ``output_items`` 都取不到正文时介入。
若模型给了非空正文、或给了工具调用（``output_items`` 里有 tool_call），一律不介入——
后者是正常的"先调工具、稍后再发正文"流程，不是失败。

## 与「异常兜底提示词」共用配置

兜底文本复用 ``[response_splitter] fallback_prompts``（同一份列表、同样的
``{bot_name}`` / ``{user_name}`` 占位符语义）；列表为空时回退宿主自带兜底提示词
（``build_default_fallback_replies``）。「兜底回复的自称」取 ``[response_splitter]
fallback_nickname``。

## 独立开关

``[empty_reply_fallback] enabled``——**默认关闭**。打开后才会介入；关闭时本模块
**完全不注册副作用**（Hook 直接放行）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from ..post_processing import build_default_fallback_replies

# 兜底句里的占位符（与 fallback_prompts 的语义完全一致）。
_PLACEHOLDER_BOT_NAME = "{bot_name}"
_PLACEHOLDER_USER_NAME = "{user_name}"


class EmptyReplyFallbackMixin:
    """空回复兜底的状态与 Hook。"""

    def _init_empty_reply_fallback(self) -> None:
        """初始化状态（由插件类 ``__init__`` 调用）。

        只记一个"本会话本轮已经补过兜底"的集合：``after_response`` 可能在一轮里被调用
        多次（重试），不设防会让同一轮补出多条兜底。
        """
        self._empty_reply_fallback_done: set = set()

    # ---- 开关 ----

    def _empty_reply_fallback_active(self) -> bool:
        """空回复兜底是否生效：插件开启 + 本模块开关打开。"""
        if not self._plugin_enabled():
            return False
        return bool(getattr(self.config.empty_reply_fallback, "enabled", False))

    # ---- 兜底文本 ----

    @staticmethod
    def _has_text(value: Any) -> bool:
        """``value`` 里是否有非空文本（去空白后非空即算）。"""
        return bool(str(value or "").strip())

    @staticmethod
    def _output_items_have_tool_call(output_items: Any) -> bool:
        """``output_items`` 里是否有工具调用。

        有工具调用 = 模型正常走了"先调工具、稍后再发正文"的流程，**不是**生成失败，
        绝对不能补兜底（否则会打断正常的多步工具链）。
        """
        if not isinstance(output_items, list):
            return False
        for item in output_items:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or item.get("item_type") or "").strip().lower()
            if item_type in {"tool_call", "tool_calls", "function_call"}:
                return True
            if item.get("tool_name") or item.get("tool_calls"):
                return True
        return False

    def _fallback_prompt_list(self) -> List[str]:
        """生效的兜底提示词列表（用户自定义优先，空则用宿主自带的）。"""
        raw = getattr(self.config.response_splitter, "fallback_prompts", None)
        if isinstance(raw, (list, tuple)):
            prompts = [str(item).strip() for item in raw if str(item).strip()]
            if prompts:
                return prompts
        bot_name = str(
            self._resolve_mirror("response_splitter", "fallback_nickname", "bot_nickname", "text") or ""
        )
        return build_default_fallback_replies(bot_name)

    async def _build_empty_reply_fallback_text(self, reply_message_id: str) -> str:
        """抽一条兜底文本并替换占位符。

        ``{bot_name}`` 取「兜底回复的自称」生效值；``{user_name}`` 取要回复的那条消息的
        发送者名称（复用 ``quote_takeover`` 的带 TTL 缓存查询；查不到替换为空串）。
        """
        import random

        prompts = self._fallback_prompt_list()
        text = random.choice(prompts) if prompts else ""
        if _PLACEHOLDER_BOT_NAME not in text and _PLACEHOLDER_USER_NAME not in text:
            return text
        bot_name = str(
            self._resolve_mirror("response_splitter", "fallback_nickname", "bot_nickname", "text") or ""
        )
        user_name = ""
        if reply_message_id and _PLACEHOLDER_USER_NAME in text:
            try:
                target = await self._lookup_reply_target(reply_message_id)
            except Exception as exc:
                target = None
                self.ctx.logger.debug("空回复兜底 user_name 查询失败（按空处理）: %s", exc)
            if target is not None:
                user_name = str(target[2] or "").strip() or str(target[1] or "").strip()
        return text.replace(_PLACEHOLDER_BOT_NAME, bot_name).replace(_PLACEHOLDER_USER_NAME, user_name)

    # ---- Hook ----

    async def handle_replyer_after_response_fallback(self, **kwargs: Any) -> Dict[str, Any]:
        """``maisaka.replyer.after_response``：模型回复为空时补兜底。

        只在 ``response`` 与 ``output_items`` 都取不到正文、且没有工具调用时介入。
        介入方式 = 回传改写后的 ``response``（宿主会采纳并写回 ``generation_result``）。
        """
        if not self._empty_reply_fallback_active():
            return {"action": "continue"}

        response_text = str(kwargs.get("response") or "")
        if self._has_text(response_text):
            return {"action": "continue"}

        output_items = kwargs.get("output_items")
        if self._output_items_have_tool_call(output_items):
            # 正常的多步工具流程（先调工具），不是生成失败。
            return {"action": "continue"}
        if self._output_items_contain_text(output_items):
            # output_items 里其实有正文，宿主自己能取到 —— 不介入。
            return {"action": "continue"}

        session_id = str(kwargs.get("session_id") or "").strip()
        if session_id and session_id in self._empty_reply_fallback_done:
            # 本轮已经补过（重试会再次走到这里）：不重复补。
            return {"action": "continue"}

        reply_message_id = str(kwargs.get("reply_message_id") or "").strip()
        try:
            fallback_text = await self._build_empty_reply_fallback_text(reply_message_id)
        except Exception as exc:
            self.ctx.logger.warning("空回复兜底文本生成失败，放弃介入: %s", exc)
            return {"action": "continue"}
        if not fallback_text:
            return {"action": "continue"}

        if session_id:
            self._empty_reply_fallback_done.add(session_id)
            # 只挡本轮：稍后新的一轮重新允许补兜底（用 task 在下个事件循环 tick 清理）。
            self._schedule_empty_reply_fallback_reset(session_id)

        self.ctx.logger.warning(
            "replyer 生成可见回复为空（attempt=%s），已补发兜底提示词: %r（会话 %s）",
            kwargs.get("attempt"),
            fallback_text[:60],
            session_id,
        )
        modified = dict(kwargs)
        modified["response"] = fallback_text
        return {"action": "continue", "modified_kwargs": modified}

    @staticmethod
    def _output_items_contain_text(output_items: Any) -> bool:
        """``output_items`` 里是否已有非空正文（宿主能自己取到 → 不需要兜底）。"""
        if not isinstance(output_items, list):
            return False
        for item in output_items:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or item.get("item_type") or "").strip().lower()
            if item_type in {"text", "output_text", "message"}:
                if str(item.get("text") or item.get("content") or "").strip():
                    return True
        return False

    def _schedule_empty_reply_fallback_reset(self, session_id: str) -> None:
        """本轮结束后清掉"已补兜底"标记（避免永久占用该会话）。"""
        import asyncio

        async def _reset() -> None:
            await asyncio.sleep(_EMPTY_REPLY_FALLBACK_RESET_SECONDS)
            self._empty_reply_fallback_done.discard(session_id)

        try:
            task = asyncio.create_task(_reset())
            self._track_task(task)
        except Exception:
            # 没有运行中的事件循环（理论上不会）→ 直接清掉，宁可重复补也不要永久锁死。
            self._empty_reply_fallback_done.discard(session_id)


# 补兜底标记的存活时间（秒）：只用来挡住"同一轮的重试"，跨轮必须允许再次补。
_EMPTY_REPLY_FALLBACK_RESET_SECONDS = 90.0
