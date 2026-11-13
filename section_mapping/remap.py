"""Map an existing Markdown sectioning onto a revised HTML manual.

Inputs
------
- old HTML / new HTML: two snapshots of the same manual.
- manifest: JSON describing the existing Markdown sectioning results
  (reading records hang off the manifest keys).

Matching rules (in priority order)
----------------------------------
1. ``id`` anchors are identity.  A section that keeps its id is the same
   section even if its title changed or it moved in the document order.
2. Sections without a matching id are paired by an exact fingerprint of
   their own content (blocks converted to Markdown and normalised).
3. Anything left over is analysed block by block to detect splits,
   merges, moved paragraphs and removals.  These are never auto-paired.

Titles are *never* used as a matching key.  In particular, when two
sections share the same title (e.g. two sections both called "限制" that
swapped order), pairing strictly follows id/content, so reading records
stay attached to the right section instead of being swapped by ordinal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup
from markdownify import markdownify

HEADING_RE = re.compile(r"^h([1-6])$")
BLOCK_TAGS = ["p", "ul", "ol", "dl", "table", "pre", "blockquote", "figure"]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _md(html_fragment: str) -> str:
    return _norm(markdownify(html_fragment, heading_style="ATX"))


def _fp(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


@dataclass
class Section:
    source: str  # "old" | "new"
    index: int  # document order of the heading
    sec_id: str | None
    title: str
    level: int
    parent_id: str | None
    parent_title: str | None
    blocks: list[str]  # fingerprints of the section's own blocks
    block_texts: list[str]  # normalised Markdown of each own block

    @property
    def content_fp(self) -> str:
        return _fp("\n".join(self.blocks))

    @property
    def label(self) -> str:
        return f"{self.sec_id or '(no id)'} «{self.title}»"


def extract_sections(html: str, source: str) -> list[Section]:
    """Split an HTML document into heading-anchored sections.

    A section owns the block elements (p/ul/table/...) that appear after
    its heading and before the next heading of any level, so nested
    subsections are tracked independently and paragraphs can be followed
    when they move to a different parent section.
    """
    soup = BeautifulSoup(html, "html.parser")
    sections: list[Section] = []
    stack: list[Section] = []
    for tag in soup.find_all(True):
        heading = HEADING_RE.match(tag.name or "")
        if heading:
            level = int(heading.group(1))
            while stack and stack[-1].level >= level:
                stack.pop()
            anchor = tag.get("id")
            if not anchor:
                ancestor = tag.find_parent(attrs={"id": True})
                anchor = ancestor.get("id") if ancestor else None
            section = Section(
                source=source,
                index=len(sections),
                sec_id=anchor,
                title=_norm(tag.get_text()),
                level=level,
                parent_id=stack[-1].sec_id if stack else None,
                parent_title=stack[-1].title if stack else None,
                blocks=[],
                block_texts=[],
            )
            sections.append(section)
            stack.append(section)
        elif tag.name in BLOCK_TAGS and stack:
            if tag.find_parent(BLOCK_TAGS) is not None:
                continue  # nested inside an already-counted block
            text = _md(str(tag))
            stack[-1].blocks.append(_fp(text))
            stack[-1].block_texts.append(text)
    return sections


@dataclass
class MatchResult:
    pairs: dict[int, int] = field(default_factory=dict)  # old idx -> new idx
    pair_notes: dict[int, list[str]] = field(default_factory=dict)
    manual: list[dict] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)


def _note(result: MatchResult, old_index: int, note: str) -> None:
    result.pair_notes.setdefault(old_index, []).append(note)


def match_sections(old: list[Section], new: list[Section]) -> MatchResult:
    result = MatchResult()
    new_by_id: dict[str, list[Section]] = {}
    for section in new:
        if section.sec_id:
            new_by_id.setdefault(section.sec_id, []).append(section)

    # Pass 1: id anchors are identity (immune to title edits and reordering).
    for section in old:
        candidates = new_by_id.get(section.sec_id or "", [])
        free = [c for c in candidates if c.index not in result.pairs.values()]
        if section.sec_id and len(free) == 1:
            result.pairs[section.index] = free[0].index
            _note(result, section.index, "按 id 配对")

    # Pass 2: exact content fingerprint for what is left.
    new_by_content: dict[str, list[Section]] = {}
    for section in new:
        if section.index not in result.pairs.values():
            new_by_content.setdefault(section.content_fp, []).append(section)
    for section in old:
        if section.index in result.pairs or not section.blocks:
            continue
        candidates = new_by_content.get(section.content_fp, [])
        free = [c for c in candidates if c.index not in result.pairs.values()]
        if len(free) == 1:
            result.pairs[section.index] = free[0].index
            _note(result, section.index, "id 缺失/变更，按完整内容指纹配对")

    # Pass 3: block-level analysis for the remainder (splits/merges/removals).
    block_to_new: dict[str, list[Section]] = {}
    for section in new:
        for block in section.blocks:
            block_to_new.setdefault(block, []).append(section)

    containment: dict[int, dict[int, int]] = {}  # old idx -> {new idx: hits}
    for section in old:
        if section.index in result.pairs:
            continue
        hits: dict[int, int] = {}
        for block in section.blocks:
            for target in block_to_new.get(block, []):
                hits[target.index] = hits.get(target.index, 0) + 1
        containment[section.index] = hits

    for section in old:
        if section.index in result.pairs:
            continue
        hits = containment[section.index]
        if not hits:
            result.removed.append(section.index)
            continue
        targets = sorted(hits, key=hits.get, reverse=True)
        if len(hits) == 1 and hits[targets[0]] == len(section.blocks):
            # Whole section relocated under a new id/parent; safe to pair.
            result.pairs[section.index] = targets[0]
            _note(result, section.index, "整节内容迁移，按块级内容配对")
        else:
            result.manual.append({
                "type": "split",
                "old": section.label,
                "old_index": section.index,
                "candidates": [
                    {"new": new[t].label, "new_index": t,
                     "matched_blocks": hits[t], "of": len(section.blocks)}
                    for t in targets
                ],
                "detail": "原章节内容被拆分到多个新章节（或只有部分块找到归属），"
                          "需人工认领各块的阅读记录",
            })

    # Merge detection: one new section holding blocks of several old sections.
    origin: dict[int, list[int]] = {}
    for old_index, hits in containment.items():
        for new_index in hits:
            origin.setdefault(new_index, []).append(old_index)
    for new_index, old_indexes in sorted(origin.items()):
        if len(old_indexes) > 1:
            result.manual.append({
                "type": "merge",
                "new": new[new_index].label,
                "new_index": new_index,
                "old_sections": [old[i].label for i in sorted(old_indexes)],
                "detail": "多个旧章节的内容并入同一新章节，需人工确认阅读记录归属",
            })
    return result


def _order_changed_notes(old: list[Section], new: list[Section],
                         result: MatchResult) -> None:
    """Flag pairs whose relative order changed (e.g. two «限制» swapped)."""
    paired = sorted(result.pairs.items())
    new_order = sorted(range(len(paired)), key=lambda k: paired[k][1])
    for rank, position in enumerate(new_order):
        if rank != position:
            old_index = paired[position][0]
            _note(result, old_index,
                  "相对顺序发生变化（按 id/内容配对，阅读记录不随序号迁移）")


def _duplicate_title_warnings(sections: list[Section], result: MatchResult,
                              pairs: dict[int, int], is_old: bool) -> None:
    titles: dict[str, int] = {}
    for section in sections:
        titles[section.title] = titles.get(section.title, 0) + 1
    for section in sections:
        if titles[section.title] < 2:
            continue
        if is_old and section.index in pairs:
            _note(result, section.index,
                  f"存在同名章节「{section.title}」，配对未使用出现序号")


def build_report(old: list[Section], new: list[Section],
                 manifest: dict | None = None) -> dict:
    result = match_sections(old, new)
    _order_changed_notes(old, new, result)
    _duplicate_title_warnings(old, result, result.pairs, is_old=True)

    manifest_by_id = {
        entry.get("id"): entry
        for entry in (manifest or {}).get("sections", [])
        if entry.get("id")
    }
    reusable, rescrape = [], []
    paired_new = set(result.pairs.values())

    for old_index, new_index in sorted(result.pairs.items()):
        old_sec, new_sec = old[old_index], new[new_index]
        notes = list(result.pair_notes.get(old_index, []))
        if old_sec.title != new_sec.title:
            notes.append(f"标题变更：「{old_sec.title}」→「{new_sec.title}」")
        if (old_sec.parent_id, old_sec.parent_title) != \
                (new_sec.parent_id, new_sec.parent_title):
            notes.append(f"父节变更：{old_sec.parent_title or '(root)'}"
                         f" → {new_sec.parent_title or '(root)'}")
        entry = {
            "old": old_sec.label,
            "new": new_sec.label,
            "old_id": old_sec.sec_id,
            "new_id": new_sec.sec_id,
            "notes": notes,
        }
        manifest_entry = manifest_by_id.get(old_sec.sec_id)
        if manifest_entry:
            entry["manifest_key"] = manifest_entry.get("key")
            entry["markdown_file"] = manifest_entry.get("markdown_file")
            entry["reading_record"] = manifest_entry.get("reading_record")
        if old_sec.content_fp == new_sec.content_fp:
            reusable.append(entry)
        else:
            moved_out = [b for b in old_sec.blocks if b not in new_sec.blocks]
            moved_in = [b for b in new_sec.blocks if b not in old_sec.blocks]
            reasons = ["内容有改动"]
            for block in moved_out:
                hosts = [s.label for s in new
                         if s.index != new_index and block in s.blocks]
                if hosts:
                    reasons.append(f"原段落移至 {'、'.join(hosts)}")
            if moved_in:
                sources = [s.label for s in old
                           if any(b in moved_in for b in s.blocks)]
                reasons.append(
                    f"新增 {len(moved_in)} 个块"
                    + (f"（来源：{'、'.join(sorted(set(sources)))}）" if sources else ""))
            entry["reasons"] = reasons
            rescrape.append(entry)

    for section in new:
        if section.index not in paired_new:
            rescrape.append({
                "old": None,
                "new": section.label,
                "old_id": None,
                "new_id": section.sec_id,
                "notes": [],
                "reasons": ["新增章节"],
            })

    for old_index in result.removed:
        result.manual.append({
            "type": "removed",
            "old": old[old_index].label,
            "old_index": old_index,
            "detail": "新版中已找不到该章节的任何内容，请确认删除并归档阅读记录",
        })

    old_ids = {section.sec_id for section in old if section.sec_id}
    unmatched_manifest = [
        entry.get("key") for entry in (manifest or {}).get("sections", [])
        if entry.get("id") and entry["id"] not in old_ids
    ]

    return {
        "reusable": reusable,
        "rescrape": rescrape,
        "manual": result.manual,
        "warnings": [
            f"manifest 条目 {key} 未能在旧 HTML 中定位" for key in unmatched_manifest
        ],
    }


def render_markdown(report: dict) -> str:
    lines = ["# 手册更新对照报告", ""]

    def _notes(entry):
        return f"（{'；'.join(entry['notes'])}）" if entry.get("notes") else ""

    lines.append("## 可沿用的内容")
    if report["reusable"]:
        for entry in report["reusable"]:
            record = "，阅读记录随 id 沿用" if entry.get("reading_record") else ""
            lines.append(f"- `{entry['old']}` → `{entry['new']}`"
                         f"{_notes(entry)}{record}")
    else:
        lines.append("- （无）")
    lines.append("")
    lines.append("## 需要重采的章节")
    if report["rescrape"]:
        for entry in report["rescrape"]:
            reasons = "；".join(entry.get("reasons", []))
            origin = f"（旧：`{entry['old']}`）" if entry.get("old") else "（全新）"
            lines.append(f"- `{entry['new']}` {origin}：{reasons}")
    else:
        lines.append("- （无）")
    lines.append("")
    lines.append("## 需要人工认领的拆分")
    if report["manual"]:
        for item in report["manual"]:
            if item["type"] == "split":
                lines.append(f"- 拆分：旧 `{item['old']}` →")
                for cand in item["candidates"]:
                    lines.append(f"  - `{cand['new']}`"
                                 f"（命中 {cand['matched_blocks']}/{cand['of']} 块）")
                lines.append(f"  - {item['detail']}")
            elif item["type"] == "merge":
                olds = "、".join(f"`{o}`" for o in item["old_sections"])
                lines.append(f"- 合并：{olds} → `{item['new']}`。{item['detail']}")
            else:
                lines.append(f"- {item['type']}：`{item['old']}`。{item['detail']}")
    else:
        lines.append("- （无）")
    if report.get("warnings"):
        lines.append("")
        lines.append("## 警告")
        for warning in report["warnings"]:
            lines.append(f"- {warning}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("old_html", type=Path)
    parser.add_argument("new_html", type=Path)
    parser.add_argument("manifest", type=Path,
                        help="JSON manifest of the existing Markdown sectioning")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--md-out", type=Path)
    args = parser.parse_args(argv)

    old = extract_sections(args.old_html.read_text(encoding="utf-8"), "old")
    new = extract_sections(args.new_html.read_text(encoding="utf-8"), "new")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    report = build_report(old, new, manifest)

    if args.json_out:
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
    markdown = render_markdown(report)
    if args.md_out:
        args.md_out.write_text(markdown, encoding="utf-8")
    if not args.json_out and not args.md_out:
        print(markdown)
    print(f"可沿用 {len(report['reusable'])} 节，"
          f"需重采 {len(report['rescrape'])} 节，"
          f"待人工认领 {len(report['manual'])} 项", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
