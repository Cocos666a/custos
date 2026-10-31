"""The four graders, bank integrity pins, selection modes and bank validation.

These are the rules that decide whether a freshly started session is healthy, so
they are the part of the tool where a silent change of behaviour would be least
visible: a grader that starts accepting wrong answers reports a degraded session
as fine, and nothing else in the pipeline would notice.
"""

from __future__ import annotations

import json
import os

import pytest

from custos.core.gate import (
    SELECTION_MODES,
    Question,
    describe_questions,
    extract_last_number,
    judge,
    judge_many,
    load_questions,
    normalise,
    pick_questions,
    prompt_digest,
    run_gate,
    summarise_bank,
    tail_streak,
    validate_bank,
    validate_exact,
    validate_last_number,
    validate_regex,
    validate_script,
)
from custos.core.util import CustosError, have_yaml

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


class StubAdapter:
    """An adapter whose replies are decided by the test."""

    name = "stub"

    def __init__(self, replies=(), run_prompt=True) -> None:
        self.replies = list(replies)
        self.prompts = []
        self._index = 0
        self._run_prompt = run_prompt

    def run_prompt(self, prompt, timeout=240.0, cwd=None, extra_args=None):
        self.prompts.append(prompt)
        reply = self.replies[min(self._index, len(self.replies) - 1)] if self.replies else ""
        self._index += 1

        class Outcome:
            pass

        outcome = Outcome()
        if isinstance(reply, tuple):
            outcome.text, outcome.rc = reply[0], reply[1]
            outcome.timed_out = False
            outcome.stderr = reply[2] if len(reply) > 2 else ""
        else:
            outcome.text = reply
            outcome.rc = 0
            outcome.timed_out = False
            outcome.stderr = ""
        return outcome

    def capabilities(self):
        return {"run_prompt": self._run_prompt, "tool": self.name}

    def one_shot_command(self):
        return ["stub", "--print"]


# ------------------------------------------------------------------ loading


def test_load_questions_from_json_fixture():
    questions = load_questions(fixture("bank.json"))
    assert [question.id for question in questions] == [
        "sum-of-two", "count-letters", "reverse-word", "fixed-format", "three-words"]
    assert questions[0].answer == 42
    assert questions[0].match == "last_number"


@pytest.mark.skipif(not have_yaml(), reason="PyYAML is not installed in this interpreter")
def test_yaml_and_json_banks_agree():
    yaml_bank = load_questions(fixture("bank.yaml"))
    json_bank = load_questions(fixture("bank.json"))
    assert [q.to_dict() for q in yaml_bank] == [q.to_dict() for q in json_bank]


def test_load_questions_rejects_missing_file(tmp_path):
    with pytest.raises(CustosError) as excinfo:
        load_questions(str(tmp_path / "absent.json"))
    assert "question bank not found" in str(excinfo.value)


def test_load_questions_rejects_unknown_match_kind(sandbox):
    path = sandbox.write_questions({"questions": [
        {"id": "q", "prompt": "a prompt here", "answer": 1, "match": "vibes"}]})
    with pytest.raises(CustosError) as excinfo:
        load_questions(path)
    assert "unknown match kind" in str(excinfo.value)


def test_load_questions_rejects_empty_bank(sandbox):
    path = sandbox.write_questions({"questions": []})
    with pytest.raises(CustosError) as excinfo:
        load_questions(path)
    assert "contains no questions" in str(excinfo.value)


def test_question_rejects_regex_grader_without_pattern():
    with pytest.raises(CustosError) as excinfo:
        Question(id="q", prompt="a prompt", answer=None, match="regex")
    assert "no pattern" in str(excinfo.value)


# ---------------------------------------------------------------- normalising


@pytest.mark.parametrize("raw,expected", [
    ("  Answer:   ECBaf. ", "ecbaf"),
    ("ANSWER：ecbaf", "ecbaf"),
    ("Final answer: ECBaf", "ecbaf"),
    ("", ""),
])
def test_normalise_strips_labels_whitespace_and_case(raw, expected):
    assert normalise(raw) == expected


# ------------------------------------------------------------------ exact


def test_exact_grader_accepts_normalised_reply():
    question = Question(id="q", prompt="p", answer="ecbaf", match="exact")
    assert validate_exact(question, "  Answer: ECBaf.\n").passed


def test_exact_grader_accepts_answer_on_the_last_line():
    question = Question(id="q", prompt="p", answer="ecbaf", match="exact")
    verdict = validate_exact(question, "Working through it:\n\nso the result is\necbaf")
    assert verdict.passed
    assert verdict.method == "exact:last-line"


def test_exact_grader_rejects_a_wrong_answer():
    question = Question(id="q", prompt="p", answer="ecbaf", match="exact")
    verdict = validate_exact(question, "edcbaf")
    assert not verdict.passed
    assert "expected" in verdict.failed_reason


def test_exact_grader_rejects_an_empty_reply():
    question = Question(id="q", prompt="p", answer="ecbaf", match="exact")
    verdict = validate_exact(question, "   ")
    assert not verdict.passed
    assert verdict.failed_reason == "empty reply"


# ------------------------------------------------------------ last_number


@pytest.mark.parametrize("text,expected", [
    ("The answer is 30.", 30),
    ("28 + 29 + 30 = 87, so 30", 30),
    ("the value is 1,000 in total", 1000),
    ("no digits here", None),
    # Digits inside a decimal are not an integer answer: the guard rejects a
    # digit that follows a dot, so a fractional reply yields nothing rather than
    # a misleading whole number.
    ("a decimal like 1.5 is not an integer", None),
    ("pi is 3.14159", None),
])
def test_extract_last_number_is_positional(text, expected):
    assert extract_last_number(text) == expected


def test_last_number_grader_passes_on_the_trailing_number():
    question = Question(id="q", prompt="p", answer=30, match="last_number")
    assert validate_last_number(question, "The three are 28, 29 and 30.").passed


def test_last_number_grader_fails_when_the_last_number_differs():
    question = Question(id="q", prompt="p", answer=30, match="last_number")
    verdict = validate_last_number(question, "I think it is 30. Final answer: 37")
    assert not verdict.passed
    assert verdict.extracted == 37


def test_last_number_grader_fails_when_the_question_is_malformed():
    question = Question(id="q", prompt="p", answer=None, match="last_number")
    verdict = validate_last_number(question, "30")
    assert not verdict.passed
    assert "not an integer" in verdict.failed_reason


def test_last_number_grader_fails_on_a_reply_with_no_number():
    question = Question(id="q", prompt="p", answer=30, match="last_number")
    verdict = validate_last_number(question, "thirty")
    assert not verdict.passed
    assert verdict.failed_reason == "no number in reply"


# ------------------------------------------------------------------ regex


def test_regex_grader_extracts_the_capture_group():
    question = Question(id="q", prompt="p", answer=None, match="regex",
                        pattern=r"^status=(OK) retries=(\d+)$")
    verdict = validate_regex(question, "status=OK retries=0")
    assert verdict.passed
    assert verdict.extracted == "OK"


def test_regex_grader_compares_capture_group_against_the_answer():
    question = Question(id="q", prompt="p", answer="7", match="regex",
                        pattern=r"retries=(\d+)")
    assert not validate_regex(question, "retries=3").passed
    assert validate_regex(question, "retries=7").passed


def test_regex_grader_reports_an_invalid_pattern():
    question = Question(id="q", prompt="p", answer=None, match="regex",
                        pattern="(unclosed")
    verdict = validate_regex(question, "anything")
    assert not verdict.passed
    assert "invalid pattern" in verdict.failed_reason


# ----------------------------------------------------------------- script


def test_script_grader_evaluates_a_bare_expression():
    question = Question(id="q", prompt="p", answer=None, match="script",
                        script="len(text.split()) == 3")
    assert validate_script(question, "one two three").passed
    assert not validate_script(question, "one two").passed


def test_script_grader_reads_the_result_binding_from_statements():
    question = Question(id="q", prompt="p", answer=None, match="script",
                        script="words = text.split()\nresult = len(words) == 2")
    assert validate_script(question, "two words").passed


def test_script_grader_compares_a_value_against_the_answer():
    question = Question(id="q", prompt="p", answer=3, match="script",
                        script="len(text.split())")
    assert validate_script(question, "one two three").passed
    assert not validate_script(question, "one two").passed


def test_script_grader_reports_a_validator_that_raises():
    question = Question(id="q", prompt="p", answer=None, match="script",
                        script="int(text) + 1")
    verdict = validate_script(question, "not a number")
    assert not verdict.passed
    assert "ValueError" in verdict.failed_reason


def test_script_grader_has_no_open_builtins():
    """The restricted namespace is a guard rail, and it has to actually restrict."""
    question = Question(id="q", prompt="p", answer=None, match="script",
                        script="__import__('os').getcwd()")
    verdict = validate_script(question, "anything")
    assert not verdict.passed
    assert "NameError" in verdict.failed_reason


# ------------------------------------------------------------------ judging


def test_judge_dispatches_on_the_match_kind():
    question = Question(id="q", prompt="p", answer=42, match="last_number")
    assert judge(question, "the answer is 42.").passed


def test_judge_reports_an_unknown_kind_rather_than_raising():
    question = Question.__new__(Question)
    question.id, question.match, question.answer = "q", "telepathy", None
    verdict = judge(question, "42")
    assert not verdict.passed
    assert "no validator" in verdict.failed_reason


def test_judge_many_requires_every_answer():
    questions = [
        Question(id="a", prompt="p", answer=42, match="last_number"),
        Question(id="b", prompt="p", answer=9, match="last_number"),
    ]
    verdicts = judge_many(questions, "42 then 9")
    # `last_number` grades the last number in the reply, so two questions asked
    # in one prompt both read the same trailing value. That is exactly why
    # `mode: all` uses a single answer at the end rather than per-question text.
    assert [verdict.passed for verdict in verdicts] == [False, True]
    assert verdicts[0].extracted == 9


# ---------------------------------------------------------------- integrity


def test_prompt_digest_is_reproducible_and_pins_the_text():
    question = Question(id="q", prompt="What is 6 times 7?", answer=42,
                        match="last_number")
    question.prompt_sha256 = prompt_digest(question.prompt)
    assert question.check_integrity() is None

    question.prompt = "What is 6 times 8?"
    problem = question.check_integrity()
    assert problem is not None
    assert "prompt_sha256 mismatch" in problem


def test_question_without_a_pin_always_passes_integrity():
    question = Question(id="q", prompt="anything", answer=1, match="last_number")
    assert question.check_integrity() is None


def test_question_rejects_a_malformed_pin():
    with pytest.raises(CustosError) as excinfo:
        Question(id="q", prompt="p", answer=1, match="last_number",
                 prompt_sha256="not-a-digest")
    assert "64 hex characters" in str(excinfo.value)


def test_run_gate_refuses_a_question_whose_prompt_moved(sandbox):
    """A pinned question whose text changed must not be asked at all."""
    adapter = StubAdapter(["42"])
    question = Question(id="pinned", prompt="What is 6 times 7?", answer=42,
                        match="last_number")
    question.prompt_sha256 = prompt_digest("a different prompt")
    result = run_gate(adapter, [question], attempts=1)
    assert not result.ran
    assert result.skipped_reason == "every question failed its integrity check"
    assert adapter.prompts == []
    assert any("pinned" in problem for problem in result.integrity_errors)


def test_run_gate_asks_the_remaining_questions_when_one_fails_its_pin():
    good = Question(id="good", prompt="What is 6 times 7?", answer=42,
                    match="last_number")
    bad = Question(id="bad", prompt="What is 6 times 7?", answer=42,
                   match="last_number", prompt_sha256=prompt_digest("moved"))
    adapter = StubAdapter(["42"])
    result = run_gate(adapter, [good, bad], attempts=1)
    assert result.passed
    assert result.question_ids == ["good"]
    assert len(result.integrity_errors) == 1


# ---------------------------------------------------------------- selection


def test_pick_questions_random_is_reproducible_with_a_seed(sandbox):
    questions = load_questions(fixture("bank.json"))
    first = pick_questions(questions, mode="random", seed=99)
    second = pick_questions(questions, mode="random", seed=99)
    assert [q.id for q in first] == [q.id for q in second]
    assert len(first) == 1


def test_pick_questions_sequence_walks_the_bank_and_advances_the_cursor():
    questions = load_questions(fixture("bank.json"))
    state = {}
    seen = [pick_questions(questions, mode="sequence", state=state)[0].id
            for _ in range(3)]
    assert seen == ["sum-of-two", "count-letters", "reverse-word"]
    assert state["gate_sequence"] == 3


def test_pick_questions_sequence_wraps_around():
    questions = load_questions(fixture("bank.json"))
    state = {"gate_sequence": len(questions) - 1}
    chosen = pick_questions(questions, mode="sequence", state=state)
    assert chosen[0].id == questions[-1].id
    assert state["gate_sequence"] == 0


def test_pick_questions_all_returns_every_question_in_order():
    questions = load_questions(fixture("bank.json"))
    assert pick_questions(questions, mode="all") == questions


def test_pick_questions_rejects_an_unknown_mode():
    questions = load_questions(fixture("bank.json"))
    with pytest.raises(CustosError) as excinfo:
        pick_questions(questions, mode="roulette")
    assert "unknown gate mode" in str(excinfo.value)


def test_pick_questions_on_an_empty_bank_returns_nothing():
    assert pick_questions([], mode="all") == []


def test_selection_modes_constant_matches_the_documented_set():
    assert set(SELECTION_MODES) == {"random", "sequence", "all"}


# ------------------------------------------------------------------- runner


def test_run_gate_retries_until_the_answer_is_right():
    # `sum-of-two` is the first question in bank order and its answer is 42.
    questions = load_questions(fixture("bank.json"))
    adapter = StubAdapter(["wrong, the answer is 37", "the answer is 42"])
    result = run_gate(adapter, questions, mode="sequence", attempts=5,
                      interval_seconds=0, sleep=lambda _seconds: None)
    assert result.passed
    assert result.attempts_total == 2
    assert result.failures_in_a_row == 0
    assert len(adapter.prompts) == 2


def test_run_gate_sends_the_prefix_before_the_question():
    question = Question(id="q", prompt="What is 6 times 7?", answer=42,
                        match="last_number")
    adapter = StubAdapter(["42"])
    run_gate(adapter, [question], attempts=1, prefix="PREFIX-TEXT")
    assert adapter.prompts[0].startswith("PREFIX-TEXT")
    assert adapter.prompts[0].endswith("What is 6 times 7?")


def test_run_gate_records_a_streak_of_wrong_answers():
    question = Question(id="q", prompt="What is 6 times 7?", answer=42,
                        match="last_number")
    adapter = StubAdapter(["41"])
    result = run_gate(adapter, [question], attempts=3, interval_seconds=0,
                      sleep=lambda _seconds: None)
    assert not result.passed
    assert result.failures_in_a_row == 3
    assert result.invalid_in_a_row == 0


def test_run_gate_counts_unusable_answers_separately():
    question = Question(id="q", prompt="What is 6 times 7?", answer=42,
                        match="last_number")
    adapter = StubAdapter([("", 1, "the tool exploded")])
    result = run_gate(adapter, [question], attempts=2, interval_seconds=0,
                      sleep=lambda _seconds: None)
    assert not result.passed
    assert result.invalid_in_a_row == 2
    assert result.failures_in_a_row == 0
    assert "no usable answer" in result.summary()


def test_run_gate_treats_a_timeout_as_invalid():
    question = Question(id="q", prompt="What is 6 times 7?", answer=42,
                        match="last_number")

    class TimeoutAdapter(StubAdapter):
        def run_prompt(self, prompt, timeout=240.0, cwd=None, extra_args=None):
            self.prompts.append(prompt)

            class Outcome:
                text = ""
                rc = -1
                timed_out = True
                stderr = ""

            return Outcome()

    result = run_gate(TimeoutAdapter(), [question], attempts=1,
                      attempt_timeout=1.0, sleep=lambda _seconds: None)
    assert not result.passed
    assert result.attempts[0].timed_out
    assert "no answer within" in result.attempts[0].error


def test_run_gate_skips_an_adapter_that_cannot_run_prompts():
    question = Question(id="q", prompt="What is 6 times 7?", answer=42,
                        match="last_number")
    result = run_gate(StubAdapter(["42"], run_prompt=False), [question])
    assert not result.ran
    assert "cannot run a one-shot prompt" in result.skipped_reason


def test_run_gate_dry_run_plans_without_asking():
    question = Question(id="q", prompt="What is 6 times 7?", answer=42,
                        match="last_number")
    adapter = StubAdapter(["42"])
    result = run_gate(adapter, [question], dry_run=True, seed=3)
    assert result.dry_run and not result.ran
    assert adapter.prompts == []
    assert result.plan["command"] == ["stub", "--print"]
    assert result.question_ids == ["q"]
    assert result.summary() == "dry run: planned, not executed"


def test_run_gate_mode_all_grades_every_question_in_one_prompt():
    # One reply, one trailing number: both questions are asked in the same
    # prompt, so both are graded against the last number in the answer.
    questions = [
        Question(id="a", prompt="first question", answer=42, match="last_number"),
        Question(id="b", prompt="second question", answer=42, match="last_number"),
    ]
    adapter = StubAdapter(["the answer is 42."])
    result = run_gate(adapter, questions, mode="all", attempts=1)
    assert result.passed
    assert len(adapter.prompts) == 1
    assert result.question_ids == ["a", "b"]
    assert adapter.prompts[0].count("first question") == 1
    assert adapter.prompts[0].count("second question") == 1


def test_run_gate_mode_all_fails_when_one_answer_is_wrong():
    questions = [
        Question(id="a", prompt="first question", answer=42, match="last_number"),
        Question(id="b", prompt="second question", answer=9, match="last_number"),
    ]
    result = run_gate(StubAdapter(["the first one is 8, and the total is 42"]),
                      questions, mode="all", attempts=1)
    assert not result.passed
    assert "failed: b" in result.attempts[0].detail
    assert result.attempts[0].extracted == [42, 42]


def test_run_gate_rejects_an_unknown_mode():
    question = Question(id="q", prompt="p", answer=1, match="last_number")
    result = run_gate(StubAdapter(["1"]), [question], mode="coin-flip")
    assert result.error
    assert "unknown gate mode" in result.error


def test_run_gate_folds_prior_attempts_into_the_streak():
    question = Question(id="q", prompt="What is 6 times 7?", answer=42,
                        match="last_number")
    history = [{"attempt": 1, "verdict": "fail", "valid": True},
               {"attempt": 2, "verdict": "fail", "valid": True}]
    result = run_gate(StubAdapter(["41"]), [question], attempts=1,
                      prior_attempts=history, sleep=lambda _seconds: None)
    assert result.failures_in_a_row == 3
    assert len(result.attempts) == 1


def test_tail_streak_stops_at_the_first_different_result():
    attempts = [
        {"attempt": 1, "verdict": "fail", "valid": True},
        {"attempt": 2, "verdict": "pass", "valid": True},
        {"attempt": 3, "verdict": "fail", "valid": True},
        {"attempt": 4, "verdict": "fail", "valid": True},
    ]
    assert tail_streak(attempts, "fail") == 2
    assert tail_streak(attempts, "invalid") == 0
    assert tail_streak(attempts, "pass") == 0


def test_tail_streak_of_invalid_stops_at_a_valid_attempt():
    attempts = [
        {"attempt": 1, "verdict": "invalid", "valid": False},
        {"attempt": 2, "verdict": "fail", "valid": True},
        {"attempt": 3, "verdict": "invalid", "valid": False},
    ]
    assert tail_streak(attempts, "invalid") == 1
    assert tail_streak(attempts, "fail") == 0


def test_run_gate_selection_state_carries_the_sequence_cursor():
    questions = load_questions(fixture("bank.json"))
    state = {}
    for _ in range(3):
        run_gate(StubAdapter(["42"]), questions, mode="sequence", attempts=1,
                 selection_state=state, sleep=lambda _seconds: None)
    assert state["gate_sequence"] == 3


# ----------------------------------------------------------------- validate


def test_validate_bank_accepts_the_shipped_fixture_shape(tmp_path, sandbox):
    report = validate_bank(fixture("bank.json"))
    assert report["ok"], report["problems"]
    assert report["total"] == 5
    assert report["summary"]["by_match"]["last_number"] == 2


def test_validate_bank_reports_duplicate_ids(sandbox):
    path = sandbox.write_questions({"questions": [
        {"id": "same", "prompt": "a long enough prompt", "answer": 1,
         "match": "last_number"},
        {"id": "same", "prompt": "another long enough prompt", "answer": 2,
         "match": "last_number"},
        {"id": "third", "prompt": "a third long enough prompt", "answer": 3,
         "match": "last_number"},
    ]})
    report = validate_bank(path)
    assert not report["ok"]
    assert any("appears 2 times" in problem["problem"] for problem in report["problems"])


def test_validate_bank_reports_a_wrong_shaped_answer(sandbox):
    path = sandbox.write_questions({"questions": [
        {"id": "a", "prompt": "a long enough prompt", "answer": "thirty",
         "match": "last_number"},
        {"id": "b", "prompt": "another long enough prompt", "answer": None,
         "match": "exact"},
        {"id": "c", "prompt": "a third long enough prompt", "answer": None,
         "match": "regex", "pattern": "(unclosed"},
        {"id": "d", "prompt": "a fourth long enough prompt", "answer": None,
         "match": "script", "script": "this is not python("},
    ]})
    report = validate_bank(path)
    problems = " | ".join(problem["problem"] for problem in report["problems"])
    assert "needs an integer answer" in problems
    assert "needs an answer" in problems
    assert "invalid regex" in problems
    assert "does not parse" in problems
    assert report["errors"] >= 4


def test_validate_bank_reports_a_stale_pin(sandbox):
    path = sandbox.write_questions({"questions": [
        {"id": "a", "prompt": "a long enough prompt", "answer": 1,
         "match": "last_number", "prompt_sha256": "0" * 64}]})
    report = validate_bank(path)
    assert not report["ok"]
    assert any("prompt_sha256 mismatch" in problem["problem"]
               for problem in report["problems"])


def test_validate_bank_warns_on_a_bank_that_is_too_small(sandbox):
    path = sandbox.write_questions({"questions": [
        {"id": "only", "prompt": "a long enough prompt", "answer": 1,
         "match": "last_number"}]})
    report = validate_bank(path)
    assert report["ok"]
    assert report["warnings"] == 1
    assert "three or more is recommended" in report["problems"][0]["problem"]


def test_validate_bank_reports_an_unreadable_bank_rather_than_raising(tmp_path):
    report = validate_bank(str(tmp_path / "absent.json"))
    assert report["ok"] is False
    assert report["total"] == 0
    assert "question bank not found" in report["problems"][0]["problem"]


# ------------------------------------------------------------------ helpers


def test_describe_questions_shortens_long_prompts():
    question = Question(id="q", prompt="x" * 200, answer=1, match="last_number")
    row = describe_questions([question])[0]
    assert row.startswith("q ")
    assert len(row) < 120
    assert row.endswith("...")


def test_summarise_bank_counts_by_match_kind():
    questions = load_questions(fixture("bank.json"))
    summary = summarise_bank(questions)
    assert summary["total"] == 5
    assert summary["by_match"] == {
        "last_number": 2, "exact": 1, "regex": 1, "script": 1}
