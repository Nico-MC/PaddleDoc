import json
import hashlib
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import routes
from app.models.models import JobStatus
from app.schemas.jobs import EvaluationDatasetGenerateRequest
from app.workers import dataset_tasks
from app.services import encourage_evaluation
from app.services.dataset_assistant import _normalize_dataset_api_base_url
from app.services.encourage_bridge import _normalize_openai_base_url


class _Store:
    def __init__(self):
        self.hashes = {}
        self.locks = set()

    def hset(self, key, field=None, value=None, mapping=None):
        self.hashes.setdefault(key, {}).update(mapping or {field: value})

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def expire(self, key, ttl):
        return True

    def set(self, key, value, nx=False, ex=None):
        if key in self.locks:
            return False
        self.locks.add(key)
        return True

    def delete(self, key):
        self.locks.discard(key)


class _Database:
    def __init__(self, jobs):
        self.jobs = {job.id: job for job in jobs}

    def get(self, model, job_id, **kwargs):
        return self.jobs.get(job_id)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def _job(job_id='job-1'):
    return SimpleNamespace(
        id=job_id, owner_id='user-1', status=JobStatus.FINISHED,
        original_filename='original.pdf', content_sha256='pdf-content-hash',
        result_markdown='Eine belegbare Passage.',
    )


@pytest.mark.parametrize(('base_url', 'expected'), [
    ('https://aihub.example', 'https://aihub.example/api'),
    ('https://aihub.example/', 'https://aihub.example/api'),
    ('https://aihub.example/api', 'https://aihub.example/api'),
    ('http://ollama:11434/v1', 'http://ollama:11434/v1'),
])
def test_normalize_dataset_api_base_url(base_url, expected):
    normalized_url = _normalize_dataset_api_base_url(base_url)
    assert normalized_url == expected
    assert _normalize_openai_base_url(normalized_url) == expected


def _start(monkeypatch, store, jobs, existing=None, model_name=None, overwrite_datasets=None, skip_existing=True, sampling_seed=None):
    monkeypatch.setattr(routes, 'enforce_rate_limit', lambda _: None)
    monkeypatch.setattr(routes, 'generation_store', lambda: store)
    monkeypatch.setattr(routes, '_owner_visible', lambda *args: True)
    monkeypatch.setattr(routes, '_synthetic_markdown_path', lambda job: f'{job.id}.md')
    monkeypatch.setattr(routes, 'list_evaluation_datasets', lambda: existing or [])
    monkeypatch.setattr(routes.settings, 'dataset_llm_api_base_url', 'http://ollama:11434/v1')
    monkeypatch.setattr(routes.settings, 'dataset_llm_api_key', 'ollama')
    monkeypatch.setattr(routes, 'list_dataset_models', lambda **kwargs: [routes.settings.dataset_llm_model, 'qwen2.5:3b'])
    calls = []
    monkeypatch.setattr(routes.generate_document_dataset, 'apply_async', lambda **kwargs: calls.append(kwargs))
    result = routes.start_dataset_generation(
        EvaluationDatasetGenerateRequest(
            markdown_paths=[f'{job.id}.md' for job in jobs], model_name=model_name,
            skip_existing=skip_existing, overwrite_datasets=overwrite_datasets or {},
            sampling_seed=sampling_seed,
        ),
        SimpleNamespace(), _Database(jobs), SimpleNamespace(id='user-1'),
    )
    return result['run_id'], calls


def test_generation_queues_only_new_documents(monkeypatch):
    store = _Store()
    current_markdown_hash = hashlib.sha256(_job().result_markdown.encode('utf-8')).hexdigest()
    existing = [{
        'source_documents': ['job-1.md'],
        'source_file_sha256': 'pdf-content-hash',
        'source_markdown_sha256': current_markdown_hash,
    }]
    run_id, calls = _start(monkeypatch, store, [_job(), _job('job-2')], existing)
    assert calls == [{'args': [run_id, 'job-2'], 'queue': 'datasets'}]
    result = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))
    assert [item['status'] for item in result['items']] == ['skipped', 'queued']
    assert result['finished'] is False


def test_generation_skip_matches_dataset_from_another_markdown_for_same_pdf(monkeypatch):
    store = _Store()
    existing = [{
        'path': 'docs/evaluation/old.jsonl',
        'filename': 'old.jsonl',
        'source_documents': ['old-job.md'],
        'source_files': ['original.pdf'],
        'source_file_sha256': 'pdf-content-hash',
        'source_markdown_sha256': 'outdated-markdown-hash',
    }]
    run_id, calls = _start(monkeypatch, store, [_job()], existing=existing)

    result = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))

    assert calls == [{'args': [run_id, 'job-1'], 'queue': 'datasets'}]
    assert result['items'][0]['status'] == 'queued'


def test_generation_does_not_match_legacy_dataset_by_filename_alone(monkeypatch):
    store = _Store()
    existing = [{
        'path': 'docs/evaluation/legacy.jsonl',
        'filename': 'legacy.jsonl',
        'source_documents': ['deleted-job.md'],
        'source_files': ['original.pdf'],
    }]

    run_id, calls = _start(monkeypatch, store, [_job()], existing=existing)
    result = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))

    assert calls == [{'args': [run_id, 'job-1'], 'queue': 'datasets'}]
    assert result['items'][0]['status'] == 'queued'


def test_generation_rejects_legacy_overwrite_matched_only_by_filename(monkeypatch):
    store = _Store()
    existing = [{
        'path': 'docs/evaluation/legacy.jsonl',
        'filename': 'legacy.jsonl',
        'source_documents': ['deleted-job.md'],
        'source_files': ['original.pdf'],
    }]

    with pytest.raises(HTTPException) as error:
        _start(
            monkeypatch, store, [_job()], existing=existing, skip_existing=False,
            overwrite_datasets={'job-1.md': 'docs/evaluation/legacy.jsonl'},
        )

    assert error.value.status_code == 422
    assert not store.hashes


def test_generation_skip_skips_when_markdown_hash_matches(monkeypatch):
    store = _Store()
    current_markdown = _job().result_markdown
    existing = [{
        'path': 'docs/evaluation/current.jsonl',
        'filename': 'current.jsonl',
        'source_documents': ['job-1.md'],
        'source_files': ['original.pdf'],
        'source_file_sha256': 'pdf-content-hash',
        'source_markdown_sha256': hashlib.sha256(current_markdown.encode('utf-8')).hexdigest(),
    }]

    run_id, calls = _start(monkeypatch, store, [_job()], existing=existing)
    result = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))

    assert calls == []
    assert result['items'][0]['status'] == 'skipped'


def test_generation_does_not_skip_when_markdown_hash_is_outdated(monkeypatch):
    store = _Store()
    existing = [{
        'path': 'docs/evaluation/old.jsonl',
        'filename': 'old.jsonl',
        'source_documents': ['job-1.md'],
        'source_files': ['original.pdf'],
        'source_file_sha256': 'pdf-content-hash',
        'source_markdown_sha256': 'outdated-markdown-hash',
    }]

    run_id, calls = _start(monkeypatch, store, [_job()], existing=existing)
    result = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))

    assert calls == [{'args': [run_id, 'job-1'], 'queue': 'datasets'}]
    assert result['items'][0]['status'] == 'queued'


def test_generation_does_not_skip_legacy_dataset_without_markdown_hash(monkeypatch):
    store = _Store()
    existing = [{
        'path': 'docs/evaluation/legacy.jsonl',
        'filename': 'legacy.jsonl',
        'source_documents': ['job-1.md'],
        'source_files': ['original.pdf'],
    }]

    run_id, calls = _start(monkeypatch, store, [_job()], existing=existing)
    result = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))

    assert calls == [{'args': [run_id, 'job-1'], 'queue': 'datasets'}]
    assert result['items'][0]['status'] == 'queued'


def test_generation_can_overwrite_dataset_from_another_markdown_for_same_pdf(monkeypatch):
    store = _Store()
    existing = [{
        'path': 'docs/evaluation/old.jsonl',
        'filename': 'old.jsonl',
        'source_documents': ['old-job.md'],
        'source_files': ['original.pdf'],
        'source_file_sha256': 'pdf-content-hash',
        'source_markdown_sha256': 'outdated-markdown-hash',
    }]
    run_id, calls = _start(
        monkeypatch, store, [_job()], existing=existing, skip_existing=False,
        overwrite_datasets={'job-1.md': 'docs/evaluation/old.jsonl'},
    )

    item = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))['items'][0]
    assert len(calls) == 1
    assert item['target_dataset_filename'] == 'old.jsonl'


def test_generation_rejects_overwrite_when_known_pdf_hash_differs(monkeypatch):
    store = _Store()
    existing = [{
        'path': 'docs/evaluation/other.pdf.jsonl',
        'filename': 'other.pdf.jsonl',
        'source_documents': ['old-job.md'],
        'source_files': ['original.pdf'],
        'source_file_sha256': 'different-pdf-content-hash',
        'source_markdown_sha256': 'old-markdown-hash',
    }]

    with pytest.raises(HTTPException) as error:
        _start(
            monkeypatch, store, [_job()], existing=existing, skip_existing=False,
            overwrite_datasets={'job-1.md': 'docs/evaluation/other.pdf.jsonl'},
        )

    assert error.value.status_code == 422
    assert not store.hashes


def test_generation_status_is_owner_scoped(monkeypatch):
    store = _Store()
    run_id, _ = _start(monkeypatch, store, [_job()])
    with pytest.raises(HTTPException) as error:
        routes.dataset_generation_status(run_id, SimpleNamespace(id='another-user'))
    assert error.value.status_code == 404


def test_generation_rejects_invisible_source_before_enqueue(monkeypatch):
    store = _Store()
    monkeypatch.setattr(routes, 'enforce_rate_limit', lambda _: None)
    monkeypatch.setattr(routes.settings, 'dataset_llm_api_base_url', 'local')
    monkeypatch.setattr(routes.settings, 'dataset_llm_api_key', 'local')
    monkeypatch.setattr(routes, '_synthetic_markdown_path', lambda job: f'{job.id}.md')
    monkeypatch.setattr(routes, '_owner_visible', lambda *args: False)
    monkeypatch.setattr(routes, 'generation_store', lambda: pytest.fail('Must not create a run'))
    with pytest.raises(HTTPException) as error:
        routes.start_dataset_generation(
            EvaluationDatasetGenerateRequest(markdown_paths=['job-1.md']),
            SimpleNamespace(), _Database([_job()]), SimpleNamespace(id='user-1'),
        )
    assert error.value.status_code == 404


def test_cancel_generation_never_calls_llm(monkeypatch):
    store = _Store()
    run_id, _ = _start(monkeypatch, store, [_job()])
    routes.cancel_dataset_generation(run_id, SimpleNamespace(id='user-1'))
    monkeypatch.setattr(dataset_tasks, 'generation_store', lambda: store)
    monkeypatch.setattr(dataset_tasks, 'generate_dataset_rows', lambda **kwargs: pytest.fail('Cancelled run must not call LLM'))
    dataset_tasks.generate_document_dataset.run(run_id, 'job-1')
    result = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))
    assert result['finished'] is True
    assert result['items'][0]['status'] == 'cancelled'


def test_generation_worker_saves_once_and_records_progress(monkeypatch):
    store = _Store()
    run_id, _ = _start(monkeypatch, store, [_job()], model_name='qwen2.5:3b')
    monkeypatch.setattr(dataset_tasks, 'generation_store', lambda: store)
    monkeypatch.setattr(dataset_tasks, 'SessionLocal', lambda: _Database([_job()]))
    captured = []
    def generate(**kwargs):
        captured.append(kwargs)
        kwargs['progress_callback']({
            'phase': 'coverage', 'region': 1, 'region_count': 1,
            'passage_attempt': 1, 'passages_checked': 1, 'passages_available': 2,
            'questions_generated': 1, 'question_target': 10,
        })
        return [{'id': 'q001'}]
    monkeypatch.setattr(dataset_tasks, 'generate_dataset_rows', generate)
    saved = []
    monkeypatch.setattr(dataset_tasks, 'save_evaluation_dataset', lambda filename, rows: saved.append(filename) or {'path': f'docs/evaluation/{filename}'})
    dataset_tasks.generate_document_dataset.run(run_id, 'job-1')
    dataset_tasks.generate_document_dataset.run(run_id, 'job-1')
    result = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))
    assert result['finished'] is True
    assert result['items'][0]['status'] == 'completed'
    assert result['items'][0]['row_count'] == 1
    assert 'Nur 1 von 10' in result['items'][0]['warning']
    assert 'Dokumentabschnitten' not in result['items'][0]['warning']
    assert '1 von 10' in result['items'][0]['coverage_note']
    assert captured[0]['source_document'] == 'job-1.md'
    assert captured[0]['source_file'] == 'original.pdf'
    assert captured[0]['model_name'] == 'qwen2.5:3b'
    assert isinstance(captured[0]['sampling_seed'], int)
    assert len(saved) == 1
    result_item = result['items'][0]
    assert result_item['dataset_action'] == 'created'
    assert result_item['dataset_filename'] == saved[0]
    assert isinstance(result_item['started_at'], float)
    assert result_item['duration_seconds'] >= 0
    assert result_item['progress']['passages_checked'] == 1


def test_generation_worker_records_failures(monkeypatch):
    store = _Store()
    run_id, _ = _start(monkeypatch, store, [_job()])
    monkeypatch.setattr(dataset_tasks, 'generation_store', lambda: store)
    monkeypatch.setattr(dataset_tasks, 'SessionLocal', lambda: _Database([]))
    dataset_tasks.generate_document_dataset.run(run_id, 'job-1')
    item = json.loads(store.hget(dataset_tasks.generation_key(run_id), 'job-1'))
    assert item['status'] == 'failed'
    assert 'no longer available' in item['error']
    assert item['duration_seconds'] >= 0
    assert not store.locks


def test_generation_rejects_uninstalled_model(monkeypatch):
    store = _Store()
    with pytest.raises(HTTPException) as error:
        _start(monkeypatch, store, [_job()], model_name='missing-model')
    assert error.value.status_code == 422
    assert not store.hashes


def test_generation_config_lists_installed_models(monkeypatch):
    monkeypatch.setattr(routes.settings, 'dataset_llm_api_base_url', 'http://ollama:11434/v1')
    monkeypatch.setattr(routes.settings, 'dataset_llm_api_key', 'ollama')
    monkeypatch.setattr(routes, 'list_dataset_models', lambda **kwargs: ['qwen2.5:3b', 'qwen2.5:14b'])
    result = routes.dataset_generation_config()
    assert result['configured'] is True
    assert result['models'] == ['qwen2.5:3b', 'qwen2.5:14b']


def test_generation_overwrites_only_selected_dataset(monkeypatch):
    store = _Store()
    existing = [
        {'path': 'docs/evaluation/old.jsonl', 'filename': 'old.jsonl', 'source_documents': ['job-1.md']},
        {'path': 'docs/evaluation/other.jsonl', 'filename': 'other.jsonl', 'source_documents': ['job-1.md']},
    ]
    run_id, calls = _start(
        monkeypatch, store, [_job()], existing, skip_existing=False,
        overwrite_datasets={'job-1.md': 'docs/evaluation/old.jsonl'},
    )
    monkeypatch.setattr(dataset_tasks, 'generation_store', lambda: store)
    monkeypatch.setattr(dataset_tasks, 'SessionLocal', lambda: _Database([_job()]))
    monkeypatch.setattr(dataset_tasks, 'generate_dataset_rows', lambda **kwargs: [{
        'id': 'q001', 'sampling_region': 1, 'sampling_region_count': 10,
    }])
    saved = []
    monkeypatch.setattr(dataset_tasks, 'save_evaluation_dataset', lambda filename, rows: saved.append(filename) or {'path': f'docs/evaluation/{filename}'})
    dataset_tasks.generate_document_dataset.run(run_id, 'job-1')
    assert saved == ['old.jsonl']
    item = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))['items'][0]
    assert item['dataset_action'] == 'overwritten'
    assert item['dataset_filename'] == 'old.jsonl'
    assert len(calls) == 1


def test_generation_rejects_overwrite_for_other_document(monkeypatch):
    store = _Store()
    with pytest.raises(HTTPException) as error:
        _start(
            monkeypatch, store, [_job()], skip_existing=False,
            existing=[{'path': 'docs/evaluation/other.jsonl', 'filename': 'other.jsonl', 'source_documents': ['job-2.md']}],
            overwrite_datasets={'job-1.md': 'docs/evaluation/other.jsonl'},
        )
    assert error.value.status_code == 422
    assert not store.hashes


def test_generation_failure_keeps_overwrite_target(monkeypatch):
    store = _Store()
    run_id, _ = _start(
        monkeypatch, store, [_job()], skip_existing=False,
        existing=[{'path': 'docs/evaluation/old.jsonl', 'filename': 'old.jsonl', 'source_documents': ['job-1.md']}],
        overwrite_datasets={'job-1.md': 'docs/evaluation/old.jsonl'},
    )
    monkeypatch.setattr(dataset_tasks, 'generation_store', lambda: store)
    monkeypatch.setattr(dataset_tasks, 'SessionLocal', lambda: _Database([_job()]))
    def fail_generation(**kwargs):
        raise RuntimeError('LLM failed')
    monkeypatch.setattr(dataset_tasks, 'generate_dataset_rows', fail_generation)
    monkeypatch.setattr(dataset_tasks, 'save_evaluation_dataset', lambda *args: pytest.fail('Must not overwrite on failure'))
    dataset_tasks.generate_document_dataset.run(run_id, 'job-1')
    assert json.loads(store.hget(dataset_tasks.generation_key(run_id), 'job-1'))['status'] == 'failed'


def test_dataset_overwrite_replaces_rows_without_creating_extra_file(monkeypatch):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        monkeypatch.setattr(encourage_evaluation, '_evaluation_root', lambda: root)
        row = {
            'id': 'q001', 'question': 'Alte Frage?', 'gold_answer': 'Alte Antwort.',
            'evidence_quote': 'Ein Beleg.', 'source_document': 'job-1.md',
        }
        encourage_evaluation.save_evaluation_dataset('target.jsonl', [row])
        encourage_evaluation.save_evaluation_dataset('other.jsonl', [row])
        other_content = (root / 'other.jsonl').read_text()
        updated = {**row, 'question': 'Neue Frage?', 'gold_answer': 'Neue Antwort.'}
        result = encourage_evaluation.save_evaluation_dataset('target.jsonl', [updated])
        assert result['path'] == 'docs/evaluation/target.jsonl'
        assert result['rows'] == [updated]
        assert sorted(path.name for path in root.iterdir()) == ['other.jsonl', 'target.jsonl']
        assert (root / 'other.jsonl').read_text() == other_content


def test_generation_preserves_explicit_zero_seed(monkeypatch):
    store = _Store()
    run_id, _ = _start(monkeypatch, store, [_job()], sampling_seed=0)
    result = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))
    assert result['sampling_seed'] == 0
    with pytest.raises(ValidationError):
        EvaluationDatasetGenerateRequest(markdown_paths=['job-1.md'], sampling_seed=-1)