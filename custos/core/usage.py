"""Usage readings: the normalised answer to "how full is this session?".

Every adapter returns :class:`Reading` objects, so the threshold engine, the
handover writer and the CLI never need to know which tool produced them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from .util import CustosError


class AdapterNotInstalled(CustosError):
    """The tool this adapter integrates with is not present on this machine."""

    def __init__(self, tool: str, detail: str = "") -> None:
        self.tool = tool
        self.detail = detail
        super().__init__("%s is not installed or has no local data%s" % (
            tool, (": " + detail) if detail else ""))


class NoSessions(CustosError):
    """The tool is installed but has no sessions yet."""

    def __init__(self, tool: str, detail: str = "") -> None:
        self.tool = tool
        self.detail = detail
        super().__init__("%s has no sessions%s" % (tool, (": " + detail) if detail else ""))


class ReadFailed(CustosError):
    """A store exists but could not be read or understood."""

    def __init__(self, tool: str, reason: str, session: Optional[str] = None) -> None:
        self.tool = tool
        self.reason = reason
        self.session = session
        super().__init__("%s read failed%s: %s" % (
            tool, (" for session %s" % session) if session else "", reason))


# Which source wins when two readings share a timestamp. Higher sorts first.
# Values recorded by the tool itself beat values we inferred from a transcript.
# ``report`` and ``hook`` are the names used by pushed readings (the CLI and the
# store); ``push`` is kept so a payload written by an older version still wins.
SOURCE_PRIORITY: Dict[str, int] = {
    "report": 45,        # explicitly reported via `custos report`
    "hook": 45,          # reported by the tool's own hook, same trust
    "push": 45,          # historical spelling of the two above
    "session-store": 30,  # a field the tool maintains for the whole session
    "transcript": 20,     # parsed out of a conversation log
    "external": 10,       # a configured file or command
}


@dataclass
class Reading:
    """One observation of a session's context occupancy.

    ``tokens`` is the size of the prompt that will be sent on the next turn,
    i.e. everything currently occupying the window. ``window`` is the model's
    context window when the tool records it; ``pct`` is derived when it is
    known. ``compressed`` records that the tool has already compacted this
    session at least once, which is a trigger on its own because a recent
    compaction means the window is being recycled rather than grown.
    """

    tool: str
    session: str
    tokens: int
    window: Optional[int] = None
    pct: Optional[float] = None
    source: str = "transcript"
    ts: float = 0.0
    compressed: bool = False
    detail: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.pct is None and self.window:
            self.pct = round(100.0 * self.tokens / float(self.window), 2)

    @property
    def source_priority(self) -> int:
        return SOURCE_PRIORITY.get(self.source, 0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "session": self.session,
            "tokens": self.tokens,
            "window": self.window,
            "pct": self.pct,
            "source": self.source,
            "ts": self.ts,
            "compressed": self.compressed,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Reading":
        return cls(
            tool=str(payload.get("tool", "")),
            session=str(payload.get("session", "")),
            tokens=int(payload.get("tokens") or 0),
            window=payload.get("window"),
            pct=payload.get("pct"),
            source=str(payload.get("source", "transcript")),
            ts=float(payload.get("ts") or 0.0),
            compressed=bool(payload.get("compressed", False)),
            detail=payload.get("detail") or {},
        )

    def describe(self) -> str:
        """One-line human summary used by the CLI table and log lines."""
        parts = [
            self.tool,
            self.session[:12] or "-",
            "%s tok" % self.tokens,
        ]
        if self.window:
            parts.append("window %s" % self.window)
        if self.pct is not None:
            parts.append("%.1f%%" % self.pct)
        parts.append(self.source)
        if self.compressed:
            parts.append("compacted")
        return " | ".join(parts)


def pick_latest(readings: Iterable[Reading]) -> Optional[Reading]:
    """Pick the most trustworthy reading.

    Newest timestamp wins; ties break on source priority (a value the tool
    maintains beats one parsed from a transcript), then on the larger token
    count, so a truncated parse can never win over a complete one.
    """
    candidates: List[Reading] = [r for r in readings if r is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (r.ts, r.source_priority, r.tokens))


def session_key(reading: Reading) -> tuple:
    """Identity of the session a reading belongs to."""
    return (reading.tool, reading.session)


def merge_readings(readings: Iterable[Reading]) -> List[Reading]:
    """Reduce readings to one winner per session.

    Two sources describing the same session must not both be reported: the
    reading that wins is chosen by :func:`pick_latest`, so a hook that knows the
    real number overrides whatever was parsed out of a transcript. The winner
    keeps a note of the sources it beat in ``detail["other_sources"]`` so the
    table can show that a merge happened instead of hiding it.
    """
    groups: Dict[tuple, List[Reading]] = {}
    for reading in readings:
        if reading is None:
            continue
        groups.setdefault(session_key(reading), []).append(reading)

    winners: List[Reading] = []
    for group in groups.values():
        winner = pick_latest(group)
        if winner is None:
            continue
        others = sorted(
            {r.source for r in group if r is not winner and r.source != winner.source})
        if others:
            detail = dict(winner.detail or {})
            detail["other_sources"] = others
            winner.detail = detail
        winners.append(winner)
    # Newest first, then the fullest session: the table reads top-down as
    # "what needs attention soonest", and an old empty session never leads.
    winners.sort(key=lambda r: (r.ts, r.tokens), reverse=True)
    return winners
