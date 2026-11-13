from datetime import date

import pytest

from markdownify import (
    CharterError,
    charter_as_markdown,
    version_charter,
)


CHARTER = """
<article data-clause="第一条">
  <h2>第一条 名称</h2>
  <p>本组织名为<del data-effective="2024-01-01" cite="章程2024A">旧名称</del
  ><ins data-effective="2024-01-01" cite="章程2024A">新名称</ins>。</p>
</article>
<article data-clause="第二条">
  <h2>第二条 会期</h2>
  <p>每年召开一次大会。
    <ins data-effective="2025-06-01" cite="章程2025B">必要时可召开临时大会。</ins>
  </p>
</article>
<article data-clause="第三条">
  <h2>第三条 会费</h2>
  <p>会员每年缴纳会费
    <del data-effective="2025-01-01" cite="章程2025A">一百元
      <ins data-effective="2025-01-01" cite="章程2025A">（含资料费）</ins>
    </del
    ><ins data-effective="2025-01-01" cite="章程2025A">两百元</ins>。
  </p>
</article>
"""


def test_before_any_amendment_keeps_original_text():
    result = version_charter(CHARTER, '2023-12-31')
    assert not result.has_conflicts
    first = result.clauses[0]
    assert '旧名称' in first.text
    assert '新名称' not in first.text
    second = result.clauses[1]
    assert '临时大会' not in second.text
    assert '每年召开一次大会' in second.text


def test_future_insertion_is_hidden_until_effective_date():
    result = version_charter(CHARTER, date(2025, 5, 31))
    assert '临时大会' not in result.clauses[1].text
    result = version_charter(CHARTER, '2025-06-01')
    assert '临时大会' in result.clauses[1].text
    assert '章程2025B' in result.clauses[1].sources


def test_effective_amendment_applies_regardless_of_document_order():
    # 先公布的未来修订写在前面，补录的旧修订写在后面，仍按生效日采算。
    html = """
    <article data-clause="第一条">
      <p>
        <ins data-effective="2026-01-01" cite="未来修订">未来追加</ins>
        <del data-effective="2024-01-01" cite="补录旧修订">旧条文</del>
      </p>
    </article>
    """
    result = version_charter(html, '2025-01-01')
    clause = result.clauses[0]
    assert '旧条文' not in clause.text
    assert '未来追加' not in clause.text
    assert '补录旧修订' in clause.sources
    assert '未来修订' not in clause.sources


def test_nested_insertion_inside_deletion_dies_with_parent():
    result = version_charter(CHARTER, '2025-01-01')
    third = result.clauses[2]
    assert '两百元' in third.text
    assert '一百元' not in third.text
    # 嵌套在整段删除里的插入不能脱离父条文单独生效。
    assert '资料费' not in third.text
    assert '章程2025A' in third.sources


def test_sources_are_attributed_per_clause():
    result = version_charter(CHARTER, '2025-06-01')
    assert result.clauses[0].sources == ['章程2024A']
    assert result.clauses[1].sources == ['章程2025B']
    markdown = result.to_markdown()
    assert '出处：章程2024A' in markdown
    assert '出处：章程2025B' in markdown


def test_same_day_contradictory_amendments_flag_minimal_clause():
    html = """
    <section data-clause="第五条">
      <h2>第五条 表决</h2>
      <p id="p1">普通事项过半数通过。</p>
      <p id="p2">特殊事项
        <del data-effective="2025-03-01" cite="修订甲">三分之二</del>
        <ins data-effective="2025-03-01" cite="修订甲">四分之三</ins>
        通过。
      </p>
      <p id="p3">另一处
        <del data-effective="2025-03-01" cite="修订乙">旧表述</del>
        <ins data-effective="2025-03-01" cite="修订乙">新表述</ins>。
      </p>
    </section>
    <section data-clause="第六条">
      <h2>第六条 不受影响</h2>
      <p>本章没有任何变化，照常成文。</p>
    </section>
    """
    result = version_charter(html, '2025-03-01')
    assert result.has_conflicts
    disputed = result.conflicts[0]
    assert disputed.identifier == '第五条'
    # 受影响的最小条款是两个具体 <p>，而不是整条第五条。
    assert set(disputed.disputed_clauses) == {'p2', 'p3'}
    assert set(disputed.conflict_sources) == {'修订甲', '修订乙'}
    # 争议痕迹保留，不替读者取舍。
    assert '~~三分之二~~' in disputed.text
    assert '++四分之三++' in disputed.text
    assert '~~旧表述~~' in disputed.text
    assert '++新表述++' in disputed.text
    # 没有变化的章节照常成文。
    sixth = result.clauses[1]
    assert not sixth.conflict
    assert '照常成文' in sixth.text
    markdown = result.to_markdown()
    assert '争议条款：p2、p3' in markdown
    assert '## 第六条 不受影响' in markdown


def test_same_day_amendments_with_explicit_order_are_not_conflicts():
    html = """
    <article data-clause="第一条">
      <p>金额
        <del data-effective="2025-03-01" data-order="1" cite="修订甲">旧标准</del
        ><ins data-effective="2025-03-01" data-order="2" cite="修订甲">新标准</ins>。
      </p>
    </article>
    """
    result = version_charter(html, '2025-03-01')
    assert not result.has_conflicts
    assert '新标准' in result.clauses[0].text
    assert '旧标准' not in result.clauses[0].text
    assert '~~' not in result.clauses[0].text


def test_missing_effective_date_raises():
    html = '<article><p><ins cite="无日期">文字</ins></p></article>'
    with pytest.raises(CharterError):
        version_charter(html, '2025-01-01')


def test_markdown_helper_returns_full_document():
    markdown = charter_as_markdown(CHARTER, '2024-01-01')
    assert markdown.startswith('# 章程（2024-01-01 有效）')
    assert '## 第一条 名称' in markdown
    assert '新名称' in markdown


def test_cli_entrypoint(capsys, tmp_path):
    from markdownify.charter import main

    html_path = tmp_path / 'charter.html'
    html_path.write_text(CHARTER, encoding='utf-8')
    main(['-d', '2025-06-01', str(html_path)])
    output = capsys.readouterr().out
    assert '# 章程（2025-06-01 有效）' in output
    assert '临时大会' in output
    assert '旧名称' not in output
