from bs4 import BeautifulSoup, Comment, Doctype, NavigableString, Tag
from textwrap import fill
import re


# General-purpose regex patterns
re_convert_heading = re.compile(r'convert_h(\d+)')
re_line_with_content = re.compile(r'^(.*)', flags=re.MULTILINE)
re_whitespace = re.compile(r'[\t ]+')
re_all_whitespace = re.compile(r'[\t \r\n]+')
re_newline_whitespace = re.compile(r'[\t \r\n]*[\r\n][\t \r\n]*')
re_html_heading = re.compile(r'h(\d+)')
re_pre_lstrip1 = re.compile(r'^ *\n')
re_pre_rstrip1 = re.compile(r'\n *$')
re_pre_lstrip = re.compile(r'^[ \n]*\n')
re_pre_rstrip = re.compile(r'[ \n]*$')

# Pattern for creating convert_<tag> function names from tag names
re_make_convert_fn_name = re.compile(r'[\[\]:-]')

# Extract (leading_nl, content, trailing_nl) from a string
# (functionally equivalent to r'^(\n*)(.*?)(\n*)$', but greedy is faster than reluctant here)
re_extract_newlines = re.compile(r'^(\n*)((?:.*[^\n])?)(\n*)$', flags=re.DOTALL)

# Escape miscellaneous special Markdown characters
re_escape_misc_chars = re.compile(r'([]\\&<`[>~=+|])')

# Escape sequence of one or more consecutive '-', preceded
# and followed by whitespace or start/end of fragment, as it
# might be confused with an underline of a header, or with a
# list marker
re_escape_misc_dash_sequences = re.compile(r'(\s|^)(-+(?:\s|$))')

# Escape sequence of up to six consecutive '#', preceded
# and followed by whitespace or start/end of fragment, as
# it might be confused with an ATX heading
re_escape_misc_hashes = re.compile(r'(\s|^)(#{1,6}(?:\s|$))')

# Escape '.' or ')' preceded by up to nine digits, as it might be
# confused with a list item
re_escape_misc_list_items = re.compile(r'((?:\s|^)[0-9]{1,9})([.)](?:\s|$))')

# Find consecutive backtick sequences in a string
re_backtick_runs = re.compile(r'`+')

# Split part lists such as "A B", "A,B" or "A、B" (used for data-part attributes)
re_part_split = re.compile(r'[\s,，、;；]+')

# Normalize plate/figure references for lookup (e.g. "Fig. 1-B" -> "fig1b")
re_plate_ref_non_alnum = re.compile(r'[^0-9a-z]+')

# Strip leading reference words ("fig", "figure", "plate") from figure keys
re_figure_ref_prefix = re.compile(r'^(?:figure|fig|plate)')

# Detect an explicit plate label at the start of a caption text, e.g.
# "B: ...", "(A) ...", "A and B: ..." or "Fig. 2B: ...". The label may
# reference several parts ("A and B") because one caption may describe
# multiple exhibits. Part letters are uppercase A-Z.
re_caption_label = re.compile(
    r'^\s*'
    r'(?:(?i:fig(?:ure)?s?|plate|图版)\.?\s*)?'  # optional "Fig."/"Plate" prefix
    r'(\d{1,4})?'                                # optional figure number
    r'\s*'
    r'(?:'
    r'\(\s*([A-Z](?:\s*(?:,|&|、|和|(?i:and))\s*[A-Z])*)\s*\)\s*[:：.]?'  # "(A)", "(A, B)"
    r'|'
    r'([A-Z](?:\s*(?:,|&|、|和|(?i:and))\s*[A-Z])*)\s*[:：.]'             # "A:", "A and B."
    r')'
    r'(?=\s|$)'
)

# Heading styles
ATX = 'atx'
ATX_CLOSED = 'atx_closed'
UNDERLINED = 'underlined'
SETEXT = UNDERLINED

# Newline style
SPACES = 'spaces'
BACKSLASH = 'backslash'

# Strong and emphasis style
ASTERISK = '*'
UNDERSCORE = '_'

# Document/pre strip styles
LSTRIP = 'lstrip'
RSTRIP = 'rstrip'
STRIP = 'strip'
STRIP_ONE = 'strip_one'


def strip1_pre(text):
    """Strip one leading and trailing newline from a <pre> string."""
    text = re_pre_lstrip1.sub('', text)
    text = re_pre_rstrip1.sub('', text)
    return text


def strip_pre(text):
    """Strip all leading and trailing newlines from a <pre> string."""
    text = re_pre_lstrip.sub('', text)
    text = re_pre_rstrip.sub('', text)
    return text


def chomp(text):
    """
    If the text in an inline tag like b, a, or em contains a leading or trailing
    space, strip the string and return a space as suffix of prefix, if needed.
    This function is used to prevent conversions like
        <b> foo</b> => ** foo**
    """
    prefix = ' ' if text and text[0] == ' ' else ''
    suffix = ' ' if text and text[-1] == ' ' else ''
    text = text.strip()
    return (prefix, suffix, text)


def abstract_inline_conversion(markup_fn):
    """
    This abstracts all simple inline tags like b, em, del, ...
    Returns a function that wraps the chomped text in a pair of the string
    that is returned by markup_fn, with '/' inserted in the string used after
    the text if it looks like an HTML tag. markup_fn is necessary to allow for
    references to self.strong_em_symbol etc.
    """
    def implementation(self, el, text, parent_tags):
        markup_prefix = markup_fn(self)
        if markup_prefix.startswith('<') and markup_prefix.endswith('>'):
            markup_suffix = '</' + markup_prefix[1:]
        else:
            markup_suffix = markup_prefix
        if '_noformat' in parent_tags:
            return text
        prefix, suffix, text = chomp(text)
        if not text:
            return ''
        return '%s%s%s%s%s' % (prefix, markup_prefix, text, markup_suffix, suffix)
    return implementation


def _todict(obj):
    return dict((k, getattr(obj, k)) for k in dir(obj) if not k.startswith('_'))


def should_remove_whitespace_inside(el):
    """Return to remove whitespace immediately inside a block-level element."""
    if not el or not el.name:
        return False
    if re_html_heading.match(el.name) is not None:
        return True
    return el.name in ('p', 'blockquote',
                       'article', 'div', 'section',
                       'ol', 'ul', 'li',
                       'dl', 'dt', 'dd',
                       'table', 'thead', 'tbody', 'tfoot',
                       'tr', 'td', 'th')


def should_remove_whitespace_outside(el):
    """Return to remove whitespace immediately outside a block-level element."""
    return should_remove_whitespace_inside(el) or (el and el.name == 'pre')


def _is_block_content_element(el):
    """
    In a block context, returns:

    - True for content elements (tags and non-whitespace text)
    - False for non-content elements (whitespace text, comments, doctypes)
    """
    if isinstance(el, Tag):
        return True
    elif isinstance(el, (Comment, Doctype)):
        return False  # (subclasses of NavigableString, must test first)
    elif isinstance(el, NavigableString):
        return el.strip() != ''
    else:
        return False


def _prev_block_content_sibling(el):
    """Returns the first previous sibling that is a content element, else None."""
    while el is not None:
        el = el.previous_sibling
        if _is_block_content_element(el):
            return el
    return None


def _next_block_content_sibling(el):
    """Returns the first next sibling that is a content element, else None."""
    while el is not None:
        el = el.next_sibling
        if _is_block_content_element(el):
            return el
    return None


def _split_parts(value):
    """Split a part list such as "A B", "A,B" or "A、B" into normalized labels."""
    if not value:
        return []
    parts = []
    for token in re_part_split.split(str(value)):
        token = token.strip().upper()
        if token and token not in parts:
            parts.append(token)
    return parts


def _parse_caption_label(text):
    """
    Parse an explicit plate label at the start of a caption text.
    Returns (figure_number, part_letters, end_index) or None.
    """
    match = re_caption_label.match(text)
    if not match:
        return None
    number = match.group(1)
    letters = re.findall(r'[A-Z]', match.group(2) or match.group(3) or '')
    if not letters:
        return None
    return number, letters, match.end()


def _format_plate_ref(figure_key, part):
    """Build a fully qualified plate reference such as "fig1B"."""
    return '%s%s' % (figure_key, part) if part else str(figure_key)


def _normalize_plate_ref(plate_ref):
    """Normalize a plate reference for tolerant lookup ("Fig. 1-B" -> "fig1b")."""
    return re_plate_ref_non_alnum.sub('', str(plate_ref).lower())


def _normalize_figure_ref(figure_ref):
    """Normalize a figure reference, ignoring "fig"/"plate" style prefixes."""
    norm = _normalize_plate_ref(figure_ref)
    return re_figure_ref_prefix.sub('', norm)


class FigureCatalogEntry(object):
    """
    One caption together with the plates it may describe.

    ``candidates`` is a tuple of ``(figure_key, part)`` pairs (``part`` is
    None for a whole-figure plate). It holds more than one pair when one
    caption references several exhibits, or when the evidence allows more
    than one interpretation; in that case all candidates are kept rather
    than resolved.
    """

    def __init__(self, caption, candidates, explicit=False, ambiguous=False):
        self.caption = caption
        self.candidates = tuple(candidates)
        self.explicit = explicit
        self.ambiguous = ambiguous

    @property
    def plates(self):
        """Fully qualified plate references for all candidates."""
        return [_format_plate_ref(figure, part) for figure, part in self.candidates]

    def to_dict(self):
        return {
            'caption': self.caption,
            'plates': list(self.plates),
            'explicit': self.explicit,
            'ambiguous': self.ambiguous,
        }

    def __repr__(self):
        return 'FigureCatalogEntry(%r, plates=%r)' % (self.caption, self.plates)


class FigureCatalog(object):
    """
    A queryable catalog of figure plates （图版目录） extracted from a document.

    ``figures`` maps each figure key to its part labels; part labels are
    scoped per figure, so the "B" of one figure is never confused with the
    "B" of an adjacent figure. ``entries`` holds the captions found in the
    document; plates without a caption simply have no entry (captions are
    never borrowed from a neighbouring figure to fill a gap).
    """

    def __init__(self, figures, entries):
        self.figures = dict(figures)
        self.entries = list(entries)
        self._index = {}
        for entry in self.entries:
            for plate in entry.plates:
                self._index.setdefault(_normalize_plate_ref(plate), []).append(entry)

    def __iter__(self):
        return iter(self.entries)

    def __len__(self):
        return len(self.entries)

    def plates(self):
        """Return all known plate references, including uncaptioned ones."""
        result = []
        for key, parts in self.figures.items():
            if parts:
                result.extend(_format_plate_ref(key, part) for part in parts)
            else:
                result.append(key)
        return result

    def lookup(self, plate_ref):
        """Return all catalog entries whose candidates include the given plate."""
        return list(self._index.get(_normalize_plate_ref(plate_ref), []))

    def captions_for(self, plate_ref):
        """Return all caption texts associated with the given plate."""
        return [entry.caption for entry in self.lookup(plate_ref)]

    def to_dict(self):
        return {
            'figures': {key: list(parts) for key, parts in self.figures.items()},
            'entries': [entry.to_dict() for entry in self.entries],
        }

    def to_markdown(self):
        """Render the catalog as a Markdown table."""
        lines = ['| Plate | Caption | Match |', '| --- | --- | --- |']
        for entry in self.entries:
            if entry.explicit:
                match = 'explicit'
            elif entry.ambiguous:
                match = 'ambiguous'
            else:
                match = 'inferred'
            caption = entry.caption.replace('|', '\\|')
            lines.append('| %s | %s | %s |' % (', '.join(entry.plates), caption, match))
        return '\n'.join(lines)


def _is_caption_element(el):
    """Return True for elements that hold a figure caption."""
    if not isinstance(el, Tag):
        return False
    if el.name == 'figcaption':
        return True
    classes = el.get('class') or []
    return 'caption' in classes or 'figure-caption' in classes


def _caption_text(el):
    return ' '.join(el.stripped_strings)


def _detect_figure_parts(fig_el):
    """Collect the part labels (A, B, ...) declared inside a figure element."""
    parts = []

    def add(part):
        if part and part not in parts:
            parts.append(part)

    for el in fig_el.find_all(attrs={'data-part': True}):
        for part in _split_parts(el.get('data-part')):
            add(part)
    for el in fig_el.find_all(attrs={'data-parts': True}):
        for part in _split_parts(el.get('data-parts')):
            add(part)
    # nested sub-figures may be labeled by their own figcaption ("A. ...")
    for sub in fig_el.find_all('figure'):
        cap = sub.find('figcaption')
        if cap is not None:
            label = _parse_caption_label(_caption_text(cap))
            if label:
                for part in label[1]:
                    add(part)
    return parts


def _find_inner_captions(fig_el):
    """Find the outermost caption elements contained in a figure element."""
    result = []
    for el in fig_el.find_all(True):
        if not _is_caption_element(el):
            continue
        ancestor = el.parent
        nested = False
        while ancestor is not None and ancestor is not fig_el:
            if _is_caption_element(ancestor):
                nested = True
                break
            ancestor = ancestor.parent
        if not nested:
            result.append(el)
    return result


def _explicit_parts_from_attrs(el, stop_at):
    """Find a data-part/data-parts attribute on el or its ancestors (up to stop_at)."""
    node = el
    while node is not None and node is not stop_at:
        if isinstance(node, Tag):
            for attr in ('data-part', 'data-parts'):
                parts = _split_parts(node.get(attr))
                if parts:
                    return parts
        node = node.parent
    return None


def _whole_figure_candidates(figures, key):
    """Candidates for a caption applying to a whole figure: its parts, if any."""
    for fig_key, _, parts in figures:
        if fig_key == key:
            if parts:
                return [(key, part) for part in parts]
            return [(key, None)]
    return [(key, None)]


def _resolve_figure_key(figures, ref):
    """Match a figure reference (e.g. "2", "fig2", "Plate 2") to a figure key."""
    ref = str(ref)
    for key, _, _ in figures:
        if key == ref:
            return key
    ref_norm = _normalize_figure_ref(ref)
    for key, _, _ in figures:
        if _normalize_figure_ref(key) == ref_norm:
            return key
    return ref


def _adjacent_figures(el, figures):
    """Return the (key, element, parts) of the figures closest before/after el."""
    prev_fig = None
    for fig in el.find_all_previous('figure'):
        if not fig.find_parent('figure'):
            prev_fig = fig
            break
    next_fig = None
    for fig in el.find_all_next('figure'):
        if not fig.find_parent('figure'):
            next_fig = fig
            break
    return [entry for entry in figures
            if entry[1] is prev_fig or entry[1] is next_fig]


def _build_figure_caption_entry(cap_el, fig_el, key, parts, text):
    """Build a catalog entry for a caption element inside a figure."""
    explicit_parts = _explicit_parts_from_attrs(cap_el, fig_el)
    label = _parse_caption_label(text)
    if explicit_parts:
        caption = text[label[2]:].strip() if label else text
        candidates = [(key, part) for part in explicit_parts]
        return FigureCatalogEntry(caption, candidates, explicit=True)
    if label:
        # An explicit label in the caption text ("B: ...", "A and B: ...")
        caption = text[label[2]:].strip()
        candidates = [(key, part) for part in label[1]]
        return FigureCatalogEntry(caption, candidates, explicit=True)
    # Unlabeled caption on the parent figure: it may describe the whole
    # composite, so every part is kept as a candidate.
    if parts:
        candidates = [(key, part) for part in parts]
        return FigureCatalogEntry(text, candidates, ambiguous=True)
    return FigureCatalogEntry(text, [(key, None)])


def _build_standalone_caption_entry(el, figures, text):
    """Build a catalog entry for a labeled caption outside any figure."""
    label = _parse_caption_label(text)
    number = label[0] if label else None
    letters = list(label[1]) if label else []
    caption = text[label[2]:].strip() if label else text

    attr_parts = _split_parts(el.get('data-part') or el.get('data-parts'))
    if attr_parts:
        letters = attr_parts
    attr_figure = el.get('data-figure')

    if attr_figure or number:
        # The caption explicitly names its figure ("Fig. 2B: ...", data-figure).
        key = _resolve_figure_key(figures, attr_figure or number)
        if letters:
            candidates = [(key, part) for part in letters]
        else:
            candidates = _whole_figure_candidates(figures, key)
        return FigureCatalogEntry(caption, candidates, explicit=True)

    if letters:
        # A bare part label ("B: ...") between two figures: the "B" of the
        # previous figure and the "B" of the next one are different plates,
        # and the evidence allows both readings, so keep both candidates.
        candidates = []
        for key, _, _ in _adjacent_figures(el, figures):
            for part in letters:
                if (key, part) not in candidates:
                    candidates.append((key, part))
        return FigureCatalogEntry(caption, candidates,
                                  ambiguous=len(candidates) > 1)

    # No label at all: the caption may belong to either neighbouring figure.
    candidates = []
    for key, _, parts in _adjacent_figures(el, figures):
        for candidate in _whole_figure_candidates(figures, key):
            if candidate not in candidates:
                candidates.append(candidate)
    return FigureCatalogEntry(text, candidates, ambiguous=True)


def _find_standalone_captions(soup):
    """Find labeled caption elements that live outside any figure element."""
    result = []
    for el in soup.find_all(True):
        if el.find_parent('figure'):
            continue
        if not (_is_caption_element(el)
                or el.has_attr('data-figure')
                or el.has_attr('data-part')
                or el.has_attr('data-parts')):
            continue
        if not _caption_text(el):
            continue
        # keep only the outermost element if caption markers are nested
        nested = any(
            ancestor.name != '[document]'
            and (_is_caption_element(ancestor)
                 or ancestor.has_attr('data-figure')
                 or ancestor.has_attr('data-part')
                 or ancestor.has_attr('data-parts'))
            for ancestor in el.parents
        )
        if not nested:
            result.append(el)
    return result


def _extract_figure_catalog(soup):
    """Extract a FigureCatalog from a BeautifulSoup tree."""
    figure_els = [fig for fig in soup.find_all('figure')
                  if not fig.find_parent('figure')]
    figures = []  # list of (key, element, parts)
    for index, fig_el in enumerate(figure_els, 1):
        key = fig_el.get('id') or str(index)
        figures.append((key, fig_el, _detect_figure_parts(fig_el)))

    entries = []
    for key, fig_el, parts in figures:
        for cap_el in _find_inner_captions(fig_el):
            text = _caption_text(cap_el)
            if text:
                entries.append(
                    _build_figure_caption_entry(cap_el, fig_el, key, parts, text))

    if figures:
        for el in _find_standalone_captions(soup):
            entries.append(
                _build_standalone_caption_entry(el, figures, _caption_text(el)))

    return FigureCatalog(
        figures={key: list(parts) for key, _, parts in figures},
        entries=entries)


class MarkdownConverter(object):
    class DefaultOptions:
        autolinks = True
        bs4_options = 'html.parser'
        bullets = '*+-'  # An iterable of bullet types.
        code_language = ''
        code_language_callback = None
        convert = None
        default_title = False
        escape_asterisks = True
        escape_underscores = True
        escape_misc = False
        figure_catalog = False
        heading_style = UNDERLINED
        keep_inline_images_in = []
        newline_style = SPACES
        strip = None
        strip_document = STRIP
        strip_pre = STRIP
        strong_em_symbol = ASTERISK
        sub_symbol = ''
        sup_symbol = ''
        table_infer_header = False
        wrap = False
        wrap_width = 80

    class Options(DefaultOptions):
        pass

    def __init__(self, **options):
        # Create an options dictionary. Use DefaultOptions as a base so that
        # it doesn't have to be extended.
        self.options = _todict(self.DefaultOptions)
        self.options.update(_todict(self.Options))
        self.options.update(options)
        if self.options['strip'] is not None and self.options['convert'] is not None:
            raise ValueError('You may specify either tags to strip or tags to'
                             ' convert, but not both.')

        # If a string or list is passed to bs4_options, assume it is a 'features' specification
        if not isinstance(self.options['bs4_options'], dict):
            self.options['bs4_options'] = {'features': self.options['bs4_options']}

        # Initialize the conversion function cache
        self.convert_fn_cache = {}

        # Figure plate catalog, populated by convert()/convert_soup() when
        # the 'figure_catalog' option is enabled (None otherwise).
        self.figure_catalog = None

    def convert(self, html):
        soup = BeautifulSoup(html, **self.options['bs4_options'])
        return self.convert_soup(soup)

    def convert_soup(self, soup):
        if self.options['figure_catalog']:
            self.figure_catalog = _extract_figure_catalog(soup)
        return self.process_tag(soup, parent_tags=set())

    def process_element(self, node, parent_tags=None):
        if isinstance(node, NavigableString):
            return self.process_text(node, parent_tags=parent_tags)
        else:
            return self.process_tag(node, parent_tags=parent_tags)

    def process_tag(self, node, parent_tags=None):
        # For the top-level element, initialize the parent context with an empty set.
        if parent_tags is None:
            parent_tags = set()

        # Collect child elements to process, ignoring whitespace-only text elements
        # adjacent to the inner/outer boundaries of block elements.
        should_remove_inside = should_remove_whitespace_inside(node)

        def _can_ignore(el):
            if isinstance(el, Tag):
                # Tags are always processed.
                return False
            elif isinstance(el, (Comment, Doctype)):
                # Comment and Doctype elements are always ignored.
                # (subclasses of NavigableString, must test first)
                return True
            elif isinstance(el, NavigableString):
                if str(el).strip() != '':
                    # Non-whitespace text nodes are always processed.
                    return False
                elif should_remove_inside and (not el.previous_sibling or not el.next_sibling):
                    # Inside block elements (excluding <pre>), ignore adjacent whitespace elements.
                    return True
                elif should_remove_whitespace_outside(el.previous_sibling) or should_remove_whitespace_outside(el.next_sibling):
                    # Outside block elements (including <pre>), ignore adjacent whitespace elements.
                    return True
                else:
                    return False
            elif el is None:
                return True
            else:
                raise ValueError('Unexpected element type: %s' % type(el))

        children_to_convert = [el for el in node.children if not _can_ignore(el)]

        # Create a copy of this tag's parent context, then update it to include this tag
        # to propagate down into the children.
        parent_tags_for_children = set(parent_tags)
        parent_tags_for_children.add(node.name)

        # if this tag is a heading or table cell, add an '_inline' parent pseudo-tag
        if (
            re_html_heading.match(node.name) is not None  # headings
            or node.name in {'td', 'th'}  # table cells
        ):
            parent_tags_for_children.add('_inline')

        # if this tag is a preformatted element, add a '_noformat' parent pseudo-tag
        if node.name in {'pre', 'code', 'kbd', 'samp'}:
            parent_tags_for_children.add('_noformat')

        # Convert the children elements into a list of result strings.
        child_strings = [
            self.process_element(el, parent_tags=parent_tags_for_children)
            for el in children_to_convert
        ]

        # Remove empty string values.
        child_strings = [s for s in child_strings if s]

        # Collapse newlines at child element boundaries, if needed.
        if node.name == 'pre' or node.find_parent('pre'):
            # Inside <pre> blocks, do not collapse newlines.
            pass
        else:
            # Collapse newlines at child element boundaries.
            updated_child_strings = ['']  # so the first lookback works
            for child_string in child_strings:
                # Separate the leading/trailing newlines from the content.
                leading_nl, content, trailing_nl = re_extract_newlines.match(child_string).groups()

                # If the last child had trailing newlines and this child has leading newlines,
                # use the larger newline count, limited to 2.
                if updated_child_strings[-1] and leading_nl:
                    prev_trailing_nl = updated_child_strings.pop()  # will be replaced by the collapsed value
                    num_newlines = min(2, max(len(prev_trailing_nl), len(leading_nl)))
                    leading_nl = '\n' * num_newlines

                # Add the results to the updated child string list.
                updated_child_strings.extend([leading_nl, content, trailing_nl])

            child_strings = updated_child_strings

        # Join all child text strings into a single string.
        text = ''.join(child_strings)

        # apply this tag's final conversion function
        convert_fn = self.get_conv_fn_cached(node.name)
        if convert_fn is not None:
            text = convert_fn(node, text, parent_tags=parent_tags)

        return text

    def convert__document_(self, el, text, parent_tags):
        """Final document-level formatting for BeautifulSoup object (node.name == "[document]")"""
        if self.options['strip_document'] == LSTRIP:
            text = text.lstrip('\n')  # remove leading separation newlines
        elif self.options['strip_document'] == RSTRIP:
            text = text.rstrip('\n')  # remove trailing separation newlines
        elif self.options['strip_document'] == STRIP:
            text = text.strip('\n')  # remove leading and trailing separation newlines
        elif self.options['strip_document'] is None:
            pass  # leave leading and trailing separation newlines as-is
        else:
            raise ValueError('Invalid value for strip_document: %s' % self.options['strip_document'])

        return text

    def process_text(self, el, parent_tags=None):
        # For the top-level element, initialize the parent context with an empty set.
        if parent_tags is None:
            parent_tags = set()

        text = str(el)

        # normalize whitespace if we're not inside a preformatted element
        if 'pre' not in parent_tags:
            if self.options['wrap']:
                text = re_all_whitespace.sub(' ', text)
            else:
                text = re_newline_whitespace.sub('\n', text)
                text = re_whitespace.sub(' ', text)

        # escape special characters if we're not inside a preformatted or code element
        if '_noformat' not in parent_tags:
            text = self.escape(text, parent_tags)

        # remove leading whitespace at the start or just after a
        # block-level element; remove traliing whitespace at the end
        # or just before a block-level element.
        if (should_remove_whitespace_outside(el.previous_sibling)
                or (should_remove_whitespace_inside(el.parent)
                    and not el.previous_sibling)):
            text = text.lstrip(' \t\r\n')
        if (should_remove_whitespace_outside(el.next_sibling)
                or (should_remove_whitespace_inside(el.parent)
                    and not el.next_sibling)):
            text = text.rstrip()

        return text

    def get_conv_fn_cached(self, tag_name):
        """Given a tag name, return the conversion function using the cache."""
        # If conversion function is not in cache, add it
        if tag_name not in self.convert_fn_cache:
            self.convert_fn_cache[tag_name] = self.get_conv_fn(tag_name)

        # Return the cached entry
        return self.convert_fn_cache[tag_name]

    def get_conv_fn(self, tag_name):
        """Given a tag name, find and return the conversion function."""
        tag_name = tag_name.lower()

        # Handle strip/convert exclusion options
        if not self.should_convert_tag(tag_name):
            return None

        # Look for an explicitly defined conversion function by tag name first
        convert_fn_name = "convert_%s" % re_make_convert_fn_name.sub("_", tag_name)
        convert_fn = getattr(self, convert_fn_name, None)
        if convert_fn:
            return convert_fn

        # If tag is any heading, handle with convert_hN() function
        match = re_html_heading.match(tag_name)
        if match:
            n = int(match.group(1))  # get value of N from <hN>
            return lambda el, text, parent_tags: self.convert_hN(n, el, text, parent_tags)

        # No conversion function was found
        return None

    def should_convert_tag(self, tag):
        """Given a tag name, return whether to convert based on strip/convert options."""
        strip = self.options['strip']
        convert = self.options['convert']
        if strip is not None:
            return tag not in strip
        elif convert is not None:
            return tag in convert
        else:
            return True

    def escape(self, text, parent_tags):
        if not text:
            return ''
        if self.options['escape_misc']:
            text = re_escape_misc_chars.sub(r'\\\1', text)
            text = re_escape_misc_dash_sequences.sub(r'\1\\\2', text)
            text = re_escape_misc_hashes.sub(r'\1\\\2', text)
            text = re_escape_misc_list_items.sub(r'\1\\\2', text)

        if self.options['escape_asterisks']:
            text = text.replace('*', r'\*')
        if self.options['escape_underscores']:
            text = text.replace('_', r'\_')
        return text

    def underline(self, text, pad_char):
        text = (text or '').rstrip()
        return '\n\n%s\n%s\n\n' % (text, pad_char * len(text)) if text else ''

    def convert_a(self, el, text, parent_tags):
        if '_noformat' in parent_tags:
            return text
        prefix, suffix, text = chomp(text)
        if not text:
            return ''
        href = el.get('href')
        title = el.get('title')
        # For the replacement see #29: text nodes underscores are escaped
        if (self.options['autolinks']
                and text.replace(r'\_', '_') == href
                and not title
                and not self.options['default_title']):
            # Shortcut syntax
            return '<%s>' % href
        if self.options['default_title'] and not title:
            title = href
        title_part = ' "%s"' % title.replace('"', r'\"') if title else ''
        return '%s[%s](%s%s)%s' % (prefix, text, href, title_part, suffix) if href else text

    convert_b = abstract_inline_conversion(lambda self: 2 * self.options['strong_em_symbol'])

    def convert_blockquote(self, el, text, parent_tags):
        # handle some early-exit scenarios
        text = (text or '').strip(' \t\r\n')
        if '_inline' in parent_tags:
            return ' ' + text + ' '
        if not text:
            return "\n"

        # indent lines with blockquote marker
        def _indent_for_blockquote(match):
            line_content = match.group(1)
            return '> ' + line_content if line_content else '>'
        text = re_line_with_content.sub(_indent_for_blockquote, text)

        return '\n' + text + '\n\n'

    def convert_br(self, el, text, parent_tags):
        if '_inline' in parent_tags:
            return text + ' ' if text else ' '

        if self.options['newline_style'].lower() == BACKSLASH:
            return '\\\n' + text
        else:
            return '  \n' + text

    def convert_code(self, el, text, parent_tags):
        if '_noformat' in parent_tags:
            return text

        prefix, suffix, text = chomp(text)
        if not text:
            return ''

        # Find the maximum number of consecutive backticks in the text, then
        # delimit the code span with one more backtick than that
        max_backticks = max((len(match) for match in re.findall(re_backtick_runs, text)), default=0)
        markup_delimiter = '`' * (max_backticks + 1)

        # If the maximum number of backticks is greater than zero, add a space
        # to avoid interpretation of inside backticks as literals
        if max_backticks > 0:
            text = " " + text + " "

        return '%s%s%s%s%s' % (prefix, markup_delimiter, text, markup_delimiter, suffix)

    convert_del = abstract_inline_conversion(lambda self: '~~')

    def convert_div(self, el, text, parent_tags):
        if '_inline' in parent_tags:
            return ' ' + text.strip() + ' '
        text = text.strip()
        return '\n\n%s\n\n' % text if text else ''

    convert_article = convert_div

    convert_section = convert_div

    convert_em = abstract_inline_conversion(lambda self: self.options['strong_em_symbol'])

    convert_kbd = convert_code

    def convert_dd(self, el, text, parent_tags):
        text = (text or '').strip()
        if '_inline' in parent_tags:
            return ' ' + text + ' '
        if not text:
            return '\n'

        # indent definition content lines by four spaces
        def _indent_for_dd(match):
            line_content = match.group(1)
            return '    ' + line_content if line_content else ''
        text = re_line_with_content.sub(_indent_for_dd, text)

        # insert definition marker into first-line indent whitespace
        text = ':' + text[1:]

        return '%s\n' % text

    # definition lists are formatted as follows:
    #   https://pandoc.org/MANUAL.html#definition-lists
    #   https://michelf.ca/projects/php-markdown/extra/#def-list
    convert_dl = convert_div

    def convert_dt(self, el, text, parent_tags):
        # remove newlines from term text
        text = (text or '').strip()
        text = re_all_whitespace.sub(' ', text)
        if '_inline' in parent_tags:
            return ' ' + text + ' '
        if not text:
            return '\n'

        # TODO - format consecutive <dt> elements as directly adjacent lines):
        #   https://michelf.ca/projects/php-markdown/extra/#def-list

        return '\n\n%s\n' % text

    def convert_hN(self, n, el, text, parent_tags):
        # convert_hN() converts <hN> tags, where N is any integer
        if '_inline' in parent_tags:
            return text

        # Markdown does not support heading depths of n > 6
        n = max(1, min(6, n))

        style = self.options['heading_style'].lower()
        text = text.strip()
        if style == UNDERLINED and n <= 2:
            line = '=' if n == 1 else '-'
            return self.underline(text, line)
        text = re_all_whitespace.sub(' ', text)
        hashes = '#' * n
        if style == ATX_CLOSED:
            return '\n\n%s %s %s\n\n' % (hashes, text, hashes)
        return '\n\n%s %s\n\n' % (hashes, text)

    def convert_hr(self, el, text, parent_tags):
        return '\n\n---\n\n'

    convert_i = convert_em

    def convert_img(self, el, text, parent_tags):
        alt = el.attrs.get('alt', None) or ''
        src = el.attrs.get('src', None) or ''
        title = el.attrs.get('title', None) or ''
        title_part = ' "%s"' % title.replace('"', r'\"') if title else ''
        if ('_inline' in parent_tags
                and el.parent.name not in self.options['keep_inline_images_in']):
            return alt

        return '![%s](%s%s)' % (alt, src, title_part)

    def convert_video(self, el, text, parent_tags):
        if ('_inline' in parent_tags
                and el.parent.name not in self.options['keep_inline_images_in']):
            return text
        src = el.attrs.get('src', None) or ''
        if not src:
            sources = el.find_all('source', attrs={'src': True})
            if sources:
                src = sources[0].attrs.get('src', None) or ''
        poster = el.attrs.get('poster', None) or ''
        if src and poster:
            return '[![%s](%s)](%s)' % (text, poster, src)
        if src:
            return '[%s](%s)' % (text, src)
        if poster:
            return '![%s](%s)' % (text, poster)
        return text

    def convert_list(self, el, text, parent_tags):

        # Converting a list to inline is undefined.
        # Ignoring inline conversion parents for list.

        before_paragraph = False
        next_sibling = _next_block_content_sibling(el)
        if next_sibling and next_sibling.name not in ['ul', 'ol']:
            before_paragraph = True
        if 'li' in parent_tags:
            # remove trailing newline if we're in a nested list
            return '\n' + text.rstrip()
        return '\n\n' + text + ('\n' if before_paragraph else '')

    convert_ul = convert_list
    convert_ol = convert_list

    def convert_li(self, el, text, parent_tags):
        # handle some early-exit scenarios
        text = (text or '').strip()
        if not text:
            return "\n"

        # determine list item bullet character to use
        parent = el.parent
        if parent is not None and parent.name == 'ol':
            if parent.get("start") and str(parent.get("start")).isnumeric():
                start = int(parent.get("start"))
            else:
                start = 1
            bullet = '%s.' % (start + len(el.find_previous_siblings('li')))
        else:
            depth = -1
            while el:
                if el.name == 'ul':
                    depth += 1
                el = el.parent
            bullets = self.options['bullets']
            bullet = bullets[depth % len(bullets)]
        bullet = bullet + ' '
        bullet_width = len(bullet)
        bullet_indent = ' ' * bullet_width

        # indent content lines by bullet width
        def _indent_for_li(match):
            line_content = match.group(1)
            return bullet_indent + line_content if line_content else ''
        text = re_line_with_content.sub(_indent_for_li, text)

        # insert bullet into first-line indent whitespace
        text = bullet + text[bullet_width:]

        return '%s\n' % text

    def convert_p(self, el, text, parent_tags):
        if '_inline' in parent_tags:
            return ' ' + text.strip(' \t\r\n') + ' '
        text = text.strip(' \t\r\n')
        if self.options['wrap']:
            # Preserve newlines (and preceding whitespace) resulting
            # from <br> tags.  Newlines in the input have already been
            # replaced by spaces.
            if self.options['wrap_width'] is not None:
                lines = text.split('\n')
                new_lines = []
                for line in lines:
                    line = line.lstrip(' \t\r\n')
                    line_no_trailing = line.rstrip()
                    trailing = line[len(line_no_trailing):]
                    line = fill(line,
                                width=self.options['wrap_width'],
                                break_long_words=False,
                                break_on_hyphens=False)
                    new_lines.append(line + trailing)
                text = '\n'.join(new_lines)
        return '\n\n%s\n\n' % text if text else ''

    def convert_pre(self, el, text, parent_tags):
        if not text:
            return ''
        code_language = self.options['code_language']

        if self.options['code_language_callback']:
            code_language = self.options['code_language_callback'](el) or code_language

        if self.options['strip_pre'] == STRIP:
            text = strip_pre(text)  # remove all leading/trailing newlines
        elif self.options['strip_pre'] == STRIP_ONE:
            text = strip1_pre(text)  # remove one leading/trailing newline
        elif self.options['strip_pre'] is None:
            pass  # leave leading and trailing newlines as-is
        else:
            raise ValueError('Invalid value for strip_pre: %s' % self.options['strip_pre'])

        return '\n\n```%s\n%s\n```\n\n' % (code_language, text)

    def convert_q(self, el, text, parent_tags):
        return '"' + text + '"'

    def convert_script(self, el, text, parent_tags):
        return ''

    def convert_style(self, el, text, parent_tags):
        return ''

    convert_s = convert_del

    convert_strong = convert_b

    convert_samp = convert_code

    convert_sub = abstract_inline_conversion(lambda self: self.options['sub_symbol'])

    convert_sup = abstract_inline_conversion(lambda self: self.options['sup_symbol'])

    def convert_table(self, el, text, parent_tags):
        return '\n\n' + text.strip() + '\n\n'

    def convert_caption(self, el, text, parent_tags):
        return text.strip() + '\n\n'

    def convert_figcaption(self, el, text, parent_tags):
        return '\n\n' + text.strip() + '\n\n'

    def convert_td(self, el, text, parent_tags):
        colspan = 1
        if 'colspan' in el.attrs and el['colspan'].isdigit():
            colspan = max(1, min(1000, int(el['colspan'])))
        return ' ' + text.strip().replace("\n", " ") + ' |' * colspan

    def convert_th(self, el, text, parent_tags):
        colspan = 1
        if 'colspan' in el.attrs and el['colspan'].isdigit():
            colspan = max(1, min(1000, int(el['colspan'])))
        return ' ' + text.strip().replace("\n", " ") + ' |' * colspan

    def convert_tr(self, el, text, parent_tags):
        cells = el.find_all(['td', 'th'])
        is_first_row = el.find_previous_sibling() is None
        is_headrow = (
            all([cell.name == 'th' for cell in cells])
            or (el.parent.name == 'thead'
                # avoid multiple tr in thead
                and len(el.parent.find_all('tr')) == 1)
        )
        is_head_row_missing = (
            (is_first_row and not el.parent.name == 'tbody')
            or (is_first_row and el.parent.name == 'tbody' and len(el.parent.parent.find_all(['thead'])) < 1)
        )
        overline = ''
        underline = ''
        full_colspan = 0
        for cell in cells:
            if 'colspan' in cell.attrs and cell['colspan'].isdigit():
                full_colspan += max(1, min(1000, int(cell['colspan'])))
            else:
                full_colspan += 1
        if ((is_headrow
             or (is_head_row_missing
                 and self.options['table_infer_header']))
                and is_first_row):
            # first row and:
            # - is headline or
            # - headline is missing and header inference is enabled
            # print headline underline
            underline += '| ' + ' | '.join(['---'] * full_colspan) + ' |' + '\n'
        elif ((is_head_row_missing
               and not self.options['table_infer_header'])
              or (is_first_row
                  and (el.parent.name == 'table'
                       or (el.parent.name == 'tbody'
                           and not el.parent.find_previous_sibling())))):
            # headline is missing and header inference is disabled or:
            # first row, not headline, and:
            #  - the parent is table or
            #  - the parent is tbody at the beginning of a table.
            # print empty headline above this row
            overline += '| ' + ' | '.join([''] * full_colspan) + ' |' + '\n'
            overline += '| ' + ' | '.join(['---'] * full_colspan) + ' |' + '\n'
        return overline + '|' + text + '\n' + underline


def markdownify(html, **options):
    return MarkdownConverter(**options).convert(html)


def markdownify_with_catalog(html, **options):
    """
    Convert HTML to Markdown and additionally extract the figure plate
    catalog （图版目录）. Returns a (markdown_text, FigureCatalog) tuple.
    """
    options['figure_catalog'] = True
    converter = MarkdownConverter(**options)
    return converter.convert(html), converter.figure_catalog
