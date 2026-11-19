"""离线验证发送回执。运行 python -m pytest checks/send_delivery_regression.py。"""

import asyncio
import importlib
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "bpp_delivery_" + ROOT.name.replace("-", "_").replace(".", "_")
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT)]
sys.modules[PACKAGE] = package
takeover = importlib.import_module(PACKAGE + ".modules.post_process_takeover")
processing = importlib.import_module(PACKAGE + ".post_processing")


class FollowUpHarness(takeover.PostProcessTakeoverMixin):
    def __init__(self, send, blocked=()):
        self._init_post_process_takeover()
        self.ctx = SimpleNamespace(
            send=SimpleNamespace(hybrid=send),
            logger=logging.getLogger("bpp.delivery.regression"),
        )
        self.config = SimpleNamespace(chinese_typo=SimpleNamespace(recall_fallback="quote"))
        self.blocked = set(blocked)
        self._recall_message = AsyncMock(return_value=True)

    def _multi_message_active(self):
        return True

    def _is_group_message(self, message):
        return message.get("scope") == "group"

    def _text_rules_would_drop(self, text):
        return text in self.blocked

    def _recall_delay_seconds(self, text):
        return 0

    def _simulated_typing_seconds(self, text):
        return 0

    def record(self):
        record = {"sent": [], "done": asyncio.Event()}
        self._planner_segment_records["chat"] = record
        return record

    async def run_items(self, items):
        await self._run_follow_up_items(
            "chat", items, typing=False, previous_message_id="first-id", first_text="首段",
            planner_record=self._planner_segment_records.get("chat"),
            allow_recall=True,
        )

    async def build_first_segment(self, items):
        self._store_prepared_segments("chat", "完整回复", "首段", items, target_id="original")
        message = {
            "session_id": "chat", "message_id": "first-id", "reply_to": "original", "scope": "group",
            "processed_plain_text": "完整回复", "raw_message": [{"type": "text", "data": "完整回复"}],
        }
        result = await self.handle_after_build_message(message=message, processed_plain_text="完整回复")
        return result["modified_kwargs"]["message"], self._get_planner_segment_record("chat")


def test_unconfirmed_first_segment_does_not_start_follow_ups():
    send = AsyncMock(return_value=True)
    harness = FollowUpHarness(send)
    async def scenario():
        message, record = await harness.build_first_segment(
            [takeover.FollowUpItem(kind="text", text="第二段", slot=1)]
        )
        await harness.handle_after_send_follow_up(message=message, sent=False)
        return record

    record = asyncio.run(scenario())
    send.assert_not_awaited()
    assert record["done"].is_set()
    assert record["sent"] == []


def test_rpc_timeout_leaves_one_late_delivery_and_releases_the_planner():
    async def scenario():
        release_delivery = asyncio.Event()
        delivered = []
        pending_host_calls = []

        async def finish_late(text):
            await release_delivery.wait()
            delivered.append(text)

        async def send_after_local_timeout(segments, session_id, **kwargs):
            # 模拟宿主在调用方停止等待后仍持有并执行原请求。
            pending_host_calls.append(asyncio.create_task(finish_late(segments[0]["data"])))
            raise TimeoutError("请求 cap.call 超时 (45000ms)")

        send = AsyncMock(side_effect=send_after_local_timeout)
        harness = FollowUpHarness(send)
        items = [
            takeover.FollowUpItem(kind="text", text="第二段", slot=1),
            takeover.FollowUpItem(kind="recall", text="修正", recall_slot=1),
            takeover.FollowUpItem(kind="text", text="第三段", slot=2),
        ]
        message, record = await harness.build_first_segment(items)
        await harness.handle_after_send_follow_up(message=message, sent=True)
        await asyncio.gather(*harness._stream_follow_up_tasks["chat"])
        assert record["done"].is_set()
        assert record["sent"] == [("首段", "first-id")]
        assert harness._follow_up_intents == {}
        harness._recall_message.assert_not_awaited()

        release_delivery.set()
        await asyncio.gather(*pending_host_calls)
        assert delivered == ["第二段"]
        send.assert_awaited_once()
        assert send.call_args.kwargs["rpc_timeout_ms"] == 45_000
        assert send.call_args.kwargs["return_details"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize("receipt", [False, {"sent": False}, None])
def test_unconfirmed_response_does_not_retry_or_advance(receipt):
    send = AsyncMock(return_value=receipt)
    harness = FollowUpHarness(send)
    record = harness.record()
    asyncio.run(harness.run_items([
        takeover.FollowUpItem(kind="text", text="第二段", slot=1),
        takeover.FollowUpItem(kind="text", text="第三段", slot=2),
    ]))
    send.assert_awaited_once()
    assert record["sent"] == []


@pytest.mark.parametrize("receipt", [True, {"sent": True, "message_id": None}])
def test_confirmed_send_without_id_advances_without_recalling_previous_message(receipt):
    send = AsyncMock(side_effect=[receipt, {"sent": True, "message_id": "fixed-id"}, True])
    harness = FollowUpHarness(send)
    record = harness.record()
    segments = [processing.ProcessedResponseSegment(text) for text in ("首段", "第二段", "第三段")]
    correction = processing.CorrectionDecision(1, "recall", "更正", sentence="修正第二段")
    items = harness._build_follow_up_items(segments, {1: correction}, ())
    asyncio.run(harness.run_items(items))
    harness._recall_message.assert_not_awaited()
    assert [call.args[0][0]["data"] for call in send.call_args_list] == ["第二段", "修正第二段", "第三段"]
    assert record["sent"] == [("修正第二段", "fixed-id"), ("第三段", "")]


@pytest.mark.parametrize("blocked_text", ["第二段", "修正第二段"])
def test_filter_does_not_recall_unrelated_or_unreplaceable_segment(blocked_text):
    send = AsyncMock(return_value={"sent": True, "message_id": "reply"})
    harness = FollowUpHarness(send, blocked=[blocked_text])
    segments = [processing.ProcessedResponseSegment(text) for text in ("首段", "第二段", "第三段")]
    correction = processing.CorrectionDecision(1, "recall", "更正", sentence="修正第二段")
    asyncio.run(harness.run_items(harness._build_follow_up_items(segments, {1: correction}, ())))
    harness._recall_message.assert_not_awaited()
    expected = ["第三段"] if blocked_text == "第二段" else ["第二段", "第三段"]
    assert [call.args[0][0]["data"] for call in send.call_args_list] == expected
    assert all(call.kwargs["sync_to_maisaka_history"] for call in send.call_args_list)
