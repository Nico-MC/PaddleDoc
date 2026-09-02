from app.services.encourage_bridge import _chunk_markdown_by_sections


def test_chunking_keeps_heading_path_with_section_content():
    markdown = (
        '# Tarif AZS\n\n'
        'Einleitung.\n\n'
        '## II. Leistungen\n\n'
        '### 1. Ambulante Heilbehandlung\n\n'
        '#### 1.5. Hilfsmittel\n\n'
        '- 1.5.1. Hörhilfen\n'
        '- 1.5.2. Sehhilfen\n\n'
        '#### 1.6. Digitale Gesundheitsanwendungen\n\n'
        'Beschreibung der Anwendungen.'
    )

    chunks = _chunk_markdown_by_sections(markdown, max_chars=180, overlap_chars=20)

    hilfsmittel = next(chunk for chunk in chunks if 'Hörhilfen' in chunk)
    assert '# Tarif AZS' in hilfsmittel
    assert '## II. Leistungen' in hilfsmittel
    assert '### 1. Ambulante Heilbehandlung' in hilfsmittel
    assert '#### 1.5. Hilfsmittel' in hilfsmittel
    assert 'Digitale Gesundheitsanwendungen' not in hilfsmittel

    diga = next(chunk for chunk in chunks if 'Beschreibung der Anwendungen.' in chunk)
    assert '#### 1.6. Digitale Gesundheitsanwendungen' in diga
    assert '#### 1.5. Hilfsmittel' not in diga


def test_chunking_does_not_emit_headings_as_separate_chunks():
    chunks = _chunk_markdown_by_sections(
        '# Document\n\n## Section\n\nShort content.',
        max_chars=120,
        overlap_chars=10,
    )

    assert chunks == ['# Document\n\n## Section\n\nShort content.']


def test_chunking_splits_long_content_on_word_boundaries_with_context():
    paragraph = ' '.join(f'word{index}' for index in range(80))

    chunks = _chunk_markdown_by_sections(
        f'# Document\n\n## Long section\n\n{paragraph}',
        max_chars=160,
        overlap_chars=20,
    )

    assert len(chunks) > 1
    assert all(chunk.startswith('# Document\n\n## Long section\n\n') for chunk in chunks)
    assert all(len(chunk) <= 160 for chunk in chunks)
    assert all(not chunk.endswith('word') for chunk in chunks)


def test_chunking_repeats_table_headers_when_splitting_large_tables():
    markdown = (
        '# Tarif\n\n'
        '## Leistungen\n\n'
        '| Tarifklasse | Erwachsene |\n'
        '| --- | --- |\n'
        '|  | garantierte Beitragsrückerstattung |\n'
        '| AZS/50 | 600 EUR |\n'
        '| AZS/25 | 300 EUR |\n'
        '| AZS/10 | 120 EUR |'
    )

    chunks = _chunk_markdown_by_sections(markdown, max_chars=140, overlap_chars=20)

    assert len(chunks) == 3
    assert all('| Tarifklasse | Erwachsene |' in chunk for chunk in chunks)
    assert all('|  | garantierte Beitragsrückerstattung |' in chunk for chunk in chunks)
    assert all('# Tarif\n\n## Leistungen' in chunk for chunk in chunks)
