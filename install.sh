#!/usr/bin/env bash
#
# custos installer.
#
# Copies the package into a lib directory, writes a small wrapper onto PATH,
# seeds the configuration on first run, and prints how to schedule it. It never
# edits an existing configuration file, never installs a scheduler, and never
# needs root: everything lands under --prefix (default ~/.local) and
# $HOME/.config.
#
#   bash install.sh [--yes] [--dry-run] [--prefix DIR] [--no-daemon] [--with-skills]
#
# Every action is idempotent, so re-running it after an update is the supported
# way to upgrade.

set -eu

PROGRAM="custos"
PREFIX="${HOME}/.local"
ASSUME_YES=0
DRY_RUN=0
NO_DAEMON=0
WITH_SKILLS=0

CONFIG_DIR="${HOME}/.config/${PROGRAM}"

usage() {
    cat <<'USAGE'
Usage: bash install.sh [options]

Options:
  --yes            do not prompt (required when there is no terminal)
  --dry-run        print what would happen; change nothing
  --prefix DIR     installation prefix (default: ~/.local)
  --no-daemon      skip the scheduler templates
  --with-skills    also copy skills/ into ~/.claude/skills/ when that exists
  -h, --help       show this message
USAGE
}

log()  { printf '%s\n' "$*"; }
note() { printf '  %s\n' "$*"; }
fail() { printf '%s: %s\n' "${PROGRAM}" "$*" >&2; exit 1; }

run() {
    # Execute unless this is a dry run, in which case echo what would run.
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '  [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y)      ASSUME_YES=1 ;;
        --dry-run|-n)  DRY_RUN=1 ;;
        --prefix)      shift; [ $# -gt 0 ] || fail "--prefix needs a directory"; PREFIX="$1" ;;
        --prefix=*)    PREFIX="${1#*=}" ;;
        --no-daemon)   NO_DAEMON=1 ;;
        --with-skills) WITH_SKILLS=1 ;;
        -h|--help)     usage; exit 0 ;;
        *)             usage >&2; fail "unknown option: $1" ;;
    esac
    shift
done

# ---------------------------------------------------------------- sanity

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
[ -f "${SCRIPT_DIR}/pyproject.toml" ] || fail "run this from the repository root (pyproject.toml not found)"
[ -d "${SCRIPT_DIR}/custos" ] || fail "the custos package is missing from ${SCRIPT_DIR}"

if [ "${ASSUME_YES}" -ne 1 ]; then
    if [ -t 0 ]; then
        printf 'Install %s into %s? [y/N] ' "${PROGRAM}" "${PREFIX}"
        read -r reply
        case "${reply}" in
            y|Y|yes|YES) ;;
            *) log "aborted"; exit 0 ;;
        esac
    else
        fail "no terminal to confirm on; re-run with --yes"
    fi
fi

PYTHON_BIN="${CUSTOS_PYTHON:-}"
if [ -z "${PYTHON_BIN}" ]; then
    PYTHON_BIN=$(command -v python3 || true)
fi
[ -n "${PYTHON_BIN}" ] || fail "python3 was not found on PATH; set CUSTOS_PYTHON to a Python 3.9+ interpreter"

PY_OK=$("${PYTHON_BIN}" -c 'import sys; print(1 if sys.version_info[:2] >= (3, 9) else 0)' 2>/dev/null || echo 0)
[ "${PY_OK}" = "1" ] || fail "${PYTHON_BIN} is older than Python 3.9 (found $("${PYTHON_BIN}" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo unknown))"

LIB_DIR="${PREFIX}/share/${PROGRAM}"
BIN_DIR="${PREFIX}/bin"
WRAPPER="${BIN_DIR}/${PROGRAM}"

log "${PROGRAM} installer"
note "prefix : ${PREFIX}"
note "python : ${PYTHON_BIN}"
note "lib    : ${LIB_DIR}"
note "wrapper: ${WRAPPER}"
note "config : ${CONFIG_DIR}"
log ""

# ------------------------------------------------------------- writability

if [ ! -d "${PREFIX}" ]; then
    PARENT=$(dirname "${PREFIX}")
    [ -w "${PARENT}" ] || fail "cannot create ${PREFIX}: ${PARENT} is not writable (use --prefix with a writable path, or install elsewhere)"
elif [ ! -w "${PREFIX}" ]; then
    fail "${PREFIX} is not writable by $(id -un); use --prefix with a writable path"
fi

# ------------------------------------------------------------------- copy

log "1. package"
if [ "${DRY_RUN}" -eq 1 ]; then
    note "would copy custos/ into ${LIB_DIR}/"
else
    mkdir -p "${LIB_DIR}"
fi
# Replace the package wholesale rather than merging: a file removed upstream
# would otherwise linger and shadow the new one.
if [ -d "${LIB_DIR}/custos" ]; then
    run rm -rf "${LIB_DIR}/custos"
fi
if [ "${DRY_RUN}" -eq 1 ]; then
    note "would copy ${SCRIPT_DIR}/custos -> ${LIB_DIR}/custos"
else
    cp -R "${SCRIPT_DIR}/custos" "${LIB_DIR}/custos"
    # Byte-compile so the first run is not slower than the rest. A failure here
    # is not fatal: a read-only or unusual filesystem still runs from source.
    "${PYTHON_BIN}" -m compileall -q "${LIB_DIR}/custos" >/dev/null 2>&1 || \
        note "note: could not pre-compile the package; it will run from source"
fi
run cp "${SCRIPT_DIR}/pyproject.toml" "${LIB_DIR}/pyproject.toml"
run cp "${SCRIPT_DIR}/README.md" "${LIB_DIR}/README.md" 2>/dev/null || true
run cp "${SCRIPT_DIR}/LICENSE" "${LIB_DIR}/LICENSE" 2>/dev/null || true
if [ -d "${SCRIPT_DIR}/config" ]; then
    run mkdir -p "${LIB_DIR}/config"
    run cp -R "${SCRIPT_DIR}/config/." "${LIB_DIR}/config/"
fi
log ""

# ----------------------------------------------------------------- wrapper

log "2. command"
if [ "${DRY_RUN}" -eq 1 ]; then
    note "would write ${WRAPPER}"
else
    mkdir -p "${BIN_DIR}"
    # POSIX sh, not bash: the wrapper has to work under whatever /bin/sh is.
    cat > "${WRAPPER}" <<WRAPPER_EOF
#!/bin/sh
# ${PROGRAM} launcher. Generated by install.sh; edit install.sh, not this file.
PYTHONPATH="${LIB_DIR}\${PYTHONPATH:+:\${PYTHONPATH}}"
export PYTHONPATH
exec "${PYTHON_BIN}" -m custos "\$@"
WRAPPER_EOF
    chmod 755 "${WRAPPER}"
fi
log ""

# ------------------------------------------------------------------ config

log "3. configuration"
if [ "${DRY_RUN}" -eq 1 ]; then
    note "would create ${CONFIG_DIR} and seed config/questions if absent"
else
    mkdir -p "${CONFIG_DIR}"
fi

seed_if_absent() {
    source_file="$1"
    target_name="$2"
    target="${CONFIG_DIR}/${target_name}"
    if [ -e "${target}" ]; then
        note "kept existing ${target}"
        return
    fi
    if [ -f "${source_file}" ]; then
        run cp "${source_file}" "${target}"
        note "created ${target}"
    fi
}

# The config file has to be readable by the interpreter this is installing
# against. PyYAML is optional and a stock `python3` usually does not have it, so
# the extension follows what is available: seeding config.yaml for an
# interpreter without PyYAML installs a tool that refuses to start.
YAML_AVAILABLE=$("${PYTHON_BIN}" -c 'import yaml' >/dev/null 2>&1 && echo 1 || echo 0)
if [ "${YAML_AVAILABLE}" = "1" ]; then
    seed_if_absent "${SCRIPT_DIR}/config/config.example.yaml" "config.yaml"
    seed_if_absent "${SCRIPT_DIR}/config/questions.example.yaml" "questions.yaml"
else
    seed_if_absent "${SCRIPT_DIR}/config/config.example.json" "config.json"
    seed_if_absent "${SCRIPT_DIR}/config/questions.example.json" "questions.json"
    note "PyYAML is not installed for ${PYTHON_BIN}; seeding JSON instead (same settings)"
    note "the commented original is config/config.example.yaml in this repository"
fi
log ""

# ------------------------------------------------------------------ agents

log "4. detected agents"
FOUND_ANY=0
detect_agent() {
    binary="$1"
    label="$2"
    data_dir="$3"
    found=""
    if command -v "${binary}" >/dev/null 2>&1; then
        found="binary ${binary}"
    fi
    if [ -n "${data_dir}" ] && [ -d "${data_dir}" ]; then
        if [ -n "${found}" ]; then found="${found}, ${data_dir}"; else found="${data_dir}"; fi
    fi
    if [ -n "${found}" ]; then
        FOUND_ANY=1
        note "${label}: ${found}"
    fi
}
detect_agent claude   "Claude Code" "${HOME}/.claude"
detect_agent codex    "Codex"       "${HOME}/.codex"
detect_agent opencode "OpenCode"    "${HOME}/.local/share/opencode"
detect_agent pi       "Pi"          "${HOME}/.pi"

if [ "${FOUND_ANY}" -eq 0 ]; then
    note "none found. custos still works with the generic adapter and with"
    note "readings pushed by your own hooks; see docs/adapters.md."
else
    log ""
    note "Wire up push mode (optional, one line per tool):"
    note "  ${PROGRAM} report --tool <name> --session <id> --tokens <n> --window <m>"
    note "  see docs/adapters.md -> \"Push mode\" for wrapper, hook and cron patterns."
fi
log ""

# ----------------------------------------------------------------- daemon

if [ "${NO_DAEMON}" -eq 0 ]; then
    log "5. scheduler (pick one; nothing is installed for you)"
    log ""
    log "   macOS launchd -- save as ~/Library/LaunchAgents/com.custos.monitor.plist"
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.custos.monitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>${WRAPPER}</string>
    <string>monitor</string>
    <string>--once</string>
  </array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><false/>
  <key>StandardErrorPath</key><string>${HOME}/Library/Logs/custos.log</string>
</dict>
</plist>
PLIST
    log "   then: launchctl load ~/Library/LaunchAgents/com.custos.monitor.plist"
    log ""
    log "   cron -- run 'crontab -e' and add:"
    log "   */5 * * * * ${WRAPPER} monitor --once"
    log ""
    log "   systemd -- save as ~/.config/systemd/user/custos.service and"
    log "   ~/.config/systemd/user/custos.timer, then 'systemctl --user"
    log "   enable --now custos.timer'"
    cat <<UNIT
# custos.service
[Unit]
Description=custos context monitor

[Service]
Type=oneshot
ExecStart=${WRAPPER} monitor --once
UNIT
    cat <<UNIT2
# custos.timer
[Unit]
Description=Run custos every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
UNIT2
    log ""
    note "Only stdout carries messages, so cron mails you when something happens"
    note "and stays silent otherwise. Add a webhook in config.yaml for chat."
    log ""
fi

# ----------------------------------------------------------------- skills

if [ "${WITH_SKILLS}" -eq 1 ]; then
    log "6. skills"
    SKILL_SRC="${SCRIPT_DIR}/skills/context-rotation"
    if [ ! -d "${SKILL_SRC}" ]; then
        note "no skills/ directory in ${SCRIPT_DIR}; nothing to copy"
    elif [ -d "${HOME}/.claude" ]; then
        SKILL_DST="${HOME}/.claude/skills/context-rotation"
        if [ -e "${SKILL_DST}" ]; then
            note "kept existing ${SKILL_DST} (remove it first to refresh)"
        else
            run mkdir -p "${HOME}/.claude/skills"
            run cp -R "${SKILL_SRC}" "${SKILL_DST}"
            note "copied to ${SKILL_DST}"
        fi
    else
        note "~/.claude not found; to use the skill with another agent, copy"
        note "${SKILL_SRC} into that agent's skills directory yourself."
    fi
    log ""
fi

# --------------------------------------------------------------- summary

log "done"
if [ "${DRY_RUN}" -eq 1 ]; then
    log ""
    log "this was a dry run; nothing was written"
    exit 0
fi

log ""
log "next steps:"
case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) note "add ${BIN_DIR} to PATH: export PATH=\"${BIN_DIR}:\$PATH\"" ;;
esac
note "${WRAPPER} doctor      # check the installation"
note "${WRAPPER} scan        # read your sessions"
note "${WRAPPER} demo        # see the whole pipeline, no agent needed"
note "${WRAPPER} monitor --once   # what the scheduler will run"
log ""
note "stop everything at any time with: touch ${CONFIG_DIR}/DISABLE"
note "remove it with: bash uninstall.sh"
