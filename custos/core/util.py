"""Low-level helpers shared by every other module.

Everything here is dependency-free and side-effect free unless the function
name says otherwise. Two rules matter throughout the project and are
implemented here:

* every significant write is atomic (temporary file + :func:`os.replace`), so a
  crash can never leave a half-written state file behind;
* every spawned process gets a cleaned environment and a hard kill on timeout,
  because agent CLIs change their behaviour based on inherited environment
  variables.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# Default deny lists for :func:`clean_env`. They cover the well-known agent
# session-marker families. A parent agent that exports extra markers must extend
# these lists; silently inheriting them is what makes a "fresh" child session
# join the caller's session instead of starting its own.
DEFAULT_DENY_PREFIXES: Tuple[str, ...] = (
    "CLAUDE_CODE_",
    "CLAUDE_",
    "CODEX_",
    "OPENCODE_",
    "PI_",
    "CUSTOS_",
)

DEFAULT_DENY_EXACT: Tuple[str, ...] = (
    "CLAUDECODE",
    "CLAUDE_CONFIG_DIR",
    "TERMINAL_CWD",
    "AI_AGENT",
    "AGENT",
    "AGENT_NAME",
    "AGENT_SESSION_ID",
    "SESSION_ID",
)


class CustosError(Exception):
    """Base class for every error this package raises deliberately."""


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string with a trailing ``Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_stamp() -> str:
    """Compact local timestamp suitable for filenames: ``20261016-194501``."""
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def parse_iso(value: Any) -> float:
    """Best-effort conversion of a timestamp to epoch seconds.

    Accepts epoch seconds/milliseconds, ISO-8601 strings (with or without a
    trailing ``Z``) and plain numeric strings. Unparseable input returns ``0.0``
    rather than raising, because a single malformed record must never abort a
    whole scan.
    """
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return _normalise_epoch(float(value))
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        return _normalise_epoch(float(text))
    except ValueError:
        pass
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _normalise_epoch(value: float) -> float:
    """Treat clearly-millisecond values as milliseconds."""
    if value > 1e11:
        return value / 1000.0
    return value


def format_age(epoch: float, reference: Optional[float] = None) -> str:
    """Human readable age such as ``4m ago``; ``unknown`` for a zero epoch."""
    if not epoch:
        return "unknown"
    delta = (reference if reference is not None else time.time()) - epoch
    if delta < 0:
        return "just now"
    if delta < 90:
        return "%ds ago" % int(delta)
    if delta < 5400:
        return "%dm ago" % int(delta / 60)
    if delta < 172800:
        return "%.1fh ago" % (delta / 3600)
    return "%.1fd ago" % (delta / 86400)


def format_tokens(value: Optional[int]) -> str:
    """Format a token count compactly (``1.2M``, ``331k``, ``950``)."""
    if value is None:
        return "-"
    if value >= 1_000_000:
        return "%.1fM" % (value / 1_000_000.0)
    if value >= 1_000:
        return "%.0fk" % (value / 1_000.0)
    return str(int(value))


# --------------------------------------------------------------------- atomic


def atomic_write_text(path: str, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically.

    The temporary file is created in the destination directory so the final
    :func:`os.replace` stays on one filesystem and is therefore atomic.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle, tmp_path = tempfile.mkstemp(
        prefix=".custos-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(handle, "w", encoding=encoding) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        _unlink_quietly(tmp_path)
        raise


def atomic_write_json(path: str, payload: Any, indent: int = 1) -> None:
    """Serialise ``payload`` as JSON and write it atomically."""
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=indent) + "\n")


def append_jsonl(path: str, record: Dict[str, Any]) -> None:
    """Append one JSON record to a ``.jsonl`` file.

    Appends are small and single-writer (the rotation pipeline holds a lock), so
    a plain append is used; the record itself is serialised before the handle is
    opened so a serialisation error cannot truncate the file.
    """
    line = json.dumps(record, ensure_ascii=False)
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()


def read_json(path: str, default: Any = None) -> Any:
    """Read JSON, returning ``default`` when the file is missing or corrupt."""
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return default


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------- environment


def clean_env(
    deny_prefixes: Iterable[str] = (),
    deny_exact: Iterable[str] = (),
    base: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Return a copy of the environment with agent session markers removed.

    A child process that inherits ``CLAUDE_CODE_*`` (or any other agent's marker
    variables) can attach to the *current* session instead of starting a new
    one, which silently invalidates a rotation. Callers should pass their own
    additional prefixes through ``deny_prefixes``.

    ``deny_prefixes`` and ``deny_exact`` extend the built-in defaults; they do
    not replace them.
    """
    source = os.environ if base is None else base
    prefixes = tuple(DEFAULT_DENY_PREFIXES) + tuple(deny_prefixes)
    exact = set(DEFAULT_DENY_EXACT) | set(deny_exact)
    cleaned: Dict[str, str] = {}
    for key, value in source.items():
        if key in exact:
            continue
        if any(key.startswith(prefix) for prefix in prefixes):
            continue
        cleaned[key] = value
    # A child that believes it is interactive can block on a prompt that will
    # never be answered in a scheduler context.
    cleaned.pop("CUSTOS_INTERACTIVE", None)
    return cleaned


# ----------------------------------------------------------------- subprocess


def run_cmd(
    cmd: Sequence[str],
    env: Optional[Dict[str, str]] = None,
    timeout: float = 60.0,
    cwd: Optional[str] = None,
    input_text: Optional[str] = None,
) -> Tuple[int, str, str, bool]:
    """Run ``cmd`` and return ``(returncode, stdout, stderr, timed_out)``.

    On timeout the whole process group is killed. ``start_new_session=True``
    puts the child in its own group so a CLI that spawns helpers cannot leave
    orphans behind; macOS has no ``timeout(1)`` binary, so this is implemented
    here rather than shelled out.

    ``returncode`` is ``-1`` when the process had to be killed.
    """
    try:
        process = subprocess.Popen(
            list(cmd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            cwd=cwd,
            env=env,
            start_new_session=True,
            text=True,
        )
    except OSError as exc:
        return 127, "", "failed to start %r: %s" % (cmd[0] if cmd else "", exc), False

    timed_out = False
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_process_group(process)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            stdout, stderr = "", ""
    except (OSError, ValueError) as exc:
        _kill_process_group(process)
        return -1, "", "process communication failed: %s" % exc, False

    code = -1 if timed_out else process.returncode
    return code, stdout or "", stderr or "", timed_out


def _kill_process_group(process: subprocess.Popen) -> None:
    """SIGKILL the child's process group, falling back to the child itself."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            pass


# ----------------------------------------------------------------------- lock


class FileLock:
    """Advisory exclusive lock built on :func:`fcntl.flock`.

    Usage::

        with FileLock(path) as lock:
            if not lock.acquired:
                return              # another instance is running

    ``acquired`` is ``False`` when the lock is already held elsewhere; callers
    treat that as "skip this round" rather than as an error. Platforms without
    ``fcntl`` degrade to "always acquired" so the tool still runs.
    """

    def __init__(self, path: str, timeout: float = 0.0) -> None:
        self.path = path
        self.timeout = max(0.0, timeout)
        self.acquired = False
        self._handle = None
        self._fcntl = _load_fcntl()

    def acquire(self) -> bool:
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        if self._fcntl is None:
            # No flock available: behave as an uncontended lock rather than
            # refusing to run at all.
            self.acquired = True
            return True
        self._handle = open(self.path, "a+")
        deadline = time.time() + self.timeout
        while True:
            try:
                self._fcntl.flock(
                    self._handle.fileno(), self._fcntl.LOCK_EX | self._fcntl.LOCK_NB
                )
                self.acquired = True
                return True
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    self.release()
                    raise
                if time.time() >= deadline:
                    self.release()
                    return False
                time.sleep(0.2)

    def release(self) -> None:
        if self._handle is not None:
            try:
                if self._fcntl is not None:
                    self._fcntl.flock(self._handle.fileno(), self._fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        self.acquired = False

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _load_fcntl():
    try:
        import fcntl  # type: ignore
    except ImportError:  # pragma: no cover - Windows only
        return None
    return fcntl


# ------------------------------------------------------------------ optional


def have_yaml() -> bool:
    """Whether PyYAML is importable (drives YAML vs JSON config loading)."""
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


def log(message: str) -> None:
    """Diagnostics go to stderr; stdout stays reserved for delivery."""
    print(message, file=sys.stderr)


def truncate(text: Optional[str], limit: int) -> str:
    """Collapse whitespace and cut ``text`` to ``limit`` characters."""
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: max(0, limit - 1)] + "…"


def first_lines(text: str, count: int, width: int = 400) -> List[str]:
    """Return up to ``count`` non-empty lines, each truncated to ``width``."""
    out: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        out.append(truncate(stripped, width))
        if len(out) >= count:
            break
    return out
