"""Score what the decision plane concluded against the scenario's answer key.

The Phase-2 harness scores whether the detectors saw the right *symptoms*. This
scores the thing the product is actually judged on: given those symptoms, did
the platform reach the right conclusion and take a defensible decision.

**The answer key is assembled, not stored.** A decision label in a scenario
profile owns no interval of its own - it points at the residual and symptom
labels it is the consequence of, and its window is their union. Those labels
were materialized to *measured* offsets when the capture was recorded, so a
decision expectation inherits real execution timing and can be added to a
profile without re-recording a single capture. A referenced label that the
capture does not carry fails closed rather than being skipped.

**Five metrics, and the one that matters most is a count of mistakes.**

* ``decision_accuracy`` - was the labelled window handled the way it had to be?
  A window that names a class must be surfaced; an ``UNEXPLAINED`` window must
  be surfaced *and* never acted on; an ``EXPECTED_EVENT`` window must never be
  acted on. Note what is deliberately absent: the required handling is stated
  in terms of surfacing and acting, never in terms of a specific rung. The rung
  is operator-owned policy data, and an operator retuning their own ladder must
  not register as a regression of the platform.
* ``decision_reason_accuracy`` - the same, plus the class the platform named
  matching the one the evidence supports. For an ``UNEXPLAINED`` window the
  requirement is that the platform named **nothing**: refusing to name a class
  is the correct answer there, so claiming one is a failure even if the
  handling was right.
* ``attack_recall`` - of the windows where hostility is genuinely recognisable
  from behaviour, how many did the platform call hostile.
* ``origin_accuracy`` - over the windows that state where the trouble started,
  how often the collapse named that service. Windows with no stated origin are
  excluded rather than counted as failures: the answer key does not know, so it
  does not grade.
* ``false_acts`` - every autonomous action taken while no window that names a
  class was live. This is the safety number, it is a count and not a rate, and
  its floor is zero.

Nothing here is read by anything at runtime, and the private labels are opened
only after the whole replay has finished.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from contracts import ACTING_ACTIONS, DecisionAction, Incident, VerdictClass
from decision import IncidentOutcome
from lab.captures import load_private_labels
from lab.scenarios import load_profile
from lab.scenarios.models import NAMED_OUTCOMES, DecisionLabelInterval
from lab.scoring.decisions import DecisionReplay
from lab.scoring.metrics import MetricValue, ratio

# Hostility the evidence can actually support. A window labelled ATTACK is one
# where behaviour deforms, never one where volume merely rose.
HOSTILE_OUTCOMES: frozenset[str] = frozenset({"ATTACK", "COMBINATION"})


@dataclass(frozen=True, slots=True)
class LabelledWindow:
    """One decision expectation, resolved to the interval the capture measured."""

    label_id: str
    expectation: str
    origin_service: str | None
    start_offset_seconds: float
    end_offset_seconds: float
    services: tuple[str, ...] = ()
    missing_refs: tuple[str, ...] = ()

    @property
    def names_a_class(self) -> bool:
        """Whether the answer key claims to know what was happening here."""
        return self.expectation in NAMED_OUTCOMES

    def overlaps(self, start: float, end: float) -> bool:
        """Half-open overlap against an incident's own span."""
        return start < self.end_offset_seconds and end > self.start_offset_seconds


@dataclass(frozen=True, slots=True)
class WindowOutcome:
    """What the platform actually did over one labelled window."""

    window: LabelledWindow
    surfaced: bool
    acted: bool
    named_classes: tuple[str, ...]
    named_origins: tuple[str, ...]
    handled_correctly: bool
    reason_correct: bool
    origin_correct: bool | None

    @property
    def recognised_as_hostile(self) -> bool:
        """Whether the platform called this window hostile at all."""
        return bool(set(self.named_classes) & HOSTILE_OUTCOMES)


@dataclass(frozen=True, slots=True)
class FalseAct:
    """One autonomous action taken where the answer key says nothing was wrong."""

    offset_seconds: float
    incident_id: str
    action: DecisionAction
    target_service: str | None


@dataclass(frozen=True, slots=True)
class DecisionScore:
    """Every decision-level number for one capture."""

    capture_id: str
    scenario_id: str
    seed: int
    seed_purpose: str
    windows: tuple[WindowOutcome, ...]
    false_acts: tuple[FalseAct, ...]
    decision_count: int

    @property
    def decision_accuracy(self) -> MetricValue:
        return ratio(
            sum(1 for outcome in self.windows if outcome.handled_correctly),
            len(self.windows),
        )

    @property
    def decision_reason_accuracy(self) -> MetricValue:
        return ratio(
            sum(1 for outcome in self.windows if outcome.reason_correct),
            len(self.windows),
        )

    @property
    def attack_recall(self) -> MetricValue:
        hostile = [
            outcome for outcome in self.windows if outcome.window.expectation in HOSTILE_OUTCOMES
        ]
        return ratio(sum(1 for outcome in hostile if outcome.recognised_as_hostile), len(hostile))

    @property
    def origin_accuracy(self) -> MetricValue:
        graded = [outcome for outcome in self.windows if outcome.origin_correct is not None]
        return ratio(sum(1 for outcome in graded if outcome.origin_correct), len(graded))

    @property
    def false_act_count(self) -> int:
        return len(self.false_acts)


def resolve_windows(
    labels: Mapping[str, object],
    *,
    decision_labels: Sequence[DecisionLabelInterval],
    residual_service: str,
) -> tuple[LabelledWindow, ...]:
    """Union each expectation's referenced evidence into the window it covers.

    A window also inherits the *services* its evidence was measured on, because
    a window is a question about a particular problem and not merely about a
    stretch of time. Several incidents run at once in a real scenario, and
    without that scoping a long-running fault elsewhere in the mesh would be
    graded as the answer to a question about the frontend.
    """
    measured = _measured_intervals(labels)
    services = _label_services(labels, residual_service=residual_service)
    windows: list[LabelledWindow] = []
    for label in decision_labels:
        present = [ref for ref in label.label_refs if ref in measured]
        missing = tuple(sorted(set(label.label_refs) - set(measured)))
        if not present:
            # A window with no evidence at all in this capture is not a narrower
            # window, it is an unanswerable question. Fail rather than grade it.
            raise ValueError(
                f"{label.label_id} references no label this capture carries "
                f"({', '.join(label.label_refs)}); the capture predates the expectation "
                "entirely and cannot be scored against it"
            )
        # A capture recorded before one of the referenced labels existed still
        # measured the others, so the window is the union of what it does carry.
        # The gap is recorded on the window rather than silently absorbed: a
        # narrower window is a weaker question, and the report says so.
        windows.append(
            LabelledWindow(
                label_id=label.label_id,
                expectation=label.expectation,
                origin_service=label.origin_service,
                start_offset_seconds=min(measured[ref][0] for ref in present),
                end_offset_seconds=max(measured[ref][1] for ref in present),
                services=tuple(sorted({services[ref] for ref in present})),
                missing_refs=missing,
            )
        )
    return tuple(sorted(windows, key=lambda window: (window.start_offset_seconds, window.label_id)))


def score_decisions(
    replay: DecisionReplay,
    *,
    scenario_root: Path,
    labels: Mapping[str, object],
) -> DecisionScore:
    """Grade one capture's decisions against its scenario's answer key."""
    profile = load_profile(scenario_root / f"{replay.scenario_id}.yml")
    windows = resolve_windows(
        labels,
        decision_labels=profile.decision_labels,
        residual_service=profile.telemetry.logical_service,
    )
    combination_allowed = (
        len({window.expectation for window in windows if window.names_a_class}) > 1
    )
    outcomes = tuple(
        _score_window(window, replay=replay, combination_allowed=combination_allowed)
        for window in windows
    )
    return DecisionScore(
        capture_id=replay.capture_id,
        scenario_id=replay.scenario_id,
        seed=replay.seed,
        seed_purpose=replay.seed_purpose,
        windows=outcomes,
        false_acts=_false_acts(replay, windows),
        decision_count=sum(len(tick.outcomes) for tick in replay.ticks),
    )


def _score_window(
    window: LabelledWindow,
    *,
    replay: DecisionReplay,
    combination_allowed: bool,
) -> WindowOutcome:
    surfaced = False
    acted = False
    classes: set[str] = set()
    origins: set[str] = set()
    for offset, outcome in _decisions(replay):
        incident = outcome.incident
        span = (
            _offset(incident.opened_ts, replay.anchor_ts),
            _offset(incident.last_activity_ts, replay.anchor_ts),
        )
        # An incident answers a window when it overlaps it in time AND touches a
        # service the window's evidence was measured on. The time test alone
        # over-attributes: a real scenario runs several incidents at once, and a
        # long-running fault on another service would otherwise be read as the
        # answer to a question it has nothing to do with.
        if not window.overlaps(*span) or offset < window.start_offset_seconds:
            continue
        if not _touches(incident, window):
            continue
        decision = outcome.decision
        if decision.action is not DecisionAction.SUPPRESS:
            surfaced = True
        if decision.action in ACTING_ACTIONS:
            acted = True
        if decision.verdict_class is not None:
            classes.add(decision.verdict_class.value)
        if incident.origin_service is not None:
            origins.add(incident.origin_service)
    handled = _handled_correctly(window, surfaced=surfaced, acted=acted)
    return WindowOutcome(
        window=window,
        surfaced=surfaced,
        acted=acted,
        named_classes=tuple(sorted(classes)),
        named_origins=tuple(sorted(origins)),
        handled_correctly=handled,
        reason_correct=handled
        and _reason_correct(window, classes, combination_allowed=combination_allowed),
        origin_correct=(
            None if window.origin_service is None else window.origin_service in origins
        ),
    )


def _handled_correctly(window: LabelledWindow, *, surfaced: bool, acted: bool) -> bool:
    """The required handling, stated without naming a single policy rung."""
    if window.expectation == "EXPECTED_EVENT":
        # The context explained everything, so there is nothing to remediate.
        # Whether it is mentioned at all is a noise preference, not a mistake.
        return not acted
    if window.expectation == "UNEXPLAINED":
        # Something real is happening that no signature accounts for: it must
        # reach a person, and nothing may be done to a system nobody understands.
        return surfaced and not acted
    # A named class is a problem the platform is expected to recognise. Acting
    # on it is legitimate; staying silent about it is not.
    return surfaced


def _reason_correct(
    window: LabelledWindow,
    classes: set[str],
    *,
    combination_allowed: bool,
) -> bool:
    if window.expectation == "UNEXPLAINED":
        # Refusing to name a class IS the answer here.
        return not classes
    if window.expectation == "EXPECTED_EVENT":
        return not classes or classes == {VerdictClass.EXPECTED_EVENT.value}
    if window.expectation in classes:
        return True
    # COMBINATION asserts that a hostile and a degrading thing are happening at
    # once. That is a true statement about a window in a scenario that really
    # contains both, so it is accepted there - and only there, so a capture with
    # one fault cannot score correct by calling it a combination.
    return combination_allowed and VerdictClass.COMBINATION.value in classes


def _false_acts(
    replay: DecisionReplay,
    windows: Sequence[LabelledWindow],
) -> tuple[FalseAct, ...]:
    """Autonomous actions taken where the answer key says nothing was wrong."""
    faults = tuple(window for window in windows if window.names_a_class)
    found: list[FalseAct] = []
    for offset, outcome in _decisions(replay):
        decision = outcome.decision
        if decision.action not in ACTING_ACTIONS:
            continue
        incident = outcome.incident
        span = (
            _offset(incident.opened_ts, replay.anchor_ts),
            _offset(incident.last_activity_ts, replay.anchor_ts),
        )
        if any(window.overlaps(*span) and _touches(incident, window) for window in faults):
            continue
        found.append(
            FalseAct(
                offset_seconds=offset,
                incident_id=incident.incident_id,
                action=decision.action,
                target_service=decision.target_service,
            )
        )
    return tuple(found)


def _decisions(replay: DecisionReplay) -> Iterable[tuple[float, IncidentOutcome]]:
    for tick in replay.ticks:
        offset = _offset(tick.ts, replay.anchor_ts)
        for outcome in tick.outcomes:
            yield offset, outcome


def _touches(incident: Incident, window: LabelledWindow) -> bool:
    """Whether an incident is about any service this window's evidence names."""
    named = set(incident.services) | set(incident.implicated_services)
    return bool(named & set(window.services))


def _label_services(labels: Mapping[str, object], *, residual_service: str) -> dict[str, str]:
    """The service each evidence label was measured on.

    A residual label names no service of its own - the residual is measured on
    the scenario's own ingress signal - so it takes the profile's logical
    service, which is the service that signal belongs to.
    """
    services: dict[str, str] = {}
    for entry in _entries(labels, "intervals"):
        services[str(entry["label_id"])] = residual_service
    for entry in _entries(labels, "symptom_intervals"):
        services[str(entry["label_id"])] = str(entry["service"])
    return services


def _entries(labels: Mapping[str, object], key: str) -> list[dict[str, Any]]:
    raw = labels.get(key, [])
    if not isinstance(raw, list):
        raise ValueError(f"capture labels field {key} is not a list")
    entries: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ValueError(f"capture label in {key} is not an object")
        entries.append(entry)
    return entries


def _measured_intervals(labels: Mapping[str, object]) -> dict[str, tuple[float, float]]:
    """Every evidence label the capture carries, at the offsets it measured."""
    measured: dict[str, tuple[float, float]] = {}
    for key in ("intervals", "symptom_intervals"):
        for entry in _entries(labels, key):
            measured[str(entry["label_id"])] = (
                float(entry["start_offset_seconds"]),
                float(entry["end_offset_seconds"]),
            )
    return measured


def load_capture_labels(root: Path) -> Mapping[str, object]:
    """Open a capture's private answer key. Call this only after the replay."""
    value = json.loads(load_private_labels(root).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("capture labels root must be an object")
    return value


def _offset(moment: datetime, anchor: datetime) -> float:
    return round((moment - anchor).total_seconds(), 6)
