'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { CircleCheck, CircleX, LoaderCircle, Pencil, PlugZap, Plus, Trash2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ApiError, apiJson } from '@/lib/api';
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
  Toggle,
  apiSend,
} from '@/components/admin/admin-shared';
import {
  REFRESH_INTERVAL_OPTIONS,
  TEST_COOLDOWN_FALLBACK_MS,
  formatRefreshInterval,
  type ImportAuthType,
  type ImportSource,
  type ImportSourceListResponse,
  type ImportSourceTestResponse,
} from '@/lib/imports';

export function ConfluenceConnectionsTab() {
  const [sources, setSources] = useState<ImportSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  // 404 means the backend hasn't shipped this endpoint yet, not a real
  // failure (mirrors vl-connections-tab.tsx's `unavailable` state).
  const [unavailable, setUnavailable] = useState(false);

  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<ImportSource | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, ImportSourceTestResponse>>({});
  // Deadline (Date.now() + window) per source id -- the server enforces the
  // cooldown per source, so tracked as a map rather than one global timer.
  const [testCooldowns, setTestCooldowns] = useState<Record<string, number>>({});
  const [nowTick, setNowTick] = useState(() => Date.now());

  // Reusable for post-mutation refreshes (create/delete). NOT called from the
  // mount effect below -- react-hooks/set-state-in-effect requires an
  // effect's own body stay await-first, so the mount fetch duplicates this
  // logic instead of calling `reload()` directly (mirrors openwebui-connections-tab.tsx).
  const reload = useCallback(async () => {
    try {
      const data = await apiJson<ImportSourceListResponse>('/api/v1/import/sources', { cache: 'no-store' });
      setSources(data.items);
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

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await apiJson<ImportSourceListResponse>('/api/v1/import/sources', { cache: 'no-store' });
        if (cancelled) return;
        setSources(data.items);
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

  // Countdown ticker for the per-source test cooldowns; self-stops once every
  // deadline has passed.
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
    setTestCooldowns((current) => ({ ...current, [id]: Date.now() + TEST_COOLDOWN_FALLBACK_MS }));
  };

  const testSource = async (source: ImportSource) => {
    setTestingId(source.id);
    setTestResults((prev) => {
      const next = { ...prev };
      delete next[source.id];
      return next;
    });
    try {
      const result = await apiJson<ImportSourceTestResponse>(`/api/v1/import/sources/${source.id}/test`, {
        method: 'POST',
      });
      setTestResults((prev) => ({ ...prev, [source.id]: result }));
      armTestCooldown(source.id);
      if (result.ok && result.server_kind) {
        setSources((current) =>
          current.map((entry) =>
            entry.id === source.id
              ? { ...entry, server_kind: result.server_kind ?? entry.server_kind, last_validated_at: new Date().toISOString() }
              : entry,
          ),
        );
      }
    } catch (err) {
      // A 429 here is itself the per-source cooldown, not a fresh failure --
      // still arm the client-side timer so the button re-enables in step
      // with the server (matches imports/new/page.tsx's testConnection).
      if (err instanceof ApiError && err.status === 429) {
        armTestCooldown(source.id);
      }
      setTestResults((prev) => ({ ...prev, [source.id]: { ok: false, detail: errorMessage(err), server_kind: null } }));
    } finally {
      setTestingId(null);
    }
  };

  const startRename = (source: ImportSource) => {
    setRenamingId(source.id);
    setRenameValue(source.name);
    setActionError(null);
  };

  const saveRename = async (source: ImportSource) => {
    const name = renameValue.trim();
    if (!name) {
      setActionError('Name cannot be empty.');
      return;
    }
    setBusyId(source.id);
    try {
      const updated = await apiJson<ImportSource>(`/api/v1/import/sources/${source.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      setSources((current) => current.map((entry) => (entry.id === source.id ? updated : entry)));
      setRenamingId(null);
      setActionError(null);
    } catch (err) {
      setActionError(errorMessage(err));
    } finally {
      setBusyId(null);
    }
  };

  const updateRefresh = async (
    source: ImportSource,
    patch: { refresh_enabled?: boolean; refresh_interval_seconds?: number },
  ) => {
    setBusyId(source.id);
    try {
      const updated = await apiJson<ImportSource>(`/api/v1/import/sources/${source.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });
      setSources((current) => current.map((entry) => (entry.id === source.id ? updated : entry)));
      setActionError(null);
    } catch (err) {
      setActionError(errorMessage(err));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="space-y-6">
      <SectionCard
        title="Confluence connections"
        description="Connections used to import Confluence pages. Connections are private to you; stored credentials are write-only and never displayed."
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
            Confluence import is not available on this backend yet. This page starts showing data automatically
            once the endpoint is deployed.
          </div>
        )}
        {!unavailable && actionError && <ErrorNotice message={actionError} />}
        {loading ? (
          <LoadingState label="Loading connections..." />
        ) : unavailable ? null : sources.length === 0 ? (
          <p className="py-8 text-center text-sm text-slate-500">
            No Confluence connections configured. Add one to start importing pages.
          </p>
        ) : (
          <ul className="space-y-4">
            {sources.map((source) => (
              <li key={source.id} className="rounded-xl border border-slate-200 p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    {renamingId === source.id ? (
                      <div className="flex flex-wrap items-center gap-2">
                        <input
                          value={renameValue}
                          onChange={(event) => setRenameValue(event.target.value)}
                          aria-label="Connection name"
                          onKeyDown={(event) => {
                            if (event.key === 'Enter') void saveRename(source);
                            if (event.key === 'Escape') setRenamingId(null);
                          }}
                          className={inputClass}
                          autoFocus
                        />
                        <Button size="sm" onClick={() => void saveRename(source)} disabled={busyId === source.id}>
                          Save
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => setRenamingId(null)}>
                          Cancel
                        </Button>
                      </div>
                    ) : (
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-semibold text-slate-950">{source.name}</span>
                        <span
                          className={`rounded px-2 py-0.5 text-xs ${
                            source.server_kind ? 'bg-emerald-100 text-emerald-800' : 'bg-slate-100 text-slate-600'
                          }`}
                        >
                          {source.server_kind === 'cloud'
                            ? 'Cloud'
                            : source.server_kind === 'datacenter'
                              ? 'Server/DC'
                              : 'untested'}
                        </span>
                        <Badge tone="slate">
                          {source.auth_type === 'cloud_basic' ? 'Email + API token' : 'Personal access token'}
                        </Badge>
                      </div>
                    )}
                    <dl className="mt-2 space-y-1 text-xs text-slate-500">
                      <div className="flex gap-2">
                        <dt className="w-16 flex-shrink-0 font-medium">Base URL</dt>
                        <dd className="break-all">{source.base_url}</dd>
                      </div>
                      <div className="flex gap-2">
                        <dt className="w-16 flex-shrink-0 font-medium">Created</dt>
                        <dd>{new Date(source.created_at).toLocaleDateString()}</dd>
                      </div>
                    </dl>
                    <div className="mt-3 flex flex-wrap items-center gap-3">
                      <Toggle
                        checked={source.refresh_enabled ?? false}
                        onChange={(next) => void updateRefresh(source, { refresh_enabled: next })}
                        label="Auto-refresh"
                        disabled={busyId === source.id}
                      />
                      <select
                        value={source.refresh_interval_seconds ?? REFRESH_INTERVAL_OPTIONS[2].value}
                        onChange={(event) =>
                          void updateRefresh(source, { refresh_interval_seconds: Number(event.target.value) })
                        }
                        disabled={!(source.refresh_enabled ?? false) || busyId === source.id}
                        className="rounded border border-slate-200 bg-white px-2 py-1 text-xs text-slate-950 disabled:opacity-50"
                      >
                        {/* Covers the server's own floor default (e.g. 900s), which the
                            toggle-only enable path sets without going through this select --
                            without it, `value` above would match none of the options below
                            and the control would render blank. */}
                        {source.refresh_interval_seconds != null &&
                          !REFRESH_INTERVAL_OPTIONS.some((option) => option.value === source.refresh_interval_seconds) && (
                            <option value={source.refresh_interval_seconds}>
                              {formatRefreshInterval(source.refresh_interval_seconds)}
                            </option>
                          )}
                        {REFRESH_INTERVAL_OPTIONS.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                    </div>
                    {(source.refresh_enabled ?? false) && (
                      <p className="mt-1 text-xs text-slate-500">
                        {source.last_refresh_at
                          ? `Last refreshed ${new Date(source.last_refresh_at).toLocaleString()}`
                          : 'Not refreshed yet.'}
                      </p>
                    )}
                    {source.last_refresh_error && (
                      <p className="mt-1 text-xs text-red-600">{source.last_refresh_error}</p>
                    )}
                  </div>
                  <div className="flex flex-shrink-0 items-center gap-1">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => void testSource(source)}
                      disabled={testingId !== null || cooldownSecondsFor(source.id) > 0}
                    >
                      {testingId === source.id ? (
                        <LoaderCircle className="h-4 w-4 animate-spin" />
                      ) : (
                        <PlugZap className="h-4 w-4" />
                      )}
                      {cooldownSecondsFor(source.id) > 0 ? `Test (${cooldownSecondsFor(source.id)}s)` : 'Test'}
                    </Button>
                    <button
                      onClick={() => startRename(source)}
                      aria-label={`Rename ${source.name}`}
                      title="Rename"
                      disabled={busyId === source.id}
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-50 hover:text-slate-700 disabled:pointer-events-none disabled:opacity-50"
                    >
                      <Pencil className="h-4 w-4" />
                    </button>
                    <button
                      onClick={() => setDeleting(source)}
                      aria-label={`Delete ${source.name}`}
                      title="Delete"
                      disabled={busyId === source.id}
                      className="flex h-8 w-8 items-center justify-center rounded-lg text-slate-400 transition hover:bg-red-50 hover:text-red-600 disabled:pointer-events-none disabled:opacity-50"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                </div>
                {testResults[source.id] && <TestResult result={testResults[source.id]} />}
              </li>
            ))}
          </ul>
        )}
      </SectionCard>

      <p className="text-sm text-slate-500">
        Import runs live under{' '}
        <Link href="/imports" className="text-emerald-700 hover:text-emerald-800">
          Processing &gt; Confluence Import
        </Link>
        .
      </p>

      {creating && (
        <CreateSourceModal
          onClose={() => setCreating(false)}
          onCreated={async () => {
            setCreating(false);
            await reload();
          }}
        />
      )}

      {deleting && (
        <ConfirmDialog
          title="Delete Confluence connection"
          body={
            <p>
              Delete <span className="font-semibold text-slate-950">{deleting.name}</span>? Past import runs
              keep their history.
            </p>
          }
          confirmLabel="Delete connection"
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await apiSend(`/api/v1/import/sources/${deleting.id}`, { method: 'DELETE' });
            setDeleting(null);
            await reload();
          }}
        />
      )}
    </div>
  );
}

function TestResult({ result }: { result: ImportSourceTestResponse }) {
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

/** Create-only modal -- credentials are write-only, so there is nothing to prefill for an edit variant. */
function CreateSourceModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => Promise<void> }) {
  const [name, setName] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [authType, setAuthType] = useState<ImportAuthType>('cloud_basic');
  const [email, setEmail] = useState('');
  // Write-only secret: only ever holds what the user is typing right now.
  const [credential, setCredential] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Toggling the auth scheme clears the typed secret: the field is masked, so
  // a token entered for one scheme must not silently become the credential
  // of the other (mirrors imports/new/page.tsx's selectAuthType).
  const selectAuthType = (next: ImportAuthType) => {
    if (next !== authType) setCredential('');
    setAuthType(next);
  };

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const trimmedEmail = email.trim();
    if (authType === 'cloud_basic' && !trimmedEmail) {
      setError('The Atlassian account email is required for Cloud connections.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await apiJson<ImportSource>('/api/v1/import/sources', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name.trim(),
          base_url: baseUrl.trim(),
          auth_type: authType,
          auth_username: authType === 'cloud_basic' ? trimmedEmail : '',
          credential: credential.trim(),
        }),
      });
      await onCreated();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal title="Add Confluence connection" onClose={onClose}>
      <form onSubmit={submit} className="space-y-4">
        <Field label="Name">
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            className={inputClass}
            required
            autoFocus
            placeholder="ACME Confluence"
          />
        </Field>
        <Field label="Base URL">
          <input
            type="url"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            className={inputClass}
            required
            placeholder="https://acme.atlassian.net"
          />
        </Field>
        <div>
          <p className="text-sm font-medium text-slate-700">Authentication</p>
          <div className="mt-1 grid gap-3 sm:grid-cols-2">
            <button
              type="button"
              onClick={() => selectAuthType('cloud_basic')}
              className={`rounded-xl border p-3 text-left ${
                authType === 'cloud_basic' ? 'border-emerald-300 bg-emerald-50' : 'border-slate-200 bg-white'
              }`}
            >
              <p className="text-sm font-semibold text-slate-950">Confluence Cloud</p>
              <p className="mt-1 text-xs text-slate-600">Atlassian account email + API token.</p>
            </button>
            <button
              type="button"
              onClick={() => selectAuthType('pat_bearer')}
              className={`rounded-xl border p-3 text-left ${
                authType === 'pat_bearer' ? 'border-emerald-300 bg-emerald-50' : 'border-slate-200 bg-white'
              }`}
            >
              <p className="text-sm font-semibold text-slate-950">Server / Data Center</p>
              <p className="mt-1 text-xs text-slate-600">Personal access token (PAT).</p>
            </button>
          </div>
        </div>
        {authType === 'cloud_basic' && (
          <Field label="Atlassian account email">
            <input
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              type="email"
              className={inputClass}
              required
              placeholder="name@company.com"
            />
          </Field>
        )}
        <Field label={authType === 'cloud_basic' ? 'API token' : 'Personal access token'} hint="Stored encrypted and write-only: it is never shown again.">
          <input
            value={credential}
            onChange={(e) => setCredential(e.target.value)}
            type="password"
            className={inputClass}
            required
            autoComplete="new-password"
            data-1p-ignore
            data-lpignore="true"
            placeholder={authType === 'cloud_basic' ? 'Atlassian API token' : 'Confluence PAT'}
          />
        </Field>
        <ErrorNotice message={error} />
        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="outline" size="sm" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button type="submit" size="sm" disabled={busy}>
            {busy && <LoaderCircle className="h-4 w-4 animate-spin" />}
            Add connection
          </Button>
        </div>
      </form>
    </Modal>
  );
}
