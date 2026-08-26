import base64
import html
import importlib.util
import json as _json
import platform
import re
import urllib.error
import urllib.request
import zipfile
from xml.etree import ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from celery.exceptions import TimeoutError as CeleryTimeoutError
from pypdf import PdfReader, PdfWriter
from redis import Redis

from app.core.config import settings
from app.services.quality_gate import evaluate_document_quality

_RUNTIME_SETTINGS_KEY = 'paddle:runtime_settings'
_DEFAULT_PROFILE_ID = 'ppocrv6_tiny'
_PDF_CHUNK_PAGE_SIZE = 6
_PADDLE_VL_PIPELINES: dict[tuple[str, str], object] = {}
_PDF_CHUNK_PAGE_SIZE_BY_PROFILE: dict[str, int] = {
    'ppocrv6_medium_structurev3': 2,
    'ppocrv6_medium': 2,
    'ppocrv6_small_structurev3': 4,
    'ppocrv6_small': 4,
    'ppocrv6_tiny_structurev3': 6,
    'ppocrv6_tiny': 8,
    # OpenAI vision: always 1 page per request (vision API constraint)
    'openai_vision': 1,
}

_PADDLE_PROFILES: dict[str, dict[str, str]] = {
    'no_profile': {
        'value': 'no_profile',
        'label': 'No profile (native extraction)',
        'description': 'Skip OCR and use native text extraction for DOCX, PDF, and spreadsheets.',
        'pipeline': 'native',
    },
    'ppocrv6_tiny': {
        'value': 'ppocrv6_tiny',
        'label': 'PP-OCRv6 tiny det + rec',
        'description': 'Fastest OCR preset (det+rec) for CPU-first deployments with minimal memory usage.',
        'pipeline': 'ppstructurev3',
        'text_detection_model_name': 'PP-OCRv6_tiny_det',
        'text_recognition_model_name': 'PP-OCRv6_tiny_rec',
        'use_table_recognition': 'false',
    },
    'ppocrv6_tiny_structurev3': {
        'value': 'ppocrv6_tiny_structurev3',
        'label': 'PP-StructureV3 + PP-OCRv6 tiny det + rec',
        'description': 'Tiny det+rec with PP-StructureV3 layout parsing for tables/blocks.',
        'pipeline': 'ppstructurev3',
        'text_detection_model_name': 'PP-OCRv6_tiny_det',
        'text_recognition_model_name': 'PP-OCRv6_tiny_rec',
        'use_table_recognition': 'true',
    },
    'ppocrv6_small': {
        'value': 'ppocrv6_small',
        'label': 'PP-OCRv6 small det + rec',
        'description': 'Balanced OCR preset (det+rec). Mapped to the standard PP-OCRv6 model family.',
        'pipeline': 'ppstructurev3',
        'text_detection_model_name': 'PP-OCRv6_det',
        'text_recognition_model_name': 'PP-OCRv6_rec',
        'use_table_recognition': 'false',
    },
    'ppocrv6_small_structurev3': {
        'value': 'ppocrv6_small_structurev3',
        'label': 'PP-StructureV3 + PP-OCRv6 small det + rec',
        'description': 'Small det+rec with PP-StructureV3 for richer structured output.',
        'pipeline': 'ppstructurev3',
        'text_detection_model_name': 'PP-OCRv6_det',
        'text_recognition_model_name': 'PP-OCRv6_rec',
        'use_table_recognition': 'true',
    },
    'ppocrv6_medium': {
        'value': 'ppocrv6_medium',
        'label': 'PP-OCRv6 medium det + rec',
        'description': 'Higher-accuracy OCR preset (det+rec) with larger CPU footprint than small/tiny.',
        'pipeline': 'ppstructurev3',
        'text_detection_model_name': 'PP-OCRv6_medium_det',
        'text_recognition_model_name': 'PP-OCRv6_medium_rec',
        'use_table_recognition': 'false',
    },
    'ppocrv6_medium_structurev3': {
        'value': 'ppocrv6_medium_structurev3',
        'label': 'PP-StructureV3 + PP-OCRv6 medium det + rec',
        'description': 'Best structure quality preset: medium det+rec plus PP-StructureV3 for layouts/tables.',
        'pipeline': 'ppstructurev3',
        'text_detection_model_name': 'PP-OCRv6_medium_det',
        'text_recognition_model_name': 'PP-OCRv6_medium_rec',
        'use_table_recognition': 'true',
    },
    'paddlevl_1_6_0_9b': {
        'value': 'paddlevl_1_6_0_9b',
        'label': 'PaddleOCR-VL 1.6 (0.9B)',
        'description': 'Vision-language parsing profile for richer document understanding on GPU-enabled deployments.',
        'pipeline': 'paddlevl',
        'use_table_recognition': 'true',
        'text_detection_model_name': 'PaddleOCR-VL-1.6-0.9B',
        'text_recognition_model_name': 'PaddleOCR-VL-1.6-0.9B',
    },
    'openai_vision': {
        'value': 'openai_vision',
        'label': 'OpenAI-compatible Vision API',
        'description': 'Sends each PDF page as a base64 image to any OpenAI-compatible vision endpoint. Configure OPENAI_API_BASE_URL and OPENAI_API_BEARER_TOKEN.',
        'pipeline': 'openai_vision',
    },
}


def _default_runtime_settings() -> dict[str, str | int]:
    return {
        'default_profile': settings.paddle_default_profile,
        'timeout_seconds': settings.paddle_timeout_seconds,
    }


def _redis_client() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def _runtime_platform_label() -> str:
    return f"{platform.system().lower()}-{platform.machine().lower()}"


def _has_torch() -> bool:
    return importlib.util.find_spec('torch') is not None


def _has_paddle() -> bool:
    return importlib.util.find_spec('paddle') is not None


def _has_cuda() -> bool:
    try:
        import paddle  # noqa: PLC0415

        if paddle.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0:
            return True
    except Exception:
        pass

    try:
        import torch  # noqa: PLC0415
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _runtime_capability() -> dict:
    cuda_available = _has_cuda()
    info: dict = {
        'torch_available': _has_torch(),
        'paddle_available': _has_paddle(),
        'cuda_available': cuda_available,
        'selected_device': 'gpu' if cuda_available else 'cpu',
        'platform': _runtime_platform_label(),
    }
    if not cuda_available:
        info['no_cuda_reason'] = 'CUDA is unavailable in this deployment; OCR runtime will use CPU'
    return info


def get_runtime_capability() -> dict:
    return _runtime_capability()


def _paddleocr_available() -> bool:
    return importlib.util.find_spec('paddleocr') is not None


def is_paddle_available() -> bool:
    return _paddleocr_available()


def _fallback_pdf_to_markdown(source: Path) -> str:
    reader = PdfReader(str(source))
    sections: list[str] = []
    for index, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or '').strip()
        if not text:
            continue
        sections.append(f'## Page {index}\n\n{text}')

    if not sections:
        raise RuntimeError('PDF fallback extraction produced no text')
    return '\n\n'.join(sections)


def _pdf_page_count(source: Path) -> int:
    reader = PdfReader(str(source))
    return len(reader.pages)


def _to_markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    if not headers:
        return ''
    header_line = '| ' + ' | '.join(headers) + ' |'
    divider_line = '| ' + ' | '.join(['---'] * len(headers)) + ' |'
    body_lines = ['| ' + ' | '.join(row) + ' |' for row in rows]
    return '\n'.join([header_line, divider_line, *body_lines])


def _fallback_spreadsheet_to_markdown(source: Path) -> tuple[str, int, int]:
    import pandas as pd  # noqa: PLC0415

    suffix = source.suffix.lower()
    engine = 'xlrd' if suffix == '.xls' else None
    sheets = pd.read_excel(source, sheet_name=None, dtype=str, engine=engine)

    sections: list[str] = []
    sheet_count = 0
    row_count = 0

    for sheet_name, frame in sheets.items():
        if frame is None:
            continue
        frame = frame.fillna('')
        headers = [str(col).strip() or f'col_{index + 1}' for index, col in enumerate(frame.columns.tolist())]
        rows = [[str(value).replace('\n', ' ').strip() for value in record] for record in frame.values.tolist()]

        if not headers and not rows:
            continue

        table_md = _to_markdown_table(headers, rows)
        sections.append(f'## Sheet: {sheet_name}\n\n{table_md}'.strip())
        sheet_count += 1
        row_count += len(rows)

    if not sections:
        raise RuntimeError('Spreadsheet fallback extraction produced no rows')

    return '\n\n---\n\n'.join(sections), sheet_count, row_count


def _fallback_docx_to_markdown(source: Path) -> tuple[str, int]:
    """Extract plain-text paragraphs from DOCX XML as a resilient fallback."""
    try:
        with zipfile.ZipFile(source, 'r') as archive:
            xml_bytes = archive.read('word/document.xml')
    except KeyError as exc:
        raise RuntimeError('DOCX fallback extraction could not find word/document.xml') from exc
    except zipfile.BadZipFile as exc:
        raise RuntimeError('DOCX fallback extraction received an invalid DOCX file') from exc

    root = ET.fromstring(xml_bytes)
    namespace = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    body = root.find('./w:body', namespace)
    if body is None:
        raise RuntimeError('DOCX fallback extraction could not find document body')
    body_items = list(body)
    numbering = _load_docx_numbering(source, namespace)
    styles = _load_docx_styles(source, namespace)

    lines: list[str] = []
    paragraph_count = 0
    counters: dict[tuple[str, int], int] = {}
    section_index = 0
    section_child_index = 0
    section_subchild_index = 0
    section_subsubchild_index = 0
    for item in body_items:
        local_name = item.tag.rsplit('}', 1)[-1]
        if local_name == 'tbl':
            table = _docx_table_to_markdown(item, namespace)
            if table:
                lines.append(table)
            continue
        if local_name != 'p':
            continue

        textbox_lines = _docx_textbox_markdown(item, namespace)
        if textbox_lines:
            lines.extend(textbox_lines)
            continue

        text = _docx_paragraph_text(item, namespace)
        if not text:
            continue

        style_element = item.find('./w:pPr/w:pStyle', namespace)
        style_id = style_element.get(f'{{{namespace["w"]}}}val', '') if style_element is not None else ''
        style = styles.get(style_id, style_id)
        num_element = item.find('./w:pPr/w:numPr', namespace)
        num_id, level = _docx_numbering_info(num_element, namespace)
        has_explicit_numbering = (num_id, level) in numbering
        is_heading = _is_docx_major_heading(style)
        if is_heading:
            section_index += 1
            section_child_index = 0
            section_subchild_index = 0
            section_subsubchild_index = 0
            prefix = f'{_roman(section_index)}. '
            lines.append(f'# {prefix}{text}')
        elif _is_docx_bullet_style(style) and num_id is None:
            lines.append(f'- {text}')
        elif has_explicit_numbering and level == 0 and _is_docx_numbered_heading(style, text):
            section_child_index += 1
            section_subchild_index = 0
            section_subsubchild_index = 0
            lines.append(f'## {section_child_index}. {text}')
        elif has_explicit_numbering and level == 1 and _is_docx_numbered_heading(style, text):
            section_subchild_index += 1
            section_subsubchild_index = 0
            lines.append(f'### {section_child_index}.{section_subchild_index}. {text}')
        elif has_explicit_numbering and level == 2 and _is_docx_inline_number(style, item, namespace):
            section_subsubchild_index += 1
            lines.append(
                f'{section_child_index}.{section_subchild_index}.{section_subsubchild_index}. '
                f'{_docx_inline_markdown(item, namespace)}'
                f'{_DOCX_NO_PARAGRAPH_GAP if _docx_spacing_after_is_zero(item, namespace) else ""}'
            )
        elif _is_docx_subheading(text, style, num_id, level):
            section_child_index += 1
            section_subchild_index = 0
            section_subsubchild_index = 0
            lines.append(f'## {section_child_index}. {text}')
        else:
            lines.append(_docx_inline_markdown(item, namespace))
        paragraph_count += 1

    if not lines:
        raise RuntimeError('DOCX fallback extraction produced no text')

    markdown = '\n\n'.join(lines).replace(f'{_DOCX_NO_PARAGRAPH_GAP}\n\n', '  \n')
    return markdown, paragraph_count


_DOCX_NO_PARAGRAPH_GAP = '\x00DOCX_NO_PARAGRAPH_GAP\x00'


def _load_docx_styles(source: Path, namespace: dict[str, str]) -> dict[str, str]:
    styles: dict[str, str] = {}
    with zipfile.ZipFile(source, 'r') as archive:
        try:
            root = ET.fromstring(archive.read('word/styles.xml'))
        except KeyError:
            return styles
    key = f'{{{namespace["w"]}}}val'
    style_key = f'{{{namespace["w"]}}}styleId'
    for style in root.findall('./w:style', namespace):
        style_id = style.get(style_key)
        name = style.find('./w:name', namespace)
        if style_id and name is not None:
            styles[style_id] = name.get(key, style_id)
    return styles


def _load_docx_numbering(source: Path, namespace: dict[str, str]) -> dict[tuple[str, int], str]:
    numbering: dict[tuple[str, int], str] = {}
    with zipfile.ZipFile(source, 'r') as archive:
        try:
            root = ET.fromstring(archive.read('word/numbering.xml'))
        except KeyError:
            return numbering

    abstract_formats: dict[tuple[str, int], tuple[str, str]] = {}
    for abstract in root.findall('./w:abstractNum', namespace):
        abstract_id = abstract.get(f'{{{namespace["w"]}}}abstractNumId', '')
        for level in abstract.findall('./w:lvl', namespace):
            ilvl = int(level.get(f'{{{namespace["w"]}}}ilvl', '0'))
            fmt = level.find('./w:numFmt', namespace)
            text = level.find('./w:lvlText', namespace)
            abstract_formats[(abstract_id, ilvl)] = (
                fmt.get(f'{{{namespace["w"]}}}val', 'decimal') if fmt is not None else 'decimal',
                text.get(f'{{{namespace["w"]}}}val', '%1.') if text is not None else '%1.',
            )
    for num in root.findall('./w:num', namespace):
        num_id = num.get(f'{{{namespace["w"]}}}numId', '')
        abstract = num.find('./w:abstractNumId', namespace)
        abstract_id = abstract.get(f'{{{namespace["w"]}}}val', '') if abstract is not None else ''
        for (mapped_abstract, level), (fmt, pattern) in abstract_formats.items():
            if mapped_abstract == abstract_id:
                numbering[(num_id, level)] = f'{fmt}:{pattern}'
    return numbering


def _docx_numbering_info(num_element, namespace: dict[str, str]) -> tuple[str | None, int]:
    if num_element is None:
        return None, 0
    key = f'{{{namespace["w"]}}}val'
    num_id = num_element.find('./w:numId', namespace)
    level = num_element.find('./w:ilvl', namespace)
    return (
        num_id.get(key) if num_id is not None else None,
        int(level.get(key, '0')) if level is not None else 0,
    )


def _docx_spacing_after_is_zero(paragraph, namespace: dict[str, str]) -> bool:
    spacing = paragraph.find('./w:pPr/w:spacing', namespace)
    if spacing is None:
        return False
    value = spacing.get(f'{{{namespace["w"]}}}after')
    return value in {'0', '0.0'}


def _is_docx_major_heading(style: str) -> bool:
    normalized = style.lower().replace('ü', 'u')
    return 'heading' in normalized or 'berschrift' in normalized


def _is_docx_numbered_heading(style: str, text: str) -> bool:
    normalized = style.lower().replace('ä', 'a').replace('ö', 'o').replace('ü', 'u')
    return ('list' in normalized or 'aufzahlung' in normalized or 'aufzaehlung' in normalized) and len(text) < 160


def _is_docx_inline_number(style: str, paragraph, namespace: dict[str, str]) -> bool:
    return _is_docx_numbered_heading(style, _docx_paragraph_text(paragraph, namespace))


def _is_docx_bullet_style(style: str) -> bool:
    normalized = style.lower().replace('ü', 'u')
    return any(token in normalized for token in ('bullet', 'bullets', 'spiegelstrich', 'bulletpoint'))


def _docx_number_prefix(
    num_id: str,
    level: int,
    numbering: dict[tuple[str, int], str],
    counters: dict[tuple[str, int], int],
) -> str:
    for previous_level in range(level + 1):
        key = (num_id, previous_level)
        if previous_level == level:
            counters[key] = counters.get(key, 0) + 1
        elif key not in counters:
            counters[key] = 1
    counters = {key: value for key, value in counters.items() if key[0] != num_id or key[1] <= level}
    definition = numbering.get((num_id, level), 'decimal:%1.')
    fmt, pattern = definition.split(':', 1)
    values = [counters.get((num_id, index), 1) for index in range(level + 1)]
    prefix = pattern
    for index, value in enumerate(values, start=1):
        replacement = _format_docx_number(value, fmt if index == level + 1 else 'decimal')
        prefix = prefix.replace(f'%{index}', replacement)
    return prefix


def _format_docx_number(value: int, fmt: str) -> str:
    if fmt == 'lowerLetter':
        return chr(ord('a') + value - 1)
    if fmt == 'lowerRoman':
        numerals = ((1000, 'm'), (900, 'cm'), (500, 'd'), (400, 'cd'), (100, 'c'), (90, 'xc'), (50, 'l'), (40, 'xl'), (10, 'x'), (9, 'ix'), (5, 'v'), (4, 'iv'), (1, 'i'))
        result = ''
        for unit, numeral in numerals:
            result += numeral * (value // unit)
            value %= unit
        return result
    return str(value)


def _roman(value: int) -> str:
    return _format_docx_number(value, 'lowerRoman').upper()


def _is_docx_subheading(text: str, style: str, num_id: str | None, level: int) -> bool:
    return _is_docx_numbered_heading(style, text) and num_id is None and len(text) < 100


def _docx_paragraph_text(paragraph, namespace: dict[str, str]) -> str:
    raw_text = ''.join(_iter_docx_text_nodes(paragraph))
    text = '\n'.join(re.sub(r'[ \t]+', ' ', line).strip() for line in raw_text.splitlines()).strip()
    text = text.replace('\n', '  \n')
    return text


def _docx_inline_markdown(paragraph, namespace: dict[str, str]) -> str:
    parts: list[str] = []
    for run in paragraph.findall('./w:r', namespace):
        tokens: list[str] = []
        for node in run.iter():
            local_name = node.tag.rsplit('}', 1)[-1]
            if local_name == 't':
                tokens.append(node.text or '')
            elif local_name == 'tab':
                tokens.append(' ')
            elif local_name == 'br':
                tokens.append('\n')
        raw_text = ''.join(tokens)
        text = '\n'.join(re.sub(r'[ \t]+', ' ', line) for line in raw_text.split('\n'))
        if not text:
            continue
        bold_element = run.find('./w:rPr/w:b', namespace)
        bold = bold_element is not None and bold_element.get(
            f'{{{namespace["w"]}}}val', 'true'
        ) not in {'0', 'false', 'off'}
        fonts = run.find('./w:rPr/w:rFonts', namespace)
        if fonts is not None:
            font_names = ' '.join(fonts.attrib.values()).lower()
            bold = bold or 'semibold' in font_names or 'bold' in font_names
        if bold:
            leading = text[: len(text) - len(text.lstrip())]
            trailing = text[len(text.rstrip()) :]
            parts.append(f'{leading}**{text.strip()}**{trailing}')
        else:
            parts.append(text)
    return ''.join(parts).replace('\n', '  \n').strip() or _docx_paragraph_text(paragraph, namespace)


def _docx_table_to_markdown(table, namespace: dict[str, str]) -> str:
    rows: list[list[str]] = []
    for row in table.findall('./w:tr', namespace):
        cells = []
        for cell in row.findall('./w:tc', namespace):
            value = _docx_paragraph_text(cell, namespace).replace('|', '\\|')
            cells.append(value)
        if cells:
            rows.append(cells)
    if not rows:
        return ''
    width = max(len(row) for row in rows)
    rows = [row + [''] * (width - len(row)) for row in rows]
    rendered = ['| ' + ' | '.join(rows[0]) + ' |', '| ' + ' | '.join(['---'] * width) + ' |']
    rendered.extend('| ' + ' | '.join(row) + ' |' for row in rows[1:])
    return '\n'.join(rendered)


def _docx_textbox_markdown(paragraph, namespace: dict[str, str]) -> list[str]:
    """Extract the visible paragraph structure from an embedded Word text box."""
    lines: list[str] = []
    key = f'{{{namespace["w"]}}}val'

    def visit(element, inside_choice: bool = False) -> None:
        local_name = element.tag.rsplit('}', 1)[-1]
        if local_name == 'Fallback':
            return
        if local_name == 'Choice':
            inside_choice = True
        if local_name == 'txbxContent' and inside_choice:
            for child in element.findall('./w:p', namespace):
                style_element = child.find('./w:pPr/w:pStyle', namespace)
                style = style_element.get(key, '') if style_element is not None else ''
                text = _docx_paragraph_text(child, namespace)
                if not text:
                    continue
                if style == 'KastenAVBberschrift':
                    lines.append(f'# {text}')
                elif style == 'KastenAVBFlietext' and child.find('.//w:rPr/w:rFonts', namespace) is not None:
                    lines.append(f'**{text}**')
                else:
                    lines.append(text)
            return
        for child in element:
            visit(child, inside_choice)

    visit(paragraph)
    return lines


def _iter_docx_text_nodes(element):
    """Yield DOCX text nodes once, excluding duplicate AlternateContent fallbacks."""
    for child in element:
        local_name = child.tag.rsplit('}', 1)[-1]
        if local_name == 'Fallback':
            continue
        if local_name == 't':
            yield child.text or ''
            continue
        if local_name == 'br':
            yield '\n'
            continue
        yield from _iter_docx_text_nodes(child)


def _clean_block_text(value: str) -> str:
    return re.sub(r'\s+', ' ', value or '').strip()


def _html_table_to_markdown(table_html: str) -> str:
    """Convert a simple HTML table to GitHub Flavored Markdown table."""
    rows: list[list[str]] = []
    for row_match in re.finditer(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL | re.IGNORECASE):
        cells: list[str] = []
        for cell_match in re.finditer(r'<t[dh][^>]*>(.*?)</t[dh]>', row_match.group(1), re.DOTALL | re.IGNORECASE):
            cell_text = re.sub(r'<[^>]+>', '', cell_match.group(1))
            cell_text = html.unescape(cell_text)
            cell_text = re.sub(r'\s+', ' ', cell_text).strip()
            cells.append(cell_text or ' ')
        if cells:
            rows.append(cells)

    if not rows:
        return ''

    # Align all rows to the width of the widest row
    max_cols = max(len(row) for row in rows)
    rows = [row + [' '] * (max_cols - len(row)) for row in rows]

    def md_row(cells: list[str]) -> str:
        return '| ' + ' | '.join(cells) + ' |'

    lines = [md_row(rows[0])]
    lines.append('| ' + ' | '.join(['---'] * max_cols) + ' |')
    for row in rows[1:]:
        lines.append(md_row(row))
    return '\n'.join(lines)


def _render_block_content(label: str, content: str, page_number: int) -> str:
    # LLM-generated blocks already contain valid Markdown — return verbatim.
    if label == 'llm_markdown':
        return (content or '').strip()

    cleaned = _clean_block_text(content)

    if label in {'paragraph_title', 'doc_title'} and cleaned:
        return f'## {cleaned}'
    if label in {'text', 'paragraph', 'content'} and cleaned:
        return cleaned
    if label == 'table_title' and cleaned:
        return f'### {cleaned}'
    if label == 'table':
        if cleaned and '<table' in cleaned.lower():
            md_table = _html_table_to_markdown(cleaned)
            if md_table:
                return md_table
        if cleaned:
            return cleaned
        return ''
    if label in {'figure', 'image'}:
        return f'*[Figure on page {page_number}]*'
    if label in {'header', 'footer', 'footnote', 'aside_text', 'reference'}:
        if cleaned:
            return f'> {cleaned}'
        return ''
    if cleaned:
        return cleaned
    return ''


def _build_rag_frontmatter(
    source_name: str,
    page_count: int,
    profile_label: str,
    metadata: dict[str, str] | None = None,
) -> str:
    safe_name = source_name.replace('"', "'")
    metadata = metadata or {}
    mode = (metadata.get('mode') or 'single').replace('"', "'")
    email = (metadata.get('email') or '').replace('"', "'")
    department = (metadata.get('department') or '').replace('"', "'")

    lines = [
        '---',
        f'source: "{safe_name}"',
        f'pages: {page_count}',
        f'profile: "{profile_label}"',
        f'mode: "{mode}"',
        f'email: "{email}"',
    ]
    if department:
        lines.append(f'department: "{department}"')
    lines.append('---')
    return '\n'.join(lines)


def _convert_structure_to_markdown(
    page_structures: list[dict],
    source_name: str = '',
    profile_label: str = '',
    metadata: dict[str, str] | None = None,
) -> tuple[str, dict]:
    sections: list[str] = []
    block_count = 0
    labels: dict[str, int] = {}
    page_count = len(page_structures)

    frontmatter = _build_rag_frontmatter(source_name, page_count, profile_label, metadata=metadata)
    sections.append(frontmatter)

    for page_index, page in enumerate(page_structures, start=1):
        page_blocks = page.get('parsing_res_list', []) or []
        page_parts: list[str] = []

        ordered_blocks = sorted(
            page_blocks,
            key=lambda item: (
                item.get('block_order') is None,
                item.get('block_order') if item.get('block_order') is not None else 10**9,
                item.get('block_id', 10**9),
            ),
        )

        for block in ordered_blocks:
            label = str(block.get('block_label') or 'unknown')
            labels[label] = labels.get(label, 0) + 1
            rendered = _render_block_content(
                label=label,
                content=str(block.get('block_content') or ''),
                page_number=page_index,
            )
            if rendered:
                page_parts.append(rendered)
                block_count += 1

        if page_parts:
            page_header = f'<!-- page:{page_index}/{page_count} -->'
            sections.append(page_header + '\n\n' + '\n\n'.join(page_parts))

    markdown = ('\n\n---\n\n'.join(sections)).strip()
    if not markdown or markdown == frontmatter.strip():
        raise RuntimeError('Structured PP-Structure conversion produced empty markdown')
    return markdown, {
        'page_count': page_count,
        'block_count': block_count,
        'block_labels': labels,
    }


def _adaptive_pdf_chunk_page_size(
    source: Path,
    profile_id: str,
    total_pages: int,
    capability: dict,
) -> tuple[int, dict[str, int | str | bool]]:
    default_chunk = _PDF_CHUNK_PAGE_SIZE_BY_PROFILE.get(profile_id, _PDF_CHUNK_PAGE_SIZE)
    file_size_mb = source.stat().st_size / (1024 * 1024)
    cpu_only = bool(capability.get('selected_device') == 'cpu')

    # Keep quality profile, but reduce chunk size for risky large PDFs on CPU to lower peak memory.
    adaptive_chunk = default_chunk
    if cpu_only and profile_id.startswith('ppocrv6_medium'):
        if total_pages >= 20 or file_size_mb >= 30:
            adaptive_chunk = 1
        elif total_pages >= 12 or file_size_mb >= 18:
            adaptive_chunk = min(adaptive_chunk, 2)
    elif cpu_only and profile_id.startswith('ppocrv6_small'):
        if total_pages >= 40 or file_size_mb >= 45:
            adaptive_chunk = min(adaptive_chunk, 2)
        elif total_pages >= 24 or file_size_mb >= 28:
            adaptive_chunk = min(adaptive_chunk, 3)

    adaptive_chunk = max(1, adaptive_chunk)
    return adaptive_chunk, {
        'enabled': adaptive_chunk != default_chunk,
        'chunk_page_size': adaptive_chunk,
        'default_chunk_page_size': default_chunk,
        'total_pages': total_pages,
        'file_size_mb': int(file_size_mb),
        'cpu_only': cpu_only,
    }


def _paddleocr_to_structure(
    source: Path,
    profile_id: str,
    profile: dict[str, str],
    capability: dict,
) -> tuple[list[dict], dict]:
    from paddleocr import PPStructureV3  # noqa: PLC0415

    use_table_recognition = profile.get('use_table_recognition', 'false').lower() == 'true'

    pipeline = PPStructureV3(
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        use_formula_recognition=False,
        use_table_recognition=use_table_recognition,
        use_seal_recognition=False,
        use_chart_recognition=False,
        text_detection_model_name=profile['text_detection_model_name'],
        text_recognition_model_name=profile['text_recognition_model_name'],
        engine='onnxruntime',
        device='cpu',
    )
    page_structures: list[dict] = []
    raw_outputs: list[dict] = []

    def _collect_results(pred_results: list) -> None:
        for result in pred_results:
            result_json = cast(dict, result.json)
            result_markdown = cast(dict, result.markdown)
            res_payload = cast(dict | None, result_json.get('res'))
            if not res_payload:
                continue
            page_structures.append(res_payload)
            raw_outputs.append({
                'json': result_json,
                'markdown': result_markdown,
            })

    chunking_meta: dict[str, int | str | bool] = {'enabled': False, 'chunk_page_size': _PDF_CHUNK_PAGE_SIZE}

    if source.suffix.lower() == '.pdf':
        reader = PdfReader(str(source))
        total_pages = len(reader.pages)
        if total_pages == 0:
            raise RuntimeError('PDF has no pages to process')

        chunk_page_size, chunking_meta = _adaptive_pdf_chunk_page_size(
            source=source,
            profile_id=profile_id,
            total_pages=total_pages,
            capability=capability,
        )

        with TemporaryDirectory(prefix='paddledoc_pdf_chunks_') as tmpdir:
            tmpdir_path = Path(tmpdir)
            for chunk_start in range(0, total_pages, chunk_page_size):
                chunk_end = min(chunk_start + chunk_page_size, total_pages)
                writer = PdfWriter()
                for page_index in range(chunk_start, chunk_end):
                    writer.add_page(reader.pages[page_index])

                chunk_path = tmpdir_path / f'chunk_{chunk_start + 1}_{chunk_end}.pdf'
                with chunk_path.open('wb') as handle:
                    writer.write(handle)

                chunk_results = list(pipeline.predict(str(chunk_path)))
                if not chunk_results:
                    raise RuntimeError(
                        f'PaddleOCR PP-StructureV3 produced no results for PDF chunk {chunk_start + 1}-{chunk_end}'
                    )
                _collect_results(chunk_results)
    else:
        results = list(pipeline.predict(str(source)))
        if not results:
            raise RuntimeError('PaddleOCR PP-StructureV3 produced no results')
        _collect_results(results)

    if not page_structures:
        raise RuntimeError('PaddleOCR PP-StructureV3 returned no structured pages')

    return page_structures, {
        'raw_outputs': raw_outputs,
        'pdf_chunking': chunking_meta,
    }


def _openai_vision_to_structure(
    source: Path,
    profile: dict[str, str],
) -> tuple[list[dict], dict]:
    """Convert a document to structured pages by sending each page as a base64
    PNG to an OpenAI-compatible vision endpoint.

    Requires:
        OPENAI_API_BASE_URL  – e.g. https://api.openai.com or http://localhost:11434
        OPENAI_API_BEARER_TOKEN – API key / bearer token

    The endpoint is expected to be compatible with the OpenAI chat-completions API
    (POST /v1/chat/completions). The model name is taken from the profile's
    'vision_model' key (default: 'gpt-4o').
    """
    try:
        from pypdf import PdfReader, PdfWriter  # noqa: PLC0415
        import pypdfium2 as pdfium  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            f'pypdfium2 is required for the OpenAI vision pipeline: {exc}'
        ) from exc

    api_base = (settings.openai_api_base_url or '').rstrip('/')
    bearer_token = settings.openai_api_bearer_token or ''
    if not api_base:
        raise RuntimeError(
            'OPENAI_API_BASE_URL is not configured. '
            'Set it via environment variable before using the openai_vision profile.'
        )
    if not bearer_token:
        raise RuntimeError(
            'OPENAI_API_BEARER_TOKEN is not configured. '
            'Set it via environment variable before using the openai_vision profile.'
        )

    model_name = profile.get('vision_model') or 'gpt-4o'
    system_prompt = (
        'You are a precise document OCR and layout extraction assistant. '
        'Given an image of a document page, extract all text and structure faithfully. '
        'Return only well-structured Markdown. '
        'Preserve headings, bullet lists, numbered lists, and tables (as GFM tables). '
        'Do not add commentary, preamble, or explanation outside the Markdown.'
    )

    def _page_to_base64_png(pdf_path: Path, page_index: int) -> str:
        doc = pdfium.PdfDocument(str(pdf_path))
        page = doc[page_index]
        bitmap = page.render(scale=2.0)  # 144 dpi — good quality / reasonable token cost
        pil_image = bitmap.to_pil()
        import io  # noqa: PLC0415
        buf = io.BytesIO()
        pil_image.save(buf, format='PNG')
        return base64.b64encode(buf.getvalue()).decode()

    def _call_vision_api(image_b64: str, page_num: int) -> str:
        payload = {
            'model': model_name,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {
                    'role': 'user',
                    'content': [
                        {
                            'type': 'image_url',
                            'image_url': {'url': f'data:image/png;base64,{image_b64}'},
                        },
                        {
                            'type': 'text',
                            'text': f'Extract the full text and layout of page {page_num} as Markdown.',
                        },
                    ],
                },
            ],
            'max_tokens': 4096,
        }
        data = _json.dumps(payload).encode()
        req = urllib.request.Request(
            f'{api_base}/v1/chat/completions',
            data=data,
            method='POST',
            headers={
                'Content-Type': 'application/json',
                'Authorization': f'Bearer {bearer_token}',
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310
                body = _json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode(errors='replace')
            raise RuntimeError(
                f'OpenAI vision API returned HTTP {exc.code} for page {page_num}: {error_body[:400]}'
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f'OpenAI vision API unreachable ({api_base}): {exc.reason}'
            ) from exc

        choices = body.get('choices') or []
        if not choices:
            raise RuntimeError(f'OpenAI vision API returned no choices for page {page_num}')
        content = (choices[0].get('message') or {}).get('content') or ''
        return content.strip()

    suffix = source.suffix.lower()
    page_structures: list[dict] = []
    raw_outputs: list[dict] = []

    if suffix == '.pdf':
        reader = PdfReader(str(source))
        total_pages = len(reader.pages)
        if total_pages == 0:
            raise RuntimeError('PDF has no pages to process')

        for page_index in range(total_pages):
            page_num = page_index + 1
            image_b64 = _page_to_base64_png(source, page_index)
            markdown_text = _call_vision_api(image_b64, page_num)
            # Wrap output in the same page_structures schema the rest of the pipeline expects
            page_structures.append({
                'parsing_res_list': [
                    {
                        'block_label': 'llm_markdown',
                        'block_content': markdown_text,
                        'block_order': 0,
                        'block_id': 0,
                    }
                ]
            })
            raw_outputs.append({'page': page_num, 'markdown': markdown_text})
    else:
        # For non-PDF files (images) render directly
        with source.open('rb') as fh:
            image_b64 = base64.b64encode(fh.read()).decode()
        markdown_text = _call_vision_api(image_b64, 1)
        page_structures.append({
            'parsing_res_list': [
                {
                    'block_label': 'llm_markdown',
                    'block_content': markdown_text,
                    'block_order': 0,
                    'block_id': 0,
                }
            ]
        })
        raw_outputs.append({'page': 1, 'markdown': markdown_text})

    if not page_structures:
        raise RuntimeError('OpenAI vision pipeline returned no structured pages')

    return page_structures, {
        'raw_outputs': raw_outputs,
        'pdf_chunking': {'enabled': False, 'chunk_page_size': 1},
        'vision_model': model_name,
        'api_base': api_base,
    }


def _paddlevl_to_structure(
    source: Path,
    capability: dict,
) -> tuple[list[dict], dict]:
    from paddleocr import PaddleOCRVL  # noqa: PLC0415

    device = 'gpu' if capability.get('selected_device') == 'gpu' else 'cpu'
    pipeline_key = ('v1.6', device)
    cached_pipeline = _PADDLE_VL_PIPELINES.get(pipeline_key)
    if cached_pipeline is None:
        cached_pipeline = PaddleOCRVL(pipeline_version='v1.6', device=device)
        _PADDLE_VL_PIPELINES[pipeline_key] = cached_pipeline
    pipeline = cast(PaddleOCRVL, cached_pipeline)

    results = list(pipeline.predict(str(source)))
    if not results:
        raise RuntimeError('PaddleOCR-VL produced no results')

    page_structures: list[dict] = []
    raw_outputs: list[dict] = []

    for result in results:
        result_json = cast(dict, getattr(result, 'json', {}) or {})
        res_payload = cast(dict | None, result_json.get('res')) if isinstance(result_json, dict) else None
        if not isinstance(res_payload, dict):
            continue
        page_structures.append(res_payload)
        raw_outputs.append({'json': result_json})

    if not page_structures:
        raise RuntimeError('PaddleOCR-VL returned no structured pages')

    return page_structures, {
        'raw_outputs': raw_outputs,
        'pdf_chunking': {'enabled': False, 'chunk_page_size': 1},
    }


def get_paddle_status() -> tuple[str, str | None, dict | None]:
    try:
        from app.workers.tasks import probe_paddle

        task = probe_paddle.delay()
        payload = cast(dict[str, str | None], task.get(timeout=12))
        status_name = payload.get('status')
        runtime_fields = {k: v for k, v in payload.items() if k not in ('status', 'detail')}
        if status_name in {'running', 'failed', 'stopped'}:
            return status_name, payload.get('detail'), runtime_fields or None
        return 'failed', 'Unexpected probe payload from worker', None
    except CeleryTimeoutError:
        return 'stopped', 'Worker unavailable or Paddle probe timed out', None
    except Exception as exc:  # pragma: no cover
        return 'failed', str(exc), None


def get_paddle_settings() -> dict[str, str | int]:
    defaults = _default_runtime_settings()
    try:
        payload = _redis_client().hgetall(_RUNTIME_SETTINGS_KEY)
    except Exception:
        payload = {}

    if not payload:
        return defaults

    runtime = dict(defaults)
    if payload.get('default_profile'):
        runtime['default_profile'] = payload['default_profile']
    timeout_value = payload.get('timeout_seconds')
    if timeout_value is not None:
        try:
            runtime['timeout_seconds'] = max(1, int(timeout_value))
        except ValueError:
            runtime['timeout_seconds'] = defaults['timeout_seconds']
    return runtime


def update_paddle_settings(*, default_profile: str, timeout_seconds: int) -> None:
    selected_profile = default_profile.strip() if default_profile.strip() in _PADDLE_PROFILES else _DEFAULT_PROFILE_ID
    payload = {
        'default_profile': selected_profile,
        'timeout_seconds': str(timeout_seconds),
    }
    try:
        _redis_client().hset(_RUNTIME_SETTINGS_KEY, mapping=payload)
    except Exception:
        settings.paddle_default_profile = payload['default_profile']
        settings.paddle_timeout_seconds = timeout_seconds


def get_paddle_capabilities() -> dict[str, list[dict[str, str]]]:
    profile_order = [
        'no_profile',
        'ppocrv6_tiny',
        'ppocrv6_tiny_structurev3',
        'ppocrv6_small',
        'ppocrv6_small_structurev3',
        'ppocrv6_medium',
        'ppocrv6_medium_structurev3',
        'paddlevl_1_6_0_9b',
        'openai_vision',
    ]
    return {
        'profiles': [
            _PADDLE_PROFILES[profile_id]
            for profile_id in profile_order
            if profile_id in _PADDLE_PROFILES
        ],
    }


def _resolve_profile(profile_id: str | None) -> tuple[str, dict[str, str]]:
    requested_profile = (profile_id or '').strip() or cast(str, get_paddle_settings()['default_profile'])
    if requested_profile not in _PADDLE_PROFILES:
        requested_profile = _DEFAULT_PROFILE_ID
    return requested_profile, _PADDLE_PROFILES[requested_profile]


def _convert_with_native_extractors(
    source: Path,
    *,
    selected_profile_id: str,
    selected_profile: dict[str, str],
    capability: dict,
    fallback_reason: str,
    used_fallback: bool,
) -> tuple[str, dict]:
    suffix = source.suffix.lower()

    if suffix == '.pdf':
        markdown = _fallback_pdf_to_markdown(source)
        page_count = _pdf_page_count(source)
        quality_gate = evaluate_document_quality(markdown)
        return markdown, {
            'engine': 'pypdf-native' if not used_fallback else 'pypdf-fallback',
            'used_fallback': used_fallback,
            'fallback_reason': fallback_reason,
            'profile_id': selected_profile_id,
            'profile_label': selected_profile['label'],
            'page_count': page_count,
            'quality_gate': quality_gate,
            **capability,
        }

    if suffix in {'.xls', '.xlsx'}:
        markdown, sheet_count, row_count = _fallback_spreadsheet_to_markdown(source)
        quality_gate = evaluate_document_quality(markdown) 
        return markdown, {
            'engine': 'spreadsheet-native' if not used_fallback else 'spreadsheet-fallback',
            'used_fallback': used_fallback,
            'fallback_reason': fallback_reason,
            'profile_id': selected_profile_id,
            'profile_label': selected_profile['label'],
            'page_count': max(1, sheet_count),
            'sheet_count': sheet_count,
            'row_count': row_count,
            'quality_gate': quality_gate,
            **capability,
        }

    if suffix == '.docx':
        markdown, paragraph_count = _fallback_docx_to_markdown(source)
        quality_gate = evaluate_document_quality(markdown)
        return markdown, {
            'engine': 'docx-native' if not used_fallback else 'docx-fallback',
            'used_fallback': used_fallback,
            'fallback_reason': fallback_reason,
            'profile_id': selected_profile_id,
            'profile_label': selected_profile['label'],
            'page_count': 1,
            'paragraph_count': paragraph_count,
            'quality_gate': quality_gate,
            **capability,
        }

    raise RuntimeError(
        'Native extraction supports only .pdf, .docx, .xls, and .xlsx. '
        'Use an OCR profile for images and other formats.'
    )


def convert_to_markdown_with_details(
    input_path: str,
    profile_id: str | None = None,
    metadata: dict[str, str] | None = None,
) -> tuple[str, dict]:
    source = Path(input_path).resolve()
    if not source.exists():
        raise FileNotFoundError(f'Input file not found: {source}')

    selected_profile_id, selected_profile = _resolve_profile(profile_id)
    capability = _runtime_capability()

    if selected_profile_id == 'no_profile':
        return _convert_with_native_extractors(
            source,
            selected_profile_id=selected_profile_id,
            selected_profile=selected_profile,
            capability=capability,
            fallback_reason='OCR disabled by profile selection',
            used_fallback=False,
        )

    if not _paddleocr_available():
        return _convert_with_native_extractors(
            source,
            selected_profile_id=selected_profile_id,
            selected_profile=selected_profile,
            capability=capability,
            fallback_reason='PaddleOCR is not installed in this worker image',
            used_fallback=True,
        )

    try:
        selected_pipeline = selected_profile.get('pipeline', 'ppstructurev3')
        converter = 'ppstructure-json-to-rag-markdown'
        if selected_pipeline == 'openai_vision':
            page_structures, extraction_meta = _openai_vision_to_structure(source, selected_profile)
            converter = 'openai-vision-to-rag-markdown'
        elif selected_pipeline == 'paddlevl':
            page_structures, extraction_meta = _paddlevl_to_structure(source, capability)
            converter = 'paddlevl-json-to-rag-markdown'
        else:
            page_structures, extraction_meta = _paddleocr_to_structure(
                source,
                selected_profile_id,
                selected_profile,
                capability,
            )
        markdown, block_stats = _convert_structure_to_markdown(
            page_structures,
            source_name=source.name,
            profile_label=selected_profile['label'],
            metadata=metadata,
        )
        quality_gate = evaluate_document_quality(
            markdown,
            page_structures=page_structures,
            raw_outputs=cast(list[dict], extraction_meta.get('raw_outputs', [])),
            block_stats=block_stats,
        )
        return markdown, {
            'engine': 'paddleocr',
            'used_fallback': False,
            'profile_id': selected_profile_id,
            'profile_label': selected_profile['label'],
            'page_count': block_stats['page_count'],
            'profile': selected_profile,
            'structure': {
                'page_count': block_stats['page_count'],
                'block_count': block_stats['block_count'],
                'block_labels': block_stats['block_labels'],
            },
            'quality_gate': quality_gate,
            'pdf_chunking': extraction_meta.get('pdf_chunking'),
            'converter': converter,
            **capability,
        }
    except Exception as exc:
        return _convert_with_native_extractors(
            source,
            selected_profile_id=selected_profile_id,
            selected_profile=selected_profile,
            capability=capability,
            fallback_reason=str(exc),
            used_fallback=True,
        )


def convert_to_markdown(input_path: str, profile_id: str | None = None) -> str:
    markdown, _ = convert_to_markdown_with_details(input_path, profile_id=profile_id)
    return markdown