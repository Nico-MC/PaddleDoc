from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.encourage_bridge import (
    get_pipeline_metadata,
    get_pipeline_rag,
    load_markdown_chunks,
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


def _resolve_repo_path(relative_path: str) -> Path:
    root = _repo_root().resolve()
    candidate = (root / relative_path).resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError(f'Invalid path outside repository: {relative_path}')
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
            relative_path = str(path.resolve().relative_to(_repo_root().resolve()))
            items.append(
                {
                    'path': relative_path,
                    'filename': path.name,
                    'row_count': len(rows),
                    'source_documents': source_documents,
                    'size_bytes': path.stat().st_size,
                    'updated_at': datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
                }
            )
        except Exception:
            continue
    return items


def get_evaluation_dataset_details(dataset_path: str) -> dict[str, Any]:
    dataset_file = _resolve_repo_path(dataset_path)
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

    return {
        'path': str(dataset_file.relative_to(_repo_root().resolve())),
        'filename': dataset_file.name,
        'row_count': len(rows),
        'source_documents': source_documents,
        'size_bytes': dataset_file.stat().st_size,
        'updated_at': datetime.fromtimestamp(dataset_file.stat().st_mtime, tz=timezone.utc),
        'rows': rows,
    }


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
        if candidate == selected_markdown:
            matched_rows.append(row)
    return matched_rows


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
) -> dict[str, Any]:
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

    dataset_file = _resolve_repo_path(dataset_path)
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
        mrr=float(mrr.score),
        recall_at_k=float(recall.score),
        hit_rate_at_k=float(hit_rate.score),
        retrieval_metrics=retrieval_metrics,
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
        'mrr': float(mrr.score),
        'mean_average_precision': float(map_score.score),
        'ndcg': float(ndcg_score.score),
        'context_length': context_length_value,
        'context_length_metric_source': context_length_metric_source,
        'recall_at_k': float(recall.score),
        'hit_rate_at_k': float(hit_rate.score),
        'retrieval_metrics': retrieval_metrics,
        'mlflow_experiment_id': mlflow_run.get('experiment_id'),
        'mlflow_run_id': mlflow_run.get('run_id'),
        'evaluation_summary': evaluation_summary,
        'per_question_results': per_question_results,
    }