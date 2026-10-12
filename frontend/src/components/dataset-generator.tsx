'use client';

import { useEffect, useRef, useState } from 'react';
import { Download, LoaderCircle, RefreshCw, Sparkles, Square, SquareCheck, StopCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { ErrorNotice, Field, inputClass } from '@/components/admin/admin-shared';
import { ApiError, apiFetch, apiJson } from '@/lib/api';

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
  updated_at?: string | null;
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
  source_audit_available?: boolean;
  started_at?: number;
  finished_at?: number;
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
    round?: number;
    max_rounds?: number;
    candidates?: number;
    escalated?: number;
    rejected?: number;
    validation_done?: number;
    validation_total?: number;
    validation_accepted?: number;
    validation_review?: number;
    validation_rejected?: number;
    mode?: 'batch' | 'direct';
    stage_done?: number | null;
    stage_total?: number | null;
  };
  validation_summary?: {
    candidates: number; escalated?: number; accepted?: number; review?: number;
    rejected: number; thresholds?: { reject_faithfulness: number; reject_answer_relevancy: number }; details?: Array<{
      id: string; question: string; status: string;
      faithfulness: number | null; answer_relevancy: number | null;
      reason?: string | null; errors?: string[];
      criteria?: Record<string, 'pass' | 'review' | 'fail'>;
      issues?: Array<{ metric: string; status: 'review' | 'fail'; reason: string }>;
    }>;
  } | null;
  error?: string;
  warning?: string | null;
};

type GenerationLog = {
  format: string;
  note?: string;
  limitations?: string[];
  settings: Record<string, unknown>;
  result: Record<string, unknown>;
  audit: Record<string, unknown>;
};

type RagasQueryMode = 'facts' | 'themes' | 'mixed';
const DEFAULT_RAGAS_PERSONA_NAME = 'Sachbearbeiter:in der HanseMerkur';
const DEFAULT_RAGAS_PERSONA_ROLE_DESCRIPTION = 'Arbeitet in der Sachbearbeitung der HanseMerkur und stellt auf Deutsch präzise, praxisnahe Fragen zum vorliegenden Dokument. Fragt nur nach Informationen, die im bereitgestellten Abschnitt belegbar sind.';

type GenerationRun = {
  run_id: string;
  items: GenerationItem[];
  finished: boolean;
  cancelled: boolean;
  model_name: string;
  validation_provider?: string | null;
  validation_model_name?: string | null;
  ragas_validate?: boolean;
  question_count: number;
  pipeline?: string;
  ragas_options?: { query_mode: RagasQueryMode; context_chars: number; passage_tries: number; persona_name?: string; persona_role_description?: string } | null;
  sampling_seed?: number | null;
};

type GenerationProvider = {
  id: string;
  label: string;
  model_name: string;
  models: string[];
  models_error?: string | null;
};

type GenerationConfig = {
  configured: boolean;
  providers: GenerationProvider[];
};

type QualityCriteriaSettings = {
  criteria: Record<string, string>;
  defaults: Record<string, string>;
  customized: boolean;
};

const MODEL_CHOICE_SEPARATOR = ':::';
const DEFAULT_VALIDATION_MODEL = 'vllm/openai/gpt-oss-120b';
const QUALITY_CRITERION_LABELS: Record<string, string> = {
  clarity: 'Klarheit', standalone: 'Eigenständigkeit', answerability: 'Beantwortbarkeit',
  correctness: 'Korrektheit', completeness: 'Vollständigkeit', informativeness: 'Informationsgehalt',
};
const PHASE_LABELS: Record<string, string> = {
  classifying: 'Quellenabschnitte prüfen',
  generating: 'Generierung',
  validating: 'Validierung',
  escalating: 'Eskalation',
  finalizing: 'Abschluss',
};

function defaultModelChoice(config: GenerationConfig): { providerId: string; modelName: string } {
  const provider = config.providers.find((candidate) => candidate.id === 'hcds' && candidate.models.length > 0)
    ?? config.providers.find((candidate) => candidate.models.length > 0);
  if (!provider) return { providerId: '', modelName: '' };
  const modelName = provider.models.includes(provider.model_name) ? provider.model_name : provider.models[0];
  return { providerId: provider.id, modelName };
}

function defaultValidationModelChoice(config: GenerationConfig): { providerId: string; modelName: string } {
  const provider = config.providers.find((candidate) => candidate.id === 'hcds' && candidate.models.includes(DEFAULT_VALIDATION_MODEL));
  return provider
    ? { providerId: provider.id, modelName: DEFAULT_VALIDATION_MODEL }
    : defaultModelChoice(config);
}

const URL = '/api/v1/evaluation-datasets/generation';
const RUN_KEY = 'paddledoc-dataset-generation';
const LAST_RUN_KEY = 'paddledoc-dataset-generation-last';
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

function itemProgressPercent(item: GenerationItem, pipeline?: string, ragasValidate = true): number {
  if (['completed', 'skipped', 'failed', 'cancelled'].includes(item.status)) return 100;
  const progress = item.progress;
  if (!progress) return 0;
  if (pipeline === 'ragas') {
    if (progress.phase === 'classifying') return 5;
    if (progress.phase === 'validating') {
      return 70 + Math.round(25 * (progress.validation_done ?? 0) / Math.max(1, progress.validation_total ?? 0));
    }
    if (progress.phase === 'finalizing') return 95;
    return Math.max(5, Math.round((ragasValidate ? 70 : 95) * progress.region / Math.max(1, progress.region_count)));
  }
  return progress.question_target > 0
    ? Math.min(95, Math.round(progress.questions_generated / progress.question_target * 95))
    : 0;
}

function ragasOutcomeMessage(item: GenerationItem, target: number): string | null {
  if (item.status !== 'completed' || item.row_count == null || item.row_count >= target) return null;
  const concisePrefix = item.row_count + ' von ' + target + ' Fragen gespeichert.';
  if (item.warning?.startsWith(concisePrefix)) return item.warning;
  const generated = item.validation_summary?.candidates ?? item.row_count;
  const notGenerated = Math.max(0, target - generated);
  const rejected = item.validation_summary?.rejected ?? 0;
  const reasons = [concisePrefix];
  if (notGenerated === 1) reasons.push('Eine Frage konnte nicht erzeugt werden.');
  else if (notGenerated > 1) reasons.push(notGenerated + ' Fragen konnten nicht erzeugt werden.');
  if (rejected === 1) reasons.push('Eine Frage wurde bei der Validierung verworfen.');
  else if (rejected > 1) reasons.push(rejected + ' Fragen wurden bei der Validierung verworfen.');
  return reasons.join(' ');
}

function rejectionReason(detail: NonNullable<NonNullable<GenerationItem['validation_summary']>['details']>[number], summary: NonNullable<GenerationItem['validation_summary']>): string {
  if (detail.reason) return detail.reason;
  const thresholds = summary.thresholds;
  const faithfulnessLimit = thresholds?.reject_faithfulness ?? 0.50;
  const relevanceLimit = thresholds?.reject_answer_relevancy ?? 0.40;
  const reasons = [];
  if (detail.faithfulness != null && detail.faithfulness < faithfulnessLimit) {
    reasons.push('Faithfulness ' + detail.faithfulness.toFixed(2) + ' unter Ablehnungsgrenze ' + faithfulnessLimit.toFixed(2));
  }
  if (detail.answer_relevancy != null && detail.answer_relevancy < relevanceLimit) {
    reasons.push('Antwort-Relevanz ' + detail.answer_relevancy.toFixed(2) + ' unter Ablehnungsgrenze ' + relevanceLimit.toFixed(2));
  }
  return reasons.join('; ') || detail.errors?.join('; ') || 'Im alten Lauf wurde kein genauer Grund gespeichert.';
}

function reviewErrorReason(errors: string[]): string {
  return errors.map((entry) => {
    const [metric, errorType, ...message] = entry.split(': ');
    const label = metric === 'answer_relevancy' ? 'Antwort-Relevanz' : metric === 'faithfulness' ? 'Faithfulness' : metric;
    const explanation = message.join(': ');
    return label + ' konnte nicht berechnet werden (' + (errorType ?? 'unbekannter Fehler')
      + (explanation ? ': ' + explanation : '') + ').'
      + (explanation ? '' : ' Im damaligen Lauf wurde nur der Fehlertyp protokolliert.');
  }).join(' ');
}

function LogSection({ title, value, initialOpen = false }: { title: string; value: unknown; initialOpen?: boolean }) {
  return (
    <details open={initialOpen || undefined} className="rounded-md border border-slate-200 bg-white">
      <summary className="cursor-pointer px-3 py-2 font-medium">{title}</summary>
      <pre className="max-h-[72dvh] overflow-auto border-t border-slate-200 p-3 text-xs whitespace-pre-wrap break-words">{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

function validationResultsWithoutTraces(results: unknown): unknown {
  if (!Array.isArray(results)) return results;
  return results.map((result) => {
    if (result === null || typeof result !== 'object') return result;
    return Object.fromEntries(
      Object.entries(result).filter(([key]) => key !== 'traces'),
    );
  });
}

type LlmHttpEvent = {
  type: 'llm_http_request' | 'llm_http_response';
  request_id: string;
  at?: string;
  scope?: { phase?: string; region?: number; passage_id?: string; page?: number | null; question_id?: string; metric?: string };
  body?: unknown;
  status_code?: number;
  duration_ms?: number;
};

async function copyLogJson(value: unknown): Promise<void> {
  const serialized = JSON.stringify(value, null, 2);
  if (typeof serialized !== 'string') throw new Error('Kein kopierbarer Log-Inhalt vorhanden.');

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(serialized);
      return;
    } catch {
      // The fallback also works when clipboard permissions are unavailable.
    }
  }

  const input = document.createElement('textarea');
  input.value = serialized;
  input.setAttribute('readonly', '');
  input.style.position = 'fixed';
  input.style.opacity = '0';
  document.body.appendChild(input);
  input.select();
  try {
    if (!document.execCommand('copy')) throw new Error('Kopieren wurde vom Browser blockiert.');
  } finally {
    input.remove();
  }
}

function CopyLogButton({ label, value }: { label: string; value: unknown }) {
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle');

  async function handleCopy() {
    try {
      await copyLogJson(value);
      setCopyState('copied');
    } catch {
      setCopyState('failed');
    }
  }

  return (
    <button
      type="button"
      className="rounded-md border border-sky-300 bg-white px-2 py-1 text-xs font-medium text-sky-900 hover:bg-sky-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600"
      onClick={() => void handleCopy()}
      aria-label={label}
    >
      <span aria-live="polite">{copyState === 'copied' ? 'Kopiert ✓' : copyState === 'failed' ? 'Kopieren fehlgeschlagen' : label}</span>
    </button>
  );
}

type ClassificationResult = {
  status?: string;
  section_ids?: string[];
  decisions?: { sections?: Array<{ id: string; kind: string }> };
  error_type?: string;
  error_message?: string;
};

type ClassifiedSection = {
  id: string;
  heading?: string;
  page?: number | null;
  preview?: string;
  excluded?: boolean;
};

const CLASSIFICATION_LABELS: Record<string, string> = {
  content: 'Inhalt', cover: 'Deckblatt', title_page: 'Titelseite',
  contents: 'Inhaltsverzeichnis', table_index: 'Tabellenverzeichnis',
  figure_index: 'Abbildungsverzeichnis', abbreviations: 'Abkürzungsverzeichnis',
  glossary: 'Glossar',
};

function ClassificationOverview({ results, sections, requestCount }: {
  results: ClassificationResult[];
  sections: ClassifiedSection[];
  requestCount: number;
}) {
  const byId = new Map(sections.map((section) => [section.id, section]));
  const decisions = results.flatMap((result) => {
    const byDecisionId = new Map((result.decisions?.sections ?? []).map((decision) => [decision.id, decision]));
    const ids = result.section_ids?.length ? result.section_ids : [...byDecisionId.keys()];
    return ids.map((id) => ({ id, kind: byDecisionId.get(id)?.kind ?? null, section: byId.get(id) }));
  });
  const classifiedIds = new Set(decisions.map((decision) => decision.id));
  const answeredCount = decisions.filter((decision) => decision.kind !== null).length;
  const ruleExcluded = sections.filter((section) => section.excluded && !classifiedIds.has(section.id));
  const notReviewed = Math.max(0, sections.length - classifiedIds.size - ruleExcluded.length);
  const errors = results.filter((result) => result.status === 'model_error');

  return (
    <section className="space-y-3">
      {ruleExcluded.length > 0 && (
        <details open className="rounded-lg border border-sky-200 bg-sky-50 text-xs text-sky-950">
          <summary className="cursor-pointer px-4 py-3 text-sm font-semibold">Vorab per Überschrift ausgeschlossen · {ruleExcluded.length}</summary>
          <div className="border-t border-sky-200 px-4 py-3">
            <ul className="space-y-1">
              {ruleExcluded.map((section) => <li key={section.id}>Seite {section.page ?? '–'} · {section.heading || 'Ohne Überschrift'} ({section.id})</li>)}
            </ul>
          </div>
        </details>
      )}
      <details className="rounded-lg border-2 border-sky-400 bg-sky-50">
        <summary className="cursor-pointer px-4 py-3 text-sm font-semibold text-sky-950">Abschnittsklassifizierung · {answeredCount} LLM-Entscheidungen</summary>
        <div className="space-y-3 border-t border-sky-200 px-4 py-3 text-xs text-sky-950">
          {decisions.length > 0 && <div className="flex justify-end"><CopyLogButton label="Einordnungen kopieren" value={decisions.map(({ id, kind, section }) => ({ id, heading: section?.heading, page: section?.page, kind, excluded: section?.excluded }))} /></div>}
          <p>{requestCount} protokollierte LLM-Anfrage{requestCount === 1 ? '' : 'n'} für die Klassifizierung. Prompt und Modellantwort stehen unten bei den API-Anfragen.</p>
          {sections.length > 0 && <p>{notReviewed} weitere Abschnitte wurden nicht einzeln vom LLM klassifiziert.</p>}
          {errors.map((result, index) => <p key={index} className="text-amber-800">Klassifizierungsfehler: {result.error_type ?? 'unbekannt'}{result.error_message ? ' · ' + result.error_message : ''}</p>)}
          {decisions.length === 0 ? (
            <p className="text-slate-600">Noch keine Klassifizierungsentscheidungen protokolliert.</p>
          ) : (
            <div className="max-h-[65dvh] overflow-auto rounded-md border border-sky-200 bg-white">
              <table className="w-full min-w-[600px] text-left text-xs">
                <thead className="sticky top-0 bg-sky-100 text-sky-950">
                  <tr><th className="px-3 py-2">Abschnitt</th><th className="px-3 py-2">Seite</th><th className="px-3 py-2">Vom LLM eingeordnet als</th><th className="px-3 py-2">Quellenauswahl</th></tr>
                </thead>
                <tbody className="divide-y divide-sky-100">
                  {decisions.map(({ id, kind, section }) => (
                    <tr key={id}>
                      <td className="px-3 py-2 align-top">
                        <span className="font-medium">{section?.heading || 'Ohne Überschrift'}</span> <span className="text-slate-500">({id})</span>
                        {section?.preview && <p className="mt-1 max-w-xl text-slate-600">{section.preview}</p>}
                      </td>
                      <td className="px-3 py-2 align-top">{section?.page ?? '–'}</td>
                      <td className="px-3 py-2 align-top font-medium">{kind ? CLASSIFICATION_LABELS[kind] ?? kind : 'Keine Entscheidung'}</td>
                      <td className="px-3 py-2 align-top">{section?.excluded ? 'Ausgeschlossen' : kind ? 'Für Auswahl zugelassen' : 'Mangels Entscheidung belassen'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </details>
    </section>
  );
}

function LlmRequestTimeline({ events }: {
  events: LlmHttpEvent[] | undefined;
}) {
  const responses = new Map(events?.filter((event) => event.type === 'llm_http_response').map((event) => [event.request_id, event]) ?? []);
  const requests = events?.filter((event) => event.type === 'llm_http_request') ?? [];
  const phaseLabel = (phase?: string) => phase === 'classification' ? 'Abschnittsauswahl' : phase === 'generation' ? 'Fragengenerierung' : phase === 'validation' ? 'Validierung' : phase ?? 'Unbekannte Phase';
  return (
    <details open className="rounded-lg border-2 border-sky-400 bg-sky-50">
      <summary className="cursor-pointer px-4 py-3 text-sm font-semibold text-sky-950">Tatsächliche LLM-API-Anfragen · {requests.length}</summary>
      <div className="space-y-2 border-t border-sky-200 px-4 py-3">
        <p className="text-xs text-sky-900">Chronologisch protokollierte Anfragen einschließlich Klassifizierung, RAGAS-interner Prompts und Validierung samt API-Antworten. Wiederholungen erscheinen als eigene Anfragen. HTTP-Header und Zugangsdaten werden nicht gespeichert.</p>
        {!events && <p className="text-xs text-amber-800">Für diesen älteren Lauf wurden die tatsächlichen API-Anfragen nicht erfasst. Die an RAGAS übergebenen Ausschnitte stehen im Tab „Prozessdetails“.</p>}
        {events && requests.length === 0 && <p className="text-xs text-slate-600">Bisher wurde keine LLM-API-Anfrage protokolliert.</p>}
        {requests.map((request, index) => {
          const response = responses.get(request.request_id);
          const payload = request.body && typeof request.body === 'object' ? request.body as Record<string, unknown> : {};
          const scope = request.scope ?? {};
          return (
            <details key={request.request_id} className="rounded-md border border-sky-200 bg-white">
              <summary className="cursor-pointer px-3 py-2 text-xs font-medium">
                {index + 1}. {phaseLabel(scope.phase)} · {request.at ? new Date(request.at).toLocaleString('de-DE') : 'Zeit unbekannt'} · {String(payload.model ?? 'Modell unbekannt')}
                {scope.question_id ? ' · ' + scope.question_id : ''}{scope.metric ? ' · ' + scope.metric : ''}{scope.region ? ' · Bereich ' + scope.region : ''}{scope.page ? ' · Seite ' + scope.page : ''}{scope.passage_id ? ' · ' + scope.passage_id : ''}
                {' · '}{response ? 'HTTP ' + response.status_code + ' · ' + response.duration_ms + ' ms' : 'Keine API-Antwort erfasst'}
              </summary>
              <div className="space-y-2 border-t border-sky-100 p-3">
                <div className="flex justify-end">
                  <CopyLogButton label="Gesamten Eintrag kopieren" value={{ request, response: response ?? null }} />
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-xs font-semibold">Exakter JSON-Anfragekörper</p>
                  <CopyLogButton label="Anfrage kopieren" value={request.body} />
                </div>
                <pre className="max-h-[60dvh] overflow-auto rounded bg-slate-50 p-3 text-xs whitespace-pre-wrap break-words">{JSON.stringify(request.body, null, 2)}</pre>
                {response && (
                  <>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <p className="text-xs font-semibold">API-Antwort</p>
                      <CopyLogButton label="Antwort kopieren" value={response.body} />
                    </div>
                    <pre className="max-h-[60dvh] overflow-auto rounded bg-slate-50 p-3 text-xs whitespace-pre-wrap break-words">{JSON.stringify(response.body, null, 2)}</pre>
                  </>
                )}
              </div>
            </details>
          );
        })}
      </div>
    </details>
  );
}

function GenerationLogDetails({ runId, item, datasetChangedSinceRun }: { runId: string; item: GenerationItem; datasetChangedSinceRun: boolean }) {
  const [open, setOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<'llm' | 'details'>('llm');
  const [log, setLog] = useState<GenerationLog | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    let active = true;
    const load = async () => {
      try {
        const result = await apiJson<GenerationLog>(URL + '/' + runId + '/audit/' + item.job_id + '/download');
        if (active) { setLog(result); setError(null); }
      } catch (cause) {
        if (active) setError(String(cause));
      }
    };
    void load();
    const timer = item.status === 'processing' ? window.setInterval(() => void load(), 3000) : null;
    return () => { active = false; if (timer != null) window.clearInterval(timer); };
  }, [open, runId, item.job_id, item.status]);

  useEffect(() => {
    if (!open) return;
    closeButtonRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open]);

  async function downloadLog() {
    setDownloading(true);
    setDownloadError(null);
    try {
      const response = await apiFetch(URL + '/' + runId + '/audit/' + item.job_id + '/download');
      if (!response.ok) throw new Error('Download fehlgeschlagen (HTTP ' + response.status + ').');
      const objectUrl = window.URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = objectUrl;
      link.download = 'dataset-log-' + item.job_id + '.json';
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => window.URL.revokeObjectURL(objectUrl), 1000);
    } catch (cause) {
      setDownloadError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setDownloading(false);
    }
  }

  const audit = log?.audit;
  const prepared = audit?.prepared_document as {
    text?: string; picture_text_blocks_removed?: number; page_furniture_lines_removed?: number;
    sha256?: string;
    reconstructed_at_read_time?: boolean;
  } | undefined;
  const sentContexts = Array.isArray(audit?.generation_inputs)
    ? audit.generation_inputs as Array<{ region?: number; page?: number | null; passage_id?: string; context?: string }>
    : [];
  const llmEvents = Array.isArray(audit?.llm_http_events) ? audit.llm_http_events as LlmHttpEvent[] : undefined;
  const classificationResults = Array.isArray(audit?.classification_results)
    ? audit.classification_results as ClassificationResult[] : [];
  const sectionInventory = Array.isArray(audit?.section_inventory)
    ? audit.section_inventory as ClassifiedSection[] : [];
  const classificationRequestCount = llmEvents?.filter((event) =>
    event.type === 'llm_http_request' && event.scope?.phase === 'classification').length ?? 0;
  const hasClassificationOverview = classificationRequestCount > 0 || classificationResults.length > 0 || sectionInventory.length > 0;
  const rejectedDetails = item.validation_summary?.details?.filter((detail) => detail.status === 'rejected') ?? [];
  const reviewErrors = item.validation_summary?.details?.filter((detail) => detail.status === 'review' && detail.errors?.length) ?? [];
  return (
    <div className="mt-2 flex flex-wrap items-center justify-between gap-2 rounded-md border border-slate-200 bg-slate-50 p-2 text-xs text-slate-700">
      <button type="button" className="font-medium text-slate-800 underline" aria-haspopup="dialog" onClick={() => { setActiveTab('llm'); setOpen(true); }}>
        Log ansehen
      </button>
      <Button type="button" size="sm" variant="outline" disabled={downloading} onClick={() => void downloadLog()}>
        {downloading ? <LoaderCircle className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Download className="mr-1 h-3.5 w-3.5" />}
        Log herunterladen (.json)
      </Button>
      {downloadError && <p className="w-full text-red-700">{downloadError}</p>}
      {open && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/70 p-2 sm:p-5">
          <section role="dialog" aria-modal="true" aria-labelledby={'dataset-log-title-' + item.job_id} className="flex h-[94dvh] w-full max-w-[1600px] flex-col overflow-hidden rounded-xl bg-white text-slate-800 shadow-2xl">
            <header className="flex shrink-0 items-center justify-between gap-3 border-b border-slate-200 px-4 py-3">
              <h3 id={'dataset-log-title-' + item.job_id} className="text-base font-semibold">Generierungslog</h3>
              <button ref={closeButtonRef} type="button" className="rounded-md border border-slate-300 px-3 py-1.5 text-sm hover:bg-slate-50" onClick={() => setOpen(false)}>Schließen</button>
            </header>
            <div role="tablist" aria-label="Generierungslog-Ansichten" className="flex shrink-0 gap-1 border-b border-slate-200 px-3 pt-2 sm:px-5">
              {(['llm', 'details'] as const).map((tab) => (
                <button
                  key={tab}
                  id={'dataset-log-tab-' + tab + '-' + item.job_id}
                  type="button"
                  role="tab"
                  aria-selected={activeTab === tab}
                  aria-controls={'dataset-log-panel-' + tab + '-' + item.job_id}
                  tabIndex={activeTab === tab ? 0 : -1}
                  className={'rounded-t-lg border border-b-0 px-4 py-2 text-sm font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-sky-600 '
                    + (activeTab === tab ? 'border-sky-300 bg-sky-50 text-sky-950' : 'border-transparent text-slate-600 hover:bg-slate-50 hover:text-slate-900')}
                  onClick={() => setActiveTab(tab)}
                  onKeyDown={(event) => {
                    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
                    event.preventDefault();
                    const next = event.key === 'Home' ? 'llm'
                      : event.key === 'End' ? 'details'
                        : tab === 'llm' ? 'details' : 'llm';
                    setActiveTab(next);
                    document.getElementById('dataset-log-tab-' + next + '-' + item.job_id)?.focus();
                  }}
                >
                  {tab === 'llm' ? 'LLM-Kommunikation' : 'Prozessdetails'}
                </button>
              ))}
            </div>
            <div
              id={'dataset-log-panel-' + activeTab + '-' + item.job_id}
              key={activeTab}
              role="tabpanel"
              aria-labelledby={'dataset-log-tab-' + activeTab + '-' + item.job_id}
              tabIndex={0}
              className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3 sm:p-5"
            >
              {error && <p className="text-red-700">Log konnte nicht geladen werden: {error}</p>}
              {!log && !error && <p>Log wird geladen …</p>}
              {log && audit && activeTab === 'llm' && (
                <>
                  {hasClassificationOverview && (
                    <ClassificationOverview
                      results={classificationResults}
                      sections={sectionInventory}
                      requestCount={classificationRequestCount}
                    />
                  )}
                  <LlmRequestTimeline events={llmEvents} />
                </>
              )}
              {log && audit && activeTab === 'details' && (
                <>
                  {datasetChangedSinceRun && <p className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">Dieses Log gehört zu einem früheren Lauf. Die Dataset-Datei wurde danach verändert oder überschrieben; „Dataset öffnen“ zeigt den aktuellen Dateistand.</p>}
                  {item.validation_summary && (
                    <section className="rounded-lg border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
                      <p className="font-semibold">Ergebnis dieses Laufs: {item.validation_summary.candidates} Kandidaten erzeugt, {item.row_count ?? 0} Fragen gespeichert.</p>
                      {rejectedDetails.map((detail) => (
                        <p key={detail.id} className="mt-1 break-words">{detail.id} wurde generiert, aber nicht gespeichert: {rejectionReason(detail, item.validation_summary!)}.</p>
                      ))}
                      {reviewErrors.map((detail) => (
                        <p key={detail.id} className="mt-1 break-words">{detail.id} wurde gespeichert, muss aber manuell geprüft werden: {reviewErrorReason(detail.errors ?? [])}</p>
                      ))}
                    </section>
                  )}
                  <p className="text-xs text-slate-600">Ablauf: Markdown bereinigen → Abschnitte klassifizieren → einzelne Ausschnitte mit RAGAS zur Fragegenerierung nutzen{log.settings.ragas_validate === false ? ' → Fragen ungeprüft speichern.' : ' → Fragen validieren.'}</p>
                  <details className="rounded-lg border border-sky-200 bg-sky-50">
                    <summary className="cursor-pointer px-4 py-3 text-sm font-semibold text-sky-950">An RAGAS übergebene Textausschnitte · {sentContexts.length}</summary>
                    <div className="space-y-2 border-t border-sky-200 px-4 py-3">
                      <p className="text-xs text-sky-900">Diese Ausschnitte erhielt RAGAS nach Bereinigung und Abschnittsfilterung. Die tatsächlichen Modell-Prompts und Antworten stehen im Tab „LLM-Kommunikation“.</p>
                      {sentContexts.length === 0 && <p className="text-xs text-slate-600">Für diesen Lauf sind keine Textausschnitte protokolliert.</p>}
                      <div className="max-h-[72dvh] space-y-2 overflow-y-auto">
                        {sentContexts.map((input, index) => (
                          <article key={index} className="rounded-md border border-sky-200 bg-white p-3">
                            <p className="mb-2 font-medium">Versuch {index + 1} · Bereich {input.region ?? '–'}{input.page != null ? ' · Seite ' + input.page : ''}{input.passage_id ? ' · ' + input.passage_id : ''}</p>
                            <pre className="text-xs whitespace-pre-wrap break-words">{input.context ?? ''}</pre>
                          </article>
                        ))}
                      </div>
                    </div>
                  </details>
                  <details className="rounded-lg border-2 border-emerald-400 bg-emerald-50">
                    <summary className="cursor-pointer px-4 py-3 text-sm font-semibold text-emerald-950">Bereinigtes Markdown · vor der Abschnittsklassifikation</summary>
                    <div className="border-t border-emerald-200 px-4 py-3">
                      <p className="mb-2 text-xs text-emerald-900">
                        Grafiktext-Blöcke und wiederkehrende Kopf-/Fußzeilen sind entfernt; Seitenmarkierungen bleiben erhalten. Das vollständige Markdown wird nicht als Ganzes an das LLM geschickt.
                      </p>
                      {typeof prepared?.text === 'string' ? (
                        <>
                          <p className="mb-2 text-xs text-emerald-900">Entfernt: {prepared.picture_text_blocks_removed ?? '–'} Grafiktext-Blöcke · {prepared.page_furniture_lines_removed ?? '–'} wiederkehrende Randzeilen</p>
                          {prepared.reconstructed_at_read_time && <p className="mb-2 text-xs text-amber-800">Für diesen älteren Lauf wurde die vollständige bereinigte Fassung nicht protokolliert. Diese Vorschau wurde beim Öffnen mit der aktuellen Bereinigungsfunktion aus der unveränderten Quelle erstellt (Quell-Hash stimmt). Die damaligen RAGAS-Eingaben stehen oben; die tatsächlichen internen LLM-Prompts wurden nicht erfasst.</p>}
                          <pre className="max-h-[72dvh] overflow-auto rounded-md border border-emerald-200 bg-white p-3 text-xs whitespace-pre-wrap break-words">{prepared.text}</pre>
                        </>
                      ) : (
                        <p className="text-xs text-amber-800">Dieser ältere Lauf enthält noch keine vollständige bereinigte Textfassung im Log. Die tatsächlich verwendeten Ausschnitte stehen unter „An RAGAS übergebene Textausschnitte“.</p>
                      )}
                    </div>
                  </details>
                  <LogSection title="Lauf und Einstellungen" value={{ settings: log.settings, result: log.result }} />
                  <LogSection title="Dokumentauswahl und Abschnittsinventar" value={{
                    source_document: audit.source_document,
                    source_file: audit.source_file,
                    source_markdown_sha256: audit.source_markdown_sha256,
                    section_inventory: audit.section_inventory,
                    excluded_sections: audit.excluded_sections,
                    passages_total: audit.passages_total,
                    passages_eligible: audit.passages_eligible,
                  }} />
                  <LogSection title="Generierung: Ergebnisse und Entscheidungen" value={{
                    generation_results: audit.generation_results,
                    region_results: audit.region_results,
                  }} />
                  <LogSection title="Validierung: Kandidaten und Metrikergebnisse" value={{
                    validation_candidates: audit.validation_candidates,
                    validation_results: validationResultsWithoutTraces(audit.validation_results),
                    validation_summary: audit.validation_summary ?? log.result.validation_summary,
                  }} />
                  <LogSection title="Gespeichertes Dataset" value={{
                    dataset_path: audit.dataset_path ?? log.result.dataset_path,
                    dataset_rows: audit.dataset_rows,
                  }} />
                </>
              )}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}

export function DatasetGenerator({ files, datasets, preferredPath, onSaved, onOpenDataset }: {
  files: MarkdownFile[];
  datasets: ExistingDataset[];
  preferredPath: string;
  onSaved: (path: string) => Promise<void> | void;
  onOpenDataset: (path: string) => Promise<void> | void;
}) {
  const [selected, setSelected] = useState<Set<string>>(() => new Set(preferredPath ? [preferredPath] : []));
  const [questionCount, setQuestionCount] = useState(10);
  const [samplingSeed, setSamplingSeed] = useState('');
  const [style, setStyle] = useState('user-paraphrases');
  const [focus, setFocus] = useState('');
  const [existingMode, setExistingMode] = useState<'skip' | 'new' | 'overwrite'>('skip');
  const [overwriteTargets, setOverwriteTargets] = useState<Record<string, string>>({});
  const [config, setConfig] = useState<GenerationConfig | null>(null);
  const [providerId, setProviderId] = useState('');
  const [modelName, setModelName] = useState('');
  const [validationProviderId, setValidationProviderId] = useState('');
  const [validationModelName, setValidationModelName] = useState('');
  const [pipeline, setPipeline] = useState<'ragas' | 'standard'>('ragas');
  const [ragasValidate, setRagasValidate] = useState(true);
  const [ragasQueryMode, setRagasQueryMode] = useState<RagasQueryMode>('facts');
  const [ragasContextChars, setRagasContextChars] = useState<800 | 1400 | 2400>(1400);
  const [ragasPassageTries, setRagasPassageTries] = useState(3);
  const [ragasPersonaName, setRagasPersonaName] = useState(DEFAULT_RAGAS_PERSONA_NAME);
  const [ragasPersonaRoleDescription, setRagasPersonaRoleDescription] = useState(DEFAULT_RAGAS_PERSONA_ROLE_DESCRIPTION);
  const [qualityCriteria, setQualityCriteria] = useState<Record<string, string> | null>(null);
  const [savedQualityCriteria, setSavedQualityCriteria] = useState<Record<string, string> | null>(null);
  const [defaultQualityCriteria, setDefaultQualityCriteria] = useState<Record<string, string> | null>(null);
  const [criteriaSaving, setCriteriaSaving] = useState(false);
  const [refreshingModels, setRefreshingModels] = useState(false);
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<GenerationRun | null>(null);
  const [nowSeconds, setNowSeconds] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const completedRunRef = useRef<string | null>(null);
  const modelChoice = providerId && modelName ? providerId + MODEL_CHOICE_SEPARATOR + modelName : '';
  const validationModelChoice = validationProviderId && validationModelName
    ? validationProviderId + MODEL_CHOICE_SEPARATOR + validationModelName : '';
  const hasModelChoices = config?.providers.some((provider) => provider.models.length > 0) ?? false;
  const criteriaDirty = qualityCriteria !== null && savedQualityCriteria !== null
    && Object.keys(QUALITY_CRITERION_LABELS).some((key) => qualityCriteria[key] !== savedQualityCriteria[key]);
  const criteriaValid = qualityCriteria !== null && Object.keys(QUALITY_CRITERION_LABELS).every((key) => {
    const value = qualityCriteria[key]?.trim();
    return Boolean(value && value.length <= 2000);
  });
  const hasCustomCriteria = savedQualityCriteria !== null && defaultQualityCriteria !== null
    && Object.keys(QUALITY_CRITERION_LABELS).some((key) => savedQualityCriteria[key] !== defaultQualityCriteria[key]);
  const running = Boolean(runId && !run?.finished);
  const hasProcessingItems = run?.items.some((item) => item.status === 'processing') ?? false;
  const settledCount = run?.items.filter((item) => !['queued', 'processing'].includes(item.status)).length ?? 0;
  const runTotal = run?.items.length ?? 0;
  const progressPercent = runTotal > 0
    ? Math.round(run!.items.reduce((total, item) => total + itemProgressPercent(item, run!.pipeline, run!.ragas_validate !== false), 0) / runTotal)
    : 0;

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
        const choice = defaultModelChoice(result);
        const validationChoice = defaultValidationModelChoice(result);
        setProviderId(choice.providerId);
        setModelName(choice.modelName);
        setValidationProviderId(validationChoice.providerId);
        setValidationModelName(validationChoice.modelName);
        setPipeline(choice.providerId === 'hcds' ? 'ragas' : 'standard');
        setRunId(sessionStorage.getItem(RUN_KEY) ?? sessionStorage.getItem(LAST_RUN_KEY));
      })
      .catch((cause: unknown) => { if (active) setError(String(cause)); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    void apiJson<QualityCriteriaSettings>(`${URL}/quality-criteria`)
      .then((result) => {
        if (!active) return;
        setQualityCriteria(result.criteria);
        setSavedQualityCriteria(result.criteria);
        setDefaultQualityCriteria(result.defaults);
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
          const wasActiveRun = sessionStorage.getItem(RUN_KEY) === runId;
          if (wasActiveRun) {
            sessionStorage.setItem(LAST_RUN_KEY, runId);
            sessionStorage.removeItem(RUN_KEY);
          }
          if (wasActiveRun && completedRunRef.current !== runId) {
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
            if (sessionStorage.getItem(LAST_RUN_KEY) === runId) sessionStorage.removeItem(LAST_RUN_KEY);
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
      const currentProvider = result.providers.find((provider) => provider.id === providerId);
      if (!currentProvider?.models.includes(modelName)) {
        const choice = defaultModelChoice(result);
        setProviderId(choice.providerId);
        setModelName(choice.modelName);
        setPipeline(choice.providerId === 'hcds' ? 'ragas' : 'standard');
      }
      const currentValidationProvider = result.providers.find((provider) => provider.id === validationProviderId);
      if (!currentValidationProvider?.models.includes(validationModelName)) {
        const choice = defaultValidationModelChoice(result);
        setValidationProviderId(choice.providerId);
        setValidationModelName(choice.modelName);
      }
    } catch (cause) {
      setError(String(cause));
    } finally {
      setRefreshingModels(false);
    }
  }

  async function persistQualityCriteria(criteria: Record<string, string>) {
    const result = await apiJson<QualityCriteriaSettings>(`${URL}/quality-criteria`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ criteria }),
    });
    setQualityCriteria(result.criteria);
    setSavedQualityCriteria(result.criteria);
    setDefaultQualityCriteria(result.defaults);
  }

  async function saveQualityCriteria() {
    if (!qualityCriteria || !criteriaValid) return;
    setCriteriaSaving(true);
    setError(null);
    try {
      await persistQualityCriteria(qualityCriteria);
    } catch (cause) {
      setError(String(cause));
    } finally {
      setCriteriaSaving(false);
    }
  }

  async function resetQualityCriteria() {
    if (!defaultQualityCriteria) return;
    setCriteriaSaving(true);
    setError(null);
    try {
      await persistQualityCriteria(defaultQualityCriteria);
    } catch (cause) {
      setError(String(cause));
    } finally {
      setCriteriaSaving(false);
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
      if (pipeline === 'ragas' && ragasValidate && criteriaDirty && qualityCriteria) {
        await persistQualityCriteria(qualityCriteria);
      }
      const result = await apiJson<{ run_id: string }>(URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          markdown_paths: [...selected], question_count: questionCount,
          question_style: pipeline === 'ragas' ? 'user-paraphrases' : style,
          focus: pipeline === 'ragas' ? '' : focus,
          skip_existing: existingMode === 'skip',
          overwrite_datasets: existingMode === 'overwrite'
            ? Object.fromEntries(selectedWithDatasets.map((path) => [path, overwriteTargetFor(path)]))
            : {},
          provider: providerId,
          model_name: modelName,
          ...(pipeline === 'ragas' && ragasValidate ? {
            validation_provider: validationProviderId,
            validation_model_name: validationModelName,
          } : {}),
          ragas_validate: ragasValidate,
          pipeline,
          ragas_query_mode: ragasQueryMode,
          ragas_context_chars: ragasContextChars,
          ragas_passage_tries: ragasPassageTries,
          ...(pipeline === 'ragas' ? {
            ragas_persona_name: ragasPersonaName.trim(),
            ragas_persona_role_description: ragasPersonaRoleDescription.trim(),
          } : {}),
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
        <Field label="Generierungsmethode">
          <select className={inputClass} value={pipeline} disabled={running || busy || providerId !== 'hcds'} onChange={(event) => setPipeline(event.target.value as 'ragas' | 'standard')}>
            <option value="ragas">RAGAS</option>
            <option value="standard">Custom Generator</option>
          </select>
        </Field>
        <Field label="Generierungsmodell">
          <div className="flex min-w-0 items-center gap-2">
            <select
              className={inputClass + ' min-w-0 flex-1'}
              value={modelChoice}
              disabled={running || busy || refreshingModels || !hasModelChoices}
              onChange={(event) => {
                const separator = event.target.value.indexOf(MODEL_CHOICE_SEPARATOR);
                const nextProvider = event.target.value.slice(0, separator);
                setProviderId(nextProvider);
                setPipeline(nextProvider === 'hcds' ? 'ragas' : 'standard');
                setModelName(event.target.value.slice(separator + MODEL_CHOICE_SEPARATOR.length));
              }}
            >
              {!hasModelChoices && <option value="">Keine Modelle verfügbar</option>}
              {config?.providers.filter((provider) => provider.models.length > 0).map((provider) => (
                <optgroup key={provider.id} label={provider.label}>
                  {provider.models.map((model) => (
                    <option key={model} value={provider.id + MODEL_CHOICE_SEPARATOR + model}>{model}</option>
                  ))}
                </optgroup>
              ))}
            </select>
            <Button type="button" size="sm" variant="outline" disabled={running || busy || refreshingModels} onClick={() => void refreshModels()} aria-label="Modelle aktualisieren" title="Modelle aktualisieren">
              <RefreshCw className={`h-4 w-4${refreshingModels ? ' animate-spin' : ''}`} />
            </Button>
          </div>
        </Field>
        {pipeline === 'ragas' && <label className="flex items-center gap-2 text-sm md:col-span-2">
          <input type="checkbox" checked={ragasValidate} disabled={running || busy}
            onChange={(event) => setRagasValidate(event.target.checked)} />
          Automatische Validierung aktivieren
          {!ragasValidate && <span className="text-slate-500">Fragen werden ungeprüft gespeichert.</span>}
        </label>}
        {pipeline === 'ragas' && ragasValidate && (
          <Field label="Validierungsmodell (separat)">
            <select
              className={inputClass}
              value={validationModelChoice}
              disabled={running || busy || refreshingModels || !hasModelChoices}
              onChange={(event) => {
                const separator = event.target.value.indexOf(MODEL_CHOICE_SEPARATOR);
                setValidationProviderId(event.target.value.slice(0, separator));
                setValidationModelName(event.target.value.slice(separator + MODEL_CHOICE_SEPARATOR.length));
              }}
            >
              {!hasModelChoices && <option value="">Keine Modelle verfügbar</option>}
              {config?.providers.filter((provider) => provider.models.length > 0).map((provider) => (
                <optgroup key={provider.id} label={provider.label}>
                  {provider.models.map((model) => (
                    <option key={model} value={provider.id + MODEL_CHOICE_SEPARATOR + model}>{model}</option>
                  ))}
                </optgroup>
              ))}
            </select>
          </Field>
        )}
      </div>
      {config?.providers.filter((provider) => provider.models_error).map((provider) => (
        <ErrorNotice key={provider.id} message={provider.models_error ?? null} />
      ))}
      <details className="my-3 border-y border-slate-200 py-2">
        <summary className="cursor-pointer text-sm font-medium">Weitere Einstellungen</summary>
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          {pipeline === 'ragas' && (
            <>
              <Field label="Fragetyp">
                <select className={inputClass} value={ragasQueryMode} disabled={running || busy} onChange={(event) => setRagasQueryMode(event.target.value as RagasQueryMode)}>
                  <option value="facts">Konkrete Faktenfragen</option>
                  <option value="themes">Themenfragen aus einer Quellenstelle</option>
                  <option value="mixed">Gemischt, abschnittsweise abwechselnd</option>
                </select>
              </Field>
              <Field label="Text pro Quellenstelle">
                <select className={inputClass} value={ragasContextChars} disabled={running || busy} onChange={(event) => setRagasContextChars(Number(event.target.value) as 800 | 1400 | 2400)}>
                  <option value={800}>Kurz · bis 800 Zeichen</option>
                  <option value={1400}>Standard · bis 1.400 Zeichen</option>
                  <option value={2400}>Ausführlich · bis 2.400 Zeichen</option>
                </select>
              </Field>
              <Field label="Quellenstellen je Dokumentbereich">
                <select className={inputClass} value={ragasPassageTries} disabled={running || busy} onChange={(event) => setRagasPassageTries(Number(event.target.value))}>
                  <option value={1}>1 Versuch · sparsam</option>
                  <option value={3}>Bis zu 3 Versuche · Standard</option>
                  <option value={5}>Bis zu 5 Versuche · gründlich</option>
                </select>
              </Field>
              <div className="rounded-lg border border-slate-200 p-3 md:col-span-2">
                <p className="mb-2 text-sm font-medium text-slate-700">Generierungs-Persona</p>
                <div className="grid gap-3 md:grid-cols-2">
                  <Field label="Name">
                    <input className={inputClass} value={ragasPersonaName} maxLength={120} disabled={running || busy} onChange={(event) => setRagasPersonaName(event.target.value)} />
                  </Field>
                  <Field label="Rollenbeschreibung">
                    <textarea className={inputClass} rows={3} value={ragasPersonaRoleDescription} maxLength={500} disabled={running || busy} onChange={(event) => setRagasPersonaRoleDescription(event.target.value)} />
                  </Field>
                </div>
              </div>
              {ragasValidate && <details className="rounded-lg border border-slate-200 p-3 md:col-span-2">
                <summary className="cursor-pointer text-sm font-medium text-slate-700">
                  Qualitätskriterien der Validierung{criteriaDirty ? ' · geändert' : ''}
                </summary>
                <div className="mt-3 space-y-3">
                  {qualityCriteria ? Object.entries(QUALITY_CRITERION_LABELS).map(([key, label]) => (
                    <Field key={key} label={label}>
                      <textarea className={inputClass} rows={3} maxLength={2000}
                        value={qualityCriteria[key] ?? ''}
                        disabled={running || busy || criteriaSaving}
                        onChange={(event) => setQualityCriteria((previous) => previous
                          ? { ...previous, [key]: event.target.value } : previous)}
                      />
                    </Field>
                  )) : <p className="text-sm text-slate-500">Kriterien werden geladen.</p>}
                  {qualityCriteria && !criteriaValid && (
                    <p className="text-xs text-rose-700">Alle sechs Kriterien brauchen einen Text.</p>
                  )}
                  <div className="flex flex-wrap items-center gap-2">
                    <Button type="button" size="sm" variant="outline"
                      disabled={running || busy || criteriaSaving || !criteriaDirty || !criteriaValid}
                      onClick={() => void saveQualityCriteria()}>
                      {criteriaSaving ? 'Speichert …' : 'Kriterien speichern'}
                    </Button>
                    <Button type="button" size="sm" variant="outline"
                      disabled={running || busy || criteriaSaving || !qualityCriteria || (!criteriaDirty && !hasCustomCriteria)}
                      onClick={() => void resetQualityCriteria()}>
                      Standard wiederherstellen
                    </Button>
                    {qualityCriteria && <span className="text-xs text-slate-500">
                      {criteriaDirty ? 'Änderungen werden beim Start ebenfalls gespeichert.' : hasCustomCriteria ? 'Für dein Konto gespeichert.' : 'Standard aktiv.'}
                    </span>}
                  </div>
                </div>
              </details>}
            </>
          )}
          {pipeline === 'standard' && (
            <>
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
            </>
          )}
          <Field label="Zufalls-Seed (optional)">
            <input className={inputClass} type="number" min={0} max={2**31 - 1} step={1} value={samplingSeed} disabled={running || busy} onChange={(event) => setSamplingSeed(event.target.value)} placeholder="Zufällig" />
          </Field>
        </div>
        {pipeline === 'ragas' && <p className="mt-2 text-xs text-slate-500">Längere Texte und mehr Versuche können mehr Modellaufrufe verursachen.</p>}
      </details>
      <div className="my-3">
        <Field label="Vorhandene Datasets">
          <select className={inputClass} value={existingMode} disabled={running || busy} onChange={(event) => setExistingMode(event.target.value as 'skip' | 'new' | 'overwrite')}>
            <option value="skip">Bei gleicher Markdown-Fassung überspringen</option>
            <option value="new">Zusätzliches Dataset erstellen</option>
            <option value="overwrite">Ausgewähltes Dataset ersetzen</option>
          </select>
        </Field>
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
                  {markdownFreshness(file) !== 'neuester Markdown-Stand' && <span className="block text-xs text-amber-700">{markdownFreshness(file)}</span>}
                </span>
              </label>
              {isSelected && existingMode !== 'overwrite' && sourceDatasets.length > 0 && (
                <ul className="ml-6 mt-1 space-y-0.5 text-xs text-slate-500">
                  {sourceDatasets.map((dataset) => (
                    <li key={dataset.path} className="break-words">
                      {dataset.filename} <span>({datasetRelationLabel(datasetSourceRelation(dataset, file.path))})</span>
                    </li>
                  ))}
                </ul>
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
        <Button onClick={() => void start()} disabled={busy || running || refreshingModels || criteriaSaving || !validSamplingSeed || missingOverwriteTarget || !config?.configured || !modelName || (pipeline === 'ragas' && ((ragasValidate && (!validationModelName || !criteriaValid)) || !ragasPersonaName.trim() || !ragasPersonaRoleDescription.trim())) || selected.size === 0 || selected.size > 1000}>
          {busy || running ? <LoaderCircle className="mr-2 h-4 w-4 animate-spin" /> : <Sparkles className="mr-2 h-4 w-4" />}Generierung starten
        </Button>
        {running && <Button variant="outline" disabled={busy || run?.cancelled} onClick={() => void cancel()}><StopCircle className="mr-2 h-4 w-4" />Abbrechen</Button>}
      </div>
      {selected.size > 1000 && <ErrorNotice message="Maximal 1000 Dokumente pro Auftrag." />}
      {run && (
        <div className="mt-4" aria-live="polite">
          <p className="mb-1 break-words text-sm text-slate-500">
            {run.pipeline === 'ragas' ? 'Generierungsmodell' : 'Modell'}: {run.model_name}
          </p>
          {run.pipeline === 'ragas' && run.ragas_validate === false && (
            <p className="mb-1 text-sm text-amber-700">Automatische Validierung deaktiviert · Fragen sind noch nicht für die Evaluation freigegeben.</p>
          )}
          {run.pipeline === 'ragas' && run.validation_model_name && (
            <p className="mb-1 break-words text-sm text-slate-500">
              Validierungsmodell: {run.validation_model_name}
              {run.validation_provider ? ' (' + run.validation_provider + ')' : ''}
            </p>
          )}
          <div className="mb-1 flex items-center justify-between gap-3 text-sm font-medium">
            <p>{settledCount} / {runTotal} Quelldateien abgeschlossen{run.finished ? '' : run.cancelled ? ' · Abbruch angefordert' : ''}</p>
            <span>{progressPercent}%</span>
          </div>
          <progress className="h-2 w-full accent-emerald-600" max={100} value={progressPercent} aria-label={`Fortschritt: ${progressPercent}%`} />
          <div className="mt-2 max-h-[70vh] overflow-y-auto">
            {run.items.map((item) => {
              const duration = item.duration_seconds
                ?? (item.status === 'processing' && item.started_at != null && nowSeconds > 0
                  ? Math.max(0, nowSeconds - item.started_at)
                  : null);
              const isProgressComplete = item.status === 'completed' || item.status === 'skipped';
              const currentDataset = datasets.find((dataset) => dataset.path === item.dataset_path);
              const currentUpdatedAt = currentDataset?.updated_at ? Date.parse(currentDataset.updated_at) / 1000 : NaN;
              const datasetChangedSinceRun = item.finished_at != null && Number.isFinite(currentUpdatedAt)
                && currentUpdatedAt > item.finished_at + 5;
              const itemProgress = itemProgressPercent(item, run.pipeline, run.ragas_validate !== false);
              const outcomeMessage = run.pipeline === 'ragas'
                ? ragasOutcomeMessage(item, run.question_count)
                : item.warning;
              return (
                <div key={item.job_id} className="border-b border-slate-100 py-2 text-sm">
                  <p className="break-words">{item.filename}: {item.status === 'processing' && item.progress?.phase === 'validating' ? 'Wird validiert' : STATUS_LABELS[item.status]}{item.row_count ? ` (${item.row_count} Fragen)` : ''}</p>
                  {item.started_at != null && <p className="text-xs text-slate-500">Lauf vom {new Date(item.started_at * 1000).toLocaleString('de-DE')}</p>}
                  {duration != null && <p className="text-xs text-slate-500">{item.status === 'processing' ? 'Läuft seit' : 'Dauer'}: {formatDuration(duration)}</p>}
                  {item.status === 'processing' && item.progress && PHASE_LABELS[item.progress.phase] && (
                    <p className="text-xs text-slate-500">
                      {run.pipeline === 'ragas' ? (
                        item.progress.phase === 'validating'
                          ? `RAGAS · Validierung ${item.progress.validation_done ?? 0}/${item.progress.validation_total ?? '?'} · ${item.progress.validation_accepted ?? 0} akzeptiert · ${item.progress.validation_review ?? 0} prüfen · ${item.progress.validation_rejected ?? 0} verworfen`
                          : `RAGAS · Generierung, Abschnitt ${item.progress.region}/${item.progress.region_count || '?'} · ${item.progress.questions_generated}/${item.progress.question_target} Fragen`
                      ) : (
                        <>
                          {PHASE_LABELS[item.progress.phase]}{item.progress.mode === 'direct' ? '' : ' (Batch)'}
                          {item.progress.stage_total ? ` · ${item.progress.stage_done ?? 0}/${item.progress.stage_total} Anfragen` : ''}
                          {item.progress.round ? ` · Runde ${item.progress.round}/${item.progress.max_rounds ?? ''}` : ''}
                          {` · ${item.progress.candidates ?? 0} Kandidaten · ${item.progress.escalated ?? 0} eskaliert · ${item.progress.rejected ?? 0} verworfen`}
                        </>
                      )}
                    </p>
                  )}
                  {item.validation_summary && (
                    <p className="text-xs text-slate-500">
                      Validierung: {item.validation_summary.accepted != null
                        ? `${item.validation_summary.accepted} unauffällig · ${item.validation_summary.review ?? 0} prüfen · ${item.validation_summary.rejected} verworfen`
                        : `${item.validation_summary.escalated ?? 0} eskaliert · ${item.validation_summary.rejected} verworfen`}
                    </p>
                  )}
                  {item.validation_summary?.details && (
                    <details className="mt-1 text-xs text-slate-600">
                      <summary className="cursor-pointer">RAGAS-Bewertungen je Frage</summary>
                      <ul className="mt-1 space-y-1">
                        {item.validation_summary.details.map((detail) => (
                          <li key={detail.id} className="break-words">
                            {detail.id}: {detail.status === 'accepted' ? 'akzeptiert' : detail.status === 'review' ? 'prüfen' : 'verworfen'} · {detail.question}
                            {' · '}Faithfulness {detail.faithfulness == null ? '–' : detail.faithfulness.toFixed(2)}
                            {' · '}Relevanz {detail.answer_relevancy == null ? '–' : detail.answer_relevancy.toFixed(2)}
                            {detail.criteria && <span> · Qualitätskriterien: {Object.entries(detail.criteria).map(([name, verdict]) => `${QUALITY_CRITERION_LABELS[name] ?? name} ${verdict}`).join(', ')}</span>}
                            {detail.status === 'review' && detail.issues?.length ? <span className="text-amber-700"> · Prüfen: {detail.issues.map((issue) => `${QUALITY_CRITERION_LABELS[issue.metric] ?? issue.metric}: ${issue.reason}`).join('; ')}</span> : null}
                            {detail.status === 'rejected' && <span className="text-amber-700"> · Grund: {rejectionReason(detail, item.validation_summary!)}</span>}
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
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
                  {outcomeMessage && !datasetChangedSinceRun && <p className="break-words text-amber-700">{outcomeMessage}</p>}
                  {item.validation_summary?.details?.filter((detail) => detail.status === 'rejected').map((detail) => (
                    <p key={detail.id} className="mt-1 break-words text-xs text-amber-800">
                      {detail.id} wurde generiert, aber nicht ins Dataset übernommen: {rejectionReason(detail, item.validation_summary!)}. Automatische Bewertung; bei Zweifel fachlich prüfen.
                    </p>
                  ))}
                  {datasetChangedSinceRun && <p className="mt-1 rounded-md border border-amber-200 bg-amber-50 px-2 py-1 text-xs text-amber-900">Die Dataset-Datei wurde nach diesem Lauf geändert oder überschrieben. Bilanz und Log beschreiben den damaligen Lauf; „Dataset öffnen“ zeigt den aktuellen Dateistand.</p>}
                  {item.validation_summary?.details?.filter((detail) => detail.status === 'review' && detail.errors?.length).map((detail) => (
                    <p key={detail.id} className="mt-1 break-words text-xs text-amber-800">
                      {detail.id} wurde gespeichert, muss aber manuell geprüft werden: {reviewErrorReason(detail.errors ?? [])}
                    </p>
                  ))}
                  {run.pipeline !== 'ragas' && item.coverage_note && <p className="mt-1 rounded-md bg-slate-50 px-2 py-1 text-xs text-slate-600">{item.coverage_note}</p>}
                  {run.pipeline === 'ragas' && item.source_audit_available && <GenerationLogDetails runId={run.run_id} item={item} datasetChangedSinceRun={datasetChangedSinceRun} />}
                  {item.dataset_action && item.dataset_filename && <p className="text-xs text-slate-600">{item.dataset_action === 'overwritten' ? 'Bestehendes Dataset ersetzt:' : 'Neues Dataset erstellt:'} {item.dataset_filename}</p>}
                  {item.dataset_path && <button className="text-emerald-700 underline" onClick={() => void onOpenDataset(item.dataset_path!)}>{datasetChangedSinceRun ? 'Aktuelles Dataset öffnen' : 'Dataset öffnen'}</button>}
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
