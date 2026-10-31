# Full-auto rotation and the goal driver

Two things changed together: the Claude Code and Codex adapters can now start a
replacement session themselves, and `custos goal` uses that to run one long task
across as many sessions as it needs.

Neither is on by default. A rotation that spawns an agent is a bigger act than
one that writes a file and tells you about it, and the tool should not take the
bigger act without being asked.

## Turning it on

Both adapters read the same keys in their own config section:

```yaml
adapters:
  claude_code:
    rotate_mode: full_auto        # assisted (default) | full_auto
    rotate_timeout: 900           # seconds for the spawn
    spawn_args: ["--permission-mode", "acceptEdits"]
    continuation_prompt_file: null
    rotate_cwd: null
  codex:
    rotate_mode: full_auto
    spawn_args: ["--sandbox", "workspace-write"]
```

`spawn_args` is a complete list, not an addition: a configured value replaces
the default, so an argument can be removed as well as added.

`continuation_prompt_file` points at a template. `{handover}` and `{session}`
are substituted. The built-in template is:

```
You are the continuation of a previous session that ran out of context window.
Read the handover file at: {handover}
Your FIRST output line must be exactly: CONTINUATION RECAP: <2-3 sentences: where the previous session left off, and what you will do next>
Then continue the work described in the handover until it is done.
```

The `CONTINUATION RECAP:` line is not decoration. Its presence in the spawn
output is recorded as `recap_seen` in the rotation detail -- evidence that the
new session read the handover rather than starting cold.

### What the adapter actually runs

Claude Code:

```
claude -p <continuation prompt> --output-format json --permission-mode acceptEdits
```

The new session id comes from the `session_id` field of that JSON document. If
the output does not parse -- an older CLI, a wrapper that prints something else
-- the adapter takes the newest transcript that appeared in the same project
directory during the spawn. A rotation that started a session but could not
learn its id is still a rotation; reporting failure would invite a second spawn
of the same handover.

Codex:

```
codex exec --skip-git-repo-check --sandbox workspace-write <continuation prompt>
```

`codex exec` prints no session id, so discovery works from the store: the set of
rollout files under `~/.codex/sessions` and `~/.codex/archived_sessions` is
snapshotted before the spawn, and the new session is the rollout that appeared
during it. The id is the uuid in `rollout-<timestamp>-<uuid>.jsonl`, with the
file's own `session_meta` payload as the cross-check.

### Where the working directory comes from

`rotate_cwd` when set. Otherwise the adapter reads it from the old session:
Claude Code records `cwd` on the first record of the transcript, Codex records
it in the `session_meta` payload. The project directory name under
`~/.claude/projects` is *not* decoded -- that encoding replaces `/` with `-`, so
a path that itself contains `-` cannot be recovered from it.

## `custos goal`

```
custos goal --tool claude_code --task-file TASK.md [--cwd DIR] [--max-sessions 5]
            [--max-rounds-per-session 10] [--rotate-line TOKENS] [--no-resume]
            [--state-file PATH] [--json]
```

The driver runs rounds. A round is one of three things:

1. **First round.** Starts a session with the task text (first 4000 characters)
   and the task file's path.
2. **Resume round.** The current session is below the rotate line, so the same
   session is asked to continue: `claude -p --resume <id> --output-format json`
   or `codex exec resume <id>`.
3. **Rotation round.** The current session is at or above the line. A handover
   is written through the same writer the monitor uses, then
   `adapter.rotate()` starts the replacement. The new session's first turn is
   the continuation prompt, which the adapter already sent; the driver's next
   round for it is a normal resume.

`--no-resume` turns every round after the first into a rotation round. It exists
for tools whose `--resume` is unreliable, and for reproducing rotation behaviour
without waiting for a session to fill up.

### Refusals and stop conditions

| Situation | Outcome |
| --- | --- |
| `<config dir>/DISABLE` exists at start | nothing runs; `killed_switch` |
| another driver holds the state file lock | exit status 3, one line on stdout |
| the rotation reports `rotated: false` | `failed`, with the adapter's error |
| two spawns fail in a row | `failed` |
| the goal is done | `completed` |
| more sessions than `--max-sessions` | `stopped_max_sessions` |
| more rounds than `max-sessions * max-rounds-per-session` | `stopped_max_rounds` |
| `DISABLE` appears between rounds | `killed_switch` |

A rotation that reports success but no session id does not stop the run: a fresh
session is started with the handover path in the prompt instead.

### The files it keeps

`.custos-goal-state.json` (or `--state-file`) is the record. It is written
atomically -- temporary file plus `os.replace` -- after every state change, so a
killed run leaves a readable account of what happened.

```json
{
  "tool": "claude_code",
  "task_file": "/abs/TASK.md",
  "cwd": "/abs/project",
  "started_at": "2026-10-29T02:14:03Z",
  "pid": 48213,
  "rotate_line": 325500,
  "sessions": [
    {"session": "9a1f...", "rounds": 4, "tokens_last": 340112, "started_at": "..."}
  ],
  "rotations": [
    {"ts": "...", "old": "9a1f...", "new": "c07e...", "tokens": 340112,
     "rotate_line": 325500, "trigger": "tokens 340112 >= line 325500",
     "handover": "/abs/.config/custos/handovers/handover_....md", "mode": "full-auto"}
  ],
  "outcome": "running"
}
```

`custos-goal-progress.md` is created in the working directory on the first run,
with the task text under a `# Goal task` heading. Every round prompt tells the
agent to append one dated line per step. It is the continuity the handover only
partly provides: a handover describes the state at one moment, the progress file
accumulates across all of them.

Handovers land wherever `rotation.handover_dir` points, one per rotation.

### The log format

One line per event on stdout, so a scheduler log can be grepped by event name:

```
[goal] <ts> round=<k> event=<name> session=<sid> tokens=<n> rotate_line=<m> detail=<short>
```

`--json` prints the same fields as one JSON object per line instead:

```json
{"event": "rotate", "round": 3, "session": "c07e...", "tokens": 340112, "rotate_line": 325500, "detail": "old=9a1f... new=c07e... handover=/...", "ts": "..."}
```

The run ends with a summary line that is not in the per-event shape:

```
[goal] outcome=<outcome> sessions=<n> rotations=<n> state=<path>
```

### Resolving the rotate line

`--rotate-line` wins. Otherwise the driver calls `resolve_thresholds()` with the
same configuration the monitor uses, so a goal run rotates where the monitor
would. The resolved number is written into the state file, because a run that
spans hours must not have its threshold drift underneath it when the config is
edited.

## Things that can bite

* **The first spawn is a real CLI call.** Nothing here validates that
  `claude` or `codex` is installed before it runs; the adapter's `detect()` is
  not consulted by the driver. A missing binary surfaces as a failed spawn,
  twice, and then `failed`.
* **`--resume` semantics belong to the tool.** If the tool rejects an id the
  driver cannot tell that from a tool that crashed; both are a failed round.
* **Tokens are read from the store, not from the spawn output.** A session whose
  store has not been flushed yet reads as 0, which resumes rather than rotates.
  That is the conservative direction: the cost is one wasted round, not lost
  work.
