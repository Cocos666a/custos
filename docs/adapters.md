# Adapters

An adapter knows one agent tool: where it keeps its sessions, how to read the
context occupancy, and how to run a one-shot prompt for the gate. Everything
above that layer works on a `Reading` and never sees a tool-specific format.

## Support matrix

| Agent | Reading source | Verification | Rotation | Gate |
| --- | --- | --- | --- | --- |
| Claude Code | `~/.claude/projects/**/*.jsonl` message usage | **verified** | assisted | `claude -p` |
| Codex | `~/.codex/sessions/**` rollout token counts | **verified** | assisted | `codex exec` |
| OpenCode | `~/.local/share/opencode/opencode.db` (read-only) | **verified** | assisted | `opencode run` |
| Pi | `~/.pi/agent/sessions` | **doc-verified** | assisted | `pi --print` |
| Generic | a file or a command you configure | **experimental** | configured command, else assisted | configured command |
| *(any tool)* | pushed via `custos report` | **experimental** | assisted | not available |

**What the levels mean.**

- **verified** -- the occupancy semantics were checked against real local data
  from that tool, and the field conventions are documented in the adapter source
  with the numbers that established them.
- **doc-verified** -- the paths, file layout and flags come from the tool's
  published documentation. No local install was available to confirm the field
  names inside a usage record, so the adapter searches for them instead of
  hard-coding them and fails loudly rather than inventing a number.
- **experimental** -- correctness depends on configuration you wrote. The
  adapter is a mechanism, not a verified integration.

## Occupancy semantics, and the one trap in each

Every adapter answers the same question: **how big is the prompt the next turn
would send?** Getting that wrong by a constant factor is the most likely way to
build a monitor that never fires or always fires, so each adapter documents how
it decided.

### Claude Code -- `input_tokens` is the whole prompt

The tempting sum is `input_tokens + cache_read + cache_creation`. It is wrong by
roughly a factor of two. Evidence from a real transcript:

```
first record:  input_tokens=59151  cache_read=0
next record:   input_tokens=59394  cache_read=59136
```

The prompt grew by about 250 tokens, not 59000. So `input_tokens` is the whole
prompt and `cache_read` is a component of it. Across 4151 local usage records the
convention holds 87.0% of the time, and 0.3% are written the other way round.
The adapter takes `max(input_tokens, cache_read + cache_creation)`, which is
correct under both conventions and never double-counts.

### Codex -- `last_token_usage`, never `total_token_usage`

A rollout records both. `total_token_usage` is cumulative over the session: one
local rollout reached 37494149, which is not a window size by any reading.
`last_token_usage.input_tokens` is the occupancy of the most recent turn --
144167 in the same session. The adapter reads the last one, and takes the window
from the tool's own `model_context_window` when the rollout records it
(258400 there) rather than from configuration.

### OpenCode -- a message, not the session

`session.tokens_input` is a sum over every message in the session: 2853144 in
one local session, against a final assistant message of 2062. The adapter reads
the newest assistant message and adds `input + cache.read`, which is the prompt
that was actually sent.

### Pi -- searched, not assumed

The adapter reads the session directory and the `--print` flag from the
documentation. It does not hard-code the field names inside a usage record,
because no local install was available to confirm them; it searches each record
for a recognised token-count key and reports a read failure when it finds none.
A wrong number is worse than an honest error here.

### Generic -- your problem, stated plainly

The adapter reads a JSON object from a file or a command and looks for a token
count under `tokens`, `input_tokens`, `prompt_tokens` or `total_tokens`, and a
window under `window`, `context_window`, `contextWindow` or `max_tokens`. If the
source reports a cumulative number under one of those names, the adapter will
believe it; that is the price of being configuration-driven, and the reason the
verification level says so.

```yaml
adapters:
  generic:
    enabled: true
    sessions:
      - id: my-agent-main
        window: 128000
        source:
          type: file
          path: ~/.my-agent/usage.json
      - id: my-agent-worker
        source:
          type: command
          cmd: ["my-agent", "usage", "--json"]
          timeout: 15
```

A file source parses the whole file as JSON; if that fails it uses the last JSON
object in the file, which covers both "a status file" and "an append-only log of
readings". A command source runs with a cleaned environment and a hard timeout.

## Push mode

A tool that already knows its own token count does not need an adapter. Report
the reading and it is stored, merged, and preferred over anything inferred:

```sh
custos report --tool my-agent --session abc123 --tokens 190000 --window 200000
```

A pushed reading wins over an adapter reading for the same session, because it
was measured rather than reconstructed. `scan` prints which source won:
`my-agent abc123: report wins over external`.

Three patterns cover most tools:

**1. Wrapper script.** The tool does not report usage, but you control how it is
launched.

```sh
#!/bin/sh
# Launch the agent, then report what the session ended up costing.
my-agent "$@"
status=$?
tokens=$(my-agent usage --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["tokens"])')
[ -n "$tokens" ] && custos report --tool my-agent --session "$(my-agent current-session)" --tokens "$tokens" --window 128000
exit $status
```

**2. Hook.** The tool can run a command at a lifecycle point, which is the best
case: the number is exact and arrives without a wrapper.

```sh
# ~/.my-agent/hooks/post-turn.sh  (tool-specific mechanism)
custos report --tool my-agent --session "$MY_AGENT_SESSION_ID" \
  --tokens "$MY_AGENT_CONTEXT_TOKENS" --window "$MY_AGENT_CONTEXT_WINDOW"
```

**3. Periodic collection.** The tool writes usage somewhere you can poll, but
only while it is running.

```sh
# crontab: */2 * * * * /path/to/collect-usage.sh
for session_file in ~/.my-agent/sessions/*.json; do
  id=$(basename "$session_file" .json)
  tokens=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["context_tokens"])' "$session_file")
  custos report --tool my-agent --session "$id" --tokens "$tokens" --window 128000
done
```

Use `--compressed` when the tool has already compacted the session. That is a
trigger on its own regardless of the token count, because a session that has
started discarding history is already past the point the tool exists to catch.

Use `--source hook` when the reading comes from the tool's own hook rather than
from a script you wrote; both sort above adapter readings, and the label is what
appears in `scan`.

## Adding your own adapter

An adapter is one module and one line. Here is a complete one for a tool that
writes a status file:

```python
# custos/adapters/my_agent.py
"""Adapter for My Agent."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ..core.usage import Reading
from ..core.util import read_json
from .base import VERIFIED, Adapter, PromptResult, SessionRef


class MyAgentAdapter(Adapter):
    name = "my_agent"
    VERIFICATION = VERIFIED          # only say this once you have checked it
    VERIFICATION_NOTE = "occupancy from the context_tokens field of the status file"

    def store_dir(self) -> str:
        return self.path_setting("sessions_dir", "~/.my-agent/sessions")

    def detect(self) -> bool:
        return os.path.isdir(self.store_dir())

    def sessions(self, limit: int = 20) -> List[SessionRef]:
        directory = self.store_dir()
        if not os.path.isdir(directory):
            raise self._no_sessions("no session directory")
        refs = [
            SessionRef(
                session=name[:-5],
                path=os.path.join(directory, name),
                updated=os.path.getmtime(os.path.join(directory, name)),
                tool=self.name,
            )
            for name in os.listdir(directory) if name.endswith(".json")
        ]
        if not refs:
            raise self._no_sessions("no session files found")
        refs.sort(key=lambda ref: ref.updated, reverse=True)
        return refs[:limit]

    def read_usage(self, session: SessionRef) -> Reading:
        payload = read_json(session.path, default=None)
        if not isinstance(payload, dict):
            raise self._read_failed("status file is not readable JSON", session.session)
        tokens = payload.get("context_tokens")
        if not isinstance(tokens, int):
            raise self._read_failed("no integer context_tokens field", session.session)
        return Reading(
            tool=self.name,
            session=session.session,
            tokens=tokens,
            window=payload.get("context_window") or self.window_setting(),
            source="session-store",
            ts=session.updated,
        )

    def one_shot_command(self) -> List[str]:
        return ["my-agent", "run"]

    def run_prompt(self, prompt, timeout=240.0, cwd=None, extra_args=None) -> PromptResult:
        cmd = list(self.one_shot_command()) + list(extra_args or []) + [prompt]
        rc, stdout, stderr, timed_out = self._spawn(cmd, timeout, cwd)
        return PromptResult(text=(stdout or "").strip(), rc=rc, timed_out=timed_out,
                            stderr=(stderr or "").strip()[:2000])

    def capabilities(self) -> Dict[str, Any]:
        caps = super().capabilities()
        caps.update({"history": False, "run_prompt": True})
        return caps
```

Then register it:

```python
# custos/adapters/__init__.py
from .my_agent import MyAgentAdapter

ADAPTERS = {
    "claude_code": ClaudeCodeAdapter,
    # ...
    "my_agent": MyAgentAdapter,      # <- the one line
}
```

That is the whole contract. Four things are worth stating explicitly:

- **Read-only on foreign stores.** Never write to another tool's files. The
  OpenCode adapter opens SQLite with `mode=ro` for exactly this reason.
- **Never raise past the boundary.** Raise `AdapterNotInstalled`, `NoSessions`
  or `ReadFailed`; anything else aborts a scheduled run.
- **State the verification level honestly.** `VERIFIED` means you checked the
  occupancy semantics against real data. Claiming more than you verified is
  worse than admitting the gap, because the reader cannot tell the difference.
- **Set `BUSY_DETECTION = True` only if `busy()` really can tell.** Returning
  `None` from an adapter that claims detection means "the check failed", and the
  pipeline will defer. Returning `None` from one that does not claim detection
  means "this tool has no such concept", and the pipeline proceeds on the
  activity window.
