import json
from pathlib import Path

from section_mapping.remap import (
    build_report,
    extract_sections,
    match_sections,
    render_markdown,
)

EXAMPLES = Path(__file__).parent.parent / "examples"


def load_example():
    old = extract_sections((EXAMPLES / "old.html").read_text(encoding="utf-8"), "old")
    new = extract_sections((EXAMPLES / "new.html").read_text(encoding="utf-8"), "new")
    manifest = json.loads((EXAMPLES / "manifest.json").read_text(encoding="utf-8"))
    return old, new, manifest


def reusable_by_old_id(report):
    return {entry["old_id"]: entry for entry in report["reusable"]}


def test_title_change_with_same_id_is_reusable():
    old, new, manifest = load_example()
    report = build_report(old, new, manifest)
    intro = reusable_by_old_id(report)["intro"]
    assert intro["new_id"] == "intro"
    assert any("标题变更" in note and "介绍" in note and "产品简介" in note
               for note in intro["notes"])
    assert intro["reading_record"]["reader"] == "alice"


def test_swapped_duplicate_titles_follow_id_not_ordinal():
    old, new, manifest = load_example()
    report = build_report(old, new, manifest)
    reusable = reusable_by_old_id(report)
    # 两个「限制」互换了顺序：记录必须跟着 id 走，不能按出现次序对调。
    assert reusable["limits-a"]["new_id"] == "limits-a"
    assert reusable["limits-b"]["new_id"] == "limits-b"
    assert reusable["limits-a"]["reading_record"]["reader"] == "carol"
    assert reusable["limits-b"]["reading_record"]["reader"] == "dave"
    for entry in (reusable["limits-a"], reusable["limits-b"]):
        assert any("同名章节" in note for note in entry["notes"])
        assert any("顺序" in note for note in entry["notes"])


def test_duplicate_titles_without_ids_pair_by_content_not_order():
    old_html = """
    <h2>限制</h2><p>每天限 100 次。</p>
    <h2>限制</h2><p>单次不超过 10MB。</p>
    """
    new_html = """
    <h2>限制</h2><p>单次不超过 10MB。</p>
    <h2>限制</h2><p>每天限 100 次。</p>
    """
    old = extract_sections(old_html, "old")
    new = extract_sections(new_html, "new")
    result = match_sections(old, new)
    # 无 id 时按内容指纹配对，绝不按同名序号互换。
    assert result.pairs == {0: 1, 1: 0}


def test_moved_paragraph_to_other_parent_is_flagged():
    old, new, manifest = load_example()
    report = build_report(old, new, manifest)
    rescrape = {entry["new_id"]: entry for entry in report["rescrape"]}
    install = rescrape["install"]
    usage = rescrape["usage"]
    assert any("原段落移至" in reason and "usage" in reason
               for reason in install["reasons"])
    assert any("新增 1 个块" in reason and "install" in reason
               for reason in usage["reasons"])


def test_split_section_goes_to_manual_with_candidates():
    old, new, manifest = load_example()
    report = build_report(old, new, manifest)
    splits = [item for item in report["manual"] if item["type"] == "split"]
    assert len(splits) == 1
    split = splits[0]
    assert split["old"].startswith("misc")
    candidates = {cand["new"].split(" ")[0]: cand for cand in split["candidates"]}
    assert set(candidates) == {"notes-a", "notes-b"}
    assert all(cand["matched_blocks"] == 2 and cand["of"] == 4
               for cand in candidates.values())


def test_new_and_removed_sections():
    old, new, manifest = load_example()
    report = build_report(old, new, manifest)
    new_ids = {entry["new_id"] for entry in report["rescrape"]}
    assert "changelog" in new_ids
    removed = [item for item in report["manual"] if item["type"] == "removed"]
    assert [item["old"].split(" ")[0] for item in removed] == ["legacy"]


def test_relocated_section_with_new_id_is_reusable():
    old_html = '<h1 id="a">甲</h1><p>内容不变。</p><h1 id="b">乙</h1><p>乙内容。</p>'
    new_html = ('<h1 id="b">乙</h1><p>乙内容。</p>'
                '<h1 id="a2">甲</h1><p>内容不变。</p>')
    old = extract_sections(old_html, "old")
    new = extract_sections(new_html, "new")
    report = build_report(old, new, manifest=None)
    reusable = reusable_by_old_id(report)
    assert reusable["a"]["new_id"] == "a2"
    assert reusable["b"]["new_id"] == "b"
    assert report["manual"] == []


def test_report_renders_three_lists():
    old, new, manifest = load_example()
    markdown = render_markdown(build_report(old, new, manifest))
    assert "## 可沿用的内容" in markdown
    assert "## 需要重采的章节" in markdown
    assert "## 需要人工认领的拆分" in markdown
