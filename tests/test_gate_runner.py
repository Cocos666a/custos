"""The gate runner: retries, throttling, streaks and the two kinds of alert.

The distinction this file exists to protect is **failed** versus **invalid**. A
session that answers "29" five times is degraded: it is responding, and it is
wrong. A session that times out five times is broken infrastructure. Those need
different alerts and different advice, and a change that collapsed them would
still look like it was working.
"""

from __future__ import annotations

import time

import pytest

from custos.core import gate_runner as gate_runner_module
from custos.core.gate import GateAttempt, GateResult, Question
from custos.core.notify import (
    KIND_ALERT_DEGRADED,
    KIND_ALERT_INFRA,
    KIND_GATE,
)
from custos.core.rotation import recent_attempts, record_attempts
from custos.core.state import EVENT_PENDING, State

SESSION_KEY = "synthetic:session-1"


class ScriptedAdapter:
    """Replies from a script; the last entry repeats."""

    name = "synthetic"

    def __init__(self, replies=("42",), run_prompt=True) -> None:
        self.replies = list(replies)
        self.prompts = []
        self._index = 0
        self._run_prompt = run_prompt

    def run_prompt(self, prompt, timeout=240.0, cwd=None, extra_args=None):
        self.prompts.append(prompt)
        reply = self.replies[min(self._index, len(self.replies) - 1)]
        self._index += 1

        class Outcome:
            pass

        outcome = Outcome()
        if isinstance(reply, tuple) and reply and reply[0] is None:
            outcome.text, outcome.rc, outcome.timed_out = "", -1, True
            outcome.stderr = "no answer"
        else:
            outcome.text = str(reply)
            outcome.rc = 0
            outcome.timed_out = False
            outcome.stderr = ""
        return outcome

    def capabilities(self):
        return {"run_prompt": self._run_prompt, "tool": self.name}

    def one_shot_command(self):
        return ["synthetic", "--print"]


def make_config(sandbox, questions=None, **gate):
    from custos.core import config as config_module

    sandbox.write_questions({"questions": questions or [
        {"id": "q1", "prompt": "What is the sum of 17 and 25?",
         "answer": 42, "match": "last_number"},
        {"id": "q2", "prompt": "How many letters are in the word \"synthetic\"?",
         "answer": 9, "match": "last_number"},
    ]})
    payload = {"version": 1, "gate": dict({
        "enabled": True,
        "questions": "questions.json",
        "mode": "sequence",
        "attempt_limit": 2,
        "attempt_interval_seconds": 0,
        "timeout_seconds": 5,
        "alert_after_failures": 3,
        "alert_after_invalid": 3,
        "alert_repeat_seconds": 3600,
    }, **gate)}
    sandbox.write_config(payload)
    return config_module.load_config(sandbox.config_dir)


def make_state(sandbox):
    return State.load(sandbox.path("state.json"))


def make_reading(session="session-1"):
    from custos.core.usage import Reading

    return Reading(tool="synthetic", session=session, tokens=184000,
                   window=200000, source="transcript", ts=time.time() - 3600)


def pending_event(state, session_key=SESSION_KEY):
    state.put_event(session_key, {
        "event_id": "rot-20261016-120000-0001", "status": EVENT_PENDING,
        "session_key": session_key, "tool": "synthetic", "old_session": "session-1",
        "created_epoch": time.time(), "attempts": [], "alerts": {},
    })


# ------------------------------------------------------------------- retries


def test_run_gate_with_state_uses_the_configured_attempt_limit(sandbox):
    config = make_config(sandbox, attempt_limit=3)
    state = make_state(sandbox)
    adapter = ScriptedAdapter(["41", "40", "39"])
    result = gate_runner_module.run_gate_with_state(
        adapter, config, state, session_key=SESSION_KEY, sleep=lambda _s: None)
    assert result.attempts_total == 3
    assert not result.passed
    assert len(adapter.prompts) == 3


def test_run_gate_with_state_stops_at_the_first_pass(sandbox):
    config = make_config(sandbox, attempt_limit=5)
    state = make_state(sandbox)
    adapter = ScriptedAdapter(["41", "42", "43"])
    result = gate_runner_module.run_gate_with_state(
        adapter, config, state, session_key=SESSION_KEY, sleep=lambda _s: None)
    assert result.passed
    assert result.attempts_total == 2


def test_an_explicit_override_wins_over_the_configuration(sandbox):
    config = make_config(sandbox, attempt_limit=5, mode="sequence")
    state = make_state(sandbox)
    result = gate_runner_module.run_gate_with_state(
        ScriptedAdapter(["41"]), config, state, session_key=SESSION_KEY,
        attempts=1, mode="all", sleep=lambda _s: None)
    assert result.attempts_total == 1
    assert result.mode == "all"
    assert result.question_ids == ["q1", "q2"]


def test_the_sequence_cursor_advances_across_fires(sandbox):
    """The defect this guards against asked the same question forever."""
    config = make_config(sandbox, mode="sequence", attempt_limit=1)
    state = make_state(sandbox)
    asked = []
    for _ in range(3):
        result = gate_runner_module.run_gate_with_state(
            ScriptedAdapter(["42"]), config, state, session_key=SESSION_KEY,
            sleep=lambda _s: None)
        asked.append(result.question_ids[0])
        state.save()
        state = State.load(sandbox.path("state.json"))
    assert asked == ["q1", "q2", "q1"]
    assert state.gate_sequence() == 1


def test_attempts_are_recorded_on_the_pending_event(sandbox):
    config = make_config(sandbox, attempt_limit=2)
    state = make_state(sandbox)
    pending_event(state)
    gate_runner_module.run_gate_with_state(
        ScriptedAdapter(["41", "40"]), config, state, session_key=SESSION_KEY,
        sleep=lambda _s: None)
    assert len(recent_attempts(state, SESSION_KEY)) == 2


def test_a_streak_accumulates_across_fires(sandbox):
    config = make_config(sandbox, attempt_limit=2)
    state = make_state(sandbox)
    pending_event(state)
    first = gate_runner_module.run_gate_with_state(
        ScriptedAdapter(["41"]), config, state, session_key=SESSION_KEY,
        sleep=lambda _s: None)
    second = gate_runner_module.run_gate_with_state(
        ScriptedAdapter(["40"]), config, state, session_key=SESSION_KEY,
        sleep=lambda _s: None)
    assert first.failures_in_a_row == 2
    assert second.failures_in_a_row == 4


def test_a_pass_clears_the_streak(sandbox):
    # One question, so the reply can be right for the same question the first
    # fire answered wrongly -- a bank of two would move the cursor between them.
    config = make_config(sandbox, attempt_limit=1, mode="sequence",
                         questions=[{"id": "q1", "prompt": "What is the sum of 17 and 25?",
                                     "answer": 42, "match": "last_number"}])
    state = make_state(sandbox)
    pending_event(state)
    wrong = gate_runner_module.run_gate_with_state(
        ScriptedAdapter(["41"]), config, state, session_key=SESSION_KEY,
        sleep=lambda _s: None)
    recovered = gate_runner_module.run_gate_with_state(
        ScriptedAdapter(["42"]), config, state, session_key=SESSION_KEY,
        sleep=lambda _s: None)
    assert wrong.failures_in_a_row == 1
    assert recovered.passed
    assert recovered.failures_in_a_row == 0


def test_a_dry_run_does_not_touch_the_counters(sandbox):
    config = make_config(sandbox, attempt_limit=2)
    state = make_state(sandbox)
    result = gate_runner_module.run_gate_with_state(
        ScriptedAdapter(["42"]), config, state, session_key=SESSION_KEY, dry_run=True)
    assert result.dry_run
    assert state.gate_attempts_total() == 0
    assert state.gate_sequence() == 0


# --------------------------------------------------------- skipping the gate


def test_a_disabled_gate_is_skipped_not_run(sandbox):
    config = make_config(sandbox, enabled=False)
    state = make_state(sandbox)
    adapter = ScriptedAdapter(["42"])
    result = gate_runner_module.run_gate_with_state(
        adapter, config, state, session_key=SESSION_KEY)
    assert not result.ran
    assert "disabled in configuration" in result.skipped_reason
    assert adapter.prompts == []


def test_a_missing_bank_is_reported_rather_than_raised(sandbox):
    config = make_config(sandbox)
    state = make_state(sandbox)
    # Point the config at a bank that does not exist.
    config.data["gate"]["questions"] = "absent.json"
    result = gate_runner_module.run_gate_with_state(
        ScriptedAdapter(["42"]), config, state, session_key=SESSION_KEY)
    assert not result.ran
    assert "no usable question bank" in result.skipped_reason


def test_an_adapter_disabled_in_configuration_does_not_run_a_subprocess(sandbox):
    config = make_config(sandbox)
    config.data.setdefault("adapters", {})["synthetic"] = {"enabled": False}
    state = make_state(sandbox)
    adapter = ScriptedAdapter(["42"])
    result = gate_runner_module.run_gate_with_state(
        adapter, config, state, session_key=SESSION_KEY)
    assert not result.ran
    assert "disabled in configuration" in result.skipped_reason
    assert adapter.prompts == []


def test_gate_settings_fall_back_to_defaults_without_a_config():
    settings = gate_runner_module.gate_settings(None)
    assert settings["enabled"] is True
    assert settings["mode"] == "random"
    assert settings["attempts"] == gate_runner_module.DEFAULT_ATTEMPTS
    assert settings["alert_after_failures"] == 5


# -------------------------------------------------------------- retry gap


def test_a_second_fire_inside_the_gap_is_deferred(sandbox):
    config = make_config(sandbox, attempt_limit=1)
    config.data["monitor"]["min_retry_gap_seconds"] = 600
    state = make_state(sandbox)
    pending_event(state)
    gate_runner_module.run_gate_with_state(
        ScriptedAdapter(["41"]), config, state, session_key=SESSION_KEY,
        sleep=lambda _s: None)
    assert gate_runner_module.retry_allowed(state, SESSION_KEY, config) is False


def test_the_retry_gap_lapses(sandbox):
    config = make_config(sandbox, attempt_limit=1)
    config.data["monitor"]["min_retry_gap_seconds"] = 600
    state = make_state(sandbox)
    pending_event(state)
    gate_runner_module.run_gate_with_state(
        ScriptedAdapter(["41"]), config, state, session_key=SESSION_KEY,
        sleep=lambda _s: None)
    # The event's own timestamp is what the interlock reads when it is present.
    state.get_event(SESSION_KEY)["last_attempt_epoch"] = time.time() - 700
    assert gate_runner_module.retry_allowed(state, SESSION_KEY, config) is True


def test_the_retry_gap_falls_back_to_the_global_attempt_time(sandbox):
    """With no event for the session, the last attempt anywhere is the clock."""
    config = make_config(sandbox, attempt_limit=1)
    config.data["monitor"]["min_retry_gap_seconds"] = 600
    state = make_state(sandbox)
    pending_event(state, session_key="synthetic:other")
    gate_runner_module.run_gate_with_state(
        ScriptedAdapter(["41"]), config, state, session_key="synthetic:other",
        sleep=lambda _s: None)
    assert gate_runner_module.retry_allowed(state, SESSION_KEY, config) is False


def test_a_zero_gap_always_allows_a_retry(sandbox):
    config = make_config(sandbox)
    config.data["monitor"]["min_retry_gap_seconds"] = 0
    assert gate_runner_module.retry_allowed(make_state(sandbox), SESSION_KEY, config)


def test_a_session_that_never_ran_is_allowed_to_run(sandbox):
    config = make_config(sandbox)
    state = make_state(sandbox)
    assert gate_runner_module.retry_allowed(state, SESSION_KEY, config) is True


# ------------------------------------------------------------------- alerts


def result_with(failures=0, invalid=0, passed=False, ran=True):
    result = GateResult(ran=ran, passed=passed, mode="sequence")
    result.failures_in_a_row = failures
    result.invalid_in_a_row = invalid
    if ran and not passed:
        attempt = GateAttempt(attempt=1, question_id="q1", valid=bool(failures),
                              verdict="fail" if failures else "invalid",
                              raw_head="answer: 41", error="" if failures else "empty reply")
        result.attempts = [attempt]
    return result


def test_a_failure_streak_raises_the_degraded_alert(sandbox):
    config = make_config(sandbox, alert_after_failures=3)
    state = make_state(sandbox)
    pending_event(state)
    messages = gate_runner_module.evaluate_alerts(
        state, SESSION_KEY, make_reading(), result_with(failures=3), config)
    assert len(messages) == 1
    assert messages[0]["kind"] == KIND_ALERT_DEGRADED
    assert messages[0]["streak"] == 3
    assert "failed the check 3 times in a row" in messages[0]["text"]


def test_a_streak_below_the_threshold_is_silent(sandbox):
    config = make_config(sandbox, alert_after_failures=5)
    state = make_state(sandbox)
    pending_event(state)
    assert gate_runner_module.evaluate_alerts(
        state, SESSION_KEY, make_reading(), result_with(failures=4), config) == []


def test_an_invalid_streak_raises_the_infrastructure_alert(sandbox):
    config = make_config(sandbox, alert_after_invalid=2)
    state = make_state(sandbox)
    pending_event(state)
    messages = gate_runner_module.evaluate_alerts(
        state, SESSION_KEY, make_reading(), result_with(invalid=2), config)
    assert len(messages) == 1
    assert messages[0]["kind"] == KIND_ALERT_INFRA
    assert "produced no usable answer" in messages[0]["text"]
    # The advice points at the command, not at the model: an unusable reply is
    # an invocation problem.
    assert "one-shot command" in messages[0]["text"]


def test_failures_take_precedence_over_invalidity(sandbox):
    """A session that answers wrongly is the case this feature exists for."""
    config = make_config(sandbox, alert_after_failures=2, alert_after_invalid=2)
    state = make_state(sandbox)
    pending_event(state)
    messages = gate_runner_module.evaluate_alerts(
        state, SESSION_KEY, make_reading(), result_with(failures=2, invalid=5), config)
    assert [message["kind"] for message in messages] == [KIND_ALERT_DEGRADED]


def test_the_same_alert_is_not_repeated_inside_the_window(sandbox):
    config = make_config(sandbox, alert_after_failures=2, alert_repeat_seconds=3600)
    state = make_state(sandbox)
    pending_event(state)
    first = gate_runner_module.evaluate_alerts(
        state, SESSION_KEY, make_reading(), result_with(failures=2), config)
    second = gate_runner_module.evaluate_alerts(
        state, SESSION_KEY, make_reading(), result_with(failures=3), config)
    assert len(first) == 1
    assert second == []
    assert state.alerts(SESSION_KEY)["degraded_count"] == 1


def test_the_alert_repeats_once_the_window_lapses(sandbox):
    config = make_config(sandbox, alert_after_failures=2, alert_repeat_seconds=3600)
    state = make_state(sandbox)
    pending_event(state)
    gate_runner_module.evaluate_alerts(
        state, SESSION_KEY, make_reading(), result_with(failures=2), config)
    state.alerts(SESSION_KEY)["degraded_last_epoch"] = time.time() - 7200
    again = gate_runner_module.evaluate_alerts(
        state, SESSION_KEY, make_reading(), result_with(failures=3), config)
    assert len(again) == 1
    assert state.alerts(SESSION_KEY)["degraded_count"] == 2


def test_a_recovery_is_reported_once(sandbox):
    config = make_config(sandbox, alert_after_failures=2)
    state = make_state(sandbox)
    pending_event(state)
    gate_runner_module.evaluate_alerts(
        state, SESSION_KEY, make_reading(), result_with(failures=2), config)

    recovered = gate_runner_module.evaluate_alerts(
        state, SESSION_KEY, make_reading(), result_with(passed=True), config)
    assert len(recovered) == 1
    assert recovered[0]["kind"] == KIND_GATE
    assert "gate recovered" in recovered[0]["text"]

    quiet = gate_runner_module.evaluate_alerts(
        state, SESSION_KEY, make_reading(), result_with(passed=True), config)
    assert quiet == []


def test_a_pass_without_an_earlier_alert_says_nothing(sandbox):
    config = make_config(sandbox)
    state = make_state(sandbox)
    pending_event(state)
    assert gate_runner_module.evaluate_alerts(
        state, SESSION_KEY, make_reading(), result_with(passed=True), config) == []


def test_a_skipped_gate_produces_no_alert(sandbox):
    config = make_config(sandbox)
    state = make_state(sandbox)
    pending_event(state)
    skipped = GateResult(ran=False, skipped_reason="no bank")
    assert gate_runner_module.evaluate_alerts(
        state, SESSION_KEY, make_reading(), skipped, config) == []


# ------------------------------------------------------------------ messages


def test_attempt_message_reports_the_verdict(sandbox):
    attempt = GateAttempt(attempt=2, question_id="q1", valid=True, verdict="fail",
                          extracted=41, detail="expected 42, last number was 41")
    message = gate_runner_module.attempt_message(attempt, tool="synthetic")
    assert message["kind"] == KIND_GATE
    assert message["verdict"] == "fail"
    assert message["attempt"] == 2
    assert "41" in message["text"]


def test_attempt_message_marks_an_unusable_reply_as_invalid(sandbox):
    attempt = GateAttempt(attempt=1, question_id="q1", valid=False, verdict="invalid",
                          error="empty reply")
    assert gate_runner_module.attempt_message(attempt)["verdict"] == "invalid"


def test_gate_message_summarises_a_pass(sandbox):
    result = result_with(passed=True)
    result.attempts = [GateAttempt(attempt=1, valid=True, verdict="pass")]
    message = gate_runner_module.gate_message(result, tool="synthetic")
    assert "gate passed on attempt 1" in message["text"]


def test_gate_message_summarises_a_dry_run(sandbox):
    result = GateResult(mode="sequence", dry_run=True)
    result.question_ids = ["q1"]
    result.plan = {"attempts": 2}
    assert "gate dry run" in gate_runner_module.gate_message(result)["text"]


def test_gate_message_summarises_a_skip(sandbox):
    message = gate_runner_module.gate_message(
        GateResult(ran=False, skipped_reason="no bank configured"))
    assert "gate skipped" in message["text"]


# ------------------------------------------------------------------ helpers


def test_describe_streaks_names_both_kinds(sandbox):
    result = result_with(failures=2, invalid=1)
    assert gate_runner_module.describe_streaks(result) == "2 fail in a row, 1 invalid in a row"


def test_describe_streaks_on_a_clean_history(sandbox):
    assert gate_runner_module.describe_streaks(result_with()) == "no streak"


def test_verdict_of_normalises_an_attempt():
    assert gate_runner_module.verdict_of(GateAttempt(valid=False)) == "invalid"
    assert gate_runner_module.verdict_of(GateAttempt(valid=True, verdict="fail")) == "fail"
    assert gate_runner_module.verdict_of(GateAttempt(valid=True, verdict="pass")) == "pass"


# ------------------------------------------------------------------- wiring


def test_gate_runner_closure_passes_the_mode_and_seed_through(sandbox):
    from custos.core.rotation import RotationContext

    config = make_config(sandbox, mode="random", attempt_limit=1)
    state = make_state(sandbox)
    runner = gate_runner_module.gate_runner(
        config, state, mode="sequence", seed=7)
    ctx = RotationContext(session_key=SESSION_KEY, state=state)
    outcome = runner(ScriptedAdapter(["42"]), config, ctx, None, "/tmp/handover.md")
    assert outcome.mode == "sequence"
    assert outcome.plan["seed"] == 7
    assert outcome.plan["handover"] == "/tmp/handover.md"
    assert outcome.question_ids == ["q1"]


def test_record_attempts_keeps_the_order_and_the_last_time(sandbox):
    state = make_state(sandbox)
    pending_event(state)
    record_attempts(state, SESSION_KEY, [
        GateAttempt(attempt=1, verdict="fail", valid=True),
        GateAttempt(attempt=2, verdict="pass", valid=True),
    ])
    stored = recent_attempts(state, SESSION_KEY)
    assert [item["attempt"] for item in stored] == [1, 2]
    assert state.get_event(SESSION_KEY)["last_attempt_epoch"] > 0


def test_record_attempts_on_a_session_with_no_event_is_a_no_op(sandbox):
    state = make_state(sandbox)
    record_attempts(state, "synthetic:absent", [GateAttempt(attempt=1)])
    assert recent_attempts(state, "synthetic:absent") == []


def test_question_bank_is_loaded_from_the_configured_path(sandbox):
    config = make_config(sandbox)
    settings = gate_runner_module.gate_settings(config)
    questions = gate_runner_module.load_bank(settings)
    assert [question.id for question in questions] == ["q1", "q2"]


def test_an_unconfigured_bank_raises_bank_unavailable(sandbox):
    settings = gate_runner_module.gate_settings(None)
    with pytest.raises(gate_runner_module.BankUnavailable):
        gate_runner_module.load_bank(settings)
