"""Threshold resolution and trigger evaluation.

Two ways to express "this session is full" reach the same two lines, and the
fallback chain behind them has three steps. All of it is arithmetic that a
scheduling bug would quietly move, so the numbers are asserted exactly rather
than approximately.
"""

from __future__ import annotations

import pytest

from custos.core.thresholds import (
    Baseline,
    Thresholds,
    describe_trigger,
    evaluate,
    resolve_baseline,
    resolve_thresholds,
)
from custos.core.usage import Reading


def config(sandbox, **sections):
    """Load a config from the sandbox with the given overrides merged in."""
    from custos.core import config as config_module

    payload = {"version": 1}
    payload.update(sections)
    sandbox.write_config(payload)
    return config_module.load_config(sandbox.config_dir)


def reading(tokens=0, window=None, **kwargs):
    kwargs.setdefault("tool", "synthetic")
    kwargs.setdefault("session", "session-1")
    kwargs.setdefault("ts", 1000.0)
    return Reading(tokens=tokens, window=window, **kwargs)


# ------------------------------------------------------------------ baseline


def test_absolute_baseline_comes_from_configuration(sandbox):
    cfg = config(sandbox, thresholds={"baseline_tokens": 100000,
                                      "baseline_percent": None})
    baseline = resolve_baseline(cfg, reading(tokens=1, window=200000))
    assert baseline.tokens == 100000
    assert baseline.mode == "absolute"
    assert baseline.source == "configured"


def test_percent_baseline_is_applied_to_the_readings_window(sandbox):
    cfg = config(sandbox, thresholds={"baseline_tokens": None,
                                      "baseline_percent": 80})
    baseline = resolve_baseline(cfg, reading(tokens=1, window=200000))
    assert baseline.tokens == 160000
    assert baseline.mode == "percent"
    assert "80% of window 200000" in baseline.source


def test_percent_below_one_is_read_as_a_fraction(sandbox):
    cfg = config(sandbox, thresholds={"baseline_tokens": None,
                                      "baseline_percent": 0.75})
    baseline = resolve_baseline(cfg, reading(tokens=1, window=200000))
    assert baseline.tokens == 150000


def test_percent_mode_without_a_window_falls_back_to_the_absolute_value(sandbox):
    cfg = config(sandbox, thresholds={"baseline_tokens": 90000,
                                      "baseline_percent": 80})
    baseline = resolve_baseline(cfg, reading(tokens=1, window=None))
    assert baseline.tokens == 90000
    assert "no window" in baseline.source


def test_no_configuration_at_all_uses_a_share_of_the_window(sandbox):
    cfg = config(sandbox, thresholds={"baseline_tokens": None,
                                      "baseline_percent": None})
    baseline = resolve_baseline(cfg, reading(tokens=1, window=200000))
    assert baseline.tokens == 150000
    assert baseline.source == "fallback"


def test_fallback_without_a_window_is_a_fixed_default(sandbox):
    cfg = config(sandbox, thresholds={"baseline_tokens": None,
                                      "baseline_percent": None})
    assert resolve_baseline(cfg, reading(tokens=1)).tokens == 200000


# --------------------------------------------------------------------- lines


def test_warn_and_rotate_lines_are_factors_of_the_baseline(sandbox):
    cfg = config(sandbox, thresholds={
        "baseline_tokens": 200000, "baseline_percent": None,
        "warn_factor": 0.5, "rotate_factor": 0.75})
    thresholds = resolve_thresholds(cfg, reading(tokens=1, window=200000))
    assert (thresholds.warn, thresholds.rotate) == (100000, 150000)
    assert thresholds.describe() == "baseline 200000; warn 100000; rotate 150000"


def test_a_non_positive_factor_falls_back_to_the_default(sandbox):
    cfg = config(sandbox, thresholds={
        "baseline_tokens": 100000, "baseline_percent": None,
        "warn_factor": 0, "rotate_factor": -1})
    thresholds = resolve_thresholds(cfg, reading(tokens=1, window=200000))
    assert thresholds.warn_factor == 0.90
    assert thresholds.rotate_factor == 0.93


def test_an_unparseable_factor_falls_back_to_the_default(sandbox):
    cfg = config(sandbox, thresholds={
        "baseline_tokens": 100000, "baseline_percent": None,
        "warn_factor": "eighty percent"})
    assert resolve_thresholds(cfg, reading(tokens=1)).warn_factor == 0.90


# ------------------------------------------------------------------ evaluate


def _thresholds(baseline=100000, warn_factor=0.90, rotate_factor=0.93):
    return Thresholds(
        warn=int(baseline * warn_factor),
        rotate=int(baseline * rotate_factor),
        baseline=Baseline(tokens=baseline, source="test", mode="absolute"),
        warn_factor=warn_factor, rotate_factor=rotate_factor)


def test_no_trigger_below_the_warn_line():
    assert evaluate(reading(tokens=89999), _thresholds()) is None


def test_the_warn_line_triggers_a_warning():
    decision = evaluate(reading(tokens=90000), _thresholds())
    assert decision["level"] == "warn"
    assert decision["reason"] == "warn_line"
    assert decision["pct_of_baseline"] == 90.0


def test_the_rotate_line_triggers_a_rotation():
    decision = evaluate(reading(tokens=93000), _thresholds())
    assert decision["level"] == "rotate"
    assert decision["reason"] == "near_line"


def test_a_negative_reading_is_reported_as_an_anomaly():
    decision = evaluate(reading(tokens=-1), _thresholds())
    assert decision["level"] == "anomaly"
    assert decision["reason"] == "invalid_reading"


def test_no_reading_is_not_a_trigger():
    assert evaluate(None, _thresholds()) is None


def test_a_compacted_session_triggers_regardless_of_its_size():
    """Already-compacted means history is being discarded; waiting is pointless."""
    decision = evaluate(reading(tokens=100, compressed=True), _thresholds())
    assert decision["level"] == "rotate"
    assert decision["reason"] == "compressed_today"


def test_a_remembered_compaction_triggers_even_when_the_reading_is_quiet():
    decision = evaluate(reading(tokens=100), _thresholds(),
                        already_seen_compression=True)
    assert decision["reason"] == "compressed_today"


@pytest.mark.parametrize("level,reason,expected", [
    ("rotate", "compressed_today", "session already compacted (tokens 100)"),
    ("rotate", "near_line", "tokens 100 reached the rotate line 93000"),
    ("warn", "warn_line", "tokens 100 reached the warn line 90000"),
    ("anomaly", "invalid_reading", "reading 100 looks invalid"),
])
def test_describe_trigger_explains_each_reason(level, reason, expected):
    decision = {
        "reason": reason, "level": level, "tokens": 100,
        "thresholds": _thresholds(),
    }
    assert describe_trigger(decision) == expected


def test_describe_trigger_passes_through_an_unknown_reason():
    assert describe_trigger({"reason": "manual", "tokens": 0}) == "manual"
