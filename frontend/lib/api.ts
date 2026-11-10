import type { PairingCreated, PairingStatus } from './types';

const BASE = process.env.NEXT_PUBLIC_API_BASE ?? 'http://localhost:8000';

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail ?? `HTTP ${r.status}`);
  }
  return r.json();
}

async function getJson<T>(path: string): Promise<T> {
  const r = await fetch(`${BASE}${path}`);
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail ?? `HTTP ${r.status}`);
  }
  return r.json();
}

export const api = {
  createPairing: () => postJson<PairingCreated>('/api/pairing/new', {}),
  status: (polling_token: string) =>
    getJson<PairingStatus>(`/api/pairing/status?polling_token=${encodeURIComponent(polling_token)}`),
};
