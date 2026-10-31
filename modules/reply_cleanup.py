"""出站回复清洗：删除中括号内的表情文本，并清理清洗后粘连的标点。

为什么挂在 ``maisaka.reply.before_post_process``：这是**回复正文刚生成、引用/@ 组件还没
引入、分段还没开始**的时刻（宿主 ``builtin_tool/reply.py`` 在分段/错字处理前触发）。
在这个点清洗：

* 不会误伤引用原文、不会误删真实 @ 组件（此时它们还不存在）；
* 完整中括号还没被分段切开——``[表情包图片内容描述：…]`` 若等到出站才处理，
  可能已经被拆到两条消息里，无法再按对删除。

清洗口径（``[plugin] strip_emoji_text``，默认开启）：

1. 成对中括号（``[]`` / ``［］`` / ``【】``）内**包含「表情」二字**时，整对括号连内容一起删；
   覆盖宿主 ``[表情包: 描述]``、插件 ``[表情包标签：…]``、``[表情包图片内容描述：…]``
   以及 ``[表情]`` 等形态；
2. 删除后如果标点粘连（``，。`` / ``，，`` / 行首悬挂的逗号句号等），合并/清理粘连标点，
   并清掉因此变空的小括号/引号对；
3. 只处理 bot 本轮回复正文，不碰用户消息、命令响应或其它插件的发送。

``clean_reply_text`` 是纯函数，便于离线单测；Hook 只是把它接到回复链路上。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Tuple

from maibot_sdk import HookHandler
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

# 同类型成对中括号；内容必须出现「表情」才整对删除。三类分开写，避免 `[表情】` 这种
# 跨类型误配，也避免把普通 `[链接]`、`[图片]` 一并清掉。
_BRACKET_PATTERNS = (
    re.compile(r"\[[^\[\]]*?表情[^\[\]]*?\]"),
    re.compile(r"［[^［］]*?表情[^［］]*?］"),
    re.compile(r"【[^【】]*?表情[^【】]*?】"),
)

# 清洗后可能粘连的标点：中文逗号/句号/顿号/分号/冒号/叹号/问号 + 半角同义标点。
_PUNCT_RUN = re.compile(r"[，。、；：！？,.!?;:]{2,}")
_SPACE_BEFORE_PUNCT = re.compile(r"[ \t\u3000]+([，。、；：！？,.!?;:])")
_LEADING_PUNCT = re.compile(r"^[\s，。、；：！？,.!?;:]+")
_MULTI_SPACE = re.compile(r"[ \t\u3000]{2,}")
_EMPTY_PAIRS = re.compile(r"[（(]\s*[）)]|[「『]\s*[」』]|《\s*》|【\s*】")
_ONLY_PUNCT = re.compile(r"^[\s，。、；：！？,.!?;:]+$")


def strip_emoji_brackets(text: str) -> Tuple[str, int]:
    """删除所有「中括号内含表情」的整对文本；返回 ``(新文本, 删除处数)``。"""
    result = str(text or "")
    total = 0
    for pattern in _BRACKET_PATTERNS:
        result, count = pattern.subn("", result)
        total += count
    return result, total


def _collapse_punct_run(match: re.Match[str]) -> str:
    """把连续标点压成一个：优先保留句末标点，``！？`` 这类双叹问保留原样。"""
    run = match.group(0)
    if len(run) <= 2 and all(ch in "！？!?" for ch in run):
        return run
    for ch in "。！？":
        if ch in run:
            return ch
    return run[0]


def tidy_punctuation(text: str) -> str:
    """清理删除表情括号后留下的粘连标点、悬挂标点与多余空格。"""
    value = _SPACE_BEFORE_PUNCT.sub(r"\1", str(text or ""))
    value = _PUNCT_RUN.sub(_collapse_punct_run, value)
    value = _LEADING_PUNCT.sub("", value)
    value = _EMPTY_PAIRS.sub("", value)
    value = _MULTI_SPACE.sub(" ", value)
    if _ONLY_PUNCT.fullmatch(value):
        return ""
    return value.strip()


def clean_reply_text(text: str) -> Tuple[str, int]:
    """清洗一段回复正文；返回 ``(清洗后文本, 删除的表情括号数)``。

    没有命中表情括号时**原样返回**（不重新排版、不动空格），保证未命中路径零副作用。
    """
    stripped, count = strip_emoji_brackets(text)
    if not count:
        return str(text or ""), 0
    return tidy_punctuation(stripped), count


class ReplyCleanupMixin:
    """``maisaka.reply.before_post_process`` 上的出站表情文本清洗。"""

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="reply_emoji_text_cleanup",
        description=(
            "blocking/early：删除回复正文里中括号包含「表情」的整段文本，并清理粘连标点；"
            "在引用/@ 注入与分段之前执行"
        ),
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_reply_emoji_text_cleanup(self, **kwargs: Any) -> Dict[str, Any]:
        """清洗回复正文（默认开启，``[plugin] strip_emoji_text`` 可关）。

        只改 ``response`` 字段；空回复照常回传空串，由宿主/兜底模块决定后续行为。
        """
        if not self._plugin_enabled():
            return {"action": "continue"}
        if not bool(getattr(self.config.plugin, "strip_emoji_text", True)):
            return {"action": "continue"}
        response = kwargs.get("response")
        if not isinstance(response, str) or "表情" not in response:
            return {"action": "continue"}
        cleaned, count = clean_reply_text(response)
        if count <= 0:
            return {"action": "continue"}
        session_id = str(kwargs.get("session_id") or "").strip()
        modified = dict(kwargs)
        modified["response"] = cleaned
        self.ctx.logger.info(
            "出站清洗：删除 %d 处表情中括号（会话 %s，原 %d 字 → %d 字）",
            count,
            session_id[:12] or "?",
            len(response),
            len(cleaned),
        )
        if not cleaned:
            self.ctx.logger.warning(
                "出站清洗后回复为空（原回复只有表情文本），按空回复交给宿主处理（会话 %s）",
                session_id[:12] or "?",
            )
        return {"action": "continue", "modified_kwargs": modified}
