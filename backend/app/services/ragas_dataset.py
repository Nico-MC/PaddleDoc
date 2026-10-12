"""Ragas test-set generation over small, page-aware Markdown sections."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Any

from openai import AsyncOpenAI

from app.schemas.jobs import DEFAULT_RAGAS_PERSONA_NAME, DEFAULT_RAGAS_PERSONA_ROLE_DESCRIPTION
from app.services.dataset_assistant import (
    QUESTION_STYLES,
    _Passage,
    _markdown_passages,
    _sampling_regions,
)
from app.services.generation_source_filter import filter_generation_passages
from app.services.rag_text_preprocessing import prepare_rag_text, prepare_rag_text_with_report
from app.services.llm_request_audit import audited_llm_http_client

logger = logging.getLogger(__name__)
_EMBEDDING_MODEL = 'intfloat/multilingual-e5-small'
DEFAULT_CONTEXT_CHARS = 1_400
DEFAULT_PASSAGE_TRIES = 3
MIN_CONTEXT_CHARS = 40
QUERY_MODES = frozenset({'facts', 'themes', 'mixed'})


@lru_cache(maxsize=1)
def _local_embeddings() -> Any:
    from ragas.embeddings import HuggingFaceEmbeddings

    return HuggingFaceEmbeddings(model=_EMBEDDING_MODEL, device='cpu')


def _section_context(passage: _Passage, max_chars: int = DEFAULT_CONTEXT_CHARS) -> str:
    text = passage.text.strip()
    if len(text) <= max_chars:
        return text
    boundary = text.rfind(' ', 0, max_chars)
    return text[:boundary if boundary >= max_chars // 2 else max_chars].strip()


def _generation_candidates(
    region: list[_Passage], passage_tries: int, context_chars: int,
) -> list[tuple[_Passage, str]]:
    """Short snippets do not consume Ragas attempts."""
    candidates: list[tuple[_Passage, str]] = []
    for passage in region:
        context = _section_context(passage, context_chars)
        if len(context) < MIN_CONTEXT_CHARS:
            continue
        candidates.append((passage, context))
        if len(candidates) >= passage_tries:
            break
    return candidates


def _section_regions(markdown: str, question_count: int, seed: int) -> tuple[str, list[list[_Passage]]]:
    passages = _markdown_passages(prepare_rag_text(markdown))
    method, regions = _sampling_regions(passages, question_count, seed)
    return method, [region for region in regions if region]


def _query_mode_for_region(query_mode: str, region_index: int) -> str:
    return ('themes' if region_index % 2 == 0 else 'facts') if query_mode == 'mixed' else query_mode


def _model_request_options(model_name: str) -> dict[str, Any]:
    lowered = model_name.casefold()
    if 'deepseek' in lowered:
        return {'extra_body': {'chat_template_kwargs': {'thinking': False}}}
    if 'qwen3' in lowered:
        return {'extra_body': {'chat_template_kwargs': {'enable_thinking': False}}}
    return {}


def generate_ragas_dataset_rows(
    *,
    markdown: str,
    source_document: str,
    source_file: str,
    source_file_sha256: str | None,
    api_base_url: str,
    api_key: str,
    model_name: str,
    question_count: int,
    question_style: str,
    focus: str,
    sampling_seed: int,
    request_headers: Mapping[str, str],
    pdf_toc: list[list[Any]] | None = None,
    query_mode: str = 'facts',
    context_chars: int = DEFAULT_CONTEXT_CHARS,
    passage_tries: int = DEFAULT_PASSAGE_TRIES,
    persona_name: str = DEFAULT_RAGAS_PERSONA_NAME,
    persona_role_description: str = DEFAULT_RAGAS_PERSONA_ROLE_DESCRIPTION,
    progress_callback: Callable[[dict[str, int | str]], None] | None = None,
    audit_callback: Callable[[dict[str, Any]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Run Ragas section by section and retain only exact source contexts."""

    if question_style not in QUESTION_STYLES:
        raise ValueError('Unknown question style.')
    if not 1 <= question_count <= 30:
        raise ValueError('Question count must be between 1 and 30.')
    if query_mode not in QUERY_MODES:
        raise ValueError('Unknown Ragas question type.')
    if context_chars not in {800, 1400, 2400}:
        raise ValueError('Ragas context length must be 800, 1400 or 2400 characters.')
    if not 1 <= passage_tries <= 5:
        raise ValueError('Ragas passage attempts must be between 1 and 5.')
    persona_name = persona_name.strip()
    persona_role_description = persona_role_description.strip()
    if not persona_name or not persona_role_description:
        raise ValueError('Ragas generation persona must have a name and role description.')
    if not api_key or not api_base_url or not model_name:
        raise ValueError('HCDS model configuration is incomplete.')

    from ragas.llms import llm_factory
    from ragas.run_config import RunConfig
    from ragas.testset import TestsetGenerator
    from ragas.testset.persona import Persona
    from ragas.testset.transforms.extractors.llm_based import KeyphrasesExtractor, NERExtractor
    from ragas.testset.synthesizers.single_hop.specific import SingleHopSpecificQuerySynthesizer
    from langchain_core.documents import Document

    cleaned_markdown, cleaning_report = prepare_rag_text_with_report(markdown)
    if audit_callback:
        audit_callback({
            'type': 'prepared_document', 'text': cleaned_markdown,
            **cleaning_report,
            'sha256': hashlib.sha256(cleaned_markdown.encode('utf-8')).hexdigest(),
            'stage': 'after_picture_text_and_page_furniture_cleanup_before_section_filter',
        })
    passages = _markdown_passages(cleaned_markdown)
    raw_hash = hashlib.sha256(markdown.encode('utf-8')).hexdigest()
    rows: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    last_error: Exception | None = None
    context_instruction = (
        'Stelle Fragen und Referenzantworten auf Deutsch. '
        'Nutze ausschließlich den bereitgestellten Dokumentabschnitt. '
        'Die Antwort muss vollständig aus diesem Abschnitt belegbar sein. '
        f'Fragenstil: {QUESTION_STYLES[question_style]}. '
        f'Themenfokus, sofern im Abschnitt belegt: {focus.strip() or "alle wesentlichen Inhalte"}.'
    )

    request_scope: dict[str, Any] = {'phase': 'classification'}
    client = AsyncOpenAI(
        base_url=api_base_url,
        api_key=api_key,
        default_headers=dict(request_headers),
        timeout=90.0,
        max_retries=1,
        http_client=audited_llm_http_client(audit_callback, lambda: request_scope),
    )
    try:
        llm = llm_factory(
            model_name, client=client, temperature=0.0, top_p=1.0, max_tokens=2_400,
            **_model_request_options(model_name),
        )
        if progress_callback:
            progress_callback({
                'phase': 'classifying', 'region': 0, 'region_count': 1,
                'passage_attempt': 0, 'passages_checked': 0,
                'passages_available': len(passages), 'questions_generated': 0,
                'question_target': question_count,
            })
        eligible, excluded_sections = filter_generation_passages(
            passages, llm=llm, toc=pdf_toc, audit_callback=audit_callback,
        )
        request_scope['phase'] = 'generation'
        if audit_callback:
            audit_callback({
                'type': 'source_selection', 'passages_total': len(passages),
                'passages_eligible': len(eligible),
                'excluded_sections': [
                    {'heading': section.heading, 'page': section.page, 'excerpt': section.preview}
                    for section in excluded_sections
                ],
            })
        logger.info('Ragas source filter kept %s of %s passages; excluded %s sections',
                    len(eligible), len(passages), len(excluded_sections))
        method, regions = _sampling_regions(eligible, question_count, sampling_seed)
        regions = [region for region in regions if region]
        quotas = [
            question_count // len(regions) + (index < question_count % len(regions))
            for index in range(len(regions))
        ]
        embeddings = _local_embeddings()
        persona = Persona(
            name=persona_name,
            role_description=persona_role_description,
        )
        synthesizers = {
            mode: SingleHopSpecificQuerySynthesizer(
                llm=llm, llm_context=context_instruction, property_name=property_name,
            )
            for mode, property_name in [('facts', 'entities'), ('themes', 'keyphrases')]
        }
        for region_index, (region, quota) in enumerate(zip(regions, quotas), start=1):
            if should_cancel and should_cancel():
                break
            region_query_mode = _query_mode_for_region(query_mode, region_index)
            extractor = NERExtractor(llm=llm) if region_query_mode == 'facts' else KeyphrasesExtractor(llm=llm)
            candidates = _generation_candidates(region, passage_tries, context_chars)
            region_start = len(rows)
            attempts = 0
            model_errors = 0
            for passage, context in candidates:
                if len(rows) - region_start >= quota or len(rows) >= question_count:
                    break
                generator = TestsetGenerator(
                    llm=llm,
                    embedding_model=embeddings,
                    persona_list=[persona],
                    llm_context=context_instruction,
                )
                if audit_callback:
                    audit_callback({
                        'type': 'generation_input', 'passage_id': passage.id,
                        'page': passage.page_number, 'anchor': passage.anchor,
                        'region': region_index, 'query_mode': region_query_mode,
                        'question_quota': quota, 'context': context,
                        'context_instruction': context_instruction,
                        'persona_name': persona.name,
                        'persona_role_description': persona.role_description,
                    })
                attempts += 1
                request_scope.update(region=region_index, passage_id=passage.id,
                                     page=passage.page_number, query_mode=region_query_mode)
                try:
                    testset = generator.generate_with_chunks(
                        chunks=[Document(
                            page_content=context,
                            metadata={
                                'source_document': source_document,
                                'source_page': passage.page_number,
                                'evidence_anchor': passage.anchor,
                            },
                        )],
                        testset_size=quota - (len(rows) - region_start),
                        transforms=[extractor],
                        query_distribution=[(synthesizers[region_query_mode], 1.0)],
                        run_config=RunConfig(
                            timeout=90, max_retries=2, max_wait=10,
                            max_workers=2, seed=sampling_seed + region_index,
                        ),
                        raise_exceptions=True,
                    )
                except Exception as exc:
                    last_error = exc
                    model_errors += 1
                    if audit_callback:
                        audit_callback({
                            'type': 'generation_result', 'region': region_index,
                            'passage_id': passage.id, 'status': 'model_error',
                            'error_type': type(exc).__name__, 'error_message': str(exc)[:1000],
                            'samples': 0, 'accepted': 0,
                        })
                    logger.warning(
                        'Ragas could not generate from region %s, passage %s: %s',
                        region_index, passage.id, type(exc).__name__,
                    )
                    continue

                samples = testset.to_list()
                accepted = 0
                sample_decisions: list[dict[str, Any]] = []
                discarded = {
                    'context_mismatch': 0, 'missing_question': 0,
                    'missing_answer': 0, 'duplicate_question': 0,
                }
                for sample_index, sample in enumerate(samples):
                    contexts = sample.get('reference_contexts') or []
                    question = str(sample.get('user_input') or '').strip()
                    answer = str(sample.get('reference') or '').strip()
                    if contexts != [context]:
                        discarded['context_mismatch'] += 1
                        sample_decisions.append({'sample_index': sample_index, 'outcome': 'discarded', 'reason': 'context_mismatch'})
                        continue
                    if not question:
                        discarded['missing_question'] += 1
                        sample_decisions.append({'sample_index': sample_index, 'outcome': 'discarded', 'reason': 'missing_question'})
                        continue
                    if not answer:
                        discarded['missing_answer'] += 1
                        sample_decisions.append({'sample_index': sample_index, 'outcome': 'discarded', 'reason': 'missing_answer'})
                        continue
                    normalized_question = ' '.join(question.casefold().split())
                    if normalized_question in seen_questions:
                        discarded['duplicate_question'] += 1
                        sample_decisions.append({'sample_index': sample_index, 'outcome': 'discarded', 'reason': 'duplicate_question'})
                        continue
                    seen_questions.add(normalized_question)
                    row: dict[str, Any] = {
                        'id': f'q{len(rows) + 1:03d}',
                        'question': question,
                        'gold_answer': answer,
                        'evidence_quote': context,
                        'evidence_anchor': passage.anchor,
                        'evidence_passage_id': passage.id,
                        'source_document': source_document,
                        'source_file': source_file,
                        'source_markdown_sha256': raw_hash,
                        'review_status': 'synthetic',
                        'generation_model': model_name,
                        'generation_pipeline': 'ragas',
                        'generation_question_target': question_count,
                        'ragas_synthesizer': sample.get('synthesizer_name'),
                        'ragas_persona_name': sample.get('persona_name') or persona.name,
                        'ragas_persona_role_description': persona.role_description,
                        'ragas_query_mode': region_query_mode,
                        'ragas_context_chars': context_chars,
                        'ragas_passage_tries': passage_tries,
                        'sampling_method': method,
                        'sampling_seed': sampling_seed,
                        'sampling_region': region_index,
                        'sampling_region_count': len(regions),
                        'notes': 'Von Ragas erzeugt; Referenzkontext wortgetreu geprüft, Antwort fachlich noch nicht manuell geprüft.',
                    }
                    if source_file_sha256:
                        row['source_file_sha256'] = source_file_sha256
                    if passage.page_number is not None:
                        row['source_page'] = passage.page_number
                    rows.append(row)
                    sample_decisions.append({'sample_index': sample_index, 'outcome': 'accepted', 'row_id': row['id']})
                    accepted += 1
                    if len(rows) - region_start >= quota or len(rows) >= question_count:
                        break
                for sample_index in range(len(sample_decisions), len(samples)):
                    sample_decisions.append({
                        'sample_index': sample_index, 'outcome': 'not_processed', 'reason': 'quota_reached',
                    })
                if audit_callback:
                    audit_callback({
                        'type': 'generation_result', 'region': region_index,
                        'passage_id': passage.id,
                        'status': 'generated' if accepted else 'no_usable_question',
                        'samples': len(samples), 'accepted': accepted, 'discarded': discarded,
                        'raw_samples': samples, 'sample_decisions': sample_decisions,
                    })

            generated_in_region = len(rows) - region_start
            reason = (
                'complete' if generated_in_region >= quota else
                'no_usable_passage' if not candidates else
                'model_error' if attempts and model_errors == attempts else
                'no_usable_question'
            )
            if audit_callback:
                audit_callback({
                    'type': 'region_result', 'region': region_index, 'target': quota,
                    'generated': generated_in_region, 'attempts': attempts,
                    'passages_available': len(region),
                    'short_passages_in_region': sum(
                        len(_section_context(passage, context_chars)) < MIN_CONTEXT_CHARS
                        for passage in region
                    ),
                    'reason': reason,
                })

            if progress_callback:
                progress_callback({
                    'phase': 'generating',
                    'region': region_index,
                    'region_count': len(regions),
                    'passage_attempt': region_index,
                    'passages_checked': region_index,
                    'passages_available': len(regions),
                    'questions_generated': len(rows),
                    'question_target': question_count,
                })

    finally:
        asyncio.run(client.close())

    if not rows and not (should_cancel and should_cancel()):
        message = 'Ragas konnte keine belegten Fragen aus den ausgewählten Dokumentabschnitten erzeugen.'
        if last_error is not None:
            message = f'{message} Technischer Fehler: {type(last_error).__name__}.'
        raise RuntimeError(message)
    return rows
