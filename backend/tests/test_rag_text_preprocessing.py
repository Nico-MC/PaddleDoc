from app.services.rag_text_preprocessing import (
    count_picture_text_blocks,
    prepare_rag_text,
    prepare_rag_text_with_report,
)


def test_prepare_rag_text_removes_complete_picture_blocks_and_keeps_page_markers() -> None:
    original = (
        '# Tarif\n\n'
        '<!-- page:14/90 -->\n\n'
        'Leistung A.\n\n'
        '<!-- Start of picture text -->\n'
        'Irrefuehrender Grafiktext.\n'
        '<!-- page:15/90 -->\n'
        'Weitere falsche Beschriftung.\n'
        '<!-- End of picture text -->\n\n'
        'Leistung B.\n\n'
        '<!-- page:16/90 -->\n'
    )

    cleaned = prepare_rag_text(original)

    assert 'Irrefuehrender Grafiktext.' not in cleaned
    assert 'Weitere falsche Beschriftung.' not in cleaned
    assert 'Start of picture text' not in cleaned
    assert 'End of picture text' not in cleaned
    assert '<!-- page:14/90 -->' in cleaned
    assert '<!-- page:15/90 -->' in cleaned
    assert '<!-- page:16/90 -->' in cleaned
    assert count_picture_text_blocks(original) == 1
    assert 'Leistung A.' in cleaned
    assert 'Leistung B.' in cleaned
    assert original != cleaned


def test_prepare_rag_text_leaves_an_incomplete_picture_block_unchanged() -> None:
    original = 'Vorher\n<!-- Start of picture text -->\nNicht vollstaendig'

    assert prepare_rag_text(original) == original


def test_prepare_rag_text_removes_repeated_headers_footers_and_page_numbers() -> None:
    pages = []
    for page in range(14, 26):
        header = 'Arbeitsanweisung' if page % 2 else 'Version 2.1 – Vertraulich'
        pages.append(
            f'<!-- page:{page}/90 -->\n\n'
            f'{header}\n\n'
            f'# Fachabschnitt {page}\n\n'
            'Ein fachlicher Satz mit einer belegbaren Aussage.\n\n'
            'Ein fachlicher Satz mit einer belegbaren Aussage.\n\n'
            f'Seite {page} von 90\n'
        )
    original = '\n'.join(pages)

    cleaned, report = prepare_rag_text_with_report(original)

    assert original.count('Arbeitsanweisung') == 6
    assert 'Arbeitsanweisung' not in cleaned
    assert 'Version 2.1 – Vertraulich' not in cleaned
    assert 'Seite 14 von 90' not in cleaned
    assert 'Seite 25 von 90' not in cleaned
    assert cleaned.count('Ein fachlicher Satz mit einer belegbaren Aussage.') == 24
    assert cleaned.count('<!-- page:') == 12
    assert '# Fachabschnitt 14' in cleaned
    assert report['page_furniture_lines_removed'] == 24
    assert report['page_furniture_removed'][0] == {'page': 14, 'text': 'Version 2.1 – Vertraulich'}
    assert prepare_rag_text(cleaned) == cleaned


def test_prepare_rag_text_preserves_tables_and_unrepeated_page_edges() -> None:
    original = '\n'.join(
        f'<!-- page:{page}/4 -->\n\n'
        f'# Eigenständiger Abschnitt {page}\n\n'
        '| Merkmal | Wert |\n| --- | --- |\n| Leistung | 100 |\n\n'
        f'Fachlicher Schlussabsatz {page}.\n'
        for page in range(1, 5)
    )

    cleaned, report = prepare_rag_text_with_report(original)

    assert cleaned == original
    assert report['page_furniture_lines_removed'] == 0


def test_prepare_rag_text_keeps_unmarked_markdown_unchanged() -> None:
    original = 'Technische Dokumentation\n\nFachlicher Inhalt.\n\nTechnische Dokumentation'

    assert prepare_rag_text(original) == original


def test_repeated_numeric_measurement_is_not_mistaken_for_page_numbers() -> None:
    original = '\n'.join(
        f'<!-- page:{page}/5 -->\n\n'
        'Fachlicher Abschnitt.\n\n'
        '100\n'
        for page in range(1, 6)
    )

    assert prepare_rag_text(original) == original


def test_legacy_page_headings_also_support_existing_pdf_jobs() -> None:
    original = '\n\n'.join(
        f'## Page {page}\n\n'
        'SFCR - HMK\n\n'
        f'Fachinhalt der PDF-Seite {page}.\n\n'
        f'{page - 2} von 76'
        for page in range(3, 7)
    )

    cleaned, report = prepare_rag_text_with_report(original)

    assert cleaned.count('## Page ') == 4
    assert 'SFCR - HMK' not in cleaned
    assert 'von 76' not in cleaned
    assert report['page_furniture_lines_removed'] == 8
