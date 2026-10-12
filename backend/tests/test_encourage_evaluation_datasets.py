import json
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


def test_ragas_dataset_export_excludes_nested_rejected_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: tmp_path)
    row = _row()
    row['generation_pipeline'] = 'ragas'
    row['generation_outcome'] = {
        'generated': 2, 'saved': 1, 'target': 2,
        'rejected_candidates': [
            {'id': 'q-002', 'question': 'Verworfene Frage?', 'reason': 'Antwort-Relevanz zu niedrig'},
        ],
    }

    saved = encourage_evaluation.save_evaluation_dataset('ragas.jsonl', [row])
    on_disk = json.loads((tmp_path / 'ragas.jsonl').read_text(encoding='utf-8').strip())
    assert on_disk['question'] == row['question']
    assert on_disk['generation_outcome']['rejected_candidates'] == [
        {'id': 'q-002', 'reason': 'Antwort-Relevanz zu niedrig'},
    ]
    assert saved['rows'] == [on_disk]

    # Old JSONL files may still contain the nested question. The API used by
    # the download button must export the clean row without rewriting the file.
    (tmp_path / 'ragas.jsonl').write_text(json.dumps(row, ensure_ascii=False) + '\n', encoding='utf-8')
    loaded = encourage_evaluation.get_evaluation_dataset_details(saved['path'])
    assert loaded['row_count'] == 1
    assert loaded['rows'][0]['generation_outcome']['rejected_candidates'] == [
        {'id': 'q-002', 'reason': 'Antwort-Relevanz zu niedrig'},
    ]


def test_rejected_candidates_are_browsable_but_not_dataset_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: tmp_path)
    row = _row()
    row['generation_pipeline'] = 'ragas'
    row['generation_outcome'] = {
        'run_id': 'run-1', 'generated': 2, 'saved': 1, 'target': 2,
        'rejected_candidates': [{'id': 'q-002', 'reason': 'Antwort-Relevanz zu niedrig'}],
    }
    candidate = {
        'id': 'q-002', 'question': 'Verworfene Frage?', 'reason': 'Antwort-Relevanz zu niedrig',
        'faithfulness': 0.9, 'answer_relevancy': 0.1,
        'row': _row(id='q-002', question='Verworfene Frage?', gold_answer='Verworfene Antwort'),
    }

    saved = encourage_evaluation.save_evaluation_dataset(
        'ragas.jsonl', [row], rejected_candidates=[candidate], generation_run_id='run-1',
    )
    assert saved['row_count'] == 1
    assert saved['rejected_candidates'] == [candidate]
    assert saved['rows'][0]['generation_outcome']['rejected_candidates'] == [
        {'id': 'q-002', 'reason': 'Antwort-Relevanz zu niedrig'},
    ]
    assert 'Verworfene Frage?' not in (tmp_path / 'ragas.jsonl').read_text(encoding='utf-8')

    reviewed = encourage_evaluation.save_evaluation_dataset('ragas.jsonl', saved['rows'])
    assert reviewed['rejected_candidates'] == [candidate]
    archived = encourage_evaluation.archive_evaluation_dataset(reviewed['path'])
    assert archived['rejected_candidates'] == [candidate]
    assert not (tmp_path / 'ragas.jsonl.rejected-candidates.json').exists()
    restored = encourage_evaluation.restore_evaluation_dataset(archived['path'])
    assert restored['rejected_candidates'] == [candidate]
    (tmp_path / 'ragas.jsonl.rejected-candidates.json').write_text('{', encoding='utf-8')
    assert encourage_evaluation.get_evaluation_dataset_details(restored['path'])['rejected_candidates'] == [
        {'id': 'q-002', 'reason': 'Antwort-Relevanz zu niedrig'},
    ]

    newer_row = {**row, 'generation_outcome': {
        'run_id': 'run-2', 'generated': 1, 'saved': 1, 'target': 1, 'rejected_candidates': [],
    }}
    newer = encourage_evaluation.save_evaluation_dataset(
        'ragas.jsonl', [newer_row], rejected_candidates=[], generation_run_id='run-2',
    )
    assert newer['rejected_candidates'] == []


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
        reason='not_answerable', note='Nur Tabellenverzeichnis',
    )
    assert excluded['row_count'] == 2
    assert excluded['rows'][0]['review_decision'] == 'excluded'
    assert excluded['rows'][0]['review_decided_by'] == 'user-1'
    assert excluded['rows'][0]['validation_faithfulness'] == 1.0
    assert [row['id'] for row in encourage_evaluation._active_evaluation_rows(excluded['rows'])] == ['q-002']
    archived_questions = encourage_evaluation.list_archived_dataset_questions(review['source_document'])
    assert len(archived_questions) == 1
    assert archived_questions[0]['reason'] == 'not_answerable'
    assert archived_questions[0]['row']['question'] == review['question']

    included = encourage_evaluation.set_evaluation_dataset_review_decision(
        saved['path'], 'q-001', 'included',
        expected_updated_at=excluded['updated_at'], decided_by='user-1',
    )
    assert [row['id'] for row in encourage_evaluation._active_evaluation_rows(included['rows'])] == ['q-001', 'q-002']
    reloaded = encourage_evaluation.get_evaluation_dataset_details(saved['path'])
    assert reloaded['rows'][0]['review_decision'] == 'included'
    assert reloaded['rows'][0]['review_decided_at']
    assert encourage_evaluation.list_archived_dataset_questions(review['source_document']) == []


def test_review_decision_rejects_stale_or_missing_rows(
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
    with pytest.raises(ValueError, match='Question not found'):
        encourage_evaluation.set_evaluation_dataset_review_decision(
            saved['path'], 'missing', 'excluded',
            expected_updated_at=saved['updated_at'], decided_by='user-1',
        )
    excluded = encourage_evaluation.set_evaluation_dataset_review_decision(
        saved['path'], 'q-002', 'excluded', expected_updated_at=saved['updated_at'], decided_by='user-1',
    )
    assert [row['id'] for row in encourage_evaluation._active_evaluation_rows(excluded['rows'])] == []


def test_question_archive_survives_dataset_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: tmp_path)
    first_row = _row(generation_pipeline='ragas', validation_status='accepted')
    saved = encourage_evaluation.save_evaluation_dataset('same.jsonl', [first_row])
    encourage_evaluation.set_evaluation_dataset_review_decision(
        saved['path'], 'q-001', 'excluded', expected_updated_at=saved['updated_at'],
        decided_by='user-1', reason='uninformative', note='Frage sagt nichts Fachliches',
    )

    # A new generation run can reuse the filename and row ID.
    second_row = _row(question='Andere Frage?', gold_answer='Andere Antwort.',
                      generation_pipeline='ragas', validation_status='accepted')
    overwritten = encourage_evaluation.save_evaluation_dataset('same.jsonl', [second_row])
    archived = encourage_evaluation.list_archived_dataset_questions(first_row['source_document'])
    assert len(archived) == 1
    assert archived[0]['row']['question'] == first_row['question']
    assert archived[0]['note'] == 'Frage sagt nichts Fachliches'
    assert encourage_evaluation._active_evaluation_rows(overwritten['rows']) == overwritten['rows']

    encourage_evaluation.set_evaluation_dataset_review_decision(
        overwritten['path'], 'q-001', 'excluded', expected_updated_at=overwritten['updated_at'],
        decided_by='user-1', reason='incorrect_answer',
    )
    archived = encourage_evaluation.list_archived_dataset_questions(first_row['source_document'])
    assert {entry['row']['question'] for entry in archived} == {
        first_row['question'], second_row['question'],
    }


def test_question_archive_includes_legacy_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: tmp_path)
    row = _row(generation_pipeline='ragas', validation_status='review', review_decision='excluded')
    encourage_evaluation.save_evaluation_dataset('legacy.jsonl', [row])
    archived = encourage_evaluation.list_archived_dataset_questions(row['source_document'])
    assert len(archived) == 1
    assert archived[0]['row']['question'] == row['question']


def test_review_decision_api_persists_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import uuid4
    from app.api.routes import _synthetic_markdown_path
    from app.models.models import Job, JobStatus
    from conftest import TestingSessionLocal, create_test_user, login_as

    monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: tmp_path)
    name = 'review-' + uuid4().hex[:12]
    user = create_test_user(username=name, email=name + '@example.test')
    job_id = str(uuid4())
    with TestingSessionLocal() as db:
        job = Job(
            id=job_id, original_filename='source.pdf', upload_path='/tmp/source.pdf',
            upload_content=b'%PDF-1.4 test', status=JobStatus.FINISHED,
            result_markdown='# Quelle', owner_id=user.id,
        )
        db.add(job)
        db.commit()
        source_document = _synthetic_markdown_path(job)
    saved = encourage_evaluation.save_evaluation_dataset('api-review.jsonl', [
        _row(source_document=source_document, source_file='source.pdf',
             generation_pipeline='ragas', validation_status='review'),
    ])
    client = login_as(name)
    payload = {
        'dataset_path': saved['path'], 'row_id': 'q-001', 'decision': 'excluded',
        'expected_updated_at': saved['updated_at'].isoformat(),
    }
    pdf_response = client.get(f'/api/v1/jobs/{job_id}/source-pdf')
    assert pdf_response.status_code == 200
    assert pdf_response.content == b'%PDF-1.4 test'
    other_name = 'other-' + uuid4().hex[:12]
    create_test_user(username=other_name, email=other_name + '@example.test')
    other_client = login_as(other_name)
    assert other_client.get(f'/api/v1/jobs/{job_id}/source-pdf').status_code == 404
    assert other_client.get('/api/v1/evaluation-datasets/question-archive',
                            params={'markdown_path': source_document}).status_code == 404
    assert other_client.post('/api/v1/evaluation-datasets/review-decision', json=payload).status_code == 404


    response = client.post('/api/v1/evaluation-datasets/review-decision', json=payload)

    assert response.status_code == 200, response.text
    assert response.json()['rows'][0]['review_decision'] == 'excluded'
    archived_response = client.get('/api/v1/evaluation-datasets/question-archive',
                                   params={'markdown_path': source_document})
    assert archived_response.status_code == 200
    assert archived_response.json()['items'][0]['row_id'] == 'q-001'
    assert client.post('/api/v1/evaluation-datasets/review-decision', json=payload).status_code == 409
