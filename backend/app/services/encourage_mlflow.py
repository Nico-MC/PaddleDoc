from __future__ import annotations

import os
from pathlib import Path
from statistics import mean
from typing import Any


def _mlflow_from_pandas() -> Any:
    import mlflow.data

    return getattr(mlflow.data, 'from_pandas')


def _tracking_uri() -> str:
    configured = os.getenv('MLFLOW_TRACKING_URI', '').strip()
    if configured:
        return configured

    # Inside docker-compose network the service is reachable as `mlflow`.
    if Path('/.dockerenv').exists():
        return 'http://mlflow:5000'
    return 'http://localhost:5000'


def _experiment_name() -> str:
    return os.getenv('MLFLOW_EXPERIMENT_NAME', 'paddledoc-encourage').strip() or 'paddledoc-encourage'


def _ensure_mlflow_no_proxy() -> None:
    hosts = {'mlflow', 'mlflow:5000'}
    for key in ('NO_PROXY', 'no_proxy'):
        current = os.getenv(key, '').strip()
        values = [part.strip() for part in current.split(',') if part.strip()]
        updated = values + [host for host in hosts if host not in values]
        os.environ[key] = ','.join(updated)


def _safe_param_value(value: Any) -> str | int | float | bool:
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _log_common_params(*, event: str, metadata: dict[str, Any], query: str | None = None) -> None:
    import mlflow

    params = {
        'event': event,
        'pipeline_id': metadata.get('pipeline_id', ''),
        'collection_name': metadata.get('collection_name', ''),
        'rag_method': metadata.get('rag_method', ''),
        'top_k': metadata.get('top_k', 0),
        'document_count': metadata.get('document_count', 0),
        'chunk_max_chars': metadata.get('chunk_max_chars', 0),
        'chunk_overlap_chars': metadata.get('chunk_overlap_chars', 0),
        'source_md_path': metadata.get('source_md_path', ''),
        'source_md_filename': metadata.get('source_md_filename', ''),
    }

    if query is not None:
        params['query'] = query.strip()[:500]

    mlflow.log_params({key: _safe_param_value(value) for key, value in params.items()})


def _log_source_md_dataset(*, metadata: dict[str, Any]) -> None:
    """Log source markdown provenance as an MLflow input dataset and artifact."""
    source_md_path = str(metadata.get('source_md_path', '') or '')
    source_md_filename = str(metadata.get('source_md_filename', '') or '')
    if not source_md_path:
        return

    import mlflow
    import pandas as pd

    dataset = _mlflow_from_pandas()(
        pd.DataFrame(
            [
                {
                    'source_md_path': source_md_path,
                    'source_md_filename': source_md_filename,
                    'pipeline_id': str(metadata.get('pipeline_id', '')),
                    'collection_name': str(metadata.get('collection_name', '')),
                }
            ]
        ),
        source=source_md_path,
        name=source_md_filename or Path(source_md_path).name,
    )
    mlflow.log_input(dataset, context='source_markdown')

    mlflow.log_dict(
        {
            'source_md_path': source_md_path,
            'source_md_filename': source_md_filename,
            'pipeline_id': str(metadata.get('pipeline_id', '')),
            'collection_name': str(metadata.get('collection_name', '')),
        },
        'source_markdown.json',
    )


def log_ingest_run(
    *,
    metadata: dict[str, Any],
    config: dict[str, Any],
    collection: dict[str, Any],
    document_dump: dict[str, Any],
    query: str,
    run_generation: bool,
    rag_run: dict[str, Any] | None,
) -> None:
    """Best-effort MLflow logging for Encourage ingest runs."""
    try:
        import mlflow

        _ensure_mlflow_no_proxy()
        mlflow.set_tracking_uri(_tracking_uri())
        mlflow.set_experiment(_experiment_name())

        with mlflow.start_run(run_name='encourage_ingest'):
            mlflow.set_tags(
                {
                    'component': 'encourage',
                    'event_type': 'ingest',
                    'pipeline_id': str(metadata.get('pipeline_id', '')),
                    'source_md_path': str(metadata.get('source_md_path', '')),
                }
            )
            _log_common_params(event='ingest', metadata=metadata, query=query)
            _log_source_md_dataset(metadata=metadata)
            mlflow.log_param('run_generation', run_generation)

            mlflow.log_metric('chunk_count', float(metadata.get('document_count', 0) or 0))

            if rag_run is not None:
                answer_text = str(rag_run.get('answer', ''))
                mlflow.log_param('generation_model_name', str(rag_run.get('model_name', '')))
                mlflow.log_metric('generation_answer_chars', float(len(answer_text)))

            mlflow.log_dict(config, 'config.json')
            mlflow.log_dict(collection, 'collection.json')
            mlflow.log_dict(document_dump, 'document_dump.json')
            if rag_run is not None:
                mlflow.log_dict(rag_run, 'rag_run.json')
    except Exception:
        # Never fail API requests because tracking is unavailable.
        return


def log_retrieve_run(
    *,
    metadata: dict[str, Any],
    query: str,
    results: list[dict[str, Any]],
) -> None:
    """Best-effort MLflow logging for Encourage retrieval probes."""
    try:
        import mlflow

        _ensure_mlflow_no_proxy()
        mlflow.set_tracking_uri(_tracking_uri())
        mlflow.set_experiment(_experiment_name())

        with mlflow.start_run(run_name='encourage_retrieve'):
            mlflow.set_tags(
                {
                    'component': 'encourage',
                    'event_type': 'retrieve',
                    'pipeline_id': str(metadata.get('pipeline_id', '')),
                    'source_md_path': str(metadata.get('source_md_path', '')),
                }
            )
            _log_common_params(event='retrieve', metadata=metadata, query=query)

            scores = [float(item['score']) for item in results if isinstance(item.get('score'), (int, float))]
            distances = [
                float(item['distance'])
                for item in results
                if isinstance(item.get('distance'), (int, float))
            ]

            mlflow.log_metric('retrieved_count', float(len(results)))
            if scores:
                mlflow.log_metric('score_max', max(scores))
                mlflow.log_metric('score_mean', mean(scores))
            if distances:
                mlflow.log_metric('distance_min', min(distances))
                mlflow.log_metric('distance_mean', mean(distances))

            artifact_payload = {
                'query': query,
                'results': results,
            }
            mlflow.log_dict(artifact_payload, 'retrieval_results.json')
    except Exception:
        # Never fail API requests because tracking is unavailable.
        return


def log_generate_run(
    *,
    metadata: dict[str, Any],
    query: str,
    model_name: str,
    answer: str,
) -> None:
    """Best-effort MLflow logging for Encourage generation probes."""
    try:
        import mlflow

        _ensure_mlflow_no_proxy()
        mlflow.set_tracking_uri(_tracking_uri())
        mlflow.set_experiment(_experiment_name())

        with mlflow.start_run(run_name='encourage_generate'):
            mlflow.set_tags(
                {
                    'component': 'encourage',
                    'event_type': 'generate',
                    'pipeline_id': str(metadata.get('pipeline_id', '')),
                    'source_md_path': str(metadata.get('source_md_path', '')),
                }
            )
            _log_common_params(event='generate', metadata=metadata, query=query)
            mlflow.log_param('generation_model_name', model_name)
            mlflow.log_metric('generation_answer_chars', float(len(answer)))
            mlflow.log_dict(
                {
                    'query': query,
                    'model_name': model_name,
                    'answer': answer,
                },
                'generation_preview.json',
            )
    except Exception:
        # Never fail API requests because tracking is unavailable.
        return


def log_evaluation_run(
    *,
    metadata: dict[str, Any],
    dataset_path: str,
    dataset_filename: str,
    question_count: int,
    evaluated_question_count: int,
    recall_k: int,
    evaluation_mode: str,
    mrr: float,
    recall_at_k: float,
    hit_rate_at_k: float,
    retrieval_metrics: dict[str, float],
    advanced_metrics: dict[str, float],
    advanced_status: str,
    warnings: list[str],
    dataset_rows: list[dict[str, Any]],
    per_question_results: list[dict[str, Any]],
    evaluation_summary: dict[str, Any],
) -> dict[str, str | None]:
    """Best-effort MLflow logging for Encourage retrieval evaluation runs."""
    try:
        import mlflow
        import pandas as pd

        _ensure_mlflow_no_proxy()
        mlflow.set_tracking_uri(_tracking_uri())
        mlflow.set_experiment(_experiment_name())

        with mlflow.start_run(run_name='encourage_evaluation'):
            mlflow.set_tags(
                {
                    'component': 'encourage',
                    'event_type': 'evaluation',
                    'pipeline_id': str(metadata.get('pipeline_id', '')),
                    'source_md_path': str(metadata.get('source_md_path', '')),
                    'dataset_path': dataset_path,
                    'dataset_filename': dataset_filename,
                }
            )
            _log_common_params(event='evaluation', metadata=metadata)
            evaluation_dataset = _mlflow_from_pandas()(
                pd.DataFrame(dataset_rows),
                source=dataset_path,
                name=dataset_filename or Path(dataset_path).name,
            )
            mlflow.log_input(evaluation_dataset, context='evaluation_dataset')
            mlflow.log_param('dataset_path', dataset_path)
            mlflow.log_param('dataset_filename', dataset_filename)
            mlflow.log_param('question_count', question_count)
            mlflow.log_param('evaluated_question_count', evaluated_question_count)
            mlflow.log_param('recall_k', recall_k)
            mlflow.log_param('evaluation_mode', evaluation_mode)
            mlflow.log_param('advanced_status', advanced_status)
            mlflow.log_metric('mrr', float(mrr))
            mlflow.log_metric(f'recall_at_{recall_k}', float(recall_at_k))
            mlflow.log_metric(f'hit_rate_at_{recall_k}', float(hit_rate_at_k))
            for metric_name, metric_value in retrieval_metrics.items():
                mlflow.log_metric(metric_name, float(metric_value))
            for metric_name, metric_value in advanced_metrics.items():
                mlflow.log_metric(metric_name, float(metric_value))
            mlflow.log_dict({'rows': dataset_rows}, 'evaluation_dataset_rows.json')
            mlflow.log_dict({'results': per_question_results}, 'evaluation_question_results.json')
            mlflow.log_dict(evaluation_summary, 'evaluation_summary.json')
            mlflow.log_dict(retrieval_metrics, 'evaluation_retrieval_metrics.json')
            mlflow.log_dict(advanced_metrics, 'evaluation_advanced_metrics.json')
            mlflow.log_dict({'warnings': warnings}, 'evaluation_warnings.json')
            mlflow.log_dict(
                {
                    'metadata': {
                        'pipeline_id': str(metadata.get('pipeline_id', '')),
                        'source_md_path': str(metadata.get('source_md_path', '')),
                        'dataset_path': dataset_path,
                        'dataset_filename': dataset_filename,
                        'question_count': question_count,
                        'evaluated_question_count': evaluated_question_count,
                        'recall_k': recall_k,
                        'evaluation_mode': evaluation_mode,
                        'advanced_status': advanced_status,
                    },
                    'summary': {
                        'mrr': float(mrr),
                        'recall_at_k': float(recall_at_k),
                        'hit_rate_at_k': float(hit_rate_at_k),
                        'retrieval_metrics': retrieval_metrics,
                        'advanced_metrics': advanced_metrics,
                        'warnings': warnings,
                        'evaluation_summary': evaluation_summary,
                    },
                    'dataset_rows': dataset_rows,
                    'per_question_results': per_question_results,
                },
                'evaluation_run.json',
            )
            mlflow.log_dict(
                {
                    'pipeline_id': str(metadata.get('pipeline_id', '')),
                    'source_md_path': str(metadata.get('source_md_path', '')),
                    'dataset_path': dataset_path,
                    'dataset_filename': dataset_filename,
                    'question_count': question_count,
                    'evaluated_question_count': evaluated_question_count,
                    'recall_k': recall_k,
                    'evaluation_mode': evaluation_mode,
                    'advanced_status': advanced_status,
                    'mrr': float(mrr),
                    'recall_at_k': float(recall_at_k),
                    'hit_rate_at_k': float(hit_rate_at_k),
                    'retrieval_metrics': retrieval_metrics,
                    'advanced_metrics': advanced_metrics,
                    'warnings': warnings,
                },
                'evaluation_overview.json',
            )
            active_run = mlflow.active_run()
            if active_run is None:
                return {'run_id': None, 'experiment_id': None}
            return {
                'run_id': active_run.info.run_id,
                'experiment_id': active_run.info.experiment_id,
            }
    except Exception:
        return {'run_id': None, 'experiment_id': None}
