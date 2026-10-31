# Publish checklist

For the maintainer, before the repository goes public. Work top to bottom; each
step is verifiable and none of them are optional.

## 1. Clean up the tree

```sh
git status                     # nothing unexpected staged or untracked
python3 -m compileall -q custos
python3 tools/sanitize_check.py .
python3 -m custos demo
python3 -m custos scan
```

All four must be clean. `sanitize_check.py` reports `0 hit(s)`; `demo` must end
with the summary line and exit 0; `scan` must not traceback even on a machine
with no agents installed (`HOME=/tmp/empty python3 -m custos scan`).

## 2. Check the sanitisation rules cover your fork

The scanner ships with rules for the machine this was developed on: usernames,
home paths, internal service names, ports, chat identifiers, credential shapes.
If you developed on a different machine, **add your own rules** before
publishing, using the same obfuscated form so the scanner can scan itself:

```sh
python3 tools/sanitize_check.py --list-rules     # rule names and descriptions
python3 tools/sanitize_check.py --json           # machine-readable, safe to paste
```

Add entries to `_RULE_SPECS`. Then plant a file containing your own strings and
confirm the scanner catches it before you trust a clean run.

## 3. Repository name and description

Suggested: **`custos`**, with the description:

> Automatic context-window monitoring & session rotation for AI coding agents.

Topics (GitHub, up to 20):

```
ai-agents  context-window  session-management  claude-code  codex  opencode
llm  cli  python  developer-tools  automation  observability  context-engineering
```

Skip topics that imply an official integration ("anthropic", "openai") -- these
are third-party adapters reading local files, and saying so is part of the
project's honesty about verification levels.

## 4. License and attribution

- `LICENSE` is MIT with a placeholder copyright holder. Put a real name or
  organisation in it, or leave it as the project name -- but decide deliberately.
- No code was copied from another project; the adapters were written against
  observed local data and public documentation. If you add anything derived from
  elsewhere, record the source and licence.

## 5. Git identity

Commits should not carry a personal or machine identity you did not choose.
Check before the first push:

```sh
git config user.name
git config user.email
git log --format='%an <%ae>' | sort -u
```

If the history carries an identity you would rather not publish, either set a
repository-local identity and rewrite, or start a fresh repository with a single
initial commit. A neutral placeholder is fine:

```sh
git config user.name  "custos"
git config user.email "custos@users.noreply.github.com"
```

## 6. Final scan

Run the scanner one more time after every other step, including after editing
docs and this file:

```sh
python3 tools/sanitize_check.py .
echo "exit: $?"        # must be 0
```

Then read the diff of the last few commits. A scanner catches patterns, not
judgement: check for anything that identifies a person, a machine or a private
project.

## 7. Optional: a screenshot

The demo's output is the best screenshot the project has:

```sh
python3 -m custos demo --keep
```

Capture the terminal, trim to the threshold/handover/gate/alert lines, and save
as `docs/demo.png`. Do not include the temporary directory path at the top of the
output if it contains anything identifying; on macOS it is under `/var/folders/`
with a random name, which is fine.

## 8. Publish

```sh
git remote add origin git@github.com:<owner>/custos.git
git branch -M main
git push -u origin main
```

Then, on the repository:

- set the description and topics from step 3;
- confirm the license is detected as MIT;
- pin an issue explaining the verification levels if you want contributions,
  since "what does doc-verified mean" is the first question the support matrix
  invites.

## 9. After publishing

- **Do not** tag a release until the CI workflow passes on a clean checkout.
- Add a `SECURITY.md` if you want a private reporting channel rather than the
  issue tracker.
- Re-run `sanitize_check.py` before every release, not just this one.
