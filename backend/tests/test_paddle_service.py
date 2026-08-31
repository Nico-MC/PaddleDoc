import io
import sys
import types
from pathlib import Path
import zipfile

import pytest
import yaml

from app.models.models import VlConnection
from app.services import paddle_service, security
from app.services.quality_gate import evaluate_document_quality
from conftest import TestingSessionLocal


def test_runtime_capability_cpu_selected(monkeypatch):
    monkeypatch.setattr(paddle_service, '_has_torch', lambda: True)
    monkeypatch.setattr(paddle_service, '_has_cuda', lambda: False)

    cap = paddle_service.get_runtime_capability()
    assert cap['selected_device'] == 'cpu'
    assert cap['cuda_available'] is False


def test_convert_to_markdown_with_paddle_backend(monkeypatch, tmp_path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4 test')

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 30,
    })
    monkeypatch.setattr(paddle_service, '_paddleocr_available', lambda: True)
    monkeypatch.setattr(
        paddle_service,
        '_paddleocr_to_structure',
        lambda _source, _profile_id, _profile, _capability: (
            [
                {
                    'page_index': 0,
                    'parsing_res_list': [
                        {
                            'block_label': 'paragraph_title',
                            'block_content': 'Parsed title',
                            'block_bbox': [0, 0, 10, 10],
                            'block_id': 1,
                            'block_order': 1,
                        },
                        {
                            'block_label': 'text',
                            'block_content': 'Parsed text',
                            'block_bbox': [0, 10, 10, 20],
                            'block_id': 2,
                            'block_order': 2,
                        },
                    ],
                }
            ],
            {
                'raw_outputs': [
                    {
                        'json': {'res': {'dt_scores': [0.99, 0.97], 'rec_score': 0.98}},
                        'markdown': {'markdown': 'sample'},
                    }
                ],
                'pdf_chunking': None,
            },
        ),
    )

    markdown, details = paddle_service.convert_to_markdown_with_details(str(source), profile_id='ppocrv6_tiny')
    assert 'Parsed title' in markdown
    assert 'Parsed text' in markdown
    assert details['engine'] == 'paddleocr'
    assert details['used_fallback'] is False
    assert details['profile_id'] == 'ppocrv6_tiny'
    assert details['converter'] == 'ppstructure-json-to-rag-markdown'
    assert details['quality_gate']['grade'] in {'A', 'B', 'C'}
    assert details['quality_gate']['recommendation'] in {'allow', 'warn', 'block'}


def test_convert_to_markdown_falls_back_to_pypdf_when_paddle_missing(monkeypatch, tmp_path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4 test')

    class FakePage:
        def __init__(self, text: str):
            self._text = text

        def extract_text(self):
            return self._text

    class FakeReader:
        def __init__(self, _path: str):
            self.pages = [FakePage('Hello from PDF')]

    monkeypatch.setattr(paddle_service, 'PdfReader', FakeReader)
    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 30,
    })
    monkeypatch.setattr(paddle_service, '_paddleocr_available', lambda: False)

    markdown, details = paddle_service.convert_to_markdown_with_details(str(source), profile_id='ppocrv6_tiny')
    assert 'Hello from PDF' in markdown
    assert details['engine'] == 'pypdf-fallback'
    assert details['used_fallback'] is True
    assert details['quality_gate']['grade'] in {'A', 'B', 'C'}


def test_convert_to_markdown_falls_back_to_docx_when_paddle_missing(monkeypatch, tmp_path):
    source = tmp_path / 'sample.docx'
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>'
        '<w:p><w:r><w:t>Hello from DOCX</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p>'
        '</w:body>'
        '</w:document>'
    )
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('word/document.xml', document_xml)

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 30,
    })
    monkeypatch.setattr(paddle_service, '_paddleocr_available', lambda: False)

    markdown, details = paddle_service.convert_to_markdown_with_details(str(source), profile_id='ppocrv6_tiny')
    assert 'Hello from DOCX' in markdown
    assert details['engine'] == 'docx-fallback'
    assert details['used_fallback'] is True
    assert details['paragraph_count'] == 2
    assert details['quality_gate']['grade'] in {'A', 'B', 'C'}


def test_target_docx_preserves_native_structure():
    candidates = [
        Path(__file__).resolve().parents[2] / 'docs' / 'TypischeDokumente' / 'AZS 2512[87].docx',
        Path('/app/docs/TypischeDokumente/AZS 2512[87].docx'),
    ]
    source = next((candidate for candidate in candidates if candidate.exists()), None)
    if source is None:
        pytest.skip('Target DOCX fixture is not available')

    markdown, paragraph_count = paddle_service._fallback_docx_to_markdown(source)

    assert paragraph_count > 100
    assert markdown.count('# Tarif AZS') == 1
    assert '**für ambulante und zahnärztliche Heilbehandlung**' in markdown
    assert '1.5.2. Sehhilfen.' in markdown
    assert '1.5.3. Sehschärfenkorrektur mittels Lasertechnologie.' in markdown

    section_start = markdown.index('### 1.6. Digitale Gesundheitsanwendungen')
    section_end = markdown.index('### 1.7.', section_start)
    section = markdown[section_start:section_end]
    assert section.count('\n- ') == 3
    assert '## 2. durch vorherige' not in section
    assert '- Die digitalen Gesundheitsanwendungen müssen' not in section


def test_non_pdf_uses_paddle_profile(monkeypatch, tmp_path):
    source = tmp_path / 'sample.docx'
    source.write_bytes(b'test')

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 30,
    })
    monkeypatch.setattr(paddle_service, '_paddleocr_available', lambda: True)
    monkeypatch.setattr(
        paddle_service,
        '_paddleocr_to_structure',
        lambda _source, _profile_id, _profile, _capability: (
            [
                {
                    'page_index': 0,
                    'parsing_res_list': [
                        {
                            'block_label': 'text',
                            'block_content': 'docx parsed',
                            'block_bbox': [0, 0, 10, 10],
                            'block_id': 1,
                            'block_order': 1,
                        }
                    ],
                }
            ],
            {
                'raw_outputs': [
                    {
                        'json': {'res': {'confidence': 0.99}},
                        'markdown': {'markdown': 'docx parsed'},
                    }
                ],
                'pdf_chunking': None,
            },
        ),
    )

    markdown, details = paddle_service.convert_to_markdown_with_details(str(source), profile_id='ppocrv6_tiny')
    assert 'docx parsed' in markdown
    assert details['engine'] == 'paddleocr'
    assert details['quality_gate']['grade'] in {'A', 'B', 'C'}


def test_get_paddle_capabilities_exposes_profiles():
    caps = paddle_service.get_paddle_capabilities()
    assert any(profile['value'] == 'ppocrv6_tiny' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_small' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_medium' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_tiny_structurev3' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_small_structurev3' for profile in caps['profiles'])
    assert any(profile['value'] == 'ppocrv6_medium_structurev3' for profile in caps['profiles'])
    assert any(profile['value'] == 'paddlevl_1_6_0_9b' for profile in caps['profiles'])
    # Every static preset is 'kind': 'ocr' -- distinguishes it from the
    # dynamic 'kind': 'vl' entries appended below.
    assert all(profile['kind'] == 'ocr' for profile in caps['profiles'])


def _make_vl_connection_row(*, name: str = 'Test VL', model: str = 'vl-model', enabled: bool = True) -> VlConnection:
    db = TestingSessionLocal()
    try:
        connection = VlConnection(
            name=name,
            base_url='https://vl.example.com',
            model=model,
            api_key_encrypted=security.encrypt_vl_api_key('secret-key'),
            system_prompt='',
            enabled=enabled,
        )
        db.add(connection)
        db.commit()
        db.refresh(connection)
        db.expunge(connection)
        return connection
    finally:
        db.close()


def test_get_paddle_capabilities_appends_vl_entries_static_profiles_first():
    connection = _make_vl_connection_row(name='Prod Vision', model='gpt-4o')

    caps = paddle_service.get_paddle_capabilities(vl_connections=[connection])
    profiles = caps['profiles']

    static_count = 8  # 6 ppocrv6 presets + paddlevl_1_6_0_9b + openai_vision
    assert [p['kind'] for p in profiles[:static_count]] == ['ocr'] * static_count
    vl_entry = profiles[static_count]
    assert vl_entry == {
        'value': f'vl:{connection.id}',
        'label': 'VL: Prod Vision',
        'description': 'gpt-4o — vision-language connection',
        'kind': 'vl',
    }


def test_resolve_profile_selection_static_profile_and_unknown_static_are_no_ops():
    db = TestingSessionLocal()
    try:
        assert paddle_service.resolve_profile_selection(db, 'ppocrv6_tiny') == {}
        # Deliberately un-validated here, same as today's silent clamp
        # downstream (paddle_service._resolve_profile) -- see
        # resolve_profile_selection's docstring.
        assert paddle_service.resolve_profile_selection(db, 'not-a-real-profile') == {}
        assert paddle_service.resolve_profile_selection(db, None) == {}
    finally:
        db.close()


def test_resolve_profile_selection_vl_profile_returns_settings_shape():
    connection = _make_vl_connection_row(name='Selectable Conn')
    db = TestingSessionLocal()
    try:
        resolved = paddle_service.resolve_profile_selection(db, f'vl:{connection.id}')
    finally:
        db.close()
    assert resolved == {
        'profile_id': f'vl:{connection.id}',
        'vl_connection_id': connection.id,
        'variant_label': 'Selectable Conn',
    }


def test_resolve_profile_selection_rejects_unknown_and_disabled_vl_connection():
    disabled = _make_vl_connection_row(enabled=False)
    db = TestingSessionLocal()
    try:
        with pytest.raises(paddle_service.HTTPException) as unknown_exc:
            paddle_service.resolve_profile_selection(db, 'vl:does-not-exist')
        assert unknown_exc.value.status_code == 422
        assert unknown_exc.value.detail == "Unknown profile 'vl:does-not-exist'"

        with pytest.raises(paddle_service.HTTPException) as disabled_exc:
            paddle_service.resolve_profile_selection(db, f'vl:{disabled.id}')
        assert disabled_exc.value.status_code == 422
        assert disabled_exc.value.detail == f"Unknown profile 'vl:{disabled.id}'"
    finally:
        db.close()


def test_vl_settings_for_worker_never_raises_and_keeps_connection_id():
    db = TestingSessionLocal()
    try:
        # Static profile: no-op, same as resolve_profile_selection.
        assert paddle_service.vl_settings_for_worker(db, 'ppocrv6_tiny') == {}
        # Unknown/deleted connection: does not raise (unlike
        # resolve_profile_selection) -- still carries vl_connection_id so
        # process_job's own disabled/missing-connection check can fail just
        # that one job later (see the function's docstring).
        assert paddle_service.vl_settings_for_worker(db, 'vl:does-not-exist') == {
            'profile_id': 'vl:does-not-exist',
            'vl_connection_id': 'does-not-exist',
        }
    finally:
        db.close()

    connection = _make_vl_connection_row(name='Worker Conn')
    db = TestingSessionLocal()
    try:
        resolved = paddle_service.vl_settings_for_worker(db, f'vl:{connection.id}')
    finally:
        db.close()
    assert resolved == {
        'profile_id': f'vl:{connection.id}',
        'vl_connection_id': connection.id,
        'variant_label': 'Worker Conn',
    }


def test_effective_pipeline_profile_id_translates_vl_to_openai_vision():
    assert paddle_service.effective_pipeline_profile_id('vl:some-connection-id') == 'openai_vision'
    assert paddle_service.effective_pipeline_profile_id('ppocrv6_tiny') == 'ppocrv6_tiny'
    assert paddle_service.effective_pipeline_profile_id(None) is None


def test_convert_to_markdown_uses_paddlevl_profile(monkeypatch, tmp_path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4 test')

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 30,
    })
    monkeypatch.setattr(paddle_service, '_paddleocr_available', lambda: True)
    monkeypatch.setattr(
        paddle_service,
        '_paddlevl_to_structure',
        lambda _source, _capability: (
            [
                {
                    'page_index': 0,
                    'parsing_res_list': [
                        {
                            'block_label': 'paragraph_title',
                            'block_content': 'VL title',
                            'block_bbox': [0, 0, 10, 10],
                            'block_id': 1,
                            'block_order': 1,
                        },
                        {
                            'block_label': 'text',
                            'block_content': 'VL text',
                            'block_bbox': [0, 10, 10, 20],
                            'block_id': 2,
                            'block_order': 2,
                        },
                    ],
                }
            ],
            {
                'raw_outputs': [
                    {
                        'json': {'res': {'confidence': 0.99}},
                    }
                ],
                'pdf_chunking': {'enabled': False, 'chunk_page_size': 1},
            },
        ),
    )

    markdown, details = paddle_service.convert_to_markdown_with_details(str(source), profile_id='paddlevl_1_6_0_9b')
    assert 'VL title' in markdown
    assert 'VL text' in markdown
    assert details['engine'] == 'paddleocr'
    assert details['used_fallback'] is False
    assert details['profile_id'] == 'paddlevl_1_6_0_9b'
    assert details['converter'] == 'paddlevl-json-to-rag-markdown'


def test_convert_structure_to_markdown_renders_rag_blocks():
    markdown, stats = paddle_service._convert_structure_to_markdown(
        [
            {
                'page_index': 0,
                'parsing_res_list': [
                    {
                        'block_label': 'paragraph_title',
                        'block_content': 'Heading',
                        'block_bbox': [1, 2, 3, 4],
                        'block_id': 10,
                        'block_order': 1,
                    },
                    {
                        'block_label': 'table',
                        'block_content': '<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>',
                        'block_bbox': [5, 6, 7, 8],
                        'block_id': 11,
                        'block_order': 2,
                    },
                ],
            }
        ],
        source_name='test.pdf',
        profile_label='PP-OCRv6 tiny det + rec',
        metadata={
            'mode': 'collection',
            'email': 'team@example.com',
            'department': 'Sales',
            'profile_id': 'ppocrv6_tiny',
            'engine': 'paddleocr',
            'job_id': 'job-123',
            'document_version': 2,
            'content_sha256': 'abc123',
            'previous_job_id': 'job-122',
            'uploaded_by': 'alice',
            'team': 'Research',
            'tags': ['finance', 'invoices'],
        },
    )

    assert markdown.startswith('---\n')
    assert '## Heading' in markdown
    # yaml.safe_dump renders plain scalars unquoted (no more manual
    # f-string double-quoting).
    assert 'source: test.pdf' in markdown
    assert 'profile_id: ppocrv6_tiny' in markdown
    assert 'mode: collection' in markdown
    assert 'email: team@example.com' in markdown
    assert 'department: Sales' in markdown
    assert 'job_id: job-123' in markdown
    assert 'document_version: 2' in markdown
    assert 'content_sha256: abc123' in markdown
    assert 'previous_job_id: job-122' in markdown
    assert 'uploaded_by: alice' in markdown
    assert 'team: Research' in markdown
    assert 'tags:' in markdown and '- finance' in markdown and '- invoices' in markdown
    assert 'engine: paddleocr' in markdown
    assert 'used_fallback' not in markdown  # only included when true
    # Table rendered as markdown, not raw HTML
    assert '| A | B |' in markdown
    assert '| 1 | 2 |' in markdown
    assert '---' in markdown  # separator present
    assert stats['block_count'] == 2


def test_build_rag_frontmatter_omits_empty_optional_keys():
    frontmatter = paddle_service._build_rag_frontmatter(
        'plain.pdf', 3, 'PP-OCRv6 tiny det + rec', metadata={'engine': 'paddleocr', 'profile_id': 'ppocrv6_tiny'}
    )

    assert frontmatter.startswith('---\n')
    assert frontmatter.rstrip('\n').endswith('---')
    assert 'email:' not in frontmatter
    assert 'department:' not in frontmatter
    assert 'previous_job_id:' not in frontmatter
    assert 'uploaded_by:' not in frontmatter
    assert 'team:' not in frontmatter
    assert 'tags:' not in frontmatter
    assert 'used_fallback' not in frontmatter
    assert 'job_id: null' in frontmatter
    assert 'content_sha256: null' in frontmatter
    assert 'document_version: 1' in frontmatter


def test_build_rag_frontmatter_includes_used_fallback_only_when_true():
    frontmatter = paddle_service._build_rag_frontmatter(
        'plain.pdf', 1, 'pypdf fallback', metadata={'engine': 'pypdf-fallback', 'used_fallback': True}
    )
    assert 'used_fallback: true' in frontmatter


def _split_frontmatter(frontmatter: str) -> dict:
    """Parse a `_build_rag_frontmatter` block (`---\\n<yaml>---\\n`) with a
    real YAML parser instead of substring matching, so a value that itself
    contains a colon or a `---` line can't produce a false-positive (or
    false-negative) match. Mirrors _split_frontmatter in
    test_confluence_markdown.py, which the docstring on _build_rag_frontmatter
    calls out as the same discipline this function follows.
    """
    assert frontmatter.startswith('---\n')
    end = frontmatter.index('\n---\n', 3)
    return yaml.safe_load(frontmatter[4:end + 1])


def test_build_rag_frontmatter_includes_original_filename_when_present():
    frontmatter = paddle_service._build_rag_frontmatter(
        'a1b2c3.pdf', 2, 'PP-OCRv6 tiny det + rec',
        metadata={'engine': 'paddleocr', 'original_filename': 'Quarterly Report.pdf'},
    )
    data = _split_frontmatter(frontmatter)
    assert data['source'] == 'a1b2c3.pdf'
    assert data['original_filename'] == 'Quarterly Report.pdf'
    # Placed right after `source`, ahead of the rest of the block (A4).
    assert list(data.keys())[:2] == ['source', 'original_filename']


def test_build_rag_frontmatter_omits_original_filename_when_absent():
    frontmatter = paddle_service._build_rag_frontmatter(
        'a1b2c3.pdf', 2, 'PP-OCRv6 tiny det + rec', metadata={'engine': 'paddleocr'},
    )
    data = _split_frontmatter(frontmatter)
    assert data['source'] == 'a1b2c3.pdf'
    assert 'original_filename' not in data


@pytest.mark.parametrize(
    'hostile_filename',
    [
        'weird: name #1.pdf',
        'multi\nline\nname.pdf',
        '---\nsource: spoofed\n---.pdf',
    ],
)
def test_build_rag_frontmatter_original_filename_survives_yaml_hostile_names(hostile_filename):
    frontmatter = paddle_service._build_rag_frontmatter(
        'a1b2c3.pdf', 1, 'PP-OCRv6 tiny det + rec',
        metadata={'engine': 'paddleocr', 'original_filename': hostile_filename},
    )
    data = _split_frontmatter(frontmatter)
    # yaml.safe_dump quoting keeps the hostile filename as one scalar value
    # instead of letting it inject a sibling key or open a second document.
    assert data['original_filename'] == hostile_filename
    assert data['source'] == 'a1b2c3.pdf'


def test_convert_structure_to_markdown_bbox_coverage_no_boxes():
    _, stats = paddle_service._convert_structure_to_markdown(
        [
            {
                'page_index': 0,
                'parsing_res_list': [
                    {'block_label': 'text', 'block_content': 'no box here', 'block_id': 1, 'block_order': 1},
                    {'block_label': 'text', 'block_content': 'still no box', 'block_id': 2, 'block_order': 2},
                ],
            }
        ],
    )
    coverage = stats['bbox_coverage']
    assert coverage['blocks_total'] == 2
    assert coverage['blocks_with_bbox'] == 0
    assert coverage['keys_seen'] == []


def test_convert_structure_to_markdown_bbox_coverage_flat_list_is_counted():
    _, stats = paddle_service._convert_structure_to_markdown(
        [
            {
                'page_index': 0,
                'parsing_res_list': [
                    {
                        'block_label': 'text',
                        'block_content': 'boxed',
                        'block_bbox': [1, 2, 3, 4],
                        'block_id': 1,
                        'block_order': 1,
                    },
                ],
            }
        ],
    )
    coverage = stats['bbox_coverage']
    assert coverage['blocks_total'] == 1
    assert coverage['blocks_with_bbox'] == 1
    assert coverage['keys_seen'] == ['block_bbox']


def test_convert_structure_to_markdown_bbox_coverage_polygon_is_counted():
    _, stats = paddle_service._convert_structure_to_markdown(
        [
            {
                'page_index': 0,
                'parsing_res_list': [
                    {
                        'block_label': 'text',
                        'block_content': 'boxed',
                        'poly': [[0, 0], [10, 0], [10, 10], [0, 10]],
                        'block_id': 1,
                        'block_order': 1,
                    },
                ],
            }
        ],
    )
    coverage = stats['bbox_coverage']
    assert coverage['blocks_total'] == 1
    assert coverage['blocks_with_bbox'] == 1
    assert coverage['keys_seen'] == ['poly']


def test_convert_structure_to_markdown_bbox_coverage_reports_every_geometry_key():
    """A block carrying two geometry keys must report both.

    This is the real PaddleOCR-VL 1.6 shape: measured on a six-page scanned
    form, all 115 blocks carried `block_bbox` AND `block_polygon_points`.
    Reporting only the first match would hide the polygon -- precisely the
    richer shape a geometric label/value pairing would want.
    """
    _, stats = paddle_service._convert_structure_to_markdown(
        [
            {
                'page_index': 0,
                'parsing_res_list': [
                    {
                        'block_label': 'text',
                        'block_content': 'boxed twice',
                        'block_bbox': [0, 0, 10, 10],
                        'block_polygon_points': [[0, 0], [10, 0], [10, 10], [0, 10]],
                        'block_id': 1,
                        'block_order': 1,
                    },
                ],
            }
        ],
    )
    coverage = stats['bbox_coverage']
    assert coverage['blocks_total'] == 1
    # counted once as a block, but both key names surface
    assert coverage['blocks_with_bbox'] == 1
    assert coverage['keys_seen'] == ['block_bbox', 'block_polygon_points']


def test_convert_structure_to_markdown_bbox_coverage_broken_values_are_not_counted():
    _, stats = paddle_service._convert_structure_to_markdown(
        [
            {
                'page_index': 0,
                'parsing_res_list': [
                    {
                        'block_label': 'text', 'block_content': 'a', 'block_id': 1, 'block_order': 1,
                        'block_bbox': ['x', 'y', 'z', 'w'],  # strings, not numbers
                    },
                    {
                        'block_label': 'text', 'block_content': 'b', 'block_id': 2, 'block_order': 2,
                        'bbox': [1, 2, 3],  # flat but too short (needs >= 4)
                    },
                    {
                        'block_label': 'text', 'block_content': 'c', 'block_id': 3, 'block_order': 3,
                        'block_box': None,
                    },
                ],
            }
        ],
    )
    coverage = stats['bbox_coverage']
    assert coverage['blocks_total'] == 3
    assert coverage['blocks_with_bbox'] == 0
    assert coverage['keys_seen'] == []


# --- FEATURE: A8 -- frontmatter no longer double-separated -------------------
#
# _build_rag_frontmatter already ends its own block in a closing '---\n' YAML
# delimiter. Before A8, _convert_structure_to_markdown (and the pypdf/.eml
# fallback paths) treated that frontmatter as just another list item to join
# with '\n\n---\n\n', wedging a redundant separator right after it -- measured
# as 8 `^---$` lines across a real 6-page document (2 real YAML delimiters +
# 6 redundant join separators, one per page).

def test_prepend_frontmatter_joins_without_doubled_separator():
    frontmatter = '---\nsource: x\n---\n'
    result = paddle_service._prepend_frontmatter(frontmatter, 'body text')
    assert result == '---\nsource: x\n---\n\nbody text'
    assert '---\n\n---' not in result


def test_prepend_frontmatter_empty_body_returns_frontmatter_only():
    frontmatter = '---\nsource: x\n---\n'
    assert paddle_service._prepend_frontmatter(frontmatter, '') == frontmatter.strip()


def test_convert_structure_to_markdown_single_page_has_exactly_two_separator_lines():
    markdown, _ = paddle_service._convert_structure_to_markdown(
        [{'parsing_res_list': [{'block_label': 'text', 'block_content': 'Only page', 'block_id': 1, 'block_order': 1}]}],
    )
    # The two YAML frontmatter delimiters only -- no extra page separator is
    # needed (and none must be introduced) when there is nothing to separate
    # the frontmatter from but the page content itself.
    separator_lines = [line for line in markdown.splitlines() if line == '---']
    assert len(separator_lines) == 2


def test_convert_structure_to_markdown_two_pages_have_exactly_three_separator_lines():
    markdown, _ = paddle_service._convert_structure_to_markdown(
        [
            {'parsing_res_list': [{'block_label': 'text', 'block_content': 'Page one', 'block_id': 1, 'block_order': 1}]},
            {'parsing_res_list': [{'block_label': 'text', 'block_content': 'Page two', 'block_id': 1, 'block_order': 1}]},
        ],
    )
    # 2 YAML delimiters + exactly 1 real separator between the two pages --
    # NOT 2 + 2 (the A8 bug: a redundant separator between frontmatter and
    # the first page on top of the legitimate one between the two pages).
    separator_lines = [line for line in markdown.splitlines() if line == '---']
    assert len(separator_lines) == 3
    assert 'Page one' in markdown
    assert 'Page two' in markdown


def test_fallback_convert_with_frontmatter_does_not_double_separator(monkeypatch, tmp_path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4 test')

    class FakePage:
        def extract_text(self):
            return 'Hello from PDF'

    class FakeReader:
        def __init__(self, _path):
            self.pages = [FakePage()]

    monkeypatch.setattr(paddle_service, 'PdfReader', FakeReader)
    markdown, _ = paddle_service._fallback_convert_with_frontmatter(
        source, '.pdf', 'ppocrv6_tiny', paddle_service._PADDLE_PROFILES['ppocrv6_tiny'],
        None, 'test fallback reason', {'selected_device': 'cpu'},
    )
    separator_lines = [line for line in markdown.splitlines() if line == '---']
    assert len(separator_lines) == 2
    assert 'Hello from PDF' in markdown


def test_fallback_convert_with_frontmatter_wires_field_validation_into_quality_gate(monkeypatch, tmp_path):
    # B4 integration: field_validation.validate_document() is built and
    # tested standalone, but was not called from anywhere -- this proves the
    # fallback path actually runs it and surfaces the result through
    # evaluate_document_quality rather than silently dropping it.
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4 test')

    class FakePage:
        def extract_text(self):
            return 'IBAN: DE89370400450533013100'  # deliberately wrong checksum

    class FakeReader:
        def __init__(self, _path):
            self.pages = [FakePage()]

    monkeypatch.setattr(paddle_service, 'PdfReader', FakeReader)
    _markdown, details = paddle_service._fallback_convert_with_frontmatter(
        source, '.pdf', 'ppocrv6_tiny', paddle_service._PADDLE_PROFILES['ppocrv6_tiny'],
        None, 'test fallback reason', {'selected_device': 'cpu'},
    )
    quality_gate = details['quality_gate']
    assert any('Pruefziffer' in issue for issue in quality_gate['issues'])
    assert quality_gate['signals']['field_validation']['iban_invalid'] == 1


def test_eml_conversion_does_not_double_separator(tmp_path, monkeypatch):
    from email.mime.text import MIMEText

    msg = MIMEText('Plain body for separator check.', 'plain')
    eml_file = tmp_path / 'sep_check.eml'
    eml_file.write_bytes(msg.as_bytes())

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny', 'timeout_seconds': 30,
    })

    markdown, _ = paddle_service.convert_to_markdown_with_details(str(eml_file), profile_id='ppocrv6_tiny')
    separator_lines = [line for line in markdown.splitlines() if line == '---']
    assert len(separator_lines) == 2


# --- FEATURE: B1 wiring -- normalize_form_latex is called label-independently -

def test_convert_structure_to_markdown_applies_form_latex_normalization():
    markdown, _ = paddle_service._convert_structure_to_markdown(
        [{'parsing_res_list': [
            {
                'block_label': 'text',
                'block_content': r'Nachname: $ \underline{\text{Mueller}} $',
                'block_id': 1,
                'block_order': 1,
            },
        ]}],
    )
    assert 'Nachname: Mueller' in markdown
    assert '\\underline' not in markdown


def test_convert_structure_to_markdown_leaves_real_math_untouched():
    markdown, _ = paddle_service._convert_structure_to_markdown(
        [{'parsing_res_list': [
            {'block_label': 'text', 'block_content': r'$E = mc^2$', 'block_id': 1, 'block_order': 1},
        ]}],
    )
    assert r'$E = mc^2$' in markdown


# --- FEATURE: A3 -- checkbox glyphs unified end-to-end ------------------------

def test_convert_structure_to_markdown_unifies_checkbox_glyphs():
    markdown, _ = paddle_service._convert_structure_to_markdown(
        [{'parsing_res_list': [
            {'block_label': 'text', 'block_content': '☒ ja  ☐ nein', 'block_id': 1, 'block_order': 1},
        ]}],
    )
    assert '[x] ja [ ] nein' in markdown
    assert '☒' not in markdown and '☐' not in markdown


# --- FEATURE: B2 -- repeated header/footer/number boilerplate deduplication --
#
# Measured: 80x footer address, 47+6x "Seite X von Y", 42x "> Versicherungsnummer"
# repeated across pages -- ~18.4% of all non-empty lines. header/footer/number
# blocks measure block_order=None and sort to the end of each page (see the
# sort key in _convert_structure_to_markdown), which is why they show up as
# scattered blockquotes there; dedup tracks the pattern globally across the
# whole document regardless of that per-page position.

def test_convert_structure_to_markdown_deduplicates_repeated_footer_across_pages():
    markdown, stats = paddle_service._convert_structure_to_markdown(
        [
            {'parsing_res_list': [
                {'block_label': 'text', 'block_content': 'Page one body', 'block_id': 1, 'block_order': 1},
                {'block_label': 'footer', 'block_content': 'Musterstrasse 1, 12345 Musterstadt', 'block_id': 2, 'block_order': None},
            ]},
            {'parsing_res_list': [
                {'block_label': 'text', 'block_content': 'Page two body', 'block_id': 1, 'block_order': 1},
                {'block_label': 'footer', 'block_content': 'Musterstrasse 1, 12345 Musterstadt', 'block_id': 2, 'block_order': None},
            ]},
        ],
    )
    assert markdown.count('Musterstrasse 1, 12345 Musterstadt') == 1
    assert 'Page one body' in markdown
    assert 'Page two body' in markdown
    # The suppressed repeat does not count as a rendered block either.
    assert stats['block_count'] == 3


def test_convert_structure_to_markdown_deduplicates_page_numbers_via_digit_folding():
    """'Seite 2 von 6' and 'Seite 3 von 6' must compare equal for dedup
    purposes -- the digit-run-to-'#' normalization is what makes that work.
    """
    markdown, _ = paddle_service._convert_structure_to_markdown(
        [
            {'parsing_res_list': [
                {'block_label': 'number', 'block_content': 'Seite 2 von 6', 'block_id': 1, 'block_order': None},
            ]},
            {'parsing_res_list': [
                {'block_label': 'number', 'block_content': 'Seite 3 von 6', 'block_id': 1, 'block_order': None},
            ]},
        ],
    )
    assert 'Seite 2 von 6' in markdown
    assert 'Seite 3 von 6' not in markdown


def test_convert_structure_to_markdown_boilerplate_appearing_once_is_kept():
    """Nothing is deduplicated that only occurs once."""
    markdown, _ = paddle_service._convert_structure_to_markdown(
        [{'parsing_res_list': [
            {'block_label': 'footer', 'block_content': 'Only ever appears once', 'block_id': 1, 'block_order': None},
        ]}],
    )
    assert 'Only ever appears once' in markdown


def test_convert_structure_to_markdown_text_blocks_with_same_numbers_are_not_deduplicated():
    """Digit-folding must NOT apply to 'text'-labelled blocks -- collapsing
    it there would falsely treat two distinct field values (or one value
    genuinely repeated across pages, e.g. a total carried forward) as
    boilerplate and silently drop it.
    """
    markdown, stats = paddle_service._convert_structure_to_markdown(
        [
            {'parsing_res_list': [
                {'block_label': 'text', 'block_content': 'Betrag: 100 EUR', 'block_id': 1, 'block_order': 1},
            ]},
            {'parsing_res_list': [
                {'block_label': 'text', 'block_content': 'Betrag: 100 EUR', 'block_id': 1, 'block_order': 1},
            ]},
        ],
    )
    assert markdown.count('Betrag: 100 EUR') == 2
    assert stats['block_count'] == 2


def test_convert_structure_to_markdown_boilerplate_dedup_can_be_disabled(monkeypatch):
    """The module-level switch (no config system needed) restores the
    repeat-every-page behavior wholesale."""
    monkeypatch.setattr(paddle_service, '_DEDUPLICATE_REPEATED_BOILERPLATE', False)
    markdown, _ = paddle_service._convert_structure_to_markdown(
        [
            {'parsing_res_list': [
                {'block_label': 'footer', 'block_content': 'Repeat me', 'block_id': 1, 'block_order': None},
            ]},
            {'parsing_res_list': [
                {'block_label': 'footer', 'block_content': 'Repeat me', 'block_id': 1, 'block_order': None},
            ]},
        ],
    )
    assert markdown.count('Repeat me') == 2


def test_evaluate_document_quality_prefers_clean_high_confidence_documents():
    quality = evaluate_document_quality(
        '# Title\n\nClean document with table content.',
        page_structures=[
            {
                'parsing_res_list': [
                    {'block_label': 'paragraph_title', 'block_content': 'Title', 'block_order': 1},
                    {'block_label': 'table', 'block_content': '| A | B |', 'block_order': 2},
                ]
            }
        ],
        raw_outputs=[{'json': {'res': {'dt_scores': [0.98, 0.97], 'rec_score': 0.99}}}],
        block_stats={'page_count': 1, 'block_count': 2, 'block_labels': {'paragraph_title': 1, 'table': 1}},
    )

    assert quality['grade'] == 'A'
    assert quality['recommendation'] == 'allow'
    assert quality['score'] >= 0.9


def test_evaluate_document_quality_penalizes_noise():
    quality = evaluate_document_quality('@@@ @@ @@@\n@@@ @@ @@@\n@@@ @@ @@@')

    assert quality['grade'] == 'C'
    assert quality['recommendation'] == 'block'


# --- FEATURE: VL connection benchmarking (vl_override) ------------------------
#
# _openai_vision_to_structure requires `import pypdfium2` to succeed as an
# availability check even on the non-PDF (image) path exercised below, which
# never actually calls into it -- pypdfium2 isn't installed in this test
# environment, so a bare, empty stand-in module is injected into
# sys.modules for the duration of each test (the real `import pypdfium2`
# statement resolves from sys.modules first, before touching the loader).

def _install_fake_pypdfium2(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, 'pypdfium2', types.ModuleType('pypdfium2'))


def test_openai_vision_env_based_path_unaffected_when_vl_override_is_none(monkeypatch, tmp_path):
    _install_fake_pypdfium2(monkeypatch)
    source = tmp_path / 'sample.png'
    source.write_bytes(b'\x89PNG\r\n\x1a\nfakepng')

    calls = []

    def fake_call(*, api_base, bearer_token, model_name, system_prompt, image_b64, page_num, connection_label):
        calls.append({
            'api_base': api_base,
            'bearer_token': bearer_token,
            'model_name': model_name,
            'system_prompt': system_prompt,
            'page_num': page_num,
            'connection_label': connection_label,
        })
        return 'extracted markdown'

    monkeypatch.setattr(paddle_service, '_call_vision_chat_api', fake_call)
    monkeypatch.setattr(paddle_service.settings, 'openai_api_base_url', 'https://api.example.com')
    monkeypatch.setattr(paddle_service.settings, 'openai_api_bearer_token', 'env-token')

    page_structures, meta = paddle_service._openai_vision_to_structure(
        source, {'vision_model': 'gpt-4o-mini'}, vl_override=None
    )

    assert len(calls) == 1
    # Byte-identical to the pre-override behavior: env settings and the
    # profile's vision_model/default prompt flow through untouched.
    assert calls[0]['api_base'] == 'https://api.example.com'
    assert calls[0]['bearer_token'] == 'env-token'
    assert calls[0]['model_name'] == 'gpt-4o-mini'
    assert 'precise document OCR' in calls[0]['system_prompt']
    # No VlConnection on the env-based path -- generic, non-secret label.
    assert calls[0]['connection_label'] == 'OpenAI vision (env-configured)'
    assert meta['vision_model'] == 'gpt-4o-mini'
    assert meta['api_base'] == 'https://api.example.com'
    assert page_structures[0]['parsing_res_list'][0]['block_content'] == 'extracted markdown'


def test_openai_vision_vl_override_takes_priority_over_env(monkeypatch, tmp_path):
    _install_fake_pypdfium2(monkeypatch)
    source = tmp_path / 'sample.png'
    source.write_bytes(b'\x89PNG\r\n\x1a\nfakepng')

    calls = []

    def fake_call(*, api_base, bearer_token, model_name, system_prompt, image_b64, page_num, connection_label):
        calls.append({
            'api_base': api_base, 'bearer_token': bearer_token,
            'model_name': model_name, 'system_prompt': system_prompt,
            'connection_label': connection_label,
        })
        return 'override markdown'

    monkeypatch.setattr(paddle_service, '_call_vision_chat_api', fake_call)
    # Env is configured too, but the override must win on every field.
    monkeypatch.setattr(paddle_service.settings, 'openai_api_base_url', 'https://env.example.com')
    monkeypatch.setattr(paddle_service.settings, 'openai_api_bearer_token', 'env-token')

    override = {
        'base_url': 'https://vl-connection.internal:8080/',
        'api_key': 'connection-key',
        'model': 'qwen-vl',
        'system_prompt': 'Custom system prompt for this connection.',
        'name': 'My Internal VL Connection',
    }

    page_structures, meta = paddle_service._openai_vision_to_structure(
        source, {'vision_model': 'gpt-4o'}, vl_override=override
    )

    assert len(calls) == 1
    assert calls[0]['api_base'] == 'https://vl-connection.internal:8080'
    assert calls[0]['bearer_token'] == 'connection-key'
    assert calls[0]['model_name'] == 'qwen-vl'
    assert calls[0]['system_prompt'] == 'Custom system prompt for this connection.'
    # The connection's name, never its base_url, is what error messages show.
    assert calls[0]['connection_label'] == 'My Internal VL Connection'
    assert meta['vision_model'] == 'qwen-vl'
    assert meta['api_base'] == 'https://vl-connection.internal:8080'
    assert page_structures[0]['parsing_res_list'][0]['block_content'] == 'override markdown'


def test_call_vision_chat_api_unreachable_omits_api_base_uses_connection_label(monkeypatch, caplog):
    """The admin-configured base_url (may point at an internal/VPC-only
    host) must never appear in the RuntimeError raised here -- it lands
    verbatim in job.error_message, processing_info.execution.fallback_reason,
    and the benchmark report/export, all readable by any teammate who can
    see the job/run, not just admins. It is kept out of the worker log
    stream too (which feeds the admin Logs tab); the connection label is
    the operator-facing identifier, the URL lives in the VL connections tab."""
    secret_host = 'https://vl-internal.example.corp:9443'

    # SafeFetchError messages embed the URL that was being fetched -- exactly
    # what must not escape into the raised message or the log stream.
    _fake_safe_fetch(
        monkeypatch,
        raises=paddle_service.safe_fetch_module.SafeFetchError(
            f'connection refused fetching {secret_host!r}'
        ),
    )

    with caplog.at_level('WARNING'):
        with pytest.raises(RuntimeError) as exc_info:
            paddle_service._call_vision_chat_api(
                api_base=secret_host,
                bearer_token='secret-token',
                model_name='m',
                system_prompt='p',
                image_b64='aGk=',
                page_num=1,
                connection_label='Prod VL Connection',
            )

    message = str(exc_info.value)
    assert message == 'VL endpoint "Prod VL Connection" unreachable'
    assert secret_host not in message

    # The URL stays out of the log stream as well; the label identifies the connection.
    assert not any(secret_host in record.getMessage() for record in caplog.records)
    assert any('Prod VL Connection' in record.getMessage() for record in caplog.records)


def test_call_vision_chat_api_http_error_omits_api_base_uses_connection_label(monkeypatch, caplog):
    secret_host = 'https://vl-internal.example.corp:9443'

    _fake_safe_fetch(monkeypatch, status=500, body=b'boom')

    with caplog.at_level('WARNING'):
        with pytest.raises(RuntimeError) as exc_info:
            paddle_service._call_vision_chat_api(
                api_base=secret_host,
                bearer_token='secret-token',
                model_name='m',
                system_prompt='p',
                image_b64='aGk=',
                page_num=3,
                connection_label='Prod VL Connection',
            )

    message = str(exc_info.value)
    assert message == 'VL endpoint "Prod VL Connection" returned HTTP 500 for page 3: boom'
    assert secret_host not in message
    assert not any(secret_host in record.getMessage() for record in caplog.records)
    assert any('Prod VL Connection' in record.getMessage() for record in caplog.records)


def test_openai_vision_to_structure_propagates_connection_label_not_base_url(monkeypatch, tmp_path):
    """End-to-end wiring check (real `_call_vision_chat_api`, not mocked):
    a VlConnection's base_url must not survive into the RuntimeError that
    `_openai_vision_to_structure` lets propagate -- that message is what
    ultimately becomes job.error_message / fallback_reason and the
    benchmark report/export."""
    _install_fake_pypdfium2(monkeypatch)
    source = tmp_path / 'sample.png'
    source.write_bytes(b'\x89PNG\r\n\x1a\nfakepng')

    secret_host = 'https://vl-internal.example.corp:9443'

    # Stub safe_fetch rather than relying on the host failing to resolve --
    # otherwise this asserts nothing on a network where it does.
    _fake_safe_fetch(
        monkeypatch,
        raises=paddle_service.safe_fetch_module.SafeFetchError(f'connection refused fetching {secret_host!r}'),
    )

    override = {
        'base_url': secret_host,
        'api_key': 'k',
        'model': 'm',
        'system_prompt': '',
        'name': 'Prod VL Connection',
    }

    with pytest.raises(RuntimeError) as exc_info:
        paddle_service._openai_vision_to_structure(source, {'vision_model': 'gpt-4o'}, vl_override=override)

    message = str(exc_info.value)
    assert secret_host not in message
    assert 'Prod VL Connection' in message


def test_openai_vision_missing_config_still_raises_with_override_absent(monkeypatch, tmp_path):
    _install_fake_pypdfium2(monkeypatch)
    source = tmp_path / 'sample.png'
    source.write_bytes(b'\x89PNG\r\n\x1a\nfakepng')

    monkeypatch.setattr(paddle_service.settings, 'openai_api_base_url', '')
    monkeypatch.setattr(paddle_service.settings, 'openai_api_bearer_token', '')

    with pytest.raises(RuntimeError, match='OPENAI_API_BASE_URL'):
        paddle_service._openai_vision_to_structure(source, {}, vl_override=None)


def test_convert_to_markdown_forwards_vl_override_only_for_openai_vision(monkeypatch, tmp_path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4 test')

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny', 'timeout_seconds': 30,
    })
    monkeypatch.setattr(paddle_service, '_paddleocr_available', lambda: True)

    seen_overrides = []

    def fake_openai_vision(source_arg, profile_arg, *, vl_override=None):
        seen_overrides.append(vl_override)
        return (
            [{'parsing_res_list': [
                {'block_label': 'llm_markdown', 'block_content': 'vl text', 'block_order': 0, 'block_id': 0}
            ]}],
            {'raw_outputs': [], 'pdf_chunking': {'enabled': False, 'chunk_page_size': 1}, 'vision_model': 'x', 'api_base': 'y'},
        )

    monkeypatch.setattr(paddle_service, '_openai_vision_to_structure', fake_openai_vision)

    override = {'base_url': 'https://x', 'api_key': 'k', 'model': 'm', 'system_prompt': ''}
    markdown, details = paddle_service.convert_to_markdown_with_details(
        str(source), profile_id='openai_vision', vl_override=override
    )
    assert seen_overrides == [override]
    assert 'vl text' in markdown


def test_convert_to_markdown_ignores_vl_override_for_non_vl_profile(monkeypatch, tmp_path):
    source = tmp_path / 'sample.pdf'
    source.write_bytes(b'%PDF-1.4 test')

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny', 'timeout_seconds': 30,
    })
    monkeypatch.setattr(paddle_service, '_paddleocr_available', lambda: True)

    def fake_ppstructure(_source, _profile_id, _profile, _capability):
        return (
            [{'parsing_res_list': [
                {'block_label': 'text', 'block_content': 'ocr text', 'block_order': 0, 'block_id': 0}
            ]}],
            {'raw_outputs': [], 'pdf_chunking': None},
        )

    # A vl_override accidentally forwarded into the ppstructurev3 path would
    # raise TypeError here (it takes no such kwarg) -- this call succeeding
    # is the proof it was never passed through.
    monkeypatch.setattr(paddle_service, '_paddleocr_to_structure', fake_ppstructure)

    override = {'base_url': 'https://x', 'api_key': 'k', 'model': 'm', 'system_prompt': ''}
    markdown, details = paddle_service.convert_to_markdown_with_details(
        str(source), profile_id='ppocrv6_tiny', vl_override=override
    )
    assert 'ocr text' in markdown
    assert details['engine'] == 'paddleocr'


# --- FEATURE: VL connection admin /test probe (test_vl_connection) ------------
#
# The VL calls go through app.services.safe_fetch (SSRF protection, DNS-pinning,
# per-hop redirect checks), so these tests stub safe_fetch rather than urlopen.

def _fake_safe_fetch(monkeypatch, *, status=200, body=b'{"choices": []}', captured=None, raises=None):
    """Replace safe_fetch with a stub; record what it was called with."""
    def fake(url, *, method='GET', headers=None, body=None, timeout=5.0, max_redirects=5,
             max_bytes=2 * 1024 * 1024, allowed_private_hosts=None):
        if captured is not None:
            captured['url'] = url
            captured['method'] = method
            captured['timeout'] = timeout
            captured['auth'] = (headers or {}).get('Authorization')
            captured['allowed_private_hosts'] = allowed_private_hosts
        if raises is not None:
            raise raises
        return paddle_service.safe_fetch_module.SafeFetchResponse(
            status_code=status, headers={}, body=_fake_body, final_url=url
        )
    global _fake_body
    _fake_body = body
    monkeypatch.setattr(paddle_service.safe_fetch_module, 'safe_fetch', fake)


_fake_body = b''


def test_test_vl_connection_success(monkeypatch):
    captured = {}

    _fake_safe_fetch(monkeypatch, captured=captured)

    result = paddle_service.test_vl_connection(
        'https://vl.example.com/', 'model-x', 'key-x', 'custom prompt', timeout_seconds=5
    )
    assert result == {'ok': True, 'detail': 'Connected', 'latency_ms': result['latency_ms']}
    assert isinstance(result['latency_ms'], int)
    assert result['latency_ms'] >= 0
    assert captured['url'] == 'https://vl.example.com/v1/chat/completions'
    assert captured['timeout'] == 5
    assert captured['auth'] == 'Bearer key-x'


def test_test_vl_connection_http_error(monkeypatch):
    _fake_safe_fetch(monkeypatch, status=401, body=b'bad key')

    result = paddle_service.test_vl_connection('https://vl.example.com', 'model-x', 'bad-key', '')
    assert result['ok'] is False
    assert 'HTTP 401' in result['detail']
    # The remote body must NOT be echoed back: this endpoint would otherwise
    # be a convenient way to fingerprint internal services from the outside.
    assert 'bad key' not in result['detail']
    assert isinstance(result['latency_ms'], int)


def test_test_vl_connection_url_error(monkeypatch):
    secret_host = 'https://unreachable.example.corp:9443'
    _fake_safe_fetch(
        monkeypatch,
        raises=paddle_service.safe_fetch_module.SafeFetchError(f'blocked private address for {secret_host!r}'),
    )

    result = paddle_service.test_vl_connection(secret_host, 'model-x', 'key', '')
    assert result['ok'] is False
    assert result['detail'] == 'Endpoint unreachable or not permitted'
    assert secret_host not in result['detail']
    assert isinstance(result['latency_ms'], int)


def test_test_vl_connection_uses_safe_fetch_with_private_allowlist(monkeypatch):
    """The outbound probe must run through safe_fetch and hand it the
    configured private-host allowlist -- that is what keeps DNS-rebinding
    protection and the cloud-metadata block in force while still allowing a
    self-hosted vLLM/Ollama endpoint on the internal network."""
    captured = {}
    monkeypatch.setattr(paddle_service.settings, 'vl_private_host_allowlist', ['vl.internal:8000'])
    _fake_safe_fetch(monkeypatch, captured=captured)

    paddle_service.test_vl_connection('http://vl.internal:8000', 'model-x', 'key-x', '', timeout_seconds=7)

    assert captured['url'] == 'http://vl.internal:8000/v1/chat/completions'
    assert captured['method'] == 'POST'
    assert captured['timeout'] == 7
    assert captured['allowed_private_hosts'] == frozenset({'vl.internal:8000'})


# --- FEATURE: .eml (RFC-822 email) upload support ----------------------------

def test_eml_plain_text_body_no_attachments(tmp_path, monkeypatch):
    """Test .eml conversion with plain text body and no attachments."""
    from email.mime.text import MIMEText

    msg = MIMEText('This is the email body text.', 'plain')
    msg['Subject'] = 'Test Email'
    msg['From'] = 'sender@example.com'
    msg['To'] = 'recipient@example.com'

    eml_file = tmp_path / 'test.eml'
    eml_file.write_bytes(msg.as_bytes())

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 30,
    })

    markdown, details = paddle_service.convert_to_markdown_with_details(str(eml_file), profile_id='ppocrv6_tiny')

    # Check frontmatter
    assert markdown.startswith('---\n')
    assert 'engine: mail-eml' in markdown
    assert 'source: test.eml' in markdown
    assert 'profile_id: ppocrv6_tiny' in markdown

    # Check body is present
    assert 'This is the email body text.' in markdown
    assert details['engine'] == 'mail-eml'
    assert details['used_fallback'] is False
    assert details['page_count'] >= 1
    assert details['quality_gate']['grade'] in {'A', 'B', 'C'}


def test_eml_html_body(tmp_path, monkeypatch):
    """Test .eml conversion with HTML body."""
    from email.mime.text import MIMEText

    html_content = '<html><body><h1>Hello</h1><p>This is HTML email.</p></body></html>'
    msg = MIMEText(html_content, 'html')
    msg['Subject'] = 'HTML Email'
    msg['From'] = 'sender@example.com'

    eml_file = tmp_path / 'html_test.eml'
    eml_file.write_bytes(msg.as_bytes())

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 30,
    })

    markdown, details = paddle_service.convert_to_markdown_with_details(str(eml_file), profile_id='ppocrv6_tiny')

    # HTML should be converted to markdown
    assert 'Hello' in markdown
    assert 'This is HTML email.' in markdown
    assert details['engine'] == 'mail-eml'


def test_eml_with_supported_attachment(tmp_path, monkeypatch):
    """Test .eml with a supported attachment (PNG image).

    This test verifies that:
    1. Email body is preserved
    2. Supported attachments are recognized and processed
    3. Page count reflects attachment pages
    """
    from email.mime.text import MIMEText
    from email.mime.image import MIMEImage
    from email.mime.multipart import MIMEMultipart

    msg = MIMEMultipart()
    msg['Subject'] = 'Email with Attachment'
    msg['From'] = 'sender@example.com'
    msg['To'] = 'recipient@example.com'

    # Add text body
    text_part = MIMEText('Email with an image attachment.', 'plain')
    msg.attach(text_part)

    # Add a fake PNG image (minimal PNG header)
    fake_png = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
    img_part = MIMEImage(fake_png, _subtype='png')
    img_part.add_header('Content-Disposition', 'attachment', filename='test.png')
    msg.attach(img_part)

    eml_file = tmp_path / 'with_attachment.eml'
    eml_file.write_bytes(msg.as_bytes())

    # Mock the attachment conversion so we can verify the flow without needing actual OCR
    # We need to be careful: we only mock the internal call to convert_to_markdown_with_details
    # that _eml_to_markdown makes for attachments, not the top-level call itself
    original_convert = paddle_service.convert_to_markdown_with_details

    def selective_mock_convert(input_path, profile_id=None, metadata=None, vl_override=None):
        # If this is being called on a .png file (the attachment), mock it
        if str(input_path).endswith('.png'):
            return '# Converted PNG\n\nSome text extracted from image.', {
                'engine': 'paddleocr',
                'used_fallback': False,
                'page_count': 1,
                'quality_gate': {'grade': 'A', 'recommendation': 'allow'},
                'profile_id': profile_id or 'ppocrv6_tiny',
            }
        # Otherwise use the real function (for the .eml file itself)
        return original_convert(input_path, profile_id, metadata, vl_override)

    monkeypatch.setattr(paddle_service, 'convert_to_markdown_with_details', selective_mock_convert)
    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 30,
    })

    markdown, details = paddle_service.convert_to_markdown_with_details(str(eml_file), profile_id='ppocrv6_tiny')

    # Check email body and attachment section are present
    assert 'Email with an image attachment.' in markdown
    assert '## Attachment: test.png' in markdown
    assert 'Converted PNG' in markdown
    assert details['engine'] == 'mail-eml'
    assert details['page_count'] >= 1


def test_eml_with_unsupported_attachment(tmp_path, monkeypatch):
    """Test .eml with unsupported attachment (.xyz) marked as skipped."""
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email import encoders

    msg = MIMEMultipart()
    msg['Subject'] = 'Email with Unsupported Attachment'
    msg['From'] = 'sender@example.com'

    # Add text body
    text_part = MIMEText('Email body with unsupported file.', 'plain')
    msg.attach(text_part)

    # Add an unsupported file type
    unsupported_part = MIMEBase('application', 'x-xyz')
    unsupported_part.set_payload(b'unsupported binary data')
    encoders.encode_base64(unsupported_part)
    unsupported_part.add_header('Content-Disposition', 'attachment', filename='document.xyz')
    msg.attach(unsupported_part)

    eml_file = tmp_path / 'with_unsupported.eml'
    eml_file.write_bytes(msg.as_bytes())

    # No need to mock if we're not calling convert_to_markdown_with_details recursively
    # but for consistency, we can keep the monkeypatch calls
    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 30,
    })

    markdown, details = paddle_service.convert_to_markdown_with_details(str(eml_file), profile_id='ppocrv6_tiny')

    # Check email body is present
    assert 'Email body with unsupported file.' in markdown
    # Check unsupported attachment is noted as skipped
    assert 'skipped:' in markdown.lower()
    assert 'document.xyz' in markdown
    assert 'unsupported_type' in markdown
    assert details['engine'] == 'mail-eml'


def test_eml_conversion_includes_standard_frontmatter(tmp_path, monkeypatch):
    """Test that .eml produces markdown with all standard frontmatter keys."""
    from email.mime.text import MIMEText

    msg = MIMEText('Test body', 'plain')
    msg['Subject'] = 'Frontmatter Test'
    msg['From'] = 'test@example.com'

    eml_file = tmp_path / 'frontmatter_test.eml'
    eml_file.write_bytes(msg.as_bytes())

    monkeypatch.setattr(paddle_service, 'get_paddle_settings', lambda: {
        'default_profile': 'ppocrv6_tiny',
        'timeout_seconds': 30,
    })

    metadata = {
        'job_id': 'test-job-123',
        'document_version': 1,
        'content_sha256': 'abc123def456',
        'profile_id': 'ppocrv6_tiny',
        'engine': 'mail-eml',
    }

    markdown, _ = paddle_service.convert_to_markdown_with_details(
        str(eml_file), profile_id='ppocrv6_tiny', metadata=metadata
    )

    # Parse frontmatter
    assert markdown.startswith('---\n')
    parts = markdown.split('---\n', 2)
    assert len(parts) >= 3
    frontmatter = parts[1]

    # Check standard keys
    assert 'source: frontmatter_test.eml' in frontmatter
    assert 'engine: mail-eml' in frontmatter
    assert 'pages:' in frontmatter
    assert 'profile_id: ppocrv6_tiny' in frontmatter
    assert 'job_id: test-job-123' in frontmatter
    assert 'document_version: 1' in frontmatter
    assert 'content_sha256: abc123def456' in frontmatter
    assert 'processed_at:' in frontmatter
