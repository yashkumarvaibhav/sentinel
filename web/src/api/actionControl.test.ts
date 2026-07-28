import { expect, it } from 'vitest';

import { parseActionControlResponse } from '@/api/actionControl';
import { ACTION_RESPONSE } from '@/test/actionControlFixture';

it('strictly parses the complete evidence-owned action control', () => {
  const parsed = parseActionControlResponse(ACTION_RESPONSE);

  expect(parsed.control?.plan.target_ref).toBe('route/login');
  expect(parsed.control?.guard_results).toHaveLength(2);
});

it('rejects an unknown field and a rung that disagrees with its plan', () => {
  expect(() =>
    parseActionControlResponse({
      ...ACTION_RESPONSE,
      attacker_target: 'deployment/payment',
    }),
  ).toThrow(/unknown field/i);
  expect(() =>
    parseActionControlResponse({
      ...ACTION_RESPONSE,
      control: {
        ...ACTION_RESPONSE.control,
        rung: { ...ACTION_RESPONSE.control.rung, actuator: 'KUBERNETES' },
      },
    }),
  ).toThrow(/rung.*plan/i);
});
