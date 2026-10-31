"""Configuration-driven adapter for any other tool.

Use this when a tool is not covered by a dedicated adapter. Each session is one
entry under ``adapters.generic.sessions``::

    adapters:
      generic:
        enabled: true
        sessions:
          - id: my-agent-main
            window: 128000
            source:
              type: file
              path: ~/.my-agent/usage.json
          - id: my-agent-worker
            source:
              type: command
              cmd: ["my-agent", "usage", "--json"]
              timeout: 15

A source produces a JSON object with a token count and optionally a window::

    {"tokens": 41230, "window": 128000}

Recognised key spellings for the count are ``tokens``, ``input_tokens``,
``prompt_tokens`` and ``total_tokens``; adding to the list is a one-line change.
For a file source the whole file is parsed as JSON, and if that fails the last
JSON object in the file is used instead -- which covers both "a status file" and
"an append-only log of readings".

Command sources run with a cleaned environment and a hard timeout, so a wrapper
script cannot inherit the calling agent's session markers.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ..core.usage import Reading
from ..core.util import clean_env, run_cmd
from .base import EXPERIMENTAL, Adapter, PromptResult, SessionRef

_TOKEN_KEYS = ("tokens", "input_tokens", "prompt_tokens", "total_tokens",
               "inputTokens", "promptTokens", "totalTokens")
_WINDOW_KEYS = ("window", "context_window", "contextWindow", "max_tokens")
_PCT_KEYS = ("pct", "percent", "percentage", "used_pct")


class GenericAdapter(Adapter):
    """Adapter driven entirely by configuration."""

    name = "generic"
    VERIFICATION = EXPERIMENTAL
    VERIFICATION_NOTE = (
        "behaviour depends on the configured source; correctness is the "
        "configuration's responsibility, not a verified integration"
    )

    def entries(self) -> List[Dict[str, Any]]:
        configured = self.setting("sessions", default=[]) or []
        return [entry for entry in configured if isinstance(entry, dict)]

    def detect(self) -> bool:
        """Available when at least one configured source exists locally.

        Unlike the other adapters there is no install to look for, so the
        presence of a usable source is the signal. A command source counts as
        present because whether it works can only be known by running it.
        """
        for entry in self.entries():
            source = entry.get("source")
            if not isinstance(source, dict):
                continue
            if source.get("type") == "command":
                return True
            path = source.get("path") or source.get("glob")
            if path and _source_pattern_exists(os.path.expanduser(str(path))):
                return True
        return False

    def sessions(self, limit: int = 20) -> List[SessionRef]:
        refs: List[SessionRef] = []
        for index, entry in enumerate(self.entries()):
            session_id = str(entry.get("id") or "session-%d" % (index + 1))
            source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
            refs.append(SessionRef(
                session=session_id,
                path=str(source.get("path") or source.get("cmd") or ""),
                updated=0.0,
                tool=self.name,
                meta={"entry": entry},
            ))
        if not refs:
            raise self._no_sessions("no `sessions` entries configured")
        return refs[:limit]

    def read_usage(self, session: SessionRef) -> Reading:
        entry = session.meta.get("entry") or {}
        source = entry.get("source")
        if not isinstance(source, dict):
            raise self._read_failed("session entry has no `source` mapping",
                                    session.session)
        payload = self._collect(source, session.session)
        tokens = _int_from(payload, _TOKEN_KEYS)
        if tokens is None:
            raise self._read_failed(
                "source produced no token count (expected one of: %s)"
                % ", ".join(_TOKEN_KEYS), session.session)

        window = _int_from(payload, _WINDOW_KEYS)
        if window is None:
            configured = entry.get("window")
            try:
                window = int(configured) if configured is not None else None
            except (TypeError, ValueError):
                window = None
        if window is None:
            window = self.window_setting()

        compressed = bool(payload.get("compressed", False))
        ts = _float_from(payload, ("ts", "timestamp", "time")) or 0.0

        return Reading(
            tool=str(entry.get("tool") or self.name),
            session=session.session,
            tokens=tokens,
            window=window,
            source="external",
            ts=ts,
            compressed=compressed,
            detail={
                "source_type": source.get("type"),
                "reported_pct": _float_from(payload, _PCT_KEYS),
                "keys": sorted(payload.keys()),
            },
        )

    def capabilities(self) -> Dict[str, Any]:
        caps = super().capabilities()
        # `run_prompt` is configuration-dependent here, unlike the fixed
        # adapters: without a `prompt_cmd` there is nothing to run, and claiming
        # otherwise would make the gate report five infrastructure failures
        # instead of one honest skip.
        has_prompt = bool(self.setting("prompt_cmd", default=None))
        caps.update({
            "history": False,
            "rotate": self._rotation_configured(),
            "full_auto": self._rotation_configured(),
            "run_prompt": has_prompt,
            "note": "sources are declared in configuration; a command source "
                    "can also implement rotation through your own wrapper",
        })
        return caps

    def _rotation_configured(self) -> bool:
        return any(entry.get("rotate_cmd") for entry in self.entries())

    def rotate(
        self,
        session: str,
        handover_path: Optional[str],
        dry_run: bool = False,
        config: Any = None,
    ) -> Any:
        """Run the configured rotation command, when one is present.

        The entry may declare ``rotate_cmd``; it is executed with the handover
        path in the ``CUSTOS_HANDOVER`` environment variable and the session
        id in ``CUSTOS_SESSION``. Without it, rotation is reported as
        assisted.
        """
        from .base import RotateResult

        entry = self._entry_for(session)
        rotate_cmd = (entry or {}).get("rotate_cmd")
        if not rotate_cmd:
            return super().rotate(session, handover_path, dry_run, config)
        if isinstance(rotate_cmd, str):
            cmd = [rotate_cmd]
        elif isinstance(rotate_cmd, list):
            cmd = [str(part) for part in rotate_cmd]
        else:
            return RotateResult(
                mode="assisted",
                rotated=False,
                error="`rotate_cmd` must be a string or a list",
            )

        if dry_run:
            return RotateResult(
                mode="full-auto",
                rotated=False,
                detail={"would_run": cmd, "handover": handover_path},
            )

        env = clean_env()
        env["CUSTOS_HANDOVER"] = handover_path or ""
        env["CUSTOS_SESSION"] = session
        rc, stdout, stderr, timed_out = run_cmd(
            cmd, env=env, timeout=float((entry or {}).get("rotate_timeout", 120)))
        ok = rc == 0 and not timed_out
        detail: Dict[str, Any] = {
            "command": cmd[0],
            "rc": rc,
            "timed_out": timed_out,
            "stdout": stdout.strip()[:500],
        }
        new_session = stdout.strip().splitlines()[-1].strip() if ok and stdout.strip() else None
        return RotateResult(
            mode="full-auto",
            rotated=ok,
            new_session=new_session or None,
            detail=detail,
            error=None if ok else (stderr.strip()[:300] or "command failed"),
        )

    def one_shot_command(self) -> List[str]:
        """The configured ``prompt_cmd``, with the prompt placeholder kept.

        This is what a dry run prints, so it has to show the command that would
        actually run -- returning nothing (the base default) would make the plan
        read as "no command" while a real fire runs one.
        """
        template = self.setting("prompt_cmd", default=None)
        if not template:
            return []
        if isinstance(template, str):
            return [template]
        return [str(part) for part in template]

    def run_prompt(
        self,
        prompt: str,
        timeout: float = 240.0,
        cwd: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
    ) -> PromptResult:
        """Run the configured ``prompt_cmd`` template.

        The template is a list; an item equal to ``{prompt}`` is replaced by the
        prompt text, so the caller controls where it lands in the argument list.
        """
        template = self.setting("prompt_cmd", default=None)
        if not template:
            return PromptResult(rc=-1, stderr="no `prompt_cmd` configured")
        if isinstance(template, str):
            template = [template]
        cmd = [prompt if str(part) == "{prompt}" else str(part) for part in template]
        if prompt not in cmd:
            cmd.append(prompt)
        rc, stdout, stderr, timed_out = self._spawn(cmd, timeout, cwd)
        return PromptResult(
            text=(stdout or "").strip(),
            rc=rc,
            timed_out=timed_out,
            stderr=(stderr or "").strip()[:2000],
        )

    # -- internals -------------------------------------------------------
    def _entry_for(self, session_id: str) -> Optional[Dict[str, Any]]:
        for entry in self.entries():
            if str(entry.get("id")) == str(session_id):
                return entry
        return None

    def _collect(self, source: Dict[str, Any], session_id: str) -> Dict[str, Any]:
        """Read one configured source and return its JSON object."""
        source_type = str(source.get("type") or "file")
        if source_type == "command":
            return self._collect_command(source)
        if source_type == "file":
            return self._collect_file(source, session_id)
        raise self._read_failed("unknown source type %r" % source_type, session_id)

    def _collect_command(self, source: Dict[str, Any]) -> Dict[str, Any]:
        cmd = source.get("cmd")
        if isinstance(cmd, str):
            cmd = [cmd]
        if not isinstance(cmd, list) or not cmd:
            raise self._read_failed("command source needs a `cmd` list")
        timeout = float(source.get("timeout", 30))
        rc, stdout, stderr, timed_out = run_cmd(
            [str(part) for part in cmd], env=clean_env(), timeout=timeout)
        if timed_out:
            raise self._read_failed("command timed out after %gs" % timeout)
        if rc != 0:
            raise self._read_failed("command exited %s: %s" % (rc, stderr.strip()[:200]))
        payload = _parse_json_object(stdout)
        if payload is None:
            raise self._read_failed("command did not print a JSON object")
        return payload

    def _collect_file(self, source: Dict[str, Any], session_id: str) -> Dict[str, Any]:
        raw_path = source.get("path") or source.get("glob")
        if not raw_path:
            raise self._read_failed("file source needs a `path`")
        import glob as glob_module

        matches = sorted(glob_module.glob(os.path.expanduser(str(raw_path))))
        matches = [path for path in matches if os.path.isfile(path)]
        if not matches:
            raise self._read_failed("no file matched %s" % raw_path, session_id)
        newest = max(matches, key=lambda path: os.path.getmtime(path))
        try:
            with open(newest, "r", encoding="utf-8", errors="replace") as stream:
                text = stream.read()
        except OSError as exc:
            raise self._read_failed("cannot read %s: %s" % (newest, exc), session_id)
        payload = _parse_json_object(text)
        if payload is None:
            raise self._read_failed(
                "no JSON object found in %s" % newest, session_id)
        return payload


def _int_from(payload: Dict[str, Any], keys: tuple) -> Optional[int]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            try:
                return int(float(value))
            except ValueError:
                continue
    return None


def _float_from(payload: Dict[str, Any], keys: tuple) -> Optional[float]:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _parse_json_object(text: str) -> Optional[Dict[str, Any]]:
    """Parse JSON, falling back to the last object in a stream of them."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except ValueError:
        pass
    # Last complete line that parses, for append-only logs.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _source_pattern_exists(pattern: str) -> bool:
    """Whether a configured path or glob matches at least one file."""
    import glob as glob_module

    try:
        return bool(glob_module.glob(pattern))
    except (OSError, ValueError):
        return False
