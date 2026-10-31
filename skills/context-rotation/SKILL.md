---
name: context-rotation
description: Watch an AI coding agent's context window and rotate the session before it degrades. Use when a session is near its context limit, when the agent starts forgetting or repeating itself, when you want a handover snapshot before starting fresh, or when setting up automatic monitoring for a long-running agent session. Works with any agent that exposes its token usage, either through a built-in adapter or through a usage reading pushed by a hook.
---

# Context rotation

A long agent session eventually fills its context window. What happens next is
the problem: the tool compacts the conversation, the oldest and often most
important material is summarised away, and the session keeps going while quietly
getting worse. Answer quality drops before anything visibly breaks.

`custos` watches the window and intervenes before that point: it writes a
handover snapshot, tells you to start a fresh session, and then checks that the
fresh session is actually healthy before you trust it with the work.

## When to reach for this

- A session is at or near its context limit and you want to hand the work over
  rather than lose it to a compaction.
- The agent has started repeating itself, forgetting earlier decisions, or
  contradicting work it did earlier in the same session.
- You are about to run a long task in one session and want the rotation to
  happen on its own when the window fills.
- You already rotated and want to confirm the new session is not degraded.

## The everyday commands

```sh
custos scan                 # what is running and how full it is
custos status               # configuration, adapters, pending checks
custos monitor --once       # what the scheduler runs; silent when idle
custos rotate --tool NAME --session ID
custos gate --tool NAME     # run the intelligence check by hand
custos demo                 # the whole pipeline on synthetic sessions
custos doctor               # diagnose the installation
```

`scan` marks the sessions that have crossed a line, so the answer to "do I need
to care right now" is visible without reading the numbers.

## What happens during a rotation

1. **Interlocks.** The tool refuses to act on a session that was read recently,
   was already rotated, or cannot be confirmed idle. A session that is still
   being used is left alone.
2. **Handover first.** A markdown snapshot is written before anything else, so
   the work is recoverable even if the rotation then fails. It records the
   reading, the trigger, and the recent conversation when the tool can read it.
3. **Rotation.** `custos` asks the adapter to start a replacement. Most
   adapters cannot drive another tool's session lifecycle, so the usual outcome
   is *assisted*: the snapshot is written and you are told how to start the new
   session. An adapter that can start one does.
4. **Gate.** Once a replacement session exists, a question from the bank is sent
   to it. If the session cannot answer a question it has not seen before, it is
   not a healthy place to continue, and that is worth knowing immediately.

## Reading the messages

Only stdout carries messages, and only when something happened. A scheduled run
with nothing to report prints nothing, which is what makes cron mail useful.

Silence therefore means "nothing needed you", not "the tool is broken". Use
`custos status` to confirm it is alive and see when it last ran.

To stop everything immediately:

```sh
touch ~/.config/custos/DISABLE
```

`monitor` exits silently and `rotate` refuses until the file is removed. A
`--dry-run` still works, so you can see what would have happened.

## Replacing the questions

The gate is only as good as its questions, and a question that has been asked
before stops meaning anything. Put questions your agent has never seen into the
bank:

```sh
cp config/questions.example.yaml ~/.config/custos/questions.yaml
custos questions validate    # catches typos before they waste a rotation
```

Keep them answerable without tools and hard enough that a degraded session gets
them wrong. See `docs/questions.md` for the format, the four graders, and how to
pin a question's text so an edit cannot silently change what is being tested.

## Adding a tool

Adapters live in `custos/adapters/`. A new one is a single module plus one
line in the registry; `docs/adapters.md` has a worked example. If a tool already
knows its own token count, it does not need an adapter at all:

```sh
custos report --tool my-agent --session abc123 --tokens 190000 --window 200000
```

A value reported this way outranks anything parsed from a transcript, which
makes it the right answer for a tool with a hook or a wrapper script.

## Safety

- Third-party stores are opened read-only. Nothing outside the configuration
  directory is written.
- No network access unless you configure a webhook, and never during a
  `--dry-run`.
- Every mutating command has `--dry-run`.
- A `script` grader in a question bank runs Python. Treat a bank as code and
  only use banks you trust.
