'use client';

import { useEffect, useMemo, useState } from 'react';

import { Button } from '@/components/ui/button';
import { API_BASE_URL } from '@/lib/api-base';

type MarkdownFileEntry = {
  path: string;
  filename: string;
  folder: string;
  size_bytes: number;
  updated_at: string;
};

type MarkdownBrowserResponse = {
  items: MarkdownFileEntry[];
};

type ParsedFileEntry = MarkdownFileEntry & {
  logicalFolder: string;
  logicalSubfolder: string;
};

type IngestedPayload = {
  count: number;
  documents: Array<{
    id: string;
    content: string;
    score: number;
    distance: null;
    meta_data: {
      tags: Record<string, string | number | boolean>;
    };
  }>;
};

function normalizeFrontmatterValue(value: unknown): string | number | boolean {
  if (typeof value === 'boolean' || typeof value === 'number' || typeof value === 'string') {
    return value;
  }
  return JSON.stringify(value);
}

function extractFrontmatter(rawText: string): {
  frontmatter: Record<string, unknown>;
  content: string;
} {
  if (!rawText.startsWith('---\n')) {
    return { frontmatter: {}, content: rawText };
  }

  const marker = '\n---\n';
  const end = rawText.indexOf(marker, 4);
  if (end === -1) {
    return { frontmatter: {}, content: rawText };
  }

  const frontmatterText = rawText.slice(4, end);
  const content = rawText.slice(end + marker.length).replace(/^\n+/, '');
  const frontmatter: Record<string, unknown> = {};

  for (const line of frontmatterText.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) {
      continue;
    }
    const separatorIndex = trimmed.indexOf(':');
    if (separatorIndex <= 0) {
      continue;
    }
    const key = trimmed.slice(0, separatorIndex).trim();
    const rawValue = trimmed.slice(separatorIndex + 1).trim();
    if (!key) {
      continue;
    }

    const unquoted = rawValue.replace(/^['\"]|['\"]$/g, '');
    if (unquoted === 'true') {
      frontmatter[key] = true;
    } else if (unquoted === 'false') {
      frontmatter[key] = false;
    } else if (unquoted !== '' && !Number.isNaN(Number(unquoted))) {
      frontmatter[key] = Number(unquoted);
    } else {
      frontmatter[key] = unquoted;
    }
  }

  return { frontmatter, content };
}

function parseLogicalFolder(entry: MarkdownFileEntry): ParsedFileEntry {
  const parts = entry.folder.split('/').filter(Boolean);
  const withoutJobId = parts.length > 0 ? parts.slice(0, -1) : [];
  const logicalFolder = withoutJobId[0] ?? 'inbox';
  const logicalSubfolder = withoutJobId.slice(1).join('/');

  return {
    ...entry,
    logicalFolder,
    logicalSubfolder,
  };
}

function encodeRelativePath(path: string): string {
  return path
    .split('/')
    .filter((part) => part.length > 0)
    .map((part) => encodeURIComponent(part))
    .join('/');
}

export function EncourageIngestion() {
  const [loading, setLoading] = useState(true);
  const [ingesting, setIngesting] = useState(false);
  const [loadingSelectedFile, setLoadingSelectedFile] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [entries, setEntries] = useState<ParsedFileEntry[]>([]);
  const [selectedFolder, setSelectedFolder] = useState('');
  const [selectedSubfolder, setSelectedSubfolder] = useState('');
  const [selectedPath, setSelectedPath] = useState('');
  const [payload, setPayload] = useState<IngestedPayload | null>(null);
  const [jsonOpen, setJsonOpen] = useState(true);

  useEffect(() => {
    let cancelled = false;

    async function loadMarkdownFiles() {
      setLoading(true);
      setError(null);
      try {
        const response = await fetch(`${API_BASE_URL}/api/v1/markdown-files`, { cache: 'no-store' });
        if (!response.ok) {
          throw new Error('Failed to load markdown files.');
        }

        const data = (await response.json()) as MarkdownBrowserResponse;
        const parsed = data.items.map(parseLogicalFolder);

        if (cancelled) {
          return;
        }

        setEntries(parsed);

        if (parsed.length > 0) {
          const initialFolder = parsed[0].logicalFolder;
          const initialSubfolder = parsed[0].logicalSubfolder;
          const initialFile = parsed.find(
            (item) => item.logicalFolder === initialFolder && item.logicalSubfolder === initialSubfolder,
          );

          setSelectedFolder(initialFolder);
          setSelectedSubfolder(initialSubfolder);
          setSelectedPath(initialFile?.path ?? '');
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(loadError instanceof Error ? loadError.message : 'Unexpected error while loading files.');
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    void loadMarkdownFiles();
    return () => {
      cancelled = true;
    };
  }, []);

  const folderOptions = useMemo(() => {
    const unique = new Set(entries.map((entry) => entry.logicalFolder));
    return Array.from(unique).sort((left, right) => left.localeCompare(right));
  }, [entries]);

  const subfolderOptions = useMemo(() => {
    const unique = new Set(
      entries
        .filter((entry) => entry.logicalFolder === selectedFolder)
        .map((entry) => entry.logicalSubfolder),
    );
    return Array.from(unique).sort((left, right) => left.localeCompare(right));
  }, [entries, selectedFolder]);

  const filteredFiles = useMemo(() => {
    return entries.filter(
      (entry) =>
        entry.logicalFolder === selectedFolder &&
        entry.logicalSubfolder === selectedSubfolder,
    );
  }, [entries, selectedFolder, selectedSubfolder]);

  useEffect(() => {
    const firstMatch = filteredFiles[0];
    if (!firstMatch) {
      setSelectedPath('');
      return;
    }

    const exists = filteredFiles.some((entry) => entry.path === selectedPath);
    if (!exists) {
      setSelectedPath(firstMatch.path);
    }
  }, [filteredFiles, selectedPath]);

  async function ingestMarkdownFile(path: string, manual = false) {
    if (!path) {
      setError('Please select a markdown file first.');
      return;
    }

    if (manual) {
      setIngesting(true);
      setError(null);
    } else {
      setLoadingSelectedFile(true);
    }

    try {
      const encodedPath = encodeRelativePath(path);
      const response = await fetch(`${API_BASE_URL}/api/v1/markdown-files/${encodedPath}`);
      if (!response.ok) {
        throw new Error('Failed to load selected markdown file content.');
      }

      const rawMarkdown = await response.text();
      const { frontmatter, content } = extractFrontmatter(rawMarkdown);
      const selectedEntry = entries.find((entry) => entry.path === path);
      const metadataTags: Record<string, string | number | boolean> = {
        source: 'markdown',
        loader: 'MarkdownIngestion',
        filename: selectedEntry?.filename ?? 'unknown.md',
        filepath: path,
      };

      for (const [key, value] of Object.entries(frontmatter)) {
        metadataTags[`fm_${key}`] = normalizeFrontmatterValue(value);
      }

      const nextPayload: IngestedPayload = {
        count: 1,
        documents: [
          {
            id: path,
            content,
            score: 0.0,
            distance: null,
            meta_data: {
              tags: metadataTags,
            },
          },
        ],
      };

      setPayload(nextPayload);
      setJsonOpen(true);
      setError(null);
    } catch (ingestError) {
      setPayload(null);
      setError(ingestError instanceof Error ? ingestError.message : 'Unexpected ingest error.');
    } finally {
      if (manual) {
        setIngesting(false);
      } else {
        setLoadingSelectedFile(false);
      }
    }
  }

  useEffect(() => {
    if (!selectedPath) {
      setPayload(null);
      setError(null);
      return;
    }

    void ingestMarkdownFile(selectedPath, false);
  }, [selectedPath, entries]);

  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold text-slate-950">encouRAGe Ingestion</h1>
        <p className="mt-2 text-sm text-slate-600">
          Select folder, subfolder, and markdown output from PaddleDoc. Then ingest to generate the JSON payload for encourage.
        </p>
      </div>

      {loading ? (
        <p className="text-sm text-slate-500">Loading markdown files...</p>
      ) : (
        <div className="grid gap-4 md:grid-cols-3">
          <label className="flex flex-col gap-2 text-sm text-slate-700">
            Folder
            <select
              value={selectedFolder}
              onChange={(event) => {
                const nextFolder = event.target.value;
                setSelectedFolder(nextFolder);
                const nextSubfolder = entries.find((entry) => entry.logicalFolder === nextFolder)?.logicalSubfolder ?? '';
                setSelectedSubfolder(nextSubfolder);
              }}
              className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm"
            >
              {folderOptions.map((folder) => (
                <option key={folder} value={folder}>
                  {folder}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-2 text-sm text-slate-700">
            Subfolder
            <select
              value={selectedSubfolder}
              onChange={(event) => setSelectedSubfolder(event.target.value)}
              className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm"
            >
              {subfolderOptions.map((subfolder) => (
                <option key={subfolder || '__root__'} value={subfolder}>
                  {subfolder || '(none)'}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-2 text-sm text-slate-700">
            Markdown file
            <select
              value={selectedPath}
              onChange={(event) => setSelectedPath(event.target.value)}
              className="rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm"
            >
              {filteredFiles.map((entry) => (
                <option key={entry.path} value={entry.path}>
                  {entry.filename}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}

      <div className="mt-5 flex items-center gap-3">
        <Button onClick={() => void ingestMarkdownFile(selectedPath, true)} disabled={loading || ingesting || !selectedPath}>
          {ingesting ? 'Ingesting...' : 'Ingest MD file'}
        </Button>
        {selectedPath && (
          <p className="text-xs text-slate-500">
            Selected: {selectedPath}
            {loadingSelectedFile ? ' (loading...)' : ''}
          </p>
        )}
      </div>

      {error && (
        <p className="mt-4 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">{error}</p>
      )}

      {payload && (
        <div className="mt-6 rounded-xl border border-slate-200 bg-slate-50">
          <button
            type="button"
            onClick={() => setJsonOpen((open) => !open)}
            className="flex w-full items-center justify-between px-4 py-3 text-left text-sm font-medium text-slate-800"
          >
            <span>Generated JSON payload</span>
            <span className="text-xs text-slate-500">{jsonOpen ? 'Collapse' : 'Expand'}</span>
          </button>

          {jsonOpen && (
            <pre className="max-h-[480px] overflow-auto border-t border-slate-200 bg-slate-950/95 p-4 text-xs text-slate-100">
              {JSON.stringify(payload, null, 2)}
            </pre>
          )}
        </div>
      )}
    </section>
  );
}
