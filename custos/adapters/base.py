"""Adapter contract.

An adapter knows one agent tool: where it keeps its sessions, how to read the
context occupancy out of them, and how to start a new session. Everything above
this layer works on :class:`~custos.core.usage.Reading` and never sees a
tool-specific format.

Three rules apply to every adapter:

* **read-only on foreign stores.** Transcripts and databases belong to the other
  tool. They are opened read-only (``mode=ro`` for SQLite, plain reads for
  files) and never written to.
* **never raise past the boundary.** Failures are reported as
  :class:`~custos.core.usage.AdapterNotInstalled`, ``NoSessions`` or
  ``ReadFailed`` so a scheduled run can report the problem and continue.
* **state the verification level.** ``VERIFICATION`` says how much the
  implementation has actually been checked against. Claiming more than was
  verified is worse than admitting a gap.
"""

from __future__ import annotations

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.usage import AdapterNotInstalled, NoSessions, Reading, ReadFailed
from ..core.util import clean_env

VERIFIED = "verified"
DOC_VERIFIED = "doc-verified"
EXPERIMENTAL = "experimental"

# `rotate_mode` values live in adapter configuration. The reported RotateResult
# mode keeps the hyphenated spelling every other adapter already uses.
ROTATE_MODE_ASSISTED = "assisted"
ROTATE_MODE_FULL_AUTO = "full_auto"
ROTATE_RESULT_FULL_AUTO = "full-auto"

DEFAULT_ROTATE_TIMEOUT = 900.0

# The prompt a full-auto rotation sends to the replacement session. `{handover}`
# and `{session}` are substituted; a configured `continuation_prompt_file`
# replaces the whole template.
CONTINUATION_PROMPT_TEMPLATE = (
    "You are the continuation of a previous session that ran out of context window.\n"
    "Read the handover file at: {handover}\n"
    "Your FIRST output line must be exactly: CONTINUATION RECAP: <2-3 sentences: "
    "where the previous session left off, and what you will do next>\n"
    "Then continue the work described in the handover until it is done.\n"
)

# What the replacement session has to start its reply with. Its presence in the
# spawn output is evidence that the new session actually read the handover.
RECAP_MARKER = "CONTINUATION RECAP:"

# How much of stderr a failed spawn carries into its error string.
ERROR_TAIL_CHARS = 2000

# How many sessions the default `ref_for` looks through. Generous on purpose: a
# session outside this window is treated as not found, and "not found" means the
# handover reports its history as unavailable. Adapters with a cheaper exact
# lookup override `ref_for` and never come near this bound.
REF_SCAN_LIMIT = 2000


@dataclass
class SessionRef:
    """A session discovered on disk, with no conversation content attached."""

    session: str
    path: str
    updated: float = 0.0
    tool: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session": self.session,
            "path": self.path,
            "updated": self.updated,
            "tool": self.tool,
        }


@dataclass
class RotateResult:
    """Outcome of an adapter rotation attempt.

    ``mode`` is ``full-auto`` when the adapter started a new session itself, and
    ``assisted`` when it only prepared the ground and a human (or a later CLI
    call) has to start the session.
    """

    mode: str = "assisted"
    rotated: bool = False
    new_session: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "rotated": self.rotated,
            "new_session": self.new_session,
            "detail": self.detail,
            "error": self.error,
        }


@dataclass
class PromptResult:
    """Outcome of running a one-shot prompt through the tool."""

    text: str = ""
    rc: int = -1
    timed_out: bool = False
    session: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    stderr: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "rc": self.rc,
            "timed_out": self.timed_out,
            "session": self.session,
            "usage": self.usage,
        }


class Adapter(ABC):
    """Base class for every tool integration."""

    name: str = "adapter"
    VERIFICATION: str = EXPERIMENTAL
    VERIFICATION_NOTE: str = ""

    def __init__(self, config: Any = None) -> None:
        self.config = config

    # -- identity --------------------------------------------------------
    @property
    def tool(self) -> str:
        return self.name

    def settings(self) -> Dict[str, Any]:
        """Per-adapter configuration section."""
        if self.config is None:
            return {}
        return self.config.adapter(self.name)

    def setting(self, key: str, default: Any = None) -> Any:
        value = self.settings().get(key, default)
        return value

    def path_setting(self, key: str, default: str = "") -> str:
        """A configured path with ``~`` and variables expanded."""
        value = self.setting(key, default)
        return os.path.expandvars(os.path.expanduser(str(value))) if value else ""

    def window_setting(self, default: int = 200000) -> int:
        if self.config is None:
            return default
        return self.config.adapter_window(self.name, default)

    # -- contract --------------------------------------------------------
    @abstractmethod
    def detect(self) -> bool:
        """Whether this tool is present (its store directory exists)."""

    @abstractmethod
    def sessions(self, limit: int = 20) -> List[SessionRef]:
        """Discover sessions, newest first, without reading their content."""

    def ref_for(self, session_id: str) -> SessionRef:
        """Resolve a session id to a usable reference.

        A caller that holds only an id -- the handover builder, the rotation
        interlock -- needs a ref whose ``path`` actually points at the session's
        store. Building a placeholder ref with an empty path instead is silently
        fatal for a transcript-backed adapter: the reader gets an ``OSError`` on
        the empty path, swallows it, and reports the history as unavailable.
        That failure looks exactly like a session that has no history, which is
        why it survived until a real run exposed it.

        The default looks the id up through :meth:`sessions`. Adapters with a
        cheaper exact lookup -- a filename convention, a primary key -- override
        this. The fallback is a ref with an empty path, which reads the same way
        an unresolvable session should: nothing is attributed to a file that was
        never confirmed to exist.

        Never raises: an unresolvable id is a normal outcome, not an error.
        """
        session_id = str(session_id or "")
        if not session_id:
            return SessionRef(session=session_id, path="", tool=self.name)
        try:
            for ref in self.sessions(limit=REF_SCAN_LIMIT):
                if ref.session == session_id:
                    return ref
        except Exception:
            # NoSessions, ReadFailed, a broken store: all mean "not resolved".
            pass
        return SessionRef(session=session_id, path="", tool=self.name)

    @abstractmethod
    def read_usage(self, session: SessionRef) -> Reading:
        """Read the context occupancy of one session."""

    def compression_seen(self, session: SessionRef) -> bool:
        """Whether the tool has already compacted this session.

        Best effort: adapters that record compaction say so, and the default is
        a truthful ``False`` rather than a guess.
        """
        try:
            return bool(self.read_usage(session).compressed)
        except Exception:
            return False

    def recent_messages(self, session: SessionRef, limit: int = 12) -> List[str]:
        """Short excerpts of the newest messages, oldest first.

        Returns an empty list when the adapter cannot read history; callers turn
        that into an explicit "not available" line in the handover.
        """
        return []

    def history_unavailable_reason(self) -> str:
        """Why :meth:`recent_messages` returns nothing, for the handover file."""
        return "this adapter does not read conversation history"

    def rotate(
        self,
        session: str,
        handover_path: Optional[str],
        dry_run: bool = False,
        config: Any = None,
    ) -> RotateResult:
        """Start a fresh session after a handover.

        The default implementation is the honest one: this tool cannot drive the
        agent's session lifecycle, so it reports an assisted rotation and leaves
        the decision to the user.
        """
        return RotateResult(
            mode="assisted",
            rotated=False,
            detail={
                "note": "this adapter cannot start a new session by itself",
                "handover": handover_path,
                "next": self.start_hint(handover_path),
            },
        )

    def start_hint(self, handover_path: Optional[str] = None) -> str:
        """What a person should do to start the replacement session.

        Quoted verbatim in the rotation message. The default is a sentence with
        no command in it: inventing a command line for a tool this adapter has
        not verified would send the reader to a shell that does not have it.
        """
        if handover_path:
            return ("start a new session in %s and ask it to read %s"
                    % (self.name, handover_path))
        return "start a new session in %s" % self.name

    def run_prompt(
        self,
        prompt: str,
        timeout: float = 240.0,
        cwd: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
    ) -> PromptResult:
        """Run a one-shot prompt through the tool's CLI.

        Every implementation must spawn with a cleaned environment: inheriting
        the caller's session markers makes the child attach to the *current*
        session, which defeats the point of a fresh-session check.
        """
        raise AdapterNotInstalled(self.name, "one-shot prompts are not implemented")

    def busy(self, session: SessionRef) -> Optional[bool]:
        """Whether a turn is currently in flight, or ``None`` when unknown.

        ``None`` has two meanings and the pipeline distinguishes them by
        :attr:`BUSY_DETECTION`:

        * the adapter **implements** detection and the check failed -- fail
          closed, defer;
        * the adapter **has no** detection -- the activity quiet window is the
          only interlock, and the pipeline proceeds on that.

        Without that distinction an adapter that cannot see turn state would
        defer forever, which is not caution but a dead feature.
        """
        return None

    # -- full-auto rotation ----------------------------------------------
    # Adapters that implement `rotate()` overrides share these three pieces.
    def rotate_mode(self) -> str:
        """``full_auto`` or ``assisted``, as configured for this adapter.

        An unrecognised value reads as assisted: the safe failure of a typo is
        the behaviour that was already there, not a spawn nobody asked for.
        """
        value = str(self.setting("rotate_mode", ROTATE_MODE_ASSISTED) or "").strip().lower()
        return ROTATE_MODE_FULL_AUTO if value == ROTATE_MODE_FULL_AUTO else ROTATE_MODE_ASSISTED

    def rotate_timeout(self) -> float:
        """Seconds allowed for the continuation spawn."""
        try:
            value = float(self.setting("rotate_timeout", DEFAULT_ROTATE_TIMEOUT))
        except (TypeError, ValueError):
            return DEFAULT_ROTATE_TIMEOUT
        return value if value > 0 else DEFAULT_ROTATE_TIMEOUT

    def spawn_args(self, default: List[str]) -> List[str]:
        """Extra CLI arguments for the continuation spawn.

        A configured value replaces the default list rather than extending it,
        so a caller can drop an argument it does not want as well as add one.
        Strings are accepted as a single argument each; anything else is
        stringified, because an argument list is user configuration and a
        confusing type is not worth raising over.
        """
        configured = self.setting("spawn_args", None)
        if configured is None:
            return list(default)
        if isinstance(configured, str):
            return [configured]
        if isinstance(configured, (list, tuple)):
            return [str(item) for item in configured]
        return list(default)

    def continuation_prompt(self, handover_path: str, session: Optional[str]) -> str:
        """The prompt that starts the replacement session.

        A configured file replaces the built-in template. A file that cannot be
        read falls back to the template and says nothing: a rotation whose
        prompt could not be customised is still a rotation, and refusing to
        start the session over a missing optional file would be worse.
        """
        template = CONTINUATION_PROMPT_TEMPLATE
        path = self.path_setting("continuation_prompt_file", "")
        if path:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as stream:
                    text = stream.read()
                if text.strip():
                    template = text
            except OSError:
                pass
        return (template
                .replace("{handover}", handover_path or "")
                .replace("{session}", str(session or "")))

    def _rotate_failure(self, rc: int, stderr: str, timed_out: bool) -> RotateResult:
        """A failed spawn, reported with the tail of what the tool said."""
        reason = "continuation spawn timed out" if timed_out else (
            "continuation spawn exited %s" % rc)
        tail = (stderr or "").strip()[-ERROR_TAIL_CHARS:]
        return RotateResult(
            mode=ROTATE_RESULT_FULL_AUTO,
            rotated=False,
            detail={"rc": rc, "timed_out": timed_out, "stderr_tail": tail},
            error="%s: %s" % (reason, tail) if tail else reason,
        )

    # -- helpers ---------------------------------------------------------
    # Whether this adapter can tell whether a turn is in flight. Adapters that
    # override `busy()` should set this to True.
    BUSY_DETECTION: bool = False

    def capabilities(self) -> Dict[str, Any]:
        """What this adapter can and cannot do, for ``scan``/``status`` output."""
        return {
            "tool": self.name,
            "verification": self.VERIFICATION,
            "detect": True,
            "sessions": True,
            "read_usage": True,
            "compression_seen": True,
            "history": False,
            "rotate": False,
            "full_auto": False,
            "run_prompt": False,
            "start_hint": True,
            "busy_detection": self.BUSY_DETECTION,
        }

    def one_shot_command(self) -> List[str]:
        """Base command for a one-shot prompt, if this tool has one."""
        return []

    def _spawn(
        self,
        cmd: List[str],
        timeout: float,
        cwd: Optional[str],
        env: Optional[Dict[str, str]] = None,
    ):
        """Run a command with a cleaned environment and a hard timeout."""
        from ..core.util import run_cmd

        merged_env = clean_env()
        if env:
            merged_env.update(env)
        return run_cmd(cmd, env=merged_env, timeout=timeout, cwd=cwd or _safe_cwd())

    def _require_dir(self, path: str, hint: str = "") -> None:
        if not path or not os.path.isdir(path):
            raise AdapterNotInstalled(
                self.name, hint or "expected directory %s does not exist" % (path or "<unset>"))

    def _require_file(self, path: str, hint: str = "") -> None:
        if not path or not os.path.isfile(path):
            raise AdapterNotInstalled(
                self.name, hint or "expected file %s does not exist" % (path or "<unset>"))

    def _no_sessions(self, detail: str = "") -> NoSessions:
        return NoSessions(self.name, detail)

    def _read_failed(self, reason: str, session: Optional[str] = None) -> ReadFailed:
        return ReadFailed(self.name, reason, session)

    def _newest_session(self, refs: List[SessionRef]) -> SessionRef:
        if not refs:
            raise self._no_sessions("no session files found")
        return max(refs, key=lambda ref: ref.updated)


def _safe_cwd() -> Optional[str]:
    """A neutral working directory for spawned tools.

    Running a child in the user's project directory can make it pick up project
    configuration; the temp directory is inert.
    """
    for candidate in ("/tmp", os.environ.get("TMPDIR")):
        if candidate and os.path.isdir(candidate):
            return candidate
    return None


def timestamp_or_mtime(path: str, value: Any = None) -> float:
    """Prefer an explicit timestamp, fall back to the file's mtime."""
    from ..core.util import parse_iso

    parsed = parse_iso(value)
    if parsed:
        return parsed
    try:
        return os.path.getmtime(path)
    except OSError:
        return time.time()


def count_lines(path: str, chunk_limit: int = 64 * 1024 * 1024) -> int:
    """Count lines in a file without loading it into memory."""
    total = 0
    read_bytes = 0
    try:
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                read_bytes += len(chunk)
                total += chunk.count(b"\n")
                if read_bytes >= chunk_limit:
                    break
    except OSError:
        return 0
    return total
