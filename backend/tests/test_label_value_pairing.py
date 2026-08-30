"""Unit tests for the B3 geometric label/value pairing in
app/services/paddle_service.py (`_pair_label_value_blocks` and its wiring
into `_convert_structure_to_markdown`).

Fixture geometry is modeled on the real dump measured for this task
(HanseMerkur form, page 2, six-page scan, 1191x1682 page): a text block
"Buchungsdatum:" (block_bbox [128, 297, 273, 324]) sits next to an
inline_formula block carrying the actual date (block_bbox
[398, 293, 631, 341]), decoupled by PaddleOCR-VL into two separate blocks
with no group_id relationship between them. The Nachname:/Vorname: and
"value with no bbox" fixtures use bbox coordinates in the same measured
scale (row height ~25-30px, page width ~1191px) since the real broken
side-by-side-labels document is not the one this dump was taken from.
"""

from __future__ import annotations

from app.services import paddle_service

convert = paddle_service._convert_structure_to_markdown


def _block(label: str, content: str, block_id: int, order: int | None, bbox: list[float] | None = None) -> dict:
    block: dict = {'block_label': label, 'block_content': content, 'block_id': block_id, 'block_order': order}
    if bbox is not None:
        block['block_bbox'] = bbox
    return block


# --- the measured pattern: label and value decoupled, value to the right ----

def test_pairs_label_with_value_across_the_measured_gap():
    """The exact 'Buchungsdatum:' / inline_formula case (groups.json, page 2,
    ord=2/ord=3): label and value overlap in y and the value sits 125px to
    the label's right -- well inside the calibrated gap threshold."""
    markdown, _ = convert([
        {'parsing_res_list': [
            _block('text', 'Buchungsdatum:', 1, 1, [128.0, 297.0, 273.0, 324.0]),
            _block('inline_formula', '20 01 2026', 2, 2, [398.0, 293.0, 631.0, 341.0]),
        ]},
    ])
    assert 'Buchungsdatum: 20 01 2026' in markdown
    # The value must not also survive as its own separate line.
    assert markdown.count('20 01 2026') == 1
    assert '\nBuchungsdatum:\n' not in markdown


def test_unpaired_when_geometry_disagrees_column_gap_too_wide():
    """trap 3: a second-column value (ord=4, block_bbox x0=804) sitting far
    to the right of the SAME label must not be pulled in just because it is
    also somewhere to the right -- the gap (531px, 44.6% of the page) is
    well past the calibrated 25% threshold, unlike the real value at 125px
    (10.5%)."""
    markdown, _ = convert([
        {'parsing_res_list': [
            _block('text', 'Buchungsdatum:', 1, 1, [128.0, 297.0, 273.0, 324.0]),
            _block('inline_formula', '20 01 2026', 2, 2, [398.0, 293.0, 631.0, 341.0]),
            _block('inline_formula', '10 06 2026', 3, 3, [804.0, 295.0, 1036.0, 341.0]),
        ]},
    ])
    assert 'Buchungsdatum: 20 01 2026' in markdown
    # The far column's value stays on its own -- never appended to the
    # label, and never silently dropped either.
    assert 'Buchungsdatum: 20 01 2026 10 06 2026' not in markdown
    assert '10 06 2026' in markdown


def test_unpaired_when_value_is_on_a_different_row():
    """A value block with no y-overlap to any label (a different form row
    entirely) must stay unpaired rather than reach for the nearest label
    regardless of vertical distance."""
    markdown, _ = convert([
        {'parsing_res_list': [
            _block('text', 'Buchungsdatum:', 1, 1, [128.0, 297.0, 273.0, 324.0]),
            _block('display_formula', '15 06 2026', 2, 2, [398.0, 348.0, 630.0, 396.0]),
        ]},
    ])
    assert 'Buchungsdatum: 15 06 2026' not in markdown
    assert 'Buchungsdatum:' in markdown
    assert '15 06 2026' in markdown


# --- trap 1: side-by-side labels -------------------------------------------

def test_side_by_side_label_is_never_consumed_as_a_value():
    """'Nachname:' and 'Vorname:' sit next to each other on the same row.
    A naive "nearest block to the right" rule would swallow 'Vorname:' as
    Nachname's value; excluding anything that itself ends in ':' from the
    value-candidate pool defeats that regardless of distance/overlap."""
    markdown, _ = convert([
        {'parsing_res_list': [
            _block('text', 'Nachname:', 1, 1, [128.0, 618.0, 260.0, 645.0]),
            _block('text', 'Vorname:', 2, 2, [300.0, 618.0, 420.0, 645.0]),
        ]},
    ])
    assert 'Nachname: Vorname:' not in markdown
    assert 'Nachname:' in markdown
    assert 'Vorname:' in markdown


def test_a_blank_field_does_not_steal_the_next_labels_value():
    """'Nachname:' has no value of its own (a blank field left empty on the
    scanned form -- no OCR block for it at all); 'Vorname:' sits right next
    to it and DOES have a value further along the same row. The nearest-gap
    search alone would let 'Nachname:' reach across 'Vorname:' and steal
    'Peter' just because it is still within the generic gap budget for this
    (narrow) row -- a wrong pairing, worse than leaving Nachname unpaired.
    An intervening label must block that reach."""
    markdown, _ = convert([
        {'parsing_res_list': [
            _block('text', 'Nachname:', 1, 1, [128.0, 618.0, 200.0, 645.0]),
            _block('text', 'Vorname:', 2, 2, [210.0, 618.0, 280.0, 645.0]),
            _block('text', 'Peter', 3, 3, [290.0, 618.0, 360.0, 645.0]),
        ]},
    ])
    assert 'Vorname: Peter' in markdown
    assert 'Nachname: Peter' not in markdown
    assert 'Nachname: Vorname:' not in markdown
    assert markdown.count('Peter') == 1


def test_a_label_does_not_reach_past_a_closer_label_for_a_farther_value():
    """Same row as above, plus a real value further right that actually
    belongs to 'Vorname:'. 'Nachname:' must not steal it just because it is
    still "to the right" -- the gap from Nachname alone (180px, > the 25%-
    of-520px = 130px threshold this page's estimated width implies) is
    already enough to keep it out, and 'Vorname:' -- the closer, correct
    label -- must still get it."""
    markdown, _ = convert([
        {'parsing_res_list': [
            _block('text', 'Nachname:', 1, 1, [128.0, 618.0, 260.0, 645.0]),
            _block('text', 'Vorname:', 2, 2, [300.0, 618.0, 420.0, 645.0]),
            _block('text', 'Peter', 3, 3, [440.0, 618.0, 520.0, 645.0]),
        ]},
    ])
    assert 'Vorname: Peter' in markdown
    assert 'Nachname: Peter' not in markdown
    assert markdown.count('Peter') == 1


# --- trap 2: value with no matching label -----------------------------------

def test_value_with_no_label_anywhere_stays_unpaired():
    markdown, _ = convert([
        {'parsing_res_list': [
            _block('inline_formula', '10 06 2026', 1, 1, [804.0, 295.0, 1036.0, 341.0]),
        ]},
    ])
    assert '10 06 2026' in markdown


# --- blocks without geometry take the old, unpaired path --------------------

def test_blocks_without_bbox_are_never_paired():
    """Engines/older jobs that don't emit block_bbox at all must fall back
    to the pre-B3 one-block-per-line behavior -- pairing requires geometry
    on both halves."""
    markdown, _ = convert([
        {'parsing_res_list': [
            _block('text', 'Buchungsdatum:', 1, 1),
            _block('inline_formula', '20 01 2026', 2, 2),
        ]},
    ])
    assert 'Buchungsdatum: 20 01 2026' not in markdown
    assert 'Buchungsdatum:' in markdown
    assert '20 01 2026' in markdown


def test_mixed_bbox_and_no_bbox_block_only_the_geometric_one_can_pair():
    """A label with real geometry must not accidentally pair with a value
    that has none -- there is nothing to measure overlap/gap against."""
    markdown, _ = convert([
        {'parsing_res_list': [
            _block('text', 'Buchungsdatum:', 1, 1, [128.0, 297.0, 273.0, 324.0]),
            _block('inline_formula', '20 01 2026', 2, 2),
        ]},
    ])
    assert 'Buchungsdatum: 20 01 2026' not in markdown
    assert 'Buchungsdatum:' in markdown
    assert '20 01 2026' in markdown


# --- module-level off switch -------------------------------------------------

def test_pairing_can_be_disabled(monkeypatch):
    monkeypatch.setattr(paddle_service, '_PAIR_LABEL_VALUE_GEOMETRY', False)
    markdown, _ = convert([
        {'parsing_res_list': [
            _block('text', 'Buchungsdatum:', 1, 1, [128.0, 297.0, 273.0, 324.0]),
            _block('inline_formula', '20 01 2026', 2, 2, [398.0, 293.0, 631.0, 341.0]),
        ]},
    ])
    assert 'Buchungsdatum: 20 01 2026' not in markdown
    assert 'Buchungsdatum:' in markdown
    assert '20 01 2026' in markdown


# --- block_count stays a pre-merge count of rendered blocks ------------------

def test_block_count_counts_pre_merge_rendered_blocks_not_final_lines():
    """Pairing folds two rendered blocks into one output line, but the
    bbox-coverage/labels probe (`stats['block_count']`) should keep
    reflecting how many source blocks actually rendered content -- the same
    contract `_DEDUPLICATE_REPEATED_BOILERPLATE` already keeps for
    suppressed repeats."""
    _, stats = convert([
        {'parsing_res_list': [
            _block('text', 'Buchungsdatum:', 1, 1, [128.0, 297.0, 273.0, 324.0]),
            _block('inline_formula', '20 01 2026', 2, 2, [398.0, 293.0, 631.0, 341.0]),
        ]},
    ])
    assert stats['block_count'] == 2
