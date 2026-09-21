"""模块前置条件：哪些功能模块需要宿主关闭对应能力才能工作。

设计原则：**只有"依赖宿主关闭对应能力"的模块才受制**。插件配置里把某个模块打开
≠ 它就能工作——宿主对应开关没关时，该模块必须**完全静默**（不介入、不改写、不发送），
避免与宿主原生能力重复处理同一条消息。

| 模块 | 需要的宿主前置条件 |
|---|---|
| `quote_reply`（引用回复接管，含私聊） | `chat.reply_style.enable_reply_quote = false` 且 `experimental.enable_rich_reply = false` |
| `post_process`（后处理接管，含多段发送） | `response_post_process.enable_response_post_process = false` 且 `experimental.enable_rich_reply = false` |

**不受前置条件约束**的模块（宿主开关任意状态都照常工作）：
`emoji_after_reply`（回复后表情包）、`emoji_follow`（表情包跟风）、
`emoji_meaning`（表情包含义库）、`text_rules`（文本替换规则）。

为什么两个接管都需要关掉「丰富回复」：开启后回复工具会给消息挂图片/表情/@ 等附件，
分段与引用语义都由富回复链路接管，插件的"首段文本"判定与引用注入会与宿主打架。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Tuple

# 丰富回复（宿主实验性开关）配置路径：两个接管模块都要它关闭。
RICH_REPLY_PATH = "experimental.enable_rich_reply"

# 模块键 -> ((宿主配置路径, 展示名, 需要的值), ...)
MODULE_REQUIREMENTS: Dict[str, Tuple[Tuple[str, str, Any], ...]] = {
    "quote_reply": (
        ("chat.reply_style.enable_reply_quote", "启用引用回复", False),
        (RICH_REPLY_PATH, "丰富回复", False),
    ),
    "post_process": (
        ("response_post_process.enable_response_post_process", "启用回复后处理", False),
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
}

# 不受宿主开关约束的模块（仅用于启动日志展示）。
UNRESTRICTED_MODULES: Tuple[str, ...] = (
    "emoji_after_reply",
    "emoji_follow",
    "emoji_meaning",
    "text_rules",
)

# 前置条件判定需要读取的全部宿主配置路径（按声明顺序去重）。
REQUIRED_HOST_PATHS: Tuple[str, ...] = tuple(
    dict.fromkeys(path for requirements in MODULE_REQUIREMENTS.values() for path, _, _ in requirements)
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


def evaluate_module(
    key: str,
    host_values: Mapping[str, Any],
    *,
    rich_reply_gate: bool = True,
) -> ModuleStatus:
    """判定单个模块是否可用。

    ``host_values`` 为宿主全局配置的快照（按**完整配置路径**取值）。
    读取失败/缺失（值为 ``None``）时按**未满足**处理，让模块保持静默而不是冒险介入。

    ``rich_reply_gate=False``（对应插件配置 ``[plugin] rich_reply_gate`` 关闭）时，
    不再把「丰富回复」当硬门槛：宿主开着丰富回复也允许接管，状态行里会注明
    "丰富回复未做门控"，提醒此时附件会挂在首段、补发分段不带附件。
    """
    label = MODULE_LABELS.get(key, key)
    requirements = MODULE_REQUIREMENTS.get(key)
    if not requirements:
        return ModuleStatus(key=key, label=label, available=True, restricted=False)

    unmet: List[str] = []
    met: List[str] = []
    for path, name, expected in requirements:
        if path == RICH_REPLY_PATH and not rich_reply_gate:
            met.append(f"{name}未做门控")
            continue
        actual = host_values.get(path, None)
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
