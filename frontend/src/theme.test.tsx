import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { HeaderThemeControl, ThemeSettings } from './ThemeControl';
import { useTheme } from './theme';
import startupScript from '../public/theme-init.js?raw';

let dark = false;
const listeners = new Set<() => void>();
function Harness() {
  const theme = useTheme();
  return <><HeaderThemeControl {...theme} /><ThemeSettings {...theme} />
    {theme.persistenceError && <p role="alert">{theme.persistenceError}</p>}</>;
}
function device(value: boolean) { act(() => { dark = value; listeners.forEach(listener => listener()); }); }
beforeEach(() => {
  localStorage.clear(); dark = false; listeners.clear();
  delete document.documentElement.dataset.theme;
  delete document.documentElement.dataset.themePreference;
  vi.stubGlobal('matchMedia', vi.fn(() => ({ get matches() { return dark; },
    addEventListener: (_event: string, callback: () => void) => listeners.add(callback),
    removeEventListener: (_event: string, callback: () => void) => listeners.delete(callback),
  })));
});
afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

describe('theme preferences', () => {
  it('defaults to System and follows the current device appearance', () => {
    dark = true; render(<Harness />);
    expect(screen.getByRole('radio', { name: 'System' })).toBeChecked();
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
  });
  it('persists explicit preferences, synchronizes both controls and restores on remount', () => {
    const first = render(<Harness />);
    fireEvent.click(screen.getByRole('radio', { name: 'Dark' }));
    expect(localStorage.getItem('agentx.theme')).toBe('dark');
    expect(screen.getByLabelText('Theme preference')).toHaveValue('dark');
    first.unmount(); render(<Harness />);
    expect(screen.getByRole('radio', { name: 'Dark' })).toBeChecked();
    fireEvent.change(screen.getByLabelText('Theme preference'), { target: { value: 'light' } });
    expect(screen.getByRole('radio', { name: 'Light' })).toBeChecked();
    expect(document.documentElement).toHaveAttribute('data-theme', 'light');
    expect(localStorage.getItem('agentx.theme')).toBe('light');
  });
  it('updates System immediately when the OS changes and ignores it for explicit choices', () => {
    render(<Harness />);
    expect(document.documentElement).toHaveAttribute('data-theme', 'light');
    device(true); expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    fireEvent.click(screen.getByRole('radio', { name: 'Light' }));
    device(false); device(true);
    expect(document.documentElement).toHaveAttribute('data-theme', 'light');
    fireEvent.click(screen.getByRole('radio', { name: 'System' }));
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    expect(localStorage.getItem('agentx.theme')).toBe('system');
  });
  it('handles invalid stored values and cross-tab updates without accepting invalid themes', () => {
    localStorage.setItem('agentx.theme', 'untrusted-value'); render(<Harness />);
    expect(screen.getByRole('radio', { name: 'System' })).toBeChecked();
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'agentx.theme', newValue: 'dark' })));
    expect(screen.getByRole('radio', { name: 'Dark' })).toBeChecked();
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'agentx.theme', newValue: 'invalid' })));
    expect(screen.getByRole('radio', { name: 'System' })).toBeChecked();
  });
  it('handles storage clearing in another tab', () => {
    localStorage.setItem('agentx.theme', 'dark'); render(<Harness />);
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: null, newValue: null })));
    expect(screen.getByRole('radio', { name: 'System' })).toBeChecked();
    expect(document.documentElement).toHaveAttribute('data-theme', 'light');
  });
  it('applies a session theme and reports the failure if storage is blocked', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('Blocked'); });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('Blocked'); });
    render(<Harness />);
    fireEvent.click(screen.getByRole('radio', { name: 'Dark' }));
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    expect(await screen.findByRole('alert')).toHaveTextContent('applies for this session');
  });
  it('removes device listeners on unmount', async () => {
    const result = render(<Harness />); expect(listeners.size).toBe(1);
    result.unmount(); await waitFor(() => expect(listeners.size).toBe(0));
  });
});

describe('before-paint initialization', () => {
  it('applies a saved choice before React starts', () => {
    localStorage.setItem('agentx.theme', 'dark');
    // Execute our own static bootstrap, not user/model-supplied code.
    window.eval(startupScript);
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    expect(document.documentElement).toHaveAttribute('data-theme-preference', 'dark');
  });
  it('resolves System from the OS before React starts', () => {
    dark = true; localStorage.setItem('agentx.theme', 'system'); window.eval(startupScript);
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    expect(document.documentElement).toHaveAttribute('data-theme-preference', 'system');
  });
  it('falls back safely when storage is unavailable', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('Blocked'); });
    window.eval(startupScript);
    expect(document.documentElement).toHaveAttribute('data-theme', 'light');
  });
});
