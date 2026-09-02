from pathlib import Path

import pytest

from app.services import encourage_evaluation


def _row(**overrides: str) -> dict[str, str]:
    row = {
        'id': 'q-001',
        'question': 'Wie hoch ist die Erstattung?',
        'gold_answer': 'Sie beträgt 42 Euro.',
        'evidence_quote': 'Die Erstattung beträgt 42 Euro.',
        'evidence_anchor': 'Erstattung',
        'source_document': 'word/job-1/job-1.md',
        'source_file': 'TypischeDokumente/beispiel.docx',
        'notes': '',
    }
    row.update(overrides)
    return row


def test_save_list_and_load_dataset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evaluation_root = tmp_path / 'evaluation'
    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: evaluation_root)

    saved = encourage_evaluation.save_evaluation_dataset('example.jsonl', [_row()])

    assert saved['path'] == 'docs/evaluation/example.jsonl'
    assert saved['row_count'] == 1
    assert saved['rows'][0]['source_file'] == 'TypischeDokumente/beispiel.docx'
    assert encourage_evaluation.list_evaluation_datasets()[0]['filename'] == 'example.jsonl'
    loaded = encourage_evaluation.get_evaluation_dataset_details(saved['path'])
    assert loaded['rows'][0]['question'] == 'Wie hoch ist die Erstattung?'


@pytest.mark.parametrize('filename', ['../escape.jsonl', 'nested/data.jsonl', 'not-json.txt'])
def test_save_rejects_invalid_dataset_paths(
    filename: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: tmp_path)

    with pytest.raises(ValueError, match='Invalid evaluation dataset path'):
        encourage_evaluation.save_evaluation_dataset(filename, [_row()])


def test_save_validates_required_fields_and_unique_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: tmp_path)

    with pytest.raises(ValueError, match='question is required'):
        encourage_evaluation.save_evaluation_dataset('missing.jsonl', [_row(question='')])

    with pytest.raises(ValueError, match='duplicate id'):
        encourage_evaluation.save_evaluation_dataset('duplicate.jsonl', [_row(), _row()])


def test_lists_only_real_word_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_root = tmp_path / '.docs'
    source_folder = source_root / 'TypischeDokumente'
    source_folder.mkdir(parents=True)
    (source_folder / 'Beispiel.docx').write_bytes(b'docx')
    (source_folder / 'Notizen.txt').write_text('ignore', encoding='utf-8')
    metadata_folder = source_folder / '__MACOSX'
    metadata_folder.mkdir()
    (metadata_folder / '._Beispiel.docx').write_bytes(b'metadata')
    monkeypatch.setattr(encourage_evaluation, '_source_documents_root', lambda: source_root)

    items = encourage_evaluation.list_evaluation_source_documents()

    assert [item['path'] for item in items] == ['TypischeDokumente/Beispiel.docx']


def test_dataset_rows_match_relative_markdown_suffix() -> None:
    rows = [_row(source_document='word/job-1/job-1.md')]

    matched = encourage_evaluation._resolve_dataset_rows(
        rows,
        markdown_path='/app/backend/storage/results/word/job-1/job-1.md',
    )

    assert matched == rows
