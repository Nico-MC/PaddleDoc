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

type EvaluationDatasetEntry = {
  path: string;
  filename: string;
  row_count: number;
  source_documents: string[];
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
  source_markdown: {
    path: string;
    filename: string;
    document_count: number;
    chunk_preview: string[];
  };
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
  rag_run: {
    query: string;
    model_name: string;
    answer: string;
  } | null;
};

type EncourageRetrieveResponse = {
  pipeline_id: string;
  collection_name: string;
  query: string;
  top_k: number;
  results: EncourageDocument[];
};

type EncourageEvaluationResponse = {
  pipeline_id: string;
  collection_name: string;
  markdown_path: string;
  dataset_path: string;
  dataset_filename: string;
  question_count: number;
  evaluated_question_count: number;
  top_k: number;
  recall_k: number;
  mrr: number;
  recall_at_k: number;
  hit_rate_at_k: number;
  mlflow_run_id: string | null;
};

const API = API_BASE_URL;

export default function EncouragePage() {
  const [items, setItems] = useState<MarkdownFileEntry[]>([]);
  const [datasets, setDatasets] = useState<EvaluationDatasetEntry[]>([]);
  const [selectedPath, setSelectedPath] = useState<string>('');
  const [selectedDatasetPath, setSelectedDatasetPath] = useState<string>('');
  const [markdownPreview, setMarkdownPreview] = useState<string>('');
  const [ingested, setIngested] = useState<EncourageIngestResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingDatasets, setIsLoadingDatasets] = useState(true);
  const [isIngesting, setIsIngesting] = useState(false);
  const [query, setQuery] = useState('Worum geht es in diesem Dokument?');
  const [runGeneration, setRunGeneration] = useState(true);
  const [isRetrieving, setIsRetrieving] = useState(false);
  const [retrieval, setRetrieval] = useState<EncourageRetrieveResponse | null>(null);
  const [isEvaluating, setIsEvaluating] = useState(false);
  const [evaluation, setEvaluation] = useState<EncourageEvaluationResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const loadFiles = async () => {
      setIsLoading(true);
      setError(null);
      try {
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
      } catch {
        setError('Failed to reach the backend while loading markdown files.');
      } finally {
        setIsLoading(false);
      }
    };

    void loadFiles();
  }, []);

  useEffect(() => {
    const loadDatasets = async () => {
      setIsLoadingDatasets(true);
      try {
        const response = await fetch(`${API}/api/v1/evaluation-datasets`, { cache: 'no-store' });
        if (!response.ok) {
          setError('Failed to load evaluation datasets.');
          setIsLoadingDatasets(false);
          return;
        }
        const payload = await response.json();
        const nextDatasets = (payload.items ?? []) as EvaluationDatasetEntry[];
        setDatasets(nextDatasets);
        if (nextDatasets.length > 0) {
          setSelectedDatasetPath(nextDatasets[0].path);
        }
      } catch {
        setError('Failed to reach the backend while loading evaluation datasets.');
      } finally {
        setIsLoadingDatasets(false);
      }
    };

    void loadDatasets();
  }, []);

  useEffect(() => {
    const loadPreview = async () => {
      if (!selectedPath) {
        setMarkdownPreview('');
        return;
      }
      setError(null);
      try {
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
        setRetrieval(null);
        setEvaluation(null);
      } catch {
        setError('Failed to reach the backend while loading the markdown preview.');
        setMarkdownPreview('');
        setRetrieval(null);
        setEvaluation(null);
      }
    };

    void loadPreview();
  }, [selectedPath]);

  const ingestSelectedFile = async () => {
    if (!selectedPath) {
      return;
    }
    setIsIngesting(true);
    setError(null);
    try {
      const response = await fetch(`${API}/api/v1/encourage/ingest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: selectedPath, query, run_generation: runGeneration }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const detail = typeof payload?.detail === 'string' ? payload.detail : 'Failed to ingest markdown file.';
        setError(detail);
        return;
      }
      const payload = (await response.json()) as EncourageIngestResponse;
      setIngested(payload);
      setRetrieval(null);
      setEvaluation(null);
    } catch {
      setError('Failed to reach the backend while ingesting the markdown file.');
    } finally {
      setIsIngesting(false);
    }
  };

  const retrieveFromPipeline = async () => {
    if (!ingested?.pipeline.pipeline_id || !query.trim()) {
      return;
    }
    setIsRetrieving(true);
    setError(null);
    try {
      const response = await fetch(`${API}/api/v1/encourage/retrieve`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pipeline_id: ingested.pipeline.pipeline_id,
          query,
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const detail = typeof payload?.detail === 'string' ? payload.detail : 'Failed to retrieve from pipeline.';
        setError(detail);
        return;
      }
      const payload = (await response.json()) as EncourageRetrieveResponse;
      setRetrieval(payload);
    } catch {
      setError('Failed to reach the backend while retrieving from the pipeline.');
    } finally {
      setIsRetrieving(false);
    }
  };

  const runEvaluation = async () => {
    if (!ingested?.pipeline.pipeline_id || !selectedDatasetPath) {
      return;
    }
    setIsEvaluating(true);
    setError(null);
    try {
      const response = await fetch(`${API}/api/v1/encourage/evaluate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pipeline_id: ingested.pipeline.pipeline_id,
          dataset_path: selectedDatasetPath,
          recall_k: 3,
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const detail = typeof payload?.detail === 'string' ? payload.detail : 'Failed to run evaluation.';
        setError(detail);
        return;
      }
      const payload = (await response.json()) as EncourageEvaluationResponse;
      setEvaluation(payload);
    } catch {
      setError('Failed to reach the backend while running evaluation.');
    } finally {
      setIsEvaluating(false);
    }
  };

  return (
    <main className="min-h-screen bg-white px-4 py-6 text-slate-950 sm:px-6 lg:px-8">
      <div className="mx-auto w-full max-w-7xl space-y-4 rounded-3xl border border-slate-200 bg-white p-4 shadow-[0_24px_70px_rgba(15,23,42,0.08)] sm:p-6 lg:p-8">
        <div className="max-w-3xl">
          <h1 className="font-serif text-3xl font-semibold">Encourage</h1>
          <p className="mt-2 text-slate-600">
            Select a PaddleDoc markdown result, then run the full Encourage flow in one step
            (indexing, retrieval, generation).
          </p>
        </div>

        {error && <p className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{error}</p>}

        <div className="grid gap-6 lg:grid-cols-[0.9fr_1.1fr]">
          <section className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <h2 className="text-lg font-semibold">Markdown files</h2>
              <div className="flex items-center gap-2">
                <span
                  className={`rounded-full border px-2 py-1 text-xs font-semibold ${
                    runGeneration
                      ? 'border-emerald-300 bg-emerald-100 text-emerald-800'
                      : 'border-slate-300 bg-slate-100 text-slate-700'
                  }`}
                >
                  Generation: {runGeneration ? 'ON' : 'OFF'}
                </span>
                <Button onClick={ingestSelectedFile} disabled={!selectedPath || isIngesting || isLoading}>
                  {isIngesting ? 'Ingesting...' : 'Ingest MD file'}
                </Button>
              </div>
            </div>
            <div className="mb-4 rounded-xl border border-slate-200 bg-white p-3">
              <div className="mb-2 flex items-center justify-between gap-3">
                <h3 className="text-sm font-semibold text-slate-950">Evaluation datasets</h3>
                <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-1 text-xs text-slate-500">
                  {datasets.length} available
                </span>
              </div>
              {isLoadingDatasets ? (
                <p className="text-sm text-slate-500">Loading evaluation datasets...</p>
              ) : datasets.length === 0 ? (
                <p className="text-sm text-slate-500">No evaluation datasets found.</p>
              ) : (
                <div className="space-y-2">
                  {datasets.map((item) => {
                    const isActive = item.path === selectedDatasetPath;
                    return (
                      <button
                        key={item.path}
                        type="button"
                        onClick={() => setSelectedDatasetPath(item.path)}
                        className={`w-full rounded-xl border px-3 py-3 text-left transition ${
                          isActive
                            ? 'border-emerald-300 bg-emerald-50 text-emerald-900'
                            : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300'
                        }`}
                      >
                        <p className="font-medium">{item.filename}</p>
                        <p className="mt-1 text-xs text-slate-500">
                          {item.path} · {item.row_count} row(s)
                        </p>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
            <label className="mb-3 flex items-center gap-2 rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700">
              <input
                type="checkbox"
                checked={runGeneration}
                onChange={(event: { target: { checked: boolean } }) => setRunGeneration(event.target.checked)}
                className="h-4 w-4 rounded border-slate-300 text-emerald-600 focus:ring-emerald-300"
              />
              Run generation on ingest (uses configured OpenAI endpoint)
            </label>
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
                <div className="space-y-4 text-sm text-slate-700">
                  <div className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4">
                    <p className="font-semibold text-emerald-900">Ingested Markdown</p>
                    <p className="mt-2 text-xs text-emerald-700">
                      <span className="font-medium">{ingested.source_markdown.filename}</span>
                    </p>
                    <p className="mt-1 text-xs text-emerald-700">
                      Path: <span className="font-medium text-emerald-900">{ingested.source_markdown.path}</span>
                    </p>
                    <p className="mt-1 text-xs text-emerald-700">
                      Chunks created: <span className="font-medium text-emerald-900">{ingested.source_markdown.document_count}</span>
                    </p>
                  </div>

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
                          source_markdown: ingested.source_markdown,
                          document: ingested.document,
                          pipeline: ingested.pipeline,
                          config: ingested.debug.config,
                          collection: ingested.debug.collection,
                          document_dump: ingested.debug.document_dump,
                          rag_run: ingested.rag_run,
                        },
                        null,
                        2,
                      )}
                    </pre>
                  </div>
                  {ingested.rag_run ? (
                    <div className="rounded-2xl border border-emerald-200 bg-emerald-50 p-4">
                      <p className="font-semibold text-emerald-900">RAG answer (run on ingest)</p>
                      <p className="mt-1 text-xs text-emerald-700">
                        Model <span className="font-medium">{ingested.rag_run.model_name}</span> answered query:{' '}
                        <span className="font-medium">{ingested.rag_run.query}</span>
                      </p>
                      <pre className="mt-3 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-xl border border-emerald-200 bg-white p-4 text-sm text-emerald-900">
                        {ingested.rag_run.answer || 'No answer returned by the model.'}
                      </pre>
                    </div>
                  ) : (
                    <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
                      Generation was skipped. Either the toggle was disabled at ingest time or OpenAI settings are missing.
                    </div>
                  )}
                  <div className="rounded-2xl border border-slate-200 bg-slate-50 p-4">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <p className="font-semibold text-slate-950">Retrieval probe</p>
                        <p className="text-xs text-slate-500">
                          Inspect only what the vector store returned for your query (no LLM/OpenAI call).
                        </p>
                      </div>
                      <Button
                        onClick={retrieveFromPipeline}
                        disabled={isRetrieving || !query.trim() || !ingested?.pipeline.pipeline_id}
                      >
                        {isRetrieving ? 'Retrieving...' : 'Retrieve'}
                      </Button>
                    </div>
                    <textarea
                      value={query}
                      onChange={(event: { target: { value: string } }) => setQuery(event.target.value)}
                      rows={3}
                      className="mt-3 w-full rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 outline-none transition focus:border-emerald-300"
                      placeholder="Ask a question about the selected markdown file"
                    />
                    {retrieval ? (
                      <div className="mt-4 space-y-3">
                        <p className="text-xs text-slate-500">
                          Collection <span className="font-medium text-slate-700">{retrieval.collection_name}</span> returned {retrieval.results.length} result(s).
                        </p>
                        {retrieval.results.map((result) => (
                          <div key={result.id} className="rounded-xl border border-slate-200 bg-white p-4">
                            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
                              <span>ID: {result.id}</span>
                              <span>Score: {result.score.toFixed(4)}</span>
                              <span>Distance: {result.distance ?? 'n/a'}</span>
                            </div>
                            <pre className="mt-3 max-h-48 overflow-auto whitespace-pre-wrap break-words text-sm text-emerald-800">
                              {result.content}
                            </pre>
                          </div>
                        ))}
                      </div>
                    ) : (
                      <p className="mt-3 text-sm text-slate-500">Run a query to inspect what the vector store would return for this document.</p>
                    )}
                  </div>

                  <div className="rounded-2xl border border-slate-200 bg-white p-4">
                    <div className="flex items-center justify-between gap-3">
                      <div>
                        <p className="font-semibold text-slate-950">Retrieval evaluation</p>
                        <p className="text-xs text-slate-500">
                          Run the selected markdown file against the selected dataset and log the metrics to MLflow.
                        </p>
                      </div>
                      <Button
                        onClick={runEvaluation}
                        disabled={isEvaluating || !selectedDatasetPath || !ingested?.pipeline.pipeline_id}
                      >
                        {isEvaluating ? 'Evaluating...' : 'Run evaluation'}
                      </Button>
                    </div>
                    <p className="mt-3 text-sm text-slate-600">
                      Dataset: <span className="font-medium text-slate-900">{selectedDatasetPath || 'none selected'}</span>
                    </p>
                    {evaluation ? (
                      <div className="mt-4 space-y-3 text-sm text-slate-700">
                        <p>
                          <span className="font-semibold text-slate-950">MLflow run ID:</span>{' '}
                          {evaluation.mlflow_run_id || 'not available'}
                        </p>
                        <p>
                          <span className="font-semibold text-slate-950">Evaluated questions:</span>{' '}
                          {evaluation.evaluated_question_count} / {evaluation.question_count}
                        </p>
                        <p>
                          <span className="font-semibold text-slate-950">MRR:</span> {evaluation.mrr.toFixed(4)}
                        </p>
                        <p>
                          <span className="font-semibold text-slate-950">Recall@{evaluation.recall_k}:</span>{' '}
                          {evaluation.recall_at_k.toFixed(4)}
                        </p>
                        <p>
                          <span className="font-semibold text-slate-950">HitRate@{evaluation.recall_k}:</span>{' '}
                          {evaluation.hit_rate_at_k.toFixed(4)}
                        </p>
                      </div>
                    ) : (
                      <p className="mt-3 text-sm text-slate-500">
                        Ingest a markdown file, select a dataset, then run evaluation to send metrics to MLflow.
                      </p>
                    )}
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
