"""python -m pytest checks/splitter_regression.py；不连接宿主或群聊。"""

import importlib
import random
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
NAME = "bpp_split_" + ROOT.name.replace("-", "_").replace(".", "_")
package = types.ModuleType(NAME)
package.__path__ = [str(ROOT)]
sys.modules[NAME] = package
splitter = importlib.import_module(NAME + ".sentence_splitter")
processing = importlib.import_module(NAME + ".post_processing")


def split(text, merge=False):
    with patch.object(splitter.random, "random", return_value=0.0 if merge else 1.0):
        return splitter.split_sentence_segments(text)


@pytest.mark.parametrize("mark", ["……", "…", "...", "......", "？！", "！？", "!", "?", "；"])
def test_tone_marks_remain_at_the_end_of_the_previous_segment(mark):
    segments = split("第一句完整表达" + mark + "第二句继续说明")
    assert [text for text, _ in segments] == ["第一句完整表达" + mark, "第二句继续说明"]


@pytest.mark.parametrize("boundary", ["\n", "\r\n", "\r", " ", "  \n  "])
def test_same_response_line_initial_ellipsis_moves_back_before_the_boundary(boundary):
    segments = split("第一句完整表达" + boundary + "……第二句继续说明")
    assert [text for text, _ in segments] == ["第一句完整表达……", "第二句继续说明"]
    if "\r" in boundary or "\n" in boundary:
        assert "\n" in segments[0][1]


def test_independent_replies_do_not_share_punctuation_state():
    first = split("这是第一条独立回复")
    second = split("……这是另一条独立回复")
    assert [text for text, _ in first] == ["这是第一条独立回复"]
    assert [text for text, _ in second] == ["……这是另一条独立回复"]


def test_two_clause_replies_preserve_the_ellipsis_on_the_first_message():
    random.seed(4)
    text = "第一段先完整表达一件事，随后把后半句话说完\n……第二段又说另外一件事，随后补充后半句话"
    segments = splitter.split_sentence_segments(text)
    assert [content for content, _ in segments] == [
        "第一段先完整表达一件事，随后把后半句话说完……",
        "第二段又说另外一件事，随后补充后半句话",
    ]


@pytest.mark.parametrize("text", ["……", "?!", "好", "好的", "?", "!", "。"])
def test_short_or_punctuation_only_replies_stay_in_one_piece(text):
    for seed in range(120):
        random.seed(seed)
        assert splitter.split_sentence_segments(text) == [(text, "")]


def test_ellipsis_and_closing_brackets_do_not_form_separate_messages():
    assert [text for text, _ in split("第一句\n……\n第二句")] == ["第一句……", "第二句"]
    assert [text for text, _ in split("第一句！？）\n第二句")] == ["第一句！？）", "第二句"]


@pytest.mark.parametrize("quotes", [("“", "”"), ("‘", "’"), ("「", "」"), ("『", "』"), ('"', '"'), ("'", "'")])
def test_matched_quotes_protect_only_their_own_contents(quotes):
    opening, closing = quotes
    quoted = opening + "第一句，第二句……第三句" + closing
    assert [text for text, _ in split(quoted + "，引号外继续，最后一句")] == [quoted, "引号外继续", "最后一句"]


def test_nested_quotes_and_apostrophes_do_not_swallow_later_boundaries():
    quoted = '“外层，里面是「内层，仍然完整」”'
    assert [text for text, _ in split(quoted + "，外部继续")] == [quoted, "外部继续"]
    assert [text for text, _ in split("It's fine，下一句")] == ["It's fine", "下一句"]
    assert [text for text, _ in split("没闭合的“引号，仍可继续，最后一句")] == ["没闭合的“引号", "仍可继续", "最后一句"]


def test_url_query_decimal_and_code_are_not_new_sentence_boundaries():
    url = "https://example.org/view?q=1.5&value=2"
    assert [text for text, _ in split(url + "，继续说明")] == [url, "继续说明"]
    assert [text for text, _ in split("版本1.5.0，价格3.14")] == ["版本1.5.0", "价格3.14"]
    assert [text for text, _ in split('`print("a,b?!")`，继续说明')] == ['`print("a,b?!")`', "继续说明"]


def test_repeated_spaces_do_not_allow_random_merge_across_a_newline():
    segments = split("第一句 \n 第二句", merge=True)
    assert [text for text, _ in segments] == ["第一句", "第二句"]
    assert "\n" in segments[0][1]


def test_punctuation_survives_probabilistic_and_count_limited_merges_once():
    text = "第一句……第二句？！第三句"
    merged = split(text, merge=True)
    assert "".join(content + separator for content, separator in merged) == text
    segments = [processing.ProcessedResponseSegment(content, separator=separator) for content, separator in split(text)]
    grouped, _ = processing._merge_groups(segments, 1)
    assert grouped[0].text == text
    assert grouped[0].text.count("……") == 1 and grouped[0].text.count("？！") == 1


@pytest.mark.parametrize("quote", [False, True])
def test_correction_messages_stay_separate_even_when_count_limit_is_one(quote):
    segments = [processing.ProcessedResponseSegment("正文一"),
                processing.ProcessedResponseSegment("纠正内容", quote_previous=quote, is_correction=True),
                processing.ProcessedResponseSegment("正文二")]
    grouped, starts = processing._merge_groups(segments, 1)
    assert [segment.text for segment in grouped] == ["正文一", "纠正内容", "正文二"]
    assert starts == [0, 1, 2] and grouped[1].is_correction


def test_quote_previous_alone_preserves_both_sides_of_its_boundary():
    segments = [processing.ProcessedResponseSegment("正文一"),
                processing.ProcessedResponseSegment("引用纠正", quote_previous=True),
                processing.ProcessedResponseSegment("正文二")]
    assert [s.text for s in processing._merge_groups(segments, 1)[0]] == ["正文一", "引用纠正", "正文二"]


def test_finalization_keeps_correction_flags_after_kaomoji_recovery():
    processor = processing.PostProcessor(splitter_enable_kaomoji_protection=True, splitter_max_split_num=1)
    segments = [processing.ProcessedResponseSegment("正文"),
                processing.ProcessedResponseSegment("修正__KAOMOJI_0__", is_correction=True),
                processing.ProcessedResponseSegment("后续")]
    finalized = processor._finalize(segments, "全文", {"__KAOMOJI_0__": "(o_o)"})
    assert finalized[0][1].text == "修正(o_o)" and finalized[0][1].is_correction
