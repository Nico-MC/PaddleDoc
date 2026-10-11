"""Create the retrieval-safe view of stored Markdown without mutating it."""

from __future__ import annotations

import re


_PICTURE_TEXT_BLOCK = re.compile(
    r'<!--[ \t]*Start of picture text[ \t]*-->(?P<body>.*?)'
    r'<!--[ \t]*End of picture text[ \t]*-->',
    re.IGNORECASE | re.DOTALL,
)
_PAGE_MARKER = re.compile(r'<!--[ \t]*page:\s*\d+\s*/\s*\d+\s*-->', re.IGNORECASE)


def prepare_rag_text(markdown: str) -> str:
    """Remove complete picture-text blocks before retrieval or dataset use.

    The persisted Markdown is intentionally never changed. Page markers that
    happen to be nested in a picture-text block are carried into the cleaned
    view so page-based retrieval and dataset provenance remain intact.
    """

    def replace_picture_block(match: re.Match[str]) -> str:
        page_markers = _PAGE_MARKER.findall(match.group('body'))
        return '\n'.join(page_markers)

    return _PICTURE_TEXT_BLOCK.sub(replace_picture_block, markdown)


def count_picture_text_blocks(markdown: str) -> int:
    """Return how many complete graphic-text blocks are excluded from RAG."""

    return sum(1 for _ in _PICTURE_TEXT_BLOCK.finditer(markdown))
