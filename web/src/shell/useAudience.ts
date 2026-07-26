import { useCallback, useState } from 'react';

export type Audience = 'exec' | 'technical';

const STORAGE_KEY = 'sentinel.audience';

/**
 * Which audience the shell is speaking to.
 *
 * The rule this exists to enforce is `UIUX_SPEC` §1.4: Exec and Technical are
 * two renderings of **the same data**, never a separate mocked view. So this is
 * deliberately only a preference — no component may fetch differently because
 * of it, and any component that did would be able to show an executive a
 * number the technical view would contradict.
 *
 * Technical is the default because the platform's own claim is that every
 * verdict is legible from its evidence; hiding the evidence by default would
 * argue against the product.
 */
export function useAudience(): {
  audience: Audience;
  setAudience: (next: Audience) => void;
  toggle: () => void;
} {
  const [audience, setAudienceState] = useState<Audience>(() => {
    try {
      return window.localStorage.getItem(STORAGE_KEY) === 'exec' ? 'exec' : 'technical';
    } catch {
      return 'technical';
    }
  });

  const setAudience = useCallback((next: Audience) => {
    setAudienceState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // A forgetful toggle beats a broken page.
    }
  }, []);

  const toggle = useCallback(() => {
    setAudience(audience === 'exec' ? 'technical' : 'exec');
  }, [audience, setAudience]);

  return { audience, setAudience, toggle };
}
