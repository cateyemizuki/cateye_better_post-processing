"""按聊天流独立的 @ 冷却（默认开启，15 秒）。

目标：避免 bot 在一个群里连续多条回复都 @ 同一个人/不同人，造成刷屏式提醒。冷却按
``session_id`` 独立，群聊与私聊、不同群之间互不影响。

两条来源都管：

* **宿主/其它插件产出的 @**：``send_service.before_send`` 里直接检查出站消息的 ``at``
  组件，冷却中整段移除（并清掉紧随其后的空格文本组件）；
* **本插件自己产出的 @**：``quote_takeover`` 抽回复方式时把 ``at`` / ``quote_at`` 权重清零；
  撤回重发头部恢复 @ 的分支也跳过。``before_send`` 末尾再做一次兜底移除。

"直接探查上下文"：每个聊天流**首次**出站时，用 ``message.get_recent`` 扫最近
``_AT_COOLDOWN_SCAN_LIMIT`` 条消息，找 bot 自己带 ``at`` 组件的消息，按消息时间戳初始化
冷却——即使上一次 @ 是宿主或别的插件发的、发生在插件加载之前，也能接上。之后每次送达
（``send_service.after_send`` 且 ``sent=True``）观察到 ``at`` 就刷新冷却起点。

时间统一用单调时钟存状态；上下文消息时间戳（wall time）只在初始化时换算成"距今多久"。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Mapping

from ..helpers import extract_message_fields, msg_timestamp, unwrap_messages

# 首次初始化时扫多少条最近消息（足够覆盖 15 秒的活跃群；扫不到就按无冷却处理）。
_AT_COOLDOWN_SCAN_LIMIT = 50
# 冷却状态保留上限：超过 1 小时没再 @ 的会话直接丢弃，防止长期运行内存膨胀。
_AT_COOLDOWN_STATE_TTL = 3600.0
_AT_COOLDOWN_MAX_SESSIONS = 1000


class AtCooldownMixin:
    """``@`` 冷却的判定、上下文初始化、出站 at 清洗与送达登记。"""

    # ------------------------------------------------------------------
    # 配置与状态
    # ------------------------------------------------------------------

    def _at_cooldown_enabled(self) -> bool:
        enabled_fn = getattr(self, "_plugin_enabled", None)
        if callable(enabled_fn):
            try:
                if not enabled_fn():
                    return False
            except Exception:
                pass
        cfg = getattr(self.config, "at_cooldown", None)
        return bool(getattr(cfg, "enabled", True))

    def _at_cooldown_seconds(self) -> float:
        cfg = getattr(self.config, "at_cooldown", None)
        try:
            value = float(getattr(cfg, "seconds", 15.0))
        except (TypeError, ValueError):
            value = 15.0
        return max(0.0, value)

    def _at_cooldown_remaining(self, session_id: str, now: float | None = None) -> float:
        """该会话的 @ 冷却剩余秒数（0 = 不在冷却）。"""
        if not self._at_cooldown_enabled():
            return 0.0
        seconds = self._at_cooldown_seconds()
        sid = str(session_id or "").strip()
        if not sid or seconds <= 0:
            return 0.0
        moment = time.monotonic() if now is None else float(now)
        last = self._at_cooldown_last.get(sid)
        if last is None:
            return 0.0
        remaining = seconds - (moment - float(last))
        if remaining <= 0:
            self._at_cooldown_last.pop(sid, None)
            return 0.0
        return remaining

    def _at_cooldown_active(self, session_id: str) -> bool:
        return self._at_cooldown_remaining(session_id) > 0.0

    def _mark_at_cooldown(self, session_id: str, *, when: float | None = None) -> None:
        """登记一次"刚刚 @ 过"（``when`` 为单调时钟；默认现在）。"""
        sid = str(session_id or "").strip()
        if not sid or not self._at_cooldown_enabled():
            return
        self._at_cooldown_last[sid] = time.monotonic() if when is None else float(when)
        while len(self._at_cooldown_last) > _AT_COOLDOWN_MAX_SESSIONS:
            oldest = min(self._at_cooldown_last, key=self._at_cooldown_last.get)
            self._at_cooldown_last.pop(oldest, None)

    def _prune_at_cooldown_state(self, now: float) -> None:
        """清理长期不活跃的会话状态（由插件的周期清理调用）。"""
        for session_id, last in list(self._at_cooldown_last.items()):
            if now - float(last) > _AT_COOLDOWN_STATE_TTL:
                self._at_cooldown_last.pop(session_id, None)
        if len(self._at_cooldown_seeded) > _AT_COOLDOWN_MAX_SESSIONS:
            self._at_cooldown_seeded.clear()

    # ------------------------------------------------------------------
    # 消息扫描 / at 组件清洗
    # ------------------------------------------------------------------

    @staticmethod
    def _message_has_at(message: Any) -> bool:
        """消息载荷里是否带真实 ``at`` 组件（不是文本里手写的 @）。"""
        if not isinstance(message, Mapping):
            return False
        components = message.get("raw_message")
        if not isinstance(components, list):
            return False
        return any(
            isinstance(component, Mapping) and str(component.get("type") or "") == "at"
            for component in components
        )

    @staticmethod
    def _strip_at_components(message: Dict[str, Any]) -> int:
        """原地移除消息里的全部 ``at`` 组件；返回移除数量。

        ``at`` 后面紧跟的文本组件若以空白开头，一并去掉那段空白（宿主 attach_at 会插
        ``[at, " ", 正文]``，只删 at 会留下前导空格）；纯空白文本组件直接丢弃。
        """
        components = message.get("raw_message")
        if not isinstance(components, list):
            return 0
        cleaned: list[Any] = []
        removed = 0
        trim_next = False
        for component in components:
            if not isinstance(component, Mapping):
                cleaned.append(component)
                trim_next = False
                continue
            if str(component.get("type") or "") == "at":
                removed += 1
                trim_next = True
                continue
            if trim_next and str(component.get("type") or "") == "text":
                text = str(component.get("data") or "")
                stripped = text.lstrip(" \t\u3000\u00a0")
                trim_next = False
                if not stripped:
                    continue
                if stripped != text:
                    component = {**component, "data": stripped}
                cleaned.append(component)
                continue
            trim_next = False
            cleaned.append(component)
        if removed:
            message["raw_message"] = cleaned
        return removed

    def _note_outbound_at(self, session_id: str, message: Any) -> None:
        """送达观察：出站消息带 at 就刷新该会话的冷却起点。"""
        if not self._at_cooldown_enabled():
            return
        if not self._message_has_at(message):
            return
        self._mark_at_cooldown(session_id)
        self.ctx.logger.debug(
            "@ 冷却已刷新：会话 %s 最近一次 @ 为刚刚（冷却 %.0f 秒）",
            str(session_id or "?")[:12],
            self._at_cooldown_seconds(),
        )

    async def _seed_at_cooldown_from_context(self, session_id: str) -> None:
        """首次出站时扫最近消息里 bot 的 @，把冷却接上（每个会话只扫一次）。

        只看 bot 自己发的消息（``extract_message_fields().is_self``），只认真实 ``at``
        组件；消息时间戳取自消息本身，超过冷却窗口的直接忽略。扫描失败不影响发送。
        """
        sid = str(session_id or "").strip()
        if not sid or not self._at_cooldown_enabled() or self._at_cooldown_seconds() <= 0:
            return
        if sid in self._at_cooldown_seeded:
            return
        self._at_cooldown_seeded.add(sid)
        try:
            raw = await self.ctx.message.get_recent(
                chat_id=sid, limit=_AT_COOLDOWN_SCAN_LIMIT
            )
        except Exception as exc:
            self.ctx.logger.debug("@ 冷却初始化：读取最近消息失败（会话 %s）: %s", sid[:12], exc)
            return
        messages = unwrap_messages(raw, self.ctx.logger, "@冷却初始化")
        now_wall = time.time()
        latest = 0.0
        for message in messages:
            fields = extract_message_fields(message)
            if not fields.get("is_self") or not self._message_has_at(message):
                continue
            stamp = msg_timestamp(message)
            if stamp > latest:
                latest = stamp
        if latest <= 0:
            self.ctx.logger.debug("@ 冷却初始化：最近消息里没有 bot 的 @（会话 %s）", sid[:12])
            return
        age = max(0.0, now_wall - latest)
        if age >= self._at_cooldown_seconds():
            return
        self._at_cooldown_last[sid] = time.monotonic() - age
        self.ctx.logger.info(
            "@ 冷却初始化：上下文里 bot 最近一次 @ 在 %.1f 秒前，本次冷却剩余 %.1f 秒（会话 %s）",
            age,
            self._at_cooldown_seconds() - age,
            sid[:12],
        )
