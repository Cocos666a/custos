"""OpenCode adapter.

OpenCode keeps its state in a SQLite database at
``~/.local/share/opencode/opencode.db``. Older or differently configured
installs keep a ``storage/`` directory next to it; that directory is detected so
``detect()`` can answer "this tool is installed" even when the database is
somewhere unexpected.

How occupancy is read
---------------------

The ``message`` table stores one JSON document per message, and assistant
documents carry a ``tokens`` object::

    {"total": 334311, "input": 2062, "output": 664, "reasoning": 65,
     "cache": {"read": 331520, "write": 0}}

``total`` equals ``input + cache.read + output + reasoning``, which makes it a
*cumulative* figure for the message, not the prompt size. The prompt that
occupied the window on that call is ``input + cache.read``: on the example above,
331520 of the 334311 tokens are a cached prefix that was still sent.

The ``session`` table also carries ``tokens_input`` and ``tokens_cache_read``.
Those are sums over every message in the session -- the local database has a
session reporting ``tokens_input=2853144`` against a final message of
``input=2062`` -- so they describe how much work the session has done, not how
full it is. They are reported as context but never used as the reading.

The window is not recorded anywhere in the database, so the configured value is
used.

A session with ``parent_id`` set is a sub-agent session; its messages belong to
a separate context and are skipped when choosing the session to report.

``session.time_compacting`` is non-null while a compaction is in flight, which
is the one reliable busy signal this tool exposes; the adapter uses it for the
rotation interlock.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

from ..core.usage import Reading
from .base import DOC_VERIFIED, VERIFIED, Adapter, PromptResult, SessionRef

DEFAULT_DB_PATH = "~/.local/share/opencode/opencode.db"
DEFAULT_STORAGE_DIR = "~/.local/share/opencode/storage"

# Columns this adapter reads. Each is probed before use so a schema change
# degrades into a clear error instead of an SQL failure.
_SESSION_COLUMNS = (
    "id", "parent_id", "time_created", "time_updated", "time_compacting",
    "tokens_input", "tokens_output", "tokens_cache_read", "tokens_cache_write",
    "directory", "title", "model", "cost",
)


class OpenCodeAdapter(Adapter):
    """Adapter for the OpenCode SQLite store."""

    name = "opencode"
    # Compaction state is readable from the database; an in-flight turn is not.
    BUSY_DETECTION = True
    VERIFICATION = VERIFIED
    VERIFICATION_NOTE = (
        "occupancy from the newest assistant message's input + cache.read; "
        "cross-checked against the session's cumulative counters on real data"
    )

    @property
    def db_path(self) -> str:
        return self.path_setting("db_path", DEFAULT_DB_PATH)

    @property
    def storage_dir(self) -> str:
        return self.path_setting("storage_dir", DEFAULT_STORAGE_DIR)

    def detect(self) -> bool:
        return os.path.isfile(self.db_path) or os.path.isdir(self.storage_dir)

    # -- connection ------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """Open the store read-only.

        ``mode=ro`` is enforced by SQLite itself, so an adapter bug cannot write
        to the user's database even by accident.
        """
        if not os.path.isfile(self.db_path):
            if os.path.isdir(self.storage_dir):
                raise self._read_failed(
                    "no database at %s; this install only has a legacy storage "
                    "directory (%s), which this adapter cannot read"
                    % (self.db_path, self.storage_dir))
            raise self._read_failed("database not found at %s" % self.db_path)
        try:
            connection = sqlite3.connect(
                "file:%s?mode=ro" % _uri_path(self.db_path), uri=True, timeout=10)
        except sqlite3.Error as exc:
            raise self._read_failed("cannot open database read-only: %s" % exc)
        connection.row_factory = sqlite3.Row
        return connection

    def _columns(self, connection: sqlite3.Connection, table: str) -> List[str]:
        try:
            rows = connection.execute('PRAGMA table_info("%s")' % table).fetchall()
        except sqlite3.Error:
            return []
        return [row["name"] for row in rows]

    def _selectable(self, available: List[str], wanted: tuple) -> str:
        """Build a SELECT list from the columns that actually exist."""
        chosen = [name for name in wanted if name in available]
        if not chosen:
            raise self._read_failed(
                "none of the expected columns exist (found: %s)" % ", ".join(available))
        return ", ".join('"%s"' % name for name in chosen)

    # -- sessions --------------------------------------------------------
    def sessions(self, limit: int = 20) -> List[SessionRef]:
        connection = self._connect()
        try:
            available = self._columns(connection, "session")
            if not available:
                raise self._read_failed("no `session` table in %s" % self.db_path)
            selection = self._selectable(available, _SESSION_COLUMNS)
            rows = connection.execute(
                'SELECT %s FROM "session" ORDER BY time_updated DESC LIMIT ?' % selection,
                (max(1, limit * 3),),
            ).fetchall()
        except sqlite3.Error as exc:
            raise self._read_failed("cannot query sessions: %s" % exc)
        finally:
            connection.close()

        refs: List[SessionRef] = []
        for row in rows:
            record = dict(row)
            if record.get("parent_id"):
                # Sub-agent session: a different context window.
                continue
            refs.append(SessionRef(
                session=str(record.get("id") or ""),
                path=self.db_path,
                updated=_epoch(record.get("time_updated")),
                tool=self.name,
                meta={
                    "directory": record.get("directory"),
                    "title": record.get("title"),
                },
            ))
        if not refs:
            raise self._no_sessions("no top-level sessions in %s" % self.db_path)
        return refs[:limit]

    def ref_for(self, session_id: str) -> SessionRef:
        """Resolve a session id against one row of the ``session`` table.

        Every OpenCode ref carries the same ``path`` -- the database -- so the
        base implementation's scan would find a ref no more usable than a
        placeholder. What a caller actually needs is the confirmation that the
        row exists and the timestamps that go with it, and a primary-key lookup
        is both cheaper and more precise than filtering the session list.
        """
        session_id = str(session_id or "").strip()
        if not session_id:
            return SessionRef(session="", path="", tool=self.name)

        connection = self._connect()
        try:
            session_row = self._load_session(connection, session_id)
        except Exception:
            session_row = None
        finally:
            connection.close()

        if session_row is None or session_row.get("parent_id"):
            return SessionRef(session=session_id, path="", tool=self.name)

        return SessionRef(
            session=session_id,
            path=self.db_path,
            updated=_epoch(session_row.get("time_updated")),
            tool=self.name,
            meta={
                "directory": session_row.get("directory"),
                "title": session_row.get("title"),
            },
        )

    def read_usage(self, session: SessionRef) -> Reading:
        connection = self._connect()
        try:
            session_row = self._load_session(connection, session.session)
            if session_row is None:
                raise self._read_failed("session row disappeared", session.session)
            tokens, message_detail = self._newest_assistant_tokens(
                connection, session.session)
            if tokens is None:
                raise self._read_failed(
                    "no assistant message with token data", session.session)
        finally:
            connection.close()

        window = self.window_setting()
        detail = {
            "prompt_tokens": message_detail.get("prompt"),
            "message_total": message_detail.get("total"),
            "message_input": message_detail.get("input"),
            "message_cache_read": message_detail.get("cache_read"),
            "message_output": message_detail.get("output"),
            "message_reasoning": message_detail.get("reasoning"),
            "message_id": message_detail.get("message_id"),
            "session_cumulative_input": session_row.get("tokens_input"),
            "session_cumulative_cache_read": session_row.get("tokens_cache_read"),
            "compacting": session_row.get("time_compacting") is not None,
        }
        return Reading(
            tool=self.name,
            session=session.session,
            tokens=tokens,
            window=window,
            source="transcript",
            ts=_epoch(session_row.get("time_updated")) or session.updated,
            compressed=False,
            detail=detail,
        )

    def busy(self, session: SessionRef) -> Optional[bool]:
        """``True`` while a compaction is running; ``False`` otherwise.

        OpenCode does not expose "a turn is in flight" through this database, so
        the only thing that can be observed is an in-progress compaction. The
        answer is ``False`` rather than ``None`` when a session row was read and
        no compaction is running: ``None`` would mean "the check failed", and
        the rotation pipeline defers on that. The activity quiet window is what
        protects a session that is mid-turn.
        """
        connection = self._connect()
        try:
            session_row = self._load_session(connection, session.session)
        except Exception:
            return None
        finally:
            connection.close()
        if session_row is None:
            return None
        if session_row.get("time_compacting") is not None:
            return True
        return False

    def recent_messages(self, session: SessionRef, limit: int = 12) -> List[str]:
        """Newest user/assistant text from the ``part`` table, oldest first."""
        from ..core.util import truncate

        connection = self._connect()
        try:
            available = self._columns(connection, "part")
            if "data" not in available:
                return []
            rows = connection.execute(
                'SELECT data FROM "part" WHERE session_id = ? '
                "ORDER BY time_created DESC LIMIT ?",
                (session.session, max(1, limit * 6)),
            ).fetchall()
        except sqlite3.Error:
            return []
        finally:
            connection.close()

        texts: List[str] = []
        for row in reversed(rows):
            try:
                payload = json.loads(row["data"])
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "text":
                continue
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(truncate(text, 240))
        return ["[text] %s" % text for text in texts[-limit:]]

    def capabilities(self) -> Dict[str, Any]:
        caps = super().capabilities()
        caps.update({
            "history": True,
            "rotate": False,
            "full_auto": False,
            "run_prompt": True,
            "busy_detection": "compaction only",
            "note": "a running opencode server also exposes an HTTP API for "
                    "session control; this adapter reads the database only",
        })
        return caps

    def one_shot_command(self) -> List[str]:
        return ["opencode", "run"]

    def start_hint(self, handover_path: Optional[str] = None) -> str:
        if handover_path:
            return ("start `opencode` in your project and ask it to read %s before "
                    "continuing" % handover_path)
        return "start `opencode` in your project directory"

    def run_prompt(
        self,
        prompt: str,
        timeout: float = 240.0,
        cwd: Optional[str] = None,
        extra_args: Optional[List[str]] = None,
    ) -> PromptResult:
        """Run ``opencode run <prompt>`` with a cleaned environment."""
        cmd = list(self.one_shot_command()) + list(extra_args or []) + [prompt]
        rc, stdout, stderr, timed_out = self._spawn(cmd, timeout, cwd)
        return PromptResult(
            text=(stdout or "").strip(),
            rc=rc,
            timed_out=timed_out,
            stderr=(stderr or "").strip()[:2000],
        )

    # -- internals -------------------------------------------------------
    def _load_session(
        self, connection: sqlite3.Connection, session_id: str
    ) -> Optional[Dict[str, Any]]:
        available = self._columns(connection, "session")
        if not available:
            return None
        selection = self._selectable(available, _SESSION_COLUMNS)
        try:
            row = connection.execute(
                'SELECT %s FROM "session" WHERE id = ?' % selection, (session_id,)
            ).fetchone()
        except sqlite3.Error:
            return None
        return dict(row) if row else None

    def _newest_assistant_tokens(
        self, connection: sqlite3.Connection, session_id: str
    ):
        """Prompt size from the newest assistant message, plus its detail."""
        available = self._columns(connection, "message")
        if "data" not in available:
            raise self._read_failed("`message` table has no data column")
        order_column = "time_created" if "time_created" in available else "rowid"
        try:
            rows = connection.execute(
                'SELECT data FROM "message" WHERE session_id = ? '
                "ORDER BY %s DESC LIMIT 40" % order_column,
                (session_id,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise self._read_failed("cannot query messages: %s" % exc)

        for row in rows:
            try:
                payload = json.loads(row["data"])
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("role") != "assistant":
                continue
            tokens = payload.get("tokens")
            if not isinstance(tokens, dict):
                continue
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            input_tokens = _int(tokens.get("input"))
            cache_read = _int(cache.get("read"))
            prompt = input_tokens + cache_read
            if prompt <= 0:
                continue
            return prompt, {
                "prompt": prompt,
                "total": _int(tokens.get("total")),
                "input": input_tokens,
                "cache_read": cache_read,
                "output": _int(tokens.get("output")),
                "reasoning": _int(tokens.get("reasoning")),
                "message_id": payload.get("id"),
            }
        return None, {}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _epoch(value: Any) -> float:
    """OpenCode stores millisecond epochs."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number > 1e11:
        return number / 1000.0
    return number


def _uri_path(path: str) -> str:
    """Encode a filesystem path for a SQLite URI.

    ``?`` and ``#`` would otherwise terminate the path, and the mode parameter
    is appended by the caller.
    """
    return path.replace("?", "%3f").replace("#", "%23")
