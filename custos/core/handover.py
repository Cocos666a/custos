"""Handover snapshots.

A handover is the artefact that makes rotation safe: a fresh session reads it to
recover the state of the work that was in flight. The markdown layout is fixed
because downstream tooling (and humans) parse it, so :data:`SCHEMA_VERSION` is
written into the file and the section headings are constants rather than
free-form text.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence

from .thresholds import describe_trigger
from .usage import Reading
from .util import atomic_write_text, now_iso, now_stamp, truncate

SCHEMA_VERSION = 1

HEADER_PREFIX = "<!-- custos handover"
SECTION_RECENT = "## Recent messages"
SECTION_NEXT = "## Next steps"
UNAVAILABLE = "_Not available: this adapter cannot read conversation history._"


def handover_filename(tool: str, session: str, stamp: Optional[str] = None) -> str:
    """``handover_<tool>_<session8>_<timestamp>.md``."""
    stamp = stamp or now_stamp()
    tool_part = _slug(tool)
    session_part = _slug(session)[:8] or "session"
    return "handover_%s_%s_%s.md" % (tool_part, session_part, stamp)


def unique_handover_path(directory: str, tool: str, session: str) -> str:
    """A path no existing snapshot occupies.

    The timestamp has one-second resolution, so two rotations in the same second
    -- which a test or a scripted run does routinely -- would otherwise write to
    the same name and lose the earlier snapshot. A handover is the artefact that
    makes rotation safe, so silently overwriting one is not acceptable; a counter
    suffix is cheap and stays readable.
    """
    base = handover_filename(tool, session)
    path = os.path.join(directory, base)
    if not os.path.exists(path):
        return path
    stem, extension = os.path.splitext(base)
    for index in range(2, 1000):
        candidate = os.path.join(directory, "%s-%d%s" % (stem, index, extension))
        if not os.path.exists(candidate):
            return candidate
    # A thousand snapshots inside one second means something is looping; fall
    # back to a unique name rather than overwrite.
    import tempfile

    handle, fallback = tempfile.mkstemp(
        prefix="%s-" % stem, suffix=extension, dir=directory)
    os.close(handle)
    return fallback


def write_handover(
    directory: str,
    *,
    reading: Reading,
    decision: Optional[Dict[str, Any]] = None,
    trigger_reason: Optional[str] = None,
    recent_messages: Optional[Sequence[str]] = None,
    recent_unavailable_reason: Optional[str] = None,
    next_steps: Optional[Sequence[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
    generated_at: Optional[str] = None,
) -> str:
    """Write a handover snapshot and return its path.

    ``recent_messages`` is a list of already-formatted lines; pass
    ``recent_unavailable_reason`` instead when the adapter cannot read history,
    so the file says so explicitly rather than looking empty.
    """
    os.makedirs(directory, exist_ok=True)
    path = unique_handover_path(directory, reading.tool, reading.session)
    atomic_write_text(path, render_handover(
        reading=reading,
        decision=decision,
        trigger_reason=trigger_reason,
        recent_messages=recent_messages,
        recent_unavailable_reason=recent_unavailable_reason,
        next_steps=next_steps,
        extra=extra,
        generated_at=generated_at,
    ))
    return path


def render_handover(
    *,
    reading: Reading,
    decision: Optional[Dict[str, Any]] = None,
    trigger_reason: Optional[str] = None,
    recent_messages: Optional[Sequence[str]] = None,
    recent_unavailable_reason: Optional[str] = None,
    next_steps: Optional[Sequence[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
    generated_at: Optional[str] = None,
) -> str:
    """Render the snapshot text without writing it (used by tests and demo)."""
    generated = generated_at or now_iso()
    reason = trigger_reason or (describe_trigger(decision) if decision else "manual")

    lines: List[str] = [
        "%s schema=%d -->" % (HEADER_PREFIX, SCHEMA_VERSION),
        "",
        "# Session handover: %s" % reading.tool,
        "",
        "This file was written automatically before starting a new session. Read it",
        "first to recover the state of the work in progress.",
        "",
        "## Reading",
        "",
        "| field | value |",
        "| --- | --- |",
        "| generated | %s |" % generated,
        "| tool | `%s` |" % reading.tool,
        "| session | `%s` |" % reading.session,
        "| tokens | %s |" % reading.tokens,
        "| window | %s |" % (reading.window if reading.window else "unknown"),
        "| percent | %s |" % (
            ("%.2f%%" % reading.pct) if reading.pct is not None else "unknown"),
        "| source | %s |" % reading.source,
        "| reading time | %s |" % _format_epoch(reading.ts),
        "| compacted already | %s |" % ("yes" if reading.compressed else "no"),
        "| trigger | %s |" % reason,
        "",
    ]

    if decision and decision.get("thresholds") is not None:
        thresholds = decision["thresholds"]
        lines.extend([
            "Thresholds in force: baseline %s, warn %s, rotate %s (`%s`)." % (
                thresholds.baseline.tokens, thresholds.warn, thresholds.rotate,
                thresholds.baseline.mode),
            "",
        ])

    if extra:
        lines.extend(["## Context", ""])
        for key in sorted(extra):
            lines.append("- **%s**: %s" % (key, truncate(str(extra[key]), 300)))
        lines.append("")

    lines.extend([SECTION_RECENT, ""])
    if recent_messages:
        lines.append("Oldest first.")
        lines.append("")
        for entry in recent_messages:
            lines.append("- %s" % truncate(str(entry), 400))
    elif recent_unavailable_reason:
        lines.append("_Not available: %s_" % truncate(recent_unavailable_reason, 300))
    else:
        lines.append(UNAVAILABLE)
    lines.append("")

    lines.extend([SECTION_NEXT, ""])
    if next_steps:
        for step in next_steps:
            lines.append("1. %s" % truncate(str(step), 400))
    else:
        lines.append(
            "_Fill this in: list the unfinished work, the files that were being"
            " edited, and the next command to run._"
        )
    lines.append("")

    return "\n".join(lines)


def read_handover(path: str) -> Dict[str, Any]:
    """Parse a handover file back into its header fields.

    Only the fields this tool wrote are parsed; the prose sections are returned
    as raw text for a caller that wants to display them.
    """
    with open(path, "r", encoding="utf-8") as stream:
        text = stream.read()
    result: Dict[str, Any] = {"path": path, "raw": text, "schema_version": None, "fields": {}}
    for line in text.splitlines():
        if line.startswith(HEADER_PREFIX):
            for token in line.split():
                if token.startswith("schema="):
                    try:
                        result["schema_version"] = int(token.split("=", 1)[1])
                    except ValueError:
                        pass
            break
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("| field |"):
            in_table = True
            continue
        if in_table:
            if not stripped.startswith("|"):
                in_table = False
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if len(cells) == 2 and not set(cells[0]) <= set("- "):
                result["fields"][cells[0]] = cells[1].strip("`")
    return result


def _format_epoch(epoch: float) -> str:
    if not epoch:
        return "unknown"
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(value))
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")
