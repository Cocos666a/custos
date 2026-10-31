# Questions

A gate is a small reasoning check run against a freshly started session. The
premise is narrow and worth stating: a session that cannot answer a question it
has not seen before is not a healthy place to continue working. It has either
lost the thread of the conversation, or it is a different model than you think
it is.

The check is only as good as its questions. A question the session has already
answered is worthless, which is why the bank is data you can replace without
touching code.

## Bank format

```yaml
version: 1

defaults:
  prefix: >-
    Do not use any tools. Do not read any files. Answer with the final answer only.

questions:
  - id: consecutive-sum-87          # required, unique, stable
    prompt: >-                      # required, the exact text sent
      Three consecutive integers add up to 87. What is the largest of the three?
    answer: 30                      # required for exact and last_number
    match: last_number              # required: exact | last_number | regex | script
    hint: 28 + 29 + 30 = 87.        # optional, shown to humans, never sent
    prompt_sha256: ""               # optional integrity pin, see below
```

| field | required | notes |
| --- | --- | --- |
| `id` | yes | appears in logs, messages and the ledger; must be unique |
| `prompt` | yes | sent verbatim, prefixed by `prefix` |
| `answer` | for `exact` and `last_number` | the expected value |
| `match` | yes | one of the four graders below |
| `hint` | no | for the person maintaining the bank |
| `pattern` | for `regex` | the regular expression, if it differs from `answer` |
| `script` | for `script` | the Python validator |
| `notes` | no | free text, never sent |
| `prompt_sha256` | no | integrity pin |

JSON works identically and needs no optional dependency; `questions.yaml` and
`questions.json` are interchangeable. A YAML bank requires PyYAML, and the error
message says so rather than failing obscurely.

## The four graders

**`last_number`** -- the last integer appearing anywhere in the reply must equal
`answer`. Position, not pattern: a model that explains its reasoning puts the
conclusion last far more reliably than it uses any particular label. Thousands
separators are folded first, so `1,000` is one number. This is the right grader
for most questions, and the one to reach for by default.

**`exact`** -- the reply, normalised (whitespace collapsed, an `answer:` label
stripped, trailing punctuation dropped, case folded), must equal `answer`. A
multi-line reply passes when its last non-empty line is the answer. Case folding
matters: an answer of `ecbaf` is a reasoning result, not a spelling test.

**`regex`** -- the pattern is searched in the reply; a capture group, when
present, is the extracted value and is compared against `answer` if one is set.
Use this when the answer has fixed formatting, like a `key=value` line.

**`script`** -- a short Python expression or statement, evaluated with `text`
bound to the reply. It must produce a boolean, or a value compared against
`answer`. This is the escape hatch for everything the other three cannot
express. The script sees a small allow-list of builtins (`len`, `set`, `sorted`,
`any`, ...) and the `re` and `json` modules.

> **A `script` grader is code.** It runs in the process that runs `custos`,
> with that user's privileges. The restricted namespace is a guard rail to keep
> a bank entry honest, not a security boundary -- anything that can edit the bank
> can already run code as you. Treat a question bank like a script you are about
> to execute, and only run banks you trust. See `security.md`.

## Validating a bank

```sh
custos questions validate          # human-readable
custos questions validate --json
```

It exits non-zero when anything is wrong. What it catches:

- a duplicate `id`
- an empty or suspiciously short `prompt`
- `match: last_number` with a non-integer `answer`
- `match: exact` with no `answer`
- `match: regex` with a pattern that does not compile
- `match: script` with a script that does not parse
- a `prompt_sha256` that no longer matches the prompt
- a bank of fewer than three questions (a warning, not an error)

That fourth check earns its keep. A YAML folded scalar joins lines with spaces,
so a multi-line validator written with `>-` instead of `|` becomes one invalid
line -- valid YAML, invalid Python, and invisible until a rotation needs it:

```yaml
# wrong: `>-` folds these into one line
script: >-
  words = text.split()
  result = len(words) == 3

# right: `|` keeps the newlines
script: |
  words = text.split()
  result = len(words) == 3
```

## Modes

`gate.mode` (or `custos gate --mode`) chooses what gets asked:

| mode | behaviour |
| --- | --- |
| `random` | one question per fire, chosen randomly |
| `sequence` | the next question in bank order, wrapping around |
| `all` | every question in one prompt; all must pass |

`sequence` keeps a cursor in `state.json` under `gate.sequence`, so successive
rotations walk the bank instead of repeating.

`random` seeds itself from the running attempt total. That is deliberate: a fixed
seed would ask the same question on every rotation, which is worse than
`sequence` because it looks varied and is not. Pass `--seed N` to make one run
reproducible.

`all` is the strictest and the slowest -- one prompt, every answer graded, one
wrong answer fails the fire.

## Integrity pins

`prompt_sha256` is the SHA-256 of the prompt text exactly as it will be sent.
When it is present, the question is refused unless the text hashes to it:

```sh
printf %s 'Three consecutive integers add up to 87. What is the largest of the three?' | shasum -a 256
```

This exists so a bank can be copied, shared or edited with a guarantee that the
question actually asked is the one that was reviewed. Without a pin, editing a
prompt silently changes what is being tested, and a bank that has drifted is
indistinguishable from one that has not.

The pin is opt-in. A refusal is recorded as an event and the question is skipped;
if every question is pinned and every pin fails, the gate is recorded as skipped
rather than run against questions nobody approved.

## Swapping the bank

**1. Copy the example and validate it.**

```sh
mkdir -p ~/.config/custos
cp config/questions.example.yaml ~/.config/custos/questions.yaml
custos questions validate
```

**2. Write questions the session has not seen.** They should be answerable from
knowledge alone, unambiguous, and hard enough that a degraded session gets them
wrong. Arithmetic with a twist, a short string transformation, a small counting
problem. Avoid anything a session might have memorised verbatim from training
data, and avoid anything requiring tools -- the prefix tells it not to use them.

**3. Set the mode and check it end to end.**

```sh
custos questions list
custos gate --tool claude_code --dry-run
custos gate --tool claude_code --attempts 1
```

The last command runs one real question through the tool's one-shot command. If
it comes back `invalid`, the gate is misconfigured rather than the session being
unhealthy -- check that `claude -p` (or the equivalent) works on its own.

## Where the answers go

Attempts are recorded on the rotation event in `state.json`: the verdict, what
was extracted, how long it took, and the head of the raw reply. Streaks are
counted from the tail, so a valid-but-different result stops the count -- a
session that recovers is not reported as a continuing failure.

Consecutive wrong answers raise the degraded alert after
`gate.alert_after_failures`; consecutive unusable answers (timeout, empty output,
a tool that errored) raise the infrastructure alert after
`gate.alert_after_invalid`. They are separate counters because they are separate
problems: one means the session is degraded, the other means the command is
broken, and the advice for each is different.
