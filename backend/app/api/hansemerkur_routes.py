"""Admin API for the HanseMerkur discovery and RAG preparation workflow."""
from __future__ import annotations

import json
import mimetypes
import os
import random
import signal
import subprocess
import sys
import threading
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.datastructures import Headers

from app.api.deps import get_current_user
from app.api.routes import (
    DuplicateUploadError,
    _parse_tags,
    _sanitize_storage_path,
    _storage_folder,
    create_job_from_upload,
)
from app.database.session import get_db as get_database
from app.models.models import User
from app.services.paddle_service import effective_pipeline_profile_id, resolve_profile_selection
from app.services.security import enforce_rate_limit
from app.workers.tasks import process_job

router = APIRouter(prefix='/api/v1/hansemerkur', tags=['hansemerkur-admin'])

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_DIR = Path(os.environ.get('HANSEMERKUR_CRAWLER_DIR', _REPO_ROOT / 'scripts' / 'hansemerkur_crawler')).resolve()
_CRAWLER_SCRIPT = Path(os.environ.get('HANSEMERKUR_CRAWLER_SCRIPT', _SCRIPT_DIR / 'crawl.py')).resolve()
_DATA_DIR = Path(
    os.environ.get(
        'HANSEMERKUR_CRAWL_DIR',
        _REPO_ROOT.parent / '.docs' / 'hansemerkur-oeffentlich-2026-10-04',
    )
).expanduser().resolve()
_CATALOG = _DATA_DIR / 'catalog.json'
_RUNTIME_STATE = _DATA_DIR / 'crawler-ui-state.json'
_RUNTIME_LOG = _DATA_DIR / 'crawler-ui.log'
_ORIGINALS = _DATA_DIR / 'originale'
_ALLOWED_EXTENSIONS = {'.pdf', '.doc', '.docx', '.docm', '.dot', '.dotx', '.dotm'}
_PROCESSABLE_EXTENSIONS = {'.pdf', '.docx'}
_PROCESSING_PROFILE_ID = 'no_profile'
_MAX_CRAWL_PAGES = 100_000
_LOCK = threading.Lock()
_PROCESS: subprocess.Popen | None = None


class CrawlStartRequest(BaseModel):
    mode: Literal['full', 'discover-only', 'download-pending-only'] = 'full'
    include_robots_blocked: bool = False
    max_pages: int = Field(default=10_000, ge=1, le=_MAX_CRAWL_PAGES)
    max_bytes: int = Field(default=0, ge=0, le=1024 * 1024 * 1024)
    max_seconds: int = Field(default=0, ge=0, le=7 * 24 * 60 * 60)


class ProcessDocumentsRequest(BaseModel):
    file_ids: list[str] = Field(min_length=1, max_length=50)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def _recent_log_lines(limit: int = 40) -> list[str]:
    state = _read_json(_RUNTIME_STATE)
    start_offset = state.get('log_start_offset')
    if not isinstance(start_offset, int) or start_offset < 0:
        return []
    try:
        with _RUNTIME_LOG.open('rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start_offset = min(start_offset, size)
            handle.seek(max(start_offset, size - 24_000))
            content = handle.read().decode('utf-8', errors='replace')
    except OSError:
        return []
    return content.splitlines()[-limit:]


def _process_matches_crawler(pid: int) -> bool:
    try:
        command = Path(f'/proc/{pid}/cmdline').read_bytes().replace(b'\0', b' ').decode(errors='replace')
    except OSError:
        return False
    return str(_CRAWLER_SCRIPT) in command


def _runtime_state() -> dict:
    global _PROCESS
    state = _read_json(_RUNTIME_STATE)
    if state.get('status') not in {'running', 'stopping'}:
        return state

    pid = state.get('pid')
    if _PROCESS is not None:
        return_code = _PROCESS.poll()
        if return_code is None:
            return state
        final_status = 'stopped' if state.get('status') == 'stopping' else 'finished' if return_code == 0 else 'failed'
        state.update(status=final_status, return_code=return_code)
        state['finished_at'] = datetime.now(timezone.utc).isoformat()
        _PROCESS = None
        _write_json(_RUNTIME_STATE, state)
        return state

    if isinstance(pid, int) and _process_matches_crawler(pid):
        return state
    state.update(status='interrupted', detail='Backend restarted or crawler process is no longer running.')
    state['finished_at'] = datetime.now(timezone.utc).isoformat()
    _write_json(_RUNTIME_STATE, state)
    return state


def _catalog_status() -> dict:
    catalog = _read_json(_CATALOG)
    summary = catalog.get('summary', {}) if isinstance(catalog.get('summary'), dict) else {}
    documents = catalog.get('documents', {}) if isinstance(catalog.get('documents'), dict) else {}
    statuses = Counter(item.get('status', 'unknown') for item in documents.values() if isinstance(item, dict))
    local_paths = {
        item.get('local_path')
        for item in documents.values()
        if isinstance(item, dict) and item.get('local_path')
    }
    existing_paths = {
        path for path in local_paths
        if isinstance(path, str) and (_DATA_DIR / path).is_file()
        and Path(path).suffix.lower() in _ALLOWED_EXTENSIONS
    }
    return {
        'updated_at': catalog.get('updated_at_utc'),
        'pages_scanned': len(catalog.get('pages', {})) if isinstance(catalog.get('pages'), dict) else 0,
        'pending_pages': len(catalog.get('pending_pages', [])),
        'limit_pending_pages': len(catalog.get('limit_pending_pages', [])),
        'documents_found': len(documents),
        'document_statuses': dict(statuses),
        'unique_files': len(existing_paths),
        'robots_blocked': statuses.get('robots_blocked', 0),
        'outside_scope': statuses.get('outside_scope', 0),
        'sitemap_errors': len(catalog.get('sitemap_errors', [])),
        'time_budget_exhausted': bool(summary.get('time_budget_exhausted', False)),
        'discovery_limit_hits': int(summary.get('discovery_limit_hits', 0) or 0),
    }


def _category_for(filename: str, source_text: str = '') -> str:
    text = f'{filename} {source_text}'.casefold()
    rules = (
        ('Zahn', ('zahn', 'dental')),
        ('Tier', ('tier', 'hund', 'katze', 'pet')),
        ('Reise', ('reise', 'travel', 'urlaub')),
        ('Kranken', ('kranken', 'pkv', 'gesundheit', 'pflege')),
        ('Leben', ('leben', 'sterbegeld', 'risikoleben')),
        ('Unfall', ('unfall',)),
        ('Haftpflicht', ('haftpflicht',)),
        ('Unternehmen', ('geschaeftsbericht', 'geschäftsbericht', 'nachhaltigkeit', 'presse', 'newsroom')),
    )
    for category, terms in rules:
        if any(term in text for term in terms):
            return category
    return 'Weitere'


def _document_items() -> list[dict]:
    catalog = _read_json(_CATALOG)
    records = catalog.get('documents', {})
    if not isinstance(records, dict):
        return []
    originals_root = _ORIGINALS.resolve()
    by_path: dict[str, dict] = {}
    for url, record in records.items():
        if not isinstance(record, dict):
            continue
        local_path = record.get('local_path')
        if not isinstance(local_path, str) or Path(local_path).suffix.lower() not in _ALLOWED_EXTENSIONS:
            continue
        file_path = (_DATA_DIR / local_path).resolve()
        if not file_path.is_relative_to(originals_root) or not file_path.is_file():
            continue
        item = by_path.setdefault(local_path, {
            'id': local_path,
            'filename': file_path.name,
            'size_bytes': record.get('size_bytes', file_path.stat().st_size),
            'processable': file_path.suffix.lower() in _PROCESSABLE_EXTENSIONS,
            'category': _category_for(file_path.name),
            'status': record.get('status', 'downloaded'),
            'sha256': record.get('sha256'),
            'urls': [],
            'sources': [],
        })
        if url not in item['urls']:
            item['urls'].append(url)
        for source in record.get('sources', []):
            if isinstance(source, dict) and source not in item['sources']:
                item['sources'].append(source)
        item['category'] = _category_for(file_path.name, ' '.join(
            str(source.get('link_text', '')) for source in item['sources']
        ))
    return sorted(by_path.values(), key=lambda item: (item['category'], item['filename'].casefold()))


@router.get('/crawler/status')
def crawler_status() -> dict:
    state = _runtime_state()
    return {
        'run': state,
        'catalog': _catalog_status(),
        'recent_log': _recent_log_lines(),
        'configured': _CRAWLER_SCRIPT.is_file(),
    }


@router.post('/crawler/start')
def start_crawler(
    payload: CrawlStartRequest,
    request: Request,
) -> dict:
    global _PROCESS
    enforce_rate_limit(request)
    if not _CRAWLER_SCRIPT.is_file():
        raise HTTPException(status_code=503, detail=f'Crawler script not found: {_CRAWLER_SCRIPT}')
    if payload.mode == 'discover-only' and payload.include_robots_blocked:
        raise HTTPException(status_code=422, detail='Robots-blocked download requires a download-capable mode')

    with _LOCK:
        current = _runtime_state()
        if current.get('status') in {'running', 'stopping'}:
            raise HTTPException(status_code=409, detail='A HanseMerkur crawler run is already active')

        command = [sys.executable, str(_CRAWLER_SCRIPT)]
        if payload.mode == 'discover-only':
            command.append('--discover-only')
        elif payload.mode == 'download-pending-only':
            command.append('--download-pending-only')
        command.extend(['--max-pages', str(payload.max_pages), '--max-bytes', str(payload.max_bytes)])
        if payload.max_seconds:
            command.extend(['--max-seconds', str(payload.max_seconds)])
        if payload.include_robots_blocked:
            command.append('--include-robots-blocked')

        env = os.environ.copy()
        env['HANSEMERKUR_CRAWL_DIR'] = str(_DATA_DIR)
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        log_start_offset = _RUNTIME_LOG.stat().st_size if _RUNTIME_LOG.exists() else 0
        _write_json(_RUNTIME_STATE, {
            'status': 'running', 'mode': payload.mode,
            'include_robots_blocked': payload.include_robots_blocked,
            'started_at': datetime.now(timezone.utc).isoformat(),
            'log_start_offset': log_start_offset,
            'pid': None, 'command_options': command[2:],
        })
        try:
            with _RUNTIME_LOG.open('ab', buffering=0) as log_file:
                _PROCESS = subprocess.Popen(
                    command,
                    cwd=str(_SCRIPT_DIR),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            _write_json(_RUNTIME_STATE, {
                'status': 'failed', 'detail': str(exc),
                'finished_at': datetime.now(timezone.utc).isoformat(),
            })
            raise HTTPException(status_code=500, detail=f'Could not start crawler: {exc}') from exc

        state = _read_json(_RUNTIME_STATE)
        state['pid'] = _PROCESS.pid
        _write_json(_RUNTIME_STATE, state)
    return {'run': _runtime_state(), 'catalog': _catalog_status()}


@router.post('/crawler/stop')
def stop_crawler(request: Request) -> dict:
    enforce_rate_limit(request)
    with _LOCK:
        state = _runtime_state()
        if state.get('status') not in {'running', 'stopping'}:
            raise HTTPException(status_code=409, detail='No HanseMerkur crawler run is active')
        pid = state.get('pid')
        if not isinstance(pid, int) or not _process_matches_crawler(pid):
            raise HTTPException(status_code=409, detail='Crawler process is no longer running')
        try:
            os.killpg(pid, signal.SIGINT)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f'Could not stop crawler: {exc}') from exc
        state['status'] = 'stopping'
        _write_json(_RUNTIME_STATE, state)
    return {'run': state, 'catalog': _catalog_status()}


@router.get('/documents')
def list_documents(
    search: str = Query(default='', max_length=200),
    category: str | None = Query(default=None, max_length=80),
    processable_only: bool = Query(default=False),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    sample: int = Query(default=0, ge=0, le=200),
) -> dict:
    items = _document_items()
    if processable_only:
        items = [item for item in items if item['processable']]
    query = search.strip().casefold()
    if category:
        items = [item for item in items if item['category'] == category]
    if query:
        items = [item for item in items if query in ' '.join([
            item['filename'], item['category'], *item['urls'],
            *(str(source.get('link_text', '')) for source in item['sources']),
        ]).casefold()]
    total = len(items)
    if sample:
        items = random.SystemRandom().sample(items, min(sample, len(items)))
    else:
        items = items[offset:offset + limit]
    categories = Counter(item['category'] for item in _document_items())
    return {
        'items': items,
        'total': total,
        'categories': [{'name': name, 'count': count} for name, count in sorted(categories.items())],
        'offset': offset,
        'limit': limit,
        'sampled': bool(sample),
    }


@router.post('/documents/process')
def process_documents(
    payload: ProcessDocumentsRequest,
    request: Request,
    db: Session = Depends(get_database),
    user: User = Depends(get_current_user),
) -> dict:
    enforce_rate_limit(request)
    profile_settings = resolve_profile_selection(db, _PROCESSING_PROFILE_ID)
    dispatch_profile = effective_pipeline_profile_id(_PROCESSING_PROFILE_ID)
    folder = _sanitize_storage_path('HanseMerkur')

    items = {item['id']: item for item in _document_items()}
    created = []
    duplicates = []
    failures = []

    for file_id in dict.fromkeys(payload.file_ids):
        item = items.get(file_id)
        if item is None:
            failures.append({'file_id': file_id, 'error': 'File is missing from the current downloaded-file catalog'})
            continue
        source_path = (_DATA_DIR / file_id).resolve()
        if not source_path.is_relative_to(_ORIGINALS.resolve()) or source_path.suffix.lower() not in _ALLOWED_EXTENSIONS:
            failures.append({'file_id': file_id, 'error': 'Unsupported file path or type'})
            continue
        if source_path.suffix.lower() not in _PROCESSABLE_EXTENSIONS:
            failures.append({'file_id': file_id, 'error': 'The current Markdown pipeline supports PDF and DOCX only'})
            continue
        job_id = str(uuid.uuid4())
        category = item['category']
        target_subfolder = _sanitize_storage_path(category)
        storage_folder = _storage_folder(job_id, folder, target_subfolder)
        mime_type = mimetypes.guess_type(source_path.name)[0] or 'application/octet-stream'
        extra_settings = {
            **profile_settings,
            'source_kind': 'hansemerkur_crawler',
            'source_path': file_id,
            'source_urls': item['urls'],
            'source_category': category,
        }
        try:
            with source_path.open('rb') as source_file:
                upload = UploadFile(
                    filename=source_path.name,
                    file=source_file,
                    headers=Headers({'content-type': mime_type}),
                )
                job = create_job_from_upload(
                    db,
                    file=upload,
                    user=user,
                    storage_folder=storage_folder,
                    mode='single',
                    email='',
                    department=None,
                    profile_id=_PROCESSING_PROFILE_ID,
                    folder=folder or None,
                    subfolder=target_subfolder or None,
                    tags=_parse_tags(f'HanseMerkur,{category}'),
                    extra_settings=extra_settings,
                )
            db.commit()
        except DuplicateUploadError as exc:
            db.rollback()
            duplicates.append({'file_id': file_id, 'job_id': exc.predecessor.id})
            continue
        except Exception as exc:
            db.rollback()
            failures.append({'file_id': file_id, 'error': str(exc)})
            continue

        try:
            process_job.delay(job.id, dispatch_profile, 'single', '', None)
        except Exception as exc:
            failures.append({'file_id': file_id, 'job_id': job.id, 'error': f'Job saved but queue dispatch failed: {exc}'})
            continue
        created.append({'file_id': file_id, 'job_id': job.id, 'filename': item['filename'], 'category': category})

    return {
        'created': created,
        'duplicates': duplicates,
        'failures': failures,
        'collection_id': None,
        'mode': 'single',
    }