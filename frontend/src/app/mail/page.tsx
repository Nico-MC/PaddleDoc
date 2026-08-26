'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { Inbox, LoaderCircle, RefreshCcw, Search, SlidersHorizontal } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ApiError, apiJson } from '@/lib/api';
import {
  mailAggregateStatus,
  mailDisplayDate,
  mailPartsSummary,
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
export default function MailPage() {
  const [items, setItems] = useState<MailMessage[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [query, setQuery] = useState('');
  const [source, setSource] = useState('');
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');
  const [filtersOpen, setFiltersOpen] = useState(false);

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
      // Load only API-ingested mails (source=api)
      await loadItems({ query: '', source: 'api', fromDate: '', toDate: '' }, 0);
      // Update the source filter display to api
      setSource('api');
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
    setSource('api');
    setFromDate('');
    setToDate('');
    setLoading(true);
    void loadItems({ query: '', source: 'api', fromDate: '', toDate: '' }, 0);
  };

  const goToOffset = (nextOffset: number) => {
    setLoading(true);
    void loadItems(undefined, nextOffset);
  };

  const rangeStart = items.length === 0 ? 0 : offset + 1;
  const rangeEnd = offset + items.length;

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-6xl px-4 py-8 text-slate-950 sm:px-6 lg:px-8">
        <section className="mb-6 flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 className="text-3xl font-semibold">Mail</h1>
            <p className="mt-2 text-slate-600">
              API-ingested email messages
            </p>
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="h-9 w-9 shrink-0 self-start px-0 sm:self-auto"
            aria-label="Refresh"
            title="Refresh"
            onClick={() => {
              setLoading(true);
              void loadItems();
            }}
            disabled={loading}
          >
            <RefreshCcw className="h-4 w-4" />
          </Button>
        </section>

        {loadError && (
          <p role="alert" className="mb-4 rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
            {loadError}
          </p>
        )}

        <section className="mb-3 flex flex-col gap-2 sm:flex-row">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-4 top-1/2 h-5 w-5 -translate-y-1/2 text-slate-400" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => event.key === 'Enter' && applyFilters()}
              className="w-full rounded-2xl border border-slate-200 bg-slate-50 py-3 pl-12 pr-4 text-base text-slate-950 outline-none transition placeholder:text-slate-400 focus:border-emerald-300 focus:bg-white"
              placeholder="Search subject or sender…"
            />
          </div>
          <Button
            variant="outline"
            aria-expanded={filtersOpen}
            onClick={() => setFiltersOpen((value) => !value)}
            className="shrink-0"
          >
            <SlidersHorizontal className="mr-2 h-4 w-4" /> Filters
          </Button>
        </section>

        {filtersOpen && (
          <section className="mb-6 rounded-3xl border border-slate-200 bg-white p-4 sm:p-5">
            <div className="grid gap-3 sm:grid-cols-3">
              <label className="text-sm text-slate-700">
                Source
                <input
                  value={source}
                  readOnly
                  className="mt-1 w-full rounded-2xl border border-slate-200 bg-slate-100 px-3 py-2 text-slate-950 outline-none cursor-not-allowed"
                  title="This page displays API-ingested messages only"
                />
              </label>
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
        )}

        <section className="rounded-3xl border border-slate-200 bg-white p-4 sm:p-5">
          <div className="mb-3 flex items-center justify-between gap-4">
            <h2 className="text-lg font-semibold">Messages</h2>
            <p className="text-sm text-slate-500">{total} message(s)</p>
          </div>
          <div>
            {items.map((message) => {
              const status = mailAggregateStatus(message.parts);
              return (
                <Link
                  key={message.id}
                  href={`/mail/${message.id}`}
                  className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 border-t border-slate-100 py-3 first:border-t-0 hover:bg-slate-50"
                >
                  <div className="min-w-0 flex-1 basis-64">
                    <p className="font-medium text-slate-950">{message.from_address || '-'}</p>
                    <p className="truncate font-semibold text-slate-950">
                      {message.subject.trim() || '(no subject)'}
                    </p>
                    <p className="truncate text-sm text-slate-500">{mailPartsSummary(message.parts)}</p>
                  </div>
                  <div className="flex shrink-0 flex-col items-end gap-1">
                    <p className="whitespace-nowrap text-xs text-slate-500">
                      {new Date(mailDisplayDate(message)).toLocaleString()}
                    </p>
                    <span className={`whitespace-nowrap rounded px-2 py-1 text-xs ${status.chipClass}`}>
                      {status.label}
                    </span>
                  </div>
                </Link>
              );
            })}
            {items.length === 0 && !loading && (
              <div className="flex flex-col items-center gap-3 py-10 text-center">
                <Inbox className="h-8 w-8 text-slate-300" />
                <p className="text-sm text-slate-500">No ingested messages yet.</p>
                <p className="text-sm text-slate-500">
                  Send raw emails to{' '}
                  <code className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-xs text-slate-700">
                    POST /api/v1/mail/messages
                  </code>{' '}
                  — from an SMTP gateway, n8n, or a script — and they appear here.
                </p>
              </div>
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
