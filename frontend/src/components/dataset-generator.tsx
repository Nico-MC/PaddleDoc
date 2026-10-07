'use client';

import { useEffect, useRef, useState } from 'react';
import { LoaderCircle, RefreshCw, Sparkles, Square, SquareCheck, StopCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ErrorNotice, Field, inputClass } from '@/components/admin/admin-shared';
import { ApiError, apiJson } from '@/lib/api';

type MarkdownFile = {
  path: string;
  filename: string;
  original_filename: string;
  workspace_folder: string;
  source_file_sha256?: string | null;
  source_markdown_sha256?: string | null;
  updated_at?: string;
  document_version?: number;
};

type ExistingDataset = {
  path: string;
  filename: string;
  source_documents: string[];
  source_files: string[];
  source_file_sha256?: string | null;
  source_markdown_sha256?: string | null;
};

type DatasetSourceRelation = 'current' | 'different-markdown' | 'exact-path-unknown' | 'pdf-hash-only';

type GenerationItem = {
  job_id: string;
  filename: string;
  status: 'queued' | 'processing' | 'completed' | 'failed' | 'skipped' | 'cancelled';
  dataset_path?: string;
  row_count?: number;
  dataset_action?: 'created' | 'overwritten';
  dataset_filename?: string;
  started_at?: number;
  duration_seconds?: number;
  coverage_note?: string | null;
  progress?: {
    phase: string;
    region: number;
    region_count: number;
    passage_attempt: number;
    passages_checked: number;
    passages_available: number;
    questions_generated: number;
    question_target: number;
  };
  error?: string;
  warning?: string | null;
};

type GenerationRun = {
  run_id: string;
  items: GenerationItem[];
  finished: boolean;
  cancelled: boolean;
  model_name: string;
  sampling_seed?: number | null;
};

type GenerationConfig = {
  configured: boolean;
  model_name: string;
  models: string[];
};

const URL = '/api/v1/evaluation-datasets/generation';
const RUN_KEY = 'paddledoc-dataset-generation';
const STATUS_LABELS = {
  queued: 'Wartend', processing: 'Generiert', completed: 'Fertig',
  failed: 'Fehler', skipped: 'Vorhanden', cancelled: 'Abgebrochen',
};

function formatDuration(seconds: number): string {
  const safeSeconds = Math.max(0, seconds);
  if (safeSeconds < 60) return `${safeSeconds.toFixed(1)} s`;
  const totalSeconds = Math.floor(safeSeconds);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const remainingSeconds = totalSeconds % 60;
  if (hours > 0) return `${hours} h ${minutes} min ${remainingSeconds} s`;
  if (minutes > 0) return `${minutes} min ${remainingSeconds} s`;
  return `${safeSeconds.toFixed(1)} s`;
}

export function DatasetGenerator({ files, datasets, preferredPath, onSaved }: {
  files: MarkdownFile[];
  datasets: ExistingDataset[];
  preferredPath: string;
  onSaved: (path: string) => Promise<void> | void;
}) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set(preferredPath ? [preferredPath] : []));
  const [questionCount, setQuestionCount] = useState(10);
  const [samplingSeed, setSamplingSeed] = useState('');
  const [style, setStyle] = useState('user-paraphrases');
  const [focus, setFocus] = useState('');
  const [existingMode, setExistingMode] = useState<'skip' | 'new' | 'overwrite'>('skip');
  const [overwriteTargets, setOverwriteTargets] = useState<Record<string, string>>({});
  const [config, setConfig] = useState<GenerationConfig | null>(null);
  const [modelName, setModelName] = useState('');
  const [refreshingModels, setRefreshingModels] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<GenerationRun | null>(null);
  const [nowSeconds, setNowSeconds] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const completedRunRef = useRef<string | null>(null);
  const running = Boolean(runId && !run?.finished);
  const hasProcessingItems = run?.items.some((item) => item.status === 'processing') ?? false;
  const settledCount = run?.items.filter((item) => !['queued', 'processing'].includes(item.status)).length ?? 0;
  const runTotal = run?.items.length ?? 0;
  const progressPercent = runTotal > 0 ? Math.round((settledCount / runTotal) * 100) : 0;

  useEffect(() => {
    if (!hasProcessingItems) return;
    const timer = window.setInterval(() => setNowSeconds(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [hasProcessingItems]);
  const validSamplingSeed = samplingSeed === '' || (
    Number.isInteger(Number(samplingSeed)) && Number(samplingSeed) >= 0 && Number(samplingSeed) <= 2**31 - 1
  );
  const newestMarkdownByPdfHash = new Map<string, MarkdownFile>();
  for (const file of files) {
    if (!file.source_file_sha256) continue;
    const current = newestMarkdownByPdfHash.get(file.source_file_sha256);
    if (!current || Date.parse(file.updated_at ?? '') >= Date.parse(current.updated_at ?? '')) {
      newestMarkdownByPdfHash.set(file.source_file_sha256, file);
    }
  }

  function datasetSourceRelation(dataset: ExistingDataset, path: string): DatasetSourceRelation | null {
    const file = files.find((candidate) => candidate.path === path);
    if (!file || dataset.source_documents.length !== 1) return null;
    const exactPath = dataset.source_documents[0] === path;
    const fileHash = file.source_file_sha256;
    const datasetSourceFile = files.find((candidate) => candidate.path === dataset.source_documents[0]);
    const datasetHash = dataset.source_file_sha256 || datasetSourceFile?.source_file_sha256;
    const samePdf = fileHash && datasetHash
      ? fileHash === datasetHash
      : exactPath;
    if (!samePdf) return null;

    if (file.source_markdown_sha256 && dataset.source_markdown_sha256) {
      return file.source_markdown_sha256 === dataset.source_markdown_sha256 ? 'current' : 'different-markdown';
    }
    if (fileHash && datasetHash) return 'pdf-hash-only';
    return exactPath ? 'exact-path-unknown' : null;
  }

  function datasetsFor(path: string) {
    return datasets.filter((dataset) => datasetSourceRelation(dataset, path) !== null);
  }

  function datasetRelationLabel(relation: DatasetSourceRelation | null): string {
    switch (relation) {
      case 'current': return 'aktueller Markdown-Stand';
      case 'different-markdown': return 'anderer Markdown-Stand, gleiche PDF';
      case 'exact-path-unknown': return 'gleicher Markdown-Pfad, Hash fehlt';
      case 'pdf-hash-only': return 'gleicher PDF-Inhalt, Markdown-Hash fehlt';
      default: return 'Quelle nicht zugeordnet';
    }
  }

  function datasetOverwriteWarning(dataset: ExistingDataset, path: string): string | null {
    const relation = datasetSourceRelation(dataset, path);
    if (relation === 'different-markdown') {
      return 'Gleicher PDF-Inhalt, aber anderer Markdown-Hash. Beim Ersetzen werden die bisherigen Fragen durch Fragen aus dem ausgewählten Markdown ersetzt.';
    }
    if (relation === 'pdf-hash-only') {
      return 'Der PDF-Inhalt stimmt überein, aber der Markdown-Hash fehlt. Der Markdown-Stand ist nicht verifizierbar.';
    }
    if (relation === 'exact-path-unknown') {
      return 'Der Markdown-Pfad stimmt überein, aber ein Markdown-Hash zum Versionsvergleich fehlt.';
    }
    return null;
  }

  function markdownFreshness(file: MarkdownFile): string {
    if (!file.source_file_sha256) return 'PDF-Identität nicht verifiziert';
    const latest = newestMarkdownByPdfHash.get(file.source_file_sha256);
    return latest?.path === file.path ? 'neuester Markdown-Stand' : 'älterer Markdown-Stand';
  }

  function isCurrentMarkdownDataset(dataset: ExistingDataset, path: string): boolean {
    return datasetSourceRelation(dataset, path) === 'current';
  }

  function overwriteTargetFor(path: string) {
    const choices = datasetsFor(path);
    const selectedTarget = overwriteTargets[path];
    const currentTargets = choices.filter((dataset) => {
      return isCurrentMarkdownDataset(dataset, path);
    });
    return choices.some((dataset) => dataset.path === selectedTarget)
      ? selectedTarget
      : currentTargets.length === 1 ? currentTargets[0].path : '';
  }

  const selectedWithDatasets = [...selected].filter((path) => datasetsFor(path).length > 0);
  const missingOverwriteTarget = existingMode === 'overwrite' && selectedWithDatasets.some((path) => !overwriteTargetFor(path));

  useEffect(() => {
    let active = true;
    void apiJson<GenerationConfig>(`${URL}/config`)
      .then((result) => {
        if (!active) return;
        setConfig(result);
        setModelName(result.models.includes(result.model_name) ? result.model_name : result.models[0] ?? '');
        setRunId(sessionStorage.getItem(RUN_KEY));
      })
      .catch((cause: unknown) => { if (active) setError(String(cause)); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!runId) return;
    let active = true;
    const poll = async () => {
      try {
        const result = await apiJson<GenerationRun>(`${URL}/${runId}`);
        if (!active) return;
        setRun(result);
        if (result.finished) {
          window.clearInterval(timer);
          sessionStorage.removeItem(RUN_KEY);
          if (completedRunRef.current !== runId) {
            completedRunRef.current = runId;
            const saved = result.items.find((item) => item.dataset_path);
            if (saved?.dataset_path) await onSaved(saved.dataset_path);
          }
        }
      } catch (cause) {
        if (active) {
          setError(String(cause));
          if (cause instanceof ApiError && cause.status === 404) {
            sessionStorage.removeItem(RUN_KEY);
            setRunId(null);
            window.clearInterval(timer);
          }
        }
      }
    };
    const timer = window.setInterval(() => void poll(), 3000);
    void poll();
    return () => { active = false; window.clearInterval(timer); };
  }, [runId, onSaved]);

  async function refreshModels() {
    setRefreshingModels(true);
    setError(null);
    try {
      const result = await apiJson<GenerationConfig>(`${URL}/config`);
      setConfig(result);
      setModelName((current) => result.models.includes(current)
        ? current
        : result.models.includes(result.model_name) ? result.model_name : result.models[0] ?? '');
    } catch (cause) {
      setError(String(cause));
    } finally {
      setRefreshingModels(false);
    }
  }

  async function start() {
    if (missingOverwriteTarget) return;
    if (existingMode === 'overwrite' && selectedWithDatasets.length > 0) {
      const filenames = selectedWithDatasets
        .map((path) => datasetsFor(path).find((dataset) => dataset.path === overwriteTargetFor(path))?.filename)
        .filter((filename): filename is string => Boolean(filename));
      const preview = filenames.slice(0, 5).map((filename) => `- ${filename}`).join('\n');
      const remaining = filenames.length - 5;
      const more = remaining > 0 ? `\n… und ${remaining} weitere` : '';
      const historicalCount = selectedWithDatasets.filter((path) => {
        const target = datasetsFor(path).find((dataset) => dataset.path === overwriteTargetFor(path));
        return Boolean(target && !isCurrentMarkdownDataset(target, path));
      }).length;
      const historicalWarning = historicalCount > 0
        ? `\n\n${historicalCount} Zieldataset(s) haben einen abweichenden oder nicht verifizierbaren Markdown-Stand. Beim Ersetzen gehen ihre bisherigen Fragen verloren.`
        : '';
      if (!window.confirm(
        `Diese ${filenames.length} bestehenden Datasets zu den ausgewählten PDFs werden nach erfolgreicher Generierung ersetzt:\n${preview}${more}${historicalWarning}\n\nDokumente ohne ausgewähltes Zieldataset erhalten eine neue Datei. Fortfahren?`,
      )) return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await apiJson<{ run_id: string }>(URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          markdown_paths: [...selected], question_count: questionCount,
          question_style: style, focus, skip_existing: existingMode === 'skip',
          overwrite_datasets: existingMode === 'overwrite'
            ? Object.fromEntries(selectedWithDatasets.map((path) => [path, overwriteTargetFor(path)]))
            : {},
          model_name: modelName,
          sampling_seed: samplingSeed === '' ? null : Number(samplingSeed),
        }),
      });
      sessionStorage.setItem(RUN_KEY, result.run_id);
      setRun(null);
      setRunId(result.run_id);
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    if (!runId) return;
    setBusy(true);
    try {
      await apiJson(`${URL}/${runId}/cancel`, { method: 'POST' });
    } catch (cause) {
      setError(String(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-4 border-t border-slate-200 pt-4">
      <ErrorNotice message={error} />
      {config && !config.configured && <ErrorNotice message="Die Dataset-Generierung ist nicht konfiguriert. Prüfe die ausgewählte LLM-Konfiguration." />}
      <div className="grid gap-3 md:grid-cols-2">
        <Field label="Fragen pro Dokument">
          <input className={inputClass} type="number" min={1} max={30} value={questionCount} disabled={running || busy} onChange={(event) => setQuestionCount(Math.max(1, Math.min(30, Number(event.target.value) || 1)))} />
        </Field>
        <Field label="Modell">
          <div className="flex min-w-0 items-center gap-2">
            <select className={`${inputClass} min-w-0 flex-1`} value={modelName} disabled={running || busy || refreshingModels || !config?.models.length} onChange={(event) => setModelName(event.target.value)}>
              {!config?.models.length && <option value="">Keine Modelle verfügbar</option>}
              {config?.models.map((model) => <option key={model} value={model}>{model}</option>)}
            </select>
            <Button type="button" size="sm" variant="outline" disabled={running || busy || refreshingModels} onClick={() => void refreshModels()} aria-label="Modelle aktualisieren" title="Modelle aktualisieren">
              <RefreshCw className={`h-4 w-4${refreshingModels ? ' animate-spin' : ''}`} />
            </Button>
          </div>
        </Field>
      </div>
      <Field label="Verteilung über das Dokument">
        <p className="text-sm text-slate-700">Der Generator teilt das Dokument möglichst gleichmäßig in bis zu {questionCount} Abschnitte und versucht, aus jedem eine belegte Frage zu erstellen. Bei 90 markierten Seiten und 10 Fragen sind das grob 9 Seiten je Abschnitt. Ohne Seitenmarkierungen wird nach Textposition verteilt; ein Abschnitt ist also nicht zwingend eine einzelne Seite.</p>
      </Field>
      <details className="my-3 border-y border-slate-200 py-2">
        <summary className="cursor-pointer text-sm font-medium">Optionale Einstellungen</summary>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <Field label="Fragenstil">
            <select className={inputClass} value={style} disabled={running || busy} onChange={(event) => setStyle(event.target.value)}>
              <option value="user-paraphrases">Natürliche Kundenfragen</option>
              <option value="contract-language">Vertragsnahe Fragen</option>
              <option value="mixed-questions">Gemischte Fragen</option>
            </select>
          </Field>
          <Field label="Themenfokus (optional)">
            <input className={inputClass} value={focus} maxLength={1000} disabled={running || busy} onChange={(event) => setFocus(event.target.value)} placeholder="Zum Beispiel: Leistungen und Ausschlüsse" />
          </Field>
          <Field label="Zufalls-Seed (optional)">
            <input className={inputClass} type="number" min={0} max={2**31 - 1} step={1} value={samplingSeed} disabled={running || busy} onChange={(event) => setSamplingSeed(event.target.value)} placeholder="Zufällig" />
          </Field>
        </div>
      </details>
      <div className="my-3">
        <Field label="Verhalten bei vorhandenen Datasets">
          <select className={inputClass} value={existingMode} disabled={running || busy} onChange={(event) => setExistingMode(event.target.value as 'skip' | 'new' | 'overwrite')}>
            <option value="skip">Bei aktuellem Dataset nicht neu generieren</option>
            <option value="new">Zusätzliches Dataset erstellen</option>
            <option value="overwrite">Ausgewähltes Dataset ersetzen</option>
          </select>
        </Field>
        <p className="mt-1 text-sm text-slate-600">
          {existingMode === 'skip' && 'Nur wenn ein Dataset exakt zur ausgewählten Markdown-Fassung existiert, wird die Generierung ausgelassen. Ältere oder nicht verifizierbare Datasets zählen nicht als aktueller Stand.'}
          {existingMode === 'new' && 'Standard: Für jede ausgewählte Quelldatei wird ein zusätzliches Dataset angelegt. Vorhandene Datasets bleiben erhalten.'}
          {existingMode === 'overwrite' && 'Bei ausgewählten Quelldateien mit vorhandenen Datasets wählst du unten das konkrete Dataset zum Ersetzen aus. Quelldateien ohne Bestand erhalten ein neues Dataset.'}
        </p>
      </div>
      <p className="mb-1 text-sm font-medium text-slate-700">Markdown-Dateien für die Generierung</p>
      <div className="my-2 flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" disabled={running || busy} onClick={() => setSelected((previous) => new Set([...previous, ...files.map((file) => file.path)]))}><SquareCheck className="mr-2 h-4 w-4" />Alle Markdown-Dateien</Button>
        <Button size="sm" variant="outline" disabled={running || busy} onClick={() => setSelected(new Set())}><Square className="mr-2 h-4 w-4" />Auswahl leeren</Button>
        <span className="text-sm text-slate-500">{selected.size} ausgewählt</span>
      </div>
      <div className="max-h-64 overflow-y-auto border-y border-slate-200">
        {files.map((file) => {
          const sourceDatasets = datasetsFor(file.path);
          const isSelected = selected.has(file.path);
          const overwriteTarget = sourceDatasets.find((dataset) => dataset.path === overwriteTargetFor(file.path));
          const overwriteWarning = overwriteTarget ? datasetOverwriteWarning(overwriteTarget, file.path) : null;
          return (
            <div key={file.path} className="border-b border-slate-100 py-2 text-sm">
              <label className="flex items-start gap-2">
                <input className="mt-1" type="checkbox" disabled={running || busy} checked={isSelected} onChange={() => setSelected((previous) => {
                  const next = new Set(previous);
                  if (next.has(file.path)) next.delete(file.path); else next.add(file.path);
                  return next;
                })} />
                <span className="min-w-0 break-words">
                  <span className="block font-medium">Markdown zu {file.original_filename || file.filename}</span>
                  <span className="block text-xs text-slate-500">
                    {file.filename} · PDF-Version {file.document_version ?? 'unbekannt'} · {file.workspace_folder || 'inbox'}
                  </span>
                  <span className="block text-xs text-emerald-700">{markdownFreshness(file)}</span>
                </span>
              </label>
              {isSelected && (
                <div className="ml-6 mt-2 rounded-md bg-slate-50 px-3 py-2 text-xs">
                  <p className="font-medium text-slate-700">Vorhandene Datasets zu dieser PDF</p>
                  {sourceDatasets.length > 0 ? (
                    <ul className="mt-1 space-y-0.5 text-slate-600">
                      {sourceDatasets.map((dataset) => (
                        <li key={dataset.path} className="break-words">
                          {dataset.filename}
                          <span className="ml-1 text-slate-500">({datasetRelationLabel(datasetSourceRelation(dataset, file.path))})</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-1 text-slate-500">Keine vorhanden</p>
                  )}
                </div>
              )}
              {existingMode === 'overwrite' && isSelected && sourceDatasets.length > 0 && (
                <label className="ml-6 mt-2 block min-w-0 overflow-hidden text-xs font-medium text-slate-600">
                  Zu ersetzendes Dataset
                  <select className={`${inputClass} min-w-0 max-w-full truncate`} aria-label={`Zu ersetzendes Dataset für ${file.original_filename || file.filename}`} value={overwriteTargetFor(file.path)} disabled={running || busy} onChange={(event) => setOverwriteTargets((previous) => ({ ...previous, [file.path]: event.target.value }))}>
                    <option value="">Bitte Dataset auswählen</option>
                    {sourceDatasets.map((dataset) => <option key={dataset.path} value={dataset.path}>
                      {dataset.filename} — {datasetRelationLabel(datasetSourceRelation(dataset, file.path))}
                    </option>)}
                  </select>
                  {overwriteWarning && (
                    <span className="mt-1 block font-normal text-amber-700">
                      {overwriteWarning}
                    </span>
                  )}
                </label>
              )}
            </div>
          );
        })}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Button onClick={() => void start()} disabled={busy || running || refreshingModels || !validSamplingSeed || missingOverwriteTarget || !config?.configured || !modelName || selected.size === 0 || selected.size > 1000}>
          {busy || running ? <LoaderCircle className="mr-2 h-4 w-4 animate-spin" /> : <Sparkles className="mr-2 h-4 w-4" />}Generierung starten
        </Button>
        {running && <Button variant="outline" disabled={busy || run?.cancelled} onClick={() => void cancel()}><StopCircle className="mr-2 h-4 w-4" />Abbrechen</Button>}
      </div>
      {selected.size > 1000 && <ErrorNotice message="Maximal 1000 Dokumente pro Auftrag." />}
      {run && (
        <div className="mt-4" aria-live="polite">
          <p className="mb-1 break-words text-sm text-slate-500">Modell: {run.model_name}</p>
          {run.sampling_seed != null && <p className="mb-1 text-sm text-slate-500">Zufalls-Seed: {run.sampling_seed}</p>}
          <div className="mb-1 flex items-center justify-between gap-3 text-sm font-medium">
            <p>{settledCount} / {runTotal} Quelldateien abgeschlossen{run.finished ? '' : run.cancelled ? ' · Abbruch angefordert' : ''}</p>
            <span>{progressPercent}%</span>
          </div>
          <progress className="h-2 w-full accent-emerald-600" max={100} value={progressPercent} aria-label={`Fortschritt: ${progressPercent}%`} />
          <p className="mt-1 text-xs text-slate-500">Fortschritt nach Quelldateien; die Dauer wird pro Datei angezeigt.</p>
          <div className="mt-2 max-h-64 overflow-y-auto">
            {run.items.map((item) => {
              const duration = item.duration_seconds
                ?? (item.status === 'processing' && item.started_at != null && nowSeconds > 0
                  ? Math.max(0, nowSeconds - item.started_at)
                  : null);
              const isProgressComplete = item.status === 'completed' || item.status === 'skipped';
              const questionProgress = item.progress && item.progress.question_target > 0
                ? Math.min(95, Math.round(item.progress.questions_generated / item.progress.question_target * 95))
                : 0;
              const itemProgress = isProgressComplete ? 100 : questionProgress;
              return (
                <div key={item.job_id} className="border-b border-slate-100 py-2 text-sm">
                  <p className="break-words">{item.filename}: {STATUS_LABELS[item.status]}{item.row_count ? ` (${item.row_count} Fragen)` : ''}</p>
                  {duration != null && <p className="text-xs text-slate-500">{item.status === 'processing' ? 'Läuft seit' : 'Dauer'}: {formatDuration(duration)}</p>}
                  {(item.status === 'processing' || isProgressComplete || item.progress) && (
                    <div
                      className="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-emerald-100"
                      role="progressbar"
                      aria-label={`Generierungsfortschritt für ${item.filename}`}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={itemProgress}
                    >
                      <div
                        className="h-full rounded-full bg-emerald-600 transition-[width] duration-500 ease-out"
                        style={{ width: `${itemProgress}%` }}
                      />
                    </div>
                  )}
                  {item.error && <p className="break-words text-red-700">{item.error}</p>}
                  {item.warning && <p className="break-words text-amber-700">{item.warning}</p>}
                  {item.coverage_note && <p className="mt-1 rounded-md bg-slate-50 px-2 py-1 text-xs text-slate-600">{item.coverage_note}</p>}
                  {item.dataset_action && item.dataset_filename && <p className="text-xs text-slate-600">{item.dataset_action === 'overwritten' ? 'Bestehendes Dataset ersetzt:' : 'Neues Dataset erstellt:'} {item.dataset_filename}</p>}
                  {item.dataset_path && <button className="text-emerald-700 underline" onClick={() => void onSaved(item.dataset_path!)}>Dataset öffnen</button>}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}