'use client';

import { useCallback, useMemo } from 'react';
import Link from 'next/link';
import { FileInput, FilePlus, FileText, Files, Import, Mail } from 'lucide-react';

import { buttonVariants } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { apiFetch } from '@/lib/api';
import { useCachedResource, useVisiblePolling } from '@/lib/data-cache';

// This component intentionally keeps its own Job type instead of importing
// the one from ./shared: that type only carries the fields HomeDashboard
// needs (settings.folder/subfolder). document-browser.tsx follows the same
// pattern for the same reason — the fields below (settings.mode,
// execution.page_count) come straight from the backend's processing_info
// blob and are read defensively rather than trusted against a shared shape.
type JobStatus = 'PENDING' | 'RUNNING' | 'FINISHED' | 'FAILED';

type Job = {
  id: string;
  original_filename: string;
  status: JobStatus;
  created_at: string;
  // Optional/defensive per contract: GET /api/v1/jobs list items carry
  // owner (see backend schemas/import_.py ImportRunOwner), read as
  // job.owner?.username since older cached payloads may lack it.
  owner?: { id: string; username: string } | null;
  processing_info?: {
    settings?: Record<string, unknown>;
    execution?: Record<string, unknown>;
  } | null;
};


// Both creation entry points always appear together, centered, in a soft
// emerald pair — deliberately lighter than a solid primary and never tucked
// into the page corner (explicit user preference).
const softCtaClass = cn(
  buttonVariants({ variant: 'outline' }),
  'border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-100 hover:text-emerald-900',
);
const softCtaSmClass = cn(
  buttonVariants({ variant: 'outline', size: 'sm' }),
  'border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-100 hover:text-emerald-900',
);

const JOBS_KEY = '/api/v1/jobs';

// Mirrors document-browser.tsx's statusBadge palette exactly (kept local per
// task instructions — this component must not import document-browser.tsx).
const statusBadge: Record<JobStatus, string> = {
  PENDING: 'bg-slate-100 text-slate-700',
  RUNNING: 'bg-emerald-100 text-emerald-800',
  FINISHED: 'bg-emerald-100 text-emerald-800',
  FAILED: 'bg-red-600/20 text-red-700',
};

/** Same derivation as document-browser.tsx's pageCountForJob, returning a number for summation. */
function pageCountForJob(job: Job): number | null {
  const execution = job.processing_info?.execution;
  const direct = execution?.page_count;
  if (typeof direct === 'number') {
    return direct;
  }
  const structure = execution?.structure;
  const nested = typeof structure === 'object' && structure !== null ? (structure as Record<string, unknown>).page_count : null;
  return typeof nested === 'number' ? nested : null;
}

type JobType = 'Confluence Import' | 'Mail' | 'Multiple files' | 'Single file';

/**
 * Job type is read from processing_info.settings.mode, whose values were
 * confirmed against the backend (backend/app/api/routes.py,
 * backend/app/api/mail_routes.py, backend/app/workers/import_tasks.py):
 * 'single' (explicit or absent) is a lone upload, 'collection' is a file
 * uploaded into a multi-file Collection, 'mail_attachment' is pulled from an
 * API-ingested mail message, and 'import' / 'import_attachment' are a
 * Confluence page import and the attachment jobs it spawns — both belong to
 * the same import run (see routes.py:807/848, import_tasks.py:467/681).
 */
function jobType(job: Job): JobType {
  const mode = job.processing_info?.settings?.mode;
  switch (mode) {
    case 'import':
    case 'import_attachment':
      return 'Confluence Import';
    case 'mail_attachment':
      return 'Mail';
    case 'collection':
      return 'Multiple files';
    default:
      return 'Single file';
  }
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

async function fetchJobs(): Promise<Job[]> {
  const response = await apiFetch(JOBS_KEY, { cache: 'no-store' });
  if (!response.ok) {
    throw new Error(`Request to ${JOBS_KEY} failed with status ${response.status}`);
  }
  const payload = (await response.json()) as { items?: Job[] };
  return payload.items ?? [];
}

type Stats = {
  running: number;
  finished: number;
  failed: number;
  pagesProcessed: number;
};

function deriveStats(jobs: Job[]): Stats {
  let running = 0;
  let finished = 0;
  let failed = 0;
  let pagesProcessed = 0;

  for (const job of jobs) {
    if (job.status === 'RUNNING' || job.status === 'PENDING') {
      running += 1;
    } else if (job.status === 'FINISHED') {
      finished += 1;
      pagesProcessed += pageCountForJob(job) ?? 0;
    } else if (job.status === 'FAILED') {
      failed += 1;
    }
  }

  return { running, finished, failed, pagesProcessed };
}

function deriveTypeCounts(jobs: Job[]): Record<JobType, number> {
  const counts: Record<JobType, number> = {
    'Single file': 0,
    'Multiple files': 0,
    Mail: 0,
    'Confluence Import': 0,
  };
  for (const job of jobs) {
    counts[jobType(job)] += 1;
  }
  return counts;
}

// urlType is the ?type= value /jobs accepts as an initial client filter
// (contract). Order and bar colors are the "By type" section's own spec:
// single/collection/import/mail in emerald-600/emerald-400/emerald-200/slate-300.
const TYPE_META: Array<{
  type: JobType;
  urlType: 'single' | 'collection' | 'import' | 'mail';
  barColor: string;
  icon: typeof FileText;
}> = [
  { type: 'Single file', urlType: 'single', barColor: 'bg-emerald-600', icon: FileText },
  { type: 'Multiple files', urlType: 'collection', barColor: 'bg-emerald-400', icon: Files },
  { type: 'Confluence Import', urlType: 'import', barColor: 'bg-emerald-200', icon: Import },
  { type: 'Mail', urlType: 'mail', barColor: 'bg-slate-300', icon: Mail },
];

export function ProcessingOverview() {
  const fetch = useCallback(() => fetchJobs(), []);
  // Same cache key as HomeDashboard/DocumentBrowser: a shared in-flight
  // fetch and instant last-known repaint across the app's job-consuming views.
  const jobsResource = useCachedResource(JOBS_KEY, fetch, { ttlMs: 5_000 });
  const jobs = useMemo(() => jobsResource.data ?? [], [jobsResource.data]);
  const jobsStale = Boolean(jobsResource.error);

  const stats = useMemo(() => deriveStats(jobs), [jobs]);
  const typeCounts = useMemo(() => deriveTypeCounts(jobs), [jobs]);
  const typeTotal = useMemo(
    () => TYPE_META.reduce((sum, meta) => sum + typeCounts[meta.type], 0),
    [typeCounts],
  );
  const recentJobs = useMemo(
    () =>
      [...jobs]
        .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
        .slice(0, 10),
    [jobs],
  );

  const isActive = stats.running > 0;
  useVisiblePolling(() => void jobsResource.revalidate(), isActive ? 5_000 : 30_000);

  const statCards = [
    { label: 'Running', value: stats.running, hint: 'Pending + running jobs' },
    { label: 'Finished', value: stats.finished, hint: 'Completed successfully' },
    { label: 'Failed', value: stats.failed, hint: 'Jobs with status FAILED' },
    { label: 'Pages processed', value: stats.pagesProcessed, hint: 'Sum across finished jobs' },
  ];

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-10 text-slate-950 sm:px-6 lg:px-8">
      <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="font-serif text-3xl font-semibold tracking-tight text-slate-950">Processing</h1>
          <p className="mt-1 text-slate-600">
            The team&apos;s work center — every job, import, and mail extraction.
          </p>
        </div>
      </div>

      <div className="mb-6 flex flex-wrap items-center justify-center gap-3">
        <Link href="/processing/new" className={softCtaClass}>
          <FilePlus className="h-4 w-4" />
          New File Task
        </Link>
        <Link href="/imports/new" className={softCtaClass}>
          <FileInput className="h-4 w-4" />
          New import
        </Link>
      </div>

      {jobsStale && (
        <div
          aria-live="polite"
          className="mb-4 flex items-center gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm text-amber-800"
        >
          <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500" />
          Showing last known jobs — reconnecting to the backend.
        </div>
      )}

      <section className="mb-8 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {statCards.map((item) => (
          <div
            key={item.label}
            className="rounded-2xl border border-slate-200 bg-gradient-to-br from-emerald-50 to-white p-5 shadow-[0_18px_45px_rgba(15,23,42,0.06)]"
          >
            <p className="text-sm text-slate-600">{item.label}</p>
            <p className="mt-3 text-3xl font-semibold text-slate-950">{item.value}</p>
            <p className="mt-2 text-xs text-slate-500">{item.hint}</p>
          </div>
        ))}
      </section>

      <section className="mb-8 rounded-2xl border border-slate-200 bg-white p-5">
        <h2 className="text-sm font-semibold text-slate-950">By type</h2>

        {/* Decorative — the counts it visualizes are the accessible text in
            the stat cards below, so the bar itself is hidden from AT. */}
        <div aria-hidden="true" className="mt-3 flex h-2.5 w-full overflow-hidden rounded-full bg-slate-100">
          {typeTotal === 0 ? (
            <div className="h-full w-full bg-slate-100" />
          ) : (
            TYPE_META.map((meta) => {
              const count = typeCounts[meta.type];
              if (count === 0) {
                return null;
              }
              return (
                <div
                  key={meta.type}
                  className={meta.barColor}
                  style={{ width: `${(count / typeTotal) * 100}%` }}
                />
              );
            })
          )}
        </div>

        <div className="mt-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {TYPE_META.map((meta) => {
            const Icon = meta.icon;
            return (
              <Link
                key={meta.type}
                href={`/jobs?type=${meta.urlType}`}
                className="group flex items-center justify-between gap-2 rounded-xl border border-slate-200 bg-white p-3 text-left transition hover:border-emerald-300 hover:bg-emerald-50"
              >
                <span className="flex items-center gap-2 min-w-0">
                  <Icon className="h-4 w-4 shrink-0 text-slate-400 group-hover:text-emerald-700" />
                  <span className="truncate text-sm font-medium text-slate-700 group-hover:text-emerald-900">
                    {meta.type}
                  </span>
                </span>
                <span className="shrink-0 tabular-nums text-sm font-semibold text-slate-950">
                  {typeCounts[meta.type]}
                </span>
              </Link>
            );
          })}
        </div>
      </section>

      <section className="rounded-2xl border border-slate-200 bg-white p-5">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-950">Recent jobs</h2>
          <Link href="/jobs" className="text-sm font-medium text-emerald-700 hover:text-emerald-800">
            All jobs →
          </Link>
        </div>

        {recentJobs.length === 0 ? (
          <div className="flex flex-col items-center gap-3 py-10 text-center">
            <FileText className="h-8 w-8 text-slate-300" />
            <p className="text-sm text-slate-500">No jobs yet. Start a file task or an import to see it here.</p>
            <div className="flex flex-wrap items-center justify-center gap-2">
              <Link href="/processing/new" className={softCtaSmClass}>
                <FilePlus className="h-4 w-4" />
                New File Task
              </Link>
              <Link href="/imports/new" className={softCtaSmClass}>
                <FileInput className="h-4 w-4" />
                New import
              </Link>
            </div>
          </div>
        ) : (
          <ul className="mt-4 divide-y divide-slate-100">
            {recentJobs.map((job) => (
              <li key={job.id} className="flex flex-wrap items-center justify-between gap-3 py-3">
                <div className="min-w-0 flex-1">
                  <Link
                    href={`/jobs/${job.id}`}
                    className="line-clamp-1 text-sm font-medium text-slate-950 hover:text-emerald-700"
                  >
                    {job.original_filename}
                  </Link>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {formatTimestamp(job.created_at)}
                    {job.owner?.username && <span className="text-slate-400"> · by {job.owner.username}</span>}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <span className="rounded-full bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-700">
                    {jobType(job)}
                  </span>
                  <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${statusBadge[job.status]}`}>
                    {job.status}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
