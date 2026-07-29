import { type FormEvent, type ReactNode, useCallback, useMemo, useState } from 'react';

import { OperatorCredentialContext, useOperatorCredential } from '@/shell/useOperatorCredential';

/**
 * The Phase-6 interim credential exists only in this React tree.
 *
 * A reload forgets it. No URL, localStorage, sessionStorage, cookie, or build
 * artifact receives it. OIDC replaces this entire boundary in Phase 8.1.
 */
export function OperatorCredentialProvider({ children }: { children: ReactNode }) {
  const [credential, setCredentialState] = useState<string | null>(null);
  const setCredential = useCallback((next: string) => {
    const trimmed = next.trim();
    setCredentialState(trimmed.length === 0 ? null : trimmed);
  }, []);
  const clearCredential = useCallback(() => setCredentialState(null), []);
  const value = useMemo(
    () => ({ credential, setCredential, clearCredential }),
    [credential, setCredential, clearCredential],
  );
  return (
    <OperatorCredentialContext.Provider value={value}>
      {children}
    </OperatorCredentialContext.Provider>
  );
}

export function OperatorCredentialPrompt({
  detail,
  title = 'Protected operator surface',
  buttonLabel = 'Unlock action controls',
}: {
  detail: string | undefined;
  title?: string;
  buttonLabel?: string;
}) {
  const { setCredential } = useOperatorCredential();
  const [draft, setDraft] = useState('');

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setCredential(draft);
    setDraft('');
  };

  return (
    <form
      className="border-line bg-sidebar grid max-w-xl gap-3 rounded-lg border p-4"
      onSubmit={submit}
    >
      <div>
        <strong className="text-sm">{title}</strong>
        <p className="text-muted mt-1 text-xs">
          This interim credential is held in memory only and is forgotten on reload.
        </p>
        {detail && <p className="text-bad mt-2 text-xs">{detail}</p>}
      </div>
      <label className="grid gap-1 text-xs">
        <span>Operator credential</span>
        <input
          autoComplete="off"
          className="border-line bg-raised min-h-11 rounded border px-3 font-mono"
          onChange={(event) => setDraft(event.target.value)}
          required
          type="password"
          value={draft}
        />
      </label>
      <button
        className="bg-accent text-accent-contrast min-h-11 rounded px-4 text-sm font-medium"
        type="submit"
      >
        {buttonLabel}
      </button>
    </form>
  );
}
