"""cateye_better_post_processing —— 回复方式抽取 / 表情包互动 / 出站文本规则增强。

历史说明：本插件早期版本曾在框架关闭「回复后处理总开关」时于
``maisaka.reply.before_post_process`` Hook 里复刻后处理逻辑。经核对宿主源码
（``src/maisaka/builtin_tool/reply.py``），分段发送循环锁死在回复工具内部，
受 ``response_post_process.enable_response_post_process`` 全局开关门控：Hook
只能把分段关掉，无法在开关关闭时重新触发分段，也没有任何 Hook 能把一条待发
消息拆成多条（``send_service.*`` 的 ``message`` 载荷始终是单条）。强行绕行
（置空 response 再由插件自发）会让 planner 收到"回复工具失败"的观察结果，
存在模型重新回复导致刷屏的风险，因此本版本移除后处理接管，后处理交还框架
（请保持 ``response_post_process.enable_response_post_process`` 开启）。

当前提供五个互相独立的功能：

1. 引用回复接管（``[quote_reply]``）：框架 ``chat.reply_style.enable_reply_quote``
   关闭时，在 ``send_service.before_send`` 里为指向目标消息（``reply_message_id``
   非空）的回复按权重抽取发送方式：直接回复 / 引用回复 / @回复 / 引用＋@回复。
   @ 回复的@目标为被回复消息的发送者（经 ``message.get_by_id`` 查询并缓存）。
   目标消息过旧有两条规则（超时优先）：目标发出超过 ``stale_age_seconds`` 秒后
   强制不引用直接发送；目标之后已出现 ≥ ``stale_threshold_messages`` 条消息时
   视为"对话已推进"——抽取池剔除"直接回复"，并把 @回复 权重调为
   ``stale_at_weight``（留空则取原 @权重 的一半），避免裸回复指代不明。
   私聊会话自动剔除包含 @ 的权重判定（只有引用/不引用两种可能）。
   同一轮回复只抽取一次（首段生效），后续分段保持原样——与宿主分段语义一致
   （分段循环里仅首段携带对目标消息的引用意图，错别字更正段由宿主原生引用）。
2. 回复后表情包（``[emoji_after_reply]``）：以 ``maisaka.reply.before_post_process``
   （observe）标记"planner 激活了一轮回复"，再通过 ``send_service.after_send``
   （observe）观察本轮全部出站消息；若整轮回复没有携带表情包组件，则按概率
   抽取指定情绪（``emotion`` 非空）或随机表情包，用 ``send.emoji`` 补发
   （发送前检查聊天流表情冷却，见 ``[emoji_cooldown]``）。
3. 表情包跟风（``[emoji_follow]``）：通过 ``chat.receive.after_process``（observe）
   统计群聊里连续入站表情包条数，达到 ``threshold`` 后按配置模式跟发一张：
   ``same``（与最后一个表情包相同情绪）/ ``specified``（配置的指定情绪）/
   ``random``（纯随机）（同样受聊天流表情冷却约束）。
4. 出站文本规则（``[text_rules]``）：在 ``send_service.before_send`` 里对 bot
   发出的纯文本组件应用 ``"词"replace"替换词"`` / ``"词"cover"覆盖文本"`` 规则，
   格式错误的规则在加载时记录日志并忽略（见 ``text_rules.py``）。
5. 表情包含义库（``[emoji_meaning]``）：维护一份插件自有 SQLite 库，为表情包的
   宿主描述标签（replyer 上下文里 ``[表情包: 描述]`` 的"描述"）补录由视觉模型
   生成的"准确内容"，并在 replyer 请求前（``maisaka.replyer.before_request``）
   把上下文中出现的表情包含义注入 ``extra_prompt``。两条补录通道：周期扫描
   宿主表情库补录已有表情包（``database.query`` + ``emoji.get_all`` + ``llm.generate``），
   以及 ``emoji.register.after_build_description`` 钩子即时补录插件安装后新入库
   的表情包。

聊天流表情冷却（``[emoji_cooldown]``）：``send_service.after_send``（observe）观察到
任何来源（planner 的 send_emoji 工具、其它插件、本插件自身）的表情包消息**入库**
（``storage_message=True``）后，该聊天流进入冷却，冷却期内本插件不会主动发送
表情包（回复后表情包与表情包跟风共用同一冷却）。唯一例外是 CLI 控制台平台的
本地渲染路径不经 send_service，不会计入冷却（仅影响本地调试）。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from pathlib import Path
from typing import Any, ClassVar, Deque, Dict, List, Literal, Optional, Tuple

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import CONFIG_RELOAD_SCOPE_SELF, ErrorPolicy, HookMode, HookOrder

from .emoji_meanings import (
    DEFAULT_GENERATION_PROMPT,
    MAX_DESCRIPTION_LENGTH,
    EmojiMeaningStore,
    build_generation_messages,
    build_injection_block,
    extract_emoji_descriptions_from_message,
    sniff_image_format,
)
from .text_rules import ParsedRules, apply_rules, apply_replaces, find_cover, parse_rules

SUPPORTED_CONFIG_VERSION = "0.6.1"


def _ui_i18n(en_label: str, en_hint: str = "") -> Dict[str, Dict[str, Dict[str, str]]]:
    """构造字段级 WebUI i18n 元数据（并入 ``json_schema_extra``，当前提供英文翻译）。

    WebUI 按界面语言读取 ``field.i18n[locale]['label'/'hint']``，未命中时回退到
    字段的中文字段名/描述。
    """
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}

# @ 目标发送者查询缓存时长与容量上限（失败项用更短的负缓存，避免失败路径反复 RPC）。
_TARGET_CACHE_TTL_SECONDS = 600.0
_TARGET_CACHE_NEGATIVE_TTL_SECONDS = 30.0
_TARGET_CACHE_MAX_ENTRIES = 512

# 回复轮/跟风连击状态的最大保留时长（防长期驻留的陈旧状态）。
_STALE_ROUND_SECONDS = 600.0
_STALE_STREAK_SECONDS = 600.0

# 判定"本轮开始前 bot 刚发过表情包"（planner 先 send_emoji 再 reply 的情况）的时间窗。
_RECENT_BOT_EMOJI_WINDOW_SECONDS = 15.0

# 会话近期表情包描述缓存与 @ 目标表情包描述缓存的时效/容量。
_SESSION_EMOJI_TTL_SECONDS = 1800.0
_SESSION_EMOJI_MAX_ENTRIES = 20
_TARGET_EMOJI_TTL_SECONDS = 600.0
_TARGET_EMOJI_MAX_ENTRIES = 256


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Plugin", "description": "Plugin master switch and config version."},
    }

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={
            "label": "启用插件",
            **_ui_i18n("Enabled", "Enable or disable the whole plugin."),
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={"hidden": True, "disabled": True},
    )


class QuoteReplySectionConfig(PluginConfigBase):
    """引用回复接管与回复方式权重配置。"""

    __ui_label__ = "引用回复接管"
    __ui_icon__ = "format_quote"
    __ui_order__ = 1
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Quote Reply Takeover",
            "description": "Weighted reply styles when the host quote-reply switch is off, with stale-target adjustments.",
        },
    }

    takeover: bool = Field(
        default=True,
        description="框架【启用引用回复】关闭时，由插件为指向目标消息的回复按权重抽取回复方式",
        json_schema_extra={
            "label": "接管引用回复",
            **_ui_i18n(
                "Take over quote replies",
                "When the host chat.reply_style.enable_reply_quote switch is off, pick a reply style by weight for replies that target a message.",
            ),
        },
    )
    weight_direct: int = Field(
        default=2,
        ge=0,
        description="权重：直接回复（不引用也不@）",
        json_schema_extra={
            "label": "权重：直接回复",
            **_ui_i18n("Weight: direct reply", "Chance weight of replying with neither quote nor @."),
        },
    )
    weight_quote: int = Field(
        default=6,
        ge=0,
        description="权重：引用回复",
        json_schema_extra={
            "label": "权重：引用回复",
            **_ui_i18n("Weight: quote reply", "Chance weight of quoting the target message."),
        },
    )
    weight_at: int = Field(
        default=1,
        ge=0,
        description="权重：@回复（@被回复消息的发送者，仅群聊生效）",
        json_schema_extra={
            "label": "权重：@回复",
            **_ui_i18n(
                "Weight: @ reply",
                "Weight of @-replying the sender of the target message. Group chats only.",
            ),
        },
    )
    weight_quote_at: int = Field(
        default=1,
        ge=0,
        description="权重：引用＋@回复（仅群聊生效）",
        json_schema_extra={
            "label": "权重：引用＋@回复",
            **_ui_i18n("Weight: quote + @ reply", "Quote the target message and @ its sender. Group chats only."),
        },
    )
    stale_threshold_messages: int = Field(
        default=3,
        ge=0,
        description=(
            "回复目标之后出现 ≥N 条消息即视为'对话已推进'：抽取回复方式时剔除直接回复，"
            "并把 @回复 权重按 stale_at_weight 调整（0=关闭）"
        ),
        json_schema_extra={
            "label": "对话已推进阈值（条）",
            **_ui_i18n(
                "Stale threshold (messages)",
                "When this many messages appear after the target, direct replies are excluded and the @ weight is adjusted. 0 disables.",
            ),
        },
    )
    stale_exclude_bot_messages: bool = Field(
        default=False,
        description="统计目标后消息数时排除 bot 自己的消息（默认计入所有消息）",
        json_schema_extra={
            "label": "统计时排除 bot 消息",
            **_ui_i18n(
                "Exclude bot messages",
                "Exclude the bot's own messages when counting messages after the target. Default counts all messages.",
            ),
        },
    )
    stale_at_weight: int = Field(
        default=-1,
        ge=-1,
        description="对话已推进时 @回复 的权重；-1（或留空）表示自动取原 @权重 的一半",
        json_schema_extra={
            "label": "推进时 @ 权重",
            **_ui_i18n(
                "Stale @ weight",
                "The @ reply weight once the conversation has moved on; -1 (or empty) means half of the original @ weight.",
            ),
        },
    )
    stale_age_seconds: float = Field(
        default=1800.0,
        ge=0.0,
        description=(
            "回复目标发出超过 N 秒后，本次回复强制不引用直接发送"
            "（0=关闭；优先级高于 stale_threshold_messages 的剔直调整）"
        ),
        json_schema_extra={
            "label": "目标超时强制直发（秒）",
            **_ui_i18n(
                "Stale age (seconds)",
                "Force a plain unquoted reply when the target message is older than this. 0 disables; takes priority over the stale threshold rule.",
            ),
        },
    )


class EmojiAfterReplySectionConfig(PluginConfigBase):
    """planner 回复后概率补发表情包配置。"""

    __ui_label__ = "回复后表情包"
    __ui_icon__ = "mood"
    __ui_order__ = 2
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Emoji After Reply",
            "description": "Probabilistically send an emoji when a reply round carried none.",
        },
    }

    enabled: bool = Field(
        default=True,
        description="是否在整轮回复未携带表情包时按概率补发一张",
        json_schema_extra={
            "label": "启用回复后表情包",
            **_ui_i18n("Enabled", "Send an emoji when a whole reply round carried none."),
        },
    )
    probability: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="每次满足条件的回复后补发表情包的概率（0~1）",
        json_schema_extra={
            "label": "补发概率",
            **_ui_i18n("Probability", "Chance (0~1) of sending an emoji after a qualifying reply."),
        },
    )
    emotion: str = Field(
        default="",
        description="抽取表情包使用的情绪标签（留空则随机抽取一张）",
        json_schema_extra={
            "label": "指定情绪标签",
            **_ui_i18n("Emotion tag", "Emoji description tag to fetch; empty means random."),
        },
    )
    quiet_seconds: float = Field(
        default=4.0,
        ge=0.5,
        description="回复分段发送完成后等待多少秒没有新出站消息，才认定本轮结束",
        json_schema_extra={
            "label": "静默判定时长（秒）",
            **_ui_i18n(
                "Quiet window (seconds)",
                "Wait this many seconds without new outgoing messages before treating the round as finished.",
            ),
        },
    )
    round_window_seconds: float = Field(
        default=90.0,
        ge=1.0,
        description="从回复生成开始，超过多少秒未等到出站消息则丢弃该轮记录",
        json_schema_extra={
            "label": "回复轮窗口（秒）",
            **_ui_i18n(
                "Round window (seconds)",
                "Discard the round record if no outgoing message arrives within this many seconds.",
            ),
        },
    )


class EmojiFollowSectionConfig(PluginConfigBase):
    """群友连续表情包跟发配置。"""

    __ui_label__ = "表情包跟风"
    __ui_icon__ = "emoji_emotions"
    __ui_order__ = 3
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Emoji Follow",
            "description": "Follow up with an emoji when group members send stickers consecutively.",
        },
    }

    enabled: bool = Field(
        default=True,
        description="是否在群友连续发送表情包后跟发一张",
        json_schema_extra={
            "label": "启用表情包跟风",
            **_ui_i18n("Enabled", "Follow up with an emoji on consecutive group stickers."),
        },
    )
    mode: Literal["same", "specified", "random"] = Field(
        default="same",
        description="跟发方式：same=与最后一个表情包相同情绪，specified=使用下方指定情绪，random=纯随机",
        json_schema_extra={
            "label": "跟发方式",
            **_ui_i18n(
                "Follow mode",
                "same = same emotion as the last sticker; specified = use the emotion below; random = purely random.",
            ),
        },
    )
    threshold: int = Field(
        default=3,
        ge=1,
        description="群友连续发送多少条表情包后触发跟发（中间出现任何非表情包消息即重新计数）",
        json_schema_extra={
            "label": "触发条数",
            **_ui_i18n(
                "Threshold",
                "Follow once group members send this many stickers in a row; any non-sticker message resets the count.",
            ),
        },
    )
    probability: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="达到条数后实际跟发的概率（0~1）",
        json_schema_extra={
            "label": "跟发概率",
            **_ui_i18n("Probability", "Chance (0~1) of actually following once the threshold is reached."),
        },
    )
    emotion: str = Field(
        default="",
        description="mode=specified 时抽取表情包使用的情绪标签",
        json_schema_extra={
            "label": "指定情绪标签",
            **_ui_i18n("Emotion tag", "Used when mode = specified."),
        },
    )


class EmojiCooldownSectionConfig(PluginConfigBase):
    """聊天流表情包冷却配置。

    任意来源（planner 的 send_emoji 工具、其它插件、本插件自身）的表情包消息
    入库后，该聊天流进入冷却；冷却期内本插件不会主动发送表情包（回复后表情包
    与表情包跟风共用同一冷却）。
    """

    __ui_label__ = "表情包冷却"
    __ui_icon__ = "timer"
    __ui_order__ = 6
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Emoji Cooldown",
            "description": "Per-chat cooldown after any emoji is stored.",
        },
    }

    seconds: float = Field(
        default=300.0,
        ge=0.0,
        description="聊天流表情包冷却时长（秒）：任意来源的表情包入库后，冷却期内插件不主动发送表情包",
        json_schema_extra={
            "label": "冷却时长（秒）",
            **_ui_i18n(
                "Cooldown (seconds)",
                "After any emoji is stored in a chat (from any source), the plugin will not proactively send emojis for this long.",
            ),
        },
    )


class TextRulesSectionConfig(PluginConfigBase):
    """出站文本替换/覆盖规则配置。"""

    __ui_label__ = "文本替换规则"
    __ui_icon__ = "find_replace"
    __ui_order__ = 4
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Text Rules",
            "description": "Replace or overwrite words in the bot's outgoing plain text.",
        },
    }

    rules: List[str] = Field(
        default_factory=list,
        description=(
            '规则列表，每条形如 "shit"replace"filter"（把消息中的 shit 替换为 filter）'
            '或 "草"cover"你好"（包含"草"的消息整条覆盖为"你好"）；格式错误的条目会被忽略'
        ),
        json_schema_extra={
            "label": "替换/覆盖规则",
            "rows": 6,
            **_ui_i18n(
                "Rules",
                'One item per rule: "word"replace"new" replaces occurrences, "word"cover"text" overwrites the whole text when it contains the word. Malformed items are ignored.',
            ),
        },
    )


class EmojiMeaningSectionConfig(PluginConfigBase):
    """表情包含义库与上下文注入配置。"""

    __ui_label__ = "表情包含义库"
    __ui_icon__ = "image_search"
    __ui_order__ = 5
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Emoji Meaning Library",
            "description": "Backfill precise emoji meanings and inject them into the replyer context.",
        },
    }

    enabled: bool = Field(
        default=True,
        description="是否启用表情包含义库（周期补录 + 新表情包即时补录）",
        json_schema_extra={
            "label": "启用含义库",
            **_ui_i18n(
                "Enabled",
                "Periodically backfill meanings for existing emojis and instantly for newly registered ones.",
            ),
        },
    )
    inject_enabled: bool = Field(
        default=True,
        description="是否在回复前把上下文中出现的表情包含义注入 replyer 提示词",
        json_schema_extra={
            "label": "启用上下文注入",
            **_ui_i18n(
                "Context injection",
                "Inject meanings of emojis found in the replied-to message and recent messages into the replyer prompt.",
            ),
        },
    )
    model_task: str = Field(
        default="vlm",
        description="生成含义使用的模型任务名（对应 model_config 的 model_task_config 键，如 vlm）",
        json_schema_extra={
            "label": "模型任务名",
            **_ui_i18n("Model task", "Model task name for meaning generation (see model_task_config, e.g. vlm)."),
        },
    )
    generation_prompt: str = Field(
        default="",
        description="生成含义使用的提示词（留空使用内置提示词）",
        json_schema_extra={
            "label": "生成提示词",
            "rows": 4,
            **_ui_i18n("Generation prompt", "Custom prompt for meaning generation; empty uses the built-in one."),
        },
    )
    max_tokens: int = Field(
        default=256,
        ge=32,
        le=4096,
        description="单次含义生成的 max_tokens",
        json_schema_extra={
            "label": "生成 max_tokens",
            **_ui_i18n("Max tokens", "max_tokens for each meaning generation call."),
        },
    )
    scan_interval_seconds: float = Field(
        default=1800.0,
        ge=60.0,
        description="周期补录通道：扫描宿主表情库间隔（秒）",
        json_schema_extra={
            "label": "库扫描间隔（秒）",
            **_ui_i18n("Scan interval (seconds)", "How often to scan the host emoji library for unrecorded emojis."),
        },
    )
    worker_interval_seconds: float = Field(
        default=30.0,
        ge=5.0,
        description="含义生成工作循环间隔（秒），每轮最多生成 batch_size 条",
        json_schema_extra={
            "label": "生成循环间隔（秒）",
            **_ui_i18n(
                "Worker interval (seconds)",
                "Meaning generation loop interval; each round generates at most batch_size entries.",
            ),
        },
    )
    batch_size: int = Field(
        default=5,
        ge=1,
        le=50,
        description="工作循环每轮最多生成的含义条数（控制 token 消耗）",
        json_schema_extra={
            "label": "每轮生成条数",
            **_ui_i18n("Batch size", "Max meanings generated per worker round (controls token usage)."),
        },
    )
    max_attempts: int = Field(
        default=3,
        ge=1,
        le=20,
        description="同一条含义生成失败的最大尝试次数",
        json_schema_extra={
            "label": "失败重试上限",
            **_ui_i18n("Max attempts", "Max generation attempts per emoji before giving up."),
        },
    )
    max_meaning_length: int = Field(
        default=160,
        ge=20,
        le=1000,
        description="单条含义文本的最大长度（超出截断）",
        json_schema_extra={
            "label": "单条含义最大长度",
            **_ui_i18n("Max meaning length", "Meaning texts are truncated to this length."),
        },
    )
    inject_max_emojis: int = Field(
        default=5,
        ge=1,
        le=20,
        description="单次回复最多注入的表情包含义条数",
        json_schema_extra={
            "label": "单次注入条数上限",
            **_ui_i18n("Injection limit", "Max emoji meanings injected per reply."),
        },
    )


class BetterPostProcessingConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    quote_reply: QuoteReplySectionConfig = Field(default_factory=QuoteReplySectionConfig)
    emoji_after_reply: EmojiAfterReplySectionConfig = Field(default_factory=EmojiAfterReplySectionConfig)
    emoji_follow: EmojiFollowSectionConfig = Field(default_factory=EmojiFollowSectionConfig)
    text_rules: TextRulesSectionConfig = Field(default_factory=TextRulesSectionConfig)
    emoji_meaning: EmojiMeaningSectionConfig = Field(default_factory=EmojiMeaningSectionConfig)
    emoji_cooldown: EmojiCooldownSectionConfig = Field(default_factory=EmojiCooldownSectionConfig)


class BetterPostProcessingPlugin(MaiBotPlugin):
    """回复方式抽取、表情包互动与出站文本规则增强插件。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = BetterPostProcessingConfig
    config_reload_subscriptions: ClassVar[Tuple[str, ...]] = ("bot",)

    def __init__(self) -> None:
        # 必须先初始化 SDK 基类状态（_ctx / _dynamic_api_components / 配置实例等），
        # 否则 Runner 激活插件调用 get_components() 时会因属性缺失而崩溃。
        super().__init__()
        # 缓存的宿主全局配置关键值；在 on_load 与 bot 配置热重载时刷新。
        self._cfg: Dict[str, Any] = {}
        # 插件自身配置解析出的派生状态；在 on_load 与配置热更新时重建。
        self._parsed_rules = ParsedRules()
        self._reply_style_weights: Dict[str, int] = {}
        # @ 目标发送者查询缓存：reply_message_id -> (缓存时刻, user_id, 昵称, 群名片, 消息时间戳)。
        # 查询失败的负缓存项 user_id 为空串。
        self._target_sender_cache: Dict[str, Tuple[float, str, str, str, float]] = {}
        # 回复轮跟踪：session_id -> 轮状态（含自增轮 id、目标消息 id、是否已抽取回复方式）。
        self._reply_rounds: Dict[str, Dict[str, Any]] = {}
        self._reply_round_seq = 0
        # 出站消息活跃令牌（防抖）：session_id -> 递增序号。
        self._outbound_tokens: Dict[str, int] = {}
        # 最近 bot 发送表情包的时间（任意来源，供"本轮已带表情"判定）：session_id -> 单调时钟。
        self._chat_emoji_last: Dict[str, float] = {}
        # 聊天流表情冷却起点（仅入库的表情包记录）：session_id -> 单调时钟。
        self._chat_emoji_cooldown_start: Dict[str, float] = {}
        # 群聊连续表情包计数：session_id -> {"count", "last", "last_desc"}。
        self._emoji_streaks: Dict[str, Dict[str, Any]] = {}
        # "对话已推进"/"回复目标超时"首次触发时用 info 记录，后续降为 debug。
        self._stale_adjust_logged = False
        self._stale_age_logged = False
        # 表情包含义库与补录队列。
        self._meaning_store: Optional[EmojiMeaningStore] = None
        self._meaning_queue: Deque[str] = deque()
        self._meaning_queued: set[str] = set()
        self._meaning_attempts: Dict[str, int] = {}
        # 多次生成失败后放弃的描述标签；周期扫描不再入队，注册钩子可将其复活。
        self._meaning_abandoned: set[str] = set()
        # 会话近期表情包描述缓存（供 replyer 注入）：session_id -> deque[(单调时钟, 描述)]。
        self._session_emoji_descs: Dict[str, Deque[Tuple[float, str]]] = {}
        # 被回复消息中的表情包描述缓存：message_id -> (单调时钟, 描述列表)。
        self._target_emoji_cache: Dict[str, Tuple[float, List[str]]] = {}
        # 后台任务集合（on_unload 时统一取消）。
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        self._rebuild_derived_state()
        await self._refresh_global_config()
        self._initialize_meaning_store()
        # 循环无条件启动（内部按配置与库可用性自门控），保证含义库关闭时状态清理仍有节奏。
        self._track_task(asyncio.create_task(self._meaning_scan_loop()))
        self._track_task(asyncio.create_task(self._meaning_worker_loop()))
        self.ctx.logger.info(
            "插件已加载。引用回复开关=%s，插件接管=%s（权重 %s）；回复后表情包=%s（p=%.2f）；"
            "表情包跟风=%s（mode=%s, threshold=%s）；文本规则：replace=%d, cover=%d, 忽略=%d；"
            "表情包含义库=%s（已收录 %d 条）；聊天流表情冷却=%.0f 秒",
            self._cfg.get("enable_reply_quote"),
            self._quote_takeover_active(),
            self._reply_style_weights,
            self.config.emoji_after_reply.enabled,
            self.config.emoji_after_reply.probability,
            self.config.emoji_follow.enabled,
            self.config.emoji_follow.mode,
            self.config.emoji_follow.threshold,
            len(self._parsed_rules.replaces),
            len(self._parsed_rules.covers),
            len(self._parsed_rules.invalid),
            "启用" if self._meaning_store is not None else "不可用",
            self._meaning_store.count() if self._meaning_store is not None else 0,
            self.config.emoji_cooldown.seconds,
        )

    async def on_unload(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if self._meaning_store is not None:
            self._meaning_store.close()
            self._meaning_store = None
        self.ctx.logger.info("插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._rebuild_derived_state()
            self.ctx.logger.info(
                "插件配置已更新：文本规则 replace=%d, cover=%d, 忽略=%d；回复方式权重 %s",
                len(self._parsed_rules.replaces),
                len(self._parsed_rules.covers),
                len(self._parsed_rules.invalid),
                self._reply_style_weights,
            )
        elif scope == "bot":
            await self._refresh_global_config()
            self.ctx.logger.info(
                "全局配置已热更新，重新读取框架开关：引用回复=%s",
                self._cfg.get("enable_reply_quote"),
            )

    # ------------------------------------------------------------------
    # 配置刷新与派生状态
    # ------------------------------------------------------------------

    async def _refresh_global_config(self) -> None:
        """从宿主全局配置读取与引用回复相关的关键值并缓存。"""
        try:
            value = await self.ctx.config.get("chat.reply_style.enable_reply_quote", True)
        except Exception as exc:  # 能力被拒/读取异常时回退默认
            self.ctx.logger.warning("读取宿主配置 chat.reply_style.enable_reply_quote 失败，使用默认值: %s", exc)
            value = True
        if not isinstance(value, bool):
            # 宿主执行失败时 SDK 原样返回错误 dict（truthy），显式回退避免误判开关状态。
            self.ctx.logger.warning("宿主配置 enable_reply_quote 返回异常形态 %r，使用默认值 True", value)
            value = True
        self._cfg = {"enable_reply_quote": value}

    def _rebuild_derived_state(self) -> None:
        """解析文本规则并重建回复方式权重（插件配置变化时调用）。"""
        self._parsed_rules = parse_rules(self.config.text_rules.rules)
        for bad_rule in self._parsed_rules.invalid:
            self.ctx.logger.warning("文本规则格式错误，已忽略: %r", bad_rule)

        quote_cfg = self.config.quote_reply
        weights = {
            "direct": max(0, int(quote_cfg.weight_direct)),
            "quote": max(0, int(quote_cfg.weight_quote)),
            "at": max(0, int(quote_cfg.weight_at)),
            "quote_at": max(0, int(quote_cfg.weight_quote_at)),
        }
        if sum(weights.values()) <= 0:
            self.ctx.logger.warning("回复方式权重全为 0，回退为仅引用回复")
            weights = {"direct": 0, "quote": 1, "at": 0, "quote_at": 0}
        self._reply_style_weights = weights

    def _plugin_enabled(self) -> bool:
        return bool(self.config.plugin.enabled)

    def _quote_takeover_active(self) -> bool:
        """框架引用回复开关关闭、且插件接管开关开启时，接管引用回复。"""
        if not self._plugin_enabled():
            return False
        if not bool(self.config.quote_reply.takeover):
            return False
        return bool(self._cfg.get("enable_reply_quote", True)) is False

    def _track_task(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # Hook 1：出站消息（文本规则 + 引用回复方式抽取）
    # ------------------------------------------------------------------

    @HookHandler(
        "send_service.before_send",
        name="outbound_message_enhancer",
        description="对 bot 出站文本应用替换/覆盖规则；引用回复接管激活时按权重抽取直接/引用/@/引用＋@回复方式",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_before_send(self, **kwargs: Any) -> Dict[str, Any]:
        modified = dict(kwargs)
        raw_message = modified.get("message")
        if not isinstance(raw_message, dict):
            return {"action": "continue", "modified_kwargs": modified}

        # 拷贝一层，避免直接改动宿主原始载荷结构。
        message = dict(raw_message)
        modified["message"] = message

        try:
            self._apply_text_rules_to_message(message)
        except Exception as exc:
            self.ctx.logger.warning("出站文本规则应用失败，跳过: %s", exc)

        try:
            await self._apply_reply_style(modified, message)
        except Exception as exc:
            self.ctx.logger.warning("回复方式抽取失败，保持原样: %s", exc)

        return {"action": "continue", "modified_kwargs": modified}

    def _apply_text_rules_to_message(self, message: Dict[str, Any]) -> None:
        """对出站消息的纯文本组件应用替换/覆盖规则（原地修改传入副本）。"""
        rules = self._parsed_rules
        if not rules or not self._plugin_enabled():
            return

        components = message.get("raw_message")
        if not isinstance(components, list):
            return

        text_indexes = [
            index
            for index, component in enumerate(components)
            if isinstance(component, dict) and str(component.get("type") or "") == "text"
        ]
        if not text_indexes:
            return

        text_set = set(text_indexes)
        full_text = "".join(
            str(components[index].get("data") or "") for index in text_indexes
        )
        if not full_text:
            return

        covered = find_cover(apply_replaces(full_text, rules.replaces), rules.covers)
        if covered:
            # 命中覆盖规则：整条文本替换为覆盖内容（保留首个文本组件的位置）。
            rebuilt: List[Any] = []
            written = False
            for index, component in enumerate(components):
                if index in text_set:
                    if not written:
                        rebuilt.append({"type": "text", "data": covered})
                        written = True
                    continue
                rebuilt.append(component)
            message["raw_message"] = rebuilt
            message["processed_plain_text"] = covered
            return

        new_components: List[Any] = []
        changed = False
        for index, component in enumerate(components):
            if index not in text_set:
                new_components.append(component)
                continue
            original_text = str(component.get("data") or "")
            new_text = apply_rules(original_text, rules)
            if new_text != original_text:
                new_component = dict(component)
                new_component["data"] = new_text
                new_components.append(new_component)
                changed = True
            else:
                new_components.append(component)

        if changed:
            message["raw_message"] = new_components
            processed = message.get("processed_plain_text")
            if isinstance(processed, str) and processed:
                message["processed_plain_text"] = apply_rules(processed, rules)

    async def _apply_reply_style(self, modified: Dict[str, Any], message: Dict[str, Any]) -> None:
        """引用回复接管激活时，为指向目标消息的回复按权重抽取发送方式。

        同一轮回复（由 ``maisaka.reply.before_post_process`` 标记）只抽取一次：
        首个携带轮目标消息 ID 的分段生效，后续分段保持原样（与宿主分段语义一致，
        错别字更正段由宿主原生引用上一条消息，不经本插件处理）。轮信息缺失时
        退回逐条抽取。
        """
        if not self._quote_takeover_active():
            return

        set_reply = bool(modified.get("set_reply", False))
        if set_reply:
            return
        reply_message_id = str(modified.get("reply_message_id") or "").strip()
        if not reply_message_id:
            return

        session_id = str(message.get("session_id") or "").strip()
        reply_round = self._reply_rounds.get(session_id) if session_id else None
        if reply_round is not None:
            round_target_id = str(reply_round.get("target_id") or "").strip()
            if not round_target_id or reply_message_id != round_target_id:
                # 同一轮内指向其它消息的发送（如更正段已由宿主原生引用），不动。
                return
            if reply_round.get("style_done"):
                return
            reply_round["style_done"] = True

        # 目标消息过旧的两条规则（超时优先）：
        # 1) 目标发出超过 stale_age_seconds 秒 → 强制不引用直接发送；
        # 2) 目标之后已出现 ≥ stale_threshold_messages 条消息 → 剔除直接回复、调低 @ 权重。
        quote_cfg = self.config.quote_reply
        stale_threshold = int(quote_cfg.stale_threshold_messages)
        stale_age = float(quote_cfg.stale_age_seconds)
        weights: Optional[Dict[str, float]] = None
        if stale_threshold > 0 or stale_age > 0:
            target_info = await self._lookup_reply_target(reply_message_id)
            if target_info is not None:
                target_ts = target_info[3]
                now_ts = time.time()
                if stale_age > 0 and target_ts > 0 and now_ts - target_ts > stale_age:
                    if not self._stale_age_logged:
                        self._stale_age_logged = True
                        self.ctx.logger.info(
                            "首次触发回复目标超时：目标发出已超过 %.0f 秒（本次实际 %.0f 秒），"
                            "本次起超时回复将不引用直接发送",
                            stale_age,
                            now_ts - target_ts,
                        )
                    else:
                        self.ctx.logger.debug(
                            "回复目标超时（距今 %.0f 秒 > %.0f 秒），不引用直接发送",
                            now_ts - target_ts,
                            stale_age,
                        )
                    return
                if stale_threshold > 0:
                    count = await self._count_messages_after_target(session_id, reply_message_id, target_ts)
                    if count >= stale_threshold:
                        weights = self._build_stale_weights()
                        if not self._stale_adjust_logged:
                            self._stale_adjust_logged = True
                            self.ctx.logger.info(
                                "首次触发对话已推进调整：回复目标之后已有 %d 条消息（≥%d），"
                                "本次起将剔除直接回复并调整 @ 权重 %s",
                                count,
                                stale_threshold,
                                weights,
                            )
                        else:
                            self.ctx.logger.debug(
                                "回复目标已过旧（其后 %d 条消息 ≥ %d），调整回复方式权重: %s",
                                count,
                                stale_threshold,
                                weights,
                            )

        # 私聊只有"引用/不引用"两种可能：自动剔除包含 @ 的权重判定。
        if not self._is_group_message(message):
            if weights is None:
                weights = {name: float(weight) for name, weight in self._reply_style_weights.items()}
            weights["at"] = 0.0
            weights["quote_at"] = 0.0

        style = self._pick_reply_style(weights)
        if style == "direct":
            return

        if style in ("at", "quote_at") and self._is_group_message(message):
            target = await self._lookup_reply_target(reply_message_id)
            if target is not None:
                user_id, nickname, cardname = target[0], target[1], target[2]
                message["raw_message"] = self._inject_at_component(
                    message.get("raw_message"),
                    user_id=user_id,
                    nickname=nickname,
                    cardname=cardname,
                )
                if style == "quote_at":
                    modified["set_reply"] = True
                return
            # @ 目标查不到（消息被清理等）时回退为引用回复。
            self.ctx.logger.debug("@ 目标查询失败（reply_message_id=%s），回退为引用回复", reply_message_id)
        elif style in ("at", "quote_at"):
            # 私聊的权重池已剔除 @ 相关判定，此处仅为兜底（理论上不可达）。
            self.ctx.logger.debug("私聊会话不支持 @ 回复（reply_message_id=%s），回退为引用回复", reply_message_id)

        modified["set_reply"] = True

    def _pick_reply_style(self, weights: Optional[Dict[str, float]] = None) -> str:
        """按权重抽取回复方式（只从正权重中抽；权重全空时回退为引用回复）。"""
        pool = (
            weights
            if weights is not None
            else {name: float(weight) for name, weight in self._reply_style_weights.items()}
        )
        styles = [name for name, weight in pool.items() if weight > 0]
        if not styles:
            return "quote"
        return random.choices(styles, weights=[float(pool[name]) for name in styles], k=1)[0]

    def _build_stale_weights(self) -> Dict[str, float]:
        """构造"对话已推进"时的回复方式权重：剔除直接回复，@权重调为指定值或原值一半。"""
        cfg = self.config.quote_reply
        weights: Dict[str, float] = {name: float(weight) for name, weight in self._reply_style_weights.items()}
        weights["direct"] = 0.0
        if cfg.stale_at_weight >= 0:
            weights["at"] = float(cfg.stale_at_weight)
        else:
            weights["at"] = max(0.0, float(self._reply_style_weights.get("at", 0)) / 2.0)
        return weights

    async def _count_messages_after_target(self, session_id: str, target_id: str, target_ts: float) -> int:
        """统计目标消息之后入库的消息条数；失败或时间戳缺失返回 -1（回退为不调整）。

        查询区间为 ``[目标时间戳, 现在]``，limit 取 ``阈值+1``（latest 模式）：
        区间内消息多于阈值时必然返回阈值+1 条，足以判定；目标消息自身按
        message_id 剔除。
        """
        cfg = self.config.quote_reply
        threshold = int(cfg.stale_threshold_messages)
        if not session_id or target_ts <= 0:
            return -1
        try:
            result = await self.ctx.message.get_by_time_in_chat(
                chat_id=session_id,
                start_time=target_ts,
                end_time=time.time(),
                limit=threshold + 1,
                filter_mai=bool(cfg.stale_exclude_bot_messages),
            )
        except Exception as exc:
            self.ctx.logger.debug("统计目标后消息数失败（reply_message_id=%s）: %s", target_id, exc)
            return -1
        if not isinstance(result, list):
            self.ctx.logger.debug("统计目标后消息数失败，返回形态异常: %r", type(result).__name__)
            return -1
        return sum(
            1
            for item in result
            if isinstance(item, dict) and str(item.get("message_id") or "") != target_id
        )

    @staticmethod
    def _is_group_message(message: Dict[str, Any]) -> bool:
        message_info = message.get("message_info")
        if not isinstance(message_info, dict):
            return False
        return isinstance(message_info.get("group_info"), dict)

    @staticmethod
    def _inject_at_component(
        raw_message: Any,
        *,
        user_id: str,
        nickname: str,
        cardname: str,
    ) -> List[Any]:
        """把 at 组件插入组件列表首位（返回新列表，不修改入参）。"""
        components = list(raw_message) if isinstance(raw_message, list) else []
        at_component = {
            "type": "at",
            "data": {
                "target_user_id": user_id,
                "target_user_nickname": nickname or None,
                "target_user_cardname": cardname or None,
            },
        }
        return [at_component, *components]

    async def _lookup_reply_target(self, reply_message_id: str) -> Optional[Tuple[str, str, str, float]]:
        """查询被回复消息的发送者与时间戳（带 TTL 缓存）；失败返回 None。

        返回 ``(user_id, 昵称, 群名片, 消息时间戳)``；时间戳缺失时为 0.0。
        查询失败会写入短 TTL 的负缓存，避免同一条目标消息反复触发失败 RPC。
        """
        now = time.monotonic()
        cached = self._target_sender_cache.get(reply_message_id)
        if cached is not None:
            ttl = _TARGET_CACHE_TTL_SECONDS if cached[1] else _TARGET_CACHE_NEGATIVE_TTL_SECONDS
            if now - cached[0] <= ttl:
                if not cached[1]:
                    return None
                return cached[1], cached[2], cached[3], cached[4]
            self._target_sender_cache.pop(reply_message_id, None)

        target = await self._fetch_reply_target(reply_message_id)
        if target is None:
            self._target_sender_cache[reply_message_id] = (now, "", "", "", 0.0)
            return None

        user_id, nickname, cardname, message_ts = target
        if len(self._target_sender_cache) >= _TARGET_CACHE_MAX_ENTRIES:
            # 先淘汰过期项；仍满则整体重建。
            for key in [
                key
                for key, entry in self._target_sender_cache.items()
                if now - entry[0] > _TARGET_CACHE_TTL_SECONDS
            ]:
                self._target_sender_cache.pop(key, None)
            if len(self._target_sender_cache) >= _TARGET_CACHE_MAX_ENTRIES:
                self._target_sender_cache.clear()
        self._target_sender_cache[reply_message_id] = (now, user_id, nickname, cardname, message_ts)
        return user_id, nickname, cardname, message_ts

    async def _fetch_reply_target(self, reply_message_id: str) -> Optional[Tuple[str, str, str, float]]:
        """执行一次被回复消息查询并提取发送者与时间戳；结构不合法返回 None。"""
        result = await self.ctx.message.get_by_id(reply_message_id)
        if not isinstance(result, dict) or result.get("success") is False:
            return None
        message_info = result.get("message_info")
        user_info = message_info.get("user_info") if isinstance(message_info, dict) else None
        if not isinstance(user_info, dict):
            return None
        user_id = str(user_info.get("user_id") or "").strip()
        if not user_id:
            return None
        nickname = str(user_info.get("user_nickname") or "").strip()
        cardname = str(user_info.get("user_cardname") or "").strip()
        try:
            message_ts = float(result.get("timestamp") or 0.0)
        except (TypeError, ValueError):
            message_ts = 0.0
        return user_id, nickname, cardname, message_ts

    # ------------------------------------------------------------------
    # Hook 2：回复轮标记（planner 激活了回复）
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="reply_round_marker",
        description="observe：标记 planner 激活了一轮回复，供回复方式一致性与回复后表情包判定使用",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_reply_round_marker(self, **kwargs: Any) -> None:
        if not self._plugin_enabled():
            return
        # 轮信息仅服务于两个消费方：回复方式一致性抽取与回复后表情包判定。
        if not (self._quote_takeover_active() or self.config.emoji_after_reply.enabled):
            return
        session_id = str(kwargs.get("session_id") or "").strip()
        if not session_id:
            return
        now = time.monotonic()
        # 出站消息里没有表情包时才需要补发；若本轮开始前几秒内 bot 刚发过表情包
        # （planner 先调 send_emoji 再回复的情况），视为本轮已有表情包。
        recent_emoji_ts = self._chat_emoji_last.get(session_id, 0.0)
        had_emoji = 0.0 < (now - recent_emoji_ts) <= _RECENT_BOT_EMOJI_WINDOW_SECONDS
        self._reply_round_seq += 1
        self._reply_rounds[session_id] = {
            "id": self._reply_round_seq,
            "started": now,
            "had_emoji": had_emoji,
            "window": float(self.config.emoji_after_reply.round_window_seconds),
            "target_id": str(kwargs.get("reply_message_id") or "").strip(),
            "style_done": False,
        }
        self._prune_stale_state(now)

    # ------------------------------------------------------------------
    # Hook 3：出站消息观察（回复轮表情包判断）
    # ------------------------------------------------------------------

    @HookHandler(
        "send_service.after_send",
        name="outbound_observer",
        description="observe：观察每条已发出消息的组件，判断本轮回复是否携带表情包并安排补发判定",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_outbound_observer(self, **kwargs: Any) -> None:
        if not bool(kwargs.get("sent", False)):
            return
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return
        session_id = str(message.get("session_id") or "").strip()
        if not session_id:
            return

        now = time.monotonic()
        components = message.get("raw_message")
        has_emoji = isinstance(components, list) and any(
            isinstance(component, dict) and str(component.get("type") or "") == "emoji"
            for component in components
        )
        if has_emoji:
            self._chat_emoji_last[session_id] = now
            # 只要入库就进入聊天流表情冷却（storage_message 为宿主在本轮发送中生效的值）。
            if bool(kwargs.get("storage_message", True)):
                self._chat_emoji_cooldown_start[session_id] = now

        reply_round = self._reply_rounds.get(session_id)
        if reply_round is None:
            return
        if now - float(reply_round.get("started", now)) > float(reply_round.get("window", 90.0)):
            self._reply_rounds.pop(session_id, None)
            return
        if has_emoji:
            reply_round["had_emoji"] = True

        # 每条出站消息都会刷新防抖令牌；最后一次出站消息静默 quiet_seconds 后判定。
        quiet_seconds = float(self.config.emoji_after_reply.quiet_seconds)
        token = self._outbound_tokens.get(session_id, 0) + 1
        self._outbound_tokens[session_id] = token
        self._track_task(
            asyncio.create_task(
                self._decide_reply_emoji(session_id, token, quiet_seconds, int(reply_round.get("id", 0)))
            )
        )

    async def _decide_reply_emoji(self, session_id: str, token: int, quiet_seconds: float, round_id: int) -> None:
        """等待出站消息静默后判定是否补发表情包。"""
        try:
            await asyncio.sleep(quiet_seconds)
        except asyncio.CancelledError:
            return
        if self._outbound_tokens.get(session_id) != token:
            return  # 期间有新的出站消息，由更晚的任务判定。
        try:
            await self._consume_reply_round(session_id, round_id)
        except Exception as exc:
            self.ctx.logger.warning("回复后表情包判定失败: %s", exc)

    async def _consume_reply_round(self, session_id: str, round_id: int) -> None:
        """消费一个待判定回复轮：整轮无表情包时按配置补发。

        仅当会话当前轮 id 与任务创建时的轮 id 一致时才消费，避免 planner 连续
        两轮回复时前一轮的判定任务误消费新一轮的记录。
        """
        reply_round = self._reply_rounds.get(session_id)
        if reply_round is None or int(reply_round.get("id", 0)) != round_id:
            return
        self._reply_rounds.pop(session_id, None)

        now = time.monotonic()
        if reply_round.get("had_emoji"):
            return
        if now - float(reply_round.get("started", now)) > float(reply_round.get("window", 90.0)):
            return

        cfg = self.config.emoji_after_reply
        if not self._plugin_enabled() or not cfg.enabled:
            return
        if self._emoji_cooldown_active(session_id, now):
            return
        if random.random() >= cfg.probability:
            return

        emoji_base64 = await self._fetch_emoji_base64(cfg.emotion)
        if not emoji_base64:
            self.ctx.logger.debug("未取到可用表情包（emotion=%r），跳过补发", cfg.emotion)
            return

        await self._send_emoji(session_id, emoji_base64, source="回复后表情包")

    # ------------------------------------------------------------------
    # Hook 4：入站消息观察（群友连续表情包跟风）
    # ------------------------------------------------------------------

    @HookHandler(
        "chat.receive.after_process",
        name="inbound_emoji_observer",
        description="observe：统计群聊连续入站表情包条数，达到阈值后按配置模式跟发表情包",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_inbound_emoji_observer(self, **kwargs: Any) -> None:
        if not self._plugin_enabled():
            return
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return
        if bool(message.get("is_notify", False)):
            return
        session_id = str(message.get("session_id") or "").strip()
        if not session_id:
            return

        components = message.get("raw_message")
        has_emoji = isinstance(components, list) and any(
            isinstance(component, dict) and str(component.get("type") or "") == "emoji"
            for component in components
        )
        if not has_emoji:
            # 连续计数被非表情包消息打断，归零。
            self._emoji_streaks.pop(session_id, None)
            return

        now = time.monotonic()
        descs = extract_emoji_descriptions_from_message(message)
        # 会话近期表情包缓存服务于 replyer 含义注入，群聊/私聊都要记录。
        if descs:
            self._remember_session_emoji_descs(session_id, now, descs)

        # —— 表情包跟风（仅群聊）——
        cfg = self.config.emoji_follow
        if not cfg.enabled or not self._is_group_message(message):
            return

        streak = self._emoji_streaks.get(session_id)
        if streak is None or now - float(streak.get("last", 0.0)) > _STALE_STREAK_SECONDS:
            streak = {"count": 0, "last": 0.0, "last_desc": ""}
        streak["count"] = int(streak.get("count", 0)) + 1
        streak["last"] = now
        if descs:
            streak["last_desc"] = descs[-1]
        self._emoji_streaks[session_id] = streak

        if streak["count"] < cfg.threshold:
            return
        # 触发后立即重新计数；概率/冷却落空也需重新攒满 threshold 才会再次尝试。
        self._emoji_streaks.pop(session_id, None)
        self._track_task(asyncio.create_task(self._decide_follow_emoji(session_id, str(streak.get("last_desc") or ""))))

    def _remember_session_emoji_descs(self, session_id: str, now: float, descs: List[str]) -> None:
        """把入站消息中的表情包描述追加进会话缓存（限长限时效，按消息内出现顺序入桶）。"""
        bucket = self._session_emoji_descs.get(session_id)
        if bucket is None:
            bucket = deque()
            self._session_emoji_descs[session_id] = bucket
        for desc in descs:
            bucket.append((now, desc))
        while len(bucket) > _SESSION_EMOJI_MAX_ENTRIES:
            bucket.popleft()

    def _recent_emoji_descs(self, session_id: str) -> List[str]:
        """读取会话近期表情包描述（新→旧，剔除过期项）。"""
        if not session_id:
            return []
        bucket = self._session_emoji_descs.get(session_id)
        if not bucket:
            return []
        now = time.monotonic()
        while bucket and now - bucket[0][0] > _SESSION_EMOJI_TTL_SECONDS:
            bucket.popleft()
        return [desc for _, desc in reversed(bucket)]

    async def _lookup_target_emoji_descs(self, reply_message_id: str) -> List[str]:
        """查询被回复消息中出现的表情包描述（带 TTL 缓存；失败返回空）。"""
        now = time.monotonic()
        cached = self._target_emoji_cache.get(reply_message_id)
        if cached is not None and now - cached[0] <= _TARGET_EMOJI_TTL_SECONDS:
            return cached[1]

        descs: List[str] = []
        try:
            result = await self.ctx.message.get_by_id(reply_message_id)
            if isinstance(result, dict) and result.get("success") is not False:
                descs = extract_emoji_descriptions_from_message(result)
        except Exception as exc:
            self.ctx.logger.debug("查询被回复消息的表情包失败（message_id=%s）: %s", reply_message_id, exc)

        if len(self._target_emoji_cache) >= _TARGET_EMOJI_MAX_ENTRIES:
            for key in [
                key
                for key, entry in self._target_emoji_cache.items()
                if now - entry[0] > _TARGET_EMOJI_TTL_SECONDS
            ]:
                self._target_emoji_cache.pop(key, None)
            if len(self._target_emoji_cache) >= _TARGET_EMOJI_MAX_ENTRIES:
                self._target_emoji_cache.clear()
        self._target_emoji_cache[reply_message_id] = (now, descs)
        return descs

    async def _decide_follow_emoji(self, session_id: str, last_desc: str) -> None:
        """按配置模式跟发一张表情包。"""
        try:
            cfg = self.config.emoji_follow
            now = time.monotonic()
            if self._emoji_cooldown_active(session_id, now):
                return
            if random.random() >= cfg.probability:
                return

            emotion = ""
            if cfg.mode == "same":
                if not last_desc:
                    self.ctx.logger.debug("最后一个表情包缺少描述，无法按相同情绪跟发，跳过")
                    return
                emotion = last_desc
            elif cfg.mode == "specified":
                emotion = cfg.emotion
                if not emotion:
                    self.ctx.logger.debug("mode=specified 但未配置情绪标签，跳过跟发")
                    return

            emoji_base64 = await self._fetch_emoji_base64(emotion)
            if not emoji_base64:
                self.ctx.logger.debug("未取到可用表情包（emotion=%r），跳过跟发", emotion)
                return

            await self._send_emoji(session_id, emoji_base64, source="表情包跟风")
        except Exception as exc:
            self.ctx.logger.warning("表情包跟风执行失败: %s", exc)

    # ------------------------------------------------------------------
    # 表情包获取与发送
    # ------------------------------------------------------------------

    async def _fetch_emoji_base64(self, emotion: str) -> Optional[str]:
        """按情绪标签（留空则随机）抽取一张表情包的 base64 数据。"""
        try:
            if emotion.strip():
                result = await self.ctx.emoji.get_by_description(emotion.strip())
                if isinstance(result, dict) and result.get("base64"):
                    return str(result["base64"])
                if isinstance(result, dict) and result.get("success") is False:
                    self.ctx.logger.warning("按情绪抽取表情包失败（emotion=%r）: %s", emotion, result.get("error"))
                return None
            result = await self.ctx.emoji.get_random(1)
            if isinstance(result, list) and result:
                first = result[0]
                if isinstance(first, dict) and first.get("base64"):
                    return str(first["base64"])
            elif isinstance(result, dict) and result.get("success") is False:
                self.ctx.logger.warning("随机抽取表情包失败: %s", result.get("error"))
            return None
        except Exception as exc:
            self.ctx.logger.warning("获取表情包失败: %s", exc)
            return None

    async def _send_emoji(self, session_id: str, emoji_base64: str, *, source: str) -> None:
        """用 send.emoji 能力发送表情包；失败仅记录日志。"""
        try:
            sent = await self.ctx.send.emoji(emoji_base64, session_id)
            if sent:
                self.ctx.logger.info("已向会话 %s 补发表情包（%s）", session_id, source)
            else:
                self.ctx.logger.warning("表情包发送失败（%s，会话 %s）", source, session_id)
        except Exception as exc:
            self.ctx.logger.warning("表情包发送异常（%s，会话 %s）: %s", source, session_id, exc)

    def _emoji_cooldown_active(self, session_id: str, now: float) -> bool:
        """聊天流表情冷却：任意来源的表情包入库后，冷却期内插件不主动发送表情。"""
        last = self._chat_emoji_cooldown_start.get(session_id)
        return last is not None and now - last < float(self.config.emoji_cooldown.seconds)

    # ------------------------------------------------------------------
    # 表情包含义库（周期补录 + 新表情包即时补录 + replyer 注入）
    # ------------------------------------------------------------------

    def _initialize_meaning_store(self) -> None:
        """初始化插件自有的表情包含义 SQLite 库（失败不阻塞其它功能）。"""
        try:
            data_dir = getattr(self.ctx.paths, "data_dir", None)
            if not data_dir:
                raise RuntimeError("ctx.paths.data_dir 不可用")
            store = EmojiMeaningStore(Path(str(data_dir)) / "emoji_meanings.db")
            store.initialize()
            self._meaning_store = store
        except Exception as exc:
            self._meaning_store = None
            self.ctx.logger.warning("表情包含义库初始化失败，含义补录与注入不可用: %s", exc)

    @HookHandler(
        "emoji.register.after_build_description",
        name="emoji_register_observer",
        description="observe：新表情包完成描述生成时，把其描述标签加入含义补录队列（即时补录通道）",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_emoji_register_observer(self, **kwargs: Any) -> None:
        if not self._plugin_enabled() or not self.config.emoji_meaning.enabled:
            return
        if self._meaning_store is None:
            return
        # 宿主在 hook 触发后才把 description 写回 emoji 对象，因此优先读 hook 载荷的 description。
        description = str(kwargs.get("description") or "").strip()
        if not description:
            emoji_payload = kwargs.get("emoji")
            if isinstance(emoji_payload, dict):
                description = str(emoji_payload.get("description") or "").strip()
        if not description:
            return
        # 新注册是"复活"放弃条目的唯一入口。
        self._meaning_abandoned.discard(description)
        self._enqueue_emoji_meaning(description, force=True)

    @HookHandler(
        "maisaka.replyer.before_request",
        name="replyer_emoji_meaning_injector",
        description="当回复目标或会话近期消息中出现表情包时，把含义库中的准确内容注入 extra_prompt",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_replyer_emoji_injector(self, **kwargs: Any) -> Dict[str, Any]:
        modified = dict(kwargs)
        try:
            block = await self._build_emoji_meaning_block(modified)
        except Exception as exc:
            self.ctx.logger.warning("表情包含义注入失败，跳过: %s", exc)
            return {"action": "continue", "modified_kwargs": modified}
        if not block:
            return {"action": "continue", "modified_kwargs": modified}
        existing = str(modified.get("extra_prompt") or "").strip()
        modified["extra_prompt"] = f"{existing}\n\n{block}" if existing else block
        return {"action": "continue", "modified_kwargs": modified}

    async def _build_emoji_meaning_block(self, modified: Dict[str, Any]) -> str:
        """收集回复目标与会话近期消息中的表情包描述，从含义库生成注入文本块。"""
        cfg = self.config.emoji_meaning
        if not self._plugin_enabled() or not cfg.enabled or not cfg.inject_enabled:
            return ""
        if self._meaning_store is None:
            return ""

        # 被回复的消息优先（bot 正在对某个表情包作出回应）。
        refs: List[str] = []
        reply_message_id = str(modified.get("reply_message_id") or "").strip()
        if reply_message_id:
            refs.extend(await self._lookup_target_emoji_descs(reply_message_id))
        session_id = str(modified.get("session_id") or "").strip()
        refs.extend(self._recent_emoji_descs(session_id))

        meanings = self._meaning_store.get_meanings(refs)
        entries: List[Tuple[str, str]] = []
        for desc in refs:
            if len(entries) >= cfg.inject_max_emojis:
                break
            meaning = meanings.get(desc)
            if meaning and all(desc != existing_desc for existing_desc, _ in entries):
                entries.append((desc, meaning))
        return build_injection_block(entries)

    def _enqueue_emoji_meaning(self, description: str, *, force: bool = False) -> None:
        """把描述标签加入含义补录队列（去重、限长、放弃条目默认不再入队）。"""
        normalized = str(description or "").strip()
        if not normalized or len(normalized) > MAX_DESCRIPTION_LENGTH:
            return
        if not force and normalized in self._meaning_abandoned:
            return
        if normalized in self._meaning_queued:
            return
        self._meaning_queued.add(normalized)
        self._meaning_queue.append(normalized)

    async def _meaning_scan_loop(self) -> None:
        """周期补录通道：定期扫描宿主表情库，把没有含义的描述加入队列。"""
        first = True
        while True:
            try:
                if first:
                    first = False
                    # 启动后稍等片刻，等宿主表情库就绪。
                    await asyncio.sleep(15.0)
                else:
                    await asyncio.sleep(max(60.0, float(self.config.emoji_meaning.scan_interval_seconds)))
                if self.config.emoji_meaning.enabled and self._meaning_store is not None:
                    await self._scan_emoji_library()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.ctx.logger.warning("表情包含义库扫描失败: %s", exc)
                await asyncio.sleep(60.0)

    async def _scan_emoji_library(self) -> None:
        """扫描宿主 images 表中的表情包，把缺少含义的描述加入补录队列。

        只取"已注册、有文件、未禁用"的行：宿主 ``emoji.get_all`` 只序列化内存
        库（已注册且文件可用），Images 表里仅完成描述构建、未注册成功或文件
        缺失的行永远取不到图，不过滤会导致这些条目被周期扫描无限复活。
        """
        rows = await self.ctx.db.query("Images", filters={"image_type": "emoji"})
        if not isinstance(rows, list):
            self.ctx.logger.warning("扫描表情包库失败，database.query 返回形态异常: %r", type(rows).__name__)
            return
        known = self._meaning_store.known_descriptions()
        enqueued = 0
        for row in rows:
            if not isinstance(row, dict) or bool(row.get("is_banned", False)):
                continue
            if not bool(row.get("is_registered", False)) or bool(row.get("no_file_flag", False)):
                continue
            description = str(row.get("description") or "").strip()
            if not description or len(description) > MAX_DESCRIPTION_LENGTH:
                continue
            if description in known or description in self._meaning_queued:
                continue
            self._enqueue_emoji_meaning(description)
            enqueued += 1
        if enqueued:
            self.ctx.logger.info(
                "表情包含义补录扫描：新增待生成 %d 条（库内表情 %d，已收录 %d）",
                enqueued,
                len(rows),
                len(known),
            )

    async def _meaning_worker_loop(self) -> None:
        """含义生成工作循环：每轮从队列取 batch_size 条，借助 emoji.get_all 取图后调 VLM。"""
        while True:
            try:
                await asyncio.sleep(max(5.0, float(self.config.emoji_meaning.worker_interval_seconds)))
                # 循环无条件启动，顺带承担全局状态清理的节拍（R3：不依赖回复轮标记触发）。
                self._prune_stale_state(time.monotonic())
                if self.config.emoji_meaning.enabled and self._meaning_store is not None:
                    await self._process_meaning_batch()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.ctx.logger.warning("表情包含义生成工作循环异常: %s", exc)

    async def _process_meaning_batch(self) -> None:
        """处理一批待补录描述：取图、生成含义、入库。"""
        cfg = self.config.emoji_meaning
        batch: List[str] = []
        while self._meaning_queue and len(batch) < cfg.batch_size:
            description = self._meaning_queue.popleft()
            self._meaning_queued.discard(description)
            batch.append(description)
        if not batch:
            return

        try:
            snapshot = await self.ctx.emoji.get_all()
        except Exception as exc:
            # RPC 异常时整批重新入队，等下一轮再试（避免批次丢失后只能靠周期扫描找回）。
            self.ctx.logger.warning("获取表情包库快照异常，本轮 %d 条含义生成顺延: %s", len(batch), exc)
            for description in batch:
                self._enqueue_emoji_meaning(description, force=True)
            return
        if not isinstance(snapshot, list):
            self.ctx.logger.warning("获取表情包库快照失败，本轮 %d 条含义生成顺延", len(batch))
            for description in batch:
                self._enqueue_emoji_meaning(description, force=True)
            return

        # emoji.get_all 载荷不含 file_hash，按描述标签取第一张匹配图。
        desc_to_image: Dict[str, Tuple[str, str]] = {}
        for item in snapshot:
            if not isinstance(item, dict):
                continue
            description = str(item.get("description") or "").strip()
            image_base64 = str(item.get("base64") or "")
            if not description or not image_base64 or description in desc_to_image:
                continue
            desc_to_image[description] = (image_base64, sniff_image_format(image_base64))

        for description in batch:
            image = desc_to_image.get(description)
            if image is None:
                # 描述不在运行库中：可能尚未注册完成或已被删除，计入尝试次数。
                self._record_meaning_failure(description, "表情库中未找到该描述")
                continue
            try:
                meaning = await self._generate_emoji_meaning(description, image[0], image[1])
            except Exception as exc:
                self.ctx.logger.warning("表情包含义生成失败（desc=%r）: %s", description, exc)
                self._record_meaning_failure(description, str(exc))
                continue
            self._meaning_attempts.pop(description, None)
            self._meaning_store.upsert(description, meaning, image_format=image[1], source="scan")
            self.ctx.logger.info("已收录表情包含义（desc=%r，长度 %d）", description, len(meaning))

    async def _generate_emoji_meaning(self, description: str, image_base64: str, image_format: str) -> str:
        """调用视觉模型生成一条表情包的准确含义。"""
        cfg = self.config.emoji_meaning
        prompt_text = cfg.generation_prompt.strip() or DEFAULT_GENERATION_PROMPT
        messages = build_generation_messages(prompt_text, image_base64, image_format)
        result = await self.ctx.llm.generate(messages, model=cfg.model_task, max_tokens=cfg.max_tokens)
        if not isinstance(result, dict):
            raise RuntimeError(f"llm.generate 返回形态异常: {type(result).__name__}")
        if result.get("success") is False:
            raise RuntimeError(str(result.get("error") or "llm.generate 执行失败"))
        text = " ".join(str(result.get("response") or "").split())
        if not text:
            raise RuntimeError("视觉模型返回空描述")
        if len(text) > cfg.max_meaning_length:
            text = text[: cfg.max_meaning_length]
        return text

    def _record_meaning_failure(self, description: str, reason: str) -> None:
        """记录一次含义生成失败；超过上限后放弃（周期扫描不再复活，注册钩子可解除）。"""
        cfg = self.config.emoji_meaning
        attempts = self._meaning_attempts.get(description, 0) + 1
        if attempts >= cfg.max_attempts:
            self._meaning_attempts.pop(description, None)
            self._meaning_abandoned.add(description)
            self.ctx.logger.warning(
                "表情包含义生成多次失败，放弃（desc=%r，尝试 %d 次，末次原因：%s）",
                description,
                attempts,
                reason,
            )
            return
        self._meaning_attempts[description] = attempts
        self._enqueue_emoji_meaning(description, force=True)

    # ------------------------------------------------------------------
    # 状态清理
    # ------------------------------------------------------------------

    def _prune_stale_state(self, now: float) -> None:
        """清理长期驻留的陈旧回复轮、计数与冷却状态。"""
        for session_id in [
            key
            for key, value in self._reply_rounds.items()
            if now - float(value.get("started", 0.0)) > max(_STALE_ROUND_SECONDS, float(value.get("window", 90.0)))
        ]:
            self._reply_rounds.pop(session_id, None)
        for session_id in [
            key
            for key, value in self._emoji_streaks.items()
            if now - float(value.get("last", 0.0)) > _STALE_STREAK_SECONDS
        ]:
            self._emoji_streaks.pop(session_id, None)
        for session_id in [key for key, ts in self._chat_emoji_last.items() if now - ts > _STALE_STREAK_SECONDS]:
            self._chat_emoji_last.pop(session_id, None)
        # 冷却起点的保留时长不得低于配置的冷却时长，否则长冷却会被清理截断。
        cooldown_keep = max(_STALE_STREAK_SECONDS, float(self.config.emoji_cooldown.seconds))
        for session_id in [
            key for key, ts in self._chat_emoji_cooldown_start.items() if now - ts > cooldown_keep
        ]:
            self._chat_emoji_cooldown_start.pop(session_id, None)
        for session_id in [
            key
            for key, bucket in self._session_emoji_descs.items()
            if not bucket or now - bucket[-1][0] > _SESSION_EMOJI_TTL_SECONDS
        ]:
            self._session_emoji_descs.pop(session_id, None)
        for message_id in [
            key
            for key, entry in self._target_emoji_cache.items()
            if now - entry[0] > _TARGET_EMOJI_TTL_SECONDS
        ]:
            self._target_emoji_cache.pop(message_id, None)


def create_plugin() -> BetterPostProcessingPlugin:
    """Runner 加载入口。"""
    return BetterPostProcessingPlugin()
