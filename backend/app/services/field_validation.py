"""B4: report-only sanity checks over already-extracted markdown.

Measured across 9 documents (see B4 task brief): 7 of 8 extracted IBANs are
mod-97-invalid, dates come out with a month of 14 or 082, and a Storno date
sits before its Buchungsdatum -- none of that is visible today because
nothing ever re-checks a value after OCR + LaTeX flattening hand it over.

Ground rule that overrides everything else here: NUR MELDEN, NIE KORRIGIEREN.
A "helpful" type coercion that rewrites a value would tear apart a label/value
pair that OCR actually got right -- e.g. the measured
'| Geburtsdatum: | 06 06/19/80 |' row, where pairing is fine and only the
date value is crooked (see `_check_orphan_labels`). This module never
touches the text, only reads it and reports findings about it.
`validate_document` always returns the same shape, never raises, regardless
of how empty, huge, or garbled the input is.
"""

from __future__ import annotations

import re

# --- IBAN --------------------------------------------------------------------

# Every country that actually issues IBANs (ISO 13616 registry). A two-letter
# prefix that is not in this set can never be a real IBAN, no matter how the
# rest of the string is shaped -- this is what catches 'OE45...' below, where
# 'OE' simply does not exist as a country code.
_IBAN_COUNTRIES = frozenset({
    'AD', 'AE', 'AL', 'AT', 'AZ', 'BA', 'BE', 'BG', 'BH', 'BR', 'BY', 'CH',
    'CR', 'CY', 'CZ', 'DE', 'DJ', 'DK', 'DO', 'EE', 'EG', 'ES', 'FI', 'FO',
    'FR', 'GB', 'GE', 'GI', 'GL', 'GR', 'GT', 'HR', 'HU', 'IE', 'IL', 'IQ',
    'IS', 'IT', 'JO', 'KW', 'KZ', 'LB', 'LC', 'LI', 'LT', 'LU', 'LV', 'LY',
    'MC', 'MD', 'ME', 'MK', 'MN', 'MR', 'MT', 'MU', 'NI', 'NL', 'NO', 'OM',
    'PK', 'PL', 'PS', 'PT', 'QA', 'RO', 'RS', 'SA', 'SC', 'SD', 'SE', 'SI',
    'SK', 'SM', 'SO', 'ST', 'SV', 'SY', 'TL', 'TN', 'TR', 'UA', 'VA', 'VG',
    'XK',
})
_IBAN_DE_LENGTH = 22
_IBAN_GENERIC_MIN_LENGTH = 15
_IBAN_GENERIC_MAX_LENGTH = 34

# Header (2 letters + 2 digits) followed by 2-9 loosely-spaced chunks of up to
# 4 characters. The chunk class deliberately includes '@' and friends -- not
# just [A-Za-z0-9] -- so a garbled candidate like 'DE29@334550122257' is still
# captured as ONE candidate to validate and report on, instead of the regex
# silently splitting it into two harmless-looking halves at the '@'.
_IBAN_CANDIDATE_RE = re.compile(
    r'\b[A-Za-z]{2}\d{2}(?:[ \t]?[A-Za-z0-9@#$%^&*]{1,4}){2,9}\b'
)


def _iban_mod97_valid(compact: str) -> bool:
    """True IFF the mod-97 check digit (ISO 7064 MOD 97-10) is correct.

    Move the first four characters to the end, map letters to A=10..Z=35,
    and the result mod 97 must equal 1. Caller guarantees `compact` is
    alnum-only and long enough for this to be meaningful.
    """
    rearranged = compact[4:] + compact[:4]
    digits = []
    for ch in rearranged:
        digits.append(ch if ch.isdigit() else str(ord(ch.upper()) - ord('A') + 10))
    return int(''.join(digits)) % 97 == 1


def _check_iban_candidates(text: str, issues: list[str], counts: dict[str, int]) -> None:
    for match in _IBAN_CANDIDATE_RE.finditer(text):
        raw = match.group(0)
        counts['iban_total'] += 1
        compact = raw.replace(' ', '').replace('\t', '')

        if not compact.isalnum():
            issues.append(f'IBAN {raw}: enthaelt ungueltiges Zeichen')
            counts['iban_invalid'] += 1
            continue

        country = compact[:2].upper()
        if country not in _IBAN_COUNTRIES:
            issues.append(f'IBAN {raw}: Laendercode {country} existiert nicht')
            counts['iban_invalid'] += 1
            continue

        expected_length = _IBAN_DE_LENGTH if country == 'DE' else None
        if expected_length is not None and len(compact) != expected_length:
            issues.append(
                f'IBAN {raw}: Laenge {len(compact)} ungueltig (erwartet {expected_length} fuer {country})'
            )
            counts['iban_invalid'] += 1
            continue
        if expected_length is None and not (_IBAN_GENERIC_MIN_LENGTH <= len(compact) <= _IBAN_GENERIC_MAX_LENGTH):
            issues.append(
                f'IBAN {raw}: Laenge {len(compact)} ungueltig (erwartet '
                f'{_IBAN_GENERIC_MIN_LENGTH}-{_IBAN_GENERIC_MAX_LENGTH})'
            )
            counts['iban_invalid'] += 1
            continue

        if not _iban_mod97_valid(compact):
            issues.append(f'IBAN {raw}: Pruefziffer falsch')
            counts['iban_invalid'] += 1


# --- ICD-10 --------------------------------------------------------------------

# Letter form: A00-Z99 with an optional '.' + 1-2 digit extension. The
# lookaround boundaries (rather than plain \b) make sure a longer alnum run
# like 'A1212' is rejected outright instead of matching just its first four
# characters.
# The trailing lookahead blocks a further alnum char (rejects 'A1212') or a
# '.' immediately followed by a digit (rejects a 3+ digit extension like
# 'E10.999' that no longer fits the 1-2 digit format) -- but NOT a bare
# trailing '.', so an ordinary sentence-ending period right after a valid
# code ('Diagnose: E10.9.') does not swallow the match.
_ICD_LETTER_RE = re.compile(r'(?<![A-Za-z0-9])([A-Za-z])(\d{2})(\.\d{1,2})?(?![A-Za-z0-9]|\.\d)')
# Bare numeric form: what an ICD-9-style code looks like once OCR drops the
# leading letter (measured: '718.0', '016.7', '744.09'). Exactly three digits
# before the dot distinguishes this from a two-decimal currency amount, and a
# dot (not comma) decimal separator is itself already unusual in German
# documents -- money here is written '1.234,56', comma-decimal -- which is
# part of why a dot-decimal three-digit number reads as a mangled code rather
# than an amount.
_ICD_BARE_RE = re.compile(r'(?<![A-Za-z0-9.])\d{3}\.\d{1,2}(?![A-Za-z0-9]|\.\d)')
# ICD-10 reserves the letter 'U' for provisional/special-purpose categories,
# so it is deliberately excluded from the otherwise-A00-Z99 ordinary range;
# every other letter A-T, V-Z is a valid chapter prefix, 'I' included.
_ICD_INVALID_LETTERS = frozenset({'U'})


def _check_icd_candidates(text: str, issues: list[str], counts: dict[str, int]) -> None:
    consumed: list[tuple[int, int]] = []

    for match in _ICD_LETTER_RE.finditer(text):
        consumed.append(match.span())
        counts['icd_total'] += 1
        letter = match.group(1).upper()
        code = match.group(0)
        if letter in _ICD_INVALID_LETTERS:
            issues.append(f"ICD-10 {code}: Buchstabe '{letter}' ausserhalb gueltigem Bereich (A-T, V-Z)")
            counts['icd_invalid'] += 1

    for match in _ICD_BARE_RE.finditer(text):
        span = match.span()
        if any(start < span[1] and span[0] < end for start, end in consumed):
            continue
        counts['icd_total'] += 1
        counts['icd_invalid'] += 1
        issues.append(f'ICD-10-artiger Code {match.group(0)}: kein Buchstabenpraefix')


# --- Datumsplausibilitaet ------------------------------------------------------

_DATE_MIN_YEAR = 1900
_DATE_MAX_YEAR = 2099

# 'day sep month sep year' with dot or single whitespace as separator, e.g.
# '02.08.2025' / '02 08 2025'. The month group allows up to 3 digits so a
# genuinely garbled field like '082' is captured whole (and then reported as
# implausible) instead of failing to match at all.
_DATE_COMPACT_RE = re.compile(r'\b(\d{1,2})[.\s](\d{1,3})[.\s](\d{4})\b')
# 'DD/MM/YYYY' or 'MM/DD/YYYY' -- ambiguous on its own, so validity below
# accepts either reading.
_DATE_SLASH_RE = re.compile(r'\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b')
# A bare 1-2 digit group glued onto an otherwise clean slash date, e.g. the
# measured '06 06/19/80' -- an extra fragment sitting next to a real date.
# Reported as torn without touching the date itself.
_DATE_TORN_PREFIX_RE = re.compile(r'\b(\d{1,2})\s+(\d{1,2}/\d{1,2}/\d{2,4})\b')
# Four separate 1-2 digit groups, e.g. '01 07 20 26' -- a year that has been
# torn into two chunks by OCR. Only flagged when the first two groups are
# themselves plausible day/month values, to keep this from firing on every
# unrelated run of small numbers.
_DATE_TORN4_RE = re.compile(r'\b(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\s+(\d{1,2})\b')


def _spans_overlap(span: tuple[int, int], consumed: list[tuple[int, int]]) -> bool:
    return any(span[0] < end and start < span[1] for start, end in consumed)


def _check_date_candidates(text: str, issues: list[str], counts: dict[str, int]) -> None:
    consumed: list[tuple[int, int]] = []

    # Torn forms first -- they must claim their span before the plain
    # compact/slash checks below get a chance to mis-read a fragment of them
    # as a clean (or cleanly-invalid) date.
    for match in _DATE_TORN_PREFIX_RE.finditer(text):
        stray = int(match.group(1))
        if stray > 31:
            continue
        span = match.span()
        consumed.append(span)
        counts['date_total'] += 1
        counts['date_implausible'] += 1
        issues.append(f"Datumsfeld zerrissen, nicht eindeutig lesbar: '{match.group(0)}'")

    for match in _DATE_TORN4_RE.finditer(text):
        span = match.span()
        if _spans_overlap(span, consumed):
            continue
        day, month = int(match.group(1)), int(match.group(2))
        if not (1 <= day <= 31 and 1 <= month <= 12):
            continue
        consumed.append(span)
        counts['date_total'] += 1
        counts['date_implausible'] += 1
        issues.append(f"Datumsfeld zerrissen, nicht eindeutig lesbar: '{match.group(0)}'")

    for match in _DATE_COMPACT_RE.finditer(text):
        span = match.span()
        if _spans_overlap(span, consumed):
            continue
        consumed.append(span)
        counts['date_total'] += 1
        day_str, month_str, year_str = match.groups()
        day, month, year = int(day_str), int(month_str), int(year_str)
        problems = []
        if not 1 <= day <= 31:
            problems.append(f'Tag {day_str} unplausibel')
        if len(month_str) > 2 or not 1 <= month <= 12:
            problems.append(f'Monat {month_str} unplausibel')
        if not _DATE_MIN_YEAR <= year <= _DATE_MAX_YEAR:
            problems.append(f'Jahr {year_str} unplausibel')
        if problems:
            counts['date_implausible'] += 1
            issues.append(f"Datum '{match.group(0)}': " + ', '.join(problems))

    for match in _DATE_SLASH_RE.finditer(text):
        span = match.span()
        if _spans_overlap(span, consumed):
            continue
        consumed.append(span)
        counts['date_total'] += 1
        a, b = int(match.group(1)), int(match.group(2))
        plausible = (1 <= a <= 12 and 1 <= b <= 31) or (1 <= a <= 31 and 1 <= b <= 12)
        if not plausible:
            counts['date_implausible'] += 1
            issues.append(
                f"Datum '{match.group(0)}': weder als TT/MM noch als MM/TT plausibel ({a}/{b})"
            )


# --- Waisen-Labels ---------------------------------------------------------------

# A placeholder value OCR leaves behind for an unfilled form field --
# underscores or dashes and nothing else.
_PLACEHOLDER_RE = re.compile(r'^[_\-]+$')
# 'Label: value' (or 'Label:' with nothing after it) on a plain line. The
# label side must contain a letter and stay short, which is what keeps this
# from matching a bare time like '14:30' (no letter) or a long sentence that
# merely happens to contain a colon somewhere past character 60.
_LABEL_LINE_RE = re.compile(r'^([^:\n]{1,60}):\s*(.*)$')


def _is_label_prefix(prefix: str) -> bool:
    prefix = prefix.strip()
    return bool(prefix) and any(ch.isalpha() for ch in prefix)


def _check_orphan_labels(text: str, counts: dict[str, int]) -> None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if stripped.startswith('|'):
            # Markdown table row: '| Label: | value |'. Split on '|' and
            # treat every cell that ends in ':' as its own label, paired with
            # the very next cell as its value (or '' if it was the last
            # cell). This is what correctly leaves the measured
            # '| Geburtsdatum: | 06 06/19/80 |' row alone here -- the value
            # cell is non-empty, so pairing is fine; the date-shaped mess in
            # it is 06/19/80's problem, caught above, not this check's.
            cells = [cell.strip() for cell in stripped.strip('|').split('|')]
            for idx, cell in enumerate(cells):
                if not cell.endswith(':') or not _is_label_prefix(cell[:-1]):
                    continue
                counts['labelled_lines'] += 1
                value = cells[idx + 1] if idx + 1 < len(cells) else ''
                if not value or _PLACEHOLDER_RE.match(value):
                    counts['orphan_labels'] += 1
            continue

        match = _LABEL_LINE_RE.match(stripped)
        if not match or not _is_label_prefix(match.group(1)):
            continue
        value = match.group(2).strip()
        # A colon immediately followed by '//' is a URL scheme separator
        # ('https://...'), not a label/value split -- catches this whether
        # the URL opens the line or sits mid-sentence ('Siehe https://...').
        if value.startswith('//'):
            continue
        counts['labelled_lines'] += 1
        if not value or _PLACEHOLDER_RE.match(value):
            counts['orphan_labels'] += 1


# --- Storno- vs. Buchungsdatum ------------------------------------------------

# Not one of the four enumerated checks, but the exact defect the task brief
# opens with ("ein Storno-Datum liegt vor dem Buchungsdatum"). Only fires
# when both labels are found with an unambiguous, individually-plausible
# 'DD sep MM sep YYYY' date within a short distance -- ambiguous or already-
# flagged-as-implausible dates are left alone rather than guessed at, in
# keeping with the module's report-only, don't-interpret-garbage stance.
_LABELLED_DATE_RE = re.compile(
    r'(Buchungsdatum|Storno(?:datum|-Datum)?)\s*:?\s*(\d{1,2})[.\s](\d{1,2})[.\s](\d{4})',
    re.IGNORECASE,
)


def _check_storno_before_buchung(text: str, issues: list[str]) -> None:
    buchung = None
    storno = None
    for match in _LABELLED_DATE_RE.finditer(text):
        label = match.group(1).lower()
        day, month, year = (int(match.group(i)) for i in (2, 3, 4))
        if not (1 <= day <= 31 and 1 <= month <= 12 and _DATE_MIN_YEAR <= year <= _DATE_MAX_YEAR):
            continue
        # Slice out just the date portion (groups 2-4), not the label text
        # captured in group 1, so the issue message reads 'Storno-Datum
        # 01.05.2024 ...' rather than repeating 'Stornodatum: 01.05.2024'.
        date_text = match.string[match.start(2):match.end(4)]
        ordinal = (year, month, day)
        if label == 'buchungsdatum' and buchung is None:
            buchung = (ordinal, date_text)
        elif label.startswith('storno') and storno is None:
            storno = (ordinal, date_text)

    if buchung is not None and storno is not None and storno[0] < buchung[0]:
        issues.append(
            f"Storno-Datum ({storno[1]}) liegt vor dem Buchungsdatum ({buchung[1]})"
        )


def validate_document(markdown: str) -> dict:
    """(issues, counts) over already-rendered markdown -- read-only.

    Never raises: empty input, input with zero matches, and very long input
    all return the same well-formed shape, just with empty issues / zeroed
    counts where nothing was found.
    """
    counts = {
        'iban_total': 0,
        'iban_invalid': 0,
        'icd_total': 0,
        'icd_invalid': 0,
        'date_total': 0,
        'date_implausible': 0,
        'orphan_labels': 0,
        'labelled_lines': 0,
    }
    issues: list[str] = []

    text = markdown or ''
    _check_iban_candidates(text, issues, counts)
    _check_icd_candidates(text, issues, counts)
    _check_date_candidates(text, issues, counts)
    _check_orphan_labels(text, counts)
    _check_storno_before_buchung(text, issues)

    return {'issues': issues, 'counts': counts}
