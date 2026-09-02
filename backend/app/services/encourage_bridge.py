from __future__ import annotations

import os
import re
import sys
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar


_RAG_PIPELINES: dict[str, Any] = {}
_RAG_PIPELINE_METADATA: dict[str, dict[str, Any]] = {}
_DEFAULT_SYSTEM_PROMPT = (
    'Du bist ein hilfreicher Assistent. Beantworte die Frage nur auf Basis des bereitgestellten Kontexts. '
    'Wenn die Antwort aus dem Kontext direkt hervorgeht oder durch offensichtliche Schreibvarianten klar belegbar ist, '
    'beantworte sie knapp und praezise. Wenn die Information im Kontext wirklich nicht enthalten ist, sage das klar.'
)
_DEFAULT_TOP_K = 5
_DEFAULT_CHUNK_MAX_CHARS = 1100
_DEFAULT_CHUNK_OVERLAP_CHARS = 140
_SUPPORTED_RAG_METHODS: tuple[str, ...] = ('Base', 'BM25', 'HybridBM25')
_SUPPORTED_EMBEDDING_MODELS: tuple[str, ...] = ('default', 'multilingual-e5-base')
_ATX_HEADING_RE = re.compile(r'^(#{1,6})\s+\S')
_MARKDOWN_TABLE_DELIMITER_RE = re.compile(r'^\|(?:\s*:?-{3,}:?\s*\|)+$')


def _table_first_cell_is_blank(line: str) -> bool:
    cells = line.split('|')
    return len(cells) > 2 and not cells[1].strip()


def _split_oversized_markdown_table(block: str, *, max_chars: int) -> list[str] | None:
    """Split a GFM table by rows while repeating its header rows."""

    lines = block.splitlines()
    if len(lines) < 3 or not _MARKDOWN_TABLE_DELIMITER_RE.fullmatch(lines[1].strip()):
        return None

    header_end = 2
    while header_end < len(lines) and _table_first_cell_is_blank(lines[header_end]):
        header_end += 1
    header = lines[:header_end]
    data_rows = lines[header_end:]
    if not data_rows or len('\n'.join(header)) >= max_chars:
        return None

    chunks: list[str] = []
    current_rows: list[str] = []
    for row in data_rows:
        candidate_rows = [*current_rows, row]
        candidate = '\n'.join([*header, *candidate_rows])
        if current_rows and len(candidate) > max_chars:
            chunks.append('\n'.join([*header, *current_rows]))
            current_rows = [row]
        else:
            current_rows = candidate_rows
    if current_rows:
        chunks.append('\n'.join([*header, *current_rows]))
    return chunks


def _split_oversized_markdown_block(
    block: str,
    *,
    max_chars: int,
    overlap_chars: int,
) -> list[str]:
    """Split a single oversized block without cutting through words."""

    if len(block) <= max_chars:
        return [block]
    parts: list[str] = []
    start = 0
    while start < len(block):
        limit = min(len(block), start + max_chars)
        end = limit
        if limit < len(block):
            boundary = block.rfind(' ', start, limit + 1)
            if boundary > start:
                end = boundary
        part = block[start:end].strip()
        if part:
            parts.append(part)
        if end >= len(block):
            break
        next_start = max(start + 1, end - overlap_chars)
        while next_start < end and not block[next_start].isspace():
            next_start += 1
        start = next_start
    return parts


def _chunk_markdown_by_sections(
    text: str,
    *,
    max_chars: int,
    overlap_chars: int,
) -> list[str]:
    """Create retrieval chunks that retain their Markdown heading path."""

    normalized = text.replace('\r\n', '\n').strip()
    if not normalized:
        return []
    blocks = [block.strip() for block in re.split(r'\n{2,}', normalized) if block.strip()]
    chunks: list[str] = []
    heading_stack: dict[int, str] = {}
    active_prefix = ''
    content_blocks: list[str] = []

    def emit() -> None:
        nonlocal content_blocks
        if not content_blocks:
            return
        prefix_length = len(active_prefix) + 2 if active_prefix else 0
        content_limit = max(80, max_chars - prefix_length)
        current: list[str] = []

        def flush_current() -> None:
            if not current:
                return
            body = '\n\n'.join(current)
            chunks.append(f'{active_prefix}\n\n{body}'.strip() if active_prefix else body)
            current.clear()

        for block in content_blocks:
            candidate = '\n\n'.join([*current, block])
            if current and len(candidate) > content_limit:
                flush_current()
            if len(block) <= content_limit:
                current.append(block)
                continue
            flush_current()
            table_parts = _split_oversized_markdown_table(block, max_chars=content_limit)
            if table_parts is not None:
                chunks.extend(
                    f'{active_prefix}\n\n{part}'.strip() if active_prefix else part
                    for part in table_parts
                )
                continue
            for part in _split_oversized_markdown_block(
                block,
                max_chars=content_limit,
                overlap_chars=min(overlap_chars, content_limit // 2),
            ):
                chunks.append(
                    f'{active_prefix}\n\n{part}'.strip() if active_prefix else part
                )
        flush_current()
        content_blocks = []

    for block in blocks:
        heading = _ATX_HEADING_RE.match(block)
        if not heading:
            content_blocks.append(block)
            continue
        emit()
        level = len(heading.group(1))
        for deeper in [candidate for candidate in heading_stack if candidate >= level]:
            del heading_stack[deeper]
        heading_stack[level] = block
        active_prefix = '\n\n'.join(heading_stack[key] for key in sorted(heading_stack))
    emit()

    # A heading-only document still needs to remain indexable.
    if not chunks and active_prefix:
        chunks.append(active_prefix)
    return chunks


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


def _ensure_namespace_package(name: str, path: Path) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing

    module = ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = name
    module.__path__ = [str(path)]  # type: ignore[attr-defined]
    sys.modules[name] = module
    return module


@lru_cache(maxsize=1)
def prepare_encourage_runtime() -> None:
    """Load Encourage as a lean source checkout integration.

    Encourage's package initializers eagerly import optional reranker,
    sentence-transformer, Qdrant and MLflow integrations. PaddleDoc uses the
    Chroma, BM25, prompting and retrieval-metric modules only, so importing
    every optional backend would unnecessarily pull CUDA/PyTorch into the API
    image. Namespace packages let Python load the selected Encourage modules
    directly while retaining Encourage's own implementations.
    """

    encourage_src = _encourage_src_path()
    encourage_src_str = str(encourage_src)
    if encourage_src_str not in sys.path:
        sys.path.insert(0, encourage_src_str)

    package_root = encourage_src / 'encourage'
    _ensure_namespace_package('encourage', package_root)
    _ensure_namespace_package('encourage.rag', package_root / 'rag')
    _ensure_namespace_package('encourage.metrics', package_root / 'metrics')
    vector_store_package = _ensure_namespace_package(
        'encourage.vector_store',
        package_root / 'vector_store',
    )

    # BaseRAG imports these symbols from the package instead of their modules.
    # Populate just the Chroma implementation and the interface; Qdrant stays
    # an optional Encourage concern and does not belong in this API image.
    from encourage.vector_store.chroma import ChromaClient  # type: ignore[reportMissingImports]
    from encourage.vector_store.vector_store import VectorStore  # type: ignore[reportMissingImports]

    vector_store_package.ChromaClient = ChromaClient  # type: ignore[attr-defined]
    vector_store_package.VectorStore = VectorStore  # type: ignore[attr-defined]


@lru_cache(maxsize=1)
def _markdown_ingestion_class() -> type:
    prepare_encourage_runtime()

    try:
        from encourage.utils.markdown_ingestion import MarkdownIngestion  # type: ignore[reportMissingImports]

        return MarkdownIngestion
    except ModuleNotFoundError:
        # Some runtime environments have an older encourage package without
        # encourage.utils.markdown_ingestion. Keep PaddleDoc functional by
        # falling back to a local, minimal markdown ingester.
        from encourage.prompts.context import Document  # type: ignore[reportMissingImports]
        from encourage.prompts.meta_data import MetaData  # type: ignore[reportMissingImports]

        class MarkdownIngestion:  # noqa: D401
            """Minimal fallback markdown ingestion for PaddleDoc runtimes."""

            def __init__(
                self,
                *,
                chunk_documents: bool = False,
                chunk_max_chars: int = 1200,
                chunk_overlap_chars: int = 150,
                include_frontmatter: bool = False,
                encoding: str = 'utf-8',
                **_: Any,
            ) -> None:
                self.chunk_documents = chunk_documents
                self.chunk_max_chars = max(200, int(chunk_max_chars))
                self.chunk_overlap_chars = max(
                    0,
                    min(int(chunk_overlap_chars), self.chunk_max_chars // 2),
                )
                self.include_frontmatter = include_frontmatter
                self.encoding = encoding

            def load(self, path: str | Path) -> list[Any]:
                file_path = Path(path).expanduser().resolve()
                if not file_path.exists() or not file_path.is_file():
                    raise FileNotFoundError(f'File not found: {file_path}')

                raw = file_path.read_text(encoding=self.encoding)
                frontmatter, content = self._split_frontmatter(raw)
                base_doc = Document(
                    id=uuid.uuid5(uuid.NAMESPACE_URL, str(file_path)),
                    content=content,
                    meta_data=MetaData(
                        tags={
                            'source': 'markdown',
                            'loader': 'PaddleDocFallbackMarkdownIngestion',
                            'filename': file_path.name,
                            'filepath': str(file_path),
                        }
                    ),
                )

                if not self.chunk_documents:
                    return [base_doc]

                chunk_texts = self._chunk_text(content)
                if self.include_frontmatter and frontmatter:
                    chunk_texts.insert(0, f'# Dokumentmetadaten\n\n{frontmatter}')
                if len(chunk_texts) <= 1:
                    return [base_doc]

                chunked_docs: list[Any] = []
                for idx, chunk_text in enumerate(chunk_texts):
                    chunked_docs.append(
                        Document(
                            id=uuid.uuid5(uuid.NAMESPACE_URL, f'{file_path}#{idx}'),
                            content=chunk_text,
                            meta_data=MetaData(
                                tags={
                                    'source': 'markdown',
                                    'loader': 'PaddleDocFallbackMarkdownIngestion',
                                    'filename': file_path.name,
                                    'filepath': str(file_path),
                                    'chunk_index': idx,
                                    'chunk_count': len(chunk_texts),
                                }
                            ),
                        )
                    )
                return chunked_docs

            @staticmethod
            def _split_frontmatter(text: str) -> tuple[str, str]:
                if not text.startswith('---\n'):
                    return '', text
                end = text.find('\n---\n', 4)
                if end == -1:
                    return '', text
                return text[4:end].strip(), text[end + 5 :].lstrip('\n')

            def _chunk_text(self, text: str) -> list[str]:
                return _chunk_markdown_by_sections(
                    text,
                    max_chars=self.chunk_max_chars,
                    overlap_chars=self.chunk_overlap_chars,
                )

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
class _MultilingualE5EmbeddingFunction:
    """Chroma embedding adapter for asymmetric multilingual E5 retrieval."""

    model_name: ClassVar[str] = 'intfloat/multilingual-e5-base'

    def __init__(self, *, device: str = 'cpu') -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                'multilingual-e5-base requires sentence-transformers in the backend runtime.'
            ) from exc
        self._model = SentenceTransformer(self.model_name, device=device)

    def _encode(self, values: list[str], *, prefix: str) -> list[list[float]]:
        embeddings = self._model.encode(
            [f'{prefix}{value}' for value in values],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.tolist()

    def __call__(self, input: list[str]) -> list[list[float]]:
        return self._encode(input, prefix='passage: ')

    def embed_query(self, input: list[str]) -> list[list[float]]:
        return self._encode(input, prefix='query: ')

    @staticmethod
    def name() -> str:
        return 'multilingual-e5-base'

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> '_MultilingualE5EmbeddingFunction':
        return _MultilingualE5EmbeddingFunction(device=str(config.get('device', 'cpu')))

    def get_config(self) -> dict[str, str]:
        return {'device': str(getattr(self._model, 'device', 'cpu'))}


def _normalize_embedding_model_name(embedding_model: str | None) -> str:
    normalized = (embedding_model or 'default').strip().lower()
    if normalized not in _SUPPORTED_EMBEDDING_MODELS:
        supported = ', '.join(_SUPPORTED_EMBEDDING_MODELS)
        raise ValueError(f'Unsupported embedding_model: {embedding_model!r}. Supported models: {supported}')
    return normalized


@lru_cache(maxsize=None)
def _rag_pipeline_components(rag_method: str, embedding_model: str = 'default') -> tuple[type, type, str]:
    prepare_encourage_runtime()

    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

    resolved_embedding_model = _normalize_embedding_model_name(embedding_model)

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
            if resolved_embedding_model == 'multilingual-e5-base':
                return _MultilingualE5EmbeddingFunction(device=device)
            return DefaultEmbeddingFunction()

    return PaddleDocMarkdownRAG, config_cls, method_label


@lru_cache(maxsize=1)
def _batch_inference_runner_class() -> type:
    prepare_encourage_runtime()

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


def ingest_markdown_file(
    path: Path,
    *,
    rag_method: str = 'Base',
    include_frontmatter: bool = False,
    embedding_model: str = 'default',
) -> dict[str, Any]:
    loader_cls = _markdown_ingestion_class()
    method_key = _normalize_rag_method_name(rag_method)
    resolved_embedding_model = _normalize_embedding_model_name(embedding_model)
    rag_cls, rag_config_cls, method_label = _rag_pipeline_components(
        method_key,
        resolved_embedding_model,
    )
    source_loader = loader_cls()
    source_documents = source_loader.load(path)
    if not source_documents:
        raise RuntimeError(f'No encourage documents created from markdown file: {path}')

    chunk_max_chars, chunk_overlap_chars = _resolve_chunk_settings()
    documents = load_markdown_chunks(
        path,
        chunk_max_chars=chunk_max_chars,
        chunk_overlap_chars=chunk_overlap_chars,
        include_frontmatter=include_frontmatter,
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
        'include_frontmatter': include_frontmatter,
        'embedding_model': resolved_embedding_model,
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
        'include_frontmatter': include_frontmatter,
        'embedding_model': resolved_embedding_model,
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
    include_frontmatter: bool = False,
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
        include_frontmatter=include_frontmatter,
    )
    documents = loader.load(path)
    if not documents:
        raise RuntimeError(f'Encourage chunking produced no content for markdown file: {path}')
    return documents


def _query_collection(collection_name: str, query: str, *, top_k: int) -> list[Any]:
    prepare_encourage_runtime()

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

    runner = create_llm_runner(
        api_base_url=api_base_url,
        api_key=api_key_clean,
        model_name=model_name,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        max_workers=1,
        batch_size=1,
    )
    model_name_clean = str(getattr(runner, 'model_name', model_name or 'gpt-4o-mini'))

    raw_output = ''
    try:
        retrieved_docs = rag_pipeline.retrieve_contexts([query_text])[0]
        context_snippets = [doc.content.strip() for doc in retrieved_docs if getattr(doc, 'content', '').strip()]
        context_block = '\n\n'.join(context_snippets[:5])

        raw_system_prompt = (
            'Du bist ein hilfreicher Assistent. Gib die bestmoegliche direkte Antwort auf die Frage. '
            'Wenn der bereitgestellte Kontext nicht reicht, darfst du eine vorsichtige Antwort geben und '
            'Unsicherheit transparent markieren.'
        )
        if context_block:
            raw_user_prompt = (
                f'Frage:\n{query_text}\n\n'
                f'Kontext (optional):\n{context_block}\n\n'
                'Antworte knapp und konkret auf Deutsch.'
            )
        else:
            raw_user_prompt = f'Frage:\n{query_text}\n\nAntworte knapp und konkret auf Deutsch.'

        raw_completion = runner.client.chat.completions.create(
            model=model_name_clean,
            messages=[
                {'role': 'system', 'content': raw_system_prompt},
                {'role': 'user', 'content': raw_user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            seed=getattr(runner.sampling_parameters, 'seed', None),
        )
        raw_output = str(raw_completion.choices[0].message.content or '').strip()
    except Exception:
        # Raw output is a debug artifact; never fail the main generation path.
        raw_output = ''

    responses = rag_pipeline.run(
        runner=runner,
        sys_prompt=system_prompt,
        user_prompts=[query_text],
        retrieval_queries=[query_text],
    )

    answer = ''
    if len(responses) > 0:
        first = responses[0].response
        constrained_output = first if isinstance(first, str) else str(first)
        answer = constrained_output.strip()

    if not raw_output:
        raw_output = answer

    return {
        'query': query_text,
        'model_name': model_name_clean,
        'answer': answer,
        'raw_output': raw_output,
    }


def create_llm_runner(
    *,
    api_base_url: str,
    api_key: str,
    model_name: str | None = None,
    max_tokens: int = 512,
    temperature: float = 0.1,
    top_p: float = 1.0,
    max_workers: int = 2,
    batch_size: int = 4,
) -> Any:
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
    return runner_cls(
        sampling_parameters=sampling_params,
        model_name=model_name_clean,
        base_url=api_base,
        env_var_name=env_var_name,
        max_workers=max(1, int(max_workers)),
        batch_size=max(1, int(batch_size)),
    )


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
