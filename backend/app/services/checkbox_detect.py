"""CB1: checkbox detection for scanned form pages.

Ground rule: NOT WIRED INTO THE PIPELINE. This module is a measured
prototype only -- see the CB1 task brief. The decision whether to plug it
into `paddle_service.py` belongs to whoever reads the measurement results
in the task's write-up, not to this code.

Measured baseline this exists to fix: of 11 checkable ticked checkboxes in
the sample documents, the PaddleOCR-VL pipeline recognizes 1. Empty boxes
come through as literal `□`/`☐` glyphs about 83% of the time;
filled ones mostly vanish. The layout detector has no checkbox class at all
(measured: 11 distinct block_label values on the sample set, none of them
checkbox-shaped) -- so a checkbox candidate can only come from the *page
image*, never from `parsing_res_list`.

Two independent halves, split for testability:

* `match_box_to_label` / `classify_checkboxes` -- pure data-structure logic
  (lists/dicts/floats only). No image library import anywhere in this half,
  so it runs in the backend image, which has neither cv2 nor numpy nor
  pypdfium2. This is the half that would move into the pipeline if the
  measurement supports it.
* `detect_box_candidates` -- looks at the actual rendered page (contours,
  fill ratio). Lazy-imports cv2/numpy/pypdfium2 *inside* the function, house
  style already established by `_page_to_base64_png` in paddle_service.py,
  because the backend image (where tests run) carries none of the three.
"""

from __future__ import annotations

from collections.abc import Sequence

# A checkbox glyph in the sample forms measures roughly 15-25px on a side at
# the 2.0 render scale `_page_to_base64_png` uses (144 dpi). Kept as a public
# default rather than hardcoded so a caller can retune for a different scale
# without editing this module.
DEFAULT_MIN_BOX_PX = 15
DEFAULT_MAX_BOX_PX = 25


def _y_overlap(box_a: Sequence[float], box_b: Sequence[float]) -> float:
    """Vertical overlap between two bboxes, as a fraction of the shorter one.

    0 when the two don't share any vertical extent, up to 1 when the
    shorter box's whole height sits inside the taller one. Used to decide
    whether a text block is "on the same line" as a checkbox candidate
    rather than in the row above or below it -- a plain center-distance
    check would happily pair a checkbox with a text block one line down.
    """
    top = max(box_a[1], box_b[1])
    bottom = min(box_a[3], box_b[3])
    if bottom <= top:
        return 0.0
    overlap = bottom - top
    shorter = min(box_a[3] - box_a[1], box_b[3] - box_b[1])
    if shorter <= 0:
        return 0.0
    return overlap / shorter


def match_box_to_label(
    box_bbox: Sequence[float],
    text_blocks: Sequence[dict],
    *,
    min_y_overlap: float = 0.3,
) -> dict | None:
    """Return the nearest text block to the right of a checkbox candidate.

    Per the task brief: "ordne jeden Kasten dem naechstliegenden Textblock
    rechts davon zu" -- assign each box to the closest text block on its
    right. Restricted to blocks sharing the box's line (`min_y_overlap`)
    so a box doesn't get paired with unrelated text one row away just
    because that text happens to start further right. Returns None when no
    text block qualifies (e.g. a box in the page margin with nothing next
    to it -- a real case worth surfacing as "unmatched" rather than
    guessing).
    """
    box_right = box_bbox[2]
    best: dict | None = None
    best_distance: float | None = None
    for block in text_blocks:
        bbox = block.get('block_bbox')
        if not bbox or len(bbox) != 4:
            continue
        if bbox[0] < box_right:
            continue  # label must start at or after the box's right edge
        if _y_overlap(box_bbox, bbox) < min_y_overlap:
            continue
        distance = bbox[0] - box_right
        if best_distance is None or distance < best_distance:
            best_distance = distance
            best = block
    return best


def classify_checkboxes(
    candidates: Sequence[dict],
    text_blocks: Sequence[dict],
    *,
    fill_threshold: float,
    min_y_overlap: float = 0.3,
) -> list[dict]:
    """Pair each box candidate with its label and decide `[x]` vs `[ ]`.

    `candidates` are plain dicts with `bbox` ([x0, y0, x1, y1]) and
    `fill_ratio` (0..1 share of dark pixels inside the box, as produced by
    `detect_box_candidates`). A box is "checked" when its fill_ratio meets
    or exceeds `fill_threshold` -- the threshold is a parameter, not a
    constant, because the CB1 measurement found no single value that cleanly
    separates checked from empty across the sample set (see the module's
    task write-up); callers must supply one deliberately rather than rely
    on a baked-in guess.
    """
    results = []
    for box in candidates:
        bbox = box['bbox']
        label_block = match_box_to_label(bbox, text_blocks, min_y_overlap=min_y_overlap)
        checked = box['fill_ratio'] >= fill_threshold
        results.append({
            'bbox': bbox,
            'fill_ratio': box['fill_ratio'],
            'checked': checked,
            'rendered': '[x]' if checked else '[ ]',
            'label_block_id': label_block.get('block_id') if label_block else None,
            'label_text': label_block.get('block_content') if label_block else None,
        })
    return results


def detect_box_candidates(
    pdf_path: str,
    page_index: int,
    *,
    min_size_px: int = DEFAULT_MIN_BOX_PX,
    max_size_px: int = DEFAULT_MAX_BOX_PX,
    scale: float = 2.0,
) -> list[dict]:
    """Find square-ish contours on a rendered PDF page and their fill ratio.

    Not covered by the backend-image test run (needs cv2/numpy/pypdfium2,
    which that image doesn't carry -- see HARTE EINSCHRAENKUNG in the task
    brief); exercised via `pytest.importorskip('cv2')` so it still runs
    wherever those libraries are present, and by the standalone measurement
    script run in the worker container (see the CB1 task write-up).

    Pipeline: rasterize the page at `scale`, Otsu-binarize, find contours,
    keep only the ones whose bounding box is roughly square and within
    [min_size_px, max_size_px] on both sides (rules out table rule lines,
    letters, and large figures in one pass), then measure what fraction of
    each box's interior is dark. Returns bbox + fill_ratio per candidate;
    deciding checked/empty is `classify_checkboxes`'s job, not this one --
    keeps the "what counts as filled" threshold out of the image-touching
    code so it can be picked (or the whole detector rejected) from the
    measurement numbers alone.
    """
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    import pypdfium2 as pdfium  # noqa: PLC0415

    doc = pdfium.PdfDocument(pdf_path)
    page = doc[page_index]
    bitmap = page.render(scale=scale)
    gray = np.array(bitmap.to_pil().convert('L'))
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    candidates = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if not (min_size_px <= w <= max_size_px and min_size_px <= h <= max_size_px):
            continue
        aspect = w / h if h else 0.0
        if not (0.7 <= aspect <= 1.4):
            continue  # rules out thin table rule lines and wide dashes
        roi = binary[y:y + h, x:x + w]
        fill_ratio = float(np.count_nonzero(roi)) / float(roi.size)
        candidates.append({
            'bbox': [float(x), float(y), float(x + w), float(y + h)],
            'fill_ratio': fill_ratio,
        })
    return candidates
