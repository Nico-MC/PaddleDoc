from pathlib import Path
import zipfile

import pytest

from app.services.docx_semantic import (
    DocxTableBlock,
    DocxTextBlock,
    build_docx_structure_hints,
    parse_docx_semantic,
    render_docx_markdown,
    semantic_docx_to_markdown,
)


W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'


def _write_docx(
    path: Path,
    *,
    body: str,
    styles: str = '',
    numbering: str = '',
    relationships: str = '',
) -> Path:
    document_xml = (
        f'<w:document xmlns:w="{W}" xmlns:r="{R}" xmlns:mc="{MC}">'
        f'<w:body>{body}</w:body>'
        '</w:document>'
    )
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', document_xml)
        if styles:
            archive.writestr('word/styles.xml', f'<w:styles xmlns:w="{W}">{styles}</w:styles>')
        if numbering:
            archive.writestr(
                'word/numbering.xml',
                f'<w:numbering xmlns:w="{W}">{numbering}</w:numbering>',
            )
        if relationships:
            archive.writestr(
                'word/_rels/document.xml.rels',
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'{relationships}'
                '</Relationships>',
            )
    return path


def test_semantic_parser_keeps_heading_runs_links_and_order(tmp_path):
    source = _write_docx(
        tmp_path / 'semantic.docx',
        styles=(
            '<w:style w:type="paragraph" w:styleId="Normal" w:default="1">'
            '<w:name w:val="Normal"/>'
            '</w:style>'
            '<w:style w:type="paragraph" w:styleId="Heading2">'
            '<w:name w:val="heading 2"/><w:pPr><w:outlineLvl w:val="1"/></w:pPr>'
            '</w:style>'
            '<w:style w:type="paragraph" w:styleId="PolicySection">'
            '<w:name w:val="Policy section"/><w:basedOn w:val="Heading2"/>'
            '</w:style>'
        ),
        relationships=(
            '<Relationship Id="rId5" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
            'Target="https://example.test/policy" TargetMode="External"/>'
        ),
        body=(
            '<w:p><w:pPr><w:pStyle w:val="PolicySection"/></w:pPr>'
            '<w:r><w:t>Coverage</w:t></w:r></w:p>'
            '<w:p>'
            '<w:r><w:rPr><w:b/></w:rPr><w:t>Bold</w:t></w:r>'
            '<w:r><w:t xml:space="preserve"> and </w:t></w:r>'
            '<w:r><w:rPr><w:i/></w:rPr><w:t>italic</w:t></w:r>'
            '<w:r><w:t xml:space="preserve"> with </w:t></w:r>'
            '<w:hyperlink r:id="rId5"><w:r><w:t>source</w:t></w:r></w:hyperlink>'
            '</w:p>'
        ),
    )

    markdown, document = semantic_docx_to_markdown(source)

    assert [block.kind for block in document.blocks if isinstance(block, DocxTextBlock)] == [
        'heading',
        'paragraph',
    ]
    assert document.blocks[0].heading_level == 2
    assert markdown == (
        '## Coverage\n\n'
        '**Bold** and *italic* with [source](https://example.test/policy)'
    )
    assert render_docx_markdown(parse_docx_semantic(source)) == markdown


def test_semantic_parser_uses_numbering_xml_without_inventing_labels(tmp_path):
    numbering = (
        '<w:abstractNum w:abstractNumId="10">'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
        '<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%2)"/></w:lvl>'
        '</w:abstractNum>'
        '<w:num w:numId="42"><w:abstractNumId w:val="10"/></w:num>'
    )

    def item(text: str, level: int) -> str:
        return (
            '<w:p><w:pPr><w:numPr>'
            f'<w:ilvl w:val="{level}"/><w:numId w:val="42"/>'
            f'</w:numPr></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'
        )

    source = _write_docx(
        tmp_path / 'numbering.docx',
        numbering=numbering,
        body=item('First', 0) + item('Nested one', 1) + item('Nested two', 1) + item('Second', 0),
    )

    markdown, document = semantic_docx_to_markdown(source)

    assert all(
        block.kind == 'list_item'
        for block in document.blocks
        if isinstance(block, DocxTextBlock)
    )
    assert markdown.splitlines() == [
        '1. First',
        '  - a) Nested one',
        '  - b) Nested two',
        '2. Second',
    ]
    assert render_docx_markdown(parse_docx_semantic(source)) == markdown


def test_structure_hints_distinguish_hidden_numbers_sections_and_real_lists(tmp_path):
    numbering = (
        '<w:abstractNum w:abstractNumId="1">'
        '<w:lvl w:ilvl="0"><w:numFmt w:val="none"/><w:lvlText w:val="%1"/></w:lvl>'
        '</w:abstractNum>'
        '<w:abstractNum w:abstractNumId="2">'
        '<w:lvl w:ilvl="0"><w:numFmt w:val="upperRoman"/><w:lvlText w:val="%1."/></w:lvl>'
        '</w:abstractNum>'
        '<w:abstractNum w:abstractNumId="3">'
        '<w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
        '</w:abstractNum>'
        '<w:abstractNum w:abstractNumId="4">'
        '<w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="-"/></w:lvl>'
        '</w:abstractNum>'
        '<w:num w:numId="10"><w:abstractNumId w:val="1"/></w:num>'
        '<w:num w:numId="20"><w:abstractNumId w:val="2"/></w:num>'
        '<w:num w:numId="30"><w:abstractNumId w:val="3"/></w:num>'
        '<w:num w:numId="40"><w:abstractNumId w:val="4"/></w:num>'
    )
    styles = (
        '<w:style w:type="paragraph" w:styleId="Heading5">'
        '<w:name w:val="heading 5"/><w:pPr><w:outlineLvl w:val="4"/></w:pPr>'
        '</w:style>'
    )

    def paragraph(text: str, *, num_id: str | None = None, style: str | None = None) -> str:
        properties = ''
        if num_id or style:
            properties = '<w:pPr>'
            if style:
                properties += f'<w:pStyle w:val="{style}"/>'
            if num_id:
                properties += (
                    '<w:numPr><w:ilvl w:val="0"/>'
                    f'<w:numId w:val="{num_id}"/></w:numPr>'
                )
            properties += '</w:pPr>'
        return f'<w:p>{properties}<w:r><w:t>{text}</w:t></w:r></w:p>'

    source = _write_docx(
        tmp_path / 'structure.docx',
        numbering=numbering,
        styles=styles,
        body=(
            paragraph('Subtitle', num_id='10')
            + paragraph('Eligibility', num_id='20', style='Heading5')
            + paragraph('Section title', num_id='30')
            + paragraph('Long explanatory paragraph.')
            + paragraph('Actual bullet', num_id='40')
        ),
    )

    document = parse_docx_semantic(source)
    hints = build_docx_structure_hints(document)

    assert [(hint.text, hint.role, hint.numbering_label) for hint in hints] == [
        ('Subtitle', 'paragraph', None),
        ('Eligibility', 'heading', 'I.'),
        ('Section title', 'heading', '1.'),
        ('Long explanatory paragraph.', 'paragraph', None),
        ('Actual bullet', 'list_item', '-'),
    ]
    assert hints[2].heading_level == 6
    assert hints[2].confidence == 'structural'


def test_structure_hints_preserve_child_counter_across_an_unrelated_list(tmp_path):
    numbering = (
        '<w:abstractNum w:abstractNumId="1">'
        '<w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
        '<w:lvl w:ilvl="1"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2."/></w:lvl>'
        '</w:abstractNum>'
        '<w:abstractNum w:abstractNumId="2">'
        '<w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
        '</w:abstractNum>'
        '<w:num w:numId="20"><w:abstractNumId w:val="1"/></w:num>'
        '<w:num w:numId="19"><w:abstractNumId w:val="2"/></w:num>'
    )

    def item(text: str, num_id: str, level: int = 0) -> str:
        return (
            '<w:p><w:pPr><w:numPr>'
            f'<w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/>'
            f'</w:numPr></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'
        )

    source = _write_docx(
        tmp_path / 'continued-section.docx',
        numbering=numbering,
        body=(
            item('Parent one', '20')
            + item('Parent two', '20')
            + item('Parent three', '20')
            + item('Child six', '20', 1)
            + '<w:p><w:r><w:t>Explanation.</w:t></w:r></w:p>'
            + item('Embedded one', '19')
            + item('Embedded two', '19')
            + item('Child seven', '20', 1)
            + '<w:p><w:r><w:t>More explanation.</w:t></w:r></w:p>'
        ),
    )

    hints = build_docx_structure_hints(parse_docx_semantic(source))
    by_text = {hint.text: hint for hint in hints}

    assert by_text['Child six'].numbering_path == (3, 1)
    assert by_text['Child seven'].numbering_path == (3, 2)
    assert by_text['Embedded two'].numbering_path == (2,)


def test_semantic_parser_preserves_table_rows_and_content_controls(tmp_path):
    source = _write_docx(
        tmp_path / 'table.docx',
        body=(
            '<w:sdt><w:sdtContent><w:p><w:r><w:t>Before table</w:t></w:r></w:p></w:sdtContent></w:sdt>'
            '<w:tbl>'
            '<w:tr><w:trPr><w:tblHeader/></w:trPr>'
            '<w:tc><w:p><w:r><w:t>Name</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc>'
            '</w:tr>'
            '<w:tr>'
            '<w:tc><w:p><w:r><w:t>A|B</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:r><w:t>42</w:t></w:r></w:p></w:tc>'
            '</w:tr>'
            '</w:tbl>'
        ),
    )

    markdown, document = semantic_docx_to_markdown(source)

    assert isinstance(document.blocks[0], DocxTextBlock)
    assert isinstance(document.blocks[1], DocxTableBlock)
    assert markdown == (
        'Before table\n\n'
        '| Name | Value |\n'
        '| --- | --- |\n'
        '| A\\|B | 42 |'
    )


def test_semantic_parser_uses_alternate_content_choice_once(tmp_path):
    source = _write_docx(
        tmp_path / 'textbox.docx',
        body=(
            '<w:p><mc:AlternateContent>'
            '<mc:Choice Requires="wps"><w:pict><w:txbxContent>'
            '<w:p><w:r><w:t>Choice text</w:t></w:r></w:p>'
            '</w:txbxContent></w:pict></mc:Choice>'
            '<mc:Fallback><w:pict><w:txbxContent>'
            '<w:p><w:r><w:t>Fallback duplicate</w:t></w:r></w:p>'
            '</w:txbxContent></w:pict></mc:Fallback>'
            '</mc:AlternateContent></w:p>'
        ),
    )

    markdown, document = semantic_docx_to_markdown(source)

    assert markdown == 'Choice text'
    assert document.paragraph_count == 1


def test_semantic_parser_does_not_promote_arbitrary_custom_style(tmp_path):
    source = _write_docx(
        tmp_path / 'custom-style.docx',
        styles=(
            '<w:style w:type="paragraph" w:styleId="KastenAVBberschrift">'
            '<w:name w:val="KastenAVBberschrift"/>'
            '</w:style>'
        ),
        body=(
            '<w:p><w:pPr><w:pStyle w:val="KastenAVBberschrift"/></w:pPr>'
            '<w:r><w:t>Domain-specific text</w:t></w:r></w:p>'
        ),
    )

    markdown, document = semantic_docx_to_markdown(source)

    block = document.blocks[0]
    assert isinstance(block, DocxTextBlock)
    assert block.kind == 'paragraph'
    assert block.heading_level is None
    assert markdown == 'Domain-specific text'


def test_semantic_parser_smoke_tests_available_docx_corpus():
    expected_structural_headings = {
        'AZS 2512[87].docx': 23,
        'BES 2512.docx': 7,
        'Beihilfehandbuch_20241205.docx': 0,
        'PSB 2512[3].docx': 0,
        'W 2512.docx': 7,
    }
    roots = [
        Path('/app/docs/TypischeDokumente'),
        Path(__file__).resolve().parents[2].parent / '.docs' / 'TypischeDokumente',
    ]
    documents = next(
        (
            sorted(
                path
                for path in root.glob('*.docx')
                if not path.name.startswith(('._', '~$'))
            )
            for root in roots
            if root.exists()
        ),
        [],
    )
    if not documents:
        pytest.skip('DOCX comparison corpus is not available')

    for source in documents:
        markdown, document = semantic_docx_to_markdown(source)
        assert len(markdown) > 100, source.name
        assert document.paragraph_count > 0, source.name
        assert '\x00' not in markdown, source.name

        hints = build_docx_structure_hints(document)
        assert hints, source.name
        assert all(
            hint.number_format == 'decimal'
            for hint in hints
            if hint.confidence == 'structural' and hint.role == 'heading'
        ), source.name
        if source.name in expected_structural_headings:
            assert sum(
                hint.confidence == 'structural' and hint.role == 'heading'
                for hint in hints
            ) == expected_structural_headings[source.name]

        if source.name in {
            'AZS 2512[87].docx',
            'BES 2512.docx',
            'PSB 2512[3].docx',
            'W 2512.docx',
        }:
            assert [hint.role for hint in hints[:4]] == [
                'paragraph',
                'paragraph',
                'paragraph',
                'heading',
            ], source.name
            assert hints[3].text == 'Versicherungsfähigkeit', source.name
            assert hints[3].numbering_label == 'I.', source.name
