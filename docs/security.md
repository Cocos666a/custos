# Security

What this tool touches, what it does not, and where the sharp edges are.

## Read-only on other tools' data

Session stores belong to other programs. Every adapter opens them read-only:

- SQLite databases are opened with `mode=ro` in the URI, so a write is a
  database-level error rather than a convention.
- JSONL transcripts are read with plain reads and never opened for append.
- Nothing outside the configuration directory is ever written.

The configuration directory is the only place `custos` creates or modifies
files: `config`, `state.json`, `events.jsonl`, `readings/`, `handovers/` and the
lock file. A `--dry-run` does not write even there; `doctor` never creates
anything, not even the directory it is checking.

## The kill switch

`touch <config dir>/DISABLE` stops everything. `monitor` exits silently with
status 0 and `rotate` refuses. A `--dry-run` still runs, so you can see what
would have happened. Removing the file re-enables.

This is checked before any other work, so it takes effect on the next scheduled
run rather than at some later stage of the pipeline.

## Network

**There is no network access by default.** The only request the tool can make is
the optional webhook, which is off unless you set `notify.webhook_url`. When it
is set:

- the payload is `{"tool": "custos", "ts": ..., "messages": [...]}`;
- it is sent with a timeout (`notify.webhook_timeout_seconds`, default 10s) and
  retried once;
- a failure is logged to stderr and otherwise ignored. A notification problem
  must never become a rotation problem;
- it is **skipped during `--dry-run`**. Printing is how a dry run reports; a POST
  is a side effect that leaves the machine, and a dry run must not have those.

Nothing is fetched, and no reading, session content or path is sent anywhere
unless you configure the webhook -- in which case the message text, which
includes session identifiers and handover paths, is what goes over the wire.
Point it at an endpoint you control.

## Child processes

Every spawned process gets a cleaned environment. This is the single most
important thing in this document, because getting it wrong is invisible.

Agent CLIs decide which session they belong to partly from environment
variables. A child that inherits `CLAUDE_CODE_*`, `CODEX_*`, `OPENCODE_*` or
`PI_*` can attach to the *caller's* session instead of starting its own. For the
gate that means the question is asked of the session being tested rather than of
a fresh one -- the check passes, and means nothing.

`clean_env()` removes those families by prefix, plus a list of exact names
(`CLAUDECODE`, `SESSION_ID`, `AGENT`, ...), and keeps everything else including
`PATH`. Extend the lists via `clean_env(deny_prefixes=..., deny_exact=...)` if
your tool exports markers outside those families.

Children also:

- run with `start_new_session=True`, so they get their own process group;
- are `SIGKILL`ed as a group on timeout, so a CLI that spawns helpers cannot
  leave orphans behind;
- run in an inert working directory (`/tmp`) by default, so they do not pick up
  a project's configuration.

## Filesystem safety

**Atomic writes.** Every significant write goes to a temporary file in the
destination directory, is `fsync`ed, and is moved into place with `os.replace`.
An interrupted write leaves the previous file intact and no temporary file
behind. State, readings and handovers all use this.

**Unique handover names.** A snapshot is never overwritten. Two rotations in the
same second get separate files rather than the second silently replacing the
first.

**Path components from user input.** Tool and session identifiers become file
names under `readings/`, so anything outside a conservative character set is
replaced. A label with no usable character at all is rejected rather than stored
under a name that would collide with another tool's.

**Single instance.** A `flock` on `<config dir>/custos.lock` means two
scheduled runs cannot act at once. A held lock makes the second run skip, not
queue.

**No privilege escalation.** Nothing is installed system-wide, no `sudo` is
used, no scheduler is installed for you, and no configuration file belonging to
another tool is edited. `install.sh` writes under `--prefix` and
`$HOME/.config` and prints scheduler templates rather than installing them.

## The `script` grader

A question bank can contain Python. `script` graders run in the `custos`
process with that user's privileges. The restricted namespace -- a small
allow-list of builtins, no import machinery -- keeps an honest bank honest. It
is not a sandbox, and it is not meant to be one: anything that can edit the bank
can already run code as you.

The rule that follows is simple. **A question bank is code.** Only use a bank you
wrote or have read. Do not point `gate.questions` at a file you downloaded.

The same applies to `generic` adapter sources: a `command` source runs a command
you configured, and a `prompt_cmd` runs a prompt through it.

## Secrets

`custos` does not read credentials, does not ask for them, and stores none.
It does not read `.env` files, keychains, or any tool's auth store.

Two things to be aware of anyway:

- Handover snapshots contain excerpts of the recent conversation when the
  adapter can read history. If your session contains a secret, the snapshot may
  contain it too. Snapshots live in `handovers/` under your config directory
  with your user's permissions; treat them the way you would treat a transcript.
- `events.jsonl` records session identifiers, token counts, file paths and error
  text. It does not record conversation content.

## Supported platforms

macOS and Linux.

Windows is not supported. The single-instance lock uses `fcntl`, which does not
exist there; the code degrades to "always acquired" rather than crashing, but
that means two scheduled runs could act at once and the safety property is gone.
Timeouts are implemented with `os.killpg`, which is also POSIX. A Windows port
would need a different lock and a job-object based timeout.

## Reporting

This is an early-stage project. If you find a security problem, please open an
issue describing the impact; do not include real session data, credentials or
paths from your machine.
