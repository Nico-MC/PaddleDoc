'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import dynamic from 'next/dynamic';
import { useParams } from 'next/navigation';
import { Download, LoaderCircle, Mail } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ApiError, apiFetch, apiJson } from '@/lib/api';
import { formatBytes } from '@/components/dashboard/shared';
import {
  hasActiveMailParts,
  mailJobStatusChip,
  mailPartOutcomeChip,
  mailSkipReasonLabel,
  type MailMessageDetail,
  type MailPart,
} from '@/lib/mail';

// react-markdown + remark-gfm + rehype-sanitize are only needed once a
// message with a body renders — deferred + client-only, same rationale as
// jobs/[id]/page.tsx.
const MarkdownView = dynamic(() => import('@/components/markdown/markdown-view').then((mod) => mod.MarkdownView), {
  ssr: false,
  loading: () => (
    <div className="animate-pulse space-y-3" role="status" aria-label="Loading rendered body">
      <div className="h-4 w-3/4 rounded bg-slate-100" />
      <div className="h-4 w-full rounded bg-slate-100" />
      <div className="h-4 w-5/6 rounded bg-slate-100" />
    </div>
  ),
});

const POLL_INTERVAL_MS = 2500;

function partDownloadName(part: MailPart): string {
  return part.filename.trim() || `part-${part.index}`;
}

export default function MailMessagePage() {
  const params = useParams<{ id: string }>();
  const messageId = params.id;

  const [message, setMessage] = useState<MailMessageDetail | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // null = not fetched yet (skeleton); non-null after a completed fetch —
  // fetched once per message, not on every poll tick (body content never
  // changes once ingested).
  const [bodyMarkdown, setBodyMarkdown] = useState<string | null>(null);
  const [bodyError, setBodyError] = useState<string | null>(null);

  const [downloadingRaw, setDownloadingRaw] = useState(false);
  const [downloadingPart, setDownloadingPart] = useState<number | null>(null);

  // Poll the detail endpoint every 2.5 s while any attachment job is still
  // PENDING/RUNNING; the timeout chain ends once every part is terminal and
  // is cleared on unmount — same pattern as imports/[id]/page.tsx.
  useEffect(() => {
    if (!messageId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      try {
        const detail = await apiJson<MailMessageDetail>(`/api/v1/mail/messages/${messageId}`, { cache: 'no-store' });
        if (cancelled) return;
        setMessage(detail);
        setLoadError(null);
        if (hasActiveMailParts(detail.parts)) {
          timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
        }
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 404) {
          setNotFound(true);
          return;
        }
        setLoadError(error instanceof ApiError ? error.detail : 'Failed to load the mail message.');
        // Transient failure: keep polling so a recovering backend resumes updates.
        timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
      }
    };

    void tick();
    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
    };
  }, [messageId]);

  // Body content lives behind its own endpoint (mirrors GET /jobs/{id}/preview).
  useEffect(() => {
    if (!message || !message.has_body || bodyMarkdown !== null) return;
    let cancelled = false;
    const loadBody = async () => {
      try {
        const response = await apiFetch(`/api/v1/mail/messages/${message.id}/body`, {
          cache: 'no-store',
          skipAuthRedirect: true,
        });
        if (cancelled) return;
        if (!response.ok) {
          setBodyError('Failed to load message body.');
          return;
        }
        const text = await response.text();
        if (cancelled) return;
        setBodyMarkdown(text);
      } catch {
        if (!cancelled) setBodyError('Failed to load message body.');
      }
    };
    void loadBody();
    return () => {
      cancelled = true;
    };
  }, [message, bodyMarkdown]);

  // Fetch-as-blob download: apiFetch -> blob -> synthesized <a download>
  // click -> revokeObjectURL — the folder-ZIP / artifact pattern (a naked
  // <a href> would 404 against the frontend origin and cannot carry the
  // session cookie cross-origin).
  const downloadBlob = async (path: string, filename: string) => {
    const response = await apiFetch(path, { cache: 'no-store' });
    if (!response.ok) {
      alert('Download failed.');
      return;
    }
    const blob = await response.blob();
    const href = window.URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = href;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.URL.revokeObjectURL(href);
  };

  const downloadRaw = async () => {
    if (!message) return;
    setDownloadingRaw(true);
    try {
      await downloadBlob(`/api/v1/mail/messages/${message.id}/raw`, `${message.id}.eml`);
    } finally {
      setDownloadingRaw(false);
    }
  };

  const downloadPart = async (part: MailPart) => {
    if (!message) return;
    setDownloadingPart(part.index);
    try {
      await downloadBlob(`/api/v1/mail/messages/${message.id}/parts/${part.index}/content`, partDownloadName(part));
    } finally {
      setDownloadingPart(null);
    }
  };

  if (notFound) {
    return (
      <main className="min-h-screen">
        <div className="mx-auto w-full max-w-4xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
          <h1 className="text-2xl font-semibold">Mail message not found</h1>
          <p className="mt-2 text-sm text-slate-600">The message does not exist or is not visible to you.</p>
          <Link href="/mail" className="mt-4 inline-block text-sm text-emerald-700 hover:text-emerald-800">
            Back to mail
          </Link>
        </div>
      </main>
    );
  }

  if (!message) {
    return (
      <main className="min-h-screen">
        <div className="mx-auto w-full max-w-4xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
          <div className="flex items-center gap-2 py-6 text-sm text-slate-600">
            <LoaderCircle className="h-4 w-4 animate-spin" /> Loading mail message...
          </div>
          {loadError && <p className="text-sm text-red-600">{loadError}</p>}
        </div>
      </main>
    );
  }

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-4xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
        <section className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <p className="flex items-center gap-2 text-xs uppercase tracking-[0.16em] text-slate-500">
              <Mail className="h-3.5 w-3.5" /> Mail message
            </p>
            <h1 className="mt-1 truncate text-2xl font-semibold">{message.subject.trim() || '(no subject)'}</h1>
            <p className="mt-1 text-sm text-slate-600">
              From {message.from_address || 'unknown sender'}
              {message.source ? ` · ${message.source}` : ''}
              {` · ${new Date(message.created_at).toLocaleString()}`}
            </p>
          </div>
          <Button variant="outline" onClick={() => void downloadRaw()} disabled={downloadingRaw}>
            <Download className="mr-2 h-4 w-4" /> {downloadingRaw ? 'Downloading...' : 'Download .eml'}
          </Button>
        </section>

        {loadError && <p className="mb-4 text-sm text-amber-700">{loadError} Retrying...</p>}

        <section className="mb-6 rounded-3xl border border-slate-200 bg-white p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <h2 className="mb-3 text-lg font-semibold">Envelope</h2>
          <dl className="grid gap-3 text-sm sm:grid-cols-2">
            <div>
              <dt className="text-xs text-slate-500">From</dt>
              <dd className="text-slate-950">{message.from_address || '-'}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">To</dt>
              <dd className="text-slate-950">{message.recipients.to.join(', ') || '-'}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Cc</dt>
              <dd className="text-slate-950">{message.recipients.cc.join(', ') || '-'}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Sent</dt>
              <dd className="text-slate-950">{message.sent_at ? new Date(message.sent_at).toLocaleString() : '-'}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Source</dt>
              <dd className="text-slate-950">{message.source || '-'}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Raw size</dt>
              <dd className="text-slate-950">{formatBytes(message.raw_size_bytes)}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Message-ID</dt>
              <dd className="truncate text-slate-950" title={message.rfc_message_id ?? ''}>
                {message.rfc_message_id || '-'}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Content hash</dt>
              <dd className="truncate font-mono text-xs text-slate-700" title={message.content_sha256}>
                {message.content_sha256}
              </dd>
            </div>
          </dl>
        </section>

        <section className="mb-6 rounded-3xl border border-slate-200 bg-white p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <h2 className="mb-3 text-lg font-semibold">Body</h2>
          {!message.has_body ? (
            <p className="text-sm text-slate-600">This message has no body content.</p>
          ) : bodyError ? (
            <p className="text-sm text-red-600">{bodyError}</p>
          ) : bodyMarkdown === null ? (
            <div className="flex items-center gap-2 py-4 text-sm text-slate-600">
              <LoaderCircle className="h-4 w-4 animate-spin" /> Loading body...
            </div>
          ) : (
            // Body has no artifacts/password — MarkdownView already strips
            // the YAML frontmatter the ingest service prepends.
            <MarkdownView markdown={bodyMarkdown} />
          )}
        </section>

        <section className="rounded-3xl border border-slate-200 bg-white p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <div className="mb-3 flex items-center justify-between gap-4">
            <h2 className="text-lg font-semibold">Parts</h2>
            <p className="text-sm text-slate-500">{message.parts.length} part(s)</p>
          </div>
          {message.parts.length === 0 ? (
            <p className="py-4 text-sm text-slate-600">This message has no parts beyond the body.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full table-auto text-left text-xs sm:text-sm">
                <thead className="text-slate-500">
                  <tr>
                    <th className="pb-2 font-medium">Filename</th>
                    <th className="hidden pb-2 font-medium sm:table-cell">Type</th>
                    <th className="hidden pb-2 font-medium md:table-cell">Size</th>
                    <th className="pb-2 font-medium">Outcome</th>
                    <th className="pb-2 text-right font-medium">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {message.parts.map((part) => (
                    <tr key={part.index} className="border-t border-slate-100">
                      <td className="py-3 text-slate-950">{part.filename || `part-${part.index}`}</td>
                      <td className="hidden py-3 text-slate-700 sm:table-cell">{part.content_type || '-'}</td>
                      <td className="hidden py-3 text-slate-700 md:table-cell">{formatBytes(part.size_bytes)}</td>
                      <td className="py-3">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className={`rounded px-2 py-1 text-xs ${mailPartOutcomeChip[part.outcome]}`}>
                            {part.outcome}
                          </span>
                          {part.outcome === 'job' && part.job_id && (
                            <>
                              <span
                                className={`rounded px-2 py-1 text-xs ${
                                  part.job_status ? mailJobStatusChip[part.job_status] : 'bg-slate-100 text-slate-700'
                                }`}
                              >
                                {part.job_status ?? 'unknown'}
                              </span>
                              <Link href={`/jobs/${part.job_id}`} className="text-xs text-emerald-700 hover:text-emerald-800">
                                View job
                              </Link>
                            </>
                          )}
                          {part.outcome === 'inline' && (
                            <span className="text-xs text-slate-500">Inline attachment (not processed)</span>
                          )}
                          {part.outcome === 'skipped' && (
                            <span className="text-xs text-slate-500">{mailSkipReasonLabel(part.skip_reason)}</span>
                          )}
                        </div>
                        {part.outcome === 'job' && part.job_status === 'FAILED' && part.job_error_message && (
                          <p className="mt-1 text-xs text-red-600">{part.job_error_message}</p>
                        )}
                      </td>
                      <td className="py-3 text-right">
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          disabled={downloadingPart === part.index}
                          onClick={() => void downloadPart(part)}
                        >
                          <Download className="mr-2 h-4 w-4" />
                          {downloadingPart === part.index ? 'Downloading...' : 'Download'}
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <p className="mt-6">
          <Link href="/mail" className="text-sm text-emerald-700 hover:text-emerald-800">
            Back to mail
          </Link>
        </p>
      </div>
    </main>
  );
}
