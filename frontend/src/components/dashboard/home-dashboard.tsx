'use client';

import { memo, useCallback, useMemo } from 'react';
import Link from 'next/link';
import { Check, ChevronDown, FileInput, FilePlus, Inbox } from 'lucide-react';

import { buttonVariants } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { apiFetch } from '@/lib/api';
import { useAuth } from '@/lib/auth-context';
import { useCachedResource, useVisiblePolling } from '@/lib/data-cache';
import {
  type ContainerState,
  type DashboardStats,
  type Job,
  type JobStatus,
  type PaddleIndicator,
  type PaddleStatusResponse,
  type RuntimeCapabilityInfo,
  type UIState,
  formatBytes,
} from './shared';


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
const STATS_KEY = '/api/v1/stats';
const PADDLE_STATUS_KEY = '/api/v1/paddle/status';

// Mirrors document-browser.tsx's / processing-overview.tsx's statusBadge
// palette (kept local per task instructions — this component must not
// import either of those files).
const statusBadge: Record<JobStatus, string> = {
  PENDING: 'bg-slate-100 text-slate-700',
  RUNNING: 'bg-emerald-100 text-emerald-800',
  FINISHED: 'bg-emerald-100 text-emerald-800',
  FAILED: 'bg-red-600/20 text-red-700 font-semibold',
};

function deriveUiState(jobs: Job[]): UIState {
  if (jobs.some((job) => job.status === 'RUNNING' || job.status === 'PENDING')) {
    return 'Processing';
  }
  if (jobs.some((job) => job.status === 'FINISHED')) {
    return 'Finished';
  }
  return 'Idle';
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

type ServiceSnapshot = {
  paddleStatus: PaddleIndicator;
  paddleStatusDetail: string | null;
  pendingJobs: number;
  runningJobs: number;
  queueTotal: number;
  runningWorkers: number;
  workerNodes: string[];
  containerStates: ContainerState[];
  runtimeCapability: RuntimeCapabilityInfo | null;
  /** True when the latest paddle-status revalidate failed — the fields above are last-known-good, not confirmed current. */
  isUnreachable: boolean;
};

const initialSnapshot: ServiceSnapshot = {
  paddleStatus: 'stopped',
  paddleStatusDetail: null,
  pendingJobs: 0,
  runningJobs: 0,
  queueTotal: 0,
  runningWorkers: 0,
  workerNodes: [],
  containerStates: [],
  runtimeCapability: null,
  isUnreachable: false,
};

/** Dot color for a compact status indicator: red once the backend is unreachable, otherwise `ok`/`warn`/neutral. */
function indicatorDot(isUnreachable: boolean, tone: 'ok' | 'warn' | 'neutral'): string {
  if (isUnreachable) return 'bg-red-500';
  if (tone === 'ok') return 'bg-emerald-500';
  if (tone === 'warn') return 'bg-amber-500';
  return 'bg-slate-300';
}

const StatusBar = memo(function StatusBar({
  uiState,
  jobsStale,
  service,
}: {
  uiState: UIState;
  jobsStale: boolean;
  service: ServiceSnapshot;
}) {
  const {
    paddleStatus,
    paddleStatusDetail,
    pendingJobs,
    runningJobs,
    queueTotal,
    runningWorkers,
    workerNodes,
    containerStates,
    runtimeCapability,
    isUnreachable,
  } = service;

  const hasDetails = containerStates.length > 0 || workerNodes.length > 0 || Boolean(runtimeCapability);

  return (
    <section
      className={`rounded-xl border px-4 py-3 text-sm ${
        isUnreachable ? 'border-red-200 bg-red-50' : 'border-slate-200 bg-white'
      }`}
    >
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <div className="flex items-center gap-2">
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${indicatorDot(isUnreachable, uiState === 'Idle' ? 'neutral' : 'ok')}`} />
          <span className="text-xs uppercase tracking-wide text-slate-500">Status</span>
          <span className="font-medium text-slate-800">{isUnreachable ? 'Unreachable' : uiState}</span>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`h-1.5 w-1.5 shrink-0 rounded-full ${indicatorDot(isUnreachable, paddleStatus === 'running' ? 'ok' : paddleStatus === 'failed' ? 'warn' : 'neutral')}`}
          />
          <span className="text-xs uppercase tracking-wide text-slate-500">Paddle service</span>
          <span className="font-medium text-slate-800">{isUnreachable ? 'unreachable' : paddleStatus}</span>
        </div>
        <div className="flex items-center gap-2">
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${indicatorDot(isUnreachable, queueTotal > 0 ? 'warn' : 'neutral')}`} />
          <span className="text-xs uppercase tracking-wide text-slate-500">Queue</span>
          <span className="font-medium text-slate-800">
            {queueTotal} <span className="text-slate-500">(pending {pendingJobs}, running {runningJobs})</span>
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${indicatorDot(isUnreachable, runningWorkers > 0 ? 'ok' : 'neutral')}`} />
          <span className="text-xs uppercase tracking-wide text-slate-500">Containers</span>
          <span className="font-medium text-slate-800">{runningWorkers} running</span>
        </div>
      </div>

      <div aria-live="polite">
        {isUnreachable && (
          <p className="mt-2 text-xs leading-5 text-red-700">
            The backend did not respond to the last health check. Figures above reflect the last successful check, not
            the current state.
            {jobsStale && ' Job list is last known — reconnecting.'}
          </p>
        )}
      </div>

      {hasDetails && (
        <details className="mt-2 text-xs text-slate-500">
          <summary className="inline-flex cursor-pointer select-none list-none items-center gap-1 text-slate-500 hover:text-slate-700 [&::-webkit-details-marker]:hidden">
            <ChevronDown className="h-3.5 w-3.5" />
            Details
          </summary>
          <div className="mt-2 space-y-2 border-t border-slate-100 pt-2">
            {containerStates.length > 0 && (
              <div>
                <p className="font-medium text-slate-600">Containers</p>
                <div className="mt-1 space-y-0.5">
                  {containerStates.map((entry) => (
                    <p key={entry.name}>
                      {entry.name}: {entry.state}
                      {entry.detail ? ` (${entry.detail})` : ''}
                    </p>
                  ))}
                </div>
              </div>
            )}
            {workerNodes.length > 0 && (
              <div>
                <p className="font-medium text-slate-600">Worker nodes</p>
                <p className="mt-1">{workerNodes.join(', ')}</p>
              </div>
            )}
            {paddleStatusDetail && !isUnreachable && <p>{paddleStatusDetail}</p>}
            {runtimeCapability && (
              <p>
                {runtimeCapability.cuda_available
                  ? 'GPU available for accelerated processing'
                  : 'PaddleOCR runtime is configured for CPU execution in this deployment'}
              </p>
            )}
          </div>
        </details>
      )}
    </section>
  );
});

const StatsLine = memo(function StatsLine({ stats, isStale }: { stats: DashboardStats | null; isStale: boolean }) {
  return (
    <div className="mb-6">
      <p className="text-sm tabular-nums text-slate-500">
        {stats ? stats.processed_documents.toLocaleString() : '...'} documents ·{' '}
        {stats ? stats.processed_pages.toLocaleString() : '...'} pages processed · Database{' '}
        {stats ? formatBytes(stats.database_size_bytes) : '...'}
      </p>
      <div aria-live="polite">
        {isStale && (
          <p className="mt-1 flex items-center gap-2 text-xs text-amber-700">
            <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-amber-500" />
            Showing last known stats — reconnecting to the backend.
          </p>
        )}
      </div>
    </div>
  );
});

const NeedsAttention = memo(function NeedsAttention({
  jobs,
  userId,
  errorCount,
}: {
  jobs: Job[];
  userId?: string;
  errorCount: number | null;
}) {
  // Own failures surface first (job.owner is read defensively — see
  // shared.ts's Job.owner doc comment); once a backend response predates the
  // owner rollout every job.owner is undefined, so `own` is simply empty and
  // this reduces to "all failed jobs" as instructed.
  const failedJobs = useMemo(() => {
    const failed = jobs.filter((job) => job.status === 'FAILED');
    const own = failed.filter((job) => job.owner?.id === userId);
    const others = failed.filter((job) => job.owner?.id !== userId);
    return [...own, ...others].slice(0, 5);
  }, [jobs, userId]);

  return (
    <section className="mb-8 rounded-2xl border border-slate-200 bg-white p-5">
      <div className="flex items-center gap-2">
        <h2 className="text-lg font-semibold text-slate-950">Needs attention</h2>
        {Boolean(errorCount) && (
          <span className="rounded-full bg-red-600/20 px-2 py-0.5 text-xs font-semibold text-red-700">
            {errorCount}
          </span>
        )}
      </div>

      {failedJobs.length === 0 ? (
        <p className="mt-3 flex items-center gap-2 text-sm text-slate-500">
          <Check className="h-4 w-4 text-emerald-600" />
          Nothing needs your attention.
        </p>
      ) : (
        <ul className="mt-4 divide-y divide-slate-100">
          {failedJobs.map((job) => (
            <li key={job.id} className="rounded-lg bg-red-50/60 px-2 py-3">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <Link
                  href={`/jobs/${job.id}`}
                  className="line-clamp-1 text-sm font-medium text-slate-950 hover:text-emerald-700"
                >
                  {job.original_filename}
                </Link>
                <span className={`shrink-0 rounded-full px-2.5 py-1 text-xs ${statusBadge[job.status]}`}>
                  {job.status}
                </span>
              </div>
              {job.error_message && (
                <p className="mt-1 line-clamp-1 text-xs text-red-700">{job.error_message}</p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
});

const RecentJobs = memo(function RecentJobs({ jobs, title }: { jobs: Job[]; title: string }) {
  const recentJobs = useMemo(
    () => [...jobs].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()).slice(0, 5),
    [jobs],
  );

  return (
    <section className="mb-8 rounded-2xl border border-slate-200 bg-white p-5">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold text-slate-950">{title}</h2>
        <Link href="/jobs" className="text-sm font-medium text-emerald-700 hover:text-emerald-800">
          All jobs →
        </Link>
      </div>

      {recentJobs.length === 0 ? (
        <div className="mt-4 flex flex-col items-center gap-3 py-6 text-center">
          <Inbox className="h-8 w-8 text-slate-300" />
          <p className="text-sm text-slate-500">No jobs yet — start a file task or an import to see it here.</p>
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
            <li
              key={job.id}
              className={`flex flex-wrap items-center justify-between gap-3 rounded-lg py-3 ${
                job.status === 'FAILED' ? 'bg-red-50/60 px-2' : ''
              }`}
            >
              <div className="min-w-0 flex-1">
                <Link
                  href={`/jobs/${job.id}`}
                  className="line-clamp-1 text-sm font-medium text-slate-950 hover:text-emerald-700"
                >
                  {job.original_filename}
                </Link>
                <p className="mt-0.5 text-xs text-slate-500">{formatTimestamp(job.created_at)}</p>
              </div>
              <span className={`rounded-full px-2.5 py-1 text-xs ${statusBadge[job.status]}`}>{job.status}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
});

async function fetchJson<T>(path: string): Promise<T> {
  const response = await apiFetch(path, { cache: 'no-store' });
  if (!response.ok) {
    throw new Error(`Request to ${path} failed with status ${response.status}`);
  }
  return (await response.json()) as T;
}

export function HomeDashboard() {
  const { user } = useAuth();
  const fetchJobs = useCallback(async () => {
    const payload = await fetchJson<{ items?: Job[] }>(JOBS_KEY);
    return (payload.items ?? []) as Job[];
  }, []);
  const fetchStats = useCallback(() => fetchJson<DashboardStats>(STATS_KEY), []);
  const fetchPaddleStatus = useCallback(() => fetchJson<PaddleStatusResponse>(PADDLE_STATUS_KEY), []);

  // ttlMs is short: these views are cheap to refetch and jobs/stats/status
  // can change under a running pipeline, so a mount should rarely trust a
  // value older than one polling tick — the cache's job here is instant
  // repaint on remount + shared in-flight dedupe, not long-lived staleness.
  const jobsResource = useCachedResource(JOBS_KEY, fetchJobs, { ttlMs: 5_000 });
  const statsResource = useCachedResource(STATS_KEY, fetchStats, { ttlMs: 10_000 });
  const paddleStatusResource = useCachedResource(PADDLE_STATUS_KEY, fetchPaddleStatus, { ttlMs: 10_000 });

  const jobs = jobsResource.data ?? [];
  const stats = statsResource.data ?? null;
  const uiState = deriveUiState(jobs);
  const isActive = uiState === 'Processing';

  // A failed revalidate leaves the cache (and therefore `.data`) holding the
  // last-known-good payload on purpose — that's the whole point of the
  // stale-while-revalidate cache. But this panel reports live service
  // health, so it must not silently keep presenting that stale payload as
  // current: `.error` tells us the most recent check actually failed, and
  // the panel below degrades honestly instead of freezing on the last good
  // reading.
  const paddleUnreachable = Boolean(paddleStatusResource.error);
  const jobsStale = Boolean(jobsResource.error);

  // owner rolled out on the jobs list response alongside this change (see
  // shared.ts's Job.owner doc comment) — until every job in the current
  // payload carries the field, treat ownership as unknown and show
  // everyone's jobs rather than filtering down to a false-empty list.
  const hasOwnerData = jobs.some((job) => job.owner !== undefined);
  const ownJobs = hasOwnerData ? jobs.filter((job) => job.owner?.id === user?.id) : jobs;
  const recentJobsTitle = hasOwnerData ? 'Your recent jobs' : 'Recent jobs';

  const service = useMemo<ServiceSnapshot>(() => {
    const payload = paddleStatusResource.data;
    if (!payload) {
      return { ...initialSnapshot, isUnreachable: paddleUnreachable };
    }
    const reportedContainers = payload.containers ?? [];
    const hasFrontend = reportedContainers.some((entry) => entry.name === 'frontend');
    const containerStates: ContainerState[] = hasFrontend
      ? reportedContainers.map((entry) =>
          entry.name === 'frontend'
            ? { ...entry, state: 'running', detail: 'Served in current browser session' }
            : entry,
        )
      : [
          { name: 'frontend', state: 'running', detail: 'Served in current browser session' },
          ...reportedContainers,
        ];
    return {
      paddleStatus: payload.status ?? 'failed',
      paddleStatusDetail: payload.detail ?? null,
      pendingJobs: payload.pending_jobs ?? 0,
      runningJobs: payload.running_jobs ?? 0,
      queueTotal: payload.queue_total ?? 0,
      runningWorkers: payload.running_workers ?? 0,
      workerNodes: payload.worker_nodes ?? [],
      containerStates,
      runtimeCapability: payload.runtime ?? null,
      isUnreachable: paddleUnreachable,
    };
  }, [paddleStatusResource.data, paddleUnreachable]);

  // Poll briskly while a job is actually queued/running, back off to a slow
  // heartbeat once idle, and pause outright while the tab is hidden — no
  // point re-rendering a dashboard nobody is looking at.
  useVisiblePolling(() => void jobsResource.revalidate(), isActive ? 5_000 : 30_000);
  useVisiblePolling(() => void statsResource.revalidate(), isActive ? 15_000 : 60_000);
  useVisiblePolling(() => void paddleStatusResource.revalidate(), isActive ? 10_000 : 45_000);

  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-10 text-slate-950 sm:px-6 lg:px-8">
      <div className="mb-1 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight text-slate-950">
            Welcome back{user?.username ? `, ${user.username}` : ''}
          </h1>
          <p className="mt-1 text-sm text-slate-500">Here&apos;s what needs your attention and what you worked on recently.</p>
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
        <Link href="/jobs" className={buttonVariants({ variant: 'outline' })}>
          Browse jobs
        </Link>
      </div>

      <StatsLine stats={stats} isStale={statsResource.isStale} />
      <NeedsAttention jobs={jobs} userId={user?.id} errorCount={stats?.errors ?? null} />
      <RecentJobs jobs={ownJobs} title={recentJobsTitle} />
      <StatusBar uiState={uiState} jobsStale={jobsStale} service={service} />
    </div>
  );
}
