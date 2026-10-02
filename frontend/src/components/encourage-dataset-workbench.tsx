'use client';

import { useEffect, useState } from 'react';
import { Button } from '@/components/ui/button';
import { apiFetch } from '@/lib/api';

type DatasetEntry = {
  path: string;
  filename: string;
  row_count: number;
  source_documents: string[];
  source_files: string[];
};

type DatasetDetail = DatasetEntry & {
  rows: Record<string, unknown>[];
};

type MarkdownEntry = {
  path: string;
  filename: string;
  original_filename: string;
  original_extension: string;
  workspace_folder: string;
};

type WordSource = {
  path: string;
  filename: string;
  extension: string;
  size_bytes: number;
  updated_at: string;
};

type DatasetRow = {
  id: string;
  question: string;
  gold_answer: string;
  evidence_quote: string;
  evidence_anchor: string;
  source_document: string;
  source_file: string;
  notes: string;
};

type DatasetAiAssistResponse = {
  question: string;
  answerable: boolean;
  gold_answer: string;
  evidence_quote: string;
  evidence_anchor: string;
  model_name: string;
  search_mode: 'full_document' | 'exhaustive_passage_search';
  review_note: string;
};

type AiAssistFeedback = {
  tone: 'success' | 'warning' | 'error';
  message: string;
};

type Props = {
  datasets: DatasetEntry[];
  markdownFiles: MarkdownEntry[];
  preferredMarkdownPath: string;
  selectedDatasetPath: string;
  onSelectDataset: (path: string) => void;
  onDatasetSaved: (path: string) => Promise<void> | void;
};

const emptyRow = (
  index: number,
  markdownPath = '',
  sourceFile = '',
): DatasetRow => ({
  id: `q${String(index + 1).padStart(3, '0')}`,
  question: '',
  gold_answer: '',
  evidence_quote: '',
  evidence_anchor: '',
  source_document: markdownPath,
  source_file: sourceFile,
  notes: '',
});

const textValue = (row: Record<string, unknown>, key: keyof DatasetRow) => {
  const value = row[key];
  return value === null || value === undefined ? '' : String(value);
};

const sourceFilename = (path: string) => path.split('/').at(-1) || path;

const nextQuestionId = (rows: DatasetRow[]) => {
  const highestNumber = rows.reduce((highest, row) => {
    const match = row.id.match(/(\d+)$/);
    return Math.max(highest, Number(match?.[1] ?? 0));
  }, 0);
  return `q${String(highestNumber + 1).padStart(3, '0')}`;
};

type DatasetQuestionStyle = 'contract-language' | 'user-paraphrases' | 'mixed-questions';

const QUESTION_STYLE_OPTIONS: Array<{
  id: DatasetQuestionStyle;
  label: string;
  description: string;
}> = [
  {
    id: 'user-paraphrases',
    label: 'Natürliche Nutzerfragen',
    description: 'Alltagssprache und Umschreibungen für semantisches Retrieval.',
  },
  {
    id: 'contract-language',
    label: 'Vertragsnahe Fragen',
    description: 'Fachbegriffe, Zahlen und Formulierungen nah am Dokument.',
  },
  {
    id: 'mixed-questions',
    label: 'Gemischte Fragen',
    description: 'Kombiniert natürliche, vertragsnahe und tabellarische Fragen.',
  },
];

const normalizeSourcePath = (path: string) => path.trim().replaceAll('\\', '/').replace(/^\.\//, '');

const matchingMarkdownEntry = (markdownFiles: MarkdownEntry[], sourcePath: string) => {
  const normalizedSource = normalizeSourcePath(sourcePath);
  return markdownFiles.find((file) => {
    const normalizedFile = normalizeSourcePath(file.path);
    return normalizedSource === normalizedFile || normalizedSource.endsWith(`/${normalizedFile}`);
  });
};

const documentSlug = (file: MarkdownEntry | undefined) => {
  const sourceName = file?.original_filename || file?.filename || 'document';
  return sourceName
    .replace(/\.[^.]+$/, '')
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'document';
};

const nextDatasetFilename = (
  file: MarkdownEntry | undefined,
  questionStyle: DatasetQuestionStyle,
  datasets: DatasetEntry[],
) => {
  const prefix = `${documentSlug(file)}_retrieval_${questionStyle}_v`;
  const versions = datasets
    .map((dataset) => dataset.filename.match(new RegExp(`^${prefix}(\\d+)\\.jsonl$`)))
    .map((match) => Number(match?.[1] ?? 0));
  return `${prefix}${Math.max(0, ...versions) + 1}.jsonl`;
};

const normalizedTextWithOffsets = (value: string) => {
  let normalized = '';
  const offsets: number[] = [];
  let previousWasSpace = false;
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index];
    if (/\s/.test(character)) {
      if (!previousWasSpace && normalized.length > 0) {
        normalized += ' ';
        offsets.push(index);
      }
      previousWasSpace = true;
      continue;
    }
    normalized += character.toLocaleLowerCase('de-DE');
    offsets.push(index);
    previousWasSpace = false;
  }
  return { normalized: normalized.trim(), offsets };
};

const evidenceOffset = (markdown: string, quote: string) => {
  if (!markdown.trim() || !quote.trim()) return -1;
  const haystack = normalizedTextWithOffsets(markdown);
  const needle = normalizedTextWithOffsets(quote).normalized;
  const normalizedIndex = haystack.normalized.indexOf(needle);
  return normalizedIndex < 0 ? -1 : (haystack.offsets[normalizedIndex] ?? -1);
};

const inferEvidenceAnchor = (markdown: string, quote: string) => {
  const offset = evidenceOffset(markdown, quote);
  if (offset < 0) return '';

  const headings: string[] = [];
  for (const line of markdown.slice(0, offset).split('\n')) {
    const match = line.match(/^(#{1,6})\s+(.+?)\s*#*$/);
    if (!match) continue;
    const level = match[1].length;
    headings[level - 1] = match[2].trim();
    headings.length = level;
  }
  return headings.filter(Boolean).join(' > ');
};

export function EncourageDatasetWorkbench({
  datasets,
  markdownFiles,
  preferredMarkdownPath,
  selectedDatasetPath,
  onSelectDataset,
  onDatasetSaved,
}: Props) {
  const [wordSources, setWordSources] = useState<WordSource[]>([]);
  const [details, setDetails] = useState<DatasetDetail | null>(null);
  const [isLoadingDetails, setIsLoadingDetails] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [isCreatingNew, setIsCreatingNew] = useState(false);
  const [selectionBeforeCreate, setSelectionBeforeCreate] = useState('');
  const [filename, setFilename] = useState('retrieval_dataset.jsonl');
  const [filenameWasEdited, setFilenameWasEdited] = useState(false);
  const [questionStyle, setQuestionStyle] = useState<DatasetQuestionStyle>('user-paraphrases');
  const [datasetMarkdownPath, setDatasetMarkdownPath] = useState('');
  const [datasetSourceFile, setDatasetSourceFile] = useState('');
  const [markdownContent, setMarkdownContent] = useState('');
  const [isLoadingMarkdownContent, setIsLoadingMarkdownContent] = useState(false);
  const [rows, setRows] = useState<DatasetRow[]>([
    emptyRow(0, markdownFiles[0]?.path, wordSources[0]?.path),
  ]);
  const [isSaving, setIsSaving] = useState(false);
  const [aiAssistingRowIndex, setAiAssistingRowIndex] = useState<number | null>(null);
  const [aiAssistFeedback, setAiAssistFeedback] = useState<Record<number, AiAssistFeedback>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

  const datasetWordSources = (dataset: DatasetEntry) => {
    const explicitSources = dataset.source_files.map(sourceFilename);
    if (explicitSources.length > 0) return explicitSources;
    return dataset.source_documents
      .map((documentPath) => markdownFiles.find(
        (file) => documentPath === file.path || documentPath.endsWith(`/${file.path}`),
      ))
      .map((file) => file?.original_filename || file?.filename || '')
      .filter(Boolean);
  };

  useEffect(() => {
    const loadWordSources = async () => {
      try {
        const response = await apiFetch('/api/v1/evaluation-source-documents', {
          cache: 'no-store',
        });
        if (!response.ok) return;
        const payload = await response.json();
        setWordSources((payload.items ?? []) as WordSource[]);
      } catch {
        // The workbench remains usable with converted markdown files only.
      }
    };
    void loadWordSources();
  }, []);

  useEffect(() => {
    if (!selectedDatasetPath) {
      setDetails(null);
      return;
    }

    const loadDetails = async () => {
      setIsLoadingDetails(true);
      setLocalError(null);
      try {
        const response = await apiFetch(
          `/api/v1/evaluation-datasets/${encodeURI(selectedDatasetPath)}`,
          { cache: 'no-store' },
        );
        if (!response.ok) {
          setLocalError('Dataset konnte nicht geladen werden.');
          return;
        }
        setDetails((await response.json()) as DatasetDetail);
      } catch {
        setLocalError('Backend beim Laden des Datasets nicht erreichbar.');
      } finally {
        setIsLoadingDetails(false);
      }
    };
    void loadDetails();
  }, [selectedDatasetPath]);

  useEffect(() => {
    if (!datasetMarkdownPath || !isEditing) {
      setMarkdownContent('');
      return;
    }

    const loadMarkdownContent = async () => {
      setIsLoadingMarkdownContent(true);
      try {
        const response = await apiFetch(
          `/api/v1/markdown-files/${encodeURI(datasetMarkdownPath)}`,
          { cache: 'no-store' },
        );
        setMarkdownContent(response.ok ? await response.text() : '');
      } catch {
        setMarkdownContent('');
      } finally {
        setIsLoadingMarkdownContent(false);
      }
    };

    void loadMarkdownContent();
  }, [datasetMarkdownPath, isEditing]);

  const matchingWordSource = (markdownPath: string) => {
    const markdown = matchingMarkdownEntry(markdownFiles, markdownPath);
    if (!markdown?.original_filename) return '';
    return wordSources.find((source) => source.filename === markdown.original_filename)?.path ?? '';
  };

  useEffect(() => {
    if (!isCreatingNew || !datasetMarkdownPath || datasetSourceFile) return;
    const markdown = matchingMarkdownEntry(markdownFiles, datasetMarkdownPath);
    if (!markdown?.original_filename) return;
    const matchedSource = wordSources.find(
      (source) => source.filename === markdown.original_filename,
    );
    if (matchedSource) setDatasetSourceFile(matchedSource.path);
  }, [datasetMarkdownPath, datasetSourceFile, isCreatingNew, markdownFiles, wordSources]);

  const startNewDataset = () => {
    const preferredMarkdown = matchingMarkdownEntry(markdownFiles, preferredMarkdownPath)
      ?? markdownFiles[0];
    const markdownPath = preferredMarkdown?.path ?? '';
    setSelectionBeforeCreate(selectedDatasetPath);
    onSelectDataset('');
    setQuestionStyle('user-paraphrases');
    setDatasetMarkdownPath(markdownPath);
    setDatasetSourceFile(matchingWordSource(markdownPath));
    setFilename(nextDatasetFilename(preferredMarkdown, 'user-paraphrases', datasets));
    setFilenameWasEdited(false);
    setRows([emptyRow(0)]);
    setAiAssistFeedback({});
    setMessage(null);
    setLocalError(null);
    setIsCreatingNew(true);
    setIsEditing(true);
  };

  const startEditingDataset = () => {
    if (!details) return;
    const sourceDocuments = [...new Set(
      details.rows.map((row) => textValue(row, 'source_document')).filter(Boolean),
    )];
    if (sourceDocuments.length > 1) {
      setLocalError(
        'Dieses Dataset enthält mehrere Markdown-Dateien und kann im dokumentbezogenen Formular nicht bearbeitet werden.',
      );
      return;
    }
    const sourceFiles = [...new Set(
      details.rows.map((row) => textValue(row, 'source_file')).filter(Boolean),
    )];
    const markdownEntry = matchingMarkdownEntry(markdownFiles, sourceDocuments[0] ?? '');
    const detectedQuestionStyle = QUESTION_STYLE_OPTIONS.find(
      (option) => details.filename.includes(`_${option.id}_`),
    )?.id ?? 'mixed-questions';
    setFilename(details.filename);
    setFilenameWasEdited(true);
    setQuestionStyle(detectedQuestionStyle);
    setDatasetMarkdownPath(markdownEntry?.path ?? sourceDocuments[0] ?? '');
    setDatasetSourceFile(sourceFiles[0] ?? matchingWordSource(markdownEntry?.path ?? ''));
    setRows(
      details.rows.map((row, index) => ({
        id: textValue(row, 'id') || `q${String(index + 1).padStart(3, '0')}`,
        question: textValue(row, 'question'),
        gold_answer: textValue(row, 'gold_answer'),
        evidence_quote: textValue(row, 'evidence_quote'),
        evidence_anchor: textValue(row, 'evidence_anchor'),
        source_document: textValue(row, 'source_document'),
        source_file: textValue(row, 'source_file'),
        notes: textValue(row, 'notes'),
      })),
    );
    setAiAssistFeedback({});
    setMessage(null);
    setLocalError(null);
    setIsCreatingNew(false);
    setSelectionBeforeCreate('');
    setIsEditing(true);
  };

  const cancelEditing = () => {
    setIsEditing(false);
    if (isCreatingNew && selectionBeforeCreate) {
      onSelectDataset(selectionBeforeCreate);
    }
    setIsCreatingNew(false);
    setSelectionBeforeCreate('');
  };

  const selectDatasetMarkdown = (markdownPath: string) => {
    const markdown = matchingMarkdownEntry(markdownFiles, markdownPath);
    setDatasetMarkdownPath(markdownPath);
    setDatasetSourceFile(matchingWordSource(markdownPath));
    setAiAssistFeedback({});
    if (isCreatingNew && !filenameWasEdited) {
      setFilename(nextDatasetFilename(markdown, questionStyle, datasets));
    }
  };

  const selectQuestionStyle = (value: DatasetQuestionStyle) => {
    setQuestionStyle(value);
    if (isCreatingNew && !filenameWasEdited) {
      const markdown = matchingMarkdownEntry(markdownFiles, datasetMarkdownPath);
      setFilename(nextDatasetFilename(markdown, value, datasets));
    }
  };

  const updateRow = (index: number, field: keyof DatasetRow, value: string) => {
    setRows((current) =>
      current.map((row, rowIndex) => (rowIndex === index ? { ...row, [field]: value } : row)),
    );
    setAiAssistFeedback((current) => {
      if (!(index in current)) return current;
      const next = { ...current };
      delete next[index];
      return next;
    });
  };

  const updateEvidenceAnchor = (index: number) => {
    setRows((current) => current.map((row, rowIndex) => {
      if (rowIndex !== index) return row;
      const inferredAnchor = inferEvidenceAnchor(markdownContent, row.evidence_quote);
      return inferredAnchor ? { ...row, evidence_anchor: inferredAnchor } : row;
    }));
  };

  const prepareRowWithAi = async (index: number) => {
    const row = rows[index];
    if (!datasetMarkdownPath) {
      setAiAssistFeedback((current) => ({
        ...current,
        [index]: { tone: 'error', message: 'Wähle zuerst die Markdown-Datei des Datasets aus.' },
      }));
      return;
    }
    if (!row?.question.trim()) {
      setAiAssistFeedback((current) => ({
        ...current,
        [index]: { tone: 'error', message: 'Formuliere zuerst die Frage, die beantwortet werden soll.' },
      }));
      return;
    }

    setAiAssistingRowIndex(index);
    setAiAssistFeedback((current) => {
      const next = { ...current };
      delete next[index];
      return next;
    });
    try {
      const response = await apiFetch('/api/v1/evaluation-datasets/ai-assist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          markdown_path: datasetMarkdownPath,
          question: row.question,
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        const detail = typeof payload?.detail === 'string'
          ? payload.detail
          : 'Der AI-Vorschlag konnte nicht erstellt werden.';
        setAiAssistFeedback((current) => ({
          ...current,
          [index]: { tone: 'error', message: detail },
        }));
        return;
      }

      const suggestion = (await response.json()) as DatasetAiAssistResponse;
      if (!suggestion.answerable) {
        setAiAssistFeedback((current) => ({
          ...current,
          [index]: {
            tone: 'warning',
            message: suggestion.review_note || 'Im Dokument wurde kein eindeutiger Beleg gefunden.',
          },
        }));
        return;
      }

      setRows((current) => current.map((currentRow, rowIndex) => (
        rowIndex === index
          ? {
              ...currentRow,
              gold_answer: suggestion.gold_answer,
              evidence_quote: suggestion.evidence_quote,
              evidence_anchor: suggestion.evidence_anchor,
            }
          : currentRow
      )));
      setAiAssistFeedback((current) => ({
        ...current,
        [index]: {
          tone: 'success',
          message: `${suggestion.review_note} Modell: ${suggestion.model_name}.`,
        },
      }));
    } catch {
      setAiAssistFeedback((current) => ({
        ...current,
        [index]: { tone: 'error', message: 'Backend für den AI-Vorschlag nicht erreichbar.' },
      }));
    } finally {
      setAiAssistingRowIndex(null);
    }
  };

  const addRow = () => {
    setRows((current) => [
      ...current,
      {
        ...emptyRow(current.length, datasetMarkdownPath, datasetSourceFile),
        id: nextQuestionId(current),
      },
    ]);
  };

  const removeRow = (index: number) => {
    setRows((current) => current.filter((_, rowIndex) => rowIndex !== index));
    setAiAssistFeedback({});
  };

  const saveDataset = async () => {
    if (!datasetMarkdownPath) {
      setLocalError('Wähle eine indexierte Markdown-Datei für das Dataset aus.');
      return;
    }
    const incompleteRowIndex = rows.findIndex(
      (row) => !row.question.trim() || !row.gold_answer.trim() || !row.evidence_quote.trim(),
    );
    if (incompleteRowIndex >= 0) {
      setLocalError(
        `Frage ${incompleteRowIndex + 1}: Frage, Goldantwort und Evidenz-Zitat sind erforderlich.`,
      );
      return;
    }
    const unmatchedEvidenceIndex = markdownContent
      ? rows.findIndex((row) => evidenceOffset(markdownContent, row.evidence_quote) < 0)
      : -1;
    if (unmatchedEvidenceIndex >= 0) {
      setLocalError(
        `Frage ${unmatchedEvidenceIndex + 1}: Das Evidenz-Zitat wurde nicht in der ausgewählten Markdown-Datei gefunden.`,
      );
      return;
    }

    setIsSaving(true);
    setLocalError(null);
    setMessage(null);
    try {
      const response = await apiFetch('/api/v1/evaluation-datasets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename,
          rows: rows.map((row, index) => ({
            ...row,
            id: row.id.trim() || `q${String(index + 1).padStart(3, '0')}`,
            evidence_anchor:
              inferEvidenceAnchor(markdownContent, row.evidence_quote) || row.evidence_anchor,
            source_document: datasetMarkdownPath,
            source_file: datasetSourceFile,
          })),
        }),
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        setLocalError(
          typeof payload?.detail === 'string' ? payload.detail : 'Dataset konnte nicht gespeichert werden.',
        );
        return;
      }
      const saved = (await response.json()) as DatasetDetail;
      setDetails(saved);
      onSelectDataset(saved.path);
      await onDatasetSaved(saved.path);
      setIsEditing(false);
      setIsCreatingNew(false);
      setSelectionBeforeCreate('');
      setMessage(`${saved.filename} wurde mit ${saved.row_count} Fragen gespeichert.`);
    } catch {
      setLocalError('Backend beim Speichern des Datasets nicht erreichbar.');
    } finally {
      setIsSaving(false);
    }
  };

  const downloadDataset = () => {
    if (!details) return;
    const jsonl = `${details.rows.map((row) => JSON.stringify(row)).join('\n')}\n`;
    const url = URL.createObjectURL(new Blob([jsonl], { type: 'application/x-ndjson' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = details.filename;
    link.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="space-y-6">
      <div className="grid gap-4 lg:grid-cols-[1.1fr_0.9fr]">
        <section className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="text-lg font-semibold text-slate-950">Evaluation Data Sets</h3>
              <p className="mt-1 text-sm text-slate-500">
                JSONL-Fragen, Goldantworten und Evidenz für eure Dokumente verwalten.
              </p>
            </div>
            <Button
              onClick={startNewDataset}
              disabled={isEditing}
              className="bg-emerald-600 hover:bg-emerald-700"
            >
              {isEditing ? 'Formular geöffnet' : 'Neues Dataset'}
            </Button>
          </div>

          {isEditing && (
            <div className="mt-4 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
              {isCreatingNew ? 'Ein neues Dataset wird erstellt.' : 'Das ausgewählte Dataset wird bearbeitet.'}{' '}
              Speichere oder brich das Formular ab, um die Auswahl wieder zu verwenden.
            </div>
          )}

          <div className="mt-4 grid gap-2">
            {datasets.length === 0 ? (
              <p className="rounded-xl border border-dashed border-slate-300 p-4 text-sm text-slate-500">
                Noch keine JSONL-Datasets vorhanden.
              </p>
            ) : (
              datasets.map((dataset) => (
                <button
                  key={dataset.path}
                  type="button"
                  disabled={isEditing}
                  onClick={() => onSelectDataset(dataset.path)}
                  className={`rounded-xl border-2 p-3 text-left transition ${
                    isEditing
                      ? 'cursor-not-allowed border-slate-200 bg-slate-50 opacity-55'
                      : dataset.path === selectedDatasetPath
                      ? 'border-purple-500 bg-purple-50'
                      : 'border-slate-200 hover:border-slate-300'
                  }`}
                >
                  {(() => {
                    const originalFiles = datasetWordSources(dataset);
                    return (
                      <>
                  <p className="font-medium text-slate-900">{dataset.filename}</p>
                  <p className="mt-1 text-xs text-slate-500">
                    {dataset.row_count} Fragen
                  </p>
                  <p className="mt-1 text-xs text-slate-600">
                    Word-Original: {originalFiles.length > 0
                      ? originalFiles.join(', ')
                      : 'nicht zugeordnet'}
                  </p>
                      </>
                    );
                  })()}
                </button>
              ))
            )}
          </div>
        </section>

      </div>

      {localError && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
          {localError}
        </div>
      )}
      {message && (
        <div className="rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-700">
          {message}
        </div>
      )}

      {isEditing ? (
        <section className="rounded-2xl border border-emerald-200 bg-white p-5 shadow-sm">
          <div className="mb-4">
            <p className="text-lg font-semibold text-slate-950">
              {isCreatingNew ? 'Neues Dataset erstellen' : 'Dataset bearbeiten'}
            </p>
            <p className="mt-1 text-sm text-slate-500">
              {isCreatingNew
                ? 'Lege Dateiname, Quelldokument und die ersten Evaluationsfragen fest.'
                : 'Passe Fragen, Goldantworten und Evidenz des ausgewählten Datasets an.'}
            </p>
          </div>
          <div className="grid gap-4 rounded-xl border border-emerald-100 bg-emerald-50/50 p-4 md:grid-cols-2">
            <label className="text-sm font-medium text-slate-700 md:col-span-2">
              Indexierte Markdown-Datei
              <select
                value={datasetMarkdownPath}
                onChange={(event) => selectDatasetMarkdown(event.target.value)}
                disabled={aiAssistingRowIndex !== null}
                className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm"
              >
                <option value="">Markdown auswählen</option>
                {markdownFiles.map((file) => (
                  <option key={file.path} value={file.path}>
                    {file.original_filename || file.filename} → {file.filename} ({file.workspace_folder || 'inbox'})
                  </option>
                ))}
              </select>
              <span className="mt-1 block text-xs font-normal text-slate-500">
                Diese Quelle gilt automatisch für alle Fragen im Dataset.
              </span>
            </label>

            <label className="text-sm font-medium text-slate-700">
              Word-Quelldatei
              <select
                value={datasetSourceFile}
                onChange={(event) => setDatasetSourceFile(event.target.value)}
                className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm"
              >
                <option value="">Keine Zuordnung</option>
                {wordSources.map((source) => (
                  <option key={source.path} value={source.path}>{source.filename}</option>
                ))}
              </select>
              <span className="mt-1 block text-xs font-normal text-slate-500">
                Wird anhand des ursprünglichen Dateinamens vorbelegt, sofern möglich.
              </span>
            </label>

            {isCreatingNew && (
              <label className="text-sm font-medium text-slate-700">
                Fragenstil
                <select
                  value={questionStyle}
                  onChange={(event) => selectQuestionStyle(event.target.value as DatasetQuestionStyle)}
                  className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm"
                >
                  {QUESTION_STYLE_OPTIONS.map((option) => (
                    <option key={option.id} value={option.id}>{option.label}</option>
                  ))}
                </select>
                <span className="mt-1 block text-xs font-normal text-slate-500">
                  {QUESTION_STYLE_OPTIONS.find((option) => option.id === questionStyle)?.description}
                </span>
              </label>
            )}

            <label className="text-sm font-medium text-slate-700 md:col-span-2">
              Dateiname
              <input
                value={filename}
                onChange={(event) => {
                  setFilename(event.target.value);
                  setFilenameWasEdited(true);
                }}
                className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm"
                placeholder="dokument_retrieval_user-paraphrases_v1.jsonl"
              />
              <span className="mt-1 block text-xs font-normal text-slate-500">
                Wird aus Dokument, Fragenstil und nächster freier Version erzeugt.
              </span>
            </label>

            <p className={`text-xs md:col-span-2 ${markdownContent ? 'text-emerald-700' : 'text-slate-500'}`}>
              {isLoadingMarkdownContent
                ? 'Markdown wird für die Evidenzprüfung geladen…'
                : markdownContent
                  ? 'Markdown geladen: Evidenz-Zitate und Überschriftenpfade werden automatisch geprüft.'
                  : 'Ohne geladenes Markdown ist keine automatische Evidenzprüfung möglich.'}
            </p>
            {markdownContent && (
              <details className="rounded-lg border border-slate-200 bg-white p-3 md:col-span-2">
                <summary className="cursor-pointer text-sm font-medium text-emerald-800">
                  Markdown als Evidenzquelle öffnen
                </summary>
                <pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-slate-950 p-4 text-xs leading-relaxed text-slate-100">
                  {markdownContent}
                </pre>
              </details>
            )}
          </div>

          <div className="mt-4 flex justify-end gap-2">
            <Button variant="outline" onClick={cancelEditing} disabled={aiAssistingRowIndex !== null}>
              Abbrechen
            </Button>
            <Button
              onClick={saveDataset}
              disabled={isSaving || rows.length === 0 || aiAssistingRowIndex !== null}
            >
              {isSaving ? 'Speichert…' : 'Dataset speichern'}
            </Button>
          </div>

          <div className="mt-5 space-y-4">
            {rows.map((row, index) => (
              <div key={index} className="rounded-xl border border-slate-200 bg-slate-50 p-4">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2">
                    <p className="text-sm font-semibold text-slate-900">Frage {index + 1}</p>
                    <span className="rounded-full bg-slate-200 px-2 py-0.5 font-mono text-[11px] text-slate-600">
                      {row.id}
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => removeRow(index)}
                    disabled={rows.length === 1 || aiAssistingRowIndex !== null}
                    className="text-xs font-medium text-rose-600 disabled:text-slate-300"
                  >
                    Entfernen
                  </button>
                </div>
                <div className="mt-3 grid gap-3 md:grid-cols-2">
                  <div className="md:col-span-2">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <label htmlFor={`dataset-question-${index}`} className="text-xs font-medium text-slate-600">
                        Frage <span className="text-rose-600">*</span>
                      </label>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => prepareRowWithAi(index)}
                        disabled={
                          aiAssistingRowIndex !== null
                          || !datasetMarkdownPath
                          || !row.question.trim()
                        }
                      >
                        {aiAssistingRowIndex === index ? 'AI durchsucht das Dokument…' : 'Mit AI vorbereiten'}
                      </Button>
                    </div>
                    <textarea
                      id={`dataset-question-${index}`}
                      value={row.question}
                      onChange={(event) => updateRow(index, 'question', event.target.value)}
                      disabled={aiAssistingRowIndex === index}
                      rows={2}
                      className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm"
                    />
                    <span className="mt-1 block font-normal text-slate-500">
                      {questionStyle === 'user-paraphrases'
                        ? 'In natürlicher Nutzersprache formulieren, ohne Vertragsbegriffe unnötig zu übernehmen.'
                        : questionStyle === 'contract-language'
                          ? 'Fachbegriffe, Zahlen und Bezeichnungen gezielt aus dem Dokument aufgreifen.'
                          : 'Natürliche, vertragsnahe und tabellarische Fragen bewusst mischen.'}
                    </span>
                    {aiAssistFeedback[index] && (
                      <p
                        aria-live="polite"
                        className={`mt-2 rounded-lg border px-3 py-2 text-xs ${
                          aiAssistFeedback[index].tone === 'success'
                            ? 'border-emerald-200 bg-emerald-50 text-emerald-800'
                            : aiAssistFeedback[index].tone === 'warning'
                              ? 'border-amber-200 bg-amber-50 text-amber-800'
                              : 'border-rose-200 bg-rose-50 text-rose-700'
                        }`}
                      >
                        {aiAssistFeedback[index].message}
                      </p>
                    )}
                  </div>
                  <label className="text-xs font-medium text-slate-600 md:col-span-2">
                    Goldantwort <span className="text-rose-600">*</span>
                    <textarea value={row.gold_answer} onChange={(event) => updateRow(index, 'gold_answer', event.target.value)} rows={3} className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm" />
                    <span className="mt-1 block font-normal text-slate-500">
                      Kurze ideale Antwort, ausschließlich durch die angegebene Evidenz belegt.
                    </span>
                  </label>
                  <label className="text-xs font-medium text-slate-600 md:col-span-2">
                    Evidenz-Zitat <span className="text-rose-600">*</span>
                    <textarea
                      value={row.evidence_quote}
                      onChange={(event) => updateRow(index, 'evidence_quote', event.target.value)}
                      onBlur={() => updateEvidenceAnchor(index)}
                      rows={3}
                      className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm"
                    />
                    <span className={`mt-1 block font-normal ${
                      row.evidence_quote && markdownContent
                        ? evidenceOffset(markdownContent, row.evidence_quote) >= 0
                          ? 'text-emerald-700'
                          : 'text-amber-700'
                        : 'text-slate-500'
                    }`}>
                      {row.evidence_quote && markdownContent
                        ? evidenceOffset(markdownContent, row.evidence_quote) >= 0
                          ? 'Exaktes Zitat im ausgewählten Markdown gefunden.'
                          : 'Noch kein exakter Treffer im ausgewählten Markdown.'
                        : 'Kopiere die kleinste Textstelle, die die Goldantwort vollständig belegt.'}
                    </span>
                  </label>
                  <label className="text-xs font-medium text-slate-600">
                    Evidenz-Anker
                    <input value={row.evidence_anchor} onChange={(event) => updateRow(index, 'evidence_anchor', event.target.value)} className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm" />
                    <span className="mt-1 block font-normal text-slate-500">
                      Wird aus dem Überschriftenpfad des Zitats vorbelegt.
                    </span>
                  </label>
                  <label className="text-xs font-medium text-slate-600">
                    Notizen <span className="font-normal text-slate-400">(optional)</span>
                    <input value={row.notes} onChange={(event) => updateRow(index, 'notes', event.target.value)} className="mt-1 w-full rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm" />
                  </label>
                </div>
              </div>
            ))}
          </div>
          <Button
            variant="outline"
            onClick={addRow}
            disabled={aiAssistingRowIndex !== null}
            className="mt-4"
          >
            Frage hinzufügen
          </Button>
        </section>
      ) : (
        <section className="rounded-2xl border border-purple-200 bg-white p-5 shadow-sm">
          {isLoadingDetails ? (
            <p className="text-sm text-slate-500">Dataset wird geladen…</p>
          ) : details ? (
            <>
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <h3 className="text-lg font-semibold text-slate-950">{details.filename}</h3>
                  <p className="mt-1 text-sm text-slate-500">{details.row_count} Fragen</p>
                  <p className="mt-1 text-sm text-slate-600">
                    Word-Original: {details.source_files.length > 0
                      ? details.source_files.map(sourceFilename).join(', ')
                      : 'nicht zugeordnet'}
                  </p>
                </div>
                <div className="flex gap-2">
                  <Button variant="outline" onClick={downloadDataset}>JSONL herunterladen</Button>
                  <Button onClick={startEditingDataset}>Bearbeiten</Button>
                </div>
              </div>
              <div className="mt-4 max-h-[36rem] space-y-3 overflow-y-auto pr-1">
                {details.rows.map((row, index) => (
                  <article key={String(row.id ?? index)} className="rounded-xl border border-purple-100 bg-purple-50/40 p-4">
                    <p className="text-xs font-semibold uppercase tracking-wide text-purple-700">{String(row.id ?? `Frage ${index + 1}`)}</p>
                    <p className="mt-1 font-medium text-slate-900">{String(row.question ?? '')}</p>
                    <p className="mt-3 text-xs font-semibold text-slate-600">Goldantwort</p>
                    <p className="mt-1 whitespace-pre-wrap text-sm text-slate-700">{String(row.gold_answer ?? '')}</p>
                    {Boolean(row.evidence_quote) && <p className="mt-3 rounded-lg bg-white p-3 text-xs text-slate-600">{String(row.evidence_quote)}</p>}
                    <div className="mt-3 grid gap-1 text-xs text-slate-500">
                      <p>Word-Original: {sourceFilename(String(row.source_file ?? '')) || 'nicht zugeordnet'}</p>
                    </div>
                  </article>
                ))}
              </div>
            </>
          ) : (
            <p className="text-sm text-slate-500">Wähle ein Dataset aus oder lege ein neues an.</p>
          )}
        </section>
      )}
    </div>
  );
}
