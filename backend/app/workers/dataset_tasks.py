import json
import logging
import time

from redis import Redis
from sqlalchemy.orm import defer

from app.core.config import settings
from app.database.session import SessionLocal
from app.models.models import Job, JobStatus
from app.services.dataset_assistant import generate_dataset_rows
from app.services.encourage_evaluation import save_evaluation_dataset
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)
GENERATION_TTL = 7 * 24 * 60 * 60


def generation_store() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def generation_key(run_id: str) -> str:
    return f'dataset-generation:{run_id}'


@celery_app.task(name='generate_document_dataset', acks_late=True, reject_on_worker_lost=True)
def generate_document_dataset(run_id: str, job_id: str) -> None:
    store = generation_store()
    key = generation_key(run_id)
    metadata_raw = store.hget(key, 'metadata')
    item_raw = store.hget(key, job_id)
    if not metadata_raw or not item_raw:
        return
    metadata = json.loads(metadata_raw)
    item = json.loads(item_raw)
    if item['status'] in {'completed', 'skipped', 'cancelled', 'failed'}:
        return
    lock = f'{key}:lock:{job_id}'
    if not store.set(lock, '1', nx=True, ex=settings.celery_task_time_limit_seconds + 60):
        raise generate_document_dataset.retry(countdown=60, max_retries=35)

    def update(**values: object) -> None:
        if values.get('status') == 'processing' and not item.get('started_at'):
            values['started_at'] = time.time()
        started_at = item.get('started_at') or values.get('started_at')
        if values.get('status') in {'completed', 'failed', 'cancelled'} and isinstance(started_at, (int, float)):
            finished_at = time.time()
            values.setdefault('finished_at', finished_at)
            values.setdefault('duration_seconds', round(finished_at - started_at, 1))
        item.update(values)
        store.hset(key, job_id, json.dumps(item))
        store.expire(key, GENERATION_TTL)

    try:
        if store.hget(key, 'cancelled') == '1':
            update(status='cancelled')
            return
        update(
            status='processing',
            progress={
                'phase': 'preparing', 'region': 0, 'region_count': 0,
                'passage_attempt': 0, 'passages_checked': 0, 'passages_available': 0,
                'questions_generated': 0, 'question_target': metadata['question_count'],
            },
        )
        with SessionLocal() as db:
            job = db.get(Job, job_id, options=[defer(Job.upload_content)])
            if job is None or job.status != JobStatus.FINISHED or not job.result_markdown:
                raise ValueError('Source document is no longer available as finished Markdown.')
            markdown = job.result_markdown
            original_filename = job.original_filename
            source_file_sha256 = getattr(job, 'content_sha256', None)
        rows = generate_dataset_rows(
            markdown=markdown,
            source_document=item['markdown_path'],
            source_file=original_filename,
            source_file_sha256=source_file_sha256,
            api_base_url=settings.dataset_llm_api_base_url,
            api_key=settings.dataset_llm_api_key,
            model_name=metadata['model_name'],
            question_count=metadata['question_count'],
            question_style=metadata['question_style'],
            focus=metadata['focus'],
            sampling_seed=metadata.get('sampling_seed'),
            progress_callback=lambda progress: update(progress=progress),
        )
        if store.hget(key, 'cancelled') == '1':
            update(status='cancelled')
            return
        is_overwrite = bool(item.get('target_dataset_filename'))
        filename = item.get('target_dataset_filename') or f'{job_id}_retrieval_{metadata["question_style"]}_{run_id}.jsonl'
        dataset = save_evaluation_dataset(filename, rows)
        warning = (
            f'Nur {len(rows)} von {metadata["question_count"]} gewünschten Fragen erzeugt. '
            'Trotz weiterer Textstellen ließen sich keine zusätzlichen unterschiedlichen Fragen '
            'mit einem wortgetreuen Quellenzitat belegen.'
        ) if len(rows) < metadata['question_count'] else None
        region_count = max((row.get('sampling_region_count', 0) for row in rows), default=0)
        covered_regions = len({row['sampling_region'] for row in rows if 'sampling_region' in row})
        if region_count and covered_regions < region_count:
            coverage_note = (
                f'Stichprobenabdeckung: {covered_regions} von {region_count} gleichmäßig verteilten '
                'Dokumentabschnitten; die Abschnitte sind keine einzelnen Seiten.'
            )
        else:
            coverage_note = None
        update(
            status='completed', dataset_path=dataset['path'], row_count=len(rows), warning=warning,
            coverage_note=coverage_note,
            dataset_action='overwritten' if is_overwrite else 'created', dataset_filename=filename,
        )
    except Exception as exc:
        logger.exception('Dataset generation failed for job %s', job_id)
        update(status='failed', error=str(exc))
    finally:
        store.delete(lock)