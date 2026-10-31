"""Persistent state: atomic writes, the ledger, events and pushed readings.

The state file is what stops the tool from repeating itself, and the event map is
what carries a gate streak across scheduled runs. When either of them loses data
quietly the visible symptom is a duplicate notification, which is exactly the
kind of bug that survives for months -- so the expiry and pruning rules are
asserted here rather than trusted.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from custos.core.state import (
    DEFAULT_PENDING_MAX_AGE_DAYS,
    EVENT_PENDING,
    EVENT_RESOLVED,
    EventLog,
    ReadingStore,
    State,
)
from custos.core.usage import SOURCE_PRIORITY, Reading, merge_readings, pick_latest


def data_reading(tokens, ts, source="transcript", session="session-1", tool="synthetic"):
    return Reading(tool=tool, session=session, tokens=tokens, ts=ts, source=source)


# ------------------------------------------------------------------- atomic


def test_state_survives_a_write_that_is_interrupted(tmp_path, monkeypatch):
    """A crash mid-write must leave the previous file intact, with no debris."""
    path = str(tmp_path / "state.json")
    state = State.load(path)
    state.mark("first:key")
    state.save()
    before = open(path, encoding="utf-8").read()

    state.mark("second:key")

    real_replace = os.replace

    def exploding_replace(src, dst):
        raise OSError("simulated crash before the rename")

    monkeypatch.setattr(os, "replace", exploding_replace)
    with pytest.raises(OSError):
        state.save()
    monkeypatch.setattr(os, "replace", real_replace)

    assert open(path, encoding="utf-8").read() == before
    assert os.listdir(str(tmp_path)) == ["state.json"]


def test_state_round_trips_through_the_file(tmp_path):
    path = str(tmp_path / "state.json")
    state = State.load(path)
    state.mark("rotate:synthetic:session-1:near_line", "the reason")
    state.record_handled("synthetic:session-1", "session-1", {"mode": "assisted"})
    state.set_gate_sequence(4)
    state.note_gate_attempts(3)
    state.save()

    reloaded = State.load(path)
    assert reloaded.seen("rotate:synthetic:session-1:near_line")
    assert reloaded.last_handled("synthetic:session-1")["session"] == "session-1"
    assert reloaded.gate_sequence() == 4
    assert reloaded.gate_attempts_total() == 3


def test_a_corrupt_state_file_loads_as_empty_rather_than_failing(tmp_path):
    path = str(tmp_path / "state.json")
    with open(path, "w", encoding="utf-8") as stream:
        stream.write("{not json at all")
    state = State.load(path)
    assert state.summary()["ledger_entries"] == 0
    assert state.ledger == {}


def test_a_state_file_with_the_wrong_types_is_repaired_on_load(tmp_path):
    path = str(tmp_path / "state.json")
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({"ledger": [], "history": "nope", "events": None, "gate": 7}, stream)
    state = State.load(path)
    assert isinstance(state.ledger, dict)
    assert isinstance(state.history, dict)
    assert isinstance(state.events, dict)
    assert isinstance(state.gate, dict)


# ------------------------------------------------------------------- ledger


def test_once_returns_true_only_the_first_time(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    assert state.once("warn:synthetic:session-1:2026-10-16") is True
    assert state.once("warn:synthetic:session-1:2026-10-16") is False


def test_unmark_forgets_a_key(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.mark("key")
    state.unmark("key")
    assert state.seen("key") is False
    assert state.once("key") is True


def test_prune_ledger_drops_entries_past_the_cutoff(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.ledger["old"] = {"ts": "2020-01-01T00:00:00Z",
                           "epoch": time.time() - 100 * 86400}
    state.ledger["fresh"] = {"ts": "2026-10-16T00:00:00Z", "epoch": time.time()}
    removed = state.prune_ledger(max_age_days=45)
    assert removed == 1
    assert set(state.ledger) == {"fresh"}


def test_prune_ledger_reads_an_iso_timestamp_when_the_epoch_is_missing(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.ledger["iso-old"] = {"ts": "2020-01-01T00:00:00Z"}
    state.ledger["no-time"] = {"detail": "written by an older version"}
    assert state.prune_ledger(max_age_days=45) == 2


# ------------------------------------------------------------------- events


def test_pending_events_are_returned_oldest_first(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    now = time.time()
    state.put_event("a", {"event_id": "ev-a", "status": EVENT_PENDING,
                          "created_epoch": now - 200.0})
    state.put_event("b", {"event_id": "ev-b", "status": EVENT_PENDING,
                          "created_epoch": now - 300.0})
    state.put_event("c", {"event_id": "ev-c", "status": EVENT_RESOLVED,
                          "created_epoch": now - 400.0})
    # Oldest first: the caller acts on one event per pass, so the order decides
    # which backlog item is cleared next.
    assert [event["event_id"] for event in state.pending_events()] == ["ev-b", "ev-a"]


def test_pending_events_expire_on_read(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.put_event("stale", {
        "event_id": "ev-stale", "status": EVENT_PENDING,
        "created_epoch": time.time() - (DEFAULT_PENDING_MAX_AGE_DAYS + 1) * 86400,
    })
    state.put_event("fresh", {
        "event_id": "ev-fresh", "status": EVENT_PENDING, "created_epoch": time.time(),
    })
    pending = state.pending_events()
    assert [event["event_id"] for event in pending] == ["ev-fresh"]
    assert state.get_event("stale")["status"] == "expired"
    assert state.get_event("stale")["expired_at"]


def test_an_expired_event_does_not_block_a_rotation(tmp_path):
    from custos.core.rotation import check_pending_gate, RotationContext

    state = State.load(str(tmp_path / "state.json"))
    state.put_event("synthetic:session-1", {
        "event_id": "ev-stale", "status": EVENT_PENDING,
        "created_epoch": time.time() - 30 * 86400, "attempts": [1, 2, 3],
    })
    # `pending_for_session` is the interlock's query and it reads the raw map, so
    # the expiry has to have happened before the check -- which is what a monitor
    # pass does by calling `pending_events()` first.
    assert check_pending_gate(
        RotationContext(session_key="synthetic:session-1", state=state))
    state.pending_events()
    assert check_pending_gate(
        RotationContext(session_key="synthetic:session-1", state=state)) == []


def test_resolve_event_keeps_the_record(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.put_event("k", {"event_id": "ev", "status": EVENT_PENDING,
                          "created_epoch": time.time()})
    state.resolve_event("k", "gate_passed", extra="value")
    event = state.get_event("k")
    assert event["status"] == EVENT_RESOLVED
    assert event["resolution"] == "gate_passed"
    assert event["extra"] == "value"
    assert state.pending_for_session("k") is None


def test_prune_events_keeps_the_newest_resolved_records(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    for index in range(5):
        state.put_event("k%d" % index, {
            "event_id": "ev-%d" % index, "status": EVENT_RESOLVED,
            "created_epoch": float(index)})
    state.put_event("pending", {"event_id": "ev-p", "status": EVENT_PENDING,
                                "created_epoch": 99.0})
    removed = state.prune_events(keep=2)
    assert removed == 3
    assert set(state.events) == {"k3", "k4", "pending"}


def test_pending_events_drops_a_non_mapping_record(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.events["broken"] = "not a mapping"
    assert state.pending_events() == []
    assert "broken" not in state.events


# ------------------------------------------------------------------ gate


def test_gate_counters_accumulate(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.note_gate_attempts(2)
    state.note_gate_attempts(3)
    assert state.gate_attempts_total() == 5
    assert state.last_gate_attempt() > 0


def test_gate_counters_ignore_a_zero_delta(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.note_gate_attempts(0)
    assert state.gate_attempts_total() == 0
    assert state.last_gate_attempt() == 0.0


def test_gate_sequence_tolerates_a_corrupt_value(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.gate["sequence"] = "not a number"
    assert state.gate_sequence() == 0


# ------------------------------------------------------------------ alerts


def test_alert_throttle_allows_one_alert_per_repeat_window(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.put_event("k", {"event_id": "ev", "status": EVENT_PENDING,
                          "created_epoch": time.time()})
    assert state.alert_due("k", "degraded", repeat_seconds=3600) is True
    state.mark_alert("k", "degraded")
    assert state.alert_due("k", "degraded", repeat_seconds=3600) is False
    assert state.alert_due("k", "infra", repeat_seconds=3600) is True


def test_alert_throttle_reopens_after_the_window(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.put_event("k", {"event_id": "ev", "status": EVENT_PENDING,
                          "created_epoch": time.time()})
    state.mark_alert("k", "degraded")
    state.alerts("k")["degraded_last_epoch"] = time.time() - 7200
    assert state.alert_due("k", "degraded", repeat_seconds=3600) is True


def test_mark_alert_counts_emissions(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.put_event("k", {"event_id": "ev", "status": EVENT_PENDING,
                          "created_epoch": time.time()})
    state.mark_alert("k", "degraded")
    state.mark_alert("k", "degraded")
    assert state.alerts("k")["degraded_count"] == 2


# ------------------------------------------------------------------ summary


def test_summary_reports_the_ledger_events_and_last_monitor_run(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    state.mark("key")
    state.put_event("k", {"event_id": "ev", "status": EVENT_PENDING,
                          "created_epoch": time.time()})
    state.put_event("done", {"event_id": "ev2", "status": EVENT_RESOLVED,
                             "created_epoch": time.time()})
    state.set_meta("last_monitor_ts", 1770000000.0)
    state.note_gate_attempts(4)
    summary = state.summary()
    assert summary["ledger_entries"] == 1
    assert summary["recorded_events"] == 2
    assert summary["pending_events"] == 1
    assert summary["last_monitor_ts"] == 1770000000.0
    assert summary["gate_attempts"] == 4


# --------------------------------------------------------------- event log


def test_event_log_appends_and_mirrors_into_state(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    events = EventLog(str(tmp_path / "events.jsonl"), state)
    events.event("rotate_assisted", session_key="synthetic:session-1")
    events.event("gate_attempts", attempts=2)

    tail = events.tail()
    assert [record["kind"] for record in tail] == ["rotate_assisted", "gate_attempts"]
    assert tail[0]["session_key"] == "synthetic:session-1"
    assert state.data["last_events"][-1]["kind"] == "gate_attempts"


def test_event_log_keeps_the_ten_most_recent_kinds_in_state(tmp_path):
    state = State.load(str(tmp_path / "state.json"))
    events = EventLog(str(tmp_path / "events.jsonl"), state)
    for index in range(15):
        events.event("kind-%d" % index)
    assert len(state.data["last_events"]) == 10
    assert state.data["last_events"][0]["kind"] == "kind-5"


def test_event_log_tail_skips_unreadable_lines(tmp_path):
    path = str(tmp_path / "events.jsonl")
    with open(path, "w", encoding="utf-8") as stream:
        stream.write('{"kind": "one"}\n')
        stream.write("this line is not json\n")
        stream.write("\n")
        stream.write('{"kind": "two"}\n')
    assert [record["kind"] for record in EventLog(path).tail()] == ["one", "two"]


def test_event_log_tail_of_a_missing_file_is_empty(tmp_path):
    assert EventLog(str(tmp_path / "absent.jsonl")).tail() == []


def test_an_unwritable_event_log_does_not_raise(tmp_path):
    events = EventLog(str(tmp_path / "missing-dir" / "events.jsonl"))
    record = events.event("kind")
    assert record["kind"] == "kind"


# ------------------------------------------------- lock file beside the state


def test_state_save_creates_its_directory(tmp_path):
    path = str(tmp_path / "deep" / "nested" / "state.json")
    state = State.load(path)
    state.mark("key")
    state.save()
    assert os.path.isfile(path)


# ------------------------------------------------------------ reading store


def test_reading_store_round_trips_a_pushed_reading(tmp_path):
    store = ReadingStore(str(tmp_path / "readings"))
    reading = Reading(tool="mytool", session="run-1", tokens=42000, window=128000,
                      source="report", ts=time.time())
    path = store.push(reading)
    assert os.path.basename(path) == "run-1.json"

    loaded = store.load_for("mytool", "run-1")
    assert loaded.tool == "mytool"
    assert loaded.tokens == 42000
    assert loaded.window == 128000
    assert [item.session for item in store.load_all()] == ["run-1"]


def test_reading_store_sanitises_path_components(tmp_path):
    store = ReadingStore(str(tmp_path / "readings"))
    path = store.push(Reading(tool="my/tool", session="../../etc/passwd",
                              tokens=1, source="report"))
    assert ".." not in path
    assert os.path.commonpath([path, store.dir]) == os.path.abspath(store.dir)


def test_reading_store_load_all_skips_unreadable_files(tmp_path):
    store = ReadingStore(str(tmp_path / "readings"))
    os.makedirs(os.path.join(store.dir, "mytool"))
    with open(os.path.join(store.dir, "mytool", "broken.json"), "w",
              encoding="utf-8") as stream:
        stream.write("{not json")
    with open(os.path.join(store.dir, "mytool", "notes.txt"), "w",
              encoding="utf-8") as stream:
        stream.write("ignored")
    assert store.load_all() == []


def test_reading_store_load_for_a_missing_session_is_none(tmp_path):
    assert ReadingStore(str(tmp_path / "readings")).load_for("mytool", "absent") is None


def test_reading_store_prunes_readings_past_the_cutoff(tmp_path):
    store = ReadingStore(str(tmp_path / "readings"))
    store.push(Reading(tool="mytool", session="old", tokens=1, source="report",
                       ts=time.time() - 30 * 86400))
    store.push(Reading(tool="mytool", session="fresh", tokens=1, source="report",
                       ts=time.time()))
    assert store.prune(max_age_days=7) == 1
    assert [item.session for item in store.load_all()] == ["fresh"]


# ------------------------------------------------------------- merge rules


def test_newest_reading_wins():
    older = data_reading(1000, ts=100.0, source="report")
    newer = data_reading(2000, ts=200.0, source="transcript")
    assert pick_latest([older, newer]).tokens == 2000


def test_a_reported_reading_wins_a_tie_against_a_transcript():
    reported = data_reading(9000, ts=500.0, source="report")
    parsed = data_reading(1000, ts=500.0, source="transcript")
    assert pick_latest([parsed, reported]).tokens == 9000


def test_a_larger_reading_wins_a_tie_on_timestamp_and_source():
    truncated = data_reading(10, ts=500.0, source="transcript")
    complete = data_reading(900, ts=500.0, source="transcript")
    assert pick_latest([truncated, complete]).tokens == 900


def test_report_outranks_a_transcript_on_a_tie():
    """The precedence the docs promise, stated as a priority table."""
    assert SOURCE_PRIORITY["report"] > SOURCE_PRIORITY["transcript"]
    assert SOURCE_PRIORITY["hook"] == SOURCE_PRIORITY["report"]
    assert SOURCE_PRIORITY["session-store"] > SOURCE_PRIORITY["transcript"]


def test_merge_readings_keeps_one_winner_per_session_and_records_the_losers():
    reported = data_reading(9000, ts=500.0, source="report")
    parsed = data_reading(1000, ts=500.0, source="transcript", session="session-1")
    other = data_reading(500, ts=100.0, source="transcript", session="session-2")

    merged = merge_readings([parsed, reported, other])
    assert len(merged) == 2
    winner = [item for item in merged if item.session == "session-1"][0]
    assert winner.source == "report"
    assert winner.detail["other_sources"] == ["transcript"]


def test_merge_readings_sorts_newest_first_then_by_size():
    merged = merge_readings([
        data_reading(100, ts=100.0, session="a"),
        data_reading(900, ts=300.0, session="b"),
        data_reading(500, ts=200.0, session="c"),
    ])
    assert [item.session for item in merged] == ["b", "c", "a"]


def test_merge_readings_of_nothing_is_empty():
    assert merge_readings([]) == []
