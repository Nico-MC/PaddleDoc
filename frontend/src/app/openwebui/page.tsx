'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { CircleCheck, CircleX, LoaderCircle, Pencil, PlugZap, Plus, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ApiError } from '@/lib/api';
import {
  Badge,
  ConfirmDialog,
  ErrorNotice,
  errorMessage,
  Field,
  inputClass,
  LoadingState,
  Modal,
  SectionCard,
} from '@/components/admin/admin-shared';
import {
  type OpenWebUIConnection,
  type OpenWebUIConnectionTestResponse,
  type OpenWebUIPush,
  OPENWEBUI_TEST_COOLDOWN_FALLBACK_MS,
  createOpenWebUIConnection,
  deleteOpenWebUIConnection,
  listOpenWebUIConnections,
  listOpenWebUIPushes,
  pushStatusChip,
  testOpenWebUIConnection,
  updateOpenWebUIConnection,
} from '@/lib/openwebui';

export default function OpenWebUIPage() {
  const [connections, setConnections] = useState<OpenWebUIConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  // 404 means the backend hasn't shipped this endpoint yet, not a real
  // failure (mirrors vl-connections-tab.tsx's `unavailable` state).
  const [unavailable, setUnavailable] = useState(false);

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<OpenWebUIConnection | null>(null);
  const [deleting, setDeleting] = useState<OpenWebUIConnection | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, OpenWebUIConnectionTestResponse>>({});
  // Deadline (Date.now() + window) per connection id -- test cooldown is
  // per-connection server-side, so tracked as a map rather than one timer.
  const [testCooldowns, setTestCooldowns] = useState<Record<string, number>>({});
  const [nowTick, setNowTick] = useState(() => Date.now());

  const [pushes, setPushes] = useState<OpenWebUIPush[]>([]);
  const [pushesLoading, setPushesLoading] = useState(true);
  const [pushesError, setPushesError] = useState<string | null>(null);
  const [expandedPushId, setExpandedPushId] = useState<string | null>(null);

  // Reusable for post-mutation refreshes (ConnectionModal's onSaved,
  // ConfirmDialog's onConfirm below). NOT called from the mount effect
  // (see that effect's own comment) -- react-hooks/set-state-in-effect
  // requires an effect's own body stay await-first, and a plain call to a
  // same-component function is inlined for that check regardless of
  // useCallback/deps, so the mount fetch below duplicates this logic instead
  // of calling `reload()` directly.
  const reload = useCallback(async () => {
    try {
      const data = await listOpenWebUIConnections();
      setConnections(data.items);
      setListError(null);
      setUnavailable(false);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) {
        setUnavailable(true);
        setListError(null);
      } else {
        setListError(errorMessage(err));
        setUnavailable(false);
      }
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial loads only (mirrors app/settings/page.tsx's `load` vs. its mount
  // effect): each IIFE's first statement is its `await`, not a synchronous
  // setState, per react-hooks/set-state-in-effect.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await listOpenWebUIConnections();
        if (cancelled) return;
        setConnections(data.items);
        setListError(null);
        setUnavailable(false);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setUnavailable(true);
          setListError(null);
        } else {
          setListError(errorMessage(err));
          setUnavailable(false);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await listOpenWebUIPushes({ limit: 20 });
        if (cancelled) return;
        setPushes(data.items);
        setPushesError(null);
      } catch (err) {
        if (cancelled) return;
        // A 404 here just means the same not-yet-deployed backend the
        // connections list already reports -- no separate banner needed.
        if (!(err instanceof ApiError && err.status === 404)) {
          setPushesError(errorMessage(err));
        }
      } finally {
        if (!cancelled) setPushesLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Countdown ticker for the per-connection test cooldowns; self-stops once
  // every deadline has passed (pruned inline so the effect doesn't keep an
  // interval alive forever).
  useEffect(() => {
    if (Object.keys(testCooldowns).length === 0) return;
    const timer = window.setInterval(() => {
      const now = Date.now();
      setNowTick(now);
      setTestCooldowns((current) => {
        const next: Record<string, number> = {};
        let changed = false;
        for (const [id, until] of Object.entries(current)) {
          if (until > now) next[id] = until;
          else changed = true;
        }
        return changed ? next : current;
      });
    }, 250);
    return () => window.clearInterval(timer);
  }, [testCooldowns]);

  const cooldownSecondsFor = (id: string): number => {
    const until = testCooldowns[id];
    if (until === undefined) return 0;
    return Math.max(0, Math.ceil((until - nowTick) / 1000));
  };

  const armTestCooldown = (id: string) => {
    setTestCooldowns((current) => ({ ...current, [id]: Date.now() + OPENWEBUI_TEST_COOLDOWN_FALLBACK_MS }));
  };

  const testConnection = async (id: string) => {
    setTestingId(id);
    setTestResults((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    try {
      const result = await testOpenWebUIConnectionSafe(id);
      setTestResults((prev) => ({ ...prev, [id]: result }));
      armTestCooldown(id);
    } finally {
      setTestingId(null);
    }
  };

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-6xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
        <section className="mb-6">
          <h1 className="text-3xl font-semibold">OpenWebUI</h1>
          <p className="mt-2 text-slate-600">
            Push finished documents into an OpenWebUI knowledge collection for retrieval.
          </p>
        </section>

        <SectionCard
          title="Connections"
          description="OpenWebUI instances used as push targets. Connections are private to you; stored API keys are never displayed."
          actions={
            <Button size="sm" onClick={() => setCreating(true)}>
              <Plus className="h-4 w-4" />
              Add connection
            </Button>
          }
        >
          <ErrorNotice message={listError} />
          {unavailable && (
            <div className="mb-4 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
              OpenWebUI push is not available on this backend yet. This page starts showing data automatically
              once the endpoint is deployed.
            </div>
          )}
          {loading ? (
            <LoadingState label="Loading connections..." />
          ) : unavailable ? null : connections.length === 0 ? (
            <p className="py-8 text-center text-sm text-slate-500">
              No OpenWebUI connections configured. Add one to start pushing documents.
            </p>
          ) : (
            <ul className="space-y-4">
              {connections.map((connection) => (
                <li key={connection.id} className="rounded-xl border border-slate-200 p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-semibold text-slate-950">{connection.name}</span>
                        <Badge tone={connection.has_api_key ? 'emerald' : 'amber'}>
                          {connection.has_api_key ? 'API key set' : 'No API key'}
                        </Badge>
                      </div>
                      <dl className="mt-2 space-y-1 text-xs text-slate-500">
                        <div className="flex gap-2">
                          <dt className="w-16 flex-shrink-0 font-medium">Base URL</dt>
                          <dd className="break-all">{connection.base_url}</dd>
                        </div>
                        <div className="flex gap-2">
                          <dt className="w-16 flex-shrink-0 font-medium">Created</dt>
                          <dd>{new Date(connection.created_at).toLocaleDateString()}</dd>
                        </div>
                      </dl>
                    </div>
                    <div className="flex flex-shrink-0 items-center gap-1">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => void testConnection(connection.id)}
                        disabled={testingId !== null || cooldownSecondsFor(connection.id) > 0}
                      >
                        {testingId === connection.id ? (
                          <LoaderCircle className="h-4 w-4 animate-spin" />
                        ) : (
                          <PlugZap className="h-4 w-4" />
                        )}
                        {cooldownSecondsFor(connection.id) > 0
                          ? `Test (${cooldownSecondsFor(connection.id)}s)`
                          : 'Test'}
                      </Button>
                      <button
                        onClick={() => setEditing(connection)}
                        aria-label={`Edit ${connection.name}`}
                        title="Edit"
                        className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-50 hover:text-slate-700"
                      >
                        <Pencil className="h-4 w-4" />
                      </button>
                      <button
                        onClick={() => setDeleting(connection)}
                        aria-label={`Delete ${connection.name}`}
                        title="Delete"
                        className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-red-50 hover:text-red-600"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </div>
                  </div>
                  {testResults[connection.id] && <TestResult result={testResults[connection.id]} />}
                </li>
              ))}
            </ul>
          )}
        </SectionCard>

        <div className="mt-6">
          <SectionCard title="Recent pushes" description="Your most recent OpenWebUI push attempts, newest first.">
            <ErrorNotice message={pushesError} />
            {pushesLoading ? (
              <LoadingState label="Loading pushes..." />
            ) : pushes.length === 0 ? (
              <p className="py-8 text-center text-sm text-slate-500">No pushes yet.</p>
            ) : (
              <ul className="divide-y divide-slate-100">
                {pushes.map((push) => (
                  <li key={push.id} className="py-3">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="min-w-0">
                        <Link
                          href={`/jobs/${push.job_id}`}
                          className="truncate text-sm font-medium text-slate-950 hover:text-emerald-700"
                        >
                          {push.knowledge_name}
                        </Link>
                        <p className="mt-0.5 text-xs text-slate-500">
                          via {push.connection_name} · {new Date(push.created_at).toLocaleString()}
                          {push.content_stale && (
                            <span className="ml-2 font-medium text-amber-700">Content changed since last push</span>
                          )}
                        </p>
                      </div>
                      <div className="flex shrink-0 items-center gap-2">
                        <span className={`rounded px-2 py-1 text-xs ${pushStatusChip[push.status]}`}>{push.status}</span>
                        {push.error_message && (
                          <button
                            type="button"
                            onClick={() => setExpandedPushId((current) => (current === push.id ? null : push.id))}
                            className="text-xs text-emerald-700 hover:text-emerald-800"
                          >
                            {expandedPushId === push.id ? 'Hide error' : 'Show error'}
                          </button>
                        )}
                      </div>
                    </div>
                    {expandedPushId === push.id && push.error_message && (
                      <p className="mt-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                        {push.error_message}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </SectionCard>
        </div>

        {creating && (
          <ConnectionModal
            onClose={() => setCreating(false)}
            onSaved={async () => {
              setCreating(false);
              await reload();
            }}
          />
        )}

        {editing && (
          <ConnectionModal
            connection={editing}
            onClose={() => setEditing(null)}
            onSaved={async () => {
              setEditing(null);
              await reload();
            }}
          />
        )}

        {deleting && (
          <ConfirmDialog
            title="Delete OpenWebUI connection"
            body={
              <p>
                Delete <span className="font-semibold text-slate-950">{deleting.name}</span>? Past pushes keep
                their history.
              </p>
            }
            confirmLabel="Delete connection"
            onClose={() => setDeleting(null)}
            onConfirm={async () => {
              await deleteOpenWebUIConnection(deleting.id);
              setDeleting(null);
              await reload();
            }}
          />
        )}
      </div>
    </main>
  );
}

/** testOpenWebUIConnection, but never throws -- a failed probe (incl. a 429 cooldown hit) is itself a result to render, not an error state. */
async function testOpenWebUIConnectionSafe(id: string): Promise<OpenWebUIConnectionTestResponse> {
  try {
    return await testOpenWebUIConnection(id);
  } catch (err) {
    return { ok: false, detail: errorMessage(err) };
  }
}

function TestResult({ result }: { result: OpenWebUIConnectionTestResponse }) {
  return (
    <div
      className={`mt-3 rounded-xl border px-4 py-3 text-sm ${
        result.ok ? 'border-emerald-200 bg-emerald-50 text-emerald-800' : 'border-red-200 bg-red-50 text-red-700'
      }`}
    >
      <div className="flex items-center gap-2 font-medium">
        {result.ok ? <CircleCheck className="h-4 w-4 flex-shrink-0" /> : <CircleX className="h-4 w-4 flex-shrink-0" />}
        {result.ok ? 'Connection successful' : 'Connection failed'}
      </div>
      {result.detail && <p className="mt-1 text-xs">{result.detail}</p>}
    </div>
  );
}

/** Create (no `connection`) or edit (with `connection`) an OpenWebUI connection. */
function ConnectionModal({
  connection,
  onClose,
  onSaved,
}: {
  connection?: OpenWebUIConnection;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const isEdit = connection !== undefined;

  const [name, setName] = useState(connection?.name ?? '');
  const [baseUrl, setBaseUrl] = useState(connection?.base_url ?? '');
  const [apiKey, setApiKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      if (isEdit) {
        await updateOpenWebUIConnection(connection.id, {
          name: name.trim(),
          base_url: baseUrl.trim(),
          ...(apiKey ? { api_key: apiKey } : {}),
        });
      } else {
        await createOpenWebUIConnection({ name: name.trim(), base_url: baseUrl.trim(), api_key: apiKey });
      }
      await onSaved();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal title={isEdit ? `Edit ${connection.name}` : 'Add OpenWebUI connection'} onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <Field label="Name">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className={inputClass}
            required
            autoFocus
          />
        </Field>
        <Field label="Base URL" hint="The OpenWebUI instance root, e.g. https://openwebui.example.com">
          <input
            type="url"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            className={inputClass}
            required
            placeholder="https://openwebui.example.com"
          />
        </Field>
        <Field label="API key" hint={isEdit ? 'Leave blank to keep the stored key.' : undefined}>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            className={inputClass}
            required={!isEdit}
            placeholder={isEdit ? 'unchanged unless filled' : undefined}
            autoComplete="new-password"
            data-1p-ignore
            data-lpignore="true"
          />
        </Field>
        <ErrorNotice message={error} />
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="outline" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" size="sm" disabled={busy}>
            {busy && <LoaderCircle className="h-4 w-4 animate-spin" />}
            {isEdit ? 'Save changes' : 'Add connection'}
          </Button>
        </div>
      </form>
    </Modal>
  );
}
