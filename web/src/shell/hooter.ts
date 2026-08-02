/**
 * The warning hooter: a short, harsh hi-lo siren blast.
 *
 * Synthesised with WebAudio rather than shipped as an audio file — no asset, no
 * request, no new dependency, and it cannot be a 404 at the moment it matters.
 * It is deliberately a siren rather than a soft chime: this fires when the
 * platform has *confirmed* hostile behaviour or a critical fault, and a
 * notification blip is the wrong register for that.
 *
 * Browsers refuse audio that was not started by a user gesture, so the context
 * is primed from the toggle click and only sounds once unlocked.
 */
let audioContext: AudioContext | null = null;

type WindowWithAudio = Window & { webkitAudioContext?: typeof AudioContext };

function contextCtor(): typeof AudioContext | null {
  if (typeof window === 'undefined') return null;
  if (typeof AudioContext !== 'undefined') return AudioContext;
  return (window as WindowWithAudio).webkitAudioContext ?? null;
}

/** Create or resume the context from inside a user gesture. Idempotent. */
export function primeHooter(): void {
  const Ctor = contextCtor();
  if (Ctor === null) return;
  audioContext ??= new Ctor();
  if (audioContext.state === 'suspended') void audioContext.resume();
}

/* A resume() begun outside a gesture stays pending until the browser unlocks
   audio. Past this age the blast is stale noise about something already read. */
const MAX_QUEUED_AGE_MS = 1_500;

function blast(ctx: AudioContext): void {
  const now = ctx.currentTime;

  const gain = ctx.createGain();
  gain.gain.setValueAtTime(0.0001, now);
  // Two urgent pulses with a dip between them, so it reads as a "hoo-er"
  // couplet rather than one continuous tone.
  gain.gain.exponentialRampToValueAtTime(0.18, now + 0.03);
  gain.gain.exponentialRampToValueAtTime(0.07, now + 0.33);
  gain.gain.exponentialRampToValueAtTime(0.18, now + 0.38);
  gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.72);
  gain.connect(ctx.destination);

  const osc = ctx.createOscillator();
  osc.type = 'sawtooth'; // harsh on purpose
  osc.frequency.setValueAtTime(620, now);
  osc.frequency.setValueAtTime(620, now + 0.34);
  osc.frequency.setValueAtTime(466, now + 0.36);
  osc.connect(gain);
  osc.start(now);
  osc.stop(now + 0.74);
}

/** Sound one blast, resuming a suspended context unless the cue has gone stale. */
export function playHooter(): void {
  const Ctor = contextCtor();
  if (Ctor === null) return;
  audioContext ??= new Ctor();
  const ctx = audioContext;

  if (ctx.state === 'running') {
    blast(ctx);
    return;
  }

  const queuedAt = Date.now();
  void ctx
    .resume()
    .then(() => {
      if (ctx.state === 'running' && Date.now() - queuedAt < MAX_QUEUED_AGE_MS) blast(ctx);
    })
    .catch(() => {
      // Audio still locked. Silence is the correct outcome, not an error the
      // operator has to dismiss while an incident is on screen.
    });
}
