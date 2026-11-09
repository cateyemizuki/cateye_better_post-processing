"""替换宿主 replyer「简短表达」尾部提示词（1.4.5 新增，默认关闭）。

宿主在 planner 的 reply 工具带 ``reply_style="简短表达"`` 时，会给 replyer 请求**末尾**
追加一句短回复提示（``_build_requested_reply_style_message``，标准路径是最后一条
user 消息，复古模板路径则嵌在模板正文里）：

* MaiBot 1.3.16：``请简短的回复，允许句子残缺，奇怪表达，倒装，省略，符合口语习惯，符合省力随意回复习惯``
* MaiBot 1.4.0 起：``请简短的回复，符合口语习惯，1-2句短句即可。不要长篇大论，无复杂修辞和比喻。``

本模块挂在 ``maisaka.replyer.before_model_request``（BLOCKING/NORMAL）上，把这句原文
**逐字**替换为 ``[replyer_post_process] host_replyer_tail_prompt`` 的文案。四条条件
**同时满足**才动手，缺一原样放行：

1. 插件总开关（``[plugin] enabled``）开启，且 ``replace_host_replyer_tail_prompt`` 开启；
2. ``host_replyer_tail_prompt`` 文案非空（留空 = 不替换）；
3. ``reply_tool_args.reply_style`` **恰好是**「简短表达」——「正常回复」本来就不追加
   提示词、「长回复」追加的是另一句，两者都不碰（不误伤其它类型下的尾部提示词）；
4. 请求 items 里确实存在上面那句原文（只动 ``type == "text"`` 的 part 文本，
   meta / item 结构 / 未命中条目一字不改）。

改的是**即将发给模型的请求副本**，宿主状态、消息库与上下文缓存都不受影响；关掉开关
即完全回退。重试（regenerate）时 Hook 每次都会重新触发，替换是幂等的。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import HookHandler
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

# 宿主「简短表达」尾部提示词的已知原文（逐字匹配；两种宿主版本各一句，命中即换）。
# 只登记「简短表达」这一档的原文——「正常回复」不追加任何提示词，「长回复」是另一句，
# 都不在这里，天然不会被误伤。
_HOST_SHORT_REPLY_TAIL_PROMPTS: Tuple[str, ...] = (
    # MaiBot 1.3.16（latest）：
    "请简短的回复，允许句子残缺，奇怪表达，倒装，省略，符合口语习惯，符合省力随意回复习惯",
    # MaiBot 1.4.0+：
    "请简短的回复，符合口语习惯，1-2句短句即可。不要长篇大论，无复杂修辞和比喻。",
)

# planner reply 工具声明简短表达时的 reply_style 取值（宿主 retro_prompt.SHORT_REPLY_STYLE）。
_SHORT_REPLY_STYLE = "简短表达"


class ReplyerTailPromptMixin:
    """「replyer后处理」：把宿主短回复尾部提示词替换为插件配置的文案。"""

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="replyer_tail_prompt_rewriter",
        description="blocking：planner 传「简短表达」时，把宿主追加的短回复尾部提示词替换为插件配置文案",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_replyer_tail_prompt_rewrite(self, **kwargs: Any) -> Dict[str, Any]:
        cfg = getattr(self.config, "replyer_post_process", None)
        if cfg is None or not bool(getattr(cfg, "replace_host_replyer_tail_prompt", False)):
            return {"action": "continue"}
        if not self._plugin_enabled():
            return {"action": "continue"}
        replacement = str(getattr(cfg, "host_replyer_tail_prompt", "") or "").strip()
        if not replacement:
            return {"action": "continue"}
        # 只在 planner 的 reply 工具声明「简短表达」时动手；其它 reply_style 的尾部提示词
        # 完全不碰（「正常回复」没有提示词，「长回复」是另一句）。
        reply_tool_args = kwargs.get("reply_tool_args")
        reply_style = ""
        if isinstance(reply_tool_args, dict):
            reply_style = str(reply_tool_args.get("reply_style") or "").strip()
        if reply_style != _SHORT_REPLY_STYLE:
            return {"action": "continue"}
        items = kwargs.get("items")
        if not isinstance(items, list) or not items:
            return {"action": "continue"}
        try:
            new_items, hits = self._replace_tail_prompt_in_items(items, replacement)
        except Exception as exc:
            self.ctx.logger.warning("替换宿主短回复尾部提示词失败，保持原样: %s", exc)
            return {"action": "continue"}
        if not hits or new_items is None:
            return {"action": "continue"}
        modified = dict(kwargs)
        modified["items"] = new_items
        self.ctx.logger.info(
            "已替换宿主「简短表达」尾部提示词：%d 处（会话 %s）",
            hits,
            str(kwargs.get("session_id") or "?")[:12],
        )
        return {"action": "continue", "modified_kwargs": modified}

    @staticmethod
    def _replace_tail_prompt_in_items(
        items: List[Any], replacement: str
    ) -> Tuple[Optional[List[Any]], int]:
        """把 items 文本段里的宿主短回复尾部提示词逐字替换；返回 (新 items 或 None, 命中数)。

        只动 ``type == "text"`` 的 part 文本，其余 part / meta / item 结构**一字不动**——
        未命中的条目原样复用（宿主按 ``item_id`` 回收引用），命中的条目浅拷贝后只换
        parts 里的文本。
        """
        changed: Optional[List[Any]] = None
        hits = 0
        for index, item in enumerate(items):
            parts = item.get("parts") if isinstance(item, dict) else None
            if not isinstance(parts, list):
                continue
            new_parts: List[Any] = []
            item_changed = False
            for part in parts:
                if not isinstance(part, dict) or str(part.get("type") or "") != "text":
                    new_parts.append(part)
                    continue
                text = part.get("text")
                if not isinstance(text, str):
                    new_parts.append(part)
                    continue
                new_text = text
                for old_prompt in _HOST_SHORT_REPLY_TAIL_PROMPTS:
                    # 用户把替换文案填成宿主原文时等于没改，跳过免得空转。
                    if old_prompt == replacement:
                        continue
                    if old_prompt in new_text:
                        new_text = new_text.replace(old_prompt, replacement)
                        hits += 1
                if new_text != text:
                    item_changed = True
                    new_parts.append({**part, "text": new_text})
                else:
                    new_parts.append(part)
            if item_changed:
                if changed is None:
                    changed = list(items)
                changed[index] = {**item, "parts": new_parts}
        return changed, hits
