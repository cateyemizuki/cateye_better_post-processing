"""总结器：把私聊记录交给 LLM 概括。

请求体使用与官方一致的结构化上下文形态：system 提示词 + 一条 user 消息
（仅用户与 bot 的私聊消息，按时间排序，尾部附加总结要求提示词）。

迁移自 ``cateye_mai_cross-chat_recall/summarizer.py``（阶段 3 合并，逐行等价），
唯一差异：配置读取口径改为合并后的**单节平铺** ``[cross_chat_recall]``
（原 ``config.summarize`` 子节降级为该节内的扁平字段，见计划书 6.2 的"嵌套风险预案"）。

提示词（1.2.0 定稿）：``DEFAULT_SUMMARY_PROMPT`` 是这里的模块级内置常量，同时被
``plugin.py`` 用作 ``[cross_chat_recall] prompt`` 的**默认值**——生成的默认配置文件里
就是内置全文；用户清空（或只留空白）时本模块回退到同一个常量。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping

from .helpers import extract_message_fields, fmt_ts, msg_timestamp
from .logging_setup import get_logger

DEFAULT_SUMMARY_PROMPT = (
    "你将看到 {bot_name} 与 {user_nickname} 的私聊记录（每行格式为 [时间] 说话者：内容）。"
    "请以「{bot_name}与{user_nickname}的私聊」为主线，输出一段供 {bot_name} 日后回忆的概括，要求：\n"
    "1. 按时间顺序概括关键事件与话题，尽量带上时间点（如「9月29日中午」「30日凌晨」），写清大概什么事件、聊了什么；\n"
    "2. 写明双方各自做了什么、说了什么，达成的约定、未完成或悬而未决的话题；\n"
    "3. 如果记录较长无法全部覆盖，至少要完整概括最近一两次聊天的时间与内容；\n"
    "4. 结尾单独一行以「需记住：」开头，列出 {bot_name} 应记住的关键事实（称呼、约定、计划、偏好等）。\n"
    "直接使用双方名字（{bot_name} 与 {user_nickname}），不要用「bot」「用户」这类代称；"
    "不要编造记录中不存在的内容；总长度控制在 400 字以内。"
)

_TAIL_PROMPT = "\n\n以上是私聊记录原文。请根据以上私聊记录输出概括（只输出概括内容本身，不要复述原文）。"


def render_prompt(template: str, *, bot_name: str, user_nickname: str, user_id: str) -> str:
    """把提示词模板中的占位符替换为实际值。

    支持 {bot_name} / {user_nickname} / {user_id}；无占位符时原样返回。
    """
    return (
        template
        .replace("{bot_name}", bot_name)
        .replace("{user_nickname}", user_nickname)
        .replace("{user_id}", user_id)
    )


class Summarizer:
    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._logger = get_logger("summarizer")

    def _build_chat_text(self, messages: List[Mapping[str, Any]], user_id: str, nickname: str) -> str:
        bot_name = self._plugin.bot_name
        lines: List[str] = []
        for msg in messages:
            fields = extract_message_fields(msg)
            ts = msg_timestamp(msg)
            text = fields.get("text") or "（非文本消息）"
            if fields.get("is_self"):
                who = f"{bot_name}（bot）"
            else:
                uid = fields.get("user_id") or user_id
                who = f"{fields.get('nickname') or nickname or '用户'}（{uid}）"
            lines.append(f"[{fmt_ts(ts)}] {who}：{text}")
        return "\n".join(lines)

    async def summarize(
        self,
        *,
        user_id: str,
        nickname: str,
        messages: List[Mapping[str, Any]],
    ) -> str | None:
        """调用 LLM 生成概括；失败返回 None（不抛异常）。"""
        cfg = self._plugin.config.cross_chat_recall
        if not messages:
            self._logger.warning("用户 %s：总结请求体为空，跳过", user_id)
            return None

        chat_text = self._build_chat_text(messages, user_id, nickname)
        # 提示词取值（1.2.0 定稿）：配置默认值就是 DEFAULT_SUMMARY_PROMPT 的**全文**；
        # ``strip()`` 让"用户强制清空 / 只留空白"一并回退内置，非空则原样用自定义。
        template = str(cfg.prompt or "").strip() or DEFAULT_SUMMARY_PROMPT
        system_prompt = render_prompt(
            template,
            bot_name=self._plugin.bot_name,
            user_nickname=nickname or user_id,
            user_id=user_id,
        )
        prompt_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": chat_text + _TAIL_PROMPT},
        ]

        kwargs: Dict[str, Any] = {}
        model = str(cfg.model or "").strip()
        if model:
            kwargs["model"] = model  # 任务名（replyer 等）或具体模型名，宿主自动识别
        max_tokens = int(cfg.max_tokens or 0)
        if max_tokens > 0:
            kwargs["max_tokens"] = max_tokens

        self._logger.info(
            "用户 %s：发起总结 LLM 请求（消息 %d 条，model=%r，max_tokens=%s，请求体 %d 字符）",
            user_id, len(messages), model or "(默认)", max_tokens or None, len(chat_text),
        )
        self._logger.debug("用户 %s：总结请求体全文：\n%s", user_id, prompt_messages[1]["content"])

        started = time.time()
        result = await self._call(prompt_messages, kwargs)
        # 指定具体模型没有跨模型兜底（§8.3）：失败时回退一次纯任务路由
        if not self._succeeded(result) and "model" in kwargs:
            self._logger.warning("用户 %s：指定模型 %r 调用失败，回退任务路由重试一次", user_id, kwargs["model"])
            result = await self._call(prompt_messages, {k: v for k, v in kwargs.items() if k != "model"})
        cost = time.time() - started

        if self._succeeded(result):
            response = str(result["response"]).strip()
            self._logger.info(
                "用户 %s：总结完成（%.1fs，实际模型=%s，输出 %d 字符）",
                user_id, cost, result.get("model") or "?", len(response),
            )
            self._logger.debug("用户 %s：总结结果全文：\n%s", user_id, response)
            return response
        error = result.get("error") if isinstance(result, dict) else result
        self._logger.error("用户 %s：总结 LLM 调用失败（%.1fs）: %s", user_id, cost, error)
        return None

    @staticmethod
    def _succeeded(result: Any) -> bool:
        return isinstance(result, dict) and bool(result.get("success")) and bool(str(result.get("response") or "").strip())

    async def _call(self, prompt_messages: List[Dict[str, str]], kwargs: Dict[str, Any]) -> Any:
        try:
            return await self._plugin.ctx.llm.generate(prompt_messages, **kwargs)
        except Exception as exc:
            self._logger.error("总结 LLM 调用异常: %s", exc)
            return {"success": False, "error": str(exc)}
