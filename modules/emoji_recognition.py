"""表情包含义识别：共享生成任务、时序拼图与可恢复的 GIF 升级队列。"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Any, Dict, Optional

from ..emoji_meanings import LEGACY_KEY_PREFIX, detect_image_format, is_animated_image, normalize_image_for_vlm


class EmojiRecognitionMixin:
    """供工作循环与聊天流入站处理共用同一条识别链路。"""

    def _init_emoji_recognition(self) -> None:
        self._meaning_inflight: Dict[str, asyncio.Task] = {}
        self._previous_config_version = ""

    def _capture_previous_config_version(self, config_data: Any) -> None:
        """新宿主在归一化时传入旧配置；早期宿主另从磁盘配置获取版本。"""
        if not self._previous_config_version and isinstance(config_data, dict):
            section = config_data.get("plugin")
            if isinstance(section, dict):
                self._previous_config_version = str(section.get("config_version") or "")

    def _prepare_gif_upgrade(self, version: str) -> None:
        previous, count = self._meaning_store.prepare_gif_reprocessing(version, self._previous_config_version)
        for image_hash, description in self._meaning_store.pending_reprocessing():
            self._enqueue_emoji_meaning(image_hash, description, force=True)
        if count:
            self.ctx.logger.info(
                "GIF 时序识别升级（%s -> %s）：%d 条历史 GIF 等待重识别，成功后替换旧含义；重启继续未完成项",
                previous or "旧库", version, count,
            )
        elif previous != version:
            self.ctx.logger.info("表情包含义库版本 %s -> %s：没有待重识别的 GIF，跳过", previous or "新库/旧库", version)

    async def _ensure_meaning_generation(
        self, image_hash: str, description: str, sample_cache: Optional[Dict[str, Any]] = None, *,
        reuse_existing: bool = False,
    ) -> bool:
        """同一哈希共享一次生成；调用方超时不会取消后台识别或丢失升级任务。"""
        if reuse_existing and self._meaning_store.content_for_hash(image_hash)[1]:
            # 调用方在排队等待期间，另一条消息可能已经完成了首次识别。
            return True
        task = self._meaning_inflight.get(image_hash)
        if task is None:
            # 入站处理抢先识别时，移除尚未被后台工作循环消费的同一项。
            if image_hash in self._meaning_queued:
                self._meaning_queue = deque(item for item in self._meaning_queue if item[0] != image_hash)
                self._meaning_queued.discard(image_hash)
            task = asyncio.create_task(self._recognize_emoji_meaning(image_hash, description, sample_cache))
            self._meaning_inflight[image_hash] = task
            self._track_task(task)

            def completed(finished: asyncio.Task) -> None:
                if self._meaning_inflight.get(image_hash) is finished:
                    self._meaning_inflight.pop(image_hash, None)

            task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def _process_meaning_batch(self) -> None:
        """逐条消费队列，保留一批内的抽样缓存；失败项留到下一轮重试。"""
        sample_cache: Dict[str, Any] = {}
        seen = set()
        for _ in range(min(len(self._meaning_queue), self.config.emoji_meaning.batch_size)):
            if not self._meaning_queue:
                break
            image_hash, description = self._meaning_queue.popleft()
            self._meaning_queued.discard(image_hash)
            if image_hash in seen:
                self._enqueue_emoji_meaning(image_hash, description, force=True)
                break
            seen.add(image_hash)
            await self._ensure_meaning_generation(image_hash, description, sample_cache)

    async def _recognize_emoji_meaning(
        self, image_hash: str, description: str, sample_cache: Optional[Dict[str, Any]] = None
    ) -> bool:
        """按哈希取图，在线程中抽帧，再调用 VLM；仅成功时替换旧含义。"""
        original_hash = image_hash
        try:
            if image_hash.startswith(LEGACY_KEY_PREFIX):
                rows = await self.ctx.db.query(
                    "Images", query_type="get", filters={"description": description, "image_type": "emoji"}, limit=2
                )
                if not isinstance(rows, list) or len(rows) != 1:
                    raise RuntimeError("旧描述键不能唯一对应宿主表情包，保留待重识别任务")
                image_hash = str(rows[0].get("image_hash") or "")
                if not re.fullmatch(r"[0-9a-fA-F]{64}", image_hash):
                    raise RuntimeError("旧描述键对应的宿主图片缺少有效哈希")
            image_base64 = self._take_inline_emoji_bytes(image_hash)
            if image_base64 is None:
                image_base64 = await self._load_emoji_bytes_by_hash(image_hash)
            if image_base64 is None:
                cache = sample_cache if sample_cache is not None else {}
                if "images" not in cache:
                    cache["images"] = await self._sample_emoji_images()
                if cache["images"] is None:
                    self._enqueue_emoji_meaning(original_hash, description, force=True)
                    return False
                image_base64 = cache["images"].get(image_hash)
            if not image_base64:
                self._record_meaning_sample_miss(original_hash, description)
                return False

            def prepare() -> tuple:
                source_format = detect_image_format(image_base64)
                animated = source_format == "gif" or is_animated_image(image_base64)
                return normalize_image_for_vlm(image_base64), source_format, animated

            normalized, source_format, animated = await asyncio.to_thread(prepare)
            if normalized is None:
                raise RuntimeError(f"图片格式不可用或动图抽帧失败/超限（{source_format or '未知'}）")
            image_payload, image_format = normalized
            meaning = await self._generate_emoji_meaning(description, image_payload, image_format, animated=animated)
            if not self._meaning_store.upsert(
                image_hash, description, meaning, image_format=source_format or image_format, source="scan"
            ):
                raise RuntimeError("含义写入被拒绝：哈希或含义为空")
            self._meaning_store.finish_reprocessing(original_hash, image_hash)
            self._meaning_attempts.pop(original_hash, None)
            self._meaning_misses.pop(original_hash, None)
            self.ctx.logger.info(
                "已收录表情包含义（hash=%s desc=%r 格式=%s，%s，长度 %d）",
                image_hash[:12], description, source_format or image_format,
                "时序拼图" if animated else "静态图片", len(meaning),
            )
            return True
        except Exception as exc:
            self.ctx.logger.warning("表情包含义生成失败（hash=%s）: %s", original_hash[:12], exc)
            self._record_meaning_failure(original_hash, description, str(exc))
            return False
