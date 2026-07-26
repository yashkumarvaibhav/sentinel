import { afterEach, expect, it, vi } from 'vitest';

import { fetchIncidentDetail, parseIncidentDetailResponse } from '@/api/incidentDetail';

afterEach(() => vi.unstubAllGlobals());

it('keeps a missing proof distinct from unavailable storage', async () => {
  const missing = parseIncidentDetailResponse({
    status: 'not_found',
    detail: null,
    message: 'No durable proof exists for this incident.',
  });
  expect(missing.status).toBe('not_found');

  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            status: 'not_found',
            detail: null,
            message: 'No durable proof exists for this incident.',
          }),
          { status: 404 },
        ),
      ),
    ),
  );
  await expect(fetchIncidentDetail('incident/missing')).resolves.toEqual(missing);
  expect(fetch).toHaveBeenCalledWith('/api/incidents/incident%2Fmissing', {});
});

it('rejects unknown wire fields instead of trusting a type assertion', () => {
  expect(() =>
    parseIncidentDetailResponse({
      status: 'not_found',
      detail: null,
      message: 'Missing.',
      invented: true,
    }),
  ).toThrow(/unknown field/i);
});
