'use client';

import { Fragment, Suspense, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { useSearchParams } from 'next/navigation';
import { Cloud, ScanEye, UploadCloud } from 'lucide-react';

import { ConfluenceConnectionsTab } from '@/components/connections/confluence-connections-tab';
import { OpenWebUIConnectionsTab } from '@/components/connections/openwebui-connections-tab';
import { VlConnectionsPanel } from '@/components/connections/vl-connections-panel';

type TabId = 'confluence' | 'openwebui' | 'vl';

type TabDef = { id: TabId; label: string; icon: typeof Cloud };

const TAB_GROUPS: { label: string; tabs: TabDef[] }[] = [
  {
    label: 'External services',
    tabs: [
      { id: 'confluence', label: 'Confluence', icon: Cloud },
      { id: 'openwebui', label: 'OpenWebUI', icon: UploadCloud },
    ],
  },
  {
    label: 'AI models',
    tabs: [{ id: 'vl', label: 'VL Models', icon: ScanEye }],
  },
];

const TAB_IDS: TabId[] = TAB_GROUPS.flatMap((group) => group.tabs.map((t) => t.id));

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
  const tabRefs = useRef<Partial<Record<TabId, HTMLButtonElement | null>>>({});

  const focusTab = (id: TabId) => {
    setTab(id);
    tabRefs.current[id]?.focus();
  };

  const handleTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, id: TabId) => {
    const index = TAB_IDS.indexOf(id);
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      focusTab(TAB_IDS[(index + 1) % TAB_IDS.length]);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      focusTab(TAB_IDS[(index - 1 + TAB_IDS.length) % TAB_IDS.length]);
    } else if (event.key === 'Home') {
      event.preventDefault();
      focusTab(TAB_IDS[0]);
    } else if (event.key === 'End') {
      event.preventDefault();
      focusTab(TAB_IDS[TAB_IDS.length - 1]);
    }
  };

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <header className="mb-6">
          <h1 className="text-3xl font-semibold text-slate-950">Connections</h1>
          <p className="mt-1 text-sm text-slate-500">
            Configure the external systems and AI models this account talks to.
          </p>
        </header>

        <div
          role="tablist"
          aria-label="Connection sections"
          className="mb-6 flex flex-wrap items-center gap-3"
        >
          {TAB_GROUPS.map((group, groupIndex) => (
            <Fragment key={group.label}>
              {groupIndex > 0 && (
                <div className="hidden h-8 w-px bg-slate-200 sm:block" aria-hidden="true" />
              )}
              <div className="flex items-center gap-1.5 rounded-2xl border border-slate-200 bg-white p-1 shadow-sm">
                <span
                  className="pl-2 text-[11px] font-medium uppercase tracking-wide text-slate-400"
                  aria-hidden="true"
                >
                  {group.label}
                </span>
                {group.tabs.map(({ id, label, icon: Icon }) => {
                  const active = tab === id;
                  return (
                    <button
                      key={id}
                      ref={(el) => {
                        tabRefs.current[id] = el;
                      }}
                      id={`connections-tab-${id}`}
                      role="tab"
                      aria-selected={active}
                      aria-controls={`connections-panel-${id}`}
                      tabIndex={active ? 0 : -1}
                      onClick={() => setTab(id)}
                      onKeyDown={(event) => handleTabKeyDown(event, id)}
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
            </Fragment>
          ))}
        </div>

        {tab === 'confluence' && (
          <div
            role="tabpanel"
            id="connections-panel-confluence"
            aria-labelledby="connections-tab-confluence"
          >
            <ConfluenceConnectionsTab />
          </div>
        )}
        {tab === 'openwebui' && (
          <div
            role="tabpanel"
            id="connections-panel-openwebui"
            aria-labelledby="connections-tab-openwebui"
          >
            <OpenWebUIConnectionsTab />
          </div>
        )}
        {tab === 'vl' && (
          <div role="tabpanel" id="connections-panel-vl" aria-labelledby="connections-tab-vl">
            <VlConnectionsPanel />
          </div>
        )}
      </div>
    </main>
  );
}
