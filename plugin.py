"""cateye_better_post_processing —— 回复后处理接管 / 回复方式抽取 / 表情包互动 / 出站文本规则增强。

**代码结构**（0.10.1 起按"接管对象"拆分）：

| 文件 | 职责 |
|---|---|
| ``plugin.py`` | 配置模型、生命周期、共享基础设施（回复轮记录、文本规则、表情包功能），组合下面两个 mixin |
| ``modules/requirements.py`` | 各模块的宿主前置条件与可用性判定 |
| ``modules/post_process_takeover.py`` | 后处理接管（错别字 + 分段 + 多段发送） |
| ``modules/quote_takeover.py`` | 引用回复接管（直接/引用/@/引用＋@ 权重抽取） |
| ``post_processing.py`` | 宿主后处理算法的逐行复刻（纯逻辑，可离线单测） |

**模块前置条件**：两个接管模块都要求宿主**关闭对应能力**才会工作，且都额外要求关闭
**丰富回复**（``experimental.enable_rich_reply``）：

* 引用回复接管：宿主 ``chat.reply_style.enable_reply_quote = false`` + 丰富回复关闭；
* 后处理接管：宿主 ``response_post_process.enable_response_post_process = false`` + 丰富回复关闭。

任一条件未满足时，对应模块**完全静默**（不改写、不介入、不发送），插件配置里打开也没用；
表情包相关功能与文本规则不受这些条件约束。启动与每次配置变更都会打印各模块的可用状态。

历史说明：本插件 0.1.x 曾在框架关闭「回复后处理总开关」时于
``maisaka.reply.before_post_process`` Hook 里复刻后处理逻辑，0.2.0~0.9.0 一度移除，
0.9.1 还原为"换行拼接一条消息"，0.10.0 起**完整复刻宿主后处理框架效果**（含每段一条消息）。
宿主的分段发送循环锁死在回复工具内部、受
``response_post_process.enable_response_post_process`` 全局开关门控，单靠
``before_post_process`` 一个 Hook 无法把一条待发消息拆成多条；因此改用 **Hook 组合 +
``ctx.send.text``** 复现同一效果（见 ``modules/post_process_takeover.py`` 的模块说明）。

当前提供六个互相独立的功能：

1. 后处理接管（``[response_splitter]``）：框架
   ``response_post_process.enable_response_post_process`` 关闭时，在
   ``maisaka.reply.before_post_process``（blocking）里以**与 MaiBot 完全一致的原逻辑**
   接管错别字注入 + 分段 + 颜文字保护 + 长度/句数守卫（``post_processing.py`` 逐行复刻
   ``process_llm_response_segments`` 与 ``ChineseTypoGenerator``）。
   分段**照宿主那样每段一条消息**依次发出：第 1 段走宿主原生发送链
   （引用/写库/历史同步原生），第 2..N 段由插件补发并按宿主的打字速度模拟打字，
   更正段引用上一段。
   分段/错别字/打字速度参数默认读宿主自身配置（``response_splitter.*``、``chinese_typo.*``、
   ``response_post_process.typing_speed``、``bot.nickname``）；插件里也有**同名镜像节**，
   留空 = 跟随宿主，填了值 = 以插件为准（见 ``[response_splitter]`` 等节）。
   框架开关开启时插件完全不介入。
2. 引用回复接管（``[quote_reply]`` 群聊 / ``[quote_reply_private]`` 私聊）：
   框架 ``chat.reply_style.enable_reply_quote`` 关闭时，在
   ``send_service.before_send`` 里为指向目标消息（``reply_message_id`` 非空）的回复
   按权重抽取发送方式：直接回复 / 引用回复 / @回复 / 引用＋@回复（私聊恒不 @，
   权重池只有直接回复与引用回复两项）。@ 回复的@目标为被回复消息的发送者
   （经 ``message.get_by_id`` 查询并缓存）。**群聊与私聊的权重、过旧规则与
   "同一消息只引用一次"记录完全独立**（``_reply_style_weights`` /
   ``_reply_style_weights_private``，按会话类型取用）。
   目标消息过旧有两条规则（超时优先）：目标发出超过 ``stale_age_seconds`` 秒后
   **强制直接回复**——既**不引用**也**不 @**（就是权重池里的"直接回复"，
   不是"以引用的方式发出去"）；目标之后已出现 ≥ ``stale_threshold_messages`` 条消息时
   视为"对话已推进"——抽取池剔除"直接回复"，并把 @回复 权重调为
   ``stale_at_weight``（留空则取原 @权重 的一半），避免裸回复指代不明。

   **同一消息只引用一次**（``quote_once_per_target``，群聊/私聊各自独立开关与记录）：
   一条目标消息一旦被引用过（本插件抽到引用/引用＋@，或在
   ``send_service.after_send`` 观测到它以 ``set_reply=True`` 发出——宿主自带的、
   其它插件注入的都算），之后**任何**对该消息的回复都不再引用，只可能直接回复或
   （群聊）@；抽到"直接回复"不消耗这次机会，下一轮 planner 再次回复同一条消息时
   仍有机会抽到引用。该规则优先于两条过旧规则（含 ``stale_age_seconds`` 的
   "目标超时后强制直接回复"），因此对"bot 回复自己刚发过的消息"这类超出时间窗的目标
   同样生效。记录按 session 存放（群聊/私聊天然分开），单会话上限
   ``_QUOTED_TARGET_MAX_ENTRIES`` 条（超出按登记顺序淘汰最旧），整会话记录
   保留 ``_QUOTED_TARGET_SESSION_TTL_SECONDS``。

   生效范围（0.7.1 收紧）：只处理**本插件已登记的回复轮**（由
   ``maisaka.reply.before_post_process`` 标记）中**指向轮目标消息**的**首条含文本
   分段**，抽取一次后该轮不再抽取——后续分段（含富回复把图片/表情挂在最后一段的
   情况）、非纯文本消息（表情/图片/语音/转发）、以及其它插件或命令响应携带
   ``reply_message_id`` 的发送（例如 ciallo 在 ``before_send`` 里注入引用的
   Ciallo 消息）一律不动；宿主已设置 ``set_reply`` 的发送也不重复处理。轮记录在
   "回复后表情包"判定完成后仍然保留（只标记 ``emoji_done``），否则 4 秒静默后
   分段才发出的回复会因记录消失而退化为逐条抽取。
   @ 注入为 ``[at, 文本" "]`` 两段（与宿主 ``attach_at`` 口径一致，QQ 不会自动补
   空格）；原文本已以空白开头时不再重复补，已带前导 at 时不重复 @。
   同一轮回复只抽取一次（首段生效），后续分段保持原样——与宿主分段语义一致
   （分段循环里仅首段携带对目标消息的引用意图，错别字更正段由宿主原生引用）。
3. 回复后表情包（``[emoji_after_reply]``）：以 ``maisaka.reply.before_post_process``
   （observe）标记"planner 激活了一轮回复"，再通过 ``send_service.after_send``
   （observe）观察本轮全部出站消息；若整轮回复没有携带表情包组件，则按概率
   抽取指定情绪（``emotion`` 非空）或随机表情包，用 ``send.emoji`` 补发
   （发送前检查聊天流表情冷却，见 ``[emoji_cooldown]``）。

   planner 是否**打算自己发表情**是提前可知的：宿主工具调用按模型输出顺序串行
   执行，而 ``send_emoji`` 内部要跑视觉子代理选图（数秒到数十秒），常常晚于
   "出站静默"判定——只看已发出的消息必然漏判。因此本插件额外挂
   ``maisaka.planner.after_response``（observe），扫描本轮 ``output_items`` 中的
   工具调用名（``planner_emoji_tools``），命中即为该会话打"本轮 planner 自行处理
   表情"标记（``planned_emoji_ttl_seconds`` 秒），本轮不再补发（贴表情类工具同样
   计入，避免"planner 贴了表情、插件又补一张"）。

   回复轮只有在 ``first_send_timeout_seconds`` 秒内看到本轮出站消息才算成立：
   超时说明这轮回复并没有真的发出去（发送失败 / 被其它插件在
   ``send_service.before_send`` 中止），直接丢弃该轮，避免之后**任何来源**的出站
   消息（其它插件的命令响应、主动问候、转发插件）"继承"这一轮并触发补发。
4. 表情包跟风（``[emoji_follow]``）：通过 ``chat.receive.after_process``（observe）
   统计群聊里连续入站表情包条数，达到 ``threshold`` 后按配置模式跟发一张：
   ``same``（与最后一个表情包相同情绪）/ ``specified``（配置的指定情绪）/
   ``random``（纯随机）（同样受聊天流表情冷却约束）。宿主没有 self 过滤
   （``ignore_self_message`` 是适配器侧行为），而 NapCat/SnowLuma 出站表情是按
   ``image/sub_type=1`` 下发的、入站又会被判为 emoji 组件——因此本功能默认
   按 ``additional_config`` 里的 ``self_id`` 剔除 bot 自身消息与插件自注入的
   合成记录（``ignore_self_messages``），连击保鲜时长可配
   （``streak_window_seconds``）。
5. 出站文本规则（``[text_rules]``）：在 ``send_service.before_send`` 里对 bot
   发出的纯文本组件应用 ``"词"replace"替换词"`` / ``"词"cover"覆盖文本"`` 规则，
   格式错误的规则在加载时记录日志并忽略（见 ``text_rules.py``）。
   ``apply_scope="reply_flow"``（默认）时只作用于 **planner 拉起的本轮回复自己发出的
   消息**（宿主回复工具的分段，含错别字更正段）：宿主的出站 Hook 载荷里没有来源
   字段，判别依据是本插件的回复轮记录（会话 + 轮目标消息 id，或"引用了本轮已发出
   的某条消息"）。其它插件用 ``ctx.send.*`` 直接发出的文本、宿主命令提示等一律
   不改写；需要覆盖 bot 全部出站文本时可改为 ``all_outbound``。
6. 表情包含义库（``[emoji_meaning]``）：维护一份插件自有 SQLite 库，为表情包的
   宿主描述标签（replyer 上下文里 ``[表情包: 描述]`` 的"描述"）补录由视觉模型
   生成的"准确内容"，并在 replyer 请求前（``maisaka.replyer.before_request``）
   把上下文中出现的表情包含义注入 ``extra_prompt``。``inject_request_types``
   （默认 ``["maisaka.replyer"]``）限定只注入 **planner 拉起的正常回复流程**：
   插件自己拉起回复生成（``generator_api`` / ``plugin.<id>`` 等）时不管；插件
   激活 planner（主动触发、注入上下文）后由 planner 拉起的 reply 照常注入。
   两条补录通道：周期扫描宿主表情库补录已有表情包（``database.query`` +
   ``emoji.get_random`` 抽样 + ``llm.generate``），以及
   ``emoji.register.after_build_description`` 钩子即时补录插件安装后新入库的表情包。

聊天流表情冷却（``[emoji_cooldown]``）：``send_service.after_send``（observe）观察到
任何来源（planner 的 send_emoji 工具、其它插件、本插件自身）的表情包消息发出后，
该聊天流进入冷却（默认连 ``storage_message=False`` 未入库的表情也计入，见
``count_unstored``），冷却期内本插件不会主动发送表情包（回复后表情包与表情包跟风
共用同一冷却）。唯一例外是 CLI 控制台平台的本地渲染路径不经 send_service，不会
计入冷却（仅影响本地调试）。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, ClassVar, Deque, Dict, List, Literal, Optional, Tuple, Union

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
from .modules.post_process_takeover import (
    _HOST_MIRROR_EXTRA_FIELDS,
    _HOST_MIRROR_FIELDS,
    _POST_PROCESS_CONFIG_KEYS,
    read_host_config_values,
    PostProcessTakeoverMixin,
)
from .modules.quote_takeover import QuoteTakeoverMixin
from .modules.requirements import REQUIRED_HOST_PATHS, evaluate_module, iter_module_statuses

SUPPORTED_CONFIG_VERSION = "0.11.2"

# 项目根目录（插件位于 <root>/plugins/<plugin_dir>/，用于定位宿主 depends-data 里的字频表）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _ui_i18n(en_label: str, en_hint: str = "") -> Dict[str, Dict[str, Dict[str, str]]]:
    """构造字段级 WebUI i18n 元数据（并入 ``json_schema_extra``，当前提供英文翻译）。

    WebUI 按界面语言读取 ``field.i18n[locale]['label'/'hint']``，未命中时回退到
    字段的中文字段名/描述。
    """
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


# 回复轮/跟风连击状态的最大保留时长（防长期驻留的陈旧状态）。
_STALE_ROUND_SECONDS = 600.0
_STALE_STREAK_SECONDS = 600.0

# 含义生成抽样张数上下限与未命中放弃阈值。
# 宿主 emoji.get_all 会把整个表情库 base64 塞进单个 RPC 帧，大库超过 16MB 帧上限
# （线上实测 47MB 被拒），因此改用 emoji.get_random 分批抽样，k 按帧错误自适应。
_MEANING_SAMPLE_K_START = 10
_MEANING_SAMPLE_K_MAX = 60
_MEANING_SAMPLE_MISS_CAP = 100

# 判定"本轮开始前 bot 刚发过表情包"（planner 先 send_emoji 再 reply 的情况）的时间窗。
_RECENT_BOT_EMOJI_WINDOW_SECONDS = 15.0

# 会话近期表情包描述缓存与 @ 目标表情包描述缓存的时效/容量。
_SESSION_EMOJI_TTL_SECONDS = 1800.0
_SESSION_EMOJI_MAX_ENTRIES = 20
_TARGET_EMOJI_TTL_SECONDS = 600.0
_TARGET_EMOJI_MAX_ENTRIES = 256


# 回复轮已发出消息 id 的保留条数（错别字更正段的归属判定用）。
_ROUND_SENT_ID_MAX_ENTRIES = 20

# 文本规则归属判定里"首条分段等待窗"的兜底秒数（宿主配置为 0 时使用）。
_REPLY_FLOW_ARM_FALLBACK_SECONDS = 20.0


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
            "x-toml-comment": "是否启用本插件。",
            **_ui_i18n("Enabled", "Enable or disable the whole plugin."),
        },
    )
    rich_reply_gate: bool = Field(
        default=True,
        description=(
            "两个接管模块（引用回复接管 / 后处理接管）是否要求宿主关闭「丰富回复」"
            "（experimental.enable_rich_reply）。默认开启门控：宿主开着丰富回复时，回复工具会把"
            "图片/表情/@ 附件挂到分段上，与多段发送、引用注入的语义冲突，因此两个接管保持静默。"
            "关闭本项后，宿主开着丰富回复也允许接管——此时附件只挂在首段、补发分段不带附件，"
            "请自行确认可接受"
        ),
        json_schema_extra={
            "label": "丰富回复门控",
            "x-toml-comment": "开启时：宿主开着「丰富回复」就不接管（附件与分段语义冲突）。",
            **_ui_i18n(
                "Rich reply gate",
                "On (default): the two takeover modules stay silent while the host "
                "experimental.enable_rich_reply is on.",
            ),
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={
            "hidden": True,
            "disabled": True,
            "label": "配置版本",
            "x-toml-comment": "配置版本，与插件版本同步（自动维护，不要手改）。",
            **_ui_i18n(
                "Configuration version",
                "Config version, kept in sync with the plugin version (maintained automatically, do not edit).",
            ),
        },
    )


# ---------------------------------------------------------------------------
# 配置按"功能板块"排版
# ---------------------------------------------------------------------------
#
# 排版原则（0.11.1 起，请照此维护）：
#
# 1. **不区分"宿主提供的"还是"插件提供的"**：同一个功能的所有开关放进同一个板块；
#    宿主本来就有的参数**直接用宿主的字段名与说明**（宿主 WebUI 上叫什么，这里就叫什么），
#    **不留"（宿主）"之类的标记**；插件扩展的参数跟在后面。
# 2. 板块顺序 = config.toml 顺序：插件基础 → 引用回复 → 引用回复·权重 → 引用回复（私聊）
#    → 引用回复·权重（私聊）→ 错别字 → 错别字·纠正方式权重 → 分段 → 其它功能 → 试验性（最后）。
# 3. **权重类参数一律另开一个"权重"板块，紧跟在它的功能板块之后**（新增权重项也照此办理）。
# 4. **留空 = 跟随宿主**：数值项用空字符串 ``""``（WebUI 里是文本框，填数字即覆盖）；
#    开关项三选一「跟随宿主 / 开启 / 关闭」；文本项空字符串。**不要用负数当哨兵**。
# 5. 首次生成 config.toml 时插件会读宿主现值填进去（见 ``get_default_config``），
#    所以默认看到的就是宿主当前值；想重新跟随宿主就把该项清空。
# 6. 唯一**故意不搬**的是宿主 ``[response_post_process] enable_response_post_process``：
#    它是本插件的生效条件（宿主开着它时宿主自己做后处理，插件必须让位），不是可改参数。

# 「跟随宿主」的取值：TOML 没有 null，空字符串即"未填写"。
FOLLOW_HOST = ""
# 开关型宿主项：三选一，默认跟随宿主。
_HostSwitch = Literal["跟随宿主", "开启", "关闭"]
# 数值型宿主项：空字符串 = 跟随宿主；否则是真值（int / float 分开，避免 512.5 被悄悄取整）。
_HostInt = Union[int, Literal[""]]
_HostFloat = Union[float, Literal[""]]

# ---------------------------------------------------------------------------
# 旧配置迁移（0.10.x / 0.11.0 → 0.11.1 的功能板块排版）
# ---------------------------------------------------------------------------
#
# 排版一变，老的键就"消失"了；如果不管它们，用户改过的权重/开关会在升级时被静默重置成默认值。
# 因此这里显式登记"旧位置 → 新位置"，在 ``normalize_plugin_config`` 里先搬再校验。
#
# 值转换规则：
#   * 旧字段是 bool 而新字段是三态开关 → ``True``/``False`` 变成 ``"开启"``/``"关闭"``；
#   * 旧字段是 ``-1``（或任何负数，曾经当"跟随宿主/自动"用）→ 变成空字符串 ``""``；
#   * 0.11.0 的 ``[host_override]`` 三态 ``host``/``on``/``off`` → ``跟随宿主``/``开启``/``关闭``；
#   * 已彻底删除的字段（``multi_message`` / ``typing`` / ``correction_gate_probability``）直接丢弃。
_MIGRATABLE_LEGACY_PATHS: Dict[str, Tuple[str, str]] = {
    # 0.10.x 的 [post_process]：接管机制散到分段板块、纠正相关散到错别字板块
    "post_process.takeover": ("response_splitter", "takeover"),
    "post_process.yield_to_other_plugins": ("response_splitter", "yield_to_other_plugins"),
    "post_process.max_segments": ("response_splitter", "max_segments"),
    "post_process.wait_timeout_seconds": ("response_splitter", "wait_timeout_seconds"),
    "post_process.correction_mode_enabled": ("chinese_typo", "correction_mode_enabled"),
    "post_process.correction_max_cjk": ("chinese_typo", "correction_max_cjk"),
    "post_process.recall_fallback": ("chinese_typo", "recall_fallback"),
    "post_process.recall_purge": ("chinese_typo", "recall_purge"),
    "post_process.quote_correction": ("chinese_typo", "enable_correction_quote"),
    "post_process.weight_correction_direct": ("chinese_typo_weights", "weight_correction_direct"),
    "post_process.weight_correction_quote": ("chinese_typo_weights", "weight_correction_quote"),
    "post_process.weight_correction_recall": ("chinese_typo_weights", "weight_correction_recall"),
    "post_process.weight_correction_none": ("chinese_typo_weights", "weight_correction_none"),
    "post_process.last_correction_enabled": ("chinese_typo_weights", "last_correction_enabled"),
    "post_process.weight_correction_last": ("chinese_typo_weights", "weight_correction_last"),
    # 引用回复权重拆到权重板块（stale_at_weight 的旧 -1 哨兵 → 留空）
    "quote_reply.weight_direct": ("quote_reply_weights", "weight_direct"),
    "quote_reply.weight_quote": ("quote_reply_weights", "weight_quote"),
    "quote_reply.weight_at": ("quote_reply_weights", "weight_at"),
    "quote_reply.weight_quote_at": ("quote_reply_weights", "weight_quote_at"),
    "quote_reply.stale_at_weight": ("quote_reply_weights", "stale_at_weight"),
    "quote_reply_private.weight_direct": ("quote_reply_private_weights", "weight_direct"),
    "quote_reply_private.weight_quote": ("quote_reply_private_weights", "weight_quote"),
    # 0.11.0 的宿主镜像节：名字变回/并入功能板块
    "response_post_process.typing_speed": ("response_splitter", "typing_speed"),
    "bot.nickname": ("response_splitter", "fallback_nickname"),
    "host_override.typing_speed": ("response_splitter", "typing_speed"),
    "host_override.splitter_enable": ("response_splitter", "enable"),
    "host_override.splitter_max_length": ("response_splitter", "max_length"),
    "host_override.splitter_max_sentence_num": ("response_splitter", "max_sentence_num"),
    "host_override.splitter_max_split_num": ("response_splitter", "max_split_num"),
    "host_override.splitter_kaomoji_protection": ("response_splitter", "enable_kaomoji_protection"),
    "host_override.splitter_overflow_return_all": ("response_splitter", "enable_overflow_return_all"),
    "host_override.typo_enable": ("chinese_typo", "enable"),
    "host_override.typo_error_rate": ("chinese_typo", "error_rate"),
    "host_override.typo_min_freq": ("chinese_typo", "min_freq"),
    "host_override.typo_tone_error_rate": ("chinese_typo", "tone_error_rate"),
    "host_override.typo_word_replace_rate": ("chinese_typo", "word_replace_rate"),
    "host_override.typo_correction_quote_probability": ("chinese_typo", "correction_quote_probability"),
    "host_override.bot_nickname": ("response_splitter", "fallback_nickname"),
    # 0.11.0 已经叫这些名字、节也同名：留着这条便于跨版本比对（值本身照搬）
    "response_splitter.enable": ("response_splitter", "enable"),
}
# 彻底删除、迁移时直接丢弃的旧字段（避免它们触发"未知键"警告）。
_LEGACY_REMOVED_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("post_process", "multi_message"),
    ("post_process", "typing"),
    ("post_process", "correction_gate_probability"),
)

_LEGACY_SWITCH_TEXT = {"host": "跟随宿主", "跟随宿主": "跟随宿主", "on": "开启", "off": "关闭"}
_LEGACY_BOOL_TEXT = {True: "开启", False: "关闭"}


def _legacy_target_kind(section: str, field: str) -> str:
    """目标字段的类别：``switch``（三态开关）/ ``emptyable``（可留空数值）/ ``plain``。

    迁移时必须按目标字段的类型做转换——例如 ``post_process.takeover`` 是**普通布尔**，
    而 ``host_override.splitter_enable`` 是**三态开关**，不能一律把 ``True`` 变成 ``"开启"``。
    类型对象在本函数运行时才解析（模块级别名在类定义处已经就绪）。
    """
    try:
        section_class = BetterPostProcessingConfig.model_fields[section].annotation
        annotation = section_class.model_fields[field].annotation
    except Exception:
        return "plain"
    if annotation == _HostSwitch:
        return "switch"
    if annotation in (_HostInt, _HostFloat):
        return "emptyable"
    return "plain"


def migrate_legacy_config(config_data: Dict[str, Any]) -> Dict[str, Any]:
    """把旧版本的配置键搬到 0.11.1 的功能板块里（幂等；新键已存在时以新键为准）。

    只做"搬运 + 类型对齐"，不做业务判断；值本身（用户改过的权重等）一律保留。
    """
    if not isinstance(config_data, dict):
        return dict(config_data or {})
    migrated: Dict[str, Any] = {section: dict(values) if isinstance(values, dict) else values
                                for section, values in config_data.items()}
    for section, field in _LEGACY_REMOVED_FIELDS:
        values = migrated.get(section)
        if isinstance(values, dict):
            values.pop(field, None)

    for legacy_path, (new_section, new_field) in _MIGRATABLE_LEGACY_PATHS.items():
        old_section, _, old_field = legacy_path.partition(".")
        old_values = migrated.get(old_section)
        if not isinstance(old_values, dict) or old_field not in old_values:
            continue
        value = old_values.pop(old_field)
        target = migrated.setdefault(new_section, {})
        if not isinstance(target, dict):
            continue
        kind = _legacy_target_kind(new_section, new_field)
        if isinstance(value, bool):
            # 只有目标是三态开关时才是 "开启"/"关闭"，普通布尔保持布尔。
            value = _LEGACY_BOOL_TEXT[value] if kind == "switch" else value
        elif isinstance(value, str) and value.strip().lower() in _LEGACY_SWITCH_TEXT and kind == "switch":
            value = _LEGACY_SWITCH_TEXT[value.strip().lower()]
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0 and kind == "emptyable":
            value = FOLLOW_HOST  # 旧的 -1 哨兵 = 留空（跟随宿主 / 自动）
        # **旧键覆盖新键**：宿主 Runner 在调用本方法之前已经用"默认值"补全过缺失的新键，
        # 不覆盖的话用户改过的旧值会被默认值悄悄顶掉。旧键只在这一版存在一次，下一版就没有了。
        target[new_field] = value

    # 搬空了的旧节直接丢掉（[post_process] / [host_override] / [bot] 等）
    for section in list(migrated):
        values = migrated[section]
        if isinstance(values, dict) and not values and section != "plugin":
            migrated.pop(section, None)
    return migrated



class QuoteReplySectionConfig(PluginConfigBase):
    """引用回复（群聊）：什么时候接管、目标过旧怎么处理、引用去重规则。

    权重在紧随其后的 ``[quote_reply_weights]``。
    """

    __ui_label__ = "引用回复"
    __ui_icon__ = "format_quote"
    __ui_order__ = 1
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Quote reply",
            "description": "Reply-style takeover for group chats, with stale-target handling and quote dedup.",
        },
    }

    takeover: bool = Field(
        default=True,
        description=(
            "宿主【启用引用回复】关闭时，由插件为指向目标消息的回复按后面的权重抽取发送方式"
            "（直接回复 / 引用 / @ / 引用＋@）"
        ),
        json_schema_extra={
            "label": "接管引用回复",
            "x-toml-comment": "宿主【启用引用回复】关闭时，由插件按权重板块决定这条回复怎么引用/@。",
            **_ui_i18n(
                "Take over quote replies",
                "When the host chat.reply_style.enable_reply_quote switch is off, pick a reply style by weight.",
            ),
        },
    )
    stale_threshold_messages: int = Field(
        default=3,
        ge=0,
        description=(
            "回复目标之后出现 ≥N 条消息即视为「对话已推进」：抽取回复方式时剔除直接回复，"
            "并把 @ 权重按权重板块里的「推进时 @ 权重」调整（0 = 关闭）"
        ),
        json_schema_extra={
            "label": "对话已推进阈值（条）",
            "x-toml-comment": "被回复消息之后又来了这么多条消息，就算「对话已推进」。0 = 不判定。",
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
            "x-toml-comment": "统计「对话已推进」时是否不数 bot 自己发的消息。",
            **_ui_i18n(
                "Exclude bot messages",
                "Exclude the bot's own messages when counting messages after the target.",
            ),
        },
    )
    stale_age_seconds: float = Field(
        default=1800.0,
        ge=0.0,
        description=(
            "回复目标发出超过 N 秒后，本次回复**直接回复**（不引用也不 @，纯文本发出）"
            "（0 = 关闭；优先级高于上面的「对话已推进」调整）"
        ),
        json_schema_extra={
            "label": "目标超时后强制直接回复（秒）",
            "x-toml-comment": "被回复的消息发出超过这么多秒后，本次回复既不引用也不 @（就是「直接回复」）。0 = 不判定。",
            **_ui_i18n(
                "Force direct reply after (seconds)",
                "When the target message is older than this many seconds, the reply is sent as a "
                "direct reply: no quote and no @. 0 disables.",
            ),
        },
    )
    quote_once_per_target: bool = Field(
        default=True,
        description=(
            "同一条目标消息一旦被引用过（本插件抽取到引用/引用＋@，或被观测到以引用方式发出），"
            "之后对该消息的任何回复都不再引用（只可能直接回复或 @）；抽到直接回复不算，"
            "下一轮仍有机会抽到引用。该规则优先于「目标超时后强制直接回复」判定"
        ),
        json_schema_extra={
            "label": "同一消息只引用一次",
            "x-toml-comment": "同一条目标消息只引用一次，后续回复不再引用它。",
            **_ui_i18n(
                "Quote each message once",
                "Once a target message has been quoted in this chat, later replies never quote again.",
            ),
        },
    )
    style_once_per_target: bool = Field(
        default=True,
        description=(
            "回复方式只取首条：同一条目标消息只有**第一条**回复能抽到 @，之后的回复只在"
            "「直接回复 / 引用回复」里抽——避免出现「前一条什么都没挂、后一条才 @ 人」的错位观感。"
            "另外抽到 @ 也算用过一次指代（计入「同一消息只引用一次」），后续不再引用该消息。"
            "关掉后回到旧行为：每一轮各自独立抽 @"
        ),
        json_schema_extra={
            "label": "回复方式只取首条",
            "x-toml-comment": "对同一条目标消息只在第一条回复里决定 @（避免第二条才 @ 的错位）。",
            **_ui_i18n(
                "Reply style only on first reply",
                "Only the first reply to a target message may use @.",
            ),
        },
    )


class QuoteReplyWeightsSectionConfig(PluginConfigBase):
    """引用回复 · 权重（群聊）：按权重抽取这条回复怎么引用 / @。"""

    __ui_label__ = "引用回复 · 权重"
    __ui_icon__ = "balance"
    __ui_order__ = 2
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Quote reply weights",
            "description": "Chance weights for each group-chat reply style.",
        },
    }

    weight_direct: int = Field(
        default=2,
        ge=0,
        description="权重：直接回复（不引用也不 @）",
        json_schema_extra={
            "label": "权重：直接回复",
            "x-toml-comment": "回复方式权重：直接回复（不引用也不 @）。",
            **_ui_i18n("Weight: direct reply", "Chance weight of replying with neither quote nor @."),
        },
    )
    weight_quote: int = Field(
        default=6,
        ge=0,
        description="权重：引用回复",
        json_schema_extra={
            "label": "权重：引用回复",
            "x-toml-comment": "回复方式权重：引用回复。",
            **_ui_i18n("Weight: quote reply", "Chance weight of quoting the target message."),
        },
    )
    weight_at: int = Field(
        default=1,
        ge=0,
        description="权重：@回复（@ 被回复消息的发送者，仅群聊生效）",
        json_schema_extra={
            "label": "权重：@回复",
            "x-toml-comment": "回复方式权重：@ 被回复的人。",
            **_ui_i18n("Weight: @ reply", "Weight of @-replying the sender of the target message."),
        },
    )
    weight_quote_at: int = Field(
        default=1,
        ge=0,
        description="权重：引用＋@回复（仅群聊生效）",
        json_schema_extra={
            "label": "权重：引用＋@回复",
            "x-toml-comment": "回复方式权重：引用＋@。",
            **_ui_i18n("Weight: quote + @ reply", "Quote the target message and @ its sender."),
        },
    )
    # 注意：本字段**不能**加 ``ge=0``。它是 ``Union[int, Literal[""]]``，"留空"那一支是字符串，
    # Pydantic 会把 ``ge`` 套到 ``""`` 上直接抛 ``TypeError: Unable to apply constraint 'ge'``
    # （默认值不触发校验，所以只有"显式填空"时才会炸——回归测试抓到过一次，别再往回加）。
    stale_at_weight: Union[int, Literal[""]] = Field(
        default="",
        description=(
            "「对话已推进」时 @ 类回复的权重。**留空** = 纯 @ 权重取原值一半（「引用＋@」不动）；"
            "**正数** = 纯 @ 权重取该值（「引用＋@」不动）；**填 0** = 纯 @ 与「引用＋@」**都归 0**，"
            "即真的「不再 @」。刻意不用负数哨兵"
        ),
        json_schema_extra={
            "label": "推进时 @ 权重",
            "x-toml-comment": (
                "对话已推进时 @ 类回复的权重。留空 = 纯 @ 取原值一半；填 0 = 纯 @ 与「引用＋@」都不再 @。"
            ),
            **_ui_i18n(
                "Stale @ weight",
                "The @ reply weight once the conversation has moved on: empty halves the plain-@ weight, "
                "0 disables both plain @ and quote+@.",
            ),
        },
    )


class QuoteReplyPrivateSectionConfig(PluginConfigBase):
    """引用回复（私聊）：与群聊各自独立记录去重与过旧判定；私聊恒不 @。

    权重在紧随其后的 ``[quote_reply_private_weights]``。
    """

    __ui_label__ = "引用回复（私聊）"
    __ui_icon__ = "person"
    __ui_order__ = 3
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Quote reply (private)",
            "description": "Private-chat reply styles, tracked separately from group chats. @ is never used.",
        },
    }

    takeover: bool = Field(
        default=True,
        description="宿主【启用引用回复】关闭时，私聊是否由插件接管回复方式抽取",
        json_schema_extra={
            "label": "接管引用回复",
            "x-toml-comment": "私聊也按权重决定怎么引用（私聊恒不 @）。",
            **_ui_i18n("Take over private replies", "Whether the plugin picks the reply style in private chats."),
        },
    )
    stale_threshold_messages: int = Field(
        default=3,
        ge=0,
        description="回复目标之后出现 ≥N 条消息即剔除直接回复（0 = 关闭）",
        json_schema_extra={
            "label": "对话已推进阈值（条）",
            "x-toml-comment": "被回复消息之后又来了这么多条消息，就算「对话已推进」。0 = 不判定。",
            **_ui_i18n(
                "Stale threshold (messages)",
                "When this many messages appear after the target, direct replies are excluded. 0 disables.",
            ),
        },
    )
    stale_exclude_bot_messages: bool = Field(
        default=False,
        description="统计目标后消息数时排除 bot 自己的消息（默认计入所有消息）",
        json_schema_extra={
            "label": "统计时排除 bot 消息",
            "x-toml-comment": "统计「对话已推进」时是否不数 bot 自己发的消息。",
            **_ui_i18n(
                "Exclude bot messages",
                "Exclude the bot's own messages when counting messages after the target.",
            ),
        },
    )
    stale_age_seconds: float = Field(
        default=1800.0,
        ge=0.0,
        description="回复目标发出超过 N 秒后强制直接回复（不引用也不 @，0 = 关闭）",
        json_schema_extra={
            "label": "目标超时后强制直接回复（秒）",
            "x-toml-comment": "被回复的消息发出超过这么多秒后，本次回复既不引用也不 @（就是「直接回复」）。0 = 不判定。",
            **_ui_i18n(
                "Force direct reply after (seconds)",
                "When the target message is older than this many seconds, the reply is sent as a "
                "direct reply: no quote and no @. 0 disables.",
            ),
        },
    )
    quote_once_per_target: bool = Field(
        default=True,
        description=(
            "同一条目标消息一旦被引用过，之后对该消息的任何回复都不再引用；"
            "抽到直接回复不算，下一轮仍有机会抽到引用。与群聊各自独立记录"
        ),
        json_schema_extra={
            "label": "同一消息只引用一次",
            "x-toml-comment": "同一条目标消息只引用一次，后续回复不再引用它。",
            **_ui_i18n(
                "Quote each message once",
                "Once a target message has been quoted, later replies never quote again.",
            ),
        },
    )


class QuoteReplyPrivateWeightsSectionConfig(PluginConfigBase):
    """引用回复 · 权重（私聊）：私聊只有「直接回复 / 引用回复」两种，没有 @ 权重。"""

    __ui_label__ = "引用回复 · 权重（私聊）"
    __ui_icon__ = "balance"
    __ui_order__ = 4
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Quote reply weights (private)",
            "description": "Chance weights for private-chat reply styles (no @ weights).",
        },
    }

    weight_direct: int = Field(
        default=2,
        ge=0,
        description="权重：直接回复（不引用）",
        json_schema_extra={
            "label": "权重：直接回复",
            "x-toml-comment": "回复方式权重：直接回复（不引用）。",
            **_ui_i18n("Weight: direct reply", "Chance weight of replying without a quote."),
        },
    )
    weight_quote: int = Field(
        default=6,
        ge=0,
        description="权重：引用回复（私聊不提供 @ 权重：QQ 私聊不会出现 @）",
        json_schema_extra={
            "label": "权重：引用回复",
            "x-toml-comment": "回复方式权重：引用回复（私聊恒不 @）。",
            **_ui_i18n(
                "Weight: quote reply",
                "Chance weight of quoting the target message. Private chats have no @ weights.",
            ),
        },
    )


class ChineseTypoSectionConfig(PluginConfigBase):
    """错别字：宿主 ``[chinese_typo]`` 的全部参数 + 插件的纠正行为。

    权重在紧随其后的 ``[chinese_typo_weights]``。

    **错字概率只有一个**：``error_rate``（宿主【单字错字概率】），它决定错字出现的总频率。
    宿主代码里那半句「抽到纠正建议后 50% 发错字、50% 整句替换成正确句」的门控**照抄硬编码 0.5**，
    不对外暴露（见 ``post_processing.plan`` 的 ``visible_probability``）。
    """

    __ui_label__ = "错别字"
    __ui_icon__ = "spellcheck"
    __ui_order__ = 5
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Chinese typo",
            "description": "Typo injection parameters plus the plugin's extra correction behaviour.",
        },
    }

    enable: _HostSwitch = Field(
        default="跟随宿主",
        description=(
            "宿主【启用错别字】：让麦麦偶尔打错字，更像真人聊天。"
            "跟随宿主 / 开启 / 关闭（关闭后不会注入任何错字，也就不会有纠正消息）"
        ),
        json_schema_extra={
            "label": "启用错别字",
            "x-toml-comment": "让麦麦偶尔打错字，更像真人聊天。「跟随宿主」= 用宿主的值。",
            **_ui_i18n("Enable Chinese typo", "Follow host / on / off."),
        },
    )
    error_rate: _HostFloat = Field(
        default=FOLLOW_HOST,
        description=(
            "宿主【单字错字概率】：单个字被替换成错字的概率，**决定错字出现的总频率**。"
            "留空 = 跟随宿主（宿主默认 0.01）。想更容易看到错字就调大（如 0.05~0.1）"
        ),
        json_schema_extra={
            "label": "单字错字概率",
            "x-toml-comment": "单个字被替换成错字的概率（错字出现的总频率）。留空 = 跟随宿主。",
            **_ui_i18n("Char error rate", "Empty = follow host. This is THE typo probability."),
        },
    )
    min_freq: _HostInt = Field(
        default=FOLLOW_HOST,
        description="宿主【最小字频】：只对常见程度达到该值的字尝试制造错字。留空 = 跟随宿主",
        json_schema_extra={
            "label": "最小字频",
            "x-toml-comment": "只对常见程度达到该值的字尝试制造错字。留空 = 跟随宿主。",
            **_ui_i18n("Min freq", "Empty = follow host."),
        },
    )
    tone_error_rate: _HostFloat = Field(
        default=FOLLOW_HOST,
        description="宿主【声调错字概率】：按相近声调制造错字的概率。留空 = 跟随宿主",
        json_schema_extra={
            "label": "声调错字概率",
            "x-toml-comment": "按相近声调制造错字的概率。留空 = 跟随宿主。",
            **_ui_i18n("Tone error rate", "Empty = follow host."),
        },
    )
    word_replace_rate: _HostFloat = Field(
        default=FOLLOW_HOST,
        description="宿主【整词替换概率】：整词被替换成错词的概率。留空 = 跟随宿主",
        json_schema_extra={
            "label": "整词替换概率",
            "x-toml-comment": "整词被替换成错词的概率。留空 = 跟随宿主。",
            **_ui_i18n("Word replace rate", "Empty = follow host."),
        },
    )
    enable_correction_quote: _HostSwitch = Field(
        default="跟随宿主",
        description=(
            "宿主【错别字纠正时引用原消息】：纠正错别字时，是否引用上一条包含错别字的消息。"
            "跟随宿主 / 开启 / 关闭。仅**未开启**下面的「错字纠正方式」时有意义"
        ),
        json_schema_extra={
            "label": "错别字纠正时引用原消息",
            "x-toml-comment": "纠正错别字时，是否引用上一条含错别字的消息。「跟随宿主」= 用宿主的值。",
            **_ui_i18n("Correction quote", "Follow host / on / off."),
        },
    )
    correction_quote_probability: _HostFloat = Field(
        default=FOLLOW_HOST,
        description=(
            "宿主【错别字纠正引用概率】：生成纠正消息时，引用上一条错别字消息的概率。"
            "留空 = 跟随宿主。仅**未开启**下面的「错字纠正方式」时有意义（那之后引用由权重决定）"
        ),
        json_schema_extra={
            "label": "错别字纠正引用概率",
            "x-toml-comment": "生成纠正消息时引用上一条错别字消息的概率。留空 = 跟随宿主。",
            **_ui_i18n("Correction quote probability", "Empty = follow host."),
        },
    )
    correction_mode_enabled: bool = Field(
        default=False,
        description=(
            "错字纠正方式：宿主原生只会「抽到纠正建议就 50% 追加一条更正消息、50% 整句换成正确句」；"
            "开启本项后改为对每处错字按权重板块里的权重抽取纠正方式"
            "（直接发送 / 引用 / 撤回重发 / 不纠正，可另加最后纠正）。"
            "开启后宿主那半句 50% 门控仍照旧生效"
        ),
        json_schema_extra={
            "label": "错字纠正方式",
            "x-toml-comment": "开启后用权重板块自己决定怎么纠正错字（宿主原生只有追加更正消息）。",
            **_ui_i18n(
                "Correction modes",
                "Off (default): host behaviour. On: pick a correction mode per typo by weight.",
            ),
        },
    )
    correction_max_cjk: int = Field(
        default=20,
        ge=0,
        description=(
            "超限不纠错：某段汉字数**超过**该值时，该处错字即使出现也不纠正"
            "（错字留着、不消耗纠正方式抽取）；0 = 不限制。用来避免长消息乱补纠正"
        ),
        json_schema_extra={
            "label": "超限不纠错",
            "x-toml-comment": "某段汉字数超过该值时不再纠正（错字留着）。0 = 不限制。",
            **_ui_i18n("Skip correction when too long", "0 = no limit."),
        },
    )
    recall_fallback: Literal["quote", "direct", "none"] = Field(
        default="quote",
        description=(
            "撤回失败（适配器不支持 / 超时）时的兜底：quote = 改成引用发送 / direct = 直接发送 / "
            "none = 放弃这条纠正"
        ),
        json_schema_extra={
            "label": "撤回失败兜底",
            "x-toml-comment": "撤回失败时怎么办：quote 引用发送 / direct 直接发送 / none 放弃这条纠正。",
            **_ui_i18n("Recall fallback", "quote (default) / direct / none."),
        },
    )
    recall_purge: bool = Field(
        default=True,
        description=(
            "撤回后清理原消息：删掉宿主消息库里那条记录，并且不把「即将被撤回」的自发分段写进"
            "bot 自己的对话历史（撤回后重发只留修正句）。"
            "注意：**宿主发出的第 1 段**由宿主自己写历史，插件拦不住——它的库记录会被删掉，"
            "但内存上下文里那条要等重启/上下文重建后才消失"
        ),
        json_schema_extra={
            "label": "撤回后清理原消息",
            "x-toml-comment": "撤回后把原消息从消息库删掉，并让被撤回的自发分段不进 bot 自己的历史。",
            **_ui_i18n(
                "Purge recalled message",
                "Delete the recalled message from the store and keep it out of the bot's own history.",
            ),
        },
    )


class ChineseTypoWeightsSectionConfig(PluginConfigBase):
    """错别字 · 纠正方式权重：开启「错字纠正方式」后，每处错字按这里的权重抽取怎么纠正。"""

    __ui_label__ = "错别字 · 纠正方式权重"
    __ui_icon__ = "balance"
    __ui_order__ = 6
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Correction mode weights",
            "description": "Chance weights for each typo correction mode (only used when correction modes are on).",
        },
    }

    weight_correction_direct: int = Field(
        default=1,
        ge=0,
        description="权重：直接发送纠正内容（不引用带错字的那条消息）",
        json_schema_extra={
            "label": "权重 · 直接发送",
            "x-toml-comment": "纠正方式权重：直接发送纠正内容（不引用）。",
            **_ui_i18n("Weight: direct", "Send the correction without quoting."),
        },
    )
    weight_correction_quote: int = Field(
        default=4,
        ge=0,
        description="权重：引用带错字的那条消息发送纠正内容（宿主原生观感）",
        json_schema_extra={
            "label": "权重 · 引用纠正",
            "x-toml-comment": "纠正方式权重：引用带错字的那条消息发送纠正内容（宿主原生观感）。",
            **_ui_i18n("Weight: quote", "Send the correction quoting the typo message."),
        },
    )
    weight_correction_recall: int = Field(
        default=1,
        ge=0,
        description="权重：撤回带错字的那条消息，然后重发修正后的整句",
        json_schema_extra={
            "label": "权重 · 撤回重发",
            "x-toml-comment": "纠正方式权重：撤回带错字的消息并重发修正句。",
            **_ui_i18n("Weight: recall", "Recall the typo message and resend the corrected sentence."),
        },
    )
    weight_correction_none: int = Field(
        default=1,
        ge=0,
        description="权重：不纠正（错字留在消息里）",
        json_schema_extra={
            "label": "权重 · 不纠正",
            "x-toml-comment": "纠正方式权重：不纠正，错字留在消息里。",
            **_ui_i18n("Weight: none", "Leave the typo as is."),
        },
    )
    last_correction_enabled: bool = Field(
        default=False,
        description=(
            "允许最后纠正：**只决定「最后纠正」是否加入权重池**（加入后由下面那个权重决定占比）。"
            "抽到它时，纠正会等本轮所有分段都发完才发出，并且强制引用对应那条错字消息"
        ),
        json_schema_extra={
            "label": "允许最后纠正",
            "x-toml-comment": "只决定「最后纠正」是否加入权重池（它等分段发完才纠正，且强制引用）。",
            **_ui_i18n(
                "Allow deferred correction",
                "Only decides whether the deferred ('last') correction joins the weight pool.",
            ),
        },
    )
    weight_correction_last: int = Field(
        default=1,
        ge=0,
        description="权重：最后纠正（仅「允许最后纠正」开启时参与抽取）",
        json_schema_extra={
            "label": "权重 · 最后纠正",
            "x-toml-comment": "「最后纠正」在权重池里的占比（仅上面那项开启时参与）。",
            **_ui_i18n("Weight: last", "Only counted when deferred correction is allowed."),
        },
    )


class ResponseSplitterSectionConfig(PluginConfigBase):
    """分段：宿主 ``[response_splitter]`` 的全部参数 + 分段打字速度 + 接管机制。

    宿主的分段本来就是"每段一条消息 + 按打字速度模拟打字"，插件接管后照此复刻，
    所以这里没有"是否多条发送"这类开关——要关分段就关 ``enable``，要关打字就把 ``typing_speed`` 设 0。
    """

    __ui_label__ = "分段"
    __ui_icon__ = "content_cut"
    __ui_order__ = 7
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Segmentation",
            "description": "Splitter parameters, typing speed and the post-process takeover switches.",
        },
    }

    takeover: bool = Field(
        default=True,
        description=(
            "宿主【启用回复后处理】关闭时，由插件以与宿主一致的原逻辑接管错字注入、分段与分段打字"
            "（错别字参数见上面的错别字板块）。宿主开着该开关时插件不介入"
        ),
        json_schema_extra={
            "label": "接管后处理",
            "x-toml-comment": "宿主【启用回复后处理】关闭时，由插件接管错字注入 / 分段 / 分段打字。",
            **_ui_i18n(
                "Take over post-process",
                "Effective only when response_post_process.enable_response_post_process is off.",
            ),
        },
    )
    enable: _HostSwitch = Field(
        default="跟随宿主",
        description=(
            "宿主【启用回复分割】：把过长回复拆成多条发送。"
            "跟随宿主 = 用宿主的值；开启 / 关闭 = 只用插件的判定（宿主关着也能开）"
        ),
        json_schema_extra={
            "label": "启用回复分割",
            "x-toml-comment": "把过长回复拆成多条发送。「跟随宿主」= 用宿主的值。",
            **_ui_i18n("Enable splitter", "Follow host / on / off."),
        },
    )
    max_length: _HostInt = Field(
        default=FOLLOW_HOST,
        description="宿主【单条最大长度】：单条回复允许的最大长度（超过会回退默认回复）。留空 = 跟随宿主",
        json_schema_extra={
            "label": "单条最大长度",
            "x-toml-comment": "单条回复允许的最大长度。留空 = 跟随宿主。",
            **_ui_i18n("Max length", "Empty = follow host."),
        },
    )
    max_sentence_num: _HostInt = Field(
        default=FOLLOW_HOST,
        description="宿主【单条最大句数】：单条回复最多包含多少个句子。留空 = 跟随宿主",
        json_schema_extra={
            "label": "单条最大句数",
            "x-toml-comment": "单条回复最多包含多少个句子。留空 = 跟随宿主。",
            **_ui_i18n("Max sentence num", "Empty = follow host."),
        },
    )
    max_split_num: _HostInt = Field(
        default=FOLLOW_HOST,
        description="宿主【最多分割条数】：一次回复最多拆成几条消息。留空 = 跟随宿主",
        json_schema_extra={
            "label": "最多分割条数",
            "x-toml-comment": "一次回复最多拆成几条消息。留空 = 跟随宿主。",
            **_ui_i18n("Max split num", "Empty = follow host."),
        },
    )
    enable_kaomoji_protection: _HostSwitch = Field(
        default="跟随宿主",
        description="宿主【保护颜文字】：尽量避免把颜文字从中间拆开。跟随宿主 / 开启 / 关闭",
        json_schema_extra={
            "label": "保护颜文字",
            "x-toml-comment": "尽量避免把颜文字从中间拆开。「跟随宿主」= 用宿主的值。",
            **_ui_i18n("Kaomoji protection", "Follow host / on / off."),
        },
    )
    enable_overflow_return_all: _HostSwitch = Field(
        default="跟随宿主",
        description=(
            "宿主【超限保留全文】：句子太多时是否直接保留完整回复，不再强行截断。"
            "跟随宿主 / 开启 / 关闭"
        ),
        json_schema_extra={
            "label": "超限保留全文",
            "x-toml-comment": "句子太多时是否直接保留完整回复，不再强行截断。「跟随宿主」= 用宿主的值。",
            **_ui_i18n("Overflow return all", "Follow host / on / off."),
        },
    )
    typing_speed: _HostFloat = Field(
        default=FOLLOW_HOST,
        description=(
            "宿主【打字速度】：模拟打字等待时间；0 最快，1 默认，2 更慢。"
            "留空 = 跟随宿主（交给宿主按它自己的速度等待）；填了值由插件按同一公式自己等待"
            "（中文 0.3s/字、其它 0.15s/字符，乘速度系数；整条只有 1 个汉字时宿主固定 1.2s）。"
            "只作用于插件补发的分段"
        ),
        json_schema_extra={
            "label": "打字速度",
            "x-toml-comment": "模拟打字等待时间；0 最快，1 默认，2 更慢。留空 = 跟随宿主。",
            **_ui_i18n("Typing speed", "Empty = follow host response_post_process.typing_speed."),
        },
    )
    fallback_nickname: str = Field(
        default=FOLLOW_HOST,
        description=(
            "宿主【bot · 昵称】：麦麦显示和自称时使用的名字。留空 = 跟随宿主。"
            "插件只用于复刻「整条回复过长 / 句子太多时回退默认回复」的文本（「<昵称>不知道哦」）"
        ),
        json_schema_extra={
            "label": "兜底回复的自称",
            "x-toml-comment": "复刻兜底默认回复用的自称（宿主 bot.nickname）。留空 = 跟随宿主。",
            **_ui_i18n("Fallback nickname", "Empty = follow host bot.nickname."),
        },
    )
    yield_to_other_plugins: bool = Field(
        default=True,
        description=(
            "让位给其它插件：同一轮回复里，若别的插件已经认领文本后处理"
            "（把 skip_post_process 置为 True，例如智能分段插件命中它的预分段缓存），"
            "本插件不改写正文、不登记补发，分段交给对方；@/引用、文本规则、表情包等功能照常。"
            "对方未认领（未安装 / 未命中 / 失败）时本插件照常接管作为兜底。"
            "关掉本项表示本插件无条件优先（会压制对方的预分段）"
        ),
        json_schema_extra={
            "label": "让位给其它插件",
            "x-toml-comment": "别的插件已认领这轮文本后处理时把分段让给它（@/引用等仍由本插件做）。",
            **_ui_i18n(
                "Yield to other plugins",
                "On (default): if another plugin already claimed post-processing, keep hands off the text.",
            ),
        },
    )
    max_segments: int = Field(
        default=0,
        description=(
            "分段条数上限：超过该值则退化为换行拼接成一条消息（防刷屏）；"
            "0 = 不额外限制，完全沿用宿主【最多分割条数】的分段结果"
        ),
        json_schema_extra={
            "label": "分段条数上限",
            "x-toml-comment": "分段条数上限，超过就并成一条消息。0 = 不限制（只用宿主的条数）。",
            **_ui_i18n("Max segments", "0 = follow the host max_split_num."),
        },
    )
    wait_timeout_seconds: int = Field(
        default=30,
        description=(
            "planner 请求与新入站消息等待本轮补发完成的秒数（超时即放行，不阻塞宿主；0 = 不等待）"
        ),
        json_schema_extra={
            "label": "补发等待超时",
            "x-toml-comment": "planner / 新消息等待本轮分段发完的秒数，超时放行。0 = 不等待。",
            **_ui_i18n("Follow-up wait timeout", "Planner/inbound wait budget in seconds."),
        },
    )


class EmojiAfterReplySectionConfig(PluginConfigBase):
    """planner 回复后概率补发表情包配置。"""

    __ui_label__ = "回复后表情包"
    __ui_icon__ = "mood"
    __ui_order__ = 8
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
            "x-toml-comment": "planner 这轮没发表情包时，按概率补发一个。",
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
            "x-toml-comment": "补发概率（0~1）。",
            **_ui_i18n("Probability", "Chance (0~1) of sending an emoji after a qualifying reply."),
        },
    )
    emotion: str = Field(
        default="",
        description="抽取表情包使用的情绪标签（留空则随机抽取一张）",
        json_schema_extra={
            "label": "指定情绪标签",
            "x-toml-comment": "补发时优先挑这个情绪标签的表情包；留空表示随机。",
            **_ui_i18n("Emotion tag", "Emoji description tag to fetch; empty means random."),
        },
    )
    quiet_seconds: float = Field(
        default=4.0,
        ge=0.5,
        description="回复分段发送完成后等待多少秒没有新出站消息，才认定本轮结束（**最少 0.5 秒**，默认 4）",
        json_schema_extra={
            "label": "静默判定时长（秒）",
            "x-toml-comment": "群聊安静这么久才考虑补发（避免打断热火朝天的聊天）。",
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
            "x-toml-comment": "判定「同一轮回复」的时间窗。",
            **_ui_i18n(
                "Round window (seconds)",
                "Discard the round record if no outgoing message arrives within this many seconds.",
            ),
        },
    )
    first_send_timeout_seconds: float = Field(
        default=20.0,
        ge=0.0,
        description=(
            "回复轮建立后等待本轮首条出站消息的秒数：超时说明这轮回复没真的发出"
            "（发送失败或被其它插件中止），直接丢弃该轮，避免之后其它插件的消息触发补发（0=关闭）"
        ),
        json_schema_extra={
            "label": "首条出站等待（秒）",
            "x-toml-comment": "等首条消息出站的最长时间。",
            **_ui_i18n(
                "First send timeout (seconds)",
                "Drop the round if no outgoing message arrives within this many seconds; the reply was never sent, so later sends from other plugins must not inherit it. 0 disables.",
            ),
        },
    )
    planner_emoji_tools: List[str] = Field(
        default_factory=lambda: ["send_emoji", "emoji_like", "emoji_like_list"],
        description=(
            "planner 计划调用这些工具（``maisaka.planner.after_response`` 的 output_items 工具名）时，"
            "视为本轮已由 planner 自行处理表情，本插件不再补发；留空则关闭该判定"
        ),
        json_schema_extra={
            "label": "planner 表情工具名",
            "x-toml-comment": "planner 调用了这些工具就认为它自己发过表情包，本轮不补发。",
            "rows": 3,
            **_ui_i18n(
                "Planner emoji tool names",
                "When the planner plans to call any of these tools, the round counts as already carrying an emoji and no extra one is sent. Empty disables the check.",
            ),
        },
    )
    planned_emoji_ttl_seconds: float = Field(
        default=90.0,
        ge=5.0,
        description="planner 表情意图标记的有效期（秒），覆盖 planner 工具执行耗时（**最少 5 秒**，默认 90）",
        json_schema_extra={
            "label": "planner 表情标记有效期（秒）",
            "x-toml-comment": "上面那个标记的有效期。",
            **_ui_i18n(
                "Planned-emoji TTL (seconds)",
                "How long the 'planner handles the emoji itself' mark stays valid; must cover the tool execution time.",
            ),
        },
    )


class EmojiFollowSectionConfig(PluginConfigBase):
    """群友连续表情包跟发配置。"""

    __ui_label__ = "表情包跟风"
    __ui_icon__ = "emoji_emotions"
    __ui_order__ = 9
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
            "x-toml-comment": "群友连着发表情包时跟着发一个。",
            **_ui_i18n("Enabled", "Follow up with an emoji on consecutive group stickers."),
        },
    )
    mode: Literal["same", "specified", "random"] = Field(
        default="same",
        description="跟发方式：same=与最后一个表情包相同情绪，specified=使用下方指定情绪，random=纯随机",
        json_schema_extra={
            "label": "跟发方式",
            "x-toml-comment": "跟发方式：same 同款 / specified 指定情绪 / random 随机。",
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
            "x-toml-comment": "连续多少条表情包后触发。",
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
            "x-toml-comment": "触发后的跟发概率（0~1）。",
            **_ui_i18n("Probability", "Chance (0~1) of actually following once the threshold is reached."),
        },
    )
    emotion: str = Field(
        default="",
        description="mode=specified 时抽取表情包使用的情绪标签",
        json_schema_extra={
            "label": "指定情绪标签",
            "x-toml-comment": "mode=specified 时用哪个情绪标签。",
            **_ui_i18n("Emotion tag", "Used when mode = specified."),
        },
    )
    ignore_self_messages: bool = Field(
        default=True,
        description=(
            "不计入 bot 自身账号发出的消息（及插件自注入的合成记录）：宿主没有 self 过滤，"
            "适配器关闭“忽略自身消息”时 bot 自己的表情会回环成入站表情并被当成群友连击"
        ),
        json_schema_extra={
            "label": "忽略自身消息",
            "x-toml-comment": "统计连击时不数 bot 自己的表情包。",
            **_ui_i18n(
                "Ignore self messages",
                "Do not count messages sent by the bot's own account (or plugin-injected records). The host has no self filter, so a disabled adapter filter would let the bot follow its own stickers.",
            ),
        },
    )
    streak_window_seconds: float = Field(
        default=120.0,
        ge=10.0,
        description="连击保鲜时长（秒）：距上一条入站表情超过该时长即重新计数（**最少 10 秒**，默认 120）",
        json_schema_extra={
            "label": "连击保鲜（秒）",
            "x-toml-comment": "连击保鲜时间，超过就重新计数。",
            **_ui_i18n(
                "Streak window (seconds)",
                "Reset the sticker streak when the gap since the last inbound sticker exceeds this.",
            ),
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
    __ui_order__ = 11
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
            "x-toml-comment": "同一会话里两次表情包之间的最小间隔（秒）。",
            **_ui_i18n(
                "Cooldown (seconds)",
                "After any emoji is stored in a chat (from any source), the plugin will not proactively send emojis for this long.",
            ),
        },
    )
    count_unstored: bool = Field(
        default=True,
        description=(
            "把 storage_message=False（未入库）的出站表情也计入冷却：其它插件常用该参数发消息，"
            "不计入时本插件可能紧接着在别人的表情后面再补/跟一张"
        ),
        json_schema_extra={
            "label": "未入库表情也计入冷却",
            "x-toml-comment": "没能入库的表情包是否也计入冷却。",
            **_ui_i18n(
                "Count unstored emojis",
                "Also start the cooldown for outgoing emojis sent with storage_message=False (other plugins commonly use it), so the plugin does not piggyback right after them.",
            ),
        },
    )


class TextRulesSectionConfig(PluginConfigBase):
    """出站文本替换/覆盖规则配置。"""

    __ui_label__ = "文本替换规则"
    __ui_icon__ = "find_replace"
    __ui_order__ = 10
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
            "x-toml-comment": "出站文本规则：replace=a=>b 词替换，cover=前缀=>整条覆盖，# 开头为注释。",
            "rows": 6,
            **_ui_i18n(
                "Rules",
                'One item per rule: "word"replace"new" replaces occurrences, "word"cover"text" overwrites the whole text when it contains the word. Malformed items are ignored.',
            ),
        },
    )
    apply_scope: Literal["reply_flow", "all_outbound"] = Field(
        default="reply_flow",
        description=(
            "规则作用范围：reply_flow=只作用于 planner 拉起的回复流程（本轮回复自己发出的消息，"
            "含分段与错别字更正段）；all_outbound=作用于 bot 的全部出站文本（含其它插件直接发出的消息）"
        ),
        json_schema_extra={
            "label": "作用范围",
            "x-toml-comment": "作用范围：reply_flow 只处理本轮回复（含插件补发的分段），all_outbound 处理所有出站文本。",
            **_ui_i18n(
                "Apply scope",
                "reply_flow = only the planner-pulled reply flow (this round's own messages, segments and typo-correction segment included); all_outbound = every outgoing text, including messages other plugins send directly.",
            ),
        },
    )


class EmojiMeaningSectionConfig(PluginConfigBase):
    """表情包含义库与上下文注入配置（试验性功能，默认关闭）。

    试验性原因：依赖宿主视觉模型任务的输出质量、token 消耗随表情库规模增长，
    且以宿主描述标签为关联键存在固有的合并折衷（见 emoji_meanings.py）。
    """

    __ui_label__ = "表情包含义库（试验性）"
    __ui_icon__ = "image_search"
    __ui_order__ = 12
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {
            "title": "Emoji Meaning Library (Experimental)",
            "description": "Backfill precise emoji meanings and inject them into the replyer context. Experimental; disabled by default.",
        },
    }

    enabled: bool = Field(
        default=False,
        description="是否启用表情包含义库（试验性功能，默认关闭；周期补录 + 新表情包即时补录）",
        json_schema_extra={
            "label": "启用含义库（试验性）",
            "x-toml-comment": "启用表情包含义库（试验性）：为表情包生成并维护准确含义。",
            **_ui_i18n(
                "Enabled (experimental)",
                "Periodically backfill meanings for existing emojis and instantly for newly registered ones. Experimental: disabled by default.",
            ),
        },
    )
    inject_enabled: bool = Field(
        default=True,
        description="是否在回复前把上下文中出现的表情包含义注入 replyer 提示词",
        json_schema_extra={
            "label": "启用上下文注入",
            "x-toml-comment": "把表情包含义注入 replyer 上下文。",
            **_ui_i18n(
                "Context injection",
                "Inject meanings of emojis found in the replied-to message and recent messages into the replyer prompt.",
            ),
        },
    )
    inject_request_types: List[str] = Field(
        default_factory=lambda: ["maisaka.replyer"],
        description=(
            "只对这些 replyer 请求类型注入含义（对应 maisaka.replyer.before_request 的 request_type）："
            "默认只注入 planner 拉起的正常回复流程；插件自己生成的回复（如 generator_api / plugin.*）不注入。"
            "留空表示不过滤，写 * 表示全部注入"
        ),
        json_schema_extra={
            "label": "注入的请求类型",
            "x-toml-comment": "注入到哪些请求类型（如 replyer）。",
            "rows": 3,
            **_ui_i18n(
                "Inject for request types",
                "Only inject for these replyer request types (maisaka.replyer = the normal planner-pulled reply flow). Replies generated by plugins themselves (generator_api / plugin.*) are left alone. Empty disables filtering; * means all.",
            ),
        },
    )
    model_task: str = Field(
        default="vlm",
        description="生成含义使用的模型任务名（对应 model_config 的 model_task_config 键，如 vlm）",
        json_schema_extra={
            "label": "模型任务名",
            "x-toml-comment": "用哪个模型任务生成含义。",
            **_ui_i18n("Model task", "Model task name for meaning generation (see model_task_config, e.g. vlm)."),
        },
    )
    generation_prompt: str = Field(
        default="",
        description="生成含义使用的提示词（留空使用内置提示词）",
        json_schema_extra={
            "label": "生成提示词",
            "x-toml-comment": "生成含义用的提示词。",
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
            "x-toml-comment": "生成含义时的 max_tokens。",
            **_ui_i18n("Max tokens", "max_tokens for each meaning generation call."),
        },
    )
    scan_interval_seconds: float = Field(
        default=1800.0,
        ge=60.0,
        description="周期补录通道：扫描宿主表情库间隔（秒）",
        json_schema_extra={
            "label": "库扫描间隔（秒）",
            "x-toml-comment": "多久扫一遍库、把缺含义的表情包入队。",
            **_ui_i18n("Scan interval (seconds)", "How often to scan the host emoji library for unrecorded emojis."),
        },
    )
    worker_interval_seconds: float = Field(
        default=30.0,
        ge=5.0,
        description="含义生成工作循环间隔（秒），每轮最多生成 batch_size 条",
        json_schema_extra={
            "label": "生成循环间隔（秒）",
            "x-toml-comment": "生成循环的间隔。",
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
            "x-toml-comment": "每轮生成几条含义。",
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
            "x-toml-comment": "单条含义生成失败后的重试上限。",
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
            "x-toml-comment": "单条含义的最大字数。",
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
            "x-toml-comment": "单次往上下文里注入多少条含义。",
            **_ui_i18n("Injection limit", "Max emoji meanings injected per reply."),
        },
    )


class BetterPostProcessingConfig(PluginConfigBase):
    """插件完整配置。

    **节顺序 = config.toml 里的顺序**（``model_dump`` 按字段定义顺序导出），按**功能板块**排：

    1. ``[plugin]`` 插件自身开关与配置版本；
    2. ``[quote_reply]`` 引用回复 → ``[quote_reply_weights]`` 它的权重 →
       ``[quote_reply_private]`` 引用回复（私聊）→ ``[quote_reply_private_weights]`` 它的权重；
    3. ``[chinese_typo]`` 错别字 → ``[chinese_typo_weights]`` 它的纠正方式权重；
    4. ``[response_splitter]`` 分段（分割参数 + 打字速度 + 接管机制）；
    5. 其它功能节（表情包 / 文本规则）；
    6. **试验性功能一律放最后**（当前是 ``[emoji_meaning]`` 表情包含义库）——新增试验性配置节
       时请加在本类末尾，别插到前面，否则用户升级时看到的分节顺序会乱。

    不区分"宿主提供的"还是"插件提供的"：同一个功能的开关都放同一个板块，宿主本来就有的参数
    直接用宿主的字段名与说明；**权重类参数一律另开一个权重板块、紧跟在它的功能板块之后**。
    """

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    # ---- 引用回复（功能 → 权重）----
    quote_reply: QuoteReplySectionConfig = Field(default_factory=QuoteReplySectionConfig)
    quote_reply_weights: QuoteReplyWeightsSectionConfig = Field(
        default_factory=QuoteReplyWeightsSectionConfig
    )
    quote_reply_private: QuoteReplyPrivateSectionConfig = Field(
        default_factory=QuoteReplyPrivateSectionConfig
    )
    quote_reply_private_weights: QuoteReplyPrivateWeightsSectionConfig = Field(
        default_factory=QuoteReplyPrivateWeightsSectionConfig
    )
    # ---- 错别字（功能 → 权重）----
    chinese_typo: ChineseTypoSectionConfig = Field(default_factory=ChineseTypoSectionConfig)
    chinese_typo_weights: ChineseTypoWeightsSectionConfig = Field(
        default_factory=ChineseTypoWeightsSectionConfig
    )
    # ---- 分段 ----
    response_splitter: ResponseSplitterSectionConfig = Field(default_factory=ResponseSplitterSectionConfig)
    # ---- 其它功能 ----
    emoji_after_reply: EmojiAfterReplySectionConfig = Field(default_factory=EmojiAfterReplySectionConfig)
    emoji_follow: EmojiFollowSectionConfig = Field(default_factory=EmojiFollowSectionConfig)
    text_rules: TextRulesSectionConfig = Field(default_factory=TextRulesSectionConfig)
    emoji_cooldown: EmojiCooldownSectionConfig = Field(default_factory=EmojiCooldownSectionConfig)
    # ---- 试验性功能：**必须在最后** ----
    emoji_meaning: EmojiMeaningSectionConfig = Field(default_factory=EmojiMeaningSectionConfig)


class BetterPostProcessingPlugin(QuoteTakeoverMixin, PostProcessTakeoverMixin, MaiBotPlugin):
    """回复后处理接管、回复方式抽取、表情包互动与出站文本规则增强插件。

    两个"接管"模块（引用回复接管 / 后处理接管）的实现分别在
    ``modules/quote_takeover.py`` 与 ``modules/post_process_takeover.py``，以 mixin
    形式组合进来（SDK 用 ``dir(instance)`` 收集组件，继承的 HookHandler 同样会被注册）；
    各自的前置条件（需要宿主关闭哪些能力）由 ``modules/requirements.py`` 统一判定。
    本类保留共享基础设施：回复轮记录、文本规则、表情包相关功能。
    """

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
        self._init_quote_takeover()
        # 回复轮跟踪：session_id -> 轮状态（含自增轮 id、目标消息 id、是否已抽取回复方式）。
        self._reply_rounds: Dict[str, Dict[str, Any]] = {}
        self._reply_round_seq = 0
        # 出站消息活跃令牌（防抖）：session_id -> 递增序号。
        self._outbound_tokens: Dict[str, int] = {}
        # 最近 bot 发送表情包的时间（任意来源，供"本轮已带表情"判定）：session_id -> 单调时钟。
        self._chat_emoji_last: Dict[str, float] = {}
        # planner 本轮已计划自行发表情/贴表情的标记截止时刻：session_id -> 单调时钟上限。
        self._planned_emoji_until: Dict[str, float] = {}
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
        self._meaning_misses: Dict[str, int] = {}
        self._meaning_sample_k = _MEANING_SAMPLE_K_START
        # 多次生成失败后放弃的描述标签；周期扫描不再入队，注册钩子可将其复活。
        self._meaning_abandoned: set[str] = set()
        # 会话近期表情包描述缓存（供 replyer 注入）：session_id -> deque[(单调时钟, 描述)]。
        self._session_emoji_descs: Dict[str, Deque[Tuple[float, str]]] = {}
        # 被回复消息中的表情包描述缓存：message_id -> (单调时钟, 描述列表)。
        self._target_emoji_cache: Dict[str, Tuple[float, List[str]]] = {}
        # 后台任务集合（on_unload 时统一取消）。
        self._tasks: set[asyncio.Task] = set()
        self._init_post_process_takeover()

    # ------------------------------------------------------------------
    # 配置生成（宿主现值作为默认值 + 给 config.toml 补行尾注释）
    # ------------------------------------------------------------------

    def _log_quiet(self, level: str, message: str, *args: Any) -> None:
        """在"ctx 可能还没注入"的时机安全打日志（``get_default_config`` / 配置注释写入）。

        这两个时机在 Runner 里的顺序是"注入 ctx → 要默认配置"，但测试或异常路径下可能没有 ctx；
        直接 ``self.ctx`` 会抛 ``RuntimeError``，反而把真正要忽略的小错误放大成崩溃。
        """
        ctx = getattr(self, "_ctx", None)
        if ctx is None:
            return
        logger = getattr(ctx, "logger", None)
        if logger is None:
            return
        try:
            getattr(logger, level, logger.debug)(message, *args)
        except Exception:
            pass

    def normalize_plugin_config(self, config_data: Any) -> Any:
        """先做旧配置迁移（见 ``migrate_legacy_config``），再走 SDK 的校验与补齐。

        排版变更会让老键"消失"，不迁移的话用户改过的权重/开关会在升级时被静默重置。
        注意：宿主 Runner 在调用本方法之前会先 ``rebuild_plugin_config_data``（以默认值补齐、
        丢弃新结构里没有的旧键），所以**主要靠 ``get_default_config`` 里把旧值带进默认值**
        （见 ``_legacy_config_values``）；这里是第二道保险，负责 WebUI 直接提交旧格式数据的场景。
        """
        return super().normalize_plugin_config(migrate_legacy_config(dict(config_data or {})))

    def _legacy_config_values(self) -> Dict[str, Any]:
        """读插件自己的 ``config.toml`` 并迁移成新排版，返回"旧文件里显式写过的值"。

        用途：宿主在版本升级时会以**最新默认值**为骨架重建配置（只保留新结构里还存在的旧键值），
        老的 ``[post_process]`` / ``[host_override]`` 会在那一步被丢掉。把迁移后的旧值合并进
        ``get_default_config`` 的返回值，这些值就会作为"默认值"被保留下来。
        """
        path = Path(__file__).resolve().parent / "config.toml"
        if not path.is_file():
            return {}
        try:
            import tomllib

            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        except Exception as exc:  # 读不动就当没有（不影响插件运行）
            self._log_quiet("debug", "读旧配置用于迁移失败（忽略）: %s", exc)
            return {}
        migrated = migrate_legacy_config(raw)
        plugin_section = migrated.get("plugin")
        if isinstance(plugin_section, dict):
            # 版本号必须用新版本，否则宿主会以为仍然是老版本配置。
            plugin_section.pop("config_version", None)
            if not plugin_section:
                migrated.pop("plugin", None)
        return migrated

    def get_default_config(self) -> Dict[str, Any]:
        """生成默认配置：宿主现值 + 迁移后的旧配置值。

        宿主配置只有宿主自己知道（而且它可能和宿主默认值不同），所以这里直接读
        ``<root>/config/bot_config.toml``（同步、只读；此时还没有事件循环，走不了
        ``ctx.config.get``）。读不到就退回模型默认值。用户把某项清空即表示"跟随宿主"。
        """
        defaults = super().get_default_config()
        host_values = read_host_config_values()
        if host_values:
            for _param, section, field, _cache_key, kind, host_section, host_field in _HOST_MIRROR_FIELDS:
                value = (host_values.get(host_section) or {}).get(host_field)
                if value is None:
                    continue
                if kind == "switch":
                    defaults.setdefault(section, {})[field] = "开启" if bool(value) else "关闭"
                else:
                    defaults.setdefault(section, {})[field] = value
            for section, field, _kind, host_section, host_field in _HOST_MIRROR_EXTRA_FIELDS:
                value = (host_values.get(host_section) or {}).get(host_field)
                if value is not None:
                    defaults.setdefault(section, {})[field] = value
        # 旧配置里显式写过的值优先于"宿主现值"（用户自己填的当然最优先）。
        for section, values in self._legacy_config_values().items():
            if not isinstance(values, dict):
                continue
            target = defaults.setdefault(section, {})
            if isinstance(target, dict):
                target.update(values)
        return defaults

    def get_webui_config_schema(self, **kwargs: Any) -> Dict[str, Any]:
        """WebUI 配置 Schema：把每项的「简短注释」也作为 ``hint`` 交给界面。

        宿主的插件配置页**只渲染字段的 ``hint``**
        （``dashboard/src/routes/plugin-config.tsx``：``resolveLocalizedText(field.hint, …)``），
        而 SDK 生成的 Schema 里 ``hint`` 只来自 ``json_schema_extra["hint"]``；本插件为"每个配置项
        都要有一句说明"用的是 ``x-toml-comment``（写进 ``config.toml`` 的行尾注释）+ 较长的
        ``description``，于是界面上只剩一个名字、看不到任何说明。这里统一补上：
        ``hint`` = 该字段的简短注释（与 ``config.toml`` 的注释同一份文案，改一处两处同步）。

        英文界面优先用 SDK 已生成的 ``i18n.en.hint``，因此只在 ``hint`` 为空时才补。
        """
        schema = super().get_webui_config_schema(**kwargs)
        if not isinstance(schema, dict):
            return schema
        comments = self._config_toml_comments()
        if not comments:
            return schema
        sections = schema.get("sections")
        if not isinstance(sections, dict):
            return schema
        for section_name, section in sections.items():
            fields = section.get("fields") if isinstance(section, dict) else None
            if not isinstance(fields, dict):
                continue
            for field_name, field_schema in fields.items():
                if not isinstance(field_schema, dict):
                    continue
                if str(field_schema.get("hint") or "").strip():
                    continue
                comment = (comments.get(section_name) or {}).get(field_name)
                if comment:
                    field_schema["hint"] = comment
        return schema

    def _config_toml_comments(self) -> Dict[str, Dict[str, str]]:
        """从配置模型的 ``json_schema_extra['x-toml-comment']`` 收集"节.字段 → 行尾注释"。"""
        comments: Dict[str, Dict[str, str]] = {}
        config_class = type(self).get_config_model()
        if config_class is None:
            return comments
        for section_name, field_info in config_class.model_fields.items():
            section_class = field_info.annotation
            if not isinstance(section_class, type) or not hasattr(section_class, "model_fields"):
                continue
            for field_name, sub_info in section_class.model_fields.items():
                extra = sub_info.json_schema_extra or {}
                if not isinstance(extra, dict):
                    continue
                comment = str(extra.get("x-toml-comment") or "").strip()
                if comment:
                    comments.setdefault(section_name, {})[field_name] = comment
        return comments

    def _write_config_comments(self) -> None:
        """给 ``config.toml`` 补上每项的行尾注释（缺注释才写，写过就跳过）。

        为什么这么写：宿主的插件配置写入器不含注释，插件只能自己补。**逐行文本处理**
        而不是用 tomlkit——这一版 tomlkit 把布尔值解析成普通 ``bool``（没有 ``.comment()``），
        用它反而加不上注释；逐行处理还能顺手跳过数组/内联表这类多行值。
        只**补注释**、绝不改值：值后面已经有 ``#`` 的、值是数组/表开头的、不是 ``键 = 值`` 形式的
        行都原样跳过。之后的合并（宿主 Runner 与 WebUI 都走增量合并）会保留这些注释。

        **两道"绝不改值"的保险**（0.11.2 加固，对应安全审查中-2）：

        1. 识别 TOML **三引号多行字符串**：``generation_prompt`` 就是多行字段，它的起始行
           ``generation_prompt = \"\"\"`` 形状上像"键 = 值"，朴素实现会把注释追加到字符串
           **内部**从而改掉值；进入多行状态后整段原样跳过，直到闭合的三引号。
        2. 写盘前用 ``tomllib`` 把**改前**与**改后**都解析一遍并断言结果相等：万一还有
           没预料到的 TOML 形态被误伤，宁可**不写**（注释只是锦上添花），也绝不改用户的配置值。
        """
        path = Path(__file__).resolve().parent / "config.toml"
        if not path.is_file():
            return
        try:
            comments = self._config_toml_comments()
            original = path.read_text(encoding="utf-8")
            lines = original.splitlines()
            header = "# 更好的消息后处理：配置按功能板块排版（引用回复 / 错别字 / 分段）；宿主已有的参数留空 = 跟随宿主。"
            changed = False
            if not any(line.strip() == header for line in lines[:5]):
                lines.insert(0, header)
                changed = True
            section = ""
            rendered: list[str] = []
            in_multiline = False
            for line in lines:
                stripped = line.strip()
                if in_multiline:
                    # 三引号字符串内部：一个字符都不要动。
                    rendered.append(line)
                    if stripped.count('"""') % 2 == 1:
                        in_multiline = False
                    continue
                if stripped.startswith("[") and stripped.endswith("]"):
                    section = stripped.strip("[]").strip()
                    rendered.append(line)
                    continue
                if section and "=" in line and not stripped.startswith("#"):
                    key, _, value = stripped.partition("=")
                    key = key.strip()
                    value = value.strip()
                    if value.count('"""') % 2 == 1:
                        # 多行字符串起始行：本行与后续行全部原样跳过。
                        in_multiline = True
                        rendered.append(line)
                        continue
                    comment = comments.get(section, {}).get(key)
                    if (
                        comment
                        and "#" not in value
                        and value
                        and not value.startswith(("[", "{"))
                    ):
                        rendered.append(f"{line.rstrip()}  # {comment}")
                        changed = True
                        continue
                rendered.append(line)
            if changed:
                candidate = "\n".join(rendered) + "\n"
                if not self._toml_values_unchanged(original, candidate):
                    self._log_quiet(
                        "warning",
                        "补 config.toml 注释会改动配置值，已放弃本次写入（配置未被修改）: %s",
                        path,
                    )
                    return
                path.write_text(candidate, encoding="utf-8")
                self._log_quiet("info", "已为 config.toml 补齐配置项注释: %s", path)
        except Exception as exc:  # 注释只是锦上添花，失败绝不影响插件运行
            self._log_quiet("debug", "补 config.toml 注释失败（忽略）: %s", exc)

    @staticmethod
    def _toml_values_unchanged(before: str, after: str) -> bool:
        """断言"只补注释"没有改动任何配置值（解析失败按"已改动"处理，宁可不写）。

        ``tomllib`` 是 Python 3.11+ 标准库；环境不支持时退回 ``tomlkit``；两者都没有就
        不做这道校验（不能因为校验器缺失就让注释功能整体失效）。
        """
        parse: Optional[Callable[[str], Any]] = None
        try:
            import tomllib

            parse = tomllib.loads
        except Exception:
            try:
                import tomlkit

                parse = lambda text: tomlkit.parse(text).unwrap()  # noqa: E731
            except Exception:
                return True
        try:
            assert parse is not None
            return bool(parse(before) == parse(after))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def on_load(self) -> None:
        self._rebuild_derived_state()
        self._write_config_comments()
        await self._refresh_global_config()
        self._initialize_meaning_store()
        # 循环无条件启动（内部按配置与库可用性自门控），保证含义库关闭时状态清理仍有节奏。
        self._track_task(asyncio.create_task(self._meaning_scan_loop()))
        self._track_task(asyncio.create_task(self._meaning_worker_loop()))
        # 后处理接管激活时预热拼音/字频/词频（后台线程，避免首次接管回复卡顿）。
        if self._post_process_takeover_active():
            self._track_task(asyncio.create_task(self._warmup_processor()))
        self.ctx.logger.info(
            "插件已加载。引用回复开关=%s，插件接管=%s（群聊权重 %s / 私聊权重 %s；"
            "群聊接管=%s 只引用一次=%s，私聊接管=%s 只引用一次=%s）；"
            "回复后表情包=%s（p=%.2f，planner 表情工具=%s）；"
            "表情包跟风=%s（mode=%s, threshold=%s, 忽略自身=%s）；文本规则：replace=%d, cover=%d, 忽略=%d；"
            "表情包含义库=%s（已收录 %d 条）；聊天流表情冷却=%.0f 秒",
            self._cfg.get("enable_reply_quote"),
            self._quote_takeover_active(),
            self._reply_style_weights,
            self._reply_style_weights_private,
            self.config.quote_reply.takeover,
            self.config.quote_reply.quote_once_per_target,
            self.config.quote_reply_private.takeover,
            self.config.quote_reply_private.quote_once_per_target,
            self.config.emoji_after_reply.enabled,
            self.config.emoji_after_reply.probability,
            sorted(self._planner_emoji_tool_names()) or "关闭",
            self.config.emoji_follow.enabled,
            self.config.emoji_follow.mode,
            self.config.emoji_follow.threshold,
            self.config.emoji_follow.ignore_self_messages,
            len(self._parsed_rules.replaces),
            len(self._parsed_rules.covers),
            len(self._parsed_rules.invalid),
            "启用" if self._meaning_store is not None else "不可用",
            self._meaning_store.count() if self._meaning_store is not None else 0,
            self.config.emoji_cooldown.seconds,
        )
        takeover_active = self._post_process_takeover_active()
        # 模块可用性（前置条件：需要宿主关闭哪些能力）：启动时打印一次，配置变更时再打印。
        self._log_module_status(reason="启动")
        if takeover_active:
            # 提前构建一次处理器：依赖缺失（jieba / pypinyin）时在此显式报错，
            # 而不是等到第一条回复才在 Hook 里静默降级。
            try:
                self._get_processor()
            except Exception as exc:
                self.ctx.logger.error("后处理接管不可用，本次回复将由框架自行处理：%s", exc)

    async def on_unload(self) -> None:
        # 周期任务与**多段发送的补发任务**一起取消：补发任务不在 self._tasks 里，
        # 漏掉会出现"卸载/热重载后仍在向会话补发"或"补发半途而废"。
        follow_up_tasks = [
            task
            for bucket in self._stream_follow_up_tasks.values()
            for task in bucket
            if not task.done()
        ]
        tasks = list(self._tasks) + follow_up_tasks
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._stream_follow_up_tasks.clear()
        self._stream_sending_texts.clear()
        self._pending_follow_ups.clear()
        self._prepared_segments.clear()
        self._pending_quote_targets.clear()
        if self._meaning_store is not None:
            self._meaning_store.close()
            self._meaning_store = None
        self.ctx.logger.info("插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == CONFIG_RELOAD_SCOPE_SELF:
            self._rebuild_derived_state()
            self.ctx.logger.info(
                "插件配置已更新：文本规则 replace=%d, cover=%d, 忽略=%d；回复方式权重 群聊=%s 私聊=%s",
                len(self._parsed_rules.replaces),
                len(self._parsed_rules.covers),
                len(self._parsed_rules.invalid),
                self._reply_style_weights,
                self._reply_style_weights_private,
            )
            self._log_module_status(reason="插件配置变更")
        elif scope == "bot":
            await self._refresh_global_config()
            if self._post_process_takeover_active():
                # 宿主把后处理总开关关掉后接管才生效：此时补一次预热，避免首条回复卡顿。
                self._track_task(asyncio.create_task(self._warmup_processor()))
            self._log_module_status(reason="宿主配置变更")

    # ------------------------------------------------------------------
    # 配置刷新与派生状态
    # ------------------------------------------------------------------

    async def _refresh_global_config(self) -> None:
        """从宿主全局配置读取模块前置条件与后处理接管参数并缓存。

        缓存里同时保留「短键」（历史代码用）与「完整配置路径」（``modules.requirements``
        的前置条件判定用）。
        """
        values: Dict[str, Any] = {}
        enable_reply_quote = await self._read_global_config("chat.reply_style.enable_reply_quote", True)
        if not isinstance(enable_reply_quote, bool):
            # 宿主执行失败时 SDK 原样返回错误 dict（truthy），显式回退避免误判开关状态。
            self.ctx.logger.warning(
                "宿主配置 enable_reply_quote 返回异常形态 %r，使用默认值 True", enable_reply_quote
            )
            enable_reply_quote = True
        values["enable_reply_quote"] = enable_reply_quote
        values["chat.reply_style.enable_reply_quote"] = enable_reply_quote

        for cache_key, path, default in _POST_PROCESS_CONFIG_KEYS:
            value = await self._read_global_config(path, default)
            values[cache_key] = value
            values[path] = value

        # 前置条件用到的其它宿主开关（如 experimental.enable_rich_reply）：读不到就存 None，
        # 模块据此保持静默（保守优先——宁可不介入，也不要与宿主原生能力重复处理）。
        for path in REQUIRED_HOST_PATHS:
            if path not in values:
                values[path] = await self._read_global_config(path, None)

        self._cfg = values
        # 分段/错别字参数可能已变化，作废旧处理器，下次按新参数重建。
        self._processor = None

    async def _read_global_config(self, path: str, default: Any) -> Any:
        """读取单个宿主全局配置项；能力被拒/读取异常时回退默认值。"""
        try:
            value = await self.ctx.config.get(path, default)
        except Exception as exc:  # 能力被拒/读取异常时回退默认
            self.ctx.logger.warning("读取宿主配置 %s 失败，使用默认值 %r: %s", path, default, exc)
            return default
        if value is None:
            return default
        return value

    def _rebuild_derived_state(self) -> None:
        """解析文本规则并重建群聊/私聊回复方式权重（插件配置变化时调用）。"""
        self._parsed_rules = parse_rules(self.config.text_rules.rules)
        for bad_rule in self._parsed_rules.invalid:
            self.ctx.logger.warning("文本规则格式错误，已忽略: %r", bad_rule)

        self._reply_style_weights = self._build_weight_pool(
            self.config.quote_reply_weights, label="群聊", allow_at=True
        )
        self._reply_style_weights_private = self._build_weight_pool(
            self.config.quote_reply_private_weights, label="私聊", allow_at=False
        )

        # 插件配置里的镜像项（[response_splitter] / [chinese_typo] / [quote_reply*]）变化后必须
        # 重建处理器，否则改插件配置后仍按旧参数处理（宿主配置变化走 _refresh_global_config）。
        self._processor = None

    def _plugin_enabled(self) -> bool:
        return bool(self.config.plugin.enabled)

    # ------------------------------------------------------------------
    # 模块可用性（前置条件：需要宿主关闭哪些能力）
    # ------------------------------------------------------------------

    def _rich_reply_gate(self) -> bool:
        """「丰富回复」是否仍作为两个接管的硬门槛（``[plugin] rich_reply_gate``）。"""
        return bool(getattr(self.config.plugin, "rich_reply_gate", True))

    def _module_status(self, key: str) -> Any:
        """判定某个模块当前是否可用（依据宿主配置快照）。"""
        return evaluate_module(key, self._cfg, rich_reply_gate=self._rich_reply_gate())

    def _module_available(self, key: str) -> bool:
        """该模块的前置条件是否已满足。未满足时模块必须完全静默。"""
        return bool(self._module_status(key).available)

    def _log_module_status(self, *, reason: str) -> None:
        """打印各模块可用状态（启动与配置变更时调用）。

        静默的模块用 ``warning`` 输出：接管类功能"配置打开却什么都不做"最难排查，
        必须让它落在日志里显眼的位置。
        """
        for status in iter_module_statuses(self._cfg, rich_reply_gate=self._rich_reply_gate()):
            line = status.describe()
            if status.restricted and not status.available:
                self.ctx.logger.warning("[模块状态｜%s] %s", reason, line)
            else:
                self.ctx.logger.info("[模块状态｜%s] %s", reason, line)

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
        """出站消息增强入口。

        只有真的改动了消息体（文本规则命中 / 引用方式抽取生效）才回传
        ``modified_kwargs``：宿主对回传的消息会做一次
        ``deserialize_session_message`` 往返重建，未改动也回传等于对**每一条**
        出站消息（包括其它插件的命令响应、提示消息）都做一次额外处理。
        """
        raw_message = kwargs.get("message")
        if not isinstance(raw_message, dict):
            return {"action": "continue"}

        # 拷贝一层，避免直接改动宿主原始载荷结构。
        message = dict(raw_message)
        modified: Dict[str, Any] = dict(kwargs)
        modified["message"] = message
        session_id = str(message.get("session_id") or "").strip()
        reply_message_id = str(modified.get("reply_message_id") or "").strip()
        changed = False

        # 本条发送是否为本插件正在补发的分段（多段发送）：文本规则据此把它认作
        # 本轮回复的一部分（这类发送 reply_message_id 为空，轮记录判定不成立）。
        outbound_text = self._extract_outbound_text(message)
        own_follow_up = self._is_own_follow_up_text(session_id, outbound_text)
        quote_target = ""
        if own_follow_up:
            quote_bucket = self._pending_quote_targets.get(session_id)
            if quote_bucket:
                quote_target = str(quote_bucket.get(self._text_key(outbound_text)) or "").strip()

        try:
            changed = self._apply_text_rules_to_message(
                message,
                session_id=session_id,
                reply_message_id=reply_message_id,
                own_follow_up=own_follow_up,
            ) or changed
        except Exception as exc:
            self.ctx.logger.warning("出站文本规则应用失败，跳过: %s", exc)

        try:
            changed = await self._apply_reply_style(modified, message) or changed
        except Exception as exc:
            self.ctx.logger.warning("回复方式抽取失败，保持原样: %s", exc)

        # 错别字更正段引用上一段（复刻宿主 quote_previous）：ctx.send.text 无法携带
        # reply_message，这里补上 set_reply + reply_message_id，宿主据此构造引用组件。
        if quote_target and not reply_message_id and not bool(modified.get("set_reply")):
            modified["set_reply"] = True
            modified["reply_message_id"] = quote_target
            changed = True

        if not changed:
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": modified}

    def _apply_text_rules_to_message(
        self,
        message: Dict[str, Any],
        *,
        session_id: str = "",
        reply_message_id: str = "",
        own_follow_up: bool = False,
    ) -> bool:
        """对出站消息的纯文本组件应用替换/覆盖规则（原地修改传入副本）；有改动返回 True。

        ``apply_scope="reply_flow"``（默认）时只处理**本轮回复自己发出的消息**
        （宿主回复工具拉起的正常回复流程，含多段发送由插件补发的分段），其它插件用
        ``ctx.send.*`` 直接发出的文本一律不动——宿主的出站 Hook 载荷里没有来源字段，
        轮记录与补发登记是唯一可靠的判别依据。
        """
        rules = self._parsed_rules
        if not rules or not self._plugin_enabled():
            return False
        if str(getattr(self.config.text_rules, "apply_scope", "reply_flow")) != "all_outbound":
            if not own_follow_up and not self._message_in_reply_flow(session_id, reply_message_id):
                self.ctx.logger.debug(
                    "跳过文本规则：该发送不属于 bot 本轮回复（会话 %s，reply_message_id=%s）",
                    session_id or "?",
                    reply_message_id or "-",
                )
                return False

        components = message.get("raw_message")
        if not isinstance(components, list):
            return False

        text_indexes = [
            index
            for index, component in enumerate(components)
            if isinstance(component, dict) and str(component.get("type") or "") == "text"
        ]
        if not text_indexes:
            return False

        text_set = set(text_indexes)
        full_text = "".join(
            str(components[index].get("data") or "") for index in text_indexes
        )
        if not full_text:
            return False

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
            return True

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
        return changed

    @staticmethod
    def _is_group_message(message: Dict[str, Any]) -> bool:
        message_info = message.get("message_info")
        if not isinstance(message_info, dict):
            return False
        return isinstance(message_info.get("group_info"), dict)

    def _message_in_reply_flow(self, session_id: str, reply_message_id: str) -> bool:
        """该出站消息是否属于"planner 拉起的本轮回复"。

        判别依据（宿主的出站 Hook 载荷里没有来源字段，轮记录是唯一可靠依据）：

        1. 会话存在本插件登记的回复轮；
        2. 轮还没看到过自己的出站消息时（首条分段正在发出），要求仍处于
           ``first_send_timeout_seconds`` 的等待窗内——超出说明这轮回复其实没发出去
           （发送失败 / 被中止），此时后面同目标的发送与它无关；
        3. 且满足下列之一：
           - 消息指向该轮的目标消息（宿主对每个分段都带 ``reply_message = 目标``）；
           - 消息引用的消息 id 属于该轮已观测到的出站消息 id（错别字更正段会引用
             上一段，也就是 bot 自己的消息）。

        其它插件用 ``ctx.send.*`` 直接发出的文本、宿主命令提示等都不满足上述条件，
        因此不会被文本规则改写。
        """
        if not session_id:
            return False
        reply_round = self._reply_rounds.get(session_id)
        if reply_round is None:
            return False
        if not reply_round.get("output_seen"):
            arming = float(self.config.emoji_after_reply.first_send_timeout_seconds)
            if arming <= 0:
                arming = _REPLY_FLOW_ARM_FALLBACK_SECONDS
            if time.monotonic() - float(reply_round.get("started", 0.0)) > arming:
                return False
        normalized_reply_id = str(reply_message_id or "").strip()
        if not normalized_reply_id:
            return False
        round_target_id = str(reply_round.get("target_id") or "").strip()
        if round_target_id and normalized_reply_id == round_target_id:
            return True
        sent_ids = reply_round.get("sent_ids")
        return isinstance(sent_ids, list) and normalized_reply_id in sent_ids

    @staticmethod
    def _remember_round_sent_id(reply_round: Dict[str, Any], message_id: Any) -> None:
        """记录本轮回复已发出的消息 id（限长，供错别字更正段的归属判定）。"""
        normalized_id = str(message_id or "").strip()
        if not normalized_id:
            return
        sent_ids = reply_round.get("sent_ids")
        if not isinstance(sent_ids, list):
            sent_ids = []
            reply_round["sent_ids"] = sent_ids
        if normalized_id in sent_ids:
            return
        sent_ids.append(normalized_id)
        if len(sent_ids) > _ROUND_SENT_ID_MAX_ENTRIES:
            del sent_ids[: len(sent_ids) - _ROUND_SENT_ID_MAX_ENTRIES]

    # ------------------------------------------------------------------
    # Hook 2：回复轮标记（planner 激活了回复）
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="reply_round_marker",
        description=(
            "blocking：标记 planner 激活了一轮回复，供回复方式一致性与回复后表情包判定使用"
            "（必须 blocking：observe 处理器宿主不等待，轮记录可能晚于首条消息的发送）"
        ),
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_reply_round_marker(self, **kwargs: Any) -> None:
        """登记回复轮记录（阻塞执行，保证"首条消息"判定成立）。

        这里**必须是 blocking**：observe 处理器由宿主 ``asyncio.create_task`` 后台调度
        （``hook_dispatcher._schedule_observe_handler``），宿主不等它完成就继续后处理并发送
        首条消息；一旦轮记录晚到，首条消息会因为"查不到本轮记录 / 只查到上一轮的旧记录"而
        被静默跳过（不置 ``style_done``），@ 就会落到**第二条**消息上（实测现象：
        "第一条没 @、第二条才 @"）。改成 blocking 后，轮记录在宿主继续之前必定就绪。

        ``order=LATE``：让同一 Hook 上其它插件的 ``NORMAL`` 处理器（例如智能分段插件的
        ``smart_segmentation_preserve_prepared_response``）先执行；本处理器与后处理接管同为
        LATE、按名字排在接管**之后**，因此记下的 ``response`` 是接管改写后的正文
        （该字段仅供调试/审计）。让位给其它插件时轮记录照常登记，@/引用 抽取不受影响。
        """
        if not self._plugin_enabled():
            return
        # 轮记录是"planner 拉起的回复正在发出"的唯一权威标记，三类消费方共用：
        # 回复方式一致性抽取、回复后表情包判定、文本规则的作用范围判定。
        # 因此无条件登记（开销仅一条 dict 记录），是否需要处理由各消费方自行门控。
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
            # planner 本轮已计划自行发表情/贴表情（由 planner.after_response 观察得到）。
            "planned_emoji": self._planned_emoji_active(session_id, now),
            "window": float(self.config.emoji_after_reply.round_window_seconds),
            "target_id": str(kwargs.get("reply_message_id") or "").strip(),
            # 本轮回复正文（后处理接管激活时为改写后的正文），仅作调试/审计用。
            "response": str(kwargs.get("response") or "")[:200],
            # 本轮已观测到的出站消息 id：错别字更正段会引用上一段（bot 自己的消息），
            # 文本规则据此把更正段也认作本轮回复的一部分。
            "sent_ids": [],
            # 本轮是否已经见到自己的出站消息：没见到说明回复没真的发出，按超时丢弃。
            "output_seen": False,
            # 本轮补发判定是否已完成（完成后轮记录保留给引用回复接管用）。
            "emoji_done": False,
            # 引用回复接管是否已抽取过回复方式（同一轮只在首条文本分段抽取一次）。
            "style_done": False,
        }
        self._prune_stale_state(now)

    # ------------------------------------------------------------------
    # Hook 2b：planner 表情意图观察（决定本轮是否还需要补发）
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.planner.after_response",
        name="planner_emoji_intent_observer",
        description=(
            "observe：读取 planner 本轮模型输出的工具调用，命中 planner_emoji_tools 时"
            "标记该会话（回复后表情包据此跳过补发）"
        ),
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_planner_after_response(self, **kwargs: Any) -> None:
        """planner 决定自己发表情时提前打标记。

        宿主工具调用按模型输出顺序串行执行，且 ``send_emoji`` 内部还要跑视觉子代理
        选图，所以"planner 已经/即将发表情"这件事必须在工具执行**之前**从
        ``maisaka.planner.after_response`` 的 ``output_items`` 里读出来——只观察已
        发出的出站消息时，本插件会在表情真正发出前就判定"本轮没有表情"并补发一张。
        """
        if not self._plugin_enabled() or not self.config.emoji_after_reply.enabled:
            return
        tool_names = self._planner_emoji_tool_names()
        if not tool_names:
            return
        session_id = str(kwargs.get("session_id") or "").strip()
        if not session_id:
            return
        output_items = kwargs.get("output_items")
        if not isinstance(output_items, list):
            return
        tool_name = self._find_planned_emoji_tool(output_items, tool_names)
        if not tool_name:
            return

        now = time.monotonic()
        ttl = max(5.0, float(self.config.emoji_after_reply.planned_emoji_ttl_seconds))
        self._planned_emoji_until[session_id] = now + ttl
        reply_round = self._reply_rounds.get(session_id)
        if reply_round is not None:
            reply_round["planned_emoji"] = True
        self.ctx.logger.debug(
            "planner 本轮计划调用 %s（会话 %s），本轮起 %.0f 秒内不补发表情包",
            tool_name,
            session_id,
            ttl,
        )

    @staticmethod
    def _find_planned_emoji_tool(output_items: List[Any], tool_names: set[str]) -> str:
        """从 planner 输出 Items 中找出命中的表情类工具调用名；未命中返回空串。

        宿主输出项快照里工具调用形如
        ``{"item_type": "FunctionCallItem", "tool_call": {"call_id": ..., "func_name": ..., "args": {...}}}``
        （见宿主 ``request_snapshot.serialize_context_item_snapshot``）。形态不匹配时
        一律返回空串——即该判定静默退化为"只看已发出的出站消息"，不会误抑制补发。
        """
        for item in output_items:
            if not isinstance(item, dict):
                continue
            if str(item.get("item_type") or "") != "FunctionCallItem":
                continue
            tool_call = item.get("tool_call")
            if not isinstance(tool_call, dict):
                continue
            func_name = str(tool_call.get("func_name") or "").strip()
            if func_name and func_name.lower() in tool_names:
                return func_name
        return ""

    def _planner_emoji_tool_names(self) -> set[str]:
        """配置的 planner 表情类工具名集合（小写，供输出 Items 比对）。"""
        raw_names = self.config.emoji_after_reply.planner_emoji_tools
        if not isinstance(raw_names, (list, tuple)):
            return set()
        return {str(name).strip().lower() for name in raw_names if str(name).strip()}

    def _planned_emoji_active(self, session_id: str, now: float) -> bool:
        """该会话当前是否处于"planner 自行处理表情"标记的有效期内。"""
        if not session_id:
            return False
        until = self._planned_emoji_until.get(session_id)
        return until is not None and now < float(until)

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
        # 「同一消息只引用一次」的事实来源：只要观测到某条出站消息确实以引用方式发出
        # （set_reply=True + reply_message_id），就登记该目标已引用过——本插件抽取的、
        # 宿主自带的、其它插件注入的引用都算，之后对该目标的回复不再引用。
        if bool(kwargs.get("set_reply", False)):
            self._remember_quoted_target(session_id, str(kwargs.get("reply_message_id") or ""))
        if has_emoji:
            self._chat_emoji_last[session_id] = now
            # planner 已经真的把表情发出来了，意图标记不再需要。
            self._planned_emoji_until.pop(session_id, None)
            # 默认连 storage_message=False 的出站表情也计入冷却：其它插件（转发类插件等）
            # 常用该参数发消息，不计入时本插件会紧接着在别人的表情后再补/跟一张。
            if bool(kwargs.get("storage_message", True)) or bool(self.config.emoji_cooldown.count_unstored):
                self._chat_emoji_cooldown_start[session_id] = now

        reply_round = self._reply_rounds.get(session_id)
        if reply_round is None:
            return
        started = float(reply_round.get("started", now))
        if now - started > float(reply_round.get("window", 90.0)):
            self._reply_rounds.pop(session_id, None)
            return
        if not reply_round.get("output_seen"):
            # 回复轮建立后一直没见到本轮出站消息：说明这轮回复并没有真的发出去
            # （发送失败、被其它插件在 send_service.before_send 中止等）。此时若继续保留
            # 该轮，之后**任何来源**的出站消息（其它插件的命令响应、主动问候、转发插件）
            # 都会被当成"本轮回复的输出"并触发补发，因此超时即丢弃。
            first_send_timeout = float(self.config.emoji_after_reply.first_send_timeout_seconds)
            if first_send_timeout > 0 and now - started > first_send_timeout:
                self._reply_rounds.pop(session_id, None)
                self.ctx.logger.debug(
                    "回复轮 %.0f 秒内未出现出站消息，判定本轮回复未发出，已丢弃（会话 %s）",
                    now - started,
                    session_id,
                )
                return
        if has_emoji:
            reply_round["had_emoji"] = True
        reply_round["output_seen"] = True
        self._remember_round_sent_id(reply_round, message.get("message_id"))

        if reply_round.get("emoji_done"):
            # 本轮补发判定已经完成：轮记录保留给引用回复接管用，不再重复判定。
            return

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

        判定完成后**不删除轮记录**，只标记 ``emoji_done``：同一轮记录还被引用回复
        接管消费（"同一轮只抽取一次"），提前删除会让后续分段/后到的发送退化成
        "逐条抽取"并给它们追加引用或 @。记录最终由新的一轮标记覆盖或超时清理。
        """
        reply_round = self._reply_rounds.get(session_id)
        if reply_round is None or int(reply_round.get("id", 0)) != round_id:
            return
        if reply_round.get("emoji_done"):
            return
        reply_round["emoji_done"] = True

        now = time.monotonic()
        if reply_round.get("had_emoji"):
            return
        if now - float(reply_round.get("started", now)) > float(reply_round.get("window", 90.0)):
            return
        # planner 本轮已自行发表情/贴表情（工具可能还没执行完）：补发会造成"多发一张"。
        if reply_round.get("planned_emoji") or self._planned_emoji_active(session_id, now):
            self.ctx.logger.debug(
                "planner 本轮已自行处理表情（会话 %s），跳过补发",
                session_id,
            )
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

        # bot 自己的消息（以及插件自注入的合成记录）不是"群友表情包"：宿主没有 self 过滤，
        # 适配器关闭"忽略自身消息"时，bot 刚发的表情包会回环成入站表情组件（NapCat/SnowLuma
        # 出站表情按 image/sub_type=1 下发，入站会判为 emoji）。这类消息既不计入连击、
        # 也不打断连击，直接忽略（含义缓存同样不记录）。
        if self.config.emoji_follow.ignore_self_messages and self._is_self_message(message):
            self.ctx.logger.debug("忽略自身/注入消息，不参与表情包跟风统计（会话 %s）", session_id)
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

        streak_window = max(10.0, float(cfg.streak_window_seconds))
        streak = self._emoji_streaks.get(session_id)
        if streak is None or now - float(streak.get("last", 0.0)) > streak_window:
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

    @staticmethod
    def _is_self_message(message: Dict[str, Any]) -> bool:
        """判断入站消息是否来自 bot 自身账号，或为插件自注入的合成记录。

        宿主本身不做 self 过滤（``ignore_self_message`` 是适配器侧行为），因此这里按
        ``additional_config`` 里的 ``self_id`` / ``platform_io_account_id`` 与发送者
        比对；带 ``*injected*`` 标记键的合成记录（如转发/补录插件注入）同样跳过。
        缺少可判定信息时返回 False（fail-open，保持原有统计行为）。
        """
        message_info = message.get("message_info")
        if not isinstance(message_info, dict):
            return False
        user_info = message_info.get("user_info")
        if not isinstance(user_info, dict):
            return False
        user_id = str(user_info.get("user_id") or "").strip()
        if not user_id:
            return False
        additional_config = message_info.get("additional_config")
        if not isinstance(additional_config, dict):
            return False
        for key in additional_config:
            if "injected" in str(key).lower():
                return True
        for key in ("self_id", "platform_io_account_id", "bot_id", "account_id"):
            value = str(additional_config.get(key) or "").strip()
            if value and value == user_id:
                return True
        return False

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
        description="planner 拉起的回复流程里，把上下文表情包的准确含义注入 extra_prompt",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_replyer_emoji_injector(self, **kwargs: Any) -> Dict[str, Any]:
        request_type = str(kwargs.get("request_type") or "").strip()
        if not self._meaning_request_type_allowed(request_type):
            self.ctx.logger.debug(
                "跳过表情包含义注入：request_type=%r 不在注入范围内（插件自行生成的回复不注入）",
                request_type or "?",
            )
            return {"action": "continue"}

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

    def _meaning_request_type_allowed(self, request_type: str) -> bool:
        """该 replyer 请求是否允许注入含义（默认只注入 planner 拉起的回复流程）。

        宿主的回复工具以 ``request_type="maisaka.replyer"`` 取生成器；插件自己拉起
        回复生成（``generator_api`` / ``plugin.<id>`` 等）不属于正常回复流程，不注入。
        插件激活 planner（主动触发、注入上下文）后由 planner 拉起的 reply 仍是
        ``maisaka.replyer``，因此照常注入。
        """
        raw_names = self.config.emoji_meaning.inject_request_types
        if not isinstance(raw_names, (list, tuple)):
            return True
        names = {str(name).strip().lower() for name in raw_names if str(name).strip()}
        if not names:
            return True  # 留空 = 不过滤
        if "*" in names:
            return True
        return str(request_type or "").strip().lower() in names

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

        只取"已注册、有文件、未禁用"的行：含义生成取图依赖宿主运行库（已注册且
        文件可用的表情才会被 ``emoji.get_random`` 抽到），Images 表里仅完成描述
        构建、未注册成功或文件缺失的行永远取不到图，不过滤会导致这些条目被
        周期扫描无限复活。
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
        """含义生成工作循环：每轮从队列取 batch_size 条，抽样取图后调 VLM。"""
        while True:
            try:
                await asyncio.sleep(max(5.0, float(self.config.emoji_meaning.worker_interval_seconds)))
                # 循环无条件启动，顺带承担全局状态清理的节拍（不依赖回复轮标记触发）。
                self._prune_stale_state(time.monotonic())
                if self.config.emoji_meaning.enabled and self._meaning_store is not None:
                    await self._process_meaning_batch()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.ctx.logger.warning("表情包含义生成工作循环异常: %s", exc)

    async def _process_meaning_batch(self) -> None:
        """处理一批待补录描述：抽样取图、生成含义、入库。

        宿主 ``emoji.get_all`` 会把整个表情库的 base64 塞进单个 RPC 帧，大库超过
        16MB 帧上限（线上实测 47MB 被拒），因此改为 ``emoji.get_random`` 分批抽样：
        每轮抽 k 张（帧错误自动减半、命中不足逐步恢复），命中队列描述的立即生成；
        随机抽样对全库均匀覆盖，未命中的描述后续轮次必然命中。
        """
        cfg = self.config.emoji_meaning
        batch: List[str] = []
        while self._meaning_queue and len(batch) < cfg.batch_size:
            description = self._meaning_queue.popleft()
            self._meaning_queued.discard(description)
            batch.append(description)
        if not batch:
            return

        sample = await self._sample_emoji_images()
        if sample is None:
            # 抽样失败（帧超限等）：整批顺延，抽样张数已自适应下调。
            for description in batch:
                self._enqueue_emoji_meaning(description, force=True)
            return

        for description in batch:
            image = sample.get(description)
            if image is None:
                # 本轮抽样未命中：不计入生成失败次数，等待后续轮次命中。
                self._record_meaning_sample_miss(description)
                continue
            try:
                meaning = await self._generate_emoji_meaning(description, image[0], image[1])
            except Exception as exc:
                self.ctx.logger.warning("表情包含义生成失败（desc=%r）: %s", description, exc)
                self._record_meaning_failure(description, str(exc))
                continue
            self._meaning_attempts.pop(description, None)
            self._meaning_misses.pop(description, None)
            self._meaning_store.upsert(description, meaning, image_format=image[1], source="scan")
            self.ctx.logger.info("已收录表情包含义（desc=%r，长度 %d）", description, len(meaning))

    async def _sample_emoji_images(self) -> Optional[Dict[str, Tuple[str, str]]]:
        """随机抽样一批表情包图片，返回 ``描述 -> (base64, 格式)``；失败返回 None。"""
        try:
            count_result = await self.ctx.emoji.get_count()
        except Exception as exc:
            self.ctx.logger.warning("获取表情包数量失败，本轮含义生成顺延: %s", exc)
            return None
        count = count_result if isinstance(count_result, int) and count_result > 0 else 0
        if count == 0:
            return {}

        k = max(1, min(self._meaning_sample_k, count))
        try:
            sample = await self.ctx.emoji.get_random(k)
        except Exception as exc:
            if self._meaning_sample_k > 1:
                self._meaning_sample_k = max(1, self._meaning_sample_k // 2)
            self.ctx.logger.warning(
                "表情包抽样失败（k=%d，已下调至 %d），本轮含义生成顺延: %s",
                k,
                self._meaning_sample_k,
                exc,
            )
            return None
        self._meaning_sample_k = min(_MEANING_SAMPLE_K_MAX, self._meaning_sample_k + 5)

        if not isinstance(sample, list):
            self.ctx.logger.warning("表情包抽样返回形态异常: %r", type(sample).__name__)
            return None
        desc_to_image: Dict[str, Tuple[str, str]] = {}
        for item in sample:
            if not isinstance(item, dict):
                continue
            description = str(item.get("description") or "").strip()
            image_base64 = str(item.get("base64") or "")
            if not description or not image_base64 or description in desc_to_image:
                continue
            desc_to_image[description] = (image_base64, sniff_image_format(image_base64))
        return desc_to_image

    def _record_meaning_sample_miss(self, description: str) -> None:
        """抽样未命中的描述不计入生成失败次数，仅按轮次计数，超限后放弃。"""
        misses = self._meaning_misses.get(description, 0) + 1
        if misses >= _MEANING_SAMPLE_MISS_CAP:
            self._meaning_misses.pop(description, None)
            self.ctx.logger.warning("表情包含义长期未抽样命中，放弃（desc=%r，等待 %d 轮）", description, misses)
            return
        self._meaning_misses[description] = misses
        self._enqueue_emoji_meaning(description, force=True)

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
        for session_id in [key for key, ts in self._chat_emoji_last.items() if now - ts > _STALE_STREAK_SECONDS]:
            self._chat_emoji_last.pop(session_id, None)
        # planner 表情意图标记：过期即清理（不依赖是否被消费）。
        for session_id in [
            key for key, until in self._planned_emoji_until.items() if now >= float(until)
        ]:
            self._planned_emoji_until.pop(session_id, None)
        # 连击保鲜时长可配：清理节奏不得低于它，否则长连击窗会被清理截断。
        streak_keep = max(_STALE_STREAK_SECONDS, float(self.config.emoji_follow.streak_window_seconds))
        for session_id in [
            key
            for key, value in self._emoji_streaks.items()
            if now - float(value.get("last", 0.0)) > streak_keep
        ]:
            self._emoji_streaks.pop(session_id, None)
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
        self._prune_quote_state(now)


def create_plugin() -> BetterPostProcessingPlugin:
    """Runner 加载入口。"""
    return BetterPostProcessingPlugin()
