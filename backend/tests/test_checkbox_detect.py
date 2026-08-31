"""Tests for app/services/checkbox_detect.py (CB1).

`match_box_to_label` / `classify_checkboxes` are pure data-structure logic
and run unconditionally in the backend image. `detect_box_candidates`
needs cv2/numpy/pypdfium2, which the backend image (where this suite runs)
does not carry -- its tests are guarded with `pytest.importorskip('cv2')`
so they only execute where those libraries are actually present (worker
image), per the CB1 task's HARTE EINSCHRAENKUNG.
"""

from __future__ import annotations

import pytest

from app.services.checkbox_detect import (
    classify_checkboxes,
    detect_box_candidates,
    match_box_to_label,
)


# --- _y_overlap (indirectly, via match_box_to_label) --------------------------

def test_match_box_to_label_picks_nearest_block_on_same_line():
    box_bbox = [100.0, 200.0, 120.0, 220.0]
    text_blocks = [
        {'block_id': 1, 'block_bbox': [130.0, 202.0, 300.0, 218.0], 'block_content': 'ja'},
        {'block_id': 2, 'block_bbox': [400.0, 202.0, 600.0, 218.0], 'block_content': 'far away'},
    ]
    match = match_box_to_label(box_bbox, text_blocks)
    assert match is not None
    assert match['block_id'] == 1


def test_match_box_to_label_ignores_blocks_to_the_left():
    # A label block that sits to the left of the box (the table-cell layout
    # measured in the sample PDFs, e.g. "Police der Reiseversicherung | ☒ | ☐")
    # must not be picked -- the spec is explicitly "rechts davon" (to its right).
    box_bbox = [200.0, 200.0, 220.0, 220.0]
    text_blocks = [
        {'block_id': 1, 'block_bbox': [10.0, 202.0, 190.0, 218.0], 'block_content': 'Nachweis...'},
    ]
    assert match_box_to_label(box_bbox, text_blocks) is None


def test_match_box_to_label_ignores_blocks_on_a_different_line():
    box_bbox = [100.0, 200.0, 120.0, 220.0]
    text_blocks = [
        # starts to the right, but two lines further down -- no vertical overlap
        {'block_id': 1, 'block_bbox': [130.0, 400.0, 300.0, 420.0], 'block_content': 'unrelated'},
    ]
    assert match_box_to_label(box_bbox, text_blocks) is None


def test_match_box_to_label_skips_blocks_without_usable_bbox():
    box_bbox = [100.0, 200.0, 120.0, 220.0]
    text_blocks = [
        {'block_id': 1, 'block_content': 'no bbox at all'},
        {'block_id': 2, 'block_bbox': [130.0, 202.0, 300.0, 218.0], 'block_content': 'ja'},
    ]
    match = match_box_to_label(box_bbox, text_blocks)
    assert match is not None
    assert match['block_id'] == 2


def test_match_box_to_label_returns_none_for_empty_text_blocks():
    assert match_box_to_label([0.0, 0.0, 10.0, 10.0], []) is None


def test_match_box_to_label_picks_closest_of_several_candidates():
    box_bbox = [0.0, 0.0, 10.0, 10.0]
    text_blocks = [
        {'block_id': 'far', 'block_bbox': [200.0, 0.0, 300.0, 10.0]},
        {'block_id': 'near', 'block_bbox': [15.0, 0.0, 50.0, 10.0]},
        {'block_id': 'mid', 'block_bbox': [80.0, 0.0, 150.0, 10.0]},
    ]
    match = match_box_to_label(box_bbox, text_blocks)
    assert match is not None
    assert match['block_id'] == 'near'


# --- classify_checkboxes ------------------------------------------------------

def test_classify_checkboxes_marks_checked_at_or_above_threshold():
    candidates = [
        {'bbox': [0.0, 0.0, 20.0, 20.0], 'fill_ratio': 0.6},
        {'bbox': [0.0, 100.0, 20.0, 120.0], 'fill_ratio': 0.05},
    ]
    text_blocks = [
        {'block_id': 1, 'block_bbox': [25.0, 2.0, 100.0, 18.0], 'block_content': 'ja'},
        {'block_id': 2, 'block_bbox': [25.0, 102.0, 100.0, 118.0], 'block_content': 'nein'},
    ]
    results = classify_checkboxes(candidates, text_blocks, fill_threshold=0.3)
    assert [r['checked'] for r in results] == [True, False]
    assert [r['rendered'] for r in results] == ['[x]', '[ ]']
    assert results[0]['label_text'] == 'ja'
    assert results[1]['label_text'] == 'nein'


def test_classify_checkboxes_boundary_value_counts_as_checked():
    # fill_ratio == threshold: spec says ">= fill_threshold" is checked.
    candidates = [{'bbox': [0.0, 0.0, 10.0, 10.0], 'fill_ratio': 0.3}]
    result = classify_checkboxes(candidates, [], fill_threshold=0.3)[0]
    assert result['checked'] is True


def test_classify_checkboxes_unmatched_box_gets_none_label():
    candidates = [{'bbox': [0.0, 0.0, 10.0, 10.0], 'fill_ratio': 0.9}]
    result = classify_checkboxes(candidates, [], fill_threshold=0.3)[0]
    assert result['label_block_id'] is None
    assert result['label_text'] is None
    assert result['checked'] is True


def test_classify_checkboxes_preserves_input_order_and_count():
    candidates = [
        {'bbox': [0.0, float(i) * 30, 10.0, float(i) * 30 + 10], 'fill_ratio': 0.1 * i}
        for i in range(5)
    ]
    results = classify_checkboxes(candidates, [], fill_threshold=0.3)
    assert len(results) == 5
    assert [r['fill_ratio'] for r in results] == [0.0, 0.1, 0.2, 0.30000000000000004, 0.4]


def test_classify_checkboxes_empty_candidates_returns_empty_list():
    assert classify_checkboxes([], [], fill_threshold=0.3) == []


# --- detect_box_candidates (needs cv2/numpy/pypdfium2) ------------------------

def test_detect_box_candidates_needs_image_libs(tmp_path):
    pytest.importorskip('cv2')
    pytest.importorskip('numpy')
    pytest.importorskip('pypdfium2')
    import pypdfium2 as pdfium

    # Build a minimal blank one-page PDF so the function has something to
    # render -- an empty page has no square contours, so we only assert it
    # runs cleanly end-to-end and returns a (possibly empty) list, not that
    # it finds anything specific. Detection quality itself was validated by
    # the CB1 measurement script against the real sample PDFs, not here.
    doc = pdfium.PdfDocument.new()
    doc.new_page(200, 200)
    pdf_path = tmp_path / 'blank.pdf'
    doc.save(str(pdf_path))

    result = detect_box_candidates(str(pdf_path), 0)
    assert isinstance(result, list)
