'use client';

import { FileText } from 'lucide-react';

import { CrawlerTab } from '@/components/evaluation/crawler-tab';

export default function HanseMerkurEvaluationPage() {
  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <header className="mb-6 flex items-start gap-3">
          <span className="mt-1 flex h-10 w-10 items-center justify-center rounded-xl bg-emerald-50 text-emerald-700">
            <FileText className="h-5 w-5" />
          </span>
          <div>
            <h1 className="text-2xl font-semibold text-slate-950">Crawler</h1>
            <p className="mt-1 text-sm text-slate-500">Discover, select, and prepare PDF/Word sources for RAG evaluation.</p>
          </div>
        </header>
        <CrawlerTab />
      </div>
    </main>
  );
}
