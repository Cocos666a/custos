"""Pi adapter (documentation-verified).

There is no Pi installation to test against on the machine where this adapter
was written, so it is marked :data:`~custos.adapters.base.DOC_VERIFIED`: the
paths, the file layout and the one-shot flag all come from Pi's published
documentation rather than from observed data, and the adapter degrades to an
explicit "cannot read" rather than inventing numbers.

Sources consulted (retrieved 2026-10-16):

* ``https://pi.dev/docs/latest/sessions`` -- session storage location and flags.
* ``https://pi.dev/docs/latest/session-format`` -- on-disk layout and record
  types.
* ``https://pi.dev/docs/latest/cli`` -- ``--print`` / ``-p`` and session flags.

What the documentation states, and what this adapter therefore assumes:

* sessions are JSONL files at
  ``~/.pi/agent/sessions/--<path>--/<timestamp>_<session-id>.jsonl``, where the
  working directory is flattened by replacing its separators with ``-``;
* the first line is a header record with ``type`` ``"session"`` carrying the
  session id and a version;
* later records carry a ``type``; message records hold an ``AgentMessage`` under
  ``message``; usage is recorded in its own entry type and compaction in another;
* ``pi --print "<prompt>"`` runs one prompt, writes the final assistant text to
  stdout and exits, which is the one-shot form the gate needs.

The exact field names inside the usage entry are **not** stated in the pages
that were reachable, so :meth:`PiAdapter._find_usage` searches for a tokens
shaped object instead of hard-coding a path. That is deliberate: if Pi names the
fields differently than expected the adapter reports a read failure, and the
first person with a real install can correct the field list from evidence. It
does not guess a number.

The session directory can be overridden with ``--session-dir``,
``PI_CODING_AGENT_SESSION_DIR`` or the ``sessionDir`` setting; the configured
``sessions_dir`` here corresponds to the CLI flag.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..core.usage import Reading
from ..core.util import parse_iso
from .base import DOC_VERIFIED, Adapter, PromptResult, SessionRef, timestamp_or_mtime

DEFAULT_SESSIONS_DIR = "~/.pi/agent/sessions"
TAIL_BYTES = 1024 * 1024

# Keys that would carry the prompt size under any plausible naming.
_INPUT_KEYS = ("inputTokens", "input_tokens", "promptTokens", "prompt_tokens")
_CACHE_READ_KEYS = ("cacheReadTokens", "cache_read_tokens", "cachedInputTokens",
                    "cacheRead", "cache_read")
_OUTPUT_KEYS = ("outputTokens", "output_tokens")
_TOTAL_KEYS = ("totalTokens", "total_tokens", "total")
_WINDOW_KEYS = ("contextWindow", "context_window", "modelContextWindow", "window")

_USAGE_TYPE_HINTS = ("usage", "tokens")
_COMPACTION_TYPE_HINTS = ("compaction",)
_MESSAGE_TYPE_HINTS = ("message",)


class PiAdapter(Adapter):
    """Adapter for Pi session files."""

    name = "pi"
    VERIFICATION = DOC_VERIFIED
    VERIFICATION_NOTE = (
        "paths, file layout and --print flag taken from pi.dev documentation "
        "(2026-10-16); usage field names are searched, not hard-coded, because "
        "no local install was available to confirm them"
    )

    @property
    def sessions_dir(self) -> str:
        return self.path_setting("sessions_dir", DEFAULT_SESSIONS_DIR)

    def detect(self) -> bool:
        return os.path.isdir(self.sessions_dir)

    def session_files(self) -> List[str]:
        root = self.sessions_dir
        if not os.path.isdir(root):
            return []
        found: List[str] = []
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    if name.endswith(".jsonl"):
                        found.append(os.path.join(dirpath, name))
        except OSError:
            return []
        return found

    def sessions(self, limit: int = 20) -> List[SessionRef]:
        refs: List[SessionRef] = []
        for path in self.session_files():
            ref = self._ref_for(path)
            if ref is not None:
                refs.append(ref)
        refs.sort(key=lambda ref: ref.updated, reverse=True)
        if not refs:
            raise self._no_sessions("no session files under %s" % self.sessions_dir)
        return refs[:limit]

    def read_usage(self, session: SessionRef) -> Reading:
        usage, detail = self._find_usage(session.path)
        if usage is None:
            raise self._read_failed(
                "no usage entry recognised in this session file; the field names "
                "may differ from the documented layout", session.session)

        tokens = _prompt_tokens(usage)
        if tokens <= 0:
            raise self._read_failed("usage entry has no prompt token count",
                                    session.session)

        window = _first_int(usage, _WINDOW_KEYS) or self.window_setting()
        compressed = self.compression_seen(session)
        detail["usage_keys"] = sorted(usage.keys())
        return Reading(
            tool=self.name,
            session=session.session,
            tokens=tokens,
            window=window,
            source="transcript",
            ts=session.updated,
            compressed=compressed,
            detail=detail,
        )

    def compression_seen(self, session: SessionRef) -> bool:
        for record in _iter_records(session.path, tail_bytes=TAIL_BYTES, reverse=True):
            record_type = str(record.get("type") or "").lower()
            if any(hint in record_type for hint in _COMPACTION_TYPE_HINTS):
                return True
        return False

    def recent_messages(self, session: SessionRef, limit: int = 12) -> List[str]:
        """Best-effort message excerpts using the documented entry shape."""
        from ..core.util import truncate

        entries: List[str] = []
        for record in _iter_records(session.path, tail_bytes=TAIL_BYTES):
            record_type = str(record.get("type") or "").lower()
            if not any(hint in record_type for hint in _MESSAGE_TYPE_HINTS):
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _content_text(message.get("content"))
            if text:
                entries.append("[%s] %s" % (role, truncate(text, 240)))
        return entries[-limit:]

    def capabilities(self) -> Dict[str, Any]:
        caps = super().capabilities()
        caps.update({
            "history": True,
            "rotate": False,
            "full_auto": False,
            "run_prompt": True,
            "note": "documentation-verified only; the usage field names are "
                    "searched rather than hard-coded",
        })
        return caps

    def one_shot_command(self) -> List[str]:
        return ["pi", "--print"]

    def start_hint(self, handover_path: Optional[str] = None) -> str:
        if handover_path:
            return ("start `pi` in your project and ask it to read %s before "
                    "continuing" % handover_path)
        return "start `pi` in your project directory"

    def run_prompt(
        self,
        prompt: str,
        timeout: float = 240.0,
        cwd: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
    ) -> PromptResult:
        """Run ``pi --print <prompt>`` with a cleaned environment.

        ``PI_CODING_AGENT_*`` variables are stripped by the default deny list,
        which matters here because they can redirect the session directory and
        would make the child write into the caller's store.
        """
        cmd = list(self.one_shot_command()) + list(extra_args or []) + [prompt]
        rc, stdout, stderr, timed_out = self._spawn(cmd, timeout, cwd)
        return PromptResult(
            text=(stdout or "").strip(),
            rc=rc,
            timed_out=timed_out,
            stderr=(stderr or "").strip()[:2000],
        )

    # -- internals -------------------------------------------------------
    def _ref_for(self, path: str) -> Optional[SessionRef]:
        """Build a session reference from a file path and its header record."""
        session_id = ""
        header_cwd = ""
        for record in _iter_records(path, tail_bytes=8192):
            if record.get("type") == "session":
                session_id = str(record.get("id") or "")
                header_cwd = str(record.get("cwd") or "")
                break
            break  # only the first line is the header

        if not session_id:
            # Fall back to the documented filename shape <timestamp>_<id>.jsonl
            stem = os.path.basename(path)
            if stem.endswith(".jsonl"):
                stem = stem[: -len(".jsonl")]
            match = re.match(r"^\d{4}-\d{2}-\d{2}T[\d-]+_(.+)$", stem)
            session_id = match.group(1) if match else stem

        return SessionRef(
            session=session_id,
            path=path,
            updated=timestamp_or_mtime(path),
            tool=self.name,
            meta={"cwd": header_cwd, "directory": os.path.basename(os.path.dirname(path))},
        )

    def _find_usage(self, path: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        """Locate a tokens-shaped object in the newest records.

        Returns the object and a small explanation of where it was found, so a
        handover file records how the number was obtained.
        """
        for record in _iter_records(path, tail_bytes=TAIL_BYTES, reverse=True):
            record_type = str(record.get("type") or "").lower()

            # Documented shape: a dedicated usage entry.
            if any(hint in record_type for hint in _USAGE_TYPE_HINTS):
                candidate = _tokens_object(record)
                if candidate:
                    return candidate, {"found_in": "usage entry", "record_type": record_type}

            # Fallback: usage nested inside a message.
            message = record.get("message")
            if isinstance(message, dict):
                candidate = _tokens_object(message)
                if candidate:
                    return candidate, {"found_in": "message", "record_type": record_type}

        return None, {}


def _tokens_object(container: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find a dict inside ``container`` that looks like a token usage record."""
    for key in ("usage", "tokens", "tokenUsage", "token_usage"):
        value = container.get(key)
        if isinstance(value, dict) and _looks_like_usage(value):
            return value
    # Some layouts put the numbers directly on the entry.
    for key in ("inputTokens", "input_tokens", "promptTokens", "prompt_tokens"):
        if isinstance(container.get(key), (int, float)):
            return container
    return None


def _looks_like_usage(candidate: Dict[str, Any]) -> bool:
    keys = set(candidate.keys())
    return bool(keys & set(_INPUT_KEYS)) or bool(keys & set(_TOTAL_KEYS))


def _prompt_tokens(usage: Dict[str, Any]) -> int:
    """Prompt size from a usage object.

    Same rule as the other transcript adapters: prefer an explicit input count,
    fall back to the cached-prefix count, and take the larger so a layout where
    one of them is the whole prompt and the other a component still reports the
    prompt rather than a fraction of it.
    """
    explicit = _first_int(usage, _INPUT_KEYS)
    cached = _first_int(usage, _CACHE_READ_KEYS)
    total = _first_int(usage, _TOTAL_KEYS)
    output = _first_int(usage, _OUTPUT_KEYS)
    if explicit:
        return max(explicit, cached)
    if total:
        # A total that merely sums input and output is still prompt-dominated.
        return max(total - output, cached, 0)
    return max(cached, 0)


def _first_int(container: Dict[str, Any], keys: tuple) -> int:
    for key in keys:
        value = container.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value))
            except ValueError:
                continue
    return 0


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return " ".join(part for part in parts if part).strip()


def _iter_records(
    path: str,
    tail_bytes: Optional[int] = None,
    reverse: bool = False,
) -> Iterator[Dict[str, Any]]:
    try:
        size = os.path.getsize(path)
    except OSError:
        return
    start = 0
    if tail_bytes and size > tail_bytes:
        start = size - tail_bytes
    try:
        with open(path, "rb") as stream:
            if start:
                stream.seek(start)
            payload = stream.read()
    except OSError:
        return

    lines = payload.decode("utf-8", errors="replace").splitlines()
    if start and lines:
        lines = lines[1:]
    if reverse:
        lines = list(reversed(lines))

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            yield record
