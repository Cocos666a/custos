"""The goal driver, exercised with a fake adapter.

Nothing here spawns a real CLI: the adapter is a stand-in that records the
prompts it was given and answers from a script. That keeps the tests about the
driver's decisions -- resume or rotate, and when to stop -- rather than about
any tool's command line.
"""

from __future__ import annotations

import json
import os

import pytest

from custos.adapters.base import PromptResult, RotateResult, SessionRef
from custos.core import goal_driver as goal_driver_module
from custos.core.goal_driver import GoalDriver
from custos.core.usage import Reading
from custos.core.util import FileLock


class FakeAdapter:
    """A scripted adapter: prompts are recorded, sessions are invented."""

    name = "fake"

    def __init__(self, usage=None, rotate_session="s-new", rotate_error=None,
                 rc=0, on_prompt=None):
        self.usage = dict(usage or {})
        self.rotate_session = rotate_session
        self.rotate_error = rotate_error
        self.rc = rc
        self.on_prompt = on_prompt
        self.prompts = []
        self.rotations = []
        self._rotation = 0

    def run_prompt(self, prompt, timeout=240.0, cwd=None, extra_args=None):
        self.prompts.append({"prompt": prompt, "extra_args": list(extra_args or []),
                             "cwd": cwd})
        if self.on_prompt is not None:
            self.on_prompt(self, prompt)
        return PromptResult(text="a synthetic round", rc=self.rc, session=None)

    def ref_for(self, session_id):
        return SessionRef(session=session_id, path="", tool=self.name)

    def read_usage(self, session):
        return Reading(tool=self.name, session=session.session,
                       tokens=int(self.usage.get(session.session, 0)),
                       source="transcript")

    def recent_messages(self, session, limit=12):
        return []

    def history_unavailable_reason(self):
        return "the fake adapter keeps no history"

    def rotate(self, session, handover_path, dry_run=False, config=None):
        self.rotations.append({"session": session, "handover": handover_path})
        if self.rotate_error:
            return RotateResult(mode="full-auto", rotated=False,
                                detail={"handover": handover_path},
                                error=self.rotate_error)
        self._rotation += 1
        new_session = self.rotate_session
        if new_session == "auto":
            new_session = "s-%d" % (len(self.rotations) + 1)
        return RotateResult(mode="full-auto", rotated=True,
                            new_session=new_session,
                            detail={"handover": handover_path})

    def session_files(self):
        return []


def load_config(sandbox, **overrides):
    from custos.core import config as config_module

    payload = {"version": 1, "thresholds": {"baseline_tokens": 100000,
                                            "rotate_factor": 0.5}}
    payload.update(overrides)
    sandbox.write_config(payload)
    return config_module.load_config(sandbox.config_dir)


def write_task(tmp_path, text="Do the synthetic goal."):
    path = tmp_path / "TASK.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


def make_driver(sandbox, adapter, tmp_path, events=None, **kwargs):
    options = {
        "tool": "fake",
        "task_file": write_task(tmp_path),
        "cwd": str(tmp_path),
        "max_sessions": 5,
        "max_rounds_per_session": 10,
        "rotate_line": 1000,
    }
    options.update(kwargs)
    return GoalDriver(
        load_config(sandbox), adapter, emit=(events.append if events is not None else None),
        **options)


def read_state(tmp_path):
    path = tmp_path / ".custos-goal-state.json"
    with open(str(path), encoding="utf-8") as stream:
        return json.load(stream)


def outcomes(events):
    return [event["event"] for event in events]


# ------------------------------------------------------------------- rounds


def test_rounds_below_the_line_resume_the_same_session(sandbox, tmp_path):
    adapter = FakeAdapter(usage={"s-fresh": 10})
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=1000,
                         tool="claude_code", max_sessions=1, max_rounds_per_session=1)
    driver._discover_session = lambda result, before, t0: "s-fresh"

    driver.run()

    # Round 1 started the session, round 2 resumed it; the round cap then
    # stopped the run, so the only rounds are those two.
    assert len(adapter.rotations) == 0
    assert len(adapter.prompts) == 2
    assert adapter.prompts[0]["extra_args"] == []
    assert adapter.prompts[1]["extra_args"] == ["--resume", "s-fresh",
                                               "--output-format", "json"]
    state = read_state(tmp_path)
    assert state["sessions"][0]["session"] == "s-fresh"
    assert state["rotations"] == []


def test_codex_resumes_with_its_own_argument_spelling(sandbox, tmp_path):
    adapter = FakeAdapter(usage={"s-fresh": 10})
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=1000,
                         tool="codex", max_sessions=1, max_rounds_per_session=1)
    driver._discover_session = lambda result, before, t0: "s-fresh"

    driver.run()
    assert adapter.prompts[1]["extra_args"] == ["resume", "s-fresh"]


def test_at_the_line_the_driver_rotates_and_continues(sandbox, tmp_path):
    adapter = FakeAdapter(usage={"s-fresh": 1500, "s-rotated": 10},
                          rotate_session="s-rotated")
    events = []
    driver = make_driver(sandbox, adapter, tmp_path, events=events, rotate_line=1000,
                         max_sessions=2, max_rounds_per_session=1)
    driver._discover_session = lambda result, before, t0: "s-fresh"

    driver.run()

    assert len(adapter.rotations) == 1
    assert adapter.rotations[0]["session"] == "s-fresh"
    assert os.path.isfile(adapter.rotations[0]["handover"])
    handover_text = open(adapter.rotations[0]["handover"], encoding="utf-8").read()
    assert "<!-- custos handover schema=1 -->" in handover_text

    state = read_state(tmp_path)
    rotation = state["rotations"][0]
    assert rotation["old"] == "s-fresh"
    assert rotation["new"] == "s-rotated"
    assert rotation["tokens"] == 1500
    assert rotation["rotate_line"] == 1000
    assert rotation["trigger"] == "tokens 1500 >= line 1000"
    assert rotation["handover"] == adapter.rotations[0]["handover"]
    assert rotation["mode"] == "full-auto"
    assert rotation["ts"]
    # The rotated-to session carries the following round.
    assert [entry["session"] for entry in state["sessions"]] == ["s-fresh", "s-rotated"]
    assert "rotate" in outcomes(events)


def test_no_resume_rotates_before_every_round(sandbox, tmp_path):
    adapter = FakeAdapter(usage={"s-1": 0, "s-2": 0, "s-3": 0}, rotate_session="auto")
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=1000,
                         no_resume=True, max_sessions=2)
    driver._discover_session = lambda result, before, t0: "s-1"

    driver.run()
    assert len(adapter.rotations) >= 1
    assert adapter.prompts[0]["extra_args"] == []


# --------------------------------------------------------------- completion


def test_goal_complete_in_the_output_stops_the_run(sandbox, tmp_path):
    adapter = FakeAdapter(usage={"s-fresh": 10})

    def reply(a, prompt):
        a.run_prompt = lambda prompt, timeout=240.0, cwd=None, extra_args=None: (
            PromptResult(text="all done\nGOAL_COMPLETE", rc=0))

    adapter.on_prompt = reply
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=1000)
    driver._discover_session = lambda result, before, t0: "s-fresh"

    assert driver.run() == 0
    state = read_state(tmp_path)
    assert state["outcome"] == "completed"


def test_goal_complete_in_the_progress_file_stops_the_run(sandbox, tmp_path):
    adapter = FakeAdapter(usage={"s-fresh": 10})
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=1000)
    driver._discover_session = lambda result, before, t0: "s-fresh"
    (tmp_path / "custos-goal-progress.md").write_text(
        "# Goal task\n\nGOAL_COMPLETE\n", encoding="utf-8")

    driver.run()
    assert read_state(tmp_path)["outcome"] == "completed"


def test_goal_finished_inside_the_rotation_spawn_stops_the_run(sandbox, tmp_path):
    """A rotation spawn is a full agent run: if it completes the goal, the
    driver must stop instead of rotating again (observed live on codex, where
    every rotation spawn cleared a stage and the last one wrote the marker)."""
    adapter = FakeAdapter(usage={"s-fresh": 2000, "s-new": 3000})
    events = []
    driver = make_driver(sandbox, adapter, tmp_path, events=events, rotate_line=1000,
                         max_sessions=5)
    driver._discover_session = lambda result, before, t0: "s-fresh"

    def finish_inside_rotation(_adapter, _prompt):
        path = os.path.join(str(tmp_path), "custos-goal-progress.md")
        with open(path, "a", encoding="utf-8") as stream:
            stream.write("STAGE 4 DONE\nGOAL_COMPLETE\n")

    original_rotate = adapter.rotate

    def rotate_with_completion(session, handover_path, dry_run=False, config=None):
        finish_inside_rotation(adapter, handover_path)
        return original_rotate(session, handover_path, dry_run=dry_run, config=config)

    adapter.rotate = rotate_with_completion

    driver.run()
    state = read_state(tmp_path)
    assert state["outcome"] == "completed"
    # The rotation that finished the goal is the LAST one: no further round,
    # no further rotation, after the marker was seen.
    assert len(state["rotations"]) == 1
    assert "completed" in outcomes(events)


def test_the_progress_file_is_created_with_the_task(sandbox, tmp_path):
    adapter = FakeAdapter(usage={"s-fresh": 10})
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=1000)
    driver._discover_session = lambda result, before, t0: "s-fresh"
    adapter.on_prompt = lambda a, prompt: setattr(
        a, "run_prompt",
        lambda prompt, timeout=240.0, cwd=None, extra_args=None: PromptResult(
            text="GOAL_COMPLETE", rc=0))

    driver.run()
    text = (tmp_path / "custos-goal-progress.md").read_text(encoding="utf-8")
    assert text.startswith("# Goal task")
    assert "Do the synthetic goal." in text


def test_the_first_prompt_carries_the_task(sandbox, tmp_path):
    adapter = FakeAdapter(usage={})
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=1000)
    driver._discover_session = lambda result, before, t0: "s-fresh"
    adapter.on_prompt = lambda a, prompt: setattr(
        a, "run_prompt",
        lambda prompt, timeout=240.0, cwd=None, extra_args=None: PromptResult(
            text="GOAL_COMPLETE", rc=0))

    driver.run()
    first = adapter.prompts[0]["prompt"]
    assert "Do the synthetic goal." in first
    assert os.path.join(str(tmp_path), "TASK.md") in first
    assert "GOAL_COMPLETE" in first


# ------------------------------------------------------------ stop conditions


def test_max_sessions_stops_the_run(sandbox, tmp_path):
    adapter = FakeAdapter(usage={}, rotate_session="auto")
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=0, max_sessions=1)
    driver._discover_session = lambda result, before, t0: "s-1"

    driver.run()
    state = read_state(tmp_path)
    assert state["outcome"] == "stopped_max_sessions"


def test_max_rounds_stops_the_run(sandbox, tmp_path):
    # A fixed rotate target keeps the session count flat, so only the round cap
    # can stop this run.
    adapter = FakeAdapter(usage={}, rotate_session="s-new")
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=0, max_sessions=10,
                         max_rounds_per_session=1)
    driver._discover_session = lambda result, before, t0: "s-1"

    driver.run()
    assert read_state(tmp_path)["outcome"] == "stopped_max_rounds"


def test_the_disable_file_stops_the_run(sandbox, tmp_path):
    adapter = FakeAdapter(usage={}, rotate_session="auto")
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=0, max_sessions=3)
    driver._discover_session = lambda result, before, t0: "s-1"

    def create_disabled(adapter_, prompt):
        open(os.path.join(sandbox.config_dir, "DISABLE"), "w").close()

    adapter.on_prompt = create_disabled

    driver.run()
    state = read_state(tmp_path)
    assert state["outcome"] == "killed_switch"


def test_a_failed_rotation_stops_with_failed(sandbox, tmp_path):
    adapter = FakeAdapter(usage={"s-1": 10}, rotate_error="the tool is gone")
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=0)
    driver._discover_session = lambda result, before, t0: "s-1"

    assert driver.run() == 1
    state = read_state(tmp_path)
    assert state["outcome"] == "failed"
    assert "the tool is gone" in state["error"]


def test_two_failed_spawns_in_a_row_stop_the_run(sandbox, tmp_path):
    adapter = FakeAdapter(usage={"s-1": 0}, rc=1)
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=1000)
    driver._discover_session = lambda result, before, t0: "s-1"

    assert driver.run() == 1
    assert read_state(tmp_path)["outcome"] == "failed"


# ------------------------------------------------------------------- locking


def test_a_second_driver_exits_three(sandbox, tmp_path):
    adapter = FakeAdapter(usage={})
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=1000)
    state_path = os.path.join(str(tmp_path), ".custos-goal-state.json")

    holding = FileLock(state_path)
    assert holding.acquire()
    try:
        assert driver.run() == 3
    finally:
        holding.release()


def test_the_cli_reports_the_lock_in_one_line(sandbox, tmp_path):
    """The CLI contract: one line on stdout and exit status 3."""
    from custos.cli import main

    task = write_task(tmp_path)
    sandbox.write_config({"version": 1})
    state_path = os.path.join(str(tmp_path), ".custos-goal-state.json")
    holding = FileLock(state_path)
    assert holding.acquire()
    try:
        rc = main(["--config-dir", sandbox.config_dir, "goal",
                   "--tool", "demo", "--task-file", task,
                   "--cwd", str(tmp_path), "--state-file", state_path])
    finally:
        holding.release()
    assert rc == 3


# --------------------------------------------------------------------- state


def test_the_state_file_is_valid_json_after_every_rotation(sandbox, tmp_path):
    adapter = FakeAdapter(usage={"s-1": 10}, rotate_session="auto")
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=0, max_sessions=2)
    driver._discover_session = lambda result, before, t0: "s-1"

    seen = []
    original_save = driver._save

    def save():
        original_save()
        with open(driver.state_path, encoding="utf-8") as stream:
            seen.append(json.load(stream))

    driver._save = save
    driver.run()

    assert seen, "state was never written"
    for payload in seen:
        assert payload["tool"] == "fake"
        assert isinstance(payload["sessions"], list)
        assert isinstance(payload["rotations"], list)
        assert payload["outcome"] in (
            "running", "completed", "stopped_max_sessions", "stopped_max_rounds",
            "failed", "killed_switch")
    assert any(payload["rotations"] for payload in seen)


def test_the_rotate_line_is_recorded_in_the_state(sandbox, tmp_path):
    from custos.core.thresholds import resolve_thresholds

    adapter = FakeAdapter(usage={})
    config = load_config(sandbox)
    expected = resolve_thresholds(config, None).rotate
    driver = GoalDriver(config, adapter, tool="fake",
                        task_file=write_task(tmp_path), cwd=str(tmp_path),
                        max_sessions=1, max_rounds_per_session=1,
                        emit=lambda event: None)
    driver._discover_session = lambda result, before, t0: "s-1"
    driver.run()
    assert read_state(tmp_path)["rotate_line"] == expected


def test_every_event_is_one_line_of_output(sandbox, tmp_path):
    adapter = FakeAdapter(usage={"s-1": 10})
    events = []
    driver = make_driver(sandbox, adapter, tmp_path, events=events, rotate_line=1000)
    driver._discover_session = lambda result, before, t0: "s-1"
    adapter.on_prompt = lambda a, prompt: setattr(
        a, "run_prompt",
        lambda prompt, timeout=240.0, cwd=None, extra_args=None: PromptResult(
            text="GOAL_COMPLETE", rc=0))

    driver.run()
    assert events[0]["event"] == "start"
    assert events[-1]["event"] == "outcome"
    for event in events:
        assert "ts" in event
        assert "rotate_line" in event


def test_goal_complete_inside_json_output_stops_the_run(sandbox, tmp_path):
    """`claude --output-format json` wraps the reply, marker included."""
    adapter = FakeAdapter(usage={"s-fresh": 10})
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=1000)
    driver._discover_session = lambda result, before, t0: "s-fresh"

    payload = json.dumps({"type": "result", "session_id": "s-fresh",
                          "result": "step done\nGOAL_COMPLETE"})
    adapter.on_prompt = lambda a, prompt: setattr(
        a, "run_prompt",
        lambda prompt, timeout=240.0, cwd=None, extra_args=None: PromptResult(
            text=payload, rc=0))

    driver.run()
    state = read_state(tmp_path)
    assert state["outcome"] == "completed"
    # One round only: the completion is noticed in the round that produced it.
    assert len(adapter.prompts) == 1


def test_the_lock_is_still_held_after_every_state_write(sandbox, tmp_path):
    """Writing state must not open a window for a second driver.

    The state write replaces the file the lock is on, so the driver re-takes the
    lock on the new file. If it released the old one first, a second driver
    could slip in between two rounds of this one.
    """
    adapter = FakeAdapter(usage={"s-1": 10}, rotate_session="auto")
    driver = make_driver(sandbox, adapter, tmp_path, rotate_line=0, max_sessions=1,
                         max_rounds_per_session=2)
    driver._discover_session = lambda result, before, t0: "s-1"

    observed = []
    original_save = driver._save

    def save():
        original_save()
        # While the driver is between rounds, someone else tries the lock.
        probe = FileLock(driver.state_path)
        observed.append(probe.acquire())
        probe.release()

    driver._save = save
    driver.run()
    assert observed and not any(observed)
