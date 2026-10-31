"""Handover snapshots.

The snapshot is the artefact that makes rotation safe: a fresh session reads it
to recover work in flight. Its layout is fixed and versioned because other
tooling parses it, so the round-trip and the section list are the contract this
file checks.
"""

from __future__ import annotations

import os

from custos.core.handover import (
    HEADER_PREFIX,
    SCHEMA_VERSION,
    SECTION_NEXT,
    SECTION_RECENT,
    UNAVAILABLE,
    handover_filename,
    read_handover,
    render_handover,
    unique_handover_path,
    write_handover,
)
from custos.core.thresholds import Baseline, Thresholds
from custos.core.usage import Reading


def make_reading(**kwargs):
    kwargs.setdefault("tool", "synthetic")
    kwargs.setdefault("session", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    kwargs.setdefault("tokens", 184000)
    kwargs.setdefault("window", 200000)
    kwargs.setdefault("source", "transcript")
    kwargs.setdefault("ts", 1770000000.0)
    return Reading(**kwargs)


def make_decision(reading):
    thresholds = Thresholds(
        warn=153000, rotate=162000,
        baseline=Baseline(tokens=180000, source="90% of window 200000",
                          mode="percent"))
    return {
        "reason": "near_line", "level": "rotate", "tokens": reading.tokens,
        "thresholds": thresholds, "pct_of_baseline": 102.2,
    }


def test_handover_round_trips_through_the_parser(sandbox):
    reading = make_reading()
    path = write_handover(sandbox.path("handovers"), reading=reading,
                          decision=make_decision(reading),
                          recent_messages=["[user] a synthetic turn",
                                           "[assistant] another synthetic turn"],
                          generated_at="2026-10-16T12:00:00Z")

    parsed = read_handover(path)
    assert parsed["schema_version"] == SCHEMA_VERSION
    fields = parsed["fields"]
    assert fields["tool"] == "synthetic"
    assert fields["session"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert fields["tokens"] == "184000"
    assert fields["window"] == "200000"
    assert fields["percent"] == "92.00%"
    assert fields["source"] == "transcript"
    assert fields["generated"] == "2026-10-16T12:00:00Z"
    assert fields["compacted already"] == "no"
    assert fields["trigger"] == "tokens 184000 reached the rotate line 162000"


def test_handover_has_every_required_section(sandbox):
    path = write_handover(sandbox.path("handovers"), reading=make_reading(),
                          extra={"session key": "synthetic:aaaaaaaa"})
    text = open(path, encoding="utf-8").read()
    for heading in ("# Session handover: synthetic", "## Reading",
                    SECTION_RECENT, SECTION_NEXT, "## Context"):
        assert heading in text, heading
    assert text.startswith(HEADER_PREFIX)


def test_handover_omits_the_context_section_when_there_is_nothing_to_add(sandbox):
    path = write_handover(sandbox.path("handovers"), reading=make_reading())
    assert "## Context" not in open(path, encoding="utf-8").read()


def test_handover_records_the_schema_version_in_its_header(sandbox):
    path = write_handover(sandbox.path("handovers"), reading=make_reading())
    first_line = open(path, encoding="utf-8").readline()
    assert first_line.strip() == "<!-- custos handover schema=%d -->" % SCHEMA_VERSION


def test_handover_says_why_history_is_missing(sandbox):
    path = write_handover(
        sandbox.path("handovers"), reading=make_reading(),
        recent_unavailable_reason="this session was reported by a hook")
    text = open(path, encoding="utf-8").read()
    assert "_Not available: this session was reported by a hook_" in text


def test_handover_without_history_and_without_a_reason_uses_the_default_line(sandbox):
    path = write_handover(sandbox.path("handovers"), reading=make_reading())
    assert UNAVAILABLE in open(path, encoding="utf-8").read()


def test_handover_lists_thresholds_and_context(sandbox):
    reading = make_reading()
    path = write_handover(
        sandbox.path("handovers"), reading=reading, decision=make_decision(reading),
        extra={"session key": "synthetic:aaaaaaaa", "rotation mode": "assisted"})
    text = open(path, encoding="utf-8").read()
    assert "Thresholds in force: baseline 180000, warn 153000, rotate 162000" in text
    assert "- **session key**: synthetic:aaaaaaaa" in text
    assert "- **rotation mode**: assisted" in text


def test_handover_renders_next_steps_as_a_list(sandbox):
    path = write_handover(sandbox.path("handovers"), reading=make_reading(),
                          next_steps=["finish the parser", "run the suite"])
    text = open(path, encoding="utf-8").read()
    assert "1. finish the parser" in text
    assert "1. run the suite" in text


def test_handover_without_next_steps_prompts_for_them(sandbox):
    path = write_handover(sandbox.path("handovers"), reading=make_reading())
    assert "_Fill this in:" in open(path, encoding="utf-8").read()


def test_render_handover_is_the_text_the_writer_produces(sandbox):
    reading = make_reading()
    rendered = render_handover(reading=reading, generated_at="2026-10-16T12:00:00Z")
    path = write_handover(sandbox.path("handovers"), reading=reading,
                          generated_at="2026-10-16T12:00:00Z")
    assert open(path, encoding="utf-8").read() == rendered


def test_a_second_rotation_in_the_same_second_does_not_overwrite(sandbox):
    directory = sandbox.path("handovers")
    first = unique_handover_path(directory, "synthetic", "session-1")
    os.makedirs(directory, exist_ok=True)
    open(first, "w", encoding="utf-8").close()

    reading = make_reading(session="session-1")
    second = write_handover(directory, reading=reading)
    assert second != first
    assert os.path.exists(first)


def test_handover_filenames_are_readable_and_unique(sandbox):
    name = handover_filename("claude_code", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                             stamp="20261016-120000")
    assert name == "handover_claude_code_aaaaaaaa_20261016-120000.md"


def test_handover_filename_survives_a_hostile_session_id(sandbox):
    name = handover_filename("tool", "../../etc/passwd", stamp="20261016-120000")
    assert "/" not in name
    assert name.endswith(".md")


def test_handover_truncates_a_very_long_excerpt(sandbox):
    path = write_handover(sandbox.path("handovers"), reading=make_reading(),
                          recent_messages=["[user] " + "x" * 900])
    line = [line for line in open(path, encoding="utf-8") if line.startswith("- [user]")][0]
    assert len(line) < 500


def test_handover_records_a_compacted_session(sandbox):
    path = write_handover(sandbox.path("handovers"),
                          reading=make_reading(compressed=True))
    assert read_handover(path)["fields"]["compacted already"] == "yes"


def test_handover_of_an_unknown_window_says_so(sandbox):
    path = write_handover(sandbox.path("handovers"),
                          reading=make_reading(window=None, tokens=5000))
    fields = read_handover(path)["fields"]
    assert fields["window"] == "unknown"
    assert fields["percent"] == "unknown"


def test_handover_with_a_manual_trigger_says_manual(sandbox):
    path = write_handover(sandbox.path("handovers"), reading=make_reading(),
                          trigger_reason="manual")
    assert read_handover(path)["fields"]["trigger"] == "manual"


def test_handover_creates_its_directory(sandbox):
    target = sandbox.path("nested", "handovers")
    path = write_handover(target, reading=make_reading())
    assert os.path.isfile(path)
