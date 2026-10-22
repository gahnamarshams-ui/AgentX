import { useEffect, useState } from 'react';
export type ThemePreference = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';
const STORAGE_KEY = 'agentx.theme';
function valid(value: string | null): value is ThemePreference { return value === 'light' || value === 'dark' || value === 'system'; }
function readPreference(): ThemePreference {
  try { const value = window.localStorage.getItem(STORAGE_KEY); return valid(value) ? value : 'system'; }
  catch { return 'system'; }
}
function resolve(preference: ThemePreference): ResolvedTheme {
  return preference === 'system' ? (window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : preference;
}
function apply(preference: ThemePreference): ResolvedTheme {
  const resolved = resolve(preference);
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.themePreference = preference;
  return resolved;
}

/** One owner in the workspace shell; Settings and header share this state. */
export function useTheme() {
  const [preference, setPreference] = useState<ThemePreference>(readPreference);
  const [resolved, setResolved] = useState<ResolvedTheme>(() => resolve(preference));
  const [persistenceError, setPersistenceError] = useState('');
  useEffect(() => {
    // Discard the retired color preference; appearance now uses fixed CSS tokens.
    try { window.localStorage.removeItem('agentx.accent'); } catch { /* Appearance still works without storage. */ }
  }, []);
  useEffect(() => {
    setResolved(apply(preference));
    const media = window.matchMedia?.('(prefers-color-scheme: dark)');
    const update = () => { if (preference === 'system') setResolved(apply(preference)); };
    media?.addEventListener?.('change', update);
    return () => media?.removeEventListener?.('change', update);
  }, [preference]);
  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.key !== STORAGE_KEY && event.key !== null) return;
      try { if (event.storageArea && event.storageArea !== window.localStorage) return; } catch { return; }
      const next = valid(event.newValue) ? event.newValue : 'system';
      setPreference(next); setResolved(apply(next)); setPersistenceError('');
    };
    window.addEventListener('storage', sync);
    return () => window.removeEventListener('storage', sync);
  }, []);
  function choose(next: ThemePreference) {
    setPreference(next); setResolved(apply(next));
    try { window.localStorage.setItem(STORAGE_KEY, next); setPersistenceError(''); }
    catch { setPersistenceError('This browser could not save the preference. Your theme applies for this session.'); }
  }
  return { preference, resolved, choose, reset: () => choose('system'), persistenceError };
}
export type ThemeState = ReturnType<typeof useTheme>;
