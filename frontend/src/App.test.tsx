import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import App from './App';
import { apiBaseUrl, getHealth } from './api/client';
const health = { status: 'ok', service: 'agentx-api', database: 'ok', phase: 1 };

describe('foundation', () => {
  it('shows real backend connection result', async () => {
    const fetchMock = vi.fn().mockImplementation(async (url: string) => ({ ok: true, json: async () => url.includes('/health') ? health : { tasks: [], limit: 20, offset: 0 } }));
    vi.stubGlobal('fetch', fetchMock);
    render(<App />);
    expect(await screen.findByText('Backend connected · SQLite ready')).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(call => call[0] === 'http://localhost:8000/api/v1/health')).toBe(true);
  });
  it('reports failure and lets the user retry', async () => {
    let healthCalls = 0;
    vi.stubGlobal('fetch', vi.fn().mockImplementation(async (url: string) => {
      if (url.includes('/health') && healthCalls++ === 0) throw new Error('Network unavailable');
      return { ok: true, json: async () => url.includes('/health') ? health : { tasks: [], limit: 20, offset: 0 } };
    }));
    render(<App />);
    expect(await screen.findByText('Network unavailable')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Check connection' }));
    expect(await screen.findByText('Backend connected · SQLite ready')).toBeInTheDocument();
  });
  it('rejects malformed backend responses', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: 'ok' }) }));
    await expect(getHealth()).rejects.toThrow('invalid health response');
  });
  it('reports non-success HTTP responses', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 503 }));
    await expect(getHealth()).rejects.toThrow('HTTP 503');
  });
  it('validates public API configuration', () => {
    expect(apiBaseUrl('https://api.example.com/')).toBe('https://api.example.com');
    expect(() => apiBaseUrl('not-a-url')).toThrow('VITE_API_BASE_URL');
    expect(() => apiBaseUrl('https://user:password@example.com')).toThrow('VITE_API_BASE_URL');
  });
});
