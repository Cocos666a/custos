"""Persistent state: the ledger, the audit log and pushed readings.

Four files live in the config directory:

``state.json``
    Small, mutable, overwritten atomically. Holds the idempotency ledger (what
    has already been acted on), the per-session rotation history, the rotation
    events still waiting on a gate result, and the gate failure streaks.
``events.jsonl``
    Append-only audit trail. Never rewritten, so it survives a corrupt state
    file and can be replayed by hand.
``readings/<tool>/<session>.json``
    Readings pushed in by the user's own hooks via ``custos report``.
``handovers/``
    Markdown snapshots, one per rotation.

The ledger is what stops the tool from rotating the same session twice: every
action records a key, and the key is pruned after a configured age so the file
cannot grow without bound.

A rotation event is kept **pending** after the rotation itself succeeds, because
the gate that follows may take several scheduled runs to finish. The event is
what carries the attempt history: without it, a run that just started a fresh
session would have nowhere to record that the check failed, and the next run
would start over from zero instead of accumulating a streak.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterator, List, Optional

from .util import (
    CustosError,
    append_jsonl,
    atomic_write_json,
    parse_iso,
    read_json,
)
from .usage import Reading

STATE_SCHEMA_VERSION = 1
DEFAULT_LEDGER_MAX_AGE_DAYS = 45
DEFAULT_KEEP_READINGS_DAYS = 7

# A pending rotation event is dropped after this long. Long enough that a slow
# gate still finishes, short enough that an abandoned event does not pin the
# monitor to a session that no longer exists.
DEFAULT_PENDING_MAX_AGE_DAYS = 3.0

# Statuses a pending rotation event can carry. Only the first blocks the next
# rotation for its session; the others are terminal for reporting purposes.
EVENT_PENDING = "gate_pending"
EVENT_RESOLVED = "resolved"


class State:
    """The ``state.json`` document."""

    def __init__(self, path: str, data: Optional[Dict[str, Any]] = None) -> None:
        self.path = path
        self.data: Dict[str, Any] = data or _empty_state()
        self._dirty = False

    # -- persistence -----------------------------------------------------
    @classmethod
    def load(cls, path: str) -> "State":
        """Load state, falling back to an empty document when unreadable.

        A corrupt state file must not stop the tool: losing the ledger costs at
        most one duplicate notification, while refusing to start costs every
        future rotation.
        """
        raw = read_json(path, default=None)
        if not isinstance(raw, dict):
            return cls(path, _empty_state())
        merged = _empty_state()
        for key, value in raw.items():
            merged[key] = value
        if not isinstance(merged.get("ledger"), dict):
            merged["ledger"] = {}
        if not isinstance(merged.get("history"), dict):
            merged["history"] = {}
        if not isinstance(merged.get("last_events"), list):
            merged["last_events"] = []
        if not isinstance(merged.get("meta"), dict):
            merged["meta"] = {}
        if not isinstance(merged.get("events"), dict):
            merged["events"] = {}
        if not isinstance(merged.get("gate"), dict):
            merged["gate"] = {}
        return cls(path, merged)

    def save(self) -> None:
        if not self._dirty:
            return
        self.data["updated_at"] = _now()
        atomic_write_json(self.path, self.data)
        self._dirty = False

    # -- ledger ----------------------------------------------------------
    @property
    def ledger(self) -> Dict[str, Any]:
        return self.data.setdefault("ledger", {})

    def seen(self, key: str) -> bool:
        """Whether ``key`` has already been acted on."""
        return key in self.ledger

    def mark(self, key: str, detail: str = "") -> None:
        """Record that ``key`` has been acted on."""
        self.ledger[key] = {"ts": _now(), "epoch": time.time(), "detail": detail[:200]}
        self._dirty = True

    def unmark(self, key: str) -> None:
        if key in self.ledger:
            del self.ledger[key]
            self._dirty = True

    def once(self, key: str) -> bool:
        """Return ``True`` the first time ``key`` is seen, and mark it.

        Callers use this as ``if state.once("warn:abc"): notify(...)``.
        """
        if self.seen(key):
            return False
        self.mark(key)
        return True

    def prune_ledger(self, max_age_days: int = DEFAULT_LEDGER_MAX_AGE_DAYS) -> int:
        """Drop ledger entries older than ``max_age_days``; returns the count."""
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        for key in list(self.ledger.keys()):
            entry = self.ledger.get(key) or {}
            epoch = entry.get("epoch") if isinstance(entry, dict) else None
            if epoch is None:
                epoch = parse_iso(entry.get("ts")) if isinstance(entry, dict) else 0.0
            if not epoch or epoch < cutoff:
                del self.ledger[key]
                removed += 1
        if removed:
            self._dirty = True
        return removed

    # -- history ---------------------------------------------------------
    @property
    def history(self) -> Dict[str, Any]:
        return self.data.setdefault("history", {})

    def last_rotated_session(self, session_key: str) -> Optional[str]:
        """The session id most recently rotated for ``session_key``.

        Used for idempotency: a session that has already been rotated is not
        rotated again even if its old reading still looks high.
        """
        entry = self.history.get(session_key)
        if isinstance(entry, dict):
            value = entry.get("last_old_session")
            return str(value) if value else None
        return None

    def record_rotation(
        self,
        session_key: str,
        old_session: str,
        new_session: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        entry = self.history.setdefault(session_key, {})
        entry["last_old_session"] = old_session
        entry["last_rotated_at"] = _now()
        entry["last_rotated_epoch"] = time.time()
        entry.pop("last_handled_epoch", None)
        if new_session:
            entry["last_new_session"] = new_session
        if detail:
            entry["last_detail"] = detail
        entry["count"] = int(entry.get("count", 0)) + 1
        self._dirty = True

    def record_handled(
        self,
        session_key: str,
        session: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record that a session was acted on without the adapter rotating it.

        This is the assisted case: the snapshot was written and the user was
        told, but starting the new session is theirs to do. Without this record
        a five-minute schedule would rewrite the snapshot and repeat the message
        on every tick.
        """
        entry = self.history.setdefault(session_key, {})
        entry["last_handled_session"] = session
        entry["last_handled_epoch"] = time.time()
        entry["last_handled_at"] = _now()
        if detail:
            entry["last_handled_detail"] = detail
        entry["handled_count"] = int(entry.get("handled_count", 0)) + 1
        self._dirty = True

    def last_handled(self, session_key: str) -> Optional[Dict[str, Any]]:
        """The last handled session and when, if any."""
        entry = self.history.get(session_key)
        if not isinstance(entry, dict):
            return None
        session = entry.get("last_handled_session")
        if not session:
            return None
        return {
            "session": str(session),
            "epoch": float(entry.get("last_handled_epoch") or 0.0),
        }

    def note_failure(self, session_key: str, reason: str) -> int:
        """Record a rotation failure; returns the consecutive failure count."""
        entry = self.history.setdefault(session_key, {})
        count = int(entry.get("fail_count", 0)) + 1
        entry["fail_count"] = count
        entry["last_failure"] = {"ts": _now(), "reason": reason[:300]}
        self._dirty = True
        return count

    def clear_failures(self, session_key: str) -> None:
        entry = self.history.setdefault(session_key, {})
        if entry.get("fail_count"):
            entry["fail_count"] = 0
            self._dirty = True

    # -- rotation events --------------------------------------------------
    @property
    def events(self) -> Dict[str, Any]:
        """Pending and recently resolved rotation events, keyed by session key."""
        return self.data.setdefault("events", {})

    def put_event(self, session_key: str, event: Dict[str, Any]) -> Dict[str, Any]:
        """Create or replace the event record for ``session_key``."""
        self.events[session_key] = event
        self._dirty = True
        return event

    def get_event(self, session_key: str) -> Optional[Dict[str, Any]]:
        event = self.events.get(session_key)
        return event if isinstance(event, dict) else None

    def pending_events(self, max_age_days: float = DEFAULT_PENDING_MAX_AGE_DAYS) -> List[Dict[str, Any]]:
        """Events still waiting on a gate result, oldest first.

        Expired events are dropped here rather than by a separate sweep: the
        only thing that reads them is the code that would otherwise act on them,
        so expiring on read keeps the file self-cleaning.
        """
        cutoff = time.time() - max(0.0, max_age_days) * 86400
        pending: List[Dict[str, Any]] = []
        for key, event in list(self.events.items()):
            if not isinstance(event, dict):
                del self.events[key]
                self._dirty = True
                continue
            if event.get("status") != EVENT_PENDING:
                continue
            if float(event.get("created_epoch") or 0.0) < cutoff:
                event["status"] = "expired"
                event["expired_at"] = _now()
                self._dirty = True
                continue
            pending.append(event)
        pending.sort(key=lambda item: float(item.get("created_epoch") or 0.0))
        return pending

    def resolve_event(self, session_key: str, resolution: str, **fields: Any) -> None:
        """Mark an event finished, keeping it for the record."""
        event = self.events.get(session_key)
        if not isinstance(event, dict):
            return
        event["status"] = EVENT_RESOLVED
        event["resolution"] = resolution
        event["resolved_at"] = _now()
        event["resolved_epoch"] = time.time()
        event.update(fields)
        self._dirty = True

    def prune_events(self, keep: int = 20) -> int:
        """Keep the newest ``keep`` resolved events; returns how many were dropped."""
        resolved = [
            (key, event) for key, event in self.events.items()
            if isinstance(event, dict) and event.get("status") != EVENT_PENDING
        ]
        resolved.sort(key=lambda item: float(item[1].get("created_epoch") or 0.0))
        removed = 0
        for key, _event in resolved[:-keep] if keep > 0 else resolved:
            del self.events[key]
            removed += 1
        if removed:
            self._dirty = True
        return removed

    def pending_for_session(self, session_key: str) -> Optional[Dict[str, Any]]:
        event = self.events.get(session_key)
        if isinstance(event, dict) and event.get("status") == EVENT_PENDING:
            return event
        return None

    # -- gate bookkeeping -------------------------------------------------
    @property
    def gate(self) -> Dict[str, Any]:
        """Gate-side counters: the sequence cursor and the last attempt time."""
        return self.data.setdefault("gate", {})

    def gate_sequence(self) -> int:
        try:
            return int(self.gate.get("sequence", 0))
        except (TypeError, ValueError):
            return 0

    def set_gate_sequence(self, value: int) -> None:
        if self.gate.get("sequence") != value:
            self.gate["sequence"] = int(value)
            self._dirty = True

    def gate_attempts_total(self) -> int:
        """How many gate attempts have ever been made.

        Used to seed the random question picker: a fixed seed would ask the same
        question on every rotation, which defeats the point of a bank.
        """
        try:
            return int(self.gate.get("attempts_total", 0))
        except (TypeError, ValueError):
            return 0

    def note_gate_attempts(self, count: int) -> None:
        if count:
            self.gate["attempts_total"] = self.gate_attempts_total() + int(count)
            self.gate["last_attempt_epoch"] = time.time()
            self._dirty = True

    def last_gate_attempt(self) -> float:
        try:
            return float(self.gate.get("last_attempt_epoch") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def alerts(self, session_key: str) -> Dict[str, Any]:
        event = self.get_event(session_key) or {}
        alerts = event.get("alerts")
        if not isinstance(alerts, dict):
            alerts = {}
            if event:
                event["alerts"] = alerts
                self._dirty = True
        return alerts

    def alert_due(self, session_key: str, kind: str, repeat_seconds: float) -> bool:
        """Whether an alert of ``kind`` may be emitted again for this session."""
        last = float(self.alerts(session_key).get("%s_last_epoch" % kind) or 0.0)
        return (time.time() - last) >= max(0.0, repeat_seconds)

    def mark_alert(self, session_key: str, kind: str) -> None:
        alerts = self.alerts(session_key)
        alerts["%s_last_epoch" % kind] = time.time()
        alerts["%s_last_ts" % kind] = _now()
        alerts["%s_count" % kind] = int(alerts.get("%s_count" % kind, 0)) + 1
        self._dirty = True

    # -- misc ------------------------------------------------------------
    def set_meta(self, key: str, value: Any) -> None:
        self.data.setdefault("meta", {})[key] = value
        self._dirty = True

    def get_meta(self, key: str, default: Any = None) -> Any:
        return self.data.get("meta", {}).get(key, default)

    def summary(self) -> Dict[str, Any]:
        """Small digest for ``custos status``."""
        events = self.data.get("last_events") or []
        pending = [
            event for event in self.events.values()
            if isinstance(event, dict) and event.get("status") == EVENT_PENDING
        ]
        meta = self.data.get("meta") if isinstance(self.data.get("meta"), dict) else {}
        return {
            "updated_at": self.data.get("updated_at"),
            "ledger_entries": len(self.ledger),
            "tracked_sessions": len(self.history),
            "recorded_events": len(self.events),
            "pending_events": len(pending),
            "last_monitor_ts": meta.get("last_monitor_ts") or 0.0,
            "gate_attempts": self.gate_attempts_total(),
            "last_events": events[-5:],
        }


def _empty_state() -> Dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "updated_at": "",
        "ledger": {},
        "history": {},
        "last_events": [],
        "events": {},
        "gate": {},
        "meta": {},
    }


def _now() -> str:
    from .util import now_iso

    return now_iso()


# ------------------------------------------------------------------- events


class EventLog:
    """Append-only audit trail in ``events.jsonl``."""

    def __init__(self, path: str, state: Optional[State] = None) -> None:
        self.path = path
        self.state = state

    def event(self, kind: str, **fields: Any) -> Dict[str, Any]:
        """Append one event and mirror it into the state's recent list."""
        record: Dict[str, Any] = {"ts": _now(), "epoch": round(time.time(), 3), "kind": kind}
        record.update(fields)
        try:
            append_jsonl(self.path, record)
        except OSError:
            # An unwritable audit log must not break the action it describes.
            pass
        if self.state is not None:
            recent = self.state.data.setdefault("last_events", [])
            recent.append({"ts": record["ts"], "kind": kind})
            del recent[:-10]
            self.state._dirty = True
        return record

    def tail(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Return up to ``limit`` most recent events (oldest first)."""
        if not os.path.exists(self.path):
            return []
        records: List[Dict[str, Any]] = []
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    line = line.strip()
                    if not line:
                        continue
                    import json

                    try:
                        parsed = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(parsed, dict):
                        records.append(parsed)
        except OSError:
            return []
        return records[-limit:]

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        return iter(self.tail(limit=10_000))


# ----------------------------------------------------------------- readings


class ReadingStore:
    """Readings pushed by external hooks.

    A tool that already knows its own token usage can hand it over instead of
    making custos guess from a transcript. One file per session keeps writes
    independent, so two tools reporting at once cannot clobber each other.
    """

    def __init__(self, directory: str) -> None:
        self.dir = directory

    def path_for(self, tool: str, session: str) -> str:
        safe_tool = _safe_component(tool)
        safe_session = _safe_component(session)
        return os.path.join(self.dir, safe_tool, "%s.json" % safe_session)

    def push(self, reading: Reading) -> str:
        """Store a reading atomically and return the file path.

        The reading's own ``source`` is preserved: the CLI labels a value it was
        handed ``report``, a tool's own hook labels it ``hook``, and both sort
        above anything parsed from a transcript.
        """
        if not reading.ts:
            reading.ts = time.time()
        path = self.path_for(reading.tool, reading.session)
        payload = reading.to_dict()
        atomic_write_json(path, payload)
        return path

    def load_all(self) -> List[Reading]:
        """Load every pushed reading, skipping unreadable files."""
        readings: List[Reading] = []
        if not os.path.isdir(self.dir):
            return readings
        for tool in sorted(os.listdir(self.dir)):
            tool_dir = os.path.join(self.dir, tool)
            if not os.path.isdir(tool_dir):
                continue
            for name in sorted(os.listdir(tool_dir)):
                if not name.endswith(".json"):
                    continue
                payload = read_json(os.path.join(tool_dir, name), default=None)
                if isinstance(payload, dict) and payload.get("session"):
                    try:
                        readings.append(Reading.from_dict(payload))
                    except (TypeError, ValueError):
                        continue
        return readings

    def load_for(self, tool: str, session: str) -> Optional[Reading]:
        payload = read_json(self.path_for(tool, session), default=None)
        if isinstance(payload, dict):
            try:
                return Reading.from_dict(payload)
            except (TypeError, ValueError):
                return None
        return None

    def prune(self, max_age_days: int = DEFAULT_KEEP_READINGS_DAYS) -> int:
        """Delete pushed readings older than ``max_age_days``."""
        removed = 0
        cutoff = time.time() - max_age_days * 86400
        readings = self.load_all()
        for reading in readings:
            if reading.ts and reading.ts < cutoff:
                try:
                    os.unlink(self.path_for(reading.tool, reading.session))
                    removed += 1
                except OSError:
                    pass
        return removed


def _safe_component(value: str) -> str:
    """Make a tool or session id safe to use as a path component.

    Session ids can contain ``/`` (file-backed tools) or ``:`` (composite ids),
    so anything outside a conservative set is replaced rather than escaped.
    """
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(value))
    cleaned = cleaned.strip("._") or "unknown"
    return cleaned[:128]
