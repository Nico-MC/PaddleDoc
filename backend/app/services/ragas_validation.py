"""Post-generation Ragas checks for source grounding and answer relevance."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Mapping
from typing import Any

from openai import AsyncOpenAI

from app.services.ragas_dataset import _local_embeddings, _model_request_options
from app.services.llm_request_audit import audited_llm_http_client

logger = logging.getLogger(__name__)
# Heuristic triage thresholds; these are not human verification.
ACCEPT_FAITHFULNESS = 0.85
ACCEPT_RELEVANCE = 0.70
REJECT_FAITHFULNESS = 0.50
REJECT_RELEVANCE = 0.40


def _score_value(result: Any) -> float:
    value = float(result.value)
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError('Ragas returned a score outside the range 0–1.')
    return value


def _classify_scores(faithfulness: float | None, relevance: float | None) -> str:
    if faithfulness is None or relevance is None:
        return 'review'
    if faithfulness < REJECT_FAITHFULNESS or relevance < REJECT_RELEVANCE:
        return 'rejected'
    if faithfulness >= ACCEPT_FAITHFULNESS and relevance >= ACCEPT_RELEVANCE:
        return 'accepted'
    return 'review'


def _score_reason(faithfulness: float | None, relevance: float | None, errors: list[str]) -> str | None:
    reasons = []
    if faithfulness is not None and faithfulness < REJECT_FAITHFULNESS:
        reasons.append(f'Faithfulness {faithfulness:.2f} unter Ablehnungsgrenze {REJECT_FAITHFULNESS:.2f}')
    if relevance is not None and relevance < REJECT_RELEVANCE:
        reasons.append(f'Antwort-Relevanz {relevance:.2f} unter Ablehnungsgrenze {REJECT_RELEVANCE:.2f}')
    if reasons:
        return '; '.join(reasons)
    if errors:
        return f'Metrik nicht berechnet: {"; ".join(errors)}'
    return None


def validate_ragas_dataset_rows(
    *,
    rows: list[dict[str, Any]],
    api_base_url: str,
    api_key: str,
    model_name: str,
    request_headers: Mapping[str, str],
    progress_callback: Callable[[dict[str, int | str]], None] | None = None,
    audit_callback: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Score candidates, retaining accepted/review rows and auditing rejections."""
    if not rows:
        raise ValueError('Ragas produced no questions to validate.')

    async def run() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        from ragas.llms import llm_factory
        from ragas.metrics.collections import AnswerRelevancy, Faithfulness

        request_scope: dict[str, Any] = {'phase': 'validation'}
        client = AsyncOpenAI(
            base_url=api_base_url, api_key=api_key, default_headers=dict(request_headers),
            timeout=90.0, max_retries=1,
            http_client=audited_llm_http_client(audit_callback, lambda: request_scope),
        )
        try:
            llm = llm_factory(
                model_name, client=client, temperature=0.0, top_p=1.0, max_tokens=2_400,
                **_model_request_options(model_name),
            )
            faithfulness_metric = Faithfulness(llm=llm)
            relevance_metric = AnswerRelevancy(llm=llm, embeddings=_local_embeddings(), strictness=1)
            retained: list[dict[str, Any]] = []
            details: list[dict[str, Any]] = []
            counts = {'accepted': 0, 'review': 0, 'rejected': 0}
            successful_scores = 0
            for index, source_row in enumerate(rows, start=1):
                if should_cancel and should_cancel():
                    break
                question = str(source_row['question'])
                answer = str(source_row['gold_answer'])
                context = str(source_row['evidence_quote'])
                scores: dict[str, float | None] = {'faithfulness': None, 'answer_relevancy': None}
                errors: list[str] = []
                for name, metric, arguments in (
                    ('faithfulness', faithfulness_metric, {
                        'user_input': question, 'response': answer, 'retrieved_contexts': [context],
                    }),
                    ('answer_relevancy', relevance_metric, {
                        'user_input': question, 'response': answer,
                    }),
                ):
                    request_scope.update(question_id=source_row.get('id'), metric=name)
                    metric_result = None
                    try:
                        if audit_callback:
                            audit_callback({
                                'type': 'validation_input', 'id': source_row.get('id'),
                                'metric': name, 'arguments': arguments,
                            })
                        metric_result = await metric.ascore(**arguments)
                        scores[name] = _score_value(metric_result)
                        successful_scores += 1
                        if audit_callback:
                            audit_callback({
                                'type': 'validation_result', 'id': source_row.get('id'),
                                'metric': name, 'status': 'scored', 'score': scores[name],
                                'reason': getattr(metric_result, 'reason', None),
                                'traces': getattr(metric_result, 'traces', None),
                            })
                    except Exception as exc:
                        if audit_callback:
                            audit_callback({
                                'type': 'validation_result', 'id': source_row.get('id'),
                                'metric': name, 'status': 'error',
                                'error_type': type(exc).__name__, 'error_message': str(exc)[:1000],
                                'raw_score': getattr(metric_result, 'value', None),
                            })
                        logger.warning(
                            'Ragas %s failed for question %s: %s',
                            name, source_row.get('id'), type(exc).__name__,
                        )
                        message = str(exc).strip()[:400]
                        errors.append(f'{name}: {type(exc).__name__}' + (f': {message}' if message else ''))
                status = _classify_scores(scores['faithfulness'], scores['answer_relevancy'])
                counts[status] += 1
                details.append({
                    'id': source_row.get('id'), 'question': question,
                    'status': status, **scores, 'errors': errors,
                    'reason': _score_reason(scores['faithfulness'], scores['answer_relevancy'], errors),
                })
                if status != 'rejected':
                    row = dict(source_row)
                    row.update(
                        validation_method='ragas-faithfulness-answer-relevancy',
                        validation_model=model_name,
                        validation_faithfulness=scores['faithfulness'],
                        validation_answer_relevancy=scores['answer_relevancy'],
                        validation_status=status,
                        review_decision='auto-included' if status == 'accepted' else 'pending',
                        validation_errors=errors,
                        review_status='synthetic-validated' if status == 'accepted' else 'synthetic-review',
                        notes=(
                            'Automatisch mit Ragas bewertet; fachliche Freigabe durch einen Menschen steht aus.'
                            if status == 'accepted' else
                            'Ragas-Bewertung unklar oder unter Akzeptanzschwelle; manuelle Prüfung erforderlich.'
                        ),
                    )
                    retained.append(row)
                if progress_callback:
                    progress_callback({
                        'phase': 'validating', 'validation_done': index,
                        'validation_total': len(rows),
                        'validation_accepted': counts['accepted'],
                        'validation_review': counts['review'],
                        'validation_rejected': counts['rejected'],
                        'questions_generated': len(rows), 'question_target': len(rows),
                    })
            if not (should_cancel and should_cancel()) and successful_scores == 0:
                raise RuntimeError('Ragas-Validierung fehlgeschlagen: keine Metrik konnte berechnet werden.')
            return retained, {
                'candidates': len(rows), **counts, 'details': details,
                'thresholds': {
                    'accept_faithfulness': ACCEPT_FAITHFULNESS,
                    'accept_answer_relevancy': ACCEPT_RELEVANCE,
                    'reject_faithfulness': REJECT_FAITHFULNESS,
                    'reject_answer_relevancy': REJECT_RELEVANCE,
                },
            }
        finally:
            await client.close()

    return asyncio.run(run())
