'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import {
  ChevronLeft,
  ChevronRight,
  FileText,
  Play,
  RefreshCw,
  Search,
  Shuffle,
  Square,
  SquareCheck,
  StopCircle,
} from 'lucide-react';

import { Badge, ErrorNotice, errorMessage, Field, inputClass, LoadingState, SectionCard, Toggle } from '@/components/admin/admin-shared';
import { Button } from '@/components/ui/button';
import { apiJson } from '@/lib/api';

type CrawlMode = 'full' | 'discover-only' | 'download-pending-only';

type CrawlerStatus = {
  configured: boolean;
  recent_log: string[];
  run: {
    status?: 'running' | 'stopping' | 'finished' | 'failed' | 'interrupted' | string;
    mode?: CrawlMode;
    started_at?: string;
    finished_at?: string;
    return_code?: number;
    include_robots_blocked?: boolean;
    detail?: string;
  };
  catalog: {
    updated_at: string | null;
    pages_scanned: number;
    pending_pages: number;
    limit_pending_pages: number;
    documents_found: number;
    document_statuses: Record<string, number>;
    unique_files: number;
    robots_blocked: number;
    outside_scope: number;
    sitemap_errors: number;
    time_budget_exhausted: boolean;
    discovery_limit_hits: number;
  };
};

type CrawledFile = {
  id: string;
  filename: string;
  size_bytes: number;
  category: string;
  processable: boolean;
  status: string;
  sha256: string | null;
  urls: string[];
  sources: { page?: string; link_text?: string }[];
};

type FileListResponse = {
  items: CrawledFile[];
  total: number;
  categories: { name: string; count: number }[];
  offset: number;
  limit: number;
  sampled: boolean;
};

type ProcessResponse = {
  created: { file_id: string; job_id: string; filename: string; category: string }[];
  duplicates: { file_id: string; job_id: string }[];
  failures: { file_id: string; error: string }[];
  collection_id: string | null;
  mode: 'single' | 'collection';
};

const STATUS_URL = '/api/v1/hansemerkur/crawler/status';
const DOCUMENTS_URL = '/api/v1/hansemerkur/documents';
const DEFAULT_PAGE_SIZE = 50;
const PAGE_SIZE_OPTIONS = [50, 100, 200] as const;
const SAMPLE_MAX = 50;
const MAX_PAGES_ALLOWED = 100_000;

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(value: string | null | undefined): string {
  if (!value) return 'Not run yet';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function count(status: CrawlerStatus | null, key: string): number {
  return status?.catalog.document_statuses[key] ?? 0;
}

function PageNavigation({
  pageNumber,
  pageCount,
  visibleCount,
  totalCount,
  onPrevious,
  onNext,
}: {
  pageNumber: number;
  pageCount: number;
  visibleCount: number;
  totalCount: number;
  onPrevious: () => void;
  onNext: () => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="mr-1 text-xs tabular-nums text-slate-500">
        Page {pageNumber} of {pageCount} · {visibleCount}/{totalCount} files
      </span>
      <Button size="sm" variant="outline" aria-label="Previous page" disabled={pageNumber <= 1} onClick={onPrevious}>
        <ChevronLeft className="h-4 w-4" />
      </Button>
      <Button size="sm" variant="outline" aria-label="Next page" disabled={pageNumber >= pageCount} onClick={onNext}>
        <ChevronRight className="h-4 w-4" />
      </Button>
    </div>
  );
}

export function CrawlerTab() {
  const [status, setStatus] = useState<CrawlerStatus | null>(null);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [files, setFiles] = useState<CrawledFile[]>([]);
  const [categories, setCategories] = useState<FileListResponse['categories']>([]);
  const [totalFiles, setTotalFiles] = useState(0);
  const [loadingFiles, setLoadingFiles] = useState(true);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [search, setSearch] = useState('');
  const [category, setCategory] = useState('');
  const [offset, setOffset] = useState(0);
  const [pageSize, setPageSize] = useState<number>(DEFAULT_PAGE_SIZE);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [createdJobs, setCreatedJobs] = useState<ProcessResponse['created']>([]);
  const [sampleSize, setSampleSize] = useState(25);
  const [mode, setMode] = useState<CrawlMode>('full');
  const [includeRobotsBlocked, setIncludeRobotsBlocked] = useState(true);
  const [maxPages, setMaxPages] = useState(10_000);
  const [maxBytesMb, setMaxBytesMb] = useState(0);
  const [maxSeconds, setMaxSeconds] = useState(0);

  async function loadStatus() {
    try {
      const result = await apiJson<CrawlerStatus>(STATUS_URL);
      setStatus(result);
      setStatusError(null);
    } catch (error) {
      setStatusError(errorMessage(error));
    }
  }

  useEffect(() => {
    let active = true;
    const poll = async () => {
      try {
        const result = await apiJson<CrawlerStatus>(STATUS_URL);
        if (active) {
          setStatus(result);
          setStatusError(null);
        }
      } catch (error) {
        if (active) setStatusError(errorMessage(error));
      }
    };
    void poll();
    const timer = window.setInterval(poll, 2500);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  useEffect(() => {
    let active = true;
    const load = async () => {
      setLoadingFiles(true);
      const params = new URLSearchParams({ offset: String(offset), limit: String(pageSize) });
      if (search.trim()) params.set('search', search.trim());
      if (category) params.set('category', category);
      try {
        const result = await apiJson<FileListResponse>(`${DOCUMENTS_URL}?${params.toString()}`);
        if (!active) return;
        setFiles(result.items);
        setTotalFiles(result.total);
        setCategories(result.categories);
      } catch (error) {
        if (active) setActionError(errorMessage(error));
      } finally {
        if (active) setLoadingFiles(false);
      }
    };
    void load();
    return () => {
      active = false;
    };
  }, [offset, pageSize, search, category, status?.catalog.updated_at]);

  const isRunning = status?.run.status === 'running' || status?.run.status === 'stopping';
  const pageNumber = Math.floor(offset / pageSize) + 1;
  const pageCount = Math.max(1, Math.ceil(totalFiles / pageSize));

  function toggleSelected(fileId: string) {
    setSelected((previous) => {
      const next = new Set(previous);
      if (next.has(fileId)) next.delete(fileId);
      else next.add(fileId);
      return next;
    });
  }

  async function startCrawler() {
    setBusy(true);
    setActionError(null);
    setActionMessage(null);
    try {
      await apiJson(`${STATUS_URL.replace('/status', '/start')}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          mode,
          include_robots_blocked: mode !== 'discover-only' && includeRobotsBlocked,
          max_pages: maxPages,
          max_bytes: Math.max(0, Math.round(maxBytesMb * 1024 * 1024)),
          max_seconds: maxSeconds,
        }),
      });
      setActionMessage('Crawler started. Progress refreshes automatically.');
      await loadStatus();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function stopCrawler() {
    setBusy(true);
    setActionError(null);
    setActionMessage(null);
    try {
      await apiJson(`${STATUS_URL.replace('/status', '/stop')}`, { method: 'POST' });
      setActionMessage('Stop signal sent. The last catalog checkpoint is retained.');
      await loadStatus();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  function selectVisible() {
    setSelected((previous) => new Set([...previous, ...files.filter((file) => file.processable).map((file) => file.id)]));
  }

  function clearSelection() {
    setSelected(new Set());
  }

  async function selectRandomSample() {
    setBusy(true);
    setActionError(null);
    setActionMessage(null);
    try {
      const params = new URLSearchParams({ sample: String(sampleSize) });
      params.set('processable_only', 'true');
      if (search.trim()) params.set('search', search.trim());
      if (category) params.set('category', category);
      const result = await apiJson<FileListResponse>(`${DOCUMENTS_URL}?${params.toString()}`);
      setSelected((previous) => new Set([...previous, ...result.items.map((file) => file.id)]));
      setActionMessage(`Added ${result.items.length} random files to the selection.`);
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  async function processSelected() {
    if (selected.size === 0 || selected.size > 50) return;
    setBusy(true);
    setActionError(null);
    setActionMessage(null);
    try {
      const result = await apiJson<ProcessResponse>('/api/v1/hansemerkur/documents/process', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          file_ids: [...selected],
        }),
      });
      setActionMessage(
        `Queued ${result.created.length} Markdown jobs${result.collection_id ? ` in collection ${result.collection_id}` : ''}; ${result.duplicates.length} duplicates skipped; ${result.failures.length} failed.`,
      );
      setActionError(
        result.failures.length > 0
          ? result.failures.map(({ file_id, error }) => `${file_id}: ${error}`).join('; ')
          : null,
      );
      setCreatedJobs(result.created);
      if (result.failures.length === 0) clearSelection();
    } catch (error) {
      setActionError(errorMessage(error));
    } finally {
      setBusy(false);
    }
  }

  const availableJobs = status?.catalog.unique_files ?? 0;

  return (
    <div className="space-y-5">
      <SectionCard
        title="Crawler"
        description="Discover and download PDF/Word files. Select files below to start Markdown processing separately."
        actions={
          <div className="flex items-center gap-2">
            <Badge tone={isRunning ? 'emerald' : status?.run.status === 'failed' ? 'red' : 'slate'}>
              {isRunning ? 'Running' : status?.run.status ?? 'Idle'}
            </Badge>
            <Button size="sm" variant="outline" onClick={loadStatus} disabled={busy} aria-label="Refresh crawler status">
              <RefreshCw className="h-4 w-4" />
            </Button>
          </div>
        }
      >
        <ErrorNotice message={statusError} />
        <ErrorNotice message={actionError} />
        <p aria-live="polite" className="mb-3 text-sm text-emerald-700">{actionMessage}</p>
        {!status?.configured && (
          <p className="mb-4 rounded-xl border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">
            Crawler or .docs data directory is not mounted in the backend container.
          </p>
        )}
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          {[
            ['Pages scanned', status?.catalog.pages_scanned ?? 0],
            ['Pages pending', status?.catalog.pending_pages ?? 0],
            ['Unique PDF/Word files', status?.catalog.unique_files ?? 0],
            ['Pending documents', count(status, 'pending')],
            ['Robots blocked', status?.catalog.robots_blocked ?? 0],
            ['Outside scope', status?.catalog.outside_scope ?? 0],
            ['HTTP errors', count(status, 'http_error')],
            ['Discovery limit hits', status?.catalog.discovery_limit_hits ?? 0],
          ].map(([label, value]) => (
            <div key={String(label)} className="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2">
              <div className="text-xs font-medium text-slate-500">{label}</div>
              <div className="mt-1 text-xl font-semibold tabular-nums text-slate-950">{value}</div>
            </div>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
          <span>Last catalog update: {formatDate(status?.catalog.updated_at)}</span>
          <span>Run mode: {status?.run.mode ?? '—'}</span>
          <span>Unique files ready: {availableJobs}</span>
          {status?.catalog.sitemap_errors ? <span>Sitemap errors: {status.catalog.sitemap_errors}</span> : null}
        </div>
        <div className="mt-5 grid gap-4 border-t border-slate-100 pt-4 md:grid-cols-2 xl:grid-cols-4">
          <Field label="Run mode">
            <select className={inputClass} value={mode} disabled={isRunning} onChange={(event) => {
              const nextMode = event.target.value as CrawlMode;
              setMode(nextMode);
              if (nextMode === 'discover-only') setIncludeRobotsBlocked(false);
            }}>
              <option value="full">Discover + download</option>
              <option value="discover-only">Discover only</option>
              <option value="download-pending-only">Download only</option>
            </select>
          </Field>
          <Field label="Maximum pages" hint="Up to 100,000 crawled HTML pages. This is separate from the file-list pagination below.">
            <input className={inputClass} type="number" min={1} max={MAX_PAGES_ALLOWED} value={maxPages} disabled={isRunning} onChange={(event) => setMaxPages(Math.min(MAX_PAGES_ALLOWED, Math.max(1, Number(event.target.value) || 1)))} />
          </Field>
          <Field label="Maximum response size (MiB)" hint="0 = unlimited">
            <input className={inputClass} type="number" min={0} max={1024} value={maxBytesMb} disabled={isRunning} onChange={(event) => setMaxBytesMb(Math.min(1024, Math.max(0, Number(event.target.value) || 0)))} />
          </Field>
          <Field label="Time limit (seconds)" hint="0 = unlimited">
            <input className={inputClass} type="number" min={0} max={604800} value={maxSeconds} disabled={isRunning} onChange={(event) => setMaxSeconds(Number(event.target.value) || 0)} />
          </Field>
        </div>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-3">
            <Toggle
              checked={includeRobotsBlocked}
              onChange={setIncludeRobotsBlocked}
              disabled={isRunning || mode === 'discover-only'}
              label="Also attempt catalogued robots-blocked documents"
            />
          </div>
          <div className="flex gap-2">
            <Button size="sm" onClick={startCrawler} disabled={busy || isRunning || !status?.configured}>
              <Play className="mr-1.5 h-4 w-4" /> Start
            </Button>
            <Button size="sm" variant="outline" onClick={stopCrawler} disabled={busy || !isRunning}>
              <StopCircle className="mr-1.5 h-4 w-4" /> Stop
            </Button>
          </div>
        </div>
        {status?.recent_log?.length ? (
          <details className="mt-4 border-t border-slate-100 pt-3">
            <summary className="cursor-pointer text-sm font-medium text-slate-700">Recent crawler output</summary>
            <pre className="mt-2 max-h-56 overflow-auto rounded-xl bg-slate-950 p-3 text-xs text-slate-100">
              {status.recent_log.join('\n')}
            </pre>
          </details>
        ) : null}
      </SectionCard>

      <SectionCard
        title="Downloaded PDF/Word corpus"
        description="Filter or sample files, then queue selected documents through native Markdown extraction (no OCR profile)."
        actions={<Badge tone="slate">{selected.size} selected</Badge>}
      >
        <div className="mb-4 grid gap-3 lg:grid-cols-[minmax(14rem,1fr)_14rem_10rem_auto_auto_auto]">
          <label className="relative block text-sm font-medium text-slate-700">
            Search
            <Search className="pointer-events-none absolute left-3 top-[2.15rem] h-4 w-4 text-slate-400" />
            <input className={`${inputClass} pl-9`} value={search} onChange={(event) => { setOffset(0); setSearch(event.target.value); }} placeholder="Filename, category, source…" />
          </label>
          <Field label="Category">
            <select className={inputClass} value={category} onChange={(event) => { setOffset(0); setCategory(event.target.value); }}>
              <option value="">All categories</option>
              {categories.map((item) => <option key={item.name} value={item.name}>{item.name} ({item.count})</option>)}
            </select>
          </Field>
          <Field label="Files per page">
            <select className={inputClass} value={pageSize} onChange={(event) => { setOffset(0); setPageSize(Number(event.target.value)); }}>
              {PAGE_SIZE_OPTIONS.map((size) => <option key={size} value={size}>{size}</option>)}
            </select>
          </Field>
          <div className="flex items-end gap-2">
            <Field label="Random sample">
              <input className={`${inputClass} w-24`} type="number" min={1} max={SAMPLE_MAX} value={sampleSize} disabled={busy} onChange={(event) => setSampleSize(Math.min(SAMPLE_MAX, Math.max(1, Number(event.target.value) || 1)))} />
            </Field>
            <Button size="sm" variant="outline" onClick={selectRandomSample} disabled={busy || totalFiles === 0}>
              <Shuffle className="mr-1.5 h-4 w-4" /> Sample
            </Button>
          </div>
          <Button size="sm" variant="outline" className="self-end" onClick={selectVisible} disabled={files.length === 0}>
            <SquareCheck className="mr-1.5 h-4 w-4" /> Select page
          </Button>
          <Button size="sm" variant="outline" className="self-end" onClick={clearSelection} disabled={selected.size === 0}>
            <Square className="mr-1.5 h-4 w-4" /> Clear
          </Button>
        </div>

        <div className="mb-3 flex flex-wrap items-center justify-end gap-3 border-b border-slate-100 pb-3">
          <PageNavigation
            pageNumber={pageNumber}
            pageCount={pageCount}
            visibleCount={files.length}
            totalCount={totalFiles}
            onPrevious={() => setOffset(Math.max(0, offset - pageSize))}
            onNext={() => setOffset(Math.min(Math.max(0, (pageCount - 1) * pageSize), offset + pageSize))}
          />
        </div>

        <div className="mb-3 flex flex-wrap items-center justify-between gap-3 text-sm text-slate-600">
          <span>{totalFiles} files match the current filter. PDF/DOCX can be queued; choose up to 50 per batch.</span>
          <Button size="sm" onClick={processSelected} disabled={busy || selected.size === 0 || selected.size > 50}>
            <FileText className="mr-1.5 h-4 w-4" /> Queue Markdown jobs
          </Button>
        </div>
        {selected.size > 50 && <p className="mb-2 text-sm text-amber-700">Reduce selection to 50 files or fewer for one batch.</p>}

        {createdJobs.length > 0 && (
          <div className="mb-4 rounded-xl border border-emerald-200 bg-emerald-50 p-3">
            <h3 className="text-sm font-semibold text-emerald-900">Queued Markdown jobs</h3>
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-sm">
              {createdJobs.map((job) => (
                <Link key={job.job_id} href={`/jobs/${job.job_id}`} className="text-emerald-800 underline decoration-emerald-300 underline-offset-2">
                  {job.filename}
                </Link>
              ))}
            </div>
          </div>
        )}

        {loadingFiles ? <LoadingState label="Loading downloaded files…" /> : files.length === 0 ? (
          <p className="py-8 text-center text-sm text-slate-500">No downloaded PDF/Word files match these filters.</p>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-slate-200">
            <table className="w-full min-w-[760px] text-left text-sm">
              <thead className="bg-slate-50 text-xs uppercase text-slate-500">
                <tr><th className="w-10 px-3 py-2">Select</th><th className="px-3 py-2">File</th><th className="px-3 py-2">Category</th><th className="px-3 py-2">Size</th><th className="px-3 py-2">Sources</th></tr>
              </thead>
              <tbody className="divide-y divide-slate-100 bg-white">
                {files.map((file) => (
                  <tr key={file.id} className="hover:bg-slate-50">
                    <td className="px-3 py-2"><input type="checkbox" aria-label={`Select ${file.filename}`} checked={selected.has(file.id)} disabled={!file.processable} onChange={() => toggleSelected(file.id)} /></td>
                    <td className="max-w-[32rem] truncate px-3 py-2 font-medium text-slate-900" title={file.filename}>{file.filename}</td>
                    <td className="px-3 py-2"><div className="flex items-center gap-2"><Badge tone="slate">{file.category}</Badge>{!file.processable && <Badge tone="amber">Unsupported by Markdown pipeline</Badge>}</div></td>
                    <td className="whitespace-nowrap px-3 py-2 text-slate-600">{formatBytes(file.size_bytes)}</td>
                    <td className="px-3 py-2 text-slate-600">{file.urls.length}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-3 flex justify-end">
          <PageNavigation
            pageNumber={pageNumber}
            pageCount={pageCount}
            visibleCount={files.length}
            totalCount={totalFiles}
            onPrevious={() => setOffset(Math.max(0, offset - pageSize))}
            onNext={() => setOffset(Math.min(Math.max(0, (pageCount - 1) * pageSize), offset + pageSize))}
          />
        </div>
      </SectionCard>
    </div>
  );
}
