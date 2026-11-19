"""后处理接管模块：错别字 + 分段 + 多段发送（完整复刻宿主后处理框架效果）。

**宿主前置条件**（由 ``modules.requirements`` 判定）：宿主需**不在做错别字处理**
（``response_post_process.enable_response_post_process = false`` 或 ``chinese_typo.enable = false``），
且丰富回复未开启（``experimental.enable_rich_reply`` 读不到 = 未开启，新版宿主已无该字段）；
任一未满足时本模块全部 Hook **静默返回**、不做任何处理。

Hook 分工（多段发送）：

1. ``maisaka.reply.before_post_process``（blocking）：算分段 → 回传全文（供宿主 reply 工具
   结果记录完整内容），其余入缓存；
2. ``send_service.after_build_message``（blocking）：按全文文本反查缓存，并把实际出站
   截回第 1 段 → 登记待补发；
3. ``send_service.after_send``（observe）：确认首段发出后，后台顺序补发剩余分段；
4. ``maisaka.planner.before_request`` / ``chat.receive.before_process``（blocking）：补发
   未完成时先等待（带超时）；planner 请求前还会按 ``items`` 中实际缺失的分段补齐，
   避免下一轮上下文缺段、新消息与旧回复交错。

错字纠正（见 ``post_processing.PostProcessor.plan``）：

- 0.12.0 起**恒由权重池决定**（``[chinese_typo] correction_mode_enabled`` 开关已按需求移除）：
  每处**真正出现**的错字抽一次纠正方式 = 直接发送 / 引用带错字的消息 / 撤回后重发修正句 /
  不纠正（四个权重全设 0 = 完全不纠正）；
- ``last_correction_enabled`` 打开时，由 ``last_correction_probability`` 决定该处纠正是否推迟到
  **本轮全部分段发完之后**（"最后纠正"）——推迟**只改时机**，纠正方式仍是权重池抽到的那个
  （直接补发 / 引用纠正 / 撤回重发），不再强制引用；
- 逐段决策在**分段全部发出之前**完成，动作序列 = 第 1 段 → 其纠正 → 第 2 段 → 其纠正 → …
  （所以"第二段出错字"的纠正一定早于第三段），"最后纠正"排在序列末尾；
- 撤回前先等 ``[chinese_typo] recall_delay_seconds``（留空 = 按被撤回那条的打字时长自动）。

共享状态（回复轮记录、文本规则、表情包功能）留在 ``plugin.py``，本模块通过 ``self`` 访问。
"""

from __future__ import annotations

import asyncio
import random
import re
import time
import tomllib
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from maibot_sdk import HookHandler
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from ..helpers import annotate_outbound, delivered_reply_style, outbound_metadata
from ..post_processing import CorrectionDecision, PostProcessor, ProcessedResponseSegment


@dataclass(frozen=True)
class FollowUpItem:
    """补发序列里的一个动作（顺序即语义）。

    - ``kind="text"``：发送 ``text``。``quote_previous=True`` 时引用**上一条**已发出的消息
      （宿主 ``quote_previous`` 语义）；``quote_slot>=0`` 时引用**第 slot 段**的消息；
      两者都没有就不引用。
    - ``kind="recall"``：撤回 ``recall_slot`` 指定的那条消息，
      然后发送 ``text``（"撤回后重发"：``text`` 是修正后的整句）。
      撤回失败时按 ``[chinese_typo] recall_fallback`` 兜底。会话不允许撤回时直接发 ``correction_text``。
    - ``slot``：本条发送对应第几段（用于给"最后纠正"记录引用/撤回目标），非分段消息填 -1。
    """

    kind: str
    text: str = ""
    quote_previous: bool = False
    quote_slot: int = -1
    slot: int = -1
    recall_slot: int = -1
    correction_text: str = ""


class FollowUpSendStatus(Enum):
    SENT = "sent"
    SKIPPED = "skipped"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class FollowUpSendResult:
    """单次补发结果。已确认送达的消息也可能没有可引用的平台 ID。"""

    status: FollowUpSendStatus
    message_id: str = ""


@dataclass(frozen=True)
class PreparedReply:
    created_at: float
    first_text: str
    items: List[FollowUpItem]
    planner_record: Dict[str, Any]
    unanchored: bool = False


@dataclass(frozen=True)
class FollowUpIntent:
    session_id: str
    quote_target: str
    at_target: str
    at_user_id: str
    at_nickname: str
    at_cardname: str
    unanchored: bool = False


# 项目根目录（插件位于 <root>/plugins/<plugin_dir>/modules/，用于定位宿主 depends-data 字频表）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# 后处理接管需要从宿主全局配置读取的参数：(缓存键, 宿主配置路径, **代码层兜底值**)。
#
# 分段/错别字/打字速度参数全部沿用宿主自身配置，改宿主配置无需改插件配置。
#
# **取值链路是四级的，前一级拿不到才退下一级**：
#   ① 插件配置里填了具体值     → 用它（用户显式覆盖，最优先）
#   ② 留空（``""``/「跟随宿主」）→ 用宿主**运行时**配置快照（``ctx.config.get``）
#   ③ 宿主快照读不到           → 用本表第 3 列的**代码层兜底**
#   ④ 第 3 列就是最终兜底      → 取自发布者实机在用的 ``config.toml``
#
# 也就是说：插件配置默认**留空** → 留空即**跟随宿主** → 跟随宿主失败才用这里的值。
# 第 3 列刻意用"发布者实机验证过的一套参数"而不是宿主官方默认值，这样在
# "宿主配置读不到"（能力异常 / 精简快照 / 单测）时得到的是可用且符合预期的行为。
_POST_PROCESS_CONFIG_KEYS: Tuple[Tuple[str, str, Any], ...] = (
    ("splitter_enable", "response_splitter.enable", True),
    ("splitter_max_length", "response_splitter.max_length", 512),
    ("splitter_max_sentence_num", "response_splitter.max_sentence_num", 8),
    ("splitter_max_split_num", "response_splitter.max_split_num", 4),
    ("splitter_enable_kaomoji_protection", "response_splitter.enable_kaomoji_protection", False),
    ("splitter_enable_overflow_return_all", "response_splitter.enable_overflow_return_all", True),
    ("typo_enable", "chinese_typo.enable", True),
    ("typo_enable_correction_quote", "chinese_typo.enable_correction_quote", True),
    ("typo_correction_quote_probability", "chinese_typo.correction_quote_probability", 1.0),
    ("typo_error_rate", "chinese_typo.error_rate", 0.01),
    ("typo_min_freq", "chinese_typo.min_freq", 9),
    ("typo_tone_error_rate", "chinese_typo.tone_error_rate", 0.1),
    ("typo_word_replace_rate", "chinese_typo.word_replace_rate", 0.006),
    ("bot_nickname", "bot.nickname", "普瑞赛斯"),
    # 「撤回反应时间」留空时按打字速度自动决定，需要宿主的速度值（见 ``_recall_delay_seconds``）。
    ("typing_speed", "response_post_process.typing_speed", 1.0),
)

# 宿主配置快照读不到时的**最终代码层兜底**（= ``_POST_PROCESS_CONFIG_KEYS`` 第 3 列，
# 取自发布者实机在用的 config.toml）。见上方"取值链路是四级的"注释。
_HOST_CONFIG_DEFAULTS: Dict[str, Any] = {key: default for key, _path, default in _POST_PROCESS_CONFIG_KEYS}

# 宿主已有参数在插件配置里的位置：(PostProcessor 形参, 插件配置节, 插件字段,
# 宿主 _cfg 短键, 类型, 宿主 bot_config.toml 的节, 宿主字段名)。
#
# 插件按"功能板块"排版（``[response_splitter]`` 分段、``[chinese_typo]`` 错别字…），
# 插件字段名/节名不一定等于宿主那一侧的名字，所以这里显式带上宿主侧信息，
# 供"首次生成配置时读宿主现值"使用（``get_default_config``）。
# 取值约定见 ``_resolve_mirror``：留空（""）/「跟随宿主」= 用 ``self._cfg`` 里的宿主值。
_HOST_MIRROR_FIELDS: Tuple[Tuple[str, str, str, str, str, str, str], ...] = (
    ("splitter_enable", "response_splitter", "enable", "splitter_enable", "switch", "response_splitter", "enable"),
    ("splitter_max_length", "response_splitter", "max_length", "splitter_max_length", "int", "response_splitter", "max_length"),
    (
        "splitter_max_sentence_num",
        "response_splitter",
        "max_sentence_num",
        "splitter_max_sentence_num",
        "int",
        "response_splitter",
        "max_sentence_num",
    ),
    (
        "splitter_max_split_num",
        "response_splitter",
        "max_split_num",
        "splitter_max_split_num",
        "int",
        "response_splitter",
        "max_split_num",
    ),
    (
        "splitter_enable_kaomoji_protection",
        "response_splitter",
        "enable_kaomoji_protection",
        "splitter_enable_kaomoji_protection",
        "switch",
        "response_splitter",
        "enable_kaomoji_protection",
    ),
    (
        "splitter_enable_overflow_return_all",
        "response_splitter",
        "enable_overflow_return_all",
        "splitter_enable_overflow_return_all",
        "switch",
        "response_splitter",
        "enable_overflow_return_all",
    ),
    ("typo_enable", "chinese_typo", "enable", "typo_enable", "switch", "chinese_typo", "enable"),
    ("typo_error_rate", "chinese_typo", "error_rate", "typo_error_rate", "float", "chinese_typo", "error_rate"),
    ("typo_min_freq", "chinese_typo", "min_freq", "typo_min_freq", "int", "chinese_typo", "min_freq"),
    (
        "typo_tone_error_rate",
        "chinese_typo",
        "tone_error_rate",
        "typo_tone_error_rate",
        "float",
        "chinese_typo",
        "tone_error_rate",
    ),
    (
        "typo_word_replace_rate",
        "chinese_typo",
        "word_replace_rate",
        "typo_word_replace_rate",
        "float",
        "chinese_typo",
        "word_replace_rate",
    ),
    ("bot_nickname", "empty_reply_fallback", "fallback_nickname", "bot_nickname", "text", "bot", "nickname"),
)

# 只喂给**宿主复刻路径**（``PostProcessor.process``）的宿主参数：(PostProcessor 形参, ``_cfg`` 短键)。
# 0.12.0 起纠正是否引用完全由权重池决定，这两个宿主开关**不再在插件配置里暴露镜像项**，
# 因此也不参与 ``get_default_config`` 的"宿主现值当默认值"逻辑，只按宿主值构造处理器。
_HOST_ONLY_PROCESSOR_PARAMS: Tuple[Tuple[str, str], ...] = (
    ("typo_enable_correction_quote", "typo_enable_correction_quote"),
    ("typo_correction_quote_probability", "typo_correction_quote_probability"),
)

# 不属于 ``PostProcessor`` 形参、但同样是"宿主已有参数 + 留空跟随宿主"的项：
# (插件配置节, 插件字段, 类型, 宿主 bot_config.toml 的节, 宿主字段名)。
# 目前只有打字速度（它在分段板块里，由插件自己按公式睡，见 ``_typing_speed_override``）。
_HOST_MIRROR_EXTRA_FIELDS: Tuple[Tuple[str, str, str, str, str], ...] = (
    ("response_splitter", "typing_speed", "float", "response_post_process", "typing_speed"),
)

# 宿主 bot_config.toml 里的 节 -> 字段（首次生成插件配置时用来取宿主现值当默认值）。
# 与 ``_POST_PROCESS_CONFIG_KEYS`` 的路径一一对应，只是这里按 TOML 结构读文件。
_HOST_CONFIG_FILE_SECTIONS: Tuple[str, ...] = (
    "response_splitter",
    "chinese_typo",
    "response_post_process",
    "bot",
)
# 「跟随宿主」的写法（开关项三选一，其余空字符串）——与 plugin.py 的约定保持一致。
HOST_SWITCH_FOLLOW = "跟随宿主"
HOST_SWITCH_ON = "开启"
HOST_SWITCH_OFF = "关闭"


def host_config_file_path() -> Optional[Path]:
    """宿主 ``config/bot_config.toml`` 的路径；找不到返回 ``None``。

    两条候选路径：插件目录往上推出来的宿主根（``<root>/plugins/<dir>/``，正常部署）、
    以及 Runner 进程的工作目录（宿主自己也用相对路径读 ``depends-data/``，所以 cwd 通常就是宿主根）。
    """
    candidates = (
        _PROJECT_ROOT / "config" / "bot_config.toml",
        Path.cwd() / "config" / "bot_config.toml",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def read_host_config_values() -> Dict[str, Dict[str, Any]]:
    """读宿主 ``bot_config.toml``（同步、只读、失败返回空 dict）。

    只用于**首次生成插件配置时把宿主现值当默认值**（``get_default_config``）——此时还没有
    事件循环/``ctx``，因此直接读文件而不是走宿主的 ``config.get`` 能力。
    """
    path = host_config_file_path()
    if path is None:
        return {}
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except Exception:
        return {}
    return {section: data[section] for section in _HOST_CONFIG_FILE_SECTIONS if isinstance(data.get(section), dict)}

# 打字模拟的逐字耗时：与宿主 ``src/chat/utils/utils.py: calculate_typing_time`` 的默认参数一致
# （宿主 docstring 里写的 0.2/0.1 是**过时注释**，实际签名默认值是 0.3/0.15——实测 1.2.3）。
_TYPING_CHINESE_SECONDS = 0.3
_TYPING_ENGLISH_SECONDS = 0.15
# 整条只有 1 个汉字时宿主用 3 倍中文时间再额外加 0.3s（回车）。
_TYPING_SINGLE_CHAR_EXTRA_SECONDS = 0.3

# 宿主"抽到纠正建议后错字是否可见"的门控是**硬编码 0.5**（``utils.py``：
# ``if random.random() < 0.5: 发错字句 + 更正段 else: 整句换成正确句``）。
# 插件照抄这个值、**不对外暴露**——错字多久出现一次由宿主【单字错字概率】``error_rate`` 决定，
# 不搞第二个"错字概率"配置项。
# 注意：**另一半**门控（"50% 才给纠正建议"）在接管分支里已被绕过（``PostProcessor.plan``
# 以 ``force_suggestion=True`` 调生成器），否则会出现"打了错字却既不纠正也不撤回"。
_HOST_TYPO_VISIBLE_PROBABILITY = 0.5

# 预分段缓存 TTL：Hook 之间靠 (会话, 全文文本) 关联，超时未消费即丢弃。
_PREPARED_SEGMENT_TTL_SECONDS = 120.0
# 单会话最多保留几轮预分段（防异常堆积）。
_PREPARED_SEGMENT_MAX_ROUNDS = 8
# planner 上下文补齐记录 TTL：记录本轮已发送分段（文本 + 消息 id），供
# maisaka.planner.before_request 只补真正缺失的段；超时未补齐即丢弃。
_PLANNER_SEGMENT_RECORD_TTL_SECONDS = 120.0
# 等待首段回执的宽限；过期只让 planner 放行，
# 避免 before_send abort / 发送管线异常时 planner 白等满 wait_timeout_seconds。
_PLANNER_SEGMENT_FIRST_SEND_GRACE_SECONDS = 5.0
# 待补发登记的 TTL（首段发送失败/被中止时的兜底清理）。
_PENDING_FOLLOW_UP_TTL_SECONDS = 120.0
# 宿主默认只等 30 秒。补发在后台执行，给发送及宿主回执处理多留 15 秒。
# 该时限只管等待 RPC，超时后宿主仍可能继续发送，因此每个动作只提交一次。
_FOLLOW_UP_SEND_RPC_TIMEOUT_MS = 45_000
# send.hybrid 只在 after_build_message 参数中保留原始 processed_plain_text。
# 宿主构建消息时会 strip 该字段，所以这个纯空白标识即使遇到 Hook 超时也不会进正文或历史。
_FOLLOW_UP_ID = "cateye_bpp_follow_up_id"
_FIRST_SEGMENT_ID = "cateye_bpp_first_segment_id"
_UNANCHORED_REPLY = "cateye_bpp_unanchored_reply"
_BITS_TO_SPACE = str.maketrans("01", " \t")
_SPACE_TO_BITS = str.maketrans(" \t", "01")
# planner / 入站消息等待补发的 Hook 超时（内部等待时长由配置决定，这里留出余量）。
#
# 为什么要显式设：`maisaka.planner.before_request` 的 Hook 规格默认超时只有 6000ms，
# 会在等待窗口中间把处理器掐掉（error_policy=SKIP → 静默不再等待）。处理器自带的
# timeout_ms 优先于规格默认值（宿主 HookDispatcher._resolve_timeout_ms），因此这里必须
# 设成比 `[response_splitter] wait_timeout_seconds`（默认 30s）更大的值，并留出补发收尾余量。
#
# 0.14.3 起取 58s（评审意见）：**不高于**系统级 `plugin_runtime.hook_blocking_timeout_sec`
# 的默认值 60s——钩子上限超过宿主全局阻塞超时的默认口径，极端情况下入站处理会被拖过
# 平台自己认的 60s。与此配套，`_wait_for_stream_follow_ups` 把内部等待钳到
# 「本上限 − `_FOLLOW_UP_WAIT_HEADROOM_SECONDS`」：即使用户把 wait_timeout_seconds 调得
# 很大，"超时放行"也永远由自己的等待逻辑干净地结束（带日志），而不是被宿主在 Hook
# 超时处掐断（SKIP 静默放行 + 一条超时告警）。想等更久的用户调大 wait_timeout_seconds
# 即可（钳制点之前的部分都生效）。
_FOLLOW_UP_WAIT_HOOK_TIMEOUT_MS = 58_000
# 内部等待相对钩子超时预留的收尾余量（秒）：覆盖处理器自身的日志等开销，
# 保证等待 + 收尾整体落在钩子超时之内。
_FOLLOW_UP_WAIT_HEADROOM_SECONDS = 3.0


class PostProcessTakeoverMixin:
    """后处理接管（错别字 + 分段 + 多段发送）的 Hook 与状态。"""

    def _init_post_process_takeover(self) -> None:
        """初始化后处理接管状态（由插件类 ``__init__`` 调用）。"""
        # 复刻宿主后处理（错别字 + 分段）的处理器，宿主配置变化时作废重建。
        self._processor: Optional[PostProcessor] = None
        # 生效参数的来源（"宿主" / "插件覆盖"），仅用于日志。
        self._processor_param_sources: Dict[str, str] = {}
        # 预分段按会话、原回复目标和全文匹配。相同目标的回复按登记顺序消费。
        self._prepared_segments: Dict[str, Dict[Tuple[str, str], List[PreparedReply]]] = {}
        # planner 上下文补齐使用会话中最新的一轮。每个发送任务保留自己的记录。
        # 只记录实际发送成功的回复分段（文本 + 平台消息 id），planner 请求前按 items 实际缺失补齐。
        self._planner_segment_records: Dict[str, Dict[str, Any]] = {}
        # 首段从构建到回执使用同一个标识，即使平台替换 message_id 也不靠文本猜配。
        self._pending_follow_ups: Dict[Tuple[str, str], Tuple[float, PreparedReply]] = {}
        # 多段发送：会话 -> 进行中的补发任务（planner / 入站消息等待用）。
        self._stream_follow_up_tasks: Dict[str, Set[asyncio.Task]] = {}
        self._follow_up_intents: Dict[str, FollowUpIntent] = {}


    # 后处理接管（错别字 + 分段）
    # ------------------------------------------------------------------

    def _post_process_takeover_active(self) -> bool:
        """插件接管开关开启、且宿主**不在做错别字处理**时，接管后处理。

        宿主前置条件（二选一，见 ``modules.requirements``）：① 后处理总开关关闭
        （``enable_response_post_process = false``，完整接管）；② 总开关开启但宿主错别字
        关闭（``chinese_typo.enable = false``，错别字接管——接管后置 ``skip_post_process=True``
        跳过宿主处理，分段与错别字都由本插件复刻执行）。另需「丰富回复」门控满足。
        未满足时本模块**完全静默**：不改写 response、不登记补发、不发任何消息。
        """
        if not self._plugin_enabled():
            return False
        if not bool(self.config.response_splitter.takeover):
            return False
        return self._module_available("post_process")

    def _typo_takeover_mode(self) -> bool:
        """是否处于「错别字接管」模式：宿主后处理总开关**开启**、但宿主错别字**关闭**。

        该模式下宿主的原生错别字不会运行，接管的目的就是提供本插件的错字纠正——
        因此处理器构建时 ``typo_enable`` 的「跟随宿主」视为开启（宿主值恒为 False，
        照抄会让接管形同虚设）；插件显式选「关闭」仍然生效（见 ``_get_processor``）。
        """
        return (
            self._cfg.get("response_post_process.enable_response_post_process") is True
            and self._cfg.get("chinese_typo.enable") is False
        )


    # ---- 宿主镜像参数：留空 = 跟随宿主 ----
    # ------------------------------------------------------------------

    def _host_config_value(self, cache_key: str, default: Any = None) -> Any:
        """取宿主配置快照（``self._cfg``）里的值。

        快照里没有这个键（宿主配置读取失败 / 测试用的精简快照）时退回**宿主官方默认值**
        （``_POST_PROCESS_CONFIG_KEYS`` 里登记的），避免把 None 传进处理器。
        """
        value = self._cfg.get(cache_key)
        if value is None:
            return _HOST_CONFIG_DEFAULTS.get(cache_key, default)
        return value

    def _mirror_raw(self, section: str, field: str) -> Any:
        """读插件配置里某个宿主镜像字段的原始值（节不存在时返回 None）。"""
        section_config = getattr(self.config, section, None)
        if section_config is None:
            return None
        return getattr(section_config, field, None)

    @staticmethod
    def _is_follow_host(raw: Any) -> bool:
        """原始值是否表示"跟随宿主"：空字符串 / None / 「跟随宿主」。"""
        if raw is None:
            return True
        text = str(raw).strip()
        return not text or text == HOST_SWITCH_FOLLOW

    def _resolve_mirror(self, section: str, field: str, cache_key: str, kind: str) -> Any:
        """把镜像字段解析成"生效值"：跟随宿主时用宿主值，否则用插件填的值。

        ``kind``：``switch``（三态开关）/ ``int`` / ``float`` / ``text``。
        插件值非法（例如数字框里写了非数字）时回退宿主值，不让一条坏配置把后处理整个打挂。
        """
        host_value = self._host_config_value(cache_key)
        raw = self._mirror_raw(section, field)
        if self._is_follow_host(raw):
            return host_value
        if kind == "switch":
            text = str(raw).strip()
            if text == HOST_SWITCH_ON:
                return True
            if text == HOST_SWITCH_OFF:
                return False
            return host_value
        if kind == "int":
            try:
                return int(float(raw))
            except (TypeError, ValueError):
                return host_value
        if kind == "float":
            try:
                return float(raw)
            except (TypeError, ValueError):
                return host_value
        text = str(raw).strip()
        return text or host_value

    def _mirror_is_explicit(self, section: str, field: str) -> bool:
        """该镜像项是否被用户显式填过（用于日志里标"插件值 / 宿主值"）。"""
        return not self._is_follow_host(self._mirror_raw(section, field))

    def _custom_fallback_prompts(self) -> List[str]:
        """用户自定义的异常兜底提示词列表（``[empty_reply_fallback] fallback_prompts``）。

        去掉空白项、保持配置顺序；空列表（默认）= 只用宿主自带的兜底提示词。
        """
        raw = getattr(self.config.empty_reply_fallback, "fallback_prompts", None)
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    def _get_processor(self) -> PostProcessor:
        """构建（或复用）后处理复刻器；宿主配置或插件配置变化时都会作废重建。

        参数来源：**宿主全局配置快照为基线**，插件配置里"没有表示跟随宿主"的镜像项优先
        （见 ``_resolve_mirror``）——因此"留空 = 跟随宿主"。
        """
        if self._processor is None:
            sources: Dict[str, str] = {}
            params: Dict[str, Any] = {"project_root": _PROJECT_ROOT}
            for param, section, field, cache_key, kind, _hs, _hf in _HOST_MIRROR_FIELDS:
                params[param] = self._resolve_mirror(section, field, cache_key, kind)
                if self._mirror_is_explicit(section, field):
                    sources[param] = "插件值"
            for param, cache_key in _HOST_ONLY_PROCESSOR_PARAMS:
                params[param] = self._host_config_value(cache_key)
            # 「错别字接管」模式（宿主后处理开启、错别字关闭，1.1.1 起）：宿主的错别字
            # 不会运行，接管的目的就是提供本插件的错字纠正——因此 ``typo_enable`` 的
            # 「跟随宿主」在此模式下视为开启（宿主值恒为 False，照抄会让接管形同虚设）；
            # 插件显式选「开启 / 关闭」时仍以插件值为准。
            if self._typo_takeover_mode() and not self._mirror_is_explicit("chinese_typo", "enable"):
                params["typo_enable"] = True
                sources["typo_enable"] = "错别字接管"
            # 自定义异常兜底提示词（1.0.0；1.3.0 起配置位于 [empty_reply_fallback]）：非空时接管路径的兜底改从这份列表随机抽取，
            # 模板里的 {bot_name}/{user_name} 占位符在事件循环侧替换（见
            # handle_before_post_process 的 _format_fallback_placeholders）。
            params["custom_fallback_prompts"] = self._custom_fallback_prompts()
            # 错字黑名单（1.1.3）：名单里的字/词不被错字命中。默认空 = 关闭。
            params["typo_blacklist"] = self._typo_blacklist()
            self._processor = PostProcessor(**params)
            self._processor_param_sources = sources
            self._log_effective_params(params, sources)
        return self._processor

    def _typo_blacklist(self) -> List[str]:
        """错字黑名单（``[chinese_typo] blacklist``）：去掉空白项、保持配置顺序。

        空列表（默认）= 关闭：生成器两个集合都为空，逐字/整词替换行为与宿主完全一致。
        """
        raw = getattr(self.config.chinese_typo, "blacklist", None)
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    def _log_effective_params(self, params: Dict[str, Any], sources: Dict[str, str]) -> None:
        """打印一次生效参数（含"插件值 / 宿主值"来源）。

        错字「改了配置却没反应」这类问题全靠这行定位；宿主值与插件值不一致时额外提示怎么恢复跟随。
        """
        self.ctx.logger.info(
            "后处理接管参数：分段enable=%s(%s) 长度=%s 句数=%s 条数=%s 颜文字=%s 超限全文=%s | "
            "错字enable=%s(%s) error_rate=%s(%s) min_freq=%s(%s) tone=%s(%s) word=%s(%s) "
            "昵称=%s(%s) 黑名单=%s",
            params.get("splitter_enable"),
            sources.get("splitter_enable", "宿主值"),
            params.get("splitter_max_length"),
            params.get("splitter_max_sentence_num"),
            params.get("splitter_max_split_num"),
            params.get("splitter_enable_kaomoji_protection"),
            params.get("splitter_enable_overflow_return_all"),
            params.get("typo_enable"),
            sources.get("typo_enable", "宿主值"),
            params.get("typo_error_rate"),
            sources.get("typo_error_rate", "宿主值"),
            params.get("typo_min_freq"),
            sources.get("typo_min_freq", "宿主值"),
            params.get("typo_tone_error_rate"),
            sources.get("typo_tone_error_rate", "宿主值"),
            params.get("typo_word_replace_rate"),
            sources.get("typo_word_replace_rate", "宿主值"),
            params.get("bot_nickname"),
            sources.get("bot_nickname", "宿主值"),
            ("关闭" if not params.get("typo_blacklist") else "%d 条" % len(params.get("typo_blacklist") or ())),
        )
        if sources:
            stale = [
                f"{section}.{field}（插件={self._mirror_raw(section, field)!r} / 宿主={self._host_config_value(cache_key)!r}）"
                for _param, section, field, cache_key, _kind, _hs, _hf in _HOST_MIRROR_FIELDS
                if self._mirror_is_explicit(section, field)
                and str(self._mirror_raw(section, field)).strip() != str(self._host_config_value(cache_key)).strip()
            ]
            if stale:
                self.ctx.logger.info(
                    "以下宿主镜像项由插件指定、与宿主当前值不同（想重新跟随宿主就把它们清空）：%s",
                    "、".join(stale),
                )

    # ---- 打字模拟（宿主 typing_speed 的插件侧镜像） ----

    def _typing_speed_override(self) -> Optional[float]:
        """``[response_splitter] typing_speed``：``None`` = 交给宿主自己按它的速度模拟。

        留空 = 跟随宿主；填 ``0`` = 不等待；``>0`` = 插件自己按该速度等待。

        **这里两个参数是"插件配置节 / 插件字段"**（``_mirror_raw`` 会 ``getattr(self.config, 节)``）
        ——不是宿主 ``bot_config.toml`` 的节名。0.11.1 及更早误传了宿主节名
        ``response_post_process``（插件里没有这个节）→ ``_mirror_raw`` 恒返回 ``None`` →
        ``_mirror_is_explicit`` 恒 ``False`` → 本函数**恒返回 ``None``**，于是 ``typing_speed``
        成了死字段：用户填什么都不会生效，打字等待永远由宿主按它自己的速度执行
        （0.11.2 修复，回归测试有专门断言）。
        """
        if not self._mirror_is_explicit("response_splitter", "typing_speed"):
            return None
        raw = self._mirror_raw("response_splitter", "typing_speed")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _typing_seconds_for(text: str, speed: float) -> float:
        """按宿主 ``calculate_typing_time`` 的公式算打字耗时（``speed`` 已解析成数值）。

        逐项对齐宿主（1.2.3 ``src/chat/utils/utils.py``）：

        1. 整条**只有 1 个汉字**（``len(strip)==1``）→ 立刻返回 ``0.3×3 + 0.3 = 1.2s``；
           宿主在这个分支里**提前返回**，既不乘 ``typing_speed``、也不做 ``<=0`` 判定——照抄，
           免得"填 0 想关掉打字"在一个字的回复上表现不一致。
        2. 其它情况：中文 0.3s/字、其余 0.15s/字符，求和后乘 ``typing_speed``；
           ``typing_speed <= 0`` → 0（不等待）。

        同一个公式同时服务于"补发分段的打字等待"（``_simulated_typing_seconds``）与
        "撤回反应时间的自动值"（``_recall_delay_seconds``）。
        """
        sample = str(text or "")
        chinese_chars = sum("\u4e00" <= char <= "\u9fff" for char in sample)
        if chinese_chars == 1 and len(sample.strip()) == 1:
            return _TYPING_CHINESE_SECONDS * 3 + _TYPING_SINGLE_CHAR_EXTRA_SECONDS
        if speed <= 0:
            return 0.0
        total = sum(
            _TYPING_CHINESE_SECONDS if "\u4e00" <= char <= "\u9fff" else _TYPING_ENGLISH_SECONDS
            for char in sample
        )
        return total * speed

    def _simulated_typing_seconds(self, text: str) -> Optional[float]:
        """补发分段的打字等待秒数；``typing_speed`` 留空（跟随宿主）时返回 ``None``。"""
        speed = self._typing_speed_override()
        if speed is None:
            return None
        return self._typing_seconds_for(text, speed)

    def _recall_delay_seconds(self, recalled_text: str) -> float:
        """撤回前的"反应时间"（秒）——``[chinese_typo] recall_delay_seconds``。

        留空（默认）= 按打字速度自动：拿**被撤回那条消息的正文**按宿主的打字公式算一遍
        （中文 0.3s/字、其它 0.15s/字符，乘生效的打字速度），也就是"刚把这句话打完才发现
        打错了"的时间量级——长句等得久、短句几乎立刻撤回。
        填了数字（支持小数点）就用它（``0`` = 立刻撤回）；填了非法值退回自动并记一条 warning。
        """
        raw = getattr(self.config.chinese_typo, "recall_delay_seconds", "")
        text = str(raw).strip()
        if text:
            try:
                return max(0.0, float(text))
            except (TypeError, ValueError):
                self.ctx.logger.warning("撤回反应时间 %r 不是数字，改为按打字速度自动决定", raw)
        speed = self._typing_speed_override()
        if speed is None:
            try:
                speed = float(self._host_config_value("typing_speed"))
            except (TypeError, ValueError):
                speed = 1.0
        return max(0.0, self._typing_seconds_for(recalled_text, speed))

    async def _format_fallback_placeholders(
        self, segments: List[ProcessedResponseSegment], reply_message_id: str
    ) -> List[ProcessedResponseSegment]:
        """替换自定义兜底提示词里的 ``{bot_name}`` / ``{user_name}`` 占位符（1.0.0）。

        只在兜底早退路径（``last_stats["early"]=True``：回复过长 / 句子太多）被调用；
        段文本里没有占位符时原样返回（宿主自带兜底提示词与「呃呃」都不含占位符）。

        * ``{bot_name}`` = 「兜底回复的自称」的生效值（插件镜像 → 宿主 ``bot.nickname`` → 代码兜底）；
        * ``{user_name}`` = **要回复的那条消息的发送者名称**（群名片优先、昵称兜底），
          经 ``_lookup_reply_target`` 带 TTL 缓存查询——与引用回复接管共用同一份缓存，
          通常不产生额外 RPC；查不到（不是回复 / 消息被清理）时替换为空串。

        占位替换需要异步查询，而分段计算跑在工作线程里（``asyncio.to_thread``），
        因此模板在 ``PostProcessor`` 里原样抽出、替换放在事件循环侧的这里做。
        """
        if not any(
            "{bot_name}" in segment.text or "{user_name}" in segment.text for segment in segments
        ):
            return segments
        bot_name = str(
            self._resolve_mirror("empty_reply_fallback", "fallback_nickname", "bot_nickname", "text") or ""
        )
        user_name = ""
        if reply_message_id and any("{user_name}" in segment.text for segment in segments):
            try:
                target = await self._lookup_reply_target(reply_message_id)
            except Exception as exc:
                target = None
                self.ctx.logger.debug("兜底占位符 user_name 查询失败（按空处理）: %s", exc)
            if target is not None:
                user_name = str(target[2] or "").strip() or str(target[1] or "").strip()
        formatted: List[ProcessedResponseSegment] = []
        for segment in segments:
            text = segment.text
            if "{bot_name}" in text or "{user_name}" in text:
                text = text.replace("{bot_name}", bot_name).replace("{user_name}", user_name)
                segment = ProcessedResponseSegment(
                    text=text,
                    quote_previous=segment.quote_previous,
                    separator=segment.separator,
                )
            formatted.append(segment)
        self.ctx.logger.info(
            "异常兜底提示词命中：已从自定义列表随机抽取并完成占位替换（bot_name=%r user_name=%r）",
            bot_name,
            user_name,
        )
        return formatted


    async def _warmup_processor(self) -> None:
        """预热高开销数据（拼音映射 / 字频 / jieba 词频），避免首次接管回复卡顿。"""
        try:
            await asyncio.to_thread(self._get_processor().warmup)
            self.ctx.logger.info("后处理接管数据预热完成")
        except Exception as exc:  # 预热失败不影响后续懒加载
            self.ctx.logger.warning("后处理接管数据预热失败（将在首次使用时重试）: %s", exc)


    def _multi_message_active(self) -> bool:
        """"每段一条消息"是否生效。

        宿主的分段本来就是"每段一条消息"，插件接管后照此复刻——因此**没有**单独的开关：
        只要后处理接管生效，分段就是多条消息（复刻宿主行为）。
        """
        return self._post_process_takeover_active()

    def _choose_correction_mode(
        self, segment_index: int, suggestion: str, *, allow_recall: bool
    ) -> Optional[str]:
        """按权重抽取某一段错字的纠正方式（跑在处理线程里，只用 random + 配置）。

        权重池：``direct``（直接发送）/``quote``（引用带错字的消息）/``recall``（撤回后重发
        修正句）/``none``（不纠正）。权重全为 0 时按"不纠正"处理（= 完全关掉纠正）。
        "最后纠正"**不在池子里**——它是"把抽到的纠正推迟到本轮分段发完之后"，由
        ``_should_defer_correction`` 单独决定（见 ``[chinese_typo_weights]`` 的 last_correction_*）。

        已知私聊时把撤回权重计入直接更正，保留各轮抽到更正的概率。

        **本方法在工作线程里执行，不要碰 ``self.ctx``**：日志在回到事件循环后再统一记录
        （见 ``handle_before_post_process`` 里的"错字纠正方式"日志）。
        """
        _ = (segment_index, suggestion)  # 仅为可读的签名保留，线程内不做任何 I/O
        cfg = self.config.chinese_typo_weights
        weights = {
            "direct": max(0, int(getattr(cfg, "weight_correction_direct", 0))),
            "quote": max(0, int(getattr(cfg, "weight_correction_quote", 0))),
            "recall": max(0, int(getattr(cfg, "weight_correction_recall", 0))),
            "none": max(0, int(getattr(cfg, "weight_correction_none", 0))),
        }
        if not allow_recall:
            weights["direct"] += weights.pop("recall")
        styles = [name for name, weight in weights.items() if weight > 0]
        if not styles:
            return "none"
        return random.choices(styles, weights=[float(weights[name]) for name in styles], k=1)[0]

    def _should_defer_correction(self, segment_index: int) -> bool:
        """该处纠正是否推迟到本轮全部分段发完之后（"最后纠正"，跑在处理线程里）。

        ``[chinese_typo_weights] last_correction_enabled`` 是总开关；打开后按
        ``last_correction_probability``（0~1）抽取：抽中才推迟，否则紧跟该段之后执行。
        **推迟只改时机**——纠正方式仍是 ``_choose_correction_mode`` 抽到的那个
        （直接补发 / 引用纠正 / 撤回重发），不再强制引用；抽到"不纠正"时什么都不会发生。
        """
        _ = segment_index  # 仅为可读的签名保留，线程内不做任何 I/O
        cfg = self.config.chinese_typo_weights
        if not bool(getattr(cfg, "last_correction_enabled", False)):
            return False
        probability = self._defer_probability(cfg)
        if probability <= 0.0:
            return False
        if probability >= 1.0:
            return True
        return random.random() < probability

    @staticmethod
    def _defer_probability(cfg: Any) -> float:
        """解析「最后纠正概率」并夹到 0~1（非法值按 0 = 从不推迟处理）。"""
        try:
            value = float(getattr(cfg, "last_correction_probability", 1.0))
        except (TypeError, ValueError):
            return 0.0
        return min(1.0, max(0.0, value))

    def _yield_to_other_plugins(self, kwargs: Dict[str, Any]) -> bool:
        """本轮是否已被其它插件认领（例如智能分段插件），是则让位。

        判据只用 ``skip_post_process``：它在本 Hook 里是"另一个阻塞处理器已经接管本次
        文本后处理"的通用信号——智能分段插件命中预分段缓存时正是这么做的
        （``smart_segmentation_preserve_prepared_response``）。此时本插件**不改写 response、
        不登记补发**，分段交给对方；而回复方式（@/引用）、文本规则、表情包等功能不受影响，
        仍由本插件的其它 Hook 完成。

        ``[response_splitter] yield_to_other_plugins=false`` 可关掉让位（本插件无条件优先）。
        """
        if not bool(getattr(self.config.response_splitter, "yield_to_other_plugins", True)):
            return False
        if not bool(kwargs.get("skip_post_process")):
            return False
        self.ctx.logger.info(
            "后处理接管：本轮已被其它插件认领（skip_post_process=True），本次让位给它的分段逻辑"
        )
        return True

    @staticmethod
    def _text_key(text: Any) -> str:
        """把文本归一成缓存键（抹平空白差异，避免宿主 strip/拼接导致失配）。"""
        return "".join(str(text or "").split())

    @staticmethod
    def _extract_outbound_text(message: Dict[str, Any], *, fallback: bool = True) -> str:
        """从出站消息载荷里取出纯文本（多段文本组件按顺序拼接）。"""
        components = message.get("raw_message")
        if isinstance(components, list):
            parts = [
                str(component.get("data") or "")
                for component in components
                if isinstance(component, dict) and str(component.get("type") or "") == "text"
            ]
            text = "".join(parts).strip()
            if text:
                return text
        return str(message.get("processed_plain_text") or "").strip() if fallback else ""


    # ---- 预分段缓存（Hook 1 写入，Hook 2 消费） ----

    def _store_prepared_segments(
        self,
        session_id: str,
        full_text: str,
        first_text: str,
        items: List["FollowUpItem"],
        *,
        target_id: str,
    ) -> bool:
        """登记原回复目标和全文对应的首段及其后续动作。"""
        full_key = self._text_key(full_text) if target_id else full_text.strip()
        first = str(first_text or "").strip()
        if not session_id or not full_key or not first or (target_id and not items):
            return False
        bucket = self._prepared_segments.setdefault(session_id, {})
        now = time.monotonic()
        for stale_key, entries in list(bucket.items()):
            entries[:] = [
                entry for entry in entries if now - entry.created_at <= _PREPARED_SEGMENT_TTL_SECONDS
            ]
            if not entries:
                bucket.pop(stale_key, None)
        record = {
            "sent": [],
            "done": asyncio.Event(),
            "rewritten": False,
            "task": None,
            "created_at": now,
        }
        self._planner_segment_records[session_id] = record
        bucket.setdefault((target_id, full_key), []).append(
            PreparedReply(now, first, list(items), record, unanchored=not target_id)
        )
        while sum(len(entries) for entries in bucket.values()) > _PREPARED_SEGMENT_MAX_ROUNDS:
            oldest_key = min(bucket, key=lambda candidate: bucket[candidate][0].created_at)
            bucket[oldest_key].pop(0)
            if not bucket[oldest_key]:
                bucket.pop(oldest_key, None)
        return True


    def _take_prepared_segments(
        self, session_id: str, text: str, *, target_id: str
    ) -> Optional[PreparedReply]:
        """仅消费同一回复目标和全文的预分段。"""
        bucket = self._prepared_segments.get(session_id)
        if not bucket:
            return None
        key = (target_id, self._text_key(text) if target_id else text.strip())
        entries = bucket.get(key)
        if not entries:
            return None
        taken = None
        now = time.monotonic()
        while entries:
            entry = entries.pop(0)
            if now - entry.created_at <= _PREPARED_SEGMENT_TTL_SECONDS:
                taken = entry
                break
        if not entries:
            bucket.pop(key, None)
        if not bucket:
            self._prepared_segments.pop(session_id, None)
        return taken


    # ---- 待补发登记（Hook 2 写入，Hook 3 消费） ----
    def _register_pending_follow_up(
        self, session_id: str, send_id: str, reply: PreparedReply
    ) -> None:
        now = time.monotonic()
        for stale_key, (registered_at, stale_reply) in list(self._pending_follow_ups.items()):
            if now - registered_at > _PENDING_FOLLOW_UP_TTL_SECONDS:
                self._pending_follow_ups.pop(stale_key, None)
                stale_reply.planner_record["done"].set()
        self._pending_follow_ups[(session_id, send_id)] = (now, reply)


    # ---- 补发任务跟踪与等待 ----

    def _track_follow_up_task(self, session_id: str, task: asyncio.Task) -> None:
        self._stream_follow_up_tasks.setdefault(session_id, set()).add(task)

        def _on_done(completed: asyncio.Task) -> None:
            bucket = self._stream_follow_up_tasks.get(session_id)
            if bucket is None:
                return
            bucket.discard(completed)
            if not bucket:
                self._stream_follow_up_tasks.pop(session_id, None)

        task.add_done_callback(_on_done)


    async def _wait_for_stream_follow_ups(self, session_id: str) -> int:
        """等待该会话的补发任务结束（带超时），返回等待过的任务数。

        等待时长被钳在「Hook 超时 − 收尾余量」（0.14.3）：两个等待 Hook 的 `timeout_ms`
        上限是 58s，用户把 `wait_timeout_seconds` 调得再大，实际等待也不会越过钳制点——
        保证 blocking 处理器**永远**由自己的等待超时放行（这里返回后照常打日志），
        而不是被宿主在 Hook 超时处掐断（SKIP 静默放行 + 超时告警）。
        """
        if not session_id:
            return 0
        tasks = [task for task in self._stream_follow_up_tasks.get(session_id, set()) if not task.done()]
        if not tasks:
            return 0
        timeout = max(float(self.config.response_splitter.wait_timeout_seconds), 0.0)
        timeout = min(
            timeout,
            _FOLLOW_UP_WAIT_HOOK_TIMEOUT_MS / 1000.0 - _FOLLOW_UP_WAIT_HEADROOM_SECONDS,
        )
        if timeout <= 0:
            return 0
        try:
            await asyncio.wait(tasks, timeout=timeout)
        except Exception as exc:  # 等待失败不应影响宿主主流程
            self.ctx.logger.debug("等待补发任务异常: %s", exc)
        return len(tasks)


    # ---- planner 上下文补齐（按实际缺失分段，不盲目补） ----
    # ------------------------------------------------------------------

    def _get_planner_segment_record(self, session_id: str) -> Optional[Dict[str, Any]]:
        """取该会话最新且未过期的分段记录。"""
        record = self._planner_segment_records.get(session_id)
        if record is None:
            return None
        if time.monotonic() - float(record.get("created_at", 0.0)) > _PLANNER_SEGMENT_RECORD_TTL_SECONDS:
            self._planner_segment_records.pop(session_id, None)
            return None
        return record

    @staticmethod
    def _collect_self_message_items(items: List[Any]) -> List[Tuple[int, str, str]]:
        """收集 planner items 里 bot 自身消息：(item_index, msg_id, 正文 key)。"""
        collected: List[Tuple[int, str, str]] = []
        for item_index, item in enumerate(items):
            if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
                continue
            text = "".join(
                str(part.get("text") or "")
                for part in item.get("parts") or []
                if isinstance(part, dict) and part.get("type") == "text"
            )
            # 只认 planner 消息前缀标签里的 is_self_message，正文里恰好出现该字符串不算。
            prefix_match = re.match(r"^\s*<message\b[^>]*>", text)
            if prefix_match is None or 'is_self_message="true"' not in prefix_match.group(0):
                continue
            # 只认消息前缀标签里的 msg_id，正文里恰好出现 msg_id="..." 不算。
            match = re.search(r'\bmsg_id="([^"]*)"', prefix_match.group(0))
            message_id = match.group(1) if match and match.group(1) else ""
            # 去掉宿主 planner 前缀后再比正文；避免把工具结果里的全文当成“消息已存在”。
            content = text[prefix_match.end():]
            content = re.sub(r"\s*</message>\s*$", "", content, count=1)
            content_key = "".join(content.split())
            collected.append((item_index, message_id, content_key))
        return collected

    def _inject_missing_planner_segments(
        self,
        session_id: str,
        kwargs: Dict[str, Any],
        *,
        record: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """只把实际发送成功、但当前 items 里确实缺失的回复分段追加进请求。

        存在性优先按平台 ``msg_id`` 判断，缺 id 时退回自消息正文比对；因此
        “三句只缺最后一句”只会补最后一句，已同步进上下文的段不会重复。

        ``record`` 由调用方传入等待过的那一轮；如果等待期间同会话已经开始新一轮
        （dict 里的对象已不是它），本次请求不做跨轮注入。
        """
        current = self._planner_segment_records.get(session_id)
        if record is not None and current is not record:
            return None
        if record is None:
            record = current
        if record is None:
            return None
        if not record.get("rewritten"):
            # 全文没有被截回（说明走了单条兜底/缓存未命中）：不按段补。
            if current is record:
                self._planner_segment_records.pop(session_id, None)
            return None
        if not record.get("sent"):
            # 首段可能还在发送中（after_send 尚未跑完）：wait_timeout_seconds=0 时
            # 不要立刻丢弃记录，留给下一轮 planner 再判断；只有已确认失败才移除。
            if record["done"].is_set() and current is record:
                self._planner_segment_records.pop(session_id, None)
            return None
        items = kwargs.get("items")
        if not isinstance(items, list):
            return None
        self_items = self._collect_self_message_items(items)
        used_item_indexes: Set[int] = set()
        present_segments: Dict[int, int] = {}
        missing: List[Tuple[int, str, str]] = []
        for segment_index, (text, message_id) in enumerate(record["sent"]):
            text_key = self._text_key(text)
            matched_item_index: Optional[int] = None
            if message_id:
                for item_index, item_message_id, _item_text_key in self_items:
                    if item_index not in used_item_indexes and item_message_id == message_id:
                        matched_item_index = item_index
                        break
            else:
                for item_index, _item_message_id, item_text_key in self_items:
                    if item_index not in used_item_indexes and item_text_key == text_key:
                        matched_item_index = item_index
                        break
            if matched_item_index is None:
                missing.append((segment_index, text, message_id))
                continue
            used_item_indexes.add(matched_item_index)
            present_segments[segment_index] = matched_item_index
        if not missing:
            # 只有补发全部结束才移除记录；wait_timeout_seconds=0 / 等待超时时，
            # 当前已发送的段可能只是前 k 段，后面还会继续发送。
            if record["done"].is_set() and current is record:
                self._planner_segment_records.pop(session_id, None)
                self.ctx.logger.debug("planner 上下文已含全部回复分段，移除补齐记录（会话 %s）", session_id)
            return None
        try:
            bot_name = str(
                self._resolve_mirror("empty_reply_fallback", "fallback_nickname", "bot_nickname", "text") or ""
            ).strip()
        except Exception:
            bot_name = ""
        safe_bot_name = (bot_name or "bot").replace('"', "&quot;")
        now = datetime.now()

        def _build_missing_item(text: str, message_id: str) -> Dict[str, Any]:
            attrs: List[str] = []
            safe_message_id = str(message_id or "").replace('"', "&quot;")
            if safe_message_id:
                attrs.append(f'msg_id="{safe_message_id}"')
            attrs.append(f'time="{time.strftime("%H:%M:%S")}"')
            attrs.append(f'user="{safe_bot_name}"')
            attrs.append('is_self_message="true"')
            return {
                "item_type": "UserMessageItem",
                "meta": {
                    "item_id": uuid.uuid4().hex,
                    "logical_turn_id": None,
                    "timestamp": now.isoformat(),
                },
                "parts": [
                    {
                        "type": "text",
                        "text": f"<message {' '.join(attrs)}>\n{text}",
                    }
                ],
            }

        missing_items = [
            _build_missing_item(text, message_id) for _index, text, message_id in missing
        ]
        new_items: List[Any] = []
        if present_segments:
            # 保持回复段顺序：缺中间段 → 插到“下一个已存在段”之前；缺尾段 →
            # 插到最后一个已存在段之后。这样不会跑到 system-reminder / 人物画像 /
            # 时间 / 分析指令这些尾部合成项之后，也不会插到已存在的中段前面。
            insert_before: Dict[int, List[Dict[str, Any]]] = {}
            insert_after_last: List[Dict[str, Any]] = []
            last_present_item_index = max(present_segments.values())
            for (segment_index, _text, _message_id), missing_item in zip(missing, missing_items):
                later_present = [
                    (present_segment_index, present_item_index)
                    for present_segment_index, present_item_index in present_segments.items()
                    if present_segment_index > segment_index
                ]
                if later_present:
                    next_present_item_index = min(later_present, key=lambda pair: pair[0])[1]
                    insert_before.setdefault(next_present_item_index, []).append(missing_item)
                else:
                    insert_after_last.append(missing_item)
            for item_index, item in enumerate(items):
                new_items.extend(insert_before.get(item_index, []))
                new_items.append(item)
                if item_index == last_present_item_index:
                    new_items.extend(insert_after_last)
        else:
            # 本轮所有段都缺失：退到“最后一条 bot 自身消息之后”（通常在聊天历史
            # 末尾、尾部合成项之前）；没有自身消息就插在末尾的分析指令之前。
            last_self_item_index = max((index for index, _, _ in self_items), default=None)
            instruction_index: Optional[int] = None
            if last_self_item_index is None:
                for item_index in range(len(items) - 1, -1, -1):
                    item = items[item_index]
                    if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
                        continue
                    text = "".join(
                        str(part.get("text") or "")
                        for part in item.get("parts") or []
                        if isinstance(part, dict) and part.get("type") == "text"
                    )
                    if "你需要输出对" in text or "思考是否进行工具调用" in text:
                        instruction_index = item_index
                        break
            if last_self_item_index is not None:
                for item_index, item in enumerate(items):
                    new_items.append(item)
                    if item_index == last_self_item_index:
                        new_items.extend(missing_items)
            elif instruction_index is not None:
                for item_index, item in enumerate(items):
                    if item_index == instruction_index:
                        new_items.extend(missing_items)
                    new_items.append(item)
            else:
                new_items = list(items) + missing_items
        self.ctx.logger.info(
            "planner 上下文补齐缺失分段：会话 %s，缺失 %s 段，已按回复段顺序插入",
            session_id,
            len(missing),
        )
        modified = dict(kwargs)
        modified["items"] = new_items
        return modified

    @staticmethod
    def _follow_up_id(message: Dict[str, Any]) -> str:
        value = outbound_metadata(message).get(_FOLLOW_UP_ID)
        return value if isinstance(value, str) else ""

    @staticmethod
    def _is_unanchored_reply(message: Dict[str, Any]) -> bool:
        return outbound_metadata(message).get(_UNANCHORED_REPLY) is True

    def _remove_unanchored_addressing(self, kwargs: Dict[str, Any], message: Dict[str, Any]) -> bool:
        """清掉直发消息里实际的 @、引用组件和宿主引用参数。"""
        changed = bool(self._strip_at_components(message))
        components = message.get("raw_message")
        if isinstance(components, list):
            cleaned = [
                component for component in components
                if not isinstance(component, dict) or component.get("type") != "reply"
            ]
            if len(cleaned) != len(components):
                message["raw_message"] = cleaned
                changed = True
        if message.pop("reply_to", None):
            changed = True
        if kwargs.get("set_reply"):
            kwargs["set_reply"] = False
            changed = True
        if kwargs.get("reply_message_id"):
            kwargs["reply_message_id"] = ""
            changed = True
        if changed:
            message["processed_plain_text"] = self._extract_outbound_text(message, fallback=False)
        return changed

    def _follow_up_from_build(self, session_id: str, processed: str) -> str:
        suffix = processed[len(processed.rstrip(" \t")):]
        if len(suffix) != 128:
            return ""
        send_id = f"{int(suffix.translate(_SPACE_TO_BITS), 2):032x}"
        intent = self._follow_up_intents.get(send_id)
        return send_id if intent is not None and intent.session_id == session_id else ""


    # ---- 补发执行 ----

    async def _send_follow_up_text(
        self,
        session_id: str,
        text: str,
        *,
        typing: bool,
        quote_target: str = "",
        at_target: str = "",
        at_user_id: str = "",
        at_nickname: str = "",
        at_cardname: str = "",
        sync_history: bool = True,
        unanchored: bool = False,
    ) -> FollowUpSendResult:
        """补发一条文本消息；``quote_target`` / ``at_target`` 非空时在 before_send 注入引用 / @。

        ``sync_history=False``：不把这条件写入 Maisaka 对话历史——用于"发出后马上会被撤回"
        的消息，让 bot 自己的上下文里只留下修正句（撤回后重发的语义）。

        打字等待：``[response_splitter] typing_speed`` **留空**时按老规矩把 ``typing=True``
        交给宿主（宿主按它自己的 ``typing_speed`` 睡）；**填了值**时改由本插件按同一公式自己睡，
        并传 ``typing=False`` 避免宿主再按它自己的速度重复等待。
        """
        normalized = str(text or "").strip()
        if not normalized:
            return FollowUpSendResult(FollowUpSendStatus.SKIPPED)
        # 提前识别预期过滤，使其不进入发送及回执判定。
        if self._text_rules_would_drop(normalized):
            self.ctx.logger.info(
                "出站文本规则：分段命中整条过滤，跳过补发: %r", normalized[:40]
            )
            return FollowUpSendResult(FollowUpSendStatus.SKIPPED)
        simulated = self._simulated_typing_seconds(normalized) if typing else None
        if simulated is not None and simulated > 0:
            await asyncio.sleep(simulated)
        nonce = uuid.uuid4()
        send_id = nonce.hex
        suffix = f"{nonce.int:0128b}".translate(_BITS_TO_SPACE)
        self._follow_up_intents[send_id] = FollowUpIntent(
            session_id, quote_target, at_target, at_user_id, at_nickname, at_cardname,
            unanchored=unanchored,
        )
        try:
            try:
                details = await self.ctx.send.hybrid(
                    [{"type": "text", "data": normalized}],
                    session_id,
                    processed_plain_text=normalized + suffix,
                    typing=bool(typing) and simulated is None,
                    sync_to_maisaka_history=bool(sync_history),
                    maisaka_source_kind="guided_reply",
                    return_details=True,
                    rpc_timeout_ms=_FOLLOW_UP_SEND_RPC_TIMEOUT_MS,
                )
            except Exception as exc:
                self.ctx.logger.warning(
                    "补发未确认送达（会话 %s）: %s",
                    session_id, exc,
                )
                return FollowUpSendResult(FollowUpSendStatus.UNCONFIRMED)
            if details is True:
                return FollowUpSendResult(FollowUpSendStatus.SENT)
            if isinstance(details, dict) and details.get("sent") is True:
                raw_id = details.get("message_id")
                message_id = raw_id.strip() if isinstance(raw_id, str) else ""
                return FollowUpSendResult(FollowUpSendStatus.SENT, message_id)
            # SDK 也会把宿主内部异常折成 sent=False，不能用它证明未实际投递。
            self.ctx.logger.warning("补发未收到成功回执（会话 %s）", session_id)
            return FollowUpSendResult(FollowUpSendStatus.UNCONFIRMED)
        finally:
            self._follow_up_intents.pop(send_id, None)


    async def _recall_message(self, message_id: str) -> bool:
        """撤回一条已发出的消息（走适配器公开 API，能力名 ``api.call``）。

        两个官方适配器都暴露 ``adapter.napcat.message.delete_msg``（SnowLuma 提供 napcat
        兼容面），参数直接展开；再兜一层通用 action 入口，适配仅支持 ``action.call`` 的适配器。
        返回是否确认撤回成功。

        **``api.call`` 必须在 ``_manifest.json`` 的 ``capabilities`` 里声明**：宿主
        ``plugin_runtime/host/authorization.py`` 的免声明白名单**只有**
        ``api.replace_dynamic`` 一项，其余能力一律按 manifest 声明的令牌放行——漏声明时
        宿主返回 ``E_CAPABILITY_DENIED``，本函数会恒返回 ``False``，"撤回重发"纠错分支
        就会静默退化成 ``recall_fallback``（0.11.2 修的就是这个漏声明）。
        """
        normalized = str(message_id or "").strip()
        if not normalized:
            return False
        attempts: Tuple[Tuple[str, Dict[str, Any]], ...] = (
            ("adapter.napcat.message.delete_msg", {"message_id": normalized}),
            ("adapter.napcat.action.call", {"action_name": "delete_msg", "params": {"message_id": normalized}}),
        )
        for api_name, kwargs in attempts:
            try:
                resp = await self.ctx.api.call(api_name, **kwargs)
            except Exception as exc:  # 能力被拒 / API 不存在 → 换下一个入口
                self.ctx.logger.debug("撤回消息走 %s 失败: %s", api_name, exc)
                continue
            if isinstance(resp, dict) and resp.get("success") is False:
                self.ctx.logger.debug("撤回消息走 %s 被拒: %r", api_name, resp.get("error"))
                continue
            if isinstance(resp, dict):
                retcode = resp.get("retcode")
                status = str(resp.get("status") or "").lower()
                if retcode not in (None, 0):
                    self.ctx.logger.debug("撤回消息走 %s 返回 retcode=%s", api_name, retcode)
                    continue
                if status and status not in {"ok", "success", "async"}:
                    self.ctx.logger.debug("撤回消息走 %s 返回 status=%s", api_name, status)
                    continue
            self.ctx.logger.info("已撤回消息 %s（%s）", normalized, api_name)
            return True
        self.ctx.logger.warning("撤回消息 %s 失败（适配器不支持或已超时）", normalized)
        return False


    async def _run_follow_up_items(
        self,
        session_id: str,
        items: List["FollowUpItem"],
        *,
        typing: bool,
        previous_message_id: str,
        first_text: str = "",
        planner_record: Optional[Dict[str, Any]] = None,
        first_style: Optional[Dict[str, str]] = None,
        unanchored: bool = False,
        allow_recall: bool = False,
    ) -> None:
        """按顺序执行补发动作序列（分段 / 直接纠正 / 引用纠正 / 撤回重发 / 最后纠正）。

        顺序即语义：某一段的纠正动作紧跟该段之后执行，**早于后续分段**；"最后纠正"
        排在序列末尾——按抽到的纠正方式引用它所属那一段的消息（``quote_slot``）、
        撤回它所属那一段的消息（``recall_slot``）、或直接补发（``direct``）。

        ``first_text``：**首段**（由宿主发出）的正文。撤回反应时间留空时按"被撤回那条的
        打字时长"自动决定，首段的正文只能由调用方传进来（``handle_after_build_message``
        登记的那份）。

        撤回根据首段送达回执里的会话类型判定。规划时尚未知会话类型的撤回动作，
        若在私聊送达，则直接发更正，保留原段及其历史。
        """
        previous_id = previous_message_id
        # 键存在表示该段已确认送达。值为空仅表示宿主未提供可引用的消息 ID。
        sent_slots: Dict[int, str] = {0: previous_message_id}
        sent_slot_texts: Dict[int, str] = {0: first_text}
        # 修正句命中整条过滤时保留原段及其聊天历史。
        recalled_slots = {
            item.recall_slot for item in items
            if allow_recall and item.kind == "recall"
            and item.text.strip()
            and not self._text_rules_would_drop(item.text.strip())
        }
        confirmed = 0
        for position, item in enumerate(items, start=1):
            if item.kind == "recall" and not allow_recall:
                if item.recall_slot not in sent_slots:
                    continue
                item = FollowUpItem(kind="text", text=item.correction_text or item.text)
            if item.kind == "recall":
                slot = item.recall_slot
                if slot not in recalled_slots or slot not in sent_slots:
                    self.ctx.logger.debug("撤回重发跳过：修正句已过滤或第 %s 段未发送", slot)
                    continue
                target_id = sent_slots[slot]
                target_text = sent_slot_texts[slot]
                fallback = str(getattr(self.config.chinese_typo, "recall_fallback", "quote") or "quote")
                # 撤回的是本轮首段（头信息）时，修正句要恢复原首段的引用/@；
                # 引用对象是原回复目标消息，而不是刚被撤回的 bot 消息。
                head_style = first_style if slot == 0 and first_style and not unanchored else {}
                preserved_quote_target = head_style.get("quote_target", "")
                preserved_at_user_id = head_style.get("at_user_id", "")
                preserved_at_nickname = head_style.get("at_user_nickname", "")
                preserved_at_cardname = head_style.get("at_user_cardname", "")
                if not target_id:
                    self.ctx.logger.warning(
                        "撤回重发：找不到要撤回的消息 id（slot=%s），改为直接发送修正句: %r",
                        slot,
                        item.text[:40],
                    )
                    result = await self._send_follow_up_text(
                        session_id,
                        item.text,
                        typing=typing,
                        quote_target=preserved_quote_target,
                        at_target=preserved_at_user_id,
                        at_user_id=preserved_at_user_id,
                        at_nickname=preserved_at_nickname,
                        at_cardname=preserved_at_cardname,
                        unanchored=unanchored,
                    )
                else:
                    delay = self._recall_delay_seconds(target_text or item.text)
                    if delay > 0:
                        self.ctx.logger.info(
                            "撤回反应时间 %.2f 秒后撤回消息 %s（会话 %s）", delay, target_id, session_id
                        )
                        await asyncio.sleep(delay)
                    # 等待反应时间期间，文本规则可能热更新。
                    if self._text_rules_would_drop(item.text.strip()):
                        self.ctx.logger.info("修正句命中文本规则，保留第 %s 段", slot)
                        continue
                    recalled = await self._recall_message(target_id)
                    if recalled:
                        sent_slots.pop(slot)
                        if previous_id == target_id:
                            previous_id = ""
                        quote_target = preserved_quote_target
                    elif fallback == "none":
                        self.ctx.logger.warning("撤回失败且配置为不兜底，跳过该条纠正: %r", item.text[:40])
                        continue
                    else:
                        # 兜底：quote = 改成引用那条错字消息发送；direct = 直接发送。
                        # 头部撤回且原首段有引用时，引用对象必须是原回复目标，不是 bot 消息。
                        quote_target = (
                            preserved_quote_target
                            or (target_id if fallback == "quote" and not unanchored else "")
                        )
                    result = await self._send_follow_up_text(
                        session_id,
                        item.text,
                        typing=typing,
                        quote_target=quote_target,
                        at_target=preserved_at_user_id,
                        at_user_id=preserved_at_user_id,
                        at_nickname=preserved_at_nickname,
                        at_cardname=preserved_at_cardname,
                        unanchored=unanchored,
                    )
                # 撤回成功或走兜底后，补齐记录应包含实际上发送的修正句。
                sync_history = True
            else:
                slot = item.slot
                sync_history = slot not in recalled_slots
                if item.quote_slot >= 0:
                    if item.quote_slot not in sent_slots:
                        self.ctx.logger.debug("第 %s 段未发送，跳过引用纠正", item.quote_slot)
                        continue
                    quote_target = "" if unanchored else sent_slots[item.quote_slot]
                    if not quote_target and not unanchored:
                        self.ctx.logger.debug(
                            "第 %s 段没有可引用的消息 id，直接发送纠正", item.quote_slot
                        )
                elif item.quote_previous and not unanchored:
                    quote_target = previous_id
                else:
                    quote_target = ""
                result = await self._send_follow_up_text(
                    session_id,
                    item.text,
                    typing=typing,
                    quote_target=quote_target,
                    sync_history=sync_history,
                    unanchored=unanchored,
                )
            if result.status is FollowUpSendStatus.SKIPPED:
                continue
            if result.status is FollowUpSendStatus.UNCONFIRMED:
                # 原请求可能稍后抵达。停止本轮，避免顺序倒置或把纠正应用到错误的消息。
                self.ctx.logger.warning(
                    "第 %s 条补发未确认送达，本轮已确认 %s 条，停止后续 %s 个动作（会话 %s）",
                    position, confirmed, len(items) - position, session_id,
                )
                return
            confirmed += 1
            previous_id = result.message_id
            if slot >= 0:
                sent_slots[slot] = result.message_id
                sent_slot_texts[slot] = item.text
            # 缺平台 ID 时仍可按正文匹配历史；引用和撤回不能使用上一条的 ID。
            if planner_record is not None and sync_history:
                planner_record["sent"].append((item.text.strip(), result.message_id))

        self.ctx.logger.info("补发完成：确认送达 %s/%s 条（会话 %s）", confirmed, len(items), session_id)

    # ------------------------------------------------------------------
    # Hook 2a：后处理接管（错别字 + 分段，blocking）
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="post_process_takeover",
        description=(
            "blocking：宿主不在做错别字处理时（后处理总开关关闭，或总开关开启但宿主错别字"
            "关闭），以与 MaiBot 完全一致的原逻辑接管错别字注入与分段，并把结果写回 response"
        ),
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_before_post_process(self, **kwargs: Any) -> Dict[str, Any]:
        """接管宿主文本后处理。

        本处理器注册为 BLOCKING/**LATE**（``reply_round_marker`` 同为 LATE、按名字排在它之后），
        因此其它插件挂在同一 Hook 的 **BLOCKING/NORMAL** 处理器（例如智能分段插件的
        ``smart_segmentation_preserve_prepared_response``）会**先**执行：它们一旦认领本轮
        （把 ``skip_post_process`` 置为 True），本次就**让位**（``_yield_to_other_plugins``）——
        分段交给它们，本插件只保留"回复方式（@/引用）/ 文本规则 / 表情包"那部分工作；
        它们没认领（未安装、未命中、失败）时，本插件照常接管，作为兜底。

        多段发送开启时这里回传**全文**（所有分段拼接），让宿主 ``reply`` 工具的结果与
        ``ToolResultMessage`` 记录完整内容；其余分段登记到预分段缓存，实际出站时由
        ``send_service.after_build_message`` 截回第 1 段、``after_send`` 补发剩余分段——
        复刻宿主"每段一条消息"的发送效果（首段仍走宿主原生发送链，引用/写库/历史同步不受影响）。

        后处理本身是纯 CPU 计算（毫秒级），放到线程里执行以免阻塞事件循环；
        出错时原样放行，交由框架按自身开关处理，不吞掉这条回复。
        """
        modified = dict(kwargs)
        if not self._post_process_takeover_active():
            return {"action": "continue", "modified_kwargs": modified}

        if self._yield_to_other_plugins(modified):
            return {"action": "continue", "modified_kwargs": modified}

        response = str(modified.get("response") or "").strip()
        if not response:
            return {"action": "continue", "modified_kwargs": modified}

        enable_splitter = bool(modified.get("enable_splitter", True))
        enable_typo = bool(modified.get("enable_chinese_typo", True))
        processor = self._get_processor()
        # 会话类型未缓存时保留抽签结果，让首段回执判定是否允许执行撤回。
        session_id = str(modified.get("session_id") or "").strip()
        allow_recall = self._session_group_flags.get(session_id) is not False
        try:
            # 0.12.0 起接管**恒走 plan()**：错字纠正方式完全由权重池决定（"宿主原逻辑"分支已随
            # ``correction_mode_enabled`` 开关一起移除；``process()`` 只作为与宿主逐行对齐的
            # 参考实现保留，不再出现在接管链路上）。
            # 错字是否可见仍沿用宿主那半句硬编码门控（0.5，见 post_processing.plan 的注释）。
            plan = await asyncio.to_thread(
                processor.plan,
                response,
                enable_splitter=enable_splitter,
                enable_chinese_typo=enable_typo,
                choose_mode=partial(self._choose_correction_mode, allow_recall=allow_recall),
                should_defer=self._should_defer_correction,
                visible_probability=_HOST_TYPO_VISIBLE_PROBABILITY,
                max_correction_cjk=int(getattr(self.config.chinese_typo, "correction_max_cjk", 20)),
            )
            segments = plan.segments
            corrections = plan.corrections
            deferred = plan.deferred
        except Exception as exc:
            self.ctx.logger.warning("后处理接管执行失败，回退为框架默认: %s", exc)
            return {"action": "continue", "modified_kwargs": modified}

        # 每轮一行诊断：宿主开关 / 复刻参数 / 本轮错字统计。**"改了错字概率却没反应"只能看这行**：
        # 「错字总开关=False」= 宿主总开关或本插件参数关着；「真出现错字=0」= 错字压根没生成
        # （error_rate 太小 / min_freq 太高 / 词频表异常 / 全被黑名单挡掉）；
        # 「真出现错字>0 但判定纠正=0」= 撞了超限闸门，或权重池抽到"不纠正"（看后面两个计数区分）；
        # 「生成错字>0 但真出现错字=0」= 宿主那半句 0.5 门控把整句换成了正确句。
        stats = dict(getattr(processor, "last_stats", {}) or {})
        # 整词同音替换因组合数超上限而跳过的**累计**次数（见 post_processing._MAX_HOMOPHONE_COMBINATIONS）：
        # 宿主原逻辑在长词上会卡死，本插件加了硬上限，非 0 就说明确实撞上了那个规模。
        try:
            combo_skips = int(getattr(processor._get_typo_generator(), "_word_combo_skips", 0) or 0)
        except Exception:  # 生成器还没建起来（本轮没走错字）→ 视为 0
            combo_skips = 0
        # 因命中错字黑名单而跳过的**累计**次数（字 + 词，1.1.3 新增）。
        try:
            blacklist_skips = int(getattr(processor._get_typo_generator(), "_blacklist_skips", 0) or 0)
        except Exception:
            blacklist_skips = 0
        # 「撞 correction_max_cjk 而未纠正」的句子数（1.1.3 新增）。旧实现在这条分支静默
        # continue，日志上「撞超限」与「权重池抽到 none」完全无法区分——排查最费时间的地方。
        overlong_skips = int(stats.get("correction_skipped_overlong") or 0)
        # 「生成器认为改动了、但被可见性门控换回正确句」的句子数（1.1.3 新增）：
        # = 生成数 − 可见数，非 0 说明日志里的"没看到错字"是 0.5 门控造成的，不是生成失败。
        visible_typos = int(stats.get("typo_sentences") or 0)
        generated_typos = int(stats.get("typo_generated") or 0)
        gate_hidden = max(0, generated_typos - visible_typos)
        extra_parts = []
        if overlong_skips:
            extra_parts.append("超限未纠正=%d 句" % overlong_skips)
        if gate_hidden:
            extra_parts.append("可见性门控隐藏=%d 句" % gate_hidden)
        if combo_skips:
            extra_parts.append("整词替换跳过=%d(累计)" % combo_skips)
        if blacklist_skips:
            extra_parts.append("黑名单跳过=%d(累计)" % blacklist_skips)
        self.ctx.logger.info(
            "后处理接管统计：宿主开关[分段=%s 错字=%s] 生效参数[错字总开关=%s error_rate=%s(源%s) "
            "min_freq=%s tone=%s word_replace=%s 分段=%s 最多条数=%s] → 真出现错字=%s 句、"
            "判定纠正=%s 处（纠正方式=权重池）、共 %s 段%s",
            enable_splitter,
            enable_typo,
            getattr(processor, "typo_enable", None),
            getattr(processor, "typo_error_rate", None),
            self._processor_param_sources.get("typo_error_rate", "宿主值"),
            getattr(processor, "typo_min_freq", None),
            getattr(processor, "typo_tone_error_rate", None),
            getattr(processor, "typo_word_replace_rate", None),
            getattr(processor, "splitter_enable", None),
            getattr(processor, "splitter_max_split_num", None),
            stats.get("typo_sentences"),
            stats.get("corrections"),
            len(segments),
            ("、" + "、".join(extra_parts)) if extra_parts else "",
        )

        # 自定义异常兜底提示词的占位替换（1.0.0）：仅兜底早退路径（过长/句子太多，
        # stats early=True）抽出的模板可能带 {bot_name}/{user_name}，正常回复正文不动。
        # 替换失败原样发送（模板里的占位符字面可见，好过丢掉整条兜底）。
        if stats.get("early") and segments:
            try:
                segments = await self._format_fallback_placeholders(
                    segments, str(modified.get("reply_message_id") or "").strip()
                )
            except Exception as exc:
                self.ctx.logger.warning("异常兜底提示词占位替换失败，按原文发送: %s", exc)

        max_segments = max(int(self.config.response_splitter.max_segments), 0)
        # 分段后回车最终处理：必须在错字/分段逻辑全部完成后、构造 follow-up 之前。
        # 早退（超长/句数超限）或已超过分段条数上限时按“不分段”，整段跳过；
        # 只有正常分段结果才处理。额外拆分允许突破条数上限 1 段。
        original_segment_count = len(segments)
        over_segment_cap = max_segments > 0 and original_segment_count > max_segments
        newline_action = str(
            getattr(self.config.response_splitter, "final_newline_action", "额外拆分一段")
            or "额外拆分一段"
        ).strip()
        extra_split_count = 0
        newline_changed = False
        # “超限保留全文”（overflow=True）同样是超限不分段：宿主返回单段全文，
        # 不能再按回车拆/改，否则违反“超限不分段即使有回车也不处理”。
        overflow_fallback = bool(stats.get("early")) or bool(stats.get("overflow"))
        if not overflow_fallback and not over_segment_cap and session_id:
            segments, corrections, deferred, extra_split_count, newline_changed = (
                self._apply_final_newline_policy(segments, corrections, deferred)
            )
            if newline_changed:
                self.ctx.logger.info(
                    "分段后回车处理：%s，%s 段 -> %s 段（额外拆分=%s，会话 %s）",
                    newline_action,
                    original_segment_count,
                    len(segments),
                    extra_split_count,
                    session_id,
                )
        elif newline_action != "保持原样" and (overflow_fallback or over_segment_cap):
            self.ctx.logger.info(
                "分段后回车处理跳过：%s（%s，会话 %s）",
                newline_action,
                "早退/超限不分段" if overflow_fallback else "超过分段条数上限",
                session_id or "?",
            )
        # 新分支把纠正动作也变成独立消息；单段 + 有纠正时同样需要多段发送通道。
        has_extra_messages = bool(corrections) or bool(deferred)
        multi_ok = (
            self._multi_message_active()
            and (len(segments) > 1 or has_extra_messages)
            # 额外拆分的那一段“无视上限”：只放宽本插件 max_segments 的最终判定；
            # 原始分段数超过上限时上面已经整段跳过。
            and (max_segments <= 0 or len(segments) <= max_segments + extra_split_count)
        )

        target_id = str(modified.get("reply_message_id") or "").strip()
        if multi_ok and session_id:
            first_segment = segments[0]
            # 回传给宿主的"全文"只由分段正文拼成（纠正动作不是回复内容，不参与拼接）；
            # 宿主 reply 工具据此生成 ToolResultMessage，planner 才不会误判"没发完"。
            if target_id:
                full_text = "".join(segment.text + segment.separator for segment in segments).strip()
            else:
                # 无目标的宿主回复按完整分段布局精确匹配，保留自然的换行。
                full_text = "\n".join(segment.text for segment in segments).strip()
            if not full_text:
                full_text = first_segment.text
            items = self._build_follow_up_items(segments, corrections, deferred)
            if items and self._store_prepared_segments(
                session_id, full_text, first_segment.text, items,
                target_id=target_id,
            ):
                modified["response"] = full_text
                modified["skip_post_process"] = True
                modified["enable_splitter"] = True
                modified["enable_chinese_typo"] = True
                self.ctx.logger.info(
                    "后处理接管：共 %s 段%s，全文回传供 reply 工具结果、首段将在出站 Hook 截回；"
                    "登记 %s 条后续动作（会话 %s）",
                    len(segments),
                    "（含错字纠正 " + str(len(corrections) + len(deferred)) + " 处）" if has_extra_messages else "",
                    len(items),
                    session_id,
                )
                if has_extra_messages:
                    self.ctx.logger.info(
                        "错字纠正方式（会话 %s）：%s",
                        session_id,
                        "；".join(
                            [f"第 {index} 段→{decision.mode}" for index, decision in sorted(corrections.items())]
                            + [
                                f"第 {decision.segment_index} 段→{decision.mode}(最后纠正)"
                                for decision in deferred
                            ]
                        ),
                    )
                return {"action": "continue", "modified_kwargs": modified}

        # 单段 / 多段发送关闭 / 超过条数上限 / 缺会话标识：退化为换行拼接一条。
        modified["response"] = "\n".join(segment.text for segment in segments)
        if session_id and not target_id:
            # 单段或超限时仍识别这次直发，以便清掉宿主附带的 @ 或引用。
            self._store_prepared_segments(
                session_id, modified["response"], modified["response"], [], target_id="",
            )
        # 由插件完成全部后处理，通知框架跳过其自身（其受总开关门控、本就不会再处理）。
        modified["skip_post_process"] = True
        modified["enable_splitter"] = True
        modified["enable_chinese_typo"] = True
        return {"action": "continue", "modified_kwargs": modified}

    def _build_follow_up_items(
        self,
        segments: List[ProcessedResponseSegment],
        corrections: Dict[int, Any],
        deferred: Any,
    ) -> List["FollowUpItem"]:
        """把"分段 + 错字纠正计划"摊平成一个按序执行的补发动作序列。

        顺序即语义：第 1 段（宿主发）→ 第 1 段的纠正 → 第 2 段 → 第 2 段的纠正 → …
        → 最后纠正（按抽到的纠正方式引用 / 撤回它所属那一段的消息）。所以"第二段的错字
        纠正"一定早于第三段发出。
        """
        items: List[FollowUpItem] = []
        for index in range(1, len(segments)):
            segment = segments[index]
            items.append(
                FollowUpItem(
                    kind="text",
                    text=segment.text,
                    quote_previous=bool(segment.quote_previous),
                    slot=index,
                )
            )
            decision = corrections.get(index)
            if decision is not None:
                items.extend(self._build_correction_items(decision))
        first_decision = corrections.get(0)
        if first_decision is not None:
            # 首段由宿主发出，它的纠正动作排在序列最前（紧随首段）。
            items[0:0] = self._build_correction_items(first_decision)
        for decision in deferred:
            items.extend(self._build_correction_items(decision))
        return items

    @staticmethod
    def _build_correction_items(decision: Any) -> List["FollowUpItem"]:
        """为即时和延迟纠正保留同一个目标段位。"""
        mode = str(getattr(decision, "mode", "none"))
        slot = int(getattr(decision, "segment_index", -1))
        if mode == "direct":
            return [FollowUpItem(kind="text", text=str(decision.text))]
        if mode == "quote":
            return [FollowUpItem(kind="text", text=str(decision.text), quote_slot=slot)]
        if mode == "recall":
            # 撤回带错字的那条，然后重发修正后的整句（撤回了就必须给完整内容）。
            return [
                FollowUpItem(
                    kind="recall",
                    text=str(decision.sentence or decision.text),
                    recall_slot=slot,
                    correction_text=str(decision.text),
                )
            ]
        return []

    def _apply_final_newline_policy(
        self,
        segments: List[ProcessedResponseSegment],
        corrections: Dict[int, CorrectionDecision],
        deferred: Tuple[CorrectionDecision, ...],
    ) -> Tuple[List[ProcessedResponseSegment], Dict[int, CorrectionDecision], Tuple[CorrectionDecision, ...], int, bool]:
        """分段全部完成后的最终回车处理（在错字/分段逻辑之后）。

        返回 ``(segments, corrections, deferred, extra_split_count, changed)``。
        ``extra_split_count`` 仅用于让调用方在“额外拆分一段”时放宽分段条数上限；
        其他动作不改变段数。
        """
        action = str(
            getattr(self.config.response_splitter, "final_newline_action", "额外拆分一段")
            or "额外拆分一段"
        ).strip()
        if action == "取消回车":
            updated: List[ProcessedResponseSegment] = []
            changed = False
            for segment in segments:
                cleaned = re.sub(r"[\r\n]+", "", segment.text)
                if cleaned != segment.text and cleaned.strip():
                    updated.append(replace(segment, text=cleaned))
                    changed = True
                else:
                    updated.append(segment)
            return updated, corrections, deferred, 0, changed
        if action != "额外拆分一段":
            return segments, corrections, deferred, 0, False

        # 带错字纠正/延迟纠正的段不能在这里拆：纠正的 segment_index 无法可靠判断
        # 错字落在回车前还是后；拆了会让撤回重发引用错段。跳过该段，继续找其它段。
        corrected_segment_indexes = set(corrections) | {
            decision.segment_index for decision in deferred
        }
        for index, segment in enumerate(segments):
            if index in corrected_segment_indexes:
                continue
            text = segment.text
            for match in re.finditer(r"\r\n|\r|\n", text):
                before = text[: match.start()]
                after = text[match.end():]
                if not before.strip() or not after.strip():
                    continue
                # 只做“额外一段”：在第一个两侧都有内容的回车处拆成前后两段，
                # 后续即使还有回车也留给后段，不继续无限拆。
                first = ProcessedResponseSegment(
                    text=before.strip(),
                    quote_previous=segment.quote_previous,
                    # 保留原回车作为 full_text 拼接分隔符：两条实际消息分开，
                    # 但回传给 reply 工具结果的全文仍保留换行，不会粘成一行。
                    separator=match.group(0),
                )
                second = ProcessedResponseSegment(
                    text=after.strip(),
                    quote_previous=False,
                    separator=segment.separator,
                )
                updated_segments = [*segments[:index], first, second, *segments[index + 1:]]
                updated_corrections: Dict[int, CorrectionDecision] = {}
                for segment_index, decision in corrections.items():
                    if segment_index > index:
                        updated_corrections[segment_index + 1] = replace(
                            decision, segment_index=segment_index + 1
                        )
                    else:
                        updated_corrections[segment_index] = decision
                updated_deferred = tuple(
                    replace(decision, segment_index=decision.segment_index + 1)
                    if decision.segment_index > index
                    else decision
                    for decision in deferred
                )
                # “额外拆分”只拆第一处回车；同一轮压缩里可能还有其它回车
                # （例如 5 个分句压成 3 条时有两处）。这些剩余回车不能再
                # 留在消息内部，统一压成空格；带纠正的段保持原样，避免撤回/
                # 引用重发时文本与纠正记录不一致。
                corrected_after_split = set(updated_corrections) | {
                    decision.segment_index for decision in updated_deferred
                }
                cleaned_segments: List[ProcessedResponseSegment] = []
                for segment_index, segment in enumerate(updated_segments):
                    if segment_index in corrected_after_split:
                        cleaned_segments.append(segment)
                        continue
                    cleaned = re.sub(
                        r"[^\S\r\n]*[\r\n]+[^\S\r\n]*", " ", segment.text
                    ).strip()
                    if cleaned and cleaned != segment.text:
                        cleaned_segments.append(replace(segment, text=cleaned))
                    else:
                        cleaned_segments.append(segment)
                return (
                    cleaned_segments,
                    updated_corrections,
                    updated_deferred,
                    1,
                    True,
                )
        return segments, corrections, deferred, 0, False

    # Hook 2c：多段发送——登记待补发分段（blocking）
    # ------------------------------------------------------------------

    @HookHandler(
        "send_service.after_build_message",
        name="post_process_follow_up_registrar",
        description="多段发送：按全文查出缓存，把实际出站截回第 1 段并登记剩余分段交给 after_send 补发",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_after_build_message(self, **kwargs: Any) -> Dict[str, Any]:
        """按**全文**消费 Hook 1 登记的预分段缓存；未命中一律放行。

        本条出站消息携带的文本可能是"全文"（``before_post_process`` 把所有分段拼成一条
        回传给宿主，让 ``reply`` 工具结果与 ``ToolResultMessage`` 记录完整内容）。插件按
        全文查到缓存后，把**实际发送**的消息体截回第 1 段（替换首个非空 text 组件，reply / at /
        image / emoji 等非 text 组件原样保留；紧跟 at 的空格 text 段也保留，1.4.2），
        其余分段仍由 ``after_send`` 补发——所以
        不会把全文一次性发出去，也不会让 planner 误以为回复没发完。

        匹配时优先用 ``raw_message`` 的原文，再退回 ``processed_plain_text``：宿主可能给
        可见文本加上 ``[发言内容]`` 等前缀，原文才是我们回传的全文。

        未命中意味着这条出站消息不是"本轮回复的全文"（命令回执、其它插件用
        ``ctx.send.*`` 发出的消息、表情/图片等），天然跳过；补发期间的重入也在此挡掉。

        补发请求使用独立标识。同一会话里另一轮回复的首段即使正文相同也照常登记。
        """
        raw_message = kwargs.get("message")
        if not isinstance(raw_message, dict):
            return {"action": "continue"}
        message = dict(raw_message)
        session_id = str(kwargs.get("stream_id") or message.get("session_id") or "").strip()
        if not session_id:
            return {"action": "continue"}
        processed = str(kwargs.get("processed_plain_text") or "")
        follow_up_id = self._follow_up_from_build(session_id, processed)
        if follow_up_id:
            annotate_outbound(message, _FOLLOW_UP_ID, follow_up_id)
            if self._follow_up_intents[follow_up_id].unanchored:
                annotate_outbound(message, _UNANCHORED_REPLY, True)
            return {
                "action": "continue",
                "modified_kwargs": {**kwargs, "message": message, "processed_plain_text": processed.strip()},
            }
        target_id = str(message.get("reply_to") or "").strip()
        if not self._multi_message_active():
            return {"action": "continue"}
        # ``processed_plain_text`` 可能被宿主的可见文本渲染加上 ``[发言内容]`` 等前缀；
        # 原始 ``raw_message`` 文本才是我们回传的全文。两个都试，优先用原文匹配缓存，
        # 避免因宿主渲染差异导致全文被当作普通消息一次性发出。
        raw_text = self._extract_outbound_text(message, fallback=bool(target_id))
        processed_text = processed.strip()
        if target_id:
            candidate_texts = (raw_text, processed_text)
        else:
            # 原生 reply 会传入可见文本。普通 send.text 的该参数为空，不能拿走待发回复。
            # 有 attach_at 时，可见文本还带 @ 前缀，所以用原始文本组件匹配正文。
            if (
                not raw_text
                or not processed_text.endswith(raw_text)
                or processed != processed.rstrip(" \t")
                or kwargs.get("typing") is True
            ):
                return {"action": "continue"}
            candidate_texts = (raw_text,)
        prepared = None
        for candidate in candidate_texts:
            prepared = self._take_prepared_segments(session_id, candidate, target_id=target_id)
            if prepared is not None:
                break
        if prepared is None:
            return {"action": "continue"}
        first_text = prepared.first_text
        prepared.planner_record["rewritten"] = True
        prepared.planner_record["initial_message_id"] = str(message.get("message_id") or "").strip()
        # 实际发送只保留第 1 段：替换首个**非空** text 组件，非 text 组件全部保留。
        # 1.4.2：紧跟 at 的纯空白 text 段（宿主 attach_at 的 [at, " ", 正文] 里那个空格）
        # 原样保留——旧实现把它当"第一个 text"替换成首段正文，at 与正文粘连丢空格。
        # 其余纯空白段照旧丢弃，避免引入前导/悬挂空格。
        mutated_message = dict(message)
        components = message.get("raw_message")
        new_components: List[Any] = []
        text_written = False
        saw_text = False
        if isinstance(components, list):
            for component in components:
                if isinstance(component, dict) and str(component.get("type") or "") == "text":
                    saw_text = True
                    data = str(component.get("data") or "")
                    if not data.strip():
                        if (
                            not text_written
                            and new_components
                            and isinstance(new_components[-1], dict)
                            and str(new_components[-1].get("type") or "") == "at"
                        ):
                            new_components.append(component)
                        continue
                    if not text_written:
                        new_components.append({"type": "text", "data": first_text})
                        text_written = True
                    continue
                new_components.append(component)
        if not text_written:
            if saw_text:
                # 只有空白 text（如 [at, " "]）：正文接在空白后面，保住 at 后的空格。
                new_components.append({"type": "text", "data": first_text})
            else:
                new_components.insert(0, {"type": "text", "data": first_text})
        mutated_message["raw_message"] = new_components
        mutated_message["processed_plain_text"] = first_text
        # 宿主 Hook 的 modified_kwargs 是**完整替换**：必须带上 stream_id / typing /
        # set_reply / storage_message / show_log 等全部原参数，否则发送行为被重置。
        modified_kwargs = dict(kwargs)
        modified_kwargs["message"] = mutated_message
        modified_kwargs["processed_plain_text"] = first_text
        if prepared.unanchored:
            annotate_outbound(mutated_message, _UNANCHORED_REPLY, True)
            self._remove_unanchored_addressing(modified_kwargs, mutated_message)
        send_id = uuid.uuid4().hex
        annotate_outbound(mutated_message, _FIRST_SEGMENT_ID, send_id)
        self._register_pending_follow_up(session_id, send_id, prepared)
        return {"action": "continue", "modified_kwargs": modified_kwargs}

    # Hook 2d：多段发送——首段发成功后补发（observe）
    # ------------------------------------------------------------------

    @HookHandler(
        "send_service.after_send",
        name="post_process_follow_up_sender",
        description="observe：首段发送成功后启动后台补发（不占用宿主 after_send 超时窗口）",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_after_send_follow_up(self, **kwargs: Any) -> None:
        """收到首段送达确认后启动补发。"""
        if not self._multi_message_active():
            return
        raw_message = kwargs.get("message")
        if not isinstance(raw_message, dict):
            return
        message = dict(raw_message)
        session_id = str(message.get("session_id") or "").strip()
        if not session_id:
            return
        send_id = outbound_metadata(message).get(_FIRST_SEGMENT_ID)
        if not isinstance(send_id, str):
            return
        entry = self._pending_follow_ups.pop((session_id, send_id), None)
        if entry is None:
            return
        registered_at, prepared = entry
        items = prepared.items
        record = prepared.planner_record
        if time.monotonic() - registered_at > _PENDING_FOLLOW_UP_TTL_SECONDS:
            record["done"].set()
            return
        if kwargs.get("sent") is not True:
            record["done"].set()
            self.ctx.logger.warning(
                "后处理接管：首段未确认送达，未启动 %s 条后续动作（会话 %s）", len(items), session_id
            )
            return
        # 平台缺省 ID 时宿主保留构建时的内部 ID，后者只能定位历史，不用于平台操作。
        receipt_id = str(message.get("message_id") or "").strip()
        previous_message_id = receipt_id if receipt_id != record.get("initial_message_id") else ""
        outbound_text = self._extract_outbound_text(message) or prepared.first_text
        record["sent"].append((outbound_text, receipt_id))
        if not items:
            record["done"].set()
            return
        try:
            task = asyncio.create_task(
                self._run_follow_up_items(
                    session_id,
                    items,
                    typing=True,
                    previous_message_id=previous_message_id,
                    first_text=outbound_text,
                    planner_record=record,
                    first_style=delivered_reply_style(message, kwargs),
                    unanchored=prepared.unanchored,
                    allow_recall=self._is_group_message(message),
                )
            )
            self._track_follow_up_task(session_id, task)
            record["task"] = task
            task.add_done_callback(lambda _task: record["done"].set())
        except Exception as exc:
            self.ctx.logger.error("启动补发任务失败（会话 %s）: %s", session_id, exc)
            record["done"].set()

    # Hook 2e：多段发送——时序守卫（planner / 入站消息等待补发完成）
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.planner.before_request",
        name="post_process_follow_up_waiter",
        description="blocking：本轮补发未完成时先等待，并按 items 中实际缺失的分段补齐 planner 上下文",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
        timeout_ms=_FOLLOW_UP_WAIT_HOOK_TIMEOUT_MS,
    )
    async def handle_planner_wait_for_follow_ups(self, **kwargs: Any) -> Dict[str, Any]:
        if not self._multi_message_active():
            return {"action": "continue"}
        # 子代理（表达选择/行为分析等）与主 planner 共用 session_id，但
        # tool_definitions=[]；它们没有 reply 工具，不应等待/注入本轮补发。
        tool_definitions = kwargs.get("tool_definitions")
        if isinstance(tool_definitions, list) and not tool_definitions:
            return {"action": "continue"}
        session_id = str(kwargs.get("session_id") or "").strip()
        if not session_id:
            return {"action": "continue"}
        record = self._get_planner_segment_record(session_id)
        if record is not None and not record.get("rewritten"):
            # 全文没有被截回（单条兜底/缓存未命中）：不会按段补，也没必要等补发任务。
            if self._planner_segment_records.get(session_id) is record:
                self._planner_segment_records.pop(session_id, None)
            record = None
        if record is not None:
            timeout = max(float(self.config.response_splitter.wait_timeout_seconds), 0.0)
            timeout = min(
                timeout,
                _FOLLOW_UP_WAIT_HOOK_TIMEOUT_MS / 1000.0 - _FOLLOW_UP_WAIT_HEADROOM_SECONDS,
            )
            wait_timeout = timeout
            record_task = record.get("task")
            has_task = record_task is not None and not record_task.done()
            age = time.monotonic() - float(record.get("created_at", 0.0))
            if timeout > 0 and not record["done"].is_set() and not record.get("sent") and not has_task:
                # 首段还没登记 sent、也没有补发任务：只等到短宽限，避免
                # before_send abort / 发送管线异常导致 planner 白等满 30s。
                if age >= _PLANNER_SEGMENT_FIRST_SEND_GRACE_SECONDS:
                    record["done"].set()
                else:
                    wait_timeout = min(timeout, _PLANNER_SEGMENT_FIRST_SEND_GRACE_SECONDS - age)
            if wait_timeout > 0 and not record["done"].is_set():
                try:
                    await asyncio.wait_for(record["done"].wait(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    self.ctx.logger.warning(
                        "后处理接管：planner 等待补发超时 %.1fs（会话 %s），按已发送分段补齐",
                        wait_timeout,
                        session_id,
                    )
            if (
                timeout > 0
                and not record["done"].is_set()
                and not record.get("sent")
                and not has_task
                and time.monotonic() - float(record.get("created_at", 0.0))
                >= _PLANNER_SEGMENT_FIRST_SEND_GRACE_SECONDS
            ):
                # 收不到首段回执时先放行 planner，待补发登记独立等实际回执。
                record["done"].set()
        else:
            waited = await self._wait_for_stream_follow_ups(session_id)
            if waited:
                self.ctx.logger.info(
                    "后处理接管：planner 请求等待 %s 个补发任务结束（会话 %s）", waited, session_id
                )
        if record is not None:
            modified = self._inject_missing_planner_segments(session_id, kwargs, record=record)
            if modified is not None:
                return {"action": "continue", "modified_kwargs": modified}
        return {"action": "continue"}


    @HookHandler(
        "chat.receive.before_process",
        name="post_process_follow_up_gate",
        description="blocking：本会话补发未完成时先等待，避免新消息与旧回复交错",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
        timeout_ms=_FOLLOW_UP_WAIT_HOOK_TIMEOUT_MS,
    )
    async def handle_inbound_wait_for_follow_ups(self, **kwargs: Any) -> Dict[str, Any]:
        if not self._multi_message_active():
            return {"action": "continue"}
        message = kwargs.get("message")
        session_id = str(message.get("session_id") or "").strip() if isinstance(message, dict) else ""
        waited = await self._wait_for_stream_follow_ups(session_id)
        if waited:
            self.ctx.logger.info(
                "后处理接管：入站消息等待 %s 个补发任务结束（会话 %s）", waited, session_id
            )
        return {"action": "continue"}
