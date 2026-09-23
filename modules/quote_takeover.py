"""引用回复接管模块：宿主关闭「启用引用回复」时按权重抽取回复方式。

**宿主前置条件**（由 ``modules.requirements`` 判定）：宿主
``chat.reply_style.enable_reply_quote`` 与 ``experimental.enable_rich_reply`` 都必须为
``false``；任一未关闭时本模块**完全静默**——既不抽取回复方式，也不改写引用/@。

抽取口径：直接回复 / 引用回复 / @回复 / 引用＋@回复 四选一（私聊恒不 @），并叠加
「对话已推进」「目标过旧」「同一消息只引用一次」三条调整规则。只作用于本插件登记的
回复轮（由 ``maisaka.reply.before_post_process`` 标记）中指向轮目标的首条文本分段。

共享状态（回复轮记录 ``self._reply_rounds``、文本规则、表情包功能）留在 ``plugin.py``。
"""

from __future__ import annotations

import random
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..post_processing import match_leading_at_mention

# @ 目标发送者查询缓存时长与容量上限（失败项用更短的负缓存，避免失败路径反复 RPC）。
_TARGET_CACHE_TTL_SECONDS = 600.0
_TARGET_CACHE_NEGATIVE_TTL_SECONDS = 30.0
_TARGET_CACHE_MAX_ENTRIES = 512

# 「同一消息只引用一次」记录：单会话容量上限与整会话保留时长（防长期驻留）。
_QUOTED_TARGET_MAX_ENTRIES = 512
_QUOTED_TARGET_SESSION_TTL_SECONDS = 86400.0

# 「回复方式只取首条」记录：单会话容量与整会话保留时长。
# 保留 30 分钟足够覆盖"同一轮/连续回复"；更久之后本来就有 stale 规则兜底
# （默认 stale_age_seconds=1800 秒后强制直接回复：不引用、也不 @）。
_STYLED_TARGET_TTL_SECONDS = 1800.0
_STYLED_TARGET_MAX_ENTRIES = 512


class QuoteTakeoverMixin:
    """引用回复接管的 Hook 与状态。"""

    def _init_quote_takeover(self) -> None:
        """初始化引用接管状态（由插件类 ``__init__`` 调用）。"""
        # 群聊/私聊回复方式权重池（由插件配置重建）。
        self._reply_style_weights: Dict[str, int] = {}
        self._reply_style_weights_private: Dict[str, int] = {}
        # 已引用过的目标消息（「同一消息只引用一次」）：session_id -> OrderedDict[target_id, 记录时刻]。
        self._quoted_targets: Dict[str, "OrderedDict[str, float]"] = {}
        # 已抽过回复方式的目标消息（「回复方式只取首条」）：session_id -> OrderedDict[target_id, 首次抽取时刻]。
        # 用于保证 @ 只可能出现在"对这条目标消息的第一条回复"上。
        self._styled_targets: Dict[str, "OrderedDict[str, float]"] = {}
        # @ 目标发送者查询缓存：reply_message_id -> (缓存时刻, user_id, 昵称, 群名片, 消息时间戳)。
        # 查询失败的负缓存项 user_id 为空串。
        self._target_sender_cache: Dict[str, Tuple[float, str, str, str, float]] = {}

    def _prune_quote_state(self, now: float) -> None:
        """清理「同一消息只引用一次」与「回复方式只取首条」的陈旧记录。"""
        # 整会话超时即丢弃；单会话容量在写入时已限。
        for session_id in [
            key
            for key, bucket in self._quoted_targets.items()
            if not bucket or now - max(bucket.values()) > _QUOTED_TARGET_SESSION_TTL_SECONDS
        ]:
            self._quoted_targets.pop(session_id, None)
        for session_id in [
            key
            for key, bucket in self._styled_targets.items()
            if not bucket or now - max(bucket.values()) > _STYLED_TARGET_TTL_SECONDS
        ]:
            self._styled_targets.pop(session_id, None)


    def _build_weight_pool(self, cfg: Any, *, label: str, allow_at: bool) -> Dict[str, int]:
        """按配置构造一组回复方式权重；权重全 0 时回退为仅引用回复。

        私聊（``allow_at=False``）不读取 @ 相关字段：私聊权重池恒为
        ``at=0 / quote_at=0``，抽取与"对话已推进"调整都不会产生 @。
        """
        weights = {
            "direct": max(0, int(cfg.weight_direct)),
            "quote": max(0, int(cfg.weight_quote)),
            "at": max(0, int(getattr(cfg, "weight_at", 0))) if allow_at else 0,
            "quote_at": max(0, int(getattr(cfg, "weight_quote_at", 0))) if allow_at else 0,
        }
        if sum(weights.values()) <= 0:
            self.ctx.logger.warning("%s回复方式权重全为 0，回退为仅引用回复", label)
            weights = {"direct": 0, "quote": 1, "at": 0, "quote_at": 0}
        return weights

    def _quote_takeover_active(self) -> bool:
        """框架引用回复开关关闭，且群聊或私聊任一侧接管开启时，接管引用回复。

        宿主前置条件（``chat.reply_style.enable_reply_quote`` 与
        ``experimental.enable_rich_reply`` 都关闭）由 ``modules.requirements`` 判定；
        未满足时本模块**完全静默**：不抽取回复方式、不改写引用/@。
        """
        if not self._plugin_enabled():
            return False
        if not (bool(self.config.quote_reply.takeover) or bool(self.config.quote_reply_private.takeover)):
            return False
        return self._module_available("quote_reply")


    def _quote_section_config(self, message: Dict[str, Any]) -> Any:
        """按会话类型取引用回复**功能**配置节（``[quote_reply]`` / ``[quote_reply_private]``）。"""
        return self.config.quote_reply if self._is_group_message(message) else self.config.quote_reply_private


    def _quote_weights_config(self, message: Dict[str, Any]) -> Any:
        """按会话类型取引用回复**权重**配置节（``[quote_reply_weights]`` / ``[quote_reply_private_weights]``）。

        权重单独成节（``[功能]`` → ``[功能 · 权重]``），因此"功能项"与"权重项"分开取。
        """
        if self._is_group_message(message):
            return self.config.quote_reply_weights
        return self.config.quote_reply_private_weights


    def _weights_for_message(self, message: Dict[str, Any]) -> Dict[str, int]:
        """按会话类型取回复方式权重池（群聊 / 私聊各自独立配置）。"""
        return self._reply_style_weights if self._is_group_message(message) else self._reply_style_weights_private


    def _quote_takeover_active_for(self, message: Dict[str, Any]) -> bool:
        """该会话类型是否接管（宿主前置条件满足 + 对应配置节的 takeover 开启）。"""
        if not self._plugin_enabled():
            return False
        if not self._module_available("quote_reply"):
            return False
        return bool(self._quote_section_config(message).takeover)

    async def _apply_reply_style(self, modified: Dict[str, Any], message: Dict[str, Any]) -> bool:
        """引用回复接管激活时，为**本轮回复的首条文本分段**按权重抽取发送方式。

        群聊与私聊各自使用独立配置节（``[quote_reply]`` / ``[quote_reply_private]``）
        与独立权重池，私聊权重池恒不含 @ 相关项。

        只处理本插件已登记的回复轮（``maisaka.reply.before_post_process`` 标记）：
        轮内目标匹配的第一条消息抽取一次并把轮标记为已抽取，后续分段保持原样。
        轮记录缺失时**不再逐条抽取**——宿主的回复工具对每个分段都会带上
        ``reply_message = 目标消息``（``set_reply=False``），而其它插件/命令响应也可能
        携带 ``reply_message_id``（如 ciallo 的引用注入），逐条抽取会给
        「不是本轮回复的消息」「非纯文本消息」追加引用或 @。

        「回复方式只取首条」（``style_once_per_target``，默认开）：同一条目标消息只有
        **第一条**回复能抽到 @，之后的回复只按"直接/引用"抽；并且抽到 @ 也算用过一次指代
        （计入「同一消息只引用一次」），所以 @/引用 永远不会"迟到"地出现在第二条消息上。
        返回是否真的改动了本次发送。
        """
        if not self._quote_takeover_active_for(message):
            return False

        set_reply = bool(modified.get("set_reply", False))
        if set_reply:
            # 宿主或其它插件（如 ciallo 的 before_send 引用注入）已经决定引用，不重复处理。
            return False
        reply_message_id = str(modified.get("reply_message_id") or "").strip()
        if not reply_message_id:
            return False

        session_id = str(message.get("session_id") or "").strip()
        reply_round = self._reply_rounds.get(session_id) if session_id else None
        if reply_round is None:
            self.ctx.logger.debug(
                "跳过回复方式抽取：会话 %s 没有本轮回复记录（reply_message_id=%s，可能是其它插件的发送）",
                session_id or "?",
                reply_message_id,
            )
            return False

        round_target_id = str(reply_round.get("target_id") or "").strip()
        if not round_target_id or reply_message_id != round_target_id:
            # 同一轮内指向其它消息的发送（如更正段已由宿主原生引用），不动。
            return False
        if reply_round.get("style_done"):
            # 同一轮回复只抽取一次：首条分段生效，其余分段（含富回复的图片/表情分段）保持原样。
            return False
        if not self._has_text_component(message):
            reply_round["style_done"] = True
            self.ctx.logger.debug(
                "跳过回复方式抽取：本轮首条出站消息不含文本组件（会话 %s）", session_id
            )
            return False
        reply_round["style_done"] = True

        # 「回复方式只取首条」：先判断这一轮是不是"对该目标消息的第一条回复"，**再**记账——
        # 记账之后，后续对该目标消息的回复不会再抽到 @（覆盖"第一条没 @、第二条才 @"）。
        style_taken_before = self._style_taken_before(session_id, reply_message_id)
        self._remember_styled_target(session_id, reply_message_id)

        # 「同一消息只引用一次」：优先级最高，先于"目标超时/强制直发/对话已推进"三条过旧规则——
        # 一旦这条目标消息被引用过（本插件抽到引用，或被观测到以引用方式发出），
        # 之后对该消息的任何回复都不再引用，即使已经超出 stale_age_seconds 的时间限制。
        # 抽到"直接回复"不消耗这次机会：下一轮 planner 再回复同一条消息时仍可能抽到引用。
        quote_cfg = self._quote_section_config(message)
        # 是否在 @ 后额外补一个空格组件（默认 False，见 [quote_reply] at_trailing_space）。
        at_with_space = bool(getattr(quote_cfg, "at_trailing_space", False))
        if bool(quote_cfg.quote_once_per_target) and self._quoted_before(session_id, reply_message_id):
            self.ctx.logger.debug(
                "目标消息已被引用过，本次不再引用（会话 %s，reply_message_id=%s，%s）",
                session_id,
                reply_message_id,
                "群聊" if self._is_group_message(message) else "私聊",
            )
            return False

        # 目标消息过旧的规则（超时优先，其次条数）：
        # 1) 目标发出超过 stale_age_seconds 秒 → 强制直接回复（不引用、也不 @）；
        # 2) 目标之后已出现的消息数未达到 force_direct_threshold_messages（强制直发阈值）→
        #    强制直接回复（不引用、也不 @）；
        # 3) 目标之后已出现 ≥ stale_threshold_messages 条消息 → 剔除直接回复、调低 @ 权重。
        stale_threshold = int(quote_cfg.stale_threshold_messages)
        stale_age = float(quote_cfg.stale_age_seconds)
        direct_threshold = self._effective_direct_threshold(quote_cfg)
        weights: Optional[Dict[str, float]] = None
        if stale_threshold > 0 or stale_age > 0 or direct_threshold > 0:
            target_info = await self._lookup_reply_target(reply_message_id)
            if target_info is not None:
                target_ts = target_info[3]
                now_ts = time.time()
                if stale_age > 0 and target_ts > 0 and now_ts - target_ts > stale_age:
                    if not self._stale_age_logged:
                        self._stale_age_logged = True
                        self.ctx.logger.info(
                            "首次触发回复目标超时：目标发出已超过 %.0f 秒（本次实际 %.0f 秒），"
                            "本次起超时回复一律直接回复（不引用、也不 @）",
                            stale_age,
                            now_ts - target_ts,
                        )
                    else:
                        self.ctx.logger.debug(
                            "回复目标超时（距今 %.0f 秒 > %.0f 秒），直接回复（不引用、也不 @）",
                            now_ts - target_ts,
                            stale_age,
                        )
                    return False
                if stale_threshold > 0 or direct_threshold > 0:
                    count = await self._count_messages_after_target(
                        session_id,
                        reply_message_id,
                        target_ts,
                        quote_cfg,
                        limit=max(stale_threshold, direct_threshold) + 1,
                    )
                    if count >= 0:
                        if direct_threshold > 0 and count < direct_threshold:
                            # 「强制直发」：对话还很新（目标后消息不足 direct_threshold 条），
                            # 本次回复强制直接回复——既不引用也不 @。
                            self.ctx.logger.debug(
                                "目标后消息数 %d 未达强制直发阈值 %d，直接回复（不引用、也不 @）",
                                count,
                                direct_threshold,
                            )
                            return False
                        if stale_threshold > 0 and count >= stale_threshold:
                            weights = self._build_stale_weights(
                                self._weights_for_message(message), self._quote_weights_config(message)
                            )
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

        # 私聊只有"引用/不引用"两种可能：权重池与过旧调整都不产生 @（防御性再兜一次）。
        if not self._is_group_message(message):
            if weights is None:
                weights = {name: float(weight) for name, weight in self._weights_for_message(message).items()}
            weights["at"] = 0.0
            weights["quote_at"] = 0.0

        # 「回复方式只取首条」（默认开，`style_once_per_target`）：@ 只允许出现在**对该目标
        # 消息的第一条回复**上。第一条没抽到 @，后续也不会再抽——否则会出现"前一条什么都没
        # 挂、后一条才 @ 人"的错位观感（实测现象）。
        if style_taken_before and bool(getattr(quote_cfg, "style_once_per_target", True)):
            if weights is None:
                weights = {name: float(weight) for name, weight in self._weights_for_message(message).items()}
            weights["at"] = 0.0
            weights["quote_at"] = 0.0
            self.ctx.logger.debug(
                "目标消息的回复方式已抽过，本次不再 @（会话 %s，reply_message_id=%s）",
                session_id,
                reply_message_id,
            )

        style = self._pick_reply_style(weights, message)
        if style == "direct":
            return False

        if style in ("at", "quote_at") and self._is_group_message(message):
            target = await self._lookup_reply_target(reply_message_id)
            if target is not None:
                user_id, nickname, cardname = target[0], target[1], target[2]
                literal_mention = self._leading_text_at_mention(message.get("raw_message"))
                if literal_mention is not None:
                    component_index, mention, mention_name = literal_mention
                    if self._mention_matches_target(mention_name, nickname, cardname):
                        # 正文开头已经是"字面 @目标昵称"（planner 自己写进正文的，不是 at 组件）：
                        # 把它**换成真实 at 组件**，而不是再补一个 @ ——否则渲染出来是两个 @，
                        # 目标还可能收到两次提醒（0.11.3 线上事故：@凯特艾 @凯特爱）。
                        replaced = self._strip_leading_text_mention(
                            message.get("raw_message"), component_index, mention
                        )
                        injected = self._inject_at_component(
                            replaced,
                            user_id=user_id,
                            nickname=nickname,
                            cardname=cardname,
                            with_space=at_with_space,
                        )
                        message["raw_message"] = injected
                        self.ctx.logger.info(
                            "回复方式=%s（字面 @ 升级为真实 @）：前缀组件 %s（会话 %s，引用=%s）",
                            style,
                            self._describe_components(self._component_prefix(injected, 3)),
                            session_id,
                            style == "quote_at",
                        )
                        if bool(getattr(quote_cfg, "style_once_per_target", True)):
                            self._remember_quoted_target(session_id, reply_message_id)
                        if style == "quote_at":
                            modified["set_reply"] = True
                            self._remember_quoted_target(session_id, reply_message_id)
                        return True
                    # 开头 @ 的是别人：正文已经有 @ 了，**不再补第二个**（"一条消息只 @ 一次"）。
                    # 但**空格口径仍要对齐**（0.13.17）：这条路原本"原样发出"，空格数由模型写，
                    # 于是群里会出现 0/1/2 三种；这里统一成"恰好一个半角空格"。
                    normalized_mention = self._normalize_text_mention_spacing(
                        message.get("raw_message"), component_index, mention
                    )
                    if normalized_mention is not None:
                        message["raw_message"] = normalized_mention
                    self.ctx.logger.info(
                        "回复方式=%s（正文开头已有字面 @%s，不是本轮目标，跳过 @ 注入%s）：%s（会话 %s）",
                        style,
                        mention_name,
                        "，仅统一空格" if normalized_mention is not None else "",
                        self._describe_components(self._component_prefix(message.get("raw_message"), 3)),
                        session_id,
                    )
                    if style == "quote_at":
                        modified["set_reply"] = True
                        self._remember_quoted_target(session_id, reply_message_id)
                        return True
                    return normalized_mention is not None
                if self._has_leading_at(message.get("raw_message")):
                    # 消息开头已经有一个 @（planner 富回复 attach_at，或其它插件注入）：
                    # 不重复 @。宿主 1.2.3 的 attach_at 是 [at, ...]、1.2.5+ 是
                    # [at, " ", ...]，其它插件还可能在更前面插引用段，因此判据是
                    # "首个非空文本段之前存在 at"，不是"components[0] 是 at"。
                    #
                    # **但空格口径仍要统一**：0.10.7 起这里不再"直接放行"，而是把已存在的
                    # @ 后面的空白也规范化成一个半角空格（宿主 1.2.3 不加空格、1.2.5+ 加一个、
                    # 别的注入方可能加两个），否则同一个插件在不同来源下会输出 0/1/2 个空格。
                    normalized = self._normalize_leading_at_spacing(
                        message.get("raw_message"), with_space=at_with_space
                    )
                    if normalized is not None:
                        message["raw_message"] = normalized
                        prefix = self._describe_components(self._component_prefix(normalized, 3))
                        self.ctx.logger.info(
                            "回复方式=%s（消息已有 @，仅统一空格）：前缀组件 %s（会话 %s）",
                            style,
                            prefix,
                            session_id,
                        )
                    else:
                        self.ctx.logger.info(
                            "回复方式=%s（消息已有 @，无需改动）（会话 %s）", style, session_id
                        )
                    # 仅"引用＋@"还需要补引用。
                    if style == "quote_at":
                        modified["set_reply"] = True
                        self._remember_quoted_target(session_id, reply_message_id)
                        return True
                    # style == "at" 时不需要 set_reply；但**规范化改动了消息体就必须回传**
                    # （调用方只在返回 True 时才回传 modified_kwargs，否则改动会被丢掉）。
                    return normalized is not None
                injected = self._inject_at_component(
                    message.get("raw_message"),
                    user_id=user_id,
                    nickname=nickname,
                    cardname=cardname,
                    with_space=at_with_space,
                )
                message["raw_message"] = injected
                self.ctx.logger.info(
                    "回复方式=%s（注入 @）：前缀组件 %s（会话 %s，引用=%s）",
                    style,
                    self._describe_components(self._component_prefix(injected, 3)),
                    session_id,
                    style == "quote_at",
                )
                if bool(getattr(quote_cfg, "style_once_per_target", True)):
                    # @ 也算"对这条目标消息用过一次指代"：同一消息只引用一次时，后续回复
                    # 不会再引用它（避免"先 @、再引用同一条"的重复指代）。
                    self._remember_quoted_target(session_id, reply_message_id)
                if style == "quote_at":
                    modified["set_reply"] = True
                    self._remember_quoted_target(session_id, reply_message_id)
                return True
            # @ 目标查不到（消息被清理等）时回退为引用回复。
            self.ctx.logger.debug("@ 目标查询失败（reply_message_id=%s），回退为引用回复", reply_message_id)
        elif style in ("at", "quote_at"):
            # 私聊的权重池已剔除 @ 相关判定，此处仅为兜底（理论上不可达）。
            self.ctx.logger.debug("私聊会话不支持 @ 回复（reply_message_id=%s），回退为引用回复", reply_message_id)

        modified["set_reply"] = True
        self._remember_quoted_target(session_id, reply_message_id)
        return True


    def _pick_reply_style(
        self, weights: Optional[Dict[str, float]] = None, message: Optional[Dict[str, Any]] = None
    ) -> str:
        """按权重抽取回复方式（只从正权重中抽；权重全空时回退为引用回复）。

        ``weights`` 为空时按会话类型取权重池（群聊 / 私聊独立配置）。
        """
        if weights is not None:
            pool: Dict[str, float] = weights
        elif message is not None:
            pool = {name: float(weight) for name, weight in self._weights_for_message(message).items()}
        else:
            pool = {name: float(weight) for name, weight in self._reply_style_weights.items()}
        styles = [name for name, weight in pool.items() if weight > 0]
        if not styles:
            return "quote"
        return random.choices(styles, weights=[float(pool[name]) for name in styles], k=1)[0]

    # 「同一消息只引用一次」记录（群聊 / 私聊各自独立，按 session 存放）
    # ------------------------------------------------------------------

    def _quoted_before(self, session_id: str, target_id: str) -> bool:
        """该会话里这条目标消息是否已经被引用过。"""
        if not session_id or not target_id:
            return False
        bucket = self._quoted_targets.get(session_id)
        return bool(bucket) and target_id in bucket


    def _remember_quoted_target(self, session_id: str, target_id: str) -> None:
        """登记"这条目标消息已经引用过"（容量超限时按记录顺序淘汰最旧的）。"""
        normalized_target = str(target_id or "").strip()
        if not session_id or not normalized_target:
            return
        bucket = self._quoted_targets.get(session_id)
        if bucket is None:
            bucket = OrderedDict()
            self._quoted_targets[session_id] = bucket
        bucket[normalized_target] = time.monotonic()
        bucket.move_to_end(normalized_target)
        while len(bucket) > _QUOTED_TARGET_MAX_ENTRIES:
            bucket.popitem(last=False)


    # 「回复方式只取首条」记录（@ 只允许出现在对该目标消息的第一条回复上）
    # ------------------------------------------------------------------

    def _style_taken_before(self, session_id: str, target_id: str) -> bool:
        """这条目标消息是否已经抽过一次回复方式（即本轮不是"第一条回复"）。"""
        if not session_id or not target_id:
            return False
        bucket = self._styled_targets.get(session_id)
        return bool(bucket) and target_id in bucket


    def _remember_styled_target(self, session_id: str, target_id: str) -> None:
        """登记"对这条目标消息已经抽过一次回复方式"（容量超限时淘汰最旧的）。"""
        normalized_target = str(target_id or "").strip()
        if not session_id or not normalized_target:
            return
        bucket = self._styled_targets.get(session_id)
        if bucket is None:
            bucket = OrderedDict()
            self._styled_targets[session_id] = bucket
        bucket[normalized_target] = time.monotonic()
        bucket.move_to_end(normalized_target)
        while len(bucket) > _STYLED_TARGET_MAX_ENTRIES:
            bucket.popitem(last=False)


    def _build_stale_weights(self, base: Dict[str, int], cfg: Any) -> Dict[str, float]:
        """构造"对话已推进"时的回复方式权重：剔除直接回复，@权重调为指定值或原值一半。

        ``base`` 为该会话类型的权重池（群聊含 @、私聊恒不含 @）；
        ``cfg`` 为对应的**权重**配置节（``[quote_reply_weights]`` / ``[quote_reply_private_weights]``）。

        ``stale_at_weight`` 的三档语义（与 WebUI 标签/README 一致）：

        * **留空** → 纯 @ 权重取原值的一半（``quote_at`` 不动）；
        * **正数** → 纯 @ 权重取该值（``quote_at`` 不动）；
        * **显式填 ``0``** → 文档承诺的是"**不再 @**"，@ 是"@ 类回复"（纯 @ + 引用＋@），
          所以这里**同时把 ``quote_at`` 清零**——否则用户按说明填 0 之后仍会以
          「引用＋@」@ 人（约 1/8 概率），看着像"设了没用"。
        """
        weights: Dict[str, float] = {name: float(weight) for name, weight in base.items()}
        weights["direct"] = 0.0
        if "at" not in weights:
            return weights
        # 留空（""）= 自动取原 @ 权重的一半；填数字则用填的值（0 = 不再 @）。
        raw_stale_at = getattr(cfg, "stale_at_weight", "")
        explicit = str(raw_stale_at).strip()
        if explicit == "":
            weights["at"] = max(0.0, float(base.get("at", 0)) / 2.0)
            return weights
        try:
            value = max(0.0, float(explicit))
        except (TypeError, ValueError):
            weights["at"] = max(0.0, float(base.get("at", 0)) / 2.0)
            return weights
        weights["at"] = value
        if value <= 0.0 and "quote_at" in weights:
            weights["quote_at"] = 0.0
        return weights


    def _effective_direct_threshold(self, quote_cfg: Any) -> int:
        """取「强制直发阈值」的有效值：必须**小于**「对话已推进阈值」，超出时为旧阈值让步。

        语义（与 WebUI 标签/字段说明一致）：目标之后的消息数**未达到**该值（对话还很新）
        时强制直接回复（不引用、也不 @）。本项与决定"是否剔除直接回复"的
        ``stale_threshold_messages``（对话已推进阈值）互补，因此必须小于它：
        配置值 ≥ 旧阈值时自动压到「对话已推进阈值 − 1」（例如旧阈值 3、本项填 4 → 按 2 生效；
        旧阈值为 1 时压到 0 = 关闭）。旧阈值本身为 0（未启用对话已推进判定）时不设上限。
        0 = 关闭。首次压制时用 info 记录，后续降为 debug。
        """
        try:
            raw = int(getattr(quote_cfg, "force_direct_threshold_messages", 0) or 0)
        except (TypeError, ValueError):
            return 0
        if raw <= 0:
            return 0
        stale_threshold = int(quote_cfg.stale_threshold_messages)
        if stale_threshold <= 0 or raw < stale_threshold:
            return raw
        clamped = max(0, stale_threshold - 1)
        if not getattr(self, "_direct_threshold_clamped_logged", False):
            self._direct_threshold_clamped_logged = True
            self.ctx.logger.info(
                "强制直发阈值 %d 不小于对话已推进阈值 %d，为旧阈值让步：按 %d 生效",
                raw,
                stale_threshold,
                clamped,
            )
        else:
            self.ctx.logger.debug(
                "强制直发阈值 %d 被对话已推进阈值 %d 压制，按 %d 生效",
                raw,
                stale_threshold,
                clamped,
            )
        return clamped

    async def _count_messages_after_target(
        self, session_id: str, target_id: str, target_ts: float, cfg: Any, limit: Optional[int] = None
    ) -> int:
        """统计目标消息之后入库的消息条数；失败或时间戳缺失返回 -1（回退为不调整）。

        查询区间为 ``[目标时间戳, 现在]``，limit 默认取 ``对话已推进阈值+1``（latest 模式）：
        区间内消息多于阈值时必然返回阈值+1 条，足以判定；目标消息自身按
        message_id 剔除。传入 ``limit`` 时（「强制直发阈值」与「对话已推进阈值」共用一次
        统计）取 ``max(两个阈值) + 1``，足以同时判定两条阈值。``cfg`` 是该会话类型的
        功能配置节（群聊 / 私聊阈值各自独立）。
        """
        threshold = int(limit if limit is not None else cfg.stale_threshold_messages)
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
    def _has_text_component(message: Dict[str, Any]) -> bool:
        """消息是否含非空文本组件（纯表情/图片/语音/转发等不算）。"""
        components = message.get("raw_message")
        if not isinstance(components, list):
            return False
        return any(
            isinstance(component, dict)
            and str(component.get("type") or "") == "text"
            and str(component.get("data") or "").strip()
            for component in components
        )

    @staticmethod
    def _component_prefix(components: Any, count: int) -> List[Any]:
        """取组件列表前 ``count`` 项（日志用；非列表时返回空）。"""
        if not isinstance(components, list):
            return []
        return list(components[:count])

    @staticmethod
    def _describe_components(components: Sequence[Any]) -> str:
        """把组件前缀渲染成一行可读日志（`at:昵称` / `text:' '` / `reply`）。

        用途：确认"@ 与正文之间到底几个空格"这类只能看实际组件列表的问题。
        """
        parts: List[str] = []
        for component in components:
            if not isinstance(component, dict):
                parts.append(f"{type(component).__name__}")
                continue
            component_type = str(component.get("type") or "?")
            data = component.get("data")
            if component_type == "text":
                parts.append(f"text:{data!r}")
            elif component_type == "at":
                target = ""
                if isinstance(data, dict):
                    target = str(
                        data.get("target_user_cardname")
                        or data.get("target_user_nickname")
                        or data.get("target_user_id")
                        or ""
                    )
                parts.append(f"at:{target}")
            else:
                parts.append(component_type)
        return "[" + ", ".join(parts) + "]"

    @classmethod
    def _normalize_leading_at_spacing(
        cls, raw_message: Any, *, with_space: bool = False
    ) -> Optional[List[Any]]:
        """规范化"已存在的开头 @"后面的空白。返回 ``None`` 表示**无需改动**。

        为什么需要：``attach_at`` / 别的插件注入的 @ 后面带几个空格不由我们决定
        （宿主 1.2.3 是 ``[at, ...]``、1.2.5+ 是 ``[at, " ", ...]``、其它插件可能更多），
        不统一就会出现"有时候一个空格、有时候两个"。

        口径（0.13.12 起）：

        - ``with_space=False``（**默认**）：**删掉** at 后面所有纯空白文本段，只留
          ``[at, 正文]`` —— 宿主渲染时 ``" ".join`` 会自己补一个空格，正好一个；
        - ``with_space=True``：把空白段并成**恰好一个半角空格**组件（0.10.8 的旧口径）；
        - 两种口径都会抹掉正文自带的前导空白。
        """
        if not isinstance(raw_message, list):
            return None
        components: List[Any] = list(raw_message)
        at_index: Optional[int] = None
        for index, component in enumerate(components):
            if not isinstance(component, dict):
                continue
            component_type = str(component.get("type") or "")
            if component_type == "at":
                at_index = index
                break
            if component_type == "text" and str(component.get("data") or "").strip():
                return None  # 正文之前没有 at，不归这里管
        if at_index is None:
            return None

        # at 之后连续的空白文本段：并成一个空格（删除多余的）。
        cursor = at_index + 1
        whitespace_indices: List[int] = []
        while cursor < len(components):
            component = components[cursor]
            if not isinstance(component, dict) or str(component.get("type") or "") != "text":
                break
            if str(component.get("data") or "").strip():
                break
            whitespace_indices.append(cursor)
            cursor += 1

        changed = False
        if with_space:
            if whitespace_indices:
                first = whitespace_indices[0]
                if len(whitespace_indices) > 1 or str(components[first].get("data") or "") != " ":
                    changed = True
                components[first] = {"type": "text", "data": " "}
                for index in reversed(whitespace_indices[1:]):
                    components.pop(index)
                    changed = True

            # at 后面直接跟正文（0 个空格）：补一个空格。
            if not whitespace_indices:
                body_index = at_index + 1
                has_body_text = any(
                    isinstance(item, dict)
                    and str(item.get("type") or "") == "text"
                    and str(item.get("data") or "").strip()
                    for item in components[body_index:]
                )
                if has_body_text:
                    components.insert(at_index + 1, {"type": "text", "data": " "})
                    changed = True
        elif whitespace_indices:
            # 默认口径：at 后面的纯空白段全部删掉（宿主渲染时自己会补一个空格）。
            for index in reversed(whitespace_indices):
                components.pop(index)
                changed = True

        # 正文自带前导空白（"@某人  正文"的另一种来源）：抹掉。
        for index, component in enumerate(components):
            if not isinstance(component, dict) or str(component.get("type") or "") != "text":
                continue
            data = str(component.get("data") or "")
            if not data.strip():
                continue
            stripped = data.lstrip()
            if stripped != data:
                components[index] = {"type": "text", "data": stripped}
                changed = True
            break

        return components if changed else None

    @staticmethod
    def _leading_text_at_mention(raw_message: Any) -> Optional[Tuple[int, str, str]]:
        """正文开头若是**字面** ``@昵称``，返回 ``(组件下标, 提及原文, 昵称)``；否则 ``None``。

        为什么需要：planner 有时会把 @ 直接写进正文（``@凯特艾 行吧多想了``），那只是普通
        文本、不是 at 组件。此时若再注入一个真实 at，一条消息里就会出现两个 @；若那个字面
        昵称又被错字生成器改过（宿主会改），两个 @ 的名字还不一样。
        遇到真实 at 组件（``_has_leading_at`` 的管辖范围）或开头没有 @ 时返回 ``None``。
        """
        components = raw_message if isinstance(raw_message, list) else []
        for index, component in enumerate(components):
            if not isinstance(component, dict):
                continue
            component_type = str(component.get("type") or "")
            if component_type == "at":
                return None  # 已有真实 at 组件：交给 _has_leading_at 处理
            if component_type != "text":
                continue
            data = str(component.get("data") or "")
            if not data.strip():
                continue
            mention = match_leading_at_mention(data)
            if not mention:
                return None
            return index, mention, mention[1:]
        return None

    @staticmethod
    def _mention_matches_target(name: str, nickname: str, cardname: str) -> bool:
        """字面 @ 的昵称是否就是被回复消息的发送者（群名片优先，昵称兜底）。"""
        normalized = str(name or "").strip().casefold()
        if not normalized:
            return False
        for candidate in (cardname, nickname):
            text = str(candidate or "").strip().casefold()
            if text and text == normalized:
                return True
        return False

    @staticmethod
    def _strip_leading_text_mention(raw_message: Any, component_index: int, mention: str) -> List[Any]:
        """把正文开头那段**字面** @提及 从文本组件里删掉（供随后注入真实 at 组件）。

        返回新列表、不改入参；删完的剩余空白由 ``_inject_at_component`` 统一规范化。
        """
        components: List[Any] = list(raw_message) if isinstance(raw_message, list) else []
        if not (0 <= component_index < len(components)):
            return components
        component = components[component_index]
        if not isinstance(component, dict):
            return components
        data = str(component.get("data") or "")
        remainder = data.lstrip()[len(mention):]
        components[component_index] = {**component, "data": remainder.lstrip()}
        return components

    @staticmethod
    def _normalize_text_mention_spacing(
        raw_message: Any, component_index: int, mention: str
    ) -> Optional[List[Any]]:
        """把**字面** @提及 后面的空白规范成**恰好一个半角空格**；无需改动返回 ``None``。

        为什么需要（0.13.17）：正文开头是"字面 @别人"时本插件会**跳过 @ 注入、原样发出**，
        于是那条消息的空格数**完全由模型写的文本决定**（实测 0 / 1 / 2 都可能）——
        而插件自己注入真实 at 时空格数是**恒定**的（由 ``at_trailing_space`` 决定 0 或 1）。
        两条路口径不一致，群里就会看到"有时候 @ 后没空格、有时候一个、有时候两个"。
        这里把字面 @ 也对齐到"恰好一个"。

        只处理"后面确实还有正文"的情况（只剩 ``@某人`` 时不动，免得把消息弄空）。
        """
        components: List[Any] = list(raw_message) if isinstance(raw_message, list) else []
        if not (0 <= component_index < len(components)):
            return None
        component = components[component_index]
        if not isinstance(component, dict):
            return None
        data = str(component.get("data") or "")
        head = data.lstrip()
        if not head.startswith(mention):
            return None
        remainder = head[len(mention) :].lstrip()
        if not remainder:
            return None
        normalized = f"{mention} {remainder}"
        if normalized == data:
            return None
        components[component_index] = {**component, "data": normalized}
        return components

    @staticmethod
    def _has_leading_at(raw_message: Any) -> bool:
        """消息开头是否已经有一个 @（planner 富回复 attach_at / 宿主或其它插件注入）。

        判据：**第一个非空文本段之前存在 at 组件**（没有非空文本段时，只要出现过 at 就算）。
        不能只看 ``components[0]``：

        - 宿主 1.2.3 的 ``attach_at`` 前缀是 ``[at, ...]``，1.2.5+ 是 ``[at, " ", ...]``；
        - 其它插件（如引用注入插件）还可能在更前面插一个引用段，此时 ``components[0]``
          不是 at，但开头确实已经有 @，再注入就会变成两个 @、两个空格。
        """
        components = raw_message if isinstance(raw_message, list) else []
        for component in components:
            if not isinstance(component, dict):
                continue
            component_type = str(component.get("type") or "")
            if component_type == "at":
                return True
            if component_type == "text" and str(component.get("data") or "").strip():
                # 正文之前没有 at：开头没有 @。
                return False
        return False

    @staticmethod
    def _strip_body_leading_whitespace(components: List[Any]) -> List[Any]:
        """抹掉正文自带的前导空白，保证注入 @ 后恒为**一个**半角空格。

        规则（返回新列表，不修改入参）：

        - 第一个"非空文本段"的前导空白一律 ``lstrip``（半角空格、全角空格 U+3000、
          Tab、NBSP 都算 —— Python ``str.isspace()`` 全认）；
        - 它之前若还有"纯空白文本段"，一并清空，避免与注入的那个空格叠加成两个。
        """
        normalized: List[Any] = list(components)
        for index, component in enumerate(normalized):
            if not (isinstance(component, dict) and str(component.get("type") or "") == "text"):
                continue
            data = str(component.get("data") or "")
            if not data.strip():
                if data:
                    normalized[index] = {**component, "data": ""}
                continue
            stripped = data.lstrip()
            if stripped != data:
                normalized[index] = {**component, "data": stripped}
            return normalized
        return normalized

    @classmethod
    def _inject_at_component(
        cls,
        raw_message: Any,
        *,
        user_id: str,
        nickname: str,
        cardname: str,
        with_space: bool = False,
    ) -> List[Any]:
        """把 at 组件插入组件列表首位（返回新列表，不修改入参）。

        空格口径（0.13.12 起，由 ``[quote_reply] at_trailing_space`` 决定）：

        - 注入前**一律**先 ``_strip_body_leading_whitespace`` 抹掉正文自带的前导空白 ——
          这一步无论补不补空格都要做，否则正文自带的空白会和渲染层的空格叠加；
        - ``with_space=False``（**默认**）：只插 ``at``，**不插空格组件**。
          宿主 ``send_service._build_processed_plain_text()`` 用 ``" ".join(parts)`` 拼接
          每个组件、**组件之间自己会补一个空格**，所以 ``[at, 正文]`` 渲染出来正好是
          ``@昵称 正文``（一个空格）。此时若再插一个空格组件，就会渲染成
          ``@昵称`` + join空格 + ``' '`` + join空格 + ``正文`` = **三个空格**，
          而且会跟着 ``processed_plain_text`` 进消息库、上下文与记忆抽取。
          宿主 1.2.3 自己的 ``attach_at`` 也是 ``[at, 正文]``，口径一致。
        - ``with_space=True``：保留旧行为（恒定补一个半角空格组件），
          给"某些客户端 at 段后不自动补空格"导致粘连的环境用。

        宿主自己已经 @ 过的情况不归这里管（调用方用 ``_has_leading_at`` 先判断）。
        """
        components = cls._strip_body_leading_whitespace(
            list(raw_message) if isinstance(raw_message, list) else []
        )
        at_component = {
            "type": "at",
            "data": {
                "target_user_id": user_id,
                "target_user_nickname": nickname or None,
                "target_user_cardname": cardname or None,
            },
        }
        prefix: List[Any] = [at_component]
        # 正文里还有文本组件时才补空格；消息完全没有文本（纯图片/表情）时不补，
        # 免得留下一个悬挂的空格段。
        if with_space and any(
            isinstance(component, dict) and str(component.get("type") or "") == "text"
            for component in components
        ):
            prefix.append({"type": "text", "data": " "})
        return [*prefix, *components]


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
