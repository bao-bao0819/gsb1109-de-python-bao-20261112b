from markdownify import MarkdownConverter

from .utils import md


# A rate table whose surcharge clauses live on the same page but outside the
# table itself; the cells only keep the superscript markers.
rate_table = """
<table>
    <tr>
        <th>航线</th>
        <th>运价</th>
    </tr>
    <tr>
        <td>上海-汉堡</td>
        <td>1500<sup>*</sup></td>
    </tr>
    <tr>
        <td>上海-鹿特丹</td>
        <td>1600<sup>*</sup><sup>†</sup></td>
    </tr>
</table>
<p>* 含燃油附加费，旺季另计。</p>
<p>† 需提前 7 天订舱。</p>
"""

# A second rate table reusing the same '*' symbol for a different clause.
second_rate_table = """
<table>
    <tr>
        <th>航线</th>
        <th>运价</th>
    </tr>
    <tr>
        <td>深圳-洛杉矶</td>
        <td>2000<sup>*</sup></td>
    </tr>
</table>
<p>* 含码头操作费。</p>
"""

explicit_footnotes = """
<p>报价 100<sup><a href="#fn1">1</a></sup> 和 200<sup><a href="#fn2">2</a></sup></p>
<section class="footnotes">
    <ol>
        <li id="fn1">
            <p>最低一票适用于该报价。<a class="footnote-backref" href="#ref1">↩</a></p>
        </li>
        <li id="fn2">
            <p>需签署年度协议。</p>
        </li>
    </ol>
</section>
"""


def _converter(html, **options):
    converter = MarkdownConverter(strip_document=None, **options)
    output = converter.convert(html)
    return converter, output


def test_marker_clauses_attach_to_rate_table():
    # Every starred quote links to its clause, rendered once as a footnote.
    assert md(rate_table) == (
        '\n\n'
        '| 航线 | 运价 |\n'
        '| --- | --- |\n'
        '| 上海-汉堡 | 1500[^fn1] |\n'
        '| 上海-鹿特丹 | 1600[^fn1][^fn2] |\n'
        '\n'
        '[^fn1]: 含燃油附加费，旺季另计。\n'
        '[^fn2]: 需提前 7 天订舱。\n\n'
    )


def test_one_cell_can_carry_two_conditions():
    # The Rotterdam quote is jointly limited by both clauses.
    output = md(rate_table)
    assert '1600[^fn1][^fn2]' in output
    assert output.count('[^fn1]:') == 1
    assert output.count('[^fn2]:') == 1


def test_repeated_marker_symbol_is_scoped_per_table():
    # The bare '*' is not itself a source; each table resolves to its own clause.
    output = md(rate_table + second_rate_table)
    assert '1500[^fn1]' in output
    assert '2000[^fn3]' in output
    assert '[^fn1]: 含燃油附加费，旺季另计。' in output
    assert '[^fn3]: 含码头操作费。' in output
    # Both quotes in the first table point at the same first-table clause.
    assert output.count('[^fn1]:') == 1
    assert '1500[^fn1]' in output
    assert '1600[^fn1][^fn2]' in output


def test_combined_markers_in_one_sup_split_into_refs():
    html = (
        '<table><tr><td>100<sup>*†</sup></td></tr></table>'
        '<p>* 燃油费。</p><p>† 旺季费。</p>'
    )
    assert md(html) == (
        '\n\n|  |\n| --- |\n| 100[^fn1][^fn2] |\n\n'
        '[^fn1]: 燃油费。\n[^fn2]: 旺季费。\n\n'
    )


def test_explicit_anchor_footnotes_follow_href():
    assert md(explicit_footnotes) == (
        '\n\n报价 100[^fn1] 和 200[^fn2]\n\n'
        '[^fn1]: 最低一票适用于该报价。\n'
        '[^fn2]: 需签署年度协议。\n\n'
    )


def test_numeric_marker_note_under_table():
    html = (
        '<table><tr><td>100<sup>1</sup></td></tr></table>'
        '<p>1. 含税价。</p>'
    )
    assert md(html) == (
        '\n\n|  |\n| --- |\n| 100[^fn1] |\n\n'
        '[^fn1]: 含税价。\n\n'
    )


def test_marker_note_wrapped_in_list():
    html = (
        '<table><tr><td>1<sup>#</sup></td></tr></table>'
        '<ul><li># 列表形式的表注。</li></ul>'
    )
    assert md(html) == (
        '\n\n|  |\n| --- |\n| 1[^fn1] |\n\n'
        '[^fn1]: 列表形式的表注。\n\n'
    )


def test_note_can_reference_another_regional_exception():
    html = (
        '<table><tr><td>100<sup>*</sup></td><td>200<sup>†</sup></td></tr></table>'
        '<p>* 标准附加费；北欧港口适用 †。</p>'
        '<p>† 北欧地区额外征收港口费。</p>'
    )
    output = md(html)
    assert '100[^fn1]' in output
    assert '200[^fn2]' in output
    assert '[^fn1]: 标准附加费；北欧港口适用 †。' in output
    assert '[^fn2]: 北欧地区额外征收港口费。' in output


def test_note_to_note_refs_become_cross_links_including_cycles():
    html = (
        '<table><tr><td>100<sup>*</sup></td><td>200<sup>†</sup></td></tr></table>'
        '<p>* 标准费，北欧改按 †<sup>†</sup> 执行。</p>'
        '<p>† 北欧例外；标准情形回到 *<sup>*</sup>。</p>'
    )
    output = md(html)
    assert '[^fn1]: 标准费，北欧改按 †[^fn2] 执行。' in output
    assert '[^fn2]: 北欧例外；标准情形回到 \\*[^fn1]。' in output


def test_multiline_definition_keeps_continuation_indent():
    html = (
        '<table><tr><td>100<sup>*</sup></td></tr></table>'
        '<p>* 基础附加费；<br>分两档收取。</p>'
    )
    output = md(html)
    assert '[^fn1]: 基础附加费；  \n    分两档收取。' in output


def test_external_page_leaves_unresolved_link_on_quote():
    html = (
        '<table><tr><td>100'
        '<sup><a href="surcharge.html#fn-s">*</a></sup>'
        '</td></tr></table>'
    )
    converter, output = _converter(html)
    assert output == (
        '\n\n|  |\n| --- |\n| 100[\\*](surcharge.html#fn-s) |\n\n'
    )
    unresolved = converter.footnote_unresolved
    assert unresolved['surcharge.html#fn-s']['external'] is True


def test_broken_internal_anchor_stays_on_quote():
    html = (
        '<table><tr><td>100<sup><a href="#missing">1</a></sup></td></tr></table>'
    )
    converter, output = _converter(html)
    assert output == '\n\n|  |\n| --- |\n| 100[1](#missing) |\n\n'
    assert '#missing' in converter.footnote_unresolved


def test_dangling_marker_without_clause_is_preserved():
    html = '<p>报价 <b>900</b><sup>*</sup></p>'
    converter, output = _converter(html)
    assert output == '\n\n报价 **900**\\*\n\n'
    assert '*' in converter.footnote_unresolved


def test_uncited_clause_remains_in_document_flow():
    html = (
        '<table><tr><td>100<sup>*</sup></td></tr></table>'
        '<p>* 被引用的注。</p>'
        '<p>† 没有任何报价引用的注。</p>'
    )
    output = md(html)
    assert output == (
        '\n\n|  |\n| --- |\n| 100[^fn1] |\n\n'
        '† 没有任何报价引用的注。\n\n'
        '[^fn1]: 被引用的注。\n\n'
    )


def test_marker_paragraph_without_table_is_not_treated_as_note():
    html = '<p>* 普通项目符号段落，与表格无关。</p><p>正文。</p>'
    converter, output = _converter(html)
    assert output == '\n\n\\* 普通项目符号段落，与表格无关。\n\n正文。\n\n'
    assert converter.footnote_unresolved == {}


def test_ordinary_numbered_list_is_not_treated_as_notes():
    # The cell marker stays unresolved instead of swallowing list numbering.
    html = (
        '<table><tr><td>运价 100<sup>1</sup></td></tr></table>'
        '<ol><li>1. 订舱流程第一步。</li><li>2. 订舱流程第二步。</li></ol>'
    )
    converter, output = _converter(html)
    assert '订舱流程第一步' in output
    assert '[^' not in output
    assert '1' in converter.footnote_unresolved


def test_feature_can_be_disabled():
    html = '<p>x<sup>*</sup></p><table><tr><td>1<sup>*</sup></td></tr></table>'
    output = md(html, footnotes=False)
    assert '[^' not in output
    assert ']: ' not in output
    # Marker paragraphs render inline normally when footnotes are off.
    assert md('<p>x<sup>2</sup></p>', footnotes=False, sup_symbol='^') == (
        '\n\nx^2^\n\n'
    )


def test_ordinary_superscripts_outside_tables_are_unchanged():
    converter, output = _converter('<p>x<sup>2</sup> + y<sup>10</sup></p>')
    assert output == '\n\nx2 + y10\n\n'
    assert converter.footnote_unresolved == {}


def test_preformatted_and_code_content_is_never_rewritten():
    converter, output = _converter(
        '<p>公式 <code>a<sup>*</sup></code></p>'
        '<pre>x<sup>1</sup></pre>'
    )
    assert '`a*`' in output
    assert 'x1' in output
    assert '[^' not in output
    assert converter.footnote_unresolved == {}


def test_converter_reuse_resets_footnote_state():
    converter = MarkdownConverter(strip_document=None)
    html = '<table><tr><td>1<sup>*</sup></td></tr></table><p>* 注A。</p>'
    first = converter.convert(html)
    second = converter.convert(html)
    assert first == second
    assert first.count('[^fn1]:') == 1
    assert '1[^fn1]' in first
    assert converter.footnote_unresolved == {}
