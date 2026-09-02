#!/usr/bin/env python3
"""Compare the legacy, semantic-v2, and Pandoc DOCX converters.

The command is diagnostic only: it never changes PaddleDoc jobs or selects a
converter for the worker.  With ``--output-dir`` it writes both Markdown
variants and a unified diff so a document can be reviewed side by side.
"""

from __future__ import annotations

import argparse
from collections import Counter
import difflib
import json
from pathlib import Path
import re

from app.services.docx_semantic import semantic_docx_to_markdown
from app.services.docx_pandoc import pandoc_docx_to_markdown
from app.services.paddle_service import _fallback_docx_to_markdown


_WORD_RE = re.compile(r"[\wÄÖÜäöüß]+", re.UNICODE)
_HEADING_RE = re.compile(r'^#{1,6}\s+', re.MULTILINE)
_LIST_RE = re.compile(r'^\s*(?:[-*+] |\d+[.)]\s+)', re.MULTILINE)


def _tokens(markdown: str) -> Counter[str]:
    return Counter(token.casefold() for token in _WORD_RE.findall(markdown))


def _coverage(source: Counter[str], candidate: Counter[str]) -> float:
    total = sum(source.values())
    if total == 0:
        return 1.0
    shared = sum((source & candidate).values())
    return round(shared / total, 4)


def _metrics(markdown: str) -> dict[str, int]:
    return {
        'characters': len(markdown),
        'lines': len(markdown.splitlines()),
        'headings': len(_HEADING_RE.findall(markdown)),
        'list_items': len(_LIST_RE.findall(markdown)),
        'table_rows': sum(1 for line in markdown.splitlines() if line.strip().startswith('|')),
    }


def compare(source: Path) -> tuple[dict[str, object], str, str, str]:
    legacy_markdown, legacy_paragraph_count = _fallback_docx_to_markdown(source)
    semantic_markdown, document = semantic_docx_to_markdown(source)
    pandoc_markdown, pandoc_details = pandoc_docx_to_markdown(source)
    legacy_tokens = _tokens(legacy_markdown)
    semantic_tokens = _tokens(semantic_markdown)
    pandoc_tokens = _tokens(pandoc_markdown)
    result: dict[str, object] = {
        'source': str(source),
        'legacy': {
            **_metrics(legacy_markdown),
            'paragraphs': legacy_paragraph_count,
        },
        'semantic_v2': {
            **_metrics(semantic_markdown),
            **document.statistics(),
        },
        'pandoc': {
            **_metrics(pandoc_markdown),
            **pandoc_details,
        },
        # Directional token coverage is not a quality score.  It is a quick
        # signal for text that one converter dropped or duplicated and tells
        # reviewers where a manual diff is most valuable.
        'token_coverage': {
            'legacy_found_in_semantic_v2': _coverage(legacy_tokens, semantic_tokens),
            'semantic_v2_found_in_legacy': _coverage(semantic_tokens, legacy_tokens),
            'legacy_found_in_pandoc': _coverage(legacy_tokens, pandoc_tokens),
            'pandoc_found_in_legacy': _coverage(pandoc_tokens, legacy_tokens),
            'semantic_v2_found_in_pandoc': _coverage(semantic_tokens, pandoc_tokens),
            'pandoc_found_in_semantic_v2': _coverage(pandoc_tokens, semantic_tokens),
        },
    }
    return result, legacy_markdown, semantic_markdown, pandoc_markdown


def _documents(inputs: list[Path]) -> list[Path]:
    documents: list[Path] = []
    for candidate in inputs:
        if candidate.is_dir():
            documents.extend(
                path
                for path in sorted(candidate.glob('*.docx'))
                if not path.name.startswith('._')
            )
        elif candidate.is_file() and candidate.suffix.casefold() == '.docx':
            documents.append(candidate)
    return documents


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        'inputs',
        nargs='*',
        type=Path,
        default=[Path('/app/docs/TypischeDokumente')],
        help='DOCX files or directories containing DOCX files.',
    )
    parser.add_argument(
        '--output-dir',
        type=Path,
        help='Optionally write legacy, semantic-v2 and unified-diff files.',
    )
    args = parser.parse_args()
    documents = _documents([path.expanduser().resolve() for path in args.inputs])
    if not documents:
        parser.error('No DOCX files found')

    if args.output_dir:
        args.output_dir.mkdir(parents=True, exist_ok=True)

    for source in documents:
        result, legacy, semantic, pandoc = compare(source)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        if not args.output_dir:
            continue
        stem = source.stem
        (args.output_dir / f'{stem}.legacy.md').write_text(legacy, encoding='utf-8')
        (args.output_dir / f'{stem}.semantic-v2.md').write_text(semantic, encoding='utf-8')
        (args.output_dir / f'{stem}.pandoc.md').write_text(pandoc, encoding='utf-8')
        semantic_diff = ''.join(
            difflib.unified_diff(
                legacy.splitlines(keepends=True),
                semantic.splitlines(keepends=True),
                fromfile=f'{source.name}:legacy',
                tofile=f'{source.name}:semantic-v2',
            )
        )
        (args.output_dir / f'{stem}.semantic-v2.diff').write_text(
            semantic_diff,
            encoding='utf-8',
        )
        pandoc_diff = ''.join(
            difflib.unified_diff(
                legacy.splitlines(keepends=True),
                pandoc.splitlines(keepends=True),
                fromfile=f'{source.name}:legacy',
                tofile=f'{source.name}:pandoc',
            )
        )
        (args.output_dir / f'{stem}.pandoc.diff').write_text(pandoc_diff, encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
