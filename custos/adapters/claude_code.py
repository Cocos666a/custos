"""Claude Code adapter.

Sessions live in ``~/.claude/projects/<project-slug>/<session-uuid>.jsonl``, one
JSON record per line. Sub-agent transcripts sit in a ``subagents/`` subdirectory
next to the parent and share the parent's project slug.

How occupancy is read
---------------------

Every assistant record carries ``message.usage``. The field that represents the
prompt sent on that turn is ``input_tokens``.

The obvious alternative -- ``input_tokens + cache_read_input_tokens +
cache_creation_input_tokens`` -- is wrong for the transcripts this adapter was
verified against, and the error is large enough to matter (it doubles the
number). Local evidence:

* a session's first assistant record reports ``input_tokens=59151`` with both
  cache fields at zero, so ``input_tokens`` is the whole prompt on a cold start;
* the very next record reports ``input_tokens=59394`` with
  ``cache_read_input_tokens=59136``. The prompt grew by 243 tokens, it did not
  jump to 118530, and 59394 - 59136 = 258 is that same small delta. So the cache
  read is a *component* of ``input_tokens``, not a sibling of it.

Across 4151 usage records in 77 local transcripts, this convention holds in
87.0% of records outright. A minority of records (0.3%) are written the other
way round, where ``input_tokens`` carries only the uncached remainder. That is
why :func:`_prompt_tokens` takes the larger of the two readings rather than
trusting ``input_tokens`` unconditionally: under either convention the larger
value is the prompt size, and the two only disagree by a small amount in the
direction of under-reporting.

Compaction is recorded explicitly: a ``system`` record with
``subtype="compact_boundary"`` and ``compactMetadata`` holding ``preTokens`` and
``postTokens``. Its ``preTokens`` is what the tool itself considered the context
size at that moment, which is a useful sanity check on the reading.

Sidechain records (``isSidechain: true``) belong to sub-agent conversations and
are skipped: their token counts describe a different context window.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..core.usage import Reading
from ..core.util import parse_iso
from .base import (
    DOC_VERIFIED,
    RECAP_MARKER,
    ROTATE_MODE_FULL_AUTO,
    ROTATE_RESULT_FULL_AUTO,
    VERIFIED,
    Adapter,
    PromptResult,
    RotateResult,
    SessionRef,
    timestamp_or_mtime,
)

DEFAULT_PROJECTS_DIR = "~/.claude/projects"
DEFAULT_SPAWN_ARGS = ["--permission-mode", "acceptEdits"]
# How much of a transcript to read when looking for the newest usage record.
# Transcripts grow to hundreds of megabytes; the newest assistant turn is what
# matters, so the tail is scanned first and the whole file only if the tail
# contains no usage at all.
TAIL_BYTES = 1024 * 1024


class ClaudeCodeAdapter(Adapter):
    """Adapter for Claude Code session transcripts."""

    name = "claude_code"
    VERIFICATION = VERIFIED
    VERIFICATION_NOTE = (
        "occupancy derived from message.usage.input_tokens and cross-checked "
        "against compactMetadata.preTokens on real local sessions"
    )

    def __init__(self, config: Any = None) -> None:
        super().__init__(config)
        self._usage_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}

    # -- locations -------------------------------------------------------
    @property
    def projects_dir(self) -> str:
        return self.path_setting("projects_dir", DEFAULT_PROJECTS_DIR)

    def detect(self) -> bool:
        return os.path.isdir(self.projects_dir)

    def session_files(self) -> List[str]:
        """Every top-level session transcript, sub-agent transcripts excluded."""
        root = self.projects_dir
        if not os.path.isdir(root):
            return []
        found: List[str] = []
        try:
            for project in os.listdir(root):
                project_dir = os.path.join(root, project)
                if not os.path.isdir(project_dir):
                    continue
                try:
                    for entry in os.listdir(project_dir):
                        if entry.endswith(".jsonl"):
                            found.append(os.path.join(project_dir, entry))
                except OSError:
                    continue
        except OSError:
            return []
        return found

    def sessions(self, limit: int = 20) -> List[SessionRef]:
        """Newest sessions first, by file modification time."""
        refs: List[SessionRef] = []
        for path in self.session_files():
            try:
                updated = os.path.getmtime(path)
            except OSError:
                continue
            refs.append(SessionRef(
                session=os.path.basename(path)[: -len(".jsonl")],
                path=path,
                updated=updated,
                tool=self.name,
                meta={"project": os.path.basename(os.path.dirname(path))},
            ))
        refs.sort(key=lambda ref: ref.updated, reverse=True)
        if not refs:
            raise self._no_sessions("no transcripts under %s" % self.projects_dir)
        return refs[:limit]

    def ref_for(self, session_id: str) -> SessionRef:
        """Resolve a session id to its transcript by filename.

        The transcript is named after the session id, so this is an exact
        lookup rather than a scan of every session: one glob over the project
        directories instead of reading a session list and filtering it. That
        matters because :meth:`sessions` stats every transcript under
        ``projects_dir``, which on a long-lived install is thousands of files.

        The id is matched against the filename stem, not embedded in a glob
        pattern: an id containing ``*`` or ``[`` would otherwise change which
        files the pattern matches. A session that is not on disk resolves to the
        empty-path ref, which the callers render as "history not available".
        """
        session_id = _safe_id(session_id)
        if not session_id:
            return SessionRef(session="", path="", tool=self.name)

        import glob

        matches = glob.glob(os.path.join(
            glob.escape(self.projects_dir), "*", glob.escape(session_id) + ".jsonl"))
        if not matches:
            return SessionRef(session=session_id, path="", tool=self.name)

        # A session id is a UUID and lives in exactly one project directory; if
        # a store somehow holds two, the newest is the one that was read from.
        path = max(matches, key=_mtime)
        return SessionRef(
            session=session_id,
            path=path,
            updated=_mtime(path),
            tool=self.name,
            meta={"project": os.path.basename(os.path.dirname(path))},
        )

    # -- reading ---------------------------------------------------------
    def read_usage(self, session: SessionRef) -> Reading:
        payload = self._newest_usage(session.path)
        if payload is None:
            raise self._read_failed("no assistant usage record found", session.session)

        tokens = _prompt_tokens(payload["usage"])
        compressed, compaction = self._compaction_state(session.path)

        detail = {
            "input_tokens": payload["usage"].get("input_tokens"),
            "cache_read_input_tokens": payload["usage"].get("cache_read_input_tokens"),
            "cache_creation_input_tokens": payload["usage"].get("cache_creation_input_tokens"),
            "output_tokens": payload["usage"].get("output_tokens"),
            "model": payload.get("model"),
            "records_scanned": payload.get("records_scanned"),
        }
        if compaction:
            detail["last_compaction"] = compaction

        window = self.window_setting()
        return Reading(
            tool=self.name,
            session=session.session,
            tokens=tokens,
            window=window,
            source="transcript",
            ts=payload.get("ts") or session.updated,
            compressed=compressed,
            detail=detail,
        )

    def compression_seen(self, session: SessionRef) -> bool:
        compressed, _ = self._compaction_state(session.path)
        return compressed

    def recent_messages(self, session: SessionRef, limit: int = 12) -> List[str]:
        """The newest human/assistant turns, oldest first, heavily truncated."""
        from ..core.util import truncate

        entries: List[Tuple[float, str, str]] = []
        try:
            for record in _iter_records(session.path, tail_bytes=TAIL_BYTES):
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                if record.get("isSidechain"):
                    continue
                role = message.get("role")
                if role not in ("user", "assistant"):
                    continue
                text = _message_text(message)
                if not text:
                    continue
                entries.append((
                    parse_iso(record.get("timestamp")),
                    role,
                    truncate(text, 240),
                ))
        except OSError:
            return []
        if not entries:
            return []
        tail = entries[-limit:]
        return ["[%s] %s" % (role, text) for _ts, role, text in tail]

    def history_unavailable_reason(self) -> str:
        return "transcript could not be parsed"

    # -- internals -------------------------------------------------------
    def _newest_usage(self, path: str) -> Optional[Dict[str, Any]]:
        """Newest assistant usage record in a transcript, tail first."""
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        cached = self._usage_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]

        result: Optional[Dict[str, Any]] = None
        scanned = 0
        for record in _iter_records(path, tail_bytes=TAIL_BYTES, reverse=True):
            scanned += 1
            if record.get("isSidechain"):
                continue
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            if _prompt_tokens(usage) <= 0:
                continue
            result = {
                "usage": usage,
                "model": message.get("model"),
                "ts": parse_iso(record.get("timestamp")),
                "records_scanned": scanned,
            }
            break

        if result is None and scanned:
            # The tail held records but none carried usage; fall back to a full
            # scan, which happens when the newest turn is still being written.
            for record in _iter_records(path):
                message = record.get("message")
                if isinstance(record.get("message"), dict) and not record.get("isSidechain"):
                    usage = message.get("usage")
                    if isinstance(usage, dict) and _prompt_tokens(usage) > 0:
                        result = {
                            "usage": usage,
                            "model": message.get("model"),
                            "ts": parse_iso(record.get("timestamp")),
                            "records_scanned": None,
                        }

        self._usage_cache[path] = (mtime, result)
        return result

    def _compaction_state(self, path: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Whether the transcript holds a compaction boundary, plus its metadata."""
        latest: Optional[Dict[str, Any]] = None
        for record in _iter_records(path, tail_bytes=TAIL_BYTES, reverse=True):
            if record.get("subtype") == "compact_boundary":
                meta = record.get("compactMetadata")
                latest = meta if isinstance(meta, dict) else {}
                break
        if latest is None:
            return False, None
        return True, {
            "trigger": latest.get("trigger"),
            "pre_tokens": latest.get("preTokens"),
            "post_tokens": latest.get("postTokens"),
            "at": latest.get("timestamp"),
        }

    # -- capabilities ----------------------------------------------------
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
                     "rotation writes a handover and asks for a new session; "
                     "starting one is left to the user"),
        })
        return caps

    def rotate(
        self,
        session: str,
        handover_path: Optional[str],
        dry_run: bool = False,
        config: Any = None,
    ) -> RotateResult:
        """Start the replacement session with ``claude -p --output-format json``.

        Assisted mode is the base implementation unchanged. The full-auto path
        spawns one headless turn whose only job is to read the handover and
        continue the work, then reports the session id the tool assigned.

        The session id comes from the spawn's own JSON document. When that
        cannot be parsed -- an older CLI, a wrapper that prints something else --
        the newest transcript that appeared in the same project directory during
        the spawn is used instead. A rotation that started a session but could
        not learn its id is still a rotation: reporting ``rotated=False`` there
        would invite a second spawn of the same handover.
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
        t0 = time.time()
        result = self.run_prompt(
            prompt,
            timeout=self.rotate_timeout(),
            cwd=cwd,
            extra_args=["--output-format", "json"] + self.spawn_args(DEFAULT_SPAWN_ARGS),
        )
        if result.timed_out or result.rc != 0:
            return self._rotate_failure(result.rc, result.stderr, result.timed_out)

        new_session = _session_id_from_output(result.text)
        source = "output" if new_session else ""
        if not new_session:
            new_session = self._newest_transcript_since(old_session, t0)
            source = "transcript" if new_session else "unknown"
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
                "session_id_source": source,
            },
        )

    def _rotate_cwd(self, session_id: str) -> Optional[str]:
        """The working directory the replacement session should start in.

        ``rotate_cwd`` wins. Otherwise the old transcript carries the ``cwd``
        Claude Code recorded for the project the work belongs to; the first
        record can be a ``queue-operation`` line without it, so scan a bounded
        head of the file for the first record that has one. The encoded project
        directory name is not decoded: that encoding is lossy for paths
        containing ``-``.
        """
        configured = self.path_setting("rotate_cwd", "")
        if configured:
            return configured
        ref = self.ref_for(session_id)
        if not ref.path:
            return None
        try:
            with open(ref.path, "r", encoding="utf-8", errors="replace") as stream:
                for _ in range(50):
                    line = stream.readline()
                    if not line:
                        break
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict) and record.get("cwd"):
                        return str(record["cwd"])
        except OSError:
            return None
        return None

    def _newest_transcript_since(self, old_session: str, t0: float) -> Optional[str]:
        """Newest transcript written during the spawn, other than the old one.

        Scoped to the old session's project directory when it can be resolved:
        another project's activity during the spawn is not this rotation's new
        session, and picking it would attribute the next rounds to a stranger.
        """
        roots = None
        ref = self.ref_for(old_session)
        if ref.path:
            roots = [os.path.dirname(ref.path)]
        candidates: List[Tuple[float, str]] = []
        paths = list(self.session_files()) if roots is None else [
            os.path.join(root, name) for root in roots
            for name in _listdir_names(root) if name.endswith(".jsonl")
        ]
        for path in paths:
            if _mtime(path) <= t0 - 2:
                continue
            stem = os.path.basename(path)[: -len(".jsonl")]
            if stem == old_session:
                continue
            candidates.append((_mtime(path), stem))
        if not candidates:
            return None
        return max(candidates)[1]

    def one_shot_command(self) -> List[str]:
        return ["claude", "-p"]

    def start_hint(self, handover_path: Optional[str] = None) -> str:
        if handover_path:
            return ("start `claude` in your project and say: read %s and continue "
                    "the work it describes" % handover_path)
        return "start `claude` in your project directory"

    def run_prompt(
        self,
        prompt: str,
        timeout: float = 240.0,
        cwd: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
    ) -> PromptResult:
        """Run ``claude -p <prompt>`` with a cleaned environment.

        The environment matters more here than anywhere else: ``claude`` reads
        ``CLAUDE_CODE_*`` markers to decide whether it is joining an existing
        session. Inheriting them would test the parent session instead of a
        fresh one.
        """
        cmd = list(self.one_shot_command()) + list(extra_args or []) + [prompt]
        rc, stdout, stderr, timed_out = self._spawn(cmd, timeout, cwd)
        return PromptResult(
            text=(stdout or "").strip(),
            rc=rc,
            timed_out=timed_out,
            stderr=(stderr or "").strip()[:2000],
        )


def _session_id_from_output(text: str) -> Optional[str]:
    """``session_id`` from ``claude -p --output-format json`` output.

    The CLI prints one JSON document. A wrapper that prepends a banner would
    still leave the object on its own line, so the last line that parses as an
    object is tried as well; anything else is simply not found.
    """
    text = (text or "").strip()
    if not text:
        return None
    candidates = [text] + list(reversed(text.splitlines()))
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate.startswith("{"):
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        session_id = payload.get("session_id")
        if session_id:
            return str(session_id)
    return None


def _safe_id(session_id: Any) -> str:
    """A session id that is safe to use as a path component.

    Ids are UUIDs. Anything else -- an empty string, a value carrying a path
    separator, a ``..`` -- is refused rather than joined onto a directory, so a
    crafted id cannot walk out of the projects directory.
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


def _listdir_names(path: str) -> List[str]:
    try:
        return os.listdir(path)
    except OSError:
        return []


def _prompt_tokens(usage: Dict[str, Any]) -> int:
    """Prompt size for one usage record.

    ``input_tokens`` is the whole prompt under the dominant convention;
    ``cache_read + cache_creation`` is the whole prompt under the other. Taking
    the larger is correct under both and never double-counts.
    """
    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    input_tokens = _int(usage.get("input_tokens"))
    cached = _int(usage.get("cache_read_input_tokens")) + _int(
        usage.get("cache_creation_input_tokens"))
    return max(input_tokens, cached)


def _message_text(message: Dict[str, Any]) -> str:
    """Flatten a message's content into plain text."""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts: List[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return " ".join(part for part in parts if part).strip()


def _iter_records(
    path: str,
    tail_bytes: Optional[int] = None,
    reverse: bool = False,
) -> Iterator[Dict[str, Any]]:
    """Yield JSON records from a transcript.

    ``tail_bytes`` limits the read to the end of the file; the first line of a
    partial read is discarded because it is probably truncated mid-record.
    ``reverse`` yields newest first, which is what a "find the latest" scan
    wants.
    """
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

    text = payload.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start and lines:
        # Drop the partial first line.
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
