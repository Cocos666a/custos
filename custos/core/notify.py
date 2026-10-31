"""Event message formatting and delivery.

Two rules shape this module.

**Silence is the default.** Nothing is printed when nothing happened. A
scheduled run that emits an empty stdout is the expected outcome, which is what
lets cron mail, a terminal, or a chat bridge treat output as "look at me".

**A message is data before it is text.** Every message is built as a dictionary
with a ``kind``, a one-line ``summary`` and a multi-line ``text``; the text is
what a human reads and the fields are what a script filters on. Delivery then
has two sinks: stdout (default) and an optional webhook, which is opt-in
because it is the only thing in this tool that talks to the network.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Union

from .thresholds import describe_trigger
from .usage import Reading
from .util import format_age, log, truncate

# Message kinds. Kept short and stable: they end up in JSON output and in
# whatever a user filters on.
KIND_WARN = "warn"
KIND_ROTATION = "rotation"
KIND_GATE = "gate"
KIND_ALERT_DEGRADED = "alert:degraded"
KIND_ALERT_INFRA = "alert:infra"
KIND_ALERT_INTERNAL = "alert:internal"
KIND_ERROR = "error"

WEBHOOK_PAYLOAD_TOOL = "custos"
DEFAULT_WEBHOOK_TIMEOUT = 10.0


def make_message(kind: str, text: str, summary: str = "", **fields: Any) -> Dict[str, Any]:
    """Build one message object."""
    message: Dict[str, Any] = {
        "kind": kind,
        "summary": summary or _first_line(text),
        "text": text,
        "ts": round(time.time(), 3),
    }
    message.update(fields)
    return message


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return truncate(stripped, 160)
    return ""


# --------------------------------------------------------------- formatting


def format_reading(reading: Reading) -> str:
    """One-line reading summary."""
    bits = ["%s %s" % (reading.tool, reading.session[:12] or "-")]
    bits.append("%s tokens" % reading.tokens)
    if reading.pct is not None:
        bits.append("%.1f%% of window" % reading.pct)
    bits.append("read %s" % format_age(reading.ts))
    return ", ".join(bits)


def format_warning(reading: Reading, decision: Dict[str, Any]) -> str:
    """Message for a warning-level decision."""
    thresholds = decision.get("thresholds")
    lines = [
        "[custos] session approaching its context limit",
        "  %s" % format_reading(reading),
        "  trigger: %s" % describe_trigger(decision),
    ]
    if thresholds is not None:
        lines.append("  lines: warn %s / rotate %s (baseline %s)" % (
            thresholds.warn, thresholds.rotate, thresholds.baseline.tokens))
    lines.append("  a snapshot will be written when the rotate line is reached")
    return "\n".join(lines)


def format_rotation(
    reading: Reading,
    decision: Dict[str, Any],
    handover_path: Optional[str],
    rotated: bool,
    new_session: Optional[str] = None,
    mode: str = "assisted",
    start_hint: str = "",
    gate: str = "",
) -> str:
    """Message describing a completed or assisted rotation.

    ``start_hint`` is the adapter's own instruction for starting a replacement
    session. It is the difference between "a rotation happened" and "here is
    what to type", so it is quoted verbatim rather than paraphrased.
    """
    lines = [
        "[custos] session rotated" if rotated else "[custos] rotation required",
        "  old session: %s" % format_reading(reading),
        "  trigger: %s" % describe_trigger(decision),
        "  mode: %s" % mode,
    ]
    if handover_path:
        lines.append("  handover: %s" % handover_path)
    else:
        lines.append("  handover: NOT WRITTEN")
    if new_session:
        lines.append("  new session: %s" % new_session)
    elif not rotated:
        lines.append("  next: start a new session and read the handover file above")
    if start_hint:
        lines.append("  start it with: %s" % truncate(start_hint, 300))
    if gate:
        lines.append("  gate: %s" % gate)
    return "\n".join(lines)


def format_gate_result(
    question_id: str,
    verdict: str,
    extracted: Any,
    attempt: int,
    detail: Optional[str] = None,
) -> str:
    """Message reporting one gate attempt."""
    lines = [
        "[custos] gate %s (attempt %d)" % (verdict, attempt),
        "  question: %s" % question_id,
        "  extracted: %s" % (extracted if extracted is not None else "nothing"),
    ]
    if detail:
        lines.append("  detail: %s" % truncate(detail, 200))
    return "\n".join(lines)


def format_degraded_alert(
    reading: Reading,
    streak: int,
    attempts_total: int,
    last_raw: str,
    handover_path: Optional[str] = None,
    retry_seconds: int = 120,
) -> str:
    """Alert: the replacement session keeps failing the intelligence check."""
    lines = [
        "[custos] gate alert: the new session failed the check %d times in a row" % streak,
        "  session: %s" % format_reading(reading),
        "  attempts: %d in a row, %d in total for this rotation" % (streak, attempts_total),
        "  last reply: %s" % (truncate(last_raw, 200) or "(empty)"),
    ]
    if handover_path:
        lines.append("  handover: %s" % handover_path)
    lines.append("  next: %s" % _retry_sentence(retry_seconds))
    return "\n".join(lines)


def _retry_sentence(retry_seconds: int) -> str:
    """What happens next, worded for whatever the retry gap actually is."""
    if retry_seconds <= 0:
        return ("it is retried on every scheduled run until it recovers; a "
                "recovery is reported on its own, so no action is needed.")
    return ("it is retried automatically at least %ds from now; a recovery is "
            "reported on its own, so no action is needed." % retry_seconds)


def format_infra_alert(
    reading: Reading,
    streak: int,
    detail: str,
    retry_seconds: int = 120,
) -> str:
    """Alert: the gate could not get a usable answer at all."""
    lines = [
        "[custos] gate alert: %d calls in a row produced no usable answer" % streak,
        "  session: %s" % format_reading(reading),
        "  last error: %s" % (truncate(detail, 200) or "(nothing reported)"),
        "  next: %s" % _retry_sentence(retry_seconds),
        "  if this keeps up, check that the tool's one-shot command works: "
        "`--print`/`exec`/`run` may need a flag or a login.",
    ]
    return "\n".join(lines)


def format_internal_alert(detail: str) -> str:
    """Alert: the tool itself raised something it did not expect."""
    return (
        "[custos] internal error\n"
        "  %s\n"
        "  next: the run ended without touching any session. Re-run with "
        "--json for the full record, or open an issue with the message above."
        % truncate(detail, 400)
    )


def format_error(tool: str, reason: str) -> str:
    """Message for a read or adapter failure."""
    return "[custos] %s unavailable\n  %s" % (tool, truncate(reason, 300))


def format_deferred(tool: str, session: str, reasons: List[str]) -> str:
    """Message explaining why a rotation was postponed."""
    return "[custos] rotation deferred for %s %s\n  %s" % (
        tool, session[:12] or "-", "; ".join(reasons))


def format_scan_row(reading: Reading, updated: str) -> List[str]:
    """Table cells for one reading, used by the CLI."""
    return [
        reading.tool,
        reading.session[:16] or "-",
        str(reading.tokens),
        str(reading.window) if reading.window else "-",
        ("%.1f%%" % reading.pct) if reading.pct is not None else "-",
        reading.source,
        updated,
    ]


# ---------------------------------------------------------------- delivery


def message_text(message: Union[str, Dict[str, Any]]) -> str:
    """The printable text of a message, whichever form it is in."""
    if isinstance(message, dict):
        return str(message.get("text") or "")
    return str(message or "")


def message_kind(message: Union[str, Dict[str, Any]]) -> str:
    if isinstance(message, dict):
        return str(message.get("kind") or "")
    return ""


def deliver(
    messages: Union[str, Dict[str, Any], Sequence[Any], None],
    config: Any,
    dry_run: bool = False,
    sink: Optional[List[str]] = None,
) -> bool:
    """Deliver one message or a list of them.

    Returns ``True`` when anything was written to stdout. Empty messages are
    never delivered, which is what keeps a scheduled run silent when nothing
    happened.

    ``dry_run`` suppresses the **webhook** and nothing else. A dry run must not
    have effects outside the machine, and a POST is an effect. Printing is not:
    it is how the caller finds out what the dry run decided, and a dry run that
    printed nothing would be indistinguishable from one that crashed.
    """
    batch = _normalise(messages)
    if not batch:
        return False

    if sink is not None:
        for message in batch:
            sink.append(message_text(message))

    printed = False
    if config is None or config.get("notify", "stdout", default=True):
        for message in batch:
            text = message_text(message).rstrip()
            if text:
                print(text)
                printed = True

    if not dry_run:
        _deliver_webhook(batch, config)
    return printed


def _normalise(messages: Any) -> List[Any]:
    if messages is None:
        return []
    if isinstance(messages, (str, dict)):
        candidates = [messages]
    else:
        candidates = list(messages)
    return [message for message in candidates if message_text(message).strip()]


def _deliver_webhook(batch: List[Any], config: Any) -> None:
    """POST the batch to the configured webhook, once, without ever failing.

    A webhook is a notification channel, not a source of truth: if it is down,
    the event is still in ``events.jsonl`` and the message still went to stdout.
    Raising here would turn a notification problem into a rotation problem.
    """
    if config is None:
        return
    url = config.get("notify", "webhook_url", default=None)
    if not url:
        return

    timeout = config.get("notify", "webhook_timeout_seconds", default=DEFAULT_WEBHOOK_TIMEOUT)
    try:
        timeout = float(timeout)
    except (TypeError, ValueError):
        timeout = DEFAULT_WEBHOOK_TIMEOUT
    if timeout <= 0:
        timeout = DEFAULT_WEBHOOK_TIMEOUT

    payload = json.dumps({
        "tool": WEBHOOK_PAYLOAD_TOOL,
        "ts": round(time.time(), 3),
        "messages": [_jsonable(message) for message in batch],
    }).encode("utf-8")

    request = urllib.request.Request(
        str(url),
        data=payload,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "%s/%s" % (WEBHOOK_PAYLOAD_TOOL, _version()),
        },
        method="POST",
    )

    # One retry. Two attempts distinguish "the network hiccuped" from "the
    # endpoint is not there"; more than two just delays an unattended run.
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read(1)
            return
        except (urllib.error.URLError, OSError, ValueError) as exc:
            if attempt == 2:
                log("[custos] webhook delivery failed (%s); message kept in "
                    "stdout and events.jsonl" % truncate(str(exc), 200))
            else:
                time.sleep(1.0)


def _jsonable(message: Any) -> Dict[str, Any]:
    if isinstance(message, dict):
        return message
    return make_message(KIND_WARN, str(message))


def _version() -> str:
    from .. import __version__

    return __version__
