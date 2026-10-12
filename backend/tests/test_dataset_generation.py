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
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def hset(self, key, field=None, value=None, mapping=None):
        self.hashes.setdefault(key, {}).update(mapping or {field: value})

    def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def expire(self, key, ttl):
        return True

    def set(self, key, value, nx=False, ex=None):
        if not nx:
            self.values[key] = value
            return True
        if key in self.locks:
            return False
        self.locks.add(key)
        return True

    def delete(self, key):
        self.locks.discard(key)
        self.values.pop(key, None)


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


def _start(monkeypatch, store, jobs, existing=None, model_name=None, overwrite_datasets=None, skip_existing=True, sampling_seed=None, pipeline='standard', **generation_options):
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
            sampling_seed=sampling_seed, pipeline=pipeline, **generation_options,
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
    second_job = _job('job-2')
    second_job.content_sha256 = 'another-pdf-content-hash'
    run_id, calls = _start(monkeypatch, store, [_job(), second_job], existing)
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
        return [{'id': 'q001', 'sampling_region': 1, 'sampling_region_count': 10}]
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


def _configure_gold(monkeypatch):
    monkeypatch.setattr(routes.settings, 'hcds_api_bearer_token', '')
    monkeypatch.setattr(routes.settings, 'openai_api_base_url', 'https://api.openai.com')
    monkeypatch.setattr(routes.settings, 'openai_api_bearer_token', 'sk-test')


def test_generation_config_lists_openai_as_dynamic_provider(monkeypatch):
    _configure_gold(monkeypatch)
    monkeypatch.setattr(routes.settings, 'dataset_llm_api_base_url', '')
    monkeypatch.setattr(routes.settings, 'dataset_llm_api_key', '')
    monkeypatch.setattr(routes, '_dataset_provider_models', lambda endpoint: ['gpt-test'])
    result = routes.dataset_generation_config()
    assert result['configured'] is True
    assert result['models'] == ['gpt-test']
    assert result['providers'] == [{
        'id': 'openai',
        'label': 'OpenAI – api.openai.com',
        'model_name': routes.settings.openai_model,
        'models': ['gpt-test'],
        'models_error': None,
    }]

def test_generation_gold_pipeline_skips_hub_model_check(monkeypatch):
    _configure_gold(monkeypatch)
    store = _Store()
    run_id, calls = _start(monkeypatch, store, [_job()], pipeline='openai-gold')
    monkeypatch.setattr(routes, 'list_dataset_models', lambda **kwargs: pytest.fail('Gold pipeline must not query the hub'))
    metadata = json.loads(store.hget(dataset_tasks.generation_key(run_id), 'metadata'))
    assert metadata['pipeline'] == 'openai-gold'
    assert 'Gold-Pipeline' in metadata['model_name']
    assert len(calls) == 1


def test_generation_direct_gold_pipeline_is_selectable(monkeypatch):
    _configure_gold(monkeypatch)
    store = _Store()
    run_id, calls = _start(monkeypatch, store, [_job()], pipeline='openai-gold-direct')
    metadata = json.loads(store.hget(dataset_tasks.generation_key(run_id), 'metadata'))
    assert metadata['pipeline'] == 'openai-gold-direct'
    assert 'sofort' in metadata['model_name']
    assert len(calls) == 1
    assert routes.gold_pipeline.config_from_settings(routes.settings, 'openai-gold-direct').mode == 'direct'
    assert routes.gold_pipeline.config_from_settings(routes.settings, 'openai-gold').mode == 'batch'


def test_generation_gold_pipeline_requires_openai_credentials(monkeypatch):
    monkeypatch.setattr(routes.settings, 'openai_api_bearer_token', '')
    with pytest.raises(HTTPException) as error:
        _start(monkeypatch, _Store(), [_job()], pipeline='openai-gold')
    assert error.value.status_code == 503


def test_gold_worker_reschedules_while_batch_runs_then_saves(monkeypatch):
    _configure_gold(monkeypatch)
    store = _Store()
    run_id, _ = _start(monkeypatch, store, [_job()], pipeline='openai-gold')
    monkeypatch.setattr(dataset_tasks, 'generation_store', lambda: store)
    monkeypatch.setattr(dataset_tasks, 'SessionLocal', lambda: _Database([_job()]))
    monkeypatch.setattr(dataset_tasks, 'generate_dataset_rows', lambda **kwargs: pytest.fail('Gold run must not use the hub'))
    scheduled = []
    monkeypatch.setattr(dataset_tasks.generate_document_dataset, 'apply_async', lambda **kwargs: scheduled.append(kwargs))
    calls = []

    def advance(state, job, config, on_step=None):
        calls.append(state['stage'])
        return {**state, 'stage': 'done' if len(calls) > 1 else 'validate', 'accepted': [], 'counters': {
            'generated': 2, 'validated': 2, 'escalated': 1, 'rejected': 1,
        }}

    monkeypatch.setattr(dataset_tasks.gold_pipeline, 'advance', advance)
    monkeypatch.setattr(dataset_tasks.gold_pipeline, 'progress', lambda state, job: {
        'phase': 'validating', 'region': 0, 'region_count': 1, 'passage_attempt': 1, 'passages_checked': 1,
        'passages_available': 1, 'questions_generated': 0, 'question_target': 10,
    })
    monkeypatch.setattr(dataset_tasks.gold_pipeline, 'final_rows', lambda state, job, config: [{'id': 'q001'}])
    saved = []
    monkeypatch.setattr(dataset_tasks, 'save_evaluation_dataset', lambda filename, rows: saved.append(filename) or {'path': f'docs/evaluation/{filename}'})

    dataset_tasks.generate_document_dataset.run(run_id, 'job-1')
    item = json.loads(store.hget(dataset_tasks.generation_key(run_id), 'job-1'))
    assert item['status'] == 'processing'
    assert item['progress']['phase'] == 'validating'
    assert len(scheduled) == 1 and scheduled[0]['args'] == [run_id, 'job-1']
    assert not saved

    dataset_tasks.generate_document_dataset.run(run_id, 'job-1')
    item = json.loads(store.hget(dataset_tasks.generation_key(run_id), 'job-1'))
    assert item['status'] == 'completed'
    assert item['validation_summary'] == {'candidates': 2, 'escalated': 1, 'rejected': 1}
    assert len(saved) == 1
    assert not store.values

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
        assert [
            {key: value for key, value in saved_row.items() if not key.startswith('dataset_')}
            for saved_row in result['rows']
        ] == [updated]
        assert sorted(path.name for path in root.iterdir()) == ['other.jsonl', 'target.jsonl']
        assert (root / 'other.jsonl').read_text() == other_content


def test_generation_preserves_explicit_zero_seed(monkeypatch):
    store = _Store()
    run_id, _ = _start(monkeypatch, store, [_job()], sampling_seed=0)
    result = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))
    assert result['sampling_seed'] == 0
    with pytest.raises(ValidationError):
        EvaluationDatasetGenerateRequest(markdown_paths=['job-1.md'], sampling_seed=-1)

def test_ragas_generation_uses_hcds_and_worker_path(monkeypatch):
    store = _Store()
    monkeypatch.setattr(routes.settings, 'hcds_api_bearer_token', 'test-virtual-key')
    monkeypatch.setattr(routes, '_dataset_provider_models', lambda endpoint: [endpoint.model_name])
    run_id, calls = _start(
        monkeypatch, store, [_job()], pipeline='ragas',
        ragas_query_mode='mixed', ragas_context_chars=800, ragas_passage_tries=5,
        ragas_persona_name='Fachprüfung',
        ragas_persona_role_description='Prüft dokumentierte Versicherungsleistungen.',
    )
    metadata = json.loads(store.hget(dataset_tasks.generation_key(run_id), 'metadata'))
    assert metadata['pipeline'] == 'ragas'
    assert metadata['provider'] == 'hcds'
    assert metadata['ragas_options'] == {
        'query_mode': 'mixed', 'context_chars': 800, 'passage_tries': 5,
        'persona_name': 'Fachprüfung',
        'persona_role_description': 'Prüft dokumentierte Versicherungsleistungen.',
    }
    assert calls == [{'args': [run_id, 'job-1'], 'queue': 'datasets'}]

    monkeypatch.setattr(dataset_tasks, 'generation_store', lambda: store)
    monkeypatch.setattr(dataset_tasks, 'SessionLocal', lambda: _Database([_job()]))
    monkeypatch.setattr(dataset_tasks, 'generate_dataset_rows', lambda **_: pytest.fail('Legacy generator should not run'))
    captured = []
    def generate(**kwargs):
        captured.append(kwargs)
        kwargs['audit_callback']({
            'type': 'prepared_document', 'text': 'Eine belegbare Passage.',
            'picture_text_blocks_removed': 0, 'sha256': 'cleaned-hash',
            'stage': 'after_picture_text_cleanup_before_section_filter',
        })
        kwargs['audit_callback']({'type': 'classification_input', 'prompt': 'Abschnittsliste'})
        kwargs['audit_callback']({
            'type': 'source_selection', 'passages_total': 2, 'passages_eligible': 1,
            'excluded_sections': [{'heading': 'Inhaltsverzeichnis', 'page': 1, 'excerpt': 'Kapitel ...'}],
        })
        kwargs['audit_callback']({
            'type': 'generation_input', 'passage_id': 'p002', 'page': 2,
            'anchor': 'Kapitel', 'region': 1, 'query_mode': 'facts',
            'question_quota': 10, 'context': 'Eine belegbare Passage.',
            'context_instruction': 'Nur dieser Abschnitt.',
        })
        kwargs['audit_callback']({
            'type': 'generation_result', 'region': 1, 'passage_id': 'p002',
            'status': 'generated', 'samples': 1, 'accepted': 1,
        })
        kwargs['audit_callback']({
            'type': 'region_result', 'region': 1, 'target': 10, 'generated': 1,
            'attempts': 1, 'passages_available': 1, 'short_passages_in_region': 0,
            'reason': 'no_usable_question',
        })
        return [{
            'id': 'q001', 'question': 'Frage?', 'gold_answer': 'Antwort',
            'evidence_quote': 'Eine belegbare Passage.', 'source_document': 'job-1.md',
            'sampling_region': 1, 'sampling_region_count': 1,
        }]
    monkeypatch.setattr(dataset_tasks, 'generate_ragas_dataset_rows', generate)
    validated = []
    def validate(**kwargs):
        validated.append(kwargs)
        kwargs['audit_callback']({
            'type': 'validation_input', 'id': 'q001', 'metric': 'faithfulness',
            'arguments': {'user_input': 'Frage?', 'response': 'Antwort',
                          'retrieved_contexts': ['Eine belegbare Passage.']},
        })
        row = {**kwargs['rows'][0], 'validation_status': 'accepted', 'validation_faithfulness': 1.0}
        kwargs['progress_callback']({
            'phase': 'validating', 'validation_done': 1, 'validation_total': 1,
            'validation_accepted': 1, 'validation_review': 0, 'validation_rejected': 0,
            'questions_generated': 1, 'question_target': 1,
        })
        return [row], {'candidates': 1, 'accepted': 1, 'review': 0, 'rejected': 0, 'details': []}
    monkeypatch.setattr(dataset_tasks, 'validate_ragas_dataset_rows', validate)
    saved_rows = []
    monkeypatch.setattr(dataset_tasks, 'save_evaluation_dataset', lambda filename, rows, **kwargs: saved_rows.extend(rows) or {'path': f'docs/evaluation/{filename}'})
    dataset_tasks.generate_document_dataset.run(run_id, 'job-1')
    result = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))
    assert result['pipeline'] == 'ragas'
    assert result['ragas_options'] == metadata['ragas_options']
    assert captured[0]['persona_name'] == 'Fachprüfung'
    assert captured[0]['persona_role_description'] == 'Prüft dokumentierte Versicherungsleistungen.'
    assert result['items'][0]['status'] == 'completed'
    assert result['items'][0]['validation_summary']['accepted'] == 1
    assert result['items'][0]['warning'] == '1 von 10 Fragen gespeichert. Bereich 1: keine neue belegbare Frage erhalten.'
    assert result['question_count'] == 10
    assert result['items'][0]['progress']['phase'] == 'validating'
    assert saved_rows[0]['validation_status'] == 'accepted'
    assert validated[0]['request_headers']['x-bf-vk'] == 'test-virtual-key'
    assert captured[0]['model_name'] == routes.settings.hcds_model
    assert captured[0]['request_headers']['x-bf-vk'] == 'test-virtual-key'
    assert callable(captured[0]['should_cancel'])
    assert captured[0]['query_mode'] == 'mixed'
    assert captured[0]['context_chars'] == 800
    assert captured[0]['passage_tries'] == 5
    assert result['items'][0]['source_audit_available'] is True
    assert 'generation_inputs' not in result['items'][0]
    audit = routes.dataset_generation_audit(run_id, 'job-1', SimpleNamespace(id='user-1'))
    assert audit['classification_inputs'] == ['Abschnittsliste']
    assert audit['prepared_document']['text'] == 'Eine belegbare Passage.'
    assert audit['passages_eligible'] == 1
    assert audit['generation_inputs'][0]['context'] == 'Eine belegbare Passage.'
    assert audit['generation_results'][0]['accepted'] == 1
    assert audit['region_results'][0]['reason'] == 'no_usable_question'
    assert audit['validation_inputs'][0]['arguments']['retrieved_contexts'] == ['Eine belegbare Passage.']
    assert audit['dataset_rows'][0]['question'] == 'Frage?'
    assert audit['dataset_rows'][0]['gold_answer'] == 'Antwort'
    assert audit['validation_candidates'][0]['gold_answer'] == 'Antwort'
    assert audit['validation_summary']['accepted'] == 1
    assert audit['audit_schema_version'] == 3
    downloaded = routes.download_dataset_generation_audit(run_id, 'job-1', SimpleNamespace(id='user-1'))
    log = json.loads(downloaded.body)
    assert downloaded.headers['content-disposition'].startswith('attachment;')
    assert log['audit']['generation_inputs'][0]['context'] == 'Eine belegbare Passage.'
    assert log['audit']['prepared_document']['stage'] == 'after_picture_text_cleanup_before_section_filter'
    assert log['result']['row_count'] == 1
    assert log['settings']['question_count'] == 10
    assert log['format'] == 'paddledoc-dataset-generation-log-v3'
    assert log['limitations'] == []
    assert 'owner_id' not in log['settings']
    assert 'test-virtual-key' not in downloaded.body.decode('utf-8')
    assert 'test-virtual-key' not in json.dumps(audit)
    legacy_audit = dict(audit)
    legacy_audit.pop('prepared_document')
    store.set(dataset_tasks.generation_audit_key(run_id, 'job-1'), json.dumps(legacy_audit))
    monkeypatch.setattr(routes, 'SessionLocal', lambda: _Database([_job()]))
    legacy_log = json.loads(routes.download_dataset_generation_audit(
        run_id, 'job-1', SimpleNamespace(id='user-1'),
    ).body)
    assert legacy_log['audit']['prepared_document']['text'] == 'Eine belegbare Passage.'
    assert legacy_log['audit']['prepared_document']['reconstructed_at_read_time'] is True
    legacy_audit['source_markdown_sha256'] = 'mismatched-source'
    store.set(dataset_tasks.generation_audit_key(run_id, 'job-1'), json.dumps(legacy_audit))
    changed_source_log = json.loads(routes.download_dataset_generation_audit(
        run_id, 'job-1', SimpleNamespace(id='user-1'),
    ).body)
    assert 'prepared_document' not in changed_source_log['audit']
    with pytest.raises(HTTPException) as wrong_owner:
        routes.dataset_generation_audit(run_id, 'job-1', SimpleNamespace(id='another-user'))
    assert wrong_owner.value.status_code == 404
    with pytest.raises(HTTPException) as wrong_download_owner:
        routes.download_dataset_generation_audit(run_id, 'job-1', SimpleNamespace(id='another-user'))
    assert wrong_download_owner.value.status_code == 404
    with pytest.raises(HTTPException) as wrong_job:
        routes.dataset_generation_audit(run_id, 'missing-job', SimpleNamespace(id='user-1'))
    assert wrong_job.value.status_code == 404


def test_ragas_validation_uses_separately_selected_provider_and_model(monkeypatch):
    store = _Store()
    monkeypatch.setattr(routes.settings, 'hcds_api_bearer_token', 'test-virtual-key')
    monkeypatch.setattr(routes.settings, 'openai_api_base_url', 'https://api.openai.com/v1')
    monkeypatch.setattr(routes.settings, 'openai_api_bearer_token', 'test-openai-key')
    monkeypatch.setattr(routes, '_dataset_provider_models', lambda endpoint: (
        [endpoint.model_name, 'gpt-validator'] if endpoint.provider == 'openai' else [endpoint.model_name]
    ))
    run_id, _ = _start(
        monkeypatch, store, [_job()], pipeline='ragas',
        validation_provider='openai', validation_model_name='gpt-validator',
    )
    metadata = json.loads(store.hget(dataset_tasks.generation_key(run_id), 'metadata'))
    assert metadata['provider'] == 'hcds'
    assert metadata['validation_provider'] == 'openai'
    assert metadata['validation_model_name'] == 'gpt-validator'

    monkeypatch.setattr(dataset_tasks, 'generation_store', lambda: store)
    monkeypatch.setattr(dataset_tasks, 'SessionLocal', lambda: _Database([_job()]))
    generated = []
    monkeypatch.setattr(dataset_tasks, 'generate_ragas_dataset_rows', lambda **kwargs: (
        generated.append(kwargs) or [{
            'id': 'q001', 'question': 'Frage?', 'gold_answer': 'Antwort',
            'evidence_quote': 'Eine belegbare Passage.', 'source_document': 'job-1.md',
        }]
    ))
    validated = []
    monkeypatch.setattr(dataset_tasks, 'validate_ragas_dataset_rows', lambda **kwargs: (
        validated.append(kwargs) or (
            [{**kwargs['rows'][0], 'validation_status': 'accepted'}],
            {'candidates': 1, 'accepted': 1, 'review': 0, 'rejected': 0, 'details': []},
        )
    ))
    monkeypatch.setattr(dataset_tasks, 'save_evaluation_dataset', lambda filename, rows, **kwargs: {
        'path': f'docs/evaluation/{filename}',
    })

    dataset_tasks.generate_document_dataset.run(run_id, 'job-1')
    assert generated[0]['model_name'] == routes.settings.hcds_model
    assert generated[0]['request_headers']['x-bf-vk'] == 'test-virtual-key'
    assert validated[0]['model_name'] == 'gpt-validator'
    assert validated[0]['api_key'] == 'test-openai-key'
    assert validated[0]['request_headers'] == {}
    result = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))
    assert result['validation_provider'] == 'openai'
    assert result['validation_model_name'] == 'gpt-validator'
    assert result['items'][0]['status'] == 'completed'


def test_ragas_partial_result_records_rejected_candidate_in_dataset(monkeypatch):
    store = _Store()
    monkeypatch.setattr(routes.settings, 'hcds_api_bearer_token', 'test-virtual-key')
    monkeypatch.setattr(routes, '_dataset_provider_models', lambda endpoint: [endpoint.model_name])
    run_id, _ = _start(monkeypatch, store, [_job()], pipeline='ragas')
    monkeypatch.setattr(dataset_tasks, 'generation_store', lambda: store)
    monkeypatch.setattr(dataset_tasks, 'SessionLocal', lambda: _Database([_job()]))
    candidates = [
        {'id': row_id, 'question': question, 'gold_answer': 'Antwort',
         'evidence_quote': 'Eine belegbare Passage.', 'source_document': 'job-1.md'}
        for row_id, question in (('q001', 'Frage 1?'), ('q002', 'Frage 2?'))
    ]
    monkeypatch.setattr(dataset_tasks, 'generate_ragas_dataset_rows', lambda **_: candidates)
    monkeypatch.setattr(dataset_tasks, 'validate_ragas_dataset_rows', lambda **_: (
        [{**candidates[0], 'validation_status': 'accepted'}],
        {'candidates': 2, 'accepted': 1, 'review': 0, 'rejected': 1, 'details': [
            {'id': 'q002', 'question': 'Frage 2?', 'status': 'rejected',
             'faithfulness': 0.11, 'answer_relevancy': 0.91,
             'reason': 'Faithfulness 0.11 unter Ablehnungsgrenze 0.50'},
        ]},
    ))
    saved_rows = []
    saved_options = []
    def save(filename, rows, **kwargs):
        saved_rows.extend(rows)
        saved_options.append(kwargs)
        return {'path': f'docs/evaluation/{filename}'}
    monkeypatch.setattr(dataset_tasks, 'save_evaluation_dataset', save)
    dataset_tasks.generate_document_dataset.run(run_id, 'job-1')
    outcome = saved_rows[0]['generation_outcome']
    assert outcome['target'] == 10
    assert outcome['generated'] == 2
    assert outcome['saved'] == 1
    assert outcome['rejected_candidates'] == [
        {'id': 'q002', 'reason': 'Faithfulness 0.11 unter Ablehnungsgrenze 0.50',
         'faithfulness': 0.11, 'answer_relevancy': 0.91},
    ]
    assert all(row['id'] != 'q002' for row in saved_rows)
    assert saved_options[0]['generation_run_id'] == run_id
    assert saved_options[0]['rejected_candidates'][0]['row'] == candidates[1]
    assert saved_options[0]['rejected_candidates'][0]['reason'] == 'Faithfulness 0.11 unter Ablehnungsgrenze 0.50'
    result = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))
    assert result['items'][0]['validation_summary']['details'][0]['question'] == 'Frage 2?'


def test_ragas_validation_rejects_unavailable_model(monkeypatch):
    store = _Store()
    monkeypatch.setattr(routes.settings, 'hcds_api_bearer_token', 'test-virtual-key')
    monkeypatch.setattr(routes, '_dataset_provider_models', lambda endpoint: [endpoint.model_name])
    with pytest.raises(HTTPException) as error:
        _start(
            monkeypatch, store, [_job()], pipeline='ragas',
            validation_provider='hcds', validation_model_name='unavailable-model',
        )
    assert error.value.status_code == 422
    assert not store.hashes


def test_ragas_validation_does_not_overwrite_when_all_candidates_are_rejected(monkeypatch):
    store = _Store()
    monkeypatch.setattr(routes.settings, 'hcds_api_bearer_token', 'test-virtual-key')
    monkeypatch.setattr(routes, '_dataset_provider_models', lambda endpoint: [endpoint.model_name])
    run_id, _ = _start(
        monkeypatch, store, [_job()], pipeline='ragas', skip_existing=False,
        existing=[{
            'path': 'docs/evaluation/old.jsonl', 'filename': 'old.jsonl',
            'source_documents': ['job-1.md'],
        }],
        overwrite_datasets={'job-1.md': 'docs/evaluation/old.jsonl'},
    )
    monkeypatch.setattr(dataset_tasks, 'generation_store', lambda: store)
    monkeypatch.setattr(dataset_tasks, 'SessionLocal', lambda: _Database([_job()]))
    monkeypatch.setattr(dataset_tasks, 'generate_ragas_dataset_rows', lambda **_: [{
        'id': 'q001', 'question': 'Frage?', 'gold_answer': 'Antwort',
        'evidence_quote': 'Eine belegbare Passage.', 'source_document': 'job-1.md',
    }])
    monkeypatch.setattr(dataset_tasks, 'validate_ragas_dataset_rows', lambda **_: (
        [], {'candidates': 1, 'accepted': 0, 'review': 0, 'rejected': 1, 'details': []},
    ))
    monkeypatch.setattr(
        dataset_tasks, 'save_evaluation_dataset',
        lambda *args: pytest.fail('A fully rejected run must not overwrite a dataset'),
    )

    dataset_tasks.generate_document_dataset.run(run_id, 'job-1')
    item = routes.dataset_generation_status(run_id, SimpleNamespace(id='user-1'))['items'][0]
    assert item['status'] == 'failed'
    assert item['validation_summary']['rejected'] == 1
    assert 'alle Fragen verworfen' in item['error']
