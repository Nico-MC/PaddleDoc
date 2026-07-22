'use client';

import { useEffect, useState } from 'react';

import { Button } from '@/components/ui/button';
import { API_BASE_URL } from '@/lib/api-base';

type MarkdownFileEntry = {
  path: string;
  filename: string;
  folder: string;
  size_bytes: number;
  updated_at: string;
};

type EncourageDocument = {
  id: string;
  content: string;
  score: number;
  distance: number | null;
  meta_data: Record<string, unknown>;
};

type EncourageIngestResponse = {
  path: string;
  filename: string;
  document: EncourageDocument;
  pipeline: {
    pipeline_id: string;
    collection_name: string;
    document_count: number;
    top_k: number;
    rag_method: string;
    ready: boolean;
  };
  debug: {
    config: Record<string, unknown>;
    collection: Record<string, unknown>;
    document_dump: Record<string, unknown>;
  };
};

const API = API_BASE_URL;

export default function EncouragePage() {
  const [items, setItems] = useState<MarkdownFileEntry[]>([]);
  const [selectedPath, setSelectedPath] = useState<string>('');
  const [markdownPreview, setMarkdownPreview] = useState<string>('');
  const [ingested, setIngested] = useState<EncourageIngestResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isIngesting, setIsIngesting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const loadFiles = async () => {
      setIsLoading(true);
      setError(null);
      const response = await fetch(`${API}/api/v1/markdown-files`, { cache: 'no-store' });
      if (!response.ok) {
        setError('Failed to load markdown files.');
        setIsLoading(false);
        return;
      }
      const payload = await response.json();
      const nextItems = (payload.items ?? []) as MarkdownFileEntry[];
      setItems(nextItems);
      if (nextItems.length > 0) {
        setSelectedPath(nextItems[0].path);
      }
      setIsLoading(false);
    };

    void loadFiles();
  }, []);

  useEffect(() => {
    const loadPreview = async () => {
      if (!selectedPath) {
        setMarkdownPreview('');
        return;
      }
      setError(null);
      const response = await fetch(`${API}/api/v1/markdown-files/${selectedPath}`, {
        cache: 'no-store',
      });
      if (!response.ok) {
        setError('Failed to load markdown preview.');
        setMarkdownPreview('');
        return;
      }
      const text = await response.text();
      setMarkdownPreview(text);
      setIngested(null);
    };

    void loadPreview();
  }, [selectedPath]);

  const ingestSelectedFile = async () => {
    if (!selectedPath) {
      return;
    }
    setIsIngesting(true);
    setError(null);
    const response = await fetch(`${API}/api/v1/encourage/ingest`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: selectedPath }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const detail = typeof payload?.detail === 'string' ? payload.detail : 'Failed to ingest markdown file.';
      setError(detail);
      setIsIngesting(false);
      return;
    }
    const payload = (await response.json()) as EncourageIngestResponse;
    setIngested(payload);
    setIsIngesting(false);
  };

  return (
    <main className="min-h-screen bg-white px-4 py-6 text-slate-950 sm:px-6 lg:px-8">
      <div className="mx-auto w-full max-w-7xl space-y-4 rounded-3xl border border-slate-200 bg-white p-4 shadow-[0_24px_70px_rgba(15,23,42,0.08)] sm:p-6 lg:p-8">
        <div className="max-w-3xl">
          <h1 className="font-serif text-3xl font-semibold">Encourage</h1>
          <p className="mt-2 text-slate-600">
            Select a PaddleDoc markdown result, then hand it over directly to Encourage for ingestion.
          </p>
        </div>

        {error && <p className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</p>}

        <div className="grid gap-6 lg:grid-cols-[0.9fr_1.1fr]">
          <section className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h2 className="text-lg font-semibold">Markdown files</h2>
              <Button onClick={ingestSelectedFile} disabled={!selectedPath || isIngesting || isLoading}>
                {isIngesting ? 'Ingesting...' : 'Ingest MD file'}
              </Button>
            </div>
            {isLoading ? (
              <p className="text-sm text-slate-500">Loading markdown files...</p>
            ) : items.length === 0 ? (
              <p className="text-sm text-slate-500">No markdown files found in PaddleDoc results.</p>
            ) : (
              <div className="space-y-2">
                {items.map((item) => {
                  const isActive = item.path === selectedPath;
                  return (
                    <button
                      key={item.path}
                      type="button"
                      onClick={() => setSelectedPath(item.path)}
                      className={`w-full rounded-xl border px-3 py-3 text-left transition ${
                        isActive
                          ? 'border-emerald-300 bg-emerald-50 text-emerald-900'
                          : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300'
                      }`}
                    >
                      <p className="font-medium">{item.filename}</p>
                      <p className="mt-1 text-xs text-slate-500">{item.path}</p>
                    </button>
                  );
                })}
              </div>
            )}
          </section>

          <section className="space-y-4">
            <div className="rounded-2xl border border-slate-200 bg-white p-4">
              <h2 className="mb-2 text-lg font-semibold">Markdown preview</h2>
              <pre className="max-h-[26rem] overflow-auto whitespace-pre-wrap break-words rounded-xl border border-slate-200 bg-slate-50 p-4 text-sm text-emerald-800">
                {markdownPreview || 'Select a markdown file to preview its content.'}
              </pre>
            </div>

            <div className="rounded-2xl border border-slate-200 bg-white p-4">
              <h2 className="mb-2 text-lg font-semibold">Encourage pipeline</h2>
              {ingested ? (
                <div className="space-y-3 text-sm text-slate-700">
                  <p><span className="font-semibold text-slate-950">File:</span> {ingested.filename}</p>
                  <p><span className="font-semibold text-slate-950">Document ID:</span> {ingested.document.id}</p>
                  <p><span className="font-semibold text-slate-950">Pipeline ID:</span> {ingested.pipeline.pipeline_id}</p>
                  <p><span className="font-semibold text-slate-950">Collection:</span> {ingested.pipeline.collection_name}</p>
                  <p><span className="font-semibold text-slate-950">RAG method:</span> {ingested.pipeline.rag_method}</p>
                  <p><span className="font-semibold text-slate-950">Documents:</span> {ingested.pipeline.document_count}</p>
                  <p><span className="font-semibold text-slate-950">Top K:</span> {ingested.pipeline.top_k}</p>
                  <p><span className="font-semibold text-slate-950">Ready:</span> {ingested.pipeline.ready ? 'yes' : 'no'}</p>
                  <div>
                    <p className="font-semibold text-slate-950">Metadata</p>
                    <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-xl border border-slate-200 bg-slate-50 p-4 text-xs text-slate-700">
                      {JSON.stringify(ingested.document.meta_data, null, 2)}
                    </pre>
                  </div>
                  <div>
                    <p className="font-semibold text-slate-950">Content used by Encourage</p>
                    <pre className="mt-2 max-h-[24rem] overflow-auto whitespace-pre-wrap break-words rounded-xl border border-slate-200 bg-slate-50 p-4 text-sm text-emerald-800">
                      {ingested.document.content}
                    </pre>
                  </div>
                  <div>
                    <p className="font-semibold text-slate-950">Generated JSON payload</p>
                    <pre className="mt-2 max-h-[32rem] overflow-auto whitespace-pre-wrap break-words rounded-xl border border-slate-200 bg-slate-50 p-4 text-xs text-slate-700">
                      {JSON.stringify(
                        {
                          selected_path: ingested.path,
                          document: ingested.document,
                          pipeline: ingested.pipeline,
                          config: ingested.debug.config,
                          collection: ingested.debug.collection,
                          document_dump: ingested.debug.document_dump,
                        },
                        null,
                        2,
                      )}
                    </pre>
                  </div>
                </div>
              ) : (
                <p className="text-sm text-slate-500">
                  Click <span className="font-medium text-slate-700">Ingest MD file</span> to hand the selected markdown file to Encourage.
                </p>
              )}
            </div>
          </section>
        </div>
      </div>
    </main>
  );
}