import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ThemeStudio from './ThemeStudio';
import { useTheme } from './theme';
import startupScript from '../public/theme-init.js?raw';

function Harness() { return <ThemeStudio {...useTheme()} />; }
beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: false })));
});
afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

describe('monochrome appearance', () => {
  it('offers only Light, Dark and System with no color options', () => {
    render(<Harness />);
    expect(screen.getAllByRole('radio')).toHaveLength(3);
    for (const name of ['Light', 'Dark', 'System']) expect(screen.getByRole('radio', { name })).toBeInTheDocument();
    expect(document.querySelector('input[type="color"]')).toBeNull();
    expect(screen.queryByText('Accent color')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Custom hex color')).not.toBeInTheDocument();
  });
  it('discards a retired custom accent while preserving the saved mode', () => {
    localStorage.setItem('agentx.theme', 'dark'); localStorage.setItem('agentx.accent', '#ec4899');
    render(<Harness />);
    expect(screen.getByRole('radio', { name: 'Dark' })).toBeChecked();
    expect(localStorage.getItem('agentx.accent')).toBeNull();
    act(() => window.dispatchEvent(new StorageEvent('storage', { key: 'agentx.accent', newValue: '#3b82f6' })));
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    expect(document.documentElement.style.getPropertyValue('--accent')).toBe('');
  });
  it('persists mode changes across remount with no accent setting', () => {
    const first = render(<Harness />);
    fireEvent.click(screen.getByRole('radio', { name: 'Light' }));
    expect(localStorage.getItem('agentx.theme')).toBe('light');
    first.unmount(); render(<Harness />);
    expect(screen.getByRole('radio', { name: 'Light' })).toBeChecked();
    fireEvent.click(screen.getByRole('radio', { name: 'Dark' }));
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark');
    expect(localStorage.getItem('agentx.accent')).toBeNull();
  });
  it('resets to System and reports the restored appearance', () => {
    localStorage.setItem('agentx.theme', 'dark'); render(<Harness />);
    fireEvent.click(screen.getByRole('button', { name: 'Reset to default' }));
    expect(screen.getByRole('radio', { name: 'System' })).toBeChecked();
    expect(localStorage.getItem('agentx.theme')).toBe('system');
    expect(screen.getByText('Restored System appearance.')).toBeInTheDocument();
  });
  it('initializes the saved appearance without loading retired color preferences', () => {
    localStorage.setItem('agentx.theme', 'light'); localStorage.setItem('agentx.accent', '#ff0000');
    window.eval(startupScript);
    expect(document.documentElement).toHaveAttribute('data-theme', 'light');
    expect(document.documentElement.style.getPropertyValue('--button')).toBe('');
  });
});
