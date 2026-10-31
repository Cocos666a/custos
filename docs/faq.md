# FAQ

## Why does it not start the new session for me?

Because starting another program's session is not something this tool can do
reliably, and a rotation that silently does nothing is worse than one that tells
you what to do.

That is the **assisted** outcome: the snapshot is written, you are told where it
is and how to start the replacement, and the next scheduled run does not repeat
the message. An adapter can implement full-auto when its tool offers a real way
to start a session (the `generic` adapter does, through a configured
`rotate_cmd`), and the message then says `mode: full-auto`. Nothing pretends.

The reason it is worth being strict here: the dangerous failure is not "the
rotation did not happen", it is "the tool believes it rotated and stops
watching". Assisted mode is honest about who is responsible.

## Why is there no output?

Because nothing happened, and that is the design.

`monitor` writes to stdout only when there is something to say. An empty stdout
is what makes cron mail useful, a chat bridge quiet, and a terminal readable. If
you want to know it is alive:

```sh
custos status
```

That shows the configuration, the adapters, when monitors last ran, and any
rotation still waiting on a gate check.

To make a run say something regardless, `--json` always prints a document (an
empty array when there is nothing).

## How do I check it is actually working?

Three levels, cheapest first:

```sh
custos doctor          # is it installed and configured correctly
custos scan            # can it read your sessions
custos demo            # does the whole pipeline work on this machine
```

`demo` is the real answer: it runs the entire chain against synthetic sessions
with no agent involved, and ends with the summary line if every stage worked.
If `demo` passes and `scan` sees your sessions, the tool works; anything left is
configuration.

## How do I turn it off?

```sh
touch ~/.config/custos/DISABLE
```

`monitor` exits silently and `rotate` refuses. `--dry-run` still works, so you
can see what would have happened while it is off. Remove the file to re-enable.
If you also want the scheduler gone, remove the cron line / launchd plist /
systemd timer; `install.sh` never installed one for you.

## I have several agents. Does it handle all of them?

Yes, and they are independent. Each session is watched on its own thresholds and
its own history. `monitor` acts on the fullest eligible session per pass, so a
five-minute schedule cannot turn into five rotations at once.

Use `--tool NAME` to narrow a run to one adapter.

## I have several machines.

The state lives in the config directory, so each machine keeps its own. That is
the right default: a session belongs to the machine running it, and a handover
snapshot has to be readable where the work is.

If you want the messages in one place, point every machine's
`notify.webhook_url` at the same endpoint.

## My window is 1M, not 200k.

Set it per adapter:

```yaml
adapters:
  claude_code:
    window: 1000000
```

Adapters that can read the window from the tool's own records prefer that value,
because a session may switch models mid-conversation. The configured value is
the fallback.

If you would rather express the baseline as a fraction of whatever window is in
play, use `thresholds.baseline_percent` instead of `baseline_tokens`:

```yaml
thresholds:
  baseline_tokens: null
  baseline_percent: 75      # warn at 67.5%, rotate at 69.75%
```

## Are my old sessions modified?

No. Transcripts and databases are opened read-only -- SQLite with `mode=ro`,
files with plain reads. Nothing outside the config directory is written, ever.
The only files this tool creates are its own config, state, event log, pushed
readings and handover snapshots.

## What is "push mode" for?

For tools that already know their own token count. Instead of parsing a
transcript and guessing, the tool reports the number:

```sh
custos report --tool my-agent --session abc123 --tokens 190000 --window 200000
```

A pushed reading outranks an adapter reading for the same session, because it
was measured rather than reconstructed, and `scan` prints which source won. See
`docs/adapters.md` for wrapper, hook and periodic-collection patterns.

## Windows?

Not supported. The single-instance lock needs `fcntl`, and the process-group
timeout needs `os.killpg`; both are POSIX. The code degrades to "lock always
acquired" rather than crashing on Windows, but that means two scheduled runs
could act at once, which is the safety property the lock exists for. If you want
to port it, the lock and the timeout are the two things to replace.

WSL works, since that is Linux.

## How do I change the gate questions?

```sh
cp config/questions.example.yaml ~/.config/custos/questions.yaml
# edit it
custos questions validate
custos gate --tool claude_code --dry-run
```

Full format, the four graders and the pitfalls are in `docs/questions.md`. The
short version: use `last_number` unless you have a reason not to, keep the
questions answerable without tools, and change them when they stop being
questions your sessions have not seen.

## What does "verified" versus "doc-verified" mean?

It is about how much of the adapter was checked against reality.

**verified** -- the occupancy semantics were confirmed against real local data
from that tool. The adapter source records the field names and the numbers that
established them, so you can re-derive the conclusion rather than trust it.

**doc-verified** -- the paths and flags come from the tool's published
documentation, but no local install was available to confirm the field names
inside a usage record. The adapter searches for them and fails loudly rather
than inventing a number.

**experimental** -- correctness depends on configuration you wrote.

The distinction is in `scan`, `status` and `doctor` output because a monitor
that reports the wrong number is worse than one that reports nothing: you would
trust it.

## The gate says "no usable answer" instead of failing. What is wrong?

That is the infrastructure path, and it means the one-shot command did not
produce a reply at all -- a timeout, empty output, or a non-zero exit. The
session is not being judged; the command is not working.

Check it on its own:

```sh
claude -p "say hello"        # or codex exec / opencode run / pi --print
```

Usual causes: the CLI needs a login, it needs a flag before it will run
non-interactively, or it needs longer than `gate.timeout_seconds`. If it needs
more time, raise the timeout; the alert message names the last error.

## Can I run more than one monitor at once?

You can start them, but only one will do anything: a `flock` on the config
directory means the second run skips rather than queuing. That is deliberate --
two monitors rotating the same session is the failure mode the lock exists to
prevent.

If you want finer control, run separate monitors with separate config
directories and `--tool` filters.

## How much does it cost to run?

Nothing per run. The only cost is the gate: each attempt sends one prompt
through the tool's one-shot command, so a gate fire costs one model call per
attempt. With the defaults that is at most five calls per rotation, with a
sixty-second gap between them, and rotations happen when a window fills rather
than on a timer.

Set `gate.enabled: false` to skip the check entirely; rotations are then
recorded with the gate marked as skipped, which is honest rather than silent.
