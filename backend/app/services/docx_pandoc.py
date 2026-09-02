"""DOCX conversion through Pandoc's structured JSON document model.

Pandoc owns the difficult OOXML parsing. PaddleDoc interprets the resulting
JSON AST and renders Markdown itself. Keeping the two phases separate avoids
leaking Word layout details (deep heading levels, indentation blockquotes and
HTML tables) into the document that is later used for RAG.
"""

from __future__ import annotations

from collections import Counter, deque
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from app.services.docx_semantic import (
    DocxStructureHint,
    build_docx_structure_hints,
    parse_docx_semantic,
)


class PandocDocxError(RuntimeError):
    """Base error for a failed Pandoc DOCX conversion."""


class PandocUnavailableError(PandocDocxError):
    """Raised when the Pandoc executable is not installed."""


def _custom_style(node: dict[str, Any]) -> str | None:
    content = node.get('c')
    if node.get('t') not in {'Div', 'Span'} or not isinstance(content, list) or len(content) < 2:
        return None
    attributes = content[0]
    if not isinstance(attributes, list) or len(attributes) < 3:
        return None
    key_values = attributes[2]
    if not isinstance(key_values, list):
        return None
    for item in key_values:
        if (
            isinstance(item, list)
            and len(item) == 2
            and item[0] == 'custom-style'
            and isinstance(item[1], str)
        ):
            return item[1]
    return None


def _normalize_ast(value: Any, custom_styles: Counter[str]) -> Any:
    """Remove Word-only wrappers and unresolved image targets.

    ``docx+styles`` represents custom paragraph styles as ``Div`` nodes and
    character styles as ``Span`` nodes.  GFM has no equivalent construct, so
    leaving them in produces distracting raw HTML.  Pandoc has already mapped
    meaningful built-in semantics (headers, emphasis, lists, tables) before
    this step; unknown style wrappers can therefore be unwrapped safely while
    their names remain available in the returned conversion metadata.

    DOCX image targets such as ``media/image1.png`` only become real files
    when Pandoc is asked to extract media. PaddleDoc currently stores and
    serves one Markdown artifact rather than an artifact directory, so those
    links would always be broken. Images are replaced by their alternative
    text here and counted in the conversion metadata for a future asset-aware
    pipeline.
    """

    if isinstance(value, list):
        normalized: list[Any] = []
        for item in value:
            if isinstance(item, dict):
                if item.get('t') == 'Image':
                    content = item.get('c')
                    if isinstance(content, list) and len(content) >= 2:
                        normalized.extend(_normalize_ast(content[1], custom_styles))
                    continue
                style = _custom_style(item)
                if style:
                    custom_styles[style] += 1
                    content = item.get('c')
                    normalized.extend(_normalize_ast(content[1], custom_styles))
                    continue
            normalized.append(_normalize_ast(item, custom_styles))
        return normalized
    if isinstance(value, dict):
        return {
            key: _normalize_ast(child, custom_styles)
            for key, child in value.items()
        }
    return value


def normalize_pandoc_ast(document: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Return a normalized copy of a Pandoc JSON document and style counts."""

    if not isinstance(document.get('blocks'), list):
        raise PandocDocxError('Pandoc returned JSON without a document block list')
    custom_styles: Counter[str] = Counter()
    normalized = _normalize_ast(deepcopy(document), custom_styles)
    if not isinstance(normalized, dict):  # defensive: the root is known to be a mapping
        raise PandocDocxError('Pandoc returned an invalid JSON document')
    return normalized, dict(sorted(custom_styles.items()))


def _ast_statistics(document: dict[str, Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            node_type = value.get('t')
            if isinstance(node_type, str):
                counts[node_type] += 1
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document.get('blocks', []))
    return {
        'paragraphs': counts['Para'] + counts['Plain'],
        'headings': counts['Header'],
        'ordered_lists': counts['OrderedList'],
        'bullet_lists': counts['BulletList'],
        'tables': counts['Table'],
        'links': counts['Link'],
        'images': counts['Image'],
        'footnotes': counts['Note'],
    }


_HEADING_STYLE_RE = re.compile(
    r'(?:^|[\s._-])(heading|headline|title|titel|überschrift)(?:[\s._-]|$)',
    re.IGNORECASE,
)
_HEADING_LEVEL_RE = re.compile(r'(?:heading|überschrift)\s*([1-6])', re.IGNORECASE)
_INLINE_ESCAPE_RE = re.compile(r'([\\`*+_[\]<>])')


def _attribute_value(attributes: Any, key: str) -> str | None:
    if not isinstance(attributes, list) or len(attributes) < 3:
        return None
    values = attributes[2]
    if not isinstance(values, list):
        return None
    for item in values:
        if isinstance(item, list) and len(item) == 2 and item[0] == key:
            return item[1] if isinstance(item[1], str) else None
    return None


def _style_from_block(block: Any) -> str | None:
    if not isinstance(block, dict) or block.get('t') != 'Div':
        return None
    content = block.get('c')
    if not isinstance(content, list) or len(content) < 2:
        return None
    return _attribute_value(content[0], 'custom-style')


def _div_blocks(block: dict[str, Any]) -> list[Any]:
    content = block.get('c')
    if not isinstance(content, list) or len(content) < 2 or not isinstance(content[1], list):
        return []
    return content[1]


def _looks_like_heading_style(style: str | None) -> bool:
    return bool(style and _HEADING_STYLE_RE.search(style))


def _hint_key(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip().casefold()


class _StructureHintIndex:
    def __init__(self, hints: tuple[DocxStructureHint, ...]) -> None:
        self._hints: dict[str, deque[DocxStructureHint]] = {}
        for hint in hints:
            key = _hint_key(hint.text)
            if key:
                self._hints.setdefault(key, deque()).append(hint)

    def peek(self, text: str) -> DocxStructureHint | None:
        candidates = self._hints.get(_hint_key(text))
        return candidates[0] if candidates else None

    def claim(self, text: str) -> DocxStructureHint | None:
        candidates = self._hints.get(_hint_key(text))
        return candidates.popleft() if candidates else None


class _MarkdownRenderer:
    """Render the Pandoc nodes that its DOCX reader can emit.

    Unsupported presentation-only nodes are deliberately reduced to their
    textual children. The renderer never emits raw HTML, which keeps the
    artifact readable and safe to split into semantic chunks.
    """

    def __init__(
        self,
        document: dict[str, Any],
        *,
        structure_hints: tuple[DocxStructureHint, ...] = (),
    ) -> None:
        self.document = document
        self.footnotes: list[str] = []
        self.structure_hints = _StructureHintIndex(structure_hints)
        self.structural_section_numbers: dict[int, int] = {}
        self.structural_section_sources: dict[int, tuple[str | None, int | None]] = {}
        blocks = document.get('blocks', [])
        self.title_from_style = self._has_styled_title(blocks)
        header_levels = self._header_levels(blocks)
        self.minimum_header_level = min(header_levels, default=1)
        self.header_base = 2 if self.title_from_style else 1

    def render(self) -> str:
        rendered = self._render_blocks(self.document.get('blocks', []), top_level=True)
        if self.footnotes:
            definitions = []
            for number, note in enumerate(self.footnotes, start=1):
                lines = note.splitlines() or ['']
                definition = f'[^{number}]: {lines[0]}'
                if len(lines) > 1:
                    definition += '\n' + '\n'.join(f'    {line}' for line in lines[1:])
                definitions.append(definition)
            rendered = f'{rendered}\n\n' + '\n\n'.join(definitions)
        # A structural heading may be nested inside a Pandoc list because
        # Word kept its visual indentation after the preceding list. Once a
        # block has been identified as a heading it must begin at column zero:
        # Markdown indentation would otherwise keep it inside the list and
        # destroy the section boundary used by RAG chunking.
        rendered = re.sub(r'(?m)^[ \t]+(?=#{1,6} )', '', rendered)
        return re.sub(r'\n{3,}', '\n\n', rendered).strip()

    def _header_levels(self, value: Any) -> list[int]:
        levels: list[int] = []
        if isinstance(value, dict):
            if value.get('t') == 'Header':
                content = value.get('c')
                if isinstance(content, list) and content and isinstance(content[0], int):
                    levels.append(content[0])
            for child in value.values():
                levels.extend(self._header_levels(child))
        elif isinstance(value, list):
            for child in value:
                levels.extend(self._header_levels(child))
        return levels

    def _has_styled_title(self, blocks: Any) -> bool:
        if not isinstance(blocks, list):
            return False
        for block in blocks:
            text = self._plain_block_text(block)
            if not text:
                continue
            style = _style_from_block(block)
            return (
                _looks_like_heading_style(style)
                and len(text) <= 180
                and '\n' not in text
                and not text.endswith(('.', ';', ':'))
            )
        return False

    @staticmethod
    def _escape_text(text: str) -> str:
        return _INLINE_ESCAPE_RE.sub(r'\\\1', text)

    def _render_inlines(self, inlines: Any) -> str:
        if not isinstance(inlines, list):
            return ''
        parts: list[str] = []
        for inline in inlines:
            if not isinstance(inline, dict):
                continue
            node_type = inline.get('t')
            content = inline.get('c')
            if node_type == 'Str':
                parts.append(self._escape_text(content if isinstance(content, str) else ''))
            elif node_type in {'Space', 'SoftBreak'}:
                parts.append(' ')
            elif node_type == 'LineBreak':
                parts.append('  \n')
            elif node_type in {'Emph', 'Underline'}:
                text = self._render_inlines(content)
                parts.append(f'*{text}*' if text else '')
            elif node_type == 'Strong':
                text = self._render_inlines(content)
                parts.append(f'**{text}**' if text else '')
            elif node_type == 'Strikeout':
                text = self._render_inlines(content)
                parts.append(f'~~{text}~~' if text else '')
            elif node_type in {'Superscript', 'Subscript', 'SmallCaps'}:
                parts.append(self._render_inlines(content))
            elif node_type == 'Code':
                code = content[1] if isinstance(content, list) and len(content) > 1 else ''
                fence = '``' if isinstance(code, str) and '`' in code else '`'
                parts.append(f'{fence}{code}{fence}')
            elif node_type == 'Math':
                math = content[1] if isinstance(content, list) and len(content) > 1 else ''
                parts.append(f'${math}$' if math else '')
            elif node_type == 'Link':
                label = (
                    self._render_inlines(content[1])
                    if isinstance(content, list) and len(content) > 1
                    else ''
                )
                target = content[2] if isinstance(content, list) and len(content) > 2 else []
                url = target[0] if isinstance(target, list) and target else ''
                if isinstance(url, str) and url:
                    safe_url = url.replace(' ', '%20').replace('(', '%28').replace(')', '%29')
                    parts.append(f'[{label or self._escape_text(url)}]({safe_url})')
                else:
                    parts.append(label)
            elif node_type == 'Image':
                alt = (
                    self._render_inlines(content[1])
                    if isinstance(content, list) and len(content) > 1
                    else ''
                )
                parts.append(alt)
            elif node_type == 'Span':
                children = content[1] if isinstance(content, list) and len(content) > 1 else []
                parts.append(self._render_inlines(children))
            elif node_type == 'Quoted':
                quote = (
                    self._render_inlines(content[1])
                    if isinstance(content, list) and len(content) > 1
                    else ''
                )
                parts.append(f'„{quote}“' if quote else '')
            elif node_type == 'Cite':
                children = content[1] if isinstance(content, list) and len(content) > 1 else []
                parts.append(self._render_inlines(children))
            elif node_type == 'Note':
                note = self._render_blocks(content, in_table_cell=True)
                if note:
                    self.footnotes.append(note)
                    parts.append(f'[^{len(self.footnotes)}]')
            elif node_type == 'RawInline':
                raw = content[1] if isinstance(content, list) and len(content) > 1 else ''
                if isinstance(raw, str) and '<' not in raw and '>' not in raw:
                    parts.append(self._escape_text(raw))
        text = ''.join(parts)
        text = re.sub(r'[ \t]+', ' ', text)
        return text.strip()

    def _plain_inlines(self, inlines: Any) -> str:
        if not isinstance(inlines, list):
            return ''
        parts: list[str] = []
        for inline in inlines:
            if not isinstance(inline, dict):
                continue
            node_type = inline.get('t')
            content = inline.get('c')
            if node_type == 'Str' and isinstance(content, str):
                parts.append(content)
            elif node_type in {'Space', 'SoftBreak', 'LineBreak'}:
                parts.append(' ')
            elif node_type in {
                'Emph',
                'Underline',
                'Strong',
                'Strikeout',
                'Superscript',
                'Subscript',
                'SmallCaps',
            }:
                parts.append(self._plain_inlines(content))
            elif node_type in {'Span', 'Link', 'Image', 'Quoted', 'Cite'}:
                children = content[1] if isinstance(content, list) and len(content) > 1 else []
                parts.append(self._plain_inlines(children))
            elif node_type in {'Code', 'Math', 'RawInline'}:
                value = content[1] if isinstance(content, list) and len(content) > 1 else ''
                if isinstance(value, str):
                    parts.append(value)
        return re.sub(r'\s+', ' ', ''.join(parts)).strip()

    def _plain_block_text(self, block: Any) -> str:
        if not isinstance(block, dict):
            return ''
        node_type = block.get('t')
        content = block.get('c')
        if node_type in {'Para', 'Plain'}:
            return self._plain_inlines(content)
        if node_type == 'Header' and isinstance(content, list) and len(content) > 2:
            return self._plain_inlines(content[2])
        if node_type in {'Div', 'BlockQuote'}:
            children = _div_blocks(block) if node_type == 'Div' else content
            if isinstance(children, list):
                return ' '.join(filter(None, (self._plain_block_text(child) for child in children)))
        return ''

    def _heading_level(self, original: int) -> int:
        return min(6, max(1, original - self.minimum_header_level + self.header_base))

    def _render_semantic_heading(
        self,
        text: str,
        hint: DocxStructureHint | None,
        *,
        fallback_level: int,
        label_override: str | None = None,
    ) -> str:
        original_level = hint.heading_level if hint and hint.heading_level else fallback_level
        level = self._heading_level(original_level)
        label = label_override if label_override is not None else (
            hint.numbering_label if hint else None
        )
        content = f'{label} {text}' if label and not text.startswith(label) else text
        return f'{"#" * level} {content}'.strip()

    def _first_block_text(self, blocks: Any) -> str:
        if not isinstance(blocks, list):
            return ''
        for block in blocks:
            if not isinstance(block, dict):
                continue
            node_type = block.get('t')
            if node_type in {'Para', 'Plain', 'Header', 'Div', 'BlockQuote'}:
                text = self._plain_block_text(block)
                if text:
                    if node_type in {'Div', 'BlockQuote'}:
                        children = _div_blocks(block) if node_type == 'Div' else block.get('c')
                        nested = self._first_block_text(children)
                        return nested or text
                    return text
        return ''

    def _style_heading_level(self, style: str | None, *, is_title: bool) -> int:
        if is_title:
            return 1
        match = _HEADING_LEVEL_RE.search(style or '')
        if match:
            return self._heading_level(int(match.group(1)))
        return self.header_base

    def _render_blocks(
        self,
        blocks: Any,
        *,
        in_table_cell: bool = False,
        top_level: bool = False,
    ) -> str:
        if not isinstance(blocks, list):
            return ''
        rendered: list[str] = []
        first_meaningful_seen = False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            node_type = block.get('t')
            content = block.get('c')
            output = ''
            if node_type in {'Para', 'Plain'}:
                plain_text = self._plain_inlines(content)
                hint = self.structure_hints.claim(plain_text)
                inline_text = self._render_inlines(content)
                if hint and hint.role == 'heading':
                    output = self._render_semantic_heading(
                        inline_text,
                        hint,
                        fallback_level=self.minimum_header_level + 1,
                    )
                else:
                    output = inline_text
            elif node_type == 'Header' and isinstance(content, list) and len(content) > 2:
                self.structural_section_numbers.clear()
                self.structural_section_sources.clear()
                plain_text = self._plain_inlines(content[2])
                hint = self.structure_hints.claim(plain_text)
                text = self._render_inlines(content[2])
                output = self._render_semantic_heading(
                    text,
                    hint,
                    fallback_level=content[0] if isinstance(content[0], int) else 1,
                ) if text else ''
            elif node_type == 'Div':
                style = _style_from_block(block)
                children = _div_blocks(block)
                styled_heading = _looks_like_heading_style(style)
                is_title = top_level and not first_meaningful_seen and self.title_from_style
                text = self._plain_block_text(block)
                if styled_heading and text and len(children) == 1 and len(text) <= 180:
                    hint = self.structure_hints.claim(text)
                    level = self._style_heading_level(style, is_title=is_title)
                    inline_content = children[0].get('c') if isinstance(children[0], dict) else []
                    heading_text = self._render_inlines(inline_content)
                    label = hint.numbering_label if hint else None
                    if label and heading_text and not heading_text.startswith(label):
                        heading_text = f'{label} {heading_text}'
                    output = f'{"#" * level} {heading_text}' if heading_text else ''
                else:
                    output = self._render_blocks(children, in_table_cell=in_table_cell)
            elif node_type == 'BlockQuote':
                # Word indentation is frequently represented as BlockQuote by
                # Pandoc. It is layout, not a reliable quote semantic.
                output = self._render_blocks(content, in_table_cell=in_table_cell)
            elif node_type == 'OrderedList':
                output = self._render_list(content, ordered=True, in_table_cell=in_table_cell)
            elif node_type == 'BulletList':
                output = self._render_list(content, ordered=False, in_table_cell=in_table_cell)
            elif node_type == 'Table':
                output = self._render_table(content)
            elif node_type == 'CodeBlock':
                code = content[1] if isinstance(content, list) and len(content) > 1 else ''
                if code:
                    fence = '````' if '```' in code else '```'
                    output = f'{fence}\n{code}\n{fence}'
            elif node_type == 'HorizontalRule':
                output = '---'
            elif node_type == 'LineBlock':
                if isinstance(content, list):
                    output = '  \n'.join(self._render_inlines(line) for line in content)
            # RawBlock and presentation-only nodes are intentionally omitted.
            if output.strip():
                rendered.append(output.strip())
                first_meaningful_seen = True
        separator = ' / ' if in_table_cell else '\n\n'
        return separator.join(rendered)

    def _render_list(self, content: Any, *, ordered: bool, in_table_cell: bool) -> str:
        if ordered:
            if not isinstance(content, list) or len(content) < 2:
                return ''
            attributes, items = content[0], content[1]
            start = (
                attributes[0]
                if isinstance(attributes, list)
                and attributes
                and isinstance(attributes[0], int)
                else 1
            )
        else:
            items = content
            start = 1
        if not isinstance(items, list):
            return ''
        if in_table_cell:
            flattened = [self._render_blocks(item, in_table_cell=True) for item in items]
            return ' / '.join(item for item in flattened if item)

        chunks: list[str] = []
        list_lines: list[str] = []

        def flush_list() -> None:
            if list_lines:
                chunks.append('\n'.join(list_lines))
                list_lines.clear()

        def append_list_item(marker: str, item_text: str) -> None:
            item_lines = item_text.splitlines()
            list_lines.append(f'{marker} {item_lines[0]}')
            continuation_indent = ' ' * (len(marker) + 1)
            list_lines.extend(
                continuation_indent if not line else f'{continuation_indent}{line}'
                for line in item_lines[1:]
            )

        def semantic_marker(
            hint: DocxStructureHint | None,
            fallback: str,
            item_text: str,
        ) -> tuple[str, str]:
            """Keep Word's list label while retaining valid GFM list syntax."""

            if not hint or hint.role != 'list_item' or not hint.numbering_label:
                return fallback, item_text
            if hint.number_format == 'bullet':
                return '-', item_text

            label = hint.numbering_label
            if (
                hint.number_format == 'decimal'
                and hint.numbering_level is not None
                and hint.numbering_level > 0
                and hint.numbering_path
            ):
                resolved_path = list(hint.numbering_path)
                for ancestor in range(min(hint.numbering_level, len(resolved_path))):
                    if ancestor in self.structural_section_numbers:
                        resolved_path[ancestor] = self.structural_section_numbers[ancestor]
                values = iter(resolved_path)
                label = re.sub(r'\d+', lambda match: str(next(values, int(match.group()))), label)

            if re.fullmatch(r'\d+[.)]', label):
                return label, item_text
            # CommonMark only recognizes decimal ordered-list markers. A
            # bullet plus the original Word label preserves both the list
            # boundary and labels such as a), IV. or 1.5.1.
            return '-', f'{label} {item_text}'

        for offset, item in enumerate(items):
            first_text = self._first_block_text(item)
            hint = self.structure_hints.peek(first_text)
            if hint and hint.role == 'paragraph' and hint.number_format == 'none':
                flush_list()
                paragraph = self._render_blocks(item)
                if paragraph:
                    chunks.append(paragraph)
                continue
            if hint and hint.role == 'heading':
                flush_list()
                self.structure_hints.claim(first_text)
                label_override = None
                if (
                    hint.confidence == 'structural'
                    and hint.numbering_level is not None
                    and ordered
                ):
                    level = hint.numbering_level
                    source_counter = (
                        hint.numbering_path[level]
                        if hint.numbering_path and len(hint.numbering_path) > level
                        else None
                    )
                    previous_source = self.structural_section_sources.get(level)
                    if level not in self.structural_section_numbers:
                        # Pandoc's list start is reliable at a genuine parent
                        # boundary and also preserves documents that begin at
                        # a non-one section.
                        counter = start + offset
                    elif (
                        previous_source
                        and hint.numbering_id
                        and previous_source[0] == hint.numbering_id
                        and source_counter is not None
                        and previous_source[1] is not None
                        and source_counter > previous_source[1]
                    ):
                        # Preserve deliberate gaps in a Word sequence without
                        # inheriting its stale ancestor counter.
                        counter = (
                            self.structural_section_numbers[level]
                            + source_counter
                            - previous_source[1]
                        )
                    else:
                        # A new Pandoc list container is not a numbering
                        # restart. Lists are frequently split by explanatory
                        # paragraphs, tables or an embedded bullet list.
                        counter = self.structural_section_numbers[level] + 1
                    self.structural_section_numbers[level] = counter
                    self.structural_section_sources[level] = (
                        hint.numbering_id,
                        source_counter,
                    )
                    for deeper in [
                        candidate
                        for candidate in self.structural_section_numbers
                        if candidate > level
                    ]:
                        del self.structural_section_numbers[deeper]
                        self.structural_section_sources.pop(deeper, None)
                    label_override = '.'.join(
                        str(
                            self.structural_section_numbers.get(
                                candidate,
                                hint.numbering_path[candidate]
                                if hint.numbering_path
                                and len(hint.numbering_path) > candidate
                                else 1,
                            )
                        )
                        for candidate in range(level + 1)
                    ) + '.'
                heading = self._render_semantic_heading(
                    self._escape_text(first_text),
                    hint,
                    fallback_level=self.minimum_header_level + 1,
                    label_override=label_override,
                )
                remaining = item[1:] if isinstance(item, list) else []
                body = self._render_blocks(remaining)
                chunks.append(f'{heading}\n\n{body}'.strip())
                continue

            breakout_index = self._structural_breakout_index(item)
            if breakout_index is not None:
                # Pandoc can attach normal body paragraphs and the next
                # numbered section to the final item of a preceding list.
                # OOXML gives us the linear semantic order, so retain only
                # the first block as list content and emit the remaining
                # document flow outside the list.
                item_text = self._render_blocks(item[:1])
                if item_text:
                    fallback = f'{start + offset}.' if ordered else '-'
                    marker, item_text = semantic_marker(hint, fallback, item_text)
                    append_list_item(marker, item_text)
                flush_list()
                trailing = self._render_blocks(item[1:])
                if trailing:
                    chunks.append(trailing)
                continue

            item_text = self._render_blocks(item)
            if not item_text:
                continue
            fallback = f'{start + offset}.' if ordered else '-'
            marker, item_text = semantic_marker(hint, fallback, item_text)
            append_list_item(marker, item_text)
        flush_list()
        return '\n\n'.join(chunks)

    def _structural_breakout_index(self, item: Any) -> int | None:
        """Find a nested structural section wrongly attached to a list item."""

        if not isinstance(item, list):
            return None
        for index, block in enumerate(item[1:], start=1):
            if self._contains_structural_heading(block):
                return index
        return None

    def _contains_structural_heading(self, value: Any) -> bool:
        if isinstance(value, dict):
            node_type = value.get('t')
            if node_type in {'Para', 'Plain', 'Header', 'Div', 'BlockQuote'}:
                text = self._plain_block_text(value)
                hint = self.structure_hints.peek(text)
                if hint and hint.role == 'heading':
                    return True
            return any(self._contains_structural_heading(child) for child in value.values())
        if isinstance(value, list):
            return any(self._contains_structural_heading(child) for child in value)
        return False

    def _render_table(self, content: Any) -> str:
        if not isinstance(content, list) or len(content) < 6:
            return ''
        caption = self._table_caption(content[1])
        head_rows = self._row_group(content[3])
        body_rows: list[list[str]] = []
        bodies = content[4]
        if isinstance(bodies, list):
            for body in bodies:
                if not isinstance(body, list):
                    continue
                if len(body) > 2:
                    body_rows.extend(self._rows(body[2]))
                if len(body) > 3:
                    body_rows.extend(self._rows(body[3]))
        foot_rows = self._row_group(content[5])
        rows = [*head_rows, *body_rows, *foot_rows]
        rows = [row for row in rows if any(cell.strip() for cell in row)]
        if not rows:
            return caption
        header_width = len(rows[0])
        while header_width > 1 and not rows[0][header_width - 1].strip():
            header_width -= 1
        width = max(len(row) for row in rows)
        rows = [row + [''] * (width - len(row)) for row in rows]
        # Some Word tables contain one trailing layout grid column that is
        # absent from their visible header. A merged value can spill into it
        # after colspan expansion. Remove only such duplicate/empty overflow
        # columns; never discard a distinct cell value.
        while width > header_width and all(
            not row[-1].strip() or row[-1] == row[-2]
            for row in rows
        ):
            rows = [row[:-1] for row in rows]
            width -= 1
        used_columns = [index for index in range(width) if any(row[index].strip() for row in rows)]
        if used_columns:
            rows = [[row[index] for index in used_columns] for row in rows]
        header = rows[0]
        data_rows = rows[1:]
        table_lines = [
            self._table_line(header),
            self._table_line(['---'] * len(header), escape=False),
        ]
        table_lines.extend(self._table_line(row) for row in data_rows)
        table = '\n'.join(table_lines)
        return f'**{caption}**\n\n{table}' if caption else table

    def _table_caption(self, caption: Any) -> str:
        if not isinstance(caption, list):
            return ''
        if len(caption) > 1 and isinstance(caption[1], list):
            long_caption = self._render_blocks(caption[1], in_table_cell=True)
            if long_caption:
                return long_caption
        if caption and isinstance(caption[0], list):
            return self._render_inlines(caption[0])
        return ''

    def _row_group(self, group: Any) -> list[list[str]]:
        if isinstance(group, list) and len(group) > 1:
            return self._rows(group[1])
        return []

    def _rows(self, rows: Any) -> list[list[str]]:
        if not isinstance(rows, list):
            return []
        rendered: list[list[str]] = []
        for row in rows:
            cells = row[1] if isinstance(row, list) and len(row) > 1 else []
            if not isinstance(cells, list):
                continue
            rendered_row: list[str] = []
            for cell in cells:
                if not isinstance(cell, list) or len(cell) < 5:
                    continue
                column_span = cell[3] if isinstance(cell[3], int) and cell[3] > 0 else 1
                text = self._render_blocks(cell[4], in_table_cell=True)
                rendered_row.append(text)
                # GFM has no colspan. Repeating a merged cell's semantic value
                # across the covered logical columns is less ambiguous for
                # retrieval than placing it under only the first header.
                rendered_row.extend([text] * (column_span - 1))
            rendered.append(rendered_row)
        return rendered

    @staticmethod
    def _table_line(cells: list[str], *, escape: bool = True) -> str:
        if escape:
            cells = [
                re.sub(r'\s*\n\s*', ' / ', cell).replace('|', r'\|').strip()
                for cell in cells
            ]
        return '| ' + ' | '.join(cells) + ' |'


def render_pandoc_ast(
    document: dict[str, Any],
    *,
    structure_hints: tuple[DocxStructureHint, ...] = (),
) -> str:
    """Render a Pandoc JSON document as normalized, HTML-free Markdown."""

    if not isinstance(document.get('blocks'), list):
        raise PandocDocxError('Pandoc returned JSON without a document block list')
    return _MarkdownRenderer(document, structure_hints=structure_hints).render()


def _run_pandoc(
    command: list[str],
    *,
    timeout_seconds: int,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            input=input_text,
            capture_output=True,
            text=True,
            encoding='utf-8',
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PandocDocxError(
            f'Pandoc DOCX conversion exceeded the {timeout_seconds}s timeout'
        ) from exc
    except OSError as exc:
        raise PandocDocxError(f'Failed to start Pandoc: {exc}') from exc

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or 'unknown Pandoc error'
        raise PandocDocxError(f'Pandoc DOCX conversion failed: {message[:1000]}')
    return result


def pandoc_docx_to_markdown(
    source: str | Path,
    *,
    timeout_seconds: int = 120,
) -> tuple[str, dict[str, Any]]:
    """Parse DOCX with Pandoc and render normalized PaddleDoc Markdown."""

    path = Path(source).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f'DOCX file not found: {path}')
    if path.suffix.casefold() != '.docx':
        unsupported = path.suffix or 'this file'
        raise PandocDocxError(f'Pandoc DOCX conversion does not support {unsupported}')

    executable = shutil.which('pandoc')
    if executable is None:
        raise PandocUnavailableError('Pandoc is not installed in this runtime')

    parsed = _run_pandoc(
        [
            executable,
            '--from=docx+styles',
            '--to=json',
            '--track-changes=accept',
            str(path),
        ],
        timeout_seconds=timeout_seconds,
    )
    try:
        document = json.loads(parsed.stdout)
    except json.JSONDecodeError as exc:
        raise PandocDocxError('Pandoc returned invalid JSON for the DOCX document') from exc
    if not isinstance(document, dict):
        raise PandocDocxError('Pandoc returned an invalid JSON document')

    statistics = _ast_statistics(document)
    normalized, custom_styles = normalize_pandoc_ast(document)
    try:
        structure_hints = build_docx_structure_hints(parse_docx_semantic(path))
    except (OSError, RuntimeError):
        # Pandoc is still able to extract content from some partially valid
        # packages. Structural enrichment must never turn that into a failed
        # conversion.
        structure_hints = ()
    markdown = render_pandoc_ast(document, structure_hints=structure_hints)
    if not markdown:
        raise PandocDocxError('PaddleDoc Markdown rendering produced no text')

    warnings = parsed.stderr.strip()
    api_version = normalized.get('pandoc-api-version', [])
    return markdown, {
        'docx_converter': 'pandoc',
        'markdown_renderer': 'paddledoc',
        'pandoc_api_version': '.'.join(str(part) for part in api_version),
        'pandoc_warnings': warnings[:4000],
        'custom_styles': custom_styles,
        'structure_hints': len(structure_hints),
        'structural_headings': sum(
            hint.role == 'heading' and hint.confidence == 'structural'
            for hint in structure_hints
        ),
        'paragraph_count': statistics['paragraphs'],
        'images_omitted_from_markdown': statistics['images'],
        **statistics,
    }
