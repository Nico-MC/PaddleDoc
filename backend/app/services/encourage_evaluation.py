from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.encourage_bridge import (
    create_llm_runner,
    get_pipeline_metadata,
    get_pipeline_rag,
    load_markdown_chunks,
    prepare_encourage_runtime,
    retrieve_from_pipeline,
)
from app.services.encourage_mlflow import log_evaluation_run


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _evaluation_root() -> Path:
    # Check /app/docs first (Docker mount), then relative to repo root
    docker_path = Path('/app/docs/evaluation')
    if docker_path.exists():
        return docker_path
    return _repo_root() / 'docs' / 'evaluation'


def _source_documents_root() -> Path:
    docker_path = Path('/app/docs')
    if docker_path.exists():
        return docker_path

    checkout_path = _repo_root().parent.parent / '.docs'
    if checkout_path.exists():
        return checkout_path

    return _repo_root() / 'docs'


def _dataset_public_path(path: Path) -> str:
    return f'docs/evaluation/{path.name}'


def _resolve_dataset_path(dataset_path: str) -> Path:
    normalized = dataset_path.strip().replace('\\', '/').lstrip('/')
    prefix = 'docs/evaluation/'
    filename = normalized[len(prefix) :] if normalized.startswith(prefix) else normalized
    if not filename or Path(filename).name != filename or not filename.lower().endswith('.jsonl'):
        raise ValueError(f'Invalid evaluation dataset path: {dataset_path}')

    root = _evaluation_root().resolve()
    candidate = (root / filename).resolve()
    if candidate.parent != root:
        raise ValueError(f'Invalid evaluation dataset path: {dataset_path}')
    return candidate


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        rows.append(json.loads(cleaned))
    return rows


def list_evaluation_datasets() -> list[dict[str, Any]]:
    root = _evaluation_root().resolve()
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
        return []

    items: list[dict[str, Any]] = []
    files = list(root.glob('*.jsonl'))

    for path in sorted(files):
        try:
            rows = _load_jsonl(path)
            source_documents = sorted(
                {
                    str(row.get('source_document', '')).strip()
                    for row in rows
                    if str(row.get('source_document', '')).strip()
                }
            )
            source_files = sorted(
                {
                    str(row.get('source_file', '')).strip()
                    for row in rows
                    if str(row.get('source_file', '')).strip()
                }
            )
            items.append(
                {
                    'path': _dataset_public_path(path),
                    'filename': path.name,
                    'row_count': len(rows),
                    'source_documents': source_documents,
                    'source_files': source_files,
                    'size_bytes': path.stat().st_size,
                    'updated_at': datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
                }
            )
        except Exception:
            continue
    return items


def get_evaluation_dataset_details(dataset_path: str) -> dict[str, Any]:
    dataset_file = _resolve_dataset_path(dataset_path)
    if not dataset_file.exists() or not dataset_file.is_file() or dataset_file.suffix.lower() != '.jsonl':
        raise FileNotFoundError(f'Dataset file not found: {dataset_file}')

    rows = _load_jsonl(dataset_file)
    source_documents = sorted(
        {
            str(row.get('source_document', '')).strip()
            for row in rows
            if str(row.get('source_document', '')).strip()
        }
    )
    source_files = sorted(
        {
            str(row.get('source_file', '')).strip()
            for row in rows
            if str(row.get('source_file', '')).strip()
        }
    )

    return {
        'path': _dataset_public_path(dataset_file),
        'filename': dataset_file.name,
        'row_count': len(rows),
        'source_documents': source_documents,
        'source_files': source_files,
        'size_bytes': dataset_file.stat().st_size,
        'updated_at': datetime.fromtimestamp(dataset_file.stat().st_mtime, tz=timezone.utc),
        'rows': rows,
    }


def list_evaluation_source_documents() -> list[dict[str, Any]]:
    root = _source_documents_root().resolve()
    if not root.exists():
        return []

    supported_suffixes = {'.doc', '.docx', '.docm'}
    items: list[dict[str, Any]] = []
    for path in sorted(root.rglob('*')):
        if (
            not path.is_file()
            or path.suffix.lower() not in supported_suffixes
            or path.name.startswith('._')
            or '__MACOSX' in path.parts
        ):
            continue
        items.append(
            {
                'path': str(path.relative_to(root)),
                'filename': path.name,
                'extension': path.suffix.lower(),
                'size_bytes': path.stat().st_size,
                'updated_at': datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
            }
        )
    return items


def save_evaluation_dataset(filename: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    dataset_file = _resolve_dataset_path(filename)
    if not rows:
        raise ValueError('An evaluation dataset must contain at least one row.')

    normalized_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    required_fields = ('id', 'question', 'gold_answer', 'source_document')
    for index, row in enumerate(rows, start=1):
        normalized = dict(row)
        for field in required_fields:
            value = str(normalized.get(field, '')).strip()
            if not value:
                raise ValueError(f'Row {index}: {field} is required.')
            normalized[field] = value

        row_id = normalized['id']
        if row_id in seen_ids:
            raise ValueError(f'Row {index}: duplicate id {row_id!r}.')
        seen_ids.add(row_id)

        for field in ('evidence_anchor', 'evidence_quote', 'notes', 'source_file'):
            if field in normalized:
                normalized[field] = str(normalized[field]).strip()
        normalized_rows.append(normalized)

    dataset_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = dataset_file.with_suffix('.jsonl.tmp')
    payload = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in normalized_rows)
    temporary_file.write_text(payload, encoding='utf-8')
    temporary_file.replace(dataset_file)
    return get_evaluation_dataset_details(_dataset_public_path(dataset_file))


def _normalize_text(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip().lower()


def _tokenize(text: str) -> set[str]:
    return set(re.findall(r'\w+', _normalize_text(text)))


def _average_context_token_length(responses: list[Any]) -> float:
    lengths: list[int] = []
    for response in responses:
        documents = getattr(getattr(response, 'context', None), 'documents', [])
        total_tokens = 0
        for document in documents:
            total_tokens += len(re.findall(r'\w+', str(getattr(document, 'content', ''))))
        lengths.append(total_tokens)
    if not lengths:
        return 0.0
    return float(sum(lengths) / len(lengths))


def _compute_context_length(
    response_wrapper: Any,
    responses: list[Any],
    context_length_metric_cls: Any,
) -> tuple[float, str]:
    try:
        context_length = context_length_metric_cls()(response_wrapper)
        return float(context_length.score), 'encourage_context_length'
    except LookupError:
        # Encourage relies on nltk.word_tokenize, which may need punkt resources
        # at runtime depending on the container state.
        try:
            import nltk

            for resource_name in ('punkt_tab', 'punkt'):
                resource_path = f'tokenizers/{resource_name}'
                try:
                    nltk.data.find(resource_path)
                except LookupError:
                    nltk.download(resource_name, quiet=True)

            context_length = context_length_metric_cls()(response_wrapper)
            return float(context_length.score), 'encourage_context_length'
        except Exception:
            return _average_context_token_length(responses), 'fallback_regex_token_count'


def _content_preview(text: str, *, max_chars: int = 260) -> str:
    normalized = re.sub(r'\s+', ' ', text).strip()
    if len(normalized) <= max_chars:
        return normalized
    return f'{normalized[: max_chars - 3].rstrip()}...'


def _pick_reference_documents(chunks: list[Any], evidence_quote: str) -> tuple[list[Any], dict[str, Any]]:
    normalized_quote = _normalize_text(evidence_quote)
    exact_matches = [
        chunk
        for chunk in chunks
        if normalized_quote and normalized_quote in _normalize_text(str(getattr(chunk, 'content', '')))
    ]
    if exact_matches:
        return exact_matches, {
            'strategy': 'exact_quote_substring',
            'match_score': 1.0,
            'quote_token_count': len(_tokenize(evidence_quote)),
        }

    quote_tokens = _tokenize(evidence_quote)
    if not quote_tokens:
        if chunks:
            return [chunks[0]], {
                'strategy': 'fallback_first_chunk',
                'match_score': 0.0,
                'quote_token_count': 0,
            }
        return [], {
            'strategy': 'no_reference_available',
            'match_score': 0.0,
            'quote_token_count': 0,
        }

    best_score = 0.0
    best_chunks: list[Any] = []
    for chunk in chunks:
        chunk_tokens = _tokenize(str(getattr(chunk, 'content', '')))
        if not chunk_tokens:
            continue
        score = len(chunk_tokens & quote_tokens) / len(quote_tokens)
        if score > best_score:
            best_score = score
            best_chunks = [chunk]
        elif score == best_score and score > 0:
            best_chunks.append(chunk)

    if best_chunks:
        return best_chunks, {
            'strategy': 'token_overlap',
            'match_score': best_score,
            'quote_token_count': len(quote_tokens),
        }

    if chunks:
        return [chunks[0]], {
            'strategy': 'fallback_first_chunk',
            'match_score': 0.0,
            'quote_token_count': len(quote_tokens),
        }

    return [], {
        'strategy': 'no_reference_available',
        'match_score': 0.0,
        'quote_token_count': len(quote_tokens),
    }


def _resolve_dataset_rows(rows: list[dict[str, Any]], *, markdown_path: str) -> list[dict[str, Any]]:
    root = _repo_root().resolve()
    selected_markdown = Path(markdown_path).expanduser().resolve()
    matched_rows: list[dict[str, Any]] = []
    for row in rows:
        source_document = str(row.get('source_document', '')).strip()
        if not source_document:
            continue
        candidate = Path(source_document)
        if not candidate.is_absolute():
            candidate = (root / candidate).resolve()
        else:
            candidate = candidate.resolve()
        relative_source = source_document.replace('\\', '/').lstrip('./')
        selected_path_text = selected_markdown.as_posix()
        if candidate == selected_markdown or (
            not Path(source_document).is_absolute()
            and selected_path_text.endswith(f'/{relative_source}')
        ):
            matched_rows.append(row)
    return matched_rows


def _normalize_evaluation_mode(value: str | None) -> str:
    normalized = (value or '').strip().lower()
    if normalized in {'', 'standard'}:
        return 'standard'
    if normalized in {'advanced', 'llm', 'enhanced'}:
        return 'advanced'
    raise ValueError("Unsupported evaluation mode. Supported modes: 'standard', 'advanced'.")


def _compute_advanced_metrics(
    *,
    mode: str,
    response_wrapper: Any,
    openai_api_base_url: str,
    openai_api_key: str,
    model_name: str | None,
) -> tuple[dict[str, float], str, list[str]]:
    if mode != 'advanced':
        return {}, 'disabled', []

    if not openai_api_base_url.strip() or not openai_api_key.strip():
        return (
            {},
            'skipped_missing_credentials',
            [
                'Advanced evaluation requested, but OPENAI credentials are missing. '
                'Set OPENAI_API_BASE_URL and OPENAI_API_BEARER_TOKEN.'
            ],
        )

    try:
        runner = create_llm_runner(
            api_base_url=openai_api_base_url,
            api_key=openai_api_key,
            model_name=model_name,
            max_tokens=256,
            temperature=0.0,
            max_workers=2,
            batch_size=4,
        )
        from encourage.metrics.context_precision import (  # type: ignore[reportMissingImports]
            ContextPrecision,
        )
        from encourage.metrics.context_recall import (  # type: ignore[reportMissingImports]
            ContextRecall,
        )

        context_precision = ContextPrecision(runner)(response_wrapper)
        context_recall = ContextRecall(runner)(response_wrapper)

        return (
            {
                'context_precision': float(context_precision.score),
                'context_recall': float(context_recall.score),
            },
            'computed',
            [],
        )
    except Exception as exc:
        return (
            {},
            'failed',
            [f'Advanced evaluation failed: {exc}'],
        )


def run_encourage_evaluation(
    *,
    pipeline_id: str,
    dataset_path: str,
    recall_k: int,
    collection_name: str | None = None,
    markdown_path: str | None = None,
    top_k: int | None = None,
    chunk_max_chars: int | None = None,
    chunk_overlap_chars: int | None = None,
    evaluation_mode: str = 'standard',
    openai_api_base_url: str = '',
    openai_api_key: str = '',
    model_name: str | None = None,
) -> dict[str, Any]:
    resolved_mode = _normalize_evaluation_mode(evaluation_mode)
    rag_pipeline = get_pipeline_rag(pipeline_id)
    pipeline_metadata = get_pipeline_metadata(pipeline_id)
    if pipeline_metadata is None:
        if not collection_name or not markdown_path:
            raise KeyError(f'Encourage pipeline not found: {pipeline_id}')
        pipeline_metadata = {
            'pipeline_id': pipeline_id,
            'collection_name': collection_name,
            'rag_method': 'BaseRAG',
            'top_k': max(1, int(top_k or 1)),
            'document_count': 0,
            'chunk_max_chars': chunk_max_chars or 0,
            'chunk_overlap_chars': chunk_overlap_chars or 0,
            'source_md_path': markdown_path,
            'source_md_filename': Path(markdown_path).name,
        }

    dataset_file = _resolve_dataset_path(dataset_path)
    if not dataset_file.exists() or not dataset_file.is_file() or dataset_file.suffix.lower() != '.jsonl':
        raise FileNotFoundError(f'Dataset file not found: {dataset_file}')

    dataset_rows = _load_jsonl(dataset_file)
    source_markdown_path = str(pipeline_metadata.get('source_md_path', ''))
    filtered_rows = _resolve_dataset_rows(dataset_rows, markdown_path=source_markdown_path)
    if not filtered_rows:
        raise ValueError(
            'No evaluation rows matched the selected markdown file. '
            'Make sure source_document in the dataset points to the same markdown.'
        )

    prepare_encourage_runtime()

    from encourage.llm import Response, ResponseWrapper  # type: ignore[reportMissingImports]
    from encourage.metrics.classic import (  # type: ignore[reportMissingImports]
        ContextLength,
        HitRateAtK,
        MeanAveragePrecision,
        MeanReciprocalRank,
        NDCG,
        RecallAtK,
    )
    from encourage.prompts import Context, MetaData  # type: ignore[reportMissingImports]

    if rag_pipeline is not None:
        reference_chunk_documents = rag_pipeline.context_collection
    else:
        source_markdown_file = Path(source_markdown_path)
        if not source_markdown_file.exists():
            raise FileNotFoundError(f'Markdown file not found for evaluation: {source_markdown_file}')
        reference_chunk_documents = load_markdown_chunks(
            source_markdown_file,
            chunk_max_chars=chunk_max_chars,
            chunk_overlap_chars=chunk_overlap_chars,
        )

    responses: list[Response] = []
    per_question_results: list[dict[str, Any]] = []
    question_hit_count = 0
    first_hit_rank_breakdown: dict[str, int] = {}

    for row in filtered_rows:
        query = str(row.get('question', '')).strip()
        if not query:
            continue
        evidence_quote = str(row.get('evidence_quote', '')).strip()
        gold_answer = str(row.get('gold_answer', '')).strip()

        retrieved_docs_payload = retrieve_from_pipeline(
            pipeline_id,
            query,
            collection_name=str(pipeline_metadata.get('collection_name', '') or ''),
            top_k=int(pipeline_metadata.get('top_k', 0) or 1),
        )
        retrieved_docs = retrieved_docs_payload['results']
        reference_docs, reference_selection = _pick_reference_documents(
            reference_chunk_documents,
            evidence_quote,
        )

        response = Response(
            request_id=str(row.get('id', query)),
            prompt_id=str(row.get('id', query)),
            sys_prompt='',
            user_prompt=query,
            response='',
            context=Context.from_documents(
                [
                    {
                        'content': document['content'],
                        'score': document['score'],
                        'distance': document['distance'],
                        'id': document['id'],
                        'meta_data': document['meta_data'],
                    }
                    for document in retrieved_docs
                ]
            ),
            meta_data=MetaData(
                tags={
                    'reference_answer': gold_answer,
                    'reference_document': reference_docs,
                    'question': query,
                    'source_document': source_markdown_path,
                }
            ),
        )
        responses.append(response)

        retrieved_document_ids = [str(document['id']) for document in retrieved_docs]
        reference_document_ids = [str(doc.id) for doc in reference_docs]
        reference_document_id_set = set(reference_document_ids)
        first_hit_rank: int | None = None
        for index, document in enumerate(retrieved_docs, start=1):
            if str(document['id']) in reference_document_ids:
                first_hit_rank = index
                break

        if first_hit_rank is not None:
            question_hit_count += 1
            breakdown_key = str(first_hit_rank)
        else:
            breakdown_key = 'miss'
        first_hit_rank_breakdown[breakdown_key] = first_hit_rank_breakdown.get(breakdown_key, 0) + 1

        per_question_results.append(
            {
                'id': str(row.get('id', query)),
                'question': query,
                'gold_answer': gold_answer,
                'evidence_quote': evidence_quote,
                'source_document': str(row.get('source_document', '')).strip(),
                'reference_selection': reference_selection,
                'retrieved_document_ids': retrieved_document_ids,
                'reference_document_ids': reference_document_ids,
                'first_hit_rank': first_hit_rank,
                'has_hit': first_hit_rank is not None,
                'retrieved_documents': [
                    {
                        'rank': index,
                        'id': str(document['id']),
                        'score': document['score'],
                        'distance': document['distance'],
                        'content_preview': _content_preview(str(document.get('content', ''))),
                        'is_reference_match': str(document['id']) in reference_document_id_set,
                        'meta_data': document.get('meta_data', {}),
                    }
                    for index, document in enumerate(retrieved_docs, start=1)
                ],
                'reference_documents': [
                    {
                        'id': str(doc.id),
                        'content_preview': _content_preview(str(getattr(doc, 'content', ''))),
                    }
                    for doc in reference_docs
                ],
            }
        )

    if not responses:
        raise ValueError('No valid evaluation questions found in the selected dataset.')

    response_wrapper = ResponseWrapper(responses)
    mrr = MeanReciprocalRank()(response_wrapper)
    map_score = MeanAveragePrecision()(response_wrapper)
    ndcg_score = NDCG()(response_wrapper)
    context_length_value, context_length_metric_source = _compute_context_length(
        response_wrapper,
        responses,
        ContextLength,
    )
    recall = RecallAtK(recall_k)(response_wrapper)
    hit_rate = HitRateAtK(recall_k)(response_wrapper)

    pipeline_top_k = max(1, int(pipeline_metadata.get('top_k', 0) or 1))
    recall_at_1 = RecallAtK(1)(response_wrapper)
    hit_rate_at_1 = HitRateAtK(1)(response_wrapper)
    recall_at_top_k = RecallAtK(pipeline_top_k)(response_wrapper)
    hit_rate_at_top_k = HitRateAtK(pipeline_top_k)(response_wrapper)

    retrieval_metrics = {
        'mrr': float(mrr.score),
        'mean_average_precision': float(map_score.score),
        'ndcg': float(ndcg_score.score),
        'context_length': context_length_value,
        f'recall_at_{recall_k}': float(recall.score),
        f'hit_rate_at_{recall_k}': float(hit_rate.score),
        'recall_at_1': float(recall_at_1.score),
        'hit_rate_at_1': float(hit_rate_at_1.score),
        f'recall_at_{pipeline_top_k}': float(recall_at_top_k.score),
        f'hit_rate_at_{pipeline_top_k}': float(hit_rate_at_top_k.score),
    }

    advanced_metrics, advanced_status, advanced_warnings = _compute_advanced_metrics(
        mode=resolved_mode,
        response_wrapper=response_wrapper,
        openai_api_base_url=openai_api_base_url,
        openai_api_key=openai_api_key,
        model_name=model_name,
    )

    evaluated_question_count = len(responses)
    evaluation_summary = {
        'question_hit_count': question_hit_count,
        'question_miss_count': max(evaluated_question_count - question_hit_count, 0),
        'first_hit_rank_breakdown': first_hit_rank_breakdown,
        'questions_without_hit': [item['id'] for item in per_question_results if not item['has_hit']],
        'context_length_metric_source': context_length_metric_source,
    }

    mlflow_run = log_evaluation_run(
        metadata=pipeline_metadata,
        dataset_path=str(dataset_file.relative_to(_repo_root().resolve())),
        dataset_filename=dataset_file.name,
        question_count=len(dataset_rows),
        evaluated_question_count=evaluated_question_count,
        recall_k=recall_k,
        evaluation_mode=resolved_mode,
        mrr=float(mrr.score),
        recall_at_k=float(recall.score),
        hit_rate_at_k=float(hit_rate.score),
        retrieval_metrics=retrieval_metrics,
        advanced_metrics=advanced_metrics,
        advanced_status=advanced_status,
        warnings=advanced_warnings,
        dataset_rows=filtered_rows,
        per_question_results=per_question_results,
        evaluation_summary=evaluation_summary,
    )

    return {
        'pipeline_id': pipeline_id,
        'collection_name': str(pipeline_metadata.get('collection_name', '')),
        'markdown_path': source_markdown_path,
        'dataset_path': str(dataset_file.relative_to(_repo_root().resolve())),
        'dataset_filename': dataset_file.name,
        'question_count': len(dataset_rows),
        'evaluated_question_count': evaluated_question_count,
        'top_k': int(pipeline_metadata.get('top_k', 0) or 0),
        'recall_k': recall_k,
        'evaluation_mode': resolved_mode,
        'mrr': float(mrr.score),
        'mean_average_precision': float(map_score.score),
        'ndcg': float(ndcg_score.score),
        'context_length': context_length_value,
        'context_length_metric_source': context_length_metric_source,
        'recall_at_k': float(recall.score),
        'hit_rate_at_k': float(hit_rate.score),
        'retrieval_metrics': retrieval_metrics,
        'advanced_metrics': advanced_metrics,
        'advanced_status': advanced_status,
        'warnings': advanced_warnings,
        'mlflow_experiment_id': mlflow_run.get('experiment_id'),
        'mlflow_run_id': mlflow_run.get('run_id'),
        'evaluation_summary': evaluation_summary,
        'per_question_results': per_question_results,
    }
