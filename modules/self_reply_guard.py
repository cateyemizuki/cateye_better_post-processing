"""自回复拦截 / 重复回复拦截（阶段 3 合并，迁自 ``cateye_duplicate_reply_guard`` v1.1.4）。

本模块是 ``modules/*.py`` 家族的第六个 mixin：状态与 5 个 HookHandler 全部由
``SelfReplyGuardMixin`` 提供，由 ``plugin.py`` 的插件类组合（SDK 用 ``dir(instance)``
收集组件，继承来的 HookHandler 一样会被注册）。配置节 ``[self_reply_guard]`` /
``[duplicate_reply_guard]`` / ``[intercept_log]`` 定义在 ``plugin.py`` 配置区。

原插件解决的问题（代码逐行等价迁移，仅做下述"合并适配"）：

1. **回复自己（self_reply）**：Planner 对 *bot 自己发出的消息* 调用 ``reply``。
2. **重复回复（duplicate_reply）**：Planner 在同一个内部轮次的后继回合里，对同一条
   目标消息再次调用 ``reply``。

拦截矩阵（两个功能 × 两个阶段，各自独立开关）：

============  ==========================================  ==========================================
阶段          触发点                                        动作
============  ==========================================  ==========================================
生成前        ``maisaka.planner.after_response``(BLOCKING)  从 ``output_items`` 里剔除命中的
                                                            ``reply`` 工具调用
发送前兜底    ``send_service.before_send``(BLOCKING/LATE)   ``abort`` 本次下发
============  ==========================================  ==========================================

## 私聊 / 群聊豁免（1.3.0）

自回复拦截按**会话类型**分别豁免：私聊默认豁免（``exempt_private_chat``，宿主设计内
的"补充说明自己的消息"），群聊默认不豁免（``exempt_group_chat`` 默认关闭）。
两种豁免都受**连续次数上限**约束（私聊 ``private_exempt_limit`` 默认 2、群聊
``group_exempt_limit`` 默认 1；**0 = 无上限**）：同一会话连续豁免达到上限后，
后续自回复照常拦截。计数在 ``maisaka.reply.before_post_process`` 每轮消费一次，
``send_service.after_send`` 确认成功回复了**非自身**消息后清零。

## 合并适配（与旧插件的差异，逐条有据）

* **删除** ``create_plugin()`` 工厂与 ``DuplicateReplyGuardPlugin`` 主类：改为 mixin
  （计划书 6.1）。原插件配置节的 ``plugin.enabled`` 总开关由主插件 ``[plugin].enabled``
  承担（``self._plugin_enabled()``，主插件 ``plugin.py`` 中的实现语义与之完全一致）。
* **声明名改名（1 处，主代理裁决）**：``reply_round_marker`` →
  ``guard_reply_round_marker``。主插件既有 HookHandler 已占用 ``reply_round_marker``
  （``plugin.py`` 的 ``maisaka.reply.before_post_process`` / BLOCKING / LATE），宿主
  ``component_registry`` 以 ``full_name = plugin_id.name`` 为键去重、后者静默覆盖，
  同名会导致其中一个 handler 静默失效。mode/order 等其余声明参数保持原值。
* **方法名改名（3 处，必需适配）**：``handle_before_send`` / ``handle_planner_after_response``
  与主插件类体同名方法遮蔽；``_is_self_message`` 与主插件类体同名但签名不同（被遮蔽后
  调用将 TypeError）→ 依次改为 ``handle_guard_before_send`` /
  ``handle_guard_planner_after_response`` / ``_guard_is_self_message``。
  hook 的声明 name/mode/order 不受影响。
* **依赖 ``..intercept_log``**：拦截独立日志实现文件按计划书 6.1 搬到主插件根目录
  （``intercept_log.py``，与 ``post_processing.py`` 同级），导入路径随之调整。
* **配置节路径**：``self.config.self_reply_guard.x`` / ``duplicate_reply_guard.x`` /
  ``intercept_log.x`` 与旧插件同名同义（计划书 6.2）。
* **同批去重补上功能开关门控（1 处，总复审修正）**：主拦截里"同一响应内重复指向同一目标"
  的分支（``accepted_targets``）在原插件中**不看** ``duplicate_reply_guard.enabled``，
  于是"重复回复拦截"整块关闭时它照样剔除第二条 ``reply`` 调用并写拦截日志——这会让
  计划书 6.6「两守卫默认关闭时主插件行为必须与 1.1.3 完全一致」不成立。故该分支现与
  ``_classify_target`` 走同一门控（``_duplicate_guard_enabled()``）：**功能开启时**与
  原插件行为逐行一致，**功能关闭时**零副作用放行（``strip_at_planner`` 仍由
  ``_strip_active`` 统一判定）。
* 每个 Hook 在**本模块开关关闭时零副作用直接放行**（插件总开关、
  ``self_reply_guard.enabled`` / ``duplicate_reply_guard.enabled`` 各自判定），
  保证新模块全关时主插件行为与 1.1.3 完全一致（计划书 6.6）。
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

from maibot_sdk import HookHandler
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from ..intercept_log import InterceptLogger

#: 被守护的内置工具名（只有 reply 会产出可见回复）。
GUARDED_TOOL_NAME = "reply"

#: 判定"轮标记"的有效时长：只用于把生成阶段的判定传递给紧随其后的发送阶段。
_BLOCKED_ROUND_TTL_SECONDS = 120.0

#: "待确认送达"目标的保留时长：从 ``maisaka.reply.before_post_process``（回复文本已生成）
#: 等到 ``send_service.after_send``（确认下发结果）之间的中转窗口。
#: 与轮标记不同级——发送链路本身是同步阻塞的，正常在毫秒级完成；留 120 秒只为兜住
#: 平台限流重试、网络抖动等极端情况。超时未等到回执即视为**未送达**（宁可不拦，不误拦）。
_PENDING_TARGET_TTL_SECONDS = 120.0

#: 单会话"已回复目标"记录上限（防长期驻留会话无限增长）。
_MAX_REPLIED_TARGETS_PER_SESSION = 500

#: 单会话"待确认送达"记录上限（同上，防异常路径下堆积）。
_MAX_PENDING_TARGETS_PER_SESSION = 50

#: 事件与阶段常量（同时是独立日志里的取值）。
FEATURE_SELF = "self_reply"
FEATURE_DUPLICATE = "duplicate_reply"
STAGE_PLANNER_STRIP = "planner_strip"
STAGE_ROUND_BLOCKED = "round_blocked"
STAGE_SEND_ABORT = "send_abort"

_MESSAGE_TAG_RE = re.compile(r"<message\b[^>]*>", re.IGNORECASE)
_MSG_ID_ATTR_RE = re.compile(r'msg_id="(-?\d+)"')
_SELF_ATTR_RE = re.compile(r'is_self_message="(?:true|1)"', re.IGNORECASE)


def _iter_text_fragments(node: Any, depth: int = 0) -> Iterator[str]:
    """递归收集任意嵌套结构里的字符串（限深，容忍 Item schema 形态变化）。"""

    if depth > 8:
        return
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_text_fragments(value, depth + 1)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _iter_text_fragments(value, depth + 1)


def _extract_self_message_ids(node: Any) -> Set[str]:
    """从模型上下文里取出渲染为 ``is_self_message="true"`` 的 msg_id 集合。

    宿主把 bot 自己的消息渲染成
    ``<message msg_id="100000001" quote="123456789" time="16:59:55" user="MyBot" is_self_message="true">``，
    这里按"标签内同时出现 msg_id 与 is_self_message"判定，不依赖属性顺序。
    """

    found: Set[str] = set()
    for fragment in _iter_text_fragments(node):
        if "is_self_message" not in fragment:
            continue
        for tag in _MESSAGE_TAG_RE.findall(fragment):
            if not _SELF_ATTR_RE.search(tag):
                continue
            matched = _MSG_ID_ATTR_RE.search(tag)
            if matched:
                found.add(matched.group(1))
    return found


class SelfReplyGuardMixin:
    """自回复拦截与重复回复拦截的状态、判定与 5 个 Hook。"""

    # ------------------------------------------------------------------
    # 初始化 / 生命周期（由主插件调用）
    # ------------------------------------------------------------------

    def _init_self_reply_guard(self) -> None:
        """初始化状态（由插件类 ``__init__`` 调用）。"""
        # 自身消息 ID 台账：session_id -> {msg_id: 过期单调时刻}。
        self._self_ids: Dict[str, Dict[str, float]] = {}
        # 已回复目标台账：session_id -> {target_msg_id: 最近一次**确认送达**时刻}。
        self._replied_targets: Dict[str, Dict[str, float]] = {}
        # 待确认送达台账（中转）：session_id -> {target_msg_id: 过期单调时刻}。
        # 生成阶段（before_post_process）写入，发送阶段（after_send，sent=True）消费后
        # 才真正落到 _replied_targets —— 生成成功但发送失败时不会留下"已回复"痕迹。
        self._pending_targets: Dict[str, Dict[str, float]] = {}
        # 会话类型台账：session_id -> 是否群聊（从出站载荷的 message_info.group_info 学到，零 RPC）。
        self._session_is_group: Dict[str, bool] = {}
        # 轮标记（生成阶段判定 → 发送阶段消费）：session_id -> {target: (过期时刻, 功能)}。
        self._blocked_rounds: Dict[str, Dict[str, Tuple[float, str]]] = {}
        # 自回复豁免计数（连续豁免语义）：session_id -> 已连续豁免次数。
        # 只在 ``maisaka.reply.before_post_process`` 每轮消费一次；成功回复非自身消息后清零。
        self._self_exempt_used: Dict[str, int] = {}
        # 拦截独立日志写入器。
        self._intercept_log: Optional[InterceptLogger] = None

    def _self_reply_guard_start(self) -> None:
        """启动时：按配置构造拦截独立日志写入器并打印一行生效摘要（原插件 ``on_load``）。"""
        self._rebuild_intercept_log()
        log_path = self._intercept_log.path if self._intercept_log is not None else "未启用"
        self.ctx.logger.info(
            "自回复/重复回复拦截已加载。总开关=%s；自回复拦截=%s（生成前剔除=%s / 发送前兜底=%s / 上下文扫描=%s / 出站台账=%s / %s）；"
            "重复回复拦截=%s（生成前剔除=%s / 发送前兜底=%s / 去重窗口=%s 秒 / 延时补充=%s%s / 目标登记=送达确认后）；"
            "拦截独立日志=%s",
            self._plugin_enabled(),
            self.config.self_reply_guard.enabled,
            self.config.self_reply_guard.strip_at_planner,
            self.config.self_reply_guard.abort_at_send,
            self.config.self_reply_guard.scan_planner_context,
            self.config.self_reply_guard.track_outbound_ids,
            self._exemption_summary(),
            self.config.duplicate_reply_guard.enabled,
            self.config.duplicate_reply_guard.strip_at_planner,
            self.config.duplicate_reply_guard.abort_at_send,
            self.config.duplicate_reply_guard.dedupe_window_seconds,
            "开" if self.config.duplicate_reply_guard.allow_late_repeat else "关",
            f"（阈值 {self.config.duplicate_reply_guard.late_repeat_after_seconds} 秒）"
            if self.config.duplicate_reply_guard.allow_late_repeat
            else "",
            log_path,
        )

    def _self_reply_guard_stop(self) -> None:
        """卸载时：关闭拦截独立日志写入器（原插件 ``on_unload``）。"""
        if self._intercept_log is not None:
            self._intercept_log.close()
            self._intercept_log = None

    def _self_reply_guard_on_config_update(self) -> None:
        """插件配置热更新时：重建日志写入器并打印一行摘要（原插件 ``on_config_update``）。"""
        self._rebuild_intercept_log()
        self.ctx.logger.info(
            "自回复/重复回复拦截配置已更新：自回复拦截=%s（%s），重复回复拦截=%s（去重窗口=%s 秒，延时补充=%s%s）",
            self.config.self_reply_guard.enabled,
            self._exemption_summary(),
            self.config.duplicate_reply_guard.enabled,
            self.config.duplicate_reply_guard.dedupe_window_seconds,
            "开" if self.config.duplicate_reply_guard.allow_late_repeat else "关",
            f"（阈值 {self.config.duplicate_reply_guard.late_repeat_after_seconds} 秒）"
            if self.config.duplicate_reply_guard.allow_late_repeat
            else "",
        )

    # ------------------------------------------------------------------
    # Hook 1：记录本会话里"哪些消息是 bot 自己发的"（生成前判定依据）
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.planner.before_request",
        name="self_message_id_scanner",
        description="扫描 planner 上下文，记录渲染为 is_self_message 的 msg_id（模型可能选中的自身消息全集）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_planner_before_request(self, **kwargs: Any) -> None:
        """只读扫描，不改写任何 kwargs。"""

        try:
            if not self._self_guard_enabled() or not self.config.self_reply_guard.scan_planner_context:
                return
            session_id = self._session_id_of(kwargs)
            if not session_id:
                return
            items = kwargs.get("items")
            if items is None:
                return
            found = _extract_self_message_ids(items)
            if not found:
                return
            self._remember_ids(session_id, found)
        except Exception as exc:  # noqa: BLE001 - 自身消息扫描绝不能影响 planner
            self.ctx.logger.warning("自身消息 ID 扫描失败，已跳过本轮: %s", exc)

    # ------------------------------------------------------------------
    # Hook 2：生成前剔除（主拦截）
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.planner.after_response",
        name="planner_reply_stripper",
        description="planner 响应里命中自回复/重复回复的 reply 工具调用直接从 output_items 中剔除（生成前拦截）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    # 【必需适配】原名 handle_planner_after_response → 新名 handle_guard_planner_after_response；
    # 原因：与主插件类体同名方法（plugin.py 的 planner 表情意图观察器）冲突，mixin 版本会被
    # MRO 遮蔽、handler 根本不会被 SDK 注册；hook 的声明 name/mode/order 保持原值。
    async def handle_guard_planner_after_response(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """剔除违规的 reply 工具调用；无改动时返回 None（保持宿主原始 Item 与 replay 引用）。"""

        try:
            if not self._plugin_enabled():
                return None
            output_items = kwargs.get("output_items")
            if not isinstance(output_items, list) or not output_items:
                return None
            session_id = self._session_id_of(kwargs)
            if not session_id:
                return None

            now = time.monotonic()
            kept: List[Any] = []
            removals: List[Dict[str, Any]] = []
            # 同一次响应内已被放行的目标：用于拦住"同一批次里对同一目标并行调用两次 reply"。
            accepted_targets: Set[str] = set()

            for item in output_items:
                tool_call = self._reply_tool_call_of(item)
                if tool_call is None:
                    kept.append(item)
                    continue
                call_id, target_id = tool_call
                feature = self._classify_target(session_id, target_id, now)
                # 同批去重也属于"重复回复拦截"：必须与 `_classify_target` 走同一个功能开关，
                # 否则功能整块关闭时本分支照样剔除调用并写拦截日志（合并计划书 6.6 要求
                # 两守卫默认关闭 = 主插件行为与 1.1.3 完全一致、零副作用）。
                # `strip_at_planner` 仍由下方的 `_strip_active` 统一判定，两处口径一致。
                if (
                    not feature
                    and target_id
                    and target_id in accepted_targets
                    and self._duplicate_guard_enabled()
                ):
                    # 同一响应内重复指向同一目标：按重复回复处理。
                    feature = FEATURE_DUPLICATE
                if not feature:
                    if target_id:
                        accepted_targets.add(target_id)
                    kept.append(item)
                    continue
                if not self._strip_active(feature):
                    # 生成前剔除被关闭：放行，交给发送前兜底（仍未放行则不改写 items）。
                    if target_id:
                        accepted_targets.add(target_id)
                    kept.append(item)
                    continue
                removals.append({"feature": feature, "call_id": call_id, "target_msg_id": target_id})

            if not removals:
                return None

            for removal in removals:
                self._record_intercept(
                    event=removal["feature"],
                    stage=STAGE_PLANNER_STRIP,
                    session_id=session_id,
                    target_msg_id=removal["target_msg_id"],
                    call_id=removal["call_id"],
                    detail=self._describe(removal["feature"], session_id, removal["target_msg_id"], now, stage=STAGE_PLANNER_STRIP),
                )

            modified = dict(kwargs)
            modified["output_items"] = kept
            return {"action": "continue", "modified_kwargs": modified}
        except Exception as exc:  # noqa: BLE001 - 拦截失败必须 fail-open
            self.ctx.logger.warning("生成前剔除失败，本轮放弃拦截（fail-open）: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Hook 3：记录本轮回复目标 + 标记违规轮（生成后、发送前）
    # ------------------------------------------------------------------

    @HookHandler(
        "maisaka.reply.before_post_process",
        # 【主代理裁决】声明名 reply_round_marker → guard_reply_round_marker；
        # 原因：主插件既有 HookHandler 已占用同名（plugin.py 的同名 handler），宿主按
        # plugin_id.name 去重、后者静默覆盖，同名必丢一个 handler；mode/order 保持原值。
        name="guard_reply_round_marker",
        description="回复文本生成后恰好触发一次：标记自回复/重复回复轮供发送前兜底消费；合法轮只暂存目标，等 after_send 确认送达后才登记",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_reply_before_post_process(self, **kwargs: Any) -> None:
        """只读判定 + 写入轮标记 / 待确认送达暂存，不改写 kwargs。

        这里**不再直接登记"已回复"**：本 Hook 在 ``reply.py:396-402`` 触发时，回复文本
        才刚生成完，真正的下发还在后面（``reply.py:451-532``）。任一分段 ``sent=False``
        就会 ``break`` 并返回 ``build_failure_result(..., "可见回复生成成功，但发送失败。")``，
        用户其实什么也没看到——此时若已登记，planner 在后续回合重试同一目标会被本模块误拦。
        所以合法轮只写"待确认送达"暂存，由 ``send_service.after_send``（``sent=True``）确认。
        """

        try:
            if not self._plugin_enabled():
                return
            session_id = self._session_id_of(kwargs)
            target_id = self._target_id_of(kwargs)
            if not session_id or not target_id:
                return

            now = time.monotonic()
            feature = self._classify_target(session_id, target_id, now)
            if feature:
                self._mark_blocked_round(session_id, target_id, feature, now)
                self._record_intercept(
                    event=feature,
                    stage=STAGE_ROUND_BLOCKED,
                    session_id=session_id,
                    target_msg_id=target_id,
                    detail=self._describe(feature, session_id, target_id, now, stage=STAGE_ROUND_BLOCKED),
                )
                return

            # 合法轮：先清掉该目标可能残留的旧轮标记（可能是上一轮被拦后遗留），
            # 并把目标写进"待确认送达"暂存 —— 真正的登记要等 after_send 回执 sent=True。
            self._clear_blocked_round(session_id, target_id)
            # 目标确实是 bot 自己的消息、但被豁免放行 → 消耗一次连续豁免额度。
            # 只在这里计数（每次回复轮恰好一次）：planner 阶段一次响应可能含多个 reply 调用，
            # 在那里计数会重复消耗。
            if (
                self._self_guard_enabled()
                and self._guard_is_self_message(session_id, target_id, now)
                and not self._self_reply_guard_applies(session_id)
            ):
                self._note_self_exempt_use(session_id)
            self._mark_pending_target(session_id, target_id, now)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("回复轮标记失败，已跳过: %s", exc)

    # ------------------------------------------------------------------
    # Hook 4：发送前兜底（最后一道闸）
    # ------------------------------------------------------------------

    @HookHandler(
        "send_service.before_send",
        name="reply_send_abort",
        description="下发前最终校验：自回复直接中止；被标记为重复回复的轮次整体中止",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    # 【必需适配】原名 handle_before_send → 新名 handle_guard_before_send；
    # 原因：与主插件类体同名方法（plugin.py 的 outbound_message_enhancer）冲突，mixin 版本
    # 会被 MRO 遮蔽、handler 不会被注册；hook 的声明 name/mode/order（含 LATE）保持原值。
    async def handle_guard_before_send(self, **kwargs: Any) -> Optional[Dict[str, Any]]:
        """命中即 ``abort``；否则返回 None 放行。"""

        try:
            if not self._plugin_enabled():
                return None
            message = kwargs.get("message")
            session_id = ""
            if isinstance(message, dict):
                session_id = str(message.get("session_id") or "").strip()
            if not session_id:
                session_id = self._session_id_of(kwargs)
            target_id = self._target_id_of(kwargs)
            if not session_id or not target_id:
                return None
            # 先学会话类型（出站载荷里群聊才有 group_info），再做判定——包括被中止的那条也要学到。
            self._learn_chat_type(session_id, message)

            now = time.monotonic()
            # 本阶段**只认生成阶段留下的轮标记**，两个功能都走同一套判定。
            #
            # 为什么不在这里直接用 reply_message_id 判自回复：宿主分段发送时，
            # 非首段可能引用"上一条已发送分段"（`reply.py:_resolve_segment_reply_context`
            # 的 quote_previous 分支），此时 reply_message_id 是 **bot 自己的消息**——
            # 直接判自回复会把正常的错别字更正段整段误杀。
            # 轮标记由 `maisaka.reply.before_post_process` 用 reply 工具的真实目标写入，
            # 而首段的 reply_message_id 恒等于该目标，所以首个分段命中即中止整轮下发
            # （`reply.py:488-498`：sent=False 直接跳出分段循环）。
            marked = self._blocked_round(session_id, target_id, now)
            if marked is None:
                return None
            # 轮标记是 (过期时刻, 功能名)，功能名在下标 1。
            feature = marked[1]
            # 功能级 enabled 复查（裁决修正包 1.1.4）：轮标记 TTL 为 120 秒，若标记写入后
            # 配置热更新把对应功能整块关掉，这里视为无标记——放行并清理轮标记，
            # 避免陈旧标记在窗口期内中止发送（fail-open 语义不变）。
            if feature == FEATURE_SELF and not self._self_guard_enabled():
                self._clear_blocked_round(session_id, target_id)
                return None
            if feature == FEATURE_DUPLICATE and not self._duplicate_guard_enabled():
                self._clear_blocked_round(session_id, target_id)
                return None
            if feature == FEATURE_SELF:
                if not self.config.self_reply_guard.abort_at_send:
                    return None
                if not self._self_reply_guard_applies(session_id):
                    # 豁免复核：生成阶段若因类型未知而误标，这里补一次纠正。
                    self._clear_blocked_round(session_id, target_id)
                    return None
            if feature == FEATURE_DUPLICATE and not self.config.duplicate_reply_guard.abort_at_send:
                return None
            self._clear_blocked_round(session_id, target_id)

            self._record_intercept(
                event=feature,
                stage=STAGE_SEND_ABORT,
                session_id=session_id,
                target_msg_id=target_id,
                detail=self._describe(feature, session_id, target_id, now, stage=STAGE_SEND_ABORT),
            )
            abort_message = f"{feature} 拦截：目标消息 {target_id}（会话 {session_id}）"
            return {"action": "abort", "abort_message": abort_message}
        except Exception as exc:  # noqa: BLE001 - 兜底失败不得阻断正常发送
            self.ctx.logger.warning("发送前兜底校验失败，本条按放行处理: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Hook 5：累积自身消息 ID 台账（observe）
    # ------------------------------------------------------------------

    @HookHandler(
        "send_service.after_send",
        name="outbound_id_recorder",
        description="observe：收集自身出站消息 ID；并在 sent=True 时把待确认目标确认为“已回复”（送达才算数）",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_after_send(self, **kwargs: Any) -> None:
        """纯记账：自身消息 ID 台账 + 已回复目标的**送达确认**。

        这个 Hook 是宿主唯一能拿到"本次下发到底成没成"的地方（载荷里的 ``sent: bool``，
        见 ``send_service.py:900-913``）。所以"已回复目标"的登记必须在这里做，
        而不是在回复文本刚生成时做——生成成功 ≠ 送达成功。

        注意本 Hook 是 OBSERVE（``allow_abort=False``），但宿主对 Hook 异常并不宽容，
        因此整段逻辑仍必须 try/except 兜住，绝不能影响发送链路本身。
        """

        try:
            sent = bool(kwargs.get("sent", False))
            message = kwargs.get("message")
            session_id = ""
            if isinstance(message, dict):
                session_id = str(message.get("session_id") or "").strip()
            if not session_id:
                session_id = self._session_id_of(kwargs)

            # ---- 1) 送达确认：把生成阶段暂存的目标升级为"已回复" ----
            # 必须在 self_guard / track_outbound_ids 开关之前做：它属于**重复回复拦截**
            # 的判定依据，与"自己消息台账"那个功能无关，不该被自回复拦截的开关牵连。
            if sent and session_id:
                self._confirm_reply_delivered(session_id, self._target_id_of(kwargs), time.monotonic())

            # ---- 2) 自身出站消息 ID 台账（自回复拦截的判定依据） ----
            if not self._self_guard_enabled() or not self.config.self_reply_guard.track_outbound_ids:
                return
            if not sent:
                return
            if not isinstance(message, dict):
                return
            message_id = str(message.get("message_id") or "").strip()
            if not session_id or not message_id:
                return
            self._learn_chat_type(session_id, message)
            self._remember_ids(session_id, {message_id})
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("出站消息记账失败，已跳过: %s", exc)

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    # 注：``_plugin_enabled`` 与主插件类体中的同名方法**语义完全一致**
    # （``bool(self.config.plugin.enabled)``），合并后由主插件那份生效（MRO 类体优先），
    # 本模块不自带副本以免出现两份会漂移的实现。

    def _self_guard_enabled(self) -> bool:
        return self._plugin_enabled() and bool(self.config.self_reply_guard.enabled)

    def _duplicate_guard_enabled(self) -> bool:
        return self._plugin_enabled() and bool(self.config.duplicate_reply_guard.enabled)

    def _strip_active(self, feature: str) -> bool:
        if feature == FEATURE_SELF:
            return bool(self.config.self_reply_guard.strip_at_planner)
        return bool(self.config.duplicate_reply_guard.strip_at_planner)

    def _classify_target(self, session_id: str, target_id: str, now: float) -> str:
        """返回命中的拦截功能名；空串表示放行。"""

        if not session_id or not target_id:
            return ""
        if (
            self._self_guard_enabled()
            and self._self_reply_guard_applies(session_id)
            and self._guard_is_self_message(session_id, target_id, now)
        ):
            return FEATURE_SELF
        if self._duplicate_guard_enabled() and self._is_recent_reply_target(session_id, target_id, now):
            return FEATURE_DUPLICATE
        return ""

    def _self_reply_guard_applies(self, session_id: str) -> bool:
        """自回复拦截是否作用于该会话（私聊/群聊豁免 + 连续豁免上限）。

        返回 ``False`` = 本条自回复**豁免放行**；``True`` = 需要拦截。

        私聊里"补充说明你自己发送的消息"是宿主的**设计内行为**
        （``maisaka_generator_base.py:182-189`` 专门为它写了提示词），而恋人插件的主动私聊
        正是这种场景：这一轮没有用户消息可锚，回复目标自然落在 bot 自己上一条发言。
        因此私聊默认豁免（``exempt_private_chat``），但豁免有**连续次数上限**
        （``private_exempt_limit``，默认 2；0 = 无上限），用尽后按自回复拦截处理。
        群聊对称：``exempt_group_chat`` 默认关闭（群里一律拦截）；开启后最多连续豁免
        ``group_exempt_limit`` 次（默认 1；0 = 无上限）。

        会话类型来自出站载荷的 ``message_info.group_info``：宿主构建出站消息时
        **只有群聊才会填 group_info**（``send_service.py:550-582``），私聊恒为 None。
        类型未知时按私聊处理（fail-open，宁可不拦也不误杀）。

        计数是"连续"语义：只在 ``handle_reply_before_post_process`` 每轮消费一次，
        成功回复一条非自身消息（``after_send`` 的 ``sent=True``）后清零。
        """

        config = self.config.self_reply_guard
        is_group = self._session_is_group.get(session_id) is True
        if is_group:
            if not bool(config.exempt_group_chat):
                return True
            limit = int(config.group_exempt_limit)
        else:
            if not bool(config.exempt_private_chat):
                return True
            limit = int(config.private_exempt_limit)
        if limit <= 0:
            return False
        return self._self_exempt_used.get(session_id, 0) >= limit

    def _note_self_exempt_use(self, session_id: str) -> None:
        """消费一次自回复豁免额度（仅限次模式需要记录；无上限时不留痕）。"""

        if not session_id:
            return
        config = self.config.self_reply_guard
        is_group = self._session_is_group.get(session_id) is True
        limit = int(config.group_exempt_limit if is_group else config.private_exempt_limit)
        if limit <= 0:
            return
        self._self_exempt_used[session_id] = self._self_exempt_used.get(session_id, 0) + 1

    def _reset_self_exempt_use(self, session_id: str) -> None:
        """清零连续豁免计数（成功回复了一条非自身消息）。"""

        if session_id:
            self._self_exempt_used.pop(session_id, None)

    def _exemption_summary(self) -> str:
        """自回复豁免配置的一行摘要（启动 / 热更新日志用）。"""

        def _part(enabled: bool, limit: int, label: str) -> str:
            if not bool(enabled):
                return f"{label}=关（不豁免）"
            return f"{label}=开（{'无上限' if int(limit) <= 0 else f'上限 {int(limit)} 次'}）"

        config = self.config.self_reply_guard
        return (
            f"{_part(config.exempt_private_chat, config.private_exempt_limit, '私聊豁免')} / "
            f"{_part(config.exempt_group_chat, config.group_exempt_limit, '群聊豁免')}"
        )

    def _learn_chat_type(self, session_id: str, raw_message: Any) -> None:
        """从出站消息载荷学习会话类型（群聊 / 私聊）。"""

        if not session_id or not isinstance(raw_message, dict):
            return
        message_info = raw_message.get("message_info")
        if not isinstance(message_info, dict):
            return
        self._session_is_group[session_id] = bool(message_info.get("group_info"))

    # 【必需适配】原名 _is_self_message → 新名 _guard_is_self_message；
    # 原因：主插件类体同名静态方法（入站消息自判，签名 ``(message)``）会遮蔽本方法，
    # 本模块的 ``(session_id, target_id, now)`` 调用将 TypeError 并被 fail-open 吞掉。
    def _guard_is_self_message(self, session_id: str, target_id: str, now: float) -> bool:
        entries = self._self_ids.get(session_id)
        if not entries:
            return False
        expire_at = entries.get(target_id)
        if expire_at is None:
            return False
        if expire_at <= now:
            entries.pop(target_id, None)
            return False
        return True

    def _is_recent_reply_target(self, session_id: str, target_id: str, now: float) -> bool:
        """该目标是否属于"短期内已回复过"（命中才拦；延时补充放行返回 False）。"""

        entries = self._replied_targets.get(session_id)
        if not entries:
            return False
        last_at = entries.get(target_id)
        if last_at is None:
            return False
        elapsed = now - last_at
        if elapsed > float(self.config.duplicate_reply_guard.dedupe_window_seconds):
            return False
        # 去重窗口内、但已过延时补充阈值 → 视为"补充说明"，放行本次回复。
        if self._late_repeat_grace_applies(elapsed):
            return False
        return True

    def _late_repeat_grace_applies(self, elapsed: float) -> bool:
        """延时补充放行是否生效（距上次回复达到阈值即放行；只放宽拦截，不会收紧）。"""

        config = self.config.duplicate_reply_guard
        if not bool(config.allow_late_repeat):
            return False
        return elapsed >= float(config.late_repeat_after_seconds)

    def _describe(self, feature: str, session_id: str, target_id: str, now: float, *, stage: str) -> str:
        """构造人类可读的拦截说明（写入独立日志）。"""

        if feature == FEATURE_SELF:
            base = f"目标消息 {target_id} 是 bot 自己发出的消息（命中自身消息台账），按自回复拦截"
        else:
            config = self.config.duplicate_reply_guard
            window = int(config.dedupe_window_seconds)
            last_at = (self._replied_targets.get(session_id) or {}).get(target_id)
            if last_at is not None:
                elapsed = max(0, int(now - last_at))
                if bool(config.allow_late_repeat):
                    grace = int(config.late_repeat_after_seconds)
                    base = (
                        f"目标消息 {target_id} 在 {window} 秒去重窗口内已被回复过"
                        f"（上次 {elapsed} 秒前，未达到 {grace} 秒延时补充阈值），按重复回复拦截"
                    )
                else:
                    base = (
                        f"目标消息 {target_id} 在 {window} 秒去重窗口内已被回复过"
                        f"（上次 {elapsed} 秒前，延时补充放行已关闭），按重复回复拦截"
                    )
            else:
                base = f"目标消息 {target_id} 在 {window} 秒去重窗口内已被回复过，按重复回复拦截"
        stage_text = {
            STAGE_PLANNER_STRIP: "已在生成前剔除该 reply 工具调用（未消耗回复生成 token）",
            STAGE_ROUND_BLOCKED: "回复已生成，已标记本轮为拦截轮，其下发将被中止",
            STAGE_SEND_ABORT: "已在下发前中止本次发送（平台侧不会看到该消息）",
        }.get(stage, "")
        return f"{base}；{stage_text}" if stage_text else base

    # ------------------------------------------------------------------
    # 状态读写
    # ------------------------------------------------------------------

    def _remember_ids(self, session_id: str, message_ids: Set[str]) -> None:
        """登记自身消息 ID（带过期时间与容量淘汰）。"""

        now = time.monotonic()
        ttl = float(self.config.self_reply_guard.id_ttl_seconds)
        cap = int(self.config.self_reply_guard.max_tracked_ids)
        entries = self._self_ids.setdefault(session_id, {})
        for message_id in message_ids:
            if message_id:
                entries[message_id] = now + ttl
        self._prune_expiring(entries, now, cap)

    def _remember_replied_target(self, session_id: str, target_id: str, now: float) -> None:
        """确认登记"这个目标已经被回复过（且回复**确实送到了**）"。"""

        entries = self._replied_targets.setdefault(session_id, {})
        entries[target_id] = now
        window = float(self.config.duplicate_reply_guard.dedupe_window_seconds)
        expired = [key for key, last_at in entries.items() if now - last_at > window]
        for key in expired:
            entries.pop(key, None)
        if len(entries) > _MAX_REPLIED_TARGETS_PER_SESSION:
            for key, _ in sorted(entries.items(), key=lambda pair: pair[1])[
                : len(entries) - _MAX_REPLIED_TARGETS_PER_SESSION
            ]:
                entries.pop(key, None)

    def _mark_pending_target(self, session_id: str, target_id: str, now: float) -> None:
        """登记"本轮已经为这个目标生成了回复，但还不知道能不能送出去"。"""

        entries = self._pending_targets.setdefault(session_id, {})
        entries[target_id] = now + _PENDING_TARGET_TTL_SECONDS
        self._prune_expiring(entries, now, _MAX_PENDING_TARGETS_PER_SESSION)

    def _confirm_reply_delivered(self, session_id: str, target_id: str, now: float) -> None:
        """发送回执 ``sent=True``：把待确认目标升级为"已回复"。

        只有真正走完"生成阶段写入暂存 → 发送阶段确认送达"这条链的目标才会被登记，
        于是"可见回复生成失败"（压根不触发 ``before_post_process``）与"生成成功但发送失败"
        （``sent=False``，不消费暂存）两种情况都不会留下"已回复"痕迹，
        planner 后续重试同一目标不会被本模块拦掉。
        """

        if not session_id or not target_id:
            return
        entries = self._pending_targets.get(session_id)
        if not entries:
            return
        expire_at = entries.pop(target_id, None)
        if expire_at is None:
            # 不是本次回复轮产生的目标（例如其它插件以 bot 身份发的消息）：不越权登记。
            return
        if expire_at <= now:
            # 暂存已过期：链路耗时异常，按未送达处理。
            return
        self._remember_replied_target(session_id, target_id, now)
        # 成功回复了一条非自身消息 → 自回复豁免计数清零（"连续豁免"语义）。
        # 回复自身消息（被豁免的那类轮次）不清零，连续豁免计数要能累积到上限。
        if not self._guard_is_self_message(session_id, target_id, now):
            self._reset_self_exempt_use(session_id)

    def _mark_blocked_round(self, session_id: str, target_id: str, feature: str, now: float) -> None:
        rounds = self._blocked_rounds.setdefault(session_id, {})
        rounds[target_id] = (now + _BLOCKED_ROUND_TTL_SECONDS, feature)
        expired = [key for key, value in rounds.items() if value[0] <= now]
        for key in expired:
            rounds.pop(key, None)

    def _clear_blocked_round(self, session_id: str, target_id: str) -> None:
        rounds = self._blocked_rounds.get(session_id)
        if rounds:
            rounds.pop(target_id, None)

    def _blocked_round(self, session_id: str, target_id: str, now: float) -> Optional[Tuple[float, str]]:
        rounds = self._blocked_rounds.get(session_id)
        if not rounds:
            return None
        value = rounds.get(target_id)
        if value is None:
            return None
        if value[0] <= now:
            rounds.pop(target_id, None)
            return None
        return value

    @staticmethod
    def _prune_expiring(entries: Dict[str, float], now: float, cap: int) -> None:
        expired = [key for key, expire_at in entries.items() if expire_at <= now]
        for key in expired:
            entries.pop(key, None)
        if cap > 0 and len(entries) > cap:
            for key, _ in sorted(entries.items(), key=lambda pair: pair[1])[: len(entries) - cap]:
                entries.pop(key, None)

    # ------------------------------------------------------------------
    # 载荷解析
    # ------------------------------------------------------------------

    @staticmethod
    def _session_id_of(kwargs: Dict[str, Any]) -> str:
        session_id = str(kwargs.get("session_id") or "").strip()
        if session_id:
            return session_id
        message = kwargs.get("message")
        if isinstance(message, dict):
            return str(message.get("session_id") or "").strip()
        return ""

    @staticmethod
    def _target_id_of(kwargs: Dict[str, Any]) -> str:
        """取"被回复的目标消息 ID"：读取回复工具的真实目标 ``reply_message_id``。

        注意：`reply_message_id` 与 `target_message_id` 不是"两个阶段的同义键"，而是同一个键
        的防御性写法——当前宿主版本的各 Hook 载荷里只有 `reply_message_id`，
        `target_message_id` 为防御性死键（留着只为兼容将来/其它版本的载荷形态）。
        """

        for key in ("reply_message_id", "target_message_id"):
            value = str(kwargs.get(key) or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _reply_tool_call_of(item: Any) -> Optional[Tuple[str, str]]:
        """从单个输出 Item 里取出 ``reply`` 工具调用；不是 reply 调用时返回 None。

        Item 形态（``src/llm_models/request_snapshot.py:329-335``）::

            {"item_type": "FunctionCallItem",
             "meta": {...},
             "tool_call": {"call_id": "...", "func_name": "reply", "args": {"msg_id": "..."}, ...}}
        """

        if not isinstance(item, dict):
            return None
        if str(item.get("item_type") or "") != "FunctionCallItem":
            return None
        tool_call = item.get("tool_call")
        if not isinstance(tool_call, dict):
            return None
        func_name = str(tool_call.get("func_name") or "").strip()
        if func_name != GUARDED_TOOL_NAME:
            return None
        arguments = tool_call.get("args")
        target_id = ""
        if isinstance(arguments, dict):
            target_id = str(arguments.get("msg_id") or "").strip()
        return str(tool_call.get("call_id") or ""), target_id

    # ------------------------------------------------------------------
    # 独立日志
    # ------------------------------------------------------------------

    def _rebuild_intercept_log(self) -> None:
        """按当前配置（重新）构造拦截独立日志写入器。"""

        if self._intercept_log is not None:
            self._intercept_log.close()
            self._intercept_log = None
        if not self.config.intercept_log.enabled:
            return
        data_dir = getattr(self.ctx.paths, "data_dir", None)
        if not data_dir:
            self.ctx.logger.warning("ctx.paths.data_dir 不可用，拦截独立日志已禁用")
            return
        # 路径防护：配置里的文件名只取文件名部分（剥离目录，并拒绝 `.` / `..`），
        # 日志永远落在插件数据目录内。
        raw_file_name = str(self.config.intercept_log.file_name or "").strip()
        normalized = Path(raw_file_name).name
        file_name = normalized if normalized not in ("", ".", "..") else "intercept.jsonl"
        if file_name != raw_file_name:
            self.ctx.logger.warning(
                "拦截日志文件名 %r 含目录或绝对路径，已规范化为 %r（日志只写入插件数据目录）",
                raw_file_name,
                file_name,
            )
        self._intercept_log = InterceptLogger(
            directory=Path(str(data_dir)),
            file_name=file_name,
            max_bytes=int(self.config.intercept_log.max_file_size_kb) * 1024,
            backup_count=int(self.config.intercept_log.backup_count),
            on_error=lambda message: self.ctx.logger.warning("%s", message),
        )

    def _record_intercept(
        self,
        *,
        event: str,
        stage: str,
        session_id: str,
        target_msg_id: str,
        detail: str,
        call_id: str = "",
    ) -> None:
        """写一条拦截记录：独立日志 + （可选）主日志回显。"""

        if self._intercept_log is not None:
            self._intercept_log.record(
                event,
                stage=stage,
                session_id=session_id,
                target_msg_id=target_msg_id,
                call_id=call_id,
                detail=detail,
            )
        if not self.config.intercept_log.echo_to_main_log:
            return
        level = getattr(logging, str(self.config.intercept_log.echo_level or "info").upper(), logging.INFO)
        self.ctx.logger.log(
            level,
            "[拦截] %s / %s 会话=%s 目标消息=%s %s",
            event,
            stage,
            session_id,
            target_msg_id,
            detail,
        )
