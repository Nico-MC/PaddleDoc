"""Semantic, document-independent DOCX parsing and Markdown rendering.

The production converter in :mod:`app.services.paddle_service` currently
renders OOXML directly into Markdown and contains rules tailored to one
document family.  This module is the parallel v2 path: parsing produces a
small intermediate document model first, and a separate renderer turns that
model into Markdown.  Keeping both phases separate lets us test Word
semantics without coupling every parsing decision to Markdown syntax.

Only information present in the DOCX package is used.  In particular, this
module never invents heading levels or section numbers from the text itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Literal
from xml.etree import ElementTree as ET
import zipfile


_W_NS = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
_R_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_PKG_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
_MC_NS = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
_NS = {'w': _W_NS, 'r': _R_NS, 'pr': _PKG_REL_NS, 'mc': _MC_NS}


def _w(name: str) -> str:
    return f'{{{_W_NS}}}{name}'


def _r(name: str) -> str:
    return f'{{{_R_NS}}}{name}'


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit('}', 1)[-1]


@dataclass(frozen=True, slots=True)
class DocxRun:
    text: str
    bold: bool = False
    italic: bool = False
    href: str | None = None


@dataclass(frozen=True, slots=True)
class DocxNumberingReference:
    num_id: str
    level: int


@dataclass(frozen=True, slots=True)
class DocxNumberingDefinition:
    num_id: str
    level: int
    number_format: str
    level_text: str
    start: int = 1


@dataclass(frozen=True, slots=True)
class DocxTextBlock:
    kind: Literal['paragraph', 'heading', 'list_item']
    runs: tuple[DocxRun, ...]
    style_id: str | None = None
    style_name: str | None = None
    heading_level: int | None = None
    numbering: DocxNumberingReference | None = None
    source: Literal['body', 'table_cell', 'textbox'] = 'body'

    @property
    def text(self) -> str:
        return ''.join(run.text for run in self.runs).strip()


@dataclass(frozen=True, slots=True)
class DocxTableCell:
    blocks: tuple['DocxBlock', ...]
    column_span: int = 1
    vertical_merge: Literal['restart', 'continue'] | None = None


@dataclass(frozen=True, slots=True)
class DocxTableRow:
    cells: tuple[DocxTableCell, ...]
    is_header: bool = False


@dataclass(frozen=True, slots=True)
class DocxTableBlock:
    rows: tuple[DocxTableRow, ...]
    source: Literal['body', 'table_cell', 'textbox'] = 'body'


DocxBlock = DocxTextBlock | DocxTableBlock


@dataclass(frozen=True, slots=True)
class DocxDocument:
    source: Path
    blocks: tuple[DocxBlock, ...]
    numbering: tuple[DocxNumberingDefinition, ...] = ()

    @property
    def paragraph_count(self) -> int:
        def count(blocks: tuple[DocxBlock, ...]) -> int:
            total = 0
            for block in blocks:
                if isinstance(block, DocxTextBlock):
                    total += 1
                else:
                    total += sum(count(cell.blocks) for row in block.rows for cell in row.cells)
            return total

        return count(self.blocks)

    def statistics(self) -> dict[str, int]:
        stats = {
            'paragraphs': 0,
            'headings': 0,
            'list_items': 0,
            'tables': 0,
            'table_rows': 0,
            'links': 0,
        }

        def visit(blocks: tuple[DocxBlock, ...]) -> None:
            for block in blocks:
                if isinstance(block, DocxTextBlock):
                    stats['paragraphs'] += 1
                    if block.kind == 'heading':
                        stats['headings'] += 1
                    elif block.kind == 'list_item':
                        stats['list_items'] += 1
                    stats['links'] += sum(1 for run in block.runs if run.href)
                    continue
                stats['tables'] += 1
                stats['table_rows'] += len(block.rows)
                for row in block.rows:
                    for cell in row.cells:
                        visit(cell.blocks)

        visit(self.blocks)
        return stats


@dataclass(frozen=True, slots=True)
class DocxStructureHint:
    """Document-independent Word semantics for one textual block.

    Pandoc remains the primary content extractor. These hints supplement its
    AST with OOXML information that Pandoc's DOCX reader currently discards,
    most importantly hidden numbering and generated section labels.
    """

    text: str
    role: Literal['paragraph', 'heading', 'list_item']
    heading_level: int | None
    numbering_label: str | None
    numbering_level: int | None
    number_format: str | None
    style_id: str | None
    style_name: str | None
    confidence: Literal['explicit', 'structural']
    # Numeric counters as resolved from numbering.xml. Keeping the values
    # separate from the display label lets downstream renderers combine a
    # continued child counter with the currently active parent section. This
    # matters for Word documents that reuse one child numbering definition
    # below several independently numbered parent sections.
    numbering_path: tuple[int, ...] | None = None
    numbering_id: str | None = None


@dataclass(frozen=True, slots=True)
class _StyleDefinition:
    style_id: str
    name: str
    based_on: str | None
    outline_level: int | None
    num_id: str | None
    num_level: int | None
    bold: bool | None
    italic: bool | None
    style_type: str


class _DocxParser:
    def __init__(self, source: Path, archive: zipfile.ZipFile) -> None:
        self.source = source
        self.archive = archive
        self.styles: dict[str, _StyleDefinition] = {}
        self.numbering: dict[tuple[str, int], DocxNumberingDefinition] = {}
        self.relationships: dict[str, str] = {}
        self.default_paragraph_style_id: str | None = None

    def parse(self) -> DocxDocument:
        document_root = self._required_xml('word/document.xml')
        self._load_styles()
        self._load_numbering()
        self._load_relationships()

        body = document_root.find('./w:body', _NS)
        if body is None:
            raise RuntimeError('DOCX semantic extraction could not find document body')
        blocks = tuple(self._parse_container(body, source='body'))
        if not blocks:
            raise RuntimeError('DOCX semantic extraction produced no content')
        return DocxDocument(
            source=self.source,
            blocks=blocks,
            numbering=tuple(
                definition
                for _, definition in sorted(
                    self.numbering.items(),
                    key=lambda item: (item[0][0], item[0][1]),
                )
            ),
        )
    def _required_xml(self, member: str) -> ET.Element:
        try:
            return ET.fromstring(self.archive.read(member))
        except KeyError as exc:
            raise RuntimeError(f'DOCX semantic extraction could not find {member}') from exc
        except ET.ParseError as exc:
            raise RuntimeError(f'DOCX semantic extraction could not parse {member}: {exc}') from exc

    def _optional_xml(self, member: str) -> ET.Element | None:
        try:
            return ET.fromstring(self.archive.read(member))
        except KeyError:
            return None
        except ET.ParseError as exc:
            raise RuntimeError(f'DOCX semantic extraction could not parse {member}: {exc}') from exc

    @staticmethod
    def _attribute(element: ET.Element | None, name: str, default: str = '') -> str:
        if element is None:
            return default
        return element.get(_w(name), default)

    @staticmethod
    def _on_off(parent: ET.Element | None, property_name: str) -> bool | None:
        if parent is None:
            return None
        element = parent.find(f'./w:{property_name}', _NS)
        if element is None:
            return None
        return element.get(_w('val'), 'true').lower() not in {'0', 'false', 'off', 'none'}

    def _load_styles(self) -> None:
        root = self._optional_xml('word/styles.xml')
        if root is None:
            return
        for element in root.findall('./w:style', _NS):
            style_id = element.get(_w('styleId'), '').strip()
            if not style_id:
                continue
            style_type = element.get(_w('type'), '').strip()
            name = self._attribute(element.find('./w:name', _NS), 'val', style_id)
            based_on = self._attribute(element.find('./w:basedOn', _NS), 'val') or None
            paragraph_properties = element.find('./w:pPr', _NS)
            run_properties = element.find('./w:rPr', _NS)
            outline_element = (
                paragraph_properties.find('./w:outlineLvl', _NS)
                if paragraph_properties is not None
                else None
            )
            outline_level = self._safe_int(self._attribute(outline_element, 'val'), default=None)
            num_id, num_level = self._numbering_reference(paragraph_properties)
            self.styles[style_id] = _StyleDefinition(
                style_id=style_id,
                name=name,
                based_on=based_on,
                outline_level=outline_level,
                num_id=num_id,
                num_level=num_level,
                bold=self._on_off(run_properties, 'b'),
                italic=self._on_off(run_properties, 'i'),
                style_type=style_type,
            )
            if (
                style_type == 'paragraph'
                and element.get(_w('default'), '').lower() in {'1', 'true', 'on'}
            ):
                self.default_paragraph_style_id = style_id

    def _load_numbering(self) -> None:
        root = self._optional_xml('word/numbering.xml')
        if root is None:
            return

        abstract_levels: dict[tuple[str, int], DocxNumberingDefinition] = {}
        for abstract in root.findall('./w:abstractNum', _NS):
            abstract_id = abstract.get(_w('abstractNumId'), '').strip()
            for level in abstract.findall('./w:lvl', _NS):
                parsed = self._parse_numbering_level(level)
                abstract_levels[(abstract_id, parsed.level)] = parsed

        for number in root.findall('./w:num', _NS):
            num_id = number.get(_w('numId'), '').strip()
            abstract_id = self._attribute(number.find('./w:abstractNumId', _NS), 'val')
            for (candidate_id, level), definition in abstract_levels.items():
                if candidate_id == abstract_id:
                    self.numbering[(num_id, level)] = DocxNumberingDefinition(
                        num_id=num_id,
                        level=definition.level,
                        number_format=definition.number_format,
                        level_text=definition.level_text,
                        start=definition.start,
                    )

            for override in number.findall('./w:lvlOverride', _NS):
                level = self._safe_int(override.get(_w('ilvl'), '0'), default=0) or 0
                inline_level = override.find('./w:lvl', _NS)
                if inline_level is not None:
                    definition = self._parse_numbering_level(inline_level, forced_level=level)
                else:
                    definition = self.numbering.get(
                        (num_id, level),
                        DocxNumberingDefinition(
                            num_id=num_id,
                            level=level,
                            number_format='decimal',
                            level_text=f'%{level + 1}.',
                            start=1,
                        ),
                    )
                start_override = override.find('./w:startOverride', _NS)
                if start_override is not None:
                    definition = DocxNumberingDefinition(
                        num_id=num_id,
                        level=definition.level,
                        number_format=definition.number_format,
                        level_text=definition.level_text,
                        start=self._safe_int(self._attribute(start_override, 'val'), default=definition.start)
                        or definition.start,
                    )
                self.numbering[(num_id, level)] = definition

    def _parse_numbering_level(
        self,
        element: ET.Element,
        *,
        forced_level: int | None = None,
    ) -> DocxNumberingDefinition:
        level = forced_level
        if level is None:
            level = self._safe_int(element.get(_w('ilvl'), '0'), default=0) or 0
        number_format = self._attribute(element.find('./w:numFmt', _NS), 'val', 'decimal')
        level_text = self._attribute(element.find('./w:lvlText', _NS), 'val', f'%{level + 1}.')
        start = self._safe_int(self._attribute(element.find('./w:start', _NS), 'val'), default=1) or 1
        return DocxNumberingDefinition(
            num_id='',
            level=level,
            number_format=number_format,
            level_text=level_text,
            start=start,
        )

    def _load_relationships(self) -> None:
        root = self._optional_xml('word/_rels/document.xml.rels')
        if root is None:
            return
        for relationship in root.findall('./pr:Relationship', _NS):
            rel_id = relationship.get('Id', '').strip()
            target = relationship.get('Target', '').strip()
            if rel_id and target:
                self.relationships[rel_id] = target

    @staticmethod
    def _safe_int(value: str | None, *, default: int | None) -> int | None:
        try:
            return int(value) if value not in {None, ''} else default
        except (TypeError, ValueError):
            return default

    def _parse_container(
        self,
        container: ET.Element,
        *,
        source: Literal['body', 'table_cell', 'textbox'],
    ) -> list[DocxBlock]:
        blocks: list[DocxBlock] = []
        for child in self._selected_children(container):
            name = _local_name(child)
            if name == 'p':
                blocks.extend(self._parse_paragraph(child, source=source))
            elif name == 'tbl':
                table = self._parse_table(child, source=source)
                if table.rows:
                    blocks.append(table)
            elif name == 'sdt':
                content = child.find('./w:sdtContent', _NS)
                if content is not None:
                    blocks.extend(self._parse_container(content, source=source))
            elif name in {'customXml', 'smartTag'}:
                blocks.extend(self._parse_container(child, source=source))
        return blocks

    def _selected_children(self, container: ET.Element) -> list[ET.Element]:
        children: list[ET.Element] = []
        for child in list(container):
            if child.tag != f'{{{_MC_NS}}}AlternateContent':
                children.append(child)
                continue
            choice = child.find('./mc:Choice', _NS)
            selected = choice if choice is not None else child.find('./mc:Fallback', _NS)
            if selected is not None:
                children.extend(self._selected_children(selected))
        return children

    def _parse_paragraph(
        self,
        paragraph: ET.Element,
        *,
        source: Literal['body', 'table_cell', 'textbox'],
    ) -> list[DocxBlock]:
        paragraph_properties = paragraph.find('./w:pPr', _NS)
        style_id = self._attribute(
            paragraph_properties.find('./w:pStyle', _NS) if paragraph_properties is not None else None,
            'val',
        ) or self.default_paragraph_style_id
        style_name = self.styles.get(style_id).name if style_id in self.styles else style_id
        runs = self._paragraph_runs(paragraph, paragraph_style_id=style_id)
        blocks: list[DocxBlock] = []

        if any(run.text.strip() for run in runs):
            explicit_outline = self._safe_int(
                self._attribute(
                    paragraph_properties.find('./w:outlineLvl', _NS)
                    if paragraph_properties is not None
                    else None,
                    'val',
                ),
                default=None,
            )
            outline_level = (
                explicit_outline
                if explicit_outline is not None
                else self._resolved_style_value(style_id, 'outline_level')
            )
            heading_level = self._heading_level(style_id, style_name, outline_level)
            num_id, num_level = self._numbering_reference(paragraph_properties)
            if num_id is None:
                resolved_num_id = self._resolved_style_value(style_id, 'num_id')
                resolved_num_level = self._resolved_style_value(style_id, 'num_level')
                num_id = resolved_num_id if isinstance(resolved_num_id, str) else None
                num_level = resolved_num_level if isinstance(resolved_num_level, int) else None
            numbering = (
                DocxNumberingReference(num_id=num_id, level=max(0, num_level or 0))
                if num_id and num_id != '0'
                else None
            )
            kind: Literal['paragraph', 'heading', 'list_item']
            if heading_level is not None:
                kind = 'heading'
            elif numbering is not None:
                kind = 'list_item'
            else:
                kind = 'paragraph'
            blocks.append(
                DocxTextBlock(
                    kind=kind,
                    runs=runs,
                    style_id=style_id,
                    style_name=style_name,
                    heading_level=heading_level,
                    numbering=numbering,
                    source=source,
                )
            )

        for textbox in self._textbox_contents(paragraph):
            blocks.extend(self._parse_container(textbox, source='textbox'))
        return blocks

    def _heading_level(
        self,
        style_id: str | None,
        style_name: str | None,
        outline_level: object,
    ) -> int | None:
        if isinstance(outline_level, int) and 0 <= outline_level <= 8:
            return min(outline_level + 1, 6)

        # Built-in Word heading styles occasionally omit outlineLvl from a
        # minimal DOCX.  Falling back to their explicit numeric suffix is
        # still document metadata; unlike the legacy path this never guesses
        # from paragraph text or from an arbitrary custom style name.
        for candidate in (style_id or '', style_name or ''):
            normalized = candidate.casefold().replace('ü', 'u')
            match = re.fullmatch(r'(?:heading|uberschrift|berschrift)\s*([1-9])', normalized)
            if match:
                return min(int(match.group(1)), 6)
        return None

    def _resolved_style_value(self, style_id: str | None, field: str) -> object:
        seen: set[str] = set()
        current = style_id
        while current and current not in seen:
            seen.add(current)
            style = self.styles.get(current)
            if style is None:
                return None
            value = getattr(style, field)
            if value is not None:
                return value
            current = style.based_on
        return None

    def _numbering_reference(self, paragraph_properties: ET.Element | None) -> tuple[str | None, int | None]:
        if paragraph_properties is None:
            return None, None
        num_properties = paragraph_properties.find('./w:numPr', _NS)
        if num_properties is None:
            return None, None
        num_id = self._attribute(num_properties.find('./w:numId', _NS), 'val') or None
        level = self._safe_int(
            self._attribute(num_properties.find('./w:ilvl', _NS), 'val'),
            default=0,
        )
        return num_id, level

    def _paragraph_runs(self, paragraph: ET.Element, *, paragraph_style_id: str | None) -> tuple[DocxRun, ...]:
        runs: list[DocxRun] = []
        for child in self._selected_children(paragraph):
            name = _local_name(child)
            if name == 'pPr':
                continue
            runs.extend(self._inline_runs(child, paragraph_style_id=paragraph_style_id, href=None))
        return self._merge_runs(runs)

    def _inline_runs(
        self,
        element: ET.Element,
        *,
        paragraph_style_id: str | None,
        href: str | None,
    ) -> list[DocxRun]:
        name = _local_name(element)
        if name == 'r':
            text = self._run_text(element)
            if not text:
                return []
            properties = element.find('./w:rPr', _NS)
            character_style_id = self._attribute(
                properties.find('./w:rStyle', _NS) if properties is not None else None,
                'val',
            ) or None
            bold = self._on_off(properties, 'b')
            italic = self._on_off(properties, 'i')
            if bold is None:
                inherited = self._resolved_style_value(character_style_id, 'bold')
                if inherited is None:
                    inherited = self._resolved_style_value(paragraph_style_id, 'bold')
                bold = bool(inherited)
            if italic is None:
                inherited = self._resolved_style_value(character_style_id, 'italic')
                if inherited is None:
                    inherited = self._resolved_style_value(paragraph_style_id, 'italic')
                italic = bool(inherited)
            return [DocxRun(text=text, bold=bold, italic=italic, href=href)]

        if name == 'hyperlink':
            rel_id = element.get(_r('id'), '')
            anchor = element.get(_w('anchor'), '')
            link_target = self.relationships.get(rel_id) if rel_id else None
            if link_target is None and anchor:
                link_target = f'#{anchor}'
            href = link_target or href
        elif name == 'fldSimple':
            instruction = element.get(_w('instr'), '')
            match = re.search(r'HYPERLINK\s+"([^"]+)"', instruction, flags=re.IGNORECASE)
            if match:
                href = match.group(1)
        elif name in {'drawing', 'pict'}:
            # Text boxes inside drawings are emitted as separate semantic
            # blocks by _textbox_contents.  Descending here would duplicate
            # their text inside the host paragraph.
            return []

        runs: list[DocxRun] = []
        for child in self._selected_children(element):
            runs.extend(self._inline_runs(child, paragraph_style_id=paragraph_style_id, href=href))
        return runs

    def _run_text(self, run: ET.Element) -> str:
        tokens: list[str] = []

        def visit(element: ET.Element) -> None:
            name = _local_name(element)
            if name in {'drawing', 'pict', 'txbxContent', 'instrText', 'delText'}:
                return
            if name == 't':
                tokens.append(element.text or '')
                return
            if name == 'tab':
                tokens.append('\t')
                return
            if name in {'br', 'cr'}:
                tokens.append('\n')
                return
            if name == 'noBreakHyphen':
                tokens.append('‑')
                return
            if name == 'softHyphen':
                return
            if element.tag == f'{{{_MC_NS}}}AlternateContent':
                for selected in self._selected_children(element):
                    visit(selected)
                return
            for child in list(element):
                visit(child)

        visit(run)
        return ''.join(tokens)

    @staticmethod
    def _merge_runs(runs: list[DocxRun]) -> tuple[DocxRun, ...]:
        merged: list[DocxRun] = []
        for run in runs:
            if not run.text:
                continue
            if (
                merged
                and merged[-1].bold == run.bold
                and merged[-1].italic == run.italic
                and merged[-1].href == run.href
            ):
                previous = merged[-1]
                merged[-1] = DocxRun(
                    text=previous.text + run.text,
                    bold=run.bold,
                    italic=run.italic,
                    href=run.href,
                )
            else:
                merged.append(run)
        return tuple(merged)

    def _textbox_contents(self, paragraph: ET.Element) -> list[ET.Element]:
        results: list[ET.Element] = []

        def visit(element: ET.Element) -> None:
            if _local_name(element) == 'txbxContent':
                results.append(element)
                return
            for child in self._selected_children(element):
                visit(child)

        visit(paragraph)
        return results

    def _parse_table(
        self,
        table: ET.Element,
        *,
        source: Literal['body', 'table_cell', 'textbox'],
    ) -> DocxTableBlock:
        rows: list[DocxTableRow] = []
        for row_element in table.findall('./w:tr', _NS):
            row_properties = row_element.find('./w:trPr', _NS)
            is_header = self._on_off(row_properties, 'tblHeader') is True
            cells: list[DocxTableCell] = []
            for cell_element in row_element.findall('./w:tc', _NS):
                properties = cell_element.find('./w:tcPr', _NS)
                span = self._safe_int(
                    self._attribute(
                        properties.find('./w:gridSpan', _NS) if properties is not None else None,
                        'val',
                    ),
                    default=1,
                ) or 1
                vertical_merge_element = (
                    properties.find('./w:vMerge', _NS) if properties is not None else None
                )
                vertical_merge: Literal['restart', 'continue'] | None = None
                if vertical_merge_element is not None:
                    vertical_merge = (
                        'restart'
                        if vertical_merge_element.get(_w('val'), '').lower() == 'restart'
                        else 'continue'
                    )
                cell_blocks = tuple(self._parse_container(cell_element, source='table_cell'))
                cells.append(
                    DocxTableCell(
                        blocks=cell_blocks,
                        column_span=max(1, span),
                        vertical_merge=vertical_merge,
                    )
                )
            if cells:
                rows.append(DocxTableRow(cells=tuple(cells), is_header=is_header))
        return DocxTableBlock(rows=tuple(rows), source=source)


class _NumberingResolver:
    """Resolve OOXML numbering references while preserving sequence state."""

    def __init__(self, document: DocxDocument) -> None:
        self.numbering = {
            (definition.num_id, definition.level): definition
            for definition in document.numbering
        }
        self.counters: dict[str, dict[int, int]] = {}

    def marker_with_path(
        self,
        reference: DocxNumberingReference | None,
    ) -> tuple[str, tuple[int, ...]]:
        if reference is None:
            return '', ()
        definition = self.numbering.get(
            (reference.num_id, reference.level),
            DocxNumberingDefinition(
                num_id=reference.num_id,
                level=reference.level,
                number_format='decimal',
                level_text=f'%{reference.level + 1}.',
                start=1,
            ),
        )
        counters = self.counters.setdefault(reference.num_id, {})
        for deeper_level in [level for level in counters if level > reference.level]:
            del counters[deeper_level]
        if reference.level in counters:
            counters[reference.level] += 1
        else:
            counters[reference.level] = definition.start
        for ancestor_level in range(reference.level):
            if ancestor_level not in counters:
                ancestor_definition = self.numbering.get((reference.num_id, ancestor_level))
                counters[ancestor_level] = ancestor_definition.start if ancestor_definition else 1

        path = tuple(counters.get(level, 1) for level in range(reference.level + 1))

        if definition.number_format == 'none':
            return '', path
        if definition.number_format == 'bullet':
            return '-', path
        marker = definition.level_text
        for level in range(reference.level + 1):
            level_definition = self.numbering.get((reference.num_id, level), definition)
            marker = marker.replace(
                f'%{level + 1}',
                self._format_number(counters.get(level, 1), level_definition.number_format),
            )
        return (
            marker.strip()
            or self._format_number(counters[reference.level], definition.number_format),
            path,
        )

    def marker(self, reference: DocxNumberingReference | None) -> str:
        marker, _ = self.marker_with_path(reference)
        return marker

    @staticmethod
    def _format_number(value: int, number_format: str) -> str:
        if number_format == 'lowerLetter':
            return _alphabetic(value).lower()
        if number_format == 'upperLetter':
            return _alphabetic(value).upper()
        if number_format == 'lowerRoman':
            return _roman(value).lower()
        if number_format == 'upperRoman':
            return _roman(value).upper()
        return str(value)


class _MarkdownRenderer:
    def __init__(self, document: DocxDocument) -> None:
        self.document = document
        self.numbering = {
            (definition.num_id, definition.level): definition
            for definition in document.numbering
        }
        self.numbering_resolver = _NumberingResolver(document)

    def render(self) -> str:
        rendered: list[tuple[str, str]] = []
        for block in self.document.blocks:
            value = self._render_block(block)
            if value:
                kind = block.kind if isinstance(block, DocxTextBlock) else 'table'
                rendered.append((kind, value))

        output = ''
        previous_kind: str | None = None
        for kind, value in rendered:
            separator = '\n' if kind == previous_kind == 'list_item' else '\n\n'
            output = value if not output else f'{output}{separator}{value}'
            previous_kind = kind
        return output.strip()

    def _render_block(self, block: DocxBlock) -> str:
        if isinstance(block, DocxTableBlock):
            return self._render_table(block)
        content = ''.join(self._render_run(run) for run in block.runs).strip()
        if not content:
            return ''

        marker = self.numbering_resolver.marker(block.numbering) if block.numbering else ''
        if block.kind == 'heading':
            level = min(max(block.heading_level or 1, 1), 6)
            prefix = f'{marker} ' if marker else ''
            return f'{"#" * level} {prefix}{content}'.strip()
        if block.kind == 'list_item' and block.numbering:
            indent = '  ' * block.numbering.level
            definition = self.numbering.get((block.numbering.num_id, block.numbering.level))
            if definition and definition.number_format == 'none':
                return content
            if definition and definition.number_format == 'bullet':
                return f'{indent}- {content}'
            if re.fullmatch(r'\d+[.)]', marker):
                return f'{indent}{marker} {content}'
            return f'{indent}- {marker} {content}'.rstrip()
        return content

    def _render_run(self, run: DocxRun) -> str:
        leading_match = re.match(r'^\s*', run.text)
        trailing_match = re.search(r'\s*$', run.text)
        leading = leading_match.group(0) if leading_match else ''
        trailing = trailing_match.group(0) if trailing_match else ''
        core_end = len(run.text) - len(trailing) if trailing else len(run.text)
        core = run.text[len(leading):core_end]
        if not core:
            return run.text

        escaped = _escape_markdown(core).replace('\t', '    ').replace('\n', '<br>')
        if run.bold and run.italic:
            escaped = f'***{escaped}***'
        elif run.bold:
            escaped = f'**{escaped}**'
        elif run.italic:
            escaped = f'*{escaped}*'
        if run.href:
            href = run.href.replace(' ', '%20').replace(')', '\\)')
            escaped = f'[{escaped}]({href})'
        return f'{leading}{escaped}{trailing}'

    def _render_table(self, table: DocxTableBlock) -> str:
        expanded_rows: list[tuple[list[str], bool]] = []
        width = 0
        for row in table.rows:
            cells: list[str] = []
            for cell in row.cells:
                value = self._render_table_cell(cell)
                cells.append(value)
                cells.extend([''] * (cell.column_span - 1))
            width = max(width, len(cells))
            expanded_rows.append((cells, row.is_header))
        if width == 0:
            return ''

        normalized = [(cells + [''] * (width - len(cells)), is_header) for cells, is_header in expanded_rows]
        if normalized[0][1]:
            header = normalized[0][0]
            data_rows = [cells for cells, _ in normalized[1:]]
        else:
            # GFM requires a header row.  A synthetic blank header keeps the
            # first real Word row as data instead of silently reclassifying it.
            header = [''] * width
            data_rows = [cells for cells, _ in normalized]

        lines = [
            '| ' + ' | '.join(cell or ' ' for cell in header) + ' |',
            '| ' + ' | '.join(['---'] * width) + ' |',
        ]
        lines.extend('| ' + ' | '.join(cell or ' ' for cell in row) + ' |' for row in data_rows)
        return '\n'.join(lines)

    def _render_table_cell(self, cell: DocxTableCell) -> str:
        parts: list[str] = []
        for block in cell.blocks:
            if isinstance(block, DocxTextBlock):
                content = ''.join(self._render_run(run) for run in block.runs).strip()
                if content:
                    marker = (
                        self.numbering_resolver.marker(block.numbering)
                        if block.numbering
                        else ''
                    )
                    parts.append(f'{marker} {content}'.strip())
            else:
                nested = self._render_table(block)
                if nested:
                    parts.append(nested)
        return '<br>'.join(parts).replace('|', '\\|').replace('\n', '<br>')


def _escape_markdown(value: str) -> str:
    return re.sub(r'([\\`*_[\]])', r'\\\1', value)


def _alphabetic(value: int) -> str:
    result = ''
    current = max(1, value)
    while current:
        current, remainder = divmod(current - 1, 26)
        result = chr(ord('A') + remainder) + result
    return result


def _roman(value: int) -> str:
    numerals = (
        (1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),
        (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'),
        (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I'),
    )
    current = max(1, value)
    result = ''
    for unit, numeral in numerals:
        count, current = divmod(current, unit)
        result += numeral * count
    return result


def _numbering_definition(
    document: DocxDocument,
    block: DocxTextBlock,
) -> DocxNumberingDefinition | None:
    if block.numbering is None:
        return None
    return next(
        (
            definition
            for definition in document.numbering
            if definition.num_id == block.numbering.num_id
            and definition.level == block.numbering.level
        ),
        None,
    )


def _is_structural_section_title(
    block: DocxTextBlock,
    following: DocxBlock | None,
    definition: DocxNumberingDefinition | None,
    *,
    minimum_heading_level: int | None,
) -> bool:
    """Recognize a numbered section from layout-independent structure.

    The rule is intentionally narrow: only decimal body numbering adjacent
    to explanatory content (or a deeper numbered level) is promoted. Bullet,
    alphabetic and sentence-like entries remain lists.
    """

    if (
        minimum_heading_level is None
        or block.kind != 'list_item'
        or block.source != 'body'
        or block.numbering is None
        or definition is None
        or definition.number_format != 'decimal'
        or len(block.text) > 180
        or block.text.rstrip().endswith(('.', ',', ';', ':'))
        or not isinstance(following, DocxTextBlock)
        or following.source != 'body'
    ):
        return False
    if following.kind == 'paragraph':
        return True
    return bool(
        following.numbering
        and following.numbering.level > block.numbering.level
    )


def build_docx_structure_hints(document: DocxDocument) -> tuple[DocxStructureHint, ...]:
    """Derive conservative semantic hints from the parsed Word model."""

    minimum_heading_level = min(
        (
            block.heading_level
            for block in document.blocks
            if isinstance(block, DocxTextBlock) and block.heading_level is not None
        ),
        default=None,
    )
    direct_sections: set[int] = set()
    section_groups: dict[tuple[str, int], list[int]] = {}
    for index, block in enumerate(document.blocks):
        if not isinstance(block, DocxTextBlock):
            continue
        following = document.blocks[index + 1] if index + 1 < len(document.blocks) else None
        definition = _numbering_definition(document, block)
        if _is_structural_section_title(
            block,
            following,
            definition,
            minimum_heading_level=minimum_heading_level,
        ):
            direct_sections.add(index)
        if (
            block.kind == 'list_item'
            and block.source == 'body'
            and block.numbering
            and definition
            and definition.number_format == 'decimal'
            and len(block.text) <= 180
            and not block.text.rstrip().endswith(('.', ',', ';', ':'))
        ):
            section_groups.setdefault(
                (block.numbering.num_id, block.numbering.level),
                [],
            ).append(index)
    promoted_groups = {
        group
        for group, indexes in section_groups.items()
        if len(indexes) >= 2
        and len(direct_sections.intersection(indexes)) >= 2
        and len(direct_sections.intersection(indexes)) * 2 >= len(indexes)
    }

    numbering = _NumberingResolver(document)
    hints: list[DocxStructureHint] = []
    for index, block in enumerate(document.blocks):
        if not isinstance(block, DocxTextBlock):
            continue
        definition = _numbering_definition(document, block)
        if block.numbering:
            label, numbering_path = numbering.marker_with_path(block.numbering)
        else:
            label, numbering_path = '', ()
        role = block.kind
        heading_level = block.heading_level
        confidence: Literal['explicit', 'structural'] = 'explicit'
        if definition and definition.number_format == 'none':
            role = 'paragraph'
            label = ''
        group = (
            (block.numbering.num_id, block.numbering.level)
            if block.numbering
            else None
        )
        if (
            index in direct_sections
            or group in promoted_groups
        ):
            role = 'heading'
            heading_level = (
                (minimum_heading_level or 1)
                + 1
                + (block.numbering.level if block.numbering else 0)
            )
            confidence = 'structural'
        hints.append(
            DocxStructureHint(
                text=block.text,
                role=role,
                heading_level=heading_level,
                numbering_label=label or None,
                numbering_level=block.numbering.level if block.numbering else None,
                number_format=definition.number_format if definition else None,
                style_id=block.style_id,
                style_name=block.style_name,
                confidence=confidence,
                numbering_path=numbering_path or None,
                numbering_id=block.numbering.num_id if block.numbering else None,
            )
        )
    return tuple(hints)


def parse_docx_semantic(source: str | Path) -> DocxDocument:
    """Parse a DOCX package into a semantic intermediate document model."""

    path = Path(source).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f'DOCX file not found: {path}')
    try:
        with zipfile.ZipFile(path, 'r') as archive:
            return _DocxParser(path, archive).parse()
    except zipfile.BadZipFile as exc:
        raise RuntimeError('DOCX semantic extraction received an invalid DOCX file') from exc


def render_docx_markdown(document: DocxDocument) -> str:
    """Render a parsed semantic DOCX document as Markdown."""

    return _MarkdownRenderer(document).render()


def semantic_docx_to_markdown(source: str | Path) -> tuple[str, DocxDocument]:
    """Parse and render a DOCX while retaining its intermediate model."""

    path = Path(source).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f'DOCX file not found: {path}')
    try:
        with zipfile.ZipFile(path, 'r') as archive:
            parser = _DocxParser(path, archive)
            document = parser.parse()
            markdown = render_docx_markdown(document)
    except zipfile.BadZipFile as exc:
        raise RuntimeError('DOCX semantic extraction received an invalid DOCX file') from exc
    if not markdown:
        raise RuntimeError('DOCX semantic Markdown rendering produced no text')
    return markdown, document
