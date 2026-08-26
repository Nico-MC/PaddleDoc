'use client';

import { useCallback, useEffect, useState } from 'react';
import { CircleCheck, CircleX, LoaderCircle, Pencil, PlugZap, Plus, Trash2, Webhook } from 'lucide-react';

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
  Toggle,
} from '@/components/admin/admin-shared';
import {
  WEBHOOK_EVENTS,
  WEBHOOK_TEST_COOLDOWN_FALLBACK_MS,
  type WebhookConnection,
  type WebhookConnectionTestResponse,
  type WebhookDelivery,
  type WebhookEvent,
  createWebhookConnection,
  deleteWebhookConnection,
  listWebhookConnections,
  listWebhookDeliveries,
  testWebhookConnection,
  updateWebhookConnection,
  webhookDeliveryStatusChip,
  webhookEventLabel,
} from '@/lib/webhooks';

export function WebhookConnectionsTab() {
  const [connections, setConnections] = useState<WebhookConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  // 404 means the backend hasn't shipped this endpoint yet, not a real
  // failure (mirrors openwebui-connections-tab.tsx's `unavailable` state).
  const [unavailable, setUnavailable] = useState(false);

  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<WebhookConnection | null>(null);
  const [deleting, setDeleting] = useState<WebhookConnection | null>(null);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<Record<string, WebhookConnectionTestResponse>>({});
  // Deadline (Date.now() + window) per connection id -- test cooldown is
  // per-connection server-side, so tracked as a map rather than one timer.
  const [testCooldowns, setTestCooldowns] = useState<Record<string, number>>({});
  const [nowTick, setNowTick] = useState(() => Date.now());

  const [deliveries, setDeliveries] = useState<WebhookDelivery[]>([]);
  const [deliveriesLoading, setDeliveriesLoading] = useState(true);
  const [deliveriesError, setDeliveriesError] = useState<string | null>(null);
  const [expandedDeliveryId, setExpandedDeliveryId] = useState<string | null>(null);

  // Reusable for post-mutation refreshes (ConnectionModal's onSaved,
  // ConfirmDialog's onConfirm below). NOT called from the mount effect (see
  // that effect's own comment) -- react-hooks/set-state-in-effect requires
  // an effect's own body stay await-first, so the mount fetch below
  // duplicates this logic instead of calling `reload()` directly.
  const reload = useCallback(async () => {
    try {
      const data = await listWebhookConnections();
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

  const reloadDeliveries = useCallback(async () => {
    try {
      const data = await listWebhookDeliveries({ limit: 20 });
      setDeliveries(data.items);
      setDeliveriesError(null);
    } catch (err) {
      // A 404 here just means the same not-yet-deployed backend the
      // connections list already reports -- no separate banner needed.
      if (!(err instanceof ApiError && err.status === 404)) {
        setDeliveriesError(errorMessage(err));
      }
    } finally {
      setDeliveriesLoading(false);
    }
  }, []);

  // Initial loads only (mirrors openwebui-connections-tab.tsx): each IIFE's
  // first statement is its `await`, not a synchronous setState, per
  // react-hooks/set-state-in-effect.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await listWebhookConnections();
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
        const data = await listWebhookDeliveries({ limit: 20 });
        if (cancelled) return;
        setDeliveries(data.items);
        setDeliveriesError(null);
      } catch (err) {
        if (cancelled) return;
        if (!(err instanceof ApiError && err.status === 404)) {
          setDeliveriesError(errorMessage(err));
        }
      } finally {
        if (!cancelled) setDeliveriesLoading(false);
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
    setTestCooldowns((current) => ({ ...current, [id]: Date.now() + WEBHOOK_TEST_COOLDOWN_FALLBACK_MS }));
  };

  const testConnection = async (id: string) => {
    setTestingId(id);
    setTestResults((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
    try {
      const result = await testWebhookConnectionSafe(id);
      setTestResults((prev) => ({ ...prev, [id]: result }));
      armTestCooldown(id);
    } finally {
      setTestingId(null);
    }
  };

  return (
    <div className="space-y-6">
      <SectionCard
        title="Connections"
        description="Outbound webhook targets; stored secrets are never displayed."
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
            Not available on this backend yet. This page starts showing data automatically once the endpoint is
            deployed.
          </div>
        )}
        {loading ? (
          <LoadingState label="Loading connections..." />
        ) : unavailable ? null : connections.length === 0 ? (
          <div className="flex flex-col items-center gap-3 py-10 text-center">
            <Webhook className="h-8 w-8 text-slate-300" />
            <p className="text-sm text-slate-500">No webhook connections yet. Add one to start sending events.</p>
            <Button variant="outline" size="sm" onClick={() => setCreating(true)}>
              <Plus className="h-4 w-4" />
              Add connection
            </Button>
          </div>
        ) : (
          <>
            <ul className="space-y-4">
              {connections.map((connection) => (
                <li key={connection.id} className="rounded-xl border border-slate-200 p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-semibold text-slate-950">{connection.name}</span>
                        <Badge tone={connection.enabled ? 'emerald' : 'slate'}>
                          {connection.enabled ? 'Enabled' : 'Disabled'}
                        </Badge>
                        <Badge tone={connection.has_secret ? 'emerald' : 'slate'}>
                          {connection.has_secret ? 'Secret set' : 'No secret'}
                        </Badge>
                      </div>
                      <dl className="mt-2 space-y-1 text-xs text-slate-500">
                        <div className="flex gap-2">
                          <dt className="w-16 flex-shrink-0 font-medium">URL</dt>
                          <dd className="break-all">{connection.url}</dd>
                        </div>
                        <div className="flex gap-2">
                          <dt className="w-16 flex-shrink-0 font-medium">Created</dt>
                          <dd>{new Date(connection.created_at).toLocaleDateString()}</dd>
                        </div>
                      </dl>
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {connection.events.length === 0 ? (
                          <span className="text-xs text-slate-400">No events selected</span>
                        ) : (
                          connection.events.map((event) => (
                            <span
                              key={event}
                              className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-600"
                            >
                              {webhookEventLabel[event]}
                            </span>
                          ))
                        )}
                      </div>
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
            <p className="mt-4 text-xs text-slate-500">
              If a secret is set, deliveries carry an X-PaddleDoc-Signature header (HMAC-SHA256) your n8n flow can
              verify.
            </p>
          </>
        )}
      </SectionCard>

      <SectionCard title="Recent deliveries" description="Your most recent webhook delivery attempts, newest first.">
        <ErrorNotice message={deliveriesError} />
        {deliveriesLoading ? (
          <LoadingState label="Loading deliveries..." />
        ) : deliveries.length === 0 ? (
          <div className="flex flex-col items-center gap-3 py-10 text-center">
            <Webhook className="h-8 w-8 text-slate-300" />
            <p className="text-sm text-slate-500">No deliveries yet. They will show up here once an event fires.</p>
          </div>
        ) : (
          <ul className="divide-y divide-slate-100">
            {deliveries.map((delivery) => (
              <li key={delivery.id} className="py-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-slate-950">{webhookEventLabel[delivery.event]}</p>
                    <p className="mt-0.5 text-xs text-slate-500">
                      via {delivery.connection_name} · {new Date(delivery.created_at).toLocaleString()}
                      {delivery.http_status !== null && <span className="ml-2">HTTP {delivery.http_status}</span>}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <span className={`rounded px-2 py-1 text-xs ${webhookDeliveryStatusChip[delivery.status]}`}>
                      {delivery.status}
                    </span>
                    {delivery.error_message && (
                      <button
                        type="button"
                        onClick={() =>
                          setExpandedDeliveryId((current) => (current === delivery.id ? null : delivery.id))
                        }
                        className="text-xs text-emerald-700 hover:text-emerald-800"
                      >
                        {expandedDeliveryId === delivery.id ? 'Hide error' : 'Show error'}
                      </button>
                    )}
                  </div>
                </div>
                {expandedDeliveryId === delivery.id && delivery.error_message && (
                  <p className="mt-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
                    {delivery.error_message}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </SectionCard>

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
          title="Delete webhook connection"
          body={
            <p>
              Delete <span className="font-semibold text-slate-950">{deleting.name}</span>? Past deliveries keep
              their history.
            </p>
          }
          confirmLabel="Delete connection"
          onClose={() => setDeleting(null)}
          onConfirm={async () => {
            await deleteWebhookConnection(deleting.id);
            setDeleting(null);
            await reload();
            await reloadDeliveries();
          }}
        />
      )}
    </div>
  );
}

/** testWebhookConnection, but never throws -- a failed probe (incl. a 429 cooldown hit) is itself a result to render, not an error state. */
async function testWebhookConnectionSafe(id: string): Promise<WebhookConnectionTestResponse> {
  try {
    return await testWebhookConnection(id);
  } catch (err) {
    return { ok: false, detail: errorMessage(err) };
  }
}

function TestResult({ result }: { result: WebhookConnectionTestResponse }) {
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

/** Create (no `connection`) or edit (with `connection`) a webhook connection. */
function ConnectionModal({
  connection,
  onClose,
  onSaved,
}: {
  connection?: WebhookConnection;
  onClose: () => void;
  onSaved: () => Promise<void>;
}) {
  const isEdit = connection !== undefined;

  const [name, setName] = useState(connection?.name ?? '');
  const [url, setUrl] = useState(connection?.url ?? '');
  const [secret, setSecret] = useState('');
  // Explicit tri-state for the stored secret on edit: the PATCH contract
  // treats an omitted `secret` as "keep", '' as "clear" -- without this
  // checkbox the clear state would be unreachable from the UI.
  const [clearSecret, setClearSecret] = useState(false);
  const [enabled, setEnabled] = useState(connection?.enabled ?? true);
  const [events, setEvents] = useState<WebhookEvent[]>(connection?.events ?? []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const toggleEvent = (event: WebhookEvent) => {
    setEvents((current) => (current.includes(event) ? current.filter((e) => e !== event) : [...current, event]));
  };

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (events.length === 0) {
      setError('Select at least one event.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (isEdit) {
        await updateWebhookConnection(connection.id, {
          name: name.trim(),
          url: url.trim(),
          enabled,
          events,
          ...(clearSecret ? { secret: '' } : secret ? { secret } : {}),
        });
      } else {
        await createWebhookConnection({
          name: name.trim(),
          url: url.trim(),
          enabled,
          events,
          ...(secret ? { secret } : {}),
        });
      }
      await onSaved();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <Modal title={isEdit ? `Edit ${connection.name}` : 'Add webhook connection'} onClose={onClose}>
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
        <Field label="URL" hint="Where events are POSTed, e.g. an n8n webhook trigger URL.">
          <input
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            className={inputClass}
            required
            placeholder="https://n8n.example.com/webhook/..."
          />
        </Field>
        <Field label="Secret" hint={isEdit ? 'Leave blank to keep the stored secret.' : 'Optional -- enables the X-PaddleDoc-Signature header.'}>
          <input
            type="password"
            value={secret}
            onChange={(e) => setSecret(e.target.value)}
            className={inputClass}
            placeholder={isEdit ? 'unchanged unless filled' : undefined}
            autoComplete="new-password"
            disabled={clearSecret}
            data-1p-ignore
            data-lpignore="true"
          />
        </Field>
        {isEdit && connection.has_secret && (
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={clearSecret}
              onChange={(e) => {
                setClearSecret(e.target.checked);
                if (e.target.checked) setSecret('');
              }}
              className="h-4 w-4 rounded border-slate-300 text-emerald-600"
            />
            Remove the stored secret (deliveries will no longer be signed)
          </label>
        )}
        <fieldset>
          <legend className="text-sm font-medium text-slate-700">Events</legend>
          <div className="mt-2 space-y-2">
            {WEBHOOK_EVENTS.map((event) => (
              <label key={event} className="flex items-center gap-2 text-sm text-slate-700">
                <input
                  type="checkbox"
                  checked={events.includes(event)}
                  onChange={() => toggleEvent(event)}
                  className="h-4 w-4 rounded border-slate-300 text-emerald-600 focus:ring-emerald-500"
                />
                {webhookEventLabel[event]}
              </label>
            ))}
          </div>
        </fieldset>
        <Toggle checked={enabled} onChange={setEnabled} label="Enabled" />
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
