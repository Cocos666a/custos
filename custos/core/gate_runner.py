"""Gate orchestration: the part that runs *between* scheduled runs.

``core/gate.py`` knows how to ask one question and grade the answer. This module
knows the politics around it:

* whether to ask at all (``gate.enabled``, an adapter that can run prompts, a
  bank that exists);
* how long to wait between fires, so a five-minute schedule does not turn a
  one-minute retry interval into a hammer;
* when a run of failures becomes an alert, and how often that alert may repeat;
* how to leave the event pending so the next fire continues the same streak
  instead of starting from zero.

The distinction that matters throughout is **failed** versus **invalid**. A
session that answers "29" five times is degraded: it is responding, and it is
wrong. A session that times out five times is an infrastructure problem: the
one-shot command is broken, the model is not reachable, the tool needs a login.
Those produce different alerts and different advice, so they are counted
separately.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence

from .gate import (
    DEFAULT_ATTEMPT_TIMEOUT,
    DEFAULT_ATTEMPTS,
    DEFAULT_FIRE_BUDGET_SECONDS,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_PREFIX,
    VERDICT_FAIL,
    VERDICT_INVALID,
    GateResult,
    load_questions,
    run_gate,
)
from .notify import (
    KIND_ALERT_DEGRADED,
    KIND_ALERT_INFRA,
    KIND_GATE,
    format_degraded_alert,
    format_gate_result,
    format_infra_alert,
    make_message,
)
from .rotation import record_attempts, recent_attempts
from .state import State
from .usage import Reading
from .util import CustosError, format_age, log, truncate

# Defaults, mirroring core/config.py. Duplicated as constants so the module is
# usable without a config object (the demo and the tests both do that).
DEFAULT_ALERT_AFTER_FAILURES = 5
DEFAULT_ALERT_AFTER_INVALID = 5
DEFAULT_ALERT_REPEAT_SECONDS = 3600.0
DEFAULT_MIN_RETRY_GAP_SECONDS = 120.0


class BankUnavailable(CustosError):
    """The question bank is missing or unreadable."""


def gate_settings(config: Any) -> Dict[str, Any]:
    """Every gate setting resolved once, with the defaults filled in."""
    if config is None:
        return {
            "enabled": True,
            "questions_path": "",
            "mode": "random",
            "attempts": DEFAULT_ATTEMPTS,
            "interval_seconds": DEFAULT_INTERVAL_SECONDS,
            "attempt_timeout": DEFAULT_ATTEMPT_TIMEOUT,
            "fire_budget_seconds": DEFAULT_FIRE_BUDGET_SECONDS,
            "prefix": DEFAULT_PREFIX,
            "alert_after_failures": DEFAULT_ALERT_AFTER_FAILURES,
            "alert_after_invalid": DEFAULT_ALERT_AFTER_INVALID,
            "alert_repeat_seconds": DEFAULT_ALERT_REPEAT_SECONDS,
            "min_retry_gap_seconds": DEFAULT_MIN_RETRY_GAP_SECONDS,
        }
    return {
        "enabled": config.get_bool("gate", "enabled", True),
        "questions_path": config.questions_path,
        "mode": str(config.get("gate", "mode", default="random")),
        "attempts": max(1, config.get_int("gate", "attempt_limit", DEFAULT_ATTEMPTS)),
        "interval_seconds": max(0.0, config.get_float(
            "gate", "attempt_interval_seconds", DEFAULT_INTERVAL_SECONDS)),
        "attempt_timeout": max(1.0, config.get_float(
            "gate", "timeout_seconds", DEFAULT_ATTEMPT_TIMEOUT)),
        "fire_budget_seconds": max(1.0, config.get_float(
            "monitor", "fire_budget_seconds", DEFAULT_FIRE_BUDGET_SECONDS)),
        "prefix": config.get("gate", "prefix", default=DEFAULT_PREFIX) or DEFAULT_PREFIX,
        "alert_after_failures": max(1, config.get_int(
            "gate", "alert_after_failures", DEFAULT_ALERT_AFTER_FAILURES)),
        "alert_after_invalid": max(1, config.get_int(
            "gate", "alert_after_invalid", DEFAULT_ALERT_AFTER_INVALID)),
        "alert_repeat_seconds": max(0.0, config.get_float(
            "gate", "alert_repeat_seconds", DEFAULT_ALERT_REPEAT_SECONDS)),
        "min_retry_gap_seconds": max(0.0, config.get_float(
            "monitor", "min_retry_gap_seconds", DEFAULT_MIN_RETRY_GAP_SECONDS)),
    }


def load_bank(settings: Dict[str, Any]) -> List[Any]:
    """Load the configured bank, raising :class:`BankUnavailable` when it is not."""
    path = settings.get("questions_path") or ""
    if not path:
        raise BankUnavailable("no question bank is configured")
    try:
        return load_questions(path)
    except CustosError as exc:
        raise BankUnavailable(str(exc))


def run_gate_with_state(
    adapter: Any,
    config: Any,
    state: State,
    *,
    session_key: str = "",
    prefix: Optional[str] = None,
    mode: Optional[str] = None,
    attempts: Optional[int] = None,
    interval_seconds: Optional[float] = None,
    attempt_timeout: Optional[float] = None,
    fire_budget_seconds: Optional[float] = None,
    seed: Optional[int] = None,
    dry_run: bool = False,
    sleep: Any = None,
    on_attempt: Any = None,
    questions: Optional[Sequence[Any]] = None,
) -> GateResult:
    """Run one gate fire, keeping its bookkeeping in ``state``.

    This is the single entry point used by ``custos gate``, ``monitor`` and
    ``demo``, so the sequence cursor, the attempt counter and the per-session
    history behave the same everywhere. Callers that want a one-off run with no
    state simply pass a throwaway :class:`~custos.core.state.State`.
    """
    settings = gate_settings(config)

    if not settings["enabled"] and not dry_run:
        result = GateResult(mode=mode or settings["mode"])
        result.skipped_reason = "the gate is disabled in configuration (gate.enabled)"
        return result

    if questions is None:
        try:
            questions = load_bank(settings)
        except BankUnavailable as exc:
            result = GateResult(mode=mode or settings["mode"])
            result.skipped_reason = "no usable question bank: %s" % exc
            return result

    # An adapter the user turned off does not get to run a subprocess. The
    # generic adapter reports `run_prompt: True` whenever a command template is
    # configured, and a pending gate left over from before that change would
    # otherwise keep invoking it.
    if not dry_run and not _adapter_enabled(config, adapter):
        result = GateResult(mode=mode or settings["mode"])
        result.skipped_reason = (
            "adapter %s is disabled in configuration"
            % getattr(adapter, "name", "?"))
        return result

    # The keys here are the ones `core.gate` reads and writes; a mismatch is
    # silent in both directions -- the picker would start from zero every fire
    # and the caller would write the unchanged value back, so `mode: sequence`
    # would quietly ask the same question forever.
    selection_state = {
        "gate_sequence": state.gate_sequence(),
        "attempts_total": state.gate_attempts_total(),
    }
    prior = recent_attempts(state, session_key) if session_key else []

    result = run_gate(
        adapter,
        questions,
        prefix=prefix if prefix is not None else settings["prefix"],
        mode=mode or settings["mode"],
        attempts=attempts if attempts is not None else settings["attempts"],
        interval_seconds=(
            interval_seconds if interval_seconds is not None else settings["interval_seconds"]),
        attempt_timeout=(
            attempt_timeout if attempt_timeout is not None else settings["attempt_timeout"]),
        fire_budget_seconds=(
            fire_budget_seconds if fire_budget_seconds is not None
            else settings["fire_budget_seconds"]),
        on_attempt=on_attempt,
        selection_state=selection_state,
        prior_attempts=prior,
        seed=seed,
        dry_run=dry_run,
        sleep=sleep,
    )

    if not dry_run:
        state.set_gate_sequence(
            selection_state.get("gate_sequence", state.gate_sequence()))
        state.note_gate_attempts(result.attempts_total)
        if session_key and result.attempts:
            record_attempts(state, session_key, result.attempts)
    return result


def gate_runner(
    config: Any,
    state: State,
    *,
    dry_run: bool = False,
    mode: Optional[str] = None,
    seed: Optional[int] = None,
) -> Any:
    """Build the callable that :func:`core.rotation.do_rotation` expects.

    The signature is ``(adapter, config, ctx, result, handover_path)`` and the
    return value is a :class:`GateResult`. Wiring it as a closure keeps the
    pipeline free of gate bookkeeping: rotation says "check this", and this
    module decides how.

    ``mode`` and ``seed`` override the configuration for the rotations this
    runner serves. They exist for deterministic callers -- the built-in demo
    pins both so its two batches show a different selection strategy each --
    and are left unset everywhere a real deployment runs, where the configured
    mode and the attempt-count-derived seed are the point.
    """

    def runner(adapter: Any, _config: Any, ctx: Any, rotation_result: Any,
               handover_path: Optional[str]) -> GateResult:
        started = time.time()
        outcome = run_gate_with_state(
            adapter,
            config,
            state,
            session_key=ctx.session_key,
            dry_run=dry_run,
            mode=mode,
            seed=seed,
        )
        outcome.plan.setdefault("handover", handover_path)
        outcome.plan.setdefault("elapsed_seconds", round(time.time() - started, 3))
        return outcome

    return runner


def _adapter_enabled(config: Any, adapter: Any) -> bool:
    """Whether configuration allows this adapter to do anything.

    Configurations without an ``enabled`` key at all (the built-in defaults
    always have one, but a hand-written config may not) are treated as enabled:
    the absence of a switch is not a switch turned off.
    """
    if config is None:
        return True
    name = getattr(adapter, "name", "")
    if not name:
        return True
    section = config.adapter(name)
    if not section:
        return True
    return bool(section.get("enabled", True))


def attempt_message(attempt: Any, tool: str = "") -> Dict[str, Any]:
    """A message object for one attempt, used by the standalone ``gate`` command."""
    verdict = "pass" if getattr(attempt, "verdict", "") == "pass" else (
        "invalid" if not getattr(attempt, "valid", False) else "fail")
    text = format_gate_result(
        getattr(attempt, "question_id", "?"),
        verdict,
        getattr(attempt, "extracted", None),
        getattr(attempt, "attempt", 0),
        getattr(attempt, "detail", "") or getattr(attempt, "error", ""),
    )
    return make_message(KIND_GATE, text, tool=tool, verdict=verdict,
                        attempt=getattr(attempt, "attempt", 0))


def gate_message(result: GateResult, reading: Optional[Reading] = None,
                 tool: str = "") -> Dict[str, Any]:
    """A one-line summary message for a finished gate fire."""
    if result.dry_run:
        text = "[custos] gate dry run\n  would ask: %s\n  attempts: %s" % (
            ", ".join(result.question_ids) or "(none)",
            result.plan.get("attempts", "?"))
    elif not result.ran:
        text = "[custos] gate skipped\n  %s" % (result.skipped_reason or "unknown reason")
    elif result.passed:
        text = "[custos] gate passed on attempt %d (%s)" % (
            result.attempts_total, ", ".join(result.question_ids))
    else:
        text = "[custos] gate failed after %d attempt(s) (%s)" % (
            result.attempts_total, ", ".join(result.question_ids))
    return make_message(KIND_GATE, text, tool=tool, passed=result.passed,
                        summary_text=result.summary())


def evaluate_alerts(
    state: State,
    session_key: str,
    reading: Reading,
    result: GateResult,
    config: Any,
) -> List[Dict[str, Any]]:
    """Turn a finished gate fire into zero or more alert messages.

    Called after every fire, passed or failed, because a pass is also news: it
    clears a streak that was already alerted on, and the person who received the
    earlier alert should hear that it is over.
    """
    settings = gate_settings(config)
    messages: List[Dict[str, Any]] = []
    repeat = settings["alert_repeat_seconds"]

    if result.passed:
        if _was_alerted(state, session_key):
            history = len(recent_attempts(state, session_key))
            messages.append(make_message(
                KIND_GATE,
                "[custos] gate recovered\n"
                "  session: %s %s\n"
                "  the check now passes (attempt %d of this fire, %d recorded in total)\n"
                "  no further action is needed." % (
                    reading.tool, reading.session[:12] or "-",
                    result.attempts_total, history),
                tool=reading.tool, recovered=True))
            _clear_alerted(state, session_key)
        return messages

    if not result.ran:
        return messages

    failures = result.failures_in_a_row
    invalid = result.invalid_in_a_row

    # Failures take precedence: a session that answers wrongly is the case this
    # feature exists for, and reporting "cannot reach the model" instead would
    # point the reader at the wrong problem.
    if failures >= settings["alert_after_failures"]:
        if state.alert_due(session_key, "degraded", repeat):
            state.mark_alert(session_key, "degraded")
            _mark_alerted(state, session_key)
            last = result.last()
            messages.append(make_message(
                KIND_ALERT_DEGRADED,
                format_degraded_alert(
                    reading, failures, len(recent_attempts(state, session_key)),
                    getattr(last, "raw_head", "") or getattr(last, "error", ""),
                    handover_path=(state.get_event(session_key) or {}).get("handover"),
                    retry_seconds=int(settings["min_retry_gap_seconds"])),
                tool=reading.tool, streak=failures, alert="degraded"))
            return messages
        log("[custos] degraded alert for %s suppressed (repeats every %ds)"
            % (session_key, int(repeat)))
        return messages

    if invalid >= settings["alert_after_invalid"]:
        if state.alert_due(session_key, "infra", repeat):
            state.mark_alert(session_key, "infra")
            _mark_alerted(state, session_key)
            last = result.last()
            messages.append(make_message(
                KIND_ALERT_INFRA,
                format_infra_alert(
                    reading, invalid,
                    getattr(last, "error", "") or getattr(last, "stderr_head", ""),
                    retry_seconds=int(settings["min_retry_gap_seconds"])),
                tool=reading.tool, streak=invalid, alert="infra"))
            return messages
        log("[custos] infrastructure alert for %s suppressed (repeats every %ds)"
            % (session_key, int(repeat)))
    return messages


def retry_allowed(state: State, session_key: str, config: Any) -> bool:
    """Whether enough time has passed since the last attempt for another fire.

    The scheduler's interval and the retry gap are different numbers on purpose.
    A five-minute schedule with a two-minute retry gap means a failure is retried
    every five minutes, not twelve times a minute; a person who wants faster
    retries lowers the schedule interval.
    """
    settings = gate_settings(config)
    gap = settings["min_retry_gap_seconds"]
    if gap <= 0:
        return True
    event = state.get_event(session_key) or {}
    last = float(event.get("last_attempt_epoch") or state.last_gate_attempt() or 0.0)
    if not last:
        return True
    waited = time.time() - last
    if waited >= gap:
        return True
    log("[custos] gate for %s deferred: last attempt %s (gap %ds)"
        % (session_key, format_age(last), int(gap)))
    return False


def _alert_flag(state: State, session_key: str) -> str:
    return "alerted:%s" % session_key


def _was_alerted(state: State, session_key: str) -> bool:
    return bool(state.get_meta(_alert_flag(state, session_key), False))


def _mark_alerted(state: State, session_key: str) -> None:
    state.set_meta(_alert_flag(state, session_key), True)


def _clear_alerted(state: State, session_key: str) -> None:
    state.set_meta(_alert_flag(state, session_key), False)


def describe_streaks(result: GateResult) -> str:
    """Short phrase for diagnostics: what the tail of the attempt list looks like."""
    bits = []
    if result.failures_in_a_row:
        bits.append("%d fail in a row" % result.failures_in_a_row)
    if result.invalid_in_a_row:
        bits.append("%d invalid in a row" % result.invalid_in_a_row)
    if not bits:
        return "no streak"
    return ", ".join(bits)


def verdict_of(attempt: Any) -> str:
    """Normalise an attempt's verdict for counting."""
    if not getattr(attempt, "valid", False):
        return VERDICT_INVALID
    return VERDICT_FAIL if getattr(attempt, "verdict", "") != "pass" else "pass"


def truncate_detail(text: str, limit: int = 200) -> str:
    return truncate(text, limit)
