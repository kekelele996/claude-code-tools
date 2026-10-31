"""Responsive Rich renderables for the durable workflow CLI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from claude_code_tools.workflow_cli_diff import (
    RunDiff,
    RunSideSummary,
    SettingComparison,
    StepComparison,
    StepSideSummary,
)
from claude_code_tools.workflow_cli_identity_policy import (
    colliding_abbreviations,
    display_run_id,
)
from claude_code_tools.workflow_cli_formatting import (
    bounded_error,
    bounded_text,
    format_age,
    format_duration,
    format_time,
    sanitize,
)
from claude_code_tools.workflow_cli_snapshots import (
    CallbackRecord,
    RunRecord,
    StepRecord,
)

STATUS_STYLES = {
    "canceled": "bright_black",
    "canceling": "yellow",
    "completed": "green",
    "failed": "red",
    "malformed": "red",
    "orphaned": "magenta",
    "paused": "yellow",
    "pausing": "yellow",
    "running": "cyan",
    "stale": "magenta",
    "starting": "cyan",
    "unknown": "bright_black",
    "unverifiable": "yellow",
}
CALLBACK_STYLES = {
    "armed": "cyan",
    "delivered": "green",
    "failed": "red",
    "invalid": "red",
    "none": "bright_black",
    "orphaned": "magenta",
    "sending": "cyan",
    "stale": "magenta",
    "unknown": "yellow",
    "unverifiable": "yellow",
}
MAX_LIST_NAME_CHARS = 80
MAX_DETAIL_CHARS = 500
MAX_STEP_DETAIL_CHARS = 200
MAX_SHOW_STEPS = 50
NARROW_DETAIL_WIDTH = 64
ULTRA_NARROW_WIDTH = 4
PLAIN_SHOW_MAX_WIDTH = 7
WIDE_LIST_WIDTH = 140


@dataclass(frozen=True)
class RunRowView:
    """One bounded, terminal-safe run row shared by every list layout."""

    run_id: str
    abbreviated_id: str
    abbreviation_ambiguous: bool
    workflow_name: str
    project_name: str
    status: str
    agents: str
    agents_with_workers: str
    active_workers: str
    duration: str
    updated: str
    callback_status: str
    activity: str

    @classmethod
    def from_record(cls, run: RunRecord, now: datetime) -> RunRowView:
        """Normalize and bound persisted fields once for list rendering."""
        progress = run.progress
        finished = progress["completed"] + progress["failed"] + progress["canceled"]
        agents = f"{finished}/{progress['total']}"
        active_workers = run.active_workers
        worker_label = "worker" if active_workers == 1 else "workers"
        return cls(
            run_id=sanitize(run.run_id),
            abbreviated_id=sanitize(run.abbreviated_id),
            abbreviation_ambiguous=run.abbreviation_ambiguous,
            workflow_name=bounded_text(
                run.workflow_name,
                maximum=MAX_LIST_NAME_CHARS,
                full=False,
            )[0],
            project_name=bounded_text(
                run.project_name,
                maximum=MAX_LIST_NAME_CHARS,
                full=False,
            )[0],
            status=sanitize(run.status),
            agents=agents,
            agents_with_workers=f"{agents} · {active_workers} {worker_label}",
            active_workers=str(active_workers),
            duration=format_duration(run.duration_seconds(now)),
            updated=format_age(run.updated_at, now),
            callback_status=sanitize(run.callback_status),
            activity=bounded_text(
                run.activity(),
                maximum=200,
                full=False,
            )[0],
        )


def _status_renderable(view: RunRowView, live: bool) -> RenderableType:
    """Build a safe styled status value for one run."""
    style = STATUS_STYLES.get(view.status, "bright_black")
    text = Text(view.status, style=style)
    if live and view.status in {"running", "starting"}:
        return Spinner("dots", text=text, style="cyan")
    return text


def _compact_run_details(
    view: RunRowView,
    *,
    activity_width: int,
    live: bool,
) -> Group:
    """Build compact state, timing, callback, and activity lines."""
    status_line = Table.grid(padding=(0, 1))
    status_line.add_column(no_wrap=True)
    status_line.add_column(overflow="ellipsis")
    status_line.add_row(
        _status_renderable(view, live),
        view.agents_with_workers,
    )
    callback = Text()
    callback.append(
        view.callback_status,
        style=CALLBACK_STYLES.get(view.callback_status, "bright_black"),
    )
    activity = bounded_text(
        view.activity,
        maximum=activity_width,
        full=False,
    )[0]
    callback.append(f" · {activity}")
    return Group(
        status_line,
        Text(f"{view.duration} · {view.updated}"),
        callback,
    )


def build_runs_table(
    runs: list[RunRecord],
    *,
    width: int,
    live: bool,
    now: datetime,
) -> RenderableType:
    """Build a workflow table appropriate for the available width."""
    views = [RunRowView.from_record(run, now) for run in runs]
    return _build_runs_table_from_views(views, width=width, live=live)


def _build_runs_table_from_views(
    views: list[RunRowView],
    *,
    width: int,
    live: bool,
) -> RenderableType:
    """Arrange already-normalized rows for the available width."""
    collisions = colliding_abbreviations(
        (view.run_id, view.abbreviated_id) for view in views
    )
    display_ids = [
        display_run_id(
            view.run_id,
            view.abbreviated_id,
            ambiguous=(
                view.abbreviation_ambiguous or view.abbreviated_id in collisions
            ),
        )
        for view in views
    ]
    has_collisions = any(
        display_id != view.abbreviated_id
        for view, display_id in zip(views, display_ids, strict=True)
    )
    if width <= ULTRA_NARROW_WIDTH:
        records: list[RenderableType] = []
        for view, display_id in zip(views, display_ids, strict=True):
            records.extend(
                [
                    Text(view.workflow_name, style="bold"),
                    Text(view.project_name, style="cyan"),
                    Text(display_id, style="bright_black"),
                    _compact_run_details(
                        view,
                        activity_width=max(1, width),
                        live=live,
                    ),
                ]
            )
        return Group(*records)
    table = Table(
        box=box.SIMPLE_HEAD,
        collapse_padding=True,
        expand=True,
        pad_edge=False,
        show_edge=False,
    )
    if width < 40 or has_collisions:
        table.add_column("Workflow runs", overflow="fold")
        for view, display_id in zip(views, display_ids, strict=True):
            identity = Text()
            identity.append(view.workflow_name, style="bold")
            identity.append(
                f"\n{view.project_name}",
                style="cyan",
            )
            identity.append(
                f"\n{display_id}",
                style="bright_black",
            )
            table.add_row(
                Group(
                    identity,
                    _compact_run_details(
                        view,
                        activity_width=max(12, width - 7),
                        live=live,
                    ),
                )
            )
        return table
    if width < WIDE_LIST_WIDTH:
        table.add_column(
            "Workflow / project / run",
            width=30,
            overflow="ellipsis",
        )
        table.add_column("State / activity", min_width=20, ratio=3)
        for view, display_id in zip(views, display_ids, strict=True):
            identity = Text()
            identity.append(view.workflow_name, style="bold")
            identity.append(
                f"\n{view.project_name}",
                style="cyan",
            )
            identity.append(
                f"\n{display_id}",
                style="bright_black",
            )
            table.add_row(
                identity,
                _compact_run_details(
                    view,
                    activity_width=max(12, width * 3 // 5 - 7),
                    live=live,
                ),
            )
        return table

    table.add_column(
        "Workflow / Project",
        ratio=4,
        max_width=48,
        overflow="ellipsis",
    )
    table.add_column("Run", width=17, no_wrap=True)
    table.add_column("Status", width=12, no_wrap=True)
    table.add_column("Agents", width=8, no_wrap=True)
    table.add_column("Active", width=6, justify="right", no_wrap=True)
    table.add_column("Time", width=9, no_wrap=True)
    table.add_column("Updated", width=12, no_wrap=True)
    table.add_column("Callback", width=10, no_wrap=True)
    table.add_column("Error / activity", ratio=3, overflow="ellipsis")
    for view, display_id in zip(views, display_ids, strict=True):
        identity = Text(view.workflow_name, style="bold")
        identity.append(f"\n{view.project_name}", style="cyan")
        row: list[RenderableType] = [
            identity,
            Text(display_id),
            _status_renderable(view, live),
            Text(view.agents),
            Text(view.active_workers),
            Text(view.duration),
            Text(view.updated),
            Text(
                view.callback_status,
                style=CALLBACK_STYLES.get(
                    view.callback_status,
                    "bright_black",
                ),
            ),
            Text(view.activity),
        ]
        table.add_row(*row)
    return table


def _detail_grid(values: tuple[tuple[str, object], ...], width: int) -> Table:
    """Build a stacked narrow grid or a two-column wide grid."""
    grid = Table.grid(expand=True, padding=(0, 2))
    if width < NARROW_DETAIL_WIDTH:
        grid.add_column(overflow="fold")
        for label, value in values:
            grid.add_row(Text(label, style="bold"))
            grid.add_row(Text(sanitize(value), overflow="fold"))
        return grid
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(overflow="fold", ratio=1)
    for label, value in values:
        grid.add_row(label, Text(sanitize(value), overflow="fold"))
    return grid


def _summary_grid(run: RunRecord, now: datetime, width: int, full: bool) -> Table:
    """Build the responsive run-summary grid."""
    progress = run.progress
    finished = progress["completed"] + progress["failed"] + progress["canceled"]
    activity, _ = bounded_text(
        run.activity(),
        maximum=MAX_DETAIL_CHARS,
        full=full,
    )
    values = (
        ("Run ID", run.run_id),
        ("Workflow", run.workflow_name),
        ("Project", run.project_name),
        ("Working directory", run.cwd or "—"),
        ("Workflow path", run.workflow_path or "—"),
        ("Status", run.status),
        ("Recorded status", run.recorded_status),
        ("Agent progress", f"{finished}/{progress['total']}"),
        ("Active workers", str(run.active_workers)),
        ("Created", format_time(run.created_at)),
        ("Started", format_time(run.started_at)),
        ("Completed", format_time(run.completed_at)),
        ("Last state update", format_time(run.updated_at)),
        ("Elapsed / total", format_duration(run.duration_seconds(now))),
        ("Callback", run.callback_status),
        ("Activity", activity),
    )
    bounded = tuple(
        (
            label,
            bounded_text(value, maximum=MAX_DETAIL_CHARS, full=full)[0],
        )
        for label, value in values
    )
    return _detail_grid(bounded, width)


CALLBACK_DETAIL_FIELDS = (
    ("status", "Status"),
    ("attempts", "Attempts"),
    ("createdAt", "Created"),
    ("updatedAt", "Updated"),
    ("lastAttemptAt", "Last attempt"),
    ("deliveredAt", "Delivered"),
    ("deadlineAt", "Deadline"),
    ("terminalStatus", "Terminal status"),
    ("terminalCompletedAt", "Terminal completed"),
    ("clientUserMessageId", "Client message ID"),
    ("endpoint", "Endpoint"),
    ("threadId", "Thread"),
    ("timeoutMs", "Timeout (ms)"),
    ("turnId", "Turn"),
    ("notifierPid", "Notifier PID"),
    ("notifierStartedAt", "Notifier started"),
    ("notifierProcessStatus", "Notifier process"),
    ("error", "Error"),
)
CALLBACK_ATTRIBUTES = {
    "attempts": "attempts",
    "createdAt": "created_at",
    "updatedAt": "updated_at",
    "lastAttemptAt": "last_attempt_at",
    "deliveredAt": "delivered_at",
    "deadlineAt": "deadline_at",
    "terminalStatus": "terminal_status",
    "terminalCompletedAt": "terminal_completed_at",
    "clientUserMessageId": "client_user_message_id",
    "endpoint": "endpoint",
    "threadId": "thread_id",
    "timeoutMs": "timeout_ms",
    "turnId": "turn_id",
    "notifierPid": "notifier_pid",
    "notifierStartedAt": "notifier_started_at",
    "error": "error",
}


@dataclass(frozen=True)
class CallbackDetailView:
    """Bounded callback fields for terminal rendering."""

    values: tuple[tuple[str, str], ...]

    @classmethod
    def from_record(cls, run: RunRecord, *, full: bool) -> CallbackDetailView:
        """Build callback details from typed domain data, independent of JSON."""
        callback = run.callback
        if callback is None and run.callback_error is None:
            return cls((("Status", "none"),))
        values = tuple(
            (
                label,
                bounded_text(
                    _callback_field(run, callback, key),
                    maximum=MAX_DETAIL_CHARS,
                    full=full,
                )[0],
            )
            for key, label in CALLBACK_DETAIL_FIELDS
        )
        return cls(values)


def _callback_field(
    run: RunRecord,
    callback: CallbackRecord | None,
    key: str,
) -> str:
    """Render one typed callback field before applying display bounds."""
    if key == "status":
        return "invalid" if run.callback_error else run.callback_status
    if run.callback_error:
        return run.callback_error if key == "error" else "—"
    if key == "notifierProcessStatus":
        value: object = run.callback_process_state
    elif callback is None:
        value = None
    else:
        value = getattr(callback, CALLBACK_ATTRIBUTES[key])
    if key.endswith("At") and isinstance(value, str):
        return format_time(value)
    return "—" if value is None else str(value)


def _callback_grid(run: RunRecord, width: int, full: bool) -> Table:
    """Build the responsive callback detail grid."""
    return _detail_grid(CallbackDetailView.from_record(run, full=full).values, width)


@dataclass(frozen=True)
class StepDetailView:
    """One normalized, bounded agent-step detail row."""

    label: str
    step_id: str
    status: str
    attempt: str
    duration: str
    worker: str
    has_worker: bool
    thread_id: str
    error: str

    @classmethod
    def from_record(
        cls,
        step: StepRecord,
        now: datetime,
        *,
        full: bool,
    ) -> StepDetailView:
        """Normalize a step once before selecting a responsive layout."""

        def text(value: object) -> str:
            return bounded_text(
                value,
                maximum=MAX_STEP_DETAIL_CHARS,
                full=full,
            )[0]

        return cls(
            label=text(step.label),
            step_id=text(step.id),
            status=sanitize(step.status),
            attempt=text("—" if step.attempt is None else step.attempt),
            duration=format_duration(step.duration_seconds(now)),
            worker=text("—" if step.worker_pid is None else step.worker_pid),
            has_worker=step.worker_pid is not None,
            thread_id=text(step.thread_id or ""),
            error=text(step.error or ""),
        )


def _vertical_steps(
    steps: tuple[StepDetailView, ...],
) -> Group:
    """Build vertical step records for a narrow terminal."""
    records: list[RenderableType] = []
    for index, step in enumerate(steps, start=1):
        values = (
            ("ID", step.step_id),
            ("Status", step.status),
            ("Attempt", step.attempt),
            ("Time", step.duration),
            ("Worker", step.worker),
            ("Thread", step.thread_id or "—"),
            ("Error", step.error or "—"),
        )
        records.append(
            Group(
                Text(f"{index}. {step.label}", style="bold"),
                _detail_grid(values, 0),
            )
        )
    return Group(*records)


def _step_table(
    steps: tuple[StepDetailView, ...],
    width: int,
) -> RenderableType:
    """Build responsive, safe step details."""
    has_long_number = any(
        len(value) > MAX_STEP_DETAIL_CHARS
        for step in steps
        for value in (step.attempt, step.worker)
    )
    if width < NARROW_DETAIL_WIDTH or has_long_number:
        return _vertical_steps(steps)
    table = Table(box=box.SIMPLE_HEAD, expand=True, show_edge=False)
    table.add_column("Step / ID", ratio=2, overflow="fold")
    table.add_column("Status", width=10, no_wrap=True)
    table.add_column("Try", width=3, justify="right", no_wrap=True)
    table.add_column("Time", width=8, no_wrap=True)
    if width >= 96:
        table.add_column("Worker", width=8, no_wrap=True)
        table.add_column("Thread / error", ratio=3, overflow="fold")
    else:
        table.add_column("Worker / thread / error", ratio=3, overflow="fold")
    for step in steps:
        detail_parts: list[str] = []
        if step.has_worker:
            detail_parts.append(f"PID {step.worker}")
        if step.thread_id:
            detail_parts.append(step.thread_id)
        if step.error:
            detail_parts.append(step.error)
        detail = " · ".join(detail_parts) or "—"
        identity = Group(
            Text(step.label),
            Text(step.step_id, style="bright_black"),
        )
        row: list[RenderableType] = [
            identity,
            Text(
                sanitize(step.status),
                style=STATUS_STYLES.get(step.status, "bright_black"),
            ),
            Text(step.attempt),
            Text(step.duration),
        ]
        if width >= 96:
            row.extend(
                [
                    Text(step.worker),
                    Text(
                        " · ".join(
                            part for part in [step.thread_id, step.error] if part
                        )
                        or "—"
                    ),
                ]
            )
        else:
            row.append(Text(detail))
        table.add_row(*row)
    return table


def _default_step_limit(width: int) -> int:
    """Return a stable default row budget for the available terminal width."""
    return max(1, min(MAX_SHOW_STEPS, width // 8))


def build_show_renderable(
    run: RunRecord,
    *,
    width: int,
    now: datetime,
    full: bool = False,
) -> Group:
    """Build detailed run, callback, error, and step state."""
    step_limit = MAX_SHOW_STEPS if full else _default_step_limit(width)
    run_steps = run.steps
    shown_steps = run_steps if full else run_steps[:step_limit]
    step_views = tuple(
        StepDetailView.from_record(step, now, full=full) for step in shown_steps
    )
    summary = _summary_grid(run, now, width, full)
    callback = _callback_grid(run, width, full)
    if width <= PLAIN_SHOW_MAX_WIDTH:
        parts: list[RenderableType] = [
            Text("Run", style="cyan"),
            summary,
            Text("Callback", style="blue"),
            callback,
        ]
    else:
        parts = [
            Panel(
                summary,
                title="Workflow run",
                border_style="cyan",
            ),
            Panel(
                callback,
                title="Completion callback",
                border_style="blue",
            ),
        ]
    if run.error:
        error, _ = bounded_error(run.error, full=full)
        if width <= PLAIN_SHOW_MAX_WIDTH:
            parts.extend([Text("Error", style="red"), Text(error)])
        else:
            parts.append(Panel(Text(error), title="Error", border_style="red"))
    if step_views:
        step_table = _step_table(step_views, width)
        if width <= PLAIN_SHOW_MAX_WIDTH:
            parts.extend([Text("Steps", style="bright_black"), step_table])
        else:
            parts.append(
                Panel(
                    step_table,
                    title=f"Agent steps ({len(step_views)}/{len(run_steps)})",
                    border_style="bright_black",
                )
            )
        if len(step_views) < len(run_steps):
            parts.append(
                Text(
                    f"Showing the first {len(step_views)} of "
                    f"{len(run_steps)} steps; use --full or --json for all steps.",
                    style="yellow",
                )
            )
    else:
        if width <= PLAIN_SHOW_MAX_WIDTH:
            parts.extend([Text("Steps"), Text("No agent steps recorded.")])
        else:
            parts.append(Panel("No agent steps recorded.", title="Agent steps"))
    return Group(*parts)


def fit_live_runs(
    runs: list[RunRecord],
    *,
    console: Console,
    now: datetime,
    footer: RenderableType | None = None,
    empty: RenderableType | None = None,
) -> RenderableType:
    """Fit complete, pre-normalized run rows to the current terminal height."""
    options = console.options.update(height=None)

    def fit_footer() -> RenderableType | None:
        """Return a footer that fits, prioritizing its last warning."""
        if footer is None:
            return None
        if len(console.render_lines(footer, options, pad=False)) <= console.height:
            return footer
        renderables = getattr(footer, "renderables", None)
        if renderables is not None:
            for renderable in reversed(tuple(renderables)):
                if (
                    len(console.render_lines(renderable, options, pad=False))
                    <= console.height
                ):
                    return renderable
        return None

    if not runs:
        empty_renderable = empty or Text(
            "Waiting for workflow runs…",
            style="bright_black",
            overflow="ellipsis",
            no_wrap=True,
        )
        if footer is None:
            return empty_renderable
        candidate = Group(empty_renderable, footer)
        if len(console.render_lines(candidate, options, pad=False)) <= console.height:
            return candidate
        return fit_footer() or empty_renderable

    views = [RunRowView.from_record(run, now) for run in runs]

    def build(visible: int) -> RenderableType:
        omitted = len(views) - visible
        marker = Text(
            f"Showing {visible} of {len(views)} selected runs; "
            f"{omitted} omitted (terminal height).",
            style="bright_black",
            overflow="ellipsis",
            no_wrap=True,
        )
        parts: list[RenderableType] = []
        if visible:
            parts.append(
                _build_runs_table_from_views(
                    views[:visible],
                    width=console.width,
                    live=True,
                )
            )
        if omitted:
            parts.append(marker)
        if footer is not None:
            parts.append(footer)
        return Group(*parts)

    maximum_visible = min(len(views), max(0, console.height))
    largest_candidate = build(maximum_visible)
    if (
        maximum_visible == len(views)
        and len(console.render_lines(largest_candidate, options, pad=False))
        <= console.height
    ):
        return largest_candidate

    low = 0
    high = maximum_visible
    while low < high:
        middle = (low + high + 1) // 2
        candidate = build(middle)
        if len(console.render_lines(candidate, options, pad=False)) <= console.height:
            low = middle
        else:
            high = middle - 1
    smallest_candidate = build(low)
    if (
        len(console.render_lines(smallest_candidate, options, pad=False))
        <= console.height
    ):
        return smallest_candidate
    fitted_footer = fit_footer()
    if fitted_footer is not None:
        return fitted_footer
    return smallest_candidate


MAX_DIFF_VALUE_CHARS = 160
MAX_DIFF_ERROR_CHARS = 200
MAX_DIFF_UNCHANGED_SHOWN = 12


def _bounded_diff_text(value: object, *, full: bool) -> str:
    return bounded_text(
        value,
        maximum=MAX_DIFF_VALUE_CHARS,
        full=full,
    )[0]


def _format_signed_delta(seconds: float | None) -> str:
    """Format a B-minus-A duration delta with an explicit sign."""
    if seconds is None:
        return "—"
    if abs(seconds) < 0.05:
        return "0s"
    sign = "+" if seconds > 0 else "-"
    return f"{sign}{format_duration(abs(seconds))}"


def _diff_side_heading(side: RunSideSummary, label: str) -> Text:
    """Build one colored run identity line for the compare header."""
    heading = Text()
    heading.append(f"{label} ", style="bold")
    heading.append(
        _bounded_diff_text(side.abbreviated_id, full=False),
        style="bold",
    )
    heading.append(" · ")
    heading.append(side.workflow_name)
    heading.append(" · ")
    heading.append(
        side.status,
        style=STATUS_STYLES.get(side.status, "bright_black"),
    )
    return heading


def _diff_state_errors(diff: RunDiff) -> list[RenderableType]:
    """Surface unreadable-state diagnostics without blocking comparison."""
    renderables: list[RenderableType] = []
    for label, side in (("A", diff.a), ("B", diff.b)):
        if side.state_error:
            error, _ = bounded_text(
                side.state_error,
                maximum=MAX_DIFF_ERROR_CHARS,
                full=False,
            )
            renderables.append(
                Text(f"{label} state unreadable: {error}", style="red", overflow="fold")
            )
    return renderables


def _diff_settings_table(diff: RunDiff, *, full: bool) -> Table:
    """Build the run-setting comparison table."""
    table = Table(box=box.SIMPLE_HEAD, expand=True, show_edge=False)
    table.add_column("", width=2, no_wrap=True)
    table.add_column("Setting", style="bold", width=18, no_wrap=True)
    table.add_column(
        _bounded_diff_text(diff.a.abbreviated_id, full=False),
        overflow="fold",
        ratio=1,
    )
    table.add_column(
        _bounded_diff_text(diff.b.abbreviated_id, full=False),
        overflow="fold",
        ratio=1,
    )
    for setting in diff.settings:
        value_a = _diff_setting_text(setting, "a", full=full)
        value_b = _diff_setting_text(setting, "b", full=full)
        table.add_row(
            Text("≠", style="yellow") if setting.changed else "",
            setting.label,
            value_a,
            value_b,
        )
    return table


def _diff_setting_text(
    setting: SettingComparison,
    side: str,
    *,
    full: bool,
) -> Text:
    """Render one setting value with its difference styling."""
    value = setting.a if side == "a" else setting.b
    if value is None:
        return Text("—", style="bright_black")
    style = "yellow" if setting.changed else None
    return Text(_bounded_diff_text(value, full=full), style=style)


def _diff_single_side_steps(
    side_label: str,
    run_id: str,
    steps: tuple[StepSideSummary, ...],
    *,
    full: bool,
) -> Panel:
    """List steps that exist on only one compared run."""
    rows: list[RenderableType] = []
    for step in steps:
        line = Text()
        line.append(step.id, style="bold")
        line.append("  ")
        line.append(
            step.status,
            style=STATUS_STYLES.get(step.status, "bright_black"),
        )
        attempt = "—" if step.attempt is None else str(step.attempt)
        line.append(f" · try {attempt} · {format_duration(step.duration_seconds)}")
        if step.error:
            error = bounded_text(
                step.error,
                maximum=MAX_DIFF_ERROR_CHARS,
                full=full,
            )[0]
            line.append(f" · {error}", style="red")
        rows.append(line)
    return Panel(
        Group(*rows),
        title=f"Only in {side_label} ({_bounded_diff_text(run_id, full=False)})",
        border_style="magenta",
    )


def _diff_status_transition(before: str, after: str) -> Text:
    """Render one colored status transition."""
    line = Text("status: ")
    line.append(before, style=STATUS_STYLES.get(before, "bright_black"))
    line.append(" → ")
    line.append(after, style=STATUS_STYLES.get(after, "bright_black"))
    return line


def _diff_changed_step(step: StepComparison, *, full: bool) -> Group:
    """Render one paired step's changed fields."""
    rows: list[RenderableType] = [Text(step.id, style="bold")]
    if step.input_changed:
        rows.append(
            Text("input changed (fingerprint differs; step re-ran)", style="yellow")
        )
    if step.status_changed:
        rows.append(_diff_status_transition(step.status_a, step.status_b))
    if step.attempt_changed:
        attempt_a = "—" if step.attempt_a is None else str(step.attempt_a)
        attempt_b = "—" if step.attempt_b is None else str(step.attempt_b)
        rows.append(Text(f"attempt: {attempt_a} → {attempt_b}"))
    rows.append(
        Text(
            "time: "
            f"{format_duration(step.duration_a)} → "
            f"{format_duration(step.duration_b)} "
            f"({_format_signed_delta(step.duration_delta_seconds)})",
            style="bright_black",
        )
    )
    if step.error_changed:
        rows.append(Text("error:", style="bold red"))
        error_a = bounded_text(
            step.error_a or "—",
            maximum=MAX_DIFF_ERROR_CHARS,
            full=full,
        )[0]
        error_b = bounded_text(
            step.error_b or "—",
            maximum=MAX_DIFF_ERROR_CHARS,
            full=full,
        )[0]
        rows.append(Text(f"  A: {error_a}", style="red", overflow="fold"))
        rows.append(Text(f"  B: {error_b}", style="red", overflow="fold"))
    return Group(*rows)


def _diff_changed_steps_panel(
    steps: tuple[StepComparison, ...],
    *,
    full: bool,
) -> Panel:
    """Render every paired step whose inputs or outcomes changed."""
    blocks: list[RenderableType] = []
    for index, step in enumerate(steps):
        if index:
            blocks.append(Text(""))
        blocks.append(_diff_changed_step(step, full=full))
    return Panel(
        Group(*blocks),
        title=f"Changed steps ({len(steps)})",
        border_style="yellow",
    )


def _diff_unchanged_panel(diff: RunDiff, *, width: int, full: bool) -> Panel:
    """Group identical steps and report their aggregate timing shift."""
    unchanged = diff.unchanged
    rows: list[RenderableType] = [
        Text(
            f"{unchanged.count} step{'s' if unchanged.count != 1 else ''} "
            "unchanged (same input, status, attempts, and error)"
        )
    ]
    if unchanged.duration_delta_seconds is not None:
        rows.append(
            Text(
                "total time delta: "
                + _format_signed_delta(unchanged.duration_delta_seconds),
                style="bright_black",
            )
        )
    if unchanged.ids:
        shown = unchanged.ids if full else unchanged.ids[:MAX_DIFF_UNCHANGED_SHOWN]
        listing = ", ".join(shown)
        bounded = bounded_text(
            listing,
            maximum=max(20, width - 8),
            full=full,
        )[0]
        rows.append(Text(bounded, style="bright_black", overflow="fold"))
        if len(shown) < len(unchanged.ids):
            omitted = len(unchanged.ids) - len(shown)
            rows.append(
                Text(
                    f"{omitted} more omitted; use --full or --json for all steps.",
                    style="yellow",
                )
            )
    return Panel(Group(*rows), title="Unchanged steps", border_style="green")


def build_diff_renderable(
    diff: RunDiff,
    *,
    width: int,
    full: bool = False,
    notice: RenderableType | None = None,
) -> Group:
    """Build the complete human-readable two-run comparison.

    Args:
        diff: Coherent typed comparison of two loaded runs.
        width: Current terminal width for bounded wrapping.
        full: Whether to disable human-output truncation.
        notice: Optional watch-mode banner naming the updated side.

    Returns:
        A renderable beginning with launch settings and continuing with
        single-side, changed, and unchanged steps.
    """
    parts: list[RenderableType] = []
    if notice is not None:
        parts.append(notice)
    parts.append(
        Panel(
            Group(
                _diff_side_heading(diff.a, "A"),
                _diff_side_heading(diff.b, "B"),
                *_diff_state_errors(diff),
            ),
            title="Comparing runs",
            border_style="cyan",
        )
    )
    parts.append(Panel(_diff_settings_table(diff, full=full), title="Run settings"))
    if not any(setting.changed for setting in diff.settings):
        parts.append(
            Text(
                "No differences in working directory, concurrency, or "
                "invocation limit.",
                style="green",
            )
        )
    if diff.identical:
        parts.append(
            Text(
                "No differences: the runs match on settings, step inputs, "
                "statuses, attempts, errors, and step membership.",
                style="green",
            )
        )
        return Group(*parts)
    if diff.only_a:
        parts.append(
            _diff_single_side_steps("A", diff.a.run_id, diff.only_a, full=full)
        )
    if diff.only_b:
        parts.append(
            _diff_single_side_steps("B", diff.b.run_id, diff.only_b, full=full)
        )
    if diff.changed_steps:
        parts.append(_diff_changed_steps_panel(diff.changed_steps, full=full))
    if diff.unchanged.count:
        parts.append(_diff_unchanged_panel(diff, width=width, full=full))
    return Group(*parts)
