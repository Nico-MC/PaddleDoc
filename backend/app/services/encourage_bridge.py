from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
import uuid


_RAG_PIPELINES: dict[str, Any] = {}
_RAG_PIPELINE_METADATA: dict[str, dict[str, Any]] = {}
_DEFAULT_SYSTEM_PROMPT = (
    'Du bist ein hilfreicher Assistent. Beantworte die Frage nur auf Basis des bereitgestellten Kontexts. '
    'Wenn die Information im Kontext fehlt, sage das klar.'
)
_DEFAULT_TOP_K = 5
_DEFAULT_CHUNK_MAX_CHARS = 1100
_DEFAULT_CHUNK_OVERLAP_CHARS = 140
_SUPPORTED_RAG_METHODS: tuple[str, ...] = ('Base', 'BM25', 'HybridBM25')


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

    from encourage.utils.markdown_ingestion import MarkdownIngestion  # type: ignore[reportMissingImports]

    return MarkdownIngestion


def _normalize_rag_method_name(rag_method: str | None) -> str:
    candidate = (rag_method or '').strip().lower().replace('_', '')
    method_aliases = {
        '': 'Base',
        'base': 'Base',
        'baserag': 'Base',
        'bm25': 'BM25',
        'bm25rag': 'BM25',
        'hybridbm25': 'HybridBM25',
        'hybridbm25rag': 'HybridBM25',
    }
    resolved = method_aliases.get(candidate)
    if resolved is None:
        supported = ', '.join(_SUPPORTED_RAG_METHODS)
        raise ValueError(f'Unsupported rag_method: {rag_method!r}. Supported methods: {supported}')
    return resolved


@lru_cache(maxsize=None)
def _rag_pipeline_components(rag_method: str) -> tuple[type, type, str]:
    encourage_src = _encourage_src_path()
    encourage_src_str = str(encourage_src)
    if encourage_src_str not in sys.path:
        sys.path.insert(0, encourage_src_str)

    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

    method = _normalize_rag_method_name(rag_method)
    if method == 'Base':
        from encourage.rag.base.config import BaseRAGConfig  # type: ignore[reportMissingImports]
        from encourage.rag.base_impl import BaseRAG  # type: ignore[reportMissingImports]

        base_cls = BaseRAG
        config_cls = BaseRAGConfig
        method_label = 'BaseRAG'
    elif method == 'BM25':
        try:
            from encourage.rag.base.config import BM25RAGConfig  # type: ignore[reportMissingImports]
            from encourage.rag.bm25 import BM25RAG  # type: ignore[reportMissingImports]
        except ModuleNotFoundError as exc:
            raise ValueError(
                'RAG method BM25 is unavailable because an optional dependency is missing: '
                f'{exc.name}. Install the missing package in the backend runtime.'
            ) from exc

        base_cls = BM25RAG
        config_cls = BM25RAGConfig
        method_label = 'BM25RAG'
    else:
        try:
            from encourage.rag.base.config import HybridBM25RAGConfig  # type: ignore[reportMissingImports]
            from encourage.rag.hybrid_bm25 import HybridBM25RAG  # type: ignore[reportMissingImports]
        except ModuleNotFoundError as exc:
            raise ValueError(
                'RAG method HybridBM25 is unavailable because an optional dependency is missing: '
                f'{exc.name}. Install the missing package in the backend runtime.'
            ) from exc

        base_cls = HybridBM25RAG
        config_cls = HybridBM25RAGConfig
        method_label = 'HybridBM25RAG'

    class PaddleDocMarkdownRAG(base_cls):  # type: ignore[misc, valid-type]
        def get_embedding_model(self, name: str, device: str = 'cpu') -> Any:
            return DefaultEmbeddingFunction()

    return PaddleDocMarkdownRAG, config_cls, method_label


@lru_cache(maxsize=1)
def _batch_inference_runner_class() -> type:
    encourage_src = _encourage_src_path()
    encourage_src_str = str(encourage_src)
    if encourage_src_str not in sys.path:
        sys.path.insert(0, encourage_src_str)

    from encourage.llm import BatchInferenceRunner  # type: ignore[reportMissingImports]

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


@lru_cache(maxsize=1)
def _configure_encourage_mlflow_tracing() -> None:
    enabled = os.getenv('PADDLEDOC_ENCOURAGE_ENABLE_MLFLOW_TRACING', 'true').strip().lower()
    if enabled not in {'1', 'true', 'yes', 'on'}:
        return

    try:
        import mlflow
        from encourage import enable_mlflow_tracing  # type: ignore[reportMissingImports]

        for key in ('NO_PROXY', 'no_proxy'):
            current = os.getenv(key, '').strip()
            values = [part.strip() for part in current.split(',') if part.strip()]
            for host in ('mlflow', 'mlflow:5000'):
                if host not in values:
                    values.append(host)
            os.environ[key] = ','.join(values)

        tracking_uri = os.getenv('MLFLOW_TRACKING_URI', '').strip()
        experiment_name = os.getenv('MLFLOW_EXPERIMENT_NAME', '').strip()

        if tracking_uri:
            mlflow.set_tracking_uri(tracking_uri)
        if experiment_name:
            mlflow.set_experiment(experiment_name)

        enable_mlflow_tracing()
    except Exception:
        # Tracing must never break request handling.
        return


def ingest_markdown_file(path: Path, *, rag_method: str = 'Base') -> dict[str, Any]:
    loader_cls = _markdown_ingestion_class()
    method_key = _normalize_rag_method_name(rag_method)
    rag_cls, rag_config_cls, method_label = _rag_pipeline_components(method_key)
    source_loader = loader_cls()
    source_documents = source_loader.load(path)
    if not source_documents:
        raise RuntimeError(f'No encourage documents created from markdown file: {path}')

    chunk_max_chars, chunk_overlap_chars = _resolve_chunk_settings()
    documents = load_markdown_chunks(
        path,
        chunk_max_chars=chunk_max_chars,
        chunk_overlap_chars=chunk_overlap_chars,
    )

    if not documents:
        raise RuntimeError(f'Encourage chunking produced no content for markdown file: {path}')

    source_document = source_documents[0]
    pipeline_id = str(uuid.uuid4())
    collection_name = f'encourage-{path.stem}-{pipeline_id[:8]}'
    rag_config = rag_config_cls(
        context_collection=documents,
        collection_name=collection_name,
        embedding_function='default',
        top_k=min(_DEFAULT_TOP_K, len(documents)),
        retrieval_only=False,
        device='cpu',
        template_name='paddledoc_rag.j2',
    )
    rag_pipeline = rag_cls(rag_config)
    _RAG_PIPELINES[pipeline_id] = rag_pipeline
    _RAG_PIPELINE_METADATA[pipeline_id] = {
        'pipeline_id': pipeline_id,
        'collection_name': collection_name,
        'rag_method': method_label,
        'rag_method_key': method_key,
        'top_k': rag_pipeline.top_k,
        'document_count': len(documents),
        'chunk_max_chars': chunk_max_chars,
        'chunk_overlap_chars': chunk_overlap_chars,
        'source_md_path': str(path),
        'source_md_filename': path.name,
    }
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
        'chunk_max_chars': chunk_max_chars,
        'chunk_overlap_chars': chunk_overlap_chars,
        'rag_method': method_label,
        'rag_method_key': method_key,
        'source_md_path': str(path),
        'source_md_filename': path.name,
        'document_ids': [str(doc.id) for doc in documents],
        'document_filenames': [str(doc.meta_data['filename'] or '') for doc in documents],
    }
    collection_dump = {
        'name': collection.name,
        'metadata': collection.metadata,
        'document_count': rag_pipeline.client.count_documents(collection_name),
    }
    document_dump = {
        'id': str(source_document.id),
        'content_preview': source_document.content[:500],
        'content_length': len(source_document.content),
        'meta_data': source_document.meta_data.to_dict(truncated=False),
        'chunk_count': len(documents),
        'chunk_preview': [chunk.content for chunk in documents],
    }

    return {
        'id': str(source_document.id),
        'content': source_document.content,
        'score': source_document.score,
        'distance': source_document.distance,
        'meta_data': source_document.meta_data.to_dict(truncated=False),
        'pipeline_id': pipeline_id,
        'collection_name': collection_name,
        'document_count': len(documents),
        'top_k': rag_pipeline.top_k,
        'rag_method': method_label,
        'ready': True,
        'config': config_dump,
        'collection': collection_dump,
        'document_dump': document_dump,
    }


def get_pipeline_metadata(pipeline_id: str) -> dict[str, Any] | None:
    metadata = _RAG_PIPELINE_METADATA.get(pipeline_id)
    return dict(metadata) if metadata else None


def get_pipeline_rag(pipeline_id: str) -> Any | None:
    return _RAG_PIPELINES.get(pipeline_id)


def _resolve_chunk_settings(
    *,
    chunk_max_chars: int | None = None,
    chunk_overlap_chars: int | None = None,
) -> tuple[int, int]:
    if chunk_max_chars is None:
        chunk_max_chars_env = os.getenv(
            'PADDLEDOC_ENCOURAGE_CHUNK_MAX_CHARS',
            str(_DEFAULT_CHUNK_MAX_CHARS),
        ).strip()
        try:
            resolved_chunk_max_chars = max(200, int(chunk_max_chars_env))
        except ValueError:
            resolved_chunk_max_chars = _DEFAULT_CHUNK_MAX_CHARS
    else:
        resolved_chunk_max_chars = max(200, int(chunk_max_chars))

    if chunk_overlap_chars is None:
        chunk_overlap_chars_env = os.getenv(
            'PADDLEDOC_ENCOURAGE_CHUNK_OVERLAP_CHARS',
            str(_DEFAULT_CHUNK_OVERLAP_CHARS),
        ).strip()
        try:
            resolved_chunk_overlap_chars = max(0, int(chunk_overlap_chars_env))
        except ValueError:
            resolved_chunk_overlap_chars = _DEFAULT_CHUNK_OVERLAP_CHARS
    else:
        resolved_chunk_overlap_chars = max(0, int(chunk_overlap_chars))

    return resolved_chunk_max_chars, resolved_chunk_overlap_chars


def load_markdown_chunks(
    path: Path,
    *,
    chunk_max_chars: int | None = None,
    chunk_overlap_chars: int | None = None,
) -> list[Any]:
    loader_cls = _markdown_ingestion_class()
    resolved_chunk_max_chars, resolved_chunk_overlap_chars = _resolve_chunk_settings(
        chunk_max_chars=chunk_max_chars,
        chunk_overlap_chars=chunk_overlap_chars,
    )
    loader = loader_cls(
        chunk_documents=True,
        chunk_max_chars=resolved_chunk_max_chars,
        chunk_overlap_chars=resolved_chunk_overlap_chars,
    )
    documents = loader.load(path)
    if not documents:
        raise RuntimeError(f'Encourage chunking produced no content for markdown file: {path}')
    return documents


def _query_collection(collection_name: str, query: str, *, top_k: int) -> list[Any]:
    encourage_src = _encourage_src_path()
    encourage_src_str = str(encourage_src)
    if encourage_src_str not in sys.path:
        sys.path.insert(0, encourage_src_str)

    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    from encourage.vector_store.chroma import ChromaClient  # type: ignore[reportMissingImports]

    query_text = query.strip()
    if not query_text:
        raise ValueError('Query must not be empty')

    client = ChromaClient()
    return client.query(
        collection_name=collection_name,
        query=[query_text],
        top_k=max(1, int(top_k)),
        embedding_function=DefaultEmbeddingFunction(),
        batch_size=1,
    )[0]


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
    _configure_encourage_mlflow_tracing()

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


def retrieve_from_pipeline(
    pipeline_id: str,
    query: str,
    *,
    collection_name: str | None = None,
    top_k: int | None = None,
) -> dict[str, Any]:
    rag_pipeline = _RAG_PIPELINES.get(pipeline_id)

    query_text = query.strip()
    if not query_text:
        raise ValueError('Query must not be empty')

    if rag_pipeline is not None:
        documents = rag_pipeline.retrieve_contexts([query_text])[0]
        resolved_collection_name = rag_pipeline.collection_name
        resolved_top_k = rag_pipeline.top_k
    else:
        if not collection_name:
            raise KeyError(f'Encourage pipeline not found: {pipeline_id}')
        resolved_top_k = max(1, int(top_k or _DEFAULT_TOP_K))
        documents = _query_collection(collection_name, query_text, top_k=resolved_top_k)
        resolved_collection_name = collection_name

    return {
        'pipeline_id': pipeline_id,
        'collection_name': resolved_collection_name,
        'query': query_text,
        'top_k': resolved_top_k,
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