"""Tests for two-run comparison in the durable-workflow CLI."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner, Result
from rich.console import Console

from claude_code_tools import workflow_cli
from claude_code_tools.workflow_cli import cli
from claude_code_tools.workflow_cli_contract import (
    COMPARE_KEYS,
    COMPARE_SETTING_KEYS,
    COMPARE_SIDE_KEYS,
    COMPARE_STEP_DIFF_KEYS,
    COMPARE_STEP_SIDE_KEYS,
    COMPARE_STEPS_KEYS,
    COMPARE_UNCHANGED_KEYS,
    compare_payload,
)
from claude_code_tools.workflow_cli_diff import (
    changed_sides,
    diff_runs,
    state_signature,
)
from claude_code_tools.workflow_cli_snapshots import RunRecord, RunState, StepRecord
from claude_code_tools.workflow_validation import parse_run_record

NOW = datetime(2026, 7, 14, 16, 0, tzinfo=UTC)


def _step(
    step_id: str,
    *,
    status: str = "completed",
    attempt: int = 1,
    started: str,
    completed: str | None = None,
    fingerprint: str = "fp",
    error: str | None = None,
    label: str | None = None,
) -> dict[str, object]:
    """Build one persisted version-1 step object."""
    value: dict[str, object] = {
        "attempt": attempt,
        "fingerprint": fingerprint,
        "id": step_id,
        "label": label or step_id,
        "startedAt": started,
        "status": status,
    }
    if completed is not None:
        value["completedAt"] = completed
    if error is not None:
        value["error"] = error
    return value


def _state(
    run_id: str,
    *,
    cwd: str = "/work",
    concurrency: int = 4,
    max_invocations: int | None = 100,
    created: str,
    updated: str,
    completed: str | None = None,
    steps: dict[str, object] | None = None,
    status: str = "completed",
) -> dict[str, object]:
    """Build a valid version-1 run-state object."""
    value: dict[str, object] = {
        "concurrency": concurrency,
        "createdAt": created,
        "cwd": cwd,
        "runId": run_id,
        "status": status,
        "steps": steps or {},
        "updatedAt": updated,
        "version": 1,
        "workflowHash": "hash-1",
        "workflowPath": "/work/flow.js",
    }
    if completed is not None:
        value["completedAt"] = completed
    if max_invocations is not None:
        value["maxAgentInvocations"] = max_invocations
    return value


def _write_run(home: Path, state: dict[str, object]) -> Path:
    """Write one state.json under an isolated workflow home."""
    directory = home / "runs" / str(state["runId"])
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return directory


def _record(state: dict[str, object]) -> RunRecord:
    """Parse one state mapping into its typed run record."""
    return parse_run_record(directory=Path(str(state["runId"])), state=state)


def _invoke(home: Path, arguments: list[str]) -> Result:
    """Invoke the CLI against an isolated workflow home."""
    return CliRunner().invoke(
        cli,
        arguments,
        env={"CODEX_WORKFLOW_HOME": str(home), "NO_COLOR": "1"},
        color=False,
    )


def _pair_states() -> tuple[dict[str, object], dict[str, object]]:
    """Build representative runs sharing one step and diverging elsewhere."""
    state_a = _state(
        "run-alpha",
        cwd="/work",
        concurrency=4,
        max_invocations=100,
        created="2026-07-14T14:00:00Z",
        updated="2026-07-14T14:10:00Z",
        completed="2026-07-14T14:10:00Z",
        steps={
            "root/same": _step(
                "root/same",
                started="2026-07-14T14:00:00Z",
                completed="2026-07-14T14:00:10Z",
                fingerprint="same-fp",
            ),
            "root/rework": _step(
                "root/rework",
                status="failed",
                attempt=2,
                started="2026-07-14T14:00:20Z",
                completed="2026-07-14T14:00:50Z",
                fingerprint="old-input",
                error="worker exhausted retries",
            ),
            "root/a-only": _step(
                "root/a-only",
                started="2026-07-14T14:01:00Z",
                completed="2026-07-14T14:01:05Z",
                fingerprint="a-fp",
            ),
        },
    )
    state_b = _state(
        "run-bravo",
        cwd="/work-next",
        concurrency=8,
        max_invocations=200,
        created="2026-07-14T15:00:00Z",
        updated="2026-07-14T15:10:00Z",
        completed="2026-07-14T15:10:00Z",
        steps={
            "root/same": _step(
                "root/same",
                started="2026-07-14T15:00:00Z",
                completed="2026-07-14T15:00:12Z",
                fingerprint="same-fp",
            ),
            "root/rework": _step(
                "root/rework",
                status="completed",
                attempt=1,
                started="2026-07-14T15:00:00Z",
                completed="2026-07-14T15:00:10Z",
                fingerprint="new-input",
            ),
            "root/b-only": _step(
                "root/b-only",
                started="2026-07-14T15:01:00Z",
                completed="2026-07-14T15:01:07Z",
                fingerprint="b-fp",
            ),
        },
    )
    return state_a, state_b


def test_diff_flags_cwd_concurrency_and_invocation_limit() -> None:
    """Launch settings are compared field by field."""
    state_a, state_b = _pair_states()
    diff = diff_runs(_record(state_a), _record(state_b), now_a=NOW, now_b=NOW)

    settings = {setting.field: setting for setting in diff.settings}
    assert [setting.field for setting in diff.settings] == [
        "cwd",
        "concurrency",
        "maxAgentInvocations",
    ]
    assert settings["cwd"].a == "/work"
    assert settings["cwd"].b == "/work-next"
    assert settings["concurrency"].a == 4
    assert settings["concurrency"].b == 8
    assert settings["maxAgentInvocations"].a == 100
    assert settings["maxAgentInvocations"].b == 200
    assert all(setting.changed for setting in diff.settings)
    assert diff.identical is False


def test_diff_groups_single_side_changed_and_unchanged_steps() -> None:
    """Paired steps are classified by membership, input, and outcome."""
    state_a, state_b = _pair_states()
    diff = diff_runs(_record(state_a), _record(state_b), now_a=NOW, now_b=NOW)

    assert [step.id for step in diff.only_a] == ["root/a-only"]
    assert [step.id for step in diff.only_b] == ["root/b-only"]

    changed = {step.id: step for step in diff.changed_steps}
    assert set(changed) == {"root/rework"}
    rework = changed["root/rework"]
    assert rework.input_known is True
    assert rework.input_changed is True
    assert rework.status_a == "failed"
    assert rework.status_b == "completed"
    assert rework.status_changed is True
    assert rework.attempt_a == 2
    assert rework.attempt_b == 1
    assert rework.attempt_changed is True
    assert rework.duration_a == 30.0
    assert rework.duration_b == 10.0
    assert rework.duration_delta_seconds == -20.0
    assert rework.error_a == "worker exhausted retries"
    assert rework.error_b is None
    assert rework.error_changed is True

    assert diff.unchanged.ids == ("root/same",)
    assert diff.unchanged.count == 1
    assert diff.unchanged.duration_delta_seconds == 2.0


def test_identical_runs_have_no_differences() -> None:
    """Equal settings and steps collapse into the identical state."""
    state_a, _ = _pair_states()
    state_b = json.loads(json.dumps(state_a))
    state_b["runId"] = "run-bravo"

    diff = diff_runs(_record(state_a), _record(state_b), now_a=NOW, now_b=NOW)

    assert diff.identical is True
    assert not diff.only_a
    assert not diff.only_b
    assert not diff.changed_steps
    assert diff.unchanged.count == 3
    assert diff.unchanged.duration_delta_seconds == 0.0


def test_missing_fingerprint_keeps_input_unknown() -> None:
    """Status changes still compare when one side lacks an input fingerprint."""
    step_a = StepRecord(
        id="root/x",
        label="x",
        status="running",
        attempt=1,
        started_at="2026-07-14T14:00:00Z",
        completed_at=None,
        error=None,
        worker_pid=None,
        thread_id=None,
        fingerprint=None,
    )
    step_b = StepRecord(
        id="root/x",
        label="x",
        status="completed",
        attempt=1,
        started_at="2026-07-14T14:00:00Z",
        completed_at="2026-07-14T14:00:05Z",
        error=None,
        worker_pid=None,
        thread_id=None,
        fingerprint=None,
    )
    run_a = RunRecord(
        directory=Path("a"),
        state=RunState(run_id="a", steps=(step_a,), concurrency=2),
    )
    run_b = RunRecord(
        directory=Path("b"),
        state=RunState(run_id="b", steps=(step_b,), concurrency=2),
    )

    diff = diff_runs(run_a, run_b, now_a=NOW, now_b=NOW)

    (comparison,) = diff.changed_steps
    assert comparison.input_known is False
    assert comparison.input_changed is False
    assert comparison.status_changed is True
    assert comparison.attempt_changed is False


def test_compare_json_contract_has_exact_version_1_keys() -> None:
    """The compare envelope exposes only documented key sets."""
    state_a, state_b = _pair_states()
    payload = compare_payload(
        diff_runs(_record(state_a), _record(state_b), now_a=NOW, now_b=NOW)
    )

    assert payload.keys() == COMPARE_KEYS
    assert payload["schemaVersion"] == 1
    assert payload["identical"] is False
    assert payload["a"].keys() == COMPARE_SIDE_KEYS
    assert payload["b"].keys() == COMPARE_SIDE_KEYS
    assert payload["a"]["runId"] == "run-alpha"
    assert [setting["field"] for setting in payload["settings"]] == [
        "cwd",
        "concurrency",
        "maxAgentInvocations",
    ]
    for setting in payload["settings"]:
        assert setting.keys() == COMPARE_SETTING_KEYS
    steps = payload["steps"]
    assert steps.keys() == COMPARE_STEPS_KEYS
    assert steps["onlyA"][0].keys() == COMPARE_STEP_SIDE_KEYS
    assert steps["onlyB"][0].keys() == COMPARE_STEP_SIDE_KEYS
    assert steps["changed"][0].keys() == COMPARE_STEP_DIFF_KEYS
    assert steps["unchanged"].keys() == COMPARE_UNCHANGED_KEYS
    assert steps["changed"][0]["durationDeltaSeconds"] == -20.0


def test_diff_text_output_prints_each_section(tmp_path: Path) -> None:
    """Human output leads with settings and names every difference class."""
    state_a, state_b = _pair_states()
    _write_run(tmp_path, state_a)
    _write_run(tmp_path, state_b)

    result = _invoke(tmp_path, ["diff", "run-alpha", "run-bravo"])

    assert result.exit_code == 0
    assert "Comparing runs" in result.output
    assert "Run settings" in result.output
    assert "Working directory" in result.output
    assert "Concurrency" in result.output
    assert "Invocation limit" in result.output
    assert "Only in A" in result.output
    assert "root/a-only" in result.output
    assert "Only in B" in result.output
    assert "root/b-only" in result.output
    assert "Changed steps (1)" in result.output
    assert "root/rework" in result.output
    assert "input changed (fingerprint differs; step re-ran)" in result.output
    assert "failed → completed" in result.output
    assert "attempt: 2 → 1" in result.output
    assert "-20s" in result.output
    assert "Unchanged steps" in result.output
    assert "root/same" in result.output


def test_identical_runs_print_match_message(tmp_path: Path) -> None:
    """Fully matching runs produce the consolidated success message."""
    state_a, _ = _pair_states()
    state_b = json.loads(json.dumps(state_a))
    state_b["runId"] = "run-bravo"
    _write_run(tmp_path, state_a)
    _write_run(tmp_path, state_b)

    result = _invoke(tmp_path, ["diff", "run-alpha", "run-bravo"])

    assert result.exit_code == 0
    assert "No differences in working directory, concurrency, or" in result.output
    assert "No differences: the runs match" in result.output
    assert "Changed steps" not in result.output


def test_diff_json_via_cli_and_resolution_errors(tmp_path: Path) -> None:
    """JSON output works end to end and unknown runs fail the command."""
    state_a, state_b = _pair_states()
    _write_run(tmp_path, state_a)
    _write_run(tmp_path, state_b)

    result = _invoke(tmp_path, ["diff", "run-alpha", "run-bravo", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["schemaVersion"] == 1
    assert payload["a"]["runId"] == "run-alpha"
    assert payload["b"]["runId"] == "run-bravo"

    missing = _invoke(tmp_path, ["diff", "run-alpha", "missing-run"])
    assert missing.exit_code != 0
    assert "Workflow run not found: missing-run" in missing.output

    watch_json = _invoke(
        tmp_path,
        ["diff", "run-alpha", "run-bravo", "--watch", "--json"],
    )
    assert watch_json.exit_code != 0
    assert "--json is not supported by diff --watch" in watch_json.output


def test_diff_keeps_going_with_unreadable_state(tmp_path: Path) -> None:
    """A malformed side is labeled but does not block the comparison."""
    state_a, _ = _pair_states()
    _write_run(tmp_path, state_a)
    broken = tmp_path / "runs" / "broken-run"
    broken.mkdir(parents=True)
    (broken / "state.json").write_text("{not-json", encoding="utf-8")

    result = _invoke(tmp_path, ["diff", "run-alpha", "broken-run"])

    assert result.exit_code == 0
    assert "B state unreadable" in result.output
    assert "root/a-only" in result.output


def test_change_detection_names_exactly_the_updated_side(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Watch frames re-read both sides and name whichever side changed."""
    monkeypatch.setenv("CODEX_WORKFLOW_HOME", str(tmp_path))
    state_a, state_b = _pair_states()
    _write_run(tmp_path, state_a)
    _write_run(tmp_path, state_b)

    def render_frame(frame: object) -> str:
        console = Console(
            color_system=None,
            force_terminal=False,
            record=True,
            width=120,
        )
        console.print(frame)
        return console.export_text()

    first, previous = workflow_cli._diff_frame(
        "run-alpha",
        "run-bravo",
        None,
        width=120,
        full=False,
    )
    assert "Live comparison" in render_frame(first)

    state_b["updatedAt"] = "2026-07-14T15:20:00Z"
    state_b["completedAt"] = "2026-07-14T15:20:00Z"
    (tmp_path / "runs" / "run-bravo" / "state.json").write_text(
        json.dumps(state_b),
        encoding="utf-8",
    )
    second, previous = workflow_cli._diff_frame(
        "run-alpha",
        "run-bravo",
        previous,
        width=120,
        full=False,
    )
    second_text = render_frame(second)
    assert "Updated since last refresh: run-bravo (B)" in second_text
    assert "(A)" not in second_text

    state_a["updatedAt"] = "2026-07-14T14:20:00Z"
    state_a["completedAt"] = "2026-07-14T14:20:00Z"
    (tmp_path / "runs" / "run-alpha" / "state.json").write_text(
        json.dumps(state_a),
        encoding="utf-8",
    )
    third, _ = workflow_cli._diff_frame(
        "run-alpha",
        "run-bravo",
        previous,
        width=120,
        full=False,
    )
    third_text = render_frame(third)
    assert "Updated since last refresh: run-alpha (A)" in third_text
    assert "(B)" not in third_text


def test_changed_sides_helper_handles_missing_previous_frame() -> None:
    """The first frame and recovered unreadable sides report no changes."""
    record_a = _record(_pair_states()[0])
    signature_a = state_signature(record_a)
    signature_b = state_signature(record_a)

    assert changed_sides(None, (signature_a, signature_b)) == ()
    assert changed_sides((None, None), (signature_a, signature_b)) == ()
    assert changed_sides(
        (signature_a, signature_b),
        (signature_a, ("different", None)),
    ) == (1,)
