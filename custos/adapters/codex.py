"""Codex adapter.

Codex rolls a session out to
``~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl``, with older
sessions optionally moved to ``~/.codex/archived_sessions/``. Each line is a
JSON record with a ``type`` and a ``payload``.

How occupancy is read
---------------------

The authoritative record is ``event_msg`` with ``payload.type == "token_count"``.
Its payload carries::

    payload.info.last_token_usage   -- tokens for the most recent model call
    payload.info.total_token_usage  -- cumulative across the whole session
    payload.info.model_context_window

Local evidence for choosing ``last_token_usage`` over ``total_token_usage``: in a
292-call rollout the cumulative counters reach ``total_tokens=37494149`` while
the final per-call figure is ``144838``. The session was nowhere near 37 million
tokens of context, so the cumulative figure counts every call ever made and
cannot describe the window. ``last_token_usage`` tracks the window instead.

Within ``last_token_usage``, ``input_tokens`` is the prompt size and already
includes ``cached_input_tokens``: the record satisfies
``input_tokens + output_tokens == total_tokens`` exactly, and
``cached_input_tokens <= input_tokens`` throughout. Adding the cached count to
``input_tokens`` would therefore double-count the cached prefix.

``model_context_window`` is recorded by the tool, so it is preferred over the
configured default -- a session that switched models reports the window that
actually applies. Older rollouts predate that field and fall back to config.

Compaction is recorded as a record of ``type == "compacted"``; a session that
has one has already had history discarded.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterator, List, Optional, Set

from ..core.usage import Reading
from ..core.util import parse_iso
from .base import (
    RECAP_MARKER,
    ROTATE_MODE_FULL_AUTO,
    ROTATE_RESULT_FULL_AUTO,
    VERIFIED,
    Adapter,
    PromptResult,
    RotateResult,
    SessionRef,
)

DEFAULT_SESSIONS_DIR = "~/.codex/sessions"
DEFAULT_ARCHIVED_DIR = "~/.codex/archived_sessions"
DEFAULT_SPAWN_ARGS = ["--sandbox", "workspace-write"]
TAIL_BYTES = 1024 * 1024


class CodexAdapter(Adapter):
    """Adapter for Codex rollout transcripts."""

    name = "codex"
    VERIFICATION = VERIFIED
    VERIFICATION_NOTE = (
        "occupancy from event_msg/token_count last_token_usage.input_tokens; "
        "window taken from the tool's own model_context_window when present"
    )

    @property
    def sessions_dir(self) -> str:
        return self.path_setting("sessions_dir", DEFAULT_SESSIONS_DIR)

    @property
    def archived_dir(self) -> str:
        return self.path_setting("archived_dir", DEFAULT_ARCHIVED_DIR)

    def detect(self) -> bool:
        return os.path.isdir(self.sessions_dir) or os.path.isdir(self.archived_dir)

    def session_files(self, include_archived: bool = True) -> List[str]:
        """Rollout files, newest directories first."""
        roots = [self.sessions_dir]
        if include_archived:
            roots.append(self.archived_dir)
        found: List[str] = []
        for root in roots:
            if not os.path.isdir(root):
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    if name.startswith("rollout-") and name.endswith(".jsonl"):
                        found.append(os.path.join(dirpath, name))
        return found

    def sessions(self, limit: int = 20) -> List[SessionRef]:
        refs: List[SessionRef] = []
        for path in self.session_files():
            try:
                updated = os.path.getmtime(path)
            except OSError:
                continue
            session_id = _session_id_from_filename(path)
            refs.append(SessionRef(
                session=session_id,
                path=path,
                updated=updated,
                tool=self.name,
                meta={"archived": self.archived_dir in path},
            ))
        refs.sort(key=lambda ref: ref.updated, reverse=True)
        if not refs:
            raise self._no_sessions("no rollout files under %s" % self.sessions_dir)
        return refs[:limit]

    def ref_for(self, session_id: str) -> SessionRef:
        """Resolve a session id to its rollout file.

        Codex names a rollout
        ``rollout-<timestamp>-<uuid>.jsonl`` and files it under date
        directories, so the id is the filename suffix rather than the whole
        stem. That means the id cannot be turned into a glob pattern the way
        Claude Code's can; the directories have to be walked either way.

        The walk is bounded to the date directories under each root: a rollout
        path is ``<root>/YYYY/MM/DD/name.jsonl``, so there is nothing to find
        deeper, and a store with an unexpected extra level costs a harmless
        empty listing rather than a full recursive scan. The candidate is
        confirmed by suffix rather than by substring, so a timestamp that
        happens to contain the id cannot produce a false match.
        """
        session_id = _safe_id(session_id)
        if not session_id:
            return SessionRef(session="", path="", tool=self.name)

        stamp = session_id + ".jsonl"
        best = ""
        best_mtime = 0.0
        for root in (self.sessions_dir, self.archived_dir):
            if not os.path.isdir(root):
                continue
            for path in _rollout_candidates(root):
                if not os.path.basename(path).endswith(stamp):
                    continue
                updated = _mtime(path)
                if not best or updated > best_mtime:
                    best, best_mtime = path, updated
        if not best:
            return SessionRef(session=session_id, path="", tool=self.name)

        return SessionRef(
            session=session_id,
            path=best,
            updated=best_mtime,
            tool=self.name,
            meta={"archived": self.archived_dir in best},
        )

    def read_usage(self, session: SessionRef) -> Reading:
        info = self._latest_token_info(session.path)
        if info is None:
            raise self._read_failed(
                "no token_count record found (older Codex rollouts may not "
                "record usage)", session.session)

        usage = info.get("last_token_usage") or {}
        tokens = _int(usage.get("input_tokens"))
        if tokens <= 0:
            raise self._read_failed("token_count record has no input_tokens",
                                    session.session)

        window = _int(info.get("model_context_window")) or self.window_setting()
        compressed = self.compression_seen(session)

        return Reading(
            tool=self.name,
            session=session.session,
            tokens=tokens,
            window=window,
            source="transcript",
            ts=session.updated,
            compressed=compressed,
            detail={
                "input_tokens": usage.get("input_tokens"),
                "cached_input_tokens": usage.get("cached_input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "reasoning_output_tokens": usage.get("reasoning_output_tokens"),
                "window_source": "tool" if info.get("model_context_window") else "config",
                "cumulative_total_tokens": (info.get("total_token_usage") or {}).get("total_tokens"),
            },
        )

    def compression_seen(self, session: SessionRef) -> bool:
        for record in _iter_records(session.path, tail_bytes=TAIL_BYTES, reverse=True):
            if record.get("type") == "compacted":
                return True
        return False

    def recent_messages(self, session: SessionRef, limit: int = 12) -> List[str]:
        """Newest user/assistant messages, oldest first."""
        from ..core.util import truncate

        entries: List[str] = []
        for record in _iter_records(session.path, tail_bytes=TAIL_BYTES):
            if record.get("type") != "response_item":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in ("user", "assistant"):
                continue
            text = _content_text(payload.get("content"))
            if text:
                entries.append("[%s] %s" % (role, truncate(text, 240)))
        return entries[-limit:]

    def capabilities(self) -> Dict[str, Any]:
        caps = super().capabilities()
        full_auto = self.rotate_mode() == ROTATE_MODE_FULL_AUTO
        caps.update({
            "history": True,
            "rotate": True,
            "full_auto": full_auto,
            "run_prompt": True,
            "note": ("full-auto rotation starts a new session with the "
                     "handover as continuation context"
                     if full_auto else
                     "rotation writes a handover and asks for a new session"),
        })
        return caps

    def rotate(
        self,
        session: str,
        handover_path: Optional[str],
        dry_run: bool = False,
        config: Any = None,
    ) -> RotateResult:
        """Start the replacement session with ``codex exec``.

        The new session is identified by what appeared on disk: the set of
        rollout files is snapshotted before the spawn, and the discovery takes
        one that was not there before. That is the only reliable signal --
        ``codex exec`` does not print a session id on stdout.
        """
        if self.rotate_mode() != ROTATE_MODE_FULL_AUTO:
            return super().rotate(session, handover_path, dry_run, config)
        if dry_run:
            return RotateResult(
                mode=ROTATE_RESULT_FULL_AUTO,
                rotated=False,
                detail={
                    "would_start_new_session": True,
                    "handover": handover_path,
                },
            )
        if not handover_path:
            return RotateResult(
                mode=ROTATE_RESULT_FULL_AUTO,
                rotated=False,
                error="full_auto rotation requires a handover file",
            )

        old_session = str(session or "")
        cwd = self._rotate_cwd(old_session)
        prompt = self.continuation_prompt(handover_path, old_session)
        before = set(self.session_files())
        t0 = time.time()
        result = self.run_prompt(
            prompt,
            timeout=self.rotate_timeout(),
            cwd=cwd,
            extra_args=self.spawn_args(DEFAULT_SPAWN_ARGS),
        )
        if result.timed_out or result.rc != 0:
            return self._rotate_failure(result.rc, result.stderr, result.timed_out)

        new_session = self._new_rollout_since(before, old_session, t0)
        return RotateResult(
            mode=ROTATE_RESULT_FULL_AUTO,
            rotated=True,
            new_session=new_session,
            detail={
                "rc": result.rc,
                "timed_out": False,
                "recap_seen": RECAP_MARKER in result.text,
                "cwd": cwd or "",
                "handover": handover_path,
                "session_id_source": "rollout" if new_session else "unknown",
            },
        )

    def _rotate_cwd(self, session_id: str) -> Optional[str]:
        """The working directory the replacement session should start in.

        ``rotate_cwd`` wins. Otherwise the old rollout's ``session_meta`` record
        carries the ``cwd`` the session was started in, which is the project the
        work belongs to.
        """
        configured = self.path_setting("rotate_cwd", "")
        if configured:
            return configured
        ref = self.ref_for(session_id)
        if not ref.path:
            return None
        return _session_meta_cwd(ref.path)

    def _new_rollout_since(
        self, before: Set[str], old_session: str, t0: float,
    ) -> Optional[str]:
        """The rollout the spawn wrote, identified by the pre-spawn snapshot."""
        candidates = [
            path for path in self.session_files()
            if path not in before and _mtime(path) > t0 - 2
        ]
        if not candidates:
            return None
        newest = max(candidates, key=_mtime)
        # The filename carries the id: `rollout-<timestamp>-<uuid>.jsonl`. The
        # record's own id is a cross-check for a store whose naming convention
        # changed under us, and the only source when the name does not parse.
        stem = os.path.basename(newest)[: -len(".jsonl")]
        from_filename = _session_id_from_filename(newest)
        parsed_from_name = from_filename != stem
        from_record = _session_meta_id(newest)
        if parsed_from_name and from_filename != old_session:
            return from_filename
        return from_record or (None if from_filename == old_session else from_filename)

    def one_shot_command(self) -> List[str]:
        # codex exec refuses to run in a directory that is neither trusted nor
        # a git repo unless this flag is passed; the gate spawns the one-shot
        # in a neutral cwd (/tmp), so the check has to be skipped here.
        return ["codex", "exec", "--skip-git-repo-check"]

    def start_hint(self, handover_path: Optional[str] = None) -> str:
        if handover_path:
            return ("start `codex` in your project and ask it to read %s before "
                    "continuing" % handover_path)
        return "start `codex` in your project directory"

    def run_prompt(
        self,
        prompt: str,
        timeout: float = 240.0,
        cwd: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
    ) -> PromptResult:
        """Run ``codex exec <prompt>`` with a cleaned environment."""
        cmd = list(self.one_shot_command()) + list(extra_args or []) + [prompt]
        rc, stdout, stderr, timed_out = self._spawn(cmd, timeout, cwd)
        return PromptResult(
            text=(stdout or "").strip(),
            rc=rc,
            timed_out=timed_out,
            stderr=(stderr or "").strip()[:2000],
        )

    # -- internals -------------------------------------------------------
    def _latest_token_info(self, path: str) -> Optional[Dict[str, Any]]:
        """The newest ``token_count.info`` payload in a rollout."""
        for record in _iter_records(path, tail_bytes=TAIL_BYTES, reverse=True):
            if record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if isinstance(info, dict):
                return info
        return None


def _session_meta(path: str) -> Optional[Dict[str, Any]]:
    """The rollout's ``session_meta`` payload, read from the head of the file."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as stream:
            for _ in range(20):
                line = stream.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") != "session_meta":
                    continue
                payload = record.get("payload")
                return payload if isinstance(payload, dict) else {}
    except OSError:
        return None
    return None


def _session_meta_cwd(path: str) -> Optional[str]:
    """The ``cwd`` the rollout recorded for its session."""
    payload = _session_meta(path) or {}
    cwd = payload.get("cwd")
    return str(cwd) if cwd else None


def _session_meta_id(path: str) -> Optional[str]:
    """The session id the rollout recorded for itself."""
    payload = _session_meta(path) or {}
    for key in ("id", "session_id"):
        value = payload.get(key)
        if value:
            return str(value)
    return None


def _safe_id(session_id: Any) -> str:
    """A session id safe to use in a filename comparison.

    Ids are UUIDs. An empty value, or one carrying a path separator, is refused
    rather than joined onto a directory.
    """
    text = str(session_id or "").strip()
    if not text or text in (".", ".."):
        return ""
    if os.sep in text or (os.altsep and os.altsep in text):
        return ""
    if os.path.basename(text) != text:
        return ""
    return text


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _rollout_candidates(root: str) -> List[str]:
    """Rollout files under one root, listing only the documented depth.

    The layout is ``<root>/YYYY/MM/DD/``. Anything at another depth is skipped
    on purpose: a store that has been reorganized is a store whose layout this
    adapter has not been verified against, and guessing costs a full walk.
    """
    found: List[str] = []
    try:
        years = os.listdir(root)
    except OSError:
        return found
    for year in years:
        year_dir = os.path.join(root, year)
        try:
            months = os.listdir(year_dir)
        except OSError:
            continue
        for month in months:
            month_dir = os.path.join(year_dir, month)
            try:
                days = os.listdir(month_dir)
            except OSError:
                continue
            for day in days:
                day_dir = os.path.join(month_dir, day)
                try:
                    names = os.listdir(day_dir)
                except OSError:
                    continue
                for name in names:
                    if name.startswith("rollout-") and name.endswith(".jsonl"):
                        found.append(os.path.join(day_dir, name))
    return found


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _session_id_from_filename(path: str) -> str:
    """``rollout-2026-10-15T21-40-08-<uuid>.jsonl`` -> ``<uuid>``.

    The trailing 36 characters are the UUID; the name has no other delimiter
    that is not also used inside the timestamp, so the suffix is taken by
    position and the whole stem is kept when it does not look like a UUID.
    """
    stem = os.path.basename(path)
    if stem.endswith(".jsonl"):
        stem = stem[: -len(".jsonl")]
    candidate = stem[-36:]
    if candidate.count("-") == 4:
        return candidate
    return stem


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
    """Yield JSON records from a rollout file (see the Claude Code variant)."""
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
