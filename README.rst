|build| |version| |license| |downloads|

.. |build| image:: https://img.shields.io/github/actions/workflow/status/matthewwithanm/python-markdownify/python-app.yml?branch=develop
    :alt: GitHub Workflow Status
    :target: https://github.com/matthewwithanm/python-markdownify/actions/workflows/python-app.yml?query=workflow%3A%22Python+application%22

.. |version| image:: https://img.shields.io/pypi/v/markdownify
    :alt: Pypi version
    :target: https://pypi.org/project/markdownify/

.. |license| image:: https://img.shields.io/pypi/l/markdownify
    :alt: License
    :target: https://github.com/matthewwithanm/python-markdownify/blob/develop/LICENSE

.. |downloads| image:: https://pepy.tech/badge/markdownify
    :alt: Pypi Downloads
    :target: https://pepy.tech/project/markdownify

Installation
============

``pip install markdownify``


Usage
=====

Convert some HTML to Markdown:

.. code:: python

    from markdownify import markdownify as md
    md('<b>Yay</b> <a href="http://github.com">GitHub</a>')  # > '**Yay** [GitHub](http://github.com)'

Specify tags to exclude:

.. code:: python

    from markdownify import markdownify as md
    md('<b>Yay</b> <a href="http://github.com">GitHub</a>', strip=['a'])  # > '**Yay** GitHub'

\...or specify the tags you want to include:

.. code:: python

    from markdownify import markdownify as md
    md('<b>Yay</b> <a href="http://github.com">GitHub</a>', convert=['b'])  # > '**Yay** GitHub'


Options
=======

Markdownify supports the following options:

strip
  A list of tags to strip. This option can't be used with the
  ``convert`` option.

convert
  A list of tags to convert. This option can't be used with the
  ``strip`` option.

autolinks
  A boolean indicating whether the "automatic link" style should be used when
  a ``a`` tag's contents match its href. Defaults to ``True``.

default_title
  A boolean to enable setting the title of a link to its href, if no title is
  given. Defaults to ``False``.

heading_style
  Defines how headings should be converted. Accepted values are ``ATX``,
  ``ATX_CLOSED``, ``SETEXT``, and ``UNDERLINED`` (which is an alias for
  ``SETEXT``). Defaults to ``UNDERLINED``.

bullets
  An iterable (string, list, or tuple) of bullet styles to be used. If the
  iterable only contains one item, it will be used regardless of how deeply
  lists are nested. Otherwise, the bullet will alternate based on nesting
  level. Defaults to ``'*+-'``.

strong_em_symbol
  In markdown, both ``*`` and ``_`` are used to encode **strong** or
  *emphasized* texts. Either of these symbols can be chosen by the options
  ``ASTERISK`` (default) or ``UNDERSCORE`` respectively.

sub_symbol, sup_symbol
  Define the chars that surround ``<sub>`` and ``<sup>`` text. Defaults to an
  empty string, because this is non-standard behavior. Could be something like
  ``~`` and ``^`` to result in ``~sub~`` and ``^sup^``.  If the value starts
  with ``<`` and ends with ``>``, it is treated as an HTML tag and a ``/`` is
  inserted after the ``<`` in the string used after the text; this allows
  specifying ``<sub>`` to use raw HTML in the output for subscripts, for
  example.

newline_style
  Defines the style of marking linebreaks (``<br>``) in markdown. The default
  value ``SPACES`` of this option will adopt the usual two spaces and a newline,
  while ``BACKSLASH`` will convert a linebreak to ``\\n`` (a backslash and a
  newline). While the latter convention is non-standard, it is commonly
  preferred and supported by a lot of interpreters.

code_language
  Defines the language that should be assumed for all ``<pre>`` sections.
  Useful, if all code on a page is in the same programming language and
  should be annotated with `````python`` or similar.
  Defaults to ``''`` (empty string) and can be any string.

code_language_callback
  When the HTML code contains ``pre`` tags that in some way provide the code
  language, for example as class, this callback can be used to extract the
  language from the tag and prefix it to the converted ``pre`` tag.
  The callback gets one single argument, a BeautifulSoup object, and returns
  a string containing the code language, or ``None``.
  An example to use the class name as code language could be::

    def callback(el):
        return el['class'][0] if el.has_attr('class') else None

  Defaults to ``None``.

escape_asterisks
  If set to ``False``, do not escape ``*`` to ``\*`` in text.
  Defaults to ``True``.

escape_underscores
  If set to ``False``, do not escape ``_`` to ``\_`` in text.
  Defaults to ``True``.

escape_misc
  If set to ``True``, escape miscellaneous punctuation characters
  that sometimes have Markdown significance in text.
  Defaults to ``False``.

keep_inline_images_in
  Images are converted to their alt-text when the images are located inside
  headlines or table cells. If some inline images should be converted to
  markdown images instead, this option can be set to a list of parent tags
  that should be allowed to contain inline images, for example ``['td']``.
  Defaults to an empty list.

table_infer_header
  Controls handling of tables with no header row (as indicated by ``<thead>``
  or ``<th>``). When set to ``True``, the first body row is used as the header row.
  Defaults to ``False``, which leaves the header row empty.

wrap, wrap_width
  If ``wrap`` is set to ``True``, all text paragraphs are wrapped at
  ``wrap_width`` characters. Defaults to ``False`` and ``80``.
  Use with ``newline_style=BACKSLASH`` to keep line breaks in paragraphs.
  A `wrap_width` value of `None` reflows lines to unlimited line length.

strip_document
  Controls whether leading and/or trailing separation newlines are removed from
  the final converted document. Supported values are ``LSTRIP`` (leading),
  ``RSTRIP`` (trailing), ``STRIP`` (both), and ``None`` (neither). Newlines
  within the document are unaffected.
  Defaults to ``STRIP``.

strip_pre
  Controls whether leading/trailing blank lines are removed from ``<pre>``
  tags. Supported values are ``STRIP`` (all leading/trailing blank lines),
  ``STRIP_ONE`` (one leading/trailing blank line), and ``None`` (neither).
  Defaults to ``STRIP``.

bs4_options
  Specify additional configuration options for the ``BeautifulSoup`` object
  used to interpret the HTML markup. String and list values (such as ``lxml``
  or ``html5lib``) are treated as ``features`` arguments to control parser
  selection. Dictionary values (such as ``{"from_encoding": "iso-8859-8"}``)
  are treated as full kwargs to be used for the BeautifulSoup constructor,
  allowing specification of any parameter. For parameter details, see the
  Beautiful Soup documentation at:

.. _BeautifulSoup: https://www.crummy.com/software/BeautifulSoup/bs4/doc/

Options may be specified as kwargs to the ``markdownify`` function, or as a
nested ``Options`` class in ``MarkdownConverter`` subclasses.


Converting BeautifulSoup objects
================================

.. code:: python

    from markdownify import MarkdownConverter

    # Create shorthand method for conversion
    def md(soup, **options):
        return MarkdownConverter(**options).convert_soup(soup)


Extracting forum post segments
==============================

When old forum posts are fed into a search library, only the text written by
the post author should be indexed, while quoted text is kept as context.
``post_segments`` (or ``MarkdownConverter.convert_post_segments``) splits a
post into an ordered list of ``PostSegment`` objects. Each segment records:

- ``index``: its 1-based position in document order,
- ``kind``: ``"own"`` for the author's own text, ``"quote"`` for quoted text,
- ``level``: the ``<blockquote>`` nesting depth (0 for own text, 1 for a
  direct quote, 2 for a quote inside a quote, and so on),
- ``reply_index``: the 1-based number of the author reply an ``"own"`` segment
  belongs to; every maximal run of the author's text between two quotes is one
  reply and keeps its original position (always 0 for quotes),
- ``text``: the plain-text content, with inline markup removed.

Quote depth is derived from the HTML structure, so a formula line that
literally starts with a ``>`` is never mistaken for a quote.

.. code:: python

    from markdownify import post_segments

    html = (
        '<p>开场</p>'
        '<blockquote>'
        '<blockquote><p>第一段引用</p></blockquote>'
        '<p>夹在两段引用之间的回复</p>'
        '<blockquote><p>第二段引用</p></blockquote>'
        '</blockquote>'
        '<p>看公式：&gt; a^2 + b^2 = c^2</p>'
    )

    for segment in post_segments(html):
        print(segment.as_dict())

    # {'index': 1, 'kind': 'own',   'level': 0, 'reply_index': 1, 'text': '开场'}
    # {'index': 2, 'kind': 'quote', 'level': 2, 'reply_index': 0, 'text': '第一段引用'}
    # {'index': 3, 'kind': 'quote', 'level': 1, 'reply_index': 0, 'text': '夹在两段引用之间的回复'}
    # {'index': 4, 'kind': 'quote', 'level': 2, 'reply_index': 0, 'text': '第二段引用'}
    # {'index': 5, 'kind': 'own',   'level': 0, 'reply_index': 2, 'text': '看公式：> a^2 + b^2 = c^2'}

    # Index only what the author wrote, quotes stay available as context.
    own_text = [s.text for s in post_segments(html) if s.is_own]


Creating Custom Converters
==========================

If you have a special usecase that calls for a special conversion, you can
always inherit from ``MarkdownConverter`` and override the method you want to
change.
The function that handles a HTML tag named ``abc`` is called
``convert_abc(self, el, text, parent_tags)`` and returns a string
containing the converted HTML tag.
The ``MarkdownConverter`` object will handle the conversion based on the
function names:

.. code:: python

    from markdownify import MarkdownConverter

    class ImageBlockConverter(MarkdownConverter):
        """
        Create a custom MarkdownConverter that adds two newlines after an image
        """
        def convert_img(self, el, text, parent_tags):
            return super().convert_img(el, text, parent_tags) + '\n\n'

    # Create shorthand method for conversion
    def md(html, **options):
        return ImageBlockConverter(**options).convert(html)

.. code:: python

    from markdownify import MarkdownConverter

    class IgnoreParagraphsConverter(MarkdownConverter):
        """
        Create a custom MarkdownConverter that ignores paragraphs
        """
        def convert_p(self, el, text, parent_tags):
            return ''

    # Create shorthand method for conversion
    def md(html, **options):
        return IgnoreParagraphsConverter(**options).convert(html)


Command Line Interface
======================

Use ``markdownify example.html > example.md`` or pipe input from stdin
(``cat example.html | markdownify > example.md``).
Call ``markdownify -h`` to see all available options.
They are the same as listed above and take the same arguments.


Development
===========

To run tests and the linter run ``pip install tox`` once, then ``tox``.
