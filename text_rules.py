"""出站文本替换/覆盖规则的解析与应用（纯逻辑，不依赖 SDK，可离线单测）。

规则以字符串形式写在插件配置里，格式固定为::

    "shit"replace"filter"   —— 把消息文本中的 ``shit`` 全部替换为 ``filter``
    "草"cover"你好"         —— 消息文本包含 ``草`` 时，把整条文本覆盖为 ``你好``

解析采用严格匹配：关键词非空、大小写敏感的 ``replace``/``cover`` 关键字、
ASCII 双引号包裹。任何不符合格式的条目都会被收集为错误项（由插件记录日志后
忽略），保证"格式错误不收"。

应用顺序：先依次应用全部 ``replace`` 规则（对当前文本做全量替换），再按配置
顺序检查 ``cover`` 规则，第一条命中的 ``cover`` 直接把整条文本覆盖为其内容并
结束匹配。``cover`` 的覆盖内容必须非空（覆盖为空串没有意义，按格式错误处理）；
``replace`` 允许空替换词（等价于删除关键词）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Sequence, Tuple

# 严格格式："关键词"(replace|cover)"内容"。关键词不允许为空，也不允许包含双引号。
_RULE_PATTERN = re.compile(r'^\s*"([^"]+)"(replace|cover)"([^"]*)"\s*$')


@dataclass(frozen=True)
class ReplaceRule:
    """把消息中的 ``keyword`` 全量替换为 ``replacement``（可为空串，即删除）。"""

    keyword: str
    replacement: str


@dataclass(frozen=True)
class CoverRule:
    """消息文本包含 ``keyword`` 时，整条文本覆盖为 ``content``。"""

    keyword: str
    content: str


@dataclass(frozen=True)
class ParsedRules:
    """一批规则字符串的解析结果。"""

    replaces: Tuple[ReplaceRule, ...] = ()
    covers: Tuple[CoverRule, ...] = ()
    invalid: Tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.replaces or self.covers)


def parse_rules(raw_rules: Sequence[object]) -> ParsedRules:
    """解析配置中的规则字符串列表，格式错误的条目进入 ``invalid``。

    Args:
        raw_rules: 配置项原始值（期望为字符串列表，其他类型按错误条目处理）。

    Returns:
        ParsedRules: 替换规则、覆盖规则与被拒绝的原始条目。
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
        matched = _RULE_PATTERN.match(raw_item)
        if matched is None:
            invalid.append(raw_item)
            continue
        keyword, action, content = matched.group(1), matched.group(2), matched.group(3)
        if action == "replace":
            # replace 允许空替换词（删除关键词），但空关键词无意义。
            replaces.append(ReplaceRule(keyword=keyword, replacement=content))
        else:
            # cover 的覆盖内容必须非空，否则整条消息会被替换成空串。
            if not content:
                invalid.append(raw_item)
                continue
            covers.append(CoverRule(keyword=keyword, content=content))

    return ParsedRules(replaces=tuple(replaces), covers=tuple(covers), invalid=tuple(invalid))


def apply_replaces(text: str, replaces: Sequence[ReplaceRule]) -> str:
    """依次对文本应用全部替换规则（str.replace 全量替换）。"""
    result = text
    for rule in replaces:
        if rule.keyword and rule.keyword in result:
            result = result.replace(rule.keyword, rule.replacement)
    return result


def find_cover(text: str, covers: Sequence[CoverRule]) -> str:
    """返回第一条命中的覆盖内容；没有命中时返回空串。"""
    for rule in covers:
        if rule.keyword and rule.keyword in text:
            return rule.content
    return ""


def apply_rules(text: str, rules: ParsedRules) -> str:
    """对单条文本应用规则：先全量替换，再检查覆盖。"""
    if not text or not rules:
        return text
    replaced = apply_replaces(text, rules.replaces)
    covered = find_cover(replaced, rules.covers)
    return covered if covered else replaced
