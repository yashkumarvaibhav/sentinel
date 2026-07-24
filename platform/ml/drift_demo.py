"""Run the injected-drift demonstration and report per-stream detection.

Feeds each configured stream a stable regime then a shifted one and prints whether
the detector stayed quiet through the stable half and flagged the shift — the
injected-drift test as a runnable artifact.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from ml.config import load_drift_params
from ml.drift import run_drift_demo


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ml.drift_demo")
    parser.add_argument("--repo-root", type=Path, required=True)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    params = load_drift_params(repo_root / "config" / "ml-drift.yml")
    print(f"drift monitoring ({params.detector}), fingerprint {params.fingerprint}", flush=True)
    for report in run_drift_demo(params):
        print(
            f"  {report.stream}: stable_false_alarms={report.stable_false_alarms} "
            f"drift_detected={report.drift_detected} first_drift_index={report.first_drift_index}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
