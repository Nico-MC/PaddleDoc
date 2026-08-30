"""Repariert Formularfelder, die PaddleOCR-VL faelschlich als LaTeX klassifiziert.

WARUM dieses Modul existiert: PaddleOCR-VL erkennt vorgedruckte Unterstriche in
Formularen (die Linien fuer 'Tag __ Monat __ Jahr __') als Formelsatz und liefert
statt des eingetragenen Werts LaTeX-Markup, z.B.

    $$ \\begin{array}{c}\\underline{02}\\quad\\underline{082}\\quad\\underline{0\\underline{2}5}\\\\
       Tag\\quad Monat\\quad Jahr\\end{array} $$

Dieses Modul entscheidet je `$...$`/`$$...$$`-Span, ob es sich um ein solches
Formularfeld-Artefakt handelt (dann wird der LaTeX-Muell zu Klartext eingedampft)
oder um echte Mathematik (dann bleibt der Span woertlich stehen -- lieber ein
liegen gelassenes Formular-Artefakt als eine zerstoerte Formel).

Drei Gates muessen ALLE passieren, sonst bleibt der Span unveraendert:
  1. Hartes Veto -- sichere Signale echter Mathematik (siehe _HARD_VETO_RE).
  2. Whitelist -- nach dem Flatten darf kein fremdes Makro mehr uebrig sein.
     Das wird nicht als separate Namensliste durchgesetzt, sondern dadurch, dass
     _flatten_to_fixpoint() ausschliesslich die whitelisteten Makros abbaut;
     jedes andere Makro ueberlebt als woertliches '\\foo' und faellt danach beim
     Sicherheitsnetz (_is_unsafe) auf, das auf uebrig gebliebene '\\', '{', '}',
     '$' prueft.
  3. Positivbeleg -- ohne Unterstreichungs-Makro, Checkmark oder Formularvokabular
     wird nichts angefasst (verhindert z.B., dass ein Waehrungs-$-Paar in
     normalem Fliesstext faelschlich freigegeben wird).
"""

from __future__ import annotations

import re

# --- Gate 1: hartes Veto -----------------------------------------------------
# Sichere Signale echter Mathematik. Jedes Vorkommen -> Span bleibt woertlich.
# Macro-Namen mit (?![A-Za-z]) abgesichert, damit z.B. '\bar' nicht auf ein
# woertliches '\barcode' anspringt.
_HARD_VETO_MACROS = (
    'sum', 'prod', 'int', 'oint', 'lim', 'sqrt', 'partial', 'nabla', 'infty',
    'forall', 'exists', 'alpha', 'beta', 'gamma', 'delta', 'theta', 'lambda',
    'mu', 'sigma', 'pi', 'omega', 'Delta', 'Omega', 'leq', 'geq', 'neq',
    'approx', 'equiv', 'propto', 'subset', 'cup', 'cap', 'to', 'rightarrow',
    'mapsto', 'left', 'right', 'binom', 'matrix', 'vec', 'hat', 'bar', 'log',
    'ln', 'exp', 'sin', 'cos', 'tan', 'perp', 'pm',
)
_HARD_VETO_RE = re.compile(
    r'\^|\\(?:' + '|'.join(_HARD_VETO_MACROS) + r')(?![A-Za-z])'
)
# \begin{...} ist ein Veto, AUSSER die Umgebung ist 'array' -- Formularraster
# nutzen \begin{array}{c}...\end{array} als reines Layout-Geruest.
_BEGIN_ENV_RE = re.compile(r'\\begin\{([A-Za-z*]+)\}')

# --- Gate 3: Positivbeleg -----------------------------------------------------
# Bewusst OHNE 'ja', 'nein', 'von', 'bis', 'Ort', 'Code' -- diese Allerweltswoerter
# wuerden ein Waehrungs-$-Paar in normalem Fliesstext faelschlich freigeben.
_FORM_VOCAB = (
    'Tag', 'Monat', 'Jahr', 'Datum', 'Geburtsdatum', 'Buchungsdatum', 'Storno',
    'ICD-10', 'ICD10', 'Versicherungsnummer', 'Vorname', 'Nachname', 'PLZ',
    'Bezeichnung',
)
_FORM_VOCAB_RE = re.compile(r'\b(?:' + '|'.join(re.escape(w) for w in _FORM_VOCAB) + r')\b')
_POSITIVE_MACRO_RE = re.compile(r'\\(?:underline|overline|underbrace|checkmark)\b')

# --- Strukturelles Entfernen (VOR Gate 2 / vor dem Flatten) ------------------
# \begin{array}{...}, \end{array}, \hline und Zeilenumbrueche (\\) sind reines
# Layout-Geruest ohne inhaltlichen Wert. Sie muessen weg, BEVOR das restliche
# Flatten laeuft -- sonst kann ein Umgebungsrest ein Makro-Token zerreissen
# (z.B. \ICD10Code) und das Sicherheitsnetz faelschlich am Token 'ICD' kippen.
_ARRAY_BEGIN_RE = re.compile(r'\\begin\{array\}(?:\{[^{}]*\})?')
_ARRAY_END_RE = re.compile(r'\\end\{array\}')
_HLINE_RE = re.compile(r'\\hline')
_LINEBREAK_RE = re.compile(r'\\\\')

# --- Flatten (Fixpunkt, innerste Klammer zuerst) -----------------------------
_CHECKMARK_RE = re.compile(r'\\checkmark')
_UNDERBRACE_RE = re.compile(r'\\underbrace\{([^{}]*)\}_\{([^{}]*)\}')
# Einargumentige Wrapper: der Inhalt ist die eigentliche Information, das Makro
# selbst nur Formatierung -- wird durch Leerzeichen ersetzt, nicht geloescht,
# damit sich benachbarte Felder (\underline{01}\underline{07}) nicht zu '0107'
# verkleben.
_WRAPPER_RE = re.compile(r'\\(?:underline|overline|text|mathrm|mathbf|textbf|textit)\{([^{}]*)\}')
_SUBSCRIPT_BRACE_RE = re.compile(r'_\{([^{}]*)\}')
_SUBSCRIPT_BARE_RE = re.compile(r'_([A-Za-z0-9]+)')
# Der Nenner eines \frac in einem Formularfeld ist die Feldbeschriftung, kein
# Bruchstrich -- also KEIN '/' im Ersatztext, sonst laesst es sich nicht mehr
# von einem echten Bruch unterscheiden.
_FRAC_RE = re.compile(r'\\frac\{([^{}]*)\}\{([^{}]*)\}')
_MAX_FLATTEN_ROUNDS = 12

# Spacer-Makros -- werden erst NACH dem Klammer-Fixpunkt zu Leerzeichen, weil
# sie oft erst durch das Aufloesen von _{...} oder \underbrace{...} freigelegt
# werden (z.B. '_{Tag\quad Monat}' -> ' Tag\quad Monat ' -> ' Tag Monat ').
# The trailing `\\ ` (backslash-space) is LaTeX's explicit inter-word space.
# Without it the safety net rejects an otherwise clean form field: measured on
# the real corpus, `$$ \underline{09}_{Tag}\ \underline{07}_{Monat}\ ... $$`
# flattened to text that still carried a stray backslash and was therefore
# discarded. It must stay LAST in the alternation -- `\\ ` would otherwise
# shadow the longer named spacers that also begin with a backslash.
_SPACER_RE = re.compile(r'\\qquad|\\quad|\\atop|\\cdot|\\,|\\;|\\!|\\:|\\ ')

# Confusables, wie sie in gescannten Formularen tatsaechlich vorkommen: Vollbreiten-
# Punkt/Datumstrenner und kreisfoermige Null-Varianten aus OCR-Ziffernsaetzen.
_CONFUSABLES = {
    '\uff0e': '.',  # FULLWIDTH FULL STOP
    '\u2024': '.',  # ONE DOT LEADER
    '\u25cb': '0',  # WHITE CIRCLE
    '\u25ef': '0',  # LARGE CIRCLE
    '\u3007': '0',  # IDEOGRAPHIC NUMBER ZERO
}
_CONFUSABLES_RE = re.compile('|'.join(re.escape(c) for c in _CONFUSABLES))

# Sicherheitsnetz nach dem Flatten: ueberlebt noch '\', '{', '}' oder '$', oder
# gibt es gar kein alphanumerisches Zeichen -> etwas ist schiefgelaufen (fremdes
# Makro, unausgewogene Klammer, leerer Rest) -- Original woertlich behalten.
_UNSAFE_LEFTOVER_RE = re.compile(r'[\\{}$]')
_HAS_ALNUM_RE = re.compile(r'[A-Za-z0-9]')

# Findet Display- ($$...$$) und Inline-Spans ($...$, nicht $$), DOTALL weil
# ein Formularfeld-Span ueber Zeilenumbrueche gehen kann (\\ im array).
_SPAN_RE = re.compile(r'\$\$(.*?)\$\$|\$(.*?)\$', re.DOTALL)


def _has_hard_veto(raw: str) -> bool:
    if _HARD_VETO_RE.search(raw):
        return True
    return any(m.group(1) != 'array' for m in _BEGIN_ENV_RE.finditer(raw))


def _has_positive_evidence(raw: str) -> bool:
    return bool(_POSITIVE_MACRO_RE.search(raw) or _FORM_VOCAB_RE.search(raw))


def _strip_structural(s: str) -> str:
    s = _ARRAY_BEGIN_RE.sub(' ', s)
    s = _ARRAY_END_RE.sub(' ', s)
    s = _HLINE_RE.sub(' ', s)
    s = _LINEBREAK_RE.sub(' ', s)
    return s


def _flatten_to_fixpoint(s: str) -> str:
    for _ in range(_MAX_FLATTEN_ROUNDS):
        before = s
        s = _CHECKMARK_RE.sub(' [x] ', s)
        s = _UNDERBRACE_RE.sub(r' \1 \2 ', s)
        s = _WRAPPER_RE.sub(r' \1 ', s)
        s = _SUBSCRIPT_BRACE_RE.sub(r' \1 ', s)
        s = _SUBSCRIPT_BARE_RE.sub(r' \1 ', s)
        s = _FRAC_RE.sub(r' \1 \2 ', s)
        if s == before:
            break
    return s


def _repair_excess_closing_braces(s: str) -> str:
    # Nur ueberzaehlige SCHLIESSENDE Klammern reparieren -- ein fehlendes '{'
    # waere ein echtes Strukturproblem und soll das Sicherheitsnetz ausloesen,
    # ein verwaistes '}' dagegen ist meist ein harmloser Rest aus verschachtelten
    # Wrappern und darf stillschweigend entfernt werden.
    out: list[str] = []
    depth = 0
    for ch in s:
        if ch == '{':
            depth += 1
            out.append(ch)
        elif ch == '}':
            if depth > 0:
                depth -= 1
                out.append(ch)
            # sonst: ueberzaehlige schliessende Klammer verwerfen
        else:
            out.append(ch)
    return ''.join(out)


def _is_unsafe(s: str) -> bool:
    if _UNSAFE_LEFTOVER_RE.search(s):
        return True
    return not _HAS_ALNUM_RE.search(s)


def _process_span(raw: str) -> str | None:
    """Verarbeitet den Inhalt EINES `$...$`/`$$...$$`-Spans (ohne Dollarzeichen).

    Gibt den flachen Klartext zurueck, wenn alle drei Gates passieren, sonst
    None (Aufrufer behaelt dann den Span woertlich).
    """
    if _has_hard_veto(raw):
        return None
    if not _has_positive_evidence(raw):
        return None

    working = _strip_structural(raw)
    working = _flatten_to_fixpoint(working)
    working = _SPACER_RE.sub(' ', working)
    working = _CONFUSABLES_RE.sub(lambda m: _CONFUSABLES[m.group(0)], working)
    working = _repair_excess_closing_braces(working)

    if _is_unsafe(working):
        return None

    return re.sub(r'\s+', ' ', working).strip()


def normalize_form_latex(text: str) -> tuple[str, int]:
    """Ersetzt Formularfeld-LaTeX-Artefakte durch Klartext.

    Returns:
        (normalisierter Text, Anzahl geflatteter Spans)
    """
    flattened_count = 0

    def _repl(m: re.Match[str]) -> str:
        nonlocal flattened_count
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        replacement = _process_span(raw)
        if replacement is None:
            return m.group(0)
        flattened_count += 1
        return replacement

    normalized = _SPAN_RE.sub(_repl, text)
    return normalized, flattened_count


# --- Optionale Stufe 2: verstreute ICD-10-Codes wieder zusammenziehen -------
# WARUM eigene Funktion: PaddleOCR-VL setzt Spacer-Makros (\quad, \,, Subscript)
# zwischen den Buchstaben-Praefix und die Diagnoseziffern eines ICD-10-Codes;
# normalize_form_latex() flacht diese Makros zu Leerzeichen ab, wodurch aus
# 'F43.2' 'F 43 . 2' wird. Diese Funktion zieht das wieder zusammen -- bewusst
# NUR fuer den Buchstaben-Fall (Praefix-Buchstabe + 2 Ziffern + Punkt + 1-2
# Ziffern), denn das ist die einzige ICD-10-Form mit einem eindeutigen Anker.
# Ein rein numerischer Code haette keinen verlaesslichen Anker, der ihn von
# einer gewoehnlichen Dezimalzahl unterscheidet -- der wird bewusst NICHT
# angefasst, um keine beliebigen Zahlen im Dokument zu verstuemmeln.
_ICD_JOIN = re.compile(r'\b([A-TV-Z])\s*([0-9])\s*([0-9])\s*[.]\s*([0-9]{1,2})\b')


def join_split_icd_codes(text: str) -> str:
    """Zieht durch Flatten auseinandergerissene ICD-10-Codes wieder zusammen.

    Nur der Buchstaben-Fall (z.B. 'F 43 . 2' -> 'F43.2') wird repariert, siehe
    Modul-Kommentar oben.
    """
    return _ICD_JOIN.sub(
        lambda m: f'{m.group(1)}{m.group(2)}{m.group(3)}.{m.group(4)}', text
    )
