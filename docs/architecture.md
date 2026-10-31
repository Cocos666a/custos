# Architecture

## The pipeline

```
                    ┌──────────────────────────────────────────┐
                    │ adapters: read each tool's own store      │
                    │  claude_code  codex  opencode  pi  generic│
                    └───────────────────┬──────────────────────┘
                                        │ Reading
                    ┌───────────────────▼──────────────────────┐
                    │ readings/<tool>/<session>.json            │
                    │ (pushed by `report`, merged: newest wins) │
                    └───────────────────┬──────────────────────┘
                                        │ one Reading per session
                    ┌───────────────────▼──────────────────────┐
                    │ thresholds: baseline -> warn line,        │
                    │ rotate line; `evaluate()` -> decision     │
                    └───────────────────┬──────────────────────┘
                                        │ decision
                    ┌───────────────────▼──────────────────────┐
                    │ interlocks: pending gate? already         │
                    │ rotated? recently read? turn in flight?   │
                    │ kill switch? lock held elsewhere?         │
                    └───────────────────┬──────────────────────┘
                          defer ────────┴──────── proceed
                            │                       │
                            │        ┌──────────────▼───────────────┐
                            │        │ handover: snapshot FIRST,    │
                            │        │ fixed markdown schema        │
                            │        └──────────────┬───────────────┘
                            │                       │
                            │        ┌──────────────▼───────────────┐
                            │        │ adapter.rotate()             │
                            │        │  assisted: instructions      │
                            │        │  full-auto: starts a session │
                            │        └──────────────┬───────────────┘
                            │                       │
                            │        ┌──────────────▼───────────────┐
                            │        │ event recorded as PENDING    │
                            │        │ (state.json -> events)       │
                            │        └──────────────┬───────────────┘
                            │                       │
                            │        ┌──────────────▼───────────────┐
                            │        │ gate: question -> judge      │
                            │        │ retry within a fire, streak  │
                            │        │ across fires                 │
                            │        └──────────────┬───────────────┘
                            │                       │
                            └───────────────────────▼──────────────────┐
                                                    │ notify: stdout    │
                                                    │ (+ webhook, opt-in)│
                                                    └───────────────────┘
```

The vertical split matters. Everything above the interlocks is observation and
can run as often as you like. Everything below changes state, and runs at most
once per scheduled pass.

## Why a rotation event outlives its rotation

Starting a replacement session and checking that it is healthy takes minutes and
usually several scheduled runs, because in assisted mode a human has to start
it. The rotation itself must not block on that.

So the rotation writes the event with `status: gate_pending` and returns. Later
runs pick the oldest pending event up and continue the check, appending each
attempt to the event. That is what turns "the gate failed again" into "the gate
has failed five times in a row" -- the streak is a property of the event, not of
one run.

The event is closed when the check passes, when the adapter turns out to be
incapable of ever answering, or when it is older than
`monitor.pending_max_age_days`.

## Data model

### `Reading`

One observation of one session's context occupancy. Produced by an adapter or
pushed in by a hook; after that nothing above the adapter layer knows which.

| field | meaning |
| --- | --- |
| `tool` | which agent this is about |
| `session` | the tool's own session identifier |
| `tokens` | size of the prompt the next turn would send |
| `window` | the model's context window, when the tool records it |
| `pct` | derived from `tokens` and `window` |
| `source` | `transcript`, `session-store`, `external`, `report`, `hook` |
| `ts` | when the reading was taken |
| `compressed` | the tool has already compacted this session at least once |
| `detail` | the raw evidence, for diagnosis |

Two readings for the same session are one fact observed twice. `merge_readings`
keeps one winner: newest `ts` first, then highest source priority, then larger
`tokens`. A `report` outranks a `transcript` because it was not inferred. The
winner records the sources it beat in `detail.other_sources`, and `scan` prints
which source won.

### `state.json`

Mutable, overwritten atomically, small.

| key | contents |
| --- | --- |
| `ledger` | idempotency keys, pruned after `state.ledger_max_age_days` |
| `history` | per-session rotation history: last rotated id, last handled id, failure count |
| `events` | rotation events, pending and recently resolved |
| `gate` | sequence cursor, attempt counter, last attempt time |
| `meta` | last monitor timestamp, alert flags |
| `last_events` | the last few event kinds, for `status` |

### `events.jsonl`

Append-only, never rewritten. One JSON object per line with `ts`, `epoch`,
`kind` and whatever fields that kind carries. It survives a corrupt `state.json`
and can be read with `jq` or replayed by hand. Kinds include `rotate`,
`rotate_assisted`, `rotate_failed`, `defer`, `dry_run`, `gate_attempts`,
`gate_skipped`, `handover_failed`, `adapter_unavailable`.

### `readings/<tool>/<session>.json`

One file per session, written atomically. Independent files mean two hooks
reporting at once cannot clobber each other, and a corrupt file loses one
session rather than the store.

### `handovers/handover_<tool>_<session8>_<timestamp>.md`

The snapshot. Fixed schema, versioned in the first line, section headings are
constants because humans and downstream tooling both parse it. A second rotation
in the same second gets a numeric suffix rather than overwriting the first.

## Module map

| module | responsibility |
| --- | --- |
| `custos/cli.py` | argument parsing, command wiring, output formatting |
| `custos/demo.py` | the synthetic end-to-end scenario |
| `core/config.py` | config-dir resolution, defaults, path helpers |
| `core/usage.py` | `Reading`, the error types, merge and precedence |
| `core/thresholds.py` | baseline resolution, warn/rotate lines, `evaluate` |
| `core/rotation.py` | interlocks, the rotation pipeline, pending events |
| `core/handover.py` | snapshot rendering and the fixed schema |
| `core/gate.py` | question bank, the four graders, `run_gate`, streaks |
| `core/gate_runner.py` | gate settings, cross-run bookkeeping, alerting |
| `core/state.py` | `state.json`, `events.jsonl`, the pushed-reading store |
| `core/notify.py` | message construction, stdout and webhook delivery |
| `core/util.py` | atomic writes, `clean_env`, `FileLock`, `run_cmd` |
| `adapters/` | one module per tool, plus the registry |

## Design principles

**Silence first.** `monitor` prints only when something happened. An empty
stdout is the success case for a scheduled run, and it is what makes cron mail,
a terminal, and a chat bridge all usable without filtering.

**Fail closed on the unknown.** When an adapter cannot read a store, the tool
defers that tool rather than guessing. When an adapter implements busy detection
and the check fails, the rotation defers. The only case where "cannot tell" does
not defer is an adapter that has no detection at all -- there the activity quiet
window is the interlock, and treating an absent feature as a permanent objection
would disable the tool rather than protect anything.

**Every write is atomic.** Temporary file in the destination directory, `fsync`,
`os.replace`. An interrupted write leaves the previous file intact; the tests
cover this.

**Every spawn is cleaned.** Child processes never inherit agent session markers
(`CLAUDE_CODE_*`, `CODEX_*`, `OPENCODE_*`, `PI_*`, `CUSTOS_*` and friends).
A child that inherits them joins the caller's session, which silently
invalidates the check it was spawned for.

**Every spawn can be killed.** `start_new_session=True` puts the child in its
own process group, and a timeout `SIGKILL`s the group, so a CLI that spawns
helpers cannot leave orphans behind.

**Idempotency with an expiry.** Acting on a session once records it, so the next
run does not repeat. The record expires after `rotation.idempotency_hours`
because a session only shrinks when the tool compacts it or the user starts a
new one -- without expiry, the tool would go quiet on exactly the session it
exists to watch.

**The audit log is not the state.** `events.jsonl` is append-only and is never
the source of truth for a decision, so a decision can never depend on a file
that a crash could have truncated.
