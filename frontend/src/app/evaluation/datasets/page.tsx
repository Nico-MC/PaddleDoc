'use client';

import { useEffect, useState } from 'react';
import { Database, RefreshCw } from 'lucide-react';
import { EncourageDatasetWorkbench } from '@/components/encourage-dataset-workbench';
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
};

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

export default function EvaluationDatasetsPage() {
  const [datasets, setDatasets] = useState<DatasetEntry[]>([]);
  const [markdownFiles, setMarkdownFiles] = useState<MarkdownEntry[]>([]);
  const [selectedDatasetPath, setSelectedDatasetPath] = useState('');
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshLists = async () => {
    setIsRefreshing(true);
    setError(null);
    try {
      const [markdownResponse, datasetResponse] = await Promise.all([
        apiFetch('/api/v1/markdown-files', { cache: 'no-store' }),
        apiFetch('/api/v1/evaluation-datasets', { cache: 'no-store' }),
      ]);
      if (!markdownResponse.ok || !datasetResponse.ok) {
        throw new Error('Datasets oder Markdown-Dateien konnten nicht geladen werden.');
      }
      const [markdownPayload, datasetPayload] = await Promise.all([
        markdownResponse.json(),
        datasetResponse.json(),
      ]);
      const nextDatasets = (datasetPayload.items ?? []) as DatasetEntry[];
      setMarkdownFiles((markdownPayload.items ?? []) as MarkdownEntry[]);
      setDatasets(nextDatasets);
      setSelectedDatasetPath((current) => (
        nextDatasets.some((dataset) => dataset.path === current)
          ? current
          : nextDatasets[0]?.path ?? ''
      ));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Daten konnten nicht geladen werden.');
    } finally {
      setIsRefreshing(false);
    }
  };

  useEffect(() => {
    let active = true;
    void Promise.all([
      apiFetch('/api/v1/markdown-files', { cache: 'no-store' }),
      apiFetch('/api/v1/evaluation-datasets', { cache: 'no-store' }),
    ])
      .then(async ([markdownResponse, datasetResponse]) => {
        if (!markdownResponse.ok || !datasetResponse.ok) {
          throw new Error('Datasets oder Markdown-Dateien konnten nicht geladen werden.');
        }
        const [markdownPayload, datasetPayload] = await Promise.all([
          markdownResponse.json(),
          datasetResponse.json(),
        ]);
        if (!active) return;
        const nextDatasets = (datasetPayload.items ?? []) as DatasetEntry[];
        setMarkdownFiles((markdownPayload.items ?? []) as MarkdownEntry[]);
        setDatasets(nextDatasets);
        setSelectedDatasetPath(nextDatasets[0]?.path ?? '');
      })
      .catch((cause: unknown) => {
        if (active) setError(cause instanceof Error ? cause.message : 'Daten konnten nicht geladen werden.');
      })
      .finally(() => {
        if (active) setIsLoading(false);
      });
    return () => { active = false; };
  }, []);

  const reloadDatasets = async (preferredPath: string) => {
    const response = await apiFetch('/api/v1/evaluation-datasets', { cache: 'no-store' });
    if (!response.ok) throw new Error('Datasets konnten nicht aktualisiert werden.');
    const payload = await response.json();
    const nextDatasets = (payload.items ?? []) as DatasetEntry[];
    setDatasets(nextDatasets);
    setSelectedDatasetPath((current) => {
      if (preferredPath && nextDatasets.some((dataset) => dataset.path === preferredPath)) return preferredPath;
      if (nextDatasets.some((dataset) => dataset.path === current)) return current;
      return nextDatasets[0]?.path ?? '';
    });
  };

  const handleDatasetSaved = async (preferredPath: string) => {
    try {
      await reloadDatasets(preferredPath);
      setError(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Datasets konnten nicht aktualisiert werden.');
    }
  };

  return (
    <main className="min-h-screen px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
      <div className="mx-auto w-full max-w-7xl">
        <header className="mb-6 flex flex-wrap items-start justify-between gap-3">
          <div className="flex items-start gap-3">
            <span className="mt-1 flex h-10 w-10 items-center justify-center rounded-xl bg-emerald-50 text-emerald-700">
              <Database className="h-5 w-5" />
            </span>
            <div>
              <h1 className="text-2xl font-semibold">Datasets</h1>
              <p className="mt-1 text-sm text-slate-600">Evaluationsdaten vorbereiten, generieren und verwalten.</p>
            </div>
          </div>
          <button type="button" onClick={() => void refreshLists()} disabled={isLoading || isRefreshing} className="inline-flex h-9 items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50" title="Markdown- und Dataset-Liste aktualisieren">
            <RefreshCw className={`h-4 w-4${isRefreshing ? ' animate-spin' : ''}`} />
            Aktualisieren
          </button>
        </header>
        {error && <div role="alert" className="mb-4 rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">{error}</div>}
        {isLoading ? (
          <p className="text-sm text-slate-500">Datasets werden geladen…</p>
        ) : (
          <EncourageDatasetWorkbench
            key={markdownFiles.map((file) => file.path).join('|')}
            datasets={datasets}
            markdownFiles={markdownFiles}
            preferredMarkdownPath=""
            selectedDatasetPath={selectedDatasetPath}
            onSelectDataset={setSelectedDatasetPath}
            onDatasetSaved={handleDatasetSaved}
          />
        )}
      </div>
    </main>
  );
}