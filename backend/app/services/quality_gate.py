from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


_QUALITY_THRESHOLD_A = 0.9
_QUALITY_THRESHOLD_B = 0.75
# A2: exact key match only -- the old substring-hint approach ('conf' matching
# 'preprocessor_config') vacuumed up unrelated numeric fields the moment any
# ancestor dict key merely contained a hint, and that context never reset on
# the way back down. Exact names measured across the PaddleOCR-VL, pypdf and
# openai_vision raw_outputs shapes.
_SCORE_KEYS = frozenset({
    'confidence',
    'confidences',
    'score',
    'scores',
    'rec_score',
    'rec_scores',
    'dt_score',
    'dt_scores',
    'det_score',
    'det_scores',
})
# A7: the old ASCII-only token regex split 'für' into 'f' + 'r', so every
# German umlaut/eszett word silently corrupted the repetition and gibberish
# signals. \W-complement with a Unicode flag keeps letters from any script
# together, and the optional apostrophe group still allows contractions.
_TOKEN_RE = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _is_score_key(key: Any) -> bool:
    return isinstance(key, str) and key.lower() in _SCORE_KEYS


def _normalise_score(value: Any) -> float | None:
    # A2: no more /100 rescaling here -- that silently turned an unrelated
    # count (e.g. a block total of 8) into a plausible-looking "confidence
    # 0.08". Only values that are already a fraction in [0, 1] count.
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        candidate = float(value)
        if 0.0 <= candidate <= 1.0:
            return candidate
    return None


def _collect_numeric_values(payload: Any, *, score_context: bool = False) -> list[float]:
    """Collect confidence-like leaf values.

    A2: `score_context` is now set per-key from an EXACT match on the current
    key only -- it does not inherit from an ancestor, so a 'preprocessor_config'
    dict no longer taints every number nested beneath it. Scalars are allowed
    (the paddle_service fixtures pass a bare `'rec_score': 0.99`), and lists
    of scores under a matching key are recursed into with the context still
    on so each element is picked up.
    """
    values: list[float] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            values.extend(_collect_numeric_values(value, score_context=_is_score_key(key)))
    elif isinstance(payload, (list, tuple, set)):
        for item in payload:
            values.extend(_collect_numeric_values(item, score_context=score_context))
    else:
        if score_context:
            normalised = _normalise_score(payload)
            if normalised is not None:
                values.append(normalised)
    return values


def _as_block_list(page: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = page.get('parsing_res_list')
    if isinstance(blocks, list):
        return [block for block in blocks if isinstance(block, dict)]
    return []


def _confidence_stats(raw_outputs: list[dict[str, Any]] | None) -> tuple[float | None, int]:
    """(score, sample size) shared by `ocr_confidence_score` and
    `evaluate_document_quality` so both derive the score and the sample size
    from exactly one collection pass over raw_outputs.
    """
    if not raw_outputs:
        return None, 0

    confidences: list[float] = []
    for payload in raw_outputs:
        confidences.extend(_collect_numeric_values(payload))

    if not confidences:
        return None, 0

    mean_conf = sum(confidences) / len(confidences)
    reliable = sum(confidence >= 0.8 for confidence in confidences) / len(confidences)
    # Additive, not multiplicative: the old `mean_conf * (1 - low_conf_ratio)`
    # double-punished any low-confidence reading (once via the mean, again via
    # the multiplier), which is why A2's formula weighs the two halves evenly
    # instead of compounding them.
    return _clamp(0.5 * mean_conf + 0.5 * reliable), len(confidences)


def ocr_confidence_score(raw_outputs: list[dict[str, Any]] | None) -> float | None:
    """A2: returns None -- not 0.0 -- when there is nothing to measure, so the
    caller can drop the signal entirely instead of scoring it as a confirmed
    zero. Without this, openai_vision (raw_outputs has only page/markdown,
    no matching key), the pypdf fallback, and the .eml path all produced a
    deterministic 0.0 here, which alone caps the final weighted score at 0.5
    and forces grade C regardless of actual document quality.
    """
    score, _sample_size = _confidence_stats(raw_outputs)
    return score


def structure_quality_score(page_structures: list[dict[str, Any]] | None, block_stats: dict[str, Any] | None = None) -> float:
    if not page_structures:
        return 0.0

    page_count = len(page_structures)
    if isinstance(block_stats, dict):
        block_page_count = block_stats.get('page_count')
        if isinstance(block_page_count, int) and block_page_count > 0:
            page_count = block_page_count

    pages_with_blocks = 0
    total_blocks = 0
    ordered_blocks = 0
    table_blocks = 0
    table_blocks_with_content = 0
    formula_blocks = 0

    for page in page_structures:
        blocks = _as_block_list(page)
        if blocks:
            pages_with_blocks += 1
        for block in blocks:
            total_blocks += 1
            if block.get('block_order') is not None:
                ordered_blocks += 1
            label = str(block.get('block_label') or '').lower()
            content = str(block.get('block_content') or '').strip()
            if 'table' in label:
                table_blocks += 1
                if content:
                    table_blocks_with_content += 1
            # B6: display_formula/inline_formula blocks carry the LaTeX
            # artefacts that A2's measurement showed are not configurable
            # away. A document that is mostly formula blocks is exactly the
            # 'destroyed fields' case this factor exists to catch, so it
            # must pull structure_quality down even when every other factor
            # (coverage, order, tables) looks perfect.
            if 'formula' in label:
                formula_blocks += 1

    page_coverage = pages_with_blocks / page_count if page_count else 0.0
    order_quality = ordered_blocks / total_blocks if total_blocks else 0.0
    table_quality = 1.0 if table_blocks == 0 else table_blocks_with_content / table_blocks
    formula_share = formula_blocks / total_blocks if total_blocks else 0.0
    # 10% formula share already drives this factor to 0.0 -- deliberately
    # steep, since even a handful of LaTeX-mangled blocks concentrated in a
    # short document is a real signal, not noise.
    formula_quality = 1.0 - min(1.0, formula_share / 0.1)

    return _clamp(
        (0.40 * page_coverage) + (0.25 * order_quality) + (0.20 * table_quality) + (0.15 * formula_quality)
    )


def text_noise_penalty(markdown: str) -> float:
    cleaned = markdown.strip()
    if not cleaned:
        return 1.0

    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    tokens = _TOKEN_RE.findall(cleaned)
    unique_tokens = {token.lower() for token in tokens}
    repetition_ratio = 1 - (len(unique_tokens) / len(tokens)) if tokens else 0.0
    line_repetition_ratio = 1 - (len(set(lines)) / len(lines)) if lines else 0.0
    symbol_ratio = sum(1 for character in cleaned if not character.isalnum() and not character.isspace()) / len(cleaned)
    gibberish_ratio = sum(1 for token in tokens if _looks_gibberish(token)) / len(tokens) if tokens else 0.0

    penalty = (0.35 * repetition_ratio) + (0.25 * line_repetition_ratio) + (0.2 * symbol_ratio) + (0.2 * gibberish_ratio)
    return _clamp(penalty)


def _looks_gibberish(token: str) -> bool:
    lowered = token.lower()
    letters = [character for character in lowered if character.isalpha()]
    if not letters:
        # A7: a pure digit run is a birth date (05041981) or an insurance
        # number (0012574826), not noise -- only a non-digit, non-letter
        # token (e.g. a run of punctuation-like symbols the tokenizer still
        # matched) counts as gibberish here.
        return len(token) >= 6 and not token.isdigit()

    vowel_count = sum(character in 'aeiou' for character in letters)
    if len(letters) >= 7 and vowel_count == 0:
        return True
    if len(set(lowered)) <= 2 and len(lowered) >= 6:
        return True
    return False


def evaluate_document_quality(
    markdown: str,
    *,
    page_structures: list[dict[str, Any]] | None = None,
    raw_outputs: list[dict[str, Any]] | None = None,
    block_stats: dict[str, Any] | None = None,
    field_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ocr_confidence, confidence_sample_size = _confidence_stats(raw_outputs)
    structure_quality = structure_quality_score(page_structures, block_stats)
    noise_penalty = text_noise_penalty(markdown)
    text_quality = 1 - noise_penalty

    score_components: list[tuple[str, float, float]] = []
    # A2: gate on whether a confidence value was actually found, not on
    # whether raw_outputs merely exists -- openai_vision, the pypdf fallback,
    # and .eml all pass non-empty raw_outputs that contain nothing matching
    # `_SCORE_KEYS`, which used to be scored as a confirmed 0.0 (and, at 50%
    # weight, alone capped the final score at 0.5). Dropping the component
    # here lets the existing total_weight renormalisation below spread its
    # weight across whichever signals are actually present.
    if ocr_confidence is not None:
        score_components.append(('ocr_confidence', ocr_confidence, 0.5))
    if page_structures:
        score_components.append(('structure_quality', structure_quality, 0.3))
    if markdown.strip():
        score_components.append(('text_quality', text_quality, 0.2))

    if score_components:
        weighted_score = sum(value * weight for _, value, weight in score_components)
        total_weight = sum(weight for _, _, weight in score_components)
        final_score = weighted_score / total_weight if total_weight else 0.0
    else:
        final_score = 0.0

    final_score = _clamp(final_score)
    if final_score >= _QUALITY_THRESHOLD_A:
        grade = 'A'
        recommendation = 'allow'
    elif final_score >= _QUALITY_THRESHOLD_B:
        grade = 'B'
        recommendation = 'warn'
    else:
        grade = 'C'
        recommendation = 'block'

    # B4 wiring: field_validation is a read-only, rule-based check over the
    # already-rendered markdown (IBAN/ICD/date plausibility, orphan labels --
    # see app/services/field_validation.py). It is deliberately NOT folded
    # into `score`/`grade` above -- it is exactly what lets a document graded
    # B by the confidence/structure signals still surface its concrete,
    # human-readable defects instead of hiding them behind a passing grade.
    # `field_validation` is optional so existing callers that do not run it
    # (or tests that construct a gate result directly) keep working unchanged.
    field_counts = field_validation['counts'] if field_validation is not None else {}
    field_issues = list(field_validation['issues']) if field_validation is not None else []

    return {
        'grade': grade,
        'score': round(final_score, 4),
        'recommendation': recommendation,
        'issues': field_issues,
        'signals': {
            # None (not 0.0) when the engine reports no confidences at all,
            # so a caller/UI can tell 'not measured' apart from 'measured
            # and terrible'. See confidence_sample_size for the same
            # distinction as a plain count (0 = engine gave us nothing).
            'ocr_confidence': round(ocr_confidence, 4) if ocr_confidence is not None else None,
            'confidence_sample_size': confidence_sample_size,
            'structure_quality': round(structure_quality, 4),
            'noise_penalty': round(noise_penalty, 4),
            'text_quality': round(text_quality, 4),
            'field_validation': field_counts,
        },
        'thresholds': {
            'A': _QUALITY_THRESHOLD_A,
            'B': _QUALITY_THRESHOLD_B,
        },
    }