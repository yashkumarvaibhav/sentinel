"""The demo launcher's server side: record the intent, never do the work.

Which scenarios exist and how long they take is read from the committed
profiles - but the gateway image has no ``lab/``, so it cannot read them
either. What it has instead is this module's committed catalogue, kept
deliberately small: an id, a sentence, and the modes each id may be fired in.
A mismatch between it and the profiles is caught by a test rather than by a
button that queues work no runner can execute.

The honesty text is not decoration. A replay and a live run look identical
once an incident lands, and a launcher that presented them the same way would
be inviting an audience to read a recording as a live system.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Protocol

from pydantic import ValidationError

from contracts import (
    LabRunFeed,
    LabRunFeedStatus,
    LabRunHonesty,
    LabRunMode,
    LabRunSnapshot,
    LabRunState,
    LabScenarioOption,
    LabScenarioRequest,
)

# How many past runs the launcher shows. A demo surface is a short history.
MAX_LAB_RUNS = 20

REPLAY_HONESTY = LabRunHonesty(
    telemetry="REAL — recorded from the testbed, replayed byte for byte.",
    stimulus="SIMULATED — the faults and the attack were injected when this was recorded.",
    reproducibility="Bit-exact: the same capture always produces the same decisions.",
)

LIVE_HONESTY = LabRunHonesty(
    telemetry="REAL — measured from the testbed while this ran.",
    stimulus="SIMULATED — the faults and the attack are injected into a contained cluster.",
    reproducibility="Statistical only: a live run is judged with tolerance bands, never exactly.",
)


class LabRunStore(Protocol):
    """The durable queue the launcher writes to and reads back."""

    async def enqueue_lab_run(self, run: LabRunSnapshot) -> bool: ...

    async def list_lab_runs(self, *, limit: int) -> tuple[LabRunSnapshot, ...]: ...


class LabRunRefusedError(ValueError):
    """The request is well formed but this deployment will not carry it out."""


# The scenarios a client may fire, and what each one costs. `live_duration_seconds`
# is the sum of the profile's own load phases; the test beside this module keeps
# both halves of that claim honest against `lab/scenarios/*.yml`.
SCENARIOS: tuple[LabScenarioOption, ...] = (
    LabScenarioOption(
        scenario_id="combo_night",
        name="Championship night",
        description=(
            "The north-star shape: an attack, a payment fault, resource pressure, a "
            "measured drop and an emitter silence, all inside an explained match surge."
        ),
        modes=(LabRunMode.REPLAY, LabRunMode.LIVE),
        live_duration_seconds=1004,
    ),
    LabScenarioOption(
        scenario_id="match_night",
        name="Match night, nothing wrong",
        description=(
            "A legitimate surge and nothing else. The platform should explain all of "
            "it and act on none of it."
        ),
        modes=(LabRunMode.LIVE,),
        live_duration_seconds=100,
    ),
    LabScenarioOption(
        scenario_id="attack_day",
        name="Attack, no event",
        description="Hostile traffic on an ordinary day, with no surge to hide inside.",
        modes=(LabRunMode.LIVE,),
        live_duration_seconds=84,
    ),
    LabScenarioOption(
        scenario_id="cascade_night",
        name="Cascading fault",
        description="One failing dependency, and the retry storm it pulls up the graph.",
        modes=(LabRunMode.LIVE,),
        live_duration_seconds=144,
    ),
    LabScenarioOption(
        scenario_id="quiet_day",
        name="Quiet day",
        description="Nothing happening, which is the hardest thing to report correctly.",
        modes=(LabRunMode.LIVE,),
        live_duration_seconds=84,
    ),
)

_BY_ID = {option.scenario_id: option for option in SCENARIOS}


def build_lab_run(request: LabScenarioRequest, *, ts: datetime) -> LabRunSnapshot:
    """Turn a client's intent into the queued row, refusing what is not offered."""
    option = _BY_ID.get(request.scenario_id)
    if option is None:
        raise LabRunRefusedError(f"{request.scenario_id} is not a scenario this deployment offers")
    if request.mode not in option.modes:
        offered = ", ".join(mode.value for mode in option.modes)
        raise LabRunRefusedError(
            f"{option.scenario_id} cannot be fired in {request.mode.value}; it offers {offered}"
        )
    return LabRunSnapshot(
        run_id=f"run-{uuid.uuid4().hex}",
        scenario_id=option.scenario_id,
        mode=request.mode,
        state=LabRunState.QUEUED,
        requested_at=ts,
        detail=(
            f"Queued {option.name} in {request.mode.value.lower()} mode. "
            "The runner claims it; this endpoint never executes one."
        ),
        honesty=REPLAY_HONESTY if request.mode is LabRunMode.REPLAY else LIVE_HONESTY,
    )


def lab_run_feed(
    runs: tuple[LabRunSnapshot, ...],
    *,
    runner_attached: bool,
) -> LabRunFeed:
    """The launcher's whole view, including why it may not be usable."""
    in_flight = [run for run in runs if run.state in {LabRunState.QUEUED, LabRunState.RUNNING}]
    if not runner_attached:
        status = LabRunFeedStatus.UNAVAILABLE
        note = (
            "No lab runner is attached, so a request would queue work nothing will "
            "ever claim. Start one beside the stack with `make demo-runner`."
        )
    elif in_flight:
        status = LabRunFeedStatus.BUSY
        note = (
            f"{in_flight[0].scenario_id} is already running. A second scenario would put "
            "two sets of injected faults into one stretch of telemetry, and neither "
            "run's evidence would mean anything afterwards."
        )
    else:
        status = LabRunFeedStatus.READY
        note = (
            "Pick a scenario. A replay is instant and bit-exact; a live run drives real "
            "load and real injected faults at the testbed, and takes the time it says."
        )
    return LabRunFeed(
        status=status,
        scenarios=SCENARIOS,
        runs=runs,
        runner_attached=runner_attached,
        note=note,
    )


def unavailable_lab_feed(*, detail: str) -> LabRunFeed:
    """The honest empty view when the queue itself cannot be read."""
    return LabRunFeed(
        status=LabRunFeedStatus.UNAVAILABLE,
        scenarios=SCENARIOS,
        runs=(),
        runner_attached=False,
        note=detail,
    )


def parse_lab_request(body: object) -> LabScenarioRequest:
    """Validate a client body strictly, refusing anything it may not say."""
    try:
        return LabScenarioRequest.model_validate(body)
    except ValidationError as error:
        raise LabRunRefusedError(
            f"the request names no scenario this launcher can fire: {error}"
        ) from (error)


def run_payload(run: LabRunSnapshot) -> dict[str, object]:
    """Canonical JSON for a run, for the runner CLI's own output."""
    return dict(json.loads(run.model_dump_json()))
