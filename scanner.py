"""扫描器：对群聊候选用户逐个检查其与 bot 的私聊，判定是否触发/排队总结。

一轮扫描流程：
1. 确定候选用户（focus_user_ids 优先，否则取群最近 scan_count 条非 bot 消息的用户）；
2. 用户名单过滤（``user_list`` + ``user_list_mode``：黑名单模式名单内跳过、白名单模式仅名单内保留）；
3. 逐个用户：查私聊流 → 取 24h 窗口内私聊消息 → 按条件判定（首次总结 /
   重总结队列 / 置队标记 / 不满足）；
4. TTL 淘汰过期总结。

迁移自 ``cateye_mai_cross-chat_recall/scanner.py``（阶段 3 合并，逐行等价），
唯一差异：配置读取口径改为合并后的**单节平铺** ``[cross_chat_recall]``
（原 ``config.scan`` / ``config.summarize`` / ``config.scope`` / ``config.store``
四个子节降级为该节内的扁平字段，见计划书 6.2 的"嵌套风险预案"）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional

from .helpers import allowed_by_list, extract_message_fields, msg_timestamp, unwrap_messages
from .logging_setup import get_logger

_PRIVATE_FETCH_LIMIT = 500
_SUMMARY_FAIL_COOLDOWN = 600.0  # 总结失败后的冷却窗口（秒），防止模型持续失败时每轮扫描重复烧 token


class Scanner:
    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._logger = get_logger("scanner")
        # 总结失败冷却：user_id -> 上次失败时刻
        self._fail_cooldown: Dict[str, float] = {}

    # ------------------------------------------------------------------
    async def scan_stream(
        self,
        session_id: str,
        *,
        focus_user_ids: Optional[List[str]] = None,
        reason: str = "",
    ) -> None:
        """对指定群聊流执行一轮扫描（含私聊检查与总结触发）。"""
        cfg = self._plugin.config.cross_chat_recall
        store = self._plugin.store
        started = time.time()
        self._logger.info(
            "[扫描] 开始 stream=%s reason=%r focus=%s 扫描数=%s",
            session_id, reason, focus_user_ids or "-", cfg.scan_count,
        )

        stream = await self._plugin.get_stream(session_id)
        platform = str((stream or {}).get("platform") or "qq")

        # 1. 候选用户
        if focus_user_ids:
            candidates = list(dict.fromkeys(str(u) for u in focus_user_ids if str(u).strip()))
            self._logger.info("[扫描] 定向候选用户（%d 个）: %s", len(candidates), candidates)
        else:
            candidates = await self._collect_candidates(session_id, int(cfg.scan_count))
        if not candidates:
            self._logger.info("[扫描] 无候选用户，本轮结束（耗时 %.2fs）", time.time() - started)
            return

        # 2. 用户名单（按 user_list_mode 解释：黑名单=名单内跳过；白名单=仅名单内保留）
        before = len(candidates)
        candidates = [u for u in candidates if allowed_by_list(u, cfg.user_list, cfg.user_list_mode)]
        if len(candidates) != before:
            self._logger.info("[扫描] 用户名单过滤：%d → %d 个候选用户", before, len(candidates))
        if not candidates:
            self._logger.info("[扫描] 过滤后无候选用户，本轮结束")
            return

        # 3. 逐用户处理
        now = time.time()
        window_start = now - float(cfg.window_hours) * 3600
        stats = {"summarized": 0, "queued": 0, "skipped": 0, "no_private": 0}
        for uid in candidates:
            try:
                outcome = await self._process_user(uid, platform, window_start, now)
                stats[outcome] = stats.get(outcome, 0) + 1
            except Exception:
                self._logger.exception("[扫描] 处理用户 %s 时异常", uid)
                stats["skipped"] = stats.get("skipped", 0) + 1

        # 4. TTL 淘汰
        deleted = store.cleanup(float(cfg.summary_ttl_hours))

        self._logger.info(
            "[扫描] 结束 stream=%s 候选=%d 总结=%d 入队=%d 跳过=%d 无私聊=%d TTL淘汰=%d（耗时 %.2fs）",
            session_id, len(candidates), stats["summarized"], stats["queued"],
            stats["skipped"], stats["no_private"], deleted, time.time() - started,
        )

    # ------------------------------------------------------------------
    async def _collect_candidates(self, session_id: str, scan_count: int) -> List[str]:
        """取群最近 scan_count 条非 bot 消息中出现过的用户 id（按新到旧去重）。

        消息不足 scan_count 条时回退为全部可用消息（全扫描兜底）。
        """
        raw = await self._plugin.ctx.message.get_recent(chat_id=session_id, limit=max(scan_count * 3, 30))
        messages = unwrap_messages(raw, self._logger, "get_recent")
        if not messages:
            self._logger.info("[扫描] 群 %s 最近无消息", session_id)
            return []

        fresh: List[Mapping[str, Any]] = []
        for msg in messages:
            fields = extract_message_fields(msg)
            if fields.get("is_self") or fields.get("is_synthetic") or not fields.get("user_id"):
                continue
            fresh.append(msg)
        fresh.sort(key=msg_timestamp, reverse=True)

        if len(fresh) < scan_count:
            self._logger.info(
                "[扫描] 非 bot 消息不足扫描数（%d < %d），回退为全量扫描可用消息", len(fresh), scan_count,
            )
        else:
            fresh = fresh[:scan_count]

        candidates: List[str] = []
        for msg in fresh:
            uid = extract_message_fields(msg).get("user_id") or ""
            if uid and uid not in candidates:
                candidates.append(uid)
        self._logger.info("[扫描] 群 %s 候选用户（%d 个）: %s", session_id, len(candidates), candidates)
        return candidates

    # ------------------------------------------------------------------
    async def _process_user(self, user_id: str, platform: str, window_start: float, now: float) -> str:
        """检查单个用户与 bot 的私聊；返回结果类别。"""
        cfg = self._plugin.config.cross_chat_recall
        store = self._plugin.store

        # 私聊流（区分群聊/私聊：这里只认 user 维度的私聊流）
        pstream = await self._get_private_stream(user_id, platform)
        if not pstream:
            self._logger.debug("用户 %s：与 bot 无私聊流，跳过", user_id)
            return "no_private"
        private_stream_id = str(pstream.get("stream_id") or pstream.get("session_id") or "")
        nickname = str(pstream.get("user_nickname") or pstream.get("user_cardname") or "").strip()
        if not nickname:
            nickname = user_id

        # 窗口内私聊消息（用户 + bot 都要，供 LLM 总结）
        raw = await self._plugin.ctx.message.get_by_time_in_chat(
            private_stream_id,
            start_time=str(window_start),
            end_time=str(now),
            filter_mai=False,
            limit=_PRIVATE_FETCH_LIMIT,
        )
        messages = unwrap_messages(raw, self._logger, "get_by_time_in_chat(private)")
        kept: List[Mapping[str, Any]] = []
        for msg in messages:
            fields = extract_message_fields(msg)
            if fields.get("is_synthetic") or not fields.get("user_id"):
                continue
            kept.append(msg)
        kept.sort(key=msg_timestamp)
        if len(messages) >= _PRIVATE_FETCH_LIMIT:
            self._logger.warning("用户 %s：窗口内私聊消息达到抓取上限 %d，可能被截断", user_id, _PRIVATE_FETCH_LIMIT)

        user_msgs = [m for m in kept if not extract_message_fields(m).get("is_self")]
        bot_msgs = [m for m in kept if extract_message_fields(m).get("is_self")]
        user_count = len(user_msgs)
        self._logger.debug(
            "用户 %s：私聊流=%s 窗口内消息 用户%d条/bot%d条", user_id, private_stream_id, user_count, len(bot_msgs),
        )

        row = store.get(user_id)
        pending = bool(row and row.get("pending_resummarize"))

        # 条件一：窗口内来自用户的信息数达到阈值（且未总结过或已排队）
        if user_count < int(cfg.min_user_messages) and not pending:
            self._logger.info(
                "用户 %s：窗口内用户消息 %d 条 < 阈值 %d，不触发总结", user_id, user_count, cfg.min_user_messages,
            )
            return "skipped"

        # 条件二：上次总结失败未出冷却窗口，暂不重试（防每轮扫描重复烧 token）
        fail_ts = self._fail_cooldown.get(user_id)
        if fail_ts is not None:
            remain = _SUMMARY_FAIL_COOLDOWN - (now - fail_ts)
            if remain > 0:
                self._logger.info("用户 %s：总结失败冷却中（剩 %.0f 秒），本轮跳过", user_id, remain)
                return "skipped"
            self._fail_cooldown.pop(user_id, None)

        if row is None:
            self._logger.info("用户 %s：首次满足总结条件（用户消息 %d 条）→ 立即总结", user_id, user_count)
            return await self._do_summarize(user_id, nickname, platform, private_stream_id, kept, user_msgs, now)
        if pending:
            self._logger.info("用户 %s：命中重总结队列 → 立即重新总结（全量重写）", user_id)
            return await self._do_summarize(user_id, nickname, platform, private_stream_id, kept, user_msgs, now)

        # 已有总结：检查推进数，达标后进入"可被重新总结"队列（不立刻总结）
        last_end_ts = float(row.get("end_ts") or 0)
        new_count = sum(1 for m in user_msgs if msg_timestamp(m) > last_end_ts)
        if new_count >= int(cfg.resummarize_push_count):
            store.set_pending(user_id, True)
            self._logger.info(
                "用户 %s：距上次总结新推进 %d 条用户消息 ≥ %d → 进入重总结队列（等待下次扫描触发）",
                user_id, new_count, cfg.resummarize_push_count,
            )
            return "queued"
        self._logger.debug(
            "用户 %s：距上次总结新推进 %d 条 < %d，暂不排队", user_id, new_count, cfg.resummarize_push_count,
        )
        return "skipped"

    # ------------------------------------------------------------------
    async def _do_summarize(
        self,
        user_id: str,
        nickname: str,
        platform: str,
        private_stream_id: str,
        kept: List[Mapping[str, Any]],
        user_msgs: List[Mapping[str, Any]],
        now: float,
    ) -> str:
        summary = await self._plugin.summarizer.summarize(
            user_id=user_id, nickname=nickname, messages=list(kept),
        )
        if not summary:
            self._fail_cooldown[user_id] = time.time()
            self._logger.warning(
                "用户 %s：总结失败，%d 秒内不再对该用户发起总结", user_id, _SUMMARY_FAIL_COOLDOWN,
            )
            return "skipped"
        self._fail_cooldown.pop(user_id, None)
        timestamps = [ts for ts in (msg_timestamp(m) for m in kept) if ts > 0]
        start_ts = min(timestamps, default=now)
        end_ts = max(timestamps, default=now)  # 全部时间不可解析时按扫描时刻兜底，避免入库即过期
        self._plugin.store.upsert(
            user_id=user_id,
            platform=platform,
            private_stream_id=private_stream_id,
            user_nickname=nickname,
            summary=summary,
            start_ts=start_ts,
            end_ts=end_ts,
            user_msg_count=len(user_msgs),
        )
        return "summarized"

    # ------------------------------------------------------------------
    async def _get_private_stream(self, user_id: str, platform: str) -> Optional[Dict[str, Any]]:
        """按用户查与 bot 的私聊流；返回 None 表示无私聊。"""
        try:
            raw = await self._plugin.ctx.chat.get_stream_by_user_id(user_id, platform=platform or "qq")
        except Exception as exc:
            self._logger.warning("用户 %s：查私聊流异常: %s", user_id, exc)
            return None
        if raw is None:
            return None
        stream = raw.get("stream") if isinstance(raw, Mapping) and isinstance(raw.get("stream"), Mapping) else raw
        if not isinstance(stream, Mapping):
            self._logger.warning("用户 %s：私聊流返回形态异常（%s）", user_id, type(raw).__name__)
            return None
        return dict(stream)
