'use client';

import { useEffect, useState } from 'react';

import { Button } from '@/components/ui/button';
import { apiJson } from '@/lib/api';
import { setCached } from '@/lib/data-cache';
import type { PaddleCapabilities, PaddleSettings } from '@/components/dashboard/shared';
import {
  ErrorNotice,
  errorMessage,
  Field,
  inputClass,
  LoadingState,
  SectionCard,
} from '@/components/admin/admin-shared';

const SETTINGS_PATH = '/api/v1/paddle/settings';
const CAPABILITIES_PATH = '/api/v1/paddle/capabilities';

export function PaddleTab() {
  const [settings, setSettings] = useState<PaddleSettings | null>(null);
  const [capabilities, setCapabilities] = useState<PaddleCapabilities>({ profiles: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Initial load — mirrors useAdminList's mount effect (admin-shared.tsx):
  // the effect body's first statement must be the `await` itself (not a
  // synchronous setState) for react-hooks/set-state-in-effect.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [settingsData, capabilitiesData] = await Promise.all([
          apiJson<PaddleSettings>(SETTINGS_PATH),
          apiJson<PaddleCapabilities>(CAPABILITIES_PATH),
        ]);
        if (cancelled) return;
        setSettings(settingsData);
        setCapabilities(capabilitiesData);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(errorMessage(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const selectedDescription = capabilities.profiles.find(
    (option) => option.value === settings?.default_profile,
  )?.description;

  async function saveSettings() {
    if (!settings) return;
    setSaving(true);
    setSaveError(null);
    setSaveMessage(null);
    const requestedProfile = settings.default_profile;
    try {
      const payload = await apiJson<PaddleSettings>(SETTINGS_PATH, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(settings),
      });
      const nextSettings: PaddleSettings = {
        default_profile: payload.default_profile,
        timeout_seconds: payload.timeout_seconds,
      };
      setSettings(nextSettings);
      // The PUT response is already the authoritative value — push it into
      // the shared cache so any other mounted view (e.g. the processing
      // page's step 1, or Home) reflects the change immediately instead of
      // showing the pre-save value until its own TTL expires.
      setCached(SETTINGS_PATH, nextSettings);
      if (payload.default_profile !== requestedProfile) {
        setSaveMessage(`Profile '${requestedProfile}' is not available. Saved as '${payload.default_profile}'.`);
      } else {
        setSaveMessage('Settings saved');
      }
    } catch (err) {
      setSaveError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <SectionCard
      title="Paddle runtime"
      description="Default OCR profile and processing timeout for every upload that does not override them."
    >
      <ErrorNotice message={error} />
      {loading || !settings ? (
        <LoadingState label="Loading Paddle settings…" />
      ) : (
        <div className="space-y-4">
          <div className="grid gap-4 md:grid-cols-2">
            <Field label="Default profile">
              <select
                className={inputClass}
                value={settings.default_profile}
                onChange={(event) =>
                  setSettings((prev) => (prev ? { ...prev, default_profile: event.target.value } : prev))
                }
              >
                {capabilities.profiles.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              {selectedDescription && (
                <span className="mt-1 block text-xs font-normal text-slate-400">{selectedDescription}</span>
              )}
            </Field>
            <Field label="Timeout (seconds)">
              <input
                type="number"
                min={1}
                className={inputClass}
                value={settings.timeout_seconds}
                onChange={(event) =>
                  setSettings((prev) =>
                    prev ? { ...prev, timeout_seconds: Number(event.target.value) || 1 } : prev,
                  )
                }
              />
            </Field>
          </div>
          <div className="flex items-center gap-3">
            <Button size="sm" onClick={saveSettings} disabled={saving}>
              {saving ? 'Saving…' : 'Save settings'}
            </Button>
            {saveMessage && <span className="text-sm text-slate-600">{saveMessage}</span>}
            {saveError && <span className="text-sm text-red-600">{saveError}</span>}
          </div>
        </div>
      )}
    </SectionCard>
  );
}
