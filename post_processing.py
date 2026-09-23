"""MaiBot 回复后处理的忠实复刻（插件内自包含实现）。

本模块对 MaiBot 内置的 ``src/chat/utils/utils.py`` 的
``process_llm_response_segments`` 以及 ``src/chat/utils/typo_generator.py`` 的
``ChineseTypoGenerator`` 做逐行复刻，使插件能在框架“回复后处理总开关”关闭时，
以与 MaiBot 完全一致的后处理逻辑接管该功能。

来源与许可（重要）
------------------
本文件**派生自 MaiBot**（https://github.com/Mai-with-u/MaiBot ，GPL-3.0）的
``src/chat/utils/utils.py`` 与 ``src/chat/utils/typo_generator.py``：分段、颜文字
保护、长度/句数守卫、错字候选与概率等逻辑与上游逐行一致，其中相当一部分是**逐字
复制**（含正则字面量与中文行内注释）。因此本文件按上游同一协议 **GPL-3.0** 提供，
本插件整体以 **GPL-3.0-or-later** 发布（见仓库 ``LICENSE`` 与 ``README.md`` 的
「许可证」「致谢」两节）。**请勿**把本文件当作可单独按 MIT 使用的代码。

约束与说明
----------
* 插件运行在 MaiBot 的 Runner 子进程里，且禁止 ``import src.*``，因此这里把
  MaiBot 的原逻辑整体搬进插件，不依赖宿主的任何内部模块。
* 错别字生成依赖 ``jieba``（分词/词频）与 ``pypinyin``（拼音），二者是 MaiBot
  主进程 venv 的常规第三方依赖，插件（Runner 同 venv）可直接 import。
* 汉字频率表优先读取宿主 ``depends-data/char_frequency.json``（与 MaiBot 同一份，
  保证错字候选与原逻辑完全一致）；缺失时按原逻辑从 ``jieba`` 自带词典重建。

所有确定性逻辑（分段、颜文字保护、长度/句数守卫、错字候选与概率）均与 MaiBot
源码一致；唯一无法通过宿主 Hook 复刻的是“每条分段作为独立平台消息发送”——
``maisaka.reply.before_post_process`` 载荷只能返回单个字符串，因此接管后把各
分段以换行连接成一条消息交付，正文与换行结构与原逻辑一致。

与宿主**有意不同**的两处（都在下面显式标注，方便比对上游改动）：

1. 整词同音替换的组合数硬上限（``_MAX_HOMOPHONE_COMBINATIONS``）：宿主无条件穷举，
   长词会指数爆炸把线程占死；
2. **@提及保护**（``_AT_MENTION_PATTERN`` / ``ChineseTypoGenerator.protect_at_mentions``）：
   ``@昵称`` 里的昵称**不参与错字生成**。宿主会把昵称一起改错（实测：正文里的
   ``@凯特艾`` 被改成 ``@凯特爱``），配合 ``quote_takeover`` 注入的真实 at 组件，
   群里就变成“@凯特艾 @凯特爱”——像 bot 同时 @ 了两个人（0.11.3 线上事故）。
"""

from __future__ import annotations

import itertools
import json
import math
import os
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# 整词同音替换的候选组合数硬上限（**与宿主不同的一处**，见 ``_get_word_homophones``）。
# 宿主原逻辑用 ``itertools.product(*candidates)`` 穷举"每个字的所有同音字"的全部组合，
# 既没有上限也没有提前退出：组合数 = ∏(该字同音字个数)，随词长指数爆炸——实测
# 「中华人民共和国」约 11.5 亿、「社会主义现代化」约 5028 亿组合，一旦命中
# ``word_replace_rate`` 就会在 ``asyncio.to_thread`` 的工作线程里跑几分钟到几小时，
# 把线程池占死。这里加一道硬上限：超限就**跳过该词**（本次不做整词替换，逐字错字那
# 条路照常走）。现实里的常用词（≤4 字）组合数远低于上限，行为与宿主完全一致，
# 只有"宿主自己会卡死"的规模才会退化；退化次数累计在实例属性 ``_word_combo_skips``，
# 插件在「后处理接管统计」日志里以 ``整词替换跳过=N(累计)`` 输出，便于发现。
_MAX_HOMOPHONE_COMBINATIONS = 200_000

try:  # pragma: no cover - 取决于运行环境
    import jieba  # type: ignore
    from pypinyin import Style, pinyin  # type: ignore

    try:
        _ = jieba.cut("测试")
        _ = pinyin("测", style=Style.TONE3)
        DEPENDENCY_ERROR: Optional[str] = None
    except Exception as exc:  # pragma: no cover
        DEPENDENCY_ERROR = f"{type(exc).__name__}: {exc}"
except Exception as exc:  # pragma: no cover
    # jieba / pypinyin 缺失（或损坏）时只让「后处理接管」降级，不影响插件其它功能。
    jieba = None  # type: ignore[assignment]
    Style = None  # type: ignore[assignment]
    pinyin = None  # type: ignore[assignment]
    DEPENDENCY_ERROR = f"{type(exc).__name__}: {exc}"

# ---------------------------------------------------------------------------
# 惰性单例缓存（避免每次处理都重建高开销的拼音映射 / 字频 / jieba 词典）
# ---------------------------------------------------------------------------

_pinyin_dict_cache: Optional[Dict[str, List[str]]] = None
_char_frequency_cache: Optional[Dict[str, float]] = None
_jieba_word_freq_cache: Optional[Dict[str, float]] = None

_JIEBA_DICT_PATH = os.path.join(os.path.dirname(getattr(jieba, "__file__", "") or ""), "dict.txt")


def _is_chinese_char(char: str) -> bool:
    """判断字符是否为 CJK 汉字。"""
    try:
        return "\u4e00" <= char <= "\u9fff"
    except Exception:
        return False


def _load_pinyin_dict() -> Dict[str, List[str]]:
    """建立 拼音->同音汉字 映射（原 ``_create_pinyin_dict``，进程级缓存一次）。"""
    global _pinyin_dict_cache
    if _pinyin_dict_cache is not None:
        return _pinyin_dict_cache
    result: Dict[str, List[str]] = defaultdict(list)
    for char in (chr(i) for i in range(0x4E00, 0x9FFF)):
        try:
            py = pinyin(char, style=Style.TONE3)[0][0]
            result[py].append(char)
        except Exception:
            continue
    _pinyin_dict_cache = result
    return result


def _load_char_frequency(project_root: Optional[Path] = None) -> Dict[str, float]:
    """加载汉字频率表（优先宿主的 ``depends-data/char_frequency.json``）。

    与原逻辑 ``_load_or_create_char_frequency`` 一致：有缓存文件直接读；
    否则用 jieba 词典按字累加频率并归一化。
    """
    global _char_frequency_cache
    if _char_frequency_cache is not None:
        return _char_frequency_cache

    cache_file: Optional[Path] = None
    if project_root is not None:
        cache_file = project_root / "depends-data" / "char_frequency.json"

    if cache_file is not None and cache_file.exists():
        with cache_file.open("r", encoding="utf-8") as handle:
            _char_frequency_cache = json.load(handle)
        return _char_frequency_cache

    char_freq: Dict[str, int] = defaultdict(int)
    with open(_JIEBA_DICT_PATH, "r", encoding="utf-8") as handle:
        for line in handle:
            parts = line.strip().split()
            if len(parts) < 2:
                continue
            word, freq = parts[0], int(parts[1])
            for char in word:
                if _is_chinese_char(char):
                    char_freq[char] += freq

    max_freq = max(char_freq.values()) if char_freq else 1
    _char_frequency_cache = {char: freq / max_freq * 1000 for char, freq in char_freq.items()}
    return _char_frequency_cache


def _load_jieba_word_freq() -> Dict[str, float]:
    """读取 jieba 词典词频（进程级缓存一次，用于整词同音候选过滤）。"""
    global _jieba_word_freq_cache
    if _jieba_word_freq_cache is not None:
        return _jieba_word_freq_cache
    result: Dict[str, float] = {}
    with open(_JIEBA_DICT_PATH, "r", encoding="utf-8") as handle:
        for line in handle:
            parts = line.strip().split()
            if len(parts) >= 2:
                result[parts[0]] = float(parts[1])
    _jieba_word_freq_cache = result
    return result


# ---------------------------------------------------------------------------
# 回复分段结果
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessedResponseSegment:
    """回复后处理产生的单条消息及其引用提示。"""

    text: str
    quote_previous: bool = False


# ---------------------------------------------------------------------------
# @提及保护（**与宿主不同的一处**，见模块 docstring）
# ---------------------------------------------------------------------------

# ``@`` 后面允许作为"昵称"的字符：中日韩汉字（含扩展 A）、半/全角字母数字、下划线、
# 连字符、间隔号。**不含空白与标点**——所以 ``@凯特艾，`` 只会匹配到 ``@凯特艾``。
_AT_MENTION_PATTERN = re.compile(
    r"@[0-9A-Za-z_\-\u00b7\u3400-\u4dbf\u4e00-\u9fff\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]{1,32}"
)


def _protect_at_mentions(text: str) -> Tuple[str, Dict[str, str]]:
    """把句子里的 ``@昵称`` 换成不含汉字的占位符，返回 ``(替换后的句子, 占位符映射)``。

    为什么需要：错字生成器是"逐词逐字"改字的，``@凯特艾`` 对它来说只是普通文本，
    于是会产出 ``@凯特爱``（同音字）。配合 ``quote_takeover`` 注入的真实 at 组件，
    群里就出现两个不同的 @，看起来像 bot 同时 @ 了两个人。
    占位符全部由非汉字字符组成（``_is_chinese_char`` 判否），因此 jieba 怎么切都不会
    被改，交给 ``_restore_at_mentions`` 原样换回来。
    """
    if not text or "@" not in text:
        return text, {}
    mapping: Dict[str, str] = {}

    def _replace(match: Any) -> str:
        placeholder = f"__AT_MENTION_{len(mapping)}__"
        mapping[placeholder] = match.group(0)
        return placeholder

    return _AT_MENTION_PATTERN.sub(_replace, text), mapping


def _restore_at_mentions(text: str, mapping: Dict[str, str]) -> str:
    """把 ``_protect_at_mentions`` 的占位符还原成原文（``mapping`` 为空时原样返回）。"""
    if not mapping:
        return text
    for placeholder, mention in mapping.items():
        text = text.replace(placeholder, mention)
    return text


def match_leading_at_mention(text: str) -> Optional[str]:
    """``text`` 开头若是**字面** ``@昵称``（不是真实 at 组件），返回该提及原文；否则 ``None``。

    ``quote_takeover`` 用它判断"正文里 planner 已经自己 @ 过人了"，避免再注入一个真实
    at 组件导致一条消息里出现两个 @（0.11.3 线上事故）。两个模块共用同一条正则口径。
    """
    if not text:
        return None
    match = _AT_MENTION_PATTERN.match(text.lstrip())
    return match.group(0) if match else None


# ---------------------------------------------------------------------------
# 错别字生成器（复刻 src/chat/utils/typo_generator.py::ChineseTypoGenerator）
# ---------------------------------------------------------------------------


class ChineseTypoGenerator:
    """中文错别字生成器，与 MaiBot 原实现逐逻辑一致，仅缓存了高开销数据。"""

    def __init__(
        self,
        error_rate: float = 0.3,
        min_freq: int = 5,
        tone_error_rate: float = 0.2,
        word_replace_rate: float = 0.3,
        max_freq_diff: int = 200,
        project_root: Optional[Path] = None,
        protect_at_mentions: bool = True,
    ) -> None:
        self.error_rate = error_rate
        self.min_freq = min_freq
        self.tone_error_rate = tone_error_rate
        self.word_replace_rate = word_replace_rate
        self.max_freq_diff = max_freq_diff
        # **与宿主不同的一处**：``True``（默认）时 ``@昵称`` 不参与错字生成，见
        # ``_protect_at_mentions``。宿主没有这道保护，会把昵称一起改错。
        self.protect_at_mentions = bool(protect_at_mentions)

        self.pinyin_dict = _load_pinyin_dict()
        self.char_frequency = _load_char_frequency(project_root)
        # 因组合数超上限而跳过整词替换的**累计**次数（见 ``_MAX_HOMOPHONE_COMBINATIONS``）：
        # 只在这里记账，插件把它打进「后处理接管统计」日志。
        self._word_combo_skips = 0

    def _get_pinyin(self, sentence: str) -> List[Tuple[str, str]]:
        """将中文句子拆成单个汉字并获取其拼音。"""
        result: List[Tuple[str, str]] = []
        for char in list(sentence):
            if char.isspace() or not _is_chinese_char(char):
                continue
            py = pinyin(char, style=Style.TONE3)[0][0]
            result.append((char, py))
        return result

    @staticmethod
    def _get_similar_tone_pinyin(py: str) -> str:
        """获取相似声调的拼音。"""
        if not py or len(py) < 1:
            return py
        if not py[-1].isdigit():
            return f"{py}1"
        base = py[:-1]
        tone = int(py[-1])
        if tone not in [1, 2, 3, 4]:
            return base + str(random.choice([1, 2, 3, 4]))
        possible_tones = [1, 2, 3, 4]
        possible_tones.remove(tone)
        new_tone = random.choice(possible_tones)
        return base + str(new_tone)

    def _calculate_replacement_probability(self, orig_freq: float, target_freq: float) -> float:
        """根据频率差计算替换概率。"""
        if target_freq > orig_freq:
            return 1.0
        freq_diff = orig_freq - target_freq
        if freq_diff > self.max_freq_diff:
            return 0.0
        return math.exp(-3 * freq_diff / self.max_freq_diff)

    def _get_similar_frequency_chars(self, char: str, py: str, num_candidates: int = 5) -> Optional[List[str]]:
        """获取与给定字频率相近的同音字，可能包含声调错误。"""
        homophones: List[str] = []
        if random.random() < self.tone_error_rate:
            wrong_tone_py = self._get_similar_tone_pinyin(py)
            homophones.extend(self.pinyin_dict.get(wrong_tone_py, []))
        homophones.extend(self.pinyin_dict.get(py, []))

        if not homophones:
            return None

        orig_freq = self.char_frequency.get(char, 0)
        freq_diff = [
            (h, self.char_frequency.get(h, 0))
            for h in homophones
            if h != char and self.char_frequency.get(h, 0) >= self.min_freq
        ]
        if not freq_diff:
            return None

        candidates_with_prob = []
        for h, freq in freq_diff:
            prob = self._calculate_replacement_probability(orig_freq, freq)
            if prob > 0:
                candidates_with_prob.append((h, prob))

        if not candidates_with_prob:
            return None

        candidates_with_prob.sort(key=lambda x: x[1], reverse=True)
        return [candidate for candidate, _ in candidates_with_prob[:num_candidates]]

    @staticmethod
    def _get_word_pinyin(word: str) -> List[str]:
        """获取词语的拼音列表。"""
        return [py[0] for py in pinyin(word, style=Style.TONE3)]

    @staticmethod
    def _segment_sentence(sentence: str) -> List[str]:
        """使用 jieba 分词。"""
        return list(jieba.cut(sentence))

    def _get_word_homophones(self, word: str) -> List[str]:
        """获取整词的同音词，只返回高频的有意义词语。

        **与宿主唯一的差异**：宿主在这里无条件 ``itertools.product(*candidates)`` 穷举，
        长词会指数爆炸（详见 ``_MAX_HOMOPHONE_COMBINATIONS`` 的说明）。这里先算组合数，
        超过上限就直接返回空列表（跳过整词替换），并在 ``self._word_combo_skips`` 记账。
        """
        if len(word) == 1:
            return []

        word_pinyin = self._get_word_pinyin(word)
        candidates: List[List[str]] = []
        for py in word_pinyin:
            chars = self.pinyin_dict.get(py, [])
            if not chars:
                return []
            candidates.append(chars)

        combination_count = 1
        for chars in candidates:
            combination_count *= len(chars)
            if combination_count > _MAX_HOMOPHONE_COMBINATIONS:
                self._word_combo_skips += 1
                return []

        all_combinations = itertools.product(*candidates)
        valid_words = _load_jieba_word_freq()

        original_word_freq = valid_words.get(word, 0)
        min_word_freq = original_word_freq * 0.1

        homophones = []
        for combo in all_combinations:
            new_word = "".join(combo)
            if new_word == word or new_word not in valid_words:
                continue
            new_word_freq = valid_words[new_word]
            if new_word_freq < min_word_freq:
                continue
            char_avg_freq = sum(self.char_frequency.get(c, 0) for c in new_word) / len(new_word)
            combined_score = new_word_freq * 0.7 + char_avg_freq * 0.3
            if combined_score >= self.min_freq:
                homophones.append((new_word, combined_score))

        sorted_homophones = sorted(homophones, key=lambda x: x[1], reverse=True)
        return [w for w, _ in sorted_homophones[:5]]

    def create_typo_sentence(
        self, sentence: str, *, force_suggestion: bool = False
    ) -> Tuple[str, Optional[str]]:
        """创建含同音字错误的句子，返回 (错字句, 纠正建议)。

        ``force_suggestion=True``：**跳过宿主那句硬编码的「50% 才给纠正建议」门控**——
        只要句子真的被改动过就一定给出纠正建议。插件接管分支用它保证"每处错字都能被
        权重池判一次"，否则会出现"打了错字却既不纠正也不撤回"：线上 02:32:47 的实测
        现象就是 `抽到纠正建议=0 句、真出现错字=1 句`，于是"撤回重发"永远不会触发
        （宿主原逻辑里没有纠正建议就没有任何纠正动作）。
        宿主复刻路径（``PostProcessor.process``）不传该参数，行为与宿主逐字一致。
        """
        source = sentence
        mention_mapping: Dict[str, str] = {}
        if self.protect_at_mentions:
            source, mention_mapping = _protect_at_mentions(sentence)
        result: List[str] = []
        word_typos: List[Tuple[str, str]] = []
        char_typos: List[Tuple[str, str]] = []
        current_pos = 0

        words = self._segment_sentence(source)

        for word in words:
            if all(not _is_chinese_char(c) for c in word):
                result.append(word)
                current_pos += len(word)
                continue

            word_pinyin = self._get_word_pinyin(word)

            if len(word) > 1 and random.random() < self.word_replace_rate:
                word_homophones = self._get_word_homophones(word)
                if word_homophones:
                    typo_word = random.choice(word_homophones)
                    result.append(typo_word)
                    word_typos.append((typo_word, word))
                    current_pos += len(typo_word)
                    continue

            if len(word) == 1:
                char = word
                py = word_pinyin[0]
                if random.random() < self.error_rate:
                    similar_chars = self._get_similar_frequency_chars(char, py)
                    if similar_chars:
                        typo_char = random.choice(similar_chars)
                        typo_freq = self.char_frequency.get(typo_char, 0)
                        orig_freq = self.char_frequency.get(char, 0)
                        replace_prob = self._calculate_replacement_probability(orig_freq, typo_freq)
                        if random.random() < replace_prob:
                            result.append(typo_char)
                            char_typos.append((typo_char, char))
                            current_pos += 1
                            continue
                result.append(char)
                current_pos += 1
            else:
                word_result: List[str] = []
                for char, py in zip(word, word_pinyin, strict=False):
                    word_error_rate = self.error_rate * (0.7 ** (len(word) - 1))
                    if random.random() < word_error_rate:
                        similar_chars = self._get_similar_frequency_chars(char, py)
                        if similar_chars:
                            typo_char = random.choice(similar_chars)
                            typo_freq = self.char_frequency.get(typo_char, 0)
                            orig_freq = self.char_frequency.get(char, 0)
                            replace_prob = self._calculate_replacement_probability(orig_freq, typo_freq)
                            if random.random() < replace_prob:
                                word_result.append(typo_char)
                                char_typos.append((typo_char, char))
                                continue
                    word_result.append(char)
                result.append("".join(word_result))
                current_pos += len(word)

        correction_suggestion: Optional[str] = None
        # 宿主这里是 ``if random.random() < 0.5:``；``force_suggestion`` 时**连随机数都不抽**
        # （接管分支要求"有错字就一定有纠正建议"，见方法 docstring）。
        if force_suggestion or random.random() < 0.5:
            if word_typos:
                _, correct_word = random.choice(word_typos)
                correction_suggestion = correct_word
            elif char_typos:
                _, correct_char = random.choice(char_typos)
                correction_suggestion = correct_char

        return _restore_at_mentions("".join(result), mention_mapping), correction_suggestion


# ---------------------------------------------------------------------------
# 文本后处理（复刻 src/chat/utils/utils.py）
# ---------------------------------------------------------------------------


def is_english_letter(char: str) -> bool:
    """检查字符是否为英文字母（忽略大小写）。"""
    return "a" <= char.lower() <= "z"


def protect_kaomoji(sentence: str) -> Tuple[str, Dict[str, str]]:
    """识别并保护句子中的颜文字，替换为占位符，返回占位符映射。"""
    kaomoji_pattern = __import__("re").compile(
        r"("
        r"[(\[（【]"  # 左括号
        r"[^()\[\]（）【】]*?"  # 非括号字符（惰性匹配）
        r"[^一-龥a-zA-Z0-9\s]"  # 非中文、非英文、非数字、非空格字符（必须包含至少一个）
        r"[^()\[\]（）【】]*?"  # 非括号字符（惰性匹配）
        r"[)\]）】"  # 右括号
        r"]"
        r")"
        r"|"
        r"([▼▽・ᴥω･﹏^><≧≦￣｀´∀ヮДд︿﹀へ｡ﾟ╥╯╰︶︹•⁄]{2,15})"
    )

    kaomoji_matches = kaomoji_pattern.findall(sentence)
    placeholder_to_kaomoji: Dict[str, str] = {}
    for match in kaomoji_matches:
        kaomoji = match[0] or match[1]
        if kaomoji.startswith("[表情包") and kaomoji.endswith("]"):
            continue
        idx = len(placeholder_to_kaomoji)
        placeholder = f"__KAOMOJI_{idx}__"
        sentence = sentence.replace(kaomoji, placeholder, 1)
        placeholder_to_kaomoji[placeholder] = kaomoji

    return sentence, placeholder_to_kaomoji


def recover_kaomoji(sentences: Sequence[str], placeholder_to_kaomoji: Dict[str, str]) -> List[str]:
    """根据占位符映射恢复句子中的颜文字。"""
    recovered: List[str] = []
    for sentence in sentences:
        for placeholder, kaomoji in placeholder_to_kaomoji.items():
            sentence = sentence.replace(placeholder, kaomoji)
        recovered.append(sentence)
    return recovered


def get_western_ratio(paragraph: str) -> float:
    """计算段落中字母数字字符的西文比例。"""
    alnum_chars = [char for char in paragraph if char.isalnum()]
    if not alnum_chars:
        return 0.0
    western_count = sum(bool(is_english_letter(char)) for char in alnum_chars)
    return western_count / len(alnum_chars)


def split_into_sentences_w_remove_punctuation(text: str) -> List[str]:
    """将文本分割成句子，并根据概率合并（复刻原逻辑）。"""
    import re

    # 预处理：处理多余的换行符
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = re.sub(r"\n\s*([，,。;\s])", r"\n\1", text)
    text = re.sub(r"([，,。;\s])\s*\n", r"\1\n", text)

    len_text = len(text)
    if len_text < 3:
        return list(text) if random.random() < 0.01 else [text]

    quote_chars = {'"', "'", "“", "”", "‘", "’", "「", "」", "『", "』"}
    inside_quote = [False] * len_text
    in_quote = False
    current_quote_char = ""
    for idx, ch in enumerate(text):
        if ch in quote_chars:
            if not in_quote:
                in_quote = True
                current_quote_char = ch
                inside_quote[idx] = False
            else:
                if ch == current_quote_char or ch in {'"', "'"} and current_quote_char in {'"', "'"}:
                    in_quote = False
                    current_quote_char = ""
                inside_quote[idx] = False
        else:
            inside_quote[idx] = in_quote

    separators = {"，", ",", " ", "。", ";", "\n"}
    segments: List[Tuple[str, str]] = []
    current_segment = ""

    i = 0
    while i < len(text):
        char = text[i]
        if char in separators:
            if inside_quote[i]:
                can_split = False
            else:
                if char == "\n":
                    can_split = True
                else:
                    can_split = True
                    if i > 0:
                        prev_char = text[i - 1]
                        if prev_char in {":", "："}:
                            can_split = False
                    if i < len(text) - 1:
                        next_char = text[i + 1]
                        if next_char in {":", "："}:
                            can_split = False
                    if can_split and char == " " and i > 0 and i < len(text) - 1:
                        prev_char = text[i - 1]
                        next_char = text[i + 1]
                        dash_chars = {"-", "—"}
                        if prev_char in dash_chars or next_char in dash_chars:
                            can_split = False
                        else:
                            prev_is_alnum = prev_char.isdigit() or is_english_letter(prev_char)
                            next_is_alnum = next_char.isdigit() or is_english_letter(next_char)
                            if prev_is_alnum and next_is_alnum:
                                can_split = False
            if can_split:
                if current_segment:
                    segments.append((current_segment, char))
                elif char in {" ", "\n"}:
                    segments.append(("", char))
                current_segment = ""
            else:
                current_segment += char
        else:
            current_segment += char
        i += 1

    if current_segment:
        segments.append((current_segment, ""))

    segments = [(content, sep) for content, sep in segments if content or sep]
    if not segments:
        return [text] if text else []

    if len_text < 12:
        split_strength = 0.2
    elif len_text < 32:
        split_strength = 0.6
    else:
        split_strength = 0.7
    merge_probability = 1.0 - split_strength

    merged_segments: List[Tuple[str, str]] = []
    idx = 0
    while idx < len(segments):
        current_content, current_sep = segments[idx]
        if (
            idx + 1 < len(segments)
            and current_content
            and current_sep != "\n"
            and random.random() < merge_probability
        ):
            next_content, next_sep = segments[idx + 1]
            if next_content:
                merged_segments.append((current_content + current_sep + next_content, next_sep))
            else:
                merged_segments.append((current_content, next_sep))
            idx += 2
        else:
            merged_segments.append((current_content, current_sep))
            idx += 1

    final_sentences = [content for content, _ in merged_segments if content]
    final_sentences = [s for s in final_sentences if s.strip()]
    final_sentences = [
        normalized
        for sentence in final_sentences
        if (normalized := re.sub(r"[^\S\r\n]*[\r\n]+[^\S\r\n]*", " ", sentence).strip())
    ]
    return final_sentences


def _count_cjk(text: str) -> int:
    """统计字符串里的汉字个数（``\\u4e00``-``\\u9fff``，不含标点/数字/英文）。"""
    return sum(1 for char in str(text or "") if "\u4e00" <= char <= "\u9fff")


def _merge_groups(
    segments: List[ProcessedResponseSegment],
    max_count: int,
) -> Tuple[List[ProcessedResponseSegment], List[int]]:
    """``_merge_processed_segments_to_max_count`` 的实现体，额外返回每组起始下标。

    起始下标供新分支把"错字纠正动作"从合并前的分段下标重映射到合并后的下标。
    """
    if len(segments) <= max_count:
        return list(segments), list(range(len(segments)))
    if max_count <= 0:
        return [], []

    segment_count = len(segments)
    required_starts = [index for index, segment in enumerate(segments) if index > 0 and segment.quote_previous]
    group_starts = {0, *required_starts[: max_count - 1]}

    evenly_spaced_starts: List[int] = []
    start_index = 0
    for group_index in range(max_count):
        remaining_segments = segment_count - start_index
        remaining_groups = max_count - group_index
        group_size = (remaining_segments + remaining_groups - 1) // remaining_groups
        evenly_spaced_starts.append(start_index)
        start_index += group_size

    for candidate_start in evenly_spaced_starts:
        if len(group_starts) >= max_count:
            break
        group_starts.add(candidate_start)

    sorted_starts = sorted(group_starts)
    merged_segments: List[ProcessedResponseSegment] = []
    for group_index, group_start in enumerate(sorted_starts):
        group_end = sorted_starts[group_index + 1] if group_index + 1 < len(sorted_starts) else segment_count
        group = segments[group_start:group_end]
        merged_segments.append(
            ProcessedResponseSegment(
                text="".join(segment.text for segment in group),
                quote_previous=group[0].quote_previous,
            )
        )
    return merged_segments, sorted_starts


def _merge_processed_segments_to_max_count(
    segments: List[ProcessedResponseSegment],
    max_count: int,
) -> List[ProcessedResponseSegment]:
    """压缩回复段数量，并优先让需要引用的纠正内容保持在消息开头（宿主复刻路径用）。"""
    return _merge_groups(segments, max_count)[0]


@dataclass(frozen=True)
class CorrectionDecision:
    """某个分段的错字纠正决定（新增分支用，不参与宿主复刻路径）。"""

    segment_index: int
    mode: str  # "direct" | "quote" | "recall"（"最后纠正"只改时机，不再是单独取值）
    text: str  # direct/quote：纠正建议（与宿主一致，通常是一个字/词）
    sentence: str = ""  # recall：修正后的整句（撤回后必须重发完整内容）


@dataclass(frozen=True)
class ResponsePlan:
    """自定义纠正分支的分段计划：分段方式与宿主一致，纠正方式由插件决定。"""

    segments: List[ProcessedResponseSegment]
    corrections: Dict[int, CorrectionDecision]  # 紧跟该段之后执行（direct/quote/recall）
    deferred: Tuple[CorrectionDecision, ...]  # 全部段发完后执行（last）


class PostProcessor:
    """MaiBot 回复后处理器的自包含复刻，向插件暴露 ``process`` 与 ``plan`` 两个接口。"""

    def __init__(
        self,
        *,
        project_root: Optional[Path] = None,
        enable_splitter_switch: bool = True,
        enable_chinese_typo_switch: bool = True,
        splitter_enable: bool = True,
        splitter_max_length: int = 512,
        splitter_max_sentence_num: int = 8,
        splitter_max_split_num: int = 3,
        splitter_enable_kaomoji_protection: bool = False,
        splitter_enable_overflow_return_all: bool = False,
        typo_enable: bool = True,
        typo_enable_correction_quote: bool = True,
        typo_correction_quote_probability: float = 1.0,
        typo_error_rate: float = 0.01,
        typo_min_freq: int = 9,
        typo_tone_error_rate: float = 0.1,
        typo_word_replace_rate: float = 0.006,
        bot_nickname: str = "麦麦",
        typo_protect_at_mentions: bool = True,
    ) -> None:
        if DEPENDENCY_ERROR is not None:
            raise RuntimeError(
                "后处理接管依赖 jieba / pypinyin，当前环境不可用（"
                f"{DEPENDENCY_ERROR}）；请在 MaiBot 的 venv 中安装：pip install jieba pypinyin"
            )
        self.project_root = project_root
        self.enable_splitter_switch = enable_splitter_switch
        self.enable_chinese_typo_switch = enable_chinese_typo_switch
        self.splitter_enable = splitter_enable
        self.splitter_max_length = int(splitter_max_length)
        self.splitter_max_sentence_num = int(splitter_max_sentence_num)
        self.splitter_max_split_num = int(splitter_max_split_num)
        self.splitter_enable_kaomoji_protection = bool(splitter_enable_kaomoji_protection)
        self.splitter_enable_overflow_return_all = bool(splitter_enable_overflow_return_all)
        self.typo_enable = typo_enable
        self.typo_enable_correction_quote = typo_enable_correction_quote
        self.typo_correction_quote_probability = float(typo_correction_quote_probability)
        self.typo_error_rate = float(typo_error_rate)
        self.typo_min_freq = int(typo_min_freq)
        self.typo_tone_error_rate = float(typo_tone_error_rate)
        self.typo_word_replace_rate = float(typo_word_replace_rate)
        self.bot_nickname = (bot_nickname or "麦麦").strip()
        # **与宿主不同的一处**：``@昵称`` 不参与错字生成（见 ``_protect_at_mentions``）。
        self.typo_protect_at_mentions = bool(typo_protect_at_mentions)

        self._typo_generator: Optional[ChineseTypoGenerator] = None
        # 最近一次 ``process`` / ``plan`` 的统计（插件用来打诊断日志：
        # 「抽了几句、其中几句真出现错字、抽到几条纠正建议、最后几段」）。
        self.last_stats: Dict[str, Any] = {}

    def warmup(self) -> None:
        """预加载高开销的拼音/字频数据，避免首次接管回复时卡顿。"""
        _load_pinyin_dict()
        _load_char_frequency(self.project_root)
        _load_jieba_word_freq()

    def _get_typo_generator(self) -> ChineseTypoGenerator:
        if self._typo_generator is None:
            self._typo_generator = ChineseTypoGenerator(
                error_rate=self.typo_error_rate,
                min_freq=self.typo_min_freq,
                tone_error_rate=self.typo_tone_error_rate,
                word_replace_rate=self.typo_word_replace_rate,
                project_root=self.project_root,
                protect_at_mentions=self.typo_protect_at_mentions,
            )
        return self._typo_generator

    def _get_random_default_reply(self) -> str:
        default_replies = [
            f"{self.bot_nickname}不知道哦",
            f"{self.bot_nickname}不知道",
            "不知道哦",
            "不知道",
            "不晓得",
            "懒得说",
            "()",
        ]
        return random.choice(default_replies)

    def process(
        self,
        text: str,
        *,
        enable_splitter: bool = True,
        enable_chinese_typo: bool = True,
    ) -> List[ProcessedResponseSegment]:
        """复刻 ``process_llm_response_segments``（跳过宿主总开关门控，主开关由插件判定）。

        返回复刻后的分段列表；每条 ``ProcessedResponseSegment.text`` 与 MaiBot 完全一致，
        ``quote_previous`` 标记也保留。

        **勿改这里的语句顺序与随机数调用顺序**：它是逐行复刻，RNG 序也必须一致
        （差分测试 `test_better_post_processing_*` 会比对同种子下的输出）。
        """
        cleaned_text, kaomoji_mapping, early = self._prepare_text(text)
        if early is not None:
            self.last_stats = {
                "early": True,
                "sentences": 0,
                "typo_sentences": 0,
                "suggestions": 0,
                "segments": len(early),
            }
            return early

        typo_generator = self._get_typo_generator()
        split_sentences = self._split_sentences(cleaned_text, enable_splitter)

        segments: List[ProcessedResponseSegment] = []
        typo_sentences = 0
        suggestions = 0
        for sentence in split_sentences:
            if self.typo_enable and enable_chinese_typo:
                typoed_text, typo_corrections = typo_generator.create_typo_sentence(sentence)
                if typoed_text != sentence:
                    typo_sentences += 1
                if typo_corrections:
                    suggestions += 1
                if typo_corrections:
                    if random.random() < 0.5:
                        quote_previous = (
                            self.typo_enable_correction_quote
                            and random.random() < self.typo_correction_quote_probability
                        )
                        segments.append(ProcessedResponseSegment(typoed_text))
                        segments.append(ProcessedResponseSegment(typo_corrections, quote_previous=quote_previous))
                    else:
                        segments.append(ProcessedResponseSegment(sentence))
                else:
                    segments.append(ProcessedResponseSegment(typoed_text))
            else:
                segments.append(ProcessedResponseSegment(sentence))

        finalized = self._finalize(segments, cleaned_text, kaomoji_mapping)
        if finalized is None:
            self.last_stats = {
                "early": True,
                "sentences": len(split_sentences),
                "typo_sentences": typo_sentences,
                "suggestions": suggestions,
                "segments": 1,
                "overflow": True,
            }
            return [ProcessedResponseSegment(self._get_random_default_reply())]
        self.last_stats = {
            "early": False,
            "sentences": len(split_sentences),
            "typo_sentences": typo_sentences,
            "suggestions": suggestions,
            "segments": len(finalized[0]),
        }
        return finalized[0]

    def plan(
        self,
        text: str,
        *,
        enable_splitter: bool = True,
        enable_chinese_typo: bool = True,
        choose_mode: Optional[Callable[[int, str], Optional[str]]] = None,
        should_defer: Optional[Callable[[int], bool]] = None,
        visible_probability: float = 1.0,
        max_correction_cjk: int = 0,
    ) -> ResponsePlan:
        """接管分支：分段与宿主一致，**错字纠正方式由 ``choose_mode`` 决定**。

        ``choose_mode(segment_index, suggestion)`` 返回 ``"direct"`` / ``"quote"`` /
        ``"recall"``，其余（含 ``None``）视为**不纠正**——错字留在消息里。

        **本分支不再使用宿主那句"50% 才给纠正建议"的门控**（``create_typo_sentence``
        以 ``force_suggestion=True`` 调用）：只要错字真的出现，就一定会抽一次纠正方式。
        宿主原逻辑里"没抽到纠正建议"就等于"没有任何纠正动作"，会出现"打了错字却既不
        纠正也不撤回"（0.11.3 线上实测），与"纠正方式由权重池决定"的语义矛盾。

        ``should_defer(segment_index)`` 为真时，该处纠正进 ``deferred``（"最后纠正"：
        动作推迟到本轮全部段发完之后执行），**纠正方式不变**——所以"最后纠正"也可以是
        直接补发 / 引用纠正 / 撤回重发；为假（或未传）时紧跟该段之后执行（``corrections``）。

        ``visible_probability``：宿主那句硬编码的 0.5 门控——错字出现时，以该概率
        **发送带错字的消息**（随后按 ``choose_mode`` 纠正），否则**整句替换为正确句**
        （等于没打错）。取 0.5 即与宿主同频；取 1.0 表示错字一律可见。

        ``max_correction_cjk``：分段汉字数**超过**该值时该处错字**不纠正**（错字留着），
        用于"长消息不乱补"；``0`` = 不限制。
        """
        cleaned_text, kaomoji_mapping, early = self._prepare_text(text)
        if early is not None:
            self.last_stats = {
                "early": True,
                "sentences": 0,
                "typo_sentences": 0,
                "suggestions": 0,
                "segments": len(early),
                "corrections": 0,
            }
            return ResponsePlan(segments=early, corrections={}, deferred=())

        typo_generator = self._get_typo_generator()
        split_sentences = self._split_sentences(cleaned_text, enable_splitter)

        segments: List[ProcessedResponseSegment] = []
        corrections: Dict[int, CorrectionDecision] = {}
        deferred: List[CorrectionDecision] = []
        typo_sentences = 0
        suggestions = 0
        for sentence in split_sentences:
            if not (self.typo_enable and enable_chinese_typo):
                segments.append(ProcessedResponseSegment(sentence))
                continue
            # force_suggestion=True：跳过宿主"50% 才给纠正建议"的门控，见方法 docstring。
            typoed_text, suggestion = typo_generator.create_typo_sentence(
                sentence, force_suggestion=True
            )
            if typoed_text != sentence:
                typo_sentences += 1
            if suggestion:
                suggestions += 1
            index = len(segments)
            if not suggestion or typoed_text == sentence:
                # 没抽到纠正建议、或整句其实没被改动：错字句照发，不产生纠正动作。
                segments.append(ProcessedResponseSegment(typoed_text))
                continue
            if visible_probability < 1.0 and random.random() >= visible_probability:
                # 宿主的另一半分支：整句替换成正确句（错字不出现）。
                segments.append(ProcessedResponseSegment(sentence))
                continue
            segments.append(ProcessedResponseSegment(typoed_text))
            if max_correction_cjk > 0 and _count_cjk(sentence) > max_correction_cjk:
                # 超限：错字可见但不纠正（也不消耗纠正方式抽取）。
                continue
            mode = str(choose_mode(index, suggestion) or "none") if choose_mode else "none"
            if mode not in {"direct", "quote", "recall"}:
                continue
            decision = CorrectionDecision(
                segment_index=index,
                mode=mode,
                text=suggestion,
                sentence=sentence,
            )
            if should_defer is not None and should_defer(index):
                deferred.append(decision)
            else:
                corrections[index] = decision

        finalized = self._finalize(segments, cleaned_text, kaomoji_mapping)
        if finalized is None:
            self.last_stats = {
                "early": True,
                "sentences": len(split_sentences),
                "typo_sentences": typo_sentences,
                "suggestions": suggestions,
                "segments": 1,
                "overflow": True,
                "corrections": 0,
            }
            return ResponsePlan(
                segments=[ProcessedResponseSegment(self._get_random_default_reply())],
                corrections={},
                deferred=(),
            )
        merged, starts, overflow_collapsed = finalized
        if overflow_collapsed:
            # 句数超限且配置为"保留全文"：整段文本被并成一条，段边界不复存在，
            # 纠正动作无处安放 → 一律放弃（错字留在消息里）。
            self.last_stats = {
                "early": False,
                "sentences": len(split_sentences),
                "typo_sentences": typo_sentences,
                "suggestions": suggestions,
                "segments": len(merged),
                "overflow": True,
                "corrections": 0,
            }
            return ResponsePlan(segments=merged, corrections={}, deferred=())

        old_to_new: Dict[int, int] = {}
        for new_index, start in enumerate(starts):
            end = starts[new_index + 1] if new_index + 1 < len(starts) else len(segments)
            for old_index in range(start, end):
                old_to_new[old_index] = new_index

        remapped: Dict[int, CorrectionDecision] = {}
        for old_index, decision in corrections.items():
            new_index = old_to_new.get(old_index)
            if new_index is None:
                continue
            remapped[new_index] = CorrectionDecision(
                segment_index=new_index,
                mode=decision.mode,
                text=decision.text,
                sentence=decision.sentence,
            )
        remapped_deferred = tuple(
            CorrectionDecision(
                segment_index=old_to_new.get(decision.segment_index, decision.segment_index),
                mode=decision.mode,
                text=decision.text,
                sentence=decision.sentence,
            )
            for decision in deferred
        )
        self.last_stats = {
            "early": False,
            "sentences": len(split_sentences),
            "typo_sentences": typo_sentences,
            "suggestions": suggestions,
            "segments": len(merged),
            "corrections": len(remapped) + len(remapped_deferred),
        }
        return ResponsePlan(segments=merged, corrections=remapped, deferred=remapped_deferred)

    # ------------------------------------------------------------------
    # 共用步骤（process / plan 都走这里；调用顺序与随机数顺序不得改变）
    # ------------------------------------------------------------------

    def _prepare_text(
        self, text: str
    ) -> Tuple[str, Dict[str, str], Optional[List[ProcessedResponseSegment]]]:
        """颜文字保护 → 括号内容剔除 → 两道守卫；命中守卫时返回早退结果。"""
        import re

        # 保护颜文字
        if self.splitter_enable_kaomoji_protection:
            protected_text, kaomoji_mapping = protect_kaomoji(text)
        else:
            protected_text = text
            kaomoji_mapping = {}

        pattern = re.compile(r"[(\[（](?=.*[一-鿿]).*?[)\]）]")
        cleaned_text = pattern.sub("", protected_text)

        if cleaned_text == "":
            return cleaned_text, kaomoji_mapping, [ProcessedResponseSegment("呃呃")]

        max_length = self.splitter_max_length * 2
        if get_western_ratio(cleaned_text) < 0.1 and len(cleaned_text) > max_length:
            return cleaned_text, kaomoji_mapping, [ProcessedResponseSegment(self._get_random_default_reply())]

        return cleaned_text, kaomoji_mapping, None

    def _split_sentences(self, cleaned_text: str, enable_splitter: bool) -> List[str]:
        """按宿主配置决定是否分段。"""
        if self.splitter_enable and enable_splitter:
            return split_into_sentences_w_remove_punctuation(cleaned_text)
        return [cleaned_text]

    def _finalize(
        self,
        segments: List[ProcessedResponseSegment],
        cleaned_text: str,
        kaomoji_mapping: Dict[str, str],
    ) -> Optional[Tuple[List[ProcessedResponseSegment], List[int], bool]]:
        """句数守卫 → 合并 → 颜文字恢复。

        返回 ``(合并后的分段, 每组的起始下标, 是否因"超限保留全文"被压成一段)``；
        句数超限且未开启"保留全文"时返回 ``None``（调用方回默认回复）。
        """
        overflow_collapsed = False
        if len(segments) > self.splitter_max_sentence_num:
            if self.splitter_enable_overflow_return_all:
                segments = [ProcessedResponseSegment(cleaned_text)]
                overflow_collapsed = True
            else:
                return None

        merged, starts = _merge_groups(segments, self.splitter_max_split_num)

        if self.splitter_enable_kaomoji_protection:
            recovered_sentences = recover_kaomoji([segment.text for segment in merged], kaomoji_mapping)
            merged = [
                ProcessedResponseSegment(
                    text=recovered_text,
                    quote_previous=segment.quote_previous,
                )
                for segment, recovered_text in zip(merged, recovered_sentences, strict=True)
            ]

        return merged, starts, overflow_collapsed
