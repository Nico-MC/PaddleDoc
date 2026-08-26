/**
 * Types + tiny helpers for the OpenWebUI push surface.
 * Field names mirror backend/app/schemas/openwebui.py exactly.
 */

import { ApiError, apiFetch, apiJson } from '@/lib/api';

export type OpenWebUIPushStatus = 'pending' | 'running' | 'finished' | 'failed';

export type OpenWebUIConnection = {
  id: string;
  name: string;
  base_url: string;
  /** Never the key itself -- just whether one is on file. */
  has_api_key: boolean;
  created_at: string;
  updated_at: string;
};

export type OpenWebUIConnectionListResponse = {
  items: OpenWebUIConnection[];
};

export type OpenWebUIConnectionCreateRequest = {
  name: string;
  base_url: string;
  api_key: string;
};

/** PATCH body -- api_key omitted or empty keeps the stored key. */
export type OpenWebUIConnectionUpdateRequest = {
  name?: string;
  base_url?: string;
  api_key?: string;
};

export type OpenWebUIConnectionTestResponse = {
  ok: boolean;
  detail: string | null;
};

export type OpenWebUIKnowledgeItem = {
  id: string;
  name: string;
  description: string | null;
};

export type OpenWebUIKnowledgeListResponse = {
  items: OpenWebUIKnowledgeItem[];
};

export type OpenWebUIPushCreateRequest = {
  connection_id: string;
  knowledge_id: string;
  knowledge_name: string;
  job_ids: string[];
};

export type OpenWebUIPush = {
  id: string;
  job_id: string;
  connection_id: string | null;
  connection_name: string;
  knowledge_id: string;
  knowledge_name: string;
  status: OpenWebUIPushStatus;
  error_message: string | null;
  openwebui_file_id: string | null;
  /** sha256(pushed markdown) != sha256(the job's CURRENT markdown), computed server-side at read time. */
  content_stale: boolean;
  created_at: string;
  updated_at: string;
};

export type OpenWebUIPushListResponse = {
  items: OpenWebUIPush[];
};

const PUSH_ACTIVE_STATUSES: OpenWebUIPushStatus[] = ['pending', 'running'];

export function isPushActive(status: OpenWebUIPushStatus): boolean {
  return PUSH_ACTIVE_STATUSES.includes(status);
}

/** Status chip classes, matching the import-run badge visual language. */
export const pushStatusChip: Record<OpenWebUIPushStatus, string> = {
  pending: 'bg-slate-100 text-slate-700',
  running: 'bg-sky-100 text-sky-800',
  finished: 'bg-emerald-100 text-emerald-800',
  failed: 'bg-red-100 text-red-700',
};

/**
 * Matches openwebui_test_cooldown_seconds' documented default. The 429 from
 * POST /openwebui/connections/{id}/test does carry a Retry-After header, but
 * apiJson's ApiError only exposes the parsed detail string, not response
 * headers -- so the client falls back to this fixed window, same as the
 * Confluence import wizard's TEST_COOLDOWN_FALLBACK_MS (lib/imports.ts).
 */
export const OPENWEBUI_TEST_COOLDOWN_FALLBACK_MS = 10_000;

const BASE = '/api/v1/openwebui';

/**
 * Like apiFetch + ok-check, but for the DELETE endpoint whose success body
 * we do not need. Duplicates admin-shared.tsx's apiSend rather than
 * importing it -- lib/ files stay independent of components/ in this
 * codebase (mirrors apiJson/apiSend already carrying near-identical
 * error-parsing logic side by side in their own layers).
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

export function listOpenWebUIConnections(): Promise<OpenWebUIConnectionListResponse> {
  return apiJson<OpenWebUIConnectionListResponse>(`${BASE}/connections`, { cache: 'no-store' });
}

export function createOpenWebUIConnection(payload: OpenWebUIConnectionCreateRequest): Promise<OpenWebUIConnection> {
  return apiJson<OpenWebUIConnection>(`${BASE}/connections`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export function updateOpenWebUIConnection(
  id: string,
  payload: OpenWebUIConnectionUpdateRequest,
): Promise<OpenWebUIConnection> {
  return apiJson<OpenWebUIConnection>(`${BASE}/connections/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export function deleteOpenWebUIConnection(id: string): Promise<void> {
  return send(`${BASE}/connections/${id}`, { method: 'DELETE' });
}

export function testOpenWebUIConnection(id: string): Promise<OpenWebUIConnectionTestResponse> {
  return apiJson<OpenWebUIConnectionTestResponse>(`${BASE}/connections/${id}/test`, { method: 'POST' });
}

export function listOpenWebUIKnowledge(connectionId: string): Promise<OpenWebUIKnowledgeListResponse> {
  return apiJson<OpenWebUIKnowledgeListResponse>(`${BASE}/connections/${connectionId}/knowledge`, {
    cache: 'no-store',
  });
}

export function createOpenWebUIPushes(payload: OpenWebUIPushCreateRequest): Promise<OpenWebUIPushListResponse> {
  return apiJson<OpenWebUIPushListResponse>(`${BASE}/pushes`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

/** GET /openwebui/pushes -- pass jobId to scope to one job's history, otherwise the caller's own recent pushes. */
export function listOpenWebUIPushes(params: { jobId?: string; limit?: number } = {}): Promise<OpenWebUIPushListResponse> {
  const query = new URLSearchParams();
  if (params.jobId) query.set('job_id', params.jobId);
  if (params.limit) query.set('limit', String(params.limit));
  const qs = query.toString();
  return apiJson<OpenWebUIPushListResponse>(`${BASE}/pushes${qs ? `?${qs}` : ''}`, { cache: 'no-store' });
}
