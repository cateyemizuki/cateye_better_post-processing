"""跨聊私聊回忆（阶段 3 合并，迁自 ``cateye_mai_cross-chat_recall`` v0.3.1）。

让 bot 在必要情况下感知与指定群友的私聊记录：

- 扫描：按配置时机（planner 触发 / 消息推进 / 触发回复）对群内活跃用户与 bot 的私聊做一轮扫描；
- 总结：私聊满足条件（窗口内用户消息数达标）时交给 LLM 总结并写入插件自建 SQLite；
- 注入：把私聊概括（起止时间 + 对象 + 概括正文）注入群聊 planner / replyer 的
  上下文（首条 SystemMessageItem 之后），planner 与 reply 可分别开关；
- 工具：LLM 工具 recall_private_chat 仅在群聊会话中按昵称（多候选时可传 QQ 号）主动召回
  私聊概括（候选只取当前会话上下文），召回成功后按配置的粘性时长继续随 reply 注入
  （粘性召回的注册与注入均仅群聊会话）。

注意区分群聊与私聊：扫描与注入只发生在群聊会话；私聊内容只读不改。

## 合并适配（与旧插件的差异，逐条有据）

* **改为 mixin**（计划书 6.1）：状态、3 个 HookHandler、2 个 Command、工具与粘性逻辑全部
  由 ``CrossChatRecallMixin`` 提供；``_init_cross_chat_recall()`` 由主插件 ``__init__`` 调用。
  删除旧 ``CrossChatRecallPlugin`` 主类与 ``create_plugin()``；生命周期方法改名为
  ``_cross_chat_recall_start`` / ``_cross_chat_recall_stop`` / ``_cross_chat_recall_on_config_update``
  （原名 ``on_load`` / ``on_unload`` / ``on_config_update`` 会和主插件类体同名方法冲突）。
* **配置读取改为单节平铺**（计划书 6.2 的"嵌套风险预案"）：原 ``[scan]/[summarize]/[inject]/
  [store]/[scope]/[tool]`` 六节降级为 ``[cross_chat_recall]`` 内的扁平字段（键名沿用原字段名），
  本模块的 ``cfg.xxx`` 引用随之一一对应改写。
* **模块级开关**：原 ``plugin.enabled`` 总开关由主插件 ``[plugin].enabled`` 承担；新增
  ``[cross_chat_recall].enabled``（**默认关闭**）。两个开关任一关闭，本模块的 3 个 Hook、
  2 个命令与工具一律**零副作用直接放行/短路**（计划书 6.2/6.6），错误文案与任务 A 裁决
  保持一致（工具与命令均给"未执行"结果）。
* **支撑模块搬到主插件根目录**（计划书 6.1）：``helpers`` / ``injector`` / ``store`` /
  ``summarizer`` / ``scanner`` / ``name_resolver`` 现在是主插件一级模块，相对导入路径
  相应调整为 ``..helpers`` 等。
* **文件日志不初始化**（计划书 6.6）：``logging_setup.py`` 随迁但合并插件**不调用**
  ``setup_file_logging``；为免逐条 info/debug 调试日志刷进宿主日志，这里把 ``ccr`` logger
  钉在 WARNING（原插件靠 ``propagate=False`` 达到同样效果，warning/error 仍进宿主日志）。
* **数据迁移**：``on_load`` 时执行一次性 copy（旧 CCR 库、旧 guard 拦截日志，目标存在则跳过，
  任何异常仅告警不阻断），见 :func:`migrate_legacy_data_once`（计划书 6.5）。
* **运行时就绪**：store / scanner / summarizer / bot 名称改为**按需**创建（模块关闭时不建库、
  不读宿主配置），模块被热更新打开时在 ``_cross_chat_recall_on_config_update`` 里补齐。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from maibot_sdk import Command, HookHandler, Tool
from maibot_sdk.types import HookMode, HookOrder, ToolParameterInfo, ToolParamType

from ..helpers import allowed_by_list, extract_message_fields, msg_timestamp, unwrap_messages
from ..injector import build_injection_block, build_recall_content, inject_after_first_system
from ..logging_setup import get_logger
from ..name_resolver import collect_candidates, resolve_user_name
from ..scanner import Scanner
from ..store import SummaryStore
from ..summarizer import Summarizer

_PLANNER_HOOK = "maisaka.planner.before_request"
_REPLYER_HOOK = "maisaka.replyer.before_model_request"
_RECEIVE_HOOK = "chat.receive.after_process"

_STREAM_CACHE_TTL = 60.0
_FOLLOW_PLANNER_TTL = 300.0
_FORCE_INJECT_TTL = 600.0  # /ccr debug test 强制注入标记的有效期（秒）

#: 旧插件的数据目录名（``data/plugins/<id>/``，与本插件的数据目录同级）。
_LEGACY_CCR_PLUGIN_ID = "github.cateye.mai-cross-chat-recall"
_LEGACY_GUARD_PLUGIN_ID = "github.cateye.duplicate-reply-guard"

#: 旧 CCR 总结库文件名（迁移到本插件数据目录下同名文件）。
_LEGACY_DB_FILE_NAME = "cross_chat_recall.db"

#: 旧 guard 拦截日志的轮转备份后缀（主文件 + .1/.2/.3 一起迁移，计划书 6.5）。
_LEGACY_LOG_BACKUP_SUFFIXES = ("", ".1", ".2", ".3")

#: 旧拦截日志的默认文件名（配置里改过文件名时以配置为准，见调用方）。
_LEGACY_LOG_DEFAULT_NAME = "intercept.jsonl"


def migrate_legacy_data_once(
    data_dir: Any,
    *,
    intercept_log_file_name: str = _LEGACY_LOG_DEFAULT_NAME,
    logger: Optional[logging.Logger] = None,
) -> List[str]:
    """把两个旧插件数据目录里的持久化文件**一次性复制**进合并插件数据目录（计划书 6.5）。

    规则（copy 不删、幂等、可静态走查）：

    * 源目录 = 本插件数据目录的**同级**目录 ``data/plugins/<旧插件ID>/``（宿主
      ``plugin_paths.build_plugin_paths``：``data_dir = <root>/data/plugins/<plugin_id>``）；
    * 目标已存在 → 跳过（不覆盖、不合并）；
    * 源不存在 → 跳过（用户没装过旧插件 / 已清理，属正常情况）；
    * 任何异常只记 warning，**绝不阻断插件加载**。

    Args:
        data_dir: 本插件的数据目录（``ctx.paths.data_dir``）。
        intercept_log_file_name: 当前配置的拦截日志文件名（旧 guard 的日志同名文件 + 轮转备份）。
        logger: 迁移日志用的 logger（可为 None，仅静默执行）。

    Returns:
        List[str]: 实际复制出来的目标文件路径（字符串），供调用方打 INFO 日志/测试断言。
    """

    copied: List[str] = []

    def _warn(message: str, *args: Any) -> None:
        if logger is not None:
            logger.warning(message, *args)

    try:
        base = Path(str(data_dir))
    except Exception as exc:  # noqa: BLE001 - 迁移失败不阻断加载
        _warn("数据迁移：data_dir 不可用（%s），本次跳过", exc)
        return copied

    # 1) 旧 CCR 总结库 → <data_dir>/cross_chat_recall.db
    # 2) 旧 guard 拦截日志 → <data_dir>/<日志文件名>（含 .1/.2/.3 轮转备份）
    log_name = Path(str(intercept_log_file_name or "")).name or _LEGACY_LOG_DEFAULT_NAME
    jobs: List[Tuple[str, str, str]] = [
        (str(Path(_LEGACY_CCR_PLUGIN_ID) / _LEGACY_DB_FILE_NAME), _LEGACY_DB_FILE_NAME, "跨聊私聊回忆总结库"),
    ]
    for suffix in _LEGACY_LOG_BACKUP_SUFFIXES:
        jobs.append(
            (str(Path(_LEGACY_GUARD_PLUGIN_ID) / f"{log_name}{suffix}"), f"{log_name}{suffix}", "自回复/重复回复拦截日志")
        )

    for source_rel, target_name, label in jobs:
        try:
            target = base / target_name
            if target.exists():
                continue  # 目标已存在：跳过（不覆盖用户现有数据）
            source = base.parent / source_rel
            if not source.is_file():
                continue  # 旧文件不存在：跳过
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(str(target))
        except Exception as exc:  # noqa: BLE001 - 迁移失败不阻断加载
            _warn("数据迁移：复制 %s（%s）失败，已跳过: %s", source_rel, label, exc)
    return copied


class CrossChatRecallMixin:
    """跨聊私聊回忆的状态、调度、Hook、命令与工具。"""

    # ------------------------------------------------------------------
    # 初始化 / 生命周期（由主插件调用）
    # ------------------------------------------------------------------

    def _init_cross_chat_recall(self) -> None:
        """初始化状态（由插件类 ``__init__`` 调用）。"""
        # 合并插件**不初始化** CCR 的文件日志（计划书 6.6）：只取 "ccr.*" 子 logger。
        # 原插件靠 ``setup_file_logging`` 把日志写进插件目录 logs/ 并 ``propagate=False``；
        # 合并后不写文件，这里把 "ccr" 根 logger 钉在 WARNING，避免逐条 info/debug
        # 调试日志刷进宿主日志（warning/error 仍会随宿主日志配置收集）。
        self._file_log = get_logger("plugin")
        try:
            logging.getLogger("ccr").setLevel(logging.WARNING)
        except Exception:  # noqa: BLE001 - 日志级别设置失败不影响功能
            pass
        self._store: Optional[SummaryStore] = None
        self._scanner: Optional[Scanner] = None
        self._summarizer: Optional[Summarizer] = None
        self._bg_tasks: set = set()
        self._scan_lock: Optional[asyncio.Lock] = None
        self._stream_cache: Dict[str, Mapping[str, Any]] = {}
        self._stream_cache_ts: float = 0.0
        self._planner_users_cache: Dict[str, Tuple[float, List[str]]] = {}
        self._push_counters: Dict[str, int] = {}
        # /ccr debug test 强制注入标记：session_id -> {"planner": (ts, uid), "reply": (ts, uid)}
        self._force_inject: Dict[str, Dict[str, Tuple[float, str]]] = {}
        # 工具主动召回的粘性标记：stream_id -> {user_id: 过期时间戳}（内存态，不持久化）
        self._tool_recall_sticky: Dict[str, Dict[str, float]] = {}
        # 扫描被跳过（锁忙）的会话，等当前扫描结束后补触发
        self._pending_scan: set = set()
        self._bot_name: str = ""
        self._bot_name_loaded: bool = False
        # 一次性数据迁移是否已跑过（on_load 调用；配置热更新不重复跑）。
        self._legacy_data_migrated: bool = False

    def _cross_chat_recall_active(self) -> bool:
        """模块是否生效：插件总开关 + ``[cross_chat_recall].enabled``（默认关闭）。"""
        if not self._plugin_enabled():
            return False
        return bool(self.config.cross_chat_recall.enabled)

    async def _ensure_cross_chat_recall_runtime(self) -> None:
        """按需创建运行期对象（store / scanner / summarizer / 扫描锁 / bot 名称）。"""
        if self._scan_lock is None:
            self._scan_lock = asyncio.Lock()
        if self._store is None:
            self._store = SummaryStore(self.ctx.paths.data_dir / _LEGACY_DB_FILE_NAME)
        if self._scanner is None:
            self._scanner = Scanner(self)
        if self._summarizer is None:
            self._summarizer = Summarizer(self)
        if not self._bot_name_loaded:
            await self._load_bot_name()
            self._bot_name_loaded = True

    async def _cross_chat_recall_start(self) -> None:
        """主插件 ``on_load`` 调用：一次性数据迁移 + （模块开启时）运行时就绪。"""
        if not self._legacy_data_migrated:
            self._legacy_data_migrated = True
            try:
                copied = migrate_legacy_data_once(
                    self.ctx.paths.data_dir,
                    intercept_log_file_name=str(self.config.intercept_log.file_name or ""),
                    logger=self._file_log,
                )
                if copied:
                    self.ctx.logger.info("旧插件数据已迁移（copy，不删源）：%s", "、".join(copied))
                else:
                    self._file_log.info("旧插件数据迁移：无可迁移文件（目标已存在或旧目录不存在）")
            except Exception as exc:  # noqa: BLE001 - 迁移异常仅告警，不阻断加载
                self.ctx.logger.warning("旧插件数据迁移失败，已跳过（不影响插件加载）: %s", exc)
        if not self._cross_chat_recall_active():
            return
        await self._ensure_cross_chat_recall_runtime()
        cfg = self.config.cross_chat_recall
        self.ctx.logger.info(
            "跨聊私聊回忆已加载。trigger_mode=%s scan_count=%s inject(planner=%s, reply=%s, "
            "planner_mode=%s, recent_users=%s, reply_mode=%s) min_user_msgs=%s window=%sh "
            "resummarize_push=%s model=%s ttl=%sh tool(recall_sticky=%smin, ctx_msgs=%s)",
            cfg.trigger_mode, cfg.scan_count,
            cfg.inject_planner, cfg.inject_reply,
            cfg.planner_user_mode, cfg.recent_user_count, cfg.reply_user_mode,
            cfg.min_user_messages, cfg.window_hours,
            cfg.resummarize_push_count, cfg.model, cfg.summary_ttl_hours,
            cfg.recall_retention_minutes, cfg.context_window_messages,
        )

    async def _cross_chat_recall_stop(self) -> None:
        """主插件 ``on_unload`` 调用：取消后台扫描任务并关闭总结库。"""
        tasks = [t for t in self._bg_tasks if not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            # 等被取消的任务收尾，再关库，避免残余写库撞已关闭连接
            await asyncio.gather(*tasks, return_exceptions=True)
        self._bg_tasks.clear()
        if self._store is not None:
            self._store.close()
            self._store = None
        self._file_log.info("跨聊私聊回忆已卸载，后台任务已取消")

    async def _cross_chat_recall_on_config_update(self, version: str = "") -> None:
        """主插件 ``on_config_update(scope="self")`` 调用：记录新配置；模块被打开时补齐运行时。"""
        self._file_log.info("配置热更新 version=%s", version)
        if not self._cross_chat_recall_active():
            return
        await self._ensure_cross_chat_recall_runtime()
        cfg = self.config.cross_chat_recall
        self._file_log.info(
            "新配置 trigger_mode=%s inject(planner=%s, reply=%s) scan_count=%s",
            cfg.trigger_mode, cfg.inject_planner, cfg.inject_reply, cfg.scan_count,
        )

    # -- 对内属性（按需创建，模块关闭时不建库 / 不读宿主配置） --------------

    @property
    def store(self) -> SummaryStore:
        if self._store is None:
            self._store = SummaryStore(self.ctx.paths.data_dir / _LEGACY_DB_FILE_NAME)
        return self._store

    @property
    def scanner(self) -> Scanner:
        if self._scanner is None:
            self._scanner = Scanner(self)
        return self._scanner

    @property
    def summarizer(self) -> Summarizer:
        if self._summarizer is None:
            self._summarizer = Summarizer(self)
        return self._summarizer

    @property
    def bot_name(self) -> str:
        return self._bot_name or "bot"

    async def _load_bot_name(self) -> None:
        for key in ("bot.nickname", "person.bot_name", "person.nickname"):
            try:
                value = await self.ctx.config.get(key)
            except Exception:
                value = None
            if value:
                self._bot_name = str(value).strip()
                self._file_log.info("读取 bot 名称 %s=%r", key, self._bot_name)
                return
        self._bot_name = "bot"
        self._file_log.warning("未能从宿主配置读取 bot 名称，使用兜底值 %r", self._bot_name)

    # -- 流信息缓存 ---------------------------------------------------------

    async def get_stream(self, session_id: str) -> Optional[Mapping[str, Any]]:
        """按 session_id（=stream_id）查聊天流，带 60s 缓存。"""
        if not session_id:
            return None
        now = time.time()
        if self._stream_cache and now - self._stream_cache_ts < _STREAM_CACHE_TTL:
            return self._stream_cache.get(session_id)
        try:
            streams = await self.ctx.chat.get_all_streams()
        except Exception as exc:
            self._file_log.warning("get_all_streams 异常: %s", exc)
            return self._stream_cache.get(session_id)
        mapping: Dict[str, Mapping[str, Any]] = {}
        for stream in streams or []:
            if not isinstance(stream, Mapping):
                continue
            sid = str(stream.get("stream_id") or stream.get("session_id") or "")
            if sid:
                mapping[sid] = stream
        self._stream_cache = mapping
        self._stream_cache_ts = now
        self._file_log.debug("聊天流缓存已刷新：%d 条", len(mapping))
        return mapping.get(session_id)

    async def _is_group_session(self, session_id: str) -> Tuple[bool, Optional[Mapping[str, Any]]]:
        stream = await self.get_stream(session_id)
        if not stream:
            return False, None
        is_group = bool(stream.get("is_group_session")) or str(stream.get("chat_type") or "") == "group"
        return is_group, stream

    def _group_allowed(self, group_id: str) -> bool:
        """群聊作用范围：共用名单 + 模式（黑名单=名单内不生效；白名单=仅名单内生效）。"""
        cfg = self.config.cross_chat_recall
        return allowed_by_list(group_id, cfg.group_list, cfg.group_list_mode)

    def _user_allowed(self, user_id: str) -> bool:
        """用户作用范围：共用名单 + 模式（语义同群聊名单）。"""
        cfg = self.config.cross_chat_recall
        return allowed_by_list(user_id, cfg.user_list, cfg.user_list_mode)

    # -- 近期群消息 ---------------------------------------------------------

    async def _recent_group_messages(self, session_id: str, want: int) -> List[Mapping[str, Any]]:
        """取群聊最近 want 条非 bot、非合成的消息（新到旧）。"""
        try:
            raw = await self.ctx.message.get_recent(chat_id=session_id, limit=max(want * 3, 30))
        except Exception as exc:
            self._file_log.warning("群 %s：get_recent 异常: %s", session_id, exc)
            return []
        messages = unwrap_messages(raw, self._file_log, "get_recent")
        fresh: List[Mapping[str, Any]] = []
        for msg in messages:
            fields = extract_message_fields(msg)
            if fields.get("is_self") or fields.get("is_synthetic") or not fields.get("user_id"):
                continue
            fresh.append(msg)
        fresh.sort(key=msg_timestamp, reverse=True)
        if len(fresh) > want:
            fresh = fresh[:want]
        return fresh

    # -- 注入用户选择 -------------------------------------------------------

    async def _select_planner_users(self, session_id: str) -> List[str]:
        """按 planner_user_mode 选注入用户（已过用户名单）。"""
        cfg = self.config.cross_chat_recall
        mode = cfg.planner_user_mode
        if mode == "recent_users":
            want = int(cfg.recent_user_count)
        else:
            want = 1
        recent = await self._recent_group_messages(session_id, want)
        if not recent:
            self._file_log.info("planner 注入：群 %s 最近无非 bot 消息，无法选用户", session_id)
            return []
        users: List[str] = []
        for msg in recent:
            uid = extract_message_fields(msg).get("user_id") or ""
            if uid and uid not in users:
                users.append(uid)
            if len(users) >= want:
                break
        trigger_user = users[0] if users else ""
        users = [u for u in users if self._user_allowed(u)]
        self._file_log.info(
            "planner 注入：群 %s mode=%s 触发用户=%s 选中=%s", session_id, mode, trigger_user, users,
        )
        return users

    async def _lookup_reply_user(self, session_id: str, reply_message_id: Any) -> Optional[str]:
        """通过被回复的消息 id 反查用户 id。"""
        mid = str(reply_message_id or "").strip()
        if not mid:
            return None
        try:
            msg = await self.ctx.message.get_by_id(mid, chat_id=session_id, stream_id=session_id)
        except Exception as exc:
            self._file_log.warning("reply 注入：反查消息 %s 异常: %s", mid, exc)
            return None
        if not isinstance(msg, Mapping):
            self._file_log.info("reply 注入：被回复消息 %s 不存在或形态异常", mid)
            return None
        uid = extract_message_fields(msg).get("user_id") or ""
        if not uid:
            return None
        self._file_log.info("reply 注入：被回复消息 %s → 用户 %s", mid, uid)
        return uid

    # -- 注入执行 -----------------------------------------------------------

    def _collect_valid_summaries(self, users: List[str]) -> List[Dict[str, Any]]:
        """取这些用户当前有效（未过期、非空）的总结，按最近私聊时间新→旧排序。"""
        ttl = float(self.config.cross_chat_recall.summary_ttl_hours) * 3600
        cutoff = time.time() - ttl
        rows = self.store.get_many(users)
        entries: List[Dict[str, Any]] = []
        for uid in users:
            row = rows.get(uid)
            if not row:
                continue
            if float(row.get("end_ts") or 0) < cutoff:
                self._file_log.debug("注入：用户 %s 的总结已过 TTL，跳过", uid)
                continue
            summary = str(row.get("summary") or "").strip()
            if not summary:
                continue
            entries.append({
                "user_id": uid,
                "nickname": row.get("user_nickname") or uid,
                "start_ts": row.get("start_ts") or 0,
                "end_ts": row.get("end_ts") or 0,
                "summary": summary,
            })
        entries.sort(key=lambda e: float(e.get("end_ts") or 0), reverse=True)
        return entries

    def _apply_injection(self, kwargs: Dict[str, Any], users: List[str], scene: str) -> Dict[str, Any]:
        """把 users 的总结合并为一个块插入 items；返回待返回的 hook 结果（无内容则仅 continue）。"""
        entries = self._collect_valid_summaries(users)
        if not entries:
            self._file_log.info("%s 注入：选中的用户 %s 均无有效总结，跳过", scene, users)
            return {"action": "continue"}
        block = build_injection_block(entries)
        items = inject_after_first_system(kwargs.get("items"), block, self._file_log)
        if items is None:
            return {"action": "continue"}
        modified = dict(kwargs)
        modified["items"] = items
        self._file_log.info(
            "%s 注入：已插入私聊记忆块（%d 个用户：%s，块 %d 字符）",
            scene, len(entries), [e["user_id"] for e in entries], len(block),
        )
        self._file_log.debug("%s 注入块全文：\n%s", scene, block)
        return {"action": "continue", "modified_kwargs": modified}

    # -- 工具召回粘性保留 ---------------------------------------------------

    def _register_sticky_recall(self, stream_id: str, user_id: str) -> None:
        """注册/刷新粘性召回：该用户的私聊概括在保留期内继续随 reply 注入（内存态，不落盘）。

        仅在群聊会话的工具召回成功路径上调用（裁决 A3）；非群聊会话不注册粘性。
        """
        now = time.time()
        minutes = float(self.config.cross_chat_recall.recall_retention_minutes)
        entry = self._tool_recall_sticky.setdefault(stream_id, {})
        entry[user_id] = now + minutes * 60
        self._prune_sticky_recall(stream_id, now)
        self._file_log.info(
            "工具召回：注册粘性召回 stream=%s user=%s 保留 %.1f 分钟", stream_id, user_id, minutes,
        )

    def _prune_sticky_recall(self, stream_id: str, now: Optional[float] = None) -> None:
        """惰性清理某聊流的过期粘性条目（全部过期时移除整条记录）。"""
        entry = self._tool_recall_sticky.get(stream_id)
        if not entry:
            return
        current = now if now is not None else time.time()
        for uid in [uid for uid, expire_at in entry.items() if expire_at <= current]:
            entry.pop(uid, None)
        if not entry:
            self._tool_recall_sticky.pop(stream_id, None)

    def _sticky_users(self, stream_id: str) -> List[str]:
        """取该聊流当前未过期的粘性召回用户（顺序 = 注册先后）。"""
        self._prune_sticky_recall(stream_id)
        return list((self._tool_recall_sticky.get(stream_id) or {}).keys())

    # -- 强制注入（/ccr debug test）-----------------------------------------

    def _set_force_inject(self, session_id: str, user_id: str) -> None:
        """为当前聊流的下一次 planner 与 reply 请求设置强制注入标记。"""
        now = time.time()
        entry = self._force_inject.setdefault(session_id, {})
        entry["planner"] = (now, user_id)
        entry["reply"] = (now, user_id)
        self._file_log.info("强制注入标记已设置 session=%s user=%s", session_id, user_id)

    def _peek_force_inject(self, session_id: str, scene: str) -> Optional[str]:
        """读取强制注入标记（不消费）；过期自动清除。返回 None 表示无标记。"""
        entry = self._force_inject.get(session_id)
        if not entry:
            return None
        item = entry.get(scene)
        if not item:
            return None
        ts, uid = item
        if time.time() - ts > _FORCE_INJECT_TTL:
            self._file_log.info("强制注入标记已过期 session=%s scene=%s", session_id, scene)
            entry.pop(scene, None)
            if not entry:
                self._force_inject.pop(session_id, None)
            return None
        return uid

    def _consume_force_inject(self, session_id: str, scene: str) -> None:
        """注入成功后消费标记。"""
        entry = self._force_inject.get(session_id)
        if entry:
            entry.pop(scene, None)
            if not entry:
                self._force_inject.pop(session_id, None)
        self._file_log.info("强制注入已生效并消费标记 session=%s scene=%s", session_id, scene)

    # -- 扫描调度 -----------------------------------------------------------

    def _schedule_scan(self, session_id: str, *, focus_user_ids: Optional[List[str]] = None, reason: str = "") -> None:
        task = asyncio.create_task(self._scan_runner(session_id, focus_user_ids, reason))
        self._bg_tasks.add(task)
        task.add_done_callback(self._on_bg_done)

    def _on_bg_done(self, task: "asyncio.Task[Any]") -> None:
        self._bg_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self._file_log.error("后台扫描任务异常: %s", task.exception())

    async def _scan_runner(self, session_id: str, focus_user_ids: Optional[List[str]], reason: str) -> None:
        if self._scan_lock is None:
            self._scan_lock = asyncio.Lock()
        if self._scan_lock.locked():
            # 已有扫描在进行：登记补触发，避免该批消息漏扫（计数已清零）
            self._pending_scan.add(session_id)
            self._file_log.info(
                "[扫描] 已有扫描在进行，登记补触发 stream=%s reason=%r", session_id, reason,
            )
            return
        try:
            async with self._scan_lock:
                await self.scanner.scan_stream(session_id, focus_user_ids=focus_user_ids, reason=reason)
        finally:
            if session_id in self._pending_scan:
                self._pending_scan.discard(session_id)
                self._file_log.info("[扫描] 锁已释放，补触发 stream=%s", session_id)
                self._schedule_scan(session_id, focus_user_ids=focus_user_ids, reason="补触发")

    # -- Hook：planner ------------------------------------------------------

    @HookHandler(_PLANNER_HOOK, name="ccr_planner_inject", mode=HookMode.BLOCKING, order=HookOrder.NORMAL)
    async def on_planner_before_request(self, **kwargs: Any) -> Dict[str, Any]:
        if not self._cross_chat_recall_active():
            return {"action": "continue"}
        cfg = self.config.cross_chat_recall
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return {"action": "continue"}

        # /ccr debug test 强制注入：绕过群聊/范围/开关检查（调试专用）
        force_user = self._peek_force_inject(session_id, "planner")

        is_group, stream = await self._is_group_session(session_id)
        if force_user is None:
            if not is_group or not stream:
                self._file_log.debug("planner：会话 %s 非群聊，跳过（区分群聊/私聊）", session_id)
                return {"action": "continue"}
            group_id = str(stream.get("group_id") or "")
            if not self._group_allowed(group_id):
                self._file_log.debug("planner：群 %s 不在作用范围，跳过", group_id)
                return {"action": "continue"}

        # 扫描模式 = planner 触发时：本轮顺带触发扫描（后台执行，不阻塞 planner）
        if cfg.trigger_mode == "planner":
            self._file_log.info("planner 触发扫描：群 %s（session=%s）",
                                (stream or {}).get("group_id") or "?", session_id)
            self._schedule_scan(session_id, reason="planner 触发")

        # 强制注入优先（标记在注入成功后才消费；总结未就绪时保留并走常规路径）
        if force_user is not None:
            result = self._apply_injection(dict(kwargs), [force_user], "planner(强制)")
            if "modified_kwargs" in result:
                self._consume_force_inject(session_id, "planner")
                return result
            self._file_log.info("planner：强制注入暂无用户 %s 的有效总结，保留标记", force_user)

        if not cfg.inject_planner:
            return {"action": "continue"}

        # 注入用户选择（结果同时缓存给 follow_planner 模式的 reply 使用）
        users = await self._select_planner_users(session_id)
        self._planner_users_cache[session_id] = (time.time(), list(users))
        if not users:
            return {"action": "continue"}
        return self._apply_injection(dict(kwargs), users, "planner")

    # -- Hook：replyer ------------------------------------------------------

    @HookHandler(_REPLYER_HOOK, name="ccr_replyer_inject", mode=HookMode.BLOCKING, order=HookOrder.NORMAL)
    async def on_replyer_before_model_request(self, **kwargs: Any) -> Dict[str, Any]:
        if not self._cross_chat_recall_active():
            return {"action": "continue"}
        cfg = self.config.cross_chat_recall
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return {"action": "continue"}

        # /ccr debug test 强制注入：绕过群聊/范围/开关检查（调试专用）
        force_user = self._peek_force_inject(session_id, "reply")

        is_group, stream = await self._is_group_session(session_id)
        group_id = str((stream or {}).get("group_id") or "")

        # 工具主动召回注册的粘性用户（未过期；惰性清理）。
        # 裁决 A3（取舍 ④）：粘性召回的注册与注入均仅群聊会话——非群聊不取粘性、
        # 也不注入粘性块（维持"扫描与注入只发生在群聊"不变量）；
        # 群聊内注入阶段不再过群/用户名单（粘性用户保持既有不变量的完全绕过）
        sticky_users = self._sticky_users(session_id) if is_group else []

        scope_ok = bool(is_group and stream and self._group_allowed(group_id))
        if force_user is None and not scope_ok:
            # 常规路径因"非群聊/不在作用范围"被跳过：仅当会话是群聊（被范围排除）时，
            # 粘性用户在工具调用时已过候选校验，只要未过期且 inject_reply 开启，
            # 仍按粘性块注入；非群聊会话一律不注入粘性
            if is_group and sticky_users and cfg.inject_reply:
                self._file_log.info(
                    "reply 注入：常规路径跳过（群 %s 不在作用范围），仅注入粘性召回用户 %s",
                    group_id or "?", sticky_users,
                )
                return self._apply_injection(dict(kwargs), list(sticky_users), "replyer(粘性)")
            self._file_log.debug("replyer：会话 %s 非群聊/不在作用范围，跳过（区分群聊/私聊）", session_id)
            return {"action": "continue"}

        # 反查被回复的目标用户（reply 扫描与 reply_source 注入共用一次 RPC，省 6000ms 超时预算）
        reply_user: Optional[str] = None
        if cfg.trigger_mode == "reply" or cfg.reply_user_mode == "reply_source":
            reply_user = await self._lookup_reply_user(session_id, kwargs.get("reply_message_id"))

        # 扫描模式 = 触发回复时：只扫描被回复的目标用户（reply_message_id 反查）
        if cfg.trigger_mode == "reply":
            if reply_user:
                self._file_log.info("reply 触发定向扫描：群 %s 用户 %s",
                                    (stream or {}).get("group_id") or "?", reply_user)
                self._schedule_scan(session_id, focus_user_ids=[reply_user], reason="reply 触发")
            else:
                self._file_log.info("reply 触发扫描：本次回复无可反查的目标消息，跳过扫描")

        # 强制注入优先（标记在注入成功后才消费；总结未就绪时保留并走常规路径）
        if force_user is not None:
            result = self._apply_injection(dict(kwargs), [force_user], "replyer(强制)")
            if "modified_kwargs" in result:
                self._consume_force_inject(session_id, "reply")
                return result
            self._file_log.info("replyer：强制注入暂无用户 %s 的有效总结，保留标记", force_user)

        if not cfg.inject_reply:
            return {"action": "continue"}

        # 注入用户选择
        users: List[str] = []
        if cfg.reply_user_mode == "follow_planner":
            cached = self._planner_users_cache.get(session_id)
            if cached and time.time() - cached[0] < _FOLLOW_PLANNER_TTL:
                users = list(cached[1])
                self._file_log.info("reply 注入：跟随 planner 用户 %s（session=%s）", users, session_id)
            else:
                self._file_log.info("reply 注入：follow_planner 无有效缓存（session=%s），本次不注入", session_id)
        else:
            if reply_user and self._user_allowed(reply_user):
                users = [reply_user]
            elif reply_user:
                self._file_log.info("reply 注入：用户 %s 不在作用范围", reply_user)

        # 常规选中用户与粘性召回用户取并集（去重），统一注入；粘性用户不再过用户名单
        if sticky_users:
            merged = list(dict.fromkeys([*users, *sticky_users]))
            self._file_log.info(
                "reply 注入：常规用户 %s + 粘性用户 %s → 合并 %s（session=%s）",
                users, sticky_users, merged, session_id,
            )
            users = merged
        if not users:
            return {"action": "continue"}
        return self._apply_injection(dict(kwargs), users, "replyer")

    # -- 组件：LLM 工具（主动召回）------------------------------------------

    @Tool(
        "recall_private_chat",
        brief_description="召回你与某位群友的私聊记忆",
        detailed_description=(
            "当用户让你回忆与某人的私聊内容、或你需要主动想起与某人私下聊过什么时调用。\n"
            "仅在用户明确要求回忆/查找私聊内容时调用，不要主动调用，也不要在普通闲聊里调用。\n"
            "限制：仅在群聊会话中可用；只能查询当前会话上下文里出现过的用户；"
            "传入的名称应取自上下文中的用户昵称，不要凭空编造；"
            "若返回匹配到多个用户，按名单传其中一个昵称或 QQ 号重新查询。\n"
            "参数：user_name（string，必填）——要召回私聊记忆的用户昵称（匹配到多个用户时可传 QQ 号），"
            "取当前会话上下文中的名称。\n"
            "返回：success 表示是否找到私聊记忆，content 为召回内容或失败原因"
            "（如名称有误、匹配到多个用户、没有私聊内容等）；失败时 content 会给出原因，可直接转述给用户。"
        ),
        parameters=[
            ToolParameterInfo(
                name="user_name",
                param_type=ToolParamType.STRING,
                description="要召回私聊记忆的用户昵称，取当前会话上下文中的名称（匹配到多个用户时可传 QQ 号）",
                required=True,
            ),
        ],
    )
    async def tool_recall_private_chat(self, user_name: Any = "", **kwargs: Any) -> Dict[str, Any]:
        # 裁决 A1（异议 1 / 取舍 ③）：插件总开关（或本模块开关）关闭时直接短路，不响应工具调用
        if not self._cross_chat_recall_active():
            self._file_log.info("工具 recall_private_chat 未执行：私聊回忆未启用")
            return {"success": False, "content": "私聊回忆功能未启用。"}

        stream_id = str(kwargs.get("stream_id") or "").strip()
        wanted = str(user_name or "").strip()

        # 裁决 A3（取舍 ④）：工具仅在群聊会话可用（维持"扫描与注入只发生在群聊"不变量；
        # 会话类型未知或非群聊一律拒绝，粘性召回也不会在非群聊注册）
        is_group, _tool_stream = await self._is_group_session(stream_id)
        if not is_group:
            self._file_log.info(
                "工具 recall_private_chat(%r)：会话 %s 非群聊或类型未知，拒绝（仅群聊可用）",
                wanted, stream_id or "-",
            )
            return {"success": False, "content": "私聊回忆工具仅在群聊中可用。"}

        # 1-3. 候选只取当前聊流上下文 → 精准匹配 → 正则降级（五分支见 name_resolver）
        candidates = await self._context_candidates(stream_id) if stream_id else []
        resolved = resolve_user_name(candidates, wanted)
        if resolved.get("status") == "ambiguous":
            self._file_log.info("工具 recall_private_chat(%r)：匹配到多个用户 %s", wanted, resolved.get("names"))
            return {"success": False, "content": str(resolved.get("message") or "")}
        if resolved.get("status") != "hit":
            self._file_log.info(
                "工具 recall_private_chat(%r)：名称有误（stream=%s 候选 %d 个）",
                wanted, stream_id or "-", len(candidates),
            )
            return {"success": False, "content": str(resolved.get("message") or "")}

        # 4. 命中唯一用户：查总结，无记录/为空/已过 TTL → 视为没有私聊，不注册粘性
        user_id = str(resolved.get("user_id") or "")
        matched_nickname = str(resolved.get("nickname") or "")
        row = self.store.get(user_id)
        summary = str((row or {}).get("summary") or "").strip()
        ttl_seconds = float(self.config.cross_chat_recall.summary_ttl_hours) * 3600
        if not row or not summary or float(row.get("end_ts") or 0) < time.time() - ttl_seconds:
            self._file_log.info(
                "工具 recall_private_chat(%r)：用户 %s 无有效总结（无记录/为空/已过 TTL）", wanted, user_id,
            )
            return {"success": True, "content": "你和该用户没有私聊内容。"}

        # 5. 有有效总结：注册/刷新粘性召回，返回与注入块同风格的召回内容
        self._register_sticky_recall(stream_id, user_id)
        content = build_recall_content({
            "user_id": user_id,
            "nickname": str(row.get("user_nickname") or "").strip() or matched_nickname,
            "start_ts": row.get("start_ts") or 0,
            "end_ts": row.get("end_ts") or 0,
            "summary": summary,
        })
        self._file_log.info(
            "工具 recall_private_chat(%r)：用户 %s 召回成功（粘性保留 %.1f 分钟）",
            wanted, user_id, float(self.config.cross_chat_recall.recall_retention_minutes),
        )
        return {"success": True, "content": content}

    async def _context_candidates(self, stream_id: str) -> List[Tuple[str, str]]:
        """取当前聊流最近 context_window_messages 条消息中出现过的用户昵称候选（新→旧）。"""
        limit = int(self.config.cross_chat_recall.context_window_messages)
        try:
            raw = await self.ctx.message.get_recent(chat_id=stream_id, limit=limit)
        except Exception as exc:
            self._file_log.warning("工具召回：stream=%s get_recent 异常: %s", stream_id, exc)
            return []
        messages = unwrap_messages(raw, self._file_log, "get_recent(tool)")
        candidates = collect_candidates(messages, limit)
        self._file_log.info(
            "工具召回：stream=%s 取到 %d 条消息 → 候选用户 %s",
            stream_id, len(messages), [f"{nick}({uid})" for uid, nick in candidates],
        )
        return candidates

    # -- 命令 ---------------------------------------------------------------

    @Command("ccr_debug_test", description="调试：在下一次 planner/reply 强制注入自己的私聊回忆（仅操作员）",
             pattern=r"(?<!\S)/?ccr\s+debug\s+test\s*$", permission="operator")
    async def cmd_ccr_debug_test(self, **kwargs: Any) -> tuple[bool, str, bool]:
        # 裁决 A1（异议 1）：总开关关闭时直接短路，给"未执行"结果，不设置任何标记
        if not self._cross_chat_recall_active():
            self._file_log.info("命令 /ccr debug test 未执行：私聊回忆未启用")
            return False, "插件未启用，命令未执行。", True
        stream_id = str(kwargs.get("stream_id") or "")
        user_id = str(kwargs.get("user_id") or "").strip()
        if not stream_id or not user_id:
            return False, "无法识别当前聊流或命令发起者", True

        # 私聊流里 stream_id 对应的是私聊会话，注入逻辑同样适用（调试用）
        nickname = ""
        stream = await self.get_stream(stream_id)
        if stream:
            nickname = str(stream.get("user_nickname") or "").strip()

        self._set_force_inject(stream_id, user_id)

        row = self.store.get(user_id)
        if row and str(row.get("summary") or "").strip():
            text = (
                f"[ccr debug] 已标记：当前聊流的下一次 planner 与 reply 请求将强制注入"
                f"你（{nickname or user_id}）的私聊回忆。有效期 {int(_FORCE_INJECT_TTL // 60)} 分钟。"
            )
        else:
            # 暂无总结：立即定向扫描一次，标记保留至注入成功
            self._file_log.info("强制注入：用户 %s 暂无总结，触发定向扫描 stream=%s", user_id, stream_id)
            self._schedule_scan(stream_id, focus_user_ids=[user_id], reason="强制注入前定向扫描")
            text = (
                f"[ccr debug] 你（{nickname or user_id}）当前没有可用的私聊总结，"
                f"已触发一次定向扫描；总结完成后，当前聊流的下一次 planner 与 reply 请求将强制注入。"
            )
        await self.ctx.send.text(text, stream_id)
        return True, text, True

    @Command("ccr_clean", description="清空本插件的私聊回忆总结库（仅操作员）",
             pattern=r"(?<!\S)/?ccr\s+clean\s*$", permission="operator")
    async def cmd_ccr_clean(self, **kwargs: Any) -> tuple[bool, str, bool]:
        # 裁决 A1（异议 1）：总开关关闭时直接短路，给"未执行"结果，不清库、不清标记
        if not self._cross_chat_recall_active():
            self._file_log.info("命令 /ccr clean 未执行：私聊回忆未启用")
            return False, "插件未启用，命令未执行。", True
        stream_id = str(kwargs.get("stream_id") or "")
        deleted = self.store.clear()
        # 顺带清掉所有强制注入与粘性召回标记，避免指向已被清空的数据
        self._force_inject.clear()
        self._tool_recall_sticky.clear()
        self._file_log.info("命令 /ccr clean 执行：删除 %d 条总结（发起者=%s）",
                            deleted, kwargs.get("user_id"))
        text = f"[ccr] 私聊回忆库已清空（删除 {deleted} 条总结）。"
        await self.ctx.send.text(text, stream_id)
        return True, text, True

    # -- Hook：消息推进 -----------------------------------------------------

    @HookHandler(_RECEIVE_HOOK, name="ccr_push_counter", mode=HookMode.OBSERVE, order=HookOrder.NORMAL)
    async def on_message_after_process(self, **kwargs: Any) -> None:
        if not self._cross_chat_recall_active():
            return
        cfg = self.config.cross_chat_recall
        if cfg.trigger_mode != "push":
            return
        message = kwargs.get("message")
        if not isinstance(message, Mapping):
            return
        fields = extract_message_fields(message)
        # 只统计群聊里的真实用户消息（不含 bot 自身、通知、命令、注入合成记录）
        if not fields.get("group_id") or fields.get("is_self") or fields.get("is_synthetic"):
            return
        if fields.get("is_command"):
            return
        if not self._group_allowed(fields["group_id"]):
            return
        session_id = fields.get("session_id") or ""
        if not session_id:
            return
        count = self._push_counters.get(session_id, 0) + 1
        threshold = int(cfg.scan_count)
        if count < threshold:
            self._push_counters[session_id] = count
            self._file_log.debug("消息推进：群 %s 计数 %d/%d", fields["group_id"], count, threshold)
            return
        self._push_counters[session_id] = 0
        self._file_log.info(
            "消息推进：群 %s 已推进 %d 条非 bot 消息（session=%s）→ 触发扫描", fields["group_id"], threshold, session_id,
        )
        self._schedule_scan(session_id, reason="消息推进")
