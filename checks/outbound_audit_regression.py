"""离线验证状态发送、并发补发、送达账本和文本规则。"""

import asyncio
import importlib
import logging
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT = Path(__file__).resolve().parents[1]
NAME = "bpp_outbound_audit_" + ROOT.name.replace("-", "_").replace(".", "_")
package = ModuleType(NAME)
package.__path__ = [str(ROOT)]
sys.modules[NAME] = package
implementation = importlib.import_module(NAME + ".plugin")
takeover = importlib.import_module(NAME + ".modules.post_process_takeover")
processing = importlib.import_module(NAME + ".post_processing")
rules = importlib.import_module(NAME + ".text_rules")


@pytest.fixture
def plugin():
    value = implementation.BetterPostProcessingPlugin()
    value._plugin_config_instance = implementation.BetterPostProcessingConfig()
    value._ctx = SimpleNamespace(logger=logging.getLogger("bpp.audit.regression"))
    value._multi_message_active = lambda: True
    value._seed_at_cooldown_from_context = AsyncMock()
    value._recall_delay_seconds = lambda text: 0
    return value


def outbound(text, *, anchor=None):
    message = {
        "session_id": "chat", "message_id": "send_api_same_millisecond", "timestamp": "1000.25",
        "platform": "qq", "processed_plain_text": text,
        "message_info": {"group_info": {"group_id": "fixture"}},
        "raw_message": [{"type": "text", "data": text}],
    }
    if anchor:
        message["reply_to"] = anchor
    return message


@pytest.mark.parametrize("response", [TimeoutError("late cap.call"), False])
def test_bpp_status_submits_only_one_format_when_receipt_is_unclear(plugin, response):
    outcome = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
    plugin.ctx.send = SimpleNamespace(
        forward=AsyncMock(**outcome), text=AsyncMock(return_value=True), image=AsyncMock(return_value=True),
    )
    plugin._build_status_text = lambda: ["状态"]
    plugin._render_status_image = AsyncMock(return_value=("png", ""))
    ok, info, handled = asyncio.run(plugin.handle_bpp_command(stream_id="chat", message={"platform": "QQ"}))
    assert not ok and handled and "送达未确认" in info
    plugin.ctx.send.forward.assert_awaited_once()
    plugin.ctx.send.text.assert_not_called()
    plugin.ctx.send.image.assert_not_called()


@pytest.mark.parametrize("platform", ["qq", "webui"])
def test_bpp_status_chooses_text_before_sending(plugin, platform):
    plugin.ctx.send = SimpleNamespace(forward=AsyncMock(return_value=True), text=AsyncMock(return_value=True))
    plugin._build_status_text = lambda: ["状态"]
    plugin._render_status_image = AsyncMock(return_value=("", "renderer unavailable"))
    assert asyncio.run(plugin._bpp_send_status("chat", platform=platform))
    if platform == "qq":
        assert plugin.ctx.send.forward.call_args.args[0][0]["segments"][0]["type"] == "text"
        plugin.ctx.send.text.assert_not_called()
    else:
        plugin._render_status_image.assert_not_called()
        plugin.ctx.send.forward.assert_not_called()
        plugin.ctx.send.text.assert_awaited_once()


def test_concurrent_identical_follow_ups_keep_their_own_targets(plugin):
    plugin.config.at_cooldown.enabled = False

    async def scenario():
        all_entered = asyncio.Event()
        both_enhanced = asyncio.Event()
        preparations = []
        enhanced_messages = []
        routed = []

        async def make_delivery(segments, session_id, **kwargs):
            original = kwargs["processed_plain_text"]
            # 宿主将参数原样交给构建 Hook，但消息对象里只存 strip 后的摘要。
            message = outbound(original.strip())
            message["raw_message"] = segments
            preparations.append((original, message))
            if len(preparations) == 2:
                all_entered.set()
            await asyncio.wait_for(all_entered.wait(), 2)
            assert message["processed_plain_text"] == "同一句"
            assert segments == [{"type": "text", "data": "同一句"}]
            built = await plugin.handle_after_build_message(
                message=message, stream_id=session_id, processed_plain_text=original,
            )
            assert built["modified_kwargs"]["processed_plain_text"] == "同一句"
            enhanced = await plugin.handle_before_send(message=built["modified_kwargs"]["message"])
            outgoing = enhanced["modified_kwargs"]
            enhanced_messages.append(outgoing)
            if len(enhanced_messages) == 2:
                both_enhanced.set()
            await asyncio.wait_for(both_enhanced.wait(), 2)
            # 两条消息的宿主内部 message_id 相同，LATE 仍分别放行各自的 @。
            outgoing["message"]["raw_message"].append({"type": "at", "data": {"target_user_id": "foreign"}})
            guarded = await plugin.handle_before_send_at_guard(**outgoing)
            routed.append(guarded["modified_kwargs"])
            return {"sent": True, "message_id": "confirmed"}

        plugin.ctx.send = SimpleNamespace(hybrid=make_delivery)
        plan = [takeover.FollowUpItem(kind="text", text="尾段")]
        assert plugin._store_prepared_segments("chat", "同一句", "首段", plan, target_id="reply")
        ordinary = outbound("同一句")
        assert await plugin.handle_after_build_message(message=ordinary) == {"action": "continue"}
        assert await plugin.handle_before_send(message=ordinary) == {"action": "continue"}
        results = await asyncio.gather(
            plugin._send_follow_up_text("chat", "同一句", typing=False, quote_target="reply-A",
                                        at_target="111", at_user_id="111", at_nickname="甲"),
            plugin._send_follow_up_text("chat", "同一句", typing=False, quote_target="reply-B",
                                        at_target="222", at_user_id="222", at_nickname="乙"),
        )
        assert len({original for original, _ in preparations}) == 2
        assert all(result.status is takeover.FollowUpSendStatus.SENT for result in results)
        assert {(item["reply_message_id"], item["message"]["raw_message"][0]["data"]["target_user_id"])
                for item in routed} == {("reply-A", "111"), ("reply-B", "222")}
        assert all(len([component for component in item["message"]["raw_message"] if component["type"] == "at"]) == 1
                   for item in routed)
        assert plugin._follow_up_intents == {}
        first = await plugin.handle_after_build_message(message=outbound("同一句", anchor="reply"))
        assert first["modified_kwargs"]["message"]["processed_plain_text"] == "首段"

    asyncio.run(scenario())


def test_native_receipts_match_distinct_prepared_rounds_even_in_reverse_order(plugin):
    async def scenario():
        for suffix in ("A", "B"):
            tail = takeover.FollowUpItem(kind="text", text=f"尾段{suffix}")
            assert plugin._store_prepared_segments("chat", "同一句", "首段", [tail], target_id=f"reply-{suffix}")
        built = []
        for suffix in ("A", "B"):
            result = await plugin.handle_after_build_message(message=outbound("同一句", anchor=f"reply-{suffix}"))
            message = result["modified_kwargs"]["message"]
            message["message_id"] = f"adapter-{suffix}"
            built.append(message)
        unrelated = outbound("首段", anchor="reply-A")
        assert len(plugin._pending_follow_ups) == 2
        await plugin.handle_after_send_follow_up(message=unrelated, sent=True)
        assert len(plugin._pending_follow_ups) == 2
        plugin._run_follow_up_items = AsyncMock()
        for message in reversed(built):
            await plugin.handle_after_send_follow_up(message=message, sent=True)
        await asyncio.gather(*plugin._stream_follow_up_tasks["chat"])
        latest = plugin._get_planner_segment_record("chat")
        assert latest["sent"] == [("首段", "adapter-B")]
        calls = plugin._run_follow_up_items.call_args_list
        assert {call.kwargs["previous_message_id"]: call.args[1][0].text for call in calls} == {
            "adapter-A": "尾段A", "adapter-B": "尾段B",
        }
        assert all(call.kwargs["planner_record"]["done"].is_set() for call in calls)

    asyncio.run(scenario())


def test_unanchored_reply_splits_and_removes_all_addressing(plugin):
    plugin._post_process_takeover_active = lambda: True
    plan = processing.ResponsePlan(
        segments=[
            processing.ProcessedResponseSegment("第一段", separator="。"),
            processing.ProcessedResponseSegment("第二段", quote_previous=True),
        ],
        corrections={1: processing.CorrectionDecision(1, "quote", "纠正词")},
        deferred=(),
    )
    plugin._get_processor = lambda: SimpleNamespace(plan=lambda *args, **kwargs: plan, last_stats={})
    plugin._simulated_typing_seconds = lambda text: 0

    def add_addressing(message):
        message["raw_message"] = [
            {"type": "reply", "data": {"target_message_id": "other"}},
            {"type": "at", "data": {"target_user_id": "other"}},
            {"type": "text", "data": " "},
            *message["raw_message"],
        ]

    def assert_direct(message):
        assert all(component["type"] not in {"at", "reply"} for component in message["raw_message"])
        assert not message.get("reply_to")

    async def process_outbound(message):
        # 模拟其它出站 Hook 在两个阶段分别附加了 @ 及引用。
        kwargs = {"message": message}
        for handler in (plugin.handle_before_send, plugin.handle_before_send_at_guard):
            add_addressing(message)
            message["reply_to"] = "other"
            kwargs.update(set_reply=True, reply_message_id="other")
            result = await handler(**kwargs)
            kwargs = result["modified_kwargs"]
            message = kwargs["message"]
            assert_direct(message)
            assert kwargs["set_reply"] is False and not kwargs["reply_message_id"]
        return message

    async def scenario():
        generated = await plugin.handle_before_post_process(
            response="第一段。第二段", session_id="chat", reply_message_id="", reply_tool_args={},
        )
        full = generated["modified_kwargs"]["response"]
        assert full == "第一段\n第二段"

        # 非原生首发的同正文不会抢用直发计划。
        ordinary = outbound(full)
        for preview in ("", full + "\t" * 128):
            assert await plugin.handle_after_build_message(
                message=ordinary, processed_plain_text=preview,
            ) == {"action": "continue"}
        wrong_layout = outbound("第一段第二段")
        assert await plugin.handle_after_build_message(
            message=wrong_layout, processed_plain_text="第一段第二段",
        ) == {"action": "continue"}

        first = outbound(full)
        add_addressing(first)
        built = await plugin.handle_after_build_message(
            message=first, processed_plain_text=f"[引用消息]other\n[发言内容]@other {full}",
        )
        first = built["modified_kwargs"]["message"]
        assert first["processed_plain_text"] == "第一段"
        assert_direct(first)
        first = await process_outbound(first)
        first["message_id"] = "native-first"
        delivered = []

        async def send(segments, session_id, **kwargs):
            message = outbound(kwargs["processed_plain_text"].strip())
            message["raw_message"] = segments
            built = await plugin.handle_after_build_message(
                message=message, stream_id=session_id, processed_plain_text=kwargs["processed_plain_text"],
            )
            message = await process_outbound(built["modified_kwargs"]["message"])
            delivered.append(message["processed_plain_text"])
            return {"sent": True, "message_id": f"platform-{len(delivered)}"}

        plugin.ctx.send = SimpleNamespace(hybrid=send)
        await plugin.handle_after_send_follow_up(message=first, sent=True)
        await asyncio.gather(*plugin._stream_follow_up_tasks["chat"])
        assert delivered == ["第二段", "纠正词"]
        assert plugin._planner_segment_records["chat"]["done"].is_set()

    asyncio.run(scenario())



@pytest.mark.parametrize(
    ("scope", "remembered_private", "platform_id", "mode", "recalled", "fixed_text"),
    [
        pytest.param("legacy-group", False, "native-first", "recall", True, "这是首段", id="cold-group"),
        pytest.param("group", False, "", "recall", False, "这是首段", id="missing-platform-id"),
        pytest.param("private", True, "native-first", "direct", False, "首", id="known-private"),
        pytest.param("private", False, "native-first", "recall", False, "首", id="late-private"),
    ],
)
def test_unanchored_typo_correction_checks_the_delivered_scope(
    plugin, scope, remembered_private, platform_id, mode, recalled, fixed_text,
):
    plugin._post_process_takeover_active = lambda: True
    plugin._simulated_typing_seconds = lambda text: 0
    weights = plugin.config.chinese_typo_weights
    weights.weight_correction_direct = weights.weight_correction_quote = weights.weight_correction_none = 0
    weights.weight_correction_recall = 1
    plugin._recall_message = AsyncMock(return_value=True)
    plugin._send_follow_up_text = AsyncMock(return_value=takeover.FollowUpSendResult(
        takeover.FollowUpSendStatus.SENT, "follow-up",
    ))

    def plan(response, *, choose_mode, **kwargs):
        selected = choose_mode(0, "首")
        assert selected == mode
        return processing.ResponsePlan(
            segments=[
                processing.ProcessedResponseSegment("这是手段"),
                processing.ProcessedResponseSegment("下一段"),
            ],
            corrections={0: processing.CorrectionDecision(0, selected, "首", sentence="这是首段")},
            deferred=(),
        )

    plugin._get_processor = lambda: SimpleNamespace(plan=plan, last_stats={})

    async def scenario():
        if remembered_private:
            assert plugin._remember_chat_scope({"session_id": "chat", "message_info": {}}, inbound=True) is False
        generated = await plugin.handle_before_post_process(response="这是首段。下一段", session_id="chat")
        full = generated["modified_kwargs"]["response"]
        first = outbound(full)
        first["message_info"] = {}
        if scope == "legacy-group":
            first["message_info"]["additional_config"] = {"platform_io_target_group_id": "qq-group"}
        else:
            first["scope"] = scope
        result = await plugin.handle_after_build_message(message=first, processed_plain_text=full)
        sent = result["modified_kwargs"]["message"]
        if platform_id:
            sent["message_id"] = platform_id
        await plugin.handle_after_send_follow_up(message=sent, sent=True)
        await asyncio.gather(*plugin._stream_follow_up_tasks["chat"])

        if recalled:
            plugin._recall_message.assert_awaited_once_with(platform_id)
        else:
            plugin._recall_message.assert_not_awaited()
        calls = plugin._send_follow_up_text.await_args_list
        assert [call.args[1] for call in calls] == [fixed_text, "下一段"]
        assert all(call.kwargs["unanchored"] and not call.kwargs.get("quote_target")
                   and not call.kwargs.get("at_target") for call in calls)
        record = plugin._planner_segment_records["chat"]
        assert record["sent"][0] == ("这是手段", sent["message_id"])
        assert record["done"].is_set()

    asyncio.run(scenario())


def test_private_reply_keeps_quote_and_strips_at_in_both_send_hooks(plugin):
    plugin.config.at_cooldown.enabled = False
    plugin.config.replyer_post_process.strip_external_at = False
    plugin._apply_reply_style = AsyncMock(return_value=False)
    plugin._message_in_reply_flow = lambda session_id, target: target == "question"

    async def scenario():
        message = outbound("回答", anchor="question")
        message["scope"] = "private"
        kwargs = {"message": message, "set_reply": True, "reply_message_id": "question"}
        for handle in (plugin.handle_before_send, plugin.handle_before_send_at_guard):
            kwargs["message"]["raw_message"].insert(0, {"type": "at", "data": {"target_user_id": "author"}})
            kwargs = (await handle(**kwargs))["modified_kwargs"]
            assert kwargs["message"]["raw_message"] == [{"type": "text", "data": "回答"}]
            assert kwargs["message"]["reply_to"] == "question"
            assert kwargs["set_reply"] is True and kwargs["reply_message_id"] == "question"

    asyncio.run(scenario())


def test_private_recall_config_is_retired_without_removing_other_settings():
    raw = {"chinese_typo": {"private_no_recall": False, "recall_fallback": "direct"}}
    assert implementation.migrate_legacy_config(raw)["chinese_typo"] == {"recall_fallback": "direct"}


def test_single_segment_unanchored_reply_is_also_direct(plugin):
    async def scenario():
        assert plugin._store_prepared_segments("chat", "独白", "独白", [], target_id="")
        message = outbound("独白")
        message["raw_message"][:0] = [
            {"type": "at", "data": {"target_user_id": "fixture"}},
            {"type": "text", "data": " "},
        ]
        built = await plugin.handle_after_build_message(message=message, processed_plain_text="@fixture 独白")
        delivered = built["modified_kwargs"]["message"]
        assert delivered["raw_message"] == [{"type": "text", "data": "独白"}]
        await plugin.handle_after_send_follow_up(message=delivered, sent=True)
        assert plugin._planner_segment_records["chat"]["done"].is_set()
        assert plugin._stream_follow_up_tasks == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("style", ["at", "quote_at"])
def test_quote_and_at_are_counted_only_from_delivered_components(plugin, style):
    plugin._quote_takeover_active_for = lambda message: True
    plugin._pick_reply_style = lambda *args: style
    plugin._lookup_reply_target = AsyncMock(return_value=("123", "用户", "", time.time()))
    for name in ("stale_age_seconds", "stale_threshold_messages", "force_direct_threshold_messages"):
        setattr(plugin.config.quote_reply, name, 0)

    def new_round():
        plugin._reply_rounds["chat"] = {
            "id": 1, "target_id": "target", "started": time.monotonic(), "style_done": False,
            "emoji_done": True,
        }

    async def scenario():
        new_round()
        message = outbound("回答", anchor="target")
        result = await plugin.handle_before_send(message=message, reply_message_id="target", set_reply=False)
        attempt = result["modified_kwargs"]
        assert plugin._message_has_at(attempt["message"])
        assert not plugin._quoted_before("chat", "target")
        assert not plugin._style_taken_before("chat", "target")
        assert not plugin._at_cooldown_active("chat")
        await plugin.handle_outbound_observer(**attempt, sent=False)
        assert not plugin._quoted_before("chat", "target")
        assert not plugin._at_cooldown_active("chat")

        new_round()
        result = await plugin.handle_before_send(message=message, reply_message_id="target", set_reply=False)
        delivered = result["modified_kwargs"]
        assert plugin._message_has_at(delivered["message"])
        await plugin.handle_outbound_observer(**delivered, sent=True)
        assert plugin._style_taken_before("chat", "target")
        assert plugin._quoted_before("chat", "target")
        assert plugin._at_cooldown_active("chat")

    asyncio.run(scenario())


def test_cross_component_rules_keep_attachments_and_use_replaced_text_for_cover(plugin):
    image = {"type": "image", "data": "fixture"}
    prefix = {"type": "at", "data": {"target_user_id": "123"}}
    original = [
        prefix, {"type": "text", "data": "请换坏"}, image, {"type": "text", "data": "词尾巴"},
    ]
    plugin.config.text_rules.apply_scope = "all_outbound"
    plugin._parsed_rules = rules.parse_rules(["r:坏词/好", "r:好尾/正文"])
    message = {"raw_message": original, "processed_plain_text": "请换坏 词尾巴"}
    assert plugin._apply_text_rules_to_message(message) is True
    assert message["raw_message"] == [
        prefix, {"type": "text", "data": "请换正文"}, image, {"type": "text", "data": "巴"},
    ]
    assert message["processed_plain_text"] == "请换正文巴"

    plugin._parsed_rules = rules.parse_rules(["r:坏词/好", "r:好尾/正文", "c:正文"])
    message = {"raw_message": original}
    assert plugin._apply_text_rules_to_message(message) is implementation._TEXT_RULES_DROPPED
    assert message["raw_message"] == original
