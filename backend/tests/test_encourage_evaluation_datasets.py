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


def test_dataset_list_exposes_source_and_markdown_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: tmp_path)
    row = _row(source_file_sha256='pdf-hash', source_markdown_sha256='markdown-hash')

    saved = encourage_evaluation.save_evaluation_dataset('hashed.jsonl', [row])
    listed = encourage_evaluation.list_evaluation_datasets()[0]

    assert listed['source_file_sha256'] == 'pdf-hash'
    assert listed['source_markdown_sha256'] == 'markdown-hash'
    assert saved['source_file_sha256'] == 'pdf-hash'
    assert saved['source_markdown_sha256'] == 'markdown-hash'


def test_dataset_creation_timestamp_is_preserved_and_lists_newest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: tmp_path)
    row = _row()

    first = encourage_evaluation.save_evaluation_dataset('first.jsonl', [row])
    second = encourage_evaluation.save_evaluation_dataset('second.jsonl', [_row(id='q-002')])
    overwritten = encourage_evaluation.save_evaluation_dataset(
        'first.jsonl', [{**row, 'question': 'Aktualisierte Frage?'}],
    )

    assert first['created_at'] is not None
    assert overwritten['created_at'] == first['created_at']
    listed = encourage_evaluation.list_evaluation_datasets()
    assert listed[0]['filename'] == 'second.jsonl'
    assert listed[0]['created_at'] >= listed[1]['created_at']


def test_archived_dataset_is_hidden_and_can_be_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_root = tmp_path / 'evaluation'
    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: evaluation_root)
    saved = encourage_evaluation.save_evaluation_dataset('example.jsonl', [_row()])

    archived = encourage_evaluation.archive_evaluation_dataset(saved['path'])

    assert archived['path'] == 'docs/evaluation/archive/example.jsonl'
    assert encourage_evaluation.list_evaluation_datasets() == []
    archived_items = encourage_evaluation.list_evaluation_datasets(archived=True)
    assert [item['filename'] for item in archived_items] == ['example.jsonl']
    assert encourage_evaluation.get_evaluation_dataset_details(archived['path'])['rows'] == saved['rows']

    restored = encourage_evaluation.restore_evaluation_dataset(archived['path'])

    assert restored['path'] == saved['path']
    assert [item['filename'] for item in encourage_evaluation.list_evaluation_datasets()] == ['example.jsonl']
    assert encourage_evaluation.list_evaluation_datasets(archived=True) == []


def test_restore_conflict_preserves_archived_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_root = tmp_path / 'evaluation'
    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: evaluation_root)
    saved = encourage_evaluation.save_evaluation_dataset('example.jsonl', [_row()])
    archived = encourage_evaluation.archive_evaluation_dataset(saved['path'])
    encourage_evaluation.save_evaluation_dataset('example.jsonl', [_row(id='q-002')])

    with pytest.raises(FileExistsError, match='active dataset named example.jsonl'):
        encourage_evaluation.restore_evaluation_dataset(archived['path'])

    assert len(encourage_evaluation.list_evaluation_datasets(archived=True)) == 1
    assert encourage_evaluation.list_evaluation_datasets()[0]['row_count'] == 1


def test_list_datasets_filters_for_selected_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluation_root = tmp_path / 'evaluation'
    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: evaluation_root)

    encourage_evaluation.save_evaluation_dataset(
        'job-1.jsonl',
        [_row(source_document='backend/storage/results/word/job-1/job-1.md')],
    )
    encourage_evaluation.save_evaluation_dataset(
        'job-2.jsonl',
        [_row(id='q-002', source_document='backend/storage/results/word/job-2/job-2.md')],
    )

    items = encourage_evaluation.list_evaluation_datasets(
        markdown_path='/app/backend/storage/results/word/job-1/job-1.md',
    )

    assert [item['filename'] for item in items] == ['job-1.jsonl']
    assert items[0]['matching_row_count'] == 1


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

    with pytest.raises(ValueError, match='evidence_quote is required'):
        encourage_evaluation.save_evaluation_dataset(
            'missing-evidence.jsonl',
            [_row(evidence_quote='')],
        )


def test_save_requires_one_markdown_source_per_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: tmp_path)

    with pytest.raises(ValueError, match='exactly one source_document'):
        encourage_evaluation.save_evaluation_dataset(
            'mixed-sources.jsonl',
            [
                _row(id='q-001', source_document='word/job-1/job-1.md'),
                _row(id='q-002', source_document='word/job-2/job-2.md'),
            ],
        )


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


def test_review_decision_is_persisted_and_controls_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: tmp_path)
    review = _row(generation_pipeline='ragas', validation_status='review',
                  validation_faithfulness=1.0, validation_answer_relevancy=None)
    accepted = _row(id='q-002', generation_pipeline='ragas', validation_status='accepted')
    saved = encourage_evaluation.save_evaluation_dataset('review.jsonl', [review, accepted])

    assert [row['id'] for row in encourage_evaluation._active_evaluation_rows(saved['rows'])] == ['q-002']
    excluded = encourage_evaluation.set_evaluation_dataset_review_decision(
        saved['path'], 'q-001', 'excluded',
        expected_updated_at=saved['updated_at'], decided_by='user-1',
    )
    assert excluded['row_count'] == 2
    assert excluded['rows'][0]['review_decision'] == 'excluded'
    assert excluded['rows'][0]['review_decided_by'] == 'user-1'
    assert excluded['rows'][0]['validation_faithfulness'] == 1.0
    assert [row['id'] for row in encourage_evaluation._active_evaluation_rows(excluded['rows'])] == ['q-002']

    included = encourage_evaluation.set_evaluation_dataset_review_decision(
        saved['path'], 'q-001', 'included',
        expected_updated_at=excluded['updated_at'], decided_by='user-1',
    )
    assert [row['id'] for row in encourage_evaluation._active_evaluation_rows(included['rows'])] == ['q-001', 'q-002']
    reloaded = encourage_evaluation.get_evaluation_dataset_details(saved['path'])
    assert reloaded['rows'][0]['review_decision'] == 'included'
    assert reloaded['rows'][0]['review_decided_at']


def test_review_decision_rejects_stale_or_non_review_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import timedelta

    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: tmp_path)
    saved = encourage_evaluation.save_evaluation_dataset('review.jsonl', [
        _row(generation_pipeline='ragas', validation_status='review'),
        _row(id='q-002', generation_pipeline='ragas', validation_status='accepted'),
    ])

    with pytest.raises(encourage_evaluation.DatasetVersionConflict):
        encourage_evaluation.set_evaluation_dataset_review_decision(
            saved['path'], 'q-001', 'included',
            expected_updated_at=saved['updated_at'] - timedelta(seconds=1), decided_by='user-1',
        )
    with pytest.raises(ValueError, match='Only RAGAS questions'):
        encourage_evaluation.set_evaluation_dataset_review_decision(
            saved['path'], 'q-002', 'excluded',
            expected_updated_at=saved['updated_at'], decided_by='user-1',
        )


def test_review_decision_api_persists_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4
    from conftest import create_test_user, login_as

    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: tmp_path)
    saved = encourage_evaluation.save_evaluation_dataset('api-review.jsonl', [
        _row(generation_pipeline='ragas', validation_status='review'),
    ])
    name = 'review-' + uuid4().hex[:12]
    create_test_user(username=name, email=name + '@example.test')
    client = login_as(name)
    payload = {
        'dataset_path': saved['path'], 'row_id': 'q-001', 'decision': 'excluded',
        'expected_updated_at': saved['updated_at'].isoformat(),
    }

    response = client.post('/api/v1/evaluation-datasets/review-decision', json=payload)

    assert response.status_code == 200, response.text
    assert response.json()['rows'][0]['review_decision'] == 'excluded'
    assert client.post('/api/v1/evaluation-datasets/review-decision', json=payload).status_code == 409
