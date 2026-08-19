'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { LoaderCircle, Plus, RefreshCcw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ApiError, apiJson } from '@/lib/api';
import { formatBytes } from '@/components/dashboard/shared';
import { type ImportRun, type ImportRunListResponse, runStatusChip, runTitle } from '@/lib/imports';

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
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => void loadAll()} disabled={loading}>
              <RefreshCcw className="mr-2 h-4 w-4" /> Refresh
            </Button>
            <Link href="/imports/new">
              <Button>
                <Plus className="mr-2 h-4 w-4" /> New import
              </Button>
            </Link>
          </div>
        </section>

        {loadError && (
          <p className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{loadError}</p>
        )}

        <section className="mb-6 rounded-3xl border border-slate-200 bg-white p-4 sm:p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
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
                  </tr>
                ))}
              </tbody>
            </table>
            {runs.length === 0 && !loading && (
              <p className="py-6 text-sm text-slate-600">
                No import runs yet. Start one with the New import button.
              </p>
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
