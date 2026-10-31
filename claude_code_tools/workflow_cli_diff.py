"""Pure two-run comparison for the observational workflow CLI.

The comparison is built from exactly two already-loaded, individually
coherent :class:`RunRecord` snapshots; it never re-reads the store itself,
so callers cannot splice a fresh side onto a stale side.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from claude_code_tools.workflow_cli_snapshots import RunRecord, StepRecord

DURATION_DELTA_DECIMALS = 3

SETTING_FIELDS: tuple[tuple[str, str], ...] = (
    ("cwd", "Working directory"),
    ("concurrency", "Concurrency"),
    ("maxAgentInvocations", "Invocation limit"),
)


@dataclass(frozen=True)
class RunSideSummary:
    """Identifying header data for one compared run."""

    run_id: str
    abbreviated_id: str
    workflow_name: str
    status: str
    state_error: str | None


@dataclass(frozen=True)
class SettingComparison:
    """One run-level launch setting paired across both runs."""

    field: str
    label: str
    a: object
    b: object
    changed: bool


@dataclass(frozen=True)
class StepSideSummary:
    """One step that exists on only one side of the comparison."""

    id: str
    label: str
    status: str
    attempt: int | None
    duration_seconds: float | None
    error: str | None


@dataclass(frozen=True)
class StepComparison:
    """Field-by-field comparison of one step present on both sides."""

    id: str
    label_a: str
    label_b: str
    input_known: bool
    input_changed: bool
    status_a: str
    status_b: str
    status_changed: bool
    attempt_a: int | None
    attempt_b: int | None
    attempt_changed: bool
    duration_a: float | None
    duration_b: float | None
    duration_delta_seconds: float | None
    error_a: str | None
    error_b: str | None
    error_changed: bool

    @property
    def changed(self) -> bool:
        """Return whether any compared outcome (ignoring timing) differs."""
        return (
            self.input_changed
            or self.status_changed
            or self.attempt_changed
            or self.error_changed
        )


@dataclass(frozen=True)
class UnchangedSteps:
    """Paired steps whose inputs and outcomes are identical."""

    ids: tuple[str, ...]
    duration_delta_seconds: float | None

    @property
    def count(self) -> int:
        """Return the number of grouped steps."""
        return len(self.ids)


@dataclass(frozen=True)
class RunDiff:
    """Complete typed comparison of two coherent run snapshots."""

    a: RunSideSummary
    b: RunSideSummary
    settings: tuple[SettingComparison, ...]
    only_a: tuple[StepSideSummary, ...]
    only_b: tuple[StepSideSummary, ...]
    changed_steps: tuple[StepComparison, ...]
    unchanged: UnchangedSteps

    @property
    def identical(self) -> bool:
        """Return whether settings and every step match across both runs."""
        return (
            not any(setting.changed for setting in self.settings)
            and not self.only_a
            and not self.only_b
            and not self.changed_steps
        )


def _side_summary(record: RunRecord) -> RunSideSummary:
    return RunSideSummary(
        run_id=record.run_id,
        abbreviated_id=record.abbreviated_id,
        workflow_name=record.workflow_name,
        status=record.status,
        state_error=record.state_error,
    )


def _setting_value(record: RunRecord, field: str) -> object:
    if field == "cwd":
        return record.cwd
    if field == "concurrency":
        return record.state.concurrency
    return record.state.max_agent_invocations


def _diff_settings(a: RunRecord, b: RunRecord) -> tuple[SettingComparison, ...]:
    comparisons: list[SettingComparison] = []
    for field, label in SETTING_FIELDS:
        value_a = _setting_value(a, field)
        value_b = _setting_value(b, field)
        comparisons.append(
            SettingComparison(
                field=field,
                label=label,
                a=value_a,
                b=value_b,
                changed=value_a != value_b,
            )
        )
    return tuple(comparisons)


def _step_side_summary(
    step: StepRecord,
    now: datetime,
) -> StepSideSummary:
    return StepSideSummary(
        id=step.id,
        label=step.label,
        status=step.status,
        attempt=step.attempt,
        duration_seconds=step.duration_seconds(now),
        error=step.error,
    )


def _rounded_delta(after: float | None, before: float | None) -> float | None:
    if after is None or before is None:
        return None
    delta = round(after - before, DURATION_DELTA_DECIMALS)
    return 0.0 if delta == 0 else delta


def _diff_step(
    step_a: StepRecord,
    step_b: StepRecord,
    now_a: datetime,
    now_b: datetime,
) -> StepComparison:
    duration_a = step_a.duration_seconds(now_a)
    duration_b = step_b.duration_seconds(now_b)
    input_known = step_a.fingerprint is not None and step_b.fingerprint is not None
    input_changed = bool(input_known and step_a.fingerprint != step_b.fingerprint)
    error_a = step_a.error
    error_b = step_b.error
    return StepComparison(
        id=step_a.id,
        label_a=step_a.label,
        label_b=step_b.label,
        input_known=input_known,
        input_changed=input_changed,
        status_a=step_a.status,
        status_b=step_b.status,
        status_changed=step_a.status != step_b.status,
        attempt_a=step_a.attempt,
        attempt_b=step_b.attempt,
        attempt_changed=step_a.attempt != step_b.attempt,
        duration_a=duration_a,
        duration_b=duration_b,
        duration_delta_seconds=_rounded_delta(duration_b, duration_a),
        error_a=error_a,
        error_b=error_b,
        error_changed=(error_a or None) != (error_b or None),
    )


def diff_runs(
    a: RunRecord,
    b: RunRecord,
    *,
    now_a: datetime,
    now_b: datetime,
) -> RunDiff:
    """Compare two coherent run snapshots without touching the store.

    Args:
        a: First run snapshot, loaded once for this comparison.
        b: Second run snapshot, loaded once for this comparison.
        now_a: Observation clock for side A's running-step durations.
        now_b: Observation clock for side B's running-step durations.

    Returns:
        A typed :class:`RunDiff` grouping settings, single-side steps,
        changed steps, and unchanged steps.
    """
    steps_a = {step.id: step for step in a.steps}
    steps_b = {step.id: step for step in b.steps}
    ids_a = set(steps_a)
    ids_b = set(steps_b)
    only_a = tuple(
        _step_side_summary(steps_a[step_id], now_a)
        for step_id in steps_a
        if step_id not in ids_b
    )
    only_b = tuple(
        _step_side_summary(steps_b[step_id], now_b)
        for step_id in steps_b
        if step_id not in ids_a
    )
    paired = sorted(ids_a & ids_b)
    comparisons = [
        _diff_step(steps_a[step_id], steps_b[step_id], now_a, now_b)
        for step_id in paired
    ]
    changed_steps = tuple(step for step in comparisons if step.changed)
    unchanged_ids = tuple(step.id for step in comparisons if not step.changed)
    unchanged_deltas = [
        step.duration_delta_seconds
        for step in comparisons
        if not step.changed and step.duration_delta_seconds is not None
    ]
    unchanged_duration_delta = (
        round(sum(unchanged_deltas), DURATION_DELTA_DECIMALS)
        if unchanged_deltas
        else None
    )
    if unchanged_duration_delta == 0:
        unchanged_duration_delta = 0.0
    return RunDiff(
        a=_side_summary(a),
        b=_side_summary(b),
        settings=_diff_settings(a, b),
        only_a=only_a,
        only_b=only_b,
        changed_steps=changed_steps,
        unchanged=UnchangedSteps(
            ids=unchanged_ids,
            duration_delta_seconds=unchanged_duration_delta,
        ),
    )


def state_signature(record: RunRecord) -> tuple[object, object]:
    """Return an equality token capturing every observable state change.

    Watch frames compare these tokens to report which side changed without
    retaining any other data from a previous observation.
    """
    return (record.state, record.state_error)


def changed_sides(
    previous: tuple[object | None, object | None] | None,
    current: tuple[object | None, object | None],
) -> tuple[int, ...]:
    """Return the indexes of sides that changed between coherent frames."""
    if previous is None:
        return ()
    return tuple(
        index
        for index in (0, 1)
        if previous[index] is not None and previous[index] != current[index]
    )
