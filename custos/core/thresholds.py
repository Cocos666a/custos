"""Threshold evaluation: turning a reading into "act now, or not".

Two ways to express the baseline:

* **absolute** -- ``baseline_tokens`` is a fixed number of tokens. Use this when
  the tool compacts at a known, configured point regardless of model window.
* **percent of window** -- ``baseline_percent`` is applied to the reading's own
  window. Use this when the same session may run against models with different
  windows.

The rotate line is the point at which a session should be handed over; the warn
line is earlier and only produces a message. Both are factors of the baseline,
so raising ``baseline_tokens`` moves both lines together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .usage import Reading

DEFAULT_WARN_FACTOR = 0.90
DEFAULT_ROTATE_FACTOR = 0.93


@dataclass
class Baseline:
    """The resolved baseline and an explanation of where it came from."""

    tokens: int
    source: str
    mode: str

    def describe(self) -> str:
        return "%s tokens (%s, %s)" % (self.tokens, self.source, self.mode)


@dataclass
class Thresholds:
    """The two lines derived from a baseline."""

    warn: int
    rotate: int
    baseline: Baseline
    warn_factor: float = DEFAULT_WARN_FACTOR
    rotate_factor: float = DEFAULT_ROTATE_FACTOR

    def describe(self) -> str:
        return "baseline %s; warn %s; rotate %s" % (
            self.baseline.tokens, self.warn, self.rotate)


def resolve_baseline(config: Any, reading: Optional[Reading] = None) -> Baseline:
    """Resolve the baseline from configuration.

    Percent mode needs a window; when the reading has none, the configured
    absolute value is used as a floor so the tool still does something sensible.
    """
    baseline_tokens = config.get("thresholds", "baseline_tokens", default=None)
    baseline_percent = config.get("thresholds", "baseline_percent", default=None)

    if baseline_percent is not None:
        try:
            percent = float(baseline_percent)
        except (TypeError, ValueError):
            percent = 0.0
        window = reading.window if reading else None
        if percent > 0 and window:
            # A percentage above 1 is read as "90", below 1 as "0.9".
            fraction = percent / 100.0 if percent > 1 else percent
            return Baseline(
                tokens=int(window * fraction),
                source="%g%% of window %d" % (percent, window),
                mode="percent",
            )
        if baseline_tokens:
            return Baseline(
                tokens=int(baseline_tokens),
                source="configured absolute (percent mode had no window)",
                mode="absolute",
            )
        return Baseline(tokens=_fallback_baseline(reading), source="fallback", mode="absolute")

    if baseline_tokens:
        try:
            value = int(baseline_tokens)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return Baseline(tokens=value, source="configured", mode="absolute")

    return Baseline(tokens=_fallback_baseline(reading), source="fallback", mode="absolute")


def _fallback_baseline(reading: Optional[Reading]) -> int:
    """Last resort when nothing is configured: use a share of the window."""
    if reading and reading.window:
        return int(reading.window * 0.75)
    return 200000


def resolve_thresholds(config: Any, reading: Optional[Reading] = None) -> Thresholds:
    """Resolve baseline plus warn/rotate lines for a reading."""
    baseline = resolve_baseline(config, reading)
    warn_factor = _factor(config, "warn_factor", DEFAULT_WARN_FACTOR)
    rotate_factor = _factor(config, "rotate_factor", DEFAULT_ROTATE_FACTOR)
    return Thresholds(
        warn=int(baseline.tokens * warn_factor),
        rotate=int(baseline.tokens * rotate_factor),
        baseline=baseline,
        warn_factor=warn_factor,
        rotate_factor=rotate_factor,
    )


def _factor(config: Any, key: str, default: float) -> float:
    try:
        value = float(config.get("thresholds", key, default=default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def evaluate(
    reading: Optional[Reading],
    thresholds: Thresholds,
    already_seen_compression: bool = False,
) -> Optional[Dict[str, Any]]:
    """Decide whether a reading should trigger an action.

    Returns ``None`` when nothing should happen, otherwise a dictionary with a
    machine-readable ``reason`` and the facts behind it. An already-compacted
    session triggers regardless of the token count: the tool has started
    discarding history, so waiting for a token line would be waiting for the
    thing we are trying to avoid.
    """
    if reading is None:
        return None
    if reading.tokens < 0:
        # Some stores use a negative sentinel for an invalidated session.
        return {
            "reason": "invalid_reading",
            "level": "anomaly",
            "reading": reading,
            "tokens": reading.tokens,
            "thresholds": thresholds,
        }

    if reading.compressed or already_seen_compression:
        return {
            "reason": "compressed_today",
            "level": "rotate",
            "reading": reading,
            "tokens": reading.tokens,
            "thresholds": thresholds,
            "pct_of_baseline": round(
                100.0 * reading.tokens / max(1, thresholds.baseline.tokens), 1),
        }

    if reading.tokens >= thresholds.rotate:
        return {
            "reason": "near_line",
            "level": "rotate",
            "reading": reading,
            "tokens": reading.tokens,
            "thresholds": thresholds,
            "pct_of_baseline": round(
                100.0 * reading.tokens / max(1, thresholds.baseline.tokens), 1),
        }

    if reading.tokens >= thresholds.warn:
        return {
            "reason": "warn_line",
            "level": "warn",
            "reading": reading,
            "tokens": reading.tokens,
            "thresholds": thresholds,
            "pct_of_baseline": round(
                100.0 * reading.tokens / max(1, thresholds.baseline.tokens), 1),
        }

    return None


def describe_trigger(decision: Dict[str, Any]) -> str:
    """Short human phrase for a decision, used in messages and snapshots."""
    reason = decision.get("reason", "unknown")
    tokens = decision.get("tokens")
    thresholds = decision.get("thresholds")
    if reason == "compressed_today":
        return "session already compacted (tokens %s)" % tokens
    if reason == "near_line":
        line = thresholds.rotate if thresholds else "?"
        return "tokens %s reached the rotate line %s" % (tokens, line)
    if reason == "warn_line":
        line = thresholds.warn if thresholds else "?"
        return "tokens %s reached the warn line %s" % (tokens, line)
    if reason == "invalid_reading":
        return "reading %s looks invalid" % tokens
    return reason
