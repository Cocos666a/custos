#!/usr/bin/env bash
#
# custos uninstaller.
#
# Removes the installed package and the launcher. The configuration directory is
# kept by default, because it holds the question bank you wrote, the handover
# snapshots, and the state that stops the tool from repeating itself. Pass
# --purge to remove it as well.
#
#   bash uninstall.sh [--prefix DIR] [--purge] [--dry-run] [--yes]

set -eu

PROGRAM="custos"
PREFIX="${HOME}/.local"
PURGE=0
DRY_RUN=0
ASSUME_YES=0
CONFIG_DIR="${HOME}/.config/${PROGRAM}"

usage() {
    cat <<'USAGE'
Usage: bash uninstall.sh [options]

Options:
  --prefix DIR   installation prefix used at install time (default: ~/.local)
  --purge        also remove the configuration directory (handovers included)
  --dry-run      print what would happen; remove nothing
  --yes          do not prompt
  -h, --help     show this message
USAGE
}

note() { printf '  %s\n' "$*"; }
fail() { printf '%s: %s\n' "${PROGRAM}" "$*" >&2; exit 1; }

run() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '  [dry-run] %s\n' "$*"
    else
        "$@"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --prefix)   shift; [ $# -gt 0 ] || fail "--prefix needs a directory"; PREFIX="$1" ;;
        --prefix=*) PREFIX="${1#*=}" ;;
        --purge)    PURGE=1 ;;
        --dry-run|-n) DRY_RUN=1 ;;
        --yes|-y)   ASSUME_YES=1 ;;
        -h|--help)  usage; exit 0 ;;
        *)          usage >&2; fail "unknown option: $1" ;;
    esac
    shift
done

LIB_DIR="${PREFIX}/share/${PROGRAM}"
WRAPPER="${PREFIX}/bin/${PROGRAM}"

if [ "${ASSUME_YES}" -ne 1 ] && [ "${PURGE}" -eq 1 ]; then
    if [ -t 0 ]; then
        printf 'Remove %s as well, including handovers and state? [y/N] ' "${CONFIG_DIR}"
        read -r reply
        case "${reply}" in
            y|Y|yes|YES) ;;
            *) PURGE=0; note "keeping the configuration directory" ;;
        esac
    else
        fail "no terminal to confirm the purge on; re-run with --yes"
    fi
fi

printf '%s uninstaller\n' "${PROGRAM}"
REMOVED=0

if [ -e "${WRAPPER}" ]; then
    run rm -f "${WRAPPER}"
    note "removed ${WRAPPER}"
    REMOVED=1
else
    note "no launcher at ${WRAPPER}"
fi

if [ -d "${LIB_DIR}" ]; then
    run rm -rf "${LIB_DIR}"
    note "removed ${LIB_DIR}"
    REMOVED=1
else
    note "no package directory at ${LIB_DIR}"
fi

if [ "${PURGE}" -eq 1 ]; then
    if [ -d "${CONFIG_DIR}" ]; then
        run rm -rf "${CONFIG_DIR}"
        note "removed ${CONFIG_DIR}"
        REMOVED=1
    else
        note "no configuration directory at ${CONFIG_DIR}"
    fi
else
    if [ -d "${CONFIG_DIR}" ]; then
        note "kept ${CONFIG_DIR} (config, question bank, handovers, state)"
        note "remove it yourself, or re-run with --purge"
    fi
fi

if [ "${REMOVED}" -eq 0 ]; then
    note "nothing was installed under ${PREFIX}"
fi

if [ "${DRY_RUN}" -eq 1 ]; then
    printf '\nthis was a dry run; nothing was removed\n'
else
    printf '\ndone\n'
fi
