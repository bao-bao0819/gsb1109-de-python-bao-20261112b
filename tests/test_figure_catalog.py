from markdownify import (
    FigureCatalog,
    MarkdownConverter,
    markdownify,
    markdownify_with_catalog,
)


def convert_with_catalog(html, **options):
    converter = MarkdownConverter(figure_catalog=True, **options)
    text = converter.convert(html)
    return text, converter.figure_catalog


def test_catalog_disabled_by_default():
    converter = MarkdownConverter()
    converter.convert('<figure><figcaption>Cap</figcaption></figure>')
    assert converter.figure_catalog is None


def test_parent_figcaption_covers_all_parts():
    # A caption on the parent figure of a composite (A + B) plate may
    # describe either part, so both are kept as candidates.
    _, catalog = convert_with_catalog(
        '<figure id="fig1">'
        '<img data-part="A" src="a.jpg">'
        '<img data-part="B" src="b.jpg">'
        '<figcaption>Bronze vessel and its inscription</figcaption>'
        '</figure>')
    assert catalog.figures == {'fig1': ['A', 'B']}
    assert len(catalog.entries) == 1
    entry = catalog.entries[0]
    assert entry.caption == 'Bronze vessel and its inscription'
    assert entry.plates == ['fig1A', 'fig1B']
    assert entry.ambiguous
    assert catalog.captions_for('fig1A') == ['Bronze vessel and its inscription']
    assert catalog.captions_for('fig1B') == ['Bronze vessel and its inscription']


def test_labeled_part_caption_inside_figure():
    # A caption explicitly labeled "B" points at part B only.
    _, catalog = convert_with_catalog(
        '<figure id="fig1">'
        '<figure data-part="A"><img src="a.jpg"></figure>'
        '<figure data-part="B"><img src="b.jpg">'
        '<figcaption>B: detail of the handle</figcaption></figure>'
        '</figure>')
    assert len(catalog.entries) == 1
    entry = catalog.entries[0]
    assert entry.caption == 'detail of the handle'
    assert entry.plates == ['fig1B']
    assert entry.explicit
    assert catalog.captions_for('fig1B') == ['detail of the handle']
    # part A has no caption and must not be filled from elsewhere
    assert catalog.captions_for('fig1A') == []


def test_caption_may_reference_multiple_parts():
    # One caption may reference several exhibits at once.
    _, catalog = convert_with_catalog(
        '<figure id="fig3">'
        '<img data-part="A" src="a.jpg">'
        '<img data-part="B" src="b.jpg">'
        '<figcaption>A and B: two views of the same vase</figcaption>'
        '</figure>')
    entry, = catalog.entries
    assert entry.caption == 'two views of the same vase'
    assert entry.plates == ['fig3A', 'fig3B']
    assert entry.explicit

    _, catalog = convert_with_catalog(
        '<figure id="fig4">'
        '<img data-part="A" src="a.jpg">'
        '<img data-part="B" src="b.jpg">'
        '<figcaption data-parts="A B">front and back</figcaption>'
        '</figure>')
    entry, = catalog.entries
    assert entry.plates == ['fig4A', 'fig4B']
    assert entry.explicit


def test_part_labels_are_scoped_per_figure():
    # The "B" of one plate is not the "B" of the adjacent plate.
    _, catalog = convert_with_catalog(
        '<figure id="fig1">'
        '<img data-part="A" src="a1.jpg"><img data-part="B" src="b1.jpg">'
        '</figure>'
        '<figure id="fig2">'
        '<img data-part="A" src="a2.jpg"><img data-part="B" src="b2.jpg">'
        '<figcaption>B: side view</figcaption>'
        '</figure>')
    assert catalog.captions_for('fig2B') == ['side view']
    assert catalog.captions_for('fig1B') == []
    assert catalog.captions_for('fig1A') == []
    assert set(catalog.plates()) == {'fig1A', 'fig1B', 'fig2A', 'fig2B'}


def test_no_backfill_from_next_figure():
    # A figure without a caption stays uncaptioned; the following
    # figure's caption is never used to fill the gap.
    _, catalog = convert_with_catalog(
        '<figure id="fig1"><img data-part="A" src="a1.jpg">'
        '<img data-part="B" src="b1.jpg"></figure>'
        '<figure id="fig2"><img data-part="A" src="a2.jpg">'
        '<img data-part="B" src="b2.jpg">'
        '<figcaption>Jade pendant</figcaption></figure>')
    assert catalog.lookup('fig1A') == []
    assert catalog.lookup('fig1B') == []
    assert catalog.captions_for('fig2A') == ['Jade pendant']
    assert catalog.captions_for('fig2B') == ['Jade pendant']


def test_standalone_caption_with_explicit_label():
    # A caption outside the figure may point at a plate via explicit
    # page labels (data attributes or a "Fig. 2B:" text label).
    _, catalog = convert_with_catalog(
        '<figure id="fig2"><img data-part="A" src="a.jpg">'
        '<img data-part="B" src="b.jpg"></figure>'
        '<p class="caption" data-figure="fig2" data-part="B">side view</p>')
    assert catalog.captions_for('fig2B') == ['side view']
    assert catalog.captions_for('fig2A') == []

    _, catalog = convert_with_catalog(
        '<figure id="fig2"><img data-part="A" src="a.jpg">'
        '<img data-part="B" src="b.jpg"></figure>'
        '<p class="caption">Fig. 2B: side view</p>')
    assert catalog.captions_for('fig2B') == ['side view']
    assert catalog.captions_for('fig2A') == []


def test_ambiguous_standalone_label_keeps_candidates():
    # A bare "B" label between two figures could belong to either one;
    # both candidates are preserved instead of picking one.
    _, catalog = convert_with_catalog(
        '<figure id="fig1"><img data-part="A" src="a1.jpg">'
        '<img data-part="B" src="b1.jpg"></figure>'
        '<p class="caption">B: rubbing of the inscription</p>'
        '<figure id="fig2"><img data-part="A" src="a2.jpg">'
        '<img data-part="B" src="b2.jpg"></figure>')
    entry, = catalog.entries
    assert entry.ambiguous
    assert set(entry.plates) == {'fig1B', 'fig2B'}
    assert catalog.captions_for('fig1B') == ['rubbing of the inscription']
    assert catalog.captions_for('fig2B') == ['rubbing of the inscription']
    # the candidates are distinct plates: fig1B is not fig2B
    assert catalog.captions_for('fig1A') == []
    assert catalog.captions_for('fig2A') == []


def test_figure_without_parts():
    _, catalog = convert_with_catalog(
        '<figure id="fig5"><img src="a.jpg">'
        '<figcaption>A bronze mirror</figcaption></figure>')
    entry, = catalog.entries
    assert entry.plates == ['fig5']
    assert not entry.ambiguous
    assert catalog.captions_for('fig5') == ['A bronze mirror']


def test_figures_without_id_get_ordinal_keys():
    _, catalog = convert_with_catalog(
        '<figure><img src="a.jpg"><figcaption>first</figcaption></figure>'
        '<figure><img src="b.jpg"><figcaption>second</figcaption></figure>')
    assert catalog.figures == {'1': [], '2': []}
    assert catalog.captions_for('1') == ['first']
    assert catalog.captions_for('2') == ['second']


def test_lookup_normalizes_plate_refs():
    _, catalog = convert_with_catalog(
        '<figure id="fig1"><img data-part="B" src="b.jpg">'
        '<figcaption>B: detail</figcaption></figure>')
    assert catalog.captions_for('fig1B') == ['detail']
    assert catalog.captions_for('Fig. 1-B') == ['detail']
    assert catalog.captions_for('fig1 b') == ['detail']


def test_markdownify_with_catalog_returns_text_and_catalog():
    html = ('<figure id="fig1"><img data-part="A" src="a.jpg">'
            '<figcaption>Fig. 1A: front view</figcaption></figure>')
    text, catalog = markdownify_with_catalog(html)
    assert text == markdownify(html)
    assert isinstance(catalog, FigureCatalog)
    assert catalog.captions_for('fig1A') == ['front view']


def test_catalog_does_not_change_markdown_output():
    html = ('<figure><img src="a.jpg" alt="A">'
            '<figcaption>Caption</figcaption></figure><p>TEXT</p>')
    text, _ = convert_with_catalog(html)
    assert text == markdownify(html)


def test_catalog_serialization():
    _, catalog = convert_with_catalog(
        '<figure id="fig1"><img data-part="A" src="a.jpg">'
        '<img data-part="B" src="b.jpg">'
        '<figcaption>A and B: a pair of discs</figcaption></figure>')
    data = catalog.to_dict()
    assert data['figures'] == {'fig1': ['A', 'B']}
    assert data['entries'][0]['plates'] == ['fig1A', 'fig1B']
    table = catalog.to_markdown()
    assert '| Plate | Caption | Match |' in table
    assert 'fig1A, fig1B' in table
    assert 'a pair of discs' in table
