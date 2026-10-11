from app.services.rag_text_preprocessing import count_picture_text_blocks, prepare_rag_text


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
