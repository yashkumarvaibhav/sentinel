import { useCallback, useSyncExternalStore } from 'react';

const STORAGE_KEY = 'sentinel.sound';
const CHANGE_EVENT = 'sentinel:sound-change';
let volatileEnabled = false;

function read(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === 'on';
  } catch {
    return volatileEnabled;
  }
}

function subscribe(listener: () => void): () => void {
  window.addEventListener('storage', listener);
  window.addEventListener(CHANGE_EVENT, listener);
  return () => {
    window.removeEventListener('storage', listener);
    window.removeEventListener(CHANGE_EVENT, listener);
  };
}

/**
 * Whether the warning hooter may sound.
 *
 * **Off by default, and that is not timidity.** A browser will not play audio
 * before a user gesture, so an alarm that defaulted on would simply be silent
 * on first load — and an operator who believes an alarm is armed when it is not
 * is worse off than one who knows it is off. Enabling it is the gesture that
 * unlocks audio, which is why the toggle sounds one blast as proof.
 */
export function useSound(): { enabled: boolean; setEnabled: (next: boolean) => void } {
  const enabled = useSyncExternalStore<boolean>(subscribe, read, () => false);

  const setEnabled = useCallback((next: boolean) => {
    volatileEnabled = next;
    try {
      window.localStorage.setItem(STORAGE_KEY, next ? 'on' : 'off');
    } catch {
      // A forgetful preference beats a broken page.
    }
    window.dispatchEvent(new Event(CHANGE_EVENT));
  }, []);

  return { enabled, setEnabled };
}
