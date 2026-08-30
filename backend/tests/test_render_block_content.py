"""Unit tests for `_render_block_content` / `_clean_block_text` in
app/services/paddle_service.py, plus a fixture collection of real
PaddleOCR-VL LaTeX output.

Kept out of test_paddle_service.py (already 1000+ lines, organized by
feature area -- runtime capability, profile resolution, VL connections,
.eml ingestion, ...) because this is one cohesive unit: exhaustive
per-label coverage of a single pure-function renderer, plus the fixture
data a later LaTeX normalizer's own tests will build on. Before this file,
`_render_block_content` had zero direct tests (grep hit count: 0) -- only
indirect coverage of the paragraph_title/table branches via
test_convert_structure_to_markdown_renders_rag_blocks in
test_paddle_service.py.
"""

from __future__ import annotations

import pytest

from app.services import paddle_service

render = paddle_service._render_block_content
clean = paddle_service._clean_block_text


# --- _clean_block_text -------------------------------------------------------

def test_clean_block_text_collapses_whitespace():
    assert clean('a   b\tc\n\nd') == 'a b c d'


def test_clean_block_text_none_input_returns_empty_string():
    assert clean(None) == ''


def test_clean_block_text_whitespace_only_returns_empty_string():
    assert clean('   \n\t  ') == ''


# --- _render_block_content: heading-like labels (paragraph_title, doc_title) -

@pytest.mark.parametrize('label', ['paragraph_title', 'doc_title'])
def test_render_block_content_heading_labels_wrap_in_h2(label):
    assert render(label, 'Section One', page_number=1) == '## Section One'


@pytest.mark.parametrize('label', ['paragraph_title', 'doc_title'])
def test_render_block_content_heading_labels_empty_content(label):
    assert render(label, '', page_number=1) == ''
    assert render(label, '   ', page_number=1) == ''


# With format_block_content=True (set in _paddlevl_to_structure) the engine
# supplies its own heading level. Stamping ours on top would yield '## # Title'.

@pytest.mark.parametrize('label', ['paragraph_title', 'doc_title'])
@pytest.mark.parametrize('own', ['# Doc Title', '## Section', '###### Deepest'])
def test_render_block_content_heading_labels_keep_engine_supplied_level(label, own):
    assert render(label, own, page_number=1) == own


def test_render_block_content_table_title_keeps_engine_supplied_level():
    assert render('table_title', '## Results', page_number=1) == '## Results'


@pytest.mark.parametrize('label', ['paragraph_title', 'doc_title', 'table_title'])
@pytest.mark.parametrize(
    'not_a_heading',
    [
        '#NoSpace',            # ATX needs whitespace after the hashes
        '#######  Seven',      # seven hashes is not a valid ATX level
        '#',                   # bare marker, no text
        '#   ',                # marker with only whitespace after it
        'Nr. #3 Anlage',       # hash mid-line, not at the start
    ],
)
def test_render_block_content_heading_labels_stamp_level_when_content_is_not_a_heading(
    label, not_a_heading,
):
    rendered = render(label, not_a_heading, page_number=1)
    expected_prefix = '### ' if label == 'table_title' else '## '
    assert rendered.startswith(expected_prefix)


# --- _render_block_content: body-text labels (text, paragraph, content) -----

@pytest.mark.parametrize('label', ['text', 'paragraph', 'content'])
def test_render_block_content_body_labels_return_cleaned_text(label):
    assert render(label, '  Hello   world  ', page_number=1) == 'Hello world'


@pytest.mark.parametrize('label', ['text', 'paragraph', 'content'])
def test_render_block_content_body_labels_empty_content(label):
    assert render(label, '', page_number=1) == ''


# --- _render_block_content: table_title --------------------------------------

def test_render_block_content_table_title_wraps_in_h3():
    assert render('table_title', 'Results', page_number=1) == '### Results'


def test_render_block_content_table_title_empty_content():
    assert render('table_title', '', page_number=1) == ''


# --- _render_block_content: table (HTML -> GFM, and the non-HTML passthrough)-

def test_render_block_content_table_html_converts_to_gfm():
    html_table = '<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>'
    result = render('table', html_table, page_number=1)
    assert '| A | B |' in result
    assert '| 1 | 2 |' in result
    assert '<table' not in result.lower()


def test_render_block_content_table_non_html_passes_through_cleaned():
    assert render('table', '  raw table text  ', page_number=1) == 'raw table text'


def test_render_block_content_table_empty_content():
    assert render('table', '', page_number=1) == ''


# --- _render_block_content: figure / image (A6) ------------------------------
#
# Filled-in forms carry signatures/handwriting inside figure/image regions.
# The placeholder is kept either way (downstream consumers rely on it), but
# when the block actually has content, A6 appends it after the placeholder
# instead of discarding it.

@pytest.mark.parametrize('label', ['figure', 'image'])
def test_render_block_content_figure_image_with_content_appends_to_placeholder(label):
    result = render(label, 'signature: J. Doe', page_number=3)
    assert result == '*[Figure on page 3]* signature: J. Doe'


@pytest.mark.parametrize('label', ['figure', 'image'])
def test_render_block_content_figure_image_without_content_is_placeholder_only(label):
    assert render(label, '', page_number=3) == '*[Figure on page 3]*'
    assert render(label, '   ', page_number=3) == '*[Figure on page 3]*'


# --- _render_block_content: quoted/aside labels ------------------------------

@pytest.mark.parametrize('label', ['header', 'footer', 'footnote', 'aside_text', 'reference'])
def test_render_block_content_quoted_labels_wrap_in_blockquote(label):
    assert render(label, 'Page 3 of 10', page_number=3) == '> Page 3 of 10'


@pytest.mark.parametrize('label', ['header', 'footer', 'footnote', 'aside_text', 'reference'])
def test_render_block_content_quoted_labels_empty_content(label):
    assert render(label, '', page_number=1) == ''


# --- _render_block_content: llm_markdown -------------------------------------
#
# The one label that skips _clean_block_text entirely: an LLM-authored block
# already IS markdown, so collapsing its internal whitespace would destroy
# headings/lists/paragraph breaks.

def test_render_block_content_llm_markdown_strips_but_does_not_collapse():
    markdown = '\n  # Heading\n\n- item one\n- item two  \n'
    assert render('llm_markdown', markdown, page_number=1) == markdown.strip()


def test_render_block_content_llm_markdown_preserves_internal_newlines():
    markdown = 'line one\nline two'
    assert render('llm_markdown', markdown, page_number=1) == markdown


def test_render_block_content_llm_markdown_empty_content():
    assert render('llm_markdown', '', page_number=1) == ''
    assert render('llm_markdown', None, page_number=1) == ''


# --- _render_block_content: unknown label (fall-through) --------------------

def test_render_block_content_unknown_label_falls_through_to_cleaned_text():
    assert render('some_future_label', '  raw content  ', page_number=1) == 'raw content'


def test_render_block_content_unknown_label_empty_content():
    assert render('some_future_label', '', page_number=1) == ''


# --- A3: checkbox glyph unification -------------------------------------------
#
# Five codepoints PaddleOCR-VL uses for two checkbox states (measured across
# 9 documents: 30x '□', 53x '☐', 32x '☒', 6x '☑'), collapsed to plain-text
# `[x]` / `[ ]` so retrieval on "is this checked" doesn't need five spellings.
# Wired in label-independently in _render_block_content (same step as B1's
# LaTeX normalizer), so a plain 'text' label exercises it just as well as
# any other label -- picking 'text' below is deliberate, not incidental.

@pytest.mark.parametrize(('glyph', 'expected'), [
    ('☒', '[x]'), ('☑', '[x]'), ('⊠', '[x]'),
    ('□', '[ ]'), ('☐', '[ ]'), ('▢', '[ ]'),
])
def test_render_block_content_normalizes_checkbox_glyphs(glyph, expected):
    assert render('text', f'{glyph} Einverstanden', page_number=1) == f'{expected} Einverstanden'


def test_render_block_content_normalizes_multiple_checkboxes_in_one_block():
    assert render('text', '☒ ja  ☐ nein', page_number=1) == '[x] ja [ ] nein'


def test_render_block_content_checkmark_latex_becomes_checked_box():
    assert render('text', r'$ \checkmark $ ja ___ nein', page_number=1) == '[x] ja ___ nein'


def test_render_block_content_does_not_touch_hollow_circle():
    """U+25CB ('○') is deliberately excluded from the checkbox map: in the
    measured data it is a mis-recognized digit, not a checkbox glyph. Adding
    it would turn real numbers into false checkboxes -- see the module
    comment on `_CHECKBOX_MAP`.
    """
    assert render('text', '○ 5', page_number=1) == '○ 5'


def test_render_block_content_checkbox_normalization_is_label_independent():
    assert render('header', '☒ erledigt', page_number=1) == '> [x] erledigt'


# --- The LaTeX reality: B1's normalize_form_latex is now wired in ------------
#
# `_render_block_content` calls `app.services.form_latex.normalize_form_latex`
# label-independently (3 of the 16 LaTeX-carrying blocks measured have label
# 'text', so gating this behind a label branch would miss them). The fixture
# tuples below are still exported for reuse (`from test_render_block_content
# import PADDLEOCR_VL_FORM_LATEX_LINES, REAL_MATH_LATEX_LINES`); the
# expected-output tests now assert the POST-normalization text, computed by
# calling `normalize_form_latex` directly against these exact fixtures.
#
# These are form-field artifacts, not real math: underlined blanks for
# handwritten dates/names/ICD codes, and a checkbox rendered as \checkmark.
# A normalizer is expected to rewrite/simplify these.
PADDLEOCR_VL_FORM_LATEX_LINES: tuple[str, ...] = (
    r'$$ \begin{array}{c}\underline{02}\quad\underline{082}\quad\underline{0\underline{2}5}\\ Tag\quad Monat\quad Jahr\end{array} $$',
    r'$$ \underline{01}\underline{07}\underline{20}\underline{26}_{Tag\quad Monat\quad Jahr} $$',
    r'Patientin/ Patient Nachname: $ \underline{\text{Peter}} $',
    r'bis: $ \underline{\text{Tag}} $ $ \underline{\text{Monat}} $ $ \underline{\text{2026}} $',
    r'$$ \underline{016.7}_{ICD10\mathrm{Code}} $$',
    r'$ \checkmark $ ja ___ nein',
    r'$$ \frac{7}{12}\frac{21}{1}\cdot\frac{0}{1} $$',
)

# Real mathematics. Whatever normalizer targets the form-field patterns
# above must leave these completely alone.
REAL_MATH_LATEX_LINES: tuple[str, ...] = (
    r'$E = mc^2$',
    r'$\frac{\partial u}{\partial t}$',
    r'$\sum_{i=1}^{n} x_i$',
    r'$\alpha \leq \beta$',
    r'$x_i$',
)


def test_form_latex_fixtures_preserve_literal_backslashes():
    """Regression guard on the fixture DATA itself, not on production code:
    if an edit ever lost a backslash (e.g. a raw-string typo, or someone
    "cleaning up" the escaping), every passthrough test below would keep
    passing for the wrong reason -- against a fixture that no longer
    matches what PaddleOCR-VL actually emits. Each expected value here is
    spelled with explicit `\\`-escaped strings (the opposite encoding from
    the raw strings above) so a mismatch is a real, independent signal.
    """
    assert PADDLEOCR_VL_FORM_LATEX_LINES[0] == (
        '$$ \\begin{array}{c}\\underline{02}\\quad\\underline{082}\\quad'
        '\\underline{0\\underline{2}5}\\\\ Tag\\quad Monat\\quad Jahr\\end{array} $$'
    )
    assert PADDLEOCR_VL_FORM_LATEX_LINES[0].count('\\') == 12
    assert '\\\\' in PADDLEOCR_VL_FORM_LATEX_LINES[0]  # the LaTeX array row break
    assert PADDLEOCR_VL_FORM_LATEX_LINES[4] == '$$ \\underline{016.7}_{ICD10\\mathrm{Code}} $$'
    assert PADDLEOCR_VL_FORM_LATEX_LINES[5] == '$ \\checkmark $ ja ___ nein'
    assert REAL_MATH_LATEX_LINES[1] == '$\\frac{\\partial u}{\\partial t}$'
    assert REAL_MATH_LATEX_LINES[3] == '$\\alpha \\leq \\beta$'


# Expected output of `normalize_form_latex` on each PADDLEOCR_VL_FORM_LATEX_LINES
# fixture, in the same order -- computed by running the real normalizer
# against these exact strings (not hand-derived), so this pins down actual
# behavior rather than an assumption about it.
_EXPECTED_NORMALIZED_FORM_LATEX: tuple[str, ...] = (
    '02 082 0 2 5 Tag Monat Jahr',
    '01 07 20 26 Tag Monat Jahr',
    'Patientin/ Patient Nachname: Peter',
    'bis: Tag Monat 2026',
    '016.7 ICD10 Code',
    '[x] ja ___ nein',
    # No underline/checkmark/form-vocabulary anchor (Gate 3) -- a bare
    # fraction stays a literal, unflattened span rather than risk mangling
    # real math.
    r'$$ \frac{7}{12}\frac{21}{1}\cdot\frac{0}{1} $$',
)


@pytest.mark.parametrize(
    ('latex_line', 'expected'),
    list(zip(PADDLEOCR_VL_FORM_LATEX_LINES, _EXPECTED_NORMALIZED_FORM_LATEX)),
)
def test_render_block_content_normalizes_form_latex_label_text(latex_line, expected):
    assert render('text', latex_line, page_number=1) == expected


@pytest.mark.parametrize('latex_line', REAL_MATH_LATEX_LINES)
def test_render_block_content_passes_real_math_through_unchanged(latex_line):
    assert render('text', latex_line, page_number=1) == latex_line


def test_render_block_content_form_latex_normalization_is_label_independent():
    """3 of the 16 measured LaTeX-carrying blocks have label 'text' (not a
    heading/quoted label) -- this pins the same normalization onto a
    heading-labelled block too, proving the step isn't gated behind any one
    label branch.
    """
    result = render('paragraph_title', PADDLEOCR_VL_FORM_LATEX_LINES[2], page_number=1)
    assert result == '## Patientin/ Patient Nachname: Peter'


def test_render_block_content_llm_markdown_bypasses_form_latex_normalization():
    """The llm_markdown branch returns before the normalizer runs at all --
    an LLM-authored block is already markdown and must not have its LaTeX
    reinterpreted as a form-field artifact.
    """
    markdown = r'$ \underline{\text{keep me}} $'
    assert render('llm_markdown', markdown, page_number=1) == markdown


# --- B5: table renderer repair -----------------------------------------------
#
# Five measured bugs in `_html_table_to_markdown` / the 'table' branch of
# `_render_block_content`. Fixtures below use the two real rows quoted
# verbatim in the task, plus the real "literal \n in a cell" content.


def _table_lines(result: str) -> list[str]:
    return result.split('\n')


# Bug 1a: a label/value form row must NOT be promoted to a GFM header --
# that would turn the value into a column name and delete it from the data.
# Real fixture: `<tr><td>Nachname:</td><td>Cicekli</td></tr>`.

def test_render_block_content_table_label_value_row_is_not_promoted_to_header():
    html_table = (
        '<table>'
        '<tr><td>Nachname:</td><td>Cicekli</td></tr>'
        '<tr><td>Vorname:</td><td>Ali</td></tr>'
        '</table>'
    )
    result = render('table', html_table, page_number=1)
    lines = _table_lines(result)
    # GFM still needs a header line, but it must be blank -- neither
    # "Nachname:" nor "Cicekli" may appear there.
    assert 'Nachname' not in lines[0]
    assert 'Cicekli' not in lines[0]
    assert set(lines[0]) <= {'|', ' '}
    assert '---' in lines[1]
    # Both rows -- including the would-be header -- survive as data.
    assert '| Nachname: | Cicekli |' in result
    assert '| Vorname: | Ali |' in result


# Bug 1b: the counter-example from the task -- a real header row with no
# <th>, told apart from the label/value shape because every data row below
# carries a checkbox mark (already normalised to '[x]'/'[ ]' upstream, see
# _normalize_checkbox_glyphs) under the option columns.

def test_render_block_content_table_checkbox_option_header_is_recognized():
    html_table = (
        '<table>'
        '<tr><td>Folgende Unterlagen sind der Schadenanzeige beigefuegt:</td>'
        '<td>Ja</td><td>Nein</td></tr>'
        '<tr><td>Versicherungsschein / Police der Reiseversicherung</td>'
        '<td>☒</td><td>☐</td></tr>'
        '<tr><td>Rechnung</td><td>☐</td><td>☒</td></tr>'
        '</table>'
    )
    result = render('table', html_table, page_number=1)
    lines = _table_lines(result)
    assert 'Folgende Unterlagen sind der Schadenanzeige beigefuegt:' in lines[0]
    assert 'Ja' in lines[0]
    assert 'Nein' in lines[0]
    assert '---' in lines[1]
    assert '[x]' in result
    assert '[ ]' in result
    # The header text itself must not be duplicated into the data section.
    assert result.count('Folgende Unterlagen sind der Schadenanzeige beigefuegt:') == 1


# Bug 1, documented limitation: a genuine header made of plain words (not
# checkbox marks) is indistinguishable from a label/value row under this
# heuristic and is missed -- it falls back to a blank synthetic header
# rather than risk deleting real data, so nothing is lost, it's just not
# recognized as a header.

def test_render_block_content_table_word_header_documented_limitation():
    html_table = (
        '<table>'
        '<tr><td>Name</td><td>Stadt</td></tr>'
        '<tr><td>Ali</td><td>Berlin</td></tr>'
        '</table>'
    )
    result = render('table', html_table, page_number=1)
    lines = _table_lines(result)
    assert set(lines[0]) <= {'|', ' '}
    assert '| Name | Stadt |' in result  # kept, just as a data row


# <th> is still treated as an unambiguous header regardless of the
# checkbox heuristic (already covered indirectly by
# test_render_block_content_table_html_converts_to_gfm; asserted directly
# here for the multi-row case the heuristic above operates on).

def test_render_block_content_table_th_row_is_always_header():
    html_table = (
        '<table>'
        '<tr><th>Name</th><th>Stadt</th></tr>'
        '<tr><td>Ali</td><td>Berlin</td></tr>'
        '<tr><td>Peter</td><td>Hamburg</td></tr>'
        '</table>'
    )
    result = render('table', html_table, page_number=1)
    lines = _table_lines(result)
    assert '| Name | Stadt |' == lines[0]
    assert '| Ali | Berlin |' in result
    assert '| Peter | Hamburg |' in result


# Bug 2: a literal two-character '\n' (not a real line break) inside a cell,
# measured 39x. Real fixture content (the parenthetical is on its own
# visual line in the source).

def test_render_block_content_table_literal_backslash_n_becomes_br():
    cell = (
        r'Versicherungsschein / Police der Reiseversicherung'
        r'\n(Nicht Policen-Nummer der Kreditkarte)'
    )
    assert '\\n' in cell  # sanity: this really is backslash + 'n', not a real newline
    html_table = f'<table><tr><td>{cell}</td><td>Wert</td></tr><tr><td>Other</td><td>X</td></tr></table>'
    result = render('table', html_table, page_number=1)
    assert '<br>' in result
    assert '\\n' not in result
    assert 'Versicherungsschein / Police der Reiseversicherung<br>(Nicht Policen-Nummer der Kreditkarte)' in result


# Bug 3: an unescaped '|' inside a cell would otherwise be read as a new
# column boundary and tear the row apart.

def test_render_block_content_table_pipe_in_cell_is_escaped():
    html_table = (
        '<table>'
        '<tr><td>Feld</td><td>A|B</td></tr>'
        '<tr><td>Other</td><td>C|D</td></tr>'
        '</table>'
    )
    result = render('table', html_table, page_number=1)
    assert 'A\\|B' in result
    assert 'C\\|D' in result
    # Each data row must still be exactly 2 GFM columns (3 real column
    # separators), not split into 3 by the escaped literal. An escaped pipe
    # ('\|') still contains a '|' character, so exclude those before counting.
    data_lines = [ln for ln in _table_lines(result) if 'Feld' in ln or 'Other' in ln]
    assert all(ln.count('|') - ln.count('\\|') == 3 for ln in data_lines)


# Bug 4: colspan must not silently discard the spanning cell's content, and
# must not shift every subsequent column in that row out of alignment with
# rows that don't span. Full rowspan support is explicitly not required.

def test_render_block_content_table_colspan_expands_and_keeps_content():
    html_table = (
        "<table><tr><td colspan='2'>Merged Header</td></tr>"
        '<tr><td>Left</td><td>Right</td></tr></table>'
    )
    result = render('table', html_table, page_number=1)
    assert 'Merged Header' in result
    assert '| Left | Right |' in result
    # Every row renders with the same column count (3 pipes = 2 columns).
    assert all(ln.count('|') == 3 for ln in _table_lines(result))


def test_render_block_content_table_rowspan_cell_content_is_not_dropped():
    html_table = (
        "<table><tr><td rowspan='2'>Shared</td><td>1</td></tr>"
        '<tr><td>2</td></tr></table>'
    )
    result = render('table', html_table, page_number=1)
    assert 'Shared' in result
    assert '1' in result
    assert '2' in result


# Bug 5: an empty table (no parseable <tr> rows) must not fall through to
# leaking the raw HTML into the generated markdown -- the worst of the five
# measured bugs.

def test_render_block_content_table_empty_table_does_not_leak_raw_html():
    assert render('table', '<table></table>', page_number=1) == ''
    assert render('table', '<table><tbody></tbody></table>', page_number=1) == ''


def test_render_block_content_table_no_rows_with_attributes_does_not_leak_raw_html():
    html_table = "<table border=1 style='margin: auto; border-collapse: collapse;'></table>"
    result = render('table', html_table, page_number=1)
    assert result == ''
    assert '<table' not in result.lower()


# P1's format_block_content=True carries presentational attributes on both
# <table> and <td>/<th> -- the existing regexes tolerate `[^>]*`, verified
# here explicitly per the task's stated concern.

def test_render_block_content_table_tolerates_presentation_attributes():
    html_table = (
        "<table border=1 style='margin: auto; border-collapse: collapse;'>"
        "<tr><td style='text-align: center;'>A</td>"
        "<td style='text-align: center;'>B</td></tr>"
        "<tr><td style='text-align: center;'>1</td>"
        "<td style='text-align: center;'>2</td></tr>"
        '</table>'
    )
    result = render('table', html_table, page_number=1)
    assert '<table' not in result.lower()
    assert '<td' not in result.lower()
    assert '| A | B |' in result
    assert '| 1 | 2 |' in result
