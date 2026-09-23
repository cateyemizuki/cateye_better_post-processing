"""后处理接管模块：错别字 + 分段 + 多段发送（完整复刻宿主后处理框架效果）。

**宿主前置条件**（由 ``modules.requirements`` 判定）：宿主
``response_post_process.enable_response_post_process`` 与 ``experimental.enable_rich_reply``
都必须为 ``false``；任一未关闭时本模块全部 Hook **静默返回**、不做任何处理。

Hook 分工（多段发送）：

1. ``maisaka.reply.before_post_process``（blocking）：算分段 → 只回传第 1 段，其余入缓存；
2. ``send_service.after_build_message``（blocking）：按首段文本反查缓存 → 登记待补发；
3. ``send_service.after_send``（observe）：确认首段发出后，后台顺序补发剩余分段；
4. ``maisaka.planner.before_request`` / ``chat.receive.before_process``（blocking）：补发
   未完成时先等待（带超时），避免下一轮上下文缺段、新消息与旧回复交错。

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
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

from maibot_sdk import HookHandler
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from ..post_processing import PostProcessor, ProcessedResponseSegment


@dataclass(frozen=True)
class FollowUpItem:
    """补发序列里的一个动作（顺序即语义）。

    - ``kind="text"``：发送 ``text``。``quote_previous=True`` 时引用**上一条**已发出的消息
      （宿主 ``quote_previous`` 语义 / "引用纠正"）；``quote_slot>=0`` 时引用**第 slot 段**
      的消息（"最后纠正"用）；两者都没有就不引用。
    - ``kind="recall"``：撤回 ``recall_slot`` 指定的那条消息（``-1`` = 上一条已发出的），
      然后发送 ``text``（"撤回后重发"：``text`` 是修正后的整句）。
      撤回失败时按 ``[chinese_typo] recall_fallback`` 兜底。
    - ``slot``：本条发送对应第几段（用于给"最后纠正"记录引用/撤回目标），非分段消息填 -1。
    """

    kind: str
    text: str = ""
    quote_previous: bool = False
    quote_slot: int = -1
    slot: int = -1
    recall_slot: int = -1

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
    ("bot_nickname", "response_splitter", "fallback_nickname", "bot_nickname", "text", "bot", "nickname"),
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

# 预分段缓存 TTL：Hook 之间靠 (会话, 首段文本) 关联，超时未消费即丢弃。
_PREPARED_SEGMENT_TTL_SECONDS = 120.0
# 单会话最多保留几轮预分段（防异常堆积）。
_PREPARED_SEGMENT_MAX_ROUNDS = 8
# 待补发登记的 TTL（首段发送失败/被中止时的兜底清理）。
_PENDING_FOLLOW_UP_TTL_SECONDS = 120.0
# 补发单段的重试次数。
_FOLLOW_UP_SEND_MAX_ATTEMPTS = 2
# planner / 入站消息等待补发的 Hook 超时（内部等待时长由配置决定，这里留出余量）。
#
# 为什么要显式给 65s：`maisaka.planner.before_request` 的 Hook 规格默认超时只有 6000ms，
# 会在等待窗口中间把处理器掐掉（error_policy=SKIP → 静默不再等待）。处理器自带的
# timeout_ms 优先于规格默认值（宿主 HookDispatcher._resolve_timeout_ms），因此这里必须
# 设成比 `[response_splitter] wait_timeout_seconds`（默认 30s）更大的值，并留出补发收尾余量；
# 它大于系统级 `plugin_runtime.hook_blocking_timeout_sec`（默认 60s）是**有意**的，
# 不要"顺手"改成 60s 以下。
_FOLLOW_UP_WAIT_HOOK_TIMEOUT_MS = 65_000


class PostProcessTakeoverMixin:
    """后处理接管（错别字 + 分段 + 多段发送）的 Hook 与状态。"""

    def _init_post_process_takeover(self) -> None:
        """初始化后处理接管状态（由插件类 ``__init__`` 调用）。"""
        # 复刻宿主后处理（错别字 + 分段）的处理器，宿主配置变化时作废重建。
        self._processor: Optional[PostProcessor] = None
        # 生效参数的来源（"宿主" / "插件覆盖"），仅用于日志。
        self._processor_param_sources: Dict[str, str] = {}
        # 多段发送：预分段缓存 session_id -> {首段文本键: (登记时刻, 剩余分段)}。
        self._prepared_segments: Dict[str, Dict[str, Tuple[float, List[ProcessedResponseSegment]]]] = {}
        # 多段发送：待补发登记 lookup_key -> 补发数据（首段发送成功后才真正发出）。
        self._pending_follow_ups: Dict[str, Dict[str, Any]] = {}
        # 多段发送：会话 -> 进行中的补发任务（planner / 入站消息等待用）。
        self._stream_follow_up_tasks: Dict[str, Set[asyncio.Task]] = {}
        # 多段发送：会话 -> 正在补发的分段文本键（重入保护 + 文本规则归属判定）。
        self._stream_sending_texts: Dict[str, Set[str]] = {}
        # 多段发送：会话 -> {分段文本键: 待注入的引用目标 id}（更正段引用上一段）。
        self._pending_quote_targets: Dict[str, Dict[str, str]] = {}


    # 后处理接管（错别字 + 分段）
    # ------------------------------------------------------------------

    def _post_process_takeover_active(self) -> bool:
        """框架后处理总开关关闭、且插件接管开关开启时，接管后处理。

        宿主前置条件（``response_post_process.enable_response_post_process`` 与
        ``experimental.enable_rich_reply`` 都关闭）由 ``modules.requirements`` 判定；
        未满足时本模块**完全静默**：不改写 response、不登记补发、不发任何消息。
        """
        if not self._plugin_enabled():
            return False
        if not bool(self.config.response_splitter.takeover):
            return False
        return self._module_available("post_process")


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
            self._processor = PostProcessor(**params)
            self._processor_param_sources = sources
            self._log_effective_params(params, sources)
        return self._processor

    def _log_effective_params(self, params: Dict[str, Any], sources: Dict[str, str]) -> None:
        """打印一次生效参数（含"插件值 / 宿主值"来源）。

        错字「改了配置却没反应」这类问题全靠这行定位；宿主值与插件值不一致时额外提示怎么恢复跟随。
        """
        self.ctx.logger.info(
            "后处理接管参数：分段enable=%s(%s) 长度=%s 句数=%s 条数=%s 颜文字=%s 超限全文=%s | "
            "错字enable=%s(%s) error_rate=%s(%s) min_freq=%s(%s) tone=%s(%s) word=%s(%s) "
            "昵称=%s(%s)",
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

    def _choose_correction_mode(self, segment_index: int, suggestion: str) -> Optional[str]:
        """按权重抽取某一段错字的纠正方式（跑在处理线程里，只用 random + 配置）。

        权重池：``direct``（直接发送）/``quote``（引用带错字的消息）/``recall``（撤回后重发
        修正句）/``none``（不纠正）。权重全为 0 时按"不纠正"处理（= 完全关掉纠正）。
        "最后纠正"**不在池子里**——它是"把抽到的纠正推迟到本轮分段发完之后"，由
        ``_should_defer_correction`` 单独决定（见 ``[chinese_typo_weights]`` 的 last_correction_*）。

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
    def _extract_outbound_text(message: Dict[str, Any]) -> str:
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
        return str(message.get("processed_plain_text") or "").strip()


    # ---- 预分段缓存（Hook 1 写入，Hook 2 消费） ----

    def _store_prepared_segments(
        self, session_id: str, first_text: str, items: List["FollowUpItem"]
    ) -> bool:
        """登记"首段 + 后续动作序列"，供出站 Hook 按首段文本反查。"""
        key = self._text_key(first_text)
        if not session_id or not key or not items:
            return False
        bucket = self._prepared_segments.setdefault(session_id, {})
        now = time.monotonic()
        for stale_key, (stored_at, _) in list(bucket.items()):
            if now - stored_at > _PREPARED_SEGMENT_TTL_SECONDS:
                bucket.pop(stale_key, None)
        bucket[key] = (now, list(items))
        while len(bucket) > _PREPARED_SEGMENT_MAX_ROUNDS:
            oldest_key = min(bucket.items(), key=lambda item: item[1][0])[0]
            bucket.pop(oldest_key, None)
        return True


    def _take_prepared_segments(self, session_id: str, text: str) -> List["FollowUpItem"]:
        """按首段文本取走后续动作序列（取走即删除，避免同一条发送被补发两次）。"""
        bucket = self._prepared_segments.get(session_id)
        if not bucket:
            return []
        entry = bucket.pop(self._text_key(text), None)
        if entry is None:
            if not bucket:
                self._prepared_segments.pop(session_id, None)
            return []
        if not bucket:
            self._prepared_segments.pop(session_id, None)
        stored_at, items = entry
        if time.monotonic() - stored_at > _PREPARED_SEGMENT_TTL_SECONDS:
            return []
        return list(items)


    # ---- 待补发登记（Hook 2 写入，Hook 3 消费） ----
    @staticmethod
    def _follow_up_tracking_key(session_id: str, timestamp: Any, text: str) -> str:
        """稳定追踪键：不依赖平台回执对 message_id 的改写。"""
        normalized_text = "".join(str(text or "").split())
        if not session_id or not normalized_text:
            return ""
        return "\x1f".join((str(session_id), str(timestamp or ""), normalized_text))


    def _register_pending_follow_up(self, keys: List[str], data: Dict[str, Any]) -> None:
        now = time.monotonic()
        for stale_key, entry in list(self._pending_follow_ups.items()):
            if now - float(entry.get("registered_at", 0.0)) > _PENDING_FOLLOW_UP_TTL_SECONDS:
                self._pending_follow_ups.pop(stale_key, None)
        for key in keys:
            if key:
                self._pending_follow_ups[key] = data


    def _resolve_pending_follow_up(self, keys: List[str]) -> Optional[Dict[str, Any]]:
        for key in keys:
            if not key:
                continue
            entry = self._pending_follow_ups.get(key)
            if entry is None:
                continue
            # 同一登记可能挂在多个键上，全部清掉，避免重复补发。
            for other_key, other_entry in list(self._pending_follow_ups.items()):
                if other_entry is entry:
                    self._pending_follow_ups.pop(other_key, None)
            return entry
        return None


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
        """等待该会话的补发任务结束（带超时），返回等待过的任务数。"""
        if not session_id:
            return 0
        tasks = [task for task in self._stream_follow_up_tasks.get(session_id, set()) if not task.done()]
        if not tasks:
            return 0
        timeout = max(float(self.config.response_splitter.wait_timeout_seconds), 0.0)
        if timeout <= 0:
            return 0
        try:
            await asyncio.wait(tasks, timeout=timeout)
        except Exception as exc:  # 等待失败不应影响宿主主流程
            self.ctx.logger.debug("等待补发任务异常: %s", exc)
        return len(tasks)


    def _mark_stream_sending(self, session_id: str, text_key: str) -> None:
        self._stream_sending_texts.setdefault(session_id, set()).add(text_key)


    def _unmark_stream_sending(self, session_id: str, text_key: str) -> None:
        bucket = self._stream_sending_texts.get(session_id)
        if bucket is None:
            return
        bucket.discard(text_key)
        if not bucket:
            self._stream_sending_texts.pop(session_id, None)


    def _is_own_follow_up_text(self, session_id: str, text: str) -> bool:
        """该文本是否正是本插件正在补发的分段。

        两个用途：① 补发自身的出站消息不参与预分段登记 / 不再触发补发（重入保护）；
        ② 文本规则的归属判定（这类发送没有 ``reply_message_id``，轮记录判定不成立）。

        判据是"**文本键**是否在补发中"，不是"该会话是否在补发"——后者会把补发窗口内
        同一会话里另一轮回复的首段也挡掉，导致那一轮只发出首段。
        """
        bucket = self._stream_sending_texts.get(session_id)
        if not bucket:
            return False
        return self._text_key(text) in bucket


    # ---- 补发执行 ----

    async def _send_follow_up_text(
        self,
        session_id: str,
        text: str,
        *,
        typing: bool,
        quote_target: str = "",
        previous_message_id: str = "",
        sync_history: bool = True,
    ) -> Optional[str]:
        """补发一条文本消息；``quote_target`` 非空时给它注入引用。失败返回 None。

        ``sync_history=False``：不把这条件写入 Maisaka 对话历史——用于"发出后马上会被撤回"
        的消息，让 bot 自己的上下文里只留下修正句（撤回后重发的语义）。

        打字等待：``[response_splitter] typing_speed`` **留空**时按老规矩把 ``typing=True``
        交给宿主（宿主按它自己的 ``typing_speed`` 睡）；**填了值**时改由本插件按同一公式自己睡，
        并传 ``typing=False`` 避免宿主再按它自己的速度重复等待。
        """
        normalized = str(text or "").strip()
        if not normalized:
            return previous_message_id or None
        text_key = self._text_key(normalized)
        self._mark_stream_sending(session_id, text_key)
        try:
            if quote_target:
                # ctx.send.text 传不了 reply_message，因此在 before_send 里给这条发送注入引用
                # （宿主侧 set_reply + reply_message_id 会被 _prepare_message_for_platform_io 用上）。
                self._pending_quote_targets.setdefault(session_id, {})[text_key] = quote_target
            simulated = self._simulated_typing_seconds(normalized) if typing else None
            host_typing = bool(typing) and simulated is None
            for attempt in range(_FOLLOW_UP_SEND_MAX_ATTEMPTS):
                details: Any = None
                try:
                    if simulated is not None and attempt == 0 and simulated > 0:
                        await asyncio.sleep(simulated)
                    details = await self.ctx.send.text(
                        normalized,
                        session_id,
                        typing=host_typing and attempt == 0,
                        sync_to_maisaka_history=bool(sync_history),
                        maisaka_source_kind="guided_reply",
                        return_details=True,
                    )
                except Exception as exc:
                    self.ctx.logger.warning(
                        "补发消息异常（第 %s/%s 次尝试）: %s", attempt + 1, _FOLLOW_UP_SEND_MAX_ATTEMPTS, exc
                    )
                if isinstance(details, dict):
                    if details.get("sent"):
                        return str(details.get("message_id") or "").strip() or previous_message_id or None
                elif details is True:
                    return previous_message_id or None
                self.ctx.logger.warning(
                    "补发消息失败（第 %s/%s 次尝试）: %r",
                    attempt + 1,
                    _FOLLOW_UP_SEND_MAX_ATTEMPTS,
                    normalized[:40],
                )
            return None
        finally:
            self._unmark_stream_sending(session_id, text_key)
            quote_bucket = self._pending_quote_targets.get(session_id)
            if quote_bucket is not None:
                quote_bucket.pop(text_key, None)
                if not quote_bucket:
                    self._pending_quote_targets.pop(session_id, None)


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
    ) -> None:
        """按顺序执行补发动作序列（分段 / 直接纠正 / 引用纠正 / 撤回重发 / 最后纠正）。

        顺序即语义：某一段的纠正动作紧跟该段之后执行，**早于后续分段**；"最后纠正"
        排在序列末尾——按抽到的纠正方式引用它所属那一段的消息（``quote_slot``）、
        撤回它所属那一段的消息（``recall_slot``）、或直接补发（``direct``）。

        ``first_text``：**首段**（由宿主发出）的正文。撤回反应时间留空时按"被撤回那条的
        打字时长"自动决定，首段的正文只能由调用方传进来（``handle_after_build_message``
        登记的那份）。
        """
        previous_id = previous_message_id
        previous_text = first_text
        sent_slots: Dict[int, str] = {0: previous_message_id} if previous_message_id else {}
        sent_slot_texts: Dict[int, str] = {0: first_text} if first_text else {}
        # 稍后会被撤回的**段**：不写进 Maisaka 历史，让 bot 自己的上下文里只剩修正句。
        # 判据必须按"这条 recall 到底撤回谁"来算，不能只看"紧跟其后是 recall"——
        # "最后纠正"里的撤回排在序列末尾，它要撤回的是**前面某一段**，而紧跟其后的
        # text 动作是别的段（旧实现会把那段误判成"将被撤回"，于是它的正文也不进历史）。
        recalled_slots: Set[int] = set()
        for index, item in enumerate(items):
            if item.kind != "recall":
                continue
            if item.recall_slot >= 0:
                target_slot = item.recall_slot
            elif index > 0 and items[index - 1].kind == "text":
                target_slot = items[index - 1].slot  # 即时撤回：撤回紧邻的上一条
            else:
                target_slot = -1
            if target_slot >= 0:
                recalled_slots.add(target_slot)
        failed = 0
        for position, item in enumerate(items, start=1):
            index = position - 1
            if item.kind == "recall":
                slot = item.recall_slot
                target_id = sent_slots.get(slot, "") if slot >= 0 else previous_id
                target_text = sent_slot_texts.get(slot, "") if slot >= 0 else previous_text
                fallback = str(getattr(self.config.chinese_typo, "recall_fallback", "quote") or "quote")
                if not target_id:
                    self.ctx.logger.warning(
                        "撤回重发：找不到要撤回的消息 id（slot=%s），改为直接发送修正句: %r",
                        slot,
                        item.text[:40],
                    )
                    sent_id = await self._send_follow_up_text(
                        session_id,
                        item.text,
                        typing=typing,
                        previous_message_id=previous_id,
                    )
                else:
                    delay = self._recall_delay_seconds(target_text or item.text)
                    if delay > 0:
                        self.ctx.logger.info(
                            "撤回反应时间 %.2f 秒后撤回消息 %s（会话 %s）", delay, target_id, session_id
                        )
                        await asyncio.sleep(delay)
                    recalled = await self._recall_message(target_id)
                    if recalled:
                        if slot < 0:
                            # 刚补发出去的那条已被撤回：后面若还有"引用上一条"的动作，不能引用它。
                            previous_id = ""
                            previous_text = ""
                        quote_target = ""
                        base_id = ""
                    elif fallback == "none":
                        self.ctx.logger.warning("撤回失败且配置为不兜底，跳过该条纠正: %r", item.text[:40])
                        continue
                    else:
                        # 兜底：quote = 改成引用那条错字消息发送；direct = 直接发送。
                        quote_target = target_id if fallback == "quote" else ""
                        base_id = target_id if fallback == "quote" else ""
                    sent_id = await self._send_follow_up_text(
                        session_id,
                        item.text,
                        typing=typing,
                        quote_target=quote_target,
                        previous_message_id=base_id,
                    )
                if sent_id:
                    previous_id = sent_id
                    previous_text = item.text
                    if slot >= 0:
                        sent_slots[slot] = sent_id
                        sent_slot_texts[slot] = item.text
                else:
                    failed += 1
                    self.ctx.logger.error("撤回后重发失败: %r", item.text[:40])
                continue

            if item.quote_slot >= 0:
                quote_target = sent_slots.get(item.quote_slot, "")
                if not quote_target:
                    self.ctx.logger.debug(
                        "最后纠正找不到第 %s 段的消息 id，改为直接发送", item.quote_slot
                    )
            elif item.quote_previous:
                # 引用与否完全由权重池决定（``_build_correction_items`` 只在"引用纠正"时置位），
                # 0.12.0 起不再看宿主 ``enable_correction_quote``。
                quote_target = previous_id
            else:
                quote_target = ""

            sent_id = await self._send_follow_up_text(
                session_id,
                item.text,
                typing=typing,
                quote_target=quote_target,
                previous_message_id=previous_id,
                sync_history=item.slot not in recalled_slots,
            )
            if sent_id:
                previous_id = sent_id
                previous_text = item.text
                if item.slot >= 0:
                    sent_slots[item.slot] = sent_id
                    sent_slot_texts[item.slot] = item.text
            else:
                failed += 1
                self.ctx.logger.error("补发消息失败，跳过第 %s 条: %r", position, item.text[:40])

        if failed:
            self.ctx.logger.warning("补发结束：%s/%s 条失败（会话 %s）", failed, len(items), session_id)
        else:
            self.ctx.logger.info("补发完成：%s 条（会话 %s）", len(items), session_id)

    # ------------------------------------------------------------------
    # Hook 2a：后处理接管（错别字 + 分段，blocking）
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="post_process_takeover",
        description=(
            "blocking：框架【回复后处理总开关】关闭时，以与 MaiBot 完全一致的原逻辑接管"
            "错别字注入与分段，并把结果写回 response"
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

        多段发送开启时这里只回传**第 1 段**，其余分段登记到预分段缓存，由
        ``send_service.after_build_message`` + ``after_send`` 补发——复刻宿主"每段一条
        消息"的发送效果（首段仍走宿主原生发送链，引用/写库/历史同步不受影响）。

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
                choose_mode=self._choose_correction_mode,
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
        # （error_rate 太小 / min_freq 太高 / 词频表异常）；「真出现错字>0 但判定纠正=0」= 权重池
        # 抽到"不纠正"（正常）；「回复里有错字但真出现错字=0」= 宿主那半句 0.5 门控把整句换成了正确句。
        stats = dict(getattr(processor, "last_stats", {}) or {})
        # 整词同音替换因组合数超上限而跳过的**累计**次数（见 post_processing._MAX_HOMOPHONE_COMBINATIONS）：
        # 宿主原逻辑在长词上会卡死，本插件加了硬上限，非 0 就说明确实撞上了那个规模。
        try:
            combo_skips = int(getattr(processor._get_typo_generator(), "_word_combo_skips", 0) or 0)
        except Exception:  # 生成器还没建起来（本轮没走错字）→ 视为 0
            combo_skips = 0
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
            ("、整词替换跳过=%d(累计)" % combo_skips) if combo_skips else "",
        )

        session_id = str(modified.get("session_id") or "").strip()
        max_segments = max(int(self.config.response_splitter.max_segments), 0)
        # 新分支把纠正动作也变成独立消息；单段 + 有纠正时同样需要多段发送通道。
        has_extra_messages = bool(corrections) or bool(deferred)
        multi_ok = (
            self._multi_message_active()
            and (len(segments) > 1 or has_extra_messages)
            and (max_segments <= 0 or len(segments) <= max_segments)
        )

        if multi_ok and session_id:
            first_segment = segments[0]
            items = self._build_follow_up_items(segments, corrections, deferred)
            if items and self._store_prepared_segments(session_id, first_segment.text, items):
                modified["response"] = first_segment.text
                modified["skip_post_process"] = True
                modified["enable_splitter"] = True
                modified["enable_chinese_typo"] = True
                self.ctx.logger.info(
                    "后处理接管：共 %s 段%s，首段直发、登记 %s 条后续动作（会话 %s）",
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
            items.extend(self._build_correction_items(decision, deferred=True))
        return items

    @staticmethod
    def _build_correction_items(decision: Any, *, deferred: bool = False) -> List["FollowUpItem"]:
        """单个纠正动作 → 动作序列片段（``none`` 已在上游过滤掉）。

        ``deferred=True``（"最后纠正"）时动作排在序列**末尾**，因此不能再用"上一条"定位：
        引用改成 ``quote_slot``、撤回改成 ``recall_slot``，都指向它所属那一段的消息。
        纠正方式本身与即时纠正完全一致——"最后纠正"只改时机，不再强制引用。
        """
        mode = str(getattr(decision, "mode", "none"))
        slot = int(getattr(decision, "segment_index", -1))
        if mode == "direct":
            return [FollowUpItem(kind="text", text=str(decision.text))]
        if mode == "quote":
            if deferred:
                return [FollowUpItem(kind="text", text=str(decision.text), quote_slot=slot)]
            return [FollowUpItem(kind="text", text=str(decision.text), quote_previous=True)]
        if mode == "recall":
            # 撤回带错字的那条，然后重发修正后的整句（撤回了就必须给完整内容）。
            return [
                FollowUpItem(
                    kind="recall",
                    text=str(decision.sentence or decision.text),
                    recall_slot=slot if deferred else -1,
                )
            ]
        return []

    # Hook 2c：多段发送——登记待补发分段（blocking）
    # ------------------------------------------------------------------

    @HookHandler(
        "send_service.after_build_message",
        name="post_process_follow_up_registrar",
        description="多段发送：首段出站时登记剩余分段，交给 after_send 补发",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_after_build_message(self, **kwargs: Any) -> Dict[str, Any]:
        """只消费 Hook 1 登记的预分段缓存；未命中一律放行。

        未命中意味着这条出站消息不是"本轮回复的首段"（命令回执、其它插件用
        ``ctx.send.*`` 发出的消息、表情/图片等），天然跳过；补发期间的重入也在此挡掉。

        重入判据是"**这条文本是否正在被本插件补发**"，而不是会话级开关：补发窗口内
        同一会话里另一轮回复的首段必须照常登记，否则那一轮只会发出首段（静默丢尾部）。
        """
        if not self._multi_message_active():
            return {"action": "continue"}
        raw_message = kwargs.get("message")
        if not isinstance(raw_message, dict):
            return {"action": "continue"}
        message = dict(raw_message)
        session_id = str(kwargs.get("stream_id") or message.get("session_id") or "").strip()
        if not session_id:
            return {"action": "continue"}
        outbound_text = str(kwargs.get("processed_plain_text") or "").strip() or self._extract_outbound_text(message)
        if self._is_own_follow_up_text(session_id, outbound_text):
            # 本插件自己正在补发的分段：不消费预分段缓存（否则会把另一轮的待补发
            # 分段误取走），也不需要再次登记。
            self.ctx.logger.debug("补发分段自身的出站消息，跳过预分段登记（会话 %s）", session_id)
            return {"action": "continue"}
        items = self._take_prepared_segments(session_id, outbound_text)
        if not items:
            return {"action": "continue"}
        self._register_pending_follow_up(
            [
                str(message.get("message_id") or "").strip(),
                self._follow_up_tracking_key(session_id, message.get("timestamp"), outbound_text),
            ],
            {
                "session_id": session_id,
                "items": items,
                # 分段打字复刻宿主的 `typing=index > 0`：补发的分段一律带打字等待
                # （宿主那侧由【打字速度】决定等多久，0 = 不等）。
                "typing": True,
                # 首段正文：撤回反应时间留空时按它的打字时长自动决定（见 _recall_delay_seconds）。
                "first_text": outbound_text,
                "registered_at": time.monotonic(),
            },
        )
        return {"action": "continue"}

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
        """首段真的发出去之后才补发；首段失败则整轮放弃，避免只发出"尾巴"。"""
        if not self._multi_message_active():
            return
        raw_message = kwargs.get("message")
        if not isinstance(raw_message, dict):
            return
        message = dict(raw_message)
        session_id = str(message.get("session_id") or "").strip()
        if not session_id:
            return
        outbound_text = self._extract_outbound_text(message)
        if self._is_own_follow_up_text(session_id, outbound_text):
            # 本插件补发分段自己的 after_send：不触发下一批补发。
            return
        entry = self._resolve_pending_follow_up(
            [
                str(message.get("message_id") or "").strip(),
                self._follow_up_tracking_key(session_id, message.get("timestamp"), outbound_text),
            ]
        )
        if entry is None:
            return
        items = [item for item in entry.get("items") or [] if str(getattr(item, "text", "") or "").strip()]
        if not items:
            return
        if not bool(kwargs.get("sent")):
            self.ctx.logger.warning(
                "后处理接管：首段发送失败，放弃 %s 条后续动作（会话 %s）", len(items), session_id
            )
            return
        # 宿主在 after_send 之前已回填平台消息 id，可直接作为更正段的引用目标。
        previous_message_id = str(message.get("message_id") or "").strip()
        task = asyncio.create_task(
            self._run_follow_up_items(
                session_id,
                items,
                typing=bool(entry.get("typing", True)),
                previous_message_id=previous_message_id,
                first_text=str(entry.get("first_text") or ""),
            )
        )
        self._track_follow_up_task(session_id, task)

    # Hook 2e：多段发送——时序守卫（planner / 入站消息等待补发完成）
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.planner.before_request",
        name="post_process_follow_up_waiter",
        description="blocking：本轮补发未完成时先等待，避免下一轮 planner 上下文缺少后半段",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
        timeout_ms=_FOLLOW_UP_WAIT_HOOK_TIMEOUT_MS,
    )
    async def handle_planner_wait_for_follow_ups(self, **kwargs: Any) -> Dict[str, Any]:
        if not self._multi_message_active():
            return {"action": "continue"}
        session_id = str(kwargs.get("session_id") or "").strip()
        waited = await self._wait_for_stream_follow_ups(session_id)
        if waited:
            self.ctx.logger.info(
                "后处理接管：planner 请求等待 %s 个补发任务结束（会话 %s）", waited, session_id
            )
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
