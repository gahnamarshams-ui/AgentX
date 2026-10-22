import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

// Isolate tests from the operator's .env and Node's experimental storage global.
beforeEach(() => { vi.stubEnv('VITE_API_BASE_URL', 'http://localhost:8000'); });
