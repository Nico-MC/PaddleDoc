'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import {
  Home,
  Menu,
  X,
  Cpu,
  FilePlus,
  FolderOpen,
  Gauge,
  Inbox,
  FileInput,
  PlugZap,
  Settings,
  Shield,
  LogOut,
  ChevronDown,
  ChevronRight,
} from 'lucide-react';
import { useAuth } from '@/lib/auth-context';
import { PaddleDocLogo } from '@/components/paddledoc-logo';

const processingChildren = [
  { href: '/processing/new', label: 'File Task', icon: FilePlus, description: 'Upload files for processing' },
  { href: '/jobs', label: 'Jobs', icon: FolderOpen, description: 'View processing jobs' },
  { href: '/imports', label: 'Imports', icon: FileInput, description: 'Confluence page imports' },
];

const navGroups = [
  {
    title: 'Workspace',
    items: [
      { href: '/', label: 'Home', icon: Home },
      {
        href: '/processing',
        label: 'Processing',
        icon: Cpu,
        description: 'Upload and process documents',
        children: processingChildren,
      },
      { href: '/mail', label: 'Mail API', icon: Inbox, description: 'API-ingested messages' },
    ],
  },
  {
    title: 'Analyze & connect',
    items: [
      { href: '/benchmark', label: 'VL Benchmark', icon: Gauge },
      { href: '/connections', label: 'Connections', icon: PlugZap },
    ],
  },
];

// Routes that should auto-expand the Processing submenu.
const processingRoutes = ['/processing', '/jobs', '/imports'];

function isChildActive(href: string, pathname: string) {
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function SidebarNav() {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const drawerRef = useRef<HTMLDivElement>(null);
  const { user, logout } = useAuth();

  // The user's explicit collapse/expand of the Processing submenu wins while
  // they stay on the same side of the section boundary; crossing it (in or
  // out) hands control back to the route, so navigating to a child always
  // reveals the submenu. OR-ing the two instead would pin it open for as long
  // as a child route is active, leaving the chevron inert exactly where a user
  // would reach for it. Adjusted during render rather than mirrored in an
  // effect — https://react.dev/learn/you-might-not-need-an-effect
  const autoProcessingOpen = processingRoutes.some((route) => isChildActive(route, pathname));
  const [submenuOpen, setSubmenuOpen] = useState(autoProcessingOpen);
  const [lastAutoProcessingOpen, setLastAutoProcessingOpen] = useState(autoProcessingOpen);
  if (autoProcessingOpen !== lastAutoProcessingOpen) {
    setLastAutoProcessingOpen(autoProcessingOpen);
    setSubmenuOpen(autoProcessingOpen);
  }

  // Close on outside click
  useEffect(() => {
    function onPointerDown(e: PointerEvent) {
      if (open && drawerRef.current && !drawerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('pointerdown', onPointerDown);
    return () => document.removeEventListener('pointerdown', onPointerDown);
  }, [open]);

  // Close on Escape
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false);
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, []);

  return (
    <>
      {/* Burger button — fixed top-left */}
      <button
        onClick={() => setOpen((v) => !v)}
        aria-label={open ? 'Close navigation' : 'Open navigation'}
        className="fixed left-4 top-4 z-50 flex h-9 w-9 items-center justify-center rounded-xl border border-slate-200 bg-white shadow-md transition hover:bg-slate-50 lg:hidden"
      >
        {open ? <X className="h-4 w-4 text-slate-700" /> : <Menu className="h-4 w-4 text-slate-700" />}
      </button>

      {/* Backdrop */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-slate-950/30 backdrop-blur-sm lg:hidden"
          aria-hidden="true"
        />
      )}

      {/* Drawer */}
      <div
        ref={drawerRef}
        className={`fixed left-0 top-0 z-40 flex h-full w-64 flex-col bg-white shadow-2xl transition-transform duration-200 lg:translate-x-0 lg:border-r lg:border-slate-100 lg:shadow-none ${
          open ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="flex items-center gap-3 border-b border-slate-100 px-5 py-4">
          <PaddleDocLogo className="h-8 w-8" />
          <span className="text-base font-semibold text-slate-950">PaddleDoc</span>
        </div>

        <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-3 py-4">
          {navGroups.map((group, groupIndex) => (
            <div key={group.title} className={groupIndex > 0 ? 'mt-4' : undefined}>
              <p className="px-3 pb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                {group.title}
              </p>
              <div className="flex flex-col gap-1">
                {group.items.map(({ href, label, icon: Icon, description, children }) => {
                  const childActive = children?.some((child) => isChildActive(child.href, pathname)) ?? false;
                  // An entry with children (e.g. Processing) must only read as
                  // active on an exact pathname match — startsWith(href) would
                  // also fire for every child route (e.g. /processing/new),
                  // double-highlighting parent and child at once.
                  const active = children
                    ? pathname === href
                    : pathname === href || (href !== '/' && pathname.startsWith(href));

                  if (!children) {
                    return (
                      <Link
                        key={href}
                        href={href}
                        aria-current={active ? 'page' : undefined}
                        onClick={() => setOpen(false)}
                        title={description}
                        className={`relative flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition ${
                          active
                            ? 'bg-emerald-50 font-semibold text-emerald-800'
                            : 'font-medium text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                        }`}
                      >
                        {active && (
                          <span
                            aria-hidden="true"
                            className="absolute inset-y-1.5 left-0 w-[3px] rounded-r-full bg-emerald-600"
                          />
                        )}
                        <Icon className={`h-5 w-5 flex-shrink-0 ${active ? 'text-emerald-700' : 'text-slate-400'}`} />
                        {label}
                      </Link>
                    );
                  }

                  return (
                    <div key={href}>
                      <div
                        className={`relative flex items-center gap-1 rounded-xl pr-1 text-sm transition ${
                          active
                            ? 'bg-emerald-50 font-semibold text-emerald-800'
                            : childActive
                              ? 'font-medium text-emerald-700'
                              : 'font-medium text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                        }`}
                      >
                        {active && (
                          <span
                            aria-hidden="true"
                            className="absolute inset-y-1.5 left-0 w-[3px] rounded-r-full bg-emerald-600"
                          />
                        )}
                        <Link
                          href={href}
                          aria-current={active ? 'page' : undefined}
                          onClick={() => setOpen(false)}
                          title={description}
                          className="flex flex-1 items-center gap-3 rounded-xl px-3 py-2.5"
                        >
                          <Icon
                            className={`h-5 w-5 flex-shrink-0 ${
                              active || childActive ? 'text-emerald-700' : 'text-slate-400'
                            }`}
                          />
                          {label}
                        </Link>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            setSubmenuOpen((v) => !v);
                          }}
                          aria-expanded={submenuOpen}
                          aria-label={submenuOpen ? `Collapse ${label} submenu` : `Expand ${label} submenu`}
                          className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
                        >
                          {submenuOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                        </button>
                      </div>
                      {submenuOpen && (
                        <div className="mt-1 flex flex-col gap-1">
                          {children.map((child) => {
                            const childIsActive = isChildActive(child.href, pathname);
                            return (
                              <Link
                                key={child.href}
                                href={child.href}
                                aria-current={childIsActive ? 'page' : undefined}
                                onClick={() => setOpen(false)}
                                title={child.description}
                                className={`relative flex items-center gap-3 rounded-xl py-2 pl-10 pr-3 text-sm transition ${
                                  childIsActive
                                    ? 'bg-emerald-50 font-semibold text-emerald-800'
                                    : 'font-medium text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                                }`}
                              >
                                {childIsActive && (
                                  <span
                                    aria-hidden="true"
                                    className="absolute inset-y-1.5 left-0 w-[3px] rounded-r-full bg-emerald-600"
                                  />
                                )}
                                <child.icon
                                  className={`h-4 w-4 flex-shrink-0 ${childIsActive ? 'text-emerald-700' : 'text-slate-400'}`}
                                />
                                {child.label}
                              </Link>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>

        <div className="border-t border-slate-100 px-3 py-3">
          {user ? (
            <>
              <Link
                href="/settings"
                aria-current={pathname === '/settings' || pathname.startsWith('/settings/') ? 'page' : undefined}
                onClick={() => setOpen(false)}
                className={`relative mb-2 flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition ${
                  pathname === '/settings' || pathname.startsWith('/settings/')
                    ? 'bg-emerald-50 font-semibold text-emerald-800'
                    : 'font-medium text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                }`}
              >
                {(pathname === '/settings' || pathname.startsWith('/settings/')) && (
                  <span
                    aria-hidden="true"
                    className="absolute inset-y-1.5 left-0 w-[3px] rounded-r-full bg-emerald-600"
                  />
                )}
                <Settings
                  className={`h-4 w-4 flex-shrink-0 ${
                    pathname === '/settings' || pathname.startsWith('/settings/')
                      ? 'text-emerald-700'
                      : 'text-slate-400'
                  }`}
                />
                Settings
              </Link>
              {user.role === 'admin' && (
                <Link
                  href="/admin"
                  aria-current={pathname === '/admin' || pathname.startsWith('/admin/') ? 'page' : undefined}
                  onClick={() => setOpen(false)}
                  title="Administrators only"
                  className={`relative mb-2 flex items-center justify-between gap-3 rounded-xl px-3 py-2.5 text-sm transition ${
                    pathname === '/admin' || pathname.startsWith('/admin/')
                      ? 'bg-emerald-50 font-semibold text-emerald-800'
                      : 'font-medium text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                  }`}
                >
                  {(pathname === '/admin' || pathname.startsWith('/admin/')) && (
                    <span
                      aria-hidden="true"
                      className="absolute inset-y-1.5 left-0 w-[3px] rounded-r-full bg-emerald-600"
                    />
                  )}
                  <span className="flex items-center gap-3">
                    <Shield
                      className={`h-4 w-4 flex-shrink-0 ${
                        pathname === '/admin' || pathname.startsWith('/admin/')
                          ? 'text-emerald-700'
                          : 'text-slate-400'
                      }`}
                    />
                    Admin
                  </span>
                  <span className="rounded-full bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-700">
                    Admin
                  </span>
                </Link>
              )}
              <div className="flex items-center justify-between gap-2 rounded-xl px-3 py-2">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-slate-950">{user.username}</p>
                  <span
                    className={`mt-0.5 inline-block rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${
                      user.role === 'admin'
                        ? 'bg-emerald-50 text-emerald-700'
                        : 'bg-slate-100 text-slate-600'
                    }`}
                  >
                    {user.role}
                  </span>
                </div>
                <button
                  onClick={() => logout()}
                  aria-label="Log out"
                  title="Log out"
                  className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg text-slate-400 transition hover:bg-slate-50 hover:text-slate-700"
                >
                  <LogOut className="h-4 w-4" />
                </button>
              </div>
            </>
          ) : (
            <p className="px-3 py-1 text-xs text-slate-400">PaddleOCR document pipeline</p>
          )}
        </div>
      </div>
    </>
  );
}
