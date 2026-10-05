"""``/bpp`` 状态命令模块（1.0.0）：模块状态一图流 + 异常兜底提示词测试。

提供两个子命令（同一个 ``@Command`` 组件，正则捕获子命令）：

* ``/bpp``——把插件当前各模块的状态**用合并转发发送**（``send.forward`` 单节点）：
  状态先渲染成**一图流**（``render.html2png``，HTML 卡片 → PNG，节点里放 image 段），
  **渲染失败时退回文字**（节点里放 text 段）；合并转发本身失败时再逐级退回
  ``send.image`` / ``send.text`` 直发（官方文档建议的兼容口径：适配器可能不支持转发）。
* ``/bpp fallback``——对**命令引用的那条消息**触发一次异常兜底测试：按
  ``[response_splitter] fallback_prompts`` 的当前配置抽一条兜底提示词
  （留空 = 宿主自带的那组），替换 ``{bot_name}`` / ``{user_name}`` 占位后
  **以引用回复的方式**发给被引用消息的发送者所在会话——所见即真实兜底触发时的效果。
  测试消息不写入 Maisaka 对话上下文（``sync_to_maisaka_history=False``），避免污染 bot 记忆。

两条子命令共用一个 ``@Command`` 组件，**仅 operator 可触发**（``permission="operator"``，
官方文档 §7.2 方式一：宿主按 ``plugin.permission`` 名单 + ``command_permissions`` 判定，
本地控制台天然放行；非 operator 在宿主侧即被拦截）——命令会消耗渲染 / 转发资源，
fallback 更会让 bot 真实发消息，不对普通群成员开放。``/bpp`` 状态查询**有意不受插件
总开关限制**（排查"为什么没生效"正需要它）；``/bpp fallback`` 会真实发消息，插件总开关
关闭时直接拦截。

状态行综合两个维度：**插件侧开关**（配置里开没开）与**宿主前置条件**
（``modules.requirements`` 的判定，两个接管需要宿主关闭对应能力）——
"配置开了但宿主开关没关"正是最容易被问到的"装了没效果"，必须一眼能看出来。

本模块以 **mixin** 形式被插件类继承（与两个接管模块同一模式）；依赖的共享基础设施
（``self._cfg`` / ``self.config`` / ``_lookup_reply_target`` / ``_send_follow_up_text`` /
``_resolve_mirror`` / ``_custom_fallback_prompts``）分别来自 plugin.py 与两个接管 mixin。
"""

from __future__ import annotations

import html
import random
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from maibot_sdk import Command

from ..post_processing import build_default_fallback_replies

# 一图流的渲染参数：固定宽度、**按卡片元素截取**（高度贴合内容，见 _render_status_image）、
# 2x 缩放保证清晰度。
# allow_network 保持默认 False（宿主默认禁外网），HTML 里只用内联 CSS、不引用外部资源。
_RENDER_VIEWPORT_WIDTH = 780
_RENDER_DEVICE_SCALE_FACTOR = 2.0

# 合并转发节点的展示昵称（转发气泡里显示的名字）。
_FORWARD_NICKNAME = "更好的消息后处理"

# 状态徽标的四态（按判定优先级排序）：静默（宿主前置未满足，优先于已关闭展示） /
# 已关闭（插件配置） / 异常（含义库初始化失败） / 可用。
_STATE_AVAILABLE = "可用"
_STATE_DISABLED = "已关闭"
_STATE_SILENT = "静默"
_STATE_ERROR = "异常"

# ``/bpp fallback`` 的 ``{user_name}`` 占位值清理：它来自被引用消息的发送者昵称/群名片
# （**他人可控文本**），替换后会由 bot 真实发出——长度截断 + 剔除不可打印字符
# （控制字符 / ANSI 序列不进聊天窗口与宿主日志），与未知子命令回显的加固同口径。
_PLACEHOLDER_NAME_MAX_CHARS = 24


def _sanitize_placeholder_name(name: str) -> str:
    """占位符用的第三方可控文本清理：剔除非可打印字符后截断到上限长度。"""
    cleaned = "".join(ch for ch in str(name or "") if ch.isprintable())
    return cleaned.strip()[:_PLACEHOLDER_NAME_MAX_CHARS]


class StatusCommandMixin:
    """``/bpp`` 命令的处理器与状态收集 / 渲染 / 发送逻辑。"""

    # ------------------------------------------------------------------
    # 命令入口
    # ------------------------------------------------------------------

    @Command(
        "bpp",
        description=(
            "查看「更好的消息后处理」插件各模块状态（合并转发 + 一图流，渲染失败退回文字）；"
            "/bpp fallback 引用一条消息可实测异常兜底提示词的抽取与占位替换效果"
        ),
        pattern=r"(?<!\S)/?bpp(?:\s+(?P<sub>[^\s]+))?\s*$",
        # 权限门槛（官方文档 §7.2 方式一，主程序判定）：命令会消耗一次 HTML 渲染 + 转发 /
        # 直发（/bpp），fallback 还会让 bot 真实发出一条消息并触发 get_by_id 查询——
        # 不能对普通群成员开放。仅 operator（宿主 plugin.permission 名单 +
        # command_permissions，本地控制台天然放行）可触发；非 operator 在宿主侧即被拦截。
        permission="operator",
    )
    async def handle_bpp_command(self, **kwargs: Any) -> Tuple[bool, str, bool]:
        """``/bpp`` 与 ``/bpp fallback`` 的统一入口。

        正则用 ``(?<!\\S)`` 负向后顾代替 ``^`` 锚点：**引用回复**时宿主的
        ``processed_plain_text`` = 被引用内容 + 命令文本，命令不在开头，``^`` 会失配
        （官方文档 §7.2 的安全写法）。子命令捕获任意单词，未知的子命令回一条用法提示
        （比"匹配不上、消息被当成聊天内容喂给 LLM"更符合直觉）。
        """
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not stream_id:
            return False, "缺少 stream_id", True
        matched_groups = kwargs.get("matched_groups")
        sub = ""
        if isinstance(matched_groups, dict):
            sub = str(matched_groups.get("sub") or "").strip()
        sub_l = sub.lower()
        try:
            if not sub_l:
                await self._bpp_send_status(stream_id)
                return True, "bpp status", True
            if sub_l == "fallback":
                await self._bpp_fallback_test(kwargs, stream_id)
                return True, "bpp fallback test", True
            # 回显加固：保留原始大小写、截断到 24 字符并剔除不可打印字符
            # （防控制字符 / ANSI 序列进聊天窗口与宿主日志；sub 来自触发者自己的输入）。
            shown = "".join(ch for ch in sub[:24] if ch.isprintable())
            await self.ctx.send.text(
                f"未知的子命令「{shown}」。用法：/bpp 查看模块状态；"
                "/bpp fallback 引用一条消息测试异常兜底提示词。",
                stream_id,
                sync_to_maisaka_history=False,
            )
            return False, f"unknown subcommand: {shown}", True
        except Exception as exc:
            # 命令处理器抛异常时 Runner 会返回 (False, str(exc), True)，但用户侧什么都看不到
            # ——这里自己兜底发一条错误提示，免得"发了命令没反应"。文案**固定**、不带异常
            # 细节：异常文本可能含宿主路径 / 配置键，会出现在聊天窗口的内容一律不放；
            # 详情只进日志（上面 warning 已带 exc_info）。
            self.ctx.logger.warning("/bpp 执行失败: %s", exc, exc_info=True)
            try:
                await self.ctx.send.text(
                    "/bpp 执行失败，详情请查看宿主日志。", stream_id, sync_to_maisaka_history=False
                )
            except Exception:
                pass
            return False, "bpp 执行失败（详情见日志）", True

    # ------------------------------------------------------------------
    # 状态收集
    # ------------------------------------------------------------------

    def _plugin_version(self) -> str:
        """插件版本（= ``plugin.py`` 的 ``SUPPORTED_CONFIG_VERSION``，与 manifest 同步）。

        延迟导入避免循环（plugin.py 在模块级导入本 mixin）。
        """
        try:
            from ..plugin import SUPPORTED_CONFIG_VERSION

            return str(SUPPORTED_CONFIG_VERSION)
        except Exception:
            return "未知"

    def _collect_module_rows(self) -> List[Dict[str, str]]:
        """收集各模块的状态行：``{label, state, detail}``（顺序与启动日志一致）。

        状态综合两个维度：

        * **宿主前置条件**（``iter_module_statuses``）：两个接管需要宿主关闭对应能力，
          未满足 = ``静默``（detail 里带上宿主要关哪个开关）；
        * **插件侧开关**：配置里关掉的模块 = ``已关闭``。

        判定顺序：宿主前置未满足 → 静默（最容易被误认为"坏了"，优先展示）；
        插件开关关闭 → 已关闭；其余 → 可用。含义库初始化失败单独标 ``异常``。
        """
        rows: List[Dict[str, str]] = []
        statuses = self._iter_statuses()
        for status in statuses:
            key = str(getattr(status, "key", ""))
            if key == "post_process":
                # 1.1.1 起后处理接管的两个子模块（分段 / 错别字）不完全绑定、可独立开关，
                # 状态行拆成两条分别展示（含未接管原因），不再合并成一条"后处理接管"。
                rows.extend(self._post_process_status_rows(status))
                continue
            label = str(getattr(status, "label", key))
            restricted = bool(getattr(status, "restricted", False))
            available = bool(getattr(status, "available", False))
            plugin_switch, switch_detail = self._module_plugin_switch(key)
            if restricted and not available:
                state = _STATE_SILENT
                detail = "；".join(getattr(status, "unmet", ()) or ()) or "宿主前置条件未满足"
                if switch_detail:
                    detail = f"{switch_detail}；{detail}"
            elif plugin_switch is False:
                state = _STATE_DISABLED
                detail = switch_detail or "插件配置已关闭"
            elif key == "emoji_meaning" and self._meaning_store is None:
                state = _STATE_ERROR
                detail = "含义库初始化失败（详见启动日志），补录与注入不可用"
            else:
                state = _STATE_AVAILABLE
                if restricted:
                    met = "、".join(getattr(status, "met", ()) or ()) or "宿主前置条件已满足"
                    detail = f"{switch_detail}；{met}" if switch_detail else met
                else:
                    detail = switch_detail or "不受宿主开关约束"
            rows.append({"label": label, "state": state, "detail": detail})
        return rows

    def _post_process_status_rows(self, status: Any) -> List[Dict[str, str]]:
        """后处理接管拆成的两条状态行：**分段接管** / **错别字接管**。

        两条行各自回答"该职责是否由本插件从宿主手中接管"，判定与**描述文本**都互相
        独立（各行把职责、原因、开关来源一次说全，不引用共享的门控文案）：

        * **错别字接管**：两种接管路径下都归本插件——完整接管（宿主总开关关闭）按
          ``[chinese_typo] enable`` 镜像判定；错别字接管模式（宿主总开关开启、宿主错别字
          关闭）下「跟随宿主」视为开启（宿主值恒为关，照抄会让接管形同虚设），与
          ``_get_processor`` 对 ``typo_enable`` 的实际取值同口径，显示不骗人。
        * **分段接管**：只有**完整接管**（宿主总开关关闭、宿主分段功能已停用）才由本插件
          替代宿主分段，按 ``[response_splitter] enable`` 镜像判定；**错别字接管模式下
          分段职责仍归宿主**——本插件跳过宿主处理是为了承载错字纠正，分段按宿主参数
          复刻执行、分段配置不被接管，因此该行显示静默并注明，而非"可用"。
        """
        restricted = bool(getattr(status, "restricted", False))
        available = bool(getattr(status, "available", False))
        unmet = "；".join(getattr(status, "unmet", ()) or ())
        if restricted and not available:
            # 宿主正在做错别字处理（或其他前置未满足）：两个职责都接不过来，各行写各的原因。
            seg_detail = "宿主后处理开启，分段由宿主执行；如需本插件接管分段，请关闭宿主「启用回复后处理」"
            if unmet:
                seg_detail = f"{seg_detail}（前置：{unmet}）"
            typo_detail = (
                f"宿主在自行处理错别字，本插件不介入（{unmet or '宿主前置条件未满足'}）；"
                "如需接管，请关闭宿主「启用回复后处理」或「启用错别字」"
            )
            return [
                {"label": "分段接管", "state": _STATE_SILENT, "detail": seg_detail},
                {"label": "错别字接管", "state": _STATE_SILENT, "detail": typo_detail},
            ]
        if not bool(self.config.response_splitter.takeover):
            prompts = self._custom_fallback_prompts()
            extra = f"；自定义兜底提示词 {len(prompts)} 条" if prompts else ""
            return [
                {
                    "label": "分段接管",
                    "state": _STATE_DISABLED,
                    "detail": "插件接管开关已关闭（[response_splitter] takeover），分段保持宿主行为",
                },
                {
                    "label": "错别字接管",
                    "state": _STATE_DISABLED,
                    "detail": (
                        "插件接管开关已关闭，错别字保持宿主行为；"
                        f"接管开启后纠正方式恒由权重池决定{extra}"
                    ),
                },
            ]
        typo_only_mode = self._typo_takeover_mode()
        # 错别字接管行：两种接管路径都由本插件接管错别字，按错字镜像判定生效开关。
        typo_explicit = self._mirror_is_explicit("chinese_typo", "enable")
        if typo_only_mode and not typo_explicit:
            typo_on = True
            typo_source = "错别字接管模式，「跟随宿主」视为开"
        else:
            typo_on = bool(
                self._resolve_mirror("chinese_typo", "enable", "chinese_typo.enable", "switch")
            )
            typo_source = "插件值" if typo_explicit else "跟随宿主"
        # 分段接管行：只有完整接管（宿主总开关关闭）才真正替代宿主的分段职责；
        # 错别字接管模式下分段归宿主，本插件仅复刻执行（见行文案），不算接管。
        if typo_only_mode:
            seg_row = {
                "label": "分段接管",
                "state": _STATE_SILENT,
                "detail": (
                    "错别字接管模式：分段职责仍归宿主（本插件按宿主参数复刻执行、"
                    "仅为承载错字纠正，分段配置不受接管影响）"
                ),
            }
            typo_detail = (
                "错别字接管：宿主「启用错别字」已关闭，错别字由本插件接管"
                f"（纠正方式恒由权重池决定）；错字开关={'开' if typo_on else '关'}（{typo_source}）"
            )
        else:
            seg_explicit = self._mirror_is_explicit("response_splitter", "enable")
            seg_on = bool(
                self._resolve_mirror("response_splitter", "enable", "response_splitter.enable", "switch")
            )
            seg_row = {
                "label": "分段接管",
                "state": _STATE_AVAILABLE if seg_on else _STATE_DISABLED,
                "detail": (
                    "完整接管：宿主「启用回复后处理」已关闭，分段由本插件替代宿主执行；"
                    f"分段开关={'开' if seg_on else '关'}（{'插件值' if seg_explicit else '跟随宿主'}）"
                ),
            }
            typo_detail = (
                "完整接管：错别字由本插件接管（纠正方式恒由权重池决定）；"
                f"错字开关={'开' if typo_on else '关'}（{typo_source}）"
            )
        return [
            seg_row,
            {
                "label": "错别字接管",
                "state": _STATE_AVAILABLE if typo_on else _STATE_DISABLED,
                "detail": typo_detail,
            },
        ]

    def _iter_statuses(self) -> List[Any]:
        """全部模块的宿主前置条件判定结果（复用启动日志同一套逻辑）。"""
        from .requirements import iter_module_statuses

        return iter_module_statuses(self._cfg, rich_reply_gate=self._rich_reply_gate())

    def _module_plugin_switch(self, key: str) -> Tuple[Optional[bool], str]:
        """模块对应的**插件侧**开关状态与一句展示详情；没有开关概念时返回 ``(None, 描述)``。"""
        cfg = self.config
        if key == "quote_reply":
            group_on = bool(cfg.quote_reply.takeover)
            private_on = bool(cfg.quote_reply_private.takeover)
            enabled = group_on or private_on
            return enabled, f"群聊接管={'开' if group_on else '关'}、私聊接管={'开' if private_on else '关'}"
        if key == "post_process":
            enabled = bool(cfg.response_splitter.takeover)
            prompts = self._custom_fallback_prompts()
            fallback_detail = f"自定义兜底提示词 {len(prompts)} 条" if prompts else "兜底提示词=宿主自带"
            return enabled, f"接管={'开' if enabled else '关'}；{fallback_detail}"
        if key == "emoji_after_reply":
            enabled = bool(cfg.emoji_after_reply.enabled)
            return enabled, f"补发概率={cfg.emoji_after_reply.probability:.2f}"
        if key == "emoji_follow":
            enabled = bool(cfg.emoji_follow.enabled)
            return enabled, f"mode={cfg.emoji_follow.mode}、触发条数={cfg.emoji_follow.threshold}"
        if key == "emoji_meaning":
            enabled = bool(cfg.emoji_meaning.enabled)
            count = self._meaning_store.count() if self._meaning_store is not None else 0
            scope = "、".join(str(item) for item in (cfg.emoji_meaning.rewrite_scope or [])) or "不改写"
            return enabled, f"已收录 {count} 条；标签替换范围={scope}"
        if key == "text_rules":
            parsed = self._parsed_rules
            total = len(parsed.replaces) + len(parsed.covers)
            detail = (
                f"replace={len(parsed.replaces)}、cover={len(parsed.covers)}"
                f"、忽略={len(parsed.invalid)}；范围={cfg.text_rules.apply_scope}"
            )
            # 文本规则没有独立开关：按"有没有生效规则"判定——一条都没有（或全部因
            # 格式错误被忽略）= 已关闭；有生效规则 = 可用（detail 里始终带条数明细）。
            return total > 0, detail
        if key == "empty_reply_fallback":
            enabled = bool(getattr(cfg.empty_reply_fallback, "enabled", False))
            # 兜底文本与「异常兜底提示词」共用一份列表，这里点明，方便排查"补出来的是什么"。
            prompts = self._custom_fallback_prompts()
            source = f"自定义 {len(prompts)} 条" if prompts else "宿主自带兜底"
            return enabled, f"兜底文本={source}"
        return None, ""

    def _collect_header_facts(self) -> List[Tuple[str, str]]:
        """卡片头部的全局事实（键值对）：总开关 / 门控 / 宿主丰富回复 / 版本。"""
        rich_reply = self._cfg.get("experimental.enable_rich_reply")
        if rich_reply is None:
            rich_reply_text = "未读取到"
        else:
            rich_reply_text = "开" if rich_reply else "关"
        gate_on = self._rich_reply_gate()
        return [
            ("插件版本", self._plugin_version()),
            ("总开关", "开" if self._plugin_enabled() else "关"),
            ("丰富回复门控", "开" if gate_on else "关（宿主开着丰富回复也接管）"),
            ("宿主丰富回复", rich_reply_text),
            ("异常兜底提示词", self._fallback_prompts_summary()),
        ]

    def _fallback_prompts_summary(self) -> str:
        """异常兜底提示词配置的一句摘要。"""
        prompts = self._custom_fallback_prompts()
        if not prompts:
            return "宿主自带（未自定义）"
        return f"自定义 {len(prompts)} 条"

    # ------------------------------------------------------------------
    # 文字版与一图流
    # ------------------------------------------------------------------

    def _build_status_text(self) -> List[str]:
        """状态的文字版（逐行）：渲染失败时作为合并转发节点的 text 段，也是纯文本预览。"""
        lines: List[str] = [f"【更好的消息后处理 v{self._plugin_version()} · 模块状态】"]
        lines.extend(f"{name}：{value}" for name, value in self._collect_header_facts())
        lines.append("──────────")
        state_icons = {
            _STATE_AVAILABLE: "✔",
            _STATE_DISABLED: "○",
            _STATE_SILENT: "⚠",
            _STATE_ERROR: "✖",
        }
        for row in self._collect_module_rows():
            icon = state_icons.get(row["state"], "·")
            lines.append(f"{icon} {row['label']}：{row['state']}（{row['detail']}）")
        lines.append("──────────")
        lines.append(f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("提示：/bpp fallback 引用一条消息，可实测异常兜底提示词。")
        return lines

    def _render_status_html(self) -> str:
        """把状态渲染成一图流用的 HTML 卡片（全部内联 CSS，不引用任何外部资源）。

        宿主 ``render.html2png`` 默认 ``allow_network=False``，外部图片/字体/CSS 都不会加载，
        因此样式必须完全内联；所有动态文本一律过 ``html.escape``（模块状态里会出现
        宿主配置路径、用户昵称等不可信内容）。
        """
        state_colors = {
            _STATE_AVAILABLE: ("#1a7f37", "#e6f4ea"),
            _STATE_DISABLED: ("#57606a", "#eaeef2"),
            _STATE_SILENT: ("#9a6700", "#fff8c5"),
            _STATE_ERROR: ("#cf222e", "#ffebe9"),
        }
        parts: List[str] = [
            "<!DOCTYPE html><html><head><meta charset='utf-8'><style>",
            "body{margin:0;background:transparent;}",
            # 卡片宽度取 100%：铺满截图宽度（视口 780px，若出现经典滚动条则自动收窄），
            # 不留右侧空带——固定 px 宽度小于视口时，多出的 body 透明区域会渲染成白色竖条。
            ".card{width:100%;box-sizing:border-box;padding:28px 32px;"
            "font-family:'Microsoft YaHei','PingFang SC','Noto Sans CJK SC',sans-serif;"
            "background:linear-gradient(160deg,#f6f8fc 0%,#eef1f8 100%);color:#1f2328;}",
            ".title{font-size:26px;font-weight:700;}",
            ".subtitle{font-size:14px;color:#57606a;margin-top:6px;}",
            ".facts{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0 6px;}",
            ".fact{background:#ffffff;border:1px solid #d8dee4;border-radius:10px;"
            "padding:8px 14px;font-size:14px;}",
            ".fact b{color:#0969da;font-weight:600;}",
            ".row{display:flex;align-items:flex-start;gap:12px;background:#ffffff;"
            "border:1px solid #d8dee4;border-radius:12px;padding:12px 16px;margin-top:10px;}",
            ".name{flex:0 0 150px;font-size:16px;font-weight:600;padding-top:2px;}",
            ".badge{flex:0 0 auto;font-size:13px;font-weight:600;border-radius:999px;"
            "padding:3px 12px;margin-top:1px;}",
            ".detail{flex:1 1 auto;font-size:13.5px;color:#424a53;line-height:1.55;}",
            ".footer{margin-top:18px;font-size:12.5px;color:#6e7781;}",
            "</style></head><body><div class='card'>",
        ]
        parts.append(
            f"<div class='title'>更好的消息后处理 · 模块状态</div>"
            f"<div class='subtitle'>v{html.escape(self._plugin_version())} ｜ /bpp</div>"
        )
        parts.append("<div class='facts'>")
        for name, value in self._collect_header_facts():
            parts.append(
                f"<div class='fact'>{html.escape(name)}：<b>{html.escape(str(value))}</b></div>"
            )
        parts.append("</div>")
        for row in self._collect_module_rows():
            color, background = state_colors.get(row["state"], ("#57606a", "#eaeef2"))
            parts.append(
                "<div class='row'>"
                f"<div class='name'>{html.escape(row['label'])}</div>"
                f"<div class='badge' style='color:{color};background:{background};'>{html.escape(row['state'])}</div>"
                f"<div class='detail'>{html.escape(row['detail'])}</div>"
                "</div>"
            )
        parts.append(
            f"<div class='footer'>生成时间 {html.escape(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}"
            " ｜ 提示：/bpp fallback 引用一条消息，可实测异常兜底提示词</div>"
        )
        parts.append("</div></body></html>")
        return "".join(parts)

    # ------------------------------------------------------------------
    # /bpp：合并转发发送（一图流优先，文字兜底）
    # ------------------------------------------------------------------

    async def _bpp_send_status(self, stream_id: str) -> None:
        """收集状态 → 渲染一图流 → 合并转发发送；每一环失败都有下一级兜底。

        降级链（与官方文档的兼容建议一致）：

        1. ``render.html2png`` 成功 → 合并转发单节点 **image 段**（一图流）；
        2. 渲染失败（能力被拒 / 宿主没有渲染后端 / 超时）→ 合并转发单节点 **text 段**；
        3. ``send.forward`` 失败（适配器不支持转发等）→ 有图直发 ``send.image``，
           再失败 / 没有图 → ``send.text`` 逐行文本。
        """
        text_lines = self._build_status_text()
        status_text = "\n".join(text_lines)
        image_base64 = await self._render_status_image()

        segments: List[Dict[str, str]]
        if image_base64:
            segments = [{"type": "image", "content": image_base64}]
        else:
            segments = [{"type": "text", "content": status_text}]
        node = {
            "user_id": "0",
            "nickname": _FORWARD_NICKNAME,
            "segments": segments,
        }
        forwarded = False
        try:
            forwarded = bool(
                await self.ctx.send.forward(
                    [node],
                    stream_id,
                    sync_to_maisaka_history=False,
                    processed_plain_text=status_text,
                )
            )
        except Exception as exc:
            self.ctx.logger.warning("合并转发发送模块状态失败，改为直发: %s", exc)
        if forwarded:
            self.ctx.logger.info(
                "已用合并转发发送模块状态（会话 %s，形态=%s）",
                stream_id,
                "一图流" if image_base64 else "文字",
            )
            return

        # 转发不可用：直发兜底。
        if image_base64:
            try:
                if await self.ctx.send.image(image_base64, stream_id, sync_to_maisaka_history=False):
                    self.ctx.logger.info("合并转发不可用，已直发状态一图流（会话 %s）", stream_id)
                    return
            except Exception as exc:
                self.ctx.logger.warning("直发状态图片失败，改为文字: %s", exc)
        await self.ctx.send.text(status_text, stream_id, sync_to_maisaka_history=False)
        self.ctx.logger.info("已用文字直发模块状态（会话 %s）", stream_id)

    async def _render_status_image(self) -> str:
        """把状态 HTML 渲染成 PNG，返回 base64；任何失败返回空串（调用方退回文字）。"""
        try:
            html_text = self._render_status_html()
        except Exception as exc:
            self.ctx.logger.warning("状态 HTML 构建失败，一图流退回文字: %s", exc)
            return ""
        try:
            result = await self.ctx.render.html2png(
                html_text,
                # 按卡片元素截取（高度贴合内容），而不是整页截图：整页（full_page）
                # 截图的高度不会低于视口高度——内容不足一屏时（状态行少时约 700px，
                # 视口 800px）图片底部会多出一段空白。视口仍给 780 宽定宽 + 兜底高度。
                selector=".card",
                viewport={"width": _RENDER_VIEWPORT_WIDTH, "height": 800},
                device_scale_factor=_RENDER_DEVICE_SCALE_FACTOR,
            )
        except Exception as exc:
            self.ctx.logger.warning("一图流渲染失败（render.html2png 异常），退回文字: %s", exc)
            return ""
        if not isinstance(result, dict):
            self.ctx.logger.warning("一图流渲染返回形态异常 %r，退回文字", type(result).__name__)
            return ""
        if result.get("success") is False:
            self.ctx.logger.warning("一图流渲染被拒/失败（%s），退回文字", result.get("error"))
            return ""
        image_base64 = str(result.get("image_base64") or "").strip()
        if not image_base64:
            self.ctx.logger.warning("一图流渲染结果为空，退回文字")
        return image_base64

    # ------------------------------------------------------------------
    # /bpp fallback：对引用的消息触发一次兜底测试
    # ------------------------------------------------------------------

    async def _bpp_fallback_test(self, kwargs: Dict[str, Any], stream_id: str) -> None:
        """对**命令引用的那条消息**触发一次异常兜底测试。

        流程与真实兜底完全同源：从 ``[response_splitter] fallback_prompts`` 的当前配置
        随机抽一条（留空 = 宿主自带的兜底提示词组），替换 ``{bot_name}`` / ``{user_name}``
        占位（user_name = 被引用消息的发送者名称，群名片优先；他人可控文本，替换前做
        长度截断与控制字符清理），然后**引用那条消息**发出。
        测试消息不写 Maisaka 上下文（``sync_to_maisaka_history=False``），避免污染 bot 记忆；
        发送走 ``_send_follow_up_text``（与分段补发同一条通道：文本规则、引用注入照常生效，
        所见即真实兜底触发时的效果）。

        插件总开关（``[plugin] enabled``）关闭时直接拦截：这是唯一会**真实发出消息**的
        ``/bpp`` 子命令，插件停用时不该再触发发送（状态查询 ``/bpp`` 有意不受总开关
        限制——排查"为什么没生效"正需要它）。
        """
        if not self._plugin_enabled():
            await self.ctx.send.text(
                "插件总开关（[plugin] enabled）当前为关，/bpp fallback 已拦截；"
                "状态查询 /bpp 不受总开关限制，可照常使用。",
                stream_id,
                sync_to_maisaka_history=False,
            )
            return
        message = kwargs.get("message")
        target_id, sender_name = self._extract_reply_target_from_message(message)
        if not target_id:
            await self.ctx.send.text(
                "兜底测试需要指定对象：请**引用一条消息**后再发「/bpp fallback」，"
                "插件会抽一条兜底提示词（替换 {bot_name}/{user_name} 占位）引用回复它。",
                stream_id,
                sync_to_maisaka_history=False,
            )
            return
        if not sender_name:
            sender_name = await self._resolve_sender_name(target_id)
        # {user_name} 是被引用消息发送者的昵称/群名片（他人可控）：替换与日志回显前
        # 统一做长度截断 + 控制字符清理。
        sender_name = _sanitize_placeholder_name(sender_name)

        # 「兜底回复的自称」生效值只解析一次：既是宿主自带兜底提示词的 <昵称> 前缀，
        # 也是 {bot_name} 占位符的替换值（两处原本各调一次 _resolve_mirror）。
        bot_name = str(
            self._resolve_mirror("response_splitter", "fallback_nickname", "bot_nickname", "text")
            or ""
        )
        prompts = self._custom_fallback_prompts()
        source = "自定义列表" if prompts else "宿主自带"
        if not prompts:
            prompts = build_default_fallback_replies(bot_name)
        template = random.choice(prompts)
        text = template.replace("{bot_name}", bot_name).replace("{user_name}", sender_name or "")
        if not text.strip():
            text = "（抽到的兜底提示词替换占位后为空，请检查配置）"

        # previous_message_id=target_id 只用作返回值兜底：发送成功但适配器没回传
        # message_id 时 _send_follow_up_text 会返回它，避免把"已发出"误报成失败
        # （否则用户会额外收到一条"兜底测试发送失败"）。
        sent_id = await self._send_follow_up_text(
            stream_id,
            text,
            typing=False,
            quote_target=target_id,
            previous_message_id=target_id,
            sync_history=False,
        )
        if sent_id is None:
            # 通知里只回显提示词前 40 字符：整段文本可能含被引用消息第三方发送者的
            # 名称（他人可控内容）且可能很长，全文在下面的日志里可查。
            await self.ctx.send.text(
                f"兜底测试发送失败（抽中的提示词：{text[:40]!r}，来源：{source}），详见日志。",
                stream_id,
                sync_to_maisaka_history=False,
            )
            return
        self.ctx.logger.info(
            "兜底测试已发送（会话 %s，引用 %s，来源=%s，模板=%r，bot_name=%r，user_name=%r）",
            stream_id,
            target_id,
            source,
            template,
            bot_name,
            sender_name,
        )
        if not self._post_process_takeover_active():
            # 测试与真实兜底同源，但自定义 fallback_prompts 仅在后处理接管生效时起作用：
            # 接管未生效时线上兜底文本由宿主决定，避免 operator 拿测试结果误判线上行为。
            self.ctx.logger.info(
                "注意：后处理接管当前未生效（宿主开关未关或接管配置关闭），"
                "本次兜底测试结果不代表线上行为——线上兜底文本由宿主决定"
            )

    @staticmethod
    def _extract_reply_target_from_message(message: Any) -> Tuple[str, str]:
        """从命令消息的 ``raw_message`` 里取 reply 段的 ``(target_message_id, 发送者名称)``。

        入站 reply 段形态（napcat 适配器）：
        ``{"type": "reply", "data": {"target_message_id": ..., "target_message_sender_nickname": ...,
        "target_message_sender_cardname": ...}}``。名称优先群名片、其次昵称；
        段里没带名称时返回空串（调用方再按 id 查一次）。没有 reply 段返回两个空串。
        """
        if not isinstance(message, dict):
            return "", ""
        components = message.get("raw_message")
        if not isinstance(components, list):
            return "", ""
        for component in components:
            if not isinstance(component, dict):
                continue
            if str(component.get("type") or "") != "reply":
                continue
            data = component.get("data")
            if not isinstance(data, dict):
                continue
            target_id = str(data.get("target_message_id") or data.get("id") or "").strip()
            if not target_id:
                continue
            name = str(data.get("target_message_sender_cardname") or "").strip() or str(
                data.get("target_message_sender_nickname") or ""
            ).strip()
            return target_id, name
        return "", ""

    async def _resolve_sender_name(self, target_id: str) -> str:
        """按消息 id 查发送者名称（群名片优先、昵称兜底）；查不到返回空串。

        走 ``_lookup_reply_target``（带 TTL 缓存），与引用回复接管同一份缓存。
        """
        try:
            target = await self._lookup_reply_target(target_id)
        except Exception as exc:
            self.ctx.logger.debug("兜底测试查询被引用消息失败: %s", exc)
            return ""
        if target is None:
            return ""
        return str(target[2] or "").strip() or str(target[1] or "").strip()
