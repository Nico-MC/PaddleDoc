'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { LoaderCircle } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ApiError } from '@/lib/api';
import {
  ErrorNotice,
  errorMessage,
  Field,
  inputClass,
  LoadingState,
  Modal,
} from '@/components/admin/admin-shared';
import {
  type OpenWebUIConnection,
  type OpenWebUIKnowledgeItem,
  type OpenWebUIPush,
  createOpenWebUIPushes,
  isPushActive,
  listOpenWebUIConnections,
  listOpenWebUIKnowledge,
  listOpenWebUIPushes,
  pushStatusChip,
} from '@/lib/openwebui';

const POLL_INTERVAL_MS = 2500;

export type OpenWebUIPushDialogJob = { id: string; label: string };

type OpenWebUIPushDialogProps = {
  /** Jobs to push -- always FINISHED jobs with visible markdown by the time this opens. */
  jobs: OpenWebUIPushDialogJob[];
  onClose: () => void;
  /** Called once right after pushes are created, and again once every push reaches a terminal status -- lets callers refresh a push-status display they render outside this dialog (e.g. the job-detail page). */
  onPushed?: () => void;
};

/**
 * Modal push flow: pick a connection -> live-load its OpenWebUI knowledge
 * collections -> pick one -> push. Mirrors the Confluence import wizard's
 * connection-picker step and imports/[id]/page.tsx's poll-until-terminal
 * pattern, but as a standalone reusable component (opened from both the
 * job-detail page and the jobs list).
 */
export function OpenWebUIPushDialog({ jobs, onClose, onPushed }: OpenWebUIPushDialogProps) {
  const [connections, setConnections] = useState<OpenWebUIConnection[]>([]);
  const [connectionsLoading, setConnectionsLoading] = useState(true);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  // 404 means the backend hasn't shipped this endpoint yet, not a real
  // failure (mirrors vl-connections-tab.tsx's `unavailable` state).
  const [connectionsUnavailable, setConnectionsUnavailable] = useState(false);
  const [selectedConnectionId, setSelectedConnectionId] = useState('');

  const [knowledge, setKnowledge] = useState<OpenWebUIKnowledgeItem[] | null>(null);
  const [knowledgeLoading, setKnowledgeLoading] = useState(false);
  const [knowledgeError, setKnowledgeError] = useState<string | null>(null);
  const [selectedKnowledgeId, setSelectedKnowledgeId] = useState('');

  const [pushBusy, setPushBusy] = useState(false);
  const [pushError, setPushError] = useState<string | null>(null);
  const [pushes, setPushes] = useState<OpenWebUIPush[]>([]);
  // Bumped on every successful POST /pushes -- the poll effect below keys on
  // this (not on `pushes` itself) so its own status-refresh writes to
  // `pushes` don't retrigger/restart the loop.
  const [pushBatchId, setPushBatchId] = useState(0);
  const pushesRef = useRef<OpenWebUIPush[]>([]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await listOpenWebUIConnections();
        if (cancelled) return;
        setConnections(data.items);
        if (data.items.length > 0) {
          setSelectedConnectionId(data.items[0].id);
          // Arms the knowledge-loading indicator for the fetch the effect
          // below is about to kick off, now that a connection is selected.
          setKnowledgeLoading(true);
        }
        setConnectionsUnavailable(false);
        setConnectionsError(null);
      } catch (err) {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setConnectionsUnavailable(true);
        } else {
          setConnectionsError(errorMessage(err));
        }
      } finally {
        if (!cancelled) setConnectionsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Fetch-only: the reset of knowledge/selectedKnowledgeId/knowledgeError for
  // a NEW selection happens in `selectConnection` below (the event handler
  // that changes selectedConnectionId), not here -- an effect body must stay
  // await-first with no synchronous setState calls of its own
  // (react-hooks/set-state-in-effect; see openwebui/page.tsx's `reload` for
  // the sibling pattern in a form this rule does accept).
  useEffect(() => {
    if (!selectedConnectionId) return;
    let cancelled = false;
    (async () => {
      try {
        const data = await listOpenWebUIKnowledge(selectedConnectionId);
        if (cancelled) return;
        setKnowledge(data.items);
        if (data.items.length > 0) setSelectedKnowledgeId(data.items[0].id);
      } catch (err) {
        if (cancelled) return;
        setKnowledge([]);
        setKnowledgeError(errorMessage(err));
      } finally {
        if (!cancelled) setKnowledgeLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedConnectionId]);

  // Poll each active push's job until every push in the batch is terminal.
  // Scoped per job_id (not a single pushes-list call) so this stays correct
  // however many jobs the caller passed in.
  useEffect(() => {
    if (pushBatchId === 0) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      const current = pushesRef.current;
      const activeJobIds = Array.from(new Set(current.filter((p) => isPushActive(p.status)).map((p) => p.job_id)));
      if (activeJobIds.length === 0) return;
      const results = await Promise.all(
        activeJobIds.map((jobId) => listOpenWebUIPushes({ jobId, limit: 10 }).catch(() => null)),
      );
      if (cancelled) return;
      const byId = new Map<string, OpenWebUIPush>();
      for (const result of results) {
        if (!result) continue;
        for (const item of result.items) byId.set(item.id, item);
      }
      const next = current.map((p) => byId.get(p.id) ?? p);
      pushesRef.current = next;
      setPushes(next);
      if (next.some((p) => isPushActive(p.status))) {
        timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
      } else {
        onPushed?.();
      }
    };

    timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pushBatchId]);

  const jobLabel = (jobId: string): string => jobs.find((j) => j.id === jobId)?.label ?? jobId;

  /** User picked a different connection: resets the knowledge picker in the same tick (an event handler, not an effect, so a synchronous reset here is fine); the fetch-only effect above then loads the new connection's collections. */
  const selectConnection = (id: string) => {
    setSelectedConnectionId(id);
    setKnowledge(null);
    setSelectedKnowledgeId('');
    setKnowledgeError(null);
    setKnowledgeLoading(true);
  };

  const handlePush = async () => {
    if (!selectedConnectionId || !selectedKnowledgeId) return;
    const knowledgeItem = (knowledge ?? []).find((k) => k.id === selectedKnowledgeId);
    setPushBusy(true);
    setPushError(null);
    try {
      const response = await createOpenWebUIPushes({
        connection_id: selectedConnectionId,
        knowledge_id: selectedKnowledgeId,
        knowledge_name: knowledgeItem?.name ?? selectedKnowledgeId,
        job_ids: jobs.map((j) => j.id),
      });
      pushesRef.current = response.items;
      setPushes(response.items);
      setPushBatchId((n) => n + 1);
      onPushed?.();
    } catch (err) {
      setPushError(errorMessage(err));
    } finally {
      setPushBusy(false);
    }
  };

  const canPush = !connectionsUnavailable && connections.length > 0 && pushes.length === 0;

  return (
    <Modal title="Push to OpenWebUI" onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-slate-600">
          {jobs.length === 1 ? jobs[0].label : `${jobs.length} documents`}
        </p>

        {connectionsUnavailable ? (
          <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
            OpenWebUI push is not available on this backend yet.
          </div>
        ) : connectionsLoading ? (
          <LoadingState label="Loading connections..." />
        ) : connectionsError ? (
          <ErrorNotice message={connectionsError} />
        ) : connections.length === 0 ? (
          <p className="text-sm text-slate-600">
            No OpenWebUI connections configured.{' '}
            <Link href="/openwebui" className="text-emerald-700 hover:text-emerald-800">
              Add one
            </Link>{' '}
            first.
          </p>
        ) : pushes.length > 0 ? (
          <ul className="space-y-2">
            {pushes.map((push) => (
              <li key={push.id} className="rounded-xl border border-slate-200 px-4 py-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-slate-950">{jobLabel(push.job_id)}</p>
                    <p className="mt-0.5 truncate text-xs text-slate-500">{push.knowledge_name}</p>
                  </div>
                  <span className={`shrink-0 rounded px-2 py-1 text-xs ${pushStatusChip[push.status]}`}>
                    {isPushActive(push.status) && <LoaderCircle className="mr-1 inline h-3 w-3 animate-spin" />}
                    {push.status}
                  </span>
                </div>
                {push.error_message && <p className="mt-2 text-xs text-red-600">{push.error_message}</p>}
              </li>
            ))}
          </ul>
        ) : (
          <>
            <Field label="Connection">
              <select
                value={selectedConnectionId}
                onChange={(event) => selectConnection(event.target.value)}
                className={inputClass}
              >
                {connections.map((connection) => (
                  <option key={connection.id} value={connection.id}>
                    {connection.name}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Knowledge collection">
              {knowledgeLoading ? (
                <p className="mt-1 flex items-center gap-2 text-sm text-slate-500">
                  <LoaderCircle className="h-4 w-4 animate-spin" /> Loading collections...
                </p>
              ) : knowledgeError ? (
                <p className="mt-1 text-sm text-red-600">{knowledgeError}</p>
              ) : knowledge && knowledge.length === 0 ? (
                <p className="mt-1 text-sm text-slate-500">No knowledge collections found on this OpenWebUI instance.</p>
              ) : (
                <select
                  value={selectedKnowledgeId}
                  onChange={(event) => setSelectedKnowledgeId(event.target.value)}
                  className={inputClass}
                  disabled={!knowledge}
                >
                  {(knowledge ?? []).map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
                </select>
              )}
            </Field>
          </>
        )}

        <ErrorNotice message={pushError} />

        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="outline" size="sm" onClick={onClose}>
            {pushes.length > 0 ? 'Close' : 'Cancel'}
          </Button>
          {canPush && (
            <Button
              type="button"
              size="sm"
              onClick={() => void handlePush()}
              disabled={pushBusy || !selectedConnectionId || !selectedKnowledgeId}
            >
              {pushBusy && <LoaderCircle className="h-4 w-4 animate-spin" />}
              {pushBusy ? 'Pushing...' : 'Push'}
            </Button>
          )}
        </div>
      </div>
    </Modal>
  );
}
