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

type EncourageGenerateResponse = {
  pipeline_id: string;
  query: string;
  model_name: string;
  answer: string;
  raw_output: string;
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
  mean_average_precision: number;
  ndcg: number;
  context_length: number;
  context_length_metric_source: string;
  recall_at_k: number;
  hit_rate_at_k: number;
  evaluation_mode: 'standard' | 'advanced';
  retrieval_metrics: Record<string, number>;
  advanced_metrics: Record<string, number>;
  advanced_status: string;
  warnings: string[];
  mlflow_experiment_id: string | null;
  mlflow_run_id: string | null;
  evaluation_summary: {
    question_hit_count: number;
    question_miss_count: number;
    first_hit_rank_breakdown: Record<string, number>;
    questions_without_hit: string[];
  };
  per_question_results: Array<{
    id: string;
    question: string;
    gold_answer: string;
    evidence_quote: string;
    source_document: string;
    reference_selection: {
      strategy: string;
      match_score: number;
      quote_token_count: number;
    };
    retrieved_document_ids: string[];
    reference_document_ids: string[];
    first_hit_rank: number | null;
    has_hit: boolean;
    retrieved_documents: Array<{
      rank: number;
      id: string;
      score: number;
      distance: number | null;
      content_preview: string;
      is_reference_match: boolean;
      meta_data: Record<string, unknown>;
    }>;
    reference_documents: Array<{
      id: string;
      content_preview: string;
    }>;
  }>;
};

type RagMethodOption = {
  id: string;
  label: string;
  description: string;
};

const RAG_METHOD_OPTIONS: RagMethodOption[] = [
  {
    id: 'Base',
    label: 'Base RAG',
    description: 'Dense retrieval baseline using embeddings.',
  },
  {
    id: 'BM25',
    label: 'BM25',
    description: 'Sparse keyword retrieval for exact term matches.',
  },
  {
    id: 'HybridBM25',
    label: 'Hybrid BM25',
    description: 'Combines dense semantic retrieval with BM25 lexical signals.',
  },
];

const API = API_BASE_URL;
const DEFAULT_STEP_TWO_QUERY = 'Variiert die Hoehe der Pauschalerstattung nach Tarifklasse?';

export default function EncouragePage() {
  const [items, setItems] = useState<MarkdownFileEntry[]>([]);
  const [datasets, setDatasets] = useState<EvaluationDatasetEntry[]>([]);
  const [selectedPath, setSelectedPath] = useState<string>('');
  const [selectedDatasetPath, setSelectedDatasetPath] = useState<string>('');
  const [ingested, setIngested] = useState<EncourageIngestResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingDatasets, setIsLoadingDatasets] = useState(true);
  const [isIngesting, setIsIngesting] = useState(false);
  const [selectedRagMethod, setSelectedRagMethod] = useState<string>('Base');
  const [query, setQuery] = useState(DEFAULT_STEP_TWO_QUERY);
  const [isRetrieving, setIsRetrieving] = useState(false);
  const [retrieval, setRetrieval] = useState<EncourageRetrieveResponse | null>(null);
  const [isGenerating, setIsGenerating] = useState(false);
  const [generation, setGeneration] = useState<EncourageGenerateResponse | null>(null);
  const [isEvaluating, setIsEvaluating] = useState(false);
  const [evaluation, setEvaluation] = useState<EncourageEvaluationResponse | null>(null);
  const [selectedEvaluationMode, setSelectedEvaluationMode] = useState<'standard' | 'advanced'>(
    'standard',
  );
  const [error, setError] = useState<string | null>(null);
  const [showChunks, setShowChunks] = useState(false);
  const [showTopKChunks, setShowTopKChunks] = useState(false);
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

  const formatReferenceStrategy = (value: string) => {
    return value
      .split('_')
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(' ');
  };

  const formatValue = (value: unknown) => {
    if (value === null || value === undefined || value === '') {
      return 'n/a';
    }
    if (typeof value === 'number') {
      if (Number.isInteger(value)) {
        return String(value);
      }
      return value.toFixed(4);
    }
    if (typeof value === 'boolean') {
      return value ? 'true' : 'false';
    }
    return String(value);
  };

  const resolveMlflowUrl = () => {
    if (typeof window === 'undefined') {
      return 'http://localhost:5000';
    }
    const protocol = window.location.protocol === 'https:' ? 'https:' : 'http:';
    return `${protocol}//${window.location.hostname}:5000`;
  };

  const resolveMlflowRunUrl = (experimentId: string, runId: string) => {
    return `${resolveMlflowUrl()}/#/experiments/${experimentId}/runs/${runId}/model-metrics`;
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
    setShowTopKChunks(false);
    try {
      const response = await fetch(`${API}/api/v1/encourage/ingest`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          path: selectedPath,
          rag_method: selectedRagMethod,
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
      setGeneration(null);
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
          top_k: ingested.pipeline.top_k,
          collection_name: ingested.pipeline.collection_name,
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

  const generateFromPipeline = async () => {
    if (!ingested?.pipeline.pipeline_id || !query.trim()) {
      return;
    }
    setIsGenerating(true);
    setError(null);
    try {
      const response = await fetch(`${API}/api/v1/encourage/generate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          pipeline_id: ingested.pipeline.pipeline_id,
          query,
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const detail = typeof payload?.detail === 'string' ? payload.detail : 'Failed to generate answer.';
        setError(detail);
        return;
      }
      const payload = (await response.json()) as EncourageGenerateResponse;
      setGeneration(payload);
    } catch {
      setError('Failed to reach the backend while generating an answer.');
    } finally {
      setIsGenerating(false);
    }
  };

  const runEvaluation = async () => {
    if (!ingested?.pipeline.pipeline_id || !selectedDatasetPath) {
      return;
    }
    const chunkMaxChars =
      typeof ingested.debug.config.chunk_max_chars === 'number'
        ? ingested.debug.config.chunk_max_chars
        : undefined;
    const chunkOverlapChars =
      typeof ingested.debug.config.chunk_overlap_chars === 'number'
        ? ingested.debug.config.chunk_overlap_chars
        : undefined;
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
          evaluation_mode: selectedEvaluationMode,
          collection_name: ingested.pipeline.collection_name,
          markdown_path: ingested.source_markdown.path,
          top_k: ingested.pipeline.top_k,
          chunk_max_chars: chunkMaxChars,
          chunk_overlap_chars: chunkOverlapChars,
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

            <div className="mt-4 space-y-3 rounded-xl border border-slate-200 bg-slate-50 p-4">
              <label className="text-sm font-medium text-slate-700">Select RAG Method</label>
              <div className="grid gap-2 sm:grid-cols-3">
                {RAG_METHOD_OPTIONS.map((method) => (
                  <button
                    key={method.id}
                    onClick={() => setSelectedRagMethod(method.id)}
                    className={`rounded-lg border-2 p-3 text-left transition ${
                      selectedRagMethod === method.id
                        ? 'border-emerald-500 bg-emerald-50 text-emerald-900'
                        : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300'
                    }`}
                  >
                    <p className="text-sm font-medium">{method.label}</p>
                    <p className="mt-1 text-xs text-slate-500">{method.description}</p>
                  </button>
                ))}
              </div>
            </div>

            <div className="mt-4 flex items-center gap-3">
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
                <p className="mt-1 text-xs text-emerald-700">
                  Method: <strong>{ingested.pipeline.rag_method}</strong>
                </p>

                <div className="mt-3 rounded-lg border border-emerald-200 bg-white p-3">
                  <p className="text-xs font-medium uppercase tracking-wide text-emerald-700">
                    Ingestion & Indexing Parameters (Step 1)
                  </p>
                  <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3 text-xs text-slate-700">
                    <p>
                      <span className="font-semibold">chunk_count:</span> {ingested.pipeline.document_count}
                    </p>
                    <p>
                      <span className="font-semibold">document_count:</span> {ingested.pipeline.document_count}
                    </p>
                    <p>
                      <span className="font-semibold">chunk_max_chars:</span>{' '}
                      {formatValue(ingested.debug.config.chunk_max_chars)}
                    </p>
                    <p>
                      <span className="font-semibold">chunk_overlap_chars:</span>{' '}
                      {formatValue(ingested.debug.config.chunk_overlap_chars)}
                    </p>
                    <p>
                      <span className="font-semibold">collection_name:</span>{' '}
                      <span className="break-all">{ingested.pipeline.collection_name}</span>
                    </p>
                    <p>
                      <span className="font-semibold">top_k:</span> {ingested.pipeline.top_k}
                    </p>
                    <p>
                      <span className="font-semibold">rag_method:</span> {ingested.pipeline.rag_method}
                    </p>
                    <p className="sm:col-span-2 lg:col-span-2 break-all">
                      <span className="font-semibold">source_md_path:</span> {ingested.source_markdown.path}
                    </p>
                    <p className="break-all">
                      <span className="font-semibold">source_md_filename:</span>{' '}
                      {ingested.source_markdown.filename}
                    </p>
                  </div>
                </div>

                {/* Chunks Preview */}
                <div className="mt-3 flex flex-wrap items-center gap-3">
                  <button
                    onClick={() => setShowChunks(!showChunks)}
                    className="text-xs font-medium text-emerald-700 underline hover:text-emerald-900"
                  >
                    {showChunks ? '▼ Hide' : '▶ Show'} All Chunks ({ingested.source_markdown.document_count})
                  </button>
                  <button
                    onClick={() => setShowTopKChunks(!showTopKChunks)}
                    className="text-xs font-medium text-emerald-700 underline hover:text-emerald-900"
                  >
                    {showTopKChunks ? '▼ Hide' : '▶ Show'} Top-{ingested.pipeline.top_k} Chunks
                  </button>
                </div>

                {showTopKChunks && (
                  <div className="mt-3 space-y-2 max-h-96 overflow-y-auto">
                    {ingested.source_markdown.chunk_preview.length > 0 ? (
                      <>
                        <p className="text-xs text-emerald-700">
                          Showing first {Math.min(ingested.pipeline.top_k, ingested.source_markdown.chunk_preview.length)} of{' '}
                          {ingested.source_markdown.chunk_preview.length} chunks
                        </p>
                        {ingested.source_markdown.chunk_preview
                          .slice(0, ingested.pipeline.top_k)
                          .map((chunk, idx) => (
                            <div key={`topk-${idx}`} className="rounded border border-emerald-200 bg-white p-3">
                              <p className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-emerald-700">
                                Chunk {idx + 1}
                              </p>
                              <p className="text-xs font-mono leading-relaxed text-slate-600">{chunk}</p>
                            </div>
                          ))}
                      </>
                    ) : (
                      <p className="text-xs text-slate-500">No chunk preview returned by the backend.</p>
                    )}
                  </div>
                )}

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
                Retrieve & Generate
              </h2>
            </div>
            <p className={`mb-4 text-sm ${ingested ? 'text-slate-600' : 'text-slate-500'}`}>
              Ask your own question, inspect retrieved chunks, and generate an answer from top-k context.
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
                <div className="grid gap-2 sm:grid-cols-2">
                  <Button
                    onClick={retrieveFromPipeline}
                    disabled={isRetrieving || !query.trim()}
                    className="w-full bg-blue-600 hover:bg-blue-700"
                  >
                    {isRetrieving ? 'Retrieving...' : 'Retrieve'}
                  </Button>
                  <Button
                    onClick={generateFromPipeline}
                    disabled={isGenerating || !query.trim()}
                    className="w-full bg-cyan-600 hover:bg-cyan-700"
                  >
                    {isGenerating ? 'Generating...' : 'Generate Answer'}
                  </Button>
                </div>

                {generation && (
                  <div className="mt-4 rounded-2xl border border-cyan-200 bg-cyan-50 p-4 shadow-sm">
                    <p className="text-xs font-semibold uppercase tracking-wide text-cyan-700">
                      Generated Answer
                    </p>
                    <div className="mt-2 grid gap-2 sm:grid-cols-2 text-xs text-cyan-900">
                      <p>
                        <span className="font-semibold">model:</span> {generation.model_name}
                      </p>
                      <p>
                        <span className="font-semibold">answer chars:</span> {generation.answer.length}
                      </p>
                      <p className="sm:col-span-2 break-words">
                        <span className="font-semibold">query:</span> {generation.query}
                      </p>
                    </div>
                    <p className="mt-2 rounded-lg border border-cyan-100 bg-white p-3 text-sm whitespace-pre-wrap text-slate-800">
                      {generation.answer || 'n/a'}
                    </p>
                    <p className="mt-3 text-xs font-medium uppercase tracking-wide text-cyan-700">
                      Raw Model Output
                    </p>
                    <pre className="mt-1 overflow-x-auto rounded-lg border border-cyan-100 bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-100 whitespace-pre-wrap">
                      {generation.raw_output || generation.answer || 'n/a'}
                    </pre>
                  </div>
                )}

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

                              <div className="max-h-80 overflow-auto rounded-lg border border-slate-200 bg-slate-50 p-3">
                                <pre className="whitespace-pre-wrap break-words text-xs leading-relaxed text-slate-700">
                                  {result.content}
                                </pre>
                              </div>

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

                <div className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                  <p className="text-sm font-medium text-slate-700">Evaluation Mode</p>
                  <div className="mt-2 grid gap-2 sm:grid-cols-2">
                    <button
                      type="button"
                      onClick={() => setSelectedEvaluationMode('standard')}
                      className={`rounded-lg border-2 p-3 text-left transition ${
                        selectedEvaluationMode === 'standard'
                          ? 'border-purple-500 bg-purple-50 text-purple-900'
                          : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300'
                      }`}
                    >
                      <p className="text-sm font-medium">Standard Evaluation</p>
                      <p className="mt-1 text-xs text-slate-500">
                        Fast retrieval metrics for iterative experiments.
                      </p>
                    </button>
                    <button
                      type="button"
                      onClick={() => setSelectedEvaluationMode('advanced')}
                      className={`rounded-lg border-2 p-3 text-left transition ${
                        selectedEvaluationMode === 'advanced'
                          ? 'border-purple-500 bg-purple-50 text-purple-900'
                          : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300'
                      }`}
                    >
                      <p className="text-sm font-medium">Advanced Evaluation</p>
                      <p className="mt-1 text-xs text-slate-500">
                        Adds LLM-based context metrics (more cost and runtime).
                      </p>
                    </button>
                  </div>
                </div>

                {evaluation && (
                  <div className="rounded-lg border border-purple-200 bg-purple-50 p-4">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="font-medium text-purple-900">✓ Evaluation Complete</p>
                      <span className="rounded-full border border-purple-200 bg-white px-2.5 py-1 text-xs font-semibold text-purple-700">
                        Mode {evaluation.evaluation_mode}
                      </span>
                      {evaluation.evaluation_mode === 'advanced' && (
                        <span
                          className={`rounded-full border px-2.5 py-1 text-xs font-semibold ${
                            evaluation.advanced_status === 'computed'
                              ? 'border-emerald-200 bg-emerald-50 text-emerald-700'
                              : evaluation.advanced_status === 'failed'
                                ? 'border-rose-200 bg-rose-50 text-rose-700'
                                : 'border-amber-200 bg-amber-50 text-amber-700'
                          }`}
                        >
                          Advanced {evaluation.advanced_status}
                        </span>
                      )}
                    </div>
                    {(() => {
                      const metricEntries = Object.entries(evaluation.retrieval_metrics).sort(([left], [right]) =>
                        left.localeCompare(right),
                      );
                      const advancedMetricEntries = Object.entries(evaluation.advanced_metrics).sort(
                        ([left], [right]) => left.localeCompare(right),
                      );
                      const coreMetricSet = new Set(['mrr', 'mean_average_precision', 'ndcg']);
                      const retrievalCoreMetrics = metricEntries.filter(
                        ([metricName]) =>
                          coreMetricSet.has(metricName) ||
                          metricName.startsWith('recall_at_') ||
                          metricName.startsWith('hit_rate_at_'),
                      );
                      const retrievalCoreMetricNames = new Set(
                        retrievalCoreMetrics.map(([metricName]) => metricName),
                      );
                      const retrievalDiagnosticMetrics = metricEntries.filter(
                        ([metricName]) => !retrievalCoreMetricNames.has(metricName),
                      );
                      const resolvedQuery = retrieval?.query ?? (query.trim() || null);

                      const chunkMaxChars =
                        typeof ingested?.debug.config.chunk_max_chars === 'number'
                          ? ingested.debug.config.chunk_max_chars
                          : null;
                      const chunkOverlapChars =
                        typeof ingested?.debug.config.chunk_overlap_chars === 'number'
                          ? ingested.debug.config.chunk_overlap_chars
                          : null;

                      const ingestionIndexingMetrics: Array<[string, unknown]> = [
                        ['chunk_count', ingested?.pipeline.document_count ?? null],
                        ['document_count', ingested?.pipeline.document_count ?? null],
                        ['chunk_max_chars', chunkMaxChars],
                        ['chunk_overlap_chars', chunkOverlapChars],
                        ['collection_name', evaluation.collection_name],
                        ['rag_method', ingested?.pipeline.rag_method ?? null],
                        ['source_md_filename', ingested?.source_markdown.filename ?? null],
                        ['source_md_path', evaluation.markdown_path],
                        ['top_k', evaluation.top_k],
                      ];

                      const evaluationParameterMetrics: Array<[string, unknown]> = [
                        ['dataset_filename', evaluation.dataset_filename],
                        ['dataset_path', evaluation.dataset_path],
                        ['evaluated_question_count', evaluation.evaluated_question_count],
                        ['event', 'evaluation'],
                        ['pipeline_id', evaluation.pipeline_id],
                        ['query', resolvedQuery],
                        ['question_count', evaluation.question_count],
                        ['evaluation_mode', evaluation.evaluation_mode],
                        ['recall_k', evaluation.recall_k],
                        ['context_length_source', evaluation.context_length_metric_source],
                      ];

                      return (
                        <>
                          <details className="mt-3" open>
                            <summary className="cursor-pointer text-xs font-medium uppercase tracking-wide text-purple-700">
                              Retrieval (Core for Comparison) ({retrievalCoreMetrics.length})
                            </summary>
                            <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                              {retrievalCoreMetrics.map(([metricName, metricValue]) => (
                                <div key={metricName} className="rounded-lg border border-purple-100 bg-white px-3 py-2 text-xs">
                                  <p className="font-medium text-slate-700">{metricName}</p>
                                  <p className="mt-1 font-semibold text-slate-900">{formatNumber(metricValue)}</p>
                                </div>
                              ))}
                            </div>
                          </details>

                          {retrievalDiagnosticMetrics.length > 0 && (
                            <details className="mt-3" open>
                              <summary className="cursor-pointer text-xs font-medium uppercase tracking-wide text-purple-700">
                                Retrieval Diagnostics ({retrievalDiagnosticMetrics.length})
                              </summary>
                              <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                                {retrievalDiagnosticMetrics.map(([metricName, metricValue]) => (
                                  <div key={metricName} className="rounded-lg border border-purple-100 bg-white px-3 py-2 text-xs">
                                    <p className="font-medium text-slate-700">{metricName}</p>
                                    <p className="mt-1 font-semibold text-slate-900">{formatNumber(metricValue)}</p>
                                  </div>
                                ))}
                              </div>
                            </details>
                          )}

                          {(evaluation.evaluation_mode === 'advanced' || advancedMetricEntries.length > 0) && (
                            <details className="mt-3" open>
                              <summary className="cursor-pointer text-xs font-medium uppercase tracking-wide text-purple-700">
                                Advanced Context Metrics ({advancedMetricEntries.length})
                              </summary>
                              <p className="mt-1 text-[11px] text-purple-700/80">
                                LLM-based supplemental metrics for context precision and context recall.
                              </p>
                              <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                                <div className="rounded-lg border border-purple-100 bg-white px-3 py-2 text-xs">
                                  <p className="font-medium text-slate-700">advanced_status</p>
                                  <p className="mt-1 font-semibold text-slate-900">{evaluation.advanced_status}</p>
                                </div>
                                {advancedMetricEntries.map(([metricName, metricValue]) => (
                                  <div key={metricName} className="rounded-lg border border-purple-100 bg-white px-3 py-2 text-xs">
                                    <p className="font-medium text-slate-700">{metricName}</p>
                                    <p className="mt-1 font-semibold text-slate-900">{formatNumber(metricValue)}</p>
                                  </div>
                                ))}
                              </div>
                              {advancedMetricEntries.length === 0 && (
                                <p className="mt-2 text-xs text-slate-500">
                                  No advanced metrics were computed for this run.
                                </p>
                              )}
                              {evaluation.warnings.length > 0 && (
                                <div className="mt-3 space-y-2">
                                  {evaluation.warnings.map((warning, index) => (
                                    <div key={index} className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                                      {warning}
                                    </div>
                                  ))}
                                </div>
                              )}
                            </details>
                          )}

                          <details className="mt-3">
                            <summary className="cursor-pointer text-xs font-medium uppercase tracking-wide text-purple-700">
                              Ingestion & Indexing Parameters ({ingestionIndexingMetrics.length})
                            </summary>
                            <p className="mt-1 text-[11px] text-purple-700/80">
                              These values come from Step 1 (Ingest) and remain visible for comparison.
                            </p>
                            <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                              {ingestionIndexingMetrics.map(([name, value]) => (
                                <div key={name} className="rounded-lg border border-purple-100 bg-white px-3 py-2 text-xs">
                                  <p className="font-medium text-slate-700">{name}</p>
                                  <p className="mt-1 font-semibold text-slate-900 break-words">{formatValue(value)}</p>
                                </div>
                              ))}
                            </div>
                          </details>

                          <details className="mt-3" open>
                            <summary className="cursor-pointer text-xs font-medium uppercase tracking-wide text-purple-700">
                              Evaluation Parameters ({evaluationParameterMetrics.length})
                            </summary>
                            <div className="mt-2 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                              {evaluationParameterMetrics.map(([name, value]) => (
                                <div key={name} className="rounded-lg border border-purple-100 bg-white px-3 py-2 text-xs">
                                  <p className="font-medium text-slate-700">{name}</p>
                                  <p className="mt-1 font-semibold text-slate-900 break-words">{formatValue(value)}</p>
                                </div>
                              ))}
                            </div>
                          </details>

                          <details className="mt-3">
                            <summary className="cursor-pointer text-xs font-medium uppercase tracking-wide text-purple-700">Tracking</summary>
                            <div className="mt-2 grid grid-cols-1 gap-3 text-sm sm:grid-cols-2">
                              <div className="rounded-lg border border-purple-100 bg-white px-3 py-2">
                                <p className="text-xs text-purple-700">Context Length Source</p>
                                <p className="font-semibold text-purple-900 break-words">
                                  {evaluation.context_length_metric_source}
                                </p>
                              </div>
                              <div className="rounded-lg border border-purple-100 bg-white px-3 py-2">
                                <p className="text-xs text-purple-700">MLflow Run</p>
                                {evaluation.mlflow_run_id && evaluation.mlflow_experiment_id ? (
                                  <a
                                    href={resolveMlflowRunUrl(
                                      evaluation.mlflow_experiment_id,
                                      evaluation.mlflow_run_id,
                                    )}
                                    target="_blank"
                                    rel="noreferrer"
                                    className="block truncate font-mono text-xs font-semibold text-purple-900 underline decoration-purple-300 underline-offset-2 hover:text-purple-700"
                                  >
                                    {evaluation.mlflow_run_id}
                                  </a>
                                ) : (
                                  <p className="truncate font-mono text-xs font-semibold text-purple-900">N/A</p>
                                )}
                              </div>
                            </div>
                          </details>

                          <details className="mt-4">
                            <summary className="cursor-pointer text-xs font-medium text-purple-700">
                              Show complete raw payload
                            </summary>
                            <pre className="mt-2 overflow-x-auto rounded-lg bg-slate-950 p-3 text-[11px] leading-relaxed text-slate-100">
                              {JSON.stringify(
                                {
                                  evaluation,
                                  ingest: ingested,
                                  retrieval,
                                },
                                null,
                                2,
                              )}
                            </pre>
                          </details>
                        </>
                      );
                    })()}

                    <div className="mt-4 grid gap-3 md:grid-cols-3">
                      <div className="rounded-lg border border-purple-100 bg-white p-3">
                        <p className="text-xs font-medium uppercase tracking-[0.2em] text-purple-700">Questions With Hit</p>
                        <p className="mt-1 text-lg font-semibold text-slate-900">{evaluation.evaluation_summary.question_hit_count}</p>
                      </div>
                      <div className="rounded-lg border border-purple-100 bg-white p-3">
                        <p className="text-xs font-medium uppercase tracking-[0.2em] text-purple-700">Questions Without Hit</p>
                        <p className="mt-1 text-lg font-semibold text-slate-900">{evaluation.evaluation_summary.question_miss_count}</p>
                      </div>
                      <div className="rounded-lg border border-purple-100 bg-white p-3">
                        <p className="text-xs font-medium uppercase tracking-[0.2em] text-purple-700">First Hit Breakdown</p>
                        <p className="mt-1 break-words text-sm text-slate-700">
                          {Object.entries(evaluation.evaluation_summary.first_hit_rank_breakdown)
                            .map(([rank, count]) => `${rank}: ${count}`)
                            .join(' · ') || 'n/a'}
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
                            <p className="mt-1 text-slate-500">
                              Reference strategy: {formatReferenceStrategy(item.reference_selection.strategy)} · Match score: {formatNumber(item.reference_selection.match_score)}
                            </p>
                            {item.source_document && (
                              <p className="mt-1 break-words text-slate-500">Source document: {item.source_document}</p>
                            )}
                            {item.evidence_quote && (
                              <p className="mt-2 rounded-md bg-slate-50 p-2 text-slate-700">Evidence quote: {item.evidence_quote}</p>
                            )}
                            {item.gold_answer && (
                              <p className="mt-2 rounded-md bg-emerald-50 p-2 text-slate-700">Gold answer: {item.gold_answer}</p>
                            )}
                            <div className="mt-3 grid gap-2 lg:grid-cols-2">
                              <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                                <p className="font-semibold text-slate-900">Retrieved chunks</p>
                                <div className="mt-2 space-y-2">
                                  {item.retrieved_documents.map((document) => (
                                    <div
                                      key={document.id}
                                      className={`rounded-md border p-2 ${document.is_reference_match ? 'border-emerald-200 bg-emerald-50' : 'border-slate-200 bg-white'}`}
                                    >
                                      <p className="font-medium text-slate-900">#{document.rank} · {document.id}</p>
                                      <p className="mt-1 text-slate-600">Score {formatNumber(document.score)} · Distance {formatNumber(document.distance)}</p>
                                      <p className="mt-1 text-slate-700">{document.content_preview || 'n/a'}</p>
                                    </div>
                                  ))}
                                  {item.retrieved_documents.length === 0 && (
                                    <p className="text-slate-500">No retrieved chunks available.</p>
                                  )}
                                </div>
                              </div>
                              <div className="rounded-lg border border-slate-200 bg-slate-50 p-3">
                                <p className="font-semibold text-slate-900">Reference chunks</p>
                                <div className="mt-2 space-y-2">
                                  {item.reference_documents.map((document) => (
                                    <div key={document.id} className="rounded-md border border-slate-200 bg-white p-2">
                                      <p className="font-medium text-slate-900">{document.id}</p>
                                      <p className="mt-1 text-slate-700">{document.content_preview || 'n/a'}</p>
                                    </div>
                                  ))}
                                  {item.reference_documents.length === 0 && (
                                    <p className="text-slate-500">No reference chunks resolved.</p>
                                  )}
                                </div>
                              </div>
                            </div>
                            <p className="mt-2 break-words text-slate-500">Retrieved IDs: {item.retrieved_document_ids.join(', ') || 'n/a'}</p>
                            <p className="mt-1 break-words text-slate-500">Reference IDs: {item.reference_document_ids.join(', ') || 'n/a'}</p>
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
