/**
 * Types + tiny helpers for the mail-ingestion surface.
 * Field names mirror docs/integrations/mail-ingestion.md's response JSON
 * examples exactly (the backend is built from the same spec) — the
 * `lib/imports.ts` template.
 */

export type MailJobStatus = 'PENDING' | 'RUNNING' | 'FINISHED' | 'FAILED';

export type MailBodyFormat = 'text/plain' | 'text/html';

export type MailPartOutcome = 'job' | 'inline' | 'skipped';

export type MailRecipients = {
  to: string[];
  cc: string[];
};

/**
 * One entry of the `parts` manifest. `job_status` / `job_error_message` are
 * only ever populated by `GET /mail/messages/{id}` (detail) — the spec
 * describes list responses as carrying just `job_id` ("job states are one
 * join away"), so both fields are optional here and must be read
 * defensively, same spirit as the backend's `isinstance()` guards on
 * `processing_info`.
 */
export type MailPart = {
  index: number;
  filename: string;
  content_type: string;
  size_bytes: number;
  outcome: MailPartOutcome;
  job_id?: string | null;
  skip_reason?: string | null;
  job_status?: MailJobStatus | null;
  job_error_message?: string | null;
};

/** Shape shared by the ingest response, the list items, and the detail response. */
export type MailMessageBase = {
  id: string;
  content_sha256: string;
  rfc_message_id: string | null;
  subject: string;
  from_address: string;
  recipients: MailRecipients;
  sent_at: string | null;
  source: string;
  raw_size_bytes: number;
  body_format: MailBodyFormat | null;
  has_body: boolean;
  parts: MailPart[];
  created_at: string;
};

/** POST /api/v1/mail/messages — 201 first ingest, 200 idempotent replay, same shape. */
export type MailIngestResponse = MailMessageBase & { replayed: boolean };

/** Row of GET /api/v1/mail/messages. */
export type MailMessage = MailMessageBase;

/** GET /api/v1/mail/messages — the `/search` convention: page + real total. */
export type MailMessageListResponse = {
  items: MailMessage[];
  total: number;
};

/** GET /api/v1/mail/messages/{id} — full envelope, parts enriched with job state. */
export type MailMessageDetail = MailMessageBase;

/** One attachment entry of GET /api/v1/mail/messages/{id}/export.json. */
export type MailExportAttachment = {
  index: number;
  filename: string;
  content_type?: string;
  size_bytes?: number;
  outcome: MailPartOutcome;
  job_id?: string;
  job_status?: MailJobStatus;
  content_sha256?: string;
  markdown?: string;
  error_message?: string;
  skip_reason?: string;
};

/** GET /api/v1/mail/messages/{id}/export.json — schema `paddledoc.mail-export/1`. */
export type MailExport = {
  schema: 'paddledoc.mail-export/1';
  message: {
    id: string;
    content_sha256: string;
    rfc_message_id: string | null;
    subject: string;
    from_address: string;
    recipients: MailRecipients;
    sent_at: string | null;
    source: string;
    created_at: string;
  };
  body: { format: string; markdown: string } | null;
  attachments: MailExportAttachment[];
  complete: boolean;
};

/** Status chip classes — matches lib/imports.ts's `importJobStatusChip` palette. */
export const mailJobStatusChip: Record<MailJobStatus, string> = {
  PENDING: 'bg-slate-100 text-slate-700',
  RUNNING: 'bg-sky-100 text-sky-800',
  FINISHED: 'bg-emerald-100 text-emerald-800',
  FAILED: 'bg-red-100 text-red-700',
};

export const mailPartOutcomeChip: Record<MailPartOutcome, string> = {
  job: 'bg-emerald-100 text-emerald-800',
  inline: 'bg-slate-100 text-slate-700',
  skipped: 'bg-amber-100 text-amber-800',
};

const MAIL_SKIP_REASON_LABELS: Record<string, string> = {
  unsupported_type: 'Unsupported file type',
  too_large: 'File too large',
  nested_message: 'Forwarded message (not processed)',
};

/** Human label for a skipped part's `skip_reason` — unknown reasons fall back to a de-slugged form. */
export function mailSkipReasonLabel(reason: string | null | undefined): string {
  if (!reason) {
    return 'Skipped';
  }
  return MAIL_SKIP_REASON_LABELS[reason] ?? reason.replaceAll('_', ' ');
}

/** "2 documents, 1 inline, 1 skipped" style summary from the parts manifest. */
export function mailPartsSummary(parts: MailPart[]): string {
  const jobCount = parts.filter((part) => part.outcome === 'job').length;
  const inlineCount = parts.filter((part) => part.outcome === 'inline').length;
  const skippedCount = parts.filter((part) => part.outcome === 'skipped').length;

  const segments: string[] = [];
  if (jobCount > 0) {
    segments.push(`${jobCount} document${jobCount === 1 ? '' : 's'}`);
  }
  if (inlineCount > 0) {
    segments.push(`${inlineCount} inline`);
  }
  if (skippedCount > 0) {
    segments.push(`${skippedCount} skipped`);
  }
  return segments.length > 0 ? segments.join(', ') : 'No attachments';
}

/**
 * Aggregate status chip for a message row. `job_status` is only ever
 * present when every job-outcome part carries it (the detail endpoint);
 * the list endpoint does not enrich parts with live job state, so this
 * degrades to a neutral, outcome-only summary there rather than guessing.
 */
export function mailAggregateStatus(parts: MailPart[]): { label: string; chipClass: string } {
  const jobParts = parts.filter((part) => part.outcome === 'job');
  const skippedCount = parts.filter((part) => part.outcome === 'skipped').length;

  if (jobParts.length === 0) {
    return parts.length === 0
      ? { label: 'Body only', chipClass: 'bg-slate-100 text-slate-700' }
      : { label: 'No attachments processed', chipClass: 'bg-slate-100 text-slate-700' };
  }

  const knownStatuses = jobParts.every((part) => typeof part.job_status === 'string');
  if (knownStatuses) {
    if (jobParts.some((part) => part.job_status === 'FAILED')) {
      return { label: 'Failed', chipClass: mailJobStatusChip.FAILED };
    }
    if (jobParts.some((part) => part.job_status === 'PENDING' || part.job_status === 'RUNNING')) {
      return { label: 'Processing', chipClass: mailJobStatusChip.RUNNING };
    }
    return { label: 'Finished', chipClass: mailJobStatusChip.FINISHED };
  }

  return skippedCount > 0
    ? { label: `${jobParts.length} attachment(s), ${skippedCount} skipped`, chipClass: 'bg-amber-100 text-amber-800' }
    : { label: `${jobParts.length} attachment(s)`, chipClass: 'bg-slate-100 text-slate-700' };
}

/** True while any job-outcome part is still PENDING/RUNNING — the detail page's poll condition. */
export function hasActiveMailParts(parts: MailPart[]): boolean {
  return parts.some(
    (part) => part.outcome === 'job' && (part.job_status === 'PENDING' || part.job_status === 'RUNNING'),
  );
}

/** Best display date for a message row: sent_at (parsed Date header) falling back to created_at (ingest time). */
export function mailDisplayDate(message: Pick<MailMessageBase, 'sent_at' | 'created_at'>): string {
  return message.sent_at ?? message.created_at;
}
