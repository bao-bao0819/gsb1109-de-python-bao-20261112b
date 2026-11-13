import pytest
from bs4 import BeautifulSoup

from markdownify import MarkdownConverter, PostSegment, post_segments


def segment_dicts(html, **options):
    return [segment.as_dict() for segment in post_segments(html, **options)]


def test_plain_author_text_is_single_own_segment():
    assert segment_dicts('<p>楼主自己写的一段话</p>') == [
        {
            'index': 1,
            'kind': 'own',
            'level': 0,
            'reply_index': 1,
            'text': '楼主自己写的一段话',
        }
    ]


def test_quote_then_reply():
    html = '<blockquote><p>别人说的话</p></blockquote><p>本楼的回复</p>'
    assert segment_dicts(html) == [
        {
            'index': 1,
            'kind': 'quote',
            'level': 1,
            'reply_index': 0,
            'text': '别人说的话',
        },
        {
            'index': 2,
            'kind': 'own',
            'level': 0,
            'reply_index': 1,
            'text': '本楼的回复',
        },
    ]


def test_nested_quote_levels():
    html = '<blockquote>一层文字<blockquote>二层文字<blockquote>三层文字</blockquote></blockquote></blockquote>'
    assert segment_dicts(html) == [
        {'index': 1, 'kind': 'quote', 'level': 1, 'reply_index': 0, 'text': '一层文字'},
        {'index': 2, 'kind': 'quote', 'level': 2, 'reply_index': 0, 'text': '二层文字'},
        {'index': 3, 'kind': 'quote', 'level': 3, 'reply_index': 0, 'text': '三层文字'},
    ]


def test_reply_between_two_quotes_stays_in_place():
    html = (
        '<p>开场</p>'
        '<blockquote>'
        '<blockquote><p>第一段引用</p></blockquote>'
        '<p>夹在两段引用之间的回复</p>'
        '<blockquote><p>第二段引用</p></blockquote>'
        '</blockquote>'
        '<p>收尾</p>'
    )
    segments = post_segments(html)
    assert [segment.text for segment in segments] == [
        '开场',
        '第一段引用',
        '夹在两段引用之间的回复',
        '第二段引用',
        '收尾',
    ]
    assert [(segment.kind, segment.level, segment.reply_index) for segment in segments] == [
        ('own', 0, 1),
        ('quote', 2, 0),
        ('quote', 1, 0),
        ('quote', 2, 0),
        ('own', 0, 2),
    ]


def test_formula_starting_with_gt_is_not_a_quote():
    html = (
        '<p>看这个公式：&gt; a^2 + b^2 = c^2 说完了</p>'
        '<blockquote><p>真引用一句</p><p>&gt; E = mc^2</p></blockquote>'
    )
    segments = post_segments(html)
    assert segments[0].kind == 'own'
    assert segments[0].level == 0
    assert segments[0].text == '看这个公式：> a^2 + b^2 = c^2 说完了'
    assert segments[1].kind == 'quote'
    assert segments[1].level == 1
    assert '> E = mc^2' in segments[1].text
    assert len(segments) == 2


def test_reply_index_counts_own_runs_only():
    html = (
        '<p>第一句</p>'
        '<blockquote>引用一</blockquote>'
        '<p>第二句回复</p>'
        '<blockquote>引用二</blockquote>'
        '<p>第三句回复</p>'
    )
    segments = post_segments(html)
    own = [segment for segment in segments if segment.is_own]
    assert [segment.reply_index for segment in own] == [1, 2, 3]
    assert all(segment.reply_index == 0 for segment in segments if segment.is_quote)


def test_sibling_blockquotes_are_separate_segments():
    html = '<blockquote>引用甲</blockquote><blockquote>引用乙</blockquote><p>作者的话</p>'
    assert segment_dicts(html) == [
        {'index': 1, 'kind': 'quote', 'level': 1, 'reply_index': 0, 'text': '引用甲'},
        {'index': 2, 'kind': 'quote', 'level': 1, 'reply_index': 0, 'text': '引用乙'},
        {'index': 3, 'kind': 'own', 'level': 0, 'reply_index': 1, 'text': '作者的话'},
    ]


def test_paragraphs_inside_one_blockquote_merge():
    html = '<blockquote><p>引用第一段</p><p>引用第二段</p></blockquote>'
    segments = post_segments(html)
    assert len(segments) == 1
    assert segments[0].level == 1
    assert segments[0].text == '引用第一段\n引用第二段'


def test_inline_markup_is_stripped():
    html = '<p>加粗 <b>重点</b> 与 <a href="http://example.com">链接文字</a></p>'
    segments = post_segments(html)
    assert len(segments) == 1
    assert segments[0].text == '加粗 重点 与 链接文字'


def test_br_keeps_single_segment_with_newline():
    segments = post_segments('<p>第一行<br>第二行</p>')
    assert len(segments) == 1
    assert segments[0].text == '第一行\n第二行'


def test_pre_content_keeps_indentation():
    html = '<p>代码如下</p><pre>def f():\n    return 1\n</pre>'
    segments = post_segments(html)
    assert segments[1].kind == 'own'
    assert segments[1].text == 'def f():\n    return 1'


def test_pre_inside_blockquote_is_quote_segment():
    html = '<blockquote><pre>nested\n  code</pre></blockquote>'
    segments = post_segments(html)
    assert len(segments) == 1
    assert segments[0].kind == 'quote'
    assert segments[0].level == 1
    assert segments[0].text == 'nested\n  code'


def test_empty_and_blank_documents():
    assert post_segments('') == []
    assert post_segments('<p></p><blockquote>   </blockquote>') == []


def test_script_and_style_contents_are_ignored():
    html = (
        '<style>.foo { color: red; }</style>'
        '<p>正文</p>'
        '<script>var secret = 1;</script>'
    )
    segments = post_segments(html)
    assert len(segments) == 1
    assert segments[0].text == '正文'


def test_indices_are_one_based_and_sequential():
    html = (
        '<p>第一句</p>'
        '<blockquote>引用一</blockquote>'
        '<p>第二句回复</p>'
        '<blockquote>引用二</blockquote>'
    )
    segments = post_segments(html)
    assert [segment.index for segment in segments] == list(range(1, len(segments) + 1))


def test_segment_helpers_and_dict_roundtrip():
    segment = PostSegment(1, 'own', 0, 1, '文字')
    assert segment.is_own
    assert not segment.is_quote
    assert segment.as_dict() == {
        'index': 1,
        'kind': 'own',
        'level': 0,
        'reply_index': 1,
        'text': '文字',
    }
    assert segment == PostSegment(1, 'own', 0, 1, '文字')
    assert segment == segment.as_dict()
    assert segment != PostSegment(2, 'quote', 1, 0, '文字')


def test_converter_accepts_parsed_soup():
    soup = BeautifulSoup('<blockquote>引用</blockquote><p>回复</p>', 'html.parser')
    converter = MarkdownConverter()
    segments = converter.convert_post_segments(soup=soup)
    assert [segment.text for segment in segments] == ['引用', '回复']


def test_converter_requires_html_or_soup():
    with pytest.raises(ValueError):
        MarkdownConverter().convert_post_segments()


def test_original_markdown_output_is_unchanged():
    html = (
        '<blockquote>'
        '<blockquote><p>第一段引用</p></blockquote>'
        '<p>中间的回复</p>'
        '<blockquote><p>第二段引用</p></blockquote>'
        '</blockquote>'
    )
    converter = MarkdownConverter(strip_document=None)
    markdown = converter.convert(html)
    assert markdown == '\n> > 第一段引用\n>\n> 中间的回复\n>\n> > 第二段引用\n\n'
    segments = converter.convert_post_segments(html)
    assert [segment.text for segment in segments] == [
        '第一段引用',
        '中间的回复',
        '第二段引用',
    ]
