'use client';

import { Suspense, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { Cloud, ScanEye, UploadCloud } from 'lucide-react';

import { ConfluenceConnectionsTab } from '@/components/connections/confluence-connections-tab';
import { OpenWebUIConnectionsTab } from '@/components/connections/openwebui-connections-tab';
import { VlConnectionsPanel } from '@/components/connections/vl-connections-panel';

type TabId = 'confluence' | 'openwebui' | 'vl';

const TAB_IDS: TabId[] = ['confluence', 'openwebui', 'vl'];

const tabs: { id: TabId; label: string; icon: typeof Cloud }[] = [
  { id: 'confluence', label: 'Confluence', icon: Cloud },
  { id: 'openwebui', label: 'OpenWebUI', icon: UploadCloud },
  { id: 'vl', label: 'VL Connections', icon: ScanEye },
];

function isTabId(value: string | null): value is TabId {
  return value !== null && TAB_IDS.includes(value as TabId);
}

export default function ConnectionsPage() {
  return (
    <Suspense fallback={<main className="min-h-screen" />}>
      <ConnectionsPageInner />
    </Suspense>
  );
}

function ConnectionsPageInner() {
  const searchParams = useSearchParams();
  const initialTab = isTabId(searchParams.get('tab')) ? (searchParams.get('tab') as TabId) : 'confluence';
  const [tab, setTab] = useState<TabId>(initialTab);

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <header className="mb-6">
          <h1 className="text-2xl font-semibold text-slate-950">Connections</h1>
          <p className="mt-1 text-sm text-slate-500">
            Configure the external systems this account talks to: Confluence, OpenWebUI, and
            vision-language models.
          </p>
        </header>

        <div
          role="tablist"
          aria-label="Connection sections"
          className="mb-6 inline-flex flex-wrap gap-1 rounded-2xl border border-slate-200 bg-white p-1 shadow-sm"
        >
          {tabs.map(({ id, label, icon: Icon }) => {
            const active = tab === id;
            return (
              <button
                key={id}
                role="tab"
                aria-selected={active}
                onClick={() => setTab(id)}
                className={`flex items-center gap-2 rounded-xl px-4 py-2 text-sm font-medium transition ${
                  active
                    ? 'bg-emerald-50 text-emerald-800'
                    : 'text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                }`}
              >
                <Icon className={`h-4 w-4 ${active ? 'text-emerald-700' : 'text-slate-400'}`} />
                {label}
              </button>
            );
          })}
        </div>

        {tab === 'confluence' && <ConfluenceConnectionsTab />}
        {tab === 'openwebui' && <OpenWebUIConnectionsTab />}
        {tab === 'vl' && <VlConnectionsPanel />}
      </div>
    </main>
  );
}
