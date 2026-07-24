'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { Home, Menu, X, Cpu, FolderOpen, Sparkles, FlaskConical } from 'lucide-react';

type NavLink = {
  href: string;
  label: string;
  icon: any;
  external?: boolean;
};

const links: NavLink[] = [
  { href: '/', label: 'Home', icon: Home },
  { href: '/processing', label: 'Processing', icon: Cpu },
  { href: '/jobs', label: 'Jobs', icon: FolderOpen },
  { href: '/encourage', label: 'Encourage', icon: Sparkles },
];

function resolveMlflowUrl(): string {
  if (typeof window === 'undefined') {
    return 'http://localhost:5000';
  }
  const protocol = window.location.protocol === 'https:' ? 'https:' : 'http:';
  return `${protocol}//${window.location.hostname}:5000`;
}

export function SidebarNav() {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  const drawerRef = useRef<HTMLDivElement>(null);
  const externalLinks: NavLink[] = [
    { href: resolveMlflowUrl(), label: 'MLflow', icon: FlaskConical, external: true },
  ];

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
        className="fixed left-4 top-4 z-50 flex h-9 w-9 items-center justify-center rounded-xl border border-slate-200 bg-white shadow-md transition hover:bg-slate-50"
      >
        {open ? <X className="h-4 w-4 text-slate-700" /> : <Menu className="h-4 w-4 text-slate-700" />}
      </button>

      {/* Backdrop */}
      {open && (
        <div
          className="fixed inset-0 z-40 bg-slate-950/30 backdrop-blur-sm"
          aria-hidden="true"
        />
      )}

      {/* Drawer */}
      <div
        ref={drawerRef}
        className={`fixed left-0 top-0 z-40 flex h-full w-64 flex-col bg-white shadow-2xl transition-transform duration-200 ${
          open ? 'translate-x-0' : '-translate-x-full'
        }`}
      >
        <div className="flex items-center gap-3 border-b border-slate-100 px-5 py-4">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-emerald-600">
            <Cpu className="h-4 w-4 text-white" />
          </div>
          <span className="text-base font-semibold text-slate-950">PaddleDoc</span>
        </div>

        <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-3 py-4">
          {[...links, ...externalLinks].map(({ href, label, icon: Icon, external }) => {
            const active = pathname === href || (href !== '/' && pathname.startsWith(href));
            const baseClass = `flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition ${
              active
                ? 'bg-emerald-50 text-emerald-800'
                : 'text-slate-600 hover:bg-slate-50 hover:text-slate-950'
            }`;

            if (external) {
              return (
                <a
                  key={label}
                  href={href}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={() => setOpen(false)}
                  className={baseClass}
                >
                  <Icon className="h-4 w-4 flex-shrink-0 text-slate-400" />
                  {label}
                </a>
              );
            }

            return (
              <Link
                key={href}
                href={href}
                onClick={() => setOpen(false)}
                className={baseClass}
              >
                <Icon className={`h-4 w-4 flex-shrink-0 ${active ? 'text-emerald-700' : 'text-slate-400'}`} />
                {label}
              </Link>
            );
          })}
        </nav>

        <div className="border-t border-slate-100 px-5 py-4">
          <p className="text-xs text-slate-400">PaddleOCR document pipeline</p>
        </div>
      </div>
    </>
  );
}
