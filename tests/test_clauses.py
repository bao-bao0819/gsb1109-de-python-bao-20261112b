"""
Clause collection mode (``clauses`` option).

Ordered lists used as numbered clauses (e.g. association bylaws) must keep the
same numbers in the converted text as on the web page, including ``start``,
``reversed`` and per-item ``value``. Each nested ordered list keeps its own
counter while every item still carries its full clause-number path, and
explanation paragraphs between clauses never consume a number.
"""
from markdownify import markdownify

from .utils import md


def test_clauses_honor_ol_start():
    assert md('<ol start="3"><li>a</li><li>b</li></ol>', clauses=True) == (
        '\n\n3. a\n4. b\n'
    )


def test_clauses_honor_ol_reversed_with_start():
    html = '<ol reversed start="5"><li>a</li><li>b</li><li>c</li></ol>'
    assert md(html, clauses=True) == '\n\n5. a\n4. b\n3. c\n'


def test_clauses_reversed_without_start_counts_only_li():
    # The reversed list counts down from the number of direct <li> children;
    # the explanation <p> sandwiched between clauses is not counted.
    html = '<ol reversed><li>a</li><p>note</p><li>b</li><li>c</li></ol>'
    assert md(html, clauses=True) == (
        '\n\n3. a\n\nnote\n\n2. b\n1. c\n'
    )


def test_clauses_li_value_wins_and_following_items_continue():
    html = '<ol><li>a</li><li value="8">b</li><li>c</li></ol>'
    assert md(html, clauses=True) == '\n\n1. a\n8. b\n9. c\n'


def test_clauses_li_value_inside_reversed_list():
    html = '<ol reversed start="9"><li>a</li><li value="4">b</li><li>c</li></ol>'
    assert md(html, clauses=True) == '\n\n9. a\n4. b\n3. c\n'


def test_clauses_explanation_paragraph_does_not_consume_number():
    html = '<ol><li>a</li><p>note</p><li>b</li></ol>'
    assert md(html, clauses=True) == '\n\n1. a\n\nnote\n\n2. b\n'


def test_clauses_nested_list_has_own_counter_and_full_path():
    html = (
        '<ol start="5">'
        '<li>a<ol><li>x</li><li>y</li></ol></li>'
        '<li>b</li>'
        '</ol>'
    )
    assert md(html, clauses=True) == (
        '\n\n5. a\n   5.1. x\n   5.2. y\n6. b\n'
    )


def test_clauses_deeply_nested_paths():
    html = (
        '<ol start="2">'
        '<li>a<ol><li>b<ol><li>c</li><li>d</li></ol></li></ol></li>'
        '<li>e</li>'
        '</ol>'
    )
    assert md(html, clauses=True) == (
        '\n\n2. a\n   2.1. b\n        2.1.1. c\n        2.1.2. d\n3. e\n'
    )


def test_clauses_nested_reversed_list_numbers_independently():
    html = (
        '<ol>'
        '<li>a<ol reversed start="4"><li>x</li><li>y</li></ol></li>'
        '</ol>'
    )
    assert md(html, clauses=True) == '\n\n1. a\n   1.4. x\n   1.3. y\n'


def test_clauses_invalid_start_and_value_fall_back_to_defaults():
    assert md('<ol start="foo"><li>a</li><li>b</li></ol>', clauses=True) == (
        '\n\n1. a\n2. b\n'
    )
    assert md(
        '<ol reversed start="x"><li>a</li><li>b</li></ol>', clauses=True
    ) == '\n\n2. a\n1. b\n'


def test_clauses_mode_is_opt_in():
    # Without the option the legacy numbering ignores reversed/value entirely.
    assert md('<ol reversed start="5"><li>a</li><li>b</li></ol>') == (
        '\n\n5. a\n6. b\n'
    )
    assert md('<ol><li>a</li><li value="8">b</li></ol>') == (
        '\n\n1. a\n2. b\n'
    )
    assert md('<ol start="3"><li>a</li><li>b</li></ol>') == (
        '\n\n3. a\n4. b\n'
    )


def test_clauses_available_on_markdownify_function():
    html = '<ol reversed><li>a</li><li>b</li></ol>'
    assert markdownify(html, clauses=True) == '2. a\n1. b'


def test_clauses_charter_end_to_end():
    html = (
        '<ol start="10">'
        '<li>membership'
        '<ol><li>application</li><li>approval</li></ol>'
        '</li>'
        '<p>explanatory paragraph between clauses</p>'
        '<li value="20">meetings</li>'
        '<li>amendments</li>'
        '</ol>'
    )
    assert md(html, clauses=True) == (
        '\n\n10. membership\n'
        '    10.1. application\n'
        '    10.2. approval\n'
        '\n'
        'explanatory paragraph between clauses\n'
        '\n'
        '20. meetings\n'
        '21. amendments\n'
    )
