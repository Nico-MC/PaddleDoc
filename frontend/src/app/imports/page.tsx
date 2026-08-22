'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { FileInput, FilePlus, Inbox, LoaderCircle, RefreshCcw, RotateCcw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ApiError, apiJson } from '@/lib/api';
import { formatBytes } from '@/components/dashboard/shared';
import { type ImportRun, type ImportRunListResponse, isRunActive, runStatusChip, runTitle } from '@/lib/imports';

export default function ImportsPage() {
  const [runs, setRuns] = useState<ImportRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [reloadNonce, setReloadNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const runsPayload = await apiJson<ImportRunListResponse>('/api/v1/import/runs', { cache: 'no-store' });
        if (cancelled) return;
        setRuns(runsPayload.items);
        setLoadError(null);
      } catch (error) {
        if (!cancelled) {
          setLoadError(error instanceof ApiError ? error.detail : 'Failed to load imports.');
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [reloadNonce]);

  const loadAll = () => {
    setLoading(true);
    setReloadNonce((nonce) => nonce + 1);
  };

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-6xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
        <section className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-3xl font-semibold">Confluence Imports</h1>
            <p className="mt-2 text-slate-600">
              Import runs pull Confluence pages into PaddleDoc as markdown jobs.
            </p>
          </div>
          <Button variant="outline" onClick={() => void loadAll()} disabled={loading}>
            <RefreshCcw className="mr-2 h-4 w-4" /> Refresh
          </Button>
        </section>

        <section className="mb-6 flex flex-wrap items-center justify-center gap-3">
          <Link href="/processing/new">
            <Button variant="outline" className="border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-100 hover:text-emerald-900">
              <FilePlus className="mr-2 h-4 w-4" /> New File Task
            </Button>
          </Link>
          <Link href="/imports/new">
            <Button variant="outline" className="border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-100 hover:text-emerald-900">
              <FileInput className="mr-2 h-4 w-4" /> New import
            </Button>
          </Link>
        </section>

        {loadError && (
          <p role="alert" className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {loadError}
          </p>
        )}

        <section className="mb-6 rounded-3xl border border-slate-200 bg-white p-4 sm:p-5">
          <div className="mb-3 flex items-center justify-between gap-4">
            <h2 className="text-lg font-semibold">Runs</h2>
            <p className="text-sm text-slate-500">{runs.length} run(s)</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full table-auto text-left text-xs sm:text-sm">
              <thead className="text-slate-500">
                <tr>
                  <th className="pb-2 font-medium">Import</th>
                  <th className="pb-2 font-medium">Status</th>
                  <th className="pb-2 font-medium">Pages</th>
                  <th className="hidden pb-2 font-medium sm:table-cell">Attachments</th>
                  <th className="hidden pb-2 font-medium md:table-cell">Size</th>
                  <th className="hidden pb-2 font-medium md:table-cell">Created</th>
                  <th className="pb-2 font-medium" aria-label="Actions" />
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id} className="border-t border-slate-100">
                    <td className="py-3">
                      <Link href={`/imports/${run.id}`} className="line-clamp-2 font-medium text-slate-950 hover:text-emerald-700">
                        {runTitle(run)}
                      </Link>
                      <p className="mt-1 text-xs text-slate-500">
                        {run.scope_type === 'space' ? `Space key: ${run.scope_value}` : `Page id: ${run.scope_value}`}
                        {run.owner ? ` · ${run.owner.username}` : ''}
                      </p>
                    </td>
                    <td className="py-3">
                      <span className={`rounded px-2 py-1 text-xs ${runStatusChip[run.status]}`}>{run.status}</span>
                    </td>
                    <td className="py-3 text-slate-700">
                      {run.pages_imported} / {run.pages_discovered}
                      {run.pages_failed > 0 && <span className="ml-1 text-xs text-red-600">({run.pages_failed} failed)</span>}
                    </td>
                    <td className="hidden py-3 text-slate-700 sm:table-cell">{run.attachments_saved}</td>
                    <td className="hidden py-3 text-slate-700 md:table-cell">
                      {formatBytes(run.artifact_bytes + run.content_bytes)}
                    </td>
                    <td className="hidden py-3 text-slate-700 md:table-cell">{new Date(run.created_at).toLocaleString()}</td>
                    <td className="py-3 text-right">
                      {!isRunActive(run.status) && (
                        <Link
                          href={`/imports/new?from=${run.id}`}
                          title="Edit & run again"
                          aria-label="Edit & run again"
                          className="inline-flex items-center rounded p-1.5 text-slate-500 hover:bg-slate-100 hover:text-emerald-700"
                        >
                          <RotateCcw className="h-4 w-4" />
                        </Link>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {runs.length === 0 && !loading && (
              <div className="flex flex-col items-center gap-3 py-10 text-center">
                <Inbox className="h-8 w-8 text-slate-300" />
                <p className="text-sm text-slate-500">No import runs yet. Start your first import to see it here.</p>
                <div className="flex flex-wrap items-center justify-center gap-2">
                  <Link href="/processing/new">
                    <Button variant="outline" size="sm" className="border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-100 hover:text-emerald-900">
                      <FilePlus className="h-4 w-4" />
                      New File Task
                    </Button>
                  </Link>
                  <Link href="/imports/new">
                    <Button variant="outline" size="sm" className="border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-100 hover:text-emerald-900">
                      <FileInput className="h-4 w-4" />
                      New import
                    </Button>
                  </Link>
                </div>
              </div>
            )}
            {loading && (
              <div className="flex items-center gap-2 py-6 text-sm text-slate-600">
                <LoaderCircle className="h-4 w-4 animate-spin" /> Loading imports...
              </div>
            )}
          </div>
        </section>

        <p className="text-sm text-slate-500">
          Connections are managed under{' '}
          <Link href="/connections?tab=confluence" className="text-emerald-700 hover:text-emerald-800">
            Connections &gt; Confluence
          </Link>
          .
        </p>
      </div>
    </main>
  );
}
