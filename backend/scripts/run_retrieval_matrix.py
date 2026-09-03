#!/usr/bin/env python3
"""Run a reproducible PaddleDoc/Encourage retrieval comparison matrix."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--markdown', type=Path, required=True)
    parser.add_argument('--dataset', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--recall-k', type=int, default=3)
    parser.add_argument(
        '--embeddings',
        nargs='+',
        default=['default', 'multilingual-e5-base'],
    )
    parser.add_argument(
        '--methods',
        nargs='+',
        default=['Base', 'BM25', 'HybridBM25'],
    )
    return parser.parse_args()


def _compact_result(
    *,
    embedding: str,
    method: str,
    ingestion: dict[str, Any],
    evaluation: dict[str, Any],
) -> dict[str, Any]:
    per_question = evaluation['per_question_results']
    return {
        'embedding_model': embedding,
        'rag_method': method,
        'pipeline_rag_method': ingestion['rag_method'],
        'document_count': ingestion['document_count'],
        'top_k': evaluation['top_k'],
        'recall_k': evaluation['recall_k'],
        'evaluated_question_count': evaluation['evaluated_question_count'],
        'metrics': evaluation['retrieval_metrics'],
        'summary': evaluation['evaluation_summary'],
        'questions': [
            {
                'id': item['id'],
                'question': item['question'],
                'first_hit_rank': item['first_hit_rank'],
                'reference_selection': item['reference_selection'],
                'top_results': [
                    {
                        'rank': result['rank'],
                        'is_reference_match': result['is_reference_match'],
                        'score': result['score'],
                        'content_preview': result['content_preview'],
                    }
                    for result in item['retrieved_documents']
                ],
            }
            for item in per_question
        ],
    }


def main() -> int:
    args = _parse_args()
    app_root = Path('/app')
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))

    from app.services.encourage_bridge import ingest_markdown_file
    from app.services.encourage_evaluation import run_encourage_evaluation

    markdown = args.markdown.resolve()
    if not markdown.is_file():
        raise FileNotFoundError(markdown)

    output: dict[str, Any] = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'markdown': str(markdown),
        'dataset': args.dataset,
        'recall_k': args.recall_k,
        'runs': [],
    }
    total = len(args.embeddings) * len(args.methods)
    run_number = 0
    for embedding in args.embeddings:
        for method in args.methods:
            run_number += 1
            print(
                f'[{run_number}/{total}] embedding={embedding} method={method}',
                flush=True,
            )
            ingestion = ingest_markdown_file(
                markdown,
                rag_method=method,
                include_frontmatter=False,
                embedding_model=embedding,
            )
            evaluation = run_encourage_evaluation(
                pipeline_id=ingestion['pipeline_id'],
                dataset_path=args.dataset,
                recall_k=args.recall_k,
                evaluation_mode='standard',
            )
            output['runs'].append(
                _compact_result(
                    embedding=embedding,
                    method=method,
                    ingestion=ingestion,
                    evaluation=evaluation,
                )
            )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + '\n',
        encoding='utf-8',
    )
    print(f'Results written to {args.output}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
