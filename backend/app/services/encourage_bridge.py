from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path
from typing import Any
import uuid


_RAG_PIPELINES: dict[str, Any] = {}


def _encourage_src_path() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    encourage_src = repo_root.parent / 'encourage' / 'src'
    if not encourage_src.exists():
        raise RuntimeError(f'Encourage source path not found: {encourage_src}')
    return encourage_src


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
        retrieval_only=True,
        device='cpu',
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