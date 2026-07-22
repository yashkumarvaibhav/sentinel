# Symptom episodes — anti-flapping lifecycle

Detectors emit a fresh `Symptom` every feature window. Raw symptoms flap: a
borderline condition appears and vanishes tick to tick. `detection/episodes.py`
wraps the recurring symptoms of one `(kind, service, signal)` key into a
durable, hysteresis-guarded **episode** so the decision plane sees one stable
incident precursor instead of a stutter of symptoms.

## Two anti-flap mechanisms

Both come from `detector-params.yml` under `episodes.policies.<KIND>`; nothing is
hard-coded in the engine.

1. **Score deadband (hysteresis).** A tick is a *breach* only when its symptom
   score is `>= breach_score`, and a *clear* only when the score is
   `<= clear_score` (or the symptom is absent). A score strictly between the two
   is a *hold*: it confirms neither and breaks both consecutive runs. Because
   `clear_score < breach_score`, a signal wobbling in the band cannot toggle the
   episode.
2. **Persistence ticks.** An episode opens only after `open_after_ticks`
   consecutive breaches and closes only after `close_after_ticks` consecutive
   clears. A sub-persistence blip can never open one; a brief recovery can never
   close one.

## Lifecycle

```
IDLE ──breach×open_after──▶ ACTIVE ──clear×close_after──▶ CLOSED ──breach──▶ (new episode)
  ▲          │                 │ ▲                            
  └ blip/hold┘        breach ──┘ └── clear (<close_after) = CLEARING; hold/breach returns to ACTIVE
```

- `opened_ts` is the **onset** — the first breach of the opening run, not the
  confirming tick. `confirmed_ts` is the tick that met breach persistence.
- A reopened problem is a **distinct** episode: `episode_id` hashes
  `(kind, service, signal, opened_ts)`, so a new onset yields a new id.
- The episode carries the peak, opening and latest symptom ids plus the peak
  moment's evidence refs — raw `Symptom` records are never mutated.

## Determinism and idempotency

- The machine reads **no wall clock**; every timestamp is the caller-supplied
  event-time tick.
- Ticks for a key must arrive in event-time order. An exact redelivery of the
  most recent tick is an idempotent no-op, so at-least-once bus delivery never
  advances state twice. An out-of-order or same-time-conflicting tick fails
  closed.
- Episodes persist to Postgres (`symptom_episodes`) via `put_episode`, guarded
  by a monotone `revision` so a replayed older state can never overwrite a newer
  one; `get_episode` revalidates the stored `SymptomEpisode` contract on read.

Property tests assert episodes strictly alternate open/close, respect both
persistence bounds in each direction (flapping is impossible), and replay
deterministically. Starter policy values are tuned only on disjoint development
seeds and are frozen/calibrated with the per-symptom scoring at Phase 2.8.
