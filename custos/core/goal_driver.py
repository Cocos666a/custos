"""The goal driver: run one long task across many sessions.

`custos monitor` reacts to a session that is nearly full; the goal driver keeps
a task moving until it is done. Each round either continues the current session
(below the rotate line) or rotates into a fresh one (at or above it), so a task
larger than one context window can still be finished without a person watching.

Three files carry the state between rounds:

``<cwd>/.custos-goal-state.json``
    What has happened: the sessions seen, the rotations made, the resolved
    rotate line and the outcome. Written atomically after every change.
``<cwd>/custos-goal-progress.md``
    The agent's own log. It is the only memory the replacement session is
    guaranteed to have beyond the handover, so every round prompt asks for one
    dated line.
``<config>/handovers/*.md``
    The snapshot written at each rotation, by the same writer the monitor uses.

Every event produces exactly one stdout line, because the driver is meant to be
run under a scheduler or watched by a person and a runner that says two things
about one event is a runner nobody can grep.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from ..adapters.base import Reading
from .handover import write_handover
from .util import FileLock, atomic_write_json, atomic_write_text, now_iso, truncate

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_LOCKED = 3

DEFAULT_MAX_SESSIONS = 5
DEFAULT_MAX_ROUNDS_PER_SESSION = 10
PROGRESS_FILENAME = "custos-goal-progress.md"
STATE_FILENAME = ".custos-goal-state.json"
COMPLETE_MARKER = "GOAL_COMPLETE"

# The prompts are part of the contract: the agent is told exactly which file to
# maintain and exactly which line means "done", so the completion check does not
# depend on prose interpretation.
INITIAL_PROMPT = (
    "You are starting a goal task. The task is: {task}. "
    "Also saved at {task_file}. Work toward it step by step. Maintain "
    "custos-goal-progress.md (append one dated line per step). When the goal is "
    "fully achieved append `GOAL_COMPLETE` to custos-goal-progress.md and end "
    "your final reply with the single line GOAL_COMPLETE."
)
RESUME_PROMPT = (
    "Continue the goal. Read custos-goal-progress.md in this directory. Do the "
    "next concrete step toward the goal now. Append one dated line to "
    "custos-goal-progress.md describing what you did. If the goal is fully "
    "achieved, append a line `GOAL_COMPLETE` to custos-goal-progress.md and end "
    "your final reply with the single line GOAL_COMPLETE."
)
FRESH_CONTINUATION_PROMPT = (
    "New session for an ongoing goal. Read custos-goal-progress.md and the "
    "handover at {handover}. Continue the work."
)

OUTCOME_RUNNING = "running"
OUTCOME_COMPLETED = "completed"
OUTCOME_MAX_SESSIONS = "stopped_max_sessions"
OUTCOME_MAX_ROUNDS = "stopped_max_rounds"
OUTCOME_FAILED = "failed"
OUTCOME_KILLED_SWITCH = "killed_switch"

# The task text carried in the first prompt. Long enough for a real brief, short
# enough that the spawn command line stays well under any argument limit.
TASK_PROMPT_CHARS = 4000


class GoalLockLost(RuntimeError):
    """The state file lock was taken by another driver mid-run."""


def resolve_rotate_line(config: Any, override: Optional[int] = None) -> int:
    """The token line that triggers rotation for the goal run.

    ``--rotate-line`` wins. Otherwise the same thresholds the monitor uses are
    resolved, so a goal run rotates at the configured line rather than at a
    number invented here.
    """
    if override is not None and int(override) >= 0:
        return int(override)
    from .thresholds import resolve_thresholds

    try:
        return int(resolve_thresholds(config, None).rotate)
    except Exception:
        return 0


class GoalDriver:
    """Runs one goal across sessions, rotating when the line is reached."""

    def __init__(
        self,
        config: Any,
        adapter: Any,
        *,
        tool: str,
        task_file: str,
        cwd: str,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        max_rounds_per_session: int = DEFAULT_MAX_ROUNDS_PER_SESSION,
        rotate_line: Optional[int] = None,
        no_resume: bool = False,
        state_file: Optional[str] = None,
        progress_file: Optional[str] = None,
        emit: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.adapter = adapter
        self.tool = tool
        self.task_file = os.path.abspath(task_file)
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.max_sessions = max(1, int(max_sessions))
        self.max_rounds_per_session = max(1, int(max_rounds_per_session))
        self.rotate_line = resolve_rotate_line(config, rotate_line)
        self.no_resume = bool(no_resume)
        self.state_path = os.path.abspath(state_file or os.path.join(
            self.cwd, STATE_FILENAME))
        self.progress_path = os.path.abspath(progress_file or os.path.join(
            self.cwd, PROGRESS_FILENAME))
        self.emit = emit or (lambda event: None)

        self.state: Dict[str, Any] = {}
        self._lock: Optional[FileLock] = None
        self._round = 0
        self._total_rounds = 0
        self._consecutive_failures = 0

    # -- state -----------------------------------------------------------
    def _new_state(self) -> Dict[str, Any]:
        return {
            "tool": self.tool,
            "task_file": self.task_file,
            "cwd": self.cwd,
            "started_at": now_iso(),
            "pid": os.getpid(),
            "rotate_line": self.rotate_line,
            "sessions": [],
            "rotations": [],
            "outcome": OUTCOME_RUNNING,
        }

    def _save(self) -> None:
        """Write the state atomically, then move the lock to the new file.

        ``os.replace`` swaps the inode under the ``flock``, and a lock is held
        by the descriptor, not the path. So the lock is re-taken on the new file
        *before* the old one is released: releasing first would leave a window
        in which a second driver could take the lock and start a parallel run.
        """
        atomic_write_json(self.state_path, self.state)
        if self._lock is not None:
            replacement = FileLock(self.state_path)
            if not replacement.acquire():
                raise GoalLockLost(self.state_path)
            previous, self._lock = self._lock, replacement
            previous.release()

    def _current_session(self) -> Optional[str]:
        sessions = self.state.get("sessions") or []
        if not sessions:
            return None
        return str(sessions[-1].get("session") or "") or None

    def _session_entry(self, session: str) -> Dict[str, Any]:
        for entry in self.state.get("sessions") or []:
            if entry.get("session") == session:
                return entry
        entry = {
            "session": session,
            "rounds": 0,
            "tokens_last": 0,
            "started_at": now_iso(),
        }
        self.state.setdefault("sessions", []).append(entry)
        return entry

    # -- events ----------------------------------------------------------
    def _event(self, event: str, session: Optional[str] = None,
               tokens: int = 0, detail: str = "") -> None:
        self.emit({
            "event": event,
            "ts": now_iso(),
            "round": self._round,
            "session": session or "",
            "tokens": int(tokens or 0),
            "rotate_line": self.rotate_line,
            "detail": detail,
        })

    # -- reading ---------------------------------------------------------
    def _tokens_for(self, session: Optional[str]) -> int:
        """Latest usage for a session, or ``0`` when it cannot be read.

        ``0`` is not "empty"; it is "no evidence this session is full". A store
        that cannot be read must not cause a rotation that discards work.
        """
        if not session:
            return 0
        try:
            ref = self.adapter.ref_for(session)
            return int(self.adapter.read_usage(ref).tokens or 0)
        except Exception:
            return 0

    def _reading_for(self, session: str, tokens: int) -> Reading:
        """A reading for the handover; synthesised when the adapter cannot read."""
        try:
            ref = self.adapter.ref_for(session)
            return self.adapter.read_usage(ref)
        except Exception:
            return Reading(
                tool=self.tool,
                session=session,
                tokens=int(tokens or 0),
                source="state",
                ts=time.time(),
                detail={"from": "goal driver state"},
            )

    # -- progress --------------------------------------------------------
    def _ensure_progress_file(self, task_text: str) -> None:
        if os.path.exists(self.progress_path):
            return
        atomic_write_text(
            self.progress_path,
            "# Goal task\n\n%s\n" % task_text.strip())

    def _progress_says_complete(self) -> bool:
        try:
            with open(self.progress_path, "r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    if line.strip() == COMPLETE_MARKER:
                        return True
        except OSError:
            return False
        return False

    @staticmethod
    def _output_says_complete(text: str) -> bool:
        """Whether the round's reply ends with the marker.

        A round run with ``--output-format json`` prints one JSON document whose
        ``result`` field holds the reply; the marker lives there, not on a line
        of the document itself. Both spellings are checked, because the driver
        cannot know which one the adapter's CLI produced.
        """
        for candidate in _reply_texts(text):
            if any(line.strip() == COMPLETE_MARKER
                   for line in candidate.splitlines()):
                return True
        return False

    # -- spawning --------------------------------------------------------
    def _session_files(self) -> List[str]:
        lister = getattr(self.adapter, "session_files", None)
        if not callable(lister):
            return []
        try:
            return list(lister())
        except Exception:
            return []

    def _run_prompt(self, prompt: str, extra_args: Optional[List[str]] = None,
                    discover: bool = False) -> Tuple[Any, Optional[str]]:
        """Run one prompt and, when asked, work out which session it started."""
        before = self._session_files() if discover else []
        t0 = time.time()
        result = self.adapter.run_prompt(
            prompt, cwd=self.cwd, extra_args=extra_args or [])
        session = None
        if discover:
            session = self._discover_session(result, before, t0)
        return result, session

    def _discover_session(self, result: Any, before: List[str], t0: float) -> Optional[str]:
        """The id of the session a fresh spawn created, if it can be told."""
        if getattr(result, "session", None):
            return str(result.session)
        from_output = _session_id_from_json(getattr(result, "text", "") or "")
        if from_output:
            return from_output
        appeared = [path for path in self._session_files()
                    if path not in before and _mtime(path) > t0 - 2]
        if not appeared:
            return None
        newest = max(appeared, key=_mtime)
        return _session_id_from_path(newest, self.tool)

    # -- rounds ----------------------------------------------------------
    def _round_once(self, task_text: str) -> None:
        """Run exactly one round.

        The round does one of three things: start the first session, resume the
        current one, or rotate into a new one. ``state["outcome"]`` is left at
        ``running`` unless the round decided the run is over.
        """
        current = self._current_session()
        if current is None:
            self._fresh_round(INITIAL_PROMPT.format(
                task=task_text[:TASK_PROMPT_CHARS].strip(),
                task_file=self.task_file))
            return

        tokens = self._tokens_for(current)
        if self.no_resume or tokens >= self.rotate_line:
            self._rotate_round(current, tokens)
        else:
            self._resume_round(current, tokens)

    def _resume_round(self, session: str, tokens: int = 0) -> None:
        """Continue the current session, leaving the id unchanged."""
        self._event("round_start", session=session, tokens=tokens, detail="resume")
        result, _ = self._run_prompt(
            RESUME_PROMPT, extra_args=self._resume_args(session))
        self._finish_round(session, result, "resume")

    def _resume_args(self, session: str) -> List[str]:
        """How each CLI is asked to continue an existing session."""
        if self.tool == "claude_code":
            return ["--resume", session, "--output-format", "json"]
        if self.tool == "codex":
            return ["resume", session]
        return []

    def _fresh_round(self, prompt: str) -> Optional[str]:
        """Start a new session for the goal. Returns the id when discovered."""
        self._event("round_start", detail="fresh")
        result, session = self._run_prompt(prompt, discover=True)
        if session:
            self._session_entry(session)
        self._finish_round(session, result, "fresh")
        return session

    def _finish_round(self, session: Optional[str], result: Any, kind: str) -> None:
        """Record one round's tokens, failure streak and completion state."""
        ok = bool(getattr(result, "rc", -1) == 0 and not getattr(result, "timed_out", False))
        tokens = self._tokens_for(session)
        if session:
            entry = self._session_entry(session)
            entry["rounds"] = int(entry.get("rounds") or 0) + 1
            entry["tokens_last"] = tokens
        self._consecutive_failures = 0 if ok else self._consecutive_failures + 1
        self._save()
        if ok:
            self._event("round_ok", session=session, tokens=tokens, detail=kind)
            self._maybe_complete(result, session)
        else:
            reason = getattr(result, "stderr", "") or "spawn failed"
            self._event("round_failed", session=session, tokens=tokens,
                        detail=truncate(reason, 200))

    def _rotate_round(self, session: str, tokens: int) -> None:
        """Write a handover and rotate; fall back to a fresh session if needed."""
        reading = self._reading_for(session, tokens)
        recent, reason = self._history_for(reading)
        trigger = "tokens %d >= line %d" % (tokens, self.rotate_line)
        try:
            os.makedirs(self.config.handover_dir, exist_ok=True)
            handover_path = write_handover(
                self.config.handover_dir,
                reading=reading,
                trigger_reason="goal round %d: %s" % (self._round, trigger),
                recent_messages=recent,
                recent_unavailable_reason=reason,
                extra={"session key": "%s:%s" % (self.tool, session),
                       "rotation mode": "full-auto",
                       "goal round": self._round},
            )
        except OSError as exc:
            self._fail(session, tokens, "handover write failed: %s" % exc)
            return

        result = self.adapter.rotate(session, handover_path)
        rotated = bool(getattr(result, "rotated", False))
        new_session = getattr(result, "new_session", None)
        error = getattr(result, "error", None)
        self.state.setdefault("rotations", []).append({
            "ts": now_iso(),
            "old": session,
            "new": new_session,
            "tokens": tokens,
            "rotate_line": self.rotate_line,
            "trigger": trigger,
            "handover": handover_path,
            "mode": getattr(result, "mode", "full-auto") or "full-auto",
        })
        self._save()
        if not rotated:
            self._fail(session, tokens, error or "rotation did not start a session")
            return

        self._event("rotate", session=new_session or session, tokens=tokens,
                    detail="old=%s new=%s handover=%s" % (
                        session, new_session or "-", handover_path))
        if new_session:
            entry = self._session_entry(str(new_session))
            entry["rounds"] = int(entry.get("rounds") or 0) + 1
            entry["tokens_last"] = self._tokens_for(str(new_session))
            self._consecutive_failures = 0
            self._save()
            # The rotation spawn is a full agent run: it may have finished the
            # goal (the continuation prompt says to continue "until it is
            # done"). Check completion before spending another round on it.
            self._maybe_complete(result, str(new_session))
            return

        # The rotation started a session nobody can identify. Continuing the
        # goal needs a session id, so a fresh one is started with the handover
        # in hand -- the same artefact the unidentified session was given.
        self._fresh_round(FRESH_CONTINUATION_PROMPT.format(handover=handover_path))

    def _fail(self, session: Optional[str], tokens: int, reason: str) -> None:
        self.state["outcome"] = OUTCOME_FAILED
        self.state["error"] = reason
        self._save()
        self._event("failed", session=session, tokens=tokens,
                    detail=truncate(reason, 200))

    def _history_for(self, reading: Reading) -> Tuple[List[str], Optional[str]]:
        """Recent messages for the handover, or the reason there are none."""
        try:
            ref = self.adapter.ref_for(reading.session)
            messages = self.adapter.recent_messages(ref, limit=12)
            if messages:
                return list(messages), None
            try:
                reason = self.adapter.history_unavailable_reason()
            except Exception:
                reason = ""
            return [], reason or "this adapter does not read conversation history"
        except Exception as exc:
            return [], "%s: %s" % (type(exc).__name__, exc)

    def _maybe_complete(self, result: Any, session: Optional[str]) -> bool:
        text = getattr(result, "text", "") or ""
        if not (self._output_says_complete(text) or self._progress_says_complete()):
            return False
        self.state["outcome"] = OUTCOME_COMPLETED
        self._save()
        self._event("completed", session=session, tokens=self._tokens_for(session),
                    detail="GOAL_COMPLETE")
        return True

    # -- the loop --------------------------------------------------------
    def run(self) -> int:
        """Run until the goal is done or a stop condition fires."""
        if self._kill_switch():
            self.state = self._new_state()
            self.state["outcome"] = OUTCOME_KILLED_SWITCH
            self._event("killed_switch", detail="kill switch present at start")
            self._final()
            return EXIT_OK

        try:
            with open(self.task_file, "r", encoding="utf-8", errors="replace") as stream:
                task_text = stream.read()
        except OSError as exc:
            self.state = self._new_state()
            self._fail(None, 0, "cannot read the task file: %s" % exc)
            self._final()
            return EXIT_ERROR

        self._ensure_progress_file(task_text)

        lock = FileLock(self.state_path)
        if not lock.acquire():
            return EXIT_LOCKED
        self._lock = lock

        self.state = self._new_state()
        self._save()
        self._event("start", detail="tool=%s task=%s" % (
            self.tool, os.path.basename(self.task_file)))

        try:
            self._loop(task_text)
        except GoalLockLost:
            return EXIT_LOCKED
        else:
            self._final()
            return EXIT_ERROR if self.state.get("outcome") == OUTCOME_FAILED else EXIT_OK
        finally:
            if self._lock is not None:
                self._lock.release()
                self._lock = None

    def _loop(self, task_text: str) -> None:
        cap = self.max_rounds_per_session * self.max_sessions
        while True:
            if self._kill_switch():
                self.state["outcome"] = OUTCOME_KILLED_SWITCH
                self._save()
                self._event("killed_switch", detail="kill switch appeared between rounds")
                return
            sessions = len(self.state.get("sessions") or [])
            if sessions > self.max_sessions:
                self.state["outcome"] = OUTCOME_MAX_SESSIONS
                self._save()
                self._event("stopped_max_sessions",
                            detail="sessions=%d max=%d" % (sessions, self.max_sessions))
                return
            if self._total_rounds > cap:
                self.state["outcome"] = OUTCOME_MAX_ROUNDS
                self._save()
                self._event("stopped_max_rounds",
                            detail="rounds=%d cap=%d" % (self._total_rounds, cap))
                return
            if self._consecutive_failures >= 2:
                self._fail(None, 0, "two consecutive rounds failed to spawn")
                return

            self._round += 1
            self._total_rounds += 1
            self._round_once(task_text)
            if self.state.get("outcome") != OUTCOME_RUNNING:
                return

    # -- helpers ---------------------------------------------------------
    def _kill_switch(self) -> bool:
        try:
            return bool(self.config.disabled)
        except Exception:
            return os.path.exists(os.path.join(self.config.dir, "DISABLE"))

    def _final(self) -> None:
        self.emit({
            "event": "outcome",
            "outcome": self.state.get("outcome", OUTCOME_RUNNING),
            "sessions": len(self.state.get("sessions") or []),
            "rotations": len(self.state.get("rotations") or []),
            "state": self.state_path,
            "rotate_line": self.rotate_line,
            "ts": now_iso(),
        })


# --------------------------------------------------------------- discovery


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _reply_texts(text: str) -> List[str]:
    """The plain reply text, plus the ``result`` field of a JSON one-shot output."""
    text = text or ""
    texts = [text]
    for candidate in [text.strip()] + [line.strip() for line in text.splitlines()]:
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("result"), str):
            texts.append(payload["result"])
    return texts


def _session_id_from_json(text: str) -> Optional[str]:
    """``session_id`` from a JSON one-shot result, when the tool prints one."""
    text = (text or "").strip()
    if not text:
        return None
    for candidate in [text] + list(reversed(text.splitlines())):
        candidate = candidate.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("session_id"):
            return str(payload["session_id"])
    return None


def _session_id_from_path(path: str, tool: str) -> str:
    """The session id a store filename implies.

    Claude Code names a transcript after the id; Codex appends the uuid to a
    timestamp, so the trailing 36 characters are taken when they look like one.
    """
    stem = os.path.basename(path)
    if stem.endswith(".jsonl"):
        stem = stem[: -len(".jsonl")]
    if tool == "codex":
        candidate = stem[-36:]
        if candidate.count("-") == 4:
            return candidate
    return stem
