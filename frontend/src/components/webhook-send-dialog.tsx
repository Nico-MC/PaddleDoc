'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { LoaderCircle } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ApiError } from '@/lib/api';
import { ErrorNotice, errorMessage, Field, inputClass, LoadingState, Modal } from '@/components/admin/admin-shared';
import {
  type WebhookConnection,
  type WebhookDelivery,
  listWebhookConnections,
  sendWebhook,
  webhookDeliveryStatusChip,
} from '@/lib/webhooks';

export type WebhookSendDialogJob = { id: string; label: string };

type WebhookSendDialogProps = {
  /** The job to send -- always a FINISHED job by the time this opens. */
  job: WebhookSendDialogJob;
  onClose: () => void;
  /** Called once right after a delivery is created (terminal already -- POST /send is synchronous). */
  onSent?: () => void;
};

/**
 * Modal manual-send flow: pick an enabled webhook connection, POST /send.
 * Unlike OpenWebUIPushDialog, POST /webhooks/send takes exactly one
 * connection_id + job_id (no batch send in the contract) and returns the
 * finished delivery directly -- no polling needed.
 */
export function WebhookSendDialog({ job, onClose, onSent }: WebhookSendDialogProps) {
  const [connections, setConnections] = useState<WebhookConnection[]>([]);
  const [connectionsLoading, setConnectionsLoading] = useState(true);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  // 404 means the backend hasn't shipped this endpoint yet, not a real
  // failure (mirrors OpenWebUIPushDialog's `connectionsUnavailable` state).
  const [connectionsUnavailable, setConnectionsUnavailable] = useState(false);
  const [selectedConnectionId, setSelectedConnectionId] = useState('');

  const [sendBusy, setSendBusy] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [delivery, setDelivery] = useState<WebhookDelivery | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await listWebhookConnections();
        if (cancelled) return;
        const enabled = data.items.filter((c) => c.enabled);
        setConnections(enabled);
        if (enabled.length > 0) setSelectedConnectionId(enabled[0].id);
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

  const handleSend = async () => {
    if (!selectedConnectionId) return;
    setSendBusy(true);
    setSendError(null);
    try {
      const result = await sendWebhook({ connection_id: selectedConnectionId, job_id: job.id });
      setDelivery(result);
      onSent?.();
    } catch (err) {
      setSendError(errorMessage(err));
    } finally {
      setSendBusy(false);
    }
  };

  const canSend = !connectionsUnavailable && connections.length > 0 && delivery === null;

  return (
    <Modal title="Send to webhook" onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-slate-600">{job.label}</p>

        {connectionsUnavailable ? (
          <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
            Not available on this backend yet.
          </div>
        ) : connectionsLoading ? (
          <LoadingState label="Loading connections..." />
        ) : connectionsError ? (
          <ErrorNotice message={connectionsError} />
        ) : connections.length === 0 ? (
          <p className="text-sm text-slate-600">
            No enabled webhook connections.{' '}
            <Link href="/connections?tab=webhooks" className="text-emerald-700 hover:text-emerald-800">
              Add one
            </Link>{' '}
            first.
          </p>
        ) : delivery ? (
          <div aria-live="polite" className="rounded-xl border border-slate-200 px-4 py-3">
            <div className="flex items-center justify-between gap-3">
              <span className="text-sm font-medium text-slate-950">
                {connections.find((c) => c.id === delivery.connection_id)?.name ?? delivery.connection_name}
              </span>
              <span className={`shrink-0 rounded px-2 py-1 text-xs ${webhookDeliveryStatusChip[delivery.status]}`}>
                {delivery.status}
              </span>
            </div>
            {delivery.http_status !== null && (
              <p className="mt-1 text-xs text-slate-500">HTTP {delivery.http_status}</p>
            )}
            {delivery.error_message && <p className="mt-2 text-xs text-red-600">{delivery.error_message}</p>}
          </div>
        ) : (
          <Field label="Connection">
            <select
              value={selectedConnectionId}
              onChange={(event) => setSelectedConnectionId(event.target.value)}
              className={inputClass}
            >
              {connections.map((connection) => (
                <option key={connection.id} value={connection.id}>
                  {connection.name}
                </option>
              ))}
            </select>
          </Field>
        )}

        <div aria-live="polite">
          <ErrorNotice message={sendError} />
        </div>

        <div className="flex justify-end gap-2 pt-1">
          <Button type="button" variant="outline" size="sm" onClick={onClose}>
            {delivery ? 'Close' : 'Cancel'}
          </Button>
          {canSend && (
            <Button
              type="button"
              size="sm"
              onClick={() => void handleSend()}
              disabled={sendBusy || !selectedConnectionId}
            >
              {sendBusy && <LoaderCircle className="h-4 w-4 animate-spin" />}
              {sendBusy ? 'Sending...' : 'Send'}
            </Button>
          )}
        </div>
      </div>
    </Modal>
  );
}
