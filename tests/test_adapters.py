"""Adapters: what they read, and what they refuse to read.

These are the tests with the most to catch. Each adapter's occupancy rule is an
inference about another tool's on-disk format, documented in the adapter source
with the evidence that established it. If that tool changes its convention, the
symptom is a plausible-looking number that is wrong by a factor of two -- which
is exactly the failure that would otherwise reach a rotation decision unnoticed.

The fixtures are fabricated. That is deliberate: a fixture carrying a real
session would prove the parse works on that one file, and would put somebody's
transcript in a public repository to do it.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from custos.adapters import (
    ADAPTERS,
    SYNTHETIC_ADAPTERS,
    adapter_status,
    auto_adapter_names,
    get_adapter,
    list_adapters,
)
from custos.adapters.base import Adapter, SessionRef
from custos.adapters.claude_code import ClaudeCodeAdapter
from custos.adapters.codex import CodexAdapter
from custos.adapters.generic import GenericAdapter
from custos.adapters.opencode import OpenCodeAdapter
from custos.adapters.pi import PiAdapter
from custos.core.usage import AdapterNotInstalled, NoSessions, Reading, ReadFailed

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture_path(name: str) -> str:
    return os.path.join(FIXTURES, name)


def load_config(sandbox, adapters=None, **extra):
    from custos.core import config as config_module

    payload = {"version": 1}
    if adapters:
        payload["adapters"] = adapters
    payload.update(extra)
    sandbox.write_config(payload)
    return config_module.load_config(sandbox.config_dir)


def usage(input_tokens=0, cache_read=0, cache_creation=0, output=0):
    return {
        "input_tokens": input_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "output_tokens": output,
    }


def assistant(usage_block, ts="2026-10-15T10:00:00Z", model="synthetic-model",
              text="Synthetic turn.", sidechain=False):
    record = {
        "type": "assistant",
        "timestamp": ts,
        "message": {"role": "assistant", "model": model, "usage": usage_block,
                    "content": [{"type": "text", "text": text}]},
    }
    if sidechain:
        record["isSidechain"] = True
    return record


def user(text="Synthetic turn.", ts="2026-10-15T10:00:00Z"):
    return {"type": "user", "timestamp": ts,
            "message": {"role": "user", "content": text}}


# ============================================================ claude code


class TestClaudeCode:
    def adapter(self, sandbox, project="synthetic-project"):
        return ClaudeCodeAdapter(load_config(sandbox, {
            "claude_code": {"projects_dir": sandbox.home_path(".claude", "projects")}}))

    def test_occupancy_is_the_prompt_size_from_the_newest_usage_record(self, sandbox):
        sandbox.write_lines(
            sandbox.home_path(".claude", "projects", "p", "s.jsonl"),
            [assistant(usage(input_tokens=59151)),
             assistant(usage(input_tokens=59394, cache_read=59136))])
        adapter = self.adapter(sandbox)
        ref = adapter.sessions(limit=1)[0]
        reading = adapter.read_usage(ref)
        # Not input + cache_read: that sum is the doubling this adapter exists to
        # avoid, and the numbers here are the shape of the real cold start.
        assert reading.tokens == 59394
        assert reading.detail["input_tokens"] == 59394
        assert reading.detail["cache_read_input_tokens"] == 59136

    def test_the_larger_of_the_two_conventions_wins(self, sandbox):
        """A minority of records carry only the uncached remainder."""
        sandbox.write_lines(
            sandbox.home_path(".claude", "projects", "p", "s.jsonl"),
            [assistant(usage(input_tokens=258, cache_read=59136,
                             cache_creation=100))])
        reading = self.adapter(sandbox).read_usage(
            self.adapter(sandbox).sessions(limit=1)[0])
        assert reading.tokens == 59236

    def test_sidechain_records_are_ignored(self, sandbox):
        sandbox.write_lines(
            sandbox.home_path(".claude", "projects", "p", "s.jsonl"),
            [assistant(usage(input_tokens=1000)),
             assistant(usage(input_tokens=999999), sidechain=True)])
        reading = self.adapter(sandbox).read_usage(
            self.adapter(sandbox).sessions(limit=1)[0])
        assert reading.tokens == 1000

    def test_the_transcript_fixture_reads_as_expected(self, sandbox):
        path = os.path.join(sandbox.home_path(".claude", "projects", "p"),
                            "11111111-2222-3333-4444-555555555555.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(fixture_path("claude_session.jsonl"), encoding="utf-8") as src:
            with open(path, "w", encoding="utf-8") as dst:
                dst.write(src.read())
        adapter = self.adapter(sandbox)
        reading = adapter.read_usage(adapter.sessions(limit=1)[0])
        # The last record is the post-compaction turn, and the fixture also holds
        # a sub-agent record with a deliberately absurd count that must be skipped.
        assert reading.tokens == 420
        assert reading.compressed is True
        assert reading.detail["last_compaction"]["pre_tokens"] == 1500

    def test_compaction_is_reported_from_the_boundary_record(self, sandbox):
        sandbox.write_lines(
            sandbox.home_path(".claude", "projects", "p", "s.jsonl"),
            [assistant(usage(input_tokens=1000)),
             {"type": "system", "subtype": "compact_boundary",
              "compactMetadata": {"trigger": "auto", "preTokens": 900,
                                  "postTokens": 100}}])
        adapter = self.adapter(sandbox)
        reading = adapter.read_usage(adapter.sessions(limit=1)[0])
        assert reading.compressed is True

    def test_a_transcript_without_usage_is_a_typed_read_failure(self, sandbox):
        sandbox.write_lines(
            sandbox.home_path(".claude", "projects", "p", "s.jsonl"),
            [user(), {"type": "summary", "summary": "no usage here"}])
        adapter = self.adapter(sandbox)
        with pytest.raises(ReadFailed) as excinfo:
            adapter.read_usage(adapter.sessions(limit=1)[0])
        assert excinfo.value.tool == "claude_code"

    def test_no_transcripts_is_a_typed_no_sessions(self, sandbox):
        with pytest.raises(NoSessions):
            self.adapter(sandbox).sessions()

    def test_a_missing_store_is_not_detected(self, denv):
        adapter = ClaudeCodeAdapter(load_config(denv, {
            "claude_code": {"projects_dir": denv.home_path(".claude", "projects")}}))
        assert adapter.detect() is False

    def test_sessions_are_newest_first(self, sandbox):
        first = sandbox.home_path(".claude", "projects", "p", "old.jsonl")
        second = sandbox.home_path(".claude", "projects", "p", "new.jsonl")
        for path in (first, second):
            sandbox.write_lines(path, [assistant(usage(input_tokens=10))])
        os.utime(first, (1000.0, 1000.0))
        os.utime(second, (2000.0, 2000.0))
        adapter = self.adapter(sandbox)
        assert [ref.session for ref in adapter.sessions(limit=2)] == ["new", "old"]

    def test_ref_for_resolves_an_exact_filename(self, sandbox):
        path = sandbox.home_path(".claude", "projects", "p",
                                 "35b493db-e1d5-40d9-a849-92df27393dad.jsonl")
        sandbox.write_lines(path, [assistant(usage(input_tokens=10))])
        adapter = self.adapter(sandbox)
        ref = adapter.ref_for("35b493db-e1d5-40d9-a849-92df27393dad")
        assert ref.path == path

    def test_ref_for_refuses_an_id_that_would_escape_the_store(self, sandbox):
        sandbox.write_lines(
            sandbox.home_path(".claude", "projects", "p", "s.jsonl"),
            [assistant(usage(input_tokens=10))])
        adapter = self.adapter(sandbox)
        for hostile in ("../../etc/passwd", "..", ".", "", "sub/dir",
                        "*", "s*", "s?", "[a-z]"):
            ref = adapter.ref_for(hostile)
            assert ref.path == "", hostile
            assert os.sep not in ref.session or ref.session == ""

    def test_recent_messages_reads_the_tail_oldest_first(self, sandbox):
        sandbox.write_lines(
            sandbox.home_path(".claude", "projects", "p", "s.jsonl"),
            [user("first synthetic turn"), assistant(usage(input_tokens=10),
                                                    text="first reply"),
             user("second synthetic turn"), assistant(usage(input_tokens=11),
                                                      text="second reply")])
        adapter = self.adapter(sandbox)
        messages = adapter.recent_messages(adapter.sessions(limit=1)[0], limit=10)
        assert messages[0].startswith("[user] first synthetic turn")
        assert messages[-1].startswith("[assistant] second reply")

    def test_recent_messages_of_an_unresolvable_session_is_empty(self, sandbox):
        adapter = self.adapter(sandbox)
        assert adapter.recent_messages(adapter.ref_for("absent")) == []

    def test_capabilities_declare_a_one_shot_command(self, sandbox):
        caps = self.adapter(sandbox).capabilities()
        assert caps["run_prompt"] is True
        assert caps["history"] is True
        assert caps["full_auto"] is False
        assert self.adapter(sandbox).one_shot_command() == ["claude", "-p"]


# ================================================================ codex


def token_count(input_tokens, window=258400, total_input=1000000, cached=0):
    return {
        "timestamp": "2026-10-15T10:00:00Z",
        "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": total_input,
                                  "cached_input_tokens": cached,
                                  "output_tokens": 100,
                                  "total_tokens": total_input + 100},
            "last_token_usage": {"input_tokens": input_tokens,
                                 "cached_input_tokens": cached,
                                 "output_tokens": 100,
                                 "total_tokens": input_tokens + 100},
            "model_context_window": window,
        }},
    }


class TestCodex:
    def adapter(self, sandbox):
        return CodexAdapter(load_config(sandbox, {
            "codex": {"sessions_dir": sandbox.home_path(".codex", "sessions"),
                      "archived_dir": sandbox.home_path(".codex", "archived_sessions")}}))

    def rollout(self, sandbox, records, session="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"):
        return sandbox.codex_rollout(session=session, records=records)

    def test_occupancy_is_the_last_call_not_the_cumulative_total(self, sandbox):
        self.rollout(sandbox, [token_count(30000, total_input=30000),
                               token_count(144167, total_input=37494149)])
        adapter = self.adapter(sandbox)
        reading = adapter.read_usage(adapter.sessions(limit=1)[0])
        assert reading.tokens == 144167
        # The cumulative counter is carried through for diagnostics only; it sums
        # every call the session ever made, so using it as occupancy would put a
        # 258k window at 14,500% full.
        assert reading.detail["cumulative_total_tokens"] == 37494149 + 100
        assert reading.window == 258400
        assert reading.detail["window_source"] == "tool"

    def test_the_window_falls_back_to_configuration_when_the_tool_omits_it(self, sandbox):
        record = token_count(1000)
        del record["payload"]["info"]["model_context_window"]
        self.rollout(sandbox, [record])
        adapter = self.adapter(sandbox)
        adapter.config.data["adapters"]["codex"]["window"] = 111111
        reading = adapter.read_usage(adapter.sessions(limit=1)[0])
        assert reading.window == 111111
        assert reading.detail["window_source"] == "config"

    def test_the_rollout_fixture_reads_as_expected(self, sandbox):
        session = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        path = sandbox.home_path(
            ".codex", "sessions", "2026", "10", "16",
            "rollout-2026-10-16T10-00-00-%s.jsonl" % session)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(fixture_path("codex_rollout.jsonl"), encoding="utf-8") as src:
            with open(path, "w", encoding="utf-8") as dst:
                dst.write(src.read())
        adapter = self.adapter(sandbox)
        reading = adapter.read_usage(adapter.sessions(limit=1)[0])
        assert reading.tokens == 144167
        assert reading.compressed is True

    def test_a_rollout_without_usage_is_a_typed_read_failure(self, sandbox):
        self.rollout(sandbox, [{"type": "session_meta", "payload": {}}])
        adapter = self.adapter(sandbox)
        with pytest.raises(ReadFailed) as excinfo:
            adapter.read_usage(adapter.sessions(limit=1)[0])
        assert "no token_count record" in str(excinfo.value)

    def test_no_rollouts_is_a_typed_no_sessions(self, sandbox):
        with pytest.raises(NoSessions):
            self.adapter(sandbox).sessions()

    def test_the_session_id_is_the_uuid_suffix_of_the_filename(self, sandbox):
        self.rollout(sandbox, [token_count(1)])
        adapter = self.adapter(sandbox)
        assert adapter.sessions(limit=1)[0].session == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_ref_for_resolves_through_the_date_directories(self, sandbox):
        session = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        path = self.rollout(sandbox, [token_count(1)], session=session)
        adapter = self.adapter(sandbox)
        assert adapter.ref_for(session).path == path

    def test_ref_for_refuses_a_traversal_id(self, sandbox):
        self.rollout(sandbox, [token_count(1)])
        adapter = self.adapter(sandbox)
        assert adapter.ref_for("../../../etc/passwd").path == ""
        assert adapter.ref_for("").path == ""

    def test_ref_for_matches_a_filename_suffix_not_a_substring(self, sandbox):
        """A leading or middle fragment must not resolve.

        The candidate test is ``endswith("<id>.jsonl")`` rather than "the name
        contains the id", and the distinction is visible here: the timestamp
        carries digits that a substring match could find, and a fragment taken
        from the middle of the uuid is not a suffix. A *trailing* fragment does
        still resolve, because the id is the trailing uuid by definition; the
        adapter only ever hands out full ids, so that is a tolerance rather than
        a wrong answer.
        """
        self.rollout(sandbox, [token_count(1)],
                     session="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        adapter = self.adapter(sandbox)
        for fragment in ("rollout-2026-10-16T10-00-00", "2026-10-16T10-00-00",
                         "aaaaaaaa", "bbbb-cccc-dddd",
                         "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"):
            assert adapter.ref_for(fragment).path == "", fragment
        # The full id, and only that, resolves.
        assert adapter.ref_for("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee").path != ""

    def test_ref_for_ignores_a_store_at_another_depth(self, sandbox):
        """The layout this adapter was verified against is the one it walks."""
        deep = sandbox.home_path(".codex", "sessions", "2026", "10", "16", "extra",
                                 "rollout-2026-10-16T10-00-00-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl")
        sandbox.write_lines(deep, [token_count(1)])
        adapter = self.adapter(sandbox)
        assert adapter.ref_for("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee").path == ""
        # A scan still finds it, so the session is readable even though the
        # handover cannot attach history to it.
        assert adapter.sessions(limit=1)[0].path == deep

    def test_recent_messages_reads_message_records(self, sandbox):
        self.rollout(sandbox, [
            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                                  "content": [{"type": "input_text",
                                                               "text": "a synthetic question"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                  "content": [{"type": "output_text",
                                                               "text": "a synthetic answer"}]}},
        ])
        adapter = self.adapter(sandbox)
        messages = adapter.recent_messages(adapter.sessions(limit=1)[0])
        assert messages == ["[user] a synthetic question", "[assistant] a synthetic answer"]

    def test_capabilities_declare_a_one_shot_command(self, sandbox):
        assert self.adapter(sandbox).one_shot_command() == [
            "codex", "exec", "--skip-git-repo-check"]


# ============================================================= opencode


def opencode_session(session_id="ses_synthetic_1", updated=1770000000000,
                     parent=None, compacting=None, cumulative=2853144):
    return {
        "id": session_id, "parent_id": parent,
        "time_created": updated - 1000, "time_updated": updated,
        "time_compacting": compacting,
        "tokens_input": cumulative, "tokens_output": 100, "tokens_cache_read": 200,
        "tokens_cache_write": 0, "directory": "/synthetic/project",
        "title": "Synthetic session", "model": "synthetic-model", "cost": 0.0,
    }


def opencode_message(session_id, input_tokens=2062, cache_read=331520,
                     message_id=None, role="assistant",
                     created=1770000000000, total=None):
    # Defaults to a per-session, per-timestamp id: `message.id` is a primary key,
    # so a fixed default would collide the moment a test builds two messages.
    if message_id is None:
        message_id = "msg_%s_%d" % (session_id, created)
    tokens = {
        "input": input_tokens,
        "output": 664,
        "reasoning": 65,
        "cache": {"read": cache_read, "write": 0},
        "total": total if total is not None else input_tokens + cache_read + 664 + 65,
    }
    return {"id": message_id, "session_id": session_id, "time_created": created,
            "data": json.dumps({"id": message_id, "role": role, "tokens": tokens})}


class TestOpenCode:
    def adapter(self, sandbox):
        return OpenCodeAdapter(load_config(sandbox, {
            "opencode": {
                "db_path": sandbox.home_path(".local", "share", "opencode", "opencode.db"),
                "storage_dir": sandbox.home_path(".local", "share", "opencode", "storage")}}))

    def test_occupancy_is_input_plus_cache_read(self, sandbox):
        sandbox.opencode_db(sessions=[opencode_session()],
                            messages=[opencode_message("ses_synthetic_1")])
        adapter = self.adapter(sandbox)
        reading = adapter.read_usage(adapter.sessions(limit=1)[0])
        # 2062 + 331520, not the message total: the cached prefix was still sent.
        assert reading.tokens == 333582
        assert reading.detail["message_total"] == 334311
        assert reading.detail["session_cumulative_input"] == 2853144

    def test_the_database_is_opened_read_only(self, sandbox):
        path = sandbox.opencode_db(sessions=[opencode_session()],
                                   messages=[opencode_message("ses_synthetic_1")])
        before = open(path, "rb").read()
        adapter = self.adapter(sandbox)
        adapter.read_usage(adapter.sessions(limit=1)[0])
        assert open(path, "rb").read() == before

    def test_sub_agent_sessions_are_skipped(self, sandbox):
        sandbox.opencode_db(
            sessions=[opencode_session("ses_parent"),
                      opencode_session("ses_child", parent="ses_parent")],
            messages=[opencode_message("ses_parent"), opencode_message("ses_child")])
        adapter = self.adapter(sandbox)
        assert [ref.session for ref in adapter.sessions(limit=10)] == ["ses_parent"]

    def test_the_newest_assistant_message_wins(self, sandbox):
        sandbox.opencode_db(
            sessions=[opencode_session()],
            messages=[opencode_message("ses_synthetic_1", input_tokens=10,
                                       cache_read=0, message_id="old",
                                       created=1770000000000),
                      opencode_message("ses_synthetic_1", input_tokens=5000,
                                       cache_read=100, message_id="new",
                                       created=1770000009999)])
        adapter = self.adapter(sandbox)
        assert adapter.read_usage(adapter.sessions(limit=1)[0]).tokens == 5100

    def test_a_session_without_messages_is_a_typed_read_failure(self, sandbox):
        sandbox.opencode_db(sessions=[opencode_session()], messages=[])
        adapter = self.adapter(sandbox)
        with pytest.raises(ReadFailed) as excinfo:
            adapter.read_usage(adapter.sessions(limit=1)[0])
        assert "no assistant message" in str(excinfo.value)

    def test_an_empty_database_is_a_typed_no_sessions(self, sandbox):
        sandbox.opencode_db()
        with pytest.raises(NoSessions):
            self.adapter(sandbox).sessions()

    def test_a_missing_database_is_a_typed_read_failure(self, sandbox):
        adapter = self.adapter(sandbox)
        os.remove(sandbox.home_path(".local", "share", "opencode", "opencode.db")) \
            if os.path.exists(sandbox.home_path(".local", "share", "opencode", "opencode.db")) else None
        with pytest.raises(ReadFailed) as excinfo:
            adapter.sessions()
        assert "database not found" in str(excinfo.value)

    def test_busy_is_true_while_a_compaction_runs(self, sandbox):
        sandbox.opencode_db(
            sessions=[opencode_session(compacting=1770000000000)],
            messages=[opencode_message("ses_synthetic_1")])
        adapter = self.adapter(sandbox)
        assert adapter.busy(adapter.sessions(limit=1)[0]) is True

    def test_busy_is_false_when_the_session_row_says_nothing_is_running(self, sandbox):
        sandbox.opencode_db(sessions=[opencode_session()],
                            messages=[opencode_message("ses_synthetic_1")])
        adapter = self.adapter(sandbox)
        assert adapter.busy(adapter.sessions(limit=1)[0]) is False

    def test_busy_is_unknown_for_a_session_that_is_not_there(self, sandbox):
        sandbox.opencode_db(sessions=[opencode_session()],
                            messages=[opencode_message("ses_synthetic_1")])
        adapter = self.adapter(sandbox)
        assert adapter.busy(SessionRef(session="ses_absent", path="")) is None

    def test_recent_messages_read_text_parts(self, sandbox):
        sandbox.opencode_db(
            sessions=[opencode_session()],
            messages=[opencode_message("ses_synthetic_1")],
            parts=[{"id": "part_1", "session_id": "ses_synthetic_1",
                    "time_created": 1770000000000,
                    "data": json.dumps({"type": "text", "text": "a synthetic part"})}])
        adapter = self.adapter(sandbox)
        assert adapter.recent_messages(adapter.sessions(limit=1)[0]) == [
            "[text] a synthetic part"]

    def test_ref_for_confirms_a_row_and_rejects_a_sub_agent(self, sandbox):
        sandbox.opencode_db(
            sessions=[opencode_session("ses_parent"),
                      opencode_session("ses_child", parent="ses_parent")],
            messages=[opencode_message("ses_parent")])
        adapter = self.adapter(sandbox)
        assert adapter.ref_for("ses_parent").path.endswith("opencode.db")
        assert adapter.ref_for("ses_child").path == ""
        assert adapter.ref_for("ses_absent").path == ""

    def test_an_older_install_without_a_database_says_so(self, sandbox):
        os.makedirs(sandbox.home_path(".local", "share", "opencode", "storage"),
                    exist_ok=True)
        adapter = self.adapter(sandbox)
        assert adapter.detect() is True
        with pytest.raises(ReadFailed) as excinfo:
            adapter.sessions()
        assert "legacy storage directory" in str(excinfo.value)

    def test_a_wrong_schema_is_reported_rather_than_crashing(self, sandbox):
        import sqlite3

        path = sandbox.home_path(".local", "share", "opencode", "opencode.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        connection = sqlite3.connect(path)
        connection.executescript("CREATE TABLE unrelated (id TEXT);")
        connection.commit()
        connection.close()
        with pytest.raises(ReadFailed) as excinfo:
            self.adapter(sandbox).sessions()
        assert "no `session` table" in str(excinfo.value)

    def test_capabilities_declare_a_one_shot_command(self, sandbox):
        adapter = self.adapter(sandbox)
        assert adapter.one_shot_command() == ["opencode", "run"]
        assert adapter.capabilities()["busy_detection"] == "compaction only"


# ==================================================================== pi


class TestPi:
    def adapter(self, sandbox):
        return PiAdapter(load_config(sandbox, {
            "pi": {"sessions_dir": sandbox.home_path(".pi", "agent", "sessions")}}))

    def test_a_usage_shaped_record_is_read(self, sandbox):
        sandbox.pi_session(records=[
            {"type": "message", "timestamp": "2026-10-15T10:00:00Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}],
                         "usage": {"inputTokens": 1234, "cacheReadTokens": 500,
                                   "outputTokens": 10, "contextWindow": 190000}}}])
        adapter = self.adapter(sandbox)
        reading = adapter.read_usage(adapter.sessions(limit=1)[0])
        assert reading.tokens >= 1234
        assert reading.window == 190000

    def test_an_unrecognised_layout_fails_loudly_rather_than_inventing_a_number(self, sandbox):
        sandbox.pi_session(records=[{"type": "message", "message": {"role": "assistant"}}])
        adapter = self.adapter(sandbox)
        with pytest.raises(ReadFailed) as excinfo:
            adapter.read_usage(adapter.sessions(limit=1)[0])
        assert "no usage entry recognised" in str(excinfo.value)

    def test_a_missing_store_is_not_detected(self, denv):
        adapter = PiAdapter(load_config(denv, {
            "pi": {"sessions_dir": denv.home_path(".pi", "agent", "sessions")}}))
        assert adapter.detect() is False

    def test_verification_is_documentation_only(self, sandbox):
        from custos.adapters.base import DOC_VERIFIED

        assert PiAdapter.VERIFICATION == DOC_VERIFIED
        assert self.adapter(sandbox).one_shot_command() == ["pi", "--print"]


# =============================================================== generic


class TestGeneric:
    def adapter(self, sandbox, sessions, **extra):
        section = {"enabled": True, "sessions": sessions}
        section.update(extra)
        return GenericAdapter(load_config(sandbox, {"generic": section}))

    def test_a_file_source_is_read(self, sandbox):
        usage_path = sandbox.path("external", "usage.json")
        os.makedirs(os.path.dirname(usage_path), exist_ok=True)
        with open(usage_path, "w", encoding="utf-8") as stream:
            json.dump({"tokens": 41230, "window": 128000}, stream)
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "source": {"type": "file", "path": usage_path}}])
        reading = adapter.read_usage(adapter.sessions(limit=1)[0])
        assert reading.tokens == 41230
        assert reading.window == 128000
        assert reading.source == "external"

    def test_a_file_source_accepts_alternate_key_spellings(self, sandbox):
        usage_path = sandbox.path("external", "usage.json")
        os.makedirs(os.path.dirname(usage_path), exist_ok=True)
        with open(usage_path, "w", encoding="utf-8") as stream:
            json.dump({"prompt_tokens": 900, "context_window": 8000}, stream)
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "source": {"type": "file", "path": usage_path}}])
        reading = adapter.read_usage(adapter.sessions(limit=1)[0])
        assert reading.tokens == 900
        assert reading.window == 8000

    def test_a_config_entry_window_is_used_when_the_source_omits_one(self, sandbox):
        usage_path = sandbox.path("external", "usage.json")
        os.makedirs(os.path.dirname(usage_path), exist_ok=True)
        with open(usage_path, "w", encoding="utf-8") as stream:
            json.dump({"tokens": 900}, stream)
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "window": 64000,
             "source": {"type": "file", "path": usage_path}}])
        assert adapter.read_usage(adapter.sessions(limit=1)[0]).window == 64000

    def test_a_command_source_runs_with_the_agent_markers_stripped(self, sandbox):
        """A child that inherits ``CLAUDE_CODE_*`` can attach to the live session.

        The script reports what it saw to a file rather than to stdout, so the
        assertion can name both halves of the rule: the agent marker is gone,
        and an unrelated variable that happens to be set is still there. An
        empty environment would satisfy the first half and break the second.
        """
        observation = sandbox.path("observed.json")
        script = sandbox.path("emit.py")
        with open(script, "w", encoding="utf-8") as stream:
            stream.write(
                "import json, os\n"
                "with open(%r, 'w') as out:\n"
                "    json.dump({'marker': 'CLAUDE_CODE_MARKER' in os.environ,\n"
                "               'survivor': 'SYNTHETIC_SURVIVOR_ENV' in os.environ}, out)\n"
                "json.dump({'tokens': 7}, __import__('sys').stdout)\n" % observation)
        adapter = self.adapter(sandbox, [
            {"id": "cmd-1", "source": {"type": "command",
                                       "cmd": ["python3", script]}}])
        os.environ["CLAUDE_CODE_MARKER"] = "1"
        os.environ["SYNTHETIC_SURVIVOR_ENV"] = "1"
        try:
            reading = adapter.read_usage(adapter.sessions(limit=1)[0])
        finally:
            del os.environ["CLAUDE_CODE_MARKER"]
            del os.environ["SYNTHETIC_SURVIVOR_ENV"]
        assert reading.tokens == 7
        with open(observation, encoding="utf-8") as stream:
            assert json.load(stream) == {"marker": False, "survivor": True}

    def test_a_command_source_that_fails_is_a_typed_read_failure(self, sandbox):
        adapter = self.adapter(sandbox, [
            {"id": "cmd-1", "source": {"type": "command",
                                       "cmd": ["/bin/sh", "-c", "exit 3"]}}])
        with pytest.raises(ReadFailed) as excinfo:
            adapter.read_usage(adapter.sessions(limit=1)[0])
        assert "exited 3" in str(excinfo.value)

    def test_a_source_without_a_token_count_is_a_typed_read_failure(self, sandbox):
        usage_path = sandbox.path("external", "usage.json")
        os.makedirs(os.path.dirname(usage_path), exist_ok=True)
        with open(usage_path, "w", encoding="utf-8") as stream:
            json.dump({"note": "nothing useful"}, stream)
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "source": {"type": "file", "path": usage_path}}])
        with pytest.raises(ReadFailed) as excinfo:
            adapter.read_usage(adapter.sessions(limit=1)[0])
        assert "no token count" in str(excinfo.value)

    def test_a_missing_file_is_a_typed_read_failure(self, sandbox):
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "source": {"type": "file",
                                       "path": sandbox.path("absent.json")}}])
        with pytest.raises(ReadFailed) as excinfo:
            adapter.read_usage(adapter.sessions(limit=1)[0])
        assert "no file matched" in str(excinfo.value)

    def test_no_entries_configured_is_not_detected(self, sandbox):
        adapter = self.adapter(sandbox, [])
        assert adapter.detect() is False
        with pytest.raises(NoSessions):
            adapter.sessions()

    def test_a_configured_prompt_command_makes_a_one_shot_possible(self, sandbox):
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "source": {"type": "command", "cmd": ["/bin/echo", "{}"]}}],
            prompt_cmd=["/bin/echo", "{prompt}"])
        assert adapter.capabilities()["run_prompt"] is True
        assert adapter.one_shot_command() == ["/bin/echo", "{prompt}"]

    def test_without_a_prompt_command_the_gate_is_reported_as_unavailable(self, sandbox):
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "source": {"type": "command", "cmd": ["/bin/echo", "{}"]}}])
        assert adapter.capabilities()["run_prompt"] is False
        assert adapter.one_shot_command() == []

    def test_run_prompt_substitutes_the_placeholder(self, sandbox):
        script = sandbox.path("echo_prompt.py")
        with open(script, "w", encoding="utf-8") as stream:
            stream.write("import sys\nprint(sys.argv[1])\n")
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "source": {"type": "command", "cmd": ["/bin/echo", "{}"]}}],
            prompt_cmd=["python3", script, "{prompt}"])
        outcome = adapter.run_prompt("a synthetic question", timeout=30)
        assert outcome.text == "a synthetic question"

    def test_a_rotation_command_reports_a_new_session(self, sandbox):
        script = sandbox.path("rotate.py")
        with open(script, "w", encoding="utf-8") as stream:
            stream.write("import os\n"
                         "print('new-session-from-' + os.environ['CUSTOS_SESSION'])\n")
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "source": {"type": "command", "cmd": ["/bin/echo", "{}"]},
             "rotate_cmd": ["python3", script]}])
        # The session id is the entry id: that is what `sessions()` hands out, and
        # it is what `rotate` uses to find the entry's command.
        result = adapter.rotate("ext-1", "/tmp/handover.md")
        assert result.mode == "full-auto"
        assert result.rotated is True
        assert result.new_session == "new-session-from-ext-1"
        assert result.detail["rc"] == 0

    def test_the_rotation_command_receives_the_handover_path(self, sandbox):
        script = sandbox.path("peek.py")
        with open(script, "w", encoding="utf-8") as stream:
            stream.write("import os\n"
                         "print(os.environ['CUSTOS_HANDOVER'])\n")
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "source": {"type": "command", "cmd": ["/bin/echo", "{}"]},
             "rotate_cmd": ["python3", script]}])
        result = adapter.rotate("ext-1", sandbox.path("handovers", "one.md"))
        assert result.new_session == sandbox.path("handovers", "one.md")

    def test_a_dry_run_reports_the_command_without_running_it(self, sandbox):
        script = sandbox.path("rotate.py")
        with open(script, "w", encoding="utf-8") as stream:
            stream.write("print('should never run')\n")
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "source": {"type": "command", "cmd": ["/bin/echo", "{}"]},
             "rotate_cmd": ["python3", script]}])
        result = adapter.rotate("ext-1", "/tmp/handover.md", dry_run=True)
        assert result.mode == "full-auto"
        assert result.rotated is False
        assert result.new_session is None
        assert result.detail["would_run"] == ["python3", script]

    def test_a_failing_rotation_command_is_reported(self, sandbox):
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "source": {"type": "command", "cmd": ["/bin/echo", "{}"]},
             "rotate_cmd": ["/bin/sh", "-c", "echo broken >&2; exit 2"]}])
        result = adapter.rotate("ext-1", "/tmp/handover.md")
        assert result.rotated is False
        assert "broken" in (result.error or "")
        assert result.detail["rc"] == 2

    def test_a_rotation_command_of_the_wrong_shape_is_refused(self, sandbox):
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "source": {"type": "command", "cmd": ["/bin/echo", "{}"]},
             "rotate_cmd": {"not": "a command"}}])
        result = adapter.rotate("ext-1", "/tmp/handover.md")
        assert result.rotated is False
        assert "string or a list" in (result.error or "")

    def test_an_unknown_session_falls_back_to_assisted(self, sandbox):
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "source": {"type": "command", "cmd": ["/bin/echo", "{}"]},
             "rotate_cmd": ["/bin/echo", "never runs"]}])
        result = adapter.rotate("not-an-entry", "/tmp/handover.md")
        assert result.mode == "assisted"
        assert result.rotated is False

    def test_without_a_rotation_command_it_falls_back_to_assisted(self, sandbox):
        adapter = self.adapter(sandbox, [
            {"id": "ext-1", "source": {"type": "command", "cmd": ["/bin/echo", "{}"]}}])
        result = adapter.rotate("ext-1", "/tmp/handover.md")
        assert result.mode == "assisted"
        assert result.rotated is False


# =============================================================== registry


def test_every_adapter_is_registered_and_constructible():
    for name in list_adapters():
        assert name in ADAPTERS
        assert isinstance(get_adapter(name), Adapter)


def test_an_unknown_adapter_name_raises_key_error_with_the_alternatives():
    with pytest.raises(KeyError) as excinfo:
        get_adapter("nonexistent-tool")
    assert "claude_code" in str(excinfo.value)


def test_adapter_names_accept_the_dashed_spelling():
    assert isinstance(get_adapter("claude-code"), ClaudeCodeAdapter)


def test_the_demo_adapter_is_excluded_from_automatic_selection(sandbox):
    assert "demo" in SYNTHETIC_ADAPTERS
    assert "demo" not in auto_adapter_names(load_config(sandbox))


def test_auto_selection_honours_the_enabled_switch(sandbox):
    config = load_config(sandbox, {"claude_code": {"enabled": False},
                                   "codex": {"enabled": False},
                                   "opencode": {"enabled": False},
                                   "pi": {"enabled": False}})
    assert auto_adapter_names(config) == []


def test_adapter_status_reports_detection_and_verification(sandbox):
    rows = {row["name"]: row for row in adapter_status(load_config(sandbox))}
    assert rows["demo"]["verification"] == "doc-verified"
    assert rows["claude_code"]["detected"] is True
    assert rows["claude_code"]["verification"] == "verified"
    assert rows["pi"]["verification"] == "doc-verified"
    assert rows["generic"]["verification"] == "experimental"


def test_a_broken_store_does_not_stop_the_status_report(sandbox):
    path = sandbox.home_path(".local", "share", "opencode", "opencode.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("this is not a database")
    rows = {row["name"]: row for row in adapter_status(load_config(sandbox))}
    assert rows["opencode"]["detected"] is True


def test_the_base_ref_for_never_raises(tmp_path):
    class Minimal(Adapter):
        name = "minimal"

        def detect(self):
            return True

        def sessions(self, limit=20):
            raise NoSessions(self.name, "nothing here")

        def read_usage(self, session):
            raise ReadFailed(self.name, "nothing here")

    ref = Minimal().ref_for("anything")
    assert ref.path == ""
    assert ref.session == "anything"


def test_every_adapter_declares_its_verification_level():
    from custos.adapters import VERIFICATION_LEVELS

    for name in list_adapters():
        assert get_adapter(name).VERIFICATION in VERIFICATION_LEVELS
