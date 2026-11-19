"""离线回归：python -m pytest checks/emoji_regression.py（需 pytest-asyncio 与宿主 SDK）。"""

import asyncio
import base64
import copy
import importlib
import io
import json
import logging
import sqlite3
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "bpp_regression_" + ROOT.name.replace("-", "_").replace(".", "_")
package = types.ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
core = importlib.import_module(PACKAGE + ".emoji_meanings")
implementation = importlib.import_module(PACKAGE + ".plugin")
MODERN = hasattr(implementation.BetterPostProcessingPlugin, "handle_inbound_emoji_stream_meaning")


def encoded_image(fmt="GIF", colors=("red", "lime", "blue"), durations=None):
    frames = [Image.new("RGB", (40, 40), color) for color in colors]
    buffer = io.BytesIO()
    if fmt == "GIF":
        frames[0].save(buffer, format=fmt, save_all=True, append_images=frames[1:], duration=durations or 100, loop=0)
    else:
        frames[0].save(buffer, format=fmt)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@pytest.fixture
def store(tmp_path):
    value = core.EmojiMeaningStore(tmp_path / "meanings.db")
    value.initialize()
    yield value
    value.close()


class LLM:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.delay = 0

    async def generate(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            return {"success": False, "error": "fixture VLM error"}
        return {"success": True, "response": "小人先点头又摇头，表达犹豫"}


@pytest_asyncio.fixture
async def plugin(tmp_path):
    value = implementation.BetterPostProcessingPlugin()
    value._plugin_config_instance = implementation.BetterPostProcessingConfig()
    value._ctx = SimpleNamespace(
        paths=SimpleNamespace(data_dir=tmp_path, runtime_dir=tmp_path),
        logger=logging.getLogger("bpp.regression"), llm=LLM(),
    )
    value._initialize_meaning_store()
    yield value
    for task in list(value._tasks):
        task.cancel()
    await asyncio.gather(*value._tasks, return_exceptions=True)
    value._meaning_store.close()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_static_images_keep_their_bytes(fmt):
    payload = encoded_image(fmt)
    normalized, normalized_format = core.normalize_image_for_vlm("data:image/x;base64,\n" + payload.rstrip("="))
    assert normalized == payload
    assert normalized_format == fmt.lower()


def test_other_static_format_is_converted():
    result = core.normalize_image_for_vlm(encoded_image("BMP"))
    assert result[1] == "png"
    assert core.detect_image_format(result[0]) == "png"


def test_gif_preserves_order_last_frame_and_duration_labels(monkeypatch):
    labels = []
    original_text = ImageDraw.ImageDraw.text

    def record_text(draw, position, text, *args, **kwargs):
        labels.append(text)
        return original_text(draw, position, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record_text)
    payload = encoded_image(durations=[80, 320, 120])
    normalized, fmt = core.normalize_image_for_vlm(payload)
    image = Image.open(io.BytesIO(base64.b64decode(normalized)))
    assert fmt == "png" and image.size == (992, 390)
    assert [image.getpixel((8 + n * 328 + 160, 38 + 24 + 160)) for n in range(3)] == [
        (255, 0, 0), (0, 255, 0), (0, 0, 255)
    ]
    assert core.detect_image_format(payload) == "gif"
    assert "FRAME 02 | t=0.080s" in labels and "FRAME 03 | t=0.400s" in labels
    messages = core.build_generation_messages("custom prompt", normalized, fmt, animated=True)
    assert messages[0]["content"][0]["text"].startswith("custom prompt")
    assert "FRAME" in messages[0]["content"][0]["text"]
    assert "随时间" in messages[0]["content"][0]["text"]


def test_storyboard_bounds_frame_count_and_includes_endpoints():
    payload = encoded_image(colors=[(n * 11, 0, 0) for n in range(20)], durations=[2000] + [20] * 19)
    normalized, _ = core.normalize_image_for_vlm(payload)
    sheet = Image.open(io.BytesIO(base64.b64decode(normalized)))
    assert sheet.width <= 992 and sheet.height <= 1094
    assert sheet.getpixel((168, 222)) == (0, 0, 0)
    # 最后一格必须是原 GIF 的最后一帧，而非首帧的重复。
    centers = [sheet.getpixel((168 + x * 328, 222 + y * 352)) for y in range(3) for x in range(3)]
    assert (209, 0, 0) in centers


def test_transparent_frames_respect_disposal():
    frames = []
    for n in range(3):
        frame = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        ImageDraw.Draw(frame).rectangle((n * 12, 12, n * 12 + 7, 19), fill="red")
        frames.append(frame)
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=100, disposal=2)
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    normalized, _ = core.normalize_image_for_vlm(payload)
    sheet = Image.open(io.BytesIO(base64.b64decode(normalized)))
    # 第二帧的旧位置应被透明背景清除，只有当前位置仍然为红色。
    assert sheet.getpixel((8 + 328 + 24, 62 + 120)) == (255, 255, 255)
    assert sheet.getpixel((8 + 328 + 120, 62 + 120)) == (255, 0, 0)


def test_single_frame_gif_also_has_a_storyboard():
    normalized, fmt = core.normalize_image_for_vlm(encoded_image(colors=["red"]))
    assert fmt == "png"
    assert Image.open(io.BytesIO(base64.b64decode(normalized))).size == (336, 390)


def test_broken_or_over_budget_animation_never_falls_back_to_first_frame(monkeypatch):
    assert core.normalize_image_for_vlm(base64.b64encode(b"GIF89a broken").decode()) is None
    monkeypatch.setattr(core, "_STORYBOARD_MAX_DECODE_PIXELS", 100)
    assert core.normalize_image_for_vlm(encoded_image()) is None


@pytest.mark.parametrize("has_static", [False, True])
def test_upgrade_empty_or_without_gif_has_no_work(store, has_static):
    if has_static:
        store.upsert("a" * 64, "static", "保留", image_format="png")
    assert store.prepare_gif_reprocessing("1.5.0", "1.4.8")[1] == 0
    assert store.pending_reprocessing() == []
    assert store.count() == int(has_static)


def test_upgrade_covers_all_gifs_and_resumes_only_unfinished_records(tmp_path):
    path = tmp_path / "upgrade.db"
    value = core.EmojiMeaningStore(path)
    value.initialize()
    value.upsert("a" * 64, "one", "旧含义一", image_format="gif")
    value.upsert("b" * 64, "two", "旧含义二", image_format="GIF")
    value.upsert("c" * 64, "static", "静态", image_format="png")
    # 即使配置已由宿主先更新，旧库仍应被发现。
    assert value.prepare_gif_reprocessing("2.0.0", "2.0.0")[1] == 2
    value.upsert("a" * 64, "one", "时序新含义", image_format="gif")
    value.close()
    value = core.EmojiMeaningStore(path)
    value.initialize()
    assert value.prepare_gif_reprocessing("2.0.0")[1] == 1
    assert value.pending_reprocessing() == [("b" * 64, "two")]
    assert value.content_for_hash("b" * 64)[1] == "旧含义二"
    assert value.content_for_hash("c" * 64)[1] == "静态"
    value.upsert("b" * 64, "two", "时序新含义二", image_format="gif")
    assert value.prepare_gif_reprocessing("2.0.1")[1] == 0
    value.close()


def test_pending_upgrade_is_protected_from_ttl(store):
    store.upsert("a" * 64, "gif", "保留", image_format="gif")
    store.upsert("b" * 64, "static", "淘汰", image_format="png")
    store.prepare_gif_reprocessing("1.5.0", "1.4.8")
    assert store.purge_expired_batch(1, time.time() + 10 * 86400) == 1
    assert store.content_for_hash("a" * 64)[1] == "保留"


def test_old_description_key_schema_migrates_without_loss(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE emoji_meanings (description TEXT PRIMARY KEY, image_hash TEXT, meaning TEXT, image_format TEXT, source TEXT, created_at REAL, updated_at REAL)")
        db.execute("INSERT INTO emoji_meanings VALUES ('old', '', '保留旧描述键', 'gif', 'old', 1, 1)")
    value = core.EmojiMeaningStore(path)
    value.initialize()
    assert value.prepare_gif_reprocessing("1.5.0")[1] == 1
    assert value.pending_reprocessing() == [("desc:old", "old")]
    assert value.content_for_description("old")[1] == "保留旧描述键"
    value.close()


@pytest.mark.asyncio
async def test_worker_sends_temporal_png_but_stores_original_gif_format(plugin):
    payload = encoded_image()
    key = core.sha256_of_base64(payload)
    plugin._meaning_inline_bytes[key] = payload
    plugin._meaning_store.upsert(key, "标签", "旧首帧含义", image_format="gif")
    plugin._meaning_store._require_conn().execute("DELETE FROM meaning_metadata WHERE key = 'gif_storyboard_v1'")
    plugin._meaning_store._require_conn().execute("UPDATE meaning_metadata SET value = '1.4.8' WHERE key = 'plugin_version'")
    plugin._prepare_gif_upgrade(implementation.SUPPORTED_CONFIG_VERSION)
    await plugin._process_meaning_batch()
    messages, kwargs = plugin.ctx.llm.calls[0]
    assert len(plugin.ctx.llm.calls) == 1 and kwargs["model"] == "vlm"
    assert messages[0]["content"][1]["image_format"] == "png"
    assert "FRAME" in messages[0]["content"][0]["text"]
    assert plugin._meaning_store.content_for_hash(key)[2] == "gif"
    assert plugin._meaning_store.pending_reprocessing() == []
    await plugin._process_meaning_batch()
    assert len(plugin.ctx.llm.calls) == 1


@pytest.mark.asyncio
async def test_worker_failures_preserve_old_meaning_and_durable_job(plugin):
    payload = encoded_image()
    key = core.sha256_of_base64(payload)
    plugin._meaning_inline_bytes[key] = payload
    plugin._meaning_store.upsert(key, "标签", "旧含义", image_format="gif")
    db = plugin._meaning_store._require_conn()
    db.execute("INSERT INTO meaning_reprocess VALUES (?, '标签')", (key,))
    db.commit()
    plugin.config.emoji_meaning.max_attempts = 2
    plugin.ctx.llm.fail = True

    async def load_source(_key):
        return payload

    plugin._load_emoji_bytes_by_hash = load_source
    plugin._enqueue_emoji_meaning(key, "标签")
    await plugin._process_meaning_batch()
    await plugin._process_meaning_batch()
    assert len(plugin.ctx.llm.calls) == 2 and key in plugin._meaning_abandoned
    assert plugin._meaning_store.content_for_hash(key)[1] == "旧含义"
    assert plugin._meaning_store.pending_reprocessing() == [(key, "标签")]


@pytest.mark.asyncio
async def test_concurrent_recognition_of_one_hash_uses_one_vlm_call(plugin):
    payload = encoded_image()
    key = core.sha256_of_base64(payload)
    plugin._meaning_inline_bytes[key] = payload
    plugin.ctx.llm.delay = 0.02
    assert await asyncio.gather(plugin._ensure_meaning_generation(key, ""), plugin._ensure_meaning_generation(key, "")) == [True, True]
    assert len(plugin.ctx.llm.calls) == 1


@pytest.mark.asyncio
async def test_legacy_gif_description_key_moves_to_real_hash_after_success(plugin):
    payload = encoded_image()
    key = core.sha256_of_base64(payload)
    plugin._meaning_inline_bytes[key] = payload
    plugin._meaning_store.upsert("desc:old", "old", "旧含义", image_format="gif")
    db = plugin._meaning_store._require_conn()
    db.execute("INSERT INTO meaning_reprocess VALUES ('desc:old', 'old')")
    db.commit()

    async def query(*args, **kwargs):
        return [{"image_hash": key}]

    plugin.ctx.db = SimpleNamespace(query=query)
    plugin._enqueue_emoji_meaning("desc:old", "old")
    await plugin._process_meaning_batch()
    assert plugin._meaning_store.content_for_hash(key)[1] == "小人先点头又摇头，表达犹豫"
    assert not plugin._meaning_store.content_for_hash("desc:old")[1]
    assert plugin._meaning_store.pending_reprocessing() == []


@pytest.mark.skipif(not MODERN, reason="1.4.x 宿主专用")
def test_disabled_rewriting_survives_both_host_config_upgrade_orders(tmp_path, monkeypatch):
    from maibot_sdk.config import rebuild_plugin_config_data

    config = {"plugin": {"config_version": "1.4.7"}, "emoji_meaning": {"rewrite_scope": []}}
    (tmp_path / "config.toml").write_text('[plugin]\nconfig_version = "1.4.7"\n[emoji_meaning]\nrewrite_scope = []\n', encoding="utf-8")
    monkeypatch.setattr(implementation, "__file__", str(tmp_path / "plugin.py"))
    # 早期宿主先裁剪配置，必须从磁盘旧配置带回用户的关闭选择。
    old_host_plugin = implementation.BetterPostProcessingPlugin()
    defaults = old_host_plugin.get_default_config()
    rebuilt = rebuild_plugin_config_data(defaults, config)
    normalized, _ = old_host_plugin.normalize_plugin_config(rebuilt)
    assert normalized["emoji_meaning"]["inject_into_chat_stream"] is False
    # 新宿主直接给归一化接口完整旧配置。
    current_host_plugin = implementation.BetterPostProcessingPlugin()
    normalized, _ = current_host_plugin.normalize_plugin_config(config)
    assert normalized["emoji_meaning"]["inject_into_chat_stream"] is False


@pytest.mark.parametrize(
    ("emoji", "expected"),
    [
        ({"rewrite_scope": [["planner"], 1]}, False),
        ({"rewrite_scope": [["planner"], " replyer "]}, True),
        ({"rewrite_scope": [["planner"], "planner"], "inject_into_chat_stream": False}, False),
    ],
)
def test_legacy_scope_migration_ignores_nested_entries(emoji, expected):
    config = {"plugin": {"config_version": "1.4.7"}, "emoji_meaning": emoji}
    normalized, _ = implementation.BetterPostProcessingPlugin().normalize_plugin_config(config)
    assert normalized["emoji_meaning"]["inject_into_chat_stream"] is expected


def message(segments):
    return {"message_id": "m1", "session_id": "s1", "raw_message": segments,
            "processed_plain_text": " ".join(str(item["data"]) for item in segments), "platform": "qq"}


@pytest.mark.skipif(not MODERN, reason="1.4.x 宿主专用")
@pytest.mark.asyncio
async def test_stream_attaches_by_hash_preserves_message_and_is_idempotent(plugin):
    for key, meaning in [("a" * 64, "内容一"), ("b" * 64, "内容二")]:
        plugin._meaning_store.upsert(key, "同一标签", meaning, image_format="gif")
    source = message([{"type": "text", "data": "字面文本 [表情包: 同一标签]"},
                      {"type": "emoji", "data": "[表情包: 同一标签]", "hash": "a" * 64},
                      {"type": "emoji", "data": "[表情包: 同一标签]", "hash": "b" * 64}])
    untouched = copy.deepcopy(source)
    result = await plugin.handle_inbound_emoji_stream_meaning(message=source, extra="preserved")
    modified = result["modified_kwargs"]["message"]
    assert source == untouched and result["modified_kwargs"]["extra"] == "preserved"
    assert modified["raw_message"][0] == source["raw_message"][0]
    assert "内容一" in modified["raw_message"][1]["data"]
    assert "内容二" in modified["raw_message"][2]["data"]
    assert modified["processed_plain_text"].count(core.EMOJI_CONTENT_SUFFIX_PREFIX) == 2
    assert modified["processed_plain_text"].startswith(source["raw_message"][0]["data"])
    assert await plugin.handle_inbound_emoji_stream_meaning(message=modified) == {"action": "continue"}
    assert not plugin.ctx.llm.calls


@pytest.mark.skipif(not MODERN, reason="1.4.x 宿主专用")
@pytest.mark.asyncio
async def test_stream_first_unseen_gif_is_recognized_before_message_is_released(plugin):
    payload = encoded_image()
    key = core.sha256_of_base64(payload)
    source = message([{"type": "emoji", "data": "[表情包]", "hash": key, "binary_data_base64": payload}])
    result = await plugin.handle_inbound_emoji_stream_meaning(message=source)
    modified = result["modified_kwargs"]["message"]
    assert "犹豫" in modified["raw_message"][0]["data"]
    assert modified["raw_message"][0]["hash"] == key
    assert modified["raw_message"][0]["binary_data_base64"] == payload
    assert len(plugin.ctx.llm.calls) == 1 and not plugin._meaning_queue


@pytest.mark.asyncio
async def test_concurrent_stream_messages_share_gif_recognition_without_requeue(plugin, monkeypatch):
    payload = encoded_image()
    key = core.sha256_of_base64(payload)
    source = message([{"type": "emoji", "data": "[表情包]", "hash": key, "binary_data_base64": payload}])
    entered = asyncio.Event()
    release = asyncio.Event()
    original_generate = plugin.ctx.llm.generate

    async def delayed_generate(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_generate(*args, **kwargs)

    original_ensure = plugin._ensure_meaning_generation
    joined = asyncio.Event()

    async def observe_join(image_hash, *args, **kwargs):
        if image_hash in plugin._meaning_inflight:
            joined.set()
        return await original_ensure(image_hash, *args, **kwargs)

    monkeypatch.setattr(plugin.ctx.llm, "generate", delayed_generate)
    monkeypatch.setattr(plugin, "_ensure_meaning_generation", observe_join)
    first = asyncio.create_task(plugin.handle_inbound_emoji_stream_meaning(message=source))
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(plugin.handle_inbound_emoji_stream_meaning(message=source))
    await asyncio.wait_for(joined.wait(), 2)
    release.set()
    results = await asyncio.gather(first, second)
    await plugin._process_meaning_batch()

    assert all("犹豫" in result["modified_kwargs"]["message"]["raw_message"][0]["data"] for result in results)
    assert len(plugin.ctx.llm.calls) == 1
    assert not plugin._meaning_queue and key not in plugin._meaning_queued


@pytest.mark.skipif(not MODERN, reason="1.4.x 宿主专用")
@pytest.mark.asyncio
async def test_stream_timeout_releases_message_and_does_not_cancel_background_generation(plugin):
    payload = encoded_image()
    key = core.sha256_of_base64(payload)
    plugin.config.emoji_meaning.stream_generation_timeout_seconds = 0.001
    plugin.ctx.llm.delay = 0.03
    source = message([{"type": "emoji", "data": "[表情包]", "hash": key, "binary_data_base64": payload}])
    untouched = copy.deepcopy(source)
    assert await plugin.handle_inbound_emoji_stream_meaning(message=source) == {"action": "continue"}
    await asyncio.gather(*plugin._tasks)
    assert source == untouched and plugin._meaning_store.content_for_hash(key)[1]
    assert len(plugin.ctx.llm.calls) == 1


@pytest.mark.skipif(not MODERN, reason="1.4.x 宿主专用")
@pytest.mark.asyncio
async def test_disabled_stream_and_text_only_messages_are_unchanged(plugin):
    source = message([{"type": "text", "data": "[表情包: 普通文字]"}])
    assert await plugin.handle_inbound_emoji_stream_meaning(message=source) == {"action": "continue"}
    plugin.config.emoji_meaning.inject_into_chat_stream = False
    source = message([{"type": "emoji", "data": "[表情包]", "hash": "a" * 64}])
    assert await plugin.handle_inbound_emoji_stream_meaning(message=source) == {"action": "continue"}
    assert not plugin.ctx.llm.calls and not plugin._meaning_queue


@pytest.mark.skipif(not MODERN, reason="1.4.x 宿主专用")
@pytest.mark.asyncio
async def test_outbound_empty_component_label_does_not_duplicate_message_text(plugin):
    key = "a" * 64
    plugin._meaning_store.upsert(key, "", "内容", image_format="gif")
    source = message([{"type": "emoji", "data": "", "hash": key}])
    source["processed_plain_text"] = "[表情包]"
    result = await plugin.handle_outbound_emoji_stream_meaning(message=source)
    modified = result["modified_kwargs"]["message"]
    assert modified["processed_plain_text"] == modified["raw_message"][0]["data"]
    assert modified["processed_plain_text"].count("[表情包]") == 1


@pytest.mark.skipif(not MODERN, reason="1.4.x 宿主专用")
@pytest.mark.asyncio
async def test_nested_forward_emoji_keeps_node_metadata_and_is_recognized(plugin):
    payload = encoded_image()
    key = core.sha256_of_base64(payload)
    source = message([{"type": "forward", "data": [{"user_id": "u1", "message_id": "nested", "content": [
        {"type": "emoji", "data": "[表情包]", "hash": key, "binary_data_base64": payload}
    ]}]}])
    source["processed_plain_text"] = "[合并转发] [表情包]"
    result = await plugin.handle_inbound_emoji_stream_meaning(message=source)
    node = result["modified_kwargs"]["message"]["raw_message"][0]["data"][0]
    assert node["user_id"] == "u1" and node["message_id"] == "nested"
    assert "犹豫" in node["content"][0]["data"]
    assert "犹豫" not in source["processed_plain_text"]


def test_versions_hooks_and_config_migration_are_consistent():
    value = implementation.BetterPostProcessingPlugin()
    value._plugin_config_instance = implementation.BetterPostProcessingConfig()
    assert value.config.plugin.config_version == json.loads((ROOT / "_manifest.json").read_text(encoding="utf-8"))["version"]
    value.normalize_plugin_config({"plugin": {"config_version": "1.4.7"}})
    assert value._previous_config_version == "1.4.7"
    names = {component["name"] for component in value.get_components()}
    if MODERN:
        assert {"inbound_emoji_stream_meaning", "outbound_emoji_stream_meaning"} <= names
        assert not {"replyer_emoji_label_rewriter", "planner_emoji_label_rewriter"} & names
        assert implementation.migrate_legacy_config({"emoji_meaning": {"rewrite_scope": []}})["emoji_meaning"] == {"inject_into_chat_stream": False}
        assert implementation.migrate_legacy_config({"emoji_meaning": {"rewrite_scope": ["planner"]}})["emoji_meaning"] == {"inject_into_chat_stream": True}
        assert implementation.migrate_legacy_config({"emoji_meaning": {"rewrite_scope": [], "inject_into_chat_stream": True}})["emoji_meaning"]["inject_into_chat_stream"] is True
        assert "附加到聊天流" in value._module_plugin_switch("emoji_meaning")[1]
    else:
        assert {"replyer_emoji_label_rewriter", "planner_emoji_label_rewriter"} <= names
        assert "inbound_emoji_stream_meaning" not in names


@pytest.mark.skipif(MODERN, reason="旧宿主请求注入兼容线")
@pytest.mark.asyncio
async def test_legacy_request_rewriting_is_preserved(plugin):
    plugin._meaning_store.upsert("a" * 64, "标签", "时序含义", image_format="gif")
    items = [{"role": "user", "parts": [{"type": "text", "text": "[表情包: 标签]"}], "meta": {"timestamp": 1}}]
    untouched = copy.deepcopy(items)
    result = await plugin.handle_replyer_emoji_label_rewrite(items=items, session_id="s1")
    assert items == untouched
    assert "时序含义" in result["modified_kwargs"]["items"][0]["parts"][0]["text"]
    assert result["modified_kwargs"]["items"][0]["meta"] == items[0]["meta"]
