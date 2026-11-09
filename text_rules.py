"""出站文本替换/过滤规则的解析与应用（纯逻辑，不依赖 SDK，可离线单测）。

规则以字符串形式写在插件配置里，**每行一条**::

    r:傻逼/*filter*          —— 把消息文本中的 ``傻逼`` 全部替换为 ``*filter*``
    r:傻逼                   —— 过滤该词（等价于把 ``傻逼`` 替换为空串）
    c:The prompt could not be submitted.
                             —— 消息包含这句话时**整条不发送**
    c:草/你好                —— 消息包含 ``草`` 时，整条文本替换为 ``你好``

语法要点：

* ``r:`` = 命中词替换；``c:`` = 整条消息替换/过滤；
* 关键词与替换文本之间用**第一个未转义的** ``/`` 分隔；没有 ``/`` 或 ``/`` 后为空时，
  ``r`` 规则表示过滤该词、``c`` 规则表示整条消息过滤（不发送）；
* ``#`` 开头的行与空行忽略；
* 关键词/替换文本里的 ``/`` 写成 ``\\/``，反斜杠写成 ``\\\\``。

应用顺序：先依次应用全部 ``r`` 规则（对当前文本做全量替换），再按配置顺序检查
``c`` 规则，第一条命中的 ``c`` 直接决定整条文本（替换为给定内容，或整条丢弃）。
旧格式 ``"词"replace"替换词"`` / ``"词"cover"覆盖文本"`` 以及旧界面提示的
``replace=词=>替换词`` / ``cover=词=>覆盖文本`` 仍然兼容。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

# 新格式：r:... / c:...
_NEW_RULE_PATTERN = re.compile(r"^([rRcC]):(.*)$")

# 旧格式 1："关键词"(replace|cover)"内容"。关键词不允许为空，也不允许包含双引号。
_LEGACY_QUOTED_PATTERN = re.compile(r'^\s*"([^"]+)"(replace|cover)"([^"]*)"\s*$')

# 旧格式 2（旧版 WebUI 提示）：replace=关键词=>替换词 / cover=关键词=>覆盖文本。
_LEGACY_ARROW_PATTERN = re.compile(r"^(replace|cover)\s*=\s*(.*?)\s*=>\s*(.*)$", re.DOTALL)

_IGNORED = object()
_INVALID = object()


@dataclass(frozen=True)
class ReplaceRule:
    """把消息中的 ``keyword`` 全量替换为 ``replacement``（空串 = 过滤该词）。"""

    keyword: str
    replacement: str

    @property
    def is_filter(self) -> bool:
        """替换词为空：只删除命中词（过滤该词）。"""
        return not self.replacement


@dataclass(frozen=True)
class CoverRule:
    """消息文本包含 ``keyword`` 时，整条文本覆盖为 ``content``；空串 = 整条过滤。"""

    keyword: str
    content: str

    @property
    def is_filter(self) -> bool:
        """覆盖内容为空：包含关键词的整条消息不发送。"""
        return not self.content


@dataclass(frozen=True)
class ParsedRules:
    """一批规则字符串的解析结果。"""

    replaces: Tuple[ReplaceRule, ...] = ()
    covers: Tuple[CoverRule, ...] = ()
    invalid: Tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.replaces or self.covers)

    @property
    def filter_count(self) -> int:
        """完全过滤类规则的条数（词删除 + 整条不发送）。"""
        return sum(1 for rule in self.replaces if rule.is_filter) + sum(
            1 for rule in self.covers if rule.is_filter
        )


@dataclass(frozen=True)
class RuleOutcome:
    """对一条文本应用规则后的结果。

    ``cover_hit`` 非空表示命中了某条 ``c`` 规则；此时 ``text`` 是覆盖内容（可能为空串，
    代表整条过滤）。``dropped`` 为真时调用方必须阻止该消息发送。
    """

    text: str
    cover_hit: Optional[CoverRule] = None

    @property
    def dropped(self) -> bool:
        return self.cover_hit is not None and self.cover_hit.is_filter


def _unescape(text: str) -> str:
    """还原 ``\\/`` 与 ``\\\\`` 转义。"""
    result: List[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] in ("/", "\\"):
            result.append(text[index + 1])
            index += 2
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _split_new_rule_body(body: str) -> Tuple[str, str]:
    """按第一个未转义的 ``/`` 切出关键词与替换文本；没有分隔符时替换文本为空。"""
    keyword_chars: List[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body) and body[index + 1] in ("/", "\\"):
            keyword_chars.append(char)
            keyword_chars.append(body[index + 1])
            index += 2
            continue
        if char == "/":
            return _unescape("".join(keyword_chars)), _unescape(body[index + 1 :])
        keyword_chars.append(char)
        index += 1
    return _unescape("".join(keyword_chars)), ""


def _parse_item(raw_item: str) -> object:
    """解析单条规则；返回 ``(action, keyword, replacement)`` 或哨兵值。"""
    text = raw_item.strip()
    if not text or text.startswith("#"):
        return _IGNORED

    matched = _NEW_RULE_PATTERN.match(text)
    if matched is not None:
        action = matched.group(1).lower()
        keyword, replacement = _split_new_rule_body(matched.group(2))
        keyword = keyword.strip()
        if not keyword:
            return _INVALID
        # 替换文本允许为空（过滤）；首尾空白去掉，避免配置里的排版空格混进输出。
        return action, keyword, replacement.strip()

    matched = _LEGACY_QUOTED_PATTERN.match(text)
    if matched is not None:
        keyword, action, content = matched.group(1), matched.group(2), matched.group(3)
        return ("r" if action == "replace" else "c"), keyword, content

    matched = _LEGACY_ARROW_PATTERN.match(text)
    if matched is not None:
        action, keyword, content = matched.group(1), matched.group(2), matched.group(3)
        keyword = keyword.strip()
        if not keyword:
            return _INVALID
        return ("r" if action == "replace" else "c"), keyword, content.strip()

    return _INVALID


def parse_rules(raw_rules: Sequence[object]) -> ParsedRules:
    """解析配置中的规则字符串列表，格式错误的条目进入 ``invalid``。

    Args:
        raw_rules: 配置项原始值（期望为字符串列表，其他类型按错误条目处理）。

    Returns:
        ParsedRules: 词替换规则、整条覆盖/过滤规则与被拒绝的原始条目。
    """
    if not isinstance(raw_rules, (list, tuple)):
        return ParsedRules()

    replaces: List[ReplaceRule] = []
    covers: List[CoverRule] = []
    invalid: List[str] = []

    for raw_item in raw_rules:
        if not isinstance(raw_item, str):
            invalid.append(repr(raw_item))
            continue
        parsed = _parse_item(raw_item)
        if parsed is _IGNORED:
            continue
        if parsed is _INVALID:
            invalid.append(raw_item)
            continue
        action, keyword, content = parsed  # type: ignore[misc]
        if action == "r":
            replaces.append(ReplaceRule(keyword=keyword, replacement=content))
        else:
            covers.append(CoverRule(keyword=keyword, content=content))

    return ParsedRules(replaces=tuple(replaces), covers=tuple(covers), invalid=tuple(invalid))


def apply_replaces(text: str, replaces: Sequence[ReplaceRule]) -> str:
    """依次对文本应用全部词替换规则（``str.replace`` 全量替换）。"""
    result = text
    for rule in replaces:
        if rule.keyword and rule.keyword in result:
            result = result.replace(rule.keyword, rule.replacement)
    return result


def match_cover(text: str, covers: Sequence[CoverRule]) -> Optional[CoverRule]:
    """返回第一条命中的整条覆盖/过滤规则；没有命中时返回 ``None``。"""
    for rule in covers:
        if rule.keyword and rule.keyword in text:
            return rule
    return None


def evaluate_rules(text: str, rules: ParsedRules) -> RuleOutcome:
    """对单条文本应用规则：先全量替换，再检查整条覆盖/过滤。"""
    if not text or not rules:
        return RuleOutcome(text=text)
    replaced = apply_replaces(text, rules.replaces)
    cover = match_cover(replaced, rules.covers)
    if cover is not None:
        return RuleOutcome(text=cover.content, cover_hit=cover)
    return RuleOutcome(text=replaced)


def find_cover(text: str, covers: Sequence[CoverRule]) -> str:
    """兼容旧调用：返回第一条命中的覆盖内容；没有命中时返回空串。

    注意：空串同时也是"整条过滤"的标记，新代码请用 :func:`match_cover` /
    :func:`evaluate_rules` 区分"没有命中"与"命中过滤"。
    """
    rule = match_cover(text, covers)
    return rule.content if rule is not None else ""


def apply_rules(text: str, rules: ParsedRules) -> str:
    """兼容旧调用：返回应用规则后的文本；命中整条过滤时返回空串。"""
    return evaluate_rules(text, rules).text
