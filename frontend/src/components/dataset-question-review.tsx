'use client';

import { useEffect, useState } from 'react';
import { Archive, ArrowLeft, ArrowRight, Check } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { RagasValidationInfo } from '@/components/ragas-validation-info';
import { apiFetch } from '@/lib/api';

type Row = Record<string, unknown>;
type RejectedCandidate = {
  id?: string;
  question?: string;
  reason?: string | null;
  row?: Row | null;
  faithfulness?: number | null;
  answer_relevancy?: number | null;
  criteria?: Record<string, unknown>;
  criterion_reasons?: Record<string, unknown>;
  errors?: string[];
};
type Dataset = {
  path: string;
  rows: Row[];
  source_documents: string[];
  rejected_candidates?: RejectedCandidate[];
};
type ReviewEntry = { key: string; row: Row; rejectedCandidate?: RejectedCandidate };
type QuestionFilter = 'all' | 'approved' | 'failed';
type ArchiveEntry = {
  archive_id: string;
  dataset_path: string;
  row_id: string;
  reason?: string | null;
  note?: string;
  decided_at?: string;
  row?: Row | null;
};
type Decision = 'included' | 'excluded';
type Props = {
  dataset: Dataset;
  onDecide: (rowId: string, decision: Decision, reason?: string, note?: string) => Promise<boolean>;
  decidingRowId: string | null;
};

const REASONS = [
  ['not_answerable', 'Nicht aus der Quelle beantwortbar'],
  ['incorrect_answer', 'Antwort fachlich falsch'],
  ['incomplete_answer', 'Antwort unvollständig'],
  ['unclear_question', 'Frage unklar'],
  ['not_standalone', 'Ohne Kontext unverständlich'],
  ['uninformative', 'Keine nützliche Fachinformation'],
  ['duplicate', 'Doppelte Frage'],
  ['other', 'Anderer Grund'],
] as const;
const reasonLabel = (value: unknown) => REASONS.find(([key]) => key === value)?.[1] ?? 'Anderer Grund';
const text = (value: unknown) => typeof value === 'string' ? value : '';
const sourceName = (path: string) => path.split('/').at(-1) || path;
const isApproved = (row: Row) => row.review_decision !== 'excluded'
  && (row.validation_status !== 'review' || row.review_decision === 'included');
const isFailed = (entry: ReviewEntry) => {
  const criteria = entry.row.validation_criteria;
  return Boolean(entry.rejectedCandidate) || entry.row.validation_status === 'rejected'
    || (criteria !== null && typeof criteria === 'object' && Object.values(criteria).includes('fail'));
};
const filterEntries = (entries: ReviewEntry[], filter: QuestionFilter) => {
  if (filter === 'approved') return entries.filter((entry) => !entry.rejectedCandidate && isApproved(entry.row));
  if (filter === 'failed') return entries.filter(isFailed);
  return entries;
};

function rejectedRow(candidate: RejectedCandidate, sourceDocument: string): Row {
  return {
    ...(candidate.row ?? {}),
    id: candidate.id ?? candidate.row?.id,
    question: candidate.question ?? candidate.row?.question,
    source_document: candidate.row?.source_document ?? sourceDocument,
    generation_pipeline: 'ragas',
    validation_method: 'ragas-faithfulness-answer-relevancy-discrete-v1',
    validation_status: 'rejected',
    validation_faithfulness: candidate.faithfulness,
    validation_answer_relevancy: candidate.answer_relevancy,
    validation_criteria: candidate.criteria,
    validation_criterion_reasons: candidate.criterion_reasons,
    validation_errors: candidate.errors,
  };
}

export function DatasetQuestionReview({ dataset, onDecide, decidingRowId }: Props) {
  const [view, setView] = useState<'review' | 'archive'>('review');
  const [filter, setFilter] = useState<QuestionFilter>('all');
  const [index, setIndex] = useState(0);
  const [reason, setReason] = useState('other');
  const [note, setNote] = useState('');
  const [archive, setArchive] = useState<ArchiveEntry[]>([]);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  const [archiveVersion, setArchiveVersion] = useState(0);
  const sourceDocument = text(dataset.rows[0]?.source_document) || dataset.source_documents[0] || '';
  const approvedRows = dataset.rows.filter(isApproved);
  const allEntries: ReviewEntry[] = [
    ...dataset.rows.map((row, position) => ({ key: 'saved-' + position, row })),
    ...(dataset.rejected_candidates ?? []).map((candidate, position) => ({
      key: 'rejected-' + position, row: rejectedRow(candidate, sourceDocument), rejectedCandidate: candidate,
    })),
  ].sort((left, right) => text(left.row.id).localeCompare(text(right.row.id), 'de', { numeric: true }));
  const failedEntries = allEntries.filter(isFailed);
  const entries = filterEntries(allEntries, filter);
  const selectedIndex = Math.min(index, Math.max(0, entries.length - 1));
  const reasonCounts = REASONS.map(([key, label]) => ({
    key, label, count: archive.filter((entry) => entry.reason === key).length,
  })).filter((item) => item.count > 0);

  const entry = entries[selectedIndex];
  const row = entry?.row;
  const manuallyChecked = dataset.rows.filter((item) => item.review_decision === 'included').length;


  useEffect(() => {
    if (!sourceDocument) return;
    let cancelled = false;
    apiFetch('/api/v1/evaluation-datasets/question-archive?markdown_path=' + encodeURIComponent(sourceDocument), {
      cache: 'no-store',
    })
      .then(async (response) => {
        if (!response.ok) throw new Error('Fragenarchiv konnte nicht geladen werden.');
        return response.json();
      })
      .then((payload) => {
        if (!cancelled) {
          setArchive(Array.isArray(payload.items) ? payload.items as ArchiveEntry[] : []);
          setArchiveError(null);
        }
      })
      .catch((error) => {
        if (!cancelled) setArchiveError(error instanceof Error ? error.message : 'Fragenarchiv nicht verfügbar.');
      });
    return () => { cancelled = true; };
  }, [sourceDocument, archiveVersion]);

  function selectQuestion(nextIndex: number) {
    setIndex(nextIndex);
    setReason('other');
    setNote('');
  }

  function selectFilter(nextFilter: QuestionFilter) {
    const nextEntries = filterEntries(allEntries, nextFilter);
    const matchingIndex = nextEntries.findIndex((item) => item.key === entry?.key);
    setFilter(nextFilter);
    selectQuestion(Math.max(0, matchingIndex));
  }

  async function decide(decision: Decision) {
    if (!row || entry?.rejectedCandidate || decidingRowId) return;
    const rowId = text(row.id);
    const saved = await onDecide(rowId, decision, decision === 'excluded' ? reason : undefined, note);
    if (!saved) return;
    setArchiveVersion((previous) => previous + 1);
    setNote('');
    setReason('other');
    setIndex(filter === 'approved' && decision === 'excluded'
      ? selectedIndex : Math.min(selectedIndex + 1, entries.length - 1));
  }

  return (
    <section className="mt-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h4 className="font-semibold text-slate-900">Fragen prüfen</h4>
          <p className="text-xs text-slate-500">{approvedRows.length} zugelassen · {manuallyChecked} manuell bestätigt · {dataset.rows.filter((item) => item.review_decision === 'excluded').length} archiviert · {(dataset.rejected_candidates ?? []).length} automatisch verworfen</p>
        </div>
        <div className="flex gap-2">
          <Button type="button" size="sm" variant={view === 'review' ? 'default' : 'outline'}
            onClick={() => setView('review')}>Prüfmodus</Button>
          <Button type="button" size="sm" variant={view === 'archive' ? 'default' : 'outline'}
            onClick={() => setView('archive')}>Fragenarchiv ({archive.length})</Button>
        </div>
      </div>

      {view === 'archive' ? (
        <div className="mt-4 space-y-3">
          <p className="text-sm text-slate-600">Problematische Fragen zu diesem Markdown – auch aus älteren Generierungsläufen.</p>
          {reasonCounts.length > 0 && <div className="flex flex-wrap gap-2">
            {reasonCounts.map((item) => <span key={item.key} className="rounded-full border border-slate-200 bg-white px-2 py-1 text-xs text-slate-700">
              {item.label}: {item.count}
            </span>)}
          </div>}
          {archiveError && <p role="alert" className="text-sm text-rose-700">{archiveError}</p>}
          {!archiveError && archive.length === 0 && <p className="text-sm text-slate-500">Noch keine Fragen archiviert.</p>}
          {archive.map((entry) => {
            const archivedRow = entry.row ?? {};
            const canRestore = entry.dataset_path === dataset.path && dataset.rows.some((item) =>
              item.id === entry.row_id && item.review_decision === 'excluded'
              && item.question === archivedRow.question && item.gold_answer === archivedRow.gold_answer
            );
            return <article key={entry.archive_id} className="rounded-xl border border-slate-200 bg-slate-50 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-xs font-semibold text-rose-700">{reasonLabel(entry.reason)}</p>
                <p className="text-xs text-slate-500">{entry.decided_at ? new Date(entry.decided_at).toLocaleString('de-DE') : ''}</p>
              </div>
              <p className="mt-2 font-medium text-slate-900">{text(archivedRow.question)}</p>
              <p className="mt-2 text-sm text-slate-700"><span className="font-semibold">Goldantwort:</span> {text(archivedRow.gold_answer)}</p>
              {entry.note && <p className="mt-2 text-sm text-slate-700">Notiz: {entry.note}</p>}
              <details className="mt-2 text-sm text-slate-600">
                <summary className="cursor-pointer">Quelle und Prüfwerte</summary>
                <p className="mt-2 whitespace-pre-wrap rounded-lg bg-white p-3">{text(archivedRow.evidence_quote)}</p>
                <RagasValidationInfo row={archivedRow} />
                <p className="mt-2 text-xs">Dataset: {sourceName(entry.dataset_path)} · ID {entry.row_id}</p>
              </details>
              {canRestore && <Button type="button" size="sm" variant="outline" className="mt-3"
                disabled={decidingRowId !== null}
                onClick={() => void onDecide(entry.row_id, 'included').then((saved) => {
                  if (saved) setArchiveVersion((previous) => previous + 1);
                })}>
                Wiederherstellen
              </Button>}
            </article>;
          })}
        </div>
      ) : (
        <div className="mt-4">
          <div className="mb-3 flex flex-wrap items-center gap-2" role="group" aria-label="Fragenfilter">
            <Button type="button" size="sm" variant={filter === 'all' ? 'default' : 'outline'}
              aria-pressed={filter === 'all'} onClick={() => selectFilter('all')}>
              Alle Fragen ({allEntries.length})
            </Button>
            <Button type="button" size="sm" variant={filter === 'approved' ? 'default' : 'outline'}
              aria-pressed={filter === 'approved'} onClick={() => selectFilter('approved')}>
              Nur zugelassene ({approvedRows.length})
            </Button>
            <Button type="button" size="sm" variant={filter === 'failed' ? 'default' : 'outline'}
              aria-pressed={filter === 'failed'} onClick={() => selectFilter('failed')}>
              Nur nicht bestanden ({failedEntries.length})
            </Button>
          </div>
          {!row ? <p className="text-sm text-slate-500">In dieser Auswahl sind keine Fragen vorhanden.</p> : <>
            <div className="mb-3 flex justify-end">
              <div className="flex gap-2">
                <Button type="button" size="sm" variant="outline" disabled={selectedIndex === 0}
                  onClick={() => selectQuestion(selectedIndex - 1)}><ArrowLeft className="mr-1 h-4 w-4" />Zurück</Button>
                <Button type="button" size="sm" variant="outline" disabled={selectedIndex >= entries.length - 1}
                  onClick={() => selectQuestion(selectedIndex + 1)}>Weiter<ArrowRight className="ml-1 h-4 w-4" /></Button>
              </div>
            </div>
            {entry?.rejectedCandidate && <div className="mb-3 rounded-xl border border-amber-300 bg-amber-50 p-4 text-sm text-amber-950">
              <p><span className="font-semibold">Automatisch verworfen:</span> {entry.rejectedCandidate.reason || 'Grund nicht gespeichert.'}</p>
              {!text(row.question) && <p className="mt-1">Der Fragetext wurde in diesem älteren Lauf noch nicht dauerhaft gespeichert.</p>}
            </div>}
            {row.review_decision === 'excluded' && <div className="mb-3 rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-900">
              <p><span className="font-semibold">Manuell archiviert:</span> {reasonLabel(row.review_archive_reason)}</p>
              {text(row.review_archive_note) && <p className="mt-1">Notiz: {text(row.review_archive_note)}</p>}
            </div>}
            {(row.generation_pipeline === 'ragas' || typeof row.validation_method === 'string') &&
              <div className="mb-4"><RagasValidationInfo row={row} /></div>}
            <div className="space-y-4">
                <div className="rounded-xl border border-purple-200 bg-purple-50/40 p-5">
                  <p className="text-xs font-semibold uppercase tracking-wide text-purple-700">Frage {selectedIndex + 1} von {entries.length} · ID {text(row.id)}</p>
                  <p className="mt-2 text-lg font-medium text-slate-950">{text(row.question) || 'Fragetext für diesen älteren Lauf nicht verfügbar'}</p>
                  <p className="mt-5 text-xs font-semibold uppercase tracking-wide text-slate-600">Angebliche Goldantwort</p>
                  <p className="mt-2 whitespace-pre-wrap text-sm text-slate-900">{text(row.gold_answer) || 'Für diesen älteren Lauf nicht verfügbar.'}</p>
                  <p className="mt-5 text-xs font-semibold uppercase tracking-wide text-slate-600">Verwendete Textstelle</p>
                  <p className="mt-2 whitespace-pre-wrap rounded-lg border border-slate-200 bg-white p-3 text-sm text-slate-800">{text(row.evidence_quote) || 'Für diesen älteren Lauf nicht verfügbar.'}</p>
                  <p className="mt-3 text-xs text-slate-500">
                    {sourceName(text(row.source_file)) || 'Quelldatei nicht erfasst'}{typeof row.source_page === 'number' ? ' · Seite ' + row.source_page : ''}
                    {row.evidence_anchor ? ' · ' + text(row.evidence_anchor) : ''}
                  </p>
                </div>
                {!entry?.rejectedCandidate && (row.review_decision === 'excluded' ? <div className="rounded-xl border border-slate-200 bg-white p-4">
                  <p className="text-sm font-semibold text-slate-900">Deine Entscheidung</p>
                  <Button type="button" variant="outline" className="mt-3" disabled={decidingRowId !== null}
                    onClick={() => void decide('included')}><Check className="mr-2 h-4 w-4" />Wiederherstellen und zulassen</Button>
                </div> : <div className="rounded-xl border border-slate-200 bg-white p-4">
                  <p className="text-sm font-semibold text-slate-900">Deine Entscheidung</p>
                  <label className="mt-3 block text-xs font-medium text-slate-700" htmlFor="archive-reason">Grund fürs Archivieren (optional)</label>
                  <select id="archive-reason" value={reason} onChange={(event) => setReason(event.target.value)}
                    className="mt-1 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm">
                    {REASONS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
                  </select>
                  <input type="text" value={note} onChange={(event) => setNote(event.target.value)} maxLength={1000}
                    placeholder="Kurze Notiz (optional)" aria-label="Notiz zur Entscheidung"
                    className="mt-2 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm" />
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button type="button" disabled={decidingRowId !== null} onClick={() => void decide('included')}
                      className="bg-emerald-600 hover:bg-emerald-700"><Check className="mr-2 h-4 w-4" />Behalten</Button>
                    <Button type="button" variant="outline" disabled={decidingRowId !== null} onClick={() => void decide('excluded')}
                      className="border-rose-300 text-rose-700 hover:bg-rose-50"><Archive className="mr-2 h-4 w-4" />Archivieren</Button>
                  </div>
                  {row.review_decision === 'included' && <p className="mt-2 text-xs text-emerald-700">Bereits manuell bestätigt.</p>}
                </div>)}
            </div>
          </>}
        </div>
      )}
    </section>
  );
}
