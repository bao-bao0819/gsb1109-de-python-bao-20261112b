from markdownify import markdownify_pages


def test_same_named_headings_across_pages():
    pages = [
        ('https://example.com/book/ch1.html',
         '<h2 id="params">参数</h2><p>第一页的参数。</p>'),
        ('https://example.com/book/ch2.html',
         '<h2 id="params">参数</h2><p>细节见<a href="#params">参数</a>。</p>'),
    ]
    text = markdownify_pages(pages)
    # each page gets its own namespaced anchor for the same id
    assert '<a id="ch1--params"></a>' in text
    assert '<a id="ch2--params"></a>' in text
    # the link on page 2 points at page 2's anchor, not page 1's
    assert '[参数](#ch2--params)' in text
    assert '[参数](#ch1--params)' not in text


def test_relative_links_resolve_against_page_url():
    pages = [
        ('https://example.com/book/ch1/index.html',
         '<p>下一章见<a href="../ch2/index.html#sec">第二节</a>。</p>'),
        ('https://example.com/book/ch2/index.html',
         '<h2 id="sec">第二节</h2>'),
    ]
    text = markdownify_pages(pages)
    # both pages are named index.html, so slugs must be disambiguated
    assert '<a id="index--sec"></a>' in text or '<a id="index-2--sec"></a>' in text
    assert ('[第二节](#index--sec)' in text
            or '[第二节](#index-2--sec)' in text)


def test_external_fragment_link_is_not_treated_as_book_anchor():
    pages = [
        ('https://example.com/book/ch1.html',
         '<h2 id="params">参数</h2>'
         '<p>另见<a href="https://other.example/ref.html#params">外站参数</a>，'
         '以及<a href="sub/note.html">站外相对链接</a>。</p>'),
    ]
    text = markdownify_pages(pages)
    # external link keeps its absolute address, fragment untouched
    assert '[外站参数](https://other.example/ref.html#params)' in text
    # relative off-book links are resolved against the page's own URL
    assert '[站外相对链接](https://example.com/book/sub/note.html)' in text


def test_duplicate_id_keeps_original_link_and_source():
    pages = [
        ('https://example.com/book/ch1.html',
         '<p id="dup">第一处。</p><p id="dup">第二处。</p>'
         '<p>回到<a href="#dup">重复锚点</a>。</p>'),
    ]
    text = markdownify_pages(pages)
    # the original link is kept as written
    assert '[重复锚点](#dup)' in text
    # no namespaced anchor is generated for the ambiguous id
    assert 'ch1--dup' not in text
    # an editorial note names the source page
    assert 'https://example.com/book/ch1.html' in text.split('[重复锚点](#dup)')[1]
    assert 'appears 2 times' in text


def test_cross_page_link_without_fragment_targets_page_start():
    pages = [
        ('https://example.com/book/ch1.html',
         '<p>参见<a href="ch2.html">第二章</a>。</p>'),
        ('https://example.com/book/ch2.html', '<p>第二章内容。</p>'),
    ]
    text = markdownify_pages(pages)
    assert '[第二章](#ch2)' in text
    assert '<a id="ch2"></a>' in text
    assert '<!-- source: https://example.com/book/ch2.html -->' in text


def test_relative_image_src_is_resolved():
    pages = [
        ('https://example.com/book/ch1/index.html',
         '<p><img src="img/pic.png" alt="插图" /></p>'),
    ]
    text = markdownify_pages(pages)
    assert '![插图](https://example.com/book/ch1/img/pic.png)' in text


def test_pages_accept_mappings_and_options():
    pages = [
        {'url': 'https://example.com/book/ch1.html',
         'html': '<h2 id="a">甲</h2>'},
        {'url': 'https://example.com/book/ch2.html',
         'html': '<h2 id="a">乙</h2><p><a href="#a">见甲</a></p>'},
    ]
    text = markdownify_pages(pages, heading_style='atx')
    assert '## 甲' in text
    assert '[见甲](#ch2--a)' in text


def test_empty_book():
    assert markdownify_pages([]) == ''
