'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { motion } from 'framer-motion';
import { CheckCircle2, Info, LoaderCircle, RefreshCcw, UploadCloud, XCircle } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ApiError, apiJson } from '@/lib/api';
import { API, UploadError, formatBytes, sendFormDataWithProgress } from '@/components/dashboard/shared';
import {
  mailAggregateStatus,
  mailDisplayDate,
  mailPartsSummary,
  type MailIngestResponse,
  type MailMessage,
  type MailMessageListResponse,
} from '@/lib/mail';

const PAGE_SIZE = 50;

type Filters = {
  query: string;
  source: string;
  fromDate: string;
  toDate: string;
};

const EMPTY_FILTERS: Filters = { query: '', source: '', fromDate: '', toDate: '' };

// Manual .eml upload (frontend-only feature): each file is POSTed one at a
// time against the existing multipart branch of POST /api/v1/mail/messages.
// 201 = newly ingested, 200 + replayed:true = already known (dedup hit) —
// both are "success" for navigation purposes, just labeled differently.
type MailUploadResultKind = 'success' | 'replayed' | 'error';

type MailUploadResult = {
  fileName: string;
  kind: MailUploadResultKind;
  id?: string;
  subject?: string;
  message?: string;
};

type MailUploadProgress = {
  currentFile: string;
  filesCompleted: number;
  filesTotal: number;
  bytesLoaded: number;
  bytesTotal: number;
};

export default function MailPage() {
  const router = useRouter();

  const [items, setItems] = useState<MailMessage[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [query, setQuery] = useState('');
  const [source, setSource] = useState('');
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');

  const [uploading, setUploading] = useState(false);
  const [uploadDragActive, setUploadDragActive] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<MailUploadProgress | null>(null);
  const [uploadResults, setUploadResults] = useState<MailUploadResult[]>([]);
  const emailFileInputRef = useRef<HTMLInputElement>(null);

  // `filters`/`offsetOverride` let callers (Reset, pagination) fetch with
  // values other than this render's state, since setters only land on the
  // next render — same pattern as document-browser.tsx's loadItems.
  //
  // Deliberately does not flip `loading` on here (only off, in `finally`):
  // the mount effect below calls this directly, and setting state
  // synchronously before the first await inside an effect-invoked function
  // trips the set-state-in-effect lint rule. `loading` starts `true`, and
  // every other call site (Refresh, Apply, Reset, pagination) flips it on
  // itself before calling in — same convention as admin/logs-tab.tsx.
  const loadItems = async (filters?: Filters, offsetOverride?: number) => {
    const active = filters ?? { query, source, fromDate, toDate };
    const nextOffset = offsetOverride ?? offset;
    const params = new URLSearchParams();
    if (active.query.trim()) {
      params.set('q', active.query.trim());
    }
    if (active.source.trim()) {
      params.set('source', active.source.trim());
    }
    if (active.fromDate) {
      params.set('from_date', active.fromDate);
    }
    if (active.toDate) {
      params.set('to_date', active.toDate);
    }
    params.set('limit', String(PAGE_SIZE));
    params.set('offset', String(nextOffset));

    try {
      const payload = await apiJson<MailMessageListResponse>(`/api/v1/mail/messages?${params.toString()}`, {
        cache: 'no-store',
      });
      setItems(payload.items);
      setTotal(payload.total);
      setOffset(nextOffset);
      setLoadError(null);
    } catch (error) {
      setLoadError(error instanceof ApiError ? error.detail : 'Failed to load mail messages.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const run = async () => {
      await loadItems(EMPTY_FILTERS, 0);
    };
    void run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const applyFilters = () => {
    setLoading(true);
    void loadItems(undefined, 0);
  };

  const resetFilters = () => {
    setQuery('');
    setSource('');
    setFromDate('');
    setToDate('');
    setLoading(true);
    void loadItems(EMPTY_FILTERS, 0);
  };

  const goToOffset = (nextOffset: number) => {
    setLoading(true);
    void loadItems(undefined, nextOffset);
  };

  // Uploads each selected .eml file one at a time against the existing
  // multipart branch of POST /api/v1/mail/messages (field name `file`, same
  // as the backend's convenience mode for curl -F / n8n form mode) —
  // source=ui-upload identifies the origin. A per-file failure (413/422/etc.)
  // is recorded and the loop continues; it must not abort the remaining
  // queue. sendFormDataWithProgress resolves on any 2xx (both 201 and the
  // 200 dedup-replay share the same response shape) and rejects with
  // UploadError — whose `message` is already the parsed backend `detail` —
  // on everything else.
  const uploadEmailFiles = async (fileList: FileList | File[]) => {
    const files = Array.from(fileList);
    if (uploading || files.length === 0) {
      return;
    }
    setUploading(true);
    setUploadResults([]);

    const results: MailUploadResult[] = [];
    const totalBytes = files.reduce((sum, file) => sum + (file.size || 0), 0) || 1;
    let completedBytes = 0;

    for (const [index, file] of files.entries()) {
      setUploadProgress({
        currentFile: file.name,
        filesCompleted: index,
        filesTotal: files.length,
        bytesLoaded: completedBytes,
        bytesTotal: totalBytes,
      });
      const formData = new FormData();
      formData.append('file', file);
      try {
        const response = (await sendFormDataWithProgress(
          `${API}/api/v1/mail/messages?source=ui-upload`,
          formData,
          (loaded, total) => {
            setUploadProgress({
              currentFile: file.name,
              filesCompleted: index,
              filesTotal: files.length,
              bytesLoaded: completedBytes + loaded,
              bytesTotal: totalBytes || total || 1,
            });
          },
        )) as MailIngestResponse;
        results.push({
          fileName: file.name,
          kind: response.replayed ? 'replayed' : 'success',
          id: response.id,
          subject: response.subject,
        });
      } catch (error) {
        const detail = error instanceof UploadError ? error.message : 'Upload failed.';
        results.push({ fileName: file.name, kind: 'error', message: detail });
      }
      completedBytes += file.size || 0;
    }

    setUploadResults(results);
    setUploadProgress(null);
    setUploading(false);

    setLoading(true);
    await loadItems();

    // A nice touch, not a requirement: a single uploaded file that ingested
    // successfully (new or replayed) drops the user straight into it.
    const onlyResult = files.length === 1 ? results[0] : undefined;
    if (onlyResult && onlyResult.kind !== 'error' && onlyResult.id) {
      router.push(`/mail/${onlyResult.id}`);
    }
  };

  const onDropEmailFiles = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setUploadDragActive(false);
    const files = event.dataTransfer.files;
    if (files && files.length > 0) {
      void uploadEmailFiles(files);
    }
  };

  const uploadPercent = uploadProgress
    ? Math.min(100, Math.round((uploadProgress.bytesLoaded / uploadProgress.bytesTotal) * 100))
    : 0;

  const rangeStart = items.length === 0 ? 0 : offset + 1;
  const rangeEnd = offset + items.length;

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-6xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
        <section className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-3xl font-semibold">Mail</h1>
            <p className="mt-2 text-slate-600">
              Ingested email messages, their body, and the attachment jobs derived from them.
            </p>
          </div>
          <Button
            variant="outline"
            onClick={() => {
              setLoading(true);
              void loadItems();
            }}
            disabled={loading}
          >
            <RefreshCcw className="mr-2 h-4 w-4" /> Refresh
          </Button>
        </section>

        {loadError && (
          <p className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{loadError}</p>
        )}

        <section className="mb-6 rounded-3xl border border-slate-200 bg-white p-4 sm:p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <div className="mb-3 flex items-center justify-between gap-4">
            <div>
              <h2 className="text-lg font-semibold">Upload email</h2>
              <p className="mt-1 text-sm text-slate-600">Ingest one or more raw .eml files, the same way a mail gateway would.</p>
            </div>
            <Button variant="outline" size="sm" onClick={() => emailFileInputRef.current?.click()} disabled={uploading}>
              <UploadCloud className="mr-2 h-4 w-4" /> Select file(s)
            </Button>
          </div>
          <motion.div
            onDrop={onDropEmailFiles}
            onDragOver={(event) => event.preventDefault()}
            onDragEnter={() => setUploadDragActive(true)}
            onDragLeave={() => setUploadDragActive(false)}
            className="rounded-xl border border-dashed border-slate-200 bg-slate-50 p-8 text-center"
            animate={{ borderColor: uploadDragActive ? '#6ee7b7' : '#10b981' }}
          >
            <UploadCloud className="mx-auto mb-3 h-8 w-8 text-slate-600" />
            <p className="mb-1 text-sm font-medium text-slate-950">Drag and drop .eml file(s) here</p>
            <p className="text-xs text-slate-600">Or use &quot;Select file(s)&quot; above. Multiple files upload one at a time.</p>
            <input
              ref={emailFileInputRef}
              type="file"
              multiple
              accept=".eml,message/rfc822"
              className="hidden"
              onChange={(event) => {
                const files = event.currentTarget.files;
                if (files && files.length > 0) {
                  void uploadEmailFiles(files);
                }
                event.currentTarget.value = '';
              }}
            />
          </motion.div>

          {uploadProgress && (
            <div className="mt-4 rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-700">
              <div className="flex items-center justify-between gap-4">
                <div className="min-w-0">
                  <p className="font-medium text-slate-950">Uploading email</p>
                  <p className="truncate text-xs text-slate-500">{uploadProgress.currentFile}</p>
                </div>
                <p className="text-xs font-semibold text-slate-600">
                  {uploadProgress.filesCompleted}/{uploadProgress.filesTotal} files
                </p>
              </div>
              <div className="mt-3 h-2 overflow-hidden rounded-full bg-slate-100">
                <div className="h-full rounded-full bg-emerald-500 transition-all" style={{ width: `${uploadPercent}%` }} />
              </div>
              <div className="mt-2 flex items-center justify-between text-xs text-slate-500">
                <span>{uploadPercent}%</span>
                <span>
                  {formatBytes(uploadProgress.bytesLoaded)} / {formatBytes(uploadProgress.bytesTotal)}
                </span>
              </div>
            </div>
          )}

          {uploadResults.length > 0 && (
            <ul className="mt-4 space-y-2 text-sm">
              {uploadResults.map((result, index) => (
                <li
                  key={`${result.fileName}-${index}`}
                  className="flex items-start gap-2 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2"
                >
                  {result.kind === 'success' && (
                    <CheckCircle2 className="mt-0.5 h-4 w-4 flex-shrink-0 text-emerald-700" />
                  )}
                  {result.kind === 'replayed' && <Info className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-700" />}
                  {result.kind === 'error' && <XCircle className="mt-0.5 h-4 w-4 flex-shrink-0 text-red-600" />}
                  <div className="min-w-0">
                    <p className="truncate font-medium text-slate-950">{result.fileName}</p>
                    {result.kind === 'success' && result.id && (
                      <p className="text-xs text-emerald-700">
                        Ingested as{' '}
                        <Link href={`/mail/${result.id}`} className="underline hover:text-emerald-800">
                          {result.subject?.trim() || '(no subject)'}
                        </Link>
                      </p>
                    )}
                    {result.kind === 'replayed' && result.id && (
                      <p className="text-xs text-amber-700">
                        Already ingested —{' '}
                        <Link href={`/mail/${result.id}`} className="underline hover:text-amber-800">
                          view existing message
                        </Link>
                      </p>
                    )}
                    {result.kind === 'error' && <p className="text-xs text-red-600">{result.message}</p>}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="mb-6 rounded-3xl border border-slate-200 bg-white p-4 sm:p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <label className="text-sm text-slate-700 xl:col-span-2">
              Search subject / from
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                onKeyDown={(event) => event.key === 'Enter' && applyFilters()}
                className="mt-1 w-full rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950 outline-none transition placeholder:text-slate-400 focus:border-emerald-300 focus:bg-white"
                placeholder="quarterly report, alice@partner.example"
              />
            </label>
            <label className="text-sm text-slate-700">
              Source
              <input
                value={source}
                onChange={(event) => setSource(event.target.value)}
                onKeyDown={(event) => event.key === 'Enter' && applyFilters()}
                className="mt-1 w-full rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950 outline-none transition placeholder:text-slate-400 focus:border-emerald-300 focus:bg-white"
                placeholder="mail-gateway, n8n"
              />
            </label>
            <div />
            <label className="text-sm text-slate-700">
              From date
              <input
                type="date"
                value={fromDate}
                onChange={(event) => setFromDate(event.target.value)}
                className="mt-1 w-full rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950 outline-none transition focus:border-emerald-300 focus:bg-white"
              />
            </label>
            <label className="text-sm text-slate-700">
              To date
              <input
                type="date"
                value={toDate}
                onChange={(event) => setToDate(event.target.value)}
                className="mt-1 w-full rounded-2xl border border-slate-200 bg-slate-50 px-3 py-2 text-slate-950 outline-none transition focus:border-emerald-300 focus:bg-white"
              />
            </label>
          </div>
          <div className="mt-4 flex flex-wrap gap-2">
            <Button onClick={applyFilters}>Apply Filters</Button>
            <Button variant="outline" onClick={resetFilters}>
              Reset
            </Button>
          </div>
        </section>

        <section className="rounded-3xl border border-slate-200 bg-white p-4 sm:p-5 shadow-[0_20px_60px_rgba(15,23,42,0.05)]">
          <div className="mb-3 flex items-center justify-between gap-4">
            <h2 className="text-lg font-semibold">Messages</h2>
            <p className="text-sm text-slate-500">{total} message(s)</p>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full table-auto text-left text-xs sm:text-sm">
              <thead className="text-slate-500">
                <tr>
                  <th className="pb-2 font-medium">Subject</th>
                  <th className="pb-2 font-medium">From</th>
                  <th className="hidden pb-2 font-medium md:table-cell">Date</th>
                  <th className="hidden pb-2 font-medium sm:table-cell">Source</th>
                  <th className="pb-2 font-medium">Parts</th>
                  <th className="pb-2 font-medium">Status</th>
                </tr>
              </thead>
              <tbody>
                {items.map((message) => {
                  const status = mailAggregateStatus(message.parts);
                  return (
                    <tr key={message.id} className="border-t border-slate-100">
                      <td className="py-3">
                        <Link
                          href={`/mail/${message.id}`}
                          className="line-clamp-2 font-medium text-slate-950 hover:text-emerald-700"
                        >
                          {message.subject.trim() || '(no subject)'}
                        </Link>
                        {message.rfc_message_id && (
                          <p className="mt-1 truncate text-xs text-slate-500" title={message.rfc_message_id}>
                            {message.rfc_message_id}
                          </p>
                        )}
                      </td>
                      <td className="py-3 text-slate-700">{message.from_address || '-'}</td>
                      <td className="hidden py-3 text-slate-700 md:table-cell">
                        {new Date(mailDisplayDate(message)).toLocaleString()}
                      </td>
                      <td className="hidden py-3 text-slate-700 sm:table-cell">{message.source || '-'}</td>
                      <td className="py-3 text-slate-700">{mailPartsSummary(message.parts)}</td>
                      <td className="py-3">
                        <span className={`rounded px-2 py-1 text-xs ${status.chipClass}`}>{status.label}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {items.length === 0 && !loading && (
              <p className="py-6 text-sm text-slate-600">No mail messages match the current filters.</p>
            )}
            {loading && (
              <div className="flex items-center gap-2 py-6 text-sm text-slate-600">
                <LoaderCircle className="h-4 w-4 animate-spin" /> Loading mail...
              </div>
            )}
            {items.length > 0 && (
              <div className="mt-4 flex items-center justify-between gap-3 text-sm text-slate-600">
                <p>
                  Showing {rangeStart}-{rangeEnd} of {total}
                </p>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={offset === 0 || loading}
                    onClick={() => goToOffset(Math.max(0, offset - PAGE_SIZE))}
                  >
                    Previous
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={rangeEnd >= total || loading}
                    onClick={() => goToOffset(offset + PAGE_SIZE)}
                  >
                    Next
                  </Button>
                </div>
              </div>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
