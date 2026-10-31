"""Full-auto rotation for the Claude Code and Codex adapters.

The counterexample at the top is the defect this feature exists to remove: both
adapters report a rotation they did not perform. Everything below it pins the
new behaviour -- the spawn, the session discovery, and the failure paths -- to
the adapter boundary, with the spawned CLI replaced by a canned result so no
test ever starts a real agent.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from custos.adapters.base import PromptResult, RotateResult, SessionRef
from custos.adapters.claude_code import ClaudeCodeAdapter
from custos.adapters.codex import CodexAdapter


def load_config(sandbox, adapters=None, **extra):
    from custos.core import config as config_module

    payload = {"version": 1}
    if adapters:
        payload["adapters"] = adapters
    payload.update(extra)
    sandbox.write_config(payload)
    return config_module.load_config(sandbox.config_dir)


def claude_adapter(sandbox, **settings):
    defaults = {"projects_dir": sandbox.home_path(".claude", "projects")}
    defaults.update(settings)
    return ClaudeCodeAdapter(load_config(sandbox, {"claude_code": defaults}))


def codex_adapter(sandbox, **settings):
    defaults = {
        "sessions_dir": sandbox.home_path(".codex", "sessions"),
        "archived_dir": sandbox.home_path(".codex", "archived_sessions"),
    }
    defaults.update(settings)
    return CodexAdapter(load_config(sandbox, {"codex": defaults}))


# ------------------------------------------------------- counterexample


def test_counterexample_claude_code_defaults_to_assisted(sandbox):
    """Reproduces the defect: the adapter reports no rotation and no spawn."""
    adapter = claude_adapter(sandbox)
    result = adapter.rotate("old-session", "/tmp/handover.md")
    assert result.mode == "assisted"
    assert result.rotated is False


def test_counterexample_codex_defaults_to_assisted(sandbox):
    adapter = codex_adapter(sandbox)
    result = adapter.rotate("old-session", "/tmp/handover.md")
    assert result.mode == "assisted"
    assert result.rotated is False


# ------------------------------------------------------- claude code spawn


class SpawnRecorder:
    """Stands in for `_spawn`, capturing the command and answering canned."""

    def __init__(self, rc=0, stdout="", stderr="", timed_out=False, on_spawn=None):
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.on_spawn = on_spawn
        self.calls = []

    def __call__(self, cmd, timeout, cwd, env=None):
        self.calls.append({"cmd": list(cmd), "timeout": timeout, "cwd": cwd})
        if self.on_spawn is not None:
            self.on_spawn(self)
        return self.rc, self.stdout, self.stderr, self.timed_out

    @property
    def command(self):
        return self.calls[-1]["cmd"]

    @property
    def prompt(self):
        return self.command[-1]


CANNED_ID = "99999999-8888-7777-6666-555555555555"


def claude_json_output(session_id=CANNED_ID, recap=True):
    text = "Worked on the goal.\n"
    if recap:
        text = ("CONTINUATION RECAP: the previous session left off here; "
                "next I will finish it.\n" + text)
    return json.dumps({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "session_id": session_id,
    })


def old_claude_transcript(sandbox, session="11111111-2222-3333-4444-555555555555",
                          cwd="/tmp", project="project"):
    return sandbox.write_lines(
        sandbox.home_path(".claude", "projects", project, "%s.jsonl" % session),
        [{"type": "user", "cwd": cwd,
          "message": {"role": "user", "content": "old turn"}}])


def test_claude_code_full_auto_spawns_a_continuation(sandbox, monkeypatch):
    old_claude_transcript(sandbox)
    adapter = claude_adapter(sandbox, rotate_mode="full_auto")
    recorder = SpawnRecorder(stdout=claude_json_output())
    monkeypatch.setattr(adapter, "_spawn", recorder)

    result = adapter.rotate("11111111-2222-3333-4444-555555555555",
                            "/tmp/handover.md")

    assert result.mode == "full-auto"
    assert result.rotated is True
    assert result.new_session == CANNED_ID
    assert result.detail["recap_seen"] is True
    assert "/tmp/handover.md" in recorder.prompt
    assert recorder.command[:3] == ["claude", "-p", "--output-format"]
    assert recorder.command[3] == "json"
    # The default spawn args ride along, after the adapter's own flags.
    assert recorder.command[4:6] == ["--permission-mode", "acceptEdits"]
    assert recorder.prompt.startswith("You are the continuation of a previous session")
    assert result.error is None


def test_claude_code_uses_the_recorded_cwd(sandbox, monkeypatch):
    old_claude_transcript(sandbox, cwd="/tmp")
    adapter = claude_adapter(sandbox, rotate_mode="full_auto")
    recorder = SpawnRecorder(stdout=claude_json_output())
    monkeypatch.setattr(adapter, "_spawn", recorder)

    adapter.rotate("11111111-2222-3333-4444-555555555555", "/tmp/handover.md")
    assert recorder.calls[-1]["cwd"] == "/tmp"


def test_claude_code_cwd_scan_skips_records_without_cwd(sandbox, monkeypatch):
    """The first transcript record can be a queue-operation line with no cwd
    (observed live: the rotation then spawned in the neutral temp dir). The
    scan must keep looking and find the cwd on a later record."""
    sandbox.write_lines(
        sandbox.home_path(".claude", "projects", "project",
                          "11111111-2222-3333-4444-555555555555.jsonl"),
        [{"type": "queue-operation", "cwd": None},
         {"type": "attachment", "cwd": "/srv/example/real/project"}])
    adapter = claude_adapter(sandbox, rotate_mode="full_auto")
    recorder = SpawnRecorder(stdout=claude_json_output())
    monkeypatch.setattr(adapter, "_spawn", recorder)

    adapter.rotate("11111111-2222-3333-4444-555555555555", "/tmp/handover.md")
    assert recorder.calls[-1]["cwd"] == "/srv/example/real/project"


def test_claude_code_rotate_cwd_overrides_the_transcript(sandbox, monkeypatch):
    old_claude_transcript(sandbox, cwd="/tmp")
    adapter = claude_adapter(sandbox, rotate_mode="full_auto", rotate_cwd="/tmp/project")
    recorder = SpawnRecorder(stdout=claude_json_output())
    monkeypatch.setattr(adapter, "_spawn", recorder)

    adapter.rotate("11111111-2222-3333-4444-555555555555", "/tmp/handover.md")
    assert recorder.calls[-1]["cwd"] == "/tmp/project"


def test_claude_code_spawn_args_replace_the_default(sandbox, monkeypatch):
    old_claude_transcript(sandbox)
    adapter = claude_adapter(sandbox, rotate_mode="full_auto",
                             spawn_args=["--permission-mode", "bypassPermissions"])
    recorder = SpawnRecorder(stdout=claude_json_output())
    monkeypatch.setattr(adapter, "_spawn", recorder)

    adapter.rotate("11111111-2222-3333-4444-555555555555", "/tmp/handover.md")
    assert "acceptEdits" not in recorder.command
    assert "bypassPermissions" in recorder.command


def test_claude_code_continuation_prompt_file_is_used(sandbox, monkeypatch):
    old_claude_transcript(sandbox)
    template = os.path.join(str(sandbox.root), "prompt.txt")
    with open(template, "w", encoding="utf-8") as stream:
        stream.write("Resume from {handover} after {session}.")
    adapter = claude_adapter(sandbox, rotate_mode="full_auto",
                             continuation_prompt_file=template)
    recorder = SpawnRecorder(stdout=claude_json_output())
    monkeypatch.setattr(adapter, "_spawn", recorder)

    adapter.rotate("11111111-2222-3333-4444-555555555555", "/tmp/handover.md")
    assert recorder.prompt == (
        "Resume from /tmp/handover.md after 11111111-2222-3333-4444-555555555555.")


def test_claude_code_falls_back_to_the_newest_transcript(sandbox, monkeypatch):
    """A CLI that prints no session id still started a session."""
    old_claude_transcript(sandbox)
    adapter = claude_adapter(sandbox, rotate_mode="full_auto")
    new_id = "aaaaaaaa-1111-2222-3333-444444444444"

    def spawn(recorder):
        sandbox.write_lines(
            sandbox.home_path(".claude", "projects", "project", "%s.jsonl" % new_id),
            [{"type": "user", "message": {"role": "user", "content": "new"}}])

    recorder = SpawnRecorder(stdout="not json at all", on_spawn=spawn)
    monkeypatch.setattr(adapter, "_spawn", recorder)

    result = adapter.rotate("11111111-2222-3333-4444-555555555555", "/tmp/handover.md")
    assert result.rotated is True
    assert result.new_session == new_id
    assert result.detail["session_id_source"] == "transcript"
    assert result.detail["recap_seen"] is False


# -------------------------------------------------------------- codex spawn


def old_codex_rollout(sandbox, session="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                      cwd="/tmp/work"):
    name = "rollout-2026-10-16T10-00-00-%s.jsonl" % session
    path = sandbox.home_path(".codex", "sessions", "2026", "10", "16", name)
    return sandbox.write_lines(path, [
        {"type": "session_meta", "payload": {"id": session, "cwd": cwd}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 10, "output_tokens": 1}}}},
    ])


def new_codex_rollout(sandbox, session="dddddddd-1111-2222-3333-444444444444"):
    name = "rollout-2026-10-16T11-00-00-%s.jsonl" % session
    path = sandbox.home_path(".codex", "sessions", "2026", "10", "16", name)
    return sandbox.write_lines(path, [
        {"type": "session_meta", "payload": {"id": session, "cwd": "/tmp/work"}},
    ])


def test_codex_full_auto_spawns_a_continuation(sandbox, monkeypatch):
    old_codex_rollout(sandbox)
    adapter = codex_adapter(sandbox, rotate_mode="full_auto")
    new_id = "dddddddd-1111-2222-3333-444444444444"
    recorder = SpawnRecorder(
        stdout="CONTINUATION RECAP: picking up where it stopped.\n",
        on_spawn=lambda rec: new_codex_rollout(sandbox, new_id))
    monkeypatch.setattr(adapter, "_spawn", recorder)

    result = adapter.rotate("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "/tmp/handover.md")

    assert result.mode == "full-auto"
    assert result.rotated is True
    assert result.new_session == new_id
    assert result.detail["recap_seen"] is True
    # The prompt goes last, matching the adapter's one-shot convention.
    assert recorder.command[:4] == ["codex", "exec", "--skip-git-repo-check", "--sandbox"]
    assert recorder.command[4] == "workspace-write"
    assert "/tmp/handover.md" in recorder.prompt
    assert result.error is None


def test_codex_uses_the_rollout_cwd(sandbox, monkeypatch):
    old_codex_rollout(sandbox, cwd="/tmp/work")
    adapter = codex_adapter(sandbox, rotate_mode="full_auto")
    recorder = SpawnRecorder(stdout="", on_spawn=lambda rec: new_codex_rollout(sandbox))
    monkeypatch.setattr(adapter, "_spawn", recorder)

    adapter.rotate("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "/tmp/handover.md")
    assert recorder.calls[-1]["cwd"] == "/tmp/work"


def test_codex_ignores_a_rollout_that_was_already_there(sandbox, monkeypatch):
    """Only a file that appeared during the spawn can be the new session."""
    old_codex_rollout(sandbox)
    pre_existing = "cccccccc-0000-0000-0000-000000000000"
    new_codex_rollout(sandbox, pre_existing)
    adapter = codex_adapter(sandbox, rotate_mode="full_auto")
    recorder = SpawnRecorder(stdout="")  # writes nothing
    monkeypatch.setattr(adapter, "_spawn", recorder)

    result = adapter.rotate("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "/tmp/handover.md")
    assert result.rotated is True
    assert result.new_session is None
    assert result.detail["session_id_source"] == "unknown"


# --------------------------------------------------------------- edge paths


@pytest.mark.parametrize("factory", [claude_adapter, codex_adapter])
def test_dry_run_does_not_spawn(sandbox, monkeypatch, factory):
    adapter = factory(sandbox, rotate_mode="full_auto")

    def explode(*args, **kwargs):
        raise AssertionError("a dry run must not spawn anything")

    monkeypatch.setattr(adapter, "_spawn", explode)
    result = adapter.rotate("old-session", "/tmp/handover.md", dry_run=True)
    assert result.mode == "full-auto"
    assert result.rotated is False
    assert result.detail["would_start_new_session"] is True
    assert result.detail["handover"] == "/tmp/handover.md"


@pytest.mark.parametrize("factory", [claude_adapter, codex_adapter])
def test_a_missing_handover_is_refused(sandbox, monkeypatch, factory):
    adapter = factory(sandbox, rotate_mode="full_auto")

    def explode(*args, **kwargs):
        raise AssertionError("nothing can be spawned without a handover")

    monkeypatch.setattr(adapter, "_spawn", explode)
    result = adapter.rotate("old-session", "")
    assert result.mode == "full-auto"
    assert result.rotated is False
    assert "handover" in result.error.lower()


@pytest.mark.parametrize("factory", [claude_adapter, codex_adapter])
def test_a_failed_spawn_carries_the_stderr_tail(sandbox, monkeypatch, factory):
    adapter = factory(sandbox, rotate_mode="full_auto")
    recorder = SpawnRecorder(rc=1, stderr="the tool refused to start: reason X")
    monkeypatch.setattr(adapter, "_spawn", recorder)

    result = adapter.rotate("old-session", "/tmp/handover.md")
    assert result.rotated is False
    assert "reason X" in result.error
    assert "exited 1" in result.error


@pytest.mark.parametrize("factory", [claude_adapter, codex_adapter])
def test_capabilities_reflect_rotate_mode(sandbox, factory):
    assisted = factory(sandbox).capabilities()
    assert assisted["rotate"] is True
    assert assisted["full_auto"] is False
    assert assisted["history"] is True
    assert assisted["run_prompt"] is True

    full_auto = factory(sandbox, rotate_mode="full_auto").capabilities()
    assert full_auto["rotate"] is True
    assert full_auto["full_auto"] is True
    assert "full-auto" in full_auto["note"]


def test_claude_code_fallback_ignores_another_project(sandbox, monkeypatch):
    """Only the old session's own project can hold its replacement."""
    old_id = "11111111-2222-3333-4444-555555555555"
    old_claude_transcript(sandbox, session=old_id, project="mine")
    adapter = claude_adapter(sandbox, rotate_mode="full_auto")

    def spawn(recorder):
        sandbox.write_lines(
            sandbox.home_path(".claude", "projects", "theirs",
                              "bbbbbbbb-0000-0000-0000-000000000000.jsonl"),
            [{"type": "user", "message": {"role": "user", "content": "other"}}])

    recorder = SpawnRecorder(stdout="no json", on_spawn=spawn)
    monkeypatch.setattr(adapter, "_spawn", recorder)

    result = adapter.rotate(old_id, "/tmp/handover.md")
    assert result.new_session is None
    assert result.detail["session_id_source"] == "unknown"
