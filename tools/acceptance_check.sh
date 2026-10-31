#!/bin/bash
# The acceptance run, in one command:
#
#   bash tools/acceptance_check.sh
#
# Runs the project's own commands against the current checkout and prints one
# PASS/FAIL line each, so "is this still good?" is one command with one answer.
#
# The suite runs on two interpreters on purpose: the 3.9 floor the project
# promises, and a current one with PyYAML. The YAML tests skip on an interpreter
# that cannot read YAML, and a skip is not a pass -- CI's extras job exists for
# the same reason. uv supplies both without installing anything here; when uv is
# missing the script says so rather than quietly testing one version.
set -u

cd "$(dirname "$0")/.." || exit 1
REPO="$PWD"
export PYTHONPATH="$REPO${PYTHONPATH:+:$PYTHONPATH}"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/custos-acceptance-XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

FAIL=0
say() { printf '%-52s %s\n' "$1" "$2"; }
check() { if [ "$2" = "0" ]; then say "$1" "PASS"; else say "$1" "FAIL (rc=$2)"; FAIL=1; fi }
uv_pytest() { uv run --no-project --python "$1" --with pytest ${2:+--with "$2"} \
    python -m pytest -q -p no:cacheprovider; }

echo "checkout $(git rev-parse --short HEAD 2>/dev/null || echo '(not a git checkout)')"
echo "tree     $(git status --porcelain 2>/dev/null | wc -l | tr -d ' ') uncommitted path(s)"
echo

# --- the example files the installer seeds --------------------------------
python3 -c "import json,sys; [json.load(open(p)) for p in sys.argv[1:]]" \
  "$REPO/config/config.example.json" "$REPO/config/questions.example.json"
check "config/questions.example.json parse as JSON" "$?"

# --- the suite, on the two interpreters that matter ------------------------
if command -v uv >/dev/null 2>&1; then
    out=$(uv_pytest 3.9 "" 2>&1); rc=$?
    check "pytest, python 3.9 (no extras)  [$(printf '%s' "$out" | tail -1 | cut -c1-34)]" "$rc"
    out=$(uv_pytest 3.13 pyyaml 2>&1); rc=$?
    check "pytest, python 3.13 + PyYAML    [$(printf '%s' "$out" | tail -1 | cut -c1-34)]" "$rc"
else
    say "pytest (uv not found)" "SKIP -- uv is how this script gets both interpreters"
fi

# --- the repository's own hygiene rules ------------------------------------
python3 tools/sanitize_check.py . > "$TMP/sanitize.log" 2>&1
check "sanitize_check  [$(cut -c1-34 "$TMP/sanitize.log")]" "$?"
python3 -m compileall -q custos tests tools
check "compileall" "$?"

# --- an install a stranger would actually get ------------------------------
# A bare python3 has no PyYAML, and that is the case that matters: whatever the
# installer seeds has to be readable by the interpreter it installed against.
mkdir -p "$TMP/home"
HOME="$TMP/home" bash install.sh --yes --no-daemon --prefix "$TMP/prefix" \
  > "$TMP/install.log" 2>&1
check "install.sh into a sandbox HOME" "$?"
seeded=$(cd "$TMP/home/.config/custos" 2>/dev/null && ls -1 | tr '\n' ' ')
say "  seeded" "${seeded:-(nothing)}"
HOME="$TMP/home" "$TMP/prefix/bin/custos" doctor > "$TMP/doctor.log" 2>&1
check "the installed entry point runs (doctor)" "$?"
grep -q "all checks passed" "$TMP/doctor.log" || { say "  doctor output" "UNEXPECTED"; FAIL=1; }

# --- the end-to-end scenario, on synthetic sessions ------------------------
python3 -m custos demo > "$TMP/demo.log" 2>&1
check "demo, whole pipeline  [$(tail -1 "$TMP/demo.log" | cut -c1-30)]" "$?"

echo
[ "$FAIL" = "0" ] && echo "ALL CHECKS PASSED" || echo "SOMETHING FAILED"
exit "$FAIL"
