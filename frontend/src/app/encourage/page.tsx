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

type EvaluationDatasetDetail = EvaluationDatasetEntry & {
  rows: Record<string, unknown>[];
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
  per_question_results: Array<{
    id: string;
    question: string;
    retrieved_document_ids: string[];
    reference_document_ids: string[];
    first_hit_rank: number | null;
    has_hit: boolean;
  }>;
};

const API = API_BASE_URL;

export default function EncouragePage() {
  const [items, setItems] = useState<MarkdownFileEntry[]>([]);
  const [datasets, setDatasets] = useState<EvaluationDatasetEntry[]>([]);
  const [selectedPath, setSelectedPath] = useState<string>('');
  const [selectedDatasetPath, setSelectedDatasetPath] = useState<string>('');
  const [ingested, setIngested] = useState<EncourageIngestResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingDatasets, setIsLoadingDatasets] = useState(true);
  const [isIngesting, setIsIngesting] = useState(false);
  const [query, setQuery] = useState('');
  const [runGeneration, setRunGeneration] = useState(false);
  const [isRetrieving, setIsRetrieving] = useState(false);
  const [retrieval, setRetrieval] = useState<EncourageRetrieveResponse | null>(null);
  const [isEvaluating, setIsEvaluating] = useState(false);
  const [evaluation, setEvaluation] = useState<EncourageEvaluationResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showChunks, setShowChunks] = useState(false);
  const [showDatasetDetails, setShowDatasetDetails] = useState(false);
  const [isLoadingDatasetDetails, setIsLoadingDatasetDetails] = useState(false);
  const [selectedDatasetDetails, setSelectedDatasetDetails] = useState<EvaluationDatasetDetail | null>(null);

  const formatNumber = (value: number | null) => {
    if (value === null || Number.isNaN(value)) {
      return 'n/a';
    }
    return value.toFixed(4);
  };

  const formatDatasetValue = (value: unknown) => {
    if (value === null || value === undefined) {
      return 'n/a';
    }
    if (typeof value === 'string') {
      return value;
    }
    if (typeof value === 'number' || typeof value === 'boolean') {
      return String(value);
    }
    return JSON.stringify(value);
  };

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
    if (!showDatasetDetails || !selectedDatasetPath) {
      return;
    }

    const loadDatasetDetails = async () => {
      setIsLoadingDatasetDetails(true);
      setError(null);
      try {
        const response = await fetch(
          `${API}/api/v1/evaluation-datasets/${encodeURI(selectedDatasetPath)}`,
          { cache: 'no-store' },
        );
        if (!response.ok) {
          setError('Failed to load dataset details.');
          setSelectedDatasetDetails(null);
          return;
        }
        const payload = (await response.json()) as EvaluationDatasetDetail;
        setSelectedDatasetDetails(payload);
      } catch {
        setError('Failed to reach the backend while loading dataset details.');
        setSelectedDatasetDetails(null);
      } finally {
        setIsLoadingDatasetDetails(false);
      }
    };

    void loadDatasetDetails();
  }, [selectedDatasetPath, showDatasetDetails]);

  const ingestSelectedFile = async () => {
    if (!selectedPath) {
      return;
    }
    setIsIngesting(true);
    setError(null);
    setShowChunks(false);
    try {
      const response = await fetch(`${API}/api/v1/encourage/ingest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          path: selectedPath,
          run_generation: runGeneration,
        }),
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
      setError('Failed to reach the backend while ingesting markdown file.');
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
          top_k: 3,
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const detail = typeof payload?.detail === 'string' ? payload.detail : 'Failed to retrieve.';
        setError(detail);
        return;
      }
      const payload = (await response.json()) as EncourageRetrieveResponse;
      setRetrieval(payload);
    } catch {
      setError('Failed to reach the backend while retrieving from pipeline.');
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

  const toggleDatasetDetails = () => {
    setShowDatasetDetails((current) => !current);
  };

  return (
    <main className="min-h-screen bg-gradient-to-br from-slate-50 to-slate-100 px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
      <div className="mx-auto w-full max-w-5xl">
        <div className="mb-12 text-center">
          <h1 className="font-serif text-4xl font-bold">Encourage RAG Pipeline</h1>
          <p className="mt-3 text-lg text-slate-600">3-Step Workflow: Ingest → Retrieve → Evaluate</p>
        </div>

        {error && (
          <div className="mb-6 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
            {error}
          </div>
        )}

        <div className="space-y-6">
          {/* STEP 1: INGEST */}
          <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
            <div className="mb-4 flex items-center gap-3">
              <div className="flex h-10 w-10 items-center justify-center rounded-full bg-emerald-100 font-semibold text-emerald-700">
                1
              </div>
              <h2 className="text-xl font-semibold text-slate-950">Ingest Markdown</h2>
            </div>
            <p className="mb-4 text-sm text-slate-600">
              Select a markdown file from PaddleDoc and ingest it into the Encourage vector store.
            </p>

            <div className="space-y-3 rounded-xl border border-slate-200 bg-slate-50 p-4">
              <label className="text-sm font-medium text-slate-700">Select Markdown File</label>
              {isLoading ? (
                <p className="text-sm text-slate-500">Loading markdown files...</p>
              ) : items.length === 0 ? (
                <p className="text-sm text-slate-500">No markdown files found.</p>
              ) : (
                <div className="grid gap-2">
                  {items.map((item) => (
                    <button
                      key={item.path}
                      onClick={() => setSelectedPath(item.path)}
                      className={`rounded-lg border-2 p-3 text-left transition ${
                        selectedPath === item.path
                          ? 'border-emerald-500 bg-emerald-50 text-emerald-900'
                          : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300'
                      }`}
                    >
                      <p className="font-medium">{item.filename}</p>
                      <p className="text-xs text-slate-500">{item.path}</p>
                    </button>
                  ))}
                </div>
              )}
            </div>

            <div className="mt-4 flex items-center gap-3">
              <label className="flex items-center gap-2 text-sm text-slate-700">
                <input
                  type="checkbox"
                  checked={runGeneration}
                  onChange={(e: { target: { checked: boolean } }) => setRunGeneration(e.target.checked)}
                  className="h-4 w-4 rounded"
                />
                Run generation
              </label>
              <Button
                onClick={ingestSelectedFile}
                disabled={!selectedPath || isIngesting || isLoading}
                className="ml-auto bg-emerald-600 hover:bg-emerald-700"
              >
                {isIngesting ? 'Ingesting...' : 'Ingest MD File'}
              </Button>
            </div>

            {ingested && (
              <div className="mt-4 rounded-lg border border-emerald-200 bg-emerald-50 p-4">
                <p className="font-medium text-emerald-900">✓ Markdown Ingested</p>
                <p className="mt-2 text-xs text-emerald-700">
                  <strong>{ingested.source_markdown.filename}</strong> · {ingested.source_markdown.document_count} chunks
                </p>

                {/* Chunks Preview */}
                <button
                  onClick={() => setShowChunks(!showChunks)}
                  className="mt-3 text-xs font-medium text-emerald-700 underline hover:text-emerald-900"
                >
                  {showChunks ? '▼ Hide' : '▶ Show'} Chunks ({ingested.source_markdown.document_count})
                </button>

                {showChunks && (
                  <div className="mt-3 space-y-2 max-h-96 overflow-y-auto">
                    {ingested.source_markdown.chunk_preview.length > 0 ? (
                      <>
                        <p className="text-xs text-emerald-700">
                          Showing all {ingested.source_markdown.chunk_preview.length} chunks
                        </p>
                        {ingested.source_markdown.chunk_preview.map((chunk, idx) => (
                          <div key={idx} className="rounded border border-emerald-200 bg-white p-3">
                            <p className="text-xs font-mono leading-relaxed text-slate-600">{chunk}</p>
                          </div>
                        ))}
                      </>
                    ) : (
                      <p className="text-xs text-slate-500">No chunk preview returned by the backend.</p>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* STEP 2: RETRIEVE */}
          <div
            className={`rounded-2xl border p-6 ${
              ingested
                ? 'border-slate-200 bg-white shadow-sm'
                : 'border-slate-200 bg-slate-50 opacity-50'
            }`}
          >
            <div className="mb-4 flex items-center gap-3">
              <div
                className={`flex h-10 w-10 items-center justify-center rounded-full font-semibold ${
                  ingested ? 'bg-blue-100 text-blue-700' : 'bg-slate-200 text-slate-500'
                }`}
              >
                2
              </div>
              <h2 className={`text-xl font-semibold ${ingested ? 'text-slate-950' : 'text-slate-500'}`}>
                Retrieve
              </h2>
            </div>
            <p className={`mb-4 text-sm ${ingested ? 'text-slate-600' : 'text-slate-500'}`}>
              Query the vector store to see what documents are retrieved.
            </p>

            {ingested ? (
              <div className="space-y-3">
                <textarea
                  value={query}
                  onChange={(e: { target: { value: string } }) => setQuery(e.target.value)}
                  placeholder="Ask a question..."
                  rows={3}
                  className="w-full rounded-lg border border-slate-200 p-3 text-sm"
                />
                <Button
                  onClick={retrieveFromPipeline}
                  disabled={isRetrieving || !query.trim()}
                  className="w-full bg-blue-600 hover:bg-blue-700"
                >
                  {isRetrieving ? 'Retrieving...' : 'Retrieve'}
                </Button>

                {retrieval && (
                  <div className="mt-4 space-y-3 rounded-2xl border border-blue-200 bg-blue-50 p-4 shadow-sm">
                    <div className="flex items-start justify-between gap-3">
                      <div>
                        <p className="text-xs font-semibold uppercase tracking-wide text-blue-700">
                          Retrieval Results
                        </p>
                        <p className="mt-1 text-sm text-blue-950">
                          {retrieval.results.length} result(s) from {retrieval.collection_name}
                        </p>
                      </div>
                      <div className="rounded-full bg-white px-3 py-1 text-xs font-medium text-blue-700 shadow-sm">
                        Higher score = closer semantic match
                      </div>
                    </div>

                    {retrieval.results.map((result, index) => {
                      const isBestHit = index === 0;
                      const scoreTone =
                        result.score >= 0.2
                          ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                          : result.score >= 0
                            ? 'border-amber-200 bg-amber-50 text-amber-800'
                            : 'border-rose-200 bg-rose-50 text-rose-800';

                      return (
                        <div
                          key={result.id}
                          className={`rounded-2xl border bg-white p-4 text-sm shadow-sm transition ${
                            isBestHit ? 'border-blue-300 ring-1 ring-blue-200' : 'border-blue-100'
                          }`}
                        >
                          <div className="flex items-start gap-3">
                            <div
                              className={`flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-xs font-bold ${
                                isBestHit ? 'bg-blue-600 text-white' : 'bg-slate-100 text-slate-600'
                              }`}
                            >
                              {index + 1}
                            </div>

                            <div className="min-w-0 flex-1 space-y-3">
                              <div className="flex flex-wrap items-center gap-2">
                                <span className={`rounded-full border px-2.5 py-1 text-xs font-semibold ${scoreTone}`}>
                                  Score {formatNumber(result.score)}
                                </span>
                                <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-xs font-medium text-slate-600">
                                  Distance {formatNumber(result.distance)}
                                </span>
                                {isBestHit && (
                                  <span className="rounded-full border border-blue-200 bg-blue-50 px-2.5 py-1 text-xs font-semibold text-blue-700">
                                    Best match
                                  </span>
                                )}
                              </div>

                              <p className="line-clamp-3 text-slate-700">{result.content}</p>

                              <div className="flex flex-wrap gap-3 text-xs text-slate-500">
                                <span className="font-mono">id: {result.id}</span>
                              </div>
                            </div>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            ) : (
              <p className="text-sm text-slate-500">Complete Step 1 (Ingest) to enable retrieval.</p>
            )}
          </div>

          {/* STEP 3: EVALUATE */}
          <div
            className={`rounded-2xl border p-6 ${
              ingested
                ? 'border-slate-200 bg-white shadow-sm'
                : 'border-slate-200 bg-slate-50 opacity-50'
            }`}
          >
            <div className="mb-4 flex items-center gap-3">
              <div
                className={`flex h-10 w-10 items-center justify-center rounded-full font-semibold ${
                  ingested ? 'bg-purple-100 text-purple-700' : 'bg-slate-200 text-slate-500'
                }`}
              >
                3
              </div>
              <h2 className={`text-xl font-semibold ${ingested ? 'text-slate-950' : 'text-slate-500'}`}>
                Evaluate
              </h2>
            </div>
            <p className={`mb-4 text-sm ${ingested ? 'text-slate-600' : 'text-slate-500'}`}>
              Run evaluation metrics against an evaluation dataset.
            </p>

            {ingested ? (
              <div className="space-y-3">
                <div>
                  <div className="flex items-center justify-between gap-3">
                    <label className="text-sm font-medium text-slate-700">Select Dataset</label>
                    <button
                      type="button"
                      onClick={toggleDatasetDetails}
                      className="text-xs font-medium text-purple-700 underline hover:text-purple-900"
                    >
                      {showDatasetDetails ? 'Hide dataset' : 'Show dataset'}
                    </button>
                  </div>
                  {isLoadingDatasets ? (
                    <p className="mt-2 text-sm text-slate-500">Loading datasets...</p>
                  ) : datasets.length === 0 ? (
                    <div className="mt-2 rounded border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
                      <p className="font-medium">⚠ No evaluation datasets found</p>
                      <p className="mt-1 text-xs">Expected location: <code className="bg-amber-100 px-2 py-1 rounded">docs/evaluation/*.jsonl</code></p>
                      <p className="mt-1 text-xs">Check if <code className="bg-amber-100 px-2 py-1 rounded">retrieval_dataset_template.jsonl</code> exists in the docs/evaluation folder.</p>
                    </div>
                  ) : (
                    <div className="mt-2 grid gap-2">
                      {datasets.map((item) => (
                        <button
                          key={item.path}
                          onClick={() => setSelectedDatasetPath(item.path)}
                          className={`rounded-lg border-2 p-3 text-left transition ${
                            selectedDatasetPath === item.path
                              ? 'border-purple-500 bg-purple-50 text-purple-900'
                              : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300'
                          }`}
                        >
                          <p className="font-medium">{item.filename}</p>
                          <p className="text-xs text-slate-500">{item.row_count} questions</p>
                        </button>
                      ))}
                    </div>
                  )}

                  {showDatasetDetails && selectedDatasetPath && (
                    <div className="mt-3 rounded-2xl border border-purple-200 bg-white p-4 shadow-sm">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <p className="text-sm font-semibold text-purple-950">Dataset Inspector</p>
                          <p className="text-xs text-slate-500">Opens the selected JSONL only when needed.</p>
                        </div>
                        <span className="rounded-full border border-purple-200 bg-purple-50 px-2.5 py-1 text-xs font-medium text-purple-700">
                          {isLoadingDatasetDetails ? 'Loading...' : 'Ready'}
                        </span>
                      </div>

                      {isLoadingDatasetDetails ? (
                        <p className="mt-3 text-sm text-slate-500">Loading dataset rows...</p>
                      ) : selectedDatasetDetails ? (
                        <div className="mt-3 space-y-3">
                          <div className="grid gap-2 text-xs text-slate-600 sm:grid-cols-2">
                            <p><span className="font-semibold">Rows:</span> {selectedDatasetDetails.row_count}</p>
                            <p><span className="font-semibold">File:</span> {selectedDatasetDetails.filename}</p>
                            <p className="sm:col-span-2"><span className="font-semibold">Source docs:</span> {selectedDatasetDetails.source_documents.length > 0 ? selectedDatasetDetails.source_documents.join(', ') : 'n/a'}</p>
                          </div>

                          <div className="max-h-96 space-y-3 overflow-y-auto pr-1">
                            {selectedDatasetDetails.rows.map((row, idx) => {
                              const headline =
                                typeof row.question === 'string' && row.question.trim()
                                  ? row.question
                                  : typeof row.query === 'string' && row.query.trim()
                                    ? row.query
                                    : `Row ${idx + 1}`;

                              return (
                                <div key={idx} className="rounded-xl border border-purple-100 bg-purple-50/40 p-3">
                                  <div className="flex items-start justify-between gap-3">
                                    <div>
                                      <p className="text-xs font-semibold uppercase tracking-wide text-purple-700">
                                        Row {idx + 1}
                                      </p>
                                      <p className="mt-1 text-sm font-medium text-slate-900">{headline}</p>
                                    </div>
                                  </div>
                                  <dl className="mt-3 grid gap-2 text-xs text-slate-600 sm:grid-cols-2">
                                    {['source_document', 'gold_answer', 'answer', 'evidence_quote'].map((key) => {
                                      const value = row[key];
                                      if (value === undefined || value === null || value === '') {
                                        return null;
                                      }
                                      return (
                                        <div key={key} className="rounded-lg bg-white px-3 py-2">
                                          <dt className="font-semibold text-slate-700">{key}</dt>
                                          <dd className="mt-1 break-words text-slate-600">{formatDatasetValue(value)}</dd>
                                        </div>
                                      );
                                    })}
                                  </dl>
                                  <details className="mt-3">
                                    <summary className="cursor-pointer text-xs font-medium text-purple-700">
                                      Show raw JSON
                                    </summary>
                                    <pre className="mt-2 overflow-x-auto rounded-lg bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-100">
                                      {JSON.stringify(row, null, 2)}
                                    </pre>
                                  </details>
                                </div>
                              );
                            })}
                          </div>
                        </div>
                      ) : (
                        <p className="mt-3 text-sm text-slate-500">No dataset details available.</p>
                      )}
                    </div>
                  )}
                </div>

                <Button
                  onClick={runEvaluation}
                  disabled={isEvaluating || !selectedDatasetPath}
                  className="w-full bg-purple-600 hover:bg-purple-700"
                >
                  {isEvaluating ? 'Evaluating...' : 'Run Evaluation'}
                </Button>

                {evaluation && (
                  <div className="rounded-lg border border-purple-200 bg-purple-50 p-4">
                    <p className="font-medium text-purple-900">✓ Evaluation Complete</p>
                    <div className="mt-3 grid grid-cols-2 gap-3 text-sm">
                      <div>
                        <p className="text-xs text-purple-700">MRR</p>
                        <p className="font-semibold text-purple-900">{evaluation.mrr.toFixed(4)}</p>
                      </div>
                      <div>
                        <p className="text-xs text-purple-700">Recall@{evaluation.recall_k}</p>
                        <p className="font-semibold text-purple-900">{evaluation.recall_at_k.toFixed(4)}</p>
                      </div>
                      <div>
                        <p className="text-xs text-purple-700">HitRate@{evaluation.recall_k}</p>
                        <p className="font-semibold text-purple-900">{evaluation.hit_rate_at_k.toFixed(4)}</p>
                      </div>
                      <div>
                        <p className="text-xs text-purple-700">MLflow Run</p>
                        <p className="truncate font-mono text-xs font-semibold text-purple-900">
                          {evaluation.mlflow_run_id || 'N/A'}
                        </p>
                      </div>
                    </div>

                    <details className="mt-4">
                      <summary className="cursor-pointer text-xs font-medium text-purple-700">
                        Show per-question results ({evaluation.per_question_results.length})
                      </summary>
                      <div className="mt-3 space-y-2">
                        {evaluation.per_question_results.map((item, idx) => (
                          <div key={item.id} className="rounded-lg border border-purple-100 bg-white p-3 text-xs text-slate-600">
                            <p className="font-semibold text-slate-900">
                              {idx + 1}. {item.question}
                            </p>
                            <p className="mt-1">Hit: {item.has_hit ? 'yes' : 'no'} · First hit rank: {item.first_hit_rank ?? 'n/a'}</p>
                            <p className="mt-1 break-words">Retrieved: {item.retrieved_document_ids.join(', ') || 'n/a'}</p>
                            <p className="mt-1 break-words">Reference: {item.reference_document_ids.join(', ') || 'n/a'}</p>
                          </div>
                        ))}
                      </div>
                    </details>
                  </div>
                )}
              </div>
            ) : (
              <p className="text-sm text-slate-500">Complete Step 1 (Ingest) to enable evaluation.</p>
            )}
          </div>
        </div>
      </div>
    </main>
  );
}
