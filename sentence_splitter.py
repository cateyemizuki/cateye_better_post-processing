"""基于 MaiBot 1.4.5 规则的分句器，修正标点归属与配对引号。

派生自 Mai-with-u/MaiBot 的 src/chat/utils/utils.py，按 GPL-3.0-or-later 发布。
保留按长度概率合并、冒号和西文空格保护；标点调整仅作用于同一次输入。
"""

from __future__ import annotations

import random
import re
from typing import List, Tuple


_SOFT_SEPARATORS = {"，", ",", " ", "。", ";", "\n"}
_PUNCTUATION = set("，,。;；!?！？….")
_CLOSING = set("”’」』）)]}》】")
_NON_BODY = " \t\r\n，,。;；!?！？….”’」』）)]}》】"


def _english(char: str) -> bool:
    return "a" <= char.lower() <= "z"


def _has_body(text: str) -> bool:
    return bool(text.strip(_NON_BODY))


def _quote_mask(text: str) -> List[bool]:
    """保护配对引号、链接和行内代码；英文单词中的撇号不作为引号。"""
    pairs = {'"': '"', "'": "'", "“": "”", "‘": "’", "「": "」", "『": "』"}
    stack: List[Tuple[int, str]] = []
    edges = [0] * (len(text) + 1)
    for index, char in enumerate(text):
        if char in {"'", "’"} and 0 < index < len(text) - 1:
            if (_english(text[index - 1]) or text[index - 1].isdigit()) and (_english(text[index + 1]) or text[index + 1].isdigit()):
                continue
        if stack and char == stack[-1][1]:
            start, _ = stack.pop()
            edges[start] += 1
            edges[index + 1] -= 1
        elif char in pairs:
            stack.append((index, pairs[char]))
    for pattern in (r'https?://[^\s，。！？；「」『』“”<>]+', r'`[^`\n]+`'):
        for match in re.finditer(pattern, text):
            edges[match.start()] += 1
            edges[match.end()] -= 1
    depth = 0
    mask = []
    for index in range(len(text)):
        depth += edges[index]
        mask.append(depth > 0)
    return mask


def _append_to_previous(segments: List[Tuple[str, str]], punctuation: str, spacing: str = "") -> None:
    """同次输入中，换行/空格后的收尾标点归到前段，仍保留原有换行边界。"""
    text, separator = segments[-1]
    text += "".join(char for char in separator if not char.isspace())
    separator = "".join(char for char in separator if char.isspace())
    segments[-1] = (text + punctuation, separator + spacing)


def split_sentence_segments(text: str) -> List[Tuple[str, str]]:
    """产出正文和尾随分隔符；有语气含义的标点始终留在正文中。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n[ \t]*\n+", "\n", text)
    if len(text) < 3:
        return [(text, "")] if text else []
    mask = _quote_mask(text)
    segments: List[Tuple[str, str]] = []
    current = ""
    index = 0
    while index < len(text):
        char = text[index]
        if not mask[index] and char in _PUNCTUATION:
            end = index
            while end < len(text) and text[end] in _PUNCTUATION and not mask[end]:
                end += 1
            punctuation = text[index:end]
            terminal = any(mark in punctuation for mark in "…!?！？；") or "..." in punctuation
            if terminal:
                while end < len(text) and text[end] in _CLOSING and not mask[end]:
                    punctuation += text[end]
                    end += 1
                spacing_end = end
                while spacing_end < len(text) and text[spacing_end].isspace():
                    spacing_end += 1
                spacing = text[end:spacing_end]
                if _has_body(current):
                    segments.append((current + punctuation, spacing))
                    current = ""
                elif segments:
                    _append_to_previous(segments, current.strip() + punctuation, spacing)
                    current = ""
                else:
                    # 独立回复开头的省略号属于本次表达，不跨已经发出的消息移动。
                    current += punctuation + spacing
                index = spacing_end
                continue
        if not mask[index] and char in _CLOSING and not _has_body(current) and segments:
            _append_to_previous(segments, current.strip() + char)
            current = ""
            index += 1
            continue
        can_split = not mask[index] and char in _SOFT_SEPARATORS
        if can_split and char != "\n":
            before = text[index - 1] if index else ""
            after = text[index + 1] if index + 1 < len(text) else ""
            if before in {":", "："} or after in {":", "："}:
                can_split = False
            elif char == " " and before and after:
                if before in {"-", "—"} or after in {"-", "—"}:
                    can_split = False
                elif (before.isdigit() or _english(before)) and (after.isdigit() or _english(after)):
                    can_split = False
        if can_split and _has_body(current):
            segments.append((current, char))
            current = ""
        elif can_split and not current and segments:
            # 连续空格/回车归入前段分隔符，避免随机合并吞掉空段后丢失强制换行。
            previous, separator = segments[-1]
            segments[-1] = (previous, separator + char)
        else:
            current += char
        index += 1
    if current.strip():
        if not _has_body(current) and segments:
            _append_to_previous(segments, current.strip())
        else:
            segments.append((current, ""))
    if not segments:
        return [(text, "")] if text else []
    strength = 0.2 if len(text) < 12 else 0.6 if len(text) < 32 else 0.7
    merged: List[Tuple[str, str]] = []
    index = 0
    while index < len(segments):
        content, separator = segments[index]
        if index + 1 < len(segments) and content and "\n" not in separator and random.random() < 1.0 - strength:
            next_content, next_separator = segments[index + 1]
            merged.append((content + separator + next_content, next_separator))
            index += 2
        else:
            merged.append((content, separator))
            index += 1
    return [(normalized, separator) for content, separator in merged
            if (normalized := re.sub(r"[^\S\r\n]*[\r\n]+[^\S\r\n]*", " ", content).strip())]
