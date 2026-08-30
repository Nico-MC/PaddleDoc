"""Unit tests for app/services/quality_gate.py.

Covers three fixes made against the 9-document measurement run (all nine
scored grade C / recommendation 'block' -- correctly, but for the wrong
reason: ocr_confidence was a measurement artefact, not a real signal):

- A2: `_collect_numeric_values`'s substring-hint key matching leaked into
  unrelated numeric fields (a 'preprocessor_config' dict tainted everything
  nested under it once any ancestor key matched 'conf'), and
  `_normalise_score` silently rescaled any value in (1, 100] by /100. Fixed
  by an exact-key allowlist, no scaling, and `ocr_confidence_score` /
  `evaluate_document_quality` returning/treating the signal as None (not a
  confirmed 0.0) when nothing was actually measured -- which is the normal
  case for openai_vision, the pypdf fallback, and the .eml path.
- A7: `_TOKEN_RE` was ASCII-only (split 'für' into 'f' + 'r') and
  `_looks_gibberish` flagged any 6+ digit run as noise, which misclassifies
  birth dates and insurance numbers.
- B6: `structure_quality_score` had no formula-share factor, so a page
  entirely made of formula blocks (whose LaTeX rendering is known-broken,
  see test_render_block_content.py) still scored close to 1.0.
"""

from __future__ import annotations

from app.services.quality_gate import (
    _collect_numeric_values,
    _looks_gibberish,
    evaluate_document_quality,
    ocr_confidence_score,
    structure_quality_score,
    text_noise_penalty,
)


# --- A2(b): exact key match, no substring inheritance, no /100 scaling ------


def test_collect_numeric_values_matches_exact_score_keys_only():
    # 'preprocessor_config' contains 'conf' as a substring -- under the old
    # `score_context = score_context or _looks_like_score_key(key)` inheritance
    # this alone would have tainted every number nested under it (and every
    # sibling reached afterwards, since the flag never resets on the way back
    # down). An exact match on 'confidence' must still work.
    payload = {
        'preprocessor_config': {'block_count': 8, 'scale_factor': 2},
        'confidence': 0.91,
    }
    assert _collect_numeric_values(payload) == [0.91]


def test_collect_numeric_values_does_not_inherit_context_from_ancestor():
    # A dict value under a matching key does NOT propagate the context to
    # its own children with mismatched key names -- only an exact key match
    # at that level counts. This is the deliberate replacement for the old
    # 'never taken back' inheritance, not an oversight.
    payload = {'confidence': {'unrelated_count': 42}}
    assert _collect_numeric_values(payload) == []


def test_collect_numeric_values_allows_scalars():
    # Regression guard for the exact fixture shape used in
    # tests/test_paddle_service.py: `{'json': {'res': {'rec_score': 0.99}}}`.
    # A prior draft of the exact-match fix required a list/collection under
    # the matching key, which would have broken that fixture.
    payload = {'json': {'res': {'rec_score': 0.99}}}
    assert _collect_numeric_values(payload) == [0.99]


def test_collect_numeric_values_recurses_into_lists_under_a_matching_key():
    payload = {'res': {'dt_scores': [0.99, 0.5, 0.2]}}
    assert _collect_numeric_values(payload) == [0.99, 0.5, 0.2]


def test_collect_numeric_values_ignores_out_of_range_and_bool_values():
    # A2: no more implicit /100 rescaling -- 8 used to become "confidence
    # 0.08" (in-range after division), which is exactly the false signal A2
    # eliminates. Booleans are also excluded (bool is a subclass of int).
    payload = {'score': 8, 'confidence': True, 'rec_score': -0.1}
    assert _collect_numeric_values(payload) == []


def test_collect_numeric_values_all_documented_exact_keys():
    payload = {
        'confidence': 0.1,
        'confidences': 0.2,
        'score': 0.3,
        'scores': 0.4,
        'rec_score': 0.5,
        'rec_scores': 0.6,
        'dt_score': 0.7,
        'dt_scores': 0.8,
        'det_score': 0.9,
        'det_scores': 1.0,
    }
    assert sorted(_collect_numeric_values(payload)) == [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]


# --- A2(a): ocr_confidence_score / evaluate_document_quality with None -----


def test_ocr_confidence_score_is_none_without_raw_outputs():
    assert ocr_confidence_score(None) is None
    assert ocr_confidence_score([]) is None


def test_ocr_confidence_score_is_none_when_no_key_matches():
    # This is the real openai_vision raw_outputs shape (see
    # _openai_vision_to_structure in paddle_service.py): only 'page' and
    # 'markdown' keys, never anything confidence-like. The same is true for
    # the pypdf fallback and the .eml path, which never build raw_outputs
    # with score-like keys at all.
    raw_outputs = [{'page': 1, 'markdown': 'Some text without any score field.'}]
    assert ocr_confidence_score(raw_outputs) is None


def test_ocr_confidence_score_additive_formula():
    # mean_conf = (0.9 + 0.9 + 0.4) / 3 = 0.7333...
    # reliable  = 2/3 values >= 0.8 = 0.6666...
    # 0.5 * 0.7333 + 0.5 * 0.6667 = 0.7
    raw_outputs = [{'json': {'res': {'rec_scores': [0.9, 0.9, 0.4]}}}]
    score = ocr_confidence_score(raw_outputs)
    assert score is not None
    assert round(score, 4) == 0.7


def test_ocr_confidence_score_additive_not_multiplicative():
    # Under the old multiplicative formula `mean_conf * (1 - low_conf_ratio)`
    # a single low-confidence value among otherwise-perfect ones was punished
    # twice. Here mean_conf = (1.0 + 1.0 + 0.1) / 3 = 0.7, reliable = 2/3.
    # Old formula: 0.7 * (1 - 1/3) = 0.4667. New formula: 0.5*0.7 + 0.5*0.6667
    # = 0.6833 -- less punitive, matching the additive spec exactly.
    raw_outputs = [{'json': {'res': {'scores': [1.0, 1.0, 0.1]}}}]
    score = ocr_confidence_score(raw_outputs)
    assert score is not None
    assert round(score, 4) == 0.6833


# --- A2: caller no longer forces grade C when the engine reports nothing ---


def test_evaluate_document_quality_openai_vision_shape_is_not_forced_to_c():
    # Before A2, ocr_confidence defaulted to 0.0 whenever raw_outputs was
    # non-empty but contained no matching key -- at 50% weight that alone
    # capped the final score at 0.5, forcing grade C regardless of how clean
    # the document actually was. A clean document with this raw_outputs
    # shape must now be judged on structure_quality/text_quality alone.
    markdown = '# Clean Title\n\nA short, well formed paragraph of real prose.'
    quality = evaluate_document_quality(
        markdown,
        page_structures=[
            {
                'parsing_res_list': [
                    {'block_label': 'paragraph_title', 'block_content': 'Clean Title', 'block_order': 1},
                    {'block_label': 'text', 'block_content': 'A short paragraph.', 'block_order': 2},
                ]
            }
        ],
        raw_outputs=[{'page': 1, 'markdown': markdown}],
        block_stats={'page_count': 1},
    )
    assert quality['signals']['ocr_confidence'] is None
    assert quality['signals']['confidence_sample_size'] == 0
    assert quality['grade'] in {'A', 'B'}
    assert quality['recommendation'] in {'allow', 'warn'}


def test_evaluate_document_quality_confidence_sample_size_reflects_measured_values():
    quality = evaluate_document_quality(
        'text',
        raw_outputs=[{'json': {'res': {'rec_scores': [0.9, 0.8, 0.7]}}}],
    )
    assert quality['signals']['confidence_sample_size'] == 3
    assert quality['signals']['ocr_confidence'] is not None


def test_evaluate_document_quality_signal_is_none_with_no_raw_outputs_at_all():
    quality = evaluate_document_quality('some plain text')
    assert quality['signals']['ocr_confidence'] is None
    assert quality['signals']['confidence_sample_size'] == 0


# --- B6: formula-share factor in structure_quality_score --------------------


def test_structure_quality_score_penalizes_high_formula_share():
    # 3 of 10 blocks are formulas -> formula_share 0.3 -> already clamped to
    # 0.0 by the min(1.0, share/0.1) steepness (10% share already floors it).
    # Coverage/order/table are otherwise perfect, so the only thing pulling
    # this below 1.0 is the new formula factor -- pinning the exact value
    # both proves the factor fires and pins the 0.40/0.25/0.20/0.15 weights.
    blocks = [{'block_label': 'text', 'block_content': f't{i}', 'block_order': i} for i in range(7)]
    blocks += [
        {'block_label': 'display_formula', 'block_content': r'$x$', 'block_order': 7 + i}
        for i in range(3)
    ]
    page_structures = [{'parsing_res_list': blocks}]
    score = structure_quality_score(page_structures, block_stats={'page_count': 1})
    # page_coverage=1, order_quality=1, table_quality=1 (no tables),
    # formula_quality=0.0 -> 0.40 + 0.25 + 0.20 + 0.15*0 = 0.85
    assert round(score, 4) == 0.85


def test_structure_quality_score_no_formula_blocks_is_unaffected():
    blocks = [{'block_label': 'text', 'block_content': 't', 'block_order': 1}]
    page_structures = [{'parsing_res_list': blocks}]
    score = structure_quality_score(page_structures, block_stats={'page_count': 1})
    # page_coverage=1, order_quality=1, table_quality=1, formula_quality=1
    assert round(score, 4) == 1.0


def test_structure_quality_score_inline_formula_label_also_counts():
    # Measured labels are 'display_formula' and 'inline_formula' -- the
    # match is a substring check on 'formula', so both must count.
    blocks = [
        {'block_label': 'text', 'block_content': 't', 'block_order': 1},
        {'block_label': 'inline_formula', 'block_content': r'$y$', 'block_order': 2},
    ]
    page_structures = [{'parsing_res_list': blocks}]
    score = structure_quality_score(page_structures, block_stats={'page_count': 1})
    # formula_share = 1/2 = 0.5 -> formula_quality clamped to 0.0
    assert round(score, 4) == 0.85


def test_structure_quality_score_small_formula_share_partially_penalized():
    # 1 of 20 blocks is a formula -> formula_share = 0.05 -> formula_quality
    # = 1 - min(1.0, 0.05/0.1) = 0.5 -- below the 10% floor, so it's a
    # partial (not total) penalty.
    blocks = [{'block_label': 'text', 'block_content': 't', 'block_order': i} for i in range(19)]
    blocks.append({'block_label': 'display_formula', 'block_content': r'$z$', 'block_order': 19})
    page_structures = [{'parsing_res_list': blocks}]
    score = structure_quality_score(page_structures, block_stats={'page_count': 1})
    # 0.40 + 0.25 + 0.20 + 0.15*0.5 = 0.925
    assert round(score, 4) == 0.925


# --- A7: Unicode-aware tokenizer -------------------------------------------


def test_text_noise_penalty_does_not_split_german_umlaut_words():
    # Under the old ASCII-only `[A-Za-z0-9']+` regex, 'für' tokenized as
    # 'f' + 'r' (the 'ü' broke the match). Repeating a real German sentence
    # several times must be measured as line/word repetition, not further
    # inflated by the umlaut characters themselves being miscounted as
    # extra fragments. A single occurrence of ordinary German prose must
    # score low noise.
    markdown = 'Für Rückfragen stehen wir Ihnen jederzeit gerne zur Verfügung.'
    penalty = text_noise_penalty(markdown)
    assert penalty < 0.2


def test_text_noise_penalty_tokenizes_umlaut_word_as_one_token():
    from app.services.quality_gate import _TOKEN_RE

    tokens = _TOKEN_RE.findall('für Straße')
    assert tokens == ['für', 'Straße']


# --- A7: digit runs are not gibberish ---------------------------------------


def test_looks_gibberish_excludes_pure_digit_runs():
    # Birth date and a plausible insurance/IBAN-like digit run -- both real,
    # structured data, not noise.
    assert _looks_gibberish('05041981') is False
    assert _looks_gibberish('0012574826') is False


def test_looks_gibberish_still_flags_long_consonant_runs():
    # Unchanged behavior: a 7+ letter token with no vowels is still gibberish.
    assert _looks_gibberish('grxzpfq') is True


def test_looks_gibberish_still_flags_long_symbol_runs():
    # A non-alpha, non-digit token (the tokenizer only emits alnum, but this
    # guards the branch directly) of 6+ characters is still gibberish.
    assert _looks_gibberish('......') is True


def test_text_noise_penalty_with_real_document_numbers_is_low():
    markdown = (
        'Geburtsdatum: 05041981\n'
        'Versicherungsnummer: 0012574826\n'
        'IBAN: DE89370400440532013000'
    )
    penalty = text_noise_penalty(markdown)
    assert penalty < 0.3


# --- B4 wiring: field_validation results surface as signals + issues -------
#
# field_validation.validate_document() is built and tested on its own (see
# test_field_validation.py) but was not wired into the quality gate. This is
# the integration point: evaluate_document_quality takes the already-computed
# result as an optional keyword so callers that never run field validation
# (or construct a gate result directly, as most tests above do) keep working
# unchanged, while callers that do run it get the counts folded into
# `signals['field_validation']` and the messages surfaced as top-level
# `issues` -- visible defects on a document that may still grade B or even A.


def test_evaluate_document_quality_without_field_validation_defaults_empty():
    quality = evaluate_document_quality('# Some heading\n\nSome plain text.')
    assert quality['issues'] == []
    assert quality['signals']['field_validation'] == {}


def test_evaluate_document_quality_surfaces_field_validation_issues():
    field_validation = {
        'issues': ['IBAN DE89370400450533013100: Pruefziffer falsch'],
        'counts': {'iban_total': 1, 'iban_invalid': 1, 'icd_total': 0, 'icd_invalid': 0,
                    'date_total': 0, 'date_implausible': 0, 'orphan_labels': 0, 'labelled_lines': 0},
    }
    quality = evaluate_document_quality('IBAN: DE89370400450533013100', field_validation=field_validation)
    assert quality['issues'] == ['IBAN DE89370400450533013100: Pruefziffer falsch']
    assert quality['signals']['field_validation'] == field_validation['counts']


def test_evaluate_document_quality_field_validation_issues_do_not_affect_score():
    # field_validation is a visibility signal, not a scoring input -- a
    # document's grade must come out identical whether or not its (already
    # rendered) defects are also being reported alongside it.
    markdown = '# Klar strukturiertes Dokument\n\nEin sauberer Absatz ohne Rauschen.'
    field_validation = {
        'issues': ['IBAN DE89370400450533013100: Pruefziffer falsch'],
        'counts': {'iban_total': 1, 'iban_invalid': 1, 'icd_total': 0, 'icd_invalid': 0,
                    'date_total': 0, 'date_implausible': 0, 'orphan_labels': 0, 'labelled_lines': 0},
    }
    without = evaluate_document_quality(markdown)
    with_fv = evaluate_document_quality(markdown, field_validation=field_validation)
    assert without['score'] == with_fv['score']
    assert without['grade'] == with_fv['grade']
