"""The rotation pipeline.

Order matters here. Asked to rotate a session, the pipeline:

1. checks the interlocks, and defers when any of them objects;
2. writes the handover snapshot **before** touching the session, so a failure
   mid-rotation still leaves the work recoverable;
3. asks the adapter to rotate;
4. records the outcome in the event log and the idempotency history.

Interlocks exist because the cheap failure mode of this tool is rotating a
session that is still working. The expensive failure mode is rotating one twice.
Both are guarded here:

* **kill switch** -- a ``DISABLE`` file in the config directory stops everything.
* **activity window** -- a reading newer than ``activity_quiet_minutes`` means
  the session is live; wait.
* **idempotency** -- the id most recently rotated for a session key is never
  rotated again, and the guard expires so a session that stays full is raised
  again rather than being forgotten.
* **fail closed** -- when an adapter cannot tell us whether the session is busy,
  the pipeline defers rather than guessing.

What happens *after* a rotation is deliberately not part of it. Starting the new
session and checking that it is healthy takes minutes and several scheduled
runs; the rotation itself must not block on that. So the rotation leaves a
**pending event** behind, and the gate keeps working on it from the next run
onwards. The event carries the attempt history, which is what turns "failed
again" into "failed five times in a row".

Everything is dry-run capable: ``dry_run=True`` performs every read, computes
every decision and reports what it *would* do, without writing a snapshot or
touching the session.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .handover import write_handover
from .notify import format_error, format_rotation
from .state import EVENT_PENDING, EventLog, State
from .thresholds import describe_trigger
from .usage import Reading
from .util import format_age, log

DEFAULT_ACTIVITY_QUIET_MINUTES = 10
MAX_CONSECUTIVE_FAILURES = 3

# Interlock reason codes, so a message can be grouped and tested without
# matching on prose.
REASON_DISABLED = "kill_switch"
REASON_ACTIVE = "recent_activity"
REASON_ALREADY_ROTATED = "already_rotated"
REASON_ALREADY_HANDLED = "already_handled"
REASON_BUSY = "session_busy"
REASON_UNREADABLE = "state_unreadable"
REASON_NO_SESSION = "no_session_id"
REASON_GATE_PENDING = "gate_pending"

# How long the "already acted on this session" guard lasts. Long enough that a
# scheduled run does not repeat itself, short enough that a session left at the
# rotate line is raised again the same working day.
DEFAULT_IDEMPOTENCY_HOURS = 6.0


@dataclass
class RotationContext:
    """Facts the pipeline needs that only the caller can supply."""

    session_key: str
    state: State
    events: Optional[EventLog] = None
    activity_quiet_minutes: int = DEFAULT_ACTIVITY_QUIET_MINUTES
    idempotency_hours: float = DEFAULT_IDEMPOTENCY_HOURS
    busy: Optional[bool] = None
    """``True`` when a turn is in flight, ``False`` when idle, ``None`` unknown.

    How ``None`` is read depends on ``busy_detection``:

    * ``busy_detection=True`` and ``busy is None`` -- the adapter can tell and
      the check did not answer, so **fail closed** and defer;
    * ``busy_detection=False`` -- the adapter cannot tell at all, so there is
      nothing to be careful about and the activity quiet window carries the
      load. Treating this as "busy" would defer forever, which is not caution
      but a dead feature.
    """
    busy_detail: str = ""
    busy_detection: bool = False
    store_readable: bool = True
    mode: str = "assisted"
    start_hint: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RotationResult:
    """What the pipeline did (or would have done)."""

    session_key: str
    reading: Optional[Reading]
    decision: Optional[Dict[str, Any]]
    deferred: bool = False
    defer_reasons: List[str] = field(default_factory=list)
    handover_path: Optional[str] = None
    rotated: bool = False
    new_session: Optional[str] = None
    mode: str = "assisted"
    dry_run: bool = False
    error: Optional[str] = None
    message: str = ""
    adapter_detail: Dict[str, Any] = field(default_factory=dict)
    gate_summary: str = ""
    gate: Optional[Dict[str, Any]] = None
    pending_event_key: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_key": self.session_key,
            "reading": self.reading.to_dict() if self.reading else None,
            "decision_reason": (self.decision or {}).get("reason"),
            "deferred": self.deferred,
            "defer_reasons": self.defer_reasons,
            "handover": self.handover_path,
            "rotated": self.rotated,
            "new_session": self.new_session,
            "mode": self.mode,
            "dry_run": self.dry_run,
            "error": self.error,
            "adapter": self.adapter_detail,
            "gate": self.gate,
            "gate_summary": self.gate_summary,
            "pending_event_key": self.pending_event_key,
        }


def check_interlocks(
    reading: Optional[Reading],
    ctx: RotationContext,
    now: Optional[float] = None,
) -> List[str]:
    """Return the reasons to defer; an empty list means rotation may proceed."""
    now = now or time.time()
    reasons: List[str] = []

    if not ctx.store_readable:
        # Fail closed: without a readable store we cannot verify activity or
        # idempotency, so the safe answer is "do nothing this round".
        reasons.append("%s: cannot verify session state" % REASON_UNREADABLE)

    if reading is None:
        reasons.append("%s: no reading available" % REASON_NO_SESSION)
        return reasons

    if not reading.session:
        reasons.append(REASON_NO_SESSION)
        return reasons

    if ctx.busy is True:
        reasons.append("%s: %s" % (REASON_BUSY, ctx.busy_detail or "adapter reports a turn in flight"))
    elif ctx.busy is None and ctx.busy_detection:
        reasons.append("%s: adapter could not confirm the session is idle" % REASON_BUSY)

    quiet_seconds = max(0, ctx.activity_quiet_minutes) * 60
    if reading.ts and (now - reading.ts) < quiet_seconds:
        reasons.append("%s: last reading %s (quiet window %dm)" % (
            REASON_ACTIVE, format_age(reading.ts, now), ctx.activity_quiet_minutes))

    last_rotated = ctx.state.last_rotated_session(ctx.session_key)
    if last_rotated and last_rotated == reading.session:
        reasons.append("%s: session %s was already rotated" % (
            REASON_ALREADY_ROTATED, reading.session[:12]))
        return reasons

    # Idempotency expiry. A session that was acted on once stayed at the rotate
    # line, because a session only shrinks when the tool compacts it or the user
    # starts a new one. Without an expiry the tool would go permanently quiet on
    # exactly the session it was built to watch, so the guard lapses after
    # `idempotency_hours` and the warning is repeated.
    handled = ctx.state.last_handled(ctx.session_key)
    if handled and handled["session"] == reading.session:
        age_hours = (now - handled["epoch"]) / 3600.0 if handled["epoch"] else 0.0
        if age_hours < max(0.0, ctx.idempotency_hours):
            reasons.append("%s: handled %.1fh ago (repeats after %.1fh)" % (
                REASON_ALREADY_HANDLED, age_hours, ctx.idempotency_hours))

    return reasons


def check_pending_gate(ctx: RotationContext) -> List[str]:
    """Defer while a rotation for this session is still waiting on a gate.

    Separate from :func:`check_interlocks` because the caller may want to look at
    the pending event itself (to run the gate) rather than just skip. Rotating
    again while the previous rotation is unverified would stack sessions nobody
    has checked.
    """
    event = ctx.state.pending_for_session(ctx.session_key)
    if event is None:
        return []
    return ["%s: rotation %s is waiting on a gate check (%d attempt(s) so far)" % (
        REASON_GATE_PENDING,
        str(event.get("event_id") or "?")[-8:],
        len(event.get("attempts") or []),
    )]


def rotation_key(tool: str, session: str, reason: str) -> str:
    """Ledger key for a rotation action."""
    return "rotate:%s:%s:%s" % (tool, session, reason)


def new_event_id(reading: Reading) -> str:
    """Stable-ish identifier for one rotation event, for logs and messages."""
    from .util import now_stamp

    return "rot-%s-%s" % (now_stamp(), (reading.session or "session")[-6:])


def do_rotation(
    adapter: Any,
    reading: Reading,
    config: Any,
    ctx: RotationContext,
    decision: Optional[Dict[str, Any]] = None,
    dry_run: bool = False,
    gate_runner: Optional[Callable[..., Any]] = None,
    recent_messages: Optional[List[str]] = None,
    recent_unavailable_reason: Optional[str] = None,
    run_gate_now: bool = True,
    force: bool = False,
) -> RotationResult:
    """Run the rotation pipeline for one session.

    ``gate_runner`` is called after a successful rotation with the freshly
    written handover path; it returns an object with a ``summary()`` and a
    ``to_dict()`` (see :class:`~custos.core.gate.GateResult`). When it is
    ``None`` the check is recorded as skipped rather than pretended.

    ``run_gate_now`` is ``False`` when the caller wants the rotation recorded and
    the gate left for a later run -- which is what the monitor does, because the
    new session does not exist yet in assisted mode.
    """
    result = RotationResult(
        session_key=ctx.session_key,
        reading=reading,
        decision=decision,
        mode=ctx.mode,
        dry_run=dry_run,
    )

    if config is not None and getattr(config, "disabled", False):
        result.deferred = True
        result.defer_reasons = ["%s: %s exists" % (REASON_DISABLED, config.disable_path)]
        _record(ctx, "defer", result)
        return result

    # `force` relaxes the interlocks a person may deliberately override -- the
    # quiet window, the idempotency guards -- and keeps the two that protect the
    # session itself: the kill switch above, and the single-instance lock, which
    # is held by the caller.
    reasons = [] if force else check_interlocks(reading, ctx)
    if not force:
        reasons.extend(check_pending_gate(ctx))
    if reasons:
        result.deferred = True
        result.defer_reasons = reasons
        _record(ctx, "defer", result)
        return result

    trigger = describe_trigger(decision) if decision else "manual"

    if dry_run:
        result.handover_path = None
        result.mode = ctx.mode
        tentative = _tentative_handover_path(config, reading)
        result.message = format_rotation(
            reading, decision or {}, tentative, rotated=False, mode=ctx.mode,
            start_hint=ctx.start_hint,
            gate="would run: %s" % ("yes" if gate_runner else "no runner configured"))
        result.adapter_detail["dry_run"] = "no snapshot written, adapter not called"
        _record(ctx, "dry_run", result)
        return result

    # Snapshot first: even if the adapter then fails, the work is recoverable.
    handover_path: Optional[str] = None
    try:
        directory = config.handover_dir if config is not None else "."
        handover_path = write_handover(
            directory,
            reading=reading,
            decision=decision,
            trigger_reason=trigger,
            recent_messages=recent_messages,
            recent_unavailable_reason=recent_unavailable_reason,
            extra={
                "session key": ctx.session_key,
                "rotation mode": ctx.mode,
                "gate": "pending" if gate_runner is not None else "skipped (no runner)",
                **ctx.extra,
            },
        )
        result.handover_path = handover_path
    except OSError as exc:
        result.error = "handover write failed: %s" % exc
        _record(ctx, "handover_failed", result)
        return result

    try:
        rotate_result = adapter.rotate(
            reading.session, handover_path, dry_run=False, config=config)
    except Exception as exc:  # an adapter bug must not kill the run
        result.error = "%s.rotate raised %s: %s" % (
            getattr(adapter, "name", "adapter"), type(exc).__name__, exc)
        _record(ctx, "rotate_failed", result)
        _note_failure(ctx, result.error)
        return result

    result.adapter_detail = getattr(rotate_result, "detail", {}) or {}
    result.rotated = bool(getattr(rotate_result, "rotated", False))
    result.new_session = getattr(rotate_result, "new_session", None)
    result.mode = getattr(rotate_result, "mode", ctx.mode)
    error = getattr(rotate_result, "error", None)
    if error:
        result.error = error

    event_key = ctx.session_key
    if result.rotated:
        ctx.state.record_rotation(
            ctx.session_key, reading.session, result.new_session, result.adapter_detail)
        ctx.state.clear_failures(ctx.session_key)
        ctx.state.mark(rotation_key(reading.tool, reading.session, trigger), trigger)
    elif error:
        _note_failure(ctx, error)
        _record(ctx, "rotate_failed", result)
        result.message = format_rotation(
            reading, decision or {}, handover_path, False, None, result.mode,
            start_hint=ctx.start_hint, gate="not run: the rotation failed")
        return result
    else:
        # Assisted rotation: the snapshot is written and the user has been told,
        # but the new session is theirs to start. Recording it here is what stops
        # the next scheduled run from repeating the same message.
        ctx.state.record_handled(ctx.session_key, reading.session, result.adapter_detail)
        ctx.state.mark(rotation_key(reading.tool, reading.session, trigger), trigger)

    # The event is what the gate will attach its attempts to, so it is written
    # before the gate runs: a crash inside the gate must not lose the fact that
    # this session was rotated.
    ctx.state.put_event(event_key, {
        "event_id": new_event_id(reading),
        "session_key": ctx.session_key,
        "tool": reading.tool,
        "old_session": reading.session,
        "new_session": result.new_session,
        "reading_tokens": reading.tokens,
        "trigger": trigger,
        "handover": handover_path,
        "mode": result.mode,
        "rotated": result.rotated,
        "status": EVENT_PENDING if gate_runner is not None else "recorded",
        "created_at": _now(),
        "created_epoch": time.time(),
        "attempts": [],
        "alerts": {},
    })
    result.pending_event_key = event_key if gate_runner is not None else None

    if gate_runner is not None and run_gate_now:
        gate_result = _run_gate_safely(gate_runner, adapter, config, ctx, result, handover_path)
        if gate_result is not None:
            result.gate = gate_result.to_dict()
            result.gate_summary = gate_result.summary()
            if getattr(gate_result, "passed", False):
                ctx.state.resolve_event(event_key, "gate_passed")
                result.pending_event_key = None
            elif not getattr(gate_result, "ran", False):
                # Nothing was actually asked: record the skip and leave the event
                # pending so a later run can try when the reason has gone away.
                result.gate_summary = "skipped (%s)" % (
                    getattr(gate_result, "skipped_reason", "") or "unknown reason")

    result.message = format_rotation(
        reading, decision or {}, handover_path, result.rotated,
        result.new_session, result.mode, start_hint=ctx.start_hint,
        gate=result.gate_summary or ("pending" if result.pending_event_key else "skipped"))
    _record(ctx, "rotate" if result.rotated else "rotate_assisted", result)
    return result


def _run_gate_safely(
    gate_runner: Callable[..., Any],
    adapter: Any,
    config: Any,
    ctx: RotationContext,
    result: RotationResult,
    handover_path: Optional[str],
) -> Any:
    """Call the gate runner, turning any failure into a recorded error.

    A gate that raises is an infrastructure problem; letting it escape would
    abort a run that has already rotated a session successfully.
    """
    try:
        return gate_runner(adapter, config, ctx, result, handover_path)
    except Exception as exc:
        log("[custos] gate runner raised %s: %s" % (type(exc).__name__, exc))
        result.error = result.error or ("gate runner raised %s: %s" % (
            type(exc).__name__, exc))
        return None


def _tentative_handover_path(config: Any, reading: Reading) -> Optional[str]:
    """Where a snapshot would land, without writing it (dry-run message)."""
    import os

    from .handover import handover_filename

    try:
        directory = config.handover_dir if config is not None else "."
        name = handover_filename(reading.tool, reading.session, stamp="<timestamp>")
        return "%s (would be written)" % os.path.join(directory, name)
    except Exception:
        return None


def _note_failure(ctx: RotationContext, reason: str) -> None:
    count = ctx.state.note_failure(ctx.session_key, reason)
    if count >= MAX_CONSECUTIVE_FAILURES and ctx.events is not None:
        ctx.events.event(
            "rotation_giving_up",
            session_key=ctx.session_key,
            consecutive_failures=count,
            reason=reason[:300],
        )
        log("[custos] %d consecutive rotation failures for %s; stopping attempts"
            % (count, ctx.session_key))


def _record(ctx: RotationContext, kind: str, result: RotationResult) -> None:
    """Write the audit record for one pipeline outcome.

    A dry run writes nothing. Its whole promise is that the caller can see what
    would happen without anything happening, and an audit log entry is a thing
    that happened -- one that would then claim a rotation was considered on a
    day the user was only asking a question.
    """
    if ctx.events is None or result.dry_run:
        return
    ctx.events.event(
        kind,
        session_key=ctx.session_key,
        tool=result.reading.tool if result.reading else None,
        session=result.reading.session if result.reading else None,
        tokens=result.reading.tokens if result.reading else None,
        reason=(result.decision or {}).get("reason"),
        defer_reasons=result.defer_reasons or None,
        handover=result.handover_path,
        rotated=result.rotated,
        new_session=result.new_session,
        mode=result.mode,
        error=result.error,
        dry_run=result.dry_run or None,
    )


def report_unavailable(adapter: Any, error: Exception, events: Optional[EventLog]) -> str:
    """Build the message shown when an adapter could not be read."""
    message = format_error(getattr(adapter, "name", "adapter"), str(error))
    if events is not None:
        events.event(
            "adapter_unavailable",
            tool=getattr(adapter, "name", None),
            error=str(error)[:300],
        )
    return message


def start_hint_for(adapter: Any, handover_path: Optional[str] = None) -> str:
    """The adapter's instruction for starting a fresh session.

    Adapters answer this in their rotation detail; when they do not, the message
    falls back to a neutral sentence rather than inventing a command line.
    """
    try:
        result = adapter.rotate("", handover_path, dry_run=True)
        detail = getattr(result, "detail", {}) or {}
        hint = detail.get("next") or detail.get("start_hint") or ""
        return str(hint)
    except Exception:
        return ""


def _now() -> str:
    from .util import now_iso

    return now_iso()


def default_question_prefix(config: Any) -> str:
    """Configured gate prefix, or the shipped default."""
    from .gate import DEFAULT_PREFIX

    if config is None:
        return DEFAULT_PREFIX
    value = config.get("gate", "prefix", default=None)
    return str(value).strip() if value else DEFAULT_PREFIX


def recent_attempts(state: State, session_key: str) -> List[Dict[str, Any]]:
    """Attempt records already stored for a session's pending event."""
    event = state.get_event(session_key) or {}
    attempts = event.get("attempts")
    return list(attempts) if isinstance(attempts, list) else []


def record_attempts(state: State, session_key: str, attempts: Sequence[Any]) -> None:
    """Append this fire's attempts to the pending event."""
    event = state.get_event(session_key)
    if not isinstance(event, dict):
        return
    stored = event.setdefault("attempts", [])
    for attempt in attempts:
        stored.append(attempt.to_dict() if hasattr(attempt, "to_dict") else dict(attempt))
    event["last_attempt_epoch"] = time.time()
    state._dirty = True
