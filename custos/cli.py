"""Command line interface.

Every subcommand lives here, but none of them contain policy: what a rotation
does, when a gate runs and how a message is worded all belong to ``core``. The
CLI reads arguments, calls one core entry point, and prints what came back.

Two conventions run through the whole file.

**stdout carries messages, stderr carries diagnostics.** A scheduled run that
has nothing to report prints nothing at all, which is what lets cron mail, a
terminal, or the demo be read at a glance. ``--json`` switches stdout to a single
JSON document instead; diagnostics never move.

**Every mutating command has ``--dry-run``.** Scan, status and questions do not
need one because they change nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from . import __version__
from .adapters import (
    SYNTHETIC_ADAPTERS,
    adapter_status,
    auto_adapter_names,
    get_adapter,
    list_adapters,
)
from .core import config as config_module
from .core import gate_runner as gate_runner_module
from .core import goal_driver as goal_driver_module
from .core.gate import (
    SELECTION_MODES,
    describe_questions,
    load_questions,
    summarise_bank,
    validate_bank,
)
from .core.notify import (
    KIND_ALERT_INTERNAL,
    KIND_ERROR,
    KIND_ROTATION,
    KIND_WARN,
    deliver,
    format_error,
    format_internal_alert,
    format_warning,
    make_message,
    message_text,
)
from .core.rotation import (
    RotationContext,
    check_pending_gate,
    do_rotation,
    start_hint_for,
)
from .core.state import EventLog, ReadingStore, State
from .core.thresholds import describe_trigger, evaluate, resolve_thresholds
from .core.usage import (
    AdapterNotInstalled,
    CustosError,
    NoSessions,
    Reading,
    ReadFailed,
    merge_readings,
    pick_latest,
)
from .core.util import FileLock, format_age, format_tokens, log, truncate

EXIT_OK = 0
EXIT_ERROR = 1

MIN_PYTHON = (3, 9)


def build_parser() -> argparse.ArgumentParser:
    """Build the full argument parser.

    ``--json`` and ``--config-dir`` are accepted both before and after the
    subcommand, because both spellings are natural to type and a script should
    not have to care which one the parser wanted.
    """
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS as the default is what makes the two positions cooperate: when the
    # option is absent after the subcommand, argparse leaves the value parsed
    # before the subcommand alone instead of resetting it to None.
    common.add_argument(
        "--config-dir", metavar="DIR", default=argparse.SUPPRESS,
        help="configuration directory (default: $CUSTOS_CONFIG_DIR or "
             "~/.config/custos)")
    common.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS,
        help="machine-readable output")

    parser = argparse.ArgumentParser(
        prog="custos",
        description=(
            "Automatic context-window monitoring and session rotation for AI "
            "coding agents."
        ),
        epilog=(
            "Typical setup: run `custos monitor --once` from cron, launchd "
            "or a systemd timer. `custos demo` shows the whole pipeline "
            "without touching any agent. See docs/ for the details."
        ),
    )
    parser.add_argument(
        "--version", action="version", version="custos %s" % __version__)
    parser.add_argument(
        "--config-dir", metavar="DIR", default=None,
        help="configuration directory (default: $CUSTOS_CONFIG_DIR or "
             "~/.config/custos)")
    parser.add_argument(
        "--json", action="store_true", default=None,
        help="machine-readable output")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # -- scan ------------------------------------------------------------
    scan = subparsers.add_parser(
        "scan", parents=[common],
        help="discover sessions and read their context usage")
    scan.add_argument(
        "--tool", action="append", metavar="NAME", default=None,
        help="limit to one adapter; repeatable (available: %s)"
             % ", ".join(list_adapters()))
    scan.add_argument(
        "--limit", type=int, default=5, metavar="N",
        help="sessions per adapter (default: 5)")
    scan.add_argument(
        "--all-sessions", action="store_true",
        help="read every session instead of the newest --limit")

    # -- status ----------------------------------------------------------
    status = subparsers.add_parser(
        "status", parents=[common],
        help="show configuration, adapters and recent state")
    status.add_argument(
        "--events", type=int, default=5, metavar="N",
        help="how many recent events to show (default: 5)")

    # -- report ----------------------------------------------------------
    report = subparsers.add_parser(
        "report", parents=[common],
        help="push a usage reading from a hook or wrapper script")
    report.add_argument("--tool", required=True, metavar="NAME",
                        help="tool the reading belongs to (free-form)")
    report.add_argument("--session", required=True, metavar="ID",
                        help="session identifier")
    report.add_argument("--tokens", required=True, metavar="N",
                        help="context occupancy in tokens; must not be negative")
    report.add_argument("--window", type=int, default=None, metavar="M",
                        help="context window in tokens, when known")
    report.add_argument("--compressed", action="store_true",
                        help="the tool has already compacted this session")
    report.add_argument("--source", default="report", metavar="LABEL",
                        choices=["report", "hook"],
                        help="label recorded as the reading's origin "
                             "(default: report)")

    # -- rotate ----------------------------------------------------------
    rotate = subparsers.add_parser(
        "rotate", parents=[common],
        help="run the rotation pipeline for one session")
    rotate.add_argument("--tool", required=True, metavar="NAME",
                        help="adapter to use (available: %s)"
                             % ", ".join(list_adapters()))
    rotate.add_argument("--session", default=None, metavar="ID",
                        help="session to rotate (default: the newest one)")
    rotate.add_argument("--dry-run", action="store_true",
                        help="evaluate and report; write nothing, run nothing")
    rotate.add_argument("--force", action="store_true",
                        help="ignore the interlock windows (the kill switch and "
                             "the single-instance lock still apply)")
    rotate.add_argument("--no-gate", action="store_true",
                        help="skip the gate for this rotation")

    # -- monitor ---------------------------------------------------------
    monitor = subparsers.add_parser(
        "monitor", parents=[common],
        help="scheduled entry point: evaluate every session and act on the "
             "first eligible one")
    monitor.add_argument("--once", action="store_true",
                         help="run one pass and exit (the cron/launchd/systemd "
                              "path; without it the command loops)")
    monitor.add_argument("--dry-run", action="store_true",
                         help="evaluate and report; write nothing, run nothing")
    monitor.add_argument("--force", action="store_true",
                         help="ignore the throttle and the interlock windows")
    monitor.add_argument("--tool", action="append", metavar="NAME", default=None,
                         help="limit to one adapter; repeatable")

    # -- gate ------------------------------------------------------------
    gate = subparsers.add_parser(
        "gate", parents=[common],
        help="run the intelligence check against a tool's one-shot prompt")
    gate.add_argument("--tool", required=True, metavar="NAME",
                      help="adapter whose one-shot command runs the questions")
    gate.add_argument("--mode", choices=list(SELECTION_MODES), default=None,
                      help="question selection (default: gate.mode from config)")
    gate.add_argument("--attempts", type=int, default=None, metavar="N",
                      help="attempts in this fire (default: gate.attempt_limit)")
    gate.add_argument("--interval", type=float, default=None, metavar="SECONDS",
                      help="seconds between attempts")
    gate.add_argument("--question", action="append", metavar="ID", default=None,
                      help="ask only this question; repeatable")
    gate.add_argument("--dry-run", action="store_true",
                      help="show what would be asked; run nothing")
    gate.add_argument("--seed", type=int, default=None, metavar="N",
                      help="make a random selection reproducible")

    # -- demo ------------------------------------------------------------
    demo = subparsers.add_parser(
        "demo", parents=[common],
        help="run the whole pipeline against synthetic sessions")
    demo.add_argument("--keep", action="store_true",
                      help="keep the temporary directory and print its path")
    demo.add_argument("--dir", default=None, metavar="DIR",
                      help="use this directory instead of a fresh temporary one")

    # -- goal ------------------------------------------------------------
    goal = subparsers.add_parser(
        "goal", parents=[common],
        help="run one long task across sessions, rotating automatically")
    goal.add_argument("--tool", required=True, metavar="NAME",
                      help="adapter to drive (available: %s)"
                           % ", ".join(list_adapters()))
    goal.add_argument("--task-file", required=True, metavar="PATH",
                      help="file holding the goal, sent to the first session")
    goal.add_argument("--cwd", default=None, metavar="DIR",
                      help="working directory for every spawned round "
                           "(default: the current directory)")
    goal.add_argument("--max-sessions", type=int, default=None, metavar="N",
                      help="stop after this many sessions (default: %d)"
                           % goal_driver_module.DEFAULT_MAX_SESSIONS)
    goal.add_argument("--max-rounds-per-session", type=int, default=None,
                      metavar="N",
                      help="stop after max-sessions * N rounds (default: %d)"
                           % goal_driver_module.DEFAULT_MAX_ROUNDS_PER_SESSION)
    goal.add_argument("--rotate-line", type=int, default=None, metavar="TOKENS",
                      help="token count that triggers a rotation "
                           "(default: the configured rotate line)")
    goal.add_argument("--no-resume", action="store_true",
                      help="rotate before every round instead of resuming")
    goal.add_argument("--state-file", default=None, metavar="PATH",
                      help="where the run state lives "
                           "(default: <cwd>/.custos-goal-state.json)")

    # -- doctor ----------------------------------------------------------
    subparsers.add_parser(
        "doctor", parents=[common],
        help="check the installation, configuration and adapters")

    # -- questions -------------------------------------------------------
    questions = subparsers.add_parser(
        "questions", parents=[common],
        help="inspect and validate the question bank")
    questions.add_argument(
        "action", nargs="?", default="list", choices=["list", "validate"],
        help="what to do with the bank (default: list)")

    return parser


def _normalise_flags(args: argparse.Namespace) -> None:
    """Normalise options that may have been given before the subcommand."""
    args.json = bool(getattr(args, "json", False))
    args.config_dir = getattr(args, "config_dir", None) or None


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point. Returns the process exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _normalise_flags(args)

    if not args.command:
        parser.print_help()
        return EXIT_OK

    try:
        config = config_module.load_config(args.config_dir)
    except CustosError as exc:
        print("custos: %s" % exc, file=sys.stderr)
        return EXIT_ERROR

    handlers = {
        "scan": command_scan,
        "status": command_status,
        "report": command_report,
        "rotate": command_rotate,
        "monitor": command_monitor,
        "gate": command_gate,
        "demo": command_demo,
        "goal": command_goal,
        "doctor": command_doctor,
        "questions": command_questions,
    }
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover - argparse rejects unknown commands
        parser.print_help()
        return EXIT_OK

    try:
        return handler(args, config)
    except CustosError as exc:
        print("custos: %s" % exc, file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("custos: interrupted", file=sys.stderr)
        return EXIT_ERROR


# --------------------------------------------------------------- collection


def collect_readings(
    config: Any,
    tools: Optional[List[str]] = None,
    limit: int = 5,
    include_undetected: bool = False,
    explicit_tools: bool = False,
) -> Tuple[List[Reading], List[Dict[str, str]], List[str]]:
    """Read every configured adapter plus the pushed readings.

    Returns ``(readings, problems, hard_failures)``. ``problems`` describes a
    tool that produced nothing and is informational; ``hard_failures`` names a
    tool whose store exists but could not be read, which the caller must treat as
    fail-closed because an unreadable store means the tool cannot tell whether a
    session is busy.

    ``explicit_tools`` says whether ``tools`` is a narrowing the caller asked for
    or merely the default adapter list. It matters for pushed readings: a tool
    with no adapter is the main reason push mode exists, so narrowing to
    ``claude_code`` should hide other tools' readings, while an unfiltered scan
    must still show a reading for a tool this build has never heard of.
    """
    requested = tools or auto_adapter_names(config, include_undetected=include_undetected)
    readings: List[Reading] = []
    problems: List[Dict[str, str]] = []
    hard_failures: List[str] = []
    unknown: List[str] = []

    for name in requested:
        if name in SYNTHETIC_ADAPTERS:
            continue
        try:
            adapter = get_adapter(name, config)
        except KeyError as exc:
            # Deferred: a name with pushed readings is a tool that reports its own
            # usage, not a typo, and calling it unknown would be misleading.
            unknown.append(name)
            continue
        try:
            if not adapter.detect():
                problems.append({
                    "tool": name,
                    "reason": "not installed (no local data found)",
                })
                continue
            sessions = adapter.sessions(limit=limit if limit > 0 else 1000)
        except AdapterNotInstalled as exc:
            problems.append({"tool": name, "reason": str(exc)})
            continue
        except NoSessions as exc:
            problems.append({"tool": name, "reason": str(exc)})
            continue
        except ReadFailed as exc:
            problems.append({"tool": name, "reason": str(exc)})
            hard_failures.append(name)
            continue
        except CustosError as exc:
            problems.append({"tool": name, "reason": str(exc)})
            hard_failures.append(name)
            continue

        for session in sessions:
            try:
                readings.append(adapter.read_usage(session))
            except CustosError as exc:
                problems.append({
                    "tool": name,
                    "reason": "%s: %s" % (session.session[:16], exc),
                })

    pushed = ReadingStore(config.readings_dir).load_all()
    if explicit_tools and tools:
        wanted = {name.lower() for name in tools}
        pushed = [reading for reading in pushed if reading.tool.lower() in wanted]
    readings.extend(pushed)

    pushed_tools = {reading.tool.lower() for reading in pushed}
    for name in unknown:
        if name.lower() in pushed_tools:
            continue
        try:
            get_adapter(name, config)
        except KeyError as exc:
            problems.append({"tool": name, "reason": str(exc)})

    # One reading per session: a pushed value and an adapter value for the same
    # session are two observations of one thing, not two sessions.
    merged = merge_readings(readings)
    return merged, problems, hard_failures


def _verification_suffix(name: str) -> str:
    try:
        adapter = get_adapter(name)
    except KeyError:
        return ""
    return adapter.VERIFICATION


def _reading_for(
    config: Any,
    tool: str,
    session: Optional[str],
    *,
    limit: int = 20,
) -> Tuple[Optional[Reading], Any, List[Dict[str, str]]]:
    """Resolve one reading plus the adapter that produced it.

    Pushed readings are considered alongside adapter readings, so a session
    reported by a hook can be rotated even when no adapter can read it.
    """
    problems: List[Dict[str, str]] = []
    store = ReadingStore(config.readings_dir)
    pushed = [reading for reading in store.load_all() if reading.tool == tool]

    adapter = None
    readings: List[Reading] = []
    try:
        adapter = get_adapter(tool, config)
    except KeyError as exc:
        problems.append({"tool": tool, "reason": str(exc)})
    else:
        try:
            if adapter.detect():
                for ref in adapter.sessions(limit=limit):
                    if session and ref.session != session:
                        continue
                    try:
                        readings.append(adapter.read_usage(ref))
                    except CustosError as exc:
                        problems.append({"tool": tool, "reason": str(exc)})
            else:
                problems.append({"tool": tool, "reason": "not installed"})
        except CustosError as exc:
            problems.append({"tool": tool, "reason": str(exc)})

    excluded = {
        (reading.tool, reading.session) for reading in readings
    }
    for reading in pushed:
        if session and reading.session != session:
            continue
        if (reading.tool, reading.session) in excluded:
            continue
        readings.append(reading)
        adapter = adapter or _PushedAdapter(tool, config)

    if session:
        readings = [reading for reading in readings if reading.session == session]
    if not readings:
        return None, adapter, problems
    return pick_latest(readings), adapter, problems


class _PushedAdapter:
    """Stands in for an adapter when a session only exists as a pushed reading.

    Rotation and the gate need an adapter object; a session reported by a hook
    has no store behind it. This one is honest about it: it cannot read the
    session, cannot start a new one, and cannot run a prompt unless the generic
    settings give it a command to run.

    The read methods raise rather than return empty values, so a caller that
    forgets which kind of adapter it holds gets a clear error instead of a
    silently empty result.
    """

    VERIFICATION = "experimental"
    VERIFICATION_NOTE = "a session reported by a hook; no local store to read"

    def __init__(self, name: str, config: Any = None) -> None:
        self.name = name
        self.config = config

    def settings(self) -> Dict[str, Any]:
        return {}

    def detect(self) -> bool:
        return False

    def sessions(self, limit: int = 20) -> List[Any]:
        raise NoSessions(self.name, "sessions for this tool arrive via `custos report`")

    def read_usage(self, session: Any) -> Reading:
        raise ReadFailed(
            self.name,
            "this tool reports its usage with `custos report`; there is no "
            "store to read")

    def recent_messages(self, session: Any, limit: int = 12) -> List[str]:
        return []

    def history_unavailable_reason(self) -> str:
        return ("this session was reported by a hook, so custos has no "
                "conversation history for it")

    def capabilities(self) -> Dict[str, Any]:
        return {
            "tool": self.name,
            "verification": self.VERIFICATION,
            "detect": False,
            "sessions": False,
            "read_usage": False,
            "compression_seen": False,
            "history": False,
            "rotate": False,
            "full_auto": False,
            "run_prompt": False,
            "start_hint": True,
            "note": "readings for this tool arrive via `custos report`",
        }

    def busy(self, session: Any) -> Optional[bool]:
        """``False``: whatever pushed the reading knows more than we do.

        A hook reports usage after a turn, so the session is between turns by the
        time the report lands. Returning ``None`` here would defer every
        rotation forever, which is a worse failure than trusting the hook.
        """
        return False

    def rotate(self, session: str, handover_path: Optional[str],
               dry_run: bool = False, config: Any = None) -> Any:
        from .adapters.base import RotateResult

        if dry_run:
            return RotateResult(mode="assisted", rotated=False,
                                detail={"would_rotate": session})
        return RotateResult(
            mode="assisted",
            rotated=False,
            detail={
                "note": "this session was reported by a hook; custos cannot "
                        "start a replacement for it",
                "handover": handover_path,
                "next": self.start_hint(handover_path),
            },
        )

    def start_hint(self, handover_path: Optional[str] = None) -> str:
        if handover_path:
            return "start a new session in %s and read %s" % (self.name, handover_path)
        return "start a new session in %s" % self.name


# ------------------------------------------------------------------- scan


def command_scan(args: argparse.Namespace, config: Any) -> int:
    """Discover sessions and read their usage."""
    requested = args.tool or auto_adapter_names(config, include_undetected=True)
    limit = 0 if args.all_sessions else max(1, args.limit)

    readings, problems, _hard = collect_readings(
        config, tools=requested, limit=limit, include_undetected=bool(args.tool),
        explicit_tools=bool(args.tool))

    for problem in problems:
        if problem.get("tool") in (args.tool or []):
            problem["verification"] = _verification_suffix(problem["tool"])

    if args.json:
        _print_scan_json(readings, problems, config)
    else:
        _print_scan_table(readings, problems, config, discovered=requested)
    return EXIT_OK


def _print_scan_table(
    readings: List[Reading],
    problems: List[Dict[str, str]],
    config: Any,
    discovered: Optional[List[str]] = None,
) -> None:
    if readings:
        header = ("TOOL", "SESSION", "TOKENS", "WINDOW", "PCT", "SOURCE", "UPDATED")
        rows = []
        for reading in readings:
            thresholds = resolve_thresholds(config, reading)
            decision = evaluate(reading, thresholds)
            marker = ""
            if decision:
                marker = {"rotate": "  <- rotate", "warn": "  <- warn",
                          "anomaly": "  <- check"}.get(decision.get("level"), "")
            rows.append((
                reading.tool,
                reading.session[:20] or "-",
                format_tokens(reading.tokens),
                format_tokens(reading.window) if reading.window else "-",
                ("%.1f%%" % reading.pct) if reading.pct is not None else "-",
                reading.source,
                format_age(reading.ts),
            ))
            if marker:
                rows[-1] = rows[-1][:-1] + (rows[-1][-1] + marker,)
        _print_table(header, rows)

        # A merged session is one row, but two sources saw it. Saying which ones
        # lost is the difference between "there is one reading" and "there were
        # two and you are looking at the winner".
        for reading in readings:
            others = (reading.detail or {}).get("other_sources")
            if others:
                print("  %-12s %s: %s wins over %s" % (
                    reading.tool, reading.session[:16], reading.source,
                    ", ".join(str(item) for item in others)))
    else:
        print("no sessions found")

    # Report the adapters that produced nothing, so an absent tool is visible
    # rather than implied by a missing row.
    reported = {problem["tool"] for problem in problems}
    for name in discovered or []:
        if name in reported or name in SYNTHETIC_ADAPTERS:
            continue
        if any(reading.tool == name for reading in readings):
            continue
        problems.append({"tool": name, "reason": "no sessions found"})

    for problem in problems:
        suffix = ""
        if problem.get("verification"):
            suffix = "  [%s]" % problem["verification"]
        print("  %-12s %s%s" % (problem["tool"], problem["reason"], suffix))


def _print_table(header: tuple, rows: List[tuple]) -> None:
    """Print a simple aligned table; no external dependency needed."""
    widths = [len(cell) for cell in header]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(str(cell)))
    line = "  ".join(str(cell).ljust(widths[index])
                     for index, cell in enumerate(header))
    print(line)
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[index])
                        for index, cell in enumerate(row)))


def _print_scan_json(
    readings: List[Reading], problems: List[Dict[str, str]], config: Any
) -> None:
    payload: Dict[str, Any] = {
        "readings": [],
        "problems": problems,
    }
    for reading in readings:
        thresholds = resolve_thresholds(config, reading)
        decision = evaluate(reading, thresholds)
        entry = reading.to_dict()
        entry["thresholds"] = {
            "baseline": thresholds.baseline.tokens,
            "baseline_source": thresholds.baseline.source,
            "warn": thresholds.warn,
            "rotate": thresholds.rotate,
        }
        entry["decision"] = None if decision is None else {
            "reason": decision.get("reason"),
            "level": decision.get("level"),
            "pct_of_baseline": decision.get("pct_of_baseline"),
        }
        payload["readings"].append(entry)
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


# ----------------------------------------------------------------- status


def command_status(args: argparse.Namespace, config: Any) -> int:
    """Report configuration, adapters and recent state."""
    state = State.load(config.state_path)
    events = EventLog(config.events_path)
    readings = ReadingStore(config.readings_dir).load_all()
    adapters = adapter_status(config)

    pending = state.pending_events()
    payload: Dict[str, Any] = {
        "version": __version__,
        "config_dir": config.dir,
        "config_source": config.source or "(built-in defaults)",
        "disable_file": {
            "path": config.disable_path,
            "present": config.disabled,
        },
        "paths": {
            "state": config.state_path,
            "events": config.events_path,
            "readings": config.readings_dir,
            "handovers": config.handover_dir,
        },
        "adapters": adapters,
        "pushed_readings": len(readings),
        "pending_events": [
            {
                "event_id": event.get("event_id"),
                "session_key": event.get("session_key"),
                "tool": event.get("tool"),
                "old_session": event.get("old_session"),
                "attempts": len(event.get("attempts") or []),
                "created_at": event.get("created_at"),
            }
            for event in pending
        ],
        "state": state.summary(),
        "recent_events": events.tail(limit=max(0, args.events)),
    }

    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return EXIT_OK

    print("custos %s" % __version__)
    print("config dir   : %s" % config.dir)
    print("config source: %s" % (config.source or "(built-in defaults)"))
    if config.disabled:
        print("kill switch  : ACTIVE (%s exists)" % config.disable_path)
    else:
        print("kill switch  : not set (%s)" % config.disable_path)
    print()
    print("adapters")
    print("  %-13s %-9s %-16s %s" % ("NAME", "DETECTED", "VERIFICATION", "ENABLED"))
    for row in adapters:
        print("  %-13s %-9s %-16s %s" % (
            row["name"],
            "yes" if row["detected"] else "no",
            row["verification"],
            "yes" if row["enabled"] else "no",
        ))
    for row in adapters:
        if row["verification_note"]:
            print("    %-13s %s" % (row["name"], row["verification_note"]))
    print()

    summary = payload["state"]
    print("state")
    print("  ledger entries   : %s" % summary["ledger_entries"])
    print("  tracked sessions : %s" % summary["tracked_sessions"])
    print("  pushed readings  : %s" % len(readings))
    print("  pending gates    : %s" % len(pending))
    # "Is it alive?" is the first question a silent scheduled tool raises, and
    # this line is the answer to it.
    last_run = float(summary.get("last_monitor_ts") or 0.0)
    print("  last monitor run : %s" % (format_age(last_run) if last_run else "never"))
    print("  gate attempts    : %s" % summary.get("gate_attempts", 0))
    for event in pending:
        print("    %s  %s  %d attempt(s)  since %s" % (
            event.get("event_id"), event.get("old_session", "")[:16],
            len(event.get("attempts") or []), event.get("created_at")))
    recent = payload["recent_events"]
    if recent:
        print("  recent events    :")
        for event in recent:
            print("    %s  %s" % (event.get("ts", "?"), event.get("kind", "?")))
    else:
        print("  recent events    : none")
    return EXIT_OK


# ----------------------------------------------------------------- report


def command_report(args: argparse.Namespace, config: Any) -> int:
    """Store a usage reading pushed by an external hook.

    This is the answer to "my tool already knows its token count". Anything that
    can run a shell command can hand the number over, and a pushed reading
    outranks anything inferred from a transcript because it was not inferred.
    """
    tokens = _parse_tokens(args.tokens)
    if tokens is None:
        message = "tokens must be a whole number, got %r" % args.tokens
        _emit_error(args, message)
        return EXIT_ERROR
    if tokens < 0:
        message = ("tokens must not be negative (got %d). A negative value is "
                   "rejected rather than stored, because it would read as a "
                   "full session in some tools and as an empty one in others."
                   % tokens)
        _emit_error(args, message)
        return EXIT_ERROR

    tool = _safe_label(args.tool)
    session = _safe_label(args.session)
    if not tool or not session:
        _emit_error(args, "tool and session must contain at least one usable character")
        return EXIT_ERROR

    window = args.window
    if window is not None and window <= 0:
        _emit_error(args, "window must be positive when given (got %d)" % window)
        return EXIT_ERROR

    reading = Reading(
        tool=tool,
        session=session,
        tokens=tokens,
        window=window,
        source=args.source or "report",
        ts=time.time(),
        compressed=bool(args.compressed),
        detail={"reported": True},
    )
    store = ReadingStore(config.readings_dir)
    path = store.push(reading)

    if args.json:
        json.dump({
            "stored": path,
            "reading": reading.to_dict(),
        }, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return EXIT_OK

    print("recorded %s %s at %s tokens%s -> %s" % (
        tool, session, format_tokens(tokens),
        " of %s" % format_tokens(window) if window else "",
        path))
    return EXIT_OK


def _parse_tokens(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip().replace("_", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _safe_label(value: str) -> str:
    """Keep a label that can be used as a file name, reject one that cannot.

    The store already sanitises for the filesystem, but a tool name consisting
    only of characters that get replaced would silently collapse two different
    tools into one directory, so it is rejected instead.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if not any(ch.isalnum() for ch in text):
        return ""
    if len(text) > 128:
        text = text[:128]
    return text


def _emit_error(args: argparse.Namespace, message: str) -> None:
    if getattr(args, "json", False):
        json.dump({"error": message}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print("custos: %s" % message, file=sys.stderr)


# ----------------------------------------------------------------- rotate


def command_rotate(args: argparse.Namespace, config: Any) -> int:
    """Run the rotation pipeline for one session."""
    if config.disabled and not args.dry_run:
        _report_refusal(args, config, "the kill switch is active",
                        "%s exists; remove it to re-enable rotation"
                        % config.disable_path)
        return EXIT_OK

    if args.dry_run:
        # A dry run mutates nothing, so there is nothing to serialise against,
        # and taking the lock would create a file in a directory the caller
        # explicitly asked not to touch.
        return _rotate_locked(args, config)

    with FileLock(config.lock_path) as lock:
        if not lock.acquired:
            _report_refusal(args, config, "another instance holds the lock",
                            config.lock_path)
            return EXIT_OK
        return _rotate_locked(args, config)


def _rotate_locked(args: argparse.Namespace, config: Any) -> int:
    reading, adapter, problems = _reading_for(config, args.tool, args.session)
    state = State.load(config.state_path)
    events = EventLog(config.events_path, state)
    session_key = "%s:%s" % (args.tool, args.session or "*")

    if reading is None:
        payload = {
            "tool": args.tool,
            "session": args.session,
            "rotated": False,
            "error": "no reading for that session",
            "problems": problems,
        }
        if args.json:
            _print_json(payload)
        else:
            print("custos: no reading for %s %s" % (
                args.tool, args.session or "(newest)"), file=sys.stderr)
            for problem in problems:
                print("  %s: %s" % (problem["tool"], problem["reason"]), file=sys.stderr)
        _maybe_close_dry(state, events, args.dry_run)
        return EXIT_ERROR

    session_key = "%s:%s" % (reading.tool, reading.session)
    thresholds = resolve_thresholds(config, reading)
    decision = evaluate(reading, thresholds)
    if decision is None:
        # A manual rotation is a deliberate act: the token line is not a
        # precondition, but the message should say the line was not reached so
        # the reader knows this was not triggered by the threshold.
        decision = {
            "reason": "manual",
            "level": "manual",
            "reading": reading,
            "tokens": reading.tokens,
            "thresholds": thresholds,
            "pct_of_baseline": round(
                100.0 * reading.tokens / max(1, thresholds.baseline.tokens), 1),
        }

    ctx = _rotation_context(config, state, events, reading, adapter, args.tool)
    gate = None
    if not args.no_gate and gate_runner_module.gate_settings(config)["enabled"]:
        gate = gate_runner_module.gate_runner(config, state, dry_run=args.dry_run)

    recent, recent_reason = _history_for(adapter, reading, config)
    result = do_rotation(
        adapter, reading, config, ctx, decision=decision, dry_run=args.dry_run,
        gate_runner=gate, recent_messages=recent,
        recent_unavailable_reason=recent_reason,
        run_gate_now=not args.dry_run,
        force=args.force,
    )

    messages = _messages_for_rotation(state, events, result, config, reading)

    if args.json:
        # The messages are part of the document below. Delivering them as well
        # would put human text in front of the JSON and leave stdout unparseable,
        # which is the one thing a caller asking for `--json` cannot work with.
        _print_json({
            "result": result.to_dict(),
            "messages": messages,
        })
    else:
        deliver(messages, config, dry_run=args.dry_run)
        if not messages:
            # A command asked to act on one specific session always says
            # something, even when it decided not to act. Silence would be
            # indistinguishable from a crash for a caller watching this output.
            print("custos: no action taken for %s" % session_key)
            for reason in result.defer_reasons:
                print("  %s" % reason)
    _maybe_close_dry(state, events, args.dry_run)

    if result.error:
        return EXIT_ERROR
    return EXIT_OK


def _rotation_context(
    config: Any, state: State, events: EventLog, reading: Reading,
    adapter: Any, tool: str,
) -> RotationContext:
    """Assemble what the pipeline needs to know about one session."""
    busy = None
    busy_detail = ""
    try:
        capabilities = adapter.capabilities() or {}
    except Exception:
        capabilities = {}
    busy_detection = bool(capabilities.get("busy_detection"))

    if busy_detection:
        try:
            ref = _resolve_ref(adapter, reading)
            busy = adapter.busy(ref)
            if busy:
                busy_detail = "adapter reports a turn in flight"
            elif busy is None:
                busy_detail = "the adapter could not read the session's turn state"
        except Exception as exc:
            busy = None
            busy_detail = "%s: %s" % (type(exc).__name__, exc)

    return RotationContext(
        session_key="%s:%s" % (reading.tool, reading.session),
        state=state,
        events=events,
        activity_quiet_minutes=config.get_int(
            "monitor", "activity_quiet_minutes", 10),
        idempotency_hours=config.get_float("rotation", "idempotency_hours", 6.0),
        busy=busy,
        busy_detail=busy_detail,
        busy_detection=busy_detection,
        mode=_rotation_mode(config, tool),
        start_hint=start_hint_for(adapter),
        extra={"activity source": "reading timestamp"},
    )


def _rotation_mode(config: Any, tool: str) -> str:
    configured = config.get("rotation", "modes", default={}) or {}
    if isinstance(configured, dict) and tool in configured:
        return str(configured[tool])
    return str(config.get("rotation", "mode", default="assisted"))


def _resolve_ref(adapter: Any, reading: Reading) -> Any:
    """The real reference for the session a reading describes.

    A reading carries an id and nothing else, so the adapter has to be asked
    where that session actually lives before anything can be read from it. The
    result is never ``None``: when the adapter cannot resolve the id it returns
    a ref with an empty path, and the reader reports the session as unreadable
    rather than as empty. Keep that distinction -- an empty result that means
    "could not look" is what the "not available" line in the handover is for.
    """
    from .adapters.base import SessionRef

    session_id = str(reading.session or "")
    resolver = getattr(adapter, "ref_for", None)
    if callable(resolver):
        try:
            ref = resolver(session_id)
            if ref is not None:
                if not getattr(ref, "tool", ""):
                    ref.tool = reading.tool
                return ref
        except Exception:
            # A stand-in adapter, or a broken store: fall through to the
            # placeholder so the caller still gets a truthful empty reading.
            pass
    return SessionRef(session=session_id, path="", tool=reading.tool)


def _history_for(adapter: Any, reading: Reading, config: Any):
    """Recent messages for the handover, or the reason there are none."""
    try:
        ref = _resolve_ref(adapter, reading)
        messages = adapter.recent_messages(ref, limit=12)
        if messages:
            return list(messages), None
        reason = ""
        try:
            reason = adapter.history_unavailable_reason()
        except Exception:
            reason = ""
        return [], reason or "this adapter does not read conversation history"
    except CustosError as exc:
        return [], str(exc)
    except AttributeError as exc:
        # A stand-in object that does not implement the optional history hooks.
        return [], "no conversation history is available for this session (%s)" % exc
    except Exception as exc:
        return [], "%s: %s" % (type(exc).__name__, exc)


def _messages_for_rotation(
    state: State, events: EventLog, result: Any, config: Any, reading: Reading
) -> List[Dict[str, Any]]:
    """The rotation message plus anything the gate fire produced."""
    messages: List[Dict[str, Any]] = []
    if result.message:
        messages.append(make_message(
            KIND_ROTATION, result.message, tool=reading.tool,
            session=reading.session, rotated=result.rotated,
            handover=result.handover_path, gate=result.gate_summary or None,
            deferred=result.deferred))
    if result.gate is None:
        return messages

    gate_result = _gate_result_from_dict(result.gate)
    messages.extend(gate_runner_module.evaluate_alerts(
        state, result.session_key, reading, gate_result, config))
    return messages


def _gate_result_from_dict(payload: Dict[str, Any]) -> Any:
    """Rebuild a GateResult from its serialised form.

    The pipeline stores the dictionary (it is what goes into the event log), but
    the alert logic wants the object. Rebuilding keeps one representation in the
    pipeline and one in the state file.
    """
    from .core.gate import GateAttempt, GateResult

    result = GateResult(
        ran=bool(payload.get("ran")),
        passed=bool(payload.get("passed")),
        mode=str(payload.get("mode") or "random"),
        prefix=str(payload.get("prefix") or ""),
        question_ids=list(payload.get("question_ids") or []),
        failures_in_a_row=int(payload.get("failures_in_a_row") or 0),
        invalid_in_a_row=int(payload.get("invalid_in_a_row") or 0),
        skipped_reason=str(payload.get("skipped_reason") or ""),
        error=str(payload.get("error") or ""),
        dry_run=bool(payload.get("dry_run")),
        plan=payload.get("plan") or {},
    )
    result.attempts = [GateAttempt.from_dict(item) for item in payload.get("attempts") or []]
    return result


def _report_refusal(args: argparse.Namespace, config: Any, reason: str, detail: str) -> None:
    """Say why a command declined to act, without pretending it did something."""
    if args.json:
        _print_json({"refused": True, "reason": reason, "detail": detail})
    else:
        print("custos: %s; nothing was done" % reason)
        print("  %s" % detail)


def _maybe_close(state: State, events: EventLog) -> None:
    """Save state and prune, at the one place every command passes through."""
    try:
        state.prune_ledger()
        state.prune_events()
        state.save()
    except OSError as exc:
        log("[custos] could not save state: %s" % exc)


def _maybe_close_dry(state: State, events: EventLog, dry_run: bool) -> None:
    """Close out a command, saving only when it was allowed to write."""
    if dry_run:
        return
    _maybe_close(state, events)


# ------------------------------------------------------------------- goal


def command_goal(args: argparse.Namespace, config: Any) -> int:
    """Run one goal across sessions, rotating when the line is reached.

    The driver does the work; this function only turns a configuration and an
    adapter into it, and turns its events into stdout.
    """
    if config.disabled:
        print("custos: the kill switch is active (%s); nothing was done"
              % config.disable_path)
        return EXIT_OK
    try:
        adapter = get_adapter(args.tool, config)
    except KeyError as exc:
        print("custos: %s" % exc, file=sys.stderr)
        return EXIT_ERROR

    cwd = os.path.abspath(os.path.expanduser(args.cwd or os.getcwd()))
    if not os.path.isdir(cwd):
        print("custos: cwd %s is not a directory" % cwd, file=sys.stderr)
        return EXIT_ERROR

    driver = goal_driver_module.GoalDriver(
        config,
        adapter,
        tool=args.tool,
        task_file=args.task_file,
        cwd=cwd,
        max_sessions=(args.max_sessions
                      if args.max_sessions is not None
                      else goal_driver_module.DEFAULT_MAX_SESSIONS),
        max_rounds_per_session=(args.max_rounds_per_session
                                if args.max_rounds_per_session is not None
                                else goal_driver_module.DEFAULT_MAX_ROUNDS_PER_SESSION),
        rotate_line=args.rotate_line,
        no_resume=args.no_resume,
        state_file=args.state_file,
        emit=_goal_emitter(args),
    )
    rc = driver.run()
    if rc == goal_driver_module.EXIT_LOCKED:
        # One line, and only one: a second driver is a scheduling mistake, not
        # something to explain to the machine that made it.
        print("goal: another driver holds the lock (%s)" % driver.state_path)
    return rc


def _goal_emitter(args: argparse.Namespace) -> Any:
    """Turn the driver's events into one stdout line (or one JSON object) each."""
    def emit(event: Dict[str, Any]) -> None:
        if args.json:
            print(json.dumps(event, ensure_ascii=False, sort_keys=True))
            return
        if event.get("event") == "outcome":
            print("[goal] outcome=%s sessions=%s rotations=%s state=%s" % (
                event.get("outcome"), event.get("sessions"),
                event.get("rotations"), event.get("state")))
            return
        print("[goal] %s round=%s event=%s session=%s tokens=%s "
              "rotate_line=%s detail=%s" % (
                  event.get("ts"), event.get("round"), event.get("event"),
                  event.get("session") or "-", event.get("tokens", 0),
                  event.get("rotate_line", 0), event.get("detail") or "-"))
    return emit


# ---------------------------------------------------------------- monitor


def command_monitor(args: argparse.Namespace, config: Any) -> int:
    """The scheduled entry point."""
    if args.once:
        return _monitor_pass(args, config)
    return _monitor_loop(args, config)


def _monitor_pass(args: argparse.Namespace, config: Any) -> int:
    """One pass: check, act, print whatever came out, exit 0."""
    if config.disabled and not args.dry_run:
        # The kill switch is silent on purpose: a disabled tool that keeps
        # explaining itself every five minutes is worse than one that stops.
        log("[custos] kill switch present (%s); standing down" % config.disable_path)
        return EXIT_OK

    if args.dry_run:
        return _monitor_guarded(args, config)

    with FileLock(config.lock_path) as lock:
        if not lock.acquired:
            log("[custos] another instance holds the lock; skipping this pass")
            return EXIT_OK
        return _monitor_guarded(args, config)


def _monitor_guarded(args: argparse.Namespace, config: Any) -> int:
    """Run the pass with the catch-all that keeps stdout clean."""
    try:
        return _monitor_work(args, config)
    except Exception as exc:  # never a raw traceback on stdout
        log("[custos] unexpected failure: %s: %s" % (type(exc).__name__, exc))
        if os.environ.get("CUSTOS_DEBUG"):
            import traceback

            traceback.print_exc()
        message = make_message(
            KIND_ALERT_INTERNAL, format_internal_alert("%s: %s" % (
                type(exc).__name__, exc)))
        _emit_messages(args, [message], config)


def _monitor_work(args: argparse.Namespace, config: Any) -> int:
    state = State.load(config.state_path)
    events = EventLog(config.events_path, state)

    throttle = config.get_int("monitor", "interval_minutes", 5)
    if not args.force and _throttled(state, throttle):
        log("[custos] last pass was less than %d minutes ago; skipping" % throttle)
        if not args.dry_run:
            state.save()
        return EXIT_OK
    if not args.dry_run:
        state.set_meta("last_monitor_ts", time.time())

    readings, problems, hard_failures = collect_readings(
        config, tools=args.tool, limit=10, include_undetected=False,
        explicit_tools=bool(args.tool))
    for problem in problems:
        log("[custos] %s: %s" % (problem["tool"], problem["reason"]))

    messages: List[Dict[str, Any]] = []

    # A session whose store could not be read is deferred, not evaluated: the
    # tool cannot tell whether it is busy, and guessing there is the one mistake
    # this pipeline exists to avoid.
    deferred_tools = set(hard_failures)

    # 1. Pending gate events first, oldest first -- exactly one per pass, so a
    #    backlog of them cannot turn one scheduled run into five rotations.
    pending = state.pending_events()
    if pending:
        handled = _service_pending_gate(args, config, state, events, pending[0])
        if handled is not None:
            messages.extend(handled)
            _emit_messages(args, messages, config, dry_run=args.dry_run)
            _maybe_close_dry(state, events, args.dry_run)
            return EXIT_OK

    # 2. Threshold triggers.
    eligible: List[Tuple[Reading, Dict[str, Any], Any]] = []
    for reading in readings:
        if reading.tool in deferred_tools:
            log("[custos] deferring %s: its store could not be read" % reading.tool)
            continue
        thresholds = resolve_thresholds(config, reading)
        decision = evaluate(reading, thresholds)
        if decision is None:
            continue
        if decision.get("level") == "warn":
            key = "warn:%s:%s:%s" % (reading.tool, reading.session,
                                     time.strftime("%Y-%m-%d"))
            if state.once(key):
                messages.append(make_message(
                    KIND_WARN, format_warning(reading, decision),
                    tool=reading.tool, session=reading.session))
            continue
        eligible.append((reading, decision, thresholds))

    if not eligible:
        _emit_messages(args, messages, config, dry_run=args.dry_run)
        _maybe_close_dry(state, events, args.dry_run)
        return EXIT_OK

    # The fullest session goes first: if only one rotation happens per pass, it
    # should be the one closest to being compacted.
    eligible.sort(key=lambda item: item[0].tokens, reverse=True)

    for reading, decision, _thresholds in eligible:
        session_key = "%s:%s" % (reading.tool, reading.session)
        if not args.force:
            pending_reasons = check_pending_gate(_pending_ctx(state, session_key))
            if pending_reasons:
                log("[custos] %s: %s" % (session_key, "; ".join(pending_reasons)))
                continue
        adapter = _adapter_for(config, reading)
        ctx = _rotation_context(config, state, events, reading, adapter, reading.tool)
        gate = None
        if gate_runner_module.gate_settings(config)["enabled"]:
            gate = gate_runner_module.gate_runner(config, state, dry_run=args.dry_run)
        recent, recent_reason = _history_for(adapter, reading, config)
        result = do_rotation(
            adapter, reading, config, ctx, decision=decision, dry_run=args.dry_run,
            gate_runner=gate, recent_messages=recent,
            recent_unavailable_reason=recent_reason,
            run_gate_now=False,  # the new session does not exist yet
            force=args.force,
        )
        if result.deferred:
            log("[custos] deferred %s: %s" % (
                session_key, "; ".join(result.defer_reasons)))
            continue
        messages.extend(_messages_for_rotation(state, events, result, config, reading))
        break  # one rotation per pass, as the mechanism intends

    _emit_messages(args, messages, config, dry_run=args.dry_run)
    _maybe_close_dry(state, events, args.dry_run)
    return EXIT_OK


def _pending_ctx(state: State, session_key: str) -> RotationContext:
    """A context object good enough for the pending-gate interlock check."""
    return RotationContext(session_key=session_key, state=state)


def _adapter_for(config: Any, reading: Reading) -> Any:
    """The adapter that can act on a reading, falling back to the hook stand-in.

    An adapter the user has turned off is not used, even when its tool is
    installed: ``enabled: false`` is a statement about what the tool may touch,
    and honouring it here is what stops a pending gate from spawning a CLI the
    user has explicitly retired.
    """
    if not config.adapter_enabled(reading.tool):
        return _PushedAdapter(reading.tool, config)
    try:
        adapter = get_adapter(reading.tool, config)
    except KeyError:
        return _PushedAdapter(reading.tool, config)
    try:
        if adapter.detect():
            return adapter
    except Exception:
        pass
    return _PushedAdapter(reading.tool, config)


def _service_pending_gate(
    args: argparse.Namespace, config: Any, state: State, events: EventLog,
    event: Dict[str, Any],
) -> Optional[List[Dict[str, Any]]]:
    """Run one gate fire for a rotation that is still waiting on a check."""
    session_key = str(event.get("session_key") or "")
    settings = gate_runner_module.gate_settings(config)
    if not settings["enabled"]:
        state.resolve_event(session_key, "gate_disabled")
        return None
    if not args.force and not gate_runner_module.retry_allowed(state, session_key, config):
        return None

    reading = _reading_from_event(state, event)
    if reading is None:
        state.resolve_event(session_key, "reading_gone")
        log("[custos] dropping pending gate for %s: no reading any more" % session_key)
        return None

    if args.dry_run:
        return [make_message(
            KIND_ROTATION,
            "[custos] dry run: would retry the gate for %s (%d attempt(s) so far)"
            % (session_key, len(event.get("attempts") or [])),
            tool=reading.tool)]

    adapter = _adapter_for(config, reading)
    result = gate_runner_module.run_gate_with_state(
        adapter, config, state, session_key=session_key)
    log("[custos] gate for %s: %s (%s)" % (
        session_key, result.summary(), gate_runner_module.describe_streaks(result)))

    messages = gate_runner_module.evaluate_alerts(
        state, session_key, reading, result, config)
    if result.passed:
        state.resolve_event(session_key, "gate_passed", gate=result.to_dict())
    elif not result.ran and _gate_skip_is_permanent(adapter):
        # A session whose adapter can never run a one-shot prompt will never
        # pass this check. Leaving the event pending would block that session's
        # next rotation six hours from now for no benefit, so it is closed as
        # skipped. A skip that could still go away -- no bank configured yet --
        # stays pending and is retried.
        state.resolve_event(session_key, "gate_skipped",
                            gate_skip_reason=result.skipped_reason)
        events.event("gate_skipped", session_key=session_key,
                     reason=result.skipped_reason)
    elif result.ran:
        # Keep it pending: the next fire continues the same streak.
        events.event("gate_attempts", session_key=session_key,
                     attempts=result.attempts_total, passed=False,
                     failures_in_a_row=result.failures_in_a_row,
                     invalid_in_a_row=result.invalid_in_a_row)
    return messages


def _gate_skip_is_permanent(adapter: Any) -> bool:
    """Whether this adapter will still be unable to answer on a later run."""
    try:
        capabilities = adapter.capabilities() or {}
    except Exception:
        return True
    return not capabilities.get("run_prompt")


def _reading_from_event(state: State, event: Dict[str, Any]) -> Optional[Reading]:
    """Rebuild a reading for a session that may no longer be discoverable."""
    tool = str(event.get("tool") or "")
    session = str(event.get("old_session") or "")
    if not tool or not session:
        return None
    return Reading(
        tool=tool,
        session=session,
        tokens=int(event.get("reading_tokens") or 0),
        window=None,
        source="state",
        ts=float(event.get("created_epoch") or 0.0),
        detail={"from_event": event.get("event_id")},
    )


def _throttled(state: State, interval_minutes: int) -> bool:
    last = state.get_meta("last_monitor_ts", 0.0)
    try:
        last = float(last or 0.0)
    except (TypeError, ValueError):
        return False
    if last <= 0:
        return False
    return (time.time() - last) < max(0, interval_minutes) * 60


def _emit_messages(
    args: argparse.Namespace, messages: List[Dict[str, Any]], config: Any,
    dry_run: bool = False,
) -> None:
    """Write the messages to stdout in the requested shape.

    Silence is the default: no messages means no output at all. ``--json``
    prints the array regardless, because a machine reading the output wants a
    document it can parse, not an empty stream.
    """
    if args.json:
        _print_json(messages)
        return
    deliver(messages, config, dry_run=dry_run)


def _monitor_loop(args: argparse.Namespace, config: Any) -> int:
    """Run passes until interrupted, honouring the kill switch between them."""
    stopping = {"requested": False}

    def _stop(signum, _frame):
        stopping["requested"] = True
        log("[custos] signal %d received; finishing the current pass" % signum)

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, _stop)
        except (ValueError, OSError):  # not the main thread, or unsupported
            pass

    interval = max(1, config.get_int("monitor", "interval_minutes", 5)) * 60
    try:
        while not stopping["requested"]:
            _monitor_pass(args, config)
            if stopping["requested"]:
                break
            # A force loop would spin as fast as the sleep allows, which is what
            # someone watching it wants; the normal loop waits out its interval.
            deadline = time.time() + (5 if args.force else interval)
            while time.time() < deadline and not stopping["requested"]:
                time.sleep(min(1.0, max(0.0, deadline - time.time())))
    finally:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
    log("[custos] monitor stopped")
    return EXIT_OK


# ------------------------------------------------------------------- gate


def command_gate(args: argparse.Namespace, config: Any) -> int:
    """Run the intelligence check by hand."""
    state = State.load(config.state_path)
    try:
        adapter = get_adapter(args.tool, config)
    except KeyError as exc:
        print("custos: %s" % exc, file=sys.stderr)
        return EXIT_ERROR

    settings = gate_runner_module.gate_settings(config)
    questions = None
    if args.question:
        try:
            bank = gate_runner_module.load_bank(settings) if settings.get("questions_path") \
                else load_questions(config.questions_path)
        except CustosError as exc:
            print("custos: %s" % exc, file=sys.stderr)
            return EXIT_ERROR
        wanted = {str(item) for item in args.question}
        questions = [question for question in bank if question.id in wanted]
        missing = wanted - {question.id for question in questions}
        if missing:
            print("custos: unknown question id(s): %s"
                  % ", ".join(sorted(missing)), file=sys.stderr)
            return EXIT_ERROR

    attempts_seen: List[Dict[str, Any]] = []

    def on_attempt(attempt):
        attempts_seen.append(attempt.to_dict())
        if not args.json:
            print(gate_runner_module.attempt_message(attempt, args.tool)["text"])

    result = gate_runner_module.run_gate_with_state(
        adapter, config, state,
        mode=args.mode,
        attempts=args.attempts,
        interval_seconds=args.interval,
        seed=args.seed,
        dry_run=args.dry_run,
        on_attempt=None if args.json else on_attempt,
        questions=questions,
    )
    state.save()

    if args.json:
        _print_json(result.to_dict())
    else:
        print()
        if result.dry_run:
            print("dry run: would ask %s" % (", ".join(result.question_ids) or "(none)"))
            print("  tool    : %s %s" % (args.tool, " ".join(
                result.plan.get("command") or [])))
            print("  attempts: %s, interval %ss, timeout %ss" % (
                result.plan.get("attempts"), result.plan.get("interval_seconds"),
                result.plan.get("attempt_timeout")))
            print("  prefix  : %s" % truncate(result.prefix, 120))
        elif not result.ran:
            print("gate skipped: %s" % result.skipped_reason)
        else:
            verdict = "PASSED" if result.passed else (
                "NO ANSWER" if result.invalid_in_a_row and not result.failures_in_a_row
                else "FAILED")
            print("gate %s: %s" % (verdict, result.summary()))
            if result.integrity_errors:
                for problem in result.integrity_errors:
                    print("  refused: %s" % problem)
        for attempt in attempts_seen:
            pass  # already printed as it happened

    # Skipped is not failed: nothing was asked, so there is no verdict to report
    # as an error. A dry run likewise.
    if result.dry_run or not result.ran:
        return EXIT_OK
    return EXIT_OK if result.passed else EXIT_ERROR


# ------------------------------------------------------------------- demo


def command_demo(args: argparse.Namespace, config: Any) -> int:
    """Run the whole pipeline against synthetic sessions.

    Delegated to :mod:`custos.demo` so the scenario reads as a script rather
    than as argument handling.
    """
    from . import demo as demo_module

    return demo_module.run(args, config)


# ----------------------------------------------------------------- doctor


def command_doctor(args: argparse.Namespace, config: Any) -> int:
    """Check the installation end to end and report what is missing."""
    checks: List[Dict[str, str]] = []

    def record(status: str, name: str, detail: str = "") -> None:
        checks.append({"status": status, "check": name, "detail": detail})

    major, minor = sys.version_info[:2]
    if (major, minor) >= MIN_PYTHON:
        record("ok", "python", "%d.%d.%d" % sys.version_info[:3])
    else:
        record("fail", "python", "%d.%d.%d is older than the required %d.%d"
               % (major, minor, sys.version_info[2], MIN_PYTHON[0], MIN_PYTHON[1]))

    try:
        config_module.load_config(config.dir)
        record("ok", "config", config.source or "(built-in defaults)")
    except CustosError as exc:
        record("fail", "config", str(exc))

    if not os.path.isdir(config.dir):
        record("warn", "config dir", "%s does not exist yet; it is created on "
                                     "the first write" % config.dir)
    else:
        record("ok", "config dir", config.dir)

    bank_path = config.questions_path
    if os.path.exists(bank_path):
        report = validate_bank(bank_path)
        if report["ok"]:
            record("ok", "question bank", "%d question(s) in %s"
                   % (report["total"], bank_path))
        else:
            record("fail", "question bank", "%d problem(s); run "
                   "`custos questions validate`" % report.get("errors", 0))
        if report.get("warnings"):
            record("warn", "question bank warnings",
                   "%d warning(s)" % report["warnings"])
    else:
        record("warn", "question bank",
               "no bank at %s; copy config/questions.example.yaml there to "
               "enable the gate" % bank_path)

    for label, path in (
        ("state dir", config.dir),
        ("handover dir", config.handover_dir),
        ("readings dir", config.readings_dir),
    ):
        record(*_check_writable(label, path))

    for row in adapter_status(config, include_synthetic=False):
        if not row["enabled"]:
            record("info", "adapter %s" % row["name"], "disabled in configuration")
            continue
        if not row["detected"]:
            record("info", "adapter %s" % row["name"],
                   "not detected (%s)" % row["verification"])
            continue
        detail = row["verification"]
        sample = _sample_read(config, row["name"])
        if sample is None:
            record("warn", "adapter %s" % row["name"],
                   "detected (%s) but no session could be read" % detail)
        elif isinstance(sample, str):
            record("info", "adapter %s" % row["name"],
                   "detected (%s); sample read failed: %s" % (detail, sample))
        else:
            record("ok", "adapter %s" % row["name"],
                   "%s; newest session %s at %s tokens"
                   % (detail, sample.session[:12], format_tokens(sample.tokens)))

    if config.disabled:
        record("info", "kill switch", "ACTIVE (%s exists); nothing will rotate"
               % config.disable_path)
    else:
        record("ok", "kill switch", "not set")

    record(*_wrapper_check())

    payload = {
        "config_dir": config.dir,
        "checks": checks,
        "ok": all(check["status"] != "fail" for check in checks),
    }

    if args.json:
        _print_json(payload)
    else:
        print("custos doctor")
        print("config: %s" % config.dir)
        print()
        for check in checks:
            print("[%s] %-24s %s" % (
                check["status"], check["check"], check["detail"]))
        failed = [check for check in checks if check["status"] == "fail"]
        warned = [check for check in checks if check["status"] == "warn"]
        print()
        if failed:
            print("%d check(s) failed, %d warning(s)" % (len(failed), len(warned)))
        elif warned:
            print("no failures, %d warning(s)" % len(warned))
        else:
            print("all checks passed")

    return EXIT_ERROR if not payload["ok"] else EXIT_OK


def _check_writable(label: str, path: str) -> Tuple[str, str, str]:
    """Whether a directory can be created and written to.

    Nothing is created or probed unless the directory already exists. A health
    check that makes the thing it is checking, or that leaves a probe file in a
    config directory it was only asked to inspect, is not a read-only check --
    and this one has to be safe to run before any setup has happened.
    """
    if not os.path.isdir(path):
        parent = _nearest_existing(path)
        if parent is None:
            return ("warn", label, "%s does not exist yet" % path)
        if os.access(parent, os.W_OK):
            return ("ok", label, "%s (will be created under %s)" % (path, parent))
        return ("fail", label, "cannot create %s: %s is not writable" % (path, parent))
    if os.access(path, os.W_OK):
        return ("ok", label, path)
    return ("fail", label, "%s exists but is not writable" % path)


def _nearest_existing(path: str) -> Optional[str]:
    """Closest existing ancestor of ``path``, or ``None``."""
    candidate = os.path.abspath(path)
    while True:
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return candidate if os.path.isdir(candidate) else None
        candidate = parent
        if os.path.isdir(candidate):
            return candidate


def _sample_read(config: Any, name: str) -> Any:
    """One real read attempt, reported as information rather than as a failure."""
    try:
        adapter = get_adapter(name, config)
        sessions = adapter.sessions(limit=1)
        if not sessions:
            return None
        return adapter.read_usage(sessions[0])
    except CustosError as exc:
        return str(exc)
    except Exception as exc:
        return "%s: %s" % (type(exc).__name__, exc)


def _wrapper_check() -> Tuple[str, str, str]:
    """Whether `custos` resolves on PATH, when it was installed by wrapper."""
    import shutil

    found = shutil.which("custos")
    if found:
        return ("ok", "cli on PATH", found)
    return ("info", "cli on PATH",
            "not found; either install it (install.sh) or run `python3 -m custos`")


# -------------------------------------------------------------- questions


def command_questions(args: argparse.Namespace, config: Any) -> int:
    """List or validate the configured question bank."""
    path = config.questions_path
    if not os.path.exists(path):
        message = (
            "no question bank at %s; copy config/questions.example.yaml into "
            "your config directory to get started" % path)
        if args.json:
            _print_json({"error": message, "path": path})
        else:
            print(message, file=sys.stderr)
        return EXIT_ERROR

    if args.action == "validate":
        report = validate_bank(path)
        if args.json:
            _print_json(report)
        else:
            print("question bank: %s" % path)
            print("%d question(s); %d error(s), %d warning(s)" % (
                report["total"], report.get("errors", 0), report.get("warnings", 0)))
            for problem in report["problems"]:
                print("  [%s] %-24s %s" % (
                    problem["severity"], problem["question"], problem["problem"]))
            if report["ok"]:
                print()
                print("bank is valid")
        return EXIT_OK if report["ok"] else EXIT_ERROR

    questions = load_questions(path)
    summary = summarise_bank(questions)

    if args.json:
        _print_json({
            "path": path,
            "summary": summary,
            "questions": [question.to_dict() for question in questions],
        })
        return EXIT_OK

    print("question bank: %s" % path)
    print("questions    : %d (%s)" % (
        summary["total"],
        ", ".join("%s=%d" % item for item in sorted(summary["by_match"].items()))))
    print()
    for row in describe_questions(questions):
        print("  %s" % row)
    return EXIT_OK


# ------------------------------------------------------------------ shared


def _print_json(payload: Any) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
