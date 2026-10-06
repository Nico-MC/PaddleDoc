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
};

type ExistingDataset = {
  path: string;
  filename: string;
  source_documents: string[];
};

type GenerationItem = {
  job_id: string;
  filename: string;
  status: 'queued' | 'processing' | 'completed' | 'failed' | 'skipped' | 'cancelled';
  dataset_path?: string;
  row_count?: number;
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

export function DatasetGenerator({ files, datasets, preferredPath, onSaved }: {
  files: MarkdownFile[];
  datasets: ExistingDataset[];
  preferredPath: string;
  onSaved: (path: string) => Promise<void> | void;
}) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set(preferredPath ? [preferredPath] : []));
  const [search, setSearch] = useState('');
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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const completedRunRef = useRef<string | null>(null);
  const running = Boolean(runId && !run?.finished);
  const validSamplingSeed = samplingSeed === '' || (
    Number.isInteger(Number(samplingSeed)) && Number(samplingSeed) >= 0 && Number(samplingSeed) <= 2**31 - 1
  );
  const filteredFiles = files.filter((file) => `${file.original_filename} ${file.filename} ${file.workspace_folder}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()));

  function datasetsFor(path: string) {
    return datasets.filter((dataset) => dataset.source_documents.length === 1 && dataset.source_documents[0] === path);
  }

  function overwriteTargetFor(path: string) {
    const choices = datasetsFor(path);
    const selectedTarget = overwriteTargets[path];
    return choices.some((dataset) => dataset.path === selectedTarget)
      ? selectedTarget
      : choices.length === 1 ? choices[0].path : '';
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
    if (existingMode === 'overwrite' && selectedWithDatasets.length > 0 && !window.confirm(
      `${selectedWithDatasets.length} bestehende Dataset(s) nach erfolgreicher Generierung ersetzen? Die bisherigen Fragen werden ueberschrieben.`,
    )) return;
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
      {config && !config.configured && <ErrorNotice message="Das lokale Dataset-LLM ist nicht konfiguriert. Starte die Compose-Erweiterung local-llm." />}
      <div className="grid gap-3 md:grid-cols-2">
        <Field label="Fragen pro Dokument">
          <input className={inputClass} type="number" min={1} max={30} value={questionCount} disabled={running || busy} onChange={(event) => setQuestionCount(Math.max(1, Math.min(30, Number(event.target.value) || 1)))} />
        </Field>
        <Field label="Verteilung">
          <input className={inputClass} value="Ueber das gesamte Dokument verteilt" readOnly />
        </Field>
        <Field label="Zufalls-Seed (optional)">
          <input className={inputClass} type="number" min={0} max={2**31 - 1} step={1} value={samplingSeed} disabled={running || busy} onChange={(event) => setSamplingSeed(event.target.value)} placeholder="Zufaellig" />
        </Field>
        <Field label="Fragenstil">
          <select className={inputClass} value={style} disabled={running || busy} onChange={(event) => setStyle(event.target.value)}>
            <option value="user-paraphrases">Natuerliche Kundenfragen</option>
            <option value="contract-language">Vertragsnahe Fragen</option>
            <option value="mixed-questions">Gemischte Fragen</option>
          </select>
        </Field>
        <Field label="Themenfokus">
          <input className={inputClass} value={focus} maxLength={1000} disabled={running || busy} onChange={(event) => setFocus(event.target.value)} placeholder="Leistungen, Fristen, Ausschluesse ..." />
        </Field>
        <Field label="Modell">
          <div className="flex min-w-0 items-center gap-2">
            <select className={`${inputClass} min-w-0 flex-1`} value={modelName} disabled={running || busy || refreshingModels || !config?.models.length} onChange={(event) => setModelName(event.target.value)}>
              {!config?.models.length && <option value="">Keine installierten Modelle</option>}
              {config?.models.map((model) => <option key={model} value={model}>{model}</option>)}
            </select>
            <Button type="button" size="sm" variant="outline" disabled={running || busy || refreshingModels} onClick={() => void refreshModels()} aria-label="Modelle aktualisieren" title="Modelle aktualisieren">
              <RefreshCw className={`h-4 w-4${refreshingModels ? ' animate-spin' : ''}`} />
            </Button>
          </div>
        </Field>
      </div>
      <div className="my-3">
        <Field label="Vorhandene Datasets">
          <select className={inputClass} value={existingMode} disabled={running || busy} onChange={(event) => setExistingMode(event.target.value as 'skip' | 'new' | 'overwrite')}>
            <option value="skip">Dokumente ueberspringen</option>
            <option value="new">Zusaetzliches Dataset erstellen</option>
            <option value="overwrite">Vorhandenes Dataset ueberschreiben</option>
          </select>
        </Field>
      </div>
      <input className={inputClass} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Dokumente filtern" aria-label="Dokumente filtern" />
      <div className="my-2 flex flex-wrap items-center gap-2">
        <Button size="sm" variant="outline" disabled={running || busy} onClick={() => setSelected((previous) => new Set([...previous, ...filteredFiles.map((file) => file.path)]))}><SquareCheck className="mr-2 h-4 w-4" />Alle Treffer</Button>
        <Button size="sm" variant="outline" disabled={running || busy} onClick={() => setSelected(new Set())}><Square className="mr-2 h-4 w-4" />Auswahl leeren</Button>
        <span className="text-sm text-slate-500">{selected.size} ausgewaehlt</span>
      </div>
      <div className="max-h-64 overflow-y-auto border-y border-slate-200">
        {filteredFiles.map((file) => (
          <div key={file.path} className="border-b border-slate-100 py-2 text-sm">
            <label className="flex items-start gap-2">
            <input className="mt-1" type="checkbox" disabled={running || busy} checked={selected.has(file.path)} onChange={() => setSelected((previous) => {
              const next = new Set(previous);
              if (next.has(file.path)) next.delete(file.path); else next.add(file.path);
              return next;
            })} />
            <span className="min-w-0 break-words">{file.original_filename || file.filename}<span className="block text-xs text-slate-500">{file.workspace_folder || 'inbox'}</span></span>
            </label>
            {existingMode === 'overwrite' && selected.has(file.path) && datasetsFor(file.path).length > 0 && (
              <select className={`${inputClass} mt-2`} aria-label={`Dataset zum Ueberschreiben fuer ${file.original_filename || file.filename}`} value={overwriteTargetFor(file.path)} disabled={running || busy} onChange={(event) => setOverwriteTargets((previous) => ({ ...previous, [file.path]: event.target.value }))}>
                <option value="">Dataset zum Ueberschreiben auswaehlen</option>
                {datasetsFor(file.path).map((dataset) => <option key={dataset.path} value={dataset.path}>{dataset.filename}</option>)}
              </select>
            )}
          </div>
        ))}
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
          <p className="mb-2 text-sm font-medium">{run.items.filter((item) => !['queued', 'processing'].includes(item.status)).length} / {run.items.length} Dokumente{run.finished ? ' abgeschlossen' : run.cancelled ? ' - Abbruch angefordert' : ''}</p>
          <progress className="h-2 w-full accent-emerald-600" max={Math.max(1, run.items.length)} value={run.items.filter((item) => !['queued', 'processing'].includes(item.status)).length} />
          <div className="mt-2 max-h-64 overflow-y-auto">
            {run.items.map((item) => (
              <div key={item.job_id} className="border-b border-slate-100 py-2 text-sm">
                <p className="break-words">{item.filename}: {STATUS_LABELS[item.status]}{item.row_count ? ` (${item.row_count} Fragen)` : ''}</p>
                {item.error && <p className="break-words text-red-700">{item.error}</p>}
                {item.warning && <p className="break-words text-amber-700">{item.warning}</p>}
                {item.dataset_path && <button className="text-emerald-700 underline" onClick={() => void onSaved(item.dataset_path!)}>Dataset oeffnen</button>}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}