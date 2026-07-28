import { createContext, useContext } from 'react';

export interface OperatorCredentialValue {
  credential: string | null;
  setCredential: (credential: string) => void;
  clearCredential: () => void;
}

export const OperatorCredentialContext = createContext<OperatorCredentialValue | null>(null);

export function useOperatorCredential(): OperatorCredentialValue {
  const value = useContext(OperatorCredentialContext);
  if (value === null) {
    throw new Error('useOperatorCredential must be rendered inside OperatorCredentialProvider');
  }
  return value;
}
