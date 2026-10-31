"""Question bank, answer validators, and the gate runner.

A gate is a small reasoning check run against a *newly started* session: if the
model cannot answer a question it has almost certainly seen before, the session
is not healthy and should not be treated as a clean handover target.

A question is a prompt plus a validator. Four validator kinds cover the useful
cases:

``exact``
    The answer must match after normalisation (case, surrounding whitespace and
    a leading ``answer:`` label are ignored).
``last_number``
    The last integer appearing anywhere in the reply must equal the answer.
    Models wrap conclusions in prose, so position beats pattern.
``regex``
    A regular expression with one capture group; the group must match.
``script``
    A short Python expression/statement evaluated against the reply. This is the
    escape hatch for questions the first three cannot express.

The bank itself is data, which is what makes the questions replaceable without
touching code.

Two pieces of state make the check meaningful across scheduled runs:

* a **sequence cursor**, so ``mode: sequence`` walks the bank instead of asking
  the same question forever;
* an **attempt counter**, which seeds the random picker. A fixed seed would make
  ``mode: random`` deterministic, and a deterministic random question is just a
  worse sequence.

An optional ``prompt_sha256`` pins a question's text. It exists so a bank can be
shared, copied or edited with a guarantee that the question actually asked is the
one that was reviewed: a mismatch refuses that question instead of silently
testing something else.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .util import CustosError, have_yaml, log, truncate

MATCH_KINDS = ("exact", "last_number", "regex", "script")
SELECTION_MODES = ("random", "sequence", "all")

DEFAULT_PREFIX = (
    "Do not use any tools. Do not read any files. "
    "Answer with the final answer only."
)

# Stop conditions inside one fire.
DEFAULT_ATTEMPTS = 5
DEFAULT_INTERVAL_SECONDS = 60.0
DEFAULT_ATTEMPT_TIMEOUT = 240.0
DEFAULT_FIRE_BUDGET_SECONDS = 900.0

# A reply is considered "no usable answer" below this length. Distinguishes
# "the tool failed or timed out" from "the tool answered, and the answer was
# wrong" -- which are different problems with different alerts.
MIN_USABLE_REPLY_CHARS = 1

VERDICT_PASS = "pass"
VERDICT_FAIL = "fail"
VERDICT_INVALID = "invalid"

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# Normalisation applied before `exact` comparison.
_ANSWER_LABEL = re.compile(r"^\s*(?:final\s+)?answer\s*[:：]\s*", re.IGNORECASE)

# An integer that is not part of a decimal or a longer number.
#
# The guard has to allow a trailing sentence period ("the answer is 20.") while
# rejecting the digits of a decimal ("1.5"). "Not followed by a dot and a digit"
# expresses exactly that; the earlier "not followed by a dot" rule rejected the
# far more common sentence-final number.
_INTEGER = re.compile(r"(?<![\d.])\d+(?!\.\d)(?!\d)")
# Thousands separators inside a number: "1,000" is one number, not two.
_SEPARATOR = re.compile(r"(?<=\d)[,_](?=\d{3}(?!\d))")

# Names available to a `script` validator. Deliberately tiny: the validator only
# needs the reply text and simple helpers, and a restricted namespace keeps a
# bank entry from reaching anything else.
_SCRIPT_ALLOWED_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "enumerate": enumerate,
    "float": float, "int": int, "len": len, "list": list, "max": max, "min": min,
    "reversed": reversed, "round": round, "set": set, "sorted": sorted,
    "str": str, "sum": sum, "tuple": tuple, "zip": zip,
}


@dataclass
class Question:
    """One entry of the question bank."""

    id: str
    prompt: str
    answer: Any
    match: str = "last_number"
    hint: str = ""
    script: str = ""
    pattern: str = ""
    notes: str = ""
    prompt_sha256: str = ""

    def __post_init__(self) -> None:
        if self.match not in MATCH_KINDS:
            raise CustosError(
                "question %s has unknown match kind %r (expected one of %s)"
                % (self.id, self.match, ", ".join(MATCH_KINDS))
            )
        if self.match == "regex" and not (self.pattern or self.answer):
            raise CustosError(
                "question %s uses match=regex but has no pattern" % self.id)
        if self.match == "script" and not self.script:
            raise CustosError(
                "question %s uses match=script but has no script" % self.id)
        if self.prompt_sha256 and not _SHA256_RE.match(self.prompt_sha256.strip()):
            raise CustosError(
                "question %s has a prompt_sha256 that is not 64 hex characters: %r"
                % (self.id, self.prompt_sha256))

    def check_integrity(self) -> Optional[str]:
        """``None`` when the prompt matches its pin, else the reason it does not.

        A question with no pin always passes: the pin is opt-in, and a bank
        without pins is a bank whose author accepted that risk.
        """
        if not self.prompt_sha256:
            return None
        expected = self.prompt_sha256.strip().lower()
        actual = prompt_digest(self.prompt)
        if actual == expected:
            return None
        return (
            "prompt_sha256 mismatch: bank says %s, the prompt hashes to %s. "
            "The question text changed after it was pinned; re-pin it with "
            "`custos questions validate`." % (expected[:12], actual[:12])
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "answer": self.answer,
            "match": self.match,
            "hint": self.hint,
            "script": self.script,
            "pattern": self.pattern,
            "notes": self.notes,
            "prompt_sha256": self.prompt_sha256,
        }


def prompt_digest(prompt: str) -> str:
    """SHA-256 of the exact bytes sent for a question.

    The prompt is hashed as UTF-8 with no normalisation beyond the strip that
    :func:`load_questions` already applied, so a pin can be reproduced with
    ``printf %s '<prompt>' | shasum -a 256`` and nothing has to be re-derived.
    """
    return hashlib.sha256(str(prompt).encode("utf-8")).hexdigest()


@dataclass
class Verdict:
    """Outcome of validating one reply."""

    verdict: str  # "pass" | "fail"
    extracted: Any = None
    method: str = ""
    detail: str = ""
    failed_reason: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "extracted": self.extracted,
            "method": self.method,
            "detail": self.detail,
            "failed_reason": self.failed_reason,
        }


# ------------------------------------------------------------------ loading


def load_questions(path: str) -> List[Question]:
    """Load a question bank from YAML or JSON.

    Accepts either a bare list or a mapping with a ``questions`` key, so a bank
    can carry metadata next to the questions without a wrapper type.
    """
    if not os.path.exists(path):
        raise CustosError("question bank not found: %s" % path)
    if path.endswith((".yaml", ".yml")):
        payload = _load_yaml(path)
    else:
        try:
            with open(path, "r", encoding="utf-8") as stream:
                payload = json.load(stream)
        except OSError as exc:
            raise CustosError("cannot read %s: %s" % (path, exc))
        except ValueError as exc:
            raise CustosError("cannot parse %s: %s" % (path, exc))

    if isinstance(payload, dict):
        entries = payload.get("questions")
        if entries is None:
            raise CustosError(
                "%s has no `questions` list (or use a top-level list)" % path)
    else:
        entries = payload
    if not isinstance(entries, list):
        raise CustosError("%s: `questions` must be a list" % path)

    questions: List[Question] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise CustosError("%s: question #%d is not a mapping" % (path, index))
        try:
            questions.append(Question(
                id=str(entry.get("id") or "q%d" % (index + 1)),
                prompt=str(entry.get("prompt") or "").strip(),
                answer=entry.get("answer"),
                match=str(entry.get("match") or "last_number"),
                hint=str(entry.get("hint") or ""),
                script=str(entry.get("script") or ""),
                pattern=str(entry.get("pattern") or ""),
                notes=str(entry.get("notes") or ""),
                prompt_sha256=str(entry.get("prompt_sha256") or "").strip(),
            ))
        except CustosError as exc:
            raise CustosError("%s: %s" % (path, exc))
    if not questions:
        raise CustosError("%s contains no questions" % path)
    return questions


def _load_yaml(path: str) -> Any:
    if not have_yaml():
        raise CustosError(
            "%s is YAML but PyYAML is not installed. Install `custos[yaml]` "
            "or use a .json bank." % path)
    import yaml  # type: ignore

    try:
        with open(path, "r", encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    except OSError as exc:
        raise CustosError("cannot read %s: %s" % (path, exc))
    except Exception as exc:
        raise CustosError("cannot parse %s: %s" % (path, exc))


# --------------------------------------------------------------- validators


def normalise(text: Optional[str]) -> str:
    """Normalise a reply for comparison.

    Strips an ``answer:`` label, collapses whitespace, drops trailing
    punctuation, and folds case. Case folding matters because an answer made of
    letters (``ecbaf``) is a reasoning result, not a spelling test.
    """
    if not text:
        return ""
    candidate = " ".join(str(text).split())
    candidate = _ANSWER_LABEL.sub("", candidate)
    return candidate.strip().strip(".").strip().casefold()


def validate_exact(question: Question, text: str) -> Verdict:
    """The normalised reply must equal the normalised expected answer."""
    expected = normalise(str(question.answer))
    actual = normalise(text)
    if not actual:
        return Verdict("fail", None, "exact", failed_reason="empty reply")
    if actual == expected:
        return Verdict("pass", actual, "exact")
    # A reply of several lines passes when its last non-empty line is the answer,
    # which is how models usually sign off.
    lines = [normalise(line) for line in str(text).splitlines() if line.strip()]
    if lines and lines[-1] == expected:
        return Verdict("pass", lines[-1], "exact:last-line")
    return Verdict(
        "fail", actual, "exact",
        failed_reason="expected %r, got %r" % (expected, actual[:80]),
    )


def extract_last_number(text: str) -> Optional[int]:
    """Return the last integer in ``text``, or ``None``.

    "Last" is positional on purpose. Patterns such as a trailing
    ``answer: N`` are not assumed, because a model that explains its reasoning
    puts the conclusion last far more reliably than it uses any given label.
    Thousands separators (``1,000``) are folded into a single number first, so
    a formatted figure is not read as two.
    """
    if not text:
        return None
    cleaned = _SEPARATOR.sub("", str(text))
    matches = _INTEGER.findall(cleaned)
    if not matches:
        return None
    try:
        return int(matches[-1])
    except ValueError:
        return None


def validate_last_number(question: Question, text: str) -> Verdict:
    """The last integer appearing in the reply must equal the answer."""
    expected = question.answer
    try:
        expected_int = int(str(expected).strip())
    except (TypeError, ValueError):
        return Verdict(
            "fail", None, "last_number",
            failed_reason="question answer %r is not an integer" % (expected,),
        )
    found = extract_last_number(text)
    if found is None:
        return Verdict("fail", None, "last_number", failed_reason="no number in reply")
    if found == expected_int:
        return Verdict("pass", found, "last_number")
    return Verdict(
        "fail", found, "last_number",
        failed_reason="expected %d, last number was %d" % (expected_int, found),
    )


def validate_regex(question: Question, text: str) -> Verdict:
    """A regular expression must match; a capture group, when present, is the value."""
    pattern = question.pattern or str(question.answer or "")
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return Verdict(
            "fail", None, "regex",
            failed_reason="invalid pattern %r: %s" % (pattern, exc),
        )
    match = compiled.search(str(text or ""))
    if not match:
        return Verdict(
            "fail", None, "regex",
            failed_reason="pattern %r did not match" % pattern,
        )
    extracted = match.group(1) if match.groups() else match.group(0)
    if question.answer not in (None, "") and match.groups():
        expected = normalise(str(question.answer))
        if normalise(extracted) != expected:
            return Verdict(
                "fail", extracted, "regex",
                failed_reason="captured %r, expected %r" % (extracted, expected),
            )
    return Verdict("pass", extracted, "regex")


def validate_script(question: Question, text: str) -> Verdict:
    """Run a small Python validator against the reply.

    The script sees ``text`` and must produce a boolean, or the expected value.
    It is executed with a restricted namespace: a small allow-list of builtins
    and no import machinery.

    This is a guard rail, not a security boundary -- anything that can edit the
    question bank can already run code as the user. Its purpose is to keep a
    bank entry honest, not to contain an attacker.
    """
    script = question.script.strip()
    if not script:
        return Verdict("fail", None, "script", failed_reason="empty script")

    namespace: Dict[str, Any] = {
        "__builtins__": _SCRIPT_ALLOWED_BUILTINS,
        "re": re,
        "json": json,
        "text": str(text or ""),
        "expected": question.answer,
    }
    try:
        # A bare expression is the common case; fall back to exec for statements.
        try:
            result = eval(compile(script, "<validator>", "eval"), namespace)  # noqa: S307
        except SyntaxError:
            exec(compile(script, "<validator>", "exec"), namespace)  # noqa: S102
            result = namespace.get("result")
    except Exception as exc:  # a validator may raise on malformed input
        return Verdict(
            "fail", None, "script",
            failed_reason="validator raised %s: %s" % (type(exc).__name__, exc),
        )

    if isinstance(result, bool):
        return (
            Verdict("pass", result, "script")
            if result
            else Verdict("fail", result, "script", failed_reason="validator returned False")
        )
    if result is None:
        return Verdict(
            "fail", None, "script",
            failed_reason="validator returned nothing (assign to `result`)",
        )
    if question.answer not in (None, ""):
        if normalise(str(result)) == normalise(str(question.answer)):
            return Verdict("pass", result, "script")
        return Verdict(
            "fail", result, "script",
            failed_reason="returned %r, expected %r" % (result, question.answer),
        )
    return Verdict("pass", result, "script")


VALIDATORS = {
    "exact": validate_exact,
    "last_number": validate_last_number,
    "regex": validate_regex,
    "script": validate_script,
}


def judge(question: Question, text: str) -> Verdict:
    """Validate a reply against one question."""
    validator = VALIDATORS.get(question.match)
    if validator is None:
        return Verdict(
            "fail", None, question.match,
            failed_reason="no validator for match kind %r" % question.match,
        )
    return validator(question, text)


def judge_many(questions: Sequence[Question], text: str) -> List[Verdict]:
    """Validate a reply against several questions (``mode: all``)."""
    return [judge(question, text) for question in questions]


# ---------------------------------------------------------------- selection


def pick_questions(
    questions: Sequence[Question],
    mode: str = "random",
    count: Optional[int] = None,
    seed: Optional[int] = None,
    state: Optional[Dict[str, Any]] = None,
) -> List[Question]:
    """Choose which questions to ask.

    ``random``    -- shuffled, ``count`` of them (default one).
    ``sequence``  -- the next ``count`` questions in bank order, wrapping around;
                     the cursor is read from and written back to ``state`` under
                     ``gate_sequence`` so successive rotations do not repeat.
    ``all``       -- every question, in order.

    ``seed`` makes ``random`` reproducible, which is what the demo and the tests
    use.
    """
    pool = list(questions)
    if not pool:
        return []
    if mode not in SELECTION_MODES:
        raise CustosError(
            "unknown gate mode %r (expected one of %s)" % (mode, ", ".join(SELECTION_MODES)))

    if mode == "all":
        return pool

    if mode == "sequence":
        cursor = 0
        if isinstance(state, dict):
            try:
                cursor = int(state.get("gate_sequence", 0))
            except (TypeError, ValueError):
                cursor = 0
        wanted = count if count and count > 0 else 1
        chosen = [pool[(cursor + offset) % len(pool)] for offset in range(wanted)]
        if isinstance(state, dict):
            state["gate_sequence"] = (cursor + wanted) % len(pool)
        return chosen

    rng = random.Random(seed) if seed is not None else random.Random()
    wanted = count if count and count > 0 else 1
    wanted = min(wanted, len(pool))
    return rng.sample(pool, wanted)


def describe_questions(questions: Sequence[Question]) -> List[str]:
    """Human-readable rows for ``custos questions list``."""
    rows = []
    for question in questions:
        rows.append("%-14s %-12s %s" % (
            question.id, question.match, _shorten(question.prompt, 70)))
    return rows


def _shorten(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def summarise_bank(questions: Sequence[Question]) -> Dict[str, Any]:
    """Counts per match kind, for ``questions list --json``."""
    by_kind: Dict[str, int] = {}
    for question in questions:
        by_kind[question.match] = by_kind.get(question.match, 0) + 1
    return {"total": len(questions), "by_match": by_kind}


# ------------------------------------------------------------------ validate


def validate_bank(path: str, questions: Optional[Sequence[Question]] = None) -> Dict[str, Any]:
    """Check a bank for the mistakes that are cheap to find and costly to miss.

    Loading already rejects the structural errors (unknown match kind, a regex
    with no pattern). This adds the semantic ones: a prompt with no question, an
    answer of the wrong shape for its validator, a regex that does not compile,
    a script that does not parse, and a pin that no longer matches the text.
    Every problem is collected rather than raised on the first, because a person
    fixing a bank wants the whole list.
    """
    problems: List[Dict[str, str]] = []
    try:
        loaded = list(questions) if questions is not None else load_questions(path)
    except CustosError as exc:
        return {
            "path": path,
            "ok": False,
            "total": 0,
            "problems": [{"question": "-", "problem": str(exc), "severity": "error"}],
        }

    seen_ids: Dict[str, int] = {}
    for question in loaded:
        seen_ids[question.id] = seen_ids.get(question.id, 0) + 1
    for question_id, count in sorted(seen_ids.items()):
        if count > 1:
            problems.append({
                "question": question_id,
                "problem": "id appears %d times; ids must be unique" % count,
                "severity": "error",
            })

    for question in loaded:
        if not question.prompt.strip():
            problems.append({
                "question": question.id, "problem": "prompt is empty",
                "severity": "error",
            })
        elif len(question.prompt.strip()) < 8:
            problems.append({
                "question": question.id,
                "problem": "prompt is very short; check it is not truncated",
                "severity": "warning",
            })

        if question.match == "last_number":
            try:
                int(str(question.answer).strip())
            except (TypeError, ValueError):
                problems.append({
                    "question": question.id,
                    "problem": "match=last_number needs an integer answer, got %r"
                               % (question.answer,),
                    "severity": "error",
                })
        elif question.match == "exact":
            if question.answer in (None, ""):
                problems.append({
                    "question": question.id,
                    "problem": "match=exact needs an answer",
                    "severity": "error",
                })
        elif question.match == "regex":
            try:
                re.compile(question.pattern or str(question.answer or ""))
            except re.error as exc:
                problems.append({
                    "question": question.id,
                    "problem": "invalid regex: %s" % exc,
                    "severity": "error",
                })
        elif question.match == "script":
            try:
                compile(question.script, "<validator>", "eval")
            except SyntaxError:
                try:
                    compile(question.script, "<validator>", "exec")
                except SyntaxError as exc:
                    problems.append({
                        "question": question.id,
                        "problem": "validator script does not parse: %s" % exc.msg,
                        "severity": "error",
                    })

        integrity = question.check_integrity()
        if integrity:
            problems.append({
                "question": question.id, "problem": integrity, "severity": "error",
            })

    if len(loaded) < 3:
        problems.append({
            "question": "-",
            "problem": "a bank of %d question(s) leaves little room to vary the "
                       "check; three or more is recommended" % len(loaded),
            "severity": "warning",
        })

    errors = [problem for problem in problems if problem["severity"] == "error"]
    return {
        "path": path,
        "ok": not errors,
        "total": len(loaded),
        "errors": len(errors),
        "warnings": len(problems) - len(errors),
        "summary": summarise_bank(loaded),
        "problems": problems,
    }


# --------------------------------------------------------------------- runner


@dataclass
class GateAttempt:
    """One prompt-and-judge round."""

    attempt: int = 0
    question_id: str = ""
    verdict: str = VERDICT_INVALID
    valid: bool = False
    extracted: Any = None
    method: str = ""
    detail: str = ""
    duration: float = 0.0
    raw_head: str = ""
    stderr_head: str = ""
    timed_out: bool = False
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempt": self.attempt,
            "question_id": self.question_id,
            "verdict": self.verdict,
            "valid": self.valid,
            "extracted": self.extracted,
            "method": self.method,
            "detail": self.detail,
            "duration": round(self.duration, 3),
            "raw_head": self.raw_head,
            "stderr_head": self.stderr_head,
            "timed_out": self.timed_out,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "GateAttempt":
        return cls(
            attempt=int(payload.get("attempt") or 0),
            question_id=str(payload.get("question_id") or ""),
            verdict=str(payload.get("verdict") or VERDICT_INVALID),
            valid=bool(payload.get("valid")),
            extracted=payload.get("extracted"),
            method=str(payload.get("method") or ""),
            detail=str(payload.get("detail") or ""),
            duration=float(payload.get("duration") or 0.0),
            raw_head=str(payload.get("raw_head") or ""),
            stderr_head=str(payload.get("stderr_head") or ""),
            timed_out=bool(payload.get("timed_out")),
            error=str(payload.get("error") or ""),
        )


@dataclass
class GateResult:
    """What one gate fire did."""

    ran: bool = False
    skipped_reason: str = ""
    passed: bool = False
    mode: str = "random"
    prefix: str = ""
    question_ids: List[str] = field(default_factory=list)
    attempts: List[GateAttempt] = field(default_factory=list)
    failures_in_a_row: int = 0
    invalid_in_a_row: int = 0
    integrity_errors: List[str] = field(default_factory=list)
    error: str = ""
    dry_run: bool = False
    plan: Dict[str, Any] = field(default_factory=dict)
    message: str = ""

    @property
    def attempts_total(self) -> int:
        return len(self.attempts)

    def last(self) -> Optional[GateAttempt]:
        return self.attempts[-1] if self.attempts else None

    def summary(self) -> str:
        """One phrase for the rotation message."""
        if self.dry_run:
            return "dry run: planned, not executed"
        if not self.ran:
            return "skipped (%s)" % (self.skipped_reason or "no reason given")
        if self.passed:
            return "passed on attempt %d" % self.attempts_total
        if self.error:
            return "error: %s" % truncate(self.error, 120)
        if self.invalid_in_a_row and not self.failures_in_a_row:
            # Every attempt failed to produce an answer, which is a broken
            # invocation rather than a wrong one. Saying "failed" here would
            # send the reader looking at the model instead of at the command.
            return "no usable answer in %d attempt(s)" % self.attempts_total
        return "failed after %d attempt(s)" % self.attempts_total

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ran": self.ran,
            "skipped_reason": self.skipped_reason,
            "passed": self.passed,
            "mode": self.mode,
            "prefix": self.prefix,
            "question_ids": self.question_ids,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "failures_in_a_row": self.failures_in_a_row,
            "invalid_in_a_row": self.invalid_in_a_row,
            "integrity_errors": self.integrity_errors,
            "error": self.error,
            "dry_run": self.dry_run,
            "plan": self.plan,
            "summary": self.summary(),
        }


def tail_streak(attempts: Sequence[Any], kind: str) -> int:
    """Count consecutive results of one kind, starting from the newest.

    Two kinds are meaningful:

    ``invalid``
        Attempts that produced no usable answer at all (timeout, empty output,
        a tool that errored). These are an infrastructure problem.
    ``fail``
        Attempts that produced an answer, and the answer was wrong. These are
        the intelligence problem this whole feature exists to detect.

    Counting from the tail is what makes the streak mean "still broken". A
    valid-but-different result stops the count, so a session that recovers is
    not reported as a continuing failure even though its history is long.
    """
    streak = 0
    for raw in reversed(list(attempts)):
        attempt = raw if isinstance(raw, GateAttempt) else GateAttempt.from_dict(raw)
        if kind == VERDICT_INVALID:
            if attempt.valid:
                break
            streak += 1
            continue
        if not attempt.valid:
            break
        if attempt.verdict == kind:
            streak += 1
        else:
            break
    return streak


def run_gate(
    adapter: Any,
    questions: Sequence[Question],
    *,
    prefix: str = DEFAULT_PREFIX,
    mode: str = "random",
    attempts: int = DEFAULT_ATTEMPTS,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    attempt_timeout: float = DEFAULT_ATTEMPT_TIMEOUT,
    fire_budget_seconds: float = DEFAULT_FIRE_BUDGET_SECONDS,
    on_attempt: Optional[Callable[[GateAttempt], None]] = None,
    selection_state: Optional[Dict[str, Any]] = None,
    prior_attempts: Sequence[Any] = (),
    seed: Optional[int] = None,
    dry_run: bool = False,
    sleep: Optional[Callable[[float], None]] = None,
) -> GateResult:
    """Ask a freshly started session a question and judge the answer.

    ``selection_state`` is a mapping the caller persists between runs; the
    sequence cursor is read from and written back to it, so ``mode: sequence``
    advances across scheduled runs rather than within one.

    ``prior_attempts`` is the history of earlier fires for the same rotation.
    It is folded in **before** this fire's attempts, which is what lets the
    caller see a five-long failure streak that took five runs to accumulate --
    the retry budget inside one fire (``attempts``) is a separate limit from the
    alert threshold.

    ``sleep`` is injectable so a caller can run the retry loop without waiting.
    """
    result = GateResult(mode=mode, prefix=prefix, dry_run=dry_run)

    if mode not in SELECTION_MODES:
        result.error = "unknown gate mode %r (expected one of %s)" % (
            mode, ", ".join(SELECTION_MODES))
        return result

    if not questions:
        result.skipped_reason = "the question bank is empty"
        return result

    if not _supports_prompts(adapter):
        result.skipped_reason = (
            "%s cannot run a one-shot prompt" % getattr(adapter, "name", "adapter"))
        return result

    # Integrity is checked before selection: a pinned question whose text moved
    # must not be asked at all, because the answer would be graded against a
    # prompt nobody reviewed.
    usable: List[Question] = []
    for question in questions:
        problem = question.check_integrity()
        if problem:
            result.integrity_errors.append("%s: %s" % (question.id, problem))
            log("[custos] refusing question %s: %s" % (question.id, problem))
        else:
            usable.append(question)
    if not usable:
        result.skipped_reason = "every question failed its integrity check"
        result.error = "; ".join(result.integrity_errors)
        return result

    selection = dict(selection_state) if selection_state is not None else {}
    if seed is None:
        seed = _derive_seed(selection)
    chosen = pick_questions(usable, mode=mode, seed=seed, state=selection)
    if selection_state is not None:
        selection_state.clear()
        selection_state.update(selection)

    result.question_ids = [question.id for question in chosen]
    result.plan = {
        "tool": getattr(adapter, "name", "adapter"),
        "command": _command_preview(adapter),
        "mode": mode,
        "questions": result.question_ids,
        "attempts": attempts,
        "interval_seconds": interval_seconds,
        "attempt_timeout": attempt_timeout,
        "fire_budget_seconds": fire_budget_seconds,
        "prefix": prefix,
        "seed": seed,
    }

    if dry_run:
        result.dry_run = True
        return result

    result.ran = True
    deadline = time.time() + max(0.0, fire_budget_seconds)
    history: List[GateAttempt] = [
        attempt if isinstance(attempt, GateAttempt) else GateAttempt.from_dict(attempt)
        for attempt in prior_attempts
    ]
    do_sleep = sleep if sleep is not None else time.sleep

    for index in range(1, max(1, attempts) + 1):
        if time.time() > deadline:
            log("[custos] gate fire budget exhausted after %d attempt(s)"
                % len(result.attempts))
            break
        attempt = run_attempt(
            adapter, chosen, prefix=prefix, timeout=attempt_timeout, index=index)
        result.attempts.append(attempt)
        history.append(attempt)
        if on_attempt is not None:
            try:
                on_attempt(attempt)
            except Exception as exc:  # a caller's bookkeeping must not break the run
                log("[custos] gate on_attempt callback raised: %s" % exc)
        if attempt.valid and attempt.verdict == VERDICT_PASS:
            result.passed = True
            break
        if index < attempts:
            if time.time() + interval_seconds > deadline:
                break
            do_sleep(interval_seconds)

    result.failures_in_a_row = tail_streak(history, VERDICT_FAIL)
    result.invalid_in_a_row = tail_streak(history, VERDICT_INVALID)
    return result


def run_attempt(
    adapter: Any,
    questions: Sequence[Question],
    *,
    prefix: str,
    timeout: float,
    index: int = 1,
) -> GateAttempt:
    """Send the question batch once and judge the reply.

    In ``mode: all`` every question is asked in a single prompt and every answer
    must be right; in the other modes exactly one question is asked, so one
    reply grades against one validator.
    """
    prompt = build_prompt(prefix, questions)
    attempt = GateAttempt(attempt=index, question_id=",".join(q.id for q in questions))
    started = time.time()
    try:
        outcome = adapter.run_prompt(prompt, timeout=timeout)
    except Exception as exc:
        attempt.duration = time.time() - started
        attempt.valid = False
        attempt.verdict = VERDICT_INVALID
        attempt.error = "%s: %s" % (type(exc).__name__, exc)
        return attempt
    attempt.duration = time.time() - started
    attempt.timed_out = bool(getattr(outcome, "timed_out", False))
    text = str(getattr(outcome, "text", "") or "")
    stderr = str(getattr(outcome, "stderr", "") or "")
    rc = int(getattr(outcome, "rc", -1) or 0)
    attempt.raw_head = truncate(text, 300)
    attempt.stderr_head = truncate(stderr, 300)

    if attempt.timed_out:
        attempt.error = "no answer within %gs" % timeout
        attempt.verdict = VERDICT_INVALID
        return attempt
    if len(text.strip()) < MIN_USABLE_REPLY_CHARS:
        attempt.error = "empty reply" + (
            " (exit %s: %s)" % (rc, truncate(stderr, 120)) if stderr else "")
        attempt.verdict = VERDICT_INVALID
        return attempt

    attempt.valid = True
    if len(questions) == 1:
        verdict = judge(questions[0], text)
        attempt.verdict = VERDICT_PASS if verdict.passed else VERDICT_FAIL
        attempt.extracted = verdict.extracted
        attempt.method = verdict.method
        attempt.detail = verdict.detail or verdict.failed_reason
        return attempt

    verdicts = judge_many(questions, text)
    attempt.verdict = VERDICT_PASS if all(v.passed for v in verdicts) else VERDICT_FAIL
    attempt.extracted = [v.extracted for v in verdicts]
    attempt.method = "all"
    failed = [q.id for q, v in zip(questions, verdicts) if not v.passed]
    attempt.detail = "all questions passed" if not failed else (
        "failed: %s" % ", ".join(failed))
    return attempt


def build_prompt(prefix: str, questions: Sequence[Question]) -> str:
    """The exact text sent to the tool.

    The question comes last and the prefix first: an instruction to answer
    without tools has to precede the question, because a model reads forward.
    """
    body = "\n\n".join(question.prompt for question in questions)
    cleaned = (prefix or "").strip()
    return "%s\n\n%s" % (cleaned, body) if cleaned else body


def _supports_prompts(adapter: Any) -> bool:
    """Whether an adapter can run a one-shot prompt, judged by its own report.

    Capabilities are the contract: an adapter that has no working one-shot
    command says so, and the gate is recorded as skipped rather than being run
    and reported as five infrastructure failures.
    """
    if adapter is None:
        return False
    try:
        capabilities = adapter.capabilities() or {}
    except Exception:
        return False
    return bool(capabilities.get("run_prompt"))


def _command_preview(adapter: Any) -> List[str]:
    try:
        return [str(part) for part in adapter.one_shot_command()]
    except Exception:
        return []


def _derive_seed(state: Dict[str, Any]) -> Optional[int]:
    """Seed for the random picker: the running attempt total, or nothing.

    Deliberately not a constant. ``mode: random`` with a fixed seed asks the
    same question every time, which is worse than ``sequence`` because it looks
    varied and is not. Absent a counter the picker falls back to real entropy.
    """
    try:
        total = int(state.get("attempts_total") or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return None
    return total * 2654435761 % (2 ** 31)
