'use client';

import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { apiFetch } from '@/lib/api';

type DatasetEntry = {
  path: string;
  filename: string;
  row_count: number;
  source_documents: string[];
};

type DatasetDetail = DatasetEntry & {
  rows: Record<string, unknown>[];
};

type MarkdownEntry = {
  path: string;
  filename: string;
  original_filename: string;
  original_extension: string;
  workspace_folder: string;
};

type WordSource = {
  path: string;
  filename: string;
  extension: string;
  size_bytes: number;
  updated_at: string;
};

type DatasetRow = {
  id: string;
  question: string;
  gold_answer: string;
  evidence_quote: string;
  evidence_anchor: string;
  source_document: string;
  source_file: string;
  notes: string;
};

type Props = {
  datasets: DatasetEntry[];
  markdownFiles: MarkdownEntry[];
  selectedDatasetPath: string;
  onSelectDataset: (path: string) => void;
  onDatasetSaved: (path: string) => Promise<void> | void;
};

const emptyRow = (
  index: number,
  markdownPath = '',
  sourceFile = '',
): DatasetRow => ({
  id: `question-${String(index + 1).padStart(3, '0')}`,
  question: '',
  gold_answer: '',
  evidence_quote: '',
  evidence_anchor: '',
  source_document: markdownPath,
  source_file: sourceFile,
  notes: '',
});

const textValue = (row: Record<string, unknown>, key: keyof DatasetRow) => {
  const value = row[key];
  return value === null || value === undefined ? '' : String(value);
};

export function EncourageDatasetWorkbench({
  datasets,
  markdownFiles,
  selectedDatasetPath,
  onSelectDataset,
  onDatasetSaved,
}: Props) {
  const [wordSources, setWordSources] = useState<WordSource[]>([]);
  const [details, setDetails] = useState<DatasetDetail | null>(null);
  const [isLoadingDetails, setIsLoadingDetails] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [filename, setFilename] = useState('retrieval_dataset.jsonl');
  const [rows, setRows] = useState<DatasetRow[]>([
    emptyRow(0, markdownFiles[0]?.path, wordSources[0]?.path),
  ]);
  const [isSaving, setIsSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  useEffect(() => {
    const loadWordSources = async () => {
      try {
        const response = await apiFetch('/api/v1/evaluation-source-documents', {
          cache: 'no-store',
        });
        if (!response.ok) return;
        const payload = await response.json();
        setWordSources((payload.items ?? []) as WordSource[]);
      } catch {
        // The workbench remains usable with converted markdown files only.
      }
    };
    void loadWordSources();
  }, []);

  useEffect(() => {
    if (!selectedDatasetPath) {
      setDetails(null);
      return;
    }

    const loadDetails = async () => {
      setIsLoadingDetails(true);
      setLocalError(null);
      try {
        const response = await apiFetch(
          `/api/v1/evaluation-datasets/${encodeURI(selectedDatasetPath)}`,
          { cache: 'no-store' },
        );
        if (!response.ok) {
          setLocalError('Dataset konnte nicht geladen werden.');
          return;
        }
        setDetails((await response.json()) as DatasetDetail);
      } catch {
        setLocalError('Backend beim Laden des Datasets nicht erreichbar.');
      } finally {
        setIsLoadingDetails(false);
      }
    };
    void loadDetails();
  }, [selectedDatasetPath]);

  const startNewDataset = () => {
    setFilename('retrieval_dataset.jsonl');
    setRows([emptyRow(0, markdownFiles[0]?.path, wordSources[0]?.path)]);
    setMessage(null);
    setLocalError(null);
    setIsEditing(true);
  };

  const startEditingDataset = () => {
    if (!details) return;
    setFilename(details.filename);
    setRows(
      details.rows.map((row, index) => ({
        id: textValue(row, 'id') || `question-${String(index + 1).padStart(3, '0')}`,
        question: textValue(row, 'question'),
        gold_answer: textValue(row, 'gold_answer'),
        evidence_quote: textValue(row, 'evidence_quote'),
        evidence_anchor: textValue(row, 'evidence_anchor'),
        source_document: textValue(row, 'source_document'),
        source_file: textValue(row, 'source_file'),
        notes: textValue(row, 'notes'),
      })),
    );
    setMessage(null);
    setLocalError(null);
    setIsEditing(true);
  };

  const updateRow = (index: number, field: keyof DatasetRow, value: string) => {
    setRows((current) =>
      current.map((row, rowIndex) => (rowIndex === index ? { ...row, [field]: value } : row)),
    );
  };

  const addRow = () => {
    setRows((current) => [
      ...current,
      emptyRow(
        current.length,
        current.at(-1)?.source_document || markdownFiles[0]?.path,
        current.at(-1)?.source_file || wordSources[0]?.path,
      ),
    ]);
  };

  const removeRow = (index: number) => {
    setRows((current) => current.filter((_, rowIndex) => rowIndex !== index));
  };

  const saveDataset = async () => {
    setIsSaving(true);
    setLocalError(null);
    setMessage(null);
    try {
      const response = await apiFetch('/api/v1/evaluation-datasets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ filename, rows }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        setLocalError(
          typeof payload?.detail === 'string' ? payload.detail : 'Dataset konnte nicht gespeichert werden.',
        );
        return;
      }
      const saved = (await response.json()) as DatasetDetail;
      setDetails(saved);
      onSelectDataset(saved.path);
      await onDatasetSaved(saved.path);
      setIsEditing(false);
      setMessage(`${saved.filename} wurde mit ${saved.row_count} Fragen gespeichert.`);
    } catch {
      setLocalError('Backend beim Speichern des Datasets nicht erreichbar.');
    } finally {
      setIsSaving(false);
    }
  };

  const downloadDataset = () => {
    if (!details) return;
    const jsonl = `${details.rows.map((row) => JSON.stringify(row)).join('\n')}\n`;
    const url = URL.createObjectURL(new Blob([jsonl], { type: 'application/x-ndjson' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = details.filename;
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="space-y-6">
      <div className="grid gap-4 lg:grid-cols-[1.1fr_0.9fr]">
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="text-lg font-semibold text-slate-950">Evaluation Data Sets</h3>
              <p className="mt-1 text-sm text-slate-500">
                JSONL-Fragen, Goldantworten und Evidenz für eure Dokumente verwalten.
              </p>
            </div>
            <Button onClick={startNewDataset} className="bg-emerald-600 hover:bg-emerald-700">
              Neues Dataset
            </Button>
          </div>

          <div className="mt-4 grid gap-2">
            {datasets.length === 0 ? (
              <p className="rounded-xl border border-dashed border-slate-300 p-4 text-sm text-slate-500">
                Noch keine JSONL-Datasets vorhanden.
              </p>
            ) : (
              datasets.map((dataset) => (
                <button
                  key={dataset.path}
                  type="button"
                  onClick={() => onSelectDataset(dataset.path)}
                  className={`rounded-xl border-2 p-3 text-left transition ${
                    dataset.path === selectedDatasetPath
                      ? 'border-purple-500 bg-purple-50'
                      : 'border-slate-200 hover:border-slate-300'
                  }`}
                >
                  <p className="font-medium text-slate-900">{dataset.filename}</p>
                  <p className="mt-1 text-xs text-slate-500">
                    {dataset.row_count} Fragen · {dataset.source_documents.length} Markdown-Quelle(n)
                  </p>
                </button>
              ))
            )}
          </div>
        </section>

        <section className="rounded-2xl border border-slate-200 bg-slate-50 p-5">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-700">Word-Quellen</h3>
          <p className="mt-1 text-xs text-slate-500">
            Dateien aus <code>.docs</code>. Die Evaluation verwendet zusätzlich die konvertierte Markdown-Datei.
          </p>
          <div className="mt-3 max-h-64 space-y-2 overflow-y-auto">
            {wordSources.length === 0 ? (
              <p className="text-sm text-slate-500">Keine Word-Dateien gefunden oder .docs ist nicht gemountet.</p>
            ) : (
              wordSources.map((source) => (
                <div key={source.path} className="rounded-lg border border-slate-200 bg-white px-3 py-2">
                  <p className="text-sm font-medium text-slate-800">{source.filename}</p>
                  <p className="break-all text-xs text-slate-500">{source.path}</p>
                </div>
              ))
            )}
          </div>
        </section>
      </div>

      {localError && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
          {localError}
        </div>
      )}
      {message && (
        <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">
          {message}
        </div>
      )}

      {isEditing ? (
        <section className="rounded-2xl border border-emerald-200 bg-white p-5 shadow-sm">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <label className="min-w-64 flex-1 text-sm font-medium text-slate-700">
              Dateiname
              <input
                value={filename}
                onChange={(event) => setFilename(event.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-200 px-3 py-2 text-sm"
                placeholder="mein_dataset.jsonl"
              />
            </label>
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => setIsEditing(false)}>
                Abbrechen
              </Button>
              <Button onClick={saveDataset} disabled={isSaving || rows.length === 0}>
                {isSaving ? 'Speichert…' : 'Dataset speichern'}
              </Button>
            </div>
          </div>

          <div className="mt-5 space-y-4">
            {rows.map((row, index) => (
              <div key={index} className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                <div className="flex items-center justify-between gap-3">
                  <p className="text-sm font-semibold text-slate-900">Frage {index + 1}</p>
                  <button
                    type="button"
                    onClick={() => removeRow(index)}
                    disabled={rows.length === 1}
                    className="text-xs font-medium text-rose-600 disabled:text-slate-300"
                  >
                    Entfernen
                  </button>
                </div>
                <div className="mt-3 grid gap-3 md:grid-cols-2">
                  <label className="text-xs font-medium text-slate-600">
                    ID
                    <input value={row.id} onChange={(event) => updateRow(index, 'id', event.target.value)} className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm" />
                  </label>
                  <label className="text-xs font-medium text-slate-600">
                    Word-Quelldatei
                    <select value={row.source_file} onChange={(event) => updateRow(index, 'source_file', event.target.value)} className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm">
                      <option value="">Keine Zuordnung</option>
                      {wordSources.map((source) => <option key={source.path} value={source.path}>{source.filename}</option>)}
                    </select>
                  </label>
                  <label className="text-xs font-medium text-slate-600 md:col-span-2">
                    Indexierte Markdown-Datei
                    <select value={row.source_document} onChange={(event) => updateRow(index, 'source_document', event.target.value)} className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm">
                      <option value="">Markdown auswählen</option>
                      {markdownFiles.map((file) => (
                        <option key={file.path} value={file.path}>
                          {file.original_filename || file.filename} → {file.filename} ({file.workspace_folder || 'inbox'})
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="text-xs font-medium text-slate-600 md:col-span-2">
                    Frage
                    <textarea value={row.question} onChange={(event) => updateRow(index, 'question', event.target.value)} rows={2} className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm" />
                  </label>
                  <label className="text-xs font-medium text-slate-600 md:col-span-2">
                    Goldantwort
                    <textarea value={row.gold_answer} onChange={(event) => updateRow(index, 'gold_answer', event.target.value)} rows={3} className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm" />
                  </label>
                  <label className="text-xs font-medium text-slate-600 md:col-span-2">
                    Evidenz-Zitat
                    <textarea value={row.evidence_quote} onChange={(event) => updateRow(index, 'evidence_quote', event.target.value)} rows={3} className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm" />
                  </label>
                  <label className="text-xs font-medium text-slate-600">
                    Evidenz-Anker
                    <input value={row.evidence_anchor} onChange={(event) => updateRow(index, 'evidence_anchor', event.target.value)} className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm" />
                  </label>
                  <label className="text-xs font-medium text-slate-600">
                    Notizen
                    <input value={row.notes} onChange={(event) => updateRow(index, 'notes', event.target.value)} className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm" />
                  </label>
                </div>
              </div>
            ))}
          </div>
          <Button variant="outline" onClick={addRow} className="mt-4">
            Frage hinzufügen
          </Button>
        </section>
      ) : (
        <section className="rounded-2xl border border-purple-200 bg-white p-5 shadow-sm">
          {isLoadingDetails ? (
            <p className="text-sm text-slate-500">Dataset wird geladen…</p>
          ) : details ? (
            <>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 className="text-lg font-semibold text-slate-950">{details.filename}</h3>
                  <p className="mt-1 text-sm text-slate-500">{details.row_count} Fragen</p>
                </div>
                <div className="flex gap-2">
                  <Button variant="outline" onClick={downloadDataset}>JSONL herunterladen</Button>
                  <Button onClick={startEditingDataset}>Bearbeiten</Button>
                </div>
              </div>
              <div className="mt-4 max-h-[36rem] space-y-3 overflow-y-auto pr-1">
                {details.rows.map((row, index) => (
                  <article key={String(row.id ?? index)} className="rounded-xl border border-purple-100 bg-purple-50/40 p-4">
                    <p className="text-xs font-semibold uppercase tracking-wide text-purple-700">{String(row.id ?? `Frage ${index + 1}`)}</p>
                    <p className="mt-1 font-medium text-slate-900">{String(row.question ?? '')}</p>
                    <p className="mt-3 text-xs font-semibold text-slate-600">Goldantwort</p>
                    <p className="mt-1 whitespace-pre-wrap text-sm text-slate-700">{String(row.gold_answer ?? '')}</p>
                    {Boolean(row.evidence_quote) && <p className="mt-3 rounded-lg bg-white p-3 text-xs text-slate-600">{String(row.evidence_quote)}</p>}
                    <div className="mt-3 grid gap-1 text-xs text-slate-500">
                      <p className="break-all">Markdown: {String(row.source_document ?? 'n/a')}</p>
                      <p className="break-all">Word: {String(row.source_file ?? 'n/a')}</p>
                    </div>
                  </article>
                ))}
              </div>
            </>
          ) : (
            <p className="text-sm text-slate-500">Wähle ein Dataset aus oder lege ein neues an.</p>
          )}
        </section>
      )}
    </div>
  );
}
