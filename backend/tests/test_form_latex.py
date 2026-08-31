from __future__ import annotations

import re

import pytest

from app.services.form_latex import join_split_icd_codes, normalize_form_latex


def _norm_ws(s: str) -> str:
    # Fuer den Vergleich Mehrfach-Leerzeichen einebnen -- die exakte Anzahl
    # von Leerzeichen zwischen abgeflachten Feldern ist Implementierungsdetail,
    # die Zeichenfolge selbst (Reihenfolge, Woerter, Satzzeichen) muss stimmen.
    return re.sub(r'\s+', ' ', s).strip()


# --- Negativfaelle: echte Mathematik / kein Formularsignal -> woertlich -----

@pytest.mark.parametrize('text', [
    r'$E = mc^2$',
    r'$\frac{\partial u}{\partial t}$',
    r'$\sum_{i=1}^{n} x_i$',
    r'$\alpha \leq \beta$',
    r'$$\begin{equation} x = 1 \end{equation}$$',
    r'$x_i$',
    r'$$ \frac{7}{12}\frac{21}{1}\cdot\frac{0}{1} $$',
])
def test_negative_cases_preserved_literally(text):
    result, count = normalize_form_latex(text)
    assert result == text
    assert count == 0


def test_no_dollar_sign_is_noop():
    text = 'Ganz normaler Flieszstext ohne jedes Dollarzeichen.'
    result, count = normalize_form_latex(text)
    assert result == text
    assert count == 0


# --- Positivfaelle: gemessene Formularfeld-Artefakte -> Klartext -----------

def test_multiple_underline_text_fields_in_one_line():
    text = (
        r'bis: $ \underline{\text{Tag}} $ $ \underline{\text{Monat}} $ '
        r'$ \underline{\text{2026}} $'
    )
    result, count = normalize_form_latex(text)
    assert _norm_ws(result) == 'bis: Tag Monat 2026'
    assert count == 3


def test_single_underline_field_with_label_prefix():
    text = r'Patientin/ Patient Nachname: $ \underline{\text{Peter}} $'
    result, count = normalize_form_latex(text)
    assert _norm_ws(result) == 'Patientin/ Patient Nachname: Peter'
    assert count == 1


def test_date_field_with_trailing_subscript_labels():
    text = r'$$ \underline{01}\underline{07}\underline{20}\underline{26}_{Tag\quad Monat\quad Jahr} $$'
    result, count = normalize_form_latex(text)
    assert _norm_ws(result) == '01 07 20 26 Tag Monat Jahr'
    assert count == 1


def test_date_field_with_atop_labels():
    text = r'$$ \underline{15}\underline{06}\underline{20}\underline{26}\atop Tag Monat Jahr $$'
    result, count = normalize_form_latex(text)
    assert _norm_ws(result) == '15 06 20 26 Tag Monat Jahr'
    assert count == 1


def test_icd_code_field_with_mathrm_label():
    text = r'$$ \underline{016.7}_{ICD10\mathrm{Code}} $$'
    result, count = normalize_form_latex(text)
    assert _norm_ws(result) == '016.7 ICD10 Code'
    assert count == 1


def test_slash_separated_text_fields():
    text = r'$$ \text{Bronchopneumonia}/\text{Bezeichnung} $$'
    result, count = normalize_form_latex(text)
    assert _norm_ws(result) == 'Bronchopneumonia / Bezeichnung'
    assert count == 1


def test_checkmark_field_leaves_surrounding_text_untouched():
    text = r'$ \checkmark $ ja ___ nein'
    result, count = normalize_form_latex(text)
    assert _norm_ws(result) == '[x] ja ___ nein'
    assert count == 1


def test_full_array_block_from_real_data():
    # Das Beispiel aus dem Auftrag: verschachteltes \underline, \\-Zeilenumbruch
    # und \begin{array}{c}...\end{array} als Layout-Geruest.
    text = (
        r'$$ \begin{array}{c}\underline{02}\quad\underline{082}\quad'
        r'\underline{0\underline{2}5}\\ Tag\quad Monat\quad Jahr\end{array} $$'
    )
    result, count = normalize_form_latex(text)
    assert count == 1
    normalized = _norm_ws(result)
    assert '\\' not in normalized
    assert '{' not in normalized and '}' not in normalized
    assert '02 082 0 2 5 Tag Monat Jahr' == normalized


# --- Rueckgabezaehler / Mischtexte ------------------------------------------

def test_count_tracks_only_actually_flattened_spans():
    text = (
        r'$E = mc^2$ bleibt stehen, aber '
        r'$ \underline{\text{Vorname}} $ wird geflacht.'
    )
    result, count = normalize_form_latex(text)
    assert count == 1
    assert r'$E = mc^2$' in result
    assert 'Vorname' in result
    assert '\\underline' not in result


def test_empty_string_returns_zero_count():
    result, count = normalize_form_latex('')
    assert result == ''
    assert count == 0


# --- join_split_icd_codes: separat testbare Stufe 2 -------------------------

def test_join_split_icd_code_letter_case():
    assert join_split_icd_codes('F 43 . 2') == 'F43.2'
    assert join_split_icd_codes('Diagnose: J 18 . 9 akut') == 'Diagnose: J18.9 akut'


def test_join_split_icd_code_leaves_plain_numbers_alone():
    # Bewusster Nicht-Fall: ohne Buchstaben-Anker gibt es kein verlaessliches
    # Signal, das einen ICD-Code von einer gewoehnlichen Dezimalzahl
    # unterscheidet -- also nichts anfassen.
    text = 'Betrag: 12 . 50 EUR, Menge 3 . 4 kg'
    assert join_split_icd_codes(text) == text


def test_join_split_icd_code_u_prefix_excluded():
    # Spezifikation verwendet [A-TV-Z] -- 'U' ist bewusst kein gueltiges
    # ICD-10-Kapitel-Praefix in diesem Muster.
    text = 'U 07 . 1'
    assert join_split_icd_codes(text) == text


def test_join_split_icd_code_does_not_run_inside_normalize_form_latex():
    # normalize_form_latex() ruft die ICD-Stufe-2 NICHT automatisch auf --
    # sie ist eine eigene, separat verdrahtete Funktion (siehe Modul-Docstring).
    text = r'$$ \underline{F}\underline{43}.\underline{2}_{ICD10} $$'
    result, _count = normalize_form_latex(text)
    assert _norm_ws(result) == 'F 43 . 2 ICD10'


def test_latex_escaped_space_is_treated_as_a_spacer():
    r"""`\ ` (backslash-space) is LaTeX's explicit inter-word space.

    Measured on the real corpus: without it this line survived the flatten
    carrying a stray backslash, tripped the safety net and stayed raw --
    the only unintended false negative among the 52 real LaTeX lines.
    """
    line = r'$$ \underline{09}_{Tag}\ \underline{07}_{Monat}\ 2\underline{026}_{Jahr} $$'
    out, hits = normalize_form_latex(line)
    assert hits == 1
    assert '\\' not in out
    assert '$' not in out
    assert out.split() == ['09', 'Tag', '07', 'Monat', '2', '026', 'Jahr']
