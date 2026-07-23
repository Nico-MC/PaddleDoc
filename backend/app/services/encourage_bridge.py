from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any
import uuid


_RAG_PIPELINES: dict[str, Any] = {}
_DEFAULT_SYSTEM_PROMPT = (
    'Du bist ein hilfreicher Assistent. Beantworte die Frage nur auf Basis des bereitgestellten Kontexts. '
    'Wenn die Information im Kontext fehlt, sage das klar.'
)


class _PaddleDocSamplingParams:
    """Minimal sampling params object compatible with BatchInferenceRunner."""

    def __init__(self, max_tokens: int = 512, temperature: float = 0.1, top_p: float = 1.0):
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.top_p = top_p
        self.seed: int | None = None


def _encourage_src_path() -> Path:
    configured_path = os.getenv('PADDLEDOC_ENCOURAGE_SRC_PATH')
    candidate_paths: list[Path] = []

    if configured_path:
        candidate_paths.append(Path(configured_path).expanduser())

    for parent in Path(__file__).resolve().parents:
        candidate_paths.append(parent / 'encourage' / 'src')

    for candidate_path in candidate_paths:
        resolved_path = candidate_path.resolve()
        if resolved_path.exists():
            return resolved_path

    searched_paths = ', '.join(str(path) for path in candidate_paths)
    raise RuntimeError(f'Encourage source path not found. Checked: {searched_paths}')


@lru_cache(maxsize=1)
def _markdown_ingestion_class() -> type:
    encourage_src = _encourage_src_path()
    encourage_src_str = str(encourage_src)
    if encourage_src_str not in sys.path:
        sys.path.insert(0, encourage_src_str)

    from encourage.utils.markdown_ingestion import MarkdownIngestion

    return MarkdownIngestion


@lru_cache(maxsize=1)
def _base_rag_class() -> type:
    encourage_src = _encourage_src_path()
    encourage_src_str = str(encourage_src)
    if encourage_src_str not in sys.path:
        sys.path.insert(0, encourage_src_str)

    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    from encourage.rag.base_impl import BaseRAG

    class PaddleDocMarkdownRAG(BaseRAG):
        def get_embedding_model(self, name: str, device: str = 'cpu') -> Any:
            return DefaultEmbeddingFunction()

    return PaddleDocMarkdownRAG


@lru_cache(maxsize=1)
def _base_rag_config_class() -> type:
    encourage_src = _encourage_src_path()
    encourage_src_str = str(encourage_src)
    if encourage_src_str not in sys.path:
        sys.path.insert(0, encourage_src_str)

    from encourage.rag.base.config import BaseRAGConfig

    return BaseRAGConfig


@lru_cache(maxsize=1)
def _batch_inference_runner_class() -> type:
    encourage_src = _encourage_src_path()
    encourage_src_str = str(encourage_src)
    if encourage_src_str not in sys.path:
        sys.path.insert(0, encourage_src_str)

    from encourage.llm import BatchInferenceRunner

    return BatchInferenceRunner


def _normalize_openai_base_url(base_url: str) -> str:
    cleaned = base_url.strip().rstrip('/')
    if not cleaned:
        raise ValueError(
            'OPENAI_API_BASE_URL is not configured. Set it in PaddleDoc settings or docker env.'
        )
    if cleaned.endswith('/v1'):
        return cleaned
    return f'{cleaned}/v1'


def ingest_markdown_file(path: Path) -> dict[str, Any]:
    loader_cls = _markdown_ingestion_class()
    rag_cls = _base_rag_class()
    rag_config_cls = _base_rag_config_class()
    loader = loader_cls()
    documents = loader.load(path)
    if not documents:
        raise RuntimeError(f'No encourage documents created from markdown file: {path}')

    document = documents[0]
    pipeline_id = str(uuid.uuid4())
    collection_name = f'encourage-{path.stem}-{pipeline_id[:8]}'
    rag_config = rag_config_cls(
        context_collection=documents,
        collection_name=collection_name,
        embedding_function='default',
        top_k=min(3, len(documents)),
        retrieval_only=False,
        device='cpu',
        template_name='default.j2',
    )
    rag_pipeline = rag_cls(rag_config)
    _RAG_PIPELINES[pipeline_id] = rag_pipeline
    collection = rag_pipeline.client.get_collection(collection_name)

    config_dump = {
        'collection_name': rag_config.collection_name,
        'embedding_function': rag_config.embedding_function,
        'top_k': rag_config.top_k,
        'retrieval_only': rag_config.retrieval_only,
        'device': rag_config.device,
        'template_name': rag_config.template_name,
        'batch_size_insert': rag_config.batch_size_insert,
        'batch_size_query': rag_config.batch_size_query,
        'document_ids': [str(doc.id) for doc in documents],
        'document_filenames': [str(doc.meta_data['filename'] or '') for doc in documents],
    }
    collection_dump = {
        'name': collection.name,
        'metadata': collection.metadata,
        'document_count': rag_pipeline.client.count_documents(collection_name),
    }
    document_dump = {
        'id': str(document.id),
        'content_preview': document.content[:500],
        'content_length': len(document.content),
        'meta_data': document.meta_data.to_dict(truncated=False),
    }

    return {
        'id': str(document.id),
        'content': document.content,
        'score': document.score,
        'distance': document.distance,
        'meta_data': document.meta_data.to_dict(truncated=False),
        'pipeline_id': pipeline_id,
        'collection_name': collection_name,
        'document_count': len(documents),
        'top_k': rag_pipeline.top_k,
        'rag_method': 'BaseRAG',
        'ready': True,
        'config': config_dump,
        'collection': collection_dump,
        'document_dump': document_dump,
    }


def run_pipeline_once(
    pipeline_id: str,
    query: str,
    *,
    api_base_url: str,
    api_key: str,
    model_name: str | None = None,
    system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
    max_tokens: int = 512,
    temperature: float = 0.1,
    top_p: float = 1.0,
) -> dict[str, Any]:
    rag_pipeline = _RAG_PIPELINES.get(pipeline_id)
    if rag_pipeline is None:
        raise KeyError(f'Encourage pipeline not found: {pipeline_id}')

    query_text = query.strip()
    if not query_text:
        raise ValueError('Query must not be empty')

    api_key_clean = api_key.strip()
    if not api_key_clean:
        raise ValueError(
            'OPENAI_API_BEARER_TOKEN is not configured. Set it in PaddleDoc settings or docker env.'
        )

    model_name_clean = (model_name or '').strip() or os.getenv(
        'PADDLEDOC_ENCOURAGE_MODEL_NAME',
        'gpt-4o-mini',
    )
    api_base = _normalize_openai_base_url(api_base_url)
    runner_cls = _batch_inference_runner_class()

    env_var_name = 'PADDLEDOC_ENCOURAGE_LLM_API_KEY'
    os.environ[env_var_name] = api_key_clean

    sampling_params = _PaddleDocSamplingParams(
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
    )
    runner = runner_cls(
        sampling_parameters=sampling_params,
        model_name=model_name_clean,
        base_url=api_base,
        env_var_name=env_var_name,
        max_workers=1,
        batch_size=1,
    )

    responses = rag_pipeline.run(
        runner=runner,
        sys_prompt=system_prompt,
        user_prompts=[query_text],
        retrieval_queries=[query_text],
    )

    answer = ''
    if len(responses) > 0:
        first = responses[0].response
        answer = first.strip() if isinstance(first, str) else str(first)

    return {
        'query': query_text,
        'model_name': model_name_clean,
        'answer': answer,
    }


def retrieve_from_pipeline(pipeline_id: str, query: str) -> dict[str, Any]:
    rag_pipeline = _RAG_PIPELINES.get(pipeline_id)
    if rag_pipeline is None:
        raise KeyError(f'Encourage pipeline not found: {pipeline_id}')

    query_text = query.strip()
    if not query_text:
        raise ValueError('Query must not be empty')

    documents = rag_pipeline.retrieve_contexts([query_text])[0]
    return {
        'pipeline_id': pipeline_id,
        'collection_name': rag_pipeline.collection_name,
        'query': query_text,
        'top_k': rag_pipeline.top_k,
        'results': [
            {
                'id': str(document.id),
                'content': document.content,
                'score': document.score,
                'distance': document.distance,
                'meta_data': document.meta_data.to_dict(truncated=False),
            }
            for document in documents
        ],
    }