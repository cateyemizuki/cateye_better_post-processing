"""life 联动桥：跨插件「本轮后处理认领」表 + ``@API bpp_life_bridge``。

设计依据：``union/20261111_结构性延后项_设计方案_v2.md`` §6.2（B4 联动批）。

**为什么需要显式认领**：``life`` 插件在 ``maisaka.replyer.after_response`` 里模拟打字
时延。若 bpp 的后处理接管同样会按段模拟打字，两边都等一次就是「双重等待」。因此
life 在发送前通过宿主 ``ctx.api.call`` 显式认领本轮；bpp 只有在**后处理接管实际
生效**时才接受/拒绝，并把 ``typing_owner`` 如实告诉 life——life 只看
``typing_owner == "bpp"`` 就让出打字。

**fail-open 契约**（本模块最重要的性质）：

- 没有任何认领 → ``handle_before_post_process`` 原逻辑逐字不变；
- 认领过期（TTL 默认 10 秒、单轮有效）→ 按无认领处理；
- 认领载荷缺字段/模式未实现 → 拒绝（``accepted=False``），bpp 仍按原逻辑处理；
- 认领被接受也**不改变** bpp 的分段/错字/发送链，只影响生命侧是否让出打字。

``typing_owner`` 语义（v2 §6.2/§6.4）：只要 bpp 后处理接管生效（``active=True``），
``typing_owner`` 恒为 ``"bpp"``——即使拒绝了认领，bpp 仍会分段并模拟打字，life 也
必须让位；本插件未生效（``active=False``）时 ``typing_owner`` 为 ``"life"``。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional, Tuple

try:  # 真实 SDK
    from maibot_sdk import API
except Exception:  # pragma: no cover - 离线测试 stub 未提供 API 装饰器
    def API(*args: Any, **kwargs: Any):  # type: ignore[misc]
        def _decorator(func):
            return func

        return _decorator


#: 认领 TTL 的兜底默认（配置 ``[life_bridge].claim_ttl_seconds``）
DEFAULT_CLAIM_TTL_SECONDS = 10.0

#: 本批只实现 ``policy``；``full_text`` 预留（v2 §6.2 表「接管点」）。
SUPPORTED_CLAIM_MODES = ("policy",)
RESERVED_CLAIM_MODES = ("full_text",)

#: 认领表容量上限（防御异常调用方刷表；单轮正常只有 ≤1 条）。
MAX_CLAIMS = 64


def text_fingerprint(text: Any) -> str:
    """回复文本指纹（与 life 侧 :mod:`companion.post_process_bridge` 同算法）。"""
    return hashlib.sha1(str(text or "").encode("utf-8", "replace")).hexdigest()[:16]


def _clamp_ttl(value: Any) -> float:
    try:
        ttl = float(value)
    except (TypeError, ValueError):
        return DEFAULT_CLAIM_TTL_SECONDS
    if ttl != ttl or ttl in (float("inf"), float("-inf")):
        return DEFAULT_CLAIM_TTL_SECONDS
    return min(120.0, max(1.0, ttl))


class LifeBridgeMixin:
    """``bpp_life_bridge`` API + 进程内 TTL 认领表。"""

    def _init_life_bridge(self) -> None:
        #: claim_id -> {claim_id, stream_id, text_fingerprint, mode, typing_owner,
        #:              fragment_hint, expires_at(monotonic), created_at(monotonic)}
        self._life_claims: Dict[str, Dict[str, Any]] = {}
        #: 观测计数（诊断/测试；不进任何持久化）
        self._life_bridge_stats: Dict[str, int] = {
            "claim": 0, "accepted": 0, "rejected": 0, "consumed": 0,
            "expired": 0, "released": 0, "orphaned": 0,
        }
        self._life_bridge_seq = 0

    # ------------------------------------------------------------------
    # 配置 / 生效判定
    # ------------------------------------------------------------------

    def _life_bridge_cfg(self, key: str, default: Any) -> Any:
        section = getattr(getattr(self, "config", None), "life_bridge", None)
        value = getattr(section, key, default)
        return default if value is None else value

    def _life_bridge_enabled(self) -> bool:
        """``[life_bridge].enabled``；配置段缺失（裸 stub）→ False（fail-open）。"""
        section = getattr(getattr(self, "config", None), "life_bridge", None)
        if section is None:
            return False
        return bool(getattr(section, "enabled", True))

    def _life_bridge_active(self) -> bool:
        """bpp 的后处理接管本轮是否实际生效（path 与 Hook 完全同源）。"""
        try:
            return bool(self._post_process_takeover_active())
        except Exception:
            return False

    def _life_bridge_ttl(self) -> float:
        return _clamp_ttl(self._life_bridge_cfg("claim_ttl_seconds", DEFAULT_CLAIM_TTL_SECONDS))

    # ------------------------------------------------------------------
    # 认领表
    # ------------------------------------------------------------------

    def _life_bridge_purge(self, now: Optional[float] = None) -> int:
        """清除过期认领；返回清除条数。"""
        current = time.monotonic() if now is None else float(now)
        expired = [cid for cid, claim in self._life_claims.items()
                   if float(claim.get("expires_at") or 0.0) <= current]
        for cid in expired:
            self._life_claims.pop(cid, None)
        if expired:
            self._life_bridge_stats["expired"] += len(expired)
        return len(expired)

    def _life_bridge_find_claim(
        self,
        stream_id: str,
        fingerprint: str,
        *,
        consume: bool,
    ) -> Optional[Dict[str, Any]]:
        """按 (流, 文本指纹) 找未过期认领；``consume=True`` 时取走（单轮有效）。"""
        now = time.monotonic()
        self._life_bridge_purge(now)
        sid = str(stream_id or "").strip()
        fp = str(fingerprint or "").strip()
        if not sid or not fp:
            return None
        match: Optional[str] = None
        for cid, claim in self._life_claims.items():
            if (str(claim.get("stream_id") or "") == sid
                    and str(claim.get("text_fingerprint") or "") == fp):
                match = cid
                break
        if match is None:
            return None
        claim = self._life_claims[match]
        if consume:
            self._life_claims.pop(match, None)
            self._life_bridge_stats["consumed"] += 1
        return dict(claim)

    def _life_bridge_claim_for_round(self, kwargs: Dict[str, Any],
                                     *, consume: bool) -> Optional[Dict[str, Any]]:
        """从 Hook kwargs 里取本轮 (session_id, response) 对应的认领。"""
        if not self._life_bridge_enabled():
            return None
        stream_id = str(kwargs.get("session_id") or kwargs.get("stream_id") or "").strip()
        response = str(kwargs.get("response") or "").strip()
        if not stream_id or not response:
            return None
        return self._life_bridge_find_claim(
            stream_id, text_fingerprint(response), consume=consume)

    # ------------------------------------------------------------------
    # 留痕
    # ------------------------------------------------------------------

    def _life_bridge_note(self, event: str, **fields: Any) -> None:
        """记一行到 ``[intercept_log]``（未启用/异常时退回主日志 info）。"""
        try:
            logger = getattr(self, "_intercept_log", None)
            if logger is not None:
                logger.record(event, **fields)
                return
        except Exception:
            pass
        try:
            detail = " ".join(f"{key}={value}" for key, value in fields.items())
            self.ctx.logger.info("life 联动：%s %s", event, detail)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 跨插件 API
    # ------------------------------------------------------------------

    @API(
        "bpp_life_bridge",
        description=(
            "life 联动桥：claim/release/status。claim 只登记本轮认领，不改变后处理"
            "路径；无有效认领时 bpp 恒走原逻辑（fail-open）。返回 active/accepted/"
            "typing_owner/reason。"
        ),
        version="1",
        public=True,
    )
    async def handle_bpp_life_bridge(self, **kwargs: Any) -> Dict[str, Any]:
        """跨插件 API 入口（life 经 ``ctx.api.call`` 调用）。

        ``action``：

        - ``claim``：登记本轮认领；需要 ``stream_id`` + ``text_fingerprint`` + ``mode``
          （本批仅 ``policy``）。``active`` 为 bpp 后处理接管是否生效；只要生效，
          ``typing_owner`` 恒为 ``bpp``（无论接受与否，v2 §6.2）。
        - ``release``：按 ``claim_id``（或 ``stream_id``）释放；幂等。
        - ``status``：只读状态，**不会**创建认领。
        """
        action = str(kwargs.get("action") or "status").strip().lower()
        active = self._life_bridge_active()
        owner = "bpp" if active else "life"

        if action == "status":
            self._life_bridge_purge()
            return {
                "active": active,
                "accepted": False,
                "typing_owner": owner,
                "reason": "status",
                "enabled": self._life_bridge_enabled(),
                "claims": len(self._life_claims),
                "stats": dict(self._life_bridge_stats),
            }

        if action == "release":
            return self._life_bridge_release(kwargs, active=active, owner=owner)

        if action != "claim":
            return {
                "active": active,
                "accepted": False,
                "typing_owner": owner,
                "reason": "unknown_action",
            }

        # ---- claim ----
        self._life_bridge_stats["claim"] += 1
        if not active:
            self._life_bridge_stats["rejected"] += 1
            self._life_bridge_note(
                "life_bridge_rejected", reason="inactive",
                stream_id=str(kwargs.get("stream_id") or ""))
            return {
                "active": False,
                "accepted": False,
                "typing_owner": "life",
                "reason": "inactive",
            }
        if not self._life_bridge_enabled():
            self._life_bridge_stats["rejected"] += 1
            self._life_bridge_note(
                "life_bridge_rejected", reason="bridge_disabled",
                stream_id=str(kwargs.get("stream_id") or ""))
            return {
                "active": True,
                "accepted": False,
                "typing_owner": "bpp",
                "reason": "bridge_disabled",
            }

        mode = str(kwargs.get("mode") or "policy").strip().lower()
        stream_id = str(kwargs.get("stream_id") or "").strip()
        fingerprint = str(kwargs.get("text_fingerprint") or "").strip()
        if mode in RESERVED_CLAIM_MODES:
            reason = "full_text_not_implemented"
        elif mode not in SUPPORTED_CLAIM_MODES:
            reason = "unsupported_mode"
        elif not stream_id or not fingerprint:
            reason = "invalid_payload"
        elif not bool(self._life_bridge_cfg("accept_policy_claims", True)):
            reason = "policy_claims_disabled"
        elif (bool(self._life_bridge_cfg("require_yield_to_other_plugins", True))
              and not bool(getattr(getattr(self.config, "response_splitter", None),
                                   "yield_to_other_plugins", True))):
            # v2 §6.4：bpp 明确不让位其它插件时，拒绝认领；但它仍会分段并打字，
            # 所以 typing_owner 保持 "bpp"，life 照样让位。
            reason = "yield_to_other_plugins_disabled"
        else:
            reason = ""

        if reason:
            self._life_bridge_stats["rejected"] += 1
            self._life_bridge_note(
                "life_bridge_rejected", reason=reason, stream_id=stream_id,
                mode=mode)
            return {
                "active": True,
                "accepted": False,
                "typing_owner": "bpp",
                "reason": reason,
            }

        now = time.monotonic()
        self._life_bridge_purge(now)
        # 同一流只保留最新一条认领：旧消息的认领绝不跨轮生效。
        for cid in [cid for cid, claim in self._life_claims.items()
                    if str(claim.get("stream_id") or "") == stream_id]:
            self._life_claims.pop(cid, None)
        if len(self._life_claims) >= MAX_CLAIMS:
            oldest = min(self._life_claims,
                         key=lambda cid: float(self._life_claims[cid].get("created_at") or 0.0))
            self._life_claims.pop(oldest, None)
        self._life_bridge_seq += 1
        claim_id = str(kwargs.get("claim_id") or "").strip() or f"life-{self._life_bridge_seq}"
        claim = {
            "claim_id": claim_id,
            "stream_id": stream_id,
            "text_fingerprint": fingerprint,
            "mode": mode,
            "typing_owner": "bpp",
            "fragment_hint": kwargs.get("fragment_hint") or "",
            "created_at": now,
            "expires_at": now + self._life_bridge_ttl(),
        }
        self._life_claims[claim_id] = claim
        self._life_bridge_stats["accepted"] += 1
        self._life_bridge_note(
            "life_bridge_accepted", claim_id=claim_id, stream_id=stream_id,
            mode=mode, ttl_seconds=self._life_bridge_ttl())
        return {
            "active": True,
            "accepted": True,
            "typing_owner": "bpp",
            "reason": "accepted",
            "claim_id": claim_id,
            "expires_in": self._life_bridge_ttl(),
        }

    def _life_bridge_release(
        self, kwargs: Dict[str, Any], *, active: bool, owner: str,
    ) -> Dict[str, Any]:
        """释放认领（幂等）：``claim_id`` 优先，否则按 ``stream_id`` 批量释放。"""
        claim_id = str(kwargs.get("claim_id") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "").strip()
        removed = 0
        if claim_id:
            removed = 1 if self._life_claims.pop(claim_id, None) is not None else 0
        elif stream_id:
            for cid in [cid for cid, claim in self._life_claims.items()
                        if str(claim.get("stream_id") or "") == stream_id]:
                self._life_claims.pop(cid, None)
                removed += 1
        if removed:
            self._life_bridge_stats["released"] += removed
            self._life_bridge_note(
                "life_bridge_released", claim_id=claim_id, stream_id=stream_id,
                removed=removed)
        return {
            "active": active,
            "accepted": False,
            "typing_owner": owner,
            "reason": "released" if removed else "not_found",
            "removed": removed,
        }

    def _life_bridge_hook_claims(self) -> List[Dict[str, Any]]:
        """诊断：当前有效认领（status 的列表视图，供 /bpp 或测试）。"""
        self._life_bridge_purge()
        return [dict(claim) for claim in self._life_claims.values()]


__all__ = [
    "DEFAULT_CLAIM_TTL_SECONDS",
    "LifeBridgeMixin",
    "MAX_CLAIMS",
    "SUPPORTED_CLAIM_MODES",
    "text_fingerprint",
]
