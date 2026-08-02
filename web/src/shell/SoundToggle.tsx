import { playHooter, primeHooter } from '@/shell/hooter';
import { useSound } from '@/shell/useSound';
import { Volume2, VolumeX } from '@/ui/icons';

/**
 * Arms the warning hooter.
 *
 * Enabling counts as the user gesture that unlocks WebAudio, so the context is
 * primed here and one blast sounds immediately — that preview is not a
 * flourish, it is the only honest proof that audio actually works in this
 * browser before an operator starts relying on it.
 */
export function SoundToggle() {
  const { enabled, setEnabled } = useSound();

  function toggle() {
    const next = !enabled;
    if (next) {
      primeHooter();
      playHooter();
    }
    setEnabled(next);
  }

  const label = enabled ? 'Mute the warning alarm' : 'Enable the warning alarm';

  return (
    <button
      type="button"
      onClick={toggle}
      aria-pressed={enabled}
      aria-label={label}
      title={label}
      className="border-line text-ink hover:bg-hover flex size-11 shrink-0 items-center justify-center rounded-md border transition-colors"
    >
      {enabled ? (
        <Volume2 aria-hidden="true" className="size-[18px]" strokeWidth={2} />
      ) : (
        <VolumeX aria-hidden="true" className="size-[18px]" strokeWidth={2} />
      )}
    </button>
  );
}
