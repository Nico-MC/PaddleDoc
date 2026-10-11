'use client';

import { useCallback, useEffect, useState } from 'react';
import { Archive, Download, RotateCcw, Sparkles, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { DatasetGenerator } from '@/components/dataset-generator';
import { RagasValidationInfo, RagasValidationSummary } from '@/components/ragas-validation-info';
import { apiFetch } from '@/lib/api';

type DatasetEntry = {
  path: string;
  filename: string;
  row_count: number;
  source_documents: string[];
  source_files: string[];
  source_file_sha256?: string | null;
  source_markdown_sha256?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
};

type DatasetDetail = DatasetEntry & { rows: Record<string, unknown>[] };

type MarkdownEntry = {
  path: string;
  filename: string;
  original_filename: string;
  original_extension: string;
  workspace_folder: string;
  source_file_sha256?: string | null;
  source_markdown_sha256?: string | null;
  document_version?: number;
};

type Props = {
  datasets: DatasetEntry[];
  markdownFiles: MarkdownEntry[];
  preferredMarkdownPath: string;
  selectedDatasetPath: string;
  onSelectDataset: (path: string) => void;
  onDatasetSaved: (path: string) => Promise<void> | void;
};

const sourceFilename = (path: string) => path.split('/').at(-1) || path;

const formatCreatedAt = (value?: string | null) => {
  if (!value) return 'Erstellungszeitpunkt nicht erfasst';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return 'Erstellungszeitpunkt nicht erfasst';
  return 'Erstellt ' + new Intl.DateTimeFormat('de-DE', {
    dateStyle: 'medium', timeStyle: 'short',
  }).format(date);
};

export function EncourageDatasetWorkbench({
  datasets, markdownFiles, preferredMarkdownPath, selectedDatasetPath,
  onSelectDataset, onDatasetSaved,
}: Props) {
  const [archivedDatasets, setArchivedDatasets] = useState<DatasetEntry[]>([]);
  const [showArchivedDatasets, setShowArchivedDatasets] = useState(false);
  const [isLoadingArchive, setIsLoadingArchive] = useState(false);
  const [isDatasetActionRunning, setIsDatasetActionRunning] = useState(false);
  const [showGenerator, setShowGenerator] = useState(false);
  const [details, setDetails] = useState<DatasetDetail | null>(null);
  const [isLoadingDetails, setIsLoadingDetails] = useState(false);
  const [decidingRowId, setDecidingRowId] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  const loadArchivedDatasets = useCallback(async () => {
    setIsLoadingArchive(true);
    try {
      const response = await apiFetch('/api/v1/evaluation-datasets/archive', { cache: 'no-store' });
      if (!response.ok) throw new Error('Archivierte Datasets konnten nicht geladen werden.');
      const payload = await response.json();
      setArchivedDatasets((payload.items ?? []) as DatasetEntry[]);
    } catch (cause) {
      setLocalError(cause instanceof Error ? cause.message : 'Archiv konnte nicht geladen werden.');
    } finally {
      setIsLoadingArchive(false);
    }
  }, []);

  const loadSelectedDataset = useCallback(async () => {
    if (!selectedDatasetPath) {
      setDetails(null);
      return;
    }
    setIsLoadingDetails(true);
    try {
      const response = await apiFetch('/api/v1/evaluation-datasets/' + encodeURI(selectedDatasetPath), { cache: 'no-store' });
      if (!response.ok) throw new Error('Dataset konnte nicht geladen werden.');
      setDetails((await response.json()) as DatasetDetail);
    } catch (cause) {
      setDetails(null);
      setLocalError(cause instanceof Error ? cause.message : 'Backend beim Laden des Datasets nicht erreichbar.');
    } finally {
      setIsLoadingDetails(false);
    }
  }, [selectedDatasetPath]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void loadSelectedDataset(); }, 0);
    return () => window.clearTimeout(timer);
  }, [loadSelectedDataset]);

  const datasetWordSources = (dataset: DatasetEntry) => {
    const explicitSources = dataset.source_files.map(sourceFilename);
    if (explicitSources.length > 0) return explicitSources;
    return dataset.source_documents
      .map((path) => markdownFiles.find((file) => path === file.path || path.endsWith('/' + file.path)))
      .map((file) => file?.original_filename || file?.filename || '')
      .filter(Boolean);
  };

  const decideQuestion = async (rowId: string, decision: 'included' | 'excluded') => {
    if (!details?.updated_at) return;
    setDecidingRowId(rowId);
    setLocalError(null);
    setMessage(null);
    try {
      const response = await apiFetch('/api/v1/evaluation-datasets/review-decision', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          dataset_path: details.path, row_id: rowId, decision,
          expected_updated_at: details.updated_at,
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        if (response.status === 409) await loadSelectedDataset();
        throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Entscheidung konnte nicht gespeichert werden.');
      }
      const saved = (await response.json()) as DatasetDetail;
      setDetails(saved);
      await onDatasetSaved(saved.path);
      setMessage(rowId + (decision === 'included'
        ? ' bleibt für die Evaluation zugelassen.'
        : ' wird bei der Evaluation ausgeschlossen. Die Entscheidung bleibt im JSONL dokumentiert.'));
    } catch (cause) {
      setLocalError(cause instanceof Error ? cause.message : 'Backend beim Speichern nicht erreichbar.');
    } finally {
      setDecidingRowId(null);
    }
  };

  const downloadDataset = async (dataset: DatasetEntry) => {
    setIsDatasetActionRunning(true);
    setLocalError(null);
    try {
      const response = await apiFetch('/api/v1/evaluation-datasets/' + encodeURI(dataset.path), { cache: 'no-store' });
      if (!response.ok) throw new Error('Dataset konnte nicht geladen werden.');
      const loaded = (await response.json()) as DatasetDetail;
      const jsonl = loaded.rows.map((row) => JSON.stringify(row)).join('\n') + '\n';
      const url = URL.createObjectURL(new Blob([jsonl], { type: 'application/x-ndjson' }));
      const link = document.createElement('a');
      link.href = url;
      link.download = loaded.filename;
      link.click();
      URL.revokeObjectURL(url);
    } catch (cause) {
      setLocalError(cause instanceof Error ? cause.message : 'Dataset konnte nicht heruntergeladen werden.');
    } finally {
      setIsDatasetActionRunning(false);
    }
  };

  const archiveDataset = async (dataset: DatasetEntry) => {
    if (!window.confirm('„' + dataset.filename + '“ archivieren? Es kann später wiederhergestellt werden.')) return;
    setIsDatasetActionRunning(true);
    setLocalError(null);
    try {
      const response = await apiFetch('/api/v1/evaluation-datasets/' + encodeURI(dataset.path), { method: 'DELETE' });
      if (!response.ok) throw new Error('Dataset konnte nicht archiviert werden.');
      const nextSelection = selectedDatasetPath === dataset.path ? '' : selectedDatasetPath;
      onSelectDataset(nextSelection);
      await onDatasetSaved(nextSelection);
      await loadArchivedDatasets();
      setMessage(dataset.filename + ' wurde archiviert.');
    } catch (cause) {
      setLocalError(cause instanceof Error ? cause.message : 'Dataset konnte nicht archiviert werden.');
    } finally {
      setIsDatasetActionRunning(false);
    }
  };

  const restoreDataset = async (dataset: DatasetEntry) => {
    setIsDatasetActionRunning(true);
    setLocalError(null);
    try {
      const response = await apiFetch('/api/v1/evaluation-datasets/archive/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: dataset.path }),
      });
      if (!response.ok) throw new Error('Dataset konnte nicht wiederhergestellt werden.');
      const restored = (await response.json()) as DatasetDetail;
      setShowArchivedDatasets(false);
      await loadArchivedDatasets();
      onSelectDataset(restored.path);
      await onDatasetSaved(restored.path);
      setMessage(restored.filename + ' wurde wiederhergestellt.');
    } catch (cause) {
      setLocalError(cause instanceof Error ? cause.message : 'Dataset konnte nicht wiederhergestellt werden.');
    } finally {
      setIsDatasetActionRunning(false);
    }
  };

  const visibleDatasets = showArchivedDatasets ? archivedDatasets : datasets;

  return (
    <div className="space-y-6">
      <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <h3 className="text-lg font-semibold text-slate-950">Dataset erstellen</h3>
        <Button
          type="button"
          onClick={() => setShowGenerator((previous) => !previous)}
          variant={showGenerator ? 'outline' : 'default'}
          className={'mt-4 ' + (showGenerator ? '' : 'bg-emerald-600 hover:bg-emerald-700')}
        >
          {showGenerator ? <X className="mr-2 h-4 w-4" /> : <Sparkles className="mr-2 h-4 w-4" />}
          {showGenerator ? 'Generierung schließen' : 'Dataset generieren'}
        </Button>
        {showGenerator && <DatasetGenerator
          files={markdownFiles}
          datasets={datasets}
          preferredPath={preferredMarkdownPath}
          onSaved={onDatasetSaved}
          onOpenDataset={async (path) => {
            onSelectDataset(path);
            setShowGenerator(false);
            await onDatasetSaved(path);
          }}
        />}
      </section>

      {!showGenerator && <>
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="text-lg font-semibold text-slate-950">{showArchivedDatasets ? 'Archivierte Datasets' : 'Bestehende Evaluationssets'}</h3>
              <p className="mt-1 text-sm text-slate-500">Fragen, Goldantworten und Quellen ansehen und Prüffragen entscheiden.</p>
            </div>
            <Button type="button" variant="outline" size="sm" disabled={isDatasetActionRunning} onClick={() => {
              const next = !showArchivedDatasets;
              setShowArchivedDatasets(next);
              if (next) void loadArchivedDatasets();
              else if (!datasets.some((dataset) => dataset.path === selectedDatasetPath)) onSelectDataset(datasets[0]?.path ?? '');
            }}>
              {showArchivedDatasets ? 'Aktive Datasets' : 'Archiv' + (archivedDatasets.length ? ' (' + archivedDatasets.length + ')' : '')}
            </Button>
          </div>
          <div className="mt-4 grid gap-2">
            {showArchivedDatasets && isLoadingArchive ? <p className="text-sm text-slate-500">Archiv wird geladen…</p>
              : visibleDatasets.length === 0 ? <p className="text-sm text-slate-500">{showArchivedDatasets ? 'Das Archiv ist leer.' : 'Noch keine JSONL-Datasets vorhanden.'}</p>
              : visibleDatasets.map((dataset) => (
                <div key={dataset.path} className={'flex min-w-0 items-center gap-2 rounded-xl border-2 p-2 ' + (dataset.path === selectedDatasetPath ? 'border-purple-500 bg-purple-50' : 'border-slate-200 bg-white')}>
                  <button type="button" disabled={isDatasetActionRunning} onClick={() => onSelectDataset(dataset.path)}
                    className="min-w-0 flex-1 break-words rounded-lg p-2 text-left hover:bg-white/70">
                    <p className="font-medium text-slate-900">{dataset.filename}</p>
                    <p className="mt-1 text-xs text-slate-500">{dataset.row_count} Fragen · {formatCreatedAt(dataset.created_at)}</p>
                    <p className="mt-1 text-xs text-slate-600">Original: {datasetWordSources(dataset).join(', ') || 'nicht zugeordnet'}</p>
                  </button>
                  <div className="flex shrink-0 items-center gap-1">
                    {showArchivedDatasets
                      ? <Button type="button" size="sm" variant="outline" disabled={isDatasetActionRunning} onClick={() => void restoreDataset(dataset)} aria-label={'Dataset ' + dataset.filename + ' wiederherstellen'} title="Wiederherstellen"><RotateCcw className="h-4 w-4" /></Button>
                      : <Button type="button" size="sm" variant="outline" disabled={isDatasetActionRunning} onClick={() => void archiveDataset(dataset)} aria-label={'Dataset ' + dataset.filename + ' archivieren'} title="Archivieren"><Archive className="h-4 w-4" /></Button>}
                    <Button type="button" size="sm" variant="outline" disabled={isDatasetActionRunning} onClick={() => void downloadDataset(dataset)} aria-label={'Dataset ' + dataset.filename + ' herunterladen'} title="Herunterladen"><Download className="h-4 w-4" /></Button>
                  </div>
                </div>
              ))}
          </div>
        </section>

        {localError && <p role="alert" className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{localError}</p>}
        {message && <p role="status" className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">{message}</p>}

        <section className="rounded-2xl border border-purple-200 bg-white p-5 shadow-sm">
          {isLoadingDetails ? <p className="text-sm text-slate-500">Dataset wird geladen…</p>
            : details ? <>
              <h3 className="text-lg font-semibold text-slate-950">{details.filename}</h3>
              <p className="mt-1 text-sm text-slate-500">{details.row_count} Fragen gespeichert · Original: {details.source_files.map(sourceFilename).join(', ') || 'nicht zugeordnet'}</p>
              <RagasValidationSummary rows={details.rows} />
              <div className="mt-4 max-h-[36rem] space-y-3 overflow-y-auto pr-1">
                {details.rows.map((row, index) => {
                  const rowId = String(row.id ?? 'Frage ' + (index + 1));
                  const needsDecision = row.generation_pipeline === 'ragas' && row.validation_status === 'review';
                  const decision = String(row.review_decision ?? 'pending');
                  return <article key={rowId} className={'rounded-xl border p-4 ' + (decision === 'excluded' ? 'border-slate-200 bg-slate-50' : 'border-purple-100 bg-purple-50/40')}>
                    <p className="text-xs font-semibold uppercase tracking-wide text-purple-700">{rowId}</p>
                    <p className="mt-1 font-medium text-slate-900">{String(row.question ?? '')}</p>
                    <p className="mt-3 text-xs font-semibold text-slate-600">Goldantwort</p>
                    <p className="mt-1 whitespace-pre-wrap text-sm text-slate-700">{String(row.gold_answer ?? '')}</p>
                    {Boolean(row.evidence_quote) && <p className="mt-3 whitespace-pre-wrap rounded-lg bg-white p-3 text-xs text-slate-600">{String(row.evidence_quote)}</p>}
                    <RagasValidationInfo row={row} />
                    {needsDecision && !showArchivedDatasets && <div className="mt-3 rounded-lg border border-amber-200 bg-white p-3 text-xs">
                      <p className="mb-2 text-slate-700">Für die Evaluation verwenden? Die Frage und deine Entscheidung bleiben im JSONL erhalten.</p>
                      <div className="flex flex-wrap gap-2">
                        <Button type="button" size="sm" variant={decision === 'included' ? 'default' : 'outline'}
                          disabled={decidingRowId !== null || !details.updated_at}
                          aria-pressed={decision === 'included'}
                          onClick={() => void decideQuestion(rowId, 'included')}>Behalten</Button>
                        <Button type="button" size="sm" variant={decision === 'excluded' ? 'default' : 'outline'}
                          disabled={decidingRowId !== null || !details.updated_at}
                          aria-pressed={decision === 'excluded'}
                          onClick={() => void decideQuestion(rowId, 'excluded')}>Ausschließen</Button>
                      </div>
                    </div>}
                    <div className="mt-3 grid gap-1 text-xs text-slate-500">
                      <p>Original: {sourceFilename(String(row.source_file ?? '')) || 'nicht zugeordnet'}</p>
                      {typeof row.source_page === 'number' && <p>Seite: {row.source_page}</p>}
                      {Boolean(row.evidence_anchor) && <p>Abschnitt: {String(row.evidence_anchor)}</p>}
                    </div>
                  </article>;
                })}
              </div>
            </> : <p className="text-sm text-slate-500">Wähle ein Dataset aus.</p>}
        </section>
      </>}
    </div>
  );
}
