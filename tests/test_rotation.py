"""The rotation interlocks and the pipeline around them.

The cheap failure of this tool is rotating a session that is still working; the
expensive one is rotating the same session twice. Both are guarded here, and the
guards are the part of the design that is easy to weaken by accident -- an
interlock that silently stops objecting looks exactly like one that had nothing
to object to.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from custos.core.rotation import (
    REASON_ACTIVE,
    REASON_ALREADY_HANDLED,
    REASON_ALREADY_ROTATED,
    REASON_BUSY,
    REASON_DISABLED,
    REASON_GATE_PENDING,
    REASON_NO_SESSION,
    REASON_UNREADABLE,
    RotationContext,
    check_interlocks,
    check_pending_gate,
    do_rotation,
    rotation_key,
    start_hint_for,
)
from custos.core.state import EVENT_PENDING, EventLog, State


class StandInAdapter:
    """An adapter that records what it was asked to do."""

    name = "synthetic"

    def __init__(self, rotated=False, error=None, detail=None) -> None:
        self.rotations = []
        self._rotated = rotated
        self._error = error
        self._detail = detail or {}

    def rotate(self, session, handover_path, dry_run=False, config=None):
        self.rotations.append({"session": session, "handover": handover_path,
                               "dry_run": dry_run})

        class Outcome:
            pass

        outcome = Outcome()
        outcome.mode = "full-auto" if self._rotated else "assisted"
        outcome.rotated = self._rotated
        outcome.new_session = "session-new" if self._rotated else None
        outcome.detail = dict(self._detail)
        if self._rotated:
            outcome.detail.setdefault("next", "the adapter started session-new")
        else:
            outcome.detail.setdefault(
                "next", "start a new session and read %s" % handover_path)
        outcome.error = self._error
        return outcome

    def start_hint(self, handover_path=None):
        return "start a new session and read %s" % handover_path


class ExplodingAdapter(StandInAdapter):
    def rotate(self, session, handover_path, dry_run=False, config=None):
        raise RuntimeError("the adapter is broken")


def make_config(sandbox, **overrides):
    from custos.core import config as config_module

    payload = {"version": 1}
    payload.update(overrides)
    sandbox.write_config(payload)
    return config_module.load_config(sandbox.config_dir)


def make_reading(session="session-1", tokens=184000, ts=None, **kwargs):
    from custos.core.usage import Reading

    kwargs.setdefault("tool", "synthetic")
    kwargs.setdefault("window", 200000)
    kwargs.setdefault("source", "transcript")
    # Quiet by default: most tests here are about something other than the
    # activity window, and a reading taken "now" would trip that interlock first.
    # The tests that are about it pass an explicit timestamp.
    if ts is None:
        ts = time.time() - 3600
    return Reading(session=session, tokens=tokens, ts=ts, **kwargs)


def make_context(state, session_key="synthetic:session-1", **kwargs):
    kwargs.setdefault("activity_quiet_minutes", 10)
    kwargs.setdefault("idempotency_hours", 6.0)
    kwargs.setdefault("busy", False)
    return RotationContext(session_key=session_key, state=state, **kwargs)


# ------------------------------------------------------------------ interlocks


def test_a_quiet_idle_session_passes_the_interlocks(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    reading = make_reading(ts=time.time() - 3600)
    assert check_interlocks(reading, make_context(state)) == []


def test_a_recent_reading_defers_on_the_activity_window(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    reading = make_reading(ts=time.time() - 60)
    reasons = check_interlocks(reading, make_context(state, activity_quiet_minutes=10))
    assert len(reasons) == 1
    assert reasons[0].startswith(REASON_ACTIVE)


def test_the_activity_window_lapses(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    reading = make_reading(ts=time.time() - 3600)
    assert check_interlocks(reading, make_context(state, activity_quiet_minutes=10)) == []


def test_a_session_already_rotated_is_never_rotated_again(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.record_rotation("synthetic:session-1", "session-1")
    reasons = check_interlocks(make_reading(), make_context(state))
    assert any(reason.startswith(REASON_ALREADY_ROTATED) for reason in reasons)


def test_an_assisted_rotation_blocks_a_repeat_within_the_idempotency_window(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.record_handled("synthetic:session-1", "session-1")
    reasons = check_interlocks(make_reading(), make_context(state))
    assert any(reason.startswith(REASON_ALREADY_HANDLED) for reason in reasons)
    assert "repeats after 6.0h" in reasons[0]


def test_the_idempotency_guard_expires(tmp_path):
    """A session left at the line must be raised again, not forgotten."""
    state = State.load(str(tmp_path / "state.json"))
    state.record_handled("synthetic:session-1", "session-1")
    state.history["synthetic:session-1"]["last_handled_epoch"] = time.time() - 7 * 3600
    assert check_interlocks(make_reading(), make_context(state)) == []


def test_an_interlock_passes_once_the_session_id_changes(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.record_rotation("synthetic:session-1", "session-old")
    assert check_interlocks(make_reading(session="session-new"),
                            make_context(state)) == []


def test_a_busy_session_defers(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    reasons = check_interlocks(
        make_reading(), make_context(state, busy=True, busy_detail="a turn is in flight"))
    assert any(reason.startswith(REASON_BUSY) for reason in reasons)
    assert "a turn is in flight" in reasons[0]


def test_an_unknown_turn_state_fails_closed_for_an_adapter_that_can_tell(tmp_path):
    """Cannot-confirm is not the same as confirmed-idle."""
    state = State.load(str(tmp_path / "state.json"))
    reasons = check_interlocks(
        make_reading(), make_context(state, busy=None, busy_detection=True))
    assert any(reason.startswith(REASON_BUSY) for reason in reasons)


def test_an_adapter_that_cannot_detect_busy_state_does_not_defer_on_it(tmp_path):
    """Failing closed on a check that does not exist would be a dead feature."""
    state = State.load(str(tmp_path / "state.json"))
    assert check_interlocks(
        make_reading(), make_context(state, busy=None, busy_detection=False)) == []


def test_an_unreadable_store_fails_closed(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    reasons = check_interlocks(make_reading(), make_context(state, store_readable=False))
    assert any(reason.startswith(REASON_UNREADABLE) for reason in reasons)


def test_a_missing_reading_defers(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    reasons = check_interlocks(None, make_context(state))
    assert reasons == ["%s: no reading available" % REASON_NO_SESSION]


def test_a_reading_without_a_session_id_defers(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    reading = make_reading(session="")
    assert check_interlocks(reading, make_context(state)) == [REASON_NO_SESSION]


def test_several_interlocks_object_at_once(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.record_handled("synthetic:session-1", "session-1")
    reading = make_reading(ts=time.time())
    reasons = check_interlocks(reading, make_context(state, busy=True))
    assert len(reasons) == 3
    assert any(REASON_BUSY in reason for reason in reasons)
    assert any(REASON_ACTIVE in reason for reason in reasons)
    assert any(REASON_ALREADY_HANDLED in reason for reason in reasons)


def test_a_pending_gate_defers(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.put_event("synthetic:session-1", {
        "event_id": "rot-20261016-120000-0001", "status": EVENT_PENDING,
        "created_epoch": time.time(), "attempts": [{}, {}]})
    reasons = check_pending_gate(make_context(state))
    assert len(reasons) == 1
    assert reasons[0].startswith(REASON_GATE_PENDING)
    assert "2 attempt(s) so far" in reasons[0]


def test_no_pending_gate_means_no_objection(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    assert check_pending_gate(make_context(state)) == []


def test_rotation_key_names_the_reason(tmp_path):
    assert rotation_key("t", "s", "near_line") == "rotate:t:s:near_line"


# ------------------------------------------------------------------- pipeline


def test_a_rotation_writes_the_snapshot_before_touching_the_session(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    events = EventLog(config.events_path, state)
    adapter = StandInAdapter()

    reading = make_reading()
    result = do_rotation(adapter, reading, config, make_context(state, events=events))

    assert result.handover_path and os.path.isfile(result.handover_path)
    assert adapter.rotations[0]["handover"] == result.handover_path
    assert result.mode == "assisted"
    assert result.rotated is False


def test_an_assisted_rotation_is_recorded_so_the_next_pass_is_quiet(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    adapter = StandInAdapter()
    reading = make_reading()

    do_rotation(adapter, reading, config, make_context(state))
    assert state.last_handled("synthetic:session-1")["session"] == "session-1"
    assert state.seen(rotation_key("synthetic", "session-1", "manual"))

    second = do_rotation(adapter, reading, config, make_context(state))
    assert second.deferred
    assert any(REASON_ALREADY_HANDLED in reason for reason in second.defer_reasons)
    assert len(adapter.rotations) == 1


def test_a_full_auto_rotation_records_the_new_session(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    adapter = StandInAdapter(rotated=True)
    result = do_rotation(adapter, make_reading(), config, make_context(state))
    assert result.rotated is True
    assert result.new_session == "session-new"
    assert state.history["synthetic:session-1"]["last_new_session"] == "session-new"


def test_force_relaxes_the_interlocks_but_not_the_kill_switch(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    adapter = StandInAdapter()
    reading = make_reading(ts=time.time())

    forced = do_rotation(adapter, reading, config, make_context(state), force=True)
    assert not forced.deferred
    assert len(adapter.rotations) == 1

    open(config.disable_path, "w", encoding="utf-8").close()
    killed = do_rotation(adapter, reading, config, make_context(state), force=True)
    assert killed.deferred
    assert killed.defer_reasons[0].startswith(REASON_DISABLED)
    assert len(adapter.rotations) == 1


def test_the_kill_switch_defers_before_anything_else(sandbox):
    config = make_config(sandbox)
    open(config.disable_path, "w", encoding="utf-8").close()
    state = State.load(config.state_path)
    adapter = StandInAdapter()
    result = do_rotation(adapter, make_reading(), config, make_context(state))
    assert result.deferred
    assert result.defer_reasons == ["%s: %s exists" % (REASON_DISABLED, config.disable_path)]
    assert adapter.rotations == []


def test_a_dry_run_writes_nothing_at_all(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    events = EventLog(config.events_path, state)
    adapter = StandInAdapter()

    result = do_rotation(adapter, make_reading(), config,
                         make_context(state, events=events), dry_run=True)
    assert result.dry_run
    assert result.handover_path is None
    assert "would be written" in result.message
    assert adapter.rotations == []
    # Not even the lock, the state file or the audit log.
    assert sandbox.tree() == ["config.json"]


def test_a_dry_run_still_reports_a_deferral(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    result = do_rotation(StandInAdapter(), make_reading(), config,
                         make_context(state, busy=True), dry_run=True)
    assert result.deferred
    assert sandbox.tree() == ["config.json"]


def test_a_rotation_failure_is_recorded_and_reported(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    events = EventLog(config.events_path, state)
    adapter = StandInAdapter(error="the tool refused")

    result = do_rotation(adapter, make_reading(), config,
                         make_context(state, events=events))
    assert result.error == "the tool refused"
    assert result.pending_event_key is None
    assert "failed" in result.message
    # The snapshot was still written: the work stays recoverable.
    assert os.path.isfile(result.handover_path)
    assert state.history["synthetic:session-1"]["fail_count"] == 1


def test_an_adapter_that_raises_does_not_kill_the_run(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    result = do_rotation(ExplodingAdapter(), make_reading(), config,
                         make_context(state))
    assert result.error is not None
    assert "RuntimeError" in result.error
    assert os.path.isfile(result.handover_path)


def test_repeated_failures_stop_after_the_limit(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    events = EventLog(config.events_path, state)
    adapter = StandInAdapter(error="still broken")

    for _ in range(3):
        ctx = make_context(state, events=events)
        do_rotation(adapter, make_reading(), config, ctx, force=True)

    kinds = [record["kind"] for record in events.tail()]
    assert kinds.count("rotate_failed") == 3
    assert kinds.count("rotation_giving_up") == 1
    record = [item for item in events.tail()
              if item["kind"] == "rotation_giving_up"][0]
    assert record["consecutive_failures"] == 3


def test_a_successful_rotation_clears_the_failure_count(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    state.note_failure("synthetic:session-1", "earlier problem")
    do_rotation(StandInAdapter(rotated=True), make_reading(), config,
                make_context(state))
    assert state.history["synthetic:session-1"]["fail_count"] == 0


# ------------------------------------------------------------- pending gate


def test_a_pending_event_is_written_before_the_gate_runs(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    seen = {}

    def gate_runner(adapter, config, ctx, result, handover_path):
        seen["event"] = dict(state.get_event(ctx.session_key) or {})
        seen["handover"] = handover_path

        class Outcome:
            passed = True

            def summary(self):
                return "passed on attempt 1"

            def to_dict(self):
                return {"passed": True}

        return Outcome()

    result = do_rotation(StandInAdapter(), make_reading(), config,
                         make_context(state), gate_runner=gate_runner)
    # Snapshot and event first, gate second: a crash inside the gate must not
    # lose the fact that this session was rotated.
    assert seen["event"]["status"] == EVENT_PENDING
    assert seen["event"]["handover"] == result.handover_path
    # A pass resolves it, so nothing is left waiting.
    assert result.pending_event_key is None
    assert state.get_event("synthetic:session-1")["resolution"] == "gate_passed"


def test_a_gate_left_for_later_stays_pending(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    result = do_rotation(StandInAdapter(), make_reading(), config,
                         make_context(state), gate_runner=lambda *a, **k: None,
                         run_gate_now=False)
    assert result.pending_event_key == "synthetic:session-1"
    event = state.get_event("synthetic:session-1")
    assert event["status"] == EVENT_PENDING
    assert event["attempts"] == []
    assert "pending" in result.message


def test_a_gate_that_cannot_ask_leaves_the_event_pending(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)

    def skipped_gate(adapter, config, ctx, result, handover_path):
        class Outcome:
            ran = False
            passed = False
            skipped_reason = "no usable question bank"

            def summary(self):
                return "skipped"

            def to_dict(self):
                return {"ran": False, "passed": False,
                        "skipped_reason": self.skipped_reason}

        return Outcome()

    result = do_rotation(StandInAdapter(), make_reading(), config,
                         make_context(state), gate_runner=skipped_gate)
    assert result.pending_event_key == "synthetic:session-1"
    assert result.gate_summary == "skipped (no usable question bank)"


def test_a_gate_runner_that_raises_does_not_abort_the_rotation(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)

    def exploding_gate(*args, **kwargs):
        raise RuntimeError("the gate runner is broken")

    result = do_rotation(StandInAdapter(), make_reading(), config,
                         make_context(state), gate_runner=exploding_gate)
    assert result.error is not None
    assert "gate runner raised" in result.error
    assert os.path.isfile(result.handover_path)


def test_no_gate_runner_records_a_skip_rather_than_pretending(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    result = do_rotation(StandInAdapter(), make_reading(), config, make_context(state))
    assert result.pending_event_key is None
    assert result.gate_summary == ""
    assert state.get_event("synthetic:session-1")["status"] == "recorded"


# ---------------------------------------------------------------- audit trail


def test_the_event_log_records_the_rotation(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    events = EventLog(config.events_path, state)
    do_rotation(StandInAdapter(), make_reading(), config, make_context(state, events=events))

    tail = events.tail()
    assert [record["kind"] for record in tail] == ["rotate_assisted"]
    assert tail[-1]["tool"] == "synthetic"
    assert tail[-1]["mode"] == "assisted"


def test_a_deferral_is_recorded_with_its_reasons(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    events = EventLog(config.events_path, state)
    do_rotation(StandInAdapter(), make_reading(ts=time.time()), config,
                make_context(state, events=events, busy=True))

    record = events.tail()[-1]
    assert record["kind"] == "defer"
    assert any(REASON_BUSY in reason for reason in record["defer_reasons"])
    assert record["handover"] is None


def test_a_dry_run_writes_no_audit_record(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    events = EventLog(config.events_path, state)
    do_rotation(StandInAdapter(), make_reading(), config,
                make_context(state, events=events), dry_run=True)
    assert events.tail() == []
    assert not os.path.exists(config.events_path)


# ------------------------------------------------------------------ start hint


def test_start_hint_comes_from_the_adapter(sandbox):
    config = make_config(sandbox)
    hint = start_hint_for(StandInAdapter(), "/tmp/handover.md")
    assert "start a new session" in hint


def test_start_hint_falls_back_when_the_adapter_raises():
    class Broken:
        name = "broken"

        def rotate(self, *args, **kwargs):
            raise RuntimeError("nope")

    assert start_hint_for(Broken()) == ""


def test_the_pipeline_result_serialises(sandbox):
    config = make_config(sandbox)
    state = State.load(config.state_path)
    payload = do_rotation(StandInAdapter(), make_reading(), config,
                          make_context(state)).to_dict()
    assert json.loads(json.dumps(payload))["session_key"] == "synthetic:session-1"
