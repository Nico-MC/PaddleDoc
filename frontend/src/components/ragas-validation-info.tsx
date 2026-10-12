type ValidationRow = {
  generation_pipeline?: unknown;
  validation_method?: unknown;
  validation_faithfulness?: unknown;
  validation_answer_relevancy?: unknown;
  validation_criteria?: unknown;
  validation_criterion_reasons?: unknown;
  validation_model?: unknown;
  validation_errors?: unknown;
};

type Metric = 'faithfulness' | 'relevancy';
const CRITERION_LABELS: Record<string, string> = {
  clarity: 'Klarheit', standalone: 'Eigenständigkeit', answerability: 'Beantwortbarkeit',
  correctness: 'Fachliche Korrektheit', completeness: 'Vollständigkeit', informativeness: 'Informationsgehalt',
};
const CRITERION_STYLES: Record<string, string> = {
  pass: 'border-emerald-200 bg-emerald-100 text-emerald-900',
  review: 'border-amber-200 bg-amber-100 text-amber-900',
  fail: 'border-rose-200 bg-rose-100 text-rose-900',
};
const CRITERION_STATUS_LABELS: Record<string, string> = { pass: 'Bestanden', review: 'Prüfen', fail: 'Nicht bestanden' };
const THRESHOLDS: Record<Metric, { good: number; critical: number }> = {
  faithfulness: { good: 0.85, critical: 0.50 },
  relevancy: { good: 0.70, critical: 0.40 },
};

function metricProblem(error: string): string {
  const [metric, errorType, ...message] = error.split(': ');
  const label = metric === 'answer_relevancy' ? 'Antwort-Relevanz' : metric === 'faithfulness' ? 'Faithfulness' : metric;
  if (!errorType) return label + ' konnte nicht berechnet werden.';
  const detail = message.join(': ');
  return label + ' konnte nicht berechnet werden (' + errorType + (detail ? ': ' + detail : '') + ').'
    + (detail ? '' : ' Der konkrete Grund wurde in diesem Lauf nicht gespeichert.');
}

function scoreBand(value: unknown, metric: Metric) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return { label: 'Kein Wert', classes: 'border-slate-200 bg-slate-100 text-slate-600' };
  }
  if (value >= THRESHOLDS[metric].good) {
    return { label: 'Gut', classes: 'border-emerald-200 bg-emerald-100 text-emerald-900' };
  }
  if (value < THRESHOLDS[metric].critical) {
    return { label: 'Kritisch', classes: 'border-rose-200 bg-rose-100 text-rose-900' };
  }
  return { label: 'Prüfen', classes: 'border-amber-200 bg-amber-100 text-amber-900' };
}

function ScoreBadge({ value, metric }: { value: unknown; metric: Metric }) {
  const band = scoreBand(value, metric);
  return (
    <span className={'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 font-semibold ' + band.classes}>
      {typeof value === 'number' && Number.isFinite(value) ? value.toFixed(2) : '–'}
      <span className="font-normal">· {band.label}</span>
    </span>
  );
}

export function RagasValidationInfo({ row }: { row: ValidationRow }) {
  const isRagas = row.generation_pipeline === 'ragas'
    || (typeof row.validation_method === 'string' && row.validation_method.startsWith('ragas-faithfulness-answer-relevancy'));
  if (!isRagas) return null;
  if (row.validation_method === 'skipped') {
    return <p className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">Automatische Validierung war für diesen Lauf deaktiviert.</p>;
  }

  const criteria = row.validation_criteria && typeof row.validation_criteria === 'object'
    ? row.validation_criteria as Record<string, unknown> : null;
  const criterionReasons = row.validation_criterion_reasons && typeof row.validation_criterion_reasons === 'object'
    ? row.validation_criterion_reasons as Record<string, unknown> : null;
  const criterionResults = criteria ? Object.entries(CRITERION_LABELS).map(([key, label]) => ({
    key,
    label,
    value: typeof criteria[key] === 'string' ? criteria[key] as string : 'review',
    reason: typeof criterionReasons?.[key] === 'string' ? criterionReasons[key] as string : '',
  })) : [];
  const flaggedReasons = criterionResults.filter(({ value, reason }) => value !== 'pass' && reason);
  const passedReasons = criterionResults.filter(({ value, reason }) => value === 'pass' && reason);
  const errors = Array.isArray(row.validation_errors)
    ? row.validation_errors.filter((entry): entry is string => typeof entry === 'string') : [];
  const hasResults = typeof row.validation_faithfulness === 'number'
    || typeof row.validation_answer_relevancy === 'number' || criteria !== null;

  return (
    <div className="rounded-xl border border-slate-200 bg-white p-3 text-sm text-slate-800">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <div className="inline-flex items-center gap-2">
          <span className="font-medium">Faithfulness</span>
          <ScoreBadge value={row.validation_faithfulness} metric="faithfulness" />
        </div>
        <div className="inline-flex items-center gap-2">
          <span className="font-medium">Antwort-Relevanz</span>
          <ScoreBadge value={row.validation_answer_relevancy} metric="relevancy" />
        </div>
        {criterionResults.map(({ key, label, value }) => (
          <div key={key} className="inline-flex items-center gap-2">
            <span className="font-medium">{label}</span>
            <span className={'rounded-full border px-2 py-0.5 text-xs ' + (CRITERION_STYLES[value] ?? CRITERION_STYLES.review)}>
              {CRITERION_STATUS_LABELS[value] ?? 'Prüfen'}
            </span>
          </div>
        ))}
      </div>
      {flaggedReasons.length > 0 && <div className="mt-2 space-y-1 border-t border-slate-100 pt-2 text-xs text-slate-700">
        {flaggedReasons.map(({ key, label, reason }) => <p key={key}><span className="font-medium">{label}:</span> {reason}</p>)}
      </div>}
      {passedReasons.length > 0 && <details className="mt-2 text-xs text-slate-600">
        <summary className="cursor-pointer">Begründungen zu bestandenen Kriterien ({passedReasons.length})</summary>
        <div className="mt-1 space-y-1">
          {passedReasons.map(({ key, label, reason }) => <p key={key}><span className="font-medium">{label}:</span> {reason}</p>)}
        </div>
      </details>}
      {!hasResults && <p className="mt-3 text-xs text-slate-600">Für diese Frage wurden keine Validierungsergebnisse gespeichert.</p>}
      {errors.length > 0 && <div className="mt-3 space-y-1 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-950">
        {errors.map((error, index) => <p key={index}>{metricProblem(error)}</p>)}
      </div>}
      {typeof row.validation_model === 'string' && row.validation_model &&
        <p className="mt-3 break-all text-xs text-slate-500">Prüfmodell: {row.validation_model}</p>}
    </div>
  );
}
