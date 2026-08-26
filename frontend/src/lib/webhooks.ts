/**
 * Types + tiny helpers for the webhook delivery surface.
 * Field names mirror backend/app/schemas/webhooks.py exactly.
 */

import { ApiError, apiFetch, apiJson } from '@/lib/api';

/** Exact event strings the backend emits -- see webhook_connections.events. */
export type WebhookEvent = 'job.finished' | 'job.failed' | 'import_run.finished';

export const WEBHOOK_EVENTS: WebhookEvent[] = ['job.finished', 'job.failed', 'import_run.finished'];

/** Plain-language labels for the events checkbox group. */
export const webhookEventLabel: Record<WebhookEvent, string> = {
  'job.finished': 'Job finished',
  'job.failed': 'Job failed',
  'import_run.finished': 'Import run finished',
};

export type WebhookConnection = {
  id: string;
  name: string;
  url: string;
  /** Never the secret itself -- just whether one is on file. */
  has_secret: boolean;
  enabled: boolean;
  events: WebhookEvent[];
  created_at: string;
  updated_at: string;
};

export type WebhookConnectionListResponse = {
  items: WebhookConnection[];
};

export type WebhookConnectionCreateRequest = {
  name: string;
  url: string;
  secret?: string;
  enabled: boolean;
  events: WebhookEvent[];
};

/** PATCH body -- secret omitted or empty keeps the stored secret. */
export type WebhookConnectionUpdateRequest = {
  name?: string;
  url?: string;
  secret?: string;
  enabled?: boolean;
  events?: WebhookEvent[];
};

export type WebhookConnectionTestResponse = {
  ok: boolean;
  detail: string | null;
};

export type WebhookDeliveryStatus = 'pending' | 'sent' | 'failed';

export type WebhookDelivery = {
  id: string;
  connection_id: string | null;
  connection_name: string;
  event: WebhookEvent;
  job_id: string | null;
  import_run_id: string | null;
  status: WebhookDeliveryStatus;
  http_status: number | null;
  error_message: string | null;
  attempts: number;
  created_at: string;
  updated_at: string;
};

export type WebhookDeliveryListResponse = {
  items: WebhookDelivery[];
};

export type WebhookSendRequest = {
  connection_id: string;
  job_id: string;
};

/** Status chip classes, matching the app's existing status-badge visual language. */
export const webhookDeliveryStatusChip: Record<WebhookDeliveryStatus, string> = {
  pending: 'bg-slate-100 text-slate-700',
  sent: 'bg-emerald-100 text-emerald-800',
  failed: 'bg-red-100 text-red-700',
};

/**
 * Matches the openwebui_test_cooldown_seconds-style per-connection test
 * cooldown documented for POST /webhooks/connections/{id}/test. The 429
 * does carry a Retry-After header, but apiJson's ApiError only exposes the
 * parsed detail string, not response headers -- so the client falls back to
 * this fixed window, same as OPENWEBUI_TEST_COOLDOWN_FALLBACK_MS (lib/openwebui.ts).
 */
export const WEBHOOK_TEST_COOLDOWN_FALLBACK_MS = 10_000;

const BASE = '/api/v1/webhooks';

/**
 * Like apiFetch + ok-check, but for the DELETE endpoint whose success body
 * we do not need. Duplicates lib/openwebui.ts's `send` rather than
 * importing it -- lib/ files stay independent of each other in this
 * codebase.
 */
async function send(path: string, init?: RequestInit): Promise<void> {
  const res = await apiFetch(path, init);
  if (!res.ok) {
    let detail = `Request failed with status ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      // Non-JSON error body -- keep the generic message.
    }
    throw new ApiError(res.status, detail);
  }
}

export function listWebhookConnections(): Promise<WebhookConnectionListResponse> {
  return apiJson<WebhookConnectionListResponse>(`${BASE}/connections`, { cache: 'no-store' });
}

export function createWebhookConnection(payload: WebhookConnectionCreateRequest): Promise<WebhookConnection> {
  return apiJson<WebhookConnection>(`${BASE}/connections`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export function updateWebhookConnection(
  id: string,
  payload: WebhookConnectionUpdateRequest,
): Promise<WebhookConnection> {
  return apiJson<WebhookConnection>(`${BASE}/connections/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export function deleteWebhookConnection(id: string): Promise<void> {
  return send(`${BASE}/connections/${id}`, { method: 'DELETE' });
}

export function testWebhookConnection(id: string): Promise<WebhookConnectionTestResponse> {
  return apiJson<WebhookConnectionTestResponse>(`${BASE}/connections/${id}/test`, { method: 'POST' });
}

/** GET /webhooks/deliveries -- the caller's own recent deliveries, newest first. */
export function listWebhookDeliveries(params: { limit?: number } = {}): Promise<WebhookDeliveryListResponse> {
  const query = new URLSearchParams();
  if (params.limit) query.set('limit', String(params.limit));
  const qs = query.toString();
  return apiJson<WebhookDeliveryListResponse>(`${BASE}/deliveries${qs ? `?${qs}` : ''}`, { cache: 'no-store' });
}

/** POST /webhooks/send -- manual delivery of one FINISHED job to one connection. */
export function sendWebhook(payload: WebhookSendRequest): Promise<WebhookDelivery> {
  return apiJson<WebhookDelivery>(`${BASE}/send`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}
