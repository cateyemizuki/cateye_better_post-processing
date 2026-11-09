"""模块前置条件：哪些功能模块需要宿主关闭对应能力才能工作。

设计原则：**只有"依赖宿主关闭对应能力"的模块才受制**。插件配置里把某个模块打开
≠ 它就能工作——宿主对应开关没关时，该模块必须**完全静默**（不介入、不改写、不发送），
避免与宿主原生能力重复处理同一条消息。

| 模块 | 需要的宿主前置条件 |
|---|---|
| `quote_reply`（引用回复接管，含私聊） | `chat.reply_style.enable_reply_quote = false`（新版宿主已移除该字段，读取不到 = 按关闭放行），且丰富回复**未开启**（开关读取不到 = 未开启） |
| `post_process`（后处理接管，含多段发送） | 宿主**不在做错别字处理**（二选一）：`response_post_process.enable_response_post_process = false`（完整接管，1.0.0 起）；**或** `chinese_typo.enable = false`（错别字接管：宿主后处理开启但错别字关闭，由本插件接管错别字与分段，1.1.1 起）。且丰富回复**未开启** |

**丰富回复的判定口径**（1.3.4 起）：宿主新版已移除 `experimental.enable_rich_reply`
字段，读取结果为 `None`——此时按「丰富回复能力未开启」处理，两个接管**不再静默**。
只有明确读到 `True` 才算开启、才要求关闭（见 :func:`rich_reply_enabled`）。

**「启用引用回复」的判定口径**（1.4.1 起）：宿主新版（1.3.x 后期起）已移除
`chat.reply_style.enable_reply_quote`，改用群聊/私聊「引用频率」控制引用与否，读取结果
同样是 `None`——此时按「宿主不再把引用与否交给这个开关」处理，即**视为关闭**，引用回复
接管照常生效；只有明确读到 `True` 才按「宿主自己做引用回复、本插件静默」处理。
读不到却按 `True` 兜底会让接管在新宿主上永远静默（1.4.1 修复前正是如此）。

**不受前置条件约束**的模块（宿主开关任意状态都照常工作）：
`emoji_after_reply`（回复后表情包）、`emoji_follow`（表情包跟风）、
`emoji_meaning`（表情包含义库）、`text_rules`（文本替换规则）、
`empty_reply_fallback`（回复兜底，默认关闭）、
`self_reply_guard` / `duplicate_reply_guard`（1.2.0 合并的自回复/重复回复拦截，默认关闭）、
`cross_chat_recall`（1.2.0 合并的跨聊私聊回忆，默认关闭）。

为什么丰富回复开启时两个接管都不工作：开启后回复工具会给消息挂图片/表情/@ 等附件，
分段与引用语义都由富回复链路接管，插件的"首段文本"判定与引用注入会与宿主打架。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

# 丰富回复（宿主实验性开关）配置路径：两个接管模块都要它不开启。
# 新版宿主已移除该字段：读取不到 = 能力未开启（见 rich_reply_enabled）。
RICH_REPLY_PATH = "experimental.enable_rich_reply"

# 「启用引用回复」（宿主旧版开关）配置路径：宿主明确关掉时由本插件接管回复方式。
# 新版宿主已移除该字段、改由群聊/私聊「引用频率」控制：读取不到 = 按关闭放行
# （见 evaluate_module 里的 REPLY_QUOTE 分支与模块文档的口径说明）。
REPLY_QUOTE_PATH = "chat.reply_style.enable_reply_quote"

# 后处理接管的两个宿主开关（1.1.1 起「二选一」）：
# 总开关关闭 → 完整接管（错别字 + 分段，宿主复刻路径）；
# 总开关开启但宿主错别字关闭 → 错别字接管（宿主原生错别字不会运行，由本插件接管）。
POST_PROCESS_MASTER_PATH = "response_post_process.enable_response_post_process"
POST_PROCESS_TYPO_PATH = "chinese_typo.enable"

# 模块键 -> ((宿主配置路径, 展示名, 需要的值), ...)
# `post_process` 的门控是「二选一」的 OR 条件，不走这张表（见 _evaluate_post_process）。
MODULE_REQUIREMENTS: Dict[str, Tuple[Tuple[str, str, Any], ...]] = {
    "quote_reply": (
        (REPLY_QUOTE_PATH, "启用引用回复", False),
        (RICH_REPLY_PATH, "丰富回复", False),
    ),
}

MODULE_LABELS: Dict[str, str] = {
    "quote_reply": "引用回复接管",
    "post_process": "后处理接管",
    "emoji_after_reply": "回复后表情包",
    "emoji_follow": "表情包跟风",
    "emoji_meaning": "表情包含义库",
    "text_rules": "文本替换规则",
    "empty_reply_fallback": "回复兜底",
    "self_reply_guard": "自回复拦截",
    "duplicate_reply_guard": "重复回复拦截",
    "cross_chat_recall": "跨聊私聊回忆",
}

# 不受宿主开关约束的模块（用于启动日志与 /bpp 状态展示）。
UNRESTRICTED_MODULES: Tuple[str, ...] = (
    "emoji_after_reply",
    "emoji_follow",
    "emoji_meaning",
    "text_rules",
    "empty_reply_fallback",
    # 1.2.0 阶段 3 合并的三个模块（默认关闭；不依赖宿主关闭任何能力）
    "self_reply_guard",
    "duplicate_reply_guard",
    "cross_chat_recall",
)

# 前置条件判定需要读取的全部宿主配置路径（按声明顺序去重）。
REQUIRED_HOST_PATHS: Tuple[str, ...] = tuple(
    dict.fromkeys(
        (
            POST_PROCESS_MASTER_PATH,
            POST_PROCESS_TYPO_PATH,
            *(path for requirements in MODULE_REQUIREMENTS.values() for path, _, _ in requirements),
        )
    )
)


@dataclass(frozen=True)
class ModuleStatus:
    """单个模块的可用性判定结果。"""

    key: str
    label: str
    available: bool
    restricted: bool = True
    unmet: Tuple[str, ...] = ()
    met: Tuple[str, ...] = ()

    def describe(self) -> str:
        """一行式状态描述，用于启动/配置变更日志。"""
        if not self.restricted:
            return f"{self.label}：可用（不受宿主开关约束）"
        if self.available:
            detail = "、".join(self.met) if self.met else "前置条件已满足"
            return f"{self.label}：可用（{detail}）"
        detail = "；".join(self.unmet) if self.unmet else "前置条件未满足"
        return f"{self.label}：静默（{detail}）"


def _show(value: Any) -> str:
    """宿主配置值的展示形态（读取失败显示「未读取到」）。"""
    return "未读取到" if value is None else repr(value)


def normalize_reply_quote(value: Any) -> Tuple[Optional[bool], bool]:
    """把「启用引用回复」的读取结果归一成 (缓存值, 生效开关)。

    新版宿主已移除 chat.reply_style.enable_reply_quote（改用群聊/私聊「引用频率」），
    读取结果是 None。此时按「宿主不再把引用与否交给这个开关」处理：

    * 明确读到 bool → 缓存值 = 该 bool，生效开关同值；
    * 读不到（None）或错误 dict 等其他形态 → 缓存值 None（requirements 据此区分
      「未读取到」的放行原因），生效开关 **False**（接管照常生效，不被静默）。

    读不到却按 True 兜底会让引用回复接管在新宿主上永远静默（1.4.1 修复前正是如此）。
    """
    if isinstance(value, bool):
        return value, value
    return None, False


def rich_reply_enabled(host_values: Mapping[str, Any]) -> bool:
    """宿主「丰富回复」是否开启：**只有明确读到 True 才算开启**。

    宿主新版已移除 ``experimental.enable_rich_reply`` 字段，读取结果是 ``None``——
    这时按「丰富回复能力未开启」处理，两个接管照常工作；只有旧的宿主才可能返回
    ``True``（此时保持原有门控语义：要求用户去关掉）。
    """
    return host_values.get(RICH_REPLY_PATH) is True


def _evaluate_post_process(
    host_values: Mapping[str, Any],
    *,
    rich_reply_gate: bool = True,
) -> ModuleStatus:
    """后处理接管的门控：**宿主不在做错别字处理**时接管（两条满足路径二选一）。

    * 宿主后处理总开关关闭（``enable_response_post_process = false``）→ **完整接管**
      （错别字 + 分段，1.0.0 起的原行为）；
    * 宿主总开关开启但宿主错别字关闭（``chinese_typo.enable = false``）→ **错别字接管**
      （宿主原生错别字不会运行，接管后本插件置 ``skip_post_process=True`` 跳过宿主处理，
      分段与错别字都由本插件按宿主参数复刻执行，1.1.1 起）。

    另需满足「丰富回复」门控（``rich_reply_gate=False`` 时不把关当硬门槛）。
    丰富回复**读取不到视为未开启**（新版宿主已移除该字段）；后处理两个开关都读取失败时
    仍按**未满足**处理，让模块保持静默而不是冒险介入。
    """
    label = MODULE_LABELS["post_process"]
    unmet: List[str] = []
    met: List[str] = []

    rich = host_values.get(RICH_REPLY_PATH)
    if not rich_reply_gate:
        met.append("丰富回复未做门控")
    elif rich is True:
        unmet.append(f"需在宿主配置关闭「丰富回复」（{RICH_REPLY_PATH}，当前 True）")
    elif rich is False:
        met.append("丰富回复已关闭")
    else:
        met.append("丰富回复开关未读取到（视为未开启）")

    master = host_values.get(POST_PROCESS_MASTER_PATH)
    typo = host_values.get(POST_PROCESS_TYPO_PATH)
    if master is False:
        met.append("启用回复后处理已关闭（完整接管：错别字 + 分段）")
    elif typo is False:
        met.append("启用错别字已关闭（错别字接管：宿主后处理开启时由本插件接管错别字与分段）")
    else:
        unmet.append(
            "需在宿主配置关闭「启用回复后处理」（"
            f"{POST_PROCESS_MASTER_PATH}，当前 {_show(master)}），"
            "或关闭「启用错别字」（"
            f"{POST_PROCESS_TYPO_PATH}，当前 {_show(typo)}）"
        )

    return ModuleStatus(
        key="post_process",
        label=label,
        available=not unmet,
        restricted=True,
        unmet=tuple(unmet),
        met=tuple(met),
    )


def evaluate_module(
    key: str,
    host_values: Mapping[str, Any],
    *,
    rich_reply_gate: bool = True,
) -> ModuleStatus:
    """判定单个模块是否可用。

    ``host_values`` 为宿主全局配置的快照（按**完整配置路径**取值）。
    普通宿主开关读取失败/缺失（值为 ``None``）时按**未满足**处理，让模块保持静默而不是
    冒险介入；两个例外都是"新版宿主已移除的旧开关"：「丰富回复」读取不到 = **未开启**，
    「启用引用回复」读取不到 = **关闭**（宿主不再自己做引用回复）——否则两个接管会在新版
    宿主上永远静默。

    ``rich_reply_gate=False``（对应插件配置 ``[replyer_post_process] rich_reply_gate`` 关闭，
    1.4.5 前住在 ``[plugin]``）时，
    不再把「丰富回复」当硬门槛：宿主开着丰富回复也允许接管，状态行里会注明
    "丰富回复未做门控"，提醒此时附件会挂在首段、补发分段不带附件。
    """
    label = MODULE_LABELS.get(key, key)
    if key == "post_process":
        return _evaluate_post_process(host_values, rich_reply_gate=rich_reply_gate)

    requirements = MODULE_REQUIREMENTS.get(key)
    if not requirements:
        return ModuleStatus(key=key, label=label, available=True, restricted=False)

    unmet: List[str] = []
    met: List[str] = []
    for path, name, expected in requirements:
        actual = host_values.get(path, None)
        if path == RICH_REPLY_PATH:
            # 丰富回复：门控关闭 / 明确关着 / 读不到（新版宿主已移除该字段）都放行，
            # 只有明确读到 True 才要求去关掉。
            if not rich_reply_gate:
                met.append(f"{name}未做门控")
            elif actual is False:
                met.append(f"{name}已关闭")
            elif isinstance(actual, bool):
                unmet.append(f"需在宿主配置关闭「{name}」（{path}，当前 True）")
            else:
                met.append(f"{name}开关未读取到（视为未开启）")
            continue
        if path == REPLY_QUOTE_PATH:
            # 「启用引用回复」：新版宿主已移除该字段（改用群聊/私聊「引用频率」），
            # 读取不到时按"宿主不再自己做引用回复"放行；只有明确读到 True 才要求去关掉。
            if actual is False:
                met.append(f"{name}已关闭")
            elif isinstance(actual, bool):
                unmet.append(f"需在宿主配置关闭「{name}」（{path}，当前 True）")
            else:
                met.append(f"{name}开关未读取到（新版宿主已移除，视为宿主不再做引用回复）")
            continue
        if actual is expected:  # 严格比较：必须是 False 本身，错误 dict/None 都不算满足
            met.append(f"{name}已关闭")
        else:
            shown = "未读取到" if actual is None else repr(actual)
            unmet.append(f"需在宿主配置关闭「{name}」（{path}，当前 {shown}）")
    return ModuleStatus(
        key=key,
        label=label,
        available=not unmet,
        restricted=True,
        unmet=tuple(unmet),
        met=tuple(met),
    )


def iter_module_statuses(
    host_values: Mapping[str, Any],
    *,
    rich_reply_gate: bool = True,
) -> List[ModuleStatus]:
    """返回全部模块（受制 + 不受制）的状态对象，供日志按级别分类输出。"""
    statuses = [
        evaluate_module(key, host_values, rich_reply_gate=rich_reply_gate)
        for key in MODULE_REQUIREMENTS
    ]
    statuses.append(evaluate_module("post_process", host_values, rich_reply_gate=rich_reply_gate))
    for key in UNRESTRICTED_MODULES:
        statuses.append(evaluate_module(key, host_values, rich_reply_gate=rich_reply_gate))
    return statuses


def describe_all(host_values: Mapping[str, Any], *, rich_reply_gate: bool = True) -> List[str]:
    """返回全部模块的状态描述行（脚本/调试用）。

    插件自身的启动与配置变更日志走 :func:`iter_module_statuses`，因为需要按级别区分
    输出（可用 = ``info``、静默 = ``warning``）。
    """
    return [
        status.describe()
        for status in iter_module_statuses(host_values, rich_reply_gate=rich_reply_gate)
    ]
