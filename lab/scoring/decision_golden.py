"""Semantic regression gate over the decision plane's own transcripts.

The Phase-1 golden gate freezes what the *decomposition* concluded on a
development capture. This is its counterpart one plane up: it freezes what the
whole decision loop concluded - the four axis scores, the fused class, the
incidents the storm collapsed into, the origin, the four verification checks
and the policy gate's answer - so a change in any of that shows up as a diff a
person has to look at rather than as a number nobody noticed moving.

Two properties make it a pin rather than a snapshot.

**Both configuration surfaces are named inside the artifact.** A decision
depends on the detector configuration that produced its episodes *and* on the
decision configuration that judged them. Both fingerprints are transcribed, so
editing an axis weight, a rule threshold or a policy floor changes the golden
and has to be regenerated deliberately. That is how the decision plane is
pinned; it is deliberately NOT pinned by folding its configuration into
``SentinelConfig``, which would move the detector fingerprint - and restale
every Phase-1 capture, golden and score report - for reasons that have nothing
to do with detection.

**Only development captures may become goldens.** The same rule the Phase-1
gate enforces: a held-out seed is spent the moment it is scored, and a golden
is regenerated far too often to spend one on.
"""

from __future__ import annotations

import argparse
import difflib
from collections.abc import Sequence
from pathlib import Path

from common.config import load_config
from lab.captures import load_runtime_capture
from lab.scenarios import load_profile
from lab.scoring.decisions import (
    load_decision_configs,
    replay_capture_decisions,
    semantic_transcript,
)

# The two Phase-1 development profiles whose captures the hosted gate already
# has. The symptom-bearing cascade and combo captures join this matrix when
# they are folded into the published bootstrap bundle, which is its own slice.
GOLDEN_PROFILES = ("match_night", "quiet_day")


def load_candidates(*, repo_root: Path, captures_root: Path) -> dict[str, bytes]:
    """Replay one committed development capture per golden profile."""
    roots = tuple(
        sorted(
            path
            for path in captures_root.resolve().iterdir()
            if path.is_dir() and (path / "manifest.json").is_file()
        )
    )
    allowed = {
        profile_name: set(
            load_profile(repo_root / "lab" / "scenarios" / f"{profile_name}.yml").seeds.development
        )
        for profile_name in GOLDEN_PROFILES
    }
    config = load_config(repo_root / "config")
    decisions = load_decision_configs(repo_root / "config")
    candidates: dict[str, bytes] = {}
    for root in roots:
        manifest = load_runtime_capture(root).manifest
        if manifest.seed_purpose != "development":
            raise ValueError(f"golden capture is not a development seed: {manifest.capture_id}")
        if (
            manifest.scenario_id not in allowed
            or manifest.seed not in allowed[manifest.scenario_id]
        ):
            raise ValueError(
                f"golden capture is outside the committed development set: {manifest.capture_id}"
            )
        if manifest.scenario_id in candidates:
            raise ValueError(f"duplicate golden profile: {manifest.scenario_id}")
        replay = replay_capture_decisions(root, config=config, decisions=decisions)
        candidates[manifest.scenario_id] = semantic_transcript(replay)
    missing = sorted(set(GOLDEN_PROFILES) - set(candidates))
    if missing:
        raise ValueError(f"decision golden matrix is incomplete: missing={missing}")
    return candidates


def write_goldens(candidates: dict[str, bytes], goldens_root: Path) -> None:
    """Freeze every replayed transcript as the reviewed answer."""
    goldens_root.mkdir(parents=True, exist_ok=True)
    for profile_name, value in sorted(candidates.items()):
        (goldens_root / f"{profile_name}.json").write_bytes(value)


def check_goldens(candidates: dict[str, bytes], goldens_root: Path) -> None:
    """Fail on any difference, and show it as a diff rather than a mismatch count."""
    expected_names = {f"{profile_name}.json" for profile_name in GOLDEN_PROFILES}
    actual_names = {path.name for path in goldens_root.glob("*.json")}
    if actual_names != expected_names:
        raise ValueError(
            "decision golden files must exactly match the golden profiles: "
            f"missing={sorted(expected_names - actual_names)}, "
            f"extra={sorted(actual_names - expected_names)}"
        )
    for profile_name, actual in sorted(candidates.items()):
        path = goldens_root / f"{profile_name}.json"
        expected = path.read_bytes()
        if actual == expected:
            continue
        diff = "".join(
            difflib.unified_diff(
                expected.decode().splitlines(keepends=True),
                actual.decode().splitlines(keepends=True),
                fromfile=str(path),
                tofile=f"decision-replay:{profile_name}",
                n=3,
            )
        )
        raise ValueError(f"decision golden changed for {profile_name}:\n{diff[:12000]}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lab.scoring.decision_golden")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--captures-root", type=Path, required=True)
    parser.add_argument("--goldens-root", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    candidates = load_candidates(
        repo_root=args.repo_root.resolve(),
        captures_root=args.captures_root,
    )
    if args.write:
        write_goldens(candidates, args.goldens_root.resolve())
        print(f"decision goldens regenerated: {', '.join(sorted(candidates))}")
    else:
        check_goldens(candidates, args.goldens_root.resolve())
        print(f"decision golden PASS: {len(candidates)} decision transcripts unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
