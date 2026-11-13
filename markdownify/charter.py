"""
按生效日采算章程文本。

章程页用 ``<ins>`` / ``<del>`` 标注修订，并在标签上携带生效日（``data-effective``
或 ``datetime`` 属性）与出处（``cite`` 或 ``data-source`` 属性）。本模块把一份
累积了历次修订的 HTML 章程还原成任意一天实际有效的条文，并附上每段文字的出处。

需要处理的三个边界情形：

* 修订可能先于生效日公布，也可能在生效之后才补录，因此只按生效日排序，
  与录入顺序无关；
* ``<del>`` 整段删除里嵌套的 ``<ins>`` 随父条文一起失效，不能脱离父条文单独
  生效；
* 同一生效日存在两次互相矛盾、又没有给出先后顺序的修订时，不武断取舍，
  而是圈出受影响的最小条款并报告冲突，其余章节照常成文。
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Union

from bs4 import BeautifulSoup, Tag

from . import MarkdownConverter, chomp


# 修订标签上承载生效日与出处的属性名，按优先级排列。
DATE_ATTRS = ('data-effective', 'datetime')
SOURCE_ATTRS = ('cite', 'data-source')

# 记录修订的标签。
REVISION_TAGS = ('ins', 'del')

# 视为条款容器的标签；冲突圈定时向上找最近的一个。
CLAUSE_TAGS = (
    'article', 'section', 'li', 'p', 'blockquote', 'td', 'th', 'dt', 'dd',
    'div',
)

_DATE_RE = re.compile(r'(\d{4})-(\d{2})-(\d{2})')


@dataclass(frozen=True)
class Amendment:
    """一次插入或删除修订。"""

    effective_on: date
    kind: str  # 'ins' 或 'del'
    source: Optional[str]
    order: Optional[int]


@dataclass
class ClauseResult:
    """一条条款在指定日期的采算结果。"""

    identifier: str
    title: str
    text: str
    sources: list[str] = field(default_factory=list)
    conflict: bool = False
    disputed_clauses: list[str] = field(default_factory=list)
    conflict_sources: list[str] = field(default_factory=list)


@dataclass
class CharterResult:
    """整本章程在指定日期的采算结果。"""

    as_of: date
    clauses: list[ClauseResult]
    conflicts: list[ClauseResult]

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    def to_markdown(self) -> str:
        lines = ['# 章程（%s 有效）' % self.as_of.isoformat(), '']
        if self.conflicts:
            lines.append('> 以下条款存在同日矛盾、先后顺序未明的修订，'
                         '争议处以引用块圈出：')
            for clause in self.conflicts:
                lines.append('> - %s' % clause.identifier)
            lines.append('')
        for clause in self.clauses:
            heading = '## %s' % (clause.title or clause.identifier)
            lines.extend([heading, '', clause.text, ''])
            if clause.conflict:
                lines.append('争议条款：%s' % '、'.join(clause.disputed_clauses))
                if clause.conflict_sources:
                    lines.append('争议出处：%s'
                                 % '；'.join(clause.conflict_sources))
                lines.append('')
            if clause.sources:
                lines.append('出处：%s' % '；'.join(clause.sources))
                lines.append('')
        return '\n'.join(lines).rstrip() + '\n'


class CharterError(ValueError):
    """章程 HTML 无法解析或修订标注不完整。"""


class _CharterConverter(MarkdownConverter):
    """渲染争议条款时保留修订痕迹，不替读者取舍。"""

    def convert_ins(self, el, text, parent_tags):
        prefix, suffix, text = chomp(text)
        if not text:
            return ''
        return '%s++%s++%s' % (prefix, text, suffix)

    def convert_del(self, el, text, parent_tags):
        prefix, suffix, text = chomp(text)
        if not text:
            return ''
        return '%s~~%s~~%s' % (prefix, text, suffix)


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    match = _DATE_RE.search(value.strip())
    if not match:
        raise CharterError('无法识别的生效日：%r' % value)
    year, month, day = (int(part) for part in match.groups())
    return date(year, month, day)


def _parse_order(value: Optional[str]) -> Optional[int]:
    if value is None or str(value).strip() == '':
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _attr(tag: Tag, names: tuple[str, ...]) -> Optional[str]:
    for name in names:
        value = tag.get(name)
        if value is not None and str(value).strip() != '':
            return str(value).strip()
    return None


def _clause_identifier(tag: Tag, fallback: str) -> str:
    for name in ('data-clause', 'id'):
        value = tag.get(name)
        if value:
            return str(value)
    heading = tag.find(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    if heading is not None:
        text = heading.get_text(strip=True)
        if text:
            return text
    return fallback


def _clause_title(tag: Tag) -> str:
    heading = tag.find(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    if heading is None:
        return ''
    return heading.get_text(' ', strip=True)


def _detach_heading(tag: Tag) -> None:
    """移除条款自带的首个标题，避免与合成的条款标题重复。"""
    heading = tag.find(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
    if heading is not None:
        heading.decompose()


def _read_amendment(tag: Tag) -> Amendment:
    effective_on = _parse_date(_attr(tag, DATE_ATTRS))
    if effective_on is None:
        raise CharterError(
            '<%s> 修订缺少生效日（需要 %s 属性）'
            % (tag.name, ' 或 '.join(DATE_ATTRS))
        )
    return Amendment(
        effective_on=effective_on,
        kind=tag.name,
        source=_attr(tag, SOURCE_ATTRS),
        order=_parse_order(tag.get('data-order')),
    )


def _revision_depth(tag: Tag) -> int:
    """标签外嵌套了多少层修订标签。"""
    return sum(
        1
        for parent in tag.parents
        if isinstance(parent, Tag) and parent.name in REVISION_TAGS
    )


def _find_conflict_tags(
    entries: list[tuple[Tag, Amendment]],
) -> set[int]:
    """
    找出同日矛盾且无法排序的修订标签。

    同一生效日存在两个以上不同出处的修订时：若每个标签都带互不重复的
    ``data-order``，按顺序套用，不算冲突；否则无法确定各出处修订的先后，
    当日全部修订都圈出待裁。同一出处的 ``del`` + ``ins`` 只是一次替换动作
    的两面，不构成矛盾。
    """
    by_day: dict[date, list[tuple[Tag, Amendment]]] = {}
    for tag, amendment in entries:
        by_day.setdefault(amendment.effective_on, []).append((tag, amendment))

    conflict_ids: set[int] = set()
    for same_day in by_day.values():
        if len(same_day) < 2:
            continue
        sources = {amendment.source for _, amendment in same_day}
        if len(sources) < 2:
            continue
        orders = [amendment.order for _, amendment in same_day]
        fully_ordered = (
            all(order is not None for order in orders)
            and len(set(orders)) == len(orders)
        )
        if not fully_ordered:
            conflict_ids.update(id(tag) for tag, _ in same_day)
    return conflict_ids


def _minimal_clause(tag: Tag) -> Optional[Tag]:
    for parent in [tag, *tag.parents]:
        if isinstance(parent, Tag) and parent.name in CLAUSE_TAGS:
            return parent
    return None


def _sub_clause_id(parent_id: str, container: Tag) -> str:
    fallback = '%s / %s#%d' % (
        parent_id,
        container.name,
        len(container.find_previous_siblings(container.name)) + 1,
    )
    return _clause_identifier(container, fallback)


def _mark_disputed(clone: Tag, conflict_tags: list[Tag],
                   parent_id: str, effective_on: date) -> list[str]:
    """用引用块圈出受影响的最小条款，返回争议条款标识。"""
    containers: list[Tag] = []
    for tag in conflict_tags:
        container = _minimal_clause(tag)
        if container is not None and container not in containers:
            containers.append(container)

    # 容器互相嵌套时只留最小的，避免引用块套引用块。
    minimal = [
        container
        for container in containers
        if not any(
            other is not container and other in container.descendants
            for other in containers
        )
    ]

    disputed: list[str] = []
    for container in minimal:
        disputed.append(_sub_clause_id(parent_id, container))
        notice = BeautifulSoup(
            '<p>争议条款（%s 同日修订顺序未明）：%s</p>'
            % (effective_on.isoformat(), _sub_clause_id(parent_id, container)),
            'html.parser',
        ).p
        blockquote = BeautifulSoup('<blockquote></blockquote>',
                                   'html.parser').blockquote
        if container.parent is None:
            # container 就是条款根节点，先把内容搬进引用块再挂回根节点。
            for child in list(container.contents):
                blockquote.append(child)
            container.append(blockquote)
        else:
            container.wrap(blockquote)
        blockquote.insert(0, notice)
    return disputed


def _dedupe(items: list[Optional[str]]) -> list[str]:
    result: list[str] = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


def _version_clause(clause: Tag, identifier: str,
                    as_of: date) -> ClauseResult:
    clone = copy.deepcopy(clause)
    entries = [(tag, _read_amendment(tag))
               for tag in clone.find_all(REVISION_TAGS)]

    future = [(tag, amendment) for tag, amendment in entries
              if amendment.effective_on > as_of]
    due = [(tag, amendment) for tag, amendment in entries
           if amendment.effective_on <= as_of]

    # 尚未生效：插入内容不出现，删除内容保留原文。先内层后外层解包。
    for tag, _ in sorted(future, key=lambda item: _revision_depth(item[0]),
                         reverse=True):
        if tag.name == 'ins':
            tag.decompose()
        else:
            tag.unwrap()

    conflict_ids = _find_conflict_tags(due)
    conflict_by_day: dict[date, list[Tag]] = {}
    for tag, amendment in due:
        if id(tag) in conflict_ids:
            conflict_by_day.setdefault(amendment.effective_on, []).append(tag)

    base_sources = [_attr(clause, SOURCE_ATTRS)]
    applied_sources = [
        amendment.source for tag, amendment in due
        if id(tag) not in conflict_ids
    ]
    conflict_sources = [
        amendment.source for tag, amendment in due
        if id(tag) in conflict_ids
    ]

    # 套用无争议的到期修订：按生效日与给定顺序排，同日嵌套的先内层后外层，
    # 删除会取走整段（含其中的嵌套插入），插入则解包。
    for tag, amendment in sorted(
        (item for item in due if id(item[0]) not in conflict_ids),
        key=lambda item: (
            item[1].effective_on,
            item[1].order if item[1].order is not None else 0,
            -_revision_depth(item[0]),
        ),
    ):
        if tag.parent is None:
            continue
        if amendment.kind == 'del':
            tag.decompose()
        else:
            tag.unwrap()

    disputed: list[str] = []
    for effective_on in sorted(conflict_by_day):
        disputed.extend(
            _mark_disputed(clone, conflict_by_day[effective_on],
                           identifier, effective_on)
        )

    title = _clause_title(clone)
    _detach_heading(clone)
    text = _CharterConverter(heading_style='atx').convert_soup(clone).strip()

    return ClauseResult(
        identifier=identifier,
        title=title,
        text=text,
        sources=_dedupe(base_sources + applied_sources),
        conflict=bool(conflict_by_day),
        disputed_clauses=_dedupe(disputed),
        conflict_sources=_dedupe(conflict_sources),
    )


def _extract_clauses(soup: BeautifulSoup) -> list[Tag]:
    clauses = [
        tag for tag in soup.find_all(CLAUSE_TAGS)
        if tag.find_parent(CLAUSE_TAGS) is None
    ]
    if clauses:
        return clauses
    return [soup.body or soup]


def version_charter(
    html: Union[str, BeautifulSoup],
    as_of: Union[date, str],
) -> CharterResult:
    """
    采算 ``as_of`` 当日有效的章程。

    :param html: 累积了 ``<ins>`` / ``<del>`` 修订标注的章程 HTML，或已解析的
        BeautifulSoup 对象。
    :param as_of: 目标日期，``date`` 对象或 ``YYYY-MM-DD`` 字符串。
    """
    if isinstance(as_of, str):
        parsed = _parse_date(as_of)
        if parsed is None:
            raise CharterError('缺少目标日期')
        as_of = parsed

    if isinstance(html, BeautifulSoup):
        soup = html
    else:
        soup = BeautifulSoup(html, 'html.parser')

    results: list[ClauseResult] = []
    conflicts: list[ClauseResult] = []
    for index, clause in enumerate(_extract_clauses(soup), start=1):
        identifier = _clause_identifier(clause, '第%s条' % index)
        result = _version_clause(clause, identifier, as_of)
        results.append(result)
        if result.conflict:
            conflicts.append(result)

    return CharterResult(as_of=as_of, clauses=results, conflicts=conflicts)


def charter_as_markdown(html: Union[str, BeautifulSoup],
                        as_of: Union[date, str]) -> str:
    """采算章程并直接返回带出处与争议标记的 Markdown 文本。"""
    return version_charter(html, as_of).to_markdown()


def main(argv: Optional[list[str]] = None) -> None:
    """命令行入口：``charter-version -d 2025-01-01 charter.html``。"""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        prog='charter-version',
        description='按生效日采算章程，输出当日有效条文及其出处。',
    )
    parser.add_argument(
        'html', nargs='?',
        type=argparse.FileType('r'),
        default=sys.stdin,
        help='章程 HTML 文件，缺省读标准输入。',
    )
    parser.add_argument(
        '-d', '--date', required=True,
        help='目标日期，格式 YYYY-MM-DD。',
    )
    args = parser.parse_args(argv)
    sys.stdout.write(charter_as_markdown(args.html.read(), args.date))
