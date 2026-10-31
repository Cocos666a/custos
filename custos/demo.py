"""The built-in demo: the whole pipeline on synthetic sessions.

``custos demo`` exists so a person can see what the tool does before
pointing it at a real agent, and so a maintainer can check the wiring after a
change without installing four agent CLIs. It is deterministic: the same scripted
replies, the same seed, the same steps, every run.

The scenario walks the two paths that matter, in order:

1. **The recovery path.** A session sits at 92% of a 200k window, above the
   rotate line. The pipeline writes a handover, records an assisted rotation,
   and the gate then fails once and passes on the retry.
2. **The alert path.** The gate is scripted to answer wrongly from then on.
   After enough consecutive failures the degraded alert fires -- the case the
   whole feature exists for: a session that is answering, and answering badly.

Everything runs inside one temporary directory, which is removed at the end
unless ``--keep`` is given. No agent binary is executed, no real session is read,
and the config directory of the person running it is never touched.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from .adapters.demo import DEMO_TOOL, DemoAdapter, write_demo_session
from .core import config as config_module
from .core import gate_runner as gate_runner_module
from .core.gate import load_questions
from .core.notify import KIND_ALERT_DEGRADED, make_message
from .core.rotation import (
    RotationContext,
    do_rotation,
    start_hint_for,
)
from .core.state import EventLog, ReadingStore, State
from .core.thresholds import evaluate, resolve_thresholds
from .core.usage import Reading
from .core.util import FileLock, atomic_write_text

# The demo's numbers. Chosen so the trigger is unambiguous: 92% of the window is
# above the 90% rotate line and below the 93% the shipped defaults use for an
# absolute baseline -- the demo config states its own thresholds so the scenario
# does not depend on them.
DEMO_WINDOW = 200000
DEMO_TOKENS = 184000          # 92% of the window
DEMO_BASELINE_PERCENT = 90    # rotate line = 90% of window = 180000
DEMO_WARN_FACTOR = 0.85
DEMO_ROTATE_FACTOR = 0.90
DEMO_SESSION = "demo-session-0001"
DEMO_ALERT_AFTER = 3          # smaller than the default 5, so the demo stays short
DEMO_GATE_ATTEMPTS = 2        # per fire

# Both selection modes are demonstrated, one per batch, because "a bank of
# questions replaces a bank of one" is the claim the mode setting makes and a
# demo that only ever asked the same way would not show it:
#
#   batch 1  random, with a pinned seed, so the picked question is reproducible;
#   batch 2  sequence, walking the bank so consecutive rotations vary.
#
# The seed is part of the scenario. Without it the random batch would name a
# different question on every run, which is the one thing the demo cannot be:
# non-deterministic.
DEMO_GATE_MODE_RANDOM = "random"
DEMO_GATE_MODE_SEQUENCE = "sequence"
DEMO_GATE_SEED = 20261016

# The gate's scripted replies are derived per fire by ``_script_for_recovery``
# and ``_script_for_degradation`` rather than written out here: each entry is a
# directive ("right"/"wrong"/"invalid"), not literal text, so the scenario
# survives a change to the bank, the selection mode or the attempt limit.

DEMO_PREFIX = ("Do not use any tools. Do not read any files. "
               "Answer with the final answer only.")

# JSON, not YAML. The demo has to run on an interpreter with no optional
# dependencies at all, and a YAML config would refuse to load without PyYAML --
# the one command whose entire job is "show me this works" cannot be the command
# that needs an extra installed first.
DEMO_CONFIG: Dict[str, Any] = {
    "version": 1,
    "adapters": {
        # Every real adapter is off: the demo reads nothing outside its sandbox.
        "generic": {"enabled": False},
        "claude_code": {"enabled": False},
        "codex": {"enabled": False},
        "opencode": {"enabled": False},
        "pi": {"enabled": False},
    },
    "thresholds": {
        "baseline_tokens": None,
        "baseline_percent": DEMO_BASELINE_PERCENT,
        "warn_factor": DEMO_WARN_FACTOR,
        "rotate_factor": DEMO_ROTATE_FACTOR,
    },
    "monitor": {
        "interval_minutes": 5,
        "activity_quiet_minutes": 0,
        "min_retry_gap_seconds": 0,
        "pending_max_age_days": 3,
    },
    "rotation": {
        "mode": "assisted",
        "idempotency_hours": 6,
    },
    "gate": {
        "enabled": True,
        "questions": "questions.json",
        # Batch 1's mode. Batch 2 overrides it per fire through the gate runner.
        "mode": DEMO_GATE_MODE_RANDOM,
        "attempt_limit": DEMO_GATE_ATTEMPTS,
        "attempt_interval_seconds": 0,
        "timeout_seconds": 30,
        "alert_after_failures": DEMO_ALERT_AFTER,
        "alert_after_invalid": DEMO_ALERT_AFTER,
        "alert_repeat_seconds": 0,
        "prefix": DEMO_PREFIX,
    },
    "notify": {"stdout": False},
    "state": {"ledger_max_age_days": 45, "keep_readings_days": 7},
}

# Written in a form the demo can grade without PyYAML: JSON always loads, and
# the demo must run on a machine with no optional dependencies at all. Three
# questions rather than two, so `sequence` has something to walk between the two
# batches of the alert path.
DEMO_BANK = {
    "version": 1,
    "questions": [
        {
            "id": "consecutive-sum-87",
            "prompt": ("Three consecutive integers add up to 87. "
                       "What is the largest of the three?"),
            "answer": 30,
            "match": "last_number",
        },
        {
            "id": "lighter-ball-weighings",
            "prompt": ("You have 27 identical-looking balls. Exactly one is "
                       "lighter than the others. Using a balance scale, what is "
                       "the fewest number of weighings that guarantees finding "
                       "the lighter ball?"),
            "answer": 3,
            "match": "last_number",
        },
        {
            "id": "two-colour-draws",
            "prompt": ("A basket holds 5 red balls and 7 blue balls. You take "
                       "balls out without looking. What is the smallest number "
                       "you must take to be certain of having at least one ball "
                       "of each colour?"),
            "answer": 8,
            "match": "last_number",
        },
    ],
}


class Steps:
    """Collects the demo's narration and its machine-readable record.

    In ``--json`` mode the narration moves to stderr, because stdout has to be
    one parseable JSON document and nothing else. A caller piping the demo into
    ``jq`` should not have to strip prose first.
    """

    def __init__(self, quiet: bool = False) -> None:
        self.records: List[Dict[str, Any]] = []
        self.messages: List[str] = []
        self.quiet = quiet

    def _write(self, text: str) -> None:
        print(text, file=sys.stderr if self.quiet else sys.stdout)

    def step(self, name: str, detail: str, **fields: Any) -> Dict[str, Any]:
        record = {
            "step": name,
            "detail": detail,
            "at": round(time.time(), 3),
        }
        record.update(fields)
        self.records.append(record)
        self._write("%-14s %s" % (name, detail))
        return record

    def note(self, message: str) -> None:
        self.messages.append(message)
        self._write("")
        self._write(message)


def run(args: Any, config: Any) -> int:
    """Entry point used by ``custos demo``."""
    directory, temporary = _demo_directory(args)
    steps = Steps(quiet=bool(args.json))
    try:
        return _run_scenario(args, directory, steps)
    finally:
        if temporary and not args.keep:
            shutil.rmtree(directory, ignore_errors=True)
        elif temporary or args.keep:
            # Verify the directory is what it claims to be before announcing it.
            steps._write("")
            steps._write("demo directory kept: %s" % directory)


def _demo_directory(args: Any) -> Tuple[str, bool]:
    """The sandbox: ``--dir`` when given, otherwise a fresh temporary one."""
    if args.dir:
        path = os.path.abspath(os.path.expanduser(args.dir))
        os.makedirs(path, exist_ok=True)
        return path, False
    return tempfile.mkdtemp(prefix="custos-demo-"), True


def _run_scenario(args: Any, directory: str, steps: Steps) -> int:
    config_dir = os.path.join(directory, "config")
    work_dir = os.path.join(directory, "work")
    os.makedirs(config_dir, exist_ok=True)
    os.makedirs(work_dir, exist_ok=True)

    steps.step("sandbox", directory,
               config_dir=config_dir, work_dir=work_dir)

    atomic_write_text(os.path.join(config_dir, "config.json"),
                      json.dumps(DEMO_CONFIG, indent=2) + "\n")
    atomic_write_text(os.path.join(config_dir, "questions.json"),
                      json.dumps(DEMO_BANK, indent=1) + "\n")

    config = config_module.load_config(config_dir)
    steps.step("config", "written to %s" % config.source)

    state = State.load(config.state_path)
    events = EventLog(config.events_path, state)

    # -- 1. the synthetic session --------------------------------------
    session_path = write_demo_session(
        os.path.join(work_dir, "session.json"), DEMO_TOKENS, DEMO_WINDOW)
    reading = Reading(
        tool=DEMO_TOOL, session=DEMO_SESSION, tokens=DEMO_TOKENS,
        window=DEMO_WINDOW, source="external", ts=time.time() - 3600,
        detail={"demo": True})

    thresholds = resolve_thresholds(config, reading)
    decision = evaluate(reading, thresholds)
    steps.step(
        "reading",
        "%s %s at %s of %s tokens (%.1f%%), source=%s"
        % (DEMO_TOOL, DEMO_SESSION, DEMO_TOKENS, DEMO_WINDOW,
           reading.pct or 0.0, reading.source),
        tokens=reading.tokens, window=reading.window, pct=reading.pct,
        session_file=session_path)
    if decision is None:
        steps.step("evaluate", "no trigger; the demo cannot continue")
        return 1
    steps.step(
        "threshold",
        "baseline %s (%s); warn %s; rotate %s -> %s"
        % (thresholds.baseline.tokens, thresholds.baseline.source,
           thresholds.warn, thresholds.rotate, decision["level"]),
        reason=decision["reason"], level=decision["level"],
        pct_of_baseline=decision.get("pct_of_baseline"))
    if decision.get("level") != "rotate":
        steps.step("evaluate", "expected a rotate-level trigger, got %s"
                   % decision.get("level"))
        return 1

    # -- 2. the rotation ------------------------------------------------
    adapter = DemoAdapter(
        config, session=DEMO_SESSION, state_path=session_path,
        script=_script_for_recovery(), bank=DEMO_BANK["questions"])
    state_key = "%s:%s" % (DEMO_TOOL, DEMO_SESSION)
    ctx = RotationContext(
        session_key=state_key,
        state=state,
        events=events,
        activity_quiet_minutes=0,
        idempotency_hours=6.0,
        busy=False,
        mode="assisted",
        start_hint=start_hint_for(adapter),
    )

    gate = gate_runner_module.gate_runner(
        config, state, mode=DEMO_GATE_MODE_RANDOM, seed=DEMO_GATE_SEED)
    result = do_rotation(
        adapter, reading, config, ctx, decision=decision, dry_run=False,
        gate_runner=gate, run_gate_now=True)

    if result.deferred:
        steps.step("interlock", "deferred: %s" % "; ".join(result.defer_reasons))
        return 1
    steps.step(
        "interlock",
        "passed (idle, quiet window 0m, no earlier rotation)",
        busy=ctx.busy)
    steps.step(
        "handover",
        result.handover_path or "(not written)",
        exists=bool(result.handover_path and os.path.exists(result.handover_path)))
    steps.step(
        "rotate",
        "mode=%s rotated=%s" % (result.mode, result.rotated),
        mode=result.mode, rotated=result.rotated)
    if result.message:
        steps.note(result.message)

    # -- 3. the recovery path -------------------------------------------
    if result.gate is None:
        steps.step("gate", "no gate result; the demo cannot continue")
        return 1
    first = result.gate
    steps.step(
        "selection #1",
        "mode=%s seed=%s -> asked %s"
        % (first.get("mode"), (first.get("plan") or {}).get("seed"),
           ", ".join(first.get("question_ids") or []) or "(none)"),
        mode=first.get("mode"), seed=(first.get("plan") or {}).get("seed"),
        questions=list(first.get("question_ids") or []))
    steps.step(
        "gate #1",
        "%s (%s)" % (first.get("summary"), gate_runner_module.describe_streaks(
            _as_result(first))),
        passed=first.get("passed"), attempts=len(first.get("attempts") or []))
    for attempt in first.get("attempts") or []:
        steps.step(
            "  attempt %s" % attempt.get("attempt"),
            "%s extracted=%s %s" % (
                "pass" if attempt.get("verdict") == "pass" else (
                    "invalid" if not attempt.get("valid") else "fail"),
                attempt.get("extracted"),
                attempt.get("detail") or attempt.get("error") or ""))
    if not first.get("passed"):
        steps.step("gate #1", "expected a pass on retry; the demo cannot continue")
        return 1
    if len(first.get("attempts") or []) < 2:
        steps.step("gate #1", "expected a failure before the pass")
        return 1
    if first.get("mode") != DEMO_GATE_MODE_RANDOM:
        steps.step("selection #1", "expected mode=%s, got %s"
                   % (DEMO_GATE_MODE_RANDOM, first.get("mode")))
        return 1

    # -- 4. the alert path ----------------------------------------------
    # Rotate again with a session that is already wrong from the first attempt,
    # so the streak accumulates instead of being reset by a pass.
    adapter.script = ["wrong"]
    adapter._reply_index = 0
    second_reading = Reading(
        tool=DEMO_TOOL, session=DEMO_SESSION + "-b", tokens=DEMO_TOKENS,
        window=DEMO_WINDOW, source="external", ts=time.time() - 3600,
        detail={"demo": True})
    second_decision = evaluate(second_reading, resolve_thresholds(config, second_reading))
    state_key_b = "%s:%s" % (DEMO_TOOL, second_reading.session)
    ctx_b = RotationContext(
        session_key=state_key_b,
        state=state,
        events=events,
        activity_quiet_minutes=0,
        idempotency_hours=6.0,
        busy=False,
        mode="assisted",
        start_hint=start_hint_for(adapter),
    )
    result_b = do_rotation(
        adapter, second_reading, config, ctx_b, decision=second_decision,
        dry_run=False,
        # A gate runner is configured but not run now: this is what the monitor
        # does, because in assisted mode the replacement session does not exist
        # yet. The event stays pending and the fires below pick it up, which is
        # how a streak accumulates across scheduled runs.
        gate_runner=gate_runner_module.gate_runner(config, state),
        run_gate_now=False)
    steps.step(
        "rotate #2",
        "handover %s; gate left pending for later runs"
        % ("written" if result_b.handover_path else "NOT written"),
        handover=result_b.handover_path, pending=result_b.pending_event_key)
    if not result_b.pending_event_key:
        steps.step("rotate #2", "expected a pending gate event")
        return 1

    # Two fires of two attempts each: the alert threshold is reached inside the
    # second fire, which is what a scheduled deployment actually looks like.
    # Both fires use the second selection mode, so the carried-over path is the
    # one that shows `sequence` walking the bank.
    alerts: List[Dict[str, Any]] = []
    for fire in (1, 2):
        fire_result = gate_runner_module.run_gate_with_state(
            adapter, config, state, session_key=state_key_b,
            mode=DEMO_GATE_MODE_SEQUENCE)
        steps.step(
            "selection #2.%d" % fire,
            "mode=%s cursor=%s -> asked %s"
            % (fire_result.mode, state.gate_sequence(),
               ", ".join(fire_result.question_ids) or "(none)"),
            mode=fire_result.mode, cursor=state.gate_sequence(),
            questions=list(fire_result.question_ids))
        steps.step(
            "gate #2.%d" % fire,
            "%s (%s)" % (fire_result.summary(),
                         gate_runner_module.describe_streaks(fire_result)),
            passed=fire_result.passed,
            fail_streak=fire_result.failures_in_a_row,
            attempts=len(fire_result.attempts))
        fire_alerts = gate_runner_module.evaluate_alerts(
            state, state_key_b, second_reading, fire_result, config)
        alerts.extend(fire_alerts)
        for alert in fire_alerts:
            steps.note(alert.get("text", ""))

    if not any(alert.get("kind") == KIND_ALERT_DEGRADED for alert in alerts):
        steps.step(
            "alert",
            "no degraded alert fired; expected one after %d consecutive failures"
            % DEMO_ALERT_AFTER)
        return 1
    steps.step(
        "alert",
        "degraded alert emitted after %d consecutive wrong answers"
        % DEMO_ALERT_AFTER,
        kind=KIND_ALERT_DEGRADED)

    # The cursor moved between the two sequence fires, which is the property
    # that makes the mode worth having: the same bank, asked in order.
    if len({step.get("questions", [""])[0]
            for step in steps.records if str(step.get("step", "")).startswith("selection #2.")}) < 2:
        steps.step("selection #2", "expected the sequence cursor to advance "
                                   "between the two fires")
        return 1

    event = state.get_event(state_key_b) or {}
    steps.step(
        "pending",
        "event %s stays pending with %d recorded attempt(s) so the next "
        "scheduled run continues the streak"
        % (event.get("event_id"), len(event.get("attempts") or [])),
        attempts=len(event.get("attempts") or []), status=event.get("status"))

    # -- 5. wrap up ------------------------------------------------------
    state.save()
    handovers = sorted(
        name for name in os.listdir(config.handover_dir)
        if name.endswith(".md")) if os.path.isdir(config.handover_dir) else []
    steps.step(
        "files",
        "%d handover(s) in %s; state at %s; events at %s"
        % (len(handovers), config.handover_dir, config.state_path,
           config.events_path),
        handovers=[os.path.join(config.handover_dir, name) for name in handovers])

    if args.json:
        json.dump({
            "demo_dir": directory,
            "kept": bool(args.keep),
            "config_dir": config_dir,
            "handovers": [os.path.join(config.handover_dir, name) for name in handovers],
            "selection_modes": [DEMO_GATE_MODE_RANDOM, DEMO_GATE_MODE_SEQUENCE],
            "steps": steps.records,
            "messages": steps.messages,
        }, sys.stdout, ensure_ascii=False, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        steps._write("")
        steps._write(
            "demo finished: reading -> threshold -> interlock -> handover -> "
            "rotation -> gate (random, fail then pass) -> carried-over gate "
            "(sequence, alert)")
        if args.keep:
            steps._write("the directory above was kept; `rm -rf` it when you are done")
    return 0


def _script_for_recovery() -> List[str]:
    """One wrong answer, then a correct one: the retry path.

    Derived from the per-fire attempt limit rather than written out, so raising
    ``DEMO_GATE_ATTEMPTS`` keeps the scenario valid: the first attempt is always
    wrong and the second always right, whatever the limit is.
    """
    script = ["wrong"]
    script.extend(["right"] * max(1, DEMO_GATE_ATTEMPTS))
    return script


def _as_result(payload: Dict[str, Any]) -> Any:
    """Rebuild a GateResult from the serialised form the pipeline stores."""
    from .cli import _gate_result_from_dict

    return _gate_result_from_dict(payload)
