'use client';

import { useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { Cpu, KeyRound, ScanEye, ShieldAlert, Terminal, Users, UsersRound } from 'lucide-react';

import { useAuth } from '@/lib/auth-context';
import { UsersTab } from '@/components/admin/users-tab';
import { TeamsTab } from '@/components/admin/teams-tab';
import { ProvidersTab } from '@/components/admin/providers-tab';
import { LogsTab } from '@/components/admin/logs-tab';
import { VlConnectionsTab } from '@/components/admin/vl-connections-tab';
import { PaddleTab } from '@/components/admin/paddle-tab';

type TabId = 'users' | 'teams' | 'providers' | 'logs' | 'vl-connections' | 'paddle';

const tabs: { id: TabId; label: string; icon: typeof Users }[] = [
  { id: 'users', label: 'Users', icon: Users },
  { id: 'teams', label: 'Teams', icon: UsersRound },
  { id: 'providers', label: 'Identity Providers', icon: KeyRound },
  { id: 'logs', label: 'Logs', icon: Terminal },
  { id: 'vl-connections', label: 'VL Connections', icon: ScanEye },
  // Placed after VL Connections: both are runtime/processing configuration
  // (as opposed to Users/Teams/Providers, which are account administration).
  { id: 'paddle', label: 'Paddle', icon: Cpu },
];

const TAB_IDS: TabId[] = tabs.map((t) => t.id);

export default function AdminPage() {
  const { user } = useAuth();
  const [tab, setTab] = useState<TabId>('users');
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

  if (!user || user.role !== 'admin') {
    return (
      <main className="min-h-screen">
        <div className="mx-auto flex w-full max-w-7xl flex-col items-center px-4 py-24 sm:px-6 lg:px-8">
          <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-slate-100">
            <ShieldAlert className="h-6 w-6 text-slate-400" />
          </div>
          <h1 className="mt-4 text-lg font-semibold text-slate-950">Admin privileges required</h1>
          <p className="mt-1 max-w-md text-center text-sm text-slate-500">
            This area is restricted to administrators. Ask an admin to grant you the admin role if
            you need access.
          </p>
        </div>
      </main>
    );
  }

  return (
    <main className="min-h-screen">
      <div className="mx-auto w-full max-w-7xl px-4 py-8 sm:px-6 lg:px-8">
        <header className="mb-6">
          <h1 className="text-3xl font-semibold text-slate-950">Administration</h1>
          <p className="mt-1 text-sm text-slate-500">
            Manage accounts, access, and runtime configuration for your PaddleDoc deployment.
          </p>
        </header>

        <div
          role="tablist"
          aria-label="Admin sections"
          className="mb-6 inline-flex flex-wrap gap-1 rounded-2xl border border-slate-200 bg-white p-1 shadow-sm"
        >
          {tabs.map(({ id, label, icon: Icon }) => {
            const active = tab === id;
            return (
              <button
                key={id}
                ref={(el) => {
                  tabRefs.current[id] = el;
                }}
                id={`admin-tab-${id}`}
                role="tab"
                aria-selected={active}
                aria-controls={`admin-panel-${id}`}
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

        {tab === 'users' && (
          <div role="tabpanel" id="admin-panel-users" aria-labelledby="admin-tab-users">
            <UsersTab />
          </div>
        )}
        {tab === 'teams' && (
          <div role="tabpanel" id="admin-panel-teams" aria-labelledby="admin-tab-teams">
            <TeamsTab />
          </div>
        )}
        {tab === 'providers' && (
          <div role="tabpanel" id="admin-panel-providers" aria-labelledby="admin-tab-providers">
            <ProvidersTab />
          </div>
        )}
        {tab === 'logs' && (
          <div role="tabpanel" id="admin-panel-logs" aria-labelledby="admin-tab-logs">
            <LogsTab />
          </div>
        )}
        {tab === 'vl-connections' && (
          <div
            role="tabpanel"
            id="admin-panel-vl-connections"
            aria-labelledby="admin-tab-vl-connections"
          >
            <VlConnectionsTab />
          </div>
        )}
        {tab === 'paddle' && (
          <div role="tabpanel" id="admin-panel-paddle" aria-labelledby="admin-tab-paddle">
            <PaddleTab />
          </div>
        )}
      </div>
    </main>
  );
}
