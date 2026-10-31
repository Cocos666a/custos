# custos

Automatic context-window monitoring and session rotation for AI coding agents.

**English** · [简体中文](README.zh-CN.md)

`custos` watches how full an agent's context window is, writes a handover
snapshot before the window degrades, and can start a fresh session so long
running work continues without hitting a lossy automatic compaction. It ships
adapters for Claude Code, Codex, OpenCode and Pi, plus a configuration-driven
adapter for any other tool that can expose its token usage as JSON. The name is
Latin for *guardian*: it keeps watch over a session's context.

> **Status: 0.1.0, unreleased.** Everything described here is implemented and
> exercised against real local sessions: the usage engine, thresholds, handover
> schema, monitor loop, rotation, gate runner, installer and question bank. The
> test suite runs on Python 3.9 and 3.13. Nothing is published yet; see
> `CHANGELOG.md`.

Nothing runs an agent unless you ask it to. Readings come from files the tools
already write, opened read-only. The only command that starts a process is the
gate, and it runs the one-shot prompt you configured.

## The problem

A long session does not fail; it fades. The agent starts forgetting what it just
did, re-reading files it already read, and contradicting decisions from an hour
ago. By the time you notice, the reliable context is already gone -- and the
automatic compaction that finally fires is exactly the thing that discards it.

Two things are missing in practice:

1. **A number.** How full is the window right now, per session, without asking
   the agent to introspect?
2. **A handover.** When the window must be replaced, what does the new session
   read to pick up the work?

`custos` provides both, and stays out of the way otherwise: no messages when
nothing crossed a line, no daemon of its own, no dependency you did not already
have.

## Features

- **Usage per session, without introspection.** One row per session: tokens,
  window, percent and the source of the number, read from files the agent
  already writes.
- **Two thresholds.** A warn line and a rotate line, each an absolute token
  count or a share of the model's window.
- **Handover before rotation.** A markdown snapshot of where the work stood, the
  decisions, the open questions, recent excerpts and the paths that matter,
  written before the rotation message so the message always names a file that
  exists.
- **Interlocked rotation.** An idle check, an idempotency ledger and a
  single-instance lock keep it from rotating a session that is busy or already
  handled.
- **A gate for the new session.** One configured one-shot prompt, graded by
  `exact`, `last_number`, `regex` or `script`, with failures counted across
  scheduled runs and a degradation alert when the new session keeps failing.
- **Push mode.** `custos report` accepts a reading from any tool that already
  knows its own token count.
- **Quiet by default.** No output when nothing crossed a line, no daemon, no
  runtime dependencies, `--json` accepted anywhere on the command line and
  `--dry-run` on every mutating command.

## Requirements

- Python 3.9 or newer (3.9 is the tested floor).
- macOS or Linux. The tool uses `fcntl` locks and process groups; see
  `docs/faq.md` for the Windows position.
- No runtime dependencies. PyYAML is optional and only needed if you prefer a
  `config.yaml`; JSON configs work everywhere.

## Install

```sh
git clone https://github.com/Cocos666a/custos.git && cd custos
bash install.sh                 # ~/.local by default
```

| flag | effect |
| --- | --- |
| `--prefix DIR` | install under DIR instead of `~/.local` |
| `--yes` | do not prompt (required with no terminal) |
| `--dry-run` | print what would happen, change nothing |
| `--no-daemon` | skip the scheduler templates |
| `--with-skills` | also copy `skills/` into `~/.claude/skills/` when it exists |

The installer writes the program to `PREFIX/share/custos`, a `custos`
wrapper into `PREFIX/bin`, and prints the scheduler snippet for your platform.
`bash uninstall.sh` reverses it.

It seeds a configuration for the interpreter it installed against: with PyYAML
available that is `config.yaml` and `questions.yaml`; without it -- a stock
`python3` -- `config.json` and `questions.json` with the same settings, because
the tool refuses to start on a YAML file it cannot read. The commented original
is always `config/config.example.yaml`.

Or run it from a checkout without installing anything:

```sh
python3 -m custos --help
```

## Quickstart

```sh
# 1. a configuration directory (CUSTOS_CONFIG_DIR, else ~/.config/custos)
mkdir -p ~/.config/custos
cp config/config.example.yaml ~/.config/custos/config.yaml

# 2. what does the tool see right now?
python3 -m custos doctor
python3 -m custos scan

# 3. one monitoring pass, by hand, then from your scheduler
python3 -m custos monitor --once
```

`scan` prints one row per session -- tool, session, tokens, window, percent,
where the number came from. `monitor --once` is the scheduled entry point: it
prints messages only when something crossed a line, which is what keeps cron
quiet.

Watch the whole pipeline on synthetic sessions first, with no agent installed
and nothing outside a temporary directory touched:

```sh
python3 -m custos demo
```

```
reading        demo demo-session-0001 at 184000 of 200000 tokens (92.0%), source=external
threshold      baseline 180000 (90% of window 200000); warn 153000; rotate 162000 -> rotate
interlock      passed (idle, quiet window 0m, no earlier rotation)
handover       .../handovers/handover_demo_demo-ses_20261016-215433.md
rotate         mode=assisted rotated=False

[custos] rotation required
  old session: demo demo-session, 184000 tokens, 92.0% of window, read 60m ago
  trigger: tokens 184000 reached the rotate line 162000
  mode: assisted
  handover: .../handovers/handover_demo_demo-ses_20261016-215433.md
  next: start a new session and read the handover file above
  gate: passed on attempt 2
selection #1   mode=random seed=20261016 -> asked consecutive-sum-87
gate #1        passed on attempt 2 (no streak)
  attempt 1    fail extracted=37 expected 30, last number was 37
  attempt 2    pass extracted=30
rotate #2      handover written; gate left pending for later runs
selection #2.1 mode=sequence cursor=1 -> asked consecutive-sum-87
gate #2.1      failed after 2 attempt(s) (2 fail in a row)
selection #2.2 mode=sequence cursor=2 -> asked lighter-ball-weighings
gate #2.2      failed after 2 attempt(s) (4 fail in a row)

[custos] gate alert: the new session failed the check 4 times in a row
  session: demo demo-session, 184000 tokens, 92.0% of window, read 60m ago
  attempts: 4 in a row, 4 in total for this rotation
  last reply: answer: 10
  next: it is retried on every scheduled run until it recovers; a recovery is
        reported on its own, so no action is needed.
alert          degraded alert emitted after 3 consecutive wrong answers
pending        event rot-...-0001-b stays pending with 4 recorded attempt(s) so
               the next scheduled run continues the streak
files          2 handover(s) in .../handovers; state at .../state.json;
               events at .../events.jsonl

demo finished: reading -> threshold -> interlock -> handover -> rotation ->
gate (random, fail then pass) -> carried-over gate (sequence, alert)
```

The demo exits non-zero if a step stops failing where the scenario says it must,
so it is a smoke test as well as a tour.

## Commands

| command | what it does |
| --- | --- |
| `scan` | one row per session: tokens, window, percent, source of the number |
| `monitor` | the scheduled entry point: read, evaluate, write handover, rotate |
| `rotate` | rotate one named session; `--dry-run` shows the plan |
| `gate` | run the intelligence check once, by hand or from cron |
| `questions` | inspect and validate a question bank (`list`, `validate`) |
| `report` | push a reading from a tool that knows its own token count |
| `demo` | the documented end-to-end scenario, on synthetic sessions |
| `doctor` | what is configured, what was found, what is missing |

`--json` and `--quiet` are accepted before or after the subcommand. With
`--json`, stdout is one JSON document and nothing else, so a caller can parse it
without guessing which lines are messages. `--dry-run` changes nothing on disk.

## Supported agents

| Agent | Reading source | Verification | Rotation | Gate |
| --- | --- | --- | --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl` message usage | **verified** | assisted or full-auto | `claude -p` |
| Codex | `~/.codex/sessions/**` rollout token counts | **verified** | assisted or full-auto | `codex exec` |
| OpenCode | `~/.local/share/opencode/opencode.db` (read-only) | **verified** | assisted | `opencode run` |
| Pi | `~/.pi/agent/sessions` | **doc-verified** | assisted | `pi --print` |
| Generic | a file or a command you configure | **experimental** | configured command, else assisted | configured command |
| *(any tool)* | pushed via `custos report` | **experimental** | assisted | not available |

**verified** means the occupancy semantics were checked against real local data
from that tool, with the numbers recorded in the adapter source. **doc-verified**
means the paths and flags come from the tool's documentation and the adapter
searches for its usage fields rather than assuming them, failing loudly instead
of inventing a number. **experimental** means correctness depends on the source
you configured.

Adapters report a window when the tool's own record carries one, and fall back
to the configured `window` only when it does not -- a session can switch models
mid-life, and the configured number would be silently wrong.

## Full-auto rotation for Claude Code & Codex

Both adapters can start the replacement session themselves. It is off by
default: a tool that spawns agents without being asked is a worse default than
one that hands you a file to read.

```yaml
adapters:
  claude_code:
    rotate_mode: full_auto
    # rotate_timeout: 900                    # seconds for the spawn
    # spawn_args: ["--permission-mode", "acceptEdits"]
    # continuation_prompt_file: /path/to/prompt.md
    # rotate_cwd: /path/to/project
  codex:
    rotate_mode: full_auto
    # spawn_args: ["--sandbox", "workspace-write"]
```

| Key | Default | What it does |
| --- | --- | --- |
| `rotate_mode` | `assisted` | `full_auto` makes the adapter spawn the new session |
| `rotate_timeout` | `900` | Seconds allowed for that spawn |
| `spawn_args` | adapter-specific | Extra CLI arguments; an explicit list **replaces** the default |
| `continuation_prompt_file` | built-in | Template with `{handover}` and `{session}` placeholders |
| `rotate_cwd` | from the old session | Working directory for the new session |

Under full-auto the adapter runs one headless turn:
`claude -p <prompt> --output-format json --permission-mode acceptEdits` for
Claude Code, `codex exec --skip-git-repo-check --sandbox workspace-write <prompt>`
for Codex. The prompt tells the new session to read the handover and start its
reply with `CONTINUATION RECAP:`; that line is the evidence the new session
actually read it. `custos status` and `custos scan --json` report
`full_auto: true` for the adapters configured this way.

**Safety.** `spawn_args` controls what the spawned session may do without asking
-- `acceptEdits` for Claude Code, `workspace-write` for Codex. Both are narrower
than skipping permissions entirely; widening them (`bypassPermissions`, a
dangerous sandbox mode) means an unattended agent can rewrite your project. The
handover is the only context the new session is given, so keep the timeout at
900s or above: a spawn that times out has already written the handover and
started nothing.

### `custos goal`: one long task across sessions

`custos goal` drives a task that will not fit in one context window. Each round
continues the current session until its usage reaches the rotate line, then
rotates into a fresh one and keeps going.

```bash
custos goal --tool claude_code --task-file ./TASK.md --cwd ./myproject --max-sessions 5
```

It keeps three files:

* `.custos-goal-state.json` (or `--state-file`): sessions, rotations, the
  resolved rotate line and the outcome. Written atomically after every change.
* `custos-goal-progress.md`: created from the task on the first run; every round
  prompt asks the agent to append one dated line to it.
* the usual handover snapshots, one per rotation.

Every event is one stdout line
(`[goal] <ts> round=<k> event=<name> session=<id> tokens=<n> rotate_line=<m> detail=<...>`),
or one JSON object per line with `--json`. Full detail: `docs/full-auto-goal.md`.

## How it works

```
scan ─┬─ adapters ──> reading {tool, session, tokens, window, pct, source, ts}
      │
      └─ thresholds ──> warn line / rotate line (absolute or % of window)
                          │
monitor ── locks ─────────┤
      │                   ├─ interlocks ──> is the session idle? already handled?
      │                   ├─ handover ────> markdown snapshot + event record
      │                   ├─ rotate ──────> assisted (tell you) or full-auto
      │                   └─ gate ────────> one-shot prompt, graded reply
      └─ state ───────────> atomic writes: state.json, events.jsonl, ledger
```

A rotation is recorded as an **event** with a stable id. The gate verdict is
attached to that event across runs, which is what lets `alert_after_failures`
count a streak that spans several five-minute passes instead of resetting on
every tick.

Full detail: `docs/architecture.md`.

## Configuration

One file: `config.yaml` (or `config.json`) inside `$CUSTOS_CONFIG_DIR`,
default `~/.config/custos`. Every key is optional and documented in
`config/config.example.yaml`. The ones that change behaviour most:

| key | default | why you would move it |
| --- | --- | --- |
| `thresholds.baseline_tokens` | `350000` | the point your tool compacts at |
| `thresholds.baseline_percent` | `null` | same, as a share of the window; wins when set |
| `thresholds.warn_factor` | `0.90` | how early a warning is worth printing |
| `thresholds.rotate_factor` | `0.93` | how early a rotation is worth doing |
| `monitor.interval_minutes` | `5` | how often the schedule fires; extra calls no-op |
| `monitor.activity_quiet_minutes` | `10` | a session touched more recently than this is left alone |
| `rotation.mode` | `assisted` | `full-auto` only where the adapter can start a session |
| `rotation.idempotency_hours` | `6` | how long a handled session is left alone |
| `gate.mode` | `random` | `random`, `sequence`, or `all` |
| `gate.alert_after_failures` | `5` | consecutive wrong answers before "degraded" |

The two lines are factors of the baseline, so moving the baseline moves both.

## Handover

A handover is a markdown file in `handovers/` with a fixed schema: where the
work stood, the decisions that were made, what is still open, the recent message
excerpts, and the paths that matter. `rotation.mode: assisted` writes it and
tells you to start a new session; the file is written before the message, so the
message can always name a file that exists.

## Question bank

The gate exists to catch a session that came back *worse* than it left: wrong
model, broken context, an agent that answers confidently and incorrectly. It
asks questions from a bank and grades the answers with one of four matchers
(`exact`, `last_number`, `regex`, `script`).

The shipped bank is a starting point, not the product: it demonstrates the
format and gives you something to replace. Point `gate.questions` at your own
bank and validate it with `custos questions validate`. See
`docs/questions.md`.

## Pushing readings from any other tool

If your tool already knows its own token count, skip the parsing:

```sh
custos report --tool my-agent --session "$SESSION_ID" --tokens 128000 --window 200000
```

A pushed reading outranks anything parsed from a transcript, and the newest one
wins. Hook it to a session-start script and the tool covers agents it has no
adapter for.

## Security and privacy

The full model is in `docs/security.md`. The short version:

- **Nothing is read that the tools did not already write**, and everything is
  opened read-only. Databases are opened with `mode=ro`.
- **Nothing runs** except the gate's one-shot prompt, in a cleaned environment
  with a timeout, a process group, and no inherited secrets (see
  `docs/security.md`).
- **One network request exists** -- the optional `notify.webhook_url` -- and it
  is off by default, skipped in `--dry-run`, and a failure is logged and
  ignored: a broken endpoint must never break a rotation.
- **`DISABLE`** in the configuration directory stops everything. `monitor` exits
  silently; `rotate` refuses unless it is a dry run. It is the fastest way to
  freeze the tool on a session that must not be touched.
- **One instance at a time.** A file lock makes a second monitor skip its pass
  rather than race the first one into rotating the same session twice.
- **`--dry-run` writes nothing**, including the gate's sequence cursor and the
  alert throttle.

## Limitations and known issues

- **0.1.0, unreleased.** Interfaces may change before 1.0; `CHANGELOG.md` is the
  record.
- **macOS and Linux only.** The single-instance lock needs `fcntl` and the
  subprocess timeout needs `os.killpg`; Windows is not supported and WSL works.
  See `docs/faq.md`.
- **Assisted rotation with every shipped adapter.** `custos` writes the handover
  and tells you to start a new session; it does not type into a running TUI.
  `full-auto` needs an adapter that can start a session itself.
- **Verification levels differ.** Claude Code, Codex and OpenCode were checked
  against real local data, Pi was not, and the generic adapter and push mode are
  only as correct as the source you configure.
- **The question bank is a starter.** It demonstrates the format; replace it
  with questions about your own work before trusting the gate.
- **The gate costs model calls.** Each attempt is one prompt through the tool's
  one-shot command, at most five per rotation with the defaults. Set
  `gate.enabled: false` to skip it entirely.

## Repository layout

```
custos/                  the package
  cli.py                 argument parsing, output modes, subcommands
  core/                  usage, thresholds, handover, rotation, gate, state
  adapters/              claude_code, codex, opencode, pi, generic, demo
  demo.py                the scenario behind `custos demo`
config/                  example configuration and question bank, YAML and JSON
docs/                    architecture, adapters, full-auto goal, questions, security, FAQ
skills/context-rotation/ optional agent skill describing the workflow
tests/                   the pytest suite and its fixtures
tools/                   sanitize_check.py and acceptance_check.sh
install.sh               installer, including the seeded configuration
uninstall.sh             the reverse
pyproject.toml           packaging metadata and pytest configuration
```

Tests are organised like the package: one module per subsystem
(`test_rotation.py`, `test_gate.py`, ...), `test_cli.py` driving the real command
line, and `test_sanitize.py` planting forbidden strings to prove the scanner
catches them.

## Contributing

Issues and pull requests are welcome. Before you open one:

```sh
python3 -m pytest -q                     # the suite
python3 tools/sanitize_check.py .        # must print 0 hit(s)
bash tools/acceptance_check.sh           # the full gate, on 3.9 and 3.13
```

- Keep the scanner clean. It looks for machine-specific paths, usernames,
  internal service names, ports and credential shapes; a clean run is part of
  the review, not a formality.
- New adapters follow `docs/adapters.md`: record how the occupancy number was
  verified, search for the field instead of assuming it, and fail loudly rather
  than invent a reading.
- No new runtime dependencies -- the standard library only, and PyYAML stays
  optional.
- Tests assert behaviour, meaning what a command does, rather than the shape of
  the source.
- Report a security problem through the process in `docs/security.md`; never
  paste real session data, credentials or local paths into an issue.

## Development

```sh
python3 -m pytest -q                   # 458 tests
python3 -m custos demo                 # end-to-end smoke test, no agent needed
python3 tools/sanitize_check.py .      # no machine-specific strings
```

The suite runs on Python 3.9 and 3.13 with no dependencies beyond pytest. On an
interpreter without PyYAML the four YAML tests skip instead of failing, and CI's
extras job runs them; `tools/acceptance_check.sh` runs both interpreters. CI
also byte-compiles every module and checks that each subcommand answers
`--help`.

## Documentation

| file | what it covers |
| --- | --- |
| `docs/architecture.md` | the pipeline, the data model, the module map |
| `docs/adapters.md` | support matrix, occupancy semantics, push mode, writing an adapter |
| `docs/full-auto-goal.md` | full-auto rotation for Claude Code and Codex, and the `custos goal` driver |
| `docs/questions.md` | bank format, the four graders, swapping the bank |
| `docs/security.md` | what is read, what is written, subprocess isolation, the kill switch |
| `docs/faq.md` | assisted vs full-auto, silence, multiple machines, Windows |
| `docs/publish-checklist.md` | what to do before this repository goes public |

## License

MIT -- see `LICENSE`.
