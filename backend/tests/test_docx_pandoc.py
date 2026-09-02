import json
from pathlib import Path
import subprocess

import pytest

from app.services import docx_pandoc
from app.services.docx_semantic import DocxStructureHint


def _document() -> dict:
    return {
        'pandoc-api-version': [1, 23, 1],
        'meta': {},
        'blocks': [
            {
                't': 'Header',
                'c': [1, ['', [], []], [{'t': 'Str', 'c': 'Title'}]],
            },
            {
                't': 'Div',
                'c': [
                    ['', [], [['custom-style', 'Body Text']]],
                    [
                        {
                            't': 'Para',
                            'c': [
                                {'t': 'Str', 'c': 'Read'},
                                {'t': 'Space'},
                                {
                                    't': 'Span',
                                    'c': [
                                        ['', [], [['custom-style', 'Term']]],
                                        [{'t': 'Strong', 'c': [{'t': 'Str', 'c': 'this'}]}],
                                    ],
                                },
                                {'t': 'Space'},
                                {
                                    't': 'Image',
                                    'c': [
                                        ['', [], []],
                                        [{'t': 'Str', 'c': 'diagram'}],
                                        ['media/image1.png', ''],
                                    ],
                                },
                            ],
                        }
                    ],
                ],
            },
        ],
    }


def test_pandoc_docx_uses_json_ast_and_unwraps_word_styles(monkeypatch, tmp_path):
    source = tmp_path / 'sample.docx'
    source.write_bytes(b'word package')
    calls: list[tuple[list[str], dict]] = []

    monkeypatch.setattr(docx_pandoc.shutil, 'which', lambda _name: '/usr/bin/pandoc')

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps(_document()), '')

    monkeypatch.setattr(docx_pandoc.subprocess, 'run', run)

    markdown, details = docx_pandoc.pandoc_docx_to_markdown(source, timeout_seconds=17)

    assert markdown == '# Title\n\nRead **this** diagram'
    assert calls[0][0] == [
        '/usr/bin/pandoc',
        '--from=docx+styles',
        '--to=json',
        '--track-changes=accept',
        str(source.resolve()),
    ]
    assert len(calls) == 1
    assert all(call[1]['timeout'] == 17 for call in calls)
    assert details == {
        'docx_converter': 'pandoc',
        'markdown_renderer': 'paddledoc',
        'pandoc_api_version': '1.23.1',
        'pandoc_warnings': '',
        'custom_styles': {'Body Text': 1, 'Term': 1},
        'structure_hints': 0,
        'structural_headings': 0,
        'paragraph_count': 1,
        'paragraphs': 1,
        'headings': 1,
        'ordered_lists': 0,
        'bullet_lists': 0,
        'tables': 0,
        'links': 0,
        'images_omitted_from_markdown': 1,
        'images': 1,
        'footnotes': 0,
    }


def test_renderer_rebases_word_headings_and_flattens_indentation_quotes():
    document = {
        'pandoc-api-version': [1, 23, 1],
        'meta': {},
        'blocks': [
            {
                't': 'Div',
                'c': [
                    ['', [], [['custom-style', 'Document Title']]],
                    [{'t': 'Para', 'c': [{'t': 'Str', 'c': 'Policy'}]}],
                ],
            },
            {
                't': 'Header',
                'c': [5, ['', [], []], [{'t': 'Str', 'c': 'Coverage'}]],
            },
            {
                't': 'BlockQuote',
                'c': [{'t': 'Para', 'c': [{'t': 'Str', 'c': 'Indented body'}]}],
            },
        ],
    }

    markdown = docx_pandoc.render_pandoc_ast(document)

    assert markdown == '# Policy\n\n## Coverage\n\nIndented body'
    assert '#####' not in markdown
    assert '> ' not in markdown


def test_renderer_flattens_cell_lists_and_writes_gfm_table_without_html():
    paragraph = lambda text: {'t': 'Para', 'c': [{'t': 'Str', 'c': text}]}
    cell = lambda text: [
        ['', [], []],
        {'t': 'AlignDefault'},
        1,
        1,
        [
            {
                't': 'OrderedList',
                'c': [[7, {'t': 'Decimal'}, {'t': 'Period'}], [[paragraph(text)]]],
            }
        ],
    ]
    row = lambda *values: [['', [], []], [cell(value) for value in values]]
    document = {
        'pandoc-api-version': [1, 23, 1],
        'meta': {},
        'blocks': [
            {
                't': 'Table',
                'c': [
                    ['', [], []],
                    [None, []],
                    [],
                    [['', [], []], []],
                    [[['', [], []], 0, [], [row('Benefit', 'Value'), row('Dental', '80%')]]],
                    [['', [], []], []],
                ],
            }
        ],
    }

    markdown = docx_pandoc.render_pandoc_ast(document)

    assert markdown == '| Benefit | Value |\n| --- | --- |\n| Dental | 80% |'
    assert '<table' not in markdown
    assert '<ol' not in markdown
    assert '7.' not in markdown


def test_renderer_repeats_merged_table_values_for_each_logical_column():
    paragraph = lambda text: {'t': 'Para', 'c': [{'t': 'Str', 'c': text}]}
    cell = lambda text, span=1: [
        ['', [], []],
        {'t': 'AlignDefault'},
        1,
        span,
        [paragraph(text)],
    ]
    row = lambda *cells: [['', [], []], list(cells)]
    document = {
        'pandoc-api-version': [1, 23, 1],
        'meta': {},
        'blocks': [
            {
                't': 'Table',
                'c': [
                    ['', [], []],
                    [None, []],
                    [],
                    [['', [], []], [row(cell('Plan'), cell('A'), cell('B'))]],
                    [[['', [], []], 0, [], [row(cell('Limit'), cell('Unlimited', 2))]]],
                    [['', [], []], []],
                ],
            }
        ],
    }

    markdown = docx_pandoc.render_pandoc_ast(document)

    assert markdown == (
        '| Plan | A | B |\n'
        '| --- | --- | --- |\n'
        '| Limit | Unlimited | Unlimited |'
    )


def test_renderer_trims_only_duplicate_merged_overflow_past_the_header():
    paragraph = lambda text: {'t': 'Para', 'c': [{'t': 'Str', 'c': text}]}
    cell = lambda text, span=1: [
        ['', [], []],
        {'t': 'AlignDefault'},
        1,
        span,
        [paragraph(text)] if text else [],
    ]
    row = lambda *cells: [['', [], []], list(cells)]
    document = {
        'pandoc-api-version': [1, 23, 1],
        'meta': {},
        'blocks': [
            {
                't': 'Table',
                'c': [
                    ['', [], []], [None, []], [],
                    [['', [], []], [row(cell('Year'), cell('A'), cell('B'))]],
                    [[['', [], []], 0, [], [row(cell('Later'), cell(''), cell('All', 2))]]],
                    [['', [], []], []],
                ],
            }
        ],
    }

    markdown = docx_pandoc.render_pandoc_ast(document)

    assert markdown == (
        '| Year | A | B |\n'
        '| --- | --- | --- |\n'
        '| Later |  | All |'
    )


def test_renderer_applies_word_structure_hints_without_document_specific_rules():
    para = lambda text: {'t': 'Para', 'c': [{'t': 'Str', 'c': text}]}
    styled = lambda style, text: {
        't': 'Div',
        'c': [['', [], [['custom-style', style]]], [para(text)]],
    }
    document = {
        'pandoc-api-version': [1, 23, 1],
        'meta': {},
        'blocks': [
            styled('Document Title', 'Policy'),
            {
                't': 'OrderedList',
                'c': [
                    [1, {'t': 'DefaultStyle'}, {'t': 'DefaultDelim'}],
                    [[para('Subtitle')], [para('Terms apply.')]],
                ],
            },
            {
                't': 'Header',
                'c': [5, ['', [], []], [{'t': 'Str', 'c': 'Eligibility'}]],
            },
            para('Introductory body.'),
            {
                't': 'OrderedList',
                'c': [
                    [1, {'t': 'Decimal'}, {'t': 'Period'}],
                    [
                        [para('First section'), para('First explanation.')],
                        [para('Second section'), para('Second explanation.')],
                    ],
                ],
            },
        ],
    }
    hints = (
        DocxStructureHint('Policy', 'paragraph', None, None, None, None, 't', 'Title', 'explicit'),
        DocxStructureHint('Subtitle', 'paragraph', None, None, 0, 'none', 'p', 'Body', 'explicit'),
        DocxStructureHint(
            'Terms apply.', 'paragraph', None, None, 0, 'none', 'p', 'Body', 'explicit'
        ),
        DocxStructureHint(
            'Eligibility',
            'heading',
            5,
            'I.',
            4,
            'upperRoman',
            'h',
            'Heading 5',
            'explicit',
        ),
        DocxStructureHint(
            'Introductory body.',
            'paragraph',
            None,
            None,
            None,
            None,
            'p',
            'Body',
            'explicit',
        ),
        DocxStructureHint(
            'First section',
            'heading',
            6,
            '1.',
            0,
            'decimal',
            's',
            'Section',
            'structural',
        ),
        DocxStructureHint(
            'First explanation.',
            'paragraph',
            None,
            None,
            None,
            None,
            'p',
            'Body',
            'explicit',
        ),
        DocxStructureHint(
            'Second section',
            'heading',
            6,
            '2.',
            0,
            'decimal',
            's',
            'Section',
            'structural',
        ),
        DocxStructureHint(
            'Second explanation.',
            'paragraph',
            None,
            None,
            None,
            None,
            'p',
            'Body',
            'explicit',
        ),
    )

    markdown = docx_pandoc.render_pandoc_ast(document, structure_hints=hints)

    assert markdown == (
        '# Policy\n\n'
        'Subtitle\n\n'
        'Terms apply.\n\n'
        '## I. Eligibility\n\n'
        'Introductory body.\n\n'
        '### 1. First section\n\n'
        'First explanation.\n\n'
        '### 2. Second section\n\n'
        'Second explanation.'
    )


def test_renderer_keeps_section_continuity_when_pandoc_nests_it_under_a_list():
    para = lambda text: {'t': 'Para', 'c': [{'t': 'Str', 'c': text}]}
    document = {
        'pandoc-api-version': [1, 23, 1],
        'meta': {},
        'blocks': [
            {'t': 'Header', 'c': [1, ['', [], []], [{'t': 'Str', 'c': 'Chapter'}]]},
            {
                't': 'OrderedList',
                'c': [
                    [1, {'t': 'Decimal'}, {'t': 'Period'}],
                    [[para('Parent section'), para('Parent explanation.')]],
                ],
            },
            {
                't': 'OrderedList',
                'c': [
                    [6, {'t': 'Decimal'}, {'t': 'Period'}],
                    [[para('Current section'), para('Current explanation.')]],
                ],
            },
            {
                't': 'BulletList',
                'c': [
                    [para('First condition')],
                    [
                        para('Second condition'),
                        para('Body after the conditions.'),
                        {
                            't': 'OrderedList',
                            'c': [
                                [1, {'t': 'Decimal'}, {'t': 'Period'}],
                                [
                                    [para('Next section'), para('Next explanation.')],
                                    [para('Following section'), para('Following explanation.')],
                                ],
                            ],
                        },
                    ],
                ],
            },
        ],
    }

    def hint(
        text,
        role='paragraph',
        level=None,
        label=None,
        numbering_level=None,
        number_format=None,
        confidence='explicit',
        numbering_path=None,
    ):
        return DocxStructureHint(
            text,
            role,
            level,
            label,
            numbering_level,
            number_format,
            None,
            None,
            confidence,
            numbering_path,
        )

    hints = (
        hint('Chapter', 'heading', 1),
        hint('Parent section', 'heading', 2, '1.', 0, 'decimal', 'structural', (1,)),
        hint('Parent explanation.'),
        # The raw OOXML parent counter is 3 because this numbering definition
        # was reused earlier. The active semantic parent is nevertheless 1.
        hint('Current section', 'heading', 3, '3.6.', 1, 'decimal', 'structural', (3, 6)),
        hint('Current explanation.'),
        hint('First condition', 'list_item', None, '-', 0, 'bullet'),
        hint('Second condition', 'list_item', None, '-', 0, 'bullet'),
        hint('Body after the conditions.'),
        hint('Next section', 'heading', 3, '3.7.', 1, 'decimal', 'structural', (3, 7)),
        hint('Next explanation.'),
        hint('Following section', 'heading', 3, '3.8.', 1, 'decimal', 'structural', (3, 8)),
        hint('Following explanation.'),
    )

    markdown = docx_pandoc.render_pandoc_ast(document, structure_hints=hints)

    assert markdown == (
        '# Chapter\n\n'
        '## 1. Parent section\n\n'
        'Parent explanation.\n\n'
        '### 1.6. Current section\n\n'
        'Current explanation.\n\n'
        '- First condition\n'
        '- Second condition\n\n'
        'Body after the conditions.\n\n'
        '### 1.7. Next section\n\n'
        'Next explanation.\n\n'
        '### 1.8. Following section\n\n'
        'Following explanation.'
    )
    assert '\n  ###' not in markdown


def test_renderer_uses_word_labels_for_nested_decimal_and_alphabetic_lists():
    para = lambda text: {'t': 'Para', 'c': [{'t': 'Str', 'c': text}]}
    ordered = lambda start, *items: {
        't': 'OrderedList',
        'c': [[start, {'t': 'Decimal'}, {'t': 'Period'}], [[para(item)] for item in items]],
    }
    document = {
        'pandoc-api-version': [1, 23, 1],
        'meta': {},
        'blocks': [
            {'t': 'Header', 'c': [1, ['', [], []], [{'t': 'Str', 'c': 'Chapter'}]]},
            ordered(1, 'Parent'),
            ordered(5, 'Equipment'),
            ordered(1, 'First aid', 'Second aid'),
            ordered(1, 'First condition', 'Second condition'),
        ],
    }
    hints = (
        DocxStructureHint('Chapter', 'heading', 1, None, None, None, None, None, 'explicit'),
        DocxStructureHint(
            'Parent', 'heading', 2, '1.', 0, 'decimal', None, None,
            'structural', (1,), 'parent',
        ),
        DocxStructureHint(
            'Equipment', 'heading', 3, '3.5.', 1, 'decimal', None, None,
            'structural', (3, 5), 'sections',
        ),
        DocxStructureHint(
            'First aid', 'list_item', None, '3.5.1.', 2, 'decimal', None, None,
            'explicit', (3, 5, 1), 'sections',
        ),
        DocxStructureHint(
            'Second aid', 'list_item', None, '3.5.2.', 2, 'decimal', None, None,
            'explicit', (3, 5, 2), 'sections',
        ),
        DocxStructureHint(
            'First condition', 'list_item', None, 'a)', 0, 'lowerLetter', None, None,
            'explicit', (1,), 'letters',
        ),
        DocxStructureHint(
            'Second condition', 'list_item', None, 'b)', 0, 'lowerLetter', None, None,
            'explicit', (2,), 'letters',
        ),
    )

    markdown = docx_pandoc.render_pandoc_ast(document, structure_hints=hints)

    assert '- 1.5.1. First aid' in markdown
    assert '- 1.5.2. Second aid' in markdown
    assert '- a) First condition' in markdown
    assert '- b) Second condition' in markdown


def test_pandoc_docx_reports_missing_executable(monkeypatch, tmp_path):
    source = tmp_path / 'sample.docx'
    source.write_bytes(b'word package')
    monkeypatch.setattr(docx_pandoc.shutil, 'which', lambda _name: None)

    with pytest.raises(docx_pandoc.PandocUnavailableError, match='not installed'):
        docx_pandoc.pandoc_docx_to_markdown(source)


def test_pandoc_docx_wraps_timeout(monkeypatch, tmp_path):
    source = tmp_path / 'sample.docx'
    source.write_bytes(b'word package')
    monkeypatch.setattr(docx_pandoc.shutil, 'which', lambda _name: '/usr/bin/pandoc')

    def timeout(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 5)

    monkeypatch.setattr(docx_pandoc.subprocess, 'run', timeout)

    with pytest.raises(docx_pandoc.PandocDocxError, match='exceeded the 5s timeout'):
        docx_pandoc.pandoc_docx_to_markdown(source, timeout_seconds=5)


def test_pandoc_docx_rejects_non_docx(tmp_path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF')

    with pytest.raises(docx_pandoc.PandocDocxError, match='does not support'):
        docx_pandoc.pandoc_docx_to_markdown(source)
