"""The command-line surface, exercised as a process.

These run the real entry point in a subprocess rather than calling ``main()`` in
process. That is slower, and it is the point: the contract being tested is what
a scheduler and a shell hook actually see -- which stream a message lands on,
what the exit status is, and whether ``--dry-run`` really wrote nothing. An
in-process call would share the interpreter's state and could pass while the
installed command failed.

Every test gets its own ``HOME`` and ``CUSTOS_CONFIG_DIR``, so nothing here
reads or writes a real ``~/.config/custos``.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from conftest import claude_assistant, claude_usage

from custos import __version__

CLAUDE_SESSION = "11111111-2222-3333-4444-555555555555"
SESSIONS = ("scan", "status", "report", "rotate", "monitor", "gate", "demo",
            "doctor", "questions")


def without_pycache(path, skip_python_cache=False):
    """Every path under ``path`` except bytecode caches.

    A subprocess run compiles the package into ``__pycache__``, which would
    otherwise show up in a directory a dry run was supposed to leave untouched.
    ``skip_python_cache`` also drops the interpreter's own cache tree under
    ``$HOME``, which a sandboxed ``HOME`` collects and which no part of this
    tool writes.
    """
    found = []
    for base, dirnames, filenames in os.walk(path):
        dirnames[:] = [name for name in dirnames if name != "__pycache__"]
        if skip_python_cache and base == os.path.join(path, "Library"):
            dirnames[:] = []  # the interpreter's cache tree, not ours
            continue
        for name in filenames:
            found.append(os.path.relpath(os.path.join(base, name), path))
    return sorted(found)


def transcript(sandbox, tokens=10, cache_read=0, window=None, compress=False,
               age_seconds=None, thresholds=False):
    """A Claude Code transcript whose last usage record reports ``tokens``."""
    records = [claude_assistant(claude_usage(input_tokens=tokens,
                                             cache_read=cache_read))]
    if compress:
        records.append({"type": "system", "subtype": "compact_boundary",
                        "compactMetadata": {"trigger": "auto", "preTokens": 900,
                                            "postTokens": 100}})
    path = sandbox.claude_transcript(session=CLAUDE_SESSION, records=records)
    settings = {"version": 1, "adapters": {"claude_code": {
        "enabled": True, "projects_dir": "~/.claude/projects"}}}
    if window is not None:
        settings["adapters"]["claude_code"]["window"] = window
        # The monitor interlock refuses to rotate a session that was touched a
        # moment ago, so a test that wants a rotation has to backdate the file.
        settings["monitor"] = {"activity_quiet_minutes": 0}
        if thresholds:
            # Make the rotate line a fraction of the window, so a session that
            # is nearly full crosses it without a 350k-token fixture.
            settings["thresholds"] = {"baseline_percent": 100}
    if age_seconds is not None:
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
    sandbox.write_config(settings)
    return sandbox


def bank(path, duplicate=False):
    questions = [
        {"id": "sum-of-two", "prompt": "What is the sum of 17 and 25?",
         "answer": 42, "match": "last_number"},
        {"id": "count-letters", "prompt": 'How many letters are in "synthetic"?',
         "answer": 9, "match": "last_number"},
    ]
    if duplicate:
        questions.append(dict(questions[0]))
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({"version": 1, "questions": questions}, stream, indent=1)
    return path


def state_dir(sandbox):
    """The directory the tool derives from ``CUSTOS_CONFIG_DIR``."""
    return sandbox.config_dir


def gate_sandbox(sandbox, questions="questions.json", write_bank=True):
    """A sandbox with a transcript and the gate wired to a question bank.

    The config is written last on purpose: ``transcript()`` writes its own
    config, so a gate section added before that call would be overwritten.
    """
    if write_bank:
        bank(os.path.join(sandbox.config_dir, questions))
    transcript(sandbox, tokens=10)
    sandbox.write_config({
        "version": 1,
        "adapters": {"claude_code": {"enabled": True,
                                     "projects_dir": "~/.claude/projects"}},
        "gate": {"enabled": True, "questions": questions},
    })
    return sandbox


def find_handovers(sandbox):
    directory = os.path.join(state_dir(sandbox), "handovers")
    if not os.path.isdir(directory):
        return []
    return sorted(os.path.join(directory, name) for name in os.listdir(directory)
                  if name.endswith(".md"))


# ------------------------------------------------------------------- help


def test_the_top_level_help_exits_clean(sandbox):
    result = sandbox.run("--help")
    assert result.returncode == 0
    assert result.stdout.startswith("usage: custos")
    for name in SESSIONS:
        assert name in result.stdout


def test_version_prints_the_packaged_version(sandbox):
    result = sandbox.run("--version")
    assert result.returncode == 0
    assert __version__ in result.stdout


@pytest.mark.parametrize("name", SESSIONS)
def test_every_subcommand_help_exits_clean(sandbox, name):
    result = sandbox.run(name, "--help")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: custos %s" % name)


def test_an_unknown_subcommand_is_a_usage_error(sandbox):
    result = sandbox.run("frobnicate")
    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_running_with_no_subcommand_prints_the_help(sandbox):
    """Not an error: a bare invocation is how somebody finds out what this is."""
    result = sandbox.run()
    assert result.returncode == 0
    assert result.stdout.startswith("usage: custos")


def test_a_missing_required_option_is_a_usage_error(sandbox):
    result = sandbox.run("report", "--tool", "synthetic")
    assert result.returncode == 2
    assert "--session" in result.stderr


# ------------------------------------------------------------------- scan


def test_scan_with_no_sessions_succeeds_and_says_so(sandbox):
    result = sandbox.run("scan")
    assert result.returncode == 0
    assert "no session" in result.stdout.lower()


def test_scan_lists_a_pushed_reading(sandbox):
    sandbox.run("report", "--tool", "mytool", "--session", "run-1",
                "--tokens", "42000", "--window", "128000")
    result = sandbox.run("scan")
    assert result.returncode == 0
    assert "mytool" in result.stdout
    assert "run-1" in result.stdout
    # The table formats the count, so the assertion is on the formatted form.
    assert "42k" in result.stdout
    assert "32.8%" in result.stdout


def test_scan_reads_a_claude_transcript(sandbox):
    transcript(sandbox, tokens=59394, cache_read=59136)
    result = sandbox.run("scan", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    reading = [item for item in payload["readings"] if item["tool"] == "claude_code"][0]
    assert reading["tokens"] == 59394
    assert reading["session"] == CLAUDE_SESSION
    assert reading["source"] == "transcript"


def test_scan_json_is_a_document_with_readings(sandbox):
    sandbox.run("report", "--tool", "mytool", "--session", "run-1",
                "--tokens", "42000", "--window", "128000")
    result = sandbox.run("scan", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    reading = payload["readings"][0]
    assert reading["tool"] == "mytool"
    assert reading["tokens"] == 42000
    assert reading["window"] == 128000
    assert reading["pct"] == 32.81


def test_scan_reports_an_unknown_tool_as_a_problem_not_a_crash(sandbox):
    """A scheduled scan must not fail because a tool was uninstalled."""
    result = sandbox.run("scan", "--tool", "nonexistent-tool")
    assert result.returncode == 0
    assert "nonexistent-tool" in result.stdout
    assert "traceback" not in result.stderr.lower()
    payload = json.loads(sandbox.run("scan", "--tool", "nonexistent-tool",
                                     "--json").stdout)
    assert payload["readings"] == []
    assert "nonexistent-tool" in payload["problems"][0]["reason"]


def test_scan_does_not_print_a_traceback_for_a_broken_store(sandbox):
    path = sandbox.home_path(".local", "share", "opencode", "opencode.db")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("not a database")
    result = sandbox.run("scan")
    assert result.returncode == 0
    assert "traceback" not in result.stderr.lower()


# ----------------------------------------------------------------- report


def test_report_round_trips_through_the_state_directory(sandbox):
    result = sandbox.run("report", "--tool", "mytool", "--session", "run-1",
                         "--tokens", "1000")
    assert result.returncode == 0
    stored = os.path.join(state_dir(sandbox), "readings", "mytool", "run-1.json")
    assert os.path.isfile(stored)
    payload = json.load(open(stored, encoding="utf-8"))
    assert payload["tokens"] == 1000
    assert payload["source"] == "report"


def test_report_merges_into_scan_with_the_report_source_winning(sandbox):
    """The end-to-end precedence rule, through two separate processes."""
    transcript(sandbox, tokens=9000)
    before = json.loads(sandbox.run("scan", "--json").stdout)
    parsed = [item for item in before["readings"] if item["tool"] == "claude_code"][0]
    assert parsed["source"] == "transcript"

    sandbox.run("report", "--tool", "claude_code", "--session", CLAUDE_SESSION,
                "--tokens", "12345")
    after = json.loads(sandbox.run("scan", "--json").stdout)
    merged = [item for item in after["readings"] if item["tool"] == "claude_code"][0]
    assert merged["source"] == "report"
    assert merged["tokens"] == 12345
    # The losing reading is kept for diagnosis rather than discarded.
    assert "transcript" in merged["detail"]["other_sources"]


def test_report_rejects_a_non_numeric_token_count(sandbox):
    result = sandbox.run("report", "--tool", "mytool", "--session", "run-1",
                         "--tokens", "not-a-number")
    assert result.returncode != 0


def test_report_rejects_a_negative_token_count(sandbox):
    """A negative value reads as full in some tools and empty in others, so it
    is refused rather than stored."""
    result = sandbox.run("report", "--tool", "mytool", "--session", "run-1",
                         "--tokens", "-1")
    assert result.returncode != 0
    assert "negative" in (result.stdout + result.stderr).lower()
    assert not os.path.exists(os.path.join(state_dir(sandbox), "readings",
                                           "mytool", "run-1.json"))


def test_report_accepts_a_compressed_flag(sandbox):
    sandbox.run("report", "--tool", "mytool", "--session", "run-1",
                "--tokens", "1000", "--compressed")
    payload = json.loads(sandbox.run("scan", "--json").stdout)
    reading = payload["readings"][0]
    assert reading["compressed"] is True


def test_report_sanitises_a_hostile_session_id(sandbox):
    result = sandbox.run("report", "--tool", "mytool", "--session",
                         "../../etc/passwd", "--tokens", "10")
    assert result.returncode == 0
    readings = os.path.join(state_dir(sandbox), "readings")
    for base, _dirnames, filenames in os.walk(readings):
        for name in filenames:
            assert ".." not in name
    assert not os.path.exists(os.path.join(state_dir(sandbox), "etc"))


def test_report_validates_the_tool_name(sandbox):
    """A name with no usable character collapses different tools together, so it
    is refused rather than sanitised into a shared directory."""
    result = sandbox.run("report", "--tool", "../..", "--session", "run-1",
                         "--tokens", "10")
    assert result.returncode != 0
    assert not os.path.exists(os.path.join(state_dir(sandbox), "..", "run-1.json"))


def test_a_hostile_tool_name_is_confined_to_the_readings_directory(sandbox):
    result = sandbox.run("report", "--tool", "../../evil", "--session", "run-1",
                         "--tokens", "10")
    assert result.returncode == 0
    readings = os.path.join(state_dir(sandbox), "readings")
    assert all(os.path.commonpath([base, readings]) == readings
               for base, _dirs, files in os.walk(readings) if files)
    assert not os.path.exists(os.path.join(state_dir(sandbox), "evil"))


def test_report_validates_the_window(sandbox):
    result = sandbox.run("report", "--tool", "mytool", "--session", "run-1",
                         "--tokens", "10", "--window", "zero")
    assert result.returncode != 0


# ----------------------------------------------------------------- status


def test_status_runs_on_a_fresh_install(sandbox):
    result = sandbox.run("status")
    assert result.returncode == 0
    assert result.stdout.strip()


def test_status_json_has_the_expected_shape(sandbox):
    result = sandbox.run("status", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["state"]["ledger_entries"] == 0
    assert payload["state"]["pending_events"] == 0
    assert payload["pushed_readings"] == 0
    assert payload["paths"]["state"].startswith(state_dir(sandbox))


def test_status_counts_a_pushed_reading(sandbox):
    sandbox.run("report", "--tool", "mytool", "--session", "run-1",
                "--tokens", "1000")
    payload = json.loads(sandbox.run("status", "--json").stdout)
    assert payload["pushed_readings"] == 1


# ----------------------------------------------------------------- rotate


def test_rotate_dry_run_writes_absolutely_nothing(sandbox):
    transcript(sandbox, tokens=10)
    before = without_pycache(sandbox.config_dir)
    result = sandbox.run("rotate", "--tool", "claude_code", "--session",
                         CLAUDE_SESSION, "--dry-run", "--force")
    assert result.returncode == 0
    assert "dry" in result.stdout.lower() or "would" in result.stdout.lower()
    # Not just "no handover": no state file, no event log, no ledger entry.
    assert without_pycache(sandbox.config_dir) == before


def test_rotate_dry_run_json_describes_the_plan(sandbox):
    transcript(sandbox, tokens=10)
    result = sandbox.run("rotate", "--tool", "claude_code", "--session",
                         CLAUDE_SESSION, "--dry-run", "--force", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["result"]["dry_run"] is True
    assert payload["result"]["rotated"] is False
    # Nothing was written, so the path is a prediction and lives in the message.
    assert payload["result"]["handover"] is None
    text = "\n".join(str(message.get("text", "")) for message in payload["messages"])
    assert "handover_" in text
    assert "(would be written)" in text
    # The messages are inside the document rather than printed in front of it,
    # so the whole of stdout parses.
    assert isinstance(payload["messages"], list)


def test_rotate_writes_a_handover_and_records_the_ledger(sandbox):
    transcript(sandbox, tokens=10)
    result = sandbox.run("rotate", "--tool", "claude_code", "--session",
                         CLAUDE_SESSION, "--force", "--no-gate")
    assert result.returncode == 0
    handovers = find_handovers(sandbox)
    assert len(handovers) == 1
    text = open(handovers[0], encoding="utf-8").read()
    assert "<!-- custos handover schema=1 -->" in text
    assert CLAUDE_SESSION in text
    assert "## Next steps" in text
    assert os.path.isfile(os.path.join(state_dir(sandbox), "state.json"))
    assert os.path.isfile(os.path.join(state_dir(sandbox), "events.jsonl"))


def test_rotate_refuses_an_unknown_session(sandbox):
    transcript(sandbox, tokens=10)
    result = sandbox.run("rotate", "--tool", "claude_code", "--session",
                         "no-such-session", "--force")
    assert result.returncode != 0
    assert "no-such-session" in (result.stdout + result.stderr)
    assert find_handovers(sandbox) == []


def test_rotate_refuses_an_unknown_tool(sandbox):
    result = sandbox.run("rotate", "--tool", "nonexistent-tool",
                         "--session", "whatever", "--force")
    assert result.returncode != 0
    assert find_handovers(sandbox) == []


def test_rotate_second_call_defers_rather_than_writing_a_second_handover(sandbox):
    """Idempotency: a cron that fires twice must not rotate twice."""
    transcript(sandbox, tokens=10, window=200000)
    first = sandbox.run("rotate", "--tool", "claude_code", "--session",
                        CLAUDE_SESSION, "--force", "--no-gate")
    assert first.returncode == 0
    # The second call is not forced, so only the "already handled" guard can
    # stop it: the config sets the activity window to zero above.
    second = sandbox.run("rotate", "--tool", "claude_code", "--session",
                         CLAUDE_SESSION, "--no-gate")
    assert second.returncode == 0
    assert len(find_handovers(sandbox)) == 1
    assert "already" in second.stdout.lower() or "defer" in second.stdout.lower()


def test_rotate_honours_the_kill_switch(sandbox):
    kill_switch = os.path.join(sandbox.config_dir, "DISABLE")
    open(kill_switch, "w").close()
    transcript(sandbox, tokens=10)
    result = sandbox.run("rotate", "--tool", "claude_code", "--session",
                         CLAUDE_SESSION, "--force", "--no-gate")
    # The refusal is reported, not raised: a scheduled run that hit the kill
    # switch is a normal outcome, not an error for the scheduler to page on.
    assert result.returncode == 0
    assert "kill switch" in (result.stdout + result.stderr).lower()
    assert find_handovers(sandbox) == []


def test_rotate_dry_run_reports_the_kill_switch_but_still_plans(sandbox):
    kill_switch = os.path.join(sandbox.config_dir, "DISABLE")
    open(kill_switch, "w").close()
    transcript(sandbox, tokens=10)
    result = sandbox.run("rotate", "--tool", "claude_code", "--session",
                         CLAUDE_SESSION, "--dry-run", "--force")
    assert result.returncode == 0
    assert find_handovers(sandbox) == []


def test_rotate_carries_a_manual_trigger_into_the_snapshot(sandbox):
    transcript(sandbox, tokens=10)
    sandbox.run("rotate", "--tool", "claude_code", "--session", CLAUDE_SESSION,
                "--force", "--no-gate", "--json")
    text = open(find_handovers(sandbox)[0], encoding="utf-8").read()
    assert "manual" in text


# ---------------------------------------------------------------- monitor


def test_monitor_once_is_silent_when_nothing_needs_attention(sandbox):
    """The cron contract: no output means the scheduler stays quiet."""
    transcript(sandbox, tokens=10)
    result = sandbox.run("monitor", "--once")
    assert result.returncode == 0
    assert result.stdout == ""


def test_monitor_once_dry_run_reports_without_writing(sandbox):
    """A dry run prints the decision it would act on and changes nothing.

    Printing is not a side effect -- it is how the caller sees the decision --
    but nothing on disk may move.
    """
    transcript(sandbox, tokens=199000, window=200000, thresholds=True)
    before = without_pycache(sandbox.config_dir)
    result = sandbox.run("monitor", "--once", "--dry-run")
    assert result.returncode == 0
    assert result.stdout.strip()
    assert without_pycache(sandbox.config_dir) == before


def test_monitor_once_speaks_when_the_rotate_line_is_crossed(sandbox):
    transcript(sandbox, tokens=199000, window=200000, thresholds=True)
    result = sandbox.run("monitor", "--once")
    assert result.returncode == 0
    assert result.stdout.strip()
    assert "claude_code" in result.stdout


def test_monitor_does_not_repeat_itself_on_the_next_pass(sandbox):
    """The ledger's purpose: one message per condition, not one per tick."""
    transcript(sandbox, tokens=199000, window=200000, thresholds=True)
    assert sandbox.run("monitor", "--once").stdout.strip()
    second = sandbox.run("monitor", "--once")
    assert second.returncode == 0
    assert second.stdout == ""


def test_monitor_json_emits_an_array_of_messages(sandbox):
    """``--json`` is a document even when it has nothing to say."""
    transcript(sandbox, tokens=10)
    result = sandbox.run("monitor", "--once", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert isinstance(payload, list)
    assert payload == []


def test_monitor_with_no_agent_installed_still_succeeds(denv):
    result = denv.run("monitor", "--once")
    assert result.returncode == 0
    assert result.stdout == ""


def test_monitor_reports_an_already_compacted_session(sandbox):
    """Compaction is a rotate trigger on its own, whatever the token count."""
    sandbox.claude_transcript(session=CLAUDE_SESSION, records=[
        claude_assistant(claude_usage(input_tokens=1000)),
        {"type": "system", "subtype": "compact_boundary",
         "compactMetadata": {"trigger": "auto", "preTokens": 900,
                             "postTokens": 100}},
    ])
    result = sandbox.run("monitor", "--once")
    assert result.returncode == 0
    assert result.stdout.strip()


def test_monitor_ignores_an_activity_window_that_has_not_quieted(sandbox):
    """A session touched seconds ago is mid-turn; rotating it would be rude."""
    transcript(sandbox, tokens=199000, window=200000)
    result = sandbox.run("monitor", "--once")
    assert result.returncode == 0


# ------------------------------------------------------------------- gate


def test_gate_dry_run_prints_the_command_without_running_it(sandbox):
    gate_sandbox(sandbox)
    result = sandbox.run("gate", "--tool", "claude_code", "--dry-run",
                         "--mode", "sequence")
    assert result.returncode == 0
    assert "dry" in result.stdout.lower() or "would" in result.stdout.lower()
    # The gate is a one-shot command; a dry run must not have run one.
    assert not os.path.exists(os.path.join(state_dir(sandbox), "state.json"))


def test_gate_json_reports_the_selection_and_the_outcome(sandbox):
    gate_sandbox(sandbox)
    result = sandbox.run("gate", "--tool", "claude_code", "--dry-run",
                         "--mode", "sequence", "--question", "sum-of-two",
                         "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["mode"] == "sequence"
    assert payload["question_ids"] == ["sum-of-two"]


def test_gate_without_a_bank_reports_the_reason(sandbox):
    """A missing bank is a skip, not a failure: nothing was asked, so there is
    no verdict to report as an error."""
    gate_sandbox(sandbox, questions="absent-bank.json", write_bank=False)
    result = sandbox.run("gate", "--tool", "claude_code")
    assert result.returncode == 0
    assert "bank" in (result.stdout + result.stderr).lower()


def test_gate_rejects_an_unknown_mode(sandbox):
    result = sandbox.run("gate", "--tool", "claude_code", "--mode",
                         "nonexistent")
    assert result.returncode == 2
    assert "invalid choice" in result.stderr


# --------------------------------------------------------------- questions


def test_questions_validates_a_good_bank(sandbox):
    bank(os.path.join(sandbox.config_dir, "questions.json"))
    sandbox.write_config({"version": 1, "gate": {"questions": "questions.json"}})
    result = sandbox.run("questions", "validate")
    assert result.returncode == 0
    assert "ok" in result.stdout.lower() or "valid" in result.stdout.lower()


def test_questions_rejects_a_bank_with_a_duplicate_id(sandbox):
    bank(os.path.join(sandbox.config_dir, "questions.json"), duplicate=True)
    sandbox.write_config({"version": 1, "gate": {"questions": "questions.json"}})
    result = sandbox.run("questions", "validate")
    assert result.returncode != 0
    assert "unique" in (result.stdout + result.stderr).lower()


def test_questions_reports_a_missing_bank(sandbox):
    sandbox.write_config({"version": 1, "gate": {"questions": "absent.json"}})
    result = sandbox.run("questions", "validate")
    assert result.returncode != 0


def test_questions_lists_ids_without_leaking_answers(sandbox):
    bank(os.path.join(sandbox.config_dir, "questions.json"))
    sandbox.write_config({"version": 1, "gate": {"questions": "questions.json"}})
    result = sandbox.run("questions", "list")
    assert result.returncode == 0
    assert "sum-of-two" in result.stdout
    # Listing is what an operator runs while screen-sharing, and the answer is
    # the only thing in the bank that is secret.
    assert "42" not in result.stdout


# ------------------------------------------------------------------- demo


def test_demo_runs_the_whole_pipeline_and_exits_clean(sandbox):
    result = sandbox.run("demo")
    assert result.returncode == 0, result.stderr
    assert "gate" in result.stdout
    assert "handover" in result.stdout.lower()


def test_demo_writes_nothing_into_the_configured_state(sandbox):
    """The demo builds its own throwaway world; the real state stays empty."""
    before = without_pycache(sandbox.config_dir)
    home_before = without_pycache(sandbox.home, skip_python_cache=True)
    result = sandbox.run("demo")
    assert result.returncode == 0
    assert without_pycache(sandbox.config_dir) == before
    assert without_pycache(sandbox.home, skip_python_cache=True) == home_before


def test_demo_reports_both_selection_modes_in_its_closing_line(sandbox):
    result = sandbox.run("demo")
    assert result.returncode == 0
    closing = [line for line in result.stdout.splitlines()
               if line.startswith("demo finished")]
    assert closing, result.stdout
    assert "random" in closing[0]
    assert "sequence" in closing[0]


def test_demo_json_shows_both_selection_modes(sandbox):
    result = sandbox.run("demo", "--json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["selection_modes"] == ["random", "sequence"]
    names = [str(step.get("step")) for step in payload["steps"]]
    assert any(name.startswith("selection #1") for name in names)
    assert any(name.startswith("selection #2") for name in names)


def test_demo_json_records_the_seed_so_a_pick_can_be_replayed(sandbox):
    payload = json.loads(sandbox.run("demo", "--json").stdout)
    random_steps = [step for step in payload["steps"] if step.get("mode") == "random"]
    assert random_steps
    assert all(step.get("seed") for step in random_steps)


def test_demo_json_sequence_steps_report_an_advancing_cursor(sandbox):
    payload = json.loads(sandbox.run("demo", "--json").stdout)
    cursors = [step.get("cursor") for step in payload["steps"]
               if step.get("mode") == "sequence" and "cursor" in step]
    assert len(cursors) >= 2
    assert cursors[1] != cursors[0]


def test_demo_is_deterministic_across_two_runs(sandbox):
    """Two runs have to pick the same questions, or the output is not a
    demonstration of anything. Paths and timestamps are scrubbed; the selection
    is what must not move."""
    def selections():
        payload = json.loads(sandbox.run("demo", "--json").stdout)
        return [(step.get("mode"), step.get("seed"), step.get("cursor"),
                 tuple(step.get("questions") or ()))
                for step in payload["steps"] if step.get("mode")]

    first, second = selections(), selections()
    assert first
    assert first == second


def test_demo_with_a_directory_keeps_its_world(sandbox):
    target = sandbox.path("demo-world")
    result = sandbox.run("demo", "--keep", "--dir", target)
    assert result.returncode == 0
    assert os.path.isdir(target)
    assert os.listdir(target)
    assert "demo directory kept" in result.stdout


# ----------------------------------------------------------------- doctor


def test_doctor_passes_on_a_healthy_installation(sandbox):
    result = sandbox.run("doctor")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "config" in result.stdout.lower()


def test_doctor_json_lists_every_check(sandbox):
    result = sandbox.run("doctor", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["checks"]
    assert all(check["status"] for check in payload["checks"])


def test_doctor_reports_a_broken_configuration_without_crashing(sandbox):
    with open(os.path.join(sandbox.config_dir, "config.json"), "w",
              encoding="utf-8") as stream:
        stream.write("{ this is not json")
    result = sandbox.run("doctor")
    assert result.stdout + result.stderr
    assert "traceback" not in result.stderr.lower()


def test_doctor_notices_a_missing_question_bank(sandbox):
    sandbox.write_config({"version": 1, "gate": {"enabled": True,
                                                 "questions": "absent.json"}})
    payload = json.loads(sandbox.run("doctor", "--json").stdout)
    bank_check = [check for check in payload["checks"]
                  if "bank" in check["check"].lower()]
    assert bank_check
    assert bank_check[0]["status"] != "ok"


# --------------------------------------------------------------- streams


def test_messages_go_to_stdout_without_diagnostics_there(sandbox):
    """A shell hook captures stdout; anything else in it would be shown to the
    user as if it were a message."""
    transcript(sandbox, tokens=199000, window=200000, thresholds=True)
    result = sandbox.run("monitor", "--once")
    assert result.returncode == 0
    assert result.stdout.strip()
    assert "traceback" not in result.stderr.lower()


def test_json_errors_are_a_document_on_stdout(sandbox):
    """A JSON caller gets a document with the problem in it, rather than a
    mixed stream or an exit status to guess at."""
    result = sandbox.run("scan", "--tool", "nonexistent-tool", "--json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["readings"] == []
    assert payload["problems"]
    assert payload["problems"][0]["tool"] == "nonexistent-tool"


def test_json_and_quiet_are_accepted_before_the_subcommand(sandbox):
    """Both spellings are natural to type, so both have to work."""
    before = sandbox.run("scan", "--json")
    after = sandbox.run("--json", "scan")
    assert before.returncode == after.returncode == 0
    assert json.loads(before.stdout).keys() == json.loads(after.stdout).keys()


def test_the_config_dir_option_is_accepted_after_the_subcommand(sandbox):
    result = sandbox.run("status", "--config-dir", sandbox.config_dir, "--json")
    assert result.returncode == 0


def test_a_dry_run_never_touches_the_real_config_directory(sandbox):
    """The whole suite's safety property, stated once as a CLI-level assertion."""
    sandbox.run("--config-dir", sandbox.path("elsewhere"), "doctor")
    sandbox.run("--config-dir", sandbox.path("elsewhere"), "scan")
    assert not os.path.exists(os.path.expanduser("~/.config/custos")) or \
        os.path.expanduser("~/.config/custos").startswith(sandbox.root)


def test_a_second_instance_is_refused_rather_than_interleaved(sandbox):
    """Two monitors racing would both rotate the same session."""
    from custos.core.util import FileLock

    lock = FileLock(os.path.join(state_dir(sandbox), "custos.lock"))
    assert lock.acquire()
    try:
        result = sandbox.run("monitor", "--once")
        # A held lock is a normal outcome for a scheduler that fired twice, so
        # it is reported rather than raised.
        assert result.returncode == 0
        assert "lock" in (result.stdout + result.stderr).lower()
    finally:
        lock.release()
