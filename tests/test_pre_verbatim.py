from markdownify import MarkdownConverter, extract_pre_text, markdownify
from bs4 import BeautifulSoup

from .utils import md


def line_number_and_soft_wrap(el):
    """Vendor predicate: line-number spans and soft-wrapping <br> are decoration."""
    classes = el.get('class', []) if hasattr(el, 'get') else []
    is_line_number = el.name == 'span' and 'lineno' in classes
    is_soft_wrap = el.name == 'br' and 'softwrap' in classes
    return is_line_number or is_soft_wrap


def fenced_content(result):
    """Return the text inside the outermost ``` fence produced by convert_pre."""
    opening = result.index('```') + 3
    closing = result.rindex('```')
    inner = result[opening:closing]
    lines = inner.split('\n')
    return '\n'.join(lines[1:-1])


def test_pre_text_callback_drops_line_number_spans():
    html = (
        '<pre>'
        '<span class="lineno">1</span><span class="k">def</span> f():\n'
        '<span class="lineno">2</span>    <span class="k">return</span> <span class="mi">42</span>'
        '</pre>'
    )
    result = md(html, pre_text_callback=line_number_and_soft_wrap)
    assert result == '\n\n```\ndef f():\n    return 42\n```\n\n'


def test_pre_text_callback_treats_decorated_br_as_soft_wrap():
    html = (
        '<pre>long_expression = first_part<br class="softwrap" />'
        '    + second_part<br class="softwrap" />'
        '    + third_part</pre>'
    )
    result = md(html, pre_text_callback=line_number_and_soft_wrap)
    assert result == (
        '\n\n```\nlong_expression = first_part    + second_part    + third_part\n```\n\n'
    )


def test_plain_br_remains_a_real_newline():
    html = '<pre>a<br>b<br/>c</pre>'
    result = md(html, pre_text_callback=line_number_and_soft_wrap)
    assert result == '\n\n```\na\nb\nc\n```\n\n'


def test_code_lines_starting_with_digits_are_preserved():
    html = (
        '<pre>'
        '<span class="lineno">10</span><span class="mi">123</span> apples\n'
        '<span class="lineno">11</span><span class="mi">456</span> oranges'
        '</pre>'
    )
    result = md(html, pre_text_callback=line_number_and_soft_wrap)
    assert result == '\n\n```\n123 apples\n456 oranges\n```\n\n'


def test_spaces_split_across_highlight_tags_are_preserved():
    soup = BeautifulSoup(
        '<pre><span>a </span><span> b</span><span>c  </span><span> d</span></pre>',
        'html.parser',
    )
    assert extract_pre_text(soup.pre) == 'a  bc   d'


def test_blank_lines_and_trailing_backticks_are_preserved():
    original = 'line with `inline`\n\n\nend with ```'
    soup = BeautifulSoup('<pre></pre>', 'html.parser')
    soup.pre.string = original
    assert extract_pre_text(soup.pre) == original

    result = markdownify(str(soup), pre_text_callback=lambda el: False)
    assert result == '```\n%s\n```' % original
    assert fenced_content(result) == original


def test_markdown_fenced_code_round_trips_character_for_character():
    original = (
        '```python\n'
        'def greet(name):\n'
        '    # print a `greeting`\n'
        '\n'
        '\n'
        '    return f"hello {name}"\n'
        '```'
    )
    soup = BeautifulSoup('<pre></pre>', 'html.parser')
    soup.pre.string = original

    result = markdownify(str(soup), pre_text_callback=line_number_and_soft_wrap)
    assert result == '```\n%s\n```' % original
    assert fenced_content(result) == original
    assert list(fenced_content(result)) == list(original)


def test_decorated_manual_html_round_trips_character_for_character():
    original = (
        'row 1 with  double  spaces\n'
        '\n'
        '3rd row starts with digits 007 and a `tick`'
    )
    html = (
        '<pre>'
        '<span class="lineno">1</span><span>row 1 with  double  spaces</span>\n'
        '<span class="lineno">2</span><br class="softwrap" />\n'
        '<span class="lineno">3</span><span>3rd row starts with digits 007 and a `tick`</span>'
        '<span class="lineno">4</span>'
        '</pre>'
    )
    result = markdownify(html, pre_text_callback=line_number_and_soft_wrap)
    assert result == '```\n%s\n```' % original
    assert list(fenced_content(result)) == list(original)


def test_pre_without_callback_keeps_default_conversion():
    html = '<pre><span class="lineno">1</span>x = 1</pre>'
    assert md(html) == '\n\n```\n1x = 1\n```\n\n'


def test_extract_pre_text_ignores_comments():
    soup = BeautifulSoup('<pre>a<!-- comment -->b</pre>', 'html.parser')
    assert extract_pre_text(soup.pre) == 'ab'


def test_pre_text_callback_keeps_code_language_callback():
    def lang_callback(el):
        return el['class'][0] if el.has_attr('class') else None

    html = '<pre class="python"><span class="lineno">1</span>answer = 42</pre>'
    result = md(
        html,
        code_language_callback=lang_callback,
        pre_text_callback=line_number_and_soft_wrap,
    )
    assert result == '\n\n```python\nanswer = 42\n```\n\n'


def test_pre_text_callback_empty_block_returns_empty_string():
    converter = MarkdownConverter(pre_text_callback=line_number_and_soft_wrap)
    assert converter.convert('<pre><span class="lineno">1</span></pre>') == ''
