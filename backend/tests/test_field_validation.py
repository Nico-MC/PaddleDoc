"""Unit tests for `validate_document` in app/services/field_validation.py.

Report-only tool: every test here checks that a problem is *named*, never
that the input text comes back changed -- the module must never touch or
return the markdown it inspects (see the NUR MELDEN, NIE KORRIGIEREN rule in
the module docstring).
"""

from __future__ import annotations

from app.services.field_validation import validate_document


def _counts(markdown):
    return validate_document(markdown)['counts']


def _issues(markdown):
    return validate_document(markdown)['issues']


# --- shape / robustness -------------------------------------------------------

def test_returns_wellformed_shape_for_empty_string():
    result = validate_document('')
    assert result == {
        'issues': [],
        'counts': {
            'iban_total': 0,
            'iban_invalid': 0,
            'icd_total': 0,
            'icd_invalid': 0,
            'date_total': 0,
            'date_implausible': 0,
            'orphan_labels': 0,
            'labelled_lines': 0,
        },
    }


def test_no_matches_returns_empty_issues_and_zero_counts():
    result = validate_document('Dies ist ein ganz gewoehnlicher Absatz ohne jeden Treffer.')
    assert result['issues'] == []
    assert all(value == 0 for value in result['counts'].values())


def test_never_raises_on_very_long_text():
    huge = 'lorem ipsum dolor sit amet ' * 50_000
    result = validate_document(huge)
    assert isinstance(result, dict)
    assert 'issues' in result and 'counts' in result


def test_never_raises_on_none_like_and_weird_input():
    # markdown is typed str, but callers upstream have been known to hand
    # through None on empty pipeline output -- must degrade, not crash.
    result = validate_document(None)
    assert result['issues'] == []


def test_does_not_mutate_or_echo_back_the_input_text():
    text = 'IBAN DE89370400450533013100 ist ungueltig.'
    result = validate_document(text)
    # The function must not return the source text anywhere in its result --
    # only findings about it.
    assert 'markdown' not in result
    assert 'text' not in result
    assert result['issues'][0] != text


# --- IBAN ----------------------------------------------------------------------

def test_iban_valid_reference_iban_produces_no_issue():
    # The well-known Deutsche Bundesbank example IBAN -- correct mod-97 check
    # digit, correct DE length, real country code.
    counts = _counts('Bitte ueberweisen Sie an DE89370400440532013000.')
    assert counts['iban_total'] == 1
    assert counts['iban_invalid'] == 0


def test_iban_bad_checksum_is_reported():
    issues = _issues('IBAN: DE89370400450533013100')
    assert any('DE89370400450533013100' in issue and 'Pruefziffer' in issue for issue in issues)


def test_iban_unknown_country_code_is_reported():
    # 'OE' is not a real ISO country code -- catches this before any
    # length/checksum math even runs.
    issues = _issues('Kontoinhaber IBAN OE45123456789012345678 angegeben.')
    assert any('OE' in issue and 'Laendercode' in issue for issue in issues)


def test_iban_wrong_length_for_de_is_reported():
    # 20 characters instead of the required 22 for DE.
    issues = _issues('IBAN DE441002300450067009 auf dem Beleg.')
    assert any('DE441002300450067009' in issue and 'Laenge' in issue for issue in issues)


def test_iban_invalid_character_is_reported():
    issues = _issues('IBAN DE29@334550122257 im Formular.')
    assert any('ungueltiges Zeichen' in issue for issue in issues)


def test_iban_counts_accumulate_across_multiple_candidates():
    text = 'Erste IBAN DE89370400440532013000, zweite IBAN DE441002300450067009.'
    counts = _counts(text)
    assert counts['iban_total'] == 2
    assert counts['iban_invalid'] == 1


def test_iban_message_format_matches_house_style():
    issues = _issues('DE89370400450533013100')
    assert issues == ['IBAN DE89370400450533013100: Pruefziffer falsch']


# --- ICD-10 ----------------------------------------------------------------------

def test_icd_valid_letter_code_produces_no_issue():
    counts = _counts('Diagnose: E10.9 (Diabetes mellitus Typ 2)')
    assert counts['icd_total'] == 1
    assert counts['icd_invalid'] == 0


def test_icd_valid_code_without_decimal_extension():
    counts = _counts('Diagnose: J45')
    assert counts['icd_total'] == 1
    assert counts['icd_invalid'] == 0


def test_icd_letter_u_is_out_of_valid_range():
    issues = _issues('Diagnose: U07.1')
    assert any('U07.1' in issue for issue in issues)


def test_icd_letter_i_is_within_valid_range():
    # Spec: valid ranges are A-T, V-Z -- I is inside A-T and must NOT be
    # flagged just for being visually close to the digit 1.
    counts = _counts('Diagnose: I10')
    assert counts['icd_total'] == 1
    assert counts['icd_invalid'] == 0


def test_icd_bare_numeric_code_718_0_is_reported():
    issues = _issues('Alter Diagnoseschluessel 718.0 im Bestand.')
    assert any('718.0' in issue and 'Buchstabenpraefix' in issue for issue in issues)


def test_icd_bare_numeric_code_016_7_is_reported():
    issues = _issues('Alter Diagnoseschluessel 016.7 im Bestand.')
    assert any('016.7' in issue for issue in issues)


def test_icd_bare_numeric_code_744_09_is_reported():
    issues = _issues('Alter Diagnoseschluessel 744.09 im Bestand.')
    assert any('744.09' in issue for issue in issues)


def test_icd_counts_track_total_and_invalid_separately():
    text = 'Gueltig: E10.9. Ungueltig: U07.1. Auch ungueltig: 718.0.'
    counts = _counts(text)
    assert counts['icd_total'] == 3
    assert counts['icd_invalid'] == 2


# --- Datumsplausibilitaet ----------------------------------------------------------

def test_date_dotted_form_valid_produces_no_issue():
    counts = _counts('Ausstellungsdatum: 02.08.2025')
    assert counts['date_total'] == 1
    assert counts['date_implausible'] == 0


def test_date_spaced_form_valid_produces_no_issue():
    counts = _counts('Ausstellungsdatum: 02 08 2025')
    assert counts['date_total'] == 1
    assert counts['date_implausible'] == 0


def test_date_month_14_is_implausible():
    issues = _issues('Datum: 31 14 2025')
    assert any('Monat 14' in issue for issue in issues)


def test_date_month_082_is_implausible():
    issues = _issues('Datum: 01 082 2025')
    assert any('Monat 082' in issue for issue in issues)


def test_date_day_out_of_range_is_implausible():
    issues = _issues('Datum: 32.01.2025')
    assert any('Tag 32' in issue for issue in issues)


def test_date_torn_four_group_sequence_is_reported_without_guessing():
    issues = _issues('Geburtsdatum: 01 07 20 26')
    assert any('zerrissen' in issue and '01 07 20 26' in issue for issue in issues)
    # The exact phrase from the task brief must appear verbatim.
    assert any('nicht eindeutig lesbar' in issue for issue in issues)


def test_date_torn_sequence_is_not_also_reported_as_a_clean_implausible_date():
    # Making sure the torn-detector and the compact-date-detector don't both
    # fire on overlapping text and produce two contradictory issues.
    issues = _issues('Geburtsdatum: 01 07 20 26')
    assert len(issues) == 1


def test_geburtsdatum_table_trap_reports_date_problem_not_pairing_problem():
    # Real measured row: correctly paired label/value, but the value itself
    # is a crooked date. Must show up as a date issue (torn-prefix form) and
    # must NOT be counted as an orphan label, since the pairing is fine.
    result = validate_document('| Geburtsdatum: | 06 06/19/80 |')
    assert result['counts']['orphan_labels'] == 0
    assert result['counts']['labelled_lines'] == 1
    assert any('zerrissen' in issue for issue in result['issues'])


def test_date_slash_form_ambiguous_but_plausible_is_not_flagged():
    # 06/19/80 only makes sense as MM/DD/YY (month 06, day 19) -- accepted
    # since at least one reading is plausible, not guessed at further.
    counts = _counts('Termin am 06/19/1980 vereinbart.')
    assert counts['date_total'] == 1
    assert counts['date_implausible'] == 0


def test_date_slash_form_implausible_in_both_readings_is_flagged():
    issues = _issues('Termin am 45/99/2020 vereinbart.')
    assert any('45/99/2020' in issue for issue in issues)


# --- Waisen-Labels -----------------------------------------------------------------

def test_orphan_label_bare_colon_with_nothing_after():
    counts = _counts('Nachname:')
    assert counts['orphan_labels'] == 1
    assert counts['labelled_lines'] == 1


def test_orphan_label_placeholder_underscores():
    counts = _counts('Nachname: ___')
    assert counts['orphan_labels'] == 1
    assert counts['labelled_lines'] == 1


def test_label_with_real_value_is_not_orphan():
    counts = _counts('Nachname: Mueller')
    assert counts['orphan_labels'] == 0
    assert counts['labelled_lines'] == 1


def test_orphan_label_in_table_cell():
    counts = _counts('| Nachname: | ___ |')
    assert counts['orphan_labels'] == 1
    assert counts['labelled_lines'] == 1


def test_table_cell_with_real_value_is_not_orphan():
    counts = _counts('| Nachname: | Mueller |')
    assert counts['orphan_labels'] == 0
    assert counts['labelled_lines'] == 1


def test_orphan_labels_are_counted_not_individually_listed_as_issues():
    result = validate_document('Nachname:\nVorname:\n')
    assert result['counts']['orphan_labels'] == 2
    assert result['issues'] == []


def test_bare_time_like_value_is_not_treated_as_a_label():
    # '14:30' must not be mistaken for a field label -- the prefix is purely
    # numeric, not a word.
    counts = _counts('14:30 Uhr')
    assert counts['labelled_lines'] == 0


def test_url_is_not_treated_as_a_label():
    counts = _counts('Siehe https://example.com/formular fuer Details.')
    assert counts['labelled_lines'] == 0


# --- Storno- vs. Buchungsdatum (bonus check named in the task brief) --------------

def test_storno_before_buchungsdatum_is_reported():
    text = 'Buchungsdatum: 10.05.2024 Betrag 50 EUR. Stornodatum: 01.05.2024.'
    issues = _issues(text)
    assert any('Storno' in issue and 'Buchungsdatum' in issue for issue in issues)


def test_storno_after_buchungsdatum_is_not_reported():
    text = 'Buchungsdatum: 01.05.2024 Betrag 50 EUR. Stornodatum: 10.05.2024.'
    issues = _issues(text)
    assert not any('Storno' in issue and 'liegt vor' in issue for issue in issues)


# --- combined document -------------------------------------------------------------

def test_combined_document_aggregates_all_checks_independently():
    text = (
        'IBAN DE89370400450533013100 auf dem Beleg.\n'
        'Diagnose: 718.0\n'
        'Datum: 31 14 2025\n'
        'Nachname:\n'
        'Vorname: Peter\n'
    )
    result = validate_document(text)
    counts = result['counts']
    assert counts['iban_total'] == 1 and counts['iban_invalid'] == 1
    assert counts['icd_total'] == 1 and counts['icd_invalid'] == 1
    assert counts['date_total'] == 1 and counts['date_implausible'] == 1
    # Labelled lines: 'Diagnose:', 'Datum:', 'Nachname:' and 'Vorname:' --
    # only 'Nachname:' is orphan (no value / placeholder-only).
    assert counts['orphan_labels'] == 1 and counts['labelled_lines'] == 4
    assert len(result['issues']) == 3
