"""Shared fixtures: a throwaway config directory and a fake home, per test.

Every test in this suite runs against ``tmp_path``. Nothing here reads a real
session store, starts an agent, or reaches the network, and that is a
requirement rather than a preference: a suite that reads ``~/.claude/projects``
passes on the machine it was written on and fails everywhere else, and CI has no
agent installs at all.

Two environment variables do the sandboxing, because they are the two the tool
itself honours:

``CUSTOS_CONFIG_DIR``
    where state, handovers, readings and the lock live.
``HOME``
    where the adapters look for agent stores (``~/.claude/projects`` and
    friends), so pointing it at a temporary directory means an adapter cannot
    find -- or touch -- the real one even by accident.

``sys.path`` is extended with the repository root so ``import custos`` works
from a checkout with no installation step, under both ``pytest`` and
``python -m pytest``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Every store an adapter looks for, relative to HOME. Kept in one place so a
# test that wants "no tools installed" only has to leave the list alone.
STORE_DIRS = (
    ".claude/projects",
    ".codex/sessions",
    ".codex/archived_sessions",
    ".local/share/opencode",
    ".pi/agent/sessions",
)


class Sandbox:
    """One test's private home directory, config directory and process runner."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.home = os.path.join(root, "home")
        self.config_dir = os.path.join(root, "config")
        os.makedirs(self.home, exist_ok=True)
        os.makedirs(self.config_dir, exist_ok=True)

    # -- paths -----------------------------------------------------------
    def path(self, *parts: str) -> str:
        return os.path.join(self.config_dir, *parts)

    def home_path(self, *parts: str) -> str:
        return os.path.join(self.home, *parts)

    def tree(self) -> List[str]:
        """Every path under the config directory, relative and sorted.

        The dry-run tests assert on this: "nothing was written" is a claim about
        the whole sandbox, not about one file the test remembered to check.
        """
        found: List[str] = []
        for dirpath, _dirnames, filenames in os.walk(self.config_dir):
            for name in filenames:
                full = os.path.join(dirpath, name)
                found.append(os.path.relpath(full, self.config_dir))
        return sorted(found)

    # -- configuration ----------------------------------------------------
    def write_config(self, data: Optional[Dict[str, Any]] = None) -> str:
        path = self.path("config.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(data or {"version": 1}, stream, indent=1)
        return path

    def write_questions(self, payload: Any, name: str = "questions.json") -> str:
        path = self.path(name)
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=1)
        return path

    # -- synthetic stores -------------------------------------------------
    def write_lines(self, path: str, records: Sequence[Dict[str, Any]]) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record) + "\n")
        return path

    def claude_transcript(
        self,
        session: str = "11111111-2222-3333-4444-555555555555",
        project: str = "synthetic-project",
        records: Sequence[Dict[str, Any]] = (),
    ) -> str:
        path = self.home_path(".claude", "projects", project, "%s.jsonl" % session)
        return self.write_lines(path, records)

    def codex_rollout(
        self,
        session: str = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        records: Sequence[Dict[str, Any]] = (),
        day: str = "2026/10/16",
    ) -> str:
        name = "rollout-2026-10-16T10-00-00-%s.jsonl" % session
        path = self.home_path(".codex", "sessions", *day.split("/"), name)
        return self.write_lines(path, records)

    def pi_session(
        self,
        session: str = "pi-session-0001",
        records: Sequence[Dict[str, Any]] = (),
    ) -> str:
        path = self.home_path(".pi", "agent", "sessions", "%s.jsonl" % session)
        return self.write_lines(path, records)

    def opencode_db(
        self,
        sessions: Sequence[Dict[str, Any]] = (),
        messages: Sequence[Dict[str, Any]] = (),
        parts: Sequence[Dict[str, Any]] = (),
    ) -> str:
        """Build a SQLite store shaped like OpenCode's.

        The schema is written here rather than copied from a real install: the
        adapter probes for the columns it needs, so what has to be faithful is
        the column set, and a fixture that carried real rows would defeat the
        point of a hermetic suite.
        """
        path = self.home_path(".local", "share", "opencode", "opencode.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                """
                CREATE TABLE session (
                    id TEXT PRIMARY KEY,
                    parent_id TEXT,
                    time_created INTEGER,
                    time_updated INTEGER,
                    time_compacting INTEGER,
                    tokens_input INTEGER,
                    tokens_output INTEGER,
                    tokens_cache_read INTEGER,
                    tokens_cache_write INTEGER,
                    directory TEXT,
                    title TEXT,
                    model TEXT,
                    cost REAL
                );
                CREATE TABLE message (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    time_created INTEGER,
                    data TEXT
                );
                CREATE TABLE part (
                    id TEXT PRIMARY KEY,
                    session_id TEXT,
                    time_created INTEGER,
                    data TEXT
                );
                """
            )
            for row in sessions:
                connection.execute(
                    "INSERT INTO session (%s) VALUES (%s)"
                    % (", ".join(row), ", ".join("?" for _ in row)),
                    tuple(row.values()),
                )
            for row in messages:
                connection.execute(
                    "INSERT INTO message (%s) VALUES (%s)"
                    % (", ".join(row), ", ".join("?" for _ in row)),
                    tuple(row.values()),
                )
            for row in parts:
                connection.execute(
                    "INSERT INTO part (%s) VALUES (%s)"
                    % (", ".join(row), ", ".join("?" for _ in row)),
                    tuple(row.values()),
                )
            connection.commit()
        finally:
            connection.close()
        return path

    # -- running the CLI --------------------------------------------------
    def env(self, **extra: str) -> Dict[str, str]:
        env = dict(os.environ)
        env["HOME"] = self.home
        env["CUSTOS_CONFIG_DIR"] = self.config_dir
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        env.pop("CUSTOS_DEBUG", None)
        env.update(extra)
        return env

    def run(self, *args: str, **kwargs: Any) -> subprocess.CompletedProcess:
        """Run the CLI in a subprocess with the sandbox environment."""
        extra = kwargs.pop("env", None) or {}
        timeout = kwargs.pop("timeout", 120)
        return subprocess.run(
            [sys.executable, "-m", "custos"] + list(args),
            cwd=self.root,
            env=self.env(**extra),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )

    def run_tool(self, script: str, *args: str) -> subprocess.CompletedProcess:
        """Run ``tools/<script>`` from the repository root."""
        return subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "tools", script)] + list(args),
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=300,
        )


@pytest.fixture()
def sandbox(tmp_path, monkeypatch) -> Sandbox:
    """A private HOME and config directory for one test."""
    box = Sandbox(str(tmp_path))
    for relative in STORE_DIRS:
        # Created empty so "installed but no sessions" and "not installed" stay
        # distinguishable: an adapter whose directory is absent reports the
        # former, and several tests care about which one they get.
        os.makedirs(box.home_path(*relative.split("/")), exist_ok=True)
    monkeypatch.setenv("HOME", box.home)
    monkeypatch.setenv("CUSTOS_CONFIG_DIR", box.config_dir)
    monkeypatch.delenv("CUSTOS_DEBUG", raising=False)
    return box


@pytest.fixture()
def denv(tmp_path, monkeypatch) -> Sandbox:
    """A sandbox whose HOME has no agent stores at all."""
    box = Sandbox(str(tmp_path))
    monkeypatch.setenv("HOME", box.home)
    monkeypatch.setenv("CUSTOS_CONFIG_DIR", box.config_dir)
    return box


@pytest.fixture()
def repo_root() -> str:
    return REPO_ROOT


# --------------------------------------------------------------------- helpers


def claude_usage(
    input_tokens: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
    output: int = 0,
) -> Dict[str, int]:
    """A ``message.usage`` block, shaped like Claude Code's."""
    return {
        "input_tokens": input_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "output_tokens": output,
    }


def claude_assistant(
    usage: Dict[str, int],
    *,
    model: str = "synthetic-model",
    ts: str = "2026-10-15T10:00:00Z",
    sidechain: bool = False,
    text: str = "Synthetic assistant turn.",
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "type": "assistant",
        "timestamp": ts,
        "message": {
            "role": "assistant",
            "model": model,
            "usage": usage,
            "content": [{"type": "text", "text": text}],
        },
    }
    if sidechain:
        record["isSidechain"] = True
    return record


def claude_user(text: str = "Synthetic user turn.",
                ts: str = "2026-10-15T10:00:00Z") -> Dict[str, Any]:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


def claude_compact_boundary(
    pre: int = 90000, post: int = 8000,
    ts: str = "2026-10-15T10:05:00Z",
) -> Dict[str, Any]:
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "timestamp": ts,
        "compactMetadata": {
            "trigger": "auto",
            "preTokens": pre,
            "postTokens": post,
            "timestamp": ts,
        },
    }


def codex_token_count(
    input_tokens: int,
    *,
    window: int = 258400,
    total_input: int = 1000000,
    cached: int = 0,
    output: int = 100,
    ts: str = "2026-10-15T10:00:00Z",
) -> Dict[str, Any]:
    """One ``event_msg``/``token_count`` record."""
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": total_input,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": 0,
                    "total_tokens": total_input + output,
                },
                "last_token_usage": {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached,
                    "output_tokens": output,
                    "reasoning_output_tokens": 0,
                    "total_tokens": input_tokens + output,
                },
                "model_context_window": window,
            },
        },
    }


def codex_message(role: str = "assistant", text: str = "Synthetic turn.",
                  ts: str = "2026-10-15T10:00:00Z") -> Dict[str, Any]:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": "output_text" if role == "assistant" else "input_text",
                         "text": text}],
        },
    }


def opencode_message(
    session_id: str,
    tokens: Dict[str, Any],
    *,
    message_id: str = "msg-1",
    role: str = "assistant",
    created: int = 1770000000000,
) -> Dict[str, Any]:
    return {
        "id": message_id,
        "session_id": session_id,
        "time_created": created,
        "data": json.dumps({"id": message_id, "role": role, "tokens": tokens}),
    }
