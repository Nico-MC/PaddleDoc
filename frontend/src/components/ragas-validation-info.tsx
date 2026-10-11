type ValidationRow = {
  generation_pipeline?: unknown;
  generation_question_target?: unknown;
  validation_method?: unknown;
  validation_status?: unknown;
  validation_faithfulness?: unknown;
  validation_answer_relevancy?: unknown;
  validation_model?: unknown;
  validation_errors?: unknown;
  review_decision?: unknown;
  review_decided_at?: unknown;
  generation_outcome?: unknown;
};

const STATUS_LABELS: Record<string, string> = {
  accepted: 'Automatisch akzeptiert',
  review: 'Entscheidung offen',
  'edited-unvalidated': 'Nach Bearbeitung ungeprüft',
};

type Metric = 'faithfulness' | 'relevancy';

type RejectedCandidate = {
  id?: string; question?: string; reason?: string | null;
  faithfulness?: number | null; answer_relevancy?: number | null;
};

type GenerationOutcome = {
  target: number; generated: number; saved: number;
  rejected_candidates: RejectedCandidate[];
  incomplete_regions?: Array<{ region: number; target: number; generated: number; reason: string }>;
};

function metricProblem(error: string): string {
  const [metric, errorType, ...message] = error.split(': ');
  const label = metric === 'answer_relevancy' ? 'Antwort-Relevanz' : metric === 'faithfulness' ? 'Faithfulness' : metric;
  if (!errorType) return label + ' konnte nicht berechnet werden.';
  const detail = message.join(': ');
  return label + ' konnte nicht berechnet werden (' + errorType + (detail ? ': ' + detail : '') + ').'
    + (detail ? '' : ' Dieser Lauf hat nur den Fehlertyp gespeichert; die konkrete Ursache ist nicht mehr feststellbar.');
}

function rejectedCandidateReason(candidate: RejectedCandidate): string {
  if (candidate.reason) return candidate.reason;
  if (candidate.faithfulness != null && candidate.faithfulness < 0.5) {
    return 'Faithfulness ' + candidate.faithfulness.toFixed(2) + ' unter Ablehnungsgrenze 0.50';
  }
  if (candidate.answer_relevancy != null && candidate.answer_relevancy < 0.4) {
    return 'Antwort-Relevanz ' + candidate.answer_relevancy.toFixed(2) + ' unter Ablehnungsgrenze 0.40';
  }
  return 'bei der automatischen Validierung verworfen';
}

// Heuristic display bands mirror the validator's accept/reject thresholds.
const THRESHOLDS: Record<Metric, { good: number; critical: number }> = {
  faithfulness: { good: 0.85, critical: 0.50 },
  relevancy: { good: 0.70, critical: 0.40 },
};

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

function metricStats(
  rows: ValidationRow[], field: 'validation_faithfulness' | 'validation_answer_relevancy', metric: Metric,
) {
  const values = rows.map((row) => row[field]).filter(
    (value): value is number => typeof value === 'number' && Number.isFinite(value),
  );
  if (values.length === 0) return null;
  return {
    count: values.length,
    mean: values.reduce((total, value) => total + value, 0) / values.length,
    min: Math.min(...values),
    max: Math.max(...values),
    good: values.filter((value) => value >= THRESHOLDS[metric].good).length,
    review: values.filter((value) => value >= THRESHOLDS[metric].critical && value < THRESHOLDS[metric].good).length,
    critical: values.filter((value) => value < THRESHOLDS[metric].critical).length,
  };
}

function isRagasRow(row: ValidationRow): boolean {
  return row.generation_pipeline === 'ragas'
    || row.validation_method === 'ragas-faithfulness-answer-relevancy';
}

export function RagasValidationInfo({ row }: { row: ValidationRow }) {
  if (!isRagasRow(row)) return null;
  const status = typeof row.validation_status === 'string' ? row.validation_status : '';
  const decision = typeof row.review_decision === 'string' ? row.review_decision : 'pending';
  const needsReview = status !== 'accepted';
  const statusLabel = status === 'review' && decision === 'included' ? 'Zur Evaluation zugelassen'
    : status === 'review' && decision === 'excluded' ? 'Von der Evaluation ausgeschlossen'
    : STATUS_LABELS[status] ?? 'Noch nicht automatisch geprüft';
  const errors = Array.isArray(row.validation_errors)
    ? row.validation_errors.filter((entry): entry is string => typeof entry === 'string')
    : [];

  const wasValidated = row.validation_method === 'ragas-faithfulness-answer-relevancy';
  return (
    <div className={'mt-3 rounded-lg border p-3 text-xs ' + (needsReview
      ? 'border-amber-200 bg-amber-50 text-amber-900'
      : 'border-emerald-200 bg-emerald-50 text-emerald-900')}>
      <p className="font-semibold">{statusLabel}</p>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-2">
        <span className="flex items-center gap-2">
          Faithfulness (Belegbarkeit): <ScoreBadge value={row.validation_faithfulness} metric="faithfulness" />
        </span>
        <span className="flex items-center gap-2">
          Antwort-Relevanz: <ScoreBadge value={row.validation_answer_relevancy} metric="relevancy" />
        </span>
      </div>
      {!wasValidated && status !== 'edited-unvalidated' && (
        <p className="mt-1">Für diese Frage wurden keine RAGAS-Validierungsergebnisse gespeichert.</p>
      )}
      {typeof row.validation_model === 'string' && row.validation_model && (
        <p className="mt-1 break-all">Prüfmodell: {row.validation_model}</p>
      )}
      {errors.length > 0 && (
        <div className="mt-2 space-y-1 rounded-md border border-amber-300 bg-white p-2 text-amber-900">
          {errors.map((entry, index) => <p key={index}>{metricProblem(entry)}</p>)}
          <p>Ein fehlender Score ist keine Nullbewertung. {decision === 'pending' ? 'Entscheide bei dieser Frage, ob sie in die Evaluation aufgenommen wird.' : 'Die gespeicherte Entscheidung ändert den automatischen Score nicht.'}</p>
        </div>
      )}
      {status === 'review' && decision !== 'pending' && typeof row.review_decided_at === 'string' && (
        <p className="mt-1">Entscheidung gespeichert am {new Date(row.review_decided_at).toLocaleString('de-DE')}.</p>
      )}
      <p className="mt-1 opacity-80">
        Automatische Scores von 0 bis 1; keine fachliche Freigabe. Prüfe Antwort und Quellenstelle selbst.
      </p>
    </div>
  );
}

export function RagasValidationSummary({ rows }: { rows: ValidationRow[] }) {
  const ragasRows = rows.filter(isRagasRow);
  if (ragasRows.length === 0) return null;
  const rawOutcome = ragasRows[0]?.generation_outcome;
  const outcome = rawOutcome && typeof rawOutcome === 'object'
    && typeof (rawOutcome as GenerationOutcome).target === 'number'
    ? rawOutcome as GenerationOutcome : null;
  const accepted = ragasRows.filter((row) => row.validation_status === 'accepted').length;
  const reviewRows = ragasRows.filter((row) => row.validation_status === 'review');
  const includedReview = reviewRows.filter((row) => row.review_decision === 'included').length;
  const excluded = reviewRows.filter((row) => row.review_decision === 'excluded').length;
  const pending = reviewRows.length - includedReview - excluded;
  const unchecked = ragasRows.length - accepted - reviewRows.length;
  const activeRows = ragasRows.filter((row) => row.review_decision !== 'excluded'
    && (row.validation_status !== 'review' || row.review_decision === 'included'));
  const neverValidated = ragasRows.filter((row) => row.validation_method !== 'ragas-faithfulness-answer-relevancy'
    && row.validation_status !== 'edited-unvalidated').length;
  const metrics = [
    { label: 'Faithfulness · Belegbarkeit', metric: 'faithfulness' as const,
      stats: metricStats(activeRows, 'validation_faithfulness', 'faithfulness') },
    { label: 'Antwort-Relevanz', metric: 'relevancy' as const,
      stats: metricStats(activeRows, 'validation_answer_relevancy', 'relevancy') },
  ];
  const targets = ragasRows.map((row) => row.generation_question_target);
  const target = targets.length > 0
    && targets.every((value) => value === targets[0])
    && typeof targets[0] === 'number' && targets[0] > 0
    ? targets[0] : null;
  return (
    <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm text-slate-700">
      <p className="font-medium">RAGAS-Prüfung im Dataset</p>
      <p className="mt-1">
        {ragasRows.length}{target ? ' von ' + target : ''} RAGAS-Fragen gespeichert ·
        {' '}{activeRows.length} für die Evaluation zugelassen · {pending} Entscheidung offen · {excluded} ausgeschlossen
        {unchecked > 0 ? ' · ' + unchecked + ' ohne RAGAS-Status' : ''}
      </p>
      {outcome && outcome.saved === rows.length && outcome.saved < outcome.target && (
        <div className="mt-2 rounded-lg border border-amber-300 bg-amber-50 p-3 text-xs text-amber-950">
          <p className="font-semibold">Ursprünglicher Generierungslauf: {outcome.generated} Kandidaten erzeugt, {outcome.saved} von {outcome.target} Fragen gespeichert.</p>
          {Math.max(0, outcome.target - outcome.generated) > 0 && <p className="mt-1">{outcome.target - outcome.generated} Frage(n) konnten aus den ausgewählten Textstellen nicht erzeugt werden.</p>}
          {Array.isArray(outcome.rejected_candidates) && outcome.rejected_candidates.map((candidate, index) => (
            <p key={candidate.id ?? index} className="mt-1 break-words">
              {candidate.id ?? 'Kandidat'} wurde generiert, aber nicht gespeichert: {rejectedCandidateReason(candidate)}.
              {candidate.question ? ' Frage: ' + candidate.question : ''}
            </p>
          ))}
        </div>
      )}
      {neverValidated > 0 && (
        <p className="mt-2 text-xs text-amber-800">
          Für {neverValidated} Frage{neverValidated === 1 ? '' : 'n'} wurden keine RAGAS-Validierungsergebnisse gespeichert.
          Fehlende Scores sind keine Nullbewertung.
        </p>
      )}
      <div className="mt-3 grid gap-2 sm:grid-cols-2">
        {metrics.map(({ label, metric, stats }) => (
          <div key={label} className="rounded-lg border border-slate-200 bg-white p-3">
            <p className="text-xs font-medium text-slate-600">{label}</p>
            <div className="mt-2 flex items-center gap-2 text-base">
              <ScoreBadge value={stats?.mean} metric={metric} />
              <span className="text-xs text-slate-500">Mittelwert</span>
            </div>
            <p className="mt-1 text-xs text-slate-500">
              {stats
                ? 'Spanne ' + stats.min.toFixed(2) + '–' + stats.max.toFixed(2) + ' · ' + stats.count + ' bewertet'
                : 'Keine auswertbaren Scores'}
            </p>
            {stats && (
              <p className="mt-1 text-xs text-slate-600">
                <span className="text-emerald-800">{stats.good} gut</span>
                {' · '}<span className="text-amber-800">{stats.review} prüfen</span>
                {' · '}<span className="text-rose-800">{stats.critical} kritisch</span>
              </p>
            )}
          </div>
        ))}
      </div>
      <p className="mt-2 text-xs text-slate-500">
        Farbstufen (heuristisch): Faithfulness gut ab 0,85, kritisch unter 0,50;
        Antwort-Relevanz gut ab 0,70, kritisch unter 0,40. Dazwischen: prüfen.
        {' '}
        Mittelwerte der für die Evaluation zugelassenen Fragen mit vorhandenem Score.
        Verworfene Kandidaten sind nicht enthalten; die Werte ersetzen keine fachliche Prüfung.
      </p>
    </div>
  );
}
