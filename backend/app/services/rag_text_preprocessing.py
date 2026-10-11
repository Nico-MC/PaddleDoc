"""Create the retrieval-safe view of stored Markdown without mutating it."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass


_PICTURE_TEXT_BLOCK = re.compile(
    r'<!--[ \t]*Start of picture text[ \t]*-->(?P<body>.*?)'
    r'<!--[ \t]*End of picture text[ \t]*-->',
    re.IGNORECASE | re.DOTALL,
)
_PAGE_MARKER = re.compile(r'<!--[ \t]*page:\s*\d+\s*/\s*\d+\s*-->', re.IGNORECASE)
_LEGACY_PAGE_HEADING = re.compile(r'^#{1,6}[ \t]+Page[ \t]+\d+[ \t]*$', re.IGNORECASE | re.MULTILINE)

_PAGE_NUMBER = re.compile(
    r'^(?:seite\s+)?\d{1,4}(?:\s*(?:/|von|of)\s*\d{1,4})?$', re.IGNORECASE,
)
_PAGE_NUMBER_WITH_LABEL = re.compile(
    r'^(?:seite|page|s\.)\s+\d{1,4}(?:\s*(?:/|von|of)\s*\d{1,4})?$', re.IGNORECASE,
)
_MARKDOWN_HEADING = re.compile(r'^#{1,6}\s+')
_EDGE_LINES = 2


@dataclass(frozen=True)
class _EdgeLine:
    page: int
    line: int
    edge: str
    key: str


def _edge_key(line: str, page_index: int) -> str | None:
    """Only short, plain lines can be recurring page furniture."""

    raw = line.lstrip()
    if raw.startswith(('|', '![', '```', '~~~', '<', '- ', '* ', '+ ', '>')):
        return None
    value = _MARKDOWN_HEADING.sub('', line.strip()).strip(' *_`')
    value = ' '.join(value.split())
    if not value or len(value) > 120 or len(value.split()) > 16:
        return None
    if _PAGE_NUMBER.fullmatch(value) or _PAGE_NUMBER_WITH_LABEL.fullmatch(value):
        # A running page counter advances with the PDF page, unlike a repeated
        # numeric measurement at the bottom of a table.
        number = int(re.search(r'\d+', value).group())
        return f'<page-number-offset:{number - page_index}>'
    if value.endswith(('.', ':', ';', '!', '?')):
        return None
    # Do not merge arbitrary numbers: dates and versions can be subject matter.
    return value.casefold()


def _page_matches(markdown: str) -> list[re.Match[str]]:
    markers = list(_PAGE_MARKER.finditer(markdown))
    if markers:
        return markers
    # Older PDF jobs used Markdown page headings instead of HTML markers.
    headings = list(_LEGACY_PAGE_HEADING.finditer(markdown))
    return headings if len(headings) >= 3 else []


def _page_slices(markdown: str) -> list[tuple[int, int]]:
    markers = _page_matches(markdown)
    return [
        (marker.end(), markers[index + 1].start() if index + 1 < len(markers) else len(markdown))
        for index, marker in enumerate(markers)
    ]


def _recurring_edge_lines(markdown: str) -> set[tuple[int, int]]:
    """Identify repeated top/bottom lines without document-specific vocabulary.

    Require either broad repetition or a dense run of three nearby pages. This
    also handles odd/even page headers while avoiding isolated body headings.
    """

    pages = _page_slices(markdown)
    if len(pages) < 3:
        return set()
    occurrences: dict[tuple[str, str], list[_EdgeLine]] = defaultdict(list)
    for page_number, (start, end) in enumerate(pages):
        lines = markdown[start:end].splitlines(keepends=True)
        nonempty = [index for index, line in enumerate(lines) if line.strip()]
        for edge, indices in (
            ('top', nonempty[:_EDGE_LINES]),
            ('bottom', nonempty[-_EDGE_LINES:]),
        ):
            for index in indices:
                key = _edge_key(lines[index], page_number)
                if key is not None:
                    occurrences[(edge, key)].append(_EdgeLine(page_number, index, edge, key))

    selected: set[tuple[int, int]] = set()
    for matches in occurrences.values():
        page_numbers = sorted({match.page for match in matches})
        if len(page_numbers) < 3:
            continue
        global_repeat = len(page_numbers) >= max(3, (len(pages) + 4) // 5)
        local_repeat = any(
            page_numbers[index + 2] - page_numbers[index] <= 8
            for index in range(len(page_numbers) - 2)
        )
        if global_repeat or local_repeat:
            selected.update((match.page, match.line) for match in matches)
    return selected


def remove_recurring_page_furniture(markdown: str) -> tuple[str, list[dict[str, object]]]:
    """Remove detected running headers/footers, preserving page markers.

    The details are suitable for a dataset audit. Documents without page
    markers are left unchanged because their page edges cannot be inferred.
    """

    selected = _recurring_edge_lines(markdown)
    if not selected:
        return markdown, []
    pages = _page_slices(markdown)
    page_numbers = [
        int(re.search(r'page(?::|\s+)\s*(\d+)', marker.group(), re.IGNORECASE).group(1))
        for marker in _page_matches(markdown)
    ]

    result: list[str] = []
    removed: list[dict[str, object]] = []
    cursor = 0
    for page_number, (start, end) in enumerate(pages):
        result.append(markdown[cursor:start])
        for line_number, line in enumerate(markdown[start:end].splitlines(keepends=True)):
            if (page_number, line_number) in selected:
                removed.append({'page': page_numbers[page_number], 'text': line.strip()})
            else:
                result.append(line)
        cursor = end
    result.append(markdown[cursor:])
    return ''.join(result), removed

def prepare_rag_text_with_report(markdown: str) -> tuple[str, dict[str, object]]:
    """Remove picture text and recurring page furniture before downstream use.

    The persisted Markdown is intentionally never changed. Page markers that
    happen to be nested in a picture-text block are carried into the cleaned
    view so page-based retrieval and dataset provenance remain intact.
    """

    picture_blocks_removed = count_picture_text_blocks(markdown)

    def replace_picture_block(match: re.Match[str]) -> str:
        page_markers = _PAGE_MARKER.findall(match.group('body'))
        return '\n'.join(page_markers)

    without_picture_text = _PICTURE_TEXT_BLOCK.sub(replace_picture_block, markdown)
    cleaned, removed = remove_recurring_page_furniture(without_picture_text)
    return cleaned, {
        'picture_text_blocks_removed': picture_blocks_removed,
        'page_furniture_lines_removed': len(removed),
        'page_furniture_removed': removed,
    }


def prepare_rag_text(markdown: str) -> str:
    """Return the shared retrieval/dataset view without changing the source."""

    return prepare_rag_text_with_report(markdown)[0]


def count_picture_text_blocks(markdown: str) -> int:
    """Return how many complete graphic-text blocks are excluded from RAG."""

    return sum(1 for _ in _PICTURE_TEXT_BLOCK.finditer(markdown))
