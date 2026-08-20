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
  Mail,
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
  { href: '/mail', label: 'API Mail Extraction', icon: Mail, description: 'API-ingested messages' },
  { href: '/imports', label: 'Confluence Import', icon: FileInput, description: 'Confluence page imports' },
];

const links = [
  { href: '/', label: 'Home', icon: Home },
  {
    href: '/processing',
    label: 'Processing',
    icon: Cpu,
    description: 'Upload and process documents',
    children: processingChildren,
  },
  { href: '/benchmark', label: 'VL Benchmark', icon: Gauge },
  { href: '/connections', label: 'Connections', icon: PlugZap },
];

// Routes that should auto-expand the Processing submenu.
const processingRoutes = ['/processing', '/jobs', '/mail', '/imports'];

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
          {links.map(({ href, label, icon: Icon, description, children }) => {
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
                  onClick={() => setOpen(false)}
                  title={description}
                  className={`flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition ${
                    active
                      ? 'bg-emerald-50 text-emerald-800'
                      : 'text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                  }`}
                >
                  <Icon className={`h-4 w-4 flex-shrink-0 ${active ? 'text-emerald-700' : 'text-slate-400'}`} />
                  {label}
                </Link>
              );
            }

            return (
              <div key={href}>
                <div
                  className={`flex items-center gap-1 rounded-xl pr-1 text-sm font-medium transition ${
                    active
                      ? 'bg-emerald-50 text-emerald-800'
                      : childActive
                        ? 'text-emerald-700'
                        : 'text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                  }`}
                >
                  <Link
                    href={href}
                    onClick={() => setOpen(false)}
                    title={description}
                    className="flex flex-1 items-center gap-3 rounded-xl px-3 py-2.5"
                  >
                    <Icon
                      className={`h-4 w-4 flex-shrink-0 ${
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
                          onClick={() => setOpen(false)}
                          title={child.description}
                          className={`flex items-center gap-3 rounded-xl py-2 pl-10 pr-3 text-sm font-medium transition ${
                            childIsActive
                              ? 'bg-emerald-50 text-emerald-800'
                              : 'text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                          }`}
                        >
                          <child.icon
                            className={`h-3.5 w-3.5 flex-shrink-0 ${childIsActive ? 'text-emerald-700' : 'text-slate-400'}`}
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
        </nav>

        <div className="border-t border-slate-100 px-3 py-3">
          {user ? (
            <>
              <Link
                href="/settings"
                onClick={() => setOpen(false)}
                className={`mb-2 flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition ${
                  pathname === '/settings' || pathname.startsWith('/settings/')
                    ? 'bg-emerald-50 text-emerald-800'
                    : 'text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                }`}
              >
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
                  onClick={() => setOpen(false)}
                  className={`mb-2 flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition ${
                    pathname === '/admin' || pathname.startsWith('/admin/')
                      ? 'bg-emerald-50 text-emerald-800'
                      : 'text-slate-600 hover:bg-slate-50 hover:text-slate-950'
                  }`}
                >
                  <Shield
                    className={`h-4 w-4 flex-shrink-0 ${
                      pathname === '/admin' || pathname.startsWith('/admin/')
                        ? 'text-emerald-700'
                        : 'text-slate-400'
                    }`}
                  />
                  Admin
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
