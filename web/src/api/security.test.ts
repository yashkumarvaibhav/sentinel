import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  fetchSecurity,
  parseSecurityResponse,
  SecurityCredentialRequiredError,
} from '@/api/security';
import { SECURITY_READY_FIXTURE } from '@/test/securityFixture';

function copy(): Record<string, unknown> {
  return structuredClone(SECURITY_READY_FIXTURE);
}

afterEach(() => vi.unstubAllGlobals());

describe('security response parser', () => {
  it('accepts a complete evidence-owned security projection', () => {
    const parsed = parseSecurityResponse(SECURITY_READY_FIXTURE);

    expect(parsed.status).toBe('ready');
    expect(parsed.snapshot?.suspect_cohorts[0]?.cohort_id).toBe('public-client-group-17');
    expect(parsed.snapshot?.measurements[1].status).toBe('INSUFFICIENT');
  });

  it('rejects zero-like claims disguised as insufficient evidence', () => {
    const response = copy();
    const snapshot = response.snapshot as Record<string, unknown>;
    const measurements = snapshot.measurements as Array<Record<string, unknown>>;
    measurements[1] = { ...measurements[1], value: 0 };

    expect(() => parseSecurityResponse(response)).toThrow(/insufficient status contradicts/i);
  });

  it('rejects a cohort measurement whose scope was inferred elsewhere', () => {
    const response = copy();
    const snapshot = response.snapshot as Record<string, unknown>;
    const cohorts = snapshot.suspect_cohorts as Array<Record<string, unknown>>;
    const measurements = cohorts[0]!.measurements as Array<Record<string, unknown>>;
    measurements[0] = { ...measurements[0], scope: 'checkout' };

    expect(() => parseSecurityResponse(response)).toThrow(/evidence-owned scope/i);
  });

  it('rejects incoherent response states', () => {
    const response = copy();
    response.status = 'empty';

    expect(() => parseSecurityResponse(response)).toThrow(/unavailable security response/i);
  });
});

it('keeps the interim credential in a request header and maps 401 to a typed lock', async () => {
  const mocked = vi.fn(() => Promise.resolve(new Response(null, { status: 401 })));
  vi.stubGlobal('fetch', mocked);

  await expect(fetchSecurity('operator-secret')).rejects.toBeInstanceOf(
    SecurityCredentialRequiredError,
  );
  expect(mocked).toHaveBeenCalledWith('/api/security', {
    headers: { 'x-sentinel-secret': 'operator-secret' },
  });
});
